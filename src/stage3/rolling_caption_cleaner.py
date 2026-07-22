from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from .models import RawCue, WordEvent
from .youtube_vtt_parser import normalize_for_compare


TOKEN = re.compile(r"\S+")


def token_key(value: str) -> str:
    return re.sub(r"(^\W+|\W+$)", "", value, flags=re.UNICODE).casefold()


def _overlap_size(previous: list[str], current: list[str]) -> int:
    maximum = min(len(previous), len(current))
    for size in range(maximum, 1, -1):
        if previous[-size:] == current[:size] and all(current[:size]):
            return size
    return 0


def extract_increment(previous: str, current: str) -> tuple[str, int, str]:
    """Extract a rolling-caption suffix without globally removing spoken repetition."""
    previous_clean, current_clean = previous.strip(), current.strip()
    previous_normal, current_normal = normalize_for_compare(previous_clean), normalize_for_compare(current_clean)
    if not previous_normal:
        return current_clean, 0, "new"
    if current_normal == previous_normal:
        return "", len(TOKEN.findall(current_clean)), "exact"
    if current_normal.startswith(previous_normal):
        tokens_previous = TOKEN.findall(previous_clean)
        tokens_current = TOKEN.findall(current_clean)
        return " ".join(tokens_current[len(tokens_previous) :]), len(tokens_previous), "prefix"
    if current_normal in previous_normal and len(current_normal) >= max(4, int(len(previous_normal) * 0.6)):
        return "", len(TOKEN.findall(current_clean)), "contained"

    previous_tokens = TOKEN.findall(previous_clean)
    current_tokens = TOKEN.findall(current_clean)
    overlap = _overlap_size([token_key(item) for item in previous_tokens], [token_key(item) for item in current_tokens])
    if overlap:
        return " ".join(current_tokens[overlap:]), overlap, "suffix_prefix"
    return current_clean, 0, "new"


def _matching_words(cue: RawCue, increment: str) -> list[WordEvent]:
    wanted = [token_key(item) for item in TOKEN.findall(increment)]
    available = [token_key(item.text) for item in cue.words]
    if not wanted:
        return []
    for start in range(max(1, len(available) - len(wanted) + 1)):
        if available[start : start + len(wanted)] == wanted:
            return cue.words[start : start + len(wanted)]
    if len(wanted) <= len(cue.words):
        return cue.words[-len(wanted) :]
    return cue.words


def _fuzzy_rolling(previous_cues: list[RawCue], cue: RawCue, increment: str) -> tuple[str, int]:
    if not increment or not previous_cues:
        return increment, 0
    current_tokens = TOKEN.findall(increment)
    current_keys = [token_key(item) for item in current_tokens]
    for previous in reversed(previous_cues[-3:]):
        if cue.start - previous.end > 0.75:
            continue
        prior_tokens = TOKEN.findall(previous.text)
        prior_keys = [token_key(item) for item in prior_tokens]
        overlap = _overlap_size(prior_keys, current_keys)
        if overlap >= 2 and overlap / max(1, min(len(prior_keys), len(current_keys))) >= 0.6:
            return " ".join(current_tokens[overlap:]), overlap
        ratio = SequenceMatcher(None, prior_keys, current_keys).ratio()
        if len(current_keys) >= 4 and ratio >= 0.9 and cue.start <= previous.end + 0.25:
            return "", len(current_keys)
    return increment, 0


def build_word_events(cues: list[RawCue]) -> tuple[list[WordEvent], dict[str, Any]]:
    stats: dict[str, Any] = {
        "input_cues": len(cues),
        "empty_cues_removed": 0,
        "exact_duplicate_cues_removed": 0,
        "rolling_cues_reduced": 0,
        "rolling_units_removed": 0,
        "transition_cues_processed": 0,
        "transition_cues_without_increment": 0,
    }
    events: list[WordEvent] = []
    recent: list[RawCue] = []
    previous_full = ""
    for cue in cues:
        if cue.end - cue.start <= 0.05:
            stats["transition_cues_processed"] += 1
        if not cue.text.strip():
            stats["empty_cues_removed"] += 1
            recent.append(cue)
            continue
        increment, removed, reason = extract_increment(previous_full, cue.text)
        if reason == "new":
            increment, fuzzy_removed = _fuzzy_rolling(recent, cue, increment)
            removed += fuzzy_removed
            if fuzzy_removed:
                reason = "fuzzy"
        if reason in {"exact", "contained"}:
            stats["exact_duplicate_cues_removed"] += 1
        elif removed:
            stats["rolling_cues_reduced"] += 1
        stats["rolling_units_removed"] += removed
        if not increment.strip():
            if cue.end - cue.start <= 0.05:
                stats["transition_cues_without_increment"] += 1
        else:
            events.extend(_matching_words(cue, increment))
        previous_full = cue.text
        recent.append(cue)
    stats["word_event_count"] = len(events)
    return events, stats
