"""Tests for plugins/rp/tools/rp_gate.py: the per-beat lint, routing and logging."""
import importlib.util
import io
import json
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
participants: ["Redacted123", "Redacted648", "Redacted695"]
scene:
  time: "15:00"
  location: "the yard"
  participants:
    - name: "Redacted123"
      role: "PC (calm)"
    - name: "Redacted648"
      role: "NPC"
      profile_file: "Lore/World/Characters/Redacted648.md"
    - name: "Redacted695"
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
        f"GM_SPAWN_LOG:\n- redacted648: NOT spawned (routine)\n\n"
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
        for name in ("Redacted648", "Redacted695", "Redacted1264", "Redacted1259"):
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


ANCHOR = "Scene: Day 4, ~15:00, calm | Location: the yard | Present: Redacted648, Redacted695"
LATER = "Scene: Day 4, ~15:05, calm | Location: the yard | Present: Redacted648, Redacted695"


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
        draft = f'Redacted648 sets down the drawknife. "Shed."\nRedacted695 watches from the step.\n\n{ANCHOR}'
        result = self.lint(turn(draft))
        self.assertEqual(result["findings"], [])

    def test_missing_anchor(self):
        result = self.lint(turn('Redacted648 sets down the drawknife. "Shed."'))
        self.assertEqual(self.checks(result), ["anchor_missing"])
        self.assertEqual(result["findings"][0]["fix"], "edit")

    def test_duplicated_anchor(self):
        result = self.lint(turn(f"{ANCHOR}\nRedacted648 nods.\n{ANCHOR}"))
        self.assertIn("anchor_duplicated", self.checks(result))

    def test_anchor_must_close_the_draft(self):
        result = self.lint(turn(f"Redacted648 nods.\n{ANCHOR}\nRedacted695 looks up."))
        self.assertIn("anchor_not_last", self.checks(result))

    def test_absent_character_speaking_is_a_presence_miss(self):
        # The Redacted1264 case: a named character proposes while not in the scene.
        draft = f'Redacted648 holds the beam. Redacted1264, from nowhere: "Can you still hang a door?"\n\n{ANCHOR}'
        result = self.lint(turn(draft))
        self.assertEqual(self.checks(result), ["presence"])
        finding = result["findings"][0]
        self.assertEqual(finding["name"], "Redacted1264")
        self.assertEqual(finding["fix"], "review")

    def test_absent_character_acting_is_a_presence_miss(self):
        result = self.lint(turn(f"Redacted1264 takes the hammer from the bench.\n\n{ANCHOR}"))
        self.assertEqual(self.checks(result), ["presence"])

    def test_all_caps_log_style_speaker_is_recognised(self):
        result = self.lint(turn(f'REDACTED1264: "The door."\n\n{ANCHOR}'))
        self.assertEqual(self.checks(result), ["presence"])

    def test_word_sent_by_carrier_passes(self):
        draft = f'Redacted695 comes back from the gate. Redacted1264 sends word: the hinges are ready.\n\n{ANCHOR}'
        result = self.lint(turn(draft))
        self.assertEqual(result["findings"], [])
        self.assertIn("carried", {e["kind"] for e in result["events"]})

    def test_arrival_on_the_page_passes(self):
        draft = f'Redacted1264 comes in from the yard and sets down the hinges. "For the door."\n\n{ANCHOR}, Redacted1264'
        result = self.lint(turn(draft, delta='scene.participants: ["Redacted123", "Redacted648", "Redacted695", "Redacted1264"]'))
        self.assertEqual(result["findings"], [])
        self.assertIn("arrival", {e["kind"] for e in result["events"]})

    def test_coming_to_the_door_is_an_arrival(self):
        draft = f'Redacted1264 came to the stair door with a candle: "I heard you." Then went back up.\n\n{ANCHOR}'
        result = self.lint(turn(draft))
        self.assertNotIn("presence", self.checks(result))
        self.assertIn("arrival", {e["kind"] for e in result["events"]})

    def test_arrival_without_listing_passes_presence(self):
        draft = f'Redacted1264 steps in through the door, nods once, and goes out again.\n\n{ANCHOR}'
        result = self.lint(turn(draft))
        self.assertNotIn("presence", self.checks(result))

    def test_mentions_inside_dialogue_and_possessive_objects_pass(self):
        draft = (
            'Redacted648 taps the roll under his arm. "Redacted1259 drew these. Redacted1264 wants them back."\n'
            "Redacted1259's plans are still under his arm.\n\n" + ANCHOR
        )
        result = self.lint(turn(draft))
        self.assertEqual(result["findings"], [])

    def test_absent_character_noted_as_away_passes(self):
        draft = f"Redacted648 nods toward the road. Redacted221 is away at the market.\n\n{ANCHOR}"
        project = Project(self)
        (project.root / "Lore/World/Characters/Redacted221.md").write_text("# Redacted221\n")
        result = self.lint(turn(draft), project=project)
        self.assertEqual(result["findings"], [])

    def test_alias_of_a_present_full_name(self):
        body = FRONTMATTER.replace('    - name: "Redacted695"', '    - name: "Redacted561"\n      role: "NPC"\n    - name: "Redacted695"')
        draft = 'MARR: "Props tonight."\n\nScene: Day 4 | Location: the vault | Present: Redacted648, Redacted695, Marr'
        result = self.lint(turn(draft), session_body=body)
        self.assertEqual(result["findings"], [])

    def test_shared_surnames_are_different_people(self):
        p = Project(self)
        p.mode("turnGate:\n  cast: [Redacted804]\n")
        body = FRONTMATTER.replace('    - name: "Redacted695"', '    - name: "Redacted518 Brand"\n      role: "NPC"\n    - name: "Redacted695"')
        draft = 'Redacted804 nods to her: "Hes."\n\nScene: Day 4 | Location: the yard | Present: Redacted648, Redacted695, Redacted518 Brand'
        result = self.lint(turn(draft), project=p, session_body=body)
        self.assertEqual(self.checks(result), ["presence"])
        self.assertEqual(result["findings"][0]["name"], "Redacted804")

    def test_first_name_of_a_listed_newcomer(self):
        body = FRONTMATTER.replace('    - name: "Redacted695"', '    - name: "Redacted518 Brand"\n      role: "NPC"\n    - name: "Redacted695"')
        draft = ('Redacted648 and Odo cross from the barn. ODO stops at the door: "Hes."\n\n'
                 'Scene: Day 4 | Location: the yard | Present: Redacted648, Redacted695, Redacted518 Brand, Redacted804 (the door)')
        result = self.lint(turn(draft, delta='scene.participants: [Redacted123, Redacted648, Redacted695, Redacted518 Brand, Redacted804]'),
                           session_body=body)
        self.assertEqual(result["findings"], [])

    def test_semicolon_continues_the_present_list(self):
        draft = 'Redacted648, from the hearth: "That\'s what the pots are for."\n\nScene: Day 4 | Location: the yard | Present: Redacted695; Redacted648 by the hearth'
        result = self.lint(turn(draft))
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["present"], ["Redacted695", "Redacted648 by the hearth"])

    def test_common_word_character_names_at_sentence_start(self):
        p = Project(self)
        (p.root / "Lore/World/Characters/Frost.md").write_text("# Frost\n")
        draft = f"The pulse loud through the floor; frost came up in rings. Frost runs along the passage wall.\n\n{ANCHOR}"
        result = self.lint(turn(draft), project=p)
        self.assertEqual(result["findings"], [])

    def test_titled_names_match_only_in_full(self):
        p = Project(self)
        (p.root / "Lore/World/Characters/Redacted1143.md").write_text("# Redacted1143\n")
        result = self.lint(turn(f"Frost runs along the passage wall. The Keeper waits.\n\n{ANCHOR}"), project=p)
        self.assertEqual(result["findings"], [])
        result = self.lint(turn(f"Redacted1143 lifts a hand.\n\n{ANCHOR}"), project=p)
        self.assertEqual(self.checks(result), ["presence"])

    # --- review findings (cold review, 2026-10-24) ---

    def test_carrier_word_after_a_quote_does_not_clear_the_speaker(self):
        for draft in ('Redacted1264: "Hang it." Redacted695 writes the figure on the slate.',
                      '"Hang it," Redacted1264 says, and sends the boy for nails.',
                      'Redacted1264: "Can you still hang a door?" Redacted648 sends him a look.'):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"))
                self.assertEqual(self.checks(result), ["presence"])

    def test_plural_subject_and_dash_attribution(self):
        for draft in ("Redacted648 and Redacted1264 lift the beam.", "Redacted1264 and Redacted648 lift the beam.", 'Redacted1264 — "Hang it."'):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"))
                self.assertEqual(self.checks(result), ["presence"])

    def test_irregular_past_in_a_past_tense_draft(self):
        result = self.lint(turn(f"Redacted648 stood at the bench. Redacted1264 knelt by the door and spat.\n\n{ANCHOR}"))
        self.assertEqual(self.checks(result), ["presence"])

    def test_mentions_that_are_not_actions_pass(self):
        for draft in ("Redacted648 takes the hammer. She says the Redacted1264 brothers sold it to her.",
                      "Redacted648 knows Redacted1264 hates the cold.", "Redacted648 says Redacted1264 must be at the mill by now.",
                      "Redacted648 nods toward the road. Redacted1264 will be back by noon.", "Redacted1264's letter sits on the bench.",
                      "She glances at the stool where Redacted1264 used to sit.", "Redacted123 asks whether Redacted1264 is coming back.",
                      "~08:00 REDACTED1264'S YARD: the gate stands open.", "Redacted1264 is away at the mill.", "Redacted1264 has gone to the mill.",
                      "Redacted648 reads Redacted1264's letter twice.", "A letter from Redacted1264 lies on the bench.", "Redacted648 thinks of Redacted1264.",
                      "Without Redacted1264 the yard is quiet.", "Redacted648 talks about Redacted1264 all morning."):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"))
                self.assertEqual(result["findings"], [])

    def test_absent_character_on_the_page_is_flagged(self):
        """The core case. A false positive costs a cheap review; a miss ships unreviewed."""
        drafts = [
            # past-tense drafts, with plural nouns that look present
            "Redacted648 stood at the bench. Redacted1264 knelt by the door and spat. The shutters banged. The others waited.",
            "Redacted1264 took the hammer. The nails rattled in the tin.",
            "The shutters banged. Redacted1264 laughed.",
            # base-form plurals in present tense
            "Redacted695 watches. Redacted648 and Redacted1264 set the beam on the trestles.",
            "Redacted695 watches. Redacted648 and Redacted1264 lay the beam down.",
            "Redacted695 watches. Redacted648 and Redacted1264 put the beam down.",
            "He and Redacted1264 lift the beam.", "The boy and Redacted1264 lift the beam.",
            # modals and contractions
            "Redacted1264 will not budge.", "Redacted1264 could only stare at him.", "Redacted1264 would not look at him.",
            "Redacted1264 must decide now.", "Redacted1264 can still hang a door, and does.", "Redacted1264 doesn't look up from the bench.",
            # speech tags in every quoting style
            '"Hang it", said Redacted1264.', '"Hang it", says Redacted1264.', "'Hang it,' said Redacted1264.", "‘Hang it,’ said Redacted1264.",
            '*"Hang it,"* said Redacted1264.', '**"Hang it,"** says Redacted1264.', '**Redacted1264:** "Hang it."', 'REDACTED1264 (low): "Hang it."',
            '"Hang it" — Redacted1264, from the bench.', 'Redacted1264 — quiet — "Hang it."',
            # words before the name that are not prepositions
            "With that Redacted1264 takes the hammer.", "At that Redacted1264 laughs.", "Behind her Redacted1264 laughs.",
            "He sees that Redacted1264 has the hammer already.",
            # carriers that carry nothing, or carry and then act
            "Redacted1264 sends word: the hinges are ready. Then Redacted1264 takes the hammer from the bench.",
            'Redacted1264 writes a note. "Hang it," says Redacted1264.', "Redacted1264 sends him a look across the bench.",
            # ordinary phrases that are not arrivals
            "Redacted1264 looks up from the bench.", "Redacted1264 takes the hammer out of the box.",
            "Redacted1264 takes it down from the shelf.", "Redacted1264 returns the hammer to the bench.",
            "Redacted1264 follows his gaze.", "Redacted1264 comes to a decision.",
            # script-style speech, and negation that is not absence
            "Redacted1264: *Hang it.*", "Redacted1264 is not happy about it.",
            # ordinary prose with no recognisable verb right after the name (third review)
            "Redacted1264 just shrugs from the woodpile.", "Redacted1264 still has the hammer.", "Redacted1264 never looks up.",
            "Redacted1264 then turns to Redacted695.", "Redacted1264 cannot quite hide the grin.",
            "Redacted1264 — sweat running into his eyes — braces it.", "Redacted1264 (from the bench, not looking up) grunts.",
            "Redacted1264's eyes narrow.", "Redacted1264's already at the bench.", "Redacted1264'll take it.",
            "Redacted648 looks to Redacted1264, who only nods.", "Before Redacted1264 can answer, Redacted648 turns.",
            "Redacted1264, Redacted648 and Redacted695 lift the beam.", "Redacted648 and Redacted1264 together lift the beam.",
            '"Not yet," mutters Redacted1264.', 'Then said Redacted1264, "Hang it."', "*Hang it,* said Redacted1264.",
            "Redacted648 asks Redacted1264 for the hammer.", "Redacted1264 and Redacted648 always argue about the hammer.",
            "Redacted648 waits until Redacted1264 takes the hammer.", "Since Redacted1264 takes the far end, Redacted648 lifts.",
            # fourth review: excuse words across punctuation
            "Lost in thought, Redacted1264 turns another page of the ledger.", "As Redacted648 feared, Redacted1264 is already at the table.",
            "Whatever Redacted648 thought, Redacted1264 took the hammer.", "Nothing to worry about, Redacted1264 said, and took the hammer.",
            "It was as Redacted648 had guessed, Redacted1264 was there before her.",
            '"Hang it," Redacted648 said, Redacted1264 already reaching for the hammer.', "Whatever she said, Redacted1264 just shrugged.",
            # absence phrases that are not absence
            "Redacted1264 had left the gate open behind him and now leans on it.", "Redacted1264 was still out of breath when he took the hammer.",
            "Redacted1264 is asleep on the bench by the hearth, mouth open.", "Redacted1264 is out in the yard, splitting wood.",
            "Redacted1264 is off the cart before it stops.", "Redacted1264 has gone to the bench and taken the hammer.",
            "Redacted1264 is dead on the flags at Redacted648's feet.",
            # speaker labels after an excuse word
            'A shout from Redacted1264: "Shut that door!"',
            # possessives and prepositions that are physical
            "Redacted1264's grin is crooked as he leans on the doorframe.", "Redacted1264's smile doesn't reach his eyes.",
            "Redacted1264's laughter rings across the yard.", "Redacted1264's tail flicks once.", "Redacted1264's knuckles whiten on the table.",
            "Redacted1264's left hand closes on the hammer.", "Redacted1264's taken the far end of the beam.", "Redacted1264's quiet for a long moment.",
            "Redacted1264's over by the bench.", "Redacted648 takes the cup from Redacted1264 and drinks.", "Redacted648 plants herself in front of Redacted1264.",
            "Redacted648 pulls the hammer out of Redacted1264's hands.", "A low whistle from Redacted1264, somewhere behind the woodpile.",
            # carried phrases that carry nothing
            "Redacted1264 sends the runner off with a clap on the shoulder.", "From the corner table, Redacted1264 sends for another jug.",
            "Redacted1264 sends for the hammer, reaching across Redacted648 to take it.", "Redacted1264 sends Redacted648 a note across the table, grinning.",
            "Redacted1264 sent for Redacted648, but he was already at the bench when she came.",
            # underscore emphasis
            "_Redacted1264 takes the hammer._", "Redacted648 nods. _Redacted1264_ grins.",
            # excuse words used physically, or as nouns
            "Redacted648 wraps her arms about Redacted1264.", "Redacted648 is taller than Redacted1264, who ducks under the beam.",
            "Redacted648 moves like Redacted1264 does, slow and heavy.", "Deep in thought Redacted1264 stares at the fire.",
            "Without a second thought Redacted1264 takes the hammer.", "Redacted1264 used to the weight, lifts the beam alone.",
            "Redacted1264 is missing a tooth when he grins.", "Redacted1264 sends word with a look, and Redacted648 nods.",
            "Redacted1264 sends the boy for nails and takes the hammer himself.", "Redacted1264 sends the boy flying with a kick.",
            # stray or mixed quote marks, and an anchor sharing the line
            '“Hang it," said Redacted1264, and took the hammer.', 'The beam is 12" thick. Redacted1264 takes the hammer.',
        ]
        for draft in drafts:
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"))
                self.assertIn("presence", self.checks(result))

    def test_anchor_sharing_a_line_with_prose(self):
        result = self.lint(turn("Redacted1264 takes the hammer. " + ANCHOR))
        self.assertIn("presence", self.checks(result))

    def test_title_only_and_configured_common_word_names(self):
        p = Project(self)
        (p.root / "Lore/World/Characters/Captain.md").write_text("# Captain\n")
        p.mode("turnGate:\n  cast: [Will]\n")
        for draft in ("Captain takes the hammer.", 'CAPTAIN: "Hang it."', "Will takes the hammer. Redacted648 will not look at him."):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"), project=p)
                self.assertIn("presence", self.checks(result))

    def test_name_shapes(self):
        p = Project(self)
        for stem in ("McAllister", "Anne-Marie", "O'Brien", "redacted561", "Bo", "Redacted1143", "Captain", "Warden",
                     "The Principal", "Sergeant"):
            (p.root / "Lore/World/Characters" / f"{stem}.md").write_text("x\n")
        for draft in ("McAllister takes the hammer.", "Anne-Marie takes the hammer.", "O'Brien takes the hammer.",
                      "Redacted561 takes the hammer.", "Bo takes the hammer.", "Redacted648 turns to the Redacted463, who nods.",
                      "The Captain takes the hammer.", 'THE WARDEN: "Hang it."', "The Principal takes the hammer.",
                      "Redacted648 turns to the Warden, who nods.", "Redacted648 salutes the Warden.", "The Sergeant takes the hammer."):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"), project=p)
                self.assertIn("presence", self.checks(result))

    def test_returning_is_an_arrival(self):
        for draft in ("Redacted1264 is back from the mill.", "Redacted1264 is back. He takes the hammer from Redacted648 without a word.",
                      "As Redacted648 feared, Redacted1264 comes in through the gate.", "Redacted1264 has left the mill and walks into the yard.",
                      "Redacted1264 had gone to the well and now comes in through the gate with the buckets.",
                      "Redacted1264 sends word ahead and comes in through the gate behind it.",
                      "Redacted1264 sent word, then arrived on the heels of it."):
            with self.subTest(draft=draft):
                result = self.lint(turn(f"{draft}\n\n{ANCHOR}"))
                self.assertNotIn("presence", self.checks(result))
                self.assertIn("arrival", {e["kind"] for e in result["events"]})

    def test_common_word_name_after_a_quote_still_counts(self):
        p = Project(self)
        (p.root / "Lore/World/Characters/Redacted148.md").write_text("# Redacted148\n")
        result = self.lint(turn(f'The grate is grey with ash. Redacted648 nods. "Hang it." Redacted148 takes the hammer.\n\n{ANCHOR}'),
                           project=p)
        self.assertEqual(self.checks(result), ["presence"])

    def test_pronouns_are_never_names(self):
        for word in ("His", "Her", "Their", "She", "They", "This"):
            self.assertEqual(rp_gate.name_tokens(word), set())
        self.assertEqual(rp_gate.name_tokens("His Majesty"), {"majesty"})

    def test_more_title_abbreviations_in_present(self):
        result = self.lint(turn("Redacted648 nods.\n\nScene: Day 4 | Location: the yard | Present: Redacted648, Capt. Holt, J. Redacted901, Redacted695"))
        self.assertEqual(result["present"], ["Redacted648", "Capt. Holt", "J. Redacted901", "Redacted695"])
        result = self.lint(turn("Redacted648 nods.\n\nScene: Day 4 | Location: the yard | Present: Redacted648, Redacted695 — etc."))
        self.assertEqual(result["present"], ["Redacted648", "Redacted695"])

    def test_non_list_participants_do_not_crash(self):
        result = self.lint(turn(f"Redacted648 nods.\n\n{ANCHOR}", delta="scene.participants: 2"))
        self.assertIn("delta_malformed", self.checks(result))
        body = FRONTMATTER.replace('participants: ["Redacted123", "Redacted648", "Redacted695"]', "participants: true")
        body = body.replace("  participants:\n", "  people:\n")
        self.lint(turn(f"Redacted648 nods.\n\n{ANCHOR}"), session_body=body)

    def test_heard_absent_character_is_on_the_page(self):
        result = self.lint(turn(f"He hears Redacted648 and Redacted1264 below in the kitchen, voices raised.\n\n{ANCHOR}"))
        self.assertEqual(self.checks(result), ["presence"])

    def test_attribution_after_a_question_still_speaks(self):
        result = self.lint(turn(f'"Can you hang a door?" asks Redacted1264.\n\n{ANCHOR}'))
        self.assertEqual(self.checks(result), ["presence"])

    def test_titled_unnamed_and_abbreviated_people(self):
        body = FRONTMATTER.replace('    - name: "Redacted695"',
                                   '    - name: "Mother"\n      role: "NPC"\n    - name: "Mr. Holt"\n'
                                   '      role: "NPC"\n    - name: "two regulars"\n      role: "NPC"\n    - name: "Redacted695"')
        draft = "Redacted648 nods.\n\nScene: Day 4 | Location: the yard | Present: Mr. Holt, Mother, Redacted648, two regulars, Redacted695"
        result = self.lint(turn(draft), session_body=body)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["present"], ["Mr. Holt", "Mother", "Redacted648", "two regulars", "Redacted695"])

    def test_present_none(self):
        body = FRONTMATTER.replace('    - name: "Redacted648"\n      role: "NPC"\n      profile_file: "Lore/World/Characters/Redacted648.md"\n', "")
        body = body.replace('    - name: "Redacted695"\n      role: "NPC"\n', "")
        result = self.lint(turn("He waits.\n\nScene: Day 4 | Location: the yard | Present: none (the house within)"),
                           session_body=body)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["present"], [])

    def test_frontmatter_with_a_byte_order_mark(self):
        result = self.lint(turn(f"Redacted648 nods.\n\n{ANCHOR}"), session_body="\ufeff" + FRONTMATTER)
        self.assertEqual(result["findings"], [])

    def test_unknown_delta_key(self):
        draft = f"Redacted648 nods.\n\n{ANCHOR}"
        result = self.lint(turn(draft, delta='scene.time: "15:00"\nscene.weather: "rain"'))
        self.assertEqual(self.checks(result), ["delta_unknown_key"])
        self.assertIn("scene.weather", result["findings"][0]["detail"])

    def test_root_mirror_delta_key(self):
        draft = f"Redacted648 nods.\n\n{ANCHOR}"
        result = self.lint(turn(draft, delta='scene.time: "15:00"\ncurrentTime: "15:05"'))
        self.assertEqual(self.checks(result), ["delta_root_mirror"])

    def test_nested_delta_and_fenced_block(self):
        draft = f"Redacted648 nods.\n\n{ANCHOR}"
        delta = '```yaml\nscene:\n  time: "15:00"\n  mood: "tense"\n```'
        result = self.lint(turn(draft, delta=delta))
        self.assertEqual(result["findings"], [])
        self.assertEqual(sorted(result["delta"]), ["scene.mood", "scene.time"])

    def test_extra_delta_keys_from_config(self):
        p = Project(self)
        p.mode("turnGate:\n  deltaKeys: [scene.weather]\n")
        draft = f"Redacted648 nods.\n\n{ANCHOR}"
        result = self.lint(turn(draft, delta='scene.weather: "rain"'), project=p)
        self.assertEqual(result["findings"], [])

    def test_present_change_without_participants_delta(self):
        draft = "Redacted695 goes in through the back door.\n\nScene: Day 4 | Location: the yard | Present: Redacted648"
        result = self.lint(turn(draft, delta='scene.time: "15:00"'))
        self.assertEqual(self.checks(result), ["delta_participants_missing"])

    def test_delta_participants_disagree_with_anchor(self):
        draft = "Redacted695 goes in through the back door.\n\nScene: Day 4 | Location: the yard | Present: Redacted648"
        result = self.lint(turn(draft, delta='scene.participants: ["Redacted123", "Redacted648", "Redacted695"]'))
        self.assertEqual(self.checks(result), ["delta_participants_mismatch"])

    def test_anchor_time_without_time_delta(self):
        result = self.lint(turn(f"Redacted648 nods.\n\n{LATER}", delta='scene.mood: "tense"'))
        self.assertEqual(self.checks(result), ["delta_time_missing"])
        self.assertEqual(result["findings"][0]["fix"], "edit")

    def test_anchor_time_agrees_with_delta(self):
        result = self.lint(turn(f"Redacted648 nods.\n\n{LATER}", delta='scene.time: "15:05"'))
        self.assertEqual(result["findings"], [])

    def test_anchor_time_disagrees_with_delta(self):
        result = self.lint(turn(f"Redacted648 nods.\n\n{LATER}", delta='scene.time: "16:00"'))
        self.assertEqual(self.checks(result), ["delta_time_mismatch"])

    def test_lint_is_fast(self):
        p = Project(self)
        session = p.session()
        scene = rp_gate.load_scene(p.root, session)
        config = rp_gate.load_config(p.root)
        parsed = rp_gate.parse_turn(turn(("Redacted648 nods. Redacted695 looks up. " * 200) + "\n\n" + ANCHOR))
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
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["next"], "ship")
        self.assertEqual(got["route"]["beat"], "routine")
        self.assertEqual(got["route"]["max_rewrites"], 1)
        self.assertEqual([line["path"] for line in p.log_lines()], ["lint", "skipped"])

    def test_lint_flag_routes_to_the_haiku_pass(self):
        p = Project(self)
        got = self.check(p, turn(f'Redacted1264: "Hang it."\n\n{ANCHOR}'))
        self.assertEqual(got["route"]["next"], "review")
        self.assertEqual(got["route"]["beat"], "routine")
        self.assertEqual(got["route"]["reviewer_model"], "haiku")
        self.assertEqual(got["route"]["adjudicator_model"], "sonnet")
        self.assertIn("lint:presence", got["route"]["reasons"])
        self.assertEqual([line["path"] for line in p.log_lines()], ["lint"])

    def test_edit_fixable_findings_ask_for_a_fix_then_review_on_recheck(self):
        p = Project(self)
        got = self.check(p, turn("Redacted648 nods."))
        self.assertEqual(got["route"]["next"], "fix")
        got = self.check(p, turn("Redacted648 nods."), "--recheck")
        self.assertEqual(got["route"]["next"], "review")

    def test_final_relint_after_review_never_asks_for_review_again(self):
        p = Project(self)
        got = self.check(p, turn(f'Redacted1264: "Hang it."\n\n{ANCHOR}', source="priced_stake_touched: true"), "--final")
        self.assertEqual(got["route"]["next"], "ship")
        got = self.check(p, turn('Redacted1264: "Hang it."'), "--final")
        self.assertEqual(got["route"]["next"], "fix")
        self.assertEqual([line["path"] for line in p.log_lines()], ["lint", "lint"])
        self.assertTrue(all(line["final"] for line in p.log_lines()))

    def test_everyone_leaving_is_a_departure(self):
        p = Project(self)
        draft = "Redacted648 and Redacted695 go out through the back door and are gone.\n\nScene: Day 4 | Location: the yard | Present: none (Redacted123 alone)"
        got = self.check(p, turn(draft, delta="scene.participants: [Redacted123]"))
        self.assertIn("arrival_departure", got["triggers"])
        self.assertEqual(got["route"]["next"], "review")

    def test_any_later_attempt_is_a_rewrite(self):
        p = Project(self)
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}"), "--attempt", "2", "--recheck")
        self.assertEqual(got["route"]["next"], "review")

    def test_a_rewrite_is_always_reviewed(self):
        p = Project(self)
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}"), "--attempt", "2", "--rewrite")
        self.assertEqual(got["route"]["next"], "review")
        self.assertEqual([line["path"] for line in p.log_lines()], ["lint"])
        got = self.check(p, turn("Redacted648 nods."), "--rewrite")
        self.assertEqual(got["route"]["next"], "fix")

    def test_validate_tag_reviews_even_under_lint_only(self):
        p = Project(self)
        p.mode("turnGate:\n  review: lint-only\n")
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}", keeper="I wait. [validate]"))
        self.assertEqual(got["route"]["next"], "review")

    def test_quoted_booleans_still_declare(self):
        p = Project(self)
        for source in ('priced_stake_touched: "true"', "priced_stake_touched: 'true'", '"priced_stake_touched": true'):
            with self.subTest(source=source):
                got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}", source=source))
                self.assertIn("priced_stake", got["triggers"])

    def test_fight_pattern_false_friends(self):
        p = Project(self)
        got = self.check(p, turn(f"Redacted648 fights a smile. Her wounded pride shows. She hits her stride.\n\n{ANCHOR}"))
        self.assertNotIn("fight_or_capture", got["triggers"])

    def test_non_mapping_config_values_warn_instead_of_crashing(self):
        for block in ("  models: sonnet\n", "  models: [haiku]\n", "  rewrites: 2\n",
                      "  models:\n    - reviewer: sonnet\n"):
            with self.subTest(block=block):
                p = Project(self)
                p.mode("turnGate:\n" + block)
                got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}"))
                self.assertEqual(got["route"]["reviewer_model"], "haiku")
                self.assertEqual(len(got["config_warnings"]), 1)

    def test_flow_mappings_in_config(self):
        p = Project(self)
        p.mode("turnGate:\n  rewrites: {routine: 0, consequential: 2}\n  models: {reviewer: sonnet}\n")
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["max_rewrites"], 0)
        self.assertEqual(got["route"]["reviewer_model"], "sonnet")
        p = Project(self)
        p.mode("mode: rp\nturnGate: {review: every, models: {reviewer: opus}}\n")
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["policy"], "every")
        self.assertEqual(got["route"]["reviewer_model"], "opus")

    def test_unreadable_frontmatter_is_reported(self):
        p = Project(self)
        p.session(body="no frontmatter here\n")
        tf = p.turn_file(turn(f"Redacted648 nods.\n\n{ANCHOR}"))
        code, out, _err = run("check", "--project-dir", str(p.root), "--session-file",
                              "Work/rp/rp_session_2026-10-17.md", "--turn-file", str(tf))
        self.assertEqual(code, 0)
        self.assertIn("frontmatter", json.loads(out)["scene_warning"])

    def test_absolute_character_glob_does_not_crash(self):
        p = Project(self)
        (p.root / "Memory").mkdir()
        (p.root / "Memory/canon-layout.md").write_text(
            f"| `character` | `{p.root}/Lore/World/Characters/*.md` | cast |\n")
        got = self.check(p, turn(f'Redacted1264: "Hang it."\n\n{ANCHOR}'))
        self.assertIn("lint:presence", got["route"]["reasons"])

    def test_priced_stake_is_consequential(self):
        p = Project(self)
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}", source="priced_stake_touched: true"))
        self.assertEqual(got["route"]["next"], "review")
        self.assertEqual(got["route"]["beat"], "consequential")
        self.assertEqual(got["route"]["max_rewrites"], 3)
        self.assertIn("priced_stake", got["triggers"])

    def test_priced_stake_regex_on_keeper_input(self):
        p = Project(self)
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}", keeper="What price must I pay to leave the house?"))
        self.assertIn("priced_stake", got["triggers"])

    def test_canon_proposed_and_pc_change_are_consequential(self):
        p = Project(self)
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}", source="priced_stake_touched: false\ncanon_proposed: true"))
        self.assertIn("canon_proposed", got["triggers"])
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}", source="pc_changed: true"))
        self.assertIn("pc_change", got["triggers"])

    def test_pc_role_change_in_delta_is_a_pc_change(self):
        p = Project(self)
        delta = 'scene.participants:\n  - name: "Redacted123"\n    role: "PC (antlers grown)"\n  - name: "Redacted648"\n  - name: "Redacted695"'
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}", delta=delta))
        self.assertIn("pc_change", got["triggers"])

    def test_arrival_or_departure_is_consequential(self):
        p = Project(self)
        draft = "Redacted695 goes in.\n\nScene: Day 4 | Location: the yard | Present: Redacted648"
        got = self.check(p, turn(draft, delta='scene.participants: ["Redacted123", "Redacted648"]'))
        self.assertIn("arrival_departure", got["triggers"])
        self.assertEqual(got["route"]["beat"], "consequential")

    def test_fight_or_capture_is_consequential(self):
        p = Project(self)
        got = self.check(p, turn(f"Redacted648 seizes him by the collar and ties him to the post.\n\n{ANCHOR}"))
        self.assertIn("fight_or_capture", got["triggers"])

    def test_validate_tag_forces_review(self):
        p = Project(self)
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}", keeper="I wait. [validate]"))
        self.assertEqual(got["route"]["next"], "review")
        self.assertIn("validate_tag", got["triggers"])

    def test_policy_every_restores_review_on_every_beat(self):
        p = Project(self)
        p.mode("turnGate:\n  review: every\n")
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["next"], "review")

    def test_policy_lint_only_never_spends_a_model_call(self):
        p = Project(self)
        p.mode("turnGate:\n  review: lint-only\n")
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}", source="priced_stake_touched: true"))
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
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}"))
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
            "slots:\n  bible: ${vault}/Lore/World/Characters/{Redacted1259,Redacted648}.md\n"
            "extensions:\n  perTurnGate: continuity-reviewer\n  ratification: [rp-ratifier, canon-writer]\n"
        )
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["reviewer_model"], "haiku")
        self.assertEqual(got["route"]["policy"], "consequential")
        self.assertEqual(got["config_warnings"], [])

    def test_bad_config_values_fall_back_with_a_warning(self):
        p = Project(self)
        p.mode("turnGate:\n  review: sometimes\n  models:\n    reviewer: gpt\n  rewrites:\n    routine: 9\n")
        got = self.check(p, turn(f"Redacted648 nods.\n\n{ANCHOR}"))
        self.assertEqual(got["route"]["policy"], "consequential")
        self.assertEqual(got["route"]["reviewer_model"], "haiku")
        self.assertEqual(got["route"]["max_rewrites"], 1)
        self.assertEqual(len(got["config_warnings"]), 3)

    def test_cast_from_config_extends_the_presence_check(self):
        p = Project(self)
        p.mode("turnGate:\n  cast: [Redacted833]\n")
        got = self.check(p, turn(f'Redacted833: "Coin."\n\n{ANCHOR}'))
        self.assertIn("lint:presence", got["route"]["reasons"])


class LogTests(unittest.TestCase):
    def test_every_gate_path_appends_one_line(self):
        p = Project(self)
        p.session()
        session = "Work/rp/rp_session_2026-10-17.md"
        tf = p.turn_file(turn(f'Redacted1264: "Hang it."\n\n{ANCHOR}'))
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
        tf = p.turn_file(turn(f"Redacted648 nods.\n\n{ANCHOR}"))
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


if __name__ == "__main__":
    unittest.main()
