from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from src.stage3.review_workflow import export_review, import_review


SRT_EN = "1\n00:00:00,000 --> 00:00:01,000\nHello.\n"
SRT_ZH = "1\n00:00:00,000 --> 00:00:01,000\n你好。\n"


class ReviewWorkflowTests(TestCase):
    def _task(self, directory: str) -> Path:
        root = Path(directory)
        (root / "subtitles").mkdir()
        (root / "subtitles" / "en.selected.srt").write_text(SRT_EN, encoding="utf-8")
        (root / "subtitles" / "zh.raw.srt").write_text(SRT_ZH, encoding="utf-8")
        (root / "subtitles" / "zh.clean.srt").write_text(SRT_ZH, encoding="utf-8")
        return root

    def test_export_creates_tsv_and_html_but_not_reviewed_srt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._task(directory)
            result = export_review(root)
            self.assertEqual(result["status"], "REVIEW_EXPORTED")
            self.assertTrue((root / "stage3" / "review" / "review_export.tsv").is_file())
            self.assertTrue((root / "stage3" / "stage3_review.html").is_file())
            self.assertFalse((root / "subtitles" / "zh.reviewed.srt").exists())

    def test_export_reads_translation_qc_and_html_aligns_sources_by_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._task(directory)
            (root / "subtitles" / "en.selected.srt").write_text(
                "1\n00:00:10,000 --> 00:00:11,000\nSelected text.\n",
                encoding="utf-8",
            )
            (root / "subtitles" / "en.youtube.clean.srt").write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nWrong ordinal text.\n\n"
                "2\n00:00:10,000 --> 00:00:11,000\nTime aligned YouTube text.\n",
                encoding="utf-8",
            )
            (root / "subtitles" / "en.whisper.clean.srt").write_text(
                "1\n00:00:10,000 --> 00:00:11,000\nTime aligned Whisper text.\n",
                encoding="utf-8",
            )
            translation_dir = root / "stage3" / "translation"
            translation_dir.mkdir(parents=True)
            (translation_dir / "translation_qc.json").write_text(
                json.dumps({"empty_translation_ids": [1]}),
                encoding="utf-8",
            )
            whisper_dir = root / "stage3" / "whisper"
            whisper_dir.mkdir(parents=True)
            (whisper_dir / "words.json").write_text(
                json.dumps([{"start": 10.2, "end": 10.4, "probability": 0.4}]),
                encoding="utf-8",
            )

            export_review(root)
            review_file = root / "stage3" / "review" / "review_export.tsv"
            with review_file.open(encoding="utf-8") as handle:
                exported = next(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(exported["qc_flags"], "EMPTY_TRANSLATION")
            page = (root / "stage3" / "stage3_review.html").read_text(encoding="utf-8")
            self.assertIn("Time aligned YouTube text.", page)
            self.assertNotIn("Wrong ordinal text.", page)
            self.assertIn("data-low-confidence='1'", page)
            self.assertIn("getAttribute(`data-${filter}`)", page)

    def test_import_validates_timeline_and_empty_translation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._task(directory)
            export_review(root)
            review_file = root / "stage3" / "review" / "review_export.tsv"
            with review_file.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            rows[0]["start"] = "00:00:00,500"
            rows[0]["reviewed_translation"] = ""
            with review_file.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), delimiter="\t")
                writer.writeheader()
                writer.writerows(rows)
            result = import_review(root, review_file)
            self.assertEqual(result["status"], "REVIEW_IMPORT_FAILED")
            self.assertFalse((root / "subtitles" / "zh.reviewed.srt").exists())

    def test_import_creates_reviewed_and_refuses_default_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._task(directory)
            export_review(root)
            review_file = root / "stage3" / "review" / "review_export.tsv"
            first = import_review(root, review_file)
            self.assertEqual(first["status"], "REVIEWED")
            second = import_review(root, review_file)
            self.assertEqual(second["status"], "REVIEW_IMPORT_FAILED")
            third = import_review(root, review_file, overwrite_reviewed=True)
            self.assertEqual(third["status"], "REVIEWED")
