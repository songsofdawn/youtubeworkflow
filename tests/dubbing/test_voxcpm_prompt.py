from __future__ import annotations

import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

from src.dubbing.voxcpm import VoxCPM2Synthesizer


def write_wav(path: Path, duration: float = 0.2, rate: int = 16000) -> None:
    frames = max(1, int(duration * rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)


class FakeModel:
    def __init__(self) -> None:
        self.tts_model = SimpleNamespace(sample_rate=16000)
        self.kwargs = None

    def generate(self, **kwargs):
        self.kwargs = kwargs
        return object()


class FakeSoundFile:
    @staticmethod
    def write(path: str, audio: object, sample_rate: int, subtype: str) -> None:
        del audio, subtype
        write_wav(Path(path), 0.2, sample_rate)


def test_voxcpm_uses_reference_transcript_as_prompt_conditioning() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        reference = root / "reference.wav"
        output = root / "out.wav"
        write_wav(reference)

        synth = VoxCPM2Synthesizer.__new__(VoxCPM2Synthesizer)
        synth._model = FakeModel()
        synth._soundfile = FakeSoundFile()
        synth._reference_prompt_text = "This is the exact original reference speech."
        synth.settings = {
            "cfg_value": 2.0,
            "inference_timesteps": 10,
            "normalize": True,
            "denoise": False,
            "retry_badcase": True,
            "retry_badcase_max_times": 2,
        }
        synth.log = None

        synth.generate("这是中文配音。", reference, output)

        kwargs = synth._model.kwargs
        assert kwargs["reference_wav_path"] == str(reference.resolve())
        assert kwargs["prompt_wav_path"] == str(reference.resolve())
        assert kwargs["prompt_text"] == "This is the exact original reference speech."
        assert output.is_file()
