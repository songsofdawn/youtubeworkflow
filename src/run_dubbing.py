from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.dubbing.runtime import activate_project_tools


# This mutates only this process and descendants. It never changes the Windows
# user/system PATH, and it happens before optional audio libraries are imported.
activate_project_tools(PROJECT_ROOT)

from src.dubbing.config import load_dubbing_config
from src.dubbing.pipeline import DubbingError, DubbingPipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用现有中文字幕、Demucs 和 VoxCPM2 生成可恢复的单主播中文配音"
    )
    parser.add_argument("--video-dir", required=True, type=Path)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config" / "dubbing_config.json",
    )
    parser.add_argument(
        "--reference-mode",
        choices=("auto", "manual"),
        default="auto",
    )
    parser.add_argument("--reference-start", type=float)
    parser.add_argument("--reference-end", type=float)
    parser.add_argument("--force-separation", action="store_true")
    parser.add_argument("--force-tts", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_dubbing_config(PROJECT_ROOT, args.config)
        pipeline = DubbingPipeline(
            PROJECT_ROOT,
            config,
            python_executable=Path(sys.executable),
        )
        result = pipeline.run(
            args.video_dir,
            reference_mode=args.reference_mode,
            reference_start=args.reference_start,
            reference_end=args.reference_end,
            force_separation=args.force_separation,
            force_tts=args.force_tts,
        )
    except DubbingError as exc:
        print(f"中文配音失败：{exc.message}", file=sys.stderr, flush=True)
        if exc.details:
            print(json.dumps(exc.details, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"中文配音失败：{exc}", file=sys.stderr, flush=True)
        return 2
    print(
        json.dumps(
            {
                "status": result.status,
                "manifest_path": str(result.manifest_path),
                "dubbed_audio_path": str(result.dubbed_audio_path or ""),
                "needs_review": result.needs_review,
                "warnings": result.warnings,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
