"""Typed, bounded assignment observation. No authority, allocation or write IO.

The snapshot adapter deliberately has no forwarding fallback: production
binding helpers get only explicitly read-only implementations, never the
store's locking/sweeping ordinary readers. Re-read every input before return;
this is an optimistic observation, not a dispatch reservation.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone

from ..store import SnapshotBudget
from ..tmux import TmuxAdapter
from . import model, scheduler
from .actions import (action_outcome, salvage_dispatch_binding,
                      salvage_request_binding)
from .coordinator import anchor_liveness
from .store import InitiativeStore
from .verification import VerificationError, preflight_verification_gates

CONTRACT = "asha.orchestration-assignment-preview.v1"
MAX_RECORDS = 256
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_PREVIEW_BYTES = 256 * 1024
DEADLINE_SECONDS = 2.0
PREVIEW_ATTEMPT_ID = "00000000-0000-4000-8000-000000000000"
_CLASSES = {
    "nodes": (model.validate_node, "node_id"),
    "seals": (model.validate_seal, "seal_id"),
    "results": (model.validate_result, "result_id"),
    "reviews": (model.validate_review, "review_id"),
    "approvals": (model.validate_approval, "request_id"),
    "actions": (model.validate_action, "action_id"),
    "attempts": (model.validate_attempt, "attempt_id"),
    "coordinators": (model.validate_coordinator, "coordinator_id"),
    "events": (model.validate_event, "event_id"),
}


class PreviewError(ValueError):
    """A bounded refusal; retry only after the named evidence is available."""


def _bytes(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


class _Snapshot:
    def __init__(self, store, initiative_id):
        self.store = store
        self.config = store.config
        self.iid = initiative_id
        self.deadline = time.monotonic() + DEADLINE_SECONDS
        self.cache = {}
        self.size = 0

    def _ready(self):
        if time.monotonic() >= self.deadline:
            raise PreviewError("preview deadline reached; retry with available local evidence")

    def _load(self, key):
        self._ready()
        if key == "initiative":
            return self.store.peek(self.iid)
        if isinstance(key, int):
            value = self.store.read_plan_snapshot(self.iid, key)
            # Observation-only historical plans must not acquire execution
            # semantics just because they can be read without locking.
            return self.store._validate_stored_plan(value)
        validator, field = _CLASSES[key]
        budget = SnapshotBudget(deadline=self.deadline, limit=MAX_RECORDS)
        records = []
        size = 0
        for name, value in self.store.iter_record_snapshots(self.iid, key, validator, budget):
            # Residue is ignored, never swept, but charged to the cap.
            if name.startswith("."):
                continue
            if not name.endswith(".json"):
                raise PreviewError(f"preview {key} has an unexpected filename")
            event_match = re.fullmatch(r"([0-9]{6})-([0-9a-f-]{36})\.json", name) if key == "events" else None
            if key == "events" and event_match is None:
                raise PreviewError("preview events has an invalid filename")
            identity = (model.canonical_uuid(event_match[2]) if event_match else
                        model.validate_slug(name[:-5], "node_id") if key == "nodes"
                        else model.canonical_uuid(name[:-5]))
            if event_match and value["sequence"] != int(event_match[1]):
                raise PreviewError("preview event sequence binding changed")
            if value[field] != identity or value.get("initiative_id", self.iid) != self.iid:
                raise PreviewError(f"preview {key} contains foreign or changed identity")
            size += len(_bytes(value))
            if size > MAX_SNAPSHOT_BYTES:
                raise PreviewError("preview snapshot byte capacity exceeded")
            records.append(value)
        if not budget.summary()["complete"]:
            raise PreviewError(f"preview {key} is capped or unavailable; cannot prove binding")
        if key == "events" and sorted(r["sequence"] for r in records) != list(range(1, len(records) + 1)):
            raise PreviewError("preview event sequence is incomplete or duplicated")
        self._ready()
        return sorted(records, key=lambda r: r[field])

    def get(self, key):
        if key not in self.cache:
            value = self._load(key)
            self.size += len(_bytes(value))
            if self.size > MAX_SNAPSHOT_BYTES:
                raise PreviewError("preview snapshot byte capacity exceeded")
            self.cache[key] = value
        return copy.deepcopy(self.cache[key])

    def _identity(self, iid):
        if iid != self.iid:
            raise PreviewError("preview cannot read a foreign initiative")

    def peek(self, iid):
        self._identity(iid)
        return self.get("initiative")

    def read_plan(self, iid, revision):
        self._identity(iid)
        return self.get(revision)

    def _record(self, iid, directory, identity):
        self._identity(iid)
        field = _CLASSES[directory][1]
        found = [r for r in self.get(directory) if r[field] == identity]
        if len(found) != 1:
            raise PreviewError(f"preview requires one retained {directory} identity")
        return found[0]

    def read_node(self, iid, identity):
        return self._record(iid, "nodes", identity)

    def read_approval(self, iid, identity):
        return self._record(iid, "approvals", identity)

    def read_seal(self, iid, identity):
        return self._record(iid, "seals", identity)

    def read_result(self, iid, identity):
        return self._record(iid, "results", identity)

    def list_actions_snapshot(self, iid):
        self._identity(iid)
        return self.get("actions")

    def list_seals_snapshot(self, iid):
        self._identity(iid)
        return self.get("seals")

    def list_reviews_snapshot(self, iid):
        self._identity(iid)
        return self.get("reviews")

    def revalidate(self):
        for key, value in self.cache.items():
            if self._load(key) != value:
                raise PreviewError("preview snapshot changed; retry against current retained records")
        # Catch a controller revision racing the final subrecord revalidation.
        if self._load("initiative") != self.cache["initiative"]:
            raise PreviewError("preview initiative changed; retry")
        self._ready()


def _coordinator(snapshot, tmux):
    records = snapshot.get("coordinators")
    generations = [r["generation"] for r in records]
    if not records or len(set(generations)) != len(generations):
        raise PreviewError("preview needs one independently provable current coordinator generation")
    known = []
    for record in sorted(records, key=lambda r: r["generation"]):
        InitiativeStore._check_new_coordinator(known, record)
        known.append(record)
    current = known[-1]
    if current["state"] not in model.COORDINATOR_LIVE_STATES:
        raise PreviewError("current coordinator generation is not live; reclaim before preview")
    state, _detail = anchor_liveness(current["anchor"], tmux)
    if state != "live":
        raise PreviewError(f"coordinator anchor liveness {state}; use its host tmux namespace")
    return current


def _preflight_gates(snapshot, initiative, gates):
    # Reuse the controller's policy, exact-cwd and bounded executable-header
    # checks without launching commands or materializing a repository. Check
    # the aggregate preview deadline between specifications, not only once
    # around an entire plan's potentially large gate list.
    for gate in gates:
        for index, specification in enumerate(gate["commands"]):
            snapshot._ready()
            try:
                preflight_verification_gates(
                    {"declared_gates": [{**gate, "commands": [specification]}]}, initiative,
                )
            except VerificationError as exc:
                raise PreviewError(f"verification gate {gate['node_id']} command {index}: {exc}") from exc
            snapshot._ready()


def assignment_preview(config, initiative_id, node_id, *, salvage_request_id=None, tmux=None):
    """Compose a production-bound preview from IDs only; never execute text."""
    iid = model.canonical_uuid(initiative_id, "initiative_id")
    node_id = model.validate_slug(node_id, "node_id")
    if salvage_request_id is not None:
        model.canonical_uuid(salvage_request_id, "salvage_request_id")
    snapshot = _Snapshot(InitiativeStore(config), iid)
    initiative = snapshot.peek(iid)
    plan = scheduler._active_plan(snapshot, initiative)
    node = snapshot.read_node(iid, node_id)
    planned = next((n for n in plan["nodes"] if n["node_id"] == node_id), None)
    if planned is None or {k: v for k, v in node.items() if k != "state"} != {
        k: v for k, v in planned.items() if k != "state"
    }:
        raise PreviewError("node specification differs from active plan; resolve stale binding")
    if node["type"] in {"verify", "decision"}:
        raise PreviewError("controller-only node has no worker assignment")
    # Native adapters get the same aggregate probe deadline as the snapshot.
    if tmux is None or isinstance(tmux, TmuxAdapter):
        from .observation import BoundedTmux
        tmux = BoundedTmux(tmux or TmuxAdapter(), snapshot.deadline)
    current = _coordinator(snapshot, tmux)
    attempts = [a for a in snapshot.get("attempts") if a["node_id"] == node_id]
    allocated = [a for a in attempts if a["state"] == "allocated"]
    if len(allocated) > 1:
        raise PreviewError("node has multiple allocated attempt reservations; resolve before preview")
    # Match dispatch's sole supersedable reservation: unbound automatic retry
    # bookkeeping. The salvage binding below must still validate, even when
    # rendering the conditional requested (not yet approved) assignment.
    supersedes_retry = (salvage_request_id is not None and bool(allocated)
                        and allocated[0]["action_id"] is None)
    if any(a["state"] in model.ATTEMPT_ACTIVE_STATES or a["state"] == "indeterminate"
           or (a["state"] == "allocated" and not supersedes_retry) for a in attempts):
        raise PreviewError("node already has a reserved/live/indeterminate attempt; resolve before preview")
    # Every worker layout must observe and revalidate retained intents, even
    # before an attempt exists. A missing salvage ID is not a replay match.
    for action in snapshot.list_actions_snapshot(iid):
        outcome = action_outcome(action)
        if salvage_request_id is not None and outcome.get("salvage_request_id") == salvage_request_id:
            raise PreviewError("salvage request already bound to dispatch; inspect that attempt")
        if action["action_class"] != "dispatch-node" or action["state"] in {"completed", "refused"}:
            continue
        payload = outcome.get("payload", {})
        if (not isinstance(payload, dict)
                or (action["state"] in {"received", "validated"} and "node_id" not in payload)):
            raise PreviewError("retained dispatch intent is ambiguous; resolve before preview")
        targets = [frame["node_id"] for frame in (outcome, payload) if "node_id" in frame]
        for target in targets:
            model.validate_slug(target, "dispatch node_id")
        if not targets or len(set(targets)) != 1:
            raise PreviewError("retained dispatch intent is ambiguous; resolve before preview")
        if targets[0] == node_id or (salvage_request_id is not None
                                    and payload.get("salvage_request_id") == salvage_request_id):
            raise PreviewError("retained dispatch intent conflicts with assignment preview; resolve before preview")
    approval = None
    recovery = None
    hypothetical = False
    request_action = None
    if salvage_request_id is not None:
        approval = snapshot.read_approval(iid, salvage_request_id)
        hypothetical = approval["state"] == "requested"
        checker = salvage_request_binding if hypothetical else salvage_dispatch_binding
        approval, base, seal = checker(snapshot, initiative, node, salvage_request_id)
        request_action = next(a for a in snapshot.list_actions_snapshot(iid)
                              if a["action_class"] == "request-salvage"
                              and action_outcome(a).get("request_id") == salvage_request_id)
        if not hypothetical:
            signer = approval.get("decided_by")
            if signer is None or signer["actor_kind"] != "operator" or not any(
                e["type"] == "approval-decided" and salvage_request_id in e["subject_ids"]
                and e["actor_kind"] == signer["actor_kind"] and e["actor_id"] == signer["actor_id"]
                and e["payload"].get("action_class") == "salvage"
                and e["payload"].get("decision") == "approved"
                for e in snapshot.get("events")
            ):
                raise PreviewError("approved salvage lacks retained operator signing evidence")
        recovery = scheduler.salvage_assignment_context(approval, seal)
    else:
        base = scheduler._resolved_attempt_base(snapshot, iid, plan, node)
    gates = scheduler._assignment_gates(initiative, plan, node)
    _preflight_gates(snapshot, initiative, gates)
    scheduler.validate_goal_capacity(config, initiative, plan, nodes=[node], salvage_recovery=recovery,
                                     store=snapshot.store)
    attempt = {"attempt_id": PREVIEW_ATTEMPT_ID, "base": base}
    exact_base = scheduler._exact_base(snapshot, iid, node, attempt)
    repository = scheduler._node_repository(initiative, node)
    for item in base["seal_inputs"]:
        bound_seal = snapshot.read_seal(iid, item["seal_id"])
        if (bound_seal["repository_id"] != repository["repository_id"]
                or bound_seal["outcome"] != item["outcome"]
                or bound_seal["scope_origin"] != item["scope_origin"]):
            raise PreviewError("assignment seal repository/outcome/scope binding changed")
    evidence, findings = scheduler.assignment_evidence(snapshot, iid, base)
    rendered = scheduler.assignment_bytes(initiative, plan, node, attempt, exact_base,
                                          evidence, findings, salvage_recovery=recovery)
    path = snapshot.store.assignment_path(iid, PREVIEW_ATTEMPT_ID)
    goal = scheduler._goal(initiative, node, path)
    result = {
        "contract": CONTRACT, "authority": "none; dispatch must independently revalidate",
        "initiative_id": iid, "node_id": node_id,
        "state_revision": initiative["state_revision"], "initiative_state": initiative["state"],
        "plan": {"revision": plan["revision"], "digest": plan["digest"], "status": plan["status"]},
        "repository": repository, "repository_digest": _hash(repository),
        "base": base, "base_digest": _hash(base), "exact_base_commit": exact_base,
        "seals": [{"seal_id": item["seal_id"],
                   "record_digest": model.record_digest(snapshot.read_seal(iid, item["seal_id"])),
                   "read_only": item["read_only"]} for item in base["seal_inputs"]],
        "coordinator": {"coordinator_id": current["coordinator_id"],
                        "generation": current["generation"], "state": current["state"],
                        "liveness": "live", "record_digest": model.record_digest(current)},
        "approval": None if approval is None else {
            "request_id": approval["request_id"], "state": approval["state"],
            "expires_at": approval["expires_at"], "decided_by": approval.get("decided_by"),
            "record_digest": model.record_digest(approval),
            "binding_digest": approval["binding_digest"],
            "rationale_sha256": hashlib.sha256(approval["rationale"].encode("utf-8")).hexdigest(),
        },
        "request_action": None if request_action is None else {
            "action_id": request_action["action_id"],
            "record_digest": model.record_digest(request_action),
            "payload_digest": request_action["payload_digest"],
        },
        "controller_gates": gates,
        "worker_attestations": {
            "selection": "node goal and acceptance, not all controller gates",
            "goal": node["goal"], "acceptance": node["acceptance"],
            "commands": None, "status": "not machine-encoded in retained node schema",
        },
        "rendering": {
            "kind": "hypothetical-if-approved" if hypothetical else "prospective-assignment",
            "notice": ("NOT APPROVED: approval language below is conditional; no signature exists."
                       if hypothetical else "Preview only; this preview allocated no task or attempt.") + (
                " A later approved dispatch must supersede the unbound automatic retry reservation;"
                " preview leaves it untouched." if supersedes_retry else ""),
            "placeholder_attempt_id": PREVIEW_ATTEMPT_ID,
            "text": rendered.decode("utf-8"), "sha256": hashlib.sha256(rendered).hexdigest(),
        },
        "capacity": {"assignment_bytes": len(rendered),
                     "assignment_limit_bytes": scheduler.MAX_ASSIGNMENT_BYTES,
                     "assignment_remaining_bytes": scheduler.MAX_ASSIGNMENT_BYTES - len(rendered),
                     "future_required_framing_checked": True,
                     "goal_characters": len(goal), "goal_limit_characters": 200,
                     "goal_utf8_bytes": len(goal.encode("utf-8")),
                     "snapshot_bytes": snapshot.size, "snapshot_limit_bytes": MAX_SNAPSHOT_BYTES,
                     "response_limit_bytes": MAX_PREVIEW_BYTES},
    }
    if _coordinator(snapshot, tmux) != current:
        raise PreviewError("coordinator generation changed; retry")
    snapshot.revalidate()
    # Expiry can cross during a slow but coherent read/probe.
    if approval is not None and datetime.now(timezone.utc) >= datetime.fromisoformat(
        approval["expires_at"][:-1] + "+00:00"
    ):
        raise PreviewError("salvage request expired during preview; request a fresh binding")
    result["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    result["snapshot_digest"] = _hash({str(k): v for k, v in snapshot.cache.items()})
    if len(encode_preview(result)) > MAX_PREVIEW_BYTES:
        raise PreviewError("preview response capacity exceeded; shorten required specifications")
    return result


def encode_preview(value):
    # JSON escaping preserves exact command/rationale Unicode after decoding;
    # raw terminal controls cannot leak and alter the display.
    return _bytes(value) + b"\n"
