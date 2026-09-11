"""Codex wire decisions through SQLite, operator surfaces and owner delivery."""
import json
import os
import sys
import threading
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO

from lib.control.config import load_config
from lib.control.codex_protocol import request_key, error_status
from lib.control.native_requests import NativeRequests
from lib.control.session_harness import CAPABILITIES
from lib.control.session_store import SessionStore
from lib.control.store import StoreError


class CodexNativeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {"HOME": str(self.root), "ASHA_HOME": str(self.root / "asha")}
        self.config = load_config(self.env)
        self.store = SessionStore(self.config, create=True)
        self.addCleanup(self.store.close)
        # The production capability remains gated pending adapter acceptance.
        with mock.patch.dict(CAPABILITIES, codex={"managed": True}):
            self.sid = self.store.create(cwd=str(self.root), prompt="Review", harness="codex")["session_id"]
        self.generation = self.store.claim_owner(self.sid)["generation"]
        self.message = self.store.claim_turn(self.sid, self.generation)
        self.turn = self.message["turn_id"]
        self.store.observe(self.sid, self.generation, self.turn, "initialized", {"native_id": "thread-1"})
        self.native = NativeRequests(self.store)

    def open(self, method="item/tool/requestUserInput", rpc_id=7, **params):
        if method == "item/tool/requestUserInput":
            params.setdefault("questions", [{"id": "tone", "header": "Tone", "question": "Which tone?", "isSecret": False}])
        payload = {"protocol": "codex-app-server-v2", "rpc_id": rpc_id, "method": method,
                   "params": {"threadId": "thread-1", "turnId": "native-turn-1", "itemId": "tool-1", "isBlocking": True, "startedAtMs": 0, "cwd": str(self.root), **params}}
        return self.native.open(self.sid, self.generation, self.turn, request_key(rpc_id), payload)

    def answer(self, request, text="Quiet"):
        return self.native.answer_native(request["request_id"], {"answers": {"tone": {"answers": [text]}}}, expected_digest=request["digest"])

    def test_answer_preserves_wire_id_and_does_not_enqueue_another_turn(self):
        from lib.control.sessions import overview
        request = self.open()
        self.assertEqual(overview(self.config)["questions"], 1)
        self.assertEqual(self.store.current_work(kind="requests")["rows"][0]["next_action"], "answer")
        with self.assertRaises(StoreError):
            self.native.decide(request["request_id"], "allow", expected_digest=request["digest"])
        self.answer(request)
        self.answer(request)  # Idempotent retry before delivery.
        replies = self.native.claim_responses(self.sid, self.generation, self.turn)
        self.assertEqual(replies, [{"request_id": request["request_id"], "frame": {
            "id": 7, "result": {"answers": {"tone": {"answers": ["Quiet"]}}}}}])
        self.assertEqual(self.native.claim_responses(self.sid, self.generation, self.turn), [])
        self.native.submitted(self.sid, self.generation, self.turn, request["request_id"])
        self.assertEqual(self.native.get(request["request_id"])["response_state"], "submitted")
        self.assertEqual(self.store.get(self.sid)["turns"], 1)
        self.assertNotIn("queued", self.store.snapshot(self.sid)["message_counts"])
        self.assertEqual(overview(self.config)["questions"], 0)

    def test_native_answer_rejects_malformed_stale_and_conflicting_answers(self):
        request = self.open()
        for answer in (None, [], "yes", {}, {"answers": {"wrong": {"answers": ["yes"]}}},
                       {"answers": {"tone": {"answers": []}}}):
            with self.subTest(answer=answer), self.assertRaises(StoreError):
                self.native.answer_native(request["request_id"], answer, expected_digest=request["digest"])
        with self.assertRaises(StoreError):
            self.native.answer_native(request["request_id"], {"answers": {"tone": {"answers": ["Quiet"]}}}, expected_digest="stale")
        self.answer(request)
        with self.assertRaises(StoreError):
            self.answer(request, "Loud")

    def test_permissions_are_exact_and_turn_scoped(self):
        profile = {"network": {"enabled": True}}
        for rpc_id, decision, expected in ((1, "allow", profile), (2, "deny", {})):
            request = self.open("item/permissions/requestApproval", rpc_id, permissions=profile)
            self.assertIn("turn only", request["question"])
            self.native.decide(request["request_id"], decision, expected_digest=request["digest"])
            frame = self.native.claim_responses(self.sid, self.generation, self.turn)[0]["frame"]
            self.assertEqual(frame, {"id": rpc_id, "result": {"permissions": expected, "scope": "turn"}})

    def test_command_decision_preserves_string_rpc_id(self):
        request = self.open("item/commandExecution/requestApproval", "7", command="git status", cwd=str(self.root))
        self.native.decide(request["request_id"], "deny", expected_digest=request["digest"])
        self.assertEqual(self.native.claim_responses(self.sid, self.generation, self.turn)[0]["frame"],
                         {"id": "7", "result": {"decision": "decline"}})

    def test_secret_input_foreign_threads_and_provider_mismatch_are_refused(self):
        with self.assertRaises(StoreError):
            self.open(questions=[{"id": "secret", "question": "Password?", "isSecret": True}])
        with self.assertRaises(StoreError):
            self.open(threadId="foreign")
        with self.assertRaises(StoreError):
            self.native.open(self.sid, self.generation, self.turn, "claude-id",
                             {"subtype": "can_use_tool", "tool_name": "Bash", "input": {}})

    def test_closed_turn_cancels_unanswered_native_questions(self):
        request = self.open()
        self.store.finish(self.sid, self.generation, self.turn, success=False, reason="server lost")
        self.assertEqual(self.native.get(request["request_id"])["state"], "cancelled")
        with self.assertRaises(StoreError):
            self.answer(request)

    def test_proven_unsent_reply_is_cancelled_without_uncertainty(self):
        request = self.open()
        self.answer(request)
        self.native.claim_responses(self.sid, self.generation, self.turn)
        self.native.cancel(self.sid, self.generation, self.turn, request_key(7), response_not_submitted=True)
        self.assertEqual(self.native.get(request["request_id"])["response_state"], "cancelled")

    def test_stop_intent_allows_terminal_withdrawal_and_status_evidence(self):
        request = self.open()
        self.store.stop(self.sid)
        self.native.cancel(self.sid, self.generation, self.turn, request_key(7), response_not_submitted=True)
        self.store.observe(self.sid, self.generation, self.turn, "provider-status",
            error_status({"codexErrorInfo": "usageLimitExceeded"}, interrupted=True))
        self.store.observe(self.sid, self.generation, self.turn, "failed", {"reason": "interrupted"})
        self.assertEqual(self.native.get(request["request_id"])["response_state"], "cancelled")

    def test_scope_resolution_holds_no_database_writer(self):
        from lib.control.codex_native import scope_note
        from lib.control.database import ControlDatabase
        def resolve(payload, cwd):
            with ControlDatabase(self.config) as other, other.transaction(write=True):
                return scope_note(payload, cwd)
        with mock.patch("lib.control.codex_native.scope_note", side_effect=resolve):
            self.assertEqual(self.open()["state"], "pending")

    def test_outside_execution_directory_is_explicit_in_permission_question(self):
        request = self.open("item/commandExecution/requestApproval", command="git status", cwd="/")
        self.assertIn("Execution directory differs from session directory", request["question"])

    def test_file_scope_and_diff_are_validated_before_operator_review(self):
        from lib.control.codex_native import describe, scope_note
        payload = {"protocol": "codex-app-server-v2", "rpc_id": 1,
            "method": "item/fileChange/requestApproval", "params": {"threadId": "t", "turnId": "u", "itemId": "f", "startedAtMs": 0},
            "item": {"id": "f", "type": "fileChange", "changes": [{"path": "/elsewhere/chapter.md", "kind": {"type": "add"}, "diff": "Quiet."}]}}
        self.assertEqual(describe(payload)[0], "native-permission")
        self.assertIn("File access outside the session directory", scope_note(payload, str(self.root)))
        payload["item"]["changes"][0].pop("diff")
        with self.assertRaises(StoreError):
            describe(payload)

    def test_operator_cli_refuses_managed_owner_answers(self):
        from lib.control.sessions import main
        request = self.open()
        args = ["answer-native", request["request_id"], "--digest", request["digest"],
                "--answers", json.dumps({"answers": {"tone": {"answers": ["Quiet"]}}})]
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(main(args, env=self.env), 2)
        self.assertEqual(self.native.get(request["request_id"])["state"], "pending")
        with mock.patch("lib.control.sessions.refuse_managed_operator"), redirect_stdout(StringIO()):
            self.assertEqual(main(args, env=self.env), 0)
        self.assertEqual(self.native.get(request["request_id"])["state"], "answered")

    def test_control_native_question_routes_to_same_turn(self):
        from lib.control import tui
        request = self.open()
        model = tui.TuiModel()
        with mock.patch("lib.control.sessions.refuse_managed_operator"), \
             mock.patch.object(tui, "_prompt_line", side_effect=[request["request_id"], "Quiet"]):
            tui._execute_intent(model.dispatch_key("M"), stdscr=None, curses_module=None,
                model=model, config=self.config, env={}, store=None, journals=None, jj=None)
        self.assertEqual(json.loads(self.native.get(request["request_id"])["answer"]),
                         {"answers": {"tone": {"answers": ["Quiet"]}}})
        self.assertEqual(self.store.get(self.sid)["turns"], 1)

    def test_typed_codex_quota_retains_recovery_and_rejects_cross_provider_status(self):
        from lib.control.provider_recovery import validate_status
        status = error_status({"codexErrorInfo": "usageLimitExceeded"})
        self.assertEqual(validate_status(status)["provider"], "codex")
        with self.assertRaises(StoreError):
            validate_status({**status, "source": "rate_limit_event"})
        with self.assertRaises(StoreError):
            self.store.observe(self.sid, self.generation, self.turn, "provider-status",
                               {**status, "provider": "claude", "source": "result_status"})
        self.store.observe(self.sid, self.generation, self.turn, "provider-status", status)
        self.store.observe(self.sid, self.generation, self.turn, "failed", {"reason": "native failure"})
        self.store.finish(self.sid, self.generation, self.turn, success=False, reason="native failure")
        recovery = self.store.get(self.sid)["recovery"]
        self.assertEqual(recovery["category"], "quota")
        self.assertIsNone(recovery["retry_not_before"])
        self.assertIsNone(self.store.claim_turn(self.sid, self.generation))

    def test_nonterminal_error_followed_by_success_does_not_park_the_session(self):
        status = {**error_status({"codexErrorInfo": "internalServerError"}), "source": "turn_error"}
        self.store.observe(self.sid, self.generation, self.turn, "provider-status", status)
        self.store.observe(self.sid, self.generation, self.turn, "completed", {"reason": "completed"})
        self.store.finish(self.sid, self.generation, self.turn, success=True)
        self.assertEqual(self.store.get(self.sid)["state"], "idle")
        self.assertIsNone(self.store.get(self.sid)["recovery"])

    def test_backend_delivers_question_and_approval_then_resumes_same_thread(self):
        from lib.control.sessions import run_turn
        from lib.control.session_harness import CodexTransport
        server = self.root / "fake_server.py"
        server.write_text('''import json, sys
def read(): return json.loads(sys.stdin.readline())
def send(frame): print(json.dumps(frame), flush=True)
init = read()
send({'id': init['id'], 'result': {'userAgent': 'fake'}})
assert read()['method'] == 'initialized'
start = read()
assert start['method'] == 'thread/resume'
assert start['params']['threadId'] == 'thread-1'
send({'id': start['id'], 'result': {'thread': {'id': 'thread-1'},
 'cwd': start['params']['cwd'], 'approvalPolicy': 'untrusted', 'approvalsReviewer': 'user',
 'sandbox': {'type': 'workspaceWrite', 'networkAccess': False, 'writableRoots': []}}})
turn = read()
assert turn['method'] == 'turn/start'
assert 'approvalPolicy' not in turn['params']
assert 'sandboxPolicy' not in turn['params']
tid = 'native-' + turn['params']['clientUserMessageId']
send({'id': turn['id'], 'result': {'turn': {'id': tid, 'status': 'inProgress', 'items': []}}})
scope = {'threadId': 'thread-1', 'turnId': tid, 'itemId': 'item-1'}
send({'id': 7, 'method': 'item/tool/requestUserInput', 'params': dict(scope,
 isBlocking=True, questions=[{'id': 'tone', 'header': 'Tone', 'question': 'Which tone?', 'isSecret': False}])})
assert read() == {'id': 7, 'result': {'answers': {'tone': {'answers': ['Quiet']}}}}
send({'id': '7', 'method': 'item/commandExecution/requestApproval', 'params': dict(scope,
 command='git status', startedAtMs=0, cwd=start['params']['cwd'])})
assert read() == {'id': '7', 'result': {'decision': 'decline'}}
send({'method': 'turn/completed', 'params': {'threadId': 'thread-1',
 'turn': {'id': tid, 'status': 'completed', 'items': []}}})
assert sys.stdin.read() == ''
''')
        errors = []
        done = threading.Event()
        def answer_requests():
            try:
                with SessionStore(self.config) as other:
                    native = NativeRequests(other)
                    while not done.wait(0.01):
                        for request in other.current_work(kind="requests")["rows"]:
                            if request["kind"] == "native-clarification":
                                native.answer_native(request["request_id"], {"answers": {"tone": {"answers": ["Quiet"]}}},
                                    expected_digest=request["digest"])
                            else:
                                native.decide(request["request_id"], "deny", expected_digest=request["digest"])
            except BaseException as exc:
                errors.append(exc)
        operator = threading.Thread(target=answer_requests)
        operator.start()
        def transport(argv, **kwargs):
            self.assertEqual(argv[1:5], ["codex", "app-server", "--listen", "stdio://"])
            self.assertEqual(kwargs["env"]["ASHA_PERSONA"], "0")
            self.assertEqual(kwargs["env"]["ASHA_SESSION_PROFILE"], "worker")
            return CodexTransport([sys.executable, str(server)], timeout=5, permission_timeout=5, **kwargs)
        try:
            with mock.patch("lib.control.sessions.CodexTransport", side_effect=transport):
                for number in range(2):
                    message = self.message
                    if number:
                        self.store.enqueue(self.sid, "Continue", key="second-turn")
                        message = self.store.claim_turn(self.sid, self.generation)
                    run_turn(self.store, self.store.get(self.sid), message,
                        env={**os.environ, **self.env}, root=Path(__file__).resolve().parents[2])
                    self.assertEqual(self.store.get(self.sid)["state"], "idle")
                    self.assertEqual(self.store.get(self.sid)["native_id"], "thread-1")
        finally:
            done.set()
            operator.join(3)
        self.assertFalse(operator.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.get(self.sid)["turns"], 2)
        with self.store.db.transaction() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM session_native_requests WHERE response_state='submitted'").fetchone()[0], 4)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM session_messages").fetchone()[0], 2)
