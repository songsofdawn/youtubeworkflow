from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from src import download_core
from tests.test_download_stage2 import fake_tools, stage2_config


class FakeResponse:
    def __init__(
        self,
        data: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.data = data
        self.status = status
        self.headers = headers or {}
        self.position = 0
        self.read_calls: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_calls.append(size)
        if self.position >= len(self.data):
            return b""
        if size < 0:
            size = len(self.data) - self.position
        chunk = self.data[self.position:self.position + size]
        self.position += len(chunk)
        return chunk

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class CdnDownloaderTests(TestCase):
    def _config(self, **overrides: object) -> dict:
        config = stage2_config()
        config.update(overrides)
        return config

    def test_stops_at_content_length_without_reading_to_eof(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "audio.part"
            data = b"x" * 100_000
            response = FakeResponse(
                data,
                status=200,
                headers={"Content-Length": str(len(data))},
            )
            with mock.patch("urllib.request.urlopen", return_value=response) as opener:
                ok, error = download_core._stream_cdn_download(
                    ["https://example.test/videoplayback"],
                    destination,
                    {"format_id": "251"},
                    "audio",
                    self._config(cdn_read_chunk_bytes=65_536),
                )
            self.assertTrue(ok, error)
            self.assertEqual(destination.read_bytes(), data)
            self.assertEqual(response.position, len(data))
            self.assertEqual(sum(response.read_calls), len(data))
            self.assertLessEqual(len(response.read_calls), 2)
            opener.assert_called_once()

    def test_valid_206_resumes_from_current_offset(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "audio.part"
            destination.write_bytes(b"0123456789")
            response = FakeResponse(
                b"abcdef",
                status=206,
                headers={
                    "Content-Length": "6",
                    "Content-Range": "bytes 10-15/26",
                },
            )
            with mock.patch("urllib.request.urlopen", return_value=response):
                ok, error = download_core._stream_cdn_download(
                    ["https://example.test/videoplayback"],
                    destination,
                    {"format_id": "251"},
                    "audio",
                    self._config(),
                )
            self.assertTrue(ok, error)
            self.assertEqual(destination.read_bytes(), b"0123456789abcdef")

    def test_content_range_terminates_when_content_length_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "audio.part"
            destination.write_bytes(b"0123456789")
            response = FakeResponse(
                b"abcdef",
                status=206,
                headers={"Content-Range": "bytes 10-15/26"},
            )
            with mock.patch("urllib.request.urlopen", return_value=response):
                ok, error = download_core._stream_cdn_download(
                    ["https://example.test/videoplayback"],
                    destination,
                    {"format_id": "251"},
                    "audio",
                    self._config(),
                )
            self.assertTrue(ok, error)
            self.assertEqual(destination.read_bytes(), b"0123456789abcdef")
            self.assertEqual(sum(response.read_calls), 6)

    def test_200_after_resume_truncates_instead_of_appending(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "audio.part"
            destination.write_bytes(b"0123456789")
            response = FakeResponse(
                b"abcd",
                status=200,
                headers={"Content-Length": "4"},
            )
            with mock.patch("urllib.request.urlopen", return_value=response):
                ok, error = download_core._stream_cdn_download(
                    ["https://example.test/videoplayback"],
                    destination,
                    {"format_id": "251"},
                    "audio",
                    self._config(),
                )
            self.assertTrue(ok, error)
            self.assertEqual(destination.read_bytes(), b"abcd")

    def test_416_is_success_when_local_file_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            destination = Path(name) / "audio.part"
            destination.write_bytes(b"0123456789")
            response = FakeResponse(b"", status=416)
            with mock.patch("urllib.request.urlopen", return_value=response):
                ok, error = download_core._stream_cdn_download(
                    ["https://example.test/videoplayback"],
                    destination,
                    {"format_id": "251", "filesize": 10},
                    "audio",
                    self._config(),
                )
            self.assertTrue(ok, error)
            self.assertEqual(response.read_calls, [])


class MediaPlanTests(TestCase):
    def test_mux_streams_uses_mp4_temp_extension(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            video = root / "video.mp4"
            audio = root / "audio.webm"
            output = root / "source.mp4"
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")

            def run(command, cwd=None, **kwargs):
                temporary = Path(command[-1])
                temporary.write_bytes(b"muxed")
                return {
                    "success": True,
                    "returncode": 0,
                    "stdout": "",
                    "stderr": "",
                    "command": [str(item) for item in command],
                }

            with mock.patch("src.download_core.run_command", side_effect=run) as runner:
                result = download_core._mux_streams(
                    video,
                    audio,
                    output,
                    fake_tools(root),
                    download_core.get_project_paths(root),
                )
            self.assertTrue(result["success"])
            command = [str(item) for item in runner.call_args.args[0]]
            self.assertTrue(command[-1].endswith(".mux.mp4"))

    def test_download_video_media_keeps_video_success_when_audio_fails(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            task = root / "task"
            video_spec = download_core.StreamSpec(
                role="video", format_id="399", ext="mp4", vcodec="av1", acodec="none",
                width=1920, height=1080, abr=None, filesize=100, filesize_approx=None,
                url="https://example.test/video", http_headers={},
            )
            audio_spec = download_core.StreamSpec(
                role="audio", format_id="251", ext="webm", vcodec="none", acodec="opus",
                width=None, height=None, abr=160, filesize=20, filesize_approx=None,
                url="https://example.test/audio", http_headers={},
            )
            plan = download_core.MediaPlan(video=video_spec, audio=audio_spec, source_format_selector="bv*[height<=1080]+ba")

            def ensure(url, spec, streams_dir, tools, paths, config, *, force=False, archive_path=None, use_archive=True):
                streams_dir.mkdir(parents=True, exist_ok=True)
                if spec.role == "video":
                    path = streams_dir / "video_399.mp4"
                    path.write_bytes(b"video")
                    artifact = download_core.StreamArtifact(
                        role="video", original_format_id="399", final_format_id="399",
                        status="success", validated=True, reusable=True, attempts=1,
                        fallback_used=False, backend="yt-dlp", path=path,
                        expected_size=100, actual_size=5, attempt_history=[],
                    )
                    return artifact, [], [], [], spec
                artifact = download_core.StreamArtifact(
                    role="audio", original_format_id="251", final_format_id="251",
                    status="failed", validated=False, reusable=False, attempts=1,
                    fallback_used=True, backend="yt-dlp", path=None,
                    expected_size=20, actual_size=0, attempt_history=[{"stream": "audio", "attempt": 1, "backend": "yt-dlp", "result": "ssl_eof"}],
                )
                return artifact, [], [], ["audio failed"], spec

            probe = {"success": False, "status": "failed", "error": "missing", "command_result": {"command": ["ffprobe"]}}
            with mock.patch("src.download_core._resolve_media_plan", return_value={"success": True, "plan": plan, "command_result": {"command": ["resolve"]}}), mock.patch("src.download_core._ensure_stream_artifact", side_effect=ensure) as ensure_mock, mock.patch("src.download_core._mux_streams") as mux, mock.patch("src.download_core.probe_media", return_value=probe):
                result = download_core.download_video_media(
                    "https://youtu.be/id", task, fake_tools(root),
                    stage2_config(), download_core.get_project_paths(root),
                )
            self.assertFalse(result["success"])
            self.assertEqual(result["video_status"], "success")
            self.assertEqual(result["audio_status"], "failed")
            self.assertEqual(result["core_media_ready"], False)
            self.assertEqual(result["streams"]["video"]["status"], "success")
            self.assertEqual(result["streams"]["audio"]["status"], "failed")
            self.assertEqual(result["mux"]["status"], "skipped")
            self.assertEqual(ensure_mock.call_count, 2)
            mux.assert_not_called()

    def test_stream_artifact_attempt_history_separates_errors(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            streams = root / "streams"; streams.mkdir(parents=True)
            spec = download_core.StreamSpec(
                role="audio", format_id="251", ext="webm", vcodec="none", acodec="opus",
                width=None, height=None, abr=160, filesize=100, filesize_approx=None,
                url="https://example.test/audio", http_headers={},
            )

            def probe(path, role, tools, paths):
                return {"success": path.is_file() and path.stat().st_size > 0, "status": "success" if path.exists() else "failed", "error": "", "data": {}}

            def fallback(spec, target, tools, paths, config):
                target.write_bytes(b"audio")
                return True, ""

            failed = {"success": False, "returncode": 1, "stdout": "EOF occurred in violation of protocol", "stderr": "", "command": []}
            with mock.patch("src.download_core._probe_stream_file", side_effect=probe), mock.patch("src.download_core._download_stream_ytdlp", return_value=failed), mock.patch("src.download_core._resolve_role", return_value=None), mock.patch("src.download_core._fallback_http_stream", side_effect=fallback):
                artifact, _, warnings, errors, _ = download_core._ensure_stream_artifact(
                    "https://youtu.be/id", spec, streams, fake_tools(root),
                    download_core.get_project_paths(root), stage2_config(),
                )
            self.assertEqual(artifact.status, "success")
            self.assertTrue(artifact.fallback_used)
            self.assertEqual(artifact.attempt_history[0]["result"], "ssl_eof")
            self.assertEqual(errors, [])
            self.assertTrue(warnings)
