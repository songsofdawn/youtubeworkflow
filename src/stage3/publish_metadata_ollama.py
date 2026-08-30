from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.discovery.ollama_client import OllamaDiscoveryClient, OllamaSettings

from .manifest import sha256_file
from .models import SubtitleSegment
from .publish_metadata import (
    build_publish_metadata_messages,
    normalize_ai_recommendation,
)
from .subtitle_writer import atomic_write_json


PUBLISH_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "chinese_title": {"type": "string"},
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 10,
        },
        "tid": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["chinese_title", "tags", "tid", "reason"],
}


class OllamaPublishMetadataClient:
    """Small adapter that gives the local discovery model the metadata API shape."""

    def __init__(self, settings: OllamaSettings, response_dir: Path) -> None:
        if not settings.enabled:
            raise ValueError("Ollama 本地模型尚未启用")
        self.settings = settings
        self.client = OllamaDiscoveryClient(settings)
        self.response_dir = response_dir

    def recommend_publish_metadata(
        self,
        metadata: dict[str, Any],
        segments: list[SubtitleSegment],
        category_mapping: dict[str, Any],
    ) -> dict[str, Any]:
        payload = self.client._chat(  # Shared structured-output transport.
            build_publish_metadata_messages(metadata, segments, category_mapping),
            PUBLISH_METADATA_SCHEMA,
        )
        recommendation = normalize_ai_recommendation(
            payload,
            metadata,
            category_mapping,
        )
        response_path = self.response_dir / "publish_metadata_ollama.json"
        atomic_write_json(response_path, payload)
        return {
            "recommendation": recommendation,
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "provider": "ollama",
                "model": self.settings.model,
            },
            "response_path": str(response_path),
            "response_hash": sha256_file(response_path),
            "attempts": 1,
        }


def load_ollama_settings(project_root: Path) -> OllamaSettings:
    path = project_root / "config" / "trending_config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 Ollama 配置：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Ollama 配置必须是 JSON 对象")
    return OllamaSettings.from_config(payload)


__all__ = [
    "OllamaPublishMetadataClient",
    "PUBLISH_METADATA_SCHEMA",
    "load_ollama_settings",
]
