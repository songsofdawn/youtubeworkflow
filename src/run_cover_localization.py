from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cover_localization import CoverLocalizer


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成 Bilibili 中文本地化封面")
    parser.add_argument("--video-dir", required=True, help="下载任务目录")
    parser.add_argument("--mode", choices=("local", "cloud"), default=None)
    parser.add_argument("--allow-cloud-api", action="store_true", help="授权当前翻译 API 生成封面短文案；图片始终由 Pillow 本地绘制")
    parser.add_argument("--phase", choices=("all", "analyze", "finalize"), default="all")
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
        help="项目根目录，默认使用当前源码项目",
    )
    parser.add_argument(
        "--allow-paid-api",
        action="store_true",
        help="允许复用当前翻译供应商生成封面文案；默认不调用云端 API",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略已完成封面缓存并重新生成",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = CoverLocalizer(
            args.video_dir,
            project_root=args.project_root,
            allow_paid_api=args.allow_paid_api,
            force=args.force,
            mode=args.mode, allow_cloud_api=args.allow_cloud_api,
        ).run(phase=args.phase)
    except Exception as exc:  # Cover generation is an optional, non-blocking stage.
        result = {
            "status": "FAILED",
            "error": type(exc).__name__,
            "original_cover_preserved": True,
        }
    print(json.dumps(result, ensure_ascii=False, default=str))
    # The caller must never treat an optional cover failure as a failed video
    # download, subtitle, dubbing, render, or publish task.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
