from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.stage4.ffmpeg_runner import (
    audio_copy_supported_for_mp4,
    build_hardsub_command,
    build_softsub_command,
    detect_nvenc,
    escape_filter_path,
    FFmpegRunner,
    resolve_video_encoder,
    select_audio_mode,
    temporary_output_path,
)
from src.stage4.models import Stage4Error


PROBE_AAC = {"audio_streams": [{"codec": "aac"}, {"codec": "aac"}]}


class FFmpegCommandTests(unittest.TestCase):
    def test_softsub_copies_video_and_audio(self) -> None:
        command = build_softsub_command("ffmpeg", "source.mp4", "bilingual.ass", "out.mkv")
        self.assertIn("-c:v", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "copy")

    def test_softsub_preserves_all_audio_and_existing_subtitles(self) -> None:
        command = build_softsub_command("ffmpeg", "source.mp4", "bilingual.ass", "out.mkv")
        self.assertIn("0:a?", command)
        self.assertIn("0:s?", command)
        self.assertIn("1:0", command)

    def test_added_ass_uses_actual_output_subtitle_index(self) -> None:
        command = build_softsub_command(
            "ffmpeg", "source.mp4", "bilingual.ass", "out.mkv", existing_subtitle_count=2
        )
        self.assertIn("-c:s:2", command)
        self.assertIn("-metadata:s:s:2", command)
        self.assertIn("-disposition:s:2", command)

    def test_hardsub_uses_ass_filter_and_requested_encoder(self) -> None:
        command = build_hardsub_command(
            "ffmpeg",
            "source.mp4",
            "bilingual.ass",
            "out.mp4",
            video_encoder="libx264",
            audio_mode="copy",
            render_config={},
        )
        self.assertIn("-vf", command)
        self.assertIn("ass=filename=", command[command.index("-vf") + 1])
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-c:a") + 1], "copy")

    def test_filter_path_escapes_sensitive_characters(self) -> None:
        value = escape_filter_path(Path("C:/folder/name [x]'s.ass"))
        self.assertIn(r"\:", value)
        self.assertIn(r"\[", value)
        self.assertIn(r"\'", value)

    def test_relative_filter_path_is_not_resolved_into_task_title(self) -> None:
        self.assertEqual(escape_filter_path("bilingual.ass"), "bilingual.ass")

    def test_temporary_video_name_stays_short_for_long_task_paths(self) -> None:
        destination = Path("very-long-task") / "final_bilingual_hardsub.mp4"
        temporary = temporary_output_path(destination)
        self.assertEqual(temporary.suffix, ".mp4")
        self.assertLessEqual(len(temporary.name), 24)

    def test_mp4_incompatible_audio_falls_back_to_aac(self) -> None:
        mode, transcoded, warnings = select_audio_mode(
            {"audio_streams": [{"codec": "opus"}]}
        )
        self.assertEqual(mode, "aac")
        self.assertTrue(transcoded)
        self.assertIn("AUDIO_TRANSCODE_REQUIRED", warnings)

    def test_require_audio_copy_blocks_transcode(self) -> None:
        with self.assertRaises(Stage4Error) as caught:
            select_audio_mode(
                {"audio_streams": [{"codec": "opus"}]},
                require_audio_copy=True,
            )
        self.assertEqual(caught.exception.code, "AUDIO_COPY_NOT_SUPPORTED")

    def test_all_aac_audio_tracks_can_be_copied(self) -> None:
        self.assertTrue(audio_copy_supported_for_mp4(PROBE_AAC))

    @mock.patch("src.stage4.ffmpeg_runner.subprocess.run")
    def test_nvenc_detection_performs_real_smoke_command(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.assertTrue(detect_nvenc("ffmpeg"))
        command = run.call_args.args[0]
        self.assertIn("h264_nvenc", command)
        self.assertFalse(run.call_args.kwargs["shell"])

    @mock.patch("src.stage4.ffmpeg_runner.detect_nvenc", return_value=False)
    def test_auto_encoder_falls_back_to_libx264(self, _: mock.Mock) -> None:
        self.assertEqual(resolve_video_encoder("auto", "ffmpeg"), "libx264")

    @mock.patch("src.stage4.ffmpeg_runner.subprocess.Popen")
    def test_runner_uses_argument_list_no_shell_and_records_progress(
        self, popen: mock.Mock
    ) -> None:
        process = popen.return_value
        process.stdout = ["out_time=00:00:01.000000\n", "progress=end\n"]
        process.stderr = ["ffmpeg detail\n"]
        process.wait.return_value = 0
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "ffmpeg.log"
            result = FFmpegRunner(log).run(["ffmpeg", "-hide_banner", "-version"])
            self.assertTrue(result.success)
            self.assertTrue(log.is_file())
        actual = popen.call_args.args[0]
        self.assertIn("-progress", actual)
        self.assertFalse(popen.call_args.kwargs["shell"])
