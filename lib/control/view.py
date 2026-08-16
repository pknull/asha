"""Shared read-side composition for Control front ends."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .jj import JjAdapter, JjError
from .launch import LaunchError
from .reconcile import Adapters, reconcile_task
from .store import TaskStore, task_digest
from .tmux import TmuxAdapter
from .transaction import CreationJournalStore, JournalError


@dataclass(frozen=True)
class AttachTarget:
    """An ownership-checked tmux target for one task or run."""

    session: str
    window: str
    pane_id: str | None


def task_summary(
    task: dict[str, Any], reconciliation: dict[str, Any],
) -> dict[str, Any]:
    """Shape one reconciled task exactly as the CLI list contract expects."""
    return {
        "task_id": task["task_id"],
        "slug": task["slug"],
        "label": task["label"],
        "lifecycle": task["lifecycle"],
        "status": reconciliation["state"],
        "updated_at": task["updated_at"],
        "repository": {
            "root": task["repository"]["root"],
            "identity": task["repository"]["identity"],
        },
        "run_count": len(task["runs"]),
        "blocker": reconciliation["blocker"],
    }


def reconcile_with_creation(
    task: dict[str, Any], adapters: Adapters,
    journals: CreationJournalStore, jj: JjAdapter,
) -> dict[str, Any]:
    """Reconcile a task with its durable pre-launch journal when required."""
    if task["runs"] or task["lifecycle"] not in {"creating", "failed"}:
        return reconcile_task(task, adapters)
    try:
        creation: Any = journals.read(task["task_id"])
        workspace = Path(creation["workspace"]["path"])
        expected = creation["workspace"]["root_fact"]
        try:
            metadata = workspace.lstat()
        except FileNotFoundError:
            creation["workspace_present"] = False
        except OSError:
            creation["workspace_present"] = True
            creation["workspace_match"] = False
        else:
            creation["workspace_present"] = True
            creation["workspace_match"] = bool(
                expected is not None and stat.S_ISDIR(metadata.st_mode) and
                {
                    "dev": metadata.st_dev, "ino": metadata.st_ino,
                    "mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid,
                } == expected
            )
        if (creation.get("workspace_present") and creation.get("workspace_match") and
                creation["phase"] == "ready-for-launch"):
            source = Path(creation["repository"]["root"])
            name = creation["workspace"]["name"]
            expected_jj = creation["jj"]
            try:
                repository = jj.preflight(source)
                if (str(repository.root) != creation["repository"]["root"] or
                        str(repository.git_root) != creation["repository"]["git_root"]):
                    raise ValueError(
                        "repository root or Git backend no longer matches the journal"
                    )
                if (task["task_id"] != creation["task_id"] or
                        task["slug"] != creation["task"]["slug"] or
                        task["label"] != creation["task"]["label"] or
                        task["repository"] != {
                            "root": creation["repository"]["root"],
                            "identity": creation["repository"]["identity"],
                        } or
                        task_digest(task) != creation["task"]["digest"] or
                        task["jj"]["workspace_path"] != creation["workspace"]["path"] or
                        task["jj"]["workspace_name"] != name or
                        task["jj"]["base_commit_id"] != expected_jj["base_commit_id"] or
                        task["jj"]["change_id"] != expected_jj["change_id"] or
                        task["jj"]["working_commit_id"] != expected_jj["working_commit_id"]):
                    raise ValueError("task and creation journal jj identity differ")
                registered = jj.workspace_identities(source).get(name)
                if registered is None:
                    creation["live_outcome"] = "missing"
                    creation["live_detail"] = "prepared workspace registration is missing"
                elif registered != (
                    expected_jj["change_id"], expected_jj["working_commit_id"],
                ):
                    creation["live_outcome"] = "mismatch"
                    creation["live_detail"] = (
                        "prepared workspace registration identity changed"
                    )
                else:
                    identity = jj.inspect_workspace(workspace, name, require_empty=True)
                    if identity.change_id != expected_jj["change_id"]:
                        raise ValueError("prepared workspace change ID changed")
                    if identity.commit_id != expected_jj["working_commit_id"]:
                        raise ValueError("prepared workspace working commit ID changed")
                    if identity.parent_commit_ids != (expected_jj["base_commit_id"],):
                        raise ValueError("prepared workspace parent/base changed")
                    if identity.description != expected_jj["description"]:
                        raise ValueError("prepared workspace description changed")
                    creation["live_outcome"] = "match"
                    creation["live_detail"] = (
                        "registration, change, commit, base, description, and emptiness match"
                    )
            except JjError as exc:
                detail = str(exc)
                unavailable = any(token in detail for token in (
                    "invocation failed", "timed out", "bounded adapter limit", "not UTF-8",
                ))
                creation["live_outcome"] = "unavailable" if unavailable else "mismatch"
                safe_detail = "".join(
                    character if character.isprintable() else "?" for character in detail
                )[:400]
                creation["live_detail"] = (
                    f"jj live evidence unavailable: {safe_detail}"
                    if unavailable else safe_detail
                )
            except ValueError as exc:
                creation["live_outcome"] = "mismatch"
                creation["live_detail"] = str(exc)
    except JournalError as exc:
        creation = None if "not found" in str(exc) else {"error": "invalid"}
    return reconcile_task(task, adapters, creation=creation)


def locked_reconciliation(
    store: TaskStore, journals: CreationJournalStore, task_id: str,
    adapters: Adapters, jj: JjAdapter,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read and reconcile one task while holding its durable transaction lock."""
    with store.transaction_lock(task_id):
        task = store.read(task_id)
        return task, reconcile_with_creation(task, adapters, journals, jj)


def _adapter_for_task(task: dict[str, Any]) -> TmuxAdapter:
    socket = task["tmux"]["socket"]
    return TmuxAdapter(socket=None if socket == "default" else socket)


def _require_session_ownership(adapter: TmuxAdapter, task: dict[str, Any]) -> None:
    session = task["tmux"]["session"]
    if not adapter.has_session(session):
        raise LaunchError("recorded tmux session is missing")
    if (adapter.session_option(session, "@asha_managed") != "1" or
            adapter.session_option(session, "@asha_task_id") != task["task_id"]):
        raise LaunchError("tmux session ownership does not match the task record")


def attach_target(
    task: dict[str, Any], run_id: str | None, *, adapter: TmuxAdapter | None = None,
) -> AttachTarget:
    """Resolve and ownership-check the tmux target for a task or selected run."""
    selected_adapter = adapter or _adapter_for_task(task)
    _require_session_ownership(selected_adapter, task)
    pane_id = None
    if run_id is not None:
        selected = next(
            (run for run in task["runs"] if run["run_id"] == run_id), None,
        )
        if selected is None:
            raise LaunchError("requested run does not belong to the task")
        if selected_adapter.pane_option(
            selected["pane_id"], "@asha_run_id",
        ) != run_id:
            raise LaunchError("tmux pane ownership does not match the requested run")
        facts = selected_adapter.pane_facts(selected["pane_id"])
        if (facts.session != task["tmux"]["session"] or
                facts.window != task["tmux"]["window"]):
            raise LaunchError("tmux pane target does not match the requested run")
        pane_id = selected["pane_id"]
    return AttachTarget(
        session=task["tmux"]["session"],
        window=task["tmux"]["window"],
        pane_id=pane_id,
    )
