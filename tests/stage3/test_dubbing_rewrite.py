from __future__ import annotations

import json
from pathlib import Path

from src.stage3.dubbing_rewrite import apply_duration_rewrites
from src.stage3.dubbing_script import script_text_hash


def test_apply_duration_rewrite_updates_canonical_and_dubbing_srt(tmp_path: Path) -> None:
    video = tmp_path / "video"
    canonical_dir = video / "stage3" / "translation"
    subtitle_dir = video / "subtitles"
    canonical_dir.mkdir(parents=True)
    subtitle_dir.mkdir(parents=True)
    canonical = {
        "version": 1,
        "architecture": "single_script_dual_segmentation",
        "utterance_count": 2,
        "canonical_text_hash": script_text_hash("原来很长。第二句。"),
        "utterances": [
            {
                "id": 1,
                "start": 0.0,
                "end": 2.0,
                "source_text": "The original sentence is too long.",
                "zh_text": "原来很长。",
                "source_segment_ids": [1],
            },
            {
                "id": 2,
                "start": 2.1,
                "end": 4.0,
                "source_text": "Second sentence.",
                "zh_text": "第二句。",
                "source_segment_ids": [2],
            },
        ],
    }
    (canonical_dir / "canonical_zh.json").write_text(
        json.dumps(canonical, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (video / "stage3_manifest.json").write_text(
        json.dumps({"translation_for_dubbing": True}, ensure_ascii=False),
        encoding="utf-8",
    )

    result = apply_duration_rewrites(
        video,
        {1: "改短了。"},
        request_meta={1: {"target_duration": 1.0, "spoken_duration": 2.5}},
    )

    payload = json.loads((canonical_dir / "canonical_zh.json").read_text(encoding="utf-8"))
    assert payload["utterances"][0]["zh_text"] == "改短了。"
    assert payload["utterances"][1]["zh_text"] == "第二句。"
    assert result["changed_count"] == 1
    srt = (subtitle_dir / "zh.dubbing.srt").read_text(encoding="utf-8")
    assert "改短了。" in srt
    assert "原来很长。" not in srt
