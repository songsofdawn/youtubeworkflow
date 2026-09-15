"""Independent Hot and Potential scoring followed by result diversification."""
from __future__ import annotations

import math
from collections import Counter

from .candidate_preselector import semantic_key


class CandidateRanker:
    POTENTIAL_DEFAULTS = {"ai": .38, "user": .14, "relevance": .10, "novelty": .10,
                          "emerging": .07, "localization": .06, "diversity": .05, "heat": .02,
                          "story": .08}
    HOT_DEFAULTS = {"velocity": .36, "views": .18, "freshness": .16, "engagement": .12,
                    "ai": .12, "user": .06}

    def __init__(self, config=None):
        cfg = (config or {}).get("discovery_next", {})
        self.potential_weights = self._normalize_weights(cfg.get("potential_ranking_weights", self.POTENTIAL_DEFAULTS))
        self.hot_weights = self._normalize_weights(cfg.get("hot_ranking_weights", self.HOT_DEFAULTS))
        self.channel_limit = int(cfg.get("channel_repetition_limit", (config or {}).get("max_per_channel", 2)))
        self.semantic_limit = int(cfg.get("semantic_cluster_repetition_limit", 2))
        self.domain_factor = float(cfg.get("domain_diversity_factor", .08))
        self.semantic_factor = float(cfg.get("semantic_diversity_factor", .12))

    @staticmethod
    def _normalize_weights(weights):
        total = sum(max(0.0, float(value)) for value in weights.values()) or 1.0
        return {key: max(0.0, float(value)) / total for key, value in weights.items()}

    @staticmethod
    def _ai(row):
        return float(row.get("ai_potential_score", row.get("opportunity_score", 50))) / 100

    def potential_score(self, row):
        values = {"ai": self._ai(row), "user": row.get("user_preference_match", .5), "relevance": row.get("query_relevance", .5),
                  "novelty": row.get("novelty", .5), "emerging": row.get("emerging_fit", 0),
                  "localization": row.get("localization_value", .5), "diversity": row.get("semantic_uniqueness", .5),
                  "story": row.get("story_strength", .5), "visual": row.get("visual_payoff", .5),
                  "knowledge": row.get("knowledge_value", .5), "result": row.get("result_payoff", .5),
                  "heat": min(1, math.log1p(row.get("views_per_hour", 0)) / 12)}
        weighted_values = {
            "ai": values["ai"], "user": values["user"], "relevance": values["relevance"],
            "novelty": values["novelty"], "emerging": values["emerging"],
            "localization": values["localization"], "diversity": values["diversity"],
            "story": .35 * values["story"] + .15 * values["visual"] + .15 * values["knowledge"] + .1 * values["result"],
            "heat": values["heat"],
        }
        penalty = row.get("negative_preference_penalty", 0) + row.get("duplicate_penalty", 0)
        return 100 * sum(float(self.potential_weights.get(k, 0)) * weighted_values[k] for k in weighted_values) - penalty

    def hot_score(self, row):
        views = min(1, math.log1p(row.get("view_count", 0)) / 16)
        velocity = min(1, math.log1p(row.get("views_per_hour", 0)) / 11)
        freshness = max(0, 1 - row.get("age_hours", 999) / 720)
        engagement = min(1, (row.get("like_count", 0) + 2 * row.get("comment_count", 0)) / max(1, row.get("view_count", 0)) * 30)
        values = {"velocity": velocity, "views": views, "freshness": freshness, "engagement": engagement,
                  "ai": self._ai(row), "user": row.get("user_preference_match", .5)}
        quality_floor_penalty = max(0.0, 0.35 - self._ai(row)) * 35
        return 100 * sum(float(self.hot_weights.get(k, 0)) * v for k, v in values.items()) - quality_floor_penalty

    def _rank(self, rows, mode, limit):
        for row in rows:
            row["potential_ranking_score"] = round(self.potential_score(row), 3)
            row["hot_ranking_score"] = round(self.hot_score(row), 3)
        key = "hot_ranking_score" if mode == "hot" else "potential_ranking_score"
        pool, output = list(rows), []
        channels, domains, clusters = Counter(), Counter(), Counter()
        while pool and len(output) < limit:
            def adjusted(row):
                channel = row.get("channel_id") or row.get("channel_title")
                domain = (row.get("matched_pack_ids") or [""])[0]
                cluster = row.get("semantic_cluster") or semantic_key(row.get("title", ""))
                return row[key] - 100 * (self.domain_factor * domains[domain] + self.semantic_factor * clusters[cluster] + .10 * channels[channel])
            row = max(pool, key=adjusted); pool.remove(row)
            channel = row.get("channel_id") or row.get("channel_title")
            cluster = row.get("semantic_cluster") or semantic_key(row.get("title", ""))
            if channels[channel] >= self.channel_limit or clusters[cluster] >= self.semantic_limit:
                continue
            domain = (row.get("matched_pack_ids") or [""])[0]
            channels[channel] += 1; clusters[cluster] += 1; domains[domain] += 1
            output.append(row)
        return output

    def rank_hot(self, rows, limit):
        return self._rank(rows, "hot", limit)

    def rank_potential(self, rows, limit):
        return self._rank(rows, "potential", limit)
