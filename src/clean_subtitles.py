from __future__ import annotations

import argparse
import html
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


TIME_LINE = re.compile(
    r"(?P<start>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*"
    r"(?P<end>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})"
)
INLINE_TIMESTAMP = re.compile(r"<\d{1,2}:\d{2}:\d{2}[.,]\d{3}>")
HTML_TAG = re.compile(r"<[^>]*>")
SENTENCE_END = re.compile(r"[.!?。！？…][\"'”’）】]*$")
WORD = re.compile(r"\S+")


@dataclass
class Cue:
    start: float
    end: float
    text: str


def parse_timestamp(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        hours, minutes, seconds = 0, int(parts[0]), float(parts[1])
    elif len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    else:
        raise ValueError(f"无效字幕时间: {value}")
    return hours * 3600 + minutes * 60 + seconds


def format_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def clean_visible_text(value: str) -> str:
    value = INLINE_TIMESTAMP.sub("", value)
    value = HTML_TAG.sub("", value)
    value = html.unescape(value).replace("\u00a0", " ")
    value = "".join(character for character in value if unicodedata.category(character) not in {"Cc", "Cf"} or character in "\n\t")
    lines = []
    for line in value.splitlines():
        cleaned = re.sub(r"\s+", " ", line).strip()
        if cleaned and (not lines or cleaned != lines[-1]):
            lines.append(cleaned)
    return smart_join(lines)


def smart_join(parts: Iterable[str]) -> str:
    result = ""
    for part in (str(item).strip() for item in parts):
        if not part:
            continue
        if not result:
            result = part
        elif _is_cjk(result[-1]) and _is_cjk(part[0]):
            result += part
        elif result.endswith((" ", "-", "—", "/")) or part.startswith(("'", "’", ",", ".", "!", "?", ":", ";", "，", "。", "！", "？")):
            result += part
        else:
            result += " " + part
    return re.sub(r"\s+", " ", result).strip()


def _is_cjk(character: str) -> bool:
    return any(start <= ord(character) <= end for start, end in ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF)))


def parse_webvtt(path: Path | str) -> list[Cue]:
    lines = Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    cues: list[Cue] = []
    index = 0
    while index < len(lines):
        match = TIME_LINE.search(lines[index])
        if not match:
            index += 1
            continue
        start, end = parse_timestamp(match.group("start")), parse_timestamp(match.group("end"))
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
        cues.append(Cue(start, end, clean_visible_text("\n".join(text_lines))))
    return cues


def parse_srt(path: Path | str) -> list[Cue]:
    lines = Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    cues: list[Cue] = []
    index = 0
    while index < len(lines):
        match = TIME_LINE.search(lines[index])
        if not match:
            index += 1
            continue
        start, end = parse_timestamp(match.group("start")), parse_timestamp(match.group("end"))
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index].strip():
            text_lines.append(lines[index]); index += 1
        cues.append(Cue(start, end, clean_visible_text("\n".join(text_lines))))
    return cues


def _token_key(token: str) -> str:
    return re.sub(r"(^\W+|\W+$)", "", token, flags=re.UNICODE).casefold()


def _contains_cjk(value: str) -> bool:
    return any(_is_cjk(character) for character in value)


def extract_new_content(previous: str, current: str) -> tuple[str, int]:
    """Return the non-rolling suffix and the approximate number of removed units."""
    previous, current = previous.strip(), current.strip()
    if not previous:
        return current, 0
    if current.casefold() == previous.casefold():
        return "", max(1, len(WORD.findall(current)))
    if current.casefold().startswith(previous.casefold()):
        added = current[len(previous):].strip(" \t-–—")
        return added, max(1, len(WORD.findall(previous)))
    if previous.casefold().endswith(current.casefold()):
        return "", max(1, len(WORD.findall(current)))

    previous_tokens, current_tokens = WORD.findall(previous), WORD.findall(current)
    previous_keys = [_token_key(token) for token in previous_tokens]
    current_keys = [_token_key(token) for token in current_tokens]
    maximum = min(len(previous_keys), len(current_keys))
    for size in range(maximum, 1, -1):
        if previous_keys[-size:] == current_keys[:size] and all(previous_keys[-size:]):
            return smart_join(current_tokens[size:]), size

    if _contains_cjk(previous + current):
        previous_fold, current_fold = previous.casefold(), current.casefold()
        maximum = min(len(previous_fold), len(current_fold))
        for size in range(maximum, 1, -1):
            if previous_fold[-size:] == current_fold[:size]:
                return current[size:].strip(), size
    return current, 0


def remove_immediate_word_repeats(text: str) -> str:
    tokens = WORD.findall(text)
    result: list[str] = []
    for token in tokens:
        if result and _token_key(result[-1]) and _token_key(result[-1]) == _token_key(token):
            continue
        result.append(token)
    return smart_join(result)


def prepare_fragments(cues: list[Cue]) -> tuple[list[Cue], dict[str, int]]:
    stats = {
        "input_cues": len(cues), "empty_removed": 0, "transition_removed": 0,
        "exact_duplicates_merged": 0, "rolling_cues_reduced": 0,
        "rolling_units_removed": 0,
    }
    fragments: list[Cue] = []
    previous_full = ""
    for cue in cues:
        full_text = clean_visible_text(cue.text)
        if not full_text:
            stats["empty_removed"] += 1
            continue
        if cue.end - cue.start < 0.05:
            stats["transition_removed"] += 1
            continue
        delta, removed = extract_new_content(previous_full, full_text)
        if removed:
            stats["rolling_cues_reduced"] += 1
            stats["rolling_units_removed"] += removed
        delta = remove_immediate_word_repeats(delta)
        if not delta:
            if fragments:
                fragments[-1].end = max(fragments[-1].end, cue.end)
            stats["exact_duplicates_merged"] += 1
            previous_full = full_text
            continue
        if fragments and fragments[-1].text.casefold() == delta.casefold() and cue.start <= fragments[-1].end + 0.25:
            fragments[-1].end = max(fragments[-1].end, cue.end)
            stats["exact_duplicates_merged"] += 1
        else:
            fragments.append(Cue(cue.start, cue.end, delta))
        previous_full = full_text
    return fragments, stats


def _starts_like_continuation(text: str) -> bool:
    stripped = text.lstrip("\"'“‘(")
    return bool(stripped) and (stripped[0].islower() or stripped[0] in ",.;:，；：")


def merge_semantic_fragments(fragments: list[Cue]) -> tuple[list[Cue], int]:
    merged: list[Cue] = []
    semantic_merges = 0
    for fragment in fragments:
        if not merged:
            merged.append(Cue(fragment.start, fragment.end, fragment.text)); continue
        previous = merged[-1]
        gap = fragment.start - previous.end
        combined_duration = max(previous.end, fragment.end) - previous.start
        should_merge = (
            gap <= 0.45 and combined_duration <= 7.0
            and (not SENTENCE_END.search(previous.text) or previous.end - previous.start < 1.0 or _starts_like_continuation(fragment.text))
        )
        if should_merge:
            previous.text = remove_immediate_word_repeats(smart_join((previous.text, fragment.text)))
            previous.end = max(previous.end, fragment.end)
            semantic_merges += 1
        else:
            merged.append(Cue(fragment.start, fragment.end, fragment.text))
    return merged, semantic_merges


def _split_text(text: str, parts: int) -> list[str]:
    tokens = WORD.findall(text)
    if len(tokens) >= parts:
        boundaries = [round(index * len(tokens) / parts) for index in range(parts + 1)]
        return [smart_join(tokens[boundaries[index]:boundaries[index + 1]]) for index in range(parts)]
    boundaries = [round(index * len(text) / parts) for index in range(parts + 1)]
    return [text[boundaries[index]:boundaries[index + 1]].strip() for index in range(parts)]


def split_long_cues(cues: list[Cue]) -> list[Cue]:
    result: list[Cue] = []
    for cue in cues:
        duration = max(0.05, cue.end - cue.start)
        count = max(1, math.ceil(duration / 7.0))
        if count == 1:
            result.append(cue); continue
        texts = _split_text(cue.text, count)
        for index, text in enumerate(texts):
            start = cue.start + duration * index / count
            end = cue.start + duration * (index + 1) / count
            if text:
                result.append(Cue(start, end, text))
    return result


def fix_timeline(cues: list[Cue]) -> list[Cue]:
    cues = split_long_cues(sorted(cues, key=lambda cue: (cue.start, cue.end)))
    fixed: list[Cue] = []
    for cue in cues:
        start = max(cue.start, fixed[-1].end if fixed else 0.0)
        end = max(start + 0.05, cue.end)
        fixed.append(Cue(start, end, cue.text))
    for index, cue in enumerate(fixed):
        if cue.end - cue.start >= 1.0:
            continue
        next_start = fixed[index + 1].start if index + 1 < len(fixed) else cue.start + 1.0
        cue.end = max(cue.end, min(cue.start + 1.0, next_start))
    return fixed


def clean_cues(cues: list[Cue]) -> tuple[list[Cue], dict[str, int]]:
    fragments, stats = prepare_fragments(cues)
    merged, semantic_merges = merge_semantic_fragments(fragments)
    cleaned = fix_timeline(merged)
    stats["semantic_merges"] = semantic_merges
    stats["output_cues"] = len(cleaned)
    stats["duplicate_cues_removed"] = stats["exact_duplicates_merged"] + stats["rolling_cues_reduced"]
    return cleaned, stats


def format_two_lines(text: str, preferred_width: int = 44) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= preferred_width:
        return text
    target = len(text) // 2
    spaces = [match.start() for match in re.finditer(r" ", text)]
    split = min(spaces, key=lambda position: abs(position - target)) if spaces else target
    return text[:split].strip() + "\n" + text[split:].strip()


def write_srt(path: Path | str, cues: list[Cue]) -> Path:
    destination = Path(path); destination.parent.mkdir(parents=True, exist_ok=True)
    blocks = [
        f"{index}\n{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n{format_two_lines(cue.text)}"
        for index, cue in enumerate(cues, 1)
    ]
    destination.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8")
    return destination


def align_translation_to_english(english_cues: list[Cue], translation_fragments: list[Cue]) -> list[Cue]:
    if not english_cues or not translation_fragments:
        return []
    assignments: list[list[str]] = [[] for _ in english_cues]
    for fragment in translation_fragments:
        overlaps = [max(0.0, min(fragment.end, cue.end) - max(fragment.start, cue.start)) for cue in english_cues]
        best = max(range(len(english_cues)), key=lambda index: overlaps[index])
        if overlaps[best] <= 0:
            midpoint = (fragment.start + fragment.end) / 2
            best = min(range(len(english_cues)), key=lambda index: abs((english_cues[index].start + english_cues[index].end) / 2 - midpoint))
        assignments[best].append(fragment.text)
    result: list[Cue] = []
    for cue, parts in zip(english_cues, assignments):
        text = remove_immediate_word_repeats(smart_join(parts))
        if text:
            result.append(Cue(cue.start, cue.end, text))
    return result


def _find_chinese_reference(directory: Path) -> Path | None:
    for name in ("zh.auto.vtt", "zh.manual.vtt", "zh.auto.srt", "zh.manual.srt"):
        path = directory / name
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def clean_subtitle_file(input_path: Path | str, output_path: Path | str | None = None, chinese_reference: Path | str | None = None) -> dict[str, Any]:
    source = Path(input_path)
    if not source.is_file():
        raise FileNotFoundError(f"英文字幕不存在: {source}")
    original_bytes = source.read_bytes()
    raw_cues = parse_webvtt(source) if source.suffix.lower() == ".vtt" else parse_srt(source)
    raw_srt = source.with_name(f"{source.stem}.raw.srt")
    clean_srt = Path(output_path) if output_path else source.with_name("en.clean.srt")
    write_srt(raw_srt, raw_cues)
    clean_english, stats = clean_cues(raw_cues)
    write_srt(clean_srt, clean_english)
    if source.read_bytes() != original_bytes:
        raise RuntimeError("原始 VTT 被意外修改")

    zh_reference = Path(chinese_reference) if chinese_reference else _find_chinese_reference(source.parent)
    zh_clean: Path | None = None
    zh_stats: dict[str, Any] = {"status": "missing", "input_cues": 0, "output_cues": 0}
    if zh_reference and zh_reference.is_file():
        zh_raw = parse_webvtt(zh_reference) if zh_reference.suffix.lower() == ".vtt" else parse_srt(zh_reference)
        zh_fragments, prepared_stats = prepare_fragments(zh_raw)
        aligned = align_translation_to_english(clean_english, zh_fragments)
        zh_clean = source.with_name("zh.youtube.clean.srt")
        write_srt(zh_clean, aligned)
        zh_stats = {"status": "success", "reference": str(zh_reference), "input_cues": len(zh_raw), "output_cues": len(aligned), **prepared_stats}

    return {
        **stats,
        "input": str(source), "raw_srt": str(raw_srt), "clean_srt": str(clean_srt),
        "zh_clean_srt": str(zh_clean) if zh_clean else "", "zh": zh_stats,
    }


def clean_subtitle_directory(directory: Path | str) -> dict[str, Any]:
    subtitle_dir = Path(directory)
    for name in ("en.auto.vtt", "en.manual.vtt"):
        source = subtitle_dir / name
        if source.is_file() and source.stat().st_size > 0:
            return clean_subtitle_file(source)
    raise FileNotFoundError(f"未找到 en.auto.vtt 或 en.manual.vtt: {subtitle_dir}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Clean rolling WebVTT captions into natural, non-overlapping SRT subtitles.")
    parser.add_argument("--input", required=True, type=Path, help="Input English VTT, preferably en.auto.vtt.")
    parser.add_argument("--output", type=Path, help="Optional output path; defaults to en.clean.srt beside the input.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = clean_subtitle_file(args.input, args.output)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"字幕清洗失败: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
