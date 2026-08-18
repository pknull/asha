"""Crash-safe, task/run-bound worker result publication."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..jj import JjAdapter, JjError
from ..reconcile import LiveAdapters
from ..store import StoreError, TaskStore
from .actions import append_event
from .config import OrchestrationConfig
from .links import control_task_identity_digest
from .model import (
    RESULT_CONTRACT,
    RESULT_PUBLICATION_CONTRACT,
    MAX_RESULT_TRANSPORT_BYTES,
    ModelError,
    canonical_uuid,
    new_uuid,
    record_digest,
    validate_attempt,
    validate_result,
    validate_result_publication,
)
from .store import InitiativeStore


MAX_RESULT_BODY_BYTES = MAX_RESULT_TRANSPORT_BYTES
RESULT_RECEIPT_CONTRACT = "asha.orchestration-result-publication-receipt.v1"
TASK_RESULTS_CONTRACT = "asha.orchestration-task-results.v1"

_CLIENT_KEYS = frozenset({
    "contract", "publication_id", "supersedes_result_id", "initiative_id",
    "node_id", "attempt_id", "task_id", "run_id", "claim_status", "summary",
    "files_changed", "verification_attestations", "concerns", "follow_up",
    "published_at",
})


class ResultError(ValueError):
    """A result transport, binding, or publication phase is invalid."""


class ResultRefused(ResultError):
    """A deterministic result publication refusal."""


class _DuplicateKey(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey(key)
        value[key] = item
    return value


def canonical_body_bytes(body: Any) -> bytes:
    try:
        raw = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ResultError(f"result body is not canonical JSON: {exc}") from exc
    if len(raw) > MAX_RESULT_BODY_BYTES:
        raise ResultError(f"result body exceeds {MAX_RESULT_BODY_BYTES} bytes")
    return raw


def parse_client_body(raw: bytes) -> dict[str, Any]:
    """Apply the absolute transport cap and duplicate-key parse before reservation."""
    if not isinstance(raw, bytes) or not raw:
        raise ResultError("result file must contain strict UTF-8 JSON")
    if len(raw) > MAX_RESULT_BODY_BYTES:
        raise ResultError(f"result body exceeds {MAX_RESULT_BODY_BYTES} bytes")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except _DuplicateKey as exc:
        raise ResultError(f"result body contains duplicate key: {exc}") from exc
    except RecursionError as exc:
        raise ResultError("result body nesting exceeds the supported limit") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ResultError(f"result body is not strict UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ResultError("result body must be an object")
    # Only the fields needed to route and reserve are interpreted here.
    if value.get("contract") != RESULT_CONTRACT:
        raise ResultError(f"result contract must be {RESULT_CONTRACT}")
    try:
        canonical_uuid(value.get("publication_id"), "publication_id")
    except ModelError as exc:
        raise ResultError(str(exc)) from exc
    canonical_body_bytes(value)
    return value


def read_client_file(path: Path) -> dict[str, Any]:
    """Read one bounded regular non-symlink result file."""
    candidate = Path(path)
    flags = (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ResultError(f"cannot read result file: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ResultError("result file must be a regular non-symlink file")
        if metadata.st_size > MAX_RESULT_BODY_BYTES:
            raise ResultError(f"result body exceeds {MAX_RESULT_BODY_BYTES} bytes")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_RESULT_BODY_BYTES + 1)
    except OSError as exc:
        raise ResultError(f"cannot read result file: {exc}") from exc
    finally:
        os.close(descriptor)
    if len(raw) > MAX_RESULT_BODY_BYTES:
        raise ResultError(f"result body exceeds {MAX_RESULT_BODY_BYTES} bytes")
    return parse_client_body(raw)


def _managed_identity(env: Mapping[str, str]) -> tuple[str, str]:
    if env.get("ASHA_CONTROL_MANAGED") != "1":
        raise ResultRefused("result publication requires ASHA_CONTROL_MANAGED=1")
    try:
        task_id = canonical_uuid(env.get("ASHA_CONTROL_TASK_ID"), "ASHA_CONTROL_TASK_ID")
        run_id = canonical_uuid(env.get("ASHA_CONTROL_RUN_ID"), "ASHA_CONTROL_RUN_ID")
    except ModelError as exc:
        raise ResultRefused(str(exc)) from exc
    return task_id, run_id


def locate_task_binding(
    config: OrchestrationConfig,
    task_id: str,
    *,
    control_store: TaskStore | None = None,
) -> tuple[InitiativeStore, dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Find the unique initiative/link/attempt/node for one Control task ID."""
    try:
        task_id = canonical_uuid(task_id, "task_id")
    except ModelError as exc:
        raise ResultRefused(str(exc)) from exc
    store = InitiativeStore(config)
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for initiative in store.list_initiatives():
        for link in store.list_links_snapshot(initiative["initiative_id"]):
            if link["control_task_id"] == task_id:
                matches.append((initiative, link))
    if not matches:
        raise ResultRefused("Control task is not linked to an orchestration attempt")
    if len(matches) != 1:
        raise ResultRefused("Control task is linked to more than one initiative")
    initiative, link = matches[0]
    attempt = store.read_attempt(initiative["initiative_id"], link["attempt_id"])
    node = store.read_node(initiative["initiative_id"], link["node_id"])
    if attempt["task_id"] != task_id or attempt["node_id"] != node["node_id"]:
        raise ResultRefused("orchestration link binding disagrees with its attempt")
    try:
        task = (control_store or TaskStore(config.control)).peek(task_id)
    except StoreError as exc:
        raise ResultRefused(f"linked Control task cannot be read: {exc}") from exc
    return store, initiative, link, attempt, node, task


def _receipt(publication: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "contract": RESULT_RECEIPT_CONTRACT,
        "publication_id": publication["publication_id"],
        "result_id": publication["result_id"],
        "phase": publication["state"],
        "refusal": publication["refusal"],
    }


def _transition_publication(
    store: InitiativeStore,
    publication: dict[str, Any],
    state: str,
    *,
    refusal: str | None = None,
) -> dict[str, Any]:
    if publication["state"] == state and publication["refusal"] == refusal:
        return publication
    changed = copy.deepcopy(publication)
    changed.update({"state": state, "refusal": refusal, "updated_at": _now()})
    validate_result_publication(changed)
    store.save_result_publication(
        publication["initiative_id"], changed,
        expected_digest=record_digest(publication),
    )
    return changed


def _path_inside_workspace(workspace: Path, raw: str, label: str) -> None:
    if raw == "." and label == "files_changed":
        raise ResultRefused("files_changed must name files, not directories")
    target = workspace if raw == "." else workspace.joinpath(*raw.split("/"))
    try:
        relative = target.relative_to(workspace)
    except ValueError as exc:
        raise ResultRefused(f"{label} path escapes the task workspace: {raw}") from exc
    current = workspace
    metadata = None
    for part in relative.parts:
        current = current / part
        metadata = None
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            # A worker may truthfully report a deleted path. Existing ancestors
            # were checked and lexical canonicality was validated by the model.
            break
        except OSError as exc:
            raise ResultRefused(f"cannot inspect {label} path {raw}: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ResultRefused(f"{label} path traverses a symlink: {raw}")
    if (
        label == "files_changed"
        and metadata is not None
        and current == target
        and stat.S_ISDIR(metadata.st_mode)
    ):
        raise ResultRefused("files_changed must name files, not directories")


def _validate_workspace_paths(result: Mapping[str, Any], task: Mapping[str, Any]) -> None:
    workspace = Path(task["jj"]["workspace_path"])
    if (
        not workspace.is_absolute()
        or os.path.realpath(workspace) != str(workspace)
        or workspace.is_symlink()
        or not workspace.is_dir()
    ):
        raise ResultRefused("linked task workspace is not its exact canonical directory")
    for path in result["files_changed"]:
        _path_inside_workspace(workspace, path, "files_changed")
    for attestation in result["verification_attestations"]:
        _path_inside_workspace(workspace, attestation["cwd"], "attestation cwd")


def _semantic_result(
    publication: Mapping[str, Any],
    body: Mapping[str, Any],
    attempt: Mapping[str, Any],
    node: Mapping[str, Any],
    task: Mapping[str, Any],
    link: Mapping[str, Any],
    *,
    jj: JjAdapter,
) -> dict[str, Any]:
    if set(body) != _CLIENT_KEYS:
        raise ResultRefused(
            "result body must use the closed worker schema (without result_id/payload_digest)"
        )
    candidate = dict(copy.deepcopy(body))
    candidate.update({
        "result_id": publication["result_id"],
        "payload_digest": publication["payload_digest"],
    })
    try:
        result = validate_result(candidate)
    except ModelError as exc:
        raise ResultRefused(str(exc)) from exc
    expected = {
        "publication_id": publication["publication_id"],
        "initiative_id": publication["initiative_id"],
        "node_id": node["node_id"],
        "attempt_id": attempt["attempt_id"],
        "task_id": task["task_id"],
        "run_id": task["runs"][0]["run_id"],
    }
    for field, value in expected.items():
        if result[field] != value:
            raise ResultRefused(f"result {field} disagrees with the task binding")
    if link["control_task_id"] != task["task_id"]:
        raise ResultRefused("Control task link identity changed")
    if control_task_identity_digest(dict(task)) != link["control_task_identity_digest"]:
        raise ResultRefused("Control task ownership identity no longer matches its link")
    if attempt["state"] not in {"running", "reported", "awaiting-exit", "indeterminate"}:
        raise ResultRefused("result publication requires an active unsealed attempt")
    if attempt["seal_id"] is not None:
        raise ResultRefused("sealed attempts cannot publish or correct results")
    current_result_id = attempt["result_id"]
    if result["supersedes_result_id"] != current_result_id:
        if current_result_id is None:
            raise ResultRefused("the first accepted result must not supersede another result")
        raise ResultRefused(
            "supersedes_result_id must name the current accepted result for this attempt"
        )
    _validate_workspace_paths(result, task)
    try:
        evidence = LiveAdapters(config=None, jj=jj).jj(dict(task))
    except (JjError, OSError, ValueError) as exc:
        raise ResultRefused(f"Control jj ownership reconciliation failed: {exc}") from exc
    if evidence.outcome != "match":
        raise ResultRefused(f"Control jj ownership does not reconcile: {evidence.detail}")
    return result


def _mark_publication_indeterminate(
    store: InitiativeStore,
    publication: dict[str, Any],
    attempt: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    current = publication
    if current["state"] != "indeterminate":
        current = _transition_publication(
            store, current, "indeterminate", refusal=reason[:2048],
        )
    _pause_publication_conflict(store, current, attempt, reason)
    return current


def _pause_publication_conflict(
    store: InitiativeStore,
    publication: Mapping[str, Any],
    attempt: Mapping[str, Any],
    reason: str,
) -> None:
    """Pause affected work without rewriting a terminal publication journal."""
    latest = store.read_attempt(attempt["initiative_id"], attempt["attempt_id"])
    if latest["state"] != "indeterminate" and latest["state"] not in {
        "sealed-success", "sealed-failure", "sealed-paused", "cancelled", "stale",
    }:
        changed = copy.deepcopy(latest)
        changed.update({"state": "indeterminate", "updated_at": _now()})
        validate_attempt(changed)
        store.save_attempt(
            attempt["initiative_id"], changed, expected_digest=record_digest(latest),
        )
    node = store.read_node(attempt["initiative_id"], attempt["node_id"])
    if node["state"] in {"dispatching", "running", "evaluating"}:
        changed_node = copy.deepcopy(node)
        changed_node["state"] = "needs-input"
        store.save_node(
            attempt["initiative_id"], changed_node,
            expected_digest=record_digest(node),
        )
    from .scheduler import pause_for_breaker

    pause_for_breaker(
        store, attempt["initiative_id"], reason[:1000],
        event_type="reconciliation-conflict",
        subject_ids=[attempt["node_id"], attempt["attempt_id"], publication["publication_id"]],
    )


def _finish_acceptance(
    store: InitiativeStore,
    publication: dict[str, Any],
    result: dict[str, Any],
) -> None:
    attempt = store.read_attempt(publication["initiative_id"], publication["attempt_id"])
    if attempt["seal_id"] is not None:
        raise ResultRefused("sealed attempts cannot accept another result")
    if attempt["result_id"] not in {None, result["supersedes_result_id"], result["result_id"]}:
        raise ResultRefused("accepted result lineage changed during publication")
    changed = copy.deepcopy(attempt)
    changed.update({
        "result_publication_id": publication["publication_id"],
        "result_id": result["result_id"],
        "updated_at": _now(),
    })
    if changed["state"] in {"running", "indeterminate"}:
        changed["state"] = "reported"
    if changed != attempt:
        validate_attempt(changed)
        store.save_attempt(
            publication["initiative_id"], changed,
            expected_digest=record_digest(attempt),
        )
    already = any(
        event["type"] == "result-published"
        and result["result_id"] in event["subject_ids"]
        for event in store.list_events_snapshot(publication["initiative_id"])
    )
    if not already:
        append_event(
            store, publication["initiative_id"], "result-published",
            [result["node_id"], result["attempt_id"], result["result_id"]],
            {
                "publication_id": result["publication_id"],
                "claim_status": result["claim_status"],
                "supersedes_result_id": result["supersedes_result_id"],
            },
            actor_kind="worker", actor_id=result["run_id"],
        )


def _completion_is_durable(
    store: InitiativeStore, publication: Mapping[str, Any],
) -> bool:
    try:
        result = store.read_result(
            publication["initiative_id"], publication["result_id"],
        )
    except StoreError:
        return False
    if (
        result["publication_id"] != publication["publication_id"]
        or result["payload_digest"] != publication["payload_digest"]
        or result["attempt_id"] != publication["attempt_id"]
    ):
        return False
    return any(
        event["type"] == "result-published"
        and publication["result_id"] in event["subject_ids"]
        for event in store.list_events_snapshot(publication["initiative_id"])
    )


def _advance_publication(
    store: InitiativeStore,
    publication: dict[str, Any],
    *,
    control_store: TaskStore | None = None,
    jj: JjAdapter | None = None,
    phase_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    initiative_id = publication["initiative_id"]
    hook = phase_hook or (lambda _phase, _record: None)
    attempt = store.read_attempt(initiative_id, publication["attempt_id"])
    node = store.read_node(initiative_id, publication["node_id"])
    link = store.read_link(initiative_id, publication["attempt_id"])
    try:
        task = (control_store or TaskStore(store.config.control)).peek(publication["task_id"])
    except StoreError as exc:
        reason = f"linked Control task cannot be read: {exc}"
        if publication["state"] in {"reserved", "validating"}:
            return _transition_publication(store, publication, "refused", refusal=reason)
        if publication["state"] == "completed":
            _pause_publication_conflict(store, publication, attempt, reason)
            return publication
        return _mark_publication_indeterminate(store, publication, attempt, reason)
    expected_result: dict[str, Any] | None = None
    if publication["state"] in {"reserved", "indeterminate"}:
        if publication["state"] == "indeterminate":
            # An indeterminate journal is recoverable only when the preallocated
            # result path proves which phase completed.
            try:
                retained = store.read_result(initiative_id, publication["result_id"])
            except StoreError as exc:
                if "not found" not in str(exc):
                    return _mark_publication_indeterminate(
                        store, publication, attempt,
                        "preallocated result path contains conflicting bytes",
                    )
                publication = _transition_publication(store, publication, "reserved")
            else:
                try:
                    expected_result = _semantic_result(
                        publication, publication["body"], attempt, node, task, link,
                        jj=jj or JjAdapter(),
                    )
                except ResultRefused as exc:
                    return _mark_publication_indeterminate(
                        store, publication, attempt, str(exc),
                    )
                if retained != expected_result:
                    return _mark_publication_indeterminate(
                        store, publication, attempt,
                        "preallocated result path contains conflicting bytes",
                    )
                publication = _transition_publication(store, publication, "completed")
        if publication["state"] == "reserved":
            publication = _transition_publication(store, publication, "validating")
            hook("validating", publication)
    if publication["state"] == "validating":
        try:
            expected_result = _semantic_result(
                publication, publication["body"], attempt, node, task, link,
                jj=jj or JjAdapter(),
            )
        except ResultRefused as exc:
            publication = _transition_publication(
                store, publication, "refused", refusal=str(exc)[:2048],
            )
            hook("refused", publication)
            return publication
        publication = _transition_publication(store, publication, "persisting")
        hook("persisting", publication)
    if publication["state"] == "persisting":
        if expected_result is None:
            try:
                expected_result = _semantic_result(
                    publication, publication["body"], attempt, node, task, link,
                    jj=jj or JjAdapter(),
                )
            except ResultRefused as exc:
                return _mark_publication_indeterminate(
                    store, publication, attempt,
                    f"persisting publication no longer validates: {exc}",
                )
        try:
            retained = store.read_result(initiative_id, publication["result_id"])
        except StoreError as exc:
            if "not found" not in str(exc):
                return _mark_publication_indeterminate(
                    store, publication, attempt,
                    "preallocated result path contains conflicting bytes",
                )
            try:
                store.save_result(initiative_id, expected_result)
            except StoreError:
                try:
                    retained = store.read_result(initiative_id, publication["result_id"])
                except StoreError:
                    return _mark_publication_indeterminate(
                        store, publication, attempt,
                        "result persistence is indeterminate",
                    )
                if retained != expected_result:
                    return _mark_publication_indeterminate(
                        store, publication, attempt,
                        "preallocated result path contains conflicting bytes",
                    )
        else:
            if retained != expected_result:
                return _mark_publication_indeterminate(
                    store, publication, attempt,
                    "preallocated result path contains conflicting bytes",
                )
        hook("result-persisted", publication)
        publication = _transition_publication(store, publication, "completed")
        hook("completed", publication)
    if publication["state"] == "completed":
        try:
            result = store.read_result(initiative_id, publication["result_id"])
            if expected_result is None:
                if attempt["result_id"] == publication["result_id"]:
                    candidate = copy.deepcopy(publication["body"])
                    candidate.update({
                        "result_id": publication["result_id"],
                        "payload_digest": publication["payload_digest"],
                    })
                    expected_result = validate_result(candidate)
                else:
                    expected_result = _semantic_result(
                        publication, publication["body"], attempt, node, task, link,
                        jj=jj or JjAdapter(),
                    )
            if result != expected_result:
                raise ResultRefused("completed publication result binding changed")
            _finish_acceptance(store, publication, result)
        except (ModelError, ResultRefused, StoreError) as exc:
            # completed is a terminal journal fact and is never rewritten.
            _pause_publication_conflict(
                store, publication, attempt,
                f"completed publication cannot be bound to its attempt: {exc}",
            )
    return publication


def publish_result(
    store: InitiativeStore,
    body: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    control_store: TaskStore | None = None,
    jj: JjAdapter | None = None,
    phase_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Reserve, validate, persist, and acknowledge one exact worker result."""
    task_id, run_id = _managed_identity(env)
    raw = canonical_body_bytes(body)
    parsed = parse_client_body(raw)
    publication_id = parsed["publication_id"]
    located = locate_task_binding(store.config, task_id, control_store=control_store)
    located_store, initiative, link, attempt, node, task = located
    # Keep an explicitly supplied store (and its failure hooks) authoritative.
    if located_store.config != store.config:
        raise ResultRefused("orchestration configuration changed during publication")
    if run_id != task["runs"][0]["run_id"]:
        raise ResultRefused("managed run ID is not the linked task primary run")
    if parsed.get("task_id") != task_id or parsed.get("run_id") != run_id:
        raise ResultRefused("result task/run fields disagree with the managed environment")
    digest = hashlib.sha256(raw).hexdigest()
    with store.transaction_lock(initiative["initiative_id"]):
        attempt = store.read_attempt(initiative["initiative_id"], attempt["attempt_id"])
        if attempt["task_id"] != task_id or attempt["node_id"] != node["node_id"]:
            raise ResultRefused("attempt binding changed before publication reservation")
        try:
            publication = store.read_result_publication(
                initiative["initiative_id"], publication_id,
            )
        except StoreError as exc:
            if "not found" not in str(exc):
                raise
            now = _now()
            sequence = 1 + max(
                (item["receipt_sequence"] for item in store.list_result_publications_snapshot(
                    initiative["initiative_id"]
                )),
                default=0,
            )
            publication = validate_result_publication({
                "contract": RESULT_PUBLICATION_CONTRACT,
                "publication_id": publication_id,
                "result_id": new_uuid(),
                "payload_digest": digest,
                "initiative_id": initiative["initiative_id"],
                "node_id": attempt["node_id"],
                "attempt_id": attempt["attempt_id"],
                "task_id": task_id,
                "run_id": run_id,
                "state": "reserved",
                "body_digest": digest,
                "body": copy.deepcopy(parsed),
                "receipt_sequence": sequence,
                "attempt_revision": record_digest(attempt),
                "refusal": None,
                "created_at": now,
                "updated_at": now,
            })
            store.save_result_publication(initiative["initiative_id"], publication)
            (phase_hook or (lambda _phase, _record: None))("reserved", publication)
        else:
            binding = (
                publication["payload_digest"] == digest
                and publication["body_digest"] == digest
                and publication["body"] == parsed
                and publication["task_id"] == task_id
                and publication["run_id"] == run_id
                and publication["attempt_id"] == attempt["attempt_id"]
            )
            if not binding:
                raise ResultRefused(
                    "publication ID is already bound to different canonical bytes or task identity"
                )
        if publication["state"] in {"completed", "refused"}:
            if publication["state"] == "completed":
                if _completion_is_durable(store, publication):
                    return _receipt(publication)
                _advance_publication(
                    store, publication, control_store=control_store, jj=jj,
                    phase_hook=phase_hook,
                )
                if not _completion_is_durable(store, publication):
                    raise ResultError(
                        "completed publication is not durably accepted; retry the "
                        "same publication after reconciliation"
                    )
            return _receipt(publication)
        publication = _advance_publication(
            store, publication, control_store=control_store, jj=jj,
            phase_hook=phase_hook,
        )
        if publication["state"] == "completed" and not _completion_is_durable(
            store, publication,
        ):
            raise ResultError(
                "completed publication is not durably accepted; retry the same "
                "publication after reconciliation"
            )
        return _receipt(publication)


def reconcile_publications(
    store: InitiativeStore,
    initiative_id: str,
    *,
    control_store: TaskStore | None = None,
    jj: JjAdapter | None = None,
) -> list[dict[str, Any]]:
    """Resume every nonterminal publication before exit/seal reconciliation."""
    results: list[dict[str, Any]] = []
    with store.transaction_lock(initiative_id):
        for publication in store.list_result_publications_snapshot(initiative_id):
            if publication["state"] == "refused":
                continue
            if publication["state"] == "completed" and _completion_is_durable(
                store, publication,
            ):
                continue
            results.append(_advance_publication(
                store, publication, control_store=control_store, jj=jj,
            ))
    return results


def results_for_task(
    config: OrchestrationConfig,
    task_id: str,
) -> dict[str, Any]:
    store, initiative, _link, _attempt, _node, _task = locate_task_binding(
        config, task_id,
    )
    values = sorted(
        (
            result for result in store.list_results_snapshot(initiative["initiative_id"])
            if result["task_id"] == task_id
        ),
        key=lambda item: (item["published_at"], item["result_id"]),
    )
    return {"contract": TASK_RESULTS_CONTRACT, "task_id": task_id, "results": values}


__all__ = [
    "MAX_RESULT_BODY_BYTES", "RESULT_RECEIPT_CONTRACT", "TASK_RESULTS_CONTRACT",
    "ResultError", "ResultRefused", "canonical_body_bytes", "locate_task_binding",
    "parse_client_body", "publish_result", "read_client_file",
    "reconcile_publications", "results_for_task",
]
