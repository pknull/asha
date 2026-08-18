"""Bounded task-local Asha context copied from a read-only Memory snapshot."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


_ROOT = Path(__file__).resolve().parents[2]
_SESSION_TOOLS = _ROOT / "plugins" / "session" / "tools"
if str(_SESSION_TOOLS) not in sys.path:
    sys.path.insert(0, str(_SESSION_TOOLS))

from control_task_marker import canonical_marker_bytes  # type: ignore  # noqa: E402
from memory_v2 import read_published_snapshot  # type: ignore  # noqa: E402


class ContextError(ValueError):
    """Private context could not be read or provisioned safely."""


@dataclass(frozen=True)
class PlannedFile:
    content: bytes
    mode: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


DIRECTORY_MODES = {
    ".asha": 0o700,
    "Memory": 0o700,
    "Work": 0o700,
    "Work/session-state": 0o700,
}


def build_context_plan(
    source: Path, destination: Path, marker: dict[str, Any], *, snapshot=None
) -> dict[str, PlannedFile]:
    source = Path(source)
    destination = Path(destination)
    try:
        snapshot = snapshot if snapshot is not None else read_published_snapshot(source)
        marker_bytes = canonical_marker_bytes(marker, workspace_root=destination)
    except (OSError, ValueError) as exc:
        raise ContextError(str(exc)) from exc
    return {
        ".asha/config.json": PlannedFile(
            snapshot.config, snapshot.modes[".asha/config.json"]
        ),
        ".asha/control-task.json": PlannedFile(marker_bytes, 0o600),
        "Memory/activeContext.md": PlannedFile(
            snapshot.active_context, snapshot.modes["Memory/activeContext.md"]
        ),
        "Memory/decisions.md": PlannedFile(
            snapshot.decisions, snapshot.modes["Memory/decisions.md"]
        ),
    }


_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
    getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _open_absolute_directory(path: Path) -> int:
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        raise ContextError("task workspace must be an exact canonical directory")
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as exc:
        os.close(fd)
        raise ContextError(f"cannot open task workspace without following links: {exc}") from exc


def _root_fact(fd: int) -> tuple[int, int, int, int]:
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ContextError("task workspace must be one owned directory")
    return (
        metadata.st_dev, metadata.st_ino,
        stat.S_IMODE(metadata.st_mode), metadata.st_uid,
    )


def _reopen_destination(destination: Path, expected: tuple[int, int, int, int]) -> int:
    fd = _open_absolute_directory(destination)
    if _root_fact(fd) != expected:
        os.close(fd)
        raise ContextError("task workspace path changed during context provisioning")
    return fd


def _open_parent(root_fd: int, relative: str) -> tuple[int, str]:
    parts = Path(relative).parts
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, parts[-1]
    except OSError as exc:
        os.close(fd)
        raise ContextError(f"private context parent changed: {relative}") from exc


def _entry_kind(root_fd: int, relative: str) -> str | None:
    """Kind of an existing entry (`directory`, `file`, `other`) or None."""
    parts = Path(relative).parts
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                return None
            os.close(fd)
            fd = child
        try:
            metadata = os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISDIR(metadata.st_mode):
            return "directory"
        if stat.S_ISREG(metadata.st_mode):
            return "file"
        return "other"
    except OSError as exc:
        raise ContextError(f"private context path is unsafe: {relative}") from exc
    finally:
        os.close(fd)


def _entry_exists(root_fd: int, relative: str) -> bool:
    return _entry_kind(root_fd, relative) is not None


# The task marker is the one context entry no repository may already carry: it
# binds the workspace to exactly this task and must be created by Control.
_MARKER_PATH = ".asha/control-task.json"


def _entry_fact(metadata: os.stat_result, kind: str, *, planned: PlannedFile | None = None
                ) -> dict[str, Any]:
    value: dict[str, Any] = {
        "type": kind, "dev": metadata.st_dev, "ino": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid,
    }
    if planned is not None:
        value.update({"sha256": planned.sha256, "size": len(planned.content)})
    return value


def _write_new_file(directory_fd: int, name: str, planned: PlannedFile) -> dict[str, Any]:
    flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL |
        getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(name, flags, planned.mode, dir_fd=directory_fd)
    try:
        os.fchmod(fd, planned.mode)
        written = 0
        while written < len(planned.content):
            count = os.write(fd, planned.content[written:])
            if count <= 0:
                raise ContextError(f"short write while provisioning {name}")
            written += count
        os.fsync(fd)
        metadata = os.fstat(fd)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or
                metadata.st_uid != os.geteuid() or
                stat.S_IMODE(metadata.st_mode) != planned.mode):
            raise ContextError(f"private context file ownership changed: {name}")
        return _entry_fact(metadata, "file", planned=planned)
    finally:
        os.close(fd)


def provision_context(
    source: Path,
    destination: Path,
    marker: dict[str, Any],
    *,
    before_mutation: Callable[[dict[str, PlannedFile]], None] | None = None,
    after_file: Callable[[str], None] | None = None,
    after_entry: Callable[[str, dict[str, Any]], None] | None = None,
    snapshot=None,
) -> dict[str, dict[str, Any]]:
    """Copy the exact bounded context set; never follow or replace a leaf."""
    source = Path(source)
    destination = Path(destination)
    plan = build_context_plan(source, destination, marker, snapshot=snapshot)
    # A fresh workspace may already carry some of these paths as TRACKED
    # content of the base commit (a repository that commits `.asha/` or
    # `Memory/`). Those stay exactly as checked out: overwriting a tracked
    # file would dirty the change, and the directory is simply reused. Only
    # the task marker, symlinks, and non-regular entries are collisions.
    existing_directories: set[str] = set()
    existing_files: set[str] = set()
    initial_fd = _open_absolute_directory(destination)
    try:
        root_fact = _root_fact(initial_fd)
        for relative in DIRECTORY_MODES:
            kind = _entry_kind(initial_fd, relative)
            if kind is None:
                continue
            if kind != "directory":
                raise ContextError(f"private context destination collision: {relative}")
            existing_directories.add(relative)
        for relative in plan:
            kind = _entry_kind(initial_fd, relative)
            if kind is None:
                continue
            if kind != "file" or relative == _MARKER_PATH:
                raise ContextError(f"private context destination collision: {relative}")
            existing_files.add(relative)
    finally:
        os.close(initial_fd)
    if before_mutation is not None:
        before_mutation(plan)
    try:
        for relative, mode in DIRECTORY_MODES.items():
            if relative in existing_directories:
                continue
            root_fd = _reopen_destination(destination, root_fact)
            try:
                parent_fd, name = _open_parent(root_fd, relative)
                try:
                    os.mkdir(name, mode, dir_fd=parent_fd)
                    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
                    try:
                        os.fchmod(child_fd, mode)
                        metadata = os.fstat(child_fd)
                        if (metadata.st_uid != os.geteuid() or
                                stat.S_IMODE(metadata.st_mode) != mode):
                            raise ContextError(f"private context directory ownership changed: {relative}")
                        os.fsync(child_fd)
                        os.fsync(parent_fd)
                        fact = _entry_fact(metadata, "directory")
                    finally:
                        os.close(child_fd)
                finally:
                    os.close(parent_fd)
            finally:
                os.close(root_fd)
            if after_entry is not None:
                after_entry(relative, fact)
        for relative, planned in plan.items():
            if relative in existing_files:
                continue
            root_fd = _reopen_destination(destination, root_fact)
            try:
                parent_fd, name = _open_parent(root_fd, relative)
                try:
                    fact = _write_new_file(parent_fd, name, planned)
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            finally:
                os.close(root_fd)
            if after_entry is not None:
                after_entry(relative, fact)
            if after_file is not None:
                after_file(relative)
        for relative in reversed(tuple(DIRECTORY_MODES)):
            root_fd = _reopen_destination(destination, root_fact)
            try:
                dir_fd, name = _open_parent(root_fd, relative)
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=dir_fd)
                try:
                    os.fsync(child_fd)
                finally:
                    os.close(child_fd)
                    os.close(dir_fd)
            finally:
                os.close(root_fd)
    except (OSError, ValueError) as exc:
        if isinstance(exc, ContextError):
            raise
        raise ContextError(f"private context provisioning failed: {exc}") from exc
    return {
        relative: {"sha256": planned.sha256, "mode": planned.mode}
        for relative, planned in plan.items()
    }
