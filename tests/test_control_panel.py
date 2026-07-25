from __future__ import annotations

import json
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, mock

from src.control_panel.app import ControlPanelApp
from src.control_panel.jobs import JobStore, WorkflowWorker
from src.control_panel.server import make_handler
from src.control_panel.tasks import WorkflowScanner
from src.control_panel.youtube import (
    TargetedYouTubeSearch,
    extract_video_id,
    normalize_video_inputs,
)
from src.download_video import main as download_main


ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def make_task(project: Path, name: str = "2026-07-26/abcdefghijk_Test") -> Path:
    task = project / "downloads" / Path(name)
    write_json(
        task / "download_manifest.json",
        {
            "video_id": "abcdefghijk",
            "title": "Test video",
            "channel": "Test channel",
            "overall_status": "success",
            "errors": [],
        },
    )
    write_json(
        task / "metadata" / "info.json",
        {"id": "abcdefghijk", "title": "Test video", "duration": 123},
    )
    return task


class VideoInputTests(TestCase):
    def test_extracts_supported_youtube_shapes(self) -> None:
        video_id = "abcdefghijk"
        values = [
            video_id,
            f"https://youtu.be/{video_id}",
            f"https://www.youtube.com/watch?v={video_id}&list=abc",
            f"https://youtube.com/shorts/{video_id}",
            f"https://youtube.com/embed/{video_id}",
        ]
        self.assertEqual([extract_video_id(value) for value in values], [video_id] * 5)

    def test_normalization_deduplicates_and_rejects_unknown_values(self) -> None:
        rows = normalize_video_inputs(
            "abcdefghijk,\nhttps://youtu.be/abcdefghijk 12345678901"
        )
        self.assertEqual([row["video_id"] for row in rows], ["abcdefghijk", "12345678901"])
        with self.assertRaises(ValueError):
            normalize_video_inputs("definitely-not-a-video-id")

    def test_manual_download_records_confirmed_rights_status(self) -> None:
        captured: dict = {}

        def fake_download(url: str, **kwargs: object) -> dict:
            captured.update(kwargs)
            return {
                "overall_status": "success",
                "already_complete": False,
                "task_dir": ROOT,
            }

        with (
            mock.patch("src.download_video.load_download_config", return_value={}),
            mock.patch("src.download_video.find_local_tools", return_value={}),
            mock.patch("src.download_video.download_one_video", side_effect=fake_download),
        ):
            code = download_main(
                [
                    "--url",
                    "https://youtu.be/abcdefghijk",
                    "--confirm-rights",
                    "--rights-status",
                    "PERMISSION_GRANTED",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            captured["candidate"],
            {"rights_status": "PERMISSION_GRANTED"},
        )


class FakeYouTubeClient:
    def get(self, endpoint: str, params: dict) -> dict:
        if endpoint == "search":
            return {
                "items": [
                    {"id": {"videoId": "abcdefghijk"}},
                    {"id": {"videoId": "12345678901"}},
                ]
            }
        return {
            "items": [
                {
                    "id": video_id,
                    "snippet": {
                        "title": f"Title {video_id}",
                        "channelTitle": "Channel",
                        "publishedAt": "2026-07-25T00:00:00Z",
                        "thumbnails": {"high": {"url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"}},
                    },
                    "contentDetails": {"duration": "PT2M3S", "caption": "true"},
                    "statistics": {"viewCount": "1200", "likeCount": "80"},
                    "status": {"license": "youtube", "embeddable": True},
                }
                for video_id in params["id"].split(",")
            ]
        }


class TargetedSearchTests(TestCase):
    def test_search_returns_panel_ready_rows_in_search_order(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            (project / "config").mkdir()
            write_json(
                project / "config" / "trending_config.json",
                {
                    "region_code": "US",
                    "language": "en",
                    "safe_search": "moderate",
                    "request_timeout_seconds": 5,
                    "max_retries": 0,
                },
            )
            (project / ".env").write_text("YOUTUBE_API_KEY=test\n", encoding="utf-8")
            rows = TargetedYouTubeSearch(project).search(
                "test query", 2, client=FakeYouTubeClient()
            )
        self.assertEqual([row["video_id"] for row in rows], ["abcdefghijk", "12345678901"])
        self.assertEqual(rows[0]["duration"], "02:03")
        self.assertTrue(rows[0]["has_caption"])


class ScannerTests(TestCase):
    def test_scanner_builds_four_stage_progress(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            (task / "subtitles").mkdir()
            (task / "subtitles" / "en.selected.srt").write_text("English", encoding="utf-8")
            (task / "subtitles" / "zh.clean.srt").write_text("中文", encoding="utf-8")
            write_json(
                task / "stage4" / "stage4_manifest.json",
                {"status": "STAGE4_COMPLETED", "qc_status": "QC_PASSED"},
            )
            rows = WorkflowScanner(project).scan()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["progress"], 100)
        self.assertEqual(rows[0]["overall"], "成片完成")

    def test_task_resolution_cannot_escape_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            scanner = WorkflowScanner(Path(name))
            with self.assertRaises(ValueError):
                scanner.resolve_task("../outside")


class QueueTests(TestCase):
    def test_job_lifecycle_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            store = JobStore(root / "jobs.sqlite3", root / "logs")
            job = store.enqueue("download", "abcdefghijk", {"url": "https://youtu.be/abcdefghijk"})
            self.assertEqual(job["status"], "queued")
            claimed = store.claim_next()
            self.assertEqual(claimed["id"], job["id"])
            self.assertEqual(claimed["status"], "running")
            store.update(job["id"], status="failed", error="test")
            retried = store.retry(job["id"])
            self.assertEqual(retried["status"], "queued")

    def test_pipeline_commands_use_existing_entrypoints_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            scanner = WorkflowScanner(project)
            worker = WorkflowWorker(project, store, scanner)
            commands = worker._build_commands(
                {
                    "kind": "pipeline",
                    "target": task.relative_to(project / "downloads").as_posix(),
                    "payload": {"workflow": "complete", "render_mode": "softsub"},
                }
            )
        self.assertEqual(len(commands), 3)
        self.assertIn("run_stage3.py", commands[0][1][1])
        self.assertIn("--resume", commands[0][1])
        self.assertIn("run_stage4.py", commands[2][1][1])


class ServerSmokeTests(TestCase):
    def test_dashboard_endpoint_and_static_page(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_task(project)
            static = project / "static"
            static.mkdir()
            (static / "index.html").write_text("<!doctype html><title>Panel</title>", encoding="utf-8")
            app = ControlPanelApp(project)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, static))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(f"{base}/api/dashboard", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(base, timeout=5) as response:
                    page = response.read().decode("utf-8")
            finally:
                server.shutdown()
                server.server_close()
                app.close()
                thread.join(timeout=5)
        self.assertEqual(payload["summary"]["tasks"], 1)
        self.assertIn("<title>Panel</title>", page)
