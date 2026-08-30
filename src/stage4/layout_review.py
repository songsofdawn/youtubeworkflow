from __future__ import annotations

import unicodedata
from dataclasses import replace
from pathlib import Path
from typing import Any

from .bilingual_ass import build_bilingual_ass, resolve_fonts, single_line_text
from .models import ResolvedInputs, Stage4Error, SubtitleCue
from .stage4_manifest import atomic_write_json, atomic_write_text, load_manifest, sha256_file, utc_now
from .subtitle_validator import parse_srt_strict, validate_subtitles


LAYOUT_REVIEW_VERSION = 1
REVIEW_DIRECTORY = Path("stage4/subtitles")
REVIEW_METADATA = REVIEW_DIRECTORY / "layout_review.json"
REVIEW_ENGLISH = REVIEW_DIRECTORY / "en.layout_reviewed.srt"
REVIEW_CHINESE = REVIEW_DIRECTORY / "zh.layout_reviewed.srt"
ISSUE_LABELS = {
    "BILINGUAL_LINE_TOO_WIDE": "单行宽度超出安全区域",
    "BILINGUAL_TOO_MANY_LINES": "字幕行数超过双语单行限制",
    "BILINGUAL_FRAGMENT_DURATION_TOO_SHORT": "分页后每页显示时间过短",
}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _recorded_path(root: Path, value: object, label: str) -> Path:
    candidate = Path(str(value or "").replace("\\", "/"))
    if not candidate.is_absolute():
        candidate = root / candidate
    candidate = candidate.resolve()
    if not _inside(candidate, root):
        raise Stage4Error("LAYOUT_REVIEW_PATH_INVALID", f"{label}超出视频任务目录。")
    if not candidate.is_file():
        raise Stage4Error("LAYOUT_REVIEW_SOURCE_MISSING", f"找不到{label}：{candidate}")
    return candidate


def _format_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, fraction = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{fraction:03d}"


def _write_srt(path: Path, cues: list[SubtitleCue]) -> Path:
    blocks = [
        "\n".join(
            (
                cue.identifier,
                f"{_format_timestamp(cue.start)} --> {_format_timestamp(cue.end)}",
                cue.text,
            )
        )
        for cue in cues
    ]
    return atomic_write_text(path, "\n\n".join(blocks) + "\n")


def _normalize_text(value: object, identifier: str, language: str) -> str:
    text = single_line_text(str(value or "")).strip()
    if not text:
        raise ValueError(f"字幕 {identifier} 的{language}内容不能为空")
    if len(text) > 2000:
        raise ValueError(f"字幕 {identifier} 的{language}内容过长")
    if any(unicodedata.category(character) == "Cc" for character in text):
        raise ValueError(f"字幕 {identifier} 的{language}内容包含非法控制字符")
    return text


def _source_paths(root: Path, manifest: dict[str, Any], saved: dict[str, Any]) -> tuple[Path, Path]:
    saved_english = saved.get("source_english_path")
    saved_chinese = saved.get("source_chinese_path")
    if saved_english and saved_chinese:
        try:
            english = _recorded_path(root, saved_english, "原始英文字幕")
            chinese = _recorded_path(root, saved_chinese, "原始中文字幕")
        except Stage4Error:
            pass
        else:
            if (
                saved.get("source_english_hash") == sha256_file(english)
                and saved.get("source_chinese_hash") == sha256_file(chinese)
            ):
                return english, chinese
    return (
        _recorded_path(root, manifest.get("english_subtitle_path"), "英文字幕"),
        _recorded_path(root, manifest.get("chinese_subtitle_path"), "中文字幕"),
    )


def _saved_review_is_current(
    root: Path,
    saved: dict[str, Any],
    source_english: Path,
    source_chinese: Path,
) -> bool:
    if not saved or saved.get("schema_version") != LAYOUT_REVIEW_VERSION:
        return False
    return bool(
        saved.get("source_english_path") == str(source_english)
        and saved.get("source_chinese_path") == str(source_chinese)
        and saved.get("source_english_hash") == sha256_file(source_english)
        and saved.get("source_chinese_hash") == sha256_file(source_chinese)
        and (root / REVIEW_ENGLISH).is_file()
        and (root / REVIEW_CHINESE).is_file()
    )


def _issue_map(manifest: dict[str, Any], subtitle_qc: dict[str, Any], saved: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    rows = subtitle_qc.get("layout_warnings")
    if not isinstance(rows, list):
        rows = []
    saved_rows = saved.get("remaining_issues")
    if isinstance(saved_rows, list):
        rows = [*rows, *saved_rows]
    result: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        identifier = str(raw.get("id") or "")
        if identifier:
            result.setdefault(identifier, []).append(raw)
    review = manifest.get("review") if isinstance(manifest.get("review"), dict) else {}
    issue_ids = review.get("issue_ids") if isinstance(review.get("issue_ids"), list) else []
    saved_ids = saved.get("reviewed_ids") if isinstance(saved.get("reviewed_ids"), list) else []
    for identifier in (*issue_ids, *saved_ids):
        result.setdefault(str(identifier), [])
    return result


def _review_context(video_dir: Path | str) -> dict[str, Any]:
    root = Path(video_dir).resolve()
    manifest = load_manifest(root / "stage4" / "stage4_manifest.json")
    saved = load_manifest(root / REVIEW_METADATA)
    review = manifest.get("review") if isinstance(manifest.get("review"), dict) else {}
    if review.get("code") != "SUBTITLE_LAYOUT_REVIEW_REQUIRED" and not saved:
        raise ValueError("这个任务当前没有需要处理的字幕排版复核")
    source_english, source_chinese = _source_paths(root, manifest, saved)
    source_validation = validate_subtitles(source_english, source_chinese)
    if not source_validation.passed:
        raise ValueError("原始中英字幕结构或时间轴不一致，不能进入排版复核")
    current_english = source_english
    current_chinese = source_chinese
    if _saved_review_is_current(root, saved, source_english, source_chinese):
        reviewed_english = root / REVIEW_ENGLISH
        reviewed_chinese = root / REVIEW_CHINESE
        if (
            saved.get("review_english_hash") != sha256_file(reviewed_english)
            or saved.get("review_chinese_hash") != sha256_file(reviewed_chinese)
        ):
            raise ValueError("排版复核字幕已在面板外被修改，请重新打开复核窗口保存")
        current_english = reviewed_english
        current_chinese = reviewed_chinese
    current_validation = validate_subtitles(current_english, current_chinese)
    if not current_validation.passed:
        raise ValueError("排版复核字幕结构或时间轴不一致，请重新保存")
    subtitle_qc = load_manifest(root / "stage4" / "qc" / "subtitle_qc.json")
    issues = _issue_map(manifest, subtitle_qc, saved)
    if not issues:
        raise ValueError("成片报告没有记录需要复核的字幕编号")
    return {
        "root": root,
        "manifest": manifest,
        "saved": saved,
        "source_english": source_english,
        "source_chinese": source_chinese,
        "source_validation": source_validation,
        "current_validation": current_validation,
        "issues": issues,
    }


def _public_review(context: dict[str, Any]) -> dict[str, Any]:
    manifest = context["manifest"]
    saved = context["saved"]
    source_english = {cue.identifier: cue for cue in context["source_validation"].english}
    source_chinese = {cue.identifier: cue for cue in context["source_validation"].chinese}
    current_english = {cue.identifier: cue for cue in context["current_validation"].english}
    current_chinese = {cue.identifier: cue for cue in context["current_validation"].chinese}
    hidden_ids = {
        str(identifier)
        for identifier in saved.get("hidden_ids", [])
        if str(identifier)
    }
    rows: list[dict[str, Any]] = []
    for identifier, issue_rows in context["issues"].items():
        english = source_english.get(identifier)
        chinese = source_chinese.get(identifier)
        if english is None or chinese is None:
            continue
        codes = list(
            dict.fromkeys(str(item.get("code") or "") for item in issue_rows if item.get("code"))
        )
        rows.append(
            {
                "id": identifier,
                "start": english.start,
                "end": english.end,
                "duration": round(english.end - english.start, 3),
                "timecode": f"{_format_timestamp(english.start)} → {_format_timestamp(english.end)}",
                "issue_codes": codes,
                "issue_labels": [ISSUE_LABELS.get(code, code) for code in codes] or ["字幕排版需要复核"],
                "english_original": english.text,
                "chinese_original": chinese.text,
                "english_text": current_english.get(identifier, english).text,
                "chinese_text": current_chinese.get(identifier, chinese).text,
                "hidden_from_render": identifier in hidden_ids,
            }
        )
    rows.sort(key=lambda item: (float(item["start"]), str(item["id"])))
    review = manifest.get("review") if isinstance(manifest.get("review"), dict) else {}
    remaining = saved.get("remaining_issues") if isinstance(saved.get("remaining_issues"), list) else []
    return {
        "review_api_version": 2,
        "supports_hide_from_render": True,
        "message": str(
            review.get("message")
            or "请缩短受影响字幕；系统会在重新成片前再次检查。"
        ),
        "rows": rows,
        "issue_count": len(rows),
        "remaining_issue_count": len({str(item.get("id") or "") for item in remaining}),
        "hidden_count": len(hidden_ids),
        "ready_to_render": bool(saved.get("status") == "LAYOUT_REVIEW_PASSED" and not remaining),
        "output_mode": str(manifest.get("output_mode") or "hardsub"),
        "chinese_subtitle_source": str(manifest.get("chinese_subtitle_source") or "deepseek"),
        "source_files_preserved": True,
        "review_status": str(saved.get("status") or "NOT_SAVED"),
    }


def load_layout_review(video_dir: Path | str) -> dict[str, Any]:
    return _public_review(_review_context(video_dir))


def save_layout_review(
    video_dir: Path | str,
    edits: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    context = _review_context(video_dir)
    root: Path = context["root"]
    expected_ids = set(context["issues"])
    submitted: dict[str, dict[str, Any]] = {}
    for row in edits:
        if not isinstance(row, dict):
            raise ValueError("字幕复核内容格式无效")
        identifier = str(row.get("id") or "")
        if not identifier or identifier in submitted:
            raise ValueError("字幕复核编号为空或重复")
        if identifier not in expected_ids:
            raise ValueError(f"字幕 {identifier} 不在本次复核范围内")
        hidden = row.get("hidden_from_render") is True
        submitted[identifier] = {"hidden_from_render": hidden}
        if not hidden:
            submitted[identifier].update(
                {
                    "english": _normalize_text(row.get("english"), identifier, "英文"),
                    "chinese": _normalize_text(row.get("chinese"), identifier, "中文"),
                }
            )
    missing = expected_ids - set(submitted)
    if missing:
        raise ValueError("缺少需要复核的字幕：" + "、".join(sorted(missing)))

    source_validation = context["source_validation"]
    hidden_ids = {
        identifier
        for identifier, row in submitted.items()
        if row["hidden_from_render"]
    }
    reviewed_english = [
        replace(cue, text=submitted[cue.identifier]["english"], lines=(submitted[cue.identifier]["english"],))
        if cue.identifier in submitted and cue.identifier not in hidden_ids
        else cue
        for cue in source_validation.english
        if cue.identifier not in hidden_ids
    ]
    reviewed_chinese = [
        replace(cue, text=submitted[cue.identifier]["chinese"], lines=(submitted[cue.identifier]["chinese"],))
        if cue.identifier in submitted and cue.identifier not in hidden_ids
        else cue
        for cue in source_validation.chinese
        if cue.identifier not in hidden_ids
    ]
    if not reviewed_english:
        raise ValueError("不能隐藏全部字幕；请至少保留一条字幕用于成片")
    english_path = _write_srt(root / REVIEW_ENGLISH, reviewed_english)
    chinese_path = _write_srt(root / REVIEW_CHINESE, reviewed_chinese)
    source_probe = context["manifest"].get("source_video_probe")
    source_probe = source_probe if isinstance(source_probe, dict) else {}
    width = int(source_probe.get("display_width") or source_probe.get("width") or 0)
    height = int(source_probe.get("display_height") or source_probe.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ValueError("成片报告缺少视频尺寸，不能执行排版复核")
    validation = validate_subtitles(
        english_path,
        chinese_path,
        tolerance_ms=int(config.get("input", {}).get("subtitle_time_tolerance_ms", 20)),
        video_duration=float(source_probe.get("duration") or 0) or None,
        video_duration_tolerance_seconds=float(
            config.get("input", {}).get("subtitle_video_end_tolerance_seconds", 2.0)
        ),
    )
    if not validation.passed:
        raise ValueError("成片显示副本的中英字幕结构或时间轴不一致，已拒绝继续成片")
    expected_visible_ids = [
        cue.identifier
        for cue in source_validation.english
        if cue.identifier not in hidden_ids
    ]
    if [cue.identifier for cue in validation.english] != expected_visible_ids:
        raise ValueError("成片显示副本修改了未隐藏字幕的编号，已拒绝继续成片")
    source_by_id = {cue.identifier: cue for cue in source_validation.english}
    tolerance_seconds = int(config.get("input", {}).get("subtitle_time_tolerance_ms", 20)) / 1000
    if any(
        abs(cue.start - source_by_id[cue.identifier].start) > tolerance_seconds
        or abs(cue.end - source_by_id[cue.identifier].end) > tolerance_seconds
        for cue in validation.english
    ):
        raise ValueError("成片显示副本修改了未隐藏字幕的时间轴，已拒绝继续成片")
    style, _ = resolve_fonts(dict(config.get("subtitle_style", {})))
    _, _, remaining_issues = build_bilingual_ass(
        validation.english,
        validation.chinese,
        style,
        width=width,
        height=height,
    )
    status = "LAYOUT_REVIEW_PASSED" if not remaining_issues else "LAYOUT_REVIEW_REQUIRED"
    metadata = {
        "schema_version": LAYOUT_REVIEW_VERSION,
        "status": status,
        "source_english_path": str(context["source_english"]),
        "source_english_hash": sha256_file(context["source_english"]),
        "source_chinese_path": str(context["source_chinese"]),
        "source_chinese_hash": sha256_file(context["source_chinese"]),
        "review_english_path": str(english_path),
        "review_english_hash": sha256_file(english_path),
        "review_chinese_path": str(chinese_path),
        "review_chinese_hash": sha256_file(chinese_path),
        "reviewed_ids": sorted(expected_ids),
        "hidden_ids": sorted(hidden_ids),
        "remaining_issues": remaining_issues,
        "saved_at": utc_now(),
    }
    atomic_write_json(root / REVIEW_METADATA, metadata)
    return _public_review(_review_context(root))


def apply_layout_review_override(video_dir: Path | str, resolved: ResolvedInputs) -> ResolvedInputs:
    root = Path(video_dir).resolve()
    saved = load_manifest(root / REVIEW_METADATA)
    if not _saved_review_is_current(
        root,
        saved,
        resolved.english_subtitle,
        resolved.chinese_subtitle,
    ):
        return resolved
    english = root / REVIEW_ENGLISH
    chinese = root / REVIEW_CHINESE
    if (
        saved.get("review_english_hash") != sha256_file(english)
        or saved.get("review_chinese_hash") != sha256_file(chinese)
    ):
        raise Stage4Error(
            "LAYOUT_REVIEW_TAMPERED",
            "排版复核字幕已在面板外被修改，请重新打开复核窗口保存。",
        )
    validation = validate_subtitles(english, chinese)
    if not validation.passed:
        raise Stage4Error(
            "LAYOUT_REVIEW_INVALID",
            "排版复核字幕没有保持原 ID 和时间轴。",
            details=validation.report,
        )
    hidden_ids = [str(identifier) for identifier in saved.get("hidden_ids", [])]
    reason = "使用控制面板保存的成片排版复核副本；原字幕保持不变"
    if hidden_ids:
        reason += f"；成片中隐藏 {len(hidden_ids)} 条用户确认忽略的字幕"
    return replace(
        resolved,
        english_subtitle=english,
        chinese_subtitle=chinese,
        chinese_subtitle_reviewed=True,
        chinese_subtitle_auto_selected=False,
        chinese_subtitle_selection_reason=reason,
        chinese_subtitle_selection_score=None,
        chinese_selection_report={
            **resolved.chinese_selection_report,
            "selection_mode": "layout_reviewed",
            "english_path": str(english),
            "selected_path": str(chinese),
            "selection_reason": reason,
            "layout_review_path": str(root / REVIEW_METADATA),
            "layout_review_hidden_ids": hidden_ids,
        },
    )


__all__ = [
    "apply_layout_review_override",
    "load_layout_review",
    "save_layout_review",
]
