import tempfile
import unittest
import os
import sys
import json
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from lib.control.config import load_config
from lib.control.native_requests import NativeRequests
from lib.control.session_store import SessionStore
from lib.control.store import StoreError


class NativeRequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = load_config({"HOME": str(self.root), "ASHA_HOME": str(self.root / "asha")})
        self.store = SessionStore(self.config, create=True)
        self.addCleanup(self.store.close)
        self.sid = self.store.create(cwd=str(self.root), prompt="Review")["session_id"]
        self.generation = self.store.claim_owner(self.sid)["generation"]
        self.turn = self.store.claim_turn(self.sid, self.generation)["turn_id"]
        self.store.observe(self.sid, self.generation, self.turn, "initialized", {"native_id": "native-test"})
        self.requests = NativeRequests(self.store)
        self.payload = {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "git status"}, "tool_use_id": "tool-1"}

    def open(self, **changes):
        return self.requests.open(self.sid, self.generation, self.turn, "provider-1", {**self.payload, **changes})

    def test_exact_request_is_durable_and_replayed_once(self):
        first = self.open()
        self.assertEqual(self.open(), first)
        self.assertEqual(first["kind"], "native-permission")
        self.assertEqual(self.store.snapshot(self.sid)["pending_request_count"], 1)
        with self.assertRaisesRegex(StoreError, "reused"):
            self.open(input={"command": "different command"})

    def test_clarification_cannot_resolve_native_permission(self):
        request = self.open()
        with self.assertRaisesRegex(StoreError, "native permission"):
            self.store.answer(request["request_id"], "yes", expected_digest=request["digest"])
        self.assertEqual(self.requests.get(request["request_id"])["state"], "pending")

    def test_permission_cli_refuses_owner_even_with_role_labels_removed(self):
        from lib.control.sessions import main
        request = self.open()
        env = {"HOME": str(self.root), "ASHA_HOME": str(self.root / "asha"),
               "ASHA_CONFIG": str(self.root / "missing.json")}
        args = ["permission", request["request_id"], "--decision", "allow", "--digest", request["digest"]]
        for values in (env, {**env, "ASHA_MANAGED_SESSION_ID": self.sid}):
            with redirect_stderr(StringIO()), redirect_stdout(StringIO()):
                self.assertEqual(main(args, env=values), 2)
        self.assertEqual(self.requests.get(request["request_id"])["state"], "pending")

    def test_control_decides_native_request_without_enqueuing_a_model_turn(self):
        from unittest import mock
        from lib.control import tui
        from lib.control.sessions import overview
        request = self.open()
        self.assertEqual(overview(self.config)["permissions"], 1)
        self.assertEqual(overview(self.config)["questions"], 0)
        model = tui.TuiModel()
        with mock.patch("lib.control.sessions.refuse_managed_operator"), \
             mock.patch.object(tui, "_prompt_line", return_value=request["request_id"]), \
             mock.patch.object(tui, "_native_permission_prompt", return_value="deny") as dialog:
            tui._execute_intent(model.dispatch_key("M"), stdscr=None, curses_module=None,
                                model=model, config=self.config, env={}, store=None, journals=None, jj=None)
        self.assertEqual(dialog.call_args.args[2]["digest"], request["digest"])
        self.assertEqual(self.requests.get(request["request_id"])["answer"], "deny")
        self.assertEqual(overview(self.config)["permissions"], 0)
        self.assertEqual(self.store.get(self.sid)["turns"], 1)
        self.assertNotIn("queued", self.store.snapshot(self.sid)["message_counts"])

    def test_control_can_resolve_a_request_outside_its_bounded_suggestions(self):
        from unittest import mock
        from lib.control import tui
        request = self.open()
        model = tui.TuiModel()
        with mock.patch("lib.control.sessions.refuse_managed_operator"), \
             mock.patch.object(SessionStore, "current_work", return_value={"rows": [], "complete": False, "next_cursor": None}), \
             mock.patch.object(tui, "_prompt_line", return_value=request["request_id"]) as picker, \
             mock.patch.object(tui, "_native_permission_prompt", return_value="allow"):
            tui._execute_intent(model.dispatch_key("M"), stdscr=None, curses_module=None,
                                model=model, config=self.config, env={}, store=None, journals=None, jj=None)
        self.assertIn("Enter a request ID", picker.call_args.kwargs["context"])
        self.assertEqual(self.requests.get(request["request_id"])["answer"], "allow")

    def test_allow_binds_exact_input_and_submission_is_not_replayed(self):
        request = self.open()
        with self.assertRaises(StoreError):
            self.requests.decide(request["request_id"], "allow", expected_digest="wrong")
        decision = self.requests.decide(request["request_id"], "allow", expected_digest=request["digest"])
        self.assertEqual(self.requests.decide(request["request_id"], "allow", expected_digest=request["digest"]), decision)
        responses = self.requests.claim_responses(self.sid, self.generation, self.turn)
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["frame"]["response"]["response"], {"behavior": "allow", "updatedInput": {"command": "git status"}})
        self.assertEqual(self.requests.claim_responses(self.sid, self.generation, self.turn), [])
        self.requests.submitted(self.sid, self.generation, self.turn, request["request_id"])
        self.assertEqual(self.requests.get(request["request_id"])["response_state"], "submitted")
        with self.assertRaises(StoreError):
            self.requests.decide(request["request_id"], "deny", expected_digest=request["digest"])

    def test_cancelled_native_request_refuses_late_approval(self):
        request = self.open()
        self.requests.cancel(self.sid, self.generation, self.turn, "provider-1")
        with self.assertRaises(StoreError):
            self.requests.decide(request["request_id"], "allow", expected_digest=request["digest"])
        self.assertEqual(self.requests.claim_responses(self.sid, self.generation, self.turn), [])

    def test_provider_cancellation_never_erases_submission_evidence(self):
        request = self.open()
        rid = request["request_id"]
        self.requests.decide(rid, "allow", expected_digest=request["digest"])
        self.requests.claim_responses(self.sid, self.generation, self.turn)
        self.requests.cancel(self.sid, self.generation, self.turn, "provider-1")
        self.assertEqual(self.requests.get(rid)["response_state"], "uncertain")
        self.requests.submitted(self.sid, self.generation, self.turn, rid)
        self.requests.cancel(self.sid, self.generation, self.turn, "provider-1")
        self.assertEqual(self.requests.get(rid)["response_state"], "submitted")
        self.assertIsNotNone(self.requests.get(rid)["submitted_at"])

    def test_unclaimed_decision_is_cancelled_with_a_reason_when_turn_closes(self):
        request = self.open()
        self.requests.decide(request["request_id"], "allow", expected_digest=request["digest"])
        self.store.finish(self.sid, self.generation, self.turn, success=False, reason="provider gone")
        self.assertEqual(self.requests.get(request["request_id"])["response_state"], "cancelled")
        events = [e for e in self.store.snapshot(self.sid)["events"] if e["kind"] == "native-permission-closed"]
        self.assertEqual(events[0]["payload"]["previous_state"], "queued")
        self.assertEqual(events[0]["payload"]["reason"], "provider gone")

    def test_empty_permission_poll_does_not_compete_for_the_sqlite_writer(self):
        from lib.control.database import ControlDatabase
        self.open()
        with ControlDatabase(self.config) as other, other.transaction(write=True):
            self.assertEqual(self.requests.claim_responses(self.sid, self.generation, self.turn), [])

    def test_operator_stop_does_not_erase_an_already_completed_response_write(self):
        request = self.open()
        self.requests.decide(request["request_id"], "allow", expected_digest=request["digest"])
        self.requests.claim_responses(self.sid, self.generation, self.turn)
        self.store.stop(self.sid)
        self.requests.submitted(self.sid, self.generation, self.turn, request["request_id"])
        self.assertEqual(self.requests.get(request["request_id"])["response_state"], "submitted")

    def test_operator_refusal_is_fail_closed_for_deep_or_unobservable_ancestry(self):
        from unittest import mock
        from lib.control.sessions import refuse_managed_operator
        def parent(pid):
            return ["S", str(pid + 1)]
        for side_effect in (parent, lambda _pid: None):
            with mock.patch("lib.control.sessions.verify_process", return_value=True), \
                 mock.patch("lib.control.harness.os.getpid", return_value=1000000000), \
                 mock.patch("lib.control.harness._process_stat_fields", side_effect=side_effect):
                with self.assertRaisesRegex(StoreError, "cannot establish operator ancestry"):
                    refuse_managed_operator(self.config, {})

    def test_generation_and_terminal_turn_fence_permission(self):
        request = self.open()
        with self.assertRaises(StoreError):
            self.requests.claim_responses(self.sid, self.generation + 1, self.turn)
        self.store.finish(self.sid, self.generation, self.turn, success=False, reason="provider lost")
        with self.assertRaises(StoreError):
            self.requests.decide(request["request_id"], "allow", expected_digest=request["digest"])
        self.assertEqual(self.requests.get(request["request_id"])["state"], "cancelled")

    def test_lost_turn_retains_uncertain_response_without_automatic_replay(self):
        request = self.open()
        self.requests.decide(request["request_id"], "deny", expected_digest=request["digest"])
        self.requests.claim_responses(self.sid, self.generation, self.turn)
        self.store.finish(self.sid, self.generation, self.turn, success=False, reason="owner lost")
        result = self.requests.get(request["request_id"])
        self.assertEqual(result["response_state"], "uncertain")
        self.assertEqual(result["answer"], "deny")

    def test_provider_exit_with_a_pending_permission_cancels_the_request(self):
        from lib.control.session_harness import ClaudeTransport
        from lib.control.sessions import run_turn
        script = '''import json,sys
def send(value): print(json.dumps(value),flush=True)
initialize=json.loads(sys.stdin.readline())
send({'type':'control_response','response':{'subtype':'success','request_id':initialize['request_id']}})
json.loads(sys.stdin.readline())
send({'type':'system','subtype':'init','session_id':'native-test'})
send({'type':'control_request','request_id':'provider-1','request':{'subtype':'can_use_tool','tool_name':'Write','input':{'file_path':'unused','content':'unused'}}})
'''
        def factory(_argv, **kwargs):
            return ClaudeTransport([sys.executable, "-c", script], structured=True, timeout=5, **kwargs)
        with self.assertRaisesRegex(StoreError, "without a structured terminal"):
            run_turn(self.store, self.store.get(self.sid), {"turn_id": self.turn, "body": "Review"},
                     env=dict(os.environ), root=Path(__file__).resolve().parents[2], transport_factory=factory)
        self.assertEqual(self.store.get(self.sid)["state"], "failed")
        self.assertEqual(self.store.snapshot(self.sid)["pending_request_count"], 0)
        with self.store.db.transaction() as c:
            states = [tuple(r) for r in c.execute("SELECT r.state,n.response_state FROM session_requests r JOIN session_native_requests n USING(request_id)")]
        self.assertEqual(states, [("cancelled", "cancelled")])

    def test_streaming_provider_waits_for_exact_permission_and_finishes_one_turn(self):
        from lib.control.session_harness import ClaudeTransport
        from lib.control.sessions import run_turn
        script = self.root / "provider.py"
        script.write_text('''import json,sys
def send(value): print(json.dumps(value),flush=True)
initialize=json.loads(sys.stdin.readline())
send({'type':'control_response','response':{'subtype':'success','request_id':initialize['request_id']}})
user=json.loads(sys.stdin.readline())
assert user['type']=='user'
send({'type':'system','subtype':'init','session_id':'native-test'})
request={'type':'control_request','request_id':'provider-1','request':{'subtype':'can_use_tool','tool_name':'Bash','input':{'command':'git status'},'tool_use_id':'tool-1'}}
send(request)
send(request)
answer=json.loads(sys.stdin.readline())
assert answer['response']['response']=={'behavior':'allow','updatedInput':{'command':'git status'}}
send({'type':'result','subtype':'success','result':'Finished'})
assert sys.stdin.read()==''
''')
        errors = []
        finished = threading.Event()
        def operator():
            try:
                with SessionStore(self.config) as store:
                    until = time.monotonic() + 5
                    while time.monotonic() < until and not finished.is_set():
                        requests = store.snapshot(self.sid)["requests"]
                        if requests:
                            request = requests[0]
                            # A human wait may outlast the active-work deadline.
                            time.sleep(0.6)
                            NativeRequests(store).decide(request["request_id"], "allow", expected_digest=request["digest"])
                            return
                        time.sleep(0.02)
                    raise AssertionError("permission never became visible")
            except Exception as exc:
                errors.append(exc)
        actor = threading.Thread(target=operator)
        actor.start()
        def factory(_argv, **kwargs):
            return ClaudeTransport([sys.executable, str(script)], structured=True, timeout=0.5, permission_timeout=4, **kwargs)
        try:
            run_turn(self.store, self.store.get(self.sid), {"turn_id": self.turn, "body": "Review"},
                env=dict(os.environ), root=Path(__file__).resolve().parents[2], transport_factory=factory)
        finally:
            finished.set()
            actor.join(6)
        self.assertEqual(errors, [])
        self.assertEqual(self.store.get(self.sid)["state"], "idle")
        self.assertEqual(self.store.get(self.sid)["turns"], 1)
        with self.store.db.transaction() as c:
            rows = c.execute("SELECT response_state FROM session_native_requests").fetchall()
        self.assertEqual([r[0] for r in rows], ["submitted"])
