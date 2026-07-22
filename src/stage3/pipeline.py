from __future__ import annotations

import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .manifest import hash_config, sha256_file, utc_now, write_manifest
from .models import RawCue, SubtitleSegment, TranslationSegment, WordEvent
from .rolling_caption_cleaner import build_word_events
from .sentence_segmenter import segment_sentences
from .source_selector import asr_quality_score, assess_source, find_manual_source, find_youtube_source, select_source
from .subtitle_writer import atomic_write_json, read_srt, write_json, write_srt
from .timeline_builder import rebuild_timeline
from .translation_qc import estimate_translation, p0_quality, qc_text, translation_quality
from .translator_deepseek import DeepSeekTranslator, TranslationError, load_deepseek_settings
from .youtube_vtt_parser import parse_youtube_vtt


GLOSSARY_DEFAULT = {
    "fixed_terms": {"Roblox": "Roblox", "Minecraft": "Minecraft", "YouTube": "YouTube", "YT": "YT"},
    "do_not_translate": [],
    "preferred_translations": {},
    "notes": [],
}


def _raw_from_srt(path: Path) -> list[RawCue]:
    result: list[RawCue] = []
    for segment in read_srt(path):
        tokens = segment.text.split()
        duration = max(0.01, segment.duration)
        words = [
            WordEvent(
                token,
                segment.start + duration * index / max(1, len(tokens)),
                segment.start + duration * (index + 1) / max(1, len(tokens)),
                segment.start,
                segment.id,
            )
            for index, token in enumerate(tokens)
        ]
        result.append(RawCue(segment.id, segment.start, segment.end, segment.text, words))
    return result


def _media_metadata(video_dir: Path) -> dict[str, Any]:
    manifest_path = video_dir / "download_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    info_path = video_dir / "metadata" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.is_file() else {}
    return {
        "title": str(info.get("title") or manifest.get("title") or ""),
        "channel": str(info.get("channel") or info.get("uploader") or manifest.get("channel") or ""),
        "topic": str(info.get("categories", [""])[0] if info.get("categories") else ""),
        "duration": float(info.get("duration") or 0) or None,
    }


def _p0_text(report: dict[str, Any]) -> str:
    return qc_text(report)


def _translation_flags(item: TranslationSegment, glossary: dict[str, Any], config: dict[str, Any]) -> None:
    estimate_translation(item, config)
    for source, expected in glossary.get("fixed_terms", {}).items():
        if source.casefold() in item.source_text.casefold() and str(expected) not in item.translation:
            item.qc_flags.append(f"GLOSSARY_MISMATCH:{source}")
    if re.search(r"(进行一个|对于.*来说|值得注意的是)", item.translation):
        item.qc_flags.append("TRANSLATIONESE")


class Stage3Pipeline:
    def __init__(self, video_dir: Path | str, config: dict[str, Any]) -> None:
        self.video_dir = Path(video_dir).resolve()
        self.config = config
        self.stage3_dir = self.video_dir / "stage3"
        self.subtitle_dir = self.video_dir / "subtitles"
        self.translation_dir = self.video_dir / "translation"
        self.started_at = utc_now()
        self.manifest_path = self.video_dir / "stage3_manifest.json"
        self.manifest: dict[str, Any] = {
            "video_dir": str(self.video_dir),
            "selected_source": "",
            "source_hash": "",
            "config_hash": hash_config(config),
            "p0_status": "NOT_RUN",
            "p1_status": "NOT_RUN",
            "started_at": self.started_at,
            "finished_at": "",
            "clean_english_path": "",
            "clean_chinese_path": "",
            "segment_count": 0,
            "translation_count": 0,
            "api_model": load_deepseek_settings()["model"],
            "api_usage": {},
            "p0_qc": {},
            "p1_qc": {},
            "asr_status": "NOT_RUN",
            "asr_source_audio": "",
            "asr_source_audio_hash": "",
            "asr_model_path": "",
            "asr_device": "",
            "asr_compute_type": "",
            "asr_language_probability": 0.0,
            "asr_segment_count": 0,
            "asr_word_count": 0,
            "asr_processing_seconds": 0.0,
            "asr_realtime_factor": 0.0,
            "asr_qc": {},
            "selected_english_source": "",
            "selected_english_path": "",
            "errors": [],
        }
        if self.manifest_path.is_file():
            previous = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            for key in (
                "selected_source", "source_hash", "p0_status", "p1_status", "started_at",
                "clean_english_path", "clean_chinese_path", "segment_count", "translation_count",
                "api_model", "api_usage", "p0_qc", "p1_qc", "errors",
                "asr_status", "asr_source_audio", "asr_source_audio_hash", "asr_model_path",
                "asr_device", "asr_compute_type", "asr_language_probability", "asr_segment_count",
                "asr_word_count", "asr_processing_seconds", "asr_realtime_factor", "asr_qc",
                "selected_english_source", "selected_english_path",
            ):
                self.manifest[key] = previous.get(key, self.manifest[key])

    def _finish(self) -> None:
        self.manifest["finished_at"] = utc_now()
        write_manifest(self.video_dir, self.manifest)

    def run_p0(self, source_override: Path | None = None) -> dict[str, Any]:
        source = source_override or select_source(self.video_dir, self.config.get("source_priority"))
        if source is None:
            assessment = {"selected_source": "", "route": "NO_ENGLISH_SUBTITLE", "status": "NO_ENGLISH_SUBTITLE"}
            write_json(self.stage3_dir / "01_source_assessment.json", assessment)
            self.manifest["p0_status"] = "NO_ENGLISH_SUBTITLE"
            self.manifest["errors"].append("No English subtitle found")
            self._finish()
            return assessment
        before_hash = sha256_file(source)
        assessment = assess_source(source)
        write_json(self.stage3_dir / "01_source_assessment.json", assessment)
        cues = parse_youtube_vtt(source) if source.suffix.lower() == ".vtt" else _raw_from_srt(source)
        write_json(self.stage3_dir / "02_raw_cues.json", [cue.to_dict() for cue in cues])

        raw_segments = [SubtitleSegment(cue.id, cue.start, cue.end, cue.text, [cue.id], cue.words) for cue in cues if cue.text]
        write_srt(
            self.subtitle_dir / "en.source.raw.srt",
            raw_segments,
            width=int(self.config["english_max_chars_per_line"]),
            max_lines=int(self.config["max_lines"]),
        )
        events, cleaning_stats = build_word_events(cues)
        write_json(self.stage3_dir / "03_word_events.json", [event.to_dict() for event in events])
        segments = segment_sentences(events, self.config)
        metadata = _media_metadata(self.video_dir)
        segments = rebuild_timeline(segments, self.config, metadata["duration"])
        write_json(self.stage3_dir / "04_en_segments.json", [segment.to_dict() for segment in segments])
        clean_path = write_srt(
            self.subtitle_dir / "en.clean.srt",
            segments,
            width=int(self.config["english_max_chars_per_line"]),
            max_lines=int(self.config["max_lines"]),
        )
        report = p0_quality(len(cues), segments, cleaning_stats)
        write_json(self.stage3_dir / "05_p0_qc.json", report)
        (self.stage3_dir / "05_p0_qc.txt").write_text(_p0_text(report), encoding="utf-8")
        if sha256_file(source) != before_hash:
            raise RuntimeError(f"Original subtitle hash changed unexpectedly: {source}")
        self.manifest.update(
            selected_source=str(source),
            source_hash=before_hash,
            p0_status=report["status"],
            clean_english_path=str(clean_path),
            segment_count=len(segments),
            p0_qc=report,
        )
        self._finish()
        return report

    def _atomic_copy(self, source: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(source.read_bytes())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def _update_asr_manifest(self, result: dict[str, Any]) -> None:
        if result.get("status") == "NO_AUDIO_SOURCE":
            self.manifest["asr_status"] = "NO_AUDIO_SOURCE"
            return
        info, qc = result.get("info", {}), result.get("qc", {})
        self.manifest.update(
            asr_status=qc.get("status", result.get("status", "FAILED")),
            asr_source_audio=info.get("audio_path", ""),
            asr_source_audio_hash=sha256_file(info["audio_path"]) if info.get("audio_path") else "",
            asr_model_path=info.get("model_path", ""),
            asr_device=info.get("device", ""),
            asr_compute_type=info.get("compute_type", ""),
            asr_language_probability=info.get("language_probability", 0.0),
            asr_segment_count=info.get("segment_count", 0),
            asr_word_count=info.get("word_count", 0),
            asr_processing_seconds=info.get("processing_seconds", 0.0),
            asr_realtime_factor=info.get("realtime_factor", 0.0),
            asr_qc=qc,
        )

    def run_p2(
        self,
        *,
        subtitle_source: str = "auto",
        max_seconds: float | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        from .asr_faster_whisper import AsrError, run_faster_whisper_asr

        mode = subtitle_source.casefold()
        if mode not in {"auto", "manual", "youtube", "whisper"}:
            raise ValueError(f"Unsupported subtitle source: {subtitle_source}")
        manual_path, youtube_path = find_manual_source(self.video_dir), find_youtube_source(self.video_dir)
        manual_assessment = assess_source(manual_path) if manual_path else None
        youtube_assessment = assess_source(youtube_path) if youtube_path else None
        manual_score = manual_assessment["quality_score"] if manual_assessment else None
        youtube_score = youtube_assessment["quality_score"] if youtube_assessment else None
        selected_kind = ""
        selection_reason = ""
        if mode == "manual":
            if not manual_path:
                raise FileNotFoundError("No manual English subtitle was found")
            selected_kind, selection_reason = "manual", "User explicitly requested manual subtitles"
        elif mode == "youtube":
            if not youtube_path:
                raise FileNotFoundError("No YouTube automatic English subtitle was found")
            selected_kind, selection_reason = "youtube", "User explicitly requested YouTube subtitles"
        elif mode == "whisper":
            selected_kind, selection_reason = "whisper", "User explicitly requested local faster-whisper"
        elif manual_path and float(manual_score or 0) >= 70:
            selected_kind, selection_reason = "manual", "Manual subtitle quality score is at least 70"
        elif youtube_path and float(youtube_score or 0) >= 65:
            selected_kind, selection_reason = "youtube", "YouTube subtitle quality score is at least 65"
        else:
            selected_kind, selection_reason = "whisper", "No qualifying English subtitle; local ASR fallback selected"

        whisper_score: float | None = None
        asr_checkpoint_reused = False
        if selected_kind == "whisper":
            if not self.config.get("asr_enabled", True):
                raise AsrError("Local ASR is disabled in stage3_config.json")
            asr_result = run_faster_whisper_asr(
                self.video_dir,
                self.config,
                Path(__file__).resolve().parents[2],
                max_seconds=max_seconds,
                force=force,
            )
            self._update_asr_manifest(asr_result)
            asr_checkpoint_reused = bool(asr_result.get("skipped"))
            if asr_result.get("status") == "NO_AUDIO_SOURCE":
                comparison = {
                    "candidate_sources": [kind for kind, value in (("manual", manual_path), ("youtube", youtube_path)) if value],
                    "manual_score": manual_score,
                    "youtube_score": youtube_score,
                    "whisper_score": None,
                    "selected_source": "",
                    "selection_reason": "No audio source was found",
                    "user_override": mode != "auto",
                    "compared_at": utc_now(),
                }
                atomic_write_json(self.stage3_dir / "source_comparison.json", comparison)
                self._finish()
                return {"status": "NO_AUDIO_SOURCE", "source_comparison": comparison}
            whisper_score = asr_quality_score(asr_result.get("qc"))
            selected_path = self.subtitle_dir / "en.whisper.clean.srt"
            self.manifest["errors"] = [
                error for error in self.manifest.get("errors", []) if error != "No English subtitle found"
            ]
        else:
            selected_source_path = manual_path if selected_kind == "manual" else youtube_path
            self.run_p0(source_override=selected_source_path)
            selected_path = self.subtitle_dir / "en.clean.srt"
            existing_asr_qc = self.stage3_dir / "asr" / "asr_qc.json"
            if existing_asr_qc.is_file():
                whisper_score = asr_quality_score(json.loads(existing_asr_qc.read_text(encoding="utf-8")))

        unified_path = self._atomic_copy(selected_path, self.subtitle_dir / "en.selected.srt")
        comparison = {
            "candidate_sources": [
                {"source": "manual", "path": str(manual_path) if manual_path else "", "score": manual_score},
                {"source": "youtube", "path": str(youtube_path) if youtube_path else "", "score": youtube_score},
                {"source": "whisper", "path": str(self.subtitle_dir / "en.whisper.clean.srt"), "score": whisper_score},
            ],
            "manual_score": manual_score,
            "youtube_score": youtube_score,
            "whisper_score": whisper_score,
            "selected_source": selected_kind,
            "selected_path": str(unified_path),
            "selection_reason": selection_reason,
            "user_override": mode != "auto",
            "asr_checkpoint_reused": asr_checkpoint_reused,
            "compared_at": utc_now(),
        }
        atomic_write_json(self.stage3_dir / "source_comparison.json", comparison)
        self.manifest.update(selected_english_source=selected_kind, selected_english_path=str(unified_path))
        self._finish()
        return {"status": "SOURCE_SELECTED", "source_comparison": comparison, "selected_path": str(unified_path)}

    def _load_glossary(self) -> dict[str, Any]:
        path = self.translation_dir / "glossary.json"
        if not path.is_file():
            write_json(path, GLOSSARY_DEFAULT)
        return json.loads(path.read_text(encoding="utf-8"))

    def run_p1(
        self,
        *,
        allow_paid_api: bool = False,
        force: bool = False,
        polish_all: bool = False,
    ) -> dict[str, Any]:
        selected_path = self.subtitle_dir / "en.selected.srt"
        clean_path = selected_path if selected_path.is_file() else self.subtitle_dir / "en.clean.srt"
        if not clean_path.is_file():
            raise FileNotFoundError("subtitles/en.clean.srt does not exist; clean the English subtitles before translating")
        source = read_srt(clean_path)
        glossary = self._load_glossary()
        batch_count = math.ceil(len(source) / int(self.config["translation_batch_size"])) if source else 0
        settings = load_deepseek_settings()
        if not allow_paid_api:
            report = {
                "status": "DRY_RUN",
                "api_called": False,
                "api_key_configured": bool(settings["api_key"]),
                "model": settings["model"],
                "batch_count": batch_count,
                "estimated_translation_count": len(source),
                "input": str(clean_path),
            }
            write_json(self.translation_dir / "dry_run.json", report)
            self.manifest.update(p1_status="DRY_RUN", translation_count=0, p1_qc=report)
            self._finish()
            return report
        if not settings["api_key"]:
            raise TranslationError("--allow-paid-api requires DEEPSEEK_API_KEY in the environment")

        metadata = _media_metadata(self.video_dir)
        translator = DeepSeekTranslator(self.config, self.translation_dir)
        raw_map = translator.translate_all(source, source, glossary, metadata, pass_name="raw", force=force)
        translated = [
            TranslationSegment(item.id, item.start, item.end, item.text, raw_map.get(item.id, ""), raw_map.get(item.id, ""))
            for item in source
        ]
        for item in translated:
            _translation_flags(item, glossary, self.config)
        write_json(self.translation_dir / "translation_raw.json", [item.to_dict() for item in translated])
        write_srt(
            self.subtitle_dir / "zh.raw.srt",
            translated,
            translated=True,
            width=int(self.config["chinese_max_chars_per_line"]),
            max_lines=int(self.config["max_lines"]),
        )

        polish_ids = {item.id for item in translated if item.qc_flags} if not polish_all else {item.id for item in translated}
        if polish_ids:
            polish_targets = [item for item in source if item.id in polish_ids]
            polished_map = translator.translate_all(polish_targets, source, glossary, metadata, pass_name="polished", force=force)
            for item in translated:
                if item.id in polished_map:
                    item.translation = polished_map[item.id]
                    item.qc_flags = []
                    _translation_flags(item, glossary, self.config)
        write_json(self.translation_dir / "translation_polished.json", [item.to_dict() for item in translated])
        report = translation_quality(source, translated, self.config)
        write_json(self.translation_dir / "subtitle_qc.json", report)
        (self.translation_dir / "subtitle_qc.txt").write_text(qc_text(report), encoding="utf-8")
        usage = translator.usage_report()
        write_json(self.translation_dir / "api_usage.json", usage)
        clean_chinese = ""
        if not report["missing_ids"] and not report["extra_ids"] and not report["empty_translation_ids"]:
            clean_chinese = str(
                write_srt(
                    self.subtitle_dir / "zh.clean.srt",
                    translated,
                    translated=True,
                    width=int(self.config["chinese_max_chars_per_line"]),
                    max_lines=int(self.config["max_lines"]),
                )
            )
        self.manifest.update(
            p1_status=report["status"],
            clean_chinese_path=clean_chinese,
            translation_count=len(translated),
            api_model=settings["model"],
            api_usage=usage,
            p1_qc=report,
        )
        self._finish()
        return report
