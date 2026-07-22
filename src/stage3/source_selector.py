from __future__ import annotations

from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .subtitle_writer import read_srt
from .youtube_vtt_parser import normalize_for_compare, parse_youtube_vtt


DEFAULT_PRIORITY = ["en.manual.vtt", "en.manual.srt", "en.auto.vtt", "en.auto.srt"]


def select_source(video_dir: Path | str, priority: list[str] | None = None) -> Path | None:
    subtitle_dir = Path(video_dir) / "subtitles"
    for name in priority or DEFAULT_PRIORITY:
        path = subtitle_dir / name
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def assess_source(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".vtt":
        cues = parse_youtube_vtt(path)
    else:
        cues = read_srt(path)
    texts = [cue.text for cue in cues]
    normalized = [normalize_for_compare(text) for text in texts]
    total = max(1, len(cues))
    exact = sum(1 for left, right in zip(normalized, normalized[1:]) if left and left == right)
    rolling = 0
    overlap = 0
    for previous, current, previous_cue, current_cue in zip(normalized, normalized[1:], cues, cues[1:]):
        if previous and current and previous != current:
            ratio = SequenceMatcher(None, previous, current).ratio()
            if current.startswith(previous) or previous.endswith(current) or ratio >= 0.82:
                rolling += 1
        if current_cue.start < previous_cue.end:
            overlap += 1
    empty = sum(not text.strip() for text in texts)
    very_short = sum((cue.end - cue.start) <= 0.05 for cue in cues)
    penalties = (empty / total) * 20 + (very_short / total) * 15 + (exact / total) * 20 + (rolling / total) * 25 + (overlap / total) * 20
    return {
        "selected_source": str(path),
        "source_type": path.stem,
        "cue_count": len(cues),
        "empty_ratio": round(empty / total, 6),
        "very_short_ratio": round(very_short / total, 6),
        "exact_duplicate_ratio": round(exact / total, 6),
        "rolling_duplicate_ratio": round(rolling / total, 6),
        "overlap_count": overlap,
        "quality_score": round(max(0.0, 100 - penalties), 2),
        "route": "YOUTUBE_VTT_RECONSTRUCTION" if path.name.endswith(".auto.vtt") else "DIRECT_CLEANING",
    }
