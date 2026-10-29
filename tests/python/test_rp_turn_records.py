"""Tests for rp_gate.py's per-beat records: the pc_change trigger and the SOURCE_LOG quote check."""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "plugins/rp/tools/rp_gate.py"
spec = importlib.util.spec_from_file_location("rp_gate", SCRIPT)
assert spec is not None and spec.loader is not None
rp_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp_gate)

# Two consecutive narrative PC roles, invented for the test: the second beat changes nothing
# on him (he speaks one line), and only its opening (the time, his move, what Sabe did) is
# rewritten. Both carry the same stats, so only a stat that moves is a PC change.
TAIL = ("Her ledger has been shut since 15:40 (%s). The stove keeps him warm until 17:00 (B25). "
        "Stats: POW 53/60 (5 lent), Luck 23. He is tall and broad, in a mended left boot, with ink on both "
        "cuffs, carrying Sabe's ledger and the tin with the key. ")
ROLE_A = (
    "PC: at 16:10 on day 12 he stands in the Copper Kettle's yard by the pump with two crates. He told Pask "
    "(his move, B30) that the crates were his and that he would wait for Sabe to sign them off, and he has "
    "waited ten minutes. Sabe took the flour sack from him and set it down by Pask; the cart is at the lane's "
    "head, still loaded. Her tag is tied to his top crate (B21), fixed to the crate, not to the yard. "
    + TAIL % "she has not touched him"
    + "Sabe's condition (B30, a GM decision): 'He signs for it, or it stays on the quay.'"
)
ROLE_B = (
    "PC: at 16:13 on day 12, still by the pump with the crates, he looked at Pask and said (his move, B31): "
    "'You said the crates would fit.' Sabe's tag stays on his top crate (B21), the cart's chain looped "
    "through its cord, the free end in her hand. "
    + TAIL % "a yard away"
    + "Sabe's condition stands (B30)."
)

SESSION = "Work/rp/osmund/log/day-0012.md"
FRONTMATTER = """---
campaign: "Test"
currentDate: "Day 12 (Thursday)"
currentLocation: "the yard, by the pump"
currentTime: "16:10"
participants: ["Osmund", "Pask", "Sabe"]
scene:
  time: "16:10"
  location: "the yard, by the pump"
  participants:
    - name: "Osmund"
      role: %s
    - name: "Pask"
      role: "NPC"
    - name: "Sabe"
      role: "NPC"
      profile_file: "Canon/Characters/Sabe.md"
log_day: 12
log_dir: "Work/rp/osmund/log"
---

## Day Plan: Day 12 (Thursday)

| NPC | Goal Today | Move They Make | Clock/Deadline |
|-----|-----------|----------------|----------------|
| Sabe | the count | If Pask wants the crates moved, he asks her first. | ~16:00 |
| Pask | the count | He miscounts the crates twice and sends a boy for Sabe on the third. | ~16:00 |

Tally kept: **two crates short** (logged only).
"""

ANCHOR = "Scene: Day 12, ~16:13, calm, warm, standing | Location: the yard, by the pump | Present: Osmund, Pask, Sabe"
DRAFT = f'Osmund looks at Pask. "You said the crates would fit."\n\nPask nods once.\n\n{ANCHOR}'
SABE = """# Sabe

Counts the till twice; trusts it once.
She won't haggle, she names a figure.

**Ledger-hand**: neat figures, short words. Numbers before names. Contractions.
A joke from her comes with a bill.
"""


def participants(role):
    return ('scene.time: "16:13"\nscene.participants:\n  - name: "Osmund"\n    role: %s\n'
            '  - name: "Pask"\n    role: "NPC"\n  - name: "Sabe"\n    role: "NPC"\n'
            '    profile_file: "Canon/Characters/Sabe.md"' % json.dumps(role))


def turn(source="priced_stake_touched: false\ncanon_proposed: false\npc_changed: false",
         delta='scene.time: "16:13"', draft=DRAFT, keeper="(I look at Pask) You said the crates would fit."):
    return (f"KEEPER_INPUT:\n{keeper}\n\nDRAFT:\n{draft}\n\nGM_SPAWN_LOG:\n- pask: NOT spawned (GM voice)\n\n"
            f"SOURCE_LOG:\n{source}\n\nSCENE_STATE_DELTA:\n{delta}\n")


def sourced(*entries, tail="priced_stake_touched: false\ncanon_proposed: false\npc_changed: false"):
    rows = ["sourced:"]
    for claim, file, quote in entries:
        rows += [f"  - claim: {json.dumps(claim)}", f"    file: {json.dumps(file)}", f"    quote: {json.dumps(quote)}"]
    return "\n".join(rows) + "\ninferred: []\n" + tail


class Project:
    def __init__(self, testcase, role=ROLE_A):
        self._tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for rel, text in ((SESSION, FRONTMATTER % json.dumps(role)), ("Canon/Characters/Sabe.md", SABE),
                          ("Canon/Characters/Pask.md", "# Pask\n")):
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def check(self, text, *extra, relative=False):
        tf = self.write("Work/rp/.gate/turn.md", text)
        out, err = io.StringIO(), io.StringIO()
        here = os.getcwd()
        try:
            if relative:  # as /rp:turn runs it: from the project root, --project-dir .
                os.chdir(self.root)
            with redirect_stdout(out), redirect_stderr(err):
                code = rp_gate.main(["check", "--project-dir", "." if relative else str(self.root),
                                     "--session-file", SESSION, "--turn-file", str(tf), "--no-log", *extra])
        finally:
            os.chdir(here)
        assert code == 0, err.getvalue()
        return json.loads(out.getvalue())


class PcChangeTests(unittest.TestCase):
    def test_a_rewritten_narrative_role_with_the_pc_unchanged_is_not_a_pc_change(self):
        p = Project(self)
        got = p.check(turn(delta=participants(ROLE_B)))
        self.assertNotIn("pc_change", got["triggers"])
        self.assertEqual(got["triggers"], [])
        self.assertEqual(got["route"]["beat"], "routine")
        self.assertEqual(got["route"]["next"], "ship")

    def test_the_narrative_role_is_reported_not_compared(self):
        p = Project(self)
        got = p.check(turn(delta=participants(ROLE_B)))
        event = next(e for e in got["lint"]["events"] if e["kind"] == "pc_role_narrative")
        self.assertEqual(event["chars"], len(ROLE_B))

    def test_a_declared_change_still_fires_with_a_narrative_role(self):
        p = Project(self)
        got = p.check(turn(source="pc_changed: true", delta=participants(ROLE_B)))
        self.assertIn("pc_change", got["triggers"])

    def test_a_change_of_label_still_fires(self):
        p = Project(self, role="PC (calm)")
        got = p.check(turn(delta=participants("PC (hands grown)")))
        self.assertIn("pc_change", got["triggers"])
        self.assertFalse([e for e in got["lint"]["events"] if e["kind"] == "pc_role_narrative"])

    def test_a_role_cut_from_narrative_to_a_label_is_not_a_pc_change(self):
        p = Project(self)
        got = p.check(turn(delta=participants("PC")))
        self.assertNotIn("pc_change", got["triggers"])

    def test_the_same_label_is_not_a_pc_change(self):
        p = Project(self, role="PC (calm)")
        got = p.check(turn(delta=participants("PC (calm)")))
        self.assertEqual(got["triggers"], [])


class SourceQuoteTests(unittest.TestCase):
    def test_a_verbatim_quote_is_verified_and_located(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("he asks her first", SESSION, "If Pask wants the crates moved, he asks her first."))))
        sources = got["lint"]["sources"]
        self.assertEqual((sources["checked"], sources["verified"]), (1, 1))
        self.assertEqual(sources["quotes"][0]["file"], SESSION)
        self.assertEqual(sources["quotes"][0]["line"], 26)
        self.assertEqual(got["lint"]["findings"], [])
        self.assertEqual(got["route"]["next"], "ship")

    def test_a_paraphrase_is_unverified_and_sends_the_beat_to_review(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("he asks her first", SESSION, "If Pask needs the crates moved, Sabe must be asked."))))
        self.assertEqual(got["lint"]["sources"]["verified"], 0)
        finding = next(f for f in got["lint"]["findings"] if f["check"] == "source_unverified")
        self.assertEqual(finding["fix"], "review")
        self.assertEqual(finding["n"], 1)
        self.assertEqual(got["route"]["next"], "review")
        self.assertIn("lint:source_unverified", got["route"]["reasons"])

    def test_an_unverified_quote_never_holds_up_the_final_check(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(("x", SESSION, "words nobody wrote"))), "--final")
        self.assertEqual(got["route"]["next"], "ship")

    def test_line_numbers_and_notes_in_the_file_field_are_ignored(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("the third time", f"{SESSION} l.131 (Day Plan, Pask row; grep -n puts it at l.126)",
             "He miscounts the crates twice and sends a boy for Sabe on the third."))))
        self.assertEqual(got["lint"]["sources"]["verified"], 1)
        self.assertEqual(got["lint"]["sources"]["quotes"][0]["line"], 27)

    def test_excerpts_joined_by_a_slash_or_an_ellipsis_each_count(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("register", "Canon/Characters/Sabe.md l.3; l.4",
             "Counts the till twice; trusts it once. / She won't haggle, she names a figure."),
            ("register", "Canon/Characters/Sabe.md",
             "Ledger-hand: neat figures ... Contractions."),
            ("register", "Canon/Characters/Sabe.md",
             "Ledger-hand: neat figures ... A joke from her comes as a pun."))))
        quotes = got["lint"]["sources"]["quotes"]
        self.assertEqual([q["verified"] for q in quotes], [True, True, False])
        self.assertIn("A joke from her comes as a pun", quotes[2]["reason"])

    def test_typography_and_emphasis_do_not_count_against_a_quote(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("tally", SESSION, "Tally kept: two crates short"),
            ("register", "Canon/Characters/Sabe.md", "She won’t haggle, she names a figure."),
            ("register", "Canon/Characters/Sabe.md", "ledger-hand: NEAT figures"))))
        self.assertEqual([q["verified"] for q in got["lint"]["sources"]["quotes"]], [True, True, True])

    def test_escaped_quote_marks_do_not_count_against_a_quote(self):
        # Two quoted excerpts on one line, as a GM writes them: the YAML subset keeps the backslashes.
        p = Project(self, role="PC")
        source = ('sourced:\n  - claim: "the sheet"\n    file: "Canon/Characters/Sabe.md"\n'
                  '    quote: "\\"She won\'t haggle,\\" she names a figure." / "A joke from her comes with a bill."\n')
        got = p.check(turn(source=source))
        self.assertEqual(got["lint"]["sources"]["verified"], 1, got["lint"]["sources"])

    def test_excerpts_may_come_from_different_files_the_entry_names(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("her terms", f"Canon/Characters/Sabe.md l.4; {SESSION} l.47",
             "She won't haggle, she names a figure. / If Pask wants the crates moved, he asks her first."),
            ("one of them wrong", f"Canon/Characters/Sabe.md; {SESSION}",
             "She won't haggle, she names a figure. / If Pask needs him, he comes to her."))))
        quotes = got["lint"]["sources"]["quotes"]
        self.assertEqual([q["verified"] for q in quotes], [True, False])
        self.assertEqual((quotes[0]["file"], quotes[0]["line"]), ("Canon/Characters/Sabe.md", 4))
        self.assertIn("If Pask needs him, he comes to her.", quotes[1]["reason"])

    def test_a_bare_file_name_resolves_beside_the_session_or_by_name(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("the Day Plan", "day-0012.md l.124", "If Pask wants the crates moved, he asks her first."),
            ("the sheet", "Sabe.md", "She won't haggle, she names a figure."))))
        quotes = got["lint"]["sources"]["quotes"]
        self.assertEqual([q["verified"] for q in quotes], [True, True])
        self.assertEqual(quotes[1]["file"], "Canon/Characters/Sabe.md")

    def test_the_keepers_input_is_a_source(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("his line", "KEEPER_INPUT (this turn)", "You said the crates would fit."),
            ("his line", "KEEPER_INPUT", "This was not what I said."))))
        self.assertEqual([q["verified"] for q in got["lint"]["sources"]["quotes"]], [True, False])

    def test_a_missing_file_or_no_file_is_unverified(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("gone", "Canon/Characters/Nobody.md", "anything"),
            ("nothing", "the Day Plan above", "If Pask wants the crates moved"))))
        quotes = got["lint"]["sources"]["quotes"]
        self.assertEqual([q["verified"] for q in quotes], [False, False])
        self.assertTrue(all(q["reason"] for q in quotes))
        self.assertEqual(len([f for f in got["lint"]["findings"] if f["check"] == "source_unverified"]), 2)

    def test_no_sourced_list_checks_nothing(self):
        p = Project(self, role="PC")
        for source in ("priced_stake_touched: false", "sourced: []\ninferred: []", "sourced: not a list", ""):
            with self.subTest(source=source):
                got = p.check(turn(source=source))
                self.assertEqual(got["lint"]["sources"]["checked"], 0)
                self.assertFalse([f for f in got["lint"]["findings"] if f["check"] == "source_unverified"])

    def test_a_malformed_entry_is_unverified_not_a_crash(self):
        p = Project(self, role="PC")
        source = 'sourced:\n  - "a bare string"\n  - claim: "no quote"\n    file: "Sabe.md"\n  - claim: "x"\n    quote: 3'
        got = p.check(turn(source=source))
        self.assertEqual(got["lint"]["sources"]["checked"], 3)
        self.assertEqual(got["lint"]["sources"]["verified"], 0)


STRUCK_BODY = """
### Beat 29: Day 12, ~15:50 to ~16:00 (Thursday). (gate: routine)

Osmund waits by the pump with the flour.

GM DECISION (FLAG): Pask miscounts twice and sends for Sabe on the third.

> [!struck]- STRUCK, not canon: Beat 30, attempt 1, struck 2026-10-28. Reason: invented_mechanics
>
> GM DECISION (B30): Sabe waives the price of the ledger.
>
> <!-- rp-struck: 0123456789abcdef -->

Sabe will not open the ledger unless Pask asks, and the price is her cut.
"""


class ReviewOneTests(unittest.TestCase):
    """Cold review 1 of the change: the crash, what a quote may come from, and pc_change in a narrative role."""

    def project(self, role="PC", body=STRUCK_BODY):
        p = Project(self, role=role)
        with open(p.root / SESSION, "a", encoding="utf-8") as fh:
            fh.write(body)
        return p

    def quotes(self, p, *entries, **kw):
        return p.check(turn(source=sourced(*entries)), **kw)["lint"]["sources"]["quotes"]

    # B1
    def test_a_bare_name_found_by_name_works_with_a_relative_project_dir(self):
        p = self.project()
        quotes = self.quotes(p, ("the sheet", "Sabe.md", "She won't haggle, she names a figure."), relative=True)
        self.assertEqual((quotes[0]["verified"], quotes[0]["file"]), (True, "Canon/Characters/Sabe.md"))

    def test_an_overlong_file_field_is_unverified_not_a_crash(self):
        p = self.project()
        quotes = self.quotes(p, ("x", "the Day Plan row " + "x" * 300 + " day-0012.md", "anything"))
        self.assertFalse(quotes[0]["verified"])

    # S1: what a quote may come from
    def test_a_quote_only_in_a_struck_draft_is_not_a_source(self):
        p = self.project()
        quotes = self.quotes(p, ("the waiver", SESSION, "Sabe waives the price of the ledger."))
        self.assertFalse(quotes[0]["verified"])
        self.assertIn("struck", quotes[0]["reason"])

    def test_the_session_frontmatter_is_current_state_not_a_source(self):
        p = self.project(role="PC (hatted, aproned, fed till noon)")
        quotes = self.quotes(p, ("his state", SESSION, "hatted, aproned, fed till noon"))
        self.assertFalse(quotes[0]["verified"])
        self.assertIn("frontmatter", quotes[0]["reason"])

    def test_a_canon_files_frontmatter_is_a_source(self):
        p = self.project()
        p.write("Canon/Characters/Pask.md", "---\nage: \"early fifties\"\nheight: \"six foot\"\n---\n# Pask\n")
        quotes = self.quotes(p, ("his age", "Canon/Characters/Pask.md", 'age: "early fifties"'))
        self.assertTrue(quotes[0]["verified"])

    def test_the_gate_scratch_and_the_validator_log_are_not_sources(self):
        p = self.project()
        p.write("Work/rp/_validator_log.jsonl", '{"note": "Sabe keeps the ledger"}\n')
        quotes = self.quotes(p, ("own log", "Work/rp/.gate/turn.md", "a sentence nobody wrote"),
                             ("the log", "Work/rp/_validator_log.jsonl", "Sabe keeps the ledger"))
        self.assertEqual([q["verified"] for q in quotes], [False, False])
        self.assertTrue(all("not a source" in q["reason"] for q in quotes))

    def test_a_quote_from_an_earlier_beat_is_marked_history_and_a_decision_is_not(self):
        p = self.project()
        quotes = self.quotes(p, ("he waited", SESSION, "Osmund waits by the pump with the flour."),
                             ("the slip", SESSION, "Pask miscounts twice and sends for Sabe on the third."))
        self.assertEqual([q["verified"] for q in quotes], [True, True])
        self.assertEqual([q["history"] for q in quotes], ["29", None])

    def test_cut_excerpts_must_be_in_order(self):
        p = self.project()
        quotes = self.quotes(p, ("order", SESSION, "Sabe will not ... the price is her cut."),
                             ("reversed", SESSION, "the price is her cut ... Sabe will not"))
        self.assertEqual([q["verified"] for q in quotes], [True, False])

    def test_cut_excerpts_must_be_close(self):
        p = self.project(body=STRUCK_BODY + "\n" + ("Filler line about the yard and the frost.\n" * 40)
                         + "And at the end the price is paid in full.\n")
        quotes = self.quotes(p, ("far", SESSION, "Sabe will not open the ledger ... the price is paid in full."))
        self.assertFalse(quotes[0]["verified"])

    def test_only_an_ellipsis_in_brackets_is_a_cut(self):
        p = self.project()
        quotes = self.quotes(p, ("insert", SESSION, "Sabe will [now] open the ledger"),
                             ("cut", SESSION, "Sabe will not [...] unless Pask asks"),
                             ("cut", SESSION, "Sabe will not \u2026 unless Pask asks"))
        self.assertEqual([q["verified"] for q in quotes], [False, True, True])

    def test_a_quote_over_two_lines_is_checked_whole(self):
        p = self.project()
        for second, verified in (("and the price is her cut.", True), ("and he pays nothing at all.", False)):
            with self.subTest(second=second):
                source = ('sourced:\n  - claim: "x"\n    file: "%s"\n    quote: "Sabe will not open the ledger '
                          'unless Pask asks,\n      %s"\n  - claim: "y"\n    file: "Sabe.md"\n'
                          '    quote: "She won\'t haggle, she names a figure."\n' % (SESSION, second))
                quotes = p.check(turn(source=source))["lint"]["sources"]["quotes"]
                self.assertEqual([q["verified"] for q in quotes], [verified, True])

    # S2 and S3: the PC role
    def test_a_label_before_and_a_narrative_after_is_a_pc_change(self):
        p = Project(self, role="PC (hatted)")
        got = p.check(turn(delta=participants(ROLE_B)))
        self.assertIn("pc_change", got["triggers"])

    def test_a_stat_that_moves_inside_a_narrative_role_is_a_pc_change(self):
        p = Project(self)
        for old, new in (("POW 53/60", "POW 40/60"), ("Luck 23", "Luck 9")):
            with self.subTest(stat=new):
                got = p.check(turn(delta=participants(ROLE_B.replace(old, new))))
                self.assertIn("pc_change", got["triggers"])

    def test_the_narrative_event_says_which_side_narrates(self):
        p = Project(self)
        got = p.check(turn(delta=participants("PC")))
        event = next(e for e in got["lint"]["events"] if e["kind"] == "pc_role_narrative")
        self.assertEqual((event["chars"], event["before_chars"]), (2, len(ROLE_A)))

    def test_narrative_roles_the_delta_leaves_in_place_are_reported(self):
        p = Project(self)
        got = p.check(turn())
        event = next(e for e in got["lint"]["events"] if e["kind"] == "roles_narrative")
        self.assertEqual(event["names"], ["Osmund"])
        got = p.check(turn(delta=participants("PC")))
        self.assertFalse([e for e in got["lint"]["events"] if e["kind"] == "roles_narrative"])

    # minor findings
    def test_a_file_name_with_commas_or_parentheses_resolves(self):
        p = self.project()
        p.write("Canon/Creatures/Murmur (SMI).md", "A shadow eats the light it stands in.\n")
        p.write("Canon/Wards, Knots, and Seals.md", "Intent is the first faculty.\n")
        quotes = self.quotes(p, ("shadow", "Canon/Creatures/Murmur (SMI).md", "eats the light it stands in"),
                             ("intent", "Canon/Wards, Knots, and Seals.md l.1", "Intent is the first faculty."))
        self.assertEqual([q["verified"] for q in quotes], [True, True])

    def test_a_path_outside_the_project_is_not_read(self):
        p = self.project()
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "notes.md"
            outside.write_text("Sabe waives every price.\n", encoding="utf-8")
            quotes = self.quotes(p, ("outside", str(outside), "Sabe waives every price."),
                                 ("climb", "../" + Path(other).name + "/notes.md", "Sabe waives every price."))
        self.assertEqual([q["verified"] for q in quotes], [False, False])

    def test_the_name_search_skips_dot_directories(self):
        p = self.project()
        p.write(".obsidian/Secret.md", "Sabe waives every price.\n")
        quotes = self.quotes(p, ("hidden", "Secret.md", "Sabe waives every price."))
        self.assertFalse(quotes[0]["verified"])

    def test_the_file_beside_the_session_wins_over_one_of_the_same_name(self):
        p = self.project()
        p.write("Work/rp/osmund/log/notes.md", "The beside copy holds this line.\n")
        p.write("notes.md", "The copy at the root holds another line.\n")
        quotes = self.quotes(p, ("beside", "notes.md", "The beside copy holds this line."))
        self.assertEqual((quotes[0]["verified"], quotes[0]["file"]), (True, "Work/rp/osmund/log/notes.md"))

    def test_compatibility_forms_and_dashes_do_not_count(self):
        p = self.project()
        p.write("Canon/fire.md", "The fire is low\u2014and nobody feeds it. POW \uff15\uff13/\uff16\uff10.\n")
        quotes = self.quotes(p, ("fire", "Canon/fire.md", "The fire is low -- and nobody feeds it."),
                             ("fire", "Canon/fire.md", "The fire is low - and nobody feeds it."),
                             ("pow", "Canon/fire.md", "POW 53/60"))
        self.assertEqual([q["verified"] for q in quotes], [True, True, True])

    def test_a_quote_of_punctuation_or_one_letter_checks_nothing(self):
        p = self.project()  # the session's Day Plan table is full of "|"
        quotes = self.quotes(p, ("table", SESSION, "|"), ("dash", SESSION, "--"), ("letter", SESSION, "a"),
                             ("cut", SESSION, "If Pask ... |"))
        self.assertEqual([q["verified"] for q in quotes], [False, False, False, True])
        self.assertEqual([q["reason"] for q in quotes[:3]], ["no quote"] * 3)

    def test_a_block_scalar_quote_is_checked_whole(self):
        p = self.project()
        source = ('sourced:\n  - claim: "x"\n    file: "%s"\n    quote: |\n      Sabe will not open the ledger\n'
                  '      unless Pask asks\n' % SESSION)
        self.assertTrue(p.check(turn(source=source))["lint"]["sources"]["quotes"][0]["verified"])

    def test_a_line_number_written_after_a_colon_is_ignored(self):
        p = self.project()
        quotes = self.quotes(p, ("plan", SESSION + ":26", "If Pask wants the crates moved, he asks her first."))
        self.assertEqual((quotes[0]["verified"], quotes[0]["file"]), (True, SESSION))


class ContractTests(unittest.TestCase):
    """The prompts say what the check now does, so no agent re-derives it."""

    def test_the_gm_gives_paths_and_quotes_and_keeps_roles_as_labels(self):
        text = (REPO / "plugins/rp/agents/roleplay-gm.md").read_text(encoding="utf-8")
        self.assertIn("give no line numbers", text)
        self.assertIn("A `role` is a label", text)
        self.assertIn("never quoted back as though it were written down", text)
        self.assertNotIn("makes every beat a `pc_change`", text)

    def test_the_turn_passes_the_quote_check_to_the_reviewer(self):
        text = (REPO / "plugins/rp/commands/turn.md").read_text(encoding="utf-8")
        self.assertIn("LINT: <lint.findings, lint.sources and triggers from the check>", text)
        self.assertIn("and no line numbers: the gate finds the quote", text)
        self.assertIn("do not check the citations again", text)
        self.assertIn("pc_role_narrative", text)

    def test_priced_stake_is_declared_only_when_a_price_turns(self):
        # 2026-10-28: a price that only stays in force, or is mentioned or remembered, routes nothing to review;
        # the citation a price needs does not depend on the flag
        rule, idle = "decides, applies, tests or changes a price", "stays in force, or is mentioned or remembered"
        flat = lambda rel: " ".join((REPO / rel).read_text(encoding="utf-8").split())  # noqa: E731
        gm = (REPO / "plugins/rp/agents/roleplay-gm.md").read_text(encoding="utf-8")
        template = " ".join(gm.split("priced_stake_touched: <true|false>", 1)[1].split("canon_proposed:", 1)[0]
                            .replace("#", " ").split())
        for needle in (rule, "paid, refused, risked, set or released", idle + ", is false",
                       "states a price's terms, rests on one or adjudicates one cites it", "flag or no flag"):
            with self.subTest(needle=needle):
                self.assertIn(needle, template)
        for rel in ("plugins/rp/commands/turn.md", "plugins/rp/README.md",
                    "plugins/rp/hooks/priced-stakes/canon-priced-stakes.md"):
            with self.subTest(rel=rel):
                self.assertIn(rule, flat(rel))
                self.assertIn(idle, flat(rel))
        for rel in ("plugins/rp/commands/turn.md", "plugins/rp/README.md"):
            with self.subTest(rel=rel):  # what a routine beat's price rests on, said where routing is defined
                self.assertIn("rests on the GM's own call", flat(rel))
        # the reviewer's hard rule no longer waits on the flag
        category = flat("plugins/write/agents/continuity-reviewer.md").split("### 10. source_fidelity", 1)[1]
        category = category.split("### 11.", 1)[0]
        self.assertIn("whatever `priced_stake_touched` says", category)
        self.assertIn(rule, category)
        # the hook reminder: no unconditional flag, no claim that every matched input touches a stake
        fragment = flat("plugins/rp/hooks/priced-stakes/canon-priced-stakes.md")
        self.assertNotIn("This turn touches a PRICED STAKE", fragment)
        self.assertNotIn("- Set `SOURCE_LOG.priced_stake_touched: true`. ", fragment)
        flag = fragment.split("- Set `SOURCE_LOG.priced_stake_touched", 1)[1].split(" - ", 1)[0]
        self.assertNotIn("always", flag)
        self.assertIn("citation", flag)

    def test_the_reviewer_reads_the_quote_check_instead_of_grepping(self):
        text = (REPO / "plugins/write/agents/continuity-reviewer.md").read_text(encoding="utf-8")
        self.assertIn("**LINT.sources** is the quote check, already done", text)
        self.assertIn("Do not grep or re-verify a verified quote", text)


if __name__ == "__main__":
    unittest.main()
