from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Iterable

from src.clean_subtitles import (
    Cue,
    align_translation_to_english,
    clean_cues,
    format_timestamp,
    format_two_lines,
    parse_srt,
    parse_webvtt,
    prepare_fragments,
)

from .models import Stage4Error
from .stage4_manifest import atomic_write_text
from .subtitle_validator import parse_srt_strict, validate_subtitles


CJK_CHARACTER = re.compile(r"[\u3400-\u9fff]")
ASCII_LETTER = re.compile(r"[A-Za-z]")


def _task_path(video_dir: Path, value: str) -> Path:
    candidate = Path(value.replace("\\", "/"))
    return (candidate if candidate.is_absolute() else video_dir / candidate).resolve()


def _read_cues(path: Path) -> list[Cue]:
    return parse_webvtt(path) if path.suffix.casefold() == ".vtt" else parse_srt(path)


def _srt_text(cues: Iterable[Cue]) -> str:
    blocks = [
        (
            f"{index}\n"
            f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n"
            f"{format_two_lines(cue.text)}"
        )
        for index, cue in enumerate(cues, 1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _pair_by_aligned_timeline(
    english: list[Cue],
    chinese: list[Cue],
) -> tuple[list[Cue], list[Cue]]:
    english_by_timeline: dict[tuple[float, float], list[Cue]] = {}
    for cue in english:
        english_by_timeline.setdefault((cue.start, cue.end), []).append(cue)
    paired_english: list[Cue] = []
    paired_chinese: list[Cue] = []
    for chinese_cue in chinese:
        matches = english_by_timeline.get((chinese_cue.start, chinese_cue.end), [])
        if not matches:
            continue
        english_cue = matches.pop(0)
        paired_english.append(
            Cue(english_cue.start, english_cue.end, english_cue.text)
        )
        paired_chinese.append(
            Cue(english_cue.start, english_cue.end, chinese_cue.text)
        )
    return paired_english, paired_chinese


def _language_metrics(cues: list[Cue]) -> tuple[int, int, float]:
    content = "".join(cue.text for cue in cues)
    cjk_count = len(CJK_CHARACTER.findall(content))
    ascii_count = len(ASCII_LETTER.findall(content))
    meaningful = max(1, cjk_count + ascii_count)
    return cjk_count, ascii_count, cjk_count / meaningful


def recover_aligned_bilingual_subtitles(
    video_dir: Path,
    config: dict[str, Any],
) -> tuple[Path, Path, float, str, dict[str, Any]]:
    english_values = config.get(
        "english_recovery_candidates",
        [
            "subtitles/en.manual.vtt",
            "subtitles/en.auto.vtt",
            "subtitles/en.manual.srt",
            "subtitles/en.auto.srt",
        ],
    )
    chinese_values = config.get(
        "chinese_recovery_candidates",
        [
            "subtitles/zh.manual.vtt",
            "subtitles/zh.auto.vtt",
            "subtitles/zh.manual.srt",
            "subtitles/zh.auto.srt",
        ],
    )
    minimum_pair_ratio = float(config.get("auto_recover_min_pair_ratio", 0.85))
    minimum_cjk_ratio = float(config.get("auto_recover_min_cjk_ratio", 0.5))
    candidates: list[dict[str, Any]] = []

    for english_order, english_value in enumerate(english_values):
        english_path = _task_path(video_dir, str(english_value))
        if not english_path.is_file():
            continue
        try:
            english_clean, _ = clean_cues(_read_cues(english_path))
        except (OSError, UnicodeError, ValueError) as exc:
            candidates.append(
                {
                    "english_source": str(english_path),
                    "eligible": False,
                    "rejection_reason": f"ENGLISH_PARSE_FAILED:{exc}",
                }
            )
            continue
        if not english_clean:
            continue

        for chinese_order, chinese_value in enumerate(chinese_values):
            chinese_path = _task_path(video_dir, str(chinese_value))
            record: dict[str, Any] = {
                "english_source": str(english_path),
                "chinese_source": str(chinese_path),
                "eligible": False,
                "score": None,
                "rejection_reason": "",
            }
            if not chinese_path.is_file():
                record["rejection_reason"] = "CHINESE_SOURCE_NOT_FOUND"
                candidates.append(record)
                continue
            try:
                chinese_fragments, _ = prepare_fragments(_read_cues(chinese_path))
                chinese_aligned = align_translation_to_english(
                    english_clean,
                    chinese_fragments,
                )
            except (OSError, UnicodeError, ValueError) as exc:
                record["rejection_reason"] = f"CHINESE_PARSE_FAILED:{exc}"
                candidates.append(record)
                continue

            paired_english, paired_chinese = _pair_by_aligned_timeline(
                english_clean,
                chinese_aligned,
            )
            pair_ratio = len(paired_english) / max(1, len(english_clean))
            cjk_count, ascii_count, cjk_ratio = _language_metrics(paired_chinese)
            record.update(
                {
                    "english_clean_segment_count": len(english_clean),
                    "paired_segment_count": len(paired_english),
                    "pair_ratio": round(pair_ratio, 6),
                    "cjk_character_count": cjk_count,
                    "ascii_letter_count": ascii_count,
                    "cjk_ratio": round(cjk_ratio, 6),
                }
            )
            if not paired_english:
                record["rejection_reason"] = "NO_ALIGNED_SEGMENTS"
            elif pair_ratio < minimum_pair_ratio:
                record["rejection_reason"] = "PAIR_COVERAGE_BELOW_MINIMUM"
            elif cjk_ratio < minimum_cjk_ratio:
                record["rejection_reason"] = "CHINESE_CONTENT_RATIO_BELOW_MINIMUM"
            else:
                source_bonus = max(0.0, 4.0 - english_order - chinese_order)
                segment_bonus = min(5.0, math.log10(len(paired_english) + 1) * 2.5)
                score = 70.0 + pair_ratio * 15.0 + cjk_ratio * 10.0
                score += source_bonus + segment_bonus
                record.update(
                    {
                        "eligible": True,
                        "score": round(min(105.0, score), 3),
                        "_english_cues": paired_english,
                        "_chinese_cues": paired_chinese,
                    }
                )
            candidates.append(record)

    eligible = [record for record in candidates if record.get("eligible")]
    if not eligible:
        raise Stage4Error(
            "BILINGUAL_RECOVERY_NOT_AVAILABLE",
            "现有独立中英文字幕无法形成覆盖率合格的自动对齐字幕。",
            details={
                "minimum_pair_ratio": minimum_pair_ratio,
                "minimum_cjk_ratio": minimum_cjk_ratio,
                "candidates": candidates,
            },
        )

    selected = max(eligible, key=lambda record: float(record["score"]))
    english_cues = list(selected.pop("_english_cues"))
    chinese_cues = list(selected.pop("_chinese_cues"))
    output_dir = video_dir / "stage4" / "subtitles"
    english_output = output_dir / "en.recovered.srt"
    chinese_output = output_dir / "zh.recovered.srt"
    atomic_write_text(english_output, _srt_text(english_cues))
    atomic_write_text(chinese_output, _srt_text(chinese_cues))
    validation = validate_subtitles(english_output, chinese_output)
    if not validation.passed:
        raise Stage4Error(
            "BILINGUAL_RECOVERY_VALIDATION_FAILED",
            "自动恢复的中英字幕未通过最终一致性检查。",
            details=validation.report,
        )

    reason = (
        "阶段三精确配对不可用；已从独立中英文字幕自动重建并按时间重对齐，"
        f"配对覆盖率 {float(selected['pair_ratio']):.1%}，"
        f"中文字符占比 {float(selected['cjk_ratio']):.1%}"
    )
    public_candidates = [
        {key: value for key, value in record.items() if not key.startswith("_")}
        for record in candidates
    ]
    report = {
        "selection_mode": "auto_recovered_aligned_bilingual",
        "english_path": str(english_output.resolve()),
        "selected_path": str(chinese_output.resolve()),
        "selected_score": selected["score"],
        "selection_reason": reason,
        "source_english_path": selected["english_source"],
        "source_chinese_path": selected["chinese_source"],
        "pair_ratio": selected["pair_ratio"],
        "cjk_ratio": selected["cjk_ratio"],
        "validation": validation.report,
        "candidates": public_candidates,
    }
    return (
        english_output.resolve(),
        chinese_output.resolve(),
        float(selected["score"]),
        reason,
        report,
    )


def clip_recovered_pair_to_video_duration(
    english_path: Path,
    chinese_path: Path,
    video_duration: float,
) -> dict[str, Any]:
    if video_duration <= 0:
        return {"applied": False, "reason": "VIDEO_DURATION_UNAVAILABLE"}
    english_cues, _ = parse_srt_strict(english_path)
    chinese_cues, _ = parse_srt_strict(chinese_path)
    if len(english_cues) != len(chinese_cues):
        raise Stage4Error(
            "BILINGUAL_RECOVERY_CLIP_FAILED",
            "自动恢复字幕在按视频时长裁剪前已失去一一对应关系。",
        )

    clipped_english: list[Cue] = []
    clipped_chinese: list[Cue] = []
    clipped_count = dropped_count = 0
    for english, chinese in zip(english_cues, chinese_cues):
        if english.start >= video_duration or chinese.start >= video_duration:
            dropped_count += 1
            continue
        end = min(english.end, chinese.end, video_duration)
        start = max(english.start, chinese.start)
        if end <= start + 0.05:
            dropped_count += 1
            continue
        if end < english.end or end < chinese.end:
            clipped_count += 1
        clipped_english.append(Cue(start, end, english.text))
        clipped_chinese.append(Cue(start, end, chinese.text))
    if not clipped_english:
        raise Stage4Error(
            "BILINGUAL_RECOVERY_CLIP_FAILED",
            "自动恢复字幕全部位于视频有效时长之外。",
        )

    if clipped_count or dropped_count:
        atomic_write_text(english_path, _srt_text(clipped_english))
        atomic_write_text(chinese_path, _srt_text(clipped_chinese))
    return {
        "applied": bool(clipped_count or dropped_count),
        "video_duration": video_duration,
        "input_segment_count": len(english_cues),
        "output_segment_count": len(clipped_english),
        "clipped_segment_count": clipped_count,
        "dropped_segment_count": dropped_count,
    }
