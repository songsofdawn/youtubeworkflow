from __future__ import annotations

import unittest

from src.stage4.bilingual_ass import (
    adaptive_font_size,
    ass_generator_version,
    build_bilingual_ass,
    escape_ass_text,
    orientation_font_multiplier,
    resolve_fonts,
    scaled_style,
    split_text_to_width,
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
    "one_line_per_language": True,
    "language_order": "english_above_chinese",
    "minimum_horizontal_scale_percent": 92,
    "max_english_lines": 1,
    "max_chinese_lines": 1,
    "max_combined_lines": 2,
}


class BilingualAssTests(unittest.TestCase):
    def test_default_style_uses_large_readable_bilingual_sizes(self) -> None:
        scaled = scaled_style({}, 608, 1080)
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
        self.assertIn("WrapStyle: 2", value)

    def test_720p_style_scales(self) -> None:
        scaled = scaled_style(STYLE, 1280, 720)
        self.assertEqual(scaled["chinese_font_size"], 28)
        self.assertEqual(scaled["english_font_size"], 20)
        self.assertEqual(scaled["play_res_x"], 1280)
        self.assertEqual(scaled["orientation_font_multiplier"], 1.0)

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

    def test_portrait_style_keeps_existing_font_size(self) -> None:
        scaled = scaled_style(STYLE, 608, 1080)
        self.assertEqual(scaled["chinese_font_size"], 42)
        self.assertEqual(scaled["english_font_size"], 30)
        self.assertEqual(scaled["outline"], 2.5)
        self.assertEqual(scaled["orientation_font_multiplier"], 1.0)
        self.assertEqual(ass_generator_version(608, 1080), "1.11")

    def test_landscape_uses_configured_1080p_reference_size_once(self) -> None:
        self.assertEqual(orientation_font_multiplier(1440, 1080), 1.0)
        self.assertEqual(orientation_font_multiplier(1920, 1080), 1.0)
        self.assertEqual(orientation_font_multiplier(2560, 1080), 1.0)
        self.assertEqual(ass_generator_version(1920, 1080), "1.11")

    def test_source_wrapping_is_collapsed_to_one_line_per_language(self) -> None:
        english = [SubtitleCue("7", 0, 1, "a\nb\nc", ("a", "b", "c"))]
        chinese = [SubtitleCue("7", 0, 1, "甲\n乙", ("甲", "乙"))]
        value, _, warnings = build_bilingual_ass(english, chinese, STYLE, width=1920, height=1080)
        dialogue = next(line for line in value.splitlines() if line.startswith("Dialogue:"))
        self.assertIn("a b c", dialogue)
        self.assertIn("甲乙", dialogue)
        self.assertEqual(dialogue.count(r"\N"), 1)
        self.assertEqual(warnings, [])

    def test_language_order_can_put_chinese_above_english(self) -> None:
        style = STYLE | {"language_order": "chinese_above_english"}
        value, _, _ = build_bilingual_ass(
            [SubtitleCue("1", 0, 1, "Hello", ("Hello",))],
            [SubtitleCue("1", 0, 1, "你好", ("你好",))],
            style,
            width=1920,
            height=1080,
        )
        dialogue = next(line for line in value.splitlines() if line.startswith("Dialogue:"))
        self.assertLess(dialogue.index("你好"), dialogue.index("Hello"))

    def test_long_joined_line_is_measured_as_one_line(self) -> None:
        english = [SubtitleCue("1", 0, 1, "word " * 15 + "\n" + "word " * 15, ())]
        chinese = [SubtitleCue("1", 0, 1, "中文字幕", ("中文字幕",))]
        value, scaled, _ = build_bilingual_ass(english, chinese, STYLE, width=1280, height=720)
        dialogue = next(line for line in value.splitlines() if line.startswith("Dialogue:"))
        self.assertEqual(dialogue.count(r"\N"), 1)
        self.assertEqual(
            scaled["adaptive_font_size_summary"]["fragmented_segment_count"], 1
        )

    def test_long_but_valid_single_line_shrinks_without_width_warning(self) -> None:
        english = [SubtitleCue("1", 0, 4, "word " * 14, ())]
        chinese = [SubtitleCue("1", 0, 4, "这是用于验证普通长句能够安全缩小而不会被左右裁切的中文字幕内容" * 2, ())]
        _, scaled, warnings = build_bilingual_ass(
            english,
            chinese,
            STYLE,
            width=1920,
            height=1080,
        )
        self.assertEqual(warnings, [])
        self.assertGreaterEqual(
            scaled["adaptive_font_size_summary"]["chinese_min_used"],
            scaled["chinese_absolute_min_font_size"],
        )

    def test_long_line_is_paginated_with_one_line_per_language(self) -> None:
        english = [SubtitleCue("9", 0, 8, "a concise English source", ())]
        chinese = [SubtitleCue("9", 0, 8, "这是需要自动分页但拥有足够显示时间的中文字幕" * 6, ())]
        value, scaled, warnings = build_bilingual_ass(
            english,
            chinese,
            STYLE,
            width=1920,
            height=1080,
        )
        dialogues = [line for line in value.splitlines() if line.startswith("Dialogue:")]
        self.assertGreater(len(dialogues), 1)
        self.assertTrue(all(line.count(r"\N") == 1 for line in dialogues))
        self.assertEqual(warnings, [])
        self.assertEqual(
            scaled["adaptive_font_size_summary"]["fragmented_segment_count"], 1
        )
        self.assertEqual(
            scaled["adaptive_font_size_summary"]["font_tier_counts"], {"base": 1}
        )

    def test_pathological_single_line_is_paginated_but_still_blocked_if_unreadable(self) -> None:
        english = [SubtitleCue("165", 0, 4.2, "bootleg minecraft", ())]
        chinese = [SubtitleCue("165", 0, 4.2, "异常内容" * 100, ())]
        value, scaled, warnings = build_bilingual_ass(
            english,
            chinese,
            STYLE,
            width=1920,
            height=1080,
        )
        self.assertEqual([item["id"] for item in warnings], ["165"])
        self.assertEqual(warnings[0]["code"], "BILINGUAL_FRAGMENT_DURATION_TOO_SHORT")
        self.assertGreater(value.count("Dialogue:"), 1)
        self.assertGreater(
            scaled["adaptive_font_size_summary"]["maximum_fragment_count"], 1
        )
        self.assertGreaterEqual(
            scaled["adaptive_font_size_summary"]["chinese_min_used"],
            scaled["chinese_min_font_size"],
        )
        self.assertGreaterEqual(
            scaled["adaptive_font_size_summary"]["english_min_used"],
            scaled["english_min_font_size"],
        )

    def test_indivisible_token_is_hard_split_without_overflow(self) -> None:
        pages = split_text_to_width("A" * 100, 10)
        self.assertGreater(len(pages), 1)
        self.assertEqual("".join(pages), "A" * 100)
        # ASCII alphanumerics consume 0.56 width units each.
        self.assertTrue(all(len(page) * 0.56 <= 10 for page in pages))

    def test_pagination_balances_pages_without_orphaning_last_word(self) -> None:
        text = (
            "Since I've started my two-week Minecraft phase, thanks to Speed and Kai, "
            "I wondered"
        )
        pages = split_text_to_width(text, 40)
        self.assertEqual(len(pages), 2)
        self.assertNotEqual(pages[-1], "wondered")
        lengths = [len(page) for page in pages]
        self.assertLess(max(lengths) / min(lengths), 1.6)

    def test_exact_two_page_boundary_does_not_orphan_speaker_marker(self) -> None:
        text = (
            ">> This is exactly what I was looking for. Both our characters stay "
            "consistent from start to finish in any type of environment or mood "
            "we put them through,"
        )
        pages = split_text_to_width(text, 40.0)
        self.assertEqual(len(pages), 2)
        self.assertTrue(all(len(page) > 30 for page in pages))
        self.assertNotEqual(pages[0], ">")
        self.assertNotEqual(pages[1], text[1:])

    def test_production_size_paginates_speaker_marker_without_overflow(self) -> None:
        style = STYLE | {
            "chinese_font_size_1080p": 60,
            "english_font_size_1080p": 44,
            "chinese_min_font_size_1080p": 54,
            "english_min_font_size_1080p": 40,
            "max_line_width_ratio": 0.92,
        }
        english = [
            SubtitleCue(
                "144",
                0,
                6.19,
                (
                    ">> This is exactly what I was looking for. Both our characters "
                    "stay consistent from start to finish in any type of environment "
                    "or mood we put them through,"
                ),
                (),
            )
        ]
        chinese = [
            SubtitleCue(
                "144",
                0,
                6.19,
                "这正是我想要的。无论我们把角色置于何种环境或情绪中，他们从始至终都保持一致。",
                (),
            )
        ]
        value, _, warnings = build_bilingual_ass(
            english, chinese, style, width=1920, height=1080
        )
        dialogues = [
            line for line in value.splitlines() if line.startswith("Dialogue:")
        ]
        self.assertEqual(len(dialogues), 2)
        self.assertEqual(warnings, [])
        self.assertTrue(all(line.count(r"\N") == 1 for line in dialogues))

    def test_production_pagination_keeps_rounding_headroom(self) -> None:
        style = STYLE | {
            "chinese_font_size_1080p": 60,
            "english_font_size_1080p": 44,
            "chinese_min_font_size_1080p": 54,
            "english_min_font_size_1080p": 40,
            "max_line_width_ratio": 0.92,
        }
        english = [
            SubtitleCue(
                "38",
                0,
                6.59,
                (
                    ">> Oh, man. Humans are just a base level >> Oh, man. Humans "
                    "are just a base level food. Humans are like seasoning to its "
                    "food if you consider planets being its real meal."
                ),
                (),
            )
        ]
        chinese = [
            SubtitleCue(
                "38",
                0,
                6.59,
                (
                    "哦，天哪。人类只是最基本的食物。如果把行星视为它的真正食物，"
                    "那么人类就像是它食物中的调味料。"
                ),
                (),
            )
        ]
        _, _, warnings = build_bilingual_ass(
            english,
            chinese,
            style,
            width=1920,
            height=1080,
        )
        self.assertEqual(warnings, [])

    def test_late_chinese_comma_does_not_create_short_orphan_page(self) -> None:
        text = "自从我因为Speed和Kai开始了为期两周的Minecraft阶段，我就想知道"
        pages = split_text_to_width(text, 29)
        self.assertEqual(len(pages), 2)
        lengths = [len(page) for page in pages]
        self.assertLess(max(lengths) / min(lengths), 1.6)

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
