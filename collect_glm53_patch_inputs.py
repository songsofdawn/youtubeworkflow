#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
收集 youtubeworkflow 中生成 GLM-5.3-Flash / 动态 batch 补丁所需的源码。
不会收集 .env、API Key、下载内容、字幕、视频或模型文件。
"""

from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "glm53_patch_inputs.zip"

CANDIDATES = [
    "src/stage3/llm_providers.py",
    "src/stage3/translator_deepseek.py",
    "config/stage3_config.json",
    ".env.example",
    "tests/stage3/test_translator.py",
]

found = []
missing = []

for rel in CANDIDATES:
    path = ROOT / rel
    if path.is_file():
        found.append((rel, path))
    else:
        missing.append(rel)

if not found:
    raise SystemExit(
        "没有找到目标文件。请把本脚本放在 youtubeworkflow 项目根目录再运行。"
    )

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for rel, path in found:
        z.write(path, rel)

print("=" * 64)
print("已生成：")
print(OUT)
print("")
print("已收集：")
for rel, _ in found:
    print("  +", rel)

if missing:
    print("")
    print("本地不存在（没关系）：")
    for rel in missing:
        print("  -", rel)

print("")
print("安全说明：")
print("  - 没有收集 .env")
print("  - 没有收集任何 API Key")
print("  - 没有收集视频、字幕、下载文件或模型")
print("")
print("把 glm53_patch_inputs.zip 上传到 ChatGPT 即可。")
print("=" * 64)
