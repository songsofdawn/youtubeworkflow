from __future__ import annotations

import os
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .demucs import run_checked, valid_wav


@dataclass(frozen=True)
class TimingPlan:
    start: float
    subtitle_end: float
    available_end: float
    available_duration: float
    generated_duration: float
    ratio: float
    speed_factor: float
    final_duration: float
    needs_review: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def wav_duration(path: Path | str) -> float:
    source = Path(path)
    try:
        with wave.open(str(source), "rb") as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else 0.0
    except (OSError, EOFError, wave.Error) as exc:
        raise RuntimeError(f"无法读取 WAV 时长：{source}（{exc}）") from exc


def calculate_available_end(
    *,
    start: float,
    end: float,
    next_start: float | None,
    media_duration: float,
    min_gap: float = 0.2,
    max_extension: float = 1.0,
) -> float:
    candidates = [float(end) + max(0.0, float(max_extension))]
    if next_start is not None:
        candidates.append(float(next_start) - max(0.0, float(min_gap)))
    if media_duration > 0:
        candidates.append(float(media_duration))
    return max(float(start) + 0.05, min(candidates))


def plan_duration(
    *,
    start: float,
    end: float,
    next_start: float | None,
    media_duration: float,
    generated_duration: float,
    min_gap: float = 0.2,
    max_extension: float = 1.0,
    direct_accept_ratio: float = 1.10,
    max_stretch_ratio: float = 1.30,
) -> TimingPlan:
    available_end = calculate_available_end(
        start=start,
        end=end,
        next_start=next_start,
        media_duration=media_duration,
        min_gap=min_gap,
        max_extension=max_extension,
    )
    available_duration = max(0.05, available_end - float(start))
    duration = max(0.0, float(generated_duration))
    ratio = duration / available_duration if available_duration else float("inf")
    if ratio <= float(direct_accept_ratio):
        speed = 1.0
        review = False
        reason = "direct"
    elif ratio <= float(max_stretch_ratio):
        speed = max(1.0, ratio)
        review = False
        reason = "speed_adjusted"
    else:
        speed = max(1.0, float(max_stretch_ratio))
        review = True
        reason = "significantly_over_target"
    final_duration = duration / speed if speed else duration
    return TimingPlan(
        start=float(start),
        subtitle_end=float(end),
        available_end=available_end,
        available_duration=available_duration,
        generated_duration=duration,
        ratio=ratio,
        speed_factor=speed,
        final_duration=final_duration,
        needs_review=review,
        reason=reason,
    )


def adapt_segment(
    source: Path | str,
    destination: Path | str,
    plan: TimingPlan,
    *,
    ffmpeg_path: Path | str,
    log: Callable[[str], None] | None = None,
    command_runner: Callable[..., None] = run_checked,
) -> Path:
    input_path = Path(source).resolve()
    if not valid_wav(input_path):
        raise RuntimeError(f"待适配的 TTS WAV 不存在或无效：{input_path}")
    if plan.speed_factor <= 1.000001:
        return input_path
    output_path = Path(destination).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.stem}-{os.getpid()}.tmp.wav")
    temporary.unlink(missing_ok=True)
    try:
        command_runner(
            [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                input_path,
                "-filter:a",
                f"atempo={plan.speed_factor:.8f}",
                "-c:a",
                "pcm_s16le",
                temporary,
            ],
            cwd=output_path.parent,
            log=log,
        )
        if not valid_wav(temporary):
            raise RuntimeError(f"FFmpeg 时长适配没有生成有效 WAV：{output_path}")
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return output_path


__all__ = [
    "TimingPlan",
    "adapt_segment",
    "calculate_available_end",
    "plan_duration",
    "wav_duration",
]
