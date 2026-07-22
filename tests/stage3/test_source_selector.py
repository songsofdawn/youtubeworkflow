from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from src.stage3.source_selector import select_source


class SourceSelectorTests(TestCase):
    def test_manual_vtt_has_priority_and_chinese_is_never_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            subtitles = Path(directory) / "subtitles"; subtitles.mkdir()
            for name in ("zh.auto.srt", "en.auto.vtt", "en.manual.vtt"):
                (subtitles / name).write_text("content", encoding="utf-8")
            self.assertEqual(select_source(directory).name, "en.manual.vtt")
