from __future__ import annotations

import json
import unittest
from pathlib import Path

from lib.control import cli, doctor, prepare
from lib.control import __doc__ as control_doc
from lib.control.tui import TuiModel


ROOT = Path(__file__).resolve().parents[2]


class Issue60CleanupContractTests(unittest.TestCase):
    def test_orphan_control_symbols_and_tui_run_selection_state_are_absent(self) -> None:
        self.assertFalse(hasattr(doctor, "_not_probed"))
        self.assertFalse(hasattr(cli, "UnavailableAdapters"))
        self.assertFalse(hasattr(TuiModel, "select_run"))
        self.assertNotIn("selected_run_id", vars(TuiModel([])))

    def test_module_docstrings_describe_current_responsibilities(self) -> None:
        self.assertNotIn("Increment", control_doc or "")
        self.assertNotIn("Increment", prepare.__doc__ or "")
        self.assertIn("task workspace", (prepare.__doc__ or "").casefold())

    def test_opencode_audit_renderer_disables_force(self) -> None:
        source = (ROOT / "bin/asha-drift-check.sh").read_text(encoding="utf-8")
        function = source.split("source_opencode_renderer() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("DRY_RUN=0 VERBOSE=0 FORCE=0", function)
        self.assertNotIn("FORCE=1", function)

    def test_process_liveness_claims_name_process_liveness_tests(self) -> None:
        capabilities = json.loads(
            (ROOT / "harnesses/capabilities.json").read_text(encoding="utf-8")
        )
        expected = (
            "python test_control_increment3.HarnessAdapterTests/"
            "LiveAdapterEvidenceTests"
        )
        for harness in ("copilot", "opencode"):
            with self.subTest(harness=harness):
                verifier = capabilities["harnesses"][harness]["capabilities"][
                    "control-status"
                ]["verifier"]
                self.assertEqual(verifier, expected)
                self.assertNotIn("doctor:tmux", verifier)


if __name__ == "__main__":
    unittest.main()
