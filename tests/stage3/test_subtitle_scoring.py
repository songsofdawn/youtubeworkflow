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

    def test_sparse_subtitles_use_time_span_when_speech_intervals_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sparse.srt"
            path.write_text(
                "1\n00:00:10,000 --> 00:00:12,000\nFirst instruction.\n\n"
                "2\n00:01:30,000 --> 00:01:32,000\nFinal instruction.\n",
                encoding="utf-8",
            )
            report = score_subtitle(
                path,
                source="youtube",
                source_type="auto",
                config=CONFIG,
                audio_duration=100.0,
                speech_intervals=[],
            )
            coverage = report["scores"]["coverage"]["raw_values"]
            self.assertEqual(coverage["coverage_basis"], "subtitle_time_span_proxy")
            self.assertAlmostEqual(coverage["coverage_ratio"], 0.82)
            self.assertIn("SPEECH_INTERVALS_UNAVAILABLE", report["flags"])
            self.assertNotIn("SPEECH_COVERAGE_BELOW_MINIMUM", report["hard_fail_reasons"])
            self.assertFalse(report["hard_fail"])

    def test_narrow_subtitle_span_still_fails_without_speech_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "incomplete.srt"
            path.write_text(SRT, encoding="utf-8")
            report = score_subtitle(
                path,
                source="youtube",
                source_type="auto",
                config=CONFIG,
                audio_duration=100.0,
                speech_intervals=[],
            )
            self.assertTrue(report["hard_fail"])
            self.assertIn("SUBTITLE_SPAN_BELOW_MINIMUM", report["hard_fail_reasons"])
            self.assertNotIn("SPEECH_COVERAGE_BELOW_MINIMUM", report["hard_fail_reasons"])

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

    def test_sparse_whisper_activity_is_not_mislabeled_as_low_vad_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "whisper.srt"
            path.write_text(SRT, encoding="utf-8")
            report = score_subtitle(
                path,
                source="whisper",
                source_type="faster-whisper-large-v3",
                config=CONFIG,
                audio_duration=100.0,
                speech_intervals=[(0.0, 2.0)],
                source_qc={
                    "average_word_probability": 0.9,
                    "word_count": 2,
                    "low_confidence_words": 0,
                    "word_timestamp_missing_rate": 0.0,
                    "subtitle_active_coverage_ratio": 0.02,
                },
            )
            confidence = report["scores"]["source_confidence"]
            self.assertEqual(
                confidence["raw_values"]["subtitle_active_coverage_ratio"],
                0.02,
            )
            self.assertNotIn(
                "LOW_VAD_COVERAGE",
                [item["reason"] for item in confidence["deductions"]],
            )

    def test_agreement_is_recorded_but_not_named_accuracy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left, right = root / "left.srt", root / "right.srt"
            left.write_text(SRT, encoding="utf-8")
            right.write_text(SRT.replace("world", "there"), encoding="utf-8")
            value = subtitle_agreement(left, right)
            self.assertGreater(value, 0)
            self.assertLess(value, 100)

    def test_selector_close_scores_use_automatic_coverage_tiebreak(self) -> None:
        youtube = {
            "path": "youtube.srt", "final_score": 80, "hard_fail": False,
            "source_type": "auto", "flags": [],
            "scores": {
                "coverage": {
                    "normalized_score": 95,
                    "raw_values": {"maximum_uncovered_speech_seconds": 2},
                },
                "timeline": {"normalized_score": 90},
                "readability": {"normalized_score": 70},
            },
        }
        whisper = {
            "path": "whisper.srt", "final_score": 81, "hard_fail": False,
            "source_type": "model", "flags": [],
            "scores": {
                "coverage": {
                    "normalized_score": 86,
                    "raw_values": {"maximum_uncovered_speech_seconds": 7},
                },
                "timeline": {"normalized_score": 95},
                "readability": {"normalized_score": 80},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            youtube["path"] = str(root / "youtube.srt")
            whisper["path"] = str(root / "whisper.srt")
            Path(youtube["path"]).write_text(SRT, encoding="utf-8")
            Path(whisper["path"]).write_text(SRT, encoding="utf-8")
            decision = choose_source(
                youtube, whisper, mode="auto", minimum_score=70, margin=6,
                automatic_tiebreak=CONFIG["automatic_tiebreak"],
            )
            self.assertFalse(decision["review_required"])
            self.assertEqual(decision["selected_source"], "youtube")
            self.assertIn("AUTO_TIEBREAK_COVERAGE", decision["warnings"])
            youtube["source_type"] = "manual"
            decision = choose_source(
                youtube, whisper, mode="auto", minimum_score=70, margin=6,
                automatic_tiebreak=CONFIG["automatic_tiebreak"],
            )
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
            below_minimum = choose_source(
                youtube, whisper, mode="auto", minimum_score=70, margin=6,
                automatic_tiebreak=CONFIG["automatic_tiebreak"],
            )
            self.assertFalse(below_minimum["review_required"])
            self.assertEqual(below_minimum["selected_source"], "whisper")
            self.assertIn("AUTO_SELECTED_BELOW_MINIMUM", below_minimum["warnings"])
            override = choose_source(youtube, whisper, mode="whisper", minimum_score=70, margin=6)
            self.assertEqual(override["selected_source"], "whisper")
            self.assertFalse(override["review_required"])

    def test_selector_stops_only_when_every_source_hard_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            youtube_path = root / "youtube.srt"
            whisper_path = root / "whisper.srt"
            youtube_path.write_text(SRT, encoding="utf-8")
            whisper_path.write_text(SRT, encoding="utf-8")
            youtube = {
                "path": str(youtube_path), "final_score": 40,
                "hard_fail": True, "source_type": "auto", "flags": [],
            }
            whisper = {
                "path": str(whisper_path), "final_score": 50,
                "hard_fail": True, "source_type": "model", "flags": [],
            }
            decision = choose_source(
                youtube, whisper, mode="auto", minimum_score=70, margin=6,
                automatic_tiebreak=CONFIG["automatic_tiebreak"],
            )
            self.assertEqual(decision["selected_source"], "")
            self.assertTrue(decision["selection_failed"])
            self.assertFalse(decision["review_required"])

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
