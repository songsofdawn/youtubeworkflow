from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .models import Stage4Error


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def ratio_to_float(value: Any) -> float:
    text = str(value or "0/0")
    if "/" not in text:
        return _float(text)
    numerator, denominator = text.split("/", 1)
    divisor = _float(denominator)
    return _float(numerator) / divisor if divisor else 0.0


def _rotation(stream: dict[str, Any]) -> int:
    candidates: list[Any] = [stream.get("tags", {}).get("rotate")]
    candidates.extend(item.get("rotation") for item in stream.get("side_data_list", []))
    for value in candidates:
        try:
            return int(round(float(value))) % 360
        except (TypeError, ValueError):
            continue
    return 0


def summarize_probe(data: dict[str, Any], path: Path | str) -> dict[str, Any]:
    streams = data.get("streams", [])
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    primary = video_streams[0] if video_streams else {}
    rotation = _rotation(primary)
    width = int(primary.get("width") or 0)
    height = int(primary.get("height") or 0)
    display_width, display_height = (height, width) if rotation in {90, 270} else (width, height)
    duration_values = [data.get("format", {}).get("duration")]
    duration_values.extend(stream.get("duration") for stream in streams)
    duration = max((_float(value) for value in duration_values), default=0.0)
    format_data = data.get("format", {})
    return {
        "path": str(Path(path).resolve()),
        "format_name": str(format_data.get("format_name") or ""),
        "duration": duration,
        "size": int(_float(format_data.get("size"))),
        "bit_rate": int(_float(format_data.get("bit_rate"))),
        "video_stream_count": len(video_streams),
        "video_codec": str(primary.get("codec_name") or ""),
        "width": width,
        "height": height,
        "display_width": display_width,
        "display_height": display_height,
        "sample_aspect_ratio": str(primary.get("sample_aspect_ratio") or ""),
        "display_aspect_ratio": str(primary.get("display_aspect_ratio") or ""),
        "frame_rate": str(primary.get("avg_frame_rate") or primary.get("r_frame_rate") or ""),
        "frame_rate_value": ratio_to_float(
            primary.get("avg_frame_rate") or primary.get("r_frame_rate")
        ),
        "pixel_format": str(primary.get("pix_fmt") or ""),
        "rotation": rotation,
        "color": {
            "range": str(primary.get("color_range") or ""),
            "space": str(primary.get("color_space") or ""),
            "transfer": str(primary.get("color_transfer") or ""),
            "primaries": str(primary.get("color_primaries") or ""),
        },
        "audio_stream_count": len(audio_streams),
        "audio_streams": [
            {
                "index": stream.get("index"),
                "codec": str(stream.get("codec_name") or ""),
                "channels": int(stream.get("channels") or 0),
                "channel_layout": str(stream.get("channel_layout") or ""),
                "sample_rate": str(stream.get("sample_rate") or ""),
                "duration": _float(stream.get("duration") or duration),
                "tags": stream.get("tags", {}),
                "disposition": stream.get("disposition", {}),
            }
            for stream in audio_streams
        ],
        "subtitle_stream_count": len(subtitle_streams),
        "subtitle_streams": [
            {
                "index": stream.get("index"),
                "codec": str(stream.get("codec_name") or ""),
                "tags": stream.get("tags", {}),
                "disposition": stream.get("disposition", {}),
            }
            for stream in subtitle_streams
        ],
        "chapters": [
            {
                "id": chapter.get("id"),
                "start_time": chapter.get("start_time"),
                "end_time": chapter.get("end_time"),
                "tags": chapter.get("tags", {}),
            }
            for chapter in data.get("chapters", [])
        ],
    }


def probe_media(ffprobe_path: Path | str, media_path: Path | str) -> dict[str, Any]:
    path = Path(media_path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise Stage4Error("MEDIA_NOT_FOUND", f"媒体文件不存在或为空：{path}")
    command = [
        str(ffprobe_path),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-show_chapters",
        "-of",
        "json",
        str(path),
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
        )
    except OSError as exc:
        raise Stage4Error("FFPROBE_EXECUTION_FAILED", str(exc)) from exc
    if completed.returncode != 0:
        raise Stage4Error(
            "FFPROBE_FAILED",
            f"FFprobe 无法解析媒体：{path}",
            details={"stderr": (completed.stderr or "")[-2000:]},
        )
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise Stage4Error("FFPROBE_JSON_INVALID", f"FFprobe 返回无效 JSON：{exc}") from exc
    summary = summarize_probe(data, path)
    if not summary["video_stream_count"]:
        raise Stage4Error("SOURCE_VIDEO_STREAM_NOT_FOUND", f"媒体缺少视频轨：{path}")
    if not summary["audio_stream_count"]:
        raise Stage4Error("SOURCE_AUDIO_STREAM_NOT_FOUND", f"媒体缺少原始音频轨：{path}")
    if summary["duration"] <= 0:
        raise Stage4Error("SOURCE_DURATION_INVALID", f"媒体时长无效：{path}")
    return summary


def tool_version(tool_path: Path | str) -> str:
    try:
        completed = subprocess.run(
            [str(tool_path), "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
    except OSError:
        return ""
    return (completed.stdout or completed.stderr or "").splitlines()[0].strip()

