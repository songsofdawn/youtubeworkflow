from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.stage4.ffmpeg_runner import readable_command
from src.stage4.models import PipelineOptions, Stage4Error
from src.stage4.render_pipeline import Stage4Pipeline


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    if not source.is_absolute():
        source = PROJECT_ROOT / source
    if not source.is_file():
        raise Stage4Error("STAGE4_CONFIG_NOT_FOUND", f"阶段四配置不存在：{source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise Stage4Error("STAGE4_CONFIG_INVALID", f"阶段四配置 JSON 无效：{exc}") from exc
    if not isinstance(value, dict):
        raise Stage4Error("STAGE4_CONFIG_INVALID", "阶段四配置顶层必须是 JSON 对象。")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="阶段四：保留原始配音并生成中英双语字幕成片")
    parser.add_argument("--video-dir", required=True, help="单个视频任务目录")
    parser.add_argument("--mode", choices=("ass", "softsub", "hardsub", "both"), default=None)
    parser.add_argument(
        "--config",
        default="config/stage4_config.json",
        help="相对于项目根目录的阶段四配置路径",
    )
    parser.add_argument(
        "--video-encoder",
        choices=("auto", "h264_nvenc", "libx264"),
        default=None,
    )
    parser.add_argument("--resume", action="store_true", default=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-ass", action="store_true")
    parser.add_argument("--force-softsub", action="store_true")
    parser.add_argument("--force-hardsub", action="store_true")
    parser.add_argument("--strict-subtitle-layout", action="store_true")
    parser.add_argument("--require-reviewed", action="store_true")
    parser.add_argument("--require-audio-copy", action="store_true")
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        render = config.get("render", {})
        mode = args.mode or str(render.get("default_mode", "softsub"))
        encoder = args.video_encoder or str(render.get("video_encoder", "auto"))
        options = PipelineOptions(
            mode=mode,
            video_encoder=encoder,
            resume=args.resume,
            force=args.force,
            force_ass=args.force_ass,
            force_softsub=args.force_softsub,
            force_hardsub=args.force_hardsub,
            strict_subtitle_layout=args.strict_subtitle_layout,
            require_reviewed=args.require_reviewed,
            require_audio_copy=args.require_audio_copy,
            keep_temp=args.keep_temp,
            dry_run=args.dry_run,
        )
        result = Stage4Pipeline(PROJECT_ROOT, config).run(args.video_dir, options)
    except Stage4Error as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr, flush=True)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print(f"阶段四状态：{result.status}")
    label = "Dry-run 报告" if args.dry_run else "Manifest"
    print(f"{label}：{result.manifest_path}")
    for name, command in result.plan.get("commands", {}).items():
        print(f"{name} FFmpeg：{readable_command(command)}")
    if result.warnings:
        print("警告：" + "；".join(str(item) for item in result.warnings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
