"""Reclaim tmux sessions and jj workspaces left behind by archived tasks."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ControlConfig
from .jj import JjAdapter, JjError
from .model import canonical_uuid
from .prepare import _DIRECTORY_FLAGS, PreparationError, _open_absolute_directory
from .store import StoreError, TaskStore
from .tmux import TmuxAdapter, TmuxError
from .transaction import CreationJournalStore, JournalError

PRUNE_CONTRACT = "asha.control-task-prune.v1"
PRUNE_RECORD_CONTRACT = "asha.control-prune-record.v1"

_MAX_TREE_DEPTH = 128
_MAX_MARKER_BYTES = 64 * 1024
_MOUNTINFO = Path("/proc/self/mountinfo")
_FILE_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


class PruneError(Exception):
    """Prune refused or could not proceed safely."""


@dataclass
class ArtifactOutcome:
    action: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"action": self.action, "detail": self.detail}


@dataclass
class PruneResult:
    task_id: str
    slug: str
    outcome: str
    session: ArtifactOutcome
    workspace: ArtifactOutcome
    bindings: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "slug": self.slug,
            "outcome": self.outcome,
            "session": self.session.as_dict(),
            "workspace": self.workspace.as_dict(),
            "bindings": list(self.bindings),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def orchestration_bindings(env: dict[str, str] | None = None) -> dict[str, list[dict[str, str]]]:
    """Map Control task ids to orchestration attempts that are not terminal.

    An archived task whose attempt has not reached a terminal state may still be
    sealed from its workspace, so prune keeps that workspace.  Any failure to
    read orchestration state is reported as a refusal by the caller, never as
    an empty binding set.
    """
    # Local imports keep ordinary Control commands independent of the
    # orchestration package unless prune actually needs it.
    from .orchestration.config import OrchestrationConfigError, load_config
    from .orchestration.model import ATTEMPT_TERMINAL_STATES
    from .orchestration.store import InitiativeStore
    from .orchestration.store import StoreError as InitiativeStoreError

    try:
        config = load_config(env)
    except OrchestrationConfigError as exc:
        raise PruneError(f"orchestration configuration unreadable: {exc}") from exc
    if not config.initiatives_dir.exists():
        return {}
    store = InitiativeStore(config)
    bindings: dict[str, list[dict[str, str]]] = {}
    try:
        initiatives = store.list_initiatives()
        for initiative in initiatives:
            initiative_id = initiative["initiative_id"]
            attempts = {
                attempt["attempt_id"]: attempt
                for attempt in store.list_attempts_snapshot(initiative_id)
            }
            seen: set[tuple[str, str]] = set()

            def bind(task_id: Any, attempt_id: str, state: str) -> None:
                if not isinstance(task_id, str) or (task_id, attempt_id) in seen:
                    return
                seen.add((task_id, attempt_id))
                bindings.setdefault(task_id, []).append({
                    "initiative_id": initiative_id,
                    "attempt_id": attempt_id,
                    "state": state,
                })

            # Links are the durable binding, but an attempt reserves its task id
            # before the link is written; a dispatch interrupted between the two
            # leaves a link-less non-terminal attempt that a later replay re-links.
            for attempt in attempts.values():
                if attempt["state"] not in ATTEMPT_TERMINAL_STATES:
                    bind(attempt.get("task_id"), attempt["attempt_id"], attempt["state"])
            for link in store.list_links_snapshot(initiative_id):
                attempt = attempts.get(link["attempt_id"])
                state = "unknown" if attempt is None else attempt["state"]
                if state in ATTEMPT_TERMINAL_STATES:
                    continue
                bind(link["control_task_id"], link["attempt_id"], state)
    except (InitiativeStoreError, OSError, ValueError, KeyError) as exc:
        raise PruneError(f"orchestration state unreadable: {exc}") from exc
    if store.skipped:
        raise PruneError(
            f"{len(store.skipped)} orchestration initiative record(s) unreadable"
        )
    return bindings


class PruneRecordStore:
    """Durable per-task facts about what prune already reclaimed.

    A pruned workspace root is recorded so that a later pass never re-matches
    a reused inode at the same path (a successor task's live workspace).
    """

    def __init__(self, config: ControlConfig) -> None:
        self.directory = config.tasks_dir.parent / "prunes"

    def path(self, task_id: str) -> Path:
        return self.directory / f"{canonical_uuid(task_id)}.json"

    def read(self, task_id: str) -> dict[str, Any] | None:
        try:
            fd = os.open(self.path(task_id), _FILE_FLAGS)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PruneError(f"prune record unreadable: {exc}") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_MARKER_BYTES:
                raise PruneError("prune record is not a bounded regular file")
            raw = os.read(fd, _MAX_MARKER_BYTES + 1)
        except OSError as exc:
            raise PruneError(f"prune record unreadable: {exc}") from exc
        finally:
            os.close(fd)
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise PruneError(f"prune record is malformed: {exc}") from exc
        if (not isinstance(value, dict) or value.get("contract") != PRUNE_RECORD_CONTRACT
                or value.get("task_id") != canonical_uuid(task_id)):
            raise PruneError("prune record does not describe this task")
        return value

    def write(self, task_id: str, facts: dict[str, Any]) -> None:
        record = {
            "contract": PRUNE_RECORD_CONTRACT,
            "task_id": canonical_uuid(task_id),
            "recorded_at": _now(),
            **facts,
        }
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = self.path(task_id)
            temporary = target.with_name(target.name + ".tmp")
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.write(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, target)
            directory_fd = os.open(self.directory, _DIRECTORY_FLAGS)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            raise PruneError(f"prune record could not be written: {exc}") from exc


def _marker_task_id(path: Path) -> str | None:
    """Read the workspace's own Control marker without following links.

    Returns the marker's task id, or None when the marker is absent.  Any other
    problem raises: an unreadable marker cannot prove ownership.
    """
    try:
        parent_fd = _open_absolute_directory(path)
    except PreparationError as exc:
        raise PruneError(str(exc)) from exc
    try:
        try:
            asha_fd = os.open(".asha", _DIRECTORY_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            return None
        try:
            try:
                marker_fd = os.open("control-task.json", _FILE_FLAGS, dir_fd=asha_fd)
            except FileNotFoundError:
                return None
            try:
                metadata = os.fstat(marker_fd)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_MARKER_BYTES:
                    raise PruneError("workspace marker is not a bounded regular file")
                raw = os.read(marker_fd, _MAX_MARKER_BYTES + 1)
            finally:
                os.close(marker_fd)
        finally:
            os.close(asha_fd)
    except OSError as exc:
        raise PruneError(f"workspace marker unreadable: {exc}") from exc
    finally:
        os.close(parent_fd)
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise PruneError(f"workspace marker is malformed: {exc}") from exc
    task_id = value.get("task_id") if isinstance(value, dict) else None
    if not isinstance(task_id, str):
        raise PruneError("workspace marker names no task")
    return task_id


def _mount_points_below(path: Path) -> list[str]:
    try:
        text = _MOUNTINFO.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    prefix = str(path).rstrip("/") + "/"
    found: list[str] = []
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) < 5:
            continue
        mount = fields[4].replace("\\040", " ").replace("\\011", "\t").replace(
            "\\012", "\n").replace("\\134", "\\")
        if mount == str(path) or mount.startswith(prefix):
            found.append(mount)
    return found


def _open_owned_root(path: Path, root_fact: dict[str, int]) -> tuple[int, os.stat_result]:
    """Open the workspace parent and verify the journaled root without deleting.

    Returns the parent descriptor (caller closes it) and the root's metadata.
    """
    mounts = _mount_points_below(path)
    if mounts:
        raise PruneError(f"workspace contains {len(mounts)} mount point(s); preserved")
    try:
        parent_fd = _open_absolute_directory(path.parent)
    except PreparationError as exc:
        raise PruneError(f"{exc}; preserved") from exc
    try:
        metadata = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode):
            raise PruneError("workspace path is not a directory; preserved")
        actual = (metadata.st_dev, metadata.st_ino, metadata.st_uid)
        expected = (root_fact["dev"], root_fact["ino"], root_fact["uid"])
        if actual != expected:
            raise PruneError("workspace root identity differs from the creation journal; preserved")
        if metadata.st_uid != os.geteuid():
            raise PruneError("workspace root is not owned by the effective user; preserved")
        return parent_fd, metadata
    except OSError as exc:
        os.close(parent_fd)
        raise PruneError(f"workspace root unavailable: {exc}; preserved") from exc
    except PruneError:
        os.close(parent_fd)
        raise


def verify_owned_root(path: Path, root_fact: dict[str, int]) -> None:
    """Preflight the removal checks so forget never precedes a doomed removal."""
    parent_fd, _metadata = _open_owned_root(path, root_fact)
    os.close(parent_fd)


def _remove_owned_tree(path: Path, root_fact: dict[str, int]) -> int:
    """Remove a workspace tree whose root inode Control created and journaled.

    Every step is anchored to file descriptors, never follows symlinks, refuses
    to cross devices or ownership, and refuses loops and mount points.  Any
    doubt raises before the first unlink and preserves the tree.
    """
    parent_fd, metadata = _open_owned_root(path, root_fact)
    try:
        name = path.name
        actual = (metadata.st_dev, metadata.st_ino, metadata.st_uid)
        root_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        try:
            confirmed = os.fstat(root_fd)
            if (confirmed.st_dev, confirmed.st_ino) != actual[:2]:
                raise PruneError("workspace root changed while opening; preserved")
            removed = _empty_directory(
                root_fd, device=metadata.st_dev, uid=metadata.st_uid, depth=0,
                ancestors=frozenset({(metadata.st_dev, metadata.st_ino)}),
            )
        finally:
            os.close(root_fd)
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        return removed + 1
    except OSError as exc:
        raise PruneError(f"workspace removal failed: {exc}") from exc
    finally:
        os.close(parent_fd)


def _empty_directory(
    fd: int, *, device: int, uid: int, depth: int, ancestors: frozenset[tuple[int, int]],
) -> int:
    if depth > _MAX_TREE_DEPTH:
        raise PruneError("workspace tree is deeper than the prune bound; preserved")
    with os.scandir(fd) as iterator:
        entries = [(entry.name, entry.stat(follow_symlinks=False)) for entry in iterator]
    removed = 0
    for name, metadata in entries:
        if stat.S_ISDIR(metadata.st_mode):
            identity = (metadata.st_dev, metadata.st_ino)
            if metadata.st_dev != device:
                raise PruneError("workspace tree crosses a device boundary; preserved")
            if metadata.st_uid != uid:
                raise PruneError("workspace tree contains a foreign-owned directory; preserved")
            if identity in ancestors:
                raise PruneError("workspace tree loops onto an ancestor; preserved")
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
            try:
                confirmed = os.fstat(child)
                if (confirmed.st_dev, confirmed.st_ino) != identity:
                    raise PruneError("workspace entry changed during removal; preserved")
                removed += _empty_directory(
                    child, device=device, uid=uid, depth=depth + 1,
                    ancestors=ancestors | {identity},
                )
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=fd)
        else:
            os.unlink(name, dir_fd=fd)
        removed += 1
    return removed


def _workspace_path_owned(config: ControlConfig, path: Path) -> bool:
    root = str(config.workspace_root).rstrip("/") + "/"
    text = str(path)
    return (
        path.is_absolute()
        and os.path.normpath(text) == text
        and text.startswith(root)
        and len(text) > len(root)
    )


def _session_step(
    task: dict[str, Any], tmux: TmuxAdapter, *, dry_run: bool,
) -> tuple[ArtifactOutcome, bool]:
    """Return the session outcome and whether a live pane blocks pruning."""
    session = task["tmux"]["session"]
    try:
        if not tmux.has_session(session):
            return ArtifactOutcome("absent", "no tmux session"), False
        managed = tmux.session_option(session, "@asha_managed")
        owner = tmux.session_option(session, "@asha_task_id")
        if managed != "1" or owner != task["task_id"]:
            return ArtifactOutcome(
                "refused", "session exists but is not owned by this task; kept",
            ), False
        states = tmux.session_pane_states(session)
    except (TmuxError, ValueError) as exc:
        return ArtifactOutcome("refused", f"tmux state unavailable: {exc}"), True
    live = sum(1 for dead in states if not dead)
    if live:
        return ArtifactOutcome(
            "refused", f"{live} pane(s) still live; unarchive and stop first",
        ), True
    if dry_run:
        return ArtifactOutcome("would-kill", f"{len(states)} dead pane(s)"), False
    try:
        tmux.kill_session(session)
    except (TmuxError, ValueError) as exc:
        return ArtifactOutcome("refused", f"kill-session failed: {exc}"), False
    return ArtifactOutcome("killed", f"{len(states)} dead pane(s)"), False


def _reclaimed(records: PruneRecordStore, task_id: str) -> bool:
    try:
        record = records.read(task_id)
    except PruneError:
        return False
    return bool(record and record.get("workspace_removed"))


def _live_co_owners(
    records: PruneRecordStore, owners: dict[str, set[str]], path: Path, task_id: str,
) -> set[str]:
    """Other registry records claiming this path whose own root was not reclaimed."""
    return {
        other for other in owners.get(str(path), set()) - {task_id}
        if not _reclaimed(records, other)
    }


def _workspace_step(
    config: ControlConfig,
    task: dict[str, Any],
    *,
    tasks: TaskStore,
    journals: CreationJournalStore,
    jj: JjAdapter,
    bindings: dict[str, list[dict[str, str]]],
    bindings_error: str | None,
    dry_run: bool,
    records: PruneRecordStore,
    path_owners: dict[str, set[str]],
) -> ArtifactOutcome:
    task_id = task["task_id"]
    path = Path(task["jj"]["workspace_path"])
    name = task["jj"]["workspace_name"]
    if bindings_error is not None:
        return ArtifactOutcome("refused", f"{bindings_error}; kept")
    try:
        record = records.read(task_id)
    except PruneError as exc:
        return ArtifactOutcome("refused", f"{exc}; kept")
    if record is not None and record.get("workspace_removed"):
        # Already reclaimed by an earlier pass: whatever sits at this path now
        # belongs to someone else, even when the inode number repeats.
        return ArtifactOutcome("absent", "workspace already reclaimed by an earlier prune")
    removal_started = record is not None and record.get("workspace_removed") is False
    # A predecessor whose root prune already reclaimed cannot hold this inode;
    # only co-owners without a completed prune record count.
    others = _live_co_owners(records, path_owners, path, task_id)
    if others:
        # A successor whose own marker claims the directory is not this task's
        # residue; report it as gone rather than refusing forever.
        try:
            holder = _marker_task_id(path) if path.is_dir() and not path.is_symlink() else None
        except PruneError:
            holder = None
        if holder in others:
            return ArtifactOutcome("absent", f"workspace path now held by task {holder}")
        return ArtifactOutcome(
            "refused",
            f"workspace path is also recorded by task {sorted(others)[0]}; kept",
        )
    if bindings.get(task_id):
        bound = bindings[task_id][0]
        return ArtifactOutcome(
            "refused",
            "bound to non-terminal orchestration attempt "
            f"{bound['attempt_id']} ({bound['state']}) of initiative "
            f"{bound['initiative_id']}; kept",
        )
    try:
        journal = journals.read(task_id)
    except (JournalError, StoreError, OSError, ValueError) as exc:
        return ArtifactOutcome("refused", f"creation journal unavailable: {exc}; kept")
    root_fact = journal["workspace"]["root_fact"]
    if journal["workspace"]["path"] != str(path) or root_fact is None:
        return ArtifactOutcome("refused", "creation journal does not own this workspace root; kept")
    if not _workspace_path_owned(config, path):
        return ArtifactOutcome("refused", "workspace path lies outside control.workspace_root; kept")
    try:
        if path.is_symlink():
            return ArtifactOutcome("refused", "workspace path is a symlink; kept")
        exists = path.is_dir()
    except OSError as exc:
        return ArtifactOutcome("refused", f"workspace state unavailable: {exc}; kept")
    if exists and os.path.realpath(path) != str(path):
        return ArtifactOutcome("refused", "workspace path traverses a symlink; kept")
    if exists:
        try:
            verify_owned_root(path, root_fact)
            marker = _marker_task_id(path)
        except PruneError as exc:
            return ArtifactOutcome("refused", f"{exc}; jj registration kept")
        if marker is None and removal_started:
            # An absent marker is acceptable only when this task's own removal
            # already began (journaled intent + matching root inode): the walk
            # may have unlinked .asha before an entry refused.  Re-list the
            # registry under the task lock so a successor registered after the
            # batch snapshot (its record precedes its directory) is seen.
            try:
                fresh = workspace_path_owners(tasks)
            except StoreError as exc:
                return ArtifactOutcome("refused", f"registry unavailable: {exc}; kept")
            if _live_co_owners(records, fresh, path, task_id):
                return ArtifactOutcome(
                    "refused", "workspace path claimed by a newer task; kept",
                )
        elif marker != task_id:
            owner = "no task" if marker is None else f"task {marker}"
            return ArtifactOutcome(
                "refused", f"workspace marker names {owner}, not this task; kept",
            )

    source = Path(task["repository"]["root"])
    forget = "not-registered"
    try:
        registered = name in jj.workspace_identities(source)
    except (JjError, OSError, ValueError) as exc:
        return ArtifactOutcome("refused", f"source repository unavailable for workspace forget: {exc}; kept")
    if registered:
        forget = "would-forget" if dry_run else "forgotten"
        if not dry_run:
            try:
                jj.forget_workspace(source, name)
            except (JjError, OSError, ValueError) as exc:
                return ArtifactOutcome("refused", f"jj workspace forget failed: {exc}; kept")
    if not exists:
        action = "absent" if forget == "not-registered" else forget
        return ArtifactOutcome(action, f"no workspace directory; jj registration {forget}")
    if dry_run:
        return ArtifactOutcome("would-remove", f"jj registration {forget}")
    if not removal_started:
        # Durable removal intent before the first unlink, so a walk that stops
        # midway (permissions, SIGKILL) can be finished by the next pass.
        try:
            records.write(task_id, {
                "workspace_removed": False,
                "workspace_path": str(path),
                "workspace_name": name,
                "root_fact": {key: root_fact[key] for key in ("dev", "ino", "uid")},
                "entries_removed": 0,
            })
        except PruneError as exc:
            return ArtifactOutcome("refused", f"{exc}; jj registration {forget}")
    try:
        entries = _remove_owned_tree(path, root_fact)
    except PruneError as exc:
        return ArtifactOutcome("refused", f"{exc}; jj registration {forget}")
    try:
        records.write(task_id, {
            "workspace_removed": True,
            "workspace_path": str(path),
            "workspace_name": name,
            "root_fact": {key: root_fact[key] for key in ("dev", "ino", "uid")},
            "entries_removed": entries,
        })
    except PruneError as exc:
        return ArtifactOutcome(
            "removed",
            f"{entries} entries; jj registration {forget}; WARNING {exc}",
        )
    return ArtifactOutcome("removed", f"{entries} entries; jj registration {forget}")


def prune_task(
    config: ControlConfig,
    task: dict[str, Any],
    *,
    tasks: TaskStore,
    journals: CreationJournalStore,
    tmux: TmuxAdapter,
    jj: JjAdapter,
    bindings: dict[str, list[dict[str, str]]],
    bindings_error: str | None = None,
    remove_workspace: bool = True,
    dry_run: bool = False,
    records: PruneRecordStore | None = None,
    path_owners: dict[str, set[str]] | None = None,
) -> PruneResult:
    """Kill the dead tmux session and remove the workspace of one archived task.

    The task record is never modified: prune changes only external state that
    the record already describes.  Repeating it is safe: live state is
    re-derived each pass and a reclaimed workspace root is remembered so a
    successor task at the same path is never mistaken for the pruned one.
    """
    task_id = canonical_uuid(task["task_id"])
    record_store = records if records is not None else PruneRecordStore(config)
    if path_owners is not None:
        owners = path_owners
    elif remove_workspace:
        owners = workspace_path_owners(tasks)
    else:
        owners = {}
    with tasks.transaction_lock(task_id):
        current = tasks.read(task_id)
        slug = current["slug"]
        if current["lifecycle"] != "archived":
            return PruneResult(
                task_id, slug, "refused",
                ArtifactOutcome("kept", f"task lifecycle is {current['lifecycle']}"),
                ArtifactOutcome("kept", "only an archived task can be pruned"),
                bindings.get(task_id, []),
            )
        session, blocked = _session_step(current, tmux, dry_run=dry_run)
        if blocked:
            return PruneResult(
                task_id, slug, "refused", session,
                ArtifactOutcome("kept", "session state blocks pruning"),
                bindings.get(task_id, []),
            )
        if remove_workspace:
            workspace = _workspace_step(
                config, current, tasks=tasks, journals=journals, jj=jj,
                bindings=bindings, bindings_error=bindings_error, dry_run=dry_run,
                records=record_store, path_owners=owners,
            )
        else:
            workspace = ArtifactOutcome("kept", "--keep-workspace")
        actions = {session.action, workspace.action}
        done = actions & {"killed", "removed", "forgotten"}
        planned = actions & {"would-kill", "would-remove", "would-forget"}
        if "refused" in actions:
            outcome = "partial" if done or planned else "refused"
        elif done:
            outcome = "pruned"
        elif planned:
            outcome = "planned"
        else:
            outcome = "nothing-to-prune"
        return PruneResult(task_id, slug, outcome, session, workspace, bindings.get(task_id, []))


def workspace_path_owners(tasks: TaskStore) -> dict[str, set[str]]:
    """Every task id recorded against each workspace path in the registry."""
    owners: dict[str, set[str]] = {}
    for record in tasks.list():
        owners.setdefault(record["jj"]["workspace_path"], set()).add(record["task_id"])
    return owners


def prunable_summary(
    config: ControlConfig, *, tasks: TaskStore, tmux_for_socket,
    records: PruneRecordStore | None = None,
) -> dict[str, int]:
    """Count archived tasks that still hold a tmux session or workspace.

    Sessions are enumerated once per tmux socket.  A workspace path that a
    prune record already reclaimed, or that another task record also claims,
    is not counted: whatever exists there is not this task's residue.
    """
    record_store = records if records is not None else PruneRecordStore(config)
    listed = tasks.list()
    owners: dict[str, set[str]] = {}
    for record in listed:
        owners.setdefault(record["jj"]["workspace_path"], set()).add(record["task_id"])
    names_by_socket: dict[str, set[str] | None] = {}
    sessions = 0
    workspaces = 0
    tasks_holding = 0
    for record in listed:
        if record["lifecycle"] != "archived":
            continue
        holds = False
        socket = record["tmux"]["socket"]
        if socket not in names_by_socket:
            try:
                names_by_socket[socket] = set(tmux_for_socket(socket).session_names())
            except (TmuxError, ValueError, OSError):
                names_by_socket[socket] = None
        names = names_by_socket[socket]
        if names is not None and record["tmux"]["session"] in names:
            sessions += 1
            holds = True
        path = Path(record["jj"]["workspace_path"])
        try:
            reclaimed = record_store.read(record["task_id"])
        except PruneError:
            reclaimed = None
        shared = bool(_live_co_owners(record_store, owners, path, record["task_id"]))
        already = bool(reclaimed and reclaimed.get("workspace_removed"))
        try:
            present = path.is_dir() and not path.is_symlink()
        except OSError:
            present = False
        if present and not shared and not already:
            workspaces += 1
            holds = True
        if holds:
            tasks_holding += 1
    return {"tasks": tasks_holding, "sessions": sessions, "workspaces": workspaces}
