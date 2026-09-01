from __future__ import annotations

import array
import math
import os
import sys
import wave
from pathlib import Path
from typing import Any, Callable, Iterable

from ..stage4.stage4_manifest import atomic_write_text
from .demucs import run_checked, valid_wav


def _relative_filter_path(path: Path, root: Path) -> str:
    try:
        value = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"时间轴音频必须位于中配工作目录内：{path}") from exc
    return value.replace("\\", "\\\\").replace("'", r"\'")


def build_timeline_filter(
    segments: Iterable[dict[str, Any]],
    *,
    work_dir: Path | str,
    media_duration: float,
    sample_rate: int = 48000,
) -> str:
    root = Path(work_dir).resolve()
    rows = list(segments)
    if not rows:
        raise ValueError("没有可用于生成中文人声音轨的字幕片段")
    duration = max(0.1, float(media_duration))
    chains = [
        f"anullsrc=channel_layout=stereo:sample_rate={int(sample_rate)},"
        f"atrim=duration={duration:.6f}[base]"
    ]
    labels = ["[base]"]
    for offset, row in enumerate(rows):
        path = Path(str(row["final_wav"])).resolve()
        if not valid_wav(path):
            raise RuntimeError(f"时间轴片段不存在或无效：{path}")
        relative = _relative_filter_path(path, root)
        delay_ms = max(
            0,
            round(
                float(
                    row.get("scheduled_start")
                    if row.get("scheduled_start") is not None
                    else row.get("start") or 0
                )
                * 1000
            ),
        )
        label = f"voice{offset}"
        chains.append(
            f"amovie=filename='{relative}',aresample={int(sample_rate)},"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={delay_ms}:all=1[{label}]"
        )
        labels.append(f"[{label}]")
    chains.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0,"
        f"alimiter=limit=0.95,atrim=duration={duration:.6f}[out]"
    )
    return ";\n".join(chains) + "\n"


def build_chinese_voice_track(
    segments: Iterable[dict[str, Any]],
    output_path: Path | str,
    *,
    work_dir: Path | str,
    media_duration: float,
    ffmpeg_path: Path | str,
    sample_rate: int = 48000,
    log: Callable[[str], None] | None = None,
    command_runner: Callable[..., None] = run_checked,
) -> Path:
    root = Path(work_dir).resolve()
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    filter_path = root / "timeline_filter.txt"
    filter_text = build_timeline_filter(
        segments,
        work_dir=root,
        media_duration=media_duration,
        sample_rate=sample_rate,
    )
    atomic_write_text(filter_path, filter_text)
    temporary = destination.with_name(f".{destination.stem}-{os.getpid()}.tmp.wav")
    temporary.unlink(missing_ok=True)
    try:
        command = [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-/filter_complex",
                filter_path,
                "-map",
                "[out]",
                "-c:a",
                "pcm_s16le",
                "-ar",
                str(int(sample_rate)),
                "-ac",
                "2",
                temporary,
            ]
        try:
            command_runner(command, cwd=root, log=log)
        except RuntimeError as exc:
            if "Unrecognized option" not in str(exc) or "filter_complex" not in str(exc):
                raise
            # FFmpeg before the generic -/option file syntax used this legacy name.
            legacy = list(command)
            option_index = legacy.index("-/filter_complex")
            legacy[option_index : option_index + 2] = [
                "-filter_complex_script",
                filter_path,
            ]
            command_runner(legacy, cwd=root, log=log)
        if not valid_wav(temporary):
            raise RuntimeError("生成完整中文人声音轨失败：FFmpeg 输出无效")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def merge_speech_intervals(
    segments: Iterable[dict[str, Any]],
    *,
    media_duration: float = 0.0,
    attack_ms: float = 40.0,
    release_ms: float = 250.0,
) -> list[tuple[float, float]]:
    """Return actual spoken intervals, joining gaps that would cause pumping."""

    duration_limit = max(0.0, float(media_duration))
    rows: list[tuple[float, float]] = []
    for segment in segments:
        start = max(
            0.0,
            float(
                segment.get("scheduled_start")
                if segment.get("scheduled_start") is not None
                else segment.get("start") or 0.0
            ),
        )
        if "final_duration" in segment:
            end = start + max(0.0, float(segment.get("final_duration") or 0.0))
        else:
            end = max(start, float(segment.get("end") or start))
        if duration_limit > 0:
            start = min(start, duration_limit)
            end = min(end, duration_limit)
        if end > start:
            rows.append((start, end))
    rows.sort()
    if not rows:
        return []
    merge_gap = (max(0.0, float(attack_ms)) + max(0.0, float(release_ms))) / 1000.0
    merged: list[list[float]] = [[rows[0][0], rows[0][1]]]
    for start, end in rows[1:]:
        previous = merged[-1]
        if start <= previous[1] + merge_gap:
            previous[1] = max(previous[1], end)
        else:
            merged.append([start, end])
    return [(round(start, 6), round(end, 6)) for start, end in merged]


def build_ducking_filter(
    speech_intervals: Iterable[dict[str, Any]],
    *,
    duck_db: float,
    attack_ms: float = 40.0,
    release_ms: float = 250.0,
    sample_rate: int = 48000,
    limiter: float = 0.95,
    media_duration: float = 0.0,
) -> str:
    duck = max(0.0, min(float(duck_db), 18.0))
    limit = max(0.1, min(float(limiter), 1.0))
    intervals = merge_speech_intervals(
        speech_intervals,
        media_duration=media_duration,
        attack_ms=attack_ms,
        release_ms=release_ms,
    )
    chains = [
        f"[0:a]aresample={int(sample_rate)},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo[bg]"
    ]
    if duck > 0 and intervals:
        chains.extend(
            [
                f"[2:a]aresample={int(sample_rate)},"
                "aformat=sample_fmts=fltp:channel_layouts=stereo[gain]",
                "[bg][gain]amultiply[ducked]",
            ]
        )
    else:
        chains.append("[bg]anull[ducked]")
    chains.extend([
        f"[1:a]aresample={int(sample_rate)},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo[voice]",
        "[ducked][voice]amix=inputs=2:duration=longest:"
        f"dropout_transition=0:normalize=0,alimiter=limit={limit:.3f}[out]",
    ])
    return ";\n".join(chains) + "\n"


def write_ducking_envelope(
    path: Path | str,
    speech_intervals: Iterable[dict[str, Any]],
    *,
    media_duration: float,
    duck_db: float,
    attack_ms: float = 40.0,
    release_ms: float = 250.0,
    envelope_rate: int = 1000,
) -> Path:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.001, float(media_duration))
    rate = max(100, int(envelope_rate))
    duck = max(0.0, min(float(duck_db), 18.0))
    attack = max(0.0, float(attack_ms)) / 1000.0
    release = max(0.0, float(release_ms)) / 1000.0
    intervals = merge_speech_intervals(
        speech_intervals,
        media_duration=duration,
        attack_ms=attack_ms,
        release_ms=release_ms,
    )
    sample_count = max(1, math.ceil(duration * rate))
    values = array.array("h", [32767]) * sample_count

    def set_gain(index: int, attenuation_db: float) -> None:
        if 0 <= index < sample_count:
            gain = 10 ** (-max(0.0, min(duck, attenuation_db)) / 20.0)
            values[index] = max(0, min(32767, round(gain * 32767)))

    for start, end in intervals:
        attack_start = max(0.0, start - attack)
        release_end = min(duration, end + release)
        for index in range(max(0, math.floor(attack_start * rate)), min(sample_count, math.ceil(start * rate))):
            position = index / rate
            fraction = (
                1.0
                if start <= attack_start
                else (position - attack_start) / (start - attack_start)
            )
            set_gain(index, duck * max(0.0, min(1.0, fraction)))
        hold_gain = max(0, min(32767, round((10 ** (-duck / 20.0)) * 32767)))
        hold_start = max(0, math.floor(start * rate))
        hold_end = min(sample_count, math.ceil(end * rate))
        if hold_end > hold_start:
            values[hold_start:hold_end] = array.array(
                "h", [hold_gain]
            ) * (hold_end - hold_start)
        for index in range(max(0, math.floor(end * rate)), min(sample_count, math.ceil(release_end * rate))):
            position = index / rate
            fraction = (
                0.0
                if release_end <= end
                else 1.0 - (position - end) / (release_end - end)
            )
            set_gain(index, duck * max(0.0, min(1.0, fraction)))
    if sys.byteorder != "little":
        values.byteswap()
    with wave.open(str(destination), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(values.tobytes())
    return destination


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            return float(handle.getnframes()) / max(1, handle.getframerate())
    except (OSError, EOFError, wave.Error):
        return 0.0


def mix_background(
    background_path: Path | str,
    voice_path: Path | str,
    output_path: Path | str,
    *,
    ffmpeg_path: Path | str,
    duck_db: float = 6.0,
    speech_intervals: Iterable[dict[str, Any]] = (),
    attack_ms: float = 40.0,
    release_ms: float = 250.0,
    sample_rate: int = 48000,
    limiter: float = 0.95,
    media_duration: float = 0.0,
    log: Callable[[str], None] | None = None,
    command_runner: Callable[..., None] = run_checked,
) -> Path:
    background = Path(background_path).resolve()
    voice = Path(voice_path).resolve()
    destination = Path(output_path).resolve()
    if not valid_wav(background):
        raise RuntimeError(f"背景音不存在或无效：{background}")
    if not valid_wav(voice):
        raise RuntimeError(f"中文人声音轨不存在或无效：{voice}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}-{os.getpid()}.tmp.wav")
    temporary.unlink(missing_ok=True)
    duck = max(0.0, min(float(duck_db), 18.0))
    interval_rows = list(speech_intervals)
    effective_duration = max(
        0.001,
        float(media_duration),
        _wav_duration(background),
        _wav_duration(voice),
    )
    active_intervals = merge_speech_intervals(
        interval_rows,
        media_duration=effective_duration,
        attack_ms=attack_ms,
        release_ms=release_ms,
    )
    filter_path = destination.parent / "ducking_filter.txt"
    envelope_path = destination.parent / "ducking_envelope.wav"
    envelope_path.unlink(missing_ok=True)
    if duck > 0 and active_intervals:
        write_ducking_envelope(
            envelope_path,
            interval_rows,
            media_duration=effective_duration,
            duck_db=duck,
            attack_ms=attack_ms,
            release_ms=release_ms,
        )
    filter_value = build_ducking_filter(
        interval_rows,
        duck_db=duck,
        attack_ms=attack_ms,
        release_ms=release_ms,
        sample_rate=sample_rate,
        limiter=limiter,
        media_duration=effective_duration,
    )
    atomic_write_text(filter_path, filter_value)
    command: list[Path | str] = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        background,
        "-i",
        voice,
    ]
    if envelope_path.is_file():
        command.extend(["-i", envelope_path])
    command.extend(["-/filter_complex", filter_path, "-map", "[out]"])
    if media_duration > 0:
        command.extend(["-t", f"{float(media_duration):.6f}"])
    command.extend(
        [
            "-c:a",
            "pcm_s16le",
            "-ar",
            str(int(sample_rate)),
            "-ac",
            "2",
            temporary,
        ]
    )
    try:
        try:
            command_runner(command, cwd=destination.parent, log=log)
        except RuntimeError as exc:
            if "Unrecognized option" not in str(exc) or "filter_complex" not in str(exc):
                raise
            legacy = list(command)
            option_index = legacy.index("-/filter_complex")
            legacy[option_index : option_index + 2] = [
                "-filter_complex_script",
                filter_path,
            ]
            command_runner(legacy, cwd=destination.parent, log=log)
        if not valid_wav(temporary):
            raise RuntimeError("中配混音失败：FFmpeg 输出无效")
        os.replace(temporary, destination)
        filter_path.unlink(missing_ok=True)
        envelope_path.unlink(missing_ok=True)
    except Exception:
        if log:
            log(
                "[DUBBING] Ducking diagnostics retained: "
                f"{filter_path}, {envelope_path}"
            )
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return destination


__all__ = [
    "build_chinese_voice_track",
    "build_ducking_filter",
    "build_timeline_filter",
    "merge_speech_intervals",
    "mix_background",
    "write_ducking_envelope",
]
