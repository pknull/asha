"""Prepare isolated task workspaces and persist rollback-safe creation state."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import ControlConfig, validate_workspace_root
from .context import DIRECTORY_MODES, build_context_plan, provision_context, read_published_snapshot
from .jj import (
    JjAdapter, JjError, MAX_MATERIALIZATION_ENTRIES,
    MAX_TRACKED_BLOB_BYTES, MAX_TRACKED_TOTAL_BYTES,
)
from .model import (
    GIT_OBJECT_ID_PATTERN, TASK_CONTRACT, canonical_uuid, validate_slug,
    validate_task,
)
from .store import TaskStore, StoreError, task_digest
from .transaction import (
    CreationJournalStore, JOURNAL_CONTRACT, JournalError, MAX_JOURNAL_BYTES,
)


MAX_OWNERSHIP_ENTRIES = MAX_MATERIALIZATION_ENTRIES + 16
MAX_OWNERSHIP_MANIFEST_BYTES = 512 * 1024
_DIRECTORY_FLAGS = (
    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
    getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)


def derive_repository_identity(
    project_id: str, repository_root: Path, git_root: Path
) -> tuple[str, str]:
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("stable project_id is required for repository identity")
    payload = b"\0".join((
        b"asha-control-repository-v1", project_id.strip().encode("utf-8"),
        str(repository_root).encode("utf-8"), str(git_root).encode("utf-8"),
    ))
    digest = hashlib.sha256(payload).hexdigest()
    readable = re.sub(r"[^a-z0-9]+", "-", Path(repository_root).name.lower()).strip("-")
    readable = (readable or "repository")[:40].rstrip("-")
    return f"repo:{digest}", f"{readable}-{digest[:16]}"


def _repository_lock_id(repository_identity: str) -> str:
    return (
        f"{repository_identity[5:13]}-{repository_identity[13:17]}-"
        f"{repository_identity[17:21]}-{repository_identity[21:25]}-"
        f"{repository_identity[25:37]}"
    )


class PreparationError(ValueError):
    pass


@dataclass(frozen=True)
class PrepareRequest:
    repository: Path
    requested_base: str = "trunk()"
    task_id: str = ""
    slug: str = ""
    label: str = ""
    source: dict[str, Any] = field(default_factory=lambda: {
        "kind": "ad-hoc", "number": None, "url": None,
    })
    resolved_base_commit_id: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _same_inode(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _inode_fact(metadata: os.stat_result) -> dict[str, int]:
    return {
        "dev": metadata.st_dev, "ino": metadata.st_ino,
        "mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid,
    }


def _open_absolute_directory(path: Path) -> int:
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        raise PreparationError("Control path is not exact and canonical")
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as exc:
        os.close(fd)
        raise PreparationError(f"cannot open Control path without following links: {exc}") from exc


def _make_workspace_private(path: Path) -> None:
    """Pin and privatize the workspace root created by jj."""
    fd = _open_absolute_directory(path)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != os.geteuid():
            raise PreparationError("created task workspace is not owned by the effective user")
        os.fchmod(fd, 0o700)
        metadata = os.fstat(fd)
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            raise PreparationError("created task workspace must have mode 0700")
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_registered_workspace_private(
    adapter: JjAdapter,
    source: Path,
    destination: Path,
    name: str,
    base_commit_id: str,
    description: str,
) -> None:
    """Privatize an exceptional jj result only after exact registration checks."""
    observed = adapter.workspace_identities(source).get(name)
    if observed is None:
        return
    identity = adapter.inspect_workspace(destination, name, require_empty=False)
    if (identity.parent_commit_ids != (base_commit_id,) or
            identity.description != description or
            observed != (identity.change_id, identity.commit_id)):
        return
    _make_workspace_private(destination)


def _file_fact_at(directory_fd: int, name: str, budget: list[int]) -> dict[str, Any]:
    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    value: dict[str, Any] = {**_inode_fact(metadata)}
    if stat.S_ISDIR(metadata.st_mode):
        return {**value, "type": "directory"}
    if stat.S_ISREG(metadata.st_mode):
        if metadata.st_nlink != 1:
            raise PreparationError("hard-linked workspace file is not controller-owned")
        if metadata.st_size > MAX_TRACKED_BLOB_BYTES or budget[0] + metadata.st_size > MAX_TRACKED_TOTAL_BYTES:
            raise PreparationError("workspace ownership hashing exceeds the bounded byte limit; preserving")
        fd = os.open(
            name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
            getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
        digest = hashlib.sha256()
        remaining = metadata.st_size
        try:
            opened = os.fstat(fd)
            if not _same_inode(metadata, opened) or not stat.S_ISREG(opened.st_mode):
                raise PreparationError("workspace entry changed during ownership inspection")
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise PreparationError("workspace file shortened during ownership inspection")
                digest.update(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise PreparationError("workspace file grew during ownership inspection")
        finally:
            os.close(fd)
        budget[0] += metadata.st_size
        return {**value, "type": "file", "sha256": digest.hexdigest(), "size": metadata.st_size}
    if stat.S_ISLNK(metadata.st_mode):
        target = os.readlink(name, dir_fd=directory_fd)
        if not _same_inode(metadata, os.stat(name, dir_fd=directory_fd, follow_symlinks=False)):
            raise PreparationError("workspace symlink changed during ownership inspection")
        return {**value, "type": "symlink", "target": target}
    raise PreparationError("special filesystem object in task workspace; preserving")


def _capture_tree_fd(
    directory_fd: int, prefix: str, result: dict[str, dict[str, Any]], budget: list[int]
) -> None:
    try:
        names = os.listdir(directory_fd)
    except OSError as exc:
        raise PreparationError(f"cannot inspect task workspace: {exc}") from exc
    for name in names:
        relative = f"{prefix}/{name}" if prefix else name
        fact = _file_fact_at(directory_fd, name, budget)
        result[relative] = fact
        if len(result) > MAX_OWNERSHIP_ENTRIES:
            raise PreparationError("workspace ownership manifest exceeds 8192 entries; preserving")
        if fact["type"] == "directory":
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                if not _same_inode(
                    os.fstat(child_fd), os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                ):
                    raise PreparationError("workspace directory changed during inspection")
                _capture_tree_fd(child_fd, relative, result, budget)
            finally:
                os.close(child_fd)


def _capture_tree(root: Path, expected_root: dict[str, Any] | None = None
                  ) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    root_fd = _open_absolute_directory(root)
    try:
        root_metadata = os.fstat(root_fd)
        if root_metadata.st_uid != os.geteuid():
            raise PreparationError("task workspace is not owned by the effective user")
        root_fact = _inode_fact(root_metadata)
        if expected_root is not None and root_fact != expected_root:
            raise PreparationError("task workspace root identity changed; preserved")
        result: dict[str, dict[str, Any]] = {}
        _capture_tree_fd(root_fd, "", result, [0])
    finally:
        os.close(root_fd)
    raw = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_OWNERSHIP_MANIFEST_BYTES:
        raise PreparationError(
            f"workspace ownership manifest exceeds {MAX_OWNERSHIP_MANIFEST_BYTES} bytes; preserving"
        )
    return result, root_fact


def _planned_manifest(plan) -> dict[str, dict[str, Any]]:
    result = {
        relative: {"type": "directory", "mode": mode, "uid": os.geteuid()}
        for relative, mode in DIRECTORY_MODES.items()
    }
    for relative, item in plan.items():
        result[relative] = {
            "type": "file", "mode": item.mode, "uid": os.geteuid(),
            "sha256": item.sha256, "size": len(item.content),
        }
    return result


def _fact_projection(fact: dict[str, Any]) -> dict[str, Any]:
    if fact["type"] == "directory":
        return {"type": "directory"}
    if fact["type"] == "symlink":
        return {"type": "symlink", "target": fact["target"]}
    return {
        "type": "file", "mode": fact["mode"], "sha256": fact["sha256"],
        "size": fact["size"],
    }


_JJ_PATHS = {
    ".jj": "directory", ".jj/repo": "file",
    ".jj/working_copy": "directory", ".jj/working_copy/checkout": "file",
    ".jj/working_copy/tree_state": "file", ".jj/working_copy/type": "file",
}


def _read_small_exact(path: Path, maximum: int = 4096) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
        raise PreparationError(f"jj binding file is not one bounded regular file: {path.name}")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        data = os.read(fd, maximum + 1)
        if len(data) != metadata.st_size:
            raise PreparationError("jj binding file changed during inspection")
        return data
    finally:
        os.close(fd)


def _compact_materialized_ownership(
    actual: dict[str, dict[str, Any]], expected: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    tracked = [
        [actual[path][key] for key in ("dev", "ino", "mode", "uid")]
        for path in sorted(expected)
    ]
    private = {
        path: fact for path, fact in actual.items()
        if path == ".jj" or path.startswith(".jj/")
    }
    return {"tracked": tracked, "private": private}


def _verify_materialization(
    destination: Path, source: Path, expected: dict[str, dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, int]]:
    if any(path == ".jj" or path.startswith(".jj/") for path in expected):
        raise PreparationError("selected revision tracks the reserved .jj path")
    actual, root_fact = _capture_tree(destination)
    allowed = set(expected) | set(_JJ_PATHS)
    if set(actual) != allowed:
        raise PreparationError("workspace contains content outside the expected jj materialization; preserved")
    for relative, projected in expected.items():
        if _fact_projection(actual[relative]) != projected:
            raise PreparationError(f"jj materialization differs from selected revision at {relative}; preserved")
    for relative, kind in _JJ_PATHS.items():
        if actual[relative]["type"] != kind:
            raise PreparationError("jj working-copy binding has an unexpected type; preserved")
    expected_repo = str(source / ".jj" / "repo").encode("utf-8")
    if _read_small_exact(destination / ".jj" / "repo") != expected_repo:
        raise PreparationError("jj workspace repository binding is foreign; preserved")
    if _read_small_exact(destination / ".jj" / "working_copy" / "type") != b"local":
        raise PreparationError("jj workspace working-copy binding is foreign; preserved")
    return _compact_materialized_ownership(actual, expected), root_fact


def _validate_layout(config: ControlConfig, source: Path, destination: Path, repo_key: str, slug: str) -> None:
    validate_workspace_root(config.workspace_root, home=config.home, repository=source)
    if destination != config.workspace_root / repo_key / slug:
        raise PreparationError("workspace destination is not bound to the current Control config")
    if (not source.is_absolute() or os.path.realpath(source) != str(source) or
            source.is_symlink() or not source.is_dir()):
        raise PreparationError("source repository path identity changed")
    current = Path("/")
    for part in destination.parent.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        if not stat.S_ISDIR(metadata.st_mode):
            raise PreparationError("workspace destination ancestry contains a symlink or non-directory")


def _count_missing_destination_ancestors(
    config: ControlConfig, source: Path, destination: Path, repo_key: str, slug: str,
) -> int:
    """Count the exact absent prefix below the last existing pinned ancestor."""
    _validate_layout(config, source, destination, repo_key, slug)
    target = destination.parent
    parts = target.parts[1:]
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for index, part in enumerate(parts):
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                return len(parts) - index
            os.close(fd)
            fd = child
        return 0
    except OSError as exc:
        raise PreparationError(
            f"cannot count workspace destination ancestors without following links: {exc}"
        ) from exc
    finally:
        os.close(fd)


def _create_destination_parents(
    config: ControlConfig, source: Path, destination: Path, repo_key: str, slug: str,
    record: Callable[[dict[str, Any]], None],
) -> list[dict[str, Any]]:
    _validate_layout(config, source, destination, repo_key, slug)
    target = destination.parent
    fd = os.open("/", _DIRECTORY_FLAGS)
    current = Path("/")
    created: list[dict[str, Any]] = []
    managed_start = len(config.workspace_root.parts) - 2
    try:
        for index, part in enumerate(target.parts[1:]):
            _validate_layout(config, source, destination, repo_key, slug)
            parent_metadata = os.fstat(fd)
            visible_parent = _open_absolute_directory(current)
            try:
                if not _same_inode(parent_metadata, os.fstat(visible_parent)):
                    raise PreparationError("workspace parent ancestry changed before mutation")
            finally:
                os.close(visible_parent)
            current /= part
            made = False
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if len(created) >= 8:
                    raise PreparationError(
                        "workspace destination requires more than eight created ancestors"
                    )
                os.mkdir(part, 0o700, dir_fd=fd)
                made = True
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
                os.fchmod(child, 0o700)
                os.fsync(child)
                os.fsync(fd)
            child_metadata = os.fstat(child)
            visible_child = _open_absolute_directory(current)
            try:
                if not _same_inode(child_metadata, os.fstat(visible_child)):
                    raise PreparationError("workspace parent changed before ownership recording")
            finally:
                os.close(visible_child)
            if index >= managed_start:
                if child_metadata.st_uid != os.geteuid():
                    os.close(child)
                    raise PreparationError(
                        "workspace destination parent is not owned by the effective user: "
                        f"{current}"
                    )
                if stat.S_IMODE(child_metadata.st_mode) != 0o700:
                    os.close(child)
                    raise PreparationError(
                        f"workspace destination parent must have mode 0700: {current}"
                    )
            if made:
                item = {
                    "path": str(current), "parent_path": str(current.parent),
                    "dev": child_metadata.st_dev, "ino": child_metadata.st_ino,
                    "parent_dev": parent_metadata.st_dev, "parent_ino": parent_metadata.st_ino,
                    "mode": stat.S_IMODE(child_metadata.st_mode), "uid": child_metadata.st_uid,
                }
                created.append(item)
                record(item)
            os.close(fd)
            fd = child
    except Exception:
        os.close(fd)
        raise
    os.close(fd)
    return created


def _assert_task_binding(task: dict[str, Any], journal: dict[str, Any]) -> None:
    if task["task_id"] != journal["task_id"]:
        raise PreparationError("creation journal task identity mismatch; preserved")
    if task["slug"] != journal["task"]["slug"] or task["label"] != journal["task"]["label"]:
        raise PreparationError("creation journal task metadata mismatch; preserved")
    if task["repository"] != {k: journal["repository"][k] for k in ("root", "identity")}:
        raise PreparationError("creation journal repository mismatch; preserved")
    jj = task["jj"]
    expected = journal["jj"]
    require_task_identity = journal["phase"] in {
        "task-identity-recorded", "ready-for-launch", "tmux-intent",
        "tmux-session-created", "launch-attempted", "run-recorded",
    }
    if (jj["workspace_path"] != journal["workspace"]["path"] or
            jj["workspace_name"] != journal["workspace"]["name"] or
            jj["base_commit_id"] != expected["base_commit_id"] or
            (require_task_identity and expected["change_id"] is not None and
             jj["change_id"] != expected["change_id"]) or
            (require_task_identity and expected["working_commit_id"] is not None and
             jj["working_commit_id"] != expected["working_commit_id"])):
        raise PreparationError("creation journal and task jj identity mismatch; preserved")
    if journal["task"]["digest"] is not None and task_digest(task) != journal["task"]["digest"]:
        reconciled_identity_replace = False
        if (journal["phase"] == "task-identity-intent" and
                task["jj"]["change_id"] == expected["change_id"] and
                task["jj"]["working_commit_id"] == expected["working_commit_id"]):
            prior = copy.deepcopy(task)
            prior["jj"]["change_id"] = None
            prior["jj"]["working_commit_id"] = None
            reconciled_identity_replace = task_digest(prior) == journal["task"]["digest"]
        failure = journal["task"]["failure"]
        reconciled_failure_replace = bool(
            failure is not None and task["lifecycle"] == "failed" and
            task["updated_at"] == failure["updated_at"] and
            task_digest(task) == failure["digest"]
        )
        if not reconciled_identity_replace and not reconciled_failure_replace:
            raise PreparationError("task record changed outside its creation transaction; preserved")


def _owned_manifest(journal: dict[str, Any]) -> dict[str, dict[str, Any]]:
    materialized = journal["materialized_owned"]
    if materialized is None:
        result: dict[str, dict[str, Any]] = {}
    else:
        expected = journal["expected_materialization"]
        paths = sorted(expected)
        tracked = materialized["tracked"]
        if len(tracked) != len(paths):
            raise PreparationError("tracked ownership count differs from materialization intent; preserved")
        result = {
            path: {
                **expected[path],
                **dict(zip(("dev", "ino", "mode", "uid"), tracked[index])),
            }
            for index, path in enumerate(paths)
        }
        result.update(materialized["private"])
    if journal["recovery_owned"] is not None:
        result.update(journal["recovery_owned"])
    overlap = set(result) & set(journal["context_owned"])
    if overlap:
        raise PreparationError("context ownership overlaps jj materialization; preserved")
    for path, ownership in journal["context_owned"].items():
        planned = journal["planned_context"]
        if planned is None or path not in planned:
            raise PreparationError("context ownership is not bound to its plan; preserved")
        result[path] = {**planned[path], **ownership}
    return result


_MAX_FACT = {
    "dev": 18_446_744_073_709_551_615,
    "ino": 18_446_744_073_709_551_615,
    "mode": 4095,
    "uid": 4_294_967_295,
}


def _worst_private_ownership() -> dict[str, dict[str, Any]]:
    result = {}
    for path, kind in _JJ_PATHS.items():
        fact: dict[str, Any] = {**_MAX_FACT, "type": kind}
        if kind == "file":
            fact.update({
                "sha256": "f" * 64, "size": MAX_TRACKED_BLOB_BYTES,
            })
        result[path] = fact
    return result


def _ensure_creation_journal_capacity(
    journal: dict[str, Any], planned_context: dict[str, dict[str, Any]],
) -> int:
    """Prove the largest reachable journal fits before the first mutation."""
    expected = journal["expected_materialization"]
    if len(expected) > MAX_MATERIALIZATION_ENTRIES:
        raise PreparationError(
            f"tracked revision exceeds the {MAX_MATERIALIZATION_ENTRIES}-entry capacity"
        )
    maximum = copy.deepcopy(journal)
    maximum["phase"] = "task-identity-recorded"
    maximum["task"]["digest"] = "f" * 64
    maximum["task"]["failure"] = {
        "digest": "f" * 64, "updated_at": "9999-12-31T23:59:59.999999Z",
    }
    maximum["workspace"]["root_fact"] = dict(_MAX_FACT)
    destination = maximum["workspace"]["path"]
    maximum["workspace"]["created_parents"] = [{
        "path": destination, "parent_path": str(Path(destination).parent),
        "dev": _MAX_FACT["dev"], "ino": _MAX_FACT["ino"],
        "parent_dev": _MAX_FACT["dev"], "parent_ino": _MAX_FACT["ino"],
        "mode": _MAX_FACT["mode"], "uid": _MAX_FACT["uid"],
    } for _ in range(8)]
    maximum["jj"].update({
        "change_id": "k" * 32, "working_commit_id": "f" * 64,
        "registration_state": "absent-after-forget",
        "last_registration": {"change_id": "k" * 32, "working_commit_id": "f" * 64},
    })
    private = _worst_private_ownership()
    maximum["materialized_owned"] = {
        "tracked": [[value for value in _MAX_FACT.values()] for _ in sorted(expected)],
        "private": private,
    }
    maximum["recovery_owned"] = copy.deepcopy(private)
    maximum["planned_context"] = copy.deepcopy(planned_context)
    maximum["context_owned"] = {
        path: dict(_MAX_FACT) for path in planned_context
    }
    maximum["removal"] = {
        "entries_removed": len(expected) + len(_JJ_PATHS) + len(planned_context),
        "root_removed": True,
        "parents_removed": 8,
    }
    raw = json.dumps(
        maximum, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    if len(raw) > MAX_JOURNAL_BYTES:
        raise PreparationError(
            f"creation journal worst case exceeds the {MAX_JOURNAL_BYTES}-byte capacity"
        )
    return len(raw)


def _save_phase(
    journals: CreationJournalStore, journal: dict[str, Any], phase: str,
    failure_injector: Callable[[str], None] | None = None,
) -> None:
    previous = journal["phase"]
    journal["phase"] = phase
    journals.save(journal, expected_phase=previous)
    if failure_injector is not None:
        failure_injector(f"journal:{phase}")


def _mark_preserved(
    journals: CreationJournalStore, tasks: TaskStore, journal: dict[str, Any],
) -> None:
    if journal["phase"] != "preserved":
        previous = journal["phase"]
        journal["phase"] = "preserved"
        journals.save(journal, expected_phase=previous)
    _fail_task(tasks, journals, journal)


def _remove_owned_tree(
    destination: Path, journal: dict[str, Any], journals: CreationJournalStore,
    failure_injector: Callable[[str], None] | None,
) -> None:
    expected = _owned_manifest(journal)
    removed = journal["removal"]["entries_removed"]
    # Files/symlinks first, deepest directories next.
    ordered = sorted(
        (p for p in expected if expected[p]["type"] != "directory"),
        key=lambda p: (p.count("/"), p), reverse=True,
    ) + sorted(
        (p for p in expected if expected[p]["type"] == "directory"),
        key=lambda p: (p.count("/"), p), reverse=True,
    )
    if removed > len(ordered):
        raise PreparationError("workspace removal journal exceeds its owned manifest; preserved")
    root_fd = _open_absolute_directory(destination)
    try:
        if _inode_fact(os.fstat(root_fd)) != journal["workspace"]["root_fact"]:
            raise PreparationError("workspace root identity changed; preserved")

        def parent(relative: str) -> tuple[int, str]:
            parts = Path(relative).parts
            fd = os.dup(root_fd)
            try:
                for part in parts[:-1]:
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
                    os.close(fd)
                    fd = child
                return fd, parts[-1]
            except Exception:
                os.close(fd)
                raise

        # Infer only the contiguous crash gap immediately after a recorded
        # removal intent. Missing later entries remain ambiguous.
        while removed < len(ordered):
            relative = ordered[removed]
            pfd, name = parent(relative)
            try:
                try:
                    os.stat(name, dir_fd=pfd, follow_symlinks=False)
                except FileNotFoundError:
                    removed += 1
                    journal["removal"]["entries_removed"] = removed
                    journals.save(journal, expected_phase=journal["phase"])
                    continue
                actual = _file_fact_at(pfd, name, [0])
                if actual != expected[relative]:
                    raise PreparationError(f"workspace entry changed at {relative}; preserved")
                if actual["type"] == "directory":
                    os.rmdir(name, dir_fd=pfd)
                else:
                    os.unlink(name, dir_fd=pfd)
                os.fsync(pfd)
            finally:
                os.close(pfd)
            if failure_injector is not None:
                failure_injector(f"removed:{relative}")
            removed += 1
            journal["removal"]["entries_removed"] = removed
            journals.save(journal, expected_phase=journal["phase"])
    finally:
        os.close(root_fd)


def _remove_workspace_root(
    destination: Path, journal: dict[str, Any], journals: CreationJournalStore,
    failure_injector: Callable[[str], None] | None,
) -> None:
    if journal["removal"]["root_removed"]:
        if destination.exists() or destination.is_symlink():
            raise PreparationError("workspace root reappeared after removal; preserved")
        return
    parent_fd = _open_absolute_directory(destination.parent)
    try:
        name = destination.name
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            journal["removal"]["root_removed"] = True
            journals.save(journal, expected_phase=journal["phase"])
            return
        if _inode_fact(metadata) != journal["workspace"]["root_fact"]:
            raise PreparationError("workspace root changed before removal; preserved")
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    if failure_injector is not None:
        failure_injector("removed:workspace-root")
    journal["removal"]["root_removed"] = True
    journals.save(journal, expected_phase=journal["phase"])


def _remove_created_parents(
    journal: dict[str, Any], journals: CreationJournalStore,
    failure_injector: Callable[[str], None] | None,
) -> None:
    items = list(reversed(journal["workspace"]["created_parents"]))
    removed = journal["removal"]["parents_removed"]
    if removed > len(items):
        raise PreparationError("created-parent removal journal exceeds its owned parents; preserved")
    for item in items[removed:]:
        path = Path(item["path"])
        parent_fd = _open_absolute_directory(Path(item["parent_path"]))
        try:
            parent_meta = os.fstat(parent_fd)
            if (parent_meta.st_dev, parent_meta.st_ino) != (item["parent_dev"], item["parent_ino"]):
                raise PreparationError("created parent ancestry changed; preserved")
            try:
                metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                removed += 1
                journal["removal"]["parents_removed"] = removed
                journals.save(journal, expected_phase=journal["phase"])
                continue
            if (_inode_fact(metadata) != {k: item[k] for k in ("dev", "ino", "mode", "uid")} or
                    not stat.S_ISDIR(metadata.st_mode)):
                raise PreparationError("created parent ownership changed; preserved")
            os.rmdir(path.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        if failure_injector is not None:
            failure_injector(f"removed-parent:{item['path']}")
        removed += 1
        journal["removal"]["parents_removed"] = removed
        journals.save(journal, expected_phase=journal["phase"])


def _fail_task(tasks: TaskStore, journals: CreationJournalStore, journal: dict[str, Any]) -> None:
    try:
        task = tasks.read(journal["task_id"])
    except StoreError:
        return
    if task["lifecycle"] == "creating":
        changed = copy.deepcopy(task)
        changed["lifecycle"] = "failed"
        failure = journal["task"]["failure"]
        if failure is None:
            changed["updated_at"] = _now()
            failure = {
                "digest": task_digest(changed), "updated_at": changed["updated_at"],
            }
            journal["task"]["failure"] = failure
            journals.save(journal, expected_phase=journal["phase"])
        else:
            changed["updated_at"] = failure["updated_at"]
            if task_digest(changed) != failure["digest"]:
                raise PreparationError("planned failed task bytes no longer match; preserved")
        tasks.save(changed, expected_digest=task_digest(task))
        journal["task"]["digest"] = failure["digest"]
        journals.save(journal, expected_phase=journal["phase"])
    elif task["lifecycle"] == "failed":
        failure = journal["task"]["failure"]
        if (failure is None or task["updated_at"] != failure["updated_at"] or
                task_digest(task) != failure["digest"]):
            raise PreparationError("failed task does not match durable failure intent; preserved")
        if journal["task"]["digest"] != failure["digest"]:
            journal["task"]["digest"] = failure["digest"]
            journals.save(journal, expected_phase=journal["phase"])


def _rollback_locked(
    config: ControlConfig, task_id: str, adapter: JjAdapter,
    *, invocation_id: str | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> None:
    journals = CreationJournalStore(config)
    tasks = TaskStore(config)
    journal = journals.read(task_id)
    if invocation_id is not None and journal["invocation_id"] != invocation_id:
        raise PreparationError("creation journal belongs to another invocation; preserved")
    source = Path(journal["repository"]["root"])
    destination = Path(journal["workspace"]["path"])
    _validate_layout(
        config, source, destination, journal["repository"]["repo_key"], journal["task"]["slug"]
    )
    snapshot = read_published_snapshot(source)
    repository_facts = adapter.preflight(source)
    identity, repo_key = derive_repository_identity(
        snapshot.project_id, repository_facts.root, repository_facts.git_root,
    )
    if (identity != journal["repository"]["identity"] or
            repo_key != journal["repository"]["repo_key"] or
            str(repository_facts.git_root) != journal["repository"]["git_root"]):
        raise PreparationError("repository identity no longer matches the creation journal; preserved")
    try:
        task = tasks.read(task_id)
    except StoreError:
        task = None
    if task is not None:
        _assert_task_binding(task, journal)
        if task["lifecycle"] == "failed":
            _fail_task(tasks, journals, journal)
    elif journal["task"]["digest"] is not None:
        raise PreparationError("bound task record is missing; preserved")
    if journal["launch_attempted"] or journal["phase"] == "launch-attempted":
        raise PreparationError("launch was attempted; rollback is forbidden and workspace is preserved")
    if journal["phase"] == "rolled-back":
        return
    if journal["phase"] == "preserved":
        # The preserved phase replacement may have become visible immediately
        # before process death.  Task binding above admits only the exact
        # controller-owned creating record or its durably planned failed form.
        _fail_task(tasks, journals, journal)
        raise PreparationError("creation transaction was preserved for explicit recovery")

    name = journal["workspace"]["name"]
    registrations = adapter.workspace_identities(source)
    observed_registration = registrations.get(name)
    if observed_registration is not None:
        known = journal["jj"]["last_registration"]
        recorded = (
            (journal["jj"]["change_id"], journal["jj"]["working_commit_id"])
            if (journal["jj"]["change_id"] is not None and
                journal["jj"]["working_commit_id"] is not None)
            else None
        )
        known_tuple = (
            (known["change_id"], known["working_commit_id"])
            if known is not None else None
        )
        if ((known_tuple is not None and observed_registration != known_tuple) or
                (recorded is not None and observed_registration != recorded)):
            _mark_preserved(journals, tasks, journal)
            raise PreparationError("jj workspace registration is foreign; preserved")
        if not destination.exists() or destination.is_symlink():
            journal["jj"]["registration_state"] = "present"
            journal["jj"]["last_registration"] = {
                "change_id": observed_registration[0], "working_commit_id": observed_registration[1],
            }
            journals.save(journal, expected_phase=journal["phase"])
            _fail_task(tasks, journals, journal)
            raise PreparationError(
                "jj registration exists without its owned destination; "
                f"recovery remains interrupted at {journal['phase']}"
            )
        identity = adapter.inspect_workspace(destination, name, require_empty=False)
        if (identity.parent_commit_ids != (journal["jj"]["base_commit_id"],) or
                identity.description != journal["jj"]["description"] or
                observed_registration != (identity.change_id, identity.commit_id)):
            _mark_preserved(journals, tasks, journal)
            raise PreparationError("jj workspace identity changed; preserved")
        if journal["materialized_owned"] is None:
            try:
                owned, root_fact = _verify_materialization(
                    destination, source, journal["expected_materialization"]
                )
            except PreparationError:
                _mark_preserved(journals, tasks, journal)
                raise
            journal["materialized_owned"] = owned
            journal["workspace"]["root_fact"] = root_fact
            journal["jj"]["change_id"] = identity.change_id
            journal["jj"]["working_commit_id"] = identity.commit_id
            journal["jj"]["last_registration"] = {
                "change_id": identity.change_id, "working_commit_id": identity.commit_id,
            }
            journal["jj"]["registration_state"] = "present"
            if journal["phase"] == "workspace-add-intent":
                _save_phase(journals, journal, "workspace-added", failure_injector)
            _save_phase(journals, journal, "workspace-recorded", failure_injector)
        actual, _ = _capture_tree(destination, journal["workspace"]["root_fact"])
        expected_owned = _owned_manifest(journal)
        if actual != expected_owned:
            # A destination working-copy snapshot may atomically rewrite jj's
            # private checkout state. It may not alter or add any non-.jj
            # entry. Revalidate the exact binding before recording the new
            # removal facts; this never adopts arbitrary workspace content.
            non_jj = lambda values: {
                path: fact for path, fact in values.items()
                if path != ".jj" and not path.startswith(".jj/")
            }
            if non_jj(actual) != non_jj(expected_owned) or {
                path for path in actual if path == ".jj" or path.startswith(".jj/")
            } != set(_JJ_PATHS):
                _mark_preserved(journals, tasks, journal)
                raise PreparationError("workspace contains foreign or changed content; preserved")
            if _read_small_exact(destination / ".jj" / "repo") != str(
                source / ".jj" / "repo"
            ).encode("utf-8"):
                _mark_preserved(journals, tasks, journal)
                raise PreparationError("jj workspace binding changed; preserved")
            if any(actual[path]["type"] != kind for path, kind in _JJ_PATHS.items()):
                _mark_preserved(journals, tasks, journal)
                raise PreparationError("jj workspace binding type changed; preserved")
            journal["recovery_owned"] = {
                path: fact for path, fact in actual.items()
                if path == ".jj" or path.startswith(".jj/")
            }
            journals.save(journal, expected_phase=journal["phase"])
    else:
        if journal["jj"]["registration_state"] in {"present", "forget-intent"}:
            journal["jj"]["registration_state"] = "absent-after-forget"
        elif journal["jj"]["registration_state"] in {"add-intent", "unknown"}:
            journal["jj"]["registration_state"] = "absent"
        if destination.exists() or destination.is_symlink():
            # Without registration jj cannot authenticate this working copy.
            if journal["phase"] not in {"rollback-intent", "workspace-forgotten", "removing"}:
                _mark_preserved(journals, tasks, journal)
                raise PreparationError("unregistered workspace destination is ambiguous; preserved")

    if journal["phase"] not in {"rollback-intent", "workspace-forgotten", "removing"}:
        _save_phase(journals, journal, "rollback-intent", failure_injector)
    if observed_registration is not None:
        journal["jj"]["registration_state"] = "forget-intent"
        journals.save(journal, expected_phase=journal["phase"])
        if failure_injector is not None:
            failure_injector("before-forget")
        adapter.forget_workspace(source, name)
        if failure_injector is not None:
            failure_injector("after-forget")
        journal["jj"]["registration_state"] = "absent-after-forget"
    if journal["phase"] == "rollback-intent":
        _save_phase(journals, journal, "workspace-forgotten", failure_injector)
    if journal["phase"] == "workspace-forgotten":
        _save_phase(journals, journal, "removing", failure_injector)
    try:
        if destination.exists() and journal["materialized_owned"] is not None:
            _remove_owned_tree(destination, journal, journals, failure_injector)
            _remove_workspace_root(destination, journal, journals, failure_injector)
        elif destination.exists() or destination.is_symlink():
            raise PreparationError("workspace ownership was never established; preserved")
        else:
            journal["removal"]["root_removed"] = True
            journals.save(journal, expected_phase=journal["phase"])
        _remove_created_parents(journal, journals, failure_injector)
    except PreparationError:
        _mark_preserved(journals, tasks, journal)
        raise
    except OSError as exc:
        if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
            raise
        _mark_preserved(journals, tasks, journal)
        raise PreparationError(
            "foreign data prevented owned workspace removal; preserved"
        ) from exc
    _fail_task(tasks, journals, journal)
    _save_phase(journals, journal, "rolled-back", failure_injector)


def rollback_prelaunch(
    config: ControlConfig, task_id: str, *, jj: JjAdapter | None = None,
    invocation_id: str | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> None:
    adapter = jj or JjAdapter()
    task_id = canonical_uuid(task_id)
    with TaskStore(config).transaction_lock(task_id):
        try:
            _rollback_locked(
                config, task_id, adapter, invocation_id=invocation_id,
                failure_injector=failure_injector,
            )
        except Exception as exc:
            if isinstance(exc, PreparationError):
                raise
            raise PreparationError(f"rollback interrupted with durable recovery state: {exc}") from exc


def prepare_task_workspace(
    config: ControlConfig, request: PrepareRequest, *, jj: JjAdapter | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    adapter = jj or JjAdapter()
    try:
        task_id = canonical_uuid(request.task_id)
        slug = validate_slug(request.slug)
    except ValueError as exc:
        raise PreparationError(str(exc)) from exc
    if slug == "materializations":
        raise PreparationError(
            "task slug 'materializations' is reserved for controller materializations"
        )
    if not request.label or len(request.label) > 200:
        raise PreparationError("task label must contain 1-200 characters")
    source = Path(request.repository)
    try:
        source_record = copy.deepcopy(request.source)
        if not isinstance(source_record, dict):
            raise ValueError("task source metadata must be an object")
        validate_workspace_root(config.workspace_root, home=config.home, repository=source)
        snapshot = read_published_snapshot(source)
        repository = adapter.preflight(source)
        repository_identity, repo_key = derive_repository_identity(
            snapshot.project_id, repository.root, repository.git_root
        )
        if request.resolved_base_commit_id is None:
            base_commit_id = adapter.resolve_base(source, request.requested_base)
        else:
            base_commit_id = request.resolved_base_commit_id
            if (
                not isinstance(base_commit_id, str)
                or GIT_OBJECT_ID_PATTERN.fullmatch(base_commit_id) is None
            ):
                raise ValueError("resolved base commit ID must be a full Git object ID")
            adapter.require_visible_commit(source, base_commit_id)
        operation_id = adapter.pin_operation(source)
        expected_materialization = adapter.expected_materialization(repository.git_root, base_commit_id)
    except (OSError, ValueError, JjError) as exc:
        raise PreparationError(f"preflight failed without task mutation: {exc}") from exc
    destination = config.workspace_root / repo_key / slug
    _validate_layout(config, source, destination, repo_key, slug)
    workspace_name = f"asha-{slug}-{task_id[:8]}"
    session_name = f"{config.session_prefix}{slug}-{task_id[:8]}"
    timestamp = _now()
    task = {
        "contract": TASK_CONTRACT, "task_id": task_id, "slug": slug, "label": request.label,
        "created_at": timestamp, "updated_at": timestamp, "lifecycle": "creating",
        "repository": {"root": str(source), "identity": repository_identity},
        "source": source_record,
        "jj": {
            "workspace_name": workspace_name, "workspace_path": str(destination),
            "requested_base": request.requested_base, "base_commit_id": base_commit_id,
            "change_id": None, "working_commit_id": None,
        },
        "tmux": {"socket": "default", "session": session_name, "window": "work"},
        "runs": [],
    }
    validate_task(task)
    invocation_id = secrets.token_hex(16)
    journal = {
        "contract": JOURNAL_CONTRACT, "task_id": task_id,
        "invocation_id": invocation_id, "phase": "intent", "launch_attempted": False,
        "config": {
            "workspace_root": str(config.workspace_root), "tasks_dir": str(config.tasks_dir),
            "runtime_dir": str(config.runtime_dir),
        },
        "repository": {
            "root": str(source), "identity": repository_identity,
            "git_root": str(repository.git_root), "repo_key": repo_key,
        },
        "task": {
            "record_path": str(config.tasks_dir / f"{task_id}.json"), "slug": slug,
            "label": request.label, "digest": None, "failure": None,
        },
        "workspace": {
            "path": str(destination), "name": workspace_name,
            "root_fact": None, "created_parents": [],
        },
        "jj": {
            "pinned_operation_id": operation_id, "base_commit_id": base_commit_id,
            "change_id": None, "working_commit_id": None, "description": request.label,
            "registration_state": "absent", "last_registration": None,
        },
        "expected_materialization": expected_materialization,
        "materialized_owned": None, "recovery_owned": None,
        "planned_context": None, "context_owned": {},
        "removal": {"entries_removed": 0, "root_removed": False, "parents_removed": 0},
    }
    capacity_marker = {
        "contract": "asha.control-task-context.v1", "task_id": task_id,
        "repository": task["repository"],
        "jj": {
            "workspace_name": workspace_name, "workspace_path": str(destination),
            "change_id": "k" * 32, "working_commit_id": "f" * 64,
        },
    }
    try:
        capacity_plan = _planned_manifest(build_context_plan(
            source, destination, capacity_marker, snapshot=snapshot,
        ))
        _ensure_creation_journal_capacity(journal, capacity_plan)
        missing_ancestors = _count_missing_destination_ancestors(
            config, source, destination, repo_key, slug,
        )
        if missing_ancestors > 8:
            raise PreparationError(
                "workspace destination requires more than eight created ancestors"
            )
    except (OSError, ValueError) as exc:
        raise PreparationError(f"preflight failed without task mutation: {exc}") from exc
    journals = CreationJournalStore(config)
    tasks = TaskStore(config)
    claimed = False
    repository_lock_id = _repository_lock_id(repository_identity)

    def phase(next_phase: str) -> None:
        _save_phase(journals, journal, next_phase, failure_injector)
        # Retain the original Increment 2 injection vocabulary as well.
        if failure_injector is not None:
            failure_injector(next_phase)

    try:
        # Task identity serializes recovery; repository identity separately
        # serializes jj registration and shared workspace-parent creation.
        # Neither lock holds the registry directory flock during those steps.
        with tasks.transaction_lock(task_id), tasks.transaction_lock(repository_lock_id):
            _validate_layout(config, source, destination, repo_key, slug)
            if destination.exists() or destination.is_symlink():
                raise PreparationError("workspace destination already exists; no task was created")
            journals.save(journal)
            claimed = True
            if failure_injector is not None:
                failure_injector("journal:intent")
            tasks.save(task)
            journal["task"]["digest"] = task_digest(task)
            phase("task-recorded")
            phase("parent-intent")

            def record_parent(item: dict[str, Any]) -> None:
                journal["workspace"]["created_parents"].append(item)
                journals.save(journal, expected_phase=journal["phase"])
                if failure_injector is not None:
                    failure_injector(f"parent-created:{item['path']}")

            _create_destination_parents(
                config, source, destination, repo_key, slug, record_parent,
            )
            phase("parent-ready")
            _validate_layout(config, source, destination, repo_key, slug)
            journal["jj"]["registration_state"] = "add-intent"
            phase("workspace-add-intent")
            try:
                adapter.add_workspace(
                    source, destination, workspace_name, base_commit_id,
                    request.label, operation_id,
                )
            except BaseException:
                # jj can register and materialize the workspace before returning
                # an error. Privatize only an exactly registered partial result;
                # an unregistered same-uid collision remains foreign and
                # byte-for-byte untouched.
                try:
                    _make_registered_workspace_private(
                        adapter, source, destination, workspace_name,
                        base_commit_id, request.label,
                    )
                except (OSError, JjError, PreparationError):
                    pass
                raise
            _make_workspace_private(destination)
            phase("workspace-added")
            identity = adapter.inspect_workspace(destination, workspace_name)
            if identity.parent_commit_ids != (base_commit_id,) or identity.description != request.label:
                raise PreparationError("created workspace is not the exact empty requested change")
            adapter.require_private_context_ignored(repository.git_root, destination)
            owned, root_fact = _verify_materialization(
                destination, source, expected_materialization
            )
            journal["workspace"]["root_fact"] = root_fact
            journal["materialized_owned"] = owned
            journal["jj"].update({
                "change_id": identity.change_id, "working_commit_id": identity.commit_id,
                "registration_state": "present",
                "last_registration": {
                    "change_id": identity.change_id, "working_commit_id": identity.commit_id,
                },
            })
            phase("workspace-recorded")
            marker = {
                "contract": "asha.control-task-context.v1", "task_id": task_id,
                "repository": task["repository"],
                "jj": {
                    "workspace_name": workspace_name, "workspace_path": str(destination),
                    "change_id": identity.change_id, "working_commit_id": identity.commit_id,
                },
            }
            plan = build_context_plan(source, destination, marker, snapshot=snapshot)
            journal["planned_context"] = _planned_manifest(plan)
            phase("context-intent")
            phase("context-provisioning")

            def record_context(relative: str, fact: dict[str, Any]) -> None:
                planned = journal["planned_context"][relative]
                projection = {
                    key: fact[key] for key in planned
                    if key in fact
                }
                if any(projection.get(key) != value for key, value in planned.items()):
                    raise PreparationError(f"created context differs from intent: {relative}")
                journal["context_owned"][relative] = {
                    key: fact[key] for key in ("dev", "ino", "mode", "uid")
                }
                journals.save(journal, expected_phase=journal["phase"])
                if failure_injector is not None:
                    failure_injector(f"context-owned:{relative}")

            provision_context(
                source, destination, marker, snapshot=snapshot, after_entry=record_context,
                after_file=(
                    (lambda relative: failure_injector(f"context-file:{relative}"))
                    if failure_injector is not None else None
                ),
            )
            actual, _ = _capture_tree(destination, root_fact)
            if actual != _owned_manifest(journal):
                raise PreparationError("post-context workspace ownership changed; preserving")
            final_identity = adapter.inspect_workspace(
                destination, workspace_name, snapshot=True, require_empty=True,
            )
            if final_identity != identity:
                raise PreparationError("task workspace jj identity changed during context provisioning")
            phase("context-provisioned")
            phase("task-identity-intent")
            task["jj"]["change_id"] = identity.change_id
            task["jj"]["working_commit_id"] = identity.commit_id
            current = tasks.read(task_id)
            tasks.save(task, expected_digest=task_digest(current))
            journal["task"]["digest"] = task_digest(task)
            phase("task-identity-recorded")
            phase("ready-for-launch")
            return task
    except BaseException as exc:
        if claimed:
            try:
                rollback_prelaunch(
                    config, task_id, jj=adapter, invocation_id=invocation_id,
                )
            except PreparationError:
                pass
        if not isinstance(exc, Exception):
            raise
        if not claimed:
            raise PreparationError(f"creation intent was not claimed; existing state preserved: {exc}") from exc
        raise PreparationError(f"workspace preparation failed with durable recovery state: {exc}") from exc


_MATERIALIZATION_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII)
_MATERIALIZATION_OWNER = ".asha-control-materializations.json"


def _materialization_journal(path: Path, value: dict[str, Any]) -> None:
    """Atomically retain one private controller-materialization phase."""
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    if len(raw) > 64 * 1024:
        raise PreparationError("materialization journal exceeds 65536 bytes")
    temporary = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    parent_fd = _open_absolute_directory(path.parent)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _private_child(parent: Path, name: str) -> Path:
    """Create or verify one owned 0700 child without following links."""
    parent_fd = _open_absolute_directory(parent)
    try:
        try:
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            os.fchmod(child_fd, 0o700)
            os.fsync(parent_fd)
        try:
            metadata = os.fstat(child_fd)
            if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                raise PreparationError(
                    f"materialization directory must be owned by the effective user with mode 0700: {parent / name}"
                )
        finally:
            os.close(child_fd)
    finally:
        os.close(parent_fd)
    return parent / name


def _materialization_owner(path: Path, repo_key: str, *, create: bool) -> None:
    """Create or authenticate the reserved per-repository namespace."""
    marker = path / _MATERIALIZATION_OWNER
    expected = {
        "contract": "asha.control-materialization-namespace.v1",
        "repository_key": repo_key,
    }
    if create:
        if marker.exists() or marker.is_symlink():
            raise PreparationError("materialization namespace marker already exists")
        _materialization_journal(marker, expected)
        return
    try:
        fd = os.open(
            marker,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except OSError as exc:
        raise PreparationError(
            "existing materializations path is not an authenticated controller namespace"
        ) from exc
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size > 4096
        ):
            raise PreparationError("materialization namespace marker is not private and regular")
        raw = os.read(fd, metadata.st_size + 1)
    finally:
        os.close(fd)
    try:
        actual = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PreparationError("materialization namespace marker is invalid") from exc
    if actual != expected:
        raise PreparationError("materialization namespace marker identity changed")


def plan_materialization(
    config: ControlConfig, source: Path, name: str, *, jj: JjAdapter | None = None,
) -> dict[str, str]:
    """Resolve the deterministic controller materialization identity without mutation."""
    adapter = jj or JjAdapter()
    if not isinstance(name, str) or _MATERIALIZATION_NAME.fullmatch(name) is None:
        raise PreparationError("materialization name uses an invalid restricted grammar")
    validate_workspace_root(config.workspace_root, home=config.home, repository=source)
    snapshot = read_published_snapshot(source)
    repository = adapter.preflight(source)
    repository_identity, repo_key = derive_repository_identity(
        snapshot.project_id, repository.root, repository.git_root,
    )
    destination = config.workspace_root / repo_key / "materializations" / name
    workspace_name = "asha-materialization-" + hashlib.sha256(
        f"{repo_key}\0{name}".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "repository_identity": repository_identity,
        "repository_key": repo_key,
        "workspace_name": workspace_name,
        "workspace_path": str(destination),
    }


def prepare_materialization(
    config: ControlConfig,
    source: Path,
    base_commit_id: str,
    name: str,
    *,
    jj: JjAdapter | None = None,
) -> dict[str, str]:
    """Create one retained explicit-base jj workspace without a Control task.

    The materialization is a controller-owned verification input. It registers
    no task, run, tmux session, harness, or task-context marker. Failures retain
    their journal and any exactly registered partial workspace for inspection;
    this seam never removes data.
    """
    adapter = jj or JjAdapter()
    source = Path(source)
    if not isinstance(name, str) or _MATERIALIZATION_NAME.fullmatch(name) is None:
        raise PreparationError("materialization name uses an invalid restricted grammar")
    try:
        target = plan_materialization(config, source, name, jj=adapter)
        repository_identity = target["repository_identity"]
        repo_key = target["repository_key"]
        destination = Path(target["workspace_path"])
        workspace_name = target["workspace_name"]
        repository = adapter.preflight(source)
        if (
            not isinstance(base_commit_id, str)
            or GIT_OBJECT_ID_PATTERN.fullmatch(base_commit_id) is None
        ):
            raise ValueError("base commit ID must be a full Git object ID")
        adapter.require_visible_commit(source, base_commit_id)
        operation_id = adapter.pin_operation(source)
        expected = adapter.expected_materialization(repository.git_root, base_commit_id)
    except (OSError, ValueError, JjError) as exc:
        raise PreparationError(f"materialization preflight failed without mutation: {exc}") from exc

    repository_parent = config.workspace_root / repo_key
    materializations = repository_parent / "materializations"
    lock_id = _repository_lock_id(repository_identity)

    with TaskStore(config).transaction_lock(lock_id):
        # Reuse task preparation's descriptor-relative parent creation for the
        # managed workspace root and repository namespace, then add only the
        # fixed materializations and journal components below it.
        probe = repository_parent / "materialization-parent-probe"
        _create_destination_parents(
            config, source, probe, repo_key, "materialization-parent-probe",
            lambda _item: None,
        )
        materializations_preexisting = (
            materializations.exists() or materializations.is_symlink()
        )
        materializations = _private_child(repository_parent, "materializations")
        _materialization_owner(
            materializations, repo_key, create=not materializations_preexisting,
        )
        journals = _private_child(materializations, ".journals")
        journal_path = journals / f"{name}.json"
        if (
            destination.exists() or destination.is_symlink()
            or journal_path.exists() or journal_path.is_symlink()
        ):
            raise PreparationError("materialization destination or journal already exists; retained state was not changed")
        at = _now()
        journal: dict[str, Any] = {
            "contract": "asha.control-materialization-journal.v1",
            "name": name,
            "source": str(source),
            "base_commit_id": base_commit_id,
            "workspace_name": workspace_name,
            "workspace_path": str(destination),
            "pinned_operation_id": operation_id,
            "phase": "intent",
            "change_id": None,
            "working_commit_id": None,
            "error": None,
            "created_at": at,
            "updated_at": at,
        }
        _materialization_journal(journal_path, journal)
        try:
            journal.update({"phase": "workspace-add-intent", "updated_at": _now()})
            _materialization_journal(journal_path, journal)
            description = f"controller materialization {name}"
            try:
                adapter.add_workspace(
                    source, destination, workspace_name, base_commit_id,
                    description, operation_id,
                )
            except BaseException:
                # jj can register and materialize before returning an error.
                # Preserve that exact partial result, but do not leave its
                # workspace root more permissive than a successful result.
                try:
                    _make_registered_workspace_private(
                        adapter, source, destination, workspace_name,
                        base_commit_id, description,
                    )
                except (OSError, JjError, PreparationError):
                    pass
                raise
            _make_workspace_private(destination)
            identity = adapter.inspect_workspace(destination, workspace_name)
            if (
                identity.parent_commit_ids != (base_commit_id,)
                or identity.description != description
            ):
                raise PreparationError("created materialization is not the exact empty requested change")
            _verify_materialization(destination, source, expected)
            final = adapter.inspect_workspace(
                destination, workspace_name, snapshot=False, require_empty=True,
            )
            if final != identity:
                raise PreparationError("materialization jj identity changed during verification")
            journal.update({
                "phase": "ready",
                "change_id": identity.change_id,
                "working_commit_id": identity.commit_id,
                "updated_at": _now(),
            })
            _materialization_journal(journal_path, journal)
            return {
                "workspace_name": workspace_name,
                "workspace_path": str(destination),
                "change_id": identity.change_id,
                "working_commit_id": identity.commit_id,
            }
        except BaseException as exc:
            journal.update({
                "phase": "preserved",
                "error": str(exc)[:2048],
                "updated_at": _now(),
            })
            try:
                _materialization_journal(journal_path, journal)
            except Exception:
                pass
            raise
