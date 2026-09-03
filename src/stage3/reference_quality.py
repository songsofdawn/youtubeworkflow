from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config_adapter import normalize_stage3_config
from .subtitle_writer import atomic_write_json
from .translator_deepseek import DeepSeekTranslator, TranslationError


def _strict_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def build_reference_quality_messages(candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    system = (
        "你是英文语音克隆参考片段的 transcript 质检员。每个候选文本对应一段真实参考音频。"
        "判断该 transcript 是否适合与参考音频一起作为 VoxCPM2 prompt conditioning。"
        "优先选择：语法和语义自然完整、单一连续说话、没有明显自动字幕/ASR错词、没有被切断的短语、"
        "不过度堆叠专有名词或代码符号的候选。明显语义破碎、词序异常、疑似错识别、半句话必须判 unusable。"
        "不要修正文案，只评分。必须覆盖所有 candidate_id，只输出JSON："
        '{"candidates":[{"candidate_id":1,"usable":true,"score":92,"reason":"complete natural sentence"}]}'
    )
    payload = {
        "candidates": [
            {
                "candidate_id": int(row["candidate_id"]),
                "duration_seconds": round(float(row.get("duration") or 0.0), 2),
                "transcript": str(row.get("prompt_text") or ""),
            }
            for row in candidates
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


def run_reference_quality(
    candidates: list[dict[str, Any]],
    *,
    project_root: Path | str,
) -> dict[str, Any]:
    project = Path(project_root).resolve()
    if not candidates:
        return {"status": "NO_CANDIDATES", "results": []}
    raw_config = json.loads(
        (project / "config" / "stage3_config.json").read_text(encoding="utf-8-sig")
    )
    config = normalize_stage3_config(raw_config)
    translator = DeepSeekTranslator(config, project / ".runtime" / "reference_quality")
    payload = translator.request_json_object(
        build_reference_quality_messages(candidates),
        purpose="VoxCPM reference transcript quality gate",
    )
    rows = payload.get("candidates")
    if not isinstance(rows, list):
        raise TranslationError("REFERENCE_QUALITY_INVALID: missing candidates array")
    expected = {int(row["candidate_id"]) for row in candidates}
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, dict) or "candidate_id" not in row:
            continue
        try:
            identifier = int(row["candidate_id"])
            score = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            continue
        usable = _strict_bool(row.get("usable"))
        if identifier not in expected or identifier in seen or usable is None:
            continue
        seen.add(identifier)
        normalized.append(
            {
                "candidate_id": identifier,
                "usable": usable,
                "score": max(0.0, min(score, 100.0)),
                "reason": str(row.get("reason") or "").strip(),
            }
        )
    if seen != expected:
        raise TranslationError(
            f"REFERENCE_QUALITY_INVALID: missing candidate ids {sorted(expected - seen)}"
        )
    normalized.sort(key=lambda row: (-int(bool(row["usable"])), -float(row["score"]), int(row["candidate_id"])))
    return {
        "status": "COMPLETED",
        "results": normalized,
        "usage": translator.usage_report(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score VoxCPM reference transcript candidates")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--request-file", required=True)
    parser.add_argument("--response-file", required=True)
    args = parser.parse_args(argv)
    request = json.loads(Path(args.request_file).read_text(encoding="utf-8-sig"))
    candidates = request.get("candidates") if isinstance(request, dict) else None
    if not isinstance(candidates, list):
        raise SystemExit("request-file must contain {'candidates': [...]} JSON")
    response_path = Path(args.response_file).resolve()
    try:
        result = run_reference_quality(candidates, project_root=args.project_root)
    except Exception as exc:
        atomic_write_json(response_path, {"status": "FAILED", "error": str(exc)})
        raise
    atomic_write_json(response_path, result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
