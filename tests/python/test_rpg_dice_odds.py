"""Tests for plugins/rpg/skills/mechanic-analysis/scripts/dice_odds.py.

Needs rpg-dice 0.5.0 or later importable (installed, or a checkout on
PYTHONPATH); skipped otherwise.
"""
import importlib.util
import io
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "plugins/rpg/skills/mechanic-analysis/scripts/dice_odds.py"
spec = importlib.util.spec_from_file_location("dice_odds", SCRIPT)
assert spec is not None and spec.loader is not None
do = importlib.util.module_from_spec(spec)
spec.loader.exec_module(do)

USABLE = do.DiceThrower is not None and hasattr(do.DiceProbability, "exact")


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = do.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(USABLE, "rpg-dice 0.5.0 or later not importable")
class DiceOddsTests(unittest.TestCase):
    def test_refuses_what_rpg_dice_rejects(self):
        for expr in ("2d20kh1=+5t>=15", "1d20+1d4t>=10", "d20"):
            with self.subTest(expr=expr):
                with self.assertRaises(do.Refused):
                    do.analyse(expr, 200, 1)

    def test_pool_sums_and_dice_as_values_are_exact(self):
        self.assertEqual(do.analyse("1d8+1d6", 2000, 1)["exact"]["mean"], 8)
        first = do.analyse("3d6=+1d4", 2000, 1)["exact"]["mean"]
        self.assertEqual(first, 13)
        self.assertEqual(do.analyse("3d6=+1d4", 2000, 2)["exact"]["mean"], first)

    def test_exact_values(self):
        cases = {"2d20=+5kh1t>=15": Fraction(319, 400), "(2d20kh1)=+5t>=15": Fraction(319, 400),
                 "4d{-1,0,1}=+2t>=3": Fraction(31, 81), "1d100t<=45": Fraction(9, 20)}
        for expr, want in cases.items():
            with self.subTest(expr=expr):
                r = do.analyse(expr, 2000, 1)
                self.assertEqual(r["exact"]["pass"], want)
                self.assertEqual(r["warnings"], [])

    def test_groups_give_mixed_pools(self):
        r = do.analyse("(1d10>=6)+(1d8>=6)", 2000, 1)
        self.assertEqual(do.any_success(r), Fraction(11, 16))

    def test_zero_dice(self):
        r = do.analyse("0d6>=6", 500, 1)
        self.assertEqual(r["exact"]["mean"], 0)
        self.assertEqual(r["sampled"]["mean"], 0)

    def test_explosions_are_exact_with_unresolved_reported(self):
        r = do.analyse("3d6x", 5000, 7)
        self.assertGreater(r["exact"]["unresolved"], 0)
        self.assertLess(r["exact"]["unresolved"], Fraction(1, 10**6))
        # A d6 exploding on 6 has mean 4.2, so three dice average 12.6.
        self.assertLess(abs(r["exact"]["mean"] - Fraction(63, 5)), Fraction(1, 10**4))
        self.assertEqual(r["warnings"], [])

    def test_impossible_success_stays_impossible_under_heavy_explosion(self):
        # No d10 face reaches 11, so no success is possible however the dice explode.
        r = do.analyse("1d10>=11x>=2", 500, 1)
        self.assertEqual(do.any_success(r), 0)
        self.assertGreater(r["exact"]["unresolved"], Fraction(1, 10))
        code, out, _ = run("1d10>=11x>=2", "--samples", "500")
        self.assertIn("WARNING", out)
        self.assertIn("up to", out)
        self.assertNotIn("chance below", out)  # the caution prints after the chances it qualifies

    def test_rare_successes_a_small_sample_misses_are_not_a_disagreement(self):
        # 1/100 is exact; a 100-roll sample without a success (seeds 2 and 4) is ordinary luck.
        for seed in (2, 4):
            with self.subTest(seed=seed):
                r = do.analyse("1d100>=100", 100, seed)
                self.assertEqual(r["sampled"]["success"].get(1, 0), 0)
                self.assertEqual(r["warnings"], [])

    def test_rare_passes_a_small_sample_misses_are_not_a_disagreement(self):
        self.assertEqual(do.analyse("1d100t>=100", 100, 2)["warnings"], [])
        self.assertEqual(do.analyse("6d6t>=36", 2000, 1)["warnings"], [])

    def test_explosions_beyond_the_depth_are_not_a_disagreement(self):
        # Sampled successes can exceed the exact figure by the unresolved mass.
        for expr in ("1d10>=10x>=2", "2d6>=6x>=3"):
            with self.subTest(expr=expr):
                self.assertEqual(do.analyse(expr, 2000, 1)["warnings"], [])

    def test_the_success_check_still_catches_a_wrong_exact_chance(self):
        def old_reading(r):  # 1 - P(0 successes), which counts unresolved chains as successes
            return 1 - r["exact"]["success"].get(0, Fraction(0))
        with unittest.mock.patch.object(do, "any_success", old_reading):
            r = do.analyse("1d10>=11x>=2", 1000, 1)
        self.assertTrue(any("P(1+ success)" in w for w in r["warnings"]))

    def test_json_with_bands_parses(self):
        import json
        code, out, err = run("2d6", "--bands", "7,10", "--json", "--samples", "100")
        self.assertEqual(code, 0, err)
        bands = json.loads(out)[0]["exact"]["bands"]
        self.assertEqual(bands[2][1]["fraction"], "1/6")

    def test_fail_counts_are_exact(self):
        r = do.analyse("5d6>=6f<=1", 2000, 3)
        self.assertEqual(r["exact"]["fail"][0], Fraction(5, 6) ** 5)

    def test_per_die_modifier_note(self):
        for expr in ("2d6+3", "2d6 + 3", "(2d6+3)t>=10"):
            with self.subTest(expr=expr):
                r = do.analyse(expr, 500, 1)
                self.assertTrue(any("each die" in n for n in r["notes"]))
        for expr in ("1d8+1d6", "1d20+5", "2d6=+3", "2d20kh1+5"):
            with self.subTest(expr=expr):
                self.assertFalse(do.analyse(expr, 500, 1)["notes"])
        self.assertEqual(do.analyse("2d6+3", 500, 1)["exact"]["mean"], 13)

    def test_cli_compares_and_refuses(self):
        code, out, _ = run("1d20=+5t>=15", "2d20=+5kh1t>=15", "--samples", "2000")
        self.assertEqual(code, 0)
        self.assertIn("comparison against the first expression", out)
        self.assertIn("P(pass) +0.2475", out)
        code, _, err = run("2d20kh1=+5t>=15")
        self.assertEqual(code, 2)
        self.assertIn("refused", err)

    def test_reports_chance_of_any_success(self):
        code, out, _ = run("4d6>=6f<=1", "5d6>=6f<=1", "--samples", "1000")
        self.assertEqual(code, 0)
        self.assertIn("P(1+ success) 0.5177 (671/1296)", out)
        self.assertIn("P(1+ success) +0.0804", out)

    def test_long_tails_are_summarised(self):
        code, out, _ = run("5d10>=8x", "--samples", "500")
        self.assertEqual(code, 0)
        self.assertNotIn("0.000000", out)
        self.assertRegex(out, r"\+\d+ values below 0\.0001")

    def test_success_lines_only_with_a_success_rule(self):
        for expr in ("1d20=+4t>=16", "1d6x", "2d6=+1", "3d6x>=5", "2d20kh1>=15", "4d6dl1>=10", "12d6kh10>=40"):
            with self.subTest(expr=expr):
                code, out, _ = run(expr, "--samples", "300")
                self.assertEqual(code, 0)
                self.assertNotIn("successes", out)
                self.assertNotIn("P(1+ success)", out)
        for expr in ("5d6>=6", "4d{-1,0,1}>=1", "(1d10>=6)+(1d8>=6)", "2d6>5"):
            with self.subTest(expr=expr):
                _, out, _ = run(expr, "--samples", "300")
                self.assertIn("P(1+ success)", out)
        _, out, _ = run("1d20=+5t>=15", "2d20=+5kh1t>=15", "--samples", "300")
        self.assertNotIn("P(1+ success)", out)

    def test_bands(self):
        code, out, _ = run("2d6=+0", "2d6=+1", "--bands", "7,10", "--samples", "300")
        self.assertEqual(code, 0)
        self.assertIn("6 or less: 0.4167 (5/12), 7-9: 0.4167 (5/12), 10 or more: 0.1667 (1/6)", out)
        self.assertIn("6 or less: 0.2778 (5/18), 7-9: 0.4444 (4/9), 10 or more: 0.2778 (5/18)", out)
        self.assertIn("bands 6 or less -0.1389, 7-9 +0.0278, 10 or more +0.1111", out)
        code, out, _ = run("4d{-1,0,1}=+0", "--bands", "-1,1", "--samples", "300")
        self.assertEqual(code, 0)
        self.assertIn("-2 or less: 0.1852 (5/27), -1 to 0: 0.4321 (35/81), 1 or more: 0.3827 (31/81)", out)
        code, _, err = run("2d6", "--bands", "10,7")
        self.assertEqual(code, 2)
        self.assertIn("--bands", err)

    def test_help_shows_usage_notes(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            do.main(["--help"])
        self.assertIn("0.5.0 notation", out.getvalue())

    def test_json_output(self):
        code, out, _ = run("1d6", "--json", "--samples", "500")
        self.assertEqual(code, 0)
        self.assertIn('"unresolved"', out)


class VersionTests(unittest.TestCase):
    def test_old_rpg_dice_is_reported(self):
        saved = do.DiceProbability

        class Old:
            pass

        do.DiceProbability = Old
        try:
            code, _, err = run("1d6")
        finally:
            do.DiceProbability = saved
        self.assertEqual(code, 2)
        self.assertIn("0.5.0", err)


if __name__ == "__main__":
    unittest.main()
