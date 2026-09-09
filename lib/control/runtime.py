"""Durable operator admission policy, independent of the scheduler process."""
from __future__ import annotations

from .database import ControlDatabase, DATABASE_NAME
from .store import StoreError


MODES = frozenset({"running", "paused", "draining", "stopped"})
EXPLANATIONS = {
    "running": "New work may start; closing Control leaves background work running.",
    "paused": "New work is paused; admitted work may finish and session owners remain available.",
    "draining": "New work is paused; managed owners exit after their current turn finishes.",
    "stopped": "New work is stopped; managed turns are cancelled. Existing headless workers require their task stop action.",
}


def read_policy(c):
    row = c.execute("SELECT mode,revision,reason FROM control_runtime WHERE singleton=1").fetchone()
    if row is None or row["mode"] not in MODES:
        raise StoreError("runtime admission policy is missing or invalid")
    return dict(row)


def admission(config):
    """An absent DB retains legacy admission; damaged or outdated state refuses it."""
    if not (config.tasks_dir.parent / DATABASE_NAME).exists():
        return {"mode": "running", "revision": 0, "reason": "", "initialized": False,
                "message": EXPLANATIONS["running"]}
    with ControlDatabase(config) as db:
        with db.transaction() as c:
            row = read_policy(c)
            return {**row, "initialized": True, "message": EXPLANATIONS[row["mode"]]}


def connection_admission(db):
    """Owners reuse their validated connection; polling must not reopen SQLite."""
    with db.transaction() as c:
        return read_policy(c)


def require_admission(config):
    policy = admission(config)
    if policy["mode"] != "running":
        raise StoreError("runtime admission is " + policy["mode"] + ": " + policy["message"])


def set_admission(config, mode, *, reason="operator request", expected_revision=None):
    if mode not in MODES or not isinstance(reason, str) or len(reason.encode()) > 4096:
        raise StoreError("invalid runtime admission policy")
    with ControlDatabase(config, create=True) as db:
        with db.transaction(write=True) as c:
            current = c.execute("SELECT * FROM control_runtime WHERE singleton=1").fetchone()
            if current is None:
                raise StoreError("runtime admission policy is missing")
            if expected_revision is not None and current["revision"] != expected_revision:
                raise StoreError("runtime admission policy changed; reload before updating")
            if current["mode"] != mode:
                c.execute("UPDATE control_runtime SET mode=?,revision=revision+1,reason=? WHERE singleton=1", (mode, reason))
            if mode == "stopped" and c.execute("SELECT 1 FROM sqlite_master WHERE name='managed_sessions'").fetchone():
                # Keep stop intent even if an operator resumes admission before
                # an owner next polls. Resume must not erase a turn cancellation.
                c.execute("UPDATE managed_sessions SET stop_requested=1 WHERE state!='stopped'")
    if mode == "stopped":
        from .session_store import SessionStore, SessionsUninitialized
        try:
            store = SessionStore(config)
        except SessionsUninitialized:
            store = None
        if store is not None:
            with store:
                with store.db.transaction() as c:
                    pending = [r[0] for r in c.execute("SELECT session_id FROM managed_sessions WHERE stop_requested=1 AND state!='stopped'")]
                for sid in pending:
                    try:
                        store.stop(sid)
                    except StoreError as exc:
                        if "provider cleanup is still pending" not in str(exc):
                            raise
    return admission(config)
