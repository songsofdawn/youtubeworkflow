"""Intent-first query planning with Qwen proposals and deterministic safeguards."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from src.discovery.ollama_client import OllamaDiscoveryClient, OllamaSettings

QUERY_PLAN_SCHEMA = {"type": "object", "properties": {"queries": {"type": "array", "items": {
    "type": "object", "properties": {"domain": {"type": "string"}, "intent": {"type": "string"},
    "query": {"type": "string"}}, "required": ["domain", "intent", "query"]}}}, "required": ["queries"]}


def normalize_query(value: str) -> str:
    tokens = re.findall(r"[a-z0-9]+", str(value).casefold())
    return " ".join(sorted(dict.fromkeys(tokens)))


def query_similarity(left: str, right: str) -> float:
    a, b = set(normalize_query(left).split()), set(normalize_query(right).split())
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
        settings = OllamaSettings.from_config(self.config)
        self.client = client or (OllamaDiscoveryClient(settings) if settings.enabled and settings.query_planning_enabled else None)

    def _ai_candidates(self, profile, domains, allocation, history, count):
        if not self.client or count <= 0:
            return []
        compact_history = [{k: row.get(k) for k in ("query", "domain", "intent", "performance_score", "status")} for row in history[:80]]
        prompt = {"domain_allocation": allocation, "domains": [{"id": d["id"], "description": d["description"],
                  "entities": d.get("entities", [])[:12], "formats": d.get("formats", [])} for d in domains],
                  "emerging_interests": profile.get("emerging_interests", [])[:20],
                  "search_concepts": {d["id"]: list(profile.get("domain_dimensions", {}).get(d["id"], {}).get("search_concepts", {}))[:16] for d in domains},
                  "recent_query_performance": compact_history, "negative_preferences": profile.get("negative_preferences", {}),
                  "requested_candidates": count}
        result = self.client._chat([
            {"role": "system", "content": "Plan natural YouTube search queries from user evidence. First choose a distinct search intent, then write a concise English query. Avoid keyword permutations, near duplicates, negative preferences, and invented fixed templates. Mix exploitation, emerging, coverage, exploration and cross-domain ideas. Return only the schema."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ], QUERY_PLAN_SCHEMA)
        return [{"domain": str(row.get("domain", "")), "intent": str(row.get("intent", "core_topic")),
                 "query": str(row.get("query", "")), "recall_source": "ai_query", "generator": "qwen"}
                for row in result.get("queries", []) if isinstance(row, dict)]

    def plan(self, profile: dict, domains: list[dict], allocation: dict[str, float], history: list[dict], budget: int, *, now=None):
        now = now or datetime.now(timezone.utc)
        by_id = {d["id"]: d for d in domains}
        candidates = []
        # Learned evidence creates recall paths even when Qwen is temporarily unavailable.
        for domain, share in sorted(allocation.items(), key=lambda item: -item[1]):
            if domain == "exploration" or domain not in by_id:
                continue
            dimensions = profile.get("domain_dimensions", {}).get(domain, {})
            concepts = sorted(dimensions.get("search_concepts", {}).items(), key=lambda item: -item[1])
            entities = sorted(dimensions.get("entities", {}).items(), key=lambda item: -item[1])
            fallback_entities = list(by_id[domain].get("entities", []))
            for value, source, intent in [*[(k, "learned_concept", "learned_concept") for k, _ in concepts[:8]],
                                          *[(k, "exploitation", "core_topic") for k, _ in entities[:6]],
                                          *[(k, "coverage", "coverage") for k in fallback_entities[:6]]]:
                candidates.append({"domain": domain, "intent": intent, "query": str(value), "recall_source": source, "generator": "learned"})
        for item in profile.get("emerging_interests", [])[:24]:
            concept = str(item.get("concept", "")).strip()
            domain = next((d for d in by_id if d.replace("_", " ") in concept.casefold()), next(iter(by_id), ""))
            candidates.append({"domain": domain, "intent": "emerging_topic", "query": concept,
                               "recall_source": "emerging", "generator": "learned"})
        for channel, weight in sorted(profile.get("channel_preferences", {}).items(), key=lambda item: -item[1]):
            if weight > 0:
                candidates.append({"domain": max((d for d in allocation if d != "exploration"), key=allocation.get, default=next(iter(by_id), "")),
                                   "intent": "creator_pattern", "query": "", "channel_id": channel,
                                   "recall_source": "preferred_channel", "generator": "channel"})
        try:
            candidates.extend(self._ai_candidates(profile, domains, allocation, history, max(budget * 2, 24)))
        except Exception:
            # Query planning is an enhancement; deterministic learned recall remains available.
            pass
        if len(by_id) > 1:
            top = sorted((d for d in allocation if d != "exploration"), key=allocation.get, reverse=True)[:4]
            for left, right in zip(top, top[1:]):
                candidates.append({"domain": left, "intent": "cross_domain",
                    "query": f"{by_id[left]['label']} {by_id[right]['label']}", "recall_source": "cross_domain", "generator": "planner"})
        prior_by_norm = {row.get("normalized_query") or normalize_query(row.get("query", "")): row for row in history}
        negatives = profile.get("negative_preferences", {})
        selected = []
        target_counts = __import__("src.discovery_next.domain_allocator", fromlist=["allocate_counts"]).allocate_counts(
            {k: v for k, v in allocation.items() if k != "exploration"}, max(1, budget - round(budget * allocation.get("exploration", 0))))
        remaining = dict(target_counts)
        def score(row):
            prior = prior_by_norm.get(normalize_query(row.get("query", "")), {})
            source_bonus = {"ai_query": .28, "learned_concept": .24, "emerging": .22, "preferred_channel": .20,
                            "exploitation": .18, "coverage": .14, "cross_domain": .12}.get(row["recall_source"], .1)
            penalty = sum(float(v) for term, v in negatives.items() if term in row.get("query", "").casefold())
            return source_bonus + float(prior.get("performance_score", 0)) - .2 * penalty
        for row in sorted(candidates, key=score, reverse=True):
            if len(selected) >= budget:
                break
            if row.get("domain") not in by_id or (remaining.get(row["domain"], 0) <= 0 and row["recall_source"] not in {"emerging", "cross_domain"}):
                continue
            query = row.get("query", "").strip()[:120]
            normalized = normalize_query(query or ("channel " + row.get("channel_id", "")))
            if not normalized or any(query_similarity(normalized, item["normalized_query"]) >= self.threshold for item in selected):
                continue
            prior = prior_by_norm.get(normalized, {})
            if prior.get("cooldown_until") and str(prior["cooldown_until"]) > now.isoformat():
                continue
            item = {**row, "query": query, "normalized_query": normalized,
                    "semantic_cluster": normalized, "performance_prior": round(float(prior.get("performance_score", 0)), 4),
                    "novel": not bool(prior), "order": "date" if row["recall_source"] in {"emerging", "preferred_channel"} else "relevance"}
            selected.append(item)
            remaining[row["domain"]] = remaining.get(row["domain"], 0) - 1
        # Reserve dynamic exploration from under-covered taxonomy entities.
        exploration_target = min(budget - len(selected), max(1, round(budget * allocation.get("exploration", 0))))
        used = {item["normalized_query"] for item in selected}
        for domain in sorted(domains, key=lambda d: allocation.get(d["id"], 0)):
            for entity in domain.get("entities", []):
                normalized = normalize_query(entity)
                if normalized and normalized not in used and all(query_similarity(normalized, old) < self.threshold for old in used):
                    selected.append({"domain": domain["id"], "intent": "exploration", "query": entity[:120],
                                     "normalized_query": normalized, "semantic_cluster": normalized, "recall_source": "exploration",
                                     "generator": "coverage", "performance_prior": 0.0, "novel": True, "order": "relevance"})
                    used.add(normalized)
                    exploration_target -= 1
                    if exploration_target <= 0 or len(selected) >= budget:
                        break
            if exploration_target <= 0 or len(selected) >= budget:
                break
        return selected[:budget]
