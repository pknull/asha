"""Private actor requests use real local sockets and kernel process identity."""
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from lib.control.config import load_config
from lib.control.session_ipc import SessionRequestServer, request_question
from lib.control.session_ipc import PROTOCOL, MAX_FRAME
from lib.control.session_store import SessionStore
from lib.control.database import DatabaseBusyError
from lib.control.store import StoreError


class SessionIPCTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {"HOME": str(self.root), "ASHA_HOME": str(self.root / "asha")}
        self.config = load_config(self.env)
        self.store = SessionStore(self.config, create=True)
        self.addCleanup(self.store.close)
        session = self.store.create(cwd=str(self.root), prompt="Review")
        self.sid = session["session_id"]
        self.generation = self.store.claim_owner(self.sid)["generation"]
        self.turn = self.store.claim_turn(self.sid, self.generation)["turn_id"]

    def ask(self, question="Which chapter?", **overrides):
        values = {"session_id": self.sid, "generation": self.generation,
                  "turn_id": self.turn, "question": question}
        return request_question(self.config, **{**values, **overrides})

    def server(self):
        return SessionRequestServer(self.config, self.sid, self.generation, self.turn)

    def test_request_commits_before_reply_and_duplicate_is_idempotent(self):
        with self.server():
            first = self.ask()
            self.assertEqual(first, self.ask())
            self.assertEqual(self.store.snapshot(self.sid)["requests"], [first])
            self.assertEqual(first["kind"], "clarification")
        self.assertEqual(list((self.config.tasks_dir.parent / "session-ipc").iterdir()), [])

    def test_stale_generation_turn_and_request_content_are_refused(self):
        with self.server():
            with self.assertRaisesRegex(StoreError, "generation"):
                self.ask(generation=self.generation + 1)
            first = self.ask()
            with self.assertRaisesRegex(StoreError, "different question"):
                self.ask("Different?", request_id=first["request_id"])
            with self.store.db.transaction(write=True) as c:
                c.execute("UPDATE managed_sessions SET generation=generation+1 WHERE session_id=?", (self.sid,))
            with self.assertRaisesRegex(StoreError, "owner|generation"):
                self.ask()

    def test_unowned_turn_cannot_open_endpoint(self):
        with self.assertRaisesRegex(StoreError, "turn"):
            with SessionRequestServer(self.config, self.sid, self.generation, str(uuid.uuid4())):
                pass

    def test_actor_cli_works_without_opening_database(self):
        from lib.control.sessions import main
        from contextlib import redirect_stdout
        from io import StringIO
        env = {**self.env, "ASHA_MANAGED_STATE_DIR": str(self.config.tasks_dir.parent),
               "ASHA_MANAGED_SESSION_ID": self.sid, "ASHA_MANAGED_GENERATION": str(self.generation),
               "ASHA_MANAGED_TURN_ID": self.turn}
        with self.server(), patch("lib.control.sessions.SessionStore", side_effect=AssertionError("actor opened DB")):
            with redirect_stdout(StringIO()) as output:
                self.assertEqual(main(["ask", "--question", "Which chapter?", "--json"], env=env), 0)
        self.assertEqual(json.loads(output.getvalue())["question"], "Which chapter?")

    def test_missing_owner_endpoint_has_no_database_fallback(self):
        with self.assertRaises(StoreError):
            self.ask()
        self.assertEqual(self.store.snapshot(self.sid)["requests"], [])

    def test_database_failure_refuses_receipt_and_persists_no_question(self):
        with self.server():
            with patch("lib.control.session_ipc.SessionStore.request", side_effect=StoreError("storage full")):
                with self.assertRaisesRegex(StoreError, "storage full"):
                    self.ask()
        self.assertEqual(self.store.snapshot(self.sid)["requests"], [])

    def test_real_child_can_ask_while_owner_waits_on_tool(self):
        code = "from lib.control.session_ipc import request_question; from lib.control.config import load_config; import json,sys; print(json.dumps(request_question(load_config(), **json.loads(sys.argv[1]))))"
        with self.server():
            result = subprocess.run([sys.executable, "-c", code, json.dumps({
                "session_id": self.sid, "generation": self.generation, "turn_id": self.turn,
                "question": "A child asks?"})], env={**os.environ, **self.env},
                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["question"], "A child asks?")

    def test_peer_proof_is_required_even_with_correct_labels(self):
        with self.server(), patch("lib.control.session_ipc.caller_descends_from", return_value=False):
            with self.assertRaises(StoreError):
                self.ask()
        self.assertEqual(self.store.snapshot(self.sid)["requests"], [])

    def test_endpoint_is_private_and_never_replaces_existing_listener(self):
        with self.server() as first:
            self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(StoreError):
                with self.server():
                    pass
            self.assertEqual(self.ask()["question"], "Which chapter?")

    def connect(self, server):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        directory = os.open(server.path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            connection.connect(f"/proc/self/fd/{directory}/{server.path.name}")
        finally:
            os.close(directory)
        return connection

    def raw(self, server, value):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        with self.connect(server) as connection:
            connection.sendall(struct.pack("!I", len(raw)) + raw)
            with connection.makefile("rb") as reader:
                size = struct.unpack("!I", reader.read(4))[0]
                return json.loads(reader.read(size))

    def wire_request(self, **overrides):
        return {"protocol": PROTOCOL, "kind": "ask", "session_id": self.sid,
                "generation": self.generation, "turn_id": self.turn,
                "request_id": str(uuid.uuid4()), "question": "Question?", **overrides}

    def test_operator_operations_cross_turn_and_duplicate_fields_are_refused(self):
        with self.server() as server:
            for body in (self.wire_request(kind="answer"),
                         self.wire_request(turn_id=str(uuid.uuid4())),
                         self.wire_request(session_id=str(uuid.uuid4())),
                         self.wire_request(generation=True),
                         self.wire_request(approval="yes"),
                         b'{"kind":"ask","kind":"answer"}'):
                result = self.raw(server, body)
                self.assertFalse(result["ok"], body)
            self.assertEqual(self.store.snapshot(self.sid)["requests"], [])
            self.assertEqual(self.ask()["kind"], "clarification")

    def test_oversized_frame_is_rejected_without_reading_its_body(self):
        with self.server() as server, self.connect(server) as connection:
            connection.sendall(struct.pack("!I", MAX_FRAME + 1))
            with connection.makefile("rb") as reader:
                size = struct.unpack("!I", reader.read(4))[0]
                self.assertIn("bounds", json.loads(reader.read(size))["error"])

    def test_lost_reply_keeps_custody_and_retry_does_not_duplicate(self):
        from lib.control import session_ipc
        original_send = session_ipc._send
        def drop_reply(connection, value, deadline, **kwargs):
            if value.get("ok") is True:
                raise OSError("reply connection lost")
            return original_send(connection, value, deadline, **kwargs)
        with self.server():
            with patch("lib.control.session_ipc._send", side_effect=drop_reply):
                with self.assertRaises(StoreError):
                    self.ask()
            request = self.ask()
            self.assertEqual(self.store.snapshot(self.sid)["requests"], [request])

    def test_owner_shutdown_interrupts_incomplete_frames(self):
        server = self.server().__enter__()
        with self.connect(server) as connection:
            connection.sendall(b"\x00")
            until = time.monotonic() + 2
            while server.active_connection is None and time.monotonic() < until:
                time.sleep(0.01)
            started = time.monotonic()
            server.close()
            self.assertLess(time.monotonic() - started, 2)

    def test_missing_kernel_peer_proof_is_explicit_and_does_not_launch(self):
        with patch("lib.control.session_ipc.capability_probe", return_value={"supported": False, "reason": "peer pidfd unavailable"}):
            with self.assertRaisesRegex(StoreError, "peer pidfd unavailable"):
                with self.server():
                    pass
        self.assertEqual(self.store.snapshot(self.sid)["requests"], [])

    def test_real_unrelated_peer_cannot_impersonate_an_actor(self):
        second = self.store.create(cwd=str(self.root), prompt="Other owned session")["session_id"]
        ready = self.root / "ready.json"
        stop = self.root / "stop"
        code = (
            "import sys,json,time; from pathlib import Path; from lib.control.config import load_config; "
            "from lib.control.session_store import SessionStore; from lib.control.session_ipc import SessionRequestServer; "
            "config=load_config(); sid=sys.argv[1]; store=SessionStore(config); owner=store.claim_owner(sid); "
            "turn=store.claim_turn(sid,owner['generation'])['turn_id']; "
            "server=SessionRequestServer(config,sid,owner['generation'],turn).__enter__(); "
            "Path(sys.argv[2]).write_text(json.dumps({'turn':turn,'generation':owner['generation']})); "
            "\nwhile not Path(sys.argv[3]).exists(): time.sleep(0.02)\nserver.close(); store.close()"
        )
        child = subprocess.Popen([sys.executable, "-c", code, second, str(ready), str(stop)],
            env={**os.environ, **self.env}, cwd=Path(__file__).resolve().parents[2])
        try:
            until = time.monotonic() + 5
            while not ready.exists() and child.poll() is None and time.monotonic() < until:
                time.sleep(0.02)
            self.assertTrue(ready.exists())
            values = json.loads(ready.read_text())
            with self.assertRaisesRegex(StoreError, "ancestry"):
                request_question(self.config, session_id=second, generation=values["generation"],
                    turn_id=values["turn"], question="Not a child of that owner")
            self.assertEqual(self.store.snapshot(second)["requests"], [])
        finally:
            stop.touch()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)

    def test_busy_write_retries_same_request_without_killing_turn(self):
        from lib.control.sessions import run_turn
        original = SessionStore.request
        seen = []
        def busy_once(store, *args, **kwargs):
            seen.append(kwargs["request_id"])
            if len(seen) == 1:
                raise DatabaseBusyError("temporary writer contention")
            return original(store, *args, **kwargs)
        test = self
        class Provider:
            def __init__(self, *_args, **_kwargs):
                pass
            def events(self, _prompt, *, cancelled):
                yield "initialized", {"native_id": "fake"}
                test.ask()
                test.assertFalse(cancelled())
                yield "completed", {"summary": "Question retained"}
        message = {"turn_id": self.turn, "body": "Ask a question"}
        with patch("lib.control.session_ipc.SessionStore.request", autospec=True, side_effect=busy_once):
            run_turn(self.store, self.store.get(self.sid), message, env=self.env,
                root=Path(__file__).resolve().parents[2], transport_factory=Provider)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(self.store.get(self.sid)["state"], "waiting-input")
        self.assertEqual(len(self.store.snapshot(self.sid)["requests"]), 1)

    def test_shutdown_drains_pending_commit_and_preserves_completed_turn(self):
        from lib.control.sessions import run_turn
        original = SessionStore.request
        started, release = threading.Event(), threading.Event()
        def slow_request(store, *args, **kwargs):
            started.set()
            if not release.wait(3):
                raise StoreError("test release was lost")
            return original(store, *args, **kwargs)
        def client():
            try:
                self.ask()
            except StoreError:
                pass  # Closing the owner may withdraw the reply after custody.
        child = threading.Thread(target=client)
        timer = threading.Timer(0.2, release.set)
        test = self
        class Provider:
            def __init__(self, *_args, **_kwargs):
                pass
            def events(self, _prompt, *, cancelled):
                yield "initialized", {"native_id": "fake"}
                child.start()
                test.assertTrue(started.wait(2))
                timer.start()
                yield "completed", {"summary": "Finished"}
        try:
            with patch("lib.control.session_ipc.SessionStore.request", autospec=True, side_effect=slow_request):
                run_turn(self.store, self.store.get(self.sid), {"turn_id": self.turn, "body": "Review"},
                    env=self.env, root=Path(__file__).resolve().parents[2], transport_factory=Provider)
            self.assertEqual(self.store.get(self.sid)["state"], "waiting-input")
            with self.store.db.transaction() as c:
                self.assertEqual(c.execute("SELECT state FROM session_turns WHERE turn_id=?", (self.turn,)).fetchone()[0], "completed")
            self.assertEqual(list((self.config.tasks_dir.parent / "session-ipc").iterdir()), [])
        finally:
            release.set()
            if child.ident:
                child.join(3)
            timer.cancel()
            if timer.ident:
                timer.join(3)

    def test_unobservable_peer_pid_returns_refusal_and_keeps_server_usable(self):
        with self.server() as server:
            with patch("lib.control.session_ipc._peer", side_effect=lambda _connection: (0, os.geteuid(), os.pidfd_open(os.getpid()))):
                response = self.raw(server, self.wire_request())
            self.assertFalse(response["ok"])
            server.check()
            self.assertEqual(self.ask()["kind"], "clarification")

    def test_error_reply_still_arrives_after_a_lost_success_reply(self):
        from lib.control import session_ipc
        original_send = session_ipc._send
        retained_id = str(uuid.uuid4())
        def drop_success(connection, value, deadline, **kwargs):
            if value.get("ok") is True:
                raise OSError("lost success reply")
            return original_send(connection, value, deadline, **kwargs)
        with self.server(), patch("lib.control.session_ipc._send", side_effect=drop_success):
            with self.assertRaises(StoreError):
                self.ask(request_id=retained_id)
            with self.assertRaisesRegex(StoreError, "different question"):
                self.ask("Changed question", request_id=retained_id)
