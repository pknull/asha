"""Explicit-save identity resolution across native harness seams."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2] / "plugins" / "session" / "tools"
sys.path.insert(0, str(TOOLS))

import recovery_state  # noqa: E402
import save_identity  # noqa: E402


class SaveIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".asha").mkdir()
        (self.root / ".asha/config.json").write_text(json.dumps({
            "memory_version": 2, "project_id": "p1"
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolves_native_environment_seams_in_required_order(self):
        values = {
            "ASHA_SESSION_ID": "asha", "CLAUDE_CODE_SESSION_ID": "claude",
            "CODEX_THREAD_ID": "codex",
        }
        with mock.patch.dict(os.environ, values, clear=True):
            self.assertEqual("asha", save_identity.resolve(self.root, "opencode"))
        values.pop("ASHA_SESSION_ID")
        with mock.patch.dict(os.environ, values, clear=True):
            self.assertEqual("claude", save_identity.resolve(self.root, "claude"))
        values.pop("CLAUDE_CODE_SESSION_ID")
        with mock.patch.dict(os.environ, values, clear=True):
            self.assertEqual("codex", save_identity.resolve(self.root, "codex"))

    def test_copilot_falls_back_to_latest_current_recovery_snapshot(self):
        recovery_state.update(self.root, {
            "harness": "copilot", "session_id": "copilot-native", "event": "start"
        })
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual("copilot-native", save_identity.resolve(self.root, "copilot"))

    def test_missing_or_redacted_identity_fails_closed(self):
        with mock.patch.dict(os.environ, {"ASHA_SESSION_ID": "   "}, clear=True):
            with self.assertRaisesRegex(ValueError, "identity"):
                save_identity.resolve(self.root, "claude")


if __name__ == "__main__":
    unittest.main()
