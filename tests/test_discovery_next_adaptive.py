from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from src.discovery_next.candidate_ranker import CandidateRanker
from src.discovery_next.metric_snapshots import MetricSnapshotStore
from src.discovery_next.query_performance import QueryPerformance
from src.discovery_next.recall import RecallExecutor


NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)


class PagedYouTube:
    def __init__(self):
        self.calls = []

    def get(self, endpoint, params):
        if endpoint != "search":
            return {"items": []}
        self.calls.append(params)
        token = params.get("pageToken")
        if token == "p2":
            ids = [f"v2_{i}" for i in range(50)]
            next_token = "p3"
        elif token == "p3":
            ids = [f"v3_{i}" for i in range(10)]
            next_token = None
        else:
            ids = [f"v1_{i}" for i in range(50)]
            next_token = "p2"
        return {"items": [{"id": {"videoId": video_id}} for video_id in ids], "nextPageToken": next_token}


class DiscoveryNextAdaptiveTests(TestCase):
    def config(self):
        return {
            "region_code": "US",
            "language": "en",
            "safe_search": "moderate",
            "discovery_max_pages_per_stream": 3,
            "discovery_next": {
                "adaptive_pagination": {
                    "enabled": True,
                    "page2_min_new_unique": 8,
                    "page2_max_duplicate_rate": 0.7,
                    "page3_min_new_unique": 6,
                    "page3_max_duplicate_rate": 0.7,
                }
            },
        }

    def test_adaptive_pagination_counts_each_page_and_drops_video_embeddable(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            youtube = PagedYouTube()
            executor = RecallExecutor(youtube, QueryPerformance(root), self.config())
            with patch("src.discovery_next.recall.get_video_details", return_value={}):
                resources, records, warnings = executor.execute(
                    [{"query": "minecraft survival", "domain": "minecraft", "intent": "challenge",
                      "recall_source": "exploitation", "order": "relevance"}],
                    hours=72,
                    now=NOW,
                    seen=set(),
                    search_budget=3,
                )
            self.assertEqual(warnings, [])
            self.assertEqual(len(resources), 0)
            self.assertEqual(len(youtube.calls), 3)
            self.assertEqual([call.get("pageToken") for call in youtube.calls], [None, "p2", "p3"])
            self.assertTrue(all("videoEmbeddable" not in call for call in youtube.calls))
            run = QueryPerformance(root).history()[0]
            self.assertEqual(run["calls"], 3)
            self.assertEqual(run["returned_count"], 110)
            self.assertEqual(run["new_unique_count"], 110)
            self.assertEqual(records[0][0]["calls"], 3)

    def test_page_two_is_skipped_when_duplicate_rate_is_high(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            youtube = PagedYouTube()
            seen = {f"v1_{i}" for i in range(2, 50)}
            executor = RecallExecutor(youtube, QueryPerformance(root), self.config())
            with patch("src.discovery_next.recall.get_video_details", return_value={}):
                executor.execute(
                    [{"query": "minecraft survival", "domain": "minecraft", "intent": "challenge",
                      "recall_source": "exploitation", "order": "relevance"}],
                    hours=72,
                    now=NOW,
                    seen=seen,
                    search_budget=3,
                )
            self.assertEqual(len(youtube.calls), 1)
            run = QueryPerformance(root).history()[0]
            self.assertEqual(run["calls"], 1)
            self.assertEqual(run["new_unique_count"], 2)


class MetricSnapshotStoreTests(TestCase):
    def test_incremental_rates_fall_back_and_use_adjacent_snapshots(self):
        with tempfile.TemporaryDirectory() as name:
            store = MetricSnapshotStore(Path(name))
            video_id = "abcdefghijk"
            self.assertEqual(store.rates(video_id, fallback_views_per_hour=7.5)["source"], "age_fallback")
            store.record(video_id, 100, 5, 1, observed_at=NOW)
            store.record(video_id, 160, 11, 3, observed_at=NOW + timedelta(hours=2))
            rates = store.rates(video_id)
            self.assertEqual(rates["source"], "incremental")
            self.assertAlmostEqual(rates["views_per_hour"], 30.0)
            self.assertAlmostEqual(rates["likes_per_hour"], 3.0)
            self.assertAlmostEqual(rates["comments_per_hour"], 1.0)


class HotScoreIncrementalVelocityTests(TestCase):
    def test_hot_score_rewards_incremental_like_and_comment_velocity(self):
        base = {"video_id": "a", "view_count": 100000, "views_per_hour": 100, "age_hours": 10,
                "like_count": 1000, "comment_count": 100, "likes_per_hour": 0, "comments_per_hour": 0}
        faster = {**base, "likes_per_hour": 120, "comments_per_hour": 40}
        ranker = CandidateRanker({})
        self.assertGreater(ranker.hot_score(faster), ranker.hot_score(base))
