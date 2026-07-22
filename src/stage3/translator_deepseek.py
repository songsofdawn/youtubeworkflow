from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from .models import SubtitleSegment
from .subtitle_writer import write_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class TranslationError(RuntimeError):
    pass


def load_deepseek_settings() -> dict[str, str]:
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise TranslationError("Missing dependency: install requirements_stage3.txt") from exc
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    return {
        "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
        "base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
    }


def _usage_dict(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _response_content(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise TranslationError("API returned no choices")
    content = getattr(getattr(choices[0], "message", None), "content", None)
    if not content or not str(content).strip():
        raise TranslationError("API returned empty JSON")
    return str(content)


def build_messages(
    targets: list[SubtitleSegment],
    before: list[SubtitleSegment],
    after: list[SubtitleSegment],
    glossary: dict[str, Any],
    metadata: dict[str, str],
    *,
    polish: bool = False,
) -> list[dict[str, str]]:
    task = (
        "对有问题的中文字幕做受限润色：不改变事实，不增加信息，不改变 ID 或时间轴，压缩冗余并改成自然中文口语。"
        if polish
        else "把待翻译字幕忠实翻译为自然、简洁、适合中文视频的口语。保留事实、语气、专名、游戏梗和讽刺，不增不漏。"
    )
    system = (
        "你是专业视频字幕译者。" + task +
        "上下文仅供理解，不得返回或修改。只返回合法 JSON。输出 JSON，格式示例："
        '{"segments":[{"id":1,"translation":"我们终于等到 Roblox 的好消息了。"}]}'
    )
    payload = {
        "video_title": metadata.get("title", ""),
        "video_topic": metadata.get("topic", ""),
        "channel": metadata.get("channel", ""),
        "glossary": glossary,
        "context_before_read_only": [{"id": item.id, "text": item.text} for item in before],
        "segments_to_translate": [
            {"id": item.id, "start": item.start, "end": item.end, "duration": item.duration, "text": item.text}
            for item in targets
        ],
        "context_after_read_only": [{"id": item.id, "text": item.text} for item in after],
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "请按要求输出 JSON：\n" + json.dumps(payload, ensure_ascii=False)},
    ]


class DeepSeekTranslator:
    def __init__(
        self,
        config: dict[str, Any],
        work_dir: Path | str,
        *,
        client: Any = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.work_dir = Path(work_dir)
        self.checkpoint_dir = self.work_dir / "checkpoints"
        self.response_dir = self.work_dir / "responses"
        self.settings = load_deepseek_settings()
        self.sleeper = sleeper
        if client is None:
            if not self.settings["api_key"]:
                raise TranslationError("DEEPSEEK_API_KEY is not configured")
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise TranslationError("Missing dependency: install requirements_stage3.txt") from exc
            client = OpenAI(api_key=self.settings["api_key"], base_url=self.settings["base_url"])
        self.client = client
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def _checkpoint_path(self, batch_id: int, pass_name: str) -> Path:
        suffix = "" if pass_name == "raw" else f"_{pass_name}"
        return self.checkpoint_dir / f"batch_{batch_id:04d}{suffix}.json"

    def _load_completed(self, path: Path, expected_ids: list[int], force: bool) -> dict[int, str] | None:
        if force or not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("status") != "success" or payload.get("segment_ids") != expected_ids:
            return None
        usage = payload.get("usage", {})
        for key in self.usage:
            self.usage[key] += int(usage.get(key, 0) or 0)
        translations = payload.get("translations", {})
        return {int(key): str(value) for key, value in translations.items()}

    def _request(self, messages: list[dict[str, str]]) -> tuple[dict[int, str], dict[str, int], str]:
        response = self.client.chat.completions.create(
            model=self.settings["model"],
            messages=messages,
            temperature=float(self.config["temperature"]),
            response_format={"type": "json_object"},
        )
        content = _response_content(response)
        try:
            payload = json.loads(content)
            rows = payload["segments"]
            translations = {
                int(row["id"]): str(row.get("translation", "")).strip()
                for row in rows
                if "id" in row
            }
        except (ValueError, TypeError, KeyError) as exc:
            raise TranslationError("API returned invalid or truncated JSON") from exc
        return translations, _usage_dict(response), content

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
    ) -> dict[int, str]:
        checkpoint = self._checkpoint_path(batch_id, pass_name)
        expected = [item.id for item in targets]
        completed = self._load_completed(checkpoint, expected, force)
        if completed is not None:
            return completed
        result: dict[int, str] = {}
        batch_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if not force and checkpoint.is_file():
            previous = json.loads(checkpoint.read_text(encoding="utf-8"))
            if previous.get("segment_ids") == expected:
                result = {int(key): str(value) for key, value in previous.get("translations", {}).items() if str(value).strip()}
                for key in batch_usage:
                    batch_usage[key] = int(previous.get("usage", {}).get(key, 0) or 0)
                    self.usage[key] += batch_usage[key]
        pending = [item for item in targets if item.id not in result]
        index_by_id = {item.id: index for index, item in enumerate(all_segments)}
        attempts = 0
        last_error = ""
        response_file = self.response_dir / f"batch_{batch_id:04d}_{pass_name}.json"
        max_retries = int(self.config["max_retries"])
        delays = list(self.config.get("retry_delays_seconds", [2, 4, 8]))
        while pending and attempts <= max_retries:
            attempts += 1
            first_index = min(index_by_id[item.id] for item in pending)
            last_index = max(index_by_id[item.id] for item in pending)
            before = all_segments[max(0, first_index - int(self.config["context_before"])) : first_index]
            after = all_segments[last_index + 1 : last_index + 1 + int(self.config["context_after"])]
            try:
                received, usage, raw = self._request(
                    build_messages(pending, before, after, glossary, metadata, polish=pass_name == "polished")
                )
                write_json(response_file, json.loads(raw))
                for key in expected:
                    if key in received and received[key]:
                        result[key] = received[key]
                pending = [item for item in pending if item.id not in result]
                for key in self.usage:
                    self.usage[key] += usage[key]
                    batch_usage[key] += usage[key]
                if pending:
                    last_error = f"Missing IDs: {[item.id for item in pending]}"
                    raise TranslationError(last_error)
            except Exception as exc:
                last_error = str(exc)
                if attempts > max_retries:
                    break
                self.sleeper(float(delays[min(attempts - 1, len(delays) - 1)]))
        status = "success" if not pending else "failed"
        write_json(
            checkpoint,
            {
                "batch_id": batch_id,
                "segment_ids": expected,
                "status": status,
                "attempts": attempts,
                "model": self.settings["model"],
                "usage": batch_usage,
                "response_file": str(response_file),
                "error": "" if status == "success" else last_error,
                "translations": {str(key): value for key, value in result.items()},
            },
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
    ) -> dict[int, str]:
        batch_size = int(self.config["translation_batch_size"])
        merged: dict[int, str] = {}
        for offset in range(0, len(targets), batch_size):
            batch = targets[offset : offset + batch_size]
            merged.update(
                self.translate_batch(offset // batch_size + 1, batch, all_segments, glossary, metadata, pass_name=pass_name, force=force)
            )
        return merged

    def usage_report(self) -> dict[str, Any]:
        result: dict[str, Any] = dict(self.usage)
        input_price = self.config.get("input_price_per_million")
        output_price = self.config.get("output_price_per_million")
        if input_price is not None and output_price is not None:
            result["estimated_cost"] = round(
                self.usage["prompt_tokens"] / 1_000_000 * float(input_price)
                + self.usage["completion_tokens"] / 1_000_000 * float(output_price),
                6,
            )
        return result
