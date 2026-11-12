"""verify.py: `--file` selects the quick level, and refuses a standard or full level."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY = REPO_ROOT / "plugins/code/tools/verify.py"

FAKE_RUFF = """#!/bin/sh
printf '%s\\n' "$*" >> "$RUFF_LOG"
exit 0
"""


class VerifyFileFlagTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        # A Python project whose only checker on PATH is a fake ruff that
        # records how it was called, so no real tool runs.
        self.project = base / "project"
        self.project.mkdir()
        (self.project / "pyproject.toml").write_text("[project]\nname = 'probe'\n", encoding="utf-8")
        self.target = self.project / "probe.py"
        self.target.write_text("x = 1\n", encoding="utf-8")
        bin_dir = base / "bin"
        bin_dir.mkdir()
        ruff = bin_dir / "ruff"
        ruff.write_text(FAKE_RUFF, encoding="utf-8")
        ruff.chmod(0o755)
        self.ruff_log = base / "ruff.log"
        self.env = {"PATH": str(bin_dir), "RUFF_LOG": str(self.ruff_log), "LANG": "C.UTF-8"}

    def _verify(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(VERIFY), "--root", str(self.project), "--json", "--no-parallel", *args],
            capture_output=True, text=True, env=self.env, cwd=self.project, timeout=60,
        )

    def _ruff_calls(self) -> list[str]:
        if not self.ruff_log.exists():
            return []
        return self.ruff_log.read_text(encoding="utf-8").splitlines()

    def test_file_alone_runs_quick_checks_on_that_file(self):
        result = self._verify("--file", str(self.target))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["level"], "quick")
        self.assertEqual(self._ruff_calls(), [f"format --check {self.target}"])

    def test_file_with_an_explicit_quick_level_is_accepted(self):
        for flags in (["--quick"], ["--level", "quick"]):
            with self.subTest(flags=flags):
                self.ruff_log.unlink(missing_ok=True)
                result = self._verify("--file", str(self.target), *flags)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["level"], "quick")
                self.assertEqual(self._ruff_calls(), [f"format --check {self.target}"])

    def test_file_with_a_standard_or_full_level_is_refused(self):
        for flags in (["--full"], ["-f"], ["--level", "standard"], ["--level", "full"], ["-l", "full"]):
            with self.subTest(flags=flags):
                self.ruff_log.unlink(missing_ok=True)
                result = self._verify("--file", str(self.target), *flags)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertIn("--file", result.stderr)
                self.assertIn("quick", result.stderr)
                self.assertEqual(self._ruff_calls(), [])  # refused before any check ran

    def test_no_file_keeps_the_standard_default(self):
        result = self._verify()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["level"], "standard")
        self.assertEqual(self._ruff_calls(), ["check ."])


if __name__ == "__main__":
    unittest.main()
