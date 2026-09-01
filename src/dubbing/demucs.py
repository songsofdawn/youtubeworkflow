from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..stage4.stage4_manifest import atomic_write_json, sha256_file, utc_now


LogCallback = Callable[[str], None]


def valid_wav(path: Path | str) -> bool:
    source = Path(path)
    if not source.is_file() or source.stat().st_size <= 44:
        return False
    try:
        with wave.open(str(source), "rb") as handle:
            return handle.getnframes() > 0 and handle.getframerate() > 0
    except (OSError, EOFError, wave.Error):
        return False


def run_checked(
    command: Iterable[Path | str],
    *,
    cwd: Path | str | None = None,
    log: LogCallback | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    args = [str(item) for item in command]
    try:
        process = subprocess.Popen(
            args,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=dict(env) if env is not None else None,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise RuntimeError(f"无法启动命令：{args[0]}（{exc}）") from exc
    tail: list[str] = []
    assert process.stdout is not None
    try:
        for line in process.stdout:
            text = line.rstrip()
            if text and log:
                log(text)
            tail.append(text)
            if len(tail) > 80:
                tail.pop(0)
    finally:
        process.stdout.close()
    returncode = process.wait()
    if returncode != 0:
        details = "\n".join(item for item in tail if item)[-4000:]
        raise RuntimeError(
            f"命令执行失败（退出代码 {returncode}）：{' '.join(args[:4])}"
            + (f"\n{details}" if details else "")
        )


def _checkpoint(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class DemucsSeparator:
    def __init__(
        self,
        *,
        ffmpeg_path: Path | str,
        python_executable: Path | str | None = None,
        config: dict[str, Any] | None = None,
        log: LogCallback | None = None,
        command_runner: Callable[..., None] = run_checked,
        subprocess_env: Mapping[str, str] | None = None,
    ) -> None:
        self.ffmpeg_path = Path(ffmpeg_path)
        self.python_executable = Path(python_executable or sys.executable)
        self.config = dict(config or {})
        self.log = log
        self.command_runner = command_runner
        self.subprocess_env = dict(subprocess_env) if subprocess_env is not None else None

    def _run(self, command: list[Path | str], *, cwd: Path | None = None) -> None:
        self.command_runner(
            command,
            cwd=cwd,
            log=self.log,
            env=self.subprocess_env,
        )

    def _extract_source(self, source_video: Path, destination: Path) -> None:
        temporary = destination.with_name(f".{destination.stem}-{os.getpid()}.tmp.wav")
        temporary.unlink(missing_ok=True)
        try:
            self._run(
                [
                    self.ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    source_video,
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-c:a",
                    "pcm_s16le",
                    "-ar",
                    str(int(self.config.get("sample_rate") or 48000)),
                    "-ac",
                    str(int(self.config.get("channels") or 2)),
                    temporary,
                ]
            )
            if not valid_wav(temporary):
                raise RuntimeError("FFmpeg 没有生成有效的 source.wav；原视频可能没有音轨")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _normalize_stem(self, source: Path, destination: Path) -> None:
        temporary = destination.with_name(f".{destination.stem}-{os.getpid()}.tmp.wav")
        temporary.unlink(missing_ok=True)
        try:
            self._run(
                [
                    self.ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    source,
                    "-c:a",
                    "pcm_s16le",
                    "-ar",
                    str(int(self.config.get("sample_rate") or 48000)),
                    "-ac",
                    str(int(self.config.get("channels") or 2)),
                    temporary,
                ]
            )
            if not valid_wav(temporary):
                raise RuntimeError(f"Demucs 输出无法转换为有效 WAV：{source}")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def prepare(
        self,
        source_video: Path | str,
        work_dir: Path | str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        source = Path(source_video).resolve()
        root = Path(work_dir).resolve()
        root.mkdir(parents=True, exist_ok=True)
        if not self.ffmpeg_path.is_file():
            raise FileNotFoundError(f"FFmpeg 不存在：{self.ffmpeg_path}")
        if not self.python_executable.is_file():
            raise FileNotFoundError(f"中文配音 Python 运行时不存在：{self.python_executable}")
        if not source.is_file():
            raise FileNotFoundError(f"原视频不存在：{source}")

        source_wav = root / "source.wav"
        vocals = root / "vocals.wav"
        background = root / "background.wav"
        checkpoint_path = root / "separation_checkpoint.json"
        model = str(self.config.get("model") or "htdemucs")
        source_hash = sha256_file(source)
        expected = {
            "version": 1,
            "source_video_hash": source_hash,
            "demucs_model": model,
            "sample_rate": int(self.config.get("sample_rate") or 48000),
            "channels": int(self.config.get("channels") or 2),
        }
        previous = _checkpoint(checkpoint_path)
        reusable = (
            not force
            and all(previous.get(key) == value for key, value in expected.items())
            and valid_wav(source_wav)
            and valid_wav(vocals)
            and valid_wav(background)
            and previous.get("vocals_hash") == sha256_file(vocals)
            and previous.get("background_hash") == sha256_file(background)
        )
        if reusable:
            return {
                "source_wav": source_wav,
                "vocals": vocals,
                "background": background,
                "reused": True,
                "checkpoint": previous,
            }

        source_reusable = (
            not force
            and valid_wav(source_wav)
            and previous.get("source_video_hash") == source_hash
            and previous.get("source_wav_hash") == sha256_file(source_wav)
        )
        if not source_reusable:
            if self.log:
                self.log("[DUBBING] Preparing source audio...")
            self._extract_source(source, source_wav)
        if self.log:
            self.log("[DUBBING] Running Demucs...")
        with tempfile.TemporaryDirectory(prefix="demucs-", dir=root) as temporary_name:
            output_root = Path(temporary_name)
            try:
                self._run(
                    [
                        self.python_executable,
                        "-m",
                        "demucs.separate",
                        "--two-stems=vocals",
                        "-n",
                        model,
                        "-o",
                        output_root,
                        source_wav,
                    ],
                    cwd=root,
                )
            except RuntimeError as exc:
                message = str(exc)
                if "No module named" in message and "demucs" in message:
                    raise RuntimeError(
                        "Demucs 未安装在中文配音运行时中；请安装 requirements_dubbing.txt"
                    ) from exc
                raise RuntimeError(f"Demucs 执行失败：{exc}") from exc
            vocal_candidates = list(output_root.rglob("vocals.wav"))
            background_candidates = list(output_root.rglob("no_vocals.wav"))
            if len(vocal_candidates) != 1 or len(background_candidates) != 1:
                raise RuntimeError(
                    "Demucs 执行结束但未找到唯一的 vocals.wav / no_vocals.wav 输出"
                )
            self._normalize_stem(vocal_candidates[0], vocals)
            self._normalize_stem(background_candidates[0], background)

        checkpoint = {
            **expected,
            "status": "COMPLETED",
            "source_video_path": str(source),
            "source_wav": "source.wav",
            "source_wav_hash": sha256_file(source_wav),
            "vocals": "vocals.wav",
            "vocals_hash": sha256_file(vocals),
            "background": "background.wav",
            "background_hash": sha256_file(background),
            "completed_at": utc_now(),
        }
        atomic_write_json(checkpoint_path, checkpoint)
        if self.log:
            self.log("[DUBBING] Separation completed.")
        return {
            "source_wav": source_wav,
            "vocals": vocals,
            "background": background,
            "reused": False,
            "checkpoint": checkpoint,
        }


__all__ = ["DemucsSeparator", "run_checked", "valid_wav"]
