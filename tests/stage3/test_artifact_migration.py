from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from src.stage3.artifact_migration import migrate_legacy_artifacts


class ArtifactMigrationTests(TestCase):
    def test_legacy_file_is_copied_and_migration_is_audited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "subtitles" / "en.clean.srt"
            legacy.parent.mkdir()
            legacy.write_text("legacy", encoding="utf-8")
            rows = migrate_legacy_artifacts(root)
            canonical = root / "subtitles" / "en.youtube.clean.srt"
            self.assertEqual(canonical.read_bytes(), legacy.read_bytes())
            self.assertTrue(rows)
            audit = json.loads((root / "stage3" / "migrations.json").read_text(encoding="utf-8"))
            self.assertEqual(audit[0]["migrated_from"], str(legacy))
            self.assertEqual(audit[0]["migrated_to"], str(canonical))
            self.assertIn("migration_time", audit[0])
