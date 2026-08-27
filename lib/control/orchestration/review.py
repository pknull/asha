"""Independent exact-seal review task binding and acceptance."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..jj import JjAdapter, JjError
from ..prepare import (
    PreparationError,
    _JJ_PATHS,
    _verify_plan_entry,
)
from .actions import append_event
from .links import control_task_identity_digest
from .model import (
    EVIDENCE_CONTRACT,
    REVIEW_CONTRACT,
    new_uuid,
    record_digest,
    validate_attempt,
    validate_evidence,
    validate_node,
    validate_review,
)
from .store import InitiativeStore, StoreError


class ReviewError(ValueError):
    """A review task or verdict is not bound to the exact target seal."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def specification_digest(
    initiative: Mapping[str, Any], plan: Mapping[str, Any]
) -> str:
    """Bind the approved objective and acceptance specification."""
    return hashlib.sha256(_canonical({
        "objective": initiative["objective"],
        "acceptance_criteria": initiative["acceptance_criteria"],
        "plan_acceptance_conditions": plan["acceptance_conditions"],
    })).hexdigest()


def review_target(
    store: InitiativeStore,
    initiative: Mapping[str, Any],
    plan: Mapping[str, Any],
    node: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the one current success seal and immutable review target."""
    if node.get("type") != "review":
        raise ReviewError("review target resolution requires a review node")
    by_id = {item["node_id"]: item for item in plan["nodes"]}
    candidate_nodes = [
        by_id[item] for item in node["dependencies"]
        if item in by_id and by_id[item].get("terminal_candidate") is True
    ]
    if len(candidate_nodes) != 1:
        raise ReviewError("review node must depend on exactly one terminal candidate")
    candidate = candidate_nodes[0]
    seals = sorted(
        (
            seal for seal in store.list_seals_snapshot(initiative["initiative_id"])
            if seal["node_id"] == candidate["node_id"]
            and seal["repository_id"] == candidate["repository_id"]
            and seal["outcome"] == "success"
        ),
        key=lambda item: (item["sealed_at"], item["seal_id"]),
    )
    if not seals:
        raise ReviewError("terminal candidate has no exact success seal")
    seal = seals[-1]
    target = {
        "seal_id": seal["seal_id"],
        "active_plan_digest": plan["digest"],
        "specification_digest": specification_digest(initiative, plan),
        "repository_id": seal["repository_id"],
        "jj_commit_id": seal["jj_commit_id"],
        "base_seal_ids": list(seal["base"]["seal_ids"]),
        "diff_digest": seal["diff_digest"],
    }
    return seal, target


def register_review_attempt(
    store: InitiativeStore,
    initiative: Mapping[str, Any],
    plan: Mapping[str, Any],
    node: Mapping[str, Any],
    attempt: Mapping[str, Any],
    task: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the ordinary Control review attempt to its immutable target."""
    existing = [
        item for item in store.list_reviews_snapshot(initiative["initiative_id"])
        if item["attempt_id"] == attempt["attempt_id"]
    ]
    if len(existing) > 1:
        raise ReviewError("review attempt has multiple review records")
    _seal, target = review_target(store, initiative, plan, node)
    if existing:
        if existing[0]["target"] != target:
            raise ReviewError("retained review target binding changed")
        return existing[0]
    at = _now()
    record = validate_review({
        "contract": REVIEW_CONTRACT,
        "review_id": new_uuid(),
        "initiative_id": initiative["initiative_id"],
        "node_id": node["node_id"],
        "attempt_id": attempt["attempt_id"],
        "task_id": task["task_id"],
        "run_id": task["runs"][0]["run_id"],
        "state": "running",
        "target": target,
        "verdict": None,
        "findings": [],
        "created_at": at,
        "updated_at": at,
    })
    store.save_review(initiative["initiative_id"], record)
    return record


# Review states that have not settled a verdict.  Every one of them has a legal
# `-> stale` edge in REVIEW_TRANSITIONS.
UNSETTLED_REVIEW_STATES = frozenset({
    "pending", "running", "submitted", "indeterminate",
})


def retire_unsettled_reviews(
    store: InitiativeStore,
    initiative_id: str,
    attempt_ids: frozenset[str],
    *,
    at: str | None = None,
) -> list[str]:
    """Stale every unsettled review bound to an attempt that can no longer settle it.

    A review record is written at dispatch and settled only when its review
    attempt completes, so a stopped or stranded review attempt leaves the
    record `running` forever.  Left alone it either blocks the node's gate or,
    once the released node is re-dispatched, sits alongside a second `running`
    review for the same target.  `running -> stale` is a legal
    REVIEW_TRANSITIONS edge and `_invalidate_candidate_records` is the
    precedent for the field reset that comes with it.

    Both node-release paths retire before they release: `_release_stopped_node`
    in `actions.py` at the two stop sites, and `_recover_stranded_nodes` in
    `reconcile.py` for a node stranded before either of them ran.  Retiring
    first is what makes the order safe -- once the node reaches `ready` it is
    re-dispatchable, and a second review record would be registered against a
    target that already owns an unsettled one.
    """
    retired: list[str] = []
    moment = at or _now()
    for review in sorted(
        store.list_reviews_snapshot(initiative_id),
        key=lambda item: item["review_id"],
    ):
        if review["attempt_id"] not in attempt_ids:
            continue
        if review["state"] not in UNSETTLED_REVIEW_STATES:
            continue
        changed = copy.deepcopy(review)
        changed.update({
            "state": "stale", "verdict": None, "findings": [],
            "updated_at": moment,
        })
        validate_review(changed)
        store.save_review(
            initiative_id, changed, expected_digest=record_digest(review),
        )
        retired.append(review["review_id"])
    return retired


def _transition_attempt(
    store: InitiativeStore, attempt: dict[str, Any], state: str,
) -> dict[str, Any]:
    if attempt["state"] == state:
        return attempt
    changed = copy.deepcopy(attempt)
    changed.update({"state": state, "updated_at": _now()})
    validate_attempt(changed)
    store.save_attempt(
        attempt["initiative_id"], changed, expected_digest=record_digest(attempt),
    )
    return changed


def _save_failure_evidence(
    store: InitiativeStore,
    initiative_id: str,
    review: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    retained = [
        item for item in store.list_evidence_snapshot(initiative_id)
        if item["kind"] == "review-failure"
        and item["subject_id"] == review["review_id"]
    ]
    if len(retained) > 1:
        raise ReviewError("review has multiple retained failure evidence records")
    if retained:
        return retained[0]
    summary = json.dumps(
        {"review_id": review["review_id"], "reason": reason[:4096]},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    evidence = validate_evidence({
        "contract": EVIDENCE_CONTRACT,
        "evidence_id": new_uuid(),
        "initiative_id": initiative_id,
        "kind": "review-failure",
        "subject_id": review["review_id"],
        "digest": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        "summary": summary,
        "recorded_at": _now(),
    })
    store.save_evidence(initiative_id, evidence)
    return evidence


def _tracked_workspace_unchanged(
    adapter: JjAdapter, task: Mapping[str, Any], target_commit_id: str,
) -> bool:
    """Compare tracked bytes without snapshotting or rejecting extra paths."""
    unchanged, _non_tracked, _truncated = tracked_workspace_status(
        adapter,
        Path(task["jj"]["workspace_path"]),
        Path(task["repository"]["root"]),
        target_commit_id,
    )
    return unchanged


def tracked_workspace_status(
    adapter: JjAdapter,
    workspace: Path,
    source: Path,
    target_commit_id: str,
) -> tuple[bool, list[str], bool]:
    """Return exact tracked-tree status and bounded non-tracked path evidence.

    This is a filesystem capture against the immutable target tree. It never
    snapshots the workspace, and extra untracked or ignored paths do not alter
    the tracked-tree verdict.
    """
    facts = adapter.preflight(source)
    plan = adapter.materialization_plan(
        facts.git_root, target_commit_id, exact_root=facts.root,
    )
    root_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    root_fd = os.open(workspace, root_flags)
    try:
        root_metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_uid != os.geteuid():
            raise PreparationError("review workspace root identity is invalid")
        try:
            for entry in plan.entries:
                _verify_plan_entry(root_fd, entry, require_ownership=False)
        except PreparationError:
            return False, [], False
        actual_jj = {
            path: _tracked_path_fact(root_fd, path, None, kind_only=True)
            for path in sorted(_JJ_PATHS)
        }
    finally:
        os.close(root_fd)
    for path, kind in _JJ_PATHS.items():
        fact = actual_jj[path]
        if fact is None or fact["type"] != kind:
            return False, [], False
    non_tracked, truncated = _bounded_non_tracked_paths(
        workspace, {entry.path for entry in plan.entries}, set(_JJ_PATHS),
    )
    return True, non_tracked, truncated


def _tracked_path_fact(
    root_fd: int,
    relative: str,
    expected: Mapping[str, Any] | None,
    *,
    kind_only: bool = False,
) -> dict[str, Any] | None:
    """Read one declared target path without traversing unrelated entries.

    A mismatched regular-file size or mode is already a conclusive tracked
    mutation.  Refuse it before opening or hashing candidate-controlled bytes;
    the immutable expected projection supplies the read bound for matching
    files.  Controller metadata paths need only a kind check.
    """
    parts = relative.split("/")
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                child_fd = os.open(
                    part,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
            except (FileNotFoundError, NotADirectoryError):
                return None
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    return None
                raise
            os.close(parent_fd)
            parent_fd = child_fd
        name = parts[-1]
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISDIR(metadata.st_mode):
            return {"type": "directory"}
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(name, dir_fd=parent_fd)
            after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (metadata.st_dev, metadata.st_ino) != (after.st_dev, after.st_ino):
                raise PreparationError("tracked symlink changed during inspection")
            return {"type": "symlink", "target": target}
        if not stat.S_ISREG(metadata.st_mode):
            return {"type": "special"}
        if kind_only:
            return {"type": "file"}
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            expected is None
            or expected.get("type") != "file"
            or metadata.st_size != expected.get("size")
            or mode != expected.get("mode")
        ):
            return {
                "type": "file", "mode": mode,
                "sha256": None, "size": metadata.st_size,
            }
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (metadata.st_dev, metadata.st_ino)
                != (opened.st_dev, opened.st_ino)
                or metadata.st_size != opened.st_size
            ):
                raise PreparationError("tracked file changed during inspection")
            digest = hashlib.sha256()
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise PreparationError("tracked file shortened during inspection")
                digest.update(chunk)
                remaining -= len(chunk)
            if os.read(descriptor, 1):
                raise PreparationError("tracked file grew during inspection")
        finally:
            os.close(descriptor)
        return {
            "type": "file", "mode": mode,
            "sha256": digest.hexdigest(), "size": metadata.st_size,
        }
    finally:
        os.close(parent_fd)


def _bounded_non_tracked_paths(
    workspace: Path, expected: set[str], jj_paths: set[str],
) -> tuple[list[str], bool]:
    """Enumerate names only; never hash, open, or reject extra objects."""
    retained: list[str] = []
    retained_bytes = 0
    pending = [(workspace, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            if prefix in expected:
                raise
            continue
        with entries:
            for entry in entries:
                relative = f"{prefix}/{entry.name}" if prefix else entry.name
                if relative == ".jj" or relative.startswith(".jj/"):
                    continue
                is_expected = relative in expected or relative in jj_paths
                if not is_expected:
                    size = len(relative.encode("utf-8"))
                    if len(retained) >= 128 or retained_bytes + size > 24 * 1024:
                        return retained, True
                    retained.append(relative)
                    retained_bytes += size
                try:
                    is_directory = entry.is_dir(follow_symlinks=False)
                except OSError:
                    is_directory = False
                if is_directory:
                    pending.append((Path(entry.path), relative))
    retained.sort()
    return retained, False


def _save_workspace_evidence(
    store: InitiativeStore,
    initiative_id: str,
    review: Mapping[str, Any],
    non_tracked_paths: list[str],
    non_tracked_paths_truncated: bool,
) -> dict[str, Any]:
    retained = [
        item for item in store.list_evidence_snapshot(initiative_id)
        if item["kind"] == "review-workspace"
        and item["subject_id"] == review["review_id"]
    ]
    if len(retained) > 1:
        raise ReviewError("review has multiple retained workspace evidence records")
    if retained:
        return retained[0]
    summary = json.dumps(
        {
            "review_id": review["review_id"],
            "tracked_workspace_unchanged": True,
            "non_tracked_paths": non_tracked_paths,
            "non_tracked_paths_truncated": non_tracked_paths_truncated,
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    evidence = validate_evidence({
        "contract": EVIDENCE_CONTRACT,
        "evidence_id": new_uuid(),
        "initiative_id": initiative_id,
        "kind": "review-workspace",
        "subject_id": review["review_id"],
        "digest": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        "summary": summary,
        "recorded_at": _now(),
    })
    store.save_evidence(initiative_id, evidence)
    return evidence


def _finish_failed_review(
    store: InitiativeStore,
    initiative_id: str,
    review: Mapping[str, Any],
    attempt_id: str,
    node_id: str,
    reason: str,
) -> dict[str, Any]:
    """Converge a failed review to a bounded fresh attempt or terminal failure."""
    _save_failure_evidence(store, initiative_id, review, reason)
    current_attempt = store.read_attempt(initiative_id, attempt_id)
    if current_attempt["state"] == "reported":
        current_attempt = _transition_attempt(store, current_attempt, "awaiting-exit")
    if current_attempt["state"] in {"running", "awaiting-exit"}:
        current_attempt = _transition_attempt(store, current_attempt, "abnormal-exit")
    if current_attempt["state"] in {"abnormal-exit", "result-missing"}:
        _transition_attempt(store, current_attempt, "failed-no-artifact")
    current_node = store.read_node(initiative_id, node_id)
    if current_node["state"] in {"running", "dispatching"}:
        changed = copy.deepcopy(current_node)
        changed["state"] = "evaluating"
        store.save_node(
            initiative_id, changed, expected_digest=record_digest(current_node),
        )
        current_node = changed
    initiative = store.peek(initiative_id)
    plan = store.read_plan(
        initiative_id, initiative["active_plan"]["revision"],
    )
    attempts = [
        item for item in store.list_attempts_snapshot(initiative_id)
        if item["node_id"] == node_id
    ]
    from .scheduler import _gate_rerun_attempt_ids

    gate_rerun_ids, _pending_gate_reruns = _gate_rerun_attempt_ids(
        store, initiative_id, node_id, attempts,
    )
    ordinary_attempts = [
        item for item in attempts if item["attempt_id"] not in gate_rerun_ids
    ]
    attempt_cap = min(
        initiative["limits"]["max_attempts_per_node"],
        plan["limits"]["max_attempts_per_node"],
    )
    retry = len(ordinary_attempts) < attempt_cap
    if current_node["state"] == "evaluating":
        changed = copy.deepcopy(current_node)
        changed["state"] = "ready" if retry else "failed"
        validate_node(changed)
        store.save_node(
            initiative_id, changed, expected_digest=record_digest(current_node),
        )
        current_node = changed
    if retry and current_node["state"] == "ready" and not any(
        item["type"] == "node-ready"
        and review["review_id"] in item["subject_ids"]
        and item["payload"].get("reason") == "review-retry"
        for item in store.list_events_snapshot(initiative_id)
    ):
        append_event(
            store, initiative_id, "node-ready",
            [node_id, review["review_id"], attempt_id, review["target"]["seal_id"]],
            {
                "from": "evaluating", "to": "ready",
                "reason": "review-retry",
                "target_seal_id": review["target"]["seal_id"],
            },
            actor_kind="controller", actor_id="review-controller",
        )
    return store.read_review(initiative_id, review["review_id"])


def _finish_accepted_review(
    store: InitiativeStore,
    initiative_id: str,
    review: Mapping[str, Any],
    attempt_id: str,
    node_id: str,
) -> dict[str, Any]:
    """Converge attempts, nodes, and events after an accepted review fact."""
    verdict = review["verdict"]
    if verdict not in {"pass", "findings"}:
        raise ReviewError("accepted review lacks its terminal verdict")
    current_attempt = store.read_attempt(initiative_id, attempt_id)
    if current_attempt["state"] == "reported":
        current_attempt = _transition_attempt(store, current_attempt, "awaiting-exit")
    if current_attempt["state"] == "awaiting-exit":
        current_attempt = _transition_attempt(store, current_attempt, "readonly-ready")
    if current_attempt["state"] == "readonly-ready":
        _transition_attempt(store, current_attempt, "completed-readonly")

    current_node = store.read_node(initiative_id, node_id)
    if current_node["state"] in {"running", "dispatching"}:
        changed = copy.deepcopy(current_node)
        changed["state"] = "evaluating"
        store.save_node(
            initiative_id, changed, expected_digest=record_digest(current_node),
        )
        current_node = changed
    target_state = "succeeded" if verdict == "pass" else "needs-input"
    if current_node["state"] == "evaluating":
        changed = copy.deepcopy(current_node)
        changed["state"] = target_state
        store.save_node(
            initiative_id, changed, expected_digest=record_digest(current_node),
        )
        current_node = changed
    if current_node["state"] != target_state:
        raise ReviewError("review node does not match its accepted verdict")

    seal = store.read_seal(initiative_id, review["target"]["seal_id"])
    target_node_id = seal["node_id"]
    if verdict == "findings":
        target_node = store.read_node(initiative_id, target_node_id)
        if target_node["state"] == "succeeded":
            repair_ready = copy.deepcopy(target_node)
            repair_ready["state"] = "ready"
            validate_node(repair_ready)
            store.save_node(
                initiative_id, repair_ready,
                expected_digest=record_digest(target_node),
            )
            target_node = repair_ready
        elif target_node["state"] != "ready":
            raise ReviewError("accepted findings target is not repairable")
        if not any(
            item["type"] == "node-ready"
            and review["review_id"] in item["subject_ids"]
            and item["payload"].get("reason") == "accepted-review-findings"
            for item in store.list_events_snapshot(initiative_id)
        ):
            append_event(
                store, initiative_id, "node-ready",
                [target_node_id, review["review_id"], review["target"]["seal_id"]],
                {
                    "from": "succeeded", "to": "ready",
                    "reason": "accepted-review-findings",
                },
                actor_kind="controller", actor_id="review-controller",
            )

    if not any(
        item["type"] == "review-accepted"
        and review["review_id"] in item["subject_ids"]
        for item in store.list_events_snapshot(initiative_id)
    ):
        append_event(
            store, initiative_id, "review-accepted",
            [node_id, review["review_id"], review["target"]["seal_id"], target_node_id],
            {
                "verdict": verdict,
                "target_seal_id": review["target"]["seal_id"],
                "target_node_id": target_node_id,
                "repair_required": verdict == "findings",
                "finding_count": len(review["findings"]),
            },
            actor_kind="controller", actor_id="review-controller",
        )
    if verdict == "pass":
        from .scheduler import refresh_readiness

        refresh_readiness(store, initiative_id)
    return store.read_review(initiative_id, review["review_id"])


def complete_review_attempt(
    store: InitiativeStore,
    initiative_id: str,
    attempt_id: str,
    task: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
    *,
    jj: JjAdapter | None = None,
) -> dict[str, Any]:
    """Accept one verdict only after normal exit and a mutation-free workspace."""
    adapter = jj or JjAdapter()
    initiative = store.peek(initiative_id)
    plan = store.read_plan(initiative_id, initiative["active_plan"]["revision"])
    attempt = store.read_attempt(initiative_id, attempt_id)
    node = store.read_node(initiative_id, attempt["node_id"])
    reviews = [
        item for item in store.list_reviews_snapshot(initiative_id)
        if item["attempt_id"] == attempt_id
    ]
    if len(reviews) != 1:
        raise ReviewError("review attempt must have exactly one retained review record")
    review = reviews[0]
    if review["state"] in {"accepted-pass", "accepted-findings"}:
        return _finish_accepted_review(
            store, initiative_id, review, attempt_id, node["node_id"],
        )
    if review["state"] == "failed":
        return _finish_failed_review(
            store, initiative_id, review, attempt_id, node["node_id"],
            "review failure recovered after controller interruption",
        )
    if review["state"] not in {"running", "submitted"}:
        raise ReviewError("review attempt is not in its mutable running phase")
    try:
        result = store.read_result(initiative_id, attempt["result_id"])
    except (StoreError, TypeError) as exc:
        result = None
        failure_reason = f"review has no accepted result: {exc}"
    else:
        failure_reason = ""
    run = reconciliation.get("runs", [{}])[0]
    process_normal = (
        reconciliation.get("state") == "exited"
        and run.get("state") == "exited"
        and reconciliation.get("blocker") is None
    )
    mutation_free = False
    non_tracked_paths: list[str] = []
    non_tracked_paths_truncated = False
    try:
        identity = adapter.inspect_workspace(
            Path(task["jj"]["workspace_path"]), task["jj"]["workspace_name"],
            require_empty=False,
        )
        tree = adapter.immutable_tree(
            Path(task["jj"]["workspace_path"]), identity.commit_id,
        )
        (
            tracked_unchanged,
            non_tracked_paths,
            non_tracked_paths_truncated,
        ) = tracked_workspace_status(
            adapter,
            Path(task["jj"]["workspace_path"]),
            Path(task["repository"]["root"]),
            review["target"]["jj_commit_id"],
        )
        mutation_free = (
            task["jj"]["base_commit_id"] == review["target"]["jj_commit_id"]
            and identity.change_id == task["jj"]["change_id"]
            and identity.commit_id == task["jj"]["working_commit_id"]
            and identity.parent_commit_ids == (review["target"]["jj_commit_id"],)
            and tree.digest == store.read_seal(
                initiative_id, review["target"]["seal_id"]
            )["tree_digest"]
            and tracked_unchanged
            and control_task_identity_digest(dict(task))
            == store.read_link(initiative_id, attempt_id)["control_task_identity_digest"]
        )
    except (PreparationError, JjError, OSError, StoreError, ValueError) as exc:
        failure_reason = f"review workspace identity is indeterminate: {exc}"
    payload = None if result is None else result.get("review")
    target_node = store.read_node(
        initiative_id,
        store.read_seal(initiative_id, review["target"]["seal_id"])["node_id"],
    )
    repairable_target = (
        payload is None
        or payload.get("verdict") != "findings"
        or target_node["state"] == "succeeded"
    )
    accepted = (
        process_normal
        and mutation_free
        and result is not None
        and result["claim_status"] == "completed"
        and payload is not None
        and payload["target"] == review["target"]
        and repairable_target
    )
    if not accepted:
        reason = failure_reason or (
            "reviewer mutation detected" if not mutation_free
            else "review result or target binding is invalid"
        )
        failed = copy.deepcopy(review)
        failed.update({"state": "failed", "verdict": None, "findings": [], "updated_at": _now()})
        store.save_review(
            initiative_id, failed, expected_digest=record_digest(review),
        )
        return _finish_failed_review(
            store, initiative_id, failed, attempt_id, node["node_id"], reason,
        )

    _save_workspace_evidence(
        store, initiative_id, review, non_tracked_paths,
        non_tracked_paths_truncated,
    )

    if review["state"] == "running":
        submitted = copy.deepcopy(review)
        submitted.update({"state": "submitted", "updated_at": _now()})
        store.save_review(initiative_id, submitted, expected_digest=record_digest(review))
    else:
        submitted = review
    if not any(
        item["type"] == "review-submitted"
        and review["review_id"] in item["subject_ids"]
        for item in store.list_events_snapshot(initiative_id)
    ):
        append_event(
            store, initiative_id, "review-submitted",
            [node["node_id"], review["review_id"], review["target"]["seal_id"]],
            {"target_seal_id": review["target"]["seal_id"]},
            actor_kind="controller", actor_id="review-controller",
        )
    accepted_review = copy.deepcopy(submitted)
    accepted_review.update({
        "state": "accepted-pass" if payload["verdict"] == "pass" else "accepted-findings",
        "verdict": payload["verdict"],
        "findings": copy.deepcopy(payload["findings"]),
        "updated_at": _now(),
    })
    store.save_review(
        initiative_id, accepted_review, expected_digest=record_digest(submitted),
    )
    return _finish_accepted_review(
        store, initiative_id, accepted_review, attempt_id, node["node_id"],
    )


__all__ = [
    "ReviewError", "UNSETTLED_REVIEW_STATES", "complete_review_attempt",
    "register_review_attempt", "retire_unsettled_reviews", "review_target",
    "specification_digest", "tracked_workspace_status",
]
