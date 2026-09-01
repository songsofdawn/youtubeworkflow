from __future__ import annotations

import json
import os
import shutil
import subprocess
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..dubbing.config import public_dubbing_health
from ..portable_runtime import load_portable_manifest, resolve_python_executable
from ..stage4.layout_review import load_layout_review, save_layout_review
from ..stage3.llm_providers import (
    API_KEY_ENV_NAMES,
    PROVIDER_BY_ID,
    public_provider_catalog,
)
from .jobs import JobStore, WorkflowWorker
from .publishing import BiliupIntegration
from .settings import (
    normalize_secret,
    save_youtube_cookie_file,
    update_discovery_settings,
    update_env_file,
    youtube_cookie_status,
)
from .tasks import (
    WorkflowScanner,
    deepseek_translation_ready,
    read_json,
    youtube_auto_chinese_path,
    youtube_chinese_path,
)
from .youtube import (
    TargetedYouTubeSearch,
    load_env_values,
    normalize_video_inputs,
)


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
            self._run_discovery_job,
        )

    def start(self) -> None:
        self.worker.start()

    def close(self) -> None:
        self.worker.close()

    def health(self) -> dict[str, Any]:
        env_values = load_env_values(self.project_root / ".env")

        def configured(name: str) -> bool:
            return bool(os.getenv(name, "").strip() or env_values.get(name, "").strip())

        effective_env = dict(env_values)
        if self.project_root == Path(__file__).resolve().parents[2]:
            for name in (
                *API_KEY_ENV_NAMES,
                "TRANSLATION_PROVIDER",
                "TRANSLATION_MODEL",
                "TRANSLATION_BASE_URL",
                "TRANSLATION_THINKING",
                "TRANSLATION_BATCH_SIZE",
                "TRANSLATION_CONTEXT_BEFORE",
                "TRANSLATION_CONTEXT_AFTER",
                "TRANSLATION_MAX_OUTPUT_TOKENS",
            ):
                if name in os.environ:
                    effective_env[name] = os.environ[name]
        llm = public_provider_catalog(values=effective_env)
        active_provider = PROVIDER_BY_ID[llm["active"]["provider"]]
        translation_api_ready = configured(active_provider.key_env)

        python_runtime = resolve_python_executable(self.project_root, required=False)
        tools = {
            name: (self.project_root / "tools" / "bin" / f"{name}.exe").is_file()
            for name in ("yt-dlp", "ffmpeg", "ffprobe")
        }
        model = self.project_root / "models" / "faster-whisper-large-v3"
        cookie_path = self.project_root / "private" / "cookies.txt"
        cookie_status = youtube_cookie_status(cookie_path)
        publishing = self.publisher.health()
        dubbing = public_dubbing_health(self.project_root)
        profile = load_portable_manifest(self.project_root)
        stage3_config = read_json(self.project_root / "config" / "stage3_config.json")
        asr_config = stage3_config.get("asr") if isinstance(stage3_config.get("asr"), dict) else {}
        portable_profile = bool(profile.get("portable"))
        asr_device = str(
            (profile.get("asr_device") if portable_profile else None)
            or asr_config.get("device")
            or "unknown"
        )
        asr_compute_type = str(
            (profile.get("asr_compute_type") if portable_profile else None)
            or asr_config.get("compute_type")
            or "unknown"
        )
        checks = {
            "python_runtime": python_runtime is not None,
            "tools": all(tools.values()),
            "whisper_model": model.is_dir(),
            "youtube_api": configured("YOUTUBE_API_KEY"),
            "youtube_cookies": bool(cookie_status["ready"]),
            "translation_api": translation_api_ready,
            "deepseek_api": translation_api_ready,
            "biliup": publishing["available"],
            "biliup_account": publishing["account_ready"],
            "dubbing_runtime": bool(
                dubbing["runtime_ready"]
                and dubbing["demucs_ready"]
                and dubbing["voxcpm_ready"]
                and dubbing["device_ready"]
                and dubbing["torchcodec_ready"]
            ),
            "voxcpm2_model": bool(dubbing["model_ready"]),
        }
        discovery = self.searcher.discovery_health()
        checks["discovery_llm"] = bool(
            discovery.get("reachable") and discovery.get("model_ready")
        )
        return {
            "ready": all(
                checks[name]
                for name in (
                    "python_runtime",
                    "tools",
                    "whisper_model",
                )
            ),
            "checks": checks,
            "tools": tools,
            "publishing": publishing,
            "dubbing": dubbing,
            "youtube_cookies": cookie_status,
            "llm": llm,
            "discovery": discovery,
            "profile": profile,
            "asr": {
                "edition": str(profile.get("edition") or "development"),
                "device": asr_device,
                "compute_type": asr_compute_type,
                "model": "faster-whisper-large-v3",
                "model_ready": model.is_dir(),
                "performance_note": (
                    "CPU 模式可用，但 large-v3 识别速度较慢"
                    if asr_device.casefold() == "cpu"
                    else "GPU 模式可用，需要兼容的 NVIDIA 驱动"
                ),
            },
        }

    def save_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        current = self.health()["llm"]["active"]
        provider_id = str(values.get("translation_provider") or current["provider"]).strip().casefold()
        if provider_id not in PROVIDER_BY_ID:
            raise ValueError("不支持的 API 供应商")
        provider = PROVIDER_BY_ID[provider_id]
        provider_changed = provider_id != current["provider"]
        field_mapping = {
            "youtube_api_key": "YOUTUBE_API_KEY",
            "deepseek_api_key": "DEEPSEEK_API_KEY",
        }
        updates = {
            env_name: normalize_secret(values[field], env_name)
            for field, env_name in field_mapping.items()
            if field in values
        }
        if "translation_api_key" in values:
            updates[provider.key_env] = normalize_secret(
                values["translation_api_key"], provider.key_env
            )
        if "translation_provider" in values:
            updates["TRANSLATION_PROVIDER"] = provider.id
        if "translation_model" in values or "translation_provider" in values:
            model = str(values.get("translation_model") or provider.default_model).strip()
            allowed_models = {item.id for item in provider.models}
            if not model or len(model) > 160:
                raise ValueError("模型名称无效")
            if not provider.custom_model and model not in allowed_models:
                raise ValueError("该供应商不支持这个模型选项")
            updates["TRANSLATION_MODEL"] = model
        if "translation_base_url" in values or "translation_provider" in values:
            base_url = str(values.get("translation_base_url") or provider.base_url).strip()
            parsed = urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
                raise ValueError("API Base URL 必须是有效的 http(s) 地址")
            if len(base_url) > 500 or "\n" in base_url or "\r" in base_url:
                raise ValueError("API Base URL 无效")
            updates["TRANSLATION_BASE_URL"] = base_url.rstrip("/")
        if "translation_thinking" in values:
            thinking = str(values["translation_thinking"]).strip().casefold()
            if thinking not in {"enabled", "disabled"}:
                raise ValueError("Thinking 模式无效")
            updates["TRANSLATION_THINKING"] = thinking
        elif provider_changed:
            updates["TRANSLATION_THINKING"] = provider.default_thinking

        number_fields = {
            "translation_batch_size": ("TRANSLATION_BATCH_SIZE", 1, 100),
            "translation_context_before": ("TRANSLATION_CONTEXT_BEFORE", 0, 10),
            "translation_context_after": ("TRANSLATION_CONTEXT_AFTER", 0, 10),
            "translation_max_output_tokens": ("TRANSLATION_MAX_OUTPUT_TOKENS", 256, 32768),
        }
        for field, (env_name, minimum, maximum) in number_fields.items():
            if field not in values:
                continue
            try:
                number = int(values[field])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field} 必须是整数") from exc
            if not minimum <= number <= maximum:
                raise ValueError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
            updates[env_name] = str(number)
        discovery_saved = update_discovery_settings(
            self.project_root / "config" / "trending_config.json",
            values,
        )
        publish_saved = self.publisher.update_publish_settings(values)
        if not updates and not discovery_saved and not publish_saved:
            raise ValueError("没有需要保存的设置")
        if updates:
            update_env_file(self.project_root / ".env", updates)
        if self.project_root == Path(__file__).resolve().parents[2]:
            for name, value in updates.items():
                os.environ[name] = value
        return {
            "saved": [*sorted(updates), *discovery_saved, *publish_saved],
            "health": self.health(),
        }

    def update_youtube_cookies(self, values: dict[str, Any]) -> dict[str, Any]:
        cookie_path = self.project_root / "private" / "cookies.txt"
        action = str(values.get("action") or "save")
        if action == "clear":
            cookie_path.unlink(missing_ok=True)
            return {"cleared": True, "health": self.health()}
        if action != "save":
            raise ValueError("不支持的 Cookies 操作")
        save_youtube_cookie_file(cookie_path, values.get("content"))
        return {"saved": True, "health": self.health()}

    def open_biliup_login(self) -> dict[str, Any]:
        executable = self.project_root / "biliup" / "bbup-app" / "tauri-app.exe"
        if not executable.is_file():
            raise FileNotFoundError(f"未找到哔哩哔哩登录工具：{executable}")
        subprocess.Popen(
            [str(executable)],
            cwd=executable.parent,
            shell=False,
        )
        return {"opened": True}

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
            "scheduler": self.worker.snapshot(jobs),
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

    def discovery_catalog(self) -> list[dict[str, Any]]:
        return self.searcher.discovery_catalog()

    def discover(
        self,
        pack_ids: list[str],
        hours: int,
        per_pack: int,
        minimum_duration_minutes: int = 5,
        maximum_duration_minutes: int | None = None,
    ) -> dict[str, Any]:
        existing_tasks = self.scanner.scan()
        known_video_ids = {
            str(task.get("video_id") or "")
            for task in existing_tasks
            if str(task.get("video_id") or "")
        }
        known_titles = [str(task.get("title") or "") for task in existing_tasks]
        return self.searcher.discover(
            pack_ids,
            hours,
            per_pack,
            known_video_ids=known_video_ids,
            known_titles=known_titles,
            minimum_duration_seconds=int(minimum_duration_minutes) * 60,
            maximum_duration_seconds=(
                int(maximum_duration_minutes) * 60
                if maximum_duration_minutes is not None
                else None
            ),
        )

    def queue_discovery(
        self,
        pack_ids: list[str],
        hours: int,
        per_pack: int,
        minimum_duration_minutes: int = 5,
        maximum_duration_minutes: int | None = None,
    ) -> dict[str, Any]:
        selected_ids = list(dict.fromkeys(str(value) for value in pack_ids))
        catalog_ids = {str(item["id"]) for item in self.discovery_catalog()}
        unknown = [value for value in selected_ids if value not in catalog_ids]
        if not selected_ids:
            raise ValueError("请至少选择一个发现领域")
        if unknown:
            raise ValueError("包含未知的发现领域：" + "、".join(unknown))
        if int(hours) not in {24, 72, 168, 336, 720}:
            raise ValueError("发现时间范围只支持 24、72、168、336 或 720 小时")
        if not 1 <= int(per_pack) <= 100:
            raise ValueError("每个领域的结果数量必须在 1 到 100 之间")
        configured_maximum_duration_minutes = int(
            self.searcher.discovery_settings().get("maximum_duration_minutes") or 45
        )
        requested_maximum_duration_minutes = int(
            maximum_duration_minutes
            if maximum_duration_minutes is not None
            else configured_maximum_duration_minutes
        )
        if not 1 <= int(minimum_duration_minutes) <= configured_maximum_duration_minutes:
            raise ValueError(
                f"候选最小时长必须在 1 到 {configured_maximum_duration_minutes} 分钟之间"
            )
        if not 1 <= requested_maximum_duration_minutes <= configured_maximum_duration_minutes:
            raise ValueError(
                f"候选最大时长必须在 1 到 {configured_maximum_duration_minutes} 分钟之间"
            )
        if int(minimum_duration_minutes) > requested_maximum_duration_minutes:
            raise ValueError("候选最小时长不能大于候选最大时长")
        existing_tasks = self.scanner.scan()
        payload = {
            "packs": selected_ids,
            "hours": int(hours),
            "per_pack": int(per_pack),
            "minimum_duration_seconds": int(minimum_duration_minutes) * 60,
            "maximum_duration_seconds": requested_maximum_duration_minutes * 60,
            "known_video_ids": [
                str(task.get("video_id") or "")
                for task in existing_tasks
                if str(task.get("video_id") or "")
            ],
            "known_titles": [
                str(task.get("title") or "")
                for task in existing_tasks
                if str(task.get("title") or "").strip()
            ],
        }
        job = self.store.enqueue(
            "discovery",
            "smart-discovery",
            payload,
            resource_class="gpu_heavy",
        )
        self.worker.wake()
        return job

    def _run_discovery_job(
        self,
        payload: dict[str, Any],
        *,
        progress: Any | None = None,
        cancelled: Any | None = None,
    ) -> dict[str, Any]:
        return self.searcher.discover(
            [str(value) for value in payload.get("packs", [])],
            int(payload.get("hours") or 168),
            int(payload.get("per_pack") or 20),
            known_video_ids={str(value) for value in payload.get("known_video_ids", [])},
            known_titles=[str(value) for value in payload.get("known_titles", [])],
            minimum_duration_seconds=int(payload.get("minimum_duration_seconds") or 300),
            maximum_duration_seconds=(
                int(payload["maximum_duration_seconds"])
                if payload.get("maximum_duration_seconds") is not None
                else None
            ),
            progress=progress,
            cancelled=cancelled,
        )

    def discovery_job_result(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job["kind"] != "discovery":
            raise ValueError("该任务不是智能发现任务")
        response: dict[str, Any] = {"job": job, "result": None}
        if job["status"] != "completed":
            return response
        result_root = (
            self.project_root / "work" / "control_panel" / "discovery_results"
        ).resolve()
        result_path = (result_root / f"{job_id}.json").resolve()
        try:
            result_path.relative_to(result_root)
        except ValueError as exc:
            raise ValueError("智能发现结果路径无效") from exc
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"智能发现结果无法读取：{exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("智能发现结果格式无效")
        response["result"] = payload
        return response

    def record_discovery_feedback(
        self,
        item: dict[str, Any],
        feedback: str,
    ) -> dict[str, Any]:
        return self.searcher.record_discovery_feedback(item, feedback)

    def queue_downloads(
        self,
        *,
        raw_input: str = "",
        items: list[dict[str, Any]] | None = None,
        confirm_rights: bool,
        auto_publish: bool = False,
        whisper_for_auto_subtitles: bool = True,
        auto_translate_missing: bool = True,
        publish_metadata_provider: str = "auto",
        account_id: str = "",
        publish_only_self: bool = False,
        automation_render_mode: str = "hardsub",
        automation_failure_policy: str = "skip",
        automation_target: str = "publish",
        english_subtitle_policy: str = "",
        automation_chinese_policy: str = "",
        automation_silent_video_policy: str = "publish_original",
        automation_dubbing_review_policy: str = "auto_fallback",
        dubbing_enabled: bool = False,
        dubbing_reference_mode: str = "auto",
        dubbing_reference_start: float | None = None,
        dubbing_reference_end: float | None = None,
        dubbing_subtitle_display: str = "chinese",
        force_dubbing: bool = False,
    ) -> list[dict[str, Any]]:
        if not confirm_rights:
            raise ValueError("下载前必须确认拥有下载和使用这些视频的权利")
        normalized: list[dict[str, str]]
        if items:
            raw_urls = " ".join(str(item.get("youtube_url") or item.get("url") or "") for item in items)
            normalized = normalize_video_inputs(raw_urls)
            for item in items:
                if isinstance(item, dict) and item.get("video_id"):
                    self.searcher.record_discovery_feedback(item, "selected")
        else:
            normalized = normalize_video_inputs(raw_input)
        dubbing = self._dubbing_payload(
            enabled=dubbing_enabled,
            reference_mode=dubbing_reference_mode,
            reference_start=dubbing_reference_start,
            reference_end=dubbing_reference_end,
            subtitle_display=dubbing_subtitle_display,
            force=force_dubbing,
        )
        if auto_publish and dubbing["dubbing_enabled"]:
            normalized_target = str(automation_target or "publish").strip().casefold()
            normalized_render_mode = str(
                automation_render_mode or "hardsub"
            ).strip().casefold()
            normalized_chinese_policy = str(
                automation_chinese_policy
                or ("youtube_preferred" if auto_translate_missing else "youtube_only")
            ).strip().casefold()
            if normalized_target == "subtitles":
                raise ValueError("仅生成字幕的自动化流程不能同时生成中文配音")
            if normalized_render_mode == "ass":
                raise ValueError("中文配音需要生成带音轨的 MP4 或 MKV，不能只生成 ASS")
            if normalized_chinese_policy != "api_always":
                raise ValueError(
                    "无人值守中文配音必须选择“始终使用 API 翻译”，"
                    "因为配音不会直接使用 YouTube 自动中文字幕"
                )
        automation = self._automation_payload(
            enabled=auto_publish,
            publish_metadata_provider=publish_metadata_provider,
            account_id=account_id,
            publish_only_self=publish_only_self,
            translation_may_be_needed=(
                auto_publish
                and str(
                    automation_chinese_policy
                    or ("youtube_preferred" if auto_translate_missing else "youtube_only")
                ).strip().casefold()
                != "youtube_only"
            ),
            render_mode=automation_render_mode,
            failure_policy=automation_failure_policy,
            target=automation_target,
            english_policy=(
                english_subtitle_policy
                or ("quality" if whisper_for_auto_subtitles else "youtube_first")
            ),
            chinese_policy=(
                automation_chinese_policy
                or ("youtube_preferred" if auto_translate_missing else "youtube_only")
            ),
            silent_video_policy=automation_silent_video_policy,
            dubbing_review_policy=automation_dubbing_review_policy,
        )
        jobs = [
            self.store.enqueue(
                "download",
                item["video_id"],
                {
                    "url": item["url"],
                    "video_id": item["video_id"],
                    **dubbing,
                    **automation,
                },
                resource_class="network",
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
        whisper_for_auto_subtitles: bool = True,
        auto_translate_missing: bool = True,
        auto_publish: bool = False,
        publish_metadata_provider: str = "auto",
        account_id: str = "",
        publish_only_self: bool = False,
        automation_failure_policy: str = "skip",
        automation_target: str = "publish",
        english_subtitle_policy: str = "",
        automation_chinese_policy: str = "",
        automation_silent_video_policy: str = "publish_original",
        automation_dubbing_review_policy: str = "auto_fallback",
        dubbing_enabled: bool = False,
        dubbing_reference_mode: str = "auto",
        dubbing_reference_start: float | None = None,
        dubbing_reference_end: float | None = None,
        dubbing_subtitle_display: str = "chinese",
        force_dubbing: bool = False,
    ) -> list[dict[str, Any]]:
        normalized_target = str(automation_target or "publish").strip().casefold()
        normalized_english_policy = str(
            english_subtitle_policy
            or ("quality" if whisper_for_auto_subtitles else "youtube_first")
        ).strip().casefold()
        normalized_chinese_policy = str(
            automation_chinese_policy
            or ("youtube_preferred" if auto_translate_missing else "youtube_only")
        ).strip().casefold()
        if auto_publish:
            if normalized_target not in {"subtitles", "render", "publish"}:
                raise ValueError("不支持的自动化流程终点")
            if normalized_english_policy not in {"quality", "youtube_first", "whisper"}:
                raise ValueError("不支持的英文字幕策略")
            if normalized_chinese_policy not in {
                "youtube_preferred",
                "api_always",
                "youtube_only",
            }:
                raise ValueError("不支持的中文字幕策略")
            expected_workflow = (
                "subtitles" if normalized_target == "subtitles" else "complete"
            )
            if workflow != expected_workflow:
                raise ValueError("自动化流程终点与处理流程不匹配")
            chinese_subtitle_source = (
                "deepseek" if normalized_chinese_policy == "api_always" else "auto"
            )
            auto_translate_missing = normalized_chinese_policy != "youtube_only"
            whisper_for_auto_subtitles = normalized_english_policy != "youtube_first"
        if workflow not in {"subtitles", "render", "complete", "dubbing"}:
            raise ValueError("不支持的处理流程")
        if render_mode not in {"ass", "softsub", "hardsub", "both"}:
            raise ValueError("不支持的成片模式")
        if chinese_subtitle_source not in {"auto", "deepseek", "youtube_auto"}:
            raise ValueError("不支持的中文字幕来源")
        if not tasks:
            raise ValueError("请至少选择一个已下载的视频")
        if len(tasks) > 50:
            raise ValueError("一次最多加入 50 个视频")
        dubbing = self._dubbing_payload(
            enabled=bool(dubbing_enabled or workflow == "dubbing"),
            reference_mode=dubbing_reference_mode,
            reference_start=dubbing_reference_start,
            reference_end=dubbing_reference_end,
            subtitle_display=dubbing_subtitle_display,
            force=force_dubbing,
        )
        if dubbing["dubbing_enabled"]:
            if workflow == "subtitles":
                raise ValueError("仅生成字幕的流程不能同时生成中文配音")
            if render_mode == "ass":
                raise ValueError("中文配音需要生成带音轨的 MP4 或 MKV，不能只生成 ASS")
            if chinese_subtitle_source == "youtube_auto":
                raise ValueError(
                    "中文配音只读取 zh.reviewed.srt 或 AI 翻译生成的 zh.clean.srt"
                )
            if auto_publish and normalized_chinese_policy != "api_always":
                raise ValueError(
                    "无人值守中文配音必须选择“始终使用 API 翻译”，"
                    "因为配音不会直接使用 YouTube 自动中文字幕"
                )
        if (
            auto_publish
            and normalized_target == "publish"
            and render_mode not in {"hardsub", "both"}
        ):
            raise ValueError("自动投稿必须生成硬字幕 MP4")

        validated: list[str] = []
        task_dirs: dict[str, Path] = {}
        for task in tasks:
            task_dirs[task] = self.scanner.resolve_task(task)
            if task not in validated:
                validated.append(task)
        if workflow in {"render", "dubbing"} and dubbing["dubbing_enabled"]:
            missing_dubbing_subtitles = [
                task
                for task in validated
                if not any(
                    (task_dirs[task] / "subtitles" / name).is_file()
                    and (task_dirs[task] / "subtitles" / name).stat().st_size > 0
                    for name in ("zh.reviewed.srt", "zh.clean.srt")
                )
            ]
            if missing_dubbing_subtitles:
                labels = "、".join(Path(task).name for task in missing_dubbing_subtitles[:5])
                raise ValueError(
                    f"以下视频没有可用于配音的 zh.reviewed.srt 或 zh.clean.srt：{labels}。"
                    "请先完成中文字幕翻译。"
                )
        automatic_missing = [
            task for task in validated
            if chinese_subtitle_source == "auto"
            and youtube_chinese_path(task_dirs[task]) is None
        ]
        if automatic_missing and not auto_translate_missing and not auto_publish:
            labels = "、".join(Path(task).name for task in automatic_missing[:5])
            raise ValueError(
                f"以下视频没有 YouTube 中文字幕：{labels}。"
                "请开启“缺少中文字幕时自动调用 API 翻译”。"
            )
        uses_translation_api = (
            workflow in {"subtitles", "complete"}
            and (
                chinese_subtitle_source == "deepseek"
                or (bool(automatic_missing) and auto_translate_missing)
            )
        )
        if uses_translation_api:
            if not allow_paid_api:
                raise ValueError("翻译会调用所选 AI API，请先在面板中确认")
            health = self.health()
            if not health["checks"]["translation_api"]:
                active = health["llm"]["active"]
                key_env = PROVIDER_BY_ID[active["provider"]].key_env
                raise ValueError(f"{key_env} 尚未配置")

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
                    "请改选 AI API 翻译。"
                )
        if workflow == "render" and chinese_subtitle_source == "deepseek":
            missing = [
                task for task in validated
                if not deepseek_translation_ready(task_dirs[task])
            ]
            if missing:
                labels = "、".join(Path(task).name for task in missing[:5])
                raise ValueError(
                    f"以下视频尚未完成 AI API 翻译：{labels}。"
                    "请先处理到双语字幕。"
                )
        automation = self._automation_payload(
            enabled=auto_publish,
            publish_metadata_provider=publish_metadata_provider,
            account_id=account_id,
            publish_only_self=publish_only_self,
            translation_may_be_needed=uses_translation_api,
            render_mode=render_mode,
            failure_policy=automation_failure_policy,
            target=normalized_target,
            english_policy=normalized_english_policy,
            chinese_policy=normalized_chinese_policy,
            silent_video_policy=automation_silent_video_policy,
            dubbing_review_policy=automation_dubbing_review_policy,
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
                    "whisper_for_auto_subtitles": bool(
                        whisper_for_auto_subtitles
                    ),
                    "english_subtitle_policy": normalized_english_policy,
                    "auto_translate_missing": bool(auto_translate_missing),
                    **dubbing,
                    **automation,
                },
                resource_class="gpu_heavy",
            )
            for task in validated
        ]
        self.worker.wake()
        return jobs

    def _dubbing_payload(
        self,
        *,
        enabled: bool = False,
        reference_mode: str = "auto",
        reference_start: float | None = None,
        reference_end: float | None = None,
        subtitle_display: str = "chinese",
        force: bool = False,
    ) -> dict[str, Any]:
        normalized_enabled = bool(enabled)
        normalized_reference_mode = str(reference_mode or "auto").strip().casefold()
        normalized_subtitle_display = str(
            subtitle_display or "chinese"
        ).strip().casefold()
        normalized_reference_start = (
            float(reference_start) if reference_start is not None else None
        )
        normalized_reference_end = (
            float(reference_end) if reference_end is not None else None
        )
        if normalized_enabled:
            if normalized_reference_mode not in {"auto", "manual"}:
                raise ValueError("中文配音参考声音模式只支持自动或手动")
            if normalized_subtitle_display not in {"chinese", "bilingual"}:
                raise ValueError("中文配音字幕显示只支持中文或中英双语")
            if normalized_reference_mode == "manual":
                if normalized_reference_start is None or normalized_reference_end is None:
                    raise ValueError("手动参考声音必须填写开始和结束时间")
                if (
                    normalized_reference_start < 0
                    or normalized_reference_end <= normalized_reference_start
                ):
                    raise ValueError("手动参考声音时间范围无效")
            dubbing_health = self.health()["dubbing"]
            if not (
                dubbing_health["runtime_ready"]
                and dubbing_health["demucs_ready"]
                and dubbing_health["voxcpm_ready"]
            ):
                raise ValueError(
                    "中文配音运行时缺少 Demucs / VoxCPM2；"
                    "请先按 requirements_dubbing.txt 创建 .venv_dubbing"
                )
            if dubbing_health.get("entrypoint_ready") is False:
                detail = str(dubbing_health.get("runtime_error") or "").strip()
                raise ValueError(
                    "中文配音入口无法加载"
                    + (f"：{detail}" if detail else "；请检查独立运行时依赖")
                )
            if dubbing_health.get("torchcodec_ready") is False:
                detail = str(
                    dubbing_health.get("preflight_error")
                    or dubbing_health.get("runtime_error")
                    or ""
                ).strip()
                raise ValueError(detail or "中文配音 TorchCodec / FFmpeg Shared 预检失败")
            if not dubbing_health["device_ready"]:
                raise ValueError(
                    "中文配音运行时的 PyTorch / CUDA 不可用；"
                    "请检查独立运行时与 NVIDIA 驱动"
                )
            if not dubbing_health["model_ready"]:
                raise ValueError(
                    f"VoxCPM2 本地模型未就绪：{dubbing_health['model_path']}"
                )
        return {
            "dubbing_enabled": normalized_enabled,
            "dubbing_reference_mode": normalized_reference_mode,
            "dubbing_reference_start": normalized_reference_start,
            "dubbing_reference_end": normalized_reference_end,
            "dubbing_subtitle_display": normalized_subtitle_display,
            "force_dubbing": bool(force),
        }

    def _automation_payload(
        self,
        *,
        enabled: bool,
        publish_metadata_provider: str,
        account_id: str,
        publish_only_self: bool,
        translation_may_be_needed: bool,
        render_mode: str,
        failure_policy: str,
        target: str,
        english_policy: str,
        chinese_policy: str,
        silent_video_policy: str,
        dubbing_review_policy: str,
    ) -> dict[str, Any]:
        if not enabled:
            return {}
        provider = str(publish_metadata_provider or "auto").strip().casefold()
        if provider not in {"auto", "translation_api", "local_ollama"}:
            raise ValueError("不支持的投稿信息模型")
        normalized_target = str(target or "publish").strip().casefold()
        if normalized_target not in {"subtitles", "render", "publish"}:
            raise ValueError("不支持的自动化流程终点")
        normalized_english_policy = str(english_policy or "quality").strip().casefold()
        if normalized_english_policy not in {"quality", "youtube_first", "whisper"}:
            raise ValueError("不支持的英文字幕策略")
        normalized_chinese_policy = str(
            chinese_policy or "youtube_preferred"
        ).strip().casefold()
        if normalized_chinese_policy not in {
            "youtube_preferred",
            "api_always",
            "youtube_only",
        }:
            raise ValueError("不支持的中文字幕策略")
        normalized_render_mode = str(render_mode or "hardsub").strip().casefold()
        if normalized_render_mode not in {"ass", "softsub", "hardsub", "both"}:
            raise ValueError("不支持的自动成片模式")
        if (
            normalized_target == "publish"
            and normalized_render_mode not in {"hardsub", "both"}
        ):
            raise ValueError("无人值守投稿必须生成硬字幕 MP4")
        normalized_failure_policy = str(failure_policy or "skip").strip().casefold()
        if normalized_failure_policy not in {"skip", "fail"}:
            raise ValueError("不支持的无人值守异常策略")
        normalized_silent_video_policy = str(
            silent_video_policy or "publish_original"
        ).strip().casefold()
        if normalized_silent_video_policy not in {"publish_original", "skip"}:
            raise ValueError("不支持的无配音视频处理策略")
        normalized_dubbing_review_policy = str(
            dubbing_review_policy or "auto_fallback"
        ).strip().casefold()
        if normalized_dubbing_review_policy not in {
            "auto_fallback",
            "block",
            "continue",
        }:
            raise ValueError("不支持的中文配音复核策略")
        health = self.health()
        api_ready = bool(health["checks"]["translation_api"])
        local_ready = bool(
            health["discovery"].get("reachable")
            and health["discovery"].get("model_ready")
        )
        if translation_may_be_needed and not api_ready:
            active = health["llm"]["active"]
            key_env = PROVIDER_BY_ID[active["provider"]].key_env
            raise ValueError(f"缺少中文字幕时需要翻译，但 {key_env} 尚未配置")
        if normalized_target == "publish" and provider == "translation_api" and not api_ready:
            active = health["llm"]["active"]
            key_env = PROVIDER_BY_ID[active["provider"]].key_env
            raise ValueError(f"自动生成投稿信息需要 {key_env}")
        if normalized_target == "publish" and provider == "local_ollama" and not local_ready:
            raise ValueError("本地 Ollama 投稿信息模型尚未就绪")
        if (
            normalized_target == "publish"
            and provider == "auto"
            and not (local_ready or api_ready)
        ):
            raise ValueError("自动投稿至少需要可用的本地 Ollama 或所选 AI API")
        selected_account = ""
        if normalized_target == "publish":
            publishing = self.publisher.health()
            if not publishing["available"]:
                raise ValueError("自动投稿前必须安装并配置 biliup")
            if not publishing["account_ready"]:
                raise ValueError("自动投稿前必须先登录哔哩哔哩账号")
            selected_account = str(account_id or publishing["accounts"][0]["id"])
            self.publisher.resolve_account(selected_account)
        auto_translate_missing = normalized_chinese_policy != "youtube_only"
        return {
            "automation_enabled": True,
            "automation_target": normalized_target,
            "auto_publish": normalized_target == "publish",
            "english_subtitle_policy": normalized_english_policy,
            "whisper_for_auto_subtitles": normalized_english_policy != "youtube_first",
            "automation_chinese_policy": normalized_chinese_policy,
            "chinese_subtitle_source": (
                "deepseek" if normalized_chinese_policy == "api_always" else "auto"
            ),
            "auto_translate_missing": bool(auto_translate_missing),
            "publish_metadata_provider": provider,
            "account_id": selected_account,
            "publish_only_self": bool(publish_only_self),
            "render_mode": normalized_render_mode,
            "automation_failure_policy": normalized_failure_policy,
            "automation_silent_video_policy": normalized_silent_video_policy,
            "automation_dubbing_review_policy": normalized_dubbing_review_policy,
            "allow_paid_api": bool(
                api_ready
                and (
                    auto_translate_missing
                    or (
                        normalized_target == "publish"
                        and provider in {"auto", "translation_api"}
                    )
                )
            ),
        }

    def render_review(self, task: str) -> dict[str, Any]:
        task_dir = self.scanner.resolve_task(task)
        return {"task": task, **load_layout_review(task_dir)}

    def save_render_review(
        self,
        *,
        task: str,
        edits: list[dict[str, Any]],
        render_mode: str,
    ) -> dict[str, Any]:
        task_dir = self.scanner.resolve_task(task)
        if self.store.active_for_targets({task}):
            raise ValueError("这个视频仍有运行中或排队中的任务，请先终止后再复核")
        config_path = self.project_root / "config" / "stage4_config.json"
        config = read_json(config_path)
        if not config:
            raise ValueError(f"成片配置无法读取：{config_path}")
        review = save_layout_review(task_dir, edits, config)
        job = None
        if review["ready_to_render"]:
            chinese_source = str(review.get("chinese_subtitle_source") or "deepseek")
            if chinese_source not in {"deepseek", "youtube_auto"}:
                chinese_source = (
                    "youtube_auto"
                    if youtube_auto_chinese_path(task_dir) is not None
                    else "deepseek"
                )
            job = self.queue_pipeline(
                tasks=[task],
                workflow="render",
                render_mode=render_mode,
                chinese_subtitle_source=chinese_source,
                allow_paid_api=False,
            )[0]
        return {"task": task, "review": review, "job": job}

    def retry_job(self, job_id: str) -> dict[str, Any]:
        existing = self.store.get(job_id)
        if existing["kind"] == "publish":
            task_dir = self.scanner.resolve_task(str(existing["target"]))
            manifest = read_json(task_dir / "stage5" / "publish_manifest.json")
            if str(manifest.get("status") or "") == "PUBLISHED":
                raise ValueError("该稿件已经投稿成功，已阻止重复投稿")
            log_text = self.store.log_tail(job_id, max_chars=100000)
            if not self.publisher.failed_upload_is_safe_to_retry(log_text):
                raise ValueError(
                    "无法确认上次投稿是否已在哔哩哔哩创建稿件；"
                    "为避免重复投稿，请先到创作中心确认。"
                )
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

    def delete_tasks(
        self,
        tasks: list[str],
        confirmation: str,
    ) -> dict[str, Any]:
        unique: list[str] = []
        for raw_task in tasks:
            task = str(raw_task or "").strip()
            if task and task not in unique:
                unique.append(task)
        if not unique:
            raise ValueError("请至少选择一个视频项目")
        if len(unique) > 200:
            raise ValueError("一次最多批量删除 200 个视频项目")
        expected_confirmation = f"删除 {len(unique)} 个项目"
        normalized_confirmation = "".join(
            unicodedata.normalize("NFKC", str(confirmation or "")).split()
        )
        normalized_expected = f"删除{len(unique)}个项目"
        if normalized_confirmation != normalized_expected:
            raise ValueError(
                f"批量删除确认不匹配，请输入“{expected_confirmation}”"
            )

        deleted: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for task in unique:
            try:
                deleted.append(self.delete_task(task, task))
            except (KeyError, ValueError, OSError) as exc:
                failures.append({"task": task, "error": str(exc)})
        return {
            "requested": len(unique),
            "deleted": len(deleted),
            "failed": len(failures),
            "files": sum(int(item.get("files") or 0) for item in deleted),
            "bytes": sum(int(item.get("bytes") or 0) for item in deleted),
            "deleted_tasks": [str(item.get("task") or "") for item in deleted],
            "failures": failures,
        }

    def publish_defaults(self, task: str) -> dict[str, Any]:
        task_dir = self.scanner.resolve_task(task)
        return self.publisher.defaults(task_dir) | {"task": task}

    def queue_publish(self, task: str, values: dict[str, Any]) -> dict[str, Any]:
        task_dir = self.scanner.resolve_task(task)
        if self.store.has_active("publish", task):
            raise ValueError("这个视频已经在投稿队列中")
        payload = self.publisher.validate_submission(task_dir, values)
        job = self.store.enqueue(
            "publish",
            task,
            payload,
            resource_class=self.worker.initial_resource("publish", payload),
        )
        self.worker.wake()
        return job

    def open_task_folder(self, task: str, *, subfolder: str = "") -> None:
        path = self.scanner.resolve_task(task)
        if subfolder:
            if subfolder != "dubbing":
                raise ValueError("不支持打开这个子目录")
            path = (path / subfolder).resolve()
            if path.parent != self.scanner.resolve_task(task) or not path.is_dir():
                raise ValueError("中文配音目录尚未生成")
        if os.name == "nt":
            subprocess.Popen(
                ["explorer.exe", str(path)],
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return
        raise RuntimeError("当前系统不支持从面板打开文件夹")


__all__ = ["ControlPanelApp"]
