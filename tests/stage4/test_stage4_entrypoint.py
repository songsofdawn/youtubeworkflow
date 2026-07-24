from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.run_stage4 import discover_video_dirs, run_batch
from src.stage4.models import PipelineOptions, PipelineResult, Stage4Error


class FakePipeline:
    def run(self, task_dir: Path, options: PipelineOptions) -> PipelineResult:
        if task_dir.name == "missing":
            raise Stage4Error(
                "EN_SELECTED_SUBTITLE_NOT_FOUND",
                "English subtitle is not ready",
            )
        if task_dir.name == "broken":
            raise Stage4Error("HARDSUB_FFMPEG_FAILED", "FFmpeg failed")
        if task_dir.name == "unexpected":
            raise FileNotFoundError("temporary path was not writable")
        return PipelineResult(
            status="DRY_RUN_COMPLETED" if options.dry_run else "STAGE4_COMPLETED",
            manifest_path=task_dir / "stage4" / "stage4_manifest.json",
            warnings=[],
        )


def make_task(root: Path, name: str) -> Path:
    task = root / name
    task.mkdir()
    (task / "download_manifest.json").write_text("{}", encoding="utf-8")
    return task


class Stage4EntrypointTests(unittest.TestCase):
    def test_discovers_direct_task_without_treating_it_as_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            task = make_task(Path(temporary), "task")
            self.assertEqual(discover_video_dirs(task), [task.resolve()])

    def test_discovers_all_batch_tasks_and_ignores_stage4_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = make_task(root, "001")
            second = make_task(root, "002")
            generated = root / "stage4" / "generated"
            generated.mkdir(parents=True)
            (generated / "download_manifest.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                discover_video_dirs(root),
                [first.resolve(), second.resolve()],
            )

    def test_batch_continues_past_tasks_with_missing_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ready = make_task(root, "ready")
            missing = make_task(root, "missing")
            exit_code, report_path, report = run_batch(
                FakePipeline(),
                root,
                [ready, missing],
                PipelineOptions(mode="hardsub", dry_run=True),
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(
                report["summary"],
                {
                    "input_directory": str(root),
                    "mode": "hardsub",
                    "dry_run": True,
                    "video_task_count": 2,
                    "succeeded": 1,
                    "skipped": 1,
                    "failed": 0,
                },
            )
            saved = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["videos"][1]["status"], "SKIPPED_NOT_READY")

    def test_batch_returns_failure_when_a_ready_task_render_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broken = make_task(root, "broken")
            exit_code, _, report = run_batch(
                FakePipeline(),
                root,
                [broken],
                PipelineOptions(mode="hardsub"),
            )
            self.assertEqual(exit_code, 2)
            self.assertEqual(report["summary"]["failed"], 1)

    def test_batch_continues_after_an_unexpected_single_task_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unexpected = make_task(root, "unexpected")
            ready = make_task(root, "ready")
            exit_code, _, report = run_batch(
                FakePipeline(),
                root,
                [unexpected, ready],
                PipelineOptions(mode="hardsub"),
            )
            self.assertEqual(exit_code, 2)
            self.assertEqual(report["summary"]["succeeded"], 1)
            self.assertEqual(
                report["videos"][0]["error"]["code"],
                "UNEXPECTED_STAGE4_ERROR",
            )


if __name__ == "__main__":
    unittest.main()
