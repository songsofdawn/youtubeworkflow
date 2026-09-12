from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from PIL import Image, ImageDraw, ImageFont

from src import cover_localization as covers
from src import download_core
from src.control_panel.app import ControlPanelApp
from src.control_panel.publishing import BiliupIntegration
from src.control_panel.tasks import WorkflowScanner
from src.run_cover_localization import main
from tests.test_control_panel import make_task, make_publish_config, make_runtime, write_json
from tests.test_download_stage2 import fake_tools, stage2_config


def analysis_result() -> dict:
    return {
        "confidence": 0.95, "summary": "演示实验过程并解释测试结果。", "topic": "实验",
        "keywords": ["实验", "测试"], "entities": [],
        "source_text": ["THE TEST"],
        "text_regions": [{"x": 0.05, "y": 0.08, "width": 0.5, "height": 0.25, "confidence": 0.95}],
        "subject_regions": [{"x": 0.7, "y": 0.4, "width": 0.25, "height": 0.5, "confidence": 0.95}],
        "text_area": "top", "text_alignment": "left", "text_color": "#FFF100",
        "stroke_color": "#000000", "candidates": ["实验结果揭晓", "这次测试如何", "看懂实验原理"], "best_index": 0,
    }


class CoverTests(TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_publish_config(self.root)
        make_runtime(self.root)
        self.task = make_task(self.root)
        write_json(self.root / "config/trending_config.json", {"discovery_llm": {"enabled": True, "model": "fixture-vision"}})
        self.original = self.task / "metadata/thumbnail.jpg"
        image = Image.new("RGB", (1280, 720), (20, 25, 40))
        draw = ImageDraw.Draw(image)
        draw.text((80, 70), "THE TEST", font=ImageFont.truetype("arial.ttf", 80), fill="white")
        draw.rectangle((900, 290, 1200, 645), fill=(180, 90, 60))
        image.save(self.original, quality=98)
        self.original_bytes = self.original.read_bytes()
        self.vision = mock.patch.object(covers, "_local_vision", return_value=analysis_result()).start()
        self.addCleanup(mock.patch.stopall)
        self.cloud = mock.patch.object(covers, "_cloud_copy", return_value=analysis_result()).start()
        self.network = mock.patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("offline only")).start()

    def localizer(self, **kwargs):
        return covers.CoverLocalizer(self.task, project_root=self.root, **kwargs)

    def test_success_cache_and_publish_selection(self):
        result = self.localizer().run()
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["selected_copy"], "实验结果揭晓")
        self.assertGreaterEqual(
            result["layout"]["layout"]["font_size"], round(720 * 0.09)
        )
        self.assertTrue(result["layout"]["layout"]["centered"])
        centered_box = result["layout"]["layout"]["box"]
        self.assertEqual(centered_box[0] + centered_box[2], 1280)
        self.assertEqual(result["layout"]["layout"]["alignment"], "center")
        self.assertEqual(result["layout"]["layout"]["vertical_position"], "lower_middle")
        self.assertGreater((centered_box[1] + centered_box[3]) / 2, 720 / 2)
        self.assertRegex(result["cover_text_color"], r"^#[0-9A-F]{6}$")
        self.assertEqual(result["cover_text_color"], result["layout"]["layout"]["text_color"])
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        output = covers.localized_cover_path(self.task, self.root)
        with Image.open(output) as image:
            self.assertEqual(image.size, (1280, 720))
            # Subject remains intact outside the erased / lettering areas.
            self.assertLess(sum(abs(a-b) for a, b in zip(image.getpixel((1100, 500)), (180, 90, 60))), 15)
        self.assertEqual(BiliupIntegration(self.root).cover_path(self.task), output)
        self.assertTrue(self.localizer().run()["cached"])
        self.vision.assert_called_once()
        self.cloud.assert_not_called()
        self.network.assert_not_called()
        task = WorkflowScanner(self.root).scan()[0]
        self.assertEqual(task["progress"], 20)
        self.assertTrue(task["cover_localized_available"])

    def test_forced_regeneration_uses_a_different_palette_color(self):
        first = self.localizer().run()
        second = self.localizer(force=True).run()
        self.assertEqual(first["status"], "COMPLETED", first)
        self.assertEqual(second["status"], "COMPLETED", second)
        self.assertNotEqual(first["cover_text_color"], second["cover_text_color"])

    def test_failed_analysis_preserves_original_and_previous_output(self):
        self.assertEqual(self.localizer().run()["status"], "COMPLETED")
        output = covers.localized_cover_path(self.task, self.root)
        old = output.read_bytes()
        self.vision.side_effect = RuntimeError("request contains API_KEY=secret")
        result = self.localizer(force=True, allow_paid_api=True).run("analyze")
        self.assertEqual(result["status"], "FAILED")
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(self.localizer(force=True, allow_paid_api=True).run("finalize")["status"], "FAILED")
        self.assertEqual(output.read_bytes(), old)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assertEqual(BiliupIntegration(self.root).cover_path(self.task), self.original)
        self.cloud.assert_not_called()

    def test_complex_background_and_subject_overlap_are_repaired(self):
        noisy = Image.effect_noise((1280, 720), 100).convert("RGB")
        noisy.save(self.original)
        noisy_bytes = self.original.read_bytes()
        result = self.localizer().run()
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["layout"]["repair_method"], "opencv_telea")
        self.assertTrue(result["warnings"])
        self.assertEqual(self.original.read_bytes(), noisy_bytes)
        self.assertTrue(covers.localized_cover_path(self.task, self.root).exists())
        self.original.write_bytes(self.original_bytes)
        self.vision.return_value["subject_regions"] = self.vision.return_value["text_regions"]
        result = self.localizer(force=True).run()
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertIn("真实面部", " ".join(result["warnings"]))
        self.assertEqual(self.original.read_bytes(), self.original_bytes)

    def test_large_text_and_fully_occupied_subject_do_not_block_repair(self):
        self.vision.return_value["text_regions"] = [{"x": 0.01, "y": 0.01, "width": 0.8, "height": 0.6, "confidence": 0.95}]
        self.vision.return_value["subject_regions"] = [{"x": 0, "y": 0, "width": 1, "height": 1, "confidence": 0.95}]
        result = self.localizer().run()
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertTrue(result["warnings"])
        self.assertEqual(self.original.read_bytes(), self.original_bytes)

    def test_inpaint_preserves_every_pixel_outside_mask(self):
        image = Image.effect_noise((160, 90), 45).convert("RGBA")
        before = image.copy()
        covers._inpaint_regions(image, [(0, 0, 50, 30), (80, 40, 120, 75)])
        for y in range(90):
            for x in range(160):
                if not (x < 50 and y < 30 or 80 <= x < 120 and 40 <= y < 75):
                    self.assertEqual(image.getpixel((x, y)), before.getpixel((x, y)))

    def test_inpaint_error_preserves_original_and_previous_output(self):
        self.localizer().run()
        output = covers.localized_cover_path(self.task, self.root)
        old = output.read_bytes()
        self.vision.return_value["subject_regions"] = self.vision.return_value["text_regions"]
        with mock.patch.object(covers, "_inpaint_regions", side_effect=RuntimeError("repair failed")):
            self.assertEqual(self.localizer(force=True).run()["status"], "FAILED")
        self.assertEqual(output.read_bytes(), old)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)

    def test_explicit_paid_copy_runs_only_in_finalize(self):
        self.assertEqual(self.localizer(allow_paid_api=True).run("analyze")["status"], "ANALYZED")
        self.cloud.assert_not_called()
        self.assertEqual(self.localizer(allow_paid_api=True).run("finalize")["status"], "COMPLETED")
        self.cloud.assert_called_once()
        self.vision.assert_called_once()

    def test_context_reuse_and_ollama_public_metadata_boundary(self):
        summary = {"summary": "LOCAL_SUBTITLE_PRIVATE_SUMMARY", "topic": "实验", "keywords": [], "entities": []}
        source = self.task / "stage3/video_context.json"
        write_json(source, summary)
        before = source.read_bytes()
        result = self.localizer(allow_paid_api=True).run()
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(source.read_bytes(), before)
        self.assertNotIn(summary["summary"], repr(self.vision.call_args))
        self.assertIn(summary["summary"], repr(self.cloud.call_args))
        context = json.loads((self.task / "video_context.json").read_text(encoding="utf-8"))
        self.assertEqual(context["summary"], summary["summary"])

    def test_changed_context_or_corrupt_output_invalidates_cache(self):
        self.localizer().run()
        context_path = self.task / "video_context.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context["summary"] = "新的摘要"
        write_json(context_path, context)
        self.assertFalse(self.localizer().run().get("cached"))
        output = covers.localized_cover_path(self.task, self.root)
        output.write_bytes(b"broken")
        self.assertEqual(BiliupIntegration(self.root).cover_path(self.task), self.original)
        self.assertEqual(self.localizer().run()["status"], "COMPLETED")

    def test_cli_optional_failure_does_not_fail_video(self):
        self.vision.side_effect = RuntimeError("offline")
        with mock.patch("builtins.print"):
            self.assertEqual(main(["--video-dir", str(self.task), "--project-root", str(self.root)]), 0)
        self.assertEqual(json.loads((self.task / "cover/cover_manifest.json").read_text(encoding="utf-8"))["status"], "FAILED")

    def test_textless_thumbnail_gets_safe_overlay(self):
        Image.new("RGB", (1280, 720), (20, 25, 40)).save(self.original)
        self.vision.return_value["source_text"] = []
        self.vision.return_value["text_regions"] = []
        self.assertEqual(self.localizer().run()["status"], "COMPLETED")

    def test_paid_api_failure_keeps_original_without_provider_fallback(self):
        self.cloud.side_effect = RuntimeError("credential=secret")
        result = self.localizer(allow_paid_api=True).run()
        self.assertEqual(result["status"], "FAILED")
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assertEqual(BiliupIntegration(self.root).cover_path(self.task), self.original)
        self.cloud.assert_called_once()

    def test_failed_image_write_preserves_previous_localized_file(self):
        self.assertEqual(self.localizer().run()["status"], "COMPLETED")
        output = covers.localized_cover_path(self.task, self.root)
        previous = output.read_bytes()
        with mock.patch.object(Image.Image, "save", side_effect=OSError("disk full")):
            result = self.localizer().run("finalize")
            # Force a new render instead of accepting the completed cache.
            if result.get("cached"):
                result = self.localizer(force=True).run("finalize")
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(output.read_bytes(), previous)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)

    def test_oversized_and_invalid_coordinates_are_rejected(self):
        for region in ({"x": -1, "y": 0, "width": 0.4, "height": 0.2},
                       {"x": 0.9, "y": 0, "width": 0.4, "height": 0.2}):
            analysis = analysis_result()
            analysis["text_regions"] = [region]
            with self.assertRaises(covers.CoverDependencyError):
                covers.CoverLocalizer._validate_analysis(analysis)


class CandidateTests(TestCase):
    def test_short_candidates_not_truncated_or_invented(self):
        values, selected, index = covers.normalize_candidates(analysis_result(), {})
        self.assertTrue(3 <= len(values) <= 5)
        self.assertTrue(all(4 <= len(value) <= 12 for value in values))
        self.assertEqual(values[index], selected)
        bad = {"candidates": ["史上最强实验", "这是一个超过十二字上限不该被截断的文案", "No Chinese"]}
        with self.assertRaises(covers.CoverDependencyError):
            covers.normalize_candidates(bad, {})


class CoverIntegrationTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        make_publish_config(self.root)
        make_runtime(self.root)
        self.task = make_task(self.root)
        self.ref = self.task.relative_to(self.root / "downloads").as_posix()
        self.app = ControlPanelApp(self.root)

    def test_settings_and_paid_authorization_are_separate(self):
        covers.update_cover_settings(self.root, {"cover_enabled": True, "cover_allow_paid_copy": True})
        jobs = self.app.queue_downloads(
            raw_input="abcdefghijk", confirm_rights=True, cover_enabled=True,
        )
        self.assertTrue(jobs[0]["payload"]["cover_enabled"])
        self.assertTrue(jobs[0]["payload"]["cover_allow_paid_api"])
        with self.assertRaises(ValueError):
            covers.update_cover_settings(self.root, {"cover_enabled": "true"})

    def test_cover_only_settings_are_persisted_without_other_form_fields(self):
        with mock.patch.object(self.app, "health", return_value={"llm": {"active": {"provider": "deepseek"}}}):
            result = self.app.save_settings({"cover_enabled": True, "cover_allow_paid_copy": False})
        self.assertIn("cover_enabled", result["saved"])
        self.assertTrue(covers.load_cover_config(self.root)["enabled"])
        self.assertFalse(covers.load_cover_config(self.root)["allow_paid_copy"])

    def test_original_preview_http_returns_image_bytes(self):
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from urllib.parse import quote
        from src.control_panel.server import make_handler

        original = self.task / "metadata/thumbnail.jpg"
        Image.new("RGB", (160, 90), "navy").save(original)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app, self.root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/cover?task={quote(self.ref)}&variant=original"
            with urllib.request.urlopen(url, timeout=3) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"], "image/jpeg")
                self.assertEqual(response.read(), original.read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_cover_slots_do_not_bypass_gpu_or_paid_slots(self):
        job = self.app.queue_cover(self.ref, allow_paid_api=True)
        stages = self.app.worker._build_stages(job)
        self.assertEqual([s[2] for s in stages], ["gpu_heavy", "paid_api"])
        self.assertIn("analyze", stages[0][1])
        self.assertIn("finalize", stages[1][1])
        with self.assertRaises(ValueError):
            self.app.queue_cover(self.ref)

    def test_failed_cover_child_still_reaches_publish_followup(self):
        job = self.app.store.enqueue("pipeline", self.ref, {
            "workflow": "render", "chinese_subtitle_source": "deepseek", "render_mode": "hardsub",
            "cover_enabled": True, "cover_allow_paid_api": True, "auto_publish": True,
            "allow_paid_api": True,
        })
        worker = self.app.worker
        with mock.patch.object(worker, "_run_command", side_effect=[0, 7, OSError("cannot start")]), \
             mock.patch.object(worker, "_queue_automatic_publish", return_value="queued upload") as publish:
            for _ in range(3):
                claimed = self.app.store.claim_id(job["id"])
                worker._execute(claimed)
            final = self.app.store.get(job["id"])
            self.assertEqual(final["status"], "completed", final)
            publish.assert_called_once()
        self.assertEqual(json.loads((self.task / "cover/cover_manifest.json").read_text(encoding="utf-8"))["status"], "FAILED")

    def test_standalone_cover_job_exposes_failed_manifest_as_failed_job(self):
        job = self.app.store.enqueue(
            "cover",
            self.ref,
            {"cover_enabled": True, "cover_mode": "local"},
            resource_class="gpu_heavy",
        )
        worker = self.app.worker
        write_json(
            self.task / "cover" / "cover_manifest.json",
            {"status": "FAILED", "errors": ["fixture failure"]},
        )
        with mock.patch.object(worker, "_run_command", return_value=0):
            worker._execute(self.app.store.claim_id(job["id"]))
        failed = self.app.store.get(job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("fixture failure", failed["error"])

    def test_preview_is_limited_to_task_and_known_variants(self):
        (self.task / "metadata/thumbnail.jpg").write_bytes(b"original")
        self.assertEqual(self.app.cover_file(self.ref, "original").read_bytes(), b"original")
        for task, variant in (("../../outside", "original"), (self.ref, "../../private")):
            with self.assertRaises(ValueError):
                self.app.cover_file(task, variant)


class ThumbnailDownloadTests(TestCase):
    def test_metadata_highest_quality_and_atomic_failure(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            metadata = {"thumbnails": [
                {"url": "https://i.ytimg.com/small.jpg", "width": 320, "height": 180},
                {"url": "https://i.ytimg.com/big.jpg", "width": 1280, "height": 720},
            ]}
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b"image bytes"
            opener = mock.Mock()
            opener.open.return_value = response
            def convert(command, cwd):
                Path(command[-1]).write_bytes(b"converted image")
                return {"success": True, "command": [str(item) for item in command]}
            with mock.patch("urllib.request.build_opener", return_value=opener), \
                 mock.patch.object(download_core, "run_command", side_effect=convert):
                result = download_core.download_thumbnail("url", root, fake_tools(root), stage2_config(), download_core.get_project_paths(root), metadata)
            self.assertTrue(result["success"])
            self.assertEqual(opener.open.call_args.args[0].full_url, "https://i.ytimg.com/big.jpg")
            self.assertEqual((root / "metadata/thumbnail.jpg").read_bytes(), b"converted image")

    def test_untrusted_redirect_is_rejected(self):
        with self.assertRaises(ValueError):
            download_core._ThumbnailRedirectHandler().redirect_request(
                None, None, 302, "redirect", {}, "http://127.0.0.1/private")
