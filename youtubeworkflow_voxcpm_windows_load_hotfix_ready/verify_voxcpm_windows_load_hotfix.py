from pathlib import Path

root = Path(__file__).resolve().parent
path = root / "src" / "dubbing" / "voxcpm.py"
text = path.read_text(encoding="utf-8")

checks = {
    "windows_safe_default": 'self.settings.get("optimize", False)' in text,
    "import_log": "Importing VoxCPM2 runtime" in text,
    "load_log": "Loading VoxCPM2 (device=" in text,
}
failed = [name for name, ok in checks.items() if not ok]
if failed:
    print("[失败] VoxCPM2 Windows load hotfix 未正确覆盖：" + ", ".join(failed))
    raise SystemExit(2)

print("[通过] VoxCPM2 Windows load hotfix 已正确安装。")
print("默认将使用 optimize=false（eager CUDA），避免 Windows 下 torch.compile/warm-up 长时间卡住。")
print("请彻底关闭并重新启动控制面板后再试。")
