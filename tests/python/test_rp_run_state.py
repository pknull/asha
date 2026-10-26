"""Tests for schema'd run state, the ship step, the state block and per-day summaries (rp_gate.py)."""
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "plugins/rp/tools/rp_gate.py"
spec = importlib.util.spec_from_file_location("rp_gate_run_state", SCRIPT)
assert spec is not None and spec.loader is not None
rp_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp_gate)

LEGACY = "Work/rp/rp_session_2026-10-24.md"
LOG_DIR = "Work/rp/test/log"
STATE_FILE = "Work/rp/test/alder.md"
DAY_197 = f"{LOG_DIR}/day-0197.md"
DAY_198 = f"{LOG_DIR}/day-0198.md"
SUMMARY_DIR = "Work/rp/summaries/test"

RUN_BLOCK = """run:
  stats:
    POW: "54/60"
    HP: "20/20"
    Luck: 25
  body:
    antlers: "stumps (B3328, B3709)"
    hooves: "feathered draught hooves, shod (T21, B3549)"
  workings:
    FIGHTS THE BIT: on
    HOMING: off
  clocks:
    vine-test: {day: 198, fires: "the vine test at the wall"}
    cure: {day: 225, fires: "THE CURE ends: the post is oak"}
"""

FRONTMATTER = """---
campaign: "Test Run"
log_dir: "Work/rp/test/log"
run_state: "Work/rp/test/alder.md#Run state"
currentDate: "Day 196 (Monday)"
startDate: "Day 195 (Sunday)"
currentLocation: "the kitchen"
currentTime: "19:00"
participants: ["Alder", "Liss"]
scene:
  time: "19:00"
  location: "the kitchen"
  participants:
    - name: "Alder"
      role: "PC (calm)"
    - name: "Liss"
      role: "NPC"
      profile_file: "Lore/World/Characters/Liss.md"
  mood: "calm"
  pending_rolls: []
{run}---
"""

HEADER = """
# RP Session 2026-10-24: Alder, Day 195 afternoon onward

**Tier:** live beats delegated. Register: plain sentences.

## What is on him

- FIGHTS THE BIT (Liss's).
"""

STATE_TEXT = """# Alder

## Run state (one page — the GM updates this, nothing else)

**Paused: Day 196, ~21:10 (Monday), after Beat 4130 (paused 2026-10-25):** the kitchen. POW 48/60.

**Earlier: Day 195, ~15:00 (Sunday), after Beat 4099 (closed 2026-10-24).**

## Notes

- Not run state.
"""


def beat(n, day, extra=""):
    return (f"### Beat {n} PRE-COMMIT (GM, binding): LISS: \"SHED.\"\n\n"
            f"### Beat {n}: Day {day}, ~08:{n % 60:02d} (Monday). (Tier 1){extra}\n"
            f"**PC move (B{n}):** \"I go.\"\n\nLiss sets down the axe.\n\n")


def legacy_body():
    parts = ["## Day Plan: Day 195 (Sunday)\n\n| NPC | Goal |\n|---|---|\n\n"]
    parts += [beat(n, 195) for n in range(4100, 4110)]
    parts.append("## Day Plan: Day 196 (Monday)\n\n### Clocks\n\n- ~05:50 first light\n\n")
    parts += [beat(n, 196) for n in range(4110, 4131)]
    return "".join(parts)


def turn_text(delta="none", anchor="Scene: Day 196, ~19:05, calm | Location: the kitchen | Present: Liss"):
    return (f"KEEPER_INPUT:\nI go.\n\nDRAFT:\nLiss sets down the axe.\n\n{anchor}\n\n"
            f"GM_SPAWN_LOG:\n- liss: NOT spawned\n\nSOURCE_LOG:\npriced_stake_touched: false\n\n"
            f"SCENE_STATE_DELTA:\n{delta}\n")


NEXT_DAY_ANCHOR = "Scene: Day 197, ~06:00, calm | Location: the kitchen | Present: Liss"
NEXT_DAY = turn_text('currentDate: "Day 197 (Tuesday)"\nscene.time: "06:00"', NEXT_DAY_ANCHOR)


class Project:
    def __init__(self, testcase, run=RUN_BLOCK, state_text=STATE_TEXT):
        self.test = testcase
        self._tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for d in ("Work/markers", "Work/rp/test", "Memory", "Lore/World/Characters"):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        (self.root / "Lore/World/Characters/Liss.md").write_text("# Liss\n")
        (self.root / STATE_FILE).write_text(state_text, encoding="utf-8")
        (self.root / LEGACY).write_text(FRONTMATTER.format(run=run) + HEADER + legacy_body(), encoding="utf-8")
        self.marker(f"session_file: {LEGACY}\nsession_opened: {LEGACY}\n")

    def marker(self, text):
        (self.root / "Work/markers/rp-active").write_text(text, encoding="utf-8")

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def frontmatter(self, rel):
        return rp_gate.read_frontmatter(self.root / rel)

    def rotate(self, text=NEXT_DAY, **kw):
        kw.setdefault("today", "2026-10-26")
        return rp_gate.rotate(self.root, rp_gate.resolve_session_file(self.root)["path"], turn_text=text, **kw)

    def ship(self, text, append: "str | None" = "### Beat 4131: Day 196, ~19:05 (Monday).\n\nLiss sets down the axe.\n",
             session=None, beat="4131", **kw):
        session = session or rp_gate.resolve_session_file(self.root)["path"]
        kw.setdefault("today", "2026-10-26")
        return rp_gate.ship(self.root, session, turn_text=text, append_text=append, beat=beat, **kw)

    def lint(self, delta, anchor="Scene: Day 196, ~19:00, calm | Location: the kitchen | Present: Liss",
             session=LEGACY):
        scene = rp_gate.load_scene(self.root, session)
        config = rp_gate.load_config(self.root)
        turn = rp_gate.parse_turn(turn_text(delta, anchor))
        return rp_gate.lint(turn, scene, config, self.root), scene, config, turn

    def log_lines(self):
        log = self.root / "Work/rp/_validator_log.jsonl"
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rp_gate.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_checks(result):
    return sorted(f["check"] for f in result["findings"] if f["check"].startswith("run_"))


class YamlTests(unittest.TestCase):
    def test_emitted_yaml_round_trips(self):
        value = {
            "stats": {"POW": "54/60", "Luck": 25},
            "body": {"antlers": 'stumps ("broken" D160) # not a comment', "tail": "horse's tail: swishes \\ lifts",
                     "café": "naïve — ünïcode", "note": "line one\nline two"},
            "workings": {"FIGHTS THE BIT": "on", "T18 (D119 tell)": "off"},
            "clocks": {"vine-test": {"day": 198, "fires": "the vine test, at the wall"}, "gone": None},
            "list": ["a", 'b "c"', 3, None, {"name": "Liss", "role": "NPC # x"}],
            "empty": [], "none": {}, "flag": True,
        }
        lines = rp_gate.emit_yaml("run", value)
        self.assertEqual(rp_gate.parse_yaml("".join(lines)), {"run": value})

    def test_an_escaped_quote_before_a_hash_does_not_start_a_comment(self):
        got = rp_gate.parse_yaml('role: "said \\"go\\" # then left"\nname: \'it\'\'s # fine\'\n')
        self.assertEqual(got, {"role": 'said "go" # then left', "name": "it's # fine"})

    def test_the_logs_unescaped_inner_quotes_still_parse(self):
        got = rp_gate.parse_yaml('role: "NPC (tipped it ("Take it, then. Bench."); gone)"\n')
        self.assertEqual(got["role"], 'NPC (tipped it ("Take it, then. Bench."); gone)')


class RunLintTests(unittest.TestCase):
    def test_a_valid_run_delta_is_clean(self):
        p = Project(self)
        delta = ('run.stats.POW: "53/60"\nrun.stats.Luck: 22\nrun.workings.HOMING: on\n'
                 'run.body.antlers: "stumps, velvet over them (B4131)"\n'
                 'run.clocks.wheels: {day: 200, fires: "Jory\'s wheels"}\nrun.clocks.vine-test: null')
        result, *_ = p.lint(delta)
        self.assertEqual(run_checks(result), [])
        self.assertNotIn("delta_unknown_key", [f["check"] for f in result["findings"]])
        self.assertEqual(result["delta"]["run.stats.POW"], "53/60")

    def test_nested_and_flat_forms_agree(self):
        p = Project(self)
        flat, *_ = p.lint('run.workings.HOMING: on\nrun.stats.POW: "53/60"')
        nested, *_ = p.lint('run:\n  workings:\n    HOMING: on\n  stats:\n    POW: "53/60"')
        self.assertEqual(flat["delta"], nested["delta"])
        self.assertEqual(run_checks(nested), [])

    def test_an_unknown_stat_is_an_edit_finding_with_the_near_name(self):
        p = Project(self)
        result, *_ = p.lint('run.stats.pow: "53/60"')
        finding = next(f for f in result["findings"] if f["check"] == "run_unknown_field")
        self.assertEqual(finding["fix"], "edit")
        self.assertIn("POW", finding["detail"])

    def test_a_stat_keeps_its_type(self):
        p = Project(self)
        for delta in ('run.stats.POW: 53', 'run.stats.POW: "fifty/60"', 'run.stats.Luck: "22/55"',
                      'run.stats.Luck: null', 'run.stats.HP: true'):
            with self.subTest(delta=delta):
                result, *_ = p.lint(delta)
                self.assertEqual(run_checks(result), ["run_bad_value"])

    def test_a_working_toggles_only_to_on_or_off(self):
        p = Project(self)
        for value in ("lifted", "true", "null", "SPENT", "[on]"):
            with self.subTest(value=value):
                result, *_ = p.lint(f"run.workings.HOMING: {value}")
                self.assertEqual(run_checks(result), ["run_bad_value"])

    def test_a_new_working_lands_on_and_a_new_one_off_is_a_misspelling(self):
        p = Project(self)
        result, *_ = p.lint("run.workings.SURE-FOOTED: on")
        self.assertEqual(run_checks(result), [])
        self.assertIn({"kind": "run_added", "key": "run.workings.SURE-FOOTED"}, result["events"])
        result, *_ = p.lint("run.workings.Homing: off")
        finding = next(f for f in result["findings"] if f["check"] == "run_unknown_field")
        self.assertIn("HOMING", finding["detail"])

    def test_a_clock_day_must_be_in_range(self):
        p = Project(self)
        cases = {
            'run.clocks.wheels: {day: 190, fires: "Jory\'s wheels"}': "run_clock_day",  # before the beat's day
            'run.clocks.wheels: {day: 900, fires: "Jory\'s wheels"}': "run_clock_day",  # past the horizon
            'run.clocks.wheels: {day: "soon", fires: "Jory\'s wheels"}': "run_bad_value",
            "run.clocks.wheels: {day: 200}": "run_bad_value",  # nothing fires
            'run.clocks.wheels: {day: 200, fires: "x", when: "noon"}': "run_bad_value",  # unknown field
            'run.clocks.wheels: "Day 200"': "run_bad_value",
            "run.clocks.wheels: null": "run_unknown_field",  # clears a clock that does not exist
        }
        for delta, check in cases.items():
            with self.subTest(delta=delta):
                result, *_ = p.lint(delta)
                self.assertEqual(run_checks(result), [check])

    def test_a_clock_day_is_held_to_the_beats_day_not_the_files(self):
        p = Project(self)
        delta = ('currentDate: "Day 197 (Tuesday)"\nscene.time: "06:00"\n'
                 'run.clocks.wheels: {day: 196, fires: "Jory\'s wheels"}')
        result, *_ = p.lint(delta, anchor=NEXT_DAY_ANCHOR)
        self.assertEqual(run_checks(result), ["run_clock_day"])

    def test_body_facts_are_text(self):
        p = Project(self)
        result, *_ = p.lint("run.body.tail: 3")
        self.assertEqual(run_checks(result), ["run_bad_value"])
        result, *_ = p.lint('run.body.tail: "a horse\'s tail (B4131)"')  # a new part may appear
        self.assertEqual(run_checks(result), [])
        result, *_ = p.lint("run.body.antlers: null")  # a part may go
        self.assertEqual(run_checks(result), [])

    def test_an_unknown_section_and_a_bad_name(self):
        p = Project(self)
        result, *_ = p.lint('run.mood.today: "grim"')
        self.assertEqual(run_checks(result), ["run_unknown_field"])
        result, *_ = p.lint('run:\n  workings:\n    "a\\tname": on')  # a quoted name is fine; a tab is not
        self.assertEqual(run_checks(result), ["run_bad_name"])

    def test_a_run_delta_without_a_schema_is_an_edit_finding(self):
        p = Project(self, run="")
        result, *_ = p.lint('run.stats.POW: "53/60"')
        self.assertEqual(run_checks(result), ["run_no_schema"])
        self.assertEqual(next(f for f in result["findings"] if f["check"] == "run_no_schema")["fix"], "edit")

    def test_run_findings_never_send_a_beat_to_review(self):
        p = Project(self)
        result, scene, config, turn = p.lint("run.workings.HOMING: lifted")
        triggers = rp_gate.beat_triggers(turn, result, scene, config)
        self.assertEqual(triggers, [])
        self.assertEqual(rp_gate.route(result, triggers, config)["next"], "fix")
        self.assertEqual(rp_gate.route(result, triggers, config, recheck=True)["next"], "ship")
        self.assertEqual(rp_gate.route(result, triggers, config, final=True)["next"], "ship")

    def test_an_overdue_clock_is_a_note_not_a_finding(self):
        p = Project(self)
        delta = 'currentDate: "Day 199 (Thursday)"\nscene.time: "06:00"'
        result, *_ = p.lint(delta, anchor="Scene: Day 199, ~06:00, calm | Location: the kitchen | Present: Liss")
        self.assertEqual(run_checks(result), [])
        self.assertEqual(result["run_notes"], [{"key": "run.clocks.vine-test", "day": 198}])  # terse: read every turn
        result, *_ = p.lint(delta + "\nrun.clocks.vine-test: null",
                            anchor="Scene: Day 199, ~06:00, calm | Location: the kitchen | Present: Liss")
        self.assertEqual(result["run_notes"], [])


class ShipTests(unittest.TestCase):
    DELTA = ('scene.time: "19:05"\nscene.mood: "tense"\nrun.stats.POW: "53/60"\nrun.workings.HOMING: on\n'
             'run.clocks.vine-test: null')

    def test_ship_writes_beat_and_state_in_one_replace(self):
        p = Project(self)
        real = os.replace
        with mock.patch.object(rp_gate.os, "replace", side_effect=real) as spy:
            got = p.ship(turn_text(self.DELTA))
        self.assertEqual(got["action"], "shipped")
        self.assertEqual(spy.call_count, 1)
        text = p.read(LEGACY)
        self.assertTrue(text.rstrip().endswith("Liss sets down the axe."))
        self.assertEqual(text.count("### Beat 4131: Day 196, ~19:05"), 1)
        data = p.frontmatter(LEGACY)
        self.assertEqual(data["scene"]["time"], "19:05")
        self.assertEqual(data["currentTime"], "19:05")  # the mirror follows scene.time
        self.assertEqual(data["scene"]["mood"], "tense")
        self.assertEqual(data["run"]["stats"]["POW"], "53/60")
        self.assertEqual(data["run"]["workings"]["HOMING"], "on")
        self.assertNotIn("vine-test", data["run"]["clocks"])
        self.assertTrue(str(data["shipped"]).startswith("4131@"))
        self.assertEqual(data["run_state"], "Work/rp/test/alder.md#Run state")  # untouched lines stay

    def test_ship_keeps_untouched_frontmatter_lines_as_written(self):
        p = Project(self)
        before = p.read(LEGACY).split("---\n")[1]
        p.ship(turn_text('scene.mood: "tense"'))
        after = p.read(LEGACY).split("---\n")[1]
        kept = [line for line in before.splitlines() if not line.startswith((" ", "scene:", "shipped:"))]
        self.assertEqual([line for line in after.splitlines() if line in kept], kept)
        self.assertIn(RUN_BLOCK.rstrip("\n"), after)  # run: untouched by a scene-only delta

    def test_crash_before_the_ship_replace_leaves_the_file_unchanged(self):
        p = Project(self)
        before = p.read(LEGACY)
        with mock.patch.object(rp_gate.os, "replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                p.ship(turn_text(self.DELTA))
        self.assertEqual(p.read(LEGACY), before)
        self.assertEqual([x for x in (p.root / "Work/rp").iterdir() if x.name.startswith(".")], [])
        self.assertEqual(p.ship(turn_text(self.DELTA))["action"], "shipped")
        self.assertEqual(p.read(LEGACY).count("### Beat 4131: Day 196"), 1)

    def test_ship_retry_is_a_no_op(self):
        p = Project(self)
        p.ship(turn_text(self.DELTA))
        once = p.read(LEGACY)
        got = p.ship(turn_text(self.DELTA))
        self.assertEqual(got["action"], "already_shipped")
        self.assertEqual(p.read(LEGACY), once)
        other = p.ship(turn_text('scene.time: "19:09"'), append="### Beat 4132: Day 196, ~19:09.\n\nMore.\n",
                       beat="4132")
        self.assertEqual(other["action"], "shipped")

    def test_ship_refuses_a_closed_file(self):
        p = Project(self)
        p.rotate(beat="4131")
        before = p.read(LEGACY)
        with self.assertRaises(ValueError):
            p.ship(turn_text(self.DELTA), session=LEGACY)
        self.assertEqual(p.read(LEGACY), before)

    def test_ship_keeps_a_concurrent_edit(self):
        p = Project(self)
        real, calls = rp_gate._ship_text, []

        def edited_meanwhile(*args, **kwargs):
            if not calls:
                with open(p.root / LEGACY, "a", encoding="utf-8") as fh:
                    fh.write("\nGM NOTE: written by hand meanwhile.\n")
            calls.append(1)
            return real(*args, **kwargs)

        with mock.patch.object(rp_gate, "_ship_text", side_effect=edited_meanwhile):
            p.ship(turn_text(self.DELTA))
        text = p.read(LEGACY)
        self.assertEqual(text.count("GM NOTE: written by hand meanwhile."), 1)
        self.assertEqual(text.count("### Beat 4131: Day 196"), 1)
        self.assertLess(text.index("GM NOTE"), text.index("### Beat 4131: Day 196"))
        self.assertEqual(len(calls), 2)

    def test_ship_drops_invalid_run_entries(self):
        p = Project(self)
        got = p.ship(turn_text('run.stats.POW: "abc"\nrun.workings.HOMING: on\ncurrentTime: "19:05"'))
        self.assertEqual(sorted(d["key"] for d in got["dropped"]), ["currentTime", "run.stats.POW"])
        data = p.frontmatter(LEGACY)
        self.assertEqual(data["run"]["stats"]["POW"], "54/60")
        self.assertEqual(data["run"]["workings"]["HOMING"], "on")
        self.assertEqual(data["currentTime"], "19:00")

    def test_ship_never_touches_the_marker(self):
        p = Project(self)
        marker = p.root / "Work/markers/rp-active"
        before = (marker.read_bytes(), marker.stat().st_mtime_ns)
        p.ship(turn_text(self.DELTA))
        self.assertEqual((marker.read_bytes(), marker.stat().st_mtime_ns), before)

    def test_ship_puts_the_day_plan_in_the_new_file(self):
        p = Project(self)
        got = p.rotate(beat="4131")
        self.assertEqual(got["path"], DAY_197)
        closed = p.read(LEGACY)
        append = ("## Day Plan: Day 197 (Tuesday)\n\n### Clocks\n\n- noon: Jory\n\n"
                  "### Beat 4131: Day 196, ~21:00 to Day 197, ~06:00 (Tuesday).\n\nMorning.\n")
        p.ship(NEXT_DAY, append=append, session=got["path"])
        self.assertEqual(p.read(LEGACY), closed)
        day = p.read(DAY_197)
        self.assertLess(day.index("## Day Plan: Day 197"), day.index("### Beat 4131"))
        self.assertEqual(p.frontmatter(DAY_197)["currentDate"], "Day 197 (Tuesday)")

    def test_ship_without_append_applies_the_delta_only(self):
        p = Project(self)
        before = p.read(LEGACY)
        got = p.ship(turn_text('scene.mood: "tense"'), append=None)
        self.assertEqual(got["action"], "shipped")
        after = p.read(LEGACY)
        self.assertEqual(after.split("\n---\n", 1)[1], before.split("\n---\n", 1)[1])  # the body is unchanged
        self.assertEqual(p.frontmatter(LEGACY)["scene"]["mood"], "tense")

    def test_a_campaign_without_a_schema_is_unchanged(self):
        p = Project(self, run="")
        got = p.ship(turn_text('scene.mood: "tense"\nrun.stats.POW: "53/60"'))
        self.assertEqual([d["key"] for d in got["dropped"]], ["run.stats.POW"])
        self.assertNotIn("run", p.frontmatter(LEGACY))
        self.assertEqual(rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")["action"], "none")
        self.assertEqual(p.read(STATE_FILE), STATE_TEXT)

    def test_ship_logs_one_line(self):
        p = Project(self)
        p.ship(turn_text(self.DELTA))
        line = p.log_lines()[-1]
        self.assertEqual((line["path"], line["beat"]), ("ship", "4131"))


class StateBlockTests(unittest.TestCase):
    def test_refresh_inserts_the_block_first_and_keeps_prose(self):
        p = Project(self)
        p.ship(turn_text('run.stats.POW: "53/60"'))
        got = rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")
        self.assertEqual(got["action"], "refreshed")
        text = p.read(STATE_FILE)
        self.assertIn("**Paused: Day 196, ~21:10 (Monday), after Beat 4130", text)
        self.assertIn("## Notes\n\n- Not run state.", text)
        self.assertLess(text.index("<!-- rp-run-state"), text.index("**Paused: Day 196"))
        block = re.search(r"```yaml\n(.*?)```", text, re.S).group(1)
        self.assertEqual(rp_gate.parse_yaml(block)["run"]["stats"]["POW"], "53/60")
        state = rp_gate.live_state(p.root, LEGACY, today="2026-10-26")
        current = state["run_state"]
        self.assertTrue(current["generated"])
        self.assertEqual((current["current"]["day"], current["current"]["beat"]), (196, 4131))
        self.assertEqual(state["state_date"], "2026-10-26")

    def test_state_block_comes_only_from_the_frontmatter(self):
        p = Project(self)
        p.write("Work/rp/.gate/turn.md", turn_text('run.stats.POW: "10/60"'))  # a draft that never shipped
        rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")
        block = re.search(r"```yaml\n(.*?)```", p.read(STATE_FILE), re.S).group(1)
        self.assertEqual(rp_gate.parse_yaml(block)["run"]["stats"]["POW"], "54/60")

    def test_refresh_is_idempotent(self):
        p = Project(self)
        rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")
        path = p.root / STATE_FILE
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")["action"], "unchanged")
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        p.ship(turn_text('run.stats.POW: "53/60"'))
        self.assertEqual(rp_gate.refresh_state(p.root, LEGACY, today="2026-10-27")["action"], "refreshed")
        self.assertEqual(p.read(STATE_FILE).count("<!-- rp-run-state"), 1)

    def test_refresh_refuses_a_hand_edited_block(self):
        p = Project(self)
        rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")
        edited = p.read(STATE_FILE).replace('POW: "54/60"', 'POW: "50/60"')
        p.write(STATE_FILE, edited)
        p.ship(turn_text('run.stats.POW: "53/60"'))
        got = rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")
        self.assertEqual(got["action"], "refused")
        self.assertIn("by hand", " ".join(got["warnings"]))
        self.assertEqual(p.read(STATE_FILE), edited)

    def test_refresh_keeps_prose_and_a_concurrent_edit(self):
        p = Project(self)
        real, calls = rp_gate._state_text, []

        def edited_meanwhile(*args, **kwargs):
            if not calls:
                with open(p.root / STATE_FILE, "a", encoding="utf-8") as fh:
                    fh.write("- Edited by hand meanwhile.\n")
            calls.append(1)
            return real(*args, **kwargs)

        with mock.patch.object(rp_gate, "_state_text", side_effect=edited_meanwhile):
            self.assertEqual(rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")["action"], "refreshed")
        text = p.read(STATE_FILE)
        self.assertEqual(text.count("- Edited by hand meanwhile."), 1)
        self.assertEqual(text.count("<!-- rp-run-state"), 1)

    def test_refresh_refuses_a_session_file_target(self):
        for target in (LEGACY, "Memory/invariants.md", "../outside.md"):
            with self.subTest(target=target):
                p = Project(self)
                p.write("Memory/invariants.md", "# Invariants\n\n## Run state\n\nold\n")
                text = p.read(LEGACY).replace("Work/rp/test/alder.md#Run state", f"{target}#Run state")
                p.write(LEGACY, text)
                before = p.read(LEGACY)
                got = rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")
                self.assertEqual(got["action"], "refused")
                self.assertEqual(p.read(LEGACY), before)
                self.assertNotIn("rp-run-state", p.read("Memory/invariants.md"))

    def test_session_warns_when_a_prose_entry_is_current(self):
        p = Project(self)
        code, out, err = run("session", "--project-dir", str(p.root))
        self.assertEqual(code, 0)
        got = json.loads(out)
        self.assertTrue(got["run"]["schema"])
        self.assertIn("hand-written", " ".join(got["run"]["warnings"]))
        rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")
        got = json.loads(run("session", "--project-dir", str(p.root))[1])
        self.assertNotIn("hand-written", " ".join(got["run"]["warnings"]))

    def test_a_stale_state_block_is_reported_then_refreshed(self):
        p = Project(self)
        rp_gate.refresh_state(p.root, LEGACY, today="2026-10-25")
        p.rotate(beat="4131")
        p.ship(NEXT_DAY, append="### Beat 4131: Day 197, ~06:00.\n\nMorning.\n", session=DAY_197)
        got = json.loads(run("session", "--project-dir", str(p.root))[1])
        self.assertTrue(got["state_block"]["stale"])
        rp_gate.refresh_state(p.root, DAY_197, today="2026-10-26")
        got = json.loads(run("session", "--project-dir", str(p.root))[1])
        self.assertFalse(got["state_block"]["stale"])

    def test_session_reports_a_malformed_standing_run_block(self):
        bad = RUN_BLOCK.replace("HOMING: off", "HOMING: lifted").replace("Luck: 25", 'Luck: "lots"')
        p = Project(self, run=bad)
        got = json.loads(run("session", "--project-dir", str(p.root))[1])
        warnings = " ".join(got["run"]["warnings"])
        self.assertIn("run.workings.HOMING", warnings)
        self.assertIn("run.stats.Luck", warnings)

    def test_session_output_stays_small_without_a_schema(self):
        p = Project(self, run="")
        got = json.loads(run("session", "--project-dir", str(p.root))[1])
        self.assertFalse(got["run"]["schema"])
        self.assertIsNone(got["state_block"])


class RotationCarriesStateTests(unittest.TestCase):
    def test_rotation_carries_run_state_and_drops_shipped(self):
        p = Project(self)
        p.ship(turn_text('run.stats.POW: "53/60"'))
        p.rotate(beat="4132")
        data = p.frontmatter(DAY_197)
        self.assertEqual(data["run"]["stats"]["POW"], "53/60")
        self.assertEqual(data["run"]["workings"], {"FIGHTS THE BIT": "on", "HOMING": "off"})
        self.assertNotIn("shipped", data)
        self.assertIn("shipped", p.frontmatter(LEGACY))


def close_day(p, plan_early=True):
    """Day 196 closes: its last beat, then (often) the next day's Day Plan written early, then rotation."""
    if plan_early:
        with open(p.root / LEGACY, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n### Clocks\n\n- noon: Jory\n\n"
                     "### Beat 4131 PRE-COMMIT (GM, binding): MORNING\n\n")
    got = p.rotate(beat="4131")
    p.ship(NEXT_DAY, append="### Beat 4131: Day 196, ~21:00 to Day 197, ~06:00 (Tuesday).\n\nMorning.\n",
           session=got["path"])
    return got


class SummaryTests(unittest.TestCase):
    def closed_day(self, p, plan_early=True):
        return close_day(p, plan_early)

    def test_day_summary_range_stops_before_the_next_days_plan(self):
        p = Project(self)
        self.closed_day(p)
        plan = rp_gate.summary_plan(p.root, DAY_197)
        day = next(d for d in plan["days"] if d["day"] == 196)
        lines = p.read(LEGACY).splitlines()
        span = "\n".join(lines[day["beats"]["start"] - 1:day["beats"]["end"]])
        self.assertIn("### Beat 4130: Day 196", span)
        self.assertNotIn("Day Plan: Day 197", span)
        self.assertNotIn("Beat 4131", span)
        self.assertIn("### Beat 4110: Day 196", lines[day["beats"]["start"] - 1:day["beats"]["end"]][2] + span)
        self.assertEqual((day["beats"]["first_beat"], day["beats"]["last_beat"]), ("4110", "4130"))
        self.assertEqual([x["heading"][:30] for x in day["plans"]], ["## Day Plan: Day 196 (Monday)"])
        self.assertEqual(day["path"], f"{SUMMARY_DIR}/day-0196.md")
        self.assertTrue(day["closed"])
        # the next day's continuity read still finds its early plan in the closed file
        cont = rp_gate.continuity(p.root, DAY_197)
        self.assertTrue(any(x["path"] == LEGACY and "Day 197" in x["heading"] for x in cont["plans"]))

    def test_closed_days_without_a_summary_are_due(self):
        p = Project(self)
        self.closed_day(p)
        got = json.loads(run("session", "--project-dir", str(p.root))[1])
        self.assertEqual(got["summaries_due"], [196])
        p.write(f"{SUMMARY_DIR}/day-0196.md", "---\nfiction_day: 196\n---\n\n## Day 196\n")
        got = json.loads(run("session", "--project-dir", str(p.root))[1])
        self.assertEqual(got["summaries_due"], [])

    def summary_text(self, extra=""):
        return f"## Day 196 (Monday)\n\n- **~08:10, the yard (B4110):** Liss set down the axe.{extra}\n"

    def test_summary_publish_never_overwrites(self):
        p = Project(self)
        self.closed_day(p)
        draft = p.write("Work/rp/.gate/day-summary.md", self.summary_text())
        got = rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")
        self.assertEqual(got["action"], "published")
        published = p.read(f"{SUMMARY_DIR}/day-0196.md")
        data = rp_gate.read_frontmatter(p.root / f"{SUMMARY_DIR}/day-0196.md")
        self.assertEqual((data["fiction_day"], data["source"]), (196, LEGACY))
        self.assertEqual(rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-27")["action"],
                         "unchanged")
        p.write("Work/rp/.gate/day-summary.md", self.summary_text(" Then rain."))
        with self.assertRaises(ValueError):
            rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")
        self.assertEqual(p.read(f"{SUMMARY_DIR}/day-0196.md"), published)

    def test_summary_refuses_a_line_citation_into_an_open_file(self):
        p = Project(self)
        self.closed_day(p)
        draft = p.write("Work/rp/.gate/day-summary.md", self.summary_text(f" ({DAY_197}:12)"))
        with self.assertRaises(ValueError) as caught:
            rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")
        self.assertIn("open", str(caught.exception))
        p.write("Work/rp/.gate/day-summary.md", self.summary_text(" (rp_session_2026-10-24.md:99999)"))
        with self.assertRaises(ValueError):
            rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")
        p.write("Work/rp/.gate/day-summary.md", self.summary_text(" (rp_session_2026-10-24.md:40; Liss.md:3)"))
        self.assertEqual(rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")["action"],
                         "published")

    def test_summary_refuses_a_beat_after_the_day(self):
        p = Project(self)
        self.closed_day(p)
        draft = p.write("Work/rp/.gate/day-summary.md", self.summary_text(" Then B4131 came."))
        with self.assertRaises(ValueError):
            rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")

    def test_summary_requires_its_day_heading_and_a_closed_day(self):
        p = Project(self)
        self.closed_day(p)
        draft = p.write("Work/rp/.gate/day-summary.md", "## Day 195\n\n- Wrong day (B4100).\n")
        with self.assertRaises(ValueError):
            rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")
        draft = p.write("Work/rp/.gate/day-summary.md", "## Day 197\n\n- Open day (B4131).\n")
        with self.assertRaises(ValueError):  # Day 197's file is open
            rp_gate.publish_summary(p.root, DAY_197, 197, draft, today="2026-10-26")

    def test_stitch_orders_days_and_never_overwrites(self):
        p = Project(self)
        self.closed_day(p)
        draft = p.write("Work/rp/.gate/day-summary.md", self.summary_text())
        rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")
        extra = p.write("Work/rp/.gate/session-extra.md",
                        "## Keeper rulings\n\n- None.\n\n## Day 197 (Tuesday), so far\n\n- Morning (B4131).\n\n"
                        "## Threads open\n\n- The vine.\n")
        out = "Work/rp/summaries/2026-10-26_test-day-196-197.md"
        got = rp_gate.stitch_summary(p.root, DAY_197, out, extra, today="2026-10-26")
        self.assertEqual(got["action"], "published")
        text = p.read(out)
        order = [text.index(x) for x in ("## Keeper rulings", "## Day 196 (Monday)", "## Day 197 (Tuesday)",
                                         "## Threads open", "## State at close")]
        self.assertEqual(order, sorted(order))
        self.assertIn('POW: "54/60"', text)
        again = rp_gate.stitch_summary(p.root, DAY_197, out, extra, today="2026-10-27")
        self.assertEqual((again["action"], p.read(out)), ("unchanged", text))  # never overwritten

    def test_stitch_refuses_a_missing_day_summary_and_an_overlap(self):
        p = Project(self)
        self.closed_day(p)
        extra = p.write("Work/rp/.gate/session-extra.md", "## Day 197, so far\n\n- Morning (B4131).\n")
        out = "Work/rp/summaries/2026-10-26_test.md"
        with self.assertRaises(ValueError) as caught:
            rp_gate.stitch_summary(p.root, DAY_197, out, extra, today="2026-10-26")
        self.assertIn("196", str(caught.exception))
        draft = p.write("Work/rp/.gate/day-summary.md", self.summary_text())
        rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")
        p.write("Work/rp/.gate/session-extra.md", "## Day 196\n\n- Again (B4110).\n")
        with self.assertRaises(ValueError):
            rp_gate.stitch_summary(p.root, DAY_197, out, extra, today="2026-10-26")
        self.assertFalse((p.root / out).exists())

    def test_after_the_marker_goes_the_session_is_named_explicitly(self):
        # /rp:end removes the marker before it stitches; without session_opened the chain would reach
        # back to the legacy log and demand summaries for days earlier sessions closed.
        p = Project(self)
        self.closed_day(p)
        p.rotate(turn_text('currentDate: "Day 198 (Wednesday)"\nscene.time: "06:00"',
                           "Scene: Day 198, ~06:00, calm | Location: the kitchen | Present: Liss"), beat="4132")
        p.marker(f"session_file: {DAY_198}\nsession_opened: {DAY_197}\n")  # a later session, opened on Day 197
        draft = p.write("Work/rp/.gate/day-summary.md", "## Day 196\n\n- Axe (B4110).\n")  # the earlier session's
        rp_gate.publish_summary(p.root, DAY_198, 196, draft, today="2026-10-26")
        self.assertEqual([d["day"] for d in rp_gate.summary_plan(p.root, DAY_198)["days"]], [197])
        (p.root / "Work/markers/rp-active").unlink()
        self.assertEqual([d["day"] for d in rp_gate.summary_plan(p.root, DAY_198, opened=DAY_197)["days"]], [197])
        code, out, _err = run("summary", "--project-dir", str(p.root), "--session-file", DAY_198,
                              "--opened", DAY_197)
        self.assertEqual((code, [d["day"] for d in json.loads(out)["days"]]), (0, [197]))

    def test_no_log_dir_means_no_day_summaries(self):
        p = Project(self)
        p.write(LEGACY, p.read(LEGACY).replace('log_dir: "Work/rp/test/log"\n', ""))
        self.assertEqual(rp_gate.summary_plan(p.root, LEGACY)["days"], [])


class ReviewPassOneTests(unittest.TestCase):
    """Defects the first cold review found (review-pass1.md), each red before its fix."""

    SHIP = 'scene.mood: "tense"\nscene.time: "19:05"\nrun.workings.HOMING: on\nrun.stats.POW: "50/60"'

    def edited(self, old, new, **kw):
        p = Project(self, **kw)
        text = p.read(LEGACY)
        self.assertIn(old, text)
        p.write(LEGACY, text.replace(old, new, 1))
        return p

    def test_a_blank_line_or_comment_inside_a_block_never_reverts_a_delta(self):  # S1
        cases = {
            "blank line in scene": ('  mood: "calm"\n  pending_rolls: []\n', '  mood: "calm"\n\n  pending_rolls: []\n'),
            "column-0 comment in scene": ('  mood: "calm"\n', '# scene continues\n  mood: "calm"\n'),
            "blank line in run": ("  workings:\n", "\n  workings:\n"),
            "column-0 comment in run": ("  clocks:\n", "# the clocks\n  clocks:\n"),
        }
        for label, (old, new) in cases.items():
            with self.subTest(label):
                p = self.edited(old, new)
                p.ship(turn_text(self.SHIP))
                data = p.frontmatter(LEGACY)
                self.assertEqual(data["scene"]["mood"], "tense")
                self.assertEqual(data["run"]["workings"]["HOMING"], "on")
                self.assertEqual(data["run"]["stats"]["POW"], "50/60")
                head = p.read(LEGACY).split("\n---\n", 1)[0]
                for key in ("  mood:", "  pending_rolls:", "    HOMING:", "    POW:", "  workings:", "  clocks:"):
                    self.assertEqual(head.count("\n" + key), 1, key)

    def test_ship_keeps_untouched_lines_of_a_block_as_written(self):  # M1
        cases = {
            "indented comment": ('  mood: "calm"\n', '  # the mood the Keeper set\n  mood: "calm"\n'),
            "block scalar": ('  mood: "calm"\n', '  mood: "calm"\n  notes: |\n    Line one.\n\n    Line two # kept\n'),
            "folded continuation": ('  mood: "calm"\n', '  mood: "calm"\n  note: the barn, by the bench,\n    under the loft\n'),
            "float and date": ('  mood: "calm"\n', '  mood: "calm"\n  tension: 0.5\n  date: 2026-10-24\n'),
            "run comments": ("  stats:\n", '  stats:            # an integer, or "current/max"\n'),
        }
        for label, (old, new) in cases.items():
            with self.subTest(label):
                p = self.edited(old, new)
                before = p.read(LEGACY).split("\n---\n", 1)[0].splitlines()
                p.ship(turn_text('scene.time: "19:05"\nrun.stats.POW: "50/60"'))
                after = p.read(LEGACY).split("\n---\n", 1)[0].splitlines()
                changed = ("  time:", "currentTime:", '    POW: "54/60"')
                self.assertEqual([x for x in before if not x.startswith(changed)],
                                 [x for x in after if not x.startswith(changed + ("shipped:", '    POW: "50/60"'))])

    def test_a_day_closed_by_rp_start_is_due_and_planned(self):  # S2
        p = Project(self)
        got = rp_gate.rotate(p.root, LEGACY, day=197, start=True, today="2026-10-27")
        self.assertEqual(got["action"], "rotated")
        plan = rp_gate.summary_plan(p.root, got["path"])
        self.assertEqual([(d["day"], d["source"]) for d in plan["days"]], [(196, LEGACY)])
        session = json.loads(run("session", "--project-dir", str(p.root))[1])
        self.assertEqual(session["summaries_due"], [196])
        draft = p.write("Work/rp/.gate/day-summary.md", "## Day 196 (Monday)\n\n- Axe (B4110).\n")
        rp_gate.publish_summary(p.root, got["path"], 196, draft, today="2026-10-27")
        self.assertEqual(json.loads(run("session", "--project-dir", str(p.root))[1])["summaries_due"], [])

    def test_refresh_moves_the_block_back_to_the_top(self):  # M2
        p = Project(self)
        rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")
        heading = "## Run state (one page — the GM updates this, nothing else)\n\n"
        p.write(STATE_FILE, p.read(STATE_FILE).replace(
            heading, heading + "**Paused: Day 196, ~22:00, after Beat 4131:** the kitchen.\n\n", 1))
        warned = json.loads(run("session", "--project-dir", str(p.root))[1])["run"]["warnings"]
        self.assertIn("state --refresh", " ".join(warned))
        p.ship(turn_text('run.stats.POW: "53/60"'))
        self.assertEqual(rp_gate.refresh_state(p.root, LEGACY, today="2026-10-27")["action"], "refreshed")
        text = p.read(STATE_FILE)
        self.assertLess(text.index("<!-- rp-run-state"), text.index("**Paused: Day 196, ~22:00"))
        self.assertEqual(text.count("**Paused: Day 196, ~22:00"), 1)
        session = json.loads(run("session", "--project-dir", str(p.root))[1])
        self.assertTrue(session["live_state"]["run_state"]["generated"])
        self.assertEqual(session["run"]["warnings"], [])

    def test_line_separators_round_trip(self):  # m1
        value = {"a": "one\u2028two", "b": "x\u2029y", "c": "p\x85q", "d": "tab\there"}
        written = "".join(rp_gate.emit_yaml("k", value))
        self.assertEqual(rp_gate.parse_yaml(written), {"k": value})
        self.assertEqual(len(written.splitlines()), 5)  # every line count in the gate uses splitlines
        p = Project(self)
        for name in ("tab\tname", "line\u2028name"):
            with self.subTest(name=name):
                self.assertFalse(rp_gate._good_name(name))
        body = "### Beat 4131: Day 196.\n\nShe says\u2028nothing.\n"
        p.ship(turn_text('scene.mood: "a\u2028b"'), append=body)
        self.assertTrue(p.read(LEGACY).endswith(body))
        self.assertEqual(p.frontmatter(LEGACY)["scene"]["mood"], "a\u2028b")

    def test_a_name_with_quotes_or_a_backslash_is_written_back(self):  # m2
        run_block = RUN_BLOCK.replace("    HOMING: off\n", "    HOMING: off\n    'T18 \"the tell\"': on\n    'C:\\dir': off\n")
        p = Project(self, run=run_block)
        self.assertEqual(rp_gate.standing_run_warnings(p.frontmatter(LEGACY)["run"]), [])
        got = p.ship(turn_text('run:\n  workings:\n    \'T18 "the tell"\': off\nrun.stats.POW: "50/60"'))
        self.assertEqual(got["dropped"], [])
        workings = p.frontmatter(LEGACY)["run"]["workings"]
        self.assertEqual((workings['T18 "the tell"'], workings["C:\\dir"]), ("off", "off"))
        self.assertEqual(rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")["action"], "refreshed")
        code, out, _err = run("state", "--refresh", "--project-dir", str(p.root))
        self.assertEqual((code, json.loads(out)["action"]), (0, "unchanged"))

    def test_a_name_that_cannot_be_written_is_refused_not_raised(self):  # m2
        p = Project(self, run=RUN_BLOCK.replace("    HOMING: off\n", '    HOMING: off\n    "tab\\there": on\n'))
        self.assertIn("cannot be written", " ".join(rp_gate.standing_run_warnings(p.frontmatter(LEGACY)["run"])))
        self.assertEqual(rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")["action"], "refused")
        code, _out, err = run("state", "--refresh", "--project-dir", str(p.root))
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", err)

    def test_ship_rechecks_the_footer_on_retry(self):  # m3
        p = Project(self)
        real, calls = rp_gate._ship_text, []

        def closed_meanwhile(*args, **kwargs):
            if not calls:
                rp_gate._append_footer(p.root / LEGACY, DAY_197, 197, "2026-10-26")
            calls.append(1)
            return real(*args, **kwargs)

        with mock.patch.object(rp_gate, "_ship_text", side_effect=closed_meanwhile):
            with self.assertRaises(ValueError):
                p.ship(turn_text('scene.mood: "tense"'))
        self.assertEqual(rp_gate.rotation_footer(p.root / LEGACY), DAY_197)

    def test_ship_takes_the_beats_day_from_the_anchor_like_the_lint(self):  # m4
        p = Project(self)
        text = turn_text('run.clocks.wheels: {day: 196, fires: "Jory\'s wheels"}', NEXT_DAY_ANCHOR)
        result, *_ = p.lint('run.clocks.wheels: {day: 196, fires: "Jory\'s wheels"}', anchor=NEXT_DAY_ANCHOR)
        self.assertIn("run_clock_day", run_checks(result))
        got = p.ship(text)
        self.assertEqual([(d["key"], d["check"]) for d in got["dropped"]], [("run.clocks.wheels", "run_clock_day")])

    def test_a_body_fact_is_one_line(self):  # m5
        p = Project(self)
        result, *_ = p.lint("run.body.tail: |\n  a horse's tail\n  that swishes")
        self.assertEqual(run_checks(result), ["run_bad_value"])

    def stitched(self, extra_text):
        p = Project(self)
        close_day(p)
        draft = p.write("Work/rp/.gate/day-summary.md", "## Day 196 (Monday)\n\n- **~08:10 (B4110):** axe.\n")
        rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")
        extra = p.write("Work/rp/.gate/session-extra.md", extra_text)
        out = "Work/rp/summaries/2026-10-26_x.md"
        return p, out, extra, rp_gate.stitch_summary(p.root, DAY_197, out, extra, today="2026-10-26")

    def test_stitch_keeps_sections_between_days_and_one_state_at_close(self):  # m6
        p, out, extra, got = self.stitched(
            "## Keeper rulings\n\n- None.\n\n## Night twelve (B4131)\n\n- NIGHT.\n\n"
            "## Day 197 (Tuesday), morning\n\n- MORNING (B4131).\n\n## The wager (B4131)\n\n- WAGER.\n\n"
            "## Day 197, evening\n\n- EVENING (B4131).\n\n## State at close\n\n- POW per the GM.\n\n"
            "## Threads open\n\n- The vine.\n")
        text = p.read(out)
        order = [text.index(x) for x in ("## Keeper rulings", "## Day 196", "MORNING", "WAGER", "EVENING",
                                         "## Threads open", "## State at close")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(text.count("## State at close"), 1)
        self.assertIn("POW per the GM", text)  # the GM's section stays (review pass 2, N1); the copy goes under it
        self.assertLess(text.index("POW per the GM"), text.index("### Run state"))
        self.assertIn("## Night twelve", text)
        self.assertEqual(got["days"], [196, 197])

    def test_stitch_again_is_a_no_op(self):  # m7
        p, out, extra, _got = self.stitched("## Day 197, so far\n\n- Morning (B4131).\n")
        published = p.read(out)
        again = rp_gate.stitch_summary(p.root, DAY_197, out, extra, today="2026-10-27")
        self.assertEqual(again["action"], "unchanged")
        self.assertEqual(p.read(out), published)
        p.write("Work/rp/.gate/session-extra.md", "## Day 197, so far\n\n- Other (B4131).\n")
        with self.assertRaises(ValueError):
            rp_gate.stitch_summary(p.root, DAY_197, out, extra, today="2026-10-27")

    def test_ship_keeps_bytes(self):  # m8, m13
        p = Project(self)
        path = p.root / LEGACY
        path.write_bytes(b"\xef\xbb\xbf" + p.read(LEGACY).replace("\n", "\r\n").encode() + b"He said it\x92s fine.\r\n")
        os.chmod(path, 0o640)
        p.ship(turn_text('scene.location: "the yard"\nrun.stats.POW: "50/60"'), append="### Beat 4131: Day 196.\n\nX.\n")
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertIn(b"He said it\x92s fine.", raw)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        data = p.frontmatter(LEGACY)
        self.assertEqual((data["currentLocation"], data["run"]["stats"]["POW"]), ("the yard", "50/60"))

    def test_ship_syncs_every_mirror(self):  # m13
        p = Project(self)
        p.ship(turn_text('scene.location: "the yard"\nscene.participants:\n  - name: "Alder"\n    role: "PC"\n'
                         '  - name: "Mira"\n    role: "NPC"',
                         "Scene: Day 196, ~19:00, calm | Location: the yard | Present: Mira"))
        data = p.frontmatter(LEGACY)
        self.assertEqual((data["currentLocation"], data["participants"]), ("the yard", ["Alder", "Mira"]))

    def test_a_day_that_ends_on_an_insert_beat_allows_its_highest_beat(self):  # m10
        p = Project(self)
        with open(p.root / LEGACY, "a", encoding="utf-8") as fh:
            fh.write("### Beat 4120A (INSERT, a missed line): Day 196, ~08:20.\n\nLiss nods.\n\n")
        close_day(p, plan_early=False)
        draft = p.write("Work/rp/.gate/day-summary.md", "## Day 196\n\n- The last beat (B4130).\n")
        self.assertEqual(rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")["action"],
                         "published")

    def test_a_run_key_without_a_schema_routes_as_before(self):  # m11
        p = Project(self, run="")
        result, scene, config, turn = p.lint('run.stats.POW: "53/60"')
        triggers = rp_gate.beat_triggers(turn, result, scene, config)
        self.assertEqual([rp_gate.route(result, triggers, config, **kw)["next"]
                          for kw in ({}, {"recheck": True}, {"final": True})], ["fix", "review", "fix"])

    def test_a_log_is_known_by_its_frontmatter_too(self):  # m13
        p = Project(self)
        p.write("Work/rp/test/notes.md", "---\nscene:\n  time: \"19:00\"\n---\n\n## Run state\n\nx\n")
        p.write(LEGACY, p.read(LEGACY).replace("Work/rp/test/alder.md#Run state", "Work/rp/test/notes.md#Run state"))
        self.assertEqual(rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")["action"], "refused")

    def test_a_second_summariser_publishing_first_is_handled(self):  # m13
        p = Project(self)
        close_day(p)
        text = "## Day 196 (Monday)\n\n- Axe (B4110).\n"
        draft = p.write("Work/rp/.gate/day-summary.md", text)
        real = rp_gate._publish

        def raced(target, out, replace=False):
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            Path(target).write_text("---\nfiction_day: 196\n---\n\n" + text, encoding="utf-8")
            return real(target, out, replace)

        with mock.patch.object(rp_gate, "_publish", side_effect=raced):
            self.assertEqual(rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")["action"],
                             "unchanged")

    def test_a_plan_written_early_in_the_file_before_is_planned(self):  # m13
        p = Project(self)
        close_day(p)  # Day 197's plan sits at the end of the closed legacy log
        plans = rp_gate.summary_plan(p.root, DAY_197)["open"]["plans"]
        self.assertEqual([(x["path"], x["heading"][:30]) for x in plans], [(LEGACY, "## Day Plan: Day 197 (Tuesday)")])


class ReviewPassTwoTests(unittest.TestCase):
    """Defects the second cold review found (review-pass2.md), each red before its fix."""

    def stitch(self, run=RUN_BLOCK, extra_text=""):
        p = Project(self, run=run)
        close_day(p)
        draft = p.write("Work/rp/.gate/day-summary.md", "## Day 196 (Monday)\n\n- **~08:10 (B4110):** axe.\n")
        rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")
        extra = p.write("Work/rp/.gate/session-extra.md", extra_text)
        out = "Work/rp/summaries/2026-10-26_x.md"
        got = rp_gate.stitch_summary(p.root, DAY_197, out, extra, today="2026-10-26")
        return p.read(out), got

    GM_STATE = "## State at close\n\n| Place | the barn |\n| Gear | the breastcollar |\n"

    def test_stitch_keeps_the_gms_state_at_close(self):  # N1
        extra = "## Day 197, so far\n\n- Morning (B4131).\n\n" + self.GM_STATE + "\n## Threads open\n\n- The vine.\n"
        for run in (RUN_BLOCK, ""):
            with self.subTest(schema=bool(run)):
                text, got = self.stitch(run, extra)
                self.assertEqual(text.count("## State at close"), 1)
                self.assertIn("| Gear | the breastcollar |", text)
                self.assertLess(text.index("## Threads open"), text.index("## State at close"))
                self.assertEqual(got["days"], [196, 197])
                self.assertEqual('POW: "54/60"' in text, bool(run))
                if run:
                    self.assertLess(text.index("| Gear |"), text.index('POW: "54/60"'))

    def test_stitch_ignores_headings_in_fences_and_puts_dayless_sections_after_the_days(self):  # N5, N7
        text, _got = self.stitch(extra_text="# My own title\n\n## Day 197, so far\n\n- Morning (B4131).\n\n"
                                            "```markdown\n## Day 196 (quoted)\n```\n\n## Threads open\n\n- Vine.\n")
        self.assertEqual(text.count("\n# "), 1)  # one title, stitch's own
        self.assertNotIn("My own title", text)
        self.assertIn("```markdown\n## Day 196 (quoted)\n```", text)
        text, _got = self.stitch(extra_text="## Keeper rulings\n\n- None.\n\n## Threads open\n\n- Vine.\n")
        self.assertLess(text.index("## Day 196"), text.index("## Threads open"))

    def test_the_setter_writes_where_the_parser_reads(self):  # N2
        cases = {
            "duplicate key": ('  mood: "calm"\n', '  mood: "calm"\n  mood: "grim"\n'),
            "tab indentation": ('  mood: "calm"\n', '\tmood: "calm"\n'),
        }
        for label, (old, new) in cases.items():
            with self.subTest(label):
                p = Project(self)
                p.write(LEGACY, p.read(LEGACY).replace(old, new, 1))
                p.ship(turn_text('scene.mood: "tense"'))
                self.assertEqual(p.frontmatter(LEGACY)["scene"]["mood"], "tense")

    def test_ship_refuses_what_it_cannot_write_where_the_parser_reads(self):  # N2 guard
        p = Project(self)
        before = p.read(LEGACY)
        with mock.patch.object(rp_gate, "_set_paths", side_effect=lambda lines, root, paths: list(lines)):
            with self.assertRaises(ValueError) as caught:
                p.ship(turn_text('scene.mood: "tense"'))
        self.assertIn("scene.mood", str(caught.exception))
        self.assertEqual(p.read(LEGACY), before)

    def test_an_older_missing_day_stays_due_behind_a_summarised_one(self):  # N3
        p = Project(self)
        close_day(p)  # Day 196 closes; its summary fails
        p.rotate(turn_text('currentDate: "Day 198 (Wednesday)"\nscene.time: "06:00"',
                           "Scene: Day 198, ~06:00, calm | Location: the kitchen | Present: Liss"), beat="4132")
        draft = p.write("Work/rp/.gate/day-summary.md", "## Day 197\n\n- Morning (B4131).\n")
        rp_gate.publish_summary(p.root, DAY_198, 197, draft, today="2026-10-26")
        p.marker(f"session_file: {DAY_198}\nsession_opened: {DAY_198}\n")  # resumed without /rp:end
        self.assertEqual([d["day"] for d in rp_gate.summary_plan(p.root, DAY_198)["days"] if not d["exists"]], [196])
        # session's per-turn view stops at the first summarised day (cheap); summary, run at every rotation and
        # at /rp:end, walks the whole run, so Day 196 is written at the next of either
        self.assertEqual(json.loads(run("session", "--project-dir", str(p.root))[1])["summaries_due"], [])

    def test_rotation_keeps_a_line_separator_inside_a_line(self):  # N4
        p = Project(self)
        p.write(LEGACY, p.read(LEGACY).replace("Register: plain sentences.", "Register: plain\u2028sentences."))
        p.rotate(beat="4131")
        self.assertIn("Register: plain\u2028sentences.", p.read(DAY_197))
        self.assertEqual(p.frontmatter(DAY_197)["log_day"], 197)

    def test_clearing_the_last_clock_leaves_an_empty_mapping(self):  # N6
        p = Project(self)
        p.ship(turn_text("run.clocks.vine-test: null\nrun.clocks.cure: null"))
        self.assertEqual(p.frontmatter(LEGACY)["run"]["clocks"], {})

    def test_a_list_at_its_keys_own_indent_is_replaced_whole(self):  # N7
        p = Project(self)
        p.write(LEGACY, p.read(LEGACY).replace('participants: ["Alder", "Liss"]\n',
                                               'participants:\n- "Alder"\n- "Liss"\n'))
        p.ship(turn_text('scene.participants:\n  - name: "Alder"\n    role: "PC"\n  - name: "Mira"\n    role: "NPC"',
                         "Scene: Day 196, ~19:00, calm | Location: the kitchen | Present: Mira"))
        self.assertEqual(p.frontmatter(LEGACY)["participants"], ["Alder", "Mira"])
        self.assertNotIn('- "Liss"', p.read(LEGACY).split("\n---\n", 1)[0])

    def test_a_raw_separator_already_in_the_frontmatter_stays_in_its_line(self):  # N7
        p = Project(self)
        p.write(LEGACY, p.read(LEGACY).replace('location: "the kitchen"', 'location: "the kitchen\u2028hearth"'))
        p.ship(turn_text('scene.mood: "tense"'))
        self.assertEqual(p.frontmatter(LEGACY)["scene"]["location"], "the kitchen\u2028hearth")

    def test_a_crlf_run_state_file_gets_a_crlf_block(self):  # N7
        p = Project(self)
        path = p.root / STATE_FILE
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        rp_gate.refresh_state(p.root, LEGACY, today="2026-10-26")
        raw = path.read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))


class ReviewPassThreeTests(unittest.TestCase):
    """Defects the third cold review found (review-pass3.md), each red before its fix."""

    def edited(self, old, new):
        p = Project(self)
        text = p.read(LEGACY)
        self.assertIn(old, text)
        p.write(LEGACY, text.replace(old, new, 1))
        return p

    def test_items_written_dash_tab_are_replaced_whole_and_run_stays_read(self):  # P1, P2
        p = self.edited('participants: ["Alder", "Liss"]\n', 'participants:\n-\t"Alder"\n-\t"Liss"\n')
        p.ship(turn_text('scene.participants:\n  - name: "Alder"\n    role: "PC"\n  - name: "Mira"\n    role: "NPC"',
                         "Scene: Day 196, ~19:00, calm | Location: the kitchen | Present: Mira"))
        data = p.frontmatter(LEGACY)
        self.assertEqual(data["participants"], ["Alder", "Mira"])
        self.assertEqual(data["run"]["stats"]["POW"], "54/60")

    def test_a_tab_inside_a_name_is_found_not_duplicated(self):  # P2
        p = self.edited("    HOMING: off\n", "    HOMING: off\n    THE\tGRIP: on\n")
        p.ship(turn_text("run.workings.THE  GRIP: off"))
        head = p.read(LEGACY).split("\n---\n", 1)[0]
        self.assertEqual(head.count("GRIP"), 1)
        self.assertEqual(p.frontmatter(LEGACY)["run"]["workings"]["THE  GRIP"], "off")

    def test_a_tab_indented_key_is_replaced_not_duplicated(self):  # P8
        p = self.edited('  mood: "calm"\n', '\tmood: "calm"\n')
        p.ship(turn_text('scene.mood: "tense"'))
        self.assertEqual(p.read(LEGACY).split("\n---\n", 1)[0].count("mood:"), 1)

    def test_damage_to_an_unchanged_key_is_refused(self):  # P1
        p = self.edited('  mood: "calm"\n  pending_rolls: []\n', '  mood: "calm"\r  pending_rolls: []\n')
        before = p.read(LEGACY)
        with self.assertRaises(ValueError) as caught:
            p.ship(turn_text('scene.mood: "tense"'))
        self.assertIn("scene", str(caught.exception))
        self.assertEqual(p.read(LEGACY), before)

    def test_damage_to_a_key_the_delta_never_touches_is_refused(self):  # P1
        p = self.edited('currentLocation: "the kitchen"\ncurrentTime: "19:00"\n',
                        'currentLocation: "the kitchen"\rcurrentTime: "19:00"\n')
        before = p.read(LEGACY)
        with self.assertRaises(ValueError) as caught:
            p.ship(turn_text('scene.location: "the yard"',
                             "Scene: Day 196, ~19:00, calm | Location: the yard | Present: Liss"))
        self.assertIn("currentTime", str(caught.exception))
        self.assertEqual(p.read(LEGACY), before)

    def test_the_guard_covers_the_shipped_mark(self):  # P8
        p = Project(self)
        real = rp_gate._set_paths
        with mock.patch.object(rp_gate, "_set_paths",
                               side_effect=lambda lines, root, paths: real(lines, root, [x for x in paths
                                                                                       if x != ("shipped",)])):
            with self.assertRaises(ValueError):
                p.ship(turn_text('scene.mood: "tense"'))

    def test_a_list_item_whose_first_key_needs_quotes_ships(self):  # P3
        p = Project(self)
        p.ship(turn_text('scene.pending_rolls: [{"Liss\'s check": "DC 12", who: "Liss"}]'))
        self.assertEqual(p.frontmatter(LEGACY)["scene"]["pending_rolls"], [{"Liss's check": "DC 12", "who": "Liss"}])

    def test_a_list_value_becomes_a_mapping_through_a_delta_key(self):  # P8
        p = self.edited('campaign: "Test Run"\n', 'campaign: "Test Run"\ntracker:\n  - "a"\n  - "b"\n')
        p.write(".claude/modes/rp.yaml", 'turnGate:\n  deltaKeys: ["tracker.mood"]\n')
        got = p.ship(turn_text('tracker.mood: "grim"'))
        self.assertEqual(got["dropped"], [])
        self.assertEqual(p.frontmatter(LEGACY)["tracker"], {"mood": "grim"})

    def chain(self, p, days):
        got = {"path": None}
        for day in days:
            text = turn_text(f'currentDate: "Day {day} (X)"\nscene.time: "06:00"',
                             f"Scene: Day {day}, ~06:00, calm | Location: the kitchen | Present: Liss")
            got = p.rotate(text, beat=str(4200 + day))
            p.ship(text, append=f"### Beat {4200 + day}: Day {day}, ~06:00.\n\nMorning.\n", session=got["path"])
        return got["path"]

    def test_publish_reaches_every_day_the_plan_lists(self):  # P4
        p = Project(self)
        with mock.patch.object(rp_gate, "MAX_READ_HOPS", 3):
            current = self.chain(p, range(197, 202))
            due = [d["day"] for d in rp_gate.summary_plan(p.root, current, opened=current)["days"]]
            self.assertTrue(due)
            draft = p.write("Work/rp/.gate/day-summary.md", f"## Day {due[0]}\n\n- Morning.\n")
            self.assertEqual(rp_gate.publish_summary(p.root, current, due[0], draft, today="2026-10-26")["action"],
                             "published")

    def test_the_deep_walk_passes_a_closed_file_it_cannot_place(self):  # P5
        p = Project(self)
        current = self.chain(p, (197, 198, 199))
        day_198 = p.read(DAY_198)
        p.write(DAY_198, day_198.replace('log_dir: "Work/rp/test/log"\n', "", 1))  # a hand edit
        due = [d["day"] for d in rp_gate.summary_plan(p.root, current, opened=current)["days"]]
        self.assertEqual(due, [196, 197])

    def test_an_extra_text_with_a_bom_keeps_its_first_day(self):  # P7
        p = Project(self)
        close_day(p)
        draft = p.write("Work/rp/.gate/day-summary.md", "﻿## Day 196 (Monday)\n\n- Axe (B4110).\n")
        rp_gate.publish_summary(p.root, DAY_197, 196, draft, today="2026-10-26")
        extra = p.write("Work/rp/.gate/session-extra.md", "﻿## Day 197, so far\n\n- Morning (B4131).\n")
        got = rp_gate.stitch_summary(p.root, DAY_197, "Work/rp/summaries/x.md", extra, today="2026-10-26")
        self.assertEqual(got["days"], [196, 197])

    def test_an_orphan_with_a_line_separator_in_its_header_is_set_aside(self):  # P8
        p = Project(self)
        p.write(LEGACY, p.read(LEGACY).replace("Register: plain sentences.", "Register: plain\u2028sentences."))
        with mock.patch.object(rp_gate, "_swap_marker", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                p.rotate(beat="4131")
        got = p.rotate(turn_text("none", "Scene: Day 196, ~19:05, calm | Location: the kitchen | Present: Liss"))
        self.assertEqual(got["set_aside"], [DAY_197])


class ReviewPassFourTests(unittest.TestCase):
    """Defects the fourth cold review found (review-pass4.md), each red before its fix."""

    def test_a_frontmatter_that_reads_as_a_list_is_refused_cleanly(self):  # F1
        p = Project(self)
        p.write(LEGACY, p.read(LEGACY).replace("---\ncampaign:", "---\n- stray note\ncampaign:", 1))
        before = p.read(LEGACY)
        with self.assertRaises(ValueError):
            p.ship(turn_text('scene.mood: "tense"'))
        turn = p.write("Work/rp/.gate/turn.md", turn_text('scene.mood: "tense"'))
        code, _out, err = run("ship", "--project-dir", str(p.root), "--turn-file", str(turn))
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        self.assertEqual(p.read(LEGACY), before)

    def test_a_flow_item_escapes_line_separators(self):  # F2
        p = Project(self)
        p.ship(turn_text('scene.pending_rolls: [{"Liss\'s check": "a\\u2028b", who: "x\\u0085y"}]'))
        text = p.read(LEGACY)
        self.assertEqual(len(text.splitlines()), text.count("\n"))  # no line the gate's counts would split
        self.assertEqual(p.frontmatter(LEGACY)["scene"]["pending_rolls"], [{"Liss's check": "a\u2028b", "who": "x\x85y"}])

    def test_a_mapping_written_as_a_dash_tab_list_is_replaced_whole(self):  # F3
        p = Project(self)
        text = p.read(LEGACY)
        start, end = text.index("scene:\n"), text.index("run:\n")
        p.write(LEGACY, text[:start] + 'scene:\n  -\t"a"\n  -\t"b"\n' + text[end:])
        p.ship(turn_text('scene.mood: "tense"'))
        self.assertEqual(p.frontmatter(LEGACY)["scene"], {"mood": "tense"})


class ContractTests(unittest.TestCase):
    def read(self, rel):
        return (REPO / rel).read_text(encoding="utf-8")

    def test_turn_ships_then_refreshes_and_summarises_after_the_output(self):
        turn = self.read("plugins/rp/commands/turn.md")
        step = turn.split("# 3e. Ship", 1)[1].split("# 3f.", 1)[0]
        order = [step.index(x) for x in ('rp_gate.py" rotate', 'rp_gate.py" ship', "OUTPUT DRAFT to Keeper",
                                         "state --refresh", "--publish")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("APPEND DRAFT to SESSION_FILE", step)  # nothing appends or applies by hand
        self.assertIn("never append the beat or edit the frontmatter by hand", " ".join(step.split()))
        surrender = " ".join(turn.split("# 3f.", 1)[1].split("END LOOP", 1)[0].split())
        self.assertTrue("rp_gate.py ship" in surrender or "through 3e" in surrender)  # never applied by hand

    def test_end_stitches_and_names_the_session(self):
        end = self.read("plugins/rp/commands/end.md")
        step3 = end.split("### Step 3", 1)[1].split("### Step 5", 1)[0]
        for text in ("--opened", "--stitch", "state --refresh", "INFERRED"):
            with self.subTest(text=text):
                self.assertIn(text, step3)

    def test_agents_know_run_state(self):
        self.assertIn("run.workings.HOMING: on", self.read("plugins/rp/agents/roleplay-gm.md"))
        self.assertIn("`run.*` entries", self.read("plugins/write/agents/continuity-reviewer.md"))
        self.assertIn("DAY SUMMARY", self.read("plugins/rp/agents/timeline-search.md"))
        self.assertIn("run:", self.read("plugins/rp/commands/start.md"))


class CliTests(unittest.TestCase):
    def test_check_prints_run_findings_events_and_notes(self):
        p = Project(self)
        turn = p.write("Work/rp/.gate/turn.md", turn_text(
            'currentDate: "Day 199 (Thursday)"\nscene.time: "06:00"\nrun.workings.HOMING: lifted',
            "Scene: Day 199, ~06:00, calm | Location: the kitchen | Present: Liss"))
        code, out, _err = run("check", "--project-dir", str(p.root), "--turn-file", str(turn), "--no-log")
        got = json.loads(out)
        self.assertEqual(code, 0)
        self.assertIn("run_bad_value", [f["check"] for f in got["lint"]["findings"]])
        self.assertEqual([n["key"] for n in got["lint"]["run_notes"]], ["run.clocks.vine-test"])
        self.assertEqual(got["route"]["next"], "fix")

    def test_ship_state_and_summary_commands(self):
        p = Project(self)
        turn = p.write("Work/rp/.gate/turn.md", turn_text('run.stats.POW: "53/60"'))
        beat_file = p.write("Work/rp/.gate/beat.md", "### Beat 4131: Day 196, ~19:05.\n\nAxe.\n")
        code, out, _err = run("ship", "--project-dir", str(p.root), "--turn-file", str(turn),
                              "--append-file", str(beat_file), "--beat", "4131")
        self.assertEqual((code, json.loads(out)["action"]), (0, "shipped"))
        code, out, _err = run("state", "--refresh", "--project-dir", str(p.root))
        self.assertEqual((code, json.loads(out)["action"]), (0, "refreshed"))
        code, out, _err = run("summary", "--project-dir", str(p.root))
        self.assertEqual(code, 0)
        self.assertIn("days", json.loads(out))
        code, _out, err = run("summary", "--project-dir", str(p.root), "--day", "196", "--publish", str(turn))
        self.assertEqual(code, 2)
        self.assertIn("ERROR", err)


if __name__ == "__main__":
    unittest.main()
