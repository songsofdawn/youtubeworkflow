from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
from pathlib import Path
from typing import Any


for stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.dubbing.runtime import activate_project_tools


activate_project_tools(PROJECT_ROOT)

from src.dubbing.config import load_dubbing_config
from src.dubbing.model_pool import WarmVoxCPM2Pool
from src.dubbing.pipeline import DubbingError, DubbingPipeline


RESULT_MARKER = "[DUBBING_WORKER_RESULT] "
READY_MARKER = "[DUBBING_WORKER_READY] "


def _emit(marker: str, payload: dict[str, Any]) -> None:
    print(marker + json.dumps(payload, ensure_ascii=False), flush=True)


def _run_request(payload: dict[str, Any], pool: WarmVoxCPM2Pool) -> dict[str, Any]:
    config_path = payload.get("config")
    config = load_dubbing_config(PROJECT_ROOT, config_path)
    pipeline = DubbingPipeline(
        PROJECT_ROOT,
        config,
        python_executable=Path(sys.executable),
        synthesizer_factory=pool.acquire,
    )
    result = pipeline.run(
        Path(str(payload["video_dir"])),
        reference_mode=str(payload.get("reference_mode") or "auto"),
        reference_start=payload.get("reference_start"),
        reference_end=payload.get("reference_end"),
        force_separation=bool(payload.get("force_separation")),
        force_tts=bool(payload.get("force_tts")),
    )
    return {
        "status": result.status,
        "manifest_path": str(result.manifest_path),
        "dubbed_audio_path": str(result.dubbed_audio_path or ""),
        "needs_review": result.needs_review,
        "warnings": result.warnings,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent local VoxCPM2 dubbing worker")
    parser.add_argument("--idle-timeout", type=float, default=45.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    timeout = max(5.0, min(float(args.idle_timeout), 300.0))
    requests: queue.Queue[str | None] = queue.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            requests.put(line)
        requests.put(None)

    threading.Thread(target=read_stdin, daemon=True).start()
    pool = WarmVoxCPM2Pool(log=lambda message: print(message, flush=True))
    _emit(READY_MARKER, {"status": "ready", "idle_timeout_seconds": timeout})
    try:
        while True:
            try:
                line = requests.get(timeout=timeout)
            except queue.Empty:
                pool.close(reason="Dubbing worker idle timeout reached; releasing VoxCPM2.")
                return 0
            if line is None:
                pool.close(reason="Dubbing worker input closed; releasing VoxCPM2.")
                return 0
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                _emit(RESULT_MARKER, {"ok": False, "exit_code": 2, "error": str(exc)})
                continue
            command = str(payload.get("command") or "run")
            request_id = str(payload.get("request_id") or "")
            if command == "shutdown":
                pool.close(reason=str(payload.get("reason") or "Persistent worker shutdown."))
                _emit(RESULT_MARKER, {"ok": True, "request_id": request_id, "exit_code": 0})
                return 0
            if command == "release":
                pool.close(reason=str(payload.get("reason") or "Releasing VoxCPM2."))
                _emit(RESULT_MARKER, {"ok": True, "request_id": request_id, "exit_code": 0})
                continue
            try:
                result = _run_request(payload, pool)
            except DubbingError as exc:
                print(f"中文配音失败：{exc.message}", file=sys.stderr, flush=True)
                if exc.details:
                    print(json.dumps(exc.details, ensure_ascii=False), file=sys.stderr, flush=True)
                pool.close(reason="Dubbing task failed; releasing VoxCPM2 before worker exit.")
                _emit(
                    RESULT_MARKER,
                    {
                        "ok": False,
                        "request_id": request_id,
                        "exit_code": 2,
                        "error": exc.message,
                        "code": exc.code,
                        "worker_exiting": True,
                    },
                )
                return 2
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"中文配音失败：{exc}", file=sys.stderr, flush=True)
                pool.close(reason="Dubbing worker encountered an unrecoverable error.")
                _emit(
                    RESULT_MARKER,
                    {
                        "ok": False,
                        "request_id": request_id,
                        "exit_code": 2,
                        "error": str(exc),
                        "worker_exiting": True,
                    },
                )
                return 2
            _emit(
                RESULT_MARKER,
                {
                    "ok": True,
                    "request_id": request_id,
                    "exit_code": 0,
                    "result": result,
                    "model_loaded": pool.loaded,
                },
            )
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
