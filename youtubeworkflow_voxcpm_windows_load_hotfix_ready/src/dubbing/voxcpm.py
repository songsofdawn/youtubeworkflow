from __future__ import annotations

import gc
import os
import time
from pathlib import Path
from typing import Any, Callable

from .config import voxcpm_model_ready
from .demucs import valid_wav


class VoxCPM2Error(RuntimeError):
    pass


class VoxCPM2Synthesizer:
    """One-process, one-model VoxCPM2 adapter used for every subtitle segment."""

    def __init__(
        self,
        model_path: Path | str,
        *,
        device: str = "cuda",
        allow_cpu: bool = False,
        settings: dict[str, Any] | None = None,
        log: Callable[[str], None] | None = None,
        model_factory: Callable[..., Any] | None = None,
        soundfile_module: Any | None = None,
    ) -> None:
        self.model_path = Path(model_path).resolve()
        self.device = str(device or "cuda").strip().casefold()
        self.allow_cpu = bool(allow_cpu)
        self.settings = dict(settings or {})
        self.log = log
        self._torch: Any | None = None
        self._soundfile = soundfile_module
        self._model: Any | None = None
        self._reference_prompt_text = ""
        self.model_reused = False
        self.model_load_seconds = 0.0

        if not voxcpm_model_ready(self.model_path):
            raise FileNotFoundError(
                "VoxCPM2 本地模型不存在或不完整："
                f"{self.model_path}。程序不会自动下载大型模型。"
            )
        if self.device == "cpu" and not self.allow_cpu:
            raise VoxCPM2Error(
                "当前配置请求 CPU TTS，但 allow_cpu=false。VoxCPM2 CPU 推理会非常慢；"
                "请明确修改 config/dubbing_config.json 后再运行。"
            )
        try:
            import torch
        except ImportError as exc:
            raise VoxCPM2Error(
                "中文配音运行时缺少 PyTorch；请先安装匹配显卡/CUDA 的 PyTorch，"
                "再安装 requirements_dubbing.txt"
            ) from exc
        self._torch = torch
        if self.device.startswith("cuda") and not torch.cuda.is_available():
            raise VoxCPM2Error(
                "VoxCPM2 配置为 CUDA，但当前中文配音运行时检测不到可用 CUDA。"
                "请检查 NVIDIA 驱动、PyTorch CUDA 版本，或明确启用 CPU 模式（会非常慢）。"
            )
        if self.device == "cpu" and self.log:
            self.log("[DUBBING] WARNING: VoxCPM2 is running on CPU; TTS will be very slow.")

        if self._soundfile is None:
            try:
                import soundfile
            except ImportError as exc:
                raise VoxCPM2Error(
                    "中文配音运行时缺少 soundfile；请安装 requirements_dubbing.txt"
                ) from exc
            self._soundfile = soundfile
        if model_factory is None:
            if self.log:
                self.log("[DUBBING] Importing VoxCPM2 runtime...")
            try:
                from voxcpm import VoxCPM
            except ImportError as exc:
                raise VoxCPM2Error(
                    "VoxCPM2 未安装在中文配音运行时中；请安装 requirements_dubbing.txt"
                ) from exc
            model_factory = VoxCPM.from_pretrained

        # VoxCPM2's optimize=True enables torch.compile/warm-up.  On Windows
        # this can take a very long time or appear to hang even though eager CUDA
        # loading works normally.  Keep eager mode as the production default and
        # let advanced users explicitly opt in through tts.optimize=true.
        optimize = bool(self.settings.get("optimize", False)) and self.device.startswith("cuda")
        if self.log:
            self.log(
                f"[DUBBING] Loading VoxCPM2 (device={self.device}, optimize={str(optimize).lower()})..."
            )
        load_kwargs = {
            "load_denoiser": bool(self.settings.get("denoise", False)),
            "optimize": optimize,
            "device": self.device,
            "local_files_only": True,
        }
        load_started = time.monotonic()
        try:
            self._model = model_factory(str(self.model_path), **load_kwargs)
        except TypeError as exc:
            # Older local VoxCPM packages do not expose local_files_only. The
            # path was verified above, so removing it cannot trigger a Hub ID download.
            if "local_files_only" not in str(exc):
                raise VoxCPM2Error(f"VoxCPM2 模型加载失败：{exc}") from exc
            load_kwargs.pop("local_files_only", None)
            try:
                self._model = model_factory(str(self.model_path), **load_kwargs)
            except Exception as retry_exc:
                raise VoxCPM2Error(f"VoxCPM2 模型加载失败：{retry_exc}") from retry_exc
        except Exception as exc:
            raise VoxCPM2Error(f"VoxCPM2 模型加载失败：{exc}") from exc
        self.model_load_seconds = round(time.monotonic() - load_started, 3)
        if self.log:
            self.log(
                f"[DUBBING] VoxCPM2 loaded in {self.model_load_seconds:.1f}s."
            )

    def reset_peak_vram_stats(self) -> None:
        if self._torch is None or not self.device.startswith("cuda"):
            return
        try:
            self._torch.cuda.reset_peak_memory_stats()
        except Exception:
            pass

    @property
    def peak_vram_mb(self) -> float | None:
        if self._torch is None or not self.device.startswith("cuda"):
            return None
        try:
            return round(float(self._torch.cuda.max_memory_allocated()) / 1024**2, 1)
        except Exception:
            return None

    @property
    def sample_rate(self) -> int:
        tts_model = getattr(self._model, "tts_model", None)
        return int(getattr(tts_model, "sample_rate", 48000) or 48000)

    def set_reference_prompt_text(self, text: str) -> None:
        """Attach the exact transcript of reference.wav for VoxCPM2 prompting.

        VoxCPM2 can condition on both a prompt waveform + its transcript and a
        reference waveform.  The dubbing pipeline keeps this task-scoped so a
        warm model can be reused without leaking the previous video's prompt.
        """

        self._reference_prompt_text = str(text or "").strip()

    def generate(
        self,
        text: str,
        reference_audio: Path | str,
        output_path: Path | str,
    ) -> Path:
        value = str(text or "").strip()
        if not value:
            raise VoxCPM2Error("VoxCPM2 收到空白字幕文本")
        reference = Path(reference_audio).resolve()
        if not valid_wav(reference):
            raise VoxCPM2Error(f"reference.wav 不存在或无效：{reference}")
        destination = Path(output_path).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.stem}-{os.getpid()}.tmp.wav")
        temporary.unlink(missing_ok=True)
        kwargs = {
            "text": value,
            "reference_wav_path": str(reference),
            "cfg_value": float(self.settings.get("cfg_value", 2.0)),
            "inference_timesteps": int(self.settings.get("inference_timesteps", 10)),
            "normalize": bool(self.settings.get("normalize", True)),
            "denoise": bool(self.settings.get("denoise", False)),
            "retry_badcase": bool(self.settings.get("retry_badcase", True)),
            "retry_badcase_max_times": int(
                self.settings.get("retry_badcase_max_times", 2)
            ),
        }
        if self._reference_prompt_text:
            # Use the same clean original-speaker clip as both prompt and
            # reference.  prompt_text supplies linguistic/prosodic alignment;
            # reference_wav_path keeps the speaker identity conditioning.
            kwargs["prompt_wav_path"] = str(reference)
            kwargs["prompt_text"] = self._reference_prompt_text
        try:
            try:
                audio = self._model.generate(**kwargs)
            except TypeError as exc:
                # Compatibility fallback for older local VoxCPM packages.
                # We only downgrade when the error is clearly about prompt args.
                message = str(exc)
                prompt_kw_error = (
                    "prompt_wav_path" in message
                    or "prompt_text" in message
                    or "unexpected keyword" in message.casefold()
                )
                if not self._reference_prompt_text or not prompt_kw_error:
                    raise
                kwargs.pop("prompt_wav_path", None)
                kwargs.pop("prompt_text", None)
                if self.log:
                    self.log(
                        "[DUBBING] WARNING: 当前 VoxCPM 不支持 prompt transcript；"
                        "已回退到 reference-only 音色克隆。"
                    )
                audio = self._model.generate(**kwargs)
            self._soundfile.write(
                str(temporary),
                audio,
                self.sample_rate,
                subtype="PCM_16",
            )
            if not valid_wav(temporary):
                raise VoxCPM2Error("VoxCPM2 返回了空音频或无效 WAV")
            os.replace(temporary, destination)
        except VoxCPM2Error:
            raise
        except Exception as exc:
            raise VoxCPM2Error(f"VoxCPM2 生成失败：{exc}") from exc
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def close(self) -> None:
        self._model = None
        gc.collect()
        if self._torch is not None and self.device.startswith("cuda"):
            try:
                self._torch.cuda.empty_cache()
            except Exception:
                pass

    def __enter__(self) -> "VoxCPM2Synthesizer":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["VoxCPM2Error", "VoxCPM2Synthesizer"]
