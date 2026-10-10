"""Run the Python examples in skill references so they cannot go stale.

Each fenced ```python block is executed in a fresh namespace. Assertions in a
block check its results; print-only blocks are execution smoke tests.
Needs rpg-dice 0.5.0+ and rpg-card 0.3.0+ importable; a reference is
skipped when its library is missing.
"""
import importlib.util
import re
import unittest
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2] / "plugins/rpg/skills"
REFERENCES = {
    "mechanic-analysis/references/cards.md": "card_picker",
    "mechanic-analysis/references/simulation.md": "dice_roller",
}
BLOCK = re.compile(r"^```python\n(.*?)^```$", re.S | re.M)


def available(module):
    return importlib.util.find_spec(module) is not None


class ReferenceSnippetTests(unittest.TestCase):
    def test_snippets_run(self):
        for rel, module in REFERENCES.items():
            path = SKILLS / rel
            with self.subTest(reference=rel):
                self.assertTrue(path.exists(), f"{rel} is missing")
                if not available(module):
                    self.skipTest(f"{module} not importable")
                blocks = BLOCK.findall(path.read_text(encoding="utf-8"))
                self.assertTrue(blocks, f"{rel} has no python examples")
                for i, code in enumerate(blocks, 1):
                    with self.subTest(reference=rel, block=i):
                        exec(compile(code, f"{rel} block {i}", "exec"), {"__name__": "snippet"})


if __name__ == "__main__":
    unittest.main()
