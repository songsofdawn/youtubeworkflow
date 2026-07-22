from __future__ import annotations

import html
import re
import unicodedata
from pathlib import Path

from .models import RawCue, WordEvent
from .subtitle_writer import TIME_LINE, parse_timestamp


INLINE_TIME = re.compile(r"<(?P<time>\d{1,2}:\d{2}:\d{2}[.,]\d{3})>")
STYLE_TAG = re.compile(r"</?(?:c(?:\.[^ >]+)?|v|lang|ruby|rt|b|i|u)(?:\s+[^>]*)?>", re.I)
ANY_TAG = re.compile(r"<[^>]*>")
WORD = re.compile(r"\S+")


def clean_text(value: str) -> str:
    value = INLINE_TIME.sub(" ", value)
    value = STYLE_TAG.sub("", value)
    value = ANY_TAG.sub("", value)
    value = html.unescape(value).replace("\u00a0", " ")
    value = "".join(
        character
        for character in value
        if unicodedata.category(character) not in {"Cc", "Cf"} or character in "\n\t"
    )
    lines: list[str] = []
    for line in value.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line and (not lines or normalize_for_compare(line) != normalize_for_compare(lines[-1])):
            lines.append(line)
    return " ".join(lines).strip()


def normalize_for_compare(value: str) -> str:
    value = html.unescape(value).replace("“", '"').replace("”", '"').replace("’", "'")
    value = STYLE_TAG.sub("", value)
    value = ANY_TAG.sub("", value)
    return re.sub(r"\s+", " ", value).strip().casefold()


def _timed_words(raw: str, cue_id: int, start: float, end: float) -> list[WordEvent]:
    matches = list(INLINE_TIME.finditer(raw))
    words: list[WordEvent] = []
    if matches:
        prefix = clean_text(raw[: matches[0].start()])
        chunks: list[tuple[float, float, str]] = []
        if prefix:
            chunks.append((start, parse_timestamp(matches[0]["time"]), prefix))
        for offset, match in enumerate(matches):
            chunk_start = max(start, parse_timestamp(match["time"]))
            chunk_end = end if offset + 1 == len(matches) else min(end, parse_timestamp(matches[offset + 1]["time"]))
            text = clean_text(raw[match.end() : matches[offset + 1].start() if offset + 1 < len(matches) else len(raw)])
            if text:
                chunks.append((chunk_start, max(chunk_start + 0.01, chunk_end), text))
        for chunk_start, chunk_end, text in chunks:
            tokens = WORD.findall(text)
            duration = max(0.01, chunk_end - chunk_start)
            for index, token in enumerate(tokens):
                token_start = chunk_start + duration * index / len(tokens)
                token_end = chunk_start + duration * (index + 1) / len(tokens)
                words.append(WordEvent(token, token_start, token_end, start, cue_id))
    else:
        tokens = WORD.findall(clean_text(raw))
        duration = max(0.01, end - start)
        for index, token in enumerate(tokens):
            words.append(
                WordEvent(
                    token,
                    start + duration * index / max(1, len(tokens)),
                    start + duration * (index + 1) / max(1, len(tokens)),
                    start,
                    cue_id,
                )
            )
    return words


def parse_youtube_vtt(path: Path | str) -> list[RawCue]:
    lines = Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    cues: list[RawCue] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        match = TIME_LINE.search(lines[index])
        if not match:
            index += 1
            continue
        start, end = parse_timestamp(match["start"]), parse_timestamp(match["end"])
        index += 1
        text_lines: list[str] = []
        while index < len(lines):
            if TIME_LINE.search(lines[index]):
                break
            if not lines[index].strip():
                if text_lines:
                    break
                index += 1
                continue
            text_lines.append(lines[index])
            index += 1
        raw = "\n".join(text_lines)
        cue_id = len(cues) + 1
        cues.append(RawCue(cue_id, start, end, clean_text(raw), _timed_words(raw, cue_id, start, end)))
    return cues
