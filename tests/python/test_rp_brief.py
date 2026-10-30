"""Tests for the GM brief in plugins/rp/tools/rp_gate.py: built in code from disk each turn, within a budget,
with the project's keyed entries matched against the Keeper's input and the last beats."""
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
_spec = importlib.util.spec_from_file_location("rp_brief_fixtures", HERE / "test_rp_rotation.py")
assert _spec is not None and _spec.loader is not None
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)
rp_gate = fx.rp_gate
Project, LEGACY, beat, run = fx.Project, fx.LEGACY, fx.beat, fx.run

TABLE = "Work/rp/test/keyed-entries.md"
NOTES = "Lore/World/Notes.md"
KEYED = f"""# Keyed entries

## Gate
- keys: gate, the door
- source: {NOTES}
- quote: "opens only at dusk"
- note: the gatekeeper names the price before he opens it

## Axe
- keys: axe*
- source: {NOTES}
- quote: the axe hangs by the woodpile / it is never lent

## Kettle
- keys: /\\bkettles?\\b/
- source: {NOTES}
- quote: the kettle is always full

## Lantern
- keys: lantern
- source: {NOTES}
- quote: a lantern burns all night

## Custom
- always: true
- note: (...) is the PC's action; [...] is out of character
"""
NOTES_TEXT = ("# Notes\n\nThe north gate opens only at dusk.\n\nThe axe hangs by the woodpile, and it is never lent.\n\n"
              "A lantern burns all night in the yard.\n")


def project(testcase, table: "str | None" = KEYED):
    p = Project(testcase)
    (p.root / NOTES).parent.mkdir(parents=True)
    (p.root / NOTES).write_text(NOTES_TEXT, encoding="utf-8")
    if table is not None:
        (p.root / TABLE).write_text(table, encoding="utf-8")
    return p


def brief(p, keeper="I open the door.", **kw):
    return rp_gate.build_brief(p.root, LEGACY, keeper, **kw)


class BriefTests(unittest.TestCase):
    def test_brief_carries_state_plan_and_beats(self):
        p = project(self)
        text, report = brief(p)
        self.assertEqual(report["bytes"], len(text.encode("utf-8")))
        for needle in (f"SESSION_FILE: {LEGACY}", "NEXT_BEAT: 4131", "KEEPER_INPUT:\nI open the door.",
                       f"LIVE_STATE:\n  session: {LEGACY} lines 1-", "== Frontmatter (", "== Header (",
                       "## What is on him", "=== Day Plan (", "### Clocks", "=== Recent beats",
                       "<!-- Beat 4130: ", "=== Not carried ==="):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        self.assertEqual(report["keeper_input"], rp_gate.keeper_digest("I open the door."))
        self.assertEqual(report["beats"]["carried"][-1], "4130")
        self.assertEqual(len(report["beats"]["carried"]), 20)  # previousBeats: the continuity read, whole
        # the Day Plan is carried once, as a plan, never again inside a beat
        self.assertEqual(text.count("## Day Plan: Day 196"), 1)

    def test_the_live_state_block_is_the_turn_commands(self):
        p = project(self)
        _text, report = brief(p)
        state = rp_gate.live_state(p.root, LEGACY)
        cont = rp_gate.continuity(p.root, LEGACY)
        self.assertEqual(report["live_state"], rp_gate.live_state_block(state, cont, LEGACY))
        lines = report["live_state"].splitlines()
        fm, header = state["frontmatter"], state["header"]
        self.assertEqual(lines[1], f"  session: {LEGACY} lines {fm['start']}-{header['end']}")
        self.assertTrue(lines[2].startswith("  run_state: Work/rp/test/alder.md lines "))
        self.assertTrue(any(x.startswith(f"  day: {LEGACY} lines ") and "(Day 196: so far; B4125-B4130)" in x
                            for x in lines))
        self.assertTrue(any(x.startswith(f"  earlier: {LEGACY} lines ") and "(14 beats, B4111-B4124)" in x
                            for x in lines))

    def test_keyed_entries_match_the_input_and_the_last_beats_with_their_quotes_checked(self):
        p = project(self)
        text, report = brief(p, keeper="I open the door and look at the kettles.")
        matched = {m["id"]: m for m in report["keyed"]["matched"]}
        self.assertEqual(set(matched), {"Gate", "Axe", "Kettle", "Custom"})  # Lantern: no key in either
        self.assertEqual(matched["Gate"]["in_input"], ["the door"])
        self.assertEqual(matched["Axe"]["in_beats"], ["axe*"])  # "sets down the axe" in the last beats
        self.assertEqual(matched["Kettle"]["in_input"], ["/\\bkettles?\\b/"])
        self.assertEqual(matched["Gate"]["quotes"], [{"verified": True, "line": 3}])
        self.assertEqual(matched["Axe"]["quotes"], [{"verified": True, "line": 5}])
        self.assertEqual(report["keyed"]["unverified"], ["Kettle"])
        self.assertIn(f'quote (verified, {NOTES} line 3): "opens only at dusk"', text)
        self.assertIn("NOT FOUND", text.split("== Kettle ==", 1)[1].split("\n== ", 1)[0])
        self.assertIn("note (from the table, not a source): (...) is the PC's action", text)
        order = [m["id"] for m in report["keyed"]["matched"]]
        self.assertEqual(order[0], "Custom")  # always first, then input matches before beat matches
        self.assertLess(order.index("Gate"), order.index("Axe"))

    def test_the_priced_stakes_rule_rides_the_brief_on_every_harness(self):
        for layout in ("| Register | Path | Role |\n|---|---|---|\n| `priced_stakes` | `Lore/Rules/prices.md` | x |\n",
                       "## priced_stakes\n\n`Lore/Rules/prices.md` the router.\n\n## campaigns\n"):
            with self.subTest(layout=layout[:12]):
                p = project(self)
                (p.root / "Memory").mkdir(exist_ok=True)
                (p.root / "Memory/canon-layout.md").write_text(layout, encoding="utf-8")
                text, report = brief(p, keeper="Is there a way out of the cellar?")
                self.assertTrue(report["priced_input"])
                self.assertEqual(report["keyed"]["matched"][0]["id"], "Priced stake")
                self.assertIn("source: Lore/Rules/prices.md", text)
                self.assertIn("PRICED STAKE", text)
                self.assertNotIn("system-reminder", text)
        _text, report = brief(project(self), keeper="I open the door.")
        self.assertFalse(report["priced_input"])

    def test_the_budget_keeps_the_last_beats_and_drops_the_oldest_first(self):
        p = project(self)
        full, _report = brief(p)
        text, report = brief(p, budget=len(full.encode("utf-8")) - 1500)
        carried = report["beats"]["carried"]
        dropped = [d["beat"] for d in report["beats"]["dropped"]]
        self.assertTrue(dropped)
        self.assertEqual(carried[-3:], ["4128", "4129", "4130"])
        self.assertTrue(all(int(d) < int(carried[0]) for d in dropped))  # oldest dropped first
        for d in report["beats"]["dropped"]:
            self.assertIn(f"- Beat {d['beat']}: {LEGACY} lines {d['lines']}", text)
        self.assertFalse(report["over_budget"])
        tiny, report = brief(p, budget=100)
        self.assertTrue(report["over_budget"])  # what must go in goes in, and the report says so
        self.assertEqual(report["beats"]["carried"], ["4130"])  # the last beat; the two before it after the keyed
        self.assertIn("=== Day Plan (", tiny)
        self.assertTrue(report["keyed"]["dropped"])
        self.assertIn(f"- keyed entry {report['keyed']['dropped'][0]}", tiny)

    def test_the_governing_state_line_is_carried_whatever_the_budget(self):
        p = project(self)
        with open(p.root / LEGACY, "a", encoding="utf-8") as fh:
            fh.write("STATE: ~08:30 Day 196, at the woodpile; the axe hung.\n")
        text, report = brief(p, budget=100)
        self.assertEqual(report["state_line"], {"path": LEGACY, "beat": "4130"})
        current = text.split("=== Current state ===", 1)[1].split("\n=== ", 1)[0]
        self.assertIn("STATE: ~08:30 Day 196, at the woodpile; the axe hung.", current)

    def test_carried_findings_go_into_the_brief(self):
        p = project(self)
        carried = rp_gate.carried_path(p.root, LEGACY)
        carried.parent.mkdir(parents=True, exist_ok=True)
        carried.write_text("# Carried findings: Beat 4130\n\n- tonal_drift: one hedge\n", encoding="utf-8")
        text, report = brief(p, budget=100)
        self.assertTrue(report["carried_findings"])
        self.assertIn("=== Carried findings ===\n# Carried findings: Beat 4130", text)

    def test_the_brief_is_built_from_disk_alone(self):
        # piece 3: a fresh Room rebuilds the same brief from the same files
        p = project(self)
        self.assertEqual(brief(p)[0], brief(p)[0])

    def test_brief_reports_a_provisional_beat(self):
        # O19: the brief is built after Step 1b; if a provisional beat is still there, it says so
        p = project(self)
        turn = (f"KEEPER_INPUT:\nI wait.\n\nDRAFT:\nLiss hangs the kettle on its hook.\n\n"
                f"Scene: Day 196, ~19:05, calm | Location: the kitchen | Present: Liss\n\nGM_SPAWN_LOG:\n- liss: no\n\n"
                f"SOURCE_LOG:\npriced_stake_touched: true\n\nSCENE_STATE_DELTA:\nscene.time: \"19:05\"\n")
        key = rp_gate.ship(p.root, LEGACY, turn_text=turn, append_text=beat(4131, 196), beat="4131",
                           provisional=True, max_rewrites=3)["key"]
        _text, report = brief(p)
        self.assertEqual(report["provisional"], [key])
        self.assertTrue(any("provisional" in w for w in report["warnings"]))

    def test_the_keyed_table_comes_from_the_canon_register(self):
        p = project(self, table=None)
        (p.root / "Lore/Keys").mkdir(parents=True)
        (p.root / "Lore/Keys/a.md").write_text("## Lantern\n- keys: door\n- note: from the register\n",
                                                encoding="utf-8")
        (p.root / "Memory").mkdir(exist_ok=True)
        (p.root / "Memory/canon-layout.md").write_text(
            "| Register | Path | Role |\n|---|---|---|\n| `keyed_entries` | `Lore/Keys/*.md` | keys |\n",
            encoding="utf-8")
        _text, report = brief(p)
        self.assertEqual(report["keyed"]["tables"], ["Lore/Keys/a.md"])
        self.assertEqual([m["id"] for m in report["keyed"]["matched"]], ["Lantern"])
        (p.root / "Memory/canon-layout.md").unlink()
        self.assertEqual(brief(p)[1]["keyed"]["tables"], [])  # the default beside log_dir does not exist

    def test_a_bad_key_is_a_warning_not_a_failure(self):
        p = project(self, table="## Broken\n- keys: /([/\n- note: x\n\n## Keyless\n- note: y\n")
        _text, report = brief(p)
        self.assertEqual(len(report["warnings"]), 3)  # the bad pattern, so no keys; and the keyless entry
        self.assertIn("is not a valid pattern", report["warnings"][0])
        self.assertEqual(report["keyed"]["matched"], [])

    def test_cli_writes_the_brief_and_its_report(self):
        p = project(self)
        gate = p.root / "Work/rp/.gate"
        gate.mkdir(parents=True, exist_ok=True)
        (gate / "input.md").write_text("I open the door.\n", encoding="utf-8")
        code, out, err = run("brief", "--project-dir", str(p.root), "--input-file", str(gate / "input.md"))
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        self.assertNotIn("live_state", report)
        self.assertEqual(report["path"], rp_gate.BRIEF_FILE)
        text = (p.root / rp_gate.BRIEF_FILE).read_text(encoding="utf-8")
        self.assertEqual(report["bytes"], len(text.encode("utf-8")))
        saved = json.loads((p.root / rp_gate.BRIEF_REPORT).read_text(encoding="utf-8"))
        self.assertEqual(saved["keeper_input"], rp_gate.keeper_digest("I open the door."))


    def test_the_budget_comes_from_the_mode_file(self):
        p = project(self)
        p.mode("turnGate:\n  briefBytes: 9000\n")
        self.assertEqual(brief(p)[1]["budget"], 9000)
        p.mode("turnGate:\n  briefBytes: 10\n")
        config = rp_gate.load_config(p.root)
        self.assertEqual(config["briefBytes"], 80000)
        self.assertTrue(any("briefBytes" in w for w in config["warnings"]))


class ViaTests(unittest.TestCase):
    @staticmethod
    def check(self, p, keeper, sourced):
        tf = p.root / "Work/rp/.gate/turn.md"
        tf.parent.mkdir(parents=True, exist_ok=True)
        tf.write_text(f"KEEPER_INPUT:\n{keeper}\n\nDRAFT:\nLiss sets down the axe.\n\n"
                      f"Scene: Day 196, ~08:30, calm | Location: the kitchen | Present: Liss\n\n"
                      f"GM_SPAWN_LOG:\n- liss: no\n\nSOURCE_LOG:\nsourced:\n{sourced}\npriced_stake_touched: false\n\n"
                      f"SCENE_STATE_DELTA:\nscene.time: \"08:30\"\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"ASHA_HARNESS": "claude"}):
            code, out, err = run("check", "--project-dir", str(p.root), "--turn-file", str(tf))
        self.assertEqual(code, 0, err)
        return json.loads(out)["lint"]["sources"]

    SOURCED = (f'  - claim: "the gate"\n    file: "{NOTES}"\n    quote: "opens only at dusk"\n'
               f'  - claim: "the lantern"\n    file: "{NOTES}"\n    quote: "A lantern burns all night"\n')
    TURN_SAME = (f"KEEPER_INPUT:\nI open the door.\n\nDRAFT:\nLiss sets down the axe.\n\n"
                 f"Scene: Day 196, ~08:40, calm | Location: the kitchen | Present: Liss\n\n"
                 f"GM_SPAWN_LOG:\n- liss: no\n\nSOURCE_LOG:\npriced_stake_touched: false\n\n"
                 f"SCENE_STATE_DELTA:\nscene.time: \"08:40\"\n")

    def test_check_marks_each_quote_carried_or_opened(self):
        p = project(self)
        rp_gate.write_brief(p.root, LEGACY, "I open the door.")
        sources = ViaTests.check(self, p, "I open the door.", self.SOURCED)
        self.assertEqual([q.get("via") for q in sources["quotes"]], ["brief", "opened"])
        self.assertEqual(sources["opened"], 1)
        [line] = [x for x in p.log_lines() if x["path"] == "lint"]
        self.assertEqual(line["opened_sources"], 1)

    def test_a_brief_built_for_other_input_marks_nothing(self):
        p = project(self)
        rp_gate.write_brief(p.root, LEGACY, "I open the door.")
        sources = ViaTests.check(self, p, "I close the door.", self.SOURCED)
        self.assertNotIn("opened", sources)
        self.assertTrue(all("via" not in q for q in sources["quotes"]))

    def test_the_pending_review_keeps_the_brief_with_the_copys_live_state(self):
        p = project(self)
        turn = (f"KEEPER_INPUT:\nI wait.\n\nDRAFT:\nLiss hangs the kettle on its hook.\n\n"
                f"Scene: Day 196, ~19:05, calm | Location: the kitchen | Present: Liss\n\nGM_SPAWN_LOG:\n- liss: no\n\n"
                f"SOURCE_LOG:\npriced_stake_touched: true\n\nSCENE_STATE_DELTA:\nscene.time: \"19:05\"\n")
        rp_gate.write_brief(p.root, LEGACY, "I wait.")
        live = (p.root / rp_gate.BRIEF_FILE).read_text(encoding="utf-8")
        rp_gate.ship(p.root, LEGACY, turn_text=turn, append_text=beat(4131, 196), beat="4131", provisional=True,
                     max_rewrites=3)
        [entry] = rp_gate.pending_reviews(p.root)
        kept = (p.root / entry["brief"]).read_text(encoding="utf-8")
        self.assertIn(f"  session: {entry['review_session']} lines 1-", kept)
        self.assertNotIn(f"  session: {LEGACY}", kept)
        # S1: built from the copy, so every path, heading and beat tag names the copy, never the live file
        self.assertNotIn(LEGACY, kept)
        self.assertIn(f"SESSION_FILE: {entry['review_session']}", kept)
        self.assertEqual(kept.replace(entry["review_session"], LEGACY), live)


class ViaCountsAsOpenedTests(unittest.TestCase):
    """Q8 makes via: brief count as opened, so it marks only what the GM read from its file: a line the brief
    carries whole, or a keyed entry's verified quote of that file and line (landing review S1, S2)."""

    def sources(self, p, keeper, sourced, **kw):
        rp_gate.write_brief(p.root, LEGACY, keeper, **kw)
        return ViaTests.check(self, p, keeper, sourced)

    @staticmethod
    def entry(file, quote):
        return f'  - claim: "c"\n    file: "{file}"\n    quote: "{quote}"\n'

    def decision(self, p, text):
        body = p.read(LEGACY).replace("### Beat 4112: Day 195", text + "\n\n### Beat 4112: Day 195", 1)
        (p.root / LEGACY).write_text(body, encoding="utf-8")

    def test_words_only_in_the_keepers_input_are_not_carried(self):
        p = project(self)
        got = self.sources(p, "I say it burns all night in the yard.", self.entry(NOTES, "burns all night in the yard"))
        self.assertEqual(got["quotes"][0]["via"], "opened")

    def test_a_cut_decision_line_is_a_lead_not_a_carried_line(self):
        p = project(self)
        self.decision(p, "GM DECISION (FLAG): the shed stays locked " + "and the key stays with the hook " * 30
                      + "UNLESS the bell has rung.")
        got = self.sources(p, "I wait.", self.entry(LEGACY, "the shed stays locked and the key stays with the hook"),
                           budget=100)
        self.assertTrue(got["quotes"][0]["verified"])
        self.assertEqual(got["quotes"][0]["via"], "opened")

    def test_a_decision_line_carried_whole_is_carried(self):
        p = project(self)
        self.decision(p, "GM DECISION (FLAG): the shed stays locked till dusk.")
        got = self.sources(p, "I wait.", self.entry(LEGACY, "the shed stays locked till dusk"), budget=100)
        self.assertEqual(got["quotes"][0]["via"], "brief")

    def test_a_keyed_quote_not_found_in_its_source_carries_nothing_for_another_file(self):
        p = project(self, KEYED + f"\n## Hook\n- keys: door\n- source: {NOTES}\n- quote: the key stays with the hook\n")
        (p.root / "Lore/World/Other.md").write_text("Elsewhere the key stays with the hook, they say.\n",
                                                   encoding="utf-8")
        got = self.sources(p, "I open the door.", self.entry("Lore/World/Other.md", "the key stays with the hook"))
        self.assertIn("NOT FOUND", (p.root / rp_gate.BRIEF_FILE).read_text(encoding="utf-8"))
        self.assertEqual(got["quotes"][0]["via"], "opened")

    def test_a_header_line_the_brief_carries_is_carried(self):
        p = project(self)
        got = self.sources(p, "I wait.", self.entry(LEGACY, "Register: plain sentences"))
        self.assertEqual((got["quotes"][0]["verified"], got["quotes"][0]["via"]), (True, "brief"))

    def test_every_piece_of_a_cut_excerpt_must_be_carried(self):
        # "A ... B": B may sit on the next line, one the brief cut to its head
        parts = ["alpha line one", "beta tail of a cut line"]
        source = (" ".join(parts), [0, len(parts[0]) + 1], ["body", "body"], None)
        pieces = [("alpha line one", "alpha line one"), ("beta tail", "beta tail")]
        brief = {"text": "x alpha line one y beta", "keyed": {}}
        self.assertFalse(rp_gate._brief_gave(brief, "/p", Path("/p/f.md"), 0, source, pieces))
        brief["text"] += " tail of a cut line"
        self.assertTrue(rp_gate._brief_gave(brief, "/p", Path("/p/f.md"), 0, source, pieces))

    def test_a_history_quote_gets_no_via(self):
        # play is not a source, so whether it was opened says nothing
        p = project(self)
        got = self.sources(p, "I wait.", self.entry(LEGACY, "Liss sets down the axe"))
        self.assertIsNotNone(got["quotes"][0]["history"])
        self.assertNotIn("via", got["quotes"][0])
        self.assertEqual(got["opened"], 0)


class ReviewRoundOneTests(unittest.TestCase):
    """Review 1: S1, S4, M7, M8, M12."""

    def test_the_review_brief_is_built_without_a_brief_for_the_turn(self):
        # S1, M8: the review brief comes from the copy and the turn, never from a brief that may be stale
        p = project(self)
        rp_gate.write_brief(p.root, LEGACY, "I wait.")
        turn = (f"KEEPER_INPUT:\nI open the door.\n\nDRAFT:\nLiss hangs the kettle on its hook.\n\n"
                f"Scene: Day 196, ~19:05, calm | Location: the kitchen | Present: Liss\n\nGM_SPAWN_LOG:\n- liss: no\n\n"
                f"SOURCE_LOG:\npriced_stake_touched: true\n\nSCENE_STATE_DELTA:\nscene.time: \"19:05\"\n")
        rp_gate.ship(p.root, LEGACY, turn_text=turn, append_text=beat(4131, 196), beat="4131", provisional=True,
                     max_rewrites=3)
        [entry] = rp_gate.pending_reviews(p.root)
        kept = (p.root / entry["brief"]).read_text(encoding="utf-8")
        self.assertIn("KEEPER_INPUT:\nI open the door.", kept)
        self.assertIn("== Gate ==", kept)  # matched against this turn's input

    def test_a_stale_brief_marks_no_quote(self):
        # M8: the same input on a later beat is not the same brief
        p = project(self)
        rp_gate.write_brief(p.root, LEGACY, "I open the door.")
        with open(p.root / LEGACY, "a", encoding="utf-8") as fh:
            fh.write(beat(4131, 196))
        rp_gate.ship(p.root, LEGACY, turn_text=ViaTests.TURN_SAME, append_text=beat(4132, 196), beat="4132")
        sources = ViaTests.check(self, p, "I open the door.", ViaTests.SOURCED)
        self.assertNotIn("opened", sources)

    def test_decisions_in_beats_not_carried_are_carried_on_their_own(self):
        # S4: a GM DECISION or RULING in a beat the budget drops still reaches the GM
        p = project(self)
        text = p.read(LEGACY).replace("### Beat 4112: Day 195", "GM DECISION (FLAG): the shed stays locked till dusk.\n\n"
                                      "### Beat 4112: Day 195", 1)
        (p.root / LEGACY).write_text(text, encoding="utf-8")
        tiny, report = brief(p, budget=100)
        self.assertIn("4112", [d["beat"] for d in report["beats"]["dropped"]])  # the line is in 4112's working
        section = tiny.split("=== GM decisions and rulings in beats not carried ===", 1)[1].split("\n=== ", 1)[0]
        self.assertIn("GM DECISION (FLAG): the shed stays locked till dusk.", section)
        self.assertIn("Beat 4112", section)
        self.assertRegex(section, r"Beat 4112, Work/rp/rp_session_2026-10-24\.md line \d+: GM DECISION")
        full, _report = brief(p)
        self.assertNotIn("=== GM decisions and rulings in beats not carried ===", full)  # every beat carried

    def test_a_long_decision_is_cut_to_its_head_with_its_line(self):
        # S4 measured on day 199: 35 decision lines of about 1.2 KB each would fill the budget on their own
        p = project(self)
        long = "GM DECISION (FLAG): the shed stays locked " + "and the key stays with the hook " * 30 + "till dusk."
        text = p.read(LEGACY).replace("### Beat 4112: Day 195", long + "\n\n### Beat 4112: Day 195", 1)
        (p.root / LEGACY).write_text(text, encoding="utf-8")
        line = text.splitlines().index(long) + 1
        tiny, _report = brief(p, budget=100)
        section = tiny.split("=== GM decisions and rulings in beats not carried ===", 1)[1].split("\n=== ", 1)[0]
        [entry] = [x for x in section.splitlines() if "shed stays locked" in x]
        self.assertIn(f"line {line}", entry)
        self.assertIn("cut", entry)
        self.assertNotIn("till dusk.", entry)
        self.assertLess(len(entry.encode("utf-8")), rp_gate.DECISION_LINE_MAX + 200)

    def test_a_brief_within_its_budget_never_reads_over_it(self):
        # M7a
        p = project(self)
        _tiny, smallest = brief(p, budget=100)
        full, _report = brief(p)
        for budget in range(smallest["bytes"] + 10, len(full.encode("utf-8")), 97):  # the head states the budget
            with self.subTest(budget=budget):
                text, report = brief(p, budget=budget)
                self.assertFalse(report["over_budget"])
                self.assertLessEqual(len(text.encode("utf-8")), budget)

    def test_keys_that_match_everything_or_nothing_are_warned_and_fixed(self):
        # M7c, M12: whole words; quotes and /.../i accepted; a key with no word refused
        table = ('## Door\n- keys: "the door"\n- note: quoted\n\n## Regex\n- keys: /\\bdoor\\b/i\n- note: flags\n\n'
                 "## Star\n- keys: *\n- note: everything\n\n## Doorway\n- keys: doorway\n- note: not a door\n")
        p = project(self, table=table)
        _text, report = brief(p, keeper="I open the door.")
        self.assertEqual({m["id"] for m in report["keyed"]["matched"]}, {"Door", "Regex"})
        self.assertTrue(any("'Star'" in w and "no letter" in w for w in report["warnings"]))

    def test_a_register_row_that_finds_no_table_warns(self):
        # M7b
        p = project(self)
        (p.root / "Memory").mkdir(exist_ok=True)
        (p.root / "Memory/canon-layout.md").write_text(
            "| Register | Path | Role |\n|---|---|---|\n| `keyed_entries` | `Work/rp/<campaign>/keyed-entries.md` | x |\n",
            encoding="utf-8")
        _text, report = brief(p)
        self.assertTrue(any("keyed_entries" in w for w in report["warnings"]))

    def test_only_the_priced_stakes_rule_rides_past_the_budget(self):
        # M7d: a table entry headed like it does not
        p = project(self, table="## Priced stake\n- keys: door\n- note: " + "x" * 3000 + "\n")
        _text, report = brief(p, keeper="I open the door.", budget=100)
        self.assertEqual(report["keyed"]["dropped"], ["Priced stake"])

    def test_the_newest_state_line_governs_and_old_beats_match_no_key(self):
        # M12: STATE newest first; keys scan only the last few beats
        p = project(self)
        text = p.read(LEGACY).replace("### Beat 4120: Day 195", "STATE: old state, the lantern lit.\n\n"
                                      "### Beat 4120: Day 195", 1)
        (p.root / LEGACY).write_text(text + "STATE: new state.\n", encoding="utf-8")
        _text, report = brief(p, keeper="I wait.")
        self.assertEqual(report["state_line"]["beat"], "4130")
        self.assertNotIn("Lantern", [m["id"] for m in report["keyed"]["matched"]])


def heavy_beat(n):
    """A beat shaped like the live Garret log of B4352-B4354: a long SOURCED line and GM DECISIONS line in its
    PRE-COMMIT, a few KB of prose, and a STATE line of about 9 KB, each naming a ruling."""
    cite = f"Notes.md l.{n} (GM RULING B{n - 1}, FLAG); " * 40
    decisions = f"the shed reading {n} stands; " * 150
    prose = "Liss sets down the axe and looks along the yard. " * 60
    state = f"the kitchen at the hearth; WARM (GM RULING B{n - 2}, FLAG); the rope on his collar; " * 100
    return (f"### Beat {n} PRE-COMMIT (GM, binding): LISS: \"SHED.\"\n\nSOURCED: {cite}\n"
            f"GM DECISIONS / RULINGS (FLAG FOR RATIFICATION): {decisions}\n\n"
            f"### Beat {n}: Day 196, ~08:{n % 60:02d} (Monday). (Tier 1, roleplay-gm)\n"
            f"**PC move (B{n}):** \"I go.\"\n\n{prose}\n\nSTATE: ~08:{n % 60:02d} Day 196, {state}\n\n")


class GmStepsTests(unittest.TestCase):
    """The brief within its budget with its keyed entries (B4355: 107,696 bytes of 80,000, every keyed entry
    dropped): superseded STATE lines and source records stay out, history sections are pointers, and the keyed
    entries go in before the two beats before the last."""

    def state(self, p, n, text):
        body, nxt = p.read(LEGACY), f"### Beat {n + 1} PRE-COMMIT"
        line = f"STATE: {text}\n\n"
        body = body.replace(nxt, line + nxt, 1) if nxt in body else body + line
        (p.root / LEGACY).write_text(body, encoding="utf-8")

    def test_superseded_state_lines_stay_out_and_the_governing_one_is_carried_once(self):
        p = project(self)
        for n in (4128, 4129, 4130):
            self.state(p, n, f"after beat {n}, the lantern lit; " + "the rope on his collar; " * 20)
        text, report = brief(p)
        self.assertEqual(report["state_line"]["beat"], "4130")
        self.assertEqual(report["beats"]["carried"][-3:], ["4128", "4129", "4130"])
        current = text.split("=== Current state ===", 1)[1].split("\n=== ", 1)[0]
        self.assertIn("STATE: after beat 4130", current)
        self.assertEqual(text.count("STATE: after beat 4130"), 1)  # once, never again inside its beat
        self.assertNotIn("STATE: after beat 4129", text)
        self.assertNotIn("STATE: after beat 4128", text)
        lines = p.read(LEGACY).splitlines()
        where = {n: next(i for i, x in enumerate(lines, 1) if x.startswith(f"STATE: after beat {n}"))
                 for n in (4128, 4129, 4130)}
        recent = text.split("=== Recent beats", 1)[1].split("\n=== ", 1)[0]
        for n in (4128, 4129):
            with self.subTest(beat=n):
                head = next(x for x in recent.splitlines() if x.startswith(f"<!-- Beat {n}:"))
                self.assertIn(f"STATE line {where[n]}", head)  # a read away, by its line
                self.assertIn("superseded", head)
        head = next(x for x in recent.splitlines() if x.startswith("<!-- Beat 4130:"))
        self.assertIn(f"STATE line {where[4130]}", head)
        self.assertIn("governing", head)

    def test_the_decision_list_carries_decisions_not_state_or_source_records(self):
        p = project(self)
        body = p.read(LEGACY).replace(
            "### Beat 4112: Day 195", "GM DECISION (FLAG): the shed stays locked till dusk.\n"
            "SOURCED: B4100 GM DECISION (\"the shed stays locked\"); Notes.md l.3 (GM RULING, FLAG)\n"
            "STATE: ~08:12 the shed locked (GM RULING B4100, FLAG)\n\n### Beat 4112: Day 195", 1)
        (p.root / LEGACY).write_text(body + "STATE: ~08:30 Day 196, at the woodpile.\n", encoding="utf-8")
        tiny, report = brief(p, budget=100)
        section = tiny.split("=== GM decisions and rulings in beats not carried ===", 1)[1].split("\n=== ", 1)[0]
        self.assertIn("GM DECISION (FLAG): the shed stays locked till dusk.", section)
        self.assertNotIn("SOURCED:", section)  # a record of where a beat's words came from cites rulings
        self.assertNotIn("STATE:", section)  # superseded by the governing STATE line
        self.assertEqual(report["decisions"], 1)

    def test_keyed_entries_go_in_before_the_two_beats_before_the_last(self):
        p = project(self)
        keeper = "I open the door and look at the kettles."
        tiny, smallest = brief(p, keeper=keeper, budget=100)
        self.assertEqual(smallest["beats"]["carried"], ["4130"])  # the last beat, whatever the budget
        dropped = [d["beat"] for d in smallest["beats"]["dropped"]]
        self.assertIn("4129", dropped)
        self.assertIn("- Beat 4129: ", tiny.split("=== Not carried ===", 1)[1])
        full, report = brief(p, keeper=keeper)
        ids = [m["id"] for m in report["keyed"]["matched"]]
        section = full.split("=== Keyed entries", 1)[1].split("\n=== ", 1)[0]
        _text, fits = brief(p, keeper=keeper, budget=smallest["bytes"] + len(section.encode("utf-8")))
        self.assertEqual([m["id"] for m in fits["keyed"]["matched"]], ids)
        self.assertEqual(fits["beats"]["carried"], ["4130"])  # every keyed entry went in first
        self.assertFalse(fits["over_budget"])
        _text, more = brief(p, keeper=keeper, budget=len(full.encode("utf-8")) - 1500)
        self.assertEqual(more["beats"]["carried"][-3:], ["4128", "4129", "4130"])

    def test_a_header_section_its_own_heading_marks_history_is_a_pointer(self):
        p = project(self)
        old = "## What was on him at D190 (history; superseded by the section above)"
        resume = "## Resume point (history: B4099, verbatim from the old log)"
        hist = (f"{old}\n\n- OLD-A the old collar\n- OLD-B\n\n### Its notes\n\n- OLD-C\n\n"
                f"{resume}\n\nOLD-D resume\n\n## Family history\n\n- KEPT-E\n\n")
        body = p.read(LEGACY).replace("## Open threads", hist + "## Open threads", 1)
        (p.root / LEGACY).write_text(body, encoding="utf-8")
        lines = body.splitlines()
        a, b = lines.index(old) + 1, lines.index(resume) + 1
        text, report = brief(p)
        for gone in ("OLD-A", "OLD-B", "OLD-C", "OLD-D"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, text)
        header = text.split("== Header (", 1)[1].split("\n== ", 1)[0]
        for kept in (old, resume, "## Family history", "- KEPT-E", "## What is on him", "T30 | Fed by a hand",
                     "### RULING T16", "- THE NORTH VAULT: a finger a day."):
            with self.subTest(kept=kept):
                self.assertIn(kept, header)
        self.assertEqual(report["header_history"], [{"heading": old, "lines": f"{a + 2}-{a + 7}"},
                                                    {"heading": resume, "lines": f"{b + 2}-{b + 2}"}])
        self.assertIn(f"lines {a + 2}-{a + 7}", header)

    def live_shaped(self, p):
        body = p.read(LEGACY)
        start = body.index("## Day Plan: Day 195")
        body = body[:start] + "## Day Plan: Day 196 (Monday)\n\n### Clocks\n\n- ~05:50 first light\n\n" + "".join(
            heavy_beat(n) for n in range(4111, 4131))
        (p.root / LEGACY).write_text(body, encoding="utf-8")

    def test_a_plural_decisions_line_of_a_beat_not_carried_is_listed(self):
        # review 1: "GM DECISIONS / RULINGS (...)" is the live form, and DECISION_RE takes only the singular
        p = project(self)
        self.live_shaped(p)
        text, report = brief(p, budget=80_000)
        self.assertIn("4111", [d["beat"] for d in report["beats"]["dropped"]])
        section = text.split("=== GM decisions and rulings in beats not carried ===", 1)[1].split("\n=== ", 1)[0]
        self.assertRegex(section, r"- Beat 4111, \S+ line \d+: GM DECISIONS / RULINGS \(FLAG FOR RATIFICATION\)")
        self.assertGreaterEqual(report["decisions"], len(report["beats"]["dropped"]))

    def test_state_lines_and_source_records_are_never_decision_lines(self):
        # review 1 F2, review 2 F1: a STATE line is superseded whatever it says (the live log carries a ruling in
        # force forward in each, "GM DECISION (B4345, FLAG): ..." from B4345 on, so the governing one holds it), and
        # every SOURCED record of day-0199 to day-0201 that names a ruling with its label cites it ("l.436 GM RULING:
        # the collar answers ..."). A ruling is written on its own line.
        p = project(self)
        body = p.read(LEGACY).replace(
            "### Beat 4112: Day 195", "STATE: ~08:12 GM RULING: the cellar ward holds until the bell rings.\n"
            "SOURCED: Notes.md l.3 (GM RULING: the shed bell carries to the yard.)\n"
            "SOURCE_LOG (priced_stake_touched): true. RULING (INFERRED): the yard gate sticks.\n"
            "SOURCED (priced stake: the gate): B4100 GM DECISION (\"the gate is barred\")\n"
            "GM DECISION (FLAG): the shed stays locked till dusk.\n\n### Beat 4112: Day 195", 1)
        (p.root / LEGACY).write_text(body, encoding="utf-8")
        for n in (4128, 4129, 4130):
            self.state(p, n, f"after beat {n}" + ("; GM RULING (FLAG): the kettle is never left dry" if n == 4128
                                                  else ""))
        tiny, _report = brief(p, budget=100)
        section = tiny.split("=== GM decisions and rulings in beats not carried ===", 1)[1].split("\n=== ", 1)[0]
        for gone in ("the cellar ward holds", "the shed bell carries", "the yard gate sticks", "the gate is barred"):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, section)
        self.assertIn("GM DECISION (FLAG): the shed stays locked till dusk.", section)
        full, report = brief(p)
        self.assertIn("4128", report["beats"]["carried"])
        self.assertNotIn("the kettle is never left dry", full)
        self.assertNotIn("STATE: after beat 4129", full)

    def test_another_line_gives_the_quote_only_whole_in_order_and_carried(self):
        # review 2 F2, F3: the line found first is left out (a history section), so another line of the file may
        # give the quote, but only a body line, not play, that the brief carries whole with the pieces in order;
        # the Keeper's input carries nothing
        p = project(self)
        body = p.read(LEGACY).replace("---\n", '---\nnote: "the kettle sings at dusk"\n', 1)
        hist = ("## Old ward (history, before D190)\n\n- OLD WARD: the ward holds until the bell rings at dusk.\n"
                "- OLD-B: the gate stays barred at night, Marr's rule from before the thaw.\n"
                "- OLD-C: the lantern hangs by the door.\n- OLD-D: the kettle sings at dusk, the old way.\n\n")
        body = body.replace("## Open threads", hist + "## Open threads\n\n- The bell rings at noon in the yard; "
                            "the ward holds.", 1)
        body = body.replace("| NPC | Goal |", "the gate stays barred at night\n\n| NPC | Goal |", 1)
        body = body.replace("Liss sets down the axe.\n\n", "Liss sets down the axe.\n\nthe lantern hangs by the door\n\n")
        (p.root / LEGACY).write_text(body, encoding="utf-8")
        keeper = "the gate stays barred at night"
        rp_gate.write_brief(p.root, LEGACY, keeper)
        self.assertEqual([h["heading"] for h in json.loads((p.root / rp_gate.BRIEF_REPORT).read_text())
                          ["header_history"]], ["## Old ward (history, before D190)"])
        sourced = "".join(ViaCountsAsOpenedTests.entry(LEGACY, q) for q in (
            "the ward holds ... the bell rings",  # in order only on the left-out line; reversed on a carried one
            "the gate stays barred at night",     # whole on a line only the Keeper's input repeats
            "the lantern hangs by the door",      # carried whole, but in a beat's prose: play
            "the kettle sings at dusk"))          # carried whole, but in the frontmatter: state
        got = ViaTests.check(self, p, keeper, sourced)
        self.assertEqual([q["verified"] for q in got["quotes"]], [True] * 4)
        self.assertEqual([q.get("via") for q in got["quotes"]], ["opened"] * 4)

    def test_a_read_part_stays_under_the_token_cap(self):
        self.assertLessEqual(rp_gate.BRIEF_READ_BYTES / 1.6, 25_000)  # 25,000 tokens a Read, at 1.6 bytes a token

    def test_the_governing_state_line_is_the_last_of_the_newest_beat(self):
        p = project(self)
        self.state(p, 4130, "first of two")
        with open(p.root / LEGACY, "a", encoding="utf-8") as fh:
            fh.write("STATE: second of two\n")
        text, _report = brief(p)
        current = text.split("=== Current state ===", 1)[1].split("\n=== ", 1)[0]
        self.assertIn("STATE: second of two", current)
        self.assertNotIn("first of two", text)

    def test_keys_still_match_the_state_lines_of_the_last_beats(self):
        p = project(self)
        self.state(p, 4130, "the lantern lit at the gate post")
        _text, report = brief(p, keeper="I wait.")
        self.assertIn("Lantern", [m["id"] for m in report["keyed"]["matched"]])

    def test_a_quote_the_governing_state_line_carries_is_via_brief(self):
        # review 4: the quote's first place in the file is an earlier STATE line the brief leaves out
        p = project(self)
        for n in (4128, 4129, 4130):
            self.state(p, n, f"~08:{n % 60} the kitchen at the hearth; WARM (GM RULING B4126, FLAG); the rope on "
                             f"his collar, {n - 4127} turns")
        lines = p.read(LEGACY).splitlines()
        first = next(i for i, x in enumerate(lines, 1) if "the kitchen at the hearth; WARM" in x)
        rp_gate.write_brief(p.root, LEGACY, "I wait.")
        got = ViaTests.check(self, p, "I wait.", ViaCountsAsOpenedTests.entry(LEGACY, "the kitchen at the hearth; WARM"))
        self.assertEqual(got["quotes"][0]["line"], first)  # found first in 4128's STATE line, which is left out
        self.assertTrue(got["quotes"][0]["verified"])
        self.assertEqual(got["quotes"][0].get("via"), "brief")

    def test_only_a_marked_history_section_below_the_title_is_cut(self):
        # review 3: a level-1 title, a parenthesis that only begins with the word, a fenced heading
        p = project(self)
        body = p.read(LEGACY).replace(
            "# RP Session 2026-10-24: Alder, Day 195 afternoon onward",
            "# RP Session 2026-10-24: Alder (superseded by the day files)", 1).replace(
            "## Open threads", "## Liss (history and current standing)\n\n- KEPT-F her current line\n\n"
            "```\n## Old (history: inside a fence)\n```\n\n- KEPT-G after the fence\n\n"
            "## Old marks (superseded by the section above)\n\n- OLD-H\n\n## Open threads", 1)
        (p.root / LEGACY).write_text(body, encoding="utf-8")
        text, report = brief(p)
        for kept in ("KEPT-F", "KEPT-G", "## Old (history: inside a fence)", "### RULING T16",
                     "**GM line (standing, Keeper 2026-10-04):**"):
            with self.subTest(kept=kept):
                self.assertIn(kept, text)
        self.assertNotIn("OLD-H", text)
        self.assertEqual([h["heading"] for h in report["header_history"]],
                         ["## Old marks (superseded by the section above)"])

    def test_a_fenced_heading_does_not_end_a_history_section(self):
        p = project(self)
        body = p.read(LEGACY).replace(
            "## Open threads", "## Old marks (history: before Day 190)\n\n- OLD-I\n\n```\n## not a heading\n```\n\n"
            "- OLD-J after the fence\n\n## Open threads", 1)
        (p.root / LEGACY).write_text(body, encoding="utf-8")
        text, _report = brief(p)
        self.assertNotIn("OLD-J", text)
        self.assertIn("- THE NORTH VAULT: a finger a day.", text)

    def test_reads_count_each_line_with_its_newline(self):
        text = "abcdefghi\n" * 25  # ten bytes a line
        self.assertEqual(rp_gate.brief_reads(text, limit=100),
                         [{"offset": 1, "limit": 10}, {"offset": 11, "limit": 10}, {"offset": 21, "limit": 5}])
        self.assertEqual(rp_gate.brief_reads("x" * 300 + "\nshort\n", limit=100),
                         [{"offset": 1, "limit": 1}, {"offset": 2, "limit": 1}])  # a longer line goes alone

    def test_the_report_splits_the_brief_into_reads_the_gm_issues_together(self):
        # a 79,098-byte live brief (B4355) was 33,091 tokens: one Read of it is refused
        # past 25,000 tokens, and a Read with no limit returns only its first part
        p = project(self)
        body = p.read(LEGACY)
        start = body.index("## Day Plan: Day 195")
        body = body[:start] + "## Day Plan: Day 196 (Monday)\n\n### Clocks\n\n- ~05:50 first light\n\n" + "".join(
            heavy_beat(n) for n in range(4111, 4131))
        (p.root / LEGACY).write_text(body, encoding="utf-8")
        text, report = brief(p, budget=80_000)
        lines = text.split("\n")[:-1]
        reads = report["reads"]
        self.assertGreater(len(reads), 1)
        at = 1
        for part in reads:
            with self.subTest(part=part):
                self.assertEqual(part["offset"], at)  # in order, every line once
                size = sum(len(x.encode("utf-8")) + 1 for x in lines[at - 1:at - 1 + part["limit"]])
                self.assertTrue(size <= rp_gate.BRIEF_READ_BYTES or part["limit"] == 1, size)
                at += part["limit"]
                if at <= len(lines):  # each part as full as it can be: the next line would not fit
                    self.assertGreater(size + len(lines[at - 1].encode("utf-8")) + 1, rp_gate.BRIEF_READ_BYTES)
        self.assertEqual(at, len(lines) + 1)
        small, report = brief(project(self))
        self.assertEqual(report["reads"], [{"offset": 1, "limit": small.count("\n")}])
        # a Read counts lines by newline: a form feed or line separator in the input is no line break to it
        odd, report = brief(project(self), keeper="I wait.\x0c Then I go. Slowly.")
        self.assertEqual(sum(part["limit"] for part in report["reads"]), odd.count("\n"))

    def test_a_live_shaped_read_fits_its_budget_with_its_keyed_entries(self):
        p = project(self)
        body = p.read(LEGACY)
        start = body.index("## Day Plan: Day 195")
        body = body[:start] + "## Day Plan: Day 196 (Monday)\n\n### Clocks\n\n- ~05:50 first light\n\n" + "".join(
            heavy_beat(n) for n in range(4111, 4131))
        (p.root / LEGACY).write_text(body, encoding="utf-8")
        keeper = "I open the door and look at the kettles."
        text, report = brief(p, keeper=keeper, budget=80_000)
        self.assertFalse(report["over_budget"], report["bytes"])
        self.assertEqual({m["id"] for m in report["keyed"]["matched"]}, {"Gate", "Axe", "Kettle", "Custom"})
        self.assertEqual(report["keyed"]["dropped"], [])
        self.assertEqual(report["beats"]["carried"][-3:], ["4128", "4129", "4130"])
        self.assertIn('quote (verified, Lore/World/Notes.md line 3): "opens only at dusk"', text)


class ContractTests(unittest.TestCase):
    def read(self, rel):
        return (REPO / rel).read_text(encoding="utf-8")

    def test_turn_builds_the_brief_in_code(self):
        text = self.read("plugins/rp/commands/turn.md")
        step2 = text.split("### Step 2", 1)[1].split("\n### Step 3", 1)[0]
        self.assertIn('rp_gate.py" brief', step2)
        self.assertIn("Work/rp/.gate/input.md", step2)
        self.assertIn("BRIEF: Work/rp/.gate/brief.md", text)

    def test_the_gm_reads_the_brief_first(self):
        gm = self.read("plugins/rp/agents/roleplay-gm.md")
        self.assertIn("BRIEF", gm)
        self.assertIn("opened", gm)  # the gate records a source the GM opened beyond the brief

    def test_the_prompt_reminder_asks_for_no_hand_brief(self):
        # it fires on every prompt of an RP session: it must not ask for the brief Step 2 forbids
        routing = " ".join(self.read("plugins/session/hooks/handlers/rp-routing.md").split())
        self.assertNotIn("inline scene", routing)
        self.assertIn("/rp:turn", routing)
        self.assertIn("rp-turn skill", routing)  # the name on Codex and Copilot
        self.assertIn("builds the GM's brief", routing)
        for kept in ("Never author the player's actions", "PC-only and meta beats remain with the primary agent",
                     "lint runs on every beat", "[validate]"):
            with self.subTest(kept=kept):
                self.assertIn(kept, routing)

    def test_a_quote_the_gate_verified_in_the_brief_counts_as_opened(self):
        # Q8: a SOURCE_LOG quote verified in this turn's brief (via: brief) is opened for the priced-stakes rule
        gm = self.read("plugins/rp/agents/roleplay-gm.md")
        fidelity = gm.split("\n# Source Fidelity", 1)[1].split("\n---", 1)[0]
        priced = next(line for line in fidelity.splitlines() if "Priced stakes must be opened" in line)
        self.assertIn("`via: brief`", priced)
        self.assertIn("counts as opened", priced)
        integration = gm.split("\n# /rp:turn Integration", 1)[1]
        rule = next(line for line in integration.splitlines() if line.startswith("- **Read `BRIEF` first.**"))
        self.assertIn("counts as opened", rule)
        self.assertIn("do not open it again", rule)
        self.assertNotIn("source actually opened this turn, from the project root; nothing else", integration)
        reviewer = self.read("plugins/write/agents/continuity-reviewer.md")
        category = reviewer.split("### 10. source_fidelity", 1)[1].split("\n### 11.", 1)[0]
        self.assertIn("`via: brief`", category)
        self.assertIn("counts as opened", category)
        turn = self.read("plugins/rp/commands/turn.md")
        for step, end in (("# 3a.", "# 3b."), ("# 3c.", "# 3d.")):
            with self.subTest(step=step):
                self.assertIn("counts as opened", turn.split(step, 1)[1].split(end, 1)[0])
        priced = self.read("plugins/rp/hooks/priced-stakes/canon-priced-stakes.md")
        self.assertIn("counts as opened", priced)
        # the exception is narrow: a cut decision line is a lead, and a costless priced answer is still flagged
        self.assertIn("cut: read line N whole", fidelity)
        self.assertIn("at no cost is still a violation", category)
        for text in (fidelity, rule, category, turn, priced):
            self.assertNotRegex(text, r"(?:not|never|n't) counts? as opened")

    def test_the_gm_reads_the_brief_in_one_call_and_batches_its_tools(self):
        # B4340-B4354: 16-38 model turns a draft, 1.3 tool calls a turn; the first Read of the brief, with no
        # limit, came back cut by the harness's token cap (truncatedByTokenCap), and every capped read had none
        gm = self.read("plugins/rp/agents/roleplay-gm.md")
        integration = gm.split("\n# /rp:turn Integration", 1)[1]
        rule = next(line for line in integration.splitlines() if line.startswith("- **Read `BRIEF` first.**"))
        self.assertIn("one message", rule)
        self.assertIn("`BRIEF_READS`", rule)
        self.assertIn("`limit`", rule)
        batch = next(line for line in integration.splitlines() if line.startswith("- **Batch tool calls.**"))
        for needle in ("one message", "Grep", "Read", "verified", "do not search"):
            with self.subTest(needle=needle):
                self.assertIn(needle, batch)
        spawning = gm.split("\n# Spawning Character Agents", 1)[1].split("\n# Synthesis Protocol", 1)[0]
        self.assertIn("one message", spawning)
        self.assertIn("neither answers the other", spawning)
        turn = self.read("plugins/rp/commands/turn.md")
        prompt = turn.split("# 3a.", 1)[1].split("# 3b.", 1)[0]
        self.assertIn("BRIEF_READS: <the brief report's reads>", prompt)
        for needle in ("limit", "one message", "neither answers the other"):
            with self.subTest(needle=needle):
                self.assertIn(needle, prompt)

    def test_the_register_and_readme_name_the_keyed_table(self):
        self.assertIn("keyed_entries", self.read("plugins/rp/templates/canon-layout.md"))
        readme = self.read("plugins/rp/README.md")
        self.assertIn("keyed-entries.md", readme)
        self.assertIn("briefBytes", readme)


if __name__ == "__main__":
    unittest.main()
