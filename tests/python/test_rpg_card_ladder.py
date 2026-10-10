"""Tests for plugins/rpg/skills/mechanic-design/scripts/card_ladder.py.

Needs rpg-card 0.3.0 or later importable; skipped otherwise.
"""
import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "plugins/rpg/skills/mechanic-design/scripts/card_ladder.py"
spec = importlib.util.spec_from_file_location("card_ladder", SCRIPT)
assert spec is not None and spec.loader is not None
cl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cl)

USABLE = cl.Odds is not None


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cl.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class ExpressionTests(unittest.TestCase):
    def test_draw_expressions(self):
        self.assertEqual(cl.draws("R", 3), 3)
        self.assertEqual(cl.draws("R+1", 2), 3)
        self.assertEqual(cl.draws("2*R-1", 2), 3)
        self.assertEqual(cl.draws("3", 9), 3)
        for bad in ("R**2", "__import__('os')", "r", "R/2"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    cl.draws(bad, 2)

    def test_condition_language_rejects_code(self):
        for bad in ("__import__('os').system('true')", "c.value", "open('x')", "value.__class__", "[x for x in (1,)]",
                    "name == 'a' * 999999999", "suit in ('H' * 99999999,)", "x" * 300,
                    "name * 999999999 == ''", "suit + suit == ''", "-name == 1"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    cl.compile_condition(bad)


@unittest.skipUnless(USABLE, "rpg-card 0.3.0 or later not importable")
class CardLadderTests(unittest.TestCase):
    def test_hand_size_ladder(self):
        rows = cl.ladder("standard52", [1, 2, 3], draw="R", where="value >= 10")
        self.assertEqual([r["success"] for r in rows], [Fraction(4, 13), Fraction(116, 221), Fraction(748, 1105)])

    def test_bonus_ladder(self):
        rows = cl.ladder("standard52", [0, 1, 2], draw="1", best_target=10, bonus="R")
        self.assertEqual([r["success"] for r in rows], [Fraction(4, 13), Fraction(5, 13), Fraction(6, 13)])

    def test_at_least_k(self):
        rows = cl.ladder("standard52", [2], draw="R", where="suit == 'Hearts'", at_least=2)
        self.assertEqual(rows[0]["success"], Fraction(1, 17))

    def test_rating_in_condition(self):
        cond = "(arcana == 'minor' and value + 2*R >= 13) or (arcana == 'major' and value <= 10)"
        rows = cl.ladder("tarot", [1, 3, 5], draw="1", where=cond)
        self.assertEqual([r["success"] for r in rows], [Fraction(27, 78), Fraction(43, 78), Fraction(59, 78)])

    def test_rating_in_removal(self):
        rows = cl.ladder("standard52", [0, 1, 3], draw="1", where="value >= 10", remove="rank > 13 - R")
        self.assertEqual([r["success"] for r in rows], [Fraction(4, 13), Fraction(1, 4), Fraction(1, 10)])

    def test_options_that_would_be_ignored_are_refused(self):
        for kwargs in ({"where": "value > 1", "at_least": 0}, {"where": "value > 1", "at_least": -1},
                       {"best_target": 10, "at_least": 2}, {"where": "value > 1", "bonus": "R"}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    cl.ladder("standard52", [1], draw="1", **kwargs)

    def test_tarot_majors(self):
        rows = cl.ladder("majors", [3], draw="R", where="value >= 15")
        self.assertEqual(rows[0]["success"], Fraction(31, 44))

    def test_total_target_and_removal(self):
        rows = cl.ladder("standard", [2], draw="R", total_target=20, remove="name == 'Joker'")
        pairs = [(a, b) for a in range(52) for b in range(52) if a != b]
        vals = [r for r in range(1, 14) for _ in range(4)]
        want = Fraction(sum(1 for a, b in pairs if vals[a] + vals[b] >= 20), len(pairs))
        self.assertEqual(rows[0]["success"], want)

    def test_replacement(self):
        rows = cl.ladder("standard52", [2], draw="R", where="value >= 10", replacement=True)
        self.assertEqual(rows[0]["success"], 1 - Fraction(9, 13) ** 2)

    def test_table_and_count_bands(self):
        code, out, _ = run("--deck", "standard52", "--range", "1..3", "--draw", "R", "--where", "value >= 10",
                           "--count-bands", "1,2")
        self.assertEqual(code, 0)
        self.assertIn("| R | cards | P(success) | 0 matches | 1 match | 2 or more matches | change in P(success) |", out)
        self.assertIn("| 1 | 1 | 0.3077 | 0.6923 | 0.3077 | 0.0000 |  |", out)
        self.assertIn("| 2 | 2 | 0.5249 |", out)

    def test_cli_errors(self):
        self.assertEqual(run("--deck", "standard52", "--range", "1..2", "--draw", "R")[0], 2)
        self.assertEqual(run("--deck", "nope", "--range", "1..2", "--draw", "R", "--where", "value > 1")[0], 2)
        self.assertEqual(run("--deck", "majors", "--range", "20..23", "--draw", "R", "--where", "value > 1")[0], 2)
        code, _, err = run("--deck", "standard52", "--range", "1..2", "--draw", "R", "--where", "value.real > 1")
        self.assertEqual(code, 2)
        self.assertIn("refused", err)

    def test_ranges_are_capped_before_allocation(self):
        # Just past the cap, so a regression computes 201 rows instead of allocating a huge list.
        code, _, err = run("--deck", "standard52", "--range", "1..201", "--draw", "1", "--where", "value >= 10")
        self.assertEqual(code, 2)
        self.assertIn("refused", err)

    def test_runtime_errors_are_refused(self):
        for cond in ("value // 0 > 1", "suit > 3", "colour > 'a'"):
            with self.subTest(cond=cond):
                code, _, err = run("--deck", "standard", "--range", "1..1", "--draw", "R", "--where", cond)
                self.assertEqual(code, 2)
                self.assertIn("refused", err)
        self.assertEqual(run("--deck", "standard52", "--range", "1..1", "--draw", "'a' * 999999999",
                             "--where", "value > 1")[0], 2)

    def test_help_shows_usage_notes(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cl.main(["--help"])
        self.assertIn("rating R", out.getvalue())


if __name__ == "__main__":
    unittest.main()
