"""Bounded event snapshots and aggregate presentation for managed Control runs."""

from __future__ import annotations

import json
import os
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ControlConfig
from .harness import HarnessError, validate_harness
from .model import ModelError, canonical_uuid
from .store import (
    StoreError,
    _close_quietly,
    _directory_fd,
    _managed_start,
    _open_existing_file,
)
from .tmux import TmuxAdapter


class EventError(ValueError):
    """A Control event snapshot was invalid or could not be handled safely."""


EVENT_CONTRACT = "asha.control-event.v1"
EVENTS = (
    "session-start",
    "prompt-submitted",
    "tool-completed",
    "permission-requested",
    "turn-stopped",
    "session-ended",
)
EVENT_STATES: dict[str, str | None] = {
    "session-start": None,
    "prompt-submitted": "working",
    "tool-completed": "working",
    "permission-requested": "needs-input",
    "turn-stopped": "idle",
    # The nonzero override is applied by _event_state.
    "session-ended": "exited",
}
MAX_EVENT_BYTES = 4096
_SNAPSHOT_KEYS = frozenset({
    "contract", "task_id", "run_id", "event", "state", "harness",
    "harness_session_id", "exit_status", "pane_id", "observed_at",
})
_PANE_ID = re.compile(r"%[0-9]+", re.ASCII)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
    re.ASCII,
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class _DuplicateJsonKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


def _text(value: Any, name: str, *, maximum: int, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > maximum:
        qualifier = " or null" if optional else ""
        raise EventError(f"{name} must contain 1-{maximum} characters{qualifier}")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise EventError(f"{name} must not contain Unicode control characters")
    return value


def _exit_status(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255:
        raise EventError("exit_status must be an integer from 0 through 255 or null")
    return value


def _event_state(event: str, exit_status: int | None) -> str | None:
    if event == "session-ended" and exit_status not in {None, 0}:
        return "failed"
    return EVENT_STATES[event]


def _observed_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _events_path(config: ControlConfig) -> tuple[Path, int]:
    path = config.runtime_dir / "events"
    try:
        managed_start = _managed_start(path, ("asha-control", "events"))
    except StoreError as exc:
        raise EventError(str(exc)) from exc
    return path, managed_start


def events_dir(config: ControlConfig) -> Path:
    """Return the private event directory, creating its managed path safely."""
    path, managed_start = _events_path(config)
    try:
        with _directory_fd(path, create=True, managed_start=managed_start) as directory_fd:
            if directory_fd is None:
                raise EventError("failed to create Control event directory")
    except StoreError as exc:
        raise EventError(str(exc)) from exc
    return path


def _validated_values(
    *,
    task_id: Any,
    run_id: Any,
    event: Any,
    harness: Any,
    harness_session_id: Any,
    exit_status: Any,
    pane_id: Any,
) -> dict[str, Any]:
    try:
        task_id = canonical_uuid(task_id)
        run_id = canonical_uuid(run_id)
    except ModelError as exc:
        raise EventError(str(exc)) from exc
    if not isinstance(event, str) or event not in EVENTS:
        raise EventError("event must name a supported Control event")
    try:
        harness = validate_harness(harness)
    except HarnessError as exc:
        raise EventError(str(exc)) from exc
    session_id = _text(
        harness_session_id, "harness_session_id", maximum=200, optional=True,
    )
    status = _exit_status(exit_status)
    if event != "session-ended" and status is not None:
        raise EventError("exit_status is valid only for session-ended")
    pane = _text(pane_id, "pane_id", maximum=24, optional=True)
    if pane is not None and _PANE_ID.fullmatch(pane) is None:
        raise EventError("pane_id must use the tmux %N grammar")
    return {
        "contract": EVENT_CONTRACT,
        "task_id": task_id,
        "run_id": run_id,
        "event": event,
        "state": _event_state(event, status),
        "harness": harness,
        "harness_session_id": session_id,
        "exit_status": status,
        "pane_id": pane,
        "observed_at": _observed_at(),
    }


def write_snapshot(
    config: ControlConfig,
    *,
    task_id: str,
    run_id: str,
    event: str,
    harness: str,
    harness_session_id: str | None,
    exit_status: int | None,
    pane_id: str | None,
) -> Path:
    """Atomically replace one run's current snapshot without a task lock."""
    snapshot = _validated_values(
        task_id=task_id,
        run_id=run_id,
        event=event,
        harness=harness,
        harness_session_id=harness_session_id,
        exit_status=exit_status,
        pane_id=pane_id,
    )
    raw = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(raw) > MAX_EVENT_BYTES:
        raise EventError(f"event snapshot exceeds {MAX_EVENT_BYTES} bytes")

    directory, managed_start = _events_path(config)
    name = f"{snapshot['run_id']}.json"
    temporary = f".{name}.tmp.{secrets.token_hex(8)}"
    fd = -1
    try:
        with _directory_fd(
            directory, create=True, managed_start=managed_start,
        ) as directory_fd:
            if directory_fd is None:
                raise EventError("failed to create Control event directory")
            try:
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                    0o600,
                    dir_fd=directory_fd,
                )
                os.fchmod(fd, 0o600)
                offset = 0
                while offset < len(raw):
                    count = os.write(fd, raw[offset:])
                    if count <= 0:
                        raise EventError("short write while saving event snapshot")
                    offset += count
                os.fsync(fd)
                os.close(fd)
                fd = -1
                os.replace(
                    temporary, name,
                    src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                )
                os.fsync(directory_fd)
            finally:
                _close_quietly(fd)
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
    except EventError:
        raise
    except (StoreError, OSError) as exc:
        raise EventError(f"event snapshot write failed: {exc}") from exc
    return directory / name


def _validate_snapshot(value: Any, run_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _SNAPSHOT_KEYS:
        raise EventError("event snapshot has an invalid key set")
    if value["contract"] != EVENT_CONTRACT:
        raise EventError(f"event snapshot contract must be {EVENT_CONTRACT}")
    try:
        canonical_uuid(value["task_id"])
        snapshot_run_id = canonical_uuid(value["run_id"])
    except ModelError as exc:
        raise EventError(str(exc)) from exc
    if snapshot_run_id != run_id:
        raise EventError("event snapshot run ID does not match its filename")
    event = value["event"]
    if not isinstance(event, str) or event not in EVENTS:
        raise EventError("event snapshot names an unsupported event")
    try:
        validate_harness(value["harness"])
    except HarnessError as exc:
        raise EventError(str(exc)) from exc
    _text(value["harness_session_id"], "harness_session_id", maximum=200, optional=True)
    status = _exit_status(value["exit_status"])
    if event != "session-ended" and status is not None:
        raise EventError("event snapshot carries exit status outside session-ended")
    pane = _text(value["pane_id"], "pane_id", maximum=24, optional=True)
    if pane is not None and _PANE_ID.fullmatch(pane) is None:
        raise EventError("event snapshot pane_id is invalid")
    if value["state"] != _event_state(event, status):
        raise EventError("event snapshot state disagrees with its event")
    observed = _text(value["observed_at"], "observed_at", maximum=40)
    assert observed is not None
    if _TIMESTAMP.fullmatch(observed) is None:
        raise EventError("event snapshot observed_at is not bounded RFC3339 UTC")
    try:
        parsed = datetime.fromisoformat(observed[:-1] + "+00:00")
    except ValueError as exc:
        raise EventError("event snapshot observed_at is invalid") from exc
    if parsed.tzinfo != timezone.utc:
        raise EventError("event snapshot observed_at is not UTC")
    return value


def read_snapshot(config: ControlConfig, run_id: str) -> dict[str, Any] | None:
    """Read and strictly validate one current event snapshot."""
    try:
        run_id = canonical_uuid(run_id)
    except ModelError as exc:
        raise EventError(str(exc)) from exc
    directory, managed_start = _events_path(config)
    name = f"{run_id}.json"
    try:
        with _directory_fd(
            directory, create=False, managed_start=managed_start,
        ) as directory_fd:
            if directory_fd is None:
                return None
            try:
                fd = _open_existing_file(directory_fd, name, "event snapshot")
            except FileNotFoundError:
                return None
            try:
                metadata = os.fstat(fd)
                if metadata.st_size > MAX_EVENT_BYTES:
                    raise EventError(f"event snapshot exceeds {MAX_EVENT_BYTES} bytes")
                chunks: list[bytes] = []
                remaining = MAX_EVENT_BYTES + 1
                while remaining:
                    chunk = os.read(fd, min(4096, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
            finally:
                _close_quietly(fd)
    except EventError:
        raise
    except (StoreError, OSError) as exc:
        raise EventError(f"event snapshot read failed: {exc}") from exc
    if len(raw) > MAX_EVENT_BYTES:
        raise EventError(f"event snapshot exceeds {MAX_EVENT_BYTES} bytes")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except _DuplicateJsonKey as exc:
        raise EventError("duplicate JSON key in event snapshot") from exc
    except RecursionError as exc:
        raise EventError("event snapshot nesting exceeds supported limit") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EventError("event snapshot is not strict UTF-8 JSON") from exc
    return _validate_snapshot(value, run_id)


def expire_snapshot(config: ControlConfig, run_id: str) -> bool:
    """Durably remove one run snapshot; return whether a file was removed."""
    try:
        run_id = canonical_uuid(run_id)
    except ModelError as exc:
        raise EventError(str(exc)) from exc
    directory, managed_start = _events_path(config)
    name = f"{run_id}.json"
    try:
        with _directory_fd(
            directory, create=False, managed_start=managed_start,
        ) as directory_fd:
            if directory_fd is None:
                return False
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                return False
            os.fsync(directory_fd)
    except (StoreError, OSError) as exc:
        raise EventError(f"event snapshot expiry failed: {exc}") from exc
    return True


def expire_terminal_snapshots(
    config: ControlConfig, runs: list[dict[str, Any]],
) -> bool:
    """Best-effort expiry; return whether any unblocked terminal run was seen."""
    terminal = False
    for run in runs:
        if run.get("state") not in {"exited", "failed"} or run.get("blocker") is not None:
            continue
        terminal = True
        try:
            expire_snapshot(config, run.get("run_id"))
        except EventError:
            # Runtime presentation cleanup must not obscure a persisted
            # terminal record or the reconciliation result being reported.
            continue
    return terminal


MAX_SUMMARY_SNAPSHOTS = 256
_SUMMARY_ORDER = ("needs-input", "working", "idle", "exited", "failed")
_SUMMARY_LABELS = {"needs-input": "needs-you"}


def summarize(config: ControlConfig) -> str:
    """Aggregate last-event-only run snapshots into one bounded status string.

    Reads only the runtime snapshot directory, never the task registry, so the
    hook path takes no lock and contends with no controller transaction. Bounded
    by MAX_SUMMARY_SNAPSHOTS; a malformed or unreadable snapshot is skipped
    rather than raising, because this feeds a status line and must never be the
    reason a hook fails.
    """
    counts: dict[str, int] = {}
    total = 0
    try:
        directory = events_dir(config)
        names = sorted(
            entry.name for entry in directory.iterdir()
            if entry.is_file() and entry.name.endswith(".json")
        )
    except (EventError, OSError):
        return "asha last-event-only: snapshot status unavailable"
    inspected = names[:MAX_SUMMARY_SNAPSHOTS]
    omitted = len(names) - len(inspected)
    for name in inspected:
        try:
            snapshot = read_snapshot(config, name[: -len(".json")])
        except (EventError, OSError, ValueError):
            continue
        if snapshot is None:
            continue
        total += 1
        state = snapshot.get("state")
        if isinstance(state, str):
            counts[state] = counts.get(state, 0) + 1
    suffix = ""
    if omitted:
        noun = "snapshot" if omitted == 1 else "snapshots"
        suffix = f" (truncated: {omitted} {noun} omitted)"
    if not total:
        detail = "no snapshots" if not names else "no valid inspected snapshots"
        return f"asha last-event-only: {detail}{suffix}"
    parts = [
        f"{counts[state]} {_SUMMARY_LABELS.get(state, state)}"
        for state in _SUMMARY_ORDER
        if counts.get(state)
    ]
    parts.append(f"{total} total")
    return "asha last-event-only: " + ", ".join(parts) + suffix


def publish_server_summary(config: ControlConfig, adapter: TmuxAdapter) -> None:
    """Best-effort refresh of the cached tmux server presentation value."""
    try:
        adapter.set_server_summary(summarize(config), deadline_seconds=5)
    except (EventError, OSError, ValueError):
        return
