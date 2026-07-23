from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from .asr_qc import assess_asr_quality
from .artifact_migration import migrate_legacy_artifacts
from .cuda_runtime import configure_cuda_runtime
from .manifest import hash_config, sha256_file, utc_now
from .models import SubtitleSegment, WordEvent
from .sentence_segmenter import segment_sentences
from .subtitle_writer import atomic_write_json, atomic_write_srt
from .timeline_builder import rebuild_timeline
from .translation_qc import qc_text


LOGGER = logging.getLogger(__name__)
REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.json")
AUDIO_PRIORITY = (
    Path("audio/source_audio.wav"),
    Path("audio/source_audio.m4a"),
    Path("audio/source_audio.mp3"),
)
VIDEO_PRIORITY = (
    Path("video/source.mp4"),
    Path("video/source.mkv"),
    Path("video/source.webm"),
)


class AsrError(RuntimeError):
    pass


def normalize_asr_text(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", value)


def resolve_local_model(config: dict[str, Any], project_root: Path | str) -> Path:
    configured = str(config.get("asr_model_path", "")).strip()
    candidate = Path(configured)
    if not configured or (not candidate.is_absolute() and len(candidate.parts) == 1):
        raise AsrError("LOCAL_ASR_MODEL_INCOMPLETE: asr_model_path must point to a local model directory")
    path = candidate if candidate.is_absolute() else Path(project_root) / candidate
    path = path.resolve()
    missing = [name for name in REQUIRED_MODEL_FILES if not (path / name).is_file()]
    if missing:
        raise AsrError("LOCAL_ASR_MODEL_INCOMPLETE: " + ", ".join(missing))
    return path


def model_directory_identifier(model_path: Path) -> str:
    entries = []
    for name in REQUIRED_MODEL_FILES:
        stat = (model_path / name).stat()
        entries.append({"name": name, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
    payload = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def select_audio_source(video_dir: Path | str) -> Path | None:
    root = Path(video_dir)
    for relative in AUDIO_PRIORITY + VIDEO_PRIORITY:
        candidate = root / relative
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    video_dir_path = root / "video"
    if video_dir_path.is_dir():
        for pattern in ("*.mp4", "*.mkv", "*.webm", "*.mov"):
            candidates = sorted(path for path in video_dir_path.glob(pattern) if path.stat().st_size > 0)
            if candidates:
                return candidates[0]
    return None


def _value(item: Any, name: str, default: Any = None) -> Any:
    return getattr(item, name, default)


def _float_or_none(value: Any) -> float | None:
    return None if value is None else float(value)


def _words_from_segment(segment: Any, segment_id: int) -> tuple[list[dict[str, Any]], list[WordEvent]]:
    raw_words: list[dict[str, Any]] = []
    events: list[WordEvent] = []
    model_words = list(_value(segment, "words", None) or [])
    usable = [word for word in model_words if _value(word, "start") is not None and _value(word, "end") is not None]
    if usable and len(usable) == len(model_words):
        for word in model_words:
            text = normalize_asr_text(str(_value(word, "word", "")))
            start, end = _float_or_none(_value(word, "start")), _float_or_none(_value(word, "end"))
            probability = _float_or_none(_value(word, "probability"))
            raw_words.append(
                {
                    "word": text,
                    "start": start,
                    "end": end,
                    "probability": probability,
                    "source_segment_id": segment_id,
                    "timestamps_approximated": start is None or end is None,
                }
            )
            if text and start is not None and end is not None and end > start:
                events.append(WordEvent(text, start, end, start, segment_id, probability, segment_id))
        if events:
            return raw_words, events

    text_tokens = normalize_asr_text(str(_value(segment, "text", ""))).split()
    start = float(_value(segment, "start", 0.0) or 0.0)
    end = max(start + 0.01, float(_value(segment, "end", start + 0.01) or start + 0.01))
    duration = end - start
    for index, token in enumerate(text_tokens):
        word_start = start + duration * index / max(1, len(text_tokens))
        word_end = start + duration * (index + 1) / max(1, len(text_tokens))
        raw_words.append(
            {
                "word": token,
                "start": word_start,
                "end": word_end,
                "probability": None,
                "source_segment_id": segment_id,
                "timestamps_approximated": True,
            }
        )
        events.append(WordEvent(token, word_start, word_end, word_start, segment_id, None, segment_id))
    return raw_words, events


def convert_asr_segments(segments: list[Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[WordEvent]]:
    raw_segments: list[dict[str, Any]] = []
    raw_words: list[dict[str, Any]] = []
    events: list[WordEvent] = []
    for identifier, segment in enumerate(segments, 1):
        words, word_events = _words_from_segment(segment, identifier)
        raw_words.extend(words)
        events.extend(word_events)
        raw_segments.append(
            {
                "id": identifier,
                "start": float(_value(segment, "start", 0.0) or 0.0),
                "end": float(_value(segment, "end", 0.0) or 0.0),
                "text": normalize_asr_text(str(_value(segment, "text", ""))),
                "avg_logprob": _float_or_none(_value(segment, "avg_logprob")),
                "no_speech_prob": _float_or_none(_value(segment, "no_speech_prob")),
                "compression_ratio": _float_or_none(_value(segment, "compression_ratio")),
                "temperature": _float_or_none(_value(segment, "temperature")),
                "words": words,
            }
        )
    return raw_segments, raw_words, events


def _load_model(model_path: Path, config: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    runtime = configure_cuda_runtime(require_dlls=str(config["asr_device"]).casefold() == "cuda")
    import ctranslate2
    from faster_whisper import WhisperModel

    device = str(config["asr_device"])
    runtime["cuda_device_count"] = int(ctranslate2.get_cuda_device_count()) if device.casefold() == "cuda" else 0
    runtime["supported_compute_types"] = sorted(ctranslate2.get_supported_compute_types(device))
    model = WhisperModel(str(model_path), device=device, compute_type=str(config["asr_compute_type"]))
    return model, runtime


def _asr_config(config: dict[str, Any], max_seconds: float | None) -> dict[str, Any]:
    result = {key: value for key, value in config.items() if key.startswith("asr_")}
    result["asr_max_seconds"] = max_seconds
    return result


def _checkpoint_is_reusable(checkpoint: dict[str, Any], audio_hash: str, model_id: str, config_hash: str) -> bool:
    return bool(
        checkpoint.get("completed")
        and checkpoint.get("status") == "ASR_COMPLETED"
        and checkpoint.get("source_audio_hash") == audio_hash
        and checkpoint.get("model_identifier") == model_id
        and checkpoint.get("config_hash") == config_hash
        and all(Path(path).is_file() for path in checkpoint.get("output_paths", []))
    )


def run_faster_whisper_asr(
    video_dir: Path | str,
    config: dict[str, Any],
    project_root: Path | str,
    *,
    max_seconds: float | None = None,
    force: bool = False,
    model_factory: Callable[[Path, dict[str, Any]], tuple[Any, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    root = Path(video_dir).resolve()
    migrate_legacy_artifacts(root)
    asr_dir = root / "stage3" / "whisper"
    legacy_asr_dir = root / "stage3" / "asr"
    checkpoint_path = asr_dir / "asr_checkpoint.json"
    audio_path = select_audio_source(root)
    if audio_path is None:
        result = {"status": "NO_AUDIO_SOURCE", "completed": False, "error": "No audio or video source was found"}
        atomic_write_json(checkpoint_path, result)
        atomic_write_json(legacy_asr_dir / "asr_checkpoint.json", result)
        return result

    model_path = resolve_local_model(config, project_root)
    audio_hash = sha256_file(audio_path)
    model_id = model_directory_identifier(model_path)
    config_hash = hash_config(_asr_config(config, max_seconds))
    if not force and checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if _checkpoint_is_reusable(checkpoint, audio_hash, model_id, config_hash):
            info = json.loads((asr_dir / "asr_info.json").read_text(encoding="utf-8"))
            qc = json.loads((asr_dir / "qc.json").read_text(encoding="utf-8"))
            return {"status": "ASR_COMPLETED", "completed": True, "skipped": True, "info": info, "qc": qc}

    started_at = utc_now()
    checkpoint: dict[str, Any] = {
        "source_audio_hash": audio_hash,
        "model_identifier": model_id,
        "config_hash": config_hash,
        "status": "RUNNING",
        "completed": False,
        "segment_count": 0,
        "output_paths": [],
        "error": "",
        "started_at": started_at,
        "finished_at": "",
    }
    atomic_write_json(checkpoint_path, checkpoint)
    before_hash = audio_hash
    try:
        LOGGER.info("从本地 large-v3 模型加载英文语音识别；不会联网下载模型。")
        factory = model_factory or _load_model
        model, runtime = factory(model_path, config)
        options: dict[str, Any] = {
            "language": str(config["asr_language"]),
            "beam_size": int(config["asr_beam_size"]),
            "vad_filter": bool(config["asr_vad_filter"]),
            "vad_parameters": {
                "min_silence_duration_ms": int(config["asr_min_silence_duration_ms"]),
                "speech_pad_ms": int(config["asr_speech_pad_ms"]),
            },
            "word_timestamps": bool(config["asr_word_timestamps"]),
            "condition_on_previous_text": bool(config["asr_condition_on_previous_text"]),
            "temperature": float(config["asr_temperature"]),
            "log_progress": bool(config["asr_log_progress"]),
        }
        if max_seconds is not None:
            if max_seconds <= 0:
                raise AsrError("--asr-max-seconds must be greater than zero")
            options["clip_timestamps"] = [0.0, float(max_seconds)]
        started_clock = time.perf_counter()
        segment_generator, detected_info = model.transcribe(str(audio_path), **options)
        model_segments = list(segment_generator)
        processing_seconds = time.perf_counter() - started_clock
        raw_segments, raw_words, events = convert_asr_segments(model_segments)
        detected_duration = float(_value(detected_info, "duration", 0.0) or 0.0)
        audio_duration = min(detected_duration, float(max_seconds)) if max_seconds is not None and detected_duration else detected_duration
        if not audio_duration:
            audio_duration = max((item["end"] for item in raw_segments), default=float(max_seconds or 0.0))
        clean_segments = rebuild_timeline(segment_sentences(events, config), config, audio_duration or None)
        raw_srt_segments = [
            SubtitleSegment(item["id"], item["start"], item["end"], item["text"], [item["id"]])
            for item in raw_segments
            if item["text"]
        ]
        qc = assess_asr_quality(
            raw_segments,
            clean_segments,
            raw_words,
            audio_duration=audio_duration,
            max_cps=float(config["english_max_cps"]),
        )
        finished_at = utc_now()
        info = {
            "model_path": str(model_path),
            "model_loaded_from_local_path": True,
            "network_request_attempted": False,
            "device": str(config["asr_device"]),
            "compute_type": str(config["asr_compute_type"]),
            "language": str(_value(detected_info, "language", config["asr_language"])),
            "language_probability": float(_value(detected_info, "language_probability", 0.0) or 0.0),
            "audio_path": str(audio_path),
            "audio_duration": round(audio_duration, 3),
            "processing_seconds": round(processing_seconds, 3),
            "realtime_factor": round(processing_seconds / max(0.001, audio_duration), 6),
            "segment_count": len(raw_segments),
            "clean_segment_count": len(clean_segments),
            "word_count": len(raw_words),
            "vad_enabled": bool(config["asr_vad_filter"]),
            "word_timestamps_enabled": bool(config["asr_word_timestamps"]),
            "max_seconds": max_seconds,
            "started_at": started_at,
            "finished_at": finished_at,
            **runtime,
        }
        outputs = [
            atomic_write_json(asr_dir / "asr_info.json", info),
            atomic_write_json(asr_dir / "raw_segments.json", raw_segments),
            atomic_write_json(asr_dir / "words.json", raw_words),
            atomic_write_srt(root / "subtitles" / "en.whisper.raw.srt", raw_srt_segments, width=int(config["english_max_chars_per_line"]), max_lines=int(config["max_lines"])),
            atomic_write_json(asr_dir / "clean_segments.json", [item.to_dict() for item in clean_segments]),
            atomic_write_srt(root / "subtitles" / "en.whisper.clean.srt", clean_segments, width=int(config["english_max_chars_per_line"]), max_lines=int(config["max_lines"])),
            atomic_write_json(asr_dir / "qc.json", qc),
        ]
        qc_text_path = asr_dir / "qc.txt"
        temporary_qc = qc_text_path.with_name(f".{qc_text_path.name}.tmp")
        temporary_qc.write_text(qc_text(qc), encoding="utf-8")
        temporary_qc.replace(qc_text_path)
        outputs.append(qc_text_path)
        atomic_write_json(legacy_asr_dir / "asr_info.json", info)
        atomic_write_json(legacy_asr_dir / "asr_raw_segments.json", raw_segments)
        atomic_write_json(legacy_asr_dir / "asr_words.json", raw_words)
        atomic_write_json(legacy_asr_dir / "asr_clean_segments.json", [item.to_dict() for item in clean_segments])
        atomic_write_json(legacy_asr_dir / "asr_qc.json", qc)
        if sha256_file(audio_path) != before_hash:
            raise AsrError("SOURCE_AUDIO_HASH_CHANGED")
        checkpoint.update(
            status="ASR_COMPLETED",
            completed=True,
            segment_count=len(raw_segments),
            output_paths=[str(path) for path in outputs],
            finished_at=finished_at,
        )
        atomic_write_json(checkpoint_path, checkpoint)
        atomic_write_json(legacy_asr_dir / "asr_checkpoint.json", checkpoint)
        return {"status": "ASR_COMPLETED", "completed": True, "skipped": False, "info": info, "qc": qc}
    except Exception as exc:
        checkpoint.update(status="FAILED", completed=False, error=str(exc), finished_at=utc_now())
        atomic_write_json(checkpoint_path, checkpoint)
        atomic_write_json(legacy_asr_dir / "asr_checkpoint.json", checkpoint)
        raise
