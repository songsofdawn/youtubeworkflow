from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable

from .llm_providers import PROVIDER_BY_ID, load_llm_settings
from .manifest import hash_config, sha256_file, utc_now
from .models import SubtitleSegment
from .publish_metadata import build_publish_metadata_messages, normalize_ai_recommendation
from .subtitle_writer import atomic_write_json
from .translation_qc import translation_payload_overflow
import math

PROMPT_VERSION = "stage3-translation-v6-dubbing-oral"
TRANSLATION_CHECKPOINT_VERSION = "stage3-translation-checkpoint-v2"
USAGE_KEYS = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    "reasoning_tokens", "request_count",
)
LOGGER = logging.getLogger(__name__)

_ENGLISH_WORD_RE = re.compile(
    r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?"
)

_CJK_RE = re.compile(
    r"[\u3400-\u9fff]"
)

def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0

    english_words = len(_ENGLISH_WORD_RE.findall(text))
    cjk_chars = len(_CJK_RE.findall(text))

    # 英文大约 0.75 word/token
    language_estimate = (
        english_words / 0.75
        + cjk_chars / 1.5
    )

    # JSON、标点、ID 等结构开销的保守估计
    char_estimate = len(text) / 4

    return max(
        1,
        math.ceil(language_estimate),
        math.ceil(char_estimate),
    )
class TranslationError(RuntimeError):
    pass


class ResponsePayloadError(TranslationError):
    """A successful HTTP response whose model payload cannot be consumed."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        usage: dict[str, int] | None = None,
        finish_reason: str = "",
        reasoning_content_present: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.usage = usage or {}
        self.finish_reason = finish_reason
        self.reasoning_content_present = reasoning_content_present


class IncompleteResponseError(TranslationError):
    def __init__(self, pending_ids: list[int], *, reason: str = "missing_ids") -> None:
        label = "Structurally invalid IDs" if reason == "payload_overflow" else "Missing IDs"
        super().__init__(f"{label}: {pending_ids}")
        self.pending_ids = pending_ids
        self.missing_ids = pending_ids
        self.reason = reason


def load_deepseek_settings() -> dict[str, Any]:
    """Backward-compatible name for the active provider settings."""
    return load_llm_settings()


def _attr(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _usage_dict(response: Any) -> dict[str, int]:
    usage = _attr(response, "usage", {}) or {}
    prompt = int(_attr(usage, "prompt_tokens", _attr(usage, "input_tokens", 0)) or 0)
    completion = int(_attr(usage, "completion_tokens", _attr(usage, "output_tokens", 0)) or 0)
    prompt_details = _attr(usage, "prompt_tokens_details", {}) or {}
    completion_details = _attr(usage, "completion_tokens_details", {}) or {}
    output_details = _attr(usage, "output_tokens_details", {}) or {}
    cache_hit = int(_attr(
        usage, "prompt_cache_hit_tokens",
        _attr(usage, "cache_read_input_tokens", _attr(prompt_details, "cached_tokens", 0)),
    ) or 0)
    cache_miss = int(_attr(
        usage, "prompt_cache_miss_tokens", _attr(usage, "cache_creation_input_tokens", 0)
    ) or 0)
    reasoning = int(_attr(
        usage, "reasoning_tokens",
        _attr(completion_details, "reasoning_tokens", _attr(output_details, "thinking_tokens", 0)),
    ) or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": int(_attr(usage, "total_tokens", prompt + completion) or prompt + completion),
        "prompt_cache_hit_tokens": cache_hit,
        "prompt_cache_miss_tokens": cache_miss,
        "reasoning_tokens": reasoning,
        "request_count": 1,
    }


def _response_content(response: Any) -> tuple[str, dict[str, int], str]:
    usage = _usage_dict(response)
    choices = _attr(response, "choices", None) or []
    if not choices:
        raise ResponsePayloadError(
            "API returned no choices",
            reason="no_choices",
            usage=usage,
        )
    choice = choices[0]
    message = _attr(choice, "message", {})
    content = _attr(message, "content", None)
    finish_reason = str(_attr(choice, "finish_reason", "") or "")
    reasoning_present = bool(str(_attr(message, "reasoning_content", "") or "").strip())
    if not content or not str(content).strip():
        raise ResponsePayloadError(
            "API returned empty JSON",
            reason="empty_content",
            usage=usage,
            finish_reason=finish_reason,
            reasoning_content_present=reasoning_present,
        )
    return str(content), usage, finish_reason


def _extract_json(content: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL | re.IGNORECASE).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise TranslationError("API returned invalid or truncated JSON")
    try:
        payload = json.loads(text[start : end + 1])
    except (TypeError, ValueError) as exc:
        raise TranslationError("API returned invalid or truncated JSON") from exc
    if not isinstance(payload, dict):
        raise TranslationError("API returned invalid JSON object")
    return payload


def build_messages(
    targets: list[SubtitleSegment],
    before: list[SubtitleSegment],
    after: list[SubtitleSegment],
    glossary: dict[str, Any],
    metadata: dict[str, str],
    *,
    polish: bool = False,
    for_dubbing: bool = False,
) -> list[dict[str, str]]:
    action = (
        "润色现有中文字幕；忠实原文，不新增事实，改成自然、简洁、适合字幕阅读的中文。"
        if polish else
        "将目标英文字幕忠实翻译成自然、简洁、适合字幕阅读的中文；保留事实、语气、专名、梗和讽刺，不增不漏。"
    )
    dubbing_instruction = (
        "这些目标用于中文配音，并且已经按完整发声单元（utterance）预先合并，不是屏幕显示碎片。"
        "译文将同时作为最终中文字幕文字和TTS唯一发声文本，因此只能有一份中文版本；"
        "不得额外生成字幕版、配音版、括号说明或旁白说明。"
        "优先使用中国观众日常说话时会用的自然口语和短句，少用书面连接词、被动句和冗长名词化表达；"
        "在不损失事实、条件、否定、因果、语气、专名和笑点的前提下主动避免冗余扩写，"
        "并结合每个ID提供的秒数控制朗读时长，让中文正常朗读时尽量能在该时长附近说完；"
        "可把约4.5个有效中文字/秒作为初始预算，不要为了填满时间扩写废话，"
        "若原意可以更简洁自然地表达，应优先压缩措辞而不是删掉数字、否定、条件、因果或专名。"
        if for_dubbing else ""
    )
    system = (
        "你是视频字幕译者。" + action
        + "目标数组=[ID,秒数,英文]；上下文只用于理解，不得输出。"
        + (
            "逐个ID独立翻译：每个ID已经是完整utterance，不得再拆分、吞并或跨ID移动内容。"
            if for_dubbing
            else "逐个ID独立翻译：每条translation只能翻译同一ID的英文，禁止把相邻目标、上下文或后续内容合并进该ID。"
        )
        + dubbing_instruction
        + "禁止解释或总结。必须保留每个目标ID且只输出 JSON："
        + '{"segments":[{"id":1,"translation":"译文"}]}'
    )
    payload = {
        "meta": {
            "title": metadata.get("title", ""),
            "topic": metadata.get("topic", ""),
            "channel": metadata.get("channel", ""),
        },
        "glossary": glossary,
        "context_before_read_only": [[item.id, item.text] for item in before],
        "segments_to_translate": [[item.id, round(item.duration, 2), item.text] for item in targets],
        "context_after_read_only": [[item.id, item.text] for item in after],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "按要求输出 JSON：" + json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        )},
    ]



class LLMTranslator:
    def __init__(
        self,
        config: dict[str, Any],
        work_dir: Path | str,
        *,
        client: Any = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.config = config
        self.work_dir = Path(work_dir)
        self.checkpoint_dir = self.work_dir / "checkpoints"
        self.response_dir = self.work_dir / "responses"
        self.settings = load_llm_settings()
        self.provider = PROVIDER_BY_ID[self.settings["provider"]]
        self.sleeper = sleeper
        self.jitter = jitter
        self.batch_size = int(os.environ.get(
            "TRANSLATION_BATCH_SIZE",
            self.config.get(
                "batch_size",
                self.config.get("translation_batch_size", self.settings["batch_size"]),
            ),
        ))
        self.context_before = int(os.environ.get(
            "TRANSLATION_CONTEXT_BEFORE",
            self.config.get("context_before", self.settings["context_before"]),
        ))
        self.context_after = int(os.environ.get(
            "TRANSLATION_CONTEXT_AFTER",
            self.config.get("context_after", self.settings["context_after"]),
        ))

        dynamic_env = os.environ.get("TRANSLATION_DYNAMIC_BATCH")
        if dynamic_env is None:
            self.dynamic_batch = bool(self.config.get("dynamic_batch", False))
        else:
            self.dynamic_batch = dynamic_env.strip().casefold() in {
                "1", "true", "yes", "on", "enabled",
            }

        self.batch_min = max(1, int(os.environ.get(
            "TRANSLATION_BATCH_MIN",
            self.config.get("batch_min", 64),
        )))
        self.batch_max = max(self.batch_min, int(os.environ.get(
            "TRANSLATION_BATCH_MAX",
            self.config.get("batch_max", 96),
        )))
        self.batch_target_tokens = max(512, int(os.environ.get(
            "TRANSLATION_BATCH_TARGET_TOKENS",
            self.config.get("batch_target_tokens", 4500),
        )))

        if self.dynamic_batch:
            # Production main batches are kept in [batch_min, batch_max].
            # Recovery retries may still go below batch_min.
            self.batch_size = max(
                self.batch_min,
                min(self.batch_max, self.batch_size),
            )

        self.max_output_tokens = int(self.settings["max_output_tokens"])
        if client is None:
            if not self.settings["api_key"]:
                raise TranslationError(f'{self.settings["key_env"]} is not configured')
            if self.provider.protocol == "anthropic":
                try:
                    import httpx
                except ImportError as exc:
                    raise TranslationError("Missing dependency: install requirements.txt") from exc
                client = httpx.Client(timeout=120.0)
            else:
                try:
                    from openai import OpenAI
                except ImportError as exc:
                    raise TranslationError("Missing dependency: install requirements.txt") from exc
                client = OpenAI(
                    api_key=self.settings["api_key"],
                    base_url=self.settings["base_url"],
                    timeout=120.0,
                    # One visible retry policy is easier to tune and avoids the
                    # SDK's short hidden retries defeating GLM overload backoff.
                    max_retries=0,
                )
        self.client = client
        self.usage = {key: 0 for key in USAGE_KEYS}

    def _estimate_batch_prompt_tokens(
        self,
        targets: list[SubtitleSegment],
        all_segments: list[SubtitleSegment],
        glossary: dict[str, Any],
        metadata: dict[str, str],
        *,
        pass_name: str = "raw",
        for_dubbing: bool = False,
    ) -> int:
        """Estimate the complete request size for dynamic batch planning."""
        if not targets:
            return 0

        index_by_id = {
            item.id: index
            for index, item in enumerate(all_segments)
        }
        first_index = min(index_by_id[item.id] for item in targets)
        last_index = max(index_by_id[item.id] for item in targets)

        before = all_segments[
            max(0, first_index - self.context_before) : first_index
        ]
        after = all_segments[
            last_index + 1 : last_index + 1 + self.context_after
        ]

        messages = build_messages(
            targets,
            before,
            after,
            glossary,
            metadata,
            polish=pass_name == "polished",
            for_dubbing=for_dubbing,
        )
        # Add a small allowance for role/message framing.
        return (
            sum(
                estimate_text_tokens(str(message.get("content", "")))
                for message in messages
            )
            + 24 * len(messages)
        )

    def _choose_batch_size(
        self,
        remaining_targets: list[SubtitleSegment],
        all_segments: list[SubtitleSegment],
        glossary: dict[str, Any],
        metadata: dict[str, str],
        *,
        pass_name: str = "raw",
        for_dubbing: bool = False,
    ) -> int:
        """Choose a production batch in [batch_min, batch_max] by prompt size."""
        remaining = len(remaining_targets)
        if remaining <= 0:
            return 0
        if not self.dynamic_batch:
            return min(self.batch_size, remaining)

        # The final tail is allowed to be smaller than the production minimum.
        if remaining <= self.batch_min:
            return remaining

        minimum = min(self.batch_min, remaining)
        maximum = min(self.batch_max, remaining)
        best = minimum

        # batch_min is a hard production floor. If 64 already exceeds the
        # soft token target, keep 64 rather than silently reverting to tiny
        # batches. Recovery logic below translate_batch may still split it.
        for size in range(minimum + 1, maximum + 1):
            estimated = self._estimate_batch_prompt_tokens(
                remaining_targets[:size],
                all_segments,
                glossary,
                metadata,
                pass_name=pass_name,
                for_dubbing=for_dubbing,
            )
            if estimated > self.batch_target_tokens:
                break
            best = size

        # Avoid an unnecessarily small tail, e.g. 96 + 54. Prefer 86 + 64
        # when the smaller current batch still fits the soft token target.
        tail = remaining - best
        if remaining >= self.batch_min * 2 and 0 < tail < self.batch_min:
            balanced = remaining - self.batch_min
            if self.batch_min <= balanced <= self.batch_max:
                estimated = self._estimate_batch_prompt_tokens(
                    remaining_targets[:balanced],
                    all_segments,
                    glossary,
                    metadata,
                    pass_name=pass_name,
                    for_dubbing=for_dubbing,
                )
                if estimated <= self.batch_target_tokens or balanced == self.batch_min:
                    best = balanced

        return best

    @staticmethod
    def _exception_details(exc: Exception) -> tuple[int | None, str]:
        response = _attr(exc, "response", None)
        raw_status = _attr(exc, "status_code", _attr(response, "status_code", None))
        try:
            status = int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            status = None
        body = _attr(exc, "body", None)
        if body is None and response is not None and hasattr(response, "json"):
            try:
                body = response.json()
            except Exception:
                body = None
        error = _attr(body, "error", {}) or {}
        code = _attr(body, "code", _attr(error, "code", ""))
        return status, str(code or "")

    @staticmethod
    def _retry_after_seconds(exc: Exception) -> float:
        response = _attr(exc, "response", None)
        headers = _attr(response, "headers", _attr(exc, "headers", {})) or {}
        normalized = {str(key).casefold(): value for key, value in dict(headers).items()}
        for key, divisor in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
            if key not in normalized:
                continue
            try:
                return max(0.0, float(normalized[key]) / divisor)
            except (TypeError, ValueError):
                continue
        return 0.0

    def _retry_decision(
        self,
        exc: Exception,
        failures: dict[str, int],
    ) -> tuple[str, float, int, int] | None:
        if isinstance(exc, (ResponsePayloadError, IncompleteResponseError)):
            kind = "response"
            maximum = int(self.config.get("response_max_retries", 6))
            delays = list(
                self.config.get("response_retry_delays_seconds", [1, 2, 4, 8, 16, 30])
            )
            status, code = None, ""
        else:
            status, code = self._exception_details(exc)
            kind = ""
            maximum = 0
            delays = []
        message = str(exc).casefold()
        overloaded = (
            status == 429
            or code == "1305"
            or "1305" in message
            or "访问量过大" in message
            or "too many requests" in message
        )
        if kind == "response":
            pass
        elif overloaded:
            kind = "overload"
            maximum = int(self.config.get("overload_max_retries", 5))
            delays = list(
                self.config.get("overload_retry_delays_seconds", [5, 15, 30, 60, 120])
            )
        else:
            if status is not None and status not in {408, 409, 425, 500, 502, 503, 504}:
                return None
            kind = "transient"
            maximum = int(self.config.get("max_retries", 3))
            delays = list(self.config.get("retry_delays_seconds", [2, 4, 8]))
        failures[kind] = failures.get(kind, 0) + 1
        failure_number = failures[kind]
        if failure_number > maximum:
            return None
        if not delays:
            delays = [2.0]
        scheduled = float(delays[min(failure_number - 1, len(delays) - 1)])
        server_requested = self._retry_after_seconds(exc)
        jitter_max = max(0.0, float(self.config.get("retry_jitter_seconds", 3.0)))
        wait_seconds = max(scheduled, server_requested) + self.jitter(0.0, jitter_max)
        return kind, wait_seconds, failure_number, maximum

    def _checkpoint_path(self, batch_id: int, pass_name: str) -> Path:
        suffix = "" if pass_name == "raw" else f"_{pass_name}"
        return self.checkpoint_dir / f"batch_{batch_id:04d}{suffix}.json"

    @staticmethod
    def _legacy_optional_checkpoint_key(key: str) -> bool:
        return key in {
            "translation_config_hash", "checkpoint_version", "provider",
            "thinking", "reasoning_effort", "max_output_tokens",
        }

    @staticmethod
    def _add_usage(target: dict[str, int], usage: dict[str, Any]) -> None:
        for key in USAGE_KEYS:
            target[key] = int(target.get(key, 0) or 0) + int(usage.get(key, 0) or 0)

    def _load_completed(
        self,
        path: Path,
        expected_ids: list[int],
        force: bool,
        metadata: dict[str, Any],
    ) -> dict[int, str] | None:
        if force or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if payload.get("status") != "success" or payload.get("segment_ids") != expected_ids:
            return None
        changed = False
        for key, value in metadata.items():
            if key not in payload and self._legacy_optional_checkpoint_key(key):
                payload[key] = value
                changed = True
            elif payload.get(key) != value:
                return None
        translations = payload.get("translations", {})
        if set(map(int, translations)) != set(expected_ids):
            return None
        result = {int(key): str(value) for key, value in translations.items() if str(value).strip()}
        if set(result) != set(expected_ids):
            return None
        translations_hash = hashlib.sha256(json.dumps(
            {str(key): value for key, value in sorted(result.items())},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if payload.get("translations_hash") not in {None, translations_hash}:
            return None
        if payload.get("translations_hash") is None:
            payload["translations_hash"] = translations_hash
            changed = True
        response_path = Path(str(payload.get("response_path") or ""))
        if payload.get("response_path") and response_path.is_file():
            response_hash = sha256_file(response_path)
            if payload.get("response_hash") not in {None, response_hash}:
                return None
            if payload.get("response_hash") is None:
                payload["response_hash"] = response_hash
                changed = True
        if changed:
            atomic_write_json(path, payload)
        self._add_usage(self.usage, payload.get("usage", {}))
        return result

    def _output_limit(self, messages: list[dict[str, str]], *, degraded: bool = False) -> int:
        if degraded:
            return self.max_output_tokens
        is_glm53_flash = (
            self.provider.id == "zhipu"
            and self.settings["model"].casefold() == "glm-5.3-flash"
        )
        multiplier = 1.25 if self.provider.id == "deepseek" or is_glm53_flash else 0.9
        return min(
            self.max_output_tokens,
            max(512, int(len(messages[-1]["content"]) * multiplier)),
        )

    def _openai_kwargs(
        self,
        messages: list[dict[str, str]],
        *,
        degraded: bool = False,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.settings["model"], "messages": messages}
        limit_name = "max_completion_tokens" if self.provider.id in {"openai", "minimax"} else "max_tokens"
        kwargs[limit_name] = self._output_limit(messages, degraded=degraded)
        if self.provider.temperature:
            kwargs["temperature"] = float(self.config.get("temperature", 0.2))
        deepseek_fallback = self.provider.id == "deepseek" and degraded
        if self.provider.json_mode and not deepseek_fallback:
            kwargs["response_format"] = {"type": "json_object"}
        is_glm53_flash = (
            self.provider.id == "zhipu"
            and self.settings["model"].casefold() == "glm-5.3-flash"
        )
        enabled = (
            True
            if is_glm53_flash
            else self.settings["thinking"] == "enabled" and not deepseek_fallback
        )
        if self.provider.thinking_style == "openai" and self.settings["model"].startswith("gpt-5"):
            kwargs["reasoning_effort"] = "high" if enabled else "none"

        extra_body: dict[str, Any] = {}
        if is_glm53_flash:
            # GLM-5.3-Flash is forced-thinking. The API accepts only enabled,
            # while low/high/max controls the reasoning budget.
            extra_body["thinking"] = {"type": "enabled"}
            extra_body["reasoning_effort"] = self.settings.get(
                "reasoning_effort", "low"
            )
        elif self.provider.thinking_style == "object" and not (
            self.provider.id == "custom" and not enabled
        ):
            extra_body["thinking"] = {"type": "enabled" if enabled else "disabled"}
        elif self.provider.thinking_style == "boolean" and not self.settings["model"].startswith("qwen-mt"):
            extra_body["enable_thinking"] = enabled
        elif self.provider.thinking_style == "minimax":
            extra_body["reasoning_split"] = True
        if extra_body:
            kwargs["extra_body"] = extra_body
        return kwargs

    def _request_anthropic(
        self,
        messages: list[dict[str, str]],
        *,
        degraded: bool = False,
    ) -> tuple[str, dict[str, int], str]:
        enabled = self.settings["thinking"] == "enabled" and not degraded
        max_tokens = self._output_limit(messages, degraded=degraded)
        payload: dict[str, Any] = {
            "model": self.settings["model"],
            "max_tokens": max_tokens,
            "system": messages[0]["content"],
            "messages": messages[1:],
        }
        if enabled and "haiku-4-5" in self.settings["model"]:
            payload["thinking"] = {"type": "enabled", "budget_tokens": 1024}
            payload["max_tokens"] = max(max_tokens, 1536)
        elif enabled:
            payload["thinking"] = {"type": "adaptive"}
        else:
            payload["thinking"] = {"type": "disabled"}
        base_url = self.settings["base_url"].rstrip("/")
        endpoint = base_url + ("/messages" if base_url.endswith("/v1") else "/v1/messages")
        response = self.client.post(
            endpoint,
            headers={
                "x-api-key": self.settings["api_key"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
        )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        body = response.json() if hasattr(response, "json") else response
        blocks = _attr(body, "content", []) or []
        content = "\n".join(
            str(_attr(block, "text", ""))
            for block in blocks if _attr(block, "type", "") == "text"
        )
        usage = _usage_dict(body)
        finish_reason = str(_attr(body, "stop_reason", "") or "")
        if not content.strip():
            raise ResponsePayloadError(
                "API returned empty JSON",
                reason="empty_content",
                usage=usage,
                finish_reason=finish_reason,
            )
        return content, usage, finish_reason

    def _request_content(
        self,
        messages: list[dict[str, str]],
        *,
        degraded: bool = False,
    ) -> tuple[str, dict[str, int], str]:
        if self.provider.protocol == "anthropic":
            return self._request_anthropic(messages, degraded=degraded)
        response = self.client.chat.completions.create(
            **self._openai_kwargs(messages, degraded=degraded)
        )
        return _response_content(response)

    def _request(
        self,
        messages: list[dict[str, str]],
        *,
        degraded: bool = False,
    ) -> tuple[dict[int, str], dict[str, int], str, str]:
        content, usage, finish_reason = self._request_content(messages, degraded=degraded)
        try:
            payload = _extract_json(content)
        except TranslationError as exc:
            truncated = finish_reason in {"length", "max_tokens"}
            raise ResponsePayloadError(
                "API returned truncated JSON" if truncated else str(exc),
                reason="truncated_json" if truncated else "invalid_json",
                usage=usage,
                finish_reason=finish_reason,
            ) from exc
        try:
            translations = {
                int(row["id"]): str(row.get("translation", "")).strip()
                for row in payload["segments"] if "id" in row
            }
        except (TypeError, KeyError, ValueError) as exc:
            raise ResponsePayloadError(
                "API returned invalid or truncated JSON",
                reason="invalid_json",
                usage=usage,
                finish_reason=finish_reason,
            ) from exc
        return translations, usage, json.dumps(payload, ensure_ascii=False), finish_reason

    def request_json_object(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str = "auxiliary",
        response_filename: str | None = None,
    ) -> dict[str, Any]:
        """Run one structured auxiliary LLM request with normal retry policy.

        This is used by dubbing semantic-boundary repair and canonical duration
        rewrite so they share the exact provider/thinking/retry behavior of the
        translation path without pretending their payload is a translation batch.
        """

        attempts = 0
        failures: dict[str, int] = {}
        degraded = False
        while True:
            attempts += 1
            try:
                content, usage, finish_reason = self._request_content(
                    messages, degraded=degraded
                )
                try:
                    payload = _extract_json(content)
                except TranslationError as exc:
                    truncated = finish_reason in {"length", "max_tokens"}
                    raise ResponsePayloadError(
                        "API returned truncated JSON" if truncated else str(exc),
                        reason="truncated_json" if truncated else "invalid_json",
                        usage=usage,
                        finish_reason=finish_reason,
                    ) from exc
                self._add_usage(self.usage, usage)
                if response_filename:
                    response_path = self.response_dir / response_filename
                    atomic_write_json(
                        response_path,
                        {
                            "purpose": purpose,
                            "attempts": attempts,
                            "usage": usage,
                            "payload": payload,
                        },
                    )
                return payload
            except Exception as exc:
                if self.provider.id == "deepseek" and isinstance(exc, ResponsePayloadError):
                    degraded = True
                decision = self._retry_decision(exc, failures)
                if decision is None:
                    raise TranslationError(
                        f"{purpose} request failed after {attempts} attempt(s): {exc}"
                    ) from exc
                kind, wait_seconds, failure_number, maximum = decision
                LOGGER.warning(
                    "AI API %s during %s; retrying in %.1fs (%d/%d)",
                    kind,
                    purpose,
                    wait_seconds,
                    failure_number,
                    maximum,
                )
                self.sleeper(wait_seconds)

    def recommend_publish_metadata(
        self,
        metadata: dict[str, Any],
        segments: list[SubtitleSegment],
        category_mapping: dict[str, Any],
    ) -> dict[str, Any]:
        messages = build_publish_metadata_messages(metadata, segments, category_mapping)
        attempts = 0
        failures: dict[str, int] = {}
        last_error = ""
        degraded = False
        while True:
            attempts += 1
            try:
                content, usage, finish_reason = self._request_content(
                    messages,
                    degraded=degraded,
                )
                try:
                    payload = _extract_json(content)
                except TranslationError as exc:
                    truncated = finish_reason in {"length", "max_tokens"}
                    raise ResponsePayloadError(
                        "API returned truncated JSON" if truncated else str(exc),
                        reason="truncated_json" if truncated else "invalid_json",
                        usage=usage,
                        finish_reason=finish_reason,
                    ) from exc
                recommendation = normalize_ai_recommendation(payload, metadata, category_mapping)
                response_path = self.response_dir / "publish_metadata.json"
                atomic_write_json(response_path, payload)
                return {
                    "recommendation": recommendation,
                    "usage": usage,
                    "response_path": str(response_path),
                    "response_hash": sha256_file(response_path),
                    "attempts": attempts,
                }
            except Exception as exc:
                last_error = str(exc)
                if self.provider.id == "deepseek" and isinstance(exc, ResponsePayloadError):
                    degraded = True
                decision = self._retry_decision(exc, failures)
                if decision is None:
                    break
                kind, wait_seconds, failure_number, maximum = decision
                LOGGER.warning(
                    "AI API %s while generating publish metadata; retrying in %.1fs (%d/%d)",
                    kind,
                    wait_seconds,
                    failure_number,
                    maximum,
                )
                self.sleeper(wait_seconds)
        raise TranslationError(
            f"Publish metadata recommendation failed after {attempts} attempts: {last_error}"
        )

    def translate_batch(
        self,
        batch_id: int,
        targets: list[SubtitleSegment],
        all_segments: list[SubtitleSegment],
        glossary: dict[str, Any],
        metadata: dict[str, str],
        *,
        pass_name: str = "raw",
        force: bool = False,
        for_dubbing: bool = False,
    ) -> dict[int, str]:
        checkpoint = self._checkpoint_path(batch_id, pass_name)
        expected = [item.id for item in targets]
        source_payload = [
            {"id": item.id, "start": item.start, "end": item.end, "text": item.text}
            for item in all_segments
        ]
        translation_config = {
            "temperature": self.config.get("temperature"),
            "context_before": self.context_before,
            "context_after": self.context_after,
            "translation_batch_size": self.batch_size,
            "pass_name": pass_name,
        }
        if self.dynamic_batch:
            translation_config.update({
                "dynamic_batch": True,
                "batch_min": self.batch_min,
                "batch_max": self.batch_max,
                "batch_target_tokens": self.batch_target_tokens,
            })
        if self.settings.get("reasoning_effort"):
            translation_config["reasoning_effort"] = self.settings["reasoning_effort"]
        if for_dubbing:
            # Keep the regular translation hash compatible with existing
            # checkpoints; the dedicated dubbing mode must never reuse one.
            translation_config["for_dubbing"] = True
        checkpoint_metadata = {
            "source_hash": hashlib.sha256(json.dumps(
                source_payload, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")).hexdigest(),
            "prompt_version": PROMPT_VERSION,
            "glossary_hash": hashlib.sha256(json.dumps(
                glossary, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")).hexdigest(),
            "provider": self.settings["provider"],
            "model": self.settings["model"],
            "thinking": self.settings["thinking"],
            "reasoning_effort": self.settings.get("reasoning_effort", ""),
            "max_output_tokens": self.max_output_tokens,
            "translation_config_hash": hash_config(translation_config),
            "checkpoint_version": TRANSLATION_CHECKPOINT_VERSION,
        }
        completed = self._load_completed(checkpoint, expected, force, checkpoint_metadata)
        if completed is not None:
            return completed
        result: dict[int, str] = {}
        batch_usage = {key: 0 for key in USAGE_KEYS}
        degraded = False
        request_limit: int | None = None
        priority_targets: list[SubtitleSegment] = []
        reused_thinking_downgrade = False
        if not force and checkpoint.is_file():
            try:
                previous = json.loads(checkpoint.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = {}
            metadata_matches = all(
                previous.get(key) == value
                or (key not in previous and self._legacy_optional_checkpoint_key(key))
                for key, value in checkpoint_metadata.items()
            )
            partial_thinking_downgrade = (
                previous.get("status") in {"running", "retrying", "failed"}
                and previous.get("thinking") == "enabled"
                and checkpoint_metadata["thinking"] == "disabled"
                and all(
                    key == "thinking"
                    or previous.get(key) == value
                    or (key not in previous and self._legacy_optional_checkpoint_key(key))
                    for key, value in checkpoint_metadata.items()
                )
            )
            if previous.get("segment_ids") == expected and (
                metadata_matches or partial_thinking_downgrade
            ):
                result = {
                    int(key): str(value)
                    for key, value in previous.get("translations", {}).items()
                    if str(value).strip()
                }
                self._add_usage(batch_usage, previous.get("usage", {}))
                self._add_usage(self.usage, previous.get("usage", {}))
                reused_thinking_downgrade = partial_thinking_downgrade
                degraded = bool(previous.get("degraded", False)) or reused_thinking_downgrade
                saved_limit = int(previous.get("fallback_request_size", 0) or 0)
                request_limit = saved_limit or (
                    int(self.config.get("degraded_batch_size", 16))
                    if reused_thinking_downgrade
                    else None
                )
        pending = [item for item in targets if item.id not in result]
        index_by_id = {item.id: index for index, item in enumerate(all_segments)}
        attempts, last_error = 0, ""
        last_failure_reason, last_finish_reason = "", ""
        last_request_ids: list[int] = []
        failures: dict[str, int] = {}
        response_file = self.response_dir / f"batch_{batch_id:04d}_{pass_name}.json"

        def write_progress(
            status: str,
            *,
            error: str = "",
            retry_kind: str = "",
            next_retry_seconds: float = 0.0,
            failure_reason: str = "",
            finish_reason: str = "",
            request_ids: list[int] | None = None,
        ) -> None:
            atomic_write_json(checkpoint, {
                "batch_id": batch_id,
                "segment_ids": expected,
                **checkpoint_metadata,
                "status": status,
                "attempts": attempts,
                "usage": batch_usage,
                "response_path": str(response_file),
                "response_hash": sha256_file(response_file) if response_file.is_file() else "",
                "error": error,
                "retry_kind": retry_kind,
                "next_retry_seconds": round(next_retry_seconds, 3),
                "failure_reason": failure_reason,
                "finish_reason": finish_reason,
                "degraded": degraded,
                "partial_checkpoint_thinking_downgrade_reused": reused_thinking_downgrade,
                "fallback_request_size": request_limit or 0,
                "last_request_ids": request_ids or [],
                "translations": {str(key): value for key, value in result.items()},
                "translations_hash": hashlib.sha256(json.dumps(
                    {str(key): value for key, value in sorted(result.items())},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest(),
                "completed_at": utc_now() if status == "success" else "",
                "updated_at": utc_now(),
            })

        while pending:
            attempts += 1
            pending_ids = {item.id for item in pending}
            active_targets = [item for item in priority_targets if item.id in pending_ids]
            if not active_targets:
                active_targets = pending
                priority_targets = []
            request_targets = (
                active_targets[:request_limit]
                if request_limit is not None
                else active_targets
            )
            requested_ids = {item.id for item in request_targets}
            first_index = min(index_by_id[item.id] for item in request_targets)
            last_index = max(index_by_id[item.id] for item in request_targets)
            before = all_segments[max(0, first_index - self.context_before) : first_index]
            after = all_segments[last_index + 1 : last_index + 1 + self.context_after]
            try:
                received, usage, raw, finish_reason = self._request(
                    build_messages(
                        request_targets,
                        before,
                        after,
                        glossary,
                        metadata,
                        polish=pass_name == "polished",
                        for_dubbing=for_dubbing,
                    ),
                    degraded=degraded,
                )
                atomic_write_json(response_file, json.loads(raw))
                targets_by_id = {item.id: item for item in request_targets}
                overflow_details = [
                    detail
                    for key in requested_ids
                    if key in received and received[key]
                    if (
                        detail := translation_payload_overflow(
                            targets_by_id[key], received[key]
                        )
                    ) is not None
                ]
                overflow_ids = {int(item["id"]) for item in overflow_details}
                for key in requested_ids:
                    if key in received and received[key] and key not in overflow_ids:
                        result[key] = received[key]
                pending = [item for item in pending if item.id not in result]
                self._add_usage(self.usage, usage)
                self._add_usage(batch_usage, usage)
                request_missing = [item for item in request_targets if item.id not in result]
                response_reason = "payload_overflow" if overflow_ids else "missing_ids"
                write_progress(
                    "running" if pending else "success",
                    error=(
                        (
                            f"Structurally invalid IDs: {sorted(overflow_ids)}"
                            if overflow_ids
                            else f"Missing IDs: {[item.id for item in request_missing]}"
                        )
                        if request_missing
                        else f"Remaining IDs: {[item.id for item in pending]}" if pending else ""
                    ),
                    failure_reason=response_reason if request_missing else "",
                    finish_reason=finish_reason,
                    request_ids=sorted(requested_ids),
                )
                if request_missing:
                    raise IncompleteResponseError(
                        [item.id for item in request_missing],
                        reason=response_reason,
                    )
                priority_targets = []
            except Exception as exc:
                last_error = str(exc)
                failure_reason = ""
                finish_reason = ""
                if isinstance(exc, ResponsePayloadError):
                    self._add_usage(self.usage, exc.usage)
                    self._add_usage(batch_usage, exc.usage)
                    failure_reason = exc.reason
                    finish_reason = exc.finish_reason
                    priority_targets = [
                        item for item in request_targets if item.id in {p.id for p in pending}
                    ]
                    # Whole-response failure: halve the current request.
                    # Examples: 96 -> 48 -> 24 -> 12, 64 -> 32 -> 16 -> 8.
                    request_limit = max(1, (len(request_targets) + 1) // 2)
                    if self.provider.id == "deepseek":
                        degraded = True
                elif isinstance(exc, IncompleteResponseError):
                    failure_reason = exc.reason
                    missing = set(exc.pending_ids)
                    priority_targets = [item for item in pending if item.id in missing]
                    request_limit = (
                        1
                        if exc.reason == "payload_overflow"
                        else min(
                            int(self.config.get("degraded_batch_size", 16)),
                            request_limit
                            or int(self.config.get("degraded_batch_size", 16)),
                        )
                    )
                    if self.provider.id == "deepseek":
                        degraded = True
                last_failure_reason = failure_reason
                last_finish_reason = finish_reason
                last_request_ids = sorted(requested_ids)
                decision = self._retry_decision(exc, failures)
                if decision is None:
                    break
                kind, wait_seconds, failure_number, maximum = decision
                write_progress(
                    "retrying",
                    error=last_error,
                    retry_kind=kind,
                    next_retry_seconds=wait_seconds,
                    failure_reason=failure_reason,
                    finish_reason=finish_reason,
                    request_ids=sorted(requested_ids),
                )
                LOGGER.warning(
                    "AI API batch %d %s (%s); progress saved, retrying %d IDs in %.1fs (%d/%d)",
                    batch_id,
                    kind,
                    failure_reason or "request_error",
                    min(
                        len(priority_targets) or len(request_targets),
                        request_limit or len(request_targets),
                    ),
                    wait_seconds,
                    failure_number,
                    maximum,
                )
                self.sleeper(wait_seconds)
        status = "success" if not pending else "failed"
        write_progress(
            status,
            error="" if status == "success" else last_error,
            failure_reason="" if status == "success" else last_failure_reason,
            finish_reason="" if status == "success" else last_finish_reason,
            request_ids=[] if status == "success" else last_request_ids,
        )
        if pending:
            raise TranslationError(f"Batch {batch_id} failed after {attempts} attempts: {last_error}")
        return result

    def translate_all(
        self,
        targets: list[SubtitleSegment],
        all_segments: list[SubtitleSegment],
        glossary: dict[str, Any],
        metadata: dict[str, str],
        *,
        pass_name: str = "raw",
        force: bool = False,
        for_dubbing: bool = False,
    ) -> dict[int, str]:

        merged: dict[int, str] = {}

        cursor = 0
        batch_id = 1

        while cursor < len(targets):
            remaining_targets = targets[cursor:]

            batch_size = self._choose_batch_size(
                remaining_targets,
                all_segments,
                glossary,
                metadata,
                pass_name=pass_name,
                for_dubbing=for_dubbing,
            )
            if batch_size <= 0:
                raise TranslationError(
                    "Dynamic batch planner returned an invalid batch size"
                )

            batch = targets[cursor : cursor + batch_size]
            estimated_tokens = self._estimate_batch_prompt_tokens(
                batch,
                all_segments,
                glossary,
                metadata,
                pass_name=pass_name,
                for_dubbing=for_dubbing,
            )
            LOGGER.info(
                "Translation batch %d: %d subtitles, estimated prompt=%d tokens "
                "(dynamic=%s, production range=%d-%d, target=%d)",
                batch_id,
                len(batch),
                estimated_tokens,
                self.dynamic_batch,
                self.batch_min,
                self.batch_max,
                self.batch_target_tokens,
            )

            merged.update(
                self.translate_batch(
                    batch_id,
                    batch,
                    all_segments,
                    glossary,
                    metadata,
                    pass_name=pass_name,
                    force=force,
                    for_dubbing=for_dubbing,
                )
            )

            cursor += batch_size
            batch_id += 1

        return merged

    def usage_report(self) -> dict[str, Any]:
        result: dict[str, Any] = dict(self.usage)
        result.update({
            "provider": self.settings["provider"],
            "model": self.settings["model"],
            "thinking": self.settings["thinking"],
            "reasoning_effort": self.settings.get("reasoning_effort", ""),
            "batch_size": self.batch_size,
            "dynamic_batch": self.dynamic_batch,
            "batch_min": self.batch_min,
            "batch_max": self.batch_max,
            "batch_target_tokens": self.batch_target_tokens,
            "context_before": self.context_before,
            "context_after": self.context_after,
            "max_output_tokens": self.max_output_tokens,
        })
        input_price = self.config.get("input_price_per_million")
        output_price = self.config.get("output_price_per_million")
        if input_price is not None and output_price is not None:
            result["estimated_cost"] = round(
                self.usage["prompt_tokens"] / 1_000_000 * float(input_price)
                + self.usage["completion_tokens"] / 1_000_000 * float(output_price),
                6,
            )
        return result


DeepSeekTranslator = LLMTranslator


__all__ = [
    "PROMPT_VERSION", "TRANSLATION_CHECKPOINT_VERSION", "DeepSeekTranslator",
    "LLMTranslator", "TranslationError", "build_messages",
    "load_deepseek_settings", "load_llm_settings",
]
