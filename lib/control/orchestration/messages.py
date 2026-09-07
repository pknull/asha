"""Durable addressed context, not directives, approvals, or terminal input.

Store records are truth; journal events are notifications. Pending reads never
change either, and recipients acknowledge only by an explicit digest-bound act.
"""
from __future__ import annotations

import copy
import os
import time
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from ..harness import (caller_descends_from, pane_ancestry_ok, verify_process, process_identity,
                       _process_stat_fields, _stat_integer)
from ..store import SnapshotBudget, StoreError, TaskStore, _directory_fd, _managed_start
from . import coordinator
from .actions import append_event
from .model import (
    MESSAGE_CONTRACT, MESSAGE_RECEIPT_CONTRACT, COORDINATOR_LIVE_STATES,
    canonical_uuid, message_content_digest, message_sender_identity, chair_sender_identity,
    validate_coordinator, validate_message, record_digest,
)

PENDING_CONTRACT = "asha.orchestration-message-pending.v1"
# Sending requires a complete global role proof, not the startup sample.
ROLE_SCAN_LIMIT = 65536
ROLE_SCAN_SECONDS = 10.0


def terminal_safe(value: Any) -> Any:
    """Escape C0/C1, bidi/format controls and surrogates even in text output."""
    if isinstance(value, str):
        return "".join(
            f"\\u{ord(c):04x}" if unicodedata.category(c) in {"Cc", "Cf", "Cs"} else c
            for c in value
        )
    if isinstance(value, dict):
        return {terminal_safe(k): terminal_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [terminal_safe(v) for v in value]
    return value


def _owned_anchor(anchor: Mapping[str, Any]) -> None:
    for pid_key, identity_key in (("pane_pid", "process_start_identity"),
                                  ("server_pid", "server_start_identity")):
        pid = anchor[pid_key]
        if (Path(f"/proc/{pid}").stat().st_uid != os.geteuid()
                or not verify_process(pid, anchor[identity_key])):
            raise coordinator.CoordinatorError("anchor process is foreign or changed")
    if not pane_ancestry_ok(anchor["pane_pid"], anchor["server_pid"]):
        raise coordinator.CoordinatorError("pane is not a child of its owned tmux server")


def chair_sender(store, env, tmux) -> dict[str, Any]:
    """Observe the real chair seat; labels and environment cannot confer role.

    Global role exclusions include predecessors and retained workers, with
    process-incarnation checks defeating recycled PIDs. Incomplete scans refuse
    rather than treating the absence of evidence as evidence of a chair.
    """
    if any(env.get(key) for key in (
        coordinator.ENV_COORDINATOR_ID, coordinator.ENV_GENERATION,
        "ASHA_CONTROL_MANAGED", "ASHA_CONTROL_TASK_ID", "ASHA_CONTROL_RUN_ID",
    )):
        raise coordinator.CoordinatorError("coordinator/worker cannot impersonate operator-chair")
    anchor = coordinator.caller_anchor(env, tmux)
    _owned_anchor(anchor)
    chair = Path(store.config.control.asha_home) / "chair"
    with _directory_fd(chair, create=False,
                       managed_start=_managed_start(chair, ("chair",))) as fd:
        if fd is None:
            raise coordinator.CoordinatorError("operator chair seat is absent")
        expected = os.fstat(fd)
        # A wrapped harness is often a child of the pane's interactive shell.
        # Observe the highest chair-seated process in the proven parent chain,
        # not the transient message CLI or an env-supplied harness label.
        pid = os.getpid()
        seat = None
        for _ in range(64):
            identity = process_identity(pid)
            if identity is None or Path(f"/proc/{pid}").stat().st_uid != os.geteuid():
                raise coordinator.CoordinatorError("chair ancestry process is unavailable or foreign")
            actual = Path(f"/proc/{pid}/cwd").stat()
            if (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino):
                seat = {"pid": pid, "process_start_identity": identity}
            if pid == anchor["pane_pid"]:
                break
            fields = _process_stat_fields(pid)
            if fields is None:
                raise coordinator.CoordinatorError("chair ancestry disappeared")
            parent = _stat_integer(fields, 1)
            if parent <= 1 or parent == pid:
                raise coordinator.CoordinatorError("chair ancestry no longer reaches its pane")
            pid = parent
        else:
            raise coordinator.CoordinatorError("chair ancestry exceeds supported bound")
        if seat is None:
            raise coordinator.CoordinatorError("calling pane has no observed operator chair process")
    if tmux.pane_option(anchor["pane_id"], coordinator.PANE_COORDINATOR_OPTION):
        raise coordinator.CoordinatorError("coordinator pane cannot impersonate operator-chair")
    if (tmux.pane_option(anchor["pane_id"], "@asha_run_id")
            or tmux.session_option(anchor["session"], "@asha_task_id")):
        raise coordinator.CoordinatorError("worker pane cannot impersonate operator-chair")
    deadline = time.monotonic() + ROLE_SCAN_SECONDS
    initiatives_budget = SnapshotBudget(deadline=deadline, limit=ROLE_SCAN_LIMIT)
    roles_budget = SnapshotBudget(deadline=deadline, limit=ROLE_SCAN_LIMIT)
    tasks_budget = SnapshotBudget(deadline=deadline, limit=ROLE_SCAN_LIMIT)
    for initiative in store.bounded_snapshots(initiatives_budget):
        for record in store.bounded_records(
            initiative["initiative_id"], "coordinators", validate_coordinator,
            "coordinator_id", roles_budget,
        ):
            other = record["anchor"]
            if (verify_process(other["pane_pid"], other["process_start_identity"])
                    and caller_descends_from(other["pane_pid"])):
                raise coordinator.CoordinatorError("global coordinator ancestry refuses chair impersonation")
    for task in TaskStore(store.config.control).bounded_snapshots(tasks_budget):
        for run in task["runs"]:
            if (run["pid"] is None or run["process_start_identity"] is None) and run["state"] not in {"exited", "failed"}:
                tasks_budget.unavailable += 1
            if (run["pid"] is not None and run["process_start_identity"] is not None
                    and verify_process(run["pid"], run["process_start_identity"])
                    and caller_descends_from(run["pid"])):
                raise coordinator.CoordinatorError("global worker ancestry refuses chair impersonation")
    if any(not budget.summary()["complete"] for budget in (
        initiatives_budget, roles_budget, tasks_budget,
    )):
        raise coordinator.CoordinatorError("global chair role evidence is unavailable or truncated")
    # Recheck the process incarnation after reading cwd and role evidence.
    _owned_anchor(anchor)
    if not verify_process(seat["pid"], seat["process_start_identity"]) or not caller_descends_from(seat["pid"]):
        raise coordinator.CoordinatorError("chair process incarnation changed")
    return {"role": "operator-chair", "identity": chair_sender_identity(anchor, seat),
            "anchor": anchor, "process": seat}


def _address(current):
    return {key: copy.deepcopy(current[key]) for key in ("coordinator_id", "generation", "anchor")}


def _select(current, coordinator_id=None, generation=None):
    if coordinator_id is not None and coordinator_id != current["coordinator_id"]:
        raise coordinator.CoordinatorError("recipient coordinator identity is stale or forged")
    if generation is not None and (type(generation) is not int or generation != current["generation"]):
        raise coordinator.CoordinatorError("recipient generation is stale or forged")


def _same_address(address, current):
    return (current is not None and current["state"] in COORDINATOR_LIVE_STATES
            and address["coordinator_id"] == current["coordinator_id"]
            and address["generation"] == current["generation"]
            and message_sender_identity(address["anchor"]) == message_sender_identity(current["anchor"]))


def _journal(store, message, state, actor_kind, actor_id):
    event_type = "message-" + state
    # A retry repairs record-before-event interruption without duplicating an
    # already durable event. No content is copied to the event history.
    events = store.list_events_snapshot(message["initiative_id"])
    expected_payload = {
        "message_id": message["message_id"], "content_digest": message["content_digest"],
        "coordinator_id": message["recipient"]["coordinator_id"],
        "generation": message["recipient"]["generation"],
    }
    for event in events:
        if event["type"] == event_type and event["payload"].get("message_id") == message["message_id"]:
            if (event["payload"] != expected_payload or event["actor_kind"] != actor_kind
                    or event["actor_id"] != actor_id):
                raise StoreError("message journal identity/content conflict")
            head = store.peek(message["initiative_id"])
            if event["sequence"] > head["last_event_sequence"]:
                # Repair only our own exact single notification whose immutable
                # record landed before the atomic head replacement failed. This
                # is not general journal repair or permission to adopt history.
                if (events[-1] != event or event["sequence"] != head["last_event_sequence"] + 1):
                    raise StoreError("message journal tail requires explicit recovery")
                updated = {**head, "last_event_sequence": event["sequence"],
                           "state_revision": head["state_revision"] + 1,
                           "updated_at": event["recorded_at"]}
                store.save_initiative(updated, expected_digest=record_digest(head))
            return
    append_event(store, message["initiative_id"], event_type, [message["message_id"]], {
        "message_id": message["message_id"], "content_digest": message["content_digest"],
        "coordinator_id": message["recipient"]["coordinator_id"],
        "generation": message["recipient"]["generation"],
    }, actor_kind=actor_kind, actor_id=actor_id)


def send(store, initiative_id, *, message_id, body, env, tmux,
         coordinator_id=None, generation=None, sender_identity=None):
    canonical_uuid(message_id)
    digest = message_content_digest(body)
    sender = chair_sender(store, env, tmux)
    if sender_identity is not None and sender_identity != sender["identity"]:
        raise coordinator.CoordinatorError("supplied sender identity differs from observed chair")
    current = coordinator.require_live_coordinator(store, initiative_id)
    _select(current, coordinator_id, generation)
    address = _address(current)
    with store.transaction_lock(initiative_id):
        current = coordinator.require_live_coordinator(store, initiative_id)
        if not _same_address(address, current):
            raise coordinator.CoordinatorError("recipient changed before message persistence")
        state, detail = coordinator.anchor_liveness(current["anchor"], tmux)
        if state != "live":
            raise coordinator.CoordinatorError("recipient anchor unavailable: " + detail)
        _owned_anchor(current["anchor"])
        rechecked = chair_sender(store, env, tmux)
        if rechecked["identity"] != sender["identity"]:
            raise coordinator.CoordinatorError("sender changed before message persistence")
        existing = store.message_snapshot(initiative_id, message_id)
        if existing is not None:
            if (existing["sender"]["identity"] != sender["identity"]
                    or existing["body"] != body or existing["content_digest"] != digest
                    or not _same_address(existing["recipient"], current)):
                raise StoreError("message ID replay conflicts with immutable identity/content")
            message = existing
        else:
            message = validate_message({
                "contract": MESSAGE_CONTRACT, "initiative_id": initiative_id,
                "message_id": message_id, "sender": sender, "recipient": address,
                "body": body, "content_digest": digest, "persisted_at": coordinator._now(),
            })
            store.save_message(initiative_id, message)
        _journal(store, message, "persisted", "operator", "chair:" + sender["identity"])
        return terminal_safe(message)


def _receipt(store, message, directory):
    receipt = store.message_snapshot(message["initiative_id"], message["message_id"], directory=directory)
    if receipt is not None and any(receipt[key] != message[key] for key in ("content_digest", "recipient")):
        raise StoreError("receipt does not bind the immutable message")
    return receipt


def pending(store, initiative_id, *, current=None):
    if current is None:
        current = store.current_coordinator(initiative_id)
    rows = []
    for message in store.list_messages_snapshot(initiative_id):
        ack = _receipt(store, message, "message-acks")
        if ack is not None:
            continue
        observed = _receipt(store, message, "message-observations")
        rows.append({**message, "status": "observed" if observed else "persisted",
                     "address_status": "current" if _same_address(message["recipient"], current) else "stale-address"})
    return terminal_safe({"contract": PENDING_CONTRACT, "initiative_id": initiative_id, "messages": rows})


def pending_ids(store, initiative_id, current):
    return [row["message_id"] for row in pending(store, initiative_id, current=current)["messages"]
            if row["address_status"] == "current"]


def _receive_or_ack(store, initiative_id, message_id, *, env, tmux, digest=None,
                    coordinator_id=None, generation=None, acknowledge=False):
    canonical_uuid(message_id)
    with store.transaction_lock(initiative_id):
        current = coordinator.require_live_coordinator(store, initiative_id)
        _select(current, coordinator_id, generation)
        coordinator.require_anchored_caller(current, env, tmux)
        _owned_anchor(current["anchor"])
        message = store.message_snapshot(initiative_id, message_id)
        if message is None:
            raise StoreError("message not found")
        if not _same_address(message["recipient"], current):
            raise coordinator.CoordinatorError("stale-address message cannot be received or acked by successor")
        if acknowledge and digest != message["content_digest"]:
            raise StoreError("explicit acknowledgement requires the exact content digest")
        if acknowledge and _receipt(store, message, "message-observations") is None:
            raise StoreError("receive the message before explicitly acknowledging it")
        state = "acknowledged" if acknowledge else "observed"
        directory = "message-acks" if acknowledge else "message-observations"
        receipt = _receipt(store, message, directory)
        if receipt is None:
            receipt = {"contract": MESSAGE_RECEIPT_CONTRACT,
                       "initiative_id": initiative_id, "message_id": message_id,
                       "content_digest": message["content_digest"],
                       "recipient": message["recipient"], "state": state,
                       "recorded_at": coordinator._now()}
            store.save_message_receipt(initiative_id, receipt)
        _journal(store, message, state, "coordinator", coordinator.actor_id(current))
        return terminal_safe({"message": message, "receipt": receipt,
                              "acknowledgement": _receipt(store, message, "message-acks")})


def receive(store, initiative_id, message_id, *, env, tmux, coordinator_id=None, generation=None):
    return _receive_or_ack(store, initiative_id, message_id, env=env, tmux=tmux,
                           coordinator_id=coordinator_id, generation=generation)


def ack(store, initiative_id, message_id, *, digest, env, tmux, coordinator_id=None, generation=None):
    return _receive_or_ack(store, initiative_id, message_id, env=env, tmux=tmux,
                           digest=digest, acknowledge=True,
                           coordinator_id=coordinator_id, generation=generation)
