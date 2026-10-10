"""Tests for the scripts in plugins/rpg/skills/rulings/scripts/.

roll.py and difficulty.py need rpg-dice 0.5.0 or later; draw.py needs rpg-card
0.3.0 or later. Each group is skipped when its engine is not importable.
"""
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[2] / "plugins/rpg/skills/rulings/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


roll = load("roll")
draw = load("draw")
difficulty = load("difficulty")


def run(module, *argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = module.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(roll.DiceThrower is not None, "rpg-dice 0.5.0 or later not importable")
class RollTests(unittest.TestCase):
    def test_seeded_roll_is_repeatable_and_reported(self):
        first = run(roll, "2d20=+5kh1t>=15", "--seed", "3", "--label", "Stealth")
        second = run(roll, "2d20=+5kh1t>=15", "--seed", "3", "--label", "Stealth")
        self.assertEqual(first, second)
        code, out, _ = first
        self.assertEqual(code, 0)
        self.assertIn("Stealth: 2d20=+5kh1t>=15", out)
        self.assertIn("total 24", out)
        self.assertIn("pass", out)
        self.assertNotIn("GM ONLY", out)

    def test_successes_only_when_a_success_rule_is_set(self):
        # Seed 20 rolls a 6 that explodes; rpg-dice reports one default success, which is not shown.
        code, out, _ = run(roll, "1d6x", "--seed", "20")
        self.assertEqual(code, 0)
        self.assertIn("total 9", out)
        self.assertNotIn("successes", out)
        code, out, _ = run(roll, "4d6>=6", "--seed", "5")
        self.assertIn("successes", out)

    def test_hidden_roll_is_marked(self):
        code, out, _ = run(roll, "1d20=+3", "--hidden", "--seed", "1")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("GM ONLY"))

    def test_log_appends_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "rolls.jsonl"
            run(roll, "1d6", "--seed", "1", "--log", str(log))
            run(roll, "2d6", "--hidden", "--log", str(log))
            lines = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertEqual([x["expr"] for x in lines], ["1d6", "2d6"])
            self.assertEqual([x["hidden"] for x in lines], [False, True])
            self.assertEqual(lines[0]["seed"], 1)
            self.assertIsNone(lines[1]["seed"])
            self.assertIn("total", lines[0]["result"])

    def test_bad_expression_is_refused(self):
        for expr in ("d20", "2d6+", "hello"):
            with self.subTest(expr=expr):
                code, _, err = run(roll, expr)
                self.assertEqual(code, 2)
                self.assertIn("refused", err)


@unittest.skipUnless(difficulty.DiceProbability is not None, "rpg-dice 0.5.0 or later not importable")
class DifficultyTests(unittest.TestCase):
    def test_finds_the_target_closest_to_the_chance(self):
        rows = difficulty.ladder("1d20=+5t>=T", range(1, 31))
        best = difficulty.closest(rows, Fraction(3, 5))
        self.assertEqual(best["target"], 14)
        self.assertEqual(best["chance"], Fraction(12, 20))

    def test_roll_under_template(self):
        best = difficulty.closest(difficulty.ladder("1d100t<=T", range(1, 101)), Fraction(1, 2))
        self.assertEqual((best["target"], best["chance"]), (50, Fraction(1, 2)))

    def test_pool_threshold_and_successes_needed(self):
        best = difficulty.closest(difficulty.ladder("5d6>=T", range(1, 8)), Fraction(1, 2))
        self.assertEqual((best["target"], best["chance"]), (6, 1 - Fraction(5, 6) ** 5))
        best = difficulty.closest(difficulty.ladder("5d6>=5", range(1, 6), need="T"), Fraction(1, 2))
        self.assertEqual((best["target"], best["chance"]), (2, Fraction(131, 243)))
        self.assertEqual(difficulty.ladder("5d6>=T", [5], need="2")[0]["chance"], Fraction(131, 243))
        for template, need in (("5d6>=5", "1"), ("5dT>=5", "T"), ("1dT", "1"), ("2d6", "T")):
            with self.subTest(template=template, need=need), self.assertRaises(ValueError):
                difficulty.ladder(template, range(1, 4), need=need)

    def test_search_stops_once_past_the_chance(self):
        rows, best = difficulty.search("1d20=+5t>=T", range(1, 101), Fraction(3, 5))
        self.assertEqual(best["target"], 14)
        self.assertLessEqual(len(rows), 18)

    def test_cli_shows_neighbours(self):
        code, out, err = run(difficulty, "2d6=+1t>=T", "--chance", "0.75")
        self.assertEqual(code, 0, err)
        self.assertIn("| T | chance |", out)
        self.assertIn("closest: T = 7", out)

    def test_printed_steps_are_always_shown(self):
        code, out, err = run(difficulty, "1d20=+5t>=T", "--chance", "0.6", "--steps", "10,15,20,25")
        self.assertEqual(code, 0, err)
        self.assertIn("printed steps: 10 0.8000, 15 0.5500, 20 0.3000, 25 0.0500", out)
        code, _, err = run(difficulty, "1d20=+5t>=T", "--chance", "0.6", "--steps", "10,x")
        self.assertEqual(code, 2)

    def test_heavy_explosion_is_refused(self):
        code, _, err = run(difficulty, "1d10x>=2t>=T", "--chance", "0.9", "--range", "1..10")
        self.assertEqual(code, 2)
        self.assertIn("refused", err)

    def test_explosion_mass_left_out_is_reported_not_called_exact(self):
        rows = difficulty.ladder("1d6xt>=T", [1, 4])
        self.assertGreater(rows[0]["unresolved"], 0)
        self.assertEqual(rows[0]["chance"] + rows[0]["unresolved"], 1)  # every roll reaches 1
        self.assertEqual(difficulty.ladder("1d20=+5t>=T", [15])[0]["unresolved"], 0)
        code, out, err = run(difficulty, "1d6xt>=T", "--chance", "0.5", "--range", "1..10")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Exact figures from rpg-dice.", out)
        self.assertIn("higher by up to", out)
        code, out, err = run(difficulty, "1d6xt>=T", "--chance", "0.5", "--range", "1..10", "--json")
        self.assertEqual(code, 0, err)
        self.assertGreater(Fraction(json.loads(out)["closest"]["unresolved"]), 0)

    def test_ordinary_exploding_pools_are_computed(self):
        # Chronicles of Darkness style 8-again: about 9e-6 lies beyond the depth, reported, not refused.
        code, out, err = run(difficulty, "5d10>=8x>=8", "--need", "T", "--chance", "0.5", "--range", "1..8")
        self.assertEqual(code, 0, err)
        self.assertIn("closest: T = 2, chance 0.5798", out)
        self.assertIn("higher by up to", out)

    def test_equality_templates_scan_the_whole_range(self):
        rows, best = difficulty.search("1d{1,2,2,3,3,3,4,4,4,4,9}t==T", range(1, 11), Fraction(1, 100))
        self.assertEqual(best["chance"], 0)
        self.assertEqual(len(rows), 10)

    def test_negative_range_is_accepted(self):
        code, out, err = run(difficulty, "2d6=+Tt>=10", "--range", "-4..4", "--chance", "0.3")
        self.assertEqual(code, 0, err)
        self.assertIn("closest: T = 1,", out)

    def test_refusals(self):
        for argv in (["1d20=+5", "--chance", "0.5"], ["1d20t>=15", "--chance", "0.5"],
                     ["1d20=+5t>=T", "--chance", "1.5"], ["1d20=+5t>=T", "--chance", "x"]):
            with self.subTest(argv=argv):
                code, _, err = run(difficulty, *argv)
                self.assertEqual(code, 2)
                self.assertIn("refused", err)


@unittest.skipUnless(draw.Deck is not None, "rpg-card 0.3.0 or later not importable")
class DrawTests(unittest.TestCase):
    def test_state_persists_between_draws(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "deck.json"
            code, out1, err = run(draw, "--deck", "standard52", "--state", str(state), "--count", "3", "--seed", "4",
                                  "--json")
            self.assertEqual(code, 0, err)
            code, out2, _ = run(draw, "--deck", "standard52", "--state", str(state), "--count", "3", "--json")
            self.assertEqual(code, 0)
            names = [c["name"] for out in (out1, out2) for c in json.loads(out)["cards"]]
            self.assertEqual(len(set(names)), 6)
            self.assertEqual(json.loads(out2)["left"], 46)
            code, out3, _ = run(draw, "--deck", "standard52", "--state", str(state))
            self.assertIn("45 left", out3)

    def test_running_out_is_refused_until_reshuffle(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "deck.json"
            self.assertEqual(run(draw, "--deck", "majors", "--state", str(state), "--count", "20")[0], 0)
            code, _, err = run(draw, "--deck", "majors", "--state", str(state), "--count", "3")
            self.assertEqual(code, 2)
            self.assertIn("--reshuffle", err)
            code, out, err = run(draw, "--deck", "majors", "--state", str(state), "--count", "3", "--reshuffle")
            self.assertEqual(code, 0, err)
            self.assertIn("19 left", out)

    def test_deck_mismatch_and_bad_state_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "deck.json"
            run(draw, "--deck", "tarot", "--state", str(state))
            code, _, err = run(draw, "--deck", "standard52", "--state", str(state))
            self.assertEqual(code, 2)
            self.assertIn("tarot", err)
            state.write_text("{not json")
            code, _, err = run(draw, "--deck", "tarot", "--state", str(state))
            self.assertEqual(code, 2)
            self.assertIn("refused", err)
            code, _, err = run(draw, "--deck", "tarot", "--state", str(state), "--new")
            self.assertEqual(code, 0, err)

    def test_one_off_draw_and_reversals(self):
        code, out, _ = run(draw, "--deck", "tarot", "--count", "78", "--reversals", "--seed", "2")
        self.assertEqual(code, 0)
        self.assertIn("(reversed)", out)
        code, out, _ = run(draw, "--deck", "tarot", "--count", "78", "--seed", "2")
        self.assertNotIn("(reversed)", out)

    def test_draw_log_and_reshuffle_without_drawing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, log = Path(tmp) / "deck.json", Path(tmp) / "draws.jsonl"
            run(draw, "--deck", "majors", "--state", str(state), "--count", "5", "--log", str(log), "--seed", "1")
            code, out, err = run(draw, "--deck", "majors", "--state", str(state), "--count", "0", "--reshuffle",
                                 "--log", str(log))
            self.assertEqual(code, 0, err)
            self.assertIn("22 left", out)
            lines = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertEqual([len(x["cards"]) for x in lines], [5, 0])
            self.assertEqual([x["reshuffled"] for x in lines], [False, True])
            self.assertEqual(lines[0]["left"], 17)

    def test_odds_for_the_next_card_use_the_live_deck(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "deck.json"
            code, out, err = run(draw, "--deck", "standard52", "--state", str(state), "--count", "0",
                                 "--odds", "value >= 10")
            self.assertEqual(code, 0, err)
            self.assertIn("4/13", out)
            code, out, _ = run(draw, "--deck", "standard52", "--state", str(state), "--json")
            drawn = json.loads(out)["cards"][0]
            code, out, _ = run(draw, "--deck", "standard52", "--state", str(state), "--count", "0",
                               "--odds", "value >= 10", "--json")
            self.assertEqual(Fraction(json.loads(out)["odds"]), Fraction(16 - (drawn["value"] >= 10), 51))
            for bad in ("__import__('os')", "value.real > 1", "name * 99999999 == ''"):
                with self.subTest(bad=bad):
                    code, _, err = run(draw, "--deck", "standard52", "--count", "0", "--odds", bad)
                    self.assertEqual(code, 2)
                    self.assertIn("refused", err)

    def test_an_unusable_log_is_refused_before_drawing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "deck.json"
            run(draw, "--deck", "standard52", "--state", str(state), "--count", "0")
            for log in (tmp, str(Path(tmp) / "missing" / "draws.jsonl")):  # a folder; a missing folder
                with self.subTest(log=log):
                    code, out, err = run(draw, "--deck", "standard52", "--state", str(state), "--log", log)
                    self.assertEqual(code, 2)
                    self.assertIn("refused", err)
                    self.assertEqual(out, "")
            code, out, _ = run(draw, "--deck", "standard52", "--state", str(state), "--count", "0", "--json")
            self.assertEqual(json.loads(out)["left"], 52)

    def test_late_log_errors_report_consumed_cards_and_preserve_output_modes(self):
        # Reclassifying a log write/close failure as refusal would hide consumed cards.
        for failure in ("write", "close"):
            for json_output in (False, True):
                with self.subTest(failure=failure, json_output=json_output), tempfile.TemporaryDirectory() as tmp:
                    state, log_path = Path(tmp) / "deck.json", Path(tmp) / "draws.jsonl"
                    run(draw, "--deck", "standard52", "--state", str(state), "--count", "0", "--seed", "4")
                    log = open(log_path, "a", encoding="utf-8")
                    original_close = log.close

                    def failing_close():
                        original_close()
                        raise OSError("injected close failure")

                    options = ["--json"] if json_output else []
                    with patch.object(draw, "open", return_value=log, create=True), \
                            patch.object(log, failure, side_effect=failing_close if failure == "close"
                                         else OSError("injected write failure")):
                        code, out, err = run(draw, "--deck", "standard52", "--state", str(state),
                                             "--count", "2", "--hidden", "--log", str(log_path), *options)
                    self.assertTrue(log.closed)
                    self.assertEqual(code, 3, err)
                    self.assertIn("draw completed", err)
                    self.assertIn("state saved", err)
                    self.assertIn("log failed", err)
                    self.assertIn(f"injected {failure} failure", err)
                    self.assertIn("do not repeat", err)
                    self.assertNotIn("refused", err)
                    remaining, _ = draw.load(state, "standard52")
                    self.assertEqual(len(remaining.cards), 50)
                    if json_output:
                        result = json.loads(out)
                        self.assertEqual(set(result), {"deck", "hidden", "left", "reshuffled", "odds", "cards"})
                        self.assertTrue(result["hidden"])
                        self.assertEqual(result["left"], 50)
                        self.assertEqual(len(result["cards"]), 2)
                        self.assertTrue(set(c["name"] for c in result["cards"]).isdisjoint(
                            c.name() for c in remaining.cards))
                    else:
                        self.assertTrue(out.startswith("GM ONLY"))
                        self.assertIn("Drew 2 from standard52:", out)
                        self.assertIn("50 left", out)

    def test_log_error_without_state_reports_completed_result_not_saved_state(self):
        # Claiming a state save for a one-off draw would misreport persistence.
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "draws.jsonl"
            log = open(log_path, "a", encoding="utf-8")
            with patch.object(draw, "open", return_value=log, create=True), \
                    patch.object(log, "write", side_effect=OSError("injected write failure")):
                code, out, err = run(draw, "--deck", "standard52", "--log", str(log_path), "--json")
            self.assertTrue(log.closed)
            self.assertEqual(code, 3, err)
            self.assertEqual(json.loads(out)["left"], 51)
            self.assertEqual(len(json.loads(out)["cards"]), 1)
            self.assertIn("without a state file", err)
            self.assertNotIn("state saved", err)
            self.assertIn("do not repeat", err)

    def test_save_failure_remains_refusal_and_preserves_state_even_if_log_close_fails(self):
        # Treating any log error as completed would falsely accept a failed state save.
        for close_fails in (False, True):
            with self.subTest(close_fails=close_fails), tempfile.TemporaryDirectory() as tmp:
                state, log_path = Path(tmp) / "deck.json", Path(tmp) / "draws.jsonl"
                run(draw, "--deck", "standard52", "--state", str(state), "--count", "0", "--seed", "4")
                before = state.read_bytes()
                log = open(log_path, "a", encoding="utf-8")
                original_close = log.close

                def close():
                    original_close()
                    if close_fails:
                        raise OSError("injected close failure")

                with patch.object(draw, "open", return_value=log, create=True), \
                        patch.object(draw.os, "replace", side_effect=OSError("injected save failure")), \
                        patch.object(log, "close", side_effect=close):
                    code, out, err = run(draw, "--deck", "standard52", "--state", str(state),
                                         "--log", str(log_path))
                self.assertTrue(log.closed)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("refused", err)
                self.assertIn("injected save failure", err)
                self.assertNotIn("draw completed", err)
                self.assertEqual(state.read_bytes(), before)
                self.assertEqual(log_path.read_text(), "")

    def test_hidden_draw_is_marked(self):
        code, out, _ = run(draw, "--deck", "standard", "--hidden", "--seed", "1")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("GM ONLY"))


if __name__ == "__main__":
    unittest.main()
