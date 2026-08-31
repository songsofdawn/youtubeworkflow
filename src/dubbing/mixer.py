from __future__ import annotations

import os
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
        delay_ms = max(0, round(float(row.get("start") or 0) * 1000))
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


def mix_background(
    background_path: Path | str,
    voice_path: Path | str,
    output_path: Path | str,
    *,
    ffmpeg_path: Path | str,
    duck_db: float = 6.0,
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
    limit = max(0.1, min(float(limiter), 1.0))
    if duck > 0:
        ratio = max(2.0, duck)
        filter_value = (
            f"[0:a]aresample={int(sample_rate)},"
            "aformat=sample_fmts=fltp:channel_layouts=stereo[bg];"
            f"[1:a]aresample={int(sample_rate)},"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=2[voice][key];"
            f"[bg][key]sidechaincompress=threshold=0.015:ratio={ratio:.3f}:"
            "attack=20:release=250:knee=2.8[ducked];"
            f"[ducked][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
            f"alimiter=limit={limit:.3f}[out]"
        )
    else:
        filter_value = (
            f"[0:a]aresample={int(sample_rate)}[bg];"
            f"[1:a]aresample={int(sample_rate)}[voice];"
            f"[bg][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
            f"alimiter=limit={limit:.3f}[out]"
        )
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
        "-filter_complex",
        filter_value,
        "-map",
        "[out]",
    ]
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
        command_runner(command, cwd=destination.parent, log=log)
        if not valid_wav(temporary):
            raise RuntimeError("中配混音失败：FFmpeg 输出无效")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


__all__ = [
    "build_chinese_voice_track",
    "build_timeline_filter",
    "mix_background",
]
