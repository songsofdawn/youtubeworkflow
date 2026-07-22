from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from src.stage3.models import SubtitleSegment
from src.stage3.translator_deepseek import DeepSeekTranslator, build_messages, load_deepseek_settings


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
            checkpoint.write_text(json.dumps({"status": "success", "segment_ids": [1], "translations": {"1": "一"}}), encoding="utf-8")
            client = mock.Mock()
            item = SubtitleSegment(1, 0, 1, "one")
            result = DeepSeekTranslator(CONFIG, directory, client=client).translate_batch(1, [item], [item], {}, {})
            self.assertEqual(result, {1: "一"})
            client.chat.completions.create.assert_not_called()
