from unittest import TestCase

from src.discovery_next.candidate_preselector import CandidatePreselector
from src.discovery_next.candidate_ranker import CandidateRanker
from src.discovery_next.domain_allocator import DomainAllocator
from src.discovery_next.query_performance import performance_score
from src.discovery_next.query_planner import QueryPlanner, query_similarity


DOMAINS = [
    {"id": "minecraft", "label": "Minecraft", "description": "game", "entities": ["minecraft", "survival"], "formats": ["challenge"], "enabled": True},
    {"id": "science", "label": "Science", "description": "science", "entities": ["physics", "experiment"], "formats": ["experiment"], "enabled": True},
]


class DiscoveryNextPhase2Tests(TestCase):
    def test_strong_preference_keeps_exploration_and_cap(self):
        result = DomainAllocator({}).allocate({"domain_preferences": {"minecraft": 100, "science": 1}}, DOMAINS, [])
        self.assertGreater(result["domain_allocation"]["minecraft"], result["domain_allocation"].get("science", 0))
        self.assertLess(result["domain_allocation"]["minecraft"], 1)
        self.assertGreater(result["exploration_ratio"], 0)

    def test_manual_focus_is_round_local(self):
        profile = {"domain_preferences": {"science": 10}}
        before = dict(profile)
        result = DomainAllocator({}).allocate(profile, DOMAINS, [], manual_focus=["minecraft"])
        self.assertGreater(result["domain_allocation"]["minecraft"], result["domain_allocation"].get("science", 0))
        self.assertEqual(profile, before)

    def test_query_semantic_duplicate(self):
        self.assertGreater(query_similarity("minecraft hardcore survival", "hardcore minecraft survival"), .9)
        plan = QueryPlanner({}).plan({}, DOMAINS[:1], {"minecraft": .8, "exploration": .2}, [], 6)
        normalized = [row["normalized_query"] for row in plan]
        self.assertEqual(len(normalized), len(set(normalized)))

    def test_query_efficiency_promotes_quality_not_return_volume(self):
        bad = {"returned_count": 50, "new_unique_count": 2, "eligible_count": 1}
        good = {"returned_count": 20, "new_unique_count": 16, "eligible_count": 12, "ai_selected_count": 10,
                "ai_high_quality_count": 8, "download_count": 3}
        self.assertGreater(performance_score(good), performance_score(bad))

    def test_potential_is_not_hot_protected_first(self):
        low_pop = {"video_id": "a", "ai_potential_score": 95, "view_count": 8000, "views_per_hour": 20, "age_hours": 10}
        hot = {"video_id": "b", "ai_potential_score": 40, "view_count": 1500000, "views_per_hour": 10000, "age_hours": 10, "hot_protected": True}
        self.assertEqual(CandidateRanker({}).rank_potential([hot, low_pop], 2)[0]["video_id"], "a")
        self.assertEqual(CandidateRanker({}).rank_hot([hot, low_pop], 2)[0]["video_id"], "b")

    def test_ai_budget_is_stratified(self):
        rows = [{"video_id": str(i), "title": f"topic {i}", "channel_id": f"c{i}", "views_per_hour": 10000 - i,
                 "query_relevance": 1 if i >= 10 else .1, "user_preference_match": .5, "novelty": .5,
                 "matched_queries": [str(i)], "matched_pack_ids": ["minecraft"], "domain_pool_count": 20} for i in range(20)]
        chosen, buckets = CandidatePreselector({}).select(rows, 10)
        self.assertEqual(len(chosen), 10)
        self.assertGreater(len([key for key, value in buckets.items() if value]), 1)
        self.assertTrue(any(int(row["video_id"]) >= 10 for row in chosen))
