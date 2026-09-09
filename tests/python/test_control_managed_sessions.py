"""Managed session queue contracts; no provider credentials or running services."""
import os
import json
import sys
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from lib.control.config import load_config
from lib.control.session_store import SessionStore, digest
from lib.control.store import StoreError


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = {"HOME": str(root), "ASHA_HOME": str(root / "asha"),
                    "ASHA_CONFIG": str(root / "config.json"), "XDG_RUNTIME_DIR": str(root)}
        self.config = load_config(self.env)
        self.store = SessionStore(self.config, create=True)
        self.addCleanup(self.store.close)
        self.sid = self.store.create(cwd=str(root), prompt="Do the work")["session_id"]
        self.generation = self.store.claim_owner(self.sid)["generation"]

    def claim(self):
        return self.store.claim_turn(self.sid, self.generation)

    def test_verified_codex_adapter_is_admitted_and_unverified_harness_is_refused(self):
        from lib.control.session_harness import CAPABILITIES
        session = self.store.create(cwd=self.tmp.name, prompt='Coordinate', harness='codex')
        self.assertEqual(session['harness'], 'codex')
        self.assertTrue(CAPABILITIES['codex']['actor_tools'])
        self.assertFalse(CAPABILITIES['codex']['consumption_receipt'])
        with self.assertRaisesRegex(StoreError, 'no supported managed adapter'):
            self.store.create(cwd=self.tmp.name, prompt='Coordinate', harness='copilot')

    def test_session_doctor_reports_native_codex_actor_interface(self):
        import io
        from contextlib import redirect_stdout
        from lib.control.sessions import main
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['doctor', '--json'], env=self.env), 0)
        report = json.loads(output.getvalue())
        self.assertTrue(report['capabilities']['codex']['managed'])
        self.assertEqual(report['codex_actor']['tool'], 'asha_control')
        self.assertTrue(report['codex_actor']['experimental'])

    def test_duplicate_custody_is_idempotent_but_changed_content_conflicts(self):
        one = self.store.enqueue(self.sid, "next", key="same")
        self.assertEqual(one, self.store.enqueue(self.sid, "next", key="same"))
        with self.assertRaises(StoreError):
            self.store.enqueue(self.sid, "different", key="same")

    def test_sqlite_full_rolls_back_message_and_event_custody(self):
        # Exercise SQLite's real FULL disposition without filling the host disk.
        before = self.store.snapshot(self.sid)
        with self.store.db.transaction() as c:
            pages = c.execute('PRAGMA page_count').fetchone()[0]
            maximum = c.execute(f'PRAGMA max_page_count={pages}').fetchone()[0]
        self.assertEqual(maximum, pages)
        with self.assertRaisesRegex(StoreError, 'full'):
            self.store.enqueue(self.sid, 'x' * 64000, key='storage-exhausted')
        self.assertEqual(self.store.snapshot(self.sid), before)
        with self.store.db.transaction() as c:
            c.execute('PRAGMA max_page_count=1073741823')
        accepted = self.store.enqueue(self.sid, 'Recovered input', key='storage-exhausted')
        self.assertEqual(accepted['state'], 'queued')

    def test_sqlite_full_does_not_advance_a_running_turn_event(self):
        turn = self.claim()
        before = self.store.snapshot(self.sid)
        with self.store.db.transaction() as c:
            pages = c.execute('PRAGMA page_count').fetchone()[0]
            c.execute(f'PRAGMA max_page_count={pages}')
        with self.assertRaisesRegex(StoreError, 'full'):
            self.store.observe(self.sid, self.generation, turn['turn_id'], 'text', {'text': 'x' * 64000})
        self.assertEqual(self.store.snapshot(self.sid), before)

    def test_claim_reserves_one_turn_across_connections(self):
        one = self.claim()
        with SessionStore(self.config) as other:
            self.assertIsNone(other.claim_turn(self.sid, self.generation))
        self.assertEqual(self.store.get(self.sid)["turns"], 1)
        self.assertEqual(one["state"], "queued")

    def test_owner_generation_fences_events(self):
        one = self.claim()
        with self.assertRaises(StoreError):
            self.store.observe(self.sid, self.generation + 1, one["turn_id"], "text", {"text": "stale"})
        with self.assertRaises(StoreError):
            self.store.finish(self.sid, self.generation + 1, one["turn_id"], success=True)

    def test_live_provider_fences_recovery_and_stale_binding(self):
        turn = self.claim()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            with self.assertRaises(StoreError):
                self.store.bind_provider(self.sid, self.generation + 1, turn["turn_id"], child.pid)
            self.store.bind_provider(self.sid, self.generation, turn["turn_id"], child.pid)
            self.store.finish(self.sid, self.generation, turn["turn_id"], success=False)
            with self.assertRaisesRegex(StoreError, "provider is still live"):
                self.store.resume(self.sid, prompt="Continue", expected_digest=self.store.recovery_digest(self.store.get(self.sid)))
        finally:
            child.kill()
            child.wait(timeout=3)

    def test_missing_launcher_finishes_failed_without_false_owner_loss(self):
        from lib.control.sessions import run_turn
        turn = self.claim()
        with self.assertRaises(StoreError):
            run_turn(self.store, self.store.get(self.sid), turn, env=self.env, root=Path(self.tmp.name))
        self.assertEqual(self.store.get(self.sid)["state"], "failed")

    def test_owner_launch_backoff_survives_connections(self):
        sid = self.store.create(cwd=self.tmp.name, prompt="Queued")["session_id"]
        self.assertTrue(self.store.reserve_owner_launch(sid))
        with SessionStore(self.config) as other:
            self.assertFalse(other.reserve_owner_launch(sid))
        self.assertEqual(self.store.get(sid)["owner_launch_attempts"], 1)

    def test_turn_completion_does_not_fabricate_consumption(self):
        turn = self.claim()
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=True)
        with self.store.db.transaction() as c:
            row = c.execute("SELECT state FROM session_messages WHERE message_id=?", (turn["message_id"],)).fetchone()
        self.assertEqual(row[0], "submitted")
        self.assertEqual(self.store.get(self.sid)["state"], "idle")

    def test_question_answer_resumes_once_and_preserves_digest(self):
        turn = self.claim()
        request = str(uuid.uuid4())
        self.store.request(self.sid, turn["turn_id"], "Which chapter?", request_id=request)
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=True)
        self.assertIsNone(self.claim())
        with self.assertRaises(StoreError):
            self.store.answer(request, "Chapter 2", expected_digest="wrong")
        for _ in range(2):
            self.store.answer(request, "Chapter 2", expected_digest=digest("Which chapter?"))
        followup = self.claim()
        self.assertIn("Chapter 2", followup["body"])
        self.store.finish(self.sid, self.generation, followup["turn_id"], success=True)
        self.assertIsNone(self.claim())
        with self.assertRaises(StoreError):
            self.store.answer(request, "Chapter 3", expected_digest=digest("Which chapter?"))

    def test_new_connection_retains_pending_work(self):
        with SessionStore(self.config) as other:
            self.assertEqual(other.get(self.sid)["state"], "queued")
            self.assertEqual([e["kind"] for e in other.snapshot(self.sid)["events"]],
                             ["message-retained", "session-created", "owner-claimed"])

    def test_answer_precedes_ordinary_messages_queued_while_waiting(self):
        turn = self.claim()
        request_id = str(uuid.uuid4())
        self.store.request(self.sid, turn["turn_id"], "Which chapter?", request_id=request_id)
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=True)
        self.store.enqueue(self.sid, "Additional context", key="context")
        self.store.answer(request_id, "Two", expected_digest=digest("Which chapter?"))
        self.assertEqual(self.claim()["delivery_key"], "answer:" + request_id)

    def test_global_turn_admission_is_bounded(self):
        self.claim()
        two = self.store.create(cwd=self.tmp.name, prompt="Second")
        generation = self.store.claim_owner(two["session_id"])["generation"]
        self.assertIsNotNone(self.store.claim_turn(two["session_id"], generation))
        three = self.store.create(cwd=self.tmp.name, prompt="Third")
        generation = self.store.claim_owner(three["session_id"])["generation"]
        self.assertIsNone(self.store.claim_turn(three["session_id"], generation))
        self.assertEqual(self.store.get(three["session_id"])["turns"], 0)

    def test_budget_counts_turns_not_polls(self):
        with self.store.db.transaction(write=True) as c:
            c.execute("UPDATE managed_sessions SET max_turns=1 WHERE session_id=?", (self.sid,))
        turn = self.claim()
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=True)
        self.store.enqueue(self.sid, "follow up", key="followup")
        for _ in range(10):
            self.assertIsNone(self.claim())
        self.assertEqual(self.store.get(self.sid)["turns"], 1)
        self.assertEqual(self.store.get(self.sid)["state"], "budget-exhausted")

    def test_stop_cancels_pending_questions_and_messages(self):
        turn = self.claim()
        self.store.request(self.sid, turn["turn_id"], "Question?", request_id=str(uuid.uuid4()))
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=True)
        self.store.stop(self.sid)
        self.store.stopped(self.sid, self.generation)
        self.assertEqual(self.store.snapshot(self.sid)["requests"], [])
        self.assertIsNone(self.claim())

    def test_dead_owner_does_not_replay_a_submitted_turn(self):
        from unittest import mock
        turn = self.claim()
        with mock.patch("lib.control.session_store.verify_process", return_value=False):
            adopted = self.store.claim_owner(self.sid)
        self.assertEqual(adopted["state"], "uncertain")
        self.assertIsNone(self.store.claim_turn(self.sid, adopted["generation"]))
        snapshot = self.store.snapshot(self.sid)
        self.assertEqual(snapshot["message_counts"], {"uncertain": 1})
        self.assertEqual(snapshot["messages"][0]["turn_id"], turn["turn_id"])

    def test_failure_with_question_stays_failed_until_explicit_recovery(self):
        turn = self.claim()
        request_id = str(uuid.uuid4())
        self.store.request(self.sid, turn["turn_id"], "Which chapter?", request_id=request_id)
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=False, reason="provider quota")
        self.assertEqual(self.store.get(self.sid)["state"], "failed")
        with self.assertRaises(StoreError):
            self.store.answer(request_id, "Two", expected_digest=digest("Which chapter?"))
        session = self.store.get(self.sid)
        before = self.store.recovery_digest(session)
        for _ in range(2):
            self.store.resume(self.sid, prompt="Continue from observed progress", expected_digest=before)
        self.assertEqual(self.store.get(self.sid)["state"], "idle")
        self.assertEqual(self.store.get_request(request_id)["state"], "cancelled")
        with self.assertRaisesRegex(StoreError, "no longer pending"):
            self.store.answer(request_id, "Two", expected_digest=digest("Which chapter?"))
        self.assertEqual(self.store.snapshot(self.sid)["message_counts"]["queued"], 1)

    def test_snapshot_counts_are_complete_and_rows_report_truncation(self):
        self.store.enqueue(self.sid, "later", key="two")
        snapshot = self.store.snapshot(self.sid, limit=1)
        self.assertEqual(snapshot["message_counts"], {"queued": 2})
        self.assertFalse(snapshot["complete"]["messages"])
        self.assertFalse(snapshot["complete"]["events"])
        self.assertTrue(snapshot["complete"]["sessions"])

    def test_stop_before_owner_start_is_terminal_without_supervisor(self):
        sid = self.store.create(cwd=self.tmp.name, prompt="Do not run")["session_id"]
        self.store.stop(sid)
        self.assertEqual(self.store.get(sid)["state"], "stopped")
        self.assertEqual(self.store.snapshot(sid)["message_counts"], {"cancelled": 1})

    def test_stop_intent_is_reconciled_after_owner_crash(self):
        from unittest import mock
        from lib.control.sessions import ensure_owners
        self.store.stop(self.sid)
        with mock.patch("lib.control.session_store.process_live", return_value=False):
            self.assertEqual(ensure_owners(self.config)["owners_started"], 0)
        self.assertEqual(self.store.get(self.sid)["state"], "stopped")

    def test_tui_and_cli_share_question_projection_and_resolution(self):
        from unittest import mock
        from lib.control import tui
        from lib.control.sessions import overview
        turn = self.claim()
        request_id = str(uuid.uuid4())
        self.store.request(self.sid, turn["turn_id"], "Which chapter?", request_id=request_id)
        self.store.finish(self.sid, self.generation, turn["turn_id"], success=True)
        self.assertEqual(overview(self.config)["questions"], 1)
        model = tui.TuiModel()
        intent = model.dispatch_key("M")
        self.assertEqual(intent.kind, tui.IntentKind.SESSION_QUESTIONS)
        # This test process stands in for both roles; independent role tests
        # prove that the actual operator CLI refuses owner ancestry.
        with mock.patch("lib.control.sessions.refuse_managed_operator"), \
             mock.patch("lib.control.tui._prompt_line", side_effect=[request_id, "Chapter two"]):
            tui._execute_intent(intent, stdscr=None, curses_module=None, model=model,
                                config=self.config, env=self.env, store=None, journals=None, jj=None)
        self.assertEqual(overview(self.config)["questions"], 0)
        self.assertEqual(self.store.snapshot(self.sid)["requests"], [])
        self.assertIn("0 questions", model.managed_summary)

    def test_fake_harness_question_answer_cycle_without_terminal(self):
        from lib.control.session_harness import ClaudeTransport
        from lib.control.sessions import run_turn
        root = Path(__file__).resolve().parents[2]
        script = Path(self.tmp.name) / "provider.py"
        script.write_text("import sys, json, subprocess\n"
                          "prompt=sys.stdin.read()\n"
                          "print(json.dumps({'type':'system','subtype':'init','session_id':'native'}),flush=True)\n"
                          "if 'Do the work' in prompt:\n"
                          " for _ in range(2):\n"
                          "  p=subprocess.run([sys.executable,'-m','lib.control.sessions','ask','--question','Which chapter?','--json'],capture_output=True,text=True)\n"
                          "  if p.returncode: raise RuntimeError(p.stderr)\n"
                          "print(json.dumps({'type':'result','subtype':'success','result':'done'}),flush=True)\n")
        def factory(_argv, **kwargs):
            return ClaudeTransport([sys.executable, str(script)], **kwargs)
        env = {**os.environ, **self.env, "PYTHONPATH": str(root)}
        first = self.claim()
        run_turn(self.store, self.store.get(self.sid), first, env=env, root=root, transport_factory=factory)
        pending = self.store.snapshot(self.sid)["requests"]
        self.assertEqual(len(pending), 1)
        self.store.answer(pending[0]["request_id"], "Chapter two", expected_digest=pending[0]["digest"])
        second = self.claim()
        run_turn(self.store, self.store.get(self.sid), second, env=env, root=root, transport_factory=factory)
        self.assertEqual(self.store.get(self.sid)["turns"], 2)
        self.assertEqual(self.store.get(self.sid)["state"], "idle")


if __name__ == "__main__":
    unittest.main()
