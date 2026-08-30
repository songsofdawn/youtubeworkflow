from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from ..stage3.llm_providers import API_KEY_ENV_NAMES


EDITABLE_SECRET_KEYS = ("YOUTUBE_API_KEY", *API_KEY_ENV_NAMES)
EDITABLE_SETTING_KEYS = (
    "TRANSLATION_PROVIDER",
    "TRANSLATION_MODEL",
    "TRANSLATION_BASE_URL",
    "TRANSLATION_THINKING",
    "TRANSLATION_BATCH_SIZE",
    "TRANSLATION_CONTEXT_BEFORE",
    "TRANSLATION_CONTEXT_AFTER",
    "TRANSLATION_MAX_OUTPUT_TOKENS",
)
EDITABLE_ENV_KEYS = (*EDITABLE_SECRET_KEYS, *EDITABLE_SETTING_KEYS)
COOKIE_FILE_HEADERS = ("# Netscape HTTP Cookie File", "# HTTP Cookie File")
MAX_COOKIE_FILE_BYTES = 4 * 1024 * 1024
DISCOVERY_SETTING_FIELDS = {
    "discovery_llm_enabled",
    "discovery_ollama_base_url",
    "discovery_ollama_model",
    "discovery_embedding_model",
    "discovery_embedding_enabled",
    "discovery_query_planning_enabled",
    "discovery_visual_enabled",
    "discovery_metadata_batch_size",
    "discovery_visual_top_n",
    "discovery_timeout_seconds",
    "discovery_thinking",
    "discovery_recall_target",
    "discovery_max_search_requests",
    "discovery_metadata_max_candidates",
}
DEFAULT_ENV_LINES = (
    "YOUTUBE_API_KEY=",
    "DEEPSEEK_API_KEY=",
    "ZHIPU_API_KEY=",
    "DASHSCOPE_API_KEY=",
    "MOONSHOT_API_KEY=",
    "MINIMAX_API_KEY=",
    "ARK_API_KEY=",
    "OPENAI_API_KEY=",
    "ANTHROPIC_API_KEY=",
    "CUSTOM_LLM_API_KEY=",
    "TRANSLATION_PROVIDER=deepseek",
    "TRANSLATION_MODEL=deepseek-v4-flash",
    "TRANSLATION_BASE_URL=https://api.deepseek.com",
    "TRANSLATION_THINKING=disabled",
    "TRANSLATION_BATCH_SIZE=32",
    "TRANSLATION_CONTEXT_BEFORE=2",
    "TRANSLATION_CONTEXT_AFTER=2",
    "TRANSLATION_MAX_OUTPUT_TOKENS=4096",
    "DEEPSEEK_BASE_URL=https://api.deepseek.com",
    "DEEPSEEK_MODEL=deepseek-v4-flash",
    "BILIUP_EXE=",
    "BILIUP_COOKIE_FILE=",
)


def normalize_secret(value: object, name: str) -> str:
    secret = str(value or "").strip()
    if "\n" in secret or "\r" in secret:
        raise ValueError(f"{name} 不能包含换行符")
    if len(secret) > 2048:
        raise ValueError(f"{name} 长度异常")
    return secret


def update_env_file(path: Path | str, updates: Mapping[str, str]) -> Path:
    destination = Path(path)
    invalid = sorted(set(updates) - set(EDITABLE_ENV_KEYS))
    if invalid:
        raise ValueError(f"不允许修改这些环境变量：{', '.join(invalid)}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    original = (
        destination.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        if destination.is_file()
        else list(DEFAULT_ENV_LINES)
    )
    pending = dict(updates)
    output: list[str] = []
    for line in original:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in pending:
                output.append(f"{key}={pending.pop(key)}")
                continue
        output.append(line)
    for key in EDITABLE_ENV_KEYS:
        if key in pending:
            output.append(f"{key}={pending[key]}")
    text = "\n".join(output).rstrip("\n") + "\n"
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def update_discovery_settings(
    path: Path | str,
    values: Mapping[str, Any],
) -> list[str]:
    requested = DISCOVERY_SETTING_FIELDS & set(values)
    if not requested:
        return []
    destination = Path(path)
    try:
        payload = json.loads(destination.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取智能发现配置：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("智能发现配置必须是 JSON 对象")
    raw = payload.get("discovery_llm")
    discovery = dict(raw) if isinstance(raw, dict) else {}

    bool_fields = {
        "discovery_llm_enabled": "enabled",
        "discovery_embedding_enabled": "embedding_enabled",
        "discovery_query_planning_enabled": "query_planning_enabled",
        "discovery_visual_enabled": "visual_enabled",
        "discovery_thinking": "thinking",
    }
    for field, target in bool_fields.items():
        if field in values:
            if not isinstance(values[field], bool):
                raise ValueError(f"{field} 必须是布尔值")
            discovery[target] = values[field]

    text_fields = {
        "discovery_ollama_model": "model",
        "discovery_embedding_model": "embedding_model",
    }
    for field, target in text_fields.items():
        if field in values:
            value = str(values[field] or "").strip()
            if not value or len(value) > 160 or "\n" in value or "\r" in value:
                raise ValueError(f"{field} 无效")
            discovery[target] = value
    if "discovery_ollama_base_url" in values:
        base_url = str(values["discovery_ollama_base_url"] or "").strip().rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            raise ValueError("Ollama Base URL 必须是有效的 http(s) 地址")
        if len(base_url) > 500 or "\n" in base_url or "\r" in base_url:
            raise ValueError("Ollama Base URL 无效")
        discovery["base_url"] = base_url

    number_fields = {
        "discovery_metadata_batch_size": ("metadata_batch_size", 1, 30),
        "discovery_visual_top_n": ("visual_top_n", 0, 100),
        "discovery_timeout_seconds": ("timeout_seconds", 10, 900),
        "discovery_metadata_max_candidates": ("metadata_max_candidates", 10, 600),
    }
    for field, (target, minimum, maximum) in number_fields.items():
        if field not in values:
            continue
        try:
            number = int(values[field])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} 必须是整数") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
        discovery[target] = number

    top_level_number_fields = {
        "discovery_recall_target": ("discovery_recall_target", 50, 5000),
        "discovery_max_search_requests": ("discovery_max_search_requests", 1, 100),
    }
    for field, (target, minimum, maximum) in top_level_number_fields.items():
        if field not in values:
            continue
        try:
            number = int(values[field])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} 必须是整数") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
        payload[target] = number

    payload["discovery_llm"] = discovery
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return sorted(requested)


def validate_youtube_cookie_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Cookies 文件内容无效")
    text = value.lstrip("\ufeff")
    if not text.strip():
        raise ValueError("Cookies 文件为空")
    if "\x00" in text:
        raise ValueError("Cookies 文件包含无效字符")
    if len(text.encode("utf-8")) > MAX_COOKIE_FILE_BYTES:
        raise ValueError("Cookies 文件过大，最大允许 4 MB")
    lines = text.splitlines()
    first_line = lines[0].strip() if lines else ""
    if first_line not in COOKIE_FILE_HEADERS:
        raise ValueError(
            "Cookies 必须是 Netscape 格式，第一行应为 # Netscape HTTP Cookie File"
        )
    cookie_rows: list[list[str]] = []
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or (stripped.startswith("#") and not stripped.startswith("#HttpOnly_")):
            continue
        columns = stripped.split("\t")
        if len(columns) >= 7:
            cookie_rows.append(columns)
    if not cookie_rows:
        raise ValueError("Cookies 文件中没有可用的 Cookie 记录")
    domains = [row[0].removeprefix("#HttpOnly_").casefold() for row in cookie_rows]
    if not any(domain == "youtube.com" or domain.endswith(".youtube.com") for domain in domains):
        raise ValueError("Cookies 文件中没有找到 youtube.com 登录信息")
    return "\n".join(lines).rstrip("\n") + "\n"


def youtube_cookie_status(path: Path | str) -> dict[str, object]:
    cookie_path = Path(path)
    if not cookie_path.is_file() or cookie_path.stat().st_size <= 0:
        return {"ready": False, "exists": False, "size": 0}
    size = cookie_path.stat().st_size
    try:
        validate_youtube_cookie_text(
            cookie_path.read_text(encoding="utf-8-sig", errors="replace")
        )
    except (OSError, ValueError):
        return {"ready": False, "exists": True, "size": size}
    return {"ready": True, "exists": True, "size": size}


def save_youtube_cookie_file(path: Path | str, value: object) -> Path:
    destination = Path(path)
    text = validate_youtube_cookie_text(value)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


__all__ = [
    "EDITABLE_SECRET_KEYS",
    "EDITABLE_SETTING_KEYS",
    "EDITABLE_ENV_KEYS",
    "COOKIE_FILE_HEADERS",
    "MAX_COOKIE_FILE_BYTES",
    "DISCOVERY_SETTING_FIELDS",
    "normalize_secret",
    "save_youtube_cookie_file",
    "update_env_file",
    "update_discovery_settings",
    "validate_youtube_cookie_text",
    "youtube_cookie_status",
]
