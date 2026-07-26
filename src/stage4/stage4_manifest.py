from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def atomic_write_text(path: Path | str, value: str, *, encoding: str = "utf-8") -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep the temporary basename short. Download task directories contain the
    # full video title and can otherwise cross the legacy Windows MAX_PATH limit.
    temporary = destination.with_name(f".tmp-{uuid.uuid4().hex[:8]}{destination.suffix}")
    try:
        temporary.write_text(value, encoding=encoding)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def atomic_write_json(path: Path | str, value: Any) -> Path:
    return atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


def load_manifest(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        return {}
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def output_matches_checkpoint(
    checkpoint: dict[str, Any] | None,
    *,
    fingerprint: str,
    output_path: Path,
) -> bool:
    if not checkpoint or checkpoint.get("fingerprint") != fingerprint:
        return False
    if checkpoint.get("qc_status") != "QC_PASSED" or not output_path.is_file():
        return False
    expected = str(checkpoint.get("output_hash") or "")
    return bool(expected) and sha256_file(output_path) == expected


def empty_manifest(video_dir: Path, mode: str) -> dict[str, Any]:
    return {
        "video_dir": str(video_dir),
        "source_video_path": "",
        "source_video_hash": "",
        "source_video_probe": {},
        "english_subtitle_path": "",
        "english_subtitle_hash": "",
        "chinese_subtitle_path": "",
        "chinese_subtitle_hash": "",
        "chinese_subtitle_source": "",
        "chinese_subtitle_reviewed": False,
        "chinese_subtitle_auto_selected": False,
        "chinese_subtitle_selection_reason": "",
        "chinese_subtitle_selection_score": None,
        "chinese_selection_report_path": "",
        "bilingual_ass_path": "",
        "bilingual_ass_hash": "",
        "subtitle_segment_count": 0,
        "subtitle_style_config_hash": "",
        "output_mode": mode,
        "softsub_output_path": "",
        "softsub_output_hash": "",
        "hardsub_output_path": "",
        "hardsub_output_hash": "",
        "video_encoder": "",
        "audio_mode": "",
        "audio_transcoded": False,
        "original_audio_codec": [],
        "output_audio_codec": [],
        "original_duration": 0.0,
        "output_duration": 0.0,
        "ffmpeg_version": "",
        "ffprobe_version": "",
        "started_at": utc_now(),
        "finished_at": "",
        "processing_seconds": 0.0,
        "status": "RUNNING",
        "qc_status": "",
        "warnings": [],
        "errors": [],
        "checkpoints": {},
    }
