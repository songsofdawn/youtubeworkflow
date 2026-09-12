"""Bounded, editable query pools and public recall diagnostics."""
from __future__ import annotations

from typing import Any


def normalize_queries(value: object) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    for part in str(value or "").split("|"):
        query = " ".join(part.split())
        if not query or query.casefold() in seen:
            continue
        if len(query) > 120:
            raise ValueError("每个发现搜索词最多 120 个字符")
        seen.add(query.casefold())
        queries.append(query)
    if len(queries) > 21:
        raise ValueError("每个领域最多 1 个主搜索词和 20 个轮换搜索词")
    return queries


def query_diagnostics(
    searches: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    previously_seen: set[str],
) -> list[dict[str, Any]]:
    """Credit each query with all its unique hits, independent of call order.

    A video may be credited to several queries; counts must not be added across
    rows to compute the overall unique pool. Empty successful calls are retained.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    eligible = {str(row["video_id"]): row for row in rows}
    selected_ids = {str(row["video_id"]) for row in selected}
    for search in searches:
        key = (search["pack_id"], search["query"])
        group = groups.setdefault(key, {"ids": set(), "orders": [], "calls": 0})
        group["ids"].update(search["video_ids"])
        group["calls"] += 1
        if search["order"] not in group["orders"]:
            group["orders"].append(search["order"])
    output = []
    for (pack_id, query), group in groups.items():
        ids = group["ids"]
        qualified = ids & eligible.keys()
        new_ids = ids - previously_seen
        output.append({
            "pack_id": pack_id,
            "query": query,
            "orders": group["orders"],
            "calls": group["calls"],
            "unique_count": len(ids),
            "new_count": len(new_ids),
            "eligible_count": len(qualified),
            "new_eligible_count": len(qualified & new_ids),
            "selected_count": len(ids & selected_ids),
            "channel_count": len({
                str(eligible[video_id].get("channel_id")
                    or eligible[video_id].get("channel_title") or video_id)
                for video_id in qualified
            }),
        })
    return output
