from __future__ import annotations

import csv
import json
import tempfile
from pathlib import Path

from src.stage3.review_workflow import export_review, import_review
from src.stage3.subtitle_writer import read_srt


EN = "1\n00:00:01,000 --> 00:00:04,000\nWe test all three servers.\n"
ZH = "1\n00:00:01,000 --> 00:00:04,000\n我们测试这三台服务器。\n"


def test_manual_review_becomes_new_canonical_text() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        subtitles = root / "subtitles"
        subtitles.mkdir(parents=True)
        (subtitles / "en.dubbing.srt").write_text(EN, encoding="utf-8")
        (subtitles / "zh.clean.srt").write_text(ZH, encoding="utf-8")
        (subtitles / "zh.raw.srt").write_text(ZH, encoding="utf-8")
        (root / "stage3_manifest.json").write_text(
            json.dumps({"translation_for_dubbing": True}), encoding="utf-8"
        )
        canonical = root / "stage3" / "translation" / "canonical_zh.json"
        canonical.parent.mkdir(parents=True)
        canonical.write_text(
            json.dumps(
                {
                    "version": 1,
                    "architecture": "single_script_dual_segmentation",
                    "utterances": [
                        {
                            "id": 1,
                            "start": 1.0,
                            "end": 4.0,
                            "source_text": "We test all three servers.",
                            "zh_text": "我们测试这三台服务器。",
                            "source_segment_ids": [1],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        exported = export_review(root)
        review_path = Path(exported["review_file"])
        with review_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        rows[0]["reviewed_translation"] = "今天我们测试这三台服务器。"
        with review_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), delimiter="\t", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

        report = import_review(root, review_path)

        assert report["status"] == "REVIEWED"
        payload = json.loads(canonical.read_text(encoding="utf-8"))
        assert payload["utterances"][0]["zh_text"] == "今天我们测试这三台服务器。"
        assert payload["reviewed"] is True
        assert read_srt(subtitles / "zh.reviewed.srt")[0].text == "今天我们测试这三台服务器。"
        assert read_srt(subtitles / "zh.dubbing.srt")[0].text == "今天我们测试这三台服务器。"
