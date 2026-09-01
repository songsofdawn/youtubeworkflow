from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from src.control_panel.app import ControlPanelApp
from src.control_panel.jobs import JobStore
from src.control_panel.jobs import WorkflowWorker
from src.control_panel.server import make_handler
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
                    "dubbing_reference_mode": "manual",
                    "dubbing_reference_start": 2.5,
                    "dubbing_reference_end": 9.0,
                    "dubbing_subtitle_display": "chinese",
                    "force_dubbing": True,
                },
            }
        )
        self.assertIn("src.run_dubbing", commands[0][1])
        self.assertEqual(
            commands[0][1][commands[0][1].index("--reference-start") + 1],
            "2.5",
        )
        self.assertEqual(
            commands[0][1][commands[0][1].index("--reference-end") + 1],
            "9.0",
        )
        self.assertIn("--force-tts", commands[0][1])
        self.assertIn("src.run_stage4", commands[1][1])
        self.assertIn("--audio-source", commands[1][1])
        self.assertIn("--subtitle-display", commands[1][1])
        source_index = commands[1][1].index("--chinese-source")
        self.assertEqual(commands[1][1][source_index + 1], "auto")

    @mock.patch("src.control_panel.jobs.resolve_dubbing_python")
    @mock.patch("src.control_panel.jobs.load_dubbing_config")
    @mock.patch("src.control_panel.jobs.resolve_python_executable")
    def test_complete_dubbing_workflow_uses_conversational_translation_mode(
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
                    "workflow": "complete",
                    "render_mode": "hardsub",
                    "chinese_subtitle_source": "deepseek",
                    "dubbing_enabled": True,
                },
            }
        )

        translation = next(
            command
            for label, command in commands
            if label == "翻译并检查中文字幕"
        )
        self.assertIn("--for-dubbing", translation)

    @mock.patch("src.control_panel.jobs.subprocess.Popen")
    def test_worker_passes_project_tools_path_to_spawned_process(
        self,
        popen: mock.Mock,
    ) -> None:
        log_path = self.root / "job.log"
        process = mock.Mock()
        process.stdout = []
        process.wait.return_value = 0
        process.poll.return_value = 0
        popen.return_value = process
        self.worker.publisher = mock.Mock()
        self.worker.store = mock.Mock()
        self.worker._process_lock = threading.RLock()
        self.worker._processes = {}
        self.worker._cancel_requested = set()
        self.worker._stop_event = threading.Event()

        exit_code = self.worker._run_command(
            "test-job",
            [str(self.python), "-c", "pass"],
            log_path,
        )

        self.assertEqual(exit_code, 0)
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(
            environment["PATH"].split(os.pathsep)[0],
            str(self.root / "tools" / "bin"),
        )


class ControlPanelDubbingQueueTests(unittest.TestCase):
    DUBBING_HEALTH = {
        "runtime_ready": True,
        "demucs_ready": True,
        "voxcpm_ready": True,
        "entrypoint_ready": True,
        "torchcodec_ready": True,
        "device_ready": True,
        "model_ready": True,
        "model_path": "models/VoxCPM2",
    }

    def test_queue_downloads_persists_dubbing_options_and_old_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "publish_config.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (root / "config" / "bilibili_categories.json").write_text(
                json.dumps(
                    {
                        "fallback_tid": 21,
                        "categories": [
                            {
                                "tid": 21,
                                "name": "日常",
                                "parent_tid": 160,
                                "parent_name": "生活",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            app = ControlPanelApp(root)
            try:
                with mock.patch.object(
                    app,
                    "health",
                    return_value={"dubbing": self.DUBBING_HEALTH},
                ):
                    configured = app.queue_downloads(
                        raw_input="https://youtu.be/abcdefghijk",
                        confirm_rights=True,
                        dubbing_enabled=True,
                        dubbing_reference_mode="manual",
                        dubbing_reference_start=1.25,
                        dubbing_reference_end=8.75,
                        dubbing_subtitle_display="bilingual",
                        force_dubbing=True,
                    )[0]
                    legacy = app.queue_downloads(
                        raw_input="https://youtu.be/12345678901",
                        confirm_rights=True,
                    )[0]
            finally:
                app.close()

        self.assertEqual(
            {
                key: configured["payload"][key]
                for key in (
                    "dubbing_enabled",
                    "dubbing_reference_mode",
                    "dubbing_reference_start",
                    "dubbing_reference_end",
                    "dubbing_subtitle_display",
                    "force_dubbing",
                )
            },
            {
                "dubbing_enabled": True,
                "dubbing_reference_mode": "manual",
                "dubbing_reference_start": 1.25,
                "dubbing_reference_end": 8.75,
                "dubbing_subtitle_display": "bilingual",
                "force_dubbing": True,
            },
        )
        self.assertFalse(legacy["payload"]["dubbing_enabled"])
        self.assertEqual(legacy["payload"]["dubbing_reference_mode"], "auto")
        self.assertIsNone(legacy["payload"]["dubbing_reference_start"])
        self.assertIsNone(legacy["payload"]["dubbing_reference_end"])
        self.assertEqual(legacy["payload"]["dubbing_subtitle_display"], "chinese")
        self.assertFalse(legacy["payload"]["force_dubbing"])

    def test_unattended_dubbing_requires_api_translation_and_persists_review_policy(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "publish_config.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (root / "config" / "bilibili_categories.json").write_text(
                json.dumps(
                    {
                        "fallback_tid": 21,
                        "categories": [
                            {
                                "tid": 21,
                                "name": "日常",
                                "parent_tid": 160,
                                "parent_name": "生活",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            app = ControlPanelApp(root)
            health = {
                "dubbing": self.DUBBING_HEALTH,
                "checks": {"translation_api": True},
                "discovery": {"reachable": False, "model_ready": False},
            }
            try:
                with mock.patch.object(app, "health", return_value=health):
                    configured = app.queue_downloads(
                        raw_input="https://youtu.be/abcdefghijk",
                        confirm_rights=True,
                        auto_publish=True,
                        automation_target="render",
                        automation_chinese_policy="api_always",
                        automation_render_mode="hardsub",
                        automation_dubbing_review_policy="continue",
                        dubbing_enabled=True,
                    )[0]
                    with self.assertRaisesRegex(ValueError, "始终使用 API 翻译"):
                        app.queue_downloads(
                            raw_input="https://youtu.be/12345678901",
                            confirm_rights=True,
                            auto_publish=True,
                            automation_target="render",
                            automation_chinese_policy="youtube_preferred",
                            dubbing_enabled=True,
                        )
            finally:
                app.close()

        self.assertTrue(configured["payload"]["dubbing_enabled"])
        self.assertEqual(
            configured["payload"]["automation_chinese_policy"],
            "api_always",
        )
        self.assertEqual(
            configured["payload"]["automation_dubbing_review_policy"],
            "continue",
        )

    def test_post_download_automation_preserves_all_dubbing_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "downloads" / "task"
            task.mkdir(parents=True)
            (task / "download_manifest.json").write_text(
                json.dumps({"video_id": "abcdefghijk", "overall_status": "success"}),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs.sqlite3", root / "logs")
            worker = WorkflowWorker(
                root,
                store,
                WorkflowScanner(root),
                mock.Mock(),
            )
            try:
                with mock.patch.object(
                    worker,
                    "_task_reference_for_video_id",
                    return_value="task",
                ):
                    worker._queue_post_download_automation(
                        {
                            "target": "abcdefghijk",
                            "payload": {
                                "automation_enabled": True,
                                "automation_target": "render",
                                "auto_translate_missing": True,
                                "dubbing_enabled": True,
                                "dubbing_reference_mode": "manual",
                                "dubbing_reference_start": 2.5,
                                "dubbing_reference_end": 9.0,
                                "dubbing_subtitle_display": "bilingual",
                                "automation_dubbing_review_policy": "continue",
                                "force_dubbing": True,
                            },
                        }
                    )
                queued = next(job for job in store.list() if job["kind"] == "pipeline")
            finally:
                worker.close()

        self.assertEqual(queued["payload"]["workflow"], "complete")
        self.assertTrue(queued["payload"]["dubbing_enabled"])
        self.assertEqual(queued["payload"]["dubbing_reference_mode"], "manual")
        self.assertEqual(queued["payload"]["dubbing_reference_start"], 2.5)
        self.assertEqual(queued["payload"]["dubbing_reference_end"], 9.0)
        self.assertEqual(queued["payload"]["dubbing_subtitle_display"], "bilingual")
        self.assertEqual(
            queued["payload"]["automation_dubbing_review_policy"],
            "continue",
        )
        self.assertTrue(queued["payload"]["force_dubbing"])

    def test_unattended_review_policy_blocks_before_render_or_can_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "downloads" / "task"
            (task / "dubbing").mkdir(parents=True)
            (task / "download_manifest.json").write_text("{}", encoding="utf-8")
            (task / "dubbing" / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "COMPLETED_WITH_REVIEW",
                        "needs_review": True,
                        "segment_count": 3,
                        "segments": [
                            {"index": 1, "needs_review": True},
                            {"index": 2, "needs_review": False},
                            {"index": 3, "needs_review": True},
                        ],
                        "warnings": ["segment 1", "segment 3"],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs.sqlite3", root / "logs")
            publisher = mock.Mock()
            worker = WorkflowWorker(root, store, WorkflowScanner(root), publisher)
            try:
                blocked = store.enqueue(
                    "pipeline",
                    "task",
                    {
                        "automation_enabled": True,
                        "automation_failure_policy": "skip",
                        "automation_dubbing_review_policy": "block",
                    },
                )
                handled = worker._handle_unattended_dubbing_review(
                    blocked,
                    log_path=Path(blocked["log_path"]),
                )
                blocked_after = store.get(str(blocked["id"]))

                continued = store.enqueue(
                    "pipeline",
                    "task",
                    {
                        "automation_enabled": True,
                        "automation_failure_policy": "skip",
                        "automation_dubbing_review_policy": "continue",
                    },
                )
                should_stop = worker._handle_unattended_dubbing_review(
                    continued,
                    log_path=Path(continued["log_path"]),
                )
            finally:
                worker.close()

        self.assertTrue(handled)
        self.assertEqual(blocked_after["status"], "completed")
        self.assertIn("已阻止成片和投稿", blocked_after["step"])
        publisher.mark_automation_skipped.assert_called_once()
        self.assertEqual(
            publisher.mark_automation_skipped.call_args.args[1],
            "DUBBING_TIMING_REVIEW_REQUIRED",
        )
        self.assertFalse(should_stop)

    def test_unattended_review_auto_fallback_requeues_original_audio_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / "downloads" / "task"
            (task / "dubbing").mkdir(parents=True)
            (task / "download_manifest.json").write_text("{}", encoding="utf-8")
            (task / "dubbing" / "manifest.json").write_text(
                json.dumps(
                    {
                        "status": "COMPLETED_WITH_REVIEW",
                        "needs_review": True,
                        "segment_count": 2,
                        "segments": [
                            {"index": 1, "needs_review": True},
                            {"index": 2, "needs_review": False},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = JobStore(root / "jobs.sqlite3", root / "logs")
            publisher = mock.Mock()
            worker = WorkflowWorker(root, store, WorkflowScanner(root), publisher)
            try:
                queued = store.enqueue(
                    "pipeline",
                    "task",
                    {
                        "workflow": "complete",
                        "automation_enabled": True,
                        "automation_target": "publish",
                        "dubbing_enabled": True,
                        "automation_dubbing_review_policy": "auto_fallback",
                    },
                )
                running = store.claim_id(str(queued["id"]))
                self.assertIsNotNone(running)
                with mock.patch.object(
                    worker,
                    "_build_stages",
                    return_value=[
                        ("生成并质检双语成片", ["python", "stage4"], "gpu_heavy")
                    ],
                ):
                    handled = worker._handle_unattended_dubbing_review(
                        running,
                        log_path=Path(running["log_path"]),
                    )
                rerouted = store.get(str(queued["id"]))
            finally:
                worker.close()

        self.assertTrue(handled)
        self.assertEqual(rerouted["status"], "queued")
        self.assertFalse(rerouted["payload"]["dubbing_enabled"])
        self.assertTrue(rerouted["payload"]["dubbing_fallback"])
        self.assertEqual(
            rerouted["payload"]["media_variant"],
            "subtitled_original_audio",
        )
        publisher.mark_automation_fallback.assert_called_once()


class ServerDubbingRoutingTests(unittest.TestCase):
    def test_download_and_pipeline_routes_forward_all_dubbing_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            static = root / "static"
            static.mkdir()
            app = mock.create_autospec(ControlPanelApp, instance=True)
            app.queue_downloads.return_value = [{"id": "download-job"}]
            app.queue_pipeline.return_value = [{"id": "pipeline-job"}]
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(app, static),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def post(path: str, payload: dict[str, object]) -> dict[str, object]:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}{path}",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 202)
                    return json.loads(response.read().decode("utf-8"))

            dubbing = {
                "dubbing_enabled": True,
                "dubbing_reference_mode": "manual",
                "dubbing_reference_start": 3.5,
                "dubbing_reference_end": 11.25,
                "dubbing_subtitle_display": "bilingual",
                "automation_dubbing_review_policy": "continue",
                "force_dubbing": True,
            }
            try:
                post(
                    "/api/downloads",
                    {
                        "input": "https://youtu.be/abcdefghijk",
                        "confirm_rights": True,
                        **dubbing,
                    },
                )
                post(
                    "/api/pipeline",
                    {
                        "tasks": ["task"],
                        "workflow": "dubbing",
                        "render_mode": "hardsub",
                        "chinese_subtitle_source": "deepseek",
                        "allow_paid_api": False,
                        **dubbing,
                    },
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        for call in (app.queue_downloads.call_args, app.queue_pipeline.call_args):
            self.assertIsNotNone(call)
            forwarded = call.kwargs
            for key, value in dubbing.items():
                self.assertEqual(forwarded[key], value)


class PanelDubbingMarkupTests(unittest.TestCase):
    def test_unattended_panel_exposes_dubbing_and_review_controls(self) -> None:
        project = Path(__file__).resolve().parents[2]
        html = (project / "src" / "control_panel" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        javascript = (
            project / "src" / "control_panel" / "static" / "app.js"
        ).read_text(encoding="utf-8")

        for element_id in (
            "automationDubbingEnabled",
            "automationDubbingReferenceMode",
            "automationDubbingReferenceStart",
            "automationDubbingReferenceEnd",
            "automationDubbingSubtitleDisplay",
            "automationDubbingReviewPolicy",
            "automationDubbingFlow",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("automation_dubbing_review_policy", javascript)
        self.assertIn("dubbing_enabled: settings.dubbingEnabled", javascript)
        self.assertIn('$("#automationChinesePolicy").value = "api_always"', javascript)


if __name__ == "__main__":
    unittest.main()
