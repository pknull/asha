"""Source contracts for the encounter-design skill's damage reporting."""

from pathlib import Path
import re
import unittest


SKILL = Path(__file__).resolve().parents[2] / "plugins/rpg/skills/encounter-design/SKILL.md"


class EncounterDamageReportingTests(unittest.TestCase):
    def test_damage_reporting_makes_targets_sides_and_round_horizon_auditable(self):
        text = re.sub(r"\s+", " ", SKILL.read_text(encoding="utf-8"))
        match = re.search(
            r"(?is)###\s+damage reporting\s+(.{0,700})",
            text,
        )
        self.assertIsNotNone(match, "the skill needs an explicit damage-reporting contract")
        contract = match.group(1).lower()

        for required in (
            "receiving side",
            "target allocation or targeting policy",
            "action order",
            "round horizon",
            "attacks that resolve before each foe is defeated",
            "never state an exact total",
            "chosen script reports it",
            "deterministic action sequence",
            "these inputs make it auditable",
            "otherwise say the total is unavailable",
        ):
            with self.subTest(required=required):
                self.assertIn(required, contract)


if __name__ == "__main__":
    unittest.main()
