from __future__ import annotations

from pathlib import Path
from typing import Any

from .stage4_manifest import atomic_write_json, atomic_write_text


def _close(left: float, right: float, tolerance: float) -> bool:
    return abs(float(left or 0) - float(right or 0)) <= tolerance


def _codecs(probe: dict[str, Any]) -> list[str]:
    return [str(item.get("codec") or "") for item in probe.get("audio_streams", [])]


def evaluate_render(
    source_probe: dict[str, Any],
    output_probe: dict[str, Any],
    *,
    mode: str,
    output_path: Path | str,
    duration_tolerance: float = 0.5,
    ffmpeg_returncode: int | None = 0,
    audio_transcoded: bool = False,
    temporary_cleaned: bool = True,
    subtitle_title: str = "English / 中文",
) -> dict[str, Any]:
    path = Path(output_path)
    checks: dict[str, bool] = {
        "output_exists": path.is_file(),
        "output_size_valid": path.is_file() and path.stat().st_size > 1024,
        "ffprobe_parsed": bool(output_probe),
        "video_stream_exists": int(output_probe.get("video_stream_count", 0)) > 0,
        "audio_stream_exists": int(output_probe.get("audio_stream_count", 0)) > 0,
        "duration_matches": _close(
            float(source_probe.get("duration", 0)),
            float(output_probe.get("duration", 0)),
            duration_tolerance,
        ),
        "resolution_matches": (
            int(source_probe.get("display_width", 0)) == int(output_probe.get("display_width", 0))
            and int(source_probe.get("display_height", 0))
            == int(output_probe.get("display_height", 0))
        ),
        "frame_rate_matches": _close(
            float(source_probe.get("frame_rate_value", 0)),
            float(output_probe.get("frame_rate_value", 0)),
            0.02,
        ),
        "audio_stream_count_matches": (
            int(source_probe.get("audio_stream_count", 0))
            == int(output_probe.get("audio_stream_count", 0))
        ),
        "ffmpeg_returned_zero": ffmpeg_returncode == 0,
        "temporary_files_cleaned": temporary_cleaned,
    }
    if mode == "softsub":
        added = [
            item
            for item in output_probe.get("subtitle_streams", [])
            if item.get("tags", {}).get("title") == subtitle_title
            and item.get("tags", {}).get("language") == "mul"
        ]
        checks.update(
            {
                "video_stream_copied": (
                    source_probe.get("video_codec") == output_probe.get("video_codec")
                ),
                "audio_streams_copied": _codecs(source_probe) == _codecs(output_probe),
                "ass_subtitle_exists": any(item.get("codec") in {"ass", "ssa"} for item in added),
                "subtitle_title_and_language": bool(added),
                "subtitle_default": any(
                    int(item.get("disposition", {}).get("default", 0)) == 1 for item in added
                ),
            }
        )
    else:
        audio_codecs_changed = _codecs(source_probe) != _codecs(output_probe)
        source_audio_durations = [
            float(item.get("duration") or source_probe.get("duration") or 0)
            for item in source_probe.get("audio_streams", [])
        ]
        output_audio_durations = [
            float(item.get("duration") or output_probe.get("duration") or 0)
            for item in output_probe.get("audio_streams", [])
        ]
        checks.update(
            {
                "hardsub_video_encoded": bool(output_probe.get("video_codec")),
                "audio_content_present": int(output_probe.get("audio_stream_count", 0)) > 0,
                "audio_duration_matches": (
                    len(source_audio_durations) == len(output_audio_durations)
                    and all(
                        _close(left, right, duration_tolerance)
                        for left, right in zip(source_audio_durations, output_audio_durations)
                    )
                ),
                "audio_transcode_recorded": not audio_codecs_changed or audio_transcoded,
            }
        )
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "mode": mode,
        "output_path": str(path.resolve()),
        "checks": checks,
        "failed_checks": failed,
        "duration_difference_seconds": round(
            float(output_probe.get("duration", 0)) - float(source_probe.get("duration", 0)),
            6,
        ),
        "audio_transcoded": audio_transcoded,
        "qc_status": "QC_PASSED" if not failed else "FAILED",
    }


def write_render_qc(
    json_path: Path | str,
    text_path: Path | str,
    reports: dict[str, Any],
) -> None:
    atomic_write_json(json_path, reports)
    lines: list[str] = []
    for mode, report in reports.items():
        lines.append(f"[{mode}] {report.get('qc_status', 'UNKNOWN')}")
        for name, passed in report.get("checks", {}).items():
            lines.append(f"{'PASS' if passed else 'FAIL'} {name}")
        lines.append("")
    atomic_write_text(text_path, "\n".join(lines).rstrip() + "\n")
