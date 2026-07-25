from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.fetch_daily_candidates import (
    YouTubeAPIError,
    YouTubeClient,
    best_thumbnail,
    format_duration,
    get_video_details,
    parse_iso8601_duration,
)


VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
ALLOWED_SEARCH_ORDERS = {"relevance", "date", "viewCount"}


def load_env_values(path: Path) -> dict[str, str]:
    """Load the simple KEY=VALUE subset used by this project without exposing it."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def extract_video_id(value: str) -> str | None:
    text = value.strip()
    if VIDEO_ID_PATTERN.fullmatch(text):
        return text
    try:
        parsed = urlparse(text if "://" in text else f"https://{text}")
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold()
    candidate = ""
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path.rstrip("/") == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2 and parts[0].casefold() in {"shorts", "embed", "live"}:
                candidate = parts[1]
    return candidate if VIDEO_ID_PATTERN.fullmatch(candidate) else None


def normalize_video_inputs(value: str) -> list[dict[str, str]]:
    """Turn IDs and YouTube URLs separated by whitespace, commas, or newlines into URLs."""
    tokens = re.split(r"[\s,，;；]+", value.strip())
    output: list[dict[str, str]] = []
    seen: set[str] = set()
    for token in tokens:
        if not token:
            continue
        video_id = extract_video_id(token)
        if not video_id:
            raise ValueError(f"无法识别 YouTube 视频 ID 或链接：{token}")
        if video_id in seen:
            continue
        seen.add(video_id)
        output.append(
            {
                "video_id": video_id,
                "url": f"https://www.youtube.com/watch?v={video_id}",
            }
        )
    if not output:
        raise ValueError("请至少输入一个 YouTube 视频 ID 或链接")
    return output


class TargetedYouTubeSearch:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.config_path = self.project_root / "config" / "trending_config.json"

    def _settings(self) -> tuple[dict[str, Any], str]:
        config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        env_values = load_env_values(self.project_root / ".env")
        api_key = os.getenv("YOUTUBE_API_KEY", "").strip() or env_values.get(
            "YOUTUBE_API_KEY", ""
        ).strip()
        if not api_key:
            raise ValueError("YOUTUBE_API_KEY 尚未配置，请先填写项目根目录下的 .env")
        return config, api_key

    def search(
        self,
        query: str,
        limit: int,
        order: str = "relevance",
        *,
        client: YouTubeClient | None = None,
    ) -> list[dict[str, Any]]:
        query = " ".join(query.split())
        if not query:
            raise ValueError("搜索关键词不能为空")
        if len(query) > 120:
            raise ValueError("搜索关键词不能超过 120 个字符")
        if not 1 <= int(limit) <= 50:
            raise ValueError("搜索数量必须在 1 到 50 之间")
        if order not in ALLOWED_SEARCH_ORDERS:
            raise ValueError("不支持的排序方式")

        config, api_key = self._settings()
        youtube = client or YouTubeClient(
            api_key,
            int(config.get("request_timeout_seconds", 30)),
            int(config.get("max_retries", 4)),
        )
        payload = youtube.get(
            "search",
            {
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": int(limit),
                "order": order,
                "regionCode": str(config.get("region_code", "US")),
                "relevanceLanguage": str(config.get("language", "en")),
                "safeSearch": str(config.get("safe_search", "moderate")),
                "videoEmbeddable": "true",
            },
        )
        ordered_ids = [
            str(item.get("id", {}).get("videoId", ""))
            for item in payload.get("items", [])
            if str(item.get("id", {}).get("videoId", ""))
        ]
        resources = get_video_details(youtube, ordered_ids)
        rows: list[dict[str, Any]] = []
        for rank, video_id in enumerate(ordered_ids, 1):
            item = resources.get(video_id)
            if not item:
                continue
            snippet = item.get("snippet", {})
            content = item.get("contentDetails", {})
            statistics = item.get("statistics", {})
            status = item.get("status", {})
            duration_seconds = parse_iso8601_duration(str(content.get("duration", "")))
            rows.append(
                {
                    "rank": rank,
                    "video_id": video_id,
                    "title": str(snippet.get("title") or video_id),
                    "channel_title": str(snippet.get("channelTitle") or ""),
                    "published_at": str(snippet.get("publishedAt") or ""),
                    "duration": format_duration(duration_seconds),
                    "duration_seconds": duration_seconds,
                    "view_count": int(statistics.get("viewCount") or 0),
                    "like_count": int(statistics.get("likeCount") or 0),
                    "has_caption": str(content.get("caption", "")).casefold() == "true",
                    "license": str(status.get("license") or "unknown"),
                    "embeddable": bool(status.get("embeddable", False)),
                    "thumbnail_url": best_thumbnail(snippet),
                    "youtube_url": f"https://www.youtube.com/watch?v={video_id}",
                    "rights_status": "PENDING",
                }
            )
        return rows


__all__ = [
    "TargetedYouTubeSearch",
    "YouTubeAPIError",
    "extract_video_id",
    "load_env_values",
    "normalize_video_inputs",
]
