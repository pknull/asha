"""Tests for plugins/rpg/skills/mechanic-analysis/scripts/skirmish.py.

Deterministic fights (every chance 1) pin the turn order and targeting rules;
sampled fights are checked against exact races from rounds_until.py. Needs
rpg-dice 0.5.0 or later importable for the dice cases.
"""
import importlib.util
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path

DIR = Path(__file__).resolve().parents[2] / "plugins/rpg/skills/mechanic-analysis/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sk = load("skirmish")
ru = load("rounds_until")
USABLE = ru.DiceProbability is not None


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = sk.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def result(*units, order="listed", target="first", fights=200, seed=1):
    return sk.simulate([sk.parse_unit(u) for u in units], order=order, target=target, fights=fights, seed=seed)


class DeterministicTests(unittest.TestCase):
    def test_listed_order_decides_an_even_trade(self):
        r = result("side=A; name=Ann; hp=10; attack=1:5", "side=B; name=Bo; hp=10; attack=1:5")
        self.assertEqual((r["wins"]["A"], r["wins"]["B"], r["mean_rounds"]), (1.0, 0.0, 2.0))
        r = result("side=B; name=Bo; hp=10; attack=1:5", "side=A; name=Ann; hp=10; attack=1:5")
        self.assertEqual(r["wins"]["B"], 1.0)

    def test_a_downed_unit_does_not_act(self):
        # Ann drops Bo with her first attack, so Bo never strikes back.
        r = result("side=A; name=Ann; hp=1; attack=1:5", "side=B; name=Bo; hp=5; attack=1:5")
        self.assertEqual((r["wins"]["A"], r["standing"]["Ann"]), (1.0, 1.0))

    def test_targeting_policy_changes_the_winner(self):
        units = ("side=A; name=Duelist; hp=2; attack=1:1", "side=A; name=Squire; hp=1",
                 "side=B; name=Brute; hp=3; attack=1:1")
        self.assertEqual(result(*units, target="weakest")["wins"]["A"], 1.0)
        self.assertEqual(result(*units, target="first")["wins"]["B"], 1.0)

    def test_two_attacks_a_round(self):
        r = result("side=A; name=Ann; hp=10; attack=1:2; attack=1:3", "side=B; name=Bo; hp=10")
        self.assertEqual(r["mean_rounds"], 2.0)

    def test_any_down_per_side(self):
        r = result("side=A; name=Ann; hp=10; attack=1:5", "side=B; name=Bo; hp=10; attack=1:5")
        self.assertEqual((r["any_down"]["A"], r["any_down"]["B"]), (0.0, 1.0))

    def test_count_ladder_finds_the_tipping_point(self):
        units = [sk.parse_unit("side=A; name=Ann; hp=4; attack=1:5"),
                 sk.parse_unit("side=B; name=Goblin; hp=3; attack=1:1")]
        rows = sk.count_ladder(units, "Goblin", range(1, 6), fights=50, seed=1)
        self.assertEqual([r["wins"]["A"] for r in rows], [1.0, 1.0, 1.0, 0.0, 0.0])
        self.assertEqual([r["count"] for r in rows], [1, 2, 3, 4, 5])
        with self.assertRaises(ValueError):
            sk.count_ladder(units, "Orc", range(1, 3), fights=10, seed=1)

    def test_targeting_per_side(self):
        units = ("side=A; name=Duelist; hp=2; attack=1:1", "side=A; name=Squire; hp=1",
                 "side=B; name=Brute; hp=3; attack=1:1")
        self.assertEqual(result(*units, target="A=first,B=weakest")["wins"]["A"], 1.0)
        self.assertEqual(result(*units, target="A=weakest,B=first")["wins"]["B"], 1.0)
        for bad in ("A=first", "A=first,B=best", "C=first,B=first"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                result(*units, target=bad)

    def test_count_from_zero_and_hp_ladder(self):
        units = [sk.parse_unit("side=A; name=Ann; hp=1; attack=1:5"),
                 sk.parse_unit("side=B; name=Ogre; hp=5; attack=1:1"),
                 sk.parse_unit("side=B; name=Goblin; hp=3; attack=1:1")]
        rows = sk.ladder(units, count=("Goblin", range(0, 2)), fights=20, seed=1)
        self.assertEqual([r["count"] for r in rows], [0, 1])
        self.assertEqual([r["wins"]["A"] for r in rows], [1.0, 0.0])
        rows = sk.ladder(units[:2], hp=("Ogre", ["5", "10"]), fights=20, seed=1)
        self.assertEqual([(r["hp"], r["wins"]["A"]) for r in rows], [("5", 1.0), ("10", 0.0)])
        grid = sk.ladder(units, count=("Goblin", range(0, 2)), hp=("Ogre", ["5", "10"]), fights=20, seed=1)
        self.assertEqual(len(grid), 4)
        with self.assertRaises(ValueError):
            sk.ladder(units[:2], count=("Ogre", range(0, 1)), fights=5, seed=1)

    def test_rounds_when_side_a_wins(self):
        r = result("side=A; name=Ann; hp=10; attack=1:5", "side=B; name=Bo; hp=10; attack=1:5")
        self.assertEqual(r["mean_rounds_a_wins"], 2.0)
        self.assertEqual(r["hp_left_a_wins"], {"Ann": 5.0})

    def test_initiative_rolled_once_per_fight(self):
        r = result("side=A; name=Ann; hp=10; attack=1:5", "side=B; name=Bo; hp=10; attack=1:5",
                   order="once", fights=4000, seed=3)
        self.assertLess(abs(r["wins"]["A"] - 0.5), 4 * r["se"]["A"])

    def test_undecided_fights_keep_their_final_state(self):
        r = result("side=A; name=Ann; hp=5", "side=B; name=Bo; hp=5", fights=10)
        self.assertEqual(r["standing"], {"Ann": 1.0, "Bo": 1.0})
        self.assertEqual(r["any_down"], {"A": 0.0, "B": 0.0})
        rows = sk.ladder([sk.parse_unit("side=A; name=Ann; hp=5"), sk.parse_unit("side=B; name=Bo; hp=5")],
                         hp=("Bo", ["5"]), fights=5, seed=1)
        self.assertIn("undecided", sk.ladder_report(rows, None, "Bo"))

    def test_stalemate_is_undecided(self):
        r = result("side=A; name=Ann; hp=5", "side=B; name=Bo; hp=5", fights=10)
        self.assertEqual(r["undecided"], 1.0)


class ExplosionTests(unittest.TestCase):
    @unittest.skipUnless(USABLE, "rpg-dice 0.5.0 or later not importable")
    def test_heavy_explosion_is_refused_not_renormalised(self):
        with self.assertRaises(ValueError):
            sk.parse_unit("side=A; name=Hero; hp=1; attack=1:1d10x>=2")

    @unittest.skipUnless(USABLE, "rpg-dice 0.5.0 or later not importable")
    def test_ordinary_exploding_attacks_are_accepted(self):
        unit = sk.parse_unit("side=A; name=Hero; hp=10; attack=1/2:5d10>=8x>=8")
        self.assertEqual(sum(unit["attacks"][0][1].values()), 1)

    @unittest.skipUnless(USABLE, "rpg-dice 0.5.0 or later not importable")
    def test_json_keeps_its_shape_when_explosion_notes_exist(self):
        hero, foe = "side=A; name=Hero; hp=10; attack=1/2:1d6x", "side=B; name=Foe; hp=10; attack=1/2:1d6"
        code, out, err = run("--unit", hero, "--unit", foe, "--fights", "200", "--json")
        self.assertEqual(code, 0, err)
        r = json.loads(out)
        self.assertEqual(r["fights"], 200)
        self.assertTrue(any("1d6x" in n for n in r["notes"]))
        code, out, err = run("--unit", hero, "--unit", foe, "--fights", "200", "--count", "Foe=1..2", "--json")
        self.assertEqual(code, 0, err)
        rows = json.loads(out)
        self.assertEqual([row["count"] for row in rows], [1, 2])
        self.assertTrue(all(row["notes"] for row in rows))
        code, out, err = run("--unit", foe.replace("B", "A").replace("Foe", "Ann"), "--unit", foe,
                             "--fights", "200", "--json")
        self.assertEqual(json.loads(out)["notes"], [])


class ParsingTests(unittest.TestCase):
    def test_bad_units_are_refused(self):
        for text in ("name=Ann; hp=5", "side=C; name=Ann; hp=5", "side=A; hp=5", "side=A; name=Ann",
                     "side=A; name=Ann; hp=5; speed=3", "side=A; name=Ann; hp=0", "side=A name=Ann hp=5"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                sk.parse_unit(text)

    def test_cli_refusals(self):
        for argv in (["--unit", "side=A; name=Ann; hp=5; attack=1:1"],
                     ["--unit", "side=A; name=Ann; hp=5", "--unit", "side=B; name=Ann; hp=5"],
                     ["--unit", "side=A; name=Ann; hp=5", "--unit", "side=B; name=Bo; hp=5", "--fights", "0"],
                     ["--unit", "side=A; name=Ann; hp=5", "--unit", "side=B; name=Bo; hp=5", "--target", "best"]):
            with self.subTest(argv=argv):
                code, _, err = run(*argv)
                self.assertEqual(code, 2)
                self.assertIn("refused", err)


@unittest.skipUnless(USABLE, "rpg-dice 0.5.0 or later not importable")
class SampledTests(unittest.TestCase):
    FIGHTER = "side=A; name=Fighter; hp=28; attack=9/20:1d8=+3, 1/20:2d8=+3"
    FRIEND = "side=A; name=Friend; hp=28; attack=9/20:1d8=+3, 1/20:2d8=+3"
    OGRE = "side=B; name=Ogre; hp=5d8=+10; attack=3/5:2d6=+4"

    def exact_race(self, attackers):
        a = ru.analyse([ru.attack_distribution("9/20:1d8=+3, 1/20:2d8=+3")] * attackers,
                       ru.target_distribution("5d8=+10"), 200)["reached_by"]
        b = ru.analyse([ru.attack_distribution("3/5:2d6=+4")], {28: Fraction(1)}, 200)["reached_by"]
        return float(ru.race(a, b, "first")["a"])

    def test_duel_matches_the_exact_race(self):
        r = result(self.FIGHTER, self.OGRE, fights=40000, seed=7)
        self.assertLess(abs(r["wins"]["A"] - self.exact_race(1)), 4 * r["se"]["A"])

    def test_focused_fighter_survival_matches_the_exact_race(self):
        # The ogre focuses the fighter; she stands at the end exactly when the ogre drops first.
        r = result(self.FIGHTER, self.FRIEND, self.OGRE, fights=40000, seed=11)
        p, n = r["standing"]["Fighter"], r["fights"]
        se = (p * (1 - p) / n) ** 0.5
        self.assertLess(abs(p - self.exact_race(2)), 4 * se)

    def test_cli_reports_seed_and_errors(self):
        code, out, err = run("--unit", self.FIGHTER, "--unit", self.OGRE, "--fights", "2000", "--seed", "3")
        self.assertEqual(code, 0, err)
        self.assertIn("seed 3", out)
        self.assertIn("side A wins", out)
        self.assertIn("standard error", out)
        code, out, _ = run("--unit", self.FIGHTER, "--unit", self.OGRE, "--fights", "500", "--json")
        self.assertEqual(json.loads(out)["fights"], 500)
        code, out, err = run("--unit", self.FIGHTER, "--unit", self.OGRE, "--count", "Ogre=1..3", "--fights", "500")
        self.assertEqual(code, 0, err)
        self.assertIn("| Ogre count | side A wins |", out)
        self.assertEqual(run("--unit", self.FIGHTER, "--unit", self.OGRE, "--count", "Ogre=3..1")[0], 2)
        code, out, err = run("--unit", self.FIGHTER, "--unit", self.OGRE, "--hp-ladder", "Ogre=30,40",
                             "--fights", "500")
        self.assertEqual(code, 0, err)
        self.assertIn("| Ogre hp |", out)


if __name__ == "__main__":
    unittest.main()
