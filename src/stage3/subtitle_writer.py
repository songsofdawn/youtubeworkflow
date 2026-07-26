from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterable

from .models import SubtitleSegment, TranslationSegment


def temporary_sibling(destination: Path) -> Path:
    """Return a short same-directory temp path that stays below Windows MAX_PATH."""
    return destination.with_name(
        f".tmp-{uuid.uuid4().hex[:12]}{destination.suffix}"
    )


def format_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        hours, minutes, seconds = 0, int(parts[0]), float(parts[1])
    elif len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    else:
        raise ValueError(f"Invalid subtitle timestamp: {value}")
    return hours * 3600 + minutes * 60 + seconds


def wrap_text(text: str, width: int, max_lines: int = 2) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text or len(text) <= width or max_lines <= 1:
        return text
    spaces = [match.start() for match in re.finditer(" ", text)]
    if spaces:
        split = min(spaces, key=lambda item: abs(item - len(text) / 2))
    else:
        split = min(width, len(text))
    return text[:split].strip() + "\n" + text[split:].strip()


def write_srt(
    path: Path | str,
    segments: Iterable[SubtitleSegment | TranslationSegment],
    *,
    translated: bool = False,
    width: int = 42,
    max_lines: int = 2,
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    for index, segment in enumerate(segments, 1):
        text = segment.translation if translated else segment.text
        blocks.append(
            f"{index}\n{format_timestamp(segment.start)} --> {format_timestamp(segment.end)}\n"
            f"{wrap_text(text, width, max_lines)}"
        )
    destination.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
    return destination


def write_json(path: Path | str, value: Any) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


def atomic_write_json(path: Path | str, value: Any) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_sibling(destination)
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def atomic_write_srt(
    path: Path | str,
    segments: Iterable[SubtitleSegment | TranslationSegment],
    *,
    translated: bool = False,
    width: int = 42,
    max_lines: int = 2,
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_sibling(destination)
    try:
        write_srt(temporary, segments, translated=translated, width=width, max_lines=max_lines)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


TIME_LINE = re.compile(
    r"(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*"
    r"(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})"
)


def read_srt(path: Path | str) -> list[SubtitleSegment]:
    lines = Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    result: list[SubtitleSegment] = []
    index = 0
    while index < len(lines):
        match = TIME_LINE.search(lines[index])
        if not match:
            index += 1
            continue
        start, end = parse_timestamp(match["start"]), parse_timestamp(match["end"])
        index += 1
        text: list[str] = []
        while index < len(lines) and lines[index].strip():
            text.append(lines[index].strip())
            index += 1
        result.append(SubtitleSegment(len(result) + 1, start, end, " ".join(text)))
    return result
