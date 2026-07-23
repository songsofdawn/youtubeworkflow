from __future__ import annotations

from collections import Counter
from typing import Any

from .models import SubtitleSegment
from .youtube_vtt_parser import normalize_for_compare


def _continuous_repetitions(segments: list[SubtitleSegment]) -> int:
    count = 0
    for left, right in zip(segments, segments[1:]):
        left_tokens = normalize_for_compare(left.text).split()
        right_tokens = normalize_for_compare(right.text).split()
        maximum = min(len(left_tokens), len(right_tokens), 8)
        if any(left_tokens[-size:] == right_tokens[:size] for size in range(3, maximum + 1)):
            count += 1
    return count


def assess_asr_quality(
    raw_segments: list[dict[str, Any]],
    clean_segments: list[SubtitleSegment],
    words: list[dict[str, Any]],
    *,
    audio_duration: float,
    max_cps: float = 20.0,
) -> dict[str, Any]:
    empty = sum(not item.text.strip() for item in clean_segments)
    invalid = sum(item.start >= item.end for item in clean_segments)
    reversed_count = sum(right.start < left.start for left, right in zip(clean_segments, clean_segments[1:]))
    overlaps = sum(right.start < left.end for left, right in zip(clean_segments, clean_segments[1:]))
    short = sum(item.duration < 0.3 for item in clean_segments)
    long = sum(item.duration > 8.0 for item in clean_segments)
    fast = sum(item.duration > 0 and len(item.text) / item.duration > max_cps for item in clean_segments)
    adjacent = sum(
        bool(left.text.strip()) and normalize_for_compare(left.text) == normalize_for_compare(right.text)
        for left, right in zip(clean_segments, clean_segments[1:])
    )
    high_no_speech = sum(float(item.get("no_speech_prob") or 0.0) > 0.6 for item in raw_segments)
    log_probabilities = [
        float(item["avg_logprob"]) for item in raw_segments if item.get("avg_logprob") is not None
    ]
    no_speech_probabilities = [
        float(item["no_speech_prob"]) for item in raw_segments if item.get("no_speech_prob") is not None
    ]
    probabilities = [float(item["probability"]) for item in words if item.get("probability") is not None]
    average_probability = sum(probabilities) / len(probabilities) if probabilities else 0.0
    low_confidence_words = sum(value < 0.5 for value in probabilities)
    missing_word_times = sum(
        item.get("start") is None or item.get("end") is None or bool(item.get("timestamps_approximated"))
        for item in words
    )
    coverage_seconds = sum(item.duration for item in clean_segments)
    coverage_ratio = coverage_seconds / audio_duration if audio_duration > 0 else 0.0
    final_end = clean_segments[-1].end if clean_segments else 0.0
    tail_gap = max(0.0, audio_duration - final_end)
    truncated = bool(clean_segments and audio_duration > 0 and tail_gap > max(5.0, audio_duration * 0.1))
    passed = empty == invalid == overlaps == adjacent == 0 and len(clean_segments) > 0
    return {
        "status": "QC_PASSED" if passed else "REVIEW_REQUIRED",
        "empty_segments": empty,
        "invalid_timestamps": invalid,
        "reversed_timestamps": reversed_count,
        "overlaps": overlaps,
        "under_0_3_seconds": short,
        "over_8_seconds": long,
        "over_max_cps": fast,
        "high_no_speech_segments": high_no_speech,
        "average_log_probability": round(
            sum(log_probabilities) / len(log_probabilities), 6
        ) if log_probabilities else None,
        "average_no_speech_probability": round(
            sum(no_speech_probabilities) / len(no_speech_probabilities), 6
        ) if no_speech_probabilities else None,
        "average_word_probability": round(average_probability, 6),
        "low_confidence_words": low_confidence_words,
        "word_timestamp_missing_rate": round(missing_word_times / max(1, len(words)), 6),
        "coverage_seconds": round(coverage_seconds, 3),
        "coverage_ratio": round(coverage_ratio, 6),
        "adjacent_duplicates": adjacent,
        "continuous_repeated_phrases": _continuous_repetitions(clean_segments),
        "audio_tail_gap_seconds": round(tail_gap, 3),
        "audio_end_may_be_truncated": truncated,
        "raw_segment_count": len(raw_segments),
        "segment_count": len(clean_segments),
        "word_count": len(words),
        "warning_counts": dict(Counter(flag for segment in clean_segments for flag in segment.warnings)),
    }
