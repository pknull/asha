#!/usr/bin/env python3
"""
Tests for activeContext.md backup behaviour in pattern_analyzer.

Covers the test plan from the backup-fix prompt:
  1. Auto-synth file (matches sidecar hash) → no backup created.
  2. Manually edited file (hash diverges) → backup created in cache dir.
  3. Backup path is OUTSIDE the project's Memory tree.
  4. Helpers are import-safe.
"""

import os
import sys
import shutil
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))


class ActiveContextBackupTests(unittest.TestCase):
    """Validate manual-edit detection + cache-dir backup placement."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha_pa_backup_")
        self.project = Path(self.tmp) / "project"
        (self.project / "Memory" / "events").mkdir(parents=True)
        self.cache = Path(self.tmp) / "cache"
        self.cache.mkdir()

        self._saved_env = {
            "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR"),
            "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME"),
        }
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)
        os.environ["XDG_CACHE_HOME"] = str(self.cache)

        # Force a fresh import that picks up the patched env.
        for mod in ("pattern_analyzer",):
            sys.modules.pop(mod, None)
        import pattern_analyzer  # type: ignore[reportMissingImports]  # noqa: E402
        self.pa = pattern_analyzer

    def tearDown(self):
        for key, prior in self._saved_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        sys.modules.pop("pattern_analyzer", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_active_context(self, body: str) -> Path:
        path = self.pa.ACTIVE_CONTEXT
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def test_unchanged_file_skips_backup(self):
        """Hash matches sidecar → no manual edits → no backup."""
        body = "# active\nsynthesizedFrom: \"events\"\n"
        self._write_active_context(body)
        self.pa._write_synthhash(body)

        self.assertFalse(self.pa._was_manually_edited(body))

    def test_manual_edit_triggers_backup_in_cache(self):
        """Hash diverges → manual edits detected → cache backup created."""
        original = "# active\nsynthesizedFrom: \"events\"\n"
        self._write_active_context(original)
        self.pa._write_synthhash(original)

        edited = original + "\nUser added this paragraph.\n"
        self.assertTrue(self.pa._was_manually_edited(edited))

        backup = self.pa._backup_active_context(edited)
        self.assertTrue(backup.exists())
        self.assertEqual(backup.read_text(), edited)

    def test_backup_path_outside_project_memory(self):
        """Backup must NOT live inside the project's tracked Memory/ dir."""
        backup = self.pa._backup_active_context("anything")

        memory_dir = (self.project / "Memory").resolve()
        backup_resolved = backup.resolve()
        self.assertFalse(
            str(backup_resolved).startswith(str(memory_dir) + os.sep),
            f"backup leaked into Memory: {backup_resolved}",
        )
        self.assertTrue(
            str(backup_resolved).startswith(str(self.cache.resolve()) + os.sep),
            f"backup not under XDG cache: {backup_resolved}",
        )

    def test_legacy_substring_fallback_when_sidecar_missing(self):
        """Without a sidecar, fall back to the prior substring rule."""
        # No _write_synthhash called → sidecar missing → legacy heuristic applies.
        with_marker = "# active\nsynthesizedFrom: \"events\"\nbody\n"
        without_marker = "# active\nbody only, no marker\n"
        self.assertFalse(self.pa._was_manually_edited(with_marker))
        self.assertTrue(self.pa._was_manually_edited(without_marker))


if __name__ == "__main__":
    unittest.main()
