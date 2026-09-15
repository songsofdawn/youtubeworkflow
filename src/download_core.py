from __future__ import annotations

import json
import http.client
import logging
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit, urlunsplit


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
    "overall_status", "streams", "mux", "subtitle", "core_media_ready",
    "attempt_history", "warnings", "output_files", "commands_executed", "errors",
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
        with cookie_path.open(encoding="utf-8-sig", errors="replace") as cookie_file:
            first_line = cookie_file.readline().strip()
    except OSError:
        first_line = ""
    if first_line not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
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
    if config.get("abort_on_unavailable_fragments", False):
        options.append("--abort-on-unavailable-fragments")
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


def _is_po_token_download_error(result: dict[str, Any]) -> bool:
    text = f"{result.get('stderr', '')}\n{result.get('stdout', '')}".casefold()
    return "http error 403" in text or "403: forbidden" in text


def _download_network_hint(result: dict[str, Any]) -> str:
    if not _is_transient_download_error(result):
        return ""
    return (
        "下载链路无法与 YouTube 视频 CDN 建立稳定的 TLS 连接。"
        "若正在使用 Clash/VPN，请切换代理节点后在面板点击“重试”；"
        "已经成功的字幕、封面和元数据会被保留。"
    )


def _alternate_googlevideo_urls(url: str) -> list[str]:
    split = urlsplit(url)
    hostname = (split.hostname or "").casefold()
    if not hostname.endswith(".googlevideo.com"):
        return []
    machines = [
        value.strip()
        for value in parse_qs(split.query).get("mn", [""])[0].split(",")
        if value.strip()
    ]
    alternatives = [machine for machine in machines if machine not in hostname]
    if not alternatives:
        return []
    current_prefix = hostname.split("---", 1)[0] if "---" in hostname else "rr1"
    prefixes = list(dict.fromkeys((current_prefix, "rr1", "rr2", "r1")))
    urls: list[str] = []
    for machine in alternatives:
        for prefix in prefixes:
            netloc = f"{prefix}---{machine}.googlevideo.com"
            candidate = urlunsplit((split.scheme, netloc, split.path, split.query, split.fragment))
            if candidate not in urls:
                urls.append(candidate)
    return urls


@dataclass
class StreamSpec:
    role: str
    format_id: str
    ext: str
    vcodec: str
    acodec: str
    width: int | None
    height: int | None
    abr: float | None
    filesize: int | None
    filesize_approx: int | None
    url: str
    http_headers: dict[str, str]

    @property
    def expected_size(self) -> int:
        return int(self.filesize or self.filesize_approx or 0)

    def as_manifest_dict(self) -> dict[str, Any]:
        return {
            "format_id": self.format_id,
            "ext": self.ext,
            "vcodec": self.vcodec,
            "acodec": self.acodec,
            "width": self.width,
            "height": self.height,
            "abr": self.abr,
            "expected_size": self.expected_size,
        }


@dataclass
class MediaPlan:
    video: StreamSpec | None
    audio: StreamSpec | None
    source_format_selector: str

    def stream(self, role: str) -> StreamSpec | None:
        return self.video if role == "video" else self.audio


@dataclass
class StreamArtifact:
    role: str
    original_format_id: str
    final_format_id: str
    status: str
    validated: bool
    reusable: bool
    attempts: int
    fallback_used: bool
    backend: str
    path: Path | None
    expected_size: int
    actual_size: int
    attempt_history: list[dict[str, Any]] = field(default_factory=list)

    def as_manifest_dict(self) -> dict[str, Any]:
        relative_path = ""
        if self.path is not None:
            try:
                relative_path = str(self.path)
            except (OSError, ValueError):
                relative_path = str(self.path)
        return {
            "original_format_id": self.original_format_id,
            "final_format_id": self.final_format_id,
            "status": self.status,
            "validated": self.validated,
            "reusable": self.reusable,
            "attempts": self.attempts,
            "fallback_used": self.fallback_used,
            "backend": self.backend,
            "path": relative_path,
            "expected_size": self.expected_size,
            "actual_size": self.actual_size,
            "attempt_history": self.attempt_history,
        }


def _format_from_payload(payload: dict[str, Any], role: str) -> dict[str, Any] | None:
    selected = list(payload.get("requested_formats") or [])
    if not selected:
        selected = list(payload.get("requested_downloads") or [])
    for item in selected:
        if not isinstance(item, dict):
            continue
        if role == "video" and str(item.get("vcodec", "none")) != "none":
            return item
        if role == "audio" and str(item.get("acodec", "none")) != "none":
            return item
    return None


def _spec_from_format(item: dict[str, Any], role: str) -> StreamSpec:
    def to_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def to_float(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    headers = {
        str(key): str(value)
        for key, value in dict(item.get("http_headers") or {}).items()
        if str(key).casefold() not in {"host", "range", "accept-encoding"}
    }
    return StreamSpec(
        role=role,
        format_id=str(item.get("format_id") or ""),
        ext=re.sub(r"[^A-Za-z0-9]", "", str(item.get("ext") or "bin")) or "bin",
        vcodec=str(item.get("vcodec") or "none"),
        acodec=str(item.get("acodec") or "none"),
        width=to_int(item.get("width")),
        height=to_int(item.get("height")),
        abr=to_float(item.get("abr")),
        filesize=to_int(item.get("filesize")),
        filesize_approx=to_int(item.get("filesize_approx")),
        url=str(item.get("url") or ""),
        http_headers=headers,
    )


def _parse_content_range(value: str | None) -> tuple[int | None, int | None, int | None]:
    if not value:
        return None, None, None
    match = re.match(r"bytes\s+(\d*)-(\d*)/(\d*)", value.strip())
    if not match:
        return None, None, None

    def parse_number(text: str) -> int | None:
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    return parse_number(match.group(1)), parse_number(match.group(2)), parse_number(match.group(3))


def _response_content_length(response: Any) -> int | None:
    value = response.headers.get("Content-Length") if hasattr(response, "headers") else None
    if value:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _format_expected_size(format_info: dict[str, Any]) -> int:
    for key in ("filesize", "filesize_approx"):
        try:
            size = int(format_info.get(key) or 0)
        except (TypeError, ValueError):
            size = 0
        if size > 0:
            return size
    try:
        return max(0, int(parse_qs(urlsplit(str(format_info.get("url", ""))).query).get("clen", ["0"])[0]))
    except (TypeError, ValueError):
        return 0


def _stream_cdn_download(
    urls: list[str],
    destination: Path,
    format_info: dict[str, Any],
    label: str,
    config: dict[str, Any],
) -> tuple[bool, str]:
    if not urls:
        return False, "媒体地址中没有可用的备用 CDN"
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected_size = _format_expected_size(format_info)
    retries = max(1, int(config.get("cdn_fallback_retries", 5)))
    timeout = max(10, int(config.get("cdn_fallback_timeout_seconds", 60)))
    read_size = max(64 * 1024, int(config.get("cdn_read_chunk_bytes", 1024 * 1024)))
    source_headers = {
        str(key): str(value)
        for key, value in dict(format_info.get("http_headers") or {}).items()
        if str(key).casefold() not in {"host", "range", "accept-encoding"}
    }
    source_headers["Accept-Encoding"] = "identity"
    last_error = ""
    for attempt in range(retries):
        current_size = destination.stat().st_size if destination.is_file() else 0
        if expected_size and current_size > expected_size:
            destination.unlink(missing_ok=True)
            current_size = 0
        headers = dict(source_headers)
        if current_size:
            headers["Range"] = f"bytes={current_size}-"
        url = urls[attempt % len(urls)]
        request = urllib.request.Request(url, headers=headers)
        request_started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                ttfb = time.monotonic() - request_started
                content_range = str(response.headers.get("Content-Range") or "")
                range_start, range_end, _ = _parse_content_range(content_range)
                content_length = _response_content_length(response)
                if content_length is None and range_start is not None and range_end is not None:
                    content_length = range_end - range_start + 1
                if current_size and status == 416:
                    if not expected_size or current_size < expected_size:
                        raise OSError("服务器返回 416，但本地文件大小未知或不完整")
                    return True, ""
                mode = "wb"
                offset = 0
                if current_size and status == 206:
                    if range_start is None or range_start != current_size:
                        destination.unlink(missing_ok=True)
                        current_size = 0
                    else:
                        mode = "ab"
                        offset = current_size
                elif current_size:
                    destination.unlink(missing_ok=True)
                    current_size = 0
                response_remaining = content_length
                received = offset
                target_received = offset + response_remaining if response_remaining is not None else None
                with destination.open(mode) as handle:
                    while True:
                        if target_received is not None and received >= target_received:
                            break
                        want = read_size if target_received is None else min(read_size, target_received - received)
                        block = response.read(want)
                        if not block:
                            break
                        handle.write(block)
                        received += len(block)
                actual_size = destination.stat().st_size if destination.is_file() else 0
                elapsed = max(0.001, time.monotonic() - request_started)
                LOGGER.info(
                    "[备用 CDN][%s] format=%s host=%s range=%s status=%s content-range=%s ttfb=%.2fs received=%.1fMiB elapsed=%.1fs throughput=%.2fMiB/s",
                    label,
                    str(format_info.get("format_id") or format_info.get("format") or "?"),
                    urlsplit(url).netloc,
                    headers.get("Range", ""),
                    status,
                    content_range or "-",
                    ttfb,
                    actual_size / 1024 / 1024,
                    elapsed,
                    (actual_size / 1024 / 1024) / elapsed,
                )
                if target_received is not None and received < target_received:
                    raise http.client.IncompleteRead(b"", target_received - received)
                if expected_size and actual_size < expected_size:
                    raise http.client.IncompleteRead(b"", expected_size - actual_size)
                if actual_size <= 0:
                    raise OSError("下载结果为空")
                return True, ""
        except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException) as exc:
            last_error = str(exc)
            LOGGER.warning(
                "[备用 CDN][%s] %s 第 %d/%d 次失败，将从 %.1f MiB 处续传：%s",
                label,
                urlsplit(url).netloc,
                attempt + 1,
                retries,
                (destination.stat().st_size if destination.is_file() else 0) / 1024 / 1024,
                exc,
            )
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 8))
    return False, last_error or "备用 CDN 下载失败"


def _ytdlp_simulate_command(
    url: str,
    tools: dict[str, Path],
    paths: dict[str, Path],
    config: dict[str, Any],
    format_selector: str,
    *,
    no_cookies: bool = False,
    client: str | None = None,
) -> list[str | Path]:
    command: list[str | Path] = [
        tools["yt-dlp"],
        url,
        "--no-playlist",
        "--simulate",
        "--dump-single-json",
        "--no-warnings",
        "--format",
        format_selector,
        "--ffmpeg-location",
        paths["tools_bin"],
    ]
    if no_cookies:
        command.append("--no-cookies")
        if client:
            command.extend(["--extractor-args", f"youtube:player_client={client}"])
    else:
        cookies, _ = _cookie_argument(config, paths)
        command.extend(cookies)
    return command


def _resolve_media_plan(
    url: str,
    tools: dict[str, Path],
    paths: dict[str, Path],
    config: dict[str, Any],
    format_selector: str | None = None,
    *,
    no_cookies: bool = False,
    client: str | None = None,
) -> dict[str, Any]:
    selector = str(format_selector or config["format_selector"])
    command = _ytdlp_simulate_command(
        url, tools, paths, config, selector, no_cookies=no_cookies, client=client
    )
    result = run_command(command, paths["project_root"])
    if not result["success"]:
        return {"success": False, "error": _short_error(result), "command_result": result}
    try:
        payload = json.loads(result["stdout"])
    except (json.JSONDecodeError, TypeError) as exc:
        return {"success": False, "error": f"媒体计划元数据无法解析: {exc}", "command_result": result}
    video_format = _format_from_payload(payload, "video")
    audio_format = _format_from_payload(payload, "audio")
    if video_format is None:
        return {"success": False, "error": "没有解析出视频流", "command_result": result}
    video_spec = _spec_from_format(video_format, "video")
    audio_spec = None
    if (
        audio_format is not None
        and audio_format is not video_format
        and str(audio_format.get("format_id") or "") != str(video_format.get("format_id") or "")
    ):
        audio_spec = _spec_from_format(audio_format, "audio")
    return {
        "success": True,
        "plan": MediaPlan(video=video_spec, audio=audio_spec, source_format_selector=selector),
        "command_result": result,
    }


def _resolve_role(
    url: str,
    role: str,
    tools: dict[str, Path],
    paths: dict[str, Path],
    config: dict[str, Any],
    preferred_format_id: str | None = None,
) -> StreamSpec | None:
    selector = (
        preferred_format_id
        if preferred_format_id
        else ("bv*[height<=1080]/b" if role == "video" else "ba/b")
    )
    resolved = _resolve_media_plan(url, tools, paths, config, format_selector=selector)
    if not resolved["success"]:
        return None
    return resolved["plan"].stream(role)


def _stream_target_path(streams_dir: Path, spec: StreamSpec) -> Path:
    safe_format = re.sub(r"[^A-Za-z0-9_-]", "_", spec.format_id) or "unknown"
    return streams_dir / f"{spec.role}_{safe_format}.{spec.ext}"


def _probe_stream_file(path: Path, role: str, tools: dict[str, Path], paths: dict[str, Path]) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size <= 0:
        return {"success": False, "status": "failed", "error": "文件不存在或为空", "data": {}}
    result = run_command(
        [tools["ffprobe"], "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
        paths["project_root"],
    )
    if not result["success"]:
        return {"success": False, "status": "failed", "error": _short_error(result), "data": {}, "command_result": result}
    try:
        data = json.loads(result["stdout"])
    except (json.JSONDecodeError, TypeError) as exc:
        return {"success": False, "status": "failed", "error": f"ffprobe JSON 无法解析: {exc}", "data": {}, "command_result": result}
    streams = data.get("streams", [])
    matching = [stream for stream in streams if stream.get("codec_type") == role]
    durations = [data.get("format", {}).get("duration")] + [stream.get("duration") for stream in streams]
    duration = 0.0
    for value in durations:
        try:
            duration = max(duration, float(value or 0))
        except (TypeError, ValueError):
            pass
    errors: list[str] = []
    if not matching:
        errors.append(f"缺少{role}流")
    if duration <= 0:
        errors.append("时长无效")
    return {
        "success": not errors,
        "status": "success" if not errors else "failed",
        "error": "; ".join(errors),
        "data": data,
        "command_result": result,
    }


def _ytdlp_stream_command(
    url: str,
    spec: StreamSpec,
    target: Path,
    tools: dict[str, Path],
    paths: dict[str, Path],
    config: dict[str, Any],
    format_selector: str,
    *,
    no_cookies: bool = False,
    client: str | None = None,
    archive_path: Path | str | None = None,
    use_archive: bool = True,
) -> list[str | Path]:
    command: list[str | Path] = [
        tools["yt-dlp"],
        url,
        "--no-playlist",
        "--continue",
        "--retries",
        str(config["retries"]),
        "--fragment-retries",
        str(config["fragment_retries"]),
        "--retry-sleep",
        str(config["retry_sleep_seconds"]),
        "--ffmpeg-location",
        paths["tools_bin"],
        "--format",
        format_selector,
        "--output",
        str(target),
        "--no-write-playlist-metafiles",
        *_download_network_options(config),
    ]
    if no_cookies:
        command.extend(["--no-cookies"])
        if client:
            command.extend(["--extractor-args", f"youtube:player_client={client}"])
    else:
        cookies, _ = _cookie_argument(config, paths)
        command.extend(cookies)
    if archive_path and use_archive:
        archive = Path(archive_path)
        archive.parent.mkdir(parents=True, exist_ok=True)
        command.extend(["--download-archive", archive])
    deno = paths["tools_bin"] / "deno.exe"
    if deno.is_file():
        command.extend(["--js-runtimes", f"deno:{deno}"])
    return command


def _download_stream_ytdlp(
    url: str,
    spec: StreamSpec,
    target: Path,
    tools: dict[str, Path],
    paths: dict[str, Path],
    config: dict[str, Any],
    format_selector: str,
    *,
    no_cookies: bool = False,
    client: str | None = None,
    archive_path: Path | str | None = None,
    use_archive: bool = True,
) -> dict[str, Any]:
    command = _ytdlp_stream_command(
        url, spec, target, tools, paths, config, format_selector,
        no_cookies=no_cookies, client=client, archive_path=archive_path, use_archive=use_archive,
    )
    result = run_command(command, paths["project_root"], stream_output=True)
    return result


def _fallback_http_stream(
    spec: StreamSpec,
    target: Path,
    tools: dict[str, Path],
    paths: dict[str, Path],
    config: dict[str, Any],
) -> tuple[bool, str]:
    if not spec.url:
        return False, "没有可用的签名 URL"
    part = target.with_name(target.name + ".part")
    format_info = {
        "format_id": spec.format_id,
        "url": spec.url,
        "http_headers": spec.http_headers,
        "filesize": spec.filesize,
        "filesize_approx": spec.filesize_approx,
    }
    alternatives = _alternate_googlevideo_urls(spec.url) or [spec.url]
    ok, error = _stream_cdn_download(alternatives, part, format_info, spec.role, config)
    if not ok:
        return False, error
    probe = _probe_stream_file(part, spec.role, tools, paths)
    if not probe["success"]:
        part.unlink(missing_ok=True)
        return False, probe["error"]
    target.unlink(missing_ok=True)
    part.replace(target)
    return True, ""


def _make_artifact(
    role: str,
    original_format_id: str,
    final_format_id: str,
    status: str,
    validated: bool,
    fallback_used: bool,
    backend: str,
    path: Path | None,
    expected_size: int,
    attempt_history: list[dict[str, Any]],
) -> StreamArtifact:
    actual_size = path.stat().st_size if path and path.is_file() else 0
    return StreamArtifact(
        role=role,
        original_format_id=original_format_id,
        final_format_id=final_format_id,
        status=status,
        validated=validated,
        reusable=validated,
        attempts=len(attempt_history),
        fallback_used=fallback_used,
        backend=backend,
        path=path,
        expected_size=expected_size,
        actual_size=actual_size,
        attempt_history=attempt_history,
    )


def _ensure_stream_artifact(
    url: str,
    spec: StreamSpec,
    streams_dir: Path,
    tools: dict[str, Path],
    paths: dict[str, Path],
    config: dict[str, Any],
    *,
    force: bool = False,
    archive_path: Path | str | None = None,
    use_archive: bool = True,
) -> tuple[StreamArtifact, list[dict[str, Any]], list[str], list[str], StreamSpec]:
    target = _stream_target_path(streams_dir, spec)
    target.parent.mkdir(parents=True, exist_ok=True)
    original_format_id = spec.format_id
    current_spec = spec
    command_results: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    attempts: list[dict[str, Any]] = []

    existing_probe = _probe_stream_file(target, spec.role, tools, paths)
    if existing_probe["success"] and not force:
        if existing_probe.get("command_result"):
            command_results.append(existing_probe["command_result"])
        artifact = _make_artifact(
            spec.role, original_format_id, current_spec.format_id, "success", True, False,
            "artifact_reuse", target, current_spec.expected_size, [],
        )
        return artifact, command_results, warnings, errors, current_spec
    if force and target.exists():
        target.unlink(missing_ok=True)
        target.with_name(target.name + ".part").unlink(missing_ok=True)

    def record(backend: str, result: str, detail: str) -> None:
        attempts.append({
            "stream": spec.role,
            "attempt": len(attempts) + 1,
            "backend": backend,
            "result": result,
            "detail": detail[:500],
        })

    def try_ytdlp(selector: str, backend: str, *, no_cookies: bool = False, client: str | None = None) -> dict[str, Any]:
        result = _download_stream_ytdlp(
            url, current_spec, target, tools, paths, config, selector,
            no_cookies=no_cookies, client=client, archive_path=archive_path, use_archive=use_archive,
        )
        command_results.append(result)
        probe = _probe_stream_file(target, spec.role, tools, paths)
        if probe.get("command_result"):
            command_results.append(probe["command_result"])
        if probe["success"]:
            return {"success": True, "result": result, "probe": probe}
        error_kind = "ssl_eof" if _is_transient_download_error(result) else (
            "http_403" if _is_po_token_download_error(result) else "failed"
        )
        record(backend, error_kind, _short_error(result) or probe["error"])
        return {"success": False, "result": result, "probe": probe, "error_kind": error_kind}

    outcome = try_ytdlp(current_spec.format_id, "yt-dlp")
    if outcome["success"]:
        return _make_artifact(spec.role, original_format_id, current_spec.format_id, "success", True, False, "yt-dlp", target, current_spec.expected_size, attempts), command_results, warnings, errors, current_spec

    if outcome.get("error_kind") == "http_403" and config.get("po_token_fallback_enabled", True):
        client = str(config.get("po_token_fallback_client", "web_embedded")).strip() or "web_embedded"
        fallback = try_ytdlp(current_spec.format_id, "yt-dlp-web_embedded", no_cookies=True, client=client)
        if fallback["success"]:
            warnings.append(f"{spec.role} 主客户端 403，已通过 web_embedded 客户端恢复")
            return _make_artifact(spec.role, original_format_id, current_spec.format_id, "success", True, False, "yt-dlp-web_embedded", target, current_spec.expected_size, attempts), command_results, warnings, errors, current_spec

    refreshed = _resolve_role(url, spec.role, tools, paths, config, preferred_format_id=original_format_id)
    if refreshed is not None:
        current_spec = refreshed
        retry = try_ytdlp(current_spec.format_id, "yt-dlp-re_resolved")
        if retry["success"]:
            return _make_artifact(spec.role, original_format_id, current_spec.format_id, "success", True, False, "yt-dlp-re_resolved", target, current_spec.expected_size, attempts), command_results, warnings, errors, current_spec

    fallback_spec = _resolve_role(url, spec.role, tools, paths, config)
    if fallback_spec is not None and fallback_spec.format_id != current_spec.format_id:
        current_spec = fallback_spec
        fallback = try_ytdlp(current_spec.format_id, "yt-dlp-format-fallback")
        if fallback["success"]:
            warnings.append(f"{spec.role} 原 format {original_format_id} 不可用，已回退到 {current_spec.format_id}")
            return _make_artifact(spec.role, original_format_id, current_spec.format_id, "success", True, True, "yt-dlp-format-fallback", target, current_spec.expected_size, attempts), command_results, warnings, errors, current_spec

    if config.get("cdn_fallback_enabled", True):
        ok, error = _fallback_http_stream(current_spec, target, tools, paths, config)
        if ok:
            record("backup_cdn", "success", "")
            warnings.append(f"{spec.role} 主下载失败，已通过备用 CDN 恢复")
            return _make_artifact(spec.role, original_format_id, current_spec.format_id, "success", True, True, "backup_cdn", target, current_spec.expected_size, attempts), command_results, warnings, errors, current_spec
        record("backup_cdn", "failed", error)
        errors.append(f"{spec.role} 备用 CDN 下载失败：{error}")

    return _make_artifact(spec.role, original_format_id, current_spec.format_id, "failed", False, True, current_spec.url and "backup_cdn" or "yt-dlp", None, current_spec.expected_size, attempts), command_results, warnings, errors, current_spec


def _mux_streams(
    video_path: Path,
    audio_path: Path | None,
    output_path: Path,
    tools: dict[str, Path],
    paths: dict[str, Path],
) -> dict[str, Any]:
    temporary = output_path.with_name(f".{output_path.name}.mux.mp4")
    temporary.unlink(missing_ok=True)
    command: list[str | Path] = [tools["ffmpeg"], "-hide_banner", "-y", "-i", video_path]
    if audio_path is not None:
        command.extend(["-i", audio_path, "-map", "0:v:0", "-map", "1:a:0"])
    else:
        command.extend(["-map", "0:v:0", "-map", "0:a?"])
    command.extend(["-c", "copy", "-movflags", "+faststart", temporary])
    result = run_command(command, paths["project_root"], stream_output=True)
    if not result["success"] or not temporary.is_file() or temporary.stat().st_size <= 0:
        temporary.unlink(missing_ok=True)
        return {"success": False, "error": _short_error(result), "command_result": result}
    output_path.unlink(missing_ok=True)
    temporary.replace(output_path)
    return {"success": True, "error": "", "command_result": result}


def download_video_media(url: str, task_dir: Path | str, tools: dict[str, Path] | None = None, config: dict[str, Any] | None = None, paths: dict[str, Path] | None = None, archive_path: Path | str | None = None, use_archive: bool = True, *, force: bool = False) -> dict[str, Any]:
    paths = paths or get_project_paths(); tools = tools or find_local_tools(paths); config = config or load_download_config()
    task_dir = Path(task_dir); video_dir = task_dir / "video"; streams_dir = task_dir / "streams"
    video_dir.mkdir(parents=True, exist_ok=True); streams_dir.mkdir(parents=True, exist_ok=True)
    final = video_dir / "source.mp4"
    _, cookie_warning = _cookie_argument(config, paths)
    command_results: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    attempt_history: list[dict[str, Any]] = []
    streams_manifest: dict[str, Any] = {}
    mux_manifest: dict[str, Any] = {"status": "not_started", "validated": False}

    existing_probe = probe_media(final, tools["ffprobe"], expected="video")
    if existing_probe.get("command_result"):
        command_results.append(existing_probe["command_result"])
    if not force and final.is_file() and final.stat().st_size > 0 and existing_probe["success"]:
        return {
            "success": True,
            "status": "success",
            "file": final,
            "command_result": existing_probe.get("command_result") or {"success": True, "returncode": 0, "stdout": "已复用 source.mp4", "stderr": "", "command": ["internal:artifact_reuse"]},
            "command_results": command_results,
            "warning": cookie_warning,
            "error": "",
            "streams": streams_manifest,
            "mux": {"status": "success", "validated": True},
            "video_status": "success",
            "audio_status": "success",
            "probe_status": "success",
            "core_media_ready": True,
            "attempt_history": attempt_history,
            "warnings": warnings,
            "errors": errors,
        }

    resolved = _resolve_media_plan(url, tools, paths, config)
    command_results.append(resolved.get("command_result"))
    if not resolved["success"]:
        errors.append(resolved["error"])
        return {
            "success": False,
            "status": "failed",
            "file": None,
            "command_result": resolved.get("command_result"),
            "command_results": command_results,
            "warning": cookie_warning,
            "error": resolved["error"],
            "streams": streams_manifest,
            "mux": mux_manifest,
            "video_status": "failed",
            "audio_status": "not_started",
            "probe_status": "failed",
            "core_media_ready": False,
            "attempt_history": attempt_history,
            "warnings": warnings,
            "errors": errors,
        }
    plan: MediaPlan = resolved["plan"]
    if plan.video is None:
        errors.append("没有解析出视频流")
        return {
            "success": False,
            "status": "failed",
            "file": None,
            "command_result": resolved.get("command_result"),
            "command_results": command_results,
            "warning": cookie_warning,
            "error": "没有解析出视频流",
            "streams": streams_manifest,
            "mux": mux_manifest,
            "video_status": "failed",
            "audio_status": "not_started",
            "probe_status": "failed",
            "core_media_ready": False,
            "attempt_history": attempt_history,
            "warnings": warnings,
            "errors": errors,
        }

    video_artifact, v_cmds, v_warnings, v_errors, video_spec = _ensure_stream_artifact(
        url, plan.video, streams_dir, tools, paths, config, force=force,
        archive_path=archive_path, use_archive=use_archive,
    )
    command_results.extend(v_cmds); warnings.extend(v_warnings); errors.extend(v_errors)
    attempt_history.extend(video_artifact.attempt_history)
    streams_manifest["video"] = video_artifact.as_manifest_dict()

    audio_artifact: StreamArtifact | None = None
    if plan.audio is not None:
        audio_artifact, a_cmds, a_warnings, a_errors, _ = _ensure_stream_artifact(
            url, plan.audio, streams_dir, tools, paths, config, force=force,
            archive_path=archive_path, use_archive=use_archive,
        )
        command_results.extend(a_cmds); warnings.extend(a_warnings); errors.extend(a_errors)
        attempt_history.extend(audio_artifact.attempt_history)
        streams_manifest["audio"] = audio_artifact.as_manifest_dict()

    video_path = video_artifact.path if video_artifact.validated else None
    audio_path = audio_artifact.path if audio_artifact and audio_artifact.validated else None
    if video_path is not None and (audio_path is not None or plan.audio is None):
        mux = _mux_streams(video_path, audio_path, final, tools, paths)
        command_results.append(mux["command_result"])
        if not mux["success"]:
            errors.append(f"mux 失败：{mux['error']}")
            mux_manifest = {"status": "failed", "validated": False, "error": mux["error"]}
        else:
            mux_manifest = {"status": "success", "validated": True}
    else:
        mux_manifest = {"status": "skipped", "validated": False}

    final_probe = probe_media(final, tools["ffprobe"], expected="video")
    if final_probe.get("command_result"):
        command_results.append(final_probe["command_result"])
    success = final.is_file() and final.stat().st_size > 0 and final_probe["success"]
    if not success and final_probe["error"]:
        errors.append(f"最终媒体校验失败：{final_probe['error']}")
    video_status = "success" if video_artifact.validated else "failed"
    audio_status = "success" if (audio_artifact and audio_artifact.validated) else ("failed" if plan.audio is not None else "not_requested")
    error = "" if success else _short_error(final_probe.get("command_result") or resolved.get("command_result") or {})
    if errors:
        error = (error + "\n" if error else "") + "\n".join(dict.fromkeys(errors))
    return {
        "success": success,
        "status": "success" if success else "failed",
        "file": final if success else None,
        "command_result": final_probe.get("command_result") or resolved.get("command_result") or {"success": success, "returncode": 0 if success else 1, "stdout": "", "stderr": "", "command": ["internal:stream-engine"]},
        "command_results": command_results,
        "warning": cookie_warning,
        "error": error,
        "streams": streams_manifest,
        "mux": mux_manifest,
        "video_status": video_status,
        "audio_status": audio_status,
        "probe_status": final_probe["status"],
        "core_media_ready": success,
        "attempt_history": attempt_history,
        "warnings": warnings,
        "errors": errors,
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
        failed = [result for result in commands if not result["success"]]
        status = "failed" if failed else "missing"
        return {
            "language": label, "status": status, "source": "", "vtt_status": status, "srt_status": status,
            "vtt_file": None, "srt_file": None, "command_results": commands,
            "error": _short_error(failed[-1]) if failed else "未找到字幕",
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
    conversion_errors = [f"{label}: {track['error']}" for label, track in tracks.items() if track.get("error") and track["status"] != "missing"]
    failed = any(track["srt_status"] == "failed" for track in tracks.values())
    return {
        "success": not failed,
        "status": "failed" if failed else "success" if any(track["status"] == "success" for track in tracks.values()) else "missing",
        "source": preferred["source"], "vtt_status": preferred["vtt_status"], "srt_status": preferred["srt_status"],
        "vtt_file": preferred["vtt_file"], "srt_file": preferred["srt_file"], "tracks": tracks,
        "command_results": commands, "warning": warning, "error": "; ".join(conversion_errors),
    }


def _metadata_thumbnail_urls(metadata: dict[str, Any] | None) -> list[str]:
    if not isinstance(metadata, dict):
        return []
    ranked: list[tuple[int, int, str]] = []
    thumbnails = metadata.get("thumbnails")
    if isinstance(thumbnails, list):
        for index, item in enumerate(thumbnails):
            if not isinstance(item, dict):
                continue
            value = str(item.get("url") or "").strip()
            if not value.startswith("https://"):
                continue
            try:
                area = int(item.get("width") or 0) * int(item.get("height") or 0)
                preference = int(item.get("preference") or 0)
            except (TypeError, ValueError):
                area = preference = 0
            ranked.append((area, preference * 1000 + index, value))
    direct = str(metadata.get("thumbnail") or "").strip()
    if direct.startswith("https://"):
        ranked.append((0, -len(ranked), direct))
    ranked.sort(reverse=True)
    return list(dict.fromkeys(item[2] for item in ranked))


def _trusted_metadata_thumbnail(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    return parsed.scheme == "https" and not parsed.username and (
        host in {"i.ytimg.com", "img.youtube.com"}
        or host.endswith((".ytimg.com", ".googleusercontent.com"))
    )


class _ThumbnailRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _trusted_metadata_thumbnail(newurl):
            raise ValueError("缩略图重定向地址不可信")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download_thumbnail_from_metadata(
    metadata: dict[str, Any] | None,
    final: Path,
    tools: dict[str, Path],
    paths: dict[str, Path],
) -> dict[str, Any] | None:
    """Try the highest-resolution URL yt-dlp already returned before a second yt-dlp call."""
    for url in _metadata_thumbnail_urls(metadata):
        if not _trusted_metadata_thumbnail(url):
            continue
        temporary = final.with_name(f".{final.name}.metadata.tmp")
        converted = final.with_name(f".{final.stem}.converted.jpg")
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "YouTubeWorkflow/thumbnail"},
            )
            opener = urllib.request.build_opener(_ThumbnailRedirectHandler())
            with opener.open(request, timeout=20) as response:
                content = response.read(12 * 1024 * 1024 + 1)
            if not content or len(content) > 12 * 1024 * 1024:
                continue
            temporary.write_bytes(content)
            result = run_command(
                [
                    tools["ffmpeg"],
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    temporary,
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    converted,
                ],
                paths["project_root"],
            )
            if result["success"] and converted.is_file() and converted.stat().st_size > 0:
                converted.replace(final)
                return {
                    "success": True,
                    "status": "success",
                    "file": final,
                    "command_result": result,
                    "source_url": url,
                    "error": "",
                }
        except (OSError, urllib.error.URLError, ValueError):
            continue
        finally:
            temporary.unlink(missing_ok=True)
            converted.unlink(missing_ok=True)
    return None


def download_thumbnail(
    url: str,
    task_dir: Path | str,
    tools: dict[str, Path] | None = None,
    config: dict[str, Any] | None = None,
    paths: dict[str, Path] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    paths = paths or get_project_paths(); tools = tools or find_local_tools(paths); config = config or load_download_config()
    directory = Path(task_dir) / "metadata"; directory.mkdir(parents=True, exist_ok=True)
    final = directory / "thumbnail.jpg"
    if final.is_file() and final.stat().st_size > 0:
        return {"success": True, "status": "success", "file": final, "command_result": None, "error": ""}
    metadata_result = _download_thumbnail_from_metadata(metadata, final, tools, paths)
    if metadata_result is not None:
        return metadata_result
    cookies, warning = _cookie_argument(config, paths)
    command = [tools["yt-dlp"], url, "--no-playlist", "--skip-download", "--write-thumbnail", "--convert-thumbnails", "jpg", "--ffmpeg-location", paths["tools_bin"], "--output", directory / ".thumbnail-source.%(ext)s", *cookies]
    result = run_command(command, paths["project_root"])
    jpgs = sorted(path for path in directory.glob(".thumbnail-source*.jpg")
                  if path.stat().st_size > 0)
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
            if field in {"output_files", "commands_executed", "errors", "attempt_history", "warnings"}:
                manifest[field] = []
            elif field in {"subtitle_tracks", "subtitle_clean_stats", "streams", "mux", "subtitle"}:
                manifest[field] = {}
            elif field == "core_media_ready":
                manifest[field] = False
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
    warnings: list[str] = []
    started_at = utc_now()
    if not metadata:
        metadata_result = fetch_video_metadata(url, tools, config, paths)
        commands.append(metadata_result["command_result"]["command"])
        if metadata_result.get("warning"):
            warnings.append(str(metadata_result["warning"]))
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
                "warnings": warnings,
            }
            path = write_manifest(task_dir, manifest)
            return {"overall_status": "failed", "already_complete": False, "task_dir": task_dir, "manifest": manifest, "manifest_path": path}
        metadata = metadata_result["metadata"]
    # A metadata lookup can fail before we know the upload date/title, leaving a
    # resumable ``<today>/<id>_<id>`` task.  Prefer that already-discovered task
    # after metadata recovers; otherwise the upload date would redirect the
    # retry into a second directory and leave the failed dashboard card behind.
    task_dir = existing_task or _task_directory(
        root, source_mode, metadata, candidate_file, candidate_rank
    )
    for child in ("video", "audio", "subtitles", "metadata"):
        (task_dir / child).mkdir(parents=True, exist_ok=True)
    old_manifest = _load_json(task_dir / "download_manifest.json")
    require_audio = bool(config.get("extract_audio", True) and not no_audio_extract)
    required_subtitle_labels = {"en"}
    if config.get("download_chinese_subtitles", True):
        required_subtitle_labels.add("zh")
    if old_manifest and not force and not metadata_only and not subtitles_only and _manifest_is_complete(task_dir, old_manifest, require_audio, required_subtitle_labels):
        from src.learning.learning_service import on_download_success
        on_download_success(paths["project_root"], metadata, source_mode)
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
        if metadata_result.get("warning"): warnings.append(str(metadata_result["warning"]))

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
        "streams": {}, "mux": {}, "subtitle": {}, "core_media_ready": False, "attempt_history": [], "warnings": warnings,
    }
    write_manifest(task_dir, manifest)

    thumbnail_file = task_dir / "metadata" / "thumbnail.jpg"
    if not thumbnail_file.is_file() or force:
        thumb = download_thumbnail(
            url,
            task_dir,
            tools,
            config,
            paths,
            metadata=metadata,
        )
        if thumb.get("command_result"): commands.append(thumb["command_result"]["command"])
        manifest["thumbnail_status"] = thumb["status"]
        if thumb.get("error"): warnings.append(f"缩略图: {thumb['error']}")
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
        manifest["subtitle"] = {
            "status": subtitle["status"],
            "tracks": {
                label: track["status"]
                for label, track in subtitle.get("tracks", {}).items()
            },
        }
        if subtitle.get("warning"): warnings.append(str(subtitle["warning"]))
        if subtitle.get("error") and subtitle["status"] != "missing": warnings.append(f"字幕: {subtitle['error']}")
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
                warnings.append(f"字幕清洗: {exc}")
        else:
            manifest["subtitle_clean_status"] = "missing"

    video_file = task_dir / "video" / "source.mp4"
    audio_file = task_dir / "audio" / "source_audio.wav"
    video_ok = video_file.is_file() and video_file.stat().st_size > 0
    core_media_ready = False
    if not metadata_only and not subtitles_only:
        if not video_ok or force:
            use_archive = True
            if not video_ok and _archive_contains(paths["archive"], str(metadata.get("id", ""))):
                warning = "WARNING: 归档中已有该视频 ID，但本地视频缺失；本次修复暂不使用 download archive。"
                LOGGER.warning(warning); errors.append(warning); use_archive = False
            media = download_video_media(url, task_dir, tools, config, paths, paths["archive"] if source_mode == "candidate" else None, use_archive, force=force)
            command_results = media.get("command_results") or [media["command_result"]]
            commands.extend(result["command"] for result in command_results)
            video_ok = media["success"]
            manifest["video_status"] = media.get("video_status", media["status"])
            manifest["probe_status"] = media.get("probe_status", "pending")
            manifest["streams"] = media.get("streams", {})
            manifest["mux"] = media.get("mux", {})
            manifest["attempt_history"] = media.get("attempt_history", [])
            warnings.extend(media.get("warnings", []))
            if media.get("warning"): warnings.append(str(media["warning"]))
            if media.get("error") and not media["success"]: errors.append(f"视频: {media['error']}")
        else:
            manifest["video_status"] = "success"
            manifest["probe_status"] = "success"
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
        core_media_ready = (
            manifest.get("probe_status") == "success"
            and (not require_audio or manifest.get("audio_status") == "success")
        )

    manifest["core_media_ready"] = core_media_ready
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
        overall = "success" if (required_success and core_media_ready) else "failed"
    manifest["overall_status"] = overall
    manifest["finished_at"] = utc_now()
    manifest["commands_executed"] = commands
    manifest["warnings"] = list(dict.fromkeys(str(warning) for warning in warnings if warning))
    if overall == "success":
        manifest["errors"] = []
    else:
        manifest["errors"] = list(dict.fromkeys(str(error) for error in errors if error))
    manifest["output_files"] = sorted(str(path.relative_to(task_dir)) for path in task_dir.rglob("*") if path.is_file() and path.name != "download_manifest.json")
    manifest_path = write_manifest(task_dir, manifest)
    if overall == "success" and not metadata_only and not subtitles_only:
        from src.learning.learning_service import on_download_success
        on_download_success(paths["project_root"], metadata, source_mode)
    return {"overall_status": overall, "already_complete": False, "task_dir": task_dir, "manifest": manifest, "manifest_path": manifest_path}
