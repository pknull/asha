#!/usr/bin/env python3
"""Composition tests for silence, calibration, and OpenCode policy adaptation."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
TOOLS = REPO / "plugins/session/tools"
SESSION_END = REPO / "plugins/session/hooks/handlers/session-end.sh"
OPENCODE_POLICY = REPO / "plugins/session/hooks/handlers/opencode-policy-adapter.sh"
SAVE_SESSION = REPO / "plugins/session/tools/save-session.sh"
sys.path.insert(0, str(TOOLS))


class SessionEndSilenceTests(unittest.TestCase):
    def test_silence_persists_and_prevents_clean_exit_save(self):
        with tempfile.TemporaryDirectory(prefix="asha_session_end_") as tmp:
            root = Path(tmp)
            project = root / "project"
            marker_dir = project / "Work/markers"
            marker_dir.mkdir(parents=True)
            (project / ".asha").mkdir()
            (project / ".asha/config.json").write_text("{}")
            silence = marker_dir / "silence"
            silence.touch()

            plugin = root / "plugin"
            (plugin / "tools").mkdir(parents=True)
            sentinel = root / "automatic-save-ran"
            save = plugin / "tools/save-session.sh"
            save.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
            save.chmod(0o755)

            env = os.environ.copy()
            env.update({
                "HOME": str(root),
                "CLAUDE_PROJECT_DIR": str(project),
                "CLAUDE_PLUGIN_ROOT": str(plugin),
                "ASHA_HARNESS": "claude",
            })
            proc = subprocess.run(
                ["bash", str(SESSION_END)],
                input=json.dumps({"reason": "logout", "session_id": "sid"}),
                text=True, capture_output=True, env=env, timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(silence.exists(), "silence must persist across SessionEnd")
            self.assertFalse(sentinel.exists(), "silenced SessionEnd must not launch save")

    def test_direct_save_script_stops_before_any_mode(self):
        with tempfile.TemporaryDirectory(prefix="asha_direct_save_") as tmp:
            project = Path(tmp) / "project"
            (project / "Memory").mkdir(parents=True)
            marker = project / "Work/markers/silence"
            marker.parent.mkdir(parents=True)
            marker.touch()
            proc = subprocess.run(
                ["bash", str(SAVE_SESSION), "--archive-only"],
                text=True, capture_output=True, timeout=10,
                env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("skipped", proc.stderr)


class CalibrationPolicyTests(unittest.TestCase):
    def setUp(self):
        import pattern_analyzer  # type: ignore[reportMissingImports]
        self.pa = pattern_analyzer

    def test_default_synthesis_policy_disables_calibration(self):
        with mock.patch.object(self.pa, "_calibration_capture_enabled", wraps=self.pa._calibration_capture_enabled) as enabled:
            self.assertFalse(self.pa._calibration_capture_enabled(False))
            enabled.assert_called_once_with(False)

    def test_explicit_calibration_honors_global_config(self):
        with tempfile.TemporaryDirectory(prefix="asha_calibration_") as tmp:
            config = Path(tmp) / "config.json"
            with mock.patch.dict(os.environ, {"ASHA_CONFIG": str(config)}, clear=False):
                config.write_text('{"capture_calibration": false}')
                self.assertFalse(self.pa._calibration_capture_enabled(True))
                config.write_text('{"capture_calibration": true}')
                self.assertTrue(self.pa._calibration_capture_enabled(True))

    def test_automatic_default_never_calls_global_writers(self):
        events = [{
            "session_id": "sid", "timestamp": "2026-07-11T00:00:00Z",
            "type": "context", "subtype": "decision",
            "payload": {"detail": "I prefer concise output"}, "metadata": {},
        }]
        with tempfile.TemporaryDirectory(prefix="asha_auto_policy_") as tmp:
            active = Path(tmp) / "activeContext.md"
            with (
                mock.patch.object(self.pa, "ACTIVE_CONTEXT", active),
                mock.patch.object(self.pa, "_rebuild_events_from_transcript", return_value=(1, "sid")),
                mock.patch.object(self.pa, "load_events", return_value=events),
                mock.patch.object(self.pa, "load_existing_patterns", return_value={}),
                mock.patch.object(self.pa, "generate_active_context", return_value="# Active\n"),
                mock.patch.object(self.pa, "_was_manually_edited", return_value=False),
                mock.patch.object(self.pa, "_write_synthhash"),
                mock.patch.object(self.pa, "synthesize_learnings", return_value=[]),
                mock.patch.object(self.pa, "detect_learnable_patterns", return_value=[]),
                mock.patch.object(self.pa, "add_learnings_via_manager"),
                mock.patch.object(self.pa, "extract_calibration_signals", return_value={
                    "voice": [{"text": "voice", "category": "style", "timestamp": ""}],
                    "keeper": [{"text": "keeper", "category": "preference", "timestamp": ""}],
                }) as extract,
                mock.patch.object(self.pa, "append_to_voice") as voice,
                mock.patch.object(self.pa, "append_to_keeper") as keeper,
            ):
                result = self.pa.run_synthesis(session_id="sid", skip_eval=True)
                self.assertEqual(result["status"], "success")
                extract.assert_not_called()
                voice.assert_not_called()
                keeper.assert_not_called()

                with mock.patch.object(self.pa, "_calibration_capture_enabled", return_value=True):
                    explicit = self.pa.run_synthesis(
                        session_id="sid", skip_eval=True, capture_calibration=True
                    )
                self.assertEqual(explicit["calibration_signals"], {"voice": 1, "keeper": 1})
                voice.assert_called_once()
                keeper.assert_called_once()


class OpenCodePolicyAdapterTests(unittest.TestCase):
    def _run(self, payload):
        return subprocess.run(
            ["bash", str(OPENCODE_POLICY)], input=json.dumps(payload), text=True,
            capture_output=True, timeout=10,
        )

    def test_policy_ask_degrades_to_deny(self):
        proc = self._run({
            "sessionID": "ses_test", "tool": "bash",
            "args": {"command": "find /home"},
        })
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BLOCKED", proc.stderr)

    def test_secret_file_is_denied(self):
        proc = self._run({"tool": "read", "args": {"filePath": "/tmp/.env"}})
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Secrets file access denied", proc.stderr)

    def test_unmatched_tool_is_allowed(self):
        proc = self._run({"tool": "bash", "args": {"command": "printf ok"}})
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
