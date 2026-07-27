#!/usr/bin/env python3
"""
Tests for orphaned-session detection (pattern_analyzer.check_orphaned_session).

Covers the copilot lifecycle wiring (issue #13) behavior contract:

- A session whose events remain in events.jsonl but whose synthesis never
  published (no wwa-session stamp in activeContext.md) IS an orphan — the
  copilot identity breadcrumb written at sessionStart is exactly this trail
  for crashed sessions, since copilot has no per-tool event capture.
- A session that already published (its wwa-session stamp is present in
  activeContext.md) is NOT an orphan, even though clean saves leave its
  transcript-derived events behind as the newest events in events.jsonl.
  Without this guard every session start after a clean save re-recovered
  (redundantly re-synthesized) the prior session, on every harness.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))


class OrphanDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha_orphan_")
        self.project = Path(self.tmp) / "project"
        (self.project / "Memory" / "events").mkdir(parents=True)

        self._saved_env = {"CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR")}
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.project)

        sys.modules.pop("pattern_analyzer", None)
        import pattern_analyzer  # type: ignore[reportMissingImports]  # noqa: E402
        self.pa = pattern_analyzer

    def tearDown(self):
        for key, prior in self._saved_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def _append_event(self, session_id, subtype="session_started"):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        row = {
            "id": f"evt_test_{session_id}",
            "timestamp": now,
            "session_id": session_id,
            "type": "event",
            "subtype": subtype,
            "payload": {"detail": "test event"},
            "metadata": {"source": "test", "project_dir": str(self.project), "tool_name": None},
        }
        with open(self.project / "Memory" / "events" / "events.jsonl", "a") as f:
            f.write(json.dumps(row) + "\n")

    def _write_active_context(self, wwa_session_ids):
        stamps = "\n".join(f"<!-- wwa-session: {sid} -->" for sid in wwa_session_ids)
        (self.project / "Memory" / "activeContext.md").write_text(
            f"# Active Context\n\n## What Was Accomplished\n\n{stamps}\n- work\n"
        )

    # tests
    def test_unsynthesized_session_is_orphan(self):
        # Crash trail: breadcrumb present, synthesis never published its stamp.
        self._write_active_context(["old-session-a"])
        self._append_event("crashed-session-b")
        self.assertEqual(
            self.pa.check_orphaned_session("current-session-c"), "crashed-session-b")

    def test_cleanly_saved_session_is_not_orphan(self):
        # Clean save leaves events behind AND publishes the wwa stamp.
        self._write_active_context(["saved-session-b"])
        self._append_event("saved-session-b", subtype="file_created")
        self.assertIsNone(self.pa.check_orphaned_session("current-session-c"))

    def test_current_session_is_not_its_own_orphan(self):
        # Resumed session: last event carries the same id as current.
        self._append_event("same-session")
        self.assertIsNone(self.pa.check_orphaned_session("same-session"))

    def test_no_events_no_orphan(self):
        self._write_active_context(["old-session-a"])
        self.assertIsNone(self.pa.check_orphaned_session("current-session-c"))

    def test_missing_active_context_still_detects_orphan(self):
        # No activeContext at all (fresh project): the guard must fail open.
        self._append_event("crashed-session-b")
        self.assertEqual(
            self.pa.check_orphaned_session("current-session-c"), "crashed-session-b")


if __name__ == "__main__":
    unittest.main()
