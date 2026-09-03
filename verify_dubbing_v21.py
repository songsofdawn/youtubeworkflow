from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def fail(message: str) -> None:
    print(f"[失败] {message}")
    raise SystemExit(2)

required = [
    "src/stage3/dubbing_rewrite.py",
    "src/stage3/reference_quality.py",
    "src/stage3/dubbing_script.py",
    "src/stage3/pipeline.py",
    "src/stage3/translator_deepseek.py",
    "src/dubbing/pipeline.py",
    "src/dubbing/speech_timing.py",
    "src/dubbing/config.py",
    "config/stage3_config.json",
    "config/dubbing_config.json",
]
for rel in required:
    if not (ROOT / rel).is_file():
        fail(f"缺少 {rel}")

stage3 = json.loads((ROOT / "config/stage3_config.json").read_text(encoding="utf-8-sig"))
dubbing = json.loads((ROOT / "config/dubbing_config.json").read_text(encoding="utf-8-sig"))
script_cfg = stage3.get("dubbing_script") or {}
timing = dubbing.get("timing") or {}
reference = dubbing.get("reference") or {}
performance = dubbing.get("performance") or {}

checks = {
    "Semantic Boundary Repair": script_cfg.get("semantic_boundary_repair_enabled") is True,
    "soft shift = 500 ms": float(timing.get("soft_alignment_shift_ms", -1)) == 500.0,
    "hard shift = 750 ms": float(timing.get("max_alignment_shift_ms", -1)) == 750.0,
    "Canonical Duration Rewrite": timing.get("duration_rewrite_enabled") is True,
    "duration rewrite passes = 2": int(timing.get("duration_rewrite_max_passes", -1)) == 2,
    "Reference Quality Gate": reference.get("quality_gate_enabled") is True,
    "reference-only fallback": reference.get("quality_fallback_reference_only") is True,
    "Windows stable worker mode": performance.get("keep_voxcpm_warm") is False,
}
for name, ok in checks.items():
    if not ok:
        fail(f"配置检查未通过：{name}")

source_checks = {
    "semantic boundary helper": (ROOT / "src/stage3/dubbing_script.py", "suspicious_boundary_candidates"),
    "semantic AI stage": (ROOT / "src/stage3/pipeline.py", "semantic_boundary_repair"),
    "structured auxiliary LLM": (ROOT / "src/stage3/translator_deepseek.py", "request_json_object"),
    "canonical duration rewrite": (ROOT / "src/stage3/dubbing_rewrite.py", "build_duration_rewrite_messages"),
    "reference quality helper": (ROOT / "src/stage3/reference_quality.py", "build_reference_quality_messages"),
    "duration rewrite loop": (ROOT / "src/dubbing/pipeline.py", "Canonical duration rewrite pass"),
    "reference gate runtime": (ROOT / "src/dubbing/pipeline.py", "Reference Quality Gate"),
    "timing soft/hard limits": (ROOT / "src/dubbing/speech_timing.py", "soft_alignment_shift_limit"),
    "paid API CLI flag": (ROOT / "src/run_dubbing.py", "--allow-paid-api"),
    "paid API panel propagation": (ROOT / "src/control_panel/jobs.py", 'dubbing_command.append("--allow-paid-api")'),
    "paid API worker propagation": (ROOT / "src/dubbing/worker.py", "allow_paid_api"),
    "paid API hard guard": (ROOT / "src/dubbing/pipeline.py", "PAID_API_NOT_ALLOWED"),
}
for name, (path, needle) in source_checks.items():
    if needle not in path.read_text(encoding="utf-8"):
        fail(f"源码检查未通过：{name}")

sys.path.insert(0, str(ROOT))
try:
    from src.dubbing.config import validate_dubbing_config
    validate_dubbing_config(dubbing)
except Exception as exc:
    fail(f"dubbing_config 校验失败：{exc}")

print("[通过] Dubbing V2.1 四项核心改造已正确安装。")
print("  1. Utterance Planner V2 / Semantic Boundary Repair")
print("  2. Timing Scheduler V2 (soft 0.5s / hard 0.75s)")
print("  3. Canonical Duration Rewrite")
print("  4. Reference Quality Gate")
print("  5. Explicit paid API permission propagation")
print("建议继续运行项目本地 pytest 命令做最终回归。")
