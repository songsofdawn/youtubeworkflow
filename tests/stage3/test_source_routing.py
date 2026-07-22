from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from src.run_stage3 import load_config
from src.stage3.pipeline import Stage3Pipeline


ROOT = Path(__file__).resolve().parents[2]


def config() -> dict:
    return load_config(ROOT / "config" / "stage3_config.json")


VTT = "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello world.\n"


class SourceRoutingTests(TestCase):
    def test_auto_prefers_qualifying_manual_subtitle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory); subtitles = video / "subtitles"; subtitles.mkdir()
            (subtitles / "en.manual.vtt").write_text(VTT, encoding="utf-8")
            (subtitles / "en.auto.vtt").write_text(VTT, encoding="utf-8")
            result = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            self.assertEqual(result["source_comparison"]["selected_source"], "manual")
            self.assertTrue((subtitles / "en.selected.srt").is_file())

    def test_youtube_override_selects_youtube_even_when_manual_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory); subtitles = video / "subtitles"; subtitles.mkdir()
            (subtitles / "en.manual.vtt").write_text(VTT, encoding="utf-8")
            (subtitles / "en.auto.vtt").write_text(VTT.replace("Hello", "YouTube"), encoding="utf-8")
            result = Stage3Pipeline(video, config()).run_p2(subtitle_source="youtube")
            self.assertEqual(result["source_comparison"]["selected_source"], "youtube")
            self.assertIn("YouTube", (subtitles / "en.selected.srt").read_text(encoding="utf-8"))

    def test_auto_falls_back_to_mocked_whisper_for_low_quality_youtube(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory); subtitles = video / "subtitles"; subtitles.mkdir()
            rolling = "WEBVTT\n\n" + "\n\n".join(f"00:00:00.00{i} --> 00:00:00.01{i}\nSame" for i in range(5)) + "\n"
            (subtitles / "en.auto.vtt").write_text(rolling, encoding="utf-8")

            def fake_asr(*args, **kwargs):
                (subtitles / "en.whisper.clean.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nSpoken.\n", encoding="utf-8")
                qc = {"status": "QC_PASSED", "segment_count": 1, "empty_segments": 0, "invalid_timestamps": 0, "overlaps": 0, "adjacent_duplicates": 0, "word_timestamp_missing_rate": 0, "average_word_probability": 0.9}
                return {"status": "ASR_COMPLETED", "info": {"segment_count": 1, "word_count": 1}, "qc": qc}

            with mock.patch("src.stage3.asr_faster_whisper.run_faster_whisper_asr", side_effect=fake_asr):
                result = Stage3Pipeline(video, config()).run_p2(subtitle_source="auto")
            self.assertEqual(result["source_comparison"]["selected_source"], "whisper")
            self.assertIn("Spoken", (subtitles / "en.selected.srt").read_text(encoding="utf-8"))

    def test_translation_dry_run_prefers_selected_english(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory); subtitles = video / "subtitles"; subtitles.mkdir()
            (subtitles / "en.clean.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nOld.\n", encoding="utf-8")
            selected = subtitles / "en.selected.srt"
            selected.write_text("1\n00:00:00,000 --> 00:00:01,000\nSelected.\n", encoding="utf-8")
            report = Stage3Pipeline(video, config()).run_p1()
            self.assertEqual(Path(report["input"]), selected)
