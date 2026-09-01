from __future__ import annotations

import json
import math
import os
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .demucs import valid_wav


CaptureRunner = Callable[..., subprocess.CompletedProcess[str]]


def _number(value: object) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_loudnorm_json(output: str) -> dict[str, float | None]:
    """Extract the last loudnorm JSON object from FFmpeg diagnostic output."""

    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    for offset, character in enumerate(str(output or "")):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and (
            "input_i" in value or "output_i" in value or "target_offset" in value
        ):
            candidates.append(value)
    if not candidates:
        raise ValueError("FFmpeg loudnorm 没有返回可解析的测量 JSON")
    payload = candidates[-1]
    return {
        str(key): _number(value)
        for key, value in payload.items()
    }


def build_loudnorm_filter(
    *,
    target_lufs: float,
    true_peak_db: float,
    lra: float,
    measured: Mapping[str, float | None] | None = None,
) -> str:
    values = [
        f"I={float(target_lufs):.2f}",
        f"LRA={float(lra):.2f}",
        f"TP={float(true_peak_db):.2f}",
    ]
    if measured is not None:
        required = {
            "input_i": "measured_I",
            "input_lra": "measured_LRA",
            "input_tp": "measured_TP",
            "input_thresh": "measured_thresh",
            "target_offset": "offset",
        }
        missing = [key for key in required if measured.get(key) is None]
        if missing:
            raise ValueError(
                "FFmpeg loudnorm 第一遍测量缺少字段：" + "、".join(missing)
            )
        values.extend(
            f"{option}={float(measured[key]):.6f}"
            for key, option in required.items()
        )
        values.append("linear=true")
    values.append("print_format=json")
    return "loudnorm=" + ":".join(values)


def _default_capture_runner(
    command: Sequence[str],
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _run_capture(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    runner: CaptureRunner,
) -> str:
    completed = runner(command, cwd=cwd, env=env)
    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    returncode = int(getattr(completed, "returncode", 1) or 0)
    output = stdout + "\n" + stderr
    if returncode != 0:
        raise RuntimeError(
            f"FFmpeg loudnorm 执行失败（退出代码 {returncode}）"
            + (f"\n{output[-4000:]}" if output.strip() else "")
        )
    return output


def measure_loudness(
    input_path: Path | str,
    *,
    ffmpeg_path: Path | str,
    target_lufs: float,
    true_peak_db: float,
    lra: float,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    capture_runner: CaptureRunner = _default_capture_runner,
) -> dict[str, float | None]:
    source = Path(input_path).resolve()
    if not valid_wav(source):
        raise RuntimeError(f"响度分析输入不存在或无效：{source}")
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-af",
        build_loudnorm_filter(
            target_lufs=target_lufs,
            true_peak_db=true_peak_db,
            lra=lra,
        ),
        "-f",
        "null",
        "NUL" if os.name == "nt" else "/dev/null",
    ]
    output = _run_capture(
        command,
        cwd=Path(cwd or source.parent).resolve(),
        env=env,
        runner=capture_runner,
    )
    return parse_loudnorm_json(output)


def normalize_loudness(
    input_path: Path | str,
    output_path: Path | str,
    *,
    ffmpeg_path: Path | str,
    target_lufs: float,
    true_peak_db: float,
    lra: float = 11.0,
    sample_rate: int = 48000,
    log: Callable[[str], None] | None = None,
    env: Mapping[str, str] | None = None,
    capture_runner: CaptureRunner = _default_capture_runner,
) -> dict[str, Any]:
    """Run measured two-pass EBU R128 loudness normalization."""

    source = Path(input_path).resolve()
    destination = Path(output_path).resolve()
    if not valid_wav(source):
        raise RuntimeError(f"响度标准化输入不存在或无效：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = max(-70.0, min(float(target_lufs), -5.0))
    peak = max(-9.0, min(float(true_peak_db), 0.0))
    target_lra = max(1.0, min(float(lra), 50.0))
    first = measure_loudness(
        source,
        ffmpeg_path=ffmpeg_path,
        target_lufs=target,
        true_peak_db=peak,
        lra=target_lra,
        cwd=destination.parent,
        env=env,
        capture_runner=capture_runner,
    )
    temporary = destination.with_name(f".{destination.stem}-{os.getpid()}.tmp.wav")
    temporary.unlink(missing_ok=True)
    command = [
        str(ffmpeg_path),
        "-hide_banner",
        "-nostats",
        "-y",
        "-i",
        str(source),
        "-af",
        build_loudnorm_filter(
            target_lufs=target,
            true_peak_db=peak,
            lra=target_lra,
            measured=first,
        ),
        "-c:a",
        "pcm_s16le",
        "-ar",
        str(int(sample_rate)),
        "-ac",
        "2",
        str(temporary),
    ]
    try:
        output = _run_capture(
            command,
            cwd=destination.parent,
            env=env,
            runner=capture_runner,
        )
        second = parse_loudnorm_json(output)
        if not valid_wav(temporary):
            raise RuntimeError("FFmpeg loudnorm 第二遍没有生成有效 WAV")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)

    result = {
        "mode": "two_pass_loudnorm",
        "target_lufs": target,
        "target_lra": target_lra,
        "target_true_peak_db": peak,
        "input_lufs": first.get("input_i"),
        "input_true_peak_db": first.get("input_tp"),
        "input_lra": first.get("input_lra"),
        "output_lufs": second.get("output_i"),
        "true_peak_db": second.get("output_tp"),
        "output_lra": second.get("output_lra"),
    }
    if log:
        input_i = result["input_lufs"]
        output_i = result["output_lufs"]
        output_tp = result["true_peak_db"]
        log(
            "[DUBBING] Loudness result: "
            f"{input_i if input_i is not None else 'n/a'} LUFS -> "
            f"{output_i if output_i is not None else 'n/a'} LUFS, "
            f"true peak {output_tp if output_tp is not None else 'n/a'} dBTP"
        )
    return result


__all__ = [
    "build_loudnorm_filter",
    "measure_loudness",
    "normalize_loudness",
    "parse_loudnorm_json",
]
