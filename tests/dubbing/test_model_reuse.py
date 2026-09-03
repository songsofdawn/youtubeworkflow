from __future__ import annotations

import tempfile
import threading
import unittest
import sys
from pathlib import Path
from unittest import mock

from src.control_panel.jobs import JobStore, WorkflowWorker
from src.control_panel.dubbing_worker import (
    DubbingWorkerCrashed,
    DubbingWorkerStartError,
    PersistentDubbingWorkerClient,
)
from src.dubbing.model_pool import WarmVoxCPM2Pool, model_compatibility_key


class FakeSynthesizer:
    def __init__(self, marker: str, counters: dict[str, int]) -> None:
        self.marker = marker
        self.counters = counters
        self.model_load_seconds = 1.25

    def generate(self, *_: object, **__: object) -> None:
        return None

    def close(self) -> None:
        self.counters["closed"] += 1


class FakeFactory:
    def __init__(self) -> None:
        self.counters = {"created": 0, "closed": 0}

    def __call__(self, model_path: Path, **_: object) -> FakeSynthesizer:
        self.counters["created"] += 1
        return FakeSynthesizer(str(model_path), self.counters)


class ModelPoolTests(unittest.TestCase):
    def test_one_model_is_loaded_once_and_reused_for_compatible_jobs(self) -> None:
        factory = FakeFactory()
        pool = WarmVoxCPM2Pool(synthesizer_factory=factory)
        first = pool.acquire("model-a", device="cuda", settings={"denoise": False})
        first.close()
        second = pool.acquire("model-a", device="cuda", settings={"denoise": False})
        self.assertFalse(first.model_reused)
        self.assertTrue(second.model_reused)
        self.assertEqual(factory.counters["created"], 1)
        pool.close()
        self.assertEqual(factory.counters["closed"], 1)

    def test_model_path_or_load_settings_change_forces_reload(self) -> None:
        factory = FakeFactory()
        pool = WarmVoxCPM2Pool(synthesizer_factory=factory)
        pool.acquire("model-a", device="cuda", settings={"denoise": False})
        changed_path = pool.acquire("model-b", device="cuda", settings={"denoise": False})
        changed_denoiser = pool.acquire(
            "model-b", device="cuda", settings={"denoise": True}
        )
        self.assertFalse(changed_path.model_reused)
        self.assertFalse(changed_denoiser.model_reused)
        self.assertEqual(factory.counters["created"], 3)
        self.assertEqual(factory.counters["closed"], 2)
        pool.close()

    def test_compatibility_key_ignores_per_segment_generation_settings(self) -> None:
        first = model_compatibility_key(
            "model-a",
            device="CUDA",
            allow_cpu=False,
            settings={"denoise": False, "cfg_value": 2.0},
        )
        second = model_compatibility_key(
            "model-a",
            device="cuda",
            allow_cpu=False,
            settings={"denoise": False, "cfg_value": 3.0},
        )
        self.assertEqual(first, second)


class SchedulerReuseTests(unittest.TestCase):
    def test_warm_scheduler_prefers_waiting_dubbing_stage_over_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = JobStore(root / "jobs.sqlite3", root / "logs")
            render = store.enqueue(
                "pipeline",
                "render-task",
                {"workflow": "render", "_stage_index": 0},
                resource_class="gpu_heavy",
            )
            dubbing = store.enqueue(
                "pipeline",
                "dubbing-task",
                {"workflow": "dubbing", "_stage_index": 0},
                resource_class="gpu_heavy",
            )
            worker = WorkflowWorker.__new__(WorkflowWorker)
            worker.store = store
            worker._build_stages = mock.Mock(
                side_effect=lambda job: [
                    (
                        "生成中文 AI 配音"
                        if job["id"] == dubbing["id"]
                        else "生成并质检中文配音成片",
                        ["python.exe"],
                        "gpu_heavy",
                    )
                ]
            )
            claimed = worker._claim_next_warm_dubbing_job()
            render_status = store.get(render["id"])["status"]
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], dubbing["id"])
        self.assertEqual(render_status, "queued")

    def test_waiting_non_dubbing_gpu_stage_prevents_warm_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = JobStore(root / "jobs.sqlite3", root / "logs")
            whisper = store.enqueue(
                "pipeline",
                "whisper-task",
                {"workflow": "complete", "_stage_index": 0},
                resource_class="gpu_heavy",
            )
            dubbing = store.enqueue(
                "pipeline",
                "dubbing-task",
                {"workflow": "dubbing", "_stage_index": 0},
                resource_class="gpu_heavy",
            )
            worker = WorkflowWorker.__new__(WorkflowWorker)
            worker.store = store
            worker._build_stages = mock.Mock(
                side_effect=lambda job: [
                    (
                        "生成并选择最佳英文字幕"
                        if job["id"] == whisper["id"]
                        else "生成中文 AI 配音",
                        ["python.exe"],
                        "gpu_heavy",
                    )
                ]
            )
            claimed = worker._claim_next_warm_dubbing_job()
            dubbing_status = store.get(dubbing["id"])["status"]
        self.assertIsNone(claimed)
        self.assertEqual(dubbing_status, "queued")

    def test_persistent_disabled_uses_legacy_subprocess_path(self) -> None:
        worker = WorkflowWorker.__new__(WorkflowWorker)
        worker.project_root = Path(".").resolve()
        legacy = mock.Mock(return_value=0)
        worker._run_subprocess_command = legacy
        with mock.patch(
            "src.control_panel.jobs.load_dubbing_config",
            return_value={"performance": {"keep_voxcpm_warm": False}},
        ):
            result = worker._run_command(
                "job",
                ["python.exe", "-m", "src.run_dubbing", "--video-dir", "task"],
                Path("job.log"),
            )
        self.assertEqual(result, 0)
        legacy.assert_called_once()

    def test_persistent_start_failure_falls_back_to_legacy_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "job.log"
            worker = WorkflowWorker.__new__(WorkflowWorker)
            worker.project_root = Path(temporary)
            worker._run_persistent_dubbing_command = mock.Mock(
                side_effect=DubbingWorkerStartError("startup failed")
            )
            worker._run_subprocess_command = mock.Mock(return_value=0)
            with mock.patch(
                "src.control_panel.jobs.load_dubbing_config",
                return_value={"performance": {"keep_voxcpm_warm": True}},
            ):
                result = worker._run_command(
                    "job",
                    ["python.exe", "-m", "src.run_dubbing", "--video-dir", "task"],
                    log_path,
                )
            log = log_path.read_text(encoding="utf-8")
        self.assertEqual(result, 0)
        self.assertIn("falling back to one-task lifecycle", log)
        worker._run_subprocess_command.assert_called_once()

    def test_worker_crash_returns_failure_and_discards_warm_process(self) -> None:
        class FakeProcess:
            pass

        class FakeClient:
            def __init__(self, executable: Path) -> None:
                self.python_executable = executable
                self.alive = True
                self.loaded_model = True
                self.process = FakeProcess()
                self.terminated = False

            def run(self, *_: object, **__: object) -> dict[str, object]:
                raise DubbingWorkerCrashed("simulated crash")

            def terminate(self) -> None:
                self.terminated = True
                self.alive = False

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            executable = root / "python.exe"
            executable.write_bytes(b"python")
            log_path = root / "job.log"
            client = FakeClient(executable)
            worker = WorkflowWorker.__new__(WorkflowWorker)
            worker.project_root = root
            worker.publisher = mock.Mock()
            worker.publisher.redact_log_text.side_effect = lambda value: value
            worker._process_lock = threading.Lock()
            worker._processes = {}
            worker._cancel_requested = set()
            worker._stop_event = threading.Event()
            worker._dubbing_worker_client = client
            result = worker._run_persistent_dubbing_command(
                "job",
                [str(executable), "-m", "src.run_dubbing", "--video-dir", "task"],
                log_path,
                config={"performance": {"worker_idle_timeout_seconds": 45}},
                stage_progress_start=0,
                stage_progress_span=100,
            )
            log = log_path.read_text(encoding="utf-8")
        self.assertEqual(result, 2)
        self.assertTrue(client.terminated)
        self.assertIsNone(worker._dubbing_worker_client)
        self.assertIn("reuse segment checkpoints", log)

    def test_persistent_request_preserves_paid_api_permission(self) -> None:
        payload = WorkflowWorker._dubbing_request_from_command(
            [
                "python.exe",
                "-m",
                "src.run_dubbing",
                "--video-dir",
                "task",
                "--allow-paid-api",
            ]
        )
        self.assertTrue(payload["allow_paid_api"])

    def test_jsonl_worker_starts_and_releases_without_loading_model(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        client = PersistentDubbingWorkerClient(
            sys.executable,
            project_root,
            idle_timeout_seconds=5,
        )
        try:
            self.assertTrue(client.alive)
            self.assertFalse(client.loaded_model)
        finally:
            client.shutdown("test shutdown")
        self.assertFalse(client.alive)


if __name__ == "__main__":
    unittest.main()
