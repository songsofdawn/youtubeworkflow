from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from .voxcpm import VoxCPM2Synthesizer


def model_compatibility_key(
    model_path: Path | str,
    *,
    device: str,
    allow_cpu: bool,
    settings: dict[str, Any],
) -> tuple[Any, ...]:
    normalized_device = str(device or "cuda").strip().casefold()
    return (
        str(Path(model_path).resolve()).casefold(),
        normalized_device,
        bool(allow_cpu),
        bool(settings.get("denoise", False)),
        normalized_device.startswith("cuda"),
    )


class SynthesizerLease:
    """A task-scoped view whose close does not unload the shared model."""

    def __init__(
        self,
        synthesizer: Any,
        *,
        model_reused: bool,
        model_load_seconds: float,
    ) -> None:
        self._synthesizer = synthesizer
        self.model_reused = bool(model_reused)
        self.model_load_seconds = round(float(model_load_seconds), 3)

    def generate(self, *args: Any, **kwargs: Any) -> Any:
        return self._synthesizer.generate(*args, **kwargs)

    def reset_peak_vram_stats(self) -> None:
        reset = getattr(self._synthesizer, "reset_peak_vram_stats", None)
        if callable(reset):
            reset()

    @property
    def peak_vram_mb(self) -> float | None:
        return getattr(self._synthesizer, "peak_vram_mb", None)

    def close(self) -> None:
        # The persistent worker owns the underlying model lifecycle.
        return None


class WarmVoxCPM2Pool:
    def __init__(
        self,
        *,
        synthesizer_factory: Callable[..., Any] = VoxCPM2Synthesizer,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.synthesizer_factory = synthesizer_factory
        self.log = log
        self._key: tuple[Any, ...] | None = None
        self._synthesizer: Any | None = None

    @property
    def loaded(self) -> bool:
        return self._synthesizer is not None

    @property
    def compatibility_key(self) -> tuple[Any, ...] | None:
        return self._key

    def acquire(
        self,
        model_path: Path | str,
        *,
        device: str = "cuda",
        allow_cpu: bool = False,
        settings: dict[str, Any] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> SynthesizerLease:
        task_settings = dict(settings or {})
        key = model_compatibility_key(
            model_path,
            device=device,
            allow_cpu=allow_cpu,
            settings=task_settings,
        )
        task_log = log or self.log
        if self._synthesizer is not None and self._key == key:
            if task_log:
                task_log("[DUBBING] Reusing loaded VoxCPM2 model.")
                task_log("[DUBBING] Model load skipped.")
            return SynthesizerLease(
                self._synthesizer,
                model_reused=True,
                model_load_seconds=0.0,
            )
        if self._synthesizer is not None:
            self.close(reason="VoxCPM2 model settings changed; reloading.")
        started = time.monotonic()
        synthesizer = self.synthesizer_factory(
            model_path,
            device=device,
            allow_cpu=allow_cpu,
            settings=task_settings,
            log=task_log,
        )
        elapsed = float(
            getattr(synthesizer, "model_load_seconds", time.monotonic() - started)
        )
        self._key = key
        self._synthesizer = synthesizer
        return SynthesizerLease(
            synthesizer,
            model_reused=False,
            model_load_seconds=elapsed,
        )

    def close(self, *, reason: str = "") -> None:
        synthesizer = self._synthesizer
        self._synthesizer = None
        self._key = None
        if synthesizer is None:
            return
        if reason and self.log:
            self.log(f"[DUBBING] {reason}")
        close = getattr(synthesizer, "close", None)
        if callable(close):
            close()
        if self.log:
            self.log("[DUBBING] VoxCPM2 unloaded.")


__all__ = ["SynthesizerLease", "WarmVoxCPM2Pool", "model_compatibility_key"]
