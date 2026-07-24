from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.stage4.ffmpeg_runner import readable_command
from src.stage4.models import PipelineOptions, Stage4Error
from src.stage4.render_pipeline import Stage4Pipeline
from src.stage4.stage4_manifest import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKIPPABLE_INPUT_ERRORS = {
    "EN_SELECTED_SUBTITLE_NOT_FOUND",
    "CHINESE_SUBTITLE_NOT_FOUND",
    "NO_VALID_CHINESE_SUBTITLE",
    "ZH_REVIEWED_SUBTITLE_NOT_FOUND",
}


def load_config(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    if not source.is_absolute():
        source = PROJECT_ROOT / source
    if not source.is_file():
        raise Stage4Error("STAGE4_CONFIG_NOT_FOUND", f"阶段四配置不存在：{source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise Stage4Error(
            "STAGE4_CONFIG_INVALID",
            f"阶段四配置 JSON 无效：{exc}",
        ) from exc
    if not isinstance(value, dict):
        raise Stage4Error("STAGE4_CONFIG_INVALID", "阶段四配置顶层必须是 JSON 对象。")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="阶段四：保留原始配音并生成中英双语字幕成片"
    )
    parser.add_argument(
        "--video-dir",
        required=True,
        help="单个视频任务目录，或包含多个视频任务的日期/批次目录",
    )
    parser.add_argument(
        "--mode",
        choices=("ass", "softsub", "hardsub", "both"),
        default=None,
    )
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


def discover_video_dirs(input_dir: Path | str) -> list[Path]:
    """Resolve one downloaded-video task or all tasks below a batch directory."""
    root = Path(input_dir).resolve()
    if not root.is_dir():
        raise Stage4Error("VIDEO_DIR_NOT_FOUND", f"视频任务目录不存在：{root}")
    if (root / "download_manifest.json").is_file():
        return [root]
    return sorted(
        {
            manifest.parent.resolve()
            for manifest in root.rglob("download_manifest.json")
            if "stage4"
            not in {part.casefold() for part in manifest.relative_to(root).parts}
        }
    )


def _options_from_args(
    args: argparse.Namespace,
    config: dict[str, Any],
) -> PipelineOptions:
    render = config.get("render", {})
    mode = args.mode or str(render.get("default_mode", "softsub"))
    encoder = args.video_encoder or str(render.get("video_encoder", "auto"))
    return PipelineOptions(
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


def _print_result(result: Any, *, dry_run: bool) -> None:
    print(f"阶段四状态：{result.status}")
    label = "Dry-run 报告" if dry_run else "Manifest"
    print(f"{label}：{result.manifest_path}")
    for name, command in result.plan.get("commands", {}).items():
        print(f"{name} FFmpeg：{readable_command(command)}")
    if result.warnings:
        print("警告：" + "；".join(str(item) for item in result.warnings))


def run_batch(
    pipeline: Stage4Pipeline,
    input_dir: Path,
    video_dirs: list[Path],
    options: PipelineOptions,
) -> tuple[int, Path, dict[str, Any]]:
    records: list[dict[str, Any]] = []
    succeeded = skipped = failed = 0
    for index, task_dir in enumerate(video_dirs, 1):
        print(
            f"\n[{index}/{len(video_dirs)}] 处理视频：{task_dir.name}",
            flush=True,
        )
        try:
            result = pipeline.run(task_dir, options)
        except Stage4Error as exc:
            is_skipped = exc.code in SKIPPABLE_INPUT_ERRORS
            status = "SKIPPED_NOT_READY" if is_skipped else "FAILED"
            skipped += int(is_skipped)
            failed += int(not is_skipped)
            records.append(
                {
                    "video_dir": str(task_dir),
                    "status": status,
                    "error": exc.to_dict(),
                    "manifest_path": str(
                        task_dir / "stage4" / "stage4_manifest.json"
                    ),
                }
            )
            label = "跳过" if is_skipped else "失败"
            print(
                f"[{exc.code}] {label}：{exc.message}",
                file=sys.stderr,
                flush=True,
            )
            continue
        except Exception as exc:
            failed += 1
            records.append(
                {
                    "video_dir": str(task_dir),
                    "status": "FAILED",
                    "error": {
                        "code": "UNEXPECTED_STAGE4_ERROR",
                        "message": str(exc),
                        "details": {"type": type(exc).__name__},
                    },
                    "manifest_path": str(
                        task_dir / "stage4" / "stage4_manifest.json"
                    ),
                }
            )
            print(
                f"[UNEXPECTED_STAGE4_ERROR] 失败：{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            continue

        succeeded += 1
        records.append(
            {
                "video_dir": str(task_dir),
                "status": result.status,
                "manifest_path": str(result.manifest_path),
                "warnings": result.warnings,
            }
        )
        print(f"完成：{result.status}", flush=True)

    summary = {
        "input_directory": str(input_dir),
        "mode": options.mode,
        "dry_run": options.dry_run,
        "video_task_count": len(video_dirs),
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
    }
    report = {"summary": summary, "videos": records}
    batch_root = input_dir / "stage4"
    batch_root.mkdir(parents=True, exist_ok=True)
    report_path = batch_root / (
        "batch_dry_run_summary.json" if options.dry_run else "batch_summary.json"
    )
    atomic_write_json(report_path, report)
    print(
        "\n批次汇总："
        f"总计 {len(video_dirs)}，成功 {succeeded}，"
        f"跳过 {skipped}，失败 {failed}",
        flush=True,
    )
    print(f"批次报告：{report_path}", flush=True)
    exit_code = 0 if failed == 0 and succeeded > 0 else 2
    return exit_code, report_path, report


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        options = _options_from_args(args, config)
        input_dir = Path(args.video_dir).resolve()
        video_dirs = discover_video_dirs(input_dir)
        if not video_dirs:
            raise Stage4Error(
                "VIDEO_TASKS_NOT_FOUND",
                f"目录下没有找到含 download_manifest.json 的视频任务：{input_dir}",
            )
        pipeline = Stage4Pipeline(PROJECT_ROOT, config)
        if video_dirs != [input_dir]:
            exit_code, _, _ = run_batch(
                pipeline,
                input_dir,
                video_dirs,
                options,
            )
            return exit_code
        result = pipeline.run(input_dir, options)
    except Stage4Error as exc:
        print(f"[{exc.code}] {exc.message}", file=sys.stderr, flush=True)
        if exc.details:
            print(
                json.dumps(exc.details, ensure_ascii=False, indent=2),
                file=sys.stderr,
            )
        return 2

    _print_result(result, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
