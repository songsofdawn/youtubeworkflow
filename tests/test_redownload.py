from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from pathlib import Path
from unittest import TestCase, mock

from src import download_core, redownload_video
from src.control_panel.app import ControlPanelApp
from src.control_panel.server import make_handler
from src.control_panel.tasks import WorkflowScanner
from tests.test_control_panel import make_task, make_runtime, make_publish_config, write_json
from tests.test_download_stage2 import fake_tools, stage2_config


class RedownloadQueueTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.task = make_task(self.project, "manual/2025-01-01/abcdefghijk_Old")
        self.reference = self.task.relative_to(self.project / "downloads").as_posix()
        make_publish_config(self.project)
        self.app = ControlPanelApp(self.project)

    def test_old_task_without_job_history_queues_in_place_and_only_downloads(self):
        result = self.app.queue_redownloads(tasks=[self.reference, self.reference], confirm_rights=True)
        self.assertEqual(len(result["jobs"]), 1)
        job = result["jobs"][0]
        self.assertEqual(job["target"], self.reference)
        self.assertEqual(job["kind"], "download")
        self.assertEqual(job["resource_class"], "network")
        self.assertEqual(job["payload"], {
            "redownload": True, "confirm_rights": True, "video_id": "abcdefghijk",
            "url": "https://www.youtube.com/watch?v=abcdefghijk",
        })
        make_runtime(self.project)
        make_publish_config(self.project)
        stages = self.app.worker._build_stages(job)
        self.assertEqual(len(stages), 1)
        command = stages[0][1]
        self.assertEqual(command[1:3], ["-m", "src.redownload_video"])
        self.assertEqual(command[command.index("--task-dir") + 1], str(self.task.resolve()))
        self.assertEqual(stages[0][2], "network")

    def test_requires_confirmation_and_validates_batch_shape(self):
        with self.assertRaises(ValueError):
            self.app.queue_redownloads(tasks=[self.reference], confirm_rights=False)
        for tasks in ([], [self.reference] * 51, [None], [{}]):
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                self.app.queue_redownloads(tasks=tasks, confirm_rights=True)
        self.assertEqual(self.app.store.list(), [])

    def test_active_task_or_legacy_video_job_blocks_redownload(self):
        for target in (self.reference, "abcdefghijk"):
            with self.subTest(target=target):
                active = self.app.store.enqueue("download", target, {})
                result = self.app.queue_redownloads(tasks=[self.reference], confirm_rights=True)
                self.assertEqual(result["jobs"], [])
                self.assertIn("排队", result["errors"][0]["error"])
                self.app.store.update(active["id"], status="cancelled")

    def test_concurrent_requests_do_not_queue_duplicate_downloads(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda _: self.app.queue_redownloads(tasks=[self.reference], confirm_rights=True),
                range(2),
            ))
        self.assertEqual(sum(len(result["jobs"]) for result in results), 1)
        self.assertEqual(sum(len(result["errors"]) for result in results), 1)

    def test_batch_returns_invalid_paths_without_losing_valid_tasks(self):
        result = self.app.queue_redownloads(
            tasks=["../../outside", self.reference], confirm_rights=True,
        )
        self.assertEqual(len(result["jobs"]), 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["errors"][0]["task"], "../../outside")

    def test_server_routes_tasks_and_requires_literal_true(self):
        app = mock.Mock()
        app.queue_redownloads.return_value = {"jobs": [{"id": "download-job"}], "errors": []}
        for confirmation, expected in ((True, True), ("true", False), (None, False)):
            handler = object.__new__(make_handler(app, Path(".")))
            handler.path = "/api/tasks/redownload"
            handler._validate_local_json_request = mock.Mock()
            handler._read_json = mock.Mock(return_value={"tasks": [self.reference], "confirm_rights": confirmation})
            handler._json = mock.Mock()
            handler.do_POST()
            app.queue_redownloads.assert_called_with(tasks=[self.reference], confirm_rights=expected)
            handler._json.assert_called_once_with(HTTPStatus.ACCEPTED, app.queue_redownloads.return_value)


class RedownloadFilesTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.task = make_task(self.project, "manual/2025-01-01/abcdefghijk_Old")
        for name, data in {
            "video/source.mp4": b"nonempty damaged MP4",
            "audio/source_audio.wav": b"damaged audio",
            "subtitles/en.manual.vtt": b"damaged VTT",
            "subtitles/en.clean.srt": b"existing clean timeline",
            "subtitles/en.selected.srt": b"selected timeline",
            "subtitles/zh.clean.srt": b"translated timeline",
            "subtitles/zh.reviewed.srt": b"reviewed timeline",
            "stage3_manifest.json": b"{}",
            "dubbing/manifest.json": b"{}",
            "stage4/output.mp4": b"previous render",
            "stage5/publish_manifest.json": b"{}",
        }.items():
            path = self.task / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.before = self.snapshot()
        self.downloader = self.enterContext(mock.patch(
            "src.redownload_video.download_one_video", side_effect=self.download,
        ))
        self.decode = self.enterContext(mock.patch(
            "src.redownload_video.run_command",
            return_value={"success": True, "stderr": "", "command": ["ffmpeg", "decode"]},
        ))

    def snapshot(self):
        return {path.relative_to(self.task).as_posix(): path.read_bytes()
                for path in self.task.rglob("*") if path.is_file()}

    def download(self, url, **kwargs):
        self.assertEqual(url, "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(kwargs["source_mode"], "manual")  # no candidate archive shortcut
        self.assertFalse(kwargs.get("force", False))
        self.assertTrue(kwargs["config"]["extract_audio"])
        self.assertIn("--abort-on-unavailable-fragments", download_core._download_network_options(kwargs["config"]))
        staged = kwargs["output_root"] / "2026-09-13" / "abcdefghijk_New title"
        self.assertFalse(staged.exists())
        for name, data in {
            "video/source.mp4": b"new valid video",
            "audio/source_audio.wav": b"new valid audio",
            "subtitles/en.auto.vtt": b"new downloaded VTT",
            "subtitles/en.auto.srt": b"new downloaded SRT",
            "subtitles/en.clean.srt": b"new clean timeline",
            "metadata/info.json": b'{"id":"abcdefghijk","title":"Recovered"}',
            "metadata/description.txt": b"Recovered description",
            "metadata/thumbnail.jpg": b"new thumbnail",
        }.items():
            path = staged / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        manifest = {
            "video_id": "abcdefghijk", "title": "Recovered", "overall_status": "success",
            "source_mode": "manual", "rights_status": "PERMISSION_GRANTED",
            "commands_executed": [], "errors": [], "subtitle_clean_status": "success",
            "subtitle_clean_stats": {"clean_srt": str(staged / "subtitles/en.clean.srt")},
        }
        write_json(staged / "download_manifest.json", manifest)
        return {"overall_status": "success", "task_dir": staged, "manifest": manifest,
                "manifest_path": staged / "download_manifest.json"}

    def run_redownload(self):
        return redownload_video.redownload_task(
            self.task, project_root=self.project, confirm_rights=True,
            config={"extract_audio": False}, tools={"ffmpeg": Path("ffmpeg")},
        )

    def test_replaces_damaged_sources_backs_up_and_preserves_downstream(self):
        result = self.run_redownload()
        self.assertEqual(result["overall_status"], "success")
        self.assertEqual(result["task_dir"], self.task.resolve())
        self.assertEqual((self.task / "video/source.mp4").read_bytes(), b"new valid video")
        self.assertFalse((self.task / "subtitles/en.manual.vtt").exists())
        backup = Path(result["backup_dir"])
        for name in ("video/source.mp4", "audio/source_audio.wav", "subtitles/en.manual.vtt"):
            self.assertEqual((backup / name).read_bytes(), self.before[name])
        self.assertEqual((backup / "manifest.json").read_bytes(), self.before["download_manifest.json"])
        for name in ("subtitles/en.clean.srt", "subtitles/en.selected.srt", "subtitles/zh.clean.srt",
                     "subtitles/zh.reviewed.srt", "stage3_manifest.json", "dubbing/manifest.json",
                     "stage4/output.mp4", "stage5/publish_manifest.json"):
            self.assertEqual((self.task / name).read_bytes(), self.before[name])
        manifest = json.loads((self.task / "download_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["title"], "Recovered")
        self.assertEqual(manifest["redownload_backup"], backup.relative_to(self.task).as_posix())
        self.assertEqual(len(WorkflowScanner(self.project).scan()), 1)
        self.assertIn("-xerror", self.decode.call_args.args[0])

    def test_network_failure_preserves_every_original_file(self):
        original_download = self.download
        def fail(url, **kwargs):
            result = original_download(url, **kwargs)
            result["overall_status"] = "partial_success"
            return result
        self.downloader.side_effect = fail
        result = self.run_redownload()
        self.assertEqual(result["overall_status"], "partial_success")
        self.assertEqual(self.snapshot(), self.before)
        self.decode.assert_not_called()
        self.assertEqual(len(WorkflowScanner(self.project).scan()), 1)

    def test_failed_decode_never_replaces_old_material(self):
        self.decode.return_value = {"success": False, "stderr": "truncated frame", "command": ["ffmpeg"]}
        self.assertEqual(self.run_redownload()["overall_status"], "failed")
        self.assertEqual(self.snapshot(), self.before)
        manifests = list((self.project / "work/redownload").rglob("download_manifest.json"))
        staged_manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(staged_manifest["probe_status"], "failed")

    def test_failed_install_rolls_back_sources_and_manifest(self):
        real_replace = Path.replace
        def replace(path, target):
            if target == self.task / "audio/source_audio.wav":
                raise OSError("file locked")
            return real_replace(path, target)
        with mock.patch.object(Path, "replace", replace), self.assertRaises(OSError):
            self.run_redownload()
        after = self.snapshot()
        for name, data in self.before.items():
            self.assertEqual(after[name], data)

    def test_wrong_video_id_does_not_replace_sources(self):
        def wrong(url, **kwargs):
            result = self.download(url, **kwargs)
            result["manifest"]["video_id"] = "12345678901"
            return result
        self.downloader.side_effect = wrong
        with self.assertRaises(ValueError):
            self.run_redownload()
        self.assertEqual(self.snapshot(), self.before)

    def test_manual_rights_checked_before_network_access(self):
        with self.assertRaises(ValueError):
            redownload_video.redownload_task(self.task, project_root=self.project, confirm_rights=False)
        self.downloader.assert_not_called()

    def test_candidate_must_remain_selected_and_approved(self):
        manifest = json.loads(self.before["download_manifest.json"])
        manifest.update(source_mode="candidate", candidate_file="old-candidates.json", candidate_rank=7)
        write_json(self.task / "download_manifest.json", manifest)
        for selected, rights in ((0, "APPROVED"), (1, "PENDING")):
            write_json(self.task / "metadata/candidate.json", {
                "video_id": "abcdefghijk", "selected": selected, "rights_status": rights,
            })
            with self.subTest(selected=selected, rights=rights), self.assertRaises(ValueError):
                self.run_redownload()
        self.downloader.assert_not_called()
        write_json(self.task / "metadata/candidate.json", {
            "video_id": "abcdefghijk", "selected": 1, "rights_status": "LICENSED",
        })
        self.run_redownload()
        recovered = json.loads((self.task / "download_manifest.json").read_text(encoding="utf-8"))
        for field in ("source_mode", "candidate_file", "candidate_rank"):
            self.assertEqual(recovered[field], manifest[field])
        self.assertEqual(recovered["rights_status"], "LICENSED")
        self.assertEqual(recovered["selected"], 1)


class SubtitleNetworkFailureTests(TestCase):
    def test_network_failure_is_not_reported_as_no_subtitles(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            with mock.patch("src.download_core.run_command", return_value={
                "success": False, "stderr": "HTTP 503", "stdout": "", "command": [],
            }):
                result = download_core.download_subtitles(
                    "https://youtu.be/abcdefghijk", root, fake_tools(root),
                    stage2_config(), download_core.get_project_paths(root),
                )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "failed")
        self.assertTrue(all(track["srt_status"] == "failed" for track in result["tracks"].values()))
