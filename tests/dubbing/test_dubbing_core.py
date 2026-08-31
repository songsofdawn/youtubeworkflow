from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from src.dubbing.config import load_dubbing_config, public_dubbing_health
from src.dubbing.mixer import build_timeline_filter
from src.dubbing.pipeline import DubbingError, DubbingPipeline, subtitle_segments
from src.dubbing.timing import calculate_available_end, plan_duration


def write_wav(path: Path, duration: float = 0.5, rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(duration * rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)


class FakeSeparator:
    def __init__(self, **_: object) -> None:
        pass

    def prepare(self, source: Path, work_dir: Path, *, force: bool = False) -> dict[str, object]:
        del source, force
        root = Path(work_dir)
        source_wav = root / "source.wav"
        vocals = root / "vocals.wav"
        background = root / "background.wav"
        for path in (source_wav, vocals, background):
            write_wav(path, 12.0)
        return {
            "source_wav": source_wav,
            "vocals": vocals,
            "background": background,
            "reused": False,
            "checkpoint": {"status": "COMPLETED"},
        }


class FakeSynthesizer:
    def __init__(self, factory: "FakeSynthesizerFactory") -> None:
        self.factory = factory

    def generate(self, text: str, reference: Path, output: Path) -> None:
        del reference
        self.factory.calls.append(text)
        if text == self.factory.fail_once_text and not self.factory.failed:
            self.factory.failed = True
            raise RuntimeError("simulated TTS failure")
        write_wav(Path(output), 0.5)

    def close(self) -> None:
        self.factory.closed += 1


class FakeSynthesizerFactory:
    def __init__(self, fail_once_text: str = "") -> None:
        self.fail_once_text = fail_once_text
        self.failed = False
        self.calls: list[str] = []
        self.closed = 0

    def __call__(self, *_: object, **__: object) -> FakeSynthesizer:
        return FakeSynthesizer(self)


def fake_command_runner(command: list[object], **_: object) -> None:
    destination = Path(str(command[-1]))
    write_wav(destination, 1.0, 48000)


class DubbingCoreTests(unittest.TestCase):
    def test_srt_is_converted_to_ordered_segments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "zh.clean.srt"
            path.write_text(
                "1\n00:00:01,000 --> 00:00:02,250\n第一句。\n\n"
                "8\n00:00:03,000 --> 00:00:04,500\n第二句。\n",
                encoding="utf-8",
            )
            rows = subtitle_segments(path)
        self.assertEqual([row["index"] for row in rows], [1, 2])
        self.assertEqual(rows[0]["text"], "第一句。")
        self.assertAlmostEqual(rows[1]["start"], 3.0)

    def test_duration_planning_uses_next_gap_and_marks_large_overflow(self) -> None:
        available_end = calculate_available_end(
            start=1.0,
            end=3.0,
            next_start=3.4,
            media_duration=20.0,
            min_gap=0.2,
            max_extension=1.0,
        )
        self.assertAlmostEqual(available_end, 3.2)
        adjusted = plan_duration(
            start=1.0,
            end=3.0,
            next_start=3.4,
            media_duration=20.0,
            generated_duration=2.6,
        )
        self.assertEqual(adjusted.reason, "speed_adjusted")
        self.assertFalse(adjusted.needs_review)
        overflow = plan_duration(
            start=1.0,
            end=3.0,
            next_start=3.4,
            media_duration=20.0,
            generated_duration=4.0,
        )
        self.assertTrue(overflow.needs_review)
        self.assertAlmostEqual(overflow.speed_factor, 1.3)

    def test_timeline_filter_uses_absolute_subtitle_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "segments" / "adapted" / "000001.wav"
            second = root / "segments" / "adapted" / "000002.wav"
            write_wav(first)
            write_wav(second)
            value = build_timeline_filter(
                [
                    {"start": 1.25, "final_wav": first},
                    {"start": 9.0, "final_wav": second},
                ],
                work_dir=root,
                media_duration=15.0,
            )
        self.assertIn("adelay=1250:all=1", value)
        self.assertIn("adelay=9000:all=1", value)
        self.assertIn("atrim=duration=15.000000", value)

    def test_config_and_optional_health_do_not_affect_core_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "dubbing_config.json").write_text(
                json.dumps({"enabled": False, "voxcpm_model_path": "models/VoxCPM2"}),
                encoding="utf-8",
            )
            config = load_dubbing_config(root)
            health = public_dubbing_health(root)
        self.assertFalse(config["enabled"])
        self.assertFalse(health["configured"])
        self.assertFalse(health["model_ready"])


class DubbingResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.task = self.root / "downloads" / "task"
        (self.task / "subtitles").mkdir(parents=True)
        (self.task / "video").mkdir(parents=True)
        self.video = self.task / "video" / "source.mp4"
        self.video.write_bytes(b"video")
        self.subtitle = self.task / "subtitles" / "zh.clean.srt"
        self._write_subtitle("第二句。")
        tools = self.root / "tools" / "bin"
        tools.mkdir(parents=True)
        (tools / "ffmpeg.exe").write_bytes(b"tool")
        (tools / "ffprobe.exe").write_bytes(b"tool")
        model = self.root / "models" / "VoxCPM2"
        model.mkdir(parents=True)
        (model / "config.json").write_text("{}", encoding="utf-8")
        (model / "model.safetensors").write_bytes(b"weights")
        self.config = {
            "voxcpm_model_path": "models/VoxCPM2",
            "device": "cuda",
            "minimum_free_gb": 0,
            "reference": {
                "duration_seconds": 6,
                "minimum_seconds": 5,
                "maximum_seconds": 10,
                "skip_intro_seconds": 0,
                "maximum_continuity_gap_seconds": 0.6,
            },
            "timing": {
                "min_gap_ms": 200,
                "max_extension_ms": 1000,
                "direct_accept_ratio": 1.1,
                "max_stretch_ratio": 1.3,
            },
            "mix": {"sample_rate": 48000, "background_duck_db": 6, "limiter": 0.95},
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_subtitle(self, second: str) -> None:
        self.subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:04,000\n第一句。\n\n"
            f"2\n00:00:04,100 --> 00:00:08,200\n{second}\n",
            encoding="utf-8",
        )

    def _pipeline(self, factory: FakeSynthesizerFactory) -> DubbingPipeline:
        return DubbingPipeline(
            self.root,
            self.config,
            synthesizer_factory=factory,
            separator_factory=FakeSeparator,
            command_runner=fake_command_runner,
        )

    @patch("src.dubbing.pipeline.probe_media")
    @patch("src.dubbing.pipeline.resolve_source_video")
    def test_corrupted_manifest_fields_are_ignored_and_rebuilt(
        self,
        resolve_source_video_mock: object,
        probe_media_mock: object,
    ) -> None:
        resolve_source_video_mock.return_value = (self.video, "test", (self.video,))
        probe_media_mock.return_value = {"duration": 12.0, "audio_stream_count": 1}
        work_dir = self.task / "dubbing"
        work_dir.mkdir()
        (work_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "segments": [{"index": "not-a-number"}, None],
                    "warnings": "invalid",
                    "errors": {},
                    "reference": [],
                    "checkpoints": [],
                }
            ),
            encoding="utf-8",
        )

        factory = FakeSynthesizerFactory()
        result = self._pipeline(factory).run(self.task)

        self.assertIn(result.status, {"COMPLETED", "COMPLETED_WITH_REVIEW"})
        self.assertEqual(factory.calls, ["第一句。", "第二句。"])

    @patch("src.dubbing.pipeline.probe_media")
    @patch("src.dubbing.pipeline.resolve_source_video")
    def test_failure_saves_segment_checkpoint_and_resume_only_generates_pending(
        self,
        resolve_source_video_mock: object,
        probe_media_mock: object,
    ) -> None:
        resolve_source_video_mock.return_value = (self.video, "test", (self.video,))
        probe_media_mock.return_value = {
            "duration": 12.0,
            "audio_stream_count": 1,
        }
        first = FakeSynthesizerFactory(fail_once_text="第二句。")
        with self.assertRaisesRegex(DubbingError, "第 2 句"):
            self._pipeline(first).run(self.task)
        manifest = json.loads(
            (self.task / "dubbing" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "FAILED")
        self.assertEqual(manifest["segments"][0]["status"], "done")
        self.assertEqual(manifest["segments"][1]["status"], "failed")

        second = FakeSynthesizerFactory()
        result = self._pipeline(second).run(self.task)
        self.assertIn(result.status, {"COMPLETED", "COMPLETED_WITH_REVIEW"})
        self.assertEqual(second.calls, ["第二句。"])
        self.assertTrue((self.task / "dubbing" / "dubbed_audio.wav").is_file())
        resumed_manifest = json.loads(
            (self.task / "dubbing" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(resumed_manifest["errors"], [])
        self.assertTrue(resumed_manifest["error_history"])

        self._write_subtitle("修改后的第二句。")
        changed = FakeSynthesizerFactory()
        self._pipeline(changed).run(self.task)
        self.assertEqual(changed.calls, ["修改后的第二句。"])


if __name__ == "__main__":
    unittest.main()
