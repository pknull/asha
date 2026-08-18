"""Lazy orchestration configuration layered beside strict Control v1 config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..config import (
    HARNESSES,
    ConfigError,
    ControlConfig,
    _read_json,
    load_config as load_control_config,
    reject_symlink_components,
    reject_unsafe_writable_ancestors,
    require_existing_directory_components,
)


ORCHESTRATION_CONFIG_CONTRACT = "asha.orchestration-config.v1"

DEFAULT_COORDINATOR_HARNESS = "claude"
DEFAULT_MAX_PARALLEL_TASKS = 3
DEFAULT_MAX_TOTAL_TASKS = 12
DEFAULT_MAX_ATTEMPTS_PER_NODE = 2
DEFAULT_MAX_REPAIR_CYCLES = 2
DEFAULT_MAX_RETAINED_BYTES_BEFORE_PAUSE = 10737418240
DEFAULT_MAX_RETAINED_INODES_BEFORE_PAUSE = 200000
DEFAULT_COORDINATOR_WAIT_SECONDS = 120
DEFAULT_RESULT_GRACE_SECONDS = 120
DEFAULT_LINK_GRACE_SECONDS = 30
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

_FIELDS = frozenset({
    "contract",
    "default_coordinator_harness",
    "max_parallel_tasks",
    "max_total_tasks",
    "max_attempts_per_node",
    "max_repair_cycles",
    "max_retained_bytes_before_pause",
    "max_retained_inodes_before_pause",
    "coordinator_wait_seconds",
    "result_grace_seconds",
    "link_grace_seconds",
    "max_consecutive_failures",
})
_LIMIT_FIELDS = (
    "max_parallel_tasks",
    "max_total_tasks",
    "max_attempts_per_node",
    "max_repair_cycles",
    "max_retained_bytes_before_pause",
    "max_retained_inodes_before_pause",
    "coordinator_wait_seconds",
    "result_grace_seconds",
    "link_grace_seconds",
    "max_consecutive_failures",
)


class OrchestrationConfigError(ValueError):
    """The separate orchestration configuration is invalid or unsafe."""


@dataclass(frozen=True)
class OrchestrationConfig:
    contract: str
    control: ControlConfig
    initiatives_dir: Path
    default_coordinator_harness: str
    max_parallel_tasks: int
    max_total_tasks: int
    max_attempts_per_node: int
    max_repair_cycles: int
    max_retained_bytes_before_pause: int
    max_retained_inodes_before_pause: int
    coordinator_wait_seconds: int
    result_grace_seconds: int
    link_grace_seconds: int
    max_consecutive_failures: int

    @property
    def config_path(self) -> Path:
        return self.control.config_path

    @property
    def home(self) -> Path:
        return self.control.home

    @property
    def initiatives_root(self) -> Path:
        """Compatibility spelling for callers that treat the XDG path as a root."""
        return self.initiatives_dir


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OrchestrationConfigError(f"orchestration.{name} must be a positive integer")
    return value


def load_config(env: Mapping[str, str] | None = None) -> OrchestrationConfig:
    """Load orchestration configuration only when an orchestration caller asks.

    Control parses the same document independently and intentionally ignores the
    root ``orchestration`` key.  Keeping this second parse here prevents a bad or
    future orchestration contract from changing ordinary ``asha task`` behavior.
    """
    try:
        control = load_control_config(env)
        root = _read_json(control.config_path)
    except ConfigError as exc:
        raise OrchestrationConfigError(str(exc)) from exc

    raw = root.get("orchestration", {})
    if not isinstance(raw, dict):
        raise OrchestrationConfigError("orchestration must be an object")
    unknown = raw.keys() - _FIELDS
    if unknown:
        raise OrchestrationConfigError(
            f"orchestration has {len(unknown)} unsupported field(s)"
        )
    contract = raw.get("contract", ORCHESTRATION_CONFIG_CONTRACT)
    if contract != ORCHESTRATION_CONFIG_CONTRACT:
        raise OrchestrationConfigError(
            f"orchestration.contract must be {ORCHESTRATION_CONFIG_CONTRACT}"
        )
    harness = raw.get("default_coordinator_harness", DEFAULT_COORDINATOR_HARNESS)
    if not isinstance(harness, str) or harness not in HARNESSES:
        raise OrchestrationConfigError(
            "orchestration.default_coordinator_harness must name a supported harness"
        )
    defaults = {
        "max_parallel_tasks": DEFAULT_MAX_PARALLEL_TASKS,
        "max_total_tasks": DEFAULT_MAX_TOTAL_TASKS,
        "max_attempts_per_node": DEFAULT_MAX_ATTEMPTS_PER_NODE,
        "max_repair_cycles": DEFAULT_MAX_REPAIR_CYCLES,
        "max_retained_bytes_before_pause": DEFAULT_MAX_RETAINED_BYTES_BEFORE_PAUSE,
        "max_retained_inodes_before_pause": DEFAULT_MAX_RETAINED_INODES_BEFORE_PAUSE,
        "coordinator_wait_seconds": DEFAULT_COORDINATOR_WAIT_SECONDS,
        "result_grace_seconds": DEFAULT_RESULT_GRACE_SECONDS,
        "link_grace_seconds": DEFAULT_LINK_GRACE_SECONDS,
        "max_consecutive_failures": DEFAULT_MAX_CONSECUTIVE_FAILURES,
    }
    limits = {
        name: _positive_integer(raw.get(name, defaults[name]), name)
        for name in _LIMIT_FIELDS
    }

    initiatives_dir = control.tasks_dir.parent / "initiatives"
    try:
        reject_symlink_components(initiatives_dir, "orchestration initiatives root")
        require_existing_directory_components(
            initiatives_dir, "orchestration initiatives root"
        )
        reject_unsafe_writable_ancestors(
            initiatives_dir, "orchestration initiatives root"
        )
    except ConfigError as exc:
        raise OrchestrationConfigError(str(exc)) from exc

    return OrchestrationConfig(
        contract=contract,
        control=control,
        initiatives_dir=initiatives_dir,
        default_coordinator_harness=harness,
        **limits,
    )


load_orchestration_config = load_config


__all__ = [
    "ORCHESTRATION_CONFIG_CONTRACT",
    "OrchestrationConfig",
    "OrchestrationConfigError",
    "load_config",
    "load_orchestration_config",
]
