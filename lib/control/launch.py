"""Durable tmux launch, stop, and archive transactions for Asha Control."""

from __future__ import annotations

import copy
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import harness as harness_api
from .config import ControlConfig
from .jj import JjAdapter
from .model import RUN_CONTRACT, canonical_uuid, new_uuid
from .prepare import PreparationError, rollback_prelaunch
from .reconcile import Adapters, LiveAdapters
from .store import TaskStore, task_digest
from .tmux import TmuxAdapter, TmuxError
from .transaction import CreationJournalStore


class LaunchError(ValueError):
    """A launch, stop, or archive precondition or transaction failed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _adapter_for_task(task: dict[str, Any]) -> TmuxAdapter:
    socket = task["tmux"]["socket"]
    return TmuxAdapter(socket=None if socket == "default" else socket)


def _inject(failure_injector: Callable[[str], None] | None, boundary: str) -> None:
    if failure_injector is not None:
        failure_injector(boundary)


def _save_phase(
    journals: CreationJournalStore,
    journal: dict[str, Any],
    phase: str,
    failure_injector: Callable[[str], None] | None,
    *,
    inject_after: bool = True,
) -> None:
    previous = journal["phase"]
    journal["phase"] = phase
    journals.save(journal, expected_phase=previous)
    if inject_after:
        _inject(failure_injector, f"journal:{phase}")
        _inject(failure_injector, phase)


def _session_options(task: dict[str, Any]) -> dict[str, str]:
    return {
        "@asha_managed": "1",
        "@asha_task_id": task["task_id"],
        "@asha_repo": task["repository"]["identity"],
        "@asha_workspace": task["jj"]["workspace_name"],
        "@asha_change": task["jj"]["change_id"],
        "@asha_state": "starting",
    }


def _pane_options(run_id: str, harness: str, role: str) -> dict[str, str]:
    return {
        "@asha_run_id": run_id,
        "@asha_harness": harness,
        "@asha_role": role,
        "@asha_state": "starting",
    }


def _ownership_matches(
    tmux: TmuxAdapter, task: dict[str, Any], *, require_present: bool = True,
) -> bool:
    session = task["tmux"]["session"]
    if not tmux.has_session(session):
        return not require_present
    return (
        tmux.session_option(session, "@asha_managed") == "1"
        and tmux.session_option(session, "@asha_task_id") == task["task_id"]
    )


def _verify_created_options(
    tmux: TmuxAdapter,
    task: dict[str, Any],
    pane_id: str,
    expected_session: dict[str, str],
    expected_pane: dict[str, str],
) -> None:
    session = task["tmux"]["session"]
    for option, expected in expected_session.items():
        if tmux.session_option(session, option) != expected:
            raise LaunchError(f"created tmux session option mismatch: {option}")
    for option, expected in expected_pane.items():
        if tmux.pane_option(pane_id, option) != expected:
            raise LaunchError(f"created tmux pane option mismatch: {option}")


def _recovery_commands(task: dict[str, Any], tmux: TmuxAdapter) -> str:
    socket = [] if tmux.socket is None else ["-L", tmux.socket]
    attach = " ".join([
        tmux.executable, *socket, "attach-session", "-t", task["tmux"]["session"]
    ])
    return f"asha task show {task['task_id']}; {attach}"


def _reconciled_evidence(run: dict[str, Any]) -> str:
    evidence = run.get("evidence", [])
    summary = "; ".join(
        f"{item['source']}={item['outcome']}: {item['detail']}"
        for item in evidence
    )
    return (summary or f"reconciled terminal state: {run['state']}")[:500]


def _mark_prelaunch_preserved(
    config: ControlConfig,
    journals: CreationJournalStore,
    journal: dict[str, Any],
) -> None:
    if journal["phase"] != "preserved":
        _save_phase(journals, journal, "preserved", None)
    # The reviewed rollback path owns the exact failed-task bookkeeping for a
    # preserved creation transaction.  It intentionally refuses all removal.
    try:
        rollback_prelaunch(config, journal["task_id"])
    except PreparationError:
        pass


def _rollback_before_launch(
    config: ControlConfig,
    task: dict[str, Any],
    tmux: TmuxAdapter,
    *,
    created_session: bool,
    jj: JjAdapter | None = None,
) -> str | None:
    if created_session:
        try:
            if not tmux.has_session(task["tmux"]["session"]):
                created_session = False
            elif not _ownership_matches(tmux, task):
                return "created tmux session ownership could not be verified; preserved"
            else:
                tmux.kill_session(task["tmux"]["session"])
        except (TmuxError, ValueError) as exc:
            return f"created tmux session could not be safely removed: {exc}"
    try:
        if jj is None:
            rollback_prelaunch(config, task["task_id"])
        else:
            rollback_prelaunch(config, task["task_id"], jj=jj)
    except PreparationError as exc:
        return str(exc)
    return None


def _make_run(
    *,
    run_id: str,
    harness: str,
    role: str,
    pane_id: str,
    pid: int,
    identity: str,
    evidence: str = "controller launch",
) -> dict[str, Any]:
    return {
        "contract": RUN_CONTRACT,
        "run_id": run_id,
        "harness": harness,
        "role": role,
        "pane_id": pane_id,
        "pid": pid,
        "process_start_identity": identity,
        "harness_session_id": None,
        "state": "starting",
        "evidence": evidence,
        "evidence_at": _now(),
    }


def _preserve_after_launch(
    tasks: TaskStore,
    journals: CreationJournalStore,
    task_id: str,
    journal: dict[str, Any],
    run: dict[str, Any] | None,
) -> dict[str, Any]:
    current = tasks.read(task_id)
    changed = copy.deepcopy(current)
    known = next(
        (item for item in changed["runs"] if run is not None and item["run_id"] == run["run_id"]),
        None,
    )
    if run is not None and known is None:
        changed["runs"].append(copy.deepcopy(run))
    if changed["lifecycle"] in {"creating", "running"}:
        changed["lifecycle"] = "failed"
    changed["updated_at"] = _now()
    if changed != current:
        tasks.save(changed, expected_digest=task_digest(current))
    latest_journal = journals.read(task_id)
    # Entering step 8 means process execution is possible even when the
    # launch-attempted journal replacement itself reported an indeterminate
    # result.  Preserve that monotonic rollback guard on every recovery path.
    latest_journal["launch_attempted"] = True
    latest_journal["task"]["digest"] = task_digest(changed)
    if latest_journal["phase"] in {"tmux-session-created", "launch-attempted"}:
        previous = latest_journal["phase"]
        latest_journal["phase"] = "preserved"
        journals.save(latest_journal, expected_phase=previous)
    elif latest_journal["phase"] == "preserved":
        journals.save(latest_journal, expected_phase="preserved")
    # A run-recorded replacement may have become visible before its save
    # reported an indeterminate durability error.  That terminal journal phase
    # cannot move backward; the failed task record still carries recovery facts.
    return changed


def launch_task(
    config: ControlConfig,
    task: dict[str, Any],
    *,
    tmux: TmuxAdapter | None = None,
    tasks: TaskStore | None = None,
    journals: CreationJournalStore | None = None,
    harness: str,
    goal_args=(),
    role: str = "implementer",
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Launch exactly one primary run from a prepared creation transaction."""
    if not isinstance(task, dict) or "task_id" not in task:
        raise LaunchError("launch requires a task record")
    try:
        task_id = canonical_uuid(task["task_id"])
    except ValueError as exc:
        raise LaunchError(str(exc)) from exc
    task_store = tasks or TaskStore(config)
    journal_store = journals or CreationJournalStore(config)
    adapter = tmux or _adapter_for_task(task)
    created_session = False
    irrevocable = False
    journal: dict[str, Any] | None = None
    current: dict[str, Any] | None = None
    run: dict[str, Any] | None = None

    with task_store.transaction_lock(task_id):
        current = task_store.read(task_id)
        journal = journal_store.read(task_id)
        if journal["phase"] != "ready-for-launch" or current["lifecycle"] != "creating":
            raise LaunchError(
                "task launch requires lifecycle creating and journal phase ready-for-launch"
            )
        try:
            selected_harness = harness_api.validate_harness(harness)
            selected_role = harness_api.validate_role(role)
            run_id = new_uuid()
            environment = harness_api.controller_env(
                task_id=task_id, run_id=run_id, state_dir=config.tasks_dir,
            )
            raw_root = os.environ.get("ASHA_ROOT")
            if raw_root is None:
                asha_root = Path(__file__).resolve().parents[2]
            else:
                supplied = Path(raw_root)
                if not supplied.is_absolute():
                    raise LaunchError("ASHA_ROOT must be an absolute path")
                asha_root = supplied.resolve()
            command = harness_api.launch_argv(asha_root, selected_harness, goal_args)
            _inject(failure_injector, "validated")

            session = current["tmux"]["session"]
            if adapter.has_session(session):
                managed = adapter.session_option(session, "@asha_managed")
                owner = adapter.session_option(session, "@asha_task_id")
                _save_phase(journal_store, journal, "preserved", None)
                _mark_prelaunch_preserved(config, journal_store, journal)
                if managed == "1" and owner == task_id:
                    raise LaunchError(
                        "owned tmux session already exists; launch recovery is not implemented in this release"
                    )
                raise LaunchError(
                    f"foreign tmux session collision at {session}; session was left untouched"
                )
            _inject(failure_injector, "session-available")

            _save_phase(journal_store, journal, "tmux-intent", failure_injector)
            expected_session = _session_options(current)
            expected_pane = _pane_options(run_id, selected_harness, selected_role)
            # A chained new-session can create the session and then report a
            # later option failure.  Mark the attempt first so cleanup probes
            # for an exact owned survivor instead of assuming no mutation.
            created_session = True
            pane_id = adapter.create_task_session(
                session=session,
                window=current["tmux"]["window"],
                start_directory=current["jj"]["workspace_path"],
                environment=environment,
                holder_argv=["sleep", "3600"],
                session_options=expected_session,
                pane_options=expected_pane,
                pane_title=f"asha:{selected_harness}:{selected_role}",
            )
            _inject(failure_injector, "tmux-created")
            _verify_created_options(
                adapter, current, pane_id, expected_session, expected_pane,
            )
            _inject(failure_injector, "tmux-verified")
            _save_phase(
                journal_store, journal, "tmux-session-created", failure_injector,
            )

            irrevocable = True
            journal["launch_attempted"] = True
            _save_phase(journal_store, journal, "launch-attempted", failure_injector)
            adapter.respawn(pane_id, command)
            _inject(failure_injector, "respawned")
            facts = adapter.pane_facts(pane_id)
            if facts.dead or facts.pane_pid is None:
                raise LaunchError("launched process exited before its identity could be recorded")
            identity = harness_api.process_identity(facts.pane_pid)
            if identity is None:
                raise LaunchError("launched process identity disappeared before it could be recorded")
            if not harness_api.pane_ancestry_ok(facts.pane_pid, adapter.server_pid()):
                raise LaunchError("launched process does not have verified tmux server ancestry")
            run = _make_run(
                run_id=run_id,
                harness=selected_harness,
                role=selected_role,
                pane_id=pane_id,
                pid=facts.pane_pid,
                identity=identity,
            )
            _inject(failure_injector, "process-identified")

            launched = copy.deepcopy(current)
            launched["lifecycle"] = "running"
            launched["runs"].append(run)
            launched["updated_at"] = run["evidence_at"]
            task_store.save(launched, expected_digest=task_digest(current))
            _inject(failure_injector, "run-saved")
            journal["task"]["digest"] = task_digest(launched)
            # Inject before the terminal phase so any injected failure still
            # has the legal launch-attempted -> preserved recovery edge.
            _inject(failure_injector, "before:run-recorded")
            _save_phase(
                journal_store, journal, "run-recorded", None, inject_after=False,
            )
            return {
                "task": launched,
                "run": run,
                "session": session,
                "pane": pane_id,
                "workspace": {
                    "path": launched["jj"]["workspace_path"],
                    "name": launched["jj"]["workspace_name"],
                    "change_id": launched["jj"]["change_id"],
                },
            }
        except BaseException as exc:
            ordinary = isinstance(exc, Exception)
            if isinstance(exc, LaunchError) and journal["phase"] == "preserved":
                raise
            if irrevocable:
                try:
                    preserved = _preserve_after_launch(
                        task_store, journal_store, task_id, journal, run,
                    )
                except Exception as recovery_exc:
                    if not ordinary:
                        raise exc
                    raise LaunchError(
                        f"launch failed and recovery recording was interrupted: {recovery_exc}; "
                        f"{_recovery_commands(current, adapter)}"
                    ) from exc
                if not ordinary:
                    raise
                raise LaunchError(
                    f"launch failed after process execution became possible; resources preserved: "
                    f"{exc}; {_recovery_commands(preserved, adapter)}"
                ) from exc

            cleanup_error = _rollback_before_launch(
                config, current, adapter, created_session=created_session,
            )
            if cleanup_error is not None:
                latest = journal_store.read(task_id)
                if latest["phase"] in {"tmux-intent", "tmux-session-created"}:
                    _save_phase(journal_store, latest, "preserved", None)
                    _mark_prelaunch_preserved(config, journal_store, latest)
                if not ordinary:
                    raise
                raise LaunchError(
                    f"launch failed before process execution; recovery was preserved: "
                    f"{exc}; {cleanup_error}"
                ) from exc
            if not ordinary:
                raise
            raise LaunchError(f"launch failed before process execution and rolled back: {exc}") from exc


def stop_task(
    config: ControlConfig,
    task: dict[str, Any],
    *,
    tmux: TmuxAdapter | None = None,
    tasks: TaskStore | None = None,
    terminate: bool = False,
    signaler: Callable[[int, int], None] = os.kill,
) -> dict[str, Any]:
    """Signal the latest verified run without killing tmux or touching jj."""
    task_id = canonical_uuid(task["task_id"])
    task_store = tasks or TaskStore(config)
    adapter = tmux or _adapter_for_task(task)
    with task_store.transaction_lock(task_id):
        current = task_store.read(task_id)
        if not current["runs"]:
            raise LaunchError("task has no run to stop")
        run = current["runs"][-1]
        if not _ownership_matches(adapter, current):
            raise LaunchError("tmux session ownership does not match the task record")
        if adapter.pane_option(run["pane_id"], "@asha_run_id") != run["run_id"]:
            raise LaunchError("tmux pane ownership does not match the run record")
        facts = adapter.pane_facts(run["pane_id"])
        if (facts.session != current["tmux"]["session"] or
                facts.window != current["tmux"]["window"]):
            raise LaunchError("tmux pane target does not match the run record")
        pane_pid = facts.pane_pid or 0
        if not harness_api.stop_signal_allowed(
            pid=run["pid"],
            expected_identity=run["process_start_identity"],
            pane_pid=pane_pid,
            server_pid=adapter.server_pid(),
            pane_dead=facts.dead,
        ):
            raise LaunchError("run process identity or tmux ancestry could not be verified")
        selected_signal = signal.SIGTERM if terminate else signal.SIGINT
        try:
            signaler(run["pid"], selected_signal)
        except OSError as exc:
            raise LaunchError(f"verified run could not be signaled: {exc}") from exc
        return {
            "task_id": task_id,
            "run_id": run["run_id"],
            "pid": run["pid"],
            "signal": "TERM" if terminate else "INT",
        }


def archive_task(
    config: ControlConfig,
    task: dict[str, Any],
    *,
    tasks: TaskStore | None = None,
    adapters: Adapters | None = None,
    journals: CreationJournalStore | None = None,
    jj: JjAdapter | None = None,
) -> dict[str, Any]:
    """Persist terminal evidence, then archive without mutating external state."""
    task_id = canonical_uuid(task["task_id"])
    task_store = tasks or TaskStore(config)
    jj_adapter = jj or JjAdapter()
    live = adapters or LiveAdapters(config=config, jj=jj_adapter)
    journal_store = journals or CreationJournalStore(config)
    with task_store.transaction_lock(task_id):
        current = task_store.read(task_id)
        if current["lifecycle"] == "archived":
            raise LaunchError("task is already archived")
        if current["lifecycle"] not in {"running", "ended"}:
            raise LaunchError(
                "only a running task whose runs have all exited, or an ended "
                "task, can be archived"
            )
        ended = current
        previous_digest = task_digest(current)
        if current["lifecycle"] == "running":
            # Local import avoids the view -> launch controller dependency at
            # module initialization time.
            from . import view

            snapshot = view.reconcile_with_creation(
                current, live, journal_store, jj_adapter,
            )
            for run in snapshot["runs"]:
                if run["state"] not in {"exited", "failed"} or run["blocker"] is not None:
                    raise LaunchError(
                        "only a task whose runs have all exited can be archived; "
                        f"run {run['run_id']} is {run['state']}"
                    )
            ended = copy.deepcopy(current)
            timestamp = _now()
            derived = {run["run_id"]: run for run in snapshot["runs"]}
            for run in ended["runs"]:
                reconciled = derived[run["run_id"]]
                run["state"] = reconciled["state"]
                run["evidence"] = _reconciled_evidence(reconciled)
                run["evidence_at"] = timestamp
            ended["lifecycle"] = "ended"
            ended["updated_at"] = timestamp
            task_store.save(ended, expected_digest=previous_digest)
            previous_digest = task_digest(ended)

        archived = copy.deepcopy(ended)
        archived["lifecycle"] = "archived"
        archived["updated_at"] = _now()
        task_store.save(archived, expected_digest=previous_digest)
        return archived


def unarchive_task(
    config: ControlConfig,
    task: dict[str, Any],
    *,
    tasks: TaskStore | None = None,
) -> dict[str, Any]:
    """Restore an archived task to its terminal ended lifecycle."""
    task_id = canonical_uuid(task["task_id"])
    task_store = tasks or TaskStore(config)
    with task_store.transaction_lock(task_id):
        current = task_store.read(task_id)
        if current["lifecycle"] != "archived":
            raise LaunchError("only an archived task can be unarchived")
        changed = copy.deepcopy(current)
        changed["lifecycle"] = "ended"
        changed["updated_at"] = _now()
        task_store.save(changed, expected_digest=task_digest(current))
        return changed


_PRE_TMUX_PHASES = frozenset({
    "intent", "task-recorded", "parent-intent", "parent-ready",
    "workspace-add-intent", "workspace-added", "workspace-recorded",
    "context-intent", "context-provisioning", "context-provisioned",
    "task-identity-intent", "task-identity-recorded", "ready-for-launch",
    "rollback-intent", "workspace-forgotten", "removing",
})


def recover_task(
    config: ControlConfig,
    task: dict[str, Any],
    *,
    tasks: TaskStore,
    journals: CreationJournalStore,
    tmux: TmuxAdapter | None = None,
    jj: JjAdapter | None = None,
) -> dict[str, Any]:
    """Recover one durable task-creation transaction without adopting state."""
    task_id = canonical_uuid(task["task_id"])
    adapter = tmux or _adapter_for_task(task)
    jj_adapter = jj or JjAdapter()
    recovery_commands: str | None = None
    with tasks.transaction_lock(task_id):
        current = tasks.read(task_id)
        if current["lifecycle"] != "creating":
            raise LaunchError(
                "task is not in an interrupted creation state; nothing to recover"
            )
        journal = journals.read(task_id)
        phase = journal["phase"]
        if phase in _PRE_TMUX_PHASES:
            try:
                rollback_prelaunch(config, task_id, jj=jj_adapter)
            except PreparationError as exc:
                message = str(exc)
            else:
                message = "rolled back"
        elif phase in {"tmux-intent", "tmux-session-created"}:
            message = _rollback_before_launch(
                config, current, adapter, created_session=True, jj=jj_adapter,
            ) or "rolled back"
        elif phase == "launch-attempted":
            preserved = _preserve_after_launch(
                tasks, journals, task_id, journal, run=None,
            )
            live_process = False
            try:
                if _ownership_matches(adapter, preserved):
                    live_process = not adapter.window_pane_facts(
                        preserved["tmux"]["session"], preserved["tmux"]["window"],
                    ).dead
            except (TmuxError, ValueError):
                live_process = False
            if live_process:
                commands = _recovery_commands(preserved, adapter)
                attach = commands.split("; ", 1)[1]
                message = (
                    "harness process may still be live in tmux session "
                    f"{preserved['tmux']['session']}; attach with: {attach}; "
                    "stop it manually before reusing the destination"
                )
                recovery_commands = commands
            else:
                message = (
                    "no live process found; task marked failed and workspace preserved"
                )
                recovery_commands = _recovery_commands(preserved, adapter)
        elif phase in {"preserved", "rolled-back"}:
            message = f"creation transaction is {phase}; no action is needed"
        elif phase == "run-recorded":
            raise LaunchError(
                "creation journal is run-recorded while the task is still creating; "
                "no automatic recovery is safe"
            )
        else:
            raise LaunchError(f"unsupported interrupted creation phase: {phase}")

        resulting_task = tasks.read(task_id)
        resulting_journal = journals.read(task_id)
        return {
            "task": resulting_task,
            "journal": resulting_journal,
            "message": message,
            "recovery_commands": recovery_commands,
        }
