"""Shared reconciliation and presentation composition for Control front ends."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .events import expire_terminal_snapshots, publish_server_summary
from .jj import JjAdapter, JjError
from .launch import LaunchError, persist_terminal_reconciliation
from .reconcile import (
    Adapters,
    StateObservation,
    reconcile_task,
    reconcile_task_with_observation,
)
from .store import TaskStore, task_digest
from .tmux import TmuxAdapter, TmuxError
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


def archived_lifecycle_projection(
    task: dict[str, Any],
) -> tuple[dict[str, Any], StateObservation]:
    """Project archived history without consulting mutable live resources."""
    if task.get("lifecycle") != "archived":
        raise ValueError("lifecycle projection requires an archived task")
    run_id = task["runs"][-1]["run_id"] if task["runs"] else None
    reconciliation = {
        "contract": "asha.control-reconciliation.v1",
        "task_id": task["task_id"],
        "state": "archived",
        "blocker": None,
        "evidence": [],
        "runs": [],
    }
    observation = StateObservation(
        "archived", run_id, "task-lifecycle", task.get("updated_at"),
        "durable", "archived task lifecycle",
    )
    return reconciliation, observation


def _reconcile_with_creation(
    task: dict[str, Any], adapters: Adapters,
    journals: CreationJournalStore, jj: JjAdapter,
    *, include_observation: bool,
) -> dict[str, Any] | tuple[dict[str, Any], StateObservation]:
    """Reconcile a task with its durable pre-launch journal when required."""
    reconcile = reconcile_task_with_observation if include_observation else reconcile_task
    if task["runs"] or task["lifecycle"] not in {"creating", "failed"}:
        return reconcile(task, adapters)
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
    return reconcile(task, adapters, creation=creation)


def reconcile_with_creation(
    task: dict[str, Any], adapters: Adapters,
    journals: CreationJournalStore, jj: JjAdapter,
) -> dict[str, Any]:
    """Reconcile one task while preserving the frozen v1 return shape."""
    result = _reconcile_with_creation(
        task, adapters, journals, jj, include_observation=False,
    )
    if not isinstance(result, dict):
        raise ValueError("unexpected reconciliation projection")
    return result


def reconcile_with_creation_observation(
    task: dict[str, Any], adapters: Adapters,
    journals: CreationJournalStore, jj: JjAdapter,
) -> tuple[dict[str, Any], StateObservation]:
    """Reconcile state and TUI provenance from the same adapter reads."""
    result = _reconcile_with_creation(
        task, adapters, journals, jj, include_observation=True,
    )
    if isinstance(result, dict):
        raise ValueError("missing reconciliation observation")
    return result


def _persist_and_expire_terminal(
    store: TaskStore, task: dict[str, Any], reconciliation: dict[str, Any],
    observation: StateObservation, presentation: TmuxAdapter | None,
    *, publish_summary: bool,
    presentation_now: Callable[[], datetime] | None,
) -> dict[str, Any]:
    """Apply the shared terminal-edge maintenance inside the caller's lock."""
    task = persist_terminal_reconciliation(task, reconciliation, store)
    derived = {run["run_id"]: run for run in reconciliation["runs"]}
    durable_runs = [
        {
            "run_id": run["run_id"],
            "state": run["state"],
            "blocker": derived[run["run_id"]]["blocker"],
        }
        for run in task["runs"]
    ]
    expire_terminal_snapshots(store.config, durable_runs)
    if presentation is not None:
        _mirror_primary_run_state(task, observation, presentation)
        if publish_summary:
            if presentation_now is None:
                publish_server_summary(store.config, presentation)
            else:
                publish_server_summary(
                    store.config, presentation, now=presentation_now,
                )
    return task


def _mirror_primary_run_state(
    task: dict[str, Any], observation: StateObservation,
    presentation: TmuxAdapter,
) -> None:
    """Best-effort mirror of the atomic state observation to its owned run."""
    if observation.run_id is None:
        return
    stored = next(
        (run for run in task["runs"] if run["run_id"] == observation.run_id),
        None,
    )
    if stored is None:
        return
    session = task["tmux"]["session"]
    pane = stored["pane_id"]
    try:
        if (presentation.session_option(
                session, "@asha_managed", deadline_seconds=5,
            ) != "1" or presentation.session_option(
                session, "@asha_task_id", deadline_seconds=5,
            ) != task["task_id"] or presentation.pane_option(
                pane, "@asha_run_id", deadline_seconds=5,
            ) != stored["run_id"]):
            return
        facts = presentation.pane_facts(pane, deadline_seconds=5)
        if (facts.session != session or
                facts.window != task["tmux"]["window"]):
            return
        presentation.set_pane_option(
            pane, "@asha_state", observation.state, deadline_seconds=5,
        )
        presentation.set_session_option(
            session, "@asha_state", observation.state, deadline_seconds=5,
        )
    except (TmuxError, OSError, ValueError):
        return


def locked_reconciliation(
    store: TaskStore, journals: CreationJournalStore, task_id: str,
    adapters: Adapters, jj: JjAdapter, *, presentation: TmuxAdapter | None = None,
    publish_summary: bool = True,
    presentation_now: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reconcile under lock, persisting and expiring a terminal edge."""
    task, reconciliation, _observation = locked_reconciliation_observation(
        store, journals, task_id, adapters, jj, presentation=presentation,
        publish_summary=publish_summary, presentation_now=presentation_now,
    )
    return task, reconciliation


def locked_reconciliation_observation(
    store: TaskStore, journals: CreationJournalStore, task_id: str,
    adapters: Adapters, jj: JjAdapter, *, presentation: TmuxAdapter | None = None,
    publish_summary: bool = True,
    presentation_now: Callable[[], datetime] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], StateObservation]:
    """Locked reconciliation with an atomic presentation-only provenance."""
    with store.transaction_lock(task_id):
        task = store.read(task_id)
        # This is the authoritative lifecycle gate. It uses the record read
        # under the same task lock as any following reconciliation, so an
        # unlocked list/model snapshot can never authorize live observation
        # of a task that has since become archived.
        if task["lifecycle"] == "archived":
            reconciliation, observation = archived_lifecycle_projection(task)
            return task, reconciliation, observation
        reconciliation, observation = reconcile_with_creation_observation(
            task, adapters, journals, jj,
        )
        task = _persist_and_expire_terminal(
            store, task, reconciliation, observation, presentation,
            publish_summary=publish_summary, presentation_now=presentation_now,
        )
        return task, reconciliation, observation


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
