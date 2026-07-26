from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.control_panel import ControlPanelApp
from src.control_panel.server import make_handler


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def panel_build_id(project_root: Path) -> str:
    digest = hashlib.sha256()
    roots = (project_root / "src", project_root / "config")
    allowed_suffixes = {".py", ".js", ".css", ".html", ".json"}
    files = sorted(
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.casefold() in allowed_suffixes
        and "__pycache__" not in path.parts
    )
    for path in files:
        digest.update(path.relative_to(project_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


def _local_json(url: str, path: str, timeout: float = 1.5) -> dict:
    request = urllib.request.Request(
        f"{url}{path}",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def _same_project(left: str, right: Path) -> bool:
    try:
        return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(
            str(right.resolve())
        )
    except (OSError, ValueError):
        return False


def existing_panel(url: str, project_root: Path) -> dict:
    try:
        runtime = _local_json(url, "/api/runtime")
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return {}
    if not _same_project(str(runtime.get("project_root") or ""), project_root):
        return {}
    try:
        runtime["pid"] = int(runtime.get("pid") or 0)
    except (TypeError, ValueError):
        return {}
    return runtime if runtime["pid"] > 0 else {}


def _panel_has_active_jobs(url: str) -> bool:
    try:
        dashboard = _local_json(url, "/api/dashboard", timeout=3)
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError):
        return True
    summary = dashboard.get("summary") or {}
    return int(summary.get("running") or 0) > 0 or int(summary.get("queued") or 0) > 0


def _terminate_existing_panel(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        raise RuntimeError("旧面板进程号无效")
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            message = (completed.stdout or completed.stderr).strip()
            raise RuntimeError(f"无法关闭旧面板进程：{message}")
        return
    os.kill(pid, signal.SIGTERM)


def _wait_for_port_release(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex((host, port)) != 0:
                return True
        time.sleep(0.1)
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动 YouTube Workflow 本地控制面板")
    parser.add_argument(
        "--host",
        choices=("127.0.0.1", "localhost"),
        default="127.0.0.1",
        help="为安全起见，控制面板只允许监听本机",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--replace-existing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="代码更新且没有活动任务时，自动替换同项目的旧面板进程",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        print("端口必须在 1 到 65535 之间", file=sys.stderr)
        return 2
    static_root = PROJECT_ROOT / "src" / "control_panel" / "static"
    if not (static_root / "index.html").is_file():
        print(f"控制面板页面不存在：{static_root}", file=sys.stderr)
        return 2

    url = f"http://{args.host}:{args.port}"
    build_id = panel_build_id(PROJECT_ROOT)
    previous = existing_panel(url, PROJECT_ROOT)
    if previous:
        if str(previous.get("build_id") or "") == build_id:
            print(f"控制面板已经运行：{url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        if not args.replace_existing:
            print("检测到旧版控制面板，请先关闭旧窗口后再启动。", file=sys.stderr)
            return 2
        if _panel_has_active_jobs(url):
            print(
                "检测到旧版控制面板仍有活动任务；为保护任务，暂不自动重启。"
                "请等待任务完成后再次双击 start_panel.bat。",
                file=sys.stderr,
            )
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        try:
            _terminate_existing_panel(int(previous["pid"]))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if not _wait_for_port_release(args.host, args.port):
            print("旧面板已关闭，但端口尚未释放，请稍后重试。", file=sys.stderr)
            return 2

    app = ControlPanelApp(PROJECT_ROOT)
    handler = make_handler(
        app,
        static_root,
        {
            "project_root": str(PROJECT_ROOT.resolve()),
            "pid": os.getpid(),
            "build_id": build_id,
        },
    )
    try:
        server = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        print(f"无法启动控制面板：{exc}", file=sys.stderr)
        return 2

    app.start()
    print("=" * 66)
    print("YouTube Workflow 控制面板已启动")
    print(f"地址：{url}")
    print("关闭此窗口即可停止面板；正在运行的子任务会被安全终止。")
    print("=" * 66)
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n正在关闭控制面板…")
    finally:
        server.shutdown()
        server.server_close()
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
