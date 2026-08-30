from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.stage3.publish_metadata import (
    build_publish_description,
    category_for_tid,
    compose_bilingual_title,
    compose_localized_title,
    load_category_mapping,
    normalize_tags,
    truncate_utf8,
    truncate_utf16,
    utf8_bytes,
    utf16_code_units,
)

from .tasks import (
    deepseek_translation_ready,
    no_english_subtitle_or_recognized_speech,
    read_json,
)
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
RESPONSE_ERROR_PATTERN = re.compile(
    r'ResponseData\s*\{\s*code:\s*(-?\d+).*?message:\s*"([^"]*)"',
    re.DOTALL,
)
SENSITIVE_LOG_VALUE_PATTERN = re.compile(
    r"(?i)(\b(?:access_key|access_token|refresh_token|sign|SESSDATA|bili_jct)"
    r"(?:=|%3D|:))"
    r"[^&\s\"')]+"
)
TRANSIENT_UPLOAD_PATTERNS = (
    "tls handshake eof",
    "tls close_notify",
    "peer closed connection",
    "unexpected eof",
    "connection error",
    "connection reset",
    "error sending request",
    "network is unreachable",
    "operation timed out",
    "request timed out",
    "http2 error",
    "http 502",
    "http 503",
    "http 504",
)
BILIBILI_DESCRIPTION_MAX_UNITS = 2000
BILIBILI_DESCRIPTION_MAX_UTF8_BYTES = 1900


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
        category_setting = str(
            self.config.get("category_mapping")
            or "config/bilibili_categories.json"
        )
        self.category_mapping = load_category_mapping(
            self._path_from_setting(category_setting)
        )
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
        minimum_interval_seconds = self.publish_min_interval_seconds()
        return {
            "available": executable is not None,
            "version": self.version() if executable else "",
            "account_count": len(accounts),
            "account_ready": bool(accounts),
            "accounts": accounts,
            "publish_min_interval_seconds": minimum_interval_seconds,
            "publish_min_interval_minutes": minimum_interval_seconds // 60,
        }

    def update_publish_settings(self, values: dict[str, Any]) -> list[str]:
        if "publish_min_interval_minutes" not in values:
            return []
        raw_minutes = values["publish_min_interval_minutes"]
        if isinstance(raw_minutes, bool):
            raise ValueError("投稿最短间隔必须是整数分钟")
        try:
            minutes = int(raw_minutes)
        except (TypeError, ValueError) as exc:
            raise ValueError("投稿最短间隔必须是整数分钟") from exc
        if str(raw_minutes).strip() != str(minutes):
            raise ValueError("投稿最短间隔必须是整数分钟")
        if not 1 <= minutes <= 1440:
            raise ValueError("投稿最短间隔必须在 1 到 1440 分钟之间")
        updated = dict(self.config)
        updated["publish_min_interval_seconds"] = minutes * 60
        atomic_write_json(self.config_path, updated)
        self.config = updated
        return ["publish_min_interval_minutes"]

    @staticmethod
    def expected_hardsub(task_dir: Path) -> Path:
        return task_dir / "stage4" / "video" / "final_bilingual_hardsub.mp4"

    @staticmethod
    def expected_source_video(task_dir: Path) -> Path:
        return task_dir / "video" / "source.mp4"

    def media_for_payload(self, task_dir: Path, payload: dict[str, Any]) -> Path:
        return (
            self.expected_source_video(task_dir)
            if payload.get("publish_original_video") is True
            else self.expected_hardsub(task_dir)
        )

    def defaults(
        self,
        task_dir: Path,
        *,
        publish_original_video: bool | None = None,
    ) -> dict[str, Any]:
        download = read_json(task_dir / "download_manifest.json")
        info = read_json(task_dir / "metadata" / "info.json")
        recommendation = read_json(task_dir / "stage3" / "publish_metadata.json")
        title = str(download.get("title") or info.get("title") or task_dir.name).strip()
        video_id = str(download.get("video_id") or info.get("id") or "").strip()
        source_url = str(
            download.get("url")
            or info.get("webpage_url")
            or info.get("original_url")
            or ""
        ).strip()
        if not source_url and video_id:
            source_url = f"https://www.youtube.com/watch?v={video_id}"
        description_path = task_dir / "metadata" / "description.txt"
        original_description = (
            description_path.read_text(encoding="utf-8-sig", errors="replace")
            if description_path.is_file()
            else str(info.get("description") or "")
        )
        original_media = (
            no_english_subtitle_or_recognized_speech(task_dir)
            if publish_original_video is None
            else bool(publish_original_video)
        )
        chinese_title = str(recommendation.get("title_zh") or "").strip()
        upload_title = (
            compose_localized_title(
                chinese_title,
                title,
                prefix="【无配音】",
                fallback_title="无配音精选",
            )
            if original_media
            else compose_bilingual_title(chinese_title, title)
        )
        recommended_tid = int(
            recommendation.get("tid")
            or self.config.get("default_tid")
            or self.category_mapping["fallback_tid"]
        )
        try:
            category = category_for_tid(self.category_mapping, recommended_tid)
        except ValueError:
            category = category_for_tid(
                self.category_mapping,
                int(self.category_mapping["fallback_tid"]),
            )
            recommended_tid = int(category["tid"])
        description = build_publish_description(
            original_description,
            disclaimer=str(
                (
                    "【免责声明】\n本视频为无配音或背景音乐内容；标题、简介、标签和分区"
                    "已进行中文本地化，视频画面及音轨保持原样。"
                )
                if original_media
                else (
                    self.config.get("description_disclaimer")
                    or "【免责声明】\n本视频为中英双语本地化版本，请以原视频内容为准。"
                )
            ),
            original_heading=str(
                self.config.get("description_original_heading")
                or "【原视频简介】"
            ),
        )
        cover = task_dir / "metadata" / "thumbnail.jpg"
        accounts = self.accounts()
        media = (
            self.expected_source_video(task_dir)
            if original_media
            else self.expected_hardsub(task_dir)
        )
        translated = deepseek_translation_ready(task_dir)
        metadata_status = str(recommendation.get("status") or "MISSING")
        return {
            "title": upload_title,
            "title_zh": chinese_title,
            "original_title": title,
            "description": description,
            "description_units": utf16_code_units(description),
            "description_max_units": BILIBILI_DESCRIPTION_MAX_UNITS,
            "description_bytes": utf8_bytes(description),
            "description_max_bytes": BILIBILI_DESCRIPTION_MAX_UTF8_BYTES,
            "dynamic": upload_title,
            "tags": normalize_tags(
                recommendation.get("tags"),
                fallback=[category["name"]],
                required=["无配音"] if original_media else None,
                excluded=["中英双语", "中文翻译"] if original_media else None,
            ),
            "copyright": int(self.config.get("default_copyright", 2)),
            "source": source_url,
            "tid": recommended_tid,
            "category_name": category["name"],
            "category_path": category["path"],
            "recommendation_reason": str(
                recommendation.get("recommendation_reason")
                or "尚未生成智能推荐，请先完成中文字幕翻译。"
            ),
            "metadata_status": metadata_status,
            "metadata_warning": str(recommendation.get("warning") or ""),
            "categories": self.category_mapping["categories"],
            "category_source": self.category_mapping["source"],
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
            "publish_original_video": original_media,
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
        if utf16_code_units(title) > 80:
            raise ValueError("投稿标题不能超过 80 个字符")
        publish_original_video = values.get("publish_original_video") is True or (
            "publish_original_video" not in values
            and no_english_subtitle_or_recognized_speech(task_dir)
        )
        expected_prefix = "【无配音】" if publish_original_video else "【中英双语】"
        if not title.startswith(expected_prefix):
            raise ValueError(f"投稿标题必须以{expected_prefix}开头")
        if not re.search(r"[\u3400-\u9fff]", title):
            raise ValueError("投稿标题必须包含中文标题")
        if utf16_code_units(description) > BILIBILI_DESCRIPTION_MAX_UNITS:
            raise ValueError(
                "投稿简介超过哔哩哔哩的 2000 字限制；emoji 会按 2 个字符计算"
            )
        if utf8_bytes(description) > BILIBILI_DESCRIPTION_MAX_UTF8_BYTES:
            raise ValueError(
                "投稿简介超过安全长度；中文、emoji 和特殊符号会占用更多空间，"
                "请缩短简介后再投稿"
            )
        if "免责声明" not in description:
            raise ValueError("投稿简介必须保留免责声明")
        if utf16_code_units(dynamic) > 255:
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
        category = category_for_tid(self.category_mapping, tid)
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
        translated = deepseek_translation_ready(task_dir)
        media = self.media_for_payload(
            task_dir,
            {"publish_original_video": publish_original_video},
        )
        if not publish_original_video and not translated and not (
            media.is_file() and media.stat().st_size > 0
        ):
            raise ValueError("中文字幕尚未完成，请先运行双语字幕处理")
        if publish_original_video and not (
            media.is_file() and media.stat().st_size > 0
        ):
            raise ValueError("原始视频不存在或为空，无法按无配音模式投稿")
        if values.get("confirm_publish") is not True:
            raise ValueError("投稿前必须确认版权、分区、标题和来源均已核对")

        return {
            "title": title,
            "description": description,
            "dynamic": dynamic,
            "tags": normalize_tags(
                tags,
                required=["无配音"] if publish_original_video else None,
                excluded=["中英双语", "中文翻译"] if publish_original_video else None,
            ),
            "copyright": copyright_value,
            "source": source,
            "tid": tid,
            "category_name": category["name"],
            "parent_name": category["parent_name"],
            "category_path": category["path"],
            "submit": submit,
            "line": line,
            "limit": limit,
            "no_reprint": values.get("no_reprint") is True,
            "is_only_self": values.get("is_only_self") is True,
            "use_cover": use_cover,
            "account_id": account["id"],
            "account_label": account["label"],
            "prepare_hardsub": not (media.is_file() and media.stat().st_size > 0),
            "publish_original_video": publish_original_video,
        }

    def automatic_submission(
        self,
        task_dir: Path,
        *,
        account_id: str = "",
        is_only_self: bool | None = None,
        publish_original_video: bool = False,
    ) -> dict[str, Any]:
        """Build a validated payload for an explicitly enabled unattended run."""
        defaults = self.defaults(
            task_dir,
            publish_original_video=publish_original_video,
        )
        values = {
            **defaults,
            "account_id": account_id or defaults.get("account_id") or "",
            "confirm_publish": True,
        }
        if is_only_self is not None:
            values["is_only_self"] = bool(is_only_self)
        payload = self.validate_submission(task_dir, values)
        payload["automatic"] = True
        return payload

    @staticmethod
    def mark_automation_skipped(
        task_dir: Path,
        reason: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        atomic_write_json(
            task_dir / "stage5" / "automation_manifest.json",
            {
                "schema_version": 1,
                "status": "SKIPPED",
                "reason": str(reason),
                "details": dict(details or {}),
                "finished_at": utc_now(),
            },
        )

    @staticmethod
    def mark_automation_original_media(task_dir: Path) -> None:
        atomic_write_json(
            task_dir / "stage5" / "automation_manifest.json",
            {
                "schema_version": 1,
                "status": "ORIGINAL_MEDIA",
                "reason": "NO_NARRATION_OR_BACKGROUND_MUSIC",
                "details": {
                    "message": "未检测到可用语音；保留原画面和音轨，仅本地化投稿信息"
                },
                "finished_at": utc_now(),
            },
        )

    @staticmethod
    def prepare_payload_for_execution(payload: dict[str, Any]) -> dict[str, Any]:
        """Make previously queued publish payloads safe under current validators."""
        prepared = dict(payload)
        prepared["description"] = truncate_utf8(
            truncate_utf16(
                str(payload.get("description") or "").strip(),
                BILIBILI_DESCRIPTION_MAX_UNITS,
            ),
            BILIBILI_DESCRIPTION_MAX_UTF8_BYTES,
        )
        prepared["title"] = truncate_utf16(
            " ".join(str(payload.get("title") or "").split()),
            80,
        )
        prepared["dynamic"] = truncate_utf16(
            str(payload.get("dynamic") or "").strip(),
            255,
        )
        return prepared

    @staticmethod
    def redact_log_text(log_text: str) -> str:
        """Remove Bilibili session material from uploader output before persistence."""
        return SENSITIVE_LOG_VALUE_PATTERN.sub(r"\1<redacted>", str(log_text))

    @staticmethod
    def is_transient_upload_failure(log_text: str) -> bool:
        normalized = str(log_text).casefold()
        return any(pattern in normalized for pattern in TRANSIENT_UPLOAD_PATTERNS)

    @staticmethod
    def response_error(log_text: str) -> tuple[int, str] | None:
        matches = list(RESPONSE_ERROR_PATTERN.finditer(str(log_text)))
        if not matches:
            return None
        match = matches[-1]
        return int(match.group(1)), match.group(2).strip()

    @staticmethod
    def is_publish_rate_limited(log_text: str) -> bool:
        response = BiliupIntegration.response_error(log_text)
        return response is not None and response[0] == 137022

    @staticmethod
    def failed_upload_is_safe_to_retry(log_text: str) -> bool:
        """Allow one-click retry only when failure happened before submission."""
        normalized = str(log_text).casefold()
        if BiliupIntegration.is_publish_rate_limited(log_text):
            # Bilibili explicitly rejected archive creation, so retrying after
            # the scheduler cooldown cannot create a duplicate submission.
            return True
        if not BiliupIntegration.is_transient_upload_failure(normalized):
            return False
        if BVID_PATTERN.search(log_text) or any(
            marker in normalized
            for marker in (
                "投稿成功",
                "submit success",
                "archive created",
                "稿件创建成功",
            )
        ):
            return False
        return any(
            marker in normalized
            for marker in (
                "passport-login/oauth2/info",
                "/preupload?",
                "pre_upload:",
            )
        )

    def transient_retry_delays(self) -> list[float]:
        raw = self.config.get("transient_retry_delays_seconds", [3, 8, 15])
        if not isinstance(raw, list):
            return [3.0, 8.0, 15.0]
        delays: list[float] = []
        for value in raw[:5]:
            try:
                delay = float(value)
            except (TypeError, ValueError):
                continue
            if 0 <= delay <= 60:
                delays.append(delay)
        return delays

    def publish_min_interval_seconds(self) -> int:
        try:
            value = int(self.config.get("publish_min_interval_seconds", 180))
        except (TypeError, ValueError):
            value = 180
        return max(0, min(value, 86400))

    def publish_daily_limit(self) -> int:
        try:
            value = int(self.config.get("publish_daily_limit", 20))
        except (TypeError, ValueError):
            value = 20
        return max(0, min(value, 100))

    def publish_rate_limit_cooldown_seconds(self) -> int:
        try:
            value = int(
                self.config.get("publish_rate_limit_cooldown_seconds", 21600)
            )
        except (TypeError, ValueError):
            value = 21600
        return max(300, min(value, 604800))

    def retry_upload_command(
        self,
        command: list[str],
        retry_number: int,
    ) -> list[str]:
        """Use a different CDN after an automatic-line upload attempt fails."""
        if "--line" in command:
            return list(command)
        raw_lines = self.config.get(
            "transient_retry_lines",
            ["bldsa", "bda2", "tx"],
        )
        lines = [
            str(value).strip()
            for value in raw_lines
            if str(value).strip() in ALLOWED_UPLOAD_LINES
        ] if isinstance(raw_lines, list) else []
        index = int(retry_number) - 1
        if index < 0 or index >= len(lines):
            return list(command)
        return [*command[:-1], "--line", lines[index], command[-1]]

    def configure_upload_environment(self, environment: dict[str, str]) -> None:
        """Keep large Bilibili uploads off an optional Windows/system proxy."""
        if not bool(self.config.get("bypass_system_proxy_for_upload", True)):
            return
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            environment.pop(name, None)
        # biliup only talks to Bilibili during this command.  A wildcard also
        # covers CDN hosts selected dynamically after the initial line probe.
        environment["NO_PROXY"] = "*"
        environment["no_proxy"] = "*"

    @staticmethod
    def explain_upload_failure(log_text: str, exit_code: int) -> str:
        response = BiliupIntegration.response_error(log_text)
        if response is not None:
            code, message = response
            if code == 21010:
                return (
                    "哔哩哔哩拒绝投稿：简介字数过长（错误码 21010）。"
                    "面板现已按哔哩哔哩规则自动缩短，"
                    "请重新打开该任务的投稿窗口并确认。"
                )
            if message and "�" not in message:
                return f"哔哩哔哩拒绝投稿：{message}（错误码 {code}）"
            return f"哔哩哔哩拒绝投稿（错误码 {code}）"
        if BiliupIntegration.is_transient_upload_failure(log_text):
            return (
                "哔哩哔哩连接在登录校验或上传阶段被中断；"
                "系统已完成自动重试与线路降级，但网络仍未恢复。"
            )
        return f"上传并提交到哔哩哔哩失败，退出代码 {exit_code}"

    def build_upload_command(
        self,
        task_dir: Path,
        payload: dict[str, Any],
    ) -> list[str]:
        executable = self.executable()
        if executable is None:
            raise FileNotFoundError("未找到 biliup.exe")
        account_path, _ = self.resolve_account(str(payload.get("account_id") or ""))
        media = self.media_for_payload(task_dir, payload)
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

    def mark_waiting(
        self,
        task_dir: Path,
        payload: dict[str, Any],
        *,
        reason: str,
        resume_at: str,
    ) -> None:
        previous = read_json(task_dir / "stage5" / "publish_manifest.json")
        atomic_write_json(
            task_dir / "stage5" / "publish_manifest.json",
            self._manifest_base(task_dir, payload)
            | {
                "status": "WAITING",
                "started_at": str(previous.get("started_at") or utc_now()),
                "finished_at": "",
                "resume_at": str(resume_at),
                "wait_reason": str(reason),
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
        media = self.media_for_payload(task_dir, payload)
        stage4 = read_json(task_dir / "stage4" / "stage4_manifest.json")
        return {
            "schema_version": 1,
            "platform": "bilibili",
            "uploader": self.version(),
            "account": str(payload.get("account_label") or ""),
            "media_path": str(media),
            "media_size": media.stat().st_size if media.is_file() else 0,
            "media_hash": (
                str(stage4.get("hardsub_output_hash") or "")
                if not payload.get("publish_original_video")
                else self._sha256_file(media)
            ),
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
                    "category_name",
                    "parent_name",
                    "category_path",
                    "submit",
                    "line",
                    "limit",
                    "no_reprint",
                    "is_only_self",
                    "use_cover",
                    "publish_original_video",
                )
            },
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        if not path.is_file():
            return ""
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


__all__ = ["BiliupIntegration"]
