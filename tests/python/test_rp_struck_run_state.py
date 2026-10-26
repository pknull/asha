"""Struck drafts with run state: the day summary's ranges and checks skip struck drafts like every other reader,
and a struck draft written on a rotated day sits above the new Day Plan that ship appends."""
import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("rp_struck_run_state_fixtures", HERE / "test_rp_run_state.py")
assert _spec is not None and _spec.loader is not None
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)
rp_gate = fx.rp_gate  # the module the fixtures drive
Project, LEGACY, DAY_197, NEXT_DAY, turn_text = fx.Project, fx.LEGACY, fx.DAY_197, fx.NEXT_DAY, fx.turn_text

TODAY = "2026-10-26"
NEXT_DAY_PLAN = "## Day Plan: Day 197 (Tuesday)\n\n- Liss: the shed\n\n"


def strike(p, prose, beat_label, session=None):
    session = session or rp_gate.resolve_session_file(p.root)["path"]
    text = turn_text().replace("Liss sets down the axe.", prose)
    return rp_gate.strike(p.root, session, turn_text=text, attempt=1, beat=beat_label, reason="presence",
                          today=TODAY)


def struck_lines(p, rel):
    lines = p.read(rel).splitlines()
    return {s + 1 + n for s, e in rp_gate.struck_spans(lines) for n in range(e - s)}


def read_lines(entry):
    return {n for a, b in entry.get("parts", [[entry["start"], entry["end"]]]) for n in range(a, b + 1)}


def play_into_day_197(p):
    """Beat 4131 on Day 196 after a struck draft; then a struck draft for 4132, the beat that opens Day 197."""
    strike(p, "Vey hangs the door on its new hinges.", "4131")
    p.rotate(text=turn_text(), beat="4131")  # same day: the struck draft lands before the beat
    p.ship(turn_text())
    strike(p, "Vey sends word: the hinges are ready.", "4132")
    got = p.rotate(beat="4132")
    p.ship(NEXT_DAY, append=NEXT_DAY_PLAN + "### Beat 4132: Day 197, ~06:00 (Tuesday).\n\nMorning.\n",
           session=got["path"], beat="4132")
    return got


class DaySpanTests(unittest.TestCase):
    def assert_reads_no_struck_line(self, p, rel, entry):
        struck = struck_lines(p, rel)
        self.assertTrue(struck)
        read = read_lines(entry)
        self.assertFalse(read & struck)
        lines = p.read(rel).splitlines()
        for a, b in entry.get("parts", []):
            self.assertTrue(lines[a - 1].strip() and lines[b - 1].strip())
        beats = {n for n, line in enumerate(lines, 1) if entry["start"] <= n <= entry["end"]
                 and rp_gate.BEAT_HEADING_RE.match(line)}
        self.assertTrue(beats)
        self.assertLessEqual(beats, read)  # every beat in the span is still read

    def test_a_closed_days_summary_range_leaves_struck_drafts_out(self):
        p = Project(self)
        got = play_into_day_197(p)
        self.assertEqual(got["action"], "rotated")
        day = next(d for d in rp_gate.summary_plan(p.root, DAY_197)["days"] if d["day"] == 196)
        self.assertEqual((day["beats"]["first_beat"], day["beats"]["last_beat"]), ("4110", "4131"))
        self.assertIn("parts", day["beats"])  # the block sits between Beat 4130 and Beat 4131
        self.assert_reads_no_struck_line(p, LEGACY, day["beats"])

    def test_the_open_days_range_leaves_out_a_struck_draft_written_at_rp_end(self):
        p = Project(self)
        play_into_day_197(p)
        strike(p, "Vey turns away from the gate.", "4133")
        self.assertTrue(rp_gate.rotate(p.root, DAY_197, repair_only=True, today=TODAY)["struck"])
        opened = rp_gate.summary_plan(p.root, DAY_197)["open"]
        self.assertEqual(opened["beats"]["last_beat"], "4132")
        self.assertLess(opened["beats"]["end"], max(struck_lines(p, DAY_197)))  # the tail block is cut off
        self.assert_reads_no_struck_line(p, DAY_197, opened["beats"])


class PlacementTests(unittest.TestCase):
    def test_on_a_rotated_day_the_struck_draft_sits_above_the_new_day_plan_and_no_range_reads_it(self):
        p = Project(self)
        play_into_day_197(p)
        lines = p.read(DAY_197).splitlines()
        struck = struck_lines(p, DAY_197)
        plan = lines.index(NEXT_DAY_PLAN.splitlines()[0]) + 1
        self.assertLess(max(struck), plan)  # above the Day Plan ship appended
        header = rp_gate.live_state(p.root, DAY_197, today=TODAY)["header"]
        self.assertLess(header["end"], min(struck))
        current = rp_gate.continuity(p.root, DAY_197, 20)["current"]
        self.assertEqual((current["start"], current["first_beat"]), (plan, "4132"))
        self.assertNotIn("parts", current)
        opened = rp_gate.summary_plan(p.root, DAY_197)["open"]
        self.assertEqual([x["start"] for x in opened["plans"]], [plan])
        self.assertGreater(opened["beats"]["start"], plan)
        p.rotate(text=turn_text('currentDate: "Day 198 (Wednesday)"\nscene.time: "06:00"',
                                "Scene: Day 198, ~06:00, calm | Location: the kitchen | Present: Liss"), beat="4133")
        self.assertNotIn("[!struck]", p.read(f"{fx.LOG_DIR}/day-0198.md"))  # never copied as header


class CitationTests(unittest.TestCase):
    def test_a_day_summary_citing_a_struck_draft_is_refused(self):
        p = Project(self)
        play_into_day_197(p)
        struck = sorted(struck_lines(p, LEGACY))
        name = Path(LEGACY).name
        story = next(n for n, line in enumerate(p.read(LEGACY).splitlines(), 1)
                     if line.startswith("### Beat 4131:"))
        path, draft = "Work/rp/.gate/day-summary.md", p.root / "Work/rp/.gate/day-summary.md"
        for cite in (f"{struck[2]}", f"{struck[-1]}", f"{struck[0]}-{story}", f"{struck[0] - 2}-{struck[0]}",
                     f"{struck[0] - 1}-{struck[-1] + 1}",  # only the block, between its blank lines
                     f"{struck[0] - 1}-{story}", f"{struck[0] - 2}-{struck[-1] + 1}",  # one end on a blank line
                     f"{struck[-1] + 1}-{struck[0] - 1}"):  # written backwards
            with self.subTest(cite=cite):
                p.write(path, f"## Day 196 (Monday)\n\n- Vey hung the door ({name}:{cite}).\n")
                with self.assertRaises(ValueError) as caught:
                    rp_gate.publish_summary(p.root, DAY_197, 196, draft, today=TODAY)
                self.assertIn("struck", str(caught.exception))
        lines = p.read(LEGACY).split("\n")
        lines[struck[0] - 2] = " \t"  # a hand-edited blank line is blank too
        p.write(LEGACY, "\n".join(lines))
        p.write(path, f"## Day 196 (Monday)\n\n- Vey hung the door ({name}:{struck[0] - 1}-{story}).\n")
        with self.assertRaises(ValueError) as caught:
            rp_gate.publish_summary(p.root, DAY_197, 196, draft, today=TODAY)
        self.assertIn("struck", str(caught.exception))
        p.write(path, f"## Day 196 (Monday)\n\n- The axe ({name}:99999-{story}).\n")
        with self.assertRaises(ValueError) as caught:  # backwards, its far end past the file
            rp_gate.publish_summary(p.root, DAY_197, 196, draft, today=TODAY)
        self.assertIn("past the end", str(caught.exception))
        self.assertFalse((p.root / fx.SUMMARY_DIR / "day-0196.md").exists())
        p.write(path,  # a range around a struck draft, its ends off it (the lines next to it too), is fine
                f"## Day 196 (Monday)\n\n- The axe ({name}:{struck[0] - 2}-{story}; {name}:{struck[0] - 1}; "
                f"{name}:{struck[-1] + 1}; B4131).\n")
        self.assertEqual(rp_gate.publish_summary(p.root, DAY_197, 196, draft, today=TODAY)["action"], "published")

    def test_each_cited_file_is_checked_against_its_own_struck_drafts(self):
        p = Project(self)
        play_into_day_197(p)
        p.rotate(text=turn_text('currentDate: "Day 198 (Wednesday)"\nscene.time: "06:00"',
                                "Scene: Day 198, ~06:00, calm | Location: the kitchen | Present: Liss"), beat="4133")
        day_197 = sorted(struck_lines(p, DAY_197))
        self.assertTrue(day_197)
        self.assertFalse(set(day_197) & struck_lines(p, LEGACY))  # a line struck in one file is story in the other
        legacy, path = Path(LEGACY).name, "Work/rp/.gate/day-summary.md"
        draft = p.write(path, f"## Day 196 (Monday)\n\n- The axe ({legacy}:{day_197[2]}).\n"
                              f"- The door (day-0197.md:{day_197[2]}).\n")
        with self.assertRaises(ValueError) as caught:
            rp_gate.publish_summary(p.root, f"{fx.LOG_DIR}/day-0198.md", 196, draft, today=TODAY)
        self.assertIn("day-0197.md:", str(caught.exception))
        self.assertNotIn(f"{legacy}:", str(caught.exception))


class ContractTests(unittest.TestCase):
    def read(self, rel):
        return (fx.REPO / rel).read_text(encoding="utf-8")

    def test_the_day_summariser_is_given_the_parts(self):
        turn = self.read("plugins/rp/commands/turn.md")
        summarise = turn.split("2. Summarise the closed day", 1)[1].split("BREAK.", 1)[0]
        end = self.read("plugins/rp/commands/end.md").split("### Step 3", 1)[1].split("Then write", 1)[0]
        mode = self.read("plugins/rp/agents/timeline-search.md").split('### "DAY SUMMARY', 1)[1].split("\n### ", 1)[0]
        for name, text in (("turn.md 3e", summarise), ("end.md Step 3", end), ("timeline-search", mode)):
            with self.subTest(doc=name):
                self.assertIn("parts", text)

    def test_accept_anyway_ships_the_beat_file_of_3e(self):
        turn = self.read("plugins/rp/commands/turn.md")
        accept = turn.split("On (1) accept-anyway", 1)[1].split("On (2)", 1)[0]
        self.assertIn("beat.md", accept)  # on a rotated day the new Day Plan goes first, as in 3e

    def test_the_validator_log_lists_strike_lines(self):
        row = next(x for x in self.read("plugins/rp/README.md").splitlines() if "_validator_log.jsonl" in x)
        self.assertIn("`strike`", row)


if __name__ == "__main__":
    unittest.main()
