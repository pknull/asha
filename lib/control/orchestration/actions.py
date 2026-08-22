"""Effect-once operator action journal for Orchestration Core."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
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
    APPROVAL_CONTRACT,
    ATTEMPT_CONTRACT,
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
    validate_approval,
    validate_attempt,
    validate_event,
    validate_initiative,
    validate_node,
)
from .store import InitiativeStore


ACTION_RECONCILIATION_CONTRACT = "asha.orchestration-action-reconciliation.v1"
SUPPORTED_ACTION_KINDS = frozenset({
    "activate-initiative", "dispatch-node", "pause", "resume",
    "stop-attempt", "cancel-node", "repair-node", "request-salvage",
    "decide", "continue-node", "finalize", "archive", "unarchive",
})
_EXECUTION_AUTHORITY_ACTIONS = frozenset({
    "activate-initiative", "dispatch-node", "resume", "repair-node",
    "continue-node",
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


def _process_start_ticks(pid: int) -> int:
    """Return the Linux process start instance used to defeat PID reuse."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        fields = raw[raw.rfind(")") + 2:].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError) as exc:
        raise ActionError("controller process identity is unavailable") from exc


def _verification_controller_is_live(outcome: Mapping[str, Any]) -> bool:
    pid = outcome.get("controller_pid")
    ticks = outcome.get("controller_start_ticks")
    if (
        isinstance(pid, bool) or not isinstance(pid, int)
        or isinstance(ticks, bool) or not isinstance(ticks, int)
        or pid <= 0 or ticks < 0
    ):
        return False
    try:
        if Path(f"/proc/{pid}").stat().st_uid != os.geteuid():
            return False
        return _process_start_ticks(pid) == ticks
    except (ActionError, OSError):
        return False


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
        "repair-node": frozenset({"node_id", "seal_id"}),
        "request-salvage": frozenset({"node_id", "failure_seal_id", "plan"}),
        "decide": frozenset({"paused_seal_id", "decision"}),
        "continue-node": frozenset({"node_id", "paused_seal_id", "decision_action_id"}),
        "finalize": frozenset({"outcome", "reason"}),
        "archive": frozenset(),
        "unarchive": frozenset(),
    }[kind]
    if kind == "dispatch-node" and set(payload) == {"node_id", "salvage_request_id"}:
        expected = frozenset({"node_id", "salvage_request_id"})
    if set(payload) != expected:
        raise ActionRefused(
            f"{kind} payload requires exactly {sorted(expected)}"
        )
    if "node_id" in payload:
        from .model import validate_slug

        validate_slug(payload["node_id"], "action node_id")
    if "attempt_id" in payload:
        canonical_uuid(payload["attempt_id"], "action attempt_id")
    for field in (
        "seal_id", "failure_seal_id", "paused_seal_id", "decision_action_id",
        "salvage_request_id",
    ):
        if field in payload:
            canonical_uuid(payload[field], f"action {field}")
    for field, maximum in (("plan", 4096), ("decision", 4096)):
        if field in payload:
            value = payload[field]
            if (
                not isinstance(value, str) or not value
                or len(value.encode("utf-8")) > maximum
                or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)
            ):
                raise ActionRefused(f"action {field} must be bounded printable text")
    if "reason" in payload:
        value = payload["reason"]
        if (
            not isinstance(value, str) or not value
            or len(value.encode("utf-8")) > 4096
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)
        ):
            raise ActionRefused("action reason must be bounded printable text")
    if "outcome" in payload and payload["outcome"] not in {"partial", "failed"}:
        raise ActionRefused("finalize outcome must be partial or failed")
    return copy.deepcopy(payload)


def _parse_document(value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != _ACTION_DOCUMENT_KEYS:
        raise ActionError("action document is not the closed Core 2b schema")
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
    try:
        from .readiness import ReadinessError, bind_readiness

        bind_readiness(store, initiative_id)
    except ReadinessError:
        pass
    return {"status": store.peek(initiative_id)["state"], "already_running": False}


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
    try:
        from .readiness import ReadinessError, bind_readiness

        bind_readiness(store, initiative_id)
    except ReadinessError:
        pass
    return {"status": store.peek(initiative_id)["state"], "already_running": False}


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
    result = _stop_task(store, attempt["task_id"])
    current = store.read_attempt(initiative_id, attempt_id)
    if current["state"] not in ATTEMPT_TERMINAL_STATES:
        cancelled = copy.deepcopy(current)
        cancelled.update({"state": "cancelled", "updated_at": _now()})
        store.save_attempt(
            initiative_id, cancelled, expected_digest=record_digest(current),
        )
    return {**result, "attempt_id": attempt_id, "status": "cancelled"}


def _cancel_node(
    store: InitiativeStore, initiative_id: str, node_id: str, action_id: str
) -> dict[str, Any]:
    node = store.read_node(initiative_id, node_id)
    if node["state"] in NODE_TERMINAL_STATES:
        raise ActionRefused("a terminal node cannot be cancelled")
    owning_verifications = [
        item for item in store.list_verifications_snapshot(initiative_id)
        if item["node_id"] == node_id
        and (
            item["state"] in {"pending", "dispatching", "running"}
            or (node["state"] == "evaluating" and item["state"] != "stale")
        )
    ]
    if owning_verifications:
        raise ActionRefused(
            "an active verification must finish before the node can be cancelled"
        )
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


def _active_plan(store: InitiativeStore, initiative: Mapping[str, Any]) -> dict[str, Any]:
    active = initiative.get("active_plan")
    if not isinstance(active, Mapping):
        raise ActionRefused("initiative has no active plan")
    plan = store.read_plan(initiative["initiative_id"], active["revision"])
    if plan["digest"] != active["digest"]:
        raise ActionRefused("active plan digest changed")
    return plan


def _reserve_attempt(
    store: InitiativeStore,
    initiative: dict[str, Any],
    plan: dict[str, Any],
    node: dict[str, Any],
    action_id: str,
    base: dict[str, Any],
) -> dict[str, Any]:
    attempts = store.list_attempts_snapshot(initiative["initiative_id"])
    node_attempts = [item for item in attempts if item["node_id"] == node["node_id"]]
    attempt_cap = min(
        initiative["limits"]["max_attempts_per_node"],
        plan["limits"]["max_attempts_per_node"],
    )
    total_cap = min(
        initiative["limits"]["max_total_tasks"],
        plan["limits"]["max_total_tasks"],
    )
    if len(node_attempts) >= attempt_cap:
        raise ActionRefused("node max_attempts_per_node exhausted")
    if len(attempts) >= total_cap:
        raise ActionRefused("initiative max_total_tasks exhausted")
    if any(item["state"] == "allocated" for item in node_attempts):
        raise ActionRefused("node already has an allocated attempt")
    at = _now()
    attempt = validate_attempt({
        "contract": ATTEMPT_CONTRACT,
        "attempt_id": str(uuid.uuid4()),
        "initiative_id": initiative["initiative_id"],
        "node_id": node["node_id"],
        "task_id": str(uuid.uuid4()),
        "action_id": action_id,
        "ordinal": max((item["ordinal"] for item in node_attempts), default=0) + 1,
        "base": copy.deepcopy(base),
        "state": "allocated",
        "result_publication_id": None,
        "result_id": None,
        "seal_id": None,
        "created_at": at,
        "updated_at": at,
    })
    store.save_attempt(initiative["initiative_id"], attempt)
    return attempt


def _invalidate_candidate_records(
    store: InitiativeStore, initiative_id: str, seal_id: str,
) -> dict[str, list[str]]:
    stale_reviews: list[str] = []
    for review in store.list_reviews_snapshot(initiative_id):
        if review["target"]["seal_id"] != seal_id or review["state"] == "stale":
            continue
        stale_reviews.append(review["review_id"])
        review_node = store.read_node(initiative_id, review["node_id"])
        if review_node["state"] in {"succeeded", "needs-input"}:
            reopened = copy.deepcopy(review_node)
            reopened["state"] = "ready"
            validate_node(reopened)
            store.save_node(
                initiative_id, reopened,
                expected_digest=record_digest(review_node),
            )
            review_node = reopened
        if review_node["state"] == "ready" and not any(
            item["type"] == "node-ready"
            and review["review_id"] in item["subject_ids"]
            and item["payload"].get("reason") == "candidate-review-staled"
            for item in store.list_events_snapshot(initiative_id)
        ):
            append_event(
                store, initiative_id, "node-ready",
                [review_node["node_id"], review["review_id"], seal_id],
                {
                    "from": (
                        "succeeded" if review["state"] == "accepted-pass"
                        else "needs-input"
                    ),
                    "to": "ready",
                    "reason": "candidate-review-staled",
                },
                actor_kind="controller", actor_id="repair-invalidation",
            )
        changed = copy.deepcopy(review)
        changed.update({
            "state": "stale", "verdict": None, "findings": [], "updated_at": _now(),
        })
        store.save_review(
            initiative_id, changed, expected_digest=record_digest(review),
        )
    stale_verifications: list[str] = []
    for verification in store.list_verifications_snapshot(initiative_id):
        if (
            verification["seal_id"] != seal_id
            or verification["state"] == "stale"
        ):
            continue
        stale_verifications.append(verification["verification_id"])
        verification_node = store.read_node(
            initiative_id, verification["node_id"],
        )
        if verification_node["state"] == "succeeded":
            reopened = copy.deepcopy(verification_node)
            reopened["state"] = "ready"
            validate_node(reopened)
            store.save_node(
                initiative_id, reopened,
                expected_digest=record_digest(verification_node),
            )
            verification_node = reopened
        if verification_node["state"] == "ready" and not any(
            item["type"] == "node-ready"
            and verification["verification_id"] in item["subject_ids"]
            and item["payload"].get("reason") == "candidate-verification-staled"
            for item in store.list_events_snapshot(initiative_id)
        ):
            append_event(
                store, initiative_id, "node-ready",
                [verification_node["node_id"], verification["verification_id"], seal_id],
                {
                    "from": "succeeded", "to": "ready",
                    "reason": "candidate-verification-staled",
                },
                actor_kind="controller", actor_id="repair-invalidation",
            )
        changed = copy.deepcopy(verification)
        changed.update({"state": "stale", "outcome": None, "updated_at": _now()})
        store.save_verification(
            initiative_id, changed, expected_digest=record_digest(verification),
        )
    return {"reviews": stale_reviews, "verifications": stale_verifications}


def _repair_lineage_roots(
    store: InitiativeStore, initiative_id: str,
) -> dict[str, str]:
    """Map repair roots and inherited legacy retries to their root attempt."""
    attempts = store.list_attempts_snapshot(initiative_id)
    by_id = {item["attempt_id"]: item for item in attempts}
    root_ids: set[str] = set()
    for action in store.list_actions_snapshot(initiative_id):
        if action["action_class"] != "repair-node" or action["state"] != "completed":
            continue
        attempt_id = action_outcome(action).get("attempt_id")
        if isinstance(attempt_id, str) and attempt_id in by_id:
            root_ids.add(attempt_id)
    lineage_roots = {item: item for item in root_ids}
    events = store.list_events_snapshot(initiative_id)
    changed = True
    while changed:
        changed = False
        for event in events:
            parent = event["payload"].get("retry_of")
            if event["type"] != "node-ready" or parent not in lineage_roots:
                continue
            children = set(event["subject_ids"]) & by_id.keys()
            for child in children - lineage_roots.keys():
                lineage_roots[child] = lineage_roots[parent]
                changed = True
    root_bindings: dict[tuple[str, str], list[str]] = {}
    for root_id in root_ids:
        item = by_id[root_id]
        binding = (
            item["node_id"],
            json.dumps(item["base"], sort_keys=True, separators=(",", ":")),
        )
        root_bindings.setdefault(binding, []).append(root_id)
    for item in attempts:
        if item["attempt_id"] in lineage_roots or item["action_id"] is not None:
            continue
        binding = (
            item["node_id"],
            json.dumps(item["base"], sort_keys=True, separators=(",", ":")),
        )
        candidates = [
            root_id for root_id in root_bindings.get(binding, [])
            if by_id[root_id]["ordinal"] < item["ordinal"]
            and by_id[root_id]["created_at"] <= item["created_at"]
        ]
        if candidates:
            latest_ordinal = max(by_id[root_id]["ordinal"] for root_id in candidates)
            latest = [
                root_id for root_id in candidates
                if by_id[root_id]["ordinal"] == latest_ordinal
            ]
            if len(latest) == 1:
                lineage_roots[item["attempt_id"]] = latest[0]
    return lineage_roots


def _repair_lineage_attempts(
    store: InitiativeStore, initiative_id: str,
) -> list[dict[str, Any]]:
    """Return repair roots and inherited legacy retries, including null action IDs."""
    lineage = _repair_lineage_roots(store, initiative_id)
    return [
        item for item in store.list_attempts_snapshot(initiative_id)
        if item["attempt_id"] in lineage
    ]


def _repair_node(
    store: InitiativeStore,
    action: dict[str, Any],
    node_id: str,
    seal_id: str,
) -> dict[str, Any]:
    initiative_id = action["initiative_id"]
    initiative = store.peek(initiative_id)
    plan = _active_plan(store, initiative)
    seal = store.read_seal(initiative_id, seal_id)
    if seal["outcome"] != "success":
        raise ActionRefused("repair-node requires the exact prior success seal")
    target = store.read_node(initiative_id, node_id)
    if target["repository_id"] != seal["repository_id"]:
        raise ActionRefused("repair target repository differs from the candidate seal")
    retained = [
        item for item in store.list_attempts_snapshot(initiative_id)
        if item["action_id"] == action["action_id"]
    ]
    if len(retained) > 1:
        raise ActionError("repair action owns multiple retained attempts")
    if target["state"] != "ready" and not (
        retained and target["state"] in {"dispatching", "running", "evaluating"}
    ):
        raise ActionRefused("repair target node must be deterministically ready")
    base = {
        "policy": "upstream-seal",
        "scope_origin": copy.deepcopy(seal["scope_origin"]),
        "upstream_node_ids": [seal["node_id"]] if seal["node_id"] != node_id else [],
        "seal_inputs": [{
            "seal_id": seal_id,
            "outcome": "success",
            "read_only": False,
            "scope_origin": copy.deepcopy(seal["scope_origin"]),
        }],
    }
    if retained:
        attempt = retained[0]
        if attempt["node_id"] != node_id or attempt["base"] != base:
            raise ActionRefused("retained repair reservation binding changed")
    else:
        cycles = len(_repair_lineage_attempts(store, initiative_id))
        cap = min(
            initiative["limits"]["max_repair_cycles"],
            plan["limits"]["max_repair_cycles"],
        )
        if cycles >= cap:
            raise ActionRefused("initiative max_repair_cycles exhausted")
        attempt = _reserve_attempt(
            store, initiative, plan, target, action["action_id"], base,
        )
    return {
        "status": "repair-allocated",
        "node_id": node_id,
        "attempt_id": attempt["attempt_id"],
        "control_task_id": attempt["task_id"],
        "candidate_seal_id": seal_id,
        "base_commit_id": seal["jj_commit_id"],
        "invalidated": {"reviews": [], "verifications": []},
    }


def _salvage_binding(
    action: Mapping[str, Any],
    node: Mapping[str, Any],
    seal: Mapping[str, Any],
    plan_text: str,
) -> dict[str, Any]:
    return {
        "node_id": node["node_id"],
        "failure_seal_id": seal["seal_id"],
        "scope_origin": copy.deepcopy(seal["scope_origin"]),
        "hard_write_scope": copy.deepcopy(node["hard_write_scope"]),
        "active_plan_digest": action["active_plan_digest"],
        "plan_digest": hashlib.sha256(plan_text.encode("utf-8")).hexdigest(),
        "request_action_payload_digest": action["payload_digest"],
    }


def _request_salvage(
    store: InitiativeStore,
    action: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    initiative_id = action["initiative_id"]
    node = store.read_node(initiative_id, payload["node_id"])
    seal = store.read_seal(initiative_id, payload["failure_seal_id"])
    if seal["outcome"] != "failure":
        raise ActionRefused("request-salvage requires an immutable failure seal")
    if seal["repository_id"] != node["repository_id"]:
        raise ActionRefused("salvage node repository differs from the failure seal")
    binding = _salvage_binding(action, node, seal, payload["plan"])
    binding_digest = hashlib.sha256(_canonical(binding)).hexdigest()
    retained = [
        item for item in store.list_approvals_snapshot(initiative_id)
        if item["action_class"] == "salvage"
        and item["binding_digest"] == binding_digest
        and item["actor_id"] == action["actor_id"]
        and item["active_plan_digest"] == action["active_plan_digest"]
        and item["expected_state_revision"] == action["expected_state_revision"]
    ]
    if len(retained) > 1:
        raise ActionError("salvage action has multiple retained approval requests")
    if retained:
        approval = retained[0]
        if not any(
            event["type"] == "approval-requested"
            and approval["request_id"] in event["subject_ids"]
            for event in store.list_events_snapshot(initiative_id)
        ):
            append_event(
                store, initiative_id, "approval-requested",
                [approval["request_id"], seal["seal_id"], node["node_id"]],
                {"action_class": "salvage", "binding_digest": approval["binding_digest"]},
                actor_kind="operator", actor_id=action["actor_id"],
            )
        return {
            "status": "salvage-requested",
            "request_id": approval["request_id"],
            "salvage_binding": binding,
        }
    at = _now()
    approval = validate_approval({
        "contract": APPROVAL_CONTRACT,
        "request_id": new_uuid(),
        "initiative_id": initiative_id,
        "action_class": "salvage",
        "binding_digest": binding_digest,
        "active_plan_digest": action["active_plan_digest"],
        "expected_state_revision": action["expected_state_revision"],
        "actor_kind": "operator",
        "actor_id": action["actor_id"],
        "state": "requested",
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "rationale": payload["plan"],
        "created_at": at,
        "updated_at": at,
    })
    store.save_approval(initiative_id, approval)
    append_event(
        store, initiative_id, "approval-requested",
        [approval["request_id"], seal["seal_id"], node["node_id"]],
        {"action_class": "salvage", "binding_digest": approval["binding_digest"]},
        actor_kind="operator", actor_id=action["actor_id"],
    )
    return {
        "status": "salvage-requested",
        "request_id": approval["request_id"],
        "salvage_binding": binding,
    }


def approve_salvage(
    store: InitiativeStore,
    initiative_id: str,
    request_id: str,
    *,
    actor_id: str = "cli",
) -> dict[str, Any]:
    with store.transaction_lock(initiative_id):
        approval = store.read_approval(initiative_id, request_id)
        if approval["action_class"] != "salvage":
            raise ActionRefused("approval request is not a salvage request")
        if approval["state"] == "approved":
            if not any(
                event["type"] == "approval-decided"
                and request_id in event["subject_ids"]
                for event in store.list_events_snapshot(initiative_id)
            ):
                append_event(
                    store, initiative_id, "approval-decided", [request_id],
                    {"action_class": "salvage", "decision": "approved"},
                    actor_kind="operator", actor_id=actor_id,
                )
            return approval
        if approval["state"] != "requested":
            raise ActionRefused("salvage approval request is no longer approvable")
        if datetime.now(timezone.utc) >= datetime.fromisoformat(
            approval["expires_at"][:-1] + "+00:00"
        ):
            raise ActionRefused("salvage approval request expired")
        changed = copy.deepcopy(approval)
        changed.update({"state": "approved", "updated_at": _now()})
        store.save_approval(
            initiative_id, changed, expected_digest=record_digest(approval),
        )
        append_event(
            store, initiative_id, "approval-decided", [request_id],
            {"action_class": "salvage", "decision": "approved"},
            actor_kind="operator", actor_id=actor_id,
        )
        return changed


def salvage_dispatch_binding(
    store: InitiativeStore,
    initiative: Mapping[str, Any],
    node: Mapping[str, Any],
    request_id: str,
    *,
    allow_consumed: bool = False,
    dispatch_expected_revision: int | None = None,
    dispatch_action_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate one approved salvage authority for scheduler consumption."""
    approval = store.read_approval(initiative["initiative_id"], request_id)
    permitted_states = {"approved", "consumed"} if allow_consumed else {"approved"}
    if approval["action_class"] != "salvage" or approval["state"] not in permitted_states:
        raise ActionRefused("salvage approval is not approved and unconsumed")
    if approval["active_plan_digest"] != initiative["active_plan"]["digest"]:
        raise ActionRefused("salvage approval active plan is stale")
    # Authority is the approved request's immutable binding digest and active
    # plan, not event adjacency. Other controller/worker events may land after
    # approval without invalidating the approved salvage plan.
    del dispatch_expected_revision, dispatch_action_id
    if approval["state"] == "approved" and datetime.now(timezone.utc) >= datetime.fromisoformat(
        approval["expires_at"][:-1] + "+00:00"
    ):
        raise ActionRefused("salvage approval expired before dispatch")
    request_actions = [
        item for item in store.list_actions_snapshot(initiative["initiative_id"])
        if item["action_class"] == "request-salvage"
        and action_outcome(item).get("request_id") == request_id
    ]
    if len(request_actions) != 1 or request_actions[0]["state"] != "completed":
        raise ActionRefused("salvage approval has no exact completed request action")
    if (
        approval["expected_state_revision"]
        != request_actions[0]["expected_state_revision"]
        or approval["active_plan_digest"]
        != request_actions[0]["active_plan_digest"]
    ):
        raise ActionRefused("salvage approval revision binding changed")
    binding = action_outcome(request_actions[0]).get("salvage_binding")
    if not isinstance(binding, dict):
        raise ActionRefused("salvage request binding is missing")
    if binding.get("node_id") != node["node_id"]:
        raise ActionRefused("salvage approval cannot be substituted onto another node")
    seal = store.read_seal(initiative["initiative_id"], binding["failure_seal_id"])
    expected = _salvage_binding(
        request_actions[0], node, seal, approval["rationale"],
    )
    digest = hashlib.sha256(_canonical(expected)).hexdigest()
    if binding != expected or approval["binding_digest"] != digest:
        raise ActionRefused("salvage approval binding changed")
    base = {
        "policy": "scope-baseline",
        "scope_origin": copy.deepcopy(seal["scope_origin"]),
        "upstream_node_ids": [],
        "seal_inputs": [{
            "seal_id": seal["seal_id"], "outcome": "failure", "read_only": True,
            "scope_origin": copy.deepcopy(seal["scope_origin"]),
        }],
    }
    return approval, base, seal


def consume_salvage_approval(
    store: InitiativeStore,
    initiative_id: str,
    approval: dict[str, Any],
) -> dict[str, Any]:
    current = store.read_approval(initiative_id, approval["request_id"])
    if current["state"] != "approved" or record_digest(current) != record_digest(approval):
        raise ActionRefused("salvage approval was already consumed or changed")
    changed = copy.deepcopy(current)
    changed.update({"state": "consumed", "updated_at": _now()})
    store.save_approval(
        initiative_id, changed, expected_digest=record_digest(current),
    )
    return changed


def _decide(
    store: InitiativeStore, action: dict[str, Any], payload: dict[str, Any],
) -> dict[str, Any]:
    seal = store.read_seal(action["initiative_id"], payload["paused_seal_id"])
    if seal["outcome"] != "paused":
        raise ActionRefused("decide requires an immutable paused seal")
    return {
        "status": "decision-recorded",
        "paused_seal_id": seal["seal_id"],
        "decision": payload["decision"],
    }


def _continue_node(
    store: InitiativeStore, action: dict[str, Any], payload: dict[str, Any],
) -> dict[str, Any]:
    initiative_id = action["initiative_id"]
    seal = store.read_seal(initiative_id, payload["paused_seal_id"])
    if seal["outcome"] != "paused":
        raise ActionRefused("continue-node requires an immutable paused seal")
    decision = store.read_action(initiative_id, payload["decision_action_id"])
    decision_outcome = action_outcome(decision)
    if (
        decision["action_class"] != "decide" or decision["state"] != "completed"
        or decision_outcome.get("paused_seal_id") != seal["seal_id"]
    ):
        raise ActionRefused("continue-node requires the exact completed operator decision")
    retained = [
        item for item in store.list_attempts_snapshot(initiative_id)
        if item["action_id"] == action["action_id"]
    ]
    if len(retained) > 1:
        raise ActionError("continuation action owns multiple retained attempts")
    if not retained and any(
        item["action_class"] == "continue-node" and item["state"] == "completed"
        and action_outcome(item).get("paused_seal_id") == seal["seal_id"]
        for item in store.list_actions_snapshot(initiative_id)
    ):
        raise ActionRefused("paused seal has already been continued once")
    node = store.read_node(initiative_id, payload["node_id"])
    if node["node_id"] != seal["node_id"] or (
        not retained and node["state"] != "needs-input"
    ):
        raise ActionRefused("paused continuation node binding or state changed")
    initiative = store.peek(initiative_id)
    plan = _active_plan(store, initiative)
    base = {
        "policy": "upstream-seal",
        "scope_origin": copy.deepcopy(seal["scope_origin"]),
        "upstream_node_ids": [],
        "seal_inputs": [{
            "seal_id": seal["seal_id"], "outcome": "paused", "read_only": False,
            "scope_origin": copy.deepcopy(seal["scope_origin"]),
        }],
    }
    if retained:
        attempt = retained[0]
        if attempt["node_id"] != payload["node_id"] or attempt["base"] != base:
            raise ActionRefused("retained continuation reservation binding changed")
    else:
        attempt = _reserve_attempt(
            store, initiative, plan, node, action["action_id"], base,
        )
    if node["state"] == "needs-input":
        ready = copy.deepcopy(node)
        ready["state"] = "ready"
        store.save_node(initiative_id, ready, expected_digest=record_digest(node))
        if not any(
            event["type"] == "node-ready"
            and attempt["attempt_id"] in event["subject_ids"]
            for event in store.list_events_snapshot(initiative_id)
        ):
            append_event(
                store, initiative_id, "node-ready",
                [node["node_id"], attempt["attempt_id"], seal["seal_id"]],
                {"continuation_of": seal["seal_id"], "decision_action_id": decision["action_id"]},
                actor_kind="controller", actor_id="action-broker",
            )
    current_initiative = store.peek(initiative_id)
    if current_initiative["state"] == "needs-input":
        running = copy.deepcopy(current_initiative)
        running.update({
            "state": "running",
            "state_revision": current_initiative["state_revision"] + 1,
            "updated_at": _now(),
        })
        validate_initiative(running)
        store.save_initiative(
            running, expected_digest=record_digest(current_initiative),
        )
        append_event(
            store, initiative_id, "initiative-state-changed", [initiative_id],
            {"from": "needs-input", "to": "running", "continued_seal_id": seal["seal_id"]},
            actor_kind="controller", actor_id="action-broker",
        )
    return {
        "status": "continuation-allocated",
        "node_id": node["node_id"],
        "attempt_id": attempt["attempt_id"],
        "control_task_id": attempt["task_id"],
        "paused_seal_id": seal["seal_id"],
        "decision_action_id": decision["action_id"],
        "decision": decision_outcome["decision"],
    }


def _execute_local(
    store: InitiativeStore,
    action: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    kind = action["action_class"]
    if kind == "dispatch-node":
        node = store.read_node(action["initiative_id"], payload["node_id"])
        if node["type"] != "verify":
            raise ActionError("only a verify node uses controller-local dispatch")
        from .verification import run_verification

        verification = run_verification(
            store, action["initiative_id"], payload["node_id"],
            action_id=action["action_id"],
            verification_id=action_outcome(action).get("verification_id"),
        )
        return {
            "status": verification["outcome"],
            "verification_id": verification["verification_id"],
            "node_id": payload["node_id"],
        }
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
    if kind == "repair-node":
        return _repair_node(store, action, payload["node_id"], payload["seal_id"])
    if kind == "request-salvage":
        return _request_salvage(store, action, payload)
    if kind == "decide":
        return _decide(store, action, payload)
    if kind == "continue-node":
        return _continue_node(store, action, payload)
    if kind == "finalize":
        from .readiness import finalize_initiative

        initiative = finalize_initiative(
            store, action["initiative_id"], payload["outcome"], payload["reason"],
            action_id=action["action_id"],
            source_state=action_outcome(action).get("finalize_from"),
        )
        return {"status": initiative["state"], "reason": payload["reason"]}
    if kind == "archive":
        from .readiness import archive_initiative

        initiative, inventory = archive_initiative(
            store, action["initiative_id"],
            source_state=action_outcome(action).get("archive_from"),
            action_id=action["action_id"],
        )
        return {"status": initiative["state"], "retained_inventory": inventory}
    if kind == "unarchive":
        from .readiness import unarchive_initiative

        initiative = unarchive_initiative(
            store, action["initiative_id"], action_id=action["action_id"],
        )
        return {"status": initiative["state"]}
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
    deferred_verification: tuple[dict[str, Any], dict[str, Any]] | None = None
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

        # This fixed denial is deliberately before every approval lookup.
        if action["action_class"] in initiative["forbidden_action_classes"]:
            return _refuse(store, action, "action class is forbidden in Core v1")
        if action["action_class"] not in SUPPORTED_ACTION_KINDS:
            return _refuse(store, action, "action class is unsupported in Core 2b")
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
        if action["action_class"] in _EXECUTION_AUTHORITY_ACTIONS:
            try:
                executable_plan = store.read_plan(
                    initiative_id, initiative["active_plan"]["revision"],
                )
            except StoreError as exc:
                return _refuse(store, action, str(exc))
            if executable_plan["digest"] != initiative["active_plan"]["digest"]:
                return _refuse(
                    store, action,
                    "active plan digest does not match retained executable plan",
                )
        if action["action_class"] == "finalize" and initiative["state"] != "running":
            return _refuse(store, action, "only a running initiative may be finalized")
        if action["action_class"] == "finalize":
            from .readiness import ReadinessError, prevalidate_finalization

            try:
                prevalidate_finalization(
                    store, initiative_id, payload["outcome"], payload["reason"],
                )
            except ReadinessError as exc:
                return _refuse(store, action, str(exc))
        if action["action_class"] == "archive" and initiative["state"] not in {
            "ready-for-integration", "partial", "failed", "cancelled",
        }:
            return _refuse(store, action, "only a terminal initiative outcome may be archived")
        if action["action_class"] == "unarchive" and initiative["state"] != "archived":
            return _refuse(store, action, "only an archived initiative may be unarchived")
        if action["action_class"] == "dispatch-node":
            target_node = store.read_node(initiative_id, payload["node_id"])
            if target_node["type"] == "verify":
                from .scheduler import pause_for_breaker
                from .storage import storage_report
                from .verification import VerificationError, prevalidate_verification

                if storage_report(initiative, store=store)["pause_recommended"]:
                    pause_for_breaker(
                        store, initiative_id,
                        "retained storage pause threshold reached",
                        event_type="storage-threshold-reached",
                        subject_ids=[target_node["node_id"]],
                    )
                    return _refuse(
                        store, action, "retained storage pause threshold reached",
                    )
                try:
                    prevalidate_verification(
                        store, initiative_id, target_node["node_id"],
                        exclude_action_id=action["action_id"],
                    )
                except VerificationError as exc:
                    return _refuse(store, action, str(exc))
        action = set_action_state(
            store, action, "validated",
            {"payload": payload, "status": "validated"},
        )
        if (
            action["action_class"] == "dispatch-node"
            and store.read_node(initiative_id, payload["node_id"])["type"] != "verify"
        ):
            from .scheduler import SchedulerError, dispatch

            try:
                return dispatch(
                    store, store.config, initiative_id, payload["node_id"], action=action,
                )["action"]
            except SchedulerError as exc:
                return _refuse(store, action, str(exc))
        is_verification = (
            action["action_class"] == "dispatch-node"
            and store.read_node(initiative_id, payload["node_id"])["type"] == "verify"
        )
        dispatching_outcome = {"payload": payload, "status": "dispatching"}
        if action["action_class"] == "dispatch-node":
            dispatching_outcome["node_id"] = payload["node_id"]
        if is_verification:
            dispatching_outcome.update({
                "verification_id": new_uuid(),
                "controller_pid": os.getpid(),
                "controller_start_ticks": _process_start_ticks(os.getpid()),
            })
        if action["action_class"] == "finalize":
            dispatching_outcome["finalize_from"] = initiative["state"]
        if action["action_class"] == "archive":
            # Retain the terminal outcome before the initiative edge so
            # reconciliation can complete an interrupted archive event.
            dispatching_outcome["archive_from"] = initiative["state"]
        if action["action_class"] == "unarchive":
            archive_events = [
                event for event in store.list_events_snapshot(initiative_id)
                if event["type"] == "initiative-state-changed"
                and event["payload"].get("to") == "archived"
            ]
            dispatching_outcome["unarchive_to"] = (
                None if not archive_events else archive_events[-1]["payload"].get("from")
            )
        action = set_action_state(
            store, action, "dispatching",
            dispatching_outcome,
        )
        if is_verification:
            from .verification import VerificationError, prepare_verification_intent

            try:
                prepare_verification_intent(
                    store, initiative_id, payload["node_id"],
                    action_id=action["action_id"],
                    verification_id=dispatching_outcome["verification_id"],
                )
            except (VerificationError, StoreError, OSError, ValueError) as exc:
                current = store.read_action(initiative_id, action["action_id"])
                current = set_action_state(
                    store, current, "indeterminate",
                    {
                        **action_outcome(current), "status": "indeterminate",
                        "reason": str(exc),
                    },
                )
                append_event(
                    store, initiative_id, "action-indeterminate",
                    [current["action_id"]], {"reason": str(exc)},
                    actor_kind="controller", actor_id="action-broker",
                )
                return current
            # Both action and exact verification/materialization intent are
            # durable. Command execution must not retain the initiative lock.
            deferred_verification = (action, payload)
        else:
            try:
                result = _execute_local(store, action, payload)
                current = store.read_action(initiative_id, action["action_id"])
                return set_action_state(
                    store, current, "completed",
                    {**action_outcome(current), **result},
                )
            except ActionRefused as exc:
                return _refuse(
                    store, store.read_action(initiative_id, action["action_id"]), str(exc),
                )
            except (ActionError, StoreError, OSError, ValueError) as exc:
                current = store.read_action(initiative_id, action["action_id"])
                if current["state"] == "completed":
                    return current
                if current["state"] == "dispatching":
                    current = set_action_state(
                        store, current, "indeterminate",
                        {
                            **action_outcome(current), "status": "indeterminate",
                            "reason": str(exc),
                        },
                    )
                    append_event(
                        store, initiative_id, "action-indeterminate",
                        [current["action_id"]], {"reason": str(exc)},
                        actor_kind="controller", actor_id="action-broker",
                    )
                return current

    if deferred_verification is None:
        raise ActionError("local action dispatch lost its execution binding")
    action, payload = deferred_verification
    try:
        result = _execute_local(store, action, payload)
    except (ActionRefused, ActionError, StoreError, OSError, ValueError) as exc:
        with store.transaction_lock(initiative_id):
            current = store.read_action(initiative_id, action["action_id"])
            if current["state"] == "dispatching":
                current = set_action_state(
                    store, current, "indeterminate",
                    {
                        **action_outcome(current), "status": "indeterminate",
                        "reason": str(exc),
                    },
                )
                append_event(
                    store, initiative_id, "action-indeterminate", [current["action_id"]],
                    {"reason": str(exc)}, actor_kind="controller", actor_id="action-broker",
                )
        return store.read_action(initiative_id, action["action_id"])
    with store.transaction_lock(initiative_id):
        current = store.read_action(initiative_id, action["action_id"])
        return set_action_state(
            store, current, "completed", {**action_outcome(current), **result},
        )


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
            if (
                action["state"] == "dispatching"
                and action["action_class"] == "dispatch-node"
                and isinstance(action_outcome(action).get("verification_id"), str)
                and _verification_controller_is_live(action_outcome(action))
            ):
                # A separate reconcile/report process may observe the command
                # while its controller is legitimately outside the initiative
                # lock. PID start ticks distinguish this lease from PID reuse.
                results.append(action)
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
                node_id = outcome.get("node_id")
                if isinstance(node_id, str):
                    node_probe = store.read_node(initiative_id, node_id)
                else:
                    node_probe = None
                if node_probe is not None and node_probe["type"] == "verify":
                    retained = [
                        item for item in store.list_verifications_snapshot(initiative_id)
                        if item["node_id"] == node_id
                        and item["active_plan_digest"] == action["active_plan_digest"]
                        and item["state"] != "stale"
                    ]
                    if len(retained) > 1:
                        results.append(action)
                        continue
                    if retained and retained[0]["state"] in {"passed", "failed"}:
                        verification = retained[0]
                        desired_node_state = (
                            "succeeded" if verification["state"] == "passed" else "failed"
                        )
                        current_node = store.read_node(initiative_id, node_id)
                        if current_node["state"] == "evaluating":
                            reconciled_node = copy.deepcopy(current_node)
                            reconciled_node["state"] = desired_node_state
                            validate_node(reconciled_node)
                            store.save_node(
                                initiative_id, reconciled_node,
                                expected_digest=record_digest(current_node),
                            )
                            append_event(
                                store, initiative_id, "node-state-changed",
                                [node_id, verification["verification_id"]],
                                {
                                    "from": "evaluating", "to": desired_node_state,
                                    "reconciled": "terminal-verification",
                                },
                                actor_kind="controller", actor_id="action-reconciler",
                            )
                            current_node = reconciled_node
                        if current_node["state"] != desired_node_state:
                            results.append(action)
                            continue
                        if not any(
                            event["type"] == "node-state-changed"
                            and verification["verification_id"] in event["subject_ids"]
                            and event["payload"].get("reconciled")
                            == "terminal-verification"
                            for event in store.list_events_snapshot(initiative_id)
                        ):
                            append_event(
                                store, initiative_id, "node-state-changed",
                                [node_id, verification["verification_id"]],
                                {
                                    "from": "evaluating", "to": desired_node_state,
                                    "reconciled": "terminal-verification",
                                },
                                actor_kind="controller", actor_id="action-reconciler",
                            )
                        if not any(
                            event["type"] == "verification-finished"
                            and verification["verification_id"] in event["subject_ids"]
                            for event in store.list_events_snapshot(initiative_id)
                        ):
                            append_event(
                                store, initiative_id, "verification-finished",
                                [node_id, verification["verification_id"], verification["seal_id"]],
                                {
                                    "outcome": verification["outcome"],
                                    "bundle_digest": verification["bundle_digest"],
                                    "command_count": len(verification["commands"]),
                                    "reconciled": True,
                                },
                                actor_kind="controller", actor_id="action-reconciler",
                            )
                        if (
                            verification["state"] == "passed"
                            and store.peek(initiative_id)["state"] == "running"
                        ):
                            from .readiness import bind_readiness

                            bind_readiness(store, initiative_id)
                        action = set_action_state(
                            store, action, "completed",
                            {
                                **outcome,
                                "status": verification["outcome"],
                                "verification_id": verification["verification_id"],
                            },
                        )
                        results.append(action)
                        continue
                    if retained and retained[0]["state"] in {
                        "pending", "dispatching", "running", "indeterminate",
                    }:
                        verification = retained[0]
                        if verification["state"] != "indeterminate":
                            interrupted = copy.deepcopy(verification)
                            interrupted.update({
                                "state": "indeterminate",
                                "outcome": "indeterminate",
                                "updated_at": _now(),
                            })
                            store.save_verification(
                                initiative_id, interrupted,
                                expected_digest=record_digest(verification),
                            )
                            verification = interrupted
                        current_node = store.read_node(initiative_id, node_id)
                        if current_node["state"] == "evaluating":
                            action = set_action_state(
                                store, action, "indeterminate",
                                {
                                    **outcome,
                                    "verification_node_from": "evaluating",
                                },
                            )
                            outcome = action_outcome(action)
                            retryable = copy.deepcopy(current_node)
                            retryable["state"] = "ready"
                            validate_node(retryable)
                            store.save_node(
                                initiative_id, retryable,
                                expected_digest=record_digest(current_node),
                            )
                            current_node = retryable
                        if (
                            current_node["state"] == "ready"
                            and outcome.get("verification_node_from") == "evaluating"
                            and not any(
                            event["type"] == "node-ready"
                            and verification["verification_id"] in event["subject_ids"]
                            and event["payload"].get("reason")
                            == "controller-verification-interrupted"
                            for event in store.list_events_snapshot(initiative_id)
                            )
                        ):
                            append_event(
                                store, initiative_id, "node-ready",
                                [node_id, verification["verification_id"]],
                                {
                                    "from": "evaluating", "to": "ready",
                                    "reason": "controller-verification-interrupted",
                                },
                                actor_kind="controller", actor_id="action-reconciler",
                            )
                        if not any(
                            event["type"] == "verification-finished"
                            and verification["verification_id"] in event["subject_ids"]
                            for event in store.list_events_snapshot(initiative_id)
                        ):
                            append_event(
                                store, initiative_id, "verification-finished",
                                [node_id, verification["verification_id"], verification["seal_id"]],
                                {
                                    "outcome": "indeterminate",
                                    "bundle_digest": verification["bundle_digest"],
                                    "command_count": len(verification["commands"]),
                                },
                                actor_kind="controller", actor_id="action-reconciler",
                            )
                        action = set_action_state(
                            store, action, "completed",
                            {
                                **outcome,
                                "status": "indeterminate",
                                "verification_id": verification["verification_id"],
                            },
                        )
                        results.append(action)
                        continue
                    if not retained and node_probe["state"] == "ready":
                        action = set_action_state(
                            store, action, "refused",
                            {
                                **outcome, "status": "not-started",
                                "reason": "controller verification has no durable start record",
                            },
                        )
                        append_event(
                            store, initiative_id, "action-refused", [action["action_id"]],
                            {
                                "action_class": "dispatch-node",
                                "reason": "controller verification has no durable start record",
                            },
                            actor_kind="controller", actor_id="action-reconciler",
                        )
                        results.append(action)
                        continue
                    results.append(action)
                    continue
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
                        if outcome.get("salvage_request_id") is not None:
                            result = dispatch(
                                store, store.config, initiative_id,
                                node["node_id"], action=action,
                            )
                            results.append(result["action"])
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
                    current_attempt = store.read_attempt(
                        initiative_id, attempt["attempt_id"],
                    )
                    if current_attempt["state"] not in ATTEMPT_TERMINAL_STATES:
                        cancelled = copy.deepcopy(current_attempt)
                        cancelled.update({"state": "cancelled", "updated_at": _now()})
                        store.save_attempt(
                            initiative_id, cancelled,
                            expected_digest=record_digest(current_attempt),
                        )
                    action = set_action_state(
                        store, action, "completed",
                        {**outcome, "status": "cancelled", "control_state": observed["state"]},
                    )
                results.append(action)
                continue
            if kind in {"repair-node", "request-salvage", "decide", "continue-node"}:
                try:
                    result = _execute_local(store, action, outcome["payload"])
                    current = store.read_action(initiative_id, action["action_id"])
                    action = set_action_state(
                        store, current, "completed", {**action_outcome(current), **result},
                    )
                except (ActionError, ActionRefused, StoreError, ModelError, ValueError):
                    results.append(action)
                    continue
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
            payload = outcome.get("payload", {})
            if kind == "finalize":
                from .readiness import finalize_initiative

                try:
                    initiative = finalize_initiative(
                        store, initiative_id, payload.get("outcome"), payload.get("reason"),
                        action_id=action["action_id"],
                        source_state=outcome.get("finalize_from"),
                    )
                except (StoreError, ValueError):
                    completed = False
                else:
                    completed = initiative["state"] == payload.get("outcome")
            elif kind == "archive":
                from .readiness import archive_initiative

                try:
                    initiative, inventory = archive_initiative(
                        store, initiative_id,
                        source_state=outcome.get("archive_from"),
                        action_id=action["action_id"],
                    )
                except (StoreError, ValueError):
                    completed = False
                else:
                    outcome = {**outcome, "retained_inventory": inventory}
                    completed = initiative["state"] == "archived"
            elif kind == "unarchive":
                from .readiness import unarchive_initiative

                try:
                    initiative = unarchive_initiative(
                        store, initiative_id, action_id=action["action_id"],
                        recovery_state=outcome.get("unarchive_to"), recover=True,
                    )
                except (StoreError, ValueError):
                    completed = False
                else:
                    completed = initiative["state"] == outcome.get("unarchive_to")
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
    "approve_salvage", "build_action_document", "consume_salvage_approval",
    "payload_digest", "reconcile_actions", "salvage_dispatch_binding",
    "set_action_state", "submit_action",
]
