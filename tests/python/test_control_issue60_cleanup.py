from __future__ import annotations

import json
import unittest
from pathlib import Path

from lib.control import cli, doctor
from lib.control import __doc__ as control_doc


ROOT = Path(__file__).resolve().parents[2]


class Issue60CleanupContractTests(unittest.TestCase):
    def test_orphan_control_symbols_are_absent(self) -> None:
        self.assertFalse(hasattr(doctor, "_not_probed"))
        self.assertFalse(hasattr(cli, "UnavailableAdapters"))

    def test_module_docstrings_describe_current_responsibilities(self) -> None:
        self.assertNotIn("Increment", control_doc or "")

    def test_opencode_audit_renderer_disables_force(self) -> None:
        source = (ROOT / "bin/asha-drift-check.sh").read_text(encoding="utf-8")
        function = source.split("source_opencode_renderer() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("DRY_RUN=0 VERBOSE=0 FORCE=0", function)
        self.assertNotIn("FORCE=1", function)

    def test_control_status_claims_name_their_bridge_tests(self) -> None:
        capabilities = json.loads(
            (ROOT / "harnesses/capabilities.json").read_text(encoding="utf-8")
        )
        # The process-liveness adapter tests left with the task substrate
        # (L-b); each hub-session bridge is verified by its own render test.
        verifiers = {
            "copilot": "tests:test-install/test-doctor",
            "opencode": "tests:test-opencode",
        }
        for harness, wanted in verifiers.items():
            with self.subTest(harness=harness):
                verifier = capabilities["harnesses"][harness]["capabilities"][
                    "control-status"
                ]["verifier"]
                self.assertEqual(verifier, wanted)
                self.assertNotIn("doctor:tmux", verifier)

    def test_copilot_activity_claims_match_the_installed_bridge(self) -> None:
        capabilities = json.loads(
            (ROOT / "harnesses/capabilities.json").read_text(encoding="utf-8")
        )
        copilot = capabilities["harnesses"]["copilot"]["capabilities"]
        session_profile = " ".join(
            copilot["session-profile"]["limitations"]
        )
        session_close = " ".join(copilot["session-close"]["limitations"])
        project_memory = " ".join(
            copilot["project-memory"]["limitations"]
        )

        self.assertIn("control-event.sh", copilot["control-status"]["surface"])
        self.assertNotIn("No Control session events are claimed", session_profile)
        self.assertNotIn("No Stop seam or activity hooks", session_close)
        self.assertNotIn("activity is unobserved", copilot["session-close"]["surface"])
        self.assertNotIn("no native activity bridge", project_memory)


if __name__ == "__main__":
    unittest.main()
