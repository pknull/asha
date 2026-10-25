"""Tests for plugins/rp/tools/rp_gate.py: the per-beat lint, routing and logging."""
import importlib.util
import io
import json
import re
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "plugins/rp/tools/rp_gate.py"
spec = importlib.util.spec_from_file_location("rp_gate", SCRIPT)
assert spec is not None and spec.loader is not None
rp_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp_gate)

FRONTMATTER = """---
campaign: "Test"
currentDate: "Day 4"
currentLocation: "the yard"
currentTime: "15:00"
participants: ["Alder", "Liss", "Mira"]
scene:
  time: "15:00"
  location: "the yard"
  participants:
    - name: "Alder"
      role: "PC (calm)"
    - name: "Liss"
      role: "NPC"
      profile_file: "Lore/World/Characters/Liss.md"
    - name: "Mira"
      role: "NPC"
  mood: "work"
  pending_rolls: []
  power_holder: null
---

# Session

## Day Plan: Day 4
"""


def turn(draft, delta="none", source="priced_stake_touched: false", keeper="I keep working."):
    return (
        f"KEEPER_INPUT:\n{keeper}\n\nDRAFT:\n{draft}\n\n"
        f"GM_SPAWN_LOG:\n- liss: NOT spawned (routine)\n\n"
        f"SOURCE_LOG:\n{source}\n\nSCENE_STATE_DELTA:\n{delta}\n"
    )


class Project:
    """A throwaway project directory with the conventional RP layout."""

    def __init__(self, testcase):
        self._tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "Work/markers").mkdir(parents=True)
        (self.root / "Work/rp").mkdir(parents=True)
        (self.root / "Lore/World/Characters").mkdir(parents=True)
        for name in ("Liss", "Mira", "Vey", "Velathra"):
            (self.root / "Lore/World/Characters" / f"{name}.md").write_text(f"# {name}\n")

    def session(self, name="rp_session_2026-10-17.md", body=FRONTMATTER):
        path = self.root / "Work/rp" / name
        path.write_text(body, encoding="utf-8")
        return path

    def marker(self, text=""):
        (self.root / "Work/markers/rp-active").write_text(text, encoding="utf-8")

    def mode(self, text):
        path = self.root / ".claude/modes/rp.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def turn_file(self, text):
        path = self.root / "Work/rp/.gate/turn.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def log_lines(self):
        log = self.root / "Work/rp/_validator_log.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rp_gate.main(list(argv))
    return code, out.getvalue(), err.getvalue()


ANCHOR = "Scene: Day 4, ~15:00, calm | Location: the yard | Present: Liss, Mira"
LATER = "Scene: Day 4, ~15:05, calm | Location: the yard | Present: Liss, Mira"


class SessionFileTests(unittest.TestCase):
    def test_marker_names_the_session_file(self):
        p = Project(self)
        p.session("rp_session_2026-10-17.md")
        p.session("rp_session_2026-10-20.md")
        p.marker("session_file: Work/rp/rp_session_2026-10-17.md\n")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-24")
        self.assertEqual(got["path"], "Work/rp/rp_session_2026-10-17.md")
        self.assertEqual(got["source"], "marker")
        self.assertIsNone(got["warning"])

    def test_multi_day_session_does_not_follow_the_clock(self):
        # The 10-17 log ran seven real days; date +%Y-%m-%d named a new file each day.
        p = Project(self)
        p.session("rp_session_2026-10-17.md")
        p.marker("")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-23")
        self.assertEqual(got["path"], "Work/rp/rp_session_2026-10-17.md")
        self.assertEqual(got["source"], "latest")
        self.assertIn("marker", got["warning"])

    def test_fallback_skips_a_closed_session(self):
        p = Project(self)
        p.session("rp_session_2026-10-17.md")
        p.session("rp_session_2026-10-20.md", body="---\nstatus: closed\n---\n")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-24")
        self.assertEqual(got["path"], "Work/rp/rp_session_2026-10-17.md")

    def test_marker_naming_a_missing_file_warns_and_falls_back(self):
        p = Project(self)
        p.session("rp_session_2026-10-17.md")
        p.marker("session_file: Work/rp/rp_session_2026-09-01.md\n")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-24")
        self.assertEqual(got["path"], "Work/rp/rp_session_2026-10-17.md")
        self.assertIn("rp_session_2026-09-01.md", got["warning"])

    def test_marker_cannot_point_outside_the_session_directory(self):
        p = Project(self)
        p.session("rp_session_2026-10-17.md")
        p.marker("session_file: ../../etc/passwd\n")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-24")
        self.assertEqual(got["path"], "Work/rp/rp_session_2026-10-17.md")
        self.assertIsNotNone(got["warning"])

    def test_no_session_yet_names_todays_file(self):
        p = Project(self)
        got = rp_gate.resolve_session_file(p.root, today="2026-10-24")
        self.assertEqual(got["path"], "Work/rp/rp_session_2026-10-24.md")
        self.assertEqual(got["source"], "new")
        self.assertIsNotNone(got["warning"])

    def test_session_reports_the_gate_settings(self):
        p = Project(self)
        p.session("rp_session_2026-10-17.md")
        p.marker("session_file: Work/rp/rp_session_2026-10-17.md\n")
        p.mode("turnGate:\n  models:\n    gm: haiku\n")
        code, out, _err = run("session", "--project-dir", str(p.root))
        gate = json.loads(out)["gate"]
        self.assertEqual(gate["models"]["gm"], "haiku")
        self.assertEqual(gate["review"], "consequential")

    def test_session_directory_symlinked_elsewhere(self):
        p = Project(self)
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        (p.root / "Work/rp").rmdir()
        (p.root / "Work/rp").symlink_to(elsewhere.name)
        p.session("rp_session_2026-10-17.md")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-24")
        self.assertEqual(got["path"], "Work/rp/rp_session_2026-10-17.md")

    def test_cli_prints_the_resolution_and_warns_on_stderr(self):
        p = Project(self)
        p.session("rp_session_2026-10-17.md")
        code, out, err = run("session", "--project-dir", str(p.root))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["path"], "Work/rp/rp_session_2026-10-17.md")
        self.assertIn("WARNING", err)


class LintTests(unittest.TestCase):
    def lint(self, text, project=None, session_body=FRONTMATTER):
        p = project or Project(self)
        session = p.session(body=session_body)
        config = rp_gate.load_config(p.root)
        return rp_gate.lint(rp_gate.parse_turn(text), rp_gate.load_scene(p.root, session), config, p.root)

    def checks(self, result):
        return sorted({f["check"] for f in result["findings"]})

    def test_clean_routine_beat(self):
        draft = f'Liss sets down the drawknife. "Shed."\nMira watches from the step.\n\n{ANCHOR}'
        result = self.lint(turn(draft))
        self.assertEqual(result["findings"], [])

    def test_missing_anchor(self):
        result = self.lint(turn('Liss sets down the drawknife. "Shed."'))
        self.assertEqual(self.checks(result), ["anchor_missing"])
        self.assertEqual(result["findings"][0]["fix"], "edit")

    def test_duplicated_anchor(self):
        result = self.lint(turn(f"{ANCHOR}\nLiss nods.\n{ANCHOR}"))
        self.assertIn("anchor_duplicated", self.checks(result))

    def test_anchor_must_close_the_draft(self):
        result = self.lint(turn(f"Liss nods.\n{ANCHOR}\nMira looks up."))
        self.assertIn("anchor_not_last", self.checks(result))

    def test_absent_character_speaking_is_a_presence_miss(self):
        # The Vey case: a named character proposes while not in the scene.
        draft = f'Liss holds the beam. Vey, from nowhere: "Can you still hang a door?"\n\n{ANCHOR}'
        result = self.lint(turn(draft))
        self.assertEqual(self.checks(result), ["presence"])
        finding = result["findings"][0]
        self.assertEqual(finding["name"], "Vey")
        self.assertEqual(finding["fix"], "review")

    def test_absent_character_acting_is_a_presence_miss(self):
        result = self.lint(turn(f"Vey takes the hammer from the bench.\n\n{ANCHOR}"))
        self.assertEqual(self.checks(result), ["presence"])

    def test_all_caps_log_style_speaker_is_recognised(self):
        result = self.lint(turn(f'VEY: "The door."\n\n{ANCHOR}'))
        self.assertEqual(self.checks(result), ["presence"])

    def test_word_sent_by_carrier_passes(self):
        draft = f'Mira comes back from the gate. Vey sends word: the hinges are ready.\n\n{ANCHOR}'
        result = self.lint(turn(draft))
        self.assertEqual(result["findings"], [])
        self.assertIn("carried", {e["kind"] for e in result["events"]})

    def test_arrival_on_the_page_passes(self):
        draft = f'Vey comes in from the yard and sets down the hinges. "For the door."\n\n{ANCHOR}, Vey'
        result = self.lint(turn(draft, delta='scene.participants: ["Alder", "Liss", "Mira", "Vey"]'))
        self.assertEqual(result["findings"], [])
        self.assertIn("arrival", {e["kind"] for e in result["events"]})

    def test_coming_to_the_door_is_an_arrival(self):
        draft = f'Vey came to the stair door with a candle: "I heard you." Then went back up.\n\n{ANCHOR}'
        result = self.lint(turn(draft))
        self.assertNotIn("presence", self.checks(result))
        self.assertIn("arrival", {e["kind"] for e in result["events"]})

    def test_arrival_without_listing_passes_presence(self):
        draft = f'Vey steps in through the door, nods once, and goes out again.\n\n{ANCHOR}'
        result = self.lint(turn(draft))
        self.assertNotIn("presence", self.checks(result))

    def test_mentions_inside_dialogue_and_possessive_objects_pass(self):
        draft = (
            'Liss taps the roll under his arm. "Velathra drew these. Vey wants them back."\n'
            "Velathra's plans are still under his arm.\n\n" + ANCHOR
        )
        result = self.lint(turn(draft))
        self.assertEqual(result["findings"], [])

    def test_absent_character_noted_as_away_passes(self):
        draft = f"Liss nods toward the road. Bren is away at the market.\n\n{ANCHOR}"
        project = Project(self)
        (project.root / "Lore/World/Characters/Bren.md").write_text("# Bren\n")
        result = self.lint(turn(draft), project=project)
        self.assertEqual(result["findings"], [])

    def test_alias_of_a_present_full_name(self):
        body = FRONTMATTER.replace('    - name: "Mira"', '    - name: "Ildric Marr"\n      role: "NPC"\n    - name: "Mira"')
        draft = 'MARR: "Props tonight."\n\nScene: Day 4 | Location: the vault | Present: Liss, Mira, Marr'
        result = self.lint(turn(draft), session_body=body)
        self.assertEqual(result["findings"], [])

    def test_shared_surnames_are_different_people(self):
        p = Project(self)
        p.mode("turnGate:\n  cast: [Odo Brand]\n")
        body = FRONTMATTER.replace('    - name: "Mira"', '    - name: "Hesta Brand"\n      role: "NPC"\n    - name: "Mira"')
        draft = 'Odo Brand nods to her: "Hes."\n\nScene: Day 4 | Location: the yard | Present: Liss, Mira, Hesta Brand'
        result = self.lint(turn(draft), project=p, session_body=body)
        self.assertEqual(self.checks(result), ["presence"])
        self.assertEqual(result["findings"][0]["name"], "Odo Brand")

    def test_first_name_of_a_listed_newcomer(self):
        body = FRONTMATTER.replace('    - name: "Mira"', '    - name: "Hesta Brand"\n      role: "NPC"\n    - name: "Mira"')
        draft = ('Liss and Odo cross from the barn. ODO stops at the door: "Hes."\n\n'
                 'Scene: Day 4 | Location: the yard | Present: Liss, Mira, Hesta Brand, Odo Brand (the door)')
        result = self.lint(turn(draft, delta='scene.participants: [Alder, Liss, Mira, Hesta Brand, Odo Brand]'),
                           session_body=body)
        self.assertEqual(result["findings"], [])

    def test_semicolon_continues_the_present_list(self):
        draft = 'Liss, from the hearth: "That\'s what the pots are for."\n\nScene: Day 4 | Location: the yard | Present: Mira; Liss by the hearth'
        result = self.lint(turn(draft))
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["present"], ["Mira", "Liss by the hearth"])

    def test_common_word_character_names_at_sentence_start(self):
        p = Project(self)
        (p.root / "Lore/World/Characters/Frost.md").write_text("# Frost\n")
        draft = f"The pulse loud through the floor; frost came up in rings. Frost runs along the passage wall.\n\n{ANCHOR}"
        result = self.lint(turn(draft), project=p)
        self.assertEqual(result["findings"], [])

    def test_titled_names_match_only_in_full(self):
        p = Project(self)
        (p.root / "Lore/World/Characters/The Frost Keeper.md").write_text("# The Frost Keeper\n")
        result = self.lint(turn(f"Frost runs along the passage wall. The Keeper waits.\n\n{ANCHOR}"), project=p)
        self.assertEqual(result["findings"], [])
        result = self.lint(turn(f"The Frost Keeper lifts a hand.\n\n{ANCHOR}"), project=p)
        self.assertEqual(self.checks(result), ["presence"])

    # --- review findings (cold review, 2026-10-24) ---

    def test_carrier_word_after_a_quote_does_not_clear_the_speaker(self):
        for draft in ('Vey: "Hang it." Mira writes the figure on the slate.',
                      '"Hang it," Vey says, and sends the boy for nails.',
                      'Vey: "Can you still hang a door?" Liss sends him a look.'):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"))
                self.assertEqual(self.checks(result), ["presence"])

    def test_plural_subject_and_dash_attribution(self):
        for draft in ("Liss and Vey lift the beam.", "Vey and Liss lift the beam.", 'Vey — "Hang it."'):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"))
                self.assertEqual(self.checks(result), ["presence"])

    def test_irregular_past_in_a_past_tense_draft(self):
        result = self.lint(turn(f"Liss stood at the bench. Vey knelt by the door and spat.\n\n{ANCHOR}"))
        self.assertEqual(self.checks(result), ["presence"])

    def test_mentions_that_are_not_actions_pass(self):
        for draft in ("Liss takes the hammer. She says the Vey brothers sold it to her.",
                      "Liss knows Vey hates the cold.", "Liss says Vey must be at the mill by now.",
                      "Liss nods toward the road. Vey will be back by noon.", "Vey's letter sits on the bench.",
                      "She glances at the stool where Vey used to sit.", "Alder asks whether Vey is coming back.",
                      "~08:00 VEY'S YARD: the gate stands open.", "Vey is away at the mill.", "Vey has gone to the mill.",
                      "Liss reads Vey's letter twice.", "A letter from Vey lies on the bench.", "Liss thinks of Vey.",
                      "Without Vey the yard is quiet.", "Liss talks about Vey all morning."):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"))
                self.assertEqual(result["findings"], [])

    def test_absent_character_on_the_page_is_flagged(self):
        """The core case. A false positive costs a cheap review; a miss ships unreviewed."""
        drafts = [
            # past-tense drafts, with plural nouns that look present
            "Liss stood at the bench. Vey knelt by the door and spat. The shutters banged. The others waited.",
            "Vey took the hammer. The nails rattled in the tin.",
            "The shutters banged. Vey laughed.",
            # base-form plurals in present tense
            "Mira watches. Liss and Vey set the beam on the trestles.",
            "Mira watches. Liss and Vey lay the beam down.",
            "Mira watches. Liss and Vey put the beam down.",
            "He and Vey lift the beam.", "The boy and Vey lift the beam.",
            # modals and contractions
            "Vey will not budge.", "Vey could only stare at him.", "Vey would not look at him.",
            "Vey must decide now.", "Vey can still hang a door, and does.", "Vey doesn't look up from the bench.",
            # speech tags in every quoting style
            '"Hang it", said Vey.', '"Hang it", says Vey.', "'Hang it,' said Vey.", "‘Hang it,’ said Vey.",
            '*"Hang it,"* said Vey.', '**"Hang it,"** says Vey.', '**Vey:** "Hang it."', 'VEY (low): "Hang it."',
            '"Hang it" — Vey, from the bench.', 'Vey — quiet — "Hang it."',
            # words before the name that are not prepositions
            "With that Vey takes the hammer.", "At that Vey laughs.", "Behind her Vey laughs.",
            "He sees that Vey has the hammer already.",
            # carriers that carry nothing, or carry and then act
            "Vey sends word: the hinges are ready. Then Vey takes the hammer from the bench.",
            'Vey writes a note. "Hang it," says Vey.', "Vey sends him a look across the bench.",
            # ordinary phrases that are not arrivals
            "Vey looks up from the bench.", "Vey takes the hammer out of the box.",
            "Vey takes it down from the shelf.", "Vey returns the hammer to the bench.",
            "Vey follows his gaze.", "Vey comes to a decision.",
            # script-style speech, and negation that is not absence
            "Vey: *Hang it.*", "Vey is not happy about it.",
            # ordinary prose with no recognisable verb right after the name (third review)
            "Vey just shrugs from the woodpile.", "Vey still has the hammer.", "Vey never looks up.",
            "Vey then turns to Mira.", "Vey cannot quite hide the grin.",
            "Vey — sweat running into his eyes — braces it.", "Vey (from the bench, not looking up) grunts.",
            "Vey's eyes narrow.", "Vey's already at the bench.", "Vey'll take it.",
            "Liss looks to Vey, who only nods.", "Before Vey can answer, Liss turns.",
            "Vey, Liss and Mira lift the beam.", "Liss and Vey together lift the beam.",
            '"Not yet," mutters Vey.', 'Then said Vey, "Hang it."', "*Hang it,* said Vey.",
            "Liss asks Vey for the hammer.", "Vey and Liss always argue about the hammer.",
            "Liss waits until Vey takes the hammer.", "Since Vey takes the far end, Liss lifts.",
            # fourth review: excuse words across punctuation
            "Lost in thought, Vey turns another page of the ledger.", "As Liss feared, Vey is already at the table.",
            "Whatever Liss thought, Vey took the hammer.", "Nothing to worry about, Vey said, and took the hammer.",
            "It was as Liss had guessed, Vey was there before her.",
            '"Hang it," Liss said, Vey already reaching for the hammer.', "Whatever she said, Vey just shrugged.",
            # absence phrases that are not absence
            "Vey had left the gate open behind him and now leans on it.", "Vey was still out of breath when he took the hammer.",
            "Vey is asleep on the bench by the hearth, mouth open.", "Vey is out in the yard, splitting wood.",
            "Vey is off the cart before it stops.", "Vey has gone to the bench and taken the hammer.",
            "Vey is dead on the flags at Liss's feet.",
            # speaker labels after an excuse word
            'A shout from Vey: "Shut that door!"',
            # possessives and prepositions that are physical
            "Vey's grin is crooked as he leans on the doorframe.", "Vey's smile doesn't reach his eyes.",
            "Vey's laughter rings across the yard.", "Vey's tail flicks once.", "Vey's knuckles whiten on the table.",
            "Vey's left hand closes on the hammer.", "Vey's taken the far end of the beam.", "Vey's quiet for a long moment.",
            "Vey's over by the bench.", "Liss takes the cup from Vey and drinks.", "Liss plants herself in front of Vey.",
            "Liss pulls the hammer out of Vey's hands.", "A low whistle from Vey, somewhere behind the woodpile.",
            # carried phrases that carry nothing
            "Vey sends the runner off with a clap on the shoulder.", "From the corner table, Vey sends for another jug.",
            "Vey sends for the hammer, reaching across Liss to take it.", "Vey sends Liss a note across the table, grinning.",
            "Vey sent for Liss, but he was already at the bench when she came.",
            # underscore emphasis
            "_Vey takes the hammer._", "Liss nods. _Vey_ grins.",
            # excuse words used physically, or as nouns
            "Liss wraps her arms about Vey.", "Liss is taller than Vey, who ducks under the beam.",
            "Liss moves like Vey does, slow and heavy.", "Deep in thought Vey stares at the fire.",
            "Without a second thought Vey takes the hammer.", "Vey used to the weight, lifts the beam alone.",
            "Vey is missing a tooth when he grins.", "Vey sends word with a look, and Liss nods.",
            "Vey sends the boy for nails and takes the hammer himself.", "Vey sends the boy flying with a kick.",
            # stray or mixed quote marks, and an anchor sharing the line
            '“Hang it," said Vey, and took the hammer.', 'The beam is 12" thick. Vey takes the hammer.',
        ]
        for draft in drafts:
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"))
                self.assertIn("presence", self.checks(result))

    def test_anchor_sharing_a_line_with_prose(self):
        result = self.lint(turn("Vey takes the hammer. " + ANCHOR))
        self.assertIn("presence", self.checks(result))

    def test_title_only_and_configured_common_word_names(self):
        p = Project(self)
        (p.root / "Lore/World/Characters/Captain.md").write_text("# Captain\n")
        p.mode("turnGate:\n  cast: [Will]\n")
        for draft in ("Captain takes the hammer.", 'CAPTAIN: "Hang it."', "Will takes the hammer. Liss will not look at him."):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"), project=p)
                self.assertIn("presence", self.checks(result))

    def test_name_shapes(self):
        p = Project(self)
        for stem in ("McAllister", "Anne-Marie", "O'Brien", "ildric-marr", "Bo", "The Frost Keeper", "Captain", "Warden",
                     "The Principal", "Sergeant"):
            (p.root / "Lore/World/Characters" / f"{stem}.md").write_text("x\n")
        for draft in ("McAllister takes the hammer.", "Anne-Marie takes the hammer.", "O'Brien takes the hammer.",
                      "Ildric Marr takes the hammer.", "Bo takes the hammer.", "Liss turns to the Frost Keeper, who nods.",
                      "The Captain takes the hammer.", 'THE WARDEN: "Hang it."', "The Principal takes the hammer.",
                      "Liss turns to the Warden, who nods.", "Liss salutes the Warden.", "The Sergeant takes the hammer."):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"), project=p)
                self.assertIn("presence", self.checks(result))

    def test_returning_is_an_arrival(self):
        for draft in ("Vey is back from the mill.", "Vey is back. He takes the hammer from Liss without a word.",
                      "As Liss feared, Vey comes in through the gate.", "Vey has left the mill and walks into the yard.",
                      "Vey had gone to the well and now comes in through the gate with the buckets.",
                      "Vey sends word ahead and comes in through the gate behind it.",
                      "Vey sent word, then arrived on the heels of it."):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"))
                self.assertNotIn("presence", self.checks(result))
                self.assertIn("arrival", {e["kind"] for e in result["events"]})

    def test_common_word_name_after_a_quote_still_counts(self):
        p = Project(self)
        (p.root / "Lore/World/Characters/Ash.md").write_text("# Ash\n")
        result = self.lint(turn(f'The grate is grey with ash. Liss nods. "Hang it." Ash takes the hammer.\n\n{ANCHOR}'),
                           project=p)
        self.assertEqual(self.checks(result), ["presence"])

    def test_pronouns_are_never_names(self):
        for word in ("His", "Her", "Their", "She", "They", "This"):
            self.assertEqual(rp_gate.name_tokens(word), set())
        self.assertEqual(rp_gate.name_tokens("His Majesty"), {"majesty"})

    def test_more_title_abbreviations_in_present(self):
        result = self.lint(turn("Liss nods.\n\nScene: Day 4 | Location: the yard | Present: Liss, Capt. Holt, J. Rook, Mira"))
        self.assertEqual(result["present"], ["Liss", "Capt. Holt", "J. Rook", "Mira"])
        result = self.lint(turn("Liss nods.\n\nScene: Day 4 | Location: the yard | Present: Liss, Mira — etc."))
        self.assertEqual(result["present"], ["Liss", "Mira"])

    def test_non_list_participants_do_not_crash(self):
        result = self.lint(turn(f"Liss nods.\n\n{ANCHOR}", delta="scene.participants: 2"))
        self.assertIn("delta_malformed", self.checks(result))
        body = FRONTMATTER.replace('participants: ["Alder", "Liss", "Mira"]', "participants: true")
        body = body.replace("  participants:\n", "  people:\n")
        self.lint(turn(f"Liss nods.\n\n{ANCHOR}"), session_body=body)

    def test_heard_absent_character_is_on_the_page(self):
        result = self.lint(turn(f"He hears Liss and Vey below in the kitchen, voices raised.\n\n{ANCHOR}"))
        self.assertEqual(self.checks(result), ["presence"])

    def test_attribution_after_a_question_still_speaks(self):
        result = self.lint(turn(f'"Can you hang a door?" asks Vey.\n\n{ANCHOR}'))
        self.assertEqual(self.checks(result), ["presence"])

    def test_titled_unnamed_and_abbreviated_people(self):
        body = FRONTMATTER.replace('    - name: "Mira"',
                                   '    - name: "Mother"\n      role: "NPC"\n    - name: "Mr. Holt"\n'
                                   '      role: "NPC"\n    - name: "two regulars"\n      role: "NPC"\n    - name: "Mira"')
        draft = "Liss nods.\n\nScene: Day 4 | Location: the yard | Present: Mr. Holt, Mother, Liss, two regulars, Mira"
        result = self.lint(turn(draft), session_body=body)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["present"], ["Mr. Holt", "Mother", "Liss", "two regulars", "Mira"])

    def test_present_none(self):
        body = FRONTMATTER.replace('    - name: "Liss"\n      role: "NPC"\n      profile_file: "Lore/World/Characters/Liss.md"\n', "")
        body = body.replace('    - name: "Mira"\n      role: "NPC"\n', "")
        result = self.lint(turn("He waits.\n\nScene: Day 4 | Location: the yard | Present: none (the house within)"),
                           session_body=body)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["present"], [])

    def test_frontmatter_with_a_byte_order_mark(self):
        result = self.lint(turn(f"Liss nods.\n\n{ANCHOR}"), session_body="\ufeff" + FRONTMATTER)
        self.assertEqual(result["findings"], [])

    def test_unknown_delta_key(self):
        draft = f"Liss nods.\n\n{ANCHOR}"
        result = self.lint(turn(draft, delta='scene.time: "15:00"\nscene.weather: "rain"'))
        self.assertEqual(self.checks(result), ["delta_unknown_key"])
        self.assertIn("scene.weather", result["findings"][0]["detail"])

    def test_root_mirror_delta_key(self):
        draft = f"Liss nods.\n\n{ANCHOR}"
        result = self.lint(turn(draft, delta='scene.time: "15:00"\ncurrentTime: "15:05"'))
        self.assertEqual(self.checks(result), ["delta_root_mirror"])

    def test_nested_delta_and_fenced_block(self):
        draft = f"Liss nods.\n\n{ANCHOR}"
        delta = '```yaml\nscene:\n  time: "15:00"\n  mood: "tense"\n```'
        result = self.lint(turn(draft, delta=delta))
        self.assertEqual(result["findings"], [])
        self.assertEqual(sorted(result["delta"]), ["scene.mood", "scene.time"])

    def test_extra_delta_keys_from_config(self):
        p = Project(self)
        p.mode("turnGate:\n  deltaKeys: [scene.weather]\n")
        draft = f"Liss nods.\n\n{ANCHOR}"
        result = self.lint(turn(draft, delta='scene.weather: "rain"'), project=p)
        self.assertEqual(result["findings"], [])

    def test_present_change_without_participants_delta(self):
        draft = "Mira goes in through the back door.\n\nScene: Day 4 | Location: the yard | Present: Liss"
        result = self.lint(turn(draft, delta='scene.time: "15:00"'))
        self.assertEqual(self.checks(result), ["delta_participants_missing"])

    def test_delta_participants_disagree_with_anchor(self):
        draft = "Mira goes in through the back door.\n\nScene: Day 4 | Location: the yard | Present: Liss"
        result = self.lint(turn(draft, delta='scene.participants: ["Alder", "Liss", "Mira"]'))
        self.assertEqual(self.checks(result), ["delta_participants_mismatch"])

    def test_anchor_time_without_time_delta(self):
        result = self.lint(turn(f"Liss nods.\n\n{LATER}", delta='scene.mood: "tense"'))
        self.assertEqual(self.checks(result), ["delta_time_missing"])
        self.assertEqual(result["findings"][0]["fix"], "edit")

    def test_anchor_time_agrees_with_delta(self):
        result = self.lint(turn(f"Liss nods.\n\n{LATER}", delta='scene.time: "15:05"'))
        self.assertEqual(result["findings"], [])

    def test_anchor_time_disagrees_with_delta(self):
        result = self.lint(turn(f"Liss nods.\n\n{LATER}", delta='scene.time: "16:00"'))
        self.assertEqual(self.checks(result), ["delta_time_mismatch"])

    def test_lint_is_fast(self):
        p = Project(self)
        session = p.session()
        scene = rp_gate.load_scene(p.root, session)
        config = rp_gate.load_config(p.root)
        parsed = rp_gate.parse_turn(turn(("Liss nods. Mira looks up. " * 200) + "\n\n" + ANCHOR))
        start = time.perf_counter()
        for _ in range(20):
            rp_gate.lint(parsed, scene, config, p.root)
        self.assertLess((time.perf_counter() - start) / 20, 0.05)


class RouteTests(unittest.TestCase):
    def check(self, p, text, *extra):
        p.session()
        tf = p.turn_file(text)
        code, out, err = run("check", "--project-dir", str(p.root), "--session-file",
                             "Work/rp/rp_session_2026-10-17.md", "--turn-file", str(tf), *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_routine_clean_beat_ships_without_review(self):
        p = Project(self)
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["next"], "ship")
        self.assertEqual(got["route"]["beat"], "routine")
        self.assertEqual(got["route"]["max_rewrites"], 1)
        self.assertEqual([line["path"] for line in p.log_lines()], ["lint", "skipped"])

    def test_lint_flag_routes_to_the_haiku_pass(self):
        p = Project(self)
        got = self.check(p, turn(f'Vey: "Hang it."\n\n{ANCHOR}'))
        self.assertEqual(got["route"]["next"], "review")
        self.assertEqual(got["route"]["beat"], "routine")
        self.assertEqual(got["route"]["reviewer_model"], "haiku")
        self.assertEqual(got["route"]["adjudicator_model"], "sonnet")
        self.assertIn("lint:presence", got["route"]["reasons"])
        self.assertEqual([line["path"] for line in p.log_lines()], ["lint"])

    def test_edit_fixable_findings_ask_for_a_fix_then_review_on_recheck(self):
        p = Project(self)
        got = self.check(p, turn("Liss nods."))
        self.assertEqual(got["route"]["next"], "fix")
        got = self.check(p, turn("Liss nods."), "--recheck")
        self.assertEqual(got["route"]["next"], "review")

    def test_final_relint_after_review_never_asks_for_review_again(self):
        p = Project(self)
        got = self.check(p, turn(f'Vey: "Hang it."\n\n{ANCHOR}', source="priced_stake_touched: true"), "--final")
        self.assertEqual(got["route"]["next"], "ship")
        got = self.check(p, turn('Vey: "Hang it."'), "--final")
        self.assertEqual(got["route"]["next"], "fix")
        self.assertEqual([line["path"] for line in p.log_lines()], ["lint", "lint"])
        self.assertTrue(all(line["final"] for line in p.log_lines()))

    def test_everyone_leaving_is_a_departure(self):
        p = Project(self)
        draft = "Liss and Mira go out through the back door and are gone.\n\nScene: Day 4 | Location: the yard | Present: none (Alder alone)"
        got = self.check(p, turn(draft, delta="scene.participants: [Alder]"))
        self.assertIn("arrival_departure", got["triggers"])
        self.assertEqual(got["route"]["next"], "review")

    def test_any_later_attempt_is_a_rewrite(self):
        p = Project(self)
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}"), "--attempt", "2", "--recheck")
        self.assertEqual(got["route"]["next"], "review")

    def test_a_rewrite_is_always_reviewed(self):
        p = Project(self)
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}"), "--attempt", "2", "--rewrite")
        self.assertEqual(got["route"]["next"], "review")
        self.assertEqual([line["path"] for line in p.log_lines()], ["lint"])
        got = self.check(p, turn("Liss nods."), "--rewrite")
        self.assertEqual(got["route"]["next"], "fix")

    def test_validate_tag_reviews_even_under_lint_only(self):
        p = Project(self)
        p.mode("turnGate:\n  review: lint-only\n")
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}", keeper="I wait. [validate]"))
        self.assertEqual(got["route"]["next"], "review")

    def test_quoted_booleans_still_declare(self):
        p = Project(self)
        for source in ('priced_stake_touched: "true"', "priced_stake_touched: 'true'", '"priced_stake_touched": true'):
            with self.subTest(source=source):
                got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}", source=source))
                self.assertIn("priced_stake", got["triggers"])

    def test_fight_pattern_false_friends(self):
        p = Project(self)
        got = self.check(p, turn(f"Liss fights a smile. Her wounded pride shows. She hits her stride.\n\n{ANCHOR}"))
        self.assertNotIn("fight_or_capture", got["triggers"])

    def test_non_mapping_config_values_warn_instead_of_crashing(self):
        for block in ("  models: sonnet\n", "  models: [haiku]\n", "  rewrites: 2\n",
                      "  models:\n    - reviewer: sonnet\n"):
            with self.subTest(block=block):
                p = Project(self)
                p.mode("turnGate:\n" + block)
                got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}"))
                self.assertEqual(got["route"]["reviewer_model"], "haiku")
                self.assertEqual(len(got["config_warnings"]), 1)

    def test_flow_mappings_in_config(self):
        p = Project(self)
        p.mode("turnGate:\n  rewrites: {routine: 0, consequential: 2}\n  models: {reviewer: sonnet}\n")
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["max_rewrites"], 0)
        self.assertEqual(got["route"]["reviewer_model"], "sonnet")
        p = Project(self)
        p.mode("mode: rp\nturnGate: {review: every, models: {reviewer: opus}}\n")
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["policy"], "every")
        self.assertEqual(got["route"]["reviewer_model"], "opus")

    def test_unreadable_frontmatter_is_reported(self):
        p = Project(self)
        p.session(body="no frontmatter here\n")
        tf = p.turn_file(turn(f"Liss nods.\n\n{ANCHOR}"))
        code, out, _err = run("check", "--project-dir", str(p.root), "--session-file",
                              "Work/rp/rp_session_2026-10-17.md", "--turn-file", str(tf))
        self.assertEqual(code, 0)
        self.assertIn("frontmatter", json.loads(out)["scene_warning"])

    def test_absolute_character_glob_does_not_crash(self):
        p = Project(self)
        (p.root / "Memory").mkdir()
        (p.root / "Memory/canon-layout.md").write_text(
            f"| `character` | `{p.root}/Lore/World/Characters/*.md` | cast |\n")
        got = self.check(p, turn(f'Vey: "Hang it."\n\n{ANCHOR}'))
        self.assertIn("lint:presence", got["route"]["reasons"])

    def test_priced_stake_is_consequential(self):
        p = Project(self)
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}", source="priced_stake_touched: true"))
        self.assertEqual(got["route"]["next"], "review")
        self.assertEqual(got["route"]["beat"], "consequential")
        self.assertEqual(got["route"]["max_rewrites"], 3)
        self.assertIn("priced_stake", got["triggers"])

    def test_priced_stake_regex_on_keeper_input(self):
        p = Project(self)
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}", keeper="What price must I pay to leave the house?"))
        self.assertIn("priced_stake", got["triggers"])

    def test_canon_proposed_and_pc_change_are_consequential(self):
        p = Project(self)
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}", source="priced_stake_touched: false\ncanon_proposed: true"))
        self.assertIn("canon_proposed", got["triggers"])
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}", source="pc_changed: true"))
        self.assertIn("pc_change", got["triggers"])

    def test_pc_role_change_in_delta_is_a_pc_change(self):
        p = Project(self)
        delta = 'scene.participants:\n  - name: "Alder"\n    role: "PC (antlers grown)"\n  - name: "Liss"\n  - name: "Mira"'
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}", delta=delta))
        self.assertIn("pc_change", got["triggers"])

    def test_arrival_or_departure_is_consequential(self):
        p = Project(self)
        draft = "Mira goes in.\n\nScene: Day 4 | Location: the yard | Present: Liss"
        got = self.check(p, turn(draft, delta='scene.participants: ["Alder", "Liss"]'))
        self.assertIn("arrival_departure", got["triggers"])
        self.assertEqual(got["route"]["beat"], "consequential")

    def test_fight_or_capture_is_consequential(self):
        p = Project(self)
        got = self.check(p, turn(f"Liss seizes him by the collar and ties him to the post.\n\n{ANCHOR}"))
        self.assertIn("fight_or_capture", got["triggers"])

    def test_validate_tag_forces_review(self):
        p = Project(self)
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}", keeper="I wait. [validate]"))
        self.assertEqual(got["route"]["next"], "review")
        self.assertIn("validate_tag", got["triggers"])

    def test_policy_every_restores_review_on_every_beat(self):
        p = Project(self)
        p.mode("turnGate:\n  review: every\n")
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["next"], "review")

    def test_policy_lint_only_never_spends_a_model_call(self):
        p = Project(self)
        p.mode("turnGate:\n  review: lint-only\n")
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}", source="priced_stake_touched: true"))
        self.assertEqual(got["route"]["next"], "ship")
        self.assertEqual([line["path"] for line in p.log_lines()], ["lint", "skipped"])

    def test_models_and_rewrites_are_configurable(self):
        p = Project(self)
        p.mode(
            "mode: rp\nmodels:\n  reviewer: sonnet   # the draft-loop's own reviewers\n"
            "turnGate:\n  rewrites:\n    routine: 0\n    consequential: 2\n"
            "  models:\n    gm: haiku\n    reviewer: sonnet\n    adjudicator: opus\n"
            "  consequentialPatterns: ['\\bPOW\\s+\\d+']\n"
            "extensions:\n  perTurnGate: continuity-reviewer\n"
        )
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["max_rewrites"], 0)
        self.assertEqual(got["route"]["gm_model"], "haiku")
        self.assertEqual(got["route"]["reviewer_model"], "sonnet")
        self.assertEqual(got["route"]["adjudicator_model"], "opus")
        got = self.check(p, turn(f"He feels it go. POW 47/60.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["max_rewrites"], 2)
        self.assertIn("pattern", got["triggers"])

    def test_old_config_keeps_working_with_defaults(self):
        p = Project(self)
        p.mode(
            "mode: rp\ndefaultRunMode: gate          # solo = cheap\nmodels:\n  reviewer: sonnet\n  draft: null\n"
            "slots:\n  bible: ${vault}/Lore/World/Characters/{Velathra,Liss}.md\n"
            "extensions:\n  perTurnGate: continuity-reviewer\n  ratification: [rp-ratifier, canon-writer]\n"
        )
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["reviewer_model"], "haiku")
        self.assertEqual(got["route"]["policy"], "consequential")
        self.assertEqual(got["config_warnings"], [])

    def test_bad_config_values_fall_back_with_a_warning(self):
        p = Project(self)
        p.mode("turnGate:\n  review: sometimes\n  models:\n    reviewer: gpt\n  rewrites:\n    routine: 9\n")
        got = self.check(p, turn(f"Liss nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["policy"], "consequential")
        self.assertEqual(got["route"]["reviewer_model"], "haiku")
        self.assertEqual(got["route"]["max_rewrites"], 1)
        self.assertEqual(len(got["config_warnings"]), 3)

    def test_cast_from_config_extends_the_presence_check(self):
        p = Project(self)
        p.mode("turnGate:\n  cast: [Pell]\n")
        got = self.check(p, turn(f'Pell: "Coin."\n\n{ANCHOR}'))
        self.assertIn("lint:presence", got["route"]["reasons"])


class LogTests(unittest.TestCase):
    def test_every_gate_path_appends_one_line(self):
        p = Project(self)
        p.session()
        session = "Work/rp/rp_session_2026-10-17.md"
        tf = p.turn_file(turn(f'Vey: "Hang it."\n\n{ANCHOR}'))
        run("check", "--project-dir", str(p.root), "--session-file", session, "--turn-file", str(tf), "--beat", "B12")
        for args in (
            ("--path", "review", "--model", "haiku", "--verdict", "violations_found", "--violations", "presence"),
            ("--path", "adjudicate", "--model", "sonnet", "--verdict", "violations_found", "--violations", "presence"),
            ("--path", "turn", "--verdict", "clean", "--attempts", "2"),
        ):
            code, _out, err = run("log", "--project-dir", str(p.root), "--session-file", session, "--beat", "B12", *args)
            self.assertEqual(code, 0, err)
        lines = p.log_lines()
        self.assertEqual([line["path"] for line in lines], ["lint", "review", "adjudicate", "turn"])
        self.assertTrue(all(line["session"] == session and line["beat"] == "B12" for line in lines))
        self.assertEqual(lines[0]["findings"], ["presence"])
        self.assertEqual(lines[1]["model"], "haiku")
        self.assertEqual(lines[1]["violation_categories"], ["presence"])
        self.assertIn("elapsed_ms", lines[0])

    def test_log_rejects_an_unknown_path(self):
        p = Project(self)
        code, _out, err = run("log", "--project-dir", str(p.root), "--path", "maybe")
        self.assertEqual(code, 2)

    def test_no_log_flag_writes_nothing(self):
        p = Project(self)
        p.session()
        tf = p.turn_file(turn(f"Liss nods.\n\n{ANCHOR}"))
        run("check", "--project-dir", str(p.root), "--session-file", "Work/rp/rp_session_2026-10-17.md",
            "--turn-file", str(tf), "--no-log")
        self.assertEqual(p.log_lines(), [])


class CommandContractTests(unittest.TestCase):
    """The commands must use the tool, not the clock, to name the session file."""

    def test_turn_and_end_resolve_the_session_through_the_tool(self):
        for name in ("turn.md", "end.md"):
            with self.subTest(command=name):
                text = (REPO / "plugins/rp/commands" / name).read_text()
                self.assertNotIn("rp_session_$(date", text)
                self.assertIn("rp_gate.py\" session", text)

    def test_start_records_the_session_in_the_marker(self):
        text = (REPO / "plugins/rp/commands/start.md").read_text()
        self.assertIn("session_file:", text)
        self.assertNotIn("touch Work/markers/rp-active", text)

    def test_turn_runs_the_lint_and_logs_each_path(self):
        text = (REPO / "plugins/rp/commands/turn.md").read_text()
        self.assertIn("rp_gate.py\" check", text)
        for path in ("review", "adjudicate", "turn"):
            self.assertIn(f"--path {path}", text)

    def test_end_closes_the_session_file(self):
        text = (REPO / "plugins/rp/commands/end.md").read_text()
        self.assertIn("status: closed", text)

    def test_turn_resolves_asha_root_in_every_gate_command(self):
        text = (REPO / "plugins/rp/commands/turn.md").read_text()
        for line in text.splitlines():
            if 'python3 "$ASHA_ROOT/plugins/rp/tools/rp_gate.py"' in line:
                self.assertIn('ASHA_ROOT="${ASHA_ROOT:-', line)
        self.assertIn("treated as a rewrite and always reviewed", text)

    def test_reviewer_has_the_new_categories(self):
        text = (REPO / "plugins/write/agents/continuity-reviewer.md").read_text()
        self.assertIn("### 11. pc_authored", text)
        self.assertIn("### 12. presence", text)
        self.assertIn("ADJUDICATE", text)


# ------------------------------------------------- live state, not invariants

LIVE_FRONTMATTER = """---
campaign: "Test"
currentDate: "Day 195 (Sunday)"
currentLocation: "the yard"
currentTime: "15:00"
participants: ["Alder", "Liss", "Mira"]
{extra}scene:
  time: "15:00"
  location: "the yard"
  participants:
    - name: "Alder"
      role: "PC (calm)"
    - name: "Liss"
      role: "NPC"
    - name: "Mira"
      role: "NPC"
---
"""

HEADER = """
# RP Session 2026-10-24: Alder, Day 195 afternoon onward

Continues the 10-17 log (closed at B4099). Summary: `Work/rp/summaries/x.md`. State: `Work/rp/alder/alder.md`
§ Run state ("Closed" line). Beat numbering resumes at 4100.

**Standing since D174:** the Warden is dead. No board is held.

## What is on him

**Stats:** POW 48/60; HP 20/20.
"""

DAY_PLAN = """
## Day Plan: Day 195 (Sunday)

| NPC | Goal | Move | Clock |
|---|---|---|---|

### Beat 4100: Day 195, ~15:00 (Sunday)

Liss sets down the drawknife.
"""

RUN_STATE_FILE = """---
character: "Alder"
status: "paused at Beat 83, Day 31 (2026-09-25)"
---

# Alder

## Sheet

STR 80.

## Run state (one page — the GM updates this, nothing else)

**Closed: Day 195, ~15:00 (Sunday), after Beat 4099 (closed 2026-10-24 with /rp:end; log `rp_session_2026-10-17.md`):** the yard at the dray shafts; due again 2026-12-01.

**Earlier: Day 190, ~19:47 (Tuesday), after Beat 3899 (paused 2026-10-21):** the larder after supper.

**Vitals:** HP 9/15 · POW 48/60

---

### Briefing

Old notes.

## Later section

Not run state.
"""

STATIC_INVARIANTS = """---
type: invariants
last_extraction: {extracted}
keeper_locked_sections:
  - "Character Registers → Warden (arc CLOSED status)"
conflicts_flagged: 1
---

# RP Invariants

> **This projection is the test campaign.** Current state is not kept here: read the live session.

## Tone Anchors

- No softening. Do not skip in-world time.

## Character Registers

### Warden

- **Arc status (CLOSED)** `[KEEPER-LOCKED]`: the pilgrimage is complete.

### Alder (PC)

- **Run state block** at the foot of `Work/rp/alder/alder.md` is the only PC state the GM updates.
- Fed once (D32) is history; the calendar ruling says Day 36 = Tuesday.

## Mechanical Rules

- **Resistance**: 50 + caster's POW - target's current POW (current POW from the run state).

## World and Timeline Facts

- The tavern sits on neutral ground.

## Conflicts and Gaps

1. ~~Locked Timeline State stale~~ — retired 2026-10-25; live state is read from the session.

```yaml
# an example inside a fence is not the projection's own state
Timeline State: example
```
"""

LIVE_LINES = {
    "snapshot": "> **This projection is the run begun 2026-09-24, now at Day 60.**",
    "label": "- **Run state (Day 60, Friday, ~18:45, Beat 342 closed; resumes Beat 343)**: his own frame.",
    "clocks": "- **Live clocks**: treads 23-29 to fit.",
    "heading": "## Timeline State `[KEEPER-LOCKED]`",
    "vessel": "- **Current vessel (session-local)**: Alder, Day 60.",
    "resumes": "The run resumes at Beat 343 at the gate.",
}


class LiveStateProject(Project):
    def live_session(self, name="rp_session_2026-10-24.md", extra="", header=HEADER, plan=DAY_PLAN):
        return self.session(name, LIVE_FRONTMATTER.format(extra=extra) + header + plan)

    def run_state(self, path="Work/rp/alder/alder.md", body=RUN_STATE_FILE):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return target

    def invariants(self, body):
        path = self.root / "Memory/invariants.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path


def line_of(path, text):
    return path.read_text(encoding="utf-8").splitlines().index(text) + 1


class LiveStateTests(unittest.TestCase):
    """Current state comes from the session and the run's state file, resolved without names."""

    def test_run_state_named_by_the_session_header(self):
        p = LiveStateProject(self)
        session = p.live_session()
        target = p.run_state()
        got = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        rs = got["run_state"]
        self.assertEqual(rs["path"], "Work/rp/alder/alder.md")
        self.assertEqual(rs["source"], "header")
        self.assertEqual(rs["section"], "Run state")
        self.assertEqual(rs["heading_line"], line_of(target, "## Run state (one page — the GM updates this, nothing else)"))
        self.assertEqual(rs["end_line"], line_of(target, "## Later section") - 1)
        current = rs["current"]
        self.assertEqual(current["start"], current["end"])  # the first entry only, not the history below it
        self.assertTrue(target.read_text().splitlines()[current["start"] - 1].startswith("**Closed: Day 195"))
        self.assertEqual((current["day"], current["beat"], current["time"]), (195, 4099, "15:00"))
        self.assertIn("2026-10-24", current["dates"])
        self.assertEqual(got["warnings"], [])
        self.assertEqual(got["session_file"], "Work/rp/rp_session_2026-10-24.md")
        self.assertTrue(session.exists())

    def test_run_state_named_by_the_session_frontmatter(self):
        p = LiveStateProject(self)
        p.live_session(extra='run_state: "Work/rp/other/pc.md#Run state"\n', header="\n# RP Session\n")
        p.run_state("Work/rp/other/pc.md")
        rs = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")["run_state"]
        self.assertEqual((rs["path"], rs["source"], rs["section"]), ("Work/rp/other/pc.md", "frontmatter", "Run state"))

    def test_frontmatter_outranks_the_header_and_takes_a_section_sign(self):
        p = LiveStateProject(self)
        p.live_session(extra='run_state: "Work/rp/other/pc.md § Run state"\n')
        p.run_state("Work/rp/other/pc.md")
        p.run_state()
        rs = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")["run_state"]
        self.assertEqual((rs["path"], rs["source"]), ("Work/rp/other/pc.md", "frontmatter"))

    def test_header_and_frontmatter_ranges(self):
        p = LiveStateProject(self)
        session = p.live_session()
        got = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        lines = session.read_text().splitlines()
        closing = lines.index("---", 1) + 1
        self.assertEqual(got["frontmatter"], {"start": 1, "end": closing})
        self.assertEqual(got["header"]["start"], got["frontmatter"]["end"] + 1)
        self.assertEqual(got["header"]["end"], line_of(session, "## Day Plan: Day 195 (Sunday)") - 1)
        self.assertFalse(got["header"]["truncated"])
        self.assertEqual(got["as_of"], {"day": 195, "time": "15:00"})

    def test_header_without_a_day_plan_stops_at_the_first_beat(self):
        p = LiveStateProject(self)
        session = p.live_session(plan="\n### Beat 12: Day 4, ~15:00\n\nProse.\n")
        got = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        self.assertEqual(got["header"]["end"], line_of(session, "### Beat 12: Day 4, ~15:00") - 1)

    def test_long_header_is_capped(self):
        p = LiveStateProject(self)
        p.live_session(header=HEADER + "line\n" * 600)
        header = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")["header"]
        self.assertTrue(header["truncated"])
        self.assertLessEqual(header["end"] - header["start"] + 1, rp_gate.HEADER_MAX_LINES)

    def test_session_resolution_follows_the_marker(self):
        p = LiveStateProject(self)
        p.live_session("rp_session_2026-10-24.md")
        p.live_session("rp_session_2026-10-30.md", extra='run_state: "Work/rp/other/pc.md"\n')
        p.run_state()
        p.run_state("Work/rp/other/pc.md")
        p.marker("session_file: Work/rp/rp_session_2026-10-24.md\n")
        code, out, _err = run("session", "--project-dir", str(p.root))
        self.assertEqual(code, 0)
        live = json.loads(out)["live_state"]
        self.assertEqual(live["session_file"], "Work/rp/rp_session_2026-10-24.md")
        self.assertEqual(live["run_state"]["path"], "Work/rp/alder/alder.md")

    def test_malformed_frontmatter_pointer_warns_instead_of_falling_back(self):
        p = LiveStateProject(self)
        p.live_session(extra="run_state:\n  - Work/rp/alder/alder.md\n")
        p.run_state()
        got = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        self.assertIsNone(got["run_state"])
        self.assertTrue(any("run_state" in w for w in got["warnings"]))

    def test_no_run_state_pointer_warns(self):
        p = LiveStateProject(self)
        p.live_session(header="\n# RP Session\n")
        got = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        self.assertIsNone(got["run_state"])
        self.assertTrue(any("run_state" in w for w in got["warnings"]))

    def test_run_state_cannot_point_outside_the_project(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        outside = Path(elsewhere.name) / "outside.md"
        outside.write_text(RUN_STATE_FILE, encoding="utf-8")
        for pointer in (str(outside), f"../{Path(elsewhere.name).name}/outside.md"):
            with self.subTest(pointer=pointer):
                p = LiveStateProject(self)
                p.live_session(extra=f'run_state: "{pointer}"\n')
                got = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
                self.assertIsNone(got["run_state"])
                self.assertTrue(any("outside" in w for w in got["warnings"]))

    def test_run_state_file_without_the_section_warns(self):
        p = LiveStateProject(self)
        p.live_session()
        p.run_state(body="# Alder\n\n## Sheet\n\nSTR 80.\n")
        got = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        self.assertIsNone(got["run_state"])
        self.assertTrue(any("Run state" in w for w in got["warnings"]))

    def test_state_date_ignores_future_dates_and_floors_at_the_session_start(self):
        p = LiveStateProject(self)
        p.live_session()
        p.run_state()
        got = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        self.assertEqual((got["state_date"], got["state_date_source"]), ("2026-10-24", "run_state"))
        p2 = LiveStateProject(self)
        p2.live_session("rp_session_2026-10-24.md", header="\n# RP Session\n")
        got = rp_gate.live_state(p2.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        self.assertEqual((got["state_date"], got["state_date_source"]), ("2026-10-24", "session"))

    def test_the_tool_names_no_campaign(self):
        # The state files are resolved from the session, never from a run's name.
        source = SCRIPT.read_text(encoding="utf-8").lower()
        for name in ("garret", "gilt thorn"):
            self.assertNotIn(name, source)


class InvariantsTests(unittest.TestCase):
    """The gate warns when invariants.md carries live state or predates the run's state."""

    def project(self, invariants, run_state=True):
        p = LiveStateProject(self)
        p.live_session()
        if run_state:
            p.run_state()
        p.invariants(invariants)
        return p

    def report(self, p):
        state = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        return rp_gate.invariants_report(p.root, state)

    def kinds(self, report):
        return sorted(w["kind"] for w in report["warnings"])

    def test_static_canon_raises_nothing(self):
        p = self.project(STATIC_INVARIANTS.format(extracted="2026-10-25"))
        self.assertEqual(self.report(p)["warnings"], [])

    def test_each_live_state_shape_is_flagged(self):
        for name, line in LIVE_LINES.items():
            with self.subTest(shape=name):
                body = STATIC_INVARIANTS.format(extracted="2026-10-25").replace(
                    "## Tone Anchors\n", f"## Tone Anchors\n\n{line}\n")
                p = self.project(body)
                path = p.root / "Memory/invariants.md"
                report = self.report(p)
                self.assertEqual(self.kinds(report), ["live_state"])
                self.assertIn(line_of(path, line), report["warnings"][0]["lines"])

    def test_a_locked_live_section_named_in_the_frontmatter_is_flagged(self):
        body = STATIC_INVARIANTS.format(extracted="2026-10-25").replace(
            '  - "Character Registers', '  - "Timeline State"\n  - "Character Registers')
        self.assertEqual(self.kinds(self.report(self.project(body))), ["live_state"])

    def test_extraction_older_than_the_run_state_is_stale(self):
        report = self.report(self.project(STATIC_INVARIANTS.format(extracted="2026-09-27")))
        self.assertEqual(self.kinds(report), ["stale"])
        detail = report["warnings"][0]["detail"]
        self.assertIn("2026-09-27", detail)
        self.assertIn("2026-10-24", detail)
        self.assertEqual(report["state_date"], "2026-10-24")

    def test_extraction_on_the_state_date_is_fresh(self):
        self.assertEqual(self.report(self.project(STATIC_INVARIANTS.format(extracted="2026-10-24")))["warnings"], [])

    def test_the_session_start_dates_the_state_when_no_run_state_resolves(self):
        p = LiveStateProject(self)
        p.live_session(header="\n# RP Session\n")
        p.invariants(STATIC_INVARIANTS.format(extracted="2026-09-27"))
        report = self.report(p)
        self.assertEqual(self.kinds(report), ["stale"])
        self.assertEqual(report["state_date_source"], "session")

    def test_missing_extraction_date_warns(self):
        body = STATIC_INVARIANTS.format(extracted="2026-10-25").replace("last_extraction: 2026-10-25\n", "")
        self.assertEqual(self.kinds(self.report(self.project(body))), ["no_extraction_date"])

    def test_no_invariants_file_reports_nothing(self):
        p = LiveStateProject(self)
        p.live_session()
        self.assertIsNone(self.report(p))

    def test_the_old_shape_raises_both_warnings(self):
        body = STATIC_INVARIANTS.format(extracted="2026-09-27") + "\n" + "\n".join(LIVE_LINES.values()) + "\n"
        report = self.report(self.project(body))
        self.assertEqual(self.kinds(report), ["live_state", "stale"])
        self.assertEqual(len(report["warnings"][0]["lines"]), len(LIVE_LINES))

    def test_session_command_reports_and_warns_on_stderr(self):
        p = self.project(STATIC_INVARIANTS.format(extracted="2026-09-27") + "\n" + LIVE_LINES["heading"] + "\n")
        p.marker("session_file: Work/rp/rp_session_2026-10-24.md\n")
        code, out, err = run("session", "--project-dir", str(p.root))
        self.assertEqual(code, 0)
        self.assertEqual(sorted(w["kind"] for w in json.loads(out)["invariants"]["warnings"]), ["live_state", "stale"])
        self.assertIn("live state", err)
        self.assertIn("predates", err)

    def test_check_reports_and_logs_the_warnings(self):
        p = self.project(STATIC_INVARIANTS.format(extracted="2026-09-27") + "\n" + LIVE_LINES["heading"] + "\n")
        tf = p.turn_file(turn(f'Liss sets down the drawknife.\n\n{ANCHOR}'))
        code, out, err = run("check", "--project-dir", str(p.root), "--session-file",
                             "Work/rp/rp_session_2026-10-24.md", "--turn-file", str(tf))
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(w["kind"] for w in json.loads(out)["invariants"]["warnings"]), ["live_state", "stale"])
        lint = [line for line in p.log_lines() if line["path"] == "lint"]
        self.assertEqual(lint[0]["invariants_warnings"], ["live_state", "stale"])

    def test_check_without_invariants_is_unchanged(self):
        p = LiveStateProject(self)
        p.live_session()
        tf = p.turn_file(turn(f'Liss sets down the drawknife.\n\n{ANCHOR}'))
        code, out, err = run("check", "--project-dir", str(p.root), "--session-file",
                             "Work/rp/rp_session_2026-10-24.md", "--turn-file", str(tf))
        self.assertEqual(code, 0, err)
        self.assertIsNone(json.loads(out)["invariants"])
        self.assertNotIn("invariants_warnings", p.log_lines()[0])

    def test_invariants_command_strict_exit(self):
        static = self.project(STATIC_INVARIANTS.format(extracted="2026-10-25"))
        code, out, _err = run("invariants", "--project-dir", str(static.root), "--strict")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["warnings"], [])
        live = self.project(STATIC_INVARIANTS.format(extracted="2026-10-25") + "\n" + LIVE_LINES["clocks"] + "\n")
        code, out, _err = run("invariants", "--project-dir", str(live.root), "--strict")
        self.assertEqual(code, 1)
        self.assertEqual([w["kind"] for w in json.loads(out)["warnings"]], ["live_state"])
        code, _out, _err = run("invariants", "--project-dir", str(live.root))
        self.assertEqual(code, 0)

    def test_report_is_cheap(self):
        p = self.project(STATIC_INVARIANTS.format(extracted="2026-10-25") * 40)
        started = time.perf_counter()
        for _ in range(20):
            self.report(p)
        self.assertLess((time.perf_counter() - started) / 20, 0.05)


class LiveStateReviewTests(unittest.TestCase):
    """Cases from the cold review of the first pass: each one failed or was untested there."""

    def report_for(self, extra, extracted="2026-10-25", body=None):
        p = LiveStateProject(self)
        p.live_session()
        p.run_state()
        text = body if body is not None else STATIC_INVARIANTS.format(extracted=extracted).replace(
            "## Tone Anchors\n", f"## Tone Anchors\n\n{extra}\n")
        p.invariants(text)
        state = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        return p, rp_gate.invariants_report(p.root, state)

    def live_lines(self, report):
        return [line for w in report["warnings"] if w["kind"] == "live_state" for line in w["lines"]]

    def test_static_protocol_and_chronology_are_not_live_state(self):
        for line in (
            "## Scene State Protocol",
            "- **Scene state**: every beat closes with a SCENE_STATE_DELTA.",
            "- **Current state:** read from the session, never from here.",
            "- **Live state** comes from the session header.",
            "- **Witness arc**: CLOSED; Beat 3217 closed it.",
            "The cycle resumes at Day 1 after each reset.",
            "~~~\n## Timeline State\n~~~",
            "One line ```Timeline State``` of code.",
        ):
            with self.subTest(line=line):
                _p, report = self.report_for(line)
                self.assertEqual(self.live_lines(report), [])

    def test_more_live_state_shapes_are_flagged(self):
        for line in (
            "- Run state: Day 60, Beat 342.",
            "## Current State",
            "## Live State",
            "## 3. Timeline State",
            "- **Run state (Day 60)** `[KEEPER-LOCKED]`: his own frame.",
            "- **Live clocks** (session-local): treads 23-29.",
            "Play is now at D195, B4099 closed; resumes B4100.",
        ):
            with self.subTest(line=line):
                p, report = self.report_for(line)
                first = line.splitlines()[0]
                self.assertIn(line_of(p.root / "Memory/invariants.md", first), self.live_lines(report))

    def test_an_unclosed_fence_does_not_hide_the_rest(self):
        static = STATIC_INVARIANTS.format(extracted="2026-10-25").split("```yaml")[0]  # no later fence to close it
        body = static.replace("## Tone Anchors\n", "## Tone Anchors\n\n```\nan example that never closes\n\n## Timeline State\n")
        p, report = self.report_for("", body=body)
        self.assertIn(line_of(p.root / "Memory/invariants.md", "## Timeline State"), self.live_lines(report))

    def test_a_locked_name_path_ending_in_a_live_section_is_flagged(self):
        body = STATIC_INVARIANTS.format(extracted="2026-10-25").replace(
            '  - "Character Registers', '  - "World and Timeline Facts → Timeline State"\n  - "Character Registers')
        _p, report = self.report_for("", body=body)
        self.assertTrue(self.live_lines(report))

    def test_hits_inside_locked_content_are_marked_locked(self):
        body = STATIC_INVARIANTS.format(extracted="2026-10-25") + (
            "\n## Timeline State `[KEEPER-LOCKED]`\n\n### Active\n\n- **Current vessel (session-local)**: Alder, Day 60.\n"
            "\n## Protocol Requirements\n\n- **Live clocks**: treads 23-29.\n"
            "- **Run state (Day 60)** `[KEEPER-LOCKED]`: his own frame.\n")
        p, report = self.report_for("", body=body)
        path = p.root / "Memory/invariants.md"
        hits = {h["line"]: h["locked"] for w in report["warnings"] if w["kind"] == "live_state" for h in w["hits"]}
        self.assertTrue(hits[line_of(path, "## Timeline State `[KEEPER-LOCKED]`")])
        self.assertTrue(hits[line_of(path, "- **Current vessel (session-local)**: Alder, Day 60.")])
        self.assertFalse(hits[line_of(path, "- **Live clocks**: treads 23-29.")])
        self.assertTrue(hits[line_of(path, "- **Run state (Day 60)** `[KEEPER-LOCKED]`: his own frame.")])
        live = next(w for w in report["warnings"] if w["kind"] == "live_state")
        self.assertTrue(live["locked"])

    def test_a_locked_frontmatter_name_is_a_locked_hit(self):
        body = STATIC_INVARIANTS.format(extracted="2026-10-25").replace(
            '  - "Character Registers', '  - "Timeline State"\n  - "Character Registers')
        _p, report = self.report_for("", body=body)
        live = next(w for w in report["warnings"] if w["kind"] == "live_state")
        self.assertTrue(all(h["locked"] for h in live["hits"]))

    def test_a_later_session_start_or_pause_alone_is_not_newer_state(self):
        # Static canon goes stale when the run's state moves on, not when a session opens.
        p = LiveStateProject(self)
        p.live_session("rp_session_2026-10-24.md")
        p.run_state(body=RUN_STATE_FILE.replace("closed 2026-10-24", "closed 2026-10-20").replace(
            "due again 2026-12-01", "noted"))
        p.invariants(STATIC_INVARIANTS.format(extracted="2026-10-21"))
        state = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        self.assertEqual((state["state_date"], state["state_date_source"]), ("2026-10-20", "run_state"))
        self.assertEqual(rp_gate.invariants_report(p.root, state)["warnings"], [])

    def test_a_datetime_counts_as_its_date(self):
        p = LiveStateProject(self)
        p.live_session()
        p.run_state(body=RUN_STATE_FILE.replace("closed 2026-10-24 with", "closed 2026-10-24T10:00 with"))
        state = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        dates = state["run_state"]["current"]["dates"]
        self.assertIn("2026-10-24", dates)
        self.assertNotIn("2026-10-17", dates)  # a log's file name is not a state date

    def test_header_pointer_skips_other_kinds_of_state(self):
        p = LiveStateProject(self)
        header = ("\n# RP Session\n\nScene state: `Work/rp/scenes/yard.md` (map). "
                  "State: `Work/rp/alder/alder.md` § Run state.\n")
        p.live_session(header=header)
        p.run_state()
        rs = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")["run_state"]
        self.assertEqual(rs["path"], "Work/rp/alder/alder.md")

    def test_a_pointer_at_the_invariants_is_refused(self):
        p = LiveStateProject(self)
        p.live_session(extra='run_state: "Memory/invariants.md#Timeline State"\n')
        p.invariants(STATIC_INVARIANTS.format(extracted="2026-10-25") + "\n## Timeline State\n\nDay 60.\n")
        got = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        self.assertIsNone(got["run_state"])
        self.assertTrue(any("invariants" in w for w in got["warnings"]))

    def test_section_prefers_the_exact_heading(self):
        p = LiveStateProject(self)
        p.live_session()
        target = p.run_state(body="# Alder\n\n## Run state history\n\nOld.\n\n## Run state\n\n**Closed: Day 9.**\n")
        rs = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")["run_state"]
        self.assertEqual(rs["heading_line"], line_of(target, "## Run state"))

    def test_abbreviated_beat_headings_end_the_header(self):
        p = LiveStateProject(self)
        session = p.live_session(plan="\n### B4100 — Day 195, ~15:00\n\nProse.\n")
        header = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")["header"]
        self.assertEqual(header["end"], line_of(session, "### B4100 — Day 195, ~15:00") - 1)

    def test_empty_header_and_empty_current_entry_are_null(self):
        p = LiveStateProject(self)
        p.live_session(extra='run_state: "Work/rp/alder/alder.md"\n', header="", plan="## Day Plan: Day 195\n")
        p.run_state(body="# Alder\n\n## Run state\n\n### Workings\n\n- smooth skin\n")
        got = rp_gate.live_state(p.root, "Work/rp/rp_session_2026-10-24.md", today="2026-10-25")
        self.assertIsNone(got["header"])
        self.assertIsNone(got["run_state"]["current"])
        self.assertIsNotNone(got["frontmatter"])


class LiveStateContractTests(unittest.TestCase):
    """The commands and agents read live state from the session, never from the invariants."""

    def read(self, rel):
        return (REPO / rel).read_text(encoding="utf-8")

    def test_extract_invariants_writes_static_canon_only(self):
        text = self.read("plugins/rp/commands/extract-invariants.md")
        self.assertIn("static canon", text.lower())
        step3 = text.split("### Step 3", 1)[1].split("\n### ", 1)[0]
        sections = re.findall(r"(?m)^- \*\*(.+?)\*\*", step3)
        self.assertTrue(sections)
        for section in sections:
            self.assertNotRegex(section.lower(), r"timeline state|run state|live|current")
        self.assertIn("Timeline State", step3)  # named as excluded
        self.assertIn('rp_gate.py" invariants', text)
        self.assertIn("byte-for-byte", text)  # static locked sections are still kept verbatim
        self.assertIn("Bash", text.split("---", 2)[1])  # the verification step runs the tool
        # Locked live state halts before anything is written, by the tool, not by judgement,
        # and the after-write check never edits locked text.
        step2 = text.split("### Step 2", 1)[1].split("\n### ", 1)[0]
        self.assertIn('rp_gate.py" invariants', step2)
        self.assertIn("locked: true", step2)
        step5 = text.split("### Step 5", 1)[1].split("\n### ", 1)[0]
        self.assertRegex(step5, r"(?i)never edit locked")

    def test_turn_passes_live_state_to_the_gm_and_the_reviewer(self):
        text = self.read("plugins/rp/commands/turn.md")
        gm = text.split('subagent_type: "roleplay-gm"', 1)[1].split("Receive DRAFT", 1)[0]
        reviewer = text.split('subagent_type: "continuity-reviewer"', 1)[1].split("Receive VERDICT", 1)[0]
        self.assertIn("LIVE_STATE:", gm)
        self.assertIn("LIVE_STATE:", reviewer)
        self.assertIn("live_state", text)
        self.assertIn("invariants.warnings", text)
        self.assertIn("`header` is null", text)  # the ranges may be absent
        self.assertIn("`current` is null", text)

    def test_the_canon_register_has_a_run_state_slot(self):
        self.assertIn("| `run_state` |", self.read("plugins/rp/templates/canon-layout.md"))

    def test_reviewer_reads_current_state_from_live_state(self):
        text = self.read("plugins/write/agents/continuity-reviewer.md")
        self.assertIn("LIVE_STATE:", text)
        self.assertRegex(text, r"(?s)LIVE_STATE.{0,400}never from (?:the )?(?:INVARIANTS_FILE|invariants)")

    def test_gm_reads_current_state_from_live_state(self):
        self.assertIn("LIVE_STATE", self.read("plugins/rp/agents/roleplay-gm.md"))

    def test_start_records_the_run_state_in_the_frontmatter(self):
        self.assertIn("run_state:", self.read("plugins/rp/commands/start.md"))

    def test_ratifier_keeps_run_state_out_of_the_invariants(self):
        self.assertRegex(self.read("plugins/rp/agents/rp-ratifier.md"), r"(?i)static canon only")


if __name__ == "__main__":
    unittest.main()
