from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from src.run_stage3 import load_config
from src.stage3.pipeline import Stage3Pipeline


ROOT = Path(__file__).resolve().parents[2]
VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello world.\n"


def config() -> dict:
    return load_config(ROOT / "config" / "stage3_config.json")


def fake_asr(subtitles: Path, text: str = "Hello world."):
    def run(*args, **kwargs):
        (subtitles / "en.whisper.clean.srt").write_text(
            f"1\n00:00:00,000 --> 00:00:02,000\n{text}\n",
            encoding="utf-8",
        )
        whisper_dir = subtitles.parent / "stage3" / "whisper"
        whisper_dir.mkdir(parents=True, exist_ok=True)
        (whisper_dir / "raw_segments.json").write_text(
            '[{"id":1,"start":0.0,"end":2.0,"text":"Hello world.","no_speech_prob":0.0}]',
            encoding="utf-8",
        )
        qc = {
            "status": "QC_PASSED", "segment_count": 1, "word_count": 2,
            "empty_segments": 0, "invalid_timestamps": 0, "overlaps": 0,
            "adjacent_duplicates": 0, "word_timestamp_missing_rate": 0,
            "average_word_probability": 0.95, "low_confidence_words": 0,
        }
        (whisper_dir / "qc.json").write_text(__import__("json").dumps(qc), encoding="utf-8")
        info = {
            "audio_duration": 2.0, "segment_count": 1, "word_count": 2,
            "model_path": "local", "device": "cuda", "compute_type": "float16",
        }
        (whisper_dir / "asr_info.json").write_text(__import__("json").dumps(info), encoding="utf-8")
        return {"status": "ASR_COMPLETED", "completed": True, "skipped": False, "info": info, "qc": qc}

    return run


class SourceRoutingTests(TestCase):
    def test_auto_prepares_both_sources_and_prefers_close_manual_youtube(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            (subtitles / "en.manual.vtt").write_text(VTT, encoding="utf-8")
            with mock.patch(
                "src.stage3.asr_faster_whisper.run_faster_whisper_asr",
                side_effect=fake_asr(subtitles),
            ) as asr:
                result = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            report = result["selection_report"]
            self.assertEqual(report["selected_source"], "youtube")
            self.assertEqual(report["youtube"]["source_type"], "manual")
            self.assertIn("scores", report["youtube"])
            self.assertIn("scores", report["whisper"])
            self.assertTrue((video / "stage3" / "selection" / "selection_report.json").is_file())
            self.assertTrue((subtitles / "en.selected.srt").is_file())
            asr.assert_called_once()

    def test_no_youtube_subtitle_with_audio_selects_whisper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            with mock.patch(
                "src.stage3.asr_faster_whisper.run_faster_whisper_asr",
                side_effect=fake_asr(subtitles),
            ):
                result = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            self.assertEqual(result["selection_report"]["selected_source"], "whisper")
            self.assertTrue((subtitles / "en.selected.srt").is_file())

    def test_no_subtitles_and_no_audio_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            (video / "subtitles").mkdir()
            result = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            self.assertEqual(result["status"], "NO_AUDIO_SOURCE")
            self.assertFalse(result["selection_report"]["review_required"])
            self.assertTrue(result["selection_report"]["selection_failed"])
            self.assertFalse((video / "subtitles" / "en.selected.srt").exists())

    def test_user_override_keeps_scoring_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            (subtitles / "en.auto.vtt").write_text(VTT, encoding="utf-8")
            with mock.patch(
                "src.stage3.asr_faster_whisper.run_faster_whisper_asr",
                side_effect=fake_asr(subtitles, "Different words."),
            ):
                result = Stage3Pipeline(video, config()).run_p2(subtitle_source="whisper")
            report = result["selection_report"]
            self.assertEqual(report["selected_source"], "whisper")
            self.assertTrue(report["user_override"])
            self.assertIn("scores", report["youtube"])

    def test_selection_checkpoint_skips_rescoring_completed_video(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            (subtitles / "en.auto.vtt").write_text(VTT, encoding="utf-8")
            with mock.patch(
                "src.stage3.asr_faster_whisper.run_faster_whisper_asr",
                side_effect=fake_asr(subtitles),
            ):
                first = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            self.assertFalse(first["selection_checkpoint_reused"])
            with mock.patch(
                "src.stage3.asr_faster_whisper.run_faster_whisper_asr",
                side_effect=fake_asr(subtitles),
            ), mock.patch(
                "src.stage3.pipeline.score_subtitle",
                side_effect=AssertionError("selection should have resumed"),
            ):
                second = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            self.assertTrue(second["selection_checkpoint_reused"])

    def test_translation_requires_selected_subtitle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            (subtitles / "en.clean.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nLegacy.\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(FileNotFoundError, "EN_SELECTED_SUBTITLE_NOT_FOUND"):
                Stage3Pipeline(video, config()).run_p1()

    def test_translation_dry_run_reads_only_selected_english(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            selected = subtitles / "en.selected.srt"
            selected.write_text("1\n00:00:00,000 --> 00:00:01,000\nSelected.\n", encoding="utf-8")
            selection_dir = video / "stage3" / "selection"
            selection_dir.mkdir(parents=True)
            digest = hashlib.sha256(selected.read_bytes()).hexdigest()
            (selection_dir / "selection_report.json").write_text(
                json.dumps(
                    {
                        "selected_source": "youtube",
                        "selected_input_path": str(selected),
                        "selected_output_path": str(selected),
                        "selected_source_hash": digest,
                        "selected_output_hash": digest,
                    }
                ),
                encoding="utf-8",
            )
            report = Stage3Pipeline(video, config()).run_p1()
            self.assertEqual(Path(report["input"]), selected)
