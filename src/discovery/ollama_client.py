from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests


PROMPT_VERSION = "discovery_editor_v2"
VISUAL_PROMPT_VERSION = "discovery_visual_v1"
QUERY_PROMPT_VERSION = "discovery_query_planner_v2"


class OllamaDiscoveryError(RuntimeError):
    pass


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().casefold() in {"1", "true", "yes", "on", "enabled"}


@dataclass(frozen=True)
class OllamaSettings:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3.5:9b"
    embedding_model: str = "qwen3-embedding:0.6b"
    embedding_enabled: bool = True
    query_planning_enabled: bool = True
    visual_enabled: bool = True
    metadata_batch_size: int = 10
    visual_batch_size: int = 4
    visual_top_n: int = 24
    timeout_seconds: int = 180
    thinking: bool = False

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "OllamaSettings":
        raw = config.get("discovery_llm")
        values = raw if isinstance(raw, dict) else {}
        return cls(
            enabled=_as_bool(values.get("enabled"), False),
            base_url=str(values.get("base_url") or cls.base_url).rstrip("/"),
            model=str(values.get("model") or cls.model).strip(),
            embedding_model=str(values.get("embedding_model") or cls.embedding_model).strip(),
            embedding_enabled=_as_bool(values.get("embedding_enabled"), True),
            query_planning_enabled=_as_bool(values.get("query_planning_enabled"), True),
            visual_enabled=_as_bool(values.get("visual_enabled"), True),
            metadata_batch_size=max(1, min(int(values.get("metadata_batch_size") or 10), 30)),
            visual_batch_size=max(1, min(int(values.get("visual_batch_size") or 4), 8)),
            visual_top_n=max(0, min(int(values.get("visual_top_n") or 24), 100)),
            timeout_seconds=max(10, min(int(values.get("timeout_seconds") or 180), 900)),
            thinking=_as_bool(values.get("thinking"), False),
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "model": self.model,
            "embedding_model": self.embedding_model,
            "embedding_enabled": self.embedding_enabled,
            "query_planning_enabled": self.query_planning_enabled,
            "visual_enabled": self.visual_enabled,
            "metadata_batch_size": self.metadata_batch_size,
            "visual_batch_size": self.visual_batch_size,
            "visual_top_n": self.visual_top_n,
            "timeout_seconds": self.timeout_seconds,
            "thinking": self.thinking,
        }


def _score_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "evaluations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            }
        },
        "required": ["evaluations"],
    }


METADATA_SCHEMA = _score_schema(
    {
        "video_id": {"type": "string"},
        "topic_fit": {"type": "number", "minimum": 0, "maximum": 100},
        "interestingness": {"type": "number", "minimum": 0, "maximum": 100},
        "novelty": {"type": "number", "minimum": 0, "maximum": 100},
        "story_payoff": {"type": "number", "minimum": 0, "maximum": 100},
        "visual_potential": {"type": "number", "minimum": 0, "maximum": 100},
        "localization_value": {"type": "number", "minimum": 0, "maximum": 100},
        "clickbait_risk": {"type": "number", "minimum": 0, "maximum": 100},
        "language_confidence": {"type": "number", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string", "enum": ["keep", "maybe", "reject"]},
        "reason_zh": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
    },
    [
        "video_id",
        "topic_fit",
        "interestingness",
        "novelty",
        "story_payoff",
        "visual_potential",
        "localization_value",
        "clickbait_risk",
        "language_confidence",
        "verdict",
        "reason_zh",
        "confidence",
    ],
)

VISUAL_SCHEMA = _score_schema(
    {
        "video_id": {"type": "string"},
        "visual_potential": {"type": "number", "minimum": 0, "maximum": 100},
        "title_thumbnail_consistency": {"type": "number", "minimum": 0, "maximum": 100},
        "thumbnail_spam_risk": {"type": "number", "minimum": 0, "maximum": 100},
        "reason_zh": {"type": "string"},
    },
    [
        "video_id",
        "visual_potential",
        "title_thumbnail_consistency",
        "thumbnail_spam_risk",
        "reason_zh",
    ],
)

QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pack_id": {"type": "string"},
                    "query": {"type": "string"},
                    "angle": {"type": "string"},
                },
                "required": ["pack_id", "query", "angle"],
            },
        }
    },
    "required": ["queries"],
}


class OllamaDiscoveryClient:
    def __init__(self, settings: OllamaSettings) -> None:
        self.settings = settings
        parsed = urlparse(settings.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            raise ValueError("Ollama Base URL 必须是有效的 http(s) 地址")
        self._health_lock = threading.Lock()
        self._health_cache: tuple[float, dict[str, Any]] | None = None

    def health(self, *, cache_seconds: int = 30) -> dict[str, Any]:
        if not self.settings.enabled:
            return {**self.settings.public_dict(), "reachable": False, "model_ready": False}
        with self._health_lock:
            if self._health_cache and time.monotonic() - self._health_cache[0] < cache_seconds:
                return dict(self._health_cache[1])
        payload: dict[str, Any]
        try:
            response = requests.get(f"{self.settings.base_url}/api/tags", timeout=(1.0, 2.0))
            response.raise_for_status()
            body = response.json()
            names = {
                str(item.get("name") or item.get("model") or "")
                for item in body.get("models", [])
                if isinstance(item, dict)
            }
            payload = {
                **self.settings.public_dict(),
                "reachable": True,
                "model_ready": self.settings.model in names,
                "embedding_ready": (
                    not self.settings.embedding_enabled
                    or self.settings.embedding_model in names
                ),
            }
        except (requests.RequestException, ValueError, json.JSONDecodeError):
            payload = {
                **self.settings.public_dict(),
                "reachable": False,
                "model_ready": False,
                "embedding_ready": False,
            }
        with self._health_lock:
            self._health_cache = (time.monotonic(), dict(payload))
        return payload

    def _chat(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = requests.post(
                f"{self.settings.base_url}/api/chat",
                json={
                    "model": self.settings.model,
                    "messages": messages,
                    "stream": False,
                    "format": schema,
                    "think": self.settings.thinking,
                    "options": {"temperature": 0},
                    "keep_alive": "10m",
                },
                timeout=(5, self.settings.timeout_seconds),
            )
            response.raise_for_status()
            payload = response.json()
            content = payload.get("message", {}).get("content", "")
            parsed = json.loads(str(content))
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise OllamaDiscoveryError(f"Ollama 返回无效结果：{exc}") from exc
        if not isinstance(parsed, dict):
            raise OllamaDiscoveryError("Ollama 结构化结果不是对象")
        return parsed

    @staticmethod
    def _validate_evaluations(
        payload: dict[str, Any],
        expected_ids: set[str],
        numeric_fields: tuple[str, ...],
    ) -> dict[str, dict[str, Any]]:
        values = payload.get("evaluations")
        if not isinstance(values, list):
            raise OllamaDiscoveryError("Ollama 结果缺少 evaluations 列表")
        output: dict[str, dict[str, Any]] = {}
        for raw in values:
            if not isinstance(raw, dict):
                continue
            video_id = str(raw.get("video_id") or "")
            if video_id not in expected_ids or video_id in output:
                continue
            valid = True
            parsed_scores: dict[str, float] = {}
            for field in numeric_fields:
                try:
                    score = float(raw[field])
                except (KeyError, TypeError, ValueError):
                    valid = False
                    break
                if not 0 <= score <= 100:
                    valid = False
                    break
                parsed_scores[field] = score
            if valid:
                # Some local models obey the schema bounds but still use 0-1 or
                # 0-10 editorial scales. Normalize only when every score in the
                # row fits the smaller scale, preserving genuine 0-100 rows.
                maximum_score = max(parsed_scores.values()) if parsed_scores else 100.0
                if maximum_score <= 1:
                    scale = 100.0
                elif maximum_score <= 10:
                    scale = 10.0
                else:
                    scale = 1.0
                for field, score in parsed_scores.items():
                    raw[field] = round(score * scale, 2)
                output[video_id] = raw
        return output

    def plan_queries(
        self,
        packs: list[dict[str, Any]],
        recent_titles: dict[str, list[str]],
        preferences: dict[str, list[dict[str, str]]],
    ) -> dict[str, list[str]]:
        if not self.settings.enabled or not self.settings.query_planning_enabled:
            return {}
        allowed = {str(pack["id"]) for pack in packs}
        prompt = {
            "task": "为每个领域生成三个互补的 YouTube 英文搜索词，扩大高质量候选召回。三个角度应分别覆盖实测/制作、解释/发现、挑战/结果；不要重复原查询，不要使用中文。",
            "rules": [
                "query 必须少于 100 个字符",
                "每个 pack_id 必须返回三个不同 query",
                "不要编造近期事件；只可使用提供的近期标题作为新实体依据",
                "优先实测、制作、解释、挑战、前后变化或意外结果",
                "避免 compilation、shorts、music、official trailer、no commentary",
            ],
            "packs": [
                {
                    "pack_id": pack["id"],
                    "label": pack["label"],
                    "description": pack["description"],
                    "seed_query": pack["query"],
                    "recent_titles": recent_titles.get(str(pack["id"]), [])[:8],
                }
                for pack in packs
            ],
            "preferences": preferences,
        }
        result = self._chat(
            [
                {"role": "system", "content": f"You are a YouTube research editor. Prompt version: {QUERY_PROMPT_VERSION}."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
            QUERY_SCHEMA,
        )
        output: dict[str, list[str]] = {}
        for raw in result.get("queries", []):
            if not isinstance(raw, dict):
                continue
            pack_id = str(raw.get("pack_id") or "")
            query = " ".join(str(raw.get("query") or "").split())
            queries = output.setdefault(pack_id, [])
            if (
                pack_id in allowed
                and 2 <= len(query) <= 100
                and "\n" not in query
                and query.casefold() not in {value.casefold() for value in queries}
                and len(queries) < 3
            ):
                queries.append(query)
        return {pack_id: queries for pack_id, queries in output.items() if queries}

    def evaluate_metadata(
        self,
        rows: list[dict[str, Any]],
        preferences: dict[str, list[dict[str, str]]],
    ) -> dict[str, dict[str, Any]]:
        if not rows:
            return {}
        compact_rows = [
            {
                "video_id": row["video_id"],
                "title": row["title"],
                "channel": row["channel_title"],
                "description": str(row.get("description") or "")[:1200],
                "tags": list(row.get("tags") or [])[:20],
                "topic": row.get("pack_label"),
                "duration_seconds": row.get("duration_seconds"),
                "age_hours": row.get("age_hours"),
                "views_per_hour": row.get("views_per_hour"),
                "has_caption": row.get("has_caption"),
            }
            for row in rows
        ]
        request = {
            "task": "像严谨的中文视频选题编辑一样评估候选。评价内容承诺，而不是播放量。只根据给定字段判断，不得臆造视频内容。",
            "audience": "希望把优质英语视频本地化为中文字幕的中文观众",
            "rubric": {
                "interestingness": "是否有清晰钩子、过程和值得看到最后的结果",
                "novelty": "题材或角度是否新鲜具体，而非模板化流水账",
                "story_payoff": "是否承诺实验结果、变化、发现、解释或叙事回报",
                "visual_potential": "是否可能主要依靠画面展示，而不是纯口播或背景知识",
                "localization_value": "跨文化可理解、翻译后仍有价值，并非只服务极窄圈层",
                "clickbait_risk": "标题空洞、夸张、合集、搬运、低成本或内容承诺不具体的风险",
            },
            "hard_rules": [
                "所有评分字段必须使用 0 到 100 分制；不要使用 0 到 10 分制。90 表示优秀，50 表示普通，10 表示很差",
                "不得因为高播放量直接提高定性评分",
                "标题或简介信息不足时降低 confidence",
                "纯更新播报、普通 gameplay、reaction、compilation 默认低分，除非有明确独特过程或结果",
                "keep 表示题材明确、有具体过程或回报且值得优先本地化；maybe 表示内容可能有价值但证据、画面或受众稍弱；reject 只用于明显跑题、空洞重复、欺骗性、低成本拼接或没有具体内容承诺的候选",
                "不要仅因口播、代码演示、屏幕录制或需要背景知识就判 reject；若有明确实测、比较、解释、发现或结果，应使用 keep 或 maybe 并在对应维度扣分",
                "reason_zh 必须具体说明保留或拒绝依据，最多 60 个汉字",
                "必须逐个返回所有 video_id，且不得产生新 ID",
            ],
            "preferences": preferences,
            "candidates": compact_rows,
        }
        result = self._chat(
            [
                {"role": "system", "content": f"You are a precise bilingual editorial ranker. Prompt version: {PROMPT_VERSION}."},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
            METADATA_SCHEMA,
        )
        output = self._validate_evaluations(
            result,
            {str(row["video_id"]) for row in rows},
            (
                "topic_fit",
                "interestingness",
                "novelty",
                "story_payoff",
                "visual_potential",
                "localization_value",
                "clickbait_risk",
                "language_confidence",
                "confidence",
            ),
        )
        for video_id, row in list(output.items()):
            if row.get("verdict") not in {"keep", "maybe", "reject"}:
                output.pop(video_id)
                continue
            row["reason_zh"] = str(row.get("reason_zh") or "").strip()[:160]
            if not row["reason_zh"]:
                output.pop(video_id)
        return output

    @staticmethod
    def fetch_thumbnail(url: str, *, maximum_bytes: int = 2 * 1024 * 1024) -> str:
        parsed = urlparse(url)
        allowed_hosts = {"i.ytimg.com", "img.youtube.com"}
        if parsed.scheme != "https" or (parsed.hostname or "").casefold() not in allowed_hosts:
            raise OllamaDiscoveryError("缩略图地址不是受信任的 YouTube 图片地址")
        try:
            response = requests.get(url, timeout=(3, 15), allow_redirects=False)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise OllamaDiscoveryError(f"读取缩略图失败：{exc}") from exc
        if len(response.content) > maximum_bytes:
            raise OllamaDiscoveryError("缩略图文件过大")
        content_type = str(response.headers.get("Content-Type") or "").casefold()
        if not content_type.startswith("image/"):
            raise OllamaDiscoveryError("缩略图响应不是图片")
        return base64.b64encode(response.content).decode("ascii")

    def evaluate_visual(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "You are a careful thumbnail editor. Judge only visible evidence and the supplied title. "
                    f"Prompt version: {VISUAL_PROMPT_VERSION}."
                ),
            }
        ]
        included: list[dict[str, Any]] = []
        for row in rows:
            try:
                image = self.fetch_thumbnail(str(row.get("thumbnail_url") or ""))
            except OllamaDiscoveryError:
                continue
            included.append(row)
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {"video_id": row["video_id"], "title": row["title"]},
                        ensure_ascii=False,
                    ),
                    "images": [image],
                }
            )
        if not included:
            return {}
        messages.append(
            {
                "role": "user",
                "content": (
                    "逐个评价以上缩略图：画面能否清楚传达具体内容、是否与标题一致、"
                    "是否像模板化垃圾内容或夸张诱导。所有评分字段必须使用 0 到 100 "
                    "分制，90 表示优秀或风险极高，50 表示普通，10 表示很低；不要使用 "
                    "0 到 1 或 0 到 10 分制。仅返回 schema 要求的 JSON。"
                ),
            }
        )
        result = self._chat(messages, VISUAL_SCHEMA)
        output = self._validate_evaluations(
            result,
            {str(row["video_id"]) for row in included},
            ("visual_potential", "title_thumbnail_consistency", "thumbnail_spam_risk"),
        )
        for row in output.values():
            row["reason_zh"] = str(row.get("reason_zh") or "").strip()[:160]
        return output

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = requests.post(
                f"{self.settings.base_url}/api/embed",
                json={"model": self.settings.embedding_model, "input": texts},
                timeout=(5, self.settings.timeout_seconds),
            )
            response.raise_for_status()
            payload = response.json()
            raw = payload.get("embeddings")
            if not isinstance(raw, list) or len(raw) != len(texts):
                raise ValueError("embedding 数量不匹配")
            return [[float(value) for value in vector] for vector in raw]
        except (requests.RequestException, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise OllamaDiscoveryError(f"Ollama embedding 失败：{exc}") from exc


__all__ = [
    "METADATA_SCHEMA",
    "PROMPT_VERSION",
    "QUERY_PROMPT_VERSION",
    "VISUAL_PROMPT_VERSION",
    "OllamaDiscoveryClient",
    "OllamaDiscoveryError",
    "OllamaSettings",
]
