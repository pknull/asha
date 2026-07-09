#!/usr/bin/env python3
"""
Tests for save_preflight.gate_wwa_provenance (issue #2: bg /save handoff gap).

The lead "What Was Accomplished" in activeContext.md must belong to the current
session. A read-only / RCON / Bash-edit session emits no Edit/Write events, so
the synthesizer generates no WWA and the curated merge leaves the *previous*
session's WWA as the lead. ac_fresh (mtime-only) never notices. This gate hard-
fails that case — but only when the session actually did work; a genuinely empty
session has nothing to hand off and must not be blocked.
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


class WWAProvenanceGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asha_preflight_")
        self.project = Path(self.tmp) / "project"
        (self.project / "Memory" / "events").mkdir(parents=True)
        self._saved_env = {"ASHA_EVENTS_FILE": os.environ.get("ASHA_EVENTS_FILE")}
        # Force the gate to read the project's own events file.
        os.environ.pop("ASHA_EVENTS_FILE", None)
        for mod in ("save_preflight",):
            sys.modules.pop(mod, None)
        import save_preflight  # type: ignore[reportMissingImports]  # noqa: E402
        self.sp = save_preflight

    def tearDown(self):
        for key, prior in self._saved_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------- #
    def _write_ac(self, wwa_header, stamp=None, body="- did things"):
        lines = ["---", 'version: "2.0"', "---", "", "# Active Context", "",
                 f"## {wwa_header}", ""]
        if stamp:
            lines.append(f"<!-- wwa-session: {stamp} -->")
        lines += [body, "", "## Next Steps", "", "- [ ] next", ""]
        (self.project / "Memory" / "activeContext.md").write_text("\n".join(lines))

    def _write_events(self, sid, n=3):
        ev = self.project / "Memory" / "events" / "events.jsonl"
        with open(ev, "w") as f:
            for i in range(n):
                f.write(json.dumps({"session_id": sid, "type": "tool", "subtype": "x", "id": i}) + "\n")

    def _gate(self, sid):
        return self.sp.gate_wwa_provenance(self.project, sid)

    # -- cases ------------------------------------------------------------- #
    def test_unknown_session_id_hard_fails_session_integrity(self):
        self._write_events("sess-CUR", 2)
        r = self.sp.gate_session_integrity(self.project, None)
        self.assertEqual(r.status, "fail")
        self.assertTrue(r.hard)

    def test_stamped_current_passes(self):
        self._write_ac("What Was Accomplished (2026-06-22 — work)", stamp="sess-CUR")
        self._write_events("sess-CUR", 5)
        r = self._gate("sess-CUR")
        self.assertEqual(r.status, "pass")
        self.assertFalse(r.status == "fail")

    def test_foreign_stamp_with_activity_hard_fails(self):
        self._write_ac("What Was Accomplished (2026-06-20 — prior)", stamp="sess-PRIOR")
        self._write_events("sess-CUR", 4)  # this session did real work
        r = self._gate("sess-CUR")
        self.assertEqual(r.status, "fail")
        self.assertTrue(r.hard)
        self.assertIn("sess-CUR", r.detail)

    def test_no_stamp_with_activity_hard_fails(self):
        self._write_ac("What Was Accomplished (2026-06-20 — prior)", stamp=None)
        self._write_events("sess-CUR", 2)
        r = self._gate("sess-CUR")
        self.assertEqual(r.status, "fail")
        self.assertTrue(r.hard)
        self.assertIn("no session stamp", r.detail)

    def test_foreign_stamp_empty_session_passes(self):
        # No events for the current session -> nothing to hand off -> not blocked.
        self._write_ac("What Was Accomplished (2026-06-20 — prior)", stamp="sess-PRIOR")
        self._write_events("sess-OTHER", 3)  # events, but none are current
        r = self._gate("sess-CUR")
        self.assertEqual(r.status, "pass")
        self.assertFalse(r.hard)

    def test_unknown_session_id_warns_not_blocks(self):
        self._write_ac("What Was Accomplished (2026-06-20 — prior)", stamp="sess-PRIOR")
        self._write_events("sess-PRIOR", 3)
        r = self._gate(None)
        self.assertEqual(r.status, "warn")
        self.assertFalse(r.hard)

    def test_no_wwa_section_passes(self):
        (self.project / "Memory" / "activeContext.md").write_text(
            "---\nversion: \"2.0\"\n---\n\n# Active Context\n\n## Next Steps\n\n- [ ] x\n")
        self._write_events("sess-CUR", 3)
        r = self._gate("sess-CUR")
        self.assertEqual(r.status, "pass")
        self.assertFalse(r.hard)

    def test_missing_active_context_passes_here(self):
        # ac_fresh owns the missing-file hard fail; this gate must not double-report.
        self._write_events("sess-CUR", 3)
        r = self._gate("sess-CUR")
        self.assertEqual(r.status, "pass")
        self.assertFalse(r.hard)

    def test_lead_section_only_first_wwa_is_checked(self):
        # A stamped-current WWA lower in the file does NOT rescue a stale lead.
        text = (
            "---\nversion: \"2.0\"\n---\n\n# Active Context\n\n"
            "## What Was Accomplished (2026-06-20 — prior)\n\n"
            "<!-- wwa-session: sess-PRIOR -->\n\n- prior\n\n"
            "## What Was Accomplished (2026-06-22 — current, but not the lead)\n\n"
            "<!-- wwa-session: sess-CUR -->\n\n- current\n\n"
            "## Next Steps\n\n- [ ] x\n"
        )
        (self.project / "Memory" / "activeContext.md").write_text(text)
        self._write_events("sess-CUR", 3)
        r = self._gate("sess-CUR")
        self.assertEqual(r.status, "fail")
        self.assertTrue(r.hard)


if __name__ == "__main__":
    unittest.main()
