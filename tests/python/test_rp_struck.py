"""Tests for struck RP drafts in plugins/rp/tools/rp_gate.py: stash, write, and every reader skipping them.

Each risky ordering of the race model (strike, then rotate's publish, swap, footer and write; a crash between any
two; a retry) has a test here, named in its comment (S1-S16). Concurrent rotates are out of model.
"""
import importlib.util
import json
import re
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
_spec = importlib.util.spec_from_file_location("rp_struck_fixtures", HERE / "test_rp_rotation.py")
assert _spec is not None and _spec.loader is not None
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)
rp_gate = fx.rp_gate  # the module the fixtures drive
Project, LEGACY, LOG_DIR, DAY_197 = fx.Project, fx.LEGACY, fx.LOG_DIR, fx.DAY_197
NEXT_DAY, SAME_DAY, turn_text, beat, run, footers = (fx.NEXT_DAY, fx.SAME_DAY, fx.turn_text, fx.beat, fx.run,
                                                     fx.footers)

STASH = "Work/rp/.gate/struck"
TODAY = "2026-10-26"
SAME_197 = SAME_DAY.replace("Day 196", "Day 197")


def draft_turn(prose, delta="none", anchor_day=196):
    """A turn file whose DRAFT is prose plus its anchor."""
    return turn_text(delta, anchor_day=anchor_day).replace("Liss sets down the axe.", prose)


REJECTED = draft_turn("Vey hangs the door on its new hinges and steps back.")
REJECTED_2 = draft_turn("Vey sends word: the hinges are ready.")


def strike(p, text=REJECTED, attempt=1, beat_label="4131", reason="presence", session=None, **kw):
    session = session or rp_gate.resolve_session_file(p.root)["path"]
    kw.setdefault("today", TODAY)
    return rp_gate.strike(p.root, session, turn_text=text, attempt=attempt, beat=beat_label, reason=reason, **kw)


def inject(p, rel, before, prose, key="0123456789abcdef"):
    """Put a struck block into a file just before a heading (a reader test: rotate only ever appends)."""
    text = p.read(rel)
    block = rp_gate.struck_block(prose, key, "9999", 1, "presence", TODAY)
    at = text.index(before)
    (p.root / rel).write_text(text[:at] + block + "\n" + text[at:], encoding="utf-8")


def struck_lines(p, rel):
    lines = p.read(rel).splitlines()
    return {s + 1 + n for s, e in rp_gate.struck_spans(lines) for n in range(e - s)}


def read_lines(entry):
    return {n for a, b in entry.get("parts", [[entry["start"], entry["end"]]]) for n in range(a, b + 1)}


def stash_files(p):
    d = p.root / STASH
    return sorted(f.name for f in d.iterdir()) if d.is_dir() else []


def closing_lines(text):
    return re.findall(r"(?m)^> <!-- rp-struck: [0-9a-f]+ -->$", text)


def blocks(text):
    return re.findall(r"(?m)^> \[!struck\]", text)


class StrikeTests(unittest.TestCase):
    def test_strike_stashes_the_draft_and_writes_nothing_to_the_log(self):
        p = Project(self)
        before = p.read(LEGACY)
        got = strike(p)
        self.assertEqual(got["action"], "stashed")
        self.assertRegex(got["key"], r"^[0-9a-f]{16}$")
        self.assertEqual(stash_files(p), [got["key"] + ".json"])
        self.assertEqual(p.read(LEGACY), before)  # nothing reaches a log until rotate names its file
        entry = json.loads((p.root / STASH / (got["key"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual((entry["session"], entry["beat"], entry["attempt"], entry["reason"], entry["date"]),
                         (LEGACY, "4131", 1, "presence", TODAY))
        [line] = [x for x in p.log_lines() if x["path"] == "strike"]
        self.assertEqual((line["beat"], line["attempt"], line["reason"], line["key"]), ("4131", 1, "presence", got["key"]))

    def test_restriking_the_same_draft_is_a_no_op(self):
        # S2
        p = Project(self)
        first = strike(p)
        again = strike(p)
        self.assertEqual((again["action"], again["key"]), ("already", first["key"]))
        self.assertEqual(len(stash_files(p)), 1)

    def test_another_attempt_or_draft_is_another_strike(self):
        p = Project(self)
        keys = {strike(p)["key"], strike(p, attempt=2)["key"], strike(p, text=REJECTED_2)["key"],
                strike(p, beat_label="4132")["key"]}
        self.assertEqual(len(keys), 4)
        self.assertEqual(len(stash_files(p)), 4)

    def test_a_turn_without_a_draft_is_refused(self):
        p = Project(self)
        with self.assertRaises(ValueError):
            strike(p, text="KEEPER_INPUT:\nI go.\n\nDRAFT:\n\nSCENE_STATE_DELTA:\nnone\n")
        self.assertEqual(stash_files(p), [])

    def test_the_reason_and_label_stay_on_one_line(self):
        p = Project(self)
        strike(p, reason="presence,\nscene_state_mismatch", beat_label="41\n31")
        p.rotate(text=SAME_DAY)
        [title] = [x for x in p.read(LEGACY).splitlines() if x.startswith("> [!struck]")]
        self.assertIn("presence, scene_state_mismatch", title)

    def test_cli_strike(self):
        p = Project(self)
        tf = p.root / "Work/rp/.gate/turn.md"
        tf.parent.mkdir(parents=True)
        tf.write_text(REJECTED, encoding="utf-8")
        code, out, err = run("strike", "--project-dir", str(p.root), "--turn-file", str(tf), "--attempt", "2",
                             "--beat", "4131", "--reason", "surrendered: presence")
        self.assertEqual(code, 0, err)
        got = json.loads(out)
        self.assertEqual((got["action"], got["session"], got["attempt"]), ("stashed", LEGACY, 2))
        tf.write_text("KEEPER_INPUT:\nI go.\n", encoding="utf-8")
        code, _out, err = run("strike", "--project-dir", str(p.root), "--turn-file", str(tf), "--attempt", "3",
                              "--reason", "presence")
        self.assertEqual(code, 2)
        self.assertIn("no DRAFT", err)


class WriteTests(unittest.TestCase):
    def test_the_next_ship_writes_the_strikes_before_the_beat(self):
        # S1, S14: a strike survives a crash (nothing to do: it is on disk) and lands with the next ship.
        p = Project(self)
        k1 = strike(p, attempt=1)["key"]
        k2 = strike(p, text=REJECTED_2, attempt=2, reason="surrendered: presence")["key"]
        before = p.read(LEGACY)
        got = p.rotate(text=SAME_DAY, beat="4131")
        self.assertEqual((got["action"], got["path"]), ("none", LEGACY))
        self.assertEqual(got["struck"], [k1, k2])  # oldest first
        self.assertEqual(stash_files(p), [])
        text = p.read(LEGACY)
        self.assertTrue(text.startswith(before))
        tail = text[len(before):]
        self.assertEqual(len(blocks(tail)), 2)
        self.assertLess(tail.index("Vey hangs the door"), tail.index("Vey sends word"))
        self.assertIn(f"> <!-- rp-struck: {k1} -->", tail)
        self.assertTrue(text.endswith("\n\n"))  # a blank line ends the callout before the beat
        [rotate_line] = [x for x in p.log_lines() if x["path"] == "rotate"]
        self.assertEqual(rotate_line["struck"], [k1, k2])

    def test_the_block_names_its_beat_attempt_date_and_reason_and_quotes_every_line(self):
        p = Project(self)
        strike(p, attempt=3, reason="pc_authored, presence")
        p.rotate(text=SAME_DAY)
        lines = p.read(LEGACY).splitlines()
        start = next(i for i, x in enumerate(lines) if x.startswith("> [!struck]"))
        title = lines[start]
        self.assertRegex(title, r"^> \[!struck\]- STRUCK, not canon: Beat 4131, attempt 3, struck 2026-10-26\. "
                                r"Reason: pc_authored, presence$")
        block = []
        for line in lines[start:]:
            if not line.startswith(">"):
                break
            block.append(line)
        self.assertIn("> Vey hangs the door on its new hinges and steps back.", block)
        self.assertIn("> Scene: Day 196, ~06:00, calm | Location: the kitchen | Present: Liss", block)
        self.assertRegex(block[-1], r"^> <!-- rp-struck: [0-9a-f]{16} -->$")
        self.assertFalse(any("SCENE_STATE_DELTA" in x or "SOURCE_LOG" in x for x in block))  # the draft only

    def test_a_rotating_beat_takes_its_strikes_into_the_new_day_file(self):
        # S4, S13: the final draft's day decides the file; the old file gets only its footer.
        p = Project(self)
        key = strike(p)["key"]
        before = p.read(LEGACY)
        got = p.rotate(beat="4131")
        self.assertEqual((got["action"], got["path"], got["struck"]), ("rotated", DAY_197, [key]))
        old = p.read(LEGACY)
        self.assertEqual(blocks(old), [])
        self.assertTrue(old.startswith(before))
        self.assertEqual(footers(old), 1)
        self.assertEqual(rp_gate.rotation_footer(p.root / LEGACY), DAY_197)
        self.assertEqual(len(blocks(p.read(DAY_197))), 1)

    def test_a_flush_cut_short_before_cleanup_does_not_double(self):
        # S3: the block was appended, the stash entry survived.
        p = Project(self)
        key = strike(p)["key"]
        saved = (p.root / STASH / f"{key}.json").read_bytes()
        p.rotate(text=SAME_DAY)
        (p.root / STASH / f"{key}.json").write_bytes(saved)
        got = p.rotate(text=SAME_DAY)
        self.assertEqual(got["struck"], [key])  # written once already: the entry is cleared
        self.assertEqual(len(closing_lines(p.read(LEGACY))), 1)
        self.assertEqual(stash_files(p), [])

    def test_a_cut_short_flush_is_not_repeated_after_a_rotation(self):
        # S3b: the retry's beat rotates before the leftover entry is cleared.
        p = Project(self)
        key = strike(p)["key"]
        saved = (p.root / STASH / f"{key}.json").read_bytes()
        p.rotate(text=SAME_DAY)
        (p.root / STASH / f"{key}.json").write_bytes(saved)
        got = p.rotate()
        self.assertEqual(got["action"], "rotated")
        self.assertEqual(blocks(p.read(DAY_197)), [])
        self.assertEqual(len(closing_lines(p.read(LEGACY))), 1)
        self.assertEqual(stash_files(p), [])

    def test_a_strike_recorded_in_a_closed_file_goes_to_the_open_one(self):
        # S5: never a write after a footer.
        p = Project(self)
        p.rotate()
        key = strike(p, session=LEGACY)["key"]  # a stale caller still naming the closed file
        old = p.read(LEGACY)
        got = p.rotate(text=SAME_197)
        self.assertEqual((got["path"], got["struck"]), (DAY_197, [key]))
        self.assertEqual(p.read(LEGACY), old)
        self.assertEqual(len(blocks(p.read(DAY_197))), 1)

    def test_the_writer_never_writes_after_a_footer(self):
        # S5 at the point of writing: asked to write into a closed file, the append refuses.
        p = Project(self)
        p.rotate()  # closes LEGACY
        strike(p, session=LEGACY)
        result = {"warnings": []}
        old = p.read(LEGACY)
        rp_gate._write_struck(p.root, LEGACY, result)
        self.assertEqual(p.read(LEGACY), old)
        self.assertEqual(result["struck"], [])
        self.assertEqual(len(stash_files(p)), 1)
        self.assertTrue(any("closed" in w for w in result["warnings"]))

    def test_a_refused_rotation_writes_no_strike(self):
        # S10
        p = Project(self)
        (p.root / LOG_DIR).mkdir(parents=True)
        (p.root / DAY_197).write_text('---\nlog_day: 197\ncontinues: "Work/rp/other.md"\n---\n\n# Other run\n',
                                      encoding="utf-8")
        strike(p)
        old = p.read(LEGACY)
        got = p.rotate()
        self.assertEqual(got["action"], "refused")
        self.assertEqual(p.read(LEGACY), old)
        self.assertEqual(blocks(p.read(DAY_197)), [])
        self.assertEqual(len(stash_files(p)), 1)

    def test_a_crash_inside_the_rotation_writes_strikes_only_after_it_completes(self):
        # S12 (R1 with a strike waiting)
        p = Project(self)
        key = strike(p)["key"]
        with mock.patch.object(rp_gate, "_swap_marker", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                p.rotate()
        self.assertEqual(blocks(p.read(LEGACY)), [])
        self.assertEqual(blocks(p.read(DAY_197)), [])
        self.assertEqual(len(stash_files(p)), 1)
        got = p.rotate(session=LEGACY)
        self.assertEqual((got["action"], got["struck"]), ("rotated", [key]))
        self.assertEqual(len(blocks(p.read(DAY_197))), 1)
        self.assertEqual(footers(p.read(LEGACY)), 1)

    def test_a_crash_before_the_footer_still_writes_into_the_new_file(self):
        # S12 (R2): the marker names the new file; the repair closes the old one, the strike goes forward.
        p = Project(self)
        key = strike(p)["key"]
        with mock.patch.object(rp_gate, "_append_footer", side_effect=OSError("killed")):
            with self.assertRaises(OSError):
                p.rotate()
        got = p.rotate(text=SAME_197)
        self.assertEqual((got["path"], got["repaired"], got["struck"]), (DAY_197, LEGACY, [key]))
        self.assertEqual(blocks(p.read(LEGACY)), [])
        self.assertEqual(footers(p.read(LEGACY)), 1)

    def test_strikes_of_another_run_stay_put(self):
        # S11
        p = Project(self)
        other = "Work/rp/rp_session_2026-09-01.md"
        (p.root / other).write_text('---\ncampaign: "Other"\ncurrentDate: "Day 3"\n---\n\n# Other\n', encoding="utf-8")
        strike(p, session=other)
        gone = strike(p, text=REJECTED_2, session=LEGACY)["key"]
        entry = p.root / STASH / f"{gone}.json"
        data = json.loads(entry.read_text(encoding="utf-8"))
        data["session"] = "Work/rp/rp_session_2026-08-01.md"  # a run reset moved it away
        entry.write_text(json.dumps(data), encoding="utf-8")
        old = p.read(LEGACY)
        got = p.rotate(text=SAME_DAY)
        self.assertEqual(got["struck"], [])
        self.assertEqual(p.read(LEGACY), old)
        self.assertEqual(len(stash_files(p)), 2)
        self.assertTrue(any("rp_session_2026-08-01.md" in w for w in got["warnings"]))  # gone: reported
        self.assertFalse(any("rp_session_2026-09-01.md" in w for w in got["warnings"]))  # another run: quiet

    def test_an_unreadable_stash_entry_is_never_written(self):
        # S9
        p = Project(self)
        stash = p.root / STASH
        stash.mkdir(parents=True)
        (stash / "deadbeefdeadbeef.json").write_text('{"key": "dead', encoding="utf-8")
        (stash / ".0123456789abcdef.json.tmp-99").write_text("{}", encoding="utf-8")
        old = p.read(LEGACY)
        got = p.rotate(text=SAME_DAY)
        self.assertEqual(got["struck"], [])
        self.assertEqual(p.read(LEGACY), old)
        self.assertTrue(any("deadbeefdeadbeef.json" in w for w in got["warnings"]))
        self.assertTrue((stash / "deadbeefdeadbeef.json").exists())

    def test_end_repair_writes_pending_strikes(self):
        # S16: /rp:end's rotate --repair writes into the current file, before its END block.
        p = Project(self)
        key = strike(p)["key"]
        got = rp_gate.rotate(p.root, LEGACY, repair_only=True, today=TODAY)
        self.assertEqual(got["struck"], [key])
        self.assertEqual(len(blocks(p.read(LEGACY))), 1)
        code, out, err = run("rotate", "--repair", "--project-dir", str(p.root))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["struck"], [])

    def test_start_writes_leftovers_before_the_opening_mark(self):
        p = Project(self)
        strike(p)
        rp_gate.rotate(p.root, LEGACY, start=True, today=TODAY)
        text = p.read(LEGACY)
        self.assertLess(text.index("> [!struck]"), text.index("<!-- rp-session-opened:"))
        self.assertEqual(rp_gate.rotation_footer(p.root / LEGACY), None)


class ReaderTests(unittest.TestCase):
    def ship(self, p, n, day=196, strike_first=True, text=None):
        """Strike a draft for beat n, then ship it: rotate (which writes the strike), then the beat."""
        if strike_first:
            strike(p, text=text or draft_turn(f"Vey hangs door {n}.\n\n### Beat 9{n}: Day 300, ~01:00\n"
                                              f"## Day Plan: Day 300\n<!-- rp-rotated-to: x.md -->\n---"),
                   beat_label=str(n))
        got = p.rotate(text=turn_text("none", anchor_day=day))
        with open(p.root / got["path"], "a", encoding="utf-8") as fh:
            fh.write(beat(n, day))
        return got

    def test_struck_drafts_are_no_beats(self):
        # The day logic, beat parsing and the footer test never see a struck line.
        p = Project(self)
        lines = p.read(LEGACY).splitlines()
        units = len(rp_gate._beat_units(lines, rp_gate._body_start(lines)))
        self.ship(p, 4131)
        lines = p.read(LEGACY).splitlines()
        self.assertEqual(len(rp_gate._beat_units(lines, rp_gate._body_start(lines))), units + 1)
        data = rp_gate.read_frontmatter(p.root / LEGACY)
        self.assertEqual(rp_gate._days(data, lines), (196, 196))  # the quoted Day 300 is no beat
        strike(p, text=draft_turn("A last draft.\n<!-- rp-rotated-to: x.md -->"), beat_label="4132")
        rp_gate._write_struck(p.root, LEGACY, {"warnings": []})  # a block now ends the file
        self.assertIsNone(rp_gate.rotation_footer(p.root / LEGACY))
        self.assertEqual(rp_gate.resolve_session_file(p.root)["path"], LEGACY)
        got = p.rotate(text=SAME_DAY)
        self.assertEqual(got["action"], "none")

    def test_continuity_leaves_struck_drafts_out(self):
        # S7
        p = Project(self)
        for n in range(4131, 4134):
            self.ship(p, n)
        lines = p.read(LEGACY).splitlines()
        struck = {i for i, line in enumerate(lines, 1) if line.startswith(">")}
        self.assertTrue(struck)
        got = rp_gate.continuity(p.root, LEGACY, 20)
        cur = got["current"]
        self.assertEqual((cur["beats"], cur["last_beat"]), (9, "4133"))
        self.assertIn("parts", cur)
        self.assertEqual((cur["parts"][0][0], cur["parts"][-1][1]), (cur["start"], cur["end"]))
        read = {n for a, b in cur["parts"] for n in range(a, b + 1)}
        self.assertFalse(read & struck)
        self.assertTrue(all(n in read for n, line in enumerate(lines, 1)
                            if cur["start"] <= n <= cur["end"] and line.startswith("### Beat 413")))
        for a, b in cur["parts"]:
            self.assertTrue(lines[a - 1].strip() and lines[b - 1].strip())
        [prev] = got["previous"]
        self.assertEqual(prev["beats"], 11)  # struck blocks count toward no previousBeats

    def test_a_range_without_struck_lines_is_unchanged(self):
        p = Project(self)
        before = rp_gate.continuity(p.root, LEGACY, 20)
        self.assertNotIn("parts", before["current"])
        self.ship(p, 4131, strike_first=False)
        after = rp_gate.continuity(p.root, LEGACY, 20)
        self.assertNotIn("parts", after["current"])

    def test_struck_blocks_are_never_header(self):
        # S6: written right after the copied header of a new day file, before its Day Plan.
        p = Project(self)
        strike(p)
        p.rotate()
        new = p.read(DAY_197).splitlines()
        first = next(i for i, line in enumerate(new, 1) if line.startswith("> [!struck]"))
        state = rp_gate.live_state(p.root, DAY_197, today=TODAY)
        self.assertLess(state["header"]["end"], first)
        self.assertIsNone(rp_gate.continuity(p.root, DAY_197, 20)["current"])  # struck lines are no read
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n" + beat(4131, 197))
        cur = rp_gate.continuity(p.root, DAY_197, 20)["current"]
        self.assertGreater(cur["start"], first)
        self.assertTrue(p.read(DAY_197).splitlines()[cur["start"] - 1].startswith("## Day Plan: Day 197"))
        p.rotate(text=turn_text('currentDate: "Day 198 (Wednesday)"', anchor_day=198))
        copied = p.read(f"{LOG_DIR}/day-0198.md")
        self.assertEqual(blocks(copied), [])
        self.assertNotIn("Vey hangs the door", copied)

    def test_a_struck_block_inside_a_day_plan_section_is_left_out(self):
        p = Project(self)
        strike(p)
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n- Liss: the shed\n\n")
        strike(p, text=REJECTED_2, beat_label="4131")
        p.rotate(text=SAME_197)
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write(beat(4131, 197))
        lines = p.read(DAY_197).splitlines()
        struck = {i for i, line in enumerate(lines, 1) if line.startswith(">")}
        cur = rp_gate.continuity(p.root, DAY_197, 20)["current"]
        read = {n for a, b in cur.get("parts", [[cur["start"], cur["end"]]]) for n in range(a, b + 1)}
        self.assertFalse(read & struck)
        self.assertIn(next(i for i, x in enumerate(lines, 1) if x.startswith("## Day Plan")), read)

    def test_a_torn_block_neither_swallows_the_beat_nor_stops_a_rewrite(self):
        # S8
        p = Project(self)
        key = strike(p)["key"]
        entry = json.loads((p.root / STASH / f"{key}.json").read_text(encoding="utf-8"))
        torn = entry["block"].splitlines(keepends=True)[:3]
        with open(p.root / LEGACY, "a", encoding="utf-8") as fh:
            fh.write("\n" + "".join(torn) + beat(4131, 196))
        lines = p.read(LEGACY).splitlines()
        spans = rp_gate.struck_spans(lines)
        heading = next(i for i, x in enumerate(lines) if x.startswith("### Beat 4131 PRE-COMMIT"))
        self.assertTrue(spans and all(e <= heading for _s, e in spans))
        self.assertEqual(rp_gate._beat_units(lines, rp_gate._body_start(lines))[-1]["beat"], "4131")
        got = p.rotate(text=SAME_DAY)
        self.assertEqual(got["struck"], [key])
        text = p.read(LEGACY)
        self.assertEqual(len(closing_lines(text)), 1)  # the torn copy had none; the whole block is written
        self.assertEqual(len(blocks(text)), 2)

    def test_a_struck_line_in_the_header_region_names_no_previous_file(self):
        # The header ends at a struck draft, so a quoted "Continues `<file>`" in one is never the file before.
        p = Project(self, log_dir=False)
        older = "Work/rp/rp_session_2026-09-01.md"
        (p.root / older).write_text('---\ncampaign: "Other"\n---\n\n# Other\n', encoding="utf-8")
        text = p.read(LEGACY)
        p.legacy.write_text(text[:text.index("## Day Plan")], encoding="utf-8")  # header only: no body yet
        strike(p, text=draft_turn(f"Continues `{older}` as the GM imagines it."))
        self.assertTrue(p.rotate(text=SAME_DAY)["struck"])
        lines = p.read(LEGACY).splitlines()
        data = rp_gate.read_frontmatter(p.root / LEGACY)
        self.assertIsNone(rp_gate._previous_file(p.root, data, lines))

    def test_a_fenced_example_is_no_struck_block(self):
        lines = ["```markdown", "> [!struck]- STRUCK, not canon: Beat 1, attempt 1, struck 2026-10-26. Reason: x",
                 "> example", "```", "> [!struck]- STRUCK, not canon: attempt 2", "> real", "", "after"]
        self.assertEqual(rp_gate.struck_spans(lines), [(4, 6)])

    def test_session_reports_the_parts(self):
        p = Project(self)
        for n in (4131, 4132):
            self.ship(p, n)
        code, out, err = run("session", "--project-dir", str(p.root))
        self.assertEqual(code, 0, err)
        self.assertIn("parts", json.loads(out)["continuity"]["current"])


class ReviewFixTests(unittest.TestCase):
    """From the cold review: each read path on its own, the stash's order and wholeness, the header."""

    def test_a_long_day_window_leaves_struck_drafts_out(self):
        # whole_day false: the day holds more beats than the window
        p = Project(self)
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n" + "".join(beat(n, 197) for n in range(4131, 4156)))
        inject(p, DAY_197, "### Beat 4150 PRE-COMMIT", "Vey hangs the door.")
        cur = rp_gate.continuity(p.root, DAY_197, 20)["current"]
        self.assertFalse(cur["whole_day"])
        self.assertIn("parts", cur)
        self.assertFalse(read_lines(cur) & struck_lines(p, DAY_197))

    def test_earlier_beats_of_the_same_log_leave_struck_drafts_out(self):
        p = Project(self)
        inject(p, LEGACY, "### Beat 4118 PRE-COMMIT", "Vey hangs the door.")
        [prev] = rp_gate.continuity(p.root, LEGACY, 20)["previous"]
        self.assertEqual(prev["path"], LEGACY)
        self.assertIn("parts", prev)
        self.assertFalse(read_lines(prev) & struck_lines(p, LEGACY))

    def test_the_file_before_leaves_struck_drafts_out(self):
        p = Project(self)
        inject(p, LEGACY, "### Beat 4118 PRE-COMMIT", "Vey hangs the door.")
        p.rotate()
        [prev] = rp_gate.continuity(p.root, DAY_197, 20)["previous"]
        self.assertEqual(prev["path"], LEGACY)
        self.assertIn("parts", prev)
        self.assertFalse(read_lines(prev) & struck_lines(p, LEGACY))

    def test_a_day_plan_outside_the_window_leaves_struck_drafts_out(self):
        p = Project(self)
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n- Liss: the shed\n\n### Clocks\n\n- ~06:00 first light\n\n"
                     + "".join(beat(n, 197) for n in range(4131, 4156)))
        inject(p, DAY_197, "### Clocks", "Vey hangs the door.")
        [plan] = rp_gate.continuity(p.root, DAY_197, 20)["plans"]
        self.assertIn("parts", plan)
        self.assertFalse(read_lines(plan) & struck_lines(p, DAY_197))

    def test_struck_drafts_are_written_in_the_order_they_were_struck(self):
        p = Project(self)
        keys = [strike(p, text=draft_turn(f"Draft number {n}."), attempt=n)["key"] for n in range(1, 5)]
        by_name = sorted(keys)
        for rank, key in enumerate(reversed(by_name)):  # creation order is the reverse of name order
            path = p.root / STASH / f"{key}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            data["created"] = 1000 + rank
            path.write_text(json.dumps(data), encoding="utf-8")
        got = p.rotate(text=SAME_DAY)
        self.assertEqual(got["struck"], list(reversed(by_name)))
        text = p.read(LEGACY)
        self.assertEqual([text.index(f"rp-struck: {k}") for k in got["struck"]],
                         sorted(text.index(f"rp-struck: {k}") for k in got["struck"]))

    def test_an_entry_that_is_not_whole_is_never_written(self):
        p = Project(self)
        good = strike(p)["key"]
        data = json.loads((p.root / STASH / f"{good}.json").read_text(encoding="utf-8"))
        def keyed(key, block=data["block"]):  # a block whose closing line carries this key
            return block.replace(f"rp-struck: {good}", f"rp-struck: {key}")
        bad = {  # each breaks one check only
            "aaaaaaaaaaaaaaa1": {**data, "key": "aaaaaaaaaaaaaaa1", "block": data["block"].rsplit("> <!--", 1)[0]},
            "aaaaaaaaaaaaaaa2": {**data, "key": "aaaaaaaaaaaaaaa3", "block": keyed("aaaaaaaaaaaaaaa3")},
            "aaaaaaaaaaaaaaa4": {**data, "key": "aaaaaaaaaaaaaaa4", "block": keyed("aaaaaaaaaaaaaaa4"), "created": "soon"},
            "aaaaaaaaaaaaaaa5": {**data, "key": "aaaaaaaaaaaaaaa5",  # no callout: it would be written as story
                                 "block": "Vey hangs the door.\n" + rp_gate.STRUCK_CLOSE.format("aaaaaaaaaaaaaaa5") + "\n"},
        }
        (p.root / STASH / f"{good}.json").unlink()
        for name, entry in bad.items():
            (p.root / STASH / f"{name}.json").write_text(json.dumps(entry), encoding="utf-8")
        old = p.read(LEGACY)
        got = p.rotate(text=SAME_DAY)
        self.assertEqual((got["struck"], p.read(LEGACY)), ([], old))
        self.assertEqual(len([w for w in got["warnings"] if "not a whole struck draft" in w]), 4)

    def test_a_block_always_follows_a_blank_line(self):
        for tail in ("Liss sets down the axe.\n", "Liss sets down the axe."):
            with self.subTest(tail=repr(tail)):
                p = Project(self)
                with open(p.root / LEGACY, "a", encoding="utf-8") as fh:
                    fh.write(tail)
                strike(p)
                p.rotate(text=SAME_DAY)
                lines = p.read(LEGACY).splitlines()
                at = next(i for i, x in enumerate(lines) if x.startswith("> [!struck]"))
                self.assertEqual((lines[at - 2], lines[at - 1]), ("Liss sets down the axe.", ""))

    def test_header_text_after_a_struck_block_is_still_header(self):
        p = Project(self)
        strike(p)
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("**Steer (Keeper):** the bit fires on her orders only.\n\n## Day Plan: Day 197 (Tuesday)\n\n"
                     + beat(4131, 197))
        lines = p.read(DAY_197).splitlines()
        steer = next(i for i, x in enumerate(lines, 1) if x.startswith("**Steer"))
        header = rp_gate.live_state(p.root, DAY_197, today=TODAY)["header"]
        self.assertIn(steer, read_lines(header))
        self.assertFalse(read_lines(header) & struck_lines(p, DAY_197))
        p.rotate(text=turn_text('currentDate: "Day 198 (Wednesday)"', anchor_day=198))
        copied = p.read(f"{LOG_DIR}/day-0198.md")
        self.assertIn("**Steer (Keeper):** the bit fires", copied)
        self.assertEqual(blocks(copied), [])

    def test_a_fenced_struck_example_in_the_header_does_not_end_it(self):
        p = Project(self)
        text = p.read(LEGACY)
        example = "```markdown\n> [!struck]- STRUCK, not canon: attempt 1, struck 2026-10-26. Reason: x\n> a draft\n```\n\n"
        at = text.index("## What is on him")
        (p.root / LEGACY).write_text(text[:at] + example + text[at:], encoding="utf-8")
        lines = p.read(LEGACY).splitlines()
        threads = next(i for i, x in enumerate(lines, 1) if x.startswith("## Open threads"))
        header = rp_gate.live_state(p.root, LEGACY, today=TODAY)["header"]
        self.assertIn(threads, read_lines(header))
        p.rotate()
        self.assertIn("## Open threads", p.read(DAY_197))
        self.assertIn("> a draft", p.read(DAY_197))  # an example is copied as written

    def test_a_new_session_file_that_continues_the_old_one_takes_its_strikes(self):
        # /rp:end skipped; the next session opens a new legacy file whose header continues the old one.
        p = Project(self, log_dir=False)
        key = strike(p)["key"]
        new = "Work/rp/rp_session_2026-10-27.md"
        (p.root / new).write_text(f'---\ncampaign: "Test Run"\ncurrentDate: "Day 196"\n---\n\n# Next\n\n'
                                  f"Continues `{LEGACY}`.\n\n## Day Plan: Day 196\n\n", encoding="utf-8")
        got = rp_gate.rotate(p.root, new, start=True, today=TODAY)
        self.assertEqual(got["struck"], [key])
        self.assertEqual(len(blocks(p.read(new))), 1)
        self.assertEqual(blocks(p.read(LEGACY)), [])

    def test_a_struck_footer_quote_raises_no_footer_warning(self):
        p = Project(self)
        strike(p, text=draft_turn("<!-- rp-rotated-to: Work/rp/x.md -->"))
        p.rotate(text=SAME_DAY)
        with open(p.root / LEGACY, "a", encoding="utf-8") as fh:
            fh.write(beat(4131, 196))
        code, _out, err = run("session", "--project-dir", str(p.root))
        self.assertEqual(code, 0, err)
        self.assertNotIn("rotation footer appears", err)

    def test_a_bad_entry_never_stops_a_rotation(self):
        # Struck drafts cost play nothing: a broken stash entry never keeps a beat from shipping.
        p = Project(self)
        key = strike(p)["key"]
        bad = strike(p, text=REJECTED_2)["key"]
        path = p.root / STASH / f"{bad}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["session"] = "Work/rp/bad\u0000name.md"
        path.write_text(json.dumps(data), encoding="utf-8")
        got = p.rotate()
        self.assertEqual((got["action"], got["path"], got["struck"]), ("rotated", DAY_197, [key]))
        self.assertTrue(any(bad in w for w in got["warnings"]))
        self.assertTrue(path.exists())

    def test_a_corrupt_entry_is_rewritten_by_a_new_strike(self):
        p = Project(self)
        key = strike(p)["key"]
        (p.root / STASH / f"{key}.json").write_text('{"key": "', encoding="utf-8")
        self.assertEqual(strike(p)["action"], "stashed")
        got = p.rotate(text=SAME_DAY)
        self.assertEqual(got["struck"], [key])

    def test_start_writes_leftovers_into_the_file_the_session_ended_in(self):
        # A session that ended without /rp:end: its drafts belong with it, not with the next day's file.
        p = Project(self)
        key = strike(p)["key"]
        got = rp_gate.rotate(p.root, LEGACY, start=True, day=197, today=TODAY)
        self.assertEqual((got["action"], got["struck"]), ("rotated", [key]))
        old = p.read(LEGACY)
        self.assertEqual(len(blocks(old)), 1)
        self.assertLess(old.index("> [!struck]"), old.index("<!-- rp-rotated-to:"))
        self.assertEqual(rp_gate.rotation_footer(p.root / LEGACY), DAY_197)
        self.assertEqual(blocks(p.read(DAY_197)), [])


class SecondReviewTests(unittest.TestCase):
    """From the second cold review: cost with another run's strikes waiting, the header copy, one bad entry."""

    def test_strikes_of_another_run_do_not_slow_every_rotate(self):
        # W1: the walk back from the named file is the same for every entry: walk it once.
        p = Project(self)
        other = "Work/rp/rp_session_2026-09-01.md"
        (p.root / other).write_text('---\ncampaign: "Other"\ncurrentDate: "Day 3"\n---\n\n# Other\n', encoding="utf-8")
        for n in range(4):
            strike(p, text=draft_turn(f"Other run draft {n}."), session=other)
        calls = []
        original = rp_gate._previous_file
        def counted(*a, **k):
            calls.append(1)
            return original(*a, **k)
        with mock.patch.object(rp_gate, "_previous_file", counted):
            p.rotate(text=SAME_DAY)
        self.assertLessEqual(len(calls), rp_gate.MAX_CHAIN_BACK + 1)
        self.assertLessEqual(len(calls), 2)

    def test_a_big_header_with_struck_drafts_still_rotates(self):
        # M2: struck lines never use up the header copy limit.
        p = Project(self)
        text = p.read(LEGACY)
        at = text.index("## Open threads")
        filler = "".join(f"- standing fact {n}\n" for n in range(1940))
        p.legacy.write_text(text[:at] + filler + "\n" + text[at:], encoding="utf-8")
        for n in range(4):
            strike(p, text=draft_turn("\n".join(f"Draft line {i}." for i in range(30))), attempt=n + 1)
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("## Day Plan: Day 197 (Tuesday)\n\n" + beat(4131, 197))
        got = p.rotate(text=turn_text('currentDate: "Day 198 (Wednesday)"', anchor_day=198))
        self.assertEqual(got["action"], "rotated")
        self.assertIn("- standing fact 1939", p.read(f"{LOG_DIR}/day-0198.md"))

    def test_one_entry_that_cannot_be_written_holds_back_no_other(self):
        # M3
        p = Project(self)
        bad = strike(p, text=REJECTED_2)["key"]
        path = p.root / STASH / f"{bad}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["block"] = data["block"].replace("> Vey sends word", "> \ud800 Vey sends word")
        data["created"] = 1
        path.write_text(json.dumps(data), encoding="utf-8")
        good = strike(p)["key"]
        got = p.rotate(text=SAME_DAY)
        self.assertEqual(got["struck"], [good])
        self.assertTrue(path.exists())
        self.assertTrue(any(bad in w for w in got["warnings"]))

    def test_a_footer_quoted_in_a_struck_draft_does_not_end_the_header_copy(self):
        # M4
        p = Project(self)
        strike(p, text=draft_turn("<!-- rp-rotated-to: Work/rp/x.md -->"))
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("**Steer (Keeper):** plain sentences.\n\n## Day Plan: Day 197 (Tuesday)\n\n" + beat(4131, 197))
        p.rotate(text=turn_text('currentDate: "Day 198 (Wednesday)"', anchor_day=198))
        copied = p.read(f"{LOG_DIR}/day-0198.md")
        self.assertIn("**Steer (Keeper):** plain sentences.", copied)
        self.assertNotIn("rp-rotated-to: Work/rp/x.md", copied)

    def test_struck_lines_do_not_use_up_the_header_cap(self):
        p = Project(self)
        text = p.read(LEGACY)
        at = text.index("## Open threads")
        p.legacy.write_text(text[:at] + "".join(f"- fact {n}\n" for n in range(180)) + "\n" + text[at:],
                            encoding="utf-8")
        strike(p, text=draft_turn("\n".join(f"Draft line {i}." for i in range(80))))
        p.rotate()
        with open(p.root / DAY_197, "a", encoding="utf-8") as fh:
            fh.write("**Steer (Keeper):** plain sentences.\n\n## Day Plan: Day 197 (Tuesday)\n\n")
        header = rp_gate.live_state(p.root, DAY_197, today=TODAY)["header"]
        self.assertFalse(header["truncated"])
        steer = next(i for i, x in enumerate(p.read(DAY_197).splitlines(), 1) if x.startswith("**Steer"))
        self.assertIn(steer, read_lines(header))

    def test_a_strike_two_files_back_reaches_the_head(self):
        p = Project(self, log_dir=False)
        key = strike(p)["key"]
        middle, head = "Work/rp/rp_session_2026-10-27.md", "Work/rp/rp_session_2026-10-28.md"
        for rel, before in ((middle, LEGACY), (head, middle)):
            (p.root / rel).write_text(f'---\ncampaign: "Test Run"\ncurrentDate: "Day 196"\n---\n\n# Next\n\n'
                                      f"Continues `{before}`.\n\n## Day Plan: Day 196\n\n", encoding="utf-8")
        got = rp_gate.rotate(p.root, head, start=True, today=TODAY)
        self.assertEqual(got["struck"], [key])
        self.assertEqual(len(blocks(p.read(head))), 1)

    def test_a_struck_state_line_is_never_the_run_state_pointer(self):
        p = Project(self)
        (p.root / "Work/rp/other.md").write_text("# Other\n\n## Run state\n\nDay 3.\n", encoding="utf-8")
        inject(p, LEGACY, "# RP Session", "State: `Work/rp/other.md` § Run state")
        state = rp_gate.live_state(p.root, LEGACY, today=TODAY)
        self.assertEqual(state["run_state"]["path"], "Work/rp/test/alder.md")


class ContractTests(unittest.TestCase):
    def read(self, rel):
        return (REPO / rel).read_text(encoding="utf-8")

    def test_turn_strikes_before_the_rewrite_and_on_surrender(self):
        # S13, S15
        text = self.read("plugins/rp/commands/turn.md")
        adjudicate = text.split("# 3d.", 1)[1].split("# 3e.", 1)[0]
        self.assertIn('rp_gate.py" strike', adjudicate)
        self.assertLess(adjudicate.index('rp_gate.py" strike'), adjudicate.index("continue LOOP"))
        surrender = text.split("# 3f.", 1)[1].split("END LOOP", 1)[0]
        self.assertIn('rp_gate.py" strike', surrender)
        self.assertNotIn("APPEND DRAFT", surrender)  # a surrendered draft is struck, never appended as is
        self.assertIn("3e", surrender)  # acceptance ships through the ship path
        self.assertIn("parts", text)  # LIVE_STATE leaves struck lines out

    def test_every_reader_is_told_a_struck_block_is_not_story(self):
        for rel in ("plugins/rp/agents/rp-ratifier.md", "plugins/rp/agents/canon-writer.md",
                    "plugins/rp/agents/timeline-search.md", "plugins/rp/agents/world-lookup.md",
                    "plugins/rp/agents/roleplay-gm.md", "plugins/rp/agents/character-template.md",
                    "plugins/rp/commands/end.md", "plugins/write/agents/continuity-reviewer.md",
                    "plugins/rp/README.md", "plugins/rp/commands/extract-invariants.md",
                    "plugins/rp/commands/start.md"):
            with self.subTest(rel=rel):
                self.assertIn("[!struck]", self.read(rel))


    def test_turn_reads_the_session_range_around_struck_drafts_and_reports_their_warnings(self):
        text = self.read("plugins/rp/commands/turn.md")
        step2 = text.split("### Step 2", 1)[1].split("### Step 3", 1)[0]
        self.assertIn("header.start", step2)  # a struck draft at the top of the header is not read
        failures = text.split("## Failure Modes", 1)[1].split("\n## ", 1)[0]
        self.assertIn("struck", failures.split("rp_gate.py strike", 1)[0] + failures.split("rp_gate.py strike", 1)[1])
        self.assertRegex(failures, r"(?m)^\| `rotate` warns about struck drafts")

    def test_strike_command_lines_carry_the_beat(self):
        text = self.read("plugins/rp/commands/turn.md")
        lines = [x for x in text.splitlines() if 'rp_gate.py" strike' in x]
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIn("--beat", line)

    def test_end_reads_past_struck_drafts_for_where_play_stopped(self):
        end = self.read("plugins/rp/commands/end.md")
        step2 = end.split("### Step 2", 1)[1].split("\n### ", 1)[0]
        step4 = end.split("### Step 4", 1)[1].split("\n### ", 1)[0]
        for name, part in (("Step 2", step2), ("Step 4", step4)):
            with self.subTest(step=name):
                self.assertIn("[!struck]", part)


if __name__ == "__main__":
    unittest.main()
