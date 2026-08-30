from __future__ import annotations

import json
import os
import tempfile
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from src.stage3.manifest import hash_config
from src.stage3.llm_providers import public_provider_catalog
from src.stage3.models import SubtitleSegment
from src.stage3.publish_metadata import load_category_mapping
from src.stage3.translator_deepseek import (
    PROMPT_VERSION,
    DeepSeekTranslator,
    TranslationError,
    build_messages,
    load_deepseek_settings,
)


CONFIG = {
    "temperature": 0.2, "max_retries": 3, "retry_delays_seconds": [0, 0, 0],
    "overload_max_retries": 5,
    "overload_retry_delays_seconds": [5, 15, 30, 60, 120],
    "response_max_retries": 6,
    "response_retry_delays_seconds": [0, 0, 0, 0, 0, 0],
    "degraded_batch_size": 16,
    "retry_jitter_seconds": 0,
    "context_before": 1, "context_after": 1, "translation_batch_size": 2,
    "input_price_per_million": None, "output_price_per_million": None,
}


def response(rows, *, finish_reason="stop", reasoning_content=""):
    content = rows if isinstance(rows, str) else json.dumps({"segments": rows}, ensure_ascii=False)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    message = SimpleNamespace(content=content, reasoning_content=reasoning_content)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
    )


class TranslatorTests(TestCase):
    def setUp(self) -> None:
        self.environment = mock.patch.dict(
            os.environ,
            {
                "TRANSLATION_PROVIDER": "deepseek",
                "TRANSLATION_MODEL": "",
                "TRANSLATION_BASE_URL": "",
                "TRANSLATION_THINKING": "disabled",
                "TRANSLATION_BATCH_SIZE": "2",
                "TRANSLATION_CONTEXT_BEFORE": "1",
                "TRANSLATION_CONTEXT_AFTER": "1",
                "TRANSLATION_MAX_OUTPUT_TOKENS": "4096",
                "DEEPSEEK_API_KEY": "unit-secret",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_catalog_marks_glm_47_flash_free_and_thinking_capable(self) -> None:
        catalog = public_provider_catalog(values={"TRANSLATION_PROVIDER": "zhipu"})
        zhipu = next(item for item in catalog["providers"] if item["id"] == "zhipu")
        glm_flash = next(item for item in zhipu["models"] if item["id"] == "glm-4.7-flash")
        self.assertTrue(glm_flash["free"])
        self.assertTrue(zhipu["thinking"])
        deepseek = next(item for item in catalog["providers"] if item["id"] == "deepseek")
        self.assertEqual(deepseek["default_thinking"], "disabled")

    def test_reads_deepseek_environment(self) -> None:
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key", "DEEPSEEK_MODEL": "test-model"}, clear=False):
            settings = load_deepseek_settings()
        self.assertEqual(settings["api_key"], "test-key")
        self.assertEqual(settings["model"], "test-model")

    def test_messages_include_context_glossary_and_json_requirement(self) -> None:
        items = [SubtitleSegment(i, i, i + 1, f"text {i}") for i in range(1, 4)]
        messages = build_messages([items[1]], [items[0]], [items[2]], {"fixed_terms": {"Roblox": "Roblox"}}, {"title": "T"})
        combined = "\n".join(item["content"] for item in messages)
        self.assertIn("输出 JSON", combined)
        self.assertIn("context_before_read_only", combined)
        self.assertIn("Roblox", combined)

    def test_publish_metadata_recommends_valid_mapped_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            client.chat.completions.create.return_value = response(
                json.dumps(
                    {
                        "chinese_title": "如何构建可靠的软件系统",
                        "tags": ["软件工程", "系统设计", "编程"],
                        "tid": 231,
                        "reason": "内容主要讨论软件工程。",
                    },
                    ensure_ascii=False,
                )
            )
            translator = DeepSeekTranslator(
                CONFIG,
                directory,
                client=client,
                sleeper=lambda _: None,
            )
            result = translator.recommend_publish_metadata(
                {
                    "title": "How to Build Reliable Software Systems",
                    "description": "A practical engineering guide.",
                    "tags": ["programming"],
                },
                [SubtitleSegment(1, 0, 2, "Today we design a reliable system.")],
                load_category_mapping(),
            )
        self.assertEqual(result["recommendation"]["tid"], 231)
        self.assertEqual(
            result["recommendation"]["category_path"],
            "科技 / 计算机技术",
        )
        prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertIn("allowed_bilibili_categories", prompt)
        self.assertIn("计算机技术", prompt)

    def test_shuffled_response_is_merged_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            client.chat.completions.create.return_value = response([{"id": 2, "translation": "二"}, {"id": 1, "translation": "一"}])
            items = [SubtitleSegment(1, 0, 1, "one"), SubtitleSegment(2, 1, 2, "two")]
            result = DeepSeekTranslator(CONFIG, directory, client=client, sleeper=lambda _: None).translate_batch(1, items, items, {}, {})
            self.assertEqual(result, {1: "一", 2: "二"})

    def test_zhipu_thinking_mode_and_output_cap_are_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "TRANSLATION_PROVIDER": "zhipu",
                "TRANSLATION_MODEL": "glm-4.7-flash",
                "TRANSLATION_BASE_URL": "https://open.bigmodel.cn/api/paas/v4/",
                "TRANSLATION_THINKING": "enabled",
                "TRANSLATION_MAX_OUTPUT_TOKENS": "777",
            },
            clear=False,
        ):
            client = mock.Mock()
            client.chat.completions.create.return_value = response(
                [{"id": 1, "translation": "一"}]
            )
            item = SubtitleSegment(1, 0, 1, "one")
            DeepSeekTranslator(CONFIG, directory, client=client).translate_batch(
                1, [item], [item], {}, {}
            )
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "glm-4.7-flash")
        self.assertEqual(kwargs["extra_body"]["thinking"]["type"], "enabled")
        self.assertLessEqual(kwargs["max_tokens"], 777)

    def test_anthropic_uses_native_messages_api(self) -> None:
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"segments":[{"id":1,"translation":"一"}]}',
                        }
                    ],
                    "usage": {"input_tokens": 20, "output_tokens": 8},
                }

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "TRANSLATION_PROVIDER": "anthropic",
                "TRANSLATION_MODEL": "claude-haiku-4-5",
                "TRANSLATION_BASE_URL": "https://api.anthropic.com",
                "TRANSLATION_THINKING": "disabled",
                "ANTHROPIC_API_KEY": "unit-secret",
            },
            clear=False,
        ):
            client = mock.Mock()
            client.post.return_value = FakeResponse()
            item = SubtitleSegment(1, 0, 1, "one")
            result = DeepSeekTranslator(CONFIG, directory, client=client).translate_batch(
                1, [item], [item], {}, {}
            )
        self.assertEqual(result, {1: "一"})
        self.assertTrue(client.post.call_args.args[0].endswith("/v1/messages"))
        self.assertEqual(client.post.call_args.kwargs["json"]["system"][:3], "你是视")

    def test_empty_json_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            client.chat.completions.create.side_effect = [response(""), response([{"id": 1, "translation": "一"}])]
            item = SubtitleSegment(1, 0, 1, "one")
            DeepSeekTranslator(CONFIG, directory, client=client, sleeper=lambda _: None).translate_batch(1, [item], [item], {}, {})
            self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_deepseek_empty_content_degrades_mode_and_splits_batch(self) -> None:
        items = [SubtitleSegment(index, index, index + 1, f"text {index}") for index in range(1, 5)]
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"TRANSLATION_THINKING": "enabled"},
            clear=False,
        ):
            client = mock.Mock()
            client.chat.completions.create.side_effect = [
                response("", reasoning_content="private reasoning"),
                response([{"id": 1, "translation": "一"}, {"id": 2, "translation": "二"}]),
                response([{"id": 3, "translation": "三"}, {"id": 4, "translation": "四"}]),
            ]
            translator = DeepSeekTranslator(
                CONFIG,
                directory,
                client=client,
                sleeper=lambda _: None,
            )
            result = translator.translate_batch(1, items, items, {}, {})
            checkpoint = json.loads(
                (Path(directory) / "checkpoints" / "batch_0001.json").read_text(
                    encoding="utf-8"
                )
            )

        first, second, third = client.chat.completions.create.call_args_list
        self.assertEqual(first.kwargs["extra_body"]["thinking"]["type"], "enabled")
        self.assertIn("response_format", first.kwargs)
        self.assertEqual(second.kwargs["extra_body"]["thinking"]["type"], "disabled")
        self.assertNotIn("response_format", second.kwargs)
        self.assertEqual(second.kwargs["max_tokens"], 4096)
        self.assertIn('"segments_to_translate":[[1,', second.kwargs["messages"][1]["content"])
        self.assertNotIn('"segments_to_translate":[[3,', second.kwargs["messages"][1]["content"])
        self.assertIn('"segments_to_translate":[[3,', third.kwargs["messages"][1]["content"])
        self.assertEqual(result, {1: "一", 2: "二", 3: "三", 4: "四"})
        self.assertEqual(checkpoint["usage"]["request_count"], 3)
        self.assertTrue(checkpoint["degraded"])

    def test_truncated_json_records_finish_reason_before_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            client.chat.completions.create.side_effect = [
                response(
                    '{"segments":[{"id":1,"translation":"一"}',
                    finish_reason="length",
                ),
                response([{"id": 1, "translation": "一"}]),
            ]
            observed: list[dict] = []

            def sleeper(_delay: float) -> None:
                observed.append(json.loads(
                    (Path(directory) / "checkpoints" / "batch_0001.json").read_text(
                        encoding="utf-8"
                    )
                ))

            item = SubtitleSegment(1, 0, 1, "one")
            result = DeepSeekTranslator(
                CONFIG,
                directory,
                client=client,
                sleeper=sleeper,
            ).translate_batch(1, [item], [item], {}, {})

        self.assertEqual(result, {1: "一"})
        self.assertEqual(observed[0]["failure_reason"], "truncated_json")
        self.assertEqual(observed[0]["finish_reason"], "length")

    def test_missing_id_retry_contains_only_missing_segment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            client.chat.completions.create.side_effect = [response([{"id": 1, "translation": "一"}]), response([{"id": 2, "translation": "二"}])]
            items = [SubtitleSegment(1, 0, 1, "one"), SubtitleSegment(2, 1, 2, "two")]
            DeepSeekTranslator(CONFIG, directory, client=client, sleeper=lambda _: None).translate_batch(1, items, items, {}, {})
            second_prompt = client.chat.completions.create.call_args_list[1].kwargs["messages"][1]["content"]
            self.assertIn('"segments_to_translate":[[2,', second_prompt)
            self.assertNotIn('"segments_to_translate":[[1,', second_prompt)
            self.assertNotIn(
                "response_format",
                client.chat.completions.create.call_args_list[1].kwargs,
            )

    def test_payload_overflow_retries_only_contaminated_segment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            client.chat.completions.create.side_effect = [
                response([
                    {"id": 1, "translation": "一"},
                    {"id": 2, "translation": "异常内容" * 100},
                ]),
                response([{"id": 2, "translation": "二"}]),
            ]
            items = [
                SubtitleSegment(1, 0, 1, "one"),
                SubtitleSegment(2, 1, 2, "two"),
            ]
            result = DeepSeekTranslator(
                CONFIG,
                directory,
                client=client,
                sleeper=lambda _: None,
            ).translate_batch(1, items, items, {}, {})
            second_prompt = (
                client.chat.completions.create.call_args_list[1]
                .kwargs["messages"][1]["content"]
            )

        self.assertEqual(result, {1: "一", 2: "二"})
        self.assertEqual(client.chat.completions.create.call_count, 2)
        self.assertIn('"segments_to_translate":[[2,', second_prompt)
        self.assertNotIn('"segments_to_translate":[[1,', second_prompt)
        self.assertNotIn(
            "response_format",
            client.chat.completions.create.call_args_list[1].kwargs,
        )

    def test_glm_1305_uses_long_backoff_and_saves_retry_state(self) -> None:
        class OverloadedError(RuntimeError):
            status_code = 429
            body = {"code": 1305, "message": "该模型当前访问量过大"}

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {
                "TRANSLATION_PROVIDER": "zhipu",
                "TRANSLATION_MODEL": "glm-4.7-flash",
                "ZHIPU_API_KEY": "unit-secret",
            },
            clear=False,
        ):
            client = mock.Mock()
            client.chat.completions.create.side_effect = [
                OverloadedError("429 / 1305") for _ in range(5)
            ] + [response([{"id": 1, "translation": "一"}])]
            waits: list[float] = []
            retry_states: list[str] = []

            def sleeper(delay: float) -> None:
                waits.append(delay)
                payload = json.loads(
                    (Path(directory) / "checkpoints" / "batch_0001.json").read_text(
                        encoding="utf-8"
                    )
                )
                retry_states.append(payload["status"])

            item = SubtitleSegment(1, 0, 1, "one")
            result = DeepSeekTranslator(
                CONFIG,
                directory,
                client=client,
                sleeper=sleeper,
                jitter=lambda _start, _end: 0,
            ).translate_batch(1, [item], [item], {}, {})

        self.assertEqual(result, {1: "一"})
        self.assertEqual(waits, [5, 15, 30, 60, 120])
        self.assertEqual(retry_states, ["retrying"] * 5)
        self.assertEqual(client.chat.completions.create.call_count, 6)

    def test_authentication_error_is_not_retried(self) -> None:
        class AuthenticationError(RuntimeError):
            status_code = 401
            body = {"code": "invalid_api_key"}

        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            client.chat.completions.create.side_effect = AuthenticationError("unauthorized")
            waits: list[float] = []
            item = SubtitleSegment(1, 0, 1, "one")
            with self.assertRaises(TranslationError):
                DeepSeekTranslator(
                    CONFIG,
                    directory,
                    client=client,
                    sleeper=waits.append,
                ).translate_batch(1, [item], [item], {}, {})
        self.assertEqual(waits, [])
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_completed_checkpoint_skips_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = Path(directory) / "checkpoints" / "batch_0001.json"
            checkpoint.parent.mkdir()
            client = mock.Mock()
            item = SubtitleSegment(1, 0, 1, "one")
            source_payload = [{"id": 1, "start": 0, "end": 1, "text": "one"}]
            with mock.patch.dict(os.environ, {"DEEPSEEK_MODEL": "checkpoint-model"}, clear=False):
                checkpoint.write_text(
                    json.dumps(
                        {
                            "status": "success",
                            "segment_ids": [1],
                            "translations": {"1": "一"},
                            "source_hash": hashlib.sha256(
                                json.dumps(source_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                            ).hexdigest(),
                            "prompt_version": PROMPT_VERSION,
                            "glossary_hash": hashlib.sha256(b"{}").hexdigest(),
                            "model": "checkpoint-model",
                        }
                    ),
                    encoding="utf-8",
                )
                result = DeepSeekTranslator(CONFIG, directory, client=client).translate_batch(1, [item], [item], {}, {})
            self.assertEqual(result, {1: "一"})
            client.chat.completions.create.assert_not_called()

    def test_partial_checkpoint_resumes_only_missing_ids_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            checkpoint = work / "checkpoints" / "batch_0001.json"
            checkpoint.parent.mkdir()
            items = [
                SubtitleSegment(1, 0, 1, "one"),
                SubtitleSegment(2, 1, 2, "two"),
            ]
            source_payload = [
                {"id": item.id, "start": item.start, "end": item.end, "text": item.text}
                for item in items
            ]
            client = mock.Mock()
            client.chat.completions.create.return_value = response(
                [{"id": 2, "translation": "二"}]
            )
            with mock.patch.dict(
                os.environ, {"DEEPSEEK_MODEL": "checkpoint-model"}, clear=False
            ):
                checkpoint.write_text(
                    json.dumps(
                        {
                            "status": "running",
                            "segment_ids": [1, 2],
                            "translations": {"1": "一"},
                            "usage": {
                                "prompt_tokens": 10,
                                "completion_tokens": 5,
                                "total_tokens": 15,
                            },
                            "source_hash": hashlib.sha256(
                                json.dumps(
                                    source_payload,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                ).encode("utf-8")
                            ).hexdigest(),
                            "prompt_version": PROMPT_VERSION,
                            "glossary_hash": hashlib.sha256(b"{}").hexdigest(),
                            "model": "checkpoint-model",
                            "translation_config_hash": hash_config(
                                {
                                    key: CONFIG.get(key)
                                    for key in (
                                        "temperature",
                                        "context_before",
                                        "context_after",
                                        "translation_batch_size",
                                    )
                                }
                                | {"pass_name": "raw"}
                            ),
                            "checkpoint_version": "stage3-translation-checkpoint-v2",
                        }
                    ),
                    encoding="utf-8",
                )
                result = DeepSeekTranslator(
                    CONFIG, work, client=client, sleeper=lambda _: None
                ).translate_batch(1, items, items, {}, {})
            self.assertEqual(result, {1: "一", 2: "二"})
            prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
            self.assertIn('"segments_to_translate":[[2,', prompt)
            self.assertNotIn('"segments_to_translate":[[1,', prompt)

    def test_partial_checkpoint_survives_thinking_enabled_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            checkpoint = work / "checkpoints" / "batch_0001.json"
            checkpoint.parent.mkdir()
            items = [
                SubtitleSegment(1, 0, 1, "one"),
                SubtitleSegment(2, 1, 2, "two"),
            ]
            source_payload = [
                {"id": item.id, "start": item.start, "end": item.end, "text": item.text}
                for item in items
            ]
            translation_hash = hash_config(
                {
                    "temperature": CONFIG["temperature"],
                    "context_before": CONFIG["context_before"],
                    "context_after": CONFIG["context_after"],
                    "translation_batch_size": CONFIG["translation_batch_size"],
                    "pass_name": "raw",
                }
            )
            checkpoint.write_text(
                json.dumps(
                    {
                        "status": "retrying",
                        "segment_ids": [1, 2],
                        "translations": {"1": "一"},
                        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                        "source_hash": hashlib.sha256(
                            json.dumps(
                                source_payload,
                                ensure_ascii=False,
                                sort_keys=True,
                            ).encode("utf-8")
                        ).hexdigest(),
                        "prompt_version": PROMPT_VERSION,
                        "glossary_hash": hashlib.sha256(b"{}").hexdigest(),
                        "provider": "deepseek",
                        "model": "checkpoint-model",
                        "thinking": "enabled",
                        "max_output_tokens": 4096,
                        "translation_config_hash": translation_hash,
                        "checkpoint_version": "stage3-translation-checkpoint-v2",
                    }
                ),
                encoding="utf-8",
            )
            client = mock.Mock()
            client.chat.completions.create.return_value = response(
                [{"id": 2, "translation": "二"}]
            )
            with mock.patch.dict(
                os.environ,
                {"DEEPSEEK_MODEL": "checkpoint-model", "TRANSLATION_THINKING": "disabled"},
                clear=False,
            ):
                result = DeepSeekTranslator(
                    CONFIG,
                    work,
                    client=client,
                    sleeper=lambda _: None,
                ).translate_batch(1, items, items, {}, {})
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))

        prompt = client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        self.assertEqual(result, {1: "一", 2: "二"})
        self.assertIn('"segments_to_translate":[[2,', prompt)
        self.assertNotIn('"segments_to_translate":[[1,', prompt)
        self.assertNotIn("response_format", client.chat.completions.create.call_args.kwargs)
        self.assertTrue(saved["partial_checkpoint_thinking_downgrade_reused"])
