from __future__ import annotations

import re

from .models import SubtitleSegment, WordEvent


STRONG_END = re.compile(r"[.!?][\"')\]]*$")
SECONDARY_END = re.compile(r"[,;:][\"')\]]*$")


def _join_words(words: list[WordEvent]) -> str:
    value = " ".join(word.text for word in words)
    return re.sub(r"\s+([,.;:!?])", r"\1", value).strip()


def _make_segment(identifier: int, words: list[WordEvent]) -> SubtitleSegment:
    return SubtitleSegment(
        id=identifier,
        start=words[0].start,
        end=words[-1].end,
        text=_join_words(words),
        source_cue_ids=sorted({word.source_cue_id for word in words}),
        words=list(words),
    )


def segment_sentences(events: list[WordEvent], config: dict) -> list[SubtitleSegment]:
    if not events:
        return []
    gap_limit = float(config["sentence_gap_seconds"])
    max_duration = float(config["max_segment_duration"])
    max_chars = int(config["english_max_chars_per_line"]) * int(config["max_lines"])
    result: list[SubtitleSegment] = []
    current: list[WordEvent] = []
    for event in events:
        if current:
            gap = event.start - current[-1].end
            duration = event.end - current[0].start
            projected = len(_join_words(current + [event]))
            should_split_before = gap > gap_limit or duration > max_duration or projected > max_chars
            if should_split_before:
                result.append(_make_segment(len(result) + 1, current))
                current = []
        current.append(event)
        duration = current[-1].end - current[0].start
        if STRONG_END.search(event.text) or (duration >= max_duration * 0.72 and SECONDARY_END.search(event.text)):
            result.append(_make_segment(len(result) + 1, current))
            current = []
    if current:
        result.append(_make_segment(len(result) + 1, current))
    return result
