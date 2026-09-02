#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安全修改项目根目录 .env：
- 保留所有 API Key 原值
- 只切换翻译模型和动态 batch 相关配置
- 修改前自动备份 .env
"""

from pathlib import Path
from datetime import datetime
import shutil

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"

if not ENV.exists():
    if not EXAMPLE.exists():
        raise SystemExit("找不到 .env 和 .env.example，请把脚本放在 youtubeworkflow 根目录运行。")
    shutil.copy2(EXAMPLE, ENV)
    print("[创建] .env（来自 .env.example）")

stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup = ROOT / f".env.backup_before_glm53_{stamp}"
shutil.copy2(ENV, backup)
print(f"[备份] {backup.name}")

updates = {
    "TRANSLATION_PROVIDER": "zhipu",
    "TRANSLATION_MODEL": "glm-5.3-flash",
    "TRANSLATION_BASE_URL": "https://open.bigmodel.cn/api/paas/v4/",
    "TRANSLATION_THINKING": "enabled",
    "TRANSLATION_REASONING_EFFORT": "low",
    "TRANSLATION_BATCH_SIZE": "64",
    "TRANSLATION_DYNAMIC_BATCH": "true",
    "TRANSLATION_BATCH_MIN": "64",
    "TRANSLATION_BATCH_MAX": "96",
    "TRANSLATION_BATCH_TARGET_TOKENS": "4500",
    "TRANSLATION_CONTEXT_BEFORE": "2",
    "TRANSLATION_CONTEXT_AFTER": "2",
    "TRANSLATION_MAX_OUTPUT_TOKENS": "8192",
}

lines = ENV.read_text(encoding="utf-8").splitlines()
seen = set()
result = []

for line in lines:
    stripped = line.strip()
    if stripped and not stripped.startswith("#") and "=" in line:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            result.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
    result.append(line)

missing = [key for key in updates if key not in seen]
if missing:
    result.append("")
    result.append("# GLM-5.3-Flash / 动态字幕 batch")
    for key in missing:
        result.append(f"{key}={updates[key]}")

ENV.write_text("\n".join(result) + "\n", encoding="utf-8")

print("[完成] 已切换到 GLM-5.3-Flash")
print("       thinking=enabled")
print("       reasoning_effort=low")
print("       动态主 batch=64~96")
print("       target prompt tokens=4500")
print("       max output tokens=8192")
print("")
print("请确认 .env 中 ZHIPU_API_KEY= 后面已经有你的智谱 API Key。")
