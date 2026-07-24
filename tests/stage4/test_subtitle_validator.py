from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.stage4.subtitle_validator import parse_srt_strict, validate_subtitles


EN = """1
00:00:00,000 --> 00:00:01,000
Hello.

2
00:00:01,100 --> 00:00:02,000
World.
"""
ZH = """1
00:00:00,000 --> 00:00:01,000
你好。

2
00:00:01,100 --> 00:00:02,000
世界。
"""


class SubtitleValidatorTests(unittest.TestCase):
    def write_pair(self, root: Path, english: str = EN, chinese: str = ZH) -> tuple[Path, Path]:
        left, right = root / "en.srt", root / "zh.srt"
        left.write_text(english, encoding="utf-8")
        right.write_text(chinese, encoding="utf-8")
        return left, right

    def test_valid_ids_and_timeline_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            left, right = self.write_pair(Path(temporary))
            result = validate_subtitles(left, right, video_duration=3)
            self.assertTrue(result.passed)
            self.assertTrue(result.report["id_sets_match"])
            self.assertTrue(result.report["timeline_matches"])

    def test_real_srt_ids_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.srt"
            path.write_text(EN.replace("\n2\n", "\n42\n"), encoding="utf-8")
            cues, diagnostics = parse_srt_strict(path)
            self.assertEqual([cue.identifier for cue in cues], ["1", "42"])
            self.assertFalse(diagnostics["duplicate_ids"])

    def test_missing_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            left, right = self.write_pair(Path(temporary), chinese=ZH.split("\n\n")[0] + "\n")
            result = validate_subtitles(left, right)
            self.assertEqual(result.report["validation_status"], "SUBTITLE_ID_MISMATCH")
            self.assertEqual(result.report["missing_chinese_ids"], ["2"])

    def test_extra_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            extra = ZH + "\n3\n00:00:02,100 --> 00:00:02,500\n额外。\n"
            left, right = self.write_pair(Path(temporary), chinese=extra)
            result = validate_subtitles(left, right)
            self.assertEqual(result.report["extra_chinese_ids"], ["3"])

    def test_timestamp_tolerance_passes_20ms(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shifted = ZH.replace("00:00:00,000", "00:00:00,020", 1)
            left, right = self.write_pair(Path(temporary), chinese=shifted)
            self.assertTrue(validate_subtitles(left, right, tolerance_ms=20).passed)

    def test_timestamp_over_tolerance_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shifted = ZH.replace("00:00:00,000", "00:00:00,021", 1)
            left, right = self.write_pair(Path(temporary), chinese=shifted)
            result = validate_subtitles(left, right, tolerance_ms=20)
            self.assertEqual(result.report["validation_status"], "SUBTITLE_TIMELINE_MISMATCH")
            self.assertEqual(result.report["timestamp_mismatch_ids"], ["1"])

    def test_empty_translation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            empty = ZH.replace("你好。", "")
            left, right = self.write_pair(Path(temporary), chinese=empty)
            result = validate_subtitles(left, right)
            self.assertEqual(result.report["empty_chinese_ids"], ["1"])

    def test_duplicate_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            duplicate = ZH.replace("\n2\n", "\n1\n")
            left, right = self.write_pair(Path(temporary), chinese=duplicate)
            result = validate_subtitles(left, right)
            self.assertEqual(result.report["chinese_duplicate_ids"], ["1"])
            self.assertFalse(result.passed)

    def test_invalid_and_out_of_duration_timestamps_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            invalid = ZH.replace("00:00:01,100 --> 00:00:02,000", "00:00:03,000 --> 00:00:02,000")
            left, right = self.write_pair(Path(temporary), chinese=invalid)
            result = validate_subtitles(left, right, video_duration=2.5)
            self.assertIn("2", result.report["invalid_timestamp_ids"])

    def test_illegal_control_character_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            left, right = self.write_pair(Path(temporary), chinese=ZH.replace("你好", "你\x00好"))
            result = validate_subtitles(left, right)
            self.assertEqual(result.report["illegal_control_ids"], ["1"])

