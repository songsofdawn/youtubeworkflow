from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from .download_core import (
    PROJECT_ROOT,
    _load_json,
    download_one_video,
    find_local_tools,
    load_download_config,
    run_command,
    utc_now,
    write_manifest,
)
from .download_selected_candidates import selected_for_download
from .repair_failed_downloads import recover_candidate


APPROVED_RIGHTS = {"APPROVED", "OWNED", "LICENSED", "PERMISSION_GRANTED"}
SOURCE_FILES = (
    "video/source.mp4", "audio/source_audio.wav",
    "metadata/info.json", "metadata/description.txt", "metadata/thumbnail.jpg",
    *(f"subtitles/{language}.{source}.{extension}"
      for language in ("en", "zh") for source in ("manual", "auto")
      for extension in ("vtt", "srt", "raw.srt")),
)
CLEAN_FILES = ("subtitles/en.clean.srt", "subtitles/zh.youtube.clean.srt")


def redownload_context(
    task_dir: Path, project_root: Path, *, confirm_rights: bool,
) -> dict[str, Any]:
    """Recheck the task identity and rights at enqueue time and execution time."""
    if not confirm_rights:
        raise ValueError("重新下载前必须确认拥有下载和使用这些视频的权利")
    task_dir = task_dir.resolve()
    downloads = (project_root / "downloads").resolve()
    if task_dir == downloads or not task_dir.is_relative_to(downloads):
        raise ValueError("任务目录超出 downloads 范围")
    manifest = _load_json(task_dir / "download_manifest.json")
    if not manifest:
        raise ValueError("任务缺少有效的 download_manifest.json")
    info = _load_json(task_dir / "metadata" / "info.json")
    video_id = str(manifest.get("video_id") or info.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("任务没有有效的 YouTube 视频 ID，无法重新下载")
    candidate = {}
    if (manifest.get("source_mode") == "candidate"
            or task_dir.is_relative_to(downloads / "candidates")):
        candidate, _, error = recover_candidate({
            "manifest": {**manifest, "video_id": video_id}, "task_dir": task_dir,
        })
        if error or not candidate:
            raise ValueError(error or "找不到候选记录")
        if (not selected_for_download(candidate.get("selected"))
                or candidate.get("rights_status") not in APPROVED_RIGHTS):
            raise ValueError("候选必须 selected=1 且 rights_status 已批准，不能通过重新下载绕过授权")
    rights = candidate.get("rights_status") or manifest.get("rights_status")
    if rights not in APPROVED_RIGHTS:
        rights = "PERMISSION_GRANTED"
    return {
        "task_dir": task_dir, "manifest": manifest, "candidate": candidate,
        "video_id": video_id, "url": f"https://www.youtube.com/watch?v={video_id}",
        "rights_status": rights,
    }


def _install_sources(staged: Path, task_dir: Path, manifest: dict[str, Any], attempt: str) -> Path:
    """Keep downstream artifacts intact; back up every replaced source first."""
    backup = task_dir / "download_backups" / attempt
    replacements = [name for name in SOURCE_FILES if (staged / name).is_file()]
    # Stage 3 may already have selected/translated against these exact cue IDs.
    # Only seed cleaned subtitles for tasks that have never generated them.
    replacements.extend(name for name in CLEAN_FILES
                        if (staged / name).is_file() and not (task_dir / name).exists())
    removals = [name for name in SOURCE_FILES if name.startswith("subtitles/")
                and (task_dir / name).is_file() and not (staged / name).is_file()]
    names = [*replacements, *removals, "download_manifest.json"]
    for name in names:
        destination = task_dir / name
        if not destination.resolve().is_relative_to(task_dir):
            raise ValueError("素材路径超出任务目录")
        if destination.exists() and not destination.is_file():
            raise ValueError(f"素材路径不是文件: {name}")
    if not backup.resolve().is_relative_to(task_dir):
        raise ValueError("备份路径超出任务目录")
    backup.mkdir(parents=True, exist_ok=False)
    saved: dict[str, Path] = {}
    for name in names:
        destination = task_dir / name
        if destination.is_file():
            # A saved manifest must not be picked up as another dashboard task.
            saved_path = backup / ("manifest.json" if name == "download_manifest.json" else name)
            saved_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, saved_path)
            saved[name] = saved_path
    installed = []
    try:
        for name in removals:
            (task_dir / name).unlink()
            installed.append(name)
        for name in replacements:
            destination = task_dir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            (staged / name).replace(destination)
            installed.append(name)
        installed.append("download_manifest.json")
        manifest["output_files"] = sorted(name for name in SOURCE_FILES + CLEAN_FILES
                                          if (task_dir / name).is_file())
        write_manifest(task_dir, manifest)
    except OSError:
        for name in reversed(installed):
            if name in saved:
                shutil.copy2(saved[name], task_dir / name)
            else:
                (task_dir / name).unlink(missing_ok=True)
        raise
    return backup


def redownload_task(
    task_dir: Path, *, confirm_rights: bool, project_root: Path = PROJECT_ROOT,
    config: dict[str, Any] | None = None, tools: dict[str, Path] | None = None,
) -> dict[str, Any]:
    context = redownload_context(task_dir, project_root, confirm_rights=confirm_rights)
    task_dir = context["task_dir"]
    config = config if config is not None else load_download_config()
    tools = tools if tools is not None else find_local_tools()
    attempt = uuid.uuid4().hex
    staging_root = project_root / "work" / "redownload" / attempt
    logging.info("重新下载 %s；新素材通过校验前保留原项目", context["video_id"])
    # A fresh, separate directory prevents yt-dlp's existing-file and archive
    # shortcuts from treating a nonempty but damaged MP4/VTT as complete.
    result = download_one_video(
        context["url"], source_mode="manual", output_root=staging_root,
        candidate={"rights_status": context["rights_status"]},
        config={**config, "extract_audio": True, "abort_on_unavailable_fragments": True}, tools=tools,
    )
    if result["overall_status"] != "success":
        logging.error("重新下载未完成，原项目保留。详情: %s", result["manifest_path"])
        return {**result, "task_dir": task_dir, "staging_dir": str(staging_root)}
    staged = Path(result["task_dir"])
    if (not staged.resolve().is_relative_to(staging_root.resolve())
            or result["manifest"].get("video_id") != context["video_id"]):
        raise ValueError("重新下载的视频 ID 或素材目录与原项目不一致")
    for name in ("video/source.mp4", "audio/source_audio.wav", "metadata/info.json"):
        if not (staged / name).is_file() or not (staged / name).stat().st_size:
            raise ValueError(f"重新下载缺少必要素材: {name}")
    logging.info("检查完整视频能否解码")
    check = run_command([
        tools["ffmpeg"], "-nostdin", "-v", "error", "-xerror", "-err_detect", "explode",
        "-i", staged / "video" / "source.mp4", "-map", "0:v:0", "-map", "0:a:0",
        "-f", "null", "-",
    ], project_root)
    if not check["success"]:
        failed_manifest = result["manifest"]
        failed_manifest["overall_status"] = "partial_success"
        failed_manifest["probe_status"] = "failed"
        failed_manifest["errors"].append("完整解码校验失败: " + str(check.get("stderr") or "FFmpeg 解码错误")[-500:])
        failed_manifest["commands_executed"].append(check["command"])
        write_manifest(staged, failed_manifest)
        logging.error("新视频解码校验失败，原项目保留；可稍后重新下载")
        return {"overall_status": "failed", "task_dir": task_dir, "staging_dir": str(staging_root)}
    original = context["manifest"]
    manifest = {**original, **result["manifest"]}
    for field in ("source_mode", "candidate_file", "candidate_rank", "selected"):
        manifest[field] = original.get(field, "manual" if field == "source_mode" else "")
    manifest["rights_status"] = context["rights_status"]
    if context["candidate"]:
        manifest["source_mode"] = "candidate"
        manifest["selected"] = context["candidate"]["selected"]
    # Cleaned/selected/translated subtitles belong to the existing timeline.
    if (task_dir / "subtitles" / "en.clean.srt").is_file():
        manifest["subtitle_clean_status"] = original.get("subtitle_clean_status", "success")
        manifest["subtitle_clean_stats"] = original.get("subtitle_clean_stats", {})
    else:
        def relocate(value: Any) -> Any:
            if isinstance(value, str):
                return value.replace(str(staged), str(task_dir))
            if isinstance(value, dict):
                return {key: relocate(item) for key, item in value.items()}
            return value
        manifest["subtitle_clean_stats"] = relocate(manifest.get("subtitle_clean_stats", {}))
    manifest["redownloaded_at"] = utc_now()
    manifest["redownload_backup"] = f"download_backups/{attempt}"
    manifest["commands_executed"].append(check["command"])
    backup = _install_sources(staged, task_dir, manifest, attempt)
    logging.info("重新下载完成，旧素材备份: %s；可在任务列表继续处理字幕或成片", backup)
    return {"overall_status": "success", "task_dir": task_dir, "backup_dir": str(backup)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Redownload an existing rights-cleared task safely.")
    parser.add_argument("--task-dir", required=True, type=Path)
    parser.add_argument("--confirm-rights", action="store_true")
    parser.add_argument("--cookies-path", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
    try:
        # Validate rights before tool lookup or any network access.
        redownload_context(args.task_dir, PROJECT_ROOT, confirm_rights=args.confirm_rights)
        config = load_download_config()
        if args.cookies_path is not None:
            config["cookies_path"] = str(args.cookies_path.resolve())
        result = redownload_task(args.task_dir, confirm_rights=args.confirm_rights, config=config)
    except (OSError, ValueError) as exc:
        logging.error("重新下载失败: %s", exc)
        return 2
    print(json.dumps({key: str(value) for key, value in result.items()
                      if key in {"overall_status", "task_dir", "backup_dir", "staging_dir"}}, ensure_ascii=False))
    return 0 if result["overall_status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
