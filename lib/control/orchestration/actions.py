"""Effect-once operator action journal for Orchestration Core."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..context import read_published_snapshot
from ..jj import JjAdapter
from ..prepare import derive_repository_identity
from ..process import capture_bytes
from ..reconcile import LiveAdapters, reconcile_task
from ..store import StoreError, TaskStore
from ..transaction import CreationJournalStore, JournalError
from .doctor import run_orchestration_doctor
from .model import (
    ACTION_CONTRACT,
    ATTEMPT_NONTERMINAL_STATES,
    ATTEMPT_TERMINAL_STATES,
    EVENT_CONTRACT,
    FORBIDDEN_ACTION_CLASSES,
    ModelError,
    NODE_TERMINAL_STATES,
    canonical_uuid,
    new_uuid,
    record_digest,
    validate_action,
    validate_event,
    validate_initiative,
    validate_node,
)
from .store import InitiativeStore


ACTION_RECONCILIATION_CONTRACT = "asha.orchestration-action-reconciliation.v1"
SUPPORTED_ACTION_KINDS = frozenset({
    "activate-initiative", "dispatch-node", "pause", "resume",
    "stop-attempt", "cancel-node",
})
_ACTION_DOCUMENT_KEYS = frozenset({
    "contract", "action_id", "initiative_id", "actor_kind", "actor_id",
    "action_class", "payload", "payload_digest", "active_plan_digest",
    "expected_state_revision",
})
_MAX_ACTION_PAYLOAD_BYTES = 4096
_MAX_STOP_OUTPUT_BYTES = 64 * 1024


class ActionError(ValueError):
    """An action document or action phase is invalid."""


class ActionRefused(ActionError):
    """A valid action identity was denied without an uncertain side effect."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical(value: Any) -> bytes:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ActionError(f"action payload is not canonical JSON: {exc}") from exc
    if len(raw) > _MAX_ACTION_PAYLOAD_BYTES:
        raise ActionError(
            f"action payload exceeds {_MAX_ACTION_PAYLOAD_BYTES} bytes"
        )
    return raw


def payload_digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def build_action_document(
    initiative: Mapping[str, Any],
    action_class: str,
    payload: Mapping[str, Any],
    *,
    actor_id: str = "cli",
    action_id: str | None = None,
) -> dict[str, Any]:
    active = initiative.get("active_plan")
    if not isinstance(active, Mapping):
        raise ActionError("initiative has no active plan")
    body = copy.deepcopy(dict(payload))
    return {
        "contract": ACTION_CONTRACT,
        "action_id": action_id or new_uuid(),
        "initiative_id": initiative["initiative_id"],
        "actor_kind": "operator",
        "actor_id": actor_id,
        "action_class": action_class,
        "payload": body,
        "payload_digest": payload_digest(body),
        "active_plan_digest": active["digest"],
        "expected_state_revision": initiative["state_revision"],
    }


def _safe_outcome(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    if len(raw.encode("utf-8")) > 8192:
        raise ActionError("action outcome exceeds 8192 bytes")
    return raw


def action_outcome(action: Mapping[str, Any]) -> dict[str, Any]:
    raw = action.get("outcome")
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ActionError("retained action outcome is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ActionError("retained action outcome must be an object")
    return value


def set_action_state(
    store: InitiativeStore,
    action: dict[str, Any],
    state: str,
    outcome: Mapping[str, Any],
) -> dict[str, Any]:
    """Advance one journal phase with its bounded canonical stored outcome."""
    if action["state"] == state and action.get("outcome") == _safe_outcome(outcome):
        return action
    changed = copy.deepcopy(action)
    changed.update({
        "state": state,
        "outcome": _safe_outcome(outcome),
        "updated_at": _now(),
    })
    validate_action(changed)
    store.save_action(
        action["initiative_id"], changed, expected_digest=record_digest(action),
    )
    return changed


def append_event(
    store: InitiativeStore,
    initiative_id: str,
    event_type: str,
    subject_ids: list[str],
    payload: Mapping[str, Any],
    *,
    actor_kind: str,
    actor_id: str,
) -> dict[str, Any]:
    """Append one lock-protected canonical event against the current tail."""
    initiative = store.peek(initiative_id)
    body = copy.deepcopy(dict(payload))
    raw = json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    event = validate_event({
        "contract": EVENT_CONTRACT,
        "sequence": initiative["last_event_sequence"] + 1,
        "event_id": new_uuid(),
        "initiative_id": initiative_id,
        "type": event_type,
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "subject_ids": list(subject_ids),
        "payload_digest": hashlib.sha256(raw).hexdigest(),
        "payload": body,
        "recorded_at": _now(),
    })
    store.append_event(initiative_id, event)
    return event


def _validate_payload(kind: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ActionRefused("action payload must be an object")
    expected = {
        "activate-initiative": frozenset(),
        "pause": frozenset(),
        "resume": frozenset(),
        "dispatch-node": frozenset({"node_id"}),
        "stop-attempt": frozenset({"attempt_id"}),
        "cancel-node": frozenset({"node_id"}),
    }[kind]
    if set(payload) != expected:
        raise ActionRefused(
            f"{kind} payload requires exactly {sorted(expected)}"
        )
    if "node_id" in payload:
        from .model import validate_slug

        validate_slug(payload["node_id"], "action node_id")
    if "attempt_id" in payload:
        canonical_uuid(payload["attempt_id"], "action attempt_id")
    return copy.deepcopy(payload)


def _parse_document(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != _ACTION_DOCUMENT_KEYS:
        raise ActionError("action document is not the closed Core 2a schema")
    if value["contract"] != ACTION_CONTRACT:
        raise ActionError(f"action contract must be {ACTION_CONTRACT}")
    if not isinstance(value["payload"], dict):
        raise ActionError("action payload must be an object")
    actual_digest = payload_digest(value["payload"])
    if value["payload_digest"] != actual_digest:
        raise ActionError("action payload_digest does not match canonical payload bytes")
    at = _now()
    record = validate_action({
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key != "payload"
    } | {
        "state": "received",
        "outcome": _safe_outcome({"payload": value["payload"], "status": "received"}),
        "received_at": at,
        "updated_at": at,
    })
    return record, copy.deepcopy(value["payload"])


def _stored_matches_document(
    stored: Mapping[str, Any], requested: Mapping[str, Any]
) -> bool:
    return all(stored[field] == requested[field] for field in (
        "contract", "action_id", "initiative_id", "actor_kind", "actor_id",
        "action_class", "payload_digest", "active_plan_digest",
        "expected_state_revision",
    ))


def _refuse(
    store: InitiativeStore, action: dict[str, Any], reason: str
) -> dict[str, Any]:
    current = action
    if current["state"] == "dispatching":
        current = set_action_state(
            store, current, "indeterminate",
            {**action_outcome(current), "status": "indeterminate", "reason": reason},
        )
    if current["state"] not in {"received", "validated", "indeterminate"}:
        raise ActionRefused(reason)
    current = set_action_state(
        store, current, "refused",
        {**action_outcome(current), "status": "refused", "reason": reason},
    )
    append_event(
        store, current["initiative_id"], "action-refused", [current["action_id"]],
        {"action_class": current["action_class"], "reason": reason},
        actor_kind="controller", actor_id="action-broker",
    )
    return current


def _repository_identity_matches(initiative: Mapping[str, Any]) -> None:
    expected = initiative["scope"]["repository"]
    root = Path(expected["root"])
    jj = JjAdapter()
    facts = jj.preflight(root)
    if facts.root != root:
        raise ActionRefused("initiative repository canonical root changed")
    snapshot = read_published_snapshot(root)
    identity, _ = derive_repository_identity(
        snapshot.project_id, facts.root, facts.git_root,
    )
    digest = hashlib.sha256(json.dumps(
        [snapshot.project_id, str(facts.root), identity],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if (
        snapshot.project_id != expected["project_id"]
        or identity != expected["control_repository_id"]
        or digest != expected["initial_identity_digest"]
    ):
        raise ActionRefused("initiative repository identity digest changed")


def _activate(
    store: InitiativeStore, action: dict[str, Any]
) -> dict[str, Any]:
    from .scheduler import SchedulerError, refresh_readiness, validate_goal_capacity

    initiative_id = action["initiative_id"]
    initiative = store.peek(initiative_id)
    if initiative["state"] == "running":
        return {"status": "running", "already_running": True}
    if initiative["state"] != "approved":
        raise ActionRefused("only an approved initiative may activate")
    plan = store.read_plan(
        initiative_id, initiative["active_plan"]["revision"],
    )
    try:
        validate_goal_capacity(store.config, initiative, plan["nodes"])
    except SchedulerError as exc:
        raise ActionRefused(str(exc)) from exc
    doctor = run_orchestration_doctor(store.config)
    if doctor.get("ok") is not True:
        failed = [
            probe["name"] for probe in doctor.get("probes", [])
            if probe.get("outcome") != "match"
        ]
        raise ActionRefused(
            "runtime capability handshake failed: " + ", ".join(failed)
        )
    _repository_identity_matches(initiative)
    changed = copy.deepcopy(initiative)
    changed.update({
        "state": "running",
        "state_revision": initiative["state_revision"] + 1,
        "updated_at": _now(),
    })
    validate_initiative(changed)
    store.save_initiative(changed, expected_digest=record_digest(initiative))
    append_event(
        store, initiative_id, "initiative-state-changed", [initiative_id],
        {"from": "approved", "to": "running", "runtime_handshake": doctor["contract"]},
        actor_kind="controller", actor_id="action-broker",
    )
    refresh_readiness(store, initiative_id)
    return {"status": "running", "already_running": False}


def _pause(store: InitiativeStore, initiative_id: str) -> dict[str, Any]:
    initiative = store.peek(initiative_id)
    if initiative["state"] == "paused":
        return {"status": "paused", "already_paused": True}
    if initiative["state"] != "running":
        raise ActionRefused("only a running initiative may pause")
    changed = copy.deepcopy(initiative)
    changed.update({
        "state": "paused",
        "state_revision": initiative["state_revision"] + 1,
        "updated_at": _now(),
    })
    store.save_initiative(changed, expected_digest=record_digest(initiative))
    append_event(
        store, initiative_id, "initiative-state-changed", [initiative_id],
        {"from": "running", "to": "paused"},
        actor_kind="controller", actor_id="action-broker",
    )
    return {"status": "paused", "already_paused": False}


def _resume(
    store: InitiativeStore, initiative_id: str, action_id: str,
) -> dict[str, Any]:
    from .reconcile import reconcile_live
    from .scheduler import refresh_readiness

    initiative = store.peek(initiative_id)
    if initiative["state"] == "running":
        return {"status": "running", "already_running": True}
    if initiative["state"] != "paused":
        raise ActionRefused("only a paused initiative may resume")
    reconcile_actions(store, initiative_id, exclude_action_id=action_id)
    live = reconcile_live(store, initiative_id)
    if live["conflicts"]:
        raise ActionRefused("resume requires a clean live reconciliation")
    initiative = store.peek(initiative_id)
    if initiative["state"] != "paused":
        raise ActionRefused("live reconciliation changed the initiative state")
    changed = copy.deepcopy(initiative)
    changed.update({
        "state": "running",
        "state_revision": initiative["state_revision"] + 1,
        "updated_at": _now(),
    })
    store.save_initiative(changed, expected_digest=record_digest(initiative))
    append_event(
        store, initiative_id, "initiative-state-changed", [initiative_id],
        {"from": "paused", "to": "running"},
        actor_kind="controller", actor_id="action-broker",
    )
    refresh_readiness(store, initiative_id)
    return {"status": "running", "already_running": False}


def _asha_executable() -> Path:
    raw = os.environ.get("ASHA_ROOT")
    root = Path(raw).resolve() if raw else Path(__file__).resolve().parents[3]
    if raw and (not Path(raw).is_absolute() or root != Path(raw)):
        raise ActionError("ASHA_ROOT must be an exact canonical absolute path")
    return root / "bin" / "asha"


def _stop_task(store: InitiativeStore, task_id: str) -> dict[str, Any]:
    returncode, _stdout, stderr = capture_bytes(
        [str(_asha_executable()), "task", "stop", task_id],
        cwd=None,
        limit=_MAX_STOP_OUTPUT_BYTES,
        runner=None,
        error_type=ActionError,
        deadline_seconds=60,
    )
    if returncode != 0:
        detail = stderr[:4096].decode("utf-8", errors="replace").strip()
        raise ActionRefused(
            f"Control task stop refused ({returncode}): {detail or 'no diagnostic'}"
        )
    return {"status": "stop-requested", "control_task_id": task_id}


def _stop_attempt(
    store: InitiativeStore, initiative_id: str, attempt_id: str
) -> dict[str, Any]:
    attempt = store.read_attempt(initiative_id, attempt_id)
    if attempt["state"] == "allocated":
        raise ActionRefused("an allocated attempt has no Control process to stop")
    if attempt["state"] in ATTEMPT_TERMINAL_STATES:
        raise ActionRefused("a terminal attempt cannot be stopped")
    if attempt["state"] not in {
        "dispatching", "running", "reported", "awaiting-exit", "indeterminate",
    }:
        raise ActionRefused("attempt has already been observed exited")
    try:
        link = store.read_link(initiative_id, attempt_id)
    except StoreError as exc:
        raise ActionRefused("attempt has no proven Control task link") from exc
    if link["control_task_id"] != attempt["task_id"]:
        raise ActionRefused("attempt link task identity changed")
    try:
        task = TaskStore(store.config.control).peek(attempt["task_id"])
    except StoreError as exc:
        raise ActionRefused(f"Control task cannot be stopped: {exc}") from exc
    if (
        task["lifecycle"] in {"ended", "failed", "archived"}
        or task["runs"][0]["state"] in {"exited", "failed"}
    ):
        raise ActionRefused("attempt has already been observed exited")
    if task["lifecycle"] == "creating":
        raise ActionRefused(
            f"Control task creation is incomplete; run asha task recover {attempt['task_id']}"
        )
    return {**_stop_task(store, attempt["task_id"]), "attempt_id": attempt_id}


def _cancel_node(
    store: InitiativeStore, initiative_id: str, node_id: str, action_id: str
) -> dict[str, Any]:
    node = store.read_node(initiative_id, node_id)
    if node["state"] in NODE_TERMINAL_STATES:
        raise ActionRefused("a terminal node cannot be cancelled")
    attempts = sorted(
        (item for item in store.list_attempts_snapshot(initiative_id) if item["node_id"] == node_id),
        key=lambda item: (item["ordinal"], item["attempt_id"]),
    )
    active = [item for item in attempts if item["state"] in ATTEMPT_NONTERMINAL_STATES]
    if active:
        latest = active[-1]
        if latest["state"] == "allocated" and latest["action_id"] is None:
            bound_attempt = copy.deepcopy(latest)
            bound_attempt.update({"action_id": action_id, "updated_at": _now()})
            store.save_attempt(
                initiative_id, bound_attempt, expected_digest=record_digest(latest),
            )
            latest = bound_attempt
        if latest["state"] in {
            "dispatching", "running", "reported", "awaiting-exit", "indeterminate",
        }:
            try:
                link = store.read_link(initiative_id, latest["attempt_id"])
                task = TaskStore(store.config.control).peek(latest["task_id"])
            except StoreError:
                pass
            else:
                if (
                    link["control_task_id"] == latest["task_id"]
                    and task["lifecycle"] not in {"creating", "ended", "failed", "archived"}
                    and task["runs"][0]["state"] not in {"exited", "failed"}
                ):
                    _stop_task(store, latest["task_id"])
        changed_attempt = copy.deepcopy(latest)
        changed_attempt.update({"state": "cancelled", "updated_at": _now()})
        store.save_attempt(
            initiative_id, changed_attempt, expected_digest=record_digest(latest),
        )
    changed = copy.deepcopy(node)
    changed["state"] = "cancelled"
    validate_node(changed)
    store.save_node(initiative_id, changed, expected_digest=record_digest(node))
    append_event(
        store, initiative_id, "node-state-changed", [node_id],
        {"from": node["state"], "to": "cancelled"},
        actor_kind="controller", actor_id="action-broker",
    )
    return {"status": "cancelled", "already_cancelled": False, "node_id": node_id}


def _execute_local(
    store: InitiativeStore,
    action: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    kind = action["action_class"]
    if kind == "activate-initiative":
        return _activate(store, action)
    if kind == "pause":
        return _pause(store, action["initiative_id"])
    if kind == "resume":
        return _resume(store, action["initiative_id"], action["action_id"])
    if kind == "stop-attempt":
        return _stop_attempt(store, action["initiative_id"], payload["attempt_id"])
    if kind == "cancel-node":
        return _cancel_node(
            store, action["initiative_id"], payload["node_id"], action["action_id"],
        )
    raise ActionError(f"unsupported local action: {kind}")


def submit_action(
    store: InitiativeStore,
    initiative_id: str,
    action_document: Any,
) -> dict[str, Any]:
    """Validate, journal, and effect one exact operator action under the lock."""
    requested, payload = _parse_document(action_document)
    if requested["initiative_id"] != initiative_id:
        raise ActionError("action initiative identity differs from its route")
    with store.transaction_lock(initiative_id):
        try:
            stored = store.read_action(initiative_id, requested["action_id"])
        except StoreError as exc:
            if "not found" not in str(exc):
                raise
        else:
            if not _stored_matches_document(stored, requested):
                raise ActionRefused(
                    "action ID is already bound to a different payload or authority envelope"
                )
            return stored

        initiative = store.peek(initiative_id)
        store.save_action(initiative_id, requested)
        action = requested
        append_event(
            store, initiative_id, "action-received", [action["action_id"]],
            {"action_class": action["action_class"], "payload_digest": action["payload_digest"]},
            actor_kind=action["actor_kind"], actor_id=action["actor_id"],
        )

        # This fixed denial is deliberately before every approval lookup.  Core
        # 2a has no approval lookup for these unsupported classes at all.
        if action["action_class"] in initiative["forbidden_action_classes"]:
            return _refuse(store, action, "action class is forbidden in Core v1")
        if action["action_class"] not in SUPPORTED_ACTION_KINDS:
            return _refuse(store, action, "action class is unsupported in Core 2a")
        if initiative["active_plan"] is None:
            return _refuse(store, action, "initiative has no active approved plan")
        if action["active_plan_digest"] != initiative["active_plan"]["digest"]:
            return _refuse(store, action, "action active plan digest is stale")
        if action["expected_state_revision"] != initiative["state_revision"]:
            return _refuse(
                store, action,
                f"expected state revision {action['expected_state_revision']} does not match "
                f"current revision {initiative['state_revision']}",
            )
        try:
            payload = _validate_payload(action["action_class"], payload)
        except (ActionRefused, ModelError) as exc:
            return _refuse(store, action, str(exc))
        action = set_action_state(
            store, action, "validated",
            {"payload": payload, "status": "validated"},
        )
        if action["action_class"] == "dispatch-node":
            from .scheduler import SchedulerError, dispatch

            try:
                return dispatch(
                    store, store.config, initiative_id, payload["node_id"], action=action,
                )["action"]
            except SchedulerError as exc:
                return _refuse(store, action, str(exc))
        action = set_action_state(
            store, action, "dispatching",
            {"payload": payload, "status": "dispatching"},
        )
        try:
            result = _execute_local(store, action, payload)
            current = store.read_action(initiative_id, action["action_id"])
            return set_action_state(
                store, current, "completed",
                {**action_outcome(current), **result},
            )
        except ActionRefused as exc:
            return _refuse(store, store.read_action(initiative_id, action["action_id"]), str(exc))
        except (ActionError, StoreError, OSError, ValueError) as exc:
            # A bounded subprocess timeout or lost response cannot prove the
            # side effect, and a cross-record failure may have become visible.
            # Retain indeterminate until durable target evidence resolves it.
            current = store.read_action(initiative_id, action["action_id"])
            if current["state"] == "completed":
                return current
            if current["state"] == "dispatching":
                current = set_action_state(
                    store, current, "indeterminate",
                    {**action_outcome(current), "status": "indeterminate", "reason": str(exc)},
                )
                append_event(
                    store, initiative_id, "action-indeterminate", [current["action_id"]],
                    {"reason": str(exc)}, actor_kind="controller", actor_id="action-broker",
                )
            return current


def _attempt_to_running(
    store: InitiativeStore, initiative_id: str, attempt: dict[str, Any]
) -> dict[str, Any]:
    if attempt["state"] in {"dispatching", "indeterminate"}:
        changed = copy.deepcopy(attempt)
        changed.update({"state": "running", "updated_at": _now()})
        store.save_attempt(
            initiative_id, changed, expected_digest=record_digest(attempt),
        )
        return changed
    return attempt


def reconcile_actions(
    store: InitiativeStore, initiative_id: str, *, exclude_action_id: str | None = None,
) -> dict[str, Any]:
    """Resolve indeterminate actions only from durable Control/store evidence."""
    from .scheduler import dispatch, mark_launch_failed

    results: list[dict[str, Any]] = []
    with store.transaction_lock(initiative_id):
        for action in store.list_actions_snapshot(initiative_id):
            if action["action_id"] == exclude_action_id:
                continue
            if action["state"] not in {"dispatching", "indeterminate"}:
                continue
            if action["state"] == "dispatching":
                action = set_action_state(
                    store, action, "indeterminate",
                    {
                        **action_outcome(action),
                        "status": "indeterminate",
                        "reason": "dispatching action recovered after controller interruption",
                    },
                )
                append_event(
                    store, initiative_id, "action-indeterminate", [action["action_id"]],
                    {"reason": "controller interruption during dispatching phase"},
                    actor_kind="controller", actor_id="action-reconciler",
                )
            outcome = action_outcome(action)
            kind = action["action_class"]
            if kind == "dispatch-node":
                try:
                    attempt = store.read_attempt(initiative_id, outcome["attempt_id"])
                except StoreError as exc:
                    if "not found" not in str(exc):
                        results.append(action)
                        continue
                    action = set_action_state(
                        store, action, "refused",
                        {
                            **outcome,
                            "status": "launch-not-attempted",
                            "reason": "reserved attempt was not durably published",
                        },
                    )
                    append_event(
                        store, initiative_id, "action-refused", [action["action_id"]],
                        {
                            "action_class": action["action_class"],
                            "reason": "reserved attempt was not durably published",
                        },
                        actor_kind="controller", actor_id="action-reconciler",
                    )
                    results.append(action)
                    continue
                node = store.read_node(initiative_id, outcome["node_id"])
                try:
                    link = store.read_link(initiative_id, attempt["attempt_id"])
                except StoreError:
                    link = None
                if link is not None:
                    attempt = _attempt_to_running(store, initiative_id, attempt)
                    if node["state"] == "dispatching":
                        changed_node = copy.deepcopy(node)
                        changed_node["state"] = "running"
                        store.save_node(
                            initiative_id, changed_node,
                            expected_digest=record_digest(node),
                        )
                        append_event(
                            store, initiative_id, "node-state-changed",
                            [node["node_id"], attempt["attempt_id"]],
                            {"from": "dispatching", "to": "running"},
                            actor_kind="controller", actor_id="action-reconciler",
                        )
                    started = any(
                        event["type"] == "attempt-started"
                        and attempt["attempt_id"] in event["subject_ids"]
                        for event in store.list_events_snapshot(initiative_id)
                    )
                    if not started:
                        append_event(
                            store, initiative_id, "attempt-started",
                            [node["node_id"], attempt["attempt_id"], attempt["task_id"]],
                            {
                                "existing": True,
                                "control_task_record_digest": link[
                                    "control_task_record_digest"
                                ],
                                "reconciled": "retained-link",
                            },
                            actor_kind="controller", actor_id="action-reconciler",
                        )
                    action = set_action_state(
                        store, action, "completed",
                        {**outcome, "status": "running", "reconciled": "retained-link"},
                    )
                    results.append(action)
                    continue
                control_store = TaskStore(store.config.control)
                try:
                    task = control_store.peek(attempt["task_id"])
                except StoreError as exc:
                    if str(exc) != f"task not found: {attempt['task_id']}":
                        results.append(action)
                        continue
                    journals = CreationJournalStore(store.config.control)
                    try:
                        journals.read(attempt["task_id"])
                    except JournalError as exc:
                        if "not found" not in str(exc):
                            results.append(action)
                            continue
                        action = mark_launch_failed(
                            store, initiative_id, action, attempt, node,
                            "Control has no task or creation journal for the reserved ID",
                            reconciled=True,
                        )
                        results.append(action)
                    else:
                        remediation = f"asha task recover {attempt['task_id']}"
                        action = set_action_state(
                            store, action, "indeterminate",
                            {
                                **outcome,
                                "status": "indeterminate",
                                "reason": "Control creation journal requires recovery",
                                "remediation": remediation,
                            },
                        )
                        results.append(action)
                    continue
                if task["lifecycle"] == "creating":
                    remediation = f"asha task recover {attempt['task_id']}"
                    action = set_action_state(
                        store, action, "indeterminate",
                        {
                            **outcome,
                            "status": "indeterminate",
                            "reason": "Control task creation requires recovery",
                            "remediation": remediation,
                        },
                    )
                    results.append(action)
                    continue
                result = dispatch(
                    store, store.config, initiative_id, node["node_id"], action=action,
                )
                results.append(result["action"])
                continue
            if kind == "stop-attempt":
                attempt = store.read_attempt(initiative_id, outcome["payload"]["attempt_id"])
                try:
                    task = TaskStore(store.config.control).peek(attempt["task_id"])
                except StoreError:
                    results.append(action)
                    continue
                socket = task["tmux"]["socket"]
                from ..tmux import TmuxAdapter

                observed = reconcile_task(
                    task,
                    LiveAdapters(
                        config=store.config.control,
                        tmux=TmuxAdapter(socket=None if socket == "default" else socket),
                    ),
                )
                if observed["state"] in {"exited", "failed"}:
                    action = set_action_state(
                        store, action, "completed",
                        {**outcome, "status": "stopped", "control_state": observed["state"]},
                    )
                results.append(action)
                continue
            # Local effects are reconciled by their durable target state.
            initiative = store.peek(initiative_id)
            completed = (
                (kind == "activate-initiative" and initiative["state"] == "running")
                or (kind == "pause" and initiative["state"] == "paused")
                or (kind == "resume" and initiative["state"] == "running")
            )
            if kind == "cancel-node":
                completed = store.read_node(
                    initiative_id, outcome["payload"]["node_id"]
                )["state"] == "cancelled"
            if completed:
                action = set_action_state(
                    store, action, "completed", {**outcome, "status": "completed"},
                )
            results.append(action)
    return {
        "contract": ACTION_RECONCILIATION_CONTRACT,
        "initiative_id": initiative_id,
        "actions": results,
    }


__all__ = [
    "ACTION_RECONCILIATION_CONTRACT", "ActionError", "ActionRefused",
    "SUPPORTED_ACTION_KINDS", "action_outcome", "append_event",
    "build_action_document", "payload_digest", "reconcile_actions",
    "set_action_state", "submit_action",
]
