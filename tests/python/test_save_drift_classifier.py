#!/usr/bin/env python3
"""Issue #7: classify and recover save transcript/event drift."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent.parent / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS_DIR))


class SaveDriftClassifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asha_drift_"))
        self.project = self.tmp / "project"
        (self.project / "Memory" / "events").mkdir(parents=True)
        (self.project / "Work" / "markers").mkdir(parents=True)
        self.saved_env = {key: os.environ.get(key) for key in (
            "HOME", "CLAUDE_PROJECT_DIR", "ASHA_HARNESS", "ASHA_SESSION_ID",
            "ASHA_TRANSCRIPT_PATH", "CLAUDE_CODE_SESSION_ID", "ASHA_EVENTS_FILE",
        )}
        os.environ.update({
            "HOME": str(self.tmp),
            "CLAUDE_PROJECT_DIR": str(self.project),
            "ASHA_HARNESS": "claude",
            "ASHA_SESSION_ID": "sess-current",
            "CLAUDE_CODE_SESSION_ID": "sess-current",
        })
        os.environ.pop("ASHA_EVENTS_FILE", None)
        for module in ("pattern_analyzer", "jsonl_reader", "save_preflight"):
            sys.modules.pop(module, None)
        import pattern_analyzer  # type: ignore
        import save_preflight  # type: ignore
        self.pa = pattern_analyzer
        self.sp = save_preflight

    def tearDown(self):
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _transcript(self, path: Path, sid: str, tools=("Read",), prompt="Inspect server state"):
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        rows = [
            {"type": "file-history-snapshot", "sessionId": sid, "timestamp": ts},
            {"type": "last-prompt", "lastPrompt": prompt, "sessionId": sid},
        ]
        for index, tool in enumerate(tools):
            args = {"command": "git status"} if tool == "Bash" else {
                "file_path": str(self.project / "src" / "file.py"),
                "old_string": "a", "new_string": "b",
            }
            rows.append({
                "type": "assistant", "timestamp": ts, "sessionId": sid,
                "cwd": str(self.project),
                "message": {"content": [{
                    "type": "tool_use", "id": f"tool-{index}",
                    "name": tool, "input": args,
                }]},
            })
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def _exact_path(self, sid: str) -> Path:
        slug = str(self.project.resolve()).replace("/", "-")
        return self.tmp / ".claude" / "projects" / slug / f"{sid}.jsonl"

    def _write_prior_wwa(self, sid="sess-foreign"):
        (self.project / "Memory" / "activeContext.md").write_text(
            "# Active Context\n\n"
            "## What Was Accomplished (prior)\n\n"
            f"<!-- wwa-session: {sid} -->\n- prior work\n\n"
            "## Next Steps\n\n- [ ] retain this\n"
        )

    def test_wrong_transcript_auto_pins_exact_current_session(self):
        foreign = self._transcript(self.tmp / "foreign.jsonl", "sess-foreign", ("Edit",))
        correct = self._transcript(self._exact_path("sess-current"), "sess-current", ("Edit",))
        os.environ["ASHA_TRANSCRIPT_PATH"] = str(foreign)
        self._write_prior_wwa("sess-prior")

        decision = self.pa.classify_drift(self.project)

        self.assertEqual(decision.classification, "WRONG_TRANSCRIPT")
        self.assertEqual(decision.identity.transcript_path, correct)

        result = self.pa.run_synthesis(
            session_id="sess-current", project_dir=self.project, skip_eval=True
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["drift"], "WRONG_TRANSCRIPT→auto-pinned")
        self.assertEqual(
            self.sp.gate_session_integrity(self.project, "sess-current").status, "pass"
        )
        self.assertEqual(
            self.sp.gate_wwa_provenance(self.project, "sess-current").status, "pass"
        )
        import jsonl_reader  # type: ignore
        self.assertEqual(
            jsonl_reader.resolve_identity(self.project).transcript_path,
            correct,
            "post-hoc preflight identity resolution must retain the auto-pin",
        )

    def test_exact_path_and_event_stamps_classify_clean(self):
        transcript = self._transcript(
            self._exact_path("sess-current"), "sess-current", ("Edit",)
        )
        os.environ["ASHA_TRANSCRIPT_PATH"] = str(transcript)

        decision = self.pa.classify_drift(self.project)

        self.assertEqual(decision.classification, "CLEAN")
        self.assertFalse(decision.needs_minimal_wwa)

    def test_bash_only_session_gets_stamped_minimal_lead_wwa(self):
        transcript = self._transcript(
            self._exact_path("sess-current"), "sess-current", ("Bash", "Read")
        )
        os.environ["ASHA_TRANSCRIPT_PATH"] = str(transcript)
        self._write_prior_wwa()

        result = self.pa.run_synthesis(
            session_id="sess-current", project_dir=self.project, skip_eval=True
        )

        self.assertEqual(result["status"], "success")
        self.assertIn("NO_EDIT_EVENTS", result["drift"])
        text = (self.project / "Memory" / "activeContext.md").read_text()
        self.assertEqual(self.sp._lead_wwa_section(text).count("wwa-session: sess-current"), 1)
        self.assertIn("Tool activity: Bash, Read.", text)
        self.assertIn("wwa-session: sess-foreign", text)
        self.assertEqual(
            self.sp.gate_wwa_provenance(self.project, "sess-current").status, "pass"
        )

    def test_concurrent_session_is_preserved_below_new_lead(self):
        transcript = self._transcript(
            self._exact_path("sess-current"), "sess-current", ("Edit",)
        )
        os.environ["ASHA_TRANSCRIPT_PATH"] = str(transcript)
        self._write_prior_wwa()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        (self.project / "Memory" / "events" / "events.jsonl").write_text(json.dumps({
            "timestamp": now, "session_id": "sess-foreign", "subtype": "decision",
        }) + "\n")

        result = self.pa.run_synthesis(
            session_id="sess-current", project_dir=self.project, skip_eval=True
        )

        self.assertEqual(result["drift"], "CONCURRENT→prepended-preserving-concurrent")
        text = (self.project / "Memory" / "activeContext.md").read_text()
        self.assertLess(text.index("wwa-session: sess-current"), text.index("wwa-session: sess-foreign"))
        self.assertIn("retain this", text)

    def test_unclassifiable_mismatch_without_exact_transcript_hard_fails(self):
        foreign = self._transcript(self.tmp / "foreign.jsonl", "sess-foreign", ("Edit",))
        os.environ["ASHA_TRANSCRIPT_PATH"] = str(foreign)

        with self.assertRaisesRegex(RuntimeError, "UNCLASSIFIABLE"):
            self.pa.classify_drift(self.project)

    def test_project_save_lock_serializes_processes(self):
        script = (
            "import sys; from pathlib import Path; "
            f"sys.path.insert(0, {str(TOOLS_DIR)!r}); "
            "import pattern_analyzer as pa; "
            "\nwith pa._project_save_lock(Path(sys.argv[1])):\n print('acquired', flush=True)"
        )
        env = os.environ.copy()
        env["CLAUDE_PROJECT_DIR"] = str(self.project)
        with self.pa._project_save_lock(self.project):
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(self.project)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            time.sleep(0.15)
            self.assertIsNone(child.poll(), "second save must wait for the project lock")
        stdout, stderr = child.communicate(timeout=3)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), "acquired")


if __name__ == "__main__":
    unittest.main()
