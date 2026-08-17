"""Read-only Orchestration Core installation and contract probes."""

from __future__ import annotations

import inspect
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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


def run_orchestration_doctor(config: OrchestrationConfig) -> dict[str, Any]:
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
    limitations = [probe.detail for probe in probes if probe.outcome != "match"]
    return {
        "contract": DOCTOR_CONTRACT,
        "ok": all(probe.outcome == "match" for probe in probes),
        "probes": [asdict(probe) for probe in probes],
        "limitations": limitations,
    }


run_doctor = run_orchestration_doctor

__all__ = ["DOCTOR_CONTRACT", "Probe", "run_doctor", "run_orchestration_doctor"]
