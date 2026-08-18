"""Read-only joins plus mutating live attempt reconciliation."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ..jj import JjAdapter, JjError
from ..reconcile import LiveAdapters, reconcile_task
from ..store import StoreError, TaskStore
from ..tmux import TmuxAdapter
from .links import control_task_identity_digest
from .model import ATTEMPT_CONTRACT, record_digest, validate_attempt, validate_node
from .store import InitiativeStore
from .results import reconcile_publications
from .seals import (
    NoSealableArtifact, SealError, prepare_and_publish_seal,
    reconcile_seal_drift,
)


NODE_RECONCILIATION_CONTRACT = "asha.orchestration-node-reconciliation.v1"
LIVE_RECONCILIATION_CONTRACT = "asha.orchestration-live-reconciliation.v1"


def _unlinked(node_id: str) -> dict[str, Any]:
    return {
        "contract": NODE_RECONCILIATION_CONTRACT,
        "node_id": node_id,
        "attempt_id": None,
        "control_task_id": None,
        "control_state": "unlinked",
        "control_lifecycle": None,
        "digest_match": None,
        "evidence": [],
    }


def reconcile_nodes(
    initiative_id: str,
    nodes: list[dict[str, Any]],
    *,
    store: InitiativeStore,
    control_store: TaskStore | None = None,
    adapters_factory: Callable[[dict[str, Any]], LiveAdapters] | None = None,
) -> list[dict[str, Any]]:
    """Join stored links to live Control evidence without updating either store."""
    links = store.list_links_snapshot(initiative_id)
    attempts = {
        item["attempt_id"]: item for item in store.list_attempts_snapshot(initiative_id)
    }
    by_node: dict[str, list[dict[str, Any]]] = {}
    for link in links:
        by_node.setdefault(link["node_id"], []).append(link)
    if control_store is None:
        control_store = TaskStore(store.config.control)

    results: list[dict[str, Any]] = []
    for node in nodes:
        candidates = by_node.get(node["node_id"], [])
        if not candidates:
            results.append(_unlinked(node["node_id"]))
            continue
        link = max(
            candidates,
            key=lambda item: (
                attempts.get(item["attempt_id"], {}).get("ordinal", 0),
                item["attempt_id"],
            ),
        )
        base = {
            "contract": NODE_RECONCILIATION_CONTRACT,
            "node_id": node["node_id"],
            "attempt_id": link["attempt_id"],
            "control_task_id": link["control_task_id"],
        }
        try:
            task = control_store.peek(link["control_task_id"])
        except StoreError as exc:
            results.append({
                **base,
                "control_state": "stale",
                "control_lifecycle": None,
                "digest_match": False,
                "evidence": [{
                    "source": "control-task", "outcome": "missing",
                    "detail": str(exc), "state": None, "stale": False,
                }],
            })
            continue
        digest_match = (
            control_task_identity_digest(task)
            == link["control_task_identity_digest"]
        )
        if adapters_factory is None:
            socket = task["tmux"]["socket"]
            adapters = LiveAdapters(
                config=store.config.control,
                tmux=TmuxAdapter(socket=None if socket == "default" else socket),
                jj=JjAdapter(),
            )
        else:
            adapters = adapters_factory(task)
        reconciliation = reconcile_task(task, adapters)
        evidence = list(reconciliation["evidence"])
        if not digest_match:
            evidence.insert(0, {
                "source": "control-task", "outcome": "mismatch",
                "detail": "stored Control task digest differs from the live record",
                "state": None, "stale": False,
            })
        results.append({
            **base,
            "control_state": reconciliation["state"] if digest_match else "stale",
            "control_lifecycle": task["lifecycle"],
            "digest_match": digest_match,
            "evidence": evidence,
        })
    return results


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _transition_attempt(
    store: InitiativeStore,
    initiative_id: str,
    attempt: dict[str, Any],
    target: str,
    at: datetime,
) -> dict[str, Any]:
    if attempt["state"] == target:
        return attempt
    changed = copy.deepcopy(attempt)
    changed.update({"state": target, "updated_at": _timestamp(at)})
    validate_attempt(changed)
    store.save_attempt(
        initiative_id, changed, expected_digest=record_digest(attempt),
    )
    return changed


def _transition_node(
    store: InitiativeStore,
    initiative_id: str,
    node: dict[str, Any],
    target: str,
) -> dict[str, Any]:
    if node["state"] == target:
        return node
    changed = copy.deepcopy(node)
    changed["state"] = target
    validate_node(changed)
    store.save_node(
        initiative_id, changed, expected_digest=record_digest(node),
    )
    return changed


def _active_plan(store: InitiativeStore, initiative: dict[str, Any]) -> dict[str, Any]:
    active = initiative.get("active_plan")
    if active is None:
        raise StoreError("initiative has no active plan")
    plan = store.read_plan(initiative["initiative_id"], active["revision"])
    if plan["digest"] != active["digest"]:
        raise StoreError("active plan digest does not match retained plan")
    return plan


def _first_exit_observation(
    store: InitiativeStore, initiative_id: str, attempt_id: str
) -> datetime | None:
    for event in store.list_events_snapshot(initiative_id):
        if (
            event["type"] == "task-status-observed"
            and attempt_id in event["subject_ids"]
            and event["payload"].get("control_state") == "exited"
        ):
            return datetime.fromisoformat(event["recorded_at"][:-1] + "+00:00")
    return None


def _observe(
    store: InitiativeStore,
    initiative_id: str,
    attempt: dict[str, Any],
    task: dict[str, Any],
    reconciliation: dict[str, Any],
) -> None:
    from .actions import append_event

    evidence_raw = json.dumps(
        reconciliation, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    append_event(
        store,
        initiative_id,
        "task-status-observed",
        [attempt["node_id"], attempt["attempt_id"], task["task_id"]],
        {
            "control_state": reconciliation["state"],
            "control_lifecycle": task["lifecycle"],
            "evidence_digest": hashlib.sha256(evidence_raw).hexdigest(),
        },
        actor_kind="controller",
        actor_id="live-reconciler",
    )


def _active_jj_snapshot_window(
    task: dict[str, Any], reconciliation: dict[str, Any],
) -> bool:
    """Recognize the brief jj metadata split while a live worker snapshots.

    `jj status` updates the working-copy and repository views in separate
    durable steps. A concurrent read can observe their documented identity
    disagreement even though the owned process is still running. Result
    publication performs its own ownership check, and a mismatch that remains
    after process exit still takes the normal stale/conflict path.
    """
    if task["lifecycle"] != "running" or reconciliation.get("state") != "stale":
        return False
    evidence = reconciliation.get("evidence", [])
    process_live = any(
        item.get("source") == "process"
        and item.get("outcome") == "match"
        for item in evidence
    )
    jj_split = any(
        item.get("source") == "jj"
        and item.get("outcome") == "mismatch"
        and isinstance(item.get("detail"), str)
        and item["detail"].endswith(
            "created workspace registration identity disagrees with working copy"
        )
        for item in evidence
    )
    hard_mismatch = any(
        item.get("outcome") == "mismatch"
        and item.get("source") != "jj"
        for item in evidence
    )
    return process_live and jj_split and not hard_mismatch


def _mark_conflict(
    store: InitiativeStore,
    initiative_id: str,
    attempt: dict[str, Any],
    node: dict[str, Any],
    reason: str,
    at: datetime,
) -> None:
    from .scheduler import pause_for_breaker

    if attempt["state"] == "indeterminate":
        attempt = _transition_attempt(store, initiative_id, attempt, "running", at)
    if attempt["state"] in {"dispatching", "running", "reported", "awaiting-exit"}:
        _transition_attempt(store, initiative_id, attempt, "stale", at)
    if node["state"] in {"dispatching", "running", "evaluating", "needs-input"}:
        _transition_node(store, initiative_id, node, "stale")
    pause_for_breaker(
        store,
        initiative_id,
        reason,
        event_type="reconciliation-conflict",
        subject_ids=[node["node_id"], attempt["attempt_id"]],
    )


def _nested_violation(tasks: list[dict[str, Any]], attempt_id: str) -> list[str]:
    matches = sorted(
        task["task_id"] for task in tasks
        if attempt_id in task.get("label", "")
    )
    return matches if len(matches) > 1 else []


def _failure_breaker_recorded(
    store: InitiativeStore, initiative_id: str, failed: dict[str, Any], limit: int,
) -> bool:
    reason = f"{limit} consecutive retriable failures reached"
    return any(
        event["type"] == "limit-reached"
        and failed["attempt_id"] in event["subject_ids"]
        and event["payload"].get("reason") == reason
        for event in store.list_events_snapshot(initiative_id)
    )


def _failure_target(
    store: InitiativeStore,
    initiative_id: str,
    initiative: dict[str, Any],
    plan: dict[str, Any],
    node: dict[str, Any],
    failed: dict[str, Any],
    attempts: list[dict[str, Any]],
    at: datetime,
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """Move a failure through evaluating and reserve one autonomous retry."""
    from .actions import _repair_lineage_attempts, append_event
    from .scheduler import (
        _resolved_attempt_base,
        consecutive_failures,
        pause_for_breaker,
        storage_report as scheduler_storage_report,
    )

    current_node = store.read_node(initiative_id, node["node_id"])
    if current_node["state"] in {"dispatching", "running", "needs-input"}:
        before = current_node["state"]
        current_node = _transition_node(store, initiative_id, current_node, "evaluating")
        append_event(
            store, initiative_id, "node-state-changed",
            [current_node["node_id"], failed["attempt_id"]],
            {"from": before, "to": "evaluating", "reason": failed["state"]},
            actor_kind="controller", actor_id="live-reconciler",
        )

    salvage_lineage = any(
        item["outcome"] == "failure" and item["read_only"]
        for item in failed["base"]["seal_inputs"]
    )
    repair_lineage = any(
        item["attempt_id"] == failed["attempt_id"]
        for item in _repair_lineage_attempts(store, initiative_id)
    )
    if salvage_lineage or repair_lineage:
        if current_node["state"] == "evaluating":
            before = current_node["state"]
            current_node = _transition_node(
                store, initiative_id, current_node, "needs-input",
            )
            append_event(
                store, initiative_id, "node-state-changed",
                [current_node["node_id"], failed["attempt_id"]],
                {
                    "from": before,
                    "to": "needs-input",
                    "reason": (
                        "salvage lineage requires new approval"
                        if salvage_lineage else
                        "repair lineage requires operator input"
                    ),
                },
                actor_kind="controller", actor_id="live-reconciler",
            )
        return current_node, None, (
            "salvage-lineage-needs-input"
            if salvage_lineage else "repair-lineage-needs-input"
        )

    refreshed = [
        store.read_attempt(initiative_id, item["attempt_id"])
        if item["attempt_id"] == failed["attempt_id"] else item
        for item in attempts
    ]
    if (
        consecutive_failures(refreshed) >= store.config.max_consecutive_failures
        and not _failure_breaker_recorded(
            store, initiative_id, failed, store.config.max_consecutive_failures,
        )
    ):
        pause_for_breaker(
            store, initiative_id,
            f"{store.config.max_consecutive_failures} consecutive retriable failures reached",
            subject_ids=[current_node["node_id"], failed["attempt_id"]],
        )
        return current_node, None, "consecutive-failure-breaker"

    node_attempts = [
        item for item in refreshed if item["node_id"] == current_node["node_id"]
    ]
    attempt_cap = min(
        initiative["limits"]["max_attempts_per_node"],
        plan["limits"]["max_attempts_per_node"],
    )
    total_cap = min(
        initiative["limits"]["max_total_tasks"],
        plan["limits"]["max_total_tasks"],
    )
    raw_deadline = plan["limits"]["deadline"] or initiative["limits"]["deadline"]
    deadline_reached = (
        raw_deadline is not None
        and at >= datetime.fromisoformat(raw_deadline[:-1] + "+00:00")
    )
    storage_paused = scheduler_storage_report(
        initiative, store=store
    )["pause_recommended"]
    exhausted_reason = None
    if len(node_attempts) >= attempt_cap:
        exhausted_reason = "node attempt cap exhausted"
    elif len(refreshed) >= total_cap:
        exhausted_reason = "initiative task budget exhausted"
    elif deadline_reached:
        exhausted_reason = "initiative deadline reached"
    elif storage_paused:
        exhausted_reason = "retained storage pause threshold reached"
    if exhausted_reason is not None:
        if current_node["state"] == "evaluating":
            before = current_node["state"]
            current_node = _transition_node(store, initiative_id, current_node, "failed")
            append_event(
                store, initiative_id, "node-state-changed",
                [current_node["node_id"], failed["attempt_id"]],
                {"from": before, "to": "failed", "reason": exhausted_reason},
                actor_kind="controller", actor_id="live-reconciler",
            )
        if exhausted_reason != "node attempt cap exhausted":
            pause_for_breaker(
                store, initiative_id, exhausted_reason,
                event_type=(
                    "storage-threshold-reached"
                    if storage_paused else "limit-reached"
                ),
                subject_ids=[current_node["node_id"], failed["attempt_id"]],
            )
        return current_node, None, exhausted_reason

    retry = validate_attempt({
        "contract": ATTEMPT_CONTRACT,
        "attempt_id": str(uuid.uuid4()),
        "initiative_id": initiative_id,
        "node_id": current_node["node_id"],
        "task_id": str(uuid.uuid4()),
        "action_id": None,
        "ordinal": max(item["ordinal"] for item in node_attempts) + 1,
        "base": _resolved_attempt_base(
            store, initiative_id, plan, current_node,
        ),
        "state": "allocated",
        "result_publication_id": None,
        "result_id": None,
        "seal_id": None,
        "created_at": _timestamp(at),
        "updated_at": _timestamp(at),
    })
    store.save_attempt(initiative_id, retry)
    if current_node["state"] == "evaluating":
        current_node = _transition_node(store, initiative_id, current_node, "ready")
    append_event(
        store, initiative_id, "node-ready",
        [current_node["node_id"], retry["attempt_id"]],
        {
            "retry_of": failed["attempt_id"],
            "ordinal": retry["ordinal"],
            "original_base": retry["base"],
        },
        actor_kind="controller", actor_id="live-reconciler",
    )
    return current_node, retry, None


def reconcile_live(
    store: InitiativeStore,
    initiative_id: str,
    *,
    control_store: TaskStore | None = None,
    adapters_factory: Callable[[dict[str, Any]], LiveAdapters] | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Persist Control live evidence and deterministic retry/breaker edges."""
    observations: list[dict[str, Any]] = []
    conflicts: list[dict[str, str]] = []
    retries: list[dict[str, Any]] = []
    seals: list[dict[str, Any]] = []
    probes: list[dict[str, str]] = []
    clock = now or _now
    with store.transaction_lock(initiative_id):
        initiative = store.peek(initiative_id)
        if initiative["active_plan"] is None:
            return {
                "contract": LIVE_RECONCILIATION_CONTRACT,
                "initiative_id": initiative_id,
                "observations": observations,
                "conflicts": conflicts,
                "retries": retries,
                "seals": seals,
                "probes": probes,
            }
        # Result journals always recover before later process-exit or seal
        # evaluation, so a completion visible before a crash wins the ordering.
        reconcile_publications(
            store, initiative_id, control_store=control_store,
        )
        publication_conflicts = {
            publication["attempt_id"]
            for publication in store.list_result_publications_snapshot(initiative_id)
            if publication["state"] == "indeterminate"
        }
        plan = _active_plan(store, initiative)
        control_store = control_store or TaskStore(store.config.control)
        attempts = store.list_attempts_snapshot(initiative_id)
        links = {
            link["attempt_id"]: link
            for link in store.list_links_snapshot(initiative_id)
        }
        nodes = {
            node["node_id"]: node
            for node in store.list_nodes_snapshot(initiative_id)
        }
        try:
            control_tasks = control_store.list()
            if not isinstance(control_tasks, list):
                raise ValueError("Control task list returned a non-list value")
        except (StoreError, OSError, ValueError) as exc:
            control_tasks = []
            probes.append({
                "name": "control-task-list",
                "outcome": "unavailable",
                "detail": str(exc),
            })
        else:
            probes.append({
                "name": "control-task-list",
                "outcome": "match",
                "detail": f"observed {len(control_tasks)} Control task(s)",
            })
        handled_failures: set[str] = set()
        for attempt in sorted(
            attempts, key=lambda item: (item["node_id"], item["ordinal"], item["attempt_id"]),
        ):
            if attempt["state"] not in {
                "dispatching", "running", "reported", "awaiting-exit",
                "success-seal-ready", "failure-seal-ready", "paused-seal-ready",
                "sealing", "indeterminate",
            }:
                continue
            link = links.get(attempt["attempt_id"])
            node = nodes[attempt["node_id"]]
            current_time = clock()
            action = None
            if attempt["action_id"] is not None:
                try:
                    action = store.read_action(initiative_id, attempt["action_id"])
                except StoreError:
                    pass
            if action is not None and action["state"] in {"dispatching", "indeterminate"}:
                observations.append({
                    "attempt_id": attempt["attempt_id"],
                    "control_task_id": attempt["task_id"],
                    "control_state": "action-indeterminate",
                })
                continue
            if attempt["attempt_id"] in publication_conflicts:
                observations.append({
                    "attempt_id": attempt["attempt_id"],
                    "control_task_id": attempt["task_id"],
                    "control_state": "publication-indeterminate",
                })
                continue
            if attempt["state"] == "reported":
                attempt = _transition_attempt(
                    store, initiative_id, attempt, "awaiting-exit", current_time,
                )
            if attempt["state"] == "dispatching" and link is None:
                observations.append({
                    "attempt_id": attempt["attempt_id"],
                    "control_task_id": attempt["task_id"],
                    "control_state": "dispatch-pending-link",
                })
                continue
            if link is None:
                reason = "dispatched attempt has no immutable Control task link"
                _mark_conflict(store, initiative_id, attempt, node, reason, current_time)
                conflicts.append({"attempt_id": attempt["attempt_id"], "reason": reason})
                continue
            nested = _nested_violation(control_tasks, attempt["attempt_id"])
            if nested:
                from .scheduler import pause_for_breaker

                reason = "nested workflow created more than one Control task for the attempt"
                pause_for_breaker(
                    store, initiative_id, reason,
                    subject_ids=[attempt["attempt_id"], *nested],
                )
                conflicts.append({"attempt_id": attempt["attempt_id"], "reason": reason})
                continue
            try:
                task = control_store.peek(link["control_task_id"])
            except StoreError as exc:
                reason = f"linked Control task is missing: {exc}"
                _mark_conflict(store, initiative_id, attempt, node, reason, current_time)
                conflicts.append({"attempt_id": attempt["attempt_id"], "reason": reason})
                continue
            if (
                control_task_identity_digest(task)
                != link["control_task_identity_digest"]
            ):
                reason = "linked Control task identity digest changed"
                _mark_conflict(store, initiative_id, attempt, node, reason, current_time)
                conflicts.append({"attempt_id": attempt["attempt_id"], "reason": reason})
                continue
            if adapters_factory is None:
                socket = task["tmux"]["socket"]
                adapters = LiveAdapters(
                    config=store.config.control,
                    tmux=TmuxAdapter(socket=None if socket == "default" else socket),
                    jj=JjAdapter(),
                )
            else:
                adapters = adapters_factory(task)
            observed = reconcile_task(task, adapters)
            if _active_jj_snapshot_window(task, observed):
                observed = copy.deepcopy(observed)
                observed["state"] = "working"
                observed["blocker"] = None
            previous_exit = _first_exit_observation(
                store, initiative_id, attempt["attempt_id"]
            )
            _observe(store, initiative_id, attempt, task, observed)
            observations.append({
                "attempt_id": attempt["attempt_id"],
                "control_task_id": task["task_id"],
                "control_state": observed["state"],
            })
            state = observed["state"]
            if state == "stale":
                reason = observed.get("blocker") or "Control reconciliation is stale"
                _mark_conflict(store, initiative_id, attempt, node, reason, current_time)
                conflicts.append({"attempt_id": attempt["attempt_id"], "reason": reason})
                continue
            if state in {"starting", "working", "needs-input", "idle", "unknown"}:
                if attempt["state"] in {"dispatching", "indeterminate"}:
                    attempt = _transition_attempt(
                        store, initiative_id, attempt, "running", current_time,
                    )
                current_node = store.read_node(initiative_id, node["node_id"])
                if current_node["state"] == "dispatching":
                    _transition_node(store, initiative_id, current_node, "running")
                continue
            seal_in_progress = attempt["state"] in {
                "success-seal-ready", "failure-seal-ready", "paused-seal-ready",
                "sealing",
            }
            if seal_in_progress:
                if state not in {"exited", "failed"}:
                    continue
            elif state == "exited" and attempt["result_id"] is None:
                if previous_exit is None:
                    continue
                if (current_time - previous_exit).total_seconds() < store.config.result_grace_seconds:
                    continue
                if attempt["state"] in {"dispatching", "indeterminate"}:
                    attempt = _transition_attempt(
                        store, initiative_id, attempt, "running", current_time,
                    )
                attempt = _transition_attempt(
                    store, initiative_id, attempt, "result-missing", current_time,
                )
                from .actions import append_event

                append_event(
                    store, initiative_id, "result-missing",
                    [attempt["node_id"], attempt["attempt_id"], attempt["task_id"]],
                    {"grace_seconds": store.config.result_grace_seconds},
                    actor_kind="controller", actor_id="live-reconciler",
                )
            elif state == "failed":
                if attempt["state"] == "indeterminate":
                    attempt = _transition_attempt(
                        store, initiative_id, attempt, "running", current_time,
                    )
                attempt = _transition_attempt(
                    store, initiative_id, attempt, "abnormal-exit", current_time,
                )
            elif state == "exited" and attempt["result_id"] is not None:
                if attempt["state"] == "reported":
                    attempt = _transition_attempt(
                        store, initiative_id, attempt, "awaiting-exit", current_time,
                    )
            else:
                continue
            if node["type"] == "review":
                from .review import ReviewError, complete_review_attempt

                try:
                    complete_review_attempt(
                        store, initiative_id, attempt["attempt_id"], task, observed,
                        jj=(
                            adapters.jj_adapter
                            if isinstance(adapters, LiveAdapters) else None
                        ),
                    )
                except (ReviewError, StoreError, JjError, OSError, ValueError) as exc:
                    from .scheduler import pause_for_breaker

                    reason = f"review reconciliation failed: {exc}"
                    pause_for_breaker(
                        store, initiative_id, reason[:1000],
                        event_type="reconciliation-conflict",
                        subject_ids=[node["node_id"], attempt["attempt_id"]],
                    )
                    conflicts.append({
                        "attempt_id": attempt["attempt_id"], "reason": reason,
                    })
                continue
            try:
                seal = prepare_and_publish_seal(
                    store, initiative_id, attempt["attempt_id"], task, observed,
                    jj=(adapters.jj_adapter if isinstance(adapters, LiveAdapters) else None),
                )
            except NoSealableArtifact:
                latest = store.read_attempt(initiative_id, attempt["attempt_id"])
                if latest["state"] in {"abnormal-exit", "result-missing"}:
                    latest = _transition_attempt(
                        store, initiative_id, latest, "failed-no-artifact", current_time,
                    )
                current_attempts = store.list_attempts_snapshot(initiative_id)
                current_node, retry, _reason = _failure_target(
                    store, initiative_id, store.peek(initiative_id), plan,
                    store.read_node(initiative_id, node["node_id"]), latest,
                    current_attempts, current_time,
                )
                nodes[current_node["node_id"]] = current_node
                if retry is not None:
                    retries.append(retry)
                handled_failures.add(attempt["attempt_id"])
                continue
            except (SealError, StoreError, JjError, OSError, ValueError) as exc:
                from .scheduler import pause_for_breaker

                reason = f"seal reconciliation failed: {exc}"
                pause_for_breaker(
                    store, initiative_id, reason[:1000],
                    event_type="reconciliation-conflict",
                    subject_ids=[node["node_id"], attempt["attempt_id"]],
                )
                conflicts.append({"attempt_id": attempt["attempt_id"], "reason": reason})
                continue
            seals.append(seal)
            if seal["outcome"] == "failure":
                handled_failures.add(attempt["attempt_id"])
            refreshed_attempts = store.list_attempts_snapshot(initiative_id)
            newly_allocated = [
                item for item in refreshed_attempts
                if item["node_id"] == node["node_id"]
                and item["state"] == "allocated"
                and item["ordinal"] > attempt["ordinal"]
            ]
            retries.extend(newly_allocated)

        # The latest retriable failure is re-evaluated on every pass.  This is
        # what lets a breaker-paused initiative reserve its retry on resume.
        latest_by_node: dict[str, dict[str, Any]] = {}
        for attempt in store.list_attempts_snapshot(initiative_id):
            current = latest_by_node.get(attempt["node_id"])
            if current is None or (attempt["ordinal"], attempt["attempt_id"]) > (
                current["ordinal"], current["attempt_id"]
            ):
                latest_by_node[attempt["node_id"]] = attempt
        for node_id, failed in sorted(latest_by_node.items()):
            if (
                failed["state"] not in {
                    "launch-failed", "result-missing", "abnormal-exit",
                    "failed-no-artifact", "sealed-failure",
                }
                or failed["attempt_id"] in handled_failures
            ):
                continue
            current_node_record = store.read_node(initiative_id, node_id)
            if current_node_record["state"] not in {"evaluating", "ready"}:
                continue
            current_time = clock()
            current_attempts = store.list_attempts_snapshot(initiative_id)
            current_initiative = store.peek(initiative_id)
            current_node, retry, _reason = _failure_target(
                store, initiative_id, current_initiative, plan,
                current_node_record, failed,
                current_attempts, current_time,
            )
            nodes[node_id] = current_node
            if retry is not None:
                retries.append(retry)

        if any(item["outcome"] == "success" for item in seals):
            from .scheduler import refresh_readiness

            refresh_readiness(store, initiative_id)

        # Published seals remain immutable. Any later workspace commit drift is
        # separate evidence and pauses affected work.
        conflicts.extend({
            "attempt_id": next(
                seal["attempt_id"] for seal in store.list_seals_snapshot(initiative_id)
                if seal["seal_id"] == item["seal_id"]
            ),
            "reason": item["reason"],
        } for item in reconcile_seal_drift(
            store, initiative_id, control_store=control_store,
        ))

    return {
        "contract": LIVE_RECONCILIATION_CONTRACT,
        "initiative_id": initiative_id,
        "observations": observations,
        "conflicts": conflicts,
        "retries": retries,
        "seals": seals,
        "probes": probes,
    }


__all__ = [
    "LIVE_RECONCILIATION_CONTRACT", "NODE_RECONCILIATION_CONTRACT",
    "reconcile_live", "reconcile_nodes",
]
