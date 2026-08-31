from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .app import ControlPanelApp
from .youtube import YouTubeAPIError


MAX_REQUEST_BYTES = 6 * 1024 * 1024


def _optional_float(value: Any, label: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是数字") from exc


def make_handler(
    app: ControlPanelApp,
    static_root: Path,
    runtime_info: dict[str, Any] | None = None,
) -> type[BaseHTTPRequestHandler]:
    static_root = static_root.resolve()
    runtime = dict(runtime_info or {})

    class PanelRequestHandler(BaseHTTPRequestHandler):
        server_version = "YouTubeWorkflowPanel/2.0"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/dashboard":
                    self._json(HTTPStatus.OK, app.dashboard())
                    return
                if parsed.path == "/api/health":
                    self._json(HTTPStatus.OK, app.health())
                    return
                if parsed.path == "/api/runtime":
                    self._json(HTTPStatus.OK, runtime)
                    return
                if parsed.path == "/api/tasks":
                    self._json(HTTPStatus.OK, {"tasks": app.scanner.scan()})
                    return
                if parsed.path == "/api/discovery/packs":
                    self._json(HTTPStatus.OK, {"packs": app.discovery_catalog()})
                    return
                if parsed.path == "/api/discovery/result":
                    query = parse_qs(parsed.query)
                    job_id = str((query.get("job_id") or [""])[0])
                    if not job_id:
                        raise ValueError("缺少智能发现任务 ID")
                    self._json(HTTPStatus.OK, app.discovery_job_result(job_id))
                    return
                if parsed.path == "/api/publish/defaults":
                    query = parse_qs(parsed.query)
                    task = str((query.get("task") or [""])[0])
                    self._json(HTTPStatus.OK, app.publish_defaults(task))
                    return
                if parsed.path == "/api/render-review":
                    query = parse_qs(parsed.query)
                    task = str((query.get("task") or [""])[0])
                    self._json(HTTPStatus.OK, app.render_review(task))
                    return
                if parsed.path == "/api/jobs":
                    query = parse_qs(parsed.query)
                    limit = int((query.get("limit") or ["80"])[0])
                    self._json(HTTPStatus.OK, {"jobs": app.store.list(limit)})
                    return
                if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/log"):
                    job_id = parsed.path.split("/")[3]
                    self._json(
                        HTTPStatus.OK,
                        {"job_id": job_id, "log": app.store.log_tail(job_id)},
                    )
                    return
                self._static(parsed.path)
            except KeyError:
                self._error(HTTPStatus.NOT_FOUND, "任务不存在")
            except (ValueError, OSError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                self._validate_local_json_request()
                body = self._read_json()
                if parsed.path == "/api/search":
                    results = app.search(
                        str(body.get("query") or ""),
                        int(body.get("limit") or 10),
                        str(body.get("order") or "relevance"),
                    )
                    self._json(HTTPStatus.OK, {"results": results})
                    return
                if parsed.path == "/api/discover":
                    raw_packs = body.get("packs")
                    job = app.queue_discovery(
                        [str(item) for item in raw_packs]
                        if isinstance(raw_packs, list)
                        else [],
                        int(body.get("hours") or 72),
                        int(body.get("per_pack") or 20),
                        int(body.get("minimum_duration_minutes") or 5),
                        (
                            int(body["maximum_duration_minutes"])
                            if body.get("maximum_duration_minutes") is not None
                            else None
                        ),
                    )
                    self._json(HTTPStatus.ACCEPTED, {"job": job})
                    return
                if parsed.path == "/api/discovery/feedback":
                    item = body.get("item")
                    if not isinstance(item, dict):
                        raise ValueError("智能发现反馈缺少视频信息")
                    result = app.record_discovery_feedback(
                        item,
                        str(body.get("feedback") or ""),
                    )
                    self._json(HTTPStatus.OK, {"feedback": result})
                    return
                if parsed.path == "/api/settings":
                    self._json(HTTPStatus.OK, app.save_settings(body))
                    return
                if parsed.path == "/api/youtube/cookies":
                    self._json(HTTPStatus.OK, app.update_youtube_cookies(body))
                    return
                if parsed.path == "/api/biliup/login":
                    self._json(HTTPStatus.OK, app.open_biliup_login())
                    return
                if parsed.path == "/api/downloads":
                    jobs = app.queue_downloads(
                        raw_input=str(body.get("input") or ""),
                        items=body.get("items") if isinstance(body.get("items"), list) else None,
                        confirm_rights=body.get("confirm_rights") is True,
                        auto_publish=body.get("auto_publish") is True,
                        whisper_for_auto_subtitles=(
                            body.get("whisper_for_auto_subtitles") is not False
                        ),
                        auto_translate_missing=(
                            body.get("auto_translate_missing") is not False
                        ),
                        publish_metadata_provider=str(
                            body.get("publish_metadata_provider") or "auto"
                        ),
                        account_id=str(body.get("account_id") or ""),
                        publish_only_self=body.get("publish_only_self") is True,
                        automation_render_mode=str(
                            body.get("automation_render_mode") or "hardsub"
                        ),
                        automation_failure_policy=str(
                            body.get("automation_failure_policy") or "skip"
                        ),
                        automation_target=str(
                            body.get("automation_target") or "publish"
                        ),
                        english_subtitle_policy=str(
                            body.get("english_subtitle_policy") or ""
                        ),
                        automation_chinese_policy=str(
                            body.get("automation_chinese_policy") or ""
                        ),
                        automation_silent_video_policy=str(
                            body.get("automation_silent_video_policy")
                            or "publish_original"
                        ),
                        automation_dubbing_review_policy=str(
                            body.get("automation_dubbing_review_policy") or "block"
                        ),
                        dubbing_enabled=body.get("dubbing_enabled") is True,
                        dubbing_reference_mode=str(
                            body.get("dubbing_reference_mode") or "auto"
                        ),
                        dubbing_reference_start=(
                            _optional_float(
                                body.get("dubbing_reference_start"),
                                "参考声音开始时间",
                            )
                        ),
                        dubbing_reference_end=(
                            _optional_float(
                                body.get("dubbing_reference_end"),
                                "参考声音结束时间",
                            )
                        ),
                        dubbing_subtitle_display=str(
                            body.get("dubbing_subtitle_display") or "chinese"
                        ),
                        force_dubbing=body.get("force_dubbing") is True,
                    )
                    self._json(HTTPStatus.ACCEPTED, {"jobs": jobs})
                    return
                if parsed.path == "/api/pipeline":
                    tasks = body.get("tasks")
                    jobs = app.queue_pipeline(
                        tasks=[str(item) for item in tasks] if isinstance(tasks, list) else [],
                        workflow=str(body.get("workflow") or "complete"),
                        render_mode=str(body.get("render_mode") or "hardsub"),
                        chinese_subtitle_source=str(
                            body.get("chinese_subtitle_source") or "deepseek"
                        ),
                        allow_paid_api=body.get("allow_paid_api") is True,
                        whisper_for_auto_subtitles=(
                            body.get("whisper_for_auto_subtitles") is not False
                        ),
                        auto_translate_missing=(
                            body.get("auto_translate_missing") is not False
                        ),
                        auto_publish=body.get("auto_publish") is True,
                        publish_metadata_provider=str(
                            body.get("publish_metadata_provider") or "auto"
                        ),
                        account_id=str(body.get("account_id") or ""),
                        publish_only_self=body.get("publish_only_self") is True,
                        automation_failure_policy=str(
                            body.get("automation_failure_policy") or "skip"
                        ),
                        automation_target=str(
                            body.get("automation_target") or "publish"
                        ),
                        english_subtitle_policy=str(
                            body.get("english_subtitle_policy") or ""
                        ),
                        automation_chinese_policy=str(
                            body.get("automation_chinese_policy") or ""
                        ),
                        automation_silent_video_policy=str(
                            body.get("automation_silent_video_policy")
                            or "publish_original"
                        ),
                        automation_dubbing_review_policy=str(
                            body.get("automation_dubbing_review_policy") or "block"
                        ),
                        dubbing_enabled=body.get("dubbing_enabled") is True,
                        dubbing_reference_mode=str(
                            body.get("dubbing_reference_mode") or "auto"
                        ),
                        dubbing_reference_start=(
                            _optional_float(
                                body.get("dubbing_reference_start"),
                                "参考声音开始时间",
                            )
                        ),
                        dubbing_reference_end=(
                            _optional_float(
                                body.get("dubbing_reference_end"),
                                "参考声音结束时间",
                            )
                        ),
                        dubbing_subtitle_display=str(
                            body.get("dubbing_subtitle_display") or "chinese"
                        ),
                        force_dubbing=body.get("force_dubbing") is True,
                    )
                    self._json(HTTPStatus.ACCEPTED, {"jobs": jobs})
                    return
                if parsed.path == "/api/render-review":
                    raw_edits = body.get("edits")
                    edits = (
                        [item for item in raw_edits if isinstance(item, dict)]
                        if isinstance(raw_edits, list)
                        else []
                    )
                    result = app.save_render_review(
                        task=str(body.get("task") or ""),
                        edits=edits,
                        render_mode=str(body.get("render_mode") or "hardsub"),
                    )
                    self._json(
                        HTTPStatus.ACCEPTED if result.get("job") else HTTPStatus.OK,
                        result,
                    )
                    return
                if parsed.path == "/api/publish":
                    task = str(body.get("task") or "")
                    self._json(
                        HTTPStatus.ACCEPTED,
                        {"job": app.queue_publish(task, body)},
                    )
                    return
                if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/retry"):
                    job_id = parsed.path.split("/")[3]
                    self._json(HTTPStatus.ACCEPTED, {"job": app.retry_job(job_id)})
                    return
                if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/cancel"):
                    job_id = parsed.path.split("/")[3]
                    self._json(HTTPStatus.ACCEPTED, {"job": app.cancel_job(job_id)})
                    return
                if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/delete-log"):
                    job_id = parsed.path.split("/")[3]
                    self._json(HTTPStatus.OK, app.delete_job_log(job_id))
                    return
                if parsed.path == "/api/logs/clear":
                    self._json(HTTPStatus.OK, app.clear_old_logs())
                    return
                if parsed.path == "/api/tasks/delete":
                    self._json(
                        HTTPStatus.OK,
                        app.delete_task(
                            str(body.get("task") or ""),
                            str(body.get("confirmation") or ""),
                        ),
                    )
                    return
                if parsed.path == "/api/tasks/delete-batch":
                    raw_tasks = body.get("tasks")
                    self._json(
                        HTTPStatus.OK,
                        app.delete_tasks(
                            [str(item) for item in raw_tasks]
                            if isinstance(raw_tasks, list)
                            else [],
                            str(body.get("confirmation") or ""),
                        ),
                    )
                    return
                if parsed.path == "/api/open-folder":
                    app.open_task_folder(
                        str(body.get("task") or ""),
                        subfolder=str(body.get("subfolder") or ""),
                    )
                    self._json(HTTPStatus.OK, {"opened": True})
                    return
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
            except YouTubeAPIError as exc:
                self._error(HTTPStatus.BAD_GATEWAY, str(exc))
            except KeyError:
                self._error(HTTPStatus.NOT_FOUND, "任务不存在")
            except (ValueError, OSError, RuntimeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))

        def _validate_local_json_request(self) -> None:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
            if content_type != "application/json":
                raise ValueError("控制面板接口只接受 application/json")
            origin = self.headers.get("Origin", "").rstrip("/")
            if not origin:
                return
            host = self.headers.get("Host", "")
            allowed = {f"http://{host}", f"https://{host}"}
            if origin not in allowed:
                raise ValueError("已拒绝来自其他网页的控制请求")

        def _read_json(self) -> dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError as exc:
                raise ValueError("无效的请求长度") from exc
            if length < 1 or length > MAX_REQUEST_BYTES:
                raise ValueError("请求内容为空或过大")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("请求 JSON 无效") from exc
            if not isinstance(payload, dict):
                raise ValueError("请求 JSON 必须是对象")
            return payload

        def _static(self, request_path: str) -> None:
            relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
            candidate = (static_root / relative).resolve()
            try:
                candidate.relative_to(static_root)
            except ValueError:
                self._error(HTTPStatus.NOT_FOUND, "文件不存在")
                return
            if not candidate.is_file():
                self._error(HTTPStatus.NOT_FOUND, "文件不存在")
                return
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            data = candidate.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' https://i.ytimg.com https://*.googleusercontent.com data:; "
                "style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: HTTPStatus, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: HTTPStatus, message: str) -> None:
            self._json(status, {"error": message})

        def log_message(self, format: str, *args: Any) -> None:
            return

    return PanelRequestHandler


__all__ = ["make_handler"]
