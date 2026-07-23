from __future__ import annotations

import json
import math
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .artifact_migration import atomic_copy, migrate_legacy_artifacts
from .manifest import hash_config, sha256_file, utc_now, write_manifest
from .models import RawCue, SubtitleSegment, TranslationSegment, WordEvent
from .review_workflow import export_review, generate_review_html, import_review
from .rolling_caption_cleaner import build_word_events
from .sentence_segmenter import segment_sentences
from .source_selector import assess_source, select_source
from .subtitle_scoring import score_subtitle, subtitle_agreement
from .subtitle_selector import SELECTION_POLICY_VERSION, choose_source, write_selection_outputs
from .subtitle_writer import atomic_write_json, atomic_write_srt, read_srt
from .timeline_builder import rebuild_timeline
from .translation_qc import estimate_translation, p0_quality, qc_text, translation_quality
from .translator_deepseek import (
    PROMPT_VERSION,
    DeepSeekTranslator,
    TranslationError,
    load_deepseek_settings,
)
from .youtube_vtt_parser import parse_youtube_vtt


GLOSSARY_DEFAULT = {
    "fixed_terms": {"Roblox": "Roblox", "Minecraft": "Minecraft", "YouTube": "YouTube", "YT": "YT"},
    "do_not_translate": [],
    "preferred_translations": {},
    "notes": [],
}
YOUTUBE_CLEANER_VERSION = "stage3-youtube-clean-v2"
TRANSLATION_STAGE_VERSION = "stage3-translation-stage-v2"


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
                None,
                segment.id,
            )
            for index, token in enumerate(tokens)
        ]
        result.append(RawCue(segment.id, segment.start, segment.end, segment.text, words))
    return result


def _media_metadata(video_dir: Path) -> dict[str, Any]:
    manifest_path = video_dir / "download_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig")) if manifest_path.is_file() else {}
    info_path = video_dir / "metadata" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8-sig")) if info_path.is_file() else {}
    return {
        "title": str(info.get("title") or manifest.get("title") or ""),
        "channel": str(info.get("channel") or info.get("uploader") or manifest.get("channel") or ""),
        "topic": str(info.get("categories", [""])[0] if info.get("categories") else ""),
        "duration": float(info.get("duration") or 0) or None,
    }


def _atomic_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _translation_flags(item: TranslationSegment, glossary: dict[str, Any], config: dict[str, Any]) -> None:
    estimate_translation(item, config)
    for source, expected in glossary.get("fixed_terms", {}).items():
        if source.casefold() in item.source_text.casefold() and str(expected) not in item.translation:
            item.qc_flags.append(f"GLOSSARY_MISMATCH:{source}")
    source_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", item.source_text))
    translated_numbers = set(re.findall(r"\d+(?:[.,]\d+)?", item.translation))
    if source_numbers - translated_numbers:
        item.qc_flags.append("NUMBER_MISMATCH")
    source_has_negation = bool(re.search(r"\b(?:no|not|never|without|n't)\b", item.source_text, re.I))
    translated_has_negation = bool(re.search(r"(不|没|无|未|别|从不)", item.translation))
    if source_has_negation and not translated_has_negation:
        item.qc_flags.append("NEGATION_MISMATCH")
    english_characters = len(re.findall(r"[A-Za-z]", item.translation))
    if english_characters > max(8, len(item.translation) * 0.35):
        item.qc_flags.append("ENGLISH_LEAKAGE")
    if re.search(r"(进行一个|对于.*来说|值得注意的是)", item.translation):
        item.qc_flags.append("TRANSLATIONESE")
    if re.search(r"([，。！？])\1{1,}", item.translation):
        item.qc_flags.append("UNNATURAL_PUNCTUATION")
    item.qc_flags = list(dict.fromkeys(item.qc_flags))


class Stage3Pipeline:
    def __init__(self, video_dir: Path | str, config: dict[str, Any]) -> None:
        self.video_dir = Path(video_dir).resolve()
        self.config = config
        self.stage3_dir = self.video_dir / "stage3"
        self.subtitle_dir = self.video_dir / "subtitles"
        self.youtube_dir = self.stage3_dir / "youtube"
        self.whisper_dir = self.stage3_dir / "whisper"
        self.selection_dir = self.stage3_dir / "selection"
        self.translation_dir = self.stage3_dir / "translation"
        self.legacy_translation_dir = self.video_dir / "translation"
        self.review_dir = self.stage3_dir / "review"
        self._last_whisper_result: dict[str, Any] | None = None
        self.started_at = utc_now()
        self.manifest_path = self.video_dir / "stage3_manifest.json"
        migrations = migrate_legacy_artifacts(self.video_dir)
        defaults: dict[str, Any] = {
            "video_dir": str(self.video_dir),
            "config_hash": hash_config(config),
            "source_audio": "",
            "source_audio_hash": "",
            "original_subtitle_hashes": {},
            "youtube_status": "NOT_RUN",
            "youtube_source_path": "",
            "youtube_clean_path": "",
            "youtube_score": None,
            "whisper_status": "NOT_RUN",
            "whisper_model_path": "",
            "whisper_device": "",
            "whisper_compute_type": "",
            "whisper_clean_path": "",
            "whisper_score": None,
            "agreement_score": None,
            "selected_source": "",
            "selected_input_path": "",
            "selected_output_path": "",
            "selected_source_hash": "",
            "selected_output_hash": "",
            "selection_reason": "",
            "translation_status": "NOT_RUN",
            "translation_source_hash": "",
            "selection_report_hash": "",
            "translation_model": load_deepseek_settings()["model"],
            "api_usage": {},
            "translation_qc": {},
            "review_status": "NOT_RUN",
            "reviewed_path": "",
            "reviewed_hash": "",
            "started_at": self.started_at,
            "finished_at": "",
            "errors": [],
            "warnings": [],
            "migrations": migrations,
            # Legacy fields retained for old consumers.
            "p0_status": "NOT_RUN",
            "p1_status": "NOT_RUN",
            "asr_status": "NOT_RUN",
            "clean_english_path": "",
            "clean_chinese_path": "",
            "segment_count": 0,
            "translation_count": 0,
            "p0_qc": {},
            "p1_qc": {},
        }
        if self.manifest_path.is_file():
            previous = json.loads(self.manifest_path.read_text(encoding="utf-8-sig"))
            defaults.update(previous)
            defaults["config_hash"] = hash_config(config)
            defaults["migrations"] = migrations
        original_hashes = dict(defaults.get("original_subtitle_hashes", {}))
        for name in self.config.get("source_priority", []):
            candidate = self.subtitle_dir / str(name)
            if candidate.is_file():
                original_hashes[str(candidate)] = sha256_file(candidate)
        defaults["original_subtitle_hashes"] = original_hashes
        self.manifest = defaults

    def _finish(self) -> None:
        self.manifest["finished_at"] = utc_now()
        write_manifest(self.video_dir, self.manifest)

    def _append_error(self, message: str) -> None:
        if message not in self.manifest["errors"]:
            self.manifest["errors"].append(message)

    def _youtube_config_hash(self) -> str:
        keys = (
            "source_priority",
            "rolling_context_cues",
            "fuzzy_matching_enabled",
            "sentence_gap_seconds",
            "min_segment_duration",
            "max_segment_duration",
            "hard_max_segment_duration",
            "minimum_gap_ms",
            "english_max_chars_per_line",
            "max_lines",
            "english_max_cps",
        )
        return hash_config({key: self.config.get(key) for key in keys})

    def _selection_config_hash(self) -> str:
        keys = (
            "scoring_weights",
            "minimum_acceptable_score",
            "selection_margin",
            "minimum_speech_coverage",
            "automatic_tiebreak",
            "min_segment_duration",
            "max_segment_duration",
            "hard_max_segment_duration",
            "minimum_gap_ms",
            "english_max_chars_per_line",
            "max_lines",
            "english_max_cps",
        )
        return hash_config({key: self.config.get(key) for key in keys})

    def _youtube_checkpoint_reusable(self, source: Path, source_hash: str) -> bool:
        checkpoint_path = self.youtube_dir / "checkpoint.json"
        if not checkpoint_path.is_file():
            return False
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        outputs = [
            self.subtitle_dir / "en.youtube.raw.srt",
            self.subtitle_dir / "en.youtube.clean.srt",
            self.youtube_dir / "source_assessment.json",
            self.youtube_dir / "raw_cues.json",
            self.youtube_dir / "word_events.json",
            self.youtube_dir / "clean_segments.json",
            self.youtube_dir / "qc.json",
        ]
        checkpoint_version = checkpoint.get("code_version")
        config_matches = (
            checkpoint.get("config_hash") == self._youtube_config_hash()
            or checkpoint_version is None
        )
        base_matches = bool(
            checkpoint.get("status") == "YOUTUBE_COMPLETED"
            and checkpoint.get("source_path") == str(source)
            and checkpoint.get("source_hash") == source_hash
            and config_matches
            and all(path.is_file() for path in outputs)
        )
        if not base_matches:
            return False
        if checkpoint_version not in {None, YOUTUBE_CLEANER_VERSION}:
            return False
        output_hashes = checkpoint.get("output_hashes") or {}
        if output_hashes and any(
            output_hashes.get(str(path)) not in {None, sha256_file(path)}
            for path in outputs
        ):
            return False
        return True

    def run_p0(self, source_override: Path | None = None, *, force: bool = False) -> dict[str, Any]:
        source = source_override or select_source(self.video_dir, self.config.get("source_priority"))
        if source is None:
            assessment = {
                "selected_source": "",
                "route": "NO_YOUTUBE_ENGLISH_SOURCE",
                "status": "NO_YOUTUBE_ENGLISH_SOURCE",
            }
            atomic_write_json(self.youtube_dir / "source_assessment.json", assessment)
            atomic_write_json(self.stage3_dir / "01_source_assessment.json", assessment)
            self.manifest.update(youtube_status="NO_YOUTUBE_ENGLISH_SOURCE", p0_status="NO_YOUTUBE_ENGLISH_SOURCE")
            self._finish()
            return assessment

        source = source.resolve()
        before_hash = sha256_file(source)
        if not force and self._youtube_checkpoint_reusable(source, before_hash):
            report = json.loads((self.youtube_dir / "qc.json").read_text(encoding="utf-8"))
            report.update(
                status="YOUTUBE_COMPLETED",
                skipped=True,
                selected_source=str(source),
                source_type="manual" if ".manual." in source.name else "auto",
            )
            self.manifest.update(
                youtube_status="YOUTUBE_COMPLETED",
                youtube_source_path=str(source),
                youtube_clean_path=str(self.subtitle_dir / "en.youtube.clean.srt"),
            )
            checkpoint_outputs = [
                self.subtitle_dir / "en.youtube.raw.srt",
                self.subtitle_dir / "en.youtube.clean.srt",
                self.youtube_dir / "source_assessment.json",
                self.youtube_dir / "raw_cues.json",
                self.youtube_dir / "word_events.json",
                self.youtube_dir / "clean_segments.json",
                self.youtube_dir / "qc.json",
            ]
            atomic_write_json(
                self.youtube_dir / "checkpoint.json",
                {
                    "status": "YOUTUBE_COMPLETED",
                    "source_path": str(source),
                    "source_hash": before_hash,
                    "config_hash": self._youtube_config_hash(),
                    "code_version": YOUTUBE_CLEANER_VERSION,
                    "output_paths": [str(path) for path in checkpoint_outputs],
                    "output_hashes": {
                        str(path): sha256_file(path) for path in checkpoint_outputs
                    },
                    "completed_at": utc_now(),
                },
            )
            self._finish()
            return report

        assessment = assess_source(source)
        assessment["source_sha256"] = before_hash
        assessment["source_type"] = "manual" if ".manual." in source.name else "auto"
        atomic_write_json(self.youtube_dir / "source_assessment.json", assessment)
        atomic_write_json(self.stage3_dir / "01_source_assessment.json", assessment)
        cues = parse_youtube_vtt(source) if source.suffix.lower() == ".vtt" else _raw_from_srt(source)
        raw_cues = [cue.to_dict() for cue in cues]
        atomic_write_json(self.youtube_dir / "raw_cues.json", raw_cues)
        atomic_write_json(self.stage3_dir / "02_raw_cues.json", raw_cues)

        raw_segments = [
            SubtitleSegment(cue.id, cue.start, cue.end, cue.text, [cue.id], cue.words, source="youtube")
            for cue in cues if cue.text
        ]
        raw_path = atomic_write_srt(
            self.subtitle_dir / "en.youtube.raw.srt",
            raw_segments,
            width=int(self.config["english_max_chars_per_line"]),
            max_lines=int(self.config["max_lines"]),
        )
        atomic_copy(raw_path, self.subtitle_dir / "en.source.raw.srt")
        events, cleaning_stats = build_word_events(cues)
        event_rows = [event.to_dict() for event in events]
        atomic_write_json(self.youtube_dir / "word_events.json", event_rows)
        atomic_write_json(self.stage3_dir / "03_word_events.json", event_rows)
        segments = segment_sentences(events, self.config)
        for segment in segments:
            segment.source = "youtube"
        metadata = _media_metadata(self.video_dir)
        segments = rebuild_timeline(segments, self.config, metadata["duration"])
        clean_rows = [segment.to_dict() for segment in segments]
        atomic_write_json(self.youtube_dir / "clean_segments.json", clean_rows)
        atomic_write_json(self.stage3_dir / "04_en_segments.json", clean_rows)
        clean_path = atomic_write_srt(
            self.subtitle_dir / "en.youtube.clean.srt",
            segments,
            width=int(self.config["english_max_chars_per_line"]),
            max_lines=int(self.config["max_lines"]),
        )
        atomic_copy(clean_path, self.subtitle_dir / "en.clean.srt")
        report = p0_quality(len(cues), segments, cleaning_stats)
        report.update(
            status="YOUTUBE_COMPLETED" if report["status"] == "QC_PASSED" else report["status"],
            selected_source=str(source),
            source_type=assessment["source_type"],
            source_sha256=before_hash,
            source_hash_unchanged=sha256_file(source) == before_hash,
            raw_cue_count=len(cues),
            clean_segment_count=len(segments),
        )
        atomic_write_json(self.youtube_dir / "qc.json", report)
        atomic_write_json(self.stage3_dir / "05_p0_qc.json", report)
        _atomic_text(self.youtube_dir / "qc.txt", qc_text(report))
        _atomic_text(self.stage3_dir / "05_p0_qc.txt", qc_text(report))
        if not report["source_hash_unchanged"]:
            raise RuntimeError(f"Original subtitle hash changed unexpectedly: {source}")
        checkpoint_outputs = [
            raw_path,
            clean_path,
            self.youtube_dir / "source_assessment.json",
            self.youtube_dir / "raw_cues.json",
            self.youtube_dir / "word_events.json",
            self.youtube_dir / "clean_segments.json",
            self.youtube_dir / "qc.json",
        ]
        checkpoint = {
            "status": "YOUTUBE_COMPLETED",
            "source_path": str(source),
            "source_hash": before_hash,
            "config_hash": self._youtube_config_hash(),
            "code_version": YOUTUBE_CLEANER_VERSION,
            "output_paths": [str(path) for path in checkpoint_outputs],
            "output_hashes": {
                str(path): sha256_file(path) for path in checkpoint_outputs
            },
            "completed_at": utc_now(),
        }
        atomic_write_json(self.youtube_dir / "checkpoint.json", checkpoint)
        original_hashes = dict(self.manifest.get("original_subtitle_hashes", {}))
        original_hashes[str(source)] = before_hash
        self.manifest.update(
            original_subtitle_hashes=original_hashes,
            youtube_status=report["status"],
            youtube_source_path=str(source),
            youtube_clean_path=str(clean_path),
            p0_status=report["status"],
            clean_english_path=str(clean_path),
            segment_count=len(segments),
            p0_qc=report,
        )
        self._finish()
        return report

    def run_whisper(self, *, max_seconds: float | None = None, force: bool = False) -> dict[str, Any]:
        from .asr_faster_whisper import run_faster_whisper_asr

        result = run_faster_whisper_asr(
            self.video_dir,
            self.config,
            Path(__file__).resolve().parents[2],
            max_seconds=max_seconds,
            force=force,
        )
        self._last_whisper_result = result
        self._update_asr_manifest(result)
        self._finish()
        return result

    def _update_asr_manifest(self, result: dict[str, Any]) -> None:
        if result.get("status") == "NO_AUDIO_SOURCE":
            self.manifest.update(whisper_status="NO_AUDIO_SOURCE", asr_status="NO_AUDIO_SOURCE")
            return
        info, qc = result.get("info", {}), result.get("qc", {})
        self.manifest.update(
            source_audio=info.get("audio_path", ""),
            source_audio_hash=sha256_file(info["audio_path"]) if info.get("audio_path") else "",
            whisper_status=qc.get("status", result.get("status", "FAILED")),
            whisper_model_path=info.get("model_path", ""),
            whisper_device=info.get("device", ""),
            whisper_compute_type=info.get("compute_type", ""),
            whisper_clean_path=str(self.subtitle_dir / "en.whisper.clean.srt"),
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

    def _speech_intervals(self) -> list[tuple[float, float]]:
        path = self.whisper_dir / "raw_segments.json"
        if not path.is_file():
            return []
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [
            (float(row["start"]), float(row["end"]))
            for row in rows
            if float(row.get("end", 0)) > float(row.get("start", 0))
            and float(row.get("no_speech_prob") or 0) <= 0.6
        ]

    @staticmethod
    def _hash_if_file(path: Path) -> str:
        return sha256_file(path) if path.is_file() else ""

    def _selection_checkpoint_metadata(self, mode: str) -> dict[str, Any]:
        input_paths = {
            "youtube_clean": self.subtitle_dir / "en.youtube.clean.srt",
            "youtube_qc": self.youtube_dir / "qc.json",
            "whisper_clean": self.subtitle_dir / "en.whisper.clean.srt",
            "whisper_qc": self.whisper_dir / "qc.json",
            "whisper_raw_segments": self.whisper_dir / "raw_segments.json",
            "whisper_info": self.whisper_dir / "asr_info.json",
        }
        return {
            "selection_policy_version": SELECTION_POLICY_VERSION,
            "subtitle_source_mode": mode,
            "config_hash": self._selection_config_hash(),
            "input_hashes": {
                name: self._hash_if_file(path) for name, path in input_paths.items()
            },
        }

    def _load_selection_checkpoint(self, metadata: dict[str, Any]) -> dict[str, Any] | None:
        checkpoint_path = self.selection_dir / "checkpoint.json"
        report_path = self.selection_dir / "selection_report.json"
        if not checkpoint_path.is_file() or not report_path.is_file():
            return None
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if checkpoint.get("status") not in {
            "SOURCE_SELECTED",
            "FAILED_NO_USABLE_SUBTITLE_SOURCE",
            "NO_AUDIO_SOURCE",
        }:
            return None
        for key, value in metadata.items():
            if checkpoint.get(key) != value:
                return None
        output_hashes = checkpoint.get("output_hashes") or {}
        if not output_hashes:
            return None
        for path_value, expected_hash in output_hashes.items():
            path = Path(path_value)
            if not path.is_file() or sha256_file(path) != expected_hash:
                return None
        if report.get("selection_policy_version") != SELECTION_POLICY_VERSION:
            return None
        selected_path = str(report.get("selected_output_path") or "")
        if selected_path:
            path = Path(selected_path)
            if (
                not path.is_file()
                or sha256_file(path) != report.get("selected_output_hash")
                or report.get("selected_output_hash") != report.get("selected_source_hash")
            ):
                return None
        return report

    def _write_selection_checkpoint(
        self,
        metadata: dict[str, Any],
        report: dict[str, Any],
        status: str,
    ) -> None:
        output_paths = [
            self.selection_dir / "scoring.json",
            self.selection_dir / "comparison.json",
            self.selection_dir / "selection_report.json",
            self.stage3_dir / "source_comparison.json",
        ]
        selected_path = self.subtitle_dir / "en.selected.srt"
        html_path = self.stage3_dir / "stage3_review.html"
        if selected_path.is_file():
            output_paths.append(selected_path)
        if html_path.is_file():
            output_paths.append(html_path)
        atomic_write_json(
            self.selection_dir / "checkpoint.json",
            {
                **metadata,
                "status": status,
                "selected_source": report.get("selected_source", ""),
                "output_paths": [str(path) for path in output_paths],
                "output_hashes": {
                    str(path): sha256_file(path) for path in output_paths
                },
                "completed_at": utc_now(),
            },
        )

    def _update_selection_manifest(self, report: dict[str, Any]) -> None:
        youtube_score = report.get("youtube") or {}
        whisper_score = report.get("whisper") or {}
        self.manifest.update(
            youtube_score=youtube_score.get("final_score"),
            whisper_score=whisper_score.get("final_score"),
            agreement_score=report.get("agreement_score"),
            selected_source=report.get("selected_source", ""),
            selected_input_path=report.get("selected_input_path", ""),
            selected_output_path=report.get("selected_output_path", ""),
            selected_source_hash=report.get("selected_source_hash", ""),
            selected_output_hash=report.get("selected_output_hash", ""),
            selection_reason=report.get("selection_reason", ""),
            selected_english_source=report.get("selected_source", ""),
            selected_english_path=report.get("selected_output_path", ""),
        )
        self.manifest["warnings"] = list(
            dict.fromkeys(
                self.manifest.get("warnings", []) + report.get("warnings", [])
            )
        )

    def run_p2(
        self,
        *,
        subtitle_source: str = "auto",
        max_seconds: float | None = None,
        force: bool = False,
        force_youtube: bool = False,
        force_whisper: bool = False,
        force_selection: bool = False,
        prepare_sources: bool = True,
    ) -> dict[str, Any]:
        mode = subtitle_source.casefold()
        if mode == "manual":
            mode = "youtube"
        if prepare_sources:
            youtube_result = self.run_p0(force=force or force_youtube)
            whisper_result = self.run_whisper(max_seconds=max_seconds, force=force or force_whisper)
        else:
            youtube_qc_path = self.youtube_dir / "qc.json"
            whisper_info_path = self.whisper_dir / "asr_info.json"
            whisper_qc_path = self.whisper_dir / "qc.json"
            youtube_result = (
                json.loads(youtube_qc_path.read_text(encoding="utf-8"))
                if youtube_qc_path.is_file()
                else {"status": "NO_YOUTUBE_ENGLISH_SOURCE"}
            )
            if self._last_whisper_result is not None:
                whisper_result = self._last_whisper_result
            elif whisper_info_path.is_file() and whisper_qc_path.is_file():
                whisper_result = {
                    "status": "ASR_COMPLETED",
                    "completed": True,
                    "skipped": True,
                    "info": json.loads(whisper_info_path.read_text(encoding="utf-8")),
                    "qc": json.loads(whisper_qc_path.read_text(encoding="utf-8")),
                }
            else:
                whisper_result = {"status": "NO_AUDIO_SOURCE", "completed": False}
        youtube_path = self.subtitle_dir / "en.youtube.clean.srt"
        whisper_path = self.subtitle_dir / "en.whisper.clean.srt"
        youtube_available = youtube_path.is_file()
        whisper_available = whisper_path.is_file() and whisper_result.get("status") != "NO_AUDIO_SOURCE"
        selection_metadata = self._selection_checkpoint_metadata(mode)
        if not force and not force_selection:
            cached_report = self._load_selection_checkpoint(selection_metadata)
            if cached_report is not None:
                self._update_selection_manifest(cached_report)
                self._finish()
                if not youtube_available and whisper_result.get("status") == "NO_AUDIO_SOURCE":
                    cached_status = "NO_AUDIO_SOURCE"
                elif cached_report.get("selection_failed"):
                    cached_status = "FAILED_NO_USABLE_SUBTITLE_SOURCE"
                else:
                    cached_status = "SOURCE_SELECTED"
                return {
                    "status": cached_status,
                    "selection_report": cached_report,
                    "source_comparison": cached_report,
                    "selected_path": cached_report.get("selected_output_path", ""),
                    "whisper_started": (
                        not bool(whisper_result.get("skipped"))
                        and whisper_result.get("status") == "ASR_COMPLETED"
                    ),
                    "asr_checkpoint_reused": bool(whisper_result.get("skipped")),
                    "selection_checkpoint_reused": True,
                }
        agreement = subtitle_agreement(youtube_path, whisper_path) if youtube_available and whisper_available else 0.0
        metadata = _media_metadata(self.video_dir)
        asr_info_path = self.whisper_dir / "asr_info.json"
        asr_info = json.loads(asr_info_path.read_text(encoding="utf-8")) if asr_info_path.is_file() else {}
        audio_duration = float(asr_info.get("audio_duration") or metadata.get("duration") or 0.0)
        speech_intervals = self._speech_intervals()
        youtube_qc_path = self.youtube_dir / "qc.json"
        whisper_qc_path = self.whisper_dir / "qc.json"
        youtube_qc = json.loads(youtube_qc_path.read_text(encoding="utf-8")) if youtube_qc_path.is_file() else {}
        whisper_qc = json.loads(whisper_qc_path.read_text(encoding="utf-8")) if whisper_qc_path.is_file() else {}
        whisper_raw_path = self.whisper_dir / "raw_segments.json"
        if whisper_raw_path.is_file():
            raw_rows = json.loads(whisper_raw_path.read_text(encoding="utf-8"))
            log_probabilities = [
                float(row["avg_logprob"]) for row in raw_rows if row.get("avg_logprob") is not None
            ]
            no_speech_probabilities = [
                float(row["no_speech_prob"]) for row in raw_rows if row.get("no_speech_prob") is not None
            ]
            whisper_qc = {
                **whisper_qc,
                "average_log_probability": (
                    sum(log_probabilities) / len(log_probabilities) if log_probabilities else None
                ),
                "average_no_speech_probability": (
                    sum(no_speech_probabilities) / len(no_speech_probabilities)
                    if no_speech_probabilities else None
                ),
            }
        youtube_score = (
            score_subtitle(
                youtube_path,
                source="youtube",
                source_type=str(youtube_result.get("source_type") or "auto"),
                config=self.config,
                audio_duration=audio_duration,
                speech_intervals=speech_intervals,
                source_qc=youtube_qc,
                original_hash_unchanged=bool(youtube_result.get("source_hash_unchanged", True)),
                agreement_score=agreement if whisper_available else None,
            )
            if youtube_available else None
        )
        whisper_score = (
            score_subtitle(
                whisper_path,
                source="whisper",
                source_type="faster-whisper-large-v3",
                config=self.config,
                audio_duration=audio_duration,
                speech_intervals=speech_intervals,
                source_qc=whisper_qc,
                agreement_score=agreement if youtube_available else None,
            )
            if whisper_available else None
        )
        decision = choose_source(
            youtube_score,
            whisper_score,
            mode=mode,
            minimum_score=float(self.config["minimum_acceptable_score"]),
            margin=float(self.config["selection_margin"]),
            automatic_tiebreak=self.config.get("automatic_tiebreak", {}),
        )
        report = write_selection_outputs(
            self.video_dir,
            youtube_score,
            whisper_score,
            decision,
            agreement_score=agreement,
        )
        atomic_write_json(self.stage3_dir / "source_comparison.json", report)
        if report["selected_output_path"]:
            generate_review_html(self.video_dir)
        else:
            (self.stage3_dir / "stage3_review.html").unlink(missing_ok=True)
        self._update_selection_manifest(report)
        status = "SOURCE_SELECTED"
        if report.get("selection_failed"):
            status = "FAILED_NO_USABLE_SUBTITLE_SOURCE"
        if not youtube_available and whisper_result.get("status") == "NO_AUDIO_SOURCE":
            status = "NO_AUDIO_SOURCE"
        self._write_selection_checkpoint(selection_metadata, report, status)
        self._finish()
        return {
            "status": status,
            "selection_report": report,
            "source_comparison": report,
            "selected_path": report["selected_output_path"],
            "whisper_started": not bool(whisper_result.get("skipped")) and whisper_result.get("status") == "ASR_COMPLETED",
            "asr_checkpoint_reused": bool(whisper_result.get("skipped")),
            "selection_checkpoint_reused": False,
        }

    def _load_glossary(self) -> dict[str, Any]:
        path = self.translation_dir / "glossary.json"
        if not path.is_file():
            atomic_write_json(path, GLOSSARY_DEFAULT)
        atomic_copy(path, self.legacy_translation_dir / "glossary.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def _translation_checkpoint_metadata(
        self,
        selected_path: Path,
        selection_report_path: Path,
        glossary_path: Path,
        model: str,
        polish_all: bool,
    ) -> dict[str, Any]:
        return {
            "stage_version": TRANSLATION_STAGE_VERSION,
            "source_hash": sha256_file(selected_path),
            "selection_report_hash": sha256_file(selection_report_path),
            "glossary_hash": sha256_file(glossary_path),
            "config_hash": hash_config(self.config),
            "prompt_version": PROMPT_VERSION,
            "model": model,
            "polish_all": bool(polish_all),
        }

    @staticmethod
    def _load_output_checkpoint(
        checkpoint_path: Path,
        metadata: dict[str, Any],
        allowed_statuses: set[str],
    ) -> dict[str, Any] | None:
        if not checkpoint_path.is_file():
            return None
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if checkpoint.get("status") not in allowed_statuses:
            return None
        for key, value in metadata.items():
            if checkpoint.get(key) != value:
                return None
        output_hashes = checkpoint.get("output_hashes") or {}
        if not output_hashes:
            return None
        for path_value, expected_hash in output_hashes.items():
            path = Path(path_value)
            if not path.is_file() or sha256_file(path) != expected_hash:
                return None
        return checkpoint

    @staticmethod
    def _write_output_checkpoint(
        checkpoint_path: Path,
        metadata: dict[str, Any],
        status: str,
        output_paths: list[Path],
    ) -> None:
        atomic_write_json(
            checkpoint_path,
            {
                **metadata,
                "status": status,
                "output_paths": [str(path) for path in output_paths],
                "output_hashes": {
                    str(path): sha256_file(path) for path in output_paths
                },
                "completed_at": utc_now(),
            },
        )

    def run_p1(
        self,
        *,
        allow_paid_api: bool = False,
        force: bool = False,
        polish_all: bool = False,
    ) -> dict[str, Any]:
        selected_path = self.subtitle_dir / "en.selected.srt"
        if not selected_path.is_file():
            raise FileNotFoundError(
                "EN_SELECTED_SUBTITLE_NOT_FOUND: run --steps youtube,whisper,select before translation"
            )
        selection_report_path = self.selection_dir / "selection_report.json"
        if not selection_report_path.is_file():
            raise FileNotFoundError("SELECTION_REPORT_NOT_FOUND: run --steps select before translation")
        selection_report = json.loads(selection_report_path.read_text(encoding="utf-8"))
        selected_hash = sha256_file(selected_path)
        if (
            not selection_report.get("selected_source")
            or selection_report.get("selected_output_hash") != selected_hash
            or selection_report.get("selected_source_hash") != selected_hash
        ):
            raise RuntimeError(
                "EN_SELECTED_SUBTITLE_CHECKPOINT_MISMATCH: rerun --steps select before translation"
            )
        source = read_srt(selected_path)
        if not source:
            raise ValueError("EN_SELECTED_SUBTITLE_EMPTY")
        glossary = self._load_glossary()
        glossary_path = self.translation_dir / "glossary.json"
        batch_count = math.ceil(len(source) / int(self.config["translation_batch_size"]))
        settings = load_deepseek_settings()
        timeline_summary = {
            "first_start": source[0].start,
            "last_end": source[-1].end,
            "duration_sum": round(sum(item.duration for item in source), 3),
            "overlaps": sum(right.start < left.end for left, right in zip(source, source[1:])),
        }
        preflight = {
            "status": "DRY_RUN",
            "api_called": False,
            "paid_api_enabled": False,
            "api_key_configured": bool(settings["api_key"]),
            "model": settings["model"],
            "batch_count": batch_count,
            "estimated_translation_count": len(source),
            "input": str(selected_path),
            "source_sha256": sha256_file(selected_path),
            "selection_report_sha256": sha256_file(selection_report_path),
            "segment_count": len(source),
            "timeline_summary": timeline_summary,
        }
        translation_metadata = self._translation_checkpoint_metadata(
            selected_path,
            selection_report_path,
            glossary_path,
            settings["model"],
            polish_all,
        )
        if not allow_paid_api:
            dry_run_path = self.translation_dir / "dry_run.json"
            dry_checkpoint_path = self.translation_dir / "dry_run_checkpoint.json"
            cached = self._load_output_checkpoint(
                dry_checkpoint_path,
                translation_metadata,
                {"DRY_RUN_COMPLETED"},
            )
            if cached is not None:
                preflight = json.loads(dry_run_path.read_text(encoding="utf-8"))
                preflight["skipped"] = True
                preflight["checkpoint_reused"] = True
            else:
                atomic_write_json(dry_run_path, preflight)
                self._write_output_checkpoint(
                    dry_checkpoint_path,
                    translation_metadata,
                    "DRY_RUN_COMPLETED",
                    [dry_run_path],
                )
            atomic_write_json(self.legacy_translation_dir / "dry_run.json", preflight)
            self.manifest.update(
                translation_status="DRY_RUN",
                translation_source_hash=preflight["source_sha256"],
                selection_report_hash=preflight["selection_report_sha256"],
                p1_status="DRY_RUN",
                translation_count=0,
                p1_qc=preflight,
            )
            self._finish()
            return preflight
        if not settings["api_key"]:
            raise TranslationError("--allow-paid-api requires DEEPSEEK_API_KEY in the environment")

        stage_checkpoint_path = self.translation_dir / "translation_checkpoint.json"
        cached_stage = None if force else self._load_output_checkpoint(
            stage_checkpoint_path,
            translation_metadata,
            {"TRANSLATION_COMPLETED"},
        )
        if cached_stage is not None:
            report = json.loads(
                (self.translation_dir / "translation_qc.json").read_text(encoding="utf-8")
            )
            usage_path = self.translation_dir / "api_usage.json"
            usage = (
                json.loads(usage_path.read_text(encoding="utf-8"))
                if usage_path.is_file() else {}
            )
            report["skipped"] = True
            report["checkpoint_reused"] = True
            clean_path = self.subtitle_dir / "zh.clean.srt"
            self.manifest.update(
                translation_status=report["status"],
                translation_source_hash=translation_metadata["source_hash"],
                selection_report_hash=translation_metadata["selection_report_hash"],
                translation_model=settings["model"],
                api_usage=usage,
                translation_qc=report,
                p1_status=report["status"],
                clean_chinese_path=str(clean_path) if clean_path.is_file() else "",
                translation_count=len(source),
                p1_qc=report,
            )
            self._finish()
            return report

        metadata = _media_metadata(self.video_dir)
        translator = DeepSeekTranslator(self.config, self.translation_dir)
        raw_map = translator.translate_all(source, source, glossary, metadata, pass_name="raw", force=force)
        translated = [
            TranslationSegment(item.id, item.start, item.end, item.text, raw_map.get(item.id, ""), raw_map.get(item.id, ""))
            for item in source
        ]
        for item in translated:
            _translation_flags(item, glossary, self.config)
        raw_json = atomic_write_json(self.translation_dir / "translation_raw.json", [item.to_dict() for item in translated])
        atomic_copy(raw_json, self.legacy_translation_dir / "translation_raw.json")
        atomic_write_srt(
            self.subtitle_dir / "zh.raw.srt",
            translated,
            translated=True,
            width=int(self.config["chinese_max_chars_per_line"]),
            max_lines=int(self.config["max_lines"]),
        )

        polish_ids = {item.id for item in translated if item.qc_flags} if not polish_all else {item.id for item in translated}
        if polish_ids:
            polish_targets = [item for item in source if item.id in polish_ids]
            polished_map = translator.translate_all(
                polish_targets, source, glossary, metadata, pass_name="polished", force=force
            )
            for item in translated:
                if item.id in polished_map:
                    item.translation = polished_map[item.id]
                    item.repaired = True
                    item.qc_flags = []
                    _translation_flags(item, glossary, self.config)
        polished_json = atomic_write_json(
            self.translation_dir / "translation_polished.json",
            [item.to_dict() for item in translated],
        )
        atomic_copy(polished_json, self.legacy_translation_dir / "translation_polished.json")
        report = translation_quality(source, translated, self.config)
        qc_path = atomic_write_json(self.translation_dir / "translation_qc.json", report)
        atomic_copy(qc_path, self.legacy_translation_dir / "subtitle_qc.json")
        _atomic_text(self.translation_dir / "translation_qc.txt", qc_text(report))
        usage = translator.usage_report()
        usage_path = atomic_write_json(self.translation_dir / "api_usage.json", usage)
        atomic_copy(usage_path, self.legacy_translation_dir / "api_usage.json")
        clean_chinese = ""
        if (
            not report["missing_ids"]
            and not report["extra_ids"]
            and not report["empty_translation_ids"]
            and report["timeline_changed"] == 0
        ):
            clean_chinese = str(
                atomic_write_srt(
                    self.subtitle_dir / "zh.clean.srt",
                    translated,
                    translated=True,
                    width=int(self.config["chinese_max_chars_per_line"]),
                    max_lines=int(self.config["max_lines"]),
                )
            )
        self.manifest.update(
            translation_status=report["status"],
            translation_source_hash=sha256_file(selected_path),
            selection_report_hash=sha256_file(selection_report_path),
            translation_model=settings["model"],
            api_usage=usage,
            translation_qc=report,
            p1_status=report["status"],
            clean_chinese_path=clean_chinese,
            translation_count=len(translated),
            p1_qc=report,
        )
        stage_outputs = [
            raw_json,
            polished_json,
            qc_path,
            usage_path,
            self.subtitle_dir / "zh.raw.srt",
        ]
        clean_path = self.subtitle_dir / "zh.clean.srt"
        if clean_path.is_file():
            stage_outputs.append(clean_path)
        self._write_output_checkpoint(
            stage_checkpoint_path,
            translation_metadata,
            "TRANSLATION_COMPLETED",
            stage_outputs,
        )
        self._finish()
        return report

    def run_review_export(self) -> dict[str, Any]:
        result = export_review(self.video_dir)
        self.manifest.update(review_status=result["status"])
        self._finish()
        return result

    def run_review_import(self, review_file: Path | str, *, overwrite_reviewed: bool = False) -> dict[str, Any]:
        result = import_review(self.video_dir, review_file, overwrite_reviewed=overwrite_reviewed)
        self.manifest.update(
            review_status=result["status"],
            reviewed_path=result.get("reviewed_path", ""),
            reviewed_hash=result.get("reviewed_hash", ""),
        )
        if result["status"] == "REVIEW_IMPORT_FAILED":
            self._append_error("Review import failed")
        self._finish()
        return result
