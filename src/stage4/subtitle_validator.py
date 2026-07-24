from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

from .models import Stage4Error, SubtitleCue, SubtitleValidation
from .stage4_manifest import sha256_file


TIMELINE = re.compile(
    r"^(?P<start>\d{1,3}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>\d{1,3}:\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?$"
)
BLOCK_SEPARATOR = re.compile(r"\r?\n[ \t]*\r?\n+")


def parse_timestamp(value: str) -> float:
    normalized = value.replace(".", ",")
    hours_text, minutes_text, rest = normalized.split(":")
    seconds_text, milliseconds_text = rest.split(",")
    hours = int(hours_text)
    minutes = int(minutes_text)
    seconds = int(seconds_text)
    milliseconds = int(milliseconds_text)
    if minutes > 59 or seconds > 59 or len(milliseconds_text) != 3:
        raise ValueError(f"非法字幕时间戳：{value}")
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000.0


def _has_illegal_control(text: str) -> bool:
    return any(unicodedata.category(character) == "Cc" and character not in "\n\t" for character in text)


def parse_srt_strict(path: Path | str) -> tuple[list[SubtitleCue], dict[str, Any]]:
    source = Path(path)
    text = source.read_text(encoding="utf-8-sig", errors="strict")
    blocks = [block for block in BLOCK_SEPARATOR.split(text.strip()) if block.strip()]
    cues: list[SubtitleCue] = []
    malformed_blocks: list[int] = []
    duplicate_ids: list[str] = []
    illegal_control_ids: list[str] = []
    seen: set[str] = set()
    for block_number, block in enumerate(blocks, 1):
        lines = block.splitlines()
        if len(lines) < 2:
            malformed_blocks.append(block_number)
            continue
        identifier = lines[0].strip()
        timeline = TIMELINE.match(lines[1].strip())
        if not identifier or timeline is None:
            malformed_blocks.append(block_number)
            continue
        try:
            start = parse_timestamp(timeline["start"])
            end = parse_timestamp(timeline["end"])
        except (TypeError, ValueError):
            malformed_blocks.append(block_number)
            continue
        content_lines = tuple(line.strip() for line in lines[2:] if line.strip())
        content = "\n".join(content_lines)
        if identifier in seen:
            duplicate_ids.append(identifier)
        seen.add(identifier)
        if _has_illegal_control(content):
            illegal_control_ids.append(identifier)
        cues.append(SubtitleCue(identifier, start, end, content, content_lines))
    diagnostics = {
        "malformed_blocks": malformed_blocks,
        "duplicate_ids": sorted(set(duplicate_ids)),
        "illegal_control_ids": sorted(set(illegal_control_ids)),
    }
    return cues, diagnostics


def _timeline_errors(cues: list[SubtitleCue], duration: float | None) -> list[str]:
    invalid: list[str] = []
    previous_start = -1.0
    previous_end = -1.0
    for cue in cues:
        if (
            cue.start < 0
            or cue.end <= cue.start
            or cue.start < previous_start
            or cue.end < previous_end
            or (duration is not None and cue.end > duration + 0.001)
        ):
            invalid.append(cue.identifier)
        previous_start = cue.start
        previous_end = cue.end
    return sorted(set(invalid))


def validate_subtitles(
    english_path: Path | str,
    chinese_path: Path | str,
    *,
    tolerance_ms: int = 20,
    video_duration: float | None = None,
) -> SubtitleValidation:
    english_source = Path(english_path)
    chinese_source = Path(chinese_path)
    try:
        english, english_diagnostics = parse_srt_strict(english_source)
    except (OSError, UnicodeError) as exc:
        raise Stage4Error("ENGLISH_SUBTITLE_PARSE_FAILED", f"英文字幕无法读取：{exc}") from exc
    try:
        chinese, chinese_diagnostics = parse_srt_strict(chinese_source)
    except (OSError, UnicodeError) as exc:
        raise Stage4Error("CHINESE_SUBTITLE_PARSE_FAILED", f"中文字幕无法读取：{exc}") from exc

    english_by_id = {cue.identifier: cue for cue in english}
    chinese_by_id = {cue.identifier: cue for cue in chinese}
    english_ids = set(english_by_id)
    chinese_ids = set(chinese_by_id)
    missing_chinese = sorted(english_ids - chinese_ids)
    extra_chinese = sorted(chinese_ids - english_ids)
    mismatch_ids: list[str] = []
    tolerance = max(0, tolerance_ms) / 1000.0
    for identifier in sorted(english_ids & chinese_ids):
        left = english_by_id[identifier]
        right = chinese_by_id[identifier]
        if abs(left.start - right.start) > tolerance or abs(left.end - right.end) > tolerance:
            mismatch_ids.append(identifier)

    empty_english = sorted(cue.identifier for cue in english if not cue.text.strip())
    empty_chinese = sorted(cue.identifier for cue in chinese if not cue.text.strip())
    invalid_english = _timeline_errors(english, video_duration)
    invalid_chinese = _timeline_errors(chinese, video_duration)
    malformed = bool(
        english_diagnostics["malformed_blocks"]
        or chinese_diagnostics["malformed_blocks"]
        or english_diagnostics["duplicate_ids"]
        or chinese_diagnostics["duplicate_ids"]
    )
    timeline_failed = bool(mismatch_ids)
    ids_failed = bool(missing_chinese or extra_chinese or len(english) != len(chinese))
    content_failed = bool(
        empty_english
        or empty_chinese
        or invalid_english
        or invalid_chinese
        or english_diagnostics["illegal_control_ids"]
        or chinese_diagnostics["illegal_control_ids"]
        or malformed
        or not english
        or not chinese
    )
    if timeline_failed:
        status = "SUBTITLE_TIMELINE_MISMATCH"
    elif ids_failed:
        status = "SUBTITLE_ID_MISMATCH"
    elif content_failed:
        status = "SUBTITLE_VALIDATION_FAILED"
    else:
        status = "PASSED"
    report = {
        "english_path": str(english_source.resolve()),
        "chinese_path": str(chinese_source.resolve()),
        "english_hash": sha256_file(english_source),
        "chinese_hash": sha256_file(chinese_source),
        "english_segment_count": len(english),
        "chinese_segment_count": len(chinese),
        "missing_chinese_ids": missing_chinese,
        "extra_chinese_ids": extra_chinese,
        "timestamp_mismatch_ids": mismatch_ids,
        "empty_english_ids": empty_english,
        "empty_chinese_ids": empty_chinese,
        "invalid_timestamp_ids": sorted(set(invalid_english + invalid_chinese)),
        "english_duplicate_ids": english_diagnostics["duplicate_ids"],
        "chinese_duplicate_ids": chinese_diagnostics["duplicate_ids"],
        "english_malformed_blocks": english_diagnostics["malformed_blocks"],
        "chinese_malformed_blocks": chinese_diagnostics["malformed_blocks"],
        "illegal_control_ids": sorted(
            set(
                english_diagnostics["illegal_control_ids"]
                + chinese_diagnostics["illegal_control_ids"]
            )
        ),
        "subtitle_time_tolerance_ms": tolerance_ms,
        "video_duration": video_duration,
        "id_sets_match": not ids_failed,
        "timeline_matches": not timeline_failed,
        "validation_status": status,
    }
    return SubtitleValidation(english=english, chinese=chinese, report=report)
