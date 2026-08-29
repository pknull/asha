"""Read-only Orchestration Core installation and contract probes."""

from __future__ import annotations

import inspect
import os
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from .. import cli as control_cli
from .. import doctor as control_doctor_module
from .. import events as control_events
from .. import model as control_model
from .. import prepare as control_prepare
from ..config import (
    ConfigError,
    reject_symlink_components,
    reject_unsafe_writable_ancestors,
    require_existing_directory_components,
)
from ..doctor import run_doctor as run_control_doctor
from .config import OrchestrationConfig
from .model import approval_decider, is_operator_decider_actor


DOCTOR_CONTRACT = "asha.orchestration-doctor.v1"


@dataclass(frozen=True)
class Probe:
    name: str
    outcome: str
    detail: str


def _contracts_probe() -> Probe:
    expected = {
        "asha.control-task.v1": control_model.TASK_CONTRACT,
        "asha.control-run.v1": control_model.RUN_CONTRACT,
        "asha.control-event.v1": control_events.EVENT_CONTRACT,
    }
    try:
        sources = "\n".join((
            inspect.getsource(control_cli), inspect.getsource(control_doctor_module),
            inspect.getsource(control_prepare),
        ))
    except OSError as exc:
        return Probe(
            "control-contracts", "unavailable",
            f"live Control producers could not be inspected: {exc}",
        )
    strings = (
        "asha.control-task-list.v1", "asha.control-task-show.v1",
        "asha.control-reconcile-list.v1", "asha.control-doctor.v1",
        "asha.control-task-context.v1",
    )
    missing = [name for name, actual in expected.items() if actual != name]
    missing.extend(name for name in strings if name not in sources)
    if missing:
        return Probe("control-contracts", "mismatch", f"live Control producers lack: {', '.join(missing)}")
    return Probe("control-contracts", "match", "all frozen Control v1 identifiers have live producers")


def _root_probe(path: Path) -> Probe:
    try:
        metadata = path.lstat()
        reject_symlink_components(path, "orchestration initiatives root")
        require_existing_directory_components(path, "orchestration initiatives root")
        reject_unsafe_writable_ancestors(path, "orchestration initiatives root")
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return Probe("initiatives-root", "mismatch", "initiatives root is not a direct directory")
        if metadata.st_uid != os.geteuid():
            return Probe("initiatives-root", "mismatch", "initiatives root is not owned by the effective user")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            return Probe("initiatives-root", "mismatch", "initiatives root must have mode 0700")
        if not os.access(path, os.W_OK | os.X_OK):
            return Probe("initiatives-root", "mismatch", "initiatives root is not writable")
        return Probe("initiatives-root", "match", "initiatives root is direct, private, owned, and writable")
    except FileNotFoundError:
        return Probe("initiatives-root", "missing", "initiatives root does not exist")
    except ConfigError as exc:
        return Probe("initiatives-root", "mismatch", str(exc)[:400])
    except OSError as exc:
        return Probe("initiatives-root", "unavailable", f"initiatives root could not be inspected: {exc}")


# Advisory probes inform `limitations` but never block `ok`: `activate-initiative`
# refuses on a false `ok`, coordinator support must not gate operator-only use,
# and one suspect historical approval must not brick activation plane-wide.
_ADVISORY_PROBES = frozenset({"coordinator-seam", "approval-provenance"})

# Bound the mismatch detail the way `_root_probe` bounds its own.
_MAX_SUSPECT_APPROVALS = 5


def _approval_decider(
    approval: Mapping[str, Any], events: list[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Who signed one approval: the record first, then its journal.

    Records written before the requester/decider split carry no `decided_by`,
    so for those the `approval-decided` event is the only decider provenance.
    An approval with neither is unresolved, not suspect.
    """
    decider = approval_decider(approval)
    if decider is not None:
        return decider
    for event in reversed(events):
        if (
            event["type"] == "approval-decided"
            and approval["request_id"] in event["subject_ids"]
        ):
            return {"actor_id": event["actor_id"], "actor_kind": event["actor_kind"]}
    return None


def _suspect_approvals(
    approvals: list[Mapping[str, Any]], events: list[Mapping[str, Any]]
) -> list[str]:
    """Decided approvals whose decider cannot stand behind an operator decision.

    An operator-kind decision demands an operator surface or a named standing
    authority, and no live producer ever records a coordinator-kind decision.
    A `controller` decider is legitimate: that is how a standing authority
    journals its own pre-signed approval.
    """
    suspect = []
    for approval in approvals:
        if approval["state"] == "requested":
            continue
        decider = _approval_decider(approval, events)
        if decider is None:
            continue
        if decider["actor_kind"] == "coordinator" or (
            decider["actor_kind"] == "operator"
            and not is_operator_decider_actor(decider["actor_id"])
        ):
            suspect.append(
                f"{approval['request_id']} decided by {decider['actor_kind']} "
                f"{decider['actor_id']}"
            )
    return suspect


def _approval_provenance_probe(config: OrchestrationConfig) -> Probe:
    """Audit recorded approval deciders across every retained initiative.

    Advisory by design: the enforcing fences are `validate_approval` and
    `approve_salvage`, and this probe only reports what is already on disk.
    """
    from .store import InitiativeStore

    suspect: list[str] = []
    decided = 0
    try:
        store = InitiativeStore(config)
        for initiative in store.list_initiatives():
            initiative_id = initiative["initiative_id"]
            approvals = [
                approval for approval in store.list_approvals_snapshot(initiative_id)
                if approval["state"] != "requested"
            ]
            if not approvals:
                continue
            decided += len(approvals)
            events = (
                store.list_events_snapshot(initiative_id)
                if any(approval_decider(approval) is None for approval in approvals)
                else []
            )
            suspect.extend(_suspect_approvals(approvals, events))
    except (OSError, ValueError) as exc:
        return Probe(
            "approval-provenance", "unavailable",
            f"retained approvals could not be read: {exc}"[:400],
        )
    if suspect:
        return Probe(
            "approval-provenance", "mismatch",
            (
                "approval decisions lack an operator surface or a named standing "
                f"authority: {', '.join(suspect[:_MAX_SUSPECT_APPROVALS])}"
            )[:400],
        )
    return Probe(
        "approval-provenance", "match",
        f"{decided} decided approval(s) name an operator surface or a named authority",
    )


def _coordinator_seam_probe() -> Probe:
    """Live producers for the coordinator verbs and a callable tmux executable."""
    try:
        from . import coordinator as coordinator_module
        from . import cli as orchestration_cli

        source = inspect.getsource(orchestration_cli) + inspect.getsource(coordinator_module)
    except (ImportError, OSError) as exc:
        return Probe("coordinator-seam", "unavailable", f"coordinator verbs could not be inspected: {exc}")
    required = {
        "coordinator claim": ('"coordinator"', "asha.orchestration-coordinator-claim.v1"),
        "wait": ('"wait"', coordinator_module.WAIT_CONTRACT),
        "coordinator show": ('"show"', coordinator_module.COORDINATOR_SHOW_CONTRACT),
        "propose-plan": ('"propose-plan"', "plan-proposed"),
    }
    missing = [
        verb for verb, markers in required.items()
        if any(marker not in source for marker in markers)
    ]
    if missing:
        return Probe("coordinator-seam", "mismatch", f"coordinator verbs lack live producers: {', '.join(missing)}")
    if shutil.which("tmux") is None:
        return Probe("coordinator-seam", "unavailable", "tmux is not on PATH; coordinator claim needs a tmux pane")
    return Probe("coordinator-seam", "match", "coordinator claim, wait, and show have live producers and tmux is present")


def run_orchestration_doctor(
    config: OrchestrationConfig, *, audit_records: bool = True
) -> dict[str, Any]:
    """Probe this installation; `audit_records` also walks retained approvals.

    The record audit reads every initiative's approvals and, where a record
    predates the requester/decider split, its journal.  That cost belongs to
    the operator's diagnostic, not to the runtime capability handshake, which
    asks whether this installation can run work at all.
    """
    probes = [
        Probe("orchestration-config", "match", "orchestration configuration parsed and passed static safety validation"),
        _root_probe(config.initiatives_dir),
        _contracts_probe(),
    ]
    try:
        marker = "11111111-1111-4111-8111-111111111111"
        parsed = control_cli._parse_start(["--task-id", marker, "--goal", "x"])
        if parsed["task_id"] != marker:
            raise ValueError("task ID did not round-trip")
        probes.append(Probe("create-by-id", "match", "task start parser round-trips --task-id without starting a task"))
    except (KeyError, ValueError) as exc:
        probes.append(Probe("create-by-id", "mismatch", f"create-by-id parser seam failed: {exc}"))
    try:
        result = run_control_doctor(config.control)
        probes.append(Probe(
            "control-doctor", "match" if result.get("ok") is True else "mismatch",
            "Control doctor reports ok" if result.get("ok") is True else "Control doctor reports one or more blocking probes",
        ))
    except (OSError, ValueError) as exc:
        probes.append(Probe("control-doctor", "unavailable", f"Control doctor failed: {exc}"))
    probes.append(_coordinator_seam_probe())
    if audit_records:
        probes.append(_approval_provenance_probe(config))
    limitations = [probe.detail for probe in probes if probe.outcome != "match"]
    return {
        "contract": DOCTOR_CONTRACT,
        "ok": all(
            probe.outcome == "match" for probe in probes if probe.name not in _ADVISORY_PROBES
        ),
        "probes": [asdict(probe) for probe in probes],
        "limitations": limitations,
    }


run_doctor = run_orchestration_doctor

__all__ = ["DOCTOR_CONTRACT", "Probe", "run_doctor", "run_orchestration_doctor"]
