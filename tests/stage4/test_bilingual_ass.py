from __future__ import annotations

import unittest

from src.stage4.bilingual_ass import (
    adaptive_font_size,
    build_bilingual_ass,
    escape_ass_text,
    resolve_fonts,
    scaled_style,
)
from src.stage4.models import SubtitleCue


STYLE = {
    "chinese_font": "Microsoft YaHei",
    "english_font": "Arial",
    "fallback_font": "Fallback",
    "chinese_font_size_1080p": 42,
    "english_font_size_1080p": 30,
    "outline_1080p": 2.5,
    "shadow_1080p": 0.8,
    "margin_v_1080p": 75,
    "margin_lr_1080p": 80,
    "primary_color": "&H00FFFFFF",
    "outline_color": "&H00000000",
    "shadow_color": "&H80000000",
    "alignment": 2,
    "max_english_lines": 2,
    "max_chinese_lines": 2,
    "max_combined_lines": 4,
}


class BilingualAssTests(unittest.TestCase):
    def test_default_style_uses_large_readable_bilingual_sizes(self) -> None:
        scaled = scaled_style({}, 1920, 1080)
        self.assertEqual(scaled["chinese_font_size"], 48)
        self.assertEqual(scaled["english_font_size"], 34)
        self.assertEqual(scaled["chinese_min_font_size"], 34)
        self.assertEqual(scaled["english_min_font_size"], 25)

    def test_special_characters_are_escaped(self) -> None:
        escaped = escape_ass_text(r"{\pos(1,2)} C:\temp")
        self.assertIn(r"\{", escaped)
        self.assertIn(r"\\pos", escaped)
        self.assertNotIn(r"{\pos", escaped)

    def test_english_is_above_chinese_in_one_event(self) -> None:
        english = [SubtitleCue("1", 0, 1, "Hello", ("Hello",))]
        chinese = [SubtitleCue("1", 0, 1, "你好", ("你好",))]
        value, _, _ = build_bilingual_ass(english, chinese, STYLE, width=1920, height=1080)
        dialogue = next(line for line in value.splitlines() if line.startswith("Dialogue:"))
        self.assertLess(dialogue.index("Hello"), dialogue.index("你好"))
        self.assertIn(r"Hello\N{", dialogue)
        self.assertEqual(value.count("Dialogue:"), 1)

    def test_standard_ass_sections_and_style_are_present(self) -> None:
        value, _, _ = build_bilingual_ass(
            [SubtitleCue("1", 0, 1, "A", ("A",))],
            [SubtitleCue("1", 0, 1, "中", ("中",))],
            STYLE,
            width=1920,
            height=1080,
        )
        self.assertIn("[Script Info]", value)
        self.assertIn("[V4+ Styles]", value)
        self.assertIn("[Events]", value)
        self.assertIn("ScaledBorderAndShadow: yes", value)
        self.assertIn("WrapStyle: 0", value)

    def test_720p_style_scales(self) -> None:
        scaled = scaled_style(STYLE, 1280, 720)
        self.assertEqual(scaled["chinese_font_size"], 28)
        self.assertEqual(scaled["english_font_size"], 20)
        self.assertEqual(scaled["play_res_x"], 1280)

    def test_1080p_style_is_reference_size(self) -> None:
        scaled = scaled_style(STYLE, 1920, 1080)
        self.assertEqual(scaled["chinese_font_size"], 42)
        self.assertEqual(scaled["english_font_size"], 30)
        self.assertEqual(scaled["margin_v"], 75)

    def test_4k_style_scales(self) -> None:
        scaled = scaled_style(STYLE, 3840, 2160)
        self.assertEqual(scaled["chinese_font_size"], 84)
        self.assertEqual(scaled["english_font_size"], 60)
        self.assertEqual(scaled["outline"], 5.0)

    def test_layout_overflow_is_reported_without_deleting_text(self) -> None:
        english = [SubtitleCue("7", 0, 1, "a\nb\nc", ("a", "b", "c"))]
        chinese = [SubtitleCue("7", 0, 1, "甲\n乙", ("甲", "乙"))]
        value, _, warnings = build_bilingual_ass(english, chinese, STYLE, width=1920, height=1080)
        self.assertEqual(warnings[0]["code"], "BILINGUAL_TOO_MANY_LINES")
        self.assertIn("a", value)
        self.assertIn("c", value)

    def test_missing_font_uses_configured_fallback(self) -> None:
        resolved, warnings = resolve_fonts(STYLE, installed_fonts={"Fallback"})
        self.assertEqual(resolved["chinese_font"], "Fallback")
        self.assertEqual(resolved["english_font"], "Fallback")
        self.assertEqual(len(warnings), 2)

    def test_long_line_uses_bounded_adaptive_font_size(self) -> None:
        cue = SubtitleCue("1", 0, 1, "word " * 30, ("word " * 30,))
        size = adaptive_font_size(
            cue,
            base_size=28,
            minimum_size=23,
            safe_width=700,
            combined_line_count=2,
        )
        self.assertGreaterEqual(size, 23)
        self.assertLess(size, 28)

    def test_adaptive_summary_records_adjusted_segments(self) -> None:
        english = [SubtitleCue("1", 0, 1, "word " * 30, ("word " * 30,))]
        chinese = [SubtitleCue("1", 0, 1, "很长的中文字幕" * 12, ("很长的中文字幕" * 12,))]
        _, scaled, _ = build_bilingual_ass(
            english,
            chinese,
            STYLE,
            width=1280,
            height=720,
        )
        summary = scaled["adaptive_font_size_summary"]
        self.assertEqual(summary["adjusted_segment_count"], 1)
        self.assertGreater(summary["chinese_min_used"], summary["english_min_used"])
