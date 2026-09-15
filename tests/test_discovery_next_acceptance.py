from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from src.discovery_next.candidate_preselector import CandidatePreselector
from src.discovery_next.candidate_ranker import CandidateRanker
from src.discovery_next.domain_allocator import DomainAllocator
from src.discovery_next.query_performance import QueryPerformance, performance_score
from src.discovery_next.query_planner import QueryPlanner, query_similarity
from src.discovery_next.service import DiscoveryNextService
from src.learning.learning_service import LearningService
from src.learning.storage import atomic_json


VIDEO = "abcdefghijk"
NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)
DOMAINS = [
    {"id": "minecraft", "label": "Minecraft", "description": "game", "entities": ["minecraft", "survival"], "formats": ["challenge"], "enabled": True},
    {"id": "science", "label": "Science", "description": "science", "entities": ["physics", "experiment"], "formats": ["experiment"], "enabled": True},
]


def analysis(video_id=VIDEO):
    return {"video_id": video_id, "domains": [{"name": "minecraft", "score": 0.9}],
            "topics": ["civilization simulation"], "entities": ["civilization", "AI agents"],
            "formats": ["simulation"], "content_traits": {k: 0.8 for k in
                ("novelty", "visual_payoff", "story_strength", "knowledge_value", "localization_value", "result_payoff")},
            "positive_signals": ["visible result"], "negative_signals": [],
            "search_concepts": ["civilization simulation"], "taxonomy_suggestions": []}


def metadata(video_id=VIDEO):
    return {"id": video_id, "title": "Minecraft civilization simulation", "description": "100 players build a civilization",
            "channel_id": "UC" + "a" * 22, "channel": "Simulation Lab"}


class FakeYouTube:
    def __init__(self, *, title="Minecraft civilization simulation", fail_details=False):
        self.calls = []
        self.title = title
        self.fail_details = fail_details

    def get(self, endpoint, params):
        self.calls.append((endpoint, params))
        if endpoint == "search":
            return {"items": [{"id": {"videoId": VIDEO}}]}
        if self.fail_details:
            raise RuntimeError("offline injected failure")
        return {"items": [{"id": VIDEO, "snippet": {"title": self.title, "description": "Minecraft civilization simulation experiment",
            "channelTitle": "Lab", "channelId": "UC" + "a" * 22, "publishedAt": "2026-09-14T12:00:00Z",
            "defaultAudioLanguage": "en", "liveBroadcastContent": "none", "thumbnails": {}},
            "contentDetails": {"duration": "PT10M", "caption": "true"},
            "statistics": {"viewCount": "200000", "likeCount": "1000", "commentCount": "50"},
            "status": {"privacyStatus": "public", "embeddable": True}}]}


class DiscoveryNextAcceptanceTests(TestCase):
    def test_auto_discovery_requires_no_domain_input(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            config = {"discovery_max_search_requests": 3, "discovery_llm": {"enabled": False},
                      "english_only": True, "discovery_min_duration_seconds": 60}
            atomic_json(root / "config/trending_config.json", config)
            result = DiscoveryNextService(root).run(
                youtube=FakeYouTube(), packs=DOMAINS, selected_ids=[], hours=72, per_pack=2,
                config=config, now=NOW, ranking_mode="potential", discovery_scope="auto",
                search_strength="quick")
            self.assertEqual(result["summary"]["discovery_scope"], "auto")
            self.assertGreaterEqual(result["summary"]["search_request_count"], 1)

    def test_strong_preference_is_high_but_not_total(self):
        profile = {"domain_preferences": {"minecraft": 100, "science": 1}}
        result = DomainAllocator({}).allocate(profile, DOMAINS, [])
        self.assertGreater(result["domain_allocation"]["minecraft"], result["domain_allocation"]["science"])
        self.assertLess(result["domain_allocation"]["minecraft"], 1)
        self.assertGreater(result["exploration_ratio"], 0)

    def test_potential_and_hot_rankers_are_separated(self):
        low_pop = {"video_id": "a", "ai_potential_score": 95, "view_count": 8000, "views_per_hour": 20, "age_hours": 10}
        hot = {"video_id": "b", "ai_potential_score": 40, "view_count": 1500000, "views_per_hour": 10000, "age_hours": 10, "hot_protected": True}
        self.assertEqual(CandidateRanker({}).rank_potential([hot, low_pop], 2)[0]["video_id"], "a")
        self.assertEqual(CandidateRanker({}).rank_hot([hot, low_pop], 2)[0]["video_id"], "b")

    def test_ai_budget_is_not_hottest_only(self):
        rows = [{"video_id": str(i), "title": f"topic {i}", "channel_id": f"c{i % 4}",
                 "views_per_hour": 10000 - i, "query_relevance": 1 if i >= 12 else .1,
                 "user_preference_match": .5, "novelty": .5, "matched_queries": [str(i)],
                 "matched_pack_ids": ["minecraft"], "domain_pool_count": 20} for i in range(20)]
        chosen, buckets = CandidatePreselector({}).select(rows, 10)
        self.assertEqual(len(chosen), 10)
        self.assertTrue(any(int(row["video_id"]) >= 12 for row in chosen))
        self.assertGreater(len(buckets), 1)

    def test_semantic_duplicate_queries_are_suppressed(self):
        self.assertGreater(query_similarity("minecraft hardcore survival", "hardcore minecraft survival"), .9)
        plan = QueryPlanner({}).plan({}, DOMAINS[:1], {"minecraft": .8, "exploration": .2}, [], 8)
        norms = [row["normalized_query"] for row in plan]
        self.assertEqual(len(norms), len(set(norms)))

    def test_bad_query_enters_cooldown_and_good_query_is_promoted(self):
        with tempfile.TemporaryDirectory() as name:
            perf = QueryPerformance(Path(name), {"discovery_next": {"bad_query_suppression_threshold": .08}})
            bad = perf.start({"domain": "minecraft", "query": "bad query"}, 72)
            bad.update(status="complete", returned_count=50, new_unique_count=2, eligible_count=1,
                       ai_selected_count=1, ai_high_quality_count=0, shown_count=0)
            perf.save(bad, [])
            history = perf.history()
            self.assertTrue(history[0].get("cooldown_until"))
            plan = QueryPlanner({"discovery_next": {"query_cooldown_hours": 72}}).plan(
                {}, DOMAINS[:1], {"minecraft": .8, "exploration": .2}, history, 4)
            self.assertFalse(any(row["query"] == "bad query" for row in plan))

            good = perf.start({"domain": "minecraft", "query": "good query"}, 72)
            good.update(status="complete", returned_count=20, new_unique_count=16, eligible_count=12,
                        ai_selected_count=10, ai_high_quality_count=8, shown_count=5, download_count=2)
            perf.save(good, [])
            self.assertGreater(performance_score(good), performance_score(bad))

    def test_query_attribution_rewards_only_actual_impression(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            perf = QueryPerformance(root)
            event = SimpleNamespace(event_id="e1", video_id=VIDEO, kind="download")
            with perf.store.connect() as db:
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", ("e1", VIDEO, "download", NOW.isoformat(), "{}", "complete", ""))
                for run_id, run_at, shown in (("r1", NOW.isoformat(), 1), ("r2", NOW.isoformat(), 0)):
                    db.execute("INSERT OR REPLACE INTO query_runs VALUES (?, ?)", (run_id, json.dumps({"run_id": run_id, "run_at": run_at})))
                    db.execute("INSERT OR REPLACE INTO query_hits VALUES (?, ?, ?)", (run_id, VIDEO, shown))
                QueryPerformance.attribute(db, event)
                run = db.execute("SELECT run_id FROM attributions WHERE event_id='e1'").fetchone()[0]
            self.assertEqual(run, "r1")

    def test_emerging_interest_and_negative_feedback_propagate(self):
        profile = {"emerging_interests": [{"concept": "minecraft redstone machines", "weight": 5}],
                   "negative_domain_preferences": {"science": 10}}
        result = DomainAllocator({}).allocate(profile, DOMAINS, [])
        self.assertGreater(result["domain_allocation"]["minecraft"], result["domain_allocation"]["science"])
        plan = QueryPlanner({}).plan({"negative_preferences": {"tutorial": 10}}, DOMAINS,
                                     {"minecraft": .8, "science": .1, "exploration": .1}, [], 8)
        self.assertFalse(any("tutorial" in row["query"] for row in plan))

    def test_manual_focus_does_not_mutate_long_term_profile(self):
        profile = {"domain_preferences": {"science": 10}}
        before = dict(profile)
        DomainAllocator({}).allocate(profile, DOMAINS, [], manual_focus=["minecraft"])
        self.assertEqual(profile, before)

    def test_historical_seed_rebuild_and_recent_override(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            service = LearningService(root, Mock(analyze=Mock(side_effect=lambda row: analysis(row["video_id"]))))
            for video_id in ("11111111111", "22222222222", "33333333333"):
                service.process(service.record(metadata(video_id), source="manual"))
            self.assertEqual(service.rebuild()["sample_size"], 3)

            old_profile = service.rebuild()
            profile = {"domain_preferences": {"old_domain": 100},
                       "recent_domain_preferences": {"new_domain": 100},
                       "download_domain_preferences": {"new_domain": 100},
                       "explicit_domain_preferences": {"new_domain": 100},
                       "strong_interest_domain_preferences": {"new_domain": 100}}
            domains = [{"id": "old_domain", "label": "Old", "description": "old", "entities": ["old"], "formats": ["experiment"], "enabled": True},
                       {"id": "new_domain", "label": "New", "description": "new", "entities": ["new"], "formats": ["experiment"], "enabled": True}]
            alloc = DomainAllocator({}).allocate(profile, domains, [])
            self.assertGreater(alloc["domain_allocation"]["new_domain"], alloc["domain_allocation"]["old_domain"])

    def test_final_candidate_diversity_limits_channel_repetition(self):
        rows = [{"video_id": str(i), "channel_id": "same", "channel_title": "same", "title": f"topic {i}",
                 "views_per_hour": 100 - i, "ai_potential_score": 90 - i, "user_preference_match": .8,
                 "query_relevance": .8, "novelty": .5, "matched_pack_ids": ["minecraft"], "semantic_cluster": f"cluster{i}"} for i in range(10)]
        chosen = CandidateRanker({}).rank_potential(rows, 6)
        self.assertLessEqual(len({row["channel_id"] for row in chosen}), 2)

    def test_low_popularity_high_potential_candidate_reaches_ai_and_final(self):
        low_pop = {"video_id": "low", "title": "rare niche experiment", "channel_id": "rare", "views_per_hour": 1,
                   "query_relevance": .95, "user_preference_match": .9, "novelty": .9, "matched_queries": ["rare"],
                   "matched_pack_ids": ["minecraft"], "domain_pool_count": 2, "ai_potential_score": 95}
        hot = {"video_id": "hot", "title": "viral but shallow", "channel_id": "hot", "views_per_hour": 10000,
               "query_relevance": .1, "user_preference_match": .5, "novelty": .1, "matched_queries": ["hot"],
               "matched_pack_ids": ["minecraft"], "domain_pool_count": 2, "ai_potential_score": 40}
        chosen, buckets = CandidatePreselector({}).select([hot, low_pop], 2)
        self.assertIn("low", [row["video_id"] for row in chosen])
        self.assertGreater(buckets.get("low_pop_high_relevance", 0) + buckets.get("relevance", 0), 0)
        ranked = CandidateRanker({}).rank_potential(chosen, 2)
        self.assertEqual(ranked[0]["video_id"], "low")
