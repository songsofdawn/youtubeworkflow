from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from src.dubbing.loudness import (
    build_loudnorm_filter,
    measure_loudness,
    normalize_loudness,
    parse_loudnorm_json,
)
from src.dubbing.mixer import (
    build_ducking_filter,
    merge_speech_intervals,
    mix_background,
    write_ducking_envelope,
)
from src.dubbing.speech_timing import (
    schedule_speech_regions,
    trim_wav_silence,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FFMPEG = PROJECT_ROOT / "tools" / "bin" / "ffmpeg.exe"


def write_wav(
    path: Path,
    *,
    duration: float = 2.0,
    rate: int = 48000,
    amplitude: float = 0.1,
    frequency: float = 440.0,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(duration * rate))
    payload = bytearray()
    for index in range(frames):
        sample = round(
            max(-1.0, min(1.0, amplitude * math.sin(2 * math.pi * frequency * index / rate)))
            * 32767
        )
        payload.extend(struct.pack("<hh", sample, sample))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(payload)


def window_rms(path: Path, start: float, end: float) -> float:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        if width != 2:
            raise AssertionError(f"unexpected sample width: {width}")
        handle.setpos(round(start * rate))
        raw = handle.readframes(round((end - start) * rate))
    values = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    first_channel = values[::channels]
    return math.sqrt(sum(value * value for value in first_channel) / len(first_channel))


def write_silence_padded_tone(
    path: Path,
    *,
    leading: float,
    tone: float,
    trailing: float,
    rate: int = 16000,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    values: list[int] = []
    values.extend([0] * round(leading * rate))
    for index in range(round(tone * rate)):
        values.append(round(0.25 * 32767 * math.sin(2 * math.pi * 440 * index / rate)))
    values.extend([0] * round(trailing * rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<" + "h" * len(values), *values))


class SpeechTimingTests(unittest.TestCase):
    def test_tts_edge_silence_is_trimmed_with_safe_padding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "raw.wav"
            output = root / "trimmed.wav"
            write_silence_padded_tone(
                source,
                leading=1.0,
                tone=0.5,
                trailing=0.8,
            )
            result = trim_wav_silence(
                source,
                output,
                threshold_db=-45,
                relative_db=-35,
                padding_ms=40,
            )
            with wave.open(str(output), "rb") as handle:
                duration = handle.getnframes() / handle.getframerate()

        self.assertTrue(result.speech_detected)
        self.assertTrue(result.trimmed)
        self.assertAlmostEqual(result.leading_silence, 0.96, delta=0.03)
        self.assertAlmostEqual(result.trailing_silence, 0.76, delta=0.03)
        self.assertAlmostEqual(duration, 0.58, delta=0.04)

    def test_dense_cues_share_region_budget_without_voice_overlap(self) -> None:
        rows, qc = schedule_speech_regions(
            [
                {"index": 1, "start": 0.0, "end": 0.8, "spoken_duration": 0.72},
                {"index": 2, "start": 0.82, "end": 1.6, "spoken_duration": 0.72},
                {"index": 3, "start": 1.62, "end": 2.4, "spoken_duration": 0.72},
            ],
            media_duration=3.0,
            region_max_gap=0.5,
            internal_gap=0.04,
            boundary_gap=0.05,
            max_stretch_ratio=1.3,
            max_alignment_shift=1.5,
        )

        self.assertEqual(qc["status"], "PASS_AUTO_ADAPTED")
        self.assertTrue(qc["no_voice_overlap"])
        self.assertEqual(qc["region_count"], 1)
        for current, following in zip(rows, rows[1:]):
            current_end = (
                current["scheduled_start"]
                + current["spoken_duration"] / current["schedule_speed_factor"]
            )
            self.assertLessEqual(current_end, following["scheduled_start"])

    def test_alignment_drift_or_unfit_region_requires_fallback(self) -> None:
        rows, qc = schedule_speech_regions(
            [
                {"index": 1, "start": 0.0, "end": 0.3, "spoken_duration": 1.0},
                {"index": 2, "start": 0.31, "end": 0.6, "spoken_duration": 1.0},
            ],
            media_duration=0.8,
            region_max_gap=0.5,
            internal_gap=0.04,
            max_stretch_ratio=1.3,
            max_alignment_shift=0.2,
        )

        self.assertEqual(qc["status"], "REVIEW_REQUIRED")
        self.assertTrue(all(row["schedule_needs_review"] for row in rows))
        self.assertIn("REGION_DURATION_OVERFLOW", rows[0]["schedule_reasons"])


class DuckingFilterTests(unittest.TestCase):
    def test_zero_db_has_no_active_gain_envelope(self) -> None:
        value = build_ducking_filter(
            [{"start": 1.0, "final_duration": 2.0}],
            duck_db=0,
        )
        self.assertNotIn("volume=", value)
        self.assertNotIn("sidechaincompress", value)

    def test_six_db_is_encoded_as_gain_attenuation_not_ratio(self) -> None:
        value = build_ducking_filter(
            [{"start": 1.0, "final_duration": 2.0}],
            duck_db=6,
            attack_ms=40,
            release_ms=250,
        )
        self.assertIn("[2:a]", value)
        self.assertIn("[bg][gain]amultiply[ducked]", value)
        self.assertNotIn("sidechaincompress", value)

    def test_envelope_encodes_attack_hold_and_release_in_db(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "envelope.wav"
            write_ducking_envelope(
                path,
                [{"start": 1.0, "final_duration": 2.0}],
                media_duration=4.0,
                duck_db=6,
                attack_ms=40,
                release_ms=250,
            )
            with wave.open(str(path), "rb") as handle:
                rate = handle.getframerate()
                raw = handle.readframes(handle.getnframes())
            values = struct.unpack("<" + "h" * (len(raw) // 2), raw)
            unity = values[round(0.5 * rate)] / 32767
            attack = values[round(0.98 * rate)] / 32767
            hold = values[round(2.0 * rate)] / 32767
            release = values[round(3.125 * rate)] / 32767
            recovered = values[round(3.5 * rate)] / 32767
            self.assertAlmostEqual(20 * math.log10(unity), 0.0, delta=0.01)
            self.assertAlmostEqual(20 * math.log10(attack), -3.0, delta=0.15)
            self.assertAlmostEqual(20 * math.log10(hold), -6.0, delta=0.05)
            self.assertAlmostEqual(20 * math.log10(release), -3.0, delta=0.15)
            self.assertAlmostEqual(20 * math.log10(recovered), 0.0, delta=0.01)

    def test_nearby_intervals_merge_to_avoid_pumping(self) -> None:
        merged = merge_speech_intervals(
            [
                {"start": 1.0, "final_duration": 1.0},
                {"start": 2.1, "final_duration": 0.8},
                {"start": 5.0, "final_duration": 0.5},
            ],
            attack_ms=40,
            release_ms=250,
        )
        self.assertEqual(merged, [(1.0, 2.9), (5.0, 5.5)])

    def test_large_timeline_is_passed_by_filter_script_not_command_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            background = root / "background.wav"
            voice = root / "voice.wav"
            output = root / "mixed.wav"
            write_wav(background, duration=4)
            write_wav(voice, duration=4, amplitude=0)
            observed: dict[str, object] = {}

            def runner(command: list[object], **_: object) -> None:
                args = [str(item) for item in command]
                observed["command_chars"] = len(subprocess.list2cmdline(args))
                script = Path(args[args.index("-/filter_complex") + 1])
                observed["script_chars"] = len(script.read_text(encoding="utf-8"))
                input_indexes = [index for index, value in enumerate(args) if value == "-i"]
                envelope = Path(args[input_indexes[-1] + 1])
                observed["envelope_bytes"] = envelope.stat().st_size
                write_wav(Path(args[-1]), duration=4)

            mix_background(
                background,
                voice,
                output,
                ffmpeg_path=FFMPEG,
                speech_intervals=[
                    {"start": index * 0.4, "final_duration": 0.05}
                    for index in range(1500)
                ],
                duck_db=6,
                media_duration=700,
                command_runner=runner,
            )
            self.assertLess(int(observed["command_chars"]), 2000)
            self.assertLess(int(observed["script_chars"]), 2000)
            self.assertGreater(int(observed["envelope_bytes"]), 1_000_000)
            self.assertFalse((root / "ducking_filter.txt").exists())
            self.assertFalse((root / "ducking_envelope.wav").exists())


class LoudnessUnitTests(unittest.TestCase):
    SAMPLE = """
    [Parsed_loudnorm_0 @ test]
    {
        "input_i" : "-22.70",
        "input_tp" : "-4.20",
        "input_lra" : "3.10",
        "input_thresh" : "-32.80",
        "output_i" : "-18.10",
        "output_tp" : "-1.90",
        "output_lra" : "3.00",
        "output_thresh" : "-28.20",
        "normalization_type" : "linear",
        "target_offset" : "0.10"
    }
    """

    def test_loudnorm_json_is_parsed(self) -> None:
        result = parse_loudnorm_json(self.SAMPLE)
        self.assertAlmostEqual(result["input_i"], -22.7)
        self.assertAlmostEqual(result["output_tp"], -1.9)

    def test_two_pass_filter_contains_all_measured_parameters(self) -> None:
        measured = parse_loudnorm_json(self.SAMPLE)
        value = build_loudnorm_filter(
            target_lufs=-14,
            true_peak_db=-1,
            lra=11,
            measured=measured,
        )
        for option in (
            "measured_I=-22.700000",
            "measured_LRA=3.100000",
            "measured_TP=-4.200000",
            "measured_thresh=-32.800000",
            "offset=0.100000",
            "linear=true",
        ):
            self.assertIn(option, value)

    def test_normalizer_runs_analysis_then_measured_render(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.wav"
            output = root / "output.wav"
            write_wav(source)
            commands: list[list[str]] = []

            def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                if command[-1].endswith(".wav"):
                    shutil.copy2(source, command[-1])
                return subprocess.CompletedProcess(command, 0, "", self.SAMPLE)

            result = normalize_loudness(
                source,
                output,
                ffmpeg_path=FFMPEG,
                target_lufs=-14,
                true_peak_db=-1,
                capture_runner=runner,
            )
            self.assertEqual(len(commands), 2)
            self.assertNotIn("measured_I", commands[0][commands[0].index("-af") + 1])
            self.assertIn("measured_I", commands[1][commands[1].index("-af") + 1])
            self.assertTrue(output.is_file())
            self.assertEqual(result["mode"], "two_pass_loudnorm")


@unittest.skipUnless(FFMPEG.is_file(), "项目本地 FFmpeg 不可用")
class RealFFmpegAudioSmokeTests(unittest.TestCase):
    def test_exact_six_db_duck_only_inside_speech_window(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            background = root / "background.wav"
            voice = root / "voice.wav"
            output = root / "mixed.wav"
            write_wav(background, duration=6, amplitude=0.12, frequency=440)
            # A real but negligible second tone keeps the voice input non-silent
            # without materially changing the background RMS measurement.
            write_wav(voice, duration=6, amplitude=0.0001, frequency=880)
            mix_background(
                background,
                voice,
                output,
                ffmpeg_path=FFMPEG,
                speech_intervals=[{"start": 2.0, "final_duration": 2.0}],
                duck_db=6,
                attack_ms=40,
                release_ms=250,
                media_duration=6,
            )
            before = window_rms(output, 0.5, 1.5)
            during = window_rms(output, 2.5, 3.5)
            after = window_rms(output, 4.6, 5.6)
            attenuation = 20 * math.log10(during / before)
            recovery = 20 * math.log10(after / before)
            self.assertAlmostEqual(attenuation, -6.0, delta=1.0)
            self.assertAlmostEqual(recovery, 0.0, delta=0.25)

    def test_two_pass_loudnorm_hits_target_and_true_peak_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "quiet.wav"
            output = root / "normalized.wav"
            write_wav(source, duration=8, amplitude=0.015)
            result = normalize_loudness(
                source,
                output,
                ffmpeg_path=FFMPEG,
                target_lufs=-14,
                true_peak_db=-1,
            )
            measured = measure_loudness(
                output,
                ffmpeg_path=FFMPEG,
                target_lufs=-14,
                true_peak_db=-1,
                lra=11,
            )
            self.assertAlmostEqual(float(measured["input_i"]), -14.0, delta=0.6)
            self.assertLessEqual(float(measured["input_tp"]), -0.8)
            self.assertEqual(result["target_true_peak_db"], -1.0)


if __name__ == "__main__":
    unittest.main()


def test_soft_alignment_shift_marks_duration_rewrite_candidate_without_hard_review() -> None:
    rows, qc = schedule_speech_regions(
        [
            {"index": 1, "start": 0.0, "end": 1.0, "spoken_duration": 1.0},
            {"index": 2, "start": 1.02, "end": 2.0, "spoken_duration": 1.5},
            {"index": 3, "start": 2.02, "end": 3.0, "spoken_duration": 0.5},
        ],
        media_duration=4.0,
        region_max_gap=0.5,
        internal_gap=0.04,
        boundary_gap=0.05,
        max_extension=1.0,
        max_stretch_ratio=1.15,
        soft_alignment_shift=0.5,
        max_alignment_shift=0.75,
        duration_rewrite_trigger_ratio=1.15,
        duration_rewrite_target_ratio=1.05,
    )

    assert any(row["duration_rewrite_required"] for row in rows)
    assert qc["duration_rewrite_candidate_count"] >= 1
    assert qc["hard_alignment_shift_limit"] == 0.75
    assert qc["soft_alignment_shift_limit"] == 0.5
