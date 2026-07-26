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
from src.control_panel.publishing import BiliupIntegration
from src.control_panel.server import make_handler
from src.control_panel.tasks import WorkflowScanner
from src.control_panel.youtube import (
    TargetedYouTubeSearch,
    extract_video_id,
    normalize_video_inputs,
)
from src.download_video import main as download_main
from src.run_control_panel import panel_build_id
from src.stage3.publish_metadata import utf8_bytes, utf16_code_units


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


def make_publish_config(project: Path) -> tuple[Path, Path]:
    executable = project / "bbup-app" / "binaries" / "biliup.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"fake")
    account_dir = project / "bbup-app" / "data"
    write_json(
        account_dir / "10001.json",
        {"cookie_info": {}, "token_info": {}, "platform": "TV"},
    )
    write_json(
        project / "config" / "bilibili_categories.json",
        {
            "schema_version": 1,
            "fallback_tid": 21,
            "categories": [
                {
                    "tid": 21,
                    "name": "日常",
                    "parent_tid": 160,
                    "parent_name": "生活",
                },
                {
                    "tid": 231,
                    "name": "计算机技术",
                    "parent_tid": 188,
                    "parent_name": "科技",
                },
            ],
        },
    )
    write_json(
        project / "config" / "publish_config.json",
        {
            "biliup_executable_candidates": [str(executable)],
            "account_directories": [str(account_dir)],
            "category_mapping": "config/bilibili_categories.json",
            "default_submit": "web",
            "default_tid": 21,
            "upload_limit": 3,
            "default_copyright": 2,
            "default_only_self": True,
            "default_no_reprint": True,
            "description_disclaimer": "【免责声明】\n测试声明。",
            "description_original_heading": "【原视频简介】",
        },
    )
    return executable, account_dir / "10001.json"


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
        self.assertEqual(rows[0]["progress"], 80)
        self.assertEqual(rows[0]["overall"], "成片完成，等待投稿")

    def test_published_manifest_completes_fifth_stage(self) -> None:
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
            write_json(
                task / "stage5" / "publish_manifest.json",
                {
                    "status": "PUBLISHED",
                    "bvid": "BV1xx411c7mD",
                    "url": "https://www.bilibili.com/video/BV1xx411c7mD",
                },
            )
            row = WorkflowScanner(project).scan()[0]
        self.assertEqual(row["progress"], 100)
        self.assertEqual(row["overall"], "投稿完成")
        self.assertEqual(row["bvid"], "BV1xx411c7mD")

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
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                store,
                scanner,
                BiliupIntegration(project),
            )
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


class PublishingTests(TestCase):
    def test_detects_account_without_returning_cookie_contents(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            publishing = BiliupIntegration(project)
            accounts = publishing.accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["label"], "10001")
        self.assertNotIn("cookie_info", json.dumps(accounts))

    def test_validated_submission_builds_v122_cli_command(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            executable, account = make_publish_config(project)
            task = make_task(project)
            (task / "subtitles").mkdir()
            (task / "subtitles" / "zh.clean.srt").write_text("中文", encoding="utf-8")
            publishing = BiliupIntegration(project)
            defaults = publishing.defaults(task)
            payload = publishing.validate_submission(
                task,
                defaults | {"confirm_publish": True},
            )
            command = publishing.build_upload_command(task, payload)
        self.assertEqual(command[0], str(executable))
        self.assertEqual(command[1:4], ["--user-cookie", str(account), "upload"])
        self.assertIn("--submit", command)
        self.assertIn("web", command)
        self.assertIn("--copyright", command)
        self.assertEqual(command[-1], str(publishing.expected_hardsub(task)))
        self.assertTrue(payload["prepare_hardsub"])

    def test_defaults_load_translated_title_tags_category_and_original_description(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            task = make_task(project)
            (task / "metadata" / "description.txt").write_text(
                "This is the original metadata description.",
                encoding="utf-8",
            )
            write_json(
                task / "stage3" / "publish_metadata.json",
                {
                    "status": "RECOMMENDED",
                    "title_zh": "从零构建可靠的软件系统",
                    "upload_title": "【中英双语】从零构建可靠的软件系统｜Test video",
                    "tags": "软件工程,系统设计,编程",
                    "tid": 231,
                    "recommendation_reason": "内容主要讲解软件工程。",
                },
            )
            defaults = BiliupIntegration(project).defaults(task)
        self.assertEqual(
            defaults["title"],
            "【中英双语】从零构建可靠的软件系统｜Test video",
        )
        self.assertEqual(defaults["category_path"], "科技 / 计算机技术")
        self.assertEqual(defaults["tid"], 231)
        self.assertIn("软件工程", defaults["tags"])
        self.assertIn("【免责声明】", defaults["description"])
        self.assertIn("This is the original metadata description.", defaults["description"])

    def test_submission_requires_explicit_final_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            task = make_task(project)
            (task / "subtitles").mkdir()
            (task / "subtitles" / "zh.clean.srt").write_text("中文", encoding="utf-8")
            publishing = BiliupIntegration(project)
            with self.assertRaisesRegex(ValueError, "确认"):
                publishing.validate_submission(task, publishing.defaults(task))

    def test_old_publish_payload_is_shortened_using_bilibili_counting(self) -> None:
        payload = {
            "title": "【中英双语】测试｜Test",
            "description": ("x" * 1999) + "🧰",
            "dynamic": "动态",
        }
        prepared = BiliupIntegration.prepare_payload_for_execution(payload)
        self.assertLessEqual(utf16_code_units(prepared["description"]), 2000)
        self.assertLessEqual(utf8_bytes(prepared["description"]), 1900)
        self.assertTrue(prepared["description"].endswith("…"))

    def test_bilibili_21010_has_actionable_error(self) -> None:
        log = (
            'ResponseData { code: 21010, data: None, '
            'message: "简介字数过长，请缩减内容", ttl: Some(1) }'
        )
        message = BiliupIntegration.explain_upload_failure(log, 1)
        self.assertIn("简介字数过长", message)
        self.assertIn("重新打开", message)

    def test_worker_decodes_windows_gbk_output(self) -> None:
        text = "简介字数过长，请缩减内容"
        self.assertEqual(
            WorkflowWorker._decode_process_output(text.encode("gb18030")),
            text,
        )


class ServerSmokeTests(TestCase):
    def test_dashboard_endpoint_and_static_page(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_task(project)
            make_publish_config(project)
            static = project / "static"
            static.mkdir()
            (static / "index.html").write_text("<!doctype html><title>Panel</title>", encoding="utf-8")
            app = ControlPanelApp(project)
            runtime = {
                "project_root": str(project.resolve()),
                "pid": 12345,
                "build_id": "test-build",
            }
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                make_handler(app, static, runtime),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                with urllib.request.urlopen(f"{base}/api/dashboard", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(f"{base}/api/runtime", timeout=5) as response:
                    runtime_payload = json.loads(response.read().decode("utf-8"))
                with urllib.request.urlopen(base, timeout=5) as response:
                    page = response.read().decode("utf-8")
            finally:
                server.shutdown()
                server.server_close()
                app.close()
                thread.join(timeout=5)
        self.assertEqual(payload["summary"]["tasks"], 1)
        self.assertEqual(runtime_payload["build_id"], "test-build")
        self.assertEqual(runtime_payload["project_root"], str(project.resolve()))
        self.assertIn("<title>Panel</title>", page)


class PanelLauncherTests(TestCase):
    def test_build_id_changes_when_panel_sources_change(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            (project / "src").mkdir()
            (project / "config").mkdir()
            source = project / "src" / "panel.py"
            source.write_text("VERSION = 1\n", encoding="utf-8")
            first = panel_build_id(project)
            source.write_text("VERSION = 2\n", encoding="utf-8")
            second = panel_build_id(project)
        self.assertNotEqual(first, second)
