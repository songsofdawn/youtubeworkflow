from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from src.portable_runtime import load_portable_manifest, resolve_python_executable


class PortableRuntimeTests(TestCase):
    def test_portable_runtime_has_priority_over_development_environments(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            portable = root / "runtime" / "python" / "python.exe"
            development = root / ".venv" / "Scripts" / "python.exe"
            portable.parent.mkdir(parents=True)
            development.parent.mkdir(parents=True)
            portable.write_bytes(b"portable")
            development.write_bytes(b"development")
            self.assertEqual(resolve_python_executable(root), portable.resolve())

    def test_unified_environment_is_the_first_development_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            unified = root / ".venv" / "Scripts" / "python.exe"
            legacy = root / ".venv_stage3" / "Scripts" / "python.exe"
            unified.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            unified.write_bytes(b"unified")
            legacy.write_bytes(b"legacy")
            self.assertEqual(resolve_python_executable(root), unified.resolve())

    def test_missing_runtime_can_be_reported_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            self.assertIsNone(resolve_python_executable(name, required=False))

    def test_manifest_describes_portable_edition(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "portable_manifest.json").write_text(
                json.dumps({"portable": True, "edition": "cpu"}),
                encoding="utf-8",
            )
            manifest = load_portable_manifest(root)
            self.assertTrue(manifest["portable"])
            self.assertEqual(manifest["edition"], "cpu")
