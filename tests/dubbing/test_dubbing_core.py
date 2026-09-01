from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

import src.dubbing.config as dubbing_config
from src.dubbing.config import load_dubbing_config, public_dubbing_health
from src.dubbing.mixer import build_timeline_filter
from src.dubbing.model_pool import WarmVoxCPM2Pool
from src.dubbing.pipeline import DubbingError, DubbingPipeline, subtitle_segments
from src.dubbing.timing import calculate_available_end, plan_duration


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def write_wav(path: Path, duration: float = 0.5, rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(duration * rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)


def write_tone_wav(path: Path, duration: float = 0.5, rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(duration * rate))
    payload = bytearray()
    for index in range(frames):
        sample = round(0.2 * 32767 * math.sin(2 * math.pi * 440 * index / rate))
        payload.extend(int(sample).to_bytes(2, "little", signed=True))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(payload)


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


class FakeToneSynthesizer(FakeSynthesizer):
    def generate(self, text: str, reference: Path, output: Path) -> None:
        del reference
        self.factory.calls.append(text)
        write_tone_wav(Path(output), 0.5)


class FakeToneSynthesizerFactory(FakeSynthesizerFactory):
    def __call__(self, *_: object, **__: object) -> FakeToneSynthesizer:
        return FakeToneSynthesizer(self)


class DurationRetrySynthesizer:
    def __init__(self, factory: "DurationRetrySynthesizerFactory") -> None:
        self.factory = factory

    def generate(self, text: str, reference: Path, output: Path) -> None:
        del reference
        self.factory.calls.append(text)
        count = self.factory.calls_by_text.get(text, 0) + 1
        self.factory.calls_by_text[text] = count
        write_tone_wav(
            Path(output),
            1.0 if count == 1 else self.factory.retry_duration,
        )

    def close(self) -> None:
        self.factory.closed += 1


class DurationRetrySynthesizerFactory(FakeSynthesizerFactory):
    def __init__(self, retry_duration: float = 0.6) -> None:
        super().__init__()
        self.calls_by_text: dict[str, int] = {}
        self.retry_duration = retry_duration

    def __call__(self, *_: object, **__: object) -> DurationRetrySynthesizer:
        return DurationRetrySynthesizer(self)


def fake_command_runner(command: list[object], **_: object) -> None:
    destination = Path(str(command[-1]))
    write_wav(destination, 1.0, 48000)


class DubbingCoreTests(unittest.TestCase):
    def test_isolated_dubbing_import_does_not_load_stage3_or_discovery_pipeline(self) -> None:
        script = (
            "import sys;"
            "import src.dubbing.config;"
            "assert 'src.dubbing.pipeline' not in sys.modules;"
            "import src.dubbing.pipeline;"
            "assert 'src.stage3.pipeline' not in sys.modules;"
            "assert 'src.discovery' not in sys.modules"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_runtime_probe_imports_the_real_dubbing_entrypoint(self) -> None:
        completed = Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "torch_ready": True,
                    "torch_version": "test",
                    "entrypoint_ready": True,
                    "cuda_available": True,
                    "cuda_version": "test",
                    "cuda_device_count": 1,
                }
            ),
            stderr="",
        )
        dubbing_config._RUNTIME_PROBE_CACHE.clear()
        with patch("src.dubbing.config.subprocess.run", return_value=completed) as run:
            result = dubbing_config.probe_dubbing_runtime(
                PROJECT_ROOT / ".venv_dubbing" / "Scripts" / "python.exe",
                PROJECT_ROOT,
            )
        command = run.call_args.args[0]
        self.assertIn("import src.dubbing.pipeline", command[2])
        self.assertIn(repr(str(PROJECT_ROOT)), command[2])
        self.assertTrue(result["entrypoint_ready"])

    def test_health_rejects_a_runtime_whose_entrypoint_cannot_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "dubbing_config.json").write_text(
                json.dumps({"voxcpm_model_path": "models/VoxCPM2"}),
                encoding="utf-8",
            )
            runtime = root / "python.exe"
            runtime.write_bytes(b"python")
            with (
                patch("src.dubbing.config.resolve_dubbing_python", return_value=runtime),
                patch("src.dubbing.config.runtime_package_ready", return_value=True),
                patch("src.dubbing.config.voxcpm_model_ready", return_value=True),
                patch(
                    "src.dubbing.config.probe_dubbing_runtime",
                    return_value={
                        "torch_ready": True,
                        "entrypoint_ready": False,
                        "cuda_available": True,
                        "error": "No module named 'dependency'",
                    },
                ),
            ):
                health = public_dubbing_health(root)
        self.assertFalse(health["configured"])
        self.assertFalse(health["entrypoint_ready"])
        self.assertFalse(health["device_ready"])
        self.assertIn("dependency", health["runtime_error"])

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

    def test_timeline_filter_prefers_safe_scheduled_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audio = root / "segments" / "scheduled" / "000001.wav"
            write_wav(audio)
            value = build_timeline_filter(
                [
                    {
                        "start": 1.0,
                        "scheduled_start": 1.375,
                        "final_wav": audio,
                    }
                ],
                work_dir=root,
                media_duration=3.0,
            )
        self.assertIn("adelay=1375:all=1", value)

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
            runtime_preflight=lambda *_: None,
        )

    def _audio_pipeline(
        self,
        factory: object,
        normalizer_calls: list[tuple[str, float]],
        mix_calls: list[str],
    ) -> DubbingPipeline:
        def normalizer(source: Path, destination: Path, **kwargs: object) -> dict[str, object]:
            shutil.copy2(source, destination)
            target = float(kwargs["target_lufs"])
            normalizer_calls.append((Path(source).name, target))
            return {
                "mode": "two_pass_loudnorm",
                "input_lufs": -24.0,
                "output_lufs": target,
                "true_peak_db": float(kwargs["true_peak_db"]),
            }

        def runner(command: list[object], **_: object) -> None:
            if any("ducking_filter.txt" in str(item) for item in command):
                mix_calls.append("mix")
            fake_command_runner(command)

        return DubbingPipeline(
            self.root,
            self.config,
            synthesizer_factory=factory,
            separator_factory=FakeSeparator,
            command_runner=runner,
            runtime_preflight=lambda *_: None,
            loudness_normalizer=normalizer,
        )

    @patch("src.dubbing.pipeline.probe_media")
    @patch("src.dubbing.pipeline.resolve_source_video")
    def test_regional_timing_pipeline_trims_schedules_and_reuses_tts(
        self,
        resolve_source_video_mock: object,
        probe_media_mock: object,
    ) -> None:
        resolve_source_video_mock.return_value = (self.video, "test", (self.video,))
        probe_media_mock.return_value = {"duration": 12.0, "audio_stream_count": 1}
        self.config["timing"].update(
            regional_scheduling_enabled=True,
            trim_silence_enabled=True,
            silence_threshold_db=-45,
            silence_relative_db=-35,
            silence_padding_ms=40,
            region_max_gap_ms=500,
            region_internal_gap_ms=40,
            region_boundary_gap_ms=50,
            max_alignment_shift_ms=1500,
            overlap_tolerance_ms=20,
        )

        first = FakeToneSynthesizerFactory()
        result = self._pipeline(first).run(self.task)
        manifest = json.loads(
            (self.task / "dubbing" / "manifest.json").read_text(encoding="utf-8")
        )
        second = FakeToneSynthesizerFactory()
        self._pipeline(second).run(self.task)

        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(manifest["timing_qc"]["status"], "PASS_AUTO_ADAPTED")
        self.assertTrue(manifest["timing_qc"]["no_voice_overlap"])
        self.assertTrue(all("scheduled_start" in row for row in manifest["segments"]))
        self.assertTrue(
            (self.task / "dubbing" / "segments" / "trimmed" / "000001.wav").is_file()
        )
        self.assertEqual(second.calls, [])

    @patch("src.dubbing.pipeline.probe_media")
    @patch("src.dubbing.pipeline.resolve_source_video")
    def test_overlong_region_retries_each_segment_once_before_review(
        self,
        resolve_source_video_mock: object,
        probe_media_mock: object,
    ) -> None:
        resolve_source_video_mock.return_value = (self.video, "test", (self.video,))
        probe_media_mock.return_value = {"duration": 12.0, "audio_stream_count": 1}
        self.subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:00,300\n第一句。\n\n"
            "2\n00:00:00,310 --> 00:00:00,600\n第二句。\n\n"
            "3\n00:00:00,610 --> 00:00:00,900\n第三句。\n",
            encoding="utf-8",
        )
        self.config["timing"].update(
            regional_scheduling_enabled=True,
            duration_retry_enabled=True,
            duration_retry_max_times=1,
            trim_silence_enabled=True,
            silence_padding_ms=40,
            region_max_gap_ms=500,
            region_internal_gap_ms=40,
            region_boundary_gap_ms=50,
            max_alignment_shift_ms=1500,
            overlap_tolerance_ms=20,
        )

        first = DurationRetrySynthesizerFactory()
        result = self._pipeline(first).run(self.task)
        manifest_path = self.task / "dubbing" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(
            first.calls,
            [
                "第一句。",
                "第二句。",
                "第三句。",
                "第一句。",
                "第二句。",
                "第三句。",
            ],
        )
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(manifest["timing_qc"]["status"], "PASS_AUTO_ADAPTED")
        self.assertTrue(
            all(
                row["duration_retry"]["selected"]
                and row["duration_retry"]["target_met"]
                for row in manifest["segments"]
            )
        )

        second = DurationRetrySynthesizerFactory()
        self._pipeline(second).run(self.task)
        self.assertEqual(second.calls, [])

    @patch("src.dubbing.pipeline.probe_media")
    @patch("src.dubbing.pipeline.resolve_source_video")
    def test_duration_retry_keeps_review_when_target_still_does_not_fit(
        self,
        resolve_source_video_mock: object,
        probe_media_mock: object,
    ) -> None:
        resolve_source_video_mock.return_value = (self.video, "test", (self.video,))
        probe_media_mock.return_value = {"duration": 12.0, "audio_stream_count": 1}
        self.subtitle.write_text(
            "1\n00:00:00,000 --> 00:00:00,300\n第一句。\n\n"
            "2\n00:00:00,310 --> 00:00:00,600\n第二句。\n\n"
            "3\n00:00:00,610 --> 00:00:00,900\n第三句。\n",
            encoding="utf-8",
        )
        self.config["timing"].update(
            regional_scheduling_enabled=True,
            duration_retry_enabled=True,
            duration_retry_max_times=1,
            trim_silence_enabled=True,
            silence_padding_ms=40,
            region_max_gap_ms=500,
            region_internal_gap_ms=40,
            region_boundary_gap_ms=50,
            max_alignment_shift_ms=1500,
            overlap_tolerance_ms=20,
        )

        factory = DurationRetrySynthesizerFactory(retry_duration=0.9)
        result = self._pipeline(factory).run(self.task)
        manifest = json.loads(
            (self.task / "dubbing" / "manifest.json").read_text(encoding="utf-8")
        )

        self.assertEqual(result.status, "COMPLETED_WITH_REVIEW")
        self.assertEqual(manifest["timing_qc"]["status"], "REVIEW_REQUIRED")
        self.assertTrue(
            all(
                row["duration_retry"]["attempts"] == 1
                and row["duration_retry"]["selected"]
                and not row["duration_retry"]["target_met"]
                for row in manifest["segments"]
            )
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

    @patch("src.dubbing.pipeline.probe_media")
    @patch("src.dubbing.pipeline.resolve_source_video")
    def test_resume_recovers_valid_wav_that_manifest_did_not_record(
        self,
        resolve_source_video_mock: object,
        probe_media_mock: object,
    ) -> None:
        resolve_source_video_mock.return_value = (self.video, "test", (self.video,))
        probe_media_mock.return_value = {"duration": 12.0, "audio_stream_count": 1}
        initial = FakeSynthesizerFactory()
        self._pipeline(initial).run(self.task)
        self.assertEqual(initial.calls, ["第一句。", "第二句。"])

        work_dir = self.task / "dubbing"
        manifest_path = work_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["status"] = "FAILED"
        manifest["segments"] = manifest["segments"][:1]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        (work_dir / "segments" / "metadata" / "000002.json").unlink()

        resumed = FakeSynthesizerFactory()
        self._pipeline(resumed).run(self.task)

        self.assertEqual(resumed.calls, [])
        recovered = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            recovered["segments"][1]["recovered_from"],
            "validated_disk_scan",
        )

    @patch("src.dubbing.pipeline.probe_media")
    @patch("src.dubbing.pipeline.resolve_source_video")
    def test_audio_setting_changes_invalidate_only_downstream_checkpoints(
        self,
        resolve_source_video_mock: object,
        probe_media_mock: object,
    ) -> None:
        resolve_source_video_mock.return_value = (self.video, "test", (self.video,))
        probe_media_mock.return_value = {"duration": 12.0, "audio_stream_count": 1}
        self.config["loudness"] = {
            "enabled": True,
            "voice_target_lufs": -18.0,
            "voice_true_peak_db": -2.0,
            "final_target_lufs": -14.0,
            "final_true_peak_db": -1.0,
            "final_lra": 11.0,
        }
        normalizer_calls: list[tuple[str, float]] = []
        mix_calls: list[str] = []

        initial = FakeSynthesizerFactory()
        self._audio_pipeline(initial, normalizer_calls, mix_calls).run(self.task)
        self.assertEqual([target for _, target in normalizer_calls], [-18.0, -14.0])
        self.assertEqual(len(mix_calls), 1)

        cached = FakeSynthesizerFactory()
        self._audio_pipeline(cached, normalizer_calls, mix_calls).run(self.task)
        self.assertEqual(cached.calls, [])
        self.assertEqual(len(normalizer_calls), 2)
        self.assertEqual(len(mix_calls), 1)

        self.config["loudness"]["final_target_lufs"] = -13.0
        final_changed = FakeSynthesizerFactory()
        self._audio_pipeline(final_changed, normalizer_calls, mix_calls).run(self.task)
        self.assertEqual(final_changed.calls, [])
        self.assertEqual(normalizer_calls[-1], ("mixed_audio.wav", -13.0))
        self.assertEqual(len(mix_calls), 1)

        self.config["mix"]["background_duck_db"] = 8.0
        duck_changed = FakeSynthesizerFactory()
        self._audio_pipeline(duck_changed, normalizer_calls, mix_calls).run(self.task)
        self.assertEqual(duck_changed.calls, [])
        self.assertEqual(len(mix_calls), 2)
        self.assertEqual(normalizer_calls[-1], ("mixed_audio.wav", -13.0))

        self.config["loudness"]["voice_target_lufs"] = -16.0
        voice_changed = FakeSynthesizerFactory()
        self._audio_pipeline(voice_changed, normalizer_calls, mix_calls).run(self.task)
        self.assertEqual(voice_changed.calls, [])
        self.assertEqual(normalizer_calls[-2:], [
            ("chinese_voice.wav", -16.0),
            ("mixed_audio.wav", -13.0),
        ])
        self.assertEqual(len(mix_calls), 3)

        manifest = json.loads(
            (self.task / "dubbing" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertIn("voice_loudness", manifest["checkpoints"])
        self.assertIn("final_loudness", manifest["checkpoints"])
        self.assertEqual(manifest["audio_qc"]["voice"]["output_lufs"], -16.0)
        self.assertEqual(manifest["audio_qc"]["final_mix"]["output_lufs"], -13.0)

    @patch("src.dubbing.pipeline.probe_media")
    @patch("src.dubbing.pipeline.resolve_source_video")
    def test_manifest_records_model_reuse_and_performance_without_losing_segments(
        self,
        resolve_source_video_mock: object,
        probe_media_mock: object,
    ) -> None:
        resolve_source_video_mock.return_value = (self.video, "test", (self.video,))
        probe_media_mock.return_value = {"duration": 12.0, "audio_stream_count": 1}
        factory = FakeSynthesizerFactory()
        pool = WarmVoxCPM2Pool(synthesizer_factory=factory)
        try:
            self._pipeline(pool.acquire).run(self.task)
            first = json.loads(
                (self.task / "dubbing" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(first["performance"]["model_reused"])

            self._pipeline(pool.acquire).run(self.task, force_tts=True)
            second = json.loads(
                (self.task / "dubbing" / "manifest.json").read_text(encoding="utf-8")
            )
        finally:
            pool.close()
        self.assertTrue(second["performance"]["model_reused"])
        self.assertEqual(second["performance"]["tts_segment_count"], 2)
        self.assertEqual(len(second["segments"]), 2)
        self.assertEqual(factory.closed, 1)


if __name__ == "__main__":
    unittest.main()
