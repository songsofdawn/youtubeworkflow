from __future__ import annotations

from collections import Counter
from typing import Any


def select_candidates(candidates: list[dict[str, Any]], config: dict[str, Any], limit: int, stats: Counter[str] | None = None) -> list[dict[str, Any]]:
    selection_stats = stats if stats is not None else Counter()
    selected: list[dict[str, Any]] = []
    ids: set[str] = set()
    channels: Counter[str] = Counter()
    events: Counter[str] = Counter()
    query_groups: Counter[str] = Counter()
    popular_only = 0
    max_channel = int(config["max_per_channel"])
    max_event = int(config["max_per_event"])
    max_query = int(config["max_per_query_group"])
    popular_max = int(config["popular_max"])
    wildcard_quota = min(int(config["topic_quotas"].get("wildcard_popular", 0)), int(config["popular_target"]))

    def available_query_group(row: dict[str, Any]) -> str:
        groups = list(dict.fromkeys(detail["query_group"] for detail in row["search_source_details"]))
        return next((group for group in groups if query_groups[group] < max_query), "")

    def try_add(row: dict[str, Any], reason: str, selection_topic: str | None = None) -> bool:
        nonlocal popular_only
        if row["video_id"] in ids:
            return False
        target_topic = selection_topic or row["primary_topic"]
        if target_topic == "wildcard_popular" and sum(item["selection_topic"] == "wildcard_popular" for item in selected) >= wildcard_quota:
            selection_stats["wildcard_limit"] += 1; return False
        channel = row["channel_title"].casefold()
        event = row["semantic_duplicate_group"]
        if channels[channel] >= max_channel:
            selection_stats["channel_limit"] += 1; return False
        if events[event] >= max_event:
            selection_stats["event_limit"] += 1; return False
        query_group = available_query_group(row) if row["search_source_details"] else ""
        if row["search_source_details"] and not query_group:
            selection_stats["query_limit"] += 1; return False
        is_popular_only = bool(row["popular_source_details"]) and not row["search_source_details"]
        if is_popular_only and popular_only >= popular_max:
            selection_stats["popular_limit"] += 1; return False
        copy = row.copy()
        copy["selection_reason"] = reason
        copy["selection_topic"] = target_topic
        copy["contributing_query_group"] = query_group
        selected.append(copy)
        ids.add(row["video_id"])
        channels[channel] += 1
        events[event] += 1
        if query_group:
            query_groups[query_group] += 1
        if is_popular_only:
            popular_only += 1
        return True

    ordered = sorted(candidates, key=lambda row: row["final_score"], reverse=True)
    regular_target = max(0, limit - wildcard_quota)

    for topic, quota in config["topic_quotas"].items():
        if topic == "wildcard_popular":
            continue
        topic_rows = [row for row in ordered if row["primary_topic"] == topic]
        topic_rows.sort(key=lambda row: (bool(row["search_source_details"]), row["final_score"]), reverse=True)
        added = 0
        for row in topic_rows:
            if len(selected) >= regular_target or added >= int(quota):
                break
            if try_add(row, "topic_quota", topic):
                added += 1

    for row in (item for item in ordered if item["search_source_details"] and item["primary_topic"] != "wildcard_popular"):
        if len(selected) >= regular_target:
            break
        reason = "multi_query_match" if len(row["search_source_details"]) > 1 else "high_interest" if row["interest_score"] >= 12 else "high_growth" if row["growth_score"] >= 60 else "global_backfill"
        try_add(row, reason)

    wildcard_rows = [
        row for row in ordered
        if row["popular_source_details"]
        and row["copyright_risk"] != "very_high"
        and row["localization_suitability"] in {"medium", "high"}
        and row["interest_score"] >= float(config["wildcard_interest_min"])
    ]
    wildcard_added = 0
    for row in wildcard_rows:
        if len(selected) >= limit or wildcard_added >= wildcard_quota:
            break
        if try_add(row, "wildcard_popular", "wildcard_popular"):
            wildcard_added += 1

    for row in ordered:
        if len(selected) >= limit:
            break
        try_add(row, "global_backfill")

    selected.sort(key=lambda row: row["final_score"], reverse=True)
    for rank, row in enumerate(selected, 1):
        row["rank"] = rank
    selection_stats["selected"] = len(selected)
    selection_stats["selected_with_search"] = sum(bool(row["search_source_details"]) for row in selected)
    selection_stats["selected_popular_only"] = sum(bool(row["popular_source_details"]) and not row["search_source_details"] for row in selected)
    return selected
