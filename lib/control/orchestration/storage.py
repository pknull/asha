"""Read-only retained-storage reporting for one initiative."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from ..jj import JjAdapter, JjError
from ..store import StoreError, TaskStore
from .config import load_config
from .store import InitiativeStore


STORAGE_REPORT_CONTRACT = "asha.orchestration-storage-report.v1"


def _path_usage(path: Path) -> tuple[int, int]:
    try:
        first = path.lstat()
    except FileNotFoundError:
        return 0, 0
    if not stat.S_ISDIR(first.st_mode) or stat.S_ISLNK(first.st_mode):
        return first.st_size, 1
    total_bytes, total_inodes = first.st_size, 1
    for root, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            metadata = (Path(root) / name).lstat()
            total_bytes += metadata.st_size
            total_inodes += 1
    return total_bytes, total_inodes


def _attempt_repositories(store: InitiativeStore, initiative_id: str) -> dict[str, str | None]:
    """attempt_id -> the repository its node targets (None when the node is unknown)."""
    nodes = {item["node_id"]: item for item in store.list_nodes_snapshot(initiative_id)}
    return {
        attempt["attempt_id"]: (nodes.get(attempt["node_id"]) or {}).get("repository_id")
        for attempt in store.list_attempts_snapshot(initiative_id)
    }


def _materialization_paths(
    store: InitiativeStore, initiative_id: str,
) -> tuple[dict[str, list[str]], dict[str, str | None]]:
    """Every retained verification materialization path: the record's primary
    path plus each `verification-member` binding, labelled by repository."""
    by_path: dict[str, list[str]] = {}
    repository: dict[str, str | None] = {}
    for verification in store.list_verifications_snapshot(initiative_id):
        path = verification["materialization_path"]
        by_path.setdefault(path, []).append(verification["verification_id"])
        repository.setdefault(path, verification.get("repository_id"))
    for evidence in store.list_evidence_snapshot(initiative_id):
        if evidence.get("kind") != "verification-member":
            continue
        try:
            member = json.loads(evidence["summary"])
        except (TypeError, ValueError):
            continue
        path = member["materialization_path"]
        ids = by_path.setdefault(path, [])
        if member["verification_id"] not in ids:
            ids.append(member["verification_id"])
        repository[path] = member.get("repository_id")
    return by_path, repository


def storage_report(
    initiative: dict[str, Any],
    *,
    store: InitiativeStore | None = None,
    control_store: TaskStore | None = None,
    jj: JjAdapter | None = None,
) -> dict[str, Any]:
    if store is None:
        store = InitiativeStore(load_config())
    initiative_id = initiative["initiative_id"]
    inventory = store.inventory(initiative_id, locked=False)
    control_store = control_store or TaskStore(store.config.control)
    jj = jj or JjAdapter()
    from .model import scope_repositories

    # One jj workspace registry per member repository; a task workspace is
    # registered when any member registry knows its name.
    registrations: dict[str, tuple[str, str]] | None = {}
    registration_error: str | None = None
    for member in scope_repositories(initiative):
        try:
            registrations.update(jj.workspace_identities(Path(member["root"])))
        except (JjError, OSError, ValueError) as exc:
            registrations = None
            registration_error = str(exc)[:400]
            break

    workspaces: list[dict[str, Any]] = []
    materializations: list[dict[str, Any]] = []
    workspace_bytes = 0
    workspace_inodes = 0
    counted_paths: set[str] = set()
    usage_by_path: dict[str, tuple[int, int]] = {}
    attempt_repository = _attempt_repositories(store, initiative_id)
    for link in store.list_links_snapshot(initiative_id):
        try:
            task = control_store.peek(link["control_task_id"])
            path = Path(task["jj"]["workspace_path"])
            name = task["jj"]["workspace_name"]
            exists = path.exists() and path.is_dir() and not path.is_symlink()
            path_key = str(path)
            if exists and path_key not in usage_by_path:
                usage_by_path[path_key] = _path_usage(path)
            used_bytes, used_inodes = usage_by_path.get(path_key, (0, 0))
            registered = None if registrations is None else name in registrations
            detail = registration_error
        except StoreError as exc:
            path = None
            name = None
            exists = False
            used_bytes = used_inodes = 0
            registered = None
            detail = str(exc)[:400]
        path_key = None if path is None else str(path)
        if path_key is not None and path_key not in counted_paths:
            counted_paths.add(path_key)
            workspace_bytes += used_bytes
            workspace_inodes += used_inodes
        workspaces.append({
            "attempt_id": link["attempt_id"],
            "repository_id": attempt_repository.get(link["attempt_id"]),
            "control_task_id": link["control_task_id"],
            "path": None if path is None else str(path),
            "workspace_name": name,
            "exists": exists,
            "jj_workspace_registered": registered,
            "bytes": used_bytes,
            "inodes": used_inodes,
            "detail": detail,
        })
    verification_paths, path_repository = _materialization_paths(store, initiative_id)
    for path_key in sorted(verification_paths):
        path = Path(path_key)
        exists = path.exists() and path.is_dir() and not path.is_symlink()
        if exists and path_key not in usage_by_path:
            usage_by_path[path_key] = _path_usage(path)
        used_bytes, used_inodes = usage_by_path.get(path_key, (0, 0))
        if path_key not in counted_paths:
            counted_paths.add(path_key)
            workspace_bytes += used_bytes
            workspace_inodes += used_inodes
        materializations.append({
            "path": path_key,
            "repository_id": path_repository.get(path_key),
            "verification_ids": sorted(verification_paths[path_key]),
            "exists": exists,
            "bytes": used_bytes,
            "inodes": used_inodes,
        })
    totals = {
        "bytes": inventory["totals"]["bytes"] + workspace_bytes,
        "inodes": inventory["totals"]["inodes"] + workspace_inodes,
    }
    thresholds = {
        "max_retained_bytes_before_pause": store.config.max_retained_bytes_before_pause,
        "max_retained_inodes_before_pause": store.config.max_retained_inodes_before_pause,
    }
    return {
        "contract": STORAGE_REPORT_CONTRACT,
        "initiative_id": initiative_id,
        "inventory": inventory,
        "workspaces": workspaces,
        "materializations": materializations,
        "totals": totals,
        "thresholds": thresholds,
        "pause_recommended": (
            totals["bytes"] >= thresholds["max_retained_bytes_before_pause"]
            or totals["inodes"] >= thresholds["max_retained_inodes_before_pause"]
        ),
    }


__all__ = ["STORAGE_REPORT_CONTRACT", "storage_report"]
