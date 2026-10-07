"""Descriptor-relative private storage helpers: directories, locks and file opens."""

from __future__ import annotations

import errno
import fcntl
import os
import stat
import contextvars
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import is_canonical_absolute_path


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_HELD_REGISTRY_LOCKS: contextvars.ContextVar[frozenset[tuple[int, int]]] = contextvars.ContextVar(
    "asha_control_held_registry_locks", default=frozenset()
)
class StoreError(ValueError):
    """A registry read or write could not be completed safely."""


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
    if exc.errno == errno.ENOTDIR:
        return StoreError(f"non-directory component rejected in Control path: {path}")
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
    """Open a Control directory one component at a time from /.

    Yields None when a component is missing and ``create`` is false. With
    ``create``, missing components are made 0700 one at a time (umask-proof
    for managed ones, from ``managed_start`` down). Opens follow symlinks and
    an existing directory is used as found: its owner and mode are the
    trusted local user's business (threat model, 2026-10-05).
    """
    parts = _parts(path)
    try:
        fd = os.open("/", _DIRECTORY_FLAGS)
    except OSError as exc:
        raise StoreError(f"cannot open filesystem root for Control traversal: {exc}") from exc
    current = Path("/")
    missing = False
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
                    # Another creator won: use its directory, never chmod it.
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
                if created and index >= managed_start:
                    os.fchmod(child, 0o700)
                if create:
                    # Every visible pair may be residue from an interrupted
                    # earlier create-enabled traversal.  Re-syncing existing
                    # directories is not a semantic mutation; it lets a retry
                    # establish durability rather than trusting the failed
                    # creator or a concurrent EEXIST winner.
                    os.fsync(child)
                    os.fsync(fd)
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
    """Require a regular file: a FIFO or device would hang or mislead a reader.

    Owner, link count and mode are the trusted local user's business (threat
    model, 2026-10-05); ``required_mode`` is accepted for existing callers,
    which enforce their own mode rules.
    """
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise StoreError(f"{label} is not a regular file")
    return metadata


def _open_existing_file(directory_fd: int, name: str, label: str) -> int:
    """Open an ordinary private file, never a live SQLite database or sidecar.

    Closing a separate ordinary fd would drop SQLite's process-wide POSIX
    locks, so ControlDatabase never opens those inodes outside SQLite.
    """
    try:
        fd = os.open(name, os.O_RDONLY | _NONBLOCK | _CLOEXEC, dir_fd=directory_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
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


class SnapshotBudget:
    """Cooperative, read-only enumeration budget shared across one source.

    Counts *all* directory entries, including invalid/hidden entries. Reaching
    the cap is conservatively incomplete: there is no extra uncounted read to
    guess whether this happened to be the last entry. Blocking filesystem
    syscalls are not preemptible by this budget.
    """

    def __init__(self, *, deadline: float, limit: int = 256):
        self.deadline = deadline
        self.limit = limit
        self.scanned = 0
        self.truncated = False
        self.unavailable = 0

    def ready(self) -> bool:
        import time
        if time.monotonic() >= self.deadline or self.scanned >= self.limit:
            self.truncated = True
            return False
        return True

    def names(self, directory_fd: int) -> Iterator[str]:
        with os.scandir(directory_fd) as entries:
            while self.ready():
                entry = next(entries, None)
                if entry is None:
                    return
                self.scanned += 1
                yield entry.name

    def summary(self) -> dict[str, Any]:
        return {"scanned": self.scanned, "truncated": self.truncated,
                "unavailable_records": self.unavailable,
                "complete": not (self.truncated or self.unavailable)}
