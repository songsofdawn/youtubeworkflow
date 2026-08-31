"""Recoverable single-speaker Chinese dubbing pipeline.

Configuration and health checks must remain importable without loading the
GPU pipeline or its optional runtime dependencies.
"""

from typing import Any


__all__ = ["DubbingError", "DubbingPipeline", "DubbingResult"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from .pipeline import DubbingError, DubbingPipeline, DubbingResult

        exports = {
            "DubbingError": DubbingError,
            "DubbingPipeline": DubbingPipeline,
            "DubbingResult": DubbingResult,
        }
        globals().update(exports)
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
