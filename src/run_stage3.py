from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.stage3.config_adapter import normalize_stage3_config
from src.stage3.pipeline import Stage3Pipeline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the recoverable Stage 3 subtitle workflow.")
    parser.add_argument("--video-dir", required=True, type=Path)
    parser.add_argument(
        "--steps",
        default="clean,translate",
        help="Comma-separated actions: youtube, whisper, select, prepare, translate, review-export, review-import",
    )
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "stage3_config.json")
    parser.add_argument("--subtitle-source", choices=("auto", "manual", "youtube", "whisper"), default="auto")
    parser.add_argument("--asr-max-seconds", type=float)
    parser.add_argument("--review-file", type=Path)
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-youtube", action="store_true")
    parser.add_argument("--force-whisper", action="store_true")
    parser.add_argument("--force-selection", action="store_true")
    parser.add_argument("--force-translation", action="store_true")
    parser.add_argument("--polish-all", action="store_true")
    parser.add_argument("--allow-paid-api", action="store_true")
    parser.add_argument("--overwrite-reviewed", action="store_true")
    return parser.parse_args(argv)


def load_config(path: Path) -> dict:
    resolved = path if path.is_absolute() else PROJECT_ROOT / path
    raw = json.loads(resolved.read_text(encoding="utf-8-sig"))
    config = normalize_stage3_config(raw)
    for key in ("minimum_acceptable_score", "selection_margin"):
        if not 0 <= float(config[key]) <= 100:
            raise ValueError(f"stage3 config value must be between 0 and 100: {key}")
    if not 0 <= float(config["minimum_speech_coverage"]) <= 1:
        raise ValueError("stage3 config minimum_speech_coverage must be between 0 and 1")
    return config


def discover_video_dirs(input_dir: Path) -> list[Path]:
    resolved = input_dir.resolve()
    if (resolved / "download_manifest.json").is_file():
        return [resolved]
    return sorted({manifest.parent.resolve() for manifest in resolved.rglob("download_manifest.json")})


def _display_score(value: object) -> str:
    if value is None or value == "":
        return "无"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def print_source_selection_summary(video_dir: Path, selection: dict[str, object]) -> None:
    comparison = selection.get("selection_report") or selection.get("source_comparison") or {}
    if not isinstance(comparison, dict):
        return
    youtube = comparison.get("youtube") if isinstance(comparison.get("youtube"), dict) else {}
    whisper = comparison.get("whisper") if isinstance(comparison.get("whisper"), dict) else {}
    selected_source = str(comparison.get("selected_source") or "")
    labels = {"youtube": "YouTube 字幕", "whisper": "Whisper"}
    whisper_started = bool(selection.get("whisper_started"))
    whisper_state = "是" if whisper_started else "否"
    if selection.get("asr_checkpoint_reused"):
        whisper_state = "否（复用现有检查点）"
    print("\n========== 英文字幕双源评分与选择 ==========", flush=True)
    print(f"YouTube clean 路径：{youtube.get('path') or '未生成'}", flush=True)
    print(f"YouTube 来源类型：{youtube.get('source_type') or '无'}", flush=True)
    print(f"YouTube 六维总分：{_display_score(youtube.get('final_score'))}", flush=True)
    print(f"Whisper clean 路径：{whisper.get('path') or '未生成'}", flush=True)
    print(f"是否启动 Whisper：{whisper_state}", flush=True)
    print(f"Whisper 六维总分：{_display_score(whisper.get('final_score'))}", flush=True)
    print(f"双源一致度：{_display_score(comparison.get('agreement_score'))}", flush=True)
    print(f"最终字幕来源：{labels.get(selected_source, '未自动选择')}", flush=True)
    print(f"是否需要人工选择：{'是' if comparison.get('review_required') else '否'}", flush=True)
    print(f"选择原因：{comparison.get('selection_reason') or '无'}", flush=True)
    print(f"en.selected.srt 路径：{comparison.get('selected_output_path') or '未生成'}", flush=True)
    print(f"selection_report.json 路径：{video_dir / 'stage3' / 'selection' / 'selection_report.json'}", flush=True)
    print("============================================\n", flush=True)


def _expand_steps(raw_steps: list[str]) -> list[str]:
    aliases = {
        "clean": "youtube",
        "youtube": "youtube",
        "whisper": "whisper",
        "asr": "whisper",
        "select": "select",
        "translate": "translate",
        "review-export": "review-export",
        "review-import": "review-import",
        "p0": "youtube",
        "p1": "translate",
        "p2": "select",
    }
    expanded: list[str] = []
    for step in raw_steps:
        if step == "prepare":
            expanded.extend(("youtube", "whisper", "select"))
        elif step in aliases:
            expanded.append(aliases[step])
        else:
            raise ValueError(f"Unknown action: {step}")
    if "clean" in raw_steps and "translate" in raw_steps and "select" not in expanded:
        expanded.insert(expanded.index("translate"), "select")
    return list(dict.fromkeys(expanded))


def run_video_actions(
    video_dir: Path,
    steps: list[str],
    config: dict,
    args: argparse.Namespace,
) -> dict[str, object]:
    pipeline = Stage3Pipeline(video_dir, config)
    result: dict[str, object] = {"video_dir": str(video_dir), "status": "SUCCESS"}
    ran_youtube = False
    ran_whisper = False
    if "youtube" in steps:
        result["youtube"] = pipeline.run_p0(force=args.force or args.force_youtube or not args.resume)
        ran_youtube = True
    if "whisper" in steps:
        result["whisper"] = pipeline.run_whisper(
            max_seconds=args.asr_max_seconds,
            force=args.force or args.force_whisper or not args.resume,
        )
        ran_whisper = True
    if "select" in steps:
        selection = pipeline.run_p2(
            subtitle_source=args.subtitle_source,
            max_seconds=args.asr_max_seconds,
            force=args.force,
            force_youtube=args.force_youtube,
            force_whisper=args.force_whisper,
            force_selection=args.force_selection,
            prepare_sources=not (ran_youtube and ran_whisper),
        )
        result["english_source_selection"] = selection
        print_source_selection_summary(video_dir, selection)
        if selection["status"] in {"NO_AUDIO_SOURCE", "REVIEW_REQUIRED"}:
            result["status"] = f"SKIPPED_{selection['status']}"
            if "translate" in steps:
                result["chinese_translation"] = {
                    "status": "SKIPPED",
                    "reason": "English subtitle selection requires review",
                }
                return result
    if "translate" in steps:
        result["chinese_translation"] = pipeline.run_p1(
            allow_paid_api=args.allow_paid_api,
            force=args.force or args.force_translation or not args.resume,
            polish_all=args.polish_all,
        )
    if "review-export" in steps:
        result["review_export"] = pipeline.run_review_export()
    if "review-import" in steps:
        if args.review_file is None:
            raise ValueError("--review-file is required for review-import")
        review = pipeline.run_review_import(args.review_file, overwrite_reviewed=args.overwrite_reviewed)
        result["review_import"] = review
        if review["status"] == "REVIEW_IMPORT_FAILED":
            result["status"] = "FAILED_REVIEW_IMPORT"
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    args = parse_args(argv)
    video_dir = args.video_dir if args.video_dir.is_absolute() else PROJECT_ROOT / args.video_dir
    try:
        config = load_config(args.config)
        requested = [item.strip().casefold() for item in args.steps.split(",") if item.strip()]
        steps = _expand_steps(requested)
        video_dirs = discover_video_dirs(video_dir)
        if not video_dirs:
            raise FileNotFoundError(f"No downloaded video tasks were found below: {video_dir}")
        results: list[dict[str, object]] = []
        failed = skipped = 0
        for index, task_dir in enumerate(video_dirs, 1):
            print(f"[{index}/{len(video_dirs)}] Processing video: {task_dir.name}", flush=True)
            try:
                item = run_video_actions(task_dir, steps, config, args)
                if str(item["status"]).startswith("SKIPPED"):
                    skipped += 1
                elif str(item["status"]).startswith("FAILED"):
                    failed += 1
            except (OSError, ValueError, RuntimeError) as exc:
                failed += 1
                item = {"video_dir": str(task_dir), "status": "FAILED", "error": str(exc)}
                print(f"[{index}/{len(video_dirs)}] Failed: {exc}", file=sys.stderr, flush=True)
            results.append(item)
        output = {
            "summary": {
                "input_directory": str(video_dir.resolve()),
                "video_task_count": len(video_dirs),
                "succeeded": len(video_dirs) - failed - skipped,
                "skipped": skipped,
                "failed": failed,
                "paid_api_enabled": bool(args.allow_paid_api),
            },
            "videos": results,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 1 if failed else 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Subtitle workflow failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
