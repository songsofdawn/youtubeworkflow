from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.stage4.input_resolver import resolve_inputs
from src.stage4.models import Stage4Error


CONFIG = {
    "input": {
        "english_subtitle": "subtitles/en.selected.srt",
        "chinese_priority": [
            "subtitles/zh.reviewed.srt",
            "subtitles/zh.clean.srt",
        ],
    }
}
ENGLISH_SRT = "1\n00:00:00,000 --> 00:00:01,000\nHello world.\n"
CHINESE_SRT = "1\n00:00:00,000 --> 00:00:01,000\n你好，世界。\n"
RECOVERY_ENGLISH_SRT = """1
00:00:00,000 --> 00:00:01,000
Hello world.

2
00:00:01,500 --> 00:00:02,500
Second sentence.
"""
RECOVERY_CHINESE_SRT = """1
00:00:00,050 --> 00:00:00,950
你好，世界。

2
00:00:01,550 --> 00:00:02,450
第二句话。
"""


class InputResolverTests(unittest.TestCase):
    def prepare(self, root: Path) -> Path:
        subtitles = root / "subtitles"
        subtitles.mkdir()
        (subtitles / "en.selected.srt").write_text(ENGLISH_SRT, encoding="utf-8")
        (subtitles / "zh.clean.srt").write_text(CHINESE_SRT, encoding="utf-8")
        video = root / "video" / "source.mp4"
        video.parent.mkdir()
        video.write_bytes(b"video")
        (root / "download_manifest.json").write_text(
            json.dumps({"output_files": ["video\\source.mp4"]}),
            encoding="utf-8",
        )
        return video

    def test_finds_only_selected_english(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = self.prepare(root)
            result = resolve_inputs(root, CONFIG)
            self.assertEqual(result.english_subtitle.name, "en.selected.srt")
            self.assertEqual(result.source_video, video.resolve())
            self.assertTrue(result.chinese_subtitle_auto_selected)
            self.assertIsNotNone(result.chinese_subtitle_selection_score)

    def test_reviewed_has_priority_over_clean(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            reviewed = root / "subtitles" / "zh.reviewed.srt"
            reviewed.write_text(CHINESE_SRT, encoding="utf-8")
            result = resolve_inputs(root, CONFIG)
            self.assertEqual(result.chinese_subtitle, reviewed.resolve())
            self.assertTrue(result.chinese_subtitle_reviewed)
            self.assertFalse(result.chinese_subtitle_auto_selected)

    def test_require_reviewed_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            with self.assertRaises(Stage4Error) as caught:
                resolve_inputs(root, CONFIG, require_reviewed=True)
            self.assertEqual(caught.exception.code, "ZH_REVIEWED_SUBTITLE_NOT_FOUND")

    def test_missing_english_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            (root / "subtitles" / "en.selected.srt").unlink()
            (root / "subtitles" / "en.auto.srt").write_text("x", encoding="utf-8")
            with self.assertRaises(Stage4Error) as caught:
                resolve_inputs(root, CONFIG)
            self.assertEqual(caught.exception.code, "EN_SELECTED_SUBTITLE_NOT_FOUND")

    def test_missing_chinese_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            (root / "subtitles" / "zh.clean.srt").unlink()
            with self.assertRaises(Stage4Error) as caught:
                resolve_inputs(root, CONFIG)
            self.assertEqual(caught.exception.code, "CHINESE_SUBTITLE_NOT_FOUND")

    def test_manifest_video_beats_unrelated_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.prepare(root)
            (root / "other.webm").write_bytes(b"other")
            result = resolve_inputs(root, CONFIG)
            self.assertEqual(result.source_video, source.resolve())

    def test_auto_scoring_prefers_clean_chinese_over_english_leaking_raw(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            raw = root / "subtitles" / "zh.raw.srt"
            raw.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nThis is untranslated text.\n",
                encoding="utf-8",
            )
            config = {
                "input": {
                    **CONFIG["input"],
                    "chinese_auto_candidates": [
                        "subtitles/zh.raw.srt",
                        "subtitles/zh.clean.srt",
                    ],
                }
            }
            result = resolve_inputs(root, config)
            self.assertEqual(result.chinese_subtitle.name, "zh.clean.srt")
            records = result.chinese_selection_report["candidates"]
            scores = {Path(item["path"]).name: item["score"] for item in records}
            self.assertGreater(scores["zh.clean.srt"], scores["zh.raw.srt"])

    def test_auto_candidate_with_mismatched_timeline_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            auto = root / "subtitles" / "zh.auto.srt"
            auto.write_text(
                "1\n00:00:00,100 --> 00:00:01,000\n自动字幕。\n",
                encoding="utf-8",
            )
            config = {
                "input": {
                    **CONFIG["input"],
                    "chinese_auto_candidates": [
                        "subtitles/zh.auto.srt",
                        "subtitles/zh.clean.srt",
                    ],
                }
            }
            result = resolve_inputs(root, config)
            rejected = next(
                item
                for item in result.chinese_selection_report["candidates"]
                if Path(item["path"]).name == "zh.auto.srt"
            )
            self.assertFalse(rejected["eligible"])
            self.assertEqual(rejected["rejection_reason"], "SUBTITLE_TIMELINE_MISMATCH")
            self.assertIn("download_manifest", result.source_video_reason)

    def test_mismatched_sources_are_recovered_from_independent_auto_tracks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            subtitles = root / "subtitles"
            (subtitles / "zh.clean.srt").write_text(
                RECOVERY_CHINESE_SRT,
                encoding="utf-8",
            )
            (subtitles / "en.auto.srt").write_text(
                RECOVERY_ENGLISH_SRT,
                encoding="utf-8",
            )
            (subtitles / "zh.auto.srt").write_text(
                RECOVERY_CHINESE_SRT,
                encoding="utf-8",
            )
            config = {
                "input": {
                    **CONFIG["input"],
                    "english_recovery_candidates": ["subtitles/en.auto.srt"],
                    "chinese_recovery_candidates": ["subtitles/zh.auto.srt"],
                    "auto_recover_min_pair_ratio": 0.85,
                    "auto_recover_min_cjk_ratio": 0.5,
                }
            }
            result = resolve_inputs(root, config)
            self.assertEqual(result.english_subtitle.name, "en.recovered.srt")
            self.assertEqual(result.chinese_subtitle.name, "zh.recovered.srt")
            self.assertEqual(
                result.chinese_selection_report["selection_mode"],
                "auto_recovered_aligned_bilingual",
            )
            self.assertEqual(result.chinese_selection_report["pair_ratio"], 1.0)

    def test_ambiguous_directory_scan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.prepare(root)
            (root / "download_manifest.json").unlink()
            (root / "other.webm").write_bytes(b"other")
            with self.assertRaises(Stage4Error) as caught:
                resolve_inputs(root, CONFIG)
            self.assertEqual(caught.exception.code, "AMBIGUOUS_SOURCE_VIDEO")
            self.assertEqual(len(caught.exception.details["candidates"]), 2)

    def test_stage4_and_final_videos_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.prepare(root)
            (root / "download_manifest.json").unlink()
            generated = root / "stage4" / "video"
            generated.mkdir(parents=True)
            (generated / "final_bilingual_softsub.mkv").write_bytes(b"generated")
            (root / "preview.mp4").write_bytes(b"preview")
            result = resolve_inputs(root, CONFIG)
            self.assertEqual(result.source_video, source.resolve())
