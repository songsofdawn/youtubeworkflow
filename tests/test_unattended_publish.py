from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from src.control_panel.jobs import JobStore, WorkflowWorker
from src.control_panel.publishing import BiliupIntegration
from src.control_panel.tasks import WorkflowScanner
from tests.test_control_panel import make_publish_config, make_runtime, make_task, write_json


class UnattendedPublishTests(TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.task = make_task(self.project)
        make_runtime(self.project)
        make_publish_config(self.project)
        self.source = self.task / "video" / "source.mp4"
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(b"original-video")
        self.store = JobStore(self.project / "jobs.sqlite3", self.project / "logs")
        self.scanner = WorkflowScanner(self.project)
        self.publisher = BiliupIntegration(self.project)
        self.worker = WorkflowWorker(
            self.project, self.store, self.scanner, self.publisher,
        )
        self.reference = self.task.relative_to(self.project / "downloads").as_posix()
        self.payload = {
            "workflow": "complete",
            "render_mode": "hardsub",
            "chinese_subtitle_source": "deepseek",
            "automation_enabled": True,
            "automation_target": "publish",
            "publish_metadata_provider": "local_ollama",
            "account_id": self.publisher.accounts()[0]["id"],
            "publish_only_self": False,
            "allow_paid_api": False,
        }

    def automation(self) -> dict:
        return json.loads(
            (self.task / "stage5" / "automation_manifest.json").read_text(encoding="utf-8")
        )

    def fail_stage(self, label: str, *, error: Exception | None = None) -> dict:
        stages = self.worker._build_stages({
            "kind": "pipeline", "target": self.reference, "payload": self.payload,
        })
        index = next(i for i, stage in enumerate(stages) if stage[0] == label)
        resource = stages[index][2]
        job = self.store.enqueue(
            "pipeline", self.reference, {**self.payload, "_stage_index": index},
            resource_class=resource,
        )
        claimed = self.store.claim_next({"pipeline"}, {resource})
        with mock.patch.object(self.worker, "_run_command", return_value=2, side_effect=error):
            self.worker._execute(claimed)
        return self.store.get(job["id"])

    def assert_original_publish_queued(self, job: dict, reason: str) -> None:
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["payload"]["_stage_index"], 0)
        self.assertTrue(job["payload"]["publish_original_video"])
        self.assertFalse(job["payload"]["dubbing_enabled"])
        self.assertEqual(self.automation()["status"], "ORIGINAL_MEDIA")
        self.assertEqual(self.automation()["reason"], reason)
        self.assertFalse(self.scanner.scan()[0]["automation_skipped"])
        commands = self.worker._build_commands(job)
        self.assertEqual(len(commands), 1)
        self.assertIn("--allow-no-subtitles", commands[0][1])
        self.assertNotIn("--allow-paid-api", commands[0][1])
        write_json(self.task / "stage3" / "publish_metadata.json", {
            "status": "RECOMMENDED", "title_zh": "园艺种植演示",
            "tags": "园艺,种植", "tid": 21,
        })
        claimed = self.store.claim_next({"pipeline"}, {job["resource_class"]})
        with mock.patch.object(self.worker, "_run_command", return_value=0):
            self.worker._execute(claimed)
        self.assertEqual(self.store.get(job["id"])["status"], "completed")
        uploads = [row for row in self.store.list() if row["kind"] == "publish"]
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        self.assertEqual(upload["status"], "queued")
        self.assertEqual(upload["resource_class"], "upload")
        self.assertTrue(upload["payload"]["automatic"])
        self.assertEqual(upload["payload"]["account_id"], self.payload["account_id"])
        self.assertEqual(upload["payload"]["is_only_self"], self.payload["publish_only_self"])
        self.assertEqual(self.publisher.build_upload_command(self.task, upload["payload"])[-1], str(self.source))
        self.assertEqual(self.source.read_bytes(), b"original-video")

    def no_speech(self, *, title_track: bool = False) -> None:
        write_json(self.task / "stage3" / "01_source_assessment.json", {
            "route": "YOUTUBE_ENGLISH_SOURCE" if title_track else "NO_YOUTUBE_ENGLISH_SOURCE",
        })
        write_json(self.task / "stage3" / "whisper" / "asr_info.json", {
            "segment_count": 0, "word_count": 0,
        })
        if title_track:
            write_json(self.task / "stage3" / "selection" / "selection_report.json", {
                "selection_failed": True, "selected_source": "",
            })

    def test_english_failure_without_asr_report_continues_to_original_upload(self) -> None:
        # A stale render error must not obscure this run's English failure.
        write_json(self.task / "stage4" / "stage4_manifest.json", {
            "status": "FAILED", "errors": [{"code": "NO_VALID_CHINESE_SUBTITLE"}],
        })
        job = self.fail_stage("生成并选择最佳英文字幕")
        self.assert_original_publish_queued(job, "ENGLISH_SUBTITLE_STAGE_FAILED")

    def test_legacy_auto_publish_with_recognized_speech_continues_to_upload(self) -> None:
        self.payload.pop("automation_enabled")
        self.payload.pop("automation_target")
        self.payload.update(auto_publish=True, publish_only_self=True)
        write_json(self.task / "stage3" / "whisper" / "asr_info.json", {
            "segment_count": 12, "word_count": 60,
        })
        job = self.fail_stage("生成并选择最佳英文字幕")
        self.assert_original_publish_queued(job, "ENGLISH_SUBTITLE_STAGE_FAILED")

    def test_no_speech_defaults_to_original_upload(self) -> None:
        self.no_speech()
        job = self.fail_stage("生成并选择最佳英文字幕")
        self.assert_original_publish_queued(job, "NO_NARRATION_OR_BACKGROUND_MUSIC")

    def test_title_only_track_defaults_to_original_upload(self) -> None:
        self.no_speech(title_track=True)
        job = self.fail_stage("生成并选择最佳英文字幕")
        self.assert_original_publish_queued(job, "NO_NARRATION_OR_BACKGROUND_MUSIC")

    def test_english_process_exception_uses_same_fallback(self) -> None:
        job = self.fail_stage("生成并选择最佳英文字幕", error=OSError("process unavailable"))
        self.assert_original_publish_queued(job, "ENGLISH_SUBTITLE_STAGE_FAILED")

    def test_render_process_exception_uses_same_fallback(self) -> None:
        job = self.fail_stage("生成并质检双语成片", error=OSError("process unavailable"))
        self.assert_original_publish_queued(job, "STAGE4_RENDER_STAGE_FAILED")

    def test_original_metadata_failure_stays_retryable_without_loop_or_skip(self) -> None:
        self.payload.update(silent_video_mode=True, publish_original_video=True)
        self.publisher.mark_automation_original_media(self.task)
        job = self.fail_stage("生成无配音视频投稿信息")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.automation()["status"], "FAILED")
        self.assertFalse(self.scanner.scan()[0]["automation_skipped"])
        self.assertEqual(self.scanner.scan()[0]["stages"]["publish"]["state"], "failed")
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.retry(job["id"])["status"], "queued")

    def test_legacy_no_speech_skip_setting_uses_original_upload(self) -> None:
        self.no_speech()
        self.payload["automation_silent_video_policy"] = "skip"
        job = self.fail_stage("生成并选择最佳英文字幕")
        self.assert_original_publish_queued(job, "NO_NARRATION_OR_BACKGROUND_MUSIC")

    def test_failure_policy_uses_original_upload_for_unattended_publish(self) -> None:
        self.payload["automation_failure_policy"] = "fail"
        job = self.fail_stage("生成并选择最佳英文字幕")
        self.assert_original_publish_queued(job, "ENGLISH_SUBTITLE_STAGE_FAILED")

    def test_subtitles_target_does_not_publish(self) -> None:
        self.payload.update(automation_target="subtitles", workflow="subtitles")
        job = self.fail_stage("生成并选择最佳英文字幕")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.automation()["status"], "FAILED")
        self.assertFalse(any(row["kind"] == "publish" for row in self.store.list()))
