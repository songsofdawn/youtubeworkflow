from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.stage4.models import CommandResult, PipelineOptions
from src.stage4.render_pipeline import Stage4Pipeline
from src.stage4.stage4_manifest import sha256_file


SRT_EN = "1\n00:00:00,000 --> 00:00:01,000\nHello.\n"
SRT_ZH = "1\n00:00:00,000 --> 00:00:01,000\n你好。\n"
CONFIG = {
    "input": {
        "english_subtitle": "subtitles/en.selected.srt",
        "chinese_priority": ["subtitles/zh.reviewed.srt", "subtitles/zh.clean.srt"],
        "subtitle_time_tolerance_ms": 20,
    },
    "subtitle_style": {
        "chinese_font": "Microsoft YaHei",
        "english_font": "Arial",
        "fallback_font": "Arial",
        "chinese_font_size_1080p": 42,
        "english_font_size_1080p": 30,
        "outline_1080p": 2.5,
        "shadow_1080p": 0.8,
        "margin_v_1080p": 75,
        "margin_lr_1080p": 80,
        "max_english_lines": 2,
        "max_chinese_lines": 2,
        "max_combined_lines": 4,
    },
    "render": {
        "default_mode": "softsub",
        "video_encoder": "auto",
        "duration_tolerance_seconds": 0.5,
        "preserve_metadata": True,
        "preserve_chapters": True,
        "preserve_existing_subtitle_tracks": True,
    },
}


def source_probe() -> dict:
    return {
        "path": "source.mp4",
        "format_name": "mov,mp4",
        "duration": 2.0,
        "size": 2048,
        "bit_rate": 1000,
        "video_stream_count": 1,
        "video_codec": "h264",
        "width": 1920,
        "height": 1080,
        "display_width": 1920,
        "display_height": 1080,
        "frame_rate": "30/1",
        "frame_rate_value": 30.0,
        "pixel_format": "yuv420p",
        "rotation": 0,
        "audio_stream_count": 1,
        "audio_streams": [{"codec": "aac", "channels": 2, "sample_rate": "48000"}],
        "subtitle_stream_count": 0,
        "subtitle_streams": [],
        "chapters": [],
    }


def output_probe(path: Path) -> dict:
    value = source_probe()
    value["path"] = str(path)
    value["subtitle_stream_count"] = 1
    value["subtitle_streams"] = [
        {
            "codec": "ass",
            "tags": {"title": "English / 中文", "language": "mul"},
            "disposition": {"default": 1},
        }
    ]
    return value


class FakeRunner:
    calls = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def run(self, command: list[str]) -> CommandResult:
        type(self).calls += 1
        Path(command[-1]).write_bytes(b"x" * 4096)
        return CommandResult(command, 0, "", "", "start", "end", 0.01)


class Stage4PipelineTests(unittest.TestCase):
    def prepare(self, root: Path, *, reviewed: bool = False) -> tuple[Stage4Pipeline, Path, Path, Path]:
        tools = root / "tools" / "bin"
        tools.mkdir(parents=True)
        ffmpeg, ffprobe = tools / "ffmpeg.exe", tools / "ffprobe.exe"
        ffmpeg.write_bytes(b"x")
        ffprobe.write_bytes(b"x")
        task = root / "task"
        subtitles = task / "subtitles"
        video_dir = task / "video"
        subtitles.mkdir(parents=True)
        video_dir.mkdir()
        english = subtitles / "en.selected.srt"
        chinese = subtitles / ("zh.reviewed.srt" if reviewed else "zh.clean.srt")
        source = video_dir / "source.mp4"
        english.write_text(SRT_EN, encoding="utf-8")
        chinese.write_text(SRT_ZH, encoding="utf-8")
        source.write_bytes(b"source bytes")
        (task / "download_manifest.json").write_text(
            json.dumps({"output_files": ["video/source.mp4"]}),
            encoding="utf-8",
        )
        return Stage4Pipeline(root, CONFIG, ffmpeg_path=ffmpeg, ffprobe_path=ffprobe), task, source, english

    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch("src.stage4.render_pipeline.resolve_video_encoder", return_value="libx264")
    @mock.patch("src.stage4.render_pipeline.probe_media", side_effect=lambda _, path: source_probe())
    def test_dry_run_validates_but_creates_no_formal_media(
        self, _probe: mock.Mock, _encoder: mock.Mock, _version: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, _, _ = self.prepare(Path(temporary))
            result = pipeline.run(task, PipelineOptions(mode="both", dry_run=True))
            self.assertEqual(result.status, "DRY_RUN_COMPLETED")
            self.assertFalse((task / "stage4" / "subtitles" / "bilingual.ass").exists())
            self.assertFalse((task / "stage4" / "video" / "final_bilingual_softsub.mkv").exists())
            self.assertIn("softsub", result.plan["commands"])
            self.assertIn("hardsub", result.plan["commands"])

    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch("src.stage4.render_pipeline.resolve_video_encoder", return_value="libx264")
    @mock.patch("src.stage4.render_pipeline.probe_media", side_effect=lambda _, path: source_probe())
    def test_dry_run_does_not_overwrite_successful_manifest(
        self, _probe: mock.Mock, _encoder: mock.Mock, _version: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, _, _ = self.prepare(Path(temporary))
            completed = pipeline.run(task, PipelineOptions(mode="ass"))
            before = completed.manifest_path.read_bytes()
            dry = pipeline.run(task, PipelineOptions(mode="both", dry_run=True))
            self.assertEqual(completed.manifest_path.read_bytes(), before)
            self.assertEqual(dry.manifest_path.name, "dry_run_plan.json")

    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch("src.stage4.render_pipeline.probe_media", side_effect=lambda _, path: source_probe())
    def test_ass_generation_preserves_source_hashes(
        self, _probe: mock.Mock, _version: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, source, english = self.prepare(Path(temporary))
            before_source, before_english = sha256_file(source), sha256_file(english)
            result = pipeline.run(task, PipelineOptions(mode="ass"))
            self.assertIn(result.status, {"REVIEW_REQUIRED", "STAGE4_COMPLETED"})
            self.assertTrue((task / "stage4" / "subtitles" / "bilingual.ass").is_file())
            self.assertEqual(sha256_file(source), before_source)
            self.assertEqual(sha256_file(english), before_english)

    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch("src.stage4.render_pipeline.probe_media", side_effect=lambda _, path: source_probe())
    def test_ass_checkpoint_is_reused(self, _probe: mock.Mock, _version: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, _, _ = self.prepare(Path(temporary))
            pipeline.run(task, PipelineOptions(mode="ass"))
            second = pipeline.run(task, PipelineOptions(mode="ass"))
            self.assertTrue(second.plan["ass"]["reused"])

    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch("src.stage4.render_pipeline.probe_media", side_effect=lambda _, path: source_probe())
    def test_force_rebuilds_ass(self, _probe: mock.Mock, _version: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, _, _ = self.prepare(Path(temporary))
            pipeline.run(task, PipelineOptions(mode="ass"))
            second = pipeline.run(task, PipelineOptions(mode="ass", force=True))
            self.assertFalse(second.plan["ass"]["reused"])

    @mock.patch("src.stage4.render_pipeline.FFmpegRunner", FakeRunner)
    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch(
        "src.stage4.render_pipeline.probe_media",
        side_effect=lambda _, path: output_probe(Path(path))
        if "tmp" in Path(path).name or "final_bilingual" in Path(path).name
        else source_probe(),
    )
    def test_softsub_pipeline_writes_manifest_and_qc(
        self, _probe: mock.Mock, _version: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, _, _ = self.prepare(Path(temporary), reviewed=True)
            result = pipeline.run(task, PipelineOptions(mode="softsub", force_softsub=True))
            output = task / "stage4" / "video" / "final_bilingual_softsub.mkv"
            self.assertEqual(result.status, "STAGE4_COMPLETED")
            self.assertTrue(output.is_file())
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["softsub_output_hash"], sha256_file(output))
            self.assertEqual(manifest["qc_status"], "QC_PASSED")
            self.assertTrue((task / "stage4" / "qc" / "render_qc.json").is_file())

    @mock.patch("src.stage4.render_pipeline.resolve_video_encoder", return_value="libx264")
    @mock.patch("src.stage4.render_pipeline.FFmpegRunner", FakeRunner)
    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch(
        "src.stage4.render_pipeline.probe_media",
        side_effect=lambda _, path: output_probe(Path(path))
        if "softsub" in Path(path).name
        else {
            **source_probe(),
            "path": str(path),
            "video_codec": "h264"
            if "hardsub" in Path(path).name
            else source_probe()["video_codec"],
        },
    )
    def test_separate_soft_then_hard_runs_preserve_both_outputs_and_qc(
        self,
        _probe: mock.Mock,
        _version: mock.Mock,
        _encoder: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, _, _ = self.prepare(Path(temporary), reviewed=True)
            pipeline.run(task, PipelineOptions(mode="softsub", force_softsub=True))
            result = pipeline.run(
                task,
                PipelineOptions(mode="hardsub", force_hardsub=True),
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            reports = json.loads(
                (task / "stage4" / "qc" / "render_qc.json").read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["softsub_output_path"])
            self.assertTrue(manifest["hardsub_output_path"])
            self.assertEqual(set(reports), {"softsub", "hardsub"})

    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch("src.stage4.render_pipeline.probe_media", side_effect=lambda _, path: source_probe())
    def test_clean_chinese_is_auto_selected_and_can_complete_without_review(
        self, _probe: mock.Mock, _version: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, _, _ = self.prepare(Path(temporary), reviewed=False)
            result = pipeline.run(task, PipelineOptions(mode="ass"))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "STAGE4_COMPLETED")
            self.assertEqual(manifest["qc_status"], "QC_PASSED")
            self.assertFalse(manifest["chinese_subtitle_reviewed"])
            self.assertTrue(manifest["chinese_subtitle_auto_selected"])
            self.assertTrue(
                (task / "stage4" / "subtitles" / "chinese_selection_report.json").is_file()
            )
