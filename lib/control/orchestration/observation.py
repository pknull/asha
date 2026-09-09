"""Bounded, read-only current activity. No plans, criteria or history assembly."""
from __future__ import annotations

import json
import time
from collections import deque
from datetime import datetime, timezone
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
    missing = {key: 0 for key in budgets}

    def check_presence(source, path):
        # Missing registries are not known-empty registries. Existing readers
        # still enforce ownership/no-follow rules; this probe grants no trust.
        try:
            path.lstat()
        except FileNotFoundError:
            missing[source] += 1
            budgets[source].unavailable += 1
        except OSError:
            budgets[source].unavailable += 1

    for source, path in (("rooms", RoomStore(config.control).root),
                         ("tasks", config.control.tasks_dir),
                         ("initiatives", config.initiatives_dir)):
        check_presence(source, path)
    result = {"contract": INVENTORY_CONTRACT, "rows": [], "sources": {},
              "limits": {"rows": rows, "scanned_per_source": scanned,
                         "json_bytes": byte_limit, "seconds": seconds},
              "truncated": False, "complete": True,
              "stale_room_count": 0, "stale_address_count": 0,
              "snapshot": "non-atomic; counts are observed lower bounds, not exact totals"}
    candidates = {}

    def add(source, payload):
        counts[source] += 1
        if source == "rooms" and payload.get("status") in {"missing", "ended"}:
            result["stale_room_count"] += 1
        if payload.get("address_status") == "stale-address":
            result["stale_address_count"] += 1
        bucket = candidates.setdefault(source, deque())
        if len(bucket) < rows:
            bucket.append({**payload, "source": source})
        else:
            result["truncated"] = True

    def ready(source):
        # Check wall time without charging a second entry for processing an
        # already scanned record (including the exact last permitted entry).
        if time.monotonic() >= deadline:
            budgets[source].truncated = True
            return False
        return True

    # Managed state is independent of terminal observation. Sample it before
    # probes so an inaccessible tmux server cannot erase pending questions.
    from ..sessions import overview
    managed_sources = {"sessions": "managed-sessions", "requests": "managed-requests",
                       "deliveries": "managed-deliveries"}
    def managed_budgets():
        for source in managed_sources.values():
            budgets[source] = SnapshotBudget(deadline=deadline, limit=scanned)
            counts[source] = missing[source] = 0
    try:
        managed = overview(config.control, limit=scanned, deadline=deadline)
        if managed["initialized"]:
            managed_budgets()
        for kind, source in managed_sources.items() if managed["initialized"] else ():
            page = managed.get("pages", {}).get(kind)
            if page is None:
                missing[source] += 1
                budgets[source].unavailable += 1
                continue
            for row in page["rows"]:
                if not budgets[source].ready():
                    break
                budgets[source].scanned += 1
                add(source, row)
            if not page["complete"]:
                budgets[source].truncated = True
    except (OSError, ValueError, StoreError):
        managed_budgets()
        for source in managed_sources.values():
            budgets[source].unavailable += 1

    store = InitiativeStore(config)
    # Read global request families independently of the bounded head sample.
    # Keep them distinct from legacy decisions, which also carry file-backed
    # evidence and cannot claim exhaustive coverage on a capped graph read.
    from .sqlite_store import SQLiteInitiativeStore
    if isinstance(store, SQLiteInitiativeStore):
        from .current_actions import page as action_page
        for family in ("initiatives", "approvals"):
            source = "action-" + family
            budgets[source] = SnapshotBudget(deadline=deadline, limit=min(scanned, 100))
            counts[source] = missing[source] = 0
            try:
                action_snapshot = action_page(store, family=family, limit=min(scanned, 100), deadline=deadline)
                budgets[source].scanned = action_snapshot["scanned"]
                budgets[source].unavailable = action_snapshot["unavailable_records"]
                budgets[source].truncated = action_snapshot["deadline_exceeded"] or action_snapshot["next"] is not None
                for item in action_snapshot["rows"]:
                    if item["disposition"] == "pending-review":
                        add(source, item)
            except (OSError, ValueError, StoreError):
                budgets[source].unavailable += 1
    room_records = RoomStore(config.control).bounded_active_snapshots(budgets["rooms"])
    task_records = TaskStore(config.control).bounded_active_snapshots(budgets["tasks"])
    heads = store.bounded_activity_snapshots(budgets["initiatives"])
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
        # Presence failures belong to this initiative's address evidence too;
        # an absent coordinator directory is unknown, not a known-empty list.
        prior = budgets["coordinators"].unavailable
        for source, directory in (("decisions", "approvals"), ("messages", "messages"),
                                  ("coordinators", "coordinators")):
            check_presence(source, config.initiatives_dir / iid / directory)
        for decision in store.bounded_records(iid, "approvals", validate_approval,
                                               "request_id", budgets["decisions"]):
            if not ready("decisions"):
                break
            from .current_actions import approval_demand
            demand = approval_demand(decision, head)
            if demand and demand["disposition"] == "pending-review":
                add("decisions", {"initiative_id": iid, "request_id": decision["request_id"],
                                  "state": "pending"})
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
    # A busy managed source must not monopolize visible rows. Source counters
    # above include observed rows even when row/byte limits omit their details.
    while any(candidates.values()) and len(result["rows"]) < rows:
        for source in budgets:
            bucket = candidates.get(source)
            if bucket and len(result["rows"]) < rows:
                result["rows"].append(bucket.popleft())
    if any(candidates.values()):
        result["truncated"] = True
    result["sources"] = {key: {**budget.summary(), "observed_count": counts[key],
                                "count_kind": "lower-bound", "missing_sources": missing[key]} for key, budget in budgets.items()}
    result["complete"] = not result["truncated"] and all(b.summary()["complete"] for b in budgets.values())
    result = terminal_safe(result)
    # Bound the actual serialized JSON (including escaping and final newline),
    # not character counts. CLI uses precisely this serializer.
    while result["rows"] and len(encode_activity(result)) > byte_limit:
        result["rows"].pop()
        result["truncated"] = True
        result["complete"] = False
    return result


def encode_activity(value):
    return (json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


MAX_STARTUP_BYTES = 4096


def render_startup_observation(activity, *, observed_at):
    """Fixed vocabulary and counts only: never turn retained text into a prompt.

    A complete empty source means no qualifying records were observed, not a
    promise that a non-atomic inventory proves the absence of live activity.
    """
    lines = ["Asha current activity observation",
             f"Observed at: {terminal_safe(observed_at)}; freshness: just sampled, non-atomic.",
             "Read-only observation; no execution authority. Counts are observed lower bounds."]
    labels = (("rooms", "Rooms"), ("tasks", "Live tasks"),
              ("initiatives", "Active initiatives"), ("decisions", "Waiting decisions"),
              ("messages", "Unacknowledged messages"))
    if activity is None:
        lines.append("Activity evidence unavailable; counts unknown.")
    else:
        labels += tuple((key, label) for key, label in (
            ("managed-sessions", "Current managed sessions"),
            ("managed-requests", "Managed questions and permissions"),
            ("managed-deliveries", "Unresolved managed messages"),
            ("action-initiatives", "Global initiative decisions"),
            ("action-approvals", "Global approval requests"),
        ) if key in activity["sources"])
        for source, label in labels:
            facts = activity["sources"][source]
            n = facts["observed_count"]
            status = []
            if facts.get("missing_sources"):
                status.append("missing")
            if facts["truncated"]:
                status.append("capped")
            if facts["unavailable_records"]:
                status.append("unavailable")
            if not facts["complete"]:
                status.append("partial/unknown")
            elif n == 0:
                status.append("known-empty observed registry")
            else:
                status.append("sample complete")
            lines.append(f"{label}: >= {n}; {', '.join(status)}.")
        coords = activity["sources"]["coordinators"]
        if not coords["complete"]:
            coverage = [label for field, label in (
                ("missing_sources", "missing"), ("truncated", "capped"),
                ("unavailable_records", "unavailable"),
            ) if coords.get(field)] + ["partial/unknown"]
            lines.append(f"Coordinator address evidence: {', '.join(coverage)}.")
        # Stale addresses are observed facts, not deliveries or a claim that a
        # coordinator is alive. Row caps can hide additional stale addresses.
        stale_rooms = activity.get("stale_room_count", sum(
            row.get("source") == "rooms" and row.get("status") in {"missing", "ended"}
            for row in activity["rows"]))
        lines.append(f"Stale Room records (missing/ended pane): >= {stale_rooms}; unlisted status unknown.")
        stale = activity.get("stale_address_count", sum(
            row.get("address_status") == "stale-address" for row in activity["rows"]))
        lines.append(f"Stale message addresses: >= {stale}; unlisted addresses unknown.")
        if activity["truncated"]:
            lines.append("Visible rows capped; counts remain lower bounds.")
    text = "\n".join(lines) + "\n"
    if len(text.encode("utf-8")) > MAX_STARTUP_BYTES:
        # No unsafe prefix truncation; required freshness/refusal text survives.
        return ("Asha current activity observation\n"
                "Observed at: unavailable; freshness: unknown.\n"
                "Activity evidence unavailable (summary capacity); counts unknown.\n"
                "Read-only observation; no execution authority.\n")
    return text


def startup_observation():
    from .config import load_config
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        activity = current_activity(load_config())
    except (OSError, ValueError, StoreError):
        activity = None
    return render_startup_observation(activity, observed_at=stamp)
