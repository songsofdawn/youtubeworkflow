from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.stage3.pipeline import Stage3Pipeline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 3 English reconstruction and contextual Chinese translation.")
    parser.add_argument("--video-dir", required=True, type=Path)
    parser.add_argument("--steps", default="clean,translate", help="Comma-separated actions: clean, translate")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "stage3_config.json")
    parser.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--polish-all", action="store_true")
    parser.add_argument("--allow-paid-api", action="store_true")
    return parser.parse_args(argv)


def load_config(path: Path) -> dict:
    resolved = path if path.is_absolute() else PROJECT_ROOT / path
    config = json.loads(resolved.read_text(encoding="utf-8"))
    required = {
        "source_priority", "sentence_gap_seconds", "min_segment_duration", "max_segment_duration",
        "minimum_gap_ms", "english_max_chars_per_line", "chinese_max_chars_per_line", "max_lines",
        "translation_batch_size", "context_before", "context_after", "temperature", "max_retries",
        "chinese_chars_per_second", "tts_warning_ratio", "tts_rewrite_ratio",
    }
    missing = sorted(required - config.keys())
    if missing:
        raise ValueError(f"stage3 config missing keys: {missing}")
    return config


def discover_video_dirs(input_dir: Path) -> list[Path]:
    """Return one video task or every downloaded video task below a batch directory."""
    resolved = input_dir.resolve()
    if (resolved / "download_manifest.json").is_file():
        return [resolved]
    tasks = sorted({manifest.parent.resolve() for manifest in resolved.rglob("download_manifest.json")})
    return tasks


def run_video_actions(
    video_dir: Path,
    steps: list[str],
    config: dict,
    args: argparse.Namespace,
) -> dict[str, object]:
    pipeline = Stage3Pipeline(video_dir, config)
    result: dict[str, object] = {"video_dir": str(video_dir), "status": "SUCCESS"}
    if "clean" in steps:
        cleaning = pipeline.run_p0()
        result["english_subtitle_cleaning"] = cleaning
        if cleaning.get("status") == "NO_ENGLISH_SUBTITLE":
            result["status"] = "SKIPPED_NO_ENGLISH_SUBTITLE"
            result["chinese_translation"] = {"status": "SKIPPED", "reason": "No English subtitle was found"}
            return result
    if "translate" in steps:
        result["chinese_translation"] = pipeline.run_p1(
            allow_paid_api=args.allow_paid_api,
            force=args.force or not args.resume,
            polish_all=args.polish_all,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    video_dir = args.video_dir if args.video_dir.is_absolute() else PROJECT_ROOT / args.video_dir
    try:
        config = load_config(args.config)
        requested_steps = [item.strip().casefold() for item in args.steps.split(",") if item.strip()]
        aliases = {"clean": "clean", "translate": "translate", "p0": "clean", "p1": "translate"}
        invalid = sorted(set(requested_steps) - aliases.keys())
        if invalid:
            raise ValueError(f"Unknown actions: {invalid}")
        steps = [aliases[item] for item in requested_steps]
        video_dirs = discover_video_dirs(video_dir)
        if not video_dirs:
            raise FileNotFoundError(f"No downloaded video tasks were found below: {video_dir}")
        results: list[dict[str, object]] = []
        failed = 0
        skipped = 0
        for index, task_dir in enumerate(video_dirs, 1):
            print(f"[{index}/{len(video_dirs)}] Processing video: {task_dir.name}", flush=True)
            try:
                item = run_video_actions(task_dir, steps, config, args)
                if str(item["status"]).startswith("SKIPPED"):
                    skipped += 1
            except (OSError, ValueError, RuntimeError) as exc:
                failed += 1
                item = {"video_dir": str(task_dir), "status": "FAILED", "error": str(exc)}
                print(f"[{index}/{len(video_dirs)}] Failed: {exc}", file=sys.stderr, flush=True)
            results.append(item)
        succeeded = len(video_dirs) - failed - skipped
        output = {
            "summary": {
                "input_directory": str(video_dir.resolve()),
                "video_task_count": len(video_dirs),
                "succeeded": succeeded,
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
