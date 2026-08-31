"""Stage 3 subtitle reconstruction and contextual translation pipeline.

Keep this package initializer lightweight.  Shared modules such as
``subtitle_writer`` are also used by the isolated dubbing runtime, which does
not install the translation and discovery dependency stack.
"""

from typing import Any


__all__ = ["Stage3Pipeline"]


def __getattr__(name: str) -> Any:
    if name == "Stage3Pipeline":
        from .pipeline import Stage3Pipeline

        globals()[name] = Stage3Pipeline
        return Stage3Pipeline
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
