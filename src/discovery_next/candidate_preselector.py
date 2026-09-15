"""Stratified candidate selection before the finite local-AI budget."""
from __future__ import annotations

import math
import re
from collections import Counter


def semantic_key(text: str) -> str:
    stop = {"the", "a", "an", "to", "of", "in", "on", "and", "with", "for"}
    return " ".join(sorted({word for word in re.findall(r"[a-z0-9]+", text.casefold()) if word not in stop})[:10])


class CandidatePreselector:
    DEFAULT_BUCKETS = {"hot": .16, "relevance": .20, "user_preference": .16, "novelty": .12,
                       "emerging": .10, "exploration": .10, "low_pop_high_relevance": .12, "rare_domain": .04}

    def __init__(self, config=None):
        cfg = (config or {}).get("discovery_next", {})
        raw = cfg.get("candidate_bucket_ratios", self.DEFAULT_BUCKETS)
        total = sum(max(0, float(value)) for value in raw.values()) or 1
        self.ratios = {key: max(0, float(value)) / total for key, value in raw.items()}
        self.channel_limit = int(cfg.get("preselection_channel_limit", 4))
        self.semantic_limit = int(cfg.get("preselection_semantic_cluster_limit", 3))
        self.domain_limit = int(cfg.get("preselection_domain_limit", 18))
        self.query_limit = int(cfg.get("preselection_query_limit", 12))
        self.format_limit = int(cfg.get("preselection_format_limit", 12))

    @staticmethod
    def _bucket_score(row, bucket):
        heat = math.log1p(row.get("views_per_hour", 0)) / 12
        relevance = row.get("query_relevance", .5)
        preference = row.get("user_preference_match", .5)
        novelty = row.get("novelty", .5)
        values = {"hot": heat, "relevance": relevance, "user_preference": preference, "novelty": novelty,
                  "emerging": row.get("emerging_fit", 0), "exploration": row.get("exploration_fit", 0),
                  "low_pop_high_relevance": relevance + preference - heat,
                  "rare_domain": 1 / max(1, row.get("domain_pool_count", 1))}
        return values.get(bucket, 0)

    def select(self, rows, budget):
        if budget <= 0:
            return [], {}
        chosen, chosen_ids = [], set()
        channels, clusters, domains, queries, formats = Counter(), Counter(), Counter(), Counter(), Counter()
        counts = {key: int(budget * ratio) for key, ratio in self.ratios.items()}
        for key in sorted(counts, key=lambda k: self.ratios[k], reverse=True)[: budget - sum(counts.values())]:
            counts[key] += 1
        actual = Counter()
        def accept(row):
            channel = row.get("channel_id") or row.get("channel_title")
            cluster = row.get("semantic_cluster") or semantic_key(row.get("title", ""))
            primary_query = (row.get("matched_queries") or [""])[0]
            domain = (row.get("matched_pack_ids") or [""])[0]
            format_key = (row.get("matched_formats") or [""])[0]
            if (row["video_id"] in chosen_ids or channels[channel] >= self.channel_limit
                    or clusters[cluster] >= self.semantic_limit or domains[domain] >= self.domain_limit
                    or queries[primary_query] >= self.query_limit or formats[format_key] >= self.format_limit):
                return False
            chosen.append(row); chosen_ids.add(row["video_id"])
            channels[channel] += 1; clusters[cluster] += 1
            domains[domain] += 1; queries[primary_query] += 1; formats[format_key] += 1
            row["preselection_score"] = round(max(self._bucket_score(row, key) for key in self.ratios), 4)
            return True
        for key, limit in counts.items():
            for row in sorted(rows, key=lambda item: self._bucket_score(item, key), reverse=True):
                if actual[key] >= limit:
                    break
                if accept(row):
                    actual[key] += 1
        for row in sorted(rows, key=lambda item: max(self._bucket_score(item, key) for key in self.ratios), reverse=True):
            if len(chosen) >= budget:
                break
            if accept(row):
                actual["diversity_backfill"] += 1
        return chosen, dict(actual)
