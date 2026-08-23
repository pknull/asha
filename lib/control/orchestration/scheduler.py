"""Deterministic readiness, bounded Control dispatch, and circuit breakers."""

from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..model import validate_task
from ..process import capture_bytes
from ..store import StoreError, TaskStore, task_digest
from ..transaction import CreationJournalStore, JournalError
from .config import OrchestrationConfig
from .graph import dependency_states
from .links import build_link
from .model import (
    ATTEMPT_ACTIVE_STATES,
    ATTEMPT_CONTRACT,
    EVENT_CONTRACT,
    MUTATING_NODE_TYPES,
    record_digest,
    validate_attempt,
    validate_node,
)
from .storage import storage_report
from .store import InitiativeStore


READINESS_CONTRACT = "asha.orchestration-readiness.v1"
DISPATCH_CONTRACT = "asha.orchestration-dispatch.v1"
MAX_ASSIGNMENT_BYTES = 32 * 1024
MAX_CONTROL_OUTPUT_BYTES = 256 * 1024
DISPATCH_TIMEOUT_SECONDS = 60
_FAILURE_STATES = frozenset({
    "launch-failed", "result-missing", "abnormal-exit", "failed-no-artifact",
    "sealed-failure",
})


class SchedulerError(ValueError):
    """A deterministic scheduler refusal or bounded Control failure."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _active_plan(store: InitiativeStore, initiative: dict[str, Any]) -> dict[str, Any]:
    active = initiative.get("active_plan")
    if active is None:
        raise SchedulerError("initiative has no approved active plan")
    plan = store.read_plan(initiative["initiative_id"], active["revision"])
    if plan["digest"] != active["digest"]:
        raise SchedulerError("active plan digest does not match its retained revision")
    return plan


def _limit(initiative: dict[str, Any], plan: dict[str, Any], name: str) -> int:
    return min(initiative["limits"][name], plan["limits"][name])


def _deadline_reached(initiative: dict[str, Any], plan: dict[str, Any]) -> bool:
    raw = plan["limits"]["deadline"] or initiative["limits"]["deadline"]
    if raw is None:
        return False
    return datetime.now(timezone.utc) >= datetime.fromisoformat(raw[:-1] + "+00:00")


def _active_attempts(attempts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        attempt for attempt in attempts
        if attempt["state"] in ATTEMPT_ACTIVE_STATES or attempt["state"] == "indeterminate"
    ]


def _gate_rerun_attempt_ids(
    store: InitiativeStore, initiative_id: str, node_id: str,
    attempts: list[dict[str, Any]],
) -> tuple[set[str], int]:
    """Bind stale-gate reopen events one-to-one to their later attempts."""
    events = sorted(
        (
            event for event in store.list_events_snapshot(initiative_id)
            if event["type"] == "node-ready"
            and node_id in event["subject_ids"]
            and event["payload"].get("reason") == "candidate-review-staled"
        ),
        key=lambda item: (item["recorded_at"], item["event_id"]),
    )
    candidates = sorted(
        (item for item in attempts if item["node_id"] == node_id),
        key=lambda item: (item["created_at"], item["attempt_id"]),
    )
    bound: set[str] = set()
    for event in events:
        match = next(
            (
                attempt for attempt in candidates
                if attempt["attempt_id"] not in bound
                and attempt["created_at"] >= event["recorded_at"]
            ),
            None,
        )
        if match is not None:
            bound.add(match["attempt_id"])
    return bound, max(0, len(events) - len(bound))


def _storage_paused(store: InitiativeStore, initiative: dict[str, Any]) -> bool:
    return bool(storage_report(initiative, store=store)["pause_recommended"])


def readiness(
    store: InitiativeStore, initiative: dict[str, Any]
) -> dict[str, str]:
    """Return one deterministic effective state per active-plan node.

    The function is observation-only.  Breakers are persisted only by an
    accepted dispatch or live reconciliation transition.
    """
    plan = _active_plan(store, initiative)
    nodes = {
        node["node_id"]: node
        for node in store.list_nodes_snapshot(initiative["initiative_id"])
        if node["node_id"] in {item["node_id"] for item in plan["nodes"]}
    }
    attempts = store.list_attempts_snapshot(initiative["initiative_id"])
    seals = store.list_seals_snapshot(initiative["initiative_id"])
    dependency = dependency_states(
        plan, {node_id: node["state"] for node_id, node in nodes.items()}, seals,
    )
    globally_blocked = (
        initiative["state"] != "running"
        or _deadline_reached(initiative, plan)
        or len(_active_attempts(attempts)) >= _limit(initiative, plan, "max_parallel")
        or _storage_paused(store, initiative)
    )
    result: dict[str, str] = {}
    for node_id in sorted(nodes):
        current = nodes[node_id]["state"]
        gate_attempt_ids, pending_gate_reruns = _gate_rerun_attempt_ids(
            store, initiative["initiative_id"], node_id, attempts,
        )
        ordinary_attempts = sum(
            1 for attempt in attempts
            if attempt["node_id"] == node_id
            and attempt["attempt_id"] not in gate_attempt_ids
        )
        has_reservation = any(
            attempt["node_id"] == node_id and attempt["state"] == "allocated"
            for attempt in attempts
        )
        if current not in {"approved", "blocked", "ready"}:
            result[node_id] = current
        elif (
            globally_blocked
            or dependency[node_id] != "ready"
            or (
                not has_reservation
                and len(attempts) >= _limit(initiative, plan, "max_total_tasks")
            )
            or (
                not has_reservation
                and pending_gate_reruns == 0
                and ordinary_attempts
                >= _limit(initiative, plan, "max_attempts_per_node")
            )
            or (
                not has_reservation
                and pending_gate_reruns > 0
                and len(gate_attempt_ids)
                >= _limit(initiative, plan, "max_repair_cycles")
            )
        ):
            result[node_id] = "blocked"
        else:
            result[node_id] = "ready"
    return result


def refresh_readiness(store: InitiativeStore, initiative_id: str) -> dict[str, str]:
    """Persist legal approved/blocked-to-ready activation edges."""
    from .actions import append_event

    with store.transaction_lock(initiative_id):
        initiative = store.peek(initiative_id)
        effective = readiness(store, initiative)
        for node_id in sorted(effective):
            node = store.read_node(initiative_id, node_id)
            target = effective[node_id]
            if target == node["state"]:
                continue
            if node["state"] == "approved" and target in {"blocked", "ready"}:
                changed = copy.deepcopy(node)
                changed["state"] = target
                store.save_node(
                    initiative_id, changed, expected_digest=record_digest(node),
                )
                append_event(
                    store, initiative_id,
                    "node-ready" if target == "ready" else "node-state-changed",
                    [node_id], {"from": node["state"], "to": target},
                    actor_kind="controller", actor_id="scheduler",
                )
            elif node["state"] == "blocked" and target == "ready":
                changed = copy.deepcopy(node)
                changed["state"] = "ready"
                store.save_node(
                    initiative_id, changed, expected_digest=record_digest(node),
                )
                append_event(
                    store, initiative_id, "node-ready", [node_id],
                    {"from": "blocked", "to": "ready"},
                    actor_kind="controller", actor_id="scheduler",
                )
        return effective


def consecutive_failures(attempts: list[dict[str, Any]]) -> int:
    """Count the deterministic trailing initiative-wide retriable failures."""
    ordered = sorted(
        attempts,
        key=lambda item: (item["updated_at"], item["created_at"], item["attempt_id"]),
    )
    count = 0
    for attempt in reversed(ordered):
        if attempt["state"] not in _FAILURE_STATES:
            break
        count += 1
    return count


def pause_for_breaker(
    store: InitiativeStore,
    initiative_id: str,
    reason: str,
    *,
    event_type: str = "limit-reached",
    subject_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Pause a running initiative and retain the exact breaker evidence."""
    from .actions import append_event

    with store.transaction_lock(initiative_id):
        initiative = store.peek(initiative_id)
        subjects = subject_ids or []
        if initiative["state"] == "paused":
            duplicate = any(
                event["type"] == event_type
                and event["subject_ids"] == subjects
                and event["payload"] == {"reason": reason}
                for event in store.list_events_snapshot(initiative_id)
            )
            if duplicate:
                return initiative
        if initiative["state"] == "running":
            changed = copy.deepcopy(initiative)
            changed.update({
                "state": "paused",
                "state_revision": initiative["state_revision"] + 1,
                "updated_at": _now(),
            })
            store.save_initiative(
                changed, expected_digest=record_digest(initiative),
            )
        append_event(
            store, initiative_id, event_type, subjects, {"reason": reason},
            actor_kind="controller", actor_id="scheduler",
        )
        return store.peek(initiative_id)


def _dispatch_breaker(
    store: InitiativeStore,
    initiative: dict[str, Any],
    plan: dict[str, Any],
    attempts: list[dict[str, Any]],
    *,
    has_reservation: bool,
) -> tuple[str, str] | None:
    if _deadline_reached(initiative, plan):
        return "limit-reached", "initiative deadline reached"
    if (
        not has_reservation
        and len(attempts) >= _limit(initiative, plan, "max_total_tasks")
    ):
        return "limit-reached", "initiative max_total_tasks exhausted"
    if _storage_paused(store, initiative):
        return "storage-threshold-reached", "retained storage pause threshold reached"
    return None


def _truncate(value: str, maximum: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= maximum:
        return value
    suffix = "\n[truncated by Orchestration Core 2a]"
    room = maximum - len(suffix.encode("utf-8"))
    prefix = raw[:max(0, room)]
    while True:
        try:
            return prefix.decode("utf-8") + suffix
        except UnicodeDecodeError:
            prefix = prefix[:-1]


def _json_lines(values: list[Any], maximum: int) -> str:
    rendered = "\n".join(
        f"- {json.dumps(value, ensure_ascii=False, sort_keys=True)}"
        for value in values
    ) or "- None"
    return _truncate(rendered, maximum)


def assignment_bytes(
    initiative: dict[str, Any],
    plan: dict[str, Any],
    node: dict[str, Any],
    attempt: dict[str, Any],
    exact_base: str,
    resolved_seals: list[dict[str, Any]] | None = None,
) -> bytes:
    """Render the bounded deterministic 2b worker assignment contract."""
    base = attempt["base"]
    nested = plan["nested_workflow_policy"]
    seal_facts = resolved_seals or []
    read_only_facts = [item for item in seal_facts if item["read_only"]]
    composition = ""
    if node["type"] == "compose":
        composition = f"""
## Composition contract

- Conflict policy: {node['conflict_policy']}
- Ordered exact success seals: {json.dumps([item['seal_id'] for item in seal_facts])}
- Start from the shared scope-origin base above. Produce one candidate combining
  the ordered inputs. Rebase or merge only inside this workspace. Never touch
  the source repository or another workspace. If the declared conflict policy
  cannot be satisfied, publish `failed` or `blocked`; do not omit an input.
"""
    review_contract = ""
    if node["type"] == "review":
        from .review import specification_digest

        target = {
            "seal_id": seal_facts[0]["seal_id"],
            "active_plan_digest": plan["digest"],
            "specification_digest": specification_digest(initiative, plan),
            "repository_id": _node_repository(initiative, node)["repository_id"],
            "jj_commit_id": seal_facts[0]["jj_commit_id"],
            "base_seal_ids": seal_facts[0].get("base_seal_ids", []),
            "diff_digest": seal_facts[0]["diff_digest"],
        }
        review_contract = f"""
## Independent review contract

This is a read-only review of the exact immutable target below. Do not write
into this workspace or run any command that writes into it. Publish a
`completed` result with `files_changed: []` and a
`review` object containing `verdict` (`pass|findings`), `findings`
(`severity`, `location`, `summary`), and this exact `target` object:

{json.dumps(target, ensure_ascii=False, sort_keys=True)}
"""
    publication_workspace_rule = (
        "Do not run `jj status` or any other command that may snapshot or write "
        "the review workspace. Write the result document outside the workspace "
        "to `$XDG_RUNTIME_DIR/asha-review-$ASHA_CONTROL_TASK_ID.json`, then run "
        "`asha task report --file "
        "$XDG_RUNTIME_DIR/asha-review-$ASHA_CONTROL_TASK_ID.json`."
        if node["type"] == "review" else
        "Required: run `jj status` in this workspace to snapshot before "
        "`asha task report`, and after any later edit."
    )
    publication_contract = (
        "Before exiting, write the bounded result document outside this "
        "workspace to "
        "`$XDG_RUNTIME_DIR/asha-review-$ASHA_CONTROL_TASK_ID.json`. Do not "
        "create `.asha/result.json` or any other review-workspace artifact."
        if node["type"] == "review" else
        "Before exiting, write the bounded result document to "
        ".asha/result.json (the private `.asha/` directory inside this "
        "workspace is ignored by the repository, so the result file never "
        "enters your sealed diff; a result file placed anywhere tracked is a "
        "hard-scope violation and fails the seal) and run:\n\n"
        "```text\nasha task report --file .asha/result.json\n```"
    )
    text = f"""# Asha Orchestration Assignment

## Identity

- Initiative: {initiative['slug']} ({initiative['initiative_id']})
- Node: {node['node_id']}
- Attempt: {attempt['attempt_id']}

## Objective

{_truncate(initiative['objective'], 4096)}

## Node goal

{_truncate(node['goal'], 3000)}

## Repository and immutable base

- Repository root: {_node_repository(initiative, node)['root']}
- Repository ID: {_node_repository(initiative, node)['repository_id']}
- Base policy: {base['policy']}
- Exact base commit: {exact_base}
- Scope-origin tree digest: {base['scope_origin']['tree_digest']}
- Upstream seal inputs:
{_json_lines(seal_facts or base['seal_inputs'], 3000)}
- Read-only failure seal inputs:
{_json_lines(read_only_facts, 3000)}

## Scope

Hard write scope:
{_json_lines(node['hard_write_scope'], 2500)}

Advisory path ownership:
{_json_lines(node['advisory_path_ownership'], 2500)}

## Dependencies

Declared node dependencies:
{_json_lines(node['dependencies'], 1500)}

Upstream result summaries and their payload digests are embedded in the exact
immutable seal inputs above.
{composition}{review_contract}

## Acceptance criteria

Initiative criteria:
{_json_lines(initiative['acceptance_criteria'], 4000)}

Node acceptance:
{_truncate(node['acceptance'] or 'None declared.', 2048)}

## Verification commands

No node-level command argv exists in the approved Core node record. Run the
repository checks required by the acceptance criteria and report their exact
argv and exit status.

## Role and workflow

- Role: {node['role']}
- Workflow: {node['workflow']}
- Nested workflow policy: {nested['workflow']}
- Nested single writer: {str(nested['single_writer']).lower()}

Nested workflows are prohibited unless the approved policy above names the
same workflow. They may not create another Control task, workspace, tmux
session, coordinator, or unmanaged parallel writer.

## Prohibited actions

{_json_lines(initiative['forbidden_action_classes'], 1500)}

## Result publication contract

{publication_contract}

{publication_workspace_rule}
The controller never snapshots or otherwise mutates the worker workspace on
the worker's behalf.

The client document is `asha.orchestration-result.v1` with every result field
except controller-generated `result_id` and `payload_digest`: `publication_id`,
`supersedes_result_id`, the initiative/node/attempt/task/run identities above,
`claim_status` (`completed|failed|blocked|needs-decision`), `summary`,
`files_changed`, `verification_attestations`, `concerns`, `follow_up`, and
`published_at`. Paths are canonical repository-relative paths inside the task
workspace. Reuse a publication ID only to replay the identical document.
"""
    raw = text.encode("utf-8")
    if len(raw) > MAX_ASSIGNMENT_BYTES:
        raise SchedulerError(
            f"generated assignment exceeds {MAX_ASSIGNMENT_BYTES} bytes"
        )
    return raw


def _exact_base(
    store: InitiativeStore, initiative_id: str, node: dict[str, Any], attempt: dict[str, Any]
) -> str:
    base = attempt["base"]
    if node["type"] == "compose":
        from .composition import CompositionError, composition_inputs

        try:
            composition_inputs(store, initiative_id, node, attempt)
        except CompositionError as exc:
            raise SchedulerError(str(exc)) from exc
        return base["scope_origin"]["jj_commit_id"]
    if base["policy"] in {"approved-baseline", "scope-baseline"}:
        return base["scope_origin"]["jj_commit_id"]
    seal_ids = [item["seal_id"] for item in base["seal_inputs"]]
    seals = [
        seal for seal in store.list_seals_snapshot(initiative_id)
        if seal["seal_id"] in seal_ids
    ]
    by_id = {seal["seal_id"]: seal for seal in seals}
    for item in base["seal_inputs"]:
        seal = by_id.get(item["seal_id"])
        if seal is None or seal["outcome"] != item["outcome"]:
            raise SchedulerError("upstream seal input identity or outcome changed")
        if seal["scope_origin"] != item["scope_origin"]:
            raise SchedulerError("upstream seal input scope origin changed")
    inheritable = [
        seal for seal in seals
        if seal["outcome"] == "success"
        or (
            seal["outcome"] == "paused"
            and any(
                item["seal_id"] == seal["seal_id"] and not item["read_only"]
                for item in base["seal_inputs"]
            )
        )
    ]
    if any(seal["outcome"] == "paused" for seal in inheritable):
        if len(inheritable) != 1 or base["upstream_node_ids"]:
            raise SchedulerError(
                "paused continuation requires exactly one explicit paused seal"
            )
        return inheritable[0]["jj_commit_id"]
    successful = sorted(
        inheritable, key=lambda seal: (seal["sealed_at"], seal["seal_id"]),
    )
    commits = {seal["jj_commit_id"] for seal in successful}
    if len(commits) != 1:
        raise SchedulerError(
            "upstream-seal dispatch requires one exact successful upstream commit"
        )
    return next(iter(commits))


def _attempt_base(plan: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    if node["base"] is not None:
        return copy.deepcopy(node["base"])
    origins = [
        item["base"]["scope_origin"]
        for item in plan["nodes"]
        if item["type"] in MUTATING_NODE_TYPES and item["base"] is not None
    ]
    if not origins:
        raise SchedulerError("read-only node has no approved repository baseline")
    return {
        "policy": "approved-baseline",
        "scope_origin": copy.deepcopy(origins[0]),
        "upstream_node_ids": [],
        "seal_inputs": [],
    }


def _resolved_attempt_base(
    store: InitiativeStore,
    initiative_id: str,
    plan: dict[str, Any],
    node: dict[str, Any],
) -> dict[str, Any]:
    """Fix plan-level upstream node references to exact immutable success seals."""
    if node["type"] == "review":
        from .review import ReviewError, review_target

        initiative = store.peek(initiative_id)
        try:
            seal, _target = review_target(store, initiative, plan, node)
        except ReviewError as exc:
            raise SchedulerError(str(exc)) from exc
        return {
            "policy": "upstream-seal",
            "scope_origin": copy.deepcopy(seal["scope_origin"]),
            "upstream_node_ids": [seal["node_id"]],
            "seal_inputs": [{
                "seal_id": seal["seal_id"],
                "outcome": "success",
                "read_only": False,
                "scope_origin": copy.deepcopy(seal["scope_origin"]),
            }],
        }
    base = _attempt_base(plan, node)
    if base["policy"] != "upstream-seal" or base["seal_inputs"]:
        return base
    available = store.list_seals_snapshot(initiative_id)
    inputs: list[dict[str, Any]] = []
    for upstream_node_id in base["upstream_node_ids"]:
        candidates = sorted(
            (
                seal for seal in available
                if seal["node_id"] == upstream_node_id
                and seal["outcome"] == "success"
                and seal["scope_origin"] == base["scope_origin"]
            ),
            key=lambda seal: (seal["sealed_at"], seal["seal_id"]),
        )
        if not candidates:
            raise SchedulerError(
                f"upstream node {upstream_node_id} has no exact success seal"
            )
        seal = candidates[-1]
        inputs.append({
            "seal_id": seal["seal_id"],
            "outcome": "success",
            "read_only": False,
            "scope_origin": copy.deepcopy(seal["scope_origin"]),
        })
    base["seal_inputs"] = inputs
    return base


def _goal(initiative: dict[str, Any], node: dict[str, Any], path: Path) -> str:
    del node
    if not path.is_absolute():
        raise SchedulerError("Control assignment path must be absolute")
    tail = f"{path.stem} {path}"
    minimum = f"orch {tail}"
    if len(minimum) > 200:
        raise SchedulerError(
            "absolute assignment path and attempt identity exceed Control's 200-character goal limit"
        )
    available = 200 - len(minimum) - 1
    slug = initiative["slug"][:min(24, max(0, available))].rstrip("-")
    return f"orch {slug} {tail}" if slug else minimum


def validate_goal_capacity(
    config: OrchestrationConfig,
    initiative: dict[str, Any],
    nodes: list[dict[str, Any]],
) -> None:
    """Refuse plans whose immutable assignment path cannot fit Control's goal."""
    attempt_id = "00000000-0000-4000-8000-000000000000"
    path = (
        config.initiatives_dir / initiative["initiative_id"] / "assignments"
        / f"{attempt_id}.md"
    )
    for node in nodes:
        _goal(initiative, node, path)


def _control_creation_evidence(
    config: OrchestrationConfig, task_id: str,
) -> tuple[bool, str]:
    """Return whether Control may have created the reserved task identity."""
    try:
        task = TaskStore(config.control).peek(task_id)
    except StoreError as exc:
        if str(exc) != f"task not found: {task_id}":
            return True, f"Control task probe is indeterminate: {exc}"
    else:
        return True, f"Control task exists with lifecycle {task['lifecycle']}"
    try:
        journal = CreationJournalStore(config.control).read(task_id)
    except JournalError as exc:
        if "not found" not in str(exc):
            return True, f"Control creation journal probe is indeterminate: {exc}"
    else:
        return True, f"Control creation journal exists at phase {journal['phase']}"
    return False, "Control has no task or creation journal for the reserved ID"


def _node_repository(initiative: Mapping[str, Any], node: Mapping[str, Any]) -> dict[str, Any]:
    """The scope member a node binds; single-repository initiatives have exactly one."""
    from .model import repository_by_id, scope_repositories

    repository_id = node.get("repository_id")
    if repository_id is None:
        return scope_repositories(initiative)[0]
    return repository_by_id(initiative, repository_id)


def _asha_executable() -> Path:
    raw = os.environ.get("ASHA_ROOT")
    root = Path(raw).resolve() if raw else Path(__file__).resolve().parents[3]
    if raw and (not Path(raw).is_absolute() or root != Path(raw)):
        raise SchedulerError("ASHA_ROOT must be an exact canonical absolute path")
    return root / "bin" / "asha"


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SchedulerError(f"Control response contains duplicate key: {key}")
        value[key] = item
    return value


def _parse_start(stdout: bytes, task_id: str) -> dict[str, Any]:
    try:
        value = json.loads(stdout.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SchedulerError(f"Control start returned invalid JSON: {exc}") from exc
    expected = {
        "contract", "task", "run", "workspace", "session", "pane", "attach",
        "source_mutations", "existing",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise SchedulerError("Control start returned a non-v1 closed payload")
    if value["contract"] != "asha.control-task-start.v1":
        raise SchedulerError("Control start contract is incompatible")
    if not isinstance(value["existing"], bool):
        raise SchedulerError("Control start existing field must be boolean")
    try:
        task = validate_task(value["task"])
    except ValueError as exc:
        raise SchedulerError(f"Control start returned an invalid task: {exc}") from exc
    if task["task_id"] != task_id:
        raise SchedulerError("Control start returned another task identity")
    if not task["runs"] or value["run"] != task["runs"][0]:
        raise SchedulerError("Control start primary run disagrees with its task")
    return value


def _transition_attempt(
    store: InitiativeStore, initiative_id: str, attempt: dict[str, Any], state: str
) -> dict[str, Any]:
    if attempt["state"] == state:
        return attempt
    changed = copy.deepcopy(attempt)
    changed.update({"state": state, "updated_at": _now()})
    validate_attempt(changed)
    store.save_attempt(
        initiative_id, changed, expected_digest=record_digest(attempt),
    )
    return changed


def _transition_node(
    store: InitiativeStore, initiative_id: str, node: dict[str, Any], state: str
) -> dict[str, Any]:
    if node["state"] == state:
        return node
    changed = copy.deepcopy(node)
    changed["state"] = state
    validate_node(changed)
    store.save_node(initiative_id, changed, expected_digest=record_digest(node))
    return changed


def _diagnostic(stderr: bytes) -> str:
    text = stderr[:4096].decode("utf-8", errors="replace")
    return "".join(char if char.isprintable() else "?" for char in text).strip()


def _mark_indeterminate(
    store: InitiativeStore,
    initiative_id: str,
    action: dict[str, Any],
    attempt: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    from .actions import append_event, set_action_state

    if attempt["state"] == "dispatching":
        attempt = _transition_attempt(store, initiative_id, attempt, "indeterminate")
    if action["state"] == "dispatching":
        action = set_action_state(
            store, action, "indeterminate",
            {**json.loads(action["outcome"]), "status": "indeterminate", "reason": reason},
        )
        append_event(
            store, initiative_id, "action-indeterminate", [action["action_id"]],
            {"reason": reason}, actor_kind="controller", actor_id="scheduler",
        )
    return action


def mark_launch_failed(
    store: InitiativeStore,
    initiative_id: str,
    action: dict[str, Any],
    attempt: dict[str, Any],
    node: dict[str, Any],
    reason: str,
    *,
    reconciled: bool,
) -> dict[str, Any]:
    from .actions import append_event, set_action_state

    if attempt["state"] == "allocated":
        attempt = _transition_attempt(store, initiative_id, attempt, "indeterminate")
        attempt = _transition_attempt(store, initiative_id, attempt, "launch-failed")
    elif attempt["state"] == "indeterminate":
        attempt = _transition_attempt(store, initiative_id, attempt, "launch-failed")
    elif attempt["state"] == "dispatching":
        attempt = _transition_attempt(store, initiative_id, attempt, "launch-failed")
    if node["state"] in {"ready", "dispatching"}:
        prior_node_state = node["state"]
        node = _transition_node(store, initiative_id, node, "evaluating")
        append_event(
            store, initiative_id, "node-state-changed", [node["node_id"], attempt["attempt_id"]],
            {"from": prior_node_state, "to": "evaluating", "reason": "launch-failed"},
            actor_kind="controller", actor_id="scheduler",
        )
    target = "refused" if reconciled and action["state"] == "indeterminate" else "completed"
    action = set_action_state(
        store, action, target,
        {**json.loads(action["outcome"]), "status": "launch-failed", "reason": reason},
    )
    if target == "refused":
        append_event(
            store, initiative_id, "action-refused", [action["action_id"]],
            {"action_class": action["action_class"], "reason": reason},
            actor_kind="controller", actor_id="action-reconciler",
        )
    return action


def dispatch(
    store: InitiativeStore,
    config: OrchestrationConfig,
    initiative_id: str,
    node_id: str,
    *,
    action: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch one reserved action through Control's create-by-ID CLI seam."""
    from .actions import (
        ActionRefused,
        append_event,
        consume_salvage_approval,
        salvage_dispatch_binding,
        set_action_state,
    )

    with store.transaction_lock(initiative_id):
        initiative = store.peek(initiative_id)
        plan = _active_plan(store, initiative)
        if action["initiative_id"] != initiative_id:
            raise SchedulerError("action belongs to another initiative")
        if action["active_plan_digest"] != plan["digest"]:
            raise SchedulerError("action active plan digest is stale")
        node = store.read_node(initiative_id, node_id)
        attempts = store.list_attempts_snapshot(initiative_id)
        salvage_approval: dict[str, Any] | None = None
        salvage_base: dict[str, Any] | None = None
        if action["state"] == "validated":
            action_payload = json.loads(action["outcome"]).get("payload", {})
            salvage_request_id = action_payload.get("salvage_request_id")
            if salvage_request_id is not None:
                try:
                    salvage_approval, salvage_base, _ = salvage_dispatch_binding(
                        store, initiative, node, salvage_request_id,
                        dispatch_expected_revision=action["expected_state_revision"],
                        dispatch_action_id=action["action_id"],
                    )
                except (ActionRefused, StoreError, ValueError) as exc:
                    raise SchedulerError(str(exc)) from exc
            node_attempts = [item for item in attempts if item["node_id"] == node_id]
            allocated = [item for item in node_attempts if item["state"] == "allocated"]
            if len(allocated) > 1:
                raise SchedulerError("node has multiple allocated attempt reservations")
            reserved = allocated[0] if allocated else None
            if salvage_approval is not None and reserved is not None:
                raise SchedulerError(
                    "salvage dispatch cannot substitute an existing attempt reservation"
                )
            breaker = _dispatch_breaker(
                store, initiative, plan, attempts, has_reservation=reserved is not None,
            )
            if breaker is not None:
                event_type, reason = breaker
                pause_for_breaker(store, initiative_id, reason, event_type=event_type)
                raise SchedulerError(reason)
            if initiative["state"] != "running":
                raise SchedulerError("initiative must be running to dispatch")
            if node["state"] != "ready" or readiness(store, initiative).get(node_id) != "ready":
                raise SchedulerError("node is not deterministically ready")
            if len(_active_attempts(attempts)) >= _limit(initiative, plan, "max_parallel"):
                raise SchedulerError("initiative max_parallel limit reached")
            gate_attempt_ids, pending_gate_reruns = _gate_rerun_attempt_ids(
                store, initiative_id, node_id, attempts,
            )
            ordinary_attempts = sum(
                1 for item in node_attempts
                if item["attempt_id"] not in gate_attempt_ids
            )
            if reserved is None and salvage_approval is None:
                retained_success = sorted(
                    (
                        seal for seal in store.list_seals_snapshot(initiative_id)
                        if seal["node_id"] == node_id and seal["outcome"] == "success"
                    ),
                    key=lambda item: (item["sealed_at"], item["seal_id"]),
                )
                if retained_success:
                    raise SchedulerError(
                        "use repair-node with seal " + retained_success[-1]["seal_id"]
                    )
            if (
                reserved is None
                and pending_gate_reruns == 0
                and ordinary_attempts >= _limit(
                    initiative, plan, "max_attempts_per_node",
                )
            ):
                raise SchedulerError("node max_attempts_per_node exhausted")
            if (
                reserved is None
                and pending_gate_reruns > 0
                and len(gate_attempt_ids) >= _limit(
                    initiative, plan, "max_repair_cycles",
                )
            ):
                raise SchedulerError("initiative max_repair_cycles exhausted")
            if reserved is not None:
                bound_id = reserved["action_id"]
                if bound_id not in {None, action["action_id"]}:
                    try:
                        bound = store.read_action(initiative_id, bound_id)
                    except StoreError as exc:
                        if "not found" not in str(exc):
                            raise SchedulerError(
                                f"cannot inspect allocated attempt action: {exc}"
                            ) from exc
                    else:
                        if bound["state"] not in {"completed", "refused"}:
                            raise SchedulerError(
                                "allocated attempt belongs to a nonterminal dispatch action"
                            )
                attempt = reserved
            else:
                at = _now()
                attempt = validate_attempt({
                    "contract": ATTEMPT_CONTRACT,
                    "attempt_id": str(uuid.uuid4()),
                    "initiative_id": initiative_id,
                    "node_id": node_id,
                    "task_id": str(uuid.uuid4()),
                    "action_id": action["action_id"],
                    "ordinal": len(node_attempts) + 1,
                    "base": (
                        copy.deepcopy(salvage_base) if salvage_base else
                        _resolved_attempt_base(store, initiative_id, plan, node)
                    ),
                    "state": "allocated",
                    "result_publication_id": None,
                    "result_id": None,
                    "seal_id": None,
                    "created_at": at,
                    "updated_at": at,
                })
            outcome = json.loads(action["outcome"])
            outcome.update({
                "node_id": node_id,
                "attempt_id": attempt["attempt_id"],
                "control_task_id": attempt["task_id"],
                "status": "dispatching",
            })
            if salvage_approval is not None:
                outcome.update({
                    "salvage_request_id": salvage_approval["request_id"],
                    "read_only_failure_seal_id": salvage_base["seal_inputs"][0]["seal_id"],
                })
            action = set_action_state(store, action, "dispatching", outcome)
            if reserved is None:
                store.save_attempt(initiative_id, attempt)
            elif attempt["action_id"] != action["action_id"]:
                bound_attempt = copy.deepcopy(attempt)
                bound_attempt.update({"action_id": action["action_id"], "updated_at": _now()})
                store.save_attempt(
                    initiative_id, bound_attempt, expected_digest=record_digest(attempt),
                )
                attempt = bound_attempt
            if salvage_approval is not None:
                consume_salvage_approval(store, initiative_id, salvage_approval)
        elif action["state"] == "indeterminate":
            outcome = json.loads(action["outcome"])
            if outcome.get("node_id") != node_id:
                raise SchedulerError("indeterminate action node reservation changed")
            attempt = store.read_attempt(initiative_id, outcome["attempt_id"])
            if attempt["task_id"] != outcome.get("control_task_id"):
                raise SchedulerError("indeterminate action task reservation changed")
            if attempt["state"] == "allocated" and attempt["action_id"] != action["action_id"]:
                bound_attempt = copy.deepcopy(attempt)
                bound_attempt.update({"action_id": action["action_id"], "updated_at": _now()})
                store.save_attempt(
                    initiative_id, bound_attempt, expected_digest=record_digest(attempt),
                )
                attempt = bound_attempt
            salvage_request_id = outcome.get("salvage_request_id")
            if salvage_request_id is not None:
                try:
                    salvage_approval, salvage_base, _ = salvage_dispatch_binding(
                        store, initiative, node, salvage_request_id,
                        allow_consumed=True,
                        dispatch_expected_revision=action["expected_state_revision"],
                        dispatch_action_id=action["action_id"],
                    )
                except (ActionRefused, StoreError, ValueError) as exc:
                    raise SchedulerError(str(exc)) from exc
                if attempt["base"] != salvage_base:
                    raise SchedulerError("retained salvage attempt base binding changed")
                if salvage_approval["state"] == "approved":
                    consume_salvage_approval(store, initiative_id, salvage_approval)
        else:
            raise SchedulerError("dispatch requires a validated or indeterminate action")

        try:
            exact_base = _exact_base(store, initiative_id, node, attempt)
            assignment_path = (
                config.initiatives_dir / initiative_id / "assignments"
                / f"{attempt['attempt_id']}.md"
            )
            resolved_seals = []
            for item in attempt["base"]["seal_inputs"]:
                seal = store.read_seal(initiative_id, item["seal_id"])
                result_summary = None
                if seal["result_id"] is not None:
                    result = store.read_result(initiative_id, seal["result_id"])
                    result_summary = {
                        "result_id": result["result_id"],
                        "payload_digest": result["payload_digest"],
                        "claim_status": result["claim_status"],
                        "summary": result["summary"],
                        "concerns": result["concerns"],
                        "follow_up": result["follow_up"],
                    }
                resolved_seals.append({
                    **copy.deepcopy(item),
                    "jj_commit_id": seal["jj_commit_id"],
                    "tree_digest": seal["tree_digest"],
                    "diff_digest": seal["diff_digest"],
                    "base_seal_ids": list(seal["base"]["seal_ids"]),
                    "changed_paths": seal["changed_paths"],
                    "cumulative_changed_paths": seal["cumulative_changed_paths"],
                    "result": result_summary,
                })
            assignment = assignment_bytes(
                initiative, plan, node, attempt, exact_base, resolved_seals,
            )
            asha = _asha_executable()
            goal = _goal(initiative, node, assignment_path)
            repository_root = _node_repository(initiative, node)["root"]
            argv = [
                str(asha), "task", "start",
                "--repo", repository_root,
                "--task-id", attempt["task_id"],
                "--base", exact_base,
                "--harness", node["harness"],
                "--role", node["role"],
                "--detach", "--json",
                "--goal", goal,
            ]
            store.write_assignment(initiative_id, attempt["attempt_id"], assignment)
            if attempt["state"] == "allocated":
                attempt = _transition_attempt(store, initiative_id, attempt, "dispatching")
            if node["state"] == "ready":
                node = _transition_node(store, initiative_id, node, "dispatching")
                append_event(
                    store, initiative_id, "node-state-changed", [node_id],
                    {"from": "ready", "to": "dispatching"},
                    actor_kind="controller", actor_id="scheduler",
                )
        except (SchedulerError, StoreError, OSError, ValueError) as exc:
            node = store.read_node(initiative_id, node_id)
            attempt = store.read_attempt(initiative_id, attempt["attempt_id"])
            action = mark_launch_failed(
                store, initiative_id, action, attempt, node, str(exc)[:1000],
                reconciled=action["state"] == "indeterminate",
            )
            return {
                "contract": DISPATCH_CONTRACT,
                "action": action,
                "attempt": store.read_attempt(initiative_id, attempt["attempt_id"]),
                "link": None,
                "control": None,
            }
        try:
            returncode, stdout, stderr = capture_bytes(
                argv,
                cwd=Path(repository_root),
                limit=MAX_CONTROL_OUTPUT_BYTES,
                runner=None,
                error_type=SchedulerError,
                deadline_seconds=DISPATCH_TIMEOUT_SECONDS,
            )
        except SchedulerError as exc:
            action = _mark_indeterminate(
                store, initiative_id, action, attempt, str(exc)[:1000],
            )
            return {
                "contract": DISPATCH_CONTRACT,
                "action": action,
                "attempt": store.read_attempt(initiative_id, attempt["attempt_id"]),
                "link": None,
                "control": None,
            }
        if returncode != 0:
            reason = _diagnostic(stderr) or f"Control task start exited {returncode}"
            created, evidence = _control_creation_evidence(config, attempt["task_id"])
            if created:
                action = _mark_indeterminate(
                    store, initiative_id, action, attempt,
                    f"{reason}; {evidence}; run asha task recover {attempt['task_id']}"[:1000],
                )
            else:
                action = mark_launch_failed(
                    store, initiative_id, action, attempt, node, reason,
                    reconciled=action["state"] == "indeterminate",
                )
            return {
                "contract": DISPATCH_CONTRACT,
                "action": action,
                "attempt": store.read_attempt(initiative_id, attempt["attempt_id"]),
                "link": None,
                "control": {"returncode": returncode, "diagnostic": reason},
            }
        try:
            control = _parse_start(stdout, attempt["task_id"])
            link = build_link(initiative, node, attempt, action, control["task"])
            try:
                stored_link = store.read_link(initiative_id, attempt["attempt_id"])
            except StoreError:
                store.save_link(initiative_id, link)
                stored_link = link
            if stored_link != link:
                raise SchedulerError("retained Control link differs from replayed task")
        except (SchedulerError, StoreError, OSError, ValueError) as exc:
            action = _mark_indeterminate(
                store, initiative_id, action, attempt, str(exc)[:1000],
            )
            return {
                "contract": DISPATCH_CONTRACT,
                "action": action,
                "attempt": store.read_attempt(initiative_id, attempt["attempt_id"]),
                "link": None,
                "control": None,
            }
        try:
            attempt = store.read_attempt(initiative_id, attempt["attempt_id"])
            if attempt["state"] in {"dispatching", "indeterminate"}:
                attempt = _transition_attempt(store, initiative_id, attempt, "running")
            node = store.read_node(initiative_id, node_id)
            if node["state"] == "dispatching":
                node = _transition_node(store, initiative_id, node, "running")
                append_event(
                    store, initiative_id, "node-state-changed",
                    [node_id, attempt["attempt_id"]],
                    {"from": "dispatching", "to": "running"},
                    actor_kind="controller", actor_id="scheduler",
                )
            append_event(
                store, initiative_id, "attempt-started",
                [node_id, attempt["attempt_id"], attempt["task_id"]],
                {
                    "existing": control["existing"],
                    "control_task_record_digest": task_digest(control["task"]),
                },
                actor_kind="controller", actor_id="scheduler",
            )
            if node["type"] == "review":
                from .review import register_review_attempt

                register_review_attempt(
                    store, initiative, plan, node, attempt, control["task"],
                )
            action = store.read_action(initiative_id, action["action_id"])
            action = set_action_state(
                store, action, "completed",
                {
                    **json.loads(action["outcome"]),
                    "status": "running",
                    "existing": control["existing"],
                },
            )
        except (SchedulerError, StoreError, OSError, ValueError) as exc:
            action = store.read_action(initiative_id, action["action_id"])
            if action["state"] != "completed":
                attempt = store.read_attempt(initiative_id, attempt["attempt_id"])
                action = _mark_indeterminate(
                    store, initiative_id, action, attempt,
                    f"post-link state persistence is indeterminate: {exc}"[:1000],
                )
            return {
                "contract": DISPATCH_CONTRACT,
                "action": action,
                "attempt": store.read_attempt(initiative_id, attempt["attempt_id"]),
                "link": stored_link,
                "control": control,
            }
        return {
            "contract": DISPATCH_CONTRACT,
            "action": action,
            "attempt": attempt,
            "link": stored_link,
            "control": control,
        }


__all__ = [
    "DISPATCH_CONTRACT", "DISPATCH_TIMEOUT_SECONDS", "MAX_ASSIGNMENT_BYTES",
    "READINESS_CONTRACT", "SchedulerError", "assignment_bytes",
    "consecutive_failures", "dispatch", "mark_launch_failed",
    "pause_for_breaker", "readiness", "refresh_readiness", "validate_goal_capacity",
]
