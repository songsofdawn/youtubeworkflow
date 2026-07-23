from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifact_migration import atomic_copy
from .manifest import sha256_file, utc_now
from .subtitle_writer import atomic_write_json, read_srt


def _usable(report: dict[str, Any] | None, minimum: float) -> bool:
    return bool(
        report
        and Path(str(report.get("path", ""))).is_file()
        and not report.get("hard_fail")
        and float(report.get("final_score", 0.0)) >= minimum
    )


def choose_source(
    youtube: dict[str, Any] | None,
    whisper: dict[str, Any] | None,
    *,
    mode: str,
    minimum_score: float,
    margin: float,
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
            "review_required": bool(warnings),
            "warnings": list(dict.fromkeys(warnings)),
        }

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
        if str(youtube.get("source_type")) == "manual":
            return {
                "selected_source": "youtube",
                "selection_reason": f"两套字幕分差 {difference:.2f} 小于 {margin:g}，优先人工 YouTube 字幕",
                "user_override": False,
                "review_required": False,
                "warnings": ["SCORES_WITHIN_MARGIN_MANUAL_PREFERRED"],
            }
        return {
            "selected_source": "",
            "selection_reason": f"两套自动字幕分差 {difference:.2f} 小于 {margin:g}，需要人工选择",
            "user_override": False,
            "review_required": True,
            "warnings": ["SCORES_WITHIN_MARGIN"],
        }
    return {
        "selected_source": "",
        "selection_reason": f"没有字幕源同时通过硬性检查且达到最低分 {minimum_score:g}",
        "user_override": False,
        "review_required": True,
        "warnings": ["NO_ACCEPTABLE_SUBTITLE_SOURCE"],
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
        "warnings": list(decision.get("warnings", [])),
        "selected_at": utc_now(),
    }
    atomic_write_json(selection_dir / "scoring.json", scoring)
    atomic_write_json(selection_dir / "comparison.json", comparison)
    atomic_write_json(selection_dir / "selection_report.json", report)
    return report
