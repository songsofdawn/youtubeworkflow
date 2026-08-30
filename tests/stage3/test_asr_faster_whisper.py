from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from src.run_stage3 import load_config, parse_args
from src.stage3.asr_faster_whisper import (
    AsrError,
    convert_asr_segments,
    resolve_local_model,
    run_faster_whisper_asr,
    select_audio_source,
)
from src.stage3.subtitle_writer import read_srt


ROOT = Path(__file__).resolve().parents[2]


def config() -> dict:
    return load_config(ROOT / "config" / "stage3_config.json")


def make_model(directory: Path) -> Path:
    model = directory / "model"; model.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.json"):
        (model / name).write_bytes(name.encode())
    return model


def word(text: str, start: float, end: float, probability: float = 0.9):
    return SimpleNamespace(word=text, start=start, end=end, probability=probability)


def segment(identifier: int = 1, *, words=True):
    word_values = [word(" Hello", 0.1, 0.5), word(" world.", 0.5, 1.0)] if words else None
    return SimpleNamespace(
        id=identifier, start=0.1, end=1.0, text=" Hello world.", words=word_values,
        avg_logprob=-0.1, no_speech_prob=0.01, compression_ratio=1.1, temperature=0.0,
    )


class FakeModel:
    def __init__(self, segments=None):
        self.segments = segments or [segment()]
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        info = SimpleNamespace(language="en", language_probability=0.99, duration=10.0)
        return iter(self.segments), info


class FasterWhisperTests(TestCase):
    def test_complete_local_model_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = make_model(Path(directory))
            self.assertEqual(resolve_local_model({"asr_model_path": str(model)}, ROOT), model.resolve())

    def test_incomplete_local_model_has_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model"; model.mkdir(); (model / "config.json").write_text("{}")
            with self.assertRaisesRegex(AsrError, "LOCAL_ASR_MODEL_INCOMPLETE"):
                resolve_local_model({"asr_model_path": str(model)}, ROOT)

    def test_online_model_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(AsrError, "LOCAL_ASR_MODEL_INCOMPLETE"):
            resolve_local_model({"asr_model_path": "large-v3"}, ROOT)

    def test_audio_source_priority_prefers_wav(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "audio").mkdir(); (root / "video").mkdir()
            (root / "audio" / "source_audio.mp3").write_bytes(b"mp3")
            wav = root / "audio" / "source_audio.wav"; wav.write_bytes(b"wav")
            (root / "video" / "source.mp4").write_bytes(b"video")
            self.assertEqual(select_audio_source(root), wav)

    def test_video_is_safe_audio_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "video").mkdir()
            video = root / "video" / "source.mp4"; video.write_bytes(b"video")
            self.assertEqual(select_audio_source(root), video)

    def test_no_audio_writes_no_audio_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = run_faster_whisper_asr(root, config(), ROOT, model_factory=mock.Mock())
            self.assertEqual(result["status"], "NO_AUDIO_SOURCE")
            checkpoint = json.loads((root / "stage3" / "asr" / "asr_checkpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(checkpoint["status"], "NO_AUDIO_SOURCE")

    def test_mock_segments_convert_to_words_and_internal_events(self) -> None:
        raw, words, events = convert_asr_segments([segment()])
        self.assertEqual(raw[0]["text"], "Hello world.")
        self.assertEqual([item["word"] for item in words], ["Hello", "world."])
        self.assertEqual([item.source_cue_id for item in events], [1, 1])

    def test_missing_word_timestamps_falls_back_to_segment_timing(self) -> None:
        raw, words, events = convert_asr_segments([segment(words=False)])
        self.assertEqual(len(events), 2)
        self.assertTrue(all(item["timestamps_approximated"] for item in words))
        self.assertAlmostEqual(events[0].start, 0.1)
        self.assertAlmostEqual(events[-1].end, 1.0)

    def test_mock_run_writes_raw_clean_srt_words_and_qc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "audio").mkdir()
            audio = root / "audio" / "source_audio.wav"; audio.write_bytes(b"unchanged-audio")
            model_path = make_model(root)
            cfg = config(); cfg["asr_model_path"] = str(model_path)
            model = FakeModel()
            result = run_faster_whisper_asr(root, cfg, ROOT, model_factory=lambda *_: (model, {"cuda_device_count": 1}))
            self.assertEqual(result["status"], "ASR_COMPLETED")
            for relative in (
                "stage3/asr/asr_raw_segments.json", "stage3/asr/asr_words.json", "stage3/asr/asr_qc.json",
                "subtitles/en.whisper.raw.srt", "subtitles/en.whisper.clean.srt",
            ):
                self.assertTrue((root / relative).is_file(), relative)
            qc = json.loads((root / "stage3/asr/asr_qc.json").read_text(encoding="utf-8"))
            self.assertEqual(qc["overlaps"], 0)
            self.assertEqual(qc["coverage_basis"], "clean_subtitle_active_duration_over_audio")
            self.assertEqual(qc["coverage_ratio"], qc["subtitle_active_coverage_ratio"])

    def test_source_audio_hash_remains_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "audio").mkdir()
            audio = root / "audio" / "source_audio.wav"; audio.write_bytes(b"immutable")
            before = hashlib.sha256(audio.read_bytes()).hexdigest()
            cfg = config(); cfg["asr_model_path"] = str(make_model(root))
            run_faster_whisper_asr(root, cfg, ROOT, model_factory=lambda *_: (FakeModel(), {}))
            self.assertEqual(hashlib.sha256(audio.read_bytes()).hexdigest(), before)

    def test_max_seconds_is_passed_as_clip_without_editing_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "audio").mkdir()
            audio = root / "audio" / "source_audio.wav"; audio.write_bytes(b"audio")
            cfg = config(); cfg["asr_model_path"] = str(make_model(root)); model = FakeModel()
            run_faster_whisper_asr(root, cfg, ROOT, max_seconds=30, model_factory=lambda *_: (model, {}))
            self.assertEqual(model.calls[0][1]["clip_timestamps"], [0.0, 30.0])
            self.assertEqual(audio.read_bytes(), b"audio")

    def test_successful_checkpoint_skips_second_transcription(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "audio").mkdir(); (root / "audio/source_audio.wav").write_bytes(b"audio")
            cfg = config(); cfg["asr_model_path"] = str(make_model(root)); model = FakeModel()
            factory = mock.Mock(return_value=(model, {}))
            run_faster_whisper_asr(root, cfg, ROOT, model_factory=factory)
            result = run_faster_whisper_asr(root, cfg, ROOT, model_factory=factory)
            self.assertTrue(result["skipped"])
            self.assertEqual(factory.call_count, 1)

    def test_tampered_asr_output_invalidates_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "audio").mkdir()
            (root / "audio/source_audio.wav").write_bytes(b"audio")
            cfg = config()
            cfg["asr_model_path"] = str(make_model(root))
            factory = mock.Mock(return_value=(FakeModel(), {}))
            run_faster_whisper_asr(root, cfg, ROOT, model_factory=factory)
            (root / "subtitles/en.whisper.clean.srt").write_text(
                "tampered", encoding="utf-8"
            )
            result = run_faster_whisper_asr(root, cfg, ROOT, model_factory=factory)
            self.assertFalse(result["skipped"])
            self.assertEqual(factory.call_count, 2)

    def test_force_reruns_successful_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "audio").mkdir(); (root / "audio/source_audio.wav").write_bytes(b"audio")
            cfg = config(); cfg["asr_model_path"] = str(make_model(root)); factory = mock.Mock(return_value=(FakeModel(), {}))
            run_faster_whisper_asr(root, cfg, ROOT, model_factory=factory)
            run_faster_whisper_asr(root, cfg, ROOT, force=True, model_factory=factory)
            self.assertEqual(factory.call_count, 2)

    def test_cli_accepts_asr_limit_and_whisper_override(self) -> None:
        args = parse_args(["--video-dir", "task", "--steps", "asr", "--subtitle-source", "whisper", "--asr-max-seconds", "30"])
        self.assertEqual(args.subtitle_source, "whisper")
        self.assertEqual(args.asr_max_seconds, 30.0)
