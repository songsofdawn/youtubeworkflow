from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.discovery_next.service import DiscoveryNextService
from src.discovery_next.taxonomy import Taxonomy
from src.fetch_daily_candidates import (
    YouTubeAPIError,
    YouTubeClient,
    best_thumbnail,
    format_duration,
    get_video_details,
    parse_iso8601_duration,
)


VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")
DISCOVERY_PACK_ID_PATTERN = re.compile(r"^[a-z0-9_]{2,64}$")
ALLOWED_SEARCH_ORDERS = {"relevance", "date", "viewCount"}
DISCOVERY_WINDOWS = {24, 72, 168, 336, 720}
def __getattr__(name):
    if name in {"DISCOVERY_PACKS", "load_discovery_packs", "save_discovery_packs", "public_discovery_catalog"}:
        from src.discovery import legacy_catalog
        return getattr(legacy_catalog, name)
    raise AttributeError(name)


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
        self.discovery_config_path = (
            self.project_root / "config" / "discovery_taxonomy.json"
        )
        self.discovery_pipeline = DiscoveryNextService(self.project_root)

    def discovery_packs(self) -> tuple[dict[str, Any], ...]:
        return tuple(d for d in Taxonomy(self.project_root).load() if d["enabled"])

    def discovery_catalog(
        self,
        *,
        include_details: bool = False,
    ) -> list[dict[str, Any]]:
        return list(self.discovery_packs())

    def save_discovery_catalog(
        self,
        raw_packs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return Taxonomy(self.project_root).save(raw_packs)

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

    def discovery_settings(self) -> dict[str, Any]:
        config = (
            json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            if self.config_path.is_file()
            else {}
        )
        return self.discovery_pipeline.public_settings(config)

    def discovery_health(self) -> dict[str, Any]:
        config = (
            json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            if self.config_path.is_file()
            else {}
        )
        return self.discovery_pipeline.health(config)

    def record_discovery_feedback(
        self,
        item: dict[str, Any],
        feedback: str,
    ) -> dict[str, Any]:
        return self.discovery_pipeline.record_feedback(item, feedback)

    def discover(
        self,
        pack_ids: list[str],
        hours: int,
        per_pack: int,
        *,
        known_video_ids: set[str] | None = None,
        known_titles: list[str] | None = None,
        minimum_duration_seconds: int | None = None,
        maximum_duration_seconds: int | None = None,
        ranking_mode: str = "potential",
        discovery_scope: str = "auto",
        search_strength: str = "standard",
        client: YouTubeClient | None = None,
        now: datetime | None = None,
        progress: Any | None = None,
        cancelled: Any | None = None,
    ) -> dict[str, Any]:
        selected_ids = list(dict.fromkeys(str(value) for value in pack_ids))
        if discovery_scope == "manual" and not selected_ids:
            raise ValueError("请至少选择一个发现领域")
        discovery_packs = self.discovery_packs()
        discovery_pack_by_id = {pack["id"]: pack for pack in discovery_packs}
        unknown = [value for value in selected_ids if value not in discovery_pack_by_id]
        if unknown:
            raise ValueError("包含未知的发现领域：" + "、".join(unknown))
        if int(hours) not in DISCOVERY_WINDOWS:
            raise ValueError("发现时间范围只支持 24、72、168、336 或 720 小时")
        if not 1 <= int(per_pack) <= 100:
            raise ValueError("每个领域的结果数量必须在 1 到 100 之间")

        config, api_key = self._settings()
        configured_maximum_duration_seconds = int(
            config.get("discovery_max_duration_seconds", 10800)
        )
        requested_maximum_duration_seconds = int(
            maximum_duration_seconds
            if maximum_duration_seconds is not None
            else configured_maximum_duration_seconds
        )
        if not 60 <= requested_maximum_duration_seconds <= configured_maximum_duration_seconds:
            raise ValueError(
                "智能发现候选最大时长必须在 1 到 "
                f"{configured_maximum_duration_seconds // 60} 分钟之间"
            )
        if minimum_duration_seconds is not None:
            if not 60 <= int(minimum_duration_seconds) <= requested_maximum_duration_seconds:
                raise ValueError(
                    "智能发现候选最小时长必须在 1 到 "
                    f"{requested_maximum_duration_seconds // 60} 分钟之间"
                )
        youtube = client or YouTubeClient(
            api_key,
            int(config.get("request_timeout_seconds", 30)),
            int(config.get("max_retries", 4)),
        )
        return self.discovery_pipeline.run(
            youtube=youtube,
            packs=list(discovery_packs),
            selected_ids=selected_ids,
            hours=int(hours),
            per_pack=int(per_pack),
            config=config,
            known_video_ids=known_video_ids,
            known_titles=known_titles,
            minimum_duration_seconds=minimum_duration_seconds,
            maximum_duration_seconds=requested_maximum_duration_seconds,
            ranking_mode=ranking_mode,
            discovery_scope=discovery_scope,
            search_strength=search_strength,
            now=now,
            progress=progress,
            cancelled=cancelled,
        )


__all__ = [
    "DISCOVERY_PACKS",
    "TargetedYouTubeSearch",
    "YouTubeAPIError",
    "extract_video_id",
    "load_discovery_packs",
    "load_env_values",
    "normalize_video_inputs",
    "public_discovery_catalog",
]
