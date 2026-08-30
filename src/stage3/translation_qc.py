from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .models import SubtitleSegment, TranslationSegment
from .youtube_vtt_parser import normalize_for_compare


def p0_quality(cues_count: int, segments: list[SubtitleSegment], stats: dict[str, Any]) -> dict[str, Any]:
    empty = sum(not item.text.strip() for item in segments)
    adjacent = sum(
        normalize_for_compare(left.text) == normalize_for_compare(right.text)
        for left, right in zip(segments, segments[1:])
    )
    rolling = sum(
        bool(left.text and right.text and normalize_for_compare(right.text).startswith(normalize_for_compare(left.text)))
        for left, right in zip(segments, segments[1:])
    )
    overlaps = sum(right.start < left.end for left, right in zip(segments, segments[1:]))
    invalid = sum(item.start >= item.end for item in segments)
    reversed_count = sum(right.start < left.start for left, right in zip(segments, segments[1:]))
    short = sum(item.duration < 0.3 for item in segments)
    long = sum(item.duration > 8.0 for item in segments)
    fast = sum(item.duration and len(item.text) / item.duration > 22 for item in segments)
    coverage = sum(item.duration for item in segments)
    passed = empty == adjacent == overlaps == invalid == 0
    return {
        "status": "QC_PASSED" if passed else "REVIEW_REQUIRED",
        "empty_segments": empty,
        "adjacent_duplicates": adjacent,
        "rolling_duplicate_residuals": rolling,
        "overlaps": overlaps,
        "invalid_timestamps": invalid,
        "reversed_timestamps": reversed_count,
        "under_0_3_seconds": short,
        "over_8_seconds": long,
        "over_22_cps": fast,
        "coverage_seconds": round(coverage, 3),
        "input_cues": cues_count,
        "output_segments": len(segments),
        **stats,
    }


def estimate_translation(segment: TranslationSegment, config: dict) -> None:
    characters = sum(1 for item in segment.translation if re.match(r"[\u3400-\u9fffA-Za-z0-9]", item))
    segment.estimated_tts_duration = round(characters / float(config["chinese_chars_per_second"]), 3)
    segment.duration_ratio = round(segment.estimated_tts_duration / max(0.05, segment.source_duration), 3)
    if segment.duration_ratio > float(config["tts_rewrite_ratio"]):
        segment.qc_flags.append("TTS_TOO_LONG")
        segment.qc_flags.append("TOO_LONG_FOR_DURATION")
    elif segment.duration_ratio > float(config["tts_warning_ratio"]):
        segment.qc_flags.append("TTS_LENGTH_WARNING")
    if not segment.translation.strip():
        segment.qc_flags.append("EMPTY_TRANSLATION")
    if len(segment.translation) > int(config["chinese_max_chars_per_line"]) * int(config["max_lines"]):
        segment.qc_flags.append("CHINESE_TOO_LONG")


def translation_quality(source: list[SubtitleSegment], translations: list[TranslationSegment], config: dict) -> dict[str, Any]:
    source_ids = {item.id for item in source}
    translated_ids = {item.id for item in translations}
    missing = sorted(source_ids - translated_ids)
    extra = sorted(translated_ids - source_ids)
    empty = [item.id for item in translations if not item.translation.strip()]
    duplicates = [right.id for left, right in zip(translations, translations[1:]) if left.translation.strip() and left.translation.strip() == right.translation.strip()]
    overlaps = sum(right.start < left.end for left, right in zip(translations, translations[1:]))
    changed_timeline = sum(
        abs(source_item.start - translated.start) > 0.001 or abs(source_item.end - translated.end) > 0.001
        for source_item, translated in zip(source, translations)
        if source_item.id == translated.id
    )
    illegal_controls = [
        item.id for item in translations
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", item.translation)
    ]
    punctuation = [
        item.id for item in translations
        if re.search(r"([，。！？])\1{1,}", item.translation)
    ]
    flags = Counter(flag for item in translations for flag in item.qc_flags)
    passed = (
        not missing and not extra and not empty and overlaps == 0
        and changed_timeline == 0 and not illegal_controls
    )
    return {
        "status": "QC_PASSED" if passed else "REVIEW_REQUIRED",
        "english_ids": len(source_ids),
        "chinese_ids": len(translated_ids),
        "missing_ids": missing,
        "extra_ids": extra,
        "empty_translation_ids": empty,
        "adjacent_duplicate_ids": duplicates,
        "timeline_overlaps": overlaps,
        "timeline_changed": changed_timeline,
        "illegal_control_character_ids": illegal_controls,
        "unnatural_punctuation_ids": punctuation,
        "qc_flag_counts": dict(flags),
    }


def translation_payload_overflow(
    source: SubtitleSegment,
    translation: str,
) -> dict[str, Any] | None:
    """Describe obvious per-ID response contamination without judging wording."""
    source_characters = sum(character.isalnum() for character in source.text)
    translation_characters = sum(character.isalnum() for character in translation)
    duration = max(0.05, float(source.duration))
    length_ratio = translation_characters / max(1, source_characters)
    characters_per_second = translation_characters / duration
    reasons: list[str] = []
    if translation_characters > 120:
        reasons.append("ABSOLUTE_LENGTH")
    if (
        source_characters >= 24
        and translation_characters >= 42
        and length_ratio > 1.3
    ):
        reasons.append("SOURCE_LENGTH_RATIO")
    if translation_characters >= 42 and characters_per_second > 22.0:
        reasons.append("TIMELINE_DENSITY")
    if not reasons:
        return None
    return {
        "id": source.id,
        "source_characters": source_characters,
        "translation_characters": translation_characters,
        "duration_seconds": round(duration, 3),
        "length_ratio": round(length_ratio, 3),
        "characters_per_second": round(characters_per_second, 3),
        "reasons": reasons,
    }


def translation_structure_quality(
    source: list[SubtitleSegment], translations: list[TranslationSegment]
) -> dict[str, Any]:
    """Validate file integrity and keep each translated payload inside its source cue.

    This deliberately does not judge wording or semantic quality.  The payload
    limits only catch structural response corruption such as several later
    captions being concatenated into one ID.
    """
    source_ids = {item.id for item in source}
    translated_ids = {item.id for item in translations}
    missing = sorted(source_ids - translated_ids)
    extra = sorted(translated_ids - source_ids)
    empty = [item.id for item in translations if not item.translation.strip()]
    overlaps = sum(
        right.start < left.end for left, right in zip(translations, translations[1:])
    )
    changed_timeline = sum(
        abs(source_item.start - translated.start) > 0.001
        or abs(source_item.end - translated.end) > 0.001
        for source_item, translated in zip(source, translations)
        if source_item.id == translated.id
    )
    illegal_controls = [
        item.id
        for item in translations
        if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", item.translation)
    ]
    source_by_id = {item.id: item for item in source}
    payload_overflow_details: list[dict[str, Any]] = []
    for translated in translations:
        original = source_by_id.get(translated.id)
        if original is None:
            continue
        detail = translation_payload_overflow(original, translated.translation)
        if detail is not None:
            payload_overflow_details.append(detail)
    payload_overflow_ids = [item["id"] for item in payload_overflow_details]
    passed = (
        not missing
        and not extra
        and not empty
        and overlaps == 0
        and changed_timeline == 0
        and not illegal_controls
        and not payload_overflow_ids
    )
    return {
        "status": "QC_PASSED" if passed else "REVIEW_REQUIRED",
        "validation_mode": "structural_only",
        "semantic_quality_checks": "disabled",
        "english_ids": len(source_ids),
        "chinese_ids": len(translated_ids),
        "missing_ids": missing,
        "extra_ids": extra,
        "empty_translation_ids": empty,
        "timeline_overlaps": overlaps,
        "timeline_changed": changed_timeline,
        "illegal_control_character_ids": illegal_controls,
        "segment_payload_overflow_ids": payload_overflow_ids,
        "segment_payload_overflow_details": payload_overflow_details,
    }


def qc_text(report: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in report.items()) + "\n"
