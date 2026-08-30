from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from src.stage4.layout_review import save_layout_review
from src.stage4.models import CommandResult, PipelineOptions, ResolvedInputs
from src.stage4.render_pipeline import Stage4Pipeline
from src.stage4.stage4_manifest import hash_json, sha256_file
from src.stage4.subtitle_recovery import clip_recovered_pair_to_video_duration


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
    def setUp(self) -> None:
        FakeRunner.calls = 0

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

    @mock.patch("src.stage4.render_pipeline.FFmpegRunner", FakeRunner)
    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch("src.stage4.render_pipeline.probe_media", side_effect=lambda _, path: source_probe())
    def test_unfit_single_line_stops_before_ffmpeg_and_explains_review(
        self, _probe: mock.Mock, _version: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, _, _ = self.prepare(Path(temporary))
            (task / "subtitles" / "zh.clean.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n" + "异常内容" * 100 + "\n",
                encoding="utf-8",
            )
            result = pipeline.run(task, PipelineOptions(mode="hardsub"))
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(result.status, "REVIEW_REQUIRED")
            self.assertEqual(FakeRunner.calls, 0)
            self.assertTrue(manifest["review"]["render_blocked_before_ffmpeg"])
            self.assertEqual(manifest["review"]["issue_ids"], ["1"])
            self.assertFalse(
                (task / "stage4" / "video" / "final_bilingual_hardsub.mp4").exists()
            )

    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch("src.stage4.render_pipeline.probe_media", side_effect=lambda _, path: source_probe())
    def test_layout_review_survives_recovered_subtitle_regeneration_before_clip(
        self, _probe: mock.Mock, _version: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline, task, source, _ = self.prepare(root)
            recovered_dir = task / "stage4" / "subtitles"
            recovered_dir.mkdir(parents=True, exist_ok=True)
            english = recovered_dir / "en.recovered.srt"
            chinese = recovered_dir / "zh.recovered.srt"
            full_english = (
                "1\n00:00:00,000 --> 00:00:01,000\n"
                + "very long english text " * 30
                + "\n\n2\n00:00:01,000 --> 00:00:01,500\n"
                + "another long english text " * 20
                + "\n"
                + "\n3\n00:00:02,100 --> 00:00:03,000\nOutside media.\n"
            )
            full_chinese = (
                "1\n00:00:00,000 --> 00:00:01,000\n"
                + "很长的中文字幕" * 40
                + "\n\n2\n00:00:01,000 --> 00:00:01,500\n"
                + "另一条很长的中文字幕" * 20
                + "\n"
                + "\n3\n00:00:02,100 --> 00:00:03,000\n超出视频。\n"
            )
            english.write_text(full_english, encoding="utf-8")
            chinese.write_text(full_chinese, encoding="utf-8")
            clip_recovered_pair_to_video_duration(english, chinese, 2.0)
            (task / "stage4" / "stage4_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "REVIEW_REQUIRED",
                        "qc_status": "REVIEW_REQUIRED",
                        "output_mode": "ass",
                        "chinese_subtitle_source": "youtube_auto",
                        "english_subtitle_path": str(english),
                        "chinese_subtitle_path": str(chinese),
                        "source_video_probe": source_probe(),
                        "review": {
                            "code": "SUBTITLE_LAYOUT_REVIEW_REQUIRED",
                            "message": "2 条字幕需要复核",
                            "issue_ids": ["1", "2"],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            qc = task / "stage4" / "qc" / "subtitle_qc.json"
            qc.parent.mkdir(parents=True, exist_ok=True)
            qc.write_text(
                json.dumps(
                    {
                        "layout_warnings": [
                            {"code": "BILINGUAL_LINE_TOO_WIDE", "id": "1"},
                            {"code": "BILINGUAL_LINE_TOO_WIDE", "id": "2"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            review = save_layout_review(
                task,
                [
                    {"id": "1", "hidden_from_render": True},
                    {"id": "2", "english": "Edited cue.", "chinese": "已修改字幕。"},
                ],
                CONFIG,
            )
            self.assertEqual(review["hidden_count"], 1)

            # Automatic recovery regenerates the un-clipped pair on every run.
            english.write_text(full_english, encoding="utf-8")
            chinese.write_text(full_chinese, encoding="utf-8")
            recovered = ResolvedInputs(
                video_dir=task,
                source_video=source,
                source_video_reason="test",
                source_video_candidates=(source,),
                english_subtitle=english,
                chinese_subtitle=chinese,
                chinese_subtitle_reviewed=False,
                chinese_subtitle_auto_selected=True,
                chinese_subtitle_selection_reason="recovered",
                chinese_subtitle_selection_score=90.0,
                chinese_selection_report={
                    "selection_mode": "auto_recovered_aligned_bilingual"
                },
            )
            with mock.patch(
                "src.stage4.render_pipeline.resolve_inputs", return_value=recovered
            ):
                result = pipeline.run(task, PipelineOptions(mode="ass"))

            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            selection = json.loads(
                (task / "stage4" / "subtitles" / "chinese_selection_report.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(result.status, "STAGE4_COMPLETED")
            self.assertTrue(manifest["english_subtitle_path"].endswith("en.layout_reviewed.srt"))
            self.assertTrue(manifest["chinese_subtitle_path"].endswith("zh.layout_reviewed.srt"))
            self.assertEqual(selection["selection_mode"], "layout_reviewed")
            self.assertEqual(selection["layout_review_hidden_ids"], ["1"])
            reviewed_english = Path(manifest["english_subtitle_path"]).read_text(
                encoding="utf-8"
            )
            reviewed_chinese = Path(manifest["chinese_subtitle_path"]).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("very long english text", reviewed_english)
            self.assertIn("Edited cue.", reviewed_english)
            self.assertIn("已修改字幕。", reviewed_chinese)

    @mock.patch("src.stage4.render_pipeline.resolve_video_encoder", return_value="libx264")
    @mock.patch("src.stage4.render_pipeline.FFmpegRunner", FakeRunner)
    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch(
        "src.stage4.render_pipeline.probe_media",
        side_effect=lambda _, path: {
            **source_probe(),
            "path": str(path),
            "video_codec": "h264"
            if "hardsub" in Path(path).name
            else source_probe()["video_codec"],
        },
    )
    def test_readable_long_line_is_fragmented_and_rendered(
        self,
        _probe: mock.Mock,
        _version: mock.Mock,
        _encoder: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, _, _ = self.prepare(Path(temporary))
            (task / "subtitles" / "en.selected.srt").write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nA concise source.\n",
                encoding="utf-8",
            )
            (task / "subtitles" / "zh.clean.srt").write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n" + "可读的中文分页内容" * 10 + "\n",
                encoding="utf-8",
            )

            result = pipeline.run(task, PipelineOptions(mode="hardsub"))
            subtitle_qc = json.loads(
                (task / "stage4" / "qc" / "subtitle_qc.json").read_text(
                    encoding="utf-8"
                )
            )
            summary = subtitle_qc["scaled_style"]["adaptive_font_size_summary"]

            self.assertEqual(result.status, "STAGE4_COMPLETED")
            self.assertEqual(FakeRunner.calls, 1)
            self.assertEqual(subtitle_qc["layout_warnings"], [])
            self.assertEqual(summary["fragmented_segment_count"], 1)
            self.assertGreater(summary["generated_event_count"], 1)

    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch("src.stage4.render_pipeline.resolve_video_encoder", return_value="libx264")
    @mock.patch("src.stage4.render_pipeline.probe_media", side_effect=lambda _, path: source_probe())
    def test_hardsub_filter_uses_safe_relative_ass_path_for_apostrophe_task(
        self,
        _probe: mock.Mock,
        _encoder: mock.Mock,
        _version: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, _, _ = self.prepare(Path(temporary))
            renamed = task.rename(task.with_name("task's title"))
            result = pipeline.run(
                renamed,
                PipelineOptions(mode="hardsub", dry_run=True),
            )
            command = result.plan["commands"]["hardsub"]
            filter_value = command[command.index("-vf") + 1]
            self.assertEqual(filter_value, "ass=filename='bilingual.ass'")
            self.assertEqual(
                result.plan["ffmpeg_working_directory"],
                str(renamed / "stage4" / "subtitles"),
            )

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

    @mock.patch("src.stage4.render_pipeline.resolve_video_encoder", return_value="libx264")
    @mock.patch("src.stage4.render_pipeline.FFmpegRunner", FakeRunner)
    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch(
        "src.stage4.render_pipeline.probe_media",
        side_effect=lambda _, path: {
            **source_probe(),
            "path": str(path),
            "video_codec": "h264"
            if "hardsub" in Path(path).name
            else source_probe()["video_codec"],
        },
    )
    def test_resume_reuses_hardsub_when_only_input_recovery_config_changes(
        self,
        _probe: mock.Mock,
        _version: mock.Mock,
        _encoder: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline, task, _, _ = self.prepare(root, reviewed=True)
            pipeline.run(task, PipelineOptions(mode="hardsub"))
            self.assertEqual(FakeRunner.calls, 1)

            changed_config = deepcopy(CONFIG)
            changed_config["input"]["automatic_recovery"] = {
                "enabled": True,
                "allow_timing_repair": True,
            }
            resumed = Stage4Pipeline(
                root,
                changed_config,
                ffmpeg_path=pipeline.ffmpeg_path,
                ffprobe_path=pipeline.ffprobe_path,
            ).run(task, PipelineOptions(mode="hardsub"))

            self.assertEqual(FakeRunner.calls, 1)
            self.assertTrue(resumed.plan["hardsub"]["reused"])
            self.assertTrue(resumed.plan["resume"]["hardsub_checkpoint_valid"])

    @mock.patch("src.stage4.render_pipeline.resolve_video_encoder", return_value="libx264")
    @mock.patch("src.stage4.render_pipeline.FFmpegRunner", FakeRunner)
    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch(
        "src.stage4.render_pipeline.probe_media",
        side_effect=lambda _, path: {
            **source_probe(),
            "path": str(path),
            "video_codec": "h264"
            if "hardsub" in Path(path).name
            else source_probe()["video_codec"],
        },
    )
    def test_resume_rerenders_hardsub_when_render_quality_changes(
        self,
        _probe: mock.Mock,
        _version: mock.Mock,
        _encoder: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline, task, _, _ = self.prepare(root, reviewed=True)
            pipeline.run(task, PipelineOptions(mode="hardsub"))

            changed_config = deepcopy(CONFIG)
            changed_config["render"]["x264_crf"] = 20
            rerun = Stage4Pipeline(
                root,
                changed_config,
                ffmpeg_path=pipeline.ffmpeg_path,
                ffprobe_path=pipeline.ffprobe_path,
            ).run(task, PipelineOptions(mode="hardsub"))

            self.assertEqual(FakeRunner.calls, 2)
            self.assertFalse(rerun.plan["hardsub"]["reused"])

    @mock.patch("src.stage4.render_pipeline.resolve_video_encoder", return_value="libx264")
    @mock.patch("src.stage4.render_pipeline.FFmpegRunner", FakeRunner)
    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch(
        "src.stage4.render_pipeline.probe_media",
        side_effect=lambda _, path: {
            **source_probe(),
            "path": str(path),
            "video_codec": "h264"
            if "hardsub" in Path(path).name
            else source_probe()["video_codec"],
        },
    )
    def test_larger_subtitle_style_regenerates_ass_and_overwrites_hardsub(
        self,
        _probe: mock.Mock,
        _version: mock.Mock,
        _encoder: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline, task, _, _ = self.prepare(root, reviewed=True)
            first = pipeline.run(task, PipelineOptions(mode="hardsub"))
            old_ass = (
                task / "stage4" / "subtitles" / "bilingual.ass"
            ).read_text(encoding="utf-8")
            old_checkpoint = json.loads(
                first.manifest_path.read_text(encoding="utf-8")
            )["checkpoints"]["hardsub"]["fingerprint"]

            changed_config = deepcopy(CONFIG)
            changed_config["subtitle_style"].update(
                {
                    "chinese_font_size_1080p": 48,
                    "english_font_size_1080p": 34,
                    "chinese_min_font_size_1080p": 34,
                    "english_min_font_size_1080p": 25,
                }
            )
            rerun = Stage4Pipeline(
                root,
                changed_config,
                ffmpeg_path=pipeline.ffmpeg_path,
                ffprobe_path=pipeline.ffprobe_path,
            ).run(task, PipelineOptions(mode="hardsub"))
            new_ass = (
                task / "stage4" / "subtitles" / "bilingual.ass"
            ).read_text(encoding="utf-8")
            new_checkpoint = json.loads(
                rerun.manifest_path.read_text(encoding="utf-8")
            )["checkpoints"]["hardsub"]["fingerprint"]

            self.assertEqual(FakeRunner.calls, 2)
            self.assertFalse(rerun.plan["ass"]["reused"])
            self.assertFalse(rerun.plan["hardsub"]["reused"])
            self.assertNotEqual(old_ass, new_ass)
            self.assertNotEqual(old_checkpoint, new_checkpoint)

    @mock.patch("src.stage4.render_pipeline.resolve_video_encoder", return_value="libx264")
    @mock.patch("src.stage4.render_pipeline.FFmpegRunner", FakeRunner)
    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch(
        "src.stage4.render_pipeline.probe_media",
        side_effect=lambda _, path: {
            **source_probe(),
            "path": str(path),
            "video_codec": "h264"
            if "hardsub" in Path(path).name
            else source_probe()["video_codec"],
        },
    )
    def test_resume_migrates_legacy_checkpoint_without_reencoding(
        self,
        _probe: mock.Mock,
        _version: mock.Mock,
        _encoder: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipeline, task, _, _ = self.prepare(root, reviewed=True)
            first = pipeline.run(task, PipelineOptions(mode="hardsub"))
            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            legacy_fingerprint = hash_json(
                {
                    "source": manifest["source_video_hash"],
                    "ass": manifest["bilingual_ass_hash"],
                    "config": manifest["config_hash"],
                    "encoder": "libx264",
                    "audio_mode": "copy",
                    "kind": "hardsub",
                }
            )
            manifest["checkpoints"]["hardsub"]["fingerprint"] = legacy_fingerprint
            first.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            changed_config = deepcopy(CONFIG)
            changed_config["input"]["automatic_recovery"] = {"enabled": True}
            resumed = Stage4Pipeline(
                root,
                changed_config,
                ffmpeg_path=pipeline.ffmpeg_path,
                ffprobe_path=pipeline.ffprobe_path,
            ).run(task, PipelineOptions(mode="hardsub"))
            migrated = json.loads(resumed.manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(FakeRunner.calls, 1)
            self.assertTrue(resumed.plan["hardsub"]["reused"])
            self.assertTrue(resumed.plan["hardsub"]["checkpoint_migrated"])
            self.assertEqual(
                migrated["checkpoints"]["hardsub"]["migrated_from_fingerprint"],
                legacy_fingerprint,
            )

    @mock.patch("src.stage4.render_pipeline.resolve_video_encoder", return_value="libx264")
    @mock.patch("src.stage4.render_pipeline.FFmpegRunner", FakeRunner)
    @mock.patch("src.stage4.render_pipeline.tool_version", return_value="test")
    @mock.patch(
        "src.stage4.render_pipeline.probe_media",
        side_effect=lambda _, path: {
            **source_probe(),
            "path": str(path),
            "video_codec": "h264"
            if "hardsub" in Path(path).name
            else source_probe()["video_codec"],
        },
    )
    def test_resume_rerenders_failed_checkpoint(
        self,
        _probe: mock.Mock,
        _version: mock.Mock,
        _encoder: mock.Mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipeline, task, _, _ = self.prepare(Path(temporary), reviewed=True)
            first = pipeline.run(task, PipelineOptions(mode="hardsub"))
            manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
            manifest["checkpoints"]["hardsub"]["qc_status"] = "QC_FAILED"
            first.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            rerun = pipeline.run(task, PipelineOptions(mode="hardsub"))

            self.assertEqual(FakeRunner.calls, 2)
            self.assertFalse(rerun.plan["hardsub"]["reused"])

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
