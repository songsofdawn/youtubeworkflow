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

    def log_tail(self, job_id: str, max_chars: int = 30000) -> str:
        job = self.get(job_id)
        path = Path(job["log_path"])
        if not path.is_file():
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max_chars:]

    @staticmethod
    def _serialize(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["payload"] = json.loads(payload.pop("payload_json"))
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
        self._current_process: subprocess.Popen[str] | None = None

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
            process.terminate()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

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
        try:
            if job["kind"] == "publish":
                publish_task = self.scanner.resolve_task(str(job["target"]))
                self.publisher.mark_running(publish_task, job["payload"])
            commands = self._build_commands(job)
            total = max(1, len(commands))
            for index, (label, command) in enumerate(commands):
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
                if exit_code != 0:
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
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            cwd=self.project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            env=environment,
            creationflags=creation_flags,
        )
        with self._process_lock:
            self._current_process = process
        try:
            assert process.stdout is not None
            with log_path.open("a", encoding="utf-8") as handle:
                for line in process.stdout:
                    handle.write(line)
                    handle.flush()
            return process.wait()
        finally:
            with self._process_lock:
                self._current_process = None

    @staticmethod
    def _append_log(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)


__all__ = ["JobStore", "WorkflowWorker"]
