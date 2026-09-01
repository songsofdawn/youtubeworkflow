from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .runtime import build_dubbing_subprocess_env, preflight_dubbing_runtime


DUBBING_RUNTIME_CANDIDATES = (
    Path("runtime/dubbing/python.exe"),
    Path(".venv_dubbing/Scripts/python.exe"),
    Path(".venv/Scripts/python.exe"),
    Path("runtime/python/python.exe"),
)
_RUNTIME_PROBE_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _validate_range(
    section: dict[str, Any],
    key: str,
    minimum: float,
    maximum: float,
) -> None:
    if key not in section:
        return
    try:
        value = float(section[key])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"中文配音配置 {key} 必须是数字") from exc
    if not minimum <= value <= maximum:
        raise ValueError(
            f"中文配音配置 {key} 必须在 {minimum:g}～{maximum:g} 之间"
        )


def validate_dubbing_config(payload: dict[str, Any]) -> None:
    timing = (
        payload.get("timing") if isinstance(payload.get("timing"), dict) else {}
    )
    _validate_range(timing, "min_gap_ms", 0.0, 5000.0)
    _validate_range(timing, "max_extension_ms", 0.0, 10000.0)
    _validate_range(timing, "direct_accept_ratio", 1.0, 3.0)
    _validate_range(timing, "max_stretch_ratio", 1.0, 2.0)
    _validate_range(timing, "duration_retry_max_times", 0.0, 1.0)
    _validate_range(timing, "silence_threshold_db", -90.0, -10.0)
    _validate_range(timing, "silence_relative_db", -90.0, -3.0)
    _validate_range(timing, "silence_padding_ms", 0.0, 500.0)
    _validate_range(timing, "region_max_gap_ms", 0.0, 5000.0)
    _validate_range(timing, "region_internal_gap_ms", 0.0, 1000.0)
    _validate_range(timing, "region_boundary_gap_ms", 0.0, 2000.0)
    _validate_range(timing, "max_alignment_shift_ms", 0.0, 10000.0)
    _validate_range(timing, "overlap_tolerance_ms", 0.0, 250.0)
    mix = payload.get("mix") if isinstance(payload.get("mix"), dict) else {}
    _validate_range(mix, "background_duck_db", 0.0, 18.0)
    _validate_range(mix, "duck_attack_ms", 0.0, 2000.0)
    _validate_range(mix, "duck_release_ms", 0.0, 5000.0)
    loudness = (
        payload.get("loudness") if isinstance(payload.get("loudness"), dict) else {}
    )
    _validate_range(loudness, "voice_target_lufs", -70.0, -5.0)
    _validate_range(loudness, "voice_true_peak_db", -9.0, 0.0)
    _validate_range(loudness, "final_target_lufs", -70.0, -5.0)
    _validate_range(loudness, "final_true_peak_db", -9.0, 0.0)
    _validate_range(loudness, "final_lra", 1.0, 50.0)
    performance = (
        payload.get("performance")
        if isinstance(payload.get("performance"), dict)
        else {}
    )
    _validate_range(performance, "worker_idle_timeout_seconds", 5.0, 300.0)


def load_dubbing_config(project_root: Path | str, path: Path | str | None = None) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path) if path is not None else root / "config" / "dubbing_config.json"
    if not source.is_absolute():
        source = root / source
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"中文配音配置不存在：{source}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"中文配音配置 JSON 无效：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("中文配音配置顶层必须是 JSON 对象")
    validate_dubbing_config(payload)
    payload["_config_path"] = str(source.resolve())
    return payload


def configured_path(project_root: Path | str, value: object) -> Path:
    root = Path(project_root).resolve()
    candidate = Path(str(value or ""))
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def resolve_dubbing_python(
    project_root: Path | str,
    config: dict[str, Any] | None = None,
    *,
    required: bool = True,
) -> Path | None:
    root = Path(project_root).resolve()
    settings = config or load_dubbing_config(root)
    configured = str(settings.get("runtime_python") or "").strip()
    candidates = ([Path(configured)] if configured else []) + list(DUBBING_RUNTIME_CANDIDATES)
    for relative in candidates:
        candidate = relative if relative.is_absolute() else root / relative
        if candidate.is_file():
            return candidate.resolve()
    current = Path(sys.executable).resolve()
    if current.is_file() and root in current.parents:
        return current
    if required:
        checked = "、".join(str(root / item) for item in candidates)
        raise FileNotFoundError(
            "未找到中文配音 Python 运行时。请按 requirements_dubbing.txt 创建 "
            f".venv_dubbing；已检查：{checked}"
        )
    return None


def resolve_model_path(project_root: Path | str, config: dict[str, Any]) -> Path:
    return configured_path(project_root, config.get("voxcpm_model_path") or "models/VoxCPM2")


def voxcpm_model_ready(path: Path | str) -> bool:
    model = Path(path)
    required = ("config.json", "audiovae.pth", "tokenizer.json", "tokenizer_config.json")
    if not model.is_dir() or not all((model / name).is_file() for name in required):
        return False
    return any(model.glob("*.safetensors")) or (model / "pytorch_model.bin").is_file()


def runtime_package_ready(python_path: Path | str | None, package: str) -> bool:
    if python_path is None:
        return False
    executable = Path(python_path).resolve()
    roots = [executable.parent, executable.parent.parent]
    for root in roots:
        site_packages = root / "Lib" / "site-packages"
        if (site_packages / package).is_dir() or (site_packages / f"{package}.py").is_file():
            return True
    return False


def probe_dubbing_runtime(
    python_path: Path | str | None,
    project_root: Path | str | None = None,
) -> dict[str, Any]:
    if python_path is None:
        return {
            "torch_ready": False,
            "entrypoint_ready": False,
            "cuda_available": False,
            "error": "",
        }
    executable = Path(python_path).resolve()
    root = Path(project_root).resolve() if project_root is not None else None
    cache_key = f"{executable}|{root or ''}"
    cached = _RUNTIME_PROBE_CACHE.get(cache_key)
    now = time.monotonic()
    if cached and now - cached[0] < 60:
        return dict(cached[1])
    root_setup = f"sys.path.insert(0,{str(root)!r});" if root is not None else ""
    script = (
        "import json,sys,torch;"
        f"{root_setup}"
        "import src.dubbing.pipeline;"
        "print(json.dumps({'torch_ready':True,'torch_version':torch.__version__,"
        "'entrypoint_ready':True,"
        "'cuda_available':bool(torch.cuda.is_available()),"
        "'cuda_version':str(torch.version.cuda or ''),"
        "'cuda_device_count':int(torch.cuda.device_count())}))"
    )
    try:
        completed = subprocess.run(
            [str(executable), "-c", script],
            cwd=root,
            env=build_dubbing_subprocess_env(root or executable.parent.parent),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=20,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if completed.returncode != 0:
            result = {
                "torch_ready": False,
                "entrypoint_ready": False,
                "cuda_available": False,
                "error": (completed.stderr or completed.stdout or "PyTorch probe failed")[-1000:],
            }
        else:
            payload = json.loads(completed.stdout.strip().splitlines()[-1])
            result = payload if isinstance(payload, dict) else {}
            result.setdefault("error", "")
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError) as exc:
        result = {
            "torch_ready": False,
            "entrypoint_ready": False,
            "cuda_available": False,
            "error": str(exc),
        }
    _RUNTIME_PROBE_CACHE[cache_key] = (now, dict(result))
    return result


def public_dubbing_health(project_root: Path | str) -> dict[str, Any]:
    root = Path(project_root).resolve()
    try:
        config = load_dubbing_config(root)
        runtime = resolve_dubbing_python(root, config, required=False)
        model = resolve_model_path(root, config)
        config_error = ""
    except (OSError, ValueError) as exc:
        config = {}
        runtime = None
        model = root / "models" / "VoxCPM2"
        config_error = str(exc)
    model_ready = voxcpm_model_ready(model)
    demucs_ready = runtime_package_ready(runtime, "demucs")
    voxcpm_ready = runtime_package_ready(runtime, "voxcpm")
    runtime_probe = (
        probe_dubbing_runtime(runtime, root)
        if demucs_ready and voxcpm_ready
        else {
            "torch_ready": False,
            "entrypoint_ready": False,
            "cuda_available": False,
            "error": "",
        }
    )
    codec_probe = (
        preflight_dubbing_runtime(root, runtime, use_cache=True)
        if runtime is not None and demucs_ready and voxcpm_ready
        else {
            "ready": False,
            "code": "",
            "message": "",
            "details": {},
        }
    )
    requested_device = str(config.get("device") or "cuda").strip().casefold()
    device_ready = bool(
        runtime_probe.get("torch_ready")
        and runtime_probe.get("entrypoint_ready")
        and (
            not requested_device.startswith("cuda")
            or runtime_probe.get("cuda_available")
        )
    )
    return {
        "configured": bool(
            runtime
            and demucs_ready
            and voxcpm_ready
            and model_ready
            and device_ready
            and codec_probe.get("ready")
            and not config_error
        ),
        "runtime_ready": runtime is not None,
        "runtime_path": str(runtime or ""),
        "demucs_ready": demucs_ready,
        "voxcpm_ready": voxcpm_ready,
        "model_ready": model_ready,
        "model_path": str(model),
        "device": requested_device,
        "device_ready": device_ready,
        "torch_ready": bool(runtime_probe.get("torch_ready")),
        "entrypoint_ready": bool(runtime_probe.get("entrypoint_ready")),
        "ffmpeg_shared_ready": bool(codec_probe.get("ffmpeg_shared_ready")),
        "torchcodec_ready": bool(codec_probe.get("torchcodec_ready")),
        "preflight_code": str(codec_probe.get("code") or ""),
        "preflight_error": str(codec_probe.get("message") or ""),
        "torch_version": str(runtime_probe.get("torch_version") or ""),
        "cuda_available": bool(runtime_probe.get("cuda_available")),
        "cuda_version": str(runtime_probe.get("cuda_version") or ""),
        "cuda_device_count": int(runtime_probe.get("cuda_device_count") or 0),
        "runtime_error": str(
            runtime_probe.get("error") or codec_probe.get("message") or ""
        ),
        "backend": "voxcpm2",
        "enabled_by_default": bool(config.get("enabled", False)),
        "config_error": config_error,
    }


__all__ = [
    "configured_path",
    "load_dubbing_config",
    "public_dubbing_health",
    "probe_dubbing_runtime",
    "resolve_dubbing_python",
    "resolve_model_path",
    "runtime_package_ready",
    "validate_dubbing_config",
    "voxcpm_model_ready",
]
