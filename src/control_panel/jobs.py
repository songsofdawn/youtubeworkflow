from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .publishing import BiliupIntegration
from .tasks import WorkflowScanner


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobCancelled(RuntimeError):
    pass


class JobStore:
    def __init__(self, database_path: Path, logs_dir: Path) -> None:
        self.database_path = database_path
        self.logs_dir = logs_dir
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    target TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    step TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    exit_code INTEGER,
                    error TEXT NOT NULL DEFAULT '',
                    log_path TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                UPDATE jobs
                SET status = 'queued', step = '检测到上次中断，等待续跑',
                    started_at = '', finished_at = '', exit_code = NULL
                WHERE status = 'running'
                """
            )

    def enqueue(self, kind: str, target: str, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        log_path = self.logs_dir / f"{job_id}.log"
        record = {
            "id": job_id,
            "kind": kind,
            "target": target,
            "payload_json": json.dumps(payload, ensure_ascii=False),
            "status": "queued",
            "step": "等待执行",
            "progress": 0,
            "created_at": utc_now(),
            "log_path": str(log_path),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs
                (id, kind, target, payload_json, status, step, progress, created_at, log_path)
                VALUES (:id, :kind, :target, :payload_json, :status, :step, :progress, :created_at, :log_path)
                """,
                record,
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._serialize(row)

    def list(self, limit: int = 80) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()
        return [self._serialize(row) for row in rows]

    def claim_next(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs
                SET status = 'running', step = '正在准备', started_at = ?,
                    finished_at = '', error = '', exit_code = NULL
                WHERE id = ?
                """,
                (utc_now(), row["id"]),
            )
            connection.commit()
        return self.get(str(row["id"]))

    def update(self, job_id: str, **values: Any) -> dict[str, Any]:
        allowed = {
            "status",
            "step",
            "progress",
            "started_at",
            "finished_at",
            "exit_code",
            "error",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return self.get(job_id)
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                (*updates.values(), job_id),
            )
        return self.get(job_id)

    def retry(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] not in {"failed", "cancelled"}:
            raise ValueError("只有失败或已取消的任务可以重试")
        log_path = Path(job["log_path"])
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("\n\n===== 用户请求重试 =====\n")
        return self.update(
            job_id,
            status="queued",
            step="等待重试",
            progress=0,
            started_at="",
            finished_at="",
            exit_code=None,
            error="",
        )

    def has_active(self, kind: str, target: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE kind = ? AND target = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (kind, target),
            ).fetchone()
        return row is not None

    def active_for_targets(self, targets: set[str]) -> list[dict[str, Any]]:
        normalized = {str(target) for target in targets if str(target)}
        if not normalized:
            return []
        placeholders = ", ".join("?" for _ in normalized)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE target IN ({placeholders})
                  AND status IN ('queued', 'running')
                ORDER BY created_at
                """,
                tuple(sorted(normalized)),
            ).fetchall()
        return [self._serialize(row) for row in rows]

    def log_tail(self, job_id: str, max_chars: int = 30000) -> str:
        job = self.get(job_id)
        path = self._safe_log_path(job)
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:]

    def delete_log(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] in {"queued", "running"}:
            raise ValueError("运行中或排队中的任务不能删除日志，请先终止任务")
        path = self._safe_log_path(job)
        size = path.stat().st_size if path.is_file() else 0
        path.unlink(missing_ok=True)
        return {"deleted": size > 0, "bytes": size, "job_id": job_id}

    def clear_inactive_logs(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY created_at DESC").fetchall()
        jobs = [self._serialize(row) for row in rows]
        active_paths = {
            self._safe_log_path(job)
            for job in jobs
            if job["status"] in {"queued", "running"}
        }
        deleted = 0
        deleted_bytes = 0
        skipped_active = sum(path.is_file() for path in active_paths)
        root = self.logs_dir.resolve()
        for candidate in self.logs_dir.rglob("*.log"):
            try:
                path = candidate.resolve()
                path.relative_to(root)
            except (OSError, ValueError):
                continue
            if path in active_paths or not path.is_file():
                continue
            deleted_bytes += path.stat().st_size
            path.unlink()
            deleted += 1
        return {
            "deleted": deleted,
            "bytes": deleted_bytes,
            "skipped_active": skipped_active,
        }

    def delete_jobs_for_targets(self, targets: set[str]) -> dict[str, int]:
        normalized = {str(target) for target in targets if str(target)}
        if not normalized:
            return {"jobs": 0, "logs": 0, "log_bytes": 0}
        active = self.active_for_targets(normalized)
        if active:
            raise ValueError("视频仍有运行中或排队中的任务，请先终止后再删除")
        placeholders = ", ".join("?" for _ in normalized)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE target IN ({placeholders})",
                tuple(sorted(normalized)),
            ).fetchall()
        jobs = [self._serialize(row) for row in rows]
        deleted_logs = 0
        deleted_bytes = 0
        for job in jobs:
            path = self._safe_log_path(job)
            if path.is_file():
                deleted_bytes += path.stat().st_size
                path.unlink()
                deleted_logs += 1
        if jobs:
            with self._connect() as connection:
                connection.executemany(
                    "DELETE FROM jobs WHERE id = ?",
                    [(str(job["id"]),) for job in jobs],
                )
        return {
            "jobs": len(jobs),
            "logs": deleted_logs,
            "log_bytes": deleted_bytes,
        }

    def _safe_log_path(self, job: dict[str, Any]) -> Path:
        root = self.logs_dir.resolve()
        path = Path(str(job["log_path"])).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("日志路径超出控制面板日志目录") from exc
        return path

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["payload"] = json.loads(payload.pop("payload_json"))
        log_path = Path(str(payload["log_path"]))
        try:
            log_size = log_path.stat().st_size if log_path.is_file() else 0
        except OSError:
            log_size = 0
        payload["has_log"] = log_size > 0
        payload["log_size"] = log_size
        return payload


class WorkflowWorker:
    def __init__(
        self,
        project_root: Path,
        store: JobStore,
        scanner: WorkflowScanner,
        publisher: BiliupIntegration,
    ) -> None:
        self.project_root = project_root.resolve()
        self.store = store
        self.scanner = scanner
        self.publisher = publisher
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="workflow-worker")
        self._process_lock = threading.Lock()
        self._current_process: subprocess.Popen[bytes] | None = None
        self._current_job_id: str | None = None
        self._cancel_requested: set[str] = set()

    def start(self) -> None:
        if not self._thread.is_alive():
            self._thread.start()

    def wake(self) -> None:
        self._wake_event.set()

    def close(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        with self._process_lock:
            process = self._current_process
        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job["status"] == "queued":
            return self.store.update(
                job_id,
                status="cancelled",
                step="已取消",
                progress=0,
                exit_code=0,
                error="",
                finished_at=utc_now(),
            )
        if job["status"] != "running":
            raise ValueError("只有运行中或排队中的任务可以终止")
        with self._process_lock:
            self._cancel_requested.add(job_id)
            process = self._current_process if self._current_job_id == job_id else None
        self.store.update(job_id, step="正在终止")
        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)
        self._wake_event.set()
        return self.store.get(job_id)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            job = self.store.claim_next()
            if job is None:
                self._wake_event.wait(timeout=1.0)
                self._wake_event.clear()
                continue
            self._execute(job)

    def _execute(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        log_path = Path(job["log_path"])
        publish_task: Path | None = None
        with self._process_lock:
            self._current_job_id = job_id
        try:
            self._raise_if_cancelled(job_id)
            if job["kind"] == "publish":
                publish_task = self.scanner.resolve_task(str(job["target"]))
                original_payload = job["payload"]
                job["payload"] = self.publisher.prepare_payload_for_execution(
                    original_payload
                )
                if job["payload"] != original_payload:
                    self._append_log(
                        log_path,
                        "\n[投稿预检] 已按哔哩哔哩的字符计数规则自动修正"
                        "标题、简介或空间动态；无需重新编辑旧任务。\n",
                    )
                self.publisher.mark_running(publish_task, job["payload"])
            commands = self._build_commands(job)
            total = max(1, len(commands))
            for index, (label, command) in enumerate(commands):
                self._raise_if_cancelled(job_id)
                if self._stop_event.is_set():
                    raise RuntimeError("控制面板正在关闭，任务已停止")
                start_progress = int(index / total * 100)
                self.store.update(
                    job_id,
                    step=label,
                    progress=start_progress,
                )
                self._append_log(log_path, f"\n===== {label} =====\n")
                if job["kind"] == "publish" and label == "上传并提交到哔哩哔哩":
                    assert publish_task is not None
                    media = self.publisher.expected_hardsub(publish_task)
                    if not media.is_file() or media.stat().st_size == 0:
                        raise FileNotFoundError(f"投稿视频不存在或为空：{media}")
                exit_code = self._run_command(command, log_path)
                self._raise_if_cancelled(job_id)
                if exit_code != 0:
                    if job["kind"] == "publish":
                        raise RuntimeError(
                            self.publisher.explain_upload_failure(
                                self.store.log_tail(job_id, max_chars=100000),
                                exit_code,
                            )
                        )
                    raise RuntimeError(f"{label}失败，退出代码 {exit_code}")
                self.store.update(
                    job_id,
                    progress=int((index + 1) / total * 100),
                )
            if job["kind"] == "publish":
                assert publish_task is not None
                try:
                    self.publisher.mark_published(
                        publish_task,
                        job["payload"],
                        self.store.log_tail(job_id, max_chars=100000),
                    )
                except Exception as exc:
                    self._append_log(
                        log_path,
                        f"\n[警告] 投稿命令已成功，但本地投稿状态保存失败：{exc}\n",
                    )
                    self.store.update(
                        job_id,
                        status="completed",
                        step="投稿成功，本地状态保存失败",
                        progress=100,
                        exit_code=0,
                        error=str(exc),
                        finished_at=utc_now(),
                    )
                    return
            self.store.update(
                job_id,
                status="completed",
                step="已完成",
                progress=100,
                exit_code=0,
                finished_at=utc_now(),
            )
        except JobCancelled:
            self._append_log(log_path, "\n[任务已终止] 用户从控制面板终止了这个任务。\n")
            if job["kind"] == "publish" and publish_task is not None:
                try:
                    self.publisher.mark_failed(publish_task, job["payload"], "用户终止任务")
                except Exception:
                    pass
            self.store.update(
                job_id,
                status="cancelled",
                step="已终止",
                exit_code=0,
                error="",
                finished_at=utc_now(),
            )
        except Exception as exc:  # Worker must survive individual task failures.
            self._append_log(log_path, f"\n[任务失败] {exc}\n")
            if job["kind"] == "publish" and publish_task is not None:
                try:
                    self.publisher.mark_failed(publish_task, job["payload"], str(exc))
                except Exception:
                    pass
            self.store.update(
                job_id,
                status="failed",
                step="执行失败",
                exit_code=1,
                error=str(exc),
                finished_at=utc_now(),
            )
        finally:
            with self._process_lock:
                if self._current_job_id == job_id:
                    self._current_job_id = None
                self._cancel_requested.discard(job_id)

    def _build_commands(self, job: dict[str, Any]) -> list[tuple[str, list[str]]]:
        payload = job["payload"]
        if job["kind"] == "download":
            python = self.project_root / ".venv" / "Scripts" / "python.exe"
            command = [
                str(python),
                str(self.project_root / "src" / "download_video.py"),
                "--url",
                str(payload["url"]),
                "--confirm-rights",
                "--rights-status",
                "PERMISSION_GRANTED",
            ]
            return [("下载视频、字幕、元数据与音频", command)]

        if job["kind"] == "publish":
            task_dir = self.scanner.resolve_task(str(job["target"]))
            commands: list[tuple[str, list[str]]] = []
            if payload.get("prepare_hardsub"):
                stage3_python = self.project_root / ".venv_stage3" / "Scripts" / "python.exe"
                commands.append(
                    (
                        "生成投稿用硬字幕 MP4",
                        [
                            str(stage3_python),
                            str(self.project_root / "src" / "run_stage4.py"),
                            "--video-dir",
                            str(task_dir),
                            "--mode",
                            "hardsub",
                            "--resume",
                        ],
                    )
                )
            commands.append(
                (
                    "上传并提交到哔哩哔哩",
                    self.publisher.build_upload_command(task_dir, payload),
                )
            )
            return commands

        if job["kind"] != "pipeline":
            raise ValueError(f"未知任务类型：{job['kind']}")

        task_dir = self.scanner.resolve_task(str(job["target"]))
        stage3_python = self.project_root / ".venv_stage3" / "Scripts" / "python.exe"
        steps = str(payload.get("workflow") or "complete")
        commands: list[tuple[str, list[str]]] = []
        if steps in {"subtitles", "complete"}:
            commands.append(
                (
                    "生成并选择最佳英文字幕",
                    [
                        str(stage3_python),
                        str(self.project_root / "src" / "run_stage3.py"),
                        "--video-dir",
                        str(task_dir),
                        "--steps",
                        "select",
                        "--subtitle-source",
                        "auto",
                        "--resume",
                    ],
                )
            )
            commands.append(
                (
                    "翻译并检查中文字幕",
                    [
                        str(stage3_python),
                        str(self.project_root / "src" / "run_stage3.py"),
                        "--video-dir",
                        str(task_dir),
                        "--steps",
                        "translate",
                        "--resume",
                        "--allow-paid-api",
                    ],
                )
            )
        if steps in {"render", "complete"}:
            mode = str(payload.get("render_mode") or "softsub")
            if mode not in {"ass", "softsub", "hardsub", "both"}:
                raise ValueError("不支持的成片模式")
            commands.append(
                (
                    "生成并质检双语成片",
                    [
                        str(stage3_python),
                        str(self.project_root / "src" / "run_stage4.py"),
                        "--video-dir",
                        str(task_dir),
                        "--mode",
                        mode,
                        "--resume",
                    ],
                )
            )
        return commands

    def _run_command(self, command: list[str], log_path: Path) -> int:
        executable = Path(command[0])
        if not executable.is_file():
            raise FileNotFoundError(f"缺少运行环境：{executable}")
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            cwd=self.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            bufsize=0,
            shell=False,
            env=environment,
            creationflags=creation_flags,
        )
        with self._process_lock:
            self._current_process = process
        try:
            assert process.stdout is not None
            with log_path.open("a", encoding="utf-8") as handle:
                for raw_line in process.stdout:
                    line = self._decode_process_output(raw_line)
                    handle.write(line)
                    handle.flush()
            return process.wait()
        finally:
            with self._process_lock:
                self._current_process = None

    def _raise_if_cancelled(self, job_id: str) -> None:
        with self._process_lock:
            cancelled = job_id in self._cancel_requested
        if cancelled:
            raise JobCancelled()

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=10,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
            return
        process.terminate()

    @staticmethod
    def _decode_process_output(raw: bytes | str) -> str:
        if isinstance(raw, str):
            return raw
        for encoding in ("utf-8", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _append_log(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)


__all__ = ["JobCancelled", "JobStore", "WorkflowWorker"]
