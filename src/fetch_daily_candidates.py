from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

try:
    from .candidate_analysis import assess_language, assign_event_groups, calculate_interest, calculate_topic_relevance, copyright_risk, metric_scores, phrase_hits
    from .candidate_selection import select_candidates
except ImportError:  # Direct execution: python src/fetch_daily_candidates.py
    from candidate_analysis import assess_language, assign_event_groups, calculate_interest, calculate_topic_relevance, copyright_risk, metric_scores, phrase_hits
    from candidate_selection import select_candidates

API_BASE = "https://www.googleapis.com/youtube/v3"
LOGGER = logging.getLogger("youtube_candidates")


class YouTubeAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, endpoint: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.endpoint = endpoint


class SearchQuotaExceeded(YouTubeAPIError):
    """Permanent daily Search Queries quota exhaustion; retrying cannot help."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=429, endpoint="search")
        self.partial_ids: set[str] = set()
        self.partial_sources: dict[str, set[str]] = {}
        self.partial_topics: dict[str, set[str]] = {}


@dataclass(frozen=True)
class DayWindow:
    local_date: date
    start_local: datetime
    end_local: datetime
    start_utc: datetime
    end_utc: datetime


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discover interesting English YouTube videos for manual localization review.")
    parser.add_argument("--config", default="config/trending_config.json")
    parser.add_argument("--date", help="Target US/Eastern date (YYYY-MM-DD).")
    parser.add_argument("--limit", type=int)
    return parser.parse_args(argv)


def setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"fetch_candidates_{datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(path, encoding="utf-8")], force=True)
    LOGGER.info("Log file: %s", path)
    return path


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open(encoding="utf-8-sig") as handle:
        config = json.load(handle)
    required = {"region_code", "timezone", "candidate_limit", "topic_groups", "topic_quotas", "search_modes", "final_score_weights"}
    missing = sorted(required - config.keys())
    if missing:
        raise ValueError(f"Config is missing required fields: {', '.join(missing)}")
    limit = int(config["candidate_limit"])
    if limit < 1 or sum(int(value) for value in config["topic_quotas"].values()) != limit:
        raise ValueError("topic_quotas must sum to candidate_limit")
    for topic, target in config["topic_quotas"].items():
        if int(config["topic_max_counts"][topic]) < int(target):
            raise ValueError(f"topic_max_counts.{topic} cannot be below its target quota")
    if abs(sum(float(value) for value in config["final_score_weights"].values()) - 1.0) > 0.0001:
        raise ValueError("final_score_weights must sum to 1.0")
    for key in ("max_per_channel", "max_per_event", "max_per_query_group", "popular_pages_per_feed"):
        if int(config[key]) < 1:
            raise ValueError(f"{key} must be positive")
    daily_budget = int(config["search_daily_budget"])
    reserve = int(config["search_reserve_calls"])
    planned = int(config["search_core_query_groups_per_day"]) * len(config["search_modes"])
    if daily_budget < 1 or reserve < 0 or daily_budget + reserve > 100:
        raise ValueError("search_daily_budget + search_reserve_calls must be between 1 and the default 100-call hard limit")
    if planned >= daily_budget:
        raise ValueError("planned search requests must remain strictly below search_daily_budget")
    core_count = sum(len(values) for values in config["core_queries"].values())
    rotating_count = sum(int(value) for value in config["rotating_queries_per_topic"].values())
    if core_count + rotating_count > int(config["search_core_query_groups_per_day"]):
        raise ValueError("fixed core plus rotating queries exceed search_core_query_groups_per_day")
    ZoneInfo(str(config["timezone"]))
    return config


def build_day_window(date_text: str | None, timezone_name: str, now: datetime | None = None) -> DayWindow:
    tz = ZoneInfo(timezone_name)
    current = (now or datetime.now(tz)).astimezone(tz)
    target = date.fromisoformat(date_text) if date_text else current.date()
    if target > current.date():
        raise ValueError("Target date cannot be in the future")
    start = datetime.combine(target, dt_time.min, tzinfo=tz)
    end = current if target == current.date() else datetime.combine(target, dt_time.max, tzinfo=tz)
    return DayWindow(target, start, end, start.astimezone(timezone.utc), end.astimezone(timezone.utc))


class YouTubeClient:
    def __init__(self, api_key: str, timeout: int = 30, max_retries: int = 4, session: requests.Session | None = None) -> None:
        self.api_key, self.timeout, self.max_retries = api_key, timeout, max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "YouTubeWorkflowCandidateFetcher/3.0"})

    def get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(f"{API_BASE}/{endpoint}", params={**params, "key": self.api_key}, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise YouTubeAPIError(f"Network request failed after retries: {exc}", endpoint=endpoint) from exc
                self._backoff(attempt, f"network error: {exc}"); continue
            if response.status_code == 200:
                try:
                    return response.json()
                except requests.JSONDecodeError as exc:
                    raise YouTubeAPIError("YouTube API returned invalid JSON", status_code=200, endpoint=endpoint) from exc
            detail = self._error_detail(response)
            if response.status_code == 429 and endpoint == "search" and self._is_daily_search_quota(response):
                raise SearchQuotaExceeded("Search Queries per day quota exceeded. Partial discovery will be saved; do not retry today. " + detail)
            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                self._backoff(attempt, f"HTTP {response.status_code}"); continue
            if response.status_code == 403:
                raise YouTubeAPIError("YouTube API returned HTTP 403. Check API enablement, key validity, quota, and key restrictions. " + detail, status_code=403, endpoint=endpoint)
            raise YouTubeAPIError(f"YouTube API HTTP {response.status_code}: {detail}", status_code=response.status_code, endpoint=endpoint)
        raise YouTubeAPIError("Request failed unexpectedly", endpoint=endpoint)

    @staticmethod
    def _error_detail(response: requests.Response) -> str:
        try:
            return str(response.json().get("error", {}).get("message", "Unknown API error"))[:500]
        except (ValueError, AttributeError):
            return "Non-JSON API error response"

    @staticmethod
    def _is_daily_search_quota(response: requests.Response) -> bool:
        try:
            payload_text = json.dumps(response.json(), ensure_ascii=False).casefold()
        except (ValueError, TypeError):
            payload_text = ""
        return "search quer" in payload_text and ("per day" in payload_text or "daily" in payload_text)

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        delay = min(2**attempt, 30)
        LOGGER.warning("Temporary %s; retrying in %d second(s)", reason, delay)
        time.sleep(delay)


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def parse_iso8601_duration(value: str) -> int:
    match = re.fullmatch(r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?", value or "")
    if not match:
        return 0
    parts = {key: int(number or 0) for key, number in match.groupdict().items()}
    return parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]


def format_duration(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600); minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def int_value(mapping: dict[str, Any], key: str) -> int:
    try:
        return int(mapping.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def best_thumbnail(snippet: dict[str, Any]) -> str:
    thumbnails = snippet.get("thumbnails", {})
    for name in ("maxres", "standard", "high", "medium", "default"):
        if thumbnails.get(name, {}).get("url"):
            return str(thumbnails[name]["url"])
    return ""


def get_category_map(client: YouTubeClient, region: str) -> tuple[dict[str, str], dict[str, str]]:
    payload = client.get("videoCategories", {"part": "snippet", "regionCode": region, "hl": "en_US"})
    title_to_id, id_to_title = {}, {}
    for item in payload.get("items", []):
        category_id = str(item["id"]); title = str(item.get("snippet", {}).get("title", category_id))
        title_to_id[title], id_to_title[category_id] = category_id, title
    return title_to_id, id_to_title


def collect_popular(client: YouTubeClient, config: dict[str, Any], title_to_id: dict[str, str]) -> tuple[dict[str, Any], dict[str, set[str]], dict[str, set[str]]]:
    resources, sources, topics = {}, defaultdict(set), defaultdict(set)
    feeds: list[tuple[str, str | None]] = [("All", None)] if config.get("include_region_wide_popular", True) else []
    feeds.extend((title, title_to_id.get(title)) for title in config["popular_category_titles"])
    for title, category_id in feeds:
        if title != "All" and not category_id:
            LOGGER.warning("Category %s unavailable in %s; skipping", title, config["region_code"]); continue
        page_token: str | None = None
        for _ in range(int(config["popular_pages_per_feed"])):
            params: dict[str, Any] = {"part": "snippet,contentDetails,statistics,status", "chart": "mostPopular", "regionCode": config["region_code"], "maxResults": min(int(config["popular_results_per_category"]), 50)}
            if category_id: params["videoCategoryId"] = category_id
            if page_token: params["pageToken"] = page_token
            try:
                payload = client.get("videos", params)
            except YouTubeAPIError as exc:
                if exc.status_code in {400, 404}:
                    LOGGER.warning("mostPopular feed %s returned HTTP %s; skipped", title, exc.status_code); break
                raise
            for item in payload.get("items", []):
                video_id = str(item["id"]); resources[video_id] = item; sources[video_id].add(f"mostPopular:{title}")
                mapped = config.get("category_to_topic", {}).get(title)
                if mapped: topics[video_id].add(mapped)
            page_token = payload.get("nextPageToken")
            if not page_token: break
    return resources, sources, topics


def _save_search_checkpoint(path: Path, target_date: date, ids: set[str], sources: dict[str, set[str]], topics: dict[str, set[str]], completed: set[str], stats: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": target_date.isoformat(), "saved_at": datetime.now(timezone.utc).isoformat(), "video_ids": sorted(ids), "sources": {key: sorted(value) for key, value in sources.items()}, "topics": {key: sorted(value) for key, value in topics.items()}, "completed_requests": sorted(completed), "stats": stats}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _load_search_checkpoint(path: Path, target_date: date) -> tuple[set[str], dict[str, set[str]], dict[str, set[str]], set[str]]:
    if not path.is_file():
        return set(), defaultdict(set), defaultdict(set), set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Ignoring unreadable search checkpoint: %s", path)
        return set(), defaultdict(set), defaultdict(set), set()
    if payload.get("date") != target_date.isoformat():
        return set(), defaultdict(set), defaultdict(set), set()
    return set(payload.get("video_ids", [])), defaultdict(set, {key: set(value) for key, value in payload.get("sources", {}).items()}), defaultdict(set, {key: set(value) for key, value in payload.get("topics", {}).items()}), set(payload.get("completed_requests", []))


def collect_search_ids(client: YouTubeClient, config: dict[str, Any], window: DayWindow, checkpoint_path: Path | None = None, plan_stats: dict[str, int] | None = None) -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    if checkpoint_path is not None:
        ids, sources, topics, completed = _load_search_checkpoint(checkpoint_path, window.local_date)
    else:
        ids, sources, topics, completed = set(), defaultdict(set), defaultdict(set), set()
    planned = plan_query_groups(config, window.local_date)
    total = sum(len(group["queries"]) for group in config["topic_groups"].values())
    planned_requests = len(planned) * len(config["search_modes"])
    stats = plan_stats if plan_stats is not None else {}
    stats.update({"configured_groups": total, "planned_groups": len(planned), "planned_requests": planned_requests, "executed_requests": 0, "resumed_requests": 0, "remaining_safe_budget": int(config["search_daily_budget"]) - len(completed)})
    LOGGER.info("Search plan: configured_groups=%d planned_groups=%d requests=%d daily_budget=%d reserve=%d", total, len(planned), planned_requests, int(config["search_daily_budget"]), int(config["search_reserve_calls"]))
    for topic in config["topic_quotas"]:
        if topic == "wildcard_popular": continue
        fixed_names = [group_name for planned_topic, group_name, _ in planned if planned_topic == topic and group_name.startswith("core_")]
        rotating_names = [group_name for planned_topic, group_name, _ in planned if planned_topic == topic and group_name.startswith("tail_")]
        LOGGER.info("Search topic %s: fixed_core=%s rotating=%s", topic, ",".join(fixed_names) or "none", ",".join(rotating_names) or "none")
    for topic, group_name, query in planned:
        query_group = f"{topic}_{group_name}"
        for mode, settings in config["search_modes"].items():
            request_key = f"{query_group}::{mode}"
            if request_key in completed:
                stats["resumed_requests"] += 1
                continue
            params = {"part": "snippet", "type": "video", "q": query, "maxResults": min(int(config["search_results_per_query"]), 50), "order": settings["order"], "publishedAfter": (window.end_utc - timedelta(hours=int(settings["hours"]))).isoformat().replace("+00:00", "Z"), "publishedBefore": window.end_utc.isoformat().replace("+00:00", "Z"), "regionCode": config["region_code"], "relevanceLanguage": config["language"], "safeSearch": config["safe_search"], "videoDefinition": config["video_definition"]}
            try:
                payload = client.get("search", params)  # One page only by design.
            except SearchQuotaExceeded as exc:
                stats["remaining_safe_budget"] = 0
                if checkpoint_path is not None:
                    _save_search_checkpoint(checkpoint_path, window.local_date, ids, sources, topics, completed, stats)
                exc.partial_ids, exc.partial_sources, exc.partial_topics = set(ids), {key: set(value) for key, value in sources.items()}, {key: set(value) for key, value in topics.items()}
                raise
            for rank, item in enumerate(payload.get("items", []), 1):
                video_id = item.get("id", {}).get("videoId")
                if video_id:
                    ids.add(video_id); topics[video_id].add(topic)
                    sources[video_id].add(f"search::{mode}::{query_group}::{rank}::{query}")
            completed.add(request_key)
            stats["executed_requests"] += 1
            stats["remaining_safe_budget"] = max(0, int(config["search_daily_budget"]) - len(completed))
            if checkpoint_path is not None:
                _save_search_checkpoint(checkpoint_path, window.local_date, ids, sources, topics, completed, stats)
    LOGGER.info("Search execution: executed=%d resumed=%d remaining_safe_budget=%d", stats["executed_requests"], stats["resumed_requests"], stats["remaining_safe_budget"])
    return ids, sources, topics


def plan_query_groups(config: dict[str, Any], target_date: date) -> list[tuple[str, str, str]]:
    modes = max(1, len(config["search_modes"]))
    budget_capacity = max(1, (int(config["search_daily_budget"]) - 1) // modes)
    capacity = min(int(config["search_core_query_groups_per_day"]), budget_capacity)
    topics = [topic for topic in config["topic_quotas"] if topic != "wildcard_popular"]
    planned: list[tuple[str, str, str]] = []
    for topic in topics:
        for index, query in enumerate(config["core_queries"][topic], 1):
            planned.append((topic, f"core_{index:02d}", query))
    remaining = max(0, capacity - len(planned))
    rotation = target_date.toordinal()
    for topic in topics:
        queries = config["topic_groups"][topic]["queries"]
        take = min(int(config["rotating_queries_per_topic"].get(topic, 0)), len(queries), remaining)
        start = (rotation * max(1, take)) % len(queries)
        for offset in range(take):
            index = (start + offset) % len(queries)
            planned.append((topic, f"tail_{index + 1:02d}", queries[index]))
        remaining -= take
    return planned[:capacity]


def get_video_details(client: YouTubeClient, video_ids: Iterable[str]) -> dict[str, Any]:
    resources = {}
    for batch in chunks(sorted(set(video_ids)), 50):
        payload = client.get("videos", {"part": "snippet,contentDetails,statistics,status", "id": ",".join(batch), "maxResults": 50})
        resources.update({str(item["id"]): item for item in payload.get("items", [])})
    return resources


def _search_details(source_values: list[str]) -> list[dict[str, Any]]:
    details = []
    for value in source_values:
        parts = value.split("::", 4)
        if len(parts) == 5 and parts[0] == "search":
            details.append({"search_mode": parts[1], "query_group": parts[2], "search_rank": int(parts[3]), "query_text": parts[4]})
    return sorted(details, key=lambda item: (item["query_group"], item["search_mode"], item["search_rank"]))


def build_candidates(resources: dict[str, Any], sources: dict[str, set[str]], buckets: dict[str, set[str]], category_titles: dict[str, str], config: dict[str, Any], window: DayWindow, rejection_counts: Counter[str] | None = None, raw_output: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []; counts = rejection_counts if rejection_counts is not None else Counter()
    blocked_channels = {str(value).casefold() for value in config.get("blacklist_channels", [])}

    def reject(row: dict[str, Any], reason: str) -> None:
        row.update({"eligible": False, "filter_reason": reason}); counts[reason] += 1
        if raw_output is not None: raw_output.append(row)

    for video_id, item in resources.items():
        snippet, statistics, content, status = (item.get(name, {}) for name in ("snippet", "statistics", "contentDetails", "status"))
        title = html.unescape(str(snippet.get("title", ""))).strip(); channel = html.unescape(str(snippet.get("channelTitle", ""))).strip()
        text = f"{title} {snippet.get('description', '')}"
        source_values = sorted(sources.get(video_id, set())); popular_details = sorted(value.split(":", 1)[1] for value in source_values if value.startswith("mostPopular:")); search_details = _search_details(source_values)
        row: dict[str, Any] = {"video_id": video_id, "title": title, "channel_title": channel, "popular_source_details": popular_details, "search_source_details": search_details, "search_mode": "|".join(sorted({value["search_mode"] for value in search_details})), "query_group": "|".join(sorted({value["query_group"] for value in search_details})), "query_text": " || ".join(sorted({value["query_text"] for value in search_details})), "search_rank": min((value["search_rank"] for value in search_details), default=0), "rights_status": "PENDING", "selected": 0}
        if channel.casefold() in blocked_channels: reject(row, "blacklist_channel"); continue
        published_raw = snippet.get("publishedAt")
        if not published_raw: reject(row, "missing_published_at"); continue
        try: published_utc = datetime.fromisoformat(str(published_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError: reject(row, "invalid_published_at"); continue
        published_local = published_utc.astimezone(window.start_local.tzinfo); row["published_at_us"] = published_local.isoformat()
        popular_age = (window.end_utc - published_utc).total_seconds() / 3600
        if popular_details and (popular_age < 0 or popular_age > float(config["popular_max_age_hours"])): reject(row, "outside_popular_age_window"); continue
        if not popular_details and not search_details: reject(row, "missing_discovery_source"); continue
        if snippet.get("liveBroadcastContent", "none") != "none": reject(row, "live_or_upcoming"); continue
        if status.get("privacyStatus", "public") != "public": reject(row, "not_public"); continue
        duration = parse_iso8601_duration(str(content.get("duration", ""))); row.update({"duration_seconds": duration, "duration": format_duration(duration)})
        if not int(config["min_duration_seconds"]) <= duration <= int(config["max_duration_seconds"]): reject(row, "duration_out_of_range"); continue
        if config["exclude_shorts"] and duration <= int(config["shorts_max_duration_seconds"]) and "#shorts" in text.casefold(): reject(row, "shorts"); continue
        hard_hits = phrase_hits(text, config["hard_exclude_phrases"]); row["hard_exclude_hits"] = hard_hits
        if hard_hits: reject(row, "hard_exclude"); continue
        has_caption = content.get("caption") == "true"; language = assess_language(snippet, has_caption, config["language_markers"]); row.update({key: value for key, value in language.items() if key != "is_english"})
        if config["english_only"] and not language["is_english"]: reject(row, language["language_filter_reason"]); continue
        views, likes, comments = (int_value(statistics, key) for key in ("viewCount", "likeCount", "commentCount"))
        age_hours = max(popular_age, float(config["min_age_hours_for_rate"])); views_per_hour = views / age_hours; like_rate = likes / views if views else 0.0; comment_rate = comments / views if views else 0.0
        category_id = str(snippet.get("categoryId", "")); category_title = category_titles.get(category_id, category_id)
        primary, all_topics, relevance, keyword_hits = calculate_topic_relevance(snippet, set(buckets.get(video_id, set())), category_title, config)
        interest = calculate_interest(text, primary, config)
        if primary == "other" and popular_details and interest["interest_score"] >= float(config["wildcard_interest_min"]): primary, all_topics = "wildcard_popular", ["wildcard_popular"]
        row.update(interest); row.update({"primary_topic": primary, "topic": primary, "all_topics": all_topics, "topic_relevance_score": relevance, "topic_keyword_hits": keyword_hits})
        if primary == "other": reject(row, "low_topic_relevance"); continue
        if views < int(config["topic_min_view_count"][primary]) or views_per_hour < float(config["topic_min_views_per_hour"][primary]): reject(row, "low_absolute_heat"); continue
        risk = copyright_risk(category_title, text, str(status.get("license", "unknown")), hard_hits)
        row.update({"category_id": category_id, "category_title": category_title, "age_hours": round(age_hours, 3), "view_count": views, "like_count": likes, "comment_count": comments, "views_per_hour": round(views_per_hour, 3), "like_rate": round(like_rate, 6), "comment_rate": round(comment_rate, 6), "has_caption": has_caption, "license": status.get("license", "unknown"), "embeddable": status.get("embeddable", ""), "copyright_risk": risk, "thumbnail_url": best_thumbnail(snippet), "youtube_url": f"https://www.youtube.com/watch?v={video_id}"})
        metric_scores(row, config)
        if popular_details and not search_details and (risk == "very_high" or row["localization_suitability"] == "low"): reject(row, "popular_not_localizable"); continue
        row.update({"eligible": True, "filter_reason": ""}); output.append(row); counts["accepted"] += 1
        if raw_output is not None: raw_output.append(row)
    assign_event_groups(output, config)
    return sorted(output, key=lambda row: row["final_score"], reverse=True)


CSV_FIELDS = ["rank", "video_id", "title", "channel_title", "primary_topic", "all_topics", "category_id", "category_title", "published_at_us", "age_hours", "duration", "duration_seconds", "view_count", "like_count", "comment_count", "views_per_hour", "interest_score", "interest_hits", "boring_penalty", "boring_hits", "topic_relevance_score", "topic_keyword_hits", "growth_score", "engagement_score", "freshness_score", "localization_suitability", "localization_suitability_score", "raw_score", "final_score", "event_key", "semantic_duplicate_group", "search_mode", "query_group", "query_text", "search_rank", "search_source_details", "popular_source_details", "detected_language", "language_confidence", "language_source", "language_filter_reason", "has_caption", "copyright_risk", "license", "selection_reason", "selection_topic", "contributing_query_group", "youtube_url", "thumbnail_url", "rights_status", "selected"]


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore"); writer.writeheader()
        for row in rows:
            export = row.copy()
            for key in ("all_topics", "interest_hits", "boring_hits", "topic_keyword_hits", "search_source_details", "popular_source_details"):
                export[key] = json.dumps(export.get(key, []), ensure_ascii=False, separators=(",", ":"))
            writer.writerow(export)


def write_json(path: Path, rows: list[dict[str, Any]], config: dict[str, Any], window: DayWindow) -> None:
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), "region_code": config["region_code"], "timezone": config["timezone"], "date": window.local_date.isoformat(), "candidate_count": len(rows), "candidates": rows}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_html(path: Path, rows: list[dict[str, Any]], window: DayWindow) -> None:
    sections = []
    for topic in dict.fromkeys(row["selection_topic"] for row in rows):
        cards = []
        for row in (item for item in rows if item["selection_topic"] == topic):
            cards.append(f'<article><img src="{html.escape(row["thumbnail_url"])}" alt=""><div><b>#{row["rank"]} · {html.escape(topic)}</b><h3><a href="{html.escape(row["youtube_url"])}" target="_blank" rel="noopener">{html.escape(row["title"])}</a></h3><p>{html.escape(row["channel_title"])} · {html.escape(row["published_at_us"])}</p><p>Views {row["view_count"]:,} · VPH {row["views_per_hour"]:,.0f} · interest {row["interest_score"]:.1f} · relevance {row["topic_relevance_score"]:.1f} · final {row["final_score"]:.1f}</p><p>Interest: {html.escape(", ".join(row["interest_hits"])) or "—"}<br>Queries: {html.escape(row["query_text"]) or "mostPopular"}</p><small>language {row["detected_language"]}/{row["language_confidence"]} · captions {"yes" if row["has_caption"] else "no"} · copyright {row["copyright_risk"]} · reason {row["selection_reason"]} · rights PENDING</small></div></article>')
        sections.append(f"<section><h2>{html.escape(topic)}</h2>{''.join(cards)}</section>")
    document = f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Localization candidates {window.local_date}</title><style>body{{font-family:Arial,sans-serif;background:#f4f5f7;margin:0}}main{{max-width:1100px;margin:auto;padding:24px}}article{{display:grid;grid-template-columns:260px 1fr;gap:16px;background:white;padding:14px;margin:12px 0;border-radius:10px}}img{{width:100%;aspect-ratio:16/9;object-fit:cover}}@media(max-width:700px){{article{{grid-template-columns:1fr}}}}</style></head><body><main><h1>US localization candidates — {window.local_date}</h1><p>Manual review required. Nothing is approved, downloaded, or published.</p>{"".join(sections)}</main></body></html>'
    path.write_text(document, encoding="utf-8")


def run(args: argparse.Namespace, project_root: Path) -> int:
    setup_logging(project_root / "logs"); load_dotenv(project_root / ".env")
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not api_key: LOGGER.error("YOUTUBE_API_KEY is missing or empty in .env"); return 2
    config_path = Path(args.config); config = load_config(config_path if config_path.is_absolute() else project_root / config_path)
    limit = args.limit if args.limit is not None else int(config["candidate_limit"])
    if not 1 <= limit <= int(config["candidate_limit"]): raise ValueError("--limit must be between 1 and candidate_limit")
    window = build_day_window(args.date, config["timezone"]); LOGGER.info("Target date: %s; search endpoint time: %s", window.local_date, window.end_utc.isoformat())
    client = YouTubeClient(api_key, int(config["request_timeout_seconds"]), int(config["max_retries"]))
    output_dir = project_root / "candidates"; output_dir.mkdir(parents=True, exist_ok=True); prefix = f"{window.local_date}_{config['region_code']}"
    checkpoint_path = output_dir / f"{prefix}_search_checkpoint.json"
    title_to_id, id_to_title = get_category_map(client, config["region_code"])
    popular, popular_sources, popular_topics = collect_popular(client, config, title_to_id)
    search_plan_stats: dict[str, int] = {}
    quota_exhausted = False
    try:
        search_ids, search_sources, search_topics = collect_search_ids(client, config, window, checkpoint_path, search_plan_stats)
    except SearchQuotaExceeded as exc:
        quota_exhausted = True
        search_ids, search_sources, search_topics = exc.partial_ids, defaultdict(set, exc.partial_sources), defaultdict(set, exc.partial_topics)
        LOGGER.error("%s", exc)
        LOGGER.warning("Continuing with %d partially discovered search videos and saving candidate outputs", len(search_ids))
    LOGGER.info("Search requests: planned=%d executed=%d resumed=%d remaining_safe_budget=%d", search_plan_stats.get("planned_requests", 0), search_plan_stats.get("executed_requests", 0), search_plan_stats.get("resumed_requests", 0), search_plan_stats.get("remaining_safe_budget", 0))
    resources = {**popular, **get_video_details(client, search_ids)}
    sources, topics = defaultdict(set), defaultdict(set)
    for mapping, target in ((popular_sources, sources), (search_sources, sources), (popular_topics, topics), (search_topics, topics)):
        for video_id, values in mapping.items(): target[video_id].update(values)
    LOGGER.info("Discovery: popular=%d search=%d overlap=%d merged=%d", len(popular), len(search_ids), len(set(popular) & search_ids), len(resources))
    pre_topic_counts: Counter[str] = Counter()
    for values in topics.values():
        for topic in values: pre_topic_counts[topic] += 1
    filter_counts: Counter[str] = Counter(); raw_pool: list[dict[str, Any]] = []
    eligible = build_candidates(resources, sources, topics, id_to_title, config, window, filter_counts, raw_pool)
    selection_counts: Counter[str] = Counter(); selected = select_candidates(eligible, config, limit, selection_counts)
    csv_path = output_dir / f"{prefix}_localization_top50.csv"; json_path = output_dir / f"{prefix}_localization_top50.json"; html_path = output_dir / f"{prefix}_localization_top50.html"; raw_path = output_dir / f"{prefix}_raw_pool.json"; metrics_path = output_dir / f"{prefix}_metrics.json"
    write_csv(csv_path, selected); write_json(json_path, selected, config, window); write_html(html_path, selected, window); raw_path.write_text(json.dumps(raw_pool, ensure_ascii=False, indent=2), encoding="utf-8")
    topic_counts = Counter(row["selection_topic"] for row in selected)
    post_topic_counts = Counter(row["primary_topic"] for row in eligible)
    metrics = {"generated_at": datetime.now(timezone.utc).isoformat(), "quota_exhausted": quota_exhausted, "search_plan": search_plan_stats, "checkpoint": str(checkpoint_path), "discovery": {"popular": len(popular), "search": len(search_ids), "overlap": len(set(popular) & search_ids), "merged": len(resources)}, "filters": dict(filter_counts), "selection": dict(selection_counts), "topic_funnel": {topic: {"before_filter": pre_topic_counts[topic], "after_filter": post_topic_counts[topic], "selected": topic_counts[topic], "target": int(config["topic_quotas"][topic]), "maximum": int(config["topic_max_counts"][topic])} for topic in config["topic_quotas"]}, "topics": dict(topic_counts), "eligible": len(eligible), "selected": len(selected)}
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    for topic, quota in config["topic_quotas"].items():
        LOGGER.info("Topic %-24s before=%d after=%d selected=%d target=%d max=%d", topic, pre_topic_counts[topic], post_topic_counts[topic], topic_counts[topic], quota, int(config["topic_max_counts"][topic]))
    LOGGER.info("Skip counts: duration=%d heat=%d query_limit=%d topic_limit=%d channel_limit=%d", filter_counts["duration_out_of_range"], filter_counts["low_absolute_heat"], selection_counts["query_limit"], selection_counts["topic_limit"], selection_counts["channel_limit"])
    LOGGER.info("Filters: %s", dict(filter_counts)); LOGGER.info("Selection limits: %s", dict(selection_counts)); LOGGER.info("Eligible=%d selected=%d", len(eligible), len(selected))
    if len(selected) < limit: LOGGER.warning("Only %d of %d requested candidates satisfied hard diversity and quality limits", len(selected), limit)
    search_share = selection_counts["selected_with_search"] / len(selected) if selected else 0.0
    if search_share < float(config["min_search_share"]): LOGGER.warning("Search contribution %.1f%% is below configured %.1f%%", search_share * 100, float(config["min_search_share"]) * 100)
    return 0


def main() -> int:
    try: return run(parse_args(), Path(__file__).resolve().parents[1])
    except (FileNotFoundError, ValueError, json.JSONDecodeError, YouTubeAPIError) as exc: LOGGER.error("%s", exc); return 1


if __name__ == "__main__":
    raise SystemExit(main())
