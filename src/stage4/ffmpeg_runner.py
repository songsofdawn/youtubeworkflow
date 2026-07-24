from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import CommandResult, Stage4Error
from .stage4_manifest import atomic_write_text


MP4_COPY_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "alac"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def readable_command(command: Iterable[str | Path]) -> str:
    values: list[str] = []
    for item in command:
        value = str(item)
        if not value or re.search(r'[\s"&|<>^()]', value):
            value = '"' + value.replace('"', '\\"') + '"'
        values.append(value)
    return " ".join(values)


def escape_filter_path(path: Path | str) -> str:
    value = Path(path).resolve().as_posix()
    value = value.replace("\\", r"\\")
    value = value.replace(":", r"\:")
    value = value.replace("'", r"\'")
    value = value.replace("[", r"\[").replace("]", r"\]")
    value = value.replace(",", r"\,").replace(";", r"\;")
    return value


def build_softsub_command(
    ffmpeg_path: Path | str,
    source_video: Path | str,
    ass_path: Path | str,
    output_path: Path | str,
    *,
    existing_subtitle_count: int = 0,
    preserve_existing_subtitles: bool = True,
    preserve_metadata: bool = True,
    preserve_chapters: bool = True,
) -> list[str]:
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-y",
        "-i",
        str(source_video),
        "-i",
        str(ass_path),
        "-map",
        "0:v?",
        "-map",
        "0:a?",
    ]
    if preserve_existing_subtitles:
        command.extend(["-map", "0:s?"])
    command.extend(["-map", "1:0"])
    if preserve_metadata:
        command.extend(["-map_metadata", "0"])
    if preserve_chapters:
        command.extend(["-map_chapters", "0"])
    new_index = existing_subtitle_count if preserve_existing_subtitles else 0
    command.extend(["-c:v", "copy", "-c:a", "copy", "-c:s", "copy"])
    command.extend([f"-c:s:{new_index}", "ass"])
    for index in range(new_index):
        command.extend([f"-disposition:s:{index}", "0"])
    command.extend(
        [
            f"-metadata:s:s:{new_index}",
            "language=mul",
            f"-metadata:s:s:{new_index}",
            "title=English / 中文",
            f"-disposition:s:{new_index}",
            "default",
            str(output_path),
        ]
    )
    return command


def audio_copy_supported_for_mp4(probe: dict[str, Any]) -> bool:
    codecs = {
        str(stream.get("codec") or "").casefold()
        for stream in probe.get("audio_streams", [])
    }
    return bool(codecs) and codecs.issubset(MP4_COPY_AUDIO_CODECS)


def select_audio_mode(
    source_probe: dict[str, Any],
    *,
    require_audio_copy: bool = False,
) -> tuple[str, bool, list[str]]:
    if audio_copy_supported_for_mp4(source_probe):
        return "copy", False, []
    if require_audio_copy:
        raise Stage4Error(
            "AUDIO_COPY_NOT_SUPPORTED",
            "源音频编码无法直接封装进 MP4，且已启用 --require-audio-copy。",
            details={
                "codecs": [stream.get("codec") for stream in source_probe.get("audio_streams", [])]
            },
        )
    return "aac", True, ["AUDIO_TRANSCODE_REQUIRED"]


def build_hardsub_command(
    ffmpeg_path: Path | str,
    source_video: Path | str,
    ass_path: Path | str,
    output_path: Path | str,
    *,
    video_encoder: str,
    audio_mode: str,
    render_config: dict[str, Any],
) -> list[str]:
    filter_value = f"ass=filename='{escape_filter_path(ass_path)}'"
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-y",
        "-i",
        str(source_video),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        "-vf",
        filter_value,
    ]
    if video_encoder == "h264_nvenc":
        command.extend(
            [
                "-c:v",
                "h264_nvenc",
                "-preset",
                str(render_config.get("nvenc_preset", "p6")),
                "-rc",
                "vbr",
                "-cq",
                str(render_config.get("nvenc_cq", 19)),
                "-b:v",
                "0",
            ]
        )
    elif video_encoder == "libx264":
        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                str(render_config.get("x264_preset", "medium")),
                "-crf",
                str(render_config.get("x264_crf", 18)),
            ]
        )
    else:
        raise Stage4Error("VIDEO_ENCODER_INVALID", f"不支持的视频编码器：{video_encoder}")
    if audio_mode == "copy":
        command.extend(["-c:a", "copy"])
    else:
        command.extend(["-c:a", "aac", "-b:a", str(render_config.get("aac_bitrate", "192k"))])
    command.extend(["-movflags", "+faststart", str(output_path)])
    return command


def detect_nvenc(ffmpeg_path: Path | str) -> bool:
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=64x64:r=1:d=0.1",
        "-frames:v",
        "1",
        "-c:v",
        "h264_nvenc",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def resolve_video_encoder(requested: str, ffmpeg_path: Path | str) -> str:
    if requested == "libx264":
        return requested
    available = detect_nvenc(ffmpeg_path)
    if requested == "h264_nvenc":
        if not available:
            raise Stage4Error("NVENC_NOT_AVAILABLE", "已指定 h264_nvenc，但当前环境无法实际使用 NVENC。")
        return requested
    if requested != "auto":
        raise Stage4Error("VIDEO_ENCODER_INVALID", f"不支持的视频编码器：{requested}")
    return "h264_nvenc" if available else "libx264"


class FFmpegRunner:
    def __init__(self, log_path: Path | str, *, cwd: Path | str | None = None) -> None:
        self.log_path = Path(log_path)
        self.cwd = Path(cwd).resolve() if cwd else None

    def _append_log(self, payload: dict[str, Any]) -> None:
        previous = ""
        if self.log_path.is_file():
            previous = self.log_path.read_text(encoding="utf-8", errors="replace")
        atomic_write_text(
            self.log_path,
            previous + json.dumps(payload, ensure_ascii=False) + "\n",
        )

    def run(self, command: Iterable[str | Path]) -> CommandResult:
        args = [str(item) for item in command]
        if "-progress" not in args:
            insertion = args.index("-hide_banner") + 1 if "-hide_banner" in args else 1
            args[insertion:insertion] = ["-progress", "pipe:1", "-nostats"]
        started_at = utc_now()
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                args,
                cwd=str(self.cwd) if self.cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=1,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            stderr_lines: list[str] = []

            def consume_stderr() -> None:
                if process.stderr is not None:
                    stderr_lines.extend(process.stderr)

            stderr_thread = threading.Thread(target=consume_stderr, daemon=True)
            stderr_thread.start()
            stdout_lines: list[str] = []
            last_progress_print = started
            if process.stdout is not None:
                for line in process.stdout:
                    stdout_lines.append(line)
                    if line.startswith("out_time=") and time.monotonic() - last_progress_print >= 10:
                        print(f"[stage4] FFmpeg 进度 {line.split('=', 1)[1].strip()}", flush=True)
                        last_progress_print = time.monotonic()
            returncode = process.wait()
            stderr_thread.join()
            stdout = "".join(stdout_lines)
            stderr = "".join(stderr_lines)
        except (OSError, ValueError) as exc:
            returncode, stdout, stderr = None, "", str(exc)
        result = CommandResult(
            command=args,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            started_at=started_at,
            finished_at=utc_now(),
            processing_seconds=round(time.monotonic() - started, 3),
        )
        payload = result.to_dict()
        payload["readable_command"] = readable_command(args)
        self._append_log(payload)
        return result


def temporary_output_path(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # The final filename is descriptive, while the temporary filename must stay
    # short enough for long Windows task directories.
    kind = "softsub" if "softsub" in destination.stem.casefold() else "hardsub"
    return destination.with_name(f".{kind}-{os.getpid()}.tmp{destination.suffix}")
