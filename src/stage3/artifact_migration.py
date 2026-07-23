from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from .manifest import sha256_file, utc_now
from .subtitle_writer import atomic_write_json


MIGRATIONS = (
    ("subtitles/en.source.raw.srt", "subtitles/en.youtube.raw.srt"),
    ("subtitles/en.clean.srt", "subtitles/en.youtube.clean.srt"),
    ("stage3/01_source_assessment.json", "stage3/youtube/source_assessment.json"),
    ("stage3/02_raw_cues.json", "stage3/youtube/raw_cues.json"),
    ("stage3/03_word_events.json", "stage3/youtube/word_events.json"),
    ("stage3/04_en_segments.json", "stage3/youtube/clean_segments.json"),
    ("stage3/05_p0_qc.json", "stage3/youtube/qc.json"),
    ("stage3/asr/asr_info.json", "stage3/whisper/asr_info.json"),
    ("stage3/asr/asr_raw_segments.json", "stage3/whisper/raw_segments.json"),
    ("stage3/asr/asr_words.json", "stage3/whisper/words.json"),
    ("stage3/asr/asr_clean_segments.json", "stage3/whisper/clean_segments.json"),
    ("stage3/asr/asr_qc.json", "stage3/whisper/qc.json"),
    ("stage3/asr/asr_qc.txt", "stage3/whisper/qc.txt"),
    ("stage3/asr/asr_checkpoint.json", "stage3/whisper/asr_checkpoint.json"),
    ("stage3/source_comparison.json", "stage3/selection/comparison.json"),
    ("translation/glossary.json", "stage3/translation/glossary.json"),
    ("translation/translation_raw.json", "stage3/translation/translation_raw.json"),
    ("translation/translation_polished.json", "stage3/translation/translation_polished.json"),
    ("translation/subtitle_qc.json", "stage3/translation/translation_qc.json"),
    ("translation/subtitle_qc.txt", "stage3/translation/translation_qc.txt"),
    ("translation/api_usage.json", "stage3/translation/api_usage.json"),
)


def atomic_copy(source: Path, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(source.read_bytes())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def migrate_legacy_artifacts(video_dir: Path | str) -> list[dict[str, Any]]:
    root = Path(video_dir).resolve()
    report_path = root / "stage3" / "migrations.json"
    existing: list[dict[str, Any]] = []
    if report_path.is_file():
        try:
            loaded = json.loads(report_path.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, list) else []
        except (OSError, ValueError):
            existing = []
    known = {(item.get("migrated_from"), item.get("migrated_to")) for item in existing}
    changed = False
    pairs = list(MIGRATIONS)
    for folder in ("checkpoints", "responses"):
        legacy_folder = root / "translation" / folder
        if legacy_folder.is_dir():
            pairs.extend(
                (
                    str(path.relative_to(root)).replace("\\", "/"),
                    str((Path("stage3/translation") / folder / path.relative_to(legacy_folder))).replace("\\", "/"),
                )
                for path in legacy_folder.rglob("*")
                if path.is_file()
            )
    for old_relative, new_relative in pairs:
        source, destination = root / old_relative, root / new_relative
        key = (str(source), str(destination))
        if destination.is_file() or not source.is_file():
            continue
        atomic_copy(source, destination)
        if key not in known:
            existing.append(
                {
                    "migrated_from": str(source),
                    "migrated_to": str(destination),
                    "migration_time": utc_now(),
                    "source_sha256": sha256_file(source),
                    "destination_sha256": sha256_file(destination),
                }
            )
            known.add(key)
        changed = True
    if changed:
        atomic_write_json(report_path, existing)
    return existing
