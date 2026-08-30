from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifact_migration import atomic_copy
from .manifest import sha256_file, utc_now
from .subtitle_writer import atomic_write_json, read_srt


SELECTION_POLICY_VERSION = "stage3-auto-selection-v4"


def _hard_safe(report: dict[str, Any] | None) -> bool:
    return bool(
        report
        and Path(str(report.get("path", ""))).is_file()
        and not report.get("hard_fail")
    )


def _usable(report: dict[str, Any] | None, minimum: float) -> bool:
    return _hard_safe(report) and float(report.get("final_score", 0.0)) >= minimum


def _dimension_score(report: dict[str, Any], name: str) -> float:
    return float(((report.get("scores") or {}).get(name) or {}).get("normalized_score", 0.0) or 0.0)


def _maximum_uncovered_speech(report: dict[str, Any]) -> float:
    coverage = ((report.get("scores") or {}).get("coverage") or {}).get("raw_values") or {}
    return float(coverage.get("maximum_uncovered_speech_seconds", float("inf")) or 0.0)


def _automatic_close_score_tiebreak(
    youtube: dict[str, Any],
    whisper: dict[str, Any],
    *,
    difference: float,
    margin: float,
    options: dict[str, Any],
) -> dict[str, Any]:
    if str(youtube.get("source_type")) == "manual":
        return {
            "selected_source": "youtube",
            "selection_reason": f"两套字幕分差 {difference:.2f} 小于 {margin:g}，自动优先人工 YouTube 字幕",
            "user_override": False,
            "review_required": False,
            "warnings": ["AUTO_TIEBREAK_MANUAL_YOUTUBE"],
        }

    coverage_margin = float(options.get("coverage_score_margin", 3))
    youtube_coverage = _dimension_score(youtube, "coverage")
    whisper_coverage = _dimension_score(whisper, "coverage")
    coverage_difference = abs(youtube_coverage - whisper_coverage)
    if coverage_difference >= coverage_margin:
        selected = "youtube" if youtube_coverage > whisper_coverage else "whisper"
        return {
            "selected_source": selected,
            "selection_reason": (
                f"两套总分仅差 {difference:.2f}；{selected} 语音覆盖分高出 "
                f"{coverage_difference:.2f}，达到自动决胜差值 {coverage_margin:g}"
            ),
            "user_override": False,
            "review_required": False,
            "warnings": ["AUTO_TIEBREAK_COVERAGE"],
        }

    gap_margin = float(options.get("maximum_uncovered_speech_gap_margin_seconds", 3))
    youtube_gap = _maximum_uncovered_speech(youtube)
    whisper_gap = _maximum_uncovered_speech(whisper)
    gap_difference = abs(youtube_gap - whisper_gap)
    if gap_difference >= gap_margin:
        selected = "youtube" if youtube_gap < whisper_gap else "whisper"
        return {
            "selected_source": selected,
            "selection_reason": (
                f"两套总分和覆盖分接近；{selected} 最长无字幕语音缺口少 "
                f"{gap_difference:.2f} 秒，达到自动决胜差值 {gap_margin:g} 秒"
            ),
            "user_override": False,
            "review_required": False,
            "warnings": ["AUTO_TIEBREAK_UNCOVERED_GAP"],
        }

    for dimension, label in (("timeline", "时间轴"), ("readability", "可读性")):
        youtube_value = _dimension_score(youtube, dimension)
        whisper_value = _dimension_score(whisper, dimension)
        if abs(youtube_value - whisper_value) > 1e-9:
            selected = "youtube" if youtube_value > whisper_value else "whisper"
            return {
                "selected_source": selected,
                "selection_reason": (
                    f"两套总分、覆盖和缺口均接近；自动选择{label}分更高的 {selected}"
                ),
                "user_override": False,
                "review_required": False,
                "warnings": [f"AUTO_TIEBREAK_{dimension.upper()}"],
            }

    youtube_score = float(youtube.get("final_score", 0.0))
    whisper_score = float(whisper.get("final_score", 0.0))
    if abs(youtube_score - whisper_score) > 1e-9:
        selected = "youtube" if youtube_score > whisper_score else "whisper"
        return {
            "selected_source": selected,
            "selection_reason": f"全部决胜指标接近，自动选择总分高出 {difference:.2f} 的 {selected}",
            "user_override": False,
            "review_required": False,
            "warnings": ["AUTO_TIEBREAK_FINAL_SCORE"],
        }

    preferred = str(options.get("preferred_source_on_exact_tie", "whisper")).casefold()
    if preferred not in {"youtube", "whisper"}:
        preferred = "whisper"
    return {
        "selected_source": preferred,
        "selection_reason": f"全部指标完全相同，按配置自动选择 {preferred}",
        "user_override": False,
        "review_required": False,
        "warnings": ["AUTO_TIEBREAK_EXACT_TIE"],
    }


def choose_source(
    youtube: dict[str, Any] | None,
    whisper: dict[str, Any] | None,
    *,
    mode: str,
    minimum_score: float,
    margin: float,
    automatic_tiebreak: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mode = mode.casefold()
    if mode not in {"auto", "youtube", "whisper", "manual"}:
        raise ValueError(f"Unsupported subtitle source: {mode}")
    requested = "youtube" if mode == "manual" else mode
    reports = {"youtube": youtube, "whisper": whisper}
    if requested != "auto":
        report = reports.get(requested)
        if not report or not Path(str(report.get("path", ""))).is_file():
            raise FileNotFoundError(f"Requested clean subtitle source does not exist: {requested}")
        warnings = list(report.get("flags", []))
        if report.get("hard_fail"):
            warnings.append("USER_OVERRIDE_HARD_FAIL")
        if float(report.get("final_score", 0.0)) < minimum_score:
            warnings.append("USER_OVERRIDE_BELOW_MINIMUM")
        return {
            "selected_source": requested,
            "selection_reason": f"用户显式指定 {requested}，评分警告仍保留",
            "user_override": True,
            "review_required": False,
            "warnings": list(dict.fromkeys(warnings)),
        }

    options = automatic_tiebreak or {}
    youtube_usable = _usable(youtube, minimum_score)
    whisper_usable = _usable(whisper, minimum_score)
    if youtube_usable and not whisper_usable:
        return {
            "selected_source": "youtube",
            "selection_reason": "只有 YouTube clean 字幕通过硬性检查和最低分",
            "user_override": False,
            "review_required": False,
            "warnings": [],
        }
    if whisper_usable and not youtube_usable:
        return {
            "selected_source": "whisper",
            "selection_reason": "只有 Whisper clean 字幕通过硬性检查和最低分",
            "user_override": False,
            "review_required": False,
            "warnings": [],
        }
    if youtube_usable and whisper_usable:
        youtube_score = float(youtube["final_score"])
        whisper_score = float(whisper["final_score"])
        difference = abs(youtube_score - whisper_score)
        if difference >= margin:
            selected = "youtube" if youtube_score > whisper_score else "whisper"
            return {
                "selected_source": selected,
                "selection_reason": f"{selected} 总分高出 {difference:.2f}，达到选择差值 {margin:g}",
                "user_override": False,
                "review_required": False,
                "warnings": [],
            }
        return _automatic_close_score_tiebreak(
            youtube,
            whisper,
            difference=difference,
            margin=margin,
            options=options,
        )

    safe_reports = {
        name: report
        for name, report in reports.items()
        if _hard_safe(report)
    }
    if safe_reports and bool(options.get("select_below_minimum_when_hard_checks_pass", True)):
        selected = max(
            safe_reports,
            key=lambda name: (
                float(safe_reports[name].get("final_score", 0.0)),
                name == str(options.get("preferred_source_on_exact_tie", "whisper")).casefold(),
            ),
        )
        return {
            "selected_source": selected,
            "selection_reason": (
                f"没有字幕达到最低分 {minimum_score:g}，但硬性检查通过；"
                f"自动选择总分较高的 {selected}"
            ),
            "user_override": False,
            "review_required": False,
            "warnings": ["AUTO_SELECTED_BELOW_MINIMUM"],
        }
    return {
        "selected_source": "",
        "selection_reason": "没有任何字幕源通过硬性结构、时间轴和覆盖检查，自动流程停止",
        "user_override": False,
        "review_required": False,
        "selection_failed": True,
        "warnings": ["NO_HARD_SAFE_SUBTITLE_SOURCE"],
    }


def write_selection_outputs(
    video_dir: Path | str,
    youtube: dict[str, Any] | None,
    whisper: dict[str, Any] | None,
    decision: dict[str, Any],
    *,
    agreement_score: float,
) -> dict[str, Any]:
    root = Path(video_dir).resolve()
    selection_dir = root / "stage3" / "selection"
    selected_source = str(decision.get("selected_source") or "")
    selected_report = {"youtube": youtube, "whisper": whisper}.get(selected_source)
    selected_input = Path(str(selected_report["path"])) if selected_report else None
    selected_output = root / "subtitles" / "en.selected.srt"
    selected_source_hash = ""
    selected_output_hash = ""
    if selected_input is not None:
        segments = read_srt(selected_input)
        if not segments:
            raise RuntimeError("Selected clean subtitle contains no segments")
        atomic_copy(selected_input, selected_output)
        if len(read_srt(selected_output)) != len(segments):
            raise RuntimeError("Selected subtitle verification failed")
        selected_source_hash = sha256_file(selected_input)
        selected_output_hash = sha256_file(selected_output)
    else:
        selected_output.unlink(missing_ok=True)

    scoring = {
        "youtube": youtube,
        "whisper": whisper,
        "agreement_score": agreement_score,
    }
    comparison = {
        "youtube_final_score": youtube.get("final_score") if youtube else None,
        "whisper_final_score": whisper.get("final_score") if whisper else None,
        "score_difference": round(
            abs(float(youtube.get("final_score", 0)) - float(whisper.get("final_score", 0))), 3
        ) if youtube and whisper else None,
        "agreement_score": agreement_score,
        **decision,
    }
    report = {
        "youtube": youtube or {
            "path": "", "source_type": "", "scores": {}, "final_score": 0,
            "hard_fail": True, "flags": ["SOURCE_UNAVAILABLE"],
        },
        "whisper": whisper or {
            "path": "", "model": "", "scores": {}, "final_score": 0,
            "hard_fail": True, "flags": ["SOURCE_UNAVAILABLE"],
        },
        "agreement_score": agreement_score,
        "selected_source": selected_source,
        "selected_input_path": str(selected_input) if selected_input else "",
        "selected_output_path": str(selected_output) if selected_input else "",
        "selected_source_hash": selected_source_hash,
        "selected_output_hash": selected_output_hash,
        "selection_reason": decision["selection_reason"],
        "user_override": bool(decision.get("user_override")),
        "review_required": bool(decision.get("review_required")),
        "selection_failed": bool(decision.get("selection_failed")),
        "selection_policy_version": SELECTION_POLICY_VERSION,
        "warnings": list(decision.get("warnings", [])),
        "selected_at": utc_now(),
    }
    atomic_write_json(selection_dir / "scoring.json", scoring)
    atomic_write_json(selection_dir / "comparison.json", comparison)
    atomic_write_json(selection_dir / "selection_report.json", report)
    return report
