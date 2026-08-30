from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .models import Stage4Error, SubtitleCue
from .subtitle_validator import validate_subtitles


REPEATED_PUNCTUATION = re.compile(r"([，。！？!?；;：:、])\1+")
ASCII_WORD = re.compile(r"[A-Za-z]+")
CJK_CHARACTER = re.compile(r"[\u3400-\u9fff]")


def _candidate_path(video_dir: Path, value: str) -> Path:
    candidate = Path(value.replace("\\", "/"))
    return (candidate if candidate.is_absolute() else video_dir / candidate).resolve()


def _load_translation_qc(video_dir: Path) -> tuple[dict[str, Any], str]:
    candidates = (
        video_dir / "stage3" / "translation" / "translation_qc.json",
        video_dir / "translation" / "subtitle_qc.json",
    )
    for path in candidates:
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            return value, str(path.resolve())
    return {}, ""


def _reading_metrics(cues: list[SubtitleCue], config: dict[str, Any]) -> dict[str, Any]:
    maximum_chars = int(config.get("auto_select_max_chinese_chars_per_line", 26))
    maximum_cps = float(config.get("auto_select_max_chinese_cps", 12))
    content = "".join(cue.text for cue in cues)
    cjk_count = len(CJK_CHARACTER.findall(content))
    ascii_letters = sum(len(word) for word in ASCII_WORD.findall(content))
    meaningful = max(1, cjk_count + ascii_letters)
    overlong_lines = 0
    fast_cues = 0
    adjacent_duplicates = 0
    repeated_punctuation = 0
    for cue in cues:
        lines = cue.lines or tuple(cue.text.splitlines())
        overlong_lines += sum(
            len(re.sub(r"\s+", "", line)) > maximum_chars for line in lines
        )
        chinese_characters = len(CJK_CHARACTER.findall(cue.text))
        if cue.end > cue.start and chinese_characters / (cue.end - cue.start) > maximum_cps:
            fast_cues += 1
        repeated_punctuation += len(REPEATED_PUNCTUATION.findall(cue.text))
    for left, right in zip(cues, cues[1:]):
        if left.text.strip() and left.text.strip() == right.text.strip():
            adjacent_duplicates += 1
    return {
        "cjk_character_count": cjk_count,
        "ascii_letter_count": ascii_letters,
        "cjk_ratio": round(cjk_count / meaningful, 6),
        "english_leakage_ratio": round(ascii_letters / meaningful, 6),
        "overlong_line_count": overlong_lines,
        "fast_reading_cue_count": fast_cues,
        "adjacent_duplicate_count": adjacent_duplicates,
        "repeated_punctuation_count": repeated_punctuation,
    }


def _source_bonus(path: Path) -> tuple[float, str]:
    name = path.name.casefold()
    if name == "zh.clean.srt":
        return 6.0, "人工审核后的中文字幕"
    if name == "zh.raw.srt":
        return 1.0, "AI 原始翻译字幕"
    if name in {"zh.auto.srt", "zh.youtube.clean.srt"}:
        return -6.0, "自动字幕候选"
    return 0.0, "配置候选"


def _score_candidate(
    path: Path,
    cues: list[SubtitleCue],
    *,
    config: dict[str, Any],
    translation_qc: dict[str, Any],
) -> tuple[float, dict[str, Any], list[str]]:
    metrics = _reading_metrics(cues, config)
    count = max(1, len(cues))
    bonus, source_description = _source_bonus(path)
    score = 100.0 + bonus
    score -= metrics["english_leakage_ratio"] * 30.0
    score -= min(20.0, metrics["overlong_line_count"] / count * 40.0)
    score -= min(20.0, metrics["fast_reading_cue_count"] / count * 35.0)
    score -= min(10.0, metrics["adjacent_duplicate_count"] * 2.5)
    score -= min(10.0, metrics["repeated_punctuation_count"] * 1.5)
    reasons = [source_description]
    if path.name.casefold() == "zh.clean.srt" and translation_qc:
        if translation_qc.get("status") == "QC_PASSED":
            score += 4.0
            reasons.append("翻译结构检查已通过")
        critical_flags = sum(
            int(translation_qc.get("qc_flag_counts", {}).get(name, 0) or 0)
            for name in ("EMPTY_TRANSLATION", "ILLEGAL_CONTROL", "TIMELINE_CHANGED")
        )
        if critical_flags:
            score -= min(30.0, critical_flags * 5.0)
            reasons.append(f"翻译结构关键标记 {critical_flags} 个")
    reasons.extend(
        [
            f"中文字符占比 {metrics['cjk_ratio']:.1%}",
            f"英文字符泄漏占比 {metrics['english_leakage_ratio']:.1%}",
            f"过长行 {metrics['overlong_line_count']} 个",
            f"阅读速度过快片段 {metrics['fast_reading_cue_count']} 个",
        ]
    )
    return round(max(0.0, min(120.0, score)), 3), metrics, reasons


def select_best_chinese_subtitle(
    video_dir: Path,
    english_path: Path,
    candidate_values: Iterable[str],
    *,
    tolerance_ms: int,
    config: dict[str, Any],
) -> tuple[Path, float, str, dict[str, Any]]:
    translation_qc, translation_qc_path = _load_translation_qc(video_dir)
    candidates: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for order, value in enumerate(candidate_values):
        path = _candidate_path(video_dir, str(value))
        if path in seen:
            continue
        seen.add(path)
        record: dict[str, Any] = {
            "path": str(path),
            "exists": path.is_file(),
            "eligible": False,
            "score": None,
            "order": order,
            "rejection_reason": "",
        }
        if not path.is_file():
            record["rejection_reason"] = "FILE_NOT_FOUND"
            candidates.append(record)
            continue
        if (
            path.name.casefold() == "zh.clean.srt"
            and translation_qc
            and str(translation_qc.get("status") or "") != "QC_PASSED"
        ):
            record["rejection_reason"] = "TRANSLATION_QC_REVIEW_REQUIRED"
            record["translation_qc_path"] = translation_qc_path
            candidates.append(record)
            continue
        validation = validate_subtitles(
            english_path,
            path,
            tolerance_ms=tolerance_ms,
        )
        record["validation_status"] = validation.report["validation_status"]
        record["segment_count"] = validation.report["chinese_segment_count"]
        record["hash"] = validation.report["chinese_hash"]
        if not validation.passed:
            record["rejection_reason"] = str(validation.report["validation_status"])
            summary: dict[str, Any] = {}
            for key in (
                "missing_chinese_ids",
                "extra_chinese_ids",
                "timestamp_mismatch_ids",
                "empty_chinese_ids",
            ):
                values = list(validation.report[key])
                summary[f"{key}_count"] = len(values)
                summary[f"{key}_sample"] = values[:20]
            record["validation_summary"] = summary
            candidates.append(record)
            continue
        score, metrics, reasons = _score_candidate(
            path,
            validation.chinese,
            config=config,
            translation_qc=translation_qc,
        )
        record.update(
            {
                "eligible": True,
                "score": score,
                "metrics": metrics,
                "reasons": reasons,
            }
        )
        candidates.append(record)

    eligible = [item for item in candidates if item["eligible"]]
    if not eligible:
        existing = [item for item in candidates if item["exists"]]
        if not existing:
            raise Stage4Error(
                "CHINESE_SUBTITLE_NOT_FOUND",
                "找不到任何配置的中文字幕候选。",
                details={"candidates": candidates},
            )
        raise Stage4Error(
            "NO_VALID_CHINESE_SUBTITLE",
            "找到中文字幕文件，但没有候选能通过与 en.selected.srt 的严格一致性校验。",
            details={"candidates": candidates},
        )
    selected = max(eligible, key=lambda item: (float(item["score"]), -int(item["order"])))
    reason = (
        f"自动评分选择 {Path(selected['path']).name}，得分 {selected['score']:.3f}；"
        + "；".join(selected["reasons"])
    )
    report = {
        "selection_mode": "auto_best",
        "english_path": str(english_path.resolve()),
        "translation_qc_path": translation_qc_path,
        "selected_path": selected["path"],
        "selected_score": selected["score"],
        "selection_reason": reason,
        "candidates": candidates,
    }
    return Path(selected["path"]), float(selected["score"]), reason, report
