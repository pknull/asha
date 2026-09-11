"""Indexed, read-only current work; historical events are never loaded here."""
from __future__ import annotations

from collections import Counter
import json
import math
import time

from .provider_recovery import recovery
from .runtime import read_policy
from .session_store import MAX_RUNNING_TURNS, identifier
from .store import StoreError

CONTRACT = "asha.session-current.v1"
# Operational priority is also the keyset order; never compare state names.
SESSION_STATES = ("waiting-input", "failed", "uncertain", "budget-exhausted", "running", "queued", "idle")
DELIVERY_STATES = ("queued", "uncertain")
FAMILIES = {
    "sessions": ("managed_sessions", SESSION_STATES, ("created_at", "session_id")),
    "requests": ("session_requests", ("pending",), ("created_at", "request_id")),
    "deliveries": ("session_messages", DELIVERY_STATES, ("session_id", "sequence")),
}
KNOWN_STATES = {
    "sessions": (*SESSION_STATES, "stopped"),
    "requests": ("pending", "answered", "cancelled"),
    "deliveries": (*DELIVERY_STATES, "submitted", "consumed", "cancelled"),
}


def _states_known(c, kind, deadline):
    """Reject unknown lifecycle values using bounded index range probes.

    A NOT IN or DISTINCT scan would visit all historical receipts. There are
    only fixed, small gaps between the known state names; seek into each gap.
    """
    states = sorted(KNOWN_STATES[kind])
    ranges = [("state<?", (states[0],))]
    ranges += [("state>? AND state<?", (a, b)) for a, b in zip(states, states[1:])]
    ranges.append(("state>?", (states[-1],)))
    for condition, args in ranges:
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if c.execute(f"SELECT state FROM {FAMILIES[kind][0]} WHERE {condition} LIMIT 1", args).fetchone():
            raise StoreError("unknown current-work state; update the projection contract before reading")
    return True


def _cursor(kind, after):
    if after is None:
        return None
    try:
        if not isinstance(after, str) or len(after) > 1024:
            raise ValueError()
        value = json.loads(after)
        if (not isinstance(value, dict) or set(value) != {"contract", "kind", "position"}
                or value["contract"] != CONTRACT or value["kind"] != kind):
            raise ValueError()
        position = value["position"]
        _, states, columns = FAMILIES[kind]
        if not isinstance(position, list) or len(position) != 1 + len(columns) or position[0] not in states:
            raise ValueError()
        number = position[1]
        if kind == "deliveries":
            identifier(position[1])
            number = position[2]
            if type(number) is not int or not 0 <= number <= 9223372036854775807:
                raise ValueError()
        else:
            if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                raise ValueError()
            identifier(position[2])
        return position
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise StoreError("invalid current-work cursor or cursor family") from exc


def _waiting(row, kind, policy, capacity_full=False, pending_request=False):
    state = row["state"]
    if row.get("stop_requested") or row.get("session_state") == "stopped":
        return "operator", "inspect-session", "Session stop requested or recorded; no new dispatch"
    if state in {"failed", "uncertain", "budget-exhausted"} or row.get("session_state") in {"failed", "uncertain", "budget-exhausted"}:
        return "operator", "inspect-recovery", "Explicit recovery requires inspection of retained work"
    if kind == "requests":
        return "keeper", "answer" if row["kind"] in {"clarification", "native-clarification"} else "review-permission", "An exact request awaits a decision"
    if pending_request:
        return "keeper", "inspect-requests", "Session has a pending question or native permission request"
    if row.get("session_state") == "waiting-input":
        return "keeper", "inspect-requests", "Session is waiting for input"
    if state in {"running", "submitted", "consumed"} or row.get("session_state") == "running":
        return "agent", "observe", "Waiting for a recorded outcome; state alone does not prove a live process"
    if policy["mode"] != "running":
        return "operator", "inspect-runtime", "Runtime admission is " + policy["mode"]
    if state == "idle" and not row.get("has_queued_input"):
        return "operator", "send", "No running turn is recorded"
    if capacity_full:
        return "capacity", "observe", f"Input is retained; the limit of {MAX_RUNNING_TURNS} managed turns is occupied"
    return "supervisor", "observe", "Input is retained and awaiting dispatch"


def _page(store, c, kind, *, limit, after, now, policy, deadline=None):
    store.db._limit(limit)
    if kind not in FAMILIES:
        raise StoreError("unknown current-work family")
    position = _cursor(kind, after)
    table, states, columns = FAMILIES[kind]
    # Select metadata only: a large message body or native tool input cannot
    # inflate this view, nor can corrupt/expired display history poison it.
    selection = {
        "sessions": "session_id,initiative_id,harness,cwd,state,generation,stop_requested,turns,max_turns,created_at,updated_at",
        "requests": "request_id,session_id,turn_id,kind,digest,state,created_at,substr(question,1,160) AS question",
        "deliveries": "sequence,message_id,session_id,turn_id,digest,state,created_at",
    }[kind]
    rows = []
    timed_out = not _states_known(c, kind, deadline)
    for state in () if timed_out else states:
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        if position is not None and states.index(state) < states.index(position[0]):
            continue
        condition, args = "", [state]
        if position is not None and state == position[0]:
            key = columns[0] if len(columns) == 1 else "(" + ",".join(columns) + ")"
            placeholders = "?" if len(columns) == 1 else "(" + ",".join("?" for _ in columns) + ")"
            condition = f" AND {key}>{placeholders}"
            args.extend(position[1:])
        rows.extend(dict(r) for r in c.execute(
            f"SELECT {selection} FROM {table} WHERE state=?{condition} ORDER BY {','.join(columns)} LIMIT ?",
            (*args, limit + 1 - len(rows))))
        if len(rows) > limit:
            break
    complete = len(rows) <= limit and not timed_out
    rows = rows[:limit]
    # Same snapshot as the rows; bounded index lookup never scans turn history.
    capacity_full = bool(rows) and len(c.execute(
        "SELECT 1 FROM session_turns WHERE state='running' LIMIT ?",
        (MAX_RUNNING_TURNS,)).fetchall()) == MAX_RUNNING_TURNS
    projected = []
    for row in rows:
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            complete = False
            break
        session = row if kind == "sessions" else c.execute(
            "SELECT cwd,initiative_id,generation,state,stop_requested FROM managed_sessions WHERE session_id=?",
            (row["session_id"],)).fetchone()
        if session is None:
            raise StoreError("current work refers to a missing session")
        row.update(cwd=session["cwd"], initiative_id=session["initiative_id"],
                   generation=session["generation"], session_state=session["state"],
                   stop_requested=session["stop_requested"])
        if kind == "sessions":
            row["has_queued_input"] = c.execute(
                "SELECT 1 FROM session_messages WHERE session_id=? AND state='queued' LIMIT 1",
                (row["session_id"],)).fetchone() is not None
            active = c.execute("""SELECT t.turn_id,t.message_id,m.state AS delivery_state,t.started_at
                FROM session_turns t JOIN session_messages m ON m.message_id=t.message_id
                WHERE t.state='running' AND t.session_id=? LIMIT 1""", (row["session_id"],)).fetchone()
            row["active_turn"] = dict(active) if active else None
        retained = recovery(c, row["session_id"]) if kind == "sessions" and row["state"] in {"failed", "uncertain", "budget-exhausted"} else None
        pending_request = kind != "requests" and c.execute(
            "SELECT 1 FROM session_requests WHERE session_id=? AND state='pending' LIMIT 1",
            (row["session_id"],)).fetchone() is not None
        actor, action, reason = _waiting(row, kind, policy, capacity_full, pending_request)
        row.update(waiting_on=actor, next_action=action, reason=reason,
                   age_seconds=max(0, now - row["created_at"]))
        if retained:
            row.update(recovery_category=retained["category"], retry_not_before=retained["retry_not_before"],
                       recovery_reason=retained["reason"][:1000], recovery_condition=retained["retry_condition"][:2000])
            if row["state"] in {"failed", "uncertain"} and retained["category"] in {"quota", "provider"}:
                row["waiting_on"] = "provider"
        projected.append(row)
    cursor = (json.dumps({"contract": CONTRACT, "kind": kind,
                         "position": [projected[-1]["state"], *(projected[-1][col] for col in columns)]}, separators=(",", ":"))
              if projected else after)
    return {"contract": CONTRACT, "kind": kind, "rows": projected, "complete": complete,
            "next_cursor": cursor, "observed_at": now, "admission": policy,
            "deadline_exceeded": timed_out,
            "count_kind": "exact" if complete and after is None else "lower-bound",
            "snapshot": "one read transaction; mutable current state between pages; refresh from start",
            "limit": limit}


def page(store, *, kind="sessions", limit=100, after=None, deadline=None):
    """Read current work; internal deadline is absolute time.monotonic()."""
    with store.db.transaction() as c:
        return _page(store, c, kind, limit=limit, after=after, now=time.time(), policy=read_policy(c), deadline=deadline)


def summary(store, *, limit=100, deadline=None):
    """Read all families with one snapshot and an optional monotonic deadline."""
    with store.db.transaction() as c:
        now, policy = time.time(), read_policy(c)
        pages = {kind: _page(store, c, kind, limit=limit, after=None, now=now, policy=policy, deadline=deadline) for kind in FAMILIES}
    counts = dict(Counter(row["state"] for row in pages["sessions"]["rows"]))
    requests = Counter(row["kind"] for row in pages["requests"]["rows"])
    recovery_counts = dict(Counter(row["recovery_category"] for row in pages["sessions"]["rows"]
                                   if row.get("recovery_category") and row["state"] in {"failed", "uncertain"}))
    queued = sum(row["state"] == "queued" for row in pages["deliveries"]["rows"])
    active = sum(counts.get(state, 0) for state in ("queued", "idle", "running", "waiting-input"))
    parked = sum(counts.get(state, 0) for state in ("failed", "uncertain", "budget-exhausted"))
    complete = all(p["complete"] for p in pages.values())
    questions = requests["clarification"] + requests["native-clarification"]
    text = (f"{active} managed sessions, {questions} questions, "
            f"{requests['native-permission']} native permissions, {queued} queued, {parked} need recovery")
    waiting_capacity = sum(row['waiting_on'] == 'capacity' for row in pages['sessions']['rows'])
    if waiting_capacity:
        text += f"; {waiting_capacity} waiting for managed turn capacity (limit {MAX_RUNNING_TURNS})"
    if recovery_counts.get("quota"):
        text += f" ({recovery_counts['quota']} quota-blocked)"
    if not complete:
        text = "Observed at least: " + text + "; current-work pages capped"
    return {"initialized": True, "counts": counts, "questions": questions,
            "permissions": requests["native-permission"], "queued": queued, "recovery_counts": recovery_counts,
            "complete": complete, "count_kind": "exact" if complete else "lower-bound", "pages": pages,
            "observed_at": now, "admission": policy, "summary": text + f"; runtime {policy['mode']}"}
