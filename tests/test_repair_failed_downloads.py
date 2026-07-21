from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase, mock

from src.repair_failed_downloads import discover_incomplete_tasks, repair_incomplete_tasks


ROOT = Path(__file__).resolve().parents[1]


def config() -> dict:
    return json.loads((ROOT / "config" / "download_config.json").read_text(encoding="utf-8"))


def tools() -> dict[str, Path]:
    return {name: ROOT / "tools" / "bin" / f"{name}.exe" for name in ("yt-dlp", "ffmpeg", "ffprobe")}


class RepairFailedDownloadsTests(TestCase):
    def _fixture(self, *, selected: int = 1, rights: str = "APPROVED", status: str = "failed"):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name) / "downloads" / "candidates"
        task = root / "2026-07-21" / "001_abc_Title"; task.mkdir(parents=True)
        candidate_file = Path(temporary.name) / "2026-07-21_US_localization_top50.json"
        candidate = {"video_id": "abc", "rank": 1, "title": "Title", "selected": selected, "rights_status": rights, "youtube_url": "https://www.youtube.com/watch?v=abc"}
        candidate_file.write_text(json.dumps({"candidates": [candidate]}), encoding="utf-8")
        manifest = {
            "video_id": "abc", "url": candidate["youtube_url"], "source_mode": "candidate",
            "candidate_file": str(candidate_file), "candidate_rank": 1, "overall_status": status,
            "metadata_status": "failed", "video_status": "not_started", "audio_status": "not_started",
        }
        (task / "download_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return temporary, root, task, candidate_file

    def test_discovers_failed_manifest_with_no_other_files(self) -> None:
        temporary, root, _, _ = self._fixture()
        try:
            tasks = discover_incomplete_tasks(root, config())
            self.assertEqual(len(tasks), 1); self.assertEqual(tasks[0]["video_id"], "abc")
        finally:
            temporary.cleanup()

    def test_success_manifest_with_missing_files_is_still_repairable(self) -> None:
        temporary, root, _, _ = self._fixture(status="success")
        try:
            self.assertEqual(len(discover_incomplete_tasks(root, config())), 1)
        finally:
            temporary.cleanup()

    def test_empty_description_is_valid_when_other_required_files_exist(self) -> None:
        temporary, root, task, _ = self._fixture(status="success")
        try:
            for relative, content in (
                ("video/source.mp4", b"video"), ("audio/source_audio.wav", b"audio"),
                ("metadata/info.json", b"{}"), ("metadata/description.txt", b""),
            ):
                path = task / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
            manifest_path = task / "download_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.update(subtitle_clean_status="missing")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(discover_incomplete_tasks(root, config()), [])
        finally:
            temporary.cleanup()

    def test_recovers_candidate_from_manifest_and_repairs(self) -> None:
        temporary, root, _, candidate_file = self._fixture()
        try:
            with mock.patch("src.repair_failed_downloads.download_one_video", return_value={"overall_status": "success"}) as downloader:
                stats = repair_incomplete_tasks(root, attempts=1, retry_delay=0, config=config(), tools=tools())
            self.assertEqual(stats["repaired_success"], 1)
            kwargs = downloader.call_args.kwargs
            self.assertEqual(kwargs["candidate"]["video_id"], "abc"); self.assertEqual(kwargs["candidate_file"], candidate_file)
            self.assertFalse(kwargs["force"])
        finally:
            temporary.cleanup()

    def test_revalidates_rights_before_repair(self) -> None:
        temporary, root, _, _ = self._fixture(rights="PENDING")
        try:
            with mock.patch("src.repair_failed_downloads.download_one_video") as downloader:
                stats = repair_incomplete_tasks(root, attempts=1, retry_delay=0, config=config(), tools=tools())
            self.assertEqual(stats["skipped_rights"], 1); downloader.assert_not_called()
        finally:
            temporary.cleanup()

    def test_transient_failure_is_retried(self) -> None:
        temporary, root, _, _ = self._fixture()
        try:
            with mock.patch("src.repair_failed_downloads.download_one_video", side_effect=[{"overall_status": "failed"}, {"overall_status": "success"}]) as downloader, mock.patch("src.repair_failed_downloads.time.sleep") as sleeper:
                stats = repair_incomplete_tasks(root, attempts=3, retry_delay=1, config=config(), tools=tools())
            self.assertEqual(downloader.call_count, 2); sleeper.assert_called_once_with(1)
            self.assertEqual(stats["repaired_success"], 1); self.assertEqual(stats["failed"], 0)
        finally:
            temporary.cleanup()

    def test_dry_run_never_starts_download(self) -> None:
        temporary, root, _, _ = self._fixture()
        try:
            with mock.patch("src.repair_failed_downloads.download_one_video") as downloader:
                stats = repair_incomplete_tasks(root, dry_run=True, config=config(), tools=tools())
            self.assertEqual((stats["repairable"], stats["dry_run"]), (1, 1)); downloader.assert_not_called()
        finally:
            temporary.cleanup()
