from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from PIL import Image

from src import cover_localization as covers
from src.control_panel.app import ControlPanelApp
from src.control_panel.jobs import WorkflowWorker
from src.control_panel.publishing import BiliupIntegration
from src.control_panel.settings import update_env_file
from tests.test_control_panel import make_publish_config, make_runtime, make_task
from tests.test_cover_localization import analysis_result


class ApiPillowCoverTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_runtime(self.root)
        make_publish_config(self.root)
        self.task = make_task(self.root)
        self.original = self.task / "metadata" / "thumbnail.jpg"
        Image.new("RGB", (960, 540), "navy").save(self.original)
        self.original_bytes = self.original.read_bytes()
        covers.update_cover_settings(self.root, {"cover_mode": "cloud"})
        update_env_file(self.root / ".env", {
            "TRANSLATION_PROVIDER": "zhipu",
            "TRANSLATION_MODEL": "glm-4.7-flash",
            "ZHIPU_API_KEY": "offline-fixture-key",
        })
        self.copy = mock.patch.object(
            covers, "_cloud_copy", return_value=analysis_result()
        ).start()
        self.local = mock.patch.object(
            covers, "_local_vision", side_effect=AssertionError("no Ollama")
        ).start()
        self.addCleanup(mock.patch.stopall)

    def run_cover(self, phase: str = "all", **kwargs):
        return covers.CoverLocalizer(
            self.task,
            project_root=self.root,
            mode="cloud",
            allow_cloud_api=True,
            **kwargs,
        ).run(phase)

    def test_api_copy_is_drawn_locally_with_large_chinese(self):
        result = self.run_cover()
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["selected_copy"], "实验结果揭晓")
        self.assertEqual(result["model"]["image_renderer"], "pillow")
        self.assertGreaterEqual(result["layout"]["layout"]["font_size"], round(540 * 0.09))
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        output = covers.localized_cover_path(self.task, self.root)
        self.assertTrue(output.is_file())
        self.assertEqual(BiliupIntegration(self.root).cover_path(self.task), output)
        self.copy.assert_called_once()
        self.local.assert_not_called()
        log = (self.task / "cover" / "cover_localization.log").read_text(encoding="utf-8")
        self.assertIn("未调用图片生成或图片编辑 API", log)

    def test_api_receives_no_original_image_or_subtitles(self):
        self.run_cover()
        args = self.copy.call_args.args
        self.assertEqual(args[3], [])
        self.assertNotIn(str(self.original), repr(self.copy.call_args))

    def test_separate_phases_call_api_once_and_finalize_in_pillow(self):
        self.assertEqual(self.run_cover("analyze")["status"], "ANALYZED")
        self.assertEqual(self.run_cover("finalize")["status"], "COMPLETED")
        self.copy.assert_called_once()

    def test_cloud_needs_distinct_per_request_consent(self):
        result = covers.CoverLocalizer(
            self.task, project_root=self.root, mode="cloud", allow_paid_api=True
        ).run()
        self.assertEqual(result["status"], "FAILED")
        self.copy.assert_not_called()

    def test_health_reuses_active_zhipu_key_without_exposing_it(self):
        health = covers.public_cover_health(self.root)
        self.assertTrue(health["api_key_configured"])
        self.assertEqual(health["api_provider"], "zhipu")
        self.assertEqual(health["api_provider_label"], "智谱 GLM")
        self.assertNotIn("offline-fixture-key", json.dumps(health, ensure_ascii=False))

    def test_snapshot_and_resources_keep_api_and_pillow_independent(self):
        app = ControlPanelApp(self.root)
        reference = self.task.relative_to(self.root / "downloads").as_posix()
        job = app.queue_cover(reference, allow_cloud_api=True)
        self.assertEqual(WorkflowWorker.initial_resource("cover", job["payload"]), "paid_api")
        stages = app.worker._build_stages(job)
        self.assertEqual([stage[2] for stage in stages], ["paid_api", "paid_api"])
        self.assertTrue(all("--allow-cloud-api" in stage[1] for stage in stages))

    def test_plain_download_does_not_inherit_global_api_cover(self):
        covers.update_cover_settings(self.root, {
            "cover_enabled": True,
            "cover_allow_cloud_api": True,
        })
        app = ControlPanelApp(self.root)
        job = app.queue_downloads(raw_input="abcdefghijk", confirm_rights=True)[0]
        self.assertFalse(job["payload"]["cover_enabled"])
        self.assertFalse(job["payload"]["cover_allow_cloud_api"])
        self.assertFalse(job["payload"]["cover_allow_paid_api"])

    def test_plain_download_needs_neither_cover_key_nor_cover_consent(self):
        covers.update_cover_settings(self.root, {
            "cover_enabled": True,
            "cover_allow_cloud_api": True,
        })
        update_env_file(self.root / ".env", {"ZHIPU_API_KEY": ""})
        app = ControlPanelApp(self.root)
        job = app.queue_downloads(raw_input="abcdefghijk", confirm_rights=True)[0]
        self.assertFalse(job["payload"]["cover_enabled"])
        self.assertFalse(job["payload"].get("automation_enabled", False))
        self.assertEqual([item["kind"] for item in app.store.list()], ["download"])
        with self.assertRaisesRegex(ValueError, "权利"):
            app.queue_downloads(raw_input="123456789ab", confirm_rights=False)

    def test_explicit_download_cover_keeps_authorized_snapshot(self):
        app = ControlPanelApp(self.root)
        job = app.queue_downloads(
            raw_input="abcdefghijk", confirm_rights=True,
            cover_choice="cloud", cover_cloud_authorized=True,
        )[0]
        self.assertTrue(job["payload"]["cover_enabled"])
        self.assertTrue(job["payload"]["cover_allow_cloud_api"])
        self.assertEqual(job["payload"]["cover_mode"], "cloud")

    def test_per_request_cloud_requires_consent_and_active_provider_key(self):
        app = ControlPanelApp(self.root)
        with self.assertRaises(ValueError):
            app.queue_downloads(
                raw_input="abcdefghijk", confirm_rights=True, cover_choice="cloud"
            )
        update_env_file(self.root / ".env", {"ZHIPU_API_KEY": ""})
        with self.assertRaisesRegex(ValueError, "AI 翻译"):
            app.queue_downloads(
                raw_input="abcdefghijk",
                confirm_rights=True,
                cover_choice="cloud",
                cover_cloud_authorized=True,
            )
        self.assertFalse(app.store.list())

    def test_local_and_off_override_api_mode(self):
        app = ControlPanelApp(self.root)
        for choice in ("off", "local"):
            options = app._cover_request_options(choice, True)
            self.assertEqual(options["cover_enabled"], choice == "local")
            self.assertEqual(options["cover_mode"], "local")
            self.assertFalse(options["cover_allow_cloud_api"])
