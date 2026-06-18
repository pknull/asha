#!/usr/bin/env python3
"""
Tests for Work/markers/silence honored by synthesis pipeline.

Hooks already check the marker before capturing events; before this patch,
the synthesis pipeline did NOT — a user running /silence on then /save
manually would still get activeContext/learnings/voice/keeper written.

Coverage:
- pattern_analyzer.run_synthesis returns skipped_silence and does NOT touch
  activeContext.md when Work/markers/silence is present.
- learnings_manager.write_learnings is a no-op when the marker is present
  (defense in depth for direct CLI callers).
- Both functions behave normally when the marker is absent (regression
  guard — the new guards must not break the happy path).
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))


class SilenceMarkerSynthesisTests(unittest.TestCase):
    """run_synthesis must early-return when Work/markers/silence is present."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha_silence_")
        self.project = Path(self.tmp) / "project"
        (self.project / "Memory" / "events").mkdir(parents=True)
        (self.project / "Work" / "markers").mkdir(parents=True)

        # Seed an events file with one synthetic event so happy-path synthesis
        # would normally proceed past the "no events" early-return.
        events_file = self.project / "Memory" / "events" / "events.jsonl"
        events_file.write_text(json.dumps({
            "timestamp": "2026-05-19T00:00:00Z",
            "session_id": "test-session",
            "type": "file_edit",
            "payload": {"path": "foo.py", "detail": "edit"},
        }) + "\n")

        self._saved_env = {
            "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR"),
            "HOME": os.environ.get("HOME"),
            "ASHA_EVENTS_FILE": os.environ.get("ASHA_EVENTS_FILE"),
        }
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)
        # Redirect HOME so ~/.asha writes land in tmp, not the real home.
        os.environ["HOME"] = str(self.tmp)
        os.environ.pop("ASHA_EVENTS_FILE", None)

        # Force fresh import so module-level path constants resolve against
        # the tmp project.
        for mod in ("pattern_analyzer", "learnings_manager"):
            sys.modules.pop(mod, None)
        import pattern_analyzer  # type: ignore[reportMissingImports]
        import learnings_manager  # type: ignore[reportMissingImports]
        self.pa = pattern_analyzer
        self.lm = learnings_manager

        # Rewire the learnings bundle dir to tmp so writes don't escape the
        # sandbox if the silence guard is broken.
        self.lm.LEARNINGS_DIR = Path(self.tmp) / ".asha" / "learnings"
        self.lm.LEARNINGS_PATH = Path(self.tmp) / ".asha" / "learnings.md"
        self.lm.LEARNINGS_DIR.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for key, prior in self._saved_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch_silence(self):
        (self.project / "Work" / "markers" / "silence").touch()

    # -------------------------------------------------------------------------
    # pattern_analyzer.run_synthesis
    # -------------------------------------------------------------------------

    def test_run_synthesis_skips_when_silence_marker_present(self):
        """run_synthesis must early-return with skipped_silence status."""
        self._touch_silence()
        result = self.pa.run_synthesis(session_id="test-session", days=7)

        self.assertEqual(result.get("status"), "skipped_silence")
        self.assertIn("silence marker", result.get("reason", ""))
        # No activeContext.md written — silence is honored end-to-end.
        self.assertFalse(
            self.pa.ACTIVE_CONTEXT.exists(),
            "activeContext.md must not be created during silence",
        )

    def test_run_synthesis_proceeds_without_silence_marker(self):
        """Regression guard: without the marker, synthesis runs normally."""
        result = self.pa.run_synthesis(session_id="test-session", days=7)

        # Either "success" (events processed) or "no_events" (synthetic event
        # rejected by downstream filters) — but NOT skipped_silence.
        self.assertNotEqual(result.get("status"), "skipped_silence")

    def test_silence_marker_added_mid_session_is_respected(self):
        """The marker is checked at run_synthesis call time, not import time.

        This protects the live workflow where a user toggles /silence on
        after the synthesis module is loaded by a long-running process.
        """
        # First call: no marker → not skipped.
        first = self.pa.run_synthesis(session_id="test-session", days=7)
        self.assertNotEqual(first.get("status"), "skipped_silence")

        # User runs /silence on mid-session.
        self._touch_silence()

        # Second call: marker now present → skipped.
        second = self.pa.run_synthesis(session_id="test-session", days=7)
        self.assertEqual(second.get("status"), "skipped_silence")

    # -------------------------------------------------------------------------
    # learnings_manager.write_learnings
    # -------------------------------------------------------------------------

    def test_write_learnings_noops_when_silence_marker_present(self):
        """Direct CLI callers must also be blocked by the silence marker."""
        self._touch_silence()

        # Construct a learning that, absent the marker, would persist to disk.
        learning = self.lm.Learning(
            id="test-learning",
            category="Test Category",
            confidence=0.7,
            trigger="trigger condition",
            action="action to take",
        )
        self.lm.write_learnings({"Test Category": [learning]})

        self.assertFalse(
            self.lm._learning_path("test-learning").exists(),
            "concept file must not be written during silence",
        )
        existing = (list(self.lm.LEARNINGS_DIR.glob("*.md"))
                    if self.lm.LEARNINGS_DIR.exists() else [])
        self.assertEqual(existing, [], "no concept files may be written during silence")

    def test_write_learnings_writes_normally_without_marker(self):
        """Regression guard: without marker, write_learnings persists."""
        learning = self.lm.Learning(
            id="test-learning",
            category="Test Category",
            confidence=0.7,
            trigger="trigger condition",
            action="action to take",
        )
        self.lm.write_learnings({"Test Category": [learning]})

        path = self.lm._learning_path("test-learning")
        self.assertTrue(path.exists())
        content = path.read_text()
        self.assertIn("Test Category", content)
        self.assertIn("test-learning", content)

    def test_write_learnings_fails_open_outside_project(self):
        """When no project root is detectable, the guard must fail open
        (not block writes). Out-of-project CLI usage should still work.
        """
        # Strip env var and chdir somewhere with no Memory/ ancestor.
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        outside = Path(self.tmp) / "no-memory-here"
        outside.mkdir()
        old_cwd = os.getcwd()
        os.chdir(outside)
        try:
            learning = self.lm.Learning(
                id="oop-learning",
                category="OOP Category",
                confidence=0.5,
                trigger="t",
                action="a",
            )
            # Even with the marker file present in the original project,
            # write_learnings called from an out-of-project CWD should not
            # see it (no project root detected → fail open).
            (self.project / "Work" / "markers" / "silence").touch()
            self.lm.write_learnings({"OOP Category": [learning]})

            self.assertTrue(
                self.lm._learning_path("oop-learning").exists(),
                "fail-open: out-of-project callers should still write",
            )
        finally:
            os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
