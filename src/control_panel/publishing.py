from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .tasks import read_json
from .youtube import load_env_values


ALLOWED_SUBMIT_APIS = {"app", "web", "b-cut-android"}
ALLOWED_UPLOAD_LINES = {
    "",
    "bldsa",
    "cnbldsa",
    "andsa",
    "atdsa",
    "bda2",
    "cnbd",
    "anbd",
    "atbd",
    "tx",
    "cntx",
    "antx",
    "attx",
    "bda",
    "txa",
    "alia",
}
BVID_PATTERN = re.compile(r"\b(BV[0-9A-Za-z]{10})\b")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


class BiliupIntegration:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.config_path = self.project_root / "config" / "publish_config.json"
        self.config = self._load_config()
        self._version: str | None = None

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            raise FileNotFoundError(f"投稿配置不存在：{self.config_path}")
        payload = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("投稿配置必须是 JSON 对象")
        return payload

    def _path_from_setting(self, value: str) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.project_root / path).resolve()

    def executable(self) -> Path | None:
        env = load_env_values(self.project_root / ".env")
        override = os.getenv("BILIUP_EXE", "").strip() or env.get("BILIUP_EXE", "").strip()
        candidates = [override] if override else []
        candidates.extend(str(item) for item in self.config.get("biliup_executable_candidates", []))
        for item in candidates:
            if not item:
                continue
            path = self._path_from_setting(item)
            if path.is_file():
                return path
        return None

    def version(self) -> str:
        if self._version is not None:
            return self._version
        executable = self.executable()
        if executable is None:
            self._version = ""
            return self._version
        try:
            completed = subprocess.run(
                [str(executable), "--version"],
                cwd=executable.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._version = (completed.stdout or completed.stderr).strip() if completed.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            self._version = ""
        return self._version

    def accounts(self) -> list[dict[str, str]]:
        accounts: list[dict[str, str]] = []
        seen: set[Path] = set()
        for path in self._account_candidates():
            resolved = path.resolve()
            if resolved in seen or not self._is_account_file(resolved):
                continue
            seen.add(resolved)
            account_id = hashlib.sha256(str(resolved).casefold().encode("utf-8")).hexdigest()[:16]
            accounts.append(
                {
                    "id": account_id,
                    "label": resolved.stem,
                    "source": resolved.parent.name,
                }
            )
        return accounts

    def _account_candidates(self) -> list[Path]:
        env = load_env_values(self.project_root / ".env")
        override = (
            os.getenv("BILIUP_COOKIE_FILE", "").strip()
            or env.get("BILIUP_COOKIE_FILE", "").strip()
        )
        paths: list[Path] = []
        if override:
            paths.append(self._path_from_setting(override))
        for item in self.config.get("account_directories", []):
            directory = self._path_from_setting(str(item))
            if directory.is_dir():
                paths.extend(sorted(directory.glob("*.json")))
        return paths

    @staticmethod
    def _is_account_file(path: Path) -> bool:
        if not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and (
            "cookie_info" in payload or "token_info" in payload
        )

    def resolve_account(self, account_id: str) -> tuple[Path, dict[str, str]]:
        for path in self._account_candidates():
            resolved = path.resolve()
            candidate_id = hashlib.sha256(
                str(resolved).casefold().encode("utf-8")
            ).hexdigest()[:16]
            if candidate_id == account_id and self._is_account_file(resolved):
                return resolved, {
                    "id": candidate_id,
                    "label": resolved.stem,
                    "source": resolved.parent.name,
                }
        raise ValueError("所选 biliup 账号文件不存在或格式无效")

    def health(self) -> dict[str, Any]:
        executable = self.executable()
        accounts = self.accounts()
        return {
            "available": executable is not None,
            "version": self.version() if executable else "",
            "account_count": len(accounts),
            "account_ready": bool(accounts),
            "accounts": accounts,
        }

    @staticmethod
    def expected_hardsub(task_dir: Path) -> Path:
        return task_dir / "stage4" / "video" / "final_bilingual_hardsub.mp4"

    def defaults(self, task_dir: Path) -> dict[str, Any]:
        download = read_json(task_dir / "download_manifest.json")
        info = read_json(task_dir / "metadata" / "info.json")
        title = str(download.get("title") or info.get("title") or task_dir.name).strip()
        channel = str(
            download.get("channel") or info.get("channel") or info.get("uploader") or ""
        ).strip()
        video_id = str(download.get("video_id") or info.get("id") or "").strip()
        source_url = str(download.get("url") or "").strip()
        if not source_url and video_id:
            source_url = f"https://www.youtube.com/watch?v={video_id}"
        upload_title = f"【中英双语】{title}"[:80]
        description_lines = [
            "本视频为已获授权或许可素材的中文本地化版本。",
            "",
            f"原视频：{title}",
        ]
        if channel:
            description_lines.append(f"原作者：{channel}")
        if source_url:
            description_lines.append(f"来源：{source_url}")
        description_lines.extend(
            ["", "字幕由本地工作流生成，请以原视频内容为准。"]
        )
        cover = task_dir / "metadata" / "thumbnail.jpg"
        accounts = self.accounts()
        media = self.expected_hardsub(task_dir)
        translated = any(
            path.is_file() and path.stat().st_size > 0
            for path in (
                task_dir / "subtitles" / "zh.reviewed.srt",
                task_dir / "subtitles" / "zh.clean.srt",
            )
        )
        return {
            "title": upload_title,
            "description": "\n".join(description_lines),
            "dynamic": upload_title,
            "tags": "中英双语,中文翻译",
            "copyright": int(self.config.get("default_copyright", 2)),
            "source": source_url,
            "tid": int(self.config.get("default_tid", 171)),
            "submit": str(self.config.get("default_submit", "web")),
            "line": str(self.config.get("default_line", "")),
            "limit": int(self.config.get("upload_limit", 3)),
            "no_reprint": bool(self.config.get("default_no_reprint", True)),
            "is_only_self": bool(self.config.get("default_only_self", True)),
            "use_cover": cover.is_file() and cover.stat().st_size > 0,
            "cover_available": cover.is_file() and cover.stat().st_size > 0,
            "media_ready": media.is_file() and media.stat().st_size > 0,
            "translation_ready": translated,
            "media_name": media.name,
            "accounts": accounts,
            "account_id": accounts[0]["id"] if accounts else "",
        }

    def validate_submission(
        self,
        task_dir: Path,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        if self.executable() is None:
            raise ValueError("未找到 biliup.exe")
        existing = read_json(task_dir / "stage5" / "publish_manifest.json")
        if existing.get("status") == "PUBLISHED":
            raise ValueError("这个任务已经记录为投稿成功，为避免重复投稿已停止")

        title = " ".join(str(values.get("title") or "").split())
        description = str(values.get("description") or "").strip()
        dynamic = str(values.get("dynamic") or "").strip()
        tags = ",".join(
            item.strip()
            for item in re.split(r"[,，]", str(values.get("tags") or ""))
            if item.strip()
        )
        source = str(values.get("source") or "").strip()
        if not title:
            raise ValueError("投稿标题不能为空")
        if len(title) > 80:
            raise ValueError("投稿标题不能超过 80 个字符")
        if len(description) > 2000:
            raise ValueError("投稿简介不能超过 2000 个字符")
        if len(dynamic) > 255:
            raise ValueError("空间动态不能超过 255 个字符")
        if not tags:
            raise ValueError("请至少填写一个投稿标签")

        try:
            tid = int(values.get("tid"))
            copyright_value = int(values.get("copyright", 2))
            limit = int(values.get("limit", 3))
        except (TypeError, ValueError) as exc:
            raise ValueError("分区、版权类型或并发数格式无效") from exc
        if tid < 1:
            raise ValueError("投稿分区 TID 必须是正整数")
        if copyright_value not in {1, 2}:
            raise ValueError("版权类型只能是自制或转载")
        if copyright_value == 2 and not source:
            raise ValueError("转载投稿必须填写原视频来源")
        if not 1 <= limit <= 10:
            raise ValueError("上传并发数必须在 1 到 10 之间")

        submit = str(values.get("submit") or "web")
        line = str(values.get("line") or "")
        if submit not in ALLOWED_SUBMIT_APIS:
            raise ValueError("不支持的投稿接口")
        if line not in ALLOWED_UPLOAD_LINES:
            raise ValueError("不支持的上传线路")
        _, account = self.resolve_account(str(values.get("account_id") or ""))
        cover = task_dir / "metadata" / "thumbnail.jpg"
        use_cover = values.get("use_cover") is True
        if use_cover and (not cover.is_file() or cover.stat().st_size == 0):
            raise ValueError("任务中没有可用的封面文件")
        translated = any(
            path.is_file() and path.stat().st_size > 0
            for path in (
                task_dir / "subtitles" / "zh.reviewed.srt",
                task_dir / "subtitles" / "zh.clean.srt",
            )
        )
        media = self.expected_hardsub(task_dir)
        if not translated and not (media.is_file() and media.stat().st_size > 0):
            raise ValueError("中文字幕尚未完成，请先运行双语字幕处理")
        if values.get("confirm_publish") is not True:
            raise ValueError("投稿前必须确认版权、分区、标题和来源均已核对")

        return {
            "title": title,
            "description": description,
            "dynamic": dynamic,
            "tags": tags,
            "copyright": copyright_value,
            "source": source,
            "tid": tid,
            "submit": submit,
            "line": line,
            "limit": limit,
            "no_reprint": values.get("no_reprint") is True,
            "is_only_self": values.get("is_only_self") is True,
            "use_cover": use_cover,
            "account_id": account["id"],
            "account_label": account["label"],
            "prepare_hardsub": not (media.is_file() and media.stat().st_size > 0),
        }

    def build_upload_command(
        self,
        task_dir: Path,
        payload: dict[str, Any],
    ) -> list[str]:
        executable = self.executable()
        if executable is None:
            raise FileNotFoundError("未找到 biliup.exe")
        account_path, _ = self.resolve_account(str(payload.get("account_id") or ""))
        media = self.expected_hardsub(task_dir)
        cover = task_dir / "metadata" / "thumbnail.jpg"
        command = [
            str(executable),
            "--user-cookie",
            str(account_path),
            "upload",
            "--submit",
            str(payload["submit"]),
            "--limit",
            str(payload["limit"]),
            "--copyright",
            str(payload["copyright"]),
            "--tid",
            str(payload["tid"]),
            "--title",
            str(payload["title"]),
            "--desc",
            str(payload["description"]),
            "--dynamic",
            str(payload["dynamic"]),
            "--tag",
            str(payload["tags"]),
            "--no-reprint",
            "1" if payload.get("no_reprint") else "0",
        ]
        if payload.get("source"):
            command.extend(["--source", str(payload["source"])])
        if payload.get("line"):
            command.extend(["--line", str(payload["line"])])
        if payload.get("use_cover"):
            command.extend(["--cover", str(cover)])
        if payload.get("is_only_self"):
            command.extend(["--is-only-self", "1"])
        command.append(str(media))
        return command

    def mark_running(self, task_dir: Path, payload: dict[str, Any]) -> None:
        atomic_write_json(
            task_dir / "stage5" / "publish_manifest.json",
            self._manifest_base(task_dir, payload)
            | {
                "status": "RUNNING",
                "started_at": utc_now(),
                "finished_at": "",
                "bvid": "",
                "url": "",
                "errors": [],
            },
        )

    def mark_failed(self, task_dir: Path, payload: dict[str, Any], error: str) -> None:
        previous = read_json(task_dir / "stage5" / "publish_manifest.json")
        atomic_write_json(
            task_dir / "stage5" / "publish_manifest.json",
            self._manifest_base(task_dir, payload)
            | {
                "status": "FAILED",
                "started_at": str(previous.get("started_at") or utc_now()),
                "finished_at": utc_now(),
                "bvid": "",
                "url": "",
                "errors": [error],
            },
        )

    def mark_published(
        self,
        task_dir: Path,
        payload: dict[str, Any],
        log_text: str,
    ) -> None:
        previous = read_json(task_dir / "stage5" / "publish_manifest.json")
        match = BVID_PATTERN.search(log_text)
        bvid = match.group(1) if match else ""
        atomic_write_json(
            task_dir / "stage5" / "publish_manifest.json",
            self._manifest_base(task_dir, payload)
            | {
                "status": "PUBLISHED",
                "started_at": str(previous.get("started_at") or utc_now()),
                "finished_at": utc_now(),
                "bvid": bvid,
                "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
                "errors": [],
            },
        )

    def _manifest_base(self, task_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
        media = self.expected_hardsub(task_dir)
        stage4 = read_json(task_dir / "stage4" / "stage4_manifest.json")
        return {
            "schema_version": 1,
            "platform": "bilibili",
            "uploader": self.version(),
            "account": str(payload.get("account_label") or ""),
            "media_path": str(media),
            "media_size": media.stat().st_size if media.is_file() else 0,
            "media_hash": str(stage4.get("hardsub_output_hash") or ""),
            "metadata": {
                key: payload.get(key)
                for key in (
                    "title",
                    "description",
                    "dynamic",
                    "tags",
                    "copyright",
                    "source",
                    "tid",
                    "submit",
                    "line",
                    "limit",
                    "no_reprint",
                    "is_only_self",
                    "use_cover",
                )
            },
        }


__all__ = ["BiliupIntegration"]
