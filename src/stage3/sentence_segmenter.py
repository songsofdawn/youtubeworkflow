from __future__ import annotations

import re

from .models import SubtitleSegment, WordEvent


SECONDARY_END = re.compile(r"[,;:][\"')\]]*$")

# Words in this set are not comfortable subtitle endings: the viewer has to
# hold them until the following caption reveals their object/complement.  Keep
# the set deliberately syntax-based and conservative so P0 does not attempt
# subjective language correction.
DANGLING_BOUNDARY_WORDS = frozenset(
    {
        "a", "an", "the",
        "my", "your", "his", "her", "its", "our", "their",
        "this", "these", "those",
        "to", "of", "for", "with", "from", "at", "in", "on", "by",
        "into", "onto", "about", "over", "under", "between", "through", "without",
        "and", "or", "but", "because", "if", "when", "while", "than", "as",
    }
)


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


def _word_key(value: str) -> str:
    return re.sub(r"(^[^A-Za-z']+|[^A-Za-z']+$)", "", value).casefold()


def _has_strong_end(value: str) -> bool:
    """Return whether a token is a real sentence stop.

    YouTube and ASR frequently use an ellipsis to represent hesitation.  It is
    not a safe semantic boundary (``buy... a cat``), even though the previous
    implementation treated its final dot as a full stop.
    """
    stripped = re.sub(r"[\"')\]]+$", "", value.strip())
    if stripped.endswith(("!", "?")):
        return True
    return bool(stripped.endswith(".") and not re.search(r"\.{2,}$", stripped))


def _replace_words(segment: SubtitleSegment, words: list[WordEvent], warning: str) -> None:
    segment.start = words[0].start
    segment.end = words[-1].end
    segment.text = _join_words(words)
    segment.words = list(words)
    segment.source_cue_ids = sorted({word.source_cue_id for word in words})
    segment.source_segment_ids = sorted(
        {int(word.source_segment_id) for word in words if word.source_segment_id is not None}
    )
    segment.warnings = sorted(set(segment.warnings + [warning]))


def _dangling_tail_start(words: list[WordEvent]) -> int:
    """Return the start of a short dependent phrase at the right boundary."""
    start = len(words)
    # ``in the`` / ``as a`` should move together; limiting the scan prevents a
    # run of conversational conjunctions from draining the preceding caption.
    while start > 0 and len(words) - start < 3:
        if _word_key(words[start - 1].text) not in DANGLING_BOUNDARY_WORDS:
            break
        start -= 1
    return start


def _repair_fragmented_boundaries(
    segments: list[SubtitleSegment], config: dict
) -> list[SubtitleSegment]:
    """Join or rebalance objective cross-caption fragments.

    This pass never changes word order or text and never exceeds the configured
    per-caption text capacity.  It uses word timestamps to move a dangling word
    such as ``a`` to the caption containing its complement when a full merge is
    too large.
    """
    if len(segments) < 2:
        return segments
    max_chars = int(config["english_max_chars_per_line"]) * int(config["max_lines"])
    max_duration = float(config.get("hard_max_segment_duration", config["max_segment_duration"]))
    join_gap = max(
        float(config.get("sentence_gap_seconds", 0.6)),
        float(config.get("semantic_join_gap_seconds", 1.0)),
    )
    fragment_words = max(1, int(config.get("orphan_fragment_max_words", 2)))

    repaired = list(segments)
    index = 0
    while index + 1 < len(repaired):
        left, right = repaired[index], repaired[index + 1]
        if not left.words or not right.words:
            index += 1
            continue
        gap = right.words[0].start - left.words[-1].end
        dangling_start = _dangling_tail_start(left.words)
        left_dangles = dangling_start < len(left.words)
        right_is_fragment = len(right.words) <= fragment_words
        combined_words = left.words + right.words
        combined_text = _join_words(combined_words)
        combined_duration = combined_words[-1].end - combined_words[0].start
        is_open_boundary = not _has_strong_end(left.words[-1].text)
        may_merge = is_open_boundary and (
            (left_dangles and gap <= join_gap)
            or (right_is_fragment and gap <= float(config.get("sentence_gap_seconds", 0.6)))
        )

        if (
            may_merge
            and (left_dangles or right_is_fragment)
            and len(combined_text) <= max_chars
            and combined_duration <= max_duration
        ):
            _replace_words(left, combined_words, "SEMANTIC_FRAGMENT_MERGED")
            left.warnings = sorted(set(left.warnings + right.warnings))
            del repaired[index + 1]
            continue

        if is_open_boundary and gap <= join_gap and left_dangles and dangling_start > 0:
            shifted = left.words[dangling_start:] + right.words
            if (
                len(_join_words(shifted)) <= max_chars
                and shifted[-1].end - shifted[0].start <= max_duration
            ):
                _replace_words(left, left.words[:dangling_start], "SEMANTIC_BOUNDARY_REBALANCED")
                _replace_words(right, shifted, "SEMANTIC_BOUNDARY_REBALANCED")
                index += 1
                continue

        # If the last caption is still a one/two-word orphan and a full merge
        # cannot fit, move the smallest useful tail from the left.  This keeps
        # both pages readable without changing the configured size ceiling.
        if (
            is_open_boundary
            and gap <= float(config.get("sentence_gap_seconds", 0.6))
            and right_is_fragment
            and len(left.words) > 1
        ):
            for move_count in range(1, min(3, len(left.words) - 1) + 1):
                kept = left.words[:-move_count]
                shifted = left.words[-move_count:] + right.words
                if _word_key(kept[-1].text) in DANGLING_BOUNDARY_WORDS:
                    continue
                if (
                    len(_join_words(kept)) <= max_chars
                    and len(_join_words(shifted)) <= max_chars
                    and shifted[-1].end - shifted[0].start <= max_duration
                ):
                    _replace_words(left, kept, "ORPHAN_FRAGMENT_REBALANCED")
                    _replace_words(right, shifted, "ORPHAN_FRAGMENT_REBALANCED")
                    break
        index += 1

    for identifier, segment in enumerate(repaired, 1):
        segment.id = identifier
    return repaired


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
        if _has_strong_end(event.text) or (duration >= max_duration * 0.72 and SECONDARY_END.search(event.text)):
            result.append(_make_segment(len(result) + 1, current))
            current = []
    if current:
        result.append(_make_segment(len(result) + 1, current))
    return _repair_fragmented_boundaries(result, config)
