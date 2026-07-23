from __future__ import annotations

from copy import deepcopy
from typing import Any


SCORING_DIMENSIONS = (
    "structure",
    "timeline",
    "coverage",
    "stability",
    "readability",
    "source_confidence",
)


def normalize_stage3_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Return one config carrying both canonical sections and legacy flat aliases."""
    config = deepcopy(raw)
    if not all(section in config for section in ("youtube", "subtitle", "asr", "scoring", "translation")):
        config = _sections_from_legacy(config)

    youtube = config["youtube"]
    subtitle = config["subtitle"]
    asr = config["asr"]
    scoring = config["scoring"]
    translation = config["translation"]
    weights = scoring.get("weights", {})
    missing_weights = sorted(set(SCORING_DIMENSIONS) - set(weights))
    if missing_weights:
        raise ValueError(f"stage3 scoring weights missing: {missing_weights}")
    weight_sum = sum(float(weights[name]) for name in SCORING_DIMENSIONS)
    if abs(weight_sum - 1.0) > 1e-6:
        raise ValueError(f"stage3 scoring weights must total 1.0, got {weight_sum}")

    flat = deepcopy(config)
    flat.update(
        source_priority=list(youtube["source_priority"]),
        rolling_context_cues=int(youtube.get("rolling_context_cues", 3)),
        fuzzy_matching_enabled=bool(youtube.get("fuzzy_matching_enabled", True)),
        **subtitle,
        asr_enabled=bool(asr.get("enabled", True)),
        asr_model_path=str(asr["model_path"]),
        asr_device=str(asr["device"]),
        asr_compute_type=str(asr["compute_type"]),
        asr_language=str(asr["language"]),
        asr_beam_size=int(asr["beam_size"]),
        asr_vad_filter=bool(asr["vad_filter"]),
        asr_word_timestamps=bool(asr["word_timestamps"]),
        asr_condition_on_previous_text=bool(asr["condition_on_previous_text"]),
        asr_temperature=float(asr["temperature"]),
        asr_min_silence_duration_ms=int(asr["min_silence_duration_ms"]),
        asr_speech_pad_ms=int(asr["speech_pad_ms"]),
        asr_log_progress=bool(asr.get("log_progress", True)),
        scoring_weights={name: float(weights[name]) for name in SCORING_DIMENSIONS},
        minimum_acceptable_score=float(scoring["minimum_acceptable_score"]),
        selection_margin=float(scoring["selection_margin"]),
        minimum_speech_coverage=float(scoring["minimum_speech_coverage"]),
        manual_subtitle_quality_threshold=float(scoring["minimum_acceptable_score"]),
        youtube_subtitle_quality_threshold=float(scoring["minimum_acceptable_score"]),
        translation_batch_size=int(translation["batch_size"]),
        context_before=int(translation["context_before"]),
        context_after=int(translation["context_after"]),
        temperature=float(translation["temperature"]),
        max_retries=int(translation["max_retries"]),
        retry_delays_seconds=list(translation["retry_delays_seconds"]),
        chinese_chars_per_second=float(translation["chinese_chars_per_second"]),
        tts_warning_ratio=float(translation["tts_warning_ratio"]),
        tts_rewrite_ratio=float(translation["tts_rewrite_ratio"]),
        input_price_per_million=translation.get("input_price_per_million"),
        output_price_per_million=translation.get("output_price_per_million"),
    )
    return flat


def _sections_from_legacy(legacy: dict[str, Any]) -> dict[str, Any]:
    """Accept the previously shipped flat configuration without maintaining a second file."""
    return {
        "youtube": {
            "source_priority": legacy["source_priority"],
            "rolling_context_cues": legacy.get("rolling_context_cues", 3),
            "fuzzy_matching_enabled": legacy.get("fuzzy_matching_enabled", True),
        },
        "subtitle": {
            key: legacy[key]
            for key in (
                "sentence_gap_seconds", "min_segment_duration", "max_segment_duration",
                "minimum_gap_ms", "english_max_chars_per_line", "chinese_max_chars_per_line",
                "max_lines", "english_max_cps", "chinese_max_cps",
            )
        }
        | {"hard_max_segment_duration": legacy.get("hard_max_segment_duration", 8.0)},
        "asr": {
            "enabled": legacy.get("asr_enabled", True),
            "model_path": legacy["asr_model_path"],
            "device": legacy["asr_device"],
            "compute_type": legacy["asr_compute_type"],
            "language": legacy["asr_language"],
            "beam_size": legacy["asr_beam_size"],
            "vad_filter": legacy["asr_vad_filter"],
            "word_timestamps": legacy["asr_word_timestamps"],
            "condition_on_previous_text": legacy["asr_condition_on_previous_text"],
            "temperature": legacy["asr_temperature"],
            "min_silence_duration_ms": legacy["asr_min_silence_duration_ms"],
            "speech_pad_ms": legacy["asr_speech_pad_ms"],
            "log_progress": legacy.get("asr_log_progress", True),
        },
        "scoring": {
            "weights": legacy.get(
                "scoring_weights",
                {
                    "structure": 0.15, "timeline": 0.20, "coverage": 0.20,
                    "stability": 0.15, "readability": 0.10, "source_confidence": 0.20,
                },
            ),
            "minimum_acceptable_score": legacy.get("minimum_acceptable_score", 70),
            "selection_margin": legacy.get("selection_margin", 6),
            "minimum_speech_coverage": legacy.get("minimum_speech_coverage", 0.65),
        },
        "translation": {
            "batch_size": legacy["translation_batch_size"],
            "context_before": legacy["context_before"],
            "context_after": legacy["context_after"],
            "temperature": legacy["temperature"],
            "max_retries": legacy["max_retries"],
            "retry_delays_seconds": legacy["retry_delays_seconds"],
            "chinese_chars_per_second": legacy["chinese_chars_per_second"],
            "tts_warning_ratio": legacy["tts_warning_ratio"],
            "tts_rewrite_ratio": legacy["tts_rewrite_ratio"],
            "input_price_per_million": legacy.get("input_price_per_million"),
            "output_price_per_million": legacy.get("output_price_per_million"),
        },
    }
