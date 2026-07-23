from __future__ import annotations

import csv
import html
import io
import json
import os
import uuid
from pathlib import Path
from typing import Any

from .manifest import sha256_file, utc_now
from .models import TranslationSegment
from .subtitle_writer import atomic_write_json, atomic_write_srt, format_timestamp, read_srt


REVIEW_COLUMNS = (
    "id", "start", "end", "duration", "english", "chinese_raw", "chinese_clean",
    "reviewed_translation", "qc_flags", "source", "reviewer_note",
)


def _atomic_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8", newline="")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _by_id(path: Path) -> dict[int, Any]:
    return {item.id: item for item in read_srt(path)} if path.is_file() else {}


def _translation_flags(root: Path) -> dict[int, list[str]]:
    flags: dict[int, list[str]] = {}

    def add(identifier: Any, flag: str) -> None:
        try:
            key = int(identifier)
        except (TypeError, ValueError):
            return
        if flag and flag not in flags.setdefault(key, []):
            flags[key].append(flag)

    polished_path = root / "stage3" / "translation" / "translation_polished.json"
    if polished_path.is_file():
        try:
            for row in json.loads(polished_path.read_text(encoding="utf-8")):
                for flag in row.get("qc_flags", []):
                    add(row.get("id"), str(flag))
        except (OSError, ValueError, TypeError, KeyError):
            pass

    qc_path = root / "stage3" / "translation" / "translation_qc.json"
    if qc_path.is_file():
        try:
            report = json.loads(qc_path.read_text(encoding="utf-8"))
            fields = {
                "missing_ids": "MISSING_ID",
                "empty_translation_ids": "EMPTY_TRANSLATION",
                "adjacent_duplicate_ids": "DUPLICATE_TRANSLATION",
                "illegal_control_character_ids": "INVALID_CONTROL_CHARACTER",
                "unnatural_punctuation_ids": "UNNATURAL_PUNCTUATION",
            }
            for field, flag in fields.items():
                for identifier in report.get(field, []):
                    add(identifier, flag)
            for row in report.get("segments", report.get("items", [])):
                for flag in row.get("qc_flags", []):
                    add(row.get("id"), str(flag))
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return flags


def export_review(video_dir: Path | str) -> dict[str, Any]:
    root = Path(video_dir).resolve()
    selected_path = root / "subtitles" / "en.selected.srt"
    if not selected_path.is_file():
        raise FileNotFoundError("EN_SELECTED_SUBTITLE_NOT_FOUND: run youtube,whisper,select first")
    selected = read_srt(selected_path)
    raw = _by_id(root / "subtitles" / "zh.raw.srt")
    clean = _by_id(root / "subtitles" / "zh.clean.srt")
    flags = _translation_flags(root)
    selection_path = root / "stage3" / "selection" / "selection_report.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8")) if selection_path.is_file() else {}
    source = str(selection.get("selected_source") or "")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=REVIEW_COLUMNS, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for item in selected:
        raw_text = raw[item.id].text if item.id in raw else ""
        clean_text = clean[item.id].text if item.id in clean else raw_text
        writer.writerow(
            {
                "id": item.id,
                "start": format_timestamp(item.start),
                "end": format_timestamp(item.end),
                "duration": f"{item.duration:.3f}",
                "english": item.text,
                "chinese_raw": raw_text,
                "chinese_clean": clean_text,
                "reviewed_translation": clean_text,
                "qc_flags": "|".join(flags.get(item.id, [])),
                "source": source,
                "reviewer_note": "",
            }
        )
    review_dir = root / "stage3" / "review"
    tsv_path = _atomic_text(review_dir / "review_export.tsv", output.getvalue())
    html_path = _write_review_html(root, selected, raw, clean, flags, selection)
    report = {
        "status": "REVIEW_EXPORTED",
        "exported_at": utc_now(),
        "segment_count": len(selected),
        "source_hash": sha256_file(selected_path),
        "review_file": str(tsv_path),
        "html_file": str(html_path),
        "reviewed_srt_created": False,
    }
    atomic_write_json(review_dir / "review_manifest.json", report)
    return report


def generate_review_html(video_dir: Path | str) -> Path:
    root = Path(video_dir).resolve()
    selected_path = root / "subtitles" / "en.selected.srt"
    if not selected_path.is_file():
        raise FileNotFoundError("EN_SELECTED_SUBTITLE_NOT_FOUND: run youtube,whisper,select first")
    selection_path = root / "stage3" / "selection" / "selection_report.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8")) if selection_path.is_file() else {}
    selected = read_srt(selected_path)
    raw = _by_id(root / "subtitles" / "zh.raw.srt")
    clean = _by_id(root / "subtitles" / "zh.clean.srt")
    flags = _translation_flags(root)
    return _write_review_html(root, selected, raw, clean, flags, selection)


def import_review(
    video_dir: Path | str,
    review_file: Path | str,
    *,
    overwrite_reviewed: bool = False,
) -> dict[str, Any]:
    root = Path(video_dir).resolve()
    selected_path = root / "subtitles" / "en.selected.srt"
    if not selected_path.is_file():
        raise FileNotFoundError("EN_SELECTED_SUBTITLE_NOT_FOUND: run youtube,whisper,select first")
    input_path = Path(review_file)
    if not input_path.is_absolute():
        input_path = root / input_path
    reviewed_path = root / "subtitles" / "zh.reviewed.srt"
    report_path = root / "stage3" / "review" / "review_import_report.json"
    errors: list[str] = []
    if reviewed_path.exists() and not overwrite_reviewed:
        errors.append("REVIEWED_SUBTITLE_ALREADY_EXISTS")
    selected = read_srt(selected_path)
    selected_by_id = {item.id: item for item in selected}
    rows: list[dict[str, str]] = []
    try:
        with input_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if not reader.fieldnames or not set(REVIEW_COLUMNS).issubset(reader.fieldnames):
                errors.append("INVALID_REVIEW_COLUMNS")
            else:
                rows = [dict(row) for row in reader]
    except (OSError, UnicodeError) as exc:
        errors.append(f"REVIEW_FILE_READ_ERROR:{exc}")
    ids: list[int] = []
    translations: dict[int, str] = {}
    for row in rows:
        try:
            identifier = int(row["id"])
        except (TypeError, ValueError):
            errors.append("INVALID_ID")
            continue
        ids.append(identifier)
        original = selected_by_id.get(identifier)
        if original is None:
            errors.append(f"EXTRA_ID:{identifier}")
            continue
        if row.get("english", "") != original.text:
            errors.append(f"ENGLISH_CHANGED:{identifier}")
        if row.get("start", "") != format_timestamp(original.start) or row.get("end", "") != format_timestamp(original.end):
            errors.append(f"TIMELINE_CHANGED:{identifier}")
        translation = str(row.get("reviewed_translation", "")).strip()
        if not translation:
            errors.append(f"EMPTY_REVIEWED_TRANSLATION:{identifier}")
        translations[identifier] = translation
    if len(ids) != len(set(ids)):
        errors.append("DUPLICATE_IDS")
    missing = sorted(set(selected_by_id) - set(ids))
    if missing:
        errors.append(f"MISSING_IDS:{missing}")
    status = "REVIEW_IMPORT_FAILED" if errors else "REVIEWED"
    report: dict[str, Any] = {
        "status": status,
        "review_file": str(input_path),
        "review_file_hash": sha256_file(input_path) if input_path.is_file() else "",
        "selected_source_hash": sha256_file(selected_path),
        "segment_count": len(selected),
        "errors": errors,
        "imported_at": utc_now(),
        "reviewed_path": "",
        "reviewed_hash": "",
    }
    if not errors:
        output_segments = [
            TranslationSegment(item.id, item.start, item.end, item.text, translations[item.id], translations[item.id], manually_reviewed=True)
            for item in selected
        ]
        atomic_write_srt(reviewed_path, output_segments, translated=True, width=20, max_lines=2)
        report["reviewed_path"] = str(reviewed_path)
        report["reviewed_hash"] = sha256_file(reviewed_path)
    atomic_write_json(report_path, report)
    return report


def _write_review_html(
    root: Path,
    selected: list[Any],
    raw: dict[int, Any],
    clean: dict[int, Any],
    flags: dict[int, list[str]],
    selection: dict[str, Any],
) -> Path:
    youtube_path = root / "subtitles" / "en.youtube.clean.srt"
    whisper_path = root / "subtitles" / "en.whisper.clean.srt"
    youtube = read_srt(youtube_path) if youtube_path.is_file() else []
    whisper = read_srt(whisper_path) if whisper_path.is_file() else []
    low_confidence_words: list[tuple[float, float]] = []
    words_path = root / "stage3" / "whisper" / "words.json"
    if words_path.is_file():
        try:
            for word in json.loads(words_path.read_text(encoding="utf-8")):
                probability = word.get("probability")
                if probability is not None and float(probability) < 0.65:
                    low_confidence_words.append((float(word["start"]), float(word["end"])))
        except (OSError, ValueError, TypeError, KeyError):
            low_confidence_words = []

    def text_at_time(segments: list[Any], start: float, end: float) -> str:
        values: list[str] = []
        for segment in segments:
            if min(end, segment.end) <= max(start, segment.start):
                continue
            if not values or values[-1] != segment.text:
                values.append(segment.text)
        return " ".join(values)

    youtube_score = (selection.get("youtube") or {}).get("final_score")
    whisper_score = (selection.get("whisper") or {}).get("final_score")
    rows = []
    for item in selected:
        youtube_text = text_at_time(youtube, item.start, item.end)
        whisper_text = text_at_time(whisper, item.start, item.end)
        difference = SequenceMatcherRatio(youtube_text, whisper_text)
        row_flags = flags.get(item.id, [])
        low_confidence = (
            any("LOW_CONFIDENCE" in flag for flag in row_flags)
            or any(min(item.end, end) > max(item.start, start) for start, end in low_confidence_words)
        )
        too_long = any(
            flag in {"TTS_TOO_LONG", "TOO_LONG_FOR_DURATION", "CHINESE_TOO_LONG"}
            for flag in row_flags
        )
        rows.append(
            "<tr "
            f"data-id='{item.id}' data-qc='{int(bool(row_flags))}' data-difference='{int(difference < 0.6)}' "
            f"data-low-confidence='{int(low_confidence)}' data-too-long='{int(too_long)}'>"
            f"<td><a href='file:///{html.escape(str(root / 'video' / 'source.mp4'))}#t={item.start:.3f}'>{item.id}</a></td>"
            f"<td>{html.escape(format_timestamp(item.start))}</td>"
            f"<td>{html.escape(youtube_text)}</td><td>{html.escape(whisper_text)}</td>"
            f"<td>{html.escape(item.text)}</td>"
            f"<td>{html.escape(raw[item.id].text if item.id in raw else '')}</td>"
            f"<td>{html.escape(clean[item.id].text if item.id in clean else '')}</td>"
            f"<td>{html.escape('|'.join(row_flags))}</td></tr>"
        )
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Stage 3 字幕审核</title>
<style>body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:20px}}button,input{{margin:4px;padding:6px}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border:1px solid #ccc;padding:6px;vertical-align:top}}th{{position:sticky;top:0;background:#eee}}.hidden{{display:none}}</style></head>
<body><h1>阶段三只读字幕审核</h1>
<p>YouTube 评分：{youtube_score}；Whisper 评分：{whisper_score}；选择原因：{html.escape(str(selection.get("selection_reason", "")))}</p>
<div><button data-filter="all">全部</button><button data-filter="qc">只看 QC 失败</button>
<button data-filter="difference">只看双源差异</button><button data-filter="low-confidence">只看低置信度</button>
<button data-filter="too-long">只看中文字幕过长</button><input id="search" placeholder="搜索关键词或 ID"></div>
<table><thead><tr><th>ID</th><th>时间</th><th>YouTube clean</th><th>Whisper clean</th><th>Selected</th><th>zh.raw</th><th>zh.clean</th><th>QC</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
<script>
const rows=[...document.querySelectorAll('tbody tr')];let filter='all';
function apply(){{const q=document.querySelector('#search').value.toLowerCase();rows.forEach(r=>{{const match=filter==='all'||r.getAttribute(`data-${{filter}}`)==='1';r.classList.toggle('hidden',!match||!r.innerText.toLowerCase().includes(q));}})}}
document.querySelectorAll('button').forEach(b=>b.onclick=()=>{{filter=b.dataset.filter;apply();}});
document.querySelector('#search').oninput=apply;
</script></body></html>"""
    return _atomic_text(root / "stage3" / "stage3_review.html", page)


def SequenceMatcherRatio(left: str, right: str) -> float:
    from difflib import SequenceMatcher

    if not left and not right:
        return 1.0
    return SequenceMatcher(None, left.casefold(), right.casefold()).ratio()
