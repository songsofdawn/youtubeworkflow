from __future__ import annotations

import json
import os
import tempfile
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from src.stage3.manifest import hash_config
from src.stage3.models import SubtitleSegment
from src.stage3.publish_metadata import load_category_mapping
from src.stage3.translator_deepseek import (
    PROMPT_VERSION,
    DeepSeekTranslator,
    build_messages,
    load_deepseek_settings,
)


CONFIG = {
    "temperature": 0.2, "max_retries": 3, "retry_delays_seconds": [0, 0, 0],
    "context_before": 1, "context_after": 1, "translation_batch_size": 2,
    "input_price_per_million": None, "output_price_per_million": None,
}


def response(rows):
    content = rows if isinstance(rows, str) else json.dumps({"segments": rows}, ensure_ascii=False)
    usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=usage)


class TranslatorTests(TestCase):
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

    def test_empty_json_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            client.chat.completions.create.side_effect = [response(""), response([{"id": 1, "translation": "一"}])]
            item = SubtitleSegment(1, 0, 1, "one")
            DeepSeekTranslator(CONFIG, directory, client=client, sleeper=lambda _: None).translate_batch(1, [item], [item], {}, {})
            self.assertEqual(client.chat.completions.create.call_count, 2)

    def test_missing_id_retry_contains_only_missing_segment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            client.chat.completions.create.side_effect = [response([{"id": 1, "translation": "一"}]), response([{"id": 2, "translation": "二"}])]
            items = [SubtitleSegment(1, 0, 1, "one"), SubtitleSegment(2, 1, 2, "two")]
            DeepSeekTranslator(CONFIG, directory, client=client, sleeper=lambda _: None).translate_batch(1, items, items, {}, {})
            second_prompt = client.chat.completions.create.call_args_list[1].kwargs["messages"][1]["content"]
            self.assertIn('"id": 2', second_prompt)
            self.assertNotIn('"id": 1, "start"', second_prompt)

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
            self.assertIn('"id": 2', prompt)
            self.assertNotIn('"id": 1, "start"', prompt)
