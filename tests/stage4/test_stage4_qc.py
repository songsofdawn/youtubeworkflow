from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.stage4.quality_control import evaluate_render, write_render_qc


SOURCE = {
    "duration": 10.0,
    "display_width": 1920,
    "display_height": 1080,
    "frame_rate_value": 30.0,
    "video_stream_count": 1,
    "video_codec": "h264",
    "audio_stream_count": 1,
    "audio_streams": [{"codec": "aac"}],
}


class QualityControlTests(unittest.TestCase):
    def output(self) -> dict:
        return {
            **SOURCE,
            "duration": 10.02,
            "subtitle_streams": [
                {
                    "codec": "ass",
                    "tags": {"title": "English / 中文", "language": "mul"},
                    "disposition": {"default": 1},
                }
            ],
        }

    def test_softsub_copy_and_ass_checks_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "out.mkv"
            path.write_bytes(b"x" * 2048)
            report = evaluate_render(SOURCE, self.output(), mode="softsub", output_path=path)
            self.assertEqual(report["qc_status"], "QC_PASSED")
            self.assertTrue(report["checks"]["video_stream_copied"])
            self.assertTrue(report["checks"]["audio_streams_copied"])

    def test_duration_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "out.mkv"
            path.write_bytes(b"x" * 2048)
            output = self.output()
            output["duration"] = 11
            report = evaluate_render(SOURCE, output, mode="softsub", output_path=path)
            self.assertIn("duration_matches", report["failed_checks"])

    def test_missing_default_subtitle_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "out.mkv"
            path.write_bytes(b"x" * 2048)
            output = self.output()
            output["subtitle_streams"][0]["disposition"]["default"] = 0
            report = evaluate_render(SOURCE, output, mode="softsub", output_path=path)
            self.assertIn("subtitle_default", report["failed_checks"])

    def test_qc_json_and_text_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_render_qc(
                root / "render_qc.json",
                root / "render_qc.txt",
                {"softsub": {"qc_status": "QC_PASSED", "checks": {"ok": True}}},
            )
            self.assertTrue((root / "render_qc.json").is_file())
            self.assertIn("PASS ok", (root / "render_qc.txt").read_text(encoding="utf-8"))

