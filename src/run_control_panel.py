from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from http.server import ThreadingHTTPServer
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.control_panel import ControlPanelApp
from src.control_panel.server import make_handler


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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

    app = ControlPanelApp(PROJECT_ROOT)
    handler = make_handler(app, static_root)
    try:
        server = ThreadingHTTPServer((args.host, args.port), handler)
    except OSError as exc:
        print(f"无法启动控制面板：{exc}", file=sys.stderr)
        return 2

    app.start()
    url = f"http://{args.host}:{args.port}"
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
