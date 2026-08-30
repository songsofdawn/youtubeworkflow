from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import TestCase, mock

from src.control_panel.app import ControlPanelApp
from src.control_panel.jobs import JobCancelled, JobStore, WorkflowWorker
from src.control_panel.publishing import BiliupIntegration
from src.control_panel.server import make_handler
from src.control_panel.settings import (
    save_youtube_cookie_file,
    update_env_file,
    validate_youtube_cookie_text,
    youtube_cookie_status,
)
from src.control_panel.tasks import WorkflowScanner, youtube_chinese_path
from src.control_panel.youtube import (
    TargetedYouTubeSearch,
    extract_video_id,
    load_discovery_packs,
    normalize_video_inputs,
    public_discovery_catalog,
)
from src.download_video import main as download_main
from src.run_control_panel import panel_build_id
from src.stage3.publish_metadata import utf8_bytes, utf16_code_units


ROOT = Path(__file__).resolve().parents[1]
YOUTUBE_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tLOGIN_INFO\tcookie-secret\n"
)


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


def mark_deepseek_translation(task: Path, status: str = "QC_PASSED") -> None:
    write_json(
        task / "stage3_manifest.json",
        {
            "translation_status": status,
            "p1_status": status,
            "translation_source_hash": "translated",
        },
    )


def make_layout_review_task(project: Path) -> tuple[Path, str, str]:
    task = make_task(project)
    subtitles = task / "subtitles"
    subtitles.mkdir(parents=True, exist_ok=True)
    english_text = "very long english text " * 30
    chinese_text = "很长的中文字幕" * 40
    english = subtitles / "en.selected.srt"
    chinese = subtitles / "zh.clean.srt"
    english.write_text(
        f"1\n00:00:00,000 --> 00:00:01,000\n{english_text}"
        "\n\n2\n00:00:01,000 --> 00:00:02,000\nSafe second cue.\n",
        encoding="utf-8",
    )
    chinese.write_text(
        f"1\n00:00:00,000 --> 00:00:01,000\n{chinese_text}"
        "\n\n2\n00:00:01,000 --> 00:00:02,000\n安全的第二条字幕。\n",
        encoding="utf-8",
    )
    mark_deepseek_translation(task)
    write_json(
        task / "stage4" / "stage4_manifest.json",
        {
            "status": "REVIEW_REQUIRED",
            "qc_status": "REVIEW_REQUIRED",
            "output_mode": "hardsub",
            "chinese_subtitle_source": "deepseek",
            "english_subtitle_path": str(english),
            "chinese_subtitle_path": str(chinese),
            "source_video_probe": {
                "duration": 2.0,
                "width": 1920,
                "height": 1080,
                "display_width": 1920,
                "display_height": 1080,
            },
            "review": {
                "code": "SUBTITLE_LAYOUT_REVIEW_REQUIRED",
                "message": "1 条字幕需要复核",
                "issue_ids": ["1"],
            },
        },
    )
    write_json(
        task / "stage4" / "qc" / "subtitle_qc.json",
        {
            "layout_warnings": [
                {"code": "BILINGUAL_FRAGMENT_DURATION_TOO_SHORT", "id": "1"}
            ]
        },
    )
    (project / "config").mkdir(exist_ok=True)
    (project / "config" / "stage4_config.json").write_text(
        (ROOT / "config" / "stage4_config.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    return task, english_text, chinese_text


def make_runtime(project: Path) -> Path:
    executable = project / "runtime" / "python" / "python.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"fake-python")
    return executable


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


class StaticPanelContractTests(TestCase):
    def test_publish_interval_is_user_configurable(self) -> None:
        page = (ROOT / "src" / "control_panel" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        script = (ROOT / "src" / "control_panel" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="publishMinIntervalMinutes"', page)
        self.assertIn('value="3"', page)
        self.assertIn("publish_min_interval_minutes", script)

    def test_unattended_publishing_defaults_to_public(self) -> None:
        page = (ROOT / "src" / "control_panel" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        checkbox = re.search(
            r'<input\s+id="automationOnlySelf"[^>]*>',
            page,
        )
        self.assertIsNotNone(checkbox)
        self.assertNotIn("checked", checkbox.group(0))
        self.assertIn("默认关闭，即公开投稿", page)

    def test_batch_delete_and_automation_flow_are_visible(self) -> None:
        page = (ROOT / "src" / "control_panel" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        script = (ROOT / "src" / "control_panel" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('id="deleteSelectedTasks"', page)
        self.assertIn('id="automationRenderMode"', page)
        self.assertIn('id="automationFailurePolicy"', page)
        self.assertIn('id="automationSilentVideoPolicy"', page)
        self.assertIn('id="automationTarget"', page)
        self.assertIn('id="automationEnglishPolicy"', page)
        self.assertIn('id="automationChinesePolicy"', page)
        self.assertIn('<option value="api_always">', page)
        self.assertIn('id="automationFlow"', page)
        self.assertIn('id="discoveryMaxDurationMinutes"', page)
        self.assertIn("/api/tasks/delete-batch", script)
        batch_delete_handler = script[
            script.index('$("#deleteSelectedTasks").addEventListener') :
            script.index('$$("[data-workflow]")')
        ]
        self.assertIn("window.confirm", batch_delete_handler)
        self.assertNotIn("window.prompt", batch_delete_handler)
        self.assertIn("updateAutomationFlow", script)

    def test_sidebar_can_shrink_inside_the_workspace_grid(self) -> None:
        stylesheet = (
            ROOT / "src" / "control_panel" / "static" / "styles.css"
        ).read_text(encoding="utf-8")
        side_column_rule = re.search(r"\.side-column,\s*\n\.system-card,", stylesheet)
        self.assertIsNotNone(side_column_rule)
        shrink_group = stylesheet[
            side_column_rule.start() : stylesheet.find("}", side_column_rule.start())
        ]
        self.assertIn("min-width: 0;", shrink_group)

    def test_first_run_guide_explains_optional_services_and_cpu_mode(self) -> None:
        page = (ROOT / "src" / "control_panel" / "static" / "index.html").read_text(
            encoding="utf-8"
        )
        script = (ROOT / "src" / "control_panel" / "static" / "app.js").read_text(
            encoding="utf-8"
        )
        stylesheet = (
            ROOT / "src" / "control_panel" / "static" / "styles.css"
        ).read_text(encoding="utf-8")
        self.assertIn("首次使用：按需要配置服务", page)
        self.assertIn("直接粘贴视频 URL 下载不需要此密钥", page)
        self.assertIn("NETSCAPE COOKIES.TXT", page)
        self.assertIn("Cookie 等同登录凭据", page)
        self.assertIn("打开登录工具", page)
        self.assertIn("Whisper CPU", script)
        self.assertIn("速度较慢", script)
        self.assertIn('id="automationEnglishPolicy"', page)
        self.assertIn('value="quality"', page)
        self.assertIn('id="automationChinesePolicy"', page)
        self.assertIn('value="youtube_preferred"', page)
        self.assertIn("whisper_for_auto_subtitles", script)
        self.assertIn("并行调度 v0.5", page)
        self.assertIn("renderScheduler", script)
        self.assertIn('id="translationProviderSelect"', page)
        self.assertIn("GLM-4.7-Flash", page)
        self.assertIn("translation_api_key", script)
        self.assertIn('provider.default_thinking || "disabled"', script)
        self.assertIn('id="renderReviewDialog"', page)
        self.assertIn("保存、重新检查并继续成片", page)
        self.assertIn("openRenderReview", script)
        self.assertIn("/api/render-review", script)
        self.assertIn("忽略此条并继续生成", script)
        self.assertIn("supports_hide_from_render", script)
        self.assertIn("尚未保存", script)
        self.assertIn('id="discoveryForm"', page)
        self.assertIn("智能筛选有趣视频", page)
        self.assertIn('<option value="336">最近 14 天</option>', page)
        self.assertIn('<option value="720">最近 30 天</option>', page)
        self.assertIn("Qwen 已评审", script)
        self.assertIn("/api/discovery/feedback", script)
        self.assertIn('id="discoveryOllamaModel"', page)
        self.assertIn('id="discoveryRecallTarget"', page)
        self.assertIn('id="discoveryMetadataMaxCandidates"', page)
        self.assertIn('id="discoveryMinDurationMinutes"', page)
        self.assertIn("discoveryPacks", script)
        self.assertIn("/api/discover", script)
        self.assertIn("show-discovery-result", script)
        self.assertIn("查看结果", script)
        self.assertIn("restoreLatestDiscoveryResult", script)
        self.assertIn("loadDiscoveryResult", script)
        self.assertIn("summarizeDiscoveryWarnings", script)
        self.assertIn("逐视频错误详情", script)
        self.assertIn("已自动回退到规则评分", script)
        self.assertIn(".discovery-warning-details", stylesheet)


class SettingsTests(TestCase):
    def test_app_saves_publish_interval_and_updates_live_health(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            app = ControlPanelApp(project)
            try:
                response = app.save_settings({"publish_min_interval_minutes": 3})
                live_seconds = app.publisher.publish_min_interval_seconds()
            finally:
                app.close()
            config = json.loads(
                (project / "config" / "publish_config.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(response["saved"], ["publish_min_interval_minutes"])
        self.assertEqual(config["publish_min_interval_seconds"], 180)
        self.assertEqual(live_seconds, 180)
        self.assertEqual(
            response["health"]["publishing"]["publish_min_interval_minutes"],
            3,
        )

    def test_app_rejects_invalid_publish_interval(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            app = ControlPanelApp(project)
            try:
                for value in (0, 1441, 2.5, True):
                    with self.subTest(value=value):
                        with self.assertRaisesRegex(ValueError, "投稿最短间隔"):
                            app.save_settings({"publish_min_interval_minutes": value})
            finally:
                app.close()

    def test_switching_provider_defaults_thinking_to_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            app = ControlPanelApp(project)
            health = {
                "llm": {"active": {"provider": "zhipu"}},
                "checks": {},
            }
            try:
                with mock.patch.object(app, "health", return_value=health):
                    app.save_settings(
                        {
                            "translation_provider": "deepseek",
                            "translation_model": "deepseek-v4-flash",
                            "translation_base_url": "https://api.deepseek.com",
                        }
                    )
            finally:
                app.close()
            content = (project / ".env").read_text(encoding="utf-8")
        self.assertIn("TRANSLATION_THINKING=disabled", content)

    def test_app_saves_active_provider_key_and_token_settings_without_echo(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            app = ControlPanelApp(project)
            try:
                response = app.save_settings(
                    {
                        "translation_provider": "zhipu",
                        "translation_model": "glm-4.7-flash",
                        "translation_base_url": "https://open.bigmodel.cn/api/paas/v4/",
                        "translation_thinking": "enabled",
                        "translation_batch_size": 32,
                        "translation_context_before": 2,
                        "translation_context_after": 2,
                        "translation_max_output_tokens": 4096,
                        "translation_api_key": "zhipu-unit-secret",
                    }
                )
            finally:
                app.close()
            content = (project / ".env").read_text(encoding="utf-8")
        self.assertIn("TRANSLATION_PROVIDER=zhipu", content)
        self.assertIn("TRANSLATION_MODEL=glm-4.7-flash", content)
        self.assertIn("ZHIPU_API_KEY=zhipu-unit-secret", content)
        self.assertNotIn("zhipu-unit-secret", json.dumps(response))
        self.assertEqual(response["health"]["llm"]["active"]["provider"], "zhipu")
        self.assertTrue(response["health"]["checks"]["translation_api"])

    def test_youtube_cookie_file_is_validated_and_saved_without_echo(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            app = ControlPanelApp(project)
            try:
                response = app.update_youtube_cookies(
                    {"action": "save", "content": YOUTUBE_COOKIES}
                )
                health = app.health()
            finally:
                app.close()
            cookie_path = project / "private" / "cookies.txt"
            saved = cookie_path.read_text(encoding="utf-8")
            status = youtube_cookie_status(cookie_path)
        self.assertEqual(saved, YOUTUBE_COOKIES)
        self.assertTrue(status["ready"])
        self.assertTrue(health["checks"]["youtube_cookies"])
        self.assertNotIn("cookie-secret", json.dumps(response))

    def test_youtube_cookie_validation_rejects_json_and_other_sites(self) -> None:
        with self.assertRaisesRegex(ValueError, "Netscape"):
            validate_youtube_cookie_text('{"cookies": []}')
        with self.assertRaisesRegex(ValueError, "youtube.com"):
            validate_youtube_cookie_text(
                "# HTTP Cookie File\n.example.com\tTRUE\t/\tTRUE\t1\tSID\tvalue\n"
            )

    def test_youtube_cookie_clear_removes_local_login_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            cookie_path = project / "private" / "cookies.txt"
            save_youtube_cookie_file(cookie_path, YOUTUBE_COOKIES)
            app = ControlPanelApp(project)
            try:
                response = app.update_youtube_cookies({"action": "clear"})
            finally:
                app.close()
            self.assertFalse(cookie_path.exists())
        self.assertTrue(response["cleared"])

    def test_env_update_is_atomic_and_preserves_non_secret_settings(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / ".env"
            path.write_text(
                "# local settings\nYOUTUBE_API_KEY=old\nDEEPSEEK_MODEL=custom-model\n",
                encoding="utf-8",
            )
            update_env_file(
                path,
                {"YOUTUBE_API_KEY": "new-youtube", "DEEPSEEK_API_KEY": "new-deepseek"},
            )
            content = path.read_text(encoding="utf-8")
        self.assertIn("# local settings", content)
        self.assertIn("YOUTUBE_API_KEY=new-youtube", content)
        self.assertIn("DEEPSEEK_API_KEY=new-deepseek", content)
        self.assertIn("DEEPSEEK_MODEL=custom-model", content)
        self.assertNotIn("YOUTUBE_API_KEY=old", content)

    def test_app_saves_keys_without_returning_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            app = ControlPanelApp(project)
            try:
                response = app.save_settings(
                    {
                        "youtube_api_key": "youtube-secret",
                        "deepseek_api_key": "deepseek-secret",
                    }
                )
            finally:
                app.close()
            content = (project / ".env").read_text(encoding="utf-8")
        self.assertEqual(response["saved"], ["DEEPSEEK_API_KEY", "YOUTUBE_API_KEY"])
        self.assertNotIn("youtube-secret", json.dumps(response))
        self.assertNotIn("deepseek-secret", json.dumps(response))
        self.assertIn("YOUTUBE_API_KEY=youtube-secret", content)
        self.assertIn("DEEPSEEK_API_KEY=deepseek-secret", content)

    @mock.patch("src.control_panel.app.subprocess.Popen")
    def test_biliup_login_uses_bundled_visible_application(self, popen: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            executable = project / "biliup" / "bbup-app" / "tauri-app.exe"
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_bytes(b"fake")
            app = ControlPanelApp(project)
            try:
                response = app.open_biliup_login()
            finally:
                app.close()
        self.assertTrue(response["opened"])
        popen.assert_called_once_with([str(executable)], cwd=executable.parent, shell=False)

    def test_health_explains_cpu_whisper_profile(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            make_runtime(project)
            for tool in ("yt-dlp", "ffmpeg", "ffprobe"):
                path = project / "tools" / "bin" / f"{tool}.exe"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fake")
            (project / "models" / "faster-whisper-large-v3").mkdir(parents=True)
            write_json(
                project / "portable_manifest.json",
                {
                    "portable": True,
                    "edition": "cpu",
                    "asr_device": "cpu",
                    "asr_compute_type": "int8",
                },
            )
            app = ControlPanelApp(project)
            try:
                health = app.health()
            finally:
                app.close()
        self.assertTrue(health["ready"])
        self.assertEqual(health["asr"]["device"], "cpu")
        self.assertEqual(health["asr"]["compute_type"], "int8")
        self.assertIn("速度较慢", health["asr"]["performance_note"])

    def test_development_health_uses_stage3_asr_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            write_json(
                project / "config" / "stage3_config.json",
                {"asr": {"device": "cuda", "compute_type": "float16"}},
            )
            app = ControlPanelApp(project)
            try:
                health = app.health()
            finally:
                app.close()
        self.assertEqual(health["asr"]["device"], "cuda")
        self.assertEqual(health["asr"]["compute_type"], "float16")


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
    def test_discovery_catalog_loads_editable_keyword_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            config_path = project / "config" / "discovery_keywords.json"
            write_json(
                config_path,
                {
                    "schema_version": 1,
                    "packs": [
                        {
                            "id": "custom_topic",
                            "label": "自定义领域",
                            "description": "用户维护的搜索方向",
                            "enabled": True,
                            "default_selected": False,
                            "query": "first search|second search",
                            "keywords": ["first", "second"],
                        },
                        {
                            "id": "disabled_topic",
                            "label": "停用领域",
                            "description": "不应显示",
                            "enabled": False,
                            "query": "disabled",
                            "keywords": ["disabled"],
                        },
                    ],
                },
            )
            packs = load_discovery_packs(config_path)
            catalog = TargetedYouTubeSearch(project).discovery_catalog()

        self.assertEqual([pack["id"] for pack in packs], ["custom_topic"])
        self.assertEqual(catalog[0]["label"], "自定义领域")
        self.assertEqual(catalog[0]["examples"], ["first search", "second search"])
        self.assertFalse(catalog[0]["default_selected"])

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

    def test_discovery_groups_recent_hot_results_and_excludes_local_video(self) -> None:
        class DiscoveryClient:
            def __init__(self) -> None:
                self.search_calls: list[dict] = []

            def get(self, endpoint: str, params: dict) -> dict:
                if endpoint == "search":
                    self.search_calls.append(params)
                    unique = "minecrft001" if "Minecraft" in params["q"] else "aitech00001"
                    return {
                        "items": [
                            {"id": {"videoId": unique}},
                            {"id": {"videoId": "knownvid001"}},
                        ]
                    }
                resources = {
                    "aitech00001": {
                        "title": "I Tested a New AI Model",
                        "description": "AI model benchmark and technology experiment",
                        "views": "64000",
                    },
                    "minecrft001": {
                        "title": "Minecraft But Walking Generates the World",
                        "description": "Minecraft survival challenge",
                        "views": "92000",
                    },
                    "knownvid001": {
                        "title": "Known Video",
                        "description": "already downloaded",
                        "views": "999999",
                    },
                }
                return {
                    "items": [
                        {
                            "id": video_id,
                            "snippet": {
                                "title": resources[video_id]["title"],
                                "description": resources[video_id]["description"],
                                "channelTitle": "Channel " + video_id,
                                "publishedAt": "2026-08-23T12:00:00Z",
                                "defaultAudioLanguage": "en-US",
                                "liveBroadcastContent": "none",
                                "thumbnails": {
                                    "high": {
                                        "url": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
                                    }
                                },
                            },
                            "contentDetails": {"duration": "PT8M", "caption": "true"},
                            "statistics": {
                                "viewCount": resources[video_id]["views"],
                                "likeCount": "4000",
                                "commentCount": "300",
                            },
                            "status": {
                                "license": "youtube",
                                "embeddable": True,
                                "privacyStatus": "public",
                            },
                        }
                        for video_id in params["id"].split(",")
                    ]
                }

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
                    "min_duration_seconds": 60,
                    "max_duration_seconds": 2700,
                    "exclude_shorts": True,
                    "shorts_max_duration_seconds": 180,
                    "hard_exclude_phrases": [],
                    "discovery_recall_target": 3,
                    "discovery_recall_candidates_per_result": 1,
                    "discovery_max_search_requests": 2,
                },
            )
            (project / ".env").write_text("YOUTUBE_API_KEY=test\n", encoding="utf-8")
            client = DiscoveryClient()
            searcher = TargetedYouTubeSearch(project)
            payload = searcher.discover(
                ["ai_technology", "minecraft"],
                72,
                6,
                known_video_ids={"knownvid001"},
                known_titles=["I Tested a New AI Model Today"],
                client=client,
                now=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
            )
            repeated = searcher.discover(
                ["ai_technology", "minecraft"],
                72,
                6,
                known_video_ids={"knownvid001"},
                known_titles=["I Tested a New AI Model Today"],
                client=client,
                now=datetime(2026, 8, 24, 13, tzinfo=timezone.utc),
            )

        self.assertEqual(len(public_discovery_catalog()), 14)
        self.assertEqual(len(client.search_calls), 4)
        self.assertTrue(all(call["order"] == "viewCount" for call in client.search_calls))
        self.assertTrue(all("publishedAfter" in call for call in client.search_calls))
        self.assertTrue(all(call["maxResults"] == 50 for call in client.search_calls))
        self.assertEqual(payload["summary"]["excluded"]["known_video"], 1)
        self.assertEqual([row["video_id"] for row in payload["results"]], ["minecrft001"])
        self.assertEqual([group["label"] for group in payload["groups"]], ["AI 与新科技", "Minecraft"])
        self.assertTrue(all(row["hot_score"] >= 0 for row in payload["results"]))
        self.assertEqual(payload["summary"]["excluded"]["similar_candidate"], 1)
        self.assertEqual(repeated["summary"]["history_repeat_count"], 1)
        minecraft_repeat = next(
            row for row in repeated["results"] if row["video_id"] == "minecrft001"
        )
        self.assertTrue(minecraft_repeat["seen_in_previous_search"])
        self.assertEqual(minecraft_repeat["collision_status"], "曾展示，已轻微降权")
        self.assertFalse(minecraft_repeat["similar_candidate"])

    def test_discovery_month_window_uses_larger_pool_and_adaptive_popularity(self) -> None:
        class MonthClient:
            def __init__(self) -> None:
                self.search_params: dict = {}

            def get(self, endpoint: str, params: dict) -> dict:
                if endpoint == "search":
                    self.search_params = params
                    return {"items": [{"id": {"videoId": "monthvideo1"}}]}
                return {
                    "items": [
                        {
                            "id": "monthvideo1",
                            "snippet": {
                                "title": "I Tested a Useful New AI Tool",
                                "description": "AI tool comparison and technology test",
                                "channelTitle": "AI Channel",
                                "publishedAt": "2026-08-04T12:00:00Z",
                                "defaultAudioLanguage": "en-US",
                                "liveBroadcastContent": "none",
                                "thumbnails": {},
                            },
                            "contentDetails": {"duration": "PT12M", "caption": "true"},
                            "statistics": {
                                "viewCount": "3000",
                                "likeCount": "150",
                                "commentCount": "20",
                            },
                            "status": {
                                "license": "youtube",
                                "embeddable": True,
                                "privacyStatus": "public",
                            },
                        }
                    ]
                }

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
                    "min_duration_seconds": 60,
                    "max_duration_seconds": 2700,
                    "exclude_shorts": True,
                    "shorts_max_duration_seconds": 180,
                    "hard_exclude_phrases": [],
                    "discovery_search_results_per_pack": 50,
                    "discovery_recall_target": 1,
                    "discovery_recall_candidates_per_result": 1,
                    "discovery_max_search_requests": 1,
                    "discovery_min_view_count_by_window": {"720": 2000},
                    "discovery_min_views_per_hour_by_window": {"720": 1.5},
                },
            )
            (project / ".env").write_text("YOUTUBE_API_KEY=test\n", encoding="utf-8")
            client = MonthClient()
            payload = TargetedYouTubeSearch(project).discover(
                ["ai_technology"],
                720,
                8,
                client=client,
                now=datetime(2026, 8, 24, 12, tzinfo=timezone.utc),
            )

        self.assertEqual(client.search_params["maxResults"], 50)
        self.assertEqual(payload["summary"]["minimum_views_per_hour"], 1.5)
        self.assertEqual([row["video_id"] for row in payload["results"]], ["monthvideo1"])


class ScannerTests(TestCase):
    def test_scanner_builds_four_stage_progress(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            (task / "subtitles").mkdir()
            (task / "subtitles" / "en.selected.srt").write_text("English", encoding="utf-8")
            (task / "subtitles" / "zh.clean.srt").write_text("中文", encoding="utf-8")
            mark_deepseek_translation(task)
            write_json(
                task / "stage4" / "stage4_manifest.json",
                {"status": "STAGE4_COMPLETED", "qc_status": "QC_PASSED"},
            )
            rows = WorkflowScanner(project).scan()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["progress"], 80)
        self.assertEqual(rows[0]["overall"], "成片完成，等待投稿")

    def test_review_state_explains_layout_issue_count(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            (task / "subtitles").mkdir()
            (task / "subtitles" / "en.selected.srt").write_text("English", encoding="utf-8")
            (task / "subtitles" / "zh.clean.srt").write_text("中文", encoding="utf-8")
            mark_deepseek_translation(task)
            write_json(
                task / "stage4" / "stage4_manifest.json",
                {
                    "status": "REVIEW_REQUIRED",
                    "qc_status": "REVIEW_REQUIRED",
                    "warnings": [
                        "BILINGUAL_LINE_TOO_WIDE:4",
                        "BILINGUAL_LINE_TOO_WIDE:165",
                    ],
                },
            )
            row = WorkflowScanner(project).scan()[0]
        self.assertEqual(row["stages"]["render"]["state"], "review")
        self.assertEqual(row["review_summary"], "字幕排版异常 2 条，请勿投稿")
        self.assertEqual(row["stages"]["render"]["detail"], row["review_summary"])

    def test_unattended_skip_is_terminal_and_does_not_request_review(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            (task / "subtitles").mkdir()
            (task / "subtitles" / "en.selected.srt").write_text(
                "English", encoding="utf-8"
            )
            (task / "subtitles" / "zh.clean.srt").write_text(
                "中文", encoding="utf-8"
            )
            mark_deepseek_translation(task)
            write_json(
                task / "stage4" / "stage4_manifest.json",
                {
                    "status": "REVIEW_REQUIRED",
                    "qc_status": "REVIEW_REQUIRED",
                    "review": {
                        "code": "SUBTITLE_LAYOUT_REVIEW_REQUIRED",
                        "message": "1 条字幕需要复核",
                    },
                },
            )
            write_json(
                task / "stage5" / "automation_manifest.json",
                {
                    "status": "SKIPPED",
                    "reason": "SUBTITLE_LAYOUT_REVIEW_REQUIRED",
                },
            )
            row = WorkflowScanner(project).scan()[0]
        self.assertTrue(row["automation_skipped"])
        self.assertEqual(row["overall"], "无人值守已跳过此视频")
        self.assertEqual(row["progress"], 100)
        self.assertEqual(row["stages"]["render"]["state"], "skipped")
        self.assertEqual(row["stages"]["publish"]["state"], "skipped")
        self.assertEqual(row["review_summary"], "已自动跳过：字幕无法安全排版")

    def test_empty_audio_transcript_has_specific_unattended_skip_message(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            write_json(
                task / "stage3" / "01_source_assessment.json",
                {
                    "route": "NO_YOUTUBE_ENGLISH_SOURCE",
                    "status": "NO_YOUTUBE_ENGLISH_SOURCE",
                },
            )
            write_json(
                task / "stage3" / "whisper" / "asr_info.json",
                {"segment_count": 0, "word_count": 0},
            )
            write_json(
                task / "stage5" / "automation_manifest.json",
                {
                    "status": "SKIPPED",
                    "reason": "ENGLISH_SUBTITLE_STAGE_FAILED",
                },
            )
            row = WorkflowScanner(project).scan()[0]

        self.assertEqual(
            row["review_summary"],
            "已自动跳过：没有英文字幕，音轨也未识别到英语语音",
        )

    def test_published_manifest_completes_fifth_stage(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            (task / "subtitles").mkdir()
            (task / "subtitles" / "en.selected.srt").write_text("English", encoding="utf-8")
            (task / "subtitles" / "zh.clean.srt").write_text("中文", encoding="utf-8")
            mark_deepseek_translation(task)
            write_json(
                task / "stage4" / "stage4_manifest.json",
                {
                    "status": "REVIEW_REQUIRED",
                    "qc_status": "REVIEW_REQUIRED",
                    "review": {
                        "code": "SUBTITLE_LAYOUT_REVIEW_REQUIRED",
                        "message": "2 条字幕需要复核",
                    },
                },
            )
            write_json(
                task / "stage5" / "publish_manifest.json",
                {
                    "status": "PUBLISHED",
                    "bvid": "BV1xx411c7mD",
                    "url": "https://www.bilibili.com/video/BV1xx411c7mD",
                },
            )
            write_json(
                task / "stage5" / "automation_manifest.json",
                {
                    "status": "SKIPPED",
                    "reason": "SUBTITLE_LAYOUT_REVIEW_REQUIRED",
                },
            )
            row = WorkflowScanner(project).scan()[0]
        self.assertEqual(row["progress"], 100)
        self.assertEqual(row["overall"], "投稿完成")
        self.assertFalse(row["automation_skipped"])
        self.assertEqual(row["stages"]["render"]["state"], "complete")
        self.assertEqual(row["review_summary"], "")
        self.assertEqual(row["bvid"], "BV1xx411c7mD")

    def test_downloaded_auto_chinese_does_not_mark_deepseek_translation_complete(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            (task / "subtitles").mkdir()
            (task / "subtitles" / "zh.auto.srt").write_text("自动中文", encoding="utf-8")
            (task / "subtitles" / "zh.clean.srt").write_text(
                "旧版下载阶段清洗文件",
                encoding="utf-8",
            )
            row = WorkflowScanner(project).scan()[0]
        self.assertEqual(row["stages"]["translation"]["state"], "pending")
        self.assertTrue(row["chinese_auto_available"])
        self.assertEqual(row["chinese_auto_name"], "zh.auto.srt")

    def test_manual_chinese_track_is_not_reported_as_automatic(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            manifest = json.loads(
                (task / "download_manifest.json").read_text(encoding="utf-8")
            )
            manifest["subtitle_tracks"] = {
                "zh": {"status": "success", "source": "manual"}
            }
            write_json(task / "download_manifest.json", manifest)
            (task / "subtitles").mkdir()
            (task / "subtitles" / "zh.youtube.clean.srt").write_text(
                "人工中文",
                encoding="utf-8",
            )
            row = WorkflowScanner(project).scan()[0]
        self.assertFalse(row["chinese_auto_available"])

    def test_task_resolution_cannot_escape_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            scanner = WorkflowScanner(Path(name))
            with self.assertRaises(ValueError):
                scanner.resolve_task("../outside")


class QueueTests(TestCase):
    def test_publish_guard_recovers_cooldown_from_existing_137022_failure(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            config_path = project / "config" / "publish_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.update(
                {
                    "publish_min_interval_seconds": 0,
                    "publish_daily_limit": 0,
                    "publish_rate_limit_cooldown_seconds": 3600,
                }
            )
            write_json(config_path, config)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            now = datetime.now(timezone.utc)
            failed_at = now - timedelta(minutes=5)
            job = store.enqueue(
                "publish",
                "task",
                {},
                resource_class="upload",
            )
            store.update(
                job["id"],
                status="failed",
                exit_code=1,
                error="哔哩哔哩拒绝投稿（错误码 137022）",
                finished_at=failed_at.isoformat(),
            )
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            guard = worker.publish_guard(now)

        self.assertTrue(guard["active"])
        self.assertIn("已有 137022", guard["step"])
        self.assertEqual(
            guard["resume_at"],
            (failed_at + timedelta(hours=1)).isoformat(),
        )

    def test_publish_guard_enforces_interval_daily_limit_and_persists_state(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            config_path = project / "config" / "publish_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.update(
                {
                    "publish_min_interval_seconds": 900,
                    "publish_daily_limit": 2,
                    "publish_rate_limit_cooldown_seconds": 3600,
                }
            )
            write_json(config_path, config)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            now = datetime.now().astimezone().replace(
                hour=12,
                minute=0,
                second=0,
                microsecond=0,
            ).astimezone(timezone.utc)
            for index, finished_at in enumerate(
                (now - timedelta(minutes=20), now - timedelta(minutes=5))
            ):
                job = store.enqueue(
                    "publish",
                    f"task-{index}",
                    {},
                    resource_class="upload",
                )
                store.update(
                    job["id"],
                    status="completed",
                    progress=100,
                    exit_code=0,
                    finished_at=finished_at.isoformat(),
                )
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            guard = worker.publish_guard(now)
            cooldown_until = now + timedelta(hours=20)
            store.set_worker_state(
                worker.PUBLISH_COOLDOWN_STATE_KEY,
                cooldown_until.isoformat(),
            )
            restarted = WorkflowWorker(
                project,
                JobStore(project / "jobs.sqlite3", project / "logs"),
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            persisted = restarted.publish_guard(now)

        self.assertTrue(guard["active"])
        self.assertEqual(guard["completed_today"], 2)
        self.assertIn("今日已成功投稿 2/2", guard["step"])
        self.assertTrue(persisted["active"])
        self.assertEqual(persisted["resume_at"], cooldown_until.isoformat())
        self.assertIn("137022", persisted["step"])

    def test_legacy_job_database_migrates_resource_class_and_recovers_stage(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            database = root / "jobs.sqlite3"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """
                    CREATE TABLE jobs (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
                        payload_json TEXT NOT NULL, status TEXT NOT NULL, step TEXT NOT NULL,
                        progress INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
                        started_at TEXT NOT NULL DEFAULT '', finished_at TEXT NOT NULL DEFAULT '',
                        exit_code INTEGER, error TEXT NOT NULL DEFAULT '', log_path TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO jobs
                    (id, kind, target, payload_json, status, step, progress,
                     created_at, started_at, log_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy",
                        "pipeline",
                        "task-a",
                        json.dumps({"workflow": "complete", "_stage_index": 1}),
                        "running",
                        "翻译并检查中文字幕",
                        33,
                        "2026-08-11T00:00:00+00:00",
                        "2026-08-11T00:01:00+00:00",
                        str(root / "logs" / "legacy.log"),
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            store = JobStore(database, root / "logs")
            migrated = store.get("legacy")

        self.assertEqual(migrated["status"], "queued")
        self.assertEqual(migrated["resource_class"], "gpu_heavy")
        self.assertEqual(migrated["payload"]["_stage_index"], 1)

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

    def test_worker_pools_claim_only_their_kinds_and_lock_running_target(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            store = JobStore(root / "jobs.sqlite3", root / "logs")
            production = store.enqueue("pipeline", "task-a", {"workflow": "complete"})
            first = store.enqueue("download", "video-a", {"url": "https://youtu.be/abcdefghijk"})
            duplicate = store.enqueue("download", "video-a", {"url": "https://youtu.be/abcdefghijk"})
            second = store.enqueue("download", "video-b", {"url": "https://youtu.be/12345678901"})

            claimed_first = store.claim_next({"download"})
            claimed_second = store.claim_next({"download"})
            self.assertIsNone(store.claim_next({"pipeline"}, {"paid_api"}))
            claimed_production = store.claim_next(
                {"pipeline", "publish"}, {"gpu_heavy"}
            )
            blocked_duplicate = store.claim_next({"download"})

        self.assertEqual(claimed_first["id"], first["id"])
        self.assertEqual(claimed_second["id"], second["id"])
        self.assertEqual(claimed_production["id"], production["id"])
        self.assertIsNone(blocked_duplicate)
        self.assertEqual(duplicate["status"], "queued")

    def test_worker_runs_download_gpu_and_deepseek_slots_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            store.enqueue("download", "video-a", {"url": "https://youtu.be/abcdefghijk"})
            store.enqueue("download", "video-b", {"url": "https://youtu.be/12345678901"})
            store.enqueue(
                "pipeline",
                "task-a",
                {"workflow": "complete"},
                resource_class="gpu_heavy",
            )
            store.enqueue(
                "pipeline",
                "task-b",
                {"workflow": "complete", "_stage_index": 1},
                resource_class="paid_api",
            )
            started: list[tuple[str, str]] = []
            started_lock = threading.Lock()
            expected_started = threading.Event()
            release = threading.Event()

            def hold_job(job: dict) -> None:
                with started_lock:
                    started.append((str(job["kind"]), str(job["target"])))
                    if len(started) == 4:
                        expected_started.set()
                release.wait(timeout=5)

            with mock.patch.object(worker, "_execute", side_effect=hold_job):
                worker.start()
                worker.wake()
                try:
                    self.assertTrue(expected_started.wait(timeout=3))
                    with started_lock:
                        snapshot = list(started)
                    self.assertEqual(sum(kind == "download" for kind, _ in snapshot), 2)
                    self.assertEqual(sum(kind == "pipeline" for kind, _ in snapshot), 2)
                    self.assertEqual(len(snapshot), 4)
                finally:
                    release.set()
                    worker.close()

    def test_pipeline_requeues_between_gpu_deepseek_and_render_stages(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            make_runtime(project)
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            reference = task.relative_to(project / "downloads").as_posix()
            queued = store.enqueue(
                "pipeline",
                reference,
                {
                    "workflow": "complete",
                    "render_mode": "hardsub",
                    "chinese_subtitle_source": "deepseek",
                    "allow_paid_api": True,
                },
                resource_class="gpu_heavy",
            )
            with mock.patch.object(worker, "_run_command", return_value=0) as runner:
                english = store.claim_next({"pipeline"}, {"gpu_heavy"})
                worker._execute(english)
                after_english = store.get(queued["id"])

                translation = store.claim_next({"pipeline"}, {"paid_api"})
                worker._execute(translation)
                after_translation = store.get(queued["id"])

                render = store.claim_next({"pipeline"}, {"gpu_heavy"})
                worker._execute(render)
                completed = store.get(queued["id"])

        self.assertEqual(after_english["status"], "queued")
        self.assertEqual(after_english["resource_class"], "paid_api")
        self.assertEqual(after_english["payload"]["_stage_index"], 1)
        self.assertEqual(after_translation["status"], "queued")
        self.assertEqual(after_translation["resource_class"], "gpu_heavy")
        self.assertEqual(after_translation["payload"]["_stage_index"], 2)
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["progress"], 100)
        self.assertEqual(runner.call_count, 3)

    def test_unattended_pipeline_nonzero_exit_is_recorded_as_skip(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_runtime(project)
            make_publish_config(project)
            write_json(
                task / "stage4" / "stage4_manifest.json",
                {
                    "status": "FAILED",
                    "qc_status": "FAILED",
                    "errors": [
                        {
                            "code": "NO_VALID_CHINESE_SUBTITLE",
                            "message": "没有候选能通过严格一致性校验",
                        }
                    ],
                },
            )
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            reference = task.relative_to(project / "downloads").as_posix()
            queued = store.enqueue(
                "pipeline",
                reference,
                {
                    "workflow": "render",
                    "render_mode": "hardsub",
                    "chinese_subtitle_source": "deepseek",
                    "auto_publish": True,
                },
                resource_class="gpu_heavy",
            )
            claimed = store.claim_next({"pipeline"}, {"gpu_heavy"})
            with mock.patch.object(worker, "_run_command", return_value=2):
                worker._execute(claimed)
            completed = store.get(queued["id"])
            automation = json.loads(
                (task / "stage5" / "automation_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["progress"], 100)
        self.assertEqual(completed["exit_code"], 0)
        self.assertIn("已自动跳过此视频", completed["step"])
        self.assertEqual(automation["status"], "SKIPPED")
        self.assertEqual(automation["reason"], "NO_VALID_CHINESE_SUBTITLE")
        self.assertEqual(automation["details"]["process_exit_code"], 2)

    def test_unattended_no_speech_video_requeues_metadata_only_original_publish(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_runtime(project)
            make_publish_config(project)
            write_json(
                task / "stage3" / "01_source_assessment.json",
                {"route": "NO_YOUTUBE_ENGLISH_SOURCE"},
            )
            write_json(
                task / "stage3" / "whisper" / "asr_info.json",
                {"segment_count": 0, "word_count": 0},
            )
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            reference = task.relative_to(project / "downloads").as_posix()
            queued = store.enqueue(
                "pipeline",
                reference,
                {
                    "workflow": "complete",
                    "render_mode": "hardsub",
                    "chinese_subtitle_source": "auto",
                    "automation_enabled": True,
                    "automation_target": "publish",
                    "automation_silent_video_policy": "publish_original",
                    "publish_metadata_provider": "local_ollama",
                },
                resource_class="gpu_heavy",
            )
            claimed = store.claim_next({"pipeline"}, {"gpu_heavy"})
            with mock.patch.object(worker, "_run_command", return_value=1):
                worker._execute(claimed)
            rerouted = store.get(queued["id"])
            rerouted_commands = worker._build_commands(rerouted)
            automation = json.loads(
                (task / "stage5" / "automation_manifest.json").read_text(
                    encoding="utf-8"
                )
            )

        self.assertEqual(rerouted["status"], "queued")
        self.assertTrue(rerouted["payload"]["publish_original_video"])
        self.assertEqual(rerouted_commands[0][0], "生成无配音视频投稿信息")
        self.assertIn("--allow-no-subtitles", rerouted_commands[0][1])
        self.assertEqual(automation["status"], "ORIGINAL_MEDIA")

    def test_deepseek_pool_runs_two_different_videos_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            for index in range(3):
                store.enqueue(
                    "pipeline",
                    f"task-{index}",
                    {"workflow": "complete", "_stage_index": 1},
                    resource_class="paid_api",
                )
            started: list[str] = []
            lock = threading.Lock()
            two_started = threading.Event()
            release = threading.Event()

            def hold_job(job: dict) -> None:
                with lock:
                    started.append(str(job["target"]))
                    if len(started) == 2:
                        two_started.set()
                release.wait(timeout=5)

            with mock.patch.object(worker, "_execute", side_effect=hold_job):
                worker.start()
                worker.wake()
                try:
                    self.assertTrue(two_started.wait(timeout=3))
                    with lock:
                        self.assertEqual(len(started), 2)
                finally:
                    release.set()
                    worker.close()

    def test_pipeline_commands_use_existing_entrypoints_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            scanner = WorkflowScanner(project)
            make_runtime(project)
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
        self.assertEqual(commands[0][1][1:3], ["-m", "src.run_stage3"])
        self.assertIn("--resume", commands[0][1])
        self.assertEqual(commands[2][1][1:3], ["-m", "src.run_stage4"])
        self.assertIn("--chinese-source", commands[2][1])
        self.assertIn("deepseek", commands[2][1])

    def test_pipeline_command_can_disable_whisper_for_youtube_auto_english(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            make_runtime(project)
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            commands = worker._build_commands(
                {
                    "kind": "pipeline",
                    "target": task.relative_to(project / "downloads").as_posix(),
                    "payload": {
                        "workflow": "subtitles",
                        "whisper_for_auto_subtitles": False,
                    },
                }
            )
        self.assertIn("--no-whisper-for-auto-subtitles", commands[0][1])

    def test_pipeline_command_can_force_whisper_even_when_youtube_english_exists(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_runtime(project)
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                JobStore(project / "jobs.sqlite3", project / "logs"),
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            commands = worker._build_commands(
                {
                    "kind": "pipeline",
                    "target": task.relative_to(project / "downloads").as_posix(),
                    "payload": {
                        "workflow": "subtitles",
                        "english_subtitle_policy": "whisper",
                    },
                }
            )
        selection = commands[0][1]
        self.assertEqual(
            selection[selection.index("--subtitle-source") + 1],
            "whisper",
        )
        self.assertNotIn("--no-whisper-for-auto-subtitles", selection)

    def test_api_always_translates_even_when_youtube_chinese_exists(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            subtitles = task / "subtitles"
            subtitles.mkdir()
            (subtitles / "zh.manual.srt").write_text(
                "已有 YouTube 中文字幕",
                encoding="utf-8",
            )
            make_runtime(project)
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                JobStore(project / "jobs.sqlite3", project / "logs"),
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            commands = worker._build_commands(
                {
                    "kind": "pipeline",
                    "target": task.relative_to(project / "downloads").as_posix(),
                    "payload": {
                        "workflow": "complete",
                        "render_mode": "hardsub",
                        "chinese_subtitle_source": "deepseek",
                        "automation_enabled": True,
                        "automation_target": "render",
                        "automation_chinese_policy": "api_always",
                        "allow_paid_api": True,
                    },
                }
            )
        joined = " ".join(" ".join(command) for _label, command in commands)
        self.assertIn("--steps translate", joined)
        self.assertNotIn("--steps metadata", joined)
        self.assertEqual(
            commands[-1][1][commands[-1][1].index("--chinese-source") + 1],
            "deepseek",
        )

    def test_custom_render_target_persists_all_subtitle_policies(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_publish_config(project)
            app = ControlPanelApp(project)
            reference = task.relative_to(project / "downloads").as_posix()
            health = {
                "checks": {"translation_api": True},
                "llm": {"active": {"provider": "deepseek"}},
                "discovery": {"reachable": False, "model_ready": False},
            }
            try:
                with mock.patch.object(app, "health", return_value=health):
                    jobs = app.queue_pipeline(
                        tasks=[reference],
                        workflow="complete",
                        render_mode="softsub",
                        chinese_subtitle_source="auto",
                        allow_paid_api=True,
                        auto_publish=True,
                        automation_target="render",
                        english_subtitle_policy="whisper",
                        automation_chinese_policy="api_always",
                    )
            finally:
                app.close()
        payload = jobs[0]["payload"]
        self.assertTrue(payload["automation_enabled"])
        self.assertFalse(payload["auto_publish"])
        self.assertEqual(payload["automation_target"], "render")
        self.assertEqual(payload["english_subtitle_policy"], "whisper")
        self.assertEqual(payload["automation_chinese_policy"], "api_always")
        self.assertEqual(payload["chinese_subtitle_source"], "deepseek")
        self.assertEqual(payload["render_mode"], "softsub")

    def test_post_download_automation_preserves_whisper_quality_switch(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_runtime(project)
            make_publish_config(project)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            with mock.patch.object(
                worker,
                "_task_reference_for_video_id",
                return_value=task.relative_to(project / "downloads").as_posix(),
            ):
                worker._queue_post_download_automation(
                    {
                        "target": "abcdefghijk",
                        "payload": {
                            "auto_publish": True,
                            "auto_translate_missing": True,
                            "allow_paid_api": True,
                            "whisper_for_auto_subtitles": True,
                            "render_mode": "both",
                            "automation_failure_policy": "fail",
                        },
                    }
                )
            queued = next(job for job in store.list() if job["kind"] == "pipeline")
        self.assertTrue(queued["payload"]["whisper_for_auto_subtitles"])
        self.assertEqual(queued["payload"]["render_mode"], "both")
        self.assertEqual(queued["payload"]["automation_failure_policy"], "fail")

    def test_post_download_subtitle_target_stops_before_render_and_publish(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            subtitles = task / "subtitles"
            subtitles.mkdir()
            (subtitles / "zh.auto.srt").write_text("已有中文", encoding="utf-8")
            make_publish_config(project)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            with mock.patch.object(
                worker,
                "_task_reference_for_video_id",
                return_value=task.relative_to(project / "downloads").as_posix(),
            ):
                message = worker._queue_post_download_automation(
                    {
                        "target": "abcdefghijk",
                        "payload": {
                            "automation_enabled": True,
                            "automation_target": "subtitles",
                            "automation_chinese_policy": "youtube_only",
                            "auto_translate_missing": False,
                        },
                    }
                )
            queued = next(job for job in store.list() if job["kind"] == "pipeline")
        self.assertIn("双语字幕", message)
        self.assertEqual(queued["payload"]["workflow"], "subtitles")
        self.assertEqual(queued["payload"]["automation_target"], "subtitles")
        self.assertFalse(queued["payload"]["auto_publish"])

    def test_unattended_failure_policy_can_preserve_failure(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_runtime(project)
            make_publish_config(project)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            reference = task.relative_to(project / "downloads").as_posix()
            queued = store.enqueue(
                "pipeline",
                reference,
                {
                    "workflow": "render",
                    "render_mode": "hardsub",
                    "chinese_subtitle_source": "deepseek",
                    "auto_publish": True,
                    "automation_failure_policy": "fail",
                },
                resource_class="gpu_heavy",
            )
            claimed = store.claim_next({"pipeline"}, {"gpu_heavy"})
            with mock.patch.object(worker, "_run_command", return_value=2):
                worker._execute(claimed)
            failed = store.get(queued["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["exit_code"], 1)
        self.assertFalse((task / "stage5" / "automation_manifest.json").exists())

    def test_download_command_uses_package_module_entrypoint(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            make_runtime(project)
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            commands = worker._build_commands(
                {
                    "kind": "download",
                    "target": "abcdefghijk",
                    "payload": {"url": "https://youtu.be/abcdefghijk"},
                }
            )
        self.assertEqual(commands[0][1][1:3], ["-m", "src.download_video"])
        self.assertIn("--confirm-rights", commands[0][1])

    def test_download_worker_uses_and_cleans_an_isolated_cookie_copy(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            write_json(
                project / "config" / "download_config.json",
                {"use_cookies": True, "cookies_path": "private/cookies.txt"},
            )
            master = project / "private" / "cookies.txt"
            master.parent.mkdir(parents=True)
            master.write_text(YOUTUBE_COOKIES, encoding="utf-8")
            stale = project / "work" / "cookies" / "stale.txt"
            stale.parent.mkdir(parents=True)
            stale.write_text(YOUTUBE_COOKIES, encoding="utf-8")
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                JobStore(project / "jobs.sqlite3", project / "logs"),
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            self.assertFalse(stale.exists())
            isolated = worker._create_cookie_copy("job-a")
            self.assertIsNotNone(isolated)
            self.assertNotEqual(isolated, master)
            self.assertEqual(isolated.read_text(encoding="utf-8"), YOUTUBE_COOKIES)
            isolated.write_text("changed", encoding="utf-8")
            self.assertEqual(master.read_text(encoding="utf-8"), YOUTUBE_COOKIES)
            isolated.unlink()

    def test_youtube_auto_workflow_skips_paid_translation_and_renders_hardsub(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            make_runtime(project)
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            commands = worker._build_commands(
                {
                    "kind": "pipeline",
                    "target": task.relative_to(project / "downloads").as_posix(),
                    "payload": {
                        "workflow": "complete",
                        "render_mode": "hardsub",
                        "chinese_subtitle_source": "youtube_auto",
                    },
                }
            )
        self.assertEqual(len(commands), 2)
        self.assertNotIn("translate", " ".join(commands[0][1]))
        self.assertEqual(commands[-1][1][commands[-1][1].index("--mode") + 1], "hardsub")
        self.assertEqual(
            commands[-1][1][commands[-1][1].index("--chinese-source") + 1],
            "youtube_auto",
        )

    def test_queue_reports_when_youtube_auto_chinese_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_publish_config(project)
            app = ControlPanelApp(project)
            reference = task.relative_to(project / "downloads").as_posix()
            with self.assertRaisesRegex(ValueError, "没有自动生成的中文字幕"):
                app.queue_pipeline(
                    tasks=[reference],
                    workflow="complete",
                    render_mode="hardsub",
                    chinese_subtitle_source="youtube_auto",
                    allow_paid_api=False,
                )
            app.close()

    def test_youtube_auto_queue_does_not_require_paid_api_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            (task / "subtitles").mkdir()
            (task / "subtitles" / "zh.auto.srt").write_text(
                "自动中文",
                encoding="utf-8",
            )
            make_publish_config(project)
            app = ControlPanelApp(project)
            reference = task.relative_to(project / "downloads").as_posix()
            jobs = app.queue_pipeline(
                tasks=[reference],
                workflow="complete",
                render_mode="hardsub",
                chinese_subtitle_source="youtube_auto",
                allow_paid_api=False,
            )
            app.close()
        self.assertEqual(jobs[0]["payload"]["chinese_subtitle_source"], "youtube_auto")
        self.assertTrue(jobs[0]["payload"]["whisper_for_auto_subtitles"])

    def test_pipeline_queue_persists_disabled_whisper_option(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            (task / "subtitles").mkdir()
            (task / "subtitles" / "zh.auto.srt").write_text("自动中文", encoding="utf-8")
            make_publish_config(project)
            app = ControlPanelApp(project)
            reference = task.relative_to(project / "downloads").as_posix()
            jobs = app.queue_pipeline(
                tasks=[reference],
                workflow="complete",
                render_mode="hardsub",
                chinese_subtitle_source="youtube_auto",
                allow_paid_api=False,
                whisper_for_auto_subtitles=False,
            )
            app.close()
        self.assertFalse(jobs[0]["payload"]["whisper_for_auto_subtitles"])

    def test_unattended_routing_uses_manual_youtube_chinese_without_translation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            subtitles = task / "subtitles"
            subtitles.mkdir()
            manual = subtitles / "zh.manual.srt"
            manual.write_text("中文字幕", encoding="utf-8")
            make_runtime(project)
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                JobStore(project / "jobs.sqlite3", project / "logs"),
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            commands = worker._build_commands(
                {
                    "kind": "pipeline",
                    "target": task.relative_to(project / "downloads").as_posix(),
                    "payload": {
                        "workflow": "complete",
                        "render_mode": "hardsub",
                        "chinese_subtitle_source": "auto",
                        "auto_translate_missing": True,
                        "auto_publish": True,
                        "publish_metadata_provider": "local_ollama",
                    },
                }
            )
            selected_chinese = youtube_chinese_path(task)
        self.assertEqual(selected_chinese, manual)
        self.assertEqual(len(commands), 3)
        self.assertNotIn("--steps translate", " ".join(" ".join(row[1]) for row in commands))
        self.assertIn("--steps metadata", " ".join(" ".join(row[1]) for row in commands))
        self.assertEqual(
            commands[-1][1][commands[-1][1].index("--chinese-source") + 1],
            "auto",
        )

    def test_unattended_routing_translates_when_youtube_chinese_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_runtime(project)
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                JobStore(project / "jobs.sqlite3", project / "logs"),
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            commands = worker._build_commands(
                {
                    "kind": "pipeline",
                    "target": task.relative_to(project / "downloads").as_posix(),
                    "payload": {
                        "workflow": "complete",
                        "render_mode": "hardsub",
                        "chinese_subtitle_source": "auto",
                        "auto_translate_missing": True,
                        "auto_publish": True,
                        "publish_metadata_provider": "translation_api",
                    },
                }
            )
        joined = " ".join(" ".join(row[1]) for row in commands)
        self.assertIn("--steps translate", joined)
        self.assertNotIn("--steps metadata", joined)
        self.assertEqual(
            commands[-1][1][commands[-1][1].index("--chinese-source") + 1],
            "deepseek",
        )

    def test_unattended_routing_translates_after_youtube_chinese_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            subtitles = task / "subtitles"
            subtitles.mkdir()
            (subtitles / "zh.manual.srt").write_text("时间轴不匹配", encoding="utf-8")
            write_json(
                task / "stage4" / "stage4_manifest.json",
                {
                    "status": "FAILED",
                    "errors": [
                        {
                            "code": "NO_VALID_CHINESE_SUBTITLE",
                            "message": "YouTube 中文无法与英文对齐",
                        }
                    ],
                },
            )
            make_runtime(project)
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                JobStore(project / "jobs.sqlite3", project / "logs"),
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            commands = worker._build_commands(
                {
                    "kind": "pipeline",
                    "target": task.relative_to(project / "downloads").as_posix(),
                    "payload": {
                        "workflow": "complete",
                        "render_mode": "hardsub",
                        "chinese_subtitle_source": "auto",
                        "auto_translate_missing": True,
                        "allow_paid_api": True,
                        "auto_publish": True,
                        "publish_metadata_provider": "translation_api",
                    },
                }
            )

        joined = " ".join(" ".join(row[1]) for row in commands)
        self.assertIn("--steps translate", joined)
        self.assertEqual(
            commands[-1][1][commands[-1][1].index("--chinese-source") + 1],
            "deepseek",
        )

    def test_unattended_render_failure_requeues_api_translation_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            write_json(
                task / "stage4" / "stage4_manifest.json",
                {
                    "status": "FAILED",
                    "errors": [{"code": "NO_VALID_CHINESE_SUBTITLE"}],
                },
            )
            make_publish_config(project)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            reference = task.relative_to(project / "downloads").as_posix()
            queued = store.enqueue(
                "pipeline",
                reference,
                {
                    "workflow": "complete",
                    "render_mode": "hardsub",
                    "chinese_subtitle_source": "auto",
                    "auto_translate_missing": True,
                    "allow_paid_api": True,
                    "auto_publish": True,
                },
                resource_class="gpu_heavy",
            )
            running = store.claim_next({"pipeline"}, {"gpu_heavy"})
            retried = worker._retry_unusable_youtube_chinese_with_api(
                running,
                label="生成并质检双语成片",
                exit_code=2,
                log_path=Path(running["log_path"]),
            )
            saved = store.get(queued["id"])

        self.assertTrue(retried)
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["resource_class"], "paid_api")
        self.assertEqual(saved["payload"]["chinese_subtitle_source"], "deepseek")
        self.assertEqual(saved["payload"]["_stage_index"], 1)

    def test_unattended_publish_ignores_only_manual_review_marker(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_publish_config(project)
            write_json(
                task / "stage3" / "publish_metadata.json",
                {
                    "status": "RECOMMENDED",
                    "title_zh": "可靠的软件系统",
                    "tags": "软件工程,系统设计,编程",
                    "tid": 231,
                },
            )
            media = task / "stage4" / "video" / "final_bilingual_hardsub.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            write_json(
                task / "stage4" / "stage4_manifest.json",
                {"status": "REVIEW_REQUIRED", "qc_status": "REVIEW_REQUIRED"},
            )
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            publisher = BiliupIntegration(project)
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                publisher,
            )
            message = worker._queue_automatic_publish(
                {
                    "target": task.relative_to(project / "downloads").as_posix(),
                    "payload": {
                        "account_id": publisher.accounts()[0]["id"],
                    },
                }
            )
            publish_jobs = [job for job in store.list() if job["kind"] == "publish"]
            publish_command = publisher.build_upload_command(
                task,
                publish_jobs[0]["payload"],
            )
        self.assertIn("已自动加入投稿队列", message)
        self.assertEqual(len(publish_jobs), 1)
        self.assertTrue(publish_jobs[0]["payload"]["automatic"])
        self.assertFalse(publish_jobs[0]["payload"]["is_only_self"])
        self.assertNotIn("--is-only-self", publish_command)

    def test_unattended_original_media_publish_does_not_require_stage4(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_publish_config(project)
            source = task / "video" / "source.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"original-video")
            write_json(
                task / "stage3" / "publish_metadata.json",
                {
                    "status": "RECOMMENDED",
                    "title_zh": "静音园艺种植演示",
                    "tags": "园艺,种植",
                    "tid": 21,
                },
            )
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            publisher = BiliupIntegration(project)
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                publisher,
            )
            message = worker._queue_automatic_publish(
                {
                    "target": task.relative_to(project / "downloads").as_posix(),
                    "payload": {
                        "account_id": publisher.accounts()[0]["id"],
                        "publish_original_video": True,
                        "publish_only_self": False,
                    },
                }
            )
            publish_job = next(job for job in store.list() if job["kind"] == "publish")
            command = publisher.build_upload_command(task, publish_job["payload"])

        self.assertIn("原视频加入投稿队列", message)
        self.assertTrue(publish_job["payload"]["publish_original_video"])
        self.assertEqual(command[-1], str(source))

    def test_unattended_publish_skips_layout_block_even_if_stale_media_exists(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_publish_config(project)
            media = task / "stage4" / "video" / "final_bilingual_hardsub.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"stale-video")
            write_json(
                task / "stage4" / "stage4_manifest.json",
                {
                    "status": "REVIEW_REQUIRED",
                    "qc_status": "REVIEW_REQUIRED",
                    "review": {"render_blocked_before_ffmpeg": True},
                },
            )
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            message = worker._queue_automatic_publish(
                {
                    "target": task.relative_to(project / "downloads").as_posix(),
                    "payload": {},
                }
            )
            automation = json.loads(
                (task / "stage5" / "automation_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            has_publish_job = any(job["kind"] == "publish" for job in store.list())
        self.assertIn("已自动跳过", message)
        self.assertFalse(has_publish_job)
        self.assertEqual(automation["reason"], "SUBTITLE_LAYOUT_REVIEW_REQUIRED")

    def test_queued_job_can_be_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            job = store.enqueue("download", "abcdefghijk", {"url": "https://youtu.be/abcdefghijk"})
            cancelled = worker.cancel(job["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["step"], "已取消")

    def test_running_job_termination_targets_current_process(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            make_publish_config(project)
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            job = store.enqueue("download", "abcdefghijk", {"url": "https://youtu.be/abcdefghijk"})
            running = store.claim_next()
            process = mock.Mock()
            process.poll.return_value = None
            unrelated_process = mock.Mock()
            unrelated_process.poll.return_value = None
            with worker._process_lock:
                worker._processes[running["id"]] = process
                worker._processes["another-running-job"] = unrelated_process
            with mock.patch.object(worker, "_terminate_process_tree") as terminate:
                result = worker.cancel(running["id"])
            terminate.assert_called_once_with(process)
            self.assertNotEqual(terminate.call_args.args[0], unrelated_process)
            self.assertEqual(result["step"], "正在终止")
            with self.assertRaises(JobCancelled):
                worker._raise_if_cancelled(running["id"])

    def test_log_cleanup_removes_inactive_history_and_skips_active_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            store = JobStore(root / "jobs.sqlite3", root / "logs")
            finished = store.enqueue("download", "finished", {"url": "https://youtu.be/abcdefghijk"})
            active = store.enqueue("download", "active", {"url": "https://youtu.be/12345678901"})
            Path(finished["log_path"]).write_text("finished log", encoding="utf-8")
            Path(active["log_path"]).write_text("active log", encoding="utf-8")
            orphan = root / "logs" / "orphan.log"
            orphan.write_text("orphan log", encoding="utf-8")
            store.update(finished["id"], status="completed")
            store.claim_next()
            summary = store.clear_inactive_logs()
            self.assertFalse(Path(finished["log_path"]).exists())
            self.assertFalse(orphan.exists())
            self.assertTrue(Path(active["log_path"]).exists())
            with self.assertRaises(KeyError):
                store.get(finished["id"])
            self.assertEqual(store.get(active["id"])["status"], "running")
        self.assertEqual(summary["deleted"], 2)
        self.assertEqual(summary["deleted_logs"], 2)
        self.assertEqual(summary["deleted_jobs"], 1)
        self.assertEqual(summary["skipped_active"], 1)

    def test_log_cleanup_removes_inactive_history_without_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            store = JobStore(root / "jobs.sqlite3", root / "logs")
            finished = store.enqueue("download", "finished", {"url": "https://youtu.be/abcdefghijk"})
            store.update(finished["id"], status="completed")
            summary = store.clear_inactive_logs()
            with self.assertRaises(KeyError):
                store.get(finished["id"])
        self.assertEqual(summary["deleted_logs"], 0)
        self.assertEqual(summary["deleted_jobs"], 1)


class DestructiveActionTests(TestCase):
    def test_delete_task_removes_files_related_history_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            (task / "video").mkdir()
            (task / "video" / "source.mp4").write_bytes(b"video")
            make_publish_config(project)
            app = ControlPanelApp(project)
            reference = task.relative_to(project / "downloads").as_posix()
            job = app.store.enqueue("download", "abcdefghijk", {"url": "https://youtu.be/abcdefghijk"})
            Path(job["log_path"]).write_text("download log", encoding="utf-8")
            app.store.update(job["id"], status="completed")
            result = app.delete_task(reference, reference)
            self.assertFalse(task.exists())
            self.assertFalse(Path(job["log_path"]).exists())
            with self.assertRaises(KeyError):
                app.store.get(job["id"])
            app.close()
        self.assertTrue(result["deleted"])
        self.assertEqual(result["video_id"], "abcdefghijk")
        self.assertGreaterEqual(result["files"], 3)

    def test_delete_task_rejects_active_related_job(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_publish_config(project)
            app = ControlPanelApp(project)
            reference = task.relative_to(project / "downloads").as_posix()
            app.store.enqueue("download", "abcdefghijk", {"url": "https://youtu.be/abcdefghijk"})
            with self.assertRaisesRegex(ValueError, "先终止"):
                app.delete_task(reference, reference)
            self.assertTrue(task.is_dir())
            app.close()

    def test_delete_task_requires_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_publish_config(project)
            app = ControlPanelApp(project)
            reference = task.relative_to(project / "downloads").as_posix()
            with self.assertRaisesRegex(ValueError, "确认不匹配"):
                app.delete_task(reference, "wrong")
            self.assertTrue(task.is_dir())
            app.close()

    def test_batch_delete_removes_multiple_tasks_with_one_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            first = make_task(project, "2026-07-26/firstvideo1_First")
            second = make_task(project, "2026-07-26/secondvideo2_Second")
            write_json(first / "download_manifest.json", {"video_id": "firstvideo1"})
            write_json(second / "download_manifest.json", {"video_id": "secondvideo2"})
            make_publish_config(project)
            app = ControlPanelApp(project)
            references = [
                first.relative_to(project / "downloads").as_posix(),
                second.relative_to(project / "downloads").as_posix(),
            ]
            result = app.delete_tasks(
                [references[0], references[1], references[0]],
                "删除 2 个项目",
            )
            first_exists = first.exists()
            second_exists = second.exists()
            app.close()
        self.assertEqual(result["requested"], 2)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["failed"], 0)
        self.assertFalse(first_exists)
        self.assertFalse(second_exists)

    def test_batch_delete_skips_active_task_and_deletes_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            active = make_task(project, "2026-07-26/activevideo1_Active")
            ready = make_task(project, "2026-07-26/readyvideo22_Ready")
            write_json(active / "download_manifest.json", {"video_id": "activevideo1"})
            write_json(ready / "download_manifest.json", {"video_id": "readyvideo22"})
            make_publish_config(project)
            app = ControlPanelApp(project)
            active_reference = active.relative_to(project / "downloads").as_posix()
            ready_reference = ready.relative_to(project / "downloads").as_posix()
            app.store.enqueue("pipeline", active_reference, {"workflow": "complete"})
            result = app.delete_tasks(
                [active_reference, ready_reference],
                "删除 2 个项目",
            )
            active_exists = active.exists()
            ready_exists = ready.exists()
            app.close()
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["failed"], 1)
        self.assertTrue(active_exists)
        self.assertFalse(ready_exists)
        self.assertIn("先终止", result["failures"][0]["error"])

    def test_batch_delete_requires_counted_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task = make_task(project)
            make_publish_config(project)
            app = ControlPanelApp(project)
            reference = task.relative_to(project / "downloads").as_posix()
            with self.assertRaisesRegex(ValueError, "删除 1 个项目"):
                app.delete_tasks([reference], "删除")
            task_exists = task.exists()
            app.close()
        self.assertTrue(task_exists)

    def test_batch_delete_accepts_unspaced_fullwidth_count_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            references = []
            for index in range(9):
                task = make_task(
                    project,
                    f"2026-07-26/batchitem{index:02d}_Item_{index + 1}",
                )
                references.append(task.relative_to(project / "downloads").as_posix())
            app = ControlPanelApp(project)
            result = app.delete_tasks(references, "删除９个项目")
            remaining = [
                reference
                for reference in references
                if (project / "downloads" / reference).exists()
            ]
            app.close()
        self.assertEqual(result["requested"], 9)
        self.assertEqual(result["deleted"], 9)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(remaining, [])


class PublishingTests(TestCase):
    def test_no_speech_original_video_can_be_published_without_hardsub(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            task = make_task(project)
            source = task / "video" / "source.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"original-video")
            write_json(
                task / "stage3" / "01_source_assessment.json",
                {"route": "NO_YOUTUBE_ENGLISH_SOURCE"},
            )
            write_json(
                task / "stage3" / "whisper" / "asr_info.json",
                {"segment_count": 0, "word_count": 0},
            )
            write_json(
                task / "stage3" / "publish_metadata.json",
                {
                    "status": "RECOMMENDED",
                    "title_zh": "鸡蛋种植西瓜实验",
                    "tags": "中英双语,中文翻译,园艺,种植",
                    "tid": 21,
                },
            )
            publishing = BiliupIntegration(project)
            defaults = publishing.defaults(task)
            payload = publishing.validate_submission(
                task,
                defaults | {"confirm_publish": True, "is_only_self": False},
            )
            command = publishing.build_upload_command(task, payload)

        self.assertTrue(payload["publish_original_video"])
        self.assertFalse(payload["prepare_hardsub"])
        self.assertTrue(payload["title"].startswith("【无配音】"))
        self.assertIn("无配音", payload["tags"])
        self.assertNotIn("中英双语", payload["tags"])
        self.assertNotIn("中文翻译", payload["tags"])
        self.assertNotIn("--is-only-self", command)
        self.assertEqual(command[-1], str(source))

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
            mark_deepseek_translation(task)
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

    def test_automatic_submission_accepts_generated_title_with_emoji(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            task = make_task(project)
            original_title = (
                "#React test# 🤣🤣LADY FINGER CHIPS | Fried Lady Finger "
                "Recipe Cooking in Village | Okra Recipe"
            )
            write_json(
                task / "download_manifest.json",
                {
                    "video_id": "abcdefghijk",
                    "title": original_title,
                    "channel": "Test channel",
                    "overall_status": "success",
                    "errors": [],
                },
            )
            write_json(
                task / "metadata" / "info.json",
                {"id": "abcdefghijk", "title": original_title, "duration": 123},
            )
            write_json(
                task / "stage3" / "publish_metadata.json",
                {
                    "status": "RECOMMENDED",
                    "title_zh": "印度乡村制作炸秋葵片条食谱",
                    "tags": "中英双语,中文翻译,美食制作",
                    "tid": 76,
                },
            )
            media = task / "stage4" / "video" / "final_bilingual_hardsub.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            publishing = BiliupIntegration(project)
            payload = publishing.automatic_submission(
                task,
                account_id=publishing.accounts()[0]["id"],
                is_only_self=False,
            )

        self.assertLessEqual(utf16_code_units(payload["title"]), 80)
        self.assertIn("🤣🤣", payload["title"])
        self.assertNotIn("中英双语", payload["tags"])
        self.assertNotIn("中文翻译", payload["tags"])
        self.assertFalse(payload["is_only_self"])

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
            mark_deepseek_translation(task)
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

    def test_uploader_log_redacts_session_query_values(self) -> None:
        log = (
            "https://passport.bilibili.com/info?access_key=secret-value"
            "&sign=deadbeef&refresh_token%3Dencoded-secret "
            "SESSDATA:colon-secret&ts=1"
        )
        redacted = BiliupIntegration.redact_log_text(log)
        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("deadbeef", redacted)
        self.assertNotIn("encoded-secret", redacted)
        self.assertNotIn("colon-secret", redacted)
        self.assertIn("access_key=<redacted>", redacted)
        self.assertIn("sign=<redacted>", redacted)
        self.assertIn("refresh_token%3D<redacted>", redacted)
        self.assertIn("SESSDATA:<redacted>", redacted)

    def test_transient_tls_failure_is_actionable(self) -> None:
        log = "peer closed connection without sending TLS close_notify"
        self.assertTrue(BiliupIntegration.is_transient_upload_failure(log))
        self.assertIn(
            "自动重试",
            BiliupIntegration.explain_upload_failure(log, 1),
        )

    def test_preupload_tls_failure_is_safe_to_retry(self) -> None:
        self.assertTrue(
            BiliupIntegration.failed_upload_is_safe_to_retry(
                "error sending request for url https://member.bilibili.com/preupload?x=1 "
                "tls handshake eof"
            )
        )

    def test_rate_limit_rejection_is_safe_to_retry_after_cooldown(self) -> None:
        log = (
            'ResponseData { code: 137022, data: None, '
            'message: "投稿过于频繁，请稍后再试", ttl: Some(1) }'
        )
        self.assertTrue(BiliupIntegration.is_publish_rate_limited(log))
        self.assertTrue(BiliupIntegration.failed_upload_is_safe_to_retry(log))

    def test_latest_submission_response_controls_retry_classification(self) -> None:
        log = (
            'ResponseData { code: 137022, data: None, message: "投稿过于频繁" }\n'
            '===== 用户请求重试 =====\n'
            'ResponseData { code: 21010, data: None, message: "简介字数过长" }'
        )
        self.assertFalse(BiliupIntegration.is_publish_rate_limited(log))
        self.assertIn("简介字数过长", BiliupIntegration.explain_upload_failure(log, 1))

    def test_rate_limited_publish_is_requeued_and_cools_all_uploads(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            config_path = project / "config" / "publish_config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config.update(
                {
                    "publish_min_interval_seconds": 0,
                    "publish_daily_limit": 0,
                    "publish_rate_limit_cooldown_seconds": 3600,
                }
            )
            write_json(config_path, config)
            task = make_task(project)
            media = task / "stage4" / "video" / "final_bilingual_hardsub.mp4"
            media.parent.mkdir(parents=True)
            media.write_bytes(b"video")
            write_json(
                task / "stage3" / "publish_metadata.json",
                {
                    "status": "RECOMMENDED",
                    "title_zh": "测试视频",
                    "tags": "测试,软件工程,系统设计",
                    "tid": 21,
                },
            )
            publisher = BiliupIntegration(project)
            payload = publisher.automatic_submission(
                task,
                account_id=publisher.accounts()[0]["id"],
            )
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            target = task.relative_to(project / "downloads").as_posix()
            queued = store.enqueue(
                "publish",
                target,
                payload,
                resource_class="upload",
            )
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                publisher,
            )
            running = store.claim_next({"publish"}, {"upload"})

            def reject_rate_limit(_job_id: str, _command: list[str], path: Path) -> int:
                worker._append_log(
                    path,
                    '\nResponseData { code: 137022, data: None, '
                    'message: "投稿过于频繁，请稍后再试", ttl: Some(1) }\n',
                )
                return 1

            with mock.patch.object(
                worker,
                "_run_publish_upload_with_retries",
                side_effect=reject_rate_limit,
            ):
                worker._execute(running)
            deferred = store.get(queued["id"])
            manifest = json.loads(
                (task / "stage5" / "publish_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            guard = worker.publish_guard()

        self.assertEqual(deferred["status"], "queued")
        self.assertIn("投稿保护", deferred["step"])
        self.assertTrue(guard["active"])
        self.assertEqual(manifest["status"], "WAITING")
        self.assertIn("137022", manifest["wait_reason"])

    def test_upload_environment_bypasses_system_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            publishing = BiliupIntegration(project)
            environment = {
                "HTTP_PROXY": "http://127.0.0.1:7897",
                "HTTPS_PROXY": "http://127.0.0.1:7897",
            }
            publishing.configure_upload_environment(environment)
        self.assertNotIn("HTTP_PROXY", environment)
        self.assertNotIn("HTTPS_PROXY", environment)
        self.assertEqual(environment["NO_PROXY"], "*")
        self.assertEqual(environment["no_proxy"], "*")
        self.assertFalse(
            BiliupIntegration.failed_upload_is_safe_to_retry(
                "BV1abcdefghij submit success then tls handshake eof"
            )
        )

    def test_publish_worker_retries_tls_failure_on_fallback_line(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            make_publish_config(project)
            store = JobStore(project / "jobs.sqlite3", project / "logs")
            worker = WorkflowWorker(
                project,
                store,
                WorkflowScanner(project),
                BiliupIntegration(project),
            )
            queued = store.enqueue("publish", "task", {}, resource_class="upload")
            running = store.claim_next({"publish"}, {"upload"})
            self.assertEqual(running["id"], queued["id"])
            log_path = Path(running["log_path"])
            command = ["biliup.exe", "upload", "video.mp4"]
            attempts: list[list[str]] = []

            def fake_run(_job_id: str, current: list[str], path: Path) -> int:
                attempts.append(current)
                if len(attempts) == 1:
                    worker._append_log(path, "tls handshake eof\n")
                    return 1
                return 0

            with (
                mock.patch.object(
                    worker.publisher,
                    "transient_retry_delays",
                    return_value=[0],
                ),
                mock.patch.object(worker, "_run_command", side_effect=fake_run),
            ):
                result = worker._run_publish_upload_with_retries(
                    queued["id"],
                    command,
                    log_path,
                )
        self.assertEqual(result, 0)
        self.assertEqual(len(attempts), 2)
        self.assertIn("--line", attempts[1])
        self.assertIn("bldsa", attempts[1])

    def test_worker_decodes_windows_gbk_output(self) -> None:
        text = "简介字数过长，请缩减内容"
        self.assertEqual(
            WorkflowWorker._decode_process_output(text.encode("gb18030")),
            text,
        )


class RenderReviewFlowTests(TestCase):
    def test_review_save_preflights_and_queues_render_without_touching_sources(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task, english_original, chinese_original = make_layout_review_task(project)
            make_publish_config(project)
            app = ControlPanelApp(project)
            relative = task.relative_to(project / "downloads").as_posix()
            english_path = task / "subtitles" / "en.selected.srt"
            chinese_path = task / "subtitles" / "zh.clean.srt"
            try:
                review = app.render_review(relative)
                self.assertEqual(review["rows"][0]["id"], "1")
                with mock.patch.object(
                    app,
                    "queue_pipeline",
                    return_value=[{"id": "render-job"}],
                ) as queue:
                    result = app.save_render_review(
                        task=relative,
                        edits=[
                            {
                                "id": "1",
                                "english": "Close Blender?",
                                "chinese": "要关闭 Blender 吗？",
                            }
                        ],
                        render_mode="hardsub",
                    )
                self.assertEqual(result["job"]["id"], "render-job")
                self.assertTrue(result["review"]["ready_to_render"])
                queue.assert_called_once_with(
                    tasks=[relative],
                    workflow="render",
                    render_mode="hardsub",
                    chinese_subtitle_source="deepseek",
                    allow_paid_api=False,
                )
            finally:
                app.close()
            self.assertIn(english_original, english_path.read_text(encoding="utf-8"))
            self.assertIn(chinese_original, chinese_path.read_text(encoding="utf-8"))
            self.assertTrue(
                (task / "stage4" / "subtitles" / "en.layout_reviewed.srt").is_file()
            )
            self.assertTrue(
                (task / "stage4" / "subtitles" / "zh.layout_reviewed.srt").is_file()
            )

    def test_review_does_not_queue_while_layout_is_still_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task, english_original, chinese_original = make_layout_review_task(project)
            make_publish_config(project)
            app = ControlPanelApp(project)
            relative = task.relative_to(project / "downloads").as_posix()
            try:
                with mock.patch.object(app, "queue_pipeline") as queue:
                    result = app.save_render_review(
                        task=relative,
                        edits=[
                            {
                                "id": "1",
                                "english": english_original,
                                "chinese": chinese_original,
                            }
                        ],
                        render_mode="hardsub",
                    )
                self.assertIsNone(result["job"])
                self.assertFalse(result["review"]["ready_to_render"])
                self.assertGreater(result["review"]["remaining_issue_count"], 0)
                queue.assert_not_called()
            finally:
                app.close()

    def test_review_can_hide_problem_cue_and_queue_render(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            project = Path(name)
            task, english_original, chinese_original = make_layout_review_task(project)
            make_publish_config(project)
            app = ControlPanelApp(project)
            relative = task.relative_to(project / "downloads").as_posix()
            try:
                with mock.patch.object(
                    app,
                    "queue_pipeline",
                    return_value=[{"id": "render-job"}],
                ) as queue:
                    result = app.save_render_review(
                        task=relative,
                        edits=[{"id": "1", "hidden_from_render": True}],
                        render_mode="hardsub",
                    )
                self.assertEqual(result["job"]["id"], "render-job")
                self.assertEqual(result["review"]["hidden_count"], 1)
                queue.assert_called_once()
            finally:
                app.close()
            self.assertIn(
                english_original,
                (task / "subtitles" / "en.selected.srt").read_text(encoding="utf-8"),
            )
            self.assertIn(
                chinese_original,
                (task / "subtitles" / "zh.clean.srt").read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                english_original,
                (task / "stage4" / "subtitles" / "en.layout_reviewed.srt").read_text(
                    encoding="utf-8"
                ),
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
                settings_request = urllib.request.Request(
                    f"{base}/api/settings",
                    data=json.dumps({"youtube_api_key": "server-test-key"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(settings_request, timeout=5) as response:
                    settings_payload = json.loads(response.read().decode("utf-8"))
                cookies_request = urllib.request.Request(
                    f"{base}/api/youtube/cookies",
                    data=json.dumps(
                        {"action": "save", "content": YOUTUBE_COOKIES}
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(cookies_request, timeout=5) as response:
                    cookies_payload = json.loads(response.read().decode("utf-8"))
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
        self.assertEqual(payload["scheduler"]["mode"], "stage_pipeline_v0.5")
        self.assertEqual(
            payload["scheduler"]["resources"]["paid_api"]["capacity"],
            2,
        )
        self.assertEqual(settings_payload["saved"], ["YOUTUBE_API_KEY"])
        self.assertTrue(payload["health"]["checks"]["youtube_api"])
        self.assertTrue(payload["health"]["checks"]["youtube_cookies"])
        self.assertNotIn("server-test-key", json.dumps(settings_payload))
        self.assertNotIn("cookie-secret", json.dumps(cookies_payload))
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
