"""Transactional managed-session custody, turn claims, and outstanding requests.

This database owns the new session domain. Existing initiative records remain
authoritative for plans, work attempts, approvals and accepted evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any
from types import SimpleNamespace

from .database import ControlDatabase
from .harness import caller_descends_from, process_identity, verify_process, _process_stat_fields
from .store import StoreError


MAX_RUNNING_TURNS = 2


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS managed_sessions (
        session_id TEXT PRIMARY KEY, initiative_id TEXT, harness TEXT NOT NULL,
        cwd TEXT NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
        owner_pid INTEGER, owner_identity TEXT, native_id TEXT,
        event_cursor INTEGER NOT NULL DEFAULT 0, turns INTEGER NOT NULL DEFAULT 0,
        max_turns INTEGER NOT NULL, stop_requested INTEGER NOT NULL DEFAULT 0,
        owner_launch_attempts INTEGER NOT NULL DEFAULT 0,
        owner_launch_after REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS managed_session_state ON managed_sessions(state,created_at,session_id)",
    """CREATE TABLE IF NOT EXISTS session_messages (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL UNIQUE,
        session_id TEXT NOT NULL REFERENCES managed_sessions(session_id),
        delivery_key TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL,
        state TEXT NOT NULL, turn_id TEXT, reason TEXT, created_at REAL NOT NULL,
        UNIQUE(session_id,delivery_key))""",
    """CREATE INDEX IF NOT EXISTS session_pending
        ON session_messages(session_id,state,sequence)""",
    "CREATE INDEX IF NOT EXISTS session_message_state ON session_messages(state,session_id,sequence)",
    """CREATE TABLE IF NOT EXISTS session_turns (
        turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES managed_sessions(session_id),
        message_id TEXT NOT NULL UNIQUE REFERENCES session_messages(message_id),
        generation INTEGER NOT NULL, state TEXT NOT NULL, started_at REAL NOT NULL,
        finished_at REAL, provider_pid INTEGER, provider_identity TEXT)""",
    "CREATE INDEX IF NOT EXISTS session_turn_state ON session_turns(state,session_id)",
    """CREATE TABLE IF NOT EXISTS session_requests (
        request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES managed_sessions(session_id),
        turn_id TEXT NOT NULL REFERENCES session_turns(turn_id), kind TEXT NOT NULL,
        question TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
        answer TEXT, created_at REAL NOT NULL, resolved_at REAL)""",
    """CREATE INDEX IF NOT EXISTS session_requests_pending
        ON session_requests(session_id,state,created_at)""",
    "CREATE INDEX IF NOT EXISTS session_request_state ON session_requests(state,created_at,request_id)",
    """CREATE TABLE IF NOT EXISTS session_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL REFERENCES managed_sessions(session_id),
        turn_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS session_event_cursor
        ON session_events(session_id,sequence)""",
)


def identifier(value: str) -> str:
    try:
        valid = isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        valid = False
    if not valid:
        raise StoreError("session identifier must be a canonical UUID")
    return value


def text(value: Any, label: str, maximum: int = 64 * 1024) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > maximum or "\x00" in value:
        raise StoreError(f"invalid {label}")
    return value


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def process_live(pid, identity):
    if not verify_process(pid, identity):
        return False
    fields = _process_stat_fields(pid)
    return fields is not None and fields[0] not in {b"Z", b"X"}


def anchor_for(state_dir, session):
    return {"kind": "managed-session-v1", "state_dir": str(state_dir),
            "session_id": session["session_id"], "generation": session["generation"],
            "owner_pid": session["owner_pid"], "process_start_identity": session["owner_identity"]}


def verify_anchor(anchor, *, caller=False):
    """Labels never prove a role: compare retained owner and live process incarnation."""
    from .orchestration.model import validate_message_anchor
    validate_message_anchor(anchor)
    config = SimpleNamespace(tasks_dir=Path(anchor["state_dir"]) / "tasks")
    with SessionStore(config) as store:
        session = store.get(anchor["session_id"])
    if anchor_for(anchor["state_dir"], session) != anchor:
        raise StoreError("managed session anchor is stale")
    if session["state"] in {"stopped", "failed"} or session["stop_requested"]:
        raise StoreError("managed session is unavailable")
    pid = session["owner_pid"]
    if not process_live(pid, session["owner_identity"]) or Path(f"/proc/{pid}").stat().st_uid != os.geteuid():
        raise StoreError("managed owner is gone or foreign")
    if caller and not caller_descends_from(pid):
        raise StoreError("caller is outside the managed session owner")
    return session


def caller_anchor(env):
    sid = identifier(env.get("ASHA_MANAGED_SESSION_ID"))
    directory = Path(text(env.get("ASHA_MANAGED_STATE_DIR"), "managed state directory", 4096))
    config = SimpleNamespace(tasks_dir=directory / "tasks")
    with SessionStore(config) as store:
        session = store.get(sid)
    anchor = anchor_for(directory, session)
    verify_anchor(anchor, caller=True)
    if str(session["generation"]) != env.get("ASHA_MANAGED_GENERATION"):
        raise StoreError("managed session generation selector is stale")
    return anchor


class SessionsUninitialized(StoreError):
    pass


class SessionStore:
    def __init__(self, config, *, create=False):
        self.db = ControlDatabase(config, create=create, initialize=self._initialize)
        try:
            with self.db.transaction(write=create) as c:
                if create:
                    c.execute("CREATE TABLE IF NOT EXISTS session_schema (version INTEGER PRIMARY KEY)")
                elif not c.execute("SELECT 1 FROM sqlite_master WHERE name='session_schema'").fetchone():
                    raise SessionsUninitialized("managed sessions are not initialized; run asha control session init")
                version = c.execute("SELECT version FROM session_schema").fetchall()
                if not version and create:
                    self._initialize(c)
                elif [r[0] for r in version] != [1]:
                    raise StoreError("unsupported managed session schema")
        except BaseException:
            self.db.close()
            raise

    @staticmethod
    def _initialize(c):
        c.execute("CREATE TABLE IF NOT EXISTS session_schema (version INTEGER PRIMARY KEY)")
        for statement in SCHEMA:
            c.execute(statement)
        ControlDatabase.install_message_search(c)
        from .native_requests import install
        install(c)
        c.execute("INSERT INTO session_schema VALUES(1)")

    def close(self):
        self.db.close()

    def search(self, query, *, session_id=None, after=0, limit=50):
        """Read indexed message text with a stable custody cursor and scope."""
        if type(after) is not int or after < 0:
            raise StoreError("search cursor must be a nonnegative integer")
        self.db._limit(limit)
        clauses = ["messages_search MATCH ?", "m.sequence>?"]
        parameters = [self.db.search_phrase(query), after]
        if session_id is not None:
            clauses.append("m.session_id=?")
            parameters.append(identifier(session_id))
        with self.db.transaction() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT m.* FROM session_messages m JOIN messages_search ON messages_search.rowid=m.sequence WHERE "
                + " AND ".join(clauses) + " ORDER BY m.sequence LIMIT ?", (*parameters, limit + 1))]
        return {"messages": rows[:limit], "complete": len(rows) <= limit,
                "next_cursor": rows[min(limit, len(rows)) - 1]["sequence"] if rows else after}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _event(c, sid, kind, payload, turn=None):
        from .session_output import OUTPUT_KINDS, envelope, retain
        stored = envelope(payload, kind) if kind in OUTPUT_KINDS else payload
        cursor = c.execute("INSERT INTO session_events(session_id,turn_id,kind,payload,created_at) VALUES(?,?,?,?,?)",
                  (sid, turn, kind, json.dumps(stored, ensure_ascii=True, allow_nan=False), time.time()))
        if kind in OUTPUT_KINDS:
            retain(c, sid, cursor.lastrowid, payload)

    @staticmethod
    def _session(c, sid):
        row = c.execute("SELECT * FROM managed_sessions WHERE session_id=?", (identifier(sid),)).fetchone()
        if row is None:
            raise StoreError("managed session not found")
        from .provider_recovery import recovery
        return {**dict(row), "recovery": recovery(c, sid),
                "recovery_revision": SessionStore._recovery_revision(c, sid)}

    @staticmethod
    def _recovery_revision(c, sid):
        return c.execute("SELECT COALESCE(MAX(sequence),0) FROM session_events WHERE session_id=?", (sid,)).fetchone()[0]

    def get(self, sid):
        with self.db.transaction() as c:
            return self._session(c, sid)

    def create(self, *, cwd: str, prompt: str, harness="claude", initiative_id=None, max_turns=12):
        with self.db.transaction(write=True) as c:
            sid = self._create_in_transaction(c, cwd=cwd, prompt=prompt, harness=harness,
                                               initiative_id=initiative_id, max_turns=max_turns)
        return self.get(sid)

    def _create_in_transaction(self, c, *, cwd, prompt, harness='claude', initiative_id=None,
                               max_turns=12, session_id=None):
        directory = Path(cwd)
        if not directory.is_absolute() or str(directory.resolve()) != cwd or not directory.is_dir():
            raise StoreError("session cwd must be an existing canonical absolute directory")
        from .session_harness import CAPABILITIES
        if not CAPABILITIES.get(harness, {}).get("managed"):
            raise StoreError("harness has no supported managed adapter")
        if type(max_turns) is not int or not 1 <= max_turns <= 100:
            raise StoreError("max turns must be between 1 and 100")
        if initiative_id is not None:
            identifier(initiative_id)
        text(prompt, "prompt")
        sid = identifier(session_id) if session_id is not None else str(uuid.uuid4())
        now = time.time()
        if initiative_id and c.execute("SELECT 1 FROM managed_sessions WHERE initiative_id=? AND state NOT IN ('stopped','failed')", (initiative_id,)).fetchone():
            raise StoreError("initiative already has a managed session")
        c.execute("""INSERT INTO managed_sessions(session_id,initiative_id,harness,cwd,state,max_turns,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?)""", (sid, initiative_id, harness, cwd, "queued", max_turns, now, now))
        self._enqueue(c, sid, prompt, "opening")
        self._event(c, sid, "session-created", {"harness": harness, "initiative_id": initiative_id})
        return sid

    def _enqueue(self, c, sid, body, key):
        text(body, "message")
        text(key, "delivery key", 512)
        old = c.execute("SELECT * FROM session_messages WHERE session_id=? AND delivery_key=?", (sid, key)).fetchone()
        if old:
            if old["digest"] != digest(body):
                raise StoreError("delivery key reused with different content")
            return dict(old)
        mid = str(uuid.uuid4())
        c.execute("""INSERT INTO session_messages(message_id,session_id,delivery_key,body,digest,state,created_at)
                     VALUES(?,?,?,?,?,'queued',?)""", (mid, sid, key, body, digest(body), time.time()))
        self._event(c, sid, "message-retained", {"message_id": mid, "digest": digest(body)})
        return dict(c.execute("SELECT * FROM session_messages WHERE message_id=?", (mid,)).fetchone())

    def enqueue(self, sid, body, *, key, on_retained=None):
        from .experience_review import owned
        with self.db.transaction() as c:
            if owned(c, sid):
                raise StoreError("experience review is a single-turn utility; resume/followup refused")
        with self.db.transaction(write=True) as c:
            s = self._session(c, sid)
            if s["state"] in {"stopped", "failed"} or s["stop_requested"]:
                raise StoreError("session is stopped or failed")
            message = self._enqueue(c, sid, body, key)
            if on_retained:
                on_retained(c, message)
            return message

    def claim_owner(self, sid, *, pid=None):
        pid = os.getpid() if pid is None else pid
        identity = process_identity(pid)
        if identity is None:
            raise StoreError("owner process is absent")
        with self.db.transaction(write=True) as c:
            s = self._session(c, sid)
            if s["owner_pid"] and process_live(s["owner_pid"], s["owner_identity"]):
                if s["owner_pid"] == pid and s["owner_identity"] == identity:
                    return s
                raise StoreError("managed session already has a live owner")
            # Submission might have reached the harness before an owner died.
            active = c.execute("SELECT 1 FROM session_turns WHERE session_id=? AND state='running'", (sid,)).fetchone()
            if active:
                from .native_requests import close_turn
                for row in c.execute("SELECT turn_id FROM session_turns WHERE session_id=? AND state='running'", (sid,)).fetchall():
                    close_turn(c, row[0], reason="owner lost")
                    from .provider_recovery import record_failure
                    record_failure(c, sid, row[0], s["generation"], "owner lost during turn")
                c.execute("UPDATE session_turns SET state='uncertain' WHERE session_id=? AND state='running'", (sid,))
                c.execute("UPDATE session_messages SET state='uncertain',reason='owner lost during turn' WHERE session_id=? AND state='submitted'", (sid,))
            state = "uncertain" if active else s["state"]
            c.execute("UPDATE managed_sessions SET generation=generation+1,owner_pid=?,owner_identity=?,state=?,updated_at=?,owner_launch_attempts=0,owner_launch_after=0 WHERE session_id=?",
                      (pid, identity, state, time.time(), sid))
            self._event(c, sid, "owner-claimed", {"pid": pid, "generation": s["generation"] + 1})
        return self.get(sid)

    def _owner(self, c, sid, generation):
        s = self._session(c, sid)
        if s["generation"] != generation or s["owner_pid"] != os.getpid() or not verify_process(s["owner_pid"], s["owner_identity"]):
            raise StoreError("stale or foreign session owner")
        return s

    def claim_turn(self, sid, generation):
        with self.db.transaction(write=True) as c:
            s = self._owner(c, sid, generation)
            from .runtime import read_policy
            if read_policy(c)["mode"] != "running":
                return None
            if s["stop_requested"] or s["state"] in {"running", "uncertain", "stopped", "failed"}:
                return None
            if c.execute("SELECT 1 FROM session_requests WHERE session_id=? AND state='pending'", (sid,)).fetchone():
                return None
            msg = c.execute("SELECT * FROM session_messages WHERE session_id=? AND state='queued' ORDER BY CASE WHEN delivery_key LIKE 'recovery:%' THEN 0 WHEN delivery_key LIKE 'answer:%' THEN 1 ELSE 2 END,sequence LIMIT 1", (sid,)).fetchone()
            if msg is None:
                return None
            if s["turns"] >= s["max_turns"]:
                if s["state"] != "budget-exhausted":
                    c.execute("UPDATE managed_sessions SET state='budget-exhausted' WHERE session_id=?", (sid,))
                    self._event(c, sid, "budget-exhausted", {"max_turns": s["max_turns"]})
                return None
            if c.execute("SELECT count(*) FROM session_turns WHERE state='running'").fetchone()[0] >= MAX_RUNNING_TURNS:
                return None
            turn = str(uuid.uuid4())
            c.execute("INSERT INTO session_turns(turn_id,session_id,message_id,generation,state,started_at) VALUES(?,?,?,?,'running',?)", (turn, sid, msg["message_id"], generation, time.time()))
            c.execute("UPDATE session_messages SET state='submitted',turn_id=? WHERE message_id=?", (turn, msg["message_id"]))
            c.execute("UPDATE managed_sessions SET state='running',turns=turns+1,updated_at=? WHERE session_id=?", (time.time(), sid))
            self._event(c, sid, "turn-reserved", {"message_id": msg["message_id"]}, turn)
            return {**dict(msg), "turn_id": turn}

    def bind_provider(self, sid, generation, turn, pid):
        identity = process_identity(pid)
        if not identity or os.getpgid(pid) != pid or not caller_descends_from(os.getpid(), start_pid=pid):
            raise StoreError("provider process group is not owned by this controller")
        with self.db.transaction(write=True) as c:
            self._owner(c, sid, generation)
            t = c.execute("SELECT * FROM session_turns WHERE turn_id=? AND session_id=?", (turn, sid)).fetchone()
            if t is None or t["state"] != "running" or t["generation"] != generation or t["provider_pid"] is not None:
                raise StoreError("provider cannot bind this turn")
            c.execute("UPDATE session_turns SET provider_pid=?,provider_identity=? WHERE turn_id=?", (pid, identity, turn))
            self._event(c, sid, "provider-bound", {"pid": pid, "process_identity": identity}, turn)

    @staticmethod
    def _provider_live(c, sid):
        return any(process_live(r[0], r[1]) for r in c.execute(
            "SELECT provider_pid,provider_identity FROM session_turns WHERE session_id=? AND provider_pid IS NOT NULL", (sid,)))

    def observe(self, sid, generation, turn, kind, payload):
        if not isinstance(payload, dict):
            raise StoreError("session event payload must be an object")
        if kind not in {"initialized", "text", "tool", "progress", "consumed", "completed", "failed", "provider-status"}:
            raise StoreError("unknown session event")
        encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False)
        if len(encoded.encode()) > 256 * 1024:
            raise StoreError("session event exceeds limit")
        with self.db.transaction(write=True) as c:
            self._owner(c, sid, generation)
            t = c.execute("SELECT * FROM session_turns WHERE turn_id=? AND session_id=?", (turn, sid)).fetchone()
            if t is None or t["generation"] != generation or t["state"] != "running":
                raise StoreError("event does not belong to the running turn")
            if kind == "initialized":
                native = text(payload.get("native_id"), "native session ID", 512)
                old = self._session(c, sid)["native_id"]
                if old and old != native:
                    raise StoreError("native resume changed conversation identity")
                c.execute("UPDATE managed_sessions SET native_id=? WHERE session_id=?", (native, sid))
            elif kind == "consumed":
                if payload.get("digest") != c.execute("SELECT digest FROM session_messages WHERE message_id=?", (t["message_id"],)).fetchone()[0]:
                    raise StoreError("consumption evidence has wrong digest")
                c.execute("UPDATE session_messages SET state='consumed' WHERE message_id=?", (t["message_id"],))
            elif kind == "provider-status":
                from .provider_recovery import observe_status
                if payload.get("provider") != self._session(c, sid)["harness"]:
                    raise StoreError("provider observation belongs to another harness")
                observe_status(c, sid, turn, payload)
            if kind in {"completed", "failed"}:
                from .provider_recovery import observe_terminal
                observe_terminal(c, sid, turn, kind)
            self._event(c, sid, kind, payload, turn)

    def finish(self, sid, generation, turn, *, success, reason=None, input_not_submitted=False):
        if reason is not None:
            reason = str(reason)[:1000].encode("utf-8", "backslashreplace").decode()
        with self.db.transaction(write=True) as c:
            self._owner(c, sid, generation)
            t = c.execute("SELECT * FROM session_turns WHERE turn_id=? AND session_id=?", (turn, sid)).fetchone()
            if t is None or t["generation"] != generation or t["state"] != "running":
                raise StoreError("cannot finish an unowned or terminal turn")
            from .native_requests import close_turn
            close_turn(c, turn, reason=reason)
            state = "completed" if success else "failed"
            from .record_registry import RecordRegistry
            terminal = RecordRegistry("session-terminal", scope=sid).read(c, turn)
            if input_not_submitted and (success or terminal or c.execute("SELECT state FROM session_messages WHERE message_id=?", (t["message_id"],)).fetchone()[0] == "consumed"):
                raise StoreError("input-not-submitted conflicts with retained provider evidence")
            if terminal:
                state = terminal["value"]["kind"]
            # A later transport failure parks the session but cannot rewrite a
            # terminal result already retained from the provider.
            c.execute("UPDATE session_turns SET state=?,finished_at=? WHERE turn_id=?", (state, time.time(), turn))
            # Completion is not consumption evidence. Keep the strongest observed receipt.
            c.execute("UPDATE session_messages SET reason=? WHERE message_id=?", (reason, t["message_id"]))
            pending = c.execute("SELECT 1 FROM session_requests WHERE session_id=? AND state='pending'", (sid,)).fetchone()
            session_state = ("waiting-input" if pending else "idle") if success else "failed"
            from .provider_recovery import blocks_next_turn, record_failure
            if not success or blocks_next_turn(c, sid, turn):
                condition = record_failure(c, sid, turn, generation,
                    "provider blocked subsequent work after this turn completed" if success else reason,
                    input_not_submitted=input_not_submitted)
                session_state = "failed"
                if condition["delivery"] == "uncertain":
                    c.execute("UPDATE session_messages SET state='uncertain' WHERE message_id=? AND state='submitted'", (t["message_id"],))
                elif condition["delivery"] == "not-submitted":
                    c.execute("UPDATE session_messages SET state='cancelled',reason='provider initialization failed before input release' WHERE message_id=? AND state='submitted'", (t["message_id"],))
            c.execute("UPDATE managed_sessions SET state=?,updated_at=? WHERE session_id=?", (session_state, time.time(), sid))
            self._event(c, sid, "turn-finished", {"outcome": state, "reason": reason}, turn)

    def get_request(self, request_id):
        identifier(request_id)
        with self.db.transaction() as c:
            row = c.execute("SELECT * FROM session_requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                raise StoreError("request not found")
            return dict(row)

    def request(self, sid, turn, question, *, request_id, generation=None):
        identifier(request_id)
        text(question, "question")
        with self.db.transaction(write=True) as c:
            s = self._owner(c, sid, generation) if generation is not None else self._session(c, sid)
            if s["stop_requested"]:
                raise StoreError("question belongs to a stopping session")
            t = c.execute("SELECT * FROM session_turns WHERE turn_id=? AND session_id=?", (turn, sid)).fetchone()
            if t is None or t["state"] != "running" or t["generation"] != s["generation"]:
                raise StoreError("question must belong to the active turn")
            old = c.execute("SELECT * FROM session_requests WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["session_id"] != sid or old["turn_id"] != turn or old["digest"] != digest(question):
                    raise StoreError("request ID reused with different question")
                return dict(old)
            c.execute("INSERT INTO session_requests VALUES(?,?,?,'clarification',?,?,'pending',NULL,?,NULL)",
                      (request_id, sid, turn, question, digest(question), time.time()))
            self._event(c, sid, "request-opened", {"request_id": request_id, "question": question}, turn)
            return dict(c.execute("SELECT * FROM session_requests WHERE request_id=?", (request_id,)).fetchone())

    def answer(self, request_id, answer, *, expected_digest):
        text(answer, "answer")
        with self.db.transaction(write=True) as c:
            r = c.execute("SELECT * FROM session_requests WHERE request_id=?", (identifier(request_id),)).fetchone()
            if r is None or r["digest"] != expected_digest:
                raise StoreError("question missing or changed")
            if r["kind"] != "clarification":
                raise StoreError("native permission requires an explicit permission decision")
            if r["state"] == "answered":
                if r["answer"] != answer:
                    raise StoreError("question already has a different answer")
                return dict(r)
            if r["state"] != "pending":
                raise StoreError("question is no longer pending")
            s = self._session(c, r["session_id"])
            if s["state"] in {"stopped", "failed", "uncertain"} or s["stop_requested"]:
                raise StoreError("question belongs to an unavailable session")
            body = f"Answer to request {request_id}:\n{answer}"
            self._enqueue(c, r["session_id"], body, "answer:" + request_id)
            c.execute("UPDATE session_requests SET state='answered',answer=?,resolved_at=? WHERE request_id=?", (answer, time.time(), request_id))
            if s["state"] == "waiting-input":
                c.execute("UPDATE managed_sessions SET state='idle' WHERE session_id=?", (r["session_id"],))
            self._event(c, r["session_id"], "request-answered", {"request_id": request_id})
            return dict(c.execute("SELECT * FROM session_requests WHERE request_id=?", (request_id,)).fetchone())

    def stop(self, sid):
        with self.db.transaction(write=True) as c:
            s = self._session(c, sid)
            if s["state"] == "stopped":
                return
            if not s["stop_requested"]:
                c.execute("UPDATE managed_sessions SET stop_requested=1 WHERE session_id=?", (sid,))
                self._event(c, sid, "stop-requested", {})
            if not s["owner_pid"] or not process_live(s["owner_pid"], s["owner_identity"]):
                if self._provider_live(c, sid):
                    raise StoreError("owner lost; provider cleanup is still pending; retry stop after its process group exits")
                self._stopped(c, sid)

    def _stopped(self, c, sid):
        if self._provider_live(c, sid):
            raise StoreError("provider cleanup is still pending")
        from .native_requests import close_turn
        for row in c.execute("SELECT turn_id FROM session_turns WHERE session_id=? AND state='running'", (sid,)).fetchall():
            close_turn(c, row[0], reason="session stopped")
        c.execute("UPDATE managed_sessions SET state='stopped',stop_requested=1,updated_at=? WHERE session_id=?", (time.time(), sid))
        c.execute("UPDATE session_requests SET state='cancelled' WHERE session_id=? AND state='pending'", (sid,))
        c.execute("UPDATE session_messages SET state='cancelled' WHERE session_id=? AND state='queued'", (sid,))
        # An interrupted turn may already have acted. Do not call its input cancelled.
        c.execute("UPDATE session_turns SET state='uncertain',finished_at=? WHERE session_id=? AND state='running'", (time.time(), sid))
        c.execute("UPDATE session_messages SET state='uncertain' WHERE session_id=? AND state='submitted' AND turn_id IN (SELECT turn_id FROM session_turns WHERE state='uncertain')", (sid,))
        self._event(c, sid, "session-stopped", {})

    def stopped(self, sid, generation):
        with self.db.transaction(write=True) as c:
            self._owner(c, sid, generation)
            self._stopped(c, sid)

    def require_legacy_handoff(self, sid, initiative_id):
        """Read-only transfer gate, called while holding the initiative lock.

        Stopping execution cannot settle an ambiguous submission. A legacy
        successor must not turn loss of the owner into permission to repeat work.
        """
        with self.db.transaction() as c:
            session = self._session(c, sid)
            if session['initiative_id'] != initiative_id:
                raise StoreError('managed predecessor belongs to another initiative')
            if (session['state'] != 'stopped' or self._provider_live(c, sid)
                    or session['owner_pid'] and process_live(session['owner_pid'], session['owner_identity'])):
                raise StoreError('stop the managed session and wait for its owner/provider before changing transport')
            # Reservation events are ordered durably even if the wall clock
            # moves backward. The event and turn row are committed together.
            latest = c.execute('''SELECT state FROM session_turns WHERE session_id=? AND turn_id=(
                SELECT turn_id FROM session_events WHERE session_id=? AND kind='turn-reserved'
                ORDER BY sequence DESC LIMIT 1)''', (sid, sid)).fetchone()
            if latest is None and c.execute('SELECT 1 FROM session_turns WHERE session_id=? LIMIT 1', (sid,)).fetchone():
                raise StoreError('managed turn reservation evidence is missing; inspect before changing transport')
            recovery = session.get('recovery') or {}
            if (latest and latest['state'] in {'running', 'uncertain'}) or recovery.get('delivery') == 'uncertain':
                raise StoreError('managed submission is uncertain; inspect and resolve it before changing transport')

    @staticmethod
    def recovery_digest(session):
        return digest(json.dumps({**{k: session[k] for k in (
            "session_id", "state", "generation", "turns", "max_turns", "stop_requested", "recovery_revision")},
            "recovery": session.get("recovery")}, sort_keys=True))

    def resume(self, sid, *, prompt, expected_digest, max_turns=None, quota_reset_override=None, on_retained=None):
        from .experience_review import owned
        with self.db.transaction() as c:
            if owned(c, sid):
                raise StoreError("experience review is a single-turn utility; resume/followup refused")
        """Explicit new operator turn; never replay an ambiguous submission."""
        text(prompt, "recovery prompt")
        if quota_reset_override is not None:
            text(quota_reset_override, "quota reset override reason", 1000)
        if max_turns is not None and (type(max_turns) is not int or not 1 <= max_turns <= 100):
            raise StoreError("recovery turn budget must be an integer between 1 and 100")
        with self.db.transaction(write=True) as c:
            s = self._session(c, sid)
            from .provider_recovery import recovery_receipt, resolve
            key = "recovery:" + expected_digest
            old = c.execute("SELECT * FROM session_messages WHERE session_id=? AND delivery_key=?", (sid, key)).fetchone()
            if old:
                if old["digest"] != digest(prompt):
                    raise StoreError("recovery already recorded with different content")
                receipt = recovery_receipt(c, sid, expected_digest, digest(prompt), max_turns, quota_reset_override=quota_reset_override)
                if receipt is None and max_turns is not None:
                    raise StoreError("legacy recovery has no budget amendment receipt")
                latest = c.execute("SELECT payload FROM session_events WHERE session_id=? AND kind='operator-resumed' ORDER BY sequence DESC LIMIT 1", (sid,)).fetchone()
                if (s["state"] in {"failed", "uncertain", "budget-exhausted", "stopped"}
                        or s["stop_requested"] or s.get("recovery") or latest is None
                        or json.loads(latest[0]).get("digest") != expected_digest):
                    raise StoreError("session changed since this recovery command; inspect current recovery")
                return dict(old)
            if self.recovery_digest(s) != expected_digest:
                raise StoreError("session changed since recovery was reviewed")
            if s["state"] not in {"failed", "uncertain", "budget-exhausted", "stopped"}:
                raise StoreError("session does not need recovery")
            if self._provider_live(c, sid):
                raise StoreError("previous provider is still live; recovery cannot start another turn")
            condition = s.get("recovery")
            quota_condition = condition and (condition["category"] == "quota" or any(
                item["reason"] == "rate_limit" for item in condition.get("provider_observations", [])))
            if quota_reset_override is not None and not quota_condition:
                raise StoreError("quota reset override requires a retained quota condition")
            if condition and condition["retry_not_before"] is not None and time.time() < condition["retry_not_before"] and quota_reset_override is None:
                raise StoreError("provider quota reset has not arrived; keep this turn parked")
            budget = s["max_turns"] if max_turns is None else max_turns
            if type(budget) is not int or not s["turns"] < budget <= 100 or budget < s["max_turns"]:
                raise StoreError("recovery requires a remaining turn budget (at most 100)")
            # Recovery instructions supersede unanswered questions from the
            # failed conversation path. Keep each question as cancelled evidence.
            pending = c.execute("SELECT request_id,turn_id FROM session_requests WHERE session_id=? AND kind='clarification' AND state='pending'", (sid,)).fetchall()
            for request in pending:
                c.execute("UPDATE session_requests SET state='cancelled',resolved_at=? WHERE request_id=?", (time.time(), request["request_id"]))
                self._event(c, sid, "request-cancelled", {"request_id": request["request_id"], "reason": "superseded by explicit recovery"}, request["turn_id"])
            c.execute("UPDATE managed_sessions SET state='idle',max_turns=?,updated_at=?,owner_launch_attempts=0,owner_launch_after=0,stop_requested=0 WHERE session_id=?", (budget, time.time(), sid))
            message = self._enqueue(c, sid, prompt, key)
            if on_retained:
                on_retained(c, message)
            recovery_receipt(c, sid, expected_digest, digest(prompt), max_turns, message_id=message["message_id"], quota_reset_override=quota_reset_override)
            resolve(c, sid, message["message_id"], quota_reset_override=quota_reset_override)
            self._event(c, sid, "operator-resumed", {"previous_state": s["state"], "max_turns": budget, "digest": expected_digest,
                                                    "quota_reset_override": quota_reset_override})
            return message

    def fail_owner(self, sid, generation, reason):
        with self.db.transaction(write=True) as c:
            self._owner(c, sid, generation)
            c.execute("UPDATE managed_sessions SET state='failed',updated_at=? WHERE session_id=?", (time.time(), sid))
            self._event(c, sid, "owner-failed", {"reason": str(reason)[:1000]})

    def reserve_owner_launch(self, sid):
        with self.db.transaction(write=True) as c:
            s = self._session(c, sid)
            from .runtime import read_policy
            if read_policy(c)["mode"] != "running":
                return False
            if s["state"] not in {"queued", "idle", "running", "waiting-input"} or s["stop_requested"]:
                return False
            if (s["owner_pid"] and process_live(s["owner_pid"], s["owner_identity"])) or s["owner_launch_after"] > time.time():
                return False
            if s["owner_launch_attempts"] >= 8:
                c.execute("UPDATE managed_sessions SET state='failed' WHERE session_id=?", (sid,))
                self._event(c, sid, "owner-launch-failed", {"reason": "owner did not claim after 8 launches; inspect the session log"})
                return False
            attempt = s["owner_launch_attempts"] + 1
            c.execute("UPDATE managed_sessions SET owner_launch_attempts=?,owner_launch_after=? WHERE session_id=?", (attempt, time.time() + min(300, 5 * 2 ** (attempt - 1)), sid))
            self._event(c, sid, "owner-launch-reserved", {"attempt": attempt})
            return True

    def current_work(self, *, kind="sessions", limit=100, after=None):
        from .session_activity import page
        return page(self, kind=kind, limit=limit, after=after)

    def snapshot(self, sid=None, *, after=0, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000 or type(after) is not int or after < 0:
            raise StoreError("invalid session query bounds")
        with self.db.transaction() as c:
            sessions = [dict(r) for r in c.execute("SELECT * FROM managed_sessions ORDER BY created_at,session_id LIMIT ?", (limit,))] if sid is None else [self._session(c, sid)]
            where, args = ("", ()) if sid is None else (" AND session_id=?", (sid,))
            requests = [dict(r) for r in c.execute("SELECT * FROM session_requests WHERE state='pending'" + where + " ORDER BY created_at,request_id LIMIT ?", (*args, limit))]
            events = [dict(r) for r in c.execute("SELECT * FROM session_events WHERE sequence>?" + where + " ORDER BY sequence LIMIT ?", (after, *args, limit))]
            for event in events:
                from .session_output import project
                project(c, event)
            for session in sessions:
                from .provider_recovery import recovery
                session["recovery"] = recovery(c, session["session_id"])
                session["recovery_revision"] = self._recovery_revision(c, session["session_id"])
                session["recovery_digest"] = self.recovery_digest(session)
            counts = dict(c.execute("SELECT state,count(*) FROM managed_sessions WHERE 1=1" + where + " GROUP BY state", args).fetchall())
            message_counts = dict(c.execute("SELECT state,count(*) FROM session_messages WHERE 1=1" + where + " GROUP BY state", args).fetchall())
            request_count = c.execute("SELECT count(*) FROM session_requests WHERE state='pending'" + where, args).fetchone()[0]
            messages = [dict(r) for r in c.execute("SELECT * FROM session_messages WHERE 1=1" + where + " ORDER BY sequence DESC LIMIT ?", (*args, limit))]
            remaining_events = c.execute("SELECT 1 FROM session_events WHERE sequence>?" + where + " LIMIT 1", (events[-1]["sequence"] if events else after, *args)).fetchone()
            return {"sessions": sessions, "requests": requests, "events": events,
                    "messages": messages, "counts": counts, "message_counts": message_counts,
                    "pending_request_count": request_count, "limit": limit,
                    "complete": {"sessions": sum(counts.values()) <= len(sessions),
                                 "requests": request_count <= len(requests), "events": remaining_events is None,
                                 "messages": sum(message_counts.values()) <= len(messages)},
                    "next_event_cursor": events[-1]["sequence"] if events else after,
                    "output_gaps": [{"sequence": e["sequence"], "session_id": e["session_id"], **e["output"]}
                                    for e in events if e.get("output", {}).get("available") is False]}

    def events(self, sid, *, consumer=None, after=None, limit=100):
        """A bounded event page; reading never acknowledges delivery."""
        from .session_output import project, consumer_record
        identifier(sid)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise StoreError("invalid event page limit")
        if after is not None and (type(after) is not int or not 0 <= after <= 9223372036854775807):
            raise StoreError("invalid event cursor")
        if consumer is not None:
            text(consumer, "event consumer", 128)
        with self.db.transaction() as c:
            self._session(c, sid)
            saved = consumer_record(c, sid, consumer) if consumer else None
            acknowledged = saved["value"]["through"] if saved else 0
            cursor = acknowledged if after is None else after
            rows = [dict(r) for r in c.execute("SELECT * FROM session_events WHERE session_id=? AND sequence>? ORDER BY sequence LIMIT ?", (sid, cursor, limit + 1))]
            events = rows[:limit]
            for event in events:
                project(c, event)
            return {"session_id": sid, "consumer": consumer, "acknowledged_cursor": acknowledged,
                    "events": events, "complete": len(rows) <= limit,
                    "next_event_cursor": events[-1]["sequence"] if events else cursor,
                    "output_gaps": [{"sequence": e["sequence"], **e["output"]}
                                    for e in events if e.get("output", {}).get("available") is False]}

    def acknowledge_events(self, sid, consumer, through):
        """Durable monotonic delivery acknowledgement, never model consumption."""
        from .record_registry import RecordRegistry
        from .session_output import encode, consumer_record, timestamp, MAX_CONSUMERS
        identifier(sid)
        text(consumer, "event consumer", 128)
        if type(through) is not int or not 0 <= through <= 9223372036854775807:
            raise StoreError("invalid acknowledged event cursor")
        with self.db.transaction(write=True) as c:
            self._session(c, sid)
            if through and c.execute("SELECT 1 FROM session_events WHERE session_id=? AND sequence=?", (sid, through)).fetchone() is None:
                raise StoreError("acknowledgement does not identify an event in this session")
            registry = RecordRegistry("session-event-consumers", scope=sid)
            old = consumer_record(c, sid, consumer)
            if old is None and c.execute("SELECT count(*) FROM records WHERE domain='session-event-consumers' AND scope=?", (sid,)).fetchone()[0] >= MAX_CONSUMERS:
                raise StoreError("session event consumer limit reached; reuse an existing consumer name")
            if old and old["value"]["through"] >= through:
                return old["value"]
            value = {"session_id": sid, "consumer": consumer, "through": through}
            registry.put(c, consumer, encode(value), state="acknowledged", expected_digest=old["digest"] if old else None,
                         updated_at=timestamp())
            return value
