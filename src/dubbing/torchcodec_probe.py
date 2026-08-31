from __future__ import annotations

import argparse
import ctypes
import json
import os
import tempfile
import wave
from pathlib import Path
from typing import Any


DLL_LOAD_ORDER = ("avutil-*.dll", "swresample-*.dll", "avcodec-*.dll", "avformat-*.dll")


def _classify(stage: str, exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".casefold()
    if stage == "shared_dll":
        return "FFMPEG_SHARED_DLL_LOAD_FAILED"
    if any(
        marker in text
        for marker in (
            "not compatible",
            "incompatible",
            "undefined symbol",
            "libtorchcodec_core",
            "could not load libtorchcodec",
            "version of pytorch",
        )
    ):
        return "TORCHCODEC_PYTORCH_INCOMPATIBLE"
    if any(marker in text for marker in ("dll load failed", "winerror 126", "winerror 193")):
        return "FFMPEG_SHARED_DLL_LOAD_FAILED"
    return "TORCHCODEC_DEPENDENCY_FAILED"


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _load_shared_dlls(tools_bin: Path) -> None:
    if os.name != "nt":
        return
    add_dll_directory = getattr(os, "add_dll_directory", None)
    handle = add_dll_directory(str(tools_bin)) if callable(add_dll_directory) else None
    try:
        for pattern in DLL_LOAD_ORDER:
            match = next(iter(sorted(tools_bin.glob(pattern))), None)
            if match is None:
                raise FileNotFoundError(f"missing {pattern}")
            ctypes.WinDLL(str(match))
    finally:
        if handle is not None:
            handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tools-bin", required=True, type=Path)
    args = parser.parse_args(argv)
    tools_bin = args.tools_bin.resolve()
    stage = "shared_dll"
    try:
        _load_shared_dlls(tools_bin)
        stage = "imports"
        import torch
        import torchaudio
        import torchcodec

        stage = "encode"
        with tempfile.TemporaryDirectory(prefix="torchcodec-dubbing-") as temporary:
            output = Path(temporary) / "probe.wav"
            samples = torch.zeros((1, 800), dtype=torch.float32)
            torchaudio.save(str(output), samples, 16000)
            if not output.is_file() or output.stat().st_size <= 44:
                raise RuntimeError("torchaudio.save did not create a valid WAV file")
            with wave.open(str(output), "rb") as handle:
                if handle.getnframes() <= 0 or handle.getframerate() <= 0:
                    raise RuntimeError("TorchCodec WAV output contains no audio frames")
    except BaseException as exc:
        _emit(
            {
                "ready": False,
                "code": _classify(stage, exc),
                "stage": stage,
                "error_type": type(exc).__name__,
                "error": str(exc)[-4000:],
            }
        )
        return 2
    _emit(
        {
            "ready": True,
            "torch_version": str(torch.__version__),
            "torchaudio_version": str(torchaudio.__version__),
            "torchcodec_version": str(getattr(torchcodec, "__version__", "")),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
