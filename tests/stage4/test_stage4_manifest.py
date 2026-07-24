from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.stage4.stage4_manifest import atomic_write_text


class Stage4ManifestTests(unittest.TestCase):
    def test_atomic_write_handles_long_windows_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            long_task = Path(temporary) / ("x" * 175)
            destination = long_task / "chinese_selection_report.json"
            atomic_write_text(destination, "{}\n")
            self.assertEqual(destination.read_text(encoding="utf-8"), "{}\n")
            self.assertEqual(
                [path for path in long_task.iterdir() if path.name.startswith(".tmp-")],
                [],
            )


if __name__ == "__main__":
    unittest.main()
