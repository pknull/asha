"""Tests for plugins/rpg/skills/mechanic-analysis/scripts/rounds_until.py.

Expected values come from brute-force enumeration of whole fights, written
independently of the script's convolution. Needs rpg-dice 0.5.0 or later
importable for the dice cases; those are skipped otherwise.
"""
import importlib.util
import io
import itertools
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "plugins/rpg/skills/mechanic-analysis/scripts/rounds_until.py"
spec = importlib.util.spec_from_file_location("rounds_until", SCRIPT)
assert spec is not None and spec.loader is not None
ru = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ru)

USABLE = ru.DiceProbability is not None


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ru.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def brute_reached_by(attacks, target, rounds):
    """P(total >= target by the end of each round), by enumerating every fight.

    attacks: list of per-attack outcome lists [(probability, amount), ...]
    covering all outcomes (misses included). Each round every attack resolves once.
    """
    per_round = list(itertools.product(*attacks))
    reached = [Fraction(0)] * (rounds + 1)
    for path in itertools.product(per_round, repeat=rounds):
        p, total, done_at = Fraction(1), 0, None
        for n, outcome in enumerate(path, start=1):
            for prob, amount in outcome:
                p *= prob
                total += amount
            if done_at is None and total >= target:
                done_at = n
        if done_at is not None:
            for n in range(done_at, rounds + 1):
                reached[n] += p
    return reached[1:]


D4 = [(Fraction(1, 4), v) for v in range(1, 5)]


def half_hit_d4():
    return [(Fraction(1, 2), 0)] + [(p / 2, v) for p, v in D4]


class ParsingTests(unittest.TestCase):
    def test_chance_forms(self):
        self.assertEqual(ru.parse_chance("11/20"), Fraction(11, 20))
        self.assertEqual(ru.parse_chance("0.55"), Fraction(11, 20))
        self.assertEqual(ru.parse_chance("55%"), Fraction(11, 20))
        for bad in ("1.5", "-1/2", "120%"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ru.parse_chance(bad)

    def test_outcomes_split_at_top_level_commas_only(self):
        self.assertEqual(ru.split_outcomes("1/2:1d8=+3, 1/20:2d8=+3"), [("1/2", "1d8=+3"), ("1/20", "2d8=+3")])
        self.assertEqual(ru.split_outcomes("1/2:4d{-1,0,1}=+2"), [("1/2", "4d{-1,0,1}=+2")])
        self.assertEqual(ru.split_outcomes("1/3:(1d4,1d6)"), [("1/3", "(1d4,1d6)")])
        for bad in ("1d8", "1/2:", ":1d8", "1/2:1d8,"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ru.split_outcomes(bad)

    def test_constant_amounts_need_no_dice_engine(self):
        attack = ru.attack_distribution("1/2:2, 1/4:5")
        self.assertEqual(attack, {0: Fraction(1, 4), 2: Fraction(1, 2), 5: Fraction(1, 4)})

    def test_chances_over_one_are_refused(self):
        with self.assertRaises(ValueError):
            ru.attack_distribution("3/4:1, 1/2:2")


class ConvolutionTests(unittest.TestCase):
    def test_constant_damage_matches_brute_force(self):
        attacks = [[(Fraction(2, 5), 0), (Fraction(3, 5), 3)]]
        want = brute_reached_by(attacks, 7, 5)
        got = ru.reached_by([{0: Fraction(2, 5), 3: Fraction(3, 5)}], {7: Fraction(1)}, 5)
        self.assertEqual(got, want)

    def test_two_attacks_a_round_match_brute_force(self):
        a = [(Fraction(1, 3), 0), (Fraction(2, 3), 2)]
        b = [(Fraction(1, 2), 0), (Fraction(1, 4), 1), (Fraction(1, 4), 4)]
        want = brute_reached_by([a, b], 6, 4)
        got = ru.reached_by([dict((v, p) for p, v in a), dict((v, p) for p, v in b)], {6: Fraction(1)}, 4)
        self.assertEqual(got, want)

    def test_rolled_target_is_a_mixture(self):
        attacks = [half_hit_d4()]
        want = [Fraction(1, 2) * x + Fraction(1, 2) * y
                for x, y in zip(brute_reached_by(attacks, 3, 4), brute_reached_by(attacks, 6, 4))]
        dist = {}
        for p, v in half_hit_d4():
            dist[v] = dist.get(v, 0) + p
        got = ru.reached_by([dist], {3: Fraction(1, 2), 6: Fraction(1, 2)}, 4)
        self.assertEqual(got, want)

    def test_mean_rounds_of_a_certain_fixed_hit(self):
        result = ru.analyse([{5: Fraction(1)}], {12: Fraction(1)}, max_rounds=10)
        self.assertEqual(result["reached_by"], [0, 0, 1] + [1] * 7)
        self.assertEqual(result["mean"], 3)
        self.assertEqual(result["beyond"], 0)

    def test_geometric_mean_is_exact_to_the_cap(self):
        # One point on a 1/2 chance, target 1: rounds are geometric with mean 2.
        result = ru.analyse([{0: Fraction(1, 2), 1: Fraction(1, 2)}], {1: Fraction(1)}, max_rounds=60)
        self.assertEqual(result["beyond"], Fraction(1, 2 ** 60))
        self.assertIsNone(result["mean"])
        self.assertEqual(result["mean_at_least"], 2 - Fraction(2, 2 ** 60))

    def test_early_stop_reports_the_remainder(self):
        result = ru.analyse([{0: Fraction(1, 2), 1: Fraction(1, 2)}], {1: Fraction(1)}, max_rounds=100,
                            stop_below=Fraction(1, 1000))
        self.assertEqual(len(result["reached_by"]), 10)
        self.assertEqual(result["beyond"], Fraction(1, 1024))


class RaceTests(unittest.TestCase):
    def test_race_orders(self):
        # A needs exactly 2 rounds; B needs 1 or 3 rounds with equal chance.
        a = [Fraction(0), Fraction(1), Fraction(1)]
        b = [Fraction(1, 2), Fraction(1, 2), Fraction(1)]
        first = ru.race(a, b, order="first")
        self.assertEqual((first["a"], first["b"], first["both"]), (Fraction(1, 2), Fraction(1, 2), 0))
        second = ru.race(a, b, order="second")
        self.assertEqual((second["a"], second["b"]), (Fraction(1, 2), Fraction(1, 2)))
        same = ru.race([Fraction(1, 2), Fraction(1)], [Fraction(1, 2), Fraction(1)], order="together")
        self.assertEqual((same["a"], same["b"], same["both"]), (Fraction(1, 4), Fraction(1, 4), Fraction(1, 2)))

    def test_race_fight_length(self):
        # B drops A in round 1 half the time; otherwise A wins in round 2.
        result = ru.race([Fraction(0), Fraction(1), Fraction(1)], [Fraction(1, 2), Fraction(1, 2), Fraction(1)])
        self.assertEqual(result["mean_rounds"], Fraction(3, 2))
        self.assertEqual(result["median_rounds"], 1)
        self.assertEqual((result["p10_rounds"], result["p90_rounds"]), (1, 2))

    def test_shared_race_spread(self):
        got = ru.shared_race([ru.joint_distribution("1/6:2|0, 5/12:1|1, 5/12:0|1")], 6, 4, rounds=20)
        self.assertLessEqual(got["p10_rounds"], got["median_rounds"])
        self.assertLessEqual(got["median_rounds"], got["p90_rounds"])
        self.assertEqual((got["p10_rounds"], got["p90_rounds"]), (4, 5))

    def test_race_matches_brute_force_duel(self):
        # Each side hits half the time for 1; each needs 2. A acts first in each round.
        hit = [(Fraction(1, 2), 0), (Fraction(1, 2), 1)]
        a_wins = Fraction(0)
        for path in itertools.product(hit, repeat=12):
            p, a_dmg, b_dmg, winner = Fraction(1), 0, 0, None
            for i, (prob, amount) in enumerate(path):
                p *= prob
                if winner:
                    continue
                if i % 2 == 0:
                    a_dmg += amount
                    winner = "a" if a_dmg >= 2 else None
                else:
                    b_dmg += amount
                    winner = "b" if b_dmg >= 2 else None
            a_wins += p if winner == "a" else 0
        reached = ru.reached_by([{0: Fraction(1, 2), 1: Fraction(1, 2)}], {2: Fraction(1)}, 6)
        got = ru.race(reached, reached, order="first")
        self.assertEqual(got["a"], a_wins)


def brute_shared(outcomes, target_a, target_b, max_rolls):
    """Enumerate roll sequences where each outcome (p, a, b) advances both totals at once."""
    a_first = b_first = both = Fraction(0)
    for path in itertools.product(outcomes, repeat=max_rolls):
        p, a, b = Fraction(1), 0, 0
        for prob, da, db in path:  # every roll's probability counts; totals freeze once the race ends
            p *= prob
            if a < target_a and b < target_b:
                a, b = a + da, b + db
        if a >= target_a and b >= target_b:
            both += p
        elif a >= target_a:
            a_first += p
        elif b >= target_b:
            b_first += p
    return a_first, b_first, both


HEIST = [(Fraction(1, 6), 2, 0), (Fraction(5, 12), 1, 1), (Fraction(5, 12), 0, 1)]


class SharedRollTests(unittest.TestCase):
    def test_joint_outcomes_parse(self):
        dist = ru.joint_distribution("1/6:2|0, 5/12:1|1, 5/12:0|1")
        self.assertEqual(dist, {(2, 0): Fraction(1, 6), (1, 1): Fraction(5, 12), (0, 1): Fraction(5, 12)})
        self.assertEqual(ru.joint_distribution("1/2:1|0"), {(1, 0): Fraction(1, 2), (0, 0): Fraction(1, 2)})
        for bad in ("1/2:1", "1/2:1|2|3", "1/2:|1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ru.joint_distribution(bad)

    def test_shared_roll_race_matches_brute_force(self):
        # Every roll adds at least one segment, so 4 + 4 targets finish within 7 rolls.
        want = brute_shared(HEIST, 4, 4, 7)
        got = ru.shared_race([ru.joint_distribution("1/6:2|0, 5/12:1|1, 5/12:0|1")], 4, 4, rounds=20)
        self.assertEqual((got["a"], got["b"], got["both"]), want)
        self.assertEqual(got["undecided"], 0)

    def test_shared_race_of_independent_sides_equals_together_race(self):
        a = {(0, 0): Fraction(1, 2), (2, 0): Fraction(1, 2)}
        b = {(0, 0): Fraction(2, 3), (0, 3): Fraction(1, 3)}
        shared = ru.shared_race([a, b], 5, 4, rounds=40)
        ra = ru.reached_by([{0: Fraction(1, 2), 2: Fraction(1, 2)}], {5: Fraction(1)}, 40)
        rb = ru.reached_by([{0: Fraction(2, 3), 3: Fraction(1, 3)}], {4: Fraction(1)}, 40)
        together = ru.race(ra, rb, order="together")
        for key in ("a", "b", "both", "mean_rounds"):
            self.assertEqual(shared[key], together[key], key)


@unittest.skipUnless(USABLE, "rpg-dice 0.5.0 or later not importable")
class DiceTests(unittest.TestCase):
    def test_cli_shared_roll(self):
        code, out, err = run("--target", "6", "--rival-target", "4", "--joint", "1/6:2|0, 5/12:1|1, 5/12:0|1")
        self.assertEqual(code, 0, err)
        self.assertIn("one roll moves both sides", out)
        self.assertIn("- side A first: 0.1293", out)
        self.assertIn("- both in the same round: 0.0452", out)
        for argv in (["--target", "6", "--joint", "1/6:2|0"],
                     ["--target", "6", "--rival-target", "4", "--joint", "1/6:2|0", "--order", "first"],
                     ["--target", "1d6", "--rival-target", "4", "--joint", "1/6:2|0"]):
            with self.subTest(argv=argv):
                code, _, err = run(*argv)
                self.assertEqual(code, 2)
                self.assertIn("refused", err)

    def test_early_stop_is_explained(self):
        code, out, _ = run("--target", "30", "--each", "1/2:1d8=+3")
        self.assertEqual(code, 0)
        self.assertIn("stopped", out)
        self.assertNotIn("lower bound", out)
        code, out, _ = run("--target", "30", "--each", "1/2:1d8=+3", "--rounds", "10")
        self.assertIn("lower bound", out)
    def test_dice_amount_matches_brute_force(self):
        attack = ru.attack_distribution("1/2:1d4")
        self.assertEqual(attack, {0: Fraction(1, 2), 1: Fraction(1, 8), 2: Fraction(1, 8),
                                  3: Fraction(1, 8), 4: Fraction(1, 8)})
        got = ru.reached_by([attack], {6: Fraction(1)}, 4)
        self.assertEqual(got, brute_reached_by([half_hit_d4()], 6, 4))

    def test_chance_from_a_total_check(self):
        attack = ru.attack_distribution("1d20=+5t>=15:3")
        self.assertEqual(attack, {0: Fraction(9, 20), 3: Fraction(11, 20)})

    def test_success_rule_amount_counts_successes(self):
        self.assertEqual(ru.attack_distribution("1:2d6>=5"), {0: Fraction(4, 9), 1: Fraction(4, 9), 2: Fraction(1, 9)})
        self.assertEqual(ru.attack_distribution("1:(1d10>=6)+(1d8>=6)"),
                         {0: Fraction(5, 16), 1: Fraction(1, 2), 2: Fraction(3, 16)})
        self.assertEqual(ru.attack_distribution("1:2d6kh1"), {v: Fraction(2 * v - 1, 36) for v in range(1, 7)})
        self.assertEqual(ru.attack_distribution("1:2d6")[7], Fraction(1, 6))

    def test_total_check_amount_is_refused(self):
        with self.assertRaises(ValueError):
            ru.attack_distribution("1:1d20t>=10")

    def test_rolled_target(self):
        self.assertEqual(ru.target_distribution("1d4=+2"), {v: Fraction(1, 4) for v in (3, 4, 5, 6)})
        self.assertEqual(ru.target_distribution("7"), {7: Fraction(1)})

    def test_negative_amounts_need_a_floor(self):
        with self.assertRaises(ValueError):
            ru.attack_distribution("1:1d4=-2")
        self.assertEqual(ru.attack_distribution("1:1d4=-2", floor=0),
                         {0: Fraction(1, 2), 1: Fraction(1, 4), 2: Fraction(1, 4)})

    def test_cli_table_and_race(self):
        code, out, err = run("--target", "8", "--each", "1/2:1d4", "--rounds", "6")
        self.assertEqual(code, 0, err)
        self.assertIn("| round | reached this round | reached by now |", out)
        code, out, err = run("--target", "4", "--each", "1/2:2", "--rival-target", "4", "--rival-each", "1/2:2",
                             "--order", "first")
        self.assertEqual(code, 0, err)
        self.assertIn("side A wins", out)

    def test_heavy_explosion_is_refused(self):
        with self.assertRaises(ValueError):
            ru.attack_distribution("1:1d10x>=2")
        with self.assertRaises(ValueError):
            ru.target_distribution("1d10x>=2")

    def test_rare_explosion_keeps_probabilities_valid(self):
        # Long enough that new arrivals dry up, so any mass lost at the cap would show as a drop.
        reached = ru.reached_by([ru.attack_distribution("1/2:1d6x")], {12: Fraction(1)}, 300)
        steps = [b - a for a, b in zip([Fraction(0)] + reached, reached)]
        self.assertTrue(all(step >= 0 for step in steps))
        self.assertLessEqual(reached[-1], 1)

    def test_unresolved_explosions_count_as_adding_nothing(self):
        # 1d4x leaves about 2.4e-7 of each roll unresolved; it must not leak away as unfinished fights.
        self.assertEqual(sum(ru.attack_distribution("1:1d4x").values()), 1)
        self.assertEqual(sum(ru.attack_distribution("1d20t>=11:1d4x").values()), 1)
        r = ru.analyse([ru.attack_distribution("1:1d4x")], ru.target_distribution("3d6"), 100, ru.STOP_BELOW)
        self.assertLess(r["beyond"], ru.STOP_BELOW)
        code, out, err = run("--target", "3d6", "--each", "1:1d4x")
        self.assertEqual(code, 0, err)
        mean_line = next(line for line in out.splitlines() if line.startswith("Mean rounds"))
        self.assertNotIn("lower bound", mean_line)
        self.assertIn("adding nothing", out)

    def test_ordinary_exploding_amounts_are_computed(self):
        for each in ("1:1d10>=8x>=8", "1:5d4x"):
            with self.subTest(each=each):
                code, out, err = run("--target", "20", "--each", each)
                self.assertEqual(code, 0, err)

    def test_figures_with_unresolved_mass_are_not_called_exact(self):
        code, out, err = run("--target", "6", "--each", "1:3d6>=5x", "--floor", "1", "--rounds", "10")
        self.assertEqual(code, 0, err)
        self.assertNotIn("(exact)", out)
        self.assertNotIn("Exact figures from rpg-dice.", out)
        self.assertIn("Note:", out)

    def test_a_vanishing_undecided_race_mean_is_not_called_a_bound(self):
        code, out, err = run("--target", "1", "--each", "1/2:1", "--rival-target", "1", "--rival-each", "1/2:1")
        self.assertEqual(code, 0, err)
        race_line = next(line for line in out.splitlines() if line.startswith("- rounds until it is decided"))
        self.assertNotIn("lower bound", race_line)

    def test_capped_race_mean_is_labelled(self):
        code, out, err = run("--target", "1", "--each", "1/2:1", "--rival-target", "1", "--rival-each", "1/2:1",
                             "--rounds", "1")
        self.assertEqual(code, 0, err)
        race_line = next(line for line in out.splitlines() if line.startswith("- rounds until it is decided"))
        self.assertIn("lower bound", race_line)

    def test_cli_refusals(self):
        for argv in (["--target", "0", "--each", "1:1"], ["--target", "5"], ["--target", "5", "--each", "x:1d4"],
                     ["--target", "5", "--each", "1/2:1d4", "--rival-each", "1/2:1"],
                     ["--target", "5", "--each", "1/2:nope"], ["--target", "5", "--each", "0:1"]):
            with self.subTest(argv=argv):
                code, _, err = run(*argv)
                self.assertEqual(code, 2)
                self.assertIn("refused", err)

    def test_help_explains_outcomes(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            ru.main(["--help"])
        self.assertIn("CHANCE:AMOUNT", out.getvalue())


if __name__ == "__main__":
    unittest.main()
