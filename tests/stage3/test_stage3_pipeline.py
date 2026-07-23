from __future__ import annotations

import hashlib
import io
import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import TestCase, mock

from src.run_stage3 import discover_video_dirs, load_config, main
from src.stage3.pipeline import Stage3Pipeline


ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_config(ROOT / "config" / "stage3_config.json")


def prepare_selected(video: Path, content: str) -> Path:
    subtitles = video / "subtitles"
    subtitles.mkdir(parents=True, exist_ok=True)
    selected = subtitles / "en.selected.srt"
    selected.write_text(content, encoding="utf-8")
    selection = video / "stage3" / "selection"
    selection.mkdir(parents=True, exist_ok=True)
    (selection / "selection_report.json").write_text("{}", encoding="utf-8")
    return selected


class Stage3PipelineTests(TestCase):
    def test_batch_directory_discovers_downloaded_video_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("001_first", "002_second"):
                task = root / name; task.mkdir()
                (task / "download_manifest.json").write_text("{}", encoding="utf-8")
            (root / "stage3").mkdir()
            self.assertEqual([item.name for item in discover_video_dirs(root)], ["001_first", "002_second"])

    def test_batch_processing_continues_after_one_video_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "001_first"; second = root / "002_second"
            for task in (first, second):
                (task / "subtitles").mkdir(parents=True)
                (task / "download_manifest.json").write_text("{}", encoding="utf-8")
            prepare_selected(first, "1\n00:00:00,000 --> 00:00:01,000\nHello.\n")
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                exit_code = main(["--video-dir", str(root), "--steps", "translate"])
            self.assertEqual(exit_code, 1)
            self.assertTrue((first / "translation" / "dry_run.json").is_file())
            self.assertIn('"video_task_count": 2', output.getvalue())
            self.assertIn('"failed": 1', output.getvalue())

    def test_clean_then_translate_skips_missing_english_without_aborting_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "001_no_english"; (task / "subtitles").mkdir(parents=True)
            (task / "download_manifest.json").write_text("{}", encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                exit_code = main(["--video-dir", str(task.parent), "--steps", "clean,translate"])
            self.assertEqual(exit_code, 0)
            manifest = json.loads((task / "stage3_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["p0_status"], "NO_YOUTUBE_ENGLISH_SOURCE")

    def test_p0_writes_outputs_qc_and_preserves_source_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"; subtitles.mkdir()
            source = subtitles / "en.auto.vtt"
            source.write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nHello.\n\n00:00:01.000 --> 00:00:02.000\nNext.\n", encoding="utf-8")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            report = Stage3Pipeline(video, CONFIG).run_p0()
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            self.assertTrue((subtitles / "en.clean.srt").is_file())
            self.assertTrue((video / "stage3" / "05_p0_qc.json").is_file())
            self.assertEqual(report["overlaps"], 0)

    def test_missing_english_source_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory); (video / "subtitles").mkdir()
            (video / "subtitles" / "zh.auto.srt").write_text("中文", encoding="utf-8")
            report = Stage3Pipeline(video, CONFIG).run_p0()
            self.assertEqual(report["status"], "NO_YOUTUBE_ENGLISH_SOURCE")

    def test_p1_without_paid_flag_is_dry_run_and_does_not_create_chinese_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory); subtitles = video / "subtitles"; subtitles.mkdir()
            prepare_selected(video, "1\n00:00:00,000 --> 00:00:01,000\nHello.\n")
            report = Stage3Pipeline(video, CONFIG).run_p1(allow_paid_api=False)
            self.assertEqual(report["status"], "DRY_RUN")
            self.assertFalse(report["api_called"])
            self.assertFalse(report["paid_api_enabled"])
            self.assertFalse((subtitles / "zh.clean.srt").exists())
            glossary = json.loads((video / "translation" / "glossary.json").read_text(encoding="utf-8"))
            self.assertEqual(glossary["fixed_terms"]["Roblox"], "Roblox")

    def test_manifest_snapshots_all_original_english_subtitle_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory)
            subtitles = video / "subtitles"
            subtitles.mkdir()
            for name in ("en.manual.srt", "en.auto.vtt"):
                (subtitles / name).write_text("original", encoding="utf-8")
            pipeline = Stage3Pipeline(video, CONFIG)
            hashes = pipeline.manifest["original_subtitle_hashes"]
            self.assertEqual(set(hashes), {
                str(subtitles / "en.manual.srt"),
                str(subtitles / "en.auto.vtt"),
            })

    def test_mocked_paid_run_writes_both_srts_and_polishes_only_failed_qc(self) -> None:
        calls: list[tuple[str, list[int]]] = []

        class FakeTranslator:
            def __init__(self, *args, **kwargs):
                pass

            def translate_all(self, targets, all_segments, glossary, metadata, *, pass_name, force):
                calls.append((pass_name, [item.id for item in targets]))
                if pass_name == "raw":
                    return {1: "你好", 2: "这是一段故意写得非常非常非常非常非常非常非常非常非常非常长的中文字幕"}
                return {item.id: "简短译文" for item in targets}

            def usage_report(self):
                return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "unit-secret"}, clear=False), mock.patch("src.stage3.pipeline.DeepSeekTranslator", FakeTranslator):
            video = Path(directory); subtitles = video / "subtitles"; subtitles.mkdir()
            prepare_selected(video,
                "1\n00:00:00,000 --> 00:00:02,000\nHello.\n\n2\n00:00:02,100 --> 00:00:04,100\nThis is long.\n",
            )
            report = Stage3Pipeline(video, CONFIG).run_p1(allow_paid_api=True)
            self.assertEqual(report["status"], "QC_PASSED")
            self.assertEqual(calls, [("raw", [1, 2]), ("polished", [2])])
            self.assertTrue((subtitles / "zh.raw.srt").is_file())
            self.assertTrue((subtitles / "zh.clean.srt").is_file())
            english = (subtitles / "en.selected.srt").read_text(encoding="utf-8")
            chinese = (subtitles / "zh.clean.srt").read_text(encoding="utf-8")
            self.assertEqual([line for line in english.splitlines() if "-->" in line], [line for line in chinese.splitlines() if "-->" in line])
            self.assertNotIn("unit-secret", "".join(path.read_text(encoding="utf-8") for path in video.rglob("*.json")))

    def test_polish_all_sends_every_id_to_second_pass(self) -> None:
        calls: list[tuple[str, list[int]]] = []

        class FakeTranslator:
            def __init__(self, *args, **kwargs): pass
            def translate_all(self, targets, all_segments, glossary, metadata, *, pass_name, force):
                calls.append((pass_name, [item.id for item in targets]))
                return {item.id: "好" for item in targets}
            def usage_report(self): return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "unit-secret"}, clear=False), mock.patch("src.stage3.pipeline.DeepSeekTranslator", FakeTranslator):
            video = Path(directory); subtitles = video / "subtitles"; subtitles.mkdir()
            prepare_selected(video, "1\n00:00:00,000 --> 00:00:02,000\nHello.\n")
            Stage3Pipeline(video, CONFIG).run_p1(allow_paid_api=True, polish_all=True)
            self.assertEqual(calls, [("raw", [1]), ("polished", [1])])
