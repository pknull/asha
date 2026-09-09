"""Exact-invocation native permission decisions and durable reply custody."""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid

from .store import StoreError


def install(c):
    c.execute("""CREATE TABLE IF NOT EXISTS session_native_requests (
        request_id TEXT PRIMARY KEY REFERENCES session_requests(request_id),
        turn_id TEXT NOT NULL REFERENCES session_turns(turn_id), generation INTEGER NOT NULL,
        provider_request_id TEXT NOT NULL, payload TEXT NOT NULL,
        response_state TEXT NOT NULL CHECK(response_state IN
            ('pending','queued','submitting','submitted','cancelled','uncertain')),
        decided_by TEXT, decision_reason TEXT, submitted_at REAL,
        UNIQUE(turn_id,provider_request_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS native_reply_queue ON session_native_requests(turn_id,response_state,request_id)")


def close_turn(c, turn, *, reason):
    """Keep recorded decisions, withdraw unanswered questions, never replay replies."""
    from .session_store import SessionStore
    rows = c.execute("""SELECT r.session_id,n.request_id,n.response_state
        FROM session_native_requests n JOIN session_requests r USING(request_id)
        WHERE n.turn_id=? AND n.response_state IN ('pending','queued','submitting')""", (turn,)).fetchall()
    c.execute("UPDATE session_requests SET state='cancelled',resolved_at=? WHERE turn_id=? AND kind IN ('native-permission','native-clarification') AND state='pending'", (time.time(), turn))
    c.execute("UPDATE session_native_requests SET response_state=CASE WHEN response_state='submitting' THEN 'uncertain' ELSE 'cancelled' END WHERE turn_id=? AND response_state IN ('pending','queued','submitting')", (turn,))
    for row in rows:
        SessionStore._event(c, row["session_id"], "native-permission-closed", {
            "request_id": row["request_id"], "previous_state": row["response_state"],
            "response_state": "uncertain" if row["response_state"] == "submitting" else "cancelled",
            "reason": reason}, turn)


class NativeRequests:
    def __init__(self, store):
        self.store = store

    def _active(self, c, sid, generation, turn, *, owner=True, allow_stopping=False):
        session = self.store._owner(c, sid, generation) if owner else self.store._session(c, sid)
        actual = c.execute("SELECT state,generation FROM session_turns WHERE turn_id=? AND session_id=?", (turn, sid)).fetchone()
        if (session["generation"] != generation or (session["stop_requested"] and not allow_stopping) or actual is None
                or actual["generation"] != generation or actual["state"] != "running"):
            raise StoreError("native request belongs to an unavailable turn or generation")
        return session

    @staticmethod
    def _read(c, request_id):
        row = c.execute("""SELECT r.*,s.cwd,n.generation,n.provider_request_id,n.payload,n.response_state,
            n.decided_by,n.decision_reason,n.submitted_at FROM session_requests r
            JOIN session_native_requests n USING(request_id)
            JOIN managed_sessions s ON s.session_id=r.session_id WHERE r.request_id=?""", (request_id,)).fetchone()
        if row is None:
            raise StoreError("native permission request not found")
        return {**dict(row), "payload": json.loads(row["payload"])}

    def get(self, request_id):
        with self.store.db.transaction() as c:
            return self._read(c, request_id)

    def open(self, sid, generation, turn, provider_request_id, payload):
        from .session_store import text
        text(provider_request_id, "provider request ID", 512)
        codex = isinstance(payload, dict) and payload.get("protocol") == "codex-app-server-v2"
        if codex:
            from .codex_native import describe
            kind, question = describe(payload)
        else:
            if not isinstance(payload, dict) or payload.get("subtype") != "can_use_tool" or not isinstance(payload.get("input"), dict):
                raise StoreError("unsupported native permission request")
            text(payload.get("tool_name"), "native tool name", 512)
            kind = "native-permission"
            question = f"Allow native tool {payload['tool_name']} for this invocation?\n" + json.dumps(payload["input"], ensure_ascii=True, sort_keys=True)
            reason = payload.get("decision_reason") or payload.get("description")
            if reason:
                question += "\nProvider reason: " + json.dumps(reason, ensure_ascii=True)
        try:
            body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise StoreError("invalid native permission JSON") from exc
        from .codex_protocol import MAX_REVIEW_BYTES
        if len(body.encode()) > (MAX_REVIEW_BYTES if codex else 32768):
            raise StoreError("native permission request exceeds size limit")
        note_cwd, note = None, ""
        if codex:
            from .codex_native import scope_note
            with self.store.db.transaction() as c:
                note_cwd = self.store._session(c, sid)["cwd"]
            # Path resolution can touch a slow mount; never hold a database
            # writer while producing the informative scope notice.
            note = scope_note(payload, note_cwd)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"asha-native:{sid}:{turn}:{provider_request_id}"))
        with self.store.db.transaction(write=True) as c:
            session = self._active(c, sid, generation, turn)
            if not session["native_id"]:
                raise StoreError("native session identity is not established")
            if session["harness"] != ("codex" if codex else "claude"):
                raise StoreError("native request belongs to another provider")
            if codex:
                from .codex_protocol import request_key
                if (session["harness"] != "codex" or payload["params"]["threadId"] != session["native_id"]
                        or provider_request_id != request_key(payload["rpc_id"])):
                    raise StoreError("Codex native request belongs to another provider or thread")
                if session["cwd"] != note_cwd:
                    raise StoreError("native request session directory changed")
                if note:
                    question = note + "\n" + question
            context = {"session_id": sid, "generation": generation, "turn_id": turn,
                       "cwd": session["cwd"],
                       "native_id": session["native_id"], "provider_request_id": provider_request_id,
                       "request": json.loads(body)}
            canonical = json.dumps(context, sort_keys=True, separators=(",", ":"), allow_nan=False)
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            if c.execute("SELECT 1 FROM session_requests WHERE request_id=?", (request_id,)).fetchone():
                previous = self._read(c, request_id)
                if previous["digest"] != digest:
                    raise StoreError("provider request ID reused with different permission input")
                return previous
            c.execute("INSERT INTO session_requests VALUES(?,?,?,?,?,?,'pending',NULL,?,NULL)", (request_id, sid, turn, kind, question, digest, time.time()))
            c.execute("INSERT INTO session_native_requests(request_id,turn_id,generation,provider_request_id,payload,response_state) VALUES(?,?,?,?,?,'pending')", (request_id, turn, generation, provider_request_id, canonical))
            self.store._event(c, sid, "native-permission-opened", {"request_id": request_id, "digest": digest}, turn)
            return self._read(c, request_id)

    def decide(self, request_id, decision, *, expected_digest, reason="Operator decision"):
        from .session_store import process_live, text
        if decision not in {"allow", "deny"}:
            raise StoreError("native permission decision must be allow or deny")
        text(reason, "decision reason", 4096)
        with self.store.db.transaction(write=True) as c:
            request = self._read(c, request_id)
            if request["kind"] != "native-permission":
                raise StoreError("native question requires answers, not a permission decision")
            if request["digest"] != expected_digest:
                raise StoreError("native permission digest changed; inspect before deciding")
            if request["state"] == "answered":
                if request["answer"] != decision or request["decision_reason"] != reason:
                    raise StoreError("native permission already has a different decision")
                return request
            if request["state"] != "pending" or request["response_state"] != "pending":
                raise StoreError("native permission is no longer pending")
            session = self._active(c, request["session_id"], request["generation"], request["turn_id"], owner=False)
            if not process_live(session["owner_pid"], session["owner_identity"]):
                raise StoreError("native permission owner is gone; reconcile before deciding")
            c.execute("UPDATE session_requests SET state='answered',answer=?,resolved_at=? WHERE request_id=?", (decision, time.time(), request_id))
            c.execute("UPDATE session_native_requests SET response_state='queued',decided_by=?,decision_reason=? WHERE request_id=?", (f"operator:uid:{os.geteuid()}", reason, request_id))
            self.store._event(c, request["session_id"], "native-permission-decided", {"request_id": request_id, "decision": decision, "digest": expected_digest, "reason": reason}, request["turn_id"])
            return self._read(c, request_id)

    def answer_native(self, request_id, answers, *, expected_digest):
        from .codex_protocol import CodexProtocol
        from .session_store import process_live
        try:
            answer = json.dumps(answers, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise StoreError("invalid native answer JSON") from exc
        if len(answer.encode()) > 32768:
            raise StoreError("native answers exceed size limit")
        with self.store.db.transaction(write=True) as c:
            request = self._read(c, request_id)
            if request["kind"] != "native-clarification" or request["digest"] != expected_digest:
                raise StoreError("native question missing or changed")
            CodexProtocol._check_reply(request["payload"]["request"], answers)
            if request["state"] == "answered":
                if request["answer"] != answer:
                    raise StoreError("native question already has different answers")
                return request
            if request["state"] != "pending" or request["response_state"] != "pending":
                raise StoreError("native question is no longer pending")
            session = self._active(c, request["session_id"], request["generation"], request["turn_id"], owner=False)
            if not process_live(session["owner_pid"], session["owner_identity"]):
                raise StoreError("native question owner is gone; reconcile before answering")
            c.execute("UPDATE session_requests SET state='answered',answer=?,resolved_at=? WHERE request_id=?", (answer, time.time(), request_id))
            c.execute("UPDATE session_native_requests SET response_state='queued',decided_by=?,decision_reason='Operator answer' WHERE request_id=?", (f"operator:uid:{os.geteuid()}", request_id))
            self.store._event(c, request["session_id"], "native-question-answered", {"request_id": request_id, "digest": expected_digest}, request["turn_id"])
            return self._read(c, request_id)

    def claim_responses(self, sid, generation, turn):
        with self.store.db.transaction() as c:
            self._active(c, sid, generation, turn)
            if c.execute("SELECT 1 FROM session_native_requests WHERE turn_id=? AND response_state='queued' LIMIT 1", (turn,)).fetchone() is None:
                return []
        with self.store.db.transaction(write=True) as c:
            self._active(c, sid, generation, turn)
            ids = [r[0] for r in c.execute("SELECT request_id FROM session_native_requests WHERE turn_id=? AND response_state='queued' ORDER BY request_id LIMIT 16", (turn,))]
            responses = []
            for request_id in ids:
                request = self._read(c, request_id)
                if request["payload"]["request"].get("protocol") == "codex-app-server-v2":
                    from .codex_native import frame as codex_frame
                    frame = codex_frame(request)
                else:
                    decision = {"behavior": request["answer"]}
                    if request["answer"] == "allow":
                        decision["updatedInput"] = request["payload"]["request"]["input"]
                    else:
                        decision["message"] = request["decision_reason"]
                    frame = {"type": "control_response", "response": {"subtype": "success", "request_id": request["provider_request_id"], "response": decision}}
                c.execute("UPDATE session_native_requests SET response_state='submitting' WHERE request_id=?", (request_id,))
                self.store._event(c, sid, "native-response-reserved", {"request_id": request_id}, turn)
                responses.append({"request_id": request_id, "frame": frame})
            return responses

    def submitted(self, sid, generation, turn, request_id):
        with self.store.db.transaction(write=True) as c:
            # The complete frame is already written. Stop intent forbids new
            # approvals/submissions but cannot erase evidence of this write.
            self._active(c, sid, generation, turn, allow_stopping=True)
            request = self._read(c, request_id)
            if request["turn_id"] != turn or request["generation"] != generation:
                raise StoreError("native response belongs to another turn")
            if request["response_state"] not in {"submitting", "uncertain"}:
                raise StoreError("native response has no retained submission intent")
            c.execute("UPDATE session_native_requests SET response_state='submitted',submitted_at=? WHERE request_id=?", (time.time(), request_id))
            self.store._event(c, sid, "native-response-submitted", {"request_id": request_id}, turn)

    def cancel(self, sid, generation, turn, provider_request_id, *, response_not_submitted=False):
        with self.store.db.transaction(write=True) as c:
            self._active(c, sid, generation, turn, allow_stopping=True)
            row = c.execute("SELECT request_id FROM session_native_requests WHERE turn_id=? AND provider_request_id=?", (turn, provider_request_id)).fetchone()
            if row is None:
                raise StoreError("native cancellation references an unknown request")
            request_id = row[0]
            previous = self._read(c, request_id)["response_state"]
            next_state = {"pending": "cancelled", "queued": "cancelled",
                          "submitting": "cancelled" if response_not_submitted else "uncertain"}.get(previous, previous)
            c.execute("UPDATE session_native_requests SET response_state=? WHERE request_id=?", (next_state, request_id))
            c.execute("UPDATE session_requests SET state='cancelled',resolved_at=? WHERE request_id=? AND state='pending'", (time.time(), request_id))
            self.store._event(c, sid, "native-permission-cancelled", {"request_id": request_id,
                "previous_state": previous, "response_state": next_state}, turn)
