"""Versioned task/run schema and explicit state transition graphs."""

from __future__ import annotations

import re
import uuid
import unicodedata
from datetime import datetime, timezone
from typing import Any, Mapping

from .config import is_canonical_absolute_path


TASK_CONTRACT = "asha.control-task.v1"
RUN_CONTRACT = "asha.control-run.v1"
SLUG_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")

# Task lifecycle is durable container state.  It is intentionally distinct
# from run status.  Archive is reversible only to an ended terminal lifecycle;
# no graph edge restarts a task or a terminal run.
TASK_LIFECYCLE_TRANSITIONS = {
    "creating": frozenset({"running", "failed"}),
    "running": frozenset({"ended", "failed"}),
    "ended": frozenset({"archived"}),
    "failed": frozenset(),
    "archived": frozenset({"ended"}),
}

_ACTIVE_RUN_STATES = frozenset({"starting", "working", "needs-input", "idle", "unknown", "stale"})
RUN_STATE_TRANSITIONS = {
    "starting": frozenset({"working", "needs-input", "idle", "unknown", "stale", "exited", "failed"}),
    "working": frozenset({"needs-input", "idle", "unknown", "stale", "exited", "failed"}),
    "needs-input": frozenset({"working", "idle", "unknown", "stale", "exited", "failed"}),
    "idle": frozenset({"working", "needs-input", "unknown", "stale", "exited", "failed"}),
    "unknown": frozenset({"working", "needs-input", "idle", "stale", "exited", "failed"}),
    "stale": frozenset({"starting", "working", "needs-input", "idle", "unknown", "exited", "failed"}),
}
RUN_STATE_TRANSITIONS.update({"exited": frozenset(), "failed": frozenset()})

_TASK_KEYS = frozenset({
    "contract", "task_id", "slug", "label", "created_at", "updated_at",
    "lifecycle", "repository", "source", "jj", "tmux", "runs",
})
_RUN_KEYS = frozenset({
    "contract", "run_id", "harness", "role", "pane_id", "pid",
    "process_start_identity", "harness_session_id", "state", "evidence", "evidence_at",
})
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
    re.ASCII,
)
GIT_OBJECT_ID_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
JJ_CHANGE_ID_PATTERN = re.compile(r"[k-z]{32}", re.ASCII)


class ModelError(ValueError):
    """A Control record violates its versioned contract."""


def new_uuid() -> str:
    """Generate the canonical authoritative identifier form used by records."""
    return str(uuid.uuid4())


def canonical_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ModelError("identifier must be a canonical UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ModelError("identifier must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ModelError("identifier must be a canonical UUID")
    return value


def validate_slug(value: Any) -> str:
    if not isinstance(value, str) or SLUG_PATTERN.fullmatch(value) is None:
        raise ModelError("slug must be 1-64 lowercase ASCII letters, digits, or interior hyphens")
    return value


def _object(value: Any, name: str, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ModelError(f"{name} must be an object")
    missing = keys - value.keys()
    extra = value.keys() - keys
    if missing:
        raise ModelError(f"{name} is missing {len(missing)} required field(s)")
    if extra:
        raise ModelError(f"{name} has {len(extra)} unexpected field(s)")
    return value


def _text(value: Any, name: str, *, minimum: int = 1, maximum: int = 200, pattern=None) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ModelError(f"{name} must contain {minimum}-{maximum} characters")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ModelError(f"{name} must not contain Unicode control characters")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ModelError(f"{name} uses an invalid restricted grammar")
    return value


def _optional_text(value: Any, name: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, name, maximum=maximum)


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ModelError(f"{name} must use bounded ASCII RFC3339 UTC")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ModelError(f"{name} must be RFC3339 UTC") from exc
    if result.tzinfo != timezone.utc:
        raise ModelError(f"{name} must be RFC3339 UTC")
    return result


def _canonical_path(value: Any, name: str) -> str:
    text = _text(value, name, maximum=4096)
    if not is_canonical_absolute_path(text, resolved=True):
        raise ModelError(f"{name} must be an exact resolved canonical path")
    return text


def validate_run(run: Any) -> Mapping[str, Any]:
    run = _object(run, "run", _RUN_KEYS)
    if run["contract"] != RUN_CONTRACT:
        raise ModelError(f"run contract must be {RUN_CONTRACT}")
    canonical_uuid(run["run_id"])
    token = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    _text(run["harness"], "run harness", maximum=64, pattern=token)
    _text(run["role"], "run role", maximum=64, pattern=token)
    _text(run["pane_id"], "run pane_id", maximum=24, pattern=re.compile(r"%[0-9]+"))
    if isinstance(run["pid"], bool) or not isinstance(run["pid"], int) or run["pid"] <= 0:
        raise ModelError("run pid must be a positive integer")
    _text(run["process_start_identity"], "run process_start_identity", maximum=200)
    _optional_text(run["harness_session_id"], "run harness_session_id", maximum=200)
    if run["state"] not in RUN_STATE_TRANSITIONS:
        raise ModelError("run state is not part of asha.control-run.v1")
    _text(run["evidence"], "run evidence", maximum=500)
    _timestamp(run["evidence_at"], "run evidence_at")
    return run


def validate_task(task: Any) -> dict[str, Any]:
    task = _object(task, "task", _TASK_KEYS)
    if task["contract"] != TASK_CONTRACT:
        raise ModelError(f"task contract must be {TASK_CONTRACT}")
    canonical_uuid(task["task_id"])
    validate_slug(task["slug"])
    _text(task["label"], "task label", maximum=200)
    created = _timestamp(task["created_at"], "task created_at")
    updated = _timestamp(task["updated_at"], "task updated_at")
    if updated < created:
        raise ModelError("task updated_at must not precede created_at")
    if task["lifecycle"] not in TASK_LIFECYCLE_TRANSITIONS:
        raise ModelError("task lifecycle is not part of asha.control-task.v1")

    repository = _object(task["repository"], "repository", frozenset({"root", "identity"}))
    _canonical_path(repository["root"], "repository root")
    _text(repository["identity"], "repository identity", maximum=200,
          pattern=re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}"))

    source = _object(task["source"], "source", frozenset({"kind", "number", "url"}))
    if source["kind"] not in {"ad-hoc", "pr", "issue"}:
        raise ModelError("source kind must be ad-hoc, pr, or issue")
    if source["kind"] == "ad-hoc":
        if source["number"] is not None or source["url"] is not None:
            raise ModelError("ad-hoc source number and URL must be null")
    else:
        if (isinstance(source["number"], bool) or not isinstance(source["number"], int)
                or source["number"] <= 0):
            raise ModelError("PR/issue source number must be a positive integer")
        url = _optional_text(source["url"], "source URL", maximum=2048)
        if url is not None and not re.match(r"https?://", url):
            raise ModelError("source URL must use http or https")

    jj = _object(task["jj"], "jj", frozenset({
        "workspace_name", "workspace_path", "requested_base", "base_commit_id",
        "change_id", "working_commit_id",
    }))
    restricted = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    _text(jj["workspace_name"], "jj workspace_name", maximum=128, pattern=restricted)
    _canonical_path(jj["workspace_path"], "jj workspace_path")
    _text(jj["requested_base"], "jj requested_base", maximum=500)
    _text(
        jj["base_commit_id"], "jj base_commit_id", maximum=64,
        pattern=GIT_OBJECT_ID_PATTERN,
    )
    if jj["change_id"] is not None:
        _text(
            jj["change_id"], "jj change_id", maximum=32,
            pattern=JJ_CHANGE_ID_PATTERN,
        )
    if jj["working_commit_id"] is not None:
        _text(
            jj["working_commit_id"], "jj working_commit_id", maximum=64,
            pattern=GIT_OBJECT_ID_PATTERN,
        )

    tmux = _object(task["tmux"], "tmux", frozenset({"socket", "session", "window"}))
    for name in ("socket", "session", "window"):
        _text(tmux[name], f"tmux {name}", maximum=128, pattern=restricted)

    if not isinstance(task["runs"], list):
        raise ModelError("runs must be an array")
    seen: set[str] = set()
    for run in task["runs"]:
        validate_run(run)
        evidence_at = _timestamp(run["evidence_at"], "run evidence_at")
        if evidence_at < created or evidence_at > updated:
            raise ModelError("run evidence timestamp is outside task chronology")
        if run["run_id"] in seen:
            raise ModelError("run IDs must be unique within a task")
        seen.add(run["run_id"])
    lifecycle = task["lifecycle"]
    runs = task["runs"]
    if lifecycle == "creating":
        if runs:
            raise ModelError("creating task must not have runs")
    elif lifecycle == "running":
        if jj["change_id"] is None or jj["working_commit_id"] is None or not runs:
            raise ModelError("running task requires jj change identity and at least one run")
        if any(run["state"] not in {"exited", "failed"} for run in runs[:-1]):
            raise ModelError("running task preceding runs must be terminal")
        if runs[-1]["state"] not in _ACTIVE_RUN_STATES:
            raise ModelError("running task latest run must be nonterminal")
    elif lifecycle in {"ended", "archived"}:
        if (jj["change_id"] is None or jj["working_commit_id"] is None or not runs
                or any(run["state"] != "exited" for run in runs)):
            raise ModelError(f"{lifecycle} task requires successful terminal runs")
    elif lifecycle == "failed" and runs:
        terminal_history = all(run["state"] in {"exited", "failed"} for run in runs)
        preserved_live_run = (
            all(run["state"] in {"exited", "failed"} for run in runs[:-1])
            and runs[-1]["state"] in _ACTIVE_RUN_STATES
        )
        if (jj["change_id"] is None or jj["working_commit_id"] is None
                or not (terminal_history or preserved_live_run)):
            raise ModelError(
                "failed task runs require jj identity and coherent terminal history or one preserved live run"
            )
    return task


def require_task_transition(current: str, requested: str) -> None:
    if current not in TASK_LIFECYCLE_TRANSITIONS or requested not in TASK_LIFECYCLE_TRANSITIONS[current]:
        raise ModelError(f"illegal task lifecycle transition: {current} -> {requested}")


def require_run_transition(current: str, requested: str) -> None:
    if current not in RUN_STATE_TRANSITIONS or requested not in RUN_STATE_TRANSITIONS[current]:
        raise ModelError(f"illegal run state transition: {current} -> {requested}")
