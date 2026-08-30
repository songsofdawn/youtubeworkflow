from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .layout_review import apply_layout_review_override
from .models import ResolvedInputs, Stage4Error
from .subtitle_recovery import recover_aligned_bilingual_subtitles
from .subtitle_selector import select_best_chinese_subtitle


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov"}
EXCLUDED_VIDEO_MARKERS = (
    "preview",
    "final",
    "hardsub",
    "hard_sub",
    "burned",
    "burnt",
    ".tmp",
    ".temp",
)
YTDLP_COMPONENT_PATTERN = re.compile(r"^source\.f\d+\.", re.IGNORECASE)
VIDEO_KEYS = {
    "source_video",
    "source_video_path",
    "video_path",
    "video_file",
    "video_output_path",
    "downloaded_video",
}


def _configured_task_path(video_dir: Path, value: str) -> Path:
    candidate = Path(value.replace("\\", "/"))
    if not candidate.is_absolute():
        candidate = video_dir / candidate
    return candidate.resolve()


def _recovered_subtitle_inputs(
    video_dir: Path,
    input_config: dict[str, Any],
) -> tuple[Path, Path, bool, bool, float | None, str, dict[str, Any]]:
    english, chinese, score, reason, report = recover_aligned_bilingual_subtitles(
        video_dir,
        input_config,
    )
    return english, chinese, False, True, score, reason, report


def _deepseek_translation_ready(video_dir: Path) -> bool:
    reviewed = video_dir / "subtitles" / "zh.reviewed.srt"
    if reviewed.is_file() and reviewed.stat().st_size > 0:
        return True
    manifest_path = video_dir / "stage3_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    status = str(
        manifest.get("translation_status")
        or manifest.get("p1_status")
        or ""
    ).upper()
    return status in {"QC_PASSED", "TRANSLATION_COMPLETED"}


def _youtube_auto_chinese_available(video_dir: Path) -> bool:
    manifest_path = video_dir / "download_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        manifest = {}
    tracks = manifest.get("subtitle_tracks")
    chinese_track = tracks.get("zh") if isinstance(tracks, dict) else {}
    recorded_auto = (
        isinstance(chinese_track, dict)
        and str(chinese_track.get("source") or "").casefold() == "auto"
    )
    raw_auto_exists = any(
        _configured_task_path(video_dir, value).is_file()
        for value in ("subtitles/zh.auto.srt", "subtitles/zh.auto.vtt")
    )
    return recorded_auto or raw_auto_exists


def resolve_subtitle_inputs(
    video_dir: Path,
    config: dict[str, Any],
    *,
    require_reviewed: bool = False,
    chinese_source: str = "auto",
) -> tuple[Path, Path, bool, bool, float | None, str, dict[str, Any]]:
    if chinese_source not in {"auto", "deepseek", "youtube_auto"}:
        raise Stage4Error(
            "UNSUPPORTED_CHINESE_SUBTITLE_SOURCE",
            f"不支持的中文字幕来源：{chinese_source}",
        )
    input_config = config.get("input", {})
    youtube_auto_config = {
        **input_config,
        "chinese_recovery_candidates": [
            "subtitles/zh.youtube.clean.srt",
            "subtitles/zh.auto.vtt",
            "subtitles/zh.auto.srt",
        ],
    }
    english = _configured_task_path(
        video_dir,
        str(input_config.get("english_subtitle", "subtitles/en.selected.srt")),
    )
    if not english.is_file():
        missing_english = Stage4Error(
            "EN_SELECTED_SUBTITLE_NOT_FOUND",
            f"找不到已选定的英文字幕：{english}。请先完成英文字幕处理。",
        )
        if not require_reviewed and chinese_source in {"auto", "youtube_auto"}:
            try:
                recovery_config = (
                    youtube_auto_config
                    if chinese_source == "youtube_auto"
                    else input_config
                )
                return _recovered_subtitle_inputs(video_dir, recovery_config)
            except Stage4Error as recovery_error:
                missing_english.details["automatic_recovery"] = recovery_error.to_dict()
        raise missing_english

    if chinese_source == "deepseek" and not _deepseek_translation_ready(video_dir):
        raise Stage4Error(
            "DEEPSEEK_TRANSLATION_NOT_FOUND",
            "尚未生成 AI API 中文字幕。请先在控制面板选择 AI API 翻译并处理字幕。",
        )

    priorities = input_config.get(
        "chinese_priority",
        ["subtitles/zh.reviewed.srt", "subtitles/zh.clean.srt"],
    )
    reviewed = _configured_task_path(video_dir, "subtitles/zh.reviewed.srt")
    if reviewed.is_file() and chinese_source != "youtube_auto":
        reason = "存在人工审核字幕，按最高优先级选择 zh.reviewed.srt"
        return (
            english,
            reviewed,
            True,
            False,
            None,
            reason,
            {
                "selection_mode": "reviewed",
                "english_path": str(english),
                "selected_path": str(reviewed),
                "selected_score": None,
                "selection_reason": reason,
                "candidates": [{"path": str(reviewed), "eligible": True, "reviewed": True}],
            },
        )
    if require_reviewed and not reviewed.is_file():
        raise Stage4Error(
            "ZH_REVIEWED_SUBTITLE_NOT_FOUND",
            f"严格模式要求人工审核字幕，但文件不存在：{reviewed}",
        )
    if chinese_source == "youtube_auto":
        candidates = [
            "subtitles/zh.youtube.clean.srt",
            "subtitles/zh.auto.srt",
        ]
        if not _youtube_auto_chinese_available(video_dir):
            raise Stage4Error(
                "ZH_AUTO_SUBTITLE_NOT_FOUND",
                "该视频没有自动生成的中文字幕，请改选 AI API 翻译。",
            )
    elif chinese_source == "deepseek":
        candidates = [
            "subtitles/zh.clean.srt",
            "subtitles/zh.raw.srt",
        ]
    else:
        candidates = input_config.get("chinese_auto_candidates", priorities)
    try:
        selected, score, reason, report = select_best_chinese_subtitle(
            video_dir,
            english,
            candidates,
            tolerance_ms=int(input_config.get("subtitle_time_tolerance_ms", 20)),
            config=input_config,
        )
    except Stage4Error as selection_error:
        if selection_error.code not in {
            "CHINESE_SUBTITLE_NOT_FOUND",
            "NO_VALID_CHINESE_SUBTITLE",
        }:
            raise
        if chinese_source != "deepseek":
            try:
                recovery_config = (
                    youtube_auto_config
                    if chinese_source == "youtube_auto"
                    else input_config
                )
                return _recovered_subtitle_inputs(video_dir, recovery_config)
            except Stage4Error as recovery_error:
                selection_error.details["automatic_recovery"] = recovery_error.to_dict()
        if chinese_source == "youtube_auto":
            raise Stage4Error(
                "ZH_AUTO_SUBTITLE_UNUSABLE",
                "找到了自动生成的中文字幕，但无法与英文字幕可靠对齐。请改选 AI API 翻译。",
                details=selection_error.details,
            )
        raise selection_error
    return english, selected, False, True, score, reason, report


def _is_video_candidate(path: Path, video_dir: Path) -> bool:
    if path.suffix.casefold() not in VIDEO_EXTENSIONS or not path.is_file():
        return False
    try:
        relative = path.resolve().relative_to(video_dir.resolve())
    except ValueError:
        return False
    parts = [part.casefold() for part in relative.parts]
    if "stage4" in parts:
        return False
    name = path.name.casefold()
    if YTDLP_COMPONENT_PATTERN.match(path.name):
        return False
    return not any(marker in name for marker in EXCLUDED_VIDEO_MARKERS)


def _is_canonical_source(path: Path, video_dir: Path) -> bool:
    try:
        relative = path.resolve().relative_to(video_dir.resolve())
    except ValueError:
        return False
    return (
        len(relative.parts) == 2
        and relative.parts[0].casefold() == "video"
        and path.stem.casefold() == "source"
    )


def _iter_manifest_strings(value: Any, key: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _iter_manifest_strings(child, str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from _iter_manifest_strings(child, key)
    elif isinstance(value, str):
        yield key, value


def _manifest_video_candidates(manifest_path: Path, video_dir: Path) -> list[Path]:
    if not manifest_path.is_file():
        return []
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    explicit: list[Path] = []
    output_files: list[Path] = []
    for key, raw in _iter_manifest_strings(value):
        suffix = Path(raw.replace("\\", "/")).suffix.casefold()
        if suffix not in VIDEO_EXTENSIONS:
            continue
        candidate = _configured_task_path(video_dir, raw)
        normalized_key = re.sub(r"[^a-z0-9]+", "_", key.casefold()).strip("_")
        if normalized_key in VIDEO_KEYS:
            explicit.append(candidate)
        elif normalized_key in {"output_files", "files", "outputs"}:
            output_files.append(candidate)
    valid_explicit = list(
        dict.fromkeys(path for path in explicit if _is_video_candidate(path, video_dir))
    )
    if valid_explicit:
        return valid_explicit
    valid_outputs = list(
        dict.fromkeys(path for path in output_files if _is_video_candidate(path, video_dir))
    )
    canonical = [
        path for path in valid_outputs if _is_canonical_source(path, video_dir)
    ]
    return canonical or valid_outputs


def resolve_source_video(video_dir: Path) -> tuple[Path, str, tuple[Path, ...]]:
    sources = (
        ("stage2_manifest.json", "stage2_manifest.json"),
        ("download_manifest.json", "download_manifest.json"),
        ("metadata.json", "metadata.json"),
        ("metadata/info.json", "metadata/info.json"),
    )
    all_seen: list[Path] = []
    for relative, label in sources:
        candidates = _manifest_video_candidates(video_dir / relative, video_dir)
        all_seen.extend(candidates)
        unique = list(dict.fromkeys(candidates))
        if len(unique) == 1:
            return unique[0], f"由 {label} 唯一记录定位", tuple(dict.fromkeys(all_seen))
        if len(unique) > 1:
            raise Stage4Error(
                "AMBIGUOUS_SOURCE_VIDEO",
                f"{label} 记录了多个有效主视频，无法安全自动选择。",
                details={"candidates": [str(path) for path in unique], "source": label},
            )

    fallback: list[Path] = []
    for candidate in video_dir.rglob("*"):
        if _is_video_candidate(candidate, video_dir):
            fallback.append(candidate.resolve())
    fallback = list(dict.fromkeys(fallback))
    if len(fallback) == 1:
        return fallback[0], "任务目录扫描得到唯一主视频", tuple(fallback)
    if not fallback:
        raise Stage4Error(
            "SOURCE_VIDEO_NOT_FOUND",
            f"任务目录中找不到受支持的原始视频：{video_dir}",
        )
    raise Stage4Error(
        "AMBIGUOUS_SOURCE_VIDEO",
        "任务目录中存在多个主视频候选，且清单未能唯一定位。",
        details={"candidates": [str(path) for path in fallback], "source": "directory_scan"},
    )


def resolve_inputs(
    video_dir: Path | str,
    config: dict[str, Any],
    *,
    require_reviewed: bool = False,
    chinese_source: str = "auto",
) -> ResolvedInputs:
    root = Path(video_dir).resolve()
    if not root.is_dir():
        raise Stage4Error("VIDEO_DIR_NOT_FOUND", f"视频任务目录不存在：{root}")
    (
        english,
        chinese,
        reviewed,
        auto_selected,
        selection_score,
        selection_reason,
        selection_report,
    ) = resolve_subtitle_inputs(
        root,
        config,
        require_reviewed=require_reviewed,
        chinese_source=chinese_source,
    )
    source, reason, candidates = resolve_source_video(root)
    resolved = ResolvedInputs(
        video_dir=root,
        source_video=source,
        source_video_reason=reason,
        source_video_candidates=candidates,
        english_subtitle=english,
        chinese_subtitle=chinese,
        chinese_subtitle_reviewed=reviewed,
        chinese_subtitle_auto_selected=auto_selected,
        chinese_subtitle_selection_reason=selection_reason,
        chinese_subtitle_selection_score=selection_score,
        chinese_selection_report=selection_report,
    )
    return apply_layout_review_override(root, resolved)
