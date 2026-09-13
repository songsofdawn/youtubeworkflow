from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..cover_localization import cover_paths


def read_json(path: Path) -> dict[str, Any]:
    try:
        # Keep Windows manifest handles as short-lived as possible. In
        # particular, dubbing/manifest.json is atomically replaced after every
        # completed TTS segment while the dashboard scans it frequently.
        with path.open("r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
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


_AUTOMATION_SKIP_LABELS = {
    "SUBTITLE_LAYOUT_REVIEW_REQUIRED": "字幕无法安全排版",
    "NO_VALID_CHINESE_SUBTITLE": "没有通过校验的中文字幕",
    "CHINESE_SUBTITLE_NOT_FOUND": "没有找到中文字幕",
    "EN_SELECTED_SUBTITLE_NOT_FOUND": "没有找到可用英文字幕",
    "SUBTITLE_VALIDATION_FAILED": "中英字幕结构校验未通过",
    "YOUTUBE_CHINESE_SUBTITLE_NOT_FOUND": "没有 YouTube 中文字幕且自动翻译已关闭",
    "HARDSUB_OUTPUT_NOT_READY": "硬字幕成片未生成",
    "ENGLISH_SUBTITLE_STAGE_FAILED": "英文字幕处理未通过",
    "NO_ENGLISH_SUBTITLE_OR_RECOGNIZED_SPEECH": "没有英文字幕，音轨也未识别到英语语音",
    "CHINESE_TRANSLATION_STAGE_FAILED": "中文字幕翻译未通过",
    "STAGE4_RENDER_STAGE_FAILED": "成片安全检查未通过",
    "DUBBING_TIMING_REVIEW_REQUIRED": "配音时槽超限",
    "TRANSLATION_CONTENT_FILTERED": "翻译服务触发内容安全拦截",
}

_AUTOMATION_FAILURE_LABELS = {
    "DUBBING_RUNTIME_PREFLIGHT_FAILED": "中文配音运行时预检失败",
    "PUBLISH_METADATA_STAGE_FAILED": "投稿信息生成失败，可重试",
}


def _automation_skip_label(reason: str) -> str:
    return _AUTOMATION_SKIP_LABELS.get(reason, reason or "未达到自动投稿条件")


def _automation_failure_label(reason: str) -> str:
    return _AUTOMATION_FAILURE_LABELS.get(
        reason, _AUTOMATION_SKIP_LABELS.get(reason, reason or "自动化处理失败")
    )


def deepseek_translation_ready(task_dir: Path) -> bool:
    stage3 = read_json(task_dir / "stage3_manifest.json")
    status = str(
        stage3.get("translation_status")
        or stage3.get("p1_status")
        or ""
    ).upper()
    reviewed = task_dir / "subtitles" / "zh.reviewed.srt"
    if reviewed.is_file() and reviewed.stat().st_size > 0:
        return True
    if status not in {"QC_PASSED", "TRANSLATION_COMPLETED"}:
        return False
    clean = task_dir / "subtitles" / "zh.clean.srt"
    return clean.is_file() and clean.stat().st_size > 0


def translation_content_filter_failed(task_dir: Path) -> bool:
    """Return whether a failed translation checkpoint records a content filter."""
    checkpoint_dir = task_dir / "stage3" / "translation" / "checkpoints"
    try:
        checkpoints = list(checkpoint_dir.glob("batch_*.json"))
        checkpoints.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return False
    for checkpoint in checkpoints:
        payload = read_json(checkpoint)
        status = str(payload.get("status") or "").casefold()
        if status != "failed":
            return False
        error = str(payload.get("error") or "").casefold()
        return bool(
            "1301" in error
            or "contentfilter" in error
            or "content filter" in error
            or "内容安全" in error
            or "敏感内容" in error
        )
    return False


def youtube_auto_chinese_path(task_dir: Path) -> Path | None:
    download = read_json(task_dir / "download_manifest.json")
    tracks = download.get("subtitle_tracks")
    chinese_track = tracks.get("zh") if isinstance(tracks, dict) else {}
    recorded_auto = (
        isinstance(chinese_track, dict)
        and str(chinese_track.get("source") or "").casefold() == "auto"
    )
    raw_auto_exists = any(
        path.is_file() and path.stat().st_size > 0
        for path in (
            task_dir / "subtitles" / "zh.auto.srt",
            task_dir / "subtitles" / "zh.auto.vtt",
        )
    )
    if not recorded_auto and not raw_auto_exists:
        return None
    candidates = (
        task_dir / "subtitles" / "zh.youtube.clean.srt",
        task_dir / "subtitles" / "zh.auto.srt",
        task_dir / "subtitles" / "zh.auto.vtt",
    )
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def youtube_chinese_path(task_dir: Path) -> Path | None:
    """Return any downloaded YouTube Chinese subtitle track.

    The older ``youtube_auto_chinese_path`` helper intentionally remains strict
    for the existing UI option.  Unattended routing accepts both creator-uploaded
    and automatically generated Chinese tracks before deciding to buy a
    translation pass.
    """
    download = read_json(task_dir / "download_manifest.json")
    tracks = download.get("subtitle_tracks")
    chinese_track = tracks.get("zh") if isinstance(tracks, dict) else {}
    recorded_source = (
        str(chinese_track.get("source") or "").casefold()
        if isinstance(chinese_track, dict)
        else ""
    )
    candidates = (
        task_dir / "subtitles" / "zh.youtube.clean.srt",
        task_dir / "subtitles" / "zh.manual.srt",
        task_dir / "subtitles" / "zh.manual.vtt",
        task_dir / "subtitles" / "zh.auto.srt",
        task_dir / "subtitles" / "zh.auto.vtt",
    )
    if recorded_source not in {"manual", "auto"} and not any(
        path.is_file() and path.stat().st_size > 0 for path in candidates[1:]
    ):
        return None
    for path in candidates:
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def no_english_subtitle_or_recognized_speech(task_dir: Path) -> bool:
    """Return whether no usable English source exists and Whisper found no speech.

    A few YouTube videos expose a one-cue title track instead of subtitles.
    Stage 3 correctly rejects that track, but the old check only looked at the
    source route and therefore missed the safe original-media fallback.
    """
    assessment = read_json(task_dir / "stage3" / "01_source_assessment.json")
    asr_info = read_json(task_dir / "stage3" / "whisper" / "asr_info.json")
    if not asr_info:
        return False
    try:
        no_recognized_speech = (
            int(asr_info.get("segment_count") or 0) == 0
            and int(asr_info.get("word_count") or 0) == 0
        )
    except (TypeError, ValueError):
        return False
    if not no_recognized_speech:
        return False

    selected_path = task_dir / "subtitles" / "en.selected.srt"
    try:
        if selected_path.is_file() and selected_path.stat().st_size > 0:
            return False
    except OSError:
        return False

    no_youtube_english = str(
        assessment.get("route") or assessment.get("status") or ""
    ) == "NO_YOUTUBE_ENGLISH_SOURCE"
    selection_report = read_json(
        task_dir / "stage3" / "selection" / "selection_report.json"
    )
    selection_failed = selection_report.get("selection_failed") is True
    return no_youtube_english or selection_failed


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
        stage4_path = task_dir / "stage4" / "stage4_manifest.json"
        automation_path = task_dir / "stage5" / "automation_manifest.json"
        stage4 = read_json(stage4_path)
        dubbing_path = task_dir / "dubbing" / "manifest.json"
        dubbing = read_json(dubbing_path)
        stage5 = read_json(task_dir / "stage5" / "publish_manifest.json")
        automation = read_json(automation_path)
        info = read_json(task_dir / "metadata" / "info.json")
        cover_files = cover_paths(task_dir, self.project_root)
        cover_manifest_path = cover_files["manifest"]
        cover_manifest = read_json(cover_manifest_path)
        original_cover_path = cover_files["original"]
        localized_cover_path = cover_files["localized"]

        download_status = str(download.get("overall_status") or "unknown")
        selected_path = task_dir / "subtitles" / "en.selected.srt"
        auto_chinese = youtube_auto_chinese_path(task_dir)
        youtube_chinese = youtube_chinese_path(task_dir)
        stage4_status = str(stage4.get("status") or "")
        stage4_qc = str(stage4.get("qc_status") or "")
        translation_status = str(stage3.get("translation_status") or "")
        translation_qc = (
            stage3.get("translation_qc")
            if isinstance(stage3.get("translation_qc"), dict)
            else {}
        )
        translation_review = translation_status.upper() == "REVIEW_REQUIRED"
        dubbing_status = str(dubbing.get("status") or "").upper()
        dubbed_audio_path = task_dir / "dubbing" / "dubbed_audio.wav"
        dubbing_audio_ready = (
            dubbed_audio_path.is_file() and dubbed_audio_path.stat().st_size > 44
        )
        dubbing_complete = (
            dubbing_status in {"COMPLETED", "COMPLETED_WITH_REVIEW"}
            and dubbing_audio_ready
        )
        dubbing_review = dubbing_status == "COMPLETED_WITH_REVIEW"
        stage4_review = (
            stage4.get("review") if isinstance(stage4.get("review"), dict) else {}
        )
        layout_warning_count = sum(
            str(item).startswith(
                (
                    "BILINGUAL_LINE_TOO_WIDE:",
                    "BILINGUAL_TOO_MANY_LINES:",
                    "BILINGUAL_FRAGMENT_DURATION_TOO_SHORT:",
                )
            )
            for item in stage4.get("warnings") or []
        )
        review_summary = ""
        if stage4_status == "REVIEW_REQUIRED" or stage4_qc == "REVIEW_REQUIRED":
            review_summary = str(stage4_review.get("message") or "")
            if not review_summary and layout_warning_count:
                review_summary = f"字幕排版异常 {layout_warning_count} 条，请勿投稿"
            if not review_summary:
                review_summary = "成片需要复核，请查看成片质检报告"
        elif translation_review:
            overflow_count = len(
                translation_qc.get("segment_payload_overflow_ids") or []
            )
            review_summary = (
                f"翻译结构异常 {overflow_count} 条，暂不能成片"
                if overflow_count
                else "中文字幕需要复核，暂不能成片"
            )
        elif dubbing_review:
            review_summary = "中文配音有超出字幕时槽的片段，请试听并复核"
        publish_status = str(stage5.get("status") or "")
        automation_status = str(automation.get("status") or "")
        automation_reason = str(automation.get("reason") or "")
        automation_display_reason = automation_reason
        if (
            automation_reason == "ENGLISH_SUBTITLE_STAGE_FAILED"
            and no_english_subtitle_or_recognized_speech(task_dir)
        ):
            automation_display_reason = "NO_ENGLISH_SUBTITLE_OR_RECOGNIZED_SPEECH"
        elif (
            automation_reason == "CHINESE_TRANSLATION_STAGE_FAILED"
            and translation_content_filter_failed(task_dir)
        ):
            automation_display_reason = "TRANSLATION_CONTENT_FILTERED"
        published = publish_status == "PUBLISHED"
        unattended_fallback_active = automation_status in {
            "FALLBACK_PENDING",
            "ORIGINAL_MEDIA",
        }
        original_media_fallback = automation_status == "ORIGINAL_MEDIA"
        if unattended_fallback_active:
            # The unattended fallback path is already the decision; stale
            # subtitle/layout review artifacts must not ask the user to review
            # a video that is continuing automatically.
            review_summary = ""
            translation_review = False
            dubbing_review = False
        if published:
            # Publishing is terminal in the dashboard. A later experimental
            # rerender may leave a REVIEW_REQUIRED Stage 4 manifest, but that
            # must not make an already published card request another review.
            review_summary = ""

        automation_failure_active = (
            automation_status == "FAILED" and not published
        )
        download_complete = download_status in {"success", "skipped"}
        english_complete = selected_path.is_file() and selected_path.stat().st_size > 0
        translation_complete = deepseek_translation_ready(task_dir)
        render_complete = (
            stage4_status == "STAGE4_COMPLETED"
            or published
            or original_media_fallback
        )
        render_review = not published and not unattended_fallback_active and (
            stage4_status == "REVIEW_REQUIRED" or stage4_qc == "REVIEW_REQUIRED"
        )
        successful_render_supersedes_skip = (
            render_complete
            and stage4_path.is_file()
            and automation_path.is_file()
            and stage4_path.stat().st_mtime > automation_path.stat().st_mtime
        )
        automation_skip_active = (
            automation_status == "SKIPPED"
            and publish_status not in {"RUNNING", "PUBLISHED"}
            and not successful_render_supersedes_skip
        )
        automation_skip_summary = (
            f"已自动跳过：{_automation_skip_label(automation_display_reason)}"
            if automation_skip_active
            else ""
        )
        if automation_skip_active:
            review_summary = automation_skip_summary

        cover_status = str(cover_manifest.get("status") or "").upper()
        cover_localized_available = (
            cover_status == "COMPLETED"
            and localized_cover_path.is_file() and localized_cover_path.stat().st_size > 0
        )
        cover_original_available = (
            original_cover_path.is_file() and original_cover_path.stat().st_size > 0
        )
        if cover_status in {"RUNNING", "ANALYZED"}:
            cover_stage = {"state": "active", "detail": "正在生成中文封面"}
        elif cover_localized_available and cover_status == "COMPLETED":
            cover_stage = {"state": "complete", "detail": "中文封面已完成"}
        elif cover_manifest:
            cover_stage = {
                "state": "failed",
                "detail": "中文封面失败，已保留原始封面；可手动重试",
            }
        else:
            cover_stage = {"state": "skipped", "detail": "未生成中文封面"}

        stages = {
            "download": self._stage_state(
                download_complete,
                download_status in {"failed", "partial_success"},
                "下载完成" if download_complete else "下载失败" if download_status in {"failed", "partial_success"} else "等待下载",
            ),
            "english": self._stage_state(
                english_complete,
                self._stage3_failed(stage3, "selection"),
                "英文字幕已就绪" if english_complete else "等待处理",
            ),
            "translation": self._stage_state(
                translation_complete,
                self._stage3_failed(stage3, "translation"),
                "中文字幕已就绪" if translation_complete else "未运行 AI 翻译",
            ) if not translation_review else {
                "state": "review",
                "detail": review_summary,
            },
            "dubbing": (
                {"state": "active", "detail": "正在生成中文 AI 配音"}
                if dubbing_status == "RUNNING"
                else {"state": "review", "detail": review_summary}
                if dubbing_review
                else self._stage_state(
                    dubbing_complete,
                    dubbing_status == "FAILED",
                    "中文配音已完成"
                    if dubbing_complete
                    else "中文配音失败"
                    if dubbing_status == "FAILED"
                    else "未启用中文配音",
                )
                if dubbing_status
                else {"state": "skipped", "detail": "未启用中文配音"}
            ),
            "render": (
                {"state": "complete", "detail": "已改用原视频投稿"}
                if original_media_fallback
                else
                {"state": "review", "detail": review_summary or stage4_status}
                if render_review
                else self._stage_state(
                    render_complete,
                    stage4_status == "FAILED",
                    "双语成片已完成" if render_complete else "成片失败" if stage4_status == "FAILED" else "等待处理",
                )
            ),
            "cover": cover_stage,
            "publish": (
                {"state": "active", "detail": "正在投稿"}
                if publish_status == "RUNNING"
                else {"state": "complete", "detail": "投稿完成"}
                if publish_status == "PUBLISHED"
                else {
                    "state": "skipped",
                    "detail": automation_skip_summary,
                }
                if automation_skip_active
                else self._stage_state(
                    False,
                    publish_status == "FAILED",
                    "投稿失败" if publish_status == "FAILED" else "等待投稿",
                )
            ),
        }
        if automation_failure_active:
            failure_summary = (
                f"自动化失败：{_automation_failure_label(automation_reason)}"
            )
            details = automation.get("details") or {}
            failed_stage = {
                "生成并选择最佳英文字幕": "english",
                "翻译并检查中文字幕": "translation",
                "生成中文 AI 配音": "dubbing",
                "生成并质检中文配音成片": "render",
                "生成并质检双语成片": "render",
            }.get(str(details.get("stage_label") or ""), "publish")
            if automation_reason == "DUBBING_RUNTIME_PREFLIGHT_FAILED":
                failed_stage = "dubbing"
            stages[failed_stage] = {
                "state": "failed",
                "detail": failure_summary,
            }
            review_summary = failure_summary
        if automation_skip_active:
            for stage_name, stage in stages.items():
                if stage["state"] != "complete":
                    stages[stage_name] = {
                        "state": "skipped",
                        "detail": automation_skip_summary,
                    }
        progress_stage_names = ["download", "english", "translation", "render", "publish"]
        if dubbing_status:
            progress_stage_names.insert(3, "dubbing")
        completed_count = sum(
            stages[name]["state"] in {"complete", "review", "skipped"}
            for name in progress_stage_names
        )
        relative = task_dir.relative_to(self.downloads_root).as_posix()
        newest_mtime = max(
            path.stat().st_mtime
            for path in (
                manifest_path,
                task_dir / "stage3_manifest.json",
                dubbing_path,
                task_dir / "stage4" / "stage4_manifest.json",
                task_dir / "stage5" / "publish_manifest.json",
                task_dir / "stage5" / "automation_manifest.json",
                cover_manifest_path,
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
            "progress": (
                100
                if published or automation_skip_active
                else round(100 * completed_count / len(progress_stage_names))
            ),
            "stages": stages,
            "stage3_status": str(stage3.get("translation_status") or ""),
            "chinese_auto_available": auto_chinese is not None,
            "chinese_auto_name": auto_chinese.name if auto_chinese else "",
            "chinese_youtube_available": youtube_chinese is not None,
            "chinese_youtube_name": youtube_chinese.name if youtube_chinese else "",
            "stage4_status": stage4_status,
            "dubbing_status": dubbing_status,
            "cover_status": cover_status,
            "cover_warnings": [str(item) for item in cover_manifest.get("warnings", [])][:8],
            "cover_mode": str(cover_manifest.get("mode") or "local"),
            "cover_original_available": cover_original_available,
            "cover_localized_available": cover_localized_available,
            "cover_copy": str(cover_manifest.get("selected_copy") or ""),
            "cover_candidates": [
                str(item)
                for item in cover_manifest.get("candidates") or []
                if str(item).strip()
            ][:5],
            "dubbing_available": dubbing_path.is_file(),
            "dubbing_audio_ready": dubbing_audio_ready,
            "dubbing_needs_review": dubbing_review,
            "review_summary": review_summary,
            "review": stage4_review,
            "publish_status": publish_status,
            "automation_status": automation_status,
            "automation_reason": automation_reason,
            "automation_skipped": automation_skip_active,
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
                    + [
                        item.get("message") if isinstance(item, dict) else item
                        for item in (dubbing.get("errors") or [])
                    ]
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
        if stages["publish"]["state"] == "skipped":
            return "无人值守已跳过此视频"
        if stages["dubbing"]["state"] == "failed":
            return "中文配音失败"
        if stages["render"]["state"] == "complete":
            if stages["dubbing"]["state"] == "review":
                return "中文配音需要复核"
            return "成片完成，等待投稿"
        if stages["render"]["state"] == "review":
            return "需要复核"
        if stages["translation"]["state"] == "review":
            return "中文字幕需要复核"
        if stages["dubbing"]["state"] == "review":
            return "中文配音需要复核"
        if stages["dubbing"]["state"] == "active":
            return "正在生成中文配音"
        if "failed" in states:
            return "处理失败"
        if stages["translation"]["state"] == "complete":
            return "双语字幕完成"
        if stages["english"]["state"] == "complete":
            return "英文字幕完成"
        if stages["download"]["state"] == "complete":
            return "等待字幕处理"
        return "等待下载"


__all__ = [
    "WorkflowScanner",
    "deepseek_translation_ready",
    "no_english_subtitle_or_recognized_speech",
    "read_json",
    "translation_content_filter_failed",
    "youtube_auto_chinese_path",
    "youtube_chinese_path",
]
