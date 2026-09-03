from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .models import SubtitleSegment, TranslationSegment


STRONG_END = re.compile(r"[.!?][\"')\]]*$")
SECONDARY_END = re.compile(r"[,;:][\"')\]]*$")
CONTINUATION_START = re.compile(
    r"^(?:and|but|or|so|because|because of|that|which|who|when|while|where|"
    r"to|of|for|with|from|in|on|at|by|as|if|then|than|also|just|really|"
    r"actually|basically|probably|maybe|like)\b",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)?")
WHITESPACE_RE = re.compile(r"\s+")


def normalize_script_text(text: str) -> str:
    """Normalize layout-only whitespace without changing visible wording."""

    return WHITESPACE_RE.sub("", str(text or "")).strip()


def script_text_hash(text: str) -> str:
    return hashlib.sha256(normalize_script_text(text).encode("utf-8")).hexdigest()


def _word_count(text: str) -> int:
    return len(WORD_RE.findall(text))


def _join_text(parts: Iterable[str]) -> str:
    value = " ".join(str(part or "").strip() for part in parts if str(part or "").strip())
    return re.sub(r"\s+([,.;:!?])", r"\1", value).strip()


def _is_short_standalone(segment: SubtitleSegment, settings: dict[str, Any]) -> bool:
    max_words = max(1, int(settings.get("short_standalone_max_words", 4)))
    return bool(STRONG_END.search(segment.text.strip())) and _word_count(segment.text) <= max_words


def _make_utterance(identifier: int, members: list[SubtitleSegment]) -> SubtitleSegment:
    first = members[0]
    last = members[-1]
    source_ids: list[int] = []
    cue_ids: list[int] = []
    words = []
    warnings: list[str] = []
    for member in members:
        source_ids.extend(member.source_segment_ids or [member.id])
        cue_ids.extend(member.source_cue_ids)
        words.extend(member.words)
        warnings.extend(member.warnings)
    return SubtitleSegment(
        id=identifier,
        start=float(first.start),
        end=float(last.end),
        text=_join_text(member.text for member in members),
        source_cue_ids=sorted(set(cue_ids)),
        words=words,
        warnings=sorted(set(warnings)),
        source=first.source,
        source_segment_ids=sorted(set(source_ids)),
        metadata={
            "dubbing_utterance": True,
            "source_segment_count": len(members),
        },
    )


def _can_append(
    members: list[SubtitleSegment],
    candidate: SubtitleSegment,
    settings: dict[str, Any],
) -> bool:
    current = _make_utterance(0, members)
    last = members[-1]
    gap_limit = max(0.0, float(settings.get("merge_gap_seconds", 0.55)))
    unfinished_gap_limit = max(
        gap_limit, float(settings.get("unfinished_merge_gap_seconds", 1.0))
    )
    target_min = max(0.2, float(settings.get("target_min_duration_seconds", 2.2)))
    target_max = max(target_min, float(settings.get("target_max_duration_seconds", 6.0)))
    hard_max = max(target_max, float(settings.get("hard_max_duration_seconds", 9.0)))
    max_words = max(4, int(settings.get("max_source_words", 42)))
    max_chars = max(40, int(settings.get("max_source_chars", 240)))

    current_text = current.text.strip()
    candidate_text = candidate.text.strip()
    gap = max(0.0, float(candidate.start) - float(last.end))

    # A visibly unfinished phrase is allowed a little more silence than a
    # normal merge.  YouTube rolling captions frequently create artificial
    # 0.6-1.0 s gaps inside one sentence (e.g. "according to my" / "research").
    current_complete = bool(STRONG_END.search(current_text))
    allowed_gap = gap_limit if current_complete else unfinished_gap_limit
    if gap > allowed_gap:
        return False

    combined_duration = float(candidate.end) - float(members[0].start)
    combined_text = _join_text([current.text, candidate.text])
    if combined_duration > hard_max + 1e-6:
        return False
    if _word_count(combined_text) > max_words or len(combined_text) > max_chars:
        return False

    # Complete spoken sentences are boundaries regardless of duration.  Do not
    # glue independent reactions/sentences merely to reach the preferred 2.2 s.
    if current_complete:
        return False

    current_duration = current.duration

    # Very short unfinished fragments should always be completed when safe.
    if current_duration < target_min:
        return True

    # Continuation words strongly indicate a visual subtitle break rather than
    # a speech boundary.
    if CONTINUATION_START.search(candidate_text):
        return True

    # Commas/colons are weak boundaries.  Only use them once the unit is already
    # near the preferred maximum; otherwise finish the thought.
    if SECONDARY_END.search(current_text) and current_duration >= target_max * 0.8:
        return False

    # target_max is a preference, not a hard sentence cutter.  An unfinished
    # phrase may grow to hard_max so we do not create TTS such as
    # "versus a one-million-dollar" / "Minecraft server.".
    return combined_duration <= hard_max


def build_dubbing_utterances(
    source: list[SubtitleSegment],
    settings: dict[str, Any] | None = None,
) -> list[SubtitleSegment]:
    """Build sentence-like TTS units from display-oriented subtitle segments.

    The returned units preserve the original speech span and record every source
    segment id. They are intentionally coarser than display subtitles: one
    utterance may later be paginated into multiple ASS events without changing
    the Chinese text used by TTS.
    """

    cfg = dict(settings or {})
    ordered = sorted(
        (item for item in source if item.text.strip()),
        key=lambda item: (float(item.start), float(item.end), int(item.id)),
    )
    if not ordered:
        return []

    groups: list[list[SubtitleSegment]] = []
    current: list[SubtitleSegment] = []
    for segment in ordered:
        if not current:
            current = [segment]
            continue
        if _can_append(current, segment, cfg):
            current.append(segment)
        else:
            groups.append(current)
            current = [segment]
    if current:
        groups.append(current)

    # Repair tiny unfinished groups without crossing a completed sentence.
    # Prefer merging a fragment forward ("according to my" -> "research...")
    # instead of attaching it backward to the previous complete thought.
    target_min = max(0.2, float(cfg.get("target_min_duration_seconds", 2.2)))
    hard_max = max(
        target_min,
        float(cfg.get("hard_max_duration_seconds", 9.0)),
    )
    gap_limit = max(
        float(cfg.get("merge_gap_seconds", 0.55)),
        float(cfg.get("unfinished_merge_gap_seconds", 1.0)),
    )
    max_words = max(4, int(cfg.get("max_source_words", 42)))
    max_chars = max(40, int(cfg.get("max_source_chars", 240)))

    repaired: list[list[SubtitleSegment]] = []
    pending_groups = [list(group) for group in groups]
    index = 0
    while index < len(pending_groups):
        group = pending_groups[index]
        utterance = _make_utterance(0, group)
        is_unfinished_short = (
            utterance.duration < target_min
            and not STRONG_END.search(utterance.text.strip())
            and not (len(group) == 1 and _is_short_standalone(group[0], cfg))
        )
        if is_unfinished_short and index + 1 < len(pending_groups):
            following = pending_groups[index + 1]
            next_utterance = _make_utterance(0, following)
            combined_text = _join_text([utterance.text, next_utterance.text])
            if (
                next_utterance.start - utterance.end <= gap_limit
                and next_utterance.end - utterance.start <= hard_max
                and _word_count(combined_text) <= max_words
                and len(combined_text) <= max_chars
            ):
                pending_groups[index + 1] = group + following
                index += 1
                continue

        if is_unfinished_short and repaired:
            previous = repaired[-1]
            previous_utterance = _make_utterance(0, previous)
            combined_text = _join_text([previous_utterance.text, utterance.text])
            if (
                not STRONG_END.search(previous_utterance.text.strip())
                and utterance.start - previous_utterance.end <= gap_limit
                and utterance.end - previous_utterance.start <= hard_max
                and _word_count(combined_text) <= max_words
                and len(combined_text) <= max_chars
            ):
                previous.extend(group)
                index += 1
                continue

        repaired.append(group)
        index += 1

    result = [_make_utterance(index, group) for index, group in enumerate(repaired, 1)]
    return result


def canonical_script_payload(
    source_path: Path,
    english_path: Path,
    chinese_path: Path,
    utterances: list[SubtitleSegment],
    translations: list[TranslationSegment],
) -> dict[str, Any]:
    translated_by_id = {item.id: item for item in translations}
    rows: list[dict[str, Any]] = []
    full_text: list[str] = []
    for utterance in utterances:
        translation = translated_by_id.get(utterance.id)
        zh_text = str(translation.translation if translation is not None else "").strip()
        full_text.append(zh_text)
        rows.append(
            {
                "id": int(utterance.id),
                "start": round(float(utterance.start), 3),
                "end": round(float(utterance.end), 3),
                "duration": round(float(utterance.duration), 3),
                "source_text": utterance.text,
                "zh_text": zh_text,
                "source_segment_ids": list(utterance.source_segment_ids),
            }
        )
    canonical_text = "".join(full_text)
    return {
        "version": 1,
        "architecture": "single_script_dual_segmentation",
        "source_path": str(source_path),
        "english_path": str(english_path),
        "chinese_path": str(chinese_path),
        "utterance_count": len(rows),
        "canonical_text_hash": script_text_hash(canonical_text),
        "utterances": rows,
    }


def load_canonical_script(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    value = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or not isinstance(value.get("utterances"), list):
        raise ValueError(f"Invalid canonical script: {source}")
    return value


def canonical_text_from_payload(payload: dict[str, Any]) -> str:
    return "".join(str(item.get("zh_text") or "") for item in payload.get("utterances", []))


__all__ = [
    "build_dubbing_utterances",
    "canonical_script_payload",
    "canonical_text_from_payload",
    "load_canonical_script",
    "normalize_script_text",
    "script_text_hash",
]
