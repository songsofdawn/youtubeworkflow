from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping


READY_MARKER = "[DUBBING_WORKER_READY] "
RESULT_MARKER = "[DUBBING_WORKER_RESULT] "


class DubbingWorkerStartError(RuntimeError):
    pass


class DubbingWorkerCrashed(RuntimeError):
    pass


class PersistentDubbingWorkerClient:
    def __init__(
        self,
        python_executable: Path | str,
        project_root: Path | str,
        *,
        idle_timeout_seconds: float = 45.0,
        env: Mapping[str, str] | None = None,
        startup_timeout_seconds: float = 20.0,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.python_executable = Path(python_executable).resolve()
        self.idle_timeout_seconds = max(
            5.0, min(float(idle_timeout_seconds), 300.0)
        )
        command = [
            str(self.python_executable),
            "-m",
            "src.dubbing.worker",
            "--idle-timeout",
            f"{self.idle_timeout_seconds:g}",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                cwd=self.project_root,
                env=dict(env) if env is not None else None,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise DubbingWorkerStartError(
                f"无法启动 persistent dubbing worker：{exc}"
            ) from exc
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        deadline = time.monotonic() + max(1.0, float(startup_timeout_seconds))
        startup_output: list[str] = []
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=0.2)
            except queue.Empty:
                if self.process.poll() is not None:
                    break
                continue
            if line is None:
                break
            startup_output.append(line)
            if line.startswith(READY_MARKER):
                self.loaded_model = False
                return
        self.terminate()
        raise DubbingWorkerStartError(
            "persistent dubbing worker 未就绪"
            + ("：" + "".join(startup_output)[-2000:] if startup_output else "")
        )

    def _read_stdout(self) -> None:
        stdout = self.process.stdout
        if stdout is not None:
            for line in stdout:
                self._lines.put(line)
        self._lines.put(None)

    @property
    def alive(self) -> bool:
        return self.process.poll() is None

    def run(
        self,
        payload: dict[str, Any],
        *,
        on_line: Callable[[str], None],
        cancelled: Callable[[], None] | None = None,
        stopping: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not self.alive or self.process.stdin is None:
            raise DubbingWorkerCrashed("persistent dubbing worker 已退出")
        request_id = uuid.uuid4().hex
        request = {**payload, "command": "run", "request_id": request_id}
        try:
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise DubbingWorkerCrashed(
                f"persistent dubbing worker 通信失败：{exc}"
            ) from exc
        while True:
            if cancelled:
                cancelled()
            if stopping and stopping():
                raise DubbingWorkerCrashed("控制面板正在关闭")
            try:
                line = self._lines.get(timeout=0.2)
            except queue.Empty:
                if not self.alive:
                    raise DubbingWorkerCrashed(
                        f"persistent dubbing worker 意外退出（{self.process.returncode}）"
                    )
                continue
            if line is None:
                raise DubbingWorkerCrashed(
                    f"persistent dubbing worker 输出已关闭（{self.process.poll()}）"
                )
            if line.startswith(RESULT_MARKER):
                try:
                    result = json.loads(line[len(RESULT_MARKER) :])
                except json.JSONDecodeError as exc:
                    raise DubbingWorkerCrashed("persistent worker 返回了无效 JSON") from exc
                if str(result.get("request_id") or "") != request_id:
                    continue
                self.loaded_model = bool(result.get("model_loaded"))
                return result
            on_line(line)

    def shutdown(self, reason: str) -> None:
        if not self.alive:
            self.loaded_model = False
            self._close_pipes()
            return
        if self.process.stdin is not None:
            try:
                self.process.stdin.write(
                    json.dumps(
                        {
                            "command": "shutdown",
                            "request_id": uuid.uuid4().hex,
                            "reason": str(reason),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                self.process.stdin.flush()
                self.process.wait(timeout=10)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                self.terminate()
        self.loaded_model = False
        self._close_pipes()

    def terminate(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.loaded_model = False
        self._close_pipes()

    def _close_pipes(self) -> None:
        for stream in (self.process.stdin, self.process.stdout):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass
        if self._reader.is_alive():
            self._reader.join(timeout=1)


__all__ = [
    "DubbingWorkerCrashed",
    "DubbingWorkerStartError",
    "PersistentDubbingWorkerClient",
]
