"""Immutable bindings from orchestration attempts to opaque Control tasks."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..model import validate_task
from ..store import task_digest
from .model import LINK_CONTRACT, validate_action, validate_attempt, validate_link


def control_task_identity_digest(control_task: dict[str, Any]) -> str:
    """Digest the immutable Control identity bound by an orchestration link."""
    task = validate_task(control_task)
    primary = task["runs"][0]
    identity = {
        "task_id": task["task_id"],
        "repository": {
            "root": task["repository"]["root"],
            "identity": task["repository"]["identity"],
        },
        "jj": {
            field: task["jj"][field]
            for field in (
                "workspace_name", "workspace_path", "change_id", "base_commit_id",
            )
        },
        "tmux": {
            field: task["tmux"][field]
            for field in ("socket", "session", "window")
        },
        "primary_run": {
            field: primary[field]
            for field in (
                "run_id", "pid", "process_start_identity", "harness", "role",
            )
        },
        "label": task["label"],
    }
    raw = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def build_link(
    initiative: dict[str, Any],
    node: dict[str, Any],
    attempt: dict[str, Any],
    action: dict[str, Any],
    control_task: dict[str, Any],
) -> dict[str, Any]:
    """Build the closed v1 sidecar without extending the Control record."""
    checked_action = validate_action(action)
    checked_attempt = validate_attempt(attempt)
    if initiative["active_plan"] is None:
        raise ValueError("initiative has no active plan")
    if checked_attempt["initiative_id"] != initiative["initiative_id"]:
        raise ValueError("attempt belongs to another initiative")
    if checked_attempt["node_id"] != node["node_id"]:
        raise ValueError("attempt belongs to another node")
    if checked_attempt["action_id"] not in {None, checked_action["action_id"]}:
        raise ValueError("attempt belongs to another action")
    if control_task["task_id"] != checked_attempt["task_id"]:
        raise ValueError("Control task identity differs from the reserved task")
    value = {
        "contract": LINK_CONTRACT,
        "initiative_id": initiative["initiative_id"],
        "active_plan_digest": initiative["active_plan"]["digest"],
        "node_id": node["node_id"],
        "attempt_id": checked_attempt["attempt_id"],
        "action_id": checked_action["action_id"],
        "actor_kind": checked_action["actor_kind"],
        "actor_id": checked_action["actor_id"],
        "expected_initiative_revision": checked_action["expected_state_revision"],
        "control_task_id": control_task["task_id"],
        "control_task_identity_digest": control_task_identity_digest(control_task),
        "control_task_record_digest": task_digest(control_task),
    }
    if checked_action["actor_kind"] == "coordinator":
        value["coordinator_generation"] = checked_action["coordinator_generation"]
    return validate_link(value)


__all__ = ["build_link", "control_task_identity_digest"]
