"""Recoverable, in-place cutover from legacy registries to SQLite.

The durable transition precedes permission changes. Imported records and the
backend marker publish in one transaction without replacing the database inode.
Rollback is deliberately limited to the unchanged post-activation snapshot.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

from .database import ControlDatabase
from .initiative_migration import coordinator_requires_stop
from .model import canonical_uuid
from .record_registry import RecordRegistry
from .registry_backend import BACKEND_CONTRACT, validate_backend
from .registry_guards import drop_guards, install_guards, migration_lock
from .registry_migration import STAGED_DOMAINS, _quiescent_connection
from .registry_snapshot import state_digest
from .registry_stage_validation import checked_stage
from .registry_tree import (ARTIFACT_ROOTS, apply_modes, capture_tree, fence_locks,
                            prepare_missing_roots, validate_entries, verify_tree)
from .rooms import _owned_state
from .session_store import process_live
from .stage_ledger import iter_ledger, write_ledger
from .store import StoreError
from .tmux import TmuxAdapter


_BACKEND = RecordRegistry("registry-backend", scope="control")
_TRANSITION_CONTRACT = "asha.registry-transition.v1"
_HISTORY_CONTRACT = "asha.registry-activation-history.v1"


def _raw(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _inject(callback, point):
    if callback is not None:
        callback(point)


def _artifact_digest(config):
    return hashlib.sha256(_raw(capture_tree(config, roots=ARTIFACT_ROOTS))).hexdigest()


def _ledger(activation_id):
    return RecordRegistry("registry-activation-ledger", scope=activation_id)


def _unchanged(c, expected):
    _quiescent_connection(c)
    if state_digest(c) != expected:
        raise StoreError("Control database changed; migration or rollback would lose new writes")


def _delete(c, key, digest):
    cursor = c.execute("DELETE FROM records WHERE domain=? AND scope=? AND record_key=? AND digest=?",
                       (_BACKEND.domain, _BACKEND.scope, key, digest))
    if cursor.rowcount != 1:
        raise StoreError("registry migration marker changed")


def _validate_transition(value, config):
    fields = {"contract", "activation_id", "source_root", "stage_home", "stage_digest", "source_state",
              "fence", "artifact_digest", "mode", "expected_state", "started_at"}
    if (not isinstance(value, dict) or set(value) != fields or value["contract"] != _TRANSITION_CONTRACT
            or value["source_root"] != str(config.asha_home) or value["mode"] not in {"activate", "rollback"}):
        raise StoreError("invalid registry transition record")
    canonical_uuid(value["activation_id"])
    if (not isinstance(value["stage_home"], str) or "\x00" in value["stage_home"]
            or not Path(value["stage_home"]).is_absolute()):
        raise StoreError("invalid registry transition stage root")
    for key in ("stage_digest", "artifact_digest"):
        if not isinstance(value[key], str) or re.fullmatch("[0-9a-f]{64}", value[key]) is None:
            raise StoreError("invalid registry transition digest")
    # Reuse the canonical marker timestamp/root checks.
    validate_backend({"contract": BACKEND_CONTRACT, "backend": "sqlite", "state": "active",
        "activation_id": value["activation_id"], "source_root": value["source_root"],
        "stage_digest": value["stage_digest"], "activated_at": value["started_at"]}, config)
    return value


def _transition(c, config):
    row = _BACKEND.read(c, "transition")
    if row is None:
        raise StoreError("no registry migration to recover")
    value = _validate_transition(row["value"], config)
    entries = list(iter_ledger(c, "source", value["fence"], registry=_ledger(value["activation_id"])))
    validate_entries(entries)
    if any(entry["kind"] == "missing" for entry in entries):
        raise StoreError("transition fence contains unprepared source roots")
    return row, entries


def _history(c, config, active):
    marker = validate_backend(active["value"], config)
    row = _BACKEND.read(c, "history-" + marker["activation_id"])
    if row is None:
        raise StoreError("active backend has no rollback history")
    value = row["value"]
    if (set(value) != {"contract", "state", "activation", "backend", "post_state"}
            or value["contract"] != _HISTORY_CONTRACT or value["state"] != "active"
            or value["backend"] != marker):
        raise StoreError("invalid registry activation history")
    transition = _validate_transition(value["activation"], config)
    if (transition["mode"] != "activate" or transition["activation_id"] != marker["activation_id"]
            or transition["stage_digest"] != marker["stage_digest"]):
        raise StoreError("activation history identity differs from backend")
    return row


def _save_outcome(c, config, state, activation_id):
    previous = _BACKEND.read(c, "latest-outcome")
    value = {"contract": "asha.registry-outcome.v1", "state": state,
             "activation_id": activation_id, "source_root": str(config.asha_home)}
    _BACKEND.put(c, "latest-outcome", _raw(value), state=state,
                 expected_digest=previous["digest"] if previous else None)
    return value


def _last_outcome(c, config):
    row = _BACKEND.read(c, "latest-outcome")
    if row is None:
        raise StoreError("no registry migration to recover")
    value = row["value"]
    if (set(value) != {"contract", "state", "activation_id", "source_root"}
            or value["contract"] != "asha.registry-outcome.v1"
            or value["state"] not in {"aborted", "rolled-back"}
            or value["source_root"] != str(config.asha_home)):
        raise StoreError("invalid registry recovery outcome")
    canonical_uuid(value["activation_id"])
    history = _BACKEND.read(c, "history-" + value["activation_id"])
    if (history is None or history["value"].get("contract") != _HISTORY_CONTRACT
            or history["value"].get("state") != value["state"]):
        raise StoreError("registry outcome differs from retained history")
    return value


def _external_records(records, tmux):
    for record in records:
        value = record["value"]
        if record["domain"] == "tasks":
            if any(process_live(run["pid"], run["process_start_identity"]) for run in value["runs"]):
                raise StoreError("stop live task runs before activating registries")
        elif record["domain"] == "initiative.coordinators":
            anchor = value["anchor"]
            if coordinator_requires_stop(value) and process_live(anchor.get("owner_pid", anchor.get("pane_pid")), anchor["process_start_identity"]):
                raise StoreError("stop live initiative coordinators before activating registries")
        elif record["domain"] == "rooms" and value["lifecycle"] == "open":
            state, detail = _owned_state(value, tmux)
            if state not in {"open", "ended", "missing"}:
                raise StoreError("Room ownership could not be revalidated: " + detail)


def _matches_stage(transition, entries, stage):
    if (transition["stage_digest"] != stage["digest"] or transition["stage_home"] != stage["home"]
            or transition["source_state"] != stage["manifest"]["source_database_state"]):
        raise StoreError("retained stage differs from the prepared activation")
    source = validate_entries(stage["source_tree"])
    prepared = validate_entries(entries)
    if set(source) != set(prepared):
        raise StoreError("prepared source tree differs from stage")
    for path, original in source.items():
        if original["kind"] == "missing":
            if prepared[path]["kind"] != "directory":
                raise StoreError("prepared source root is not a directory")
        elif prepared[path] != original:
            raise StoreError("prepared source facts differ from stage")


def _publish(config, db, stage, transition_row, entries, tmux, injector):
    transition = transition_row["value"]
    _matches_stage(transition, entries, stage)
    _external_records(stage["records"], tmux)
    apply_modes(config, entries, frozen=True, after_change=lambda path: _inject(injector, "mode:" + path))
    _inject(injector, "frozen")
    _external_records(stage["records"], tmux)
    if _artifact_digest(config) != transition["artifact_digest"]:
        raise StoreError("new artifacts appeared during activation")
    with db.transaction(write=True) as c:
        _unchanged(c, transition["source_state"])
        current, _ = _transition(c, config)
        if current["digest"] != transition_row["digest"] or _BACKEND.read(c, "active") is not None:
            raise StoreError("registry activation authority changed")
        drop_guards(c)
        for domain in STAGED_DOMAINS:
            if c.execute("SELECT 1 FROM records WHERE domain=? LIMIT 1", (domain,)).fetchone():
                raise StoreError("operational SQLite registry already contains records")
        for record in stage["records"]:
            RecordRegistry(record["domain"], scope=record["scope"]).put(c, record["key"], record["raw"],
                state=record["state"], updated_at=record["updated_at"])
        _inject(injector, "imported")
        marker = validate_backend({"contract": BACKEND_CONTRACT, "backend": "sqlite", "state": "active",
            "activation_id": transition["activation_id"], "source_root": str(config.asha_home),
            "stage_digest": stage["digest"], "activated_at": _now()}, config)
        _BACKEND.put(c, "active", _raw(marker), state="active")
        history = {"contract": _HISTORY_CONTRACT, "state": "active", "activation": transition,
                   "backend": marker, "post_state": state_digest(c)}
        _BACKEND.put(c, "history-" + transition["activation_id"], _raw(history), state="active")
        _delete(c, "transition", transition_row["digest"])
        install_guards(c, STAGED_DOMAINS)
    _inject(injector, "committed")
    return marker


def activate_registries(config, stage_home, *, tmux=None, failure_injector=None):
    """Activate an authenticated offline stage; a failed preparation is recoverable."""
    tmux = tmux if tmux is not None else TmuxAdapter()
    with migration_lock(config, exclusive=True), checked_stage(config, stage_home) as stage, ControlDatabase(config) as db:
        with db.transaction() as c:
            if _BACKEND.read(c, "transition") is not None:
                raise StoreError("registry migration is incomplete; recover it before activation")
            active = _BACKEND.read(c, "active")
            if active is not None:
                marker = validate_backend(active["value"], config)
                if marker["stage_digest"] != stage["digest"]:
                    raise StoreError("another registry stage is already active")
                return marker
            _unchanged(c, stage["manifest"]["source_database_state"])
        _external_records(stage["records"], tmux)
        with fence_locks(config, stage["source_tree"]) as acquire:
            entries = prepare_missing_roots(config, stage["source_tree"])
            acquire(entries)
            activation_id = str(uuid4())
            with db.transaction(write=True) as c:
                _unchanged(c, stage["manifest"]["source_database_state"])
                if _BACKEND.read(c, "active") is not None or _BACKEND.read(c, "transition") is not None:
                    raise StoreError("registry authority changed during preparation")
                transition = {"contract": _TRANSITION_CONTRACT, "activation_id": activation_id,
                    "source_root": str(config.asha_home), "stage_home": stage["home"], "stage_digest": stage["digest"],
                    "source_state": stage["manifest"]["source_database_state"],
                    "expected_state": stage["manifest"]["source_database_state"],
                    "fence": write_ledger(c, "source", entries, registry=_ledger(activation_id)),
                    "artifact_digest": _artifact_digest(config), "mode": "activate", "started_at": _now()}
                _BACKEND.put(c, "transition", _raw(transition), state="preparing")
                install_guards(c, STAGED_DOMAINS)
                row = _BACKEND.read(c, "transition")
            _inject(failure_injector, "prepared")
            return _publish(config, db, stage, row, entries, tmux, failure_injector)


def _finish_rollback(config, db, row, entries, injector, *, abort=False):
    transition = row["value"]
    with db.transaction() as c:
        active = _BACKEND.read(c, "active")
        if active is None:
            raise StoreError("rollback has no active backend")
        history_row = _history(c, config, active)
        expected = {**history_row["value"]["activation"], "mode": "rollback",
                    "expected_state": history_row["value"]["post_state"]}
        if transition != expected:
            raise StoreError("rollback transition differs from activation history")
        _unchanged(c, transition["expected_state"])
    if _artifact_digest(config) != transition["artifact_digest"]:
        raise StoreError("artifact tree changed; rollback would discard new work")
    apply_modes(config, entries, frozen=abort, after_change=lambda path: _inject(injector, "mode:" + path))
    _inject(injector, "frozen" if abort else "thawed")
    with db.transaction(write=True) as c:
        _unchanged(c, transition["expected_state"])
        if _artifact_digest(config) != transition["artifact_digest"]:
            raise StoreError("artifact tree changed during rollback")
        if abort:
            _delete(c, "transition", row["digest"])
            return active["value"]
        drop_guards(c)
        for domain in STAGED_DOMAINS:
            c.execute("DELETE FROM records WHERE domain=?", (domain,))
        _delete(c, "active", active["digest"])
        _delete(c, "transition", row["digest"])
        history = {**history_row["value"], "state": "rolled-back"}
        _BACKEND.put(c, "history-" + transition["activation_id"], _raw(history),
                     expected_digest=history_row["digest"], state="rolled-back")
        outcome = _save_outcome(c, config, "rolled-back", transition["activation_id"])
        install_guards(c, STAGED_DOMAINS)
    _inject(injector, "committed")
    return outcome


def recover_activation(config, *, action="resume", tmux=None, failure_injector=None):
    """Resume or abort an exact durable transition, including a partial freeze."""
    if action not in {"resume", "abort"}:
        raise StoreError("registry recovery action must be resume or abort")
    tmux = tmux if tmux is not None else TmuxAdapter()
    with migration_lock(config, exclusive=True), ControlDatabase(config) as db:
        with db.transaction() as c:
            if _BACKEND.read(c, "transition") is None:
                active = _BACKEND.read(c, "active")
                if active is not None:
                    return validate_backend(active["value"], config)
                return _last_outcome(c, config)
            row, entries = _transition(c, config)
            transition = row["value"]
            _unchanged(c, transition["expected_state"])
        with fence_locks(config, entries):
            verify_tree(config, entries, transitional=True,
                        allow_extra=transition["mode"] == "activate" and action == "abort")
            if transition["mode"] == "rollback":
                return _finish_rollback(config, db, row, entries, failure_injector, abort=action == "abort")
            if action == "abort":
                apply_modes(config, entries, frozen=False, allow_extra=True,
                            after_change=lambda path: _inject(failure_injector, "mode:" + path))
                with db.transaction(write=True) as c:
                    _unchanged(c, transition["source_state"])
                    if _BACKEND.read(c, "active") is not None:
                        raise StoreError("cannot abort an already published activation")
                    _delete(c, "transition", row["digest"])
                    _BACKEND.put(c, "history-" + transition["activation_id"], _raw({"contract": _HISTORY_CONTRACT,
                        "state": "aborted", "activation": transition}), state="aborted")
                    outcome = _save_outcome(c, config, "aborted", transition["activation_id"])
                _inject(failure_injector, "committed")
                return outcome
            with checked_stage(config, transition["stage_home"]) as stage:
                return _publish(config, db, stage, row, entries, tmux, failure_injector)


def rollback_registries(config, *, failure_injector=None):
    """Restore file authority only if no database or artifact work followed cutover."""
    with migration_lock(config, exclusive=True), ControlDatabase(config) as db:
        with db.transaction() as c:
            if _BACKEND.read(c, "transition") is not None:
                raise StoreError("registry migration is incomplete; recover it before rollback")
            active = _BACKEND.read(c, "active")
            if active is None:
                raise StoreError("SQLite registries are not active")
            history = _history(c, config, active)["value"]
            transition = {**history["activation"], "mode": "rollback", "expected_state": history["post_state"]}
            _unchanged(c, transition["expected_state"])
            entries = list(iter_ledger(c, "source", transition["fence"], registry=_ledger(transition["activation_id"])))
        if _artifact_digest(config) != transition["artifact_digest"]:
            raise StoreError("artifact tree changed; rollback would discard new work")
        with fence_locks(config, entries):
            verify_tree(config, entries, frozen=True)
            with db.transaction(write=True) as c:
                _unchanged(c, transition["expected_state"])
                _BACKEND.put(c, "transition", _raw(transition), state="rolling-back")
                row = _BACKEND.read(c, "transition")
            _inject(failure_injector, "prepared")
            return _finish_rollback(config, db, row, entries, failure_injector)
