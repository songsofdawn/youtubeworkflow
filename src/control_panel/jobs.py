from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ..dubbing.config import load_dubbing_config, resolve_dubbing_python
from ..dubbing.runtime import build_dubbing_subprocess_env
from ..portable_runtime import load_portable_manifest, resolve_python_executable
from .dubbing_worker import (
    DubbingWorkerCrashed,
    DubbingWorkerStartError,
    PersistentDubbingWorkerClient,
)
from .publishing import BiliupIntegration
from .tasks import (
    WorkflowScanner,
    no_english_subtitle_or_recognized_speech,
    read_json,
    youtube_chinese_path,
)


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
                    resource_class TEXT NOT NULL DEFAULT '',
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
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "resource_class" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN resource_class TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                UPDATE jobs
                SET resource_class = CASE kind
                    WHEN 'download' THEN 'network'
                    WHEN 'pipeline' THEN 'gpu_heavy'
                    WHEN 'publish' THEN 'upload'
                    ELSE 'general'
                END
                WHERE resource_class = ''
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
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_jobs_resource_scheduler
                ON jobs(status, kind, resource_class, target, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def enqueue(
        self,
        kind: str,
        target: str,
        payload: dict[str, Any],
        *,
        resource_class: str | None = None,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        log_path = self.logs_dir / f"{job_id}.log"
        default_resources = {
            "download": "network",
            "pipeline": "gpu_heavy",
            "publish": "upload",
        }
        record = {
            "id": job_id,
            "kind": kind,
            "target": target,
            "payload_json": json.dumps(payload, ensure_ascii=False),
            "resource_class": str(
                resource_class or default_resources.get(kind) or "general"
            ),
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
                (id, kind, target, payload_json, resource_class, status, step, progress, created_at, log_path)
                VALUES (:id, :kind, :target, :payload_json, :resource_class, :status, :step, :progress, :created_at, :log_path)
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

    def publish_completion_stats(self, since: str) -> dict[str, Any]:
        """Return successful publish count since a boundary and latest success."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    SUM(
                        CASE
                            WHEN status = 'completed'
                             AND exit_code = 0
                             AND finished_at >= ?
                            THEN 1 ELSE 0
                        END
                    ) AS completed_since,
                    MAX(
                        CASE
                            WHEN status = 'completed' AND exit_code = 0
                            THEN finished_at ELSE ''
                        END
                    ) AS latest_completed_at,
                    MAX(
                        CASE
                            WHEN status = 'failed' AND error LIKE '%137022%'
                            THEN finished_at ELSE ''
                        END
                    ) AS latest_rate_limited_at
                FROM jobs
                WHERE kind = 'publish'
                """,
                (since,),
            ).fetchone()
        return {
            "completed_since": int(row["completed_since"] or 0),
            "latest_completed_at": str(row["latest_completed_at"] or ""),
            "latest_rate_limited_at": str(row["latest_rate_limited_at"] or ""),
        }

    def worker_state(self, key: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM worker_state WHERE key = ?",
                (str(key),),
            ).fetchone()
        return str(row["value"] or "") if row is not None else ""

    def set_worker_state(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO worker_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (str(key), str(value), utc_now()),
            )

    def update_queued_publish_step(self, step: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET step = ?
                WHERE kind = 'publish'
                  AND resource_class = 'upload'
                  AND status = 'queued'
                  AND step != ?
                """,
                (str(step), str(step)),
            )
        return int(cursor.rowcount)

    def claim_next(
        self,
        kinds: set[str] | None = None,
        resource_classes: set[str] | None = None,
    ) -> dict[str, Any] | None:
        normalized_kinds = sorted({str(kind) for kind in (kinds or set()) if str(kind)})
        normalized_resources = sorted(
            {
                str(resource)
                for resource in (resource_classes or set())
                if str(resource)
            }
        )
        kind_clause = ""
        resource_clause = ""
        parameters: list[Any] = []
        if normalized_kinds:
            placeholders = ", ".join("?" for _ in normalized_kinds)
            kind_clause = f"AND queued.kind IN ({placeholders})"
            parameters.extend(normalized_kinds)
        if normalized_resources:
            placeholders = ", ".join("?" for _ in normalized_resources)
            resource_clause = f"AND queued.resource_class IN ({placeholders})"
            parameters.extend(normalized_resources)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"""
                SELECT queued.id
                FROM jobs AS queued
                WHERE queued.status = 'queued'
                  {kind_clause}
                  {resource_clause}
                  AND NOT EXISTS (
                      SELECT 1
                      FROM jobs AS running
                      WHERE running.status = 'running'
                        AND running.target = queued.target
                  )
                ORDER BY queued.created_at
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs
                SET status = 'running', step = '正在准备',
                    started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                    finished_at = '', error = '', exit_code = NULL
                WHERE id = ?
                """,
                (utc_now(), row["id"]),
            )
            connection.commit()
        return self.get(str(row["id"]))

    def queued(
        self,
        *,
        resource_class: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clause = "AND resource_class = ?" if resource_class else ""
        parameters: tuple[Any, ...] = (str(resource_class),) if resource_class else ()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE status = 'queued' {clause}
                ORDER BY created_at
                LIMIT {max(1, min(int(limit), 2000))}
                """,
                parameters,
            ).fetchall()
        return [self._serialize(row) for row in rows]

    def claim_id(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT queued.id
                FROM jobs AS queued
                WHERE queued.id = ? AND queued.status = 'queued'
                  AND NOT EXISTS (
                      SELECT 1 FROM jobs AS running
                      WHERE running.status = 'running'
                        AND running.target = queued.target
                  )
                """,
                (str(job_id),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs
                SET status = 'running', step = '正在准备',
                    started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                    finished_at = '', error = '', exit_code = NULL
                WHERE id = ? AND status = 'queued'
                """,
                (utc_now(), str(job_id)),
            )
            connection.commit()
        return self.get(str(job_id))

    def cancel_if_queued(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', step = '已取消', progress = 0,
                    exit_code = 0, error = '', finished_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (utc_now(), job_id),
            )
            connection.commit()
        return self.get(job_id) if cursor.rowcount else None

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

    def replace_payload(
        self,
        job_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET payload_json = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), job_id),
            )
        return self.get(job_id)

    def requeue_stage(
        self,
        job_id: str,
        *,
        payload: dict[str, Any],
        resource_class: str,
        step: str,
        progress: int,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET payload_json = ?, resource_class = ?, status = 'queued',
                    step = ?, progress = ?, finished_at = '', exit_code = NULL,
                    error = ''
                WHERE id = ? AND status = 'running'
                """,
                (
                    json.dumps(payload, ensure_ascii=False),
                    resource_class,
                    step,
                    max(0, min(int(progress), 100)),
                    job_id,
                ),
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
        active_jobs = [
            job for job in jobs if job["status"] in {"queued", "running"}
        ]
        inactive_jobs = [
            job for job in jobs if job["status"] not in {"queued", "running"}
        ]
        active_paths = {
            self._safe_log_path(job)
            for job in active_jobs
        }
        deleted = 0
        deleted_bytes = 0
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
        deleted_results = 0
        result_root = (self.database_path.parent / "discovery_results").resolve()
        active_result_names = {
            f"{job['id']}.json"
            for job in active_jobs
            if job["kind"] == "discovery"
        }
        if result_root.is_dir():
            for candidate in result_root.glob("*.json"):
                try:
                    path = candidate.resolve()
                    path.relative_to(result_root)
                except (OSError, ValueError):
                    continue
                if path.name in active_result_names or not path.is_file():
                    continue
                deleted_bytes += path.stat().st_size
                path.unlink()
                deleted_results += 1
        if inactive_jobs:
            with self._connect() as connection:
                connection.executemany(
                    "DELETE FROM jobs WHERE id = ?",
                    [(str(job["id"]),) for job in inactive_jobs],
                )
        return {
            "deleted": deleted,
            "deleted_logs": deleted,
            "deleted_results": deleted_results,
            "deleted_jobs": len(inactive_jobs),
            "bytes": deleted_bytes,
            "skipped_active": len(active_jobs),
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
    DOWNLOAD_WORKERS = 2
    GPU_HEAVY_WORKERS = 1
    DEEPSEEK_WORKERS = 2
    UPLOAD_WORKERS = 1
    PUBLISH_COOLDOWN_STATE_KEY = "publish_cooldown_until"

    def __init__(
        self,
        project_root: Path,
        store: JobStore,
        scanner: WorkflowScanner,
        publisher: BiliupIntegration,
        discovery_runner: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.store = store
        self.scanner = scanner
        self.publisher = publisher
        self.discovery_runner = discovery_runner
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        profile = load_portable_manifest(self.project_root)
        is_cpu = bool(
            profile.get("portable")
            and str(profile.get("asr_device") or "").casefold() == "cpu"
        )
        self.max_active_processes = 3 if is_cpu else 4
        self._global_slots = threading.BoundedSemaphore(self.max_active_processes)
        download_threads = [
            threading.Thread(
                target=self._run,
                args=({"download"}, {"network"}),
                daemon=True,
                name=f"download-worker-{index + 1}",
            )
            for index in range(self.DOWNLOAD_WORKERS)
        ]
        gpu_threads = [
            threading.Thread(
                target=self._run,
                args=({"pipeline", "publish", "discovery"}, {"gpu_heavy"}),
                daemon=True,
                name=f"gpu-heavy-worker-{index + 1}",
            )
            for index in range(self.GPU_HEAVY_WORKERS)
        ]
        deepseek_threads = [
            threading.Thread(
                target=self._run,
                args=({"pipeline"}, {"paid_api"}),
                daemon=True,
                name=f"deepseek-worker-{index + 1}",
            )
            for index in range(self.DEEPSEEK_WORKERS)
        ]
        upload_threads = [
            threading.Thread(
                target=self._run,
                args=({"publish"}, {"upload"}),
                daemon=True,
                name=f"upload-worker-{index + 1}",
            )
            for index in range(self.UPLOAD_WORKERS)
        ]
        self._threads = [
            *download_threads,
            *gpu_threads,
            *deepseek_threads,
            *upload_threads,
        ]
        self._process_lock = threading.Lock()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._cancel_requested: set[str] = set()
        self._dubbing_worker_client: PersistentDubbingWorkerClient | None = None
        self._cookie_work_dir = self.project_root / "work" / "cookies"
        self._cleanup_stale_cookie_copies()

    def start(self) -> None:
        for thread in self._threads:
            if thread.ident is None:
                thread.start()

    def wake(self) -> None:
        self._wake_event.set()

    @staticmethod
    def initial_resource(kind: str, payload: dict[str, Any]) -> str:
        if kind == "download":
            return "network"
        if kind == "pipeline":
            return "gpu_heavy"
        if kind == "publish":
            return "gpu_heavy" if payload.get("prepare_hardsub") else "upload"
        if kind == "discovery":
            return "gpu_heavy"
        return "general"

    @staticmethod
    def _automation_enabled(payload: dict[str, Any]) -> bool:
        """Accept new policy payloads and legacy auto_publish jobs."""
        return bool(payload.get("automation_enabled") or payload.get("auto_publish"))

    @staticmethod
    def _automation_target(payload: dict[str, Any]) -> str:
        target = str(payload.get("automation_target") or "").strip().casefold()
        if target in {"subtitles", "render", "publish"}:
            return target
        return "publish" if payload.get("auto_publish") else ""

    def snapshot(self, jobs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        rows = jobs if jobs is not None else self.store.list(limit=200)
        publish_guard = self.publish_guard()
        running = {
            "network": 0,
            "gpu_heavy": 0,
            "paid_api": 0,
            "upload": 0,
        }
        for job in rows:
            resource = str(job.get("resource_class") or "")
            if job.get("status") == "running" and resource in running:
                running[resource] += 1
        return {
            "mode": "stage_pipeline_v0.5",
            "global": {
                "running": sum(running.values()),
                "capacity": self.max_active_processes,
            },
            "resources": {
                "network": {"running": running["network"], "capacity": self.DOWNLOAD_WORKERS},
                "gpu_heavy": {"running": running["gpu_heavy"], "capacity": self.GPU_HEAVY_WORKERS},
                "paid_api": {"running": running["paid_api"], "capacity": self.DEEPSEEK_WORKERS},
                "upload": {"running": running["upload"], "capacity": self.UPLOAD_WORKERS},
            },
            "publishing": publish_guard,
        }

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def publish_guard(self, now: datetime | None = None) -> dict[str, Any]:
        """Describe the persistent upload guard without claiming a queue job."""
        now_utc = now or datetime.now(timezone.utc)
        if now_utc.tzinfo is None:
            now_utc = now_utc.replace(tzinfo=timezone.utc)
        now_utc = now_utc.astimezone(timezone.utc)
        local_now = now_utc.astimezone()
        local_day_start = local_now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        next_local_day = local_day_start + timedelta(days=1)
        stats = self.store.publish_completion_stats(
            local_day_start.astimezone(timezone.utc).isoformat()
        )
        daily_limit = self.publisher.publish_daily_limit()
        minimum_interval = self.publisher.publish_min_interval_seconds()
        barriers: list[tuple[datetime, str]] = []

        cooldown_until = self._parse_datetime(
            self.store.worker_state(self.PUBLISH_COOLDOWN_STATE_KEY)
        )
        if cooldown_until is not None and cooldown_until > now_utc:
            barriers.append(
                (cooldown_until, "B 站返回 137022，投稿接口正在冷却")
            )
        latest_rate_limited = self._parse_datetime(
            str(stats["latest_rate_limited_at"])
        )
        if latest_rate_limited is not None:
            recovered_cooldown = latest_rate_limited + timedelta(
                seconds=self.publisher.publish_rate_limit_cooldown_seconds()
            )
            if recovered_cooldown > now_utc:
                barriers.append(
                    (recovered_cooldown, "检测到已有 137022 失败，投稿接口正在冷却")
                )

        completed_today = int(stats["completed_since"])
        if daily_limit > 0 and completed_today >= daily_limit:
            barriers.append(
                (
                    next_local_day.astimezone(timezone.utc),
                    f"今日已成功投稿 {completed_today}/{daily_limit}",
                )
            )

        latest_completed = self._parse_datetime(str(stats["latest_completed_at"]))
        if latest_completed is not None and minimum_interval > 0:
            interval_until = latest_completed + timedelta(seconds=minimum_interval)
            if interval_until > now_utc:
                minutes = max(1, round(minimum_interval / 60))
                barriers.append(
                    (interval_until, f"投稿安全间隔为 {minutes} 分钟")
                )

        if not barriers:
            return {
                "active": False,
                "step": "",
                "resume_at": "",
                "wait_seconds": 0,
                "completed_today": completed_today,
                "daily_limit": daily_limit,
                "minimum_interval_seconds": minimum_interval,
            }

        resume_at, reason = max(barriers, key=lambda item: item[0])
        local_resume = resume_at.astimezone()
        step = (
            f"投稿保护：{reason}；"
            f"{local_resume.strftime('%m-%d %H:%M')} 后自动恢复"
        )
        return {
            "active": True,
            "step": step,
            "resume_at": resume_at.isoformat(),
            "wait_seconds": max(0, int((resume_at - now_utc).total_seconds())),
            "completed_today": completed_today,
            "daily_limit": daily_limit,
            "minimum_interval_seconds": minimum_interval,
        }

    def _activate_publish_cooldown(self) -> dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        requested_until = now_utc + timedelta(
            seconds=self.publisher.publish_rate_limit_cooldown_seconds()
        )
        previous_until = self._parse_datetime(
            self.store.worker_state(self.PUBLISH_COOLDOWN_STATE_KEY)
        )
        cooldown_until = max(
            requested_until,
            previous_until or requested_until,
        )
        self.store.set_worker_state(
            self.PUBLISH_COOLDOWN_STATE_KEY,
            cooldown_until.isoformat(),
        )
        return self.publish_guard(now_utc)

    def close(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        self._shutdown_dubbing_worker("Control panel is shutting down; releasing VoxCPM2.")
        with self._process_lock:
            processes = list(self._processes.values())
        for process in processes:
            if process.poll() is None:
                self._terminate_process_tree(process)
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=5)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job["status"] == "queued":
            cancelled = self.store.cancel_if_queued(job_id)
            if cancelled is not None:
                return cancelled
            job = self.store.get(job_id)
        if job["status"] != "running":
            raise ValueError("只有运行中或排队中的任务可以终止")
        with self._process_lock:
            self._cancel_requested.add(job_id)
            process = self._processes.get(job_id)
        self.store.update(job_id, step="正在终止")
        if process is not None and process.poll() is None:
            self._terminate_process_tree(process)
        self._wake_event.set()
        return self.store.get(job_id)

    def _run(self, kinds: set[str], resource_classes: set[str]) -> None:
        while not self._stop_event.is_set():
            if kinds == {"publish"} and resource_classes == {"upload"}:
                guard = self.publish_guard()
                if guard["active"]:
                    self.store.update_queued_publish_step(str(guard["step"]))
                    self._wake_event.wait(
                        timeout=max(
                            0.5,
                            min(float(guard["wait_seconds"] or 0.5), 30.0),
                        )
                    )
                    self._wake_event.clear()
                    continue
            if not self._global_slots.acquire(timeout=0.5):
                continue
            job = self.store.claim_next(kinds, resource_classes)
            if job is None:
                self._global_slots.release()
                self._wake_event.wait(timeout=0.5)
                self._wake_event.clear()
                continue
            try:
                current_job: dict[str, Any] | None = job
                while current_job is not None:
                    self._execute(current_job)
                    if resource_classes != {"gpu_heavy"} or not self._dubbing_model_warm():
                        current_job = None
                        continue
                    next_job = self._claim_next_warm_dubbing_job()
                    if next_job is None:
                        queued_gpu = self.store.queued(
                            resource_class="gpu_heavy",
                            limit=1,
                        )
                        reason = (
                            "Next GPU-heavy task is not dubbing; releasing VoxCPM2."
                            if queued_gpu
                            else "No consecutive dubbing job is queued; releasing VoxCPM2."
                        )
                        self._shutdown_dubbing_worker(reason)
                        current_job = None
                        continue
                    self._append_log(
                        Path(current_job["log_path"]),
                        "\n[DUBBING] Keeping VoxCPM2 warm for next dubbing job.\n",
                    )
                    self._append_log(
                        Path(next_job["log_path"]),
                        "\n[DUBBING] Reusing the persistent dubbing worker while the "
                        "GPU-heavy slot remains acquired.\n",
                    )
                    current_job = next_job
            finally:
                self._global_slots.release()
                self._wake_event.set()

    def _dubbing_model_warm(self) -> bool:
        client = self._dubbing_worker_client
        return bool(client is not None and client.alive and client.loaded_model)

    def _shutdown_dubbing_worker(self, reason: str) -> None:
        client = self._dubbing_worker_client
        self._dubbing_worker_client = None
        if client is None:
            return
        try:
            client.shutdown(reason)
        except Exception:
            client.terminate()

    def _job_is_current_dubbing_stage(self, job: dict[str, Any]) -> bool:
        current = self._current_job_stage(job)
        return bool(
            current is not None
            and current[0] == "生成中文 AI 配音"
            and current[2] == "gpu_heavy"
        )

    def _current_job_stage(
        self,
        job: dict[str, Any],
    ) -> tuple[str, list[str], str] | None:
        if job.get("kind") != "pipeline" or job.get("resource_class") != "gpu_heavy":
            return None
        try:
            stages = self._build_stages(job)
            stage_index = max(0, int(job.get("payload", {}).get("_stage_index") or 0))
        except (OSError, RuntimeError, ValueError, IndexError):
            return None
        return stages[stage_index] if stage_index < len(stages) else None

    def _claim_next_warm_dubbing_job(self) -> dict[str, Any] | None:
        for candidate in self.store.queued(resource_class="gpu_heavy"):
            stage = self._current_job_stage(candidate)
            if stage is None:
                return None
            if stage[0] == "生成并质检中文配音成片":
                # A completed dub's own render continuation is deliberately
                # deferred until the consecutive dubbing batch releases VoxCPM2.
                continue
            if stage[0] != "生成中文 AI 配音":
                # Do not jump over Whisper, discovery, encoding, or another
                # GPU-heavy stage merely to keep the model resident.
                return None
            claimed = self.store.claim_id(str(candidate["id"]))
            if claimed is not None:
                return claimed
        return None

    def _execute(self, job: dict[str, Any]) -> None:
        job_id = str(job["id"])
        log_path = Path(job["log_path"])
        publish_task: Path | None = None
        cookie_copy: Path | None = None
        try:
            self._raise_if_cancelled(job_id)
            try:
                stage_index = max(0, int(job["payload"].get("_stage_index") or 0))
            except (TypeError, ValueError):
                stage_index = 0
            if job["kind"] == "discovery":
                self._execute_discovery(job, log_path)
                return
            if job["kind"] == "download":
                cookie_copy = self._create_cookie_copy(job_id)
                if cookie_copy is not None:
                    self._append_log(
                        log_path,
                        "[下载隔离] 已为此任务创建独立的 YouTube Cookie 副本。\n",
                    )
            if job["kind"] == "publish":
                publish_task = self.scanner.resolve_task(str(job["target"]))
                if stage_index == 0:
                    original_payload = job["payload"]
                    job["payload"] = self.publisher.prepare_payload_for_execution(
                        original_payload
                    )
                    if job["payload"] != original_payload:
                        self.store.replace_payload(job_id, job["payload"])
                        self._append_log(
                            log_path,
                            "\n[投稿预检] 已按哔哩哔哩的字符计数规则自动修正"
                            "标题、简介或空间动态；无需重新编辑旧任务。\n",
                        )
                    self.publisher.mark_running(publish_task, job["payload"])

            stages = self._build_stages(job)
            if not stages:
                raise RuntimeError("任务没有可执行步骤")
            if stage_index >= len(stages):
                raise RuntimeError(
                    f"任务步骤游标无效：{stage_index}/{len(stages)}"
                )
            total = len(stages)
            label, command, resource_class = stages[stage_index]
            if str(job.get("resource_class") or "") != resource_class:
                self.store.requeue_stage(
                    job_id,
                    payload=job["payload"],
                    resource_class=resource_class,
                    step=f"等待资源：{label}",
                    progress=int(stage_index / total * 100),
                )
                self._append_log(
                    log_path,
                    f"\n[资源调度] 已转入 {resource_class} 队列：{label}\n",
                )
                return
            if cookie_copy is not None:
                command = [*command, "--cookies-path", str(cookie_copy)]
            self._raise_if_cancelled(job_id)
            if self._stop_event.is_set():
                raise RuntimeError("控制面板正在关闭，任务已停止")
            self.store.update(
                job_id,
                step=label,
                progress=int(stage_index / total * 100),
            )
            self._append_log(
                log_path,
                f"\n===== {label} [{resource_class}] =====\n",
            )
            if (
                job["kind"] == "pipeline"
                and self._automation_enabled(job["payload"])
                and label == "生成并质检中文配音成片"
                and self._handle_unattended_dubbing_review(job, log_path=log_path)
            ):
                return
            if job["kind"] == "publish" and resource_class == "upload":
                assert publish_task is not None
                media = self.publisher.media_for_payload(
                    publish_task,
                    job["payload"],
                )
                if not media.is_file() or media.stat().st_size == 0:
                    raise FileNotFoundError(f"投稿视频不存在或为空：{media}")
            if job["kind"] == "publish" and resource_class == "upload":
                exit_code = self._run_publish_upload_with_retries(
                    job_id,
                    command,
                    log_path,
                )
            else:
                exit_code = self._run_command(
                    job_id,
                    command,
                    log_path,
                    stage_progress_start=stage_index / total * 100,
                    stage_progress_span=100 / total,
                )
            self._raise_if_cancelled(job_id)
            if exit_code != 0:
                if job["kind"] == "pipeline" and self._automation_enabled(
                    job["payload"]
                ):
                    if self._handle_unattended_dubbing_preflight_failure(
                        job,
                        label=label,
                        exit_code=exit_code,
                        log_path=log_path,
                        stage_index=stage_index,
                        total=total,
                        resource_class=resource_class,
                    ):
                        return
                    if self._continue_unattended_original_media_publish(
                        job,
                        label=label,
                        exit_code=exit_code,
                        log_path=log_path,
                    ):
                        return
                    if self._retry_unusable_youtube_chinese_with_api(
                        job,
                        label=label,
                        exit_code=exit_code,
                        log_path=log_path,
                    ):
                        return
                    if str(
                        job["payload"].get("automation_failure_policy") or "skip"
                    ) == "skip":
                        self._complete_unattended_pipeline_skip(
                            job,
                            label=label,
                            exit_code=exit_code,
                            log_path=log_path,
                        )
                        return
                if job["kind"] == "publish" and resource_class == "upload":
                    upload_log = self.store.log_tail(job_id, max_chars=100000)
                    if self.publisher.is_publish_rate_limited(upload_log):
                        guard = self._activate_publish_cooldown()
                        wait_reason = self.publisher.explain_upload_failure(
                            upload_log,
                            exit_code,
                        )
                        self.store.requeue_stage(
                            job_id,
                            payload=job["payload"],
                            resource_class="upload",
                            step=str(guard["step"]),
                            progress=int(stage_index / total * 100),
                        )
                        if publish_task is not None:
                            self.publisher.mark_waiting(
                                publish_task,
                                job["payload"],
                                reason=wait_reason,
                                resume_at=str(guard["resume_at"]),
                            )
                        self._append_log(
                            log_path,
                            "\n[投稿保护] B 站返回 137022；当前任务已保留，"
                            "全部投稿队列进入冷却，不会继续上传后续视频。\n"
                            f"[自动恢复] {guard['step']}\n",
                        )
                        return
                    raise RuntimeError(
                        self.publisher.explain_upload_failure(
                            upload_log,
                            exit_code,
                        )
                    )
                raise RuntimeError(f"{label}失败，退出代码 {exit_code}")

            if (
                job["kind"] == "pipeline"
                and self._automation_enabled(job["payload"])
                and label == "生成中文 AI 配音"
                and self._handle_unattended_dubbing_review(job, log_path=log_path)
            ):
                return

            completed_progress = int((stage_index + 1) / total * 100)
            self.store.update(job_id, progress=completed_progress)
            if stage_index + 1 < total:
                next_label, _, next_resource = stages[stage_index + 1]
                next_payload = dict(job["payload"])
                next_payload["_stage_index"] = stage_index + 1
                self.store.requeue_stage(
                    job_id,
                    payload=next_payload,
                    resource_class=next_resource,
                    step=f"等待资源：{next_label}",
                    progress=completed_progress,
                )
                self._append_log(
                    log_path,
                    f"\n[步骤完成] {label}\n"
                    f"[释放资源] {resource_class}\n"
                    f"[下一步] {next_label} [{next_resource}]\n",
                )
                return

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
            try:
                followup_step = ""
                if job["kind"] == "download" and self._automation_enabled(
                    job["payload"]
                ):
                    followup_step = self._queue_post_download_automation(job)
                elif (
                    job["kind"] == "pipeline"
                    and self._automation_target(job["payload"]) == "publish"
                ):
                    followup_step = self._queue_automatic_publish(job)
                if followup_step:
                    self.store.update(job_id, step=followup_step)
            except Exception as exc:
                self._append_log(log_path, f"\n[自动接力失败] {exc}\n")
                self.store.update(
                    job_id,
                    status="failed",
                    step="自动接力失败",
                    exit_code=1,
                    error=str(exc),
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
            current_stage = self._current_job_stage(job)
            current_stage_total = 1
            if current_stage is not None:
                try:
                    current_stage_total = max(1, len(self._build_stages(job)))
                except (OSError, RuntimeError, ValueError, IndexError):
                    current_stage = None
            if (
                job["kind"] == "pipeline"
                and self._automation_enabled(job["payload"])
                and current_stage is not None
                and self._handle_unattended_dubbing_preflight_failure(
                    job,
                    label=current_stage[0],
                    exit_code=1,
                    log_path=log_path,
                    stage_index=max(
                        0,
                        int(job.get("payload", {}).get("_stage_index") or 0),
                    ),
                    total=current_stage_total,
                    resource_class=current_stage[2],
                )
            ):
                return
            if (
                job["kind"] == "pipeline"
                and self._automation_enabled(job["payload"])
                and str(
                    job["payload"].get("automation_failure_policy") or "skip"
                ) == "skip"
            ):
                try:
                    self._complete_unattended_pipeline_skip(
                        job,
                        label="准备或执行自动化流程",
                        exit_code=1,
                        log_path=log_path,
                    )
                    return
                except Exception as skip_exc:
                    self._append_log(
                        log_path,
                        f"[自动跳过状态保存失败] {skip_exc}\n",
                    )
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
                self._processes.pop(job_id, None)
                self._cancel_requested.discard(job_id)
            if cookie_copy is not None:
                cookie_copy.unlink(missing_ok=True)

    def _task_reference_for_video_id(self, video_id: str) -> str:
        for task in self.scanner.scan():
            if str(task.get("video_id") or "") == str(video_id):
                return str(task["task"])
        raise FileNotFoundError(f"下载完成后未找到视频任务目录：{video_id}")

    @staticmethod
    def _last_manifest_error_code(manifest: dict[str, Any]) -> str:
        for item in reversed(list(manifest.get("errors") or [])):
            if isinstance(item, dict) and str(item.get("code") or "").strip():
                return str(item["code"]).strip()
        return ""

    @classmethod
    def _youtube_chinese_requires_api_fallback(cls, task_dir: Path) -> bool:
        stage4 = read_json(task_dir / "stage4" / "stage4_manifest.json")

        # 第一类：中文字幕本身不存在、损坏或无法结构化使用。
        if cls._last_manifest_error_code(stage4) in {
            "NO_VALID_CHINESE_SUBTITLE",
            "ZH_AUTO_SUBTITLE_UNUSABLE",
        }:
            return True

        # 第二类：YouTube 中文字幕虽然能够恢复/对齐，
        # 但最终双语字幕布局无法保证可读性。
        #
        # 这种情况下不要继续无限降低最小时长阈值，
        # 而是自动切换到 AI/API 翻译，再生成一次更干净、
        # 与英文 segment 一一对应的中文字幕。
        review = stage4.get("review")
        if isinstance(review, dict):
            review_code = str(review.get("code") or "").upper()

            issue_codes = {
                str(code).upper()
                for code in (review.get("issue_codes") or [])
            }

            if (
                review_code == "SUBTITLE_LAYOUT_REVIEW_REQUIRED"
                and (
                    "BILINGUAL_FRAGMENT_DURATION_TOO_SHORT" in issue_codes
                    or "BILINGUAL_LINE_TOO_WIDE" in issue_codes
                    or "BILINGUAL_TOO_MANY_LINES" in issue_codes
                )
            ):
                return True

        return False

    def _retry_unusable_youtube_chinese_with_api(
        self,
        job: dict[str, Any],
        *,
        label: str,
        exit_code: int,
        log_path: Path,
    ) -> bool:
        """Route a structurally unusable YouTube Chinese track to API translation."""
        payload = dict(job.get("payload") or {})
        if (
            label != "生成并质检双语成片"
            or str(payload.get("chinese_subtitle_source") or "") != "auto"
            or not payload.get("auto_translate_missing", True)
            or not payload.get("allow_paid_api")
        ):
            return False
        task_dir = self.scanner.resolve_task(str(job["target"]))
        if not self._youtube_chinese_requires_api_fallback(task_dir):
            return False

        payload["chinese_subtitle_source"] = "deepseek"
        payload["_stage_index"] = 1
        self.store.requeue_stage(
            str(job["id"]),
            payload=payload,
            resource_class="paid_api",
            step="等待资源：翻译并检查中文字幕",
            progress=25,
        )
        self._append_log(
            log_path,
            "\n[无人值守自动降级] YouTube 中文字幕无法与已选英文字幕"
            f"结构对齐（原退出码 {exit_code}），已自动改用 API 翻译。\n",
        )
        return True

    @staticmethod
    def _dubbing_preflight_diagnostic(log_path: Path) -> str:
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")[-12000:]
        except OSError:
            return ""
        lines = [
            line
            for line in text.splitlines()
            if any(
                marker in line.casefold()
                for marker in (
                    "[dubbing] preflight",
                    "torchcodec",
                    "ffmpeg shared",
                    "中文配音预检",
                    "persistent dubbing worker",
                )
            )
        ]
        return "\n".join(lines)[-4000:]

    def _handle_unattended_dubbing_preflight_failure(
        self,
        job: dict[str, Any],
        *,
        label: str,
        exit_code: int,
        log_path: Path,
        stage_index: int,
        total: int,
        resource_class: str,
    ) -> bool:
        """Retry transient runtime probes and keep permanent infra failures visible."""
        if label != "生成中文 AI 配音":
            return False
        diagnostic = self._dubbing_preflight_diagnostic(log_path)
        if not diagnostic:
            return False
        normalized = diagnostic.casefold()
        is_preflight = any(
            marker in normalized
            for marker in (
                "preflight",
                "torchcodec",
                "ffmpeg shared",
                "中文配音预检",
            )
        )
        if not is_preflight:
            return False

        payload = dict(job.get("payload") or {})
        try:
            retry_count = max(
                0,
                int(payload.get("_dubbing_preflight_retry_count") or 0),
            )
        except (TypeError, ValueError):
            retry_count = 0
        if "超时" in diagnostic or "timed out" in normalized:
            if retry_count < 1:
                payload["_dubbing_preflight_retry_count"] = retry_count + 1
                self.store.requeue_stage(
                    str(job["id"]),
                    payload=payload,
                    resource_class=resource_class,
                    step="配音运行时自检超时，正在自动重试",
                    progress=int(stage_index / max(1, total) * 100),
                )
                self._append_log(
                    log_path,
                    "\n[自动恢复] TorchCodec 配音运行时自检超时；"
                    "已启动一次全新自检进程并重新排队。\n",
                )
                return True

        task_dir = self.scanner.resolve_task(str(job["target"]))
        reason = "DUBBING_RUNTIME_PREFLIGHT_FAILED"
        message = "中文配音运行时预检失败，任务已保留为失败状态，等待修复后重试"
        self.publisher.mark_automation_failed(
            task_dir,
            reason,
            details={
                "stage_label": label,
                "process_exit_code": int(exit_code),
                "retry_count": retry_count,
                "diagnostic": diagnostic,
            },
        )
        self._append_log(
            log_path,
            f"\n[配音运行时失败] {message}：{reason}（退出代码 {exit_code}）。\n",
        )
        self.store.update(
            str(job["id"]),
            status="failed",
            step=message,
            progress=int(stage_index / max(1, total) * 100),
            exit_code=int(exit_code),
            error=reason,
            finished_at=utc_now(),
        )
        return True

    def _continue_unattended_original_media_publish(
        self,
        job: dict[str, Any],
        *,
        label: str,
        exit_code: int,
        log_path: Path,
    ) -> bool:
        """Reroute a safe unattended fallback to metadata-only original upload."""
        payload = dict(job.get("payload") or {})
        no_speech_fallback = bool(
            label == "生成并选择最佳英文字幕"
            and str(
                payload.get("automation_silent_video_policy") or "publish_original"
            ).strip().casefold()
            == "publish_original"
        )
        dubbing_render_fallback = bool(
            payload.get("dubbing_fallback")
            and label == "生成并质检双语成片"
        )
        if self._automation_target(payload) != "publish" or not (
            no_speech_fallback or dubbing_render_fallback
        ):
            return False
        task_dir = self.scanner.resolve_task(str(job["target"]))
        if no_speech_fallback and not no_english_subtitle_or_recognized_speech(task_dir):
            return False

        payload["silent_video_mode"] = True
        payload["publish_original_video"] = True
        payload["media_variant"] = "original"
        payload["dubbing_enabled"] = False
        payload["_stage_index"] = 0
        metadata_provider = str(
            payload.get("publish_metadata_provider") or "auto"
        ).strip().casefold()
        resource_class = (
            "paid_api" if metadata_provider == "translation_api" else "gpu_heavy"
        )
        if dubbing_render_fallback:
            reason = "DUBBING_FALLBACK_RENDER_FAILED"
            message = (
                "中文配音无法安全适配，原声字幕版成片也未通过检查；"
                "最终保留原始视频并继续无人值守投稿"
            )
        else:
            reason = "NO_NARRATION_OR_BACKGROUND_MUSIC"
            message = "未检测到可用语音；保留原画面和音轨，仅本地化投稿信息"
        self.publisher.mark_automation_original_media(
            task_dir,
            reason=reason,
            message=message,
        )
        self.store.requeue_stage(
            str(job["id"]),
            payload=payload,
            resource_class=resource_class,
            step="等待资源：生成无配音视频投稿信息",
            progress=45,
        )
        self._append_log(
            log_path,
            "\n[无人值守原视频降级] "
            f"{message}（原退出码 {exit_code}）。\n",
        )
        return True

    def _complete_unattended_pipeline_skip(
        self,
        job: dict[str, Any],
        *,
        label: str,
        exit_code: int,
        log_path: Path,
    ) -> None:
        """Turn a subtitle/render rejection into a terminal unattended skip.

        A single unusable video must not leave an unattended queue asking for
        human input.  The original process exit and manifests remain recorded
        in details so infrastructure and content failures are still auditable.
        """
        task_dir = self.scanner.resolve_task(str(job["target"]))
        stage3 = read_json(task_dir / "stage3_manifest.json")
        dubbing = read_json(task_dir / "dubbing" / "manifest.json")
        stage4 = read_json(task_dir / "stage4" / "stage4_manifest.json")
        review = stage4.get("review") if isinstance(stage4.get("review"), dict) else {}
        reason = str(review.get("code") or "").strip()
        if not reason:
            reason = self._last_manifest_error_code(stage4)
        if not reason:
            if (
                label == "生成并选择最佳英文字幕"
                and no_english_subtitle_or_recognized_speech(task_dir)
            ):
                reason = "NO_ENGLISH_SUBTITLE_OR_RECOGNIZED_SPEECH"
        if not reason:
            reason_by_label = {
                "生成并选择最佳英文字幕": "ENGLISH_SUBTITLE_STAGE_FAILED",
                "翻译并检查中文字幕": "CHINESE_TRANSLATION_STAGE_FAILED",
                "生成中文 AI 配音": "DUBBING_STAGE_FAILED",
                "生成并质检中文配音成片": "STAGE4_DUBBED_RENDER_STAGE_FAILED",
                "生成并质检双语成片": "STAGE4_RENDER_STAGE_FAILED",
            }
            reason = reason_by_label.get(label, "UNATTENDED_PIPELINE_STAGE_FAILED")
        details = {
            "stage_label": label,
            "process_exit_code": int(exit_code),
            "stage3_translation_status": str(
                stage3.get("translation_status") or ""
            ),
            "stage3_errors": list(stage3.get("errors") or []),
            "dubbing_status": str(dubbing.get("status") or ""),
            "dubbing_errors": list(dubbing.get("errors") or []),
            "stage4_status": str(stage4.get("status") or ""),
            "stage4_qc_status": str(stage4.get("qc_status") or ""),
            "stage4_errors": list(stage4.get("errors") or []),
            "review": review,
        }
        self.publisher.mark_automation_skipped(task_dir, reason, details=details)
        message = "字幕或成片未通过安全检查，已自动跳过此视频并继续队列"
        self._append_log(
            log_path,
            f"\n[无人值守自动跳过] {message}：{reason}（原退出代码 {exit_code}）\n",
        )
        self.store.update(
            str(job["id"]),
            status="completed",
            step=message,
            progress=100,
            exit_code=0,
            error="",
            finished_at=utc_now(),
        )

    def _handle_unattended_dubbing_review(
        self,
        job: dict[str, Any],
        *,
        log_path: Path,
    ) -> bool:
        """Apply the explicit unattended policy before a reviewed dub is rendered."""
        payload = dict(job.get("payload") or {})
        task_dir = self.scanner.resolve_task(str(job["target"]))
        details = self._dubbing_review_details(task_dir)
        if details is None:
            return False
        summary = str(details["message"])
        policy = str(
            payload.get("automation_dubbing_review_policy") or "auto_fallback"
        ).strip().casefold()
        if policy == "continue":
            self._append_log(
                log_path,
                f"\n[无人值守中配策略] {summary}；已按设置继续成片。\n",
            )
            return False
        if policy == "auto_fallback":
            payload.update(
                dubbing_enabled=False,
                force_dubbing=False,
                dubbing_fallback=True,
                media_variant="subtitled_original_audio",
                publish_original_video=False,
            )
            payload.pop("silent_video_mode", None)
            fallback_job = dict(job)
            fallback_job["payload"] = payload
            stages = self._build_stages(fallback_job)
            render_index = next(
                (
                    index
                    for index, stage in enumerate(stages)
                    if stage[0] == "生成并质检双语成片"
                ),
                -1,
            )
            if render_index < 0:
                raise RuntimeError("无法为配音复核任务建立原声字幕版成片阶段")
            label, _, resource_class = stages[render_index]
            payload["_stage_index"] = render_index
            self.publisher.mark_automation_fallback(
                task_dir,
                "DUBBING_TIMING_REVIEW_REQUIRED",
                media_variant="subtitled_original_audio",
                details=details,
            )
            self.store.requeue_stage(
                str(job["id"]),
                payload=payload,
                resource_class=resource_class,
                step=f"自动降级：等待{label}",
                progress=int(render_index / max(1, len(stages)) * 100),
            )
            self._append_log(
                log_path,
                "\n[无人值守中配自动降级] "
                f"{summary}；不会使用存在风险的中文配音，"
                "已改为生成保留原始音轨的中文字幕成片。\n",
            )
            return True

        failure_policy = str(
            payload.get("automation_failure_policy") or "skip"
        ).strip().casefold()
        if failure_policy != "skip":
            raise RuntimeError(
                f"{summary}；已阻止成片和投稿，并按设置保留失败状态"
            )

        self.publisher.mark_automation_skipped(
            task_dir,
            "DUBBING_TIMING_REVIEW_REQUIRED",
            details=details,
        )
        message = "中文配音需要复核，已阻止成片和投稿并继续队列"
        self._append_log(
            log_path,
            f"\n[无人值守自动跳过] {message}：{summary}\n",
        )
        self.store.update(
            str(job["id"]),
            status="completed",
            step=message,
            progress=100,
            exit_code=0,
            error="",
            finished_at=utc_now(),
        )
        return True

    @staticmethod
    def _dubbing_review_details(task_dir: Path) -> dict[str, Any] | None:
        manifest = read_json(task_dir / "dubbing" / "manifest.json")
        needs_review = bool(
            manifest.get("needs_review")
            or str(manifest.get("status") or "").upper()
            == "COMPLETED_WITH_REVIEW"
        )
        if not needs_review:
            return None

        review_rows = [
            row
            for row in manifest.get("segments") or []
            if isinstance(row, dict) and row.get("needs_review")
        ]
        review_count = len(review_rows)
        segment_count = int(manifest.get("segment_count") or 0)
        summary = (
            f"中文配音有 {review_count} / {segment_count} 个片段需要复核"
            if segment_count
            else "中文配音存在需要复核的时槽超限片段"
        )
        return {
            "message": summary,
            "dubbing_status": str(manifest.get("status") or ""),
            "segment_count": segment_count,
            "review_segment_count": review_count,
            "warnings": list(manifest.get("warnings") or [])[-20:],
        }

    def _queue_post_download_automation(self, job: dict[str, Any]) -> str:
        payload = dict(job["payload"])
        automation_target = self._automation_target(payload) or "publish"
        target = self._task_reference_for_video_id(str(job["target"]))
        task_dir = self.scanner.resolve_task(target)
        has_youtube_chinese = youtube_chinese_path(task_dir) is not None
        if (
            not has_youtube_chinese
            and not payload.get("auto_translate_missing", True)
            and str(payload.get("automation_failure_policy") or "skip") == "skip"
            and not (
                automation_target == "publish"
                and str(
                    payload.get("automation_silent_video_policy")
                    or "publish_original"
                ).strip().casefold()
                == "publish_original"
            )
        ):
            self.publisher.mark_automation_skipped(
                task_dir,
                "YOUTUBE_CHINESE_SUBTITLE_NOT_FOUND",
                details={"message": "没有 YouTube 中文字幕，且自动 API 翻译已关闭"},
            )
            return "下载完成；无中文字幕，已按无人值守设置跳过"
        self.store.enqueue(
            "pipeline",
            target,
            {
                "workflow": (
                    "subtitles" if automation_target == "subtitles" else "complete"
                ),
                "render_mode": str(payload.get("render_mode") or "hardsub"),
                "chinese_subtitle_source": str(
                    payload.get("chinese_subtitle_source") or "auto"
                ),
                "whisper_for_auto_subtitles": bool(
                    payload.get("whisper_for_auto_subtitles", True)
                ),
                "english_subtitle_policy": str(
                    payload.get("english_subtitle_policy") or "quality"
                ),
                "auto_translate_missing": bool(
                    payload.get("auto_translate_missing", True)
                ),
                "allow_paid_api": bool(payload.get("allow_paid_api")),
                "automation_enabled": True,
                "automation_target": automation_target,
                "auto_publish": automation_target == "publish",
                "automation_chinese_policy": str(
                    payload.get("automation_chinese_policy") or "youtube_preferred"
                ),
                "publish_metadata_provider": str(
                    payload.get("publish_metadata_provider") or "auto"
                ),
                "account_id": str(payload.get("account_id") or ""),
                "publish_only_self": bool(payload.get("publish_only_self", False)),
                "automation_failure_policy": str(
                    payload.get("automation_failure_policy") or "skip"
                ),
                "automation_silent_video_policy": str(
                    payload.get("automation_silent_video_policy")
                    or "publish_original"
                ),
                "automation_dubbing_review_policy": str(
                    payload.get("automation_dubbing_review_policy")
                    or "auto_fallback"
                ),
                "dubbing_enabled": bool(payload.get("dubbing_enabled")),
                "dubbing_reference_mode": str(
                    payload.get("dubbing_reference_mode") or "auto"
                ),
                "dubbing_reference_start": payload.get("dubbing_reference_start"),
                "dubbing_reference_end": payload.get("dubbing_reference_end"),
                "dubbing_subtitle_display": str(
                    payload.get("dubbing_subtitle_display") or "chinese"
                ),
                "force_dubbing": bool(payload.get("force_dubbing")),
            },
            resource_class="gpu_heavy",
        )
        return {
            "subtitles": "下载完成，已自动接续到双语字幕",
            "render": "下载完成，已自动接续字幕与成片",
            "publish": "下载完成，已自动接续字幕、成片与投稿",
        }[automation_target]

    def _queue_automatic_publish(self, job: dict[str, Any]) -> str:
        target = str(job["target"])
        task_dir = self.scanner.resolve_task(target)
        publish_original_video = bool(
            job["payload"].get("publish_original_video")
        )
        dubbing_review = (
            self._dubbing_review_details(task_dir)
            if job["payload"].get("dubbing_enabled")
            and str(
                job["payload"].get("automation_dubbing_review_policy")
                or "auto_fallback"
            ).strip().casefold()
            != "continue"
            else None
        )
        if dubbing_review is not None and not publish_original_video:
            if str(
                job["payload"].get("automation_failure_policy") or "skip"
            ).strip().casefold() != "skip":
                raise RuntimeError(
                    f"{dubbing_review['message']}；已阻止自动投稿并保留失败状态"
                )
            self.publisher.mark_automation_skipped(
                task_dir,
                "DUBBING_TIMING_REVIEW_REQUIRED",
                details=dubbing_review,
            )
            return "中文配音需要复核，已阻止自动投稿并继续队列"
        manifest = read_json(task_dir / "stage4" / "stage4_manifest.json")
        media = self.publisher.media_for_payload(task_dir, job["payload"])
        review = manifest.get("review") if isinstance(manifest.get("review"), dict) else {}
        render_blocked = bool(review.get("render_blocked_before_ffmpeg"))
        status = str(manifest.get("status") or "")
        media_ready = media.is_file() and media.stat().st_size > 0
        if not publish_original_video and (
            render_blocked or not media_ready or status not in {
            "STAGE4_COMPLETED",
            "REVIEW_REQUIRED",
            }
        ):
            reason = (
                "SUBTITLE_LAYOUT_REVIEW_REQUIRED"
                if render_blocked or status == "REVIEW_REQUIRED"
                else "HARDSUB_OUTPUT_NOT_READY"
            )
            if (
                job["payload"].get("dubbing_fallback")
                and self._automation_target(job["payload"]) == "publish"
            ):
                original_payload = self.publisher.automatic_submission(
                    task_dir,
                    account_id=str(job["payload"].get("account_id") or ""),
                    is_only_self=bool(
                        job["payload"].get("publish_only_self", False)
                    ),
                    publish_original_video=True,
                    media_variant="original",
                )
                self.publisher.mark_automation_original_media(
                    task_dir,
                    reason="DUBBING_FALLBACK_RENDER_REVIEW_REQUIRED",
                    message=(
                        "中文配音无法安全适配，原声字幕版也未达到可投稿条件；"
                        "已自动改用原始视频"
                    ),
                )
                if not self.store.has_active("publish", target):
                    self.store.enqueue(
                        "publish",
                        target,
                        original_payload,
                        resource_class=self.initial_resource(
                            "publish", original_payload
                        ),
                    )
                return "原声字幕版未通过成片检查，已改用原视频加入投稿队列"
            if str(
                job["payload"].get("automation_failure_policy") or "skip"
            ) != "skip":
                raise RuntimeError(
                    f"成片未达到可投稿条件（{reason}）；"
                    "已按设置保留失败状态，未自动投稿"
                )
            self.publisher.mark_automation_skipped(
                task_dir,
                reason,
                details={
                    "stage4_status": status,
                    "media_ready": media_ready,
                    "review": review,
                },
            )
            return "成片未达到可投稿条件，已自动跳过并继续队列"
        if self.store.has_active("publish", target):
            return "成片完成，投稿任务已在队列中"
        publish_payload = self.publisher.automatic_submission(
            task_dir,
            account_id=str(job["payload"].get("account_id") or ""),
            is_only_self=bool(job["payload"].get("publish_only_self", False)),
            publish_original_video=publish_original_video,
            media_variant=str(
                job["payload"].get("media_variant")
                or (
                    "dubbed"
                    if job["payload"].get("dubbing_enabled")
                    else "localized"
                )
            ),
        )
        self.store.enqueue(
            "publish",
            target,
            publish_payload,
            resource_class=self.initial_resource("publish", publish_payload),
        )
        return (
            "无配音视频投稿信息完成，已使用原视频加入投稿队列"
            if publish_original_video
            else "中配未通过结构检查，原声字幕版已自动加入投稿队列"
            if job["payload"].get("media_variant") == "subtitled_original_audio"
            else "成片完成，已自动加入投稿队列"
        )

    def _execute_discovery(self, job: dict[str, Any], log_path: Path) -> None:
        if self.discovery_runner is None:
            raise RuntimeError("智能发现执行器尚未配置")
        job_id = str(job["id"])
        self._append_log(log_path, "\n===== 智能发现 [gpu_heavy] =====\n")

        def update_progress(step: str, progress: int) -> None:
            self._raise_if_cancelled(job_id)
            self.store.update(
                job_id,
                step=str(step)[:300],
                progress=max(0, min(int(progress), 99)),
            )

        result = self.discovery_runner(
            dict(job["payload"]),
            progress=update_progress,
            cancelled=lambda: self._raise_if_cancelled(job_id),
        )
        self._raise_if_cancelled(job_id)
        result_dir = self.project_root / "work" / "control_panel" / "discovery_results"
        result_dir.mkdir(parents=True, exist_ok=True)
        result_path = result_dir / f"{job_id}.json"
        temporary = result_path.with_suffix(".json.tmp")
        try:
            temporary.write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, result_path)
        finally:
            temporary.unlink(missing_ok=True)
        payload = dict(job["payload"])
        payload["result_file"] = result_path.name
        self.store.replace_payload(job_id, payload)
        self._append_log(
            log_path,
            f"[智能发现] 返回 {len(result.get('results', []))} 个候选。\n",
        )
        self.store.update(
            job_id,
            status="completed",
            step="智能发现完成",
            progress=100,
            exit_code=0,
            error="",
            finished_at=utc_now(),
        )

    def _build_stages(
        self,
        job: dict[str, Any],
    ) -> list[tuple[str, list[str], str]]:
        stages: list[tuple[str, list[str], str]] = []
        for label, command in self._build_commands(job):
            if job["kind"] == "download":
                resource_class = "network"
            elif job["kind"] == "publish":
                resource_class = (
                    "upload"
                    if label == "上传并提交到哔哩哔哩"
                    else "gpu_heavy"
                )
            elif "--steps" in command:
                command_step = command[command.index("--steps") + 1]
                metadata_provider = (
                    command[command.index("--publish-metadata-provider") + 1]
                    if "--publish-metadata-provider" in command
                    else ""
                )
                resource_class = (
                    "paid_api"
                    if command_step == "translate"
                    or (
                        command_step == "metadata"
                        and metadata_provider == "translation_api"
                    )
                    else "gpu_heavy"
                )
            else:
                resource_class = "gpu_heavy"
            stages.append((label, command, resource_class))
        return stages

    def _build_commands(self, job: dict[str, Any]) -> list[tuple[str, list[str]]]:
        payload = job["payload"]
        if job["kind"] == "download":
            python = resolve_python_executable(self.project_root)
            command = [
                str(python),
                "-m",
                "src.download_video",
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
                stage3_python = resolve_python_executable(self.project_root)
                commands.append(
                    (
                        "生成投稿用硬字幕 MP4",
                        [
                            str(stage3_python),
                            "-m",
                            "src.run_stage4",
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
        stage3_python = resolve_python_executable(self.project_root)
        if payload.get("silent_video_mode"):
            metadata_provider = str(
                payload.get("publish_metadata_provider") or "auto"
            )
            metadata_command = [
                str(stage3_python),
                "-m",
                "src.run_stage3",
                "--video-dir",
                str(task_dir),
                "--steps",
                "metadata",
                "--publish-metadata-provider",
                metadata_provider,
                "--allow-no-subtitles",
                "--resume",
            ]
            if payload.get("allow_paid_api"):
                metadata_command.append("--allow-paid-api")
            return [("生成无配音视频投稿信息", metadata_command)]
        steps = str(payload.get("workflow") or "complete")
        requested_chinese_source = str(
            payload.get("chinese_subtitle_source") or "deepseek"
        )
        if requested_chinese_source not in {"auto", "deepseek", "youtube_auto"}:
            raise ValueError("不支持的中文字幕来源")
        has_youtube_chinese = youtube_chinese_path(task_dir) is not None
        if requested_chinese_source == "auto":
            youtube_chinese_unusable = (
                has_youtube_chinese
                and payload.get("auto_translate_missing", True)
                and self._youtube_chinese_requires_api_fallback(task_dir)
            )
            if has_youtube_chinese and not youtube_chinese_unusable:
                chinese_source = "auto"
            elif payload.get("auto_translate_missing", True):
                chinese_source = "deepseek"
            elif (
                self._automation_target(payload) == "publish"
                and str(
                    payload.get("automation_silent_video_policy")
                    or "publish_original"
                ).strip().casefold()
                == "publish_original"
            ):
                # The English selection stage must run before we can distinguish
                # a no-speech original from a voiced video missing Chinese text.
                chinese_source = "auto"
            else:
                raise ValueError("没有 YouTube 中文字幕，且自动 API 翻译已关闭")
        else:
            chinese_source = requested_chinese_source
        commands: list[tuple[str, list[str]]] = []
        if steps in {"subtitles", "complete"}:
            english_policy = str(
                payload.get("english_subtitle_policy") or ""
            ).strip().casefold()
            if english_policy not in {"quality", "youtube_first", "whisper"}:
                english_policy = (
                    "quality"
                    if payload.get("whisper_for_auto_subtitles", True)
                    else "youtube_first"
                )
            selection_command = [
                str(stage3_python),
                "-m",
                "src.run_stage3",
                "--video-dir",
                str(task_dir),
                "--steps",
                "select",
                "--subtitle-source",
                "whisper" if english_policy == "whisper" else "auto",
                "--resume",
            ]
            if english_policy == "youtube_first":
                selection_command.append("--no-whisper-for-auto-subtitles")
            commands.append(
                (
                    "生成并选择最佳英文字幕",
                    selection_command,
                )
            )
            if chinese_source == "deepseek":
                translation_command = [
                    str(stage3_python),
                    "-m",
                    "src.run_stage3",
                    "--video-dir",
                    str(task_dir),
                    "--steps",
                    "translate",
                    "--resume",
                    "--allow-paid-api",
                ]
                if payload.get("dubbing_enabled"):
                    translation_command.append("--for-dubbing")
                commands.append(("翻译并检查中文字幕", translation_command))
            metadata_provider = str(
                payload.get("publish_metadata_provider") or "translation_api"
            )
            needs_metadata_step = self._automation_target(payload) == "publish" and (
                chinese_source != "deepseek"
                or metadata_provider != "translation_api"
            )
            if needs_metadata_step:
                metadata_command = [
                    str(stage3_python),
                    "-m",
                    "src.run_stage3",
                    "--video-dir",
                    str(task_dir),
                    "--steps",
                    "metadata",
                    "--publish-metadata-provider",
                    metadata_provider,
                    "--resume",
                ]
                if payload.get("allow_paid_api"):
                    metadata_command.append("--allow-paid-api")
                commands.append(
                    ("自动生成投稿标题、标签与分区", metadata_command)
                )
        dubbing_enabled = bool(payload.get("dubbing_enabled") or steps == "dubbing")
        if dubbing_enabled and steps in {"render", "complete", "dubbing"}:
            dubbing_config = load_dubbing_config(self.project_root)
            dubbing_python = resolve_dubbing_python(
                self.project_root,
                dubbing_config,
            )
            dubbing_command = [
                str(dubbing_python),
                "-m",
                "src.run_dubbing",
                "--video-dir",
                str(task_dir),
                "--reference-mode",
                str(payload.get("dubbing_reference_mode") or "auto"),
            ]
            if str(payload.get("dubbing_reference_mode") or "auto") == "manual":
                dubbing_command.extend(
                    [
                        "--reference-start",
                        str(payload.get("dubbing_reference_start")),
                        "--reference-end",
                        str(payload.get("dubbing_reference_end")),
                    ]
                )
            if payload.get("force_dubbing"):
                dubbing_command.append("--force-tts")
            commands.append(("生成中文 AI 配音", dubbing_command))
        if steps in {"render", "complete", "dubbing"}:
            mode = str(payload.get("render_mode") or "hardsub")
            if mode not in {"ass", "softsub", "hardsub", "both"}:
                raise ValueError("不支持的成片模式")
            render_chinese_source = "auto" if steps == "dubbing" else chinese_source
            render_command = [
                str(stage3_python),
                "-m",
                "src.run_stage4",
                "--video-dir",
                str(task_dir),
                "--mode",
                mode,
                "--chinese-source",
                render_chinese_source,
                "--resume",
            ]
            if dubbing_enabled:
                render_command.extend(
                    [
                        "--audio-source",
                        str(task_dir / "dubbing" / "dubbed_audio.wav"),
                        "--subtitle-display",
                        str(payload.get("dubbing_subtitle_display") or "chinese"),
                    ]
                )
            commands.append(
                (
                    "生成并质检中文配音成片"
                    if dubbing_enabled
                    else "生成并质检双语成片",
                    render_command,
                )
            )
        return commands

    def _create_cookie_copy(self, job_id: str) -> Path | None:
        config_path = self.project_root / "config" / "download_config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None
        if not config.get("use_cookies", True):
            return None
        configured = Path(str(config.get("cookies_path") or "private/cookies.txt"))
        source = configured if configured.is_absolute() else self.project_root / configured
        if not source.is_file() or source.stat().st_size <= 0:
            return None
        self._cookie_work_dir.mkdir(parents=True, exist_ok=True)
        destination = self._cookie_work_dir / f"{job_id}.txt"
        shutil.copy2(source, destination)
        return destination

    def _cleanup_stale_cookie_copies(self) -> None:
        if not self._cookie_work_dir.is_dir():
            return
        for candidate in self._cookie_work_dir.glob("*.txt"):
            if candidate.is_file():
                candidate.unlink(missing_ok=True)

    def _run_command(
        self,
        job_id: str,
        command: list[str],
        log_path: Path,
        *,
        stage_progress_start: float = 0.0,
        stage_progress_span: float = 100.0,
    ) -> int:
        if len(command) >= 3 and command[1:3] == ["-m", "src.run_dubbing"]:
            try:
                config = load_dubbing_config(self.project_root)
            except (OSError, ValueError):
                config = {}
            performance = (
                config.get("performance")
                if isinstance(config.get("performance"), dict)
                else {}
            )
            keep_warm = bool(performance.get("keep_voxcpm_warm", False))
            if not keep_warm and getattr(self, "_dubbing_worker_client", None) is not None:
                self._shutdown_dubbing_worker(
                    "Persistent VoxCPM2 reuse was disabled; releasing the model."
                )
            if keep_warm:
                try:
                    return self._run_persistent_dubbing_command(
                        job_id,
                        command,
                        log_path,
                        config=config,
                        stage_progress_start=stage_progress_start,
                        stage_progress_span=stage_progress_span,
                    )
                except DubbingWorkerStartError as exc:
                    self._append_log(
                        log_path,
                        "\n[DUBBING] Persistent worker startup failed; "
                        f"falling back to one-task lifecycle: {exc}\n",
                    )
        return self._run_subprocess_command(
            job_id,
            command,
            log_path,
            stage_progress_start=stage_progress_start,
            stage_progress_span=stage_progress_span,
        )

    @staticmethod
    def _dubbing_request_from_command(command: list[str]) -> dict[str, Any]:
        def option(name: str, default: Any = None) -> Any:
            if name not in command:
                return default
            index = command.index(name)
            return command[index + 1] if index + 1 < len(command) else default

        return {
            "video_dir": str(option("--video-dir") or ""),
            "config": option("--config"),
            "reference_mode": str(option("--reference-mode", "auto")),
            "reference_start": (
                float(option("--reference-start"))
                if option("--reference-start") is not None
                else None
            ),
            "reference_end": (
                float(option("--reference-end"))
                if option("--reference-end") is not None
                else None
            ),
            "force_separation": "--force-separation" in command,
            "force_tts": "--force-tts" in command,
        }

    def _consume_job_output_line(
        self,
        job_id: str,
        line: str,
        log_path: Path,
        *,
        stage_progress_start: float,
        stage_progress_span: float,
    ) -> None:
        redacted = self.publisher.redact_log_text(line)
        self._append_log(log_path, redacted)
        if not redacted.startswith("[DUBBING_PROGRESS] "):
            return
        try:
            progress = json.loads(redacted.split(" ", 1)[1])
            step = str(progress.get("step") or "中文配音处理中")
            stage_value = max(0, min(float(progress.get("progress") or 0), 100))
            overall_value = round(
                stage_progress_start + stage_progress_span * stage_value / 100
            )
            self.store.update(
                job_id,
                step=step[:300],
                progress=max(0, min(overall_value, 99)),
            )
        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
            pass

    def _run_persistent_dubbing_command(
        self,
        job_id: str,
        command: list[str],
        log_path: Path,
        *,
        config: dict[str, Any],
        stage_progress_start: float,
        stage_progress_span: float,
    ) -> int:
        executable = Path(command[0]).resolve()
        if not executable.is_file():
            raise FileNotFoundError(f"缺少运行环境：{executable}")
        environment = build_dubbing_subprocess_env(self.project_root)
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        performance = (
            config.get("performance")
            if isinstance(config.get("performance"), dict)
            else {}
        )
        client = self._dubbing_worker_client
        if client is not None and (
            not client.alive or client.python_executable != executable
        ):
            self._shutdown_dubbing_worker(
                "Dubbing runtime changed; restarting persistent worker."
            )
            client = None
        if client is None:
            client = PersistentDubbingWorkerClient(
                executable,
                self.project_root,
                idle_timeout_seconds=float(
                    performance.get("worker_idle_timeout_seconds", 45.0)
                ),
                env=environment,
            )
            self._dubbing_worker_client = client
            self._append_log(
                log_path,
                "[DUBBING] Persistent .venv_dubbing worker started.\n",
            )
        with self._process_lock:
            self._processes[job_id] = client.process  # type: ignore[assignment]
            terminate_immediately = (
                job_id in self._cancel_requested or self._stop_event.is_set()
            )
        if terminate_immediately:
            client.terminate()
        try:
            result = client.run(
                self._dubbing_request_from_command(command),
                on_line=lambda line: self._consume_job_output_line(
                    job_id,
                    line,
                    log_path,
                    stage_progress_start=stage_progress_start,
                    stage_progress_span=stage_progress_span,
                ),
                cancelled=lambda: self._raise_if_cancelled(job_id),
                stopping=self._stop_event.is_set,
            )
            exit_code = int(result.get("exit_code") or 0)
            if result.get("worker_exiting") or not client.alive:
                client.terminate()
                self._dubbing_worker_client = None
            return exit_code
        except DubbingWorkerCrashed as exc:
            self._append_log(
                log_path,
                f"\n[DUBBING] Persistent worker crashed: {exc}. "
                "The next retry will start a fresh worker and reuse segment checkpoints.\n",
            )
            client.terminate()
            self._dubbing_worker_client = None
            return 2
        finally:
            with self._process_lock:
                if self._processes.get(job_id) is client.process:
                    self._processes.pop(job_id, None)

    def _run_subprocess_command(
        self,
        job_id: str,
        command: list[str],
        log_path: Path,
        *,
        stage_progress_start: float = 0.0,
        stage_progress_span: float = 100.0,
    ) -> int:
        executable = Path(command[0])
        if not executable.is_file():
            raise FileNotFoundError(f"缺少运行环境：{executable}")
        environment = build_dubbing_subprocess_env(self.project_root)
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        if executable.name.casefold() == "biliup.exe":
            self.publisher.configure_upload_environment(environment)
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
            self._processes[job_id] = process
            terminate_immediately = (
                job_id in self._cancel_requested or self._stop_event.is_set()
            )
        if terminate_immediately and process.poll() is None:
            self._terminate_process_tree(process)
        try:
            assert process.stdout is not None
            with log_path.open("a", encoding="utf-8") as handle:
                for raw_line in process.stdout:
                    line = self.publisher.redact_log_text(
                        self._decode_process_output(raw_line)
                    )
                    handle.write(line)
                    handle.flush()
                    if line.startswith("[DUBBING_PROGRESS] "):
                        try:
                            progress = json.loads(line.split(" ", 1)[1])
                            step = str(progress.get("step") or "中文配音处理中")
                            stage_value = max(
                                0,
                                min(float(progress.get("progress") or 0), 100),
                            )
                            overall_value = round(
                                stage_progress_start
                                + stage_progress_span * stage_value / 100
                            )
                            self.store.update(
                                job_id,
                                step=step[:300],
                                progress=max(0, min(overall_value, 99)),
                            )
                        except (json.JSONDecodeError, AttributeError, TypeError):
                            pass
            return process.wait()
        finally:
            with self._process_lock:
                if self._processes.get(job_id) is process:
                    self._processes.pop(job_id, None)

    def _run_publish_upload_with_retries(
        self,
        job_id: str,
        command: list[str],
        log_path: Path,
    ) -> int:
        delays = self.publisher.transient_retry_delays()
        maximum_attempts = len(delays) + 1
        for attempt_index in range(maximum_attempts):
            attempt_number = attempt_index + 1
            marker = (
                f"\n[投稿尝试 {attempt_number}/{maximum_attempts}]"
                + (" 自动线路" if attempt_index == 0 else " 故障线路降级")
                + "\n"
            )
            self._append_log(log_path, marker)
            attempt_command = self.publisher.retry_upload_command(
                command,
                attempt_index,
            )
            exit_code = self._run_command(job_id, attempt_command, log_path)
            if exit_code == 0:
                return 0
            attempt_log = self.store.log_tail(job_id, max_chars=100000).rsplit(
                marker,
                1,
            )[-1]
            if (
                attempt_index >= len(delays)
                or not self.publisher.is_transient_upload_failure(attempt_log)
            ):
                return exit_code
            delay = delays[attempt_index]
            self.store.update(
                job_id,
                step=f"投稿网络中断，{delay:g} 秒后自动重试",
            )
            self._append_log(
                log_path,
                f"\n[自动恢复] 检测到可恢复的 TLS/网络中断，"
                f"{delay:g} 秒后重试；已保留 biliup 上传检查点。\n",
            )
            deadline = time.monotonic() + delay
            while time.monotonic() < deadline:
                self._raise_if_cancelled(job_id)
                if self._stop_event.wait(
                    timeout=min(0.25, max(0.0, deadline - time.monotonic()))
                ):
                    raise RuntimeError("控制面板正在关闭，投稿自动重试已停止")
        return 1

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
