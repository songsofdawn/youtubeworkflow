from __future__ import annotations

import json
from pathlib import Path
from typing import Any


RUNTIME_CANDIDATES = (
    Path("runtime/python/python.exe"),
    Path(".venv/Scripts/python.exe"),
    Path(".venv_stage3/Scripts/python.exe"),
)


def resolve_python_executable(
    project_root: Path | str,
    *,
    required: bool = True,
) -> Path | None:
    """Return the app-local Python, with development environments as fallbacks."""
    root = Path(project_root).resolve()
    for relative in RUNTIME_CANDIDATES:
        candidate = (root / relative).resolve()
        if candidate.is_file():
            return candidate
    if required:
        candidates = ", ".join(str(root / path) for path in RUNTIME_CANDIDATES)
        raise FileNotFoundError(f"Python runtime not found. Checked: {candidates}")
    return None


def load_portable_manifest(project_root: Path | str) -> dict[str, Any]:
    path = Path(project_root).resolve() / "portable_manifest.json"
    if not path.is_file():
        return {
            "portable": False,
            "edition": "development",
            "asr_device": "configured",
            "asr_compute_type": "configured",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"portable": True, "edition": "unknown"}
    return payload if isinstance(payload, dict) else {"portable": True, "edition": "unknown"}
