"""Tests for plugins/rpg/skills/oracle/scripts/oracle.py. Needs rpg-dice 0.5.0 or later."""
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "plugins/rpg/skills/oracle/scripts/oracle.py"
spec = importlib.util.spec_from_file_location("oracle", SCRIPT)
assert spec is not None and spec.loader is not None
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)

USABLE = oracle.DiceProbability is not None
NEEDS_ENGINE = unittest.skipUnless(USABLE, "rpg-dice 0.5.0 or later not importable")


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = oracle.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class AnswerTests(unittest.TestCase):
    def test_bands_for_even_odds(self):
        bands = oracle.answer_bands(50)
        self.assertEqual(oracle.answer_for(1, bands), "yes, and")
        self.assertEqual(oracle.answer_for(10, bands), "yes, and")
        self.assertEqual(oracle.answer_for(11, bands), "yes")
        self.assertEqual(oracle.answer_for(46, bands), "yes, but")
        self.assertEqual(oracle.answer_for(50, bands), "yes, but")
        self.assertEqual(oracle.answer_for(51, bands), "no, but")
        self.assertEqual(oracle.answer_for(55, bands), "no, but")
        self.assertEqual(oracle.answer_for(90, bands), "no")
        self.assertEqual(oracle.answer_for(91, bands), "no, and")

    @NEEDS_ENGINE
    def test_odds_are_exact_and_sum_to_one(self):
        for chance in (10, 30, 50, 70, 90, 3, 97):
            with self.subTest(chance=chance):
                odds = oracle.answer_odds(chance)
                self.assertEqual(sum(odds.values()), 1)
                self.assertEqual(odds["yes, and"] + odds["yes"] + odds["yes, but"], Fraction(chance, 100))
        self.assertEqual(oracle.answer_odds(50)["yes, and"], Fraction(1, 10))

    def test_likelihood_words(self):
        self.assertEqual(oracle.parse_chance("likely"), 70)
        self.assertEqual(oracle.parse_chance("65"), 65)
        for bad in ("0", "100", "maybe", "50.5"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                oracle.parse_chance(bad)


class TableTests(unittest.TestCase):
    @NEEDS_ENGINE
    def test_ranged_table_covers_the_dice(self):
        entries = oracle.parse_table("2-6: Quiet road\n7-9: Travellers\n10-12: Bandits\n", "2d6")
        odds = oracle.table_odds(entries, "2d6")
        self.assertEqual(odds["Bandits"], Fraction(6, 36))
        self.assertEqual(sum(odds.values()), 1)
        with self.assertRaises(ValueError):
            oracle.parse_table("2-6: Quiet\n8-12: Busy\n", "2d6")
        with self.assertRaises(ValueError):
            oracle.parse_table("1-4: A\n4-6: B\n", "1d6")

    @NEEDS_ENGINE
    def test_exploding_tables_refuse_unresolved_outcomes(self):
        # The finite exact engine tail is not the complete support of a real roll.
        for options in (("--odds",), ("--odds", "--json"), ("--seed", "0")):
            with self.subTest(options=options):
                code, out, err = run("table", "--items", "1: Event; 3-101: Event",
                                     "--dice", "1d10x>=2", *options)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("unresolved", err)

    @NEEDS_ENGINE
    def test_even_tiny_unresolved_tail_is_not_complete_support(self):
        with self.assertRaisesRegex(ValueError, "unresolved"):
            oracle.totals("1d6x>=6")

    @NEEDS_ENGINE
    def test_bare_ranges_without_punctuation(self):
        table = "2-4 Thunderstorm\n5-6 Heavy rain\n7-9 Overcast\n10-11 Clear and cold\n12 Freezing fog\n"
        odds = oracle.table_odds(oracle.parse_table(table, "2d6"), "2d6")
        self.assertEqual(odds["Freezing fog"], Fraction(1, 36))
        self.assertEqual(odds["Thunderstorm"], Fraction(1, 6))

    def test_numbered_lines_without_dice_are_refused_not_flattened(self):
        with self.assertRaises(ValueError) as ctx:
            oracle.parse_table("2-4 Thunderstorm\n5-12 Overcast\n", None)
        self.assertIn("--dice", str(ctx.exception))

    @NEEDS_ENGINE
    def test_signed_ranges_on_the_command_line(self):
        code, out, err = run("table", "--items", "-4--1: Bad; 0: Mixed; 1-4: Good", "--dice", "4d{-1,0,1}", "--odds")
        self.assertEqual(code, 0, err)
        self.assertIn("Mixed: 19/81", out)

    def test_plain_lines_may_start_with_a_plus(self):
        # Magic-item lists: "+1 longsword" is an entry, not a total.
        self.assertEqual(oracle.parse_table("+1 longsword\n+1 shield\nBag of holding\n", None),
                         ["+1 longsword", "+1 shield", "Bag of holding"])
        self.assertEqual(len(oracle.parse_table("+1 longsword\n+2 longsword\n+3 longsword\n", None)), 3)

    @NEEDS_ENGINE
    def test_ranges_crossing_zero_and_written_with_to(self):
        fate = "4d{-1,0,1}"
        for table in ("-4--2: A\n-1-+1: B\n2-4: C\n", "-4 to -2: A\n-1 to 1: B\n2 to 4: C\n"):
            with self.subTest(table=table):
                odds = oracle.table_odds(oracle.parse_table(table, fate), fate)
                self.assertEqual(odds, {"A": Fraction(5, 27), "B": Fraction(17, 27), "C": Fraction(5, 27)})
        with self.assertRaises(ValueError):  # "2 to 3" is a range, so it overlaps "3"
            oracle.parse_table("1: Rain\n2 to 3: Sun\n3: Fog\n", "1d3")

    @NEEDS_ENGINE
    def test_signed_ranges(self):
        odds = oracle.table_odds(oracle.parse_table("-4--1: Bad\n0: Mixed\n1-4: Good\n", "4d{-1,0,1}"),
                                 "4d{-1,0,1}")
        self.assertEqual(odds["Mixed"], Fraction(19, 81))
        self.assertEqual(odds["Bad"], odds["Good"])

    def test_plain_lines_are_equally_likely(self):
        entries = oracle.parse_table("# weather\nRain\nFog\n\nSun\n", None)
        self.assertEqual(oracle.table_odds(entries, None), {"Rain": Fraction(1, 3), "Fog": Fraction(1, 3),
                                                            "Sun": Fraction(1, 3)})

    @NEEDS_ENGINE
    def test_cli_table_roll_and_odds(self):
        with tempfile.TemporaryDirectory() as tmp:
            table = Path(tmp) / "road.txt"
            table.write_text("2-6: Quiet road\n7-9: Travellers\n10-12: Bandits\n")
            code, out, err = run("table", str(table), "--dice", "2d6", "--seed", "4")
            self.assertEqual(code, 0, err)
            self.assertRegex(out, r"rolled \d+: (Quiet road|Travellers|Bandits)")
            code, out, _ = run("table", str(table), "--dice", "2d6", "--odds")
            self.assertIn("Bandits: 1/6", out)


@NEEDS_ENGINE
class CliTests(unittest.TestCase):
    def test_ask_is_repeatable_with_a_seed_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "oracle.jsonl"
            first = run("ask", "--likelihood", "likely", "--question", "Is the bridge guarded?", "--seed", "9",
                        "--log", str(log))
            second = run("ask", "--likelihood", "likely", "--question", "Is the bridge guarded?", "--seed", "9")
            self.assertEqual(first[0], 0)
            self.assertEqual(first[1], second[1])
            self.assertIn("Is the bridge guarded?", first[1])
            record = json.loads(log.read_text().splitlines()[0])
            self.assertEqual(record["chance"], 70)
            self.assertIn(record["answer"], oracle.ANSWERS)

    def test_notes_and_recall(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "solo.jsonl"
            table = Path(tmp) / "t.txt"
            table.write_text("Rain\nFog\n")
            run("ask", "--likelihood", "unlikely", "--question", "Anyone inside?", "--seed", "1", "--log", str(log))
            code, _, err = run("note", "The roof has partly fallen in.", "--log", str(log))
            self.assertEqual(code, 0, err)
            run("table", str(table), "--seed", "2", "--log", str(log))
            code, out, _ = run("recall", "--log", str(log))
            self.assertIn("Anyone inside?", out)
            self.assertIn("The roof has partly fallen in.", out)
            records = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertEqual(Path(records[2]["table"]), table.resolve())
            self.assertEqual(run("note", "", "--log", str(log))[0], 2)
            self.assertEqual(run("recall", "--log", str(Path(tmp) / "missing.jsonl"))[0], 2)

    def test_hidden_odds_are_marked(self):
        code, out, _ = run("table", "--items", "Secret assassin; Secret tunnel", "--hidden", "--odds")
        self.assertTrue(out.startswith("GM ONLY"))
        code, out, _ = run("table", "--items", "Secret assassin; Secret tunnel", "--hidden", "--odds", "--json")
        self.assertTrue(json.loads(out)["hidden"])
        code, out, _ = run("ask", "--likelihood", "even", "--hidden", "--odds")
        self.assertTrue(out.startswith("GM ONLY"))
        code, out, _ = run("ask", "--likelihood", "even", "--hidden", "--odds", "--json")
        self.assertTrue(json.loads(out)["hidden"])
        code, out, _ = run("table", "--items", "Rain; Fog", "--odds", "--json")
        self.assertEqual(json.loads(out), {"hidden": False, "odds": {"Rain": "1/2", "Fog": "1/2"}})
        self.assertFalse(run("table", "--items", "Rain; Fog", "--odds")[1].startswith("GM ONLY"))

    def test_ask_odds(self):
        code, out, _ = run("ask", "--likelihood", "even", "--odds")
        self.assertEqual(code, 0)
        self.assertIn("yes, and: 1/10", out)

    def test_refusals(self):
        for argv in (["ask", "--likelihood", "perhaps"], ["table", "/nonexistent/table.txt"],
                     ["table", "--items", ""]):
            with self.subTest(argv=argv):
                code, _, err = run(*argv)
                self.assertEqual(code, 2)
                self.assertIn("refused", err)


if __name__ == "__main__":
    unittest.main()
