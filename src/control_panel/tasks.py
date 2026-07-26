from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _thumbnail_url(info: dict[str, Any]) -> str:
    direct = info.get("thumbnail")
    if isinstance(direct, str) and direct.startswith(("http://", "https://")):
        return direct
    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if isinstance(item, dict):
                value = str(item.get("url") or "")
                if value.startswith(("http://", "https://")):
                    return value
    video_id = str(info.get("id") or "")
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""


class WorkflowScanner:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.downloads_root = (self.project_root / "downloads").resolve()

    def resolve_task(self, relative_path: str) -> Path:
        candidate = (self.downloads_root / Path(relative_path)).resolve()
        if not _inside(candidate, self.downloads_root):
            raise ValueError("任务目录超出 downloads 范围")
        if not (candidate / "download_manifest.json").is_file():
            raise ValueError("任务目录中没有 download_manifest.json")
        return candidate

    def scan(self) -> list[dict[str, Any]]:
        if not self.downloads_root.is_dir():
            return []
        manifests = sorted(
            self.downloads_root.rglob("download_manifest.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return [self._task_from_manifest(path) for path in manifests]

    def _task_from_manifest(self, manifest_path: Path) -> dict[str, Any]:
        task_dir = manifest_path.parent.resolve()
        download = read_json(manifest_path)
        stage3 = read_json(task_dir / "stage3_manifest.json")
        stage4 = read_json(task_dir / "stage4" / "stage4_manifest.json")
        stage5 = read_json(task_dir / "stage5" / "publish_manifest.json")
        info = read_json(task_dir / "metadata" / "info.json")

        download_status = str(download.get("overall_status") or "unknown")
        selected_path = task_dir / "subtitles" / "en.selected.srt"
        translated_paths = [
            task_dir / "subtitles" / "zh.reviewed.srt",
            task_dir / "subtitles" / "zh.clean.srt",
        ]
        stage4_status = str(stage4.get("status") or "")
        stage4_qc = str(stage4.get("qc_status") or "")
        publish_status = str(stage5.get("status") or "")

        download_complete = download_status in {"success", "skipped"}
        english_complete = selected_path.is_file() and selected_path.stat().st_size > 0
        translation_complete = any(
            path.is_file() and path.stat().st_size > 0 for path in translated_paths
        )
        render_complete = stage4_status == "STAGE4_COMPLETED"
        render_review = stage4_status == "REVIEW_REQUIRED" or stage4_qc == "REVIEW_REQUIRED"

        stages = {
            "download": self._stage_state(
                download_complete,
                download_status in {"failed", "partial_success"},
                download_status,
            ),
            "english": self._stage_state(
                english_complete,
                self._stage3_failed(stage3, "selection"),
                str(stage3.get("selected_source") or "等待处理"),
            ),
            "translation": self._stage_state(
                translation_complete,
                self._stage3_failed(stage3, "translation"),
                str(stage3.get("translation_status") or "等待处理"),
            ),
            "render": (
                {"state": "review", "detail": stage4_status}
                if render_review
                else self._stage_state(
                    render_complete,
                    stage4_status == "FAILED",
                    stage4_status or "等待处理",
                )
            ),
            "publish": (
                {"state": "active", "detail": publish_status}
                if publish_status == "RUNNING"
                else self._stage_state(
                    publish_status == "PUBLISHED",
                    publish_status == "FAILED",
                    publish_status or "等待投稿",
                )
            ),
        }
        completed_count = sum(
            stages[name]["state"] in {"complete", "review"}
            for name in ("download", "english", "translation", "render", "publish")
        )
        relative = task_dir.relative_to(self.downloads_root).as_posix()
        newest_mtime = max(
            path.stat().st_mtime
            for path in (
                manifest_path,
                task_dir / "stage3_manifest.json",
                task_dir / "stage4" / "stage4_manifest.json",
                task_dir / "stage5" / "publish_manifest.json",
            )
            if path.is_file()
        )
        overall = self._overall_label(stages)
        return {
            "task": relative,
            "video_id": str(download.get("video_id") or info.get("id") or ""),
            "title": str(download.get("title") or info.get("title") or task_dir.name),
            "channel": str(download.get("channel") or info.get("channel") or info.get("uploader") or ""),
            "thumbnail_url": _thumbnail_url(info),
            "duration_seconds": float(info.get("duration") or 0),
            "updated_at": datetime.fromtimestamp(
                newest_mtime, tz=timezone.utc
            ).isoformat(),
            "overall": overall,
            "progress": completed_count * 20,
            "stages": stages,
            "stage3_status": str(stage3.get("translation_status") or ""),
            "stage4_status": stage4_status,
            "publish_status": publish_status,
            "bvid": str(stage5.get("bvid") or ""),
            "bilibili_url": str(stage5.get("url") or ""),
            "output_path": str(
                stage4.get("hardsub_output_path")
                or stage4.get("softsub_output_path")
                or ""
            ),
            "errors": [
                str(item)
                for item in (
                    list(download.get("errors") or [])
                    + list(stage3.get("errors") or [])
                    + list(stage4.get("errors") or [])
                    + list(stage5.get("errors") or [])
                )
                if str(item).strip()
            ][-5:],
        }

    @staticmethod
    def _stage_state(complete: bool, failed: bool, detail: str) -> dict[str, str]:
        state = "complete" if complete else "failed" if failed else "pending"
        return {"state": state, "detail": detail}

    @staticmethod
    def _stage3_failed(manifest: dict[str, Any], area: str) -> bool:
        errors = manifest.get("errors") or []
        if errors:
            if area == "translation":
                status = str(manifest.get("translation_status") or "")
                return status.startswith("FAILED")
            return not bool(manifest.get("selected_output_path"))
        return False

    @staticmethod
    def _overall_label(stages: dict[str, dict[str, str]]) -> str:
        states = [item["state"] for item in stages.values()]
        if stages["publish"]["state"] == "complete":
            return "投稿完成"
        if stages["publish"]["state"] == "active":
            return "正在投稿"
        if stages["publish"]["state"] == "failed":
            return "投稿失败"
        if stages["render"]["state"] == "complete":
            return "成片完成，等待投稿"
        if stages["render"]["state"] == "review":
            return "需要复核"
        if "failed" in states:
            return "处理失败"
        if states[2] == "complete":
            return "双语字幕完成"
        if states[1] == "complete":
            return "英文字幕完成"
        if states[0] == "complete":
            return "等待字幕处理"
        return "等待下载"


__all__ = ["WorkflowScanner", "read_json"]
