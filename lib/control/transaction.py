"""Durable, per-task-locked creation journals for Control preparation."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import ControlConfig, is_canonical_absolute_path
from .model import canonical_uuid
from .store import (
    StoreError,
    _close_quietly,
    _directory_fd,
    _managed_start,
    _open_existing_file,
    _task_lock,
)


JOURNAL_CONTRACT = "asha.control-creation-journal.v1"
MAX_JOURNAL_BYTES = 256 * 1024
PHASES = frozenset({
    "intent", "task-recorded", "parent-intent", "parent-ready",
    "workspace-add-intent", "workspace-added", "workspace-recorded",
    "context-intent", "context-provisioning", "context-provisioned", "ready-for-launch",
    "task-identity-intent", "task-identity-recorded",
    "rollback-intent", "workspace-forgotten", "removing", "rolled-back", "preserved",
    "tmux-intent", "tmux-session-created", "launch-attempted", "run-recorded",
})
PHASE_TRANSITIONS = {
    "intent": frozenset({"task-recorded", "rollback-intent", "preserved"}),
    "task-recorded": frozenset({"parent-intent", "rollback-intent", "preserved"}),
    "parent-intent": frozenset({"parent-ready", "rollback-intent", "preserved"}),
    "parent-ready": frozenset({"workspace-add-intent", "rollback-intent", "preserved"}),
    "workspace-add-intent": frozenset({"workspace-added", "rollback-intent", "preserved"}),
    "workspace-added": frozenset({"workspace-recorded", "rollback-intent", "preserved"}),
    "workspace-recorded": frozenset({"context-intent", "rollback-intent", "preserved"}),
    "context-intent": frozenset({"context-provisioning", "rollback-intent", "preserved"}),
    "context-provisioning": frozenset({"context-provisioned", "rollback-intent", "preserved"}),
    "context-provisioned": frozenset({"task-identity-intent", "rollback-intent", "preserved"}),
    "task-identity-intent": frozenset({"task-identity-recorded", "rollback-intent", "preserved"}),
    "task-identity-recorded": frozenset({"ready-for-launch", "rollback-intent", "preserved"}),
    "ready-for-launch": frozenset({"tmux-intent", "rollback-intent", "preserved"}),
    # Creating the session runs only a holder and touches no jj/workspace
    # state.  It remains rollback-safe; only the real process exec is
    # irrevocable and must be preceded by launch-attempted.
    "tmux-intent": frozenset({"tmux-session-created", "rollback-intent", "preserved"}),
    "tmux-session-created": frozenset({"launch-attempted", "rollback-intent", "preserved"}),
    "rollback-intent": frozenset({"workspace-forgotten", "preserved"}),
    "workspace-forgotten": frozenset({"removing", "rolled-back", "preserved"}),
    "removing": frozenset({"rolled-back", "preserved"}),
    "rolled-back": frozenset(),
    "preserved": frozenset(),
    "launch-attempted": frozenset({"run-recorded", "preserved"}),
    "run-recorded": frozenset(),
}
_OP = re.compile(r"[0-9a-f]{128}", re.ASCII)
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_CHANGE = re.compile(r"[k-z]{32}", re.ASCII)
_REPO = re.compile(r"repo:[0-9a-f]{64}", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)
_INVOCATION = re.compile(r"[0-9a-f]{32}", re.ASCII)
_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,63}", re.ASCII)


class JournalError(ValueError):
    pass


class _DuplicateKey(ValueError):
    pass


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _exact(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise JournalError(f"{label} must contain exactly the v1 fields")
    return value


def _path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not is_canonical_absolute_path(value, resolved=True):
        raise JournalError(f"{label} must be an exact canonical absolute path")
    return value


def _validate_manifest(value: Any, label: str, *, optional: bool) -> None:
    if value is None and optional:
        return
    if not isinstance(value, dict) or len(value) > 8192:
        raise JournalError(f"{label} must be a bounded object")
    for relative, facts in value.items():
        if (not isinstance(relative, str) or not relative or relative.startswith("/") or
                ".." in Path(relative).parts or "//" in relative):
            raise JournalError(f"{label} contains an unsafe relative path")
        if not isinstance(facts, dict):
            raise JournalError(f"{label} facts must be objects")


_JJ_PRIVATE_PATHS = frozenset({
    ".jj", ".jj/repo", ".jj/working_copy",
    ".jj/working_copy/checkout", ".jj/working_copy/tree_state",
    ".jj/working_copy/type",
})


def _validate_materialized_ownership(value: Any) -> None:
    if value is None:
        return
    value = _exact(value, {"tracked", "private"}, "materialized ownership")
    tracked = value["tracked"]
    if not isinstance(tracked, list) or len(tracked) > 1024:
        raise JournalError("materialized tracked ownership must be bounded")
    for fact in tracked:
        if (not isinstance(fact, list) or len(fact) != 4 or
                any(not isinstance(item, int) or item < 0 for item in fact)):
            raise JournalError("materialized tracked ownership facts are invalid")
    _validate_manifest(value["private"], "materialized private ownership", optional=False)
    if set(value["private"]) != _JJ_PRIVATE_PATHS:
        raise JournalError("materialized private ownership is not the exact jj binding shape")


def _ownership_fact(value: Any, label: str) -> dict[str, Any]:
    value = _exact(value, {"dev", "ino", "mode", "uid"}, label)
    if any(not isinstance(value[key], int) or value[key] < 0 for key in value):
        raise JournalError(f"{label} inode facts are invalid")
    return value


def validate_journal(value: Any, *, config: ControlConfig | None = None) -> dict[str, Any]:
    journal = _exact(value, {
        "contract", "task_id", "invocation_id", "phase", "launch_attempted",
        "config", "repository", "task", "workspace", "jj",
        "expected_materialization", "materialized_owned", "recovery_owned", "planned_context",
        "context_owned", "removal",
    }, "creation journal")
    if journal["contract"] != JOURNAL_CONTRACT:
        raise JournalError(f"journal contract must be {JOURNAL_CONTRACT}")
    try:
        canonical_uuid(journal["task_id"])
    except ValueError as exc:
        raise JournalError(str(exc)) from exc
    if (not isinstance(journal["invocation_id"], str) or
            _INVOCATION.fullmatch(journal["invocation_id"]) is None):
        raise JournalError("journal invocation ID is invalid")
    if journal["phase"] not in PHASES:
        raise JournalError("journal phase is invalid")
    if not isinstance(journal["launch_attempted"], bool):
        raise JournalError("journal launch_attempted must be boolean")
    if journal["phase"] in {"launch-attempted", "run-recorded"} and not journal["launch_attempted"]:
        raise JournalError(f"{journal['phase']} phase requires launch_attempted=true")
    bound_config = _exact(
        journal["config"], {"workspace_root", "tasks_dir", "runtime_dir"},
        "journal config",
    )
    for key, label in (
        ("workspace_root", "journal workspace root"),
        ("tasks_dir", "journal tasks directory"),
        ("runtime_dir", "journal runtime directory"),
    ):
        _path(bound_config[key], label)
    repository = _exact(
        journal["repository"], {"root", "identity", "git_root", "repo_key"},
        "journal repository",
    )
    _path(repository["root"], "journal repository root")
    _path(repository["git_root"], "journal Git root")
    if not isinstance(repository["identity"], str) or _REPO.fullmatch(repository["identity"]) is None:
        raise JournalError("journal repository identity is invalid")
    if not isinstance(repository["repo_key"], str) or _KEY.fullmatch(repository["repo_key"]) is None:
        raise JournalError("journal repository key is invalid")
    task = _exact(
        journal["task"], {"record_path", "slug", "label", "digest", "failure"},
        "journal task",
    )
    _path(task["record_path"], "journal task record path")
    if (not isinstance(task["slug"], str) or
            re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", task["slug"]) is None):
        raise JournalError("journal task slug is invalid")
    if not isinstance(task["label"], str) or not 1 <= len(task["label"]) <= 200:
        raise JournalError("journal task label is invalid")
    if task["digest"] is not None and (
            not isinstance(task["digest"], str) or _DIGEST.fullmatch(task["digest"]) is None):
        raise JournalError("journal task digest is invalid")
    if task["failure"] is not None:
        failure = _exact(task["failure"], {"digest", "updated_at"}, "journal task failure")
        if not isinstance(failure["digest"], str) or _DIGEST.fullmatch(failure["digest"]) is None:
            raise JournalError("journal task failure digest is invalid")
        try:
            if (not isinstance(failure["updated_at"], str) or
                    not failure["updated_at"].endswith("Z")):
                raise ValueError
            datetime.fromisoformat(failure["updated_at"][:-1] + "+00:00")
        except ValueError as exc:
            raise JournalError("journal task failure timestamp is invalid") from exc
    workspace = _exact(
        journal["workspace"], {"path", "name", "root_fact", "created_parents"},
        "journal workspace",
    )
    _path(workspace["path"], "journal workspace path")
    if (not isinstance(workspace["name"], str) or
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", workspace["name"]) is None):
        raise JournalError("journal workspace name is invalid")
    if not isinstance(workspace["created_parents"], list) or len(workspace["created_parents"]) > 8:
        raise JournalError("journal created_parents must be bounded")
    for item in workspace["created_parents"]:
        entry = _exact(
            item, {"path", "parent_path", "dev", "ino", "parent_dev", "parent_ino", "mode", "uid"},
            "created parent",
        )
        _path(entry["path"], "created parent path")
        _path(entry["parent_path"], "created parent parent path")
        if any(not isinstance(entry[key], int) or entry[key] < 0 for key in (
            "dev", "ino", "parent_dev", "parent_ino", "mode", "uid"
        )):
            raise JournalError("created parent ownership facts are invalid")
    if workspace["root_fact"] is not None:
        _ownership_fact(workspace["root_fact"], "workspace root fact")
    jj = _exact(journal["jj"], {
        "pinned_operation_id", "base_commit_id", "change_id",
        "working_commit_id", "description", "registration_state", "last_registration",
    }, "journal jj")
    if not isinstance(jj["pinned_operation_id"], str) or _OP.fullmatch(jj["pinned_operation_id"]) is None:
        raise JournalError("journal pinned operation ID is invalid")
    if not isinstance(jj["base_commit_id"], str) or _COMMIT.fullmatch(jj["base_commit_id"]) is None:
        raise JournalError("journal base commit ID is invalid")
    if jj["change_id"] is not None and (
            not isinstance(jj["change_id"], str) or _CHANGE.fullmatch(jj["change_id"]) is None):
        raise JournalError("journal change ID is invalid")
    if jj["working_commit_id"] is not None and (
            not isinstance(jj["working_commit_id"], str) or _COMMIT.fullmatch(jj["working_commit_id"]) is None):
        raise JournalError("journal working commit ID is invalid")
    if not isinstance(jj["description"], str) or len(jj["description"]) > 200:
        raise JournalError("journal description is invalid")
    if jj["registration_state"] not in {
        "absent", "add-intent", "present", "forget-intent", "absent-after-forget", "unknown",
    }:
        raise JournalError("journal registration state is invalid")
    if jj["last_registration"] is not None:
        registration = _exact(
            jj["last_registration"], {"change_id", "working_commit_id"},
            "last registration",
        )
        if (_CHANGE.fullmatch(registration["change_id"]) is None or
                _COMMIT.fullmatch(registration["working_commit_id"]) is None):
            raise JournalError("last registration identity is invalid")
    _validate_manifest(journal["expected_materialization"], "expected materialization", optional=False)
    _validate_materialized_ownership(journal["materialized_owned"])
    _validate_manifest(journal["recovery_owned"], "recovery ownership", optional=True)
    if journal["materialized_owned"] is not None and (
            len(journal["materialized_owned"]["tracked"]) !=
            len(journal["expected_materialization"])):
        raise JournalError("materialized tracked ownership count differs from expected materialization")
    if (journal["recovery_owned"] is not None and
            set(journal["recovery_owned"]) != _JJ_PRIVATE_PATHS):
        raise JournalError("recovery ownership is not the exact jj binding shape")
    _validate_manifest(journal["planned_context"], "journal planned_context", optional=True)
    _validate_manifest(journal["context_owned"], "journal context ownership", optional=False)
    removal = _exact(
        journal["removal"], {"entries_removed", "root_removed", "parents_removed"},
        "journal removal",
    )
    if (not isinstance(removal["entries_removed"], int) or
            not 0 <= removal["entries_removed"] <= 2048):
        raise JournalError("journal removal entries_removed is invalid")
    if (not isinstance(removal["parents_removed"], int) or
            not 0 <= removal["parents_removed"] <= 8):
        raise JournalError("journal removal parents_removed is invalid")
    if not isinstance(removal["root_removed"], bool):
        raise JournalError("journal removal root_removed is invalid")
    if config is not None:
        expected_config = {
            "workspace_root": str(config.workspace_root),
            "tasks_dir": str(config.tasks_dir),
            "runtime_dir": str(config.runtime_dir),
        }
        if bound_config != expected_config:
            raise JournalError("creation journal is not bound to the current Control config")
        destination = Path(workspace["path"])
        expected_destination = config.workspace_root / repository["repo_key"] / task["slug"]
        if destination != expected_destination or destination == config.workspace_root:
            raise JournalError("journal workspace path is not the exact configured task destination")
        source = Path(repository["root"])
        root = config.workspace_root
        if source == root or source.is_relative_to(root) or root.is_relative_to(source):
            raise JournalError("journal repository and workspace root overlap")
        if Path(task["record_path"]) != config.tasks_dir / f"{journal['task_id']}.json":
            raise JournalError("journal task record path is not bound to its task ID")
        previous_path: Path | None = None
        for entry in workspace["created_parents"]:
            path = Path(entry["path"])
            parent = Path(entry["parent_path"])
            if path.parent != parent or not destination.parent.is_relative_to(path):
                raise JournalError("created parent is outside the task destination ancestry")
            if not (root.is_relative_to(path) or path.is_relative_to(root)):
                raise JournalError("created parent is outside the configured workspace ancestry")
            if previous_path is not None and parent != previous_path:
                raise JournalError("created parents are not recorded in parent/child order")
            previous_path = path
    return journal


class CreationJournalStore:
    def __init__(self, config: ControlConfig):
        self.config = config
        self.transactions_dir = config.tasks_dir.parent / "transactions"
        self.locks_dir = config.runtime_dir / "tasks"
        self._transactions_start = _managed_start(self.transactions_dir, ("control", "transactions"))
        self._locks_start = _managed_start(self.locks_dir, ("asha-control", "tasks"))

    def path(self, task_id: str) -> Path:
        try:
            canonical_uuid(task_id)
        except ValueError as exc:
            raise JournalError(str(exc)) from exc
        return self.transactions_dir / f"{task_id}.json"

    @contextmanager
    def _locked_directories(self, *, create: bool) -> Iterator[tuple[int | None, int | None]]:
        try:
            with _directory_fd(
                self.transactions_dir, create=create, managed_start=self._transactions_start
            ) as transactions_fd:
                if transactions_fd is None:
                    yield None, None
                    return
                with _directory_fd(
                    self.locks_dir, create=True, managed_start=self._locks_start
                ) as locks_fd:
                    yield transactions_fd, locks_fd
        except StoreError as exc:
            raise JournalError(str(exc)) from exc

    def _read_fd(self, directory_fd: int, task_id: str) -> dict[str, Any]:
        name = f"{task_id}.json"
        try:
            fd = _open_existing_file(directory_fd, name, "creation journal")
        except FileNotFoundError as exc:
            raise JournalError(f"creation journal not found: {task_id}") from exc
        except StoreError as exc:
            raise JournalError(str(exc)) from exc
        try:
            metadata = os.fstat(fd)
            if metadata.st_size > MAX_JOURNAL_BYTES:
                raise JournalError(f"creation journal exceeds {MAX_JOURNAL_BYTES} bytes")
            chunks: list[bytes] = []
            remaining = MAX_JOURNAL_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            _close_quietly(fd)
        if len(raw) > MAX_JOURNAL_BYTES:
            raise JournalError(f"creation journal exceeds {MAX_JOURNAL_BYTES} bytes")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
            journal = validate_journal(value, config=self.config)
        except JournalError:
            raise
        except (_DuplicateKey, UnicodeError, json.JSONDecodeError) as exc:
            raise JournalError("creation journal is not strict UTF-8 JSON") from exc
        if journal["task_id"] != task_id:
            raise JournalError("creation journal task ID does not match filename")
        return journal

    def read(self, task_id: str) -> dict[str, Any]:
        self.path(task_id)
        with self._locked_directories(create=False) as (transactions_fd, locks_fd):
            if transactions_fd is None or locks_fd is None:
                raise JournalError(f"creation journal not found: {task_id}")
            try:
                with _task_lock(locks_fd, task_id):
                    return self._read_fd(transactions_fd, task_id)
            except StoreError as exc:
                raise JournalError(str(exc)) from exc

    def save(self, value: dict[str, Any], *, expected_phase: str | None = None) -> Path:
        journal = copy.deepcopy(value)
        try:
            validate_journal(journal, config=self.config)
        except (JournalError, ValueError) as exc:
            raise JournalError(str(exc)) from exc
        raw = json.dumps(journal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > MAX_JOURNAL_BYTES:
            raise JournalError(f"creation journal exceeds {MAX_JOURNAL_BYTES} bytes")
        task_id = journal["task_id"]
        name = f"{task_id}.json"
        with self._locked_directories(create=True) as (transactions_fd, locks_fd):
            assert transactions_fd is not None and locks_fd is not None
            try:
                with _task_lock(locks_fd, task_id):
                    current = None
                    try:
                        current = self._read_fd(transactions_fd, task_id)
                    except JournalError as exc:
                        if "not found" not in str(exc):
                            raise
                    if current is None:
                        if expected_phase is not None:
                            raise JournalError("expected phase supplied for new creation journal")
                        if journal["phase"] != "intent":
                            raise JournalError("new creation journal must begin in intent phase")
                    else:
                        if expected_phase is None or current["phase"] != expected_phase:
                            raise JournalError("creation journal phase changed; reload before update")
                        if (journal["phase"] != current["phase"] and
                                journal["phase"] not in PHASE_TRANSITIONS[current["phase"]]):
                            raise JournalError(
                                f"illegal creation journal phase transition: "
                                f"{current['phase']} -> {journal['phase']}"
                            )
                        for key in (
                            "contract", "task_id", "invocation_id", "config", "repository",
                            "expected_materialization",
                        ):
                            if journal[key] != current[key]:
                                raise JournalError(f"immutable creation journal field changed: {key}")
                        for key in ("path", "name"):
                            if journal["workspace"][key] != current["workspace"][key]:
                                raise JournalError(f"immutable creation journal workspace field changed: {key}")
                        for key in ("record_path", "slug", "label"):
                            if journal["task"][key] != current["task"][key]:
                                raise JournalError(f"immutable creation journal task field changed: {key}")
                        if (current["task"]["failure"] is not None and
                                journal["task"]["failure"] != current["task"]["failure"]):
                            raise JournalError("planned task failure facts cannot change")
                        old_parents = current["workspace"]["created_parents"]
                        new_parents = journal["workspace"]["created_parents"]
                        if new_parents[:len(old_parents)] != old_parents:
                            raise JournalError("created parent ownership facts cannot change")
                        if current["workspace"]["root_fact"] is not None and (
                                journal["workspace"]["root_fact"] != current["workspace"]["root_fact"]):
                            raise JournalError("workspace root ownership fact cannot change")
                        for key in (
                            "pinned_operation_id", "base_commit_id", "description",
                        ):
                            if journal["jj"][key] != current["jj"][key]:
                                raise JournalError(f"immutable creation journal jj field changed: {key}")
                        for key in ("change_id", "working_commit_id", "last_registration"):
                            if current["jj"][key] is not None and journal["jj"][key] != current["jj"][key]:
                                raise JournalError(f"jj ownership identity cannot change: {key}")
                        registration_transitions = {
                            "absent": {"absent", "add-intent"},
                            "add-intent": {"add-intent", "present", "absent", "unknown"},
                            "present": {"present", "forget-intent"},
                            "forget-intent": {"forget-intent", "absent-after-forget", "present", "unknown"},
                            "absent-after-forget": {"absent-after-forget"},
                            "unknown": {"unknown", "present", "absent", "absent-after-forget"},
                        }
                        if journal["jj"]["registration_state"] not in registration_transitions[
                            current["jj"]["registration_state"]
                        ]:
                            raise JournalError("jj registration state cannot move backward")
                        for key in ("materialized_owned", "recovery_owned", "planned_context"):
                            if current[key] is not None and journal[key] != current[key]:
                                raise JournalError(f"journal ownership facts cannot change: {key}")
                        old_context = current["context_owned"]
                        new_context = journal["context_owned"]
                        if any(new_context.get(key) != fact for key, fact in old_context.items()):
                            raise JournalError("context ownership facts cannot change")
                        if current["task"]["digest"] is not None and journal["task"]["digest"] is None:
                            raise JournalError("task record digest cannot be cleared")
                        old_removed = current["removal"]
                        new_removed = journal["removal"]
                        for key in ("entries_removed", "parents_removed"):
                            if new_removed[key] < old_removed[key]:
                                raise JournalError("removal progress cannot move backward")
                        if old_removed["root_removed"] and not new_removed["root_removed"]:
                            raise JournalError("workspace root removal cannot move backward")
                        if current["launch_attempted"] and not journal["launch_attempted"]:
                            raise JournalError("launch_attempted cannot be cleared")
                    temporary = f".{name}.tmp.{secrets.token_hex(8)}"
                    fd = -1
                    try:
                        fd = os.open(
                            temporary,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                            0o600,
                            dir_fd=transactions_fd,
                        )
                        os.fchmod(fd, 0o600)
                        offset = 0
                        while offset < len(raw):
                            count = os.write(fd, raw[offset:])
                            if count <= 0:
                                raise JournalError("short write while saving creation journal")
                            offset += count
                        os.fsync(fd)
                        os.close(fd)
                        fd = -1
                        os.replace(temporary, name, src_dir_fd=transactions_fd, dst_dir_fd=transactions_fd)
                        os.fsync(transactions_fd)
                    finally:
                        _close_quietly(fd)
                        try:
                            os.unlink(temporary, dir_fd=transactions_fd)
                        except FileNotFoundError:
                            pass
            except StoreError as exc:
                raise JournalError(str(exc)) from exc
            except OSError as exc:
                raise JournalError(f"creation journal write failed: {exc}") from exc
        return self.path(task_id)
