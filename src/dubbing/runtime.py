from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping


WINDOWS_SHARED_DLL_PATTERNS = (
    "avcodec-*.dll",
    "avformat-*.dll",
    "avutil-*.dll",
    "swresample-*.dll",
)
_PREFLIGHT_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_PREFLIGHT_LOCK = threading.Lock()
_PREFLIGHT_PROCESS_LOCK_TIMEOUT_SECONDS = 130.0
_DLL_DIRECTORY_HANDLES: list[Any] = []


class DubbingPreflightError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def project_tools_bin(project_root: Path | str) -> Path:
    return Path(project_root).resolve() / "tools" / "bin"


def build_dubbing_subprocess_env(
    project_root: Path | str,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a process-local environment with project tools first on PATH."""

    environment = dict(os.environ if base_env is None else base_env)
    tools_bin = project_tools_bin(project_root)
    tools_text = str(tools_bin)
    tools_key = os.path.normcase(os.path.normpath(tools_text))
    existing = str(environment.get("PATH") or "")
    parts = [
        part
        for part in existing.split(os.pathsep)
        if part
        and os.path.normcase(os.path.normpath(part.strip('"'))) != tools_key
    ]
    environment["PATH"] = os.pathsep.join([tools_text, *parts])
    return environment


def activate_project_tools(project_root: Path | str) -> Path:
    """Activate project DLL lookup for this process without changing Windows PATH."""

    tools_bin = project_tools_bin(project_root)
    os.environ.update(build_dubbing_subprocess_env(project_root))
    add_dll_directory = getattr(os, "add_dll_directory", None)
    if os.name == "nt" and callable(add_dll_directory) and tools_bin.is_dir():
        normalized = os.path.normcase(str(tools_bin))
        if not any(
            os.path.normcase(str(getattr(handle, "path", ""))) == normalized
            for handle in _DLL_DIRECTORY_HANDLES
        ):
            _DLL_DIRECTORY_HANDLES.append(add_dll_directory(str(tools_bin)))
    return tools_bin


def inspect_ffmpeg_shared(
    project_root: Path | str,
    *,
    windows: bool | None = None,
) -> dict[str, Any]:
    tools_bin = project_tools_bin(project_root)
    ffmpeg = tools_bin / "ffmpeg.exe"
    ffprobe = tools_bin / "ffprobe.exe"
    missing_programs = [
        path.name for path in (ffmpeg, ffprobe) if not path.is_file()
    ]
    if missing_programs:
        return {
            "ready": False,
            "code": "FFMPEG_NOT_FOUND",
            "message": (
                "中文配音预检失败：项目 tools/bin 中缺少 "
                + "、".join(missing_programs)
                + "。"
            ),
            "tools_bin": str(tools_bin),
            "missing": missing_programs,
        }

    missing_dlls: list[str] = []
    matched_dlls: dict[str, list[str]] = {}
    is_windows = os.name == "nt" if windows is None else bool(windows)
    if is_windows:
        for pattern in WINDOWS_SHARED_DLL_PATTERNS:
            matches = sorted(path.name for path in tools_bin.glob(pattern))
            matched_dlls[pattern] = matches
            if not matches:
                missing_dlls.append(pattern)
    if missing_dlls:
        return {
            "ready": False,
            "code": "FFMPEG_SHARED_REQUIRED",
            "message": (
                "中文配音预检失败：已找到 FFmpeg，但不是 TorchCodec 需要的 "
                "Shared Build（缺少 " + "、".join(missing_dlls) + "）。"
            ),
            "tools_bin": str(tools_bin),
            "missing": missing_dlls,
            "dlls": matched_dlls,
        }
    return {
        "ready": True,
        "code": "",
        "message": "",
        "tools_bin": str(tools_bin),
        "ffmpeg": str(ffmpeg),
        "ffprobe": str(ffprobe),
        "dlls": matched_dlls,
    }


def _failure(
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ready": False,
        "code": code,
        "message": message,
        "details": details or {},
    }


def _preflight_cache_key(project_root: Path, python_path: Path) -> str:
    tools_bin = project_tools_bin(project_root)
    signatures: list[tuple[str, int, int]] = []
    for path in sorted(tools_bin.glob("*.dll")) + [
        tools_bin / "ffmpeg.exe",
        tools_bin / "ffprobe.exe",
        python_path,
    ]:
        try:
            stat = path.stat()
        except OSError:
            continue
        signatures.append((str(path), stat.st_size, stat.st_mtime_ns))
    return json.dumps(signatures, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def _preflight_process_lock(root: Path) -> Iterator[None]:
    """Serialize probes across the panel and its separate dubbing worker process."""

    lock_path = root / "work" / "control_panel" / "dubbing_preflight.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
    except OSError:
        # The lock is a reliability aid, not a prerequisite for a valid
        # installation.  Fall back to the in-process lock if its directory is
        # temporarily unavailable.
        yield
        return

    with handle:
        handle.seek(0)
        if not handle.read(1):
            handle.seek(0)
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + _PREFLIGHT_PROCESS_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        # Do not turn a stale or unavailable lock into a
                        # permanent workflow stop.  The subprocess timeout
                        # and retry still provide the final containment.
                        yield
                        return
                    time.sleep(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError:
            yield
            return
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def preflight_dubbing_runtime(
    project_root: Path | str,
    python_path: Path | str | None,
    *,
    use_cache: bool = False,
    cache_seconds: float = 60.0,
) -> dict[str, Any]:
    """Run one serialized runtime preflight and reuse recent results when asked."""

    # Health requests can arrive concurrently in the panel process.  Without
    # this guard, overlapping torch/FFmpeg DLL probes can make a transient
    # loader stall look like a broken installation.
    with _PREFLIGHT_LOCK:
        with _preflight_process_lock(Path(project_root).resolve()):
            return _preflight_dubbing_runtime(
                project_root,
                python_path,
                use_cache=use_cache,
                cache_seconds=cache_seconds,
            )


def _preflight_dubbing_runtime(
    project_root: Path | str,
    python_path: Path | str | None,
    *,
    use_cache: bool = False,
    cache_seconds: float = 60.0,
) -> dict[str, Any]:
    """Verify Shared FFmpeg and perform a real, very short TorchCodec WAV save."""

    root = Path(project_root).resolve()
    shared = inspect_ffmpeg_shared(root)
    if not shared.get("ready"):
        return {**shared, "ffmpeg_shared_ready": False, "torchcodec_ready": False}
    if python_path is None or not Path(python_path).is_file():
        return _failure(
            "DUBBING_RUNTIME_NOT_FOUND",
            "中文配音预检失败：未找到 .venv_dubbing Python 运行时。",
        )
    executable = Path(python_path).resolve()
    cache_key = _preflight_cache_key(root, executable)
    now = time.monotonic()
    cached = _PREFLIGHT_CACHE.get(cache_key)
    if use_cache and cached and now - cached[0] < cache_seconds:
        return dict(cached[1])

    environment = build_dubbing_subprocess_env(root)
    try:
        ffmpeg_probe = subprocess.run(
            [str(shared["ffmpeg"]), "-version"],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=15,
            shell=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = _failure(
            "FFMPEG_SHARED_DLL_LOAD_FAILED",
            "中文配音预检失败：FFmpeg Shared DLL 无法加载；"
            "请检查 tools/bin 中 DLL 是否完整且版本一致。",
            details={"error": str(exc)},
        )
        result.update(ffmpeg_shared_ready=False, torchcodec_ready=False)
        _PREFLIGHT_CACHE[cache_key] = (now, dict(result))
        return result
    ffmpeg_output = (ffmpeg_probe.stdout or "") + "\n" + (ffmpeg_probe.stderr or "")
    if ffmpeg_probe.returncode != 0:
        result = _failure(
            "FFMPEG_SHARED_DLL_LOAD_FAILED",
            "中文配音预检失败：FFmpeg Shared DLL 无法加载；"
            "请检查 tools/bin 中 DLL 是否完整且版本一致。",
            details={"error": ffmpeg_output[-2000:], "returncode": ffmpeg_probe.returncode},
        )
        result.update(ffmpeg_shared_ready=False, torchcodec_ready=False)
        _PREFLIGHT_CACHE[cache_key] = (now, dict(result))
        return result
    if os.name == "nt" and "--enable-shared" not in ffmpeg_output:
        result = _failure(
            "FFMPEG_SHARED_REQUIRED",
            "中文配音预检失败：已找到 FFmpeg，但不是 TorchCodec 需要的 Shared Build。",
            details={"ffmpeg_version": ffmpeg_output[-2000:]},
        )
        result.update(ffmpeg_shared_ready=False, torchcodec_ready=False)
        _PREFLIGHT_CACHE[cache_key] = (now, dict(result))
        return result

    command = [
        str(executable),
        "-m",
        "src.dubbing.torchcodec_probe",
        "--tools-bin",
        str(project_tools_bin(root)),
    ]
    completed: subprocess.CompletedProcess[str] | None = None
    timeout_errors: list[dict[str, str]] = []
    process_error: OSError | None = None
    for attempt in range(1, 3):
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=60,
                shell=False,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
            break
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or exc.output or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            timeout_errors.append(
                {
                    "attempt": str(attempt),
                    "error": str(exc),
                    "partial_output": str(partial)[-2000:],
                }
            )
            if attempt < 2:
                # A fresh process is important: a stuck TorchCodec loader
                # cannot be recovered by waiting on the same child process.
                time.sleep(1.0)
        except OSError as exc:
            process_error = exc
            break
    if process_error is not None:
        result = _failure(
            "TORCHCODEC_DEPENDENCY_FAILED",
            "中文配音预检失败：无法启动 TorchCodec WAV 编码自检。",
            details={"error": str(process_error)},
        )
    elif completed is None:
        result = _failure(
            "TORCHCODEC_PREFLIGHT_TIMEOUT",
            "中文配音预检失败：TorchCodec WAV 编码自检连续两次超时。",
            details={"attempts": timeout_errors},
        )
    else:
        payload: dict[str, Any] = {}
        for line in reversed((completed.stdout or "").splitlines()):
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        if completed.returncode == 0 and payload.get("ready"):
            result = {
                "ready": True,
                "code": "",
                "message": "",
                "torchcodec_ready": True,
                "ffmpeg_shared_ready": True,
                "torch_version": str(payload.get("torch_version") or ""),
                "torchaudio_version": str(payload.get("torchaudio_version") or ""),
                "torchcodec_version": str(payload.get("torchcodec_version") or ""),
                "tools_bin": str(project_tools_bin(root)),
            }
        else:
            code = str(payload.get("code") or "TORCHCODEC_DEPENDENCY_FAILED")
            default_messages = {
                "FFMPEG_SHARED_DLL_LOAD_FAILED": (
                    "中文配音预检失败：FFmpeg Shared DLL 无法加载；"
                    "请检查 tools/bin 中 DLL 是否完整且版本一致。"
                ),
                "TORCHCODEC_PYTORCH_INCOMPATIBLE": (
                    "中文配音预检失败：TorchCodec 与当前 PyTorch/Torchaudio "
                    "版本不兼容。"
                ),
                "TORCHCODEC_DEPENDENCY_FAILED": (
                    "中文配音预检失败：TorchCodec WAV 编码所需依赖不可用。"
                ),
            }
            detail = str(
                payload.get("error")
                or completed.stderr
                or completed.stdout
                or "unknown TorchCodec probe failure"
            )[-4000:]
            result = _failure(
                code,
                default_messages.get(code, default_messages["TORCHCODEC_DEPENDENCY_FAILED"]),
                details={
                    "stage": str(payload.get("stage") or ""),
                    "error_type": str(payload.get("error_type") or ""),
                    "error": detail,
                    "returncode": completed.returncode,
                },
            )
            result.update(ffmpeg_shared_ready=True, torchcodec_ready=False)
    _PREFLIGHT_CACHE[cache_key] = (now, dict(result))
    return result


def ensure_dubbing_runtime(
    project_root: Path | str,
    python_path: Path | str | None,
) -> dict[str, Any]:
    result = preflight_dubbing_runtime(project_root, python_path, use_cache=False)
    if not result.get("ready"):
        raise DubbingPreflightError(
            str(result.get("code") or "DUBBING_PREFLIGHT_FAILED"),
            str(result.get("message") or "中文配音运行环境预检失败。"),
            details=dict(result.get("details") or {}),
        )
    return result


__all__ = [
    "DubbingPreflightError",
    "WINDOWS_SHARED_DLL_PATTERNS",
    "activate_project_tools",
    "build_dubbing_subprocess_env",
    "ensure_dubbing_runtime",
    "inspect_ffmpeg_shared",
    "preflight_dubbing_runtime",
    "project_tools_bin",
]
