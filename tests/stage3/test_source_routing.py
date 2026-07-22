from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase, mock

from src.run_stage3 import load_config
from src.stage3.pipeline import Stage3Pipeline


ROOT = Path(__file__).resolve().parents[2]
VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello world.\n"


def config() -> dict:
    return load_config(ROOT / "config" / "stage3_config.json")


def assessment_with_scores(scores: dict[str, float]):
    def assess(path: Path) -> dict[str, object]:
        return {
            "selected_source": str(path),
            "quality_score": scores[path.name],
            "route": "DIRECT_CLEANING",
        }

    return assess


def fake_asr_for(subtitles: Path):
    def run(*args, **kwargs):
        (subtitles / "en.whisper.clean.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nSpoken.\n",
            encoding="utf-8",
        )
        qc = {
            "status": "QC_PASSED",
            "segment_count": 1,
            "empty_segments": 0,
            "invalid_timestamps": 0,
            "overlaps": 0,
            "adjacent_duplicates": 0,
            "word_timestamp_missing_rate": 0,
            "average_word_probability": 0.9,
        }
        return {
            "status": "ASR_COMPLETED",
            "info": {"segment_count": 1, "word_count": 1},
            "qc": qc,
        }

    return run


class SourceRoutingTests(TestCase):
    def test_auto_selects_manual_when_manual_reaches_configured_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            (subtitles / "en.manual.vtt").write_text(VTT, encoding="utf-8")
            (subtitles / "en.auto.vtt").write_text(VTT, encoding="utf-8")
            scores = {"en.manual.vtt": 70.0, "en.auto.vtt": 100.0}
            with (
                mock.patch("src.stage3.pipeline.assess_source", side_effect=assessment_with_scores(scores)),
                mock.patch("src.stage3.asr_faster_whisper.run_faster_whisper_asr") as asr,
            ):
                result = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            comparison = result["source_comparison"]
            self.assertEqual(comparison["selected_source"], "manual")
            self.assertFalse(comparison["whisper_started"])
            self.assertTrue((subtitles / "en.selected.srt").is_file())
            asr.assert_not_called()

    def test_auto_selects_youtube_when_manual_is_low_and_youtube_reaches_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            (subtitles / "en.manual.vtt").write_text(VTT, encoding="utf-8")
            (subtitles / "en.auto.vtt").write_text(VTT.replace("Hello", "YouTube"), encoding="utf-8")
            scores = {"en.manual.vtt": 69.99, "en.auto.vtt": 65.0}
            with (
                mock.patch("src.stage3.pipeline.assess_source", side_effect=assessment_with_scores(scores)),
                mock.patch("src.stage3.asr_faster_whisper.run_faster_whisper_asr") as asr,
            ):
                result = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            comparison = result["source_comparison"]
            self.assertEqual(comparison["selected_source"], "youtube")
            self.assertFalse(comparison["whisper_started"])
            self.assertIn("YouTube", (subtitles / "en.selected.srt").read_text(encoding="utf-8"))
            asr.assert_not_called()

    def test_auto_falls_back_to_whisper_when_both_subtitle_scores_are_low(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            (subtitles / "en.manual.vtt").write_text(VTT, encoding="utf-8")
            (subtitles / "en.auto.vtt").write_text(VTT, encoding="utf-8")
            scores = {"en.manual.vtt": 69.99, "en.auto.vtt": 64.99}
            with (
                mock.patch("src.stage3.pipeline.assess_source", side_effect=assessment_with_scores(scores)),
                mock.patch(
                    "src.stage3.asr_faster_whisper.run_faster_whisper_asr",
                    side_effect=fake_asr_for(subtitles),
                ) as asr,
            ):
                result = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            comparison = result["source_comparison"]
            self.assertEqual(comparison["selected_source"], "whisper")
            self.assertTrue(comparison["whisper_started"])
            self.assertEqual(comparison["whisper_score"], 100.0)
            self.assertIn("Spoken", (subtitles / "en.selected.srt").read_text(encoding="utf-8"))
            asr.assert_called_once()

    def test_no_subtitles_with_audio_runs_whisper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            audio = video / "audio"
            audio.mkdir()
            (audio / "source_audio.wav").write_bytes(b"mock audio")
            with mock.patch(
                "src.stage3.asr_faster_whisper.run_faster_whisper_asr",
                side_effect=fake_asr_for(subtitles),
            ) as asr:
                result = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            self.assertEqual(result["source_comparison"]["selected_source"], "whisper")
            self.assertTrue(result["source_comparison"]["whisper_started"])
            self.assertTrue((subtitles / "en.selected.srt").is_file())
            asr.assert_called_once()

    def test_no_subtitles_and_no_audio_returns_explicit_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            (video / "subtitles").mkdir()
            result = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            comparison = result["source_comparison"]
            self.assertEqual(result["status"], "NO_AUDIO_SOURCE")
            self.assertFalse(comparison["whisper_started"])
            self.assertEqual(comparison["selected_source"], "")
            self.assertEqual(comparison["selected_path"], "")
            self.assertTrue((video / "stage3" / "source_comparison.json").is_file())
            self.assertFalse((video / "subtitles" / "en.selected.srt").exists())

    def test_auto_thresholds_are_read_from_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            (subtitles / "en.manual.vtt").write_text(VTT, encoding="utf-8")
            custom_config = config()
            custom_config["manual_subtitle_quality_threshold"] = 95
            custom_config["youtube_subtitle_quality_threshold"] = 90
            scores = {"en.manual.vtt": 94.0}
            with (
                mock.patch("src.stage3.pipeline.assess_source", side_effect=assessment_with_scores(scores)),
                mock.patch(
                    "src.stage3.asr_faster_whisper.run_faster_whisper_asr",
                    side_effect=fake_asr_for(subtitles),
                ),
            ):
                result = Stage3Pipeline(video, custom_config).run_p2(subtitle_source="auto")
            comparison = result["source_comparison"]
            self.assertEqual(comparison["selected_source"], "whisper")
            self.assertEqual(comparison["manual_subtitle_quality_threshold"], 95.0)
            self.assertEqual(comparison["youtube_subtitle_quality_threshold"], 90.0)
            self.assertIn("95", comparison["selection_reason"])

    def test_youtube_override_remains_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            (subtitles / "en.manual.vtt").write_text(VTT, encoding="utf-8")
            (subtitles / "en.auto.vtt").write_text(VTT.replace("Hello", "YouTube"), encoding="utf-8")
            result = Stage3Pipeline(video, config()).run_p2(subtitle_source="youtube")
            self.assertEqual(result["source_comparison"]["selected_source"], "youtube")
            self.assertIn("YouTube", (subtitles / "en.selected.srt").read_text(encoding="utf-8"))

    def test_translation_dry_run_prefers_selected_english(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            (subtitles / "en.clean.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nOld.\n",
                encoding="utf-8",
            )
            selected = subtitles / "en.selected.srt"
            selected.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nSelected.\n",
                encoding="utf-8",
            )
            report = Stage3Pipeline(video, config()).run_p1()
            self.assertEqual(Path(report["input"]), selected)
