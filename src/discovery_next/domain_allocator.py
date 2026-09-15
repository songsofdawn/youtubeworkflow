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
        weights = cfg.get("domain_allocation_weights", {})
        self.weights = {
            "historical_seed": float(weights.get("historical_seed", .15)),
            "recent_behavior": float(weights.get("recent_behavior", .25)),
            "real_download": float(weights.get("real_download", .15)),
            "explicit_interest": float(weights.get("explicit_interest", .22)),
            "strong_interest": float(weights.get("strong_interest", .13)),
            "emerging": float(weights.get("emerging", .12)),
            "performance": float(weights.get("performance", .08)),
            "coverage": float(weights.get("coverage", .10)),
            "negative": float(weights.get("negative", .30)),
        }

    def allocate(self, profile: dict, domains: list[dict], history: list[dict], *, manual_focus: list[str] | None = None) -> dict:
        enabled = [domain for domain in domains if domain.get("enabled", True)]
        ids = [domain["id"] for domain in enabled]
        if not ids:
            raise ValueError("没有可用的发现领域")
        preference = {key: max(0.0, float(profile.get("domain_preferences", {}).get(key, 0))) for key in ids}
        recent = {key: max(0.0, float(profile.get("recent_domain_preferences", {}).get(key, 0))) for key in ids}
        downloads = {key: max(0.0, float(profile.get("download_domain_preferences", {}).get(key, 0))) for key in ids}
        explicit = {key: max(0.0, float(profile.get("explicit_domain_preferences", {}).get(key, 0))) for key in ids}
        strong = {key: max(0.0, float(profile.get("strong_interest_domain_preferences", {}).get(key, 0))) for key in ids}
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
            score = float(row.get("performance_score", 0))
            if row.get("status") != "failed":
                successes[domain] += max(0.0, score)
        pref_n, recent_n, download_n, explicit_n, strong_n, emerging_n = (
            _norm(preference), _norm(recent), _norm(downloads), _norm(explicit), _norm(strong), _norm(dict(emerging)))
        reasons = {}
        raw = {}
        for domain in ids:
            performance = max(0.0, min(1.0, successes[domain] / max(1, executions[domain])))
            coverage = 1.0 / math.sqrt(1 + executions[domain])
            negative = max(0.0, float(profile.get("negative_domain_preferences", {}).get(domain, 0)))
            components = {
                "historical_seed": self.weights["historical_seed"] * pref_n.get(domain, 0),
                "recent_behavior": self.weights["recent_behavior"] * recent_n.get(domain, 0),
                "real_download": self.weights["real_download"] * download_n.get(domain, 0),
                "explicit_interest": self.weights["explicit_interest"] * explicit_n.get(domain, 0),
                "strong_interest": self.weights["strong_interest"] * strong_n.get(domain, 0),
                "emerging": self.weights["emerging"] * emerging_n.get(domain, 0),
                "performance": self.weights["performance"] * performance,
                "coverage": self.weights["coverage"] * coverage,
                "negative": -self.weights["negative"] * min(1.0, negative),
            }
            raw[domain] = max(self.floor, sum(components.values()))
            reasons[domain] = {key: round(value, 6) for key, value in components.items()}
        concentration = 1.0 - _entropy(recent or preference or raw)
        recent_history = history[-200:]
        explored = sum(1 for row in recent_history if row.get("recall_source") in {"exploration", "coverage", "cross_domain"})
        exploration_success = sum(max(0.0, float(row.get("performance_score", 0))) for row in recent_history
                                  if row.get("recall_source") in {"exploration", "coverage", "cross_domain"}) / max(1, explored)
        recent_domain_diversity = _entropy({key: sum(1 for row in recent_history if row.get("domain") == key) for key in ids})
        emerging_weight = sum(max(0.0, float(item.get("weight", 0))) for item in profile.get("emerging_interests", []))
        novelty_bonus = min(.06, .015 * len(profile.get("emerging_interests", []))) + (0 if recent_domain_diversity < .55 else .04)
        exploration = self.minimum_exploration + .10 * concentration + (0.05 if explored < 8 else 0)
        exploration += .10 * max(0.0, 1.0 - exploration_success) + min(.06, .015 * min(4, emerging_weight))
        exploration += novelty_bonus
        exploration = max(self.minimum_exploration, min(self.maximum_exploration, exploration))
        focus = [item for item in (manual_focus or []) if item in raw]
        if focus:
            raw = {key: (1.0 if key in focus else self.floor * 0.1) for key in raw}
            exploration = self.minimum_exploration
        normalized = _norm(raw)
        # A hard ceiling must not flatten the learned ranking. Preserve rank order
        # inside the capped band so a strong preference still gets more budget.
        values = list(normalized.values())
        low, high = min(values), max(values)
        span = max(high - low, 1e-9)
        capped = {}
        for key, value in normalized.items():
            if value >= self.ceiling:
                capped[key] = self.ceiling * (0.82 + 0.18 * (value - low) / span)
            else:
                capped[key] = value
        normalized = _norm(capped)
        domain_share = 1.0 - exploration
        allocation = {key: round(value * domain_share, 6) for key, value in normalized.items()}
        allocation = {key: value for key, value in allocation.items() if value >= self.floor / 2}
        allocation["exploration"] = round(exploration, 6)
        return {"domain_allocation": allocation, "exploration_ratio": exploration,
                "signals": {"profile_concentration": round(concentration, 4),
                            "recent_exploration_runs": explored,
                            "recent_exploration_success": round(exploration_success, 4),
                            "recent_domain_diversity": round(recent_domain_diversity, 4),
                            "emerging_weight": round(emerging_weight, 4),
                            "manual_focus": focus},
                "allocation_reasons": reasons}
