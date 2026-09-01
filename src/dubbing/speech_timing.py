from __future__ import annotations

import array
import math
import os
import shutil
import sys
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class SilenceTrimResult:
    source_duration: float
    trim_start: float
    trim_end: float
    output_duration: float
    leading_silence: float
    trailing_silence: float
    speech_detected: bool
    trimmed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}-{os.getpid()}.tmp.wav")
    temporary.unlink(missing_ok=True)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def trim_wav_silence(
    source: Path | str,
    destination: Path | str,
    *,
    enabled: bool = True,
    threshold_db: float = -45.0,
    relative_db: float = -35.0,
    frame_ms: float = 10.0,
    padding_ms: float = 40.0,
) -> SilenceTrimResult:
    """Trim only detected edge silence from a PCM16 TTS WAV.

    A small padding is retained around detected speech. Unsupported WAV layouts
    are copied unchanged so silence analysis can never destroy a valid TTS
    checkpoint.
    """

    input_path = Path(source).resolve()
    output_path = Path(destination).resolve()
    try:
        with wave.open(str(input_path), "rb") as handle:
            params = handle.getparams()
            frame_rate = int(handle.getframerate())
            frame_count = int(handle.getnframes())
            channels = int(handle.getnchannels())
            sample_width = int(handle.getsampwidth())
            payload = handle.readframes(frame_count)
    except (OSError, EOFError, wave.Error) as exc:
        raise RuntimeError(f"无法分析 TTS WAV 首尾静音：{input_path}（{exc}）") from exc

    duration = frame_count / frame_rate if frame_rate else 0.0
    unchanged = SilenceTrimResult(
        source_duration=duration,
        trim_start=0.0,
        trim_end=duration,
        output_duration=duration,
        leading_silence=0.0,
        trailing_silence=0.0,
        speech_detected=False,
        trimmed=False,
    )
    if (
        not enabled
        or frame_rate <= 0
        or frame_count <= 0
        or channels <= 0
        or sample_width != 2
    ):
        _copy_atomic(input_path, output_path)
        return unchanged

    samples = array.array("h")
    samples.frombytes(payload)
    if sys.byteorder != "little":
        samples.byteswap()
    samples_per_window = max(channels, round(frame_rate * max(1.0, frame_ms) / 1000) * channels)
    window_frames = max(1, samples_per_window // channels)
    rms_values: list[float] = []
    for offset in range(0, len(samples), samples_per_window):
        block = samples[offset : offset + samples_per_window]
        if not block:
            continue
        square_sum = sum(int(value) * int(value) for value in block)
        rms_values.append(math.sqrt(square_sum / len(block)))

    peak_rms = max(rms_values, default=0.0)
    absolute_threshold = 32767.0 * 10 ** (float(threshold_db) / 20.0)
    relative_threshold = peak_rms * 10 ** (float(relative_db) / 20.0)
    active_threshold = max(1.0, absolute_threshold, relative_threshold)
    active = [index for index, value in enumerate(rms_values) if value >= active_threshold]
    if not active:
        _copy_atomic(input_path, output_path)
        return unchanged

    padding_frames = max(0, round(frame_rate * max(0.0, padding_ms) / 1000))
    first_frame = max(0, active[0] * window_frames - padding_frames)
    last_frame = min(
        frame_count,
        (active[-1] + 1) * window_frames + padding_frames,
    )
    if last_frame <= first_frame:
        _copy_atomic(input_path, output_path)
        return unchanged

    leading = first_frame / frame_rate
    trailing = max(0.0, (frame_count - last_frame) / frame_rate)
    trimmed = bool(first_frame > 0 or last_frame < frame_count)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.stem}-{os.getpid()}.tmp.wav")
    temporary.unlink(missing_ok=True)
    try:
        with wave.open(str(temporary), "wb") as handle:
            handle.setparams(params)
            byte_start = first_frame * channels * sample_width
            byte_end = last_frame * channels * sample_width
            handle.writeframes(payload[byte_start:byte_end])
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)

    output_duration = (last_frame - first_frame) / frame_rate
    return SilenceTrimResult(
        source_duration=duration,
        trim_start=leading,
        trim_end=last_frame / frame_rate,
        output_duration=output_duration,
        leading_silence=leading,
        trailing_silence=trailing,
        speech_detected=True,
        trimmed=trimmed,
    )


def _group_rows(rows: list[dict[str, Any]], max_gap: float) -> list[list[dict[str, Any]]]:
    if not rows:
        return []
    groups: list[list[dict[str, Any]]] = [[rows[0]]]
    for row in rows[1:]:
        previous = groups[-1][-1]
        gap = float(row["start"]) - float(previous["end"])
        if gap <= max_gap:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def schedule_speech_regions(
    segments: Iterable[dict[str, Any]],
    *,
    media_duration: float,
    region_max_gap: float = 0.5,
    internal_gap: float = 0.04,
    boundary_gap: float = 0.05,
    max_extension: float = 1.0,
    max_stretch_ratio: float = 1.3,
    max_alignment_shift: float = 1.5,
    overlap_tolerance: float = 0.02,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build a non-overlapping TTS schedule without changing subtitle timing.

    Dense adjacent cues share one timing budget. The returned rows preserve the
    original ``start``/``end`` fields and add ``scheduled_start`` plus a common
    region speed. Structural review is based on the final schedule, not the raw
    untrimmed TTS duration.
    """

    rows = sorted((dict(row) for row in segments), key=lambda item: (float(item["start"]), int(item["index"])))
    if not rows:
        return [], {
            "status": "FAILED",
            "reason": "NO_SEGMENTS",
            "region_count": 0,
            "review_region_count": 0,
        }
    duration_limit = max(0.05, float(media_duration))
    max_gap = max(0.0, float(region_max_gap))
    cue_gap = max(0.0, float(internal_gap))
    group_gap = max(0.0, float(boundary_gap))
    extension = max(0.0, float(max_extension))
    speed_limit = max(1.0, float(max_stretch_ratio))
    shift_limit = max(0.0, float(max_alignment_shift))
    tolerance = max(0.0, float(overlap_tolerance))
    groups = _group_rows(rows, max_gap)
    scheduled: list[dict[str, Any]] = []
    region_summaries: list[dict[str, Any]] = []
    previous_scheduled_end = 0.0

    for group_index, group in enumerate(groups, 1):
        original_start = max(0.0, float(group[0]["start"]))
        region_start = max(original_start, previous_scheduled_end + (group_gap if scheduled else 0.0))
        original_end = max(float(group[-1]["end"]), original_start + 0.05)
        next_start = (
            float(groups[group_index][0]["start"])
            if group_index < len(groups)
            else duration_limit
        )
        deadline = min(duration_limit, original_end + extension)
        if group_index < len(groups):
            deadline = min(deadline, next_start - group_gap)
        deadline = max(region_start + 0.05, deadline)
        available = max(0.05, deadline - region_start)
        spoken = [max(0.001, float(row.get("spoken_duration") or row.get("generated_duration") or 0.0)) for row in group]
        occupied = sum(spoken) + cue_gap * max(0, len(group) - 1)
        required_speed = max(1.0, occupied / available)
        speed = min(required_speed, speed_limit)
        scaled = [value / speed for value in spoken]
        cannot_fit = occupied / speed_limit > available + tolerance

        positions: list[float] = []
        for offset, row in enumerate(group):
            earliest = region_start if offset == 0 else positions[-1] + scaled[offset - 1] + cue_gap
            positions.append(max(float(row["start"]), earliest))
        scheduled_end = positions[-1] + scaled[-1]
        if not cannot_fit and scheduled_end > deadline + tolerance:
            positions[-1] = deadline - scaled[-1]
            for offset in range(len(group) - 2, -1, -1):
                positions[offset] = min(
                    positions[offset],
                    positions[offset + 1] - cue_gap - scaled[offset],
                )
            scheduled_end = positions[-1] + scaled[-1]

        shifts = [positions[offset] - float(row["start"]) for offset, row in enumerate(group)]
        max_abs_shift = max((abs(value) for value in shifts), default=0.0)
        starts_before_region = positions[0] < region_start - tolerance
        extends_past_deadline = scheduled_end > deadline + tolerance
        alignment_exceeded = max_abs_shift > shift_limit + tolerance
        reasons: list[str] = []
        if cannot_fit or extends_past_deadline:
            reasons.append("REGION_DURATION_OVERFLOW")
        if alignment_exceeded:
            reasons.append("ALIGNMENT_SHIFT_EXCEEDED")
        if starts_before_region:
            reasons.append("REGION_START_UNDERFLOW")
        needs_review = bool(reasons)

        for offset, row in enumerate(group):
            item = dict(row)
            item.update(
                scheduled_start=max(0.0, positions[offset]),
                schedule_speed_factor=speed,
                schedule_required_speed=required_speed,
                schedule_shift=shifts[offset],
                schedule_region=group_index,
                schedule_needs_review=needs_review,
                schedule_reasons=list(reasons),
            )
            scheduled.append(item)
        previous_scheduled_end = max(previous_scheduled_end, scheduled_end)
        region_summaries.append(
            {
                "region": group_index,
                "first_segment": int(group[0]["index"]),
                "last_segment": int(group[-1]["index"]),
                "segment_count": len(group),
                "start": region_start,
                "deadline": deadline,
                "available_duration": available,
                "spoken_duration": occupied,
                "required_speed_factor": required_speed,
                "speed_factor": speed,
                "scheduled_end": scheduled_end,
                "max_alignment_shift": max_abs_shift,
                "needs_review": needs_review,
                "reasons": reasons,
            }
        )

    review_regions = [item for item in region_summaries if item["needs_review"]]
    review_segments = [item for item in scheduled if item["schedule_needs_review"]]
    final_end = max(
        (
            float(item["scheduled_start"])
            + float(item.get("spoken_duration") or item.get("generated_duration") or 0.0)
            / max(1.0, float(item["schedule_speed_factor"]))
            for item in scheduled
        ),
        default=0.0,
    )
    qc = {
        "status": "REVIEW_REQUIRED" if review_regions else "PASS_AUTO_ADAPTED",
        "region_count": len(region_summaries),
        "review_region_count": len(review_regions),
        "segment_count": len(scheduled),
        "review_segment_count": len(review_segments),
        "max_required_speed_factor": max((float(item["required_speed_factor"]) for item in region_summaries), default=1.0),
        "max_applied_speed_factor": max((float(item["speed_factor"]) for item in region_summaries), default=1.0),
        "max_alignment_shift": max((float(item["max_alignment_shift"]) for item in region_summaries), default=0.0),
        "final_speech_end": final_end,
        "media_duration": duration_limit,
        "no_voice_overlap": all(
            float(scheduled[index]["scheduled_start"])
            + float(scheduled[index].get("spoken_duration") or 0.0)
            / max(1.0, float(scheduled[index]["schedule_speed_factor"]))
            <= float(scheduled[index + 1]["scheduled_start"]) + tolerance
            for index in range(len(scheduled) - 1)
        ),
        "regions": region_summaries,
    }
    return scheduled, qc


__all__ = ["SilenceTrimResult", "schedule_speech_regions", "trim_wav_silence"]
