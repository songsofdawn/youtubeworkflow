from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .models import SubtitleSegment
from .subtitle_writer import TIME_LINE, read_srt
from .youtube_vtt_parser import normalize_for_compare


TAG_PATTERN = re.compile(r"</?(?:c(?:\.[^ >]+)?|v|lang|b|i|u|ruby|rt)[^>]*>|<\d{2}:\d{2}[^>]*>")
CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
INVISIBLE_PATTERN = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")


def _inspect_srt_source(path: Path) -> dict[str, Any]:
    """Inspect details intentionally normalized away by read_srt()."""
    content = path.read_text(encoding="utf-8-sig")
    blocks = [block for block in re.split(r"\r?\n\s*\r?\n", content.strip()) if block.strip()]
    identifiers: list[int] = []
    text_lines: list[list[str]] = []
    invalid_ids = 0
    invalid_timing_lines = 0
    empty_blocks = 0
    for block in blocks:
        lines = block.splitlines()
        if not lines:
            continue
        first = lines[0].strip()
        if first.isdigit():
            identifiers.append(int(first))
            timing_index = 1
        else:
            invalid_ids += 1
            timing_index = 0
        if timing_index >= len(lines) or "-->" not in lines[timing_index]:
            invalid_timing_lines += 1
            text_lines.append([])
            empty_blocks += 1
            continue
        if TIME_LINE.fullmatch(lines[timing_index].strip()) is None:
            invalid_timing_lines += 1
        lines_for_text = [line.strip() for line in lines[timing_index + 1:] if line.strip()]
        text_lines.append(lines_for_text)
        empty_blocks += int(not lines_for_text)
    return {
        "identifiers": identifiers,
        "invalid_ids": invalid_ids,
        "invalid_timing_lines": invalid_timing_lines,
        "text_lines": text_lines,
        "empty_blocks": empty_blocks,
    }


def _deduction(label: str, count: float, points: float) -> dict[str, Any]:
    return {"reason": label, "raw_value": round(float(count), 6), "points": round(float(points), 3)}


def _dimension(raw: dict[str, Any], deductions: list[dict[str, Any]], weight: float) -> dict[str, Any]:
    score = round(max(0.0, 100.0 - sum(float(item["points"]) for item in deductions)), 3)
    return {
        "raw_values": raw,
        "normalized_score": score,
        "weight": float(weight),
        "weighted_score": round(score * float(weight), 3),
        "deductions": deductions,
    }


def _merge_intervals(intervals: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    values = sorted((max(0.0, float(start)), max(0.0, float(end))) for start, end in intervals if end > start)
    merged: list[tuple[float, float]] = []
    for start, end in values:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _duration(intervals: Iterable[tuple[float, float]]) -> float:
    return sum(end - start for start, end in _merge_intervals(intervals))


def _intersection_duration(
    left: Iterable[tuple[float, float]],
    right: Iterable[tuple[float, float]],
) -> float:
    a, b = _merge_intervals(left), _merge_intervals(right)
    total = 0.0
    left_index = right_index = 0
    while left_index < len(a) and right_index < len(b):
        start = max(a[left_index][0], b[right_index][0])
        end = min(a[left_index][1], b[right_index][1])
        if end > start:
            total += end - start
        if a[left_index][1] <= b[right_index][1]:
            left_index += 1
        else:
            right_index += 1
    return total


def _maximum_uncovered_speech(
    speech: list[tuple[float, float]],
    subtitles: list[tuple[float, float]],
) -> float:
    maximum = 0.0
    subtitle_intervals = _merge_intervals(subtitles)
    for speech_start, speech_end in _merge_intervals(speech):
        cursor = speech_start
        for subtitle_start, subtitle_end in subtitle_intervals:
            if subtitle_end <= cursor or subtitle_start >= speech_end:
                continue
            maximum = max(maximum, max(0.0, min(subtitle_start, speech_end) - cursor))
            cursor = max(cursor, min(subtitle_end, speech_end))
        maximum = max(maximum, speech_end - cursor)
    return maximum


def subtitle_agreement(youtube_path: Path | str, whisper_path: Path | str) -> float:
    youtube, whisper = read_srt(youtube_path), read_srt(whisper_path)
    if not youtube or not whisper:
        return 0.0
    weighted, weight = 0.0, 0.0
    for youtube_item in youtube:
        candidates = [
            item for item in whisper
            if min(youtube_item.end, item.end) > max(youtube_item.start, item.start)
        ]
        if not candidates:
            continue
        combined = " ".join(item.text for item in candidates)
        overlap = sum(
            max(0.0, min(youtube_item.end, item.end) - max(youtube_item.start, item.start))
            for item in candidates
        )
        ratio = SequenceMatcher(
            None,
            normalize_for_compare(youtube_item.text),
            normalize_for_compare(combined),
        ).ratio()
        weighted += ratio * max(0.01, overlap)
        weight += max(0.01, overlap)
    if not weight:
        ratio = SequenceMatcher(
            None,
            normalize_for_compare(" ".join(item.text for item in youtube)),
            normalize_for_compare(" ".join(item.text for item in whisper)),
        ).ratio()
        return round(ratio * 100, 3)
    return round(weighted / weight * 100, 3)


def score_subtitle(
    path: Path | str,
    *,
    source: str,
    source_type: str,
    config: dict[str, Any],
    audio_duration: float = 0.0,
    speech_intervals: list[tuple[float, float]] | None = None,
    source_qc: dict[str, Any] | None = None,
    original_hash_unchanged: bool = True,
    agreement_score: float | None = None,
) -> dict[str, Any]:
    subtitle_path = Path(path)
    weights = config["scoring_weights"]
    review_flags: list[str] = []
    hard_fail_reasons: list[str] = []
    inspection: dict[str, Any] = {
        "identifiers": [],
        "invalid_ids": 0,
        "invalid_timing_lines": 0,
        "text_lines": [],
        "empty_blocks": 0,
    }
    try:
        inspection = _inspect_srt_source(subtitle_path)
        segments = read_srt(subtitle_path)
        parse_error = ""
    except Exception as exc:
        segments, parse_error = [], str(exc)
    if parse_error:
        hard_fail_reasons.append("FILE_UNPARSABLE")
    if not subtitle_path.is_file():
        hard_fail_reasons.append("CLEAN_FILE_NOT_GENERATED")
    if not original_hash_unchanged:
        hard_fail_reasons.append("ORIGINAL_SOURCE_HASH_CHANGED")

    texts = [item.text.strip() for item in segments]
    ids = inspection["identifiers"]
    invalid_ids = int(inspection["invalid_ids"])
    empty = max(sum(not text for text in texts), int(inspection["empty_blocks"]))
    tags = sum(bool(TAG_PATTERN.search(text)) for text in texts)
    controls = sum(bool(CONTROL_PATTERN.search(text)) for text in texts)
    invisibles = sum(bool(INVISIBLE_PATTERN.search(text)) for text in texts)
    duplicate_ids = len(ids) - len(set(ids))
    invalid_text = sum(not normalize_for_compare(text) for text in texts)
    structure_deductions = [
        _deduction("EMPTY_SUBTITLE", empty, empty * 15),
        _deduction("TAG_RESIDUAL", tags, tags * 8),
        _deduction("CONTROL_CHARACTER", controls, controls * 10),
        _deduction("INVISIBLE_CHARACTER", invisibles, invisibles * 5),
        _deduction("DUPLICATE_ID", duplicate_ids, duplicate_ids * 15),
        _deduction("INVALID_ID", invalid_ids, invalid_ids * 10),
        _deduction("INVALID_TEXT", invalid_text, invalid_text * 10),
    ]

    invalid_times = (
        sum(item.start >= item.end for item in segments)
        + int(inspection["invalid_timing_lines"])
    )
    reverse = sum(right.start < left.start for left, right in zip(segments, segments[1:]))
    overlaps = sum(right.start < left.end for left, right in zip(segments, segments[1:]))
    under = sum(item.duration < 0.3 for item in segments)
    over = sum(item.duration > float(config.get("hard_max_segment_duration", 8.0)) for item in segments)
    bad_gaps = sum(
        0 < (right.start - left.end) < float(config["minimum_gap_ms"]) / 1000
        for left, right in zip(segments, segments[1:])
    )
    beyond_audio = sum(audio_duration > 0 and item.end > audio_duration + 0.25 for item in segments)
    timeline_deductions = [
        _deduction("INVALID_TIMESTAMP", invalid_times, invalid_times * 25),
        _deduction("REVERSED_TIMELINE", reverse, reverse * 15),
        _deduction("OVERLAP", overlaps, overlaps * 20),
        _deduction("UNDER_0_3_SECONDS", under, min(20, under * 2)),
        _deduction("OVER_8_SECONDS", over, over * 8),
        _deduction("UNREASONABLE_GAP", bad_gaps, min(10, bad_gaps)),
        _deduction("BEYOND_AUDIO_DURATION", beyond_audio, beyond_audio * 10),
    ]

    subtitle_intervals = [(item.start, item.end) for item in segments]
    speech = speech_intervals or []
    if speech:
        total_speech = _duration(speech)
        covered_speech = _intersection_duration(speech, subtitle_intervals)
        coverage_ratio = covered_speech / total_speech if total_speech else 0.0
        max_uncovered = _maximum_uncovered_speech(speech, subtitle_intervals)
        first_speech_covered = any(start <= speech[0][0] + 1.0 and end >= speech[0][0] for start, end in subtitle_intervals)
        last_speech_covered = any(start <= speech[-1][1] and end >= speech[-1][1] - 1.0 for start, end in subtitle_intervals)
    else:
        total_speech = audio_duration
        covered_speech = min(_duration(subtitle_intervals), audio_duration) if audio_duration else _duration(subtitle_intervals)
        coverage_ratio = covered_speech / audio_duration if audio_duration else (1.0 if segments else 0.0)
        max_uncovered = max(0.0, audio_duration - covered_speech) if audio_duration else 0.0
        first_speech_covered = bool(segments and segments[0].start <= 2.0)
        last_speech_covered = bool(segments and (not audio_duration or segments[-1].end >= audio_duration - 5.0))
        review_flags.append("SPEECH_INTERVALS_UNAVAILABLE")
    coverage_deductions = [
        _deduction("UNCOVERED_SPEECH", 1 - coverage_ratio, max(0.0, 1 - coverage_ratio) * 100),
        _deduction("FIRST_SPEECH_NOT_COVERED", int(not first_speech_covered), 5 if not first_speech_covered else 0),
        _deduction("LAST_SPEECH_NOT_COVERED", int(not last_speech_covered), 5 if not last_speech_covered else 0),
        _deduction("LONG_UNCOVERED_SPEECH_GAP", max_uncovered, min(15, max_uncovered / 2)),
    ]

    normalized = [normalize_for_compare(text) for text in texts]
    adjacent = sum(left and left == right for left, right in zip(normalized, normalized[1:]))
    cumulative = sum(
        bool(left and right and (right.startswith(left) or left in right))
        for left, right in zip(normalized, normalized[1:])
    )
    repeated_phrases = int((source_qc or {}).get("continuous_repeated_phrases", 0))
    transitions = int((source_qc or {}).get("under_0_3_seconds", 0)) if source == "youtube" else 0
    high_no_speech = int((source_qc or {}).get("high_no_speech_segments", 0))
    stability_deductions = [
        _deduction("ADJACENT_DUPLICATE", adjacent, adjacent * 15),
        _deduction("CUMULATIVE_TEXT_RESIDUAL", cumulative, cumulative * 8),
        _deduction("REPEATED_PHRASE", repeated_phrases, repeated_phrases * 5),
        _deduction("TRANSITION_CUE_RESIDUAL", transitions, transitions * 2),
        _deduction("HIGH_NO_SPEECH", high_no_speech, high_no_speech * 4),
    ]

    fast = sum(item.duration > 0 and len(item.text) / item.duration > float(config["english_max_cps"]) for item in segments)
    original_text_lines = inspection["text_lines"]
    long_lines = sum(
        len(line) > int(config["english_max_chars_per_line"])
        for lines in original_text_lines
        for line in lines
    )
    too_many_lines = sum(len(lines) > int(config["max_lines"]) for lines in original_text_lines)
    too_long = sum(item.duration > float(config["max_segment_duration"]) for item in segments)
    fragments = sum(item.duration < float(config["min_segment_duration"]) and len(item.text) < 18 for item in segments)
    unnatural = sum(
        bool(text and text[-1] not in ".?!…:;\"'，。！？；：”)】]") and len(text) > 84
        for text in texts
    )
    readability_deductions = [
        _deduction("HIGH_CPS", fast, min(30, fast * 2)),
        _deduction("LONG_LINE", long_lines, min(20, long_lines * 2)),
        _deduction("TOO_MANY_LINES", too_many_lines, too_many_lines * 5),
        _deduction("LONG_DURATION", too_long, too_long * 3),
        _deduction("SHORT_FRAGMENT", fragments, min(20, fragments * 2)),
        _deduction("UNNATURAL_BREAK", unnatural, min(15, unnatural * 2)),
    ]

    if source == "youtube":
        base_confidence = 95.0 if source_type == "manual" else 78.0
        confidence_raw = {
            "source_type": source_type,
            "base_confidence": base_confidence,
            "agreement_score": agreement_score,
            "rolling_residuals": cumulative,
        }
        confidence_deductions = [
            _deduction("SOURCE_BASE_CONFIDENCE", 100 - base_confidence, 100 - base_confidence),
            _deduction("ROLLING_RESIDUAL", cumulative, cumulative * 4),
        ]
    else:
        average_probability = float((source_qc or {}).get("average_word_probability", 0.0) or 0.0)
        average_log_probability = (source_qc or {}).get("average_log_probability")
        average_no_speech_probability = (source_qc or {}).get("average_no_speech_probability")
        missing_rate = float((source_qc or {}).get("word_timestamp_missing_rate", 0.0) or 0.0)
        vad_coverage_ratio = float((source_qc or {}).get("coverage_ratio", 0.0) or 0.0)
        low_words = int((source_qc or {}).get("low_confidence_words", 0))
        word_count = max(1, int((source_qc or {}).get("word_count", 0)))
        base_confidence = average_probability * 100 if average_probability else 65.0
        confidence_raw = {
            "average_word_probability": average_probability,
            "average_log_probability": average_log_probability,
            "average_no_speech_probability": average_no_speech_probability,
            "word_timestamp_missing_rate": missing_rate,
            "vad_coverage_ratio": vad_coverage_ratio,
            "low_confidence_word_ratio": low_words / word_count,
            "agreement_score": agreement_score,
        }
        confidence_deductions = [
            _deduction("MODEL_CONFIDENCE", 100 - base_confidence, max(0, 100 - base_confidence)),
            _deduction("MISSING_WORD_TIMESTAMPS", missing_rate, missing_rate * 30),
            _deduction(
                "LOW_VAD_COVERAGE",
                vad_coverage_ratio,
                max(0.0, 0.9 - vad_coverage_ratio) * 50,
            ),
            _deduction("LOW_CONFIDENCE_WORDS", low_words / word_count, low_words / word_count * 20),
            _deduction("HIGH_NO_SPEECH", high_no_speech, high_no_speech * 2),
        ]
        if average_log_probability is not None:
            log_probability_penalty = max(0.0, -float(average_log_probability) - 0.5) * 15
            confidence_deductions.append(
                _deduction(
                    "LOW_AVERAGE_LOGPROB",
                    float(average_log_probability),
                    min(15.0, log_probability_penalty),
                )
            )
        if average_no_speech_probability is not None:
            confidence_deductions.append(
                _deduction(
                    "AVERAGE_NO_SPEECH_PROBABILITY",
                    float(average_no_speech_probability),
                    min(10.0, max(0.0, float(average_no_speech_probability)) * 10),
                )
            )
    if agreement_score is not None:
        confidence_raw["agreement_score"] = agreement_score
        confidence_deductions.append(
            _deduction("LOW_CROSS_SOURCE_AGREEMENT", 100 - agreement_score, max(0, 70 - agreement_score) * 0.25)
        )

    dimensions = {
        "structure": _dimension(
            {
                "segment_count": len(segments), "empty_segments": empty, "tag_residuals": tags,
                "control_characters": controls, "invisible_characters": invisibles,
                "duplicate_ids": duplicate_ids, "invalid_ids": invalid_ids,
                "invalid_text": invalid_text,
            },
            structure_deductions,
            weights["structure"],
        ),
        "timeline": _dimension(
            {
                "invalid_timestamps": invalid_times, "reversed_timestamps": reverse,
                "overlaps": overlaps, "under_0_3_seconds": under, "over_8_seconds": over,
                "unreasonable_gaps": bad_gaps, "beyond_audio_duration": beyond_audio,
            },
            timeline_deductions,
            weights["timeline"],
        ),
        "coverage": _dimension(
            {
                "covered_speech_seconds": round(covered_speech, 3),
                "total_speech_seconds": round(total_speech, 3),
                "coverage_ratio": round(coverage_ratio, 6),
                "first_speech_covered": first_speech_covered,
                "last_speech_covered": last_speech_covered,
                "maximum_uncovered_speech_seconds": round(max_uncovered, 3),
                "subtitle_coverage_seconds": round(_duration(subtitle_intervals), 3),
                "audio_duration": round(audio_duration, 3),
            },
            coverage_deductions,
            weights["coverage"],
        ),
        "stability": _dimension(
            {
                "adjacent_duplicates": adjacent, "cumulative_text_residuals": cumulative,
                "repeated_phrases": repeated_phrases, "transition_cue_residuals": transitions,
                "high_no_speech_segments": high_no_speech,
            },
            stability_deductions,
            weights["stability"],
        ),
        "readability": _dimension(
            {
                "over_max_cps": fast, "long_lines": long_lines, "too_many_lines": too_many_lines,
                "over_recommended_duration": too_long, "short_fragments": fragments,
                "unnatural_breaks": unnatural,
            },
            readability_deductions,
            weights["readability"],
        ),
        "source_confidence": _dimension(confidence_raw, confidence_deductions, weights["source_confidence"]),
    }
    if not segments:
        hard_fail_reasons.append("SEGMENT_COUNT_ZERO")
    if invalid_times:
        hard_fail_reasons.append("INVALID_TIMESTAMPS")
    if overlaps:
        hard_fail_reasons.append("OVERLAPS_REMAIN")
    if empty:
        hard_fail_reasons.append("EMPTY_SUBTITLES_REMAIN")
    if coverage_ratio < float(config["minimum_speech_coverage"]):
        hard_fail_reasons.append("SPEECH_COVERAGE_BELOW_MINIMUM")
    hard_fail_reasons = list(dict.fromkeys(hard_fail_reasons))
    final_score = round(sum(item["weighted_score"] for item in dimensions.values()), 3)
    if hard_fail_reasons:
        review_flags.extend(hard_fail_reasons)
    return {
        "path": str(subtitle_path),
        "source": source,
        "source_type": source_type,
        "scores": dimensions,
        "final_score": final_score,
        "hard_fail": bool(hard_fail_reasons),
        "hard_fail_reasons": hard_fail_reasons,
        "flags": list(dict.fromkeys(review_flags)),
        "parse_error": parse_error,
    }
