"""Allocate a discovery round's domain budget from learned evidence."""
from __future__ import annotations

import math
from collections import Counter


def _norm(values: dict[str, float]) -> dict[str, float]:
    total = sum(max(0.0, float(value)) for value in values.values())
    if total <= 0:
        return {key: 1.0 / max(1, len(values)) for key in values}
    return {key: max(0.0, float(value)) / total for key, value in values.items()}


def _entropy(values: dict[str, float]) -> float:
    probabilities = [value for value in _norm(values).values() if value > 0]
    if len(probabilities) <= 1:
        return 0.0
    return -sum(p * math.log(p) for p in probabilities) / math.log(len(probabilities))


def allocate_counts(weights: dict[str, float], budget: int) -> dict[str, int]:
    """Largest-remainder allocation; its sum is exactly ``budget``."""
    weights = _norm(weights)
    exact = {key: value * max(0, budget) for key, value in weights.items()}
    counts = {key: int(value) for key, value in exact.items()}
    for key in sorted(exact, key=lambda item: exact[item] - counts[item], reverse=True)[: budget - sum(counts.values())]:
        counts[key] += 1
    return counts


class DomainAllocator:
    def __init__(self, config: dict | None = None):
        cfg = (config or {}).get("discovery_next", {})
        self.minimum_exploration = float(cfg.get("exploration_min", 0.12))
        self.maximum_exploration = float(cfg.get("exploration_max", 0.30))
        self.floor = float(cfg.get("domain_allocation_floor", 0.01))
        self.ceiling = float(cfg.get("domain_allocation_ceiling", 0.38))

    def allocate(self, profile: dict, domains: list[dict], history: list[dict], *, manual_focus: list[str] | None = None) -> dict:
        enabled = [domain for domain in domains if domain.get("enabled", True)]
        ids = [domain["id"] for domain in enabled]
        if not ids:
            raise ValueError("没有可用的发现领域")
        preference = {key: float(profile.get("domain_preferences", {}).get(key, 0)) for key in ids}
        recent = {key: float(profile.get("recent_domain_preferences", {}).get(key, 0)) for key in ids}
        emerging = Counter()
        for item in profile.get("emerging_interests", []):
            concept = str(item.get("concept", "")).casefold()
            for domain in enabled:
                terms = [domain["id"].replace("_", " "), *domain.get("entities", [])]
                if any(str(term).casefold() in concept or concept in str(term).casefold() for term in terms if concept):
                    emerging[domain["id"]] += max(0.0, float(item.get("weight", 0)))
        executions, successes = Counter(), Counter()
        for row in history:
            domain = row.get("domain")
            if domain not in ids:
                continue
            executions[domain] += 1
            successes[domain] += float(row.get("performance_score", 0))
        pref_n, recent_n, emerging_n = _norm(preference), _norm(recent), _norm(dict(emerging))
        raw = {}
        for domain in ids:
            performance = max(-0.5, min(1.0, successes[domain] / max(1, executions[domain])))
            coverage = 1.0 / math.sqrt(1 + executions[domain])
            negative = float(profile.get("negative_domain_preferences", {}).get(domain, 0))
            raw[domain] = max(self.floor, 0.45 * pref_n.get(domain, 0) + 0.25 * recent_n.get(domain, 0)
                              + 0.12 * emerging_n.get(domain, 0) + 0.10 * max(0, performance)
                              + 0.08 * coverage - 0.15 * negative)
        concentration = 1.0 - _entropy(preference or raw)
        explored = sum(1 for row in history[-100:] if row.get("recall_source") in {"exploration", "coverage", "cross_domain"})
        exploration = self.minimum_exploration + 0.14 * concentration + (0.04 if explored < 5 else 0)
        exploration = max(self.minimum_exploration, min(self.maximum_exploration, exploration))
        focus = [item for item in (manual_focus or []) if item in raw]
        if focus:
            raw = {key: (1.0 if key in focus else self.floor * 0.1) for key in raw}
            exploration = self.minimum_exploration
        normalized = _norm(raw)
        # Iterative cap prevents one learned interest from swallowing the round.
        capped = {key: min(self.ceiling, value) for key, value in normalized.items()}
        normalized = _norm(capped)
        domain_share = 1.0 - exploration
        allocation = {key: round(value * domain_share, 6) for key, value in normalized.items() if value * domain_share >= self.floor / 2}
        allocation["exploration"] = round(exploration, 6)
        return {"domain_allocation": allocation, "exploration_ratio": exploration,
                "signals": {"profile_concentration": round(concentration, 4), "recent_exploration_runs": explored,
                            "manual_focus": focus}}
