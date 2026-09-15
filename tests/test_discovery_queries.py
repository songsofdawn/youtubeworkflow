from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from src.control_panel.youtube import load_discovery_packs, public_discovery_catalog, save_discovery_packs
from src.discovery.pipeline import DiscoveryPipeline
from src.discovery.query_plan import normalize_queries, query_diagnostics
from src.discovery.store import DiscoveryStore
from src.fetch_daily_candidates import SearchQuotaExceeded
from tests.test_discovery import DeepPagedYouTubeClient, FakeYouTubeClient, PACK, discovery_config


class QueryPoolTests(TestCase):
    def test_legacy_catalog_is_read_only(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "packs.json"
            with self.assertRaisesRegex(ValueError, "read-only"):
                save_discovery_packs(path, [PACK])
            self.assertFalse(path.exists())
        self.assertEqual(normalize_queries(" AI |ai| local   LLM ||"), ["AI", "local LLM"])

    def test_shipped_next_taxonomy_has_shared_formats_and_no_queries(self):
        from src.discovery_next.taxonomy import Taxonomy
        taxonomy = Taxonomy(Path(__file__).resolve().parents[1])
        packs = taxonomy.load()
        self.assertEqual(len(packs), 21)
        defaults = {pack["id"] for pack in packs if pack["default_selected"]}
        self.assertEqual(len(defaults), 8)
        for pack in packs:
            self.assertNotIn("query", pack)
            self.assertTrue(set(pack["formats"]) <= taxonomy.formats.keys())

    def test_rotation_persists_explores_and_uses_yield_only_to_break_ties(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "cache.sqlite3"
            store = DiscoveryStore(path)
            pool = ["first", "second", "third", "fourth"]
            self.assertEqual(store.choose_queries("topic", pool, 2), pool[:2])
            store.record_query_performance([
                {"pack_id": "topic", "query": "FIRST", "calls": 2, "new_eligible_count": 2},
                {"pack_id": "topic", "query": "second", "calls": 1, "new_eligible_count": 5},
            ])
            store = DiscoveryStore(path)
            self.assertEqual(store.choose_queries("topic", pool, 2), pool[2:])
            self.assertEqual(store.choose_queries("topic", pool[:2], 2), ["second", "first"])
            self.assertEqual(store.choose_queries("other", pool, 2), pool[:2])
            self.assertEqual(store.choose_queries("topic", ["new", *pool], 1), ["new"])

    def test_diagnostics_handle_empty_overlap_and_history_without_call_order_bias(self):
        searches = [
            {"pack_id": "p", "query": "one", "order": "date", "video_ids": {"a", "b", "c"}},
            {"pack_id": "p", "query": "one", "order": "relevance", "video_ids": {"a", "b"}},
            {"pack_id": "p", "query": "two", "order": "relevance", "video_ids": {"b", "d"}},
            {"pack_id": "p", "query": "empty", "order": "viewCount", "video_ids": set()},
        ]
        rows = [{"video_id": video_id, "channel_id": "same", "channel_title": video_id}
                for video_id in ("a", "b", "d")]
        stats = query_diagnostics(searches, rows, [{"video_id": "b"}], {"a"})
        first = stats[0]
        self.assertEqual((first["calls"], first["unique_count"], first["new_count"]), (2, 3, 2))
        self.assertEqual((first["eligible_count"], first["new_eligible_count"], first["channel_count"]), (2, 1, 1))
        self.assertEqual([row["selected_count"] for row in stats], [1, 1, 0])
        self.assertEqual((stats[2]["calls"], stats[2]["unique_count"]), (1, 0))
        reversed_stats = query_diagnostics(list(reversed(searches)), rows, [{"video_id": "b"}], {"a"})
        self.assertEqual({row["query"]: row["new_eligible_count"] for row in stats},
                         {row["query"]: row["new_eligible_count"] for row in reversed_stats})


class QueryPoolPipelineTests(TestCase):
    def run_discovery(self, root, youtube, pack, config, **kwargs):
        with patch("src.discovery.pipeline.OllamaDiscoveryClient.health", return_value={"model_ready": False}):
            return DiscoveryPipeline(root).run(
                youtube=youtube, packs=[pack], selected_ids=[pack["id"]],
                hours=168, per_pack=20, config=config,
                now=datetime(2026, 8, 25, tzinfo=timezone.utc), **kwargs,
            )

    def config(self):
        config = discovery_config()
        config.update({"discovery_max_search_requests": 6, "discovery_recall_target": 1000,
                       "discovery_adaptive_page2_enabled": False})
        config["discovery_llm"]["enabled"] = False
        return config

    def test_all_supplements_run_before_paging_and_rotate_across_restarts(self):
        with tempfile.TemporaryDirectory() as name:
            pack = {**PACK, "query": "primary|one|two|three|four|five|six"}
            results = []
            for expected in (["one", "two", "three"], ["four", "five", "six"]):
                youtube = DeepPagedYouTubeClient()
                result = self.run_discovery(Path(name), youtube, pack, self.config())
                self.assertEqual([call["q"] for call in youtube.search_calls], ["primary"] * 3 + expected)
                self.assertEqual([call["order"] for call in youtube.search_calls],
                                 ["viewCount", "date", "relevance", "relevance", "relevance", "viewCount"])
                self.assertFalse(any("pageToken" in call for call in youtube.search_calls))
                self.assertEqual(sum(row["calls"] for row in result["summary"]["query_diagnostics"]), 6)
                self.assertEqual(result["summary"]["query_pool_sizes"], {"technology": 6})
                self.assertEqual(result["summary"]["planned_query_count"], 0)
                self.assertTrue(all(row["rights_status"] == "PENDING" for row in result["results"]))
                results.append(result)
            self.assertLessEqual(len(results[1]["results"]), 20)

    def test_rotation_can_be_disabled_and_legacy_single_order_is_respected(self):
        with tempfile.TemporaryDirectory() as name:
            config = self.config()
            config.update({"discovery_query_rotation_enabled": False,
                           "discovery_supplemental_search_orders": ["viewCount"]})
            pack = {**PACK, "query": "primary|one|two|three|four"}
            for _ in range(2):
                youtube = FakeYouTubeClient()
                self.run_discovery(Path(name), youtube, pack, config)
                self.assertEqual([call["q"] for call in youtube.search_calls[3:]], ["one", "two", "three"])
                self.assertTrue(all(call["order"] == "viewCount" for call in youtube.search_calls[3:]))

    def test_zero_results_and_partial_quota_are_reported_and_only_executed_words_rotate(self):
        class PartialClient(FakeYouTubeClient):
            def get(self, endpoint, params):
                if endpoint == "search":
                    if len(self.search_calls) >= 4:
                        raise SearchQuotaExceeded("offline quota")
                    if params["q"] == "empty":
                        self.search_calls.append(dict(params))
                        return {"items": []}
                return super().get(endpoint, params)

        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            pack = {**PACK, "query": "primary|empty|unrun|also unrun|new"}
            result = self.run_discovery(root, PartialClient(), pack, self.config(), known_video_ids={"boringvid01"})
            stats = result["summary"]["query_diagnostics"]
            self.assertTrue(result["summary"]["search_quota_exhausted"])
            self.assertEqual([row["query"] for row in stats], ["primary", "empty"])
            self.assertEqual(stats[0]["eligible_count"], 1)  # Excludes known + non-English.
            self.assertEqual(stats[1]["unique_count"], 0)
            store = DiscoveryPipeline(root).store
            self.assertEqual(store.choose_queries("technology", ["empty", "unrun", "also unrun", "new"], 3),
                             ["unrun", "also unrun", "new"])

    def test_low_global_budget_keeps_hot_lane_without_marking_unrun_queries(self):
        with tempfile.TemporaryDirectory() as name:
            config = self.config()
            config["discovery_max_search_requests"] = 2
            youtube = FakeYouTubeClient()
            result = self.run_discovery(Path(name), youtube, PACK, config)
            self.assertEqual(len(youtube.search_calls), 2)
            self.assertEqual([call["order"] for call in youtube.search_calls], ["viewCount", "date"])
            self.assertEqual(len(result["summary"]["query_diagnostics"]), 1)

    def test_selecting_more_packs_never_raises_explicit_global_budget(self):
        with tempfile.TemporaryDirectory() as name:
            config = self.config()
            config["discovery_max_search_requests"] = 2
            packs = [{**PACK, "id": f"pack_{i}"} for i in range(3)]
            youtube = FakeYouTubeClient()
            with patch("src.discovery.pipeline.OllamaDiscoveryClient.health", return_value={"model_ready": False}):
                result = DiscoveryPipeline(Path(name)).run(
                    youtube=youtube, packs=packs, selected_ids=[pack["id"] for pack in packs],
                    hours=168, per_pack=20, config=config,
                    now=datetime(2026, 8, 25, tzinfo=timezone.utc),
                )
            self.assertEqual(len(youtube.search_calls), 2)
            self.assertEqual(result["summary"]["search_request_limit"], 2)
            self.assertEqual(result["summary"]["search_requests_by_pack"]["pack_2"], 0)

    def test_short_exclusion_labels_do_not_match_inside_normal_words(self):
        class DescriptionClient(FakeYouTubeClient):
            def __init__(self, description):
                super().__init__()
                self.description = description

            def get(self, endpoint, params):
                payload = super().get(endpoint, params)
                if endpoint != "search":
                    for item in payload["items"]:
                        item["snippet"]["description"] = self.description
                return payload

        with tempfile.TemporaryDirectory() as name:
            config = self.config()
            config.update({"discovery_adaptive_page2_enabled": True,
                           "hard_exclude_phrases": ["ost", "official trailer", " "]})
            for description, qualified in (
                ("Most useful robot: almost no cost, posted after testing.", 1),
                ("The game OST: original music.", 0),
                ("Watch the official trailer here.", 0),
            ):
                with self.subTest(description=description):
                    result = self.run_discovery(Path(name), DescriptionClient(description), PACK, config,
                                                known_video_ids={"boringvid01"})
                    self.assertEqual(result["summary"]["eligible_count"], qualified)
                    self.assertEqual(result["summary"]["adaptive_counts_before"]["technology"]["qualified"], qualified)
                    self.assertEqual(result["summary"]["excluded"].get("risk_phrase", 0), 0 if qualified else 2)
