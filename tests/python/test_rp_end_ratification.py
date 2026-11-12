"""/rp:end ratifies canon per item with the Keeper even when Memory/invariants.md is absent."""

from __future__ import annotations

from pathlib import Path
import unittest


REPO = Path(__file__).resolve().parents[2]


def read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


class RatificationWithoutInvariantsTests(unittest.TestCase):
    def test_end_still_runs_the_ratifier_without_invariants(self):
        step5 = section(read("plugins/rp/commands/end.md"), "### Step 5", "\n### Step 6")
        self.assertNotRegex(step5, r"(?i)run\s+unguarded")
        self.assertNotRegex(step5, r"(?i)skip\s+ratification")
        self.assertIn("unchecked: no invariants", step5)
        self.assertIn("/rp:extract-invariants", step5)
        self.assertIn("subagent: rp-ratifier", step5)
        self.assertRegex(step5, r"(?i)per item")

    def test_canon_writer_promotes_only_accepted_items(self):
        step6 = section(read("plugins/rp/commands/end.md"), "### Step 6", "\n### Step 7")
        self.assertIn("Runs only on items the Keeper accepted in Step 5", step6)
        self.assertIn("Skip any item not in RATIFIED_ITEMS", step6)

    def test_ratifier_labels_items_unchecked_when_invariants_are_missing(self):
        text = read("plugins/rp/agents/rp-ratifier.md")
        step1 = section(text, "### Step 1", "\n### Step 2")
        self.assertIn("INVARIANTS_FILE", step1)
        self.assertIn("unchecked: no invariants", step1)
        self.assertIn("/rp:extract-invariants", step1)
        self.assertRegex(step1, r"(?i)still ask")
        self.assertRegex(text, r"(?i)static canon only")  # test_rp_gate's run-state guard


if __name__ == "__main__":
    unittest.main()
