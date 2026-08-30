from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.stage4.layout_review import (
    apply_layout_review_override,
    load_layout_review,
    save_layout_review,
)
from src.stage4.models import ResolvedInputs


ENGLISH = (
    "1\n00:00:00,000 --> 00:00:01,000\n"
    + "very long english text " * 30
    + "\n\n2\n00:00:01,000 --> 00:00:02,000\nSafe second cue.\n"
)
CHINESE = (
    "1\n00:00:00,000 --> 00:00:01,000\n"
    + "很长的中文字幕" * 40
    + "\n\n2\n00:00:01,000 --> 00:00:02,000\n安全的第二条字幕。\n"
)
CONFIG = {
    "input": {
        "subtitle_time_tolerance_ms": 20,
        "subtitle_video_end_tolerance_seconds": 1.0,
    },
    "subtitle_style": {
        "chinese_font": "Microsoft YaHei",
        "english_font": "Arial",
        "fallback_font": "Arial Unicode MS",
        "chinese_font_size_1080p": 60,
        "english_font_size_1080p": 44,
        "chinese_min_font_size_1080p": 54,
        "english_min_font_size_1080p": 40,
        "one_line_per_language": True,
        "auto_fragment_long_lines": True,
        "minimum_fragment_duration_seconds": 1.0,
        "max_line_width_ratio": 0.92,
        "minimum_horizontal_scale_percent": 92,
        "margin_lr_1080p": 80,
        "margin_v_1080p": 75,
        "max_english_lines": 1,
        "max_chinese_lines": 1,
        "max_combined_lines": 2,
    },
}


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def make_review_task(root: Path) -> tuple[Path, Path, Path]:
    task = root / "task"
    subtitles = task / "subtitles"
    subtitles.mkdir(parents=True)
    english = subtitles / "en.selected.srt"
    chinese = subtitles / "zh.clean.srt"
    english.write_text(ENGLISH, encoding="utf-8")
    chinese.write_text(CHINESE, encoding="utf-8")
    write_json(
        task / "stage4" / "stage4_manifest.json",
        {
            "status": "REVIEW_REQUIRED",
            "qc_status": "REVIEW_REQUIRED",
            "output_mode": "hardsub",
            "chinese_subtitle_source": "deepseek",
            "english_subtitle_path": str(english),
            "chinese_subtitle_path": str(chinese),
            "source_video_probe": {
                "duration": 2.0,
                "width": 1920,
                "height": 1080,
                "display_width": 1920,
                "display_height": 1080,
            },
            "review": {
                "code": "SUBTITLE_LAYOUT_REVIEW_REQUIRED",
                "message": "1 条字幕需要复核",
                "issue_ids": ["1"],
            },
        },
    )
    write_json(
        task / "stage4" / "qc" / "subtitle_qc.json",
        {
            "layout_warnings": [
                {
                    "code": "BILINGUAL_FRAGMENT_DURATION_TOO_SHORT",
                    "id": "1",
                    "seconds_per_event": 0.1,
                }
            ]
        },
    )
    return task, english, chinese


class LayoutReviewTests(unittest.TestCase):
    def test_review_lists_only_affected_cue_and_preserves_original_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task, english, chinese = make_review_task(Path(temporary))
            payload = load_layout_review(task)
            self.assertEqual(payload["review_api_version"], 2)
            self.assertTrue(payload["supports_hide_from_render"])
            self.assertEqual(payload["issue_count"], 1)
            self.assertEqual(payload["rows"][0]["id"], "1")
            self.assertIn("分页后每页显示时间过短", payload["rows"][0]["issue_labels"])
            self.assertEqual(english.read_text(encoding="utf-8"), ENGLISH)
            self.assertEqual(chinese.read_text(encoding="utf-8"), CHINESE)

    def test_unresolved_edit_is_saved_but_remains_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task, english, chinese = make_review_task(Path(temporary))
            payload = save_layout_review(
                task,
                [{"id": "1", "english": "very long english text " * 30, "chinese": "很长的中文字幕" * 40}],
                CONFIG,
            )
            self.assertFalse(payload["ready_to_render"])
            self.assertGreater(payload["remaining_issue_count"], 0)
            self.assertEqual(english.read_text(encoding="utf-8"), ENGLISH)
            self.assertEqual(chinese.read_text(encoding="utf-8"), CHINESE)

    def test_passing_edit_creates_safe_stage4_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task, english, chinese = make_review_task(Path(temporary))
            payload = save_layout_review(
                task,
                [{"id": "1", "english": "Close Blender?", "chinese": "要关闭 Blender 吗？"}],
                CONFIG,
            )
            self.assertTrue(payload["ready_to_render"])
            resolved = ResolvedInputs(
                video_dir=task,
                source_video=task / "video" / "source.mp4",
                source_video_reason="test",
                source_video_candidates=(),
                english_subtitle=english,
                chinese_subtitle=chinese,
                chinese_subtitle_reviewed=False,
            )
            reviewed = apply_layout_review_override(task, resolved)
            self.assertTrue(reviewed.chinese_subtitle_reviewed)
            self.assertEqual(reviewed.english_subtitle.name, "en.layout_reviewed.srt")
            self.assertEqual(reviewed.chinese_subtitle.name, "zh.layout_reviewed.srt")
            self.assertIn("Close Blender?", reviewed.english_subtitle.read_text(encoding="utf-8"))
            self.assertIn("要关闭 Blender 吗？", reviewed.chinese_subtitle.read_text(encoding="utf-8"))
            self.assertEqual(english.read_text(encoding="utf-8"), ENGLISH)
            self.assertEqual(chinese.read_text(encoding="utf-8"), CHINESE)

    def test_user_can_hide_problem_cue_from_render_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task, english, chinese = make_review_task(Path(temporary))
            payload = save_layout_review(
                task,
                [{"id": "1", "hidden_from_render": True}],
                CONFIG,
            )
            self.assertTrue(payload["ready_to_render"])
            self.assertEqual(payload["hidden_count"], 1)
            self.assertTrue(payload["rows"][0]["hidden_from_render"])

            reviewed_english = task / "stage4" / "subtitles" / "en.layout_reviewed.srt"
            reviewed_chinese = task / "stage4" / "subtitles" / "zh.layout_reviewed.srt"
            self.assertNotIn("very long english text", reviewed_english.read_text(encoding="utf-8"))
            self.assertNotIn("很长的中文字幕", reviewed_chinese.read_text(encoding="utf-8"))
            self.assertIn("Safe second cue.", reviewed_english.read_text(encoding="utf-8"))
            self.assertIn("安全的第二条字幕。", reviewed_chinese.read_text(encoding="utf-8"))
            self.assertEqual(english.read_text(encoding="utf-8"), ENGLISH)
            self.assertEqual(chinese.read_text(encoding="utf-8"), CHINESE)

            metadata = json.loads(
                (task / "stage4" / "subtitles" / "layout_review.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["hidden_ids"], ["1"])

    def test_missing_or_unexpected_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task, _, _ = make_review_task(Path(temporary))
            with self.assertRaisesRegex(ValueError, "不在本次复核范围"):
                save_layout_review(
                    task,
                    [{"id": "2", "english": "short", "chinese": "短"}],
                    CONFIG,
                )


if __name__ == "__main__":
    unittest.main()
