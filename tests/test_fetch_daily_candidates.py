from __future__ import annotations

import copy
import csv
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest import TestCase, mock
from zoneinfo import ZoneInfo

import requests

from src.candidate_analysis import assess_language, assign_event_groups, calculate_interest, calculate_topic_relevance, title_similarity
from src.candidate_selection import select_candidates
from src.fetch_daily_candidates import SearchQuotaExceeded, YouTubeAPIError, YouTubeClient, build_candidates, build_day_window, collect_popular, collect_search_ids, int_value, load_config, parse_iso8601_duration, plan_query_groups, write_csv


ROOT = Path(__file__).resolve().parents[1]


class Response:
    def __init__(
        self,
        status: int,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code, self._payload = status, payload or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload


def config() -> dict:
    return json.loads((ROOT / "config" / "trending_config.json").read_text(encoding="utf-8"))


def window():
    return build_day_window("2026-07-20", "America/New_York", datetime(2026, 7, 20, 12, tzinfo=ZoneInfo("America/New_York")))


def resource(title: str, *, description: str = "An English narrated explanation with useful results", views: int = 50000, category: str = "28", audio: str = "en", published: str = "2026-07-20T15:00:00Z", channel: str = "Channel", duration: str = "PT8M", live: str = "none") -> dict:
    return {
        "snippet": {"title": title, "description": description, "channelTitle": channel, "categoryId": category, "publishedAt": published, "liveBroadcastContent": live, "defaultAudioLanguage": audio, "tags": title.split()},
        "contentDetails": {"duration": duration, "caption": "true"},
        "statistics": {"viewCount": str(views), "likeCount": str(max(1, views // 20)), "commentCount": str(max(1, views // 500))},
        "status": {"privacyStatus": "public", "license": "youtube", "embeddable": True},
    }


def selection_row(index: int, topic: str, *, query_group: str = "", popular: bool = False, channel: str | None = None, event: str | None = None, score: float = 70) -> dict:
    searches = [{"search_mode": "fresh", "query_group": query_group, "query_text": "query", "search_rank": 1}] if query_group else []
    return {"video_id": f"v{index}", "primary_topic": topic, "channel_title": channel or f"channel-{index}", "semantic_duplicate_group": event or f"event-{index}", "search_source_details": searches, "popular_source_details": ["All"] if popular else [], "final_score": score, "interest_score": 15, "growth_score": 70, "copyright_risk": "medium", "localization_suitability": "high"}


class DiscoveryTests(TestCase):
    def test_config_and_quotas(self) -> None:
        loaded = load_config(ROOT / "config" / "trending_config.json")
        self.assertEqual(loaded["candidate_limit"], 50)
        self.assertEqual(sum(loaded["topic_quotas"].values()), 50)
        self.assertEqual(loaded["popular_target"], 2)
        self.assertEqual(loaded["popular_max"], 5)

    def test_durations(self) -> None:
        self.assertEqual(parse_iso8601_duration("PT45S"), 45)
        self.assertEqual(parse_iso8601_duration("PT12M30S"), 750)
        self.assertEqual(parse_iso8601_duration("PT1H02M03S"), 3723)

    def test_dst_windows(self) -> None:
        summer = build_day_window("2026-07-20", "America/New_York", datetime(2026, 7, 21, tzinfo=ZoneInfo("America/New_York")))
        winter = build_day_window("2026-01-20", "America/New_York", datetime(2026, 1, 21, tzinfo=ZoneInfo("America/New_York")))
        self.assertEqual(summer.start_local.utcoffset().total_seconds(), -14400)
        self.assertEqual(winter.start_local.utcoffset().total_seconds(), -18000)

    def test_missing_statistics(self) -> None:
        self.assertEqual(int_value({}, "viewCount"), 0)
        self.assertEqual(int_value({"likeCount": None}, "likeCount"), 0)

    def test_fresh_growth_modes_or_query_and_one_page(self) -> None:
        cfg = config(); cfg["topic_groups"] = {"gaming": {"queries": ["Minecraft hardcore|Minecraft survival"]}}
        cfg["topic_quotas"] = {"gaming": 1, "wildcard_popular": 0}
        cfg["core_queries"] = {"gaming": ["Minecraft hardcore|Minecraft survival"]}
        cfg["rotating_queries_per_topic"] = {"gaming": 0}
        cfg["search_core_query_groups_per_day"] = 1
        client = mock.Mock(); client.get.side_effect = [{"items": []}, {"items": []}]
        collect_search_ids(client, cfg, window())
        self.assertEqual(client.get.call_count, 2)
        fresh, growth = (call.args[1] for call in client.get.call_args_list)
        self.assertEqual(fresh["q"], "Minecraft hardcore|Minecraft survival")
        self.assertEqual(fresh["maxResults"], 50)
        self.assertEqual(fresh["order"], "date"); self.assertEqual(growth["order"], "viewCount")
        fresh_hours = (window().end_utc - datetime.fromisoformat(fresh["publishedAfter"].replace("Z", "+00:00"))).total_seconds() / 3600
        growth_hours = (window().end_utc - datetime.fromisoformat(growth["publishedAfter"].replace("Z", "+00:00"))).total_seconds() / 3600
        self.assertEqual((fresh_hours, growth_hours), (24, 72))
        self.assertNotIn("pageToken", fresh); self.assertNotIn("pageToken", growth)

    def test_daily_query_plan_respects_search_budget(self) -> None:
        cfg = config(); planned = plan_query_groups(cfg, window().local_date)
        self.assertEqual(len(planned), 24)
        self.assertEqual(len(planned) * len(cfg["search_modes"]), 48)
        self.assertLess(len(planned) * len(cfg["search_modes"]), cfg["search_daily_budget"])
        represented = Counter(topic for topic, _, _ in planned)
        for topic in cfg["topic_quotas"]:
            if topic != "wildcard_popular": self.assertGreater(represented[topic], 0)

    def test_query_groups_rotate_by_date(self) -> None:
        cfg = config()
        first = {(topic, index) for topic, index, _ in plan_query_groups(cfg, datetime(2026, 7, 20).date())}
        second = {(topic, index) for topic, index, _ in plan_query_groups(cfg, datetime(2026, 7, 21).date())}
        self.assertEqual((len(first), len(second)), (24, 24))
        self.assertNotEqual(first, second)
        self.assertGreater(len(first | second), 24)

    def test_fixed_core_queries_run_every_day(self) -> None:
        cfg = config()
        for day in (20, 21, 22):
            plan = plan_query_groups(cfg, datetime(2026, 7, day).date())
            fixed = {(topic, query) for topic, group_name, query in plan if group_name.startswith("core_")}
            expected = {(topic, query) for topic, queries in cfg["core_queries"].items() for query in queries}
            self.assertEqual(fixed, expected)
        gardening_core = "|".join(cfg["core_queries"]["gardening_farming"]).casefold()
        gaming_core = "|".join(cfg["core_queries"]["gaming"]).casefold()
        for phrase in ("vegetable garden", "growing vegetables", "growing tomatoes", "garden update", "garden harvest", "backyard garden", "gardening experiment", "homestead garden"):
            self.assertIn(phrase, gardening_core)
        for phrase in ("minecraft survival", "minecraft hardcore", "minecraft 100 days", "minecraft challenge", "minecraft mod", "minecraft funny moments", "trending game gameplay", "new game gameplay"):
            self.assertIn(phrase, gaming_core)

    def test_search_sources_keep_mode_group_text_and_rank(self) -> None:
        cfg = config(); cfg["topic_groups"] = {"gaming": {"queries": ["Minecraft challenge|Minecraft but"]}}
        cfg["topic_quotas"] = {"gaming": 1, "wildcard_popular": 0}
        cfg["core_queries"] = {"gaming": ["Minecraft challenge|Minecraft but"]}
        cfg["rotating_queries_per_topic"] = {"gaming": 0}
        cfg["search_core_query_groups_per_day"] = 1
        client = mock.Mock(); client.get.side_effect = [{"items": [{"id": {"videoId": "x"}}]}, {"items": [{"id": {"videoId": "x"}}]}]
        ids, sources, _ = collect_search_ids(client, cfg, window())
        self.assertEqual(ids, {"x"}); self.assertEqual(len(sources["x"]), 2)
        self.assertTrue(any("search::fresh::gaming_core_01::1::Minecraft challenge|Minecraft but" == value for value in sources["x"]))

    def test_popular_pagination_uses_next_page_token(self) -> None:
        client = mock.Mock(); client.get.side_effect = [{"items": [{"id": "a"}], "nextPageToken": "next"}, {"items": [{"id": "b"}]}]
        cfg = config(); cfg["popular_category_titles"] = []; cfg["popular_pages_per_feed"] = 2
        resources, _, _ = collect_popular(client, cfg, {})
        self.assertEqual(set(resources), {"a", "b"})
        self.assertEqual(client.get.call_args_list[1].args[1]["pageToken"], "next")

    def test_popular_404_is_skipped(self) -> None:
        client = mock.Mock(); client.get.side_effect = YouTubeAPIError("missing", status_code=404)
        cfg = config(); cfg["include_region_wide_popular"] = False; cfg["popular_category_titles"] = ["Gaming"]
        self.assertEqual(collect_popular(client, cfg, {"Gaming": "20"})[0], {})

    def test_403_is_fatal_and_retry_is_supported(self) -> None:
        session = mock.Mock(); session.headers = {}; session.get.return_value = Response(403, {"error": {"message": "quotaExceeded"}})
        with self.assertRaises(YouTubeAPIError): YouTubeClient("secret", max_retries=0, session=session).get("videos", {})
        session.get.side_effect = [requests.ConnectionError("offline"), Response(200, {"items": []})]
        with mock.patch("src.fetch_daily_candidates.time.sleep"):
            self.assertEqual(YouTubeClient("secret", max_retries=1, session=session).get("videos", {}), {"items": []})

    def test_daily_search_quota_429_is_not_retried(self) -> None:
        session = mock.Mock(); session.headers = {}
        session.get.return_value = Response(429, {"error": {"message": "Quota exceeded for quota metric 'Search Queries' and limit 'Search Queries per day'", "errors": [{"reason": "quotaExceeded"}]}})
        with self.assertRaises(SearchQuotaExceeded):
            YouTubeClient("secret", max_retries=4, session=session).get("search", {})
        self.assertEqual(session.get.call_count, 1)

    def test_temporary_429_is_retried(self) -> None:
        session = mock.Mock(); session.headers = {}
        session.get.side_effect = [Response(429, {"error": {"message": "Too many requests", "errors": [{"reason": "rateLimitExceeded"}]}}), Response(200, {"items": []})]
        with mock.patch("src.fetch_daily_candidates.time.sleep") as sleep, mock.patch(
            "src.fetch_daily_candidates.random.uniform", return_value=0.0
        ):
            self.assertEqual(YouTubeClient("secret", max_retries=1, session=session).get("search", {}), {"items": []})
        sleep.assert_called_once_with(1.0)
        self.assertEqual(session.get.call_count, 2)

    def test_temporary_http_errors_use_exponential_backoff(self) -> None:
        session = mock.Mock(); session.headers = {}
        temporary = Response(503, {"error": {"message": "Unavailable"}})
        session.get.side_effect = [temporary, temporary, temporary, Response(200, {"items": []})]
        with mock.patch("src.fetch_daily_candidates.time.sleep") as sleep, mock.patch(
            "src.fetch_daily_candidates.random.uniform", return_value=0.0
        ):
            self.assertEqual(
                YouTubeClient("secret", max_retries=3, session=session).get("videos", {}),
                {"items": []},
            )
        self.assertEqual(sleep.call_args_list, [mock.call(1.0), mock.call(2.0), mock.call(4.0)])

    def test_retry_after_header_is_honored(self) -> None:
        session = mock.Mock(); session.headers = {}
        session.get.side_effect = [
            Response(429, {"error": {"message": "Too many requests"}}, {"Retry-After": "7"}),
            Response(200, {"items": []}),
        ]
        with mock.patch("src.fetch_daily_candidates.time.sleep") as sleep, mock.patch(
            "src.fetch_daily_candidates.random.uniform", return_value=0.0
        ):
            self.assertEqual(YouTubeClient("secret", max_retries=1, session=session).get("videos", {}), {"items": []})
        sleep.assert_called_once_with(7.0)

    def test_temporary_errors_stop_after_max_retries(self) -> None:
        session = mock.Mock(); session.headers = {}
        session.get.return_value = Response(503, {"error": {"message": "Unavailable"}})
        with mock.patch("src.fetch_daily_candidates.time.sleep"), mock.patch(
            "src.fetch_daily_candidates.random.uniform", return_value=0.0
        ), self.assertRaises(YouTubeAPIError):
            YouTubeClient("secret", max_retries=2, session=session).get("videos", {})
        self.assertEqual(session.get.call_count, 3)

    def test_partial_search_results_are_checkpointed_on_quota_exhaustion(self) -> None:
        cfg = config(); cfg["topic_groups"] = {"gaming": {"queries": ["Minecraft challenge|Minecraft but"]}}; cfg["topic_quotas"] = {"gaming": 1, "wildcard_popular": 0}; cfg["search_core_query_groups_per_day"] = 1
        client = mock.Mock(); client.get.side_effect = [{"items": [{"id": {"videoId": "saved"}}]}, SearchQuotaExceeded("Search Queries per day quota exceeded")]
        checkpoint = ROOT / "tests" / "_search_checkpoint.json"; stats = {}
        try:
            with self.assertRaises(SearchQuotaExceeded) as raised:
                collect_search_ids(client, cfg, window(), checkpoint, stats)
            self.assertIn("saved", raised.exception.partial_ids)
            payload = json.loads(checkpoint.read_text(encoding="utf-8"))
            self.assertIn("saved", payload["video_ids"])
            self.assertEqual(payload["stats"]["executed_requests"], 1)
        finally:
            checkpoint.unlink(missing_ok=True)


class AnalysisTests(TestCase):
    def test_duration_and_relaxed_topic_thresholds(self) -> None:
        cfg = config()
        self.assertEqual((cfg["min_duration_seconds"], cfg["max_duration_seconds"]), (60, 2700))
        self.assertEqual((cfg["topic_min_view_count"]["gardening_farming"], cfg["topic_min_views_per_hour"]["gardening_farming"]), (200, 20))
        self.assertEqual((cfg["topic_min_view_count"]["experiments_challenges"], cfg["topic_min_views_per_hour"]["experiments_challenges"]), (300, 30))
    def test_interest_phrase_scoring_once(self) -> None:
        result = calculate_interest("I tested and I TESTED this benchmark comparison", "ai_model_testing", config())
        self.assertIn("i tested", result["interest_hits"])
        self.assertEqual(result["interest_hits"].count("i tested"), 1)
        self.assertGreaterEqual(result["interest_score"], 14)

    def test_boring_penalty(self) -> None:
        result = calculate_interest("No commentary compilation and passive income promo", "gaming", config())
        self.assertGreaterEqual(result["boring_penalty"], 15)
        self.assertIn("no commentary", result["boring_hits"])

    def test_topic_recognition(self) -> None:
        cfg = config()
        cases = [
            ("Minecraft hardcore 100 days challenge", "gaming"),
            ("Tomato harvest results from my vegetable garden", "gardening_farming"),
            ("I tested the new Claude AI model benchmark", "ai_model_testing"),
            ("Political commentary: why this tariff policy matters", "political_commentary"),
        ]
        for title, expected in cases:
            snippet = {"title": title, "description": "Detailed explained reaction and results", "tags": title.split(), "channelTitle": "Creator"}
            primary, _, score, _ = calculate_topic_relevance(snippet, set(), "People & Blogs", cfg)
            self.assertEqual(primary, expected, title); self.assertGreaterEqual(score, cfg["topic_relevance_min"])

    def test_query_source_is_not_sufficient_for_topic(self) -> None:
        primary, _, score, _ = calculate_topic_relevance({"title": "A quiet house tour", "description": "Interior rooms", "tags": [], "channelTitle": "Garden House"}, {"gardening_farming"}, "People & Blogs", config())
        self.assertEqual(primary, "other"); self.assertLess(score, config()["topic_relevance_min"])

    def test_explicit_non_english_marker_is_rejected(self) -> None:
        result = assess_language({"title": "Tutorial Español", "description": "Guía completa"}, False, config()["language_markers"])
        self.assertFalse(result["is_english"]); self.assertEqual(result["language_filter_reason"], "non_english_marker")

    def test_audio_language_overrides_marker(self) -> None:
        result = assess_language({"title": "Spanish policy explained", "defaultAudioLanguage": "en-US"}, False, config()["language_markers"])
        self.assertTrue(result["is_english"]); self.assertEqual(result["language_confidence"], "high")

    def test_event_similarity_and_grouping(self) -> None:
        jaccard, sequence = title_similarity("Official reaction: New AI Model Test", "New AI Model Test Review", config()["event_similarity"]["stop_words"])
        self.assertTrue(jaccard >= 0.65 or sequence >= 0.78)
        rows = [{"title": "Official reaction: New AI Model Test", "final_score": 80}, {"title": "New AI Model Test Review", "final_score": 75}, {"title": "Tomato harvest experiment", "final_score": 70}]
        assign_event_groups(rows, config())
        self.assertEqual(rows[0]["semantic_duplicate_group"], rows[1]["semantic_duplicate_group"])
        self.assertNotEqual(rows[0]["semantic_duplicate_group"], rows[2]["semantic_duplicate_group"])

    def test_hard_exclude_remains_in_raw_pool(self) -> None:
        cfg = config(); raw, counts = [], Counter()
        rows = build_candidates({"x": resource("Official Trailer for a New Movie", category="1")}, {"x": {"mostPopular:All"}}, {}, {"1": "Film & Animation"}, cfg, window(), counts, raw)
        self.assertEqual(rows, []); self.assertEqual(raw[0]["filter_reason"], "hard_exclude"); self.assertEqual(counts["hard_exclude"], 1)

    def test_absolute_heat_threshold_and_pending_state(self) -> None:
        cfg = config(); source = {"search::fresh::ai_model_testing_01::1::new AI model test"}
        low = build_candidates({"x": resource("I tested a new AI model benchmark", views=10)}, {"x": source}, {"x": {"ai_model_testing"}}, {"28": "Science & Technology"}, cfg, window(), Counter())
        self.assertEqual(low, [])
        accepted = build_candidates({"x": resource("I tested a new AI model benchmark", views=50000)}, {"x": source}, {"x": {"ai_model_testing"}}, {"28": "Science & Technology"}, cfg, window(), Counter())
        self.assertEqual(accepted[0]["rights_status"], "PENDING"); self.assertEqual(accepted[0]["selected"], 0)


class SelectionTests(TestCase):
    def test_50_candidates_and_topic_quotas(self) -> None:
        cfg = config(); rows = []; index = 0
        for topic, quota in cfg["topic_quotas"].items():
            if topic == "wildcard_popular": continue
            for _ in range(quota):
                rows.append(selection_row(index, topic, query_group=f"{topic}_{index // 4:02d}")); index += 1
        for _ in range(cfg["topic_quotas"]["wildcard_popular"]):
            rows.append(selection_row(index, "wildcard_popular", popular=True)); index += 1
        selected = select_candidates(rows, cfg, 50)
        self.assertEqual(len(selected), 50); self.assertEqual(len({row["video_id"] for row in selected}), 50)
        counts = Counter(row["selection_topic"] for row in selected)
        self.assertEqual(counts, Counter(cfg["topic_quotas"]))
        self.assertGreaterEqual(sum(bool(row["search_source_details"]) for row in selected) / 50, 0.8)

    def test_channel_limit(self) -> None:
        cfg = config()
        rows = [selection_row(i, "gaming", query_group=f"gaming_{i:02d}", channel="same", event=f"e{i}", score=100-i) for i in range(8)]
        selected = select_candidates(rows, cfg, 8)
        self.assertLessEqual(Counter(row["channel_title"] for row in selected)["same"], 2)

    def test_event_limit(self) -> None:
        cfg = config()
        rows = [selection_row(i, "gaming", query_group=f"gaming_{i:02d}", channel=f"c{i}", event="same-event", score=100-i) for i in range(8)]
        selected = select_candidates(rows, cfg, 8)
        self.assertLessEqual(Counter(row["semantic_duplicate_group"] for row in selected)["same-event"], 2)

    def test_query_group_limit(self) -> None:
        cfg = config()
        rows = [selection_row(i, "gaming", query_group="gaming_01", channel=f"c{i}", event=f"e{i}", score=100-i) for i in range(8)]
        selected = select_candidates(rows, cfg, 8)
        self.assertLessEqual(Counter(row["contributing_query_group"] for row in selected)["gaming_01"], 6)

    def test_popular_only_maximum(self) -> None:
        cfg = config()
        rows = [selection_row(i, "gaming", popular=True, channel=f"c{i}", event=f"e{i}", score=100-i) for i in range(12)]
        selected = select_candidates(rows, cfg, 12)
        self.assertLessEqual(sum(not row["search_source_details"] for row in selected), cfg["popular_max"])

    def test_wildcard_never_exceeds_two_even_during_backfill(self) -> None:
        cfg = config()
        rows = [selection_row(i, "wildcard_popular", popular=True, channel=f"c{i}", event=f"e{i}", score=100-i) for i in range(10)]
        selected = select_candidates(rows, cfg, 10)
        self.assertLessEqual(sum(row["selection_topic"] == "wildcard_popular" for row in selected), 2)

    def test_topic_shortage_uses_global_backfill_without_breaking_limits(self) -> None:
        cfg = config(); rows = [selection_row(i, "gaming", query_group=f"g{i // 4}") for i in range(20)] + [selection_row(100+i, "tutorials", query_group=f"t{i // 4}") for i in range(10)]
        selected = select_candidates(rows, cfg, 30)
        self.assertEqual(len(selected), 22)
        counts = Counter(row["selection_topic"] for row in selected)
        self.assertEqual(counts["gaming"], cfg["topic_max_counts"]["gaming"])
        self.assertEqual(counts["tutorials"], cfg["topic_max_counts"]["tutorials"])

    def test_gaming_never_exceeds_fourteen_during_backfill(self) -> None:
        cfg = config()
        rows = [selection_row(i, "gaming", query_group=f"g{i}") for i in range(30)]
        rows += [selection_row(100 + i, "ai_model_testing", query_group=f"a{i}") for i in range(10)]
        selected = select_candidates(rows, cfg, 40)
        self.assertLessEqual(Counter(row["selection_topic"] for row in selected)["gaming"], 14)

    def test_csv_utf8_special_text_and_structured_sources(self) -> None:
        path = ROOT / "tests" / "_output.csv"; row = {field: "" for field in __import__("src.fetch_daily_candidates", fromlist=["CSV_FIELDS"]).CSV_FIELDS}
        row.update({"title": 'Hello, "世界" 😀', "all_topics": ["tutorials"], "interest_hits": ["guide"], "boring_hits": [], "topic_keyword_hits": {"tutorials": ["guide"]}, "search_source_details": [{"search_mode": "fresh"}], "popular_source_details": [], "rights_status": "PENDING", "selected": 0})
        try:
            write_csv(path, [row]); self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with path.open(encoding="utf-8-sig", newline="") as handle: self.assertEqual(next(csv.DictReader(handle))["title"], row["title"])
        finally: path.unlink(missing_ok=True)


if __name__ == "__main__":
    import unittest
    unittest.main()
