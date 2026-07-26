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


MAX_REQUEST_BYTES = 1024 * 1024


def make_handler(app: ControlPanelApp, static_root: Path) -> type[BaseHTTPRequestHandler]:
    static_root = static_root.resolve()

    class PanelRequestHandler(BaseHTTPRequestHandler):
        server_version = "YouTubeWorkflowPanel/1.0"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/api/dashboard":
                    self._json(HTTPStatus.OK, app.dashboard())
                    return
                if parsed.path == "/api/health":
                    self._json(HTTPStatus.OK, app.health())
                    return
                if parsed.path == "/api/tasks":
                    self._json(HTTPStatus.OK, {"tasks": app.scanner.scan()})
                    return
                if parsed.path == "/api/publish/defaults":
                    query = parse_qs(parsed.query)
                    task = str((query.get("task") or [""])[0])
                    self._json(HTTPStatus.OK, app.publish_defaults(task))
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
                if parsed.path == "/api/downloads":
                    jobs = app.queue_downloads(
                        raw_input=str(body.get("input") or ""),
                        items=body.get("items") if isinstance(body.get("items"), list) else None,
                        confirm_rights=body.get("confirm_rights") is True,
                    )
                    self._json(HTTPStatus.ACCEPTED, {"jobs": jobs})
                    return
                if parsed.path == "/api/pipeline":
                    tasks = body.get("tasks")
                    jobs = app.queue_pipeline(
                        tasks=[str(item) for item in tasks] if isinstance(tasks, list) else [],
                        workflow=str(body.get("workflow") or "complete"),
                        render_mode=str(body.get("render_mode") or "softsub"),
                        allow_paid_api=body.get("allow_paid_api") is True,
                    )
                    self._json(HTTPStatus.ACCEPTED, {"jobs": jobs})
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
                if parsed.path == "/api/open-folder":
                    app.open_task_folder(str(body.get("task") or ""))
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
