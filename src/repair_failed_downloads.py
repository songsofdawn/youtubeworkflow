from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

try:
    from .download_core import PROJECT_ROOT, download_one_video, find_local_tools, get_project_paths, load_download_config
    from .download_selected_candidates import load_candidate_records, selected_for_download
except ImportError:  # Direct execution: python src/repair_failed_downloads.py
    from download_core import PROJECT_ROOT, download_one_video, find_local_tools, get_project_paths, load_download_config
    from download_selected_candidates import load_candidate_records, selected_for_download


LOGGER = logging.getLogger("stage2_repair")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resume incomplete Stage 2 candidate downloads from their manifests.")
    parser.add_argument("--root", type=Path, default=Path("downloads/candidates"), help="Candidate download root to scan.")
    parser.add_argument("--input", type=Path, help="Optional candidate JSON override when a manifest's candidate_file is unavailable.")
    parser.add_argument("--video-ids", nargs="+", help="Repair only these video IDs.")
    parser.add_argument("--attempts", type=int, default=3, help="Maximum task-level attempts for transient failures (default: 3).")
    parser.add_argument("--retry-delay", type=float, default=3.0, help="Seconds between task-level attempts (default: 3).")
    parser.add_argument("--dry-run", action="store_true", help="List repairable tasks without downloading.")
    parser.add_argument("--force", action="store_true", help="Force all steps for selected incomplete tasks; normally omit this.")
    return parser.parse_args(argv)


def load_manifest(path: Path | str) -> dict[str, Any]:
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"_manifest_error": str(exc)}
    return payload if isinstance(payload, dict) else {"_manifest_error": "manifest 顶层不是 JSON 对象"}


def task_has_required_files(task_dir: Path, manifest: dict[str, Any], config: dict[str, Any]) -> bool:
    required_nonempty = [
        task_dir / "video" / "source.mp4",
        task_dir / "metadata" / "info.json",
    ]
    if config.get("extract_audio", True):
        required_nonempty.append(task_dir / "audio" / "source_audio.wav")
    files_complete = (task_dir / "metadata" / "description.txt").is_file() and all(path.is_file() and path.stat().st_size > 0 for path in required_nonempty)
    clean_tracked = manifest.get("subtitle_clean_status") in {"success", "missing"}
    return manifest.get("overall_status") == "success" and files_complete and clean_tracked


def discover_incomplete_tasks(root: Path | str, config: dict[str, Any], video_ids: list[str] | None = None) -> list[dict[str, Any]]:
    download_root = Path(root)
    requested = set(video_ids or [])
    tasks: list[dict[str, Any]] = []
    if not download_root.is_dir():
        return tasks
    for manifest_path in sorted(download_root.rglob("download_manifest.json")):
        manifest = load_manifest(manifest_path)
        video_id = str(manifest.get("video_id") or "")
        if requested and video_id not in requested:
            continue
        if manifest.get("_manifest_error") or not task_has_required_files(manifest_path.parent, manifest, config):
            tasks.append({"manifest_path": manifest_path, "task_dir": manifest_path.parent, "manifest": manifest, "video_id": video_id})
    return tasks


def _candidate_file_from_manifest(manifest: dict[str, Any], override: Path | None) -> Path | None:
    candidates: list[Path] = []
    if override:
        candidates.append(override)
    if manifest.get("candidate_file"):
        candidates.append(Path(str(manifest["candidate_file"])))
    for candidate_file in candidates:
        path = candidate_file if candidate_file.is_absolute() else PROJECT_ROOT / candidate_file
        if path.is_file():
            return path
    return None


def recover_candidate(task: dict[str, Any], candidate_override: Path | None = None) -> tuple[dict[str, Any] | None, Path | None, str]:
    manifest = task["manifest"]
    video_id = str(manifest.get("video_id") or "")
    local_copy = task["task_dir"] / "metadata" / "candidate.json"
    if local_copy.is_file():
        try:
            candidate = json.loads(local_copy.read_text(encoding="utf-8-sig"))
            if isinstance(candidate, dict) and str(candidate.get("video_id") or candidate.get("id") or "") == video_id:
                return candidate, _candidate_file_from_manifest(manifest, candidate_override), ""
        except (OSError, json.JSONDecodeError):
            pass
    candidate_file = _candidate_file_from_manifest(manifest, candidate_override)
    if not candidate_file:
        return None, None, "找不到 manifest 指向的候选 JSON；可使用 --input 指定"
    try:
        records = load_candidate_records(candidate_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, candidate_file, f"候选 JSON 无法读取: {exc}"
    candidate = next((item for item in records if str(item.get("video_id") or item.get("id") or "") == video_id), None)
    if not candidate:
        return None, candidate_file, f"候选 JSON 中找不到视频 ID {video_id}"
    return candidate, candidate_file, ""


def _candidate_url(candidate: dict[str, Any], manifest: dict[str, Any]) -> str:
    url = str(candidate.get("youtube_url") or candidate.get("webpage_url") or candidate.get("url") or manifest.get("url") or "").strip()
    video_id = str(candidate.get("video_id") or candidate.get("id") or manifest.get("video_id") or "").strip()
    return url or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")


def repair_incomplete_tasks(
    root: Path | str,
    *,
    candidate_override: Path | None = None,
    video_ids: list[str] | None = None,
    attempts: int = 3,
    retry_delay: float = 3.0,
    dry_run: bool = False,
    force: bool = False,
    config: dict[str, Any] | None = None,
    tools: dict[str, Path] | None = None,
) -> dict[str, int]:
    config = config or load_download_config()
    tools = tools or find_local_tools()
    approved = {str(status).strip().upper() for status in config["approved_rights_statuses"]}
    tasks = discover_incomplete_tasks(root, config, video_ids)
    stats = {
        "discovered": len(tasks), "repairable": 0, "dry_run": 0, "repaired_success": 0,
        "partial_success": 0, "failed": 0, "skipped_rights": 0, "skipped_invalid": 0,
    }
    for task in tasks:
        manifest = task["manifest"]
        video_id = task["video_id"] or "<unknown>"
        if manifest.get("_manifest_error"):
            stats["skipped_invalid"] += 1
            LOGGER.error("跳过损坏 manifest %s: %s", task["manifest_path"], manifest["_manifest_error"])
            continue
        candidate, candidate_file, error = recover_candidate(task, candidate_override)
        if not candidate:
            stats["skipped_invalid"] += 1
            LOGGER.error("无法恢复 %s: %s", video_id, error)
            continue
        rights_status = str(candidate.get("rights_status") or "").strip().upper()
        if not selected_for_download(candidate.get("selected")) or rights_status not in approved:
            stats["skipped_rights"] += 1
            LOGGER.warning("跳过 %s: 当前候选记录未通过 selected/rights_status 检查", video_id)
            continue
        url = _candidate_url(candidate, manifest)
        if not url:
            stats["skipped_invalid"] += 1
            LOGGER.error("跳过 %s: 无法恢复视频 URL", video_id)
            continue
        stats["repairable"] += 1
        if dry_run:
            stats["dry_run"] += 1
            LOGGER.info("待修复 %s | %s | 原状态=%s", video_id, task["task_dir"], manifest.get("overall_status"))
            continue
        rank_value = manifest.get("candidate_rank") or candidate.get("rank")
        try:
            rank = int(rank_value)
        except (TypeError, ValueError):
            rank = None
        final_status = "failed"
        for attempt in range(1, max(1, attempts) + 1):
            try:
                result = download_one_video(
                    url, source_mode="candidate", candidate=candidate, candidate_file=candidate_file,
                    candidate_rank=rank, output_root=Path(root), config=config, tools=tools, force=force,
                )
                final_status = str(result.get("overall_status") or "failed")
            except Exception as exc:  # Keep repairing later tasks after an unexpected per-task failure.
                final_status = "failed"
                LOGGER.exception("修复 %s 第 %d 次尝试异常: %s", video_id, attempt, exc)
            if final_status == "success":
                break
            if attempt < max(1, attempts):
                LOGGER.warning("修复 %s 第 %d 次未成功（%s），%.1f 秒后重试", video_id, attempt, final_status, max(0.0, retry_delay))
                if retry_delay > 0:
                    time.sleep(retry_delay)
        if final_status == "success":
            stats["repaired_success"] += 1
        elif final_status == "partial_success":
            stats["partial_success"] += 1
        else:
            stats["failed"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
    args = parse_args(argv)
    if args.attempts < 1 or args.retry_delay < 0:
        LOGGER.error("--attempts 必须至少为 1，--retry-delay 不能为负数")
        return 2
    try:
        root = args.root if args.root.is_absolute() else PROJECT_ROOT / args.root
        candidate_override = args.input
        if candidate_override and not candidate_override.is_absolute():
            candidate_override = PROJECT_ROOT / candidate_override
        config = load_download_config()
        tools = find_local_tools()
        stats = repair_incomplete_tasks(
            root, candidate_override=candidate_override, video_ids=args.video_ids,
            attempts=args.attempts, retry_delay=args.retry_delay, dry_run=args.dry_run,
            force=args.force, config=config, tools=tools,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("修复配置、工具或目录错误: %s", exc)
        return 2
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 1 if stats["failed"] or stats["partial_success"] or stats["skipped_invalid"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
