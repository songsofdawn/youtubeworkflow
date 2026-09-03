from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.dubbing.pipeline import (
    DubbingError,
    canonical_dubbing_segments,
    validate_canonical_dubbing_script,
)
from src.stage3.models import TranslationSegment
from src.stage3.subtitle_writer import atomic_write_srt


def write_canonical(root: Path, zh_text: str = "今天我们来测试这三台服务器。") -> Path:
    (root / "stage3_manifest.json").write_text(
        json.dumps({"translation_for_dubbing": True}, ensure_ascii=False),
        encoding="utf-8",
    )
    canonical = root / "stage3" / "translation" / "canonical_zh.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(
        json.dumps(
            {
                "version": 1,
                "architecture": "single_script_dual_segmentation",
                "canonical_text_hash": "test",
                "utterances": [
                    {
                        "id": 1,
                        "start": 10.0,
                        "end": 14.0,
                        "source_text": "Today we test all three servers.",
                        "zh_text": zh_text,
                        "source_segment_ids": [1, 2, 3],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return canonical


def write_zh(root: Path, text: str) -> Path:
    path = root / "subtitles" / "zh.dubbing.srt"
    atomic_write_srt(
        path,
        [TranslationSegment(1, 10.0, 14.0, "source", text, text)],
        translated=True,
        width=8,
        max_lines=2,
    )
    return path


def test_canonical_validator_accepts_layout_only_whitespace() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_canonical(root)
        subtitle = write_zh(root, "今天我们来测试这三台服务器。")

        report = validate_canonical_dubbing_script(root, subtitle)
        rows = canonical_dubbing_segments(root)

        assert report["status"] == "PASSED"
        assert rows[0]["text"] == "今天我们来测试这三台服务器。"
        # TTS comes from canonical JSON, not the line-wrapped SRT representation.
        assert " " not in rows[0]["text"]


def test_canonical_validator_blocks_different_spoken_wording() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_canonical(root, "今天我们来测试这三台服务器。")
        subtitle = write_zh(root, "今天我们测测这三台机器。")

        with pytest.raises(DubbingError) as exc_info:
            validate_canonical_dubbing_script(root, subtitle)

        assert exc_info.value.code == "DUB_TEXT_SUBTITLE_MISMATCH"
