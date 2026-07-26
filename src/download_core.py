from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger("stage2_download")
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
MANIFEST_FIELDS = (
    "video_id", "url", "source_mode", "candidate_file", "candidate_rank",
    "rights_status", "selected", "title", "channel", "started_at", "finished_at",
    "video_status", "subtitle_status", "subtitle_source", "subtitle_tracks", "subtitle_clean_status", "subtitle_clean_stats", "vtt_status", "srt_status",
    "thumbnail_status", "metadata_status", "audio_status", "probe_status",
    "overall_status", "output_files", "commands_executed", "errors",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_project_paths(project_root: Path | str | None = None) -> dict[str, Path]:
    root = Path(project_root).resolve() if project_root else PROJECT_ROOT
    return {
        "project_root": root,
        "config": root / "config" / "download_config.json",
        "candidates": root / "candidates",
        "downloads": root / "downloads",
        "candidate_downloads": root / "downloads" / "candidates",
        "manual_downloads": root / "downloads" / "manual",
        "archive": root / "downloads" / "download_archive.txt",
        "tools_bin": root / "tools" / "bin",
        "cookies": root / "private" / "cookies.txt",
    }


def load_download_config(config_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else get_project_paths()["config"]
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if not path.is_file():
        raise FileNotFoundError(f"下载配置不存在: {path}")
    with path.open(encoding="utf-8-sig") as handle:
        config = json.load(handle)
    required = {
        "max_height", "format_selector", "video_container", "subtitle_languages",
        "extract_audio", "audio_sample_rate", "audio_channels", "audio_codec",
        "retries", "fragment_retries", "retry_sleep_seconds", "approved_rights_statuses",
    }
    missing = sorted(required - config.keys())
    if missing:
        raise ValueError(f"下载配置缺少字段: {', '.join(missing)}")
    if int(config["max_height"]) < 1 or int(config["retries"]) < 0 or int(config["fragment_retries"]) < 0:
        raise ValueError("下载配置中的高度和重试次数无效")
    return config


def find_local_tools(paths: dict[str, Path] | None = None) -> dict[str, Path]:
    paths = paths or get_project_paths()
    tools = {name: paths["tools_bin"] / f"{name}.exe" for name in ("yt-dlp", "ffmpeg", "ffprobe")}
    missing = [str(path) for path in tools.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("缺少项目本地工具: " + ", ".join(missing))
    return tools


def sanitize_windows_filename(value: str | None, video_id: str = "", max_length: int = 90) -> str:
    """Return a Windows-safe title component; add video_id only for empty names.

    Directory builders always add the video id separately, so normal titles do not duplicate it.
    """
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = re.sub(r'[<>:"/\\|?*]', "_", normalized)
    normalized = "".join(character for character in normalized if unicodedata.category(character) != "Cc")
    normalized = re.sub(r"[\s_]+", "_", normalized).strip(" ._")
    if normalized and normalized.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    normalized = normalized[:max(1, max_length)].rstrip(" ._")
    fallback = re.sub(r"[^A-Za-z0-9_-]", "_", video_id or "video").strip("_ .") or "video"
    return normalized or fallback


def _redact_argument(value: str) -> str:
    if not re.match(r"https?://", value, re.IGNORECASE):
        return value
    split = urlsplit(value)
    query = parse_qs(split.query)
    video_id = query.get("v", [""])[0]
    if video_id:
        return f"{split.scheme}://{split.netloc}{split.path}?v={video_id}"
    return f"{split.scheme}://{split.netloc}{split.path}"


def redact_command(command: Iterable[str | Path]) -> list[str]:
    values = [str(item) for item in command]
    redacted: list[str] = []
    hide_next = False
    for value in values:
        if hide_next:
            redacted.append("<cookies_file>")
            hide_next = False
        elif value == "--cookies":
            redacted.append(value)
            hide_next = True
        elif re.search(r"(?i)(api[_-]?key|token|signature)=", value):
            redacted.append("<redacted>")
        else:
            redacted.append(_redact_argument(value))
    return redacted


def run_command(
    command: Iterable[str | Path],
    cwd: Path | str | None = None,
    *,
    stream_output: bool = False,
) -> dict[str, Any]:
    args = [str(item) for item in command]
    safe_command = redact_command(args)
    LOGGER.info("执行命令: %s", safe_command)
    try:
        if stream_output:
            process = subprocess.Popen(
                args,
                cwd=str(cwd or PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            output: list[str] = []
            assert process.stdout is not None
            for line in process.stdout:
                output.append(line)
                sys.stdout.write(line)
                sys.stdout.flush()
            returncode = process.wait()
            result = {
                "success": returncode == 0,
                "returncode": returncode,
                "stdout": "".join(output),
                "stderr": "",
                "command": safe_command,
            }
        else:
            completed = subprocess.run(
                args,
                cwd=str(cwd or PROJECT_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
            )
            result = {
                "success": completed.returncode == 0,
                "returncode": completed.returncode,
                "stdout": completed.stdout or "",
                "stderr": completed.stderr or "",
                "command": safe_command,
            }
    except (OSError, ValueError) as exc:
        result = {"success": False, "returncode": None, "stdout": "", "stderr": str(exc), "command": safe_command}
    return result


def _short_error(result: dict[str, Any], limit: int = 500) -> str:
    text = (str(result.get("stderr", "")) or str(result.get("stdout", "")) or "命令执行失败").strip()
    text = re.sub(r"(?i)(--cookies\s+)(\S+)", r"\1<cookies_file>", text)
    return text[-limit:]


def _cookie_argument(config: dict[str, Any], paths: dict[str, Path]) -> tuple[list[str], str | None]:
    if not config.get("use_cookies", True):
        return [], None
    configured = Path(str(config.get("cookies_path", "private/cookies.txt")))
    cookie_path = configured if configured.is_absolute() else paths["project_root"] / configured
    if not cookie_path.is_file() or cookie_path.stat().st_size == 0:
        return [], None
    try:
        first_line = cookie_path.open(encoding="utf-8-sig", errors="replace").readline().strip()
    except OSError:
        first_line = ""
    if "# Netscape HTTP Cookie File" not in first_line:
        warning = "Cookies 文件格式异常，将先尝试不使用 Cookies；如遇登录、年龄限制或机器人验证，请重新导出 Netscape 格式 Cookies。"
        LOGGER.warning(warning)
        return [], warning
    return ["--cookies", str(cookie_path)], None


def _auth_hint(error_text: str) -> str:
    lowered = error_text.casefold()
    markers = ("sign in", "login", "age-restricted", "age restricted", "confirm you're not a bot", "cookies")
    if any(marker in lowered for marker in markers):
        return "检测到登录、年龄限制或机器人验证，请重新导出 Netscape 格式的 private/cookies.txt。"
    return ""


def probe_media(file_path: Path | str, ffprobe_path: Path | str | None = None, *, expected: str = "video") -> dict[str, Any]:
    path = Path(file_path)
    if not path.is_file() or path.stat().st_size <= 0:
        return {"success": False, "status": "failed", "error": "文件不存在或为空", "data": {}, "command_result": None}
    ffprobe = Path(ffprobe_path) if ffprobe_path else find_local_tools()["ffprobe"]
    command = [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path]
    result = run_command(command)
    if not result["success"]:
        return {"success": False, "status": "failed", "error": _short_error(result), "data": {}, "command_result": result}
    try:
        data = json.loads(result["stdout"])
    except (json.JSONDecodeError, TypeError) as exc:
        return {"success": False, "status": "failed", "error": f"ffprobe JSON 无法解析: {exc}", "data": {}, "command_result": result}
    streams = data.get("streams", [])
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    duration_values = [data.get("format", {}).get("duration")] + [stream.get("duration") for stream in streams]
    duration = 0.0
    for value in duration_values:
        try:
            duration = max(duration, float(value or 0))
        except (TypeError, ValueError):
            pass
    errors: list[str] = []
    if expected == "video":
        if not video:
            errors.append("缺少视频流")
        if not audio:
            errors.append("缺少音频流")
        if duration <= 0:
            errors.append("媒体时长无效")
    elif expected == "audio":
        if not audio:
            errors.append("缺少音频流")
        elif str(audio[0].get("sample_rate", "")) != "48000":
            errors.append("采样率不是 48000 Hz")
        if audio and int(audio[0].get("channels", 0) or 0) != 2:
            errors.append("声道数不是 2")
    return {"success": not errors, "status": "success" if not errors else "failed", "error": "; ".join(errors), "data": data, "command_result": result}


def fetch_video_metadata(url: str, tools: dict[str, Path] | None = None, config: dict[str, Any] | None = None, paths: dict[str, Path] | None = None) -> dict[str, Any]:
    paths = paths or get_project_paths()
    tools = tools or find_local_tools(paths)
    config = config or load_download_config()
    cookies, warning = _cookie_argument(config, paths)
    command = [
        tools["yt-dlp"], "--no-playlist", "--skip-download", "--dump-single-json", "--no-warnings",
        "--retries", str(config.get("retries", 10)), "--retry-sleep", str(config.get("retry_sleep_seconds", 2)),
        "--ffmpeg-location", paths["tools_bin"], *cookies, url,
    ]
    result = run_command(command, paths["project_root"])
    response = {"success": False, "metadata": {}, "command_result": result, "warning": warning, "error": ""}
    if not result["success"]:
        response["error"] = _short_error(result)
        hint = _auth_hint(response["error"])
        if hint:
            response["error"] += f" {hint}"
        return response
    try:
        payload = json.loads(result["stdout"])
    except (json.JSONDecodeError, TypeError) as exc:
        response["error"] = f"元数据 JSON 无法解析: {exc}"
        return response
    if not payload.get("id"):
        response["error"] = "元数据中缺少视频 ID"
        return response
    response.update(success=True, metadata=payload)
    return response


def _base_ytdlp_command(url: str, tools: dict[str, Path], paths: dict[str, Path], config: dict[str, Any]) -> tuple[list[str | Path], str | None]:
    cookies, warning = _cookie_argument(config, paths)
    return [tools["yt-dlp"], url, "--no-playlist"], warning


def _download_network_options(config: dict[str, Any]) -> list[str]:
    options = [
        "--socket-timeout",
        str(config.get("socket_timeout_seconds", 30)),
        "--http-chunk-size",
        str(config.get("http_chunk_size", "1M")),
        "--concurrent-fragments",
        str(config.get("concurrent_fragments", 1)),
        "--newline",
        "--progress-delta",
        "1",
    ]
    if config.get("force_ipv4", True):
        options.append("--force-ipv4")
    retry_ceiling = max(
        int(config.get("retry_sleep_seconds", 2)),
        int(config.get("retry_sleep_max_seconds", 20)),
    )
    options.extend(
        [
            "--retry-sleep",
            f"http:exp=1:{retry_ceiling}",
            "--retry-sleep",
            f"fragment:exp=1:{retry_ceiling}",
        ]
    )
    return options


def _is_transient_download_error(result: dict[str, Any]) -> bool:
    text = f"{result.get('stderr', '')}\n{result.get('stdout', '')}".casefold()
    markers = (
        "eof occurred in violation of protocol",
        "ssl",
        "tls",
        "connection reset",
        "connection aborted",
        "remote end closed",
        "incompleteread",
        "timed out",
        "timeout",
        "temporary failure",
        "http error 429",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
    )
    return any(marker in text for marker in markers)


def _download_network_hint(result: dict[str, Any]) -> str:
    if not _is_transient_download_error(result):
        return ""
    return (
        "下载链路无法与 YouTube 视频 CDN 建立稳定的 TLS 连接。"
        "若正在使用 Clash/VPN，请切换代理节点后在面板点击“重试”；"
        "已经成功的字幕、封面和元数据会被保留。"
    )


def download_video_media(url: str, task_dir: Path | str, tools: dict[str, Path] | None = None, config: dict[str, Any] | None = None, paths: dict[str, Path] | None = None, archive_path: Path | str | None = None, use_archive: bool = True) -> dict[str, Any]:
    paths = paths or get_project_paths(); tools = tools or find_local_tools(paths); config = config or load_download_config()
    task_dir = Path(task_dir); video_dir = task_dir / "video"; video_dir.mkdir(parents=True, exist_ok=True)
    base, warning = _base_ytdlp_command(url, tools, paths, config)
    cookies, _ = _cookie_argument(config, paths)
    command: list[str | Path] = [
        *base, "--continue", "--retries", str(config["retries"]), "--fragment-retries", str(config["fragment_retries"]),
        "--retry-sleep", str(config["retry_sleep_seconds"]), "--ffmpeg-location", paths["tools_bin"],
        "--format", str(config["format_selector"]), "--merge-output-format", str(config.get("video_container", "mp4")),
        "--remux-video", str(config.get("video_container", "mp4")), "--output", video_dir / "source.%(ext)s",
        "--no-write-playlist-metafiles", *_download_network_options(config), *cookies,
    ]
    if archive_path and use_archive:
        archive = Path(archive_path); archive.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--download-archive", archive])
    command_results: list[dict[str, Any]] = []
    recovery_attempts = max(0, int(config.get("network_recovery_attempts", 1)))
    for attempt in range(recovery_attempts + 1):
        if attempt:
            LOGGER.warning(
                "检测到临时网络中断，正在刷新媒体地址并从断点自动恢复（第 %s/%s 次）",
                attempt,
                recovery_attempts,
            )
        result = run_command(command, paths["project_root"], stream_output=True)
        command_results.append(result)
        if result["success"] or not _is_transient_download_error(result):
            break
    final = video_dir / "source.mp4"
    mp4_files = sorted((item for item in video_dir.glob("*.mp4") if item.is_file() and item.stat().st_size > 0), key=lambda item: item.stat().st_mtime, reverse=True)
    if mp4_files and mp4_files[0] != final:
        if final.exists():
            final.unlink()
        mp4_files[0].replace(final)
    success = result["success"] and final.is_file() and final.stat().st_size > 0
    error = "" if success else _short_error(result)
    network_hint = _download_network_hint(result)
    if network_hint:
        error = f"{error}\n{network_hint}".strip()
    hint = _auth_hint(error)
    if hint:
        error += f" {hint}"
    return {
        "success": success,
        "status": "success" if success else "failed",
        "file": final if success else None,
        "command_result": result,
        "command_results": command_results,
        "warning": warning,
        "error": error,
    }


def _subtitle_candidates(directory: Path, prefix: str) -> list[Path]:
    return sorted(
        (path for path in directory.glob(f"{prefix}.*") if path.is_file() and path.suffix.lower() in {".vtt", ".srt"}),
        key=lambda path: (path.suffix.lower() != ".vtt", path.name),
    )


def _normalize_vtt(directory: Path, prefix: str, destination: Path) -> bool:
    candidates = [path for path in _subtitle_candidates(directory, prefix) if path.suffix.lower() == ".vtt"]
    if not candidates:
        return False
    source = candidates[0]
    if source != destination:
        if destination.exists():
            destination.unlink()
        source.replace(destination)
    for extra in candidates[1:]:
        if extra != destination:
            extra.unlink(missing_ok=True)
    return destination.is_file()


def _download_subtitle_track(
    url: str,
    directory: Path,
    label: str,
    language_patterns: list[str],
    tools: dict[str, Path],
    config: dict[str, Any],
    paths: dict[str, Path],
    cookies: list[str],
) -> dict[str, Any]:
    commands: list[dict[str, Any]] = []
    source = ""
    vtt: Path | None = None
    for candidate_source in ("manual", "auto"):
        existing = directory / f"{label}.{candidate_source}.vtt"
        if existing.is_file() and existing.stat().st_size > 0:
            source, vtt = candidate_source, existing
            break
    languages = ",".join(str(item) for item in language_patterns)
    if not source and config.get("prefer_manual_subtitles", True):
        raw_prefix = f"{label}.manual_raw"
        command = [
            tools["yt-dlp"], url, "--no-playlist", "--skip-download", "--write-subs",
            "--sub-langs", languages, "--sub-format", "vtt/best", "--ffmpeg-location", paths["tools_bin"],
            "--output", directory / f"{raw_prefix}.%(ext)s", *cookies,
        ]
        manual_result = run_command(command, paths["project_root"]); commands.append(manual_result)
        destination = directory / f"{label}.manual.vtt"
        if _normalize_vtt(directory, raw_prefix, destination):
            source, vtt = "manual", destination
    if not source and config.get("fallback_to_auto_subtitles", True):
        raw_prefix = f"{label}.auto_raw"
        command = [
            tools["yt-dlp"], url, "--no-playlist", "--skip-download", "--write-auto-subs",
            "--sub-langs", languages, "--sub-format", "vtt/best", "--ffmpeg-location", paths["tools_bin"],
            "--output", directory / f"{raw_prefix}.%(ext)s", *cookies,
        ]
        auto_result = run_command(command, paths["project_root"]); commands.append(auto_result)
        destination = directory / f"{label}.auto.vtt"
        if _normalize_vtt(directory, raw_prefix, destination):
            source, vtt = "auto", destination
    if not source or vtt is None:
        return {
            "language": label, "status": "missing", "source": "", "vtt_status": "missing", "srt_status": "missing",
            "vtt_file": None, "srt_file": None, "command_results": commands, "error": "未找到字幕",
        }
    srt = directory / f"{label}.{source}.srt"
    srt_status = "success" if srt.is_file() and srt.stat().st_size > 0 else "not_requested"
    if config.get("create_srt", True) and srt_status != "success":
        conversion = run_command([tools["ffmpeg"], "-y", "-i", vtt, srt], paths["project_root"]); commands.append(conversion)
        srt_status = "success" if conversion["success"] and srt.is_file() and srt.stat().st_size > 0 else "failed"
    return {
        "language": label, "status": "success", "source": source, "vtt_status": "success", "srt_status": srt_status,
        "vtt_file": vtt, "srt_file": srt if srt.is_file() else None, "command_results": commands,
        "error": "" if srt_status != "failed" else "VTT 已保留，但 SRT 转换失败",
    }


def download_subtitles(url: str, task_dir: Path | str, tools: dict[str, Path] | None = None, config: dict[str, Any] | None = None, paths: dict[str, Path] | None = None) -> dict[str, Any]:
    paths = paths or get_project_paths(); tools = tools or find_local_tools(paths); config = config or load_download_config()
    task_dir = Path(task_dir); directory = task_dir / "subtitles"; directory.mkdir(parents=True, exist_ok=True)
    cookies, warning = _cookie_argument(config, paths)
    english = _download_subtitle_track(
        url, directory, "en", list(config.get("subtitle_languages", ["en.*", "en"])), tools, config, paths, cookies,
    )
    tracks = {"en": english}
    if config.get("download_chinese_subtitles", True):
        tracks["zh"] = _download_subtitle_track(
            url, directory, "zh",
            list(config.get("chinese_subtitle_languages", ["zh-Hans", "zh-CN", "zh-Hant", "zh-TW", "zh.*", "zh"])),
            tools, config, paths, cookies,
        )
    preferred = english
    if preferred["status"] != "success":
        preferred = next((track for track in tracks.values() if track["status"] == "success"), preferred)
    commands = [result for track in tracks.values() for result in track["command_results"]]
    conversion_errors = [f"{label}: {track['error']}" for label, track in tracks.items() if track.get("error") and track["status"] == "success"]
    return {
        "success": True,
        "status": "success" if any(track["status"] == "success" for track in tracks.values()) else "missing",
        "source": preferred["source"], "vtt_status": preferred["vtt_status"], "srt_status": preferred["srt_status"],
        "vtt_file": preferred["vtt_file"], "srt_file": preferred["srt_file"], "tracks": tracks,
        "command_results": commands, "warning": warning, "error": "; ".join(conversion_errors),
    }


def download_thumbnail(url: str, task_dir: Path | str, tools: dict[str, Path] | None = None, config: dict[str, Any] | None = None, paths: dict[str, Path] | None = None) -> dict[str, Any]:
    paths = paths or get_project_paths(); tools = tools or find_local_tools(paths); config = config or load_download_config()
    directory = Path(task_dir) / "metadata"; directory.mkdir(parents=True, exist_ok=True)
    final = directory / "thumbnail.jpg"
    if final.is_file() and final.stat().st_size > 0:
        return {"success": True, "status": "success", "file": final, "command_result": None, "error": ""}
    cookies, warning = _cookie_argument(config, paths)
    command = [tools["yt-dlp"], url, "--no-playlist", "--skip-download", "--write-thumbnail", "--convert-thumbnails", "jpg", "--ffmpeg-location", paths["tools_bin"], "--output", directory / "thumbnail.%(ext)s", *cookies]
    result = run_command(command, paths["project_root"])
    jpgs = sorted(directory.glob("thumbnail*.jpg"))
    if jpgs and jpgs[0] != final:
        if final.exists(): final.unlink()
        jpgs[0].replace(final)
    success = final.is_file() and final.stat().st_size > 0
    return {"success": success, "status": "success" if success else "failed", "file": final if success else None, "command_result": result, "warning": warning, "error": "" if success else _short_error(result)}


def extract_audio(video_file: Path | str, audio_file: Path | str, tools: dict[str, Path] | None = None, config: dict[str, Any] | None = None, paths: dict[str, Path] | None = None) -> dict[str, Any]:
    paths = paths or get_project_paths(); tools = tools or find_local_tools(paths); config = config or load_download_config()
    audio = Path(audio_file); audio.parent.mkdir(parents=True, exist_ok=True)
    command = [tools["ffmpeg"], "-y", "-i", Path(video_file), "-map", "0:a:0", "-vn", "-c:a", str(config.get("audio_codec", "pcm_s16le")), "-ar", str(config.get("audio_sample_rate", 48000)), "-ac", str(config.get("audio_channels", 2)), audio]
    result = run_command(command, paths["project_root"])
    success = result["success"] and audio.is_file() and audio.stat().st_size > 0
    return {"success": success, "status": "success" if success else "failed", "file": audio if success else None, "command_result": result, "error": "" if success else _short_error(result)}


def write_manifest(task_dir: Path | str, manifest: dict[str, Any]) -> Path:
    directory = Path(task_dir); directory.mkdir(parents=True, exist_ok=True)
    for field in MANIFEST_FIELDS:
        if field not in manifest:
            if field in {"output_files", "commands_executed", "errors"}:
                manifest[field] = []
            elif field == "subtitle_tracks":
                manifest[field] = {}
            elif field == "subtitle_clean_stats":
                manifest[field] = {}
            else:
                manifest[field] = ""
    path = directory / "download_manifest.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)
    return path


def _video_id_from_url(url: str) -> str:
    split = urlsplit(url)
    query_id = parse_qs(split.query).get("v", [""])[0]
    if query_id:
        return query_id
    if split.netloc.casefold().endswith("youtu.be"):
        return split.path.strip("/").split("/")[0]
    return ""


def _candidate_date(candidate_file: Path | str | None, metadata: dict[str, Any]) -> str:
    if candidate_file:
        match = re.search(r"(\d{4}-\d{2}-\d{2})", Path(candidate_file).name)
        if match:
            return match.group(1)
    upload = str(metadata.get("upload_date", ""))
    if re.fullmatch(r"\d{8}", upload):
        return f"{upload[:4]}-{upload[4:6]}-{upload[6:]}"
    return date.today().isoformat()


def _find_existing_task(parent: Path, video_id: str, rank: int | None = None) -> Path | None:
    if not parent.is_dir():
        return None
    rank_prefix = f"{int(rank):03d}_" if rank is not None else ""
    matches = [item for item in parent.iterdir() if item.is_dir() and video_id in item.name and (not rank_prefix or item.name.startswith(rank_prefix))]
    return sorted(matches)[0] if matches else None


def _task_directory(output_root: Path, source_mode: str, metadata: dict[str, Any], candidate_file: Path | str | None, rank: int | None) -> Path:
    video_id = str(metadata["id"])
    parent = output_root / _candidate_date(candidate_file, metadata)
    existing = _find_existing_task(parent, video_id, rank if source_mode == "candidate" else None)
    if existing:
        return existing
    safe_title = sanitize_windows_filename(metadata.get("title"), video_id, 90)
    prefix = f"{int(rank):03d}_" if source_mode == "candidate" and rank is not None else ""
    return parent / f"{prefix}{video_id}_{safe_title}"


def _manifest_is_complete(task_dir: Path, manifest: dict[str, Any], require_audio: bool, required_subtitle_labels: set[str] | None = None) -> bool:
    required_nonempty = [task_dir / "video" / "source.mp4", task_dir / "metadata" / "info.json"]
    if require_audio:
        required_nonempty.append(task_dir / "audio" / "source_audio.wav")
    description_exists = (task_dir / "metadata" / "description.txt").is_file()
    recorded_tracks = set(manifest.get("subtitle_tracks", {}))
    subtitle_tracking_complete = not required_subtitle_labels or required_subtitle_labels.issubset(recorded_tracks)
    cleaning_tracking_complete = manifest.get("subtitle_clean_status") in {"success", "missing"}
    return manifest.get("overall_status") == "success" and description_exists and subtitle_tracking_complete and cleaning_tracking_complete and all(path.is_file() and path.stat().st_size > 0 for path in required_nonempty)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _archive_contains(archive: Path, video_id: str) -> bool:
    if not archive.is_file():
        return False
    try:
        return any(line.strip().split()[-1:] == [video_id] for line in archive.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return False


def download_one_video(
    url: str,
    *,
    source_mode: str = "manual",
    candidate: dict[str, Any] | None = None,
    candidate_file: Path | str | None = None,
    candidate_rank: int | None = None,
    output_root: Path | str | None = None,
    config: dict[str, Any] | None = None,
    tools: dict[str, Path] | None = None,
    metadata_only: bool = False,
    subtitles_only: bool = False,
    no_audio_extract: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Download one video without raising for an individual media-step failure."""
    paths = get_project_paths(); config = config or load_download_config(); tools = tools or find_local_tools(paths)
    candidate = candidate or {}
    root = Path(output_root) if output_root else paths["candidate_downloads" if source_mode == "candidate" else "manual_downloads"]
    if not root.is_absolute(): root = paths["project_root"] / root
    video_id_hint = str(candidate.get("video_id") or _video_id_from_url(url))
    date_hint = _candidate_date(candidate_file, {})
    existing_parent = root / date_hint
    existing_task = _find_existing_task(existing_parent, video_id_hint, candidate_rank if source_mode == "candidate" else None) if video_id_hint else None
    local_metadata = _load_json(existing_task / "metadata" / "info.json") if existing_task else {}
    metadata_result: dict[str, Any] | None = None
    metadata = local_metadata if local_metadata and not force else {}
    commands: list[list[str]] = []
    errors: list[str] = []
    started_at = utc_now()
    if not metadata:
        metadata_result = fetch_video_metadata(url, tools, config, paths)
        commands.append(metadata_result["command_result"]["command"])
        if metadata_result.get("warning"):
            errors.append(str(metadata_result["warning"]))
        if not metadata_result["success"]:
            fallback_metadata = {"id": video_id_hint or "unknown", "title": candidate.get("title") or video_id_hint or "unknown"}
            task_dir = _task_directory(root, source_mode, fallback_metadata, candidate_file, candidate_rank)
            manifest = {
                "video_id": fallback_metadata["id"], "url": url, "source_mode": source_mode,
                "candidate_file": str(candidate_file or ""), "candidate_rank": candidate_rank or candidate.get("rank", ""),
                "rights_status": candidate.get("rights_status", ""), "selected": candidate.get("selected", ""),
                "title": fallback_metadata["title"], "channel": candidate.get("channel_title", ""),
                "started_at": started_at, "finished_at": utc_now(), "video_status": "not_started",
                "subtitle_status": "not_started", "subtitle_source": "", "vtt_status": "not_started", "srt_status": "not_started",
                "thumbnail_status": "not_started", "metadata_status": "failed", "audio_status": "not_started",
                "probe_status": "not_started", "overall_status": "failed", "output_files": [],
                "commands_executed": commands, "errors": errors + [metadata_result["error"]],
            }
            path = write_manifest(task_dir, manifest)
            return {"overall_status": "failed", "already_complete": False, "task_dir": task_dir, "manifest": manifest, "manifest_path": path}
        metadata = metadata_result["metadata"]
    task_dir = _task_directory(root, source_mode, metadata, candidate_file, candidate_rank)
    for child in ("video", "audio", "subtitles", "metadata"):
        (task_dir / child).mkdir(parents=True, exist_ok=True)
    old_manifest = _load_json(task_dir / "download_manifest.json")
    require_audio = bool(config.get("extract_audio", True) and not no_audio_extract)
    required_subtitle_labels = {"en"}
    if config.get("download_chinese_subtitles", True):
        required_subtitle_labels.add("zh")
    if old_manifest and not force and not metadata_only and not subtitles_only and _manifest_is_complete(task_dir, old_manifest, require_audio, required_subtitle_labels):
        return {"overall_status": "success", "already_complete": True, "task_dir": task_dir, "manifest": old_manifest, "manifest_path": task_dir / "download_manifest.json"}

    info_file = task_dir / "metadata" / "info.json"
    description_file = task_dir / "metadata" / "description.txt"
    candidate_copy = task_dir / "metadata" / "candidate.json"
    info_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    description_file.write_text(str(metadata.get("description") or ""), encoding="utf-8")
    if source_mode == "candidate":
        candidate_copy.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata_status = "success"
    if metadata_result:
        commands = [metadata_result["command_result"]["command"]]
        if metadata_result.get("warning"): errors.append(str(metadata_result["warning"]))

    manifest: dict[str, Any] = {
        "video_id": metadata.get("id", video_id_hint), "url": metadata.get("webpage_url") or url,
        "source_mode": source_mode, "candidate_file": str(candidate_file or ""),
        "candidate_rank": candidate_rank if candidate_rank is not None else candidate.get("rank", ""),
        "rights_status": candidate.get("rights_status", ""), "selected": candidate.get("selected", ""),
        "title": metadata.get("title", ""), "channel": metadata.get("channel") or metadata.get("uploader") or candidate.get("channel_title", ""),
        "started_at": started_at, "finished_at": "", "video_status": "not_requested" if metadata_only or subtitles_only else "pending",
        "subtitle_status": "not_requested" if metadata_only else "pending", "subtitle_source": "", "subtitle_tracks": {},
        "subtitle_clean_status": "not_requested" if metadata_only else "pending", "subtitle_clean_stats": {},
        "vtt_status": "not_requested" if metadata_only else "pending", "srt_status": "not_requested" if metadata_only else "pending",
        "thumbnail_status": "pending", "metadata_status": metadata_status,
        "audio_status": "not_requested" if metadata_only or subtitles_only or not require_audio else "pending",
        "probe_status": "not_requested", "overall_status": "failed", "output_files": [], "commands_executed": commands, "errors": errors,
    }
    write_manifest(task_dir, manifest)

    thumbnail_file = task_dir / "metadata" / "thumbnail.jpg"
    if not thumbnail_file.is_file() or force:
        thumb = download_thumbnail(url, task_dir, tools, config, paths)
        if thumb.get("command_result"): commands.append(thumb["command_result"]["command"])
        manifest["thumbnail_status"] = thumb["status"]
        if thumb.get("error"): errors.append(f"缩略图: {thumb['error']}")
    else:
        manifest["thumbnail_status"] = "success"

    if not metadata_only:
        subtitle = download_subtitles(url, task_dir, tools, config, paths)
        commands.extend(item["command"] for item in subtitle["command_results"])
        manifest.update(
            subtitle_status=subtitle["status"], subtitle_source=subtitle["source"],
            subtitle_tracks={
                label: {
                    "status": track["status"], "source": track["source"],
                    "vtt_status": track["vtt_status"], "srt_status": track["srt_status"],
                    "vtt_file": str(track["vtt_file"].relative_to(task_dir)) if track.get("vtt_file") else "",
                    "srt_file": str(track["srt_file"].relative_to(task_dir)) if track.get("srt_file") else "",
                }
                for label, track in subtitle.get("tracks", {}).items()
            },
            vtt_status=subtitle["vtt_status"], srt_status=subtitle["srt_status"],
        )
        if subtitle.get("warning"): errors.append(str(subtitle["warning"]))
        if subtitle.get("error") and subtitle["status"] != "missing": errors.append(f"字幕: {subtitle['error']}")
        english_track = subtitle.get("tracks", {}).get("en", {})
        if english_track.get("status") == "success":
            try:
                try:
                    from .clean_subtitles import clean_subtitle_directory
                except ImportError:
                    from clean_subtitles import clean_subtitle_directory
                cleaning = clean_subtitle_directory(task_dir / "subtitles")
                manifest["subtitle_clean_status"] = "success"
                manifest["subtitle_clean_stats"] = cleaning
            except (OSError, ValueError, RuntimeError) as exc:
                manifest["subtitle_clean_status"] = "failed"
                errors.append(f"字幕清洗: {exc}")
        else:
            manifest["subtitle_clean_status"] = "missing"

    video_file = task_dir / "video" / "source.mp4"
    audio_file = task_dir / "audio" / "source_audio.wav"
    video_ok = video_file.is_file() and video_file.stat().st_size > 0
    if not metadata_only and not subtitles_only:
        if not video_ok or force:
            use_archive = True
            if not video_ok and _archive_contains(paths["archive"], str(metadata.get("id", ""))):
                warning = "WARNING: 归档中已有该视频 ID，但本地视频缺失；本次修复暂不使用 download archive。"
                LOGGER.warning(warning); errors.append(warning); use_archive = False
            media = download_video_media(url, task_dir, tools, config, paths, paths["archive"] if source_mode == "candidate" else None, use_archive)
            command_results = media.get("command_results") or [media["command_result"]]
            commands.extend(result["command"] for result in command_results)
            video_ok = media["success"]
            manifest["video_status"] = media["status"]
            if media.get("warning"): errors.append(str(media["warning"]))
            if media.get("error"): errors.append(f"视频: {media['error']}")
        else:
            manifest["video_status"] = "success"
        if video_ok:
            video_probe = probe_media(video_file, tools["ffprobe"], expected="video")
            if video_probe.get("command_result"): commands.append(video_probe["command_result"]["command"])
            manifest["probe_status"] = video_probe["status"]
            if not video_probe["success"]: errors.append(f"视频校验: {video_probe['error']}")
        else:
            manifest["probe_status"] = "failed"

        if require_audio and video_ok:
            if not audio_file.is_file() or audio_file.stat().st_size <= 0 or force:
                audio = extract_audio(video_file, audio_file, tools, config, paths)
                commands.append(audio["command_result"]["command"])
                manifest["audio_status"] = audio["status"]
                if audio.get("error"): errors.append(f"音频: {audio['error']}")
            else:
                manifest["audio_status"] = "success"
            if manifest["audio_status"] == "success":
                audio_probe = probe_media(audio_file, tools["ffprobe"], expected="audio")
                if audio_probe.get("command_result"): commands.append(audio_probe["command_result"]["command"])
                if not audio_probe["success"]:
                    manifest["audio_status"] = "failed"; errors.append(f"音频校验: {audio_probe['error']}")
        elif require_audio:
            manifest["audio_status"] = "not_started"

    required_success = manifest["metadata_status"] == "success"
    if metadata_only:
        overall = "success" if required_success else "failed"
    elif subtitles_only:
        if not required_success:
            overall = "failed"
        elif manifest["subtitle_clean_status"] == "failed":
            overall = "partial_success"
        elif manifest["srt_status"] == "failed" or any(track.get("srt_status") == "failed" for track in manifest.get("subtitle_tracks", {}).values()):
            overall = "partial_success"
        else:
            overall = "success" if manifest["subtitle_status"] in {"success", "missing"} else "partial_success"
    else:
        if not required_success or manifest["video_status"] != "success":
            overall = "failed"
        elif manifest["probe_status"] != "success":
            overall = "partial_success"
        elif require_audio and manifest["audio_status"] != "success":
            overall = "partial_success"
        elif manifest["subtitle_clean_status"] == "failed":
            overall = "partial_success"
        elif manifest["srt_status"] == "failed" or any(track.get("srt_status") == "failed" for track in manifest.get("subtitle_tracks", {}).values()):
            overall = "partial_success"
        else:
            overall = "success"
    manifest["overall_status"] = overall
    manifest["finished_at"] = utc_now()
    manifest["commands_executed"] = commands
    manifest["errors"] = list(dict.fromkeys(str(error) for error in errors if error))
    manifest["output_files"] = sorted(str(path.relative_to(task_dir)) for path in task_dir.rglob("*") if path.is_file() and path.name != "download_manifest.json")
    manifest_path = write_manifest(task_dir, manifest)
    return {"overall_status": overall, "already_complete": False, "task_dir": task_dir, "manifest": manifest, "manifest_path": manifest_path}
