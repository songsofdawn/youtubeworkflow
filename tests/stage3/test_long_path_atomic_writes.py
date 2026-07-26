from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.stage3.models import SubtitleSegment
from src.stage3.subtitle_writer import atomic_write_json, atomic_write_srt


class LongPathAtomicWriteTests(unittest.TestCase):
    def _long_parent(self, root: Path) -> Path:
        filler_length = max(1, 205 - len(str(root)) - 1)
        return root / ("x" * filler_length)

    def test_json_checkpoint_uses_short_temporary_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = self._long_parent(Path(temporary))
            destination = parent / "batch_0001_polished.json"
            self.assertLess(len(str(destination)), 260)
            self.assertGreater(
                len(str(destination)) + len(destination.name) + 35,
                260,
            )
            atomic_write_json(destination, {"status": "success"})
            self.assertTrue(destination.is_file())
            self.assertEqual(list(parent.glob(".tmp-*")), [])

    def test_srt_output_uses_short_temporary_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = self._long_parent(Path(temporary))
            destination = parent / "zh.reviewed.srt"
            atomic_write_srt(
                destination,
                [SubtitleSegment(1, 0.0, 1.0, "Hello")],
            )
            self.assertIn("Hello", destination.read_text(encoding="utf-8"))
            self.assertEqual(list(parent.glob(".tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
