"""Descriptor-relative per-task storage with locks and atomic replacement."""

from __future__ import annotations

import errno
import copy
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import contextvars
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import (
    ConfigError,
    ControlConfig,
    reject_symlink_components,
    reject_unsafe_writable_ancestors,
    namespace_safety_step,
    is_canonical_absolute_path,
    require_existing_directory_components,
    validate_workspace_root,
)
from .model import (
    ModelError,
    canonical_uuid,
    require_run_transition,
    require_task_transition,
    validate_slug,
    validate_task,
)


MAX_RECORD_BYTES = 256 * 1024
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_HELD_TASK_LOCKS: contextvars.ContextVar[frozenset[tuple[int, int, str]]] = contextvars.ContextVar(
    "asha_control_held_task_locks", default=frozenset()
)
_HELD_REGISTRY_LOCKS: contextvars.ContextVar[frozenset[tuple[int, int]]] = contextvars.ContextVar(
    "asha_control_held_registry_locks", default=frozenset()
)


class StoreError(ValueError):
    """A registry read or write could not be completed safely."""


class StoreCommittedError(StoreError):
    """The replacement is visible, but its directory entry may not be durable."""


class _DuplicateJsonKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJsonKey
        value[key] = item
    return value


def _canonical_task_bytes(task: dict[str, Any]) -> bytes:
    validated = validate_task(task)
    return json.dumps(
        validated, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def task_digest(task: dict[str, Any]) -> str:
    """Return the deterministic digest callers bind to an existing snapshot."""
    return hashlib.sha256(_canonical_task_bytes(task)).hexdigest()


def _parts(path: Path) -> tuple[str, ...]:
    if not is_canonical_absolute_path(str(path)):
        raise StoreError(f"Control path must be absolute and canonical: {path}")
    return path.parts[1:]


def _managed_start(path: Path, suffix: tuple[str, ...]) -> int:
    parts = _parts(path)
    if len(parts) < len(suffix) or parts[-len(suffix):] != suffix:
        raise StoreError(f"Control path does not use expected managed layout: {path}")
    return len(parts) - len(suffix)


def _directory_error(path: Path, exc: OSError) -> StoreError:
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return StoreError(f"symlink or non-directory component rejected in Control path: {path}")
    return StoreError(f"cannot open Control directory {path}: {exc}")


def _close_quietly(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


@contextmanager
def _directory_fd(
    path: Path,
    *,
    create: bool,
    managed_start: int,
) -> Iterator[int | None]:
    """Traverse from / with one pinned no-follow directory FD per component."""
    parts = _parts(path)
    try:
        fd = os.open("/", _DIRECTORY_FLAGS)
    except OSError as exc:
        raise StoreError(f"cannot open filesystem root for Control traversal: {exc}") from exc
    current = Path("/")
    missing = False
    private_boundary = False
    try:
        for index, part in enumerate(parts):
            current /= part
            created = False
            child = -1
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    missing = True
                    break
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                    created = True
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
                except FileExistsError:
                    # Another creator won.  Treat the inode as untrusted and
                    # reopen without following links; existing-mode rules
                    # below decide whether it is safe.  Never chmod it.
                    created = False
                    try:
                        child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
                    except OSError as exc:
                        raise _directory_error(current, exc) from exc
                except OSError as exc:
                    raise _directory_error(current, exc) from exc
            except OSError as exc:
                raise _directory_error(current, exc) from exc
            try:
                metadata = os.fstat(child)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise StoreError(f"Control path component is not a directory: {current}")
                problem, private_boundary = namespace_safety_step(
                    metadata, os.geteuid(), private_boundary
                )
                if problem:
                    raise StoreError(f"{problem} rejected in Control path: {current}")
                if index >= managed_start:
                    if metadata.st_uid != os.geteuid():
                        raise StoreError(
                            f"managed Control directory is not owned by the effective user: {current}"
                        )
                    if created:
                        os.fchmod(child, 0o700)
                        metadata = os.fstat(child)
                    if stat.S_IMODE(metadata.st_mode) != 0o700:
                        raise StoreError(f"managed Control directory must have mode 0700: {current}")
                if create:
                    # Every visible pair may be residue from an interrupted
                    # earlier create-enabled traversal.  Re-syncing existing
                    # directories is not a semantic mutation; it lets a retry
                    # establish durability rather than trusting the failed
                    # creator or a concurrent EEXIST winner.
                    os.fsync(child)
                    os.fsync(fd)
            except StoreError:
                _close_quietly(child)
                raise
            except OSError as exc:
                _close_quietly(child)
                raise StoreError(
                    f"cannot establish Control directory durability {current}: {exc}"
                ) from exc
            parent = fd
            fd = child
            _close_quietly(parent)
        if missing:
            yield None
        else:
            yield fd
    finally:
        _close_quietly(fd)


def _validate_open_file(fd: int, label: str, *, required_mode: int = 0o600) -> os.stat_result:
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise StoreError(f"{label} is not a regular file")
    if metadata.st_uid != os.geteuid():
        raise StoreError(f"{label} is not owned by the effective user")
    if metadata.st_nlink != 1:
        raise StoreError(f"{label} link count must be exactly 1")
    if stat.S_IMODE(metadata.st_mode) != required_mode:
        raise StoreError(f"{label} must have mode {required_mode:04o}")
    return metadata


def _open_existing_file(directory_fd: int, name: str, label: str) -> int:
    try:
        fd = os.open(
            name, os.O_RDONLY | _NONBLOCK | _NOFOLLOW | _CLOEXEC, dir_fd=directory_fd
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise StoreError(f"symlinked {label} rejected: {name}") from exc
        raise StoreError(f"cannot open {label} {name}: {exc}") from exc
    try:
        _validate_open_file(fd, label)
    except OSError as exc:
        _close_quietly(fd)
        raise StoreError(f"cannot inspect {label}: {exc}") from exc
    except Exception:
        _close_quietly(fd)
        raise
    return fd


@contextmanager
def _task_lock(locks_fd: int, task_id: str, before_flock=None) -> Iterator[None]:
    directory_metadata = os.fstat(locks_fd)
    lock_key = (directory_metadata.st_dev, directory_metadata.st_ino, task_id)
    if lock_key in _HELD_TASK_LOCKS.get():
        yield
        return
    name = f"{task_id}.lock"
    created = False
    flags = os.O_RDWR | _NONBLOCK | _NOFOLLOW | _CLOEXEC
    try:
        fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=locks_fd)
        created = True
    except FileExistsError:
        try:
            fd = os.open(name, flags, dir_fd=locks_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise StoreError(f"symlinked task lock rejected: {name}") from exc
            raise StoreError(f"cannot open task lock {name}: {exc}") from exc
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise StoreError(f"symlinked task lock rejected: {name}") from exc
        raise StoreError(f"cannot create task lock {name}: {exc}") from exc
    locked = False
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise StoreError("task lock is not a regular file")
        if metadata.st_uid != os.geteuid():
            raise StoreError("task lock is not owned by the effective user")
        if metadata.st_nlink != 1:
            raise StoreError("task lock link count must be exactly 1")
        if created:
            os.fchmod(fd, 0o600)
        elif stat.S_IMODE(metadata.st_mode) != 0o600:
            raise StoreError("task lock must have mode 0600")
        if before_flock is not None:
            before_flock()
        fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        token = _HELD_TASK_LOCKS.set(_HELD_TASK_LOCKS.get() | {lock_key})
        try:
            yield
        finally:
            _HELD_TASK_LOCKS.reset(token)
    except StoreError:
        raise
    except OSError as exc:
        raise StoreError(f"task lock operation failed: {exc}") from exc
    finally:
        if locked:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        _close_quietly(fd)


@contextmanager
def _registry_lock(tasks_fd: int, before_flock=None, after_flock=None) -> Iterator[None]:
    """Lock the durable registry inode before any runtime-scoped task lock."""
    metadata = os.fstat(tasks_fd)
    key = (metadata.st_dev, metadata.st_ino)
    if key in _HELD_REGISTRY_LOCKS.get():
        yield
        return
    locked = False
    try:
        if before_flock is not None:
            before_flock()
        fcntl.flock(tasks_fd, fcntl.LOCK_EX)
        locked = True
        if after_flock is not None:
            after_flock()
        token = _HELD_REGISTRY_LOCKS.set(_HELD_REGISTRY_LOCKS.get() | {key})
        try:
            yield
        finally:
            _HELD_REGISTRY_LOCKS.reset(token)
    except StoreError:
        raise
    except OSError as exc:
        raise StoreError(f"state registry lock operation failed: {exc}") from exc
    finally:
        if locked:
            try:
                fcntl.flock(tasks_fd, fcntl.LOCK_UN)
            except OSError:
                pass


class TaskStore:
    def __init__(
        self,
        config: ControlConfig,
        *,
        lock_wait_hook=None,
        registry_lock_wait_hook=None,
        registry_lock_acquired_hook=None,
    ):
        self.config = config
        self._lock_wait_hook = lock_wait_hook
        self._registry_lock_wait_hook = registry_lock_wait_hook
        self._registry_lock_acquired_hook = registry_lock_acquired_hook
        self._tasks_managed_start = _managed_start(
            config.tasks_dir, ("control", "tasks")
        )
        self._locks_dir = config.runtime_dir / "tasks"
        self._locks_managed_start = _managed_start(
            self._locks_dir, ("asha-control", "tasks")
        )

    def _validate_record_paths(self, task: dict[str, Any]) -> None:
        repository = Path(task["repository"]["root"])
        workspace = Path(task["jj"]["workspace_path"])
        root = self.config.workspace_root
        try:
            validate_workspace_root(root, home=self.config.home, repository=repository)
            reject_symlink_components(repository, "task repository root")
            reject_symlink_components(workspace, "jj workspace_path")
            require_existing_directory_components(repository, "task repository root")
            require_existing_directory_components(workspace, "jj workspace_path")
            reject_unsafe_writable_ancestors(repository, "task repository root")
            reject_unsafe_writable_ancestors(workspace, "jj workspace_path")
        except ConfigError as exc:
            raise StoreError(str(exc)) from exc
        if workspace == root or not workspace.is_relative_to(root):
            raise StoreError("jj workspace_path must be a strict descendant of control.workspace_root")

    @contextmanager
    def _directories(self, *, create_state: bool) -> Iterator[tuple[int | None, int | None]]:
        with _directory_fd(
            self.config.tasks_dir,
            create=create_state,
            managed_start=self._tasks_managed_start,
        ) as tasks_fd:
            if tasks_fd is None:
                yield None, None
                return
            with _directory_fd(
                self._locks_dir,
                create=True,
                managed_start=self._locks_managed_start,
            ) as locks_fd:
                if locks_fd is None:  # create=True makes this unreachable
                    raise StoreError("failed to create Control lock directory")
                yield tasks_fd, locks_fd

    @contextmanager
    def _locked(self, task_id: str, locks_fd: int | None = None) -> Iterator[None]:
        """Hold the production per-task lock; the no-FD form supports probes/tests."""
        canonical_uuid(task_id)
        if locks_fd is not None:
            with _task_lock(locks_fd, task_id, self._lock_wait_hook):
                yield
            return
        with _directory_fd(
            self._locks_dir, create=True, managed_start=self._locks_managed_start
        ) as opened:
            if opened is None:
                raise StoreError("failed to create Control lock directory")
            with _task_lock(opened, task_id, self._lock_wait_hook):
                yield

    @contextmanager
    def _registry_locked(self, tasks_fd: int) -> Iterator[None]:
        with _registry_lock(
            tasks_fd,
            self._registry_lock_wait_hook,
            self._registry_lock_acquired_hook,
        ):
            yield

    @contextmanager
    def transaction_lock(self, task_id: str) -> Iterator[None]:
        """Hold the durable registry and production task lock across a transaction."""
        canonical_uuid(task_id)
        with self._directories(create_state=True) as (tasks_fd, locks_fd):
            assert tasks_fd is not None and locks_fd is not None
            with self._registry_locked(tasks_fd):
                with self._locked(task_id, locks_fd):
                    yield

    @staticmethod
    def _time(value: str) -> datetime:
        return datetime.fromisoformat(value[:-1] + "+00:00")

    def _validate_update(self, current: dict[str, Any], requested: dict[str, Any]) -> None:
        immutable_task = (
            "contract", "task_id", "slug", "label", "created_at", "repository",
            "source", "tmux",
        )
        for field in immutable_task:
            if requested[field] != current[field]:
                raise StoreError(f"immutable task field changed: {field}")
        for field in ("workspace_name", "workspace_path", "requested_base", "base_commit_id"):
            if requested["jj"][field] != current["jj"][field]:
                raise StoreError(f"immutable jj field changed: {field}")
        if (current["jj"]["change_id"] is not None and
                requested["jj"]["change_id"] != current["jj"]["change_id"]):
            raise StoreError("immutable jj field changed: change_id")
        if requested["lifecycle"] != current["lifecycle"]:
            try:
                require_task_transition(current["lifecycle"], requested["lifecycle"])
            except ModelError as exc:
                raise StoreError(str(exc)) from exc
        if self._time(requested["updated_at"]) < self._time(current["updated_at"]):
            raise StoreError("task updated_at must not move backward")

        requested_runs = {run["run_id"]: run for run in requested["runs"]}
        current_run_ids = [run["run_id"] for run in current["runs"]]
        requested_run_ids = [run["run_id"] for run in requested["runs"]]
        if requested_run_ids[:len(current_run_ids)] != current_run_ids:
            raise StoreError("existing runs must be preserved in their original order")
        new_runs = requested["runs"][len(current_run_ids):]
        # Authorization follows the durable current lifecycle.  A launch can
        # succeed immediately before its controller transaction fails, so a
        # creating or running task may append that preserved starting run in
        # the same update that records lifecycle failure.  Terminal current
        # lifecycles never regain append authority.
        may_append = current["lifecycle"] in {"creating", "running"}
        if new_runs and not may_append:
            raise StoreError("new runs cannot be appended to a terminal task lifecycle")
        for new_run in new_runs:
            if new_run["state"] != "starting":
                raise StoreError("new runs must enter the lifecycle in starting state")
        immutable_run = (
            "contract", "run_id", "harness", "role", "pane_id", "pid",
            "process_start_identity",
        )
        for old_run in current["runs"]:
            new_run = requested_runs.get(old_run["run_id"])
            if new_run is None:
                raise StoreError(f"existing run removed: {old_run['run_id']}")
            for field in immutable_run:
                if new_run[field] != old_run[field]:
                    raise StoreError(f"immutable run field changed: {field}")
            if (old_run["harness_session_id"] is not None and
                    new_run["harness_session_id"] != old_run["harness_session_id"]):
                raise StoreError("immutable run field changed: harness_session_id")
            if new_run["state"] != old_run["state"]:
                try:
                    require_run_transition(old_run["state"], new_run["state"])
                except ModelError as exc:
                    raise StoreError(str(exc)) from exc
            if self._time(new_run["evidence_at"]) < self._time(old_run["evidence_at"]):
                raise StoreError("run evidence_at must not move backward")

    def save(self, task: dict[str, Any], *, expected_digest: str | None = None) -> Path:
        # Caller-owned mappings are mutable.  Pin one authoritative snapshot
        # before any validation, transition comparison, or serialization.
        task = copy.deepcopy(task)
        try:
            validated = validate_task(task)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        self._validate_record_paths(validated)
        task_id = validated["task_id"]
        raw = _canonical_task_bytes(validated) + b"\n"
        if len(raw) > MAX_RECORD_BYTES:
            raise StoreError(f"task record exceeds {MAX_RECORD_BYTES} bytes")
        record_name = f"{task_id}.json"

        with self._directories(create_state=True) as (tasks_fd, locks_fd):
            assert tasks_fd is not None and locks_fd is not None
            with self._registry_locked(tasks_fd):
                with self._locked(task_id, locks_fd):
                    current = self._read_if_exists(tasks_fd, task_id)
                    if current is None:
                        if expected_digest is not None:
                            raise StoreError("expected digest supplied for a new task record")
                    else:
                        if expected_digest is None:
                            raise StoreError("expected digest is required to update an existing task record")
                        if (not isinstance(expected_digest, str) or
                                re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None):
                            raise StoreError("expected digest must be 64 lowercase hexadecimal characters")
                        if not hmac.compare_digest(task_digest(current), expected_digest):
                            raise StoreError("task digest mismatch; reload the current record")
                        self._validate_update(current, validated)
                    temporary_name = f".{record_name}.tmp.{secrets.token_hex(8)}"
                    fd = -1
                    replaced = False
                    try:
                        fd = os.open(
                            temporary_name,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
                            0o600,
                            dir_fd=tasks_fd,
                        )
                        os.fchmod(fd, 0o600)
                        written = 0
                        while written < len(raw):
                            count = os.write(fd, raw[written:])
                            if count <= 0:
                                raise StoreError("short write while saving task record")
                            written += count
                        os.fsync(fd)
                        os.close(fd)
                        fd = -1
                        try:
                            os.replace(
                                temporary_name,
                                record_name,
                                src_dir_fd=tasks_fd,
                                dst_dir_fd=tasks_fd,
                            )
                        except OSError as exc:
                            raise StoreError(
                                f"atomic replace failed for {self.config.tasks_dir / record_name}: {exc}"
                            ) from exc
                        replaced = True
                        try:
                            os.fsync(tasks_fd)
                        except OSError as exc:
                            raise StoreCommittedError(
                                "task record is visible but durability is indeterminate: "
                                f"{exc}"
                            ) from exc
                    except (StoreError, StoreCommittedError):
                        raise
                    except OSError as exc:
                        phase = "after atomic replace" if replaced else "before atomic replace"
                        if replaced:
                            raise StoreCommittedError(
                                "task record is visible but durability is indeterminate: "
                                f"{exc}"
                            ) from exc
                        raise StoreError(f"task save failed {phase}: {exc}") from exc
                    finally:
                        if fd >= 0:
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                        try:
                            os.unlink(temporary_name, dir_fd=tasks_fd)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            # The primary operation already determines success or
                            # failure; cleanup cannot safely alter that outcome.
                            pass
        return self.config.tasks_dir / record_name

    def _read_unlocked(self, tasks_fd: int, task_id: str) -> dict[str, Any]:
        task = self._read_if_exists(tasks_fd, task_id)
        if task is None:
            raise StoreError(f"task not found: {task_id}")
        return task

    def _read_if_exists(self, tasks_fd: int, task_id: str) -> dict[str, Any] | None:
        name = f"{task_id}.json"
        try:
            fd = _open_existing_file(tasks_fd, name, "task record")
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(fd)
            if metadata.st_size > MAX_RECORD_BYTES:
                raise StoreError(f"task record exceeds {MAX_RECORD_BYTES} bytes: {name}")
            chunks: list[bytes] = []
            remaining = MAX_RECORD_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > MAX_RECORD_BYTES:
                raise StoreError(f"task record exceeds {MAX_RECORD_BYTES} bytes: {name}")
        except StoreError:
            raise
        except OSError as exc:
            raise StoreError(f"cannot read task record {name}: {exc}") from exc
        finally:
            _close_quietly(fd)
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        except _DuplicateJsonKey as exc:
            raise StoreError(f"duplicate JSON key in task record {name}") from exc
        except RecursionError as exc:
            raise StoreError(f"task record nesting exceeds supported limit: {name}") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise StoreError(f"invalid JSON in task record {name}: {exc}") from exc
        try:
            task = validate_task(value)
        except ModelError as exc:
            raise StoreError(f"invalid task record {name}: {exc}") from exc
        if task["task_id"] != task_id:
            raise StoreError(f"task ID does not match record filename: {name}")
        self._validate_record_paths(task)
        return task

    def read(self, task_id: str) -> dict[str, Any]:
        try:
            task_id = canonical_uuid(task_id)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        # A negative lookup must not allocate a permanent lock inode.  The
        # pinned pre-read also rejects special files.  We re-read after taking
        # the lock to close replacement races.
        with _directory_fd(
            self.config.tasks_dir,
            create=False,
            managed_start=self._tasks_managed_start,
        ) as tasks_fd:
            if tasks_fd is None or self._read_if_exists(tasks_fd, task_id) is None:
                raise StoreError(f"task not found: {task_id}")
            with _directory_fd(
                self._locks_dir,
                create=True,
                managed_start=self._locks_managed_start,
            ) as locks_fd:
                if locks_fd is None:
                    raise StoreError("failed to create Control lock directory")
                with self._registry_locked(tasks_fd):
                    with self._locked(task_id, locks_fd):
                        return self._read_unlocked(tasks_fd, task_id)

    def list(self) -> list[dict[str, Any]]:
        with self._directories(create_state=False) as (tasks_fd, locks_fd):
            if tasks_fd is None or locks_fd is None:
                return []
            with self._registry_locked(tasks_fd):
                try:
                    names = sorted(os.listdir(tasks_fd))
                except OSError as exc:
                    raise StoreError(f"cannot list task registry: {exc}") from exc
                records: list[dict[str, Any]] = []
                for name in names:
                    if name.startswith(".") or not name.endswith(".json"):
                        continue
                    candidate = name[:-5]
                    try:
                        task_id = canonical_uuid(candidate)
                    except ModelError as exc:
                        raise StoreError("invalid task record filename in registry") from exc
                    with self._locked(task_id, locks_fd):
                        records.append(self._read_unlocked(tasks_fd, task_id))
                return records

    def resolve(self, selector: str) -> dict[str, Any]:
        try:
            task_id = canonical_uuid(selector)
        except ModelError:
            task_id = ""
        if task_id:
            return self.read(task_id)
        try:
            validate_slug(selector)
        except ModelError as exc:
            raise StoreError("task selector must be a canonical UUID or exact slug") from exc
        matches = [record for record in self.list() if record["slug"] == selector]
        if not matches:
            raise StoreError(f"task not found: {selector}")
        if len(matches) != 1:
            raise StoreError(f"task slug is ambiguous: {selector}")
        return matches[0]
