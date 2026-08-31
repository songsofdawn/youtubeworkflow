from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable


LogCallback = Callable[[str], None]
MANIFEST_REPLACE_DELAYS = (0.05, 0.10, 0.20, 0.40, 0.80, 1.00, 1.50)
_LOCKS_GUARD = threading.Lock()
_MANIFEST_LOCKS: dict[str, threading.RLock] = {}


class ManifestSaveError(RuntimeError):
    def __init__(self, path: Path, attempts: int, cause: BaseException) -> None:
        self.path = path
        self.attempts = attempts
        self.cause = cause
        super().__init__(
            "保存中文配音进度失败：manifest.json 持续被其他进程占用，"
            f"已重试 {attempts} 次。"
        )


def _path_lock(path: Path | str) -> threading.RLock:
    key = os.path.normcase(str(Path(path).resolve()))
    with _LOCKS_GUARD:
        return _MANIFEST_LOCKS.setdefault(key, threading.RLock())


def load_manifest(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        return {}
    with _path_lock(source):
        try:
            with source.open("r", encoding="utf-8-sig") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
    return value if isinstance(value, dict) else {}


def _retryable_replace_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32}


def _atomic_write_json(
    path: Path | str,
    value: Any,
    *,
    retry_delays: Iterable[float],
    log: LogCallback | None,
    sleep: Callable[[float], None],
    replace: Callable[[Path, Path], None],
    failure_factory: Callable[[Path, int, BaseException], BaseException],
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    delays = tuple(max(0.0, float(delay)) for delay in retry_delays)
    attempts = len(delays) + 1
    temporary = destination.with_name(
        f".tmp-{uuid.uuid4().hex[:12]}{destination.suffix}"
    )
    with _path_lock(destination):
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

            for attempt in range(1, attempts + 1):
                try:
                    replace(temporary, destination)
                    return destination
                except OSError as exc:
                    if not _retryable_replace_error(exc):
                        raise
                    if attempt >= attempts:
                        raise failure_factory(destination, attempts, exc) from exc
                    if log:
                        log(
                            "[DUBBING] RETRY: manifest.json 暂时被占用，"
                            f"正在重试 {attempt + 1}/{attempts}"
                        )
                    sleep(delays[attempt - 1])
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return destination


def save_manifest(
    path: Path | str,
    value: Any,
    *,
    log: LogCallback | None = None,
    retry_delays: Iterable[float] = MANIFEST_REPLACE_DELAYS,
    sleep: Callable[[float], None] = time.sleep,
    replace: Callable[[Path, Path], None] = os.replace,
) -> Path:
    return _atomic_write_json(
        path,
        value,
        retry_delays=retry_delays,
        log=log,
        sleep=sleep,
        replace=replace,
        failure_factory=ManifestSaveError,
    )


def save_segment_metadata(path: Path | str, value: Any) -> Path:
    def failure(destination: Path, attempts: int, cause: BaseException) -> BaseException:
        return RuntimeError(
            f"保存中文配音片段元数据失败：{destination.name}（已尝试 {attempts} 次）"
        )

    return _atomic_write_json(
        path,
        value,
        retry_delays=MANIFEST_REPLACE_DELAYS,
        log=None,
        sleep=time.sleep,
        replace=os.replace,
        failure_factory=failure,
    )


__all__ = [
    "MANIFEST_REPLACE_DELAYS",
    "ManifestSaveError",
    "load_manifest",
    "save_manifest",
    "save_segment_metadata",
]
