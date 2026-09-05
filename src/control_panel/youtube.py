from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from src.discovery import DiscoveryPipeline
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
DISCOVERY_PACKS: tuple[dict[str, Any], ...] = (
    {
        "id": "ai_technology",
        "label": "AI 与新科技",
        "description": "新模型、AI 工具、机器人和新硬件实测",
        "query": "new AI model test|AI tool comparison|robotics experiment|new technology test",
        "keywords": ["ai", "model", "chatgpt", "claude", "gemini", "robot", "technology"],
    },
    {
        "id": "software_programming",
        "label": "软件与编程",
        "description": "编程项目、自动化、开发工具和软件工作流",
        "query": "programming project|Python automation|coding challenge|new software workflow",
        "keywords": ["programming", "python", "coding", "software", "developer", "automation"],
    },
    {
        "id": "science",
        "label": "科学",
        "description": "科学发现、工程原理和可视化科普",
        "query": "science experiment|new scientific discovery|engineering explained|physics experiment",
        "keywords": ["science", "scientific", "physics", "engineering", "discovery", "experiment"],
    },
    {
        "id": "gaming",
        "label": "游戏",
        "description": "新游戏、更新、玩法实验和高质量挑战",
        "query": "new game gameplay|game update|gaming challenge|game experiment",
        "keywords": ["game", "gameplay", "gaming", "update", "challenge"],
    },
    {
        "id": "minecraft",
        "label": "Minecraft",
        "description": "Minecraft 生存、But、模组、建造与实验",
        "query": "Minecraft but|Minecraft challenge|Minecraft experiment|Minecraft survival",
        "keywords": ["minecraft", "hardcore", "survival", "mod", "build", "but"],
    },
    {
        "id": "chemistry",
        "label": "化学",
        "description": "化学实验、材料反应和实验室演示",
        "query": "chemistry experiment|chemical reaction|laboratory experiment|materials science test",
        "keywords": ["chemistry", "chemical", "reaction", "laboratory", "material", "molecule"],
    },
    {
        "id": "challenges_experiments",
        "label": "挑战与实验",
        "description": "30/100 天挑战、测试和意外结果",
        "query": "I tried for 30 days|100 days challenge|I tested|what happens if",
        "keywords": ["challenge", "experiment", "i tried", "i tested", "100 days", "30 days"],
    },
    {
        "id": "entertainment",
        "label": "娱乐",
        "description": "高参与度娱乐内容、喜剧和有解说反应",
        "query": "funny challenge|comedy experiment|entertainment reaction|unexpected moments",
        "keywords": ["funny", "comedy", "entertainment", "reaction", "unexpected"],
    },
    {
        "id": "agriculture_gardening",
        "label": "农业与园艺",
        "description": "种植、收获、农场技术和园艺实验",
        "query": "garden harvest|growing experiment|farm technology|vegetable garden update",
        "keywords": ["garden", "growing", "harvest", "farm", "plant", "vegetable"],
    },
    {
        "id": "food_cooking",
        "label": "美食与烹饪",
        "description": "烹饪实验、食谱测试和特色美食",
        "query": "food experiment|recipe test|cooking challenge|street food discovery",
        "keywords": ["food", "recipe", "cooking", "chef", "kitchen", "street food"],
    },
    {
        "id": "outdoor_travel",
        "label": "户外与旅行",
        "description": "露营、生存挑战、远途旅行和地点探索",
        "query": "outdoor adventure|survival challenge|camping experiment|remote travel discovery",
        "keywords": ["outdoor", "survival", "camping", "travel", "adventure", "remote"],
    },
    {
        "id": "tutorials_skills",
        "label": "教程与技能",
        "description": "完整教程、技能学习和实用工作流",
        "query": "complete tutorial|beginner guide|how to build|new skill challenge",
        "keywords": ["tutorial", "guide", "how to", "beginner", "skill", "build"],
    },
    {
        "id": "art_creativity",
        "label": "艺术与创意",
        "description": "绘画、动画、3D 制作和创意挑战",
        "query": "art challenge|animation process|creative project|3D printing project",
        "keywords": ["art", "animation", "creative", "drawing", "3d", "design"],
    },
    {
        "id": "social_experiments",
        "label": "娱乐与社会实验",
        "description": "社会实验、真人挑战和有叙事的互动内容",
        "query": "social experiment|public challenge|I asked strangers|human behavior experiment",
        "keywords": ["social experiment", "public", "strangers", "human behavior", "challenge"],
    },
)
def load_discovery_packs(path: Path | None = None) -> tuple[dict[str, Any], ...]:
    """Load and validate user-editable discovery packs, with built-ins as fallback."""
    if path is None or not path.is_file():
        return DISCOVERY_PACKS
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"智能发现关键词文件无法读取：{path}（{exc}）") from exc
    raw_packs = payload.get("packs") if isinstance(payload, dict) else None
    if not isinstance(raw_packs, list):
        raise ValueError(f"智能发现关键词文件格式错误：{path} 中缺少 packs 列表")

    packs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_pack in enumerate(raw_packs, 1):
        if not isinstance(raw_pack, dict):
            raise ValueError(f"智能发现关键词文件第 {index} 个领域必须是对象")
        if not bool(raw_pack.get("enabled", True)):
            continue
        pack_id = str(raw_pack.get("id") or "").strip()
        label = str(raw_pack.get("label") or "").strip()
        description = str(raw_pack.get("description") or "").strip()
        query = " ".join(str(raw_pack.get("query") or "").split())
        keywords_value = raw_pack.get("keywords")
        keywords = (
            [" ".join(str(value).casefold().split()) for value in keywords_value]
            if isinstance(keywords_value, list)
            else []
        )
        keywords = [value for value in keywords if value]
        if not DISCOVERY_PACK_ID_PATTERN.fullmatch(pack_id):
            raise ValueError(
                f"智能发现关键词文件第 {index} 个领域 id 无效；只能使用小写字母、数字和下划线"
            )
        if pack_id in seen_ids:
            raise ValueError(f"智能发现关键词文件存在重复领域 id：{pack_id}")
        if not label or not description or not query or not keywords:
            raise ValueError(
                f"智能发现领域 {pack_id} 必须填写 label、description、query 和 keywords"
            )
        seen_ids.add(pack_id)
        packs.append(
            {
                "id": pack_id,
                "label": label,
                "description": description,
                "query": query,
                "keywords": keywords,
                "default_selected": bool(raw_pack.get("default_selected", True)),
            }
        )
    if not packs:
        raise ValueError("智能发现关键词文件没有任何已启用领域")
    return tuple(packs)



def save_discovery_packs(
    path: Path,
    raw_packs: list[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    # Validate and atomically persist user-editable discovery packs.
    if not isinstance(raw_packs, list) or not raw_packs:
        raise ValueError("智能发现领域列表不能为空")
    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, raw_pack in enumerate(raw_packs, 1):
        if not isinstance(raw_pack, dict):
            raise ValueError(f"第 {index} 个领域格式无效")
        pack_id = str(raw_pack.get("id") or "").strip().casefold()
        label = str(raw_pack.get("label") or "").strip()
        description = str(raw_pack.get("description") or "").strip()
        query = str(raw_pack.get("query") or "").strip()
        keywords_raw = raw_pack.get("keywords")
        if isinstance(keywords_raw, str):
            keywords = [
                item.strip().casefold()
                for item in re.split(r"[,，\n|]+", keywords_raw)
                if item.strip()
            ]
        elif isinstance(keywords_raw, list):
            keywords = [
                str(item).strip().casefold()
                for item in keywords_raw
                if str(item).strip()
            ]
        else:
            keywords = []
        query_parts = [item.strip() for item in query.split("|") if item.strip()]
        query = "|".join(dict.fromkeys(query_parts))
        keywords = list(dict.fromkeys(keywords))
        if not DISCOVERY_PACK_ID_PATTERN.fullmatch(pack_id):
            raise ValueError(
                f"领域 id '{pack_id}' 无效：只允许 2-64 位小写字母、数字和下划线"
            )
        if pack_id in seen_ids:
            raise ValueError(f"存在重复领域 id：{pack_id}")
        if not label or not description or not query or not keywords:
            raise ValueError(
                f"领域 {pack_id or index} 必须填写名称、说明、至少一个搜索词和关键词"
            )
        seen_ids.add(pack_id)
        normalized.append(
            {
                "id": pack_id,
                "label": label,
                "description": description,
                "enabled": True,
                "default_selected": bool(raw_pack.get("default_selected", True)),
                "query": query,
                "keywords": keywords,
            }
        )
    payload = {
        "schema_version": 1,
        "description": "智能发现可编辑领域关键词包。可在控制面板中新增、删除和修改。",
        "packs": normalized,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        validated = load_discovery_packs(temp_path)
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return validated


def public_discovery_catalog(
    packs: tuple[dict[str, Any], ...] | None = None,
    *,
    include_details: bool = False,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for pack in (packs or DISCOVERY_PACKS):
        item = {
            "id": pack["id"],
            "label": pack["label"],
            "description": pack["description"],
            "examples": str(pack["query"]).split("|")[:3],
            "default_selected": bool(pack.get("default_selected", True)),
        }
        if include_details:
            item.update(
                {
                    "query": str(pack["query"]),
                    "keywords": [str(value) for value in pack["keywords"]],
                    "enabled": True,
                }
            )
        output.append(item)
    return output


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
            self.project_root / "config" / "discovery_keywords.json"
        )
        self.discovery_pipeline = DiscoveryPipeline(self.project_root)

    def discovery_packs(self) -> tuple[dict[str, Any], ...]:
        return load_discovery_packs(self.discovery_config_path)

    def discovery_catalog(
        self,
        *,
        include_details: bool = False,
    ) -> list[dict[str, Any]]:
        return public_discovery_catalog(
            self.discovery_packs(),
            include_details=include_details,
        )

    def save_discovery_catalog(
        self,
        raw_packs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        packs = save_discovery_packs(self.discovery_config_path, raw_packs)
        return public_discovery_catalog(packs, include_details=True)

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
        ranking_mode: str = "hot",
        client: YouTubeClient | None = None,
        now: datetime | None = None,
        progress: Any | None = None,
        cancelled: Any | None = None,
    ) -> dict[str, Any]:
        selected_ids = list(dict.fromkeys(str(value) for value in pack_ids))
        if not selected_ids:
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
