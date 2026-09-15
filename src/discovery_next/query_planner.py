"""Intent-first query planning with Qwen proposals and deterministic safeguards."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone

from src.discovery.ollama_client import OllamaDiscoveryClient, OllamaSettings
from .domain_allocator import allocate_counts

QUERY_PLAN_SCHEMA = {"type": "object", "properties": {"queries": {"type": "array", "items": {
    "type": "object", "properties": {"domain": {"type": "string"}, "intent": {"type": "string"},
    "query": {"type": "string"}}, "required": ["domain", "intent", "query"]}}}, "required": ["queries"]}


def normalize_query(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(value).casefold())
    return " ".join(sorted(dict.fromkeys(tokens)))


def query_similarity(left: str, right: str) -> float:
    a, b = set(normalize_query(left).split()), set(normalize_query(right).split())
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


class QueryPlanner:
    INTENTS = ("core_topic", "learned_concept", "emerging_topic", "challenge", "experiment",
               "transformation", "result_oriented", "story_driven", "comparison", "underrated_niche",
               "new_release", "creator_pattern", "coverage", "exploration", "cross_domain")

    def __init__(self, config: dict | None = None, client=None):
        self.config = config or {}
        cfg = self.config.get("discovery_next", {})
        self.threshold = float(cfg.get("query_diversity_threshold", 0.72))
        self.cooldown_hours = int(cfg.get("query_cooldown_hours", 72))
        self.min_exploration = float(cfg.get("exploration_min", 0.12))
        settings = OllamaSettings.from_config(self.config)
        self.client = client or (OllamaDiscoveryClient(settings) if settings.enabled and settings.query_planning_enabled else None)

    def _history_index(self, history):
        index = {}
        for row in history:
            normalized = row.get("normalized_query") or normalize_query(row.get("query", ""))
            if not normalized:
                continue
            prior = index.setdefault(normalized, {"records": [], "performance_score": 0.0, "use_count": 0,
                                                  "last_used_at": "", "cooldown_until": None})
            prior["records"].append(row)
            prior["performance_score"] = max(prior["performance_score"], float(row.get("performance_score", 0)))
            prior["use_count"] += 1
            prior["last_used_at"] = max(prior["last_used_at"], str(row.get("run_at", "")))
            cooldown = row.get("cooldown_until")
            if cooldown:
                prior["cooldown_until"] = max(prior["cooldown_until"] or "", str(cooldown))
        return index

    def _ai_candidates(self, profile, domains, allocation, history, count):
        if not self.client or count <= 0:
            return []
        compact_history = [{k: row.get(k) for k in ("query", "domain", "intent", "performance_score", "status",
                                                    "new_unique_count", "eligible_count", "ai_high_quality_count")}
                           for row in history[:160]]
        successful = [row for row in history if float(row.get("performance_score", 0)) > 0.25][:40]
        failed = [row for row in history if row.get("status") == "failed" or float(row.get("performance_score", 0)) < 0][:40]
        prompt = {
            "domain_allocation": allocation,
            "domains": [{"id": d["id"], "label": d.get("label", d["id"]), "description": d["description"],
                         "entities": d.get("entities", [])[:16], "formats": d.get("formats", []),
                         "positive_traits": d.get("positive_traits", []),
                         "negative_intents": d.get("negative_intents", [])} for d in domains],
            "profile": {
                "long_term_domain_preferences": profile.get("domain_preferences", {}),
                "recent_domain_preferences": profile.get("recent_domain_preferences", {}),
                "explicit_domain_preferences": profile.get("explicit_domain_preferences", {}),
                "emerging_interests": profile.get("emerging_interests", [])[:24],
                "preferred_content_traits": profile.get("preferred_content_traits", {}),
                "channel_preferences": dict(list(profile.get("channel_preferences", {}).items())[:20]),
                "negative_preferences": profile.get("negative_preferences", {}),
                "negative_domain_preferences": profile.get("negative_domain_preferences", {}),
                "domain_dimensions": {d["id"]: {key: list(value.items()) for key, value in
                                                profile.get("domain_dimensions", {}).get(d["id"], {}).items()}
                                      for d in domains},
            },
            "recent_successful_queries": successful,
            "recent_failed_queries": failed,
            "requested_candidates": count,
        }
        result = self.client._chat([
            {"role": "system", "content": "Plan natural YouTube search queries from user evidence. First choose a distinct search intent, then write a concise English query. Avoid keyword permutations, near duplicates, negative preferences, and invented fixed templates. Mix exploitation, emerging, coverage, exploration and cross-domain ideas. Return only the schema."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ], QUERY_PLAN_SCHEMA)
        return [{"domain": str(row.get("domain", "")), "intent": str(row.get("intent", "core_topic")),
                 "query": str(row.get("query", "")), "recall_source": "ai_query", "generator": "qwen"}
                for row in result.get("queries", []) if isinstance(row, dict)]

    def _deterministic_candidates(self, profile, domains, allocation, history):
        by_id = {d["id"]: d for d in domains}
        candidates = []
        # Learned search concepts and entities are evidence, not a fixed keyword pool.
        for domain, share in sorted(allocation.items(), key=lambda item: -item[1]):
            if domain == "exploration" or domain not in by_id:
                continue
            dimensions = profile.get("domain_dimensions", {}).get(domain, {})
            concepts = sorted(dimensions.get("search_concepts", {}).items(), key=lambda item: -item[1])
            entities = sorted(dimensions.get("entities", {}).items(), key=lambda item: -item[1])
            fallback_entities = list(by_id[domain].get("entities", []))
            seen = set()
            for value, source, intent, entity in [*[(k, "learned_concept", "learned_concept", k) for k, _ in concepts[:10]],
                                                   *[(k, "exploitation", "core_topic", k) for k, _ in entities[:8]],
                                                   *[(k, "coverage", "coverage", k) for k in fallback_entities[:8]]]:
                key = normalize_query(str(value))
                if not key or key in seen:
                    continue
                seen.add(key)
                candidates.append({"domain": domain, "intent": intent, "query": str(value),
                                   "entity": str(entity), "recall_source": source, "generator": "learned"})
            # Under-covered single-domain rounds still need a full recall plan. These
            # coverage variants are only the fallback for missing learned evidence, not
            # the primary query generation strategy.
            formats = by_id[domain].get("formats", [])
            for entity in fallback_entities[:5]:
                for format_id in formats[:4]:
                    format_label = format_id.replace("_", " ")
                    query = f"{entity} {format_label}".strip()
                    key = normalize_query(query)
                    if key and key not in seen:
                        seen.add(key)
                        candidates.append({"domain": domain, "intent": "coverage", "query": query, "entity": entity,
                                           "recall_source": "coverage", "generator": "coverage_fallback"})
        # Emerging interests are their own recall path.
        for item in profile.get("emerging_interests", [])[:32]:
            concept = str(item.get("concept", "")).strip()
            if not concept:
                continue
            domain = next((d for d in by_id if d.replace("_", " ") in concept.casefold()), next(iter(by_id), ""))
            candidates.append({"domain": domain, "intent": "emerging_topic", "query": concept,
                               "recall_source": "emerging", "generator": "learned"})
        # Preferred channels are a creator-pattern recall source, not a text query.
        for channel, weight in sorted(profile.get("channel_preferences", {}).items(), key=lambda item: -item[1]):
            if weight > 0:
                candidates.append({"domain": max((d for d in allocation if d != "exploration"), key=allocation.get, default=next(iter(by_id), "")),
                                   "intent": "creator_pattern", "query": "", "channel_id": channel,
                                   "recall_source": "preferred_channel", "generator": "channel"})
        # Cross-domain combinations are cheap, deterministic exploration.
        if len(by_id) > 1:
            top = sorted((d for d in allocation if d != "exploration"), key=allocation.get, reverse=True)[:4]
            for left, right in zip(top, top[1:]):
                query = f"{by_id[left].get('label', left)} {by_id[right].get('label', right)}"
                candidates.append({"domain": left, "intent": "cross_domain", "query": query,
                                   "recall_source": "cross_domain", "generator": "planner"})
        return candidates

    def plan(self, profile: dict, domains: list[dict], allocation: dict[str, float], history: list[dict], budget: int, *,
             now=None, recall_source_weights=None):
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        by_id = {d["id"]: d for d in domains}
        if not by_id:
            return []
        budget = max(1, int(budget))
        prior_by_norm = self._history_index(history)
        candidates = self._deterministic_candidates(profile, domains, allocation, history)
        try:
            ai_request = min(budget * 3, 160)
            candidates.extend(self._ai_candidates(profile, domains, allocation, history, ai_request))
        except Exception:
            # Query planning is an enhancement; deterministic learned recall remains available.
            pass

        domain_counts = allocate_counts({k: v for k, v in allocation.items() if k != "exploration"}, budget)
        remaining = dict(domain_counts)
        source_weights = recall_source_weights or self.config.get("discovery_next", {}).get("recall_source_budget", {
            "exploitation": .22, "learned_concept": .18, "emerging": .14, "preferred_channel": .10,
            "ai_query": .20, "coverage": .07, "exploration": .06, "cross_domain": .03})
        source_counts = allocate_counts(source_weights, budget)
        used_source = Counter()
        negatives = profile.get("negative_preferences", {})
        negative_domains = profile.get("negative_domain_preferences", {})

        def score(row):
            normalized = normalize_query(row.get("query", ""))
            prior = prior_by_norm.get(normalized, {})
            performance_prior = float(prior.get("performance_score", 0))
            source_bonus = {"ai_query": .28, "learned_concept": .24, "emerging": .22, "preferred_channel": .20,
                            "exploitation": .18, "coverage": .14, "cross_domain": .12, "exploration": .16}.get(row["recall_source"], .1)
            term_penalty = sum(float(v) for term, v in negatives.items() if term in (row.get("query", "") or "").casefold())
            domain_penalty = float(negative_domains.get(row.get("domain", ""), 0))
            prior_novelty = 0 if prior else .08
            return source_bonus + min(1.2, performance_prior) + prior_novelty - .25 * term_penalty - .20 * min(1.0, domain_penalty)

        selected, used_norms = [], set()
        # First pass respects both domain and recall-source budgets.
        for row in sorted(candidates, key=score, reverse=True):
            if len(selected) >= budget:
                break
            domain = row.get("domain")
            if domain not in by_id:
                continue
            source = row.get("recall_source", "exploitation")
            if remaining.get(domain, 0) <= 0 or used_source[source] >= source_counts.get(source, 0):
                continue
            query = str(row.get("query", "")).strip()[:120]
            normalized = normalize_query(query or ("channel " + row.get("channel_id", "")))
            if not normalized or normalized in used_norms:
                continue
            if any(query_similarity(normalized, old) >= self.threshold for old in used_norms):
                continue
            prior = prior_by_norm.get(normalized, {})
            if prior.get("cooldown_until") and str(prior["cooldown_until"]) > now.isoformat():
                continue
            item = {**row, "query": query, "query_text": query, "normalized_query": normalized,
                    "semantic_cluster": normalized, "performance_prior": round(float(prior.get("performance_score", 0)), 4),
                    "novel": not bool(prior), "novel_or_reuse": "reuse" if prior else "novel",
                    "last_used_at": str(prior.get("last_used_at", "")), "use_count": int(prior.get("use_count", 0)),
                    "source": source, "order": "date" if source in {"emerging", "preferred_channel"} else "relevance"}
            selected.append(item)
            used_norms.add(normalized)
            remaining[domain] -= 1
            used_source[source] += 1

        # Dynamic exploration reserve. Manual focus still keeps a small reserve unless it is
        # an explicit single-domain extreme; Auto Discovery always has non-zero exploration.
        exploration_target = max(1, round(budget * max(self.min_exploration, float(allocation.get("exploration", self.min_exploration)))))
        exploration_target = min(budget - len(selected), exploration_target)
        exploration_candidates = [r for r in candidates if r.get("recall_source") in {"exploration", "coverage", "cross_domain"}]
        for row in sorted(exploration_candidates, key=score, reverse=True):
            if exploration_target <= 0 or len(selected) >= budget:
                break
            domain = row.get("domain")
            if domain not in by_id:
                continue
            query = str(row.get("query", "")).strip()[:120]
            normalized = normalize_query(query)
            if not normalized or normalized in used_norms:
                continue
            if any(query_similarity(normalized, old) >= self.threshold for old in used_norms):
                continue
            prior = prior_by_norm.get(normalized, {})
            if prior.get("cooldown_until") and str(prior["cooldown_until"]) > now.isoformat():
                continue
            item = {**row, "query": query, "query_text": query, "normalized_query": normalized,
                    "semantic_cluster": normalized, "performance_prior": round(float(prior.get("performance_score", 0)), 4),
                    "novel": not bool(prior), "novel_or_reuse": "reuse" if prior else "novel",
                    "last_used_at": str(prior.get("last_used_at", "")), "use_count": int(prior.get("use_count", 0)),
                    "source": row.get("recall_source", "exploration"),
                    "recall_source": row.get("recall_source", "exploration"), "order": "relevance"}
            selected.append(item)
            used_norms.add(normalized)
            exploration_target -= 1

        # Backfill with the best remaining diverse candidates; never pad with permutations.
        for row in sorted(candidates, key=score, reverse=True):
            if len(selected) >= budget:
                break
            domain = row.get("domain")
            if domain not in by_id:
                continue
            query = str(row.get("query", "")).strip()[:120]
            normalized = normalize_query(query or ("channel " + row.get("channel_id", "")))
            if not normalized or normalized in used_norms:
                continue
            if any(query_similarity(normalized, old) >= self.threshold for old in used_norms):
                continue
            prior = prior_by_norm.get(normalized, {})
            if prior.get("cooldown_until") and str(prior["cooldown_until"]) > now.isoformat():
                continue
            item = {**row, "query": query, "query_text": query, "normalized_query": normalized,
                    "semantic_cluster": normalized, "performance_prior": round(float(prior.get("performance_score", 0)), 4),
                    "novel": not bool(prior), "novel_or_reuse": "reuse" if prior else "novel",
                    "last_used_at": str(prior.get("last_used_at", "")), "use_count": int(prior.get("use_count", 0)),
                    "source": row.get("recall_source", "exploitation"), "order": "relevance"}
            selected.append(item)
            used_norms.add(normalized)
        return selected[:budget]
