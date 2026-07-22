from __future__ import annotations

from .models import SubtitleSegment
from .youtube_vtt_parser import normalize_for_compare


def _merge(left: SubtitleSegment, right: SubtitleSegment) -> SubtitleSegment:
    left.text = f"{left.text} {right.text}".strip()
    left.end = max(left.end, right.end)
    left.words.extend(right.words)
    left.source_cue_ids = sorted(set(left.source_cue_ids + right.source_cue_ids))
    left.warnings = sorted(set(left.warnings + right.warnings + ["SHORT_SEGMENT_MERGED"]))
    return left


def rebuild_timeline(
    segments: list[SubtitleSegment], config: dict, media_duration: float | None = None
) -> list[SubtitleSegment]:
    minimum_gap = float(config["minimum_gap_ms"]) / 1000.0
    min_duration = float(config["min_segment_duration"])
    max_duration = float(config["max_segment_duration"])
    ordered = sorted((segment for segment in segments if segment.text.strip()), key=lambda item: (item.start, item.end))
    merged: list[SubtitleSegment] = []
    for segment in ordered:
        if merged and normalize_for_compare(segment.text) == normalize_for_compare(merged[-1].text):
            merged[-1].end = max(merged[-1].end, segment.end)
            merged[-1].words.extend(segment.words)
            merged[-1].source_cue_ids = sorted(set(merged[-1].source_cue_ids + segment.source_cue_ids))
            merged[-1].warnings.append("ADJACENT_DUPLICATE_MERGED")
        elif merged and segment.duration < 0.3 and segment.start - merged[-1].end <= 0.6:
            _merge(merged[-1], segment)
        else:
            merged.append(segment)
    fixed: list[SubtitleSegment] = []
    for index, segment in enumerate(merged):
        word_start = segment.words[0].start if segment.words else segment.start
        word_end = segment.words[-1].end if segment.words else segment.end
        start = max(0.0, word_start, fixed[-1].end + minimum_gap if fixed else 0.0)
        next_start = merged[index + 1].start - minimum_gap if index + 1 < len(merged) else None
        natural_end = min(word_end, start + max_duration)
        desired_end = max(natural_end, start + min_duration)
        if next_start is not None:
            desired_end = min(desired_end, next_start)
        if media_duration is not None:
            desired_end = min(desired_end, media_duration)
        if desired_end <= start:
            if fixed:
                _merge(fixed[-1], segment)
                continue
            desired_end = start + 0.05
        segment.start, segment.end = start, desired_end
        fixed.append(segment)
    # A segment can become short only after it is clipped to the next start. Merge
    # those semantic tails instead of extending them into a following caption.
    without_tiny: list[SubtitleSegment] = []
    skip_index = -1
    for index, segment in enumerate(fixed):
        if index == skip_index:
            continue
        if segment.duration < 0.3 and without_tiny:
            _merge(without_tiny[-1], segment)
        elif segment.duration < 0.3 and index == 0 and len(fixed) > 1:
            next_segment = fixed[index + 1]
            segment.text = f"{segment.text} {next_segment.text}".strip()
            segment.end = next_segment.end
            segment.words.extend(next_segment.words)
            segment.source_cue_ids = sorted(set(segment.source_cue_ids + next_segment.source_cue_ids))
            segment.warnings.append("SHORT_SEGMENT_MERGED")
            without_tiny.append(segment)
            skip_index = index + 1
        else:
            without_tiny.append(segment)
    for identifier, segment in enumerate(without_tiny, 1):
        segment.id = identifier
    return without_tiny
