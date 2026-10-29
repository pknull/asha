"""Tests for a fresh Room at each Day Plan: what `session` tells the chair before it closes a Room, and that a new
session continues from disk alone (no /rp:start, the marker and the session's opening untouched)."""
import importlib.util
import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
_spec = importlib.util.spec_from_file_location("rp_fresh_fixtures", HERE / "test_rp_rotation.py")
assert _spec is not None and _spec.loader is not None
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)
rp_gate = fx.rp_gate
Project, LEGACY, NEXT_DAY, beat, run = fx.Project, fx.LEGACY, fx.NEXT_DAY, fx.beat, fx.run

TURN = ("KEEPER_INPUT:\nI wait.\n\nDRAFT:\nLiss hangs the kettle on its hook.\n\n"
        "Scene: Day 196, ~19:05, calm | Location: the kitchen | Present: Liss\n\nGM_SPAWN_LOG:\n- liss: no\n\n"
        "SOURCE_LOG:\npriced_stake_touched: true\n\nSCENE_STATE_DELTA:\nscene.time: \"19:05\"\n")


def session(p):
    code, out, err = run("session", "--project-dir", str(p.root))
    assert code == 0, err
    return json.loads(out)


class ResumeTests(unittest.TestCase):
    def test_a_quiet_session_is_ready_for_a_fresh_room(self):
        p = Project(self)
        got = session(p)["resume"]
        self.assertEqual((got["ready"], got["reasons"]), (True, []))
        self.assertEqual((got["day"], got["last_beat"], got["day_plan"]), (196, "4130", True))

    def test_what_a_closing_room_would_lose_is_a_reason(self):
        p = Project(self)
        key = rp_gate.ship(p.root, LEGACY, turn_text=TURN, append_text=beat(4131, 196), beat="4131",
                           provisional=True, max_rewrites=3)["key"]
        self.assertEqual(session(p)["resume"]["reasons"], ["pending"])
        rp_gate.settle(p.root, key, verdict="clean")
        queued = p.root / "Work/rp/.gate/queued-input.md"
        queued.write_text("I wait some more.\n", encoding="utf-8")
        self.assertEqual(session(p)["resume"]["reasons"], ["queued_input"])
        queued.unlink()
        p.rotate()  # Day 196 closes; its summary is due until one is published
        got = session(p)["resume"]
        self.assertEqual(got["reasons"], ["summaries_due"])
        self.assertEqual((got["day"], got["beats_today"]), (197, 0))

    def test_no_marker_or_an_interrupted_rotation_is_a_reason(self):
        # M12
        p = Project(self)
        (p.root / "Work/markers/rp-active").unlink()
        self.assertIn("no_marker", session(p)["resume"]["reasons"])
        p.marker(f"session_file: {LEGACY}\nsession_opened: {LEGACY}\n")
        with __import__("unittest").mock.patch.object(rp_gate, "_append_footer", return_value=False):
            p.rotate()  # the new day file is published and named, the old one never footered
        self.assertIn("rotation", session(p)["resume"]["reasons"])

    def test_a_fresh_session_builds_the_same_brief_and_keeps_the_session(self):
        p = Project(self)
        marker = p.read("Work/markers/rp-active")
        first = rp_gate.build_brief(p.root, LEGACY, "I wait.")[0]
        # a new process: nothing carried over but the files
        again = rp_gate.build_brief(p.root, session(p)["path"], "I wait.")[0]
        self.assertEqual(first, again)
        self.assertEqual(p.read("Work/markers/rp-active"), marker)


class ContractTests(unittest.TestCase):
    def read(self, rel):
        return (REPO / rel).read_text(encoding="utf-8")

    def test_the_procedure_is_written_down(self):
        readme = self.read("plugins/rp/README.md")
        section = readme.split("## Fresh Room at each Day Plan", 1)[1].split("\n## ", 1)[0]
        for needle in ("resume.ready", "asha control session close", "asha control session launch",
                       "--profile room", "/rp:start resume", "Never /rp:start without `resume`"):
            with self.subTest(needle=needle):
                self.assertIn(needle, section)
        # Q7: the chair starts it on its own and says so in one line; with no chair the Room carries on
        for needle in ("on its own", "in one line", "When the chair is not running"):
            with self.subTest(needle=needle):
                self.assertIn(needle, section)
        # landing review S3, M5, M6: resume.ready cannot see a turn in flight, so the chair also needs the Room
        # idle in Control, checks again just before the close, and asks for chat-only steers again
        flat = " ".join(section.split())
        for needle in ("`observed: waiting`", "`background_tasks`", "again just before the close",
                       "If `ready` is false, for any reason", "repeat any steer given only in chat"):
            with self.subTest(needle=needle):
                self.assertIn(needle, flat)
        start = self.read("plugins/rp/commands/start.md")
        self.assertIn("/rp:start resume", start)
        self.assertIn("resume", start.split("\n", 6)[3])  # argument-hint
        arguments = start.split("## Arguments", 1)[1]
        self.assertIn("`resume`", arguments)  # parsed before the full start


if __name__ == "__main__":
    unittest.main()
