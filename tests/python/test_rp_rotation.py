"""Tests for fiction-day log rotation in plugins/rp/tools/rp_gate.py."""
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
spec = importlib.util.spec_from_file_location("rp_gate_rotation", SCRIPT)
assert spec is not None and spec.loader is not None
rp_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rp_gate)

LEGACY = "Work/rp/rp_session_2026-10-24.md"
LOG_DIR = "Work/rp/test/log"

LEGACY_FRONTMATTER = """---
campaign: "Test Run"
{extra}currentDate: "Day 196 (Monday)"
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
---
"""

LEGACY_HEADER = """
# RP Session 2026-10-24: Alder, Day 195 afternoon onward

Continues `Work/rp/rp_session_2026-10-17.md` (closed at B4099). State: `Work/rp/test/alder.md`
§ Run state. Beat numbering resumes at 4100.

**Tier:** live beats delegated (Tier 1, `roleplay-gm`). Register: plain sentences.

**GM line (standing, Keeper 2026-10-04):** no sexual content where he cannot refuse.

## What is on him

| # | Trigger | Effect |
|---|---|---|
| T30 | Fed by a hand | He kisses the feeding hand, once per feeding |

### RULING T16, fixed before any bell

- The trigger is the bell, not her.

## Open threads

- THE NORTH VAULT: a finger a day.
"""


def beat(n, day, extra=""):
    when = f"Day {day}, ~08:{n % 60:02d}" if isinstance(day, int) else day
    return (f"### Beat {n} PRE-COMMIT (GM, binding): LISS: \"SHED.\"\n"
            f"SOURCE_LOG (priced_stake_touched: false)\n\n"
            f"### Beat {n}: {when} (Monday). (Tier 1, roleplay-gm){extra}\n"
            f"**PC move (B{n}):** \"I go.\"\n\nLiss sets down the axe.\n\n")


def legacy_body():
    parts = ["## Day Plan: Day 195 (Sunday)\n\n| NPC | Goal |\n|---|---|\n\n"]
    parts += [beat(n, 195) for n in range(4100, 4125)]
    parts.append("## Day Plan: Day 196 (Monday)\n\n### Clocks\n\n- ~05:50 first light\n\n")
    parts.append(beat(4125, "Day 195, ~19:05 to Day 196, ~05:50"))
    parts += [beat(n, 196) for n in range(4126, 4131)]
    return "".join(parts)


def turn_text(delta="none", anchor_day=197):
    return (f"KEEPER_INPUT:\nI go.\n\nDRAFT:\nLiss sets down the axe.\n\n"
            f"Scene: Day {anchor_day}, ~06:00, calm | Location: the kitchen | Present: Liss\n\n"
            f"GM_SPAWN_LOG:\n- liss: NOT spawned\n\nSOURCE_LOG:\npriced_stake_touched: false\n\n"
            f"SCENE_STATE_DELTA:\n{delta}\n")


NEXT_DAY = turn_text('currentDate: "Day 197 (Tuesday)"\nscene.time: "06:00"')
SAME_DAY = turn_text("none", anchor_day=196)


class Project:
    def __init__(self, testcase, log_dir=True):
        self._tmp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "Work/markers").mkdir(parents=True)
        (self.root / "Work/rp").mkdir(parents=True)
        (self.root / "Work/rp/test").mkdir(parents=True)
        (self.root / "Work/rp/test/alder.md").write_text("# Alder\n\n## Run state\n\n**Closed: Day 196.**\n")
        extra = f'log_dir: "{LOG_DIR}"\n' if log_dir else ""
        self.legacy = self.root / LEGACY
        self.legacy.write_text(LEGACY_FRONTMATTER.format(extra=extra) + LEGACY_HEADER + legacy_body(),
                               encoding="utf-8")
        self.marker(f"session_file: {LEGACY}\nsession_opened: {LEGACY}\nsession_opened_line: 40\n")  # an older marker

    def marker(self, text):
        (self.root / "Work/markers/rp-active").write_text(text, encoding="utf-8")

    def marker_fields(self):
        text = (self.root / "Work/markers/rp-active").read_text(encoding="utf-8")
        return dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)

    def mode(self, text):
        path = self.root / ".claude/modes/rp.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def rotate(self, text=NEXT_DAY, session=None, **kw):
        session = session or rp_gate.resolve_session_file(self.root)["path"]
        kw.setdefault("today", "2026-10-26")
        return rp_gate.rotate(self.root, session, turn_text=text, **kw)

    def log_lines(self):
        log = self.root / "Work/rp/_validator_log.jsonl"
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    def files(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rp_gate.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def footers(text):
    return len(re.findall(r"<!-- rp-rotated-to: ", text))


DAY_197 = f"{LOG_DIR}/day-0197.md"


class FictionDayTests(unittest.TestCase):
    def test_days_as_the_logs_write_them(self):
        cases = {
            "Day 196 (Monday)": 196,
            "Day 195, ~19:05 to Day 196, ~05:50": 196,
            "Day 163→164, ~08:35 to ~08:50": 164,
            "Day 1 night → Day 2, ~05:50": 2,
            "retro Day 4 pm; Day 4 night; Day 5 ~06:00": 5,
            "Day 12 → 13:00": 12,  # a time after the arrow is not a day
            "Monday": None,
            "1889-10-03": None,
        }
        for text, day in cases.items():
            with self.subTest(text=text):
                self.assertEqual(rp_gate.fiction_day(text), day)

    def test_beat_headings(self):
        cases = {
            "### Beat 4123: Day 195, ~19:05 to Day 196, ~05:50 (Sunday). (Tier 1)": ("4123", 196),
            "### Beat 1 — Day 1, ~16:30, the yard (Tier 1 solo)": ("1", 1),
            "### Beat 96R: Day 31, ~19:57 to ~19:58, supper. (replaces struck B96)": ("96R", 31),
            "### Beat 2326 (REVISED 2, retcon of both landings above): Day 136, ~19:38": ("2326", 136),
            "### Beat 3086 (INSERT, before the hood in B3085: ~05:36 Monday). (Tier 1)": ("3086", None),
            "### Beat 2984: Day 163, ~19:20 to night (Wednesday). PRE-COMMIT TABLES (x): CASTING D164": ("2984", 163),
            "### Beat 2991: Day 164, ~09:41 to ~09:43 (Wednesday→Thursday D164). (Tier 1)": ("2991", 164),
            "### Beat 4145: Day 196, ~18:03 to ~19:00 (Monday). (/rp:turn gate: lint clean; Day 199)": ("4145", 196),
        }
        for line, (label, day) in cases.items():
            with self.subTest(line=line):
                got = rp_gate.parse_beat_heading(line)
                self.assertEqual((got["beat"], got["day"]), (label, day))
        for line in ("### Beat 4100 PRE-COMMIT (GM, binding): MIRA", "### Beat 3466 SPEC (GM, binding)",
                     "### Beat 3705 AMENDMENT (before posting)", "## Beats", "Beat 4100: Day 195"):
            with self.subTest(line=line):
                self.assertIsNone(rp_gate.parse_beat_heading(line))


class RotationTests(unittest.TestCase):
    def test_a_later_day_rotates_into_a_new_day_file(self):
        p = Project(self)
        before = p.read(LEGACY)
        got = p.rotate(beat="4131")
        self.assertEqual(got["action"], "rotated")
        self.assertEqual(got["path"], DAY_197)
        self.assertEqual(got["previous"], LEGACY)
        self.assertEqual(p.marker_fields()["session_file"], DAY_197)
        self.assertEqual(p.marker_fields()["session_opened"], LEGACY)  # the session still opened there
        old = p.read(LEGACY)
        self.assertTrue(old.startswith(before))  # nothing before the footer changed
        self.assertEqual(footers(old), 1)
        self.assertIn(f"<!-- rp-rotated-to: {DAY_197} -->", old)
        self.assertIn("Beat 4131", old[len(before):])
        self.assertEqual([x["path"] for x in p.log_lines()], ["rotate"])

    def test_new_file_inherits_the_frontmatter_and_header(self):
        p = Project(self)
        p.rotate()
        new = p.read(DAY_197)
        data = rp_gate.read_frontmatter(p.root / DAY_197)
        self.assertEqual(data["log_day"], 197)
        self.assertEqual(data["continues"], LEGACY)
        self.assertEqual(data["log_dir"], LOG_DIR)
        self.assertEqual(data["opened"], "2026-10-26")
        self.assertEqual(data["currentDate"], "Day 197 (Tuesday)")
        self.assertEqual(data["startDate"], "Day 195 (Sunday)")
        self.assertEqual(data["scene"]["participants"][1]["profile_file"], "Lore/World/Characters/Liss.md")
        self.assertNotIn("status", data)
        for line in LEGACY_HEADER.splitlines()[3:]:  # the legacy title is replaced, the rest copied as written
            self.assertIn(line + "\n", new)
        self.assertNotIn("# RP Session 2026-10-24", new)
        self.assertIn(f"Continues `{LEGACY}`", new)
        self.assertNotIn("### Beat", new)  # no beats yet
        state = rp_gate.live_state(p.root, DAY_197, today="2026-10-26")
        self.assertEqual(state["run_state"]["path"], "Work/rp/test/alder.md")  # the copied State: line
        self.assertIsNotNone(state["header"])

    def test_new_file_carries_the_beat_day(self):
        # R10: the delta is applied after the append; the frontmatter must already be on the new day.
        p = Project(self)
        p.rotate(text=turn_text("none", anchor_day=197), day=197)
        self.assertEqual(rp_gate.read_frontmatter(p.root / DAY_197)["currentDate"], "Day 197")
        again = p.rotate(text=SAME_DAY.replace("Day 196", "Day 197"))
        self.assertEqual(again["action"], "none")

    def test_the_heading_alone_does_not_move_the_day(self):
        # A slip in the heading must not rotate; the gated delta does (the lint ties it to the anchor).
        p = Project(self)
        got = p.rotate(text=turn_text("none", anchor_day=196), heading="### Beat 4131: Day 197, ~06:00 (Tuesday).")
        self.assertEqual((got["action"], got["path"]), ("none", LEGACY))
        self.assertTrue(any("heading" in w for w in got["warnings"]))

    def test_the_generated_block_never_accumulates(self):
        p = Project(self)
        p.rotate()
        p.rotate(text=turn_text('currentDate: "Day 198 (Wednesday)"', anchor_day=198))
        new = p.read(f"{LOG_DIR}/day-0198.md")
        self.assertEqual(new.count("<!-- rp-day-file"), 1)
        self.assertEqual(new.count("Continues `"), 2)  # the generated one and the copied legacy sentence
        self.assertIn(f"Continues `{DAY_197}`", new)
        self.assertIn("| T30 | Fed by a hand |", new)
        self.assertEqual(rp_gate.read_frontmatter(p.root / f"{LOG_DIR}/day-0198.md")["continues"], DAY_197)

    def test_same_day_does_nothing(self):
        p = Project(self)
        got = p.rotate(text=SAME_DAY)
        self.assertEqual((got["action"], got["path"]), ("none", LEGACY))
        self.assertFalse((p.root / LOG_DIR).exists())

    def test_step_back_stays_and_is_flagged(self):
        p = Project(self)
        p.rotate()
        got = p.rotate(text=turn_text('currentDate: "Day 195 (Sunday)"', anchor_day=195), beat="4132")
        self.assertEqual((got["action"], got["path"]), ("step_back", DAY_197))
        self.assertIn("D195", got["flag"])
        self.assertIn("D197", got["flag"])
        self.assertEqual(footers(p.read(DAY_197)), 0)
        self.assertFalse((p.root / f"{LOG_DIR}/day-0195.md").exists())
        self.assertTrue(p.log_lines()[-1]["step_back"])

    def test_a_legacy_log_with_stale_frontmatter_goes_by_its_last_beat(self):
        # The 10-17 log's frontmatter still says Day 176; its beats run to Day 195.
        p = Project(self)
        p.legacy.write_text(p.read(LEGACY).replace('"Day 196 (Monday)"', '"Day 176 (Tuesday)"'), encoding="utf-8")
        got = p.rotate(text=turn_text("none", anchor_day=196))
        self.assertEqual((got["action"], got["file_day"], got["day"]), ("none", 196, 196))
        self.assertEqual(rp_gate.continuity(p.root, LEGACY, 20)["day"], 196)
        self.assertEqual(p.rotate()["path"], DAY_197)

    def test_no_fiction_day_keeps_todays_behaviour(self):
        p = Project(self)
        p.legacy.write_text(re.sub(r"Day \d+", "Monday", p.read(LEGACY)), encoding="utf-8")
        got = p.rotate(text=turn_text("none", anchor_day=197).replace("Day 197, ", ""))
        self.assertEqual(got["action"], "none")
        self.assertEqual(got["path"], LEGACY)
        self.assertIn("no fiction day", got["reason"])

    def test_no_log_dir_warns_and_does_not_rotate(self):
        p = Project(self, log_dir=False)
        got = p.rotate()
        self.assertEqual(got["action"], "none")
        self.assertTrue(any("log_dir" in w for w in got["warnings"]))
        self.assertEqual(footers(p.read(LEGACY)), 0)

    def test_log_dir_must_stay_under_the_session_directory(self):
        p = Project(self)
        p.legacy.write_text(p.read(LEGACY).replace(LOG_DIR, "../../outside"), encoding="utf-8")
        got = p.rotate()
        self.assertEqual(got["action"], "none")
        self.assertTrue(any("log_dir" in w for w in got["warnings"]))
        self.assertFalse((p.root.parent / "outside").exists())

    def test_rotation_off_in_the_mode_file(self):
        p = Project(self)
        p.mode("sessionLog:\n  rotation: off\n")
        self.assertEqual(p.rotate()["action"], "none")

    def test_heading_disagreeing_with_the_gated_day_warns(self):
        p = Project(self)
        got = p.rotate(heading="### Beat 4131: Day 199, ~06:00 (Tuesday).")
        self.assertEqual(got["path"], DAY_197)  # the gated delta, not the heading
        self.assertTrue(any("heading" in w for w in got["warnings"]))

    def test_cli_rotate(self):
        p = Project(self)
        tf = p.root / "Work/rp/.gate/turn.md"
        tf.parent.mkdir(parents=True)
        tf.write_text(NEXT_DAY, encoding="utf-8")
        code, out, err = run("rotate", "--project-dir", str(p.root), "--session-file", LEGACY,
                             "--turn-file", str(tf), "--beat", "4131")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["path"], DAY_197)


class RaceTests(unittest.TestCase):
    """Each risky ordering from the race model."""

    def test_order_is_publish_swap_footer(self):
        # R7, R8
        p = Project(self)
        calls = []
        for name in ("_publish", "_swap_marker", "_append_footer"):
            original = getattr(rp_gate, name)
            def wrapped(*a, _name=name, _orig=original, **k):
                calls.append(_name)
                return _orig(*a, **k)
            self.enterContext(mock.patch.object(rp_gate, name, wrapped))
        p.rotate()
        self.assertEqual(calls, ["_publish", "_swap_marker", "_append_footer"])

    def test_marker_always_names_a_complete_file(self):
        # R5, R6: a hook may read between any two steps.
        p = Project(self)
        seen = []

        def check(step):
            marker = p.root / "Work/markers/rp-active"
            self.assertTrue(marker.is_file(), step)
            named = p.marker_fields()["session_file"]
            self.assertTrue(rp_gate.read_frontmatter(p.root / named), step)
            self.assertIsNotNone(rp_gate.live_state(p.root, named, today="2026-10-26")["header"], step)
            self.assertEqual(rp_gate.rotation_footer(p.root / named), None, step)  # never a closed file
            seen.append(step)

        for name in ("_publish", "_swap_marker", "_append_footer"):
            original = getattr(rp_gate, name)
            def wrapped(*a, _name=name, _orig=original, **k):
                check("before " + _name)
                result = _orig(*a, **k)
                check("after " + _name)
                return result
            self.enterContext(mock.patch.object(rp_gate, name, wrapped))
        p.rotate()
        self.assertEqual(len(seen), 6)

    def test_crash_before_the_marker_swap_is_adopted_on_rerun(self):
        # R1
        p = Project(self)
        with mock.patch.object(rp_gate, "_swap_marker", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                p.rotate()
        self.assertTrue((p.root / DAY_197).is_file())
        self.assertEqual(p.marker_fields()["session_file"], LEGACY)
        self.assertEqual(footers(p.read(LEGACY)), 0)
        first = p.read(DAY_197)
        got = p.rotate(session=LEGACY)
        self.assertEqual((got["action"], got["path"]), ("rotated", DAY_197))
        self.assertEqual(p.read(DAY_197), first)  # adopted, not rewritten
        self.assertEqual(p.marker_fields()["session_file"], DAY_197)
        self.assertEqual(footers(p.read(LEGACY)), 1)
        self.assertEqual([f for f in p.files() if "day-0197" in f], [DAY_197])

    def test_fallback_prefers_the_chain_head_after_an_interrupted_rotation(self):
        # R1 with a broken marker: both files are unfooted.
        p = Project(self)
        with mock.patch.object(rp_gate, "_swap_marker", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                p.rotate()
        p.marker("")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-26")
        self.assertEqual((got["path"], got["source"]), (DAY_197, "latest"))

    def test_crash_before_the_footer_is_repaired_once(self):
        # R2
        p = Project(self)
        with mock.patch.object(rp_gate, "_append_footer", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                p.rotate()
        self.assertEqual(p.marker_fields()["session_file"], DAY_197)
        self.assertEqual(footers(p.read(LEGACY)), 0)
        code, out, err = run("session", "--project-dir", str(p.root))
        self.assertEqual(json.loads(out)["path"], DAY_197)
        self.assertIn("interrupted", err)
        p.marker("")
        self.assertEqual(rp_gate.resolve_session_file(p.root, today="2026-10-26")["path"], DAY_197)
        p.marker(f"session_file: {DAY_197}\n")
        got = p.rotate(text=SAME_DAY.replace("Day 196", "Day 197"))
        self.assertEqual((got["action"], got["path"]), ("none", DAY_197))
        self.assertEqual(got["repaired"], LEGACY)
        self.assertEqual(footers(p.read(LEGACY)), 1)
        p.rotate(text=SAME_DAY.replace("Day 196", "Day 197"))
        self.assertEqual(footers(p.read(LEGACY)), 1)

    def test_rotate_without_a_day_only_repairs(self):
        # R11: /rp:end runs rotate with no turn before writing its END block.
        p = Project(self)
        with mock.patch.object(rp_gate, "_append_footer", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                p.rotate()
        got = rp_gate.rotate(p.root, DAY_197, today="2026-10-26")
        self.assertEqual((got["action"], got["repaired"]), ("none", LEGACY))
        self.assertEqual(footers(p.read(LEGACY)), 1)

    def test_rotation_is_idempotent(self):
        # R3, R14 sequentially
        p = Project(self)
        p.rotate()
        new, old = p.read(DAY_197), p.read(LEGACY)
        got = p.rotate()
        self.assertEqual((got["action"], got["path"]), ("none", DAY_197))
        self.assertEqual((p.read(DAY_197), p.read(LEGACY)), (new, old))
        again = p.rotate(session=LEGACY)  # a stale caller still naming the old file
        self.assertEqual(again["path"], DAY_197)
        self.assertEqual(footers(p.read(LEGACY)), 1)

    def test_crash_while_publishing_leaves_no_partial_file(self):
        # R4
        p = Project(self)
        with mock.patch.object(rp_gate.os, "link", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                p.rotate()
        self.assertFalse((p.root / DAY_197).exists())
        self.assertEqual(p.marker_fields()["session_file"], LEGACY)
        self.assertEqual(rp_gate.resolve_session_file(p.root, today="2026-10-26")["path"], LEGACY)
        got = p.rotate()
        self.assertEqual(got["action"], "rotated")
        self.assertEqual(os.listdir(p.root / LOG_DIR), ["day-0197.md"])  # the stale temp is gone

    def test_an_existing_foreign_or_closed_day_file_is_refused(self):
        # R9
        for body in ('---\nlog_day: 197\ncontinues: "Work/rp/other.md"\n---\n\n# Other run\n',
                     f'---\nlog_day: 197\ncontinues: "{LEGACY}"\n---\n\n# Closed\n\n<!-- rp-rotated-to: x.md -->\n'):
            with self.subTest(body=body[:40]):
                p = Project(self)
                (p.root / LOG_DIR).mkdir(parents=True)
                (p.root / DAY_197).write_text(body, encoding="utf-8")
                marker, old = p.read("Work/markers/rp-active"), p.read(LEGACY)
                got = p.rotate()
                self.assertEqual((got["action"], got["path"]), ("refused", LEGACY))
                self.assertEqual((p.read("Work/markers/rp-active"), p.read(LEGACY), p.read(DAY_197)),
                                 (marker, old, body))


class StartAndEndTests(unittest.TestCase):
    def test_start_on_the_same_day_continues_the_day_file(self):
        p = Project(self)
        p.rotate()
        (p.root / "Work/markers/rp-active").unlink()  # /rp:end removed it
        session = rp_gate.resolve_session_file(p.root, today="2026-10-27")["path"]
        self.assertEqual(session, DAY_197)
        lines = len(p.read(DAY_197).splitlines())
        got = rp_gate.rotate(p.root, session, day=197, start=True, today="2026-10-27")
        self.assertEqual((got["action"], got["path"]), ("none", DAY_197))
        fields = p.marker_fields()
        self.assertEqual((fields["session_file"], fields["session_opened"]), (DAY_197, DAY_197))
        code, out, _err = run("session", "--project-dir", str(p.root))
        self.assertEqual(json.loads(out)["session_opened_line"], lines + 1)

    def test_start_on_a_later_day_rotates_and_opens_there(self):
        p = Project(self)
        p.rotate()
        (p.root / "Work/markers/rp-active").unlink()
        got = rp_gate.rotate(p.root, DAY_197, day=199, start=True, today="2026-10-28")
        self.assertEqual((got["action"], got["path"]), ("rotated", f"{LOG_DIR}/day-0199.md"))
        self.assertEqual(p.marker_fields()["session_opened"], f"{LOG_DIR}/day-0199.md")
        self.assertEqual(rp_gate.read_frontmatter(p.root / f"{LOG_DIR}/day-0199.md")["currentDate"], "Day 199")

    def test_start_on_a_legacy_campaign_writes_the_marker_only(self):
        p = Project(self, log_dir=False)
        (p.root / "Work/markers/rp-active").unlink()
        got = rp_gate.rotate(p.root, LEGACY, start=True, today="2026-10-26")
        self.assertEqual((got["action"], got["path"]), ("none", LEGACY))
        self.assertEqual(p.marker_fields()["session_file"], LEGACY)

    def test_session_files_span_the_rotations(self):
        p = Project(self)
        p.rotate()
        p.rotate(text=turn_text('currentDate: "Day 198"', anchor_day=198))
        code, out, _err = run("session", "--project-dir", str(p.root))
        got = json.loads(out)
        self.assertEqual(got["session_files"], [LEGACY, DAY_197, f"{LOG_DIR}/day-0198.md"])
        self.assertEqual(got["session_opened_line"], 40)

    def test_session_files_without_an_opening_record(self):
        # Only the old /rp:start wrote no session_opened, and it only ever opened a legacy log.
        p = Project(self)
        p.rotate()
        p.marker(f"session_file: {DAY_197}\n")
        code, out, _err = run("session", "--project-dir", str(p.root))
        self.assertEqual(json.loads(out)["session_files"], [LEGACY, DAY_197])
        (p.root / DAY_197).write_text(p.read(DAY_197).replace(f'continues: "{LEGACY}"\n', ""), encoding="utf-8")
        code, out, _err = run("session", "--project-dir", str(p.root))
        self.assertEqual(json.loads(out)["session_files"], [DAY_197])  # a day file that continues nothing

    def test_fallback_takes_the_chain_head(self):
        # R13: day files ignore status (only the footer closes them); legacy files keep status: closed.
        p = Project(self)
        p.rotate()
        p.rotate(text=turn_text('currentDate: "Day 198"', anchor_day=198))
        head = f"{LOG_DIR}/day-0198.md"
        (p.root / head).write_text(p.read(head).replace("---\n", "---\nstatus: closed\n", 1), encoding="utf-8")
        other = p.root / "Work/rp/rp_session_2026-10-01.md"
        other.write_text("---\ncurrentDate: \"Day 3\"\n---\n", encoding="utf-8")
        os.utime(other, (1, 1))
        p.marker("")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-28")
        self.assertEqual(got["path"], head)


class ContinuityTests(unittest.TestCase):
    def window(self, p, session):
        code, out, err = run("session", "--project-dir", str(p.root))
        self.assertEqual(code, 0, err)
        got = json.loads(out)
        self.assertEqual(got["path"], session)
        return got["continuity"]

    def heading_line(self, p, rel, text):
        return next(i for i, line in enumerate(p.read(rel).splitlines(), 1) if line.startswith(text))

    def append(self, p, rel, text):
        with open(p.root / rel, "a", encoding="utf-8") as fh:
            fh.write(text)

    def test_a_short_day_tops_up_from_the_day_before(self):
        # The last 20 beats, wherever they fall: today's 2, then the 18 before them.
        p = Project(self)
        p.rotate()
        self.append(p, DAY_197, "## Day Plan: Day 197 (Tuesday)\n\n" + beat(4131, 197) + beat(4132, 197))
        got = self.window(p, DAY_197)
        self.assertEqual(got["day"], 197)
        self.assertEqual(got["current"]["path"], DAY_197)
        self.assertEqual(got["current"]["start"], self.heading_line(p, DAY_197, "## Day Plan: Day 197"))
        self.assertEqual((got["current"]["beats"], got["current"]["whole_day"]), (2, True))
        lines = p.read(DAY_197).splitlines()
        self.assertEqual(got["current"]["end"], max(i for i, line in enumerate(lines, 1) if line.strip()))
        [prev] = got["previous"]
        self.assertEqual((prev["path"], prev["beats"]), (LEGACY, 18))
        # B4113-B4130 (the unit of B4113 opens with its PRE-COMMIT heading)
        self.assertEqual((prev["first_beat"], prev["last_beat"]), ("4113", "4130"))
        self.assertEqual(prev["start"], self.heading_line(p, LEGACY, "### Beat 4113 PRE-COMMIT"))
        old = p.read(LEGACY).splitlines()
        self.assertFalse(any("rp-rotated-to" in line for line in old[prev["start"] - 1:prev["end"]]))
        self.assertTrue(old[prev["end"] - 1].strip())

    def test_a_long_day_reads_only_its_last_beats(self):
        # Never the whole current day: with 25 beats today, the read is its last 20 and nothing before.
        p = Project(self)
        p.rotate()
        self.append(p, DAY_197, "## Day Plan: Day 197 (Tuesday)\n\n" + "".join(beat(n, 197) for n in range(4131, 4156)))
        got = self.window(p, DAY_197)
        cur = got["current"]
        self.assertEqual((cur["path"], cur["beats"], cur["whole_day"]), (DAY_197, 20, False))
        self.assertEqual((cur["first_beat"], cur["last_beat"]), ("4136", "4155"))
        self.assertEqual(cur["start"], self.heading_line(p, DAY_197, "### Beat 4136 PRE-COMMIT"))
        self.assertLess(self.heading_line(p, DAY_197, "## Day Plan: Day 197"), cur["start"])
        self.assertEqual(got["previous"], [])

    def test_the_read_walks_back_past_a_short_day(self):
        # Today 2 beats, yesterday 3: the other 15 come from the file before that.
        p = Project(self)
        p.rotate()
        self.append(p, DAY_197, "## Day Plan: Day 197 (Tuesday)\n\n" + "".join(beat(n, 197) for n in range(4131, 4134)))
        p.rotate(turn_text('currentDate: "Day 198 (Wednesday)"', anchor_day=198))
        day_198 = f"{LOG_DIR}/day-0198.md"
        self.append(p, day_198, "## Day Plan: Day 198 (Wednesday)\n\n" + beat(4134, 198) + beat(4135, 198))
        got = self.window(p, day_198)
        self.assertEqual(got["current"]["beats"], 2)
        self.assertEqual([(r["path"], r["beats"], r["first_beat"], r["last_beat"]) for r in got["previous"]],
                         [(LEGACY, 15, "4116", "4130"), (DAY_197, 3, "4131", "4133")])  # oldest first
        footer = p.read(DAY_197).splitlines()[got["previous"][1]["end"]:]
        self.assertTrue(any("rp-rotated-to" in line for line in footer))  # the footer stays out of the read

    def test_a_legacy_day_with_enough_beats_reads_only_them(self):
        p = Project(self)
        p.mode("sessionLog:\n  previousBeats: 3\n")
        got = self.window(p, LEGACY)
        cur = got["current"]
        self.assertEqual((cur["beats"], cur["first_beat"], cur["last_beat"], cur["whole_day"]),
                         (3, "4128", "4130", False))
        self.assertEqual(cur["start"], self.heading_line(p, LEGACY, "### Beat 4128 PRE-COMMIT"))
        self.assertEqual(got["previous"], [])

    def test_zero_recent_beats_reads_none(self):
        p = Project(self)
        p.mode("sessionLog:\n  previousBeats: 0\n")
        got = self.window(p, LEGACY)
        self.assertEqual((got["beats"], got["current"], got["previous"]), (0, None, []))

    def test_the_count_comes_from_the_mode_file(self):
        p = Project(self)
        p.mode("turnGate:\n  review: every\nsessionLog:\n  previousBeats: 5\n")
        p.rotate()
        [prev] = self.window(p, DAY_197)["previous"]
        self.assertEqual((prev["beats"], prev["first_beat"]), (5, "4126"))

    def test_old_config_keeps_the_default(self):
        p = Project(self)
        p.mode("turnGate:\n  review: every\n")
        self.assertEqual(rp_gate.load_config(p.root)["log"], {"rotation": "day", "previousBeats": 20})
        p.mode("sessionLog:\n  previousBeats: 9000\n  rotation: weekly\n")
        config = rp_gate.load_config(p.root)
        self.assertEqual(config["log"], {"rotation": "day", "previousBeats": 20})
        self.assertEqual(len([w for w in config["warnings"] if "sessionLog" in w]), 2)

    def test_legacy_multi_day_file_reads_the_current_day_and_the_beats_before_it(self):
        p = Project(self)
        got = self.window(p, LEGACY)
        self.assertEqual(got["day"], 196)
        # The D196 Day Plan precedes the night beat that ends on D196: the day starts there.
        self.assertEqual(got["current"]["start"], self.heading_line(p, LEGACY, "## Day Plan: Day 196"))
        self.assertEqual(got["current"]["beats"], 6)  # B4125 (the night beat) and B4126-B4130
        [prev] = got["previous"]
        self.assertEqual((prev["path"], prev["beats"], prev["first_beat"], prev["last_beat"]),
                         (LEGACY, 14, "4111", "4124"))
        between = p.read(LEGACY).splitlines()[prev["end"]:got["current"]["start"] - 1]
        self.assertTrue(between and not any(line.strip() for line in between))  # only the blank separator

    def test_a_fresh_day_file_has_no_current_beats(self):
        p = Project(self)
        p.rotate()
        got = self.window(p, DAY_197)
        self.assertIsNone(got["current"])
        [prev] = got["previous"]
        self.assertEqual((prev["path"], prev["beats"], prev["first_beat"]), (LEGACY, 20, "4111"))


class LintDayTests(unittest.TestCase):
    def check(self, delta, anchor_day):
        p = Project(self)
        scene = rp_gate.load_scene(p.root, LEGACY)
        text = turn_text(delta, anchor_day=anchor_day)
        result = rp_gate.lint(rp_gate.parse_turn(text), scene, rp_gate.load_config(p.root), p.root)
        return [f["check"] for f in result["findings"] if f["check"].startswith("delta_day")]

    def test_anchor_day_against_the_delta(self):
        self.assertEqual(self.check("none", 196), [])
        self.assertEqual(self.check("none", 197), ["delta_day_missing"])
        self.assertEqual(self.check('currentDate: "Day 197 (Tuesday)"', 197), [])
        self.assertEqual(self.check('currentDate: "Day 198"', 197), ["delta_day_mismatch"])

    def test_only_a_leading_day_in_the_scene_field_counts(self):
        p = Project(self)
        scene = rp_gate.load_scene(p.root, LEGACY)
        text = turn_text("none").replace("Scene: Day 197, ~06:00, calm", "Scene: the siege, Day 3 of it, ~06:00")
        result = rp_gate.lint(rp_gate.parse_turn(text), scene, rp_gate.load_config(p.root), p.root)
        self.assertFalse([f for f in result["findings"] if f["check"].startswith("delta_day")])

    def test_a_non_day_current_date_is_no_day(self):
        p = Project(self)
        got = p.rotate(text=turn_text("currentDate: true", anchor_day=196))
        self.assertEqual((got["action"], got["day"]), ("none", 196))  # falls through to the frontmatter


class SplitTests(unittest.TestCase):
    LOG = (LEGACY_FRONTMATTER.format(extra="") + LEGACY_HEADER
           + "## Day Plan: Day 195 (Sunday)\n\n"
           + beat(4100, 195) + beat(4101, 195)
           + "## Day Plan: Day 196 (Monday)\n\n"
           + beat(4102, "Day 195, ~22:00 to Day 196, ~05:50")
           + beat(4103, 196)
           + "### Beat 4104 (INSERT, before the hood in B4103: ~06:00 Monday). (Tier 1)\n\nAn insert.\n\n"
           + beat(4105, "Day 195, ~08:00")
           + beat(4106, "Day 196→197, ~09:00")
           + "## PAUSE (2026-10-25)\n\n"
           + beat(4107, 199)
           + "## END (2026-10-26, /rp:end)\n\nCLOSED AT: Day 199.\n")

    def plan(self):
        p = Project(self)
        p.legacy.write_text(self.LOG, encoding="utf-8")
        return p, rp_gate.plan_split(p.root, [LEGACY], LOG_DIR)

    def test_cuts_at_fiction_day_boundaries(self):
        p, plan = self.plan()
        lines = self.LOG.splitlines()
        files = {f["day"]: f for f in plan["files"]}
        self.assertEqual(sorted(files), [195, 196, 197, 199])
        parts = [part for f in plan["files"] for part in f["parts"]]
        self.assertEqual(parts[0]["start"], 1)  # the first day keeps the frontmatter and header
        self.assertEqual(sum(part["end"] - part["start"] + 1 for part in parts), len(lines))
        d196 = files[196]["parts"][0]
        self.assertEqual(lines[d196["start"] - 1], "## Day Plan: Day 196 (Monday)")  # the plan goes with its day
        d197 = files[197]["parts"][0]
        self.assertTrue(lines[d197["start"] - 1].startswith("### Beat 4106 PRE-COMMIT"))  # the working goes with its beat
        last = files[199]["parts"][0]
        self.assertTrue(lines[last["start"] - 1].startswith("### Beat 4107 PRE-COMMIT"))  # the PAUSE stays behind
        self.assertIn("## PAUSE (2026-10-25)", lines[d197["start"] - 1:d197["end"]])
        self.assertIn("## END (2026-10-26, /rp:end)", lines[last["start"] - 1:last["end"]])  # the END stays with its day
        self.assertEqual(last["end"], len(lines))
        self.assertEqual(files[196]["path"], f"{LOG_DIR}/day-0196.md")

    def test_reports_step_backs_and_beats_without_a_day(self):
        _p, plan = self.plan()
        self.assertEqual([(s["beat"], s["day"], s["file_day"]) for s in plan["step_backs"]], [("4105", 195, 196)])
        self.assertEqual([n["beat"] for n in plan["no_day"]], ["4104"])

    def test_remap_is_exact_inside_a_part(self):
        p, plan = self.plan()
        lines = self.LOG.splitlines()
        target = next(i for i, line in enumerate(lines, 1) if line.startswith("### Beat 4103:"))
        got = rp_gate.remap_citation(plan, LEGACY, target)
        self.assertTrue(got["exact"])
        self.assertEqual(got["path"], f"{LOG_DIR}/day-0196.md")
        self.assertEqual(got["text"], lines[target - 1])
        crossing = rp_gate.remap_citation(plan, LEGACY, target, target + 40)
        self.assertFalse(crossing["exact"])
        self.assertFalse(rp_gate.remap_citation(plan, LEGACY, len(lines) + 5)["exact"])

    def test_citation_forms_the_vault_uses(self):
        p, plan = self.plan()
        target = next(i for i, line in enumerate(self.LOG.splitlines(), 1) if line.startswith("### Beat 4103:"))
        notes = p.root / "Work/rp/test/rulings.md"
        notes.write_text("\n".join([
            f"- rp_session_2026-10-24.md:{target}", f"- 10-24:{target}-{target + 1}", f"- 10-24 log:{target}",
            f"- 10-24 log l.{target}", f"- `rp_session_2026-10-24.md` line {target}", f"- 10-24 lines {target}-{target + 1}",
            "- 10-24:99999", "- 2026-10-24:5 is a date", "- 10-17:12 names no planned log",
        ]) + "\n", encoding="utf-8")
        (p.root / "Work/rp/_validator_log.jsonl").write_text('{"date": "2026-10-24T10:07:00-07:00"}\n')
        got = rp_gate.scan_citations(p.root, plan, ["Work/rp"])
        self.assertEqual((got["total"], got["exact"]), (7, 6))
        self.assertEqual(got["reasons"], {"outside the log": 1})

    def test_dry_run_writes_nothing(self):
        p = Project(self)
        p.legacy.write_text(self.LOG, encoding="utf-8")
        before = {f: p.read(f) for f in p.files()}
        code, out, err = run("split", "--project-dir", str(p.root), "--log", LEGACY, "--log-dir", LOG_DIR)
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(json.loads(out)), sorted(["files", "step_backs", "no_day", "day_plans", "logs"]))
        self.assertEqual({f: p.read(f) for f in p.files()}, before)


def set_frontmatter(p, rel, key, value):
    text = p.read(rel)
    text = re.sub(r"(?m)^%s:.*$" % key, f"{key}: {value}", text, count=1)
    (p.root / rel).write_text(text, encoding="utf-8")


class ReviewFindingTests(unittest.TestCase):
    """Cases from the cold review of the first pass; each failed there."""

    def test_fallback_ignores_older_open_legacy_logs(self):
        # None of the vault's legacy logs carries status: closed; a touched old one must not win.
        p = Project(self)
        old = p.root / "Work/rp/rp_session_2026-10-11.md"
        old.write_text(LEGACY_FRONTMATTER.format(extra="") + "\n### Beat 1: Day 3, ~08:00\n", encoding="utf-8")
        p.rotate()
        os.utime(old)  # a sync or a checkout touches it
        p.marker("")
        self.assertEqual(rp_gate.resolve_session_file(p.root, today="2026-10-26")["path"], DAY_197)

    def test_fallback_ignores_a_copy_outside_its_log_dir(self):
        p = Project(self)
        p.rotate()
        backup = p.root / "Work/rp/backup/day-0197.md"
        backup.parent.mkdir(parents=True)
        backup.write_text(p.read(DAY_197), encoding="utf-8")
        p.marker("")
        self.assertEqual(rp_gate.resolve_session_file(p.root, today="2026-10-26")["path"], DAY_197)

    def test_a_flashback_in_a_legacy_log_does_not_lower_its_day(self):
        p = Project(self)
        flash = p.rotate(text=turn_text('currentDate: "Day 150"', anchor_day=150))
        self.assertEqual(flash["action"], "step_back")
        set_frontmatter(p, LEGACY, "currentDate", '"Day 150"')  # the delta applied
        with open(p.legacy, "a", encoding="utf-8") as fh:
            fh.write(beat(4131, 150))
        back = p.rotate(text=turn_text('currentDate: "Day 196 (Monday)"', anchor_day=196))
        self.assertEqual((back["action"], back["path"]), ("none", LEGACY))
        self.assertEqual(rp_gate.continuity(p.root, LEGACY, 20)["day"], 196)

    def test_an_accepted_surrender_that_moved_the_day_rotates_on_the_next_beat(self):
        for start in (LEGACY, DAY_197):
            with self.subTest(file=start):
                p = Project(self)
                if start == DAY_197:
                    p.rotate()
                set_frontmatter(p, start, "currentDate", '"Day 198"')  # the surrendered draft's delta
                got = p.rotate(text=turn_text("none", anchor_day=198))
                self.assertEqual((got["action"], got["path"]), ("rotated", f"{LOG_DIR}/day-0198.md"))

    def test_a_day_move_the_anchor_does_not_confirm_is_reviewed(self):
        p = Project(self)
        scene = rp_gate.load_scene(p.root, LEGACY)
        def checks(text):
            result = rp_gate.lint(rp_gate.parse_turn(text), scene, rp_gate.load_config(p.root), p.root)
            return {f["check"]: f["fix"] for f in result["findings"] if f["check"].startswith("delta_day")}
        unanchored = turn_text('currentDate: "Day 197"').replace("Scene: Day 197, ~06:00, calm", "Scene: dawn, ~06:00")
        self.assertEqual(checks(unanchored), {"delta_day_unanchored": "review"})
        self.assertEqual(checks(turn_text('currentDate: "Day 1970"', anchor_day=1970)), {"delta_day_jump": "review"})
        self.assertEqual(checks(turn_text('currentDate: "Day 200"', anchor_day=200)), {})  # four days, as the logs do
        self.assertEqual(checks(turn_text("currentDate: 197", anchor_day=197)), {"delta_day_mismatch": "edit"})

    def test_the_anchor_day_is_the_day_it_ends_on(self):
        p = Project(self)
        scene = rp_gate.load_scene(p.root, LEGACY)
        text = turn_text('currentDate: "Day 197"').replace("Scene: Day 197, ~06:00", "Scene: Day 196 night → Day 197, ~05:50")
        result = rp_gate.lint(rp_gate.parse_turn(text), scene, rp_gate.load_config(p.root), p.root)
        self.assertFalse([f for f in result["findings"] if f["check"].startswith("delta_day")])

    def test_a_number_is_not_a_fiction_day(self):
        p = Project(self)
        got = p.rotate(text=turn_text("currentDate: 198", anchor_day=196))
        self.assertEqual((got["action"], got["path"]), ("none", LEGACY))

    def test_an_orphan_from_a_crash_before_the_swap_is_set_aside(self):
        # R1, then play stays on the same day and the session ends.
        p = Project(self)
        with mock.patch.object(rp_gate, "_swap_marker", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                p.rotate()
        got = p.rotate(text=SAME_DAY)
        self.assertEqual((got["action"], got["path"]), ("none", LEGACY))
        self.assertEqual(got["set_aside"], [DAY_197])
        self.assertFalse((p.root / DAY_197).exists())
        self.assertTrue(list((p.root / LOG_DIR).glob(".day-0197.md.orphan-*")))
        p.marker("")
        self.assertEqual(rp_gate.resolve_session_file(p.root, today="2026-10-26")["path"], LEGACY)
        self.assertEqual(p.rotate()["path"], DAY_197)  # the next day still rotates cleanly

    def test_a_day_file_with_a_body_is_adopted_never_rebuilt(self):
        p = Project(self)
        with mock.patch.object(rp_gate, "_swap_marker", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n- the felling\n")
        p.rotate()
        self.assertIn("## Day Plan: Day 197 (Tuesday)", p.read(DAY_197))

    def test_the_session_start_survives_frontmatter_growth(self):
        p = Project(self)
        (p.root / "Work/markers/rp-active").unlink()
        rp_gate.rotate(p.root, LEGACY, start=True, today="2026-10-26")
        before = len(p.read(LEGACY).splitlines())  # the opening comment is the last line
        p.legacy.write_text(p.read(LEGACY).replace("  mood:", "    - name: \"Mira\"\n      role: \"NPC\"\n  mood:", 1),
                            encoding="utf-8")
        code, out, _err = run("session", "--project-dir", str(p.root))
        self.assertEqual(json.loads(out)["session_opened_line"], before + 2)

    def test_the_step_back_flag_keeps_the_heading_a_beat(self):
        p = Project(self)
        flag = p.rotate(text=turn_text('currentDate: "Day 150"', anchor_day=150))["flag"]
        self.assertIsNone(rp_gate.fiction_day(flag))
        heading = f"### Beat 4131: Day 150, ~10:00 (Monday). (Tier 1) {flag}"
        self.assertEqual(rp_gate.parse_beat_heading(heading), {"beat": "4131", "day": 150})

    def test_a_symlinked_log_dir_outside_the_session_directory_is_refused(self):
        p = Project(self)
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        (p.root / "Work/rp/test/log").symlink_to(outside.name)
        got = p.rotate()
        self.assertEqual(got["action"], "none")
        self.assertTrue(any("log_dir" in w for w in got["warnings"]))
        self.assertEqual(os.listdir(outside.name), [])

    def test_a_closed_file_whose_successor_is_gone_is_refused(self):
        p = Project(self)
        p.rotate()
        (p.root / DAY_197).rename(p.root / "Work/rp/test/moved.md")
        p.marker(f"session_file: {LEGACY}\n")  # a marker still naming the closed log
        with self.assertRaises(ValueError):
            p.rotate(session=LEGACY)
        got = rp_gate.resolve_session_file(p.root, today="2026-10-26")
        self.assertIn("missing", got["warning"])

    def test_a_lost_closing_marker_does_not_double_the_generated_block(self):
        p = Project(self)
        p.rotate()
        (p.root / DAY_197).write_text(p.read(DAY_197).replace("<!-- /rp-day-file -->\n", ""), encoding="utf-8")
        p.rotate(text=turn_text('currentDate: "Day 198"', anchor_day=198))
        new = p.read(f"{LOG_DIR}/day-0198.md")
        self.assertEqual(new.count("<!-- rp-day-file"), 1)
        self.assertEqual(new.count("# Test Run: Day"), 1)

    def test_an_amendment_after_its_own_beat_stays_with_it(self):
        lines = ["## Day Plan: Day 4", "", "### Beat 3705 PRE-COMMIT (GM)", "", "### Beat 3705: Day 4, ~21:09", "",
                 "Prose.", "", "### Beat 3705 AMENDMENT (before posting)", "", "Two parts.", "",
                 "### Beat 3706 PRE-COMMIT (GM)", "", "### Beat 3706: Day 4, ~21:10", "", "More."]
        units = rp_gate._beat_units(lines)
        self.assertEqual([(u["beat"], u["start"], u["end"]) for u in units], [("3705", 1, 11), ("3706", 13, 17)])

    def test_a_precondition_error_is_not_offered_as_retryable(self):
        p = Project(self)
        code, _out, err = run("rotate", "--project-dir", str(p.root), "--session-file", "Work/rp/rp_session_2030-01-01.md",
                              "--start", "--day", "1")
        self.assertEqual(code, 2)
        self.assertNotIn("safe to repeat", err)


class SecondPassTests(unittest.TestCase):
    """Cases from the second cold review: defects the first fixes introduced or left open."""

    def lint_days(self, p, text, session=LEGACY):
        scene = rp_gate.load_scene(p.root, session)
        result = rp_gate.lint(rp_gate.parse_turn(text), scene, rp_gate.load_config(p.root), p.root)
        return {f["check"]: f["fix"] for f in result["findings"] if f["check"].startswith("delta_day")}

    def test_a_clock_later_in_the_scene_field_is_not_the_anchor_day(self):
        # N1: the run keeps a live clock written "THE CURE Day 3 of 30".
        p = Project(self)
        text = turn_text('currentDate: "Day 196 (Monday)"', anchor_day=196).replace(
            "~06:00, calm |", "~19:12, merry; THE CURE Day 3 of 30 |")
        self.assertEqual(self.lint_days(p, text), {})
        night = turn_text('currentDate: "Day 197"').replace("Scene: Day 197, ~06:00", "Scene: Day 196, ~22:00 to Day 197, ~05:50")
        self.assertEqual(self.lint_days(p, night), {})

    def test_an_edited_day_file_is_never_hidden_or_rebuilt(self):
        # N2: content under any heading (an unnumbered "### Scene" beat, an END block) is the run's.
        p = Project(self)
        with mock.patch.object(rp_gate, "_append_footer", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("### Scene: the felling\n\nLiss sets the axe.\n\n## END (2026-10-26)\n")
        p.marker(f"session_file: {LEGACY}\n")
        got = p.rotate(text=SAME_DAY)
        self.assertEqual(got["set_aside"], [])
        self.assertIn("Liss sets the axe.", p.read(DAY_197))

    def test_a_long_chain_is_followed_to_its_end(self):
        # N3: more than 64 rotations is two weeks of play.
        p = Project(self)
        for day in range(197, 267):
            p.rotate(text=turn_text(f'currentDate: "Day {day}"', anchor_day=day))
        p.marker(f"session_file: {LEGACY}\n")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-26")
        self.assertEqual((got["path"], "missing" in (got["warning"] or "")), (f"{LOG_DIR}/day-0266.md", False))

    def test_returning_from_a_flashback_in_a_day_file_is_not_a_jump(self):
        # N4: the frontmatter says Day 150 after the flashback; the file is Day 197.
        p = Project(self)
        p.rotate()
        set_frontmatter(p, DAY_197, "currentDate", '"Day 150"')
        self.assertEqual(self.lint_days(p, turn_text('currentDate: "Day 197"', anchor_day=197), DAY_197), {})

    def test_a_closed_legacy_log_continued_by_a_day_file_hides_the_older_ones(self):
        # The documented migration: /rp:end closed 10-24 (no footer), the first day file continues it.
        p = Project(self)
        set_frontmatter(p, LEGACY, "campaign", '"Test Run"\nstatus: closed')
        older = p.root / "Work/rp/rp_session_2026-10-17.md"
        older.write_text(LEGACY_FRONTMATTER.format(extra="") + "\n### Beat 1: Day 3, ~08:00\n", encoding="utf-8")
        (p.root / LOG_DIR).mkdir(parents=True)
        (p.root / DAY_197).write_text(f'---\ncampaign: "Test Run"\nlog_dir: "{LOG_DIR}"\nlog_day: 197\n'
                                      f'continues: "{LEGACY}"\ncurrentDate: "Day 197"\n---\n\n# Day 197\n',
                                      encoding="utf-8")
        os.utime(p.root / DAY_197, (1, 1))
        os.utime(older)
        p.marker("")
        self.assertEqual(rp_gate.resolve_session_file(p.root, today="2026-10-26")["path"], DAY_197)

    def test_only_the_newest_legacy_log_is_ever_offered(self):
        # Campaign names change within one run ("garret", then "Garret at the Gilt Thorn"), so the
        # fallback cannot tell an older log of another campaign from a superseded one of this run.
        # It offers neither; /rp:start opens another campaign's log with load <session>.
        p = Project(self)
        p.rotate()
        other = p.root / "Work/rp/rp_session_2026-10-20.md"
        other.write_text('---\ncampaign: "Other"\ncurrentDate: "Day 9"\n---\n\n### Beat 1: Day 9, ~08:00\n',
                         encoding="utf-8")
        os.utime(p.root / DAY_197, (1, 1))
        p.marker("")
        self.assertEqual(rp_gate.resolve_session_file(p.root, today="2026-10-26")["path"], DAY_197)
        newer = p.root / "Work/rp/rp_session_2026-10-25.md"
        newer.write_text('---\ncampaign: "Other"\ncurrentDate: "Day 9"\n---\n', encoding="utf-8")
        self.assertEqual(rp_gate.resolve_session_file(p.root, today="2026-10-26")["path"], "Work/rp/rp_session_2026-10-25.md")

    def test_rotate_refuses_a_jump_beyond_seven_days_without_the_keepers_word(self):
        # Under review: lint-only, or a --final ship, nothing else stops a typo rotating to Day 1970.
        p = Project(self)
        got = p.rotate(text=turn_text('currentDate: "Day 1970"', anchor_day=1970))
        self.assertEqual((got["action"], got["path"]), ("refused", LEGACY))
        self.assertFalse((p.root / LOG_DIR).exists())
        self.assertEqual(footers(p.read(LEGACY)), 0)
        self.assertEqual(p.rotate(text=turn_text('currentDate: "Day 204"', anchor_day=204))["action"], "refused")
        self.assertNotIn("--day", " ".join(p.rotate(text=turn_text('currentDate: "Day 204"', anchor_day=204))["warnings"]))
        allowed = p.rotate(text=turn_text('currentDate: "Day 204"', anchor_day=204), allow_jump=True)
        self.assertEqual((allowed["action"], allowed["path"]), ("rotated", f"{LOG_DIR}/day-0204.md"))
        self.assertEqual(p.rotate(text=turn_text('currentDate: "Day 211"', anchor_day=211))["action"], "rotated")

    def test_the_session_start_survives_header_edits_too(self):
        p = Project(self)
        (p.root / "Work/markers/rp-active").unlink()
        rp_gate.rotate(p.root, LEGACY, start=True, today="2026-10-26")
        opened = len(p.read(LEGACY).splitlines())  # the opening comment is the last line
        p.legacy.write_text(p.read(LEGACY).replace("## Open threads\n", "## Open threads\n\n- the vine\n- the post\n", 1),
                            encoding="utf-8")
        code, out, _err = run("session", "--project-dir", str(p.root))
        line = json.loads(out)["session_opened_line"]
        self.assertEqual(line, opened + 3)
        self.assertTrue(p.read(LEGACY).splitlines()[line - 1].startswith("<!-- rp-session-opened"))

    def test_only_a_final_footer_closes_a_file(self):
        # A header quoting a closed file's tail verbatim must not close the open file.
        p = Project(self)
        p.legacy.write_text(p.read(LEGACY).replace("## Open threads\n", "## Open threads\n\n> <!-- rp-rotated-to: "
                                                   "Work/rp/rp_session_2026-10-17.md -->\n", 1), encoding="utf-8")
        self.assertIsNone(rp_gate.rotation_footer(p.legacy))
        code, out, err = run("session", "--project-dir", str(p.root))
        got = json.loads(out)
        self.assertEqual(got["path"], LEGACY)
        self.assertIn("before the end", err)  # quoted, or text written after a close: say so
        lines = p.read(LEGACY).splitlines()
        self.assertEqual(got["continuity"]["current"]["end"], max(i for i, x in enumerate(lines, 1) if x.strip()))
        p.rotate()
        self.assertEqual(rp_gate.rotation_footer(p.legacy), DAY_197)


class ThirdPassTests(unittest.TestCase):
    """Cases from the third cold review."""

    def test_a_closed_newest_legacy_log_offers_no_older_one(self):
        # /rp:end closed 10-24 before it rotated; 10-17 is open only because nothing ever closed it.
        p = Project(self)
        set_frontmatter(p, LEGACY, "campaign", '"Test Run"\nstatus: closed')
        older = p.root / "Work/rp/rp_session_2026-10-17.md"
        older.write_text(LEGACY_FRONTMATTER.format(extra="") + "\n### Beat 1: Day 3, ~08:00\n", encoding="utf-8")
        os.utime(older)
        p.marker("")
        self.assertEqual(rp_gate.resolve_session_file(p.root, today="2026-10-26")["source"], "new")

    def test_an_older_log_under_another_campaign_name_is_not_offered(self):
        # 09-24 to 09-27 say campaign: "garret"; later logs "Garret at the Gilt Thorn".
        p = Project(self)
        older = p.root / "Work/rp/rp_session_2026-09-27.md"
        older.write_text('---\ncampaign: "garret"\ncurrentDate: "Day 60"\n---\n\n### Beat 343: Day 60, ~18:43\n',
                         encoding="utf-8")
        p.rotate()
        os.utime(p.root / DAY_197, (1, 1))
        os.utime(older)
        p.marker("")
        self.assertEqual(rp_gate.resolve_session_file(p.root, today="2026-10-26")["path"], DAY_197)

    def test_an_explicit_day_does_not_skip_the_jump_guard(self):
        p = Project(self)
        (p.root / "Work/markers/rp-active").unlink()
        got = rp_gate.rotate(p.root, LEGACY, day=1970, start=True, today="2026-10-26")
        self.assertEqual(got["action"], "refused")
        self.assertFalse((p.root / "Work/markers/rp-active").exists())
        self.assertIn("Keeper", " ".join(got["warnings"]))
        ok = rp_gate.rotate(p.root, LEGACY, day=1970, start=True, allow_jump=True, today="2026-10-26")
        self.assertEqual(ok["action"], "rotated")

    def test_opening_comments_are_not_copied_forward(self):
        p = Project(self)
        session = LEGACY
        for day in (197, 198, 199):
            (p.root / "Work/markers/rp-active").unlink(missing_ok=True)
            session = rp_gate.rotate(p.root, session, day=day, start=True, today="2026-10-26")["path"]
        text = p.read(f"{LOG_DIR}/day-0199.md")
        self.assertEqual(text.count("<!-- rp-session-opened"), 1)
        self.assertTrue(text.rstrip("\n").splitlines()[-1].startswith("<!-- rp-session-opened"))

    def test_start_on_a_closed_legacy_log_warns_that_it_reopens_it(self):
        p = Project(self)
        set_frontmatter(p, LEGACY, "campaign", '"Test Run"\nstatus: closed')
        (p.root / "Work/markers/rp-active").unlink()
        got = rp_gate.rotate(p.root, LEGACY, day=196, start=True, today="2026-10-26")
        self.assertEqual(got["action"], "none")
        self.assertTrue(any("closed" in w for w in got["warnings"]))


class FourthPassTests(unittest.TestCase):
    """Cases from the fourth cold review."""

    def test_a_log_dir_with_a_space_keeps_one_readable_footer(self):
        p = Project(self)
        spaced = "Work/rp/Gilt Thorn/log"
        p.legacy.write_text(p.read(LEGACY).replace(LOG_DIR, spaced), encoding="utf-8")
        got = p.rotate()
        self.assertEqual(got["path"], f"{spaced}/day-0197.md")
        self.assertEqual(rp_gate.rotation_footer(p.legacy), f"{spaced}/day-0197.md")
        for _ in range(3):
            p.rotate(text=SAME_DAY.replace("Day 196", "Day 197"))
        self.assertEqual(footers(p.read(LEGACY)), 1)
        p.marker(f"session_file: {LEGACY}\n")
        self.assertEqual(rp_gate.resolve_session_file(p.root, today="2026-10-26")["path"], f"{spaced}/day-0197.md")

    def test_an_old_marker_spans_back_to_the_legacy_log_it_opened(self):
        # The live Garret marker was written by the old /rp:start: session_file only.
        p = Project(self)
        p.marker(f"session_file: {LEGACY}\n")
        p.rotate()
        p.rotate(text=turn_text('currentDate: "Day 198"', anchor_day=198))
        code, out, _err = run("session", "--project-dir", str(p.root))
        got = json.loads(out)
        self.assertEqual(got["session_files"], [LEGACY, DAY_197, f"{LOG_DIR}/day-0198.md"])
        self.assertIsNone(got["session_opened_line"])

    def test_a_long_beat_label_cannot_hide_the_footer(self):
        p = Project(self)
        p.rotate(beat="4131" + "x" * 9000)
        self.assertEqual(rp_gate.rotation_footer(p.legacy), DAY_197)
        p.rotate(text=SAME_DAY.replace("Day 196", "Day 197"))
        self.assertEqual(footers(p.read(LEGACY)), 1)

    def test_a_marker_recreated_by_a_turn_records_where_the_session_was(self):
        # Pass 5: without it, the reach-back would pull every earlier session into /rp:end.
        p = Project(self)
        p.rotate()
        p.rotate(text=turn_text('currentDate: "Day 198"', anchor_day=198))
        (p.root / "Work/markers/rp-active").unlink()
        p.rotate(session=f"{LOG_DIR}/day-0198.md", text=turn_text('currentDate: "Day 199"', anchor_day=199))
        self.assertEqual(p.marker_fields()["session_opened"], f"{LOG_DIR}/day-0198.md")
        code, out, _err = run("session", "--project-dir", str(p.root))
        self.assertEqual(json.loads(out)["session_files"], [f"{LOG_DIR}/day-0198.md", f"{LOG_DIR}/day-0199.md"])

    def test_the_command_templates_quote_the_session_file(self):
        for name in ("turn.md", "start.md"):
            text = (REPO / "plugins/rp/commands" / name).read_text(encoding="utf-8")
            with self.subTest(command=name):
                self.assertNotRegex(text, r"--session-file <")


class PanelDecisionTests(unittest.TestCase):
    """Behaviour the 2026-10-25 panel decided (Work/panels/2026-10-25--rp-day-rotation-decisions)."""

    def test_a_closed_legacy_log_that_can_rotate_is_named_when_nothing_is_open(self):
        # Q4: /rp:end closed 10-24 before D197 rotated it. The fallback still offers nothing, but says how to go on.
        p = Project(self)
        set_frontmatter(p, LEGACY, "campaign", '"Test Run"\nstatus: closed')
        p.marker("")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-26")
        self.assertEqual(got["source"], "new")
        self.assertIn(LEGACY, got["warning"])
        self.assertIn("load", got["warning"])
        self.assertIn("--day", got["warning"])
        # Following it: /rp:start load <log> --day 197 closes the log and opens its day file.
        opened = rp_gate.rotate(p.root, LEGACY, day=197, start=True, today="2026-10-26")
        self.assertEqual((opened["action"], opened["path"]), ("rotated", DAY_197))
        self.assertEqual(rp_gate.rotation_footer(p.legacy), DAY_197)
        self.assertNotIn("status", rp_gate.read_frontmatter(p.root / DAY_197))
        self.assertEqual(rp_gate.resolve_session_file(p.root)["path"], DAY_197)

    def test_a_closed_legacy_log_that_cannot_rotate_is_not_offered(self):
        # No log_dir (or no fiction day): /rp:end's close is final, and a new session starts its own file.
        for extra in ("", f'log_dir: "{LOG_DIR}"\n'):
            with self.subTest(log_dir=bool(extra)):
                p = Project(self, log_dir=False)
                text = LEGACY_FRONTMATTER.format(extra=extra + "status: closed\n")
                if extra:  # a log_dir but no fiction day anywhere
                    text = text.replace('"Day 196 (Monday)"', '"Monday"').replace('"Day 195 (Sunday)"', '"Sunday"')
                    body = "\n### Beat 1: ~08:00 (Monday)\n"
                else:
                    body = LEGACY_HEADER + legacy_body()
                p.legacy.write_text(text + body, encoding="utf-8")
                p.marker("")
                got = rp_gate.resolve_session_file(p.root, today="2026-10-26")
                self.assertEqual(got["source"], "new")
                self.assertNotIn(LEGACY, got["warning"])

    def test_start_follows_the_warning_on_a_new_route(self):
        text = (REPO / "plugins/rp/commands/start.md").read_text(encoding="utf-8")
        self.assertIn("names a closed legacy log", text)

    def test_citations_cite_a_line_only_in_a_closed_file(self):
        # Q6: an open file's lines move with every frontmatter delta; a closed file never changes.
        for rel in ("plugins/rp/README.md", "plugins/rp/commands/end.md", "plugins/rp/agents/rp-ratifier.md",
                    "plugins/rp/agents/canon-writer.md"):
            with self.subTest(rel=rel):
                self.assertIn("a line only in a closed file", (REPO / rel).read_text(encoding="utf-8"))
        end = (REPO / "plugins/rp/commands/end.md").read_text(encoding="utf-8")
        self.assertNotIn("each line by its day file", end)  # the summary is written while the day file is open

    def test_run_resets_and_paths_are_documented(self):
        # Q2: bare day-NNNN.md names; the directory carries the campaign and the run.
        text = (REPO / "plugins/rp/README.md").read_text(encoding="utf-8")
        self.assertIn("A run reset moves the old run out of `Work/rp/`", text)
        self.assertIn("path-qualified", text)


class AmendmentReviewTests(unittest.TestCase):
    """Cases from the cold review of the 2026-10-25 amendment; each failed there."""

    def window(self, p, *argv):
        code, out, err = run("session", "--project-dir", str(p.root), *argv)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def line_of(self, p, rel, text):
        return next(i for i, line in enumerate(p.read(rel).splitlines(), 1) if line.startswith(text))

    def closed_legacy(self, p):
        set_frontmatter(p, LEGACY, "campaign", '"Test Run"\nstatus: closed')
        p.marker("")

    def plans(self, got):
        return [(r["path"], r["start"], r["end"]) for r in got["plans"]]

    def test_a_long_day_still_names_its_day_plan(self):
        # The window holds only the last beats; the Day Plan (NPC agendas, clocks, missing_day_plan) is named apart.
        p = Project(self)
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n### Clocks\n\n- ~09:00 the cart\n\n"
                     + "".join(beat(n, 197) for n in range(4131, 4156)))
        got = self.window(p)["continuity"]
        self.assertEqual(self.plans(got), [(DAY_197, self.line_of(p, DAY_197, "## Day Plan: Day 197"),
                                            self.line_of(p, DAY_197, "- ~09:00 the cart"))])
        self.assertLess(got["plans"][0]["end"], got["current"]["start"])
        self.assertEqual(got["plans"][0]["heading"], "## Day Plan: Day 197 (Tuesday)")

    def test_every_plan_of_the_day_is_named_and_none_the_window_holds(self):
        # A second session's plan, or an ADDENDUM, binds as much as the first.
        p = Project(self)
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n- ~09:00 the cart\n\n"
                     + "## Day Plan: Day 197, morning addendum\n\n- ~10:00 the post\n\n"
                     + "".join(beat(n, 197) for n in range(4131, 4156))
                     + "## Day Plan D197 ADDENDUM (binding)\n\n- ~20:00 the bell\n\n"
                     + "".join(beat(n, 197) for n in range(4156, 4180))
                     + "## Day Plan: Day 197, evening revision\n\n- ~23:00 the door\n\n" + beat(4180, 197))
        got = self.window(p)["continuity"]
        self.assertEqual(self.plans(got), [
            (DAY_197, self.line_of(p, DAY_197, "## Day Plan: Day 197 (Tuesday)"), self.line_of(p, DAY_197, "- ~09:00")),
            (DAY_197, self.line_of(p, DAY_197, "## Day Plan: Day 197, morning"), self.line_of(p, DAY_197, "- ~10:00")),
            (DAY_197, self.line_of(p, DAY_197, "## Day Plan D197 ADDENDUM"), self.line_of(p, DAY_197, "- ~20:00"))])
        self.assertLess(got["current"]["start"], self.line_of(p, DAY_197, "## Day Plan: Day 197, evening"))

    def test_a_short_day_names_no_plan_its_range_holds(self):
        p = Project(self)
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n" + beat(4131, 197))
        got = self.window(p)["continuity"]
        self.assertTrue(got["current"]["whole_day"])
        self.assertEqual(got["plans"], [])

    def test_a_legacy_day_plan_is_named_too(self):
        p = Project(self)
        p.mode("sessionLog:\n  previousBeats: 3\n")
        got = self.window(p)["continuity"]
        self.assertEqual(self.plans(got), [(LEGACY, self.line_of(p, LEGACY, "## Day Plan: Day 196"),
                                            self.line_of(p, LEGACY, "- ~05:50 first light"))])

    def test_a_plan_for_another_day_is_not_this_days(self):
        p = Project(self)
        p.mode("sessionLog:\n  previousBeats: 0\n")
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write(beat(4131, 197) + "## Day Plan: Day 198 (Wednesday)\n\n- ~06:00 rain\n")
        self.assertEqual(self.window(p)["continuity"]["plans"], [])

    def test_a_plan_written_early_is_found(self):
        # The logs twice wrote a day's plan before an earlier day's last beat (10-11:3110, 10-17:160).
        p = Project(self)
        p.mode("sessionLog:\n  previousBeats: 3\n")
        text = p.read(LEGACY).replace("## Day Plan: Day 196 (Monday)\n\n### Clocks\n\n- ~05:50 first light\n\n", "", 1)
        text = text.replace("### Beat 4124 PRE-COMMIT", "## Day Plan: Day 196 (Monday)\n\n- ~05:50 first light\n\n"
                            "### Beat 4124 PRE-COMMIT", 1)
        p.legacy.write_text(text, encoding="utf-8")
        got = self.window(p)["continuity"]
        self.assertEqual(self.plans(got), [(LEGACY, self.line_of(p, LEGACY, "## Day Plan: Day 196"),
                                            self.line_of(p, LEGACY, "- ~05:50 first light"))])
        # And in the file before: rotate on, the Day 197 plan written in the legacy log before its last beat.
        p.mode("sessionLog:\n  previousBeats: 0\n")
        text = p.read(LEGACY).replace("### Beat 4130 PRE-COMMIT", "## Day Plan: Day 197 (Tuesday)\n\n- ~06:00 rain\n\n"
                                      "### Beat 4130 PRE-COMMIT", 1)
        p.legacy.write_text(text, encoding="utf-8")
        p.rotate()
        got = self.window(p)["continuity"]
        self.assertEqual(self.plans(got), [(LEGACY, self.line_of(p, LEGACY, "## Day Plan: Day 197"),
                                            self.line_of(p, LEGACY, "- ~06:00 rain"))])

    def test_fenced_headings_neither_open_nor_cut_a_plan(self):
        p = Project(self)
        p.mode("sessionLog:\n  previousBeats: 1\n")
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n```\n### Beat 9999: Day 197, ~01:00\n```\n\n- ~09:00 the cart\n\n"
                     + beat(4131, 197).replace("Liss sets down the axe.", "```\n## Day Plan: Day 197 (fake)\n```") + beat(4132, 197))
        got = self.window(p)["continuity"]
        self.assertEqual(self.plans(got), [(DAY_197, self.line_of(p, DAY_197, "## Day Plan: Day 197 (Tuesday)"),
                                            self.line_of(p, DAY_197, "- ~09:00 the cart"))])

    def test_the_plan_line_and_the_reviewer_read_it(self):
        turn = (REPO / "plugins/rp/commands/turn.md").read_text(encoding="utf-8")
        self.assertIn("  plan: <path> lines <start>-<end>", turn)
        reviewer = (REPO / "plugins/write/agents/continuity-reviewer.md").read_text(encoding="utf-8")
        self.assertIn("and its Day Plan", reviewer)
        self.assertIn("plan: Work/rp/<campaign>/log/day-NNNN.md lines 81-130", reviewer)
        self.assertIn("day: Work/rp/<campaign>/log/day-NNNN.md lines 300-480", reviewer)  # the plan lies outside it
        readme = (REPO / "plugins/rp/README.md").read_text(encoding="utf-8")
        self.assertIn("`plans`", readme)

    def test_the_warning_says_what_the_same_day_does(self):
        p = Project(self)
        self.closed_legacy(p)
        warning = rp_gate.resolve_session_file(p.root, today="2026-10-26")["warning"]
        self.assertIn("on its own day it reopens", warning)

    def test_the_warning_honours_the_config_and_its_own_log_dir(self):
        p = Project(self)
        self.closed_legacy(p)
        alt = p.root / "alt.yaml"
        alt.write_text("sessionLog:\n  rotation: off\n", encoding="utf-8")
        self.assertNotIn(LEGACY, self.window(p, "--config", str(alt))["warning"])  # rotation off: the close is final
        backup = p.root / "Work/rp/backup/day-0197.md"  # a copy outside its log_dir continues nothing
        backup.parent.mkdir(parents=True)
        backup.write_text(f'---\nlog_dir: "{LOG_DIR}"\nlog_day: 197\ncontinues: "{LEGACY}"\n---\n', encoding="utf-8")
        self.assertIn(LEGACY, self.window(p)["warning"])

    def test_no_warning_for_a_log_rotation_closed(self):
        # Footered, its day file gone: rotation closed it for good, whatever its status says.
        p = Project(self)
        self.closed_legacy(p)
        p.rotate(session=LEGACY)
        (p.root / DAY_197).unlink()
        p.marker("")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-28")
        self.assertEqual(got["source"], "new")
        self.assertNotIn(f"load {LEGACY}", got["warning"])

    def test_no_warning_for_a_log_a_day_file_continues(self):
        # Its footer lost, but day-0197 (footered; its successor gone) continues it: the run moved on.
        p = Project(self)
        self.closed_legacy(p)
        p.rotate(session=LEGACY)
        rp_gate.rotate(p.root, DAY_197, turn_text=turn_text('currentDate: "Day 198 (Wednesday)"', anchor_day=198),
                       today="2026-10-27")
        (p.root / f"{LOG_DIR}/day-0198.md").unlink()
        p.legacy.write_text(p.read(LEGACY).split("\n---\n\n<!-- rp-rotated-to:")[0] + "\n", encoding="utf-8")
        self.assertIsNone(rp_gate.rotation_footer(p.legacy))
        p.marker("")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-28")
        self.assertEqual(got["source"], "new")
        self.assertNotIn(f"load {LEGACY}", got["warning"])

    def test_no_contradiction_when_todays_name_is_the_closed_log(self):
        p = Project(self)
        self.closed_legacy(p)
        warning = rp_gate.resolve_session_file(p.root, today="2026-10-24")["warning"]
        self.assertNotIn("No session file exists yet", warning)
        self.assertIn("already exists", warning)

    def test_an_existing_open_file_under_todays_name_is_not_called_closed(self):
        p = Project(self, log_dir=False)
        (p.root / "Work/rp/rp_session_2026-10-24b.md").write_text(
            LEGACY_FRONTMATTER.format(extra="status: closed\n") + "\n### Beat 1: Day 3, ~08:00\n", encoding="utf-8")
        p.marker("")
        got = rp_gate.resolve_session_file(p.root, today="2026-10-24")
        self.assertEqual(got["source"], "new")
        self.assertNotIn("is closed", got["warning"])
        self.assertIn(f"load {LEGACY}", got["warning"])

    def test_docs_never_write_over_an_existing_session_file(self):
        start = (REPO / "plugins/rp/commands/start.md").read_text(encoding="utf-8")
        self.assertIn("on its own day it reopens", start)  # Step 2 does not always close the log
        self.assertIn("If today's file already exists, never write over it", start)
        turn = (REPO / "plugins/rp/commands/turn.md").read_text(encoding="utf-8")
        self.assertNotIn("`source: new`: no session file exists.", turn)

    def test_a_continues_loop_is_walked_once(self):
        # A hand edit points day-0197 back at day-0198: the read takes 197's beat once, not until the hop cap.
        p = Project(self)
        p.rotate()
        day_198 = f"{LOG_DIR}/day-0198.md"
        rp_gate.rotate(p.root, DAY_197, turn_text=turn_text('currentDate: "Day 198 (Wednesday)"', anchor_day=198),
                       today="2026-10-27")
        text = p.read(DAY_197).replace(f'continues: "{LEGACY}"', f'continues: "{day_198}"', 1)
        head, tail = text.split(rp_gate.DAY_BLOCK_END + "\n", 1)
        (p.root / DAY_197).write_text(head + rp_gate.DAY_BLOCK_END + "\n\n" + beat(4131, 197) + tail, encoding="utf-8")
        got = rp_gate.continuity(p.root, day_198, 20)
        self.assertEqual([(r["path"], r["beats"]) for r in got["previous"]], [(DAY_197, 1)])

    def test_with_no_beat_of_the_day_yet_the_earlier_beats_stop_before_its_plan(self):
        p = Project(self)
        set_frontmatter(p, LEGACY, "currentDate", '"Day 197 (Tuesday)"')  # an accepted surrender moved the day
        with open(p.legacy, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n- ~06:00 the cart\n")
        got = rp_gate.continuity(p.root, LEGACY, 20)
        plan_line = self.line_of(p, LEGACY, "## Day Plan: Day 197")
        self.assertEqual(got["current"]["start"], plan_line)
        self.assertLess(got["previous"][-1]["end"], plan_line)

    def test_docs_match_the_read_and_the_first_day_file(self):
        gm = (REPO / "plugins/rp/agents/roleplay-gm.md").read_text(encoding="utf-8")
        self.assertIn("in the same legacy log or the files before", gm)
        turn = (REPO / "plugins/rp/commands/turn.md").read_text(encoding="utf-8")
        self.assertIn("omit the beat span when `first_beat` is null", turn)
        start = (REPO / "plugins/rp/commands/start.md").read_text(encoding="utf-8")
        self.assertNotIn("A new campaign, or a legacy campaign moving to day files: the first day file", start)
        self.assertIn("add `log_dir` to its frontmatter", start)
        self.assertIn("already exists, stop", start)  # a hand-written Day 1 must never overwrite an old run's
        readme = (REPO / "plugins/rp/README.md").read_text(encoding="utf-8")
        self.assertNotIn("(rotation refuses it)", readme)
        self.assertIn("`status: closed` does not count", readme)
        changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8").split("## Unreleased — RP live state", 1)[0]
        self.assertNotIn("never the whole day", changelog)


class ContractTests(unittest.TestCase):
    def read(self, rel):
        return (REPO / rel).read_text(encoding="utf-8")

    def test_turn_rotates_before_the_append(self):
        text = self.read("plugins/rp/commands/turn.md")
        ship = text.split("# 3e. Ship", 1)[1].split("# 3f.", 1)[0]
        self.assertIn('rp_gate.py" rotate', ship)
        self.assertLess(ship.index('rp_gate.py" rotate'), ship.index('rp_gate.py" ship'))  # ship appends the beat
        self.assertIn("earlier:", text)  # LIVE_STATE carries the continuity read, one line per range
        self.assertNotIn("previous_day", text)
        self.assertIn("never the whole day", text)

    def test_start_and_end_use_the_tool(self):
        start = self.read("plugins/rp/commands/start.md")
        self.assertIn("--start", start)
        step2 = start.split("### Step 2", 1)[1].split("\n### ", 1)[0]
        self.assertIn("refused", step2)
        self.assertIn("Step 5 first", step2)  # a brand-new file exists before the marker names it
        end = self.read("plugins/rp/commands/end.md")
        self.assertIn("session_files", end)
        self.assertIn('rp_gate.py" rotate', end)

    def test_docs_and_consumers_know_day_files(self):
        self.assertIn("previousBeats: 20", self.read("plugins/rp/README.md"))
        reviewer = self.read("plugins/write/agents/continuity-reviewer.md")
        self.assertIn("earlier:", reviewer)  # its LIVE_STATE example matches /rp:turn's
        self.assertNotIn("previous_day", reviewer)
        self.assertIn("day-", self.read("plugins/rp/templates/canon-layout.md"))
        for rel in ("plugins/rp/agents/timeline-search.md", "plugins/write/agents/continuity-reviewer.md",
                    "plugins/rp/agents/roleplay-gm.md"):
            with self.subTest(rel=rel):
                self.assertIn("day file", self.read(rel))


if __name__ == "__main__":
    unittest.main()
