from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from src import download_core
from src.download_core import (
    MANIFEST_FIELDS,
    download_subtitles,
    extract_audio,
    find_local_tools,
    get_project_paths,
    probe_media,
    run_command,
    sanitize_windows_filename,
    write_manifest,
)
from src.download_selected_candidates import find_latest_candidate_json, process_candidates, selected_for_download
from src.download_video import main as manual_main


ROOT = Path(__file__).resolve().parents[1]


def stage2_config() -> dict:
    return json.loads((ROOT / "config" / "download_config.json").read_text(encoding="utf-8"))


def fake_tools(root: Path) -> dict[str, Path]:
    return {name: root / "tools" / "bin" / f"{name}.exe" for name in ("yt-dlp", "ffmpeg", "ffprobe")}


class CandidateInputTests(TestCase):
    def test_find_latest_candidate_json(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            older = directory / "2026-07-20_US_localization_top50.json"
            newer = directory / "2026-07-21_US_localization_top50.json"
            older.write_text("[]", encoding="utf-8"); newer.write_text("[]", encoding="utf-8")
            self.assertEqual(find_latest_candidate_json(directory), newer)

    def test_selected_zero_is_skipped(self) -> None:
        stats = self._run([{"video_id": "a", "selected": 0, "rights_status": "APPROVED"}])
        self.assertEqual(stats["skipped_unselected"], 1)

    def test_pending_is_skipped(self) -> None:
        stats = self._run([{"video_id": "a", "selected": 1, "rights_status": "PENDING"}])
        self.assertEqual(stats["skipped_rights"], 1)

    def test_approved_enters_queue_and_source_is_unchanged(self) -> None:
        row = {"video_id": "a", "rank": 2, "selected": "1", "rights_status": "APPROVED"}
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "2026-07-21_US_localization_top50.json"
            path.write_text(json.dumps({"candidates": [row]}), encoding="utf-8")
            before = path.read_bytes()
            with mock.patch("src.download_selected_candidates.download_one_video", return_value={"overall_status": "success", "already_complete": False}) as downloader:
                stats = process_candidates(path, config=stage2_config(), tools=fake_tools(ROOT))
            self.assertEqual(stats["approved"], 1); self.assertEqual(stats["success"], 1)
            self.assertEqual(path.read_bytes(), before); downloader.assert_called_once()

    def test_video_id_filter_does_not_bypass_rights(self) -> None:
        rows = [{"video_id": "a", "selected": 1, "rights_status": "REJECTED"}, {"video_id": "b", "selected": 1, "rights_status": "APPROVED"}]
        stats = self._run(rows, ["a"])
        self.assertEqual(stats["total"], 1); self.assertEqual(stats["skipped_rights"], 1); self.assertEqual(stats["approved"], 0)

    def test_single_failure_continues_to_next(self) -> None:
        rows = [{"video_id": "a", "selected": 1, "rights_status": "APPROVED"}, {"video_id": "b", "selected": True, "rights_status": "OWNED"}]
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "2026-07-21_US_localization_top50.json"; path.write_text(json.dumps(rows), encoding="utf-8")
            with mock.patch("src.download_selected_candidates.download_one_video", side_effect=[RuntimeError("one failed"), {"overall_status": "success", "already_complete": False}]) as downloader:
                stats = process_candidates(path, config=stage2_config(), tools=fake_tools(ROOT))
            self.assertEqual(downloader.call_count, 2); self.assertEqual(stats["failed"], 1); self.assertEqual(stats["success"], 1)

    def _run(self, rows: list[dict], video_ids: list[str] | None = None) -> dict[str, int]:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "2026-07-21_US_localization_top50.json"; path.write_text(json.dumps(rows), encoding="utf-8")
            with mock.patch("src.download_selected_candidates.download_one_video", return_value={"overall_status": "success", "already_complete": False}):
                return process_candidates(path, video_ids, config=stage2_config(), tools=fake_tools(ROOT))


class CoreTests(TestCase):
    def test_local_tool_paths_are_project_local(self) -> None:
        tools = find_local_tools(get_project_paths())
        self.assertEqual(tools["yt-dlp"], ROOT / "tools" / "bin" / "yt-dlp.exe")
        self.assertEqual(tools["ffmpeg"], ROOT / "tools" / "bin" / "ffmpeg.exe")
        self.assertEqual(tools["ffprobe"], ROOT / "tools" / "bin" / "ffprobe.exe")

    def test_windows_filename_sanitizing(self) -> None:
        self.assertEqual(sanitize_windows_filename('  A<>:"/\\|?*  title...'), "A_title")
        self.assertTrue(sanitize_windows_filename("CON").startswith("_"))
        self.assertEqual(sanitize_windows_filename("\x00\x01", "abc123"), "abc123")
        self.assertLessEqual(len(sanitize_windows_filename("x" * 200)), 90)

    def test_run_command_uses_safe_subprocess_options_and_redacts_cookie_path(self) -> None:
        completed = SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with mock.patch("src.download_core.subprocess.run", return_value=completed) as runner:
            result = run_command(["yt-dlp", "--cookies", "private/secret.txt", "https://example.test/watch?v=id&token=secret"])
        kwargs = runner.call_args.kwargs
        self.assertFalse(kwargs["shell"]); self.assertTrue(kwargs["capture_output"]); self.assertEqual(kwargs["encoding"], "utf-8"); self.assertEqual(kwargs["errors"], "replace")
        self.assertNotIn("secret.txt", " ".join(result["command"])); self.assertNotIn("token=secret", " ".join(result["command"]))

    def test_probe_video_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            media = Path(name) / "source.mp4"; media.write_bytes(b"x")
            payload = {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}], "format": {"duration": "12.5"}}
            with mock.patch("src.download_core.run_command", return_value={"success": True, "returncode": 0, "stdout": json.dumps(payload), "stderr": "", "command": []}):
                self.assertTrue(probe_media(media, Path("ffprobe.exe"))["success"])

    def test_manifest_contains_all_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = write_manifest(Path(name), {"video_id": "abc"})
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(set(MANIFEST_FIELDS).issubset(payload)); self.assertEqual(payload["video_id"], "abc")

    def test_extract_audio_command_is_pcm_48k_stereo(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); video = root / "source.mp4"; audio = root / "source.wav"; video.write_bytes(b"video")
            def execute(command, cwd=None):
                audio.write_bytes(b"wav")
                return {"success": True, "returncode": 0, "stdout": "", "stderr": "", "command": [str(x) for x in command]}
            with mock.patch("src.download_core.run_command", side_effect=execute) as runner:
                result = extract_audio(video, audio, fake_tools(root), stage2_config(), get_project_paths(root))
            command = [str(item) for item in runner.call_args.args[0]]
            self.assertTrue(result["success"]); self.assertIn("pcm_s16le", command)
            self.assertEqual(command[command.index("-ar") + 1], "48000"); self.assertEqual(command[command.index("-ac") + 1], "2")


class SubtitleTests(TestCase):
    def _execute(self, created: str | None):
        calls: list[list[str]] = []
        def execute(command, cwd=None):
            values = [str(item) for item in command]; calls.append(values)
            output = Path(values[values.index("--output") + 1]) if "--output" in values else None
            if created == "manual" and "--write-subs" in values:
                output.parent.mkdir(parents=True, exist_ok=True); (output.parent / output.name.replace("%(ext)s", "matched.vtt")).write_text("WEBVTT", encoding="utf-8")
            if created == "auto" and "--write-auto-subs" in values:
                output.parent.mkdir(parents=True, exist_ok=True); (output.parent / output.name.replace("%(ext)s", "matched.vtt")).write_text("WEBVTT", encoding="utf-8")
            if values[0].endswith("ffmpeg.exe"):
                Path(values[-1]).write_text("SRT", encoding="utf-8")
            return {"success": True, "returncode": 0, "stdout": "", "stderr": "", "command": values}
        return calls, execute

    def test_manual_subtitle_prevents_auto_download(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            calls, execute = self._execute("manual")
            with mock.patch("src.download_core.run_command", side_effect=execute):
                result = download_subtitles("https://youtu.be/id", Path(name), fake_tools(Path(name)), stage2_config(), get_project_paths(Path(name)))
            self.assertEqual(result["source"], "manual")
            self.assertFalse(any("--write-auto-subs" in call for call in calls))

    def test_missing_manual_falls_back_to_auto(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            calls, execute = self._execute("auto")
            with mock.patch("src.download_core.run_command", side_effect=execute):
                result = download_subtitles("https://youtu.be/id", Path(name), fake_tools(Path(name)), stage2_config(), get_project_paths(Path(name)))
            self.assertEqual(result["source"], "auto"); self.assertTrue(any("--write-auto-subs" in call for call in calls))

    def test_no_subtitles_is_nonfatal(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _, execute = self._execute(None)
            with mock.patch("src.download_core.run_command", side_effect=execute):
                result = download_subtitles("https://youtu.be/id", Path(name), fake_tools(Path(name)), stage2_config(), get_project_paths(Path(name)))
            self.assertTrue(result["success"]); self.assertEqual(result["status"], "missing")

    def test_vtt_is_kept_and_srt_created(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            _, execute = self._execute("manual")
            with mock.patch("src.download_core.run_command", side_effect=execute):
                result = download_subtitles("https://youtu.be/id", Path(name), fake_tools(Path(name)), stage2_config(), get_project_paths(Path(name)))
            self.assertTrue(result["vtt_file"].is_file()); self.assertTrue(result["srt_file"].is_file())
            self.assertEqual((result["vtt_status"], result["srt_status"]), ("success", "success"))

    def test_chinese_manual_subtitles_are_requested_and_saved(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            calls: list[list[str]] = []
            def execute(command, cwd=None):
                values = [str(item) for item in command]; calls.append(values)
                if "--output" in values and "--write-subs" in values:
                    languages = values[values.index("--sub-langs") + 1]
                    output = Path(values[values.index("--output") + 1])
                    if "zh-Hans" in languages:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        (output.parent / output.name.replace("%(ext)s", "zh-Hans.vtt")).write_text("WEBVTT", encoding="utf-8")
                if values[0].endswith("ffmpeg.exe"):
                    Path(values[-1]).write_text("SRT", encoding="utf-8")
                return {"success": True, "returncode": 0, "stdout": "", "stderr": "", "command": values}
            with mock.patch("src.download_core.run_command", side_effect=execute):
                result = download_subtitles("https://youtu.be/id", Path(name), fake_tools(Path(name)), stage2_config(), get_project_paths(Path(name)))
            chinese = result["tracks"]["zh"]
            self.assertEqual((chinese["status"], chinese["source"]), ("success", "manual"))
            self.assertTrue((Path(name) / "subtitles" / "zh.manual.vtt").is_file())
            self.assertTrue((Path(name) / "subtitles" / "zh.manual.srt").is_file())
            self.assertTrue(any("zh-Hans" in call[call.index("--sub-langs") + 1] for call in calls if "--sub-langs" in call))

    def test_chinese_subtitles_fall_back_to_auto(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            def execute(command, cwd=None):
                values = [str(item) for item in command]
                if "--write-auto-subs" in values and "--output" in values:
                    languages = values[values.index("--sub-langs") + 1]
                    output = Path(values[values.index("--output") + 1])
                    if "zh-Hans" in languages:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        (output.parent / output.name.replace("%(ext)s", "zh-Hans.vtt")).write_text("WEBVTT", encoding="utf-8")
                if values[0].endswith("ffmpeg.exe"):
                    Path(values[-1]).write_text("SRT", encoding="utf-8")
                return {"success": True, "returncode": 0, "stdout": "", "stderr": "", "command": values}
            with mock.patch("src.download_core.run_command", side_effect=execute):
                result = download_subtitles("https://youtu.be/id", Path(name), fake_tools(Path(name)), stage2_config(), get_project_paths(Path(name)))
            self.assertEqual((result["tracks"]["zh"]["status"], result["tracks"]["zh"]["source"]), ("success", "auto"))


class ResumeAndManualTests(TestCase):
    def test_successful_task_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); task = root / "2026-07-21" / "001_abc_Title"
            for relative in ("video/source.mp4", "audio/source_audio.wav", "metadata/info.json", "metadata/description.txt"):
                target = task / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_text("{}" if target.name == "info.json" else "x", encoding="utf-8")
            (task / "metadata/info.json").write_text(json.dumps({"id": "abc", "title": "Title", "upload_date": "20260721"}), encoding="utf-8")
            write_manifest(task, {"video_id": "abc", "overall_status": "success", "subtitle_tracks": {"en": {"status": "missing"}, "zh": {"status": "missing"}}, "subtitle_clean_status": "missing"})
            result = download_core.download_one_video("https://youtu.be/abc", source_mode="candidate", candidate={"video_id": "abc", "selected": 1, "rights_status": "APPROVED"}, candidate_file="2026-07-21_US_localization_top50.json", candidate_rank=1, output_root=root, config=stage2_config(), tools=fake_tools(ROOT))
            self.assertTrue(result["already_complete"])

    def test_partial_task_does_not_redownload_existing_video_or_audio(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); task = root / "2026-07-21" / "001_abc_Title"
            for relative, content in (("video/source.mp4", "v"), ("audio/source_audio.wav", "a"), ("metadata/info.json", json.dumps({"id": "abc", "title": "Title", "upload_date": "20260721"})), ("metadata/description.txt", "d")):
                target = task / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_text(content, encoding="utf-8")
            write_manifest(task, {"video_id": "abc", "overall_status": "partial_success"})
            subtitle = {"success": True, "status": "missing", "source": "", "vtt_status": "missing", "srt_status": "missing", "command_results": [], "warning": None, "error": ""}
            thumb = {"success": False, "status": "failed", "command_result": None, "error": "missing"}
            probe = {"success": True, "status": "success", "error": "", "command_result": None}
            with mock.patch("src.download_core.download_video_media") as media, mock.patch("src.download_core.extract_audio") as audio, mock.patch("src.download_core.download_subtitles", return_value=subtitle), mock.patch("src.download_core.download_thumbnail", return_value=thumb), mock.patch("src.download_core.probe_media", return_value=probe):
                result = download_core.download_one_video("https://youtu.be/abc", source_mode="candidate", candidate={"video_id": "abc", "selected": 1, "rights_status": "APPROVED"}, candidate_file="2026-07-21_US_localization_top50.json", candidate_rank=1, output_root=root, config=stage2_config(), tools=fake_tools(ROOT))
            media.assert_not_called(); audio.assert_not_called(); self.assertEqual(result["overall_status"], "success")

    def test_manual_entry_requires_confirmation(self) -> None:
        self.assertEqual(manual_main(["--url", "https://youtu.be/abc"]), 2)

    def test_manual_entry_uses_no_playlist_and_does_not_touch_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            candidate = Path(name) / "candidate.json"; candidate.write_text("unchanged", encoding="utf-8"); before = candidate.read_bytes()
            captured: dict = {}
            def fake_download(url, **kwargs):
                captured.update(kwargs)
                return {"overall_status": "success", "already_complete": False, "task_dir": Path(name)}
            with mock.patch("src.download_video.load_download_config", return_value=stage2_config()), mock.patch("src.download_video.find_local_tools", return_value=fake_tools(ROOT)), mock.patch("src.download_video.download_one_video", side_effect=fake_download):
                code = manual_main(["--url", "https://example.test/watch?v=abc&list=playlist", "--confirm-rights"])
            self.assertEqual(code, 0); self.assertEqual(captured["source_mode"], "manual"); self.assertEqual(candidate.read_bytes(), before)

    def test_metadata_command_has_no_playlist(self) -> None:
        with mock.patch("src.download_core.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps({"id": "abc", "title": "T"}), stderr="")) as runner:
            result = download_core.fetch_video_metadata("https://example.test/watch?v=abc&list=x", fake_tools(ROOT), {**stage2_config(), "use_cookies": False}, get_project_paths(ROOT))
        self.assertTrue(result["success"]); self.assertIn("--no-playlist", runner.call_args.args[0])


class SelectionValueTests(TestCase):
    def test_only_documented_selected_values_are_true(self) -> None:
        self.assertTrue(selected_for_download(True)); self.assertTrue(selected_for_download(1)); self.assertTrue(selected_for_download("1"))
        self.assertFalse(selected_for_download(0)); self.assertFalse(selected_for_download(1.0)); self.assertFalse(selected_for_download("true")); self.assertFalse(selected_for_download(None))
