"""Indexed, read-only action candidates independent of retained graph sampling.

These are recorded requests to inspect, not authorization or proof that every
binding is still executable. Decision commands revalidate the exact subject.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import os
import time

from ..database import ControlDatabase, DATABASE_NAME
from ..registry_backend import selection
from ..runtime import read_policy
from ..store import StoreError
from . import model

CONTRACT = "asha.initiative-current-actions.v1"
HEAD_STATES = ("needs-input", "awaiting-plan-approval", "ready-for-integration", "approved")
QUIET_HEAD_STATES = {"draft", "planning", "running", "paused", "integrated", "partial", "failed", "cancelled", "archived"}
FAMILIES = {"initiatives": ("initiatives", HEAD_STATES), "approvals": ("initiative.approvals", ("requested",))}


def inspection_store(config):
    """Select an active SQL reader without changing restored journal mode."""
    from .sqlite_store import SQLiteInitiativeStore

    if not os.path.lexists(config.control.tasks_dir.parent / DATABASE_NAME):
        return None
    with ControlDatabase(config.control, read_only=True) as db, db.transaction() as c:
        if selection(c, config.control) is None:
            return None
    return SQLiteInitiativeStore(config)


def _epoch(value):
    if not isinstance(value, str):
        raise ValueError("timestamp is unavailable")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.timestamp()


def approval_demand(approval, initiative, *, now=None):
    """Classify retained approvals without mutating or inventing authority."""
    if approval.get("state") != "requested":
        return None
    now = time.time() if now is None else now
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise StoreError("invalid action observation time")
    iid, rid = initiative["initiative_id"], approval.get("request_id")
    action = approval.get("action_class")
    kind = {"review-budget": "review-budget-approval", "salvage": "salvage-approval"}.get(action, "approval-inspection")
    disposition, reason = "pending-review", "Recorded request awaits inspection of its exact binding"
    try:
        expiry = _epoch(approval.get("expires_at"))
    except (ValueError, OverflowError):
        expiry = None
    if approval.get("initiative_id", iid) != iid:
        disposition, reason = "unavailable", "Request belongs to another initiative"
    elif expiry is None:
        disposition, reason = "unavailable", "Request expiry is unavailable"
    elif now >= expiry:
        disposition, reason = "expired", "Request expired; inspect retained work before requesting fresh authority"
    elif initiative["state"] in model.INITIATIVE_TERMINAL_STATES:
        disposition, reason = "inactive", "Initiative is terminal; retained request is not a current approval"
    elif (not approval.get("active_plan_digest")
          or approval.get("active_plan_digest") != (initiative.get("active_plan") or {}).get("digest")):
        disposition, reason = "stale-plan", "Request belongs to a different plan"
    elif action not in {"review-budget", "salvage"}:
        disposition, reason = "unsupported", "Request type has no supported decision handler"
    elif action == "review-budget" and initiative["state"] not in {"running", "paused"}:
        disposition, reason = "deferred", "Review retry requires a running or paused initiative"
    active = disposition == "pending-review"
    command = "approve-review-budget" if action == "review-budget" else "approve-salvage"
    return {
        "kind": kind, "request_id": rid, "disposition": disposition,
        "waiting_on": "keeper" if active else "operator",
        "next_action": "inspect-request" if active else "inspect-retained-request",
        "detail": reason, "certainty": "live" if active else "unknown",
        "resolution": (f"asha initiative {command} {iid} --request {rid}" if active
                       else f"asha initiative show {iid} --json"),
        "binding_checked": False,
    }


def _cursor(family, after):
    if after is None:
        return None
    try:
        if not isinstance(after, str) or len(after) > 4096:
            raise ValueError()
        value = json.loads(after)
        if (not isinstance(value, dict) or set(value) != {"contract", "family", "position"}
                or value["contract"] != CONTRACT or value["family"] != family):
            raise ValueError()
        position = value["position"]
        if (not isinstance(position, list) or len(position) != 4
                or any(not isinstance(v, str) or len(v.encode()) > 1024 for v in position)
                or position[0] not in FAMILIES[family][1]):
            raise ValueError()
        return position
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise StoreError("invalid current-action cursor or cursor family") from exc


def _known_states(c, domain, family, deadline):
    known = model.INITIATIVE_STATES if family == "initiatives" else model.APPROVAL_STATES
    if family == "initiatives" and (set(HEAD_STATES) | QUIET_HEAD_STATES != set(known)
                                    or set(HEAD_STATES) & QUIET_HEAD_STATES):
        raise StoreError("initiative action projection does not cover the lifecycle contract")
    ordered = sorted(known)
    ranges = [("state<?", (ordered[0],))]
    ranges += [("state>? AND state<?", (a, b)) for a, b in zip(ordered, ordered[1:])]
    ranges.append(("state>?", (ordered[-1],)))
    for condition, args in ranges:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if c.execute("SELECT 1 FROM records WHERE domain=? AND " + condition + " LIMIT 1", (domain, *args)).fetchone():
            raise StoreError("unknown current-action state; projection requires inspection")
    return True


def _head_demand(head):
    kind, detail, action = {
        "needs-input": ("operator-decision", "Initiative waits on the operator; inspect the current question", "inspect-question"),
        "awaiting-plan-approval": ("plan-approval", "A proposed plan awaits review", "inspect-plan"),
        "approved": ("activation", "Approved plan awaits activation", "inspect-activation"),
        "ready-for-integration": ("integration", "Candidate work awaits an integration decision", "inspect-candidate"),
    }[head["state"]]
    return {"kind": kind, "detail": detail, "next_action": action, "waiting_on": "keeper",
            "disposition": "pending-review", "certainty": "live", "binding_checked": False,
            "resolution": f"asha initiative show {head['initiative_id']} --json"}


def page(store, *, family="initiatives", limit=50, after=None, now=None, deadline=None):
    """Page durable action candidates under one SQLite snapshot.

    Keyset order is lifecycle priority then stored timestamp/scope/key. State
    can change between pages; the cursor is a position, not a snapshot token.
    Malformed selected records count as unavailable and do not authorize acts.
    """
    from .sqlite_store import SQLiteInitiativeStore
    if not isinstance(store, SQLiteInitiativeStore):
        raise StoreError("indexed attention requires the active SQLite registry backend")
    if family not in FAMILIES or type(limit) is not int or not 1 <= limit <= 100:
        raise StoreError("invalid current-action family or limit")
    position = _cursor(family, after)
    now = time.time() if now is None else now
    deadline = time.monotonic() + 2.0 if deadline is None else deadline
    if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
        raise StoreError("invalid action observation time")
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise StoreError("invalid action observation deadline")
    domain, states = FAMILIES[family]
    rows, selected, unavailable, timed_out = [], [], 0, False
    with ControlDatabase(store.database_config, read_only=True) as db, db.transaction() as c:
        if selection(c, store.database_config) is None:
            raise StoreError("indexed attention requires the active SQLite registry backend")
        policy = read_policy(c)
        timed_out = not _known_states(c, domain, family, deadline)
        for state in () if timed_out else states:
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            if position is not None and states.index(state) < states.index(position[0]):
                continue
            condition, args = "", [domain, state]
            if family == "initiatives":
                condition += " AND scope='registry'"
            if position is not None and state == position[0]:
                condition += " AND (updated_at,scope,record_key)>(?,?,?)"
                args.extend(position[1:])
            selected.extend(dict(r) for r in c.execute(
                "SELECT state,updated_at,scope,record_key FROM records WHERE domain=? AND state=?"
                + condition + " ORDER BY updated_at,scope,record_key LIMIT ?", (*args, limit + 1 - len(selected))))
            if len(selected) > limit:
                break
        consumed = []
        for candidate in selected[:limit]:
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            consumed.append(candidate)
            try:
                key, scope = candidate["record_key"], candidate["scope"]
                iid = key if family == "initiatives" else scope
                model.canonical_uuid(iid)
                head = store._head(c, iid)["value"]
                if family == "initiatives":
                    value, identity = head, "initiative:" + iid + ":" + str(head["state_revision"])
                else:
                    if not key.endswith(".json"):
                        raise StoreError("invalid approval record key")
                    rid = model.canonical_uuid(key[:-5])
                    value = store._record(c, iid, "approvals", key, model.validate_approval)["value"]
                    if value["request_id"] != rid:
                        raise StoreError("approval record identity mismatch")
                    identity = "approval:" + iid + ":" + rid
                if (value["state"] != candidate["state"] or value["updated_at"] != candidate["updated_at"]):
                    raise StoreError("current-action metadata disagrees with its index")
                demand = _head_demand(head) if family == "initiatives" else approval_demand(value, head, now=now)
                rows.append({**demand, "request_key": identity, "initiative_id": iid,
                             "slug": head["slug"], "label": head["label"][:160],
                             "digest": model.record_digest(value), "head_digest": model.record_digest(head),
                             "digest_kind": "initiative-state" if family == "initiatives" else "approval-record",
                             "updated_at": value["updated_at"], "age_seconds": max(0, now - _epoch(value["created_at"])),
                             "state_revision": head["state_revision"]})
            except (ValueError, KeyError, TypeError, OverflowError):
                unavailable += 1
    more = timed_out or len(selected) > len(consumed)
    cursor = after if more else None
    if more and consumed:
        last = consumed[-1]
        cursor = json.dumps({"contract": CONTRACT, "family": family,
                             "position": [last[k] for k in ("state", "updated_at", "scope", "record_key")]}, separators=(",", ":"))
    return {"contract": CONTRACT, "family": family, "rows": rows, "next": cursor,
            "complete": not (more or unavailable), "unavailable_records": unavailable,
            "deadline_exceeded": timed_out, "observed_at": now, "scanned": len(consumed),
            "retry": timed_out and not consumed, "admission": policy,
            "completeness_scope": "this page only; accumulate unavailable records across pages",
            "order": "lifecycle priority, then oldest updated first",
            "snapshot": "one page; records can change before the next page", "bindings_checked": False}


def overview(config, *, limit=50, deadline=None):
    """Independent candidate pages for chair and Control, with qualified counts."""
    store = inspection_store(config)
    if store is None:
        return {"initialized": False, "summary": "Indexed actions require the SQLite registry backend"}
    deadline = time.monotonic() + 2.0 if deadline is None else deadline
    pages = {family: page(store, family=family, limit=limit, deadline=deadline) for family in FAMILIES}
    counts = {family: sum(row["disposition"] == "pending-review" for row in value["rows"])
              for family, value in pages.items()}
    retained = sum(row["disposition"] != "pending-review" for value in pages.values() for row in value["rows"])
    complete = all(value["complete"] for value in pages.values())
    summary = f"{counts['initiatives']} initiative decisions, {counts['approvals']} approval requests"
    if retained:
        summary += f", {retained} retained requests"
    if not complete:
        summary = "Observed: " + summary + "; partial pages"
    unknown = [family for family, value in pages.items() if value["retry"]]
    if unknown:
        summary += "; unread: " + ", ".join(unknown)
    return {"initialized": True, "summary": summary, "counts": counts, "retained": retained,
            "complete": complete, "count_kind": "observed" if complete else "lower-bound",
            "pages": pages, "snapshot": "independent bounded page snapshots", "bindings_checked": False}
