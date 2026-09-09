"""Explicit one-attempt authority for retrying an exhausted exact-seal review."""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from .model import (
    APPROVAL_CONTRACT, EVIDENCE_CONTRACT, OPERATOR_SURFACE_ACTOR_IDS,
    record_digest, validate_approval, validate_evidence,
)
from .store import StoreError


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_encoded(value).encode()).hexdigest()


def _id(action_id, purpose):
    return str(uuid.uuid5(uuid.UUID(action_id), "review-budget:" + purpose))


def _has_decision(store, iid, request_id, decision, *, actor_kind, actor_id, events=None):
    return any(e['type'] == 'approval-decided' and request_id in e['subject_ids']
               and isinstance(e['payload'], dict)
               and e['payload'].get('action_class') == 'review-budget'
               and e['payload'].get('decision') == decision
               and e['actor_kind'] == actor_kind and e['actor_id'] == actor_id
               for e in (store.list_events_snapshot(iid) if events is None else events))


def _binding(store, initiative_id, node_id, review_id, *, allocation_id=None):
    from .actions import ActionRefused
    from .review import review_target
    from .scheduler import _gate_rerun_attempt_ids

    initiative = store.peek(initiative_id)
    if initiative["state"] not in {"running", "paused"} or not initiative["active_plan"]:
        raise ActionRefused("review budget requires a running or paused active initiative")
    plan = store.read_plan(initiative_id, initiative["active_plan"]["revision"])
    node = store.read_node(initiative_id, node_id)
    if node["type"] != "review" or node["state"] not in {"failed", "ready", "blocked"}:
        raise ActionRefused("review budget requires an exhausted inactive review node")
    review = store.read_review(initiative_id, review_id)
    if review["node_id"] != node_id or review["state"] not in {"failed", "indeterminate"}:
        raise ActionRefused("review budget cannot retry an accepted verdict or another node")
    _, target = review_target(store, initiative, plan, node)
    if review["target"] != target:
        raise ActionRefused("review budget target is no longer the current exact seal")
    attempts = store.list_attempts_snapshot(initiative_id)
    allocation = [a for a in attempts if a["attempt_id"] == allocation_id]
    if allocation and (len(allocation) != 1 or allocation[0]["state"] != "allocated" or allocation[0]["node_id"] != node_id):
        raise ActionRefused("review budget attempt is already spent")
    attempts = [a for a in attempts if a["attempt_id"] != allocation_id]
    node_attempts = sorted((a for a in attempts if a["node_id"] == node_id), key=lambda a: a["ordinal"])
    if (not node_attempts or node_attempts[-1]["attempt_id"] != review["attempt_id"]
            or node_attempts[-1]["state"] != "failed-no-artifact"):
        raise ActionRefused("review budget requires the latest settled failed review attempt")
    gate_ids, _ = _gate_rerun_attempt_ids(store, initiative_id, node_id, attempts)
    ordinary = sum(a["attempt_id"] not in gate_ids for a in node_attempts)
    caps = {key: min(initiative["limits"][key], plan["limits"][key])
            for key in ("max_attempts_per_node", "max_total_tasks")}
    if ordinary < caps["max_attempts_per_node"] and len(attempts) < caps["max_total_tasks"]:
        raise ActionRefused("review still has ordinary budget; no amendment is needed")
    return {
        "contract": "asha.review-budget-binding.v1", "initiative_id": initiative_id,
        "node_id": node_id, "review_id": review_id, "review_digest": record_digest(review),
        "target": target, "node_specification": {k: v for k, v in node.items() if k != "state"},
        "attempt_digests": [record_digest(a) for a in node_attempts],
        "limits": {"initiative": initiative["limits"], "plan": plan["limits"]},
        "additional_attempts": 1,
    }


def request(store, action, payload):
    """Request authority; never sign it or change a budget during a request."""
    from .actions import ActionRefused, append_event

    iid = action["initiative_id"]
    binding = _binding(store, iid, payload["node_id"], payload["review_id"])
    request_id = _id(action["action_id"], "approval")
    evidence_id = _id(action["action_id"], "binding")
    for approval in store.list_approvals_snapshot(iid):
        if (approval["action_class"] == "review-budget" and approval["state"] in {"requested", "approved"}
                and approval["binding_digest"] == _digest(binding) and approval["request_id"] != request_id):
            raise ActionRefused("review budget already requested: " + approval["request_id"])
    evidence = next((e for e in store.list_evidence_snapshot(iid) if e["evidence_id"] == evidence_id), None)
    if evidence is None:
        evidence = validate_evidence({
            "contract": EVIDENCE_CONTRACT, "evidence_id": evidence_id, "initiative_id": iid,
            "kind": "review-budget-binding", "subject_id": request_id,
            "digest": _digest(binding), "summary": _encoded(binding), "recorded_at": _now(),
        })
        store.save_evidence(iid, evidence)
    elif evidence["digest"] != _digest(binding) or evidence["summary"] != _encoded(binding):
        raise ActionRefused("review budget request changed during recovery")
    approval = next((a for a in store.list_approvals_snapshot(iid) if a["request_id"] == request_id), None)
    if approval is None:
        at = _now()
        approval = validate_approval({
            "contract": APPROVAL_CONTRACT, "request_id": request_id, "initiative_id": iid,
            "action_class": "review-budget", "binding_digest": evidence["digest"],
            "active_plan_digest": action["active_plan_digest"],
            "expected_state_revision": action["expected_state_revision"],
            "actor_kind": "operator", "actor_id": action["actor_id"],
            "requested_by": {"actor_kind": action["actor_kind"], "actor_id": action["actor_id"]},
            "state": "requested", "rationale": payload["reason"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "created_at": at, "updated_at": at,
        })
        store.save_approval(iid, approval)
    if not any(e["type"] == "approval-requested" and request_id in e["subject_ids"]
               for e in store.list_events_snapshot(iid)):
        append_event(store, iid, "approval-requested", [request_id, payload["node_id"], payload["review_id"]],
                     {"action_class": "review-budget", "binding_digest": evidence["digest"], "additional_attempts": 1},
                     actor_kind=action["actor_kind"], actor_id=action["actor_id"])
    return {"status": "review-budget-requested", "request_id": request_id,
            "binding_evidence_id": evidence_id, "additional_attempts": 1}


def validate_request(store, iid, request_id, *, allow_allocation=False):
    """Recompute the immutable review binding before signing or use."""
    from .actions import ActionRefused, action_outcome

    approval = store.read_approval(iid, request_id)
    if approval["action_class"] != "review-budget":
        raise ActionRefused("approval is not a review budget request")
    actions = [a for a in store.list_actions_snapshot(iid)
               if a["action_class"] == "request-review-budget" and a["state"] == "completed"
               and action_outcome(a).get("request_id") == request_id]
    if len(actions) != 1:
        raise ActionRefused("review budget needs one completed request action")
    action = actions[0]
    outcome = action_outcome(action)
    if request_id != _id(action["action_id"], "approval") or outcome.get("binding_evidence_id") != _id(action["action_id"], "binding"):
        raise ActionRefused("review budget request identity changed")
    evidence = store.read_evidence(iid, outcome["binding_evidence_id"])
    payload = outcome["payload"]
    binding = _binding(store, iid, payload["node_id"], payload["review_id"],
                       allocation_id=_id(request_id, "attempt") if allow_allocation else None)
    if (evidence["kind"] != "review-budget-binding" or evidence["subject_id"] != request_id
            or evidence["summary"] != _encoded(binding) or evidence["digest"] != _digest(binding)
            or approval["binding_digest"] != evidence["digest"]
            or approval["rationale"] != payload["reason"]
            or approval["active_plan_digest"] != binding["target"]["active_plan_digest"]
            or approval["expected_state_revision"] != action["expected_state_revision"]):
        raise ActionRefused("review budget binding changed; inspect and request fresh authority")
    return approval, binding


def approve(store, iid, request_id, *, actor_id="cli"):
    """Sign exactly one extra review attempt from an operator surface."""
    from .actions import ActionRefused, append_event

    if actor_id not in OPERATOR_SURFACE_ACTOR_IDS:
        raise ActionRefused("review budget must be approved from an operator surface")
    with store.transaction_lock(iid):
        approval, binding = validate_request(store, iid, request_id)
        if approval["state"] not in {"requested", "approved"}:
            raise ActionRefused("review budget request is no longer approvable")
        if datetime.now(timezone.utc) >= datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00")):
            raise ActionRefused("review budget request expired")
        if approval["state"] == "requested":
            changed = copy.deepcopy(approval)
            changed.update(state="approved", decided_by={"actor_kind": "operator", "actor_id": actor_id}, updated_at=_now())
            store.save_approval(iid, changed, expected_digest=record_digest(approval))
            approval = changed
        if not _has_decision(store, iid, request_id, 'approved', actor_kind='operator',
                             actor_id=approval['decided_by']['actor_id']):
            append_event(store, iid, "approval-decided", [request_id, binding["node_id"]],
                         {"action_class": "review-budget", "decision": "approved", "additional_attempts": 1},
                     actor_kind="operator", actor_id=approval["decided_by"]["actor_id"])
        store.reopen_failed_review(iid, request_id)
        return approval


def authority(store, iid, request_id):
    """Available authority, including recovery of its one allocated attempt."""
    from .actions import ActionRefused
    approval, binding = validate_request(store, iid, request_id, allow_allocation=True)
    signer = approval.get("decided_by", {})
    if (approval["state"] not in {"approved", "consumed"} or signer.get("actor_kind") != "operator"
            or signer.get("actor_id") not in OPERATOR_SURFACE_ACTOR_IDS):
        raise ActionRefused("review budget has no operator signature")
    if not _has_decision(store, iid, request_id, 'approved', actor_kind='operator', actor_id=signer['actor_id']):
        raise ActionRefused("review budget signing event is missing")
    attempt_id = _id(request_id, "attempt")
    allocated = next((a for a in store.list_attempts_snapshot(iid) if a["attempt_id"] == attempt_id), None)
    if approval["state"] == "consumed" and allocated is None:
        raise ActionRefused("consumed review budget has no exact reservation")
    if allocated is None and datetime.now(timezone.utc) >= datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00")):
        raise ActionRefused("review budget request expired")
    return {"approval": approval, "binding": binding, "attempt_id": attempt_id,
            "task_id": _id(request_id, "task")}


def available(store, iid, node_id):
    from .actions import ActionRefused
    matches = []
    for approval in store.list_approvals_snapshot(iid):
        if approval["action_class"] != "review-budget" or approval["state"] not in {"approved", "consumed"}:
            continue
        try:
            grant = authority(store, iid, approval["request_id"])
        except ActionRefused:
            continue  # Changed or spent bindings do not release readiness.
        if grant["binding"]["node_id"] == node_id:
            matches.append(grant)
    if len(matches) > 1:
        raise StoreError("multiple review budget authorities for one node")
    return matches[0] if matches else None


def consume(store, iid, grant):
    from .actions import ActionRefused, append_event
    request_id = grant["approval"]["request_id"]
    current = authority(store, iid, request_id)
    attempt = store.read_attempt(iid, current["attempt_id"])
    if attempt["state"] != "allocated" or attempt["task_id"] != current["task_id"]:
        raise ActionRefused("review budget needs its exact allocated task")
    approval = current["approval"]
    if approval["state"] == "approved":
        changed = copy.deepcopy(approval)
        changed.update(state="consumed", updated_at=_now())
        store.save_approval(iid, changed, expected_digest=record_digest(approval))
    if not any(e["type"] == "approval-consumed" and request_id in e["subject_ids"]
               for e in store.list_events_snapshot(iid)):
        append_event(store, iid, "approval-consumed", [request_id, current["attempt_id"]],
                     {"action_class": "review-budget", "attempt_id": current["attempt_id"]},
                     actor_kind="controller", actor_id="scheduler")


def reconcile_requests(store, iid):
    """Retire obsolete asks so stale approvals do not remain as needs-input."""
    from .actions import ActionRefused, action_outcome, append_event
    completed = {action_outcome(a).get("request_id") for a in store.list_actions_snapshot(iid)
                 if a["action_class"] == "request-review-budget" and a["state"] == "completed"}
    terminal_events = None
    for approval in store.list_approvals_snapshot(iid):
        rid = approval["request_id"]
        if approval['action_class'] != 'review-budget':
            continue
        if approval['state'] in {'cancelled', 'revoked-before-use', 'expired'}:
            # Recovery of a committed retirement, not fresh authority. A crash
            # between record and journal must not leave a permanent missing edge.
            if terminal_events is None:
                terminal_events = store.list_events_snapshot(iid)
            if not _has_decision(store, iid, rid, approval['state'], actor_kind='controller',
                                 actor_id='action-reconciler', events=terminal_events):
                append_event(store, iid, 'approval-decided', [rid], {
                    'action_class': 'review-budget', 'decision': approval['state'],
                    'reason': 'Recovered the retained terminal review-budget decision'},
                    actor_kind='controller', actor_id='action-reconciler')
            continue
        if approval['state'] not in {'requested', 'approved'}:
            continue
        expired = datetime.now(timezone.utc) >= datetime.fromisoformat(approval['expires_at'].replace('Z', '+00:00'))
        if rid not in completed:
            # A request action may still be recovering. Only its existing expiry
            # can retire an unsigned orphan; never infer a successful request.
            if approval['state'] != 'requested' or not expired:
                continue
            changed = copy.deepcopy(approval)
            changed.update(state='expired', updated_at=_now())
            store.save_approval(iid, changed, expected_digest=record_digest(approval))
            append_event(store, iid, 'approval-decided', [rid], {
                'action_class': 'review-budget', 'decision': 'expired', 'reason': 'review budget request expired'},
                actor_kind='controller', actor_id='action-reconciler')
            continue
        reason = None
        target = "cancelled" if approval["state"] == "requested" else "revoked-before-use"
        try:
            validate_request(store, iid, rid, allow_allocation=True)
        except ActionRefused as exc:
            reason = str(exc)
        allocated = any(a["attempt_id"] == _id(rid, "attempt") and a["state"] == "allocated"
                        for a in store.list_attempts_snapshot(iid))
        if reason is None and not allocated and expired:
            reason, target = "review budget request expired", "expired"
        if reason is not None:
            changed = copy.deepcopy(approval)
            changed.update(state=target, updated_at=_now())
            store.save_approval(iid, changed, expected_digest=record_digest(approval))
            append_event(store, iid, "approval-decided", [rid],
                         {"action_class": "review-budget", "decision": target, "reason": reason},
                         actor_kind="controller", actor_id="action-reconciler")
