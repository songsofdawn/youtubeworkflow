from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from src.stage3 import cuda_runtime


class CudaRuntimeTests(TestCase):
    def _site(self, directory: str, *, complete: bool = True) -> Path:
        root = Path(directory)
        mapping = {
            ("ctranslate2",): None,
            ("nvidia", "cublas", "bin"): "cublas64_12.dll",
            ("nvidia", "cuda_runtime", "bin"): "cudart64_12.dll",
            ("nvidia", "cudnn", "bin"): "cudnn64_9.dll",
        }
        for parts, dll in mapping.items():
            path = root.joinpath(*parts); path.mkdir(parents=True)
            if complete and dll:
                (path / dll).write_bytes(b"dll")
        return root

    def test_discovers_expected_dll_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._site(directory)
            found = cuda_runtime.discover_cuda_dll_directories([root])
            self.assertEqual(len(found), 4)
            self.assertTrue(any(path.name == "ctranslate2" for path in found))

    def test_missing_cuda_dll_has_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._site(directory, complete=False)
            with self.assertRaisesRegex(RuntimeError, "requirements.lock.txt"):
                cuda_runtime.configure_cuda_runtime(platform_name="win32", site_packages=[root])

    def test_registration_keeps_handles_and_updates_process_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(os, "add_dll_directory", create=True, side_effect=lambda value: ("handle", value)):
            root = self._site(directory)
            original_handles = len(cuda_runtime.DLL_HANDLES)
            with mock.patch.dict(os.environ, {"PATH": "existing"}, clear=False):
                report = cuda_runtime.configure_cuda_runtime(platform_name="win32", site_packages=[root])
                self.assertIn(str(root / "nvidia" / "cublas" / "bin"), os.environ["PATH"])
            self.assertEqual(report["registered_directory_count"], 4)
            self.assertEqual(len(cuda_runtime.DLL_HANDLES), original_handles + 4)

    def test_non_windows_is_noop(self) -> None:
        report = cuda_runtime.configure_cuda_runtime(platform_name="linux")
        self.assertFalse(report["windows_setup"])
        self.assertEqual(report["registered_directory_count"], 0)
