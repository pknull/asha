"""Controller-owned immutable artifact sealing and drift evidence."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..jj import ImmutableTree, JjAdapter, JjError
from ..store import StoreError, TaskStore
from .actions import (
    _invalidate_candidate_records,
    _repair_lineage_roots,
    action_outcome,
    append_event,
)
from .links import control_task_identity_digest
from .model import (
    EVIDENCE_CONTRACT,
    EVENT_CONTRACT,
    MAX_PATH_ITEMS,
    SEAL_CONTRACT,
    SEAL_PREPARATION_CONTRACT,
    ModelError,
    new_uuid,
    record_digest,
    validate_attempt,
    validate_evidence,
    validate_event,
    validate_initiative,
    validate_node,
    validate_seal,
    validate_seal_preparation,
)
from .store import InitiativeStore


TASK_SEAL_CONTRACT = "asha.orchestration-task-seal.v1"
MAX_SEAL_EVIDENCE_BYTES = 128 * 1024
MAX_EVENT_PATHS = 32
_EVIDENCE_PATH_FIELDS = (
    "scope_violations", "advisory_divergence", "claimed-but-unsealed",
    "changed_paths", "cumulative_changed_paths",
)


class SealError(ValueError):
    """A seal cannot be prepared from the retained controller evidence."""


class NoSealableArtifact(SealError):
    """Final read-only evidence proves that no retained jj artifact exists."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _tree_map(tree: ImmutableTree) -> dict[str, tuple[str, str]]:
    return {
        path: (mode, blob_id)
        for path, mode, blob_id in tree.entries
    }


def _diff(
    before: ImmutableTree,
    after: ImmutableTree,
) -> tuple[list[str], str]:
    before_map = _tree_map(before)
    after_map = _tree_map(after)
    changed = sorted(
        path for path in set(before_map) | set(after_map)
        if before_map.get(path) != after_map.get(path)
    )
    facts = [
        {
            "path": path,
            "before": before_map.get(path),
            "after": after_map.get(path),
        }
        for path in changed
    ]
    return changed, hashlib.sha256(_canonical(facts)).hexdigest()


def _in_scope(path: str, scopes: list[str]) -> bool:
    return any(
        scope == "." or path == scope or path.startswith(scope.rstrip("/") + "/")
        for scope in scopes
    )


def _process_kind(reconciliation: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Classify Control terminal facts, preferring structured evidence state.

    Current Control reconciliation carries ``exited``/``failed`` on the task
    and primary run, with a process-evidence state when live facts permit it.
    Exact legacy dead-status/signal text is accepted only as a defensive
    fallback; unknown terminal prose fails closed when no structured state is
    available.
    """
    state = reconciliation.get("state")
    run_evidence = []
    runs = reconciliation.get("runs")
    if isinstance(runs, list) and runs:
        run = runs[0] if isinstance(runs[0], Mapping) else {}
        run_evidence = run.get("evidence", [])
        run_state = run.get("state")
    else:
        run_evidence = reconciliation.get("evidence", [])
        run_state = None
    structured_states: set[str] = set()
    parsed_signals: set[str] = set()
    parsed_statuses: set[int] = set()
    for item in run_evidence if isinstance(run_evidence, list) else []:
        if not isinstance(item, Mapping):
            continue
        if (
            item.get("source") == "process"
            and item.get("outcome") == "missing"
            and item.get("state") in {"exited", "failed"}
        ):
            structured_states.add(str(item["state"]))
        detail = item.get("detail")
        if not isinstance(detail, str):
            continue
        signal_match = re.fullmatch(
            r"tmux pane process was killed by signal ([A-Za-z0-9+-]{1,32})",
            detail,
        )
        status_match = re.fullmatch(
            r"tmux pane process exited with status (-?[0-9]{1,10})",
            detail,
        )
        if signal_match:
            parsed_signals.add(signal_match.group(1))
        if status_match:
            parsed_statuses.add(int(status_match.group(1)))
    if len(structured_states) > 1 or len(parsed_signals) > 1 or len(parsed_statuses) > 1:
        raise SealError("terminal Control process evidence is contradictory")
    structured_state = next(iter(structured_states), None)
    signal = next(iter(parsed_signals), None)
    exit_status = next(iter(parsed_statuses), None)
    if signal is not None and exit_status is not None:
        raise SealError("terminal Control process evidence has both status and signal")
    structured_terminal = (
        run_state if run_state in {"exited", "failed"}
        else state if state in {"exited", "failed"}
        else structured_state
    )
    if (
        run_state in {"exited", "failed"}
        and state in {"exited", "failed"}
        and run_state != state
    ):
        raise SealError("Control task and primary-run terminal states disagree")
    if structured_terminal == "exited":
        if signal is not None or exit_status not in {None, 0}:
            raise SealError("structured Control exit state contradicts terminal detail")
        exit_status = 0
        kind = "normal"
    elif structured_terminal == "failed":
        if exit_status == 0:
            raise SealError("structured Control failure state contradicts zero exit status")
        kind = "abnormal"
    elif signal is not None:
        kind = "abnormal"
    elif exit_status is not None:
        kind = "normal" if exit_status == 0 else "abnormal"
    else:
        raise SealError("terminal Control evidence has no structured exit fact")
    expected_state = "exited" if kind == "normal" else "failed"
    if state in {"exited", "failed"} and state != expected_state:
        raise SealError("seal preparation requires terminal Control process evidence")
    return kind, {
        "control_state": state,
        "primary_run_state": run_state,
        "structured_process_state": structured_state,
        "exit_status": exit_status,
        "signal": signal,
        "evidence": run_evidence,
    }


def _base_binding(
    store: InitiativeStore,
    initiative_id: str,
    node: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], str]:
    base = attempt["base"]
    inputs = base["seal_inputs"]
    failures = sorted(
        item["seal_id"] for item in inputs
        if item["outcome"] == "failure" and item["read_only"]
    )
    if base["policy"] in {"approved-baseline", "scope-baseline"}:
        return ({
            "kind": "repository-baseline",
            "jj_commit_id": base["scope_origin"]["jj_commit_id"],
            "tree_digest": base["scope_origin"]["tree_digest"],
            "seal_ids": [],
        }, failures, base["scope_origin"]["jj_commit_id"])
    seals = []
    for item in inputs:
        if item["read_only"]:
            continue
        try:
            seal = store.read_seal(initiative_id, item["seal_id"])
        except StoreError as exc:
            raise SealError(f"attempt base seal cannot be read: {exc}") from exc
        if seal["outcome"] != item["outcome"] or seal["scope_origin"] != base["scope_origin"]:
            raise SealError("attempt base seal binding changed")
        seals.append(seal)
    if not seals:
        raise SealError("upstream-seal attempt has no exact retained base seal")
    if len(seals) == 1:
        seal = seals[0]
        return ({
            "kind": "seal",
            "jj_commit_id": seal["jj_commit_id"],
            "tree_digest": seal["tree_digest"],
            "seal_ids": [seal["seal_id"]],
        }, failures, seal["jj_commit_id"])
    if node.get("type") != "compose":
        raise SealError("multiple upstream seals require an explicit compose node")
    from .composition import CompositionError, composition_inputs

    try:
        seals = composition_inputs(store, initiative_id, node, attempt)
    except CompositionError as exc:
        raise SealError(str(exc)) from exc
    digest = hashlib.sha256(_canonical([
        [seal["seal_id"], seal["jj_commit_id"], seal["tree_digest"]]
        for seal in seals
    ])).hexdigest()
    return ({
        "kind": "composition-inputs",
        "jj_commit_id": None,
        "tree_digest": digest,
        "seal_ids": [seal["seal_id"] for seal in seals],
    }, failures, base["scope_origin"]["jj_commit_id"])


def _transition_attempt(
    store: InitiativeStore,
    initiative_id: str,
    attempt: dict[str, Any],
    target: str,
    *,
    seal_id: str | None = None,
) -> dict[str, Any]:
    if attempt["state"] == target and (seal_id is None or attempt["seal_id"] == seal_id):
        return attempt
    changed = copy.deepcopy(attempt)
    changed.update({"state": target, "updated_at": _now()})
    if seal_id is not None:
        changed["seal_id"] = seal_id
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


def _save_evidence(
    store: InitiativeStore,
    initiative_id: str,
    attempt_id: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    evidence_id = new_uuid()
    raw = _canonical(payload)
    if len(raw) > MAX_SEAL_EVIDENCE_BYTES:
        raise SealError(
            f"seal evidence exceeds {MAX_SEAL_EVIDENCE_BYTES} UTF-8 bytes"
        )
    summary = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    evidence = validate_evidence({
        "contract": EVIDENCE_CONTRACT,
        "evidence_id": evidence_id,
        "initiative_id": initiative_id,
        "kind": "seal-evidence",
        "subject_id": attempt_id,
        "digest": hashlib.sha256(raw).hexdigest(),
        "summary": summary,
        "recorded_at": _now(),
    })
    store.save_evidence(initiative_id, evidence)
    return evidence


def _bounded_path_evidence(
    payload: Mapping[str, Any], path_lists: Mapping[str, list[str]],
) -> dict[str, Any]:
    """Retain the largest common path prefix that fits immutable evidence."""
    if set(path_lists) != set(_EVIDENCE_PATH_FIELDS):
        raise SealError("seal evidence path fields are incomplete")
    digests = {
        field: hashlib.sha256(_canonical(paths)).hexdigest()
        for field, paths in path_lists.items()
    }

    def candidate(limit: int) -> dict[str, Any]:
        bounded = copy.deepcopy(dict(payload))
        for field in _EVIDENCE_PATH_FIELDS:
            paths = path_lists[field]
            retained = paths[:limit]
            bounded[field] = retained
            bounded[f"{field}_truncated"] = len(paths) - len(retained)
            bounded[f"{field}_digest"] = digests[field]
        return bounded

    empty = candidate(0)
    if len(_canonical(empty)) > MAX_SEAL_EVIDENCE_BYTES:
        raise SealError(
            "seal evidence non-path facts exceed the immutable evidence bound"
        )
    low, high = 0, MAX_PATH_ITEMS
    best = empty
    while low <= high:
        middle = (low + high) // 2
        bounded = candidate(middle)
        if len(_canonical(bounded)) <= MAX_SEAL_EVIDENCE_BYTES:
            best = bounded
            low = middle + 1
        else:
            high = middle - 1
    return best


def _bounded_event_paths(paths: list[str]) -> list[str]:
    if len(paths) <= MAX_EVENT_PATHS:
        return list(paths)
    return [*paths[:MAX_EVENT_PATHS], f"truncated: {len(paths) - MAX_EVENT_PATHS} more"]


def _seal_published_payload(
    seal: Mapping[str, Any], *, hard_scope_valid: bool,
    scope_violations: list[str], advisory_divergence: list[str],
) -> dict[str, Any]:
    return {
        "outcome": seal["outcome"],
        "scope_violations": _bounded_event_paths(scope_violations),
        "advisory_divergence": _bounded_event_paths(advisory_divergence),
        "hard_scope_valid": hard_scope_valid,
    }


def _prevalidate_seal_published_event(
    store: InitiativeStore, initiative_id: str, seal: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> None:
    initiative = store.peek(initiative_id)
    raw = _canonical(payload)
    validate_event({
        "contract": EVENT_CONTRACT,
        "sequence": initiative["last_event_sequence"] + 1,
        "event_id": new_uuid(),
        "initiative_id": initiative_id,
        "type": "seal-published",
        "actor_kind": "controller",
        "actor_id": "seal-controller",
        "subject_ids": [seal["node_id"], seal["attempt_id"], seal["seal_id"]],
        "payload_digest": hashlib.sha256(raw).hexdigest(),
        "payload": copy.deepcopy(dict(payload)),
        "recorded_at": _now(),
    })


def _retained_seal_paths(
    store: InitiativeStore, initiative_id: str, seal: Mapping[str, Any],
) -> tuple[list[str], list[str], bool, bool]:
    """Verify and recover bounded path facts for a retained seal record."""
    try:
        evidence = store.read_evidence(initiative_id, seal["process_evidence_id"])
        payload = json.loads(evidence["summary"])
    except (StoreError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SealError(f"retained seal path evidence cannot be read: {exc}") from exc
    if hashlib.sha256(_canonical(payload)).hexdigest() != evidence["digest"]:
        raise SealError("retained seal evidence digest does not match its summary")
    bounded: dict[str, tuple[list[str], int, str]] = {}
    for field in _EVIDENCE_PATH_FIELDS:
        paths = payload.get(field)
        truncated = payload.get(f"{field}_truncated")
        digest = payload.get(f"{field}_digest")
        if (
            not isinstance(paths, list)
            or not all(isinstance(item, str) for item in paths)
            or len(paths) > MAX_PATH_ITEMS
            or paths != sorted(set(paths))
            or isinstance(truncated, bool)
            or not isinstance(truncated, int)
            or not 0 <= truncated <= 2**31 - 1
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or (
                truncated == 0
                and digest != hashlib.sha256(_canonical(paths)).hexdigest()
            )
        ):
            raise SealError(f"retained seal {field} path evidence is invalid")
        bounded[field] = (paths, truncated, digest)
    changed, changed_truncated, changed_digest = bounded["changed_paths"]
    cumulative, cumulative_truncated, cumulative_digest = bounded[
        "cumulative_changed_paths"
    ]
    if (
        changed_digest != seal["changed_paths_digest"]
        or len(changed) + changed_truncated
        != len(seal["changed_paths"]) + seal["changed_paths_truncated"]
        or changed != seal["changed_paths"][:len(changed)]
        or cumulative_digest != seal["cumulative_changed_paths_digest"]
        or len(cumulative) + cumulative_truncated
        != len(seal["cumulative_changed_paths"])
        + seal["cumulative_changed_paths_truncated"]
        or cumulative != seal["cumulative_changed_paths"][:len(cumulative)]
    ):
        raise SealError("retained seal bounded paths disagree with immutable evidence")
    scope_paths, scope_truncated, _ = bounded["scope_violations"]
    advisory_paths, _advisory_truncated, _ = bounded["advisory_divergence"]
    claimed_paths, claimed_truncated, _ = bounded["claimed-but-unsealed"]
    has_scope_violations = bool(scope_paths or scope_truncated)
    if payload.get("hard_scope_valid") is not (not has_scope_violations):
        raise SealError("retained seal hard-scope fact disagrees with path evidence")
    return (
        scope_paths,
        advisory_paths,
        has_scope_violations,
        bool(claimed_paths or claimed_truncated),
    )


def _existing_preparation(
    store: InitiativeStore, initiative_id: str, attempt_id: str,
) -> dict[str, Any] | None:
    values = [
        item for item in store.list_seal_preparations_snapshot(initiative_id)
        if item["attempt_id"] == attempt_id
    ]
    if len(values) > 1:
        raise SealError("attempt has more than one seal preparation")
    return values[0] if values else None


def prepare_and_publish_seal(
    store: InitiativeStore,
    initiative_id: str,
    attempt_id: str,
    task: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
    *,
    jj: JjAdapter | None = None,
    phase_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Persist no-outcome intent, capture immutable jj evidence, and seal once."""
    adapter = jj or JjAdapter()
    hook = phase_hook or (lambda _phase, _record: None)
    with store.transaction_lock(initiative_id):
        initiative = store.peek(initiative_id)
        attempt = store.read_attempt(initiative_id, attempt_id)
        node = store.read_node(initiative_id, attempt["node_id"])
        link = store.read_link(initiative_id, attempt_id)
        if task["task_id"] != attempt["task_id"] or link["control_task_id"] != task["task_id"]:
            raise SealError("seal task identity differs from its attempt/link")
        if task["runs"][0]["run_id"] != reconciliation.get("runs", [{}])[0].get(
            "run_id", task["runs"][0]["run_id"]
        ):
            raise SealError("seal reconciliation belongs to another primary run")
        process_kind, process_facts = _process_kind(reconciliation)
        try:
            result = (
                store.read_result(initiative_id, attempt["result_id"])
                if attempt["result_id"] is not None else None
            )
        except StoreError as exc:
            raise SealError(f"accepted result cannot be read: {exc}") from exc
        if result is not None and (
            result["attempt_id"] != attempt_id
            or result["task_id"] != task["task_id"]
            or result["run_id"] != task["runs"][0]["run_id"]
        ):
            raise SealError("accepted result binding changed before sealing")
        base, failure_inputs, base_commit = _base_binding(
            store, initiative_id, node, attempt,
        )
        preparation = _existing_preparation(store, initiative_id, attempt_id)
        if preparation is None:
            process_evidence = _save_evidence(
                store, initiative_id, attempt_id,
                {"process": process_facts, "phase": "preparing"},
            )
            at = _now()
            seal_id = attempt["seal_id"] or new_uuid()
            preparation = validate_seal_preparation({
                "contract": SEAL_PREPARATION_CONTRACT,
                "seal_id": seal_id,
                "initiative_id": initiative_id,
                "node_id": attempt["node_id"],
                "attempt_id": attempt_id,
                "task_id": task["task_id"],
                "run_id": task["runs"][0]["run_id"],
                "repository_id": node["repository_id"],
                "scope_origin": copy.deepcopy(attempt["base"]["scope_origin"]),
                "base": copy.deepcopy(base),
                "read_only_failure_seal_ids": failure_inputs,
                "result_id": attempt["result_id"],
                "process_evidence_id": process_evidence["evidence_id"],
                "state": "preparing",
                "refusal": None,
                "created_at": at,
                "updated_at": at,
            })
            store.save_seal_preparation(initiative_id, preparation)
            append_event(
                store, initiative_id, "seal-preparing",
                [attempt["node_id"], attempt_id, seal_id],
                {"result_id": attempt["result_id"], "process_kind": process_kind},
                actor_kind="controller", actor_id="seal-controller",
            )
            hook("preparing", preparation)
        else:
            seal_id = preparation["seal_id"]
            expected_binding = (
                preparation["task_id"] == task["task_id"]
                and preparation["run_id"] == task["runs"][0]["run_id"]
                and preparation["result_id"] == attempt["result_id"]
                and preparation["base"] == base
                and preparation["read_only_failure_seal_ids"] == failure_inputs
            )
            if not expected_binding:
                raise SealError("retained seal preparation binding changed")
        if not any(
            event["type"] == "seal-preparing"
            and seal_id in event["subject_ids"]
            for event in store.list_events_snapshot(initiative_id)
        ):
            append_event(
                store, initiative_id, "seal-preparing",
                [attempt["node_id"], attempt_id, seal_id],
                {"result_id": attempt["result_id"], "process_kind": process_kind},
                actor_kind="controller", actor_id="seal-controller",
            )
        try:
            retained_seal = store.read_seal(initiative_id, seal_id)
        except StoreError as exc:
            if "not found" not in str(exc):
                raise SealError(f"preallocated seal path conflicts: {exc}") from exc
            retained_seal = None
        if retained_seal is None and preparation["state"] == "completed":
            raise SealError("completed seal preparation has no immutable seal record")
        if retained_seal is not None:
            (
                scope_violations, advisory_divergence,
                has_scope_violations, claimed_but_unsealed,
            ) = _retained_seal_paths(store, initiative_id, retained_seal)
            claim = None if result is None else result["claim_status"]
            hard_scope_valid = not has_scope_violations
            return _finish_published_seal(
                store, initiative, node, attempt, preparation, retained_seal,
                retriable=(
                    retained_seal["outcome"] == "failure"
                    and hard_scope_valid
                    and (
                        claim == "failed" or process_kind == "abnormal"
                        or result is None or claimed_but_unsealed
                    )
                ),
                hard_scope_valid=hard_scope_valid,
                scope_violations=scope_violations,
                advisory_divergence=advisory_divergence,
            )
        workspace = Path(task["jj"]["workspace_path"])
        try:
            identity = adapter.inspect_workspace(
                workspace, task["jj"]["workspace_name"], require_empty=False,
            )
            final_tree = adapter.immutable_tree(workspace, identity.commit_id)
            base_tree = adapter.immutable_tree(workspace, base_commit)
            origin_tree = adapter.immutable_tree(
                workspace, attempt["base"]["scope_origin"]["jj_commit_id"],
            )
        except (JjError, OSError, ValueError) as exc:
            changed_preparation = copy.deepcopy(preparation)
            changed_preparation.update({
                "state": "indeterminate", "refusal": str(exc)[:2048],
                "updated_at": _now(),
            })
            if preparation["state"] != "indeterminate":
                store.save_seal_preparation(
                    initiative_id, changed_preparation,
                    expected_digest=record_digest(preparation),
                )
            if not workspace.exists() and attempt["state"] in {
                "abnormal-exit", "result-missing",
            }:
                raise NoSealableArtifact(
                    f"retained task workspace has no sealable artifact: {exc}"
                ) from exc
            raise SealError(f"final jj identity is indeterminate: {exc}") from exc
        changed_paths, diff_digest = _diff(base_tree, final_tree)
        cumulative_changed, cumulative_digest = _diff(origin_tree, final_tree)
        scope_violations = [
            path for path in cumulative_changed
            if not _in_scope(path, node["hard_write_scope"])
        ]
        advisory_divergence = [
            path for path in changed_paths
            if not _in_scope(path, node["advisory_path_ownership"])
        ]
        clean_identity = (
            control_task_identity_digest(dict(task)) == link["control_task_identity_digest"]
            and identity.change_id == task["jj"]["change_id"]
            and task["jj"]["base_commit_id"] == base_commit
            and identity.parent_commit_ids == (base_commit,)
            and origin_tree.digest == attempt["base"]["scope_origin"]["tree_digest"]
            and (
                base["kind"] == "composition-inputs"
                or base_tree.digest == base["tree_digest"]
            )
            and reconciliation.get("blocker") is None
            and any(
                item.get("source") == "jj" and item.get("outcome") == "match"
                for item in process_facts["evidence"]
            )
        )
        claim = None if result is None else result["claim_status"]
        claimed_but_unsealed = (
            sorted(set(result["files_changed"]) - set(changed_paths))
            if result is not None else []
        )
        hard_scope_valid = not scope_violations
        if (
            claim == "completed" and process_kind == "normal"
            and clean_identity and hard_scope_valid and not claimed_but_unsealed
        ):
            outcome = "success"
            retriable = False
        elif (
            claim in {"blocked", "needs-decision"} and process_kind == "normal"
            and clean_identity and hard_scope_valid
        ):
            outcome = "paused"
            retriable = False
        else:
            outcome = "failure"
            retriable = hard_scope_valid and (
                claim == "failed" or process_kind == "abnormal" or result is None
                or bool(claimed_but_unsealed)
            )
        evidence_payload = _bounded_path_evidence({
            "process": process_facts,
            "claim_status": claim,
            "normal_zero_exit": process_kind == "normal",
            "accepted_completed_claim": claim == "completed",
            "clean_identity": clean_identity,
            "hard_scope_valid": hard_scope_valid,
            "jj_commit_id": identity.commit_id,
            "tree_digest": final_tree.digest,
        }, {
            "scope_violations": scope_violations,
            "advisory_divergence": advisory_divergence,
            "claimed-but-unsealed": claimed_but_unsealed,
            "changed_paths": changed_paths,
            "cumulative_changed_paths": cumulative_changed,
        })
        process_evidence = _save_evidence(
            store, initiative_id, attempt_id, evidence_payload,
        )
        seal = validate_seal({
            "contract": SEAL_CONTRACT,
            "seal_id": seal_id,
            "initiative_id": initiative_id,
            "node_id": attempt["node_id"],
            "attempt_id": attempt_id,
            "task_id": task["task_id"],
            "run_id": task["runs"][0]["run_id"],
            "outcome": outcome,
            "repository_id": node["repository_id"],
            "scope_origin": copy.deepcopy(attempt["base"]["scope_origin"]),
            "base": base,
            "read_only_failure_seal_ids": failure_inputs,
            "jj_commit_id": identity.commit_id,
            "tree_digest": final_tree.digest,
            "diff_digest": diff_digest,
            "cumulative_diff_digest": cumulative_digest,
            "changed_paths": changed_paths[:MAX_PATH_ITEMS],
            "changed_paths_truncated": max(0, len(changed_paths) - MAX_PATH_ITEMS),
            "changed_paths_digest": hashlib.sha256(
                _canonical(changed_paths)
            ).hexdigest(),
            "cumulative_changed_paths": cumulative_changed[:MAX_PATH_ITEMS],
            "cumulative_changed_paths_truncated": max(
                0, len(cumulative_changed) - MAX_PATH_ITEMS,
            ),
            "cumulative_changed_paths_digest": hashlib.sha256(
                _canonical(cumulative_changed)
            ).hexdigest(),
            "result_id": None if result is None else result["result_id"],
            "process_evidence_id": process_evidence["evidence_id"],
            "sealed_at": _now(),
        })
        if seal["outcome"] == "success" and node["terminal_candidate"]:
            from .composition import CompositionError, enforce_terminal_candidate

            try:
                enforce_terminal_candidate(store, initiative_id, node)
            except CompositionError as exc:
                raise SealError(str(exc)) from exc
        published_payload = _seal_published_payload(
            seal, hard_scope_valid=hard_scope_valid,
            scope_violations=scope_violations,
            advisory_divergence=advisory_divergence,
        )
        _prevalidate_seal_published_event(
            store, initiative_id, seal, published_payload,
        )
        ready_state = {
            "success": "success-seal-ready",
            "failure": "failure-seal-ready",
            "paused": "paused-seal-ready",
        }[outcome]
        attempt = store.read_attempt(initiative_id, attempt_id)
        if attempt["state"] == "reported":
            attempt = _transition_attempt(store, initiative_id, attempt, "awaiting-exit")
        if attempt["state"] in {"abnormal-exit", "result-missing", "awaiting-exit"}:
            attempt = _transition_attempt(
                store, initiative_id, attempt, ready_state, seal_id=seal_id,
            )
        if attempt["state"] == ready_state:
            attempt = _transition_attempt(
                store, initiative_id, attempt, "sealing", seal_id=seal_id,
            )
        store.save_seal(initiative_id, seal)
        hook("published", seal)
        return _finish_published_seal(
            store, initiative, node, attempt, preparation, seal,
            retriable=retriable, hard_scope_valid=hard_scope_valid,
            scope_violations=scope_violations,
            advisory_divergence=advisory_divergence,
        )


def _finish_published_seal(
    store: InitiativeStore,
    initiative: dict[str, Any],
    node: dict[str, Any],
    attempt: dict[str, Any],
    preparation: dict[str, Any],
    seal: dict[str, Any],
    *,
    retriable: bool,
    hard_scope_valid: bool,
    scope_violations: list[str] | None = None,
    advisory_divergence: list[str] | None = None,
) -> dict[str, Any]:
    initiative_id = initiative["initiative_id"]
    terminal = {
        "success": "sealed-success",
        "failure": "sealed-failure",
        "paused": "sealed-paused",
    }[seal["outcome"]]
    attempt = store.read_attempt(initiative_id, attempt["attempt_id"])
    if attempt["state"] != terminal:
        if attempt["state"] != "sealing":
            ready = {
                "success": "success-seal-ready",
                "failure": "failure-seal-ready",
                "paused": "paused-seal-ready",
            }[seal["outcome"]]
            if attempt["state"] == "reported":
                attempt = _transition_attempt(store, initiative_id, attempt, "awaiting-exit")
            if attempt["state"] in {"awaiting-exit", "abnormal-exit", "result-missing"}:
                attempt = _transition_attempt(
                    store, initiative_id, attempt, ready, seal_id=seal["seal_id"],
                )
            if attempt["state"] == ready:
                attempt = _transition_attempt(
                    store, initiative_id, attempt, "sealing", seal_id=seal["seal_id"],
                )
        attempt = _transition_attempt(
            store, initiative_id, attempt, terminal, seal_id=seal["seal_id"],
        )
    preparation = store.read_seal_preparation(initiative_id, seal["seal_id"])
    if preparation["state"] != "completed":
        completed = copy.deepcopy(preparation)
        completed.update({"state": "completed", "refusal": None, "updated_at": _now()})
        validate_seal_preparation(completed)
        store.save_seal_preparation(
            initiative_id, completed, expected_digest=record_digest(preparation),
        )
    published = any(
        event["type"] == "seal-published" and seal["seal_id"] in event["subject_ids"]
        for event in store.list_events_snapshot(initiative_id)
    )
    if not published:
        payload = _seal_published_payload(
            seal, hard_scope_valid=hard_scope_valid,
            scope_violations=scope_violations or [],
            advisory_divergence=advisory_divergence or [],
        )
        append_event(
            store, initiative_id, "seal-published",
            [seal["node_id"], seal["attempt_id"], seal["seal_id"]],
            payload,
            actor_kind="controller", actor_id="seal-controller",
        )
    node = store.read_node(initiative_id, node["node_id"])
    if node["state"] in {"dispatching", "running", "needs-input"}:
        node = _transition_node(store, initiative_id, node, "evaluating")
    if seal["outcome"] == "success":
        repair_root = _repair_lineage_roots(store, initiative_id).get(
            seal["attempt_id"]
        )
        repair_actions = [
            action for action in store.list_actions_snapshot(initiative_id)
            if action["action_class"] == "repair-node"
            and action["state"] == "completed"
            and action_outcome(action).get("attempt_id") == repair_root
        ]
        if len(repair_actions) > 1:
            raise SealError("repair attempt is bound to multiple repair actions")
        if repair_actions:
            repair = action_outcome(repair_actions[0])
            candidate_seal_id = repair.get("candidate_seal_id")
            if not isinstance(candidate_seal_id, str):
                raise SealError("repair action is missing its candidate seal binding")
            candidate = store.read_seal(initiative_id, candidate_seal_id)
            source = store.read_node(initiative_id, candidate["node_id"])
            if source["node_id"] != node["node_id"] and source["state"] == "succeeded":
                _transition_node(store, initiative_id, source, "superseded")
            _invalidate_candidate_records(store, initiative_id, candidate_seal_id)
        if node["state"] == "evaluating":
            _transition_node(store, initiative_id, node, "succeeded")
    elif seal["outcome"] == "paused":
        if node["state"] == "evaluating":
            _transition_node(store, initiative_id, node, "needs-input")
        current = store.peek(initiative_id)
        if current["state"] == "running":
            changed = copy.deepcopy(current)
            changed.update({
                "state": "needs-input",
                "state_revision": current["state_revision"] + 1,
                "updated_at": _now(),
            })
            validate_initiative(changed)
            store.save_initiative(changed, expected_digest=record_digest(current))
    elif not hard_scope_valid:
        if node["state"] == "evaluating":
            _transition_node(store, initiative_id, node, "failed")
    elif retriable and node["state"] == "evaluating":
        from .reconcile import _failure_target

        active_plan = store.read_plan(
            initiative_id, initiative["active_plan"]["revision"],
        )
        _failure_target(
            store, initiative_id, store.peek(initiative_id), active_plan, node,
            store.read_attempt(initiative_id, seal["attempt_id"]),
            store.list_attempts_snapshot(initiative_id),
            datetime.now(timezone.utc),
        )
    elif node["state"] == "evaluating":
        _transition_node(store, initiative_id, node, "failed")
    return seal


def reconcile_seal_drift(
    store: InitiativeStore,
    initiative_id: str,
    *,
    control_store: TaskStore | None = None,
    jj: JjAdapter | None = None,
) -> list[dict[str, Any]]:
    """Record later sealed-workspace drift without rewriting a seal."""
    adapter = jj or JjAdapter()
    control = control_store or TaskStore(store.config.control)
    findings: list[dict[str, Any]] = []
    with store.transaction_lock(initiative_id):
        events = store.list_events_snapshot(initiative_id)
        for seal in store.list_seals_snapshot(initiative_id):
            if any(
                event["type"] == "seal-drift-detected"
                and seal["seal_id"] in event["subject_ids"]
                for event in events
            ):
                continue
            reason = None
            current_commit = None
            try:
                task = control.peek(seal["task_id"])
                identity = adapter.inspect_workspace(
                    Path(task["jj"]["workspace_path"]),
                    task["jj"]["workspace_name"], require_empty=False,
                )
                current_commit = identity.commit_id
                if current_commit != seal["jj_commit_id"]:
                    reason = "sealed workspace commit identity changed"
            except (StoreError, JjError, OSError, ValueError) as exc:
                reason = f"sealed workspace identity is unavailable: {exc}"
            if reason is None:
                continue
            finding = {
                "seal_id": seal["seal_id"],
                "expected_commit_id": seal["jj_commit_id"],
                "current_commit_id": current_commit,
                "reason": reason,
            }
            append_event(
                store, initiative_id, "seal-drift-detected",
                [seal["node_id"], seal["attempt_id"], seal["seal_id"]], finding,
                actor_kind="controller", actor_id="seal-reconciler",
            )
            findings.append(finding)
            current = store.peek(initiative_id)
            if current["state"] == "running":
                changed = copy.deepcopy(current)
                changed.update({
                    "state": "paused",
                    "state_revision": current["state_revision"] + 1,
                    "updated_at": _now(),
                })
                validate_initiative(changed)
                store.save_initiative(changed, expected_digest=record_digest(current))
    return findings


def seal_for_task_or_attempt(
    config,
    identity: str,
) -> dict[str, Any]:
    try:
        from .model import canonical_uuid

        identity = canonical_uuid(identity, "task or attempt ID")
    except ModelError as exc:
        raise SealError(str(exc)) from exc
    store = InitiativeStore(config)
    matches: list[dict[str, Any]] = []
    for initiative in store.list_initiatives():
        for seal in store.list_seals_snapshot(initiative["initiative_id"]):
            if identity in {seal["task_id"], seal["attempt_id"]}:
                matches.append(seal)
    if not matches:
        raise SealError("no published seal matches that task or attempt")
    if len(matches) != 1:
        raise SealError("task or attempt matches more than one published seal")
    return {"contract": TASK_SEAL_CONTRACT, "seal": matches[0]}


__all__ = [
    "TASK_SEAL_CONTRACT", "NoSealableArtifact", "SealError", "prepare_and_publish_seal",
    "reconcile_seal_drift", "seal_for_task_or_attempt",
]
