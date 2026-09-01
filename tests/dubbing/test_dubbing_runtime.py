from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.dubbing.runtime import (
    build_dubbing_subprocess_env,
    inspect_ffmpeg_shared,
    preflight_dubbing_runtime,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DUBBING_PYTHON = PROJECT_ROOT / ".venv_dubbing" / "Scripts" / "python.exe"


def create_tools(root: Path, *, shared: bool) -> Path:
    tools = root / "tools" / "bin"
    tools.mkdir(parents=True)
    (tools / "ffmpeg.exe").write_bytes(b"ffmpeg")
    (tools / "ffprobe.exe").write_bytes(b"ffprobe")
    if shared:
        for name in (
            "avcodec-63.dll",
            "avformat-63.dll",
            "avutil-61.dll",
            "swresample-7.dll",
        ):
            (tools / name).write_bytes(b"dll")
    return tools


class DubbingRuntimeTests(unittest.TestCase):
    def test_project_tools_bin_is_first_in_child_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = build_dubbing_subprocess_env(
                root,
                {"PATH": os.pathsep.join(["first", "second"])},
            )
        self.assertEqual(
            environment["PATH"].split(os.pathsep)[0],
            str(root.resolve() / "tools" / "bin"),
        )

    def test_shared_dll_set_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_tools(root, shared=True)
            result = inspect_ffmpeg_shared(root, windows=True)
        self.assertTrue(result["ready"])

    def test_static_only_ffmpeg_is_rejected_with_shared_build_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_tools(root, shared=False)
            result = inspect_ffmpeg_shared(root, windows=True)
        self.assertFalse(result["ready"])
        self.assertEqual(result["code"], "FFMPEG_SHARED_REQUIRED")
        self.assertIn("Shared Build", result["message"])

    def test_torchcodec_timeout_retries_in_a_fresh_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_tools(root, shared=True)
            runtime = root / "python.exe"
            runtime.write_bytes(b"python")
            responses = [
                subprocess.CompletedProcess(
                    ["ffmpeg", "-version"],
                    0,
                    stdout="configuration: --enable-shared",
                    stderr="",
                ),
                subprocess.TimeoutExpired(
                    ["python", "-m", "src.dubbing.torchcodec_probe"],
                    60,
                    output='{\"probe_status\":\"starting\",\"stage\":\"imports\"}',
                ),
                subprocess.CompletedProcess(
                    ["python", "-m", "src.dubbing.torchcodec_probe"],
                    0,
                    stdout='{\"ready\":true,\"torch_version\":\"test\"}\n',
                    stderr="",
                ),
            ]
            with (
                mock.patch(
                    "src.dubbing.runtime.subprocess.run",
                    side_effect=responses,
                ) as run,
                mock.patch("src.dubbing.runtime.time.sleep"),
            ):
                result = preflight_dubbing_runtime(
                    root,
                    runtime,
                    use_cache=False,
                )

        self.assertTrue(result["ready"], result)
        self.assertEqual(run.call_count, 3)

    @unittest.skipUnless(
        DUBBING_PYTHON.is_file()
        and (PROJECT_ROOT / "tools" / "bin" / "avcodec-63.dll").is_file(),
        "本机未安装可执行 TorchCodec 自检的可选中配环境",
    )
    def test_real_torchcodec_wav_encoding_preflight_succeeds(self) -> None:
        result = preflight_dubbing_runtime(
            PROJECT_ROOT,
            DUBBING_PYTHON,
            use_cache=False,
        )
        self.assertTrue(result["ready"], result)
        self.assertTrue(result["ffmpeg_shared_ready"])
        self.assertTrue(result["torchcodec_ready"])


if __name__ == "__main__":
    unittest.main()
