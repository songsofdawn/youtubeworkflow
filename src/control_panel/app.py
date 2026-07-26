from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .jobs import JobStore, WorkflowWorker
from .publishing import BiliupIntegration
from .tasks import (
    WorkflowScanner,
    deepseek_translation_ready,
    read_json,
    youtube_auto_chinese_path,
)
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
            if job["status"] not in {"queued", "running"}:
                continue
            active_by_target.setdefault(str(job["target"]), job)
        for task in tasks:
            active = active_by_target.get(str(task["task"])) or active_by_target.get(
                str(task["video_id"])
            )
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
        chinese_subtitle_source: str,
        allow_paid_api: bool,
    ) -> list[dict[str, Any]]:
        if workflow not in {"subtitles", "render", "complete"}:
            raise ValueError("不支持的处理流程")
        if render_mode not in {"ass", "softsub", "hardsub", "both"}:
            raise ValueError("不支持的成片模式")
        if chinese_subtitle_source not in {"deepseek", "youtube_auto"}:
            raise ValueError("不支持的中文字幕来源")
        if not tasks:
            raise ValueError("请至少选择一个已下载的视频")
        if len(tasks) > 50:
            raise ValueError("一次最多加入 50 个视频")
        uses_deepseek = (
            chinese_subtitle_source == "deepseek"
            and workflow in {"subtitles", "complete"}
        )
        if uses_deepseek:
            if not allow_paid_api:
                raise ValueError("翻译会调用付费 API，请先在面板中确认")
            if not self.health()["checks"]["deepseek_api"]:
                raise ValueError("DEEPSEEK_API_KEY 尚未配置")

        validated: list[str] = []
        task_dirs: dict[str, Path] = {}
        for task in tasks:
            task_dirs[task] = self.scanner.resolve_task(task)
            if task not in validated:
                validated.append(task)
        if chinese_subtitle_source == "youtube_auto":
            missing = [
                task for task in validated
                if youtube_auto_chinese_path(task_dirs[task]) is None
            ]
            if missing:
                labels = "、".join(Path(task).name for task in missing[:5])
                suffix = f"等 {len(missing)} 个视频" if len(missing) > 5 else ""
                raise ValueError(
                    f"以下视频没有自动生成的中文字幕：{labels}{suffix}。"
                    "请改选 DeepSeek 翻译。"
                )
        if workflow == "render" and chinese_subtitle_source == "deepseek":
            missing = [
                task for task in validated
                if not deepseek_translation_ready(task_dirs[task])
            ]
            if missing:
                labels = "、".join(Path(task).name for task in missing[:5])
                raise ValueError(
                    f"以下视频尚未完成 DeepSeek 翻译：{labels}。"
                    "请先处理到双语字幕。"
                )
        jobs = [
            self.store.enqueue(
                "pipeline",
                task,
                {
                    "workflow": workflow,
                    "render_mode": render_mode,
                    "chinese_subtitle_source": chinese_subtitle_source,
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

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self.worker.cancel(job_id)

    def delete_job_log(self, job_id: str) -> dict[str, Any]:
        return self.store.delete_log(job_id)

    def clear_old_logs(self) -> dict[str, int]:
        return self.store.clear_inactive_logs()

    def delete_task(self, task: str, confirmation: str) -> dict[str, Any]:
        if not task or confirmation != task:
            raise ValueError("删除确认不匹配，请重新确认视频任务")
        task_dir = self.scanner.resolve_task(task)
        downloads_root = self.scanner.downloads_root.resolve()
        resolved = task_dir.resolve()
        try:
            resolved.relative_to(downloads_root)
        except ValueError as exc:
            raise ValueError("任务目录超出 downloads 范围") from exc
        if resolved == downloads_root:
            raise ValueError("不能删除 downloads 根目录")

        manifest = read_json(resolved / "download_manifest.json")
        info = read_json(resolved / "metadata" / "info.json")
        video_id = str(manifest.get("video_id") or info.get("id") or "")
        targets = {task}
        if video_id:
            targets.add(video_id)
        if self.store.active_for_targets(targets):
            raise ValueError("视频仍有运行中或排队中的任务，请先终止后再删除")

        file_count = 0
        total_bytes = 0
        for path in resolved.rglob("*"):
            if path.is_file() and not path.is_symlink():
                file_count += 1
                total_bytes += path.stat().st_size
        shutil.rmtree(resolved)
        parent = resolved.parent
        while parent != downloads_root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
        history = self.store.delete_jobs_for_targets(targets)
        return {
            "deleted": True,
            "task": task,
            "video_id": video_id,
            "files": file_count,
            "bytes": total_bytes,
            "history": history,
        }

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
