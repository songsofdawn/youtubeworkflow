from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.download_core import (
    PROJECT_ROOT,
    download_one_video,
    find_local_tools,
    load_download_config,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download one rights-cleared YouTube URL (playlists are always disabled).")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, default=Path("downloads/manual"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--metadata-only", action="store_true")
    mode.add_argument("--subtitles-only", action="store_true")
    parser.add_argument("--no-audio-extract", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--cookies-path",
        type=Path,
        help="Override the configured cookies.txt path (used by isolated portable workers).",
    )
    parser.add_argument("--confirm-rights", action="store_true", help="Confirm you have permission to download and use this video.")
    parser.add_argument(
        "--rights-status",
        choices=("APPROVED", "OWNED", "LICENSED", "PERMISSION_GRANTED"),
        default="",
        help="Optional rights record saved in the download manifest.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
    args = parse_args(argv)
    if not args.confirm_rights:
        print("未执行：请确认你拥有下载和使用该视频的权利，并添加 --confirm-rights。", file=sys.stderr)
        return 2
    try:
        config = load_download_config()
        if args.cookies_path is not None:
            config["cookies_path"] = str(args.cookies_path.resolve())
        tools = find_local_tools()
        output = args.output if args.output.is_absolute() else PROJECT_ROOT / args.output
        result = download_one_video(
            args.url, source_mode="manual",
            candidate={"rights_status": args.rights_status},
            output_root=output, config=config, tools=tools,
            metadata_only=args.metadata_only, subtitles_only=args.subtitles_only,
            no_audio_extract=args.no_audio_extract, force=args.force,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"下载配置、工具或参数错误: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"overall_status": result["overall_status"], "already_complete": result.get("already_complete", False), "task_dir": str(result["task_dir"])}, ensure_ascii=False, indent=2))
    return 0 if result["overall_status"] in {"success", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
