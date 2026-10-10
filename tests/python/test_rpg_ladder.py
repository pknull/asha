"""Tests for plugins/rpg/skills/mechanic-design/scripts/ladder.py.

Needs rpg-dice 0.5.0 or later importable; skipped otherwise.
"""
import importlib.util
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "plugins/rpg/skills/mechanic-design/scripts/ladder.py"
spec = importlib.util.spec_from_file_location("ladder", SCRIPT)
assert spec is not None and spec.loader is not None
lad = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lad)

USABLE = lad.DiceProbability is not None and hasattr(lad.DiceProbability, "exact")


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = lad.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TemplateTests(unittest.TestCase):
    def test_fill_handles_signs(self):
        self.assertEqual(lad.fill("2d6=+R", -1), "2d6=-1")
        self.assertEqual(lad.fill("2d6=+R", 2), "2d6=+2")
        self.assertEqual(lad.fill("1d20=-R", -3), "1d20=+3")
        self.assertEqual(lad.fill("Rd6>=5", 4), "4d6>=5")

    def test_range(self):
        self.assertEqual(lad.parse_range("-1..3"), [-1, 0, 1, 2, 3])
        for bad in ("3..1", "x", "1-3"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    lad.parse_range(bad)


@unittest.skipUnless(USABLE, "rpg-dice 0.5.0 or later not importable")
class LadderTests(unittest.TestCase):
    def test_pool_any_success(self):
        rows = lad.ladder("Rd6>=5", [1, 2, 3, 4, 5], None)
        got = [row["any_success"] for row in rows]
        self.assertEqual(got, [Fraction(1, 3), Fraction(5, 9), Fraction(19, 27), Fraction(65, 81), Fraction(211, 243)])

    def test_pass_and_steps(self):
        code, out, _ = run("1d20=+Rt>=15", "--range", "0..2")
        self.assertEqual(code, 0)
        self.assertIn("| 0 | 1d20=+0t>=15 | 10.50 | 0.3000 |", out)
        self.assertIn("| 2 | 1d20=+2t>=15 | 12.50 | 0.4000 | +0.0500 |", out)

    def test_bands(self):
        code, out, _ = run("2d6=+R", "--range", "-1..3", "--bands", "7,10")
        self.assertEqual(code, 0)
        self.assertIn("| -1 | 2d6=-1 | 6.00 | 0.5833 | 0.3333 | 0.0833 |", out)
        self.assertIn("| 3 | 2d6=+3 | 10.00 | 0.0833 | 0.3333 | 0.5833 |", out)

    def test_success_bands(self):
        code, out, _ = run("Rd8>=7", "--range", "1..3", "--success-bands", "1,2")
        self.assertEqual(code, 0)
        self.assertIn("| 0 successes | 1 success | 2 or more successes |", out)
        self.assertIn("| 1 | 1d8>=7 | 4.50 | 0.2500 | 0.7500 | 0.2500 | 0.0000 |", out)
        self.assertIn("| 3 | 3d8>=7 | 13.50 | 0.5781 | 0.4219 | 0.4219 | 0.1562 |", out)
        self.assertEqual(run("2d6=+R", "--range", "0..1", "--success-bands", "1,2")[0], 2)

    def test_help_shows_usage_notes(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            lad.main(["--help"])
        self.assertIn("uppercase R", out.getvalue())

    def test_heavy_explosion_does_not_invent_successes(self):
        rows = lad.ladder("Rd10>=11x>=2", [1], None)
        self.assertEqual(rows[0]["any_success"], 0)

    def test_unresolved_mass_is_not_called_exact(self):
        code, out, err = run("Rd10>=11x>=2", "--range", "1..1")
        self.assertEqual(code, 0, err)
        footer = out.strip().splitlines()[-1]
        self.assertIn("resolved outcomes", footer)
        self.assertIn("higher by up to", footer)
        self.assertTrue(run("Rd6>=5", "--range", "1..2")[1].strip().endswith("Exact figures from rpg-dice."))

    def test_ranges_are_capped_before_allocation(self):
        # Just past the cap, so a regression computes 201 rows instead of allocating a huge list.
        self.assertEqual(len(lad.parse_range("1..200")), 200)
        with self.assertRaises(ValueError):
            lad.parse_range("1..201")

    def test_errors(self):
        self.assertEqual(run("2d6", "--range", "0..2")[0], 2)
        self.assertEqual(run("Rd6>=5", "--range", "-1..2")[0], 2)
        self.assertEqual(run("Rd6>=5", "--range", "1..3", "--bands", "9,4")[0], 2)
        code, _, err = run("Rd6kh1=+5t>=15", "--range", "1..2")
        self.assertEqual(code, 2)
        self.assertIn("rejects", err)


class VersionTests(unittest.TestCase):
    def test_old_rpg_dice_is_reported(self):
        saved = lad.DiceProbability

        class Old:
            pass

        lad.DiceProbability = Old
        try:
            code, _, err = run("Rd6>=5", "--range", "1..2")
        finally:
            lad.DiceProbability = saved
        self.assertEqual(code, 2)
        self.assertIn("0.5.0", err)


if __name__ == "__main__":
    unittest.main()
