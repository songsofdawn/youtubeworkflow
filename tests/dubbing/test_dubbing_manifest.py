from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.control_panel.tasks import read_json
from src.dubbing.manifest import ManifestSaveError, load_manifest, save_manifest


class DubbingManifestTests(unittest.TestCase):
    def test_hundreds_of_atomic_saves_remain_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            for value in range(400):
                save_manifest(path, {"value": value}, retry_delays=())
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["value"], value)
            self.assertEqual(load_manifest(path)["value"], 399)
            self.assertEqual(list(path.parent.glob(".tmp-*.json")), [])

    def test_concurrent_high_frequency_reader_and_writer_never_sees_broken_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            save_manifest(path, {"value": -1}, retry_delays=())
            finished = threading.Event()
            errors: list[BaseException] = []
            observed: list[int] = []

            def reader() -> None:
                try:
                    while not finished.is_set():
                        # The dashboard reads into memory and closes immediately;
                        # a transient Windows open denial is treated as one empty
                        # poll rather than a broken task.
                        payload = read_json(path)
                        if payload:
                            observed.append(int(payload["value"]))
                        finished.wait(0.001)
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=reader)
            thread.start()
            try:
                for value in range(300):
                    save_manifest(path, {"value": value})
            finally:
                finished.set()
                thread.join(timeout=10)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(observed)
            self.assertEqual(load_manifest(path)["value"], 299)
            self.assertEqual(list(path.parent.glob(".tmp-*.json")), [])

    def test_permission_error_is_retried_until_replace_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            calls = 0
            messages: list[str] = []
            sleep = Mock()

            def flaky_replace(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls <= 3:
                    raise PermissionError(5, "access denied", str(destination))
                os.replace(source, destination)

            save_manifest(
                path,
                {"status": "RUNNING"},
                retry_delays=(0.05, 0.10, 0.20),
                log=messages.append,
                sleep=sleep,
                replace=flaky_replace,
            )

            self.assertEqual(calls, 4)
            self.assertEqual(sleep.call_count, 3)
            self.assertEqual(load_manifest(path)["status"], "RUNNING")
            self.assertEqual(len(messages), 3)
            self.assertIn("正在重试 4/4", messages[-1])
            self.assertEqual(list(path.parent.glob(".tmp-*.json")), [])

    def test_permanent_permission_error_stops_after_eight_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            calls = 0

            def locked_replace(source: Path, destination: Path) -> None:
                nonlocal calls
                del source
                calls += 1
                raise PermissionError(5, "access denied", str(destination))

            with self.assertRaisesRegex(ManifestSaveError, "已重试 8 次"):
                save_manifest(
                    path,
                    {"status": "RUNNING"},
                    retry_delays=(0, 0, 0, 0, 0, 0, 0),
                    sleep=lambda _: None,
                    replace=locked_replace,
                )

            self.assertEqual(calls, 8)
            self.assertEqual(list(path.parent.glob(".tmp-*.json")), [])


if __name__ == "__main__":
    unittest.main()
