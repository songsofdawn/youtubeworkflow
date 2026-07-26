from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .jobs import JobStore, WorkflowWorker
from .publishing import BiliupIntegration
from .tasks import WorkflowScanner
from .youtube import TargetedYouTubeSearch, load_env_values, normalize_video_inputs


class ControlPanelApp:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        runtime_root = self.project_root / "work" / "control_panel"
        self.scanner = WorkflowScanner(self.project_root)
        self.store = JobStore(
            runtime_root / "control_panel.sqlite3",
            self.project_root / "logs" / "control_panel" / "jobs",
        )
        self.searcher = TargetedYouTubeSearch(self.project_root)
        self.publisher = BiliupIntegration(self.project_root)
        self.worker = WorkflowWorker(
            self.project_root,
            self.store,
            self.scanner,
            self.publisher,
        )

    def start(self) -> None:
        self.worker.start()

    def close(self) -> None:
        self.worker.close()

    def health(self) -> dict[str, Any]:
        env_values = load_env_values(self.project_root / ".env")

        def configured(name: str) -> bool:
            return bool(os.getenv(name, "").strip() or env_values.get(name, "").strip())

        download_python = self.project_root / ".venv" / "Scripts" / "python.exe"
        stage3_python = self.project_root / ".venv_stage3" / "Scripts" / "python.exe"
        tools = {
            name: (self.project_root / "tools" / "bin" / f"{name}.exe").is_file()
            for name in ("yt-dlp", "ffmpeg", "ffprobe")
        }
        model = self.project_root / "models" / "faster-whisper-large-v3"
        publishing = self.publisher.health()
        checks = {
            "download_environment": download_python.is_file(),
            "stage3_environment": stage3_python.is_file(),
            "tools": all(tools.values()),
            "whisper_model": model.is_dir(),
            "youtube_api": configured("YOUTUBE_API_KEY"),
            "deepseek_api": configured("DEEPSEEK_API_KEY"),
            "biliup": publishing["available"],
            "biliup_account": publishing["account_ready"],
        }
        return {
            "ready": all(
                checks[name]
                for name in (
                    "download_environment",
                    "stage3_environment",
                    "tools",
                    "whisper_model",
                )
            ),
            "checks": checks,
            "tools": tools,
            "publishing": publishing,
        }

    def dashboard(self) -> dict[str, Any]:
        jobs = self.store.list()
        tasks = self.scanner.scan()
        active_by_target: dict[str, dict[str, Any]] = {}
        for job in jobs:
            if job["kind"] not in {"pipeline", "publish"} or job["status"] not in {"queued", "running"}:
                continue
            active_by_target.setdefault(str(job["target"]), job)
        for task in tasks:
            active = active_by_target.get(str(task["task"]))
            if active:
                task["active_job"] = {
                    "id": active["id"],
                    "status": active["status"],
                    "step": active["step"],
                    "progress": active["progress"],
                }
                task["overall"] = active["step"]

        return {
            "health": self.health(),
            "tasks": tasks,
            "jobs": jobs,
            "summary": {
                "tasks": len(tasks),
                "queued": sum(job["status"] == "queued" for job in jobs),
                "running": sum(job["status"] == "running" for job in jobs),
                "failed": sum(job["status"] == "failed" for job in jobs),
                "rendered": sum(
                    task["stages"]["render"]["state"] == "complete" for task in tasks
                ),
                "published": sum(
                    task["stages"]["publish"]["state"] == "complete" for task in tasks
                ),
            },
        }

    def search(self, query: str, limit: int, order: str) -> list[dict[str, Any]]:
        return self.searcher.search(query, limit, order)

    def queue_downloads(
        self,
        *,
        raw_input: str = "",
        items: list[dict[str, Any]] | None = None,
        confirm_rights: bool,
    ) -> list[dict[str, Any]]:
        if not confirm_rights:
            raise ValueError("下载前必须确认拥有下载和使用这些视频的权利")
        normalized: list[dict[str, str]]
        if items:
            raw_urls = " ".join(str(item.get("youtube_url") or item.get("url") or "") for item in items)
            normalized = normalize_video_inputs(raw_urls)
        else:
            normalized = normalize_video_inputs(raw_input)
        jobs = [
            self.store.enqueue(
                "download",
                item["video_id"],
                {"url": item["url"], "video_id": item["video_id"]},
            )
            for item in normalized
        ]
        self.worker.wake()
        return jobs

    def queue_pipeline(
        self,
        *,
        tasks: list[str],
        workflow: str,
        render_mode: str,
        allow_paid_api: bool,
    ) -> list[dict[str, Any]]:
        if workflow not in {"subtitles", "render", "complete"}:
            raise ValueError("不支持的处理流程")
        if render_mode not in {"ass", "softsub", "hardsub", "both"}:
            raise ValueError("不支持的成片模式")
        if not tasks:
            raise ValueError("请至少选择一个已下载的视频")
        if len(tasks) > 50:
            raise ValueError("一次最多加入 50 个视频")
        if workflow in {"subtitles", "complete"}:
            if not allow_paid_api:
                raise ValueError("翻译会调用付费 API，请先在面板中确认")
            if not self.health()["checks"]["deepseek_api"]:
                raise ValueError("DEEPSEEK_API_KEY 尚未配置")

        validated = []
        for task in tasks:
            self.scanner.resolve_task(task)
            if task not in validated:
                validated.append(task)
        jobs = [
            self.store.enqueue(
                "pipeline",
                task,
                {
                    "workflow": workflow,
                    "render_mode": render_mode,
                    "allow_paid_api": bool(allow_paid_api),
                },
            )
            for task in validated
        ]
        self.worker.wake()
        return jobs

    def retry_job(self, job_id: str) -> dict[str, Any]:
        existing = self.store.get(job_id)
        if existing["kind"] == "publish":
            raise ValueError("投稿任务不能一键重试，请重新打开投稿窗口核对并确认")
        job = self.store.retry(job_id)
        self.worker.wake()
        return job

    def publish_defaults(self, task: str) -> dict[str, Any]:
        task_dir = self.scanner.resolve_task(task)
        return self.publisher.defaults(task_dir) | {"task": task}

    def queue_publish(self, task: str, values: dict[str, Any]) -> dict[str, Any]:
        task_dir = self.scanner.resolve_task(task)
        if self.store.has_active("publish", task):
            raise ValueError("这个视频已经在投稿队列中")
        payload = self.publisher.validate_submission(task_dir, values)
        job = self.store.enqueue("publish", task, payload)
        self.worker.wake()
        return job

    def open_task_folder(self, task: str) -> None:
        path = self.scanner.resolve_task(task)
        if os.name == "nt":
            subprocess.Popen(
                ["explorer.exe", str(path)],
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        raise RuntimeError("当前系统不支持从面板打开文件夹")


__all__ = ["ControlPanelApp"]
