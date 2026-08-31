from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.control_panel.jobs import WorkflowWorker
from src.control_panel.tasks import WorkflowScanner


class ScannerDubbingTests(unittest.TestCase):
    def test_old_task_keeps_original_progress_when_dubbing_was_never_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "downloads" / "task"
            task.mkdir(parents=True)
            (task / "download_manifest.json").write_text(
                json.dumps({"overall_status": "success", "video_id": "abc"}),
                encoding="utf-8",
            )
            row = WorkflowScanner(root).scan()[0]
        self.assertEqual(row["progress"], 20)
        self.assertEqual(row["stages"]["dubbing"]["state"], "skipped")
        self.assertFalse(row["dubbing_available"])

    def test_dubbing_manifest_is_exposed_as_independent_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "downloads" / "task"
            (task / "dubbing").mkdir(parents=True)
            (task / "download_manifest.json").write_text(
                json.dumps({"overall_status": "success", "video_id": "abc"}),
                encoding="utf-8",
            )
            (task / "dubbing" / "manifest.json").write_text(
                json.dumps({"status": "COMPLETED_WITH_REVIEW", "errors": []}),
                encoding="utf-8",
            )
            (task / "dubbing" / "dubbed_audio.wav").write_bytes(b"0" * 100)
            row = WorkflowScanner(root).scan()[0]
        self.assertEqual(row["stages"]["dubbing"]["state"], "review")
        self.assertTrue(row["dubbing_available"])
        self.assertTrue(row["dubbing_audio_ready"])


class WorkerDubbingCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.task = self.root / "downloads" / "task"
        self.task.mkdir(parents=True)
        self.python = self.root / "python.exe"
        self.python.write_bytes(b"python")
        scanner = mock.Mock()
        scanner.resolve_task.return_value = self.task
        self.worker = WorkflowWorker.__new__(WorkflowWorker)
        self.worker.project_root = self.root
        self.worker.scanner = scanner

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @mock.patch("src.control_panel.jobs.resolve_python_executable")
    def test_disabled_payload_keeps_original_render_command(self, runtime: mock.Mock) -> None:
        runtime.return_value = self.python
        commands = self.worker._build_commands(
            {
                "kind": "pipeline",
                "target": "task",
                "payload": {
                    "workflow": "render",
                    "render_mode": "hardsub",
                    "chinese_subtitle_source": "deepseek",
                    "dubbing_enabled": False,
                },
            }
        )
        self.assertEqual(len(commands), 1)
        self.assertNotIn("src.run_dubbing", commands[0][1])
        self.assertNotIn("--audio-source", commands[0][1])

    @mock.patch("src.control_panel.jobs.resolve_dubbing_python")
    @mock.patch("src.control_panel.jobs.load_dubbing_config")
    @mock.patch("src.control_panel.jobs.resolve_python_executable")
    def test_enabled_payload_runs_dubbing_before_stage4(
        self,
        runtime: mock.Mock,
        load_config: mock.Mock,
        dubbing_runtime: mock.Mock,
    ) -> None:
        runtime.return_value = self.python
        load_config.return_value = {}
        dubbing_runtime.return_value = self.python
        commands = self.worker._build_commands(
            {
                "kind": "pipeline",
                "target": "task",
                "payload": {
                    "workflow": "dubbing",
                    "render_mode": "hardsub",
                    "chinese_subtitle_source": "deepseek",
                    "dubbing_enabled": True,
                    "dubbing_subtitle_display": "chinese",
                    "force_dubbing": True,
                },
            }
        )
        self.assertIn("src.run_dubbing", commands[0][1])
        self.assertIn("--force-tts", commands[0][1])
        self.assertIn("src.run_stage4", commands[1][1])
        self.assertIn("--audio-source", commands[1][1])
        self.assertIn("--subtitle-display", commands[1][1])
        source_index = commands[1][1].index("--chinese-source")
        self.assertEqual(commands[1][1][source_index + 1], "auto")


if __name__ == "__main__":
    unittest.main()
