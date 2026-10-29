"""Tests for background review in plugins/rp/tools/rp_gate.py: provisional ships, settle, and every reader
refusing a beat whose review is still pending.

Each risky ordering of the concurrency model (O1-O18: a verdict arriving late, for the wrong beat, after a crash,
over a hand edit, with a rotation, a summary or /rp:end in between) has a test here, named in its comment.
"""
import importlib.util
import json
import os
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
_spec = importlib.util.spec_from_file_location("rp_background_fixtures", HERE / "test_rp_rotation.py")
assert _spec is not None and _spec.loader is not None
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)
rp_gate = fx.rp_gate
Project, LEGACY, NEXT_DAY, beat, run = fx.Project, fx.LEGACY, fx.NEXT_DAY, fx.beat, fx.run

TODAY = "2026-10-28"
PENDING = "Work/rp/.gate/pending"


def bg_turn(delta='scene.time: "19:05"\nscene.mood: "tense"', prose="Liss hangs the kettle on its hook.",
            day=196, time="19:05", priced=True, keeper="I wait."):
    """A lint-clean turn: the anchor agrees with the delta, the cast is the scene's."""
    return (f"KEEPER_INPUT:\n{keeper}\n\nDRAFT:\n{prose}\n\n"
            f"Scene: Day {day}, ~{time}, calm | Location: the kitchen | Present: Liss\n\n"
            f"GM_SPAWN_LOG:\n- liss: NOT spawned\n\n"
            f"SOURCE_LOG:\npriced_stake_touched: {'true' if priced else 'false'}\n\n"
            f"SCENE_STATE_DELTA:\n{delta}\n")


TURN = bg_turn()
TURN_2 = bg_turn('scene.time: "19:10"', prose="Liss pours the water out.", time="19:10", keeper="I watch her.")
BEAT = beat(4131, 196)
BEAT_2 = beat(4132, 196)


def ship(p, text=TURN, append=BEAT, label="4131", provisional=True, session=LEGACY, **kw):
    kw.setdefault("today", TODAY)
    extra = {"provisional": True, "attempt": kw.pop("attempt", 1), "max_rewrites": kw.pop("max_rewrites", 3)} \
        if provisional else {}
    return rp_gate.ship(p.root, session, turn_text=text, append_text=append, beat=label, **extra, **kw)


def settle(p, key, verdict="clean", **kw):
    kw.setdefault("today", TODAY)
    return rp_gate.settle(p.root, key, verdict=verdict, **kw)


def frontmatter(text):
    return text.split("\n---\n", 1)[0]


def pending_dirs(p):
    d = p.root / PENDING
    return sorted(x.name for x in d.iterdir()) if d.is_dir() else []


class ProvisionalShipTests(unittest.TestCase):
    def test_provisional_ship_wraps_the_beat_and_keeps_what_the_review_needs(self):
        p = Project(self)
        before = (p.root / LEGACY).read_bytes()
        got = ship(p)
        key = got["key"]
        self.assertEqual(got["action"], "shipped")
        self.assertRegex(key, r"^[0-9a-f]{12}$")
        text = p.read(LEGACY)
        self.assertIn(f"<!-- rp-provisional: {key} -->\n{BEAT}", text)
        self.assertTrue(text.rstrip("\n").endswith(f"<!-- /rp-provisional: {key} -->"))
        self.assertIn('mood: "tense"', frontmatter(text))  # the delta is applied: the beat is shown and played on
        [entry] = rp_gate.pending_reviews(p.root)
        self.assertEqual((entry["key"], entry["session"], entry["beat"], entry["attempt"], entry["max_rewrites"],
                          entry["shipped"]), (key, LEGACY, "4131", 1, 3, True))
        self.assertEqual(entry["keeper_input"], "I wait.")
        # O12: the reviewer reads a byte copy of the file as it was before the ship
        self.assertEqual((p.root / entry["review_session"]).read_bytes(), before)
        self.assertEqual(got["review_session"], entry["review_session"])
        self.assertEqual((p.root / entry["turn"]).read_text(encoding="utf-8"), TURN)
        [line] = [x for x in p.log_lines() if x["path"] == "ship"]
        self.assertEqual((line["provisional"], line["key"]), (True, key))

    def test_settle_clean_removes_markers_and_keeps_the_delta(self):
        # O1: once settled, the file is exactly what a reviewed ship would have written
        p, q = Project(self), Project(self)
        key = ship(p)["key"]
        got = settle(p, key, "clean")
        self.assertEqual(got["action"], "settled")
        ship(q, provisional=False)
        self.assertEqual(p.read(LEGACY), q.read(LEGACY))
        self.assertEqual(pending_dirs(p), [])
        [line] = [x for x in p.log_lines() if x["path"] == "settle"]
        self.assertEqual((line["key"], line["verdict"], line["beat"]), (key, "clean", "4131"))

    def test_strike_then_redraft_ships_on_the_state_before_the_beat(self):
        # O14: a confirmed hard finding takes the shown beat out of story and unwinds exactly its delta
        p = Project(self)
        before = p.read(LEGACY)
        key = ship(p)["key"]
        got = settle(p, key, "struck", violations=["pc_authored"], reason="pc_authored")
        self.assertEqual(got["action"], "struck")
        self.assertEqual((got["beat"], got["attempt"], got["max_rewrites"], got["keeper_input"]),
                         ("4131", 1, 3, "I wait."))
        text = p.read(LEGACY)
        self.assertEqual(frontmatter(text), frontmatter(before))
        self.assertNotIn("rp-provisional", text)
        self.assertIn("> [!struck]- STRUCK, not canon: Beat 4131, attempt 1", text)
        self.assertIn("> ### Beat 4131 PRE-COMMIT", text)  # the beat as it shipped, its working too
        self.assertNotIn("\n### Beat 4131:", text)  # no beat heading left for any reader
        self.assertEqual(pending_dirs(p), [])
        redraft = ship(p, text=TURN_2, attempt=2, provisional=False)
        self.assertEqual(redraft["action"], "shipped")
        text = p.read(LEGACY)
        self.assertIn('time: "19:10"', frontmatter(text))
        self.assertNotIn("tense", frontmatter(text))  # the struck delta stays unwound
        self.assertLess(text.index("> [!struck]"), text.rindex("### Beat 4131:"))
        spans = rp_gate.struck_spans(text.splitlines())
        self.assertEqual(len(spans), 1)
        [strike_line] = [x for x in p.log_lines() if x["path"] == "strike"]
        self.assertEqual((strike_line["beat"], strike_line["shipped"]), ("4131", True))

    def test_a_day_changing_beat_is_never_provisional(self):
        # O4: a rotation cannot be undone, so a beat that moves the day is reviewed before it ships
        p = Project(self)
        before = p.read(LEGACY)
        with self.assertRaises(ValueError) as caught:
            ship(p, text=NEXT_DAY)
        self.assertIn("day", str(caught.exception))
        self.assertEqual(p.read(LEGACY), before)
        self.assertEqual(pending_dirs(p), [])

    def test_only_a_first_attempt_ships_provisionally(self):
        p = Project(self)
        with self.assertRaises(ValueError):
            ship(p, attempt=2)
        with self.assertRaises(ValueError):
            ship(p, append=None)


class OrderingTests(unittest.TestCase):
    def test_ship_refuses_while_a_beat_is_pending(self):
        # O3
        p = Project(self)
        ship(p)
        before = p.read(LEGACY)
        with self.assertRaises(ValueError) as caught:
            ship(p, text=TURN_2, append=BEAT_2, label="4132", provisional=False)
        self.assertIn("pending", str(caught.exception))
        self.assertEqual(p.read(LEGACY), before)

    def test_provisional_ship_refuses_a_second_pending_beat(self):
        # O5
        p = Project(self)
        first = ship(p)["key"]
        before = p.read(LEGACY)
        with self.assertRaises(ValueError):
            ship(p, text=TURN_2, append=BEAT_2, label="4132")
        self.assertEqual(p.read(LEGACY), before)
        self.assertEqual(pending_dirs(p), [first])

    def test_reshipping_the_same_beat_is_a_no_op(self):
        p = Project(self)
        first = ship(p)
        before = p.read(LEGACY)
        again = ship(p)
        self.assertEqual((again["action"], again["key"]), ("already_shipped", first["key"]))
        self.assertEqual(p.read(LEGACY), before)

    def test_a_late_verdict_for_a_settled_beat_is_stale(self):
        # O2, O8
        p = Project(self)
        key = ship(p)["key"]
        settle(p, key, "clean")
        before = p.read(LEGACY)
        got = settle(p, key, "struck", violations=["presence"])
        self.assertEqual(got["action"], "stale")
        self.assertEqual(p.read(LEGACY), before)

    def test_a_stale_key_never_touches_the_next_pending_beat(self):
        # O2: the review of N lands after N was settled by a synchronous rerun and N+1 is pending
        p = Project(self)
        first = ship(p)["key"]
        settle(p, first, "clean")
        second = ship(p, text=TURN_2, append=BEAT_2, label="4132")["key"]
        before = p.read(LEGACY)
        self.assertEqual(settle(p, first, "struck")["action"], "stale")
        self.assertEqual(p.read(LEGACY), before)
        self.assertEqual(pending_dirs(p), [second])

    def test_rotate_refuses_while_a_beat_is_pending(self):
        # O4: rotation never runs over an unreviewed beat, whatever the mode
        p = Project(self)
        ship(p)
        before, marker = p.read(LEGACY), p.read("Work/markers/rp-active")
        for kw in ({}, {"repair_only": True}, {"start": True, "day": 197}):
            with self.subTest(kw=kw):
                got = p.rotate(**kw)
                self.assertEqual(got["action"], "refused")
                self.assertTrue(any("pending" in w for w in got["warnings"]))
        self.assertEqual(p.read(LEGACY), before)
        self.assertEqual(p.read("Work/markers/rp-active"), marker)
        self.assertFalse((p.root / fx.DAY_197).exists())

    def test_end_steps_refuse_while_a_beat_is_pending(self):
        # O7: /rp:end collects first; its own steps refuse an unreviewed beat
        p = Project(self)
        ship(p)
        self.assertEqual(rp_gate.refresh_state(p.root, LEGACY)["action"], "refused")
        extra = p.root / "Work/rp/.gate/extra.md"
        extra.write_text("## Open threads\n\n- none\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            rp_gate.stitch_summary(p.root, LEGACY, "Work/rp/summaries/2026-10-28.md", extra, today=TODAY)
        self.assertIn("pending", str(caught.exception))

    def test_publish_refuses_a_day_with_a_provisional_beat(self):
        # O13: only by hand can a closed day hold a provisional beat; its summary is refused all the same
        p = Project(self)
        p.rotate()
        text = p.read(LEGACY)
        at = text.index("### Beat 4130 PRE-COMMIT")
        (p.root / LEGACY).write_text(text[:at] + "<!-- rp-provisional: 0123456789ab -->\n" + text[at:],
                                     encoding="utf-8")
        summary = p.root / "Work/rp/.gate/day-summary.md"
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text("## Day 196\n\nThe kettle.\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            rp_gate.publish_summary(p.root, fx.DAY_197, 196, summary, today=TODAY)
        self.assertIn("provisional", str(caught.exception))

    def test_strike_refuses_after_a_hand_edit(self):
        # O11: a strike never writes over a hand edit; a clean settle only drops the markers
        for where in ("frontmatter", "region"):
            with self.subTest(where=where):
                p = Project(self)
                key = ship(p)["key"]
                text = p.read(LEGACY)
                if where == "frontmatter":
                    text = text.replace('mood: "tense"', 'mood: "grim"', 1)
                else:  # the provisional beat is the file's last
                    at = text.rindex("Liss sets down the axe.")
                    text = text[:at] + "Liss sets down the saw." + text[at + len("Liss sets down the axe."):]
                (p.root / LEGACY).write_text(text, encoding="utf-8")
                got = settle(p, key, "struck", violations=["presence"])
                self.assertEqual(got["action"], "refused")
                self.assertEqual(p.read(LEGACY), text)
                self.assertEqual(pending_dirs(p), [key])
                self.assertEqual(settle(p, key, "clean")["action"], "settled")
                self.assertNotIn("rp-provisional", p.read(LEGACY))

    def test_strike_keeps_text_after_the_region(self):
        # O16: ungated text appended after the provisional beat survives its strike
        p = Project(self)
        key = ship(p)["key"]
        with open(p.root / LEGACY, "a", encoding="utf-8") as fh:
            fh.write("\nOOC: the Keeper steps away.\n")
        settle(p, key, "struck", violations=["presence"])
        text = p.read(LEGACY)
        self.assertTrue(text.rstrip().endswith("OOC: the Keeper steps away."))
        self.assertLess(text.index("> [!struck]"), text.index("OOC: the Keeper"))

    def test_another_runs_pending_review_does_not_block(self):
        # O18
        p = Project(self)
        ship(p)
        other = "Work/rp/rp_session_2026-10-20.md"
        (p.root / other).write_text(fx.LEGACY_FRONTMATTER.format(extra="") + "\n# Other run\n\n"
                                    + beat(10, 196), encoding="utf-8")
        got = ship(p, session=other, provisional=False, label="11", append=beat(11, 196))
        self.assertEqual(got["action"], "shipped")
        self.assertEqual(rp_gate.rotate(p.root, other, turn_text=TURN, today=TODAY)["action"], "none")


class CrashTests(unittest.TestCase):
    def test_crash_before_the_ship_write_reruns_once(self):
        # O9: the pending review is written before the file; a crash between them reruns cleanly
        p = Project(self)
        before = (p.root / LEGACY).read_bytes()
        with mock.patch.object(rp_gate, "_replace_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                ship(p)
        [entry] = rp_gate.pending_reviews(p.root)
        self.assertFalse(entry["shipped"])
        self.assertEqual((p.root / LEGACY).read_bytes(), before)
        self.assertEqual(p.rotate(text=TURN)["action"], "none")  # an unshipped review blocks nothing
        got = ship(p)
        self.assertEqual(got["action"], "shipped")
        self.assertEqual(p.read(LEGACY).count("<!-- rp-provisional: "), 1)
        [entry] = rp_gate.pending_reviews(p.root)
        self.assertTrue(entry["shipped"])
        self.assertEqual((p.root / entry["review_session"]).read_bytes(), before)

    def test_settle_of_an_unshipped_beat(self):
        # O9: a beat that never reached the file was never shown: clean waits for its ship, struck is kept
        p = Project(self)
        with mock.patch.object(rp_gate, "_replace_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                ship(p)
        [entry] = rp_gate.pending_reviews(p.root)
        self.assertEqual(settle(p, entry["key"], "clean")["action"], "unshipped")
        self.assertEqual(pending_dirs(p), [entry["key"]])
        got = settle(p, entry["key"], "struck", violations=["presence"])
        self.assertEqual(got["action"], "dropped")
        self.assertEqual(pending_dirs(p), [])
        self.assertEqual(len(list((p.root / "Work/rp/.gate/struck").glob("*.json"))), 1)

    def test_a_settle_rerun_after_a_crash_writes_nothing_twice(self):
        # O10
        for verdict in ("clean", "struck"):
            with self.subTest(verdict=verdict):
                p = Project(self)
                key = ship(p)["key"]
                with mock.patch.object(rp_gate.shutil, "rmtree", side_effect=OSError("busy")):
                    with self.assertRaises(OSError):
                        settle(p, key, verdict, violations=["presence"])
                written = p.read(LEGACY)
                self.assertNotIn("rp-provisional", written)
                got = settle(p, key, verdict, violations=["presence"])
                self.assertEqual(got["action"], "already")
                self.assertEqual(p.read(LEGACY), written)
                self.assertEqual(pending_dirs(p), [])

    def test_pending_survives_a_new_process_and_settles_from_disk(self):
        # O6: a reviewer that crashed, never finished, or died with its Room leaves everything on disk
        p = Project(self)
        before = p.read(LEGACY)
        gate = p.root / "Work/rp/.gate"
        gate.mkdir(parents=True, exist_ok=True)
        (gate / "turn.md").write_text(TURN, encoding="utf-8")
        (gate / "beat.md").write_text(BEAT, encoding="utf-8")
        code, out, err = run("ship", "--project-dir", str(p.root), "--turn-file", str(gate / "turn.md"),
                             "--append-file", str(gate / "beat.md"), "--beat", "4131", "--provisional",
                             "--attempt", "1", "--max-rewrites", "3")
        self.assertEqual(code, 0, err)
        key = json.loads(out)["key"]
        code, out, err = run("pending", "--project-dir", str(p.root))
        self.assertEqual(code, 0, err)
        [entry] = json.loads(out)["pending"]
        self.assertEqual((entry["key"], entry["shipped"]), (key, True))
        self.assertGreaterEqual(entry["age_s"], 0)
        snapshot = entry["review_session"]  # a synchronous rerun reads the copy, never the live file
        self.assertTrue(entry["live_state"].startswith(f"LIVE_STATE:\n  session: {snapshot} lines 1-"))
        self.assertIn(f"  day: {snapshot} lines ", entry["live_state"])
        self.assertNotIn(LEGACY, entry["live_state"])
        code, out, err = run("session", "--project-dir", str(p.root))
        self.assertEqual([e["key"] for e in json.loads(out)["pending"]], [key])
        code, out, err = run("settle", "--project-dir", str(p.root), "--key", key, "--verdict", "struck",
                             "--violations", "pc_authored")
        self.assertEqual(code, 0, err)
        self.assertEqual(frontmatter(p.read(LEGACY)), frontmatter(before))
        code, out, _err = run("settle", "--project-dir", str(p.root), "--key", key, "--verdict", "clean")
        self.assertEqual((code, json.loads(out)["action"]), (1, "stale"))


class CarriedTests(unittest.TestCase):
    def test_soft_findings_are_carried_until_the_next_settled_beat(self):
        # O17
        p = Project(self)
        first = ship(p)["key"]
        settle(p, first, "soft", violations=["tonal_drift"], findings_text="- tonal_drift: one hedge phrase\n")
        carried = rp_gate.carried_path(p.root, LEGACY).read_text(encoding="utf-8")
        self.assertIn("Beat 4131", carried)
        self.assertIn("one hedge phrase", carried)
        second = ship(p, text=TURN_2, append=BEAT_2, label="4132")["key"]
        settle(p, second, "struck", violations=["presence"])
        self.assertEqual(rp_gate.carried_path(p.root, LEGACY).read_text(encoding="utf-8"), carried)  # a struck beat consumed nothing
        third = ship(p, text=TURN_2, append=BEAT_2, label="4132", attempt=1)["key"]
        settle(p, third, "clean")
        self.assertFalse(rp_gate.carried_path(p.root, LEGACY).exists())

    def test_a_reviewed_ship_consumes_carried_findings(self):
        p = Project(self)
        first = ship(p)["key"]
        settle(p, first, "soft", violations=["tonal_drift"], findings_text="- tonal_drift\n")
        ship(p, text=TURN_2, append=BEAT_2, label="4132", provisional=False, reviewed=True)
        self.assertFalse(rp_gate.carried_path(p.root, LEGACY).exists())


class RouteTests(unittest.TestCase):
    @staticmethod
    def check(self, p, text=TURN, *extra, harness="claude"):
        tf = p.root / "Work/rp/.gate/turn.md"
        tf.parent.mkdir(parents=True, exist_ok=True)
        tf.write_text(text, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "ASHA_HARNESS"}
        if harness:
            env["ASHA_HARNESS"] = harness
        with mock.patch.dict(os.environ, env, clear=True):
            code, out, err = run("check", "--project-dir", str(p.root), "--turn-file", str(tf), "--no-log", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)["route"]

    def test_check_reports_background_reasons(self):
        p = Project(self)
        cast = p.root / "Lore/World/Characters/Vey.md"  # a profiled name the lint knows
        cast.parent.mkdir(parents=True)
        cast.write_text("# Vey\n", encoding="utf-8")
        got = RouteTests.check(self, p)
        self.assertEqual(got["next"], "review")
        self.assertEqual(got["background"], {"eligible": True, "reasons": []})
        cases = {
            "harness": ((TURN,), {"harness": "codex"}),
            "harness_unknown": ((TURN,), {"harness": None}),
            "rewrite": ((TURN, "--attempt", "2"), {}),
            "validate_tag": ((bg_turn(keeper="I wait. [validate]"),), {}),
            "day_change": ((bg_turn('currentDate: "Day 197 (Tuesday)"\nscene.time: "06:00"', day=197,
                                    time="06:00"),), {}),
            "lint": ((bg_turn(prose="Vey hangs the kettle on its hook."),), {}),
        }
        for reason, (args, kw) in cases.items():
            with self.subTest(reason=reason):
                got = RouteTests.check(self, p, *args, **kw)
                self.assertEqual(got["next"], "review")
                self.assertFalse(got["background"]["eligible"])
                self.assertIn(reason, got["background"]["reasons"])
        p.mode("turnGate:\n  background: false\n")
        self.assertIn("policy", RouteTests.check(self, p)["background"]["reasons"])

    def test_a_pending_beat_is_a_reason(self):
        p = Project(self)
        ship(p)
        got = RouteTests.check(self, p, TURN_2)
        self.assertIn("pending", got["background"]["reasons"])

    def test_a_beat_that_ships_without_review_has_no_background(self):
        p = Project(self)
        got = RouteTests.check(self, p, bg_turn(priced=False))
        self.assertEqual((got["next"], got["background"]["eligible"]), ("ship", False))


class ContractTests(unittest.TestCase):
    def read(self, rel):
        return (REPO / rel).read_text(encoding="utf-8")

    def test_turn_runs_the_background_path(self):
        text = self.read("plugins/rp/commands/turn.md")
        for needle in ("ship --provisional", "settle", 'rp_gate.py" pending', "route.background", "REVIEW_KEY",
                       "review_session", "queued-input.md", "collect"):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        collect = text.split("### Step 1b", 1)[1].split("\n### ", 1)[0]
        self.assertIn("before", collect)  # collected before anything is drafted

    def test_the_strike_notice_names_no_categories(self):
        # 2026-10-28: the chat says only that the shown beat was struck after review and redrafted, and asks the
        # Keeper to confirm or revise the input held for it; the categories stay in the log (the struck callout)
        collect = self.read("plugins/rp/commands/turn.md").split("### Step 1b", 1)[1].split("\n### ", 1)[0]
        [redraft] = [line for line in collect.splitlines() if "struck after review and redrafted" in line]
        self.assertIn('"Beat <beat> was struck after review and redrafted:"', redraft)
        # no other mention of categories on the redraft bullet, the stated rule apart
        self.assertNotIn("categor", redraft.replace("names no categories or findings", ""))
        self.assertEqual(collect.count("struck after review"), 1)
        self.assertIn("play it as it stands, or send a revised input", collect)

    def test_end_and_start_collect_first(self):
        for rel in ("plugins/rp/commands/end.md", "plugins/rp/commands/start.md"):
            with self.subTest(rel=rel):
                self.assertIn("pending", self.read(rel))

    def test_reviewer_echoes_the_review_key(self):
        self.assertIn("review_key", self.read("plugins/write/agents/continuity-reviewer.md"))

    def test_only_verified_harnesses_run_reviews_in_the_background(self):
        self.assertEqual(rp_gate.BACKGROUND_HARNESSES, ("claude",))
        self.assertIn("Codex, Copilot and OpenCode", self.read("plugins/rp/commands/turn.md"))


if __name__ == "__main__":
    unittest.main()


class ReviewRoundOneTests(unittest.TestCase):
    """Review 1: S2, S3, S5, M1, M2, M5."""

    def test_carried_findings_belong_to_their_run(self):
        # S2: another campaign's ship never consumes this run's findings
        p = Project(self)
        key = ship(p)["key"]
        settle(p, key, "soft", violations=["tonal_drift"], findings_text="- tonal_drift\n")
        mine = rp_gate.carried_path(p.root, LEGACY)
        self.assertTrue(mine.is_file())
        other = "Work/rp/rp_session_2026-10-20.md"
        (p.root / other).write_text(fx.LEGACY_FRONTMATTER.format(extra="") + "\n# Other run\n\n" + beat(10, 196),
                                    encoding="utf-8")
        self.assertNotEqual(rp_gate.carried_path(p.root, other), mine)
        ship(p, session=other, provisional=False, label="11", append=beat(11, 196), reviewed=True)
        self.assertTrue(mine.is_file())

    def test_only_a_reviewed_ship_consumes_carried_findings(self):
        # S2: a routine beat nobody reviewed leaves them for the next reviewed one
        p = Project(self)
        key = ship(p)["key"]
        settle(p, key, "soft", violations=["tonal_drift"], findings_text="- tonal_drift\n")
        ship(p, text=TURN_2, append=BEAT_2, label="4132", provisional=False)
        self.assertTrue(rp_gate.carried_path(p.root, LEGACY).is_file())
        third = bg_turn('scene.time: "19:15"', prose="Liss wipes the table.", time="19:15", keeper="I sit.")
        ship(p, text=third, append=beat(4133, 196), label="4133", provisional=False, reviewed=True)
        self.assertFalse(rp_gate.carried_path(p.root, LEGACY).exists())

    def test_a_soft_settle_corrects_the_state_in_place(self):
        # S2: a soft finding that names state is corrected in the frontmatter; the shown prose stays
        p, q = Project(self), Project(self)
        key = ship(p)["key"]
        shown = p.read(LEGACY).split("\n---\n", 1)[1]
        got = settle(p, key, "soft", violations=["scene_state_mismatch"], findings_text="- mood\n",
                     delta_text='scene.mood: "watchful"')
        self.assertEqual(got["action"], "settled")
        text = p.read(LEGACY)
        self.assertIn('mood: "watchful"', frontmatter(text))
        self.assertEqual(text.split("\n---\n", 1)[1], shown.replace(
            f"<!-- rp-provisional: {key} -->\n", "").replace(f"<!-- /rp-provisional: {key} -->\n", ""))
        # the same state a reviewed ship with the corrected delta gives
        ship(q, text=bg_turn('scene.time: "19:05"\nscene.mood: "watchful"'), provisional=False)
        strip = lambda t: [x for x in frontmatter(t).splitlines() if not x.startswith("shipped:")]  # noqa: E731
        self.assertEqual(strip(text), strip(q.read(LEGACY)))
        self.assertEqual(settle(p, key, "struck")["action"], "stale")

    def test_a_soft_correction_refuses_after_a_hand_edit(self):
        p = Project(self)
        key = ship(p)["key"]
        text = p.read(LEGACY).replace('mood: "tense"', 'mood: "grim"', 1)
        (p.root / LEGACY).write_text(text, encoding="utf-8")
        got = settle(p, key, "soft", violations=["scene_state_mismatch"], delta_text='scene.mood: "watchful"')
        self.assertEqual(got["action"], "refused")
        self.assertEqual(p.read(LEGACY), text)
        self.assertEqual(pending_dirs(p), [key])

    def test_a_soft_settle_rerun_after_a_crash_still_carries(self):
        # S2: the findings survive a crash between the file write and the cleanup
        p = Project(self)
        key = ship(p)["key"]
        with mock.patch.object(rp_gate.shutil, "rmtree", side_effect=OSError("busy")):
            with self.assertRaises(OSError):
                settle(p, key, "soft", violations=["tonal_drift"], findings_text="- one hedge\n")
        rp_gate.carried_path(p.root, LEGACY).unlink()  # as if the crash came before it was written
        self.assertEqual(settle(p, key, "soft", violations=["tonal_drift"], findings_text="- one hedge\n")["action"],
                         "already")
        self.assertIn("one hedge", rp_gate.carried_path(p.root, LEGACY).read_text(encoding="utf-8"))

    def test_no_rewrites_left_keeps_the_review_first(self):
        # S3
        p = Project(self)
        p.mode("turnGate:\n  rewrites:\n    consequential: 0\n")
        got = RouteTests.check(self, p)
        self.assertIn("no_rewrites", got["background"]["reasons"])
        with self.assertRaises(ValueError):
            rp_gate.ship(p.root, LEGACY, turn_text=TURN, append_text=BEAT, beat="4131", provisional=True)

    def test_marks_removed_by_hand_block_the_next_ship_and_the_strike(self):
        # S5, trigger 1: the beat stays pending; nothing ships over it, and a strike cannot find it
        p = Project(self)
        key = ship(p)["key"]
        text = p.read(LEGACY).replace(f"<!-- rp-provisional: {key} -->\n", "").replace(
            f"<!-- /rp-provisional: {key} -->\n", "")
        (p.root / LEGACY).write_text(text, encoding="utf-8")
        with self.assertRaises(ValueError):
            ship(p, text=TURN_2, append=BEAT_2, label="4132", provisional=False)
        self.assertEqual(p.rotate()["action"], "refused")
        got = settle(p, key, "struck", violations=["presence"])
        self.assertEqual(got["action"], "refused")
        self.assertEqual(p.read(LEGACY), text)
        self.assertEqual(pending_dirs(p), [key])
        self.assertEqual(rp_gate.pending_reviews(p.root)[0]["shipped"], True)

    def test_a_retried_provisional_ship_after_its_settle_writes_no_review(self):
        # S5, trigger 2
        p = Project(self)
        key = ship(p)["key"]
        settle(p, key, "clean")
        before = p.read(LEGACY)
        again = ship(p)
        self.assertEqual(again["action"], "already_shipped")
        self.assertEqual(pending_dirs(p), [])
        self.assertEqual(p.read(LEGACY), before)

    def test_a_beat_with_odd_line_breaks_can_be_struck(self):
        # M1
        p = Project(self)
        odd = beat(4131, 196).replace("Liss sets down the axe.", "Liss sets down the axe.\x0c")
        key = ship(p, append=odd)["key"]
        self.assertEqual(settle(p, key, "struck", violations=["presence"])["action"], "struck")

    def test_pending_clears_queued_input_and_carried_findings(self):
        # M2: /rp:end and a revised input clear them through the gate
        p = Project(self)
        key = ship(p)["key"]
        settle(p, key, "soft", violations=["tonal_drift"], findings_text="- one hedge\n")
        queued = p.root / rp_gate.QUEUED_INPUT
        queued.write_text("I wait.\n", encoding="utf-8")
        code, out, err = run("pending", "--project-dir", str(p.root), "--clear-queued", "--clear-carried")
        self.assertEqual(code, 0, err)
        got = json.loads(out)
        self.assertEqual(got["cleared"]["queued_input"], "I wait.\n")
        self.assertIn("one hedge", got["cleared"]["carried"])
        self.assertFalse(queued.exists())
        self.assertFalse(rp_gate.carried_path(p.root, LEGACY).exists())

    def test_the_pending_review_keeps_the_checks_lint(self):
        # M5: a rerun from disk gives the reviewer the lint the beat was routed on
        p = Project(self)
        RouteTests.check(self, p)
        ship(p)
        [entry] = rp_gate.pending_reviews(p.root)
        kept = json.loads((p.root / entry["check"]).read_text(encoding="utf-8"))
        self.assertEqual(kept["route"]["next"], "review")
        self.assertIn("sources", kept["lint"])


class FixRoundTests(unittest.TestCase):
    def test_a_retried_ship_after_a_corrected_soft_settle_ships_nothing(self):
        # my pass over round 1: the corrected mark (+soft) is still this beat's mark
        p = Project(self)
        key = ship(p)["key"]
        settle(p, key, "soft", violations=["scene_state_mismatch"], delta_text='scene.mood: "watchful"')
        before = p.read(LEGACY)
        for provisional in (True, False):
            with self.subTest(provisional=provisional):
                self.assertEqual(ship(p, provisional=provisional)["action"], "already_shipped")
                self.assertEqual(p.read(LEGACY), before)
                self.assertEqual(pending_dirs(p), [])
