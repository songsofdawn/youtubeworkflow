from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config_adapter import normalize_stage3_config
from .dubbing_script import canonical_text_from_payload, load_canonical_script, script_text_hash
from .manifest import sha256_file, utc_now
from .models import SubtitleSegment
from .subtitle_writer import atomic_write_json, atomic_write_srt
from .translator_deepseek import DeepSeekTranslator, TranslationError


def build_duration_rewrite_messages(
    requests: list[dict[str, Any]],
    *,
    chinese_chars_per_second: float = 4.5,
) -> list[dict[str, str]]:
    system = (
        "你是中文视频配音稿时长压缩器。每个ID已经是最终字幕和TTS共用的唯一中文脚本。"
        "只允许在同一个ID内部把中文改得更短、更自然、更口语，绝对不得跨ID移动、合并或拆分内容。"
        "必须保留原英文中的核心事实、数字、否定、条件、因果、比较、专名和结论；不得新增事实。"
        "目标是让正常中文朗读尽量落在target_seconds附近；可参考约"
        f"{float(chinese_chars_per_second):.1f}个有效中文字/秒，但自然表达优先。"
        "如果原中文已经足够短，也可以轻微精简，但不能改义。"
        "必须覆盖每个输入ID且只输出JSON："
        '{"segments":[{"id":1,"translation":"更短的唯一中文脚本"}]}'
    )
    payload = {
        "segments": [
            {
                "id": int(row["id"]),
                "source_english": str(row.get("source_text") or ""),
                "current_chinese": str(row.get("zh_text") or ""),
                "target_seconds": round(float(row.get("target_duration") or 0.0), 2),
                "current_spoken_seconds": round(
                    float(row.get("spoken_duration") or 0.0), 2
                ),
                "maximum_suggested_cjk_chars": max(
                    1,
                    round(
                        max(0.2, float(row.get("target_duration") or 0.0))
                        * float(chinese_chars_per_second)
                    ),
                ),
            }
            for row in requests
        ]
    }
    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": "按要求输出 JSON："
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
    ]


def _normalize_rewrites(
    payload: dict[str, Any],
    expected_ids: set[int],
) -> dict[int, str]:
    rows = payload.get("segments")
    if not isinstance(rows, list):
        raise TranslationError("DUBBING_DURATION_REWRITE_INVALID: missing segments array")
    result: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict) or "id" not in row:
            continue
        try:
            identifier = int(row["id"])
        except (TypeError, ValueError):
            continue
        text = str(row.get("translation") or "").strip()
        if identifier in expected_ids and text:
            result[identifier] = text
    if set(result) != expected_ids:
        missing = sorted(expected_ids - set(result))
        raise TranslationError(
            f"DUBBING_DURATION_REWRITE_INVALID: missing rewritten ids {missing}"
        )
    return result


def _write_canonical_subtitle(
    video_dir: Path,
    payload: dict[str, Any],
    *,
    width: int = 20,
    max_lines: int = 2,
) -> Path:
    rows: list[SubtitleSegment] = []
    for item in payload.get("utterances") or []:
        text = str(item.get("zh_text") or "").strip()
        if not text:
            continue
        rows.append(
            SubtitleSegment(
                int(item.get("id") or len(rows) + 1),
                float(item.get("start") or 0.0),
                float(item.get("end") or 0.0),
                text,
                source_segment_ids=list(item.get("source_segment_ids") or []),
            )
        )
    output = video_dir / "subtitles" / "zh.dubbing.srt"
    return atomic_write_srt(
        output,
        rows,
        width=max(1, int(width)),
        max_lines=max(1, int(max_lines)),
    )


def apply_duration_rewrites(
    video_dir: Path | str,
    rewrites: dict[int, str],
    *,
    request_meta: dict[int, dict[str, Any]] | None = None,
    subtitle_width: int = 20,
    subtitle_max_lines: int = 2,
) -> dict[str, Any]:
    root = Path(video_dir).resolve()
    canonical_path = root / "stage3" / "translation" / "canonical_zh.json"
    payload = load_canonical_script(canonical_path)
    rows = list(payload.get("utterances") or [])
    by_id = {int(row.get("id") or 0): row for row in rows}
    changed: list[dict[str, Any]] = []
    for identifier, new_text in sorted(rewrites.items()):
        row = by_id.get(int(identifier))
        if row is None:
            raise TranslationError(
                f"DUBBING_DURATION_REWRITE_INVALID: canonical id {identifier} missing"
            )
        old_text = str(row.get("zh_text") or "").strip()
        value = str(new_text or "").strip()
        if not value:
            raise TranslationError(
                f"DUBBING_DURATION_REWRITE_INVALID: canonical id {identifier} empty"
            )
        if value == old_text:
            continue
        row["zh_text"] = value
        meta = dict((request_meta or {}).get(identifier) or {})
        changed.append(
            {
                "id": identifier,
                "before": old_text,
                "after": value,
                "target_duration": meta.get("target_duration"),
                "spoken_duration_before": meta.get("spoken_duration"),
            }
        )

    canonical_text = canonical_text_from_payload(payload)
    payload["canonical_text_hash"] = script_text_hash(canonical_text)
    history = payload.get("duration_rewrite_history")
    if not isinstance(history, list):
        history = []
    history.append(
        {
            "completed_at": utc_now(),
            "changed_count": len(changed),
            "changes": changed,
        }
    )
    payload["duration_rewrite_history"] = history[-10:]
    payload["duration_rewrite_count"] = int(payload.get("duration_rewrite_count") or 0) + len(changed)
    atomic_write_json(canonical_path, payload)
    subtitle_path = _write_canonical_subtitle(
        root,
        payload,
        width=subtitle_width,
        max_lines=subtitle_max_lines,
    )

    manifest_path = root / "stage3_manifest.json"
    if manifest_path.is_file():
        try:
            stage_manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            stage_manifest = {}
        if isinstance(stage_manifest, dict):
            stage_manifest.update(
                canonical_script_path=str(canonical_path),
                canonical_script_hash=sha256_file(canonical_path),
                dubbing_chinese_path=str(subtitle_path),
                duration_rewrite_status="COMPLETED",
                duration_rewrite_count=int(stage_manifest.get("duration_rewrite_count") or 0)
                + len(changed),
            )
            atomic_write_json(manifest_path, stage_manifest)

    return {
        "status": "COMPLETED",
        "changed_count": len(changed),
        "changes": changed,
        "canonical_path": str(canonical_path),
        "canonical_hash": sha256_file(canonical_path),
        "subtitle_path": str(subtitle_path),
        "subtitle_hash": sha256_file(subtitle_path),
        "canonical_text_hash": payload["canonical_text_hash"],
    }


def run_duration_rewrite(
    video_dir: Path | str,
    requests: list[dict[str, Any]],
    *,
    project_root: Path | str,
) -> dict[str, Any]:
    root = Path(video_dir).resolve()
    project = Path(project_root).resolve()
    if not requests:
        return {"status": "NO_CANDIDATES", "changed_count": 0, "changes": []}
    if (root / "subtitles" / "zh.reviewed.srt").is_file():
        return {
            "status": "HUMAN_REVIEWED_LOCKED",
            "changed_count": 0,
            "changes": [],
        }

    raw_config = json.loads(
        (project / "config" / "stage3_config.json").read_text(encoding="utf-8-sig")
    )
    config = normalize_stage3_config(raw_config)
    translator = DeepSeekTranslator(config, root / "stage3" / "translation")
    expected_ids = {int(row["id"]) for row in requests}
    payload = translator.request_json_object(
        build_duration_rewrite_messages(
            requests,
            chinese_chars_per_second=float(config.get("chinese_chars_per_second", 4.5)),
        ),
        purpose="canonical dubbing duration rewrite",
        response_filename="duration_rewrite_raw.json",
    )
    rewrites = _normalize_rewrites(payload, expected_ids)
    request_meta = {int(row["id"]): dict(row) for row in requests}
    result = apply_duration_rewrites(
        root,
        rewrites,
        request_meta=request_meta,
        subtitle_width=int(config.get("chinese_max_chars_per_line", 20)),
        subtitle_max_lines=int(config.get("max_lines", 2)),
    )
    result["usage"] = translator.usage_report()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rewrite overlong canonical Chinese dubbing lines")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--video-dir", required=True)
    parser.add_argument("--request-file", required=True)
    parser.add_argument("--response-file", required=True)
    args = parser.parse_args(argv)

    request_path = Path(args.request_file).resolve()
    response_path = Path(args.response_file).resolve()
    payload = json.loads(request_path.read_text(encoding="utf-8-sig"))
    requests = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(requests, list):
        raise SystemExit("request-file must contain {'segments': [...]} JSON")
    try:
        result = run_duration_rewrite(
            args.video_dir,
            requests,
            project_root=args.project_root,
        )
    except Exception as exc:
        atomic_write_json(
            response_path,
            {"status": "FAILED", "error": str(exc)},
        )
        raise
    atomic_write_json(response_path, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
