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

# The PC role strings of two consecutive Garret beats (day-0199.md, B4303 then B4304), as
# shipped, less three clauses of campaign wording. B4304 changed nothing on him: he speaks
# one line. Only the narrative head (the time, his move, what Liss did) was rewritten.
TAIL = ("LISS'S HAND OFF HIM since ~07:16 (%s); WARM (HER WARMTH, B4270, FLAG: only her hand warms him; "
        "the cold returns about ~08:16 unless her hand is back on him); SURE-FOOTED OFF; THE GRIP OFF "
        "(B4271, hidden, FLAG); TRIMMED (B4264, FLAG); his ask SPENT; unhooded; POW 53/60 (5 lent); Luck 23; "
        "near eight and a half feet; draught frame; TWO deer legs on feathered draught hooves, iron-shod; mane, "
        "horse's tail; Liss's fleeced breastcollar; the collar with THE KEY on its thong; ")
ROLE_4303 = (
    "PC (~07:46 D199 in the Gilt Thorn's yard at the trough, STANDING, two buckets in his hands; said to Marr "
    "(his move B4303): 'Who do you think. But I'll wait for her to come up and take care of it.' and waited "
    "~07:36-07:46 with the load; the sand sack hauled down off his shoulder by Liss and dropped at Marr's feet "
    "(her hands on the sack, not on him); the lime tub at the head of the passage below, the sand and water not "
    "down; LISS'S ROPE: her knot at his collar ring (B4242; HER KNOTS); its slack pulled from under the "
    "breastcollar strap by Liss and its free end hitched by her at the ring at the side of his breastcollar (her "
    "knot: it holds; made fast on him, not to a place, so TIED does not hold him there); the stone sledge's "
    "chain knotted by Liss into the rope's tail, the chain's free end in her hand, held out toward Marr, not "
    "handed over; " + TAIL % "she has not touched his hide"
    + "LISS'S TERM (B4303, GM DECISION, FLAG, no working), said to Marr in his hearing: 'And he asks me plain "
    "for my hand first.' (HIS MOVE))"
)
ROLE_4304 = (
    "PC (~07:49 D199 in the Gilt Thorn's yard at the trough, STANDING, two buckets in his hands; looked at Marr "
    "and said (his move B4304): 'This was what you recommended.'; LISS'S ROPE at his collar ring (B4242; HER "
    "KNOTS), its free end hitched by Liss at the side ring of his breastcollar (her knot: holds; on him, not "
    "fast to a place: not TIED); the stone sledge's chain knotted into the rope's tail, its free end still in "
    "Liss's hand; " + TAIL % "a yard from his hide"
    + "LISS'S TERM stands: 'And he asks me plain for my hand first.' (B4303; his move))"
)

SESSION = "Work/rp/garret/log/day-0199.md"
FRONTMATTER = """---
campaign: "Test"
currentDate: "Day 199 (Thursday)"
currentLocation: "the yard, at the trough"
currentTime: "07:46"
participants: ["Garret", "Marr", "Liss"]
scene:
  time: "07:46"
  location: "the yard, at the trough"
  participants:
    - name: "Garret"
      role: %s
    - name: "Marr"
      role: "NPC"
    - name: "Liss"
      role: "NPC"
      profile_file: "Lore/World/Characters/Liss.md"
log_day: 199
log_dir: "Work/rp/garret/log"
---

## Day Plan: Day 199 (Thursday)

| NPC | Goal Today | Move They Make | Clock/Deadline |
|-----|-----------|----------------|----------------|
| Liss | the props | If Marr needs him hitched, Marr has to come to her. | ~07:35 |
| Marr | the props | His hitch slips off the ring twice in front of him; the third time he sends for Liss. | ~07:35 |

Feeding check each witnessed shame: **no feeder** (logged, not rendered).
"""

ANCHOR = "Scene: Day 199, ~07:49, calm, warm, standing | Location: the yard, at the trough | Present: Garret, Marr, Liss"
DRAFT = f'Garret looks at Marr. "This was what you recommended."\n\nMarr nods once.\n\n{ANCHOR}'
LISS = """# Liss

A vessel grabbing the reins she flips or subverts; she does not fold.
She doesn't argue, she positions.

**Work-vocabulary**, clipped but natural. Imperatives and material nouns, not feelings. Contractions.
Teasing arrives as a dare, never an epigram.
"""


def participants(role):
    return ('scene.time: "07:49"\nscene.participants:\n  - name: "Garret"\n    role: %s\n'
            '  - name: "Marr"\n    role: "NPC"\n  - name: "Liss"\n    role: "NPC"\n'
            '    profile_file: "Lore/World/Characters/Liss.md"' % json.dumps(role))


def turn(source="priced_stake_touched: false\ncanon_proposed: false\npc_changed: false",
         delta='scene.time: "07:49"', draft=DRAFT, keeper="(I look at Marr) This was what you recommended."):
    return (f"KEEPER_INPUT:\n{keeper}\n\nDRAFT:\n{draft}\n\nGM_SPAWN_LOG:\n- marr: NOT spawned (GM voice)\n\n"
            f"SOURCE_LOG:\n{source}\n\nSCENE_STATE_DELTA:\n{delta}\n")


def sourced(*entries, tail="priced_stake_touched: false\ncanon_proposed: false\npc_changed: false"):
    rows = ["sourced:"]
    for claim, file, quote in entries:
        rows += [f"  - claim: {json.dumps(claim)}", f"    file: {json.dumps(file)}", f"    quote: {json.dumps(quote)}"]
    return "\n".join(rows) + "\ninferred: []\n" + tail


class Project:
    def __init__(self, testcase, role=ROLE_4303):
        self._tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for rel, text in ((SESSION, FRONTMATTER % json.dumps(role)), ("Lore/World/Characters/Liss.md", LISS),
                          ("Lore/World/Characters/Marr.md", "# Marr\n")):
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
        got = p.check(turn(delta=participants(ROLE_4304)))
        self.assertNotIn("pc_change", got["triggers"])
        self.assertEqual(got["triggers"], [])
        self.assertEqual(got["route"]["beat"], "routine")
        self.assertEqual(got["route"]["next"], "ship")

    def test_the_narrative_role_is_reported_not_compared(self):
        p = Project(self)
        got = p.check(turn(delta=participants(ROLE_4304)))
        event = next(e for e in got["lint"]["events"] if e["kind"] == "pc_role_narrative")
        self.assertEqual(event["chars"], len(ROLE_4304))

    def test_a_declared_change_still_fires_with_a_narrative_role(self):
        p = Project(self)
        got = p.check(turn(source="pc_changed: true", delta=participants(ROLE_4304)))
        self.assertIn("pc_change", got["triggers"])

    def test_a_change_of_label_still_fires(self):
        p = Project(self, role="PC (calm)")
        got = p.check(turn(delta=participants("PC (antlers grown)")))
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
            ("Marr has to come to her", SESSION, "If Marr needs him hitched, Marr has to come to her."))))
        sources = got["lint"]["sources"]
        self.assertEqual((sources["checked"], sources["verified"]), (1, 1))
        self.assertEqual(sources["quotes"][0]["file"], SESSION)
        self.assertEqual(sources["quotes"][0]["line"], 26)
        self.assertEqual(got["lint"]["findings"], [])
        self.assertEqual(got["route"]["next"], "ship")

    def test_a_paraphrase_is_unverified_and_sends_the_beat_to_review(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("Marr has to come to her", SESSION, "If Marr wants him hitched, he must come to Liss."))))
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
            ("the third time", f"{SESSION} l.131 (Day Plan, Marr row; grep -n puts it at l.126)",
             "His hitch slips off the ring twice in front of him; the third time he sends for Liss."))))
        self.assertEqual(got["lint"]["sources"]["verified"], 1)
        self.assertEqual(got["lint"]["sources"]["quotes"][0]["line"], 27)

    def test_excerpts_joined_by_a_slash_or_an_ellipsis_each_count(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("register", "Lore/World/Characters/Liss.md l.63; l.129",
             "A vessel grabbing the reins she flips or subverts; she does not fold. / She doesn't argue, she positions."),
            ("register", "Lore/World/Characters/Liss.md",
             "Work-vocabulary, clipped but natural ... Contractions."),
            ("register", "Lore/World/Characters/Liss.md",
             "Work-vocabulary, clipped but natural ... Teasing arrives as a pun."))))
        quotes = got["lint"]["sources"]["quotes"]
        self.assertEqual([q["verified"] for q in quotes], [True, True, False])
        self.assertIn("Teasing arrives as a pun", quotes[2]["reason"])

    def test_typography_and_emphasis_do_not_count_against_a_quote(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("feeding", SESSION, "Feeding check each witnessed shame: no feeder"),
            ("register", "Lore/World/Characters/Liss.md", "She doesn’t argue, she positions."),
            ("register", "Lore/World/Characters/Liss.md", "work-vocabulary, CLIPPED but natural"))))
        self.assertEqual([q["verified"] for q in got["lint"]["sources"]["quotes"]], [True, True, True])

    def test_escaped_quote_marks_do_not_count_against_a_quote(self):
        # Two quoted excerpts on one line, as a GM wrote them (B4284): the YAML subset keeps the backslashes.
        p = Project(self, role="PC")
        source = ('sourced:\n  - claim: "the sheet"\n    file: "Lore/World/Characters/Liss.md"\n'
                  '    quote: "\\"She doesn\'t argue,\\" she positions." / "Teasing arrives as a dare, never an epigram."\n')
        got = p.check(turn(source=source))
        self.assertEqual(got["lint"]["sources"]["verified"], 1, got["lint"]["sources"])

    def test_excerpts_may_come_from_different_files_the_entry_names(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("her height against his", f"Lore/World/Characters/Liss.md l.166; {SESSION} l.47",
             "She doesn't argue, she positions. / If Marr needs him hitched, Marr has to come to her."),
            ("one of them wrong", f"Lore/World/Characters/Liss.md; {SESSION}",
             "She doesn't argue, she positions. / If Marr needs him, he comes to her."))))
        quotes = got["lint"]["sources"]["quotes"]
        self.assertEqual([q["verified"] for q in quotes], [True, False])
        self.assertEqual((quotes[0]["file"], quotes[0]["line"]), ("Lore/World/Characters/Liss.md", 4))
        self.assertIn("If Marr needs him, he comes to her.", quotes[1]["reason"])

    def test_a_bare_file_name_resolves_beside_the_session_or_by_name(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("the Day Plan", "day-0199.md l.124", "If Marr needs him hitched, Marr has to come to her."),
            ("the sheet", "Liss.md", "She doesn't argue, she positions."))))
        quotes = got["lint"]["sources"]["quotes"]
        self.assertEqual([q["verified"] for q in quotes], [True, True])
        self.assertEqual(quotes[1]["file"], "Lore/World/Characters/Liss.md")

    def test_the_keepers_input_is_a_source(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("his line", "KEEPER_INPUT (this turn)", "This was what you recommended."),
            ("his line", "KEEPER_INPUT", "This was not what I said."))))
        self.assertEqual([q["verified"] for q in got["lint"]["sources"]["quotes"]], [True, False])

    def test_a_missing_file_or_no_file_is_unverified(self):
        p = Project(self, role="PC")
        got = p.check(turn(source=sourced(
            ("gone", "Lore/World/Characters/Nobody.md", "anything"),
            ("nothing", "the Day Plan above", "If Marr needs him hitched"))))
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
        source = 'sourced:\n  - "a bare string"\n  - claim: "no quote"\n    file: "Liss.md"\n  - claim: "x"\n    quote: 3'
        got = p.check(turn(source=source))
        self.assertEqual(got["lint"]["sources"]["checked"], 3)
        self.assertEqual(got["lint"]["sources"]["verified"], 0)


STRUCK_BODY = """
### Beat 4302: Day 199, ~07:26 to ~07:35 (Thursday). (gate: routine)

Garret waits at the trough with the sand.

GM DECISION (FLAG): Marr's hitch slips twice, and the third time he sends for Liss.

> [!struck]- STRUCK, not canon: Beat 4303, attempt 1, struck 2026-10-28. Reason: invented_mechanics
>
> GM DECISION (B4303): Liss waives the price of the knot.
>
> <!-- rp-struck: 0123456789abcdef -->

Liss will not take him below unless Marr asks, and the price is her hand.
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
        quotes = self.quotes(p, ("the sheet", "Liss.md", "She doesn't argue, she positions."), relative=True)
        self.assertEqual((quotes[0]["verified"], quotes[0]["file"]), (True, "Lore/World/Characters/Liss.md"))

    def test_an_overlong_file_field_is_unverified_not_a_crash(self):
        p = self.project()
        quotes = self.quotes(p, ("x", "the Day Plan row " + "x" * 300 + " day-0199.md", "anything"))
        self.assertFalse(quotes[0]["verified"])

    # S1: what a quote may come from
    def test_a_quote_only_in_a_struck_draft_is_not_a_source(self):
        p = self.project()
        quotes = self.quotes(p, ("the waiver", SESSION, "Liss waives the price of the knot."))
        self.assertFalse(quotes[0]["verified"])
        self.assertIn("struck", quotes[0]["reason"])

    def test_the_session_frontmatter_is_current_state_not_a_source(self):
        p = self.project(role="PC (hooded, collared, warm till noon)")
        quotes = self.quotes(p, ("his state", SESSION, "hooded, collared, warm till noon"))
        self.assertFalse(quotes[0]["verified"])
        self.assertIn("frontmatter", quotes[0]["reason"])

    def test_a_canon_files_frontmatter_is_a_source(self):
        p = self.project()
        p.write("Lore/World/Characters/Marr.md", "---\nage: \"early fifties\"\nheight: \"six foot\"\n---\n# Marr\n")
        quotes = self.quotes(p, ("his age", "Lore/World/Characters/Marr.md", 'age: "early fifties"'))
        self.assertTrue(quotes[0]["verified"])

    def test_the_gate_scratch_and_the_validator_log_are_not_sources(self):
        p = self.project()
        p.write("Work/rp/_validator_log.jsonl", '{"note": "Liss keeps the knot"}\n')
        quotes = self.quotes(p, ("own log", "Work/rp/.gate/turn.md", "a sentence nobody wrote"),
                             ("the log", "Work/rp/_validator_log.jsonl", "Liss keeps the knot"))
        self.assertEqual([q["verified"] for q in quotes], [False, False])
        self.assertTrue(all("not a source" in q["reason"] for q in quotes))

    def test_a_quote_from_an_earlier_beat_is_marked_history_and_a_decision_is_not(self):
        p = self.project()
        quotes = self.quotes(p, ("he waited", SESSION, "Garret waits at the trough with the sand."),
                             ("the slip", SESSION, "Marr's hitch slips twice, and the third time he sends for Liss."))
        self.assertEqual([q["verified"] for q in quotes], [True, True])
        self.assertEqual([q["history"] for q in quotes], ["4302", None])

    def test_cut_excerpts_must_be_in_order(self):
        p = self.project()
        quotes = self.quotes(p, ("order", SESSION, "Liss will not ... the price is her hand."),
                             ("reversed", SESSION, "the price is her hand ... Liss will not"))
        self.assertEqual([q["verified"] for q in quotes], [True, False])

    def test_cut_excerpts_must_be_close(self):
        p = self.project(body=STRUCK_BODY + "\n" + ("Filler line about the yard and the frost.\n" * 40)
                         + "And at the end the price is paid in full.\n")
        quotes = self.quotes(p, ("far", SESSION, "Liss will not take him below ... the price is paid in full."))
        self.assertFalse(quotes[0]["verified"])

    def test_only_an_ellipsis_in_brackets_is_a_cut(self):
        p = self.project()
        quotes = self.quotes(p, ("insert", SESSION, "Liss will [now] take him below"),
                             ("cut", SESSION, "Liss will not [...] unless Marr asks"),
                             ("cut", SESSION, "Liss will not \u2026 unless Marr asks"))
        self.assertEqual([q["verified"] for q in quotes], [False, True, True])

    def test_a_quote_over_two_lines_is_checked_whole(self):
        p = self.project()
        for second, verified in (("and the price is her hand.", True), ("and he pays nothing at all.", False)):
            with self.subTest(second=second):
                source = ('sourced:\n  - claim: "x"\n    file: "%s"\n    quote: "Liss will not take him below '
                          'unless Marr asks,\n      %s"\n  - claim: "y"\n    file: "Liss.md"\n'
                          '    quote: "She doesn\'t argue, she positions."\n' % (SESSION, second))
                quotes = p.check(turn(source=source))["lint"]["sources"]["quotes"]
                self.assertEqual([q["verified"] for q in quotes], [verified, True])

    # S2 and S3: the PC role
    def test_a_label_before_and_a_narrative_after_is_a_pc_change(self):
        p = Project(self, role="PC (hooded)")
        got = p.check(turn(delta=participants(ROLE_4304)))
        self.assertIn("pc_change", got["triggers"])

    def test_a_stat_that_moves_inside_a_narrative_role_is_a_pc_change(self):
        p = Project(self)
        for old, new in (("POW 53/60", "POW 40/60"), ("Luck 23", "Luck 9")):
            with self.subTest(stat=new):
                got = p.check(turn(delta=participants(ROLE_4304.replace(old, new))))
                self.assertIn("pc_change", got["triggers"])

    def test_the_narrative_event_says_which_side_narrates(self):
        p = Project(self)
        got = p.check(turn(delta=participants("PC")))
        event = next(e for e in got["lint"]["events"] if e["kind"] == "pc_role_narrative")
        self.assertEqual((event["chars"], event["before_chars"]), (2, len(ROLE_4303)))

    def test_narrative_roles_the_delta_leaves_in_place_are_reported(self):
        p = Project(self)
        got = p.check(turn())
        event = next(e for e in got["lint"]["events"] if e["kind"] == "roles_narrative")
        self.assertEqual(event["names"], ["Garret"])
        got = p.check(turn(delta=participants("PC")))
        self.assertFalse([e for e in got["lint"]["events"] if e["kind"] == "roles_narrative"])

    # minor findings
    def test_a_file_name_with_commas_or_parentheses_resolves(self):
        p = self.project()
        p.write("Lore/World/Bestiary/Shadow (HSI).md", "A shadow eats the light it stands in.\n")
        p.write("Lore/World/Forces, Faculties, and Axes.md", "Intent is the first faculty.\n")
        quotes = self.quotes(p, ("shadow", "Lore/World/Bestiary/Shadow (HSI).md", "eats the light it stands in"),
                             ("intent", "Lore/World/Forces, Faculties, and Axes.md l.1", "Intent is the first faculty."))
        self.assertEqual([q["verified"] for q in quotes], [True, True])

    def test_a_path_outside_the_project_is_not_read(self):
        p = self.project()
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "notes.md"
            outside.write_text("Liss waives every price.\n", encoding="utf-8")
            quotes = self.quotes(p, ("outside", str(outside), "Liss waives every price."),
                                 ("climb", "../" + Path(other).name + "/notes.md", "Liss waives every price."))
        self.assertEqual([q["verified"] for q in quotes], [False, False])

    def test_the_name_search_skips_dot_directories(self):
        p = self.project()
        p.write(".obsidian/Secret.md", "Liss waives every price.\n")
        quotes = self.quotes(p, ("hidden", "Secret.md", "Liss waives every price."))
        self.assertFalse(quotes[0]["verified"])

    def test_the_file_beside_the_session_wins_over_one_of_the_same_name(self):
        p = self.project()
        p.write("Work/rp/garret/log/notes.md", "The beside copy holds this line.\n")
        p.write("notes.md", "The copy at the root holds another line.\n")
        quotes = self.quotes(p, ("beside", "notes.md", "The beside copy holds this line."))
        self.assertEqual((quotes[0]["verified"], quotes[0]["file"]), (True, "Work/rp/garret/log/notes.md"))

    def test_compatibility_forms_and_dashes_do_not_count(self):
        p = self.project()
        p.write("Lore/fire.md", "The fire is low\u2014and nobody feeds it. POW \uff15\uff13/\uff16\uff10.\n")
        quotes = self.quotes(p, ("fire", "Lore/fire.md", "The fire is low -- and nobody feeds it."),
                             ("fire", "Lore/fire.md", "The fire is low - and nobody feeds it."),
                             ("pow", "Lore/fire.md", "POW 53/60"))
        self.assertEqual([q["verified"] for q in quotes], [True, True, True])

    def test_a_quote_of_punctuation_or_one_letter_checks_nothing(self):
        p = self.project()  # the session's Day Plan table is full of "|"
        quotes = self.quotes(p, ("table", SESSION, "|"), ("dash", SESSION, "--"), ("letter", SESSION, "a"),
                             ("cut", SESSION, "If Marr ... |"))
        self.assertEqual([q["verified"] for q in quotes], [False, False, False, True])
        self.assertEqual([q["reason"] for q in quotes[:3]], ["no quote"] * 3)

    def test_a_block_scalar_quote_is_checked_whole(self):
        p = self.project()
        source = ('sourced:\n  - claim: "x"\n    file: "%s"\n    quote: |\n      Liss will not take him below\n'
                  '      unless Marr asks\n' % SESSION)
        self.assertTrue(p.check(turn(source=source))["lint"]["sources"]["quotes"][0]["verified"])

    def test_a_line_number_written_after_a_colon_is_ignored(self):
        p = self.project()
        quotes = self.quotes(p, ("plan", SESSION + ":26", "If Marr needs him hitched, Marr has to come to her."))
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
