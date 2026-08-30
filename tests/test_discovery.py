from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

from src.control_panel.jobs import JobStore, WorkflowWorker
from src.control_panel.settings import update_discovery_settings
from src.control_panel.tasks import WorkflowScanner
from src.discovery.ollama_client import (
    OllamaDiscoveryClient,
    OllamaDiscoveryError,
    OllamaSettings,
)
from src.discovery.pipeline import DiscoveryPipeline
from src.discovery.store import DiscoveryStore


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class FakeYouTubeClient:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []

    def get(self, endpoint: str, params: dict) -> dict:
        if endpoint == "search":
            self.search_calls.append(dict(params))
            return {
                "items": [
                    {
                        "id": {"videoId": video_id},
                        "snippet": {"title": title},
                    }
                    for video_id, title in (
                        ("buildtest01", "I Built a Robot That Sorts Recycling"),
                        ("boringvid01", "Top 10 Technology Compilation"),
                        ("hindivideo1", "नई तकनीक परीक्षण"),
                    )
                ]
            }
        rows = {
            "buildtest01": {
                "title": "I Built a Robot That Sorts Recycling",
                "description": "A complete engineering build with tests, failures and final results.",
                "language": "en-US",
                "views": "12000",
            },
            "boringvid01": {
                "title": "Top 10 Technology Compilation",
                "description": "A stock footage compilation with no original experiment.",
                "language": "en-US",
                "views": "90000",
            },
            "hindivideo1": {
                "title": "नई तकनीक परीक्षण",
                "description": "हिंदी वीडियो",
                "language": "hi",
                "views": "50000",
            },
        }
        return {
            "items": [
                {
                    "id": video_id,
                    "snippet": {
                        "title": rows[video_id]["title"],
                        "description": rows[video_id]["description"],
                        "channelTitle": "Channel " + video_id,
                        "publishedAt": "2026-08-24T12:00:00Z",
                        "defaultAudioLanguage": rows[video_id]["language"],
                        "liveBroadcastContent": "none",
                        "tags": ["technology", "experiment"],
                        "thumbnails": {
                            "high": {
                                "url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                            }
                        },
                    },
                    "contentDetails": {"duration": "PT10M", "caption": "true"},
                    "statistics": {
                        "viewCount": rows[video_id]["views"],
                        "likeCount": "800",
                        "commentCount": "80",
                    },
                    "status": {
                        "license": "youtube",
                        "embeddable": True,
                        "privacyStatus": "public",
                    },
                }
                for video_id in params["id"].split(",")
            ]
        }


class DeepPagedYouTubeClient:
    def __init__(self) -> None:
        self.search_calls: list[dict] = []

    def get(self, endpoint: str, params: dict) -> dict:
        if endpoint == "search":
            call_index = len(self.search_calls)
            self.search_calls.append(dict(params))
            start = call_index * 50
            return {
                "items": [
                    {
                        "id": {"videoId": f"v{index:010d}"},
                        "snippet": {
                            "title": f"Minecraft Experiment {(index * 2654435761):016x}"
                        },
                    }
                    for index in range(start, start + 50)
                ],
                "nextPageToken": f"page-{call_index + 1}",
            }
        return {
            "items": [
                {
                    "id": video_id,
                    "snippet": {
                        "title": (
                            f"Minecraft Experiment "
                            f"{(int(video_id[1:]) * 2654435761):016x}"
                        ),
                        "description": "A complete survival build with tests, failures, and a final result.",
                        "channelTitle": f"Creator {video_id}",
                        "publishedAt": "2026-08-24T12:00:00Z",
                        "defaultAudioLanguage": "en-US",
                        "liveBroadcastContent": "none",
                        "tags": ["minecraft", "experiment", "survival"],
                        "thumbnails": {
                            "high": {
                                "url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                            }
                        },
                    },
                    "contentDetails": {"duration": "PT12M", "caption": "true"},
                    "statistics": {
                        "viewCount": str(1000 + int(video_id[1:])),
                        "likeCount": "200",
                        "commentCount": "30",
                    },
                    "status": {
                        "license": "youtube",
                        "embeddable": True,
                        "privacyStatus": "public",
                    },
                }
                for video_id in params["id"].split(",")
            ]
        }


def discovery_config() -> dict:
    return {
        "region_code": "US",
        "language": "en",
        "safe_search": "moderate",
        "english_only": True,
        "min_duration_seconds": 60,
        "max_duration_seconds": 2700,
        "exclude_shorts": True,
        "shorts_max_duration_seconds": 180,
        "hard_exclude_phrases": [],
        "boring_penalty_phrases": ["compilation", "stock footage"],
        "boring_penalty_per_hit": 5,
        "discovery_popularity_filter_mode": "hard",
        "discovery_min_opportunity_score": 50,
        "discovery_exclude_llm_rejects": True,
        "discovery_engagement_confidence_views": 500,
        "discovery_search_results_per_pack": 20,
        "discovery_recall_orders": ["viewCount", "date"],
        "discovery_max_search_requests": 2,
        "discovery_history_repeat_penalty": 8,
        "max_per_channel": 2,
        "max_per_event": 1,
        "discovery_llm": {
            "enabled": True,
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen3.5:9b",
            "embedding_enabled": False,
            "query_planning_enabled": True,
            "visual_enabled": False,
            "metadata_batch_size": 10,
        },
    }


PACK = {
    "id": "technology",
    "label": "科技实验",
    "description": "新技术实测与工程制作",
    "query": "technology experiment|engineering build",
    "keywords": ["technology", "engineering", "robot", "experiment"],
}


def llm_scores(rows: list[dict], _preferences: dict) -> dict[str, dict]:
    output: dict[str, dict] = {}
    for row in rows:
        interesting = row["video_id"] == "buildtest01"
        output[row["video_id"]] = {
            "video_id": row["video_id"],
            "topic_fit": 92 if interesting else 35,
            "interestingness": 95 if interesting else 15,
            "novelty": 88 if interesting else 10,
            "story_payoff": 94 if interesting else 12,
            "visual_potential": 90 if interesting else 30,
            "localization_value": 86 if interesting else 22,
            "clickbait_risk": 8 if interesting else 88,
            "language_confidence": 98,
            "verdict": "keep" if interesting else "reject",
            "reason_zh": "有完整制作过程和明确测试结果" if interesting else "模板化合集且没有原创过程",
            "confidence": 92,
        }
    return output


class DiscoveryPipelineTests(TestCase):
    def test_per_run_maximum_duration_filters_longer_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.plan_queries",
                    return_value={},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=llm_scores,
                ),
            ):
                result = pipeline.run(
                    youtube=FakeYouTubeClient(),
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=3,
                    config=discovery_config(),
                    minimum_duration_seconds=300,
                    maximum_duration_seconds=540,
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )

        self.assertEqual(result["results"], [])
        self.assertEqual(result["summary"]["maximum_duration_seconds"], 540)
        self.assertEqual(result["summary"]["excluded"]["duration"], 3)

    def test_per_run_five_minute_minimum_filters_shorter_candidates(self) -> None:
        class MixedDurationClient(FakeYouTubeClient):
            def get(self, endpoint: str, params: dict) -> dict:
                payload = super().get(endpoint, params)
                if endpoint != "search":
                    for item in payload["items"]:
                        if item["id"] == "buildtest01":
                            item["contentDetails"]["duration"] = "PT4M59S"
                        elif item["id"] == "boringvid01":
                            item["contentDetails"]["duration"] = "PT5M"
                return payload

        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.plan_queries",
                    return_value={},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=llm_scores,
                ),
            ):
                result = pipeline.run(
                    youtube=MixedDurationClient(),
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=3,
                    config=discovery_config(),
                    minimum_duration_seconds=300,
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )

        self.assertEqual(result["results"], [])
        self.assertEqual(result["summary"]["minimum_duration_seconds"], 300)
        self.assertEqual(result["summary"]["excluded"]["hard_filter"], 1)
        self.assertEqual(result["summary"]["excluded"]["llm_reject"], 1)

    def test_deep_recall_fetches_1000_then_sends_top_100_to_llm(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            youtube = DeepPagedYouTubeClient()
            config = discovery_config()
            config.update(
                {
                    "discovery_recall_target": 1000,
                    "discovery_max_search_requests": 30,
                    "discovery_max_pages_per_stream": 3,
                }
            )
            config["discovery_llm"].update(
                {
                    "query_planning_enabled": False,
                    "metadata_candidates_per_pack": 100,
                    "metadata_max_candidates": 100,
                }
            )
            pack = {
                **PACK,
                "query": (
                    "Minecraft but|Minecraft challenge|"
                    "Minecraft experiment|Minecraft survival"
                ),
            }
            evaluated_ids: list[str] = []

            def evaluate(rows: list[dict], _preferences: dict) -> dict[str, dict]:
                evaluated_ids.extend(str(row["video_id"]) for row in rows)
                return {
                    str(row["video_id"]): {
                        "video_id": str(row["video_id"]),
                        "topic_fit": 90,
                        "interestingness": 80,
                        "novelty": 75,
                        "story_payoff": 85,
                        "visual_potential": 80,
                        "localization_value": 80,
                        "clickbait_risk": 10,
                        "language_confidence": 98,
                        "verdict": "keep",
                        "reason_zh": "有明确过程、失败和最终结果",
                        "confidence": 90,
                    }
                    for row in rows
                }

            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=evaluate,
                ),
            ):
                result = pipeline.run(
                    youtube=youtube,
                    packs=[pack],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=20,
                    config=config,
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )

            self.assertEqual(len(youtube.search_calls), 20)
            self.assertTrue(any("pageToken" in call for call in youtube.search_calls))
            self.assertEqual(result["summary"]["raw_candidate_count"], 1000)
            self.assertTrue(result["summary"]["recall_target_reached"])
            self.assertEqual(result["summary"]["llm_candidate_count"], 100)
            self.assertEqual(len(set(evaluated_ids)), 100)
            self.assertEqual(len(result["results"]), 20)
            self.assertTrue(all(row["llm_status"] == "scored" for row in result["results"]))

    def test_two_topics_use_independent_upper_limits_without_duplicate_backfill(self) -> None:
        second_pack = {
            "id": "science",
            "label": "科学",
            "description": "科学实验与发现",
            "query": "science experiment|new discovery",
            "keywords": ["science", "experiment", "discovery"],
        }
        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            youtube = DeepPagedYouTubeClient()
            config = discovery_config()
            config.update(
                {
                    "discovery_recall_target": 40,
                    "discovery_recall_candidates_per_result": 1,
                    "discovery_max_search_requests": 2,
                }
            )
            config["discovery_llm"].update(
                {
                    "query_planning_enabled": False,
                    "metadata_max_candidates": 100,
                }
            )

            def evaluate(rows: list[dict], _preferences: dict) -> dict[str, dict]:
                return {
                    str(row["video_id"]): {
                        "video_id": str(row["video_id"]),
                        "topic_fit": 90,
                        "interestingness": 85,
                        "novelty": 80,
                        "story_payoff": 85,
                        "visual_potential": 80,
                        "localization_value": 85,
                        "clickbait_risk": 10,
                        "language_confidence": 98,
                        "verdict": "keep",
                        "reason_zh": "过程完整且有明确结果",
                        "confidence": 90,
                    }
                    for row in rows
                }

            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=evaluate,
                ),
            ):
                result = pipeline.run(
                    youtube=youtube,
                    packs=[PACK, second_pack],
                    selected_ids=["technology", "science"],
                    hours=72,
                    per_pack=20,
                    config=config,
                    minimum_duration_seconds=300,
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )

        self.assertEqual(len(youtube.search_calls), 2)
        self.assertTrue(all(call["videoDuration"] == "medium" for call in youtube.search_calls))
        self.assertEqual(result["summary"]["llm_candidate_count"], 100)
        counts = result["summary"]["result_counts_by_pack"]
        self.assertTrue(all(0 < count <= 20 for count in counts.values()))
        self.assertEqual(result["summary"]["complete_pack_count"], 0)
        self.assertEqual(
            len(result["results"]),
            sum(len(group["results"]) for group in result["groups"]),
        )
        self.assertEqual(
            len(result["results"]),
            len({row["video_id"] for row in result["results"]}),
        )

    def test_incomplete_ai_batch_is_split_until_candidates_are_scored(self) -> None:
        calls: list[int] = []

        def fragile_evaluate(rows: list[dict], preferences: dict) -> dict[str, dict]:
            calls.append(len(rows))
            if len(rows) > 1:
                raise OllamaDiscoveryError("invalid JSON")
            return llm_scores(rows, preferences)

        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            config = discovery_config()
            config["discovery_llm"]["query_planning_enabled"] = False
            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=fragile_evaluate,
                ),
            ):
                result = pipeline.run(
                    youtube=FakeYouTubeClient(),
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=2,
                    config=config,
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )

        self.assertIn(2, calls)
        self.assertEqual(calls.count(1), 2)
        self.assertEqual(result["summary"]["llm_scored_count"], 2)
        self.assertIn(
            "本地 AI 有批量响应不完整，已自动拆小重试",
            result["summary"]["warnings"],
        )

    def test_hybrid_pipeline_uses_llm_filters_language_and_softens_history(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            youtube = FakeYouTubeClient()
            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.plan_queries",
                    return_value={"technology": "robot engineering test results"},
                ) as planner,
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=llm_scores,
                ) as evaluator,
            ):
                first = pipeline.run(
                    youtube=youtube,
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=2,
                    config=discovery_config(),
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )
                second = pipeline.run(
                    youtube=youtube,
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=2,
                    config=discovery_config(),
                    now=datetime(2026, 8, 25, 1, tzinfo=timezone.utc),
                )

            self.assertEqual(first["summary"]["search_request_count"], 2)
            self.assertEqual(first["summary"]["planned_query_count"], 1)
            self.assertEqual(first["summary"]["excluded"]["non_english"], 1)
            self.assertEqual(first["summary"]["llm_candidate_count"], 2)
            self.assertEqual(first["summary"]["llm_scored_count"], 2)
            self.assertEqual([row["video_id"] for row in first["results"]], ["buildtest01"])
            self.assertEqual(first["results"][0]["llm_status"], "scored")
            self.assertEqual(first["summary"]["excluded"]["llm_reject"], 1)
            repeated = next(row for row in second["results"] if row["video_id"] == "buildtest01")
            self.assertTrue(repeated["seen_in_previous_search"])
            self.assertFalse(repeated["similar_candidate"])
            self.assertEqual(repeated["collision_status"], "曾展示，已轻微降权")
            self.assertEqual(evaluator.call_count, 1, "second run should use the metadata cache")
            self.assertEqual(planner.call_count, 2)

    def test_hard_popularity_gate_rejects_low_view_ai_keep(self) -> None:
        class LowViewClient(FakeYouTubeClient):
            def get(self, endpoint: str, params: dict) -> dict:
                payload = super().get(endpoint, params)
                if endpoint != "search":
                    for item in payload["items"]:
                        if item["id"] == "buildtest01":
                            item["statistics"]["viewCount"] = "9"
                return payload

        evaluated_ids: list[str] = []

        def evaluate(rows: list[dict], preferences: dict) -> dict[str, dict]:
            evaluated_ids.extend(str(row["video_id"]) for row in rows)
            return llm_scores(rows, preferences)

        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            config = discovery_config()
            config["discovery_min_view_count_by_window"] = {"72": 500}
            config["discovery_min_views_per_hour_by_window"] = {"72": 12}
            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.plan_queries",
                    return_value={},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=evaluate,
                ),
            ):
                result = pipeline.run(
                    youtube=LowViewClient(),
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=20,
                    config=config,
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )

        self.assertNotIn("buildtest01", evaluated_ids)
        self.assertNotIn("buildtest01", [row["video_id"] for row in result["results"]])
        self.assertEqual(result["summary"]["excluded"]["low_popularity"], 1)

    def test_low_view_engagement_is_confidence_weighted(self) -> None:
        class LowViewClient(FakeYouTubeClient):
            def get(self, endpoint: str, params: dict) -> dict:
                payload = super().get(endpoint, params)
                if endpoint != "search":
                    for item in payload["items"]:
                        if item["id"] == "buildtest01":
                            item["statistics"]["viewCount"] = "9"
                return payload

        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            config = discovery_config()
            config["discovery_popularity_filter_mode"] = "soft"
            config["discovery_min_opportunity_score"] = 0
            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.plan_queries",
                    return_value={},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=llm_scores,
                ),
            ):
                result = pipeline.run(
                    youtube=LowViewClient(),
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=2,
                    config=config,
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )

        row = next(row for row in result["results"] if row["video_id"] == "buildtest01")
        self.assertLess(row["engagement_score"], 20)
        self.assertLess(row["engagement_confidence"], 0.2)

    def test_balanced_popularity_adds_only_ai_keep_from_expansion_tier(self) -> None:
        class ModerateViewClient(FakeYouTubeClient):
            def get(self, endpoint: str, params: dict) -> dict:
                payload = super().get(endpoint, params)
                if endpoint != "search":
                    for item in payload["items"]:
                        if item["id"] == "buildtest01":
                            item["statistics"].update(
                                {"viewCount": "300", "likeCount": "12", "commentCount": "2"}
                            )
                return payload

        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            config = discovery_config()
            config.update(
                {
                    "discovery_popularity_filter_mode": "balanced",
                    "discovery_min_view_count_by_window": {"72": 500},
                    "discovery_min_views_per_hour_by_window": {"72": 12},
                    "discovery_popularity_expansion_view_ratio": 0.4,
                    "discovery_popularity_expansion_vph_ratio": 0.5,
                    "discovery_expansion_min_opportunity_score": 60,
                }
            )
            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.plan_queries",
                    return_value={},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=llm_scores,
                ),
            ):
                result = pipeline.run(
                    youtube=ModerateViewClient(),
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=3,
                    config=config,
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )

        self.assertEqual([row["video_id"] for row in result["results"]], ["buildtest01"])
        self.assertFalse(result["results"][0]["heat_floor_pass"])
        self.assertEqual(result["results"][0]["heat_tier"], "expanded")
        self.assertEqual(result["summary"]["expanded_result_count"], 1)
        self.assertEqual(result["summary"]["excluded"]["llm_reject"], 1)

    def test_balanced_popularity_uses_a_bounded_reserve_tier_for_shortfalls(self) -> None:
        class ReserveHeatClient(FakeYouTubeClient):
            def get(self, endpoint: str, params: dict) -> dict:
                payload = super().get(endpoint, params)
                if endpoint != "search":
                    for item in payload["items"]:
                        if item["id"] == "buildtest01":
                            item["statistics"].update(
                                {"viewCount": "100", "likeCount": "20", "commentCount": "3"}
                            )
                return payload

        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            config = discovery_config()
            config.update(
                {
                    "discovery_popularity_filter_mode": "balanced",
                    "discovery_min_view_count_by_window": {"72": 500},
                    "discovery_min_views_per_hour_by_window": {"72": 12},
                    "discovery_popularity_expansion_view_ratio": 0.4,
                    "discovery_popularity_expansion_vph_ratio": 0.5,
                    "discovery_popularity_reserve_view_ratio": 0.1,
                    "discovery_popularity_reserve_vph_ratio": 0.15,
                    "discovery_reserve_min_opportunity_score": 35,
                }
            )
            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.plan_queries",
                    return_value={},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=llm_scores,
                ),
            ):
                result = pipeline.run(
                    youtube=ReserveHeatClient(),
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=2,
                    config=config,
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )

        row = next(row for row in result["results"] if row["video_id"] == "buildtest01")
        self.assertEqual(row["heat_tier"], "reserve")
        self.assertEqual(row["selection_tier"], "reserve")
        self.assertGreaterEqual(row["view_count"], result["summary"]["reserve_minimum_view_count"])
        self.assertEqual(result["summary"]["reserve_result_count"], 1)

    def test_seed_queries_are_searched_before_planned_queries(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            youtube = FakeYouTubeClient()
            config = discovery_config()
            config.update(
                {
                    "discovery_recall_target": 20,
                    "discovery_recall_candidates_per_result": 10,
                    "discovery_max_search_requests": 2,
                }
            )
            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.plan_queries",
                    return_value={
                        "technology": [
                            "robot field test results",
                            "engineering build failure success",
                            "new hardware explained",
                        ]
                    },
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=llm_scores,
                ),
            ):
                result = pipeline.run(
                    youtube=youtube,
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=2,
                    config=config,
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )

        self.assertEqual(len(youtube.search_calls), 2)
        self.assertEqual(youtube.search_calls[1]["q"], "technology experiment")
        self.assertEqual(youtube.search_calls[1]["order"], "viewCount")
        self.assertEqual(result["summary"]["planned_query_count"], 3)
        self.assertEqual(result["summary"]["search_requests_by_pack"], {"technology": 2})

    def test_tiered_fill_keeps_preferred_first_and_marks_reserve_candidates(self) -> None:
        class ReserveClient(FakeYouTubeClient):
            def get(self, endpoint: str, params: dict) -> dict:
                payload = super().get(endpoint, params)
                if endpoint == "search":
                    for item in payload["items"]:
                        if item["id"]["videoId"] == "boringvid01":
                            item["snippet"]["title"] = "Engineering Field Test With Results"
                else:
                    for item in payload["items"]:
                        if item["id"] == "boringvid01":
                            item["snippet"].update(
                                {
                                    "title": "Engineering Field Test With Results",
                                    "description": "A concrete field test with measured results.",
                                }
                            )
                return payload

        def scores(rows: list[dict], _preferences: dict) -> dict[str, dict]:
            output = llm_scores(rows, _preferences)
            if "boringvid01" in output:
                output["boringvid01"].update(
                    {
                        "topic_fit": 80,
                        "interestingness": 72,
                        "novelty": 68,
                        "story_payoff": 74,
                        "visual_potential": 70,
                        "localization_value": 72,
                        "clickbait_risk": 25,
                        "verdict": "reject",
                        "reason_zh": "类别判断偏严，但数值评分仍适合作为补量备选",
                    }
                )
            return output

        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            config = discovery_config()
            config["discovery_reserve_min_opportunity_score"] = 20
            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.plan_queries",
                    return_value={},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=scores,
                ),
            ):
                result = pipeline.run(
                    youtube=ReserveClient(),
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=2,
                    config=config,
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )

        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["results"][0]["selection_tier"], "preferred")
        self.assertEqual(result["results"][1]["selection_tier"], "reserve")
        self.assertEqual(result["summary"]["reserve_result_count"], 1)
        self.assertEqual(result["summary"]["result_counts_by_pack"], {"technology": 2})

    def test_explicit_negative_feedback_suppresses_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            pipeline = DiscoveryPipeline(Path(name))
            pipeline.record_feedback(
                {
                    "video_id": "boringvid01",
                    "title": "Top 10 Technology Compilation",
                    "channel_title": "Channel boringvid01",
                    "pack_id": "technology",
                },
                "boring",
            )
            with (
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.health",
                    return_value={"model_ready": True, "embedding_ready": False},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.plan_queries",
                    return_value={},
                ),
                patch(
                    "src.discovery.pipeline.OllamaDiscoveryClient.evaluate_metadata",
                    side_effect=llm_scores,
                ),
            ):
                result = pipeline.run(
                    youtube=FakeYouTubeClient(),
                    packs=[PACK],
                    selected_ids=["technology"],
                    hours=72,
                    per_pack=3,
                    config=discovery_config(),
                    now=datetime(2026, 8, 25, 0, tzinfo=timezone.utc),
                )
            self.assertEqual([row["video_id"] for row in result["results"]], ["buildtest01"])


class DiscoveryStoreAndSettingsTests(TestCase):
    def test_feedback_is_local_and_latest_value_wins(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            store = DiscoveryStore(Path(name) / "discovery.sqlite3")
            item = {"video_id": "abcdefghijk", "title": "A Test", "pack_id": "science"}
            store.record_feedback(item, "interested")
            store.record_feedback(item, "boring")
            self.assertEqual(store.feedback_summary()["counts"], {"boring": 1})
            self.assertEqual(store.feedback_by_video()["abcdefghijk"], "boring")

    def test_discovery_settings_update_preserves_other_config(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "trending_config.json"
            write_json(path, {"region_code": "US", "discovery_llm": {"enabled": False}})
            saved = update_discovery_settings(
                path,
                {
                    "discovery_llm_enabled": True,
                    "discovery_ollama_model": "qwen3.5:9b",
                    "discovery_visual_top_n": 12,
                    "discovery_recall_target": 1000,
                    "discovery_max_search_requests": 30,
                    "discovery_metadata_max_candidates": 100,
                },
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["region_code"], "US")
            self.assertTrue(payload["discovery_llm"]["enabled"])
            self.assertEqual(payload["discovery_llm"]["visual_top_n"], 12)
            self.assertEqual(payload["discovery_llm"]["metadata_max_candidates"], 100)
            self.assertEqual(payload["discovery_recall_target"], 1000)
            self.assertEqual(payload["discovery_max_search_requests"], 30)
            self.assertEqual(len(saved), 6)

    def test_ollama_settings_are_bounded(self) -> None:
        settings = OllamaSettings.from_config(
            {"discovery_llm": {"metadata_batch_size": 999, "visual_top_n": 999}}
        )
        self.assertEqual(settings.metadata_batch_size, 30)
        self.assertEqual(settings.visual_top_n, 100)

    def test_query_planner_keeps_three_unique_queries_per_pack(self) -> None:
        client = OllamaDiscoveryClient(OllamaSettings(enabled=True))
        response = {
            "queries": [
                {"pack_id": "technology", "query": "robot field test", "angle": "test"},
                {"pack_id": "technology", "query": "engineering discovery", "angle": "explain"},
                {"pack_id": "technology", "query": "robot field test", "angle": "duplicate"},
                {"pack_id": "technology", "query": "hardware challenge result", "angle": "result"},
                {"pack_id": "technology", "query": "fourth query ignored", "angle": "extra"},
                {"pack_id": "unknown", "query": "ignored pack", "angle": "extra"},
            ]
        }
        with patch.object(client, "_chat", return_value=response):
            result = client.plan_queries([PACK], {"technology": []}, {"positive": [], "negative": []})

        self.assertEqual(
            result,
            {
                "technology": [
                    "robot field test",
                    "engineering discovery",
                    "hardware challenge result",
                ]
            },
        )

    def test_ollama_ten_point_scores_are_normalized(self) -> None:
        result = OllamaDiscoveryClient._validate_evaluations(
            {
                "evaluations": [
                    {
                        "video_id": "abcdefghijk",
                        "interestingness": 9,
                        "clickbait_risk": 1,
                    }
                ]
            },
            {"abcdefghijk"},
            ("interestingness", "clickbait_risk"),
        )
        self.assertEqual(result["abcdefghijk"]["interestingness"], 90)
        self.assertEqual(result["abcdefghijk"]["clickbait_risk"], 10)

    def test_ollama_unit_interval_scores_are_normalized(self) -> None:
        result = OllamaDiscoveryClient._validate_evaluations(
            {
                "evaluations": [
                    {
                        "video_id": "abcdefghijk",
                        "visual_potential": 0.8,
                        "thumbnail_spam_risk": 0.1,
                    }
                ]
            },
            {"abcdefghijk"},
            ("visual_potential", "thumbnail_spam_risk"),
        )
        self.assertEqual(result["abcdefghijk"]["visual_potential"], 80)
        self.assertEqual(result["abcdefghijk"]["thumbnail_spam_risk"], 10)

    def test_thumbnail_fetch_rejects_untrusted_hosts_without_network(self) -> None:
        with self.assertRaises(OllamaDiscoveryError):
            OllamaDiscoveryClient.fetch_thumbnail("https://example.com/image.jpg")


class DiscoveryWorkerTests(TestCase):
    def test_worker_persists_async_discovery_result(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            store = JobStore(
                project / "work" / "control_panel" / "control_panel.sqlite3",
                project / "logs",
            )
            runner = MagicMock(return_value={"results": [{"video_id": "abcdefghijk"}]})
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                MagicMock(),
                runner,
            )
            queued = store.enqueue(
                "discovery",
                "smart-discovery",
                {"packs": ["science"], "hours": 72, "per_pack": 4},
                resource_class="gpu_heavy",
            )
            running = store.claim_next({"discovery"}, {"gpu_heavy"})
            assert running is not None
            worker._execute(running)
            completed = store.get(queued["id"])
            result_path = (
                project / "work" / "control_panel" / "discovery_results" / f"{queued['id']}.json"
            )
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["progress"], 100)
            self.assertTrue(result_path.is_file())
            runner.assert_called_once()
            summary = store.clear_inactive_logs()
            self.assertFalse(result_path.exists())
            self.assertEqual(summary["deleted_results"], 1)


if __name__ == "__main__":
    import unittest

    unittest.main()
