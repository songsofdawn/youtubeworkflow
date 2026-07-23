from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from src.run_stage3 import load_config
from src.stage3.subtitle_scoring import score_subtitle, subtitle_agreement
from src.stage3.subtitle_selector import choose_source, write_selection_outputs


ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_config(ROOT / "config" / "stage3_config.json")
SRT = "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n"


class SubtitleScoringTests(TestCase):
    def test_six_dimensions_and_weights_are_explainable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clean.srt"
            path.write_text(SRT, encoding="utf-8")
            report = score_subtitle(
                path, source="youtube", source_type="manual", config=CONFIG,
                audio_duration=2.0, speech_intervals=[(0.0, 2.0)],
            )
            self.assertEqual(
                set(report["scores"]),
                {"structure", "timeline", "coverage", "stability", "readability", "source_confidence"},
            )
            self.assertAlmostEqual(sum(item["weight"] for item in report["scores"].values()), 1.0)
            self.assertTrue(all("raw_values" in item and "deductions" in item for item in report["scores"].values()))

    def test_hard_fail_detects_overlap_and_low_speech_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.srt"
            path.write_text(
                "1\n00:00:00,000 --> 00:00:00,200\nA\n\n"
                "2\n00:00:00,100 --> 00:00:00,300\nB\n",
                encoding="utf-8",
            )
            report = score_subtitle(
                path, source="youtube", source_type="auto", config=CONFIG,
                audio_duration=10.0, speech_intervals=[(0.0, 10.0)],
            )
            self.assertTrue(report["hard_fail"])
            self.assertIn("OVERLAPS_REMAIN", report["hard_fail_reasons"])
            self.assertIn("SPEECH_COVERAGE_BELOW_MINIMUM", report["hard_fail_reasons"])

    def test_structure_uses_original_ids_and_rejects_malformed_timing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.srt"
            path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nFirst.\n\n"
                "1\nnot-a-time --> 00:00:02,000\nSecond.\n",
                encoding="utf-8",
            )
            report = score_subtitle(
                path, source="youtube", source_type="auto", config=CONFIG,
                audio_duration=2.0, speech_intervals=[(0.0, 2.0)],
            )
            structure = report["scores"]["structure"]["raw_values"]
            self.assertEqual(structure["duplicate_ids"], 1)
            self.assertEqual(report["scores"]["timeline"]["raw_values"]["invalid_timestamps"], 1)
            self.assertIn("INVALID_TIMESTAMPS", report["hard_fail_reasons"])

    def test_readability_preserves_original_srt_line_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lines.srt"
            path.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n"
                "This line is deliberately much longer than forty-two characters.\n"
                "Second line.\nThird line.\n",
                encoding="utf-8",
            )
            report = score_subtitle(
                path, source="youtube", source_type="manual", config=CONFIG,
                audio_duration=2.0, speech_intervals=[(0.0, 2.0)],
            )
            readability = report["scores"]["readability"]["raw_values"]
            self.assertEqual(readability["long_lines"], 1)
            self.assertEqual(readability["too_many_lines"], 1)

    def test_agreement_is_recorded_but_not_named_accuracy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left, right = root / "left.srt", root / "right.srt"
            left.write_text(SRT, encoding="utf-8")
            right.write_text(SRT.replace("world", "there"), encoding="utf-8")
            value = subtitle_agreement(left, right)
            self.assertGreater(value, 0)
            self.assertLess(value, 100)

    def test_selector_margin_and_manual_tie_rules(self) -> None:
        youtube = {"path": "youtube.srt", "final_score": 80, "hard_fail": False, "source_type": "auto", "flags": []}
        whisper = {"path": "whisper.srt", "final_score": 78, "hard_fail": False, "source_type": "model", "flags": []}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            youtube["path"] = str(root / "youtube.srt")
            whisper["path"] = str(root / "whisper.srt")
            Path(youtube["path"]).write_text(SRT, encoding="utf-8")
            Path(whisper["path"]).write_text(SRT, encoding="utf-8")
            decision = choose_source(youtube, whisper, mode="auto", minimum_score=70, margin=6)
            self.assertTrue(decision["review_required"])
            youtube["source_type"] = "manual"
            decision = choose_source(youtube, whisper, mode="auto", minimum_score=70, margin=6)
            self.assertEqual(decision["selected_source"], "youtube")

    def test_selector_single_source_margin_low_score_and_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            youtube_path, whisper_path = root / "youtube.srt", root / "whisper.srt"
            youtube_path.write_text(SRT, encoding="utf-8")
            whisper_path.write_text(SRT, encoding="utf-8")
            youtube = {
                "path": str(youtube_path), "final_score": 82, "hard_fail": False,
                "source_type": "auto", "flags": [],
            }
            whisper = {
                "path": str(whisper_path), "final_score": 72, "hard_fail": False,
                "source_type": "model", "flags": [],
            }
            self.assertEqual(
                choose_source(youtube, None, mode="auto", minimum_score=70, margin=6)["selected_source"],
                "youtube",
            )
            self.assertEqual(
                choose_source(youtube, whisper, mode="auto", minimum_score=70, margin=6)["selected_source"],
                "youtube",
            )
            youtube["final_score"] = 60
            whisper["final_score"] = 65
            self.assertTrue(
                choose_source(youtube, whisper, mode="auto", minimum_score=70, margin=6)["review_required"]
            )
            override = choose_source(youtube, whisper, mode="whisper", minimum_score=70, margin=6)
            self.assertEqual(override["selected_source"], "whisper")
            self.assertTrue(override["review_required"])

    def test_selected_output_is_atomic_and_hash_traced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "subtitles" / "en.youtube.clean.srt"
            source.parent.mkdir()
            source.write_text(SRT, encoding="utf-8")
            youtube = {
                "path": str(source), "final_score": 90, "hard_fail": False,
                "source_type": "manual", "flags": [], "scores": {},
            }
            decision = {
                "selected_source": "youtube", "selection_reason": "test",
                "user_override": False, "review_required": False, "warnings": [],
            }
            report = write_selection_outputs(root, youtube, None, decision, agreement_score=0)
            selected = root / "subtitles" / "en.selected.srt"
            self.assertEqual(selected.read_bytes(), source.read_bytes())
            self.assertEqual(report["selected_source_hash"], report["selected_output_hash"])
