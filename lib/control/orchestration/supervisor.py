"""Pure stateless progression sweep for active orchestration initiatives."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .model import INITIATIVE_TERMINAL_STATES


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("supervisor clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        raise ValueError("supervisor timestamp is invalid")
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _exception_message(exc: BaseException) -> str:
    detail = "".join(character if character.isprintable() else "?" for character in str(exc))
    return (detail or type(exc).__name__)[:450]


def _state_snapshot(store, initiative_id: str) -> dict[str, Any]:
    initiative = store.peek(initiative_id)
    return {
        "initiative": {initiative_id: initiative["state"]},
        "node": {
            item["node_id"]: item["state"]
            for item in store.list_nodes_snapshot(initiative_id)
        },
        "attempt": {
            item["attempt_id"]: item["state"]
            for item in store.list_attempts_snapshot(initiative_id)
        },
    }


def _transitions(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for kind in ("initiative", "node", "attempt"):
        identities = sorted(set(before[kind]) | set(after[kind]))
        for identity in identities:
            old = before[kind].get(identity)
            new = after[kind].get(identity)
            if old != new:
                result.append({
                    "kind": kind, "id": identity,
                    "from": old or "absent", "to": new or "absent",
                })
    return result


def _next_deadline(store, initiative_id: str, at: datetime) -> str | None:
    first_exit: dict[str, datetime] = {}
    for event in store.list_events_snapshot(initiative_id):
        if (
            event["type"] == "task-status-observed"
            and event["payload"].get("control_state") == "exited"
        ):
            observed = _parse_timestamp(event["recorded_at"])
            for subject in event["subject_ids"]:
                first_exit.setdefault(subject, observed)
    candidates: list[datetime] = []
    interval = timedelta(seconds=store.config.supervisor_interval_seconds)
    for attempt in store.list_attempts_snapshot(initiative_id):
        if attempt["state"] == "running":
            exited = first_exit.get(attempt["attempt_id"])
            candidates.append(
                at + interval if exited is None else
                max(at, exited + timedelta(seconds=store.config.result_grace_seconds))
            )
        elif attempt["state"] in {"reported", "awaiting-exit"}:
            candidates.append(at + interval)
    return None if not candidates else _timestamp(min(candidates))


def tick(deps) -> dict[str, Any]:
    """Run one isolated ingestion/reconciliation sweep with no retained cursor."""
    at = _utc(deps.now())
    report: dict[str, Any] = {
        "started_at": _timestamp(at), "finished_at": None,
        "counts": {
            "eligible": 0, "succeeded": 0, "errors": 0,
            "ingested": 0, "transitions": 0,
        },
        "initiatives": [],
    }
    try:
        initiatives = deps.list_initiatives()
    except Exception as exc:
        report["counts"]["errors"] = 1
        report["sweep_error"] = _exception_message(exc)
        report["finished_at"] = _timestamp(at)
        return report
    for listed in initiatives:
        if (
            listed.get("state") in INITIATIVE_TERMINAL_STATES
            or listed.get("state") == "archived"
            or listed.get("active_plan") is None
        ):
            continue
        initiative_id = listed["initiative_id"]
        report["counts"]["eligible"] += 1
        item = {
            "initiative_id": initiative_id, "state": listed["state"],
            "transitions": [], "next_deadline": None,
            "ingested": 0, "error": None,
        }
        store = before = None
        try:
            store = deps.store_factory(initiative_id)
            before = _state_snapshot(store, initiative_id)
            receipts = deps.ingest(
                store, initiative_id, control_store=deps.control_store,
            )
            item["ingested"] = len(receipts)
            deps.reconcile(
                store, initiative_id, control_store=deps.control_store,
                now=deps.now,
            )
            after = _state_snapshot(store, initiative_id)
            item["state"] = store.peek(initiative_id)["state"]
            item["transitions"] = _transitions(before, after)
            item["next_deadline"] = _next_deadline(store, initiative_id, at)
            report["counts"]["succeeded"] += 1
        except Exception as exc:
            item["error"] = _exception_message(exc)
            report["counts"]["errors"] += 1
            if store is not None and before is not None:
                try:
                    after = _state_snapshot(store, initiative_id)
                    item["state"] = store.peek(initiative_id)["state"]
                    item["transitions"] = _transitions(before, after)
                    item["next_deadline"] = _next_deadline(store, initiative_id, at)
                except Exception:
                    pass
        report["counts"]["ingested"] += item["ingested"]
        report["counts"]["transitions"] += len(item["transitions"])
        report["initiatives"].append(item)
    report["finished_at"] = _timestamp(at)
    return report


__all__ = ["tick"]
