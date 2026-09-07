"""Bounded, read-only current activity. No plans, criteria or history assembly."""
from __future__ import annotations

import json
import time
from typing import Any

from ..harness import verify_process
from ..rooms import RoomStore, _owned_state
from ..store import SnapshotBudget, TaskStore, StoreError
from ..tmux import TmuxAdapter, TmuxError
from .model import (INITIATIVE_TERMINAL_STATES, validate_approval, validate_coordinator,
                    validate_message)
from .messages import terminal_safe, _receipt, _same_address
from .store import InitiativeStore

INVENTORY_CONTRACT = "asha.orchestration-current-activity.v1"
MAX_ROWS = 50
MAX_SCANNED = 256
MAX_JSON_BYTES = 64 * 1024
DEADLINE_SECONDS = 2.0


class BoundedTmux(TmuxAdapter):
    """Reuse the real adapter parser with a shared external-probe deadline."""
    def __init__(self, source, deadline):
        super().__init__(executable=source.executable, socket=source.socket,
                         config_file=source.config_file, runner=source.runner)
        self.deadline = deadline

    def _capture_bytes(self, executable, args, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TmuxError("activity observation deadline reached")
        kwargs["deadline_seconds"] = min(remaining, kwargs.get("deadline_seconds", remaining))
        kwargs["limit"] = min(MAX_JSON_BYTES, kwargs.get("limit", MAX_JSON_BYTES))
        result = super()._capture_bytes(executable, args, **kwargs)
        # The normal inventory maps some connection failures to an empty
        # server. Observation must not call inaccessible evidence 'no tasks'.
        if result[0] != 0:
            raise TmuxError("tmux activity evidence unavailable")
        return result


def current_activity(config, *, tmux=None, rows=MAX_ROWS, scanned=MAX_SCANNED,
                     seconds=DEADLINE_SECONDS, byte_limit=MAX_JSON_BYTES) -> dict[str, Any]:
    if (type(rows) is not int or not 1 <= rows <= MAX_ROWS
            or type(scanned) is not int or not 1 <= scanned <= MAX_SCANNED
            or not 0 < seconds <= DEADLINE_SECONDS
            or type(byte_limit) is not int or not 4096 <= byte_limit <= MAX_JSON_BYTES):
        raise ValueError("activity limits may only narrow the default bounds")
    deadline = time.monotonic() + seconds
    budgets = {key: SnapshotBudget(deadline=deadline, limit=scanned) for key in (
        "rooms", "tasks", "initiatives", "decisions", "messages", "coordinators",
    )}
    counts = {key: 0 for key in budgets}
    result = {"contract": INVENTORY_CONTRACT, "rows": [], "sources": {},
              "limits": {"rows": rows, "scanned_per_source": scanned,
                         "json_bytes": byte_limit, "seconds": seconds},
              "truncated": False, "complete": True,
              "snapshot": "non-atomic; counts are observed lower bounds, not exact totals"}

    def add(source, payload):
        counts[source] += 1
        if len(result["rows"]) < rows:
            result["rows"].append({"source": source, **payload})
        else:
            result["truncated"] = True

    def ready(source):
        # Check wall time without charging a second entry for processing an
        # already scanned record (including the exact last permitted entry).
        if time.monotonic() >= deadline:
            budgets[source].truncated = True
            return False
        return True

    store = InitiativeStore(config)
    room_records = RoomStore(config.control).bounded_snapshots(budgets["rooms"])
    task_records = TaskStore(config.control).bounded_snapshots(budgets["tasks"])
    heads = store.bounded_snapshots(budgets["initiatives"])
    live_tmux = None
    try:
        source = tmux or TmuxAdapter()
        live_tmux = BoundedTmux(source, deadline).inventory()
    except (OSError, ValueError):
        # Even an empty retained registry cannot assert absence of live
        # managed processes when the server is inaccessible.
        budgets["tasks"].unavailable += 1
        budgets["rooms"].unavailable += 1
    for room in room_records:
        if not ready("rooms"):
            break
        if room["lifecycle"] == "ended":
            continue
        state = "unavailable"
        if live_tmux is not None:
            state, _ = _owned_state(room, live_tmux)
        if state in {"unavailable", "mismatch"}:
            budgets["rooms"].unavailable += 1
        add("rooms", {"room_id": room["room_id"], "name": room["name"],
                      "project_name": room["project_name"], "status": state})
    for task in task_records:
        if not ready("tasks"):
            break
        live_runs = []
        for run in task["runs"]:
            if not ready("tasks"):
                break
            if run["pid"] is None or run["process_start_identity"] is None:
                if run["state"] not in {"exited", "failed"}:
                    budgets["tasks"].unavailable += 1
                continue
            try:
                if live_tmux is None:
                    continue
                facts = live_tmux.pane_facts(run["pane_id"])
                session = task["tmux"]["session"]
                owned = (facts.session == session
                         and live_tmux.session_option(session, "@asha_task_id") == task["task_id"]
                         and live_tmux.pane_option(run["pane_id"], "@asha_run_id") == run["run_id"])
                if not owned:
                    raise StoreError("task ownership mismatch")
                if (not facts.dead and facts.pane_pid == run["pid"]
                        and verify_process(run["pid"], run["process_start_identity"])):
                    live_runs.append(run["run_id"])
                elif not facts.dead:
                    budgets["tasks"].unavailable += 1
            except (OSError, ValueError, StoreError):
                # An inaccessible namespace/foreign pane is not a live task,
                # nor evidence of a stopped task.
                budgets["tasks"].unavailable += 1
        if live_runs:
            add("tasks", {"task_id": task["task_id"], "label": task["label"],
                          "status": "live", "run_ids": live_runs})
    for head in heads:
        iid = head["initiative_id"]
        if not ready("initiatives"):
            break
        if head["state"] not in INITIATIVE_TERMINAL_STATES and head["state"] != "archived":
            add("initiatives", {"initiative_id": iid, "slug": head["slug"],
                                "label": head["label"], "state": head["state"]})
        if head["state"] == "needs-input":
            add("decisions", {"initiative_id": iid, "state": "needs-input"})
        for decision in store.bounded_records(iid, "approvals", validate_approval,
                                               "request_id", budgets["decisions"]):
            if not ready("decisions"):
                break
            if decision["state"] == "requested":
                add("decisions", {"initiative_id": iid, "request_id": decision["request_id"],
                                  "state": "pending"})
        prior = budgets["coordinators"].unavailable
        coords = store.bounded_records(iid, "coordinators", validate_coordinator,
                                       "coordinator_id", budgets["coordinators"])
        coord_complete = (not budgets["coordinators"].truncated
                          and budgets["coordinators"].unavailable == prior)
        current = max(coords, key=lambda r: r["generation"], default=None)
        for message in store.bounded_records(iid, "messages", validate_message,
                                             "message_id", budgets["messages"]):
            if not ready("messages"):
                break
            try:
                if _receipt(store, message, "message-acks") is not None:
                    continue
                observed = _receipt(store, message, "message-observations")
                address_status = ("unavailable" if not coord_complete else
                                  "current" if _same_address(message["recipient"], current) else "stale-address")
                add("messages", {"initiative_id": iid, "message_id": message["message_id"],
                                 "coordinator_id": message["recipient"]["coordinator_id"],
                                 "generation": message["recipient"]["generation"],
                                 "status": "observed" if observed else "persisted",
                                 "address_status": address_status})
            except (OSError, ValueError, StoreError):
                budgets["messages"].unavailable += 1
    # Nested sources cannot claim exhaustive coverage when head enumeration
    # was incomplete or the loop ran out of time before visiting every head.
    if not budgets["initiatives"].summary()["complete"]:
        for source in ("decisions", "messages", "coordinators"):
            budgets[source].truncated = True
    result["sources"] = {key: {**budget.summary(), "observed_count": counts[key],
                                "count_kind": "lower-bound"} for key, budget in budgets.items()}
    result["complete"] = not result["truncated"] and all(b.summary()["complete"] for b in budgets.values())
    result = terminal_safe(result)
    # Bound the actual serialized JSON (including escaping and final newline),
    # not character counts. CLI uses precisely this serializer.
    while len(encode_activity(result)) > byte_limit:
        result["rows"].pop()
        result["truncated"] = True
        result["complete"] = False
    return result


def encode_activity(value):
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
