from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from src.candidate_analysis import calculate_interest
from src.discovery.ollama_client import OllamaDiscoveryClient, OllamaSettings
from src.discovery.pipeline import DiscoveryPipeline, _interest_config
from tests.test_discovery import FakeYouTubeClient, PACK, discovery_config, llm_scores


class EditorialDiscoveryTests(TestCase):
    def test_quiet_process_is_not_penalized_but_spam_still_is(self):
        config = discovery_config()
        config["boring_penalty_phrases"] = ["ASMR", "no commentary", "stock footage"]
        config["topic_penalty_phrases"] = {"gaming": ["no commentary", "loading screen"]}
        adapted = _interest_config(config)
        quiet = calculate_interest("rug cleaning asmr no commentary", "cleaning", adapted)
        spam = calculate_interest("stock footage asmr", "cleaning", adapted)
        self.assertEqual(quiet["boring_penalty"], 0)
        self.assertEqual(spam["boring_penalty"], 5)
        self.assertEqual(config["boring_penalty_phrases"], ["ASMR", "no commentary", "stock footage"])
        game = calculate_interest("no commentary loading screen", "gaming", adapted)
        self.assertEqual(game["boring_hits"], ["loading screen"])
        self.assertEqual(config["topic_penalty_phrases"]["gaming"], ["no commentary", "loading screen"])
        config["discovery_boring_penalty_exempt_phrases"] = []
        self.assertIn("no commentary", _interest_config(config)["boring_penalty_phrases"])

    def test_metadata_receives_editorial_scope_and_only_public_candidate_fields(self):
        client = OllamaDiscoveryClient(OllamaSettings(enabled=True))
        row = {
            "video_id": "buildtest01", "title": "Rug cleaning process",
            "channel_title": "Cleaning", "description": "Mud removal and rinsing.",
            "pack_label": "深度清洁与焕新", "pack_description": "关注完整清洗过程和前后变化",
            "local_path": "PRIVATE_PATH_SENTINEL", "api_key": "PRIVATE_KEY_SENTINEL",
        }
        response = {"evaluations": list(llm_scores([row], {}).values())}
        with patch.object(client, "_chat", return_value=response) as chat:
            result = client.evaluate_metadata([row], {"positive": [], "negative": []})
        messages = chat.call_args.args[0]
        request = json.loads(messages[1]["content"])
        self.assertEqual(request["candidates"][0]["topic_description"], row["pack_description"])
        self.assertEqual(set(request["viewer_value"]), {"解压", "新奇", "有趣", "知识性", "科普"})
        self.assertNotIn("PRIVATE_", json.dumps(messages))
        self.assertEqual(result["buildtest01"]["verdict"], "keep")

    def test_metadata_cache_tracks_prompt_and_editable_domain_scope(self):
        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            client = MagicMock()
            client.evaluate_metadata.side_effect = llm_scores
            settings = OllamaSettings(enabled=True)
            row = {
                "video_id": "buildtest01", "title": "Old clock restoration",
                "description": "Disassembly, cleaning and reassembly.", "tags": [],
                "pack_label": "修复", "pack_description": "关注机械原理",
            }

            def evaluate(candidate):
                value = dict(candidate)
                pipeline._metadata_evaluations(client, [value], settings, {}, [], None, None)
                return value["llm_cache_hit"]

            with patch("src.discovery.pipeline.PROMPT_VERSION", "discovery_editor_v3"):
                self.assertFalse(evaluate(row))
            self.assertFalse(evaluate(row))
            self.assertTrue(evaluate(row))
            changed = {**row, "pack_description": "关注解压过程与前后变化"}
            self.assertFalse(evaluate(changed))
            self.assertTrue(evaluate(changed))
            self.assertEqual(client.evaluate_metadata.call_count, 3)

    def test_reject_cannot_backfill_any_heat_tier_but_hot_and_opt_out_remain(self):
        class HeatClient(FakeYouTubeClient):
            def __init__(self, views):
                super().__init__()
                self.views = views

            def get(self, endpoint, params):
                response = super().get(endpoint, params)
                if endpoint != "search":
                    for item in response["items"]:
                        if item["id"] == "boringvid01":
                            item["statistics"]["viewCount"] = str(self.views)
                return response

        cases = [
            (views, True, False, "reject", False) for views in (120, 600, 1200)
        ] + [
            (120, False, False, "reject", True),
            (1200, True, True, "reject", True),
            (120, True, False, "maybe", True),
        ]
        for views, exclude, hot, verdict, expected in cases:
            with self.subTest(views=views, exclude=exclude, hot=hot, verdict=verdict):
                with tempfile.TemporaryDirectory() as name:
                    config = discovery_config()
                    config.update({
                        "discovery_popularity_filter_mode": "balanced",
                        "discovery_min_view_count_by_window": {"72": 1000},
                        "discovery_min_views_per_hour_by_window": {"72": 20},
                        "discovery_hot_view_count_by_window": {"72": 1000 if hot else 10**9},
                        "discovery_hot_views_per_hour_by_window": {"72": 10**9},
                        "discovery_min_opportunity_score": 0,
                        "discovery_expansion_min_opportunity_score": 0,
                        "discovery_reserve_min_opportunity_score": 0,
                        "discovery_exclude_llm_rejects": exclude,
                    })
                    config["discovery_llm"]["query_planning_enabled"] = False

                    def scores(rows, preferences):
                        self.assertTrue(all(row["pack_description"] == PACK["description"] for row in rows))
                        output = llm_scores(rows, preferences)
                        if "boringvid01" in output:
                            output["boringvid01"]["verdict"] = verdict
                        return output

                    with (
                        patch("src.discovery.pipeline.OllamaDiscoveryClient.health",
                              return_value={"model_ready": True, "embedding_ready": False}),
                        patch("src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata", side_effect=scores),
                    ):
                        result = DiscoveryPipeline(Path(name)).run(
                            youtube=HeatClient(views), packs=[PACK], selected_ids=[PACK["id"]],
                            hours=72, per_pack=3, config=config,
                            now=datetime(2026, 8, 25, tzinfo=timezone.utc),
                        )
                self.assertEqual("boringvid01" in {row["video_id"] for row in result["results"]}, expected)
                if not expected:
                    self.assertEqual(result["summary"]["excluded"]["llm_reject"], 1)
                self.assertTrue(all(row["rights_status"] == "PENDING" for row in result["results"]))
