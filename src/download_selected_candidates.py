from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.download_core import (
    PROJECT_ROOT,
    download_one_video,
    find_local_tools,
    get_project_paths,
    load_download_config,
)


LOGGER = logging.getLogger("stage2_batch")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download rights-approved videos from a candidate JSON file.")
    parser.add_argument("--input", type=Path, help="Candidate JSON; defaults to the newest *_US_localization_top50.json.")
    parser.add_argument("--video-ids", nargs="+", help="Only process these IDs; approval checks still apply.")
    return parser.parse_args(argv)


def find_latest_candidate_json(directory: Path | str | None = None) -> Path:
    candidate_dir = Path(directory) if directory else get_project_paths()["candidates"]
    matches = list(candidate_dir.glob("*_US_localization_top50.json"))
    if not matches:
        raise FileNotFoundError(f"未找到候选 JSON: {candidate_dir}")
    return max(matches, key=lambda path: (path.name[:10], path.stat().st_mtime_ns, path.name))


find_latest_candidate_file = find_latest_candidate_json


def load_candidate_records(path: Path | str) -> list[dict[str, Any]]:
    candidate_path = Path(path)
    with candidate_path.open(encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    records = payload.get("candidates") if isinstance(payload, dict) else payload
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise ValueError("候选 JSON 必须是记录数组，或包含 candidates 数组的对象")
    return records


def selected_for_download(value: Any) -> bool:
    return value is True or (isinstance(value, int) and not isinstance(value, bool) and value == 1) or (isinstance(value, str) and value.strip() == "1")


def candidate_url(candidate: dict[str, Any]) -> str:
    video_id = str(candidate.get("video_id") or candidate.get("id") or "").strip()
    url = str(candidate.get("youtube_url") or candidate.get("webpage_url") or candidate.get("url") or "").strip()
    if url:
        return url
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    raise ValueError("候选记录缺少 video_id 和 URL")


def process_candidates(candidate_file: Path | str, video_ids: list[str] | None = None, *, config: dict[str, Any] | None = None, tools: dict[str, Path] | None = None) -> dict[str, int]:
    path = Path(candidate_file)
    records = load_candidate_records(path)
    requested = set(video_ids or [])
    if requested:
        records = [item for item in records if str(item.get("video_id") or item.get("id") or "") in requested]
    config = config or load_download_config()
    tools = tools or find_local_tools()
    approved_statuses = {str(value).strip().upper() for value in config["approved_rights_statuses"]}
    stats = {
        "total": len(records), "approved": 0, "skipped_unselected": 0, "skipped_rights": 0,
        "already_complete": 0, "success": 0, "partial_success": 0, "failed": 0,
    }
    for position, candidate in enumerate(records, 1):
        video_id = str(candidate.get("video_id") or candidate.get("id") or "unknown")
        if not selected_for_download(candidate.get("selected")):
            stats["skipped_unselected"] += 1
            LOGGER.info("跳过 %s: selected 未设为 1", video_id)
            continue
        rights_status = str(candidate.get("rights_status") or "").strip().upper()
        if rights_status not in approved_statuses:
            stats["skipped_rights"] += 1
            LOGGER.info("跳过 %s: rights_status=%s 未获批准", video_id, rights_status or "<empty>")
            continue
        stats["approved"] += 1
        rank_value = candidate.get("rank", position)
        try:
            rank = int(rank_value)
        except (TypeError, ValueError):
            rank = position
        try:
            result = download_one_video(
                candidate_url(candidate), source_mode="candidate", candidate=candidate,
                candidate_file=path, candidate_rank=rank, config=config, tools=tools,
            )
            if result.get("already_complete"):
                stats["already_complete"] += 1
            else:
                status = str(result.get("overall_status", "failed"))
                stats[status if status in {"success", "partial_success", "failed"} else "failed"] += 1
        except Exception as exc:  # A single corrupt record/download must not stop the batch.
            stats["failed"] += 1
            LOGGER.exception("候选 %s 下载失败，继续下一个: %s", video_id, exc)
    return stats


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
    args = parse_args(argv)
    try:
        candidate_file = args.input or find_latest_candidate_json()
        if not candidate_file.is_absolute():
            candidate_file = PROJECT_ROOT / candidate_file
        if not candidate_file.is_file():
            raise FileNotFoundError(f"候选 JSON 不存在: {candidate_file}")
        config = load_download_config()
        tools = find_local_tools()
        stats = process_candidates(candidate_file, args.video_ids, config=config, tools=tools)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("下载配置、工具或输入错误: %s", exc)
        return 2
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 1 if stats["failed"] or stats["partial_success"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
