from __future__ import annotations

import logging
import os
import site
import sys
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger(__name__)
DLL_HANDLES: list[Any] = []
CRITICAL_DLLS = ("cublas64_12.dll", "cudnn64_9.dll", "cudart64_12.dll")
DLL_RELATIVE_DIRS = (
    ("ctranslate2",),
    ("nvidia", "cublas", "bin"),
    ("nvidia", "cuda_runtime", "bin"),
    ("nvidia", "cudnn", "bin"),
)


class CudaRuntimeError(RuntimeError):
    pass


def python_site_packages() -> list[Path]:
    candidates = [Path(value) for value in site.getsitepackages()]
    user_site = site.getusersitepackages()
    if user_site:
        candidates.append(Path(user_site))
    conventional = Path(sys.prefix) / "Lib" / "site-packages"
    candidates.append(conventional)
    return list(dict.fromkeys(path.resolve() for path in candidates if path.is_dir()))


def discover_cuda_dll_directories(site_packages: list[Path] | None = None) -> list[Path]:
    roots = site_packages if site_packages is not None else python_site_packages()
    found: list[Path] = []
    for root in roots:
        for parts in DLL_RELATIVE_DIRS:
            candidate = root.joinpath(*parts)
            if candidate.is_dir() and candidate not in found:
                found.append(candidate)
    return found


def find_missing_cuda_dlls(directories: list[Path]) -> list[str]:
    present = {path.name.casefold() for directory in directories for path in directory.glob("*.dll")}
    return [name for name in CRITICAL_DLLS if name.casefold() not in present]


def configure_cuda_runtime(
    *,
    platform_name: str | None = None,
    site_packages: list[Path] | None = None,
    require_dlls: bool = True,
) -> dict[str, Any]:
    platform_value = platform_name or sys.platform
    if platform_value != "win32":
        return {"platform": platform_value, "registered_directory_count": 0, "missing_dlls": [], "windows_setup": False}

    directories = discover_cuda_dll_directories(site_packages)
    missing = find_missing_cuda_dlls(directories)
    if require_dlls and missing:
        raise CudaRuntimeError(
            "CUDA_DLL_MISSING: "
            + ", ".join(missing)
            + "。当前 Python 环境缺少完整的 CUDA 12/cuDNN 9 运行库；"
            "源码运行请执行 `python -m pip install -r requirements.lock.txt`，"
            "GPU 便携版请重新完整解压发布 ZIP。"
        )

    current_path = os.environ.get("PATH", "")
    path_parts = current_path.split(os.pathsep) if current_path else []
    registered = 0
    for directory in directories:
        value = str(directory)
        if value not in path_parts:
            path_parts.insert(0, value)
        if hasattr(os, "add_dll_directory"):
            DLL_HANDLES.append(os.add_dll_directory(value))
        registered += 1
    os.environ["PATH"] = os.pathsep.join(path_parts)
    LOGGER.info("已为当前进程注册 %d 个 CUDA DLL 目录。", registered)
    if missing:
        LOGGER.warning("CUDA 关键 DLL 缺失：%s", ", ".join(missing))
    return {
        "platform": platform_value,
        "registered_directory_count": registered,
        "missing_dlls": missing,
        "windows_setup": True,
    }
