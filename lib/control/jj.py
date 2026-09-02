"""Argument-vector-only adapter for the pinned jj 0.38 workspace seam."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import tempfile
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence, Any
from urllib.parse import urlsplit

from .config import ControlConfig
from .context import ContextError, validate_reusable_context_blob
from .process import bounded_process, capture_bytes, checked_bytes
from .store import (
    StoreError, TransactionCoordinator, _close_quietly, _directory_fd, _managed_start,
    _open_existing_file,
)


_COMMIT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_GIT_SHA1 = re.compile(r"[0-9a-f]{40}", re.ASCII)
_GIT_BRANCH_REF = re.compile(r"refs/heads/[^\s\x00-\x1f\x7f]+", re.ASCII)
_GIT_REMOTE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", re.ASCII)
_CHANGE_ID = re.compile(r"[k-z]{32}", re.ASCII)
_OPERATION_ID = re.compile(r"[0-9a-f]{128}", re.ASCII)
# `jj resolve --list` prints "<path><alignment padding><N-sided conflict ...>".
# A path may contain spaces, so the descriptor column is stripped from the end
# rather than the path being split off the front.
_CONFLICT_DESCRIPTOR = re.compile(
    r" +[0-9]+-sided conflict(?: including [^\n]*)?\Z", re.ASCII,
)
MAX_OUTPUT_BYTES = 64 * 1024
MAX_TREE_LIST_BYTES = 512 * 1024
MAX_GIT_SEMANTIC_BYTES = 16 * 1024 * 1024
MAX_TRACKED_BLOB_BYTES = 16 * 1024 * 1024
MAX_TRACKED_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MATERIALIZATION_ENTRIES = 1024
MAX_IMMUTABLE_TREE_BYTES = 64 * 1024 * 1024
MAX_IMMUTABLE_TREE_ENTRIES = 200000
COLOCATION_INTENT_CONTRACT = "asha.control-colocation-intent.v1"
MAX_COLOCATION_INTENT_BYTES = 4096
MAX_GIT_MARKER_BYTES = 4096
MAX_GIT_CONFIG_BYTES = 1024 * 1024
MAX_CONTEXT_PROOF_PATHS = 64
MAX_CONTEXT_PROOF_PATH_BYTES = 16 * 1024
MAX_EXACT_GIT_REF_BYTES = 300
MAX_WORKSPACE_BASE_COMMITS = 8
MAX_WORKSPACE_CONFLICT_PATHS = 32
MAX_WORKSPACE_CONFLICT_BYTES = 64 * 1024
MAX_BASELINE_DIVERGENCE_COMMITS = 5
MAX_BASELINE_DIVERGENCE_SUMMARY = 120
MAX_BASELINE_DIVERGENCE_BYTES = 64 * 1024
MAX_REMOTE_BOOKMARKS = 1024
MAX_REMOTE_BOOKMARKS_DISPLAYED = 8
MAX_REMOTE_BOOKMARK_NAME_BYTES = 300
TRUSTED_GIT_EXECUTABLE = "/usr/bin/git"
TRUSTED_SSH_EXECUTABLE = "/usr/bin/ssh"
_EXACT_GIT_CONFIG = (
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.external=",
    "-c", "interactive.diffFilter=",
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.file.allow=never",
    "-c", "credential.helper=",
    "-c", "core.excludesFile=/dev/null",
)


def _strict_intent_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise JjError("colocation intent contains duplicate JSON keys")
        value[key] = item
    return value


# Durable v1 identity for a request that omitted --base. New starts resolve the
# actual default through exact Git; retaining this text preserves task replay
# and caller-ID compatibility. An operator who explicitly supplied this exact
# legacy expression remains indistinguishable from omission in v1 records.
DEFAULT_BASE_REVSET = (
    "coalesce(trunk() ~ root(), present(main), present(master), present(trunk))"
)
_DEFAULT_BASE_UNRESOLVED = (
    "the default base resolved to the empty root commit; this repository has "
    "neither a remote trunk nor a local main, master, or trunk bookmark. Pass "
    "an explicit --base naming a bookmark or commit, for example --base main."
)


class JjError(ValueError):
    """A jj precondition, invocation, or identity check failed."""


# jj 0.38 `workspace add` accepts a repeated `--revision`: the working-copy
# commit is created with all of them as parents, exactly as `jj new r1 r2`. A
# merge materialization is the only way to prove that two divergent seals in
# one repository actually compose, so both the add and its operation-ancestry
# proof build their argv here and can never drift apart.
def _workspace_base_commit_ids(value: str | Sequence[str], label: str) -> tuple[str, ...]:
    ids = (value,) if isinstance(value, str) else tuple(value)
    if not 1 <= len(ids) <= MAX_WORKSPACE_BASE_COMMITS:
        raise JjError(
            f"{label} requires 1-{MAX_WORKSPACE_BASE_COMMITS} full base commit IDs"
        )
    if any(not isinstance(item, str) or _COMMIT_ID.fullmatch(item) is None for item in ids):
        raise JjError(f"{label} requires a full commit ID")
    if len(set(ids)) != len(ids):
        raise JjError(f"{label} refuses a repeated base commit ID")
    return ids


def _workspace_add_argv(
    source: Path, destination: Path, name: str,
    base_commit_ids: tuple[str, ...], message: str, operation_id: str,
) -> list[str]:
    argv = [
        "-R", str(source), "--at-operation", operation_id,
        "workspace", "add", "--name", name,
    ]
    for base_commit_id in base_commit_ids:
        argv.extend(["--revision", base_commit_id])
    argv.extend(["--message", message, str(destination)])
    return argv


def _exact_ascii_line(raw: bytes, label: str) -> str:
    """Decode exactly one unpadded ASCII line, with one optional final LF."""
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise JjError(f"{label} returned non-ASCII output") from exc
    if value.endswith("\n"):
        value = value[:-1]
    if not value or "\n" in value or "\r" in value:
        raise JjError(f"{label} was not exactly one unpadded line")
    return value


def _valid_exact_git_ref(value: str, *, namespace: str) -> bool:
    """Validate complete Git ref grammar without repository-configured code."""
    if (
        not isinstance(value, str)
        or not value.startswith(namespace)
        or not 1 <= len(value) <= MAX_EXACT_GIT_REF_BYTES
        or value == "@"
        or value.endswith("/")
        or value.endswith(".")
        or "//" in value
        or ".." in value
        or "@{" in value
        or any(
            ord(character) < 32
            or ord(character) >= 127
            or character in " ~^:?*[\\"
            for character in value
        )
    ):
        return False
    components = value.split("/")
    return bool(
        len(components) >= 3
        and all(
            component
            and not component.startswith(".")
            and not component.endswith(".lock")
            for component in components
        )
    )


class LinkedGitWorktreeError(JjError):
    """Automatic colocation was requested for a linked Git worktree."""


@dataclass(frozen=True)
class RepositoryFacts:
    root: Path
    git_root: Path


@dataclass(frozen=True)
class DefaultBaseResolution:
    """One read-only omitted-base decision from exact Git facts."""

    references: tuple[str, ...]
    commit_id: str
    tier: str


@dataclass(frozen=True)
class BaselineDivergence:
    """Bounded advisory evidence that landed commits sit above one baseline.

    Display-only. No baseline is ever re-selected from these facts: silently
    moving the base to a newer commit would be far more dangerous than the
    stale plan this evidence exists to warn about (#81).
    """

    reference: str
    baseline_commit_id: str
    working_copy_parent_commit_id: str
    ahead_count: int
    commits: tuple[tuple[str, str], ...]

    def warning(self) -> str:
        """Render the bounded one-line operator warning."""
        plural = "commit" if self.ahead_count == 1 else "commits"
        if not self.commits:
            shown = "(commit summaries unavailable)"
        else:
            shown = "; ".join(
                f"{commit_id[:12]} {summary}"
                for commit_id, summary in self.commits
            )
            remaining = self.ahead_count - len(self.commits)
            if remaining > 0:
                shown += f"; and {remaining} more"
        return (
            f"baseline {self.reference} is {self.ahead_count} {plural} behind "
            f"the working copy; plans will not see: {shown}"
        )

    def record(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "baseline_commit_id": self.baseline_commit_id,
            "working_copy_parent_commit_id": self.working_copy_parent_commit_id,
            "ahead_count": self.ahead_count,
            "commits": [
                {"commit_id": commit_id, "summary": summary}
                for commit_id, summary in self.commits
            ],
            "warning": self.warning(),
        }


@dataclass(frozen=True)
class WorkspaceIdentity:
    name: str
    change_id: str
    commit_id: str
    parent_commit_ids: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class DiffSummary:
    """Bounded display-only output from an explicit working-copy refresh."""

    summary: str
    refreshed_at: str


@dataclass(frozen=True)
class ImmutableTree:
    """Exact read-only Git-tree identity for one jj commit."""

    commit_id: str
    digest: str
    entries: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class GitSemanticState:
    """Git-visible source state, excluding jj's private metadata.

    jj may rewrite raw index bookkeeping while colocating.  These facts cover
    the operator-visible contract instead: HEAD/branch, semantic index entries
    with their normalized flags, ref object IDs and symbolic targets, and raw
    tracked/untracked filesystem identity.  Clean tracked content is bound
    without rereading it when its lstat fields still match Git's index cache;
    changed paths are hashed directly without invoking Git attributes or
    repository-configured filters.
    """

    head: tuple[int, bytes]
    branch: tuple[int, bytes]
    index: bytes
    index_flags: tuple[tuple[str, int, int], ...]
    refs: tuple[bytes, ...]
    paths: tuple[tuple[str, int, str, int], ...]
    tracked_modes: tuple[tuple[str, int, int], ...]
    tracked_paths: tuple[tuple[str, int, int, int, str], ...]


@dataclass(frozen=True)
class MaterializationEntry:
    path: str
    mode: str
    oid: str | None
    size: int

    @property
    def type(self) -> str:
        if self.mode == "040000":
            return "directory"
        if self.mode == "120000":
            return "symlink"
        return "file"


@dataclass(frozen=True)
class MaterializationPlan:
    """Compact immutable Git metadata for one workspace materialization."""

    base_commit_id: str
    digest: str
    entries: tuple[MaterializationEntry, ...]
    blob_count: int
    directory_count: int
    total_blob_bytes: int

    @property
    def entry_count(self) -> int:
        return len(self.entries)

    def iter_expected(self):
        return iter(self.entries)

    def record(self) -> dict[str, Any]:
        return {
            "contract": "asha.control-materialization-plan.v1",
            "base_commit_id": self.base_commit_id,
            "digest": self.digest,
            "blob_count": self.blob_count,
            "directory_count": self.directory_count,
            "entry_count": self.entry_count,
            "total_blob_bytes": self.total_blob_bytes,
        }


@dataclass(frozen=True)
class ContextCompatibilityProof:
    """Immutable-base classification and positive-ignore evidence."""

    base_commit_id: str
    materialization_digest: str
    project_id: str
    planned_context_paths: tuple[str, ...]
    private_directory_paths: tuple[str, ...]
    reused_paths: tuple[str, ...]
    required_ignored_paths: tuple[str, ...]
    info_exclude_digest: str
    digest: str


@dataclass(frozen=True)
class MissingPositiveIgnoreEvidence:
    """Typed immutable evidence for one unambiguously missing ignore rule."""

    base_commit_id: str
    materialization_digest: str
    project_id: str
    planned_context_paths: tuple[str, ...]
    private_directory_paths: tuple[str, ...]
    reused_paths: tuple[str, ...]
    required_ignored_paths: tuple[str, ...]
    missing_paths: tuple[str, ...]
    info_exclude_digest: str
    digest: str


class ContextCompatibilityError(JjError):
    """A typed positive-ignore failure; other proof failures remain generic."""

    def __init__(self, evidence: MissingPositiveIgnoreEvidence):
        self.evidence = evidence
        super().__init__(
            "controller-created context path is not positively ignored by the "
            f"immutable base: {', '.join(evidence.missing_paths)}"
        )


@dataclass(frozen=True)
class WorkspaceAddOperationProof:
    """Public jj operation ancestry for one exact workspace-add argv."""

    workspace_add_operation_id: str
    checkout_operation_id: str


@dataclass(frozen=True)
class GitMarkerBinding:
    """Filesystem-only identity for a supported exact Git root."""

    kind: str
    marker_fact: dict[str, int]
    marker_digest: str | None
    target: Path
    target_fact: dict[str, int]

    def record(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "marker_fact": dict(self.marker_fact),
            "marker_digest": self.marker_digest,
            "target": str(self.target),
            "target_fact": dict(self.target_fact),
        }


@dataclass(frozen=True)
class RepositoryPreEnableBinding:
    """Exact filesystem facts authorized before a plain-Git mutation."""

    root: Path
    root_fact: dict[str, int]
    git_binding: GitMarkerBinding


@dataclass(frozen=True)
class GitRemoteConfiguration:
    """Execution-safe local remote configuration plus exact file identity."""

    remotes: tuple[tuple[str, tuple[str, ...]], ...]
    config_digest: str


@dataclass(frozen=True)
class ColocationIntentAssessment:
    """Filesystem-only classification of one durable colocation record."""

    kind: str
    value: dict[str, Any] | None = None
    raw: bytes | None = None
    digest: str | None = None
    current_binding: dict[str, Any] | None = None
    detail: str | None = None
    device_remap: tuple[tuple[int, int], ...] | None = None


def _path_fact(path: Path, label: str) -> dict[str, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise JjError(f"cannot inspect {label}: {exc}") from exc
    return {
        "dev": metadata.st_dev,
        "ino": metadata.st_ino,
        "mode": metadata.st_mode,
        "uid": metadata.st_uid,
    }


def _bounded_marker_bytes(path: Path, label: str) -> tuple[bytes, dict[str, int]]:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise JjError(f"{label} must be a regular file")
        if before.st_size > MAX_GIT_MARKER_BYTES:
            raise JjError(f"{label} exceeds its bounded size")
        fd = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
            getattr(os, "O_CLOEXEC", 0),
        )
    except JjError:
        raise
    except OSError as exc:
        raise JjError(f"cannot read {label}: {exc}") from exc
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise JjError(f"{label} changed during inspection")
        chunks: list[bytes] = []
        remaining = MAX_GIT_MARKER_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 4096))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
    except OSError as exc:
        raise JjError(f"cannot read {label}: {exc}") from exc
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > MAX_GIT_MARKER_BYTES:
        raise JjError(f"{label} exceeds its bounded size")
    if (
        (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
         opened.st_size, opened.st_mtime_ns) !=
        (after.st_dev, after.st_ino, after.st_mode, after.st_uid,
         after.st_size, after.st_mtime_ns) or opened.st_size != len(raw)
    ):
        raise JjError(f"{label} changed during inspection")
    return raw, {
        "dev": opened.st_dev,
        "ino": opened.st_ino,
        "mode": opened.st_mode,
        "uid": opened.st_uid,
    }


def _one_path_marker(raw: bytes, label: str, *, prefix: bytes = b"") -> str:
    if prefix:
        if not raw.startswith(prefix):
            raise JjError(f"{label} has an invalid prefix")
        raw = raw[len(prefix):]
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    if not raw or any(marker in raw for marker in (b"\x00", b"\n", b"\r")):
        raise JjError(f"{label} does not contain exactly one path")
    try:
        return os.fsdecode(raw)
    except UnicodeError as exc:
        raise JjError(f"{label} path could not be decoded") from exc


def _canonical_marker_target(base: Path, value: str, label: str) -> Path:
    target = Path(value)
    if not target.is_absolute():
        target = base / target
    target = Path(os.path.realpath(target))
    try:
        metadata = target.lstat()
    except OSError as exc:
        raise JjError(f"{label} target is unavailable: {exc}") from exc
    if target.is_symlink() or not stat.S_ISDIR(metadata.st_mode):
        raise JjError(f"{label} target must be a canonical directory")
    return target


def _linked_common_dir(target: Path) -> Path | None:
    marker = target / "commondir"
    try:
        marker.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JjError(f"cannot inspect Git commondir marker: {exc}") from exc
    raw, _fact = _bounded_marker_bytes(marker, "Git commondir marker")
    value = _one_path_marker(raw, "Git commondir marker")
    return _canonical_marker_target(target, value, "Git commondir marker")


def inspect_git_marker(root: Path) -> GitMarkerBinding | None:
    """Read and classify one exact root's Git marker without invoking Git."""
    root = Path(root)
    marker = root / ".git"
    try:
        metadata = marker.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise JjError(f"cannot inspect Git marker: {exc}") from exc
    if stat.S_ISDIR(metadata.st_mode):
        try:
            head = (marker / "HEAD").lstat()
        except OSError as exc:
            raise JjError(f"Git directory HEAD is unavailable: {exc}") from exc
        if not stat.S_ISREG(head.st_mode):
            raise JjError("Git directory HEAD must be a regular file")
        fact = _path_fact(marker, "Git marker")
        return GitMarkerBinding("directory", fact, None, marker, fact)
    if not stat.S_ISREG(metadata.st_mode):
        raise JjError("Git marker must be a directory or regular gitdir file")
    raw, marker_fact = _bounded_marker_bytes(marker, "Git marker")
    value = _one_path_marker(raw, "Git marker", prefix=b"gitdir: ")
    target = _canonical_marker_target(root, value, "Git marker")
    try:
        head = (target / "HEAD").lstat()
    except OSError as exc:
        raise JjError(f"Git marker target HEAD is unavailable: {exc}") from exc
    if not stat.S_ISREG(head.st_mode):
        raise JjError("Git marker target HEAD must be a regular file")
    common_dir = _linked_common_dir(target)
    if common_dir is not None:
        primary = common_dir.parent if common_dir.name == ".git" else common_dir
        raise LinkedGitWorktreeError(
            f"linked Git worktree {root} cannot be auto-colocated; use the "
            f"primary worktree at {primary} as --repo, or manually create a "
            "supported jj repository and pass its exact root"
        )
    return GitMarkerBinding(
        "gitdir", marker_fact, hashlib.sha256(raw).hexdigest(), target,
        _path_fact(target, "Git marker target"),
    )


def inspect_pre_enable_binding(root: Path) -> RepositoryPreEnableBinding:
    """Bind one canonical source path to its root and complete Git marker facts."""
    root = Path(root)
    if (
        not root.is_absolute() or os.path.realpath(root) != str(root)
        or root.is_symlink() or not root.is_dir()
    ):
        raise JjError("pre-enable source must be an exact canonical directory")
    git_binding = inspect_git_marker(root)
    if git_binding is None:
        raise JjError("pre-enable source must be an exact Git root")
    return RepositoryPreEnableBinding(
        root=root,
        root_fact=_path_fact(root, "pre-enable repository root"),
        git_binding=git_binding,
    )


def require_pre_enable_binding(
    root: Path, expected: RepositoryPreEnableBinding,
) -> None:
    """Refuse when the named source no longer has its authorized exact facts."""
    if not isinstance(expected, RepositoryPreEnableBinding):
        raise JjError("pre-enable repository binding is missing or invalid")
    current = inspect_pre_enable_binding(root)
    if current != expected:
        raise JjError(
            "pre-enable repository binding changed; the replacement path was not authorized"
        )


class ColocationIntentStore:
    """Durable authentication for Control-owned plain-Git initialization."""

    def __init__(self, config: ControlConfig):
        self._config = config
        self.directory = config.tasks_dir.parent / "repository-inits"
        self._managed_start = _managed_start(
            self.directory, ("control", "repository-inits"),
        )
        self._active_mutations = threading.local()

    @staticmethod
    def _key(root: Path) -> str:
        return hashlib.sha256(
            b"asha-control-colocation-intent-v1\0" + str(root).encode("utf-8")
        ).hexdigest()

    def path(self, root: Path) -> Path:
        return self.directory / f"{self._key(Path(root))}.json"

    @contextmanager
    def mutation_lock(self, root: Path):
        """Serialize every cooperative Control intent write for one source."""
        root = Path(root)
        active = getattr(self._active_mutations, "roots", set())
        key = (str(self._config.tasks_dir), str(root))
        if key in active:
            yield
            return
        with TransactionCoordinator(self._config).source_lock(root):
            updated = set(active)
            updated.add(key)
            self._active_mutations.roots = updated
            try:
                yield
            finally:
                updated.remove(key)
                self._active_mutations.roots = updated

    def _binding(self, root: Path, *, verified: bool) -> dict[str, Any]:
        root = Path(root)
        if (not root.is_absolute() or os.path.realpath(root) != str(root) or
                root.is_symlink() or not root.is_dir()):
            raise JjError("colocation intent requires an exact canonical root")
        git_binding = inspect_git_marker(root)
        if git_binding is None:
            raise JjError("colocation intent requires an exact Git root")
        jj_fact = None
        if verified:
            try:
                metadata = (root / ".jj").lstat()
            except FileNotFoundError as exc:
                raise JjError(
                    "verified Control colocation record is stale: .jj is missing"
                ) from exc
            except OSError as exc:
                raise JjError(
                    f"verified Control colocation .jj could not be inspected: {exc}"
                ) from exc
            if not stat.S_ISDIR(metadata.st_mode):
                raise JjError(
                    "verified Control colocation .jj must be an owned directory, not a link or file"
                )
            jj_fact = _path_fact(root / ".jj", "jj repository marker")
        return {
            "contract": COLOCATION_INTENT_CONTRACT,
            "root": str(root),
            "root_fact": _path_fact(root, "repository root"),
            "git_binding": git_binding.record(),
            "jj_fact": jj_fact,
        }

    @staticmethod
    def _decode(raw: bytes) -> dict[str, Any]:
        try:
            value = json.loads(
                raw.decode("utf-8"), object_pairs_hook=_strict_intent_object,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise JjError("colocation intent is not strict UTF-8 JSON") from exc
        if (not isinstance(value, dict) or set(value) != {
            "contract", "root", "root_fact", "git_binding", "jj_fact", "state",
        }):
            raise JjError("colocation intent has invalid fields")
        if value["contract"] != COLOCATION_INTENT_CONTRACT:
            raise JjError("colocation intent contract is invalid")
        if value["state"] not in {"intent", "verified"}:
            raise JjError("colocation intent state is invalid")
        for name in ("root_fact",):
            fact = value[name]
            if (not isinstance(fact, dict) or set(fact) != {"dev", "ino", "mode", "uid"} or
                    any(type(item) is not int or item < 0 for item in fact.values())):
                raise JjError(f"colocation intent {name} is invalid")
        git_binding = value["git_binding"]
        if (not isinstance(git_binding, dict) or set(git_binding) != {
            "kind", "marker_fact", "marker_digest", "target", "target_fact",
        }):
            raise JjError("colocation intent Git marker binding is invalid")
        if git_binding["kind"] not in {"directory", "gitdir"}:
            raise JjError("colocation intent Git marker kind is invalid")
        for name in ("marker_fact", "target_fact"):
            fact = git_binding[name]
            if (not isinstance(fact, dict) or set(fact) != {"dev", "ino", "mode", "uid"} or
                    any(type(item) is not int or item < 0 for item in fact.values())):
                raise JjError(f"colocation intent Git {name} is invalid")
        target = git_binding["target"]
        if (not isinstance(target, str) or not target or
                not Path(target).is_absolute() or os.path.realpath(target) != target):
            raise JjError("colocation intent Git target is invalid")
        digest = git_binding["marker_digest"]
        if git_binding["kind"] == "directory":
            if digest is not None:
                raise JjError("directory Git marker must not have a content digest")
        elif not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise JjError("gitdir marker digest is invalid")
        if value["jj_fact"] is not None:
            fact = value["jj_fact"]
            if (not isinstance(fact, dict) or set(fact) != {"dev", "ino", "mode", "uid"} or
                    any(type(item) is not int or item < 0 for item in fact.values())):
                raise JjError("colocation intent jj_fact is invalid")
        if (value["state"] == "intent") != (value["jj_fact"] is None):
            raise JjError("colocation intent jj binding does not match its state")
        return value

    def _read_raw_fd(
        self, directory_fd: int, root: Path,
    ) -> tuple[bytes, dict[str, Any]] | None:
        name = f"{self._key(root)}.json"
        try:
            fd = _open_existing_file(directory_fd, name, "colocation intent")
        except FileNotFoundError:
            return None
        except (StoreError, OSError) as exc:
            raise JjError(str(exc)) from exc
        try:
            metadata = os.fstat(fd)
            if metadata.st_size > MAX_COLOCATION_INTENT_BYTES:
                raise JjError("colocation intent exceeds its bounded size")
            chunks: list[bytes] = []
            remaining = MAX_COLOCATION_INTENT_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(remaining, 4096))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(fd)
        finally:
            _close_quietly(fd)
        if len(raw) > MAX_COLOCATION_INTENT_BYTES:
            raise JjError("colocation intent exceeds its bounded size")
        if (
            (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid,
             metadata.st_size, metadata.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_mode, after.st_uid,
                after.st_size, after.st_mtime_ns)
            or metadata.st_size != len(raw)
        ):
            raise JjError("colocation intent changed during bounded inspection")
        return raw, self._decode(raw)

    def _read_fd(self, directory_fd: int, root: Path) -> dict[str, Any] | None:
        decoded = self._read_raw_fd(directory_fd, root)
        if decoded is None:
            return None
        _raw, value = decoded
        expected = self._binding(root, verified=value["state"] == "verified")
        for field in ("contract", "root", "root_fact", "git_binding", "jj_fact"):
            if value[field] != expected[field]:
                if field == "git_binding":
                    raise JjError(
                        "colocation intent Git marker binding does not match the current repository"
                    )
                raise JjError(
                    f"colocation intent is not bound to the current {field.replace('_', ' ')}"
                )
        return value

    @staticmethod
    def _root_hardening_candidate(
        stored: dict[str, Any], current: dict[str, Any],
    ) -> bool:
        old = stored["root_fact"]
        new = current["root_fact"]
        if any(old[field] != new[field] for field in ("dev", "ino", "uid")):
            return False
        old_mode, new_mode = old["mode"], new["mode"]
        if stat.S_IFMT(old_mode) != stat.S_IFMT(new_mode):
            return False
        removed = old_mode & ~new_mode
        added = new_mode & ~old_mode
        return removed != 0 and added == 0 and removed & ~0o022 == 0

    @staticmethod
    def _binding_facts(binding: dict[str, Any]) -> tuple[dict[str, int], ...]:
        return (
            binding["root_fact"],
            binding["git_binding"]["marker_fact"],
            binding["git_binding"]["target_fact"],
            binding["jj_fact"],
        )

    @classmethod
    def _coherent_device_remap(
        cls, stored: dict[str, Any], current: dict[str, Any],
    ) -> tuple[tuple[int, int], ...] | None:
        """Return one strict injective device remap, or reject all other drift."""
        if any(stored[field] != current[field] for field in ("contract", "root")):
            return None
        old_git = stored["git_binding"]
        new_git = current["git_binding"]
        if any(old_git[field] != new_git[field] for field in (
            "kind", "marker_digest", "target",
        )):
            return None
        if stored["jj_fact"] is None or current["jj_fact"] is None:
            return None
        old_facts = cls._binding_facts(stored)
        new_facts = cls._binding_facts(current)
        mapping: dict[int, int] = {}
        inverse: dict[int, int] = {}
        changed = False
        for old, new in zip(old_facts, new_facts):
            if any(old[field] != new[field] for field in ("ino", "mode", "uid")):
                return None
            old_dev, new_dev = old["dev"], new["dev"]
            if old_dev in mapping and mapping[old_dev] != new_dev:
                return None
            if new_dev in inverse and inverse[new_dev] != old_dev:
                return None
            mapping[old_dev] = new_dev
            inverse[new_dev] = old_dev
            changed = changed or old_dev != new_dev
        if not changed:
            return None
        return tuple(sorted(mapping.items()))

    def _assessment(
        self, root: Path, raw: bytes, value: dict[str, Any],
    ) -> ColocationIntentAssessment:
        digest = hashlib.sha256(raw).hexdigest()
        try:
            current = self._binding(root, verified=value["state"] == "verified")
        except JjError as exc:
            return ColocationIntentAssessment(
                "mismatch", value, raw, digest, detail=str(exc),
            )
        exact_fields = ("contract", "root", "root_fact", "git_binding", "jj_fact")
        if all(value[field] == current[field] for field in exact_fields):
            return ColocationIntentAssessment(
                value["state"], value, raw, digest, current,
            )
        if (
            value["state"] == "verified"
            and all(value[field] == current[field] for field in (
                "contract", "root", "git_binding", "jj_fact",
            ))
            and self._root_hardening_candidate(value, current)
        ):
            return ColocationIntentAssessment(
                "verified_root_hardening_candidate", value, raw, digest, current,
            )
        device_remap = (
            self._coherent_device_remap(value, current)
            if value["state"] == "verified" else None
        )
        if device_remap is not None:
            return ColocationIntentAssessment(
                "verified_device_rebind_candidate", value, raw, digest, current,
                device_remap=device_remap,
            )
        return ColocationIntentAssessment(
            "mismatch", value, raw, digest, current,
            "stored repository binding differs from current filesystem facts",
        )

    def classify(self, root: Path) -> ColocationIntentAssessment:
        root = Path(root)
        try:
            with _directory_fd(
                self.directory, create=False, managed_start=self._managed_start,
            ) as directory_fd:
                if directory_fd is None:
                    return ColocationIntentAssessment("missing")
                decoded = self._read_raw_fd(directory_fd, root)
                if decoded is None:
                    return ColocationIntentAssessment("missing")
                raw, value = decoded
                return self._assessment(root, raw, value)
        except (StoreError, OSError) as exc:
            raise JjError(str(exc)) from exc

    def read(self, root: Path) -> dict[str, Any] | None:
        try:
            with _directory_fd(
                self.directory, create=False, managed_start=self._managed_start,
            ) as directory_fd:
                if directory_fd is None:
                    return None
                return self._read_fd(directory_fd, root)
        except (StoreError, OSError) as exc:
            raise JjError(str(exc)) from exc

    def reauthenticate_root_hardening(
        self, root: Path, expected: ColocationIntentAssessment,
    ) -> None:
        """Rewrite one exact record under Control's exclusive source lock."""
        with self.mutation_lock(root):
            self._reauthenticate_verified_candidate_locked(root, expected)

    def reauthenticate_device_rebind(
        self, root: Path, expected: ColocationIntentAssessment,
    ) -> None:
        """Refresh cached device observations after caller reauthentication."""
        with self.mutation_lock(root):
            self._reauthenticate_verified_candidate_locked(root, expected)

    def _reauthenticate_root_hardening_locked(
        self, root: Path, expected: ColocationIntentAssessment,
    ) -> None:
        self._reauthenticate_verified_candidate_locked(root, expected)

    def _reauthenticate_verified_candidate_locked(
        self, root: Path, expected: ColocationIntentAssessment,
    ) -> None:
        root = Path(root)
        if (
            not isinstance(expected, ColocationIntentAssessment)
            or expected.kind not in {
                "verified_root_hardening_candidate",
                "verified_device_rebind_candidate",
            }
            or expected.raw is None or expected.digest is None
            or expected.current_binding is None or expected.value is None
        ):
            raise JjError("colocation reauthentication requires a verified typed candidate")
        if hashlib.sha256(expected.raw).hexdigest() != expected.digest:
            raise JjError("colocation reauthentication assessment digest is invalid")
        decoded_expected = self._decode(expected.raw)
        if decoded_expected != expected.value:
            raise JjError("colocation reauthentication assessment value is invalid")
        value = copy.deepcopy(decoded_expected)
        if expected.kind == "verified_root_hardening_candidate":
            value["root_fact"] = dict(expected.current_binding["root_fact"])
        else:
            if expected.device_remap is None:
                raise JjError("device reauthentication requires a coherent device remap")
            for stored_fact, current_fact in zip(
                self._binding_facts(value),
                self._binding_facts(expected.current_binding),
            ):
                stored_fact["dev"] = current_fact["dev"]
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(raw) > MAX_COLOCATION_INTENT_BYTES:
            raise JjError("colocation intent exceeds its bounded size")
        name = f"{self._key(root)}.json"
        temporary = f".{name}.tmp.{secrets.token_hex(8)}"
        try:
            with _directory_fd(
                self.directory, create=False, managed_start=self._managed_start,
            ) as directory_fd:
                if directory_fd is None:
                    raise JjError("colocation intent changed; preserved without repair")
                current = self._read_raw_fd(directory_fd, root)
                if current is None:
                    raise JjError("colocation intent changed; preserved without repair")
                current_raw, current_value = current
                current_assessment = self._assessment(root, current_raw, current_value)
                if (
                    current_raw != expected.raw
                    or hashlib.sha256(current_raw).hexdigest() != expected.digest
                    or current_assessment.kind != expected.kind
                    or current_assessment.current_binding != expected.current_binding
                    or current_assessment.device_remap != expected.device_remap
                ):
                    raise JjError("colocation intent changed; preserved without cooperative repair")
                fd = -1
                try:
                    fd = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                        0o600, dir_fd=directory_fd,
                    )
                    os.fchmod(fd, 0o600)
                    written = 0
                    while written < len(raw):
                        count = os.write(fd, raw[written:])
                        if count <= 0:
                            raise JjError("short write while reauthenticating colocation intent")
                        written += count
                    os.fsync(fd)
                    os.close(fd)
                    fd = -1
                    final = self._read_raw_fd(directory_fd, root)
                    if final is None or final[0] != expected.raw:
                        raise JjError("colocation intent changed; preserved without cooperative repair")
                    final_assessment = self._assessment(root, final[0], final[1])
                    if (
                        hashlib.sha256(final[0]).hexdigest() != expected.digest
                        or final_assessment.kind != expected.kind
                        or final_assessment.current_binding != expected.current_binding
                        or final_assessment.device_remap != expected.device_remap
                    ):
                        raise JjError("repository facts changed; intent preserved without cooperative repair")
                    os.replace(
                        temporary, name,
                        src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    )
                    os.fsync(directory_fd)
                finally:
                    if fd >= 0:
                        _close_quietly(fd)
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except OSError:
                        pass
        except (StoreError, OSError) as exc:
            raise JjError(str(exc)) from exc

    def _write(self, root: Path, state: str, *, expected: str | None) -> None:
        value = {
            **self._binding(root, verified=state == "verified"),
            "state": state,
        }
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(raw) > MAX_COLOCATION_INTENT_BYTES:
            raise JjError("colocation intent exceeds its bounded size")
        name = f"{self._key(root)}.json"
        temporary = f".{name}.tmp.{secrets.token_hex(8)}"
        try:
            with _directory_fd(
                self.directory, create=True, managed_start=self._managed_start,
            ) as directory_fd:
                assert directory_fd is not None
                current = self._read_fd(directory_fd, root)
                current_state = None if current is None else current["state"]
                if current_state != expected:
                    raise JjError(
                        "colocation intent changed; inspect retained repository state"
                    )
                fd = -1
                try:
                    fd = os.open(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                        getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                        0o600,
                        dir_fd=directory_fd,
                    )
                    os.fchmod(fd, 0o600)
                    written = 0
                    while written < len(raw):
                        count = os.write(fd, raw[written:])
                        if count <= 0:
                            raise JjError("short write while saving colocation intent")
                        written += count
                    os.fsync(fd)
                    os.close(fd)
                    fd = -1
                    os.replace(
                        temporary, name,
                        src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                    )
                    os.fsync(directory_fd)
                finally:
                    if fd >= 0:
                        _close_quietly(fd)
                    try:
                        os.unlink(temporary, dir_fd=directory_fd)
                    except OSError:
                        pass
        except (StoreError, OSError) as exc:
            raise JjError(str(exc)) from exc

    def begin(self, root: Path) -> None:
        with self.mutation_lock(root):
            self._write(Path(root), "intent", expected=None)

    def mark_verified(self, root: Path) -> None:
        with self.mutation_lock(root):
            self._write(Path(root), "verified", expected="intent")


def discover_git_root(start: Path) -> Path | None:
    """Return the nearest exact canonical plain-or-colocated Git root.

    Discovery is deliberately filesystem-only so caller-supplied task-ID
    replay and interrupted-journal checks can run before any source mutation.
    """
    candidate = Path(start)
    if (not candidate.is_absolute() or os.path.realpath(candidate) != str(candidate) or
            candidate.is_symlink() or not candidate.is_dir()):
        raise JjError("repository discovery start must be an exact canonical directory")
    for root in (candidate, *candidate.parents):
        if inspect_git_marker(root) is not None:
            return root
    return None


def _bounded_display_text(raw: str, limit: int) -> str:
    """Collapse one untrusted line into bounded printable display text."""
    text = "".join(
        character if character.isprintable() else " " for character in raw
    ).strip()
    if len(text) > limit:
        text = text[:limit - 3] + "..."
    return text


def colocated_sync_remediation(
    root: Path, git_head: str | None, working_copy_parent: str,
) -> str | None:
    if git_head is None:
        if working_copy_parent == "0" * 40:
            return None
        head_detail = "git HEAD is unborn"
    elif git_head == working_copy_parent:
        return None
    else:
        head_detail = f"git HEAD {git_head}"
    prefix = f"source working copy is out of sync with jj: {head_detail} "
    middle = f"but jj @- is {working_copy_parent}; run `jj status` in "
    suffix = " to import it, then retry"
    root_text = "".join(
        character if character.isprintable() else "?" for character in str(root)
    )
    root_budget = 500 - len(prefix) - len(middle) - len(suffix)
    if len(root_text) > root_budget:
        root_text = root_text[:root_budget - 3] + "..."
    return prefix + middle + root_text + suffix


def untracked_remote_bookmark_remediation(
    names: Sequence[str], *, remote: str = "origin",
) -> str | None:
    """Bound one read-only tracking problem and its operator-owned remedy."""
    if not names:
        return None
    shown = ", ".join(
        _bounded_display_text(name, MAX_REMOTE_BOOKMARK_NAME_BYTES)
        for name in names[:MAX_REMOTE_BOOKMARKS_DISPLAYED]
    )
    if len(names) > MAX_REMOTE_BOOKMARKS_DISPLAYED:
        shown += f", ... (+{len(names) - MAX_REMOTE_BOOKMARKS_DISPLAYED} more)"
    return (
        f"untracked remote bookmarks at {remote}: {shown}; remediate with: "
        f"jj bookmark track NAME --remote={remote}"
    )


class JjAdapter:
    def __init__(self, *, executable: str | None = None, runner: Callable[..., Any] | None = None):
        # ASHA_JJ carries the install-time absolute path into daemon contexts
        # whose sanitized PATH cannot resolve the operator's jj (#75).
        self.executable = executable or os.environ.get("ASHA_JJ") or "jj"
        self.runner = runner

    @staticmethod
    def _bounded_process(argv: list[str], *, cwd: Path | None, limit: int) -> tuple[int, bytes, bytes]:
        return bounded_process(argv, cwd=cwd, limit=limit, error_type=JjError)

    def _run_bytes(
        self, executable: str, args: Sequence[str], *, cwd: Path | None = None,
        limit: int = MAX_OUTPUT_BYTES,
    ) -> bytes:
        argv = [executable, *map(str, args)]
        return checked_bytes(
            argv, cwd=cwd, limit=limit, runner=self.runner, error_type=JjError,
        )

    def _run(self, args: Sequence[str], *, cwd: Path | None = None) -> str:
        try:
            return self._run_bytes(self.executable, args, cwd=cwd).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise JjError("jj output was not UTF-8") from exc

    def _run_status(
        self, executable: str, args: Sequence[str], *, cwd: Path | None = None,
        limit: int = MAX_OUTPUT_BYTES,
    ) -> tuple[int, bytes, bytes]:
        return capture_bytes(
            [executable, *map(str, args)], cwd=cwd, limit=limit,
            runner=self.runner, error_type=JjError,
        )

    @staticmethod
    def _exact_git_environment() -> dict[str, str]:
        """Return the complete minimal environment passed at Git execve."""
        return {
            "HOME": "/",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "GIT_SSH": TRUSTED_SSH_EXECUTABLE,
            "PAGER": "cat",
        }

    @staticmethod
    def _exact_git_args(root: Path, args: Sequence[str]) -> list[str]:
        """Bind trusted Git to one inspected root and neutralize read-time helpers."""
        root = Path(root)
        binding = inspect_git_marker(root)
        if binding is None:
            raise JjError("exact Git operation requires a supported Git root")
        return [
            TRUSTED_GIT_EXECUTABLE,
            "--no-pager", "--no-optional-locks", "--no-replace-objects",
            *_EXACT_GIT_CONFIG,
            f"--git-dir={binding.target}", f"--work-tree={root}",
            *map(str, args),
        ]

    @staticmethod
    def _bound_git_args(
        root: Path, git_root: Path, args: Sequence[str],
    ) -> list[str]:
        """Bind Git to canonical roots already authenticated by strict jj preflight."""
        root = Path(root)
        git_root = Path(git_root)
        if (
            not root.is_absolute() or os.path.realpath(root) != str(root)
            or root.is_symlink() or not root.is_dir()
            or not git_root.is_absolute()
            or os.path.realpath(git_root) != str(git_root)
            or git_root.is_symlink() or not git_root.is_dir()
        ):
            raise JjError("exact Git operation requires canonical bound roots")
        return [
            TRUSTED_GIT_EXECUTABLE,
            "--no-pager", "--no-optional-locks", "--no-replace-objects",
            *_EXACT_GIT_CONFIG,
            f"--git-dir={git_root}", f"--work-tree={root}",
            *map(str, args),
        ]

    def _exact_git_status(
        self, root: Path, args: Sequence[str], *, limit: int = MAX_OUTPUT_BYTES,
    ) -> tuple[int, bytes, bytes]:
        return capture_bytes(
            self._exact_git_args(root, args), cwd=None, limit=limit,
            runner=self.runner, error_type=JjError,
            env=self._exact_git_environment(),
        )

    def _exact_git_bytes(
        self, root: Path, args: Sequence[str], *, limit: int = MAX_OUTPUT_BYTES,
    ) -> bytes:
        return checked_bytes(
            self._exact_git_args(root, args), cwd=None, limit=limit,
            runner=self.runner, error_type=JjError,
            env=self._exact_git_environment(),
        )

    def _bound_git_bytes(
        self, root: Path, git_root: Path, args: Sequence[str],
        *, limit: int = MAX_OUTPUT_BYTES,
    ) -> bytes:
        return checked_bytes(
            self._bound_git_args(root, git_root, args), cwd=None, limit=limit,
            runner=self.runner, error_type=JjError,
            env=self._exact_git_environment(),
        )

    def _bound_git_status(
        self, root: Path, git_root: Path, args: Sequence[str], *,
        limit: int = MAX_OUTPUT_BYTES, input_data: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        return capture_bytes(
            self._bound_git_args(root, git_root, args), cwd=None, limit=limit,
            runner=self.runner, error_type=JjError,
            env=self._exact_git_environment(), input_data=input_data,
        )

    @staticmethod
    def _one_line(output: str, label: str, pattern: re.Pattern[str] | None = None) -> str:
        lines = output.splitlines()
        if len(lines) != 1 or not lines[0]:
            raise JjError(f"jj returned ambiguous {label}")
        value = lines[0]
        if pattern is not None and pattern.fullmatch(value) is None:
            raise JjError(f"jj returned invalid {label}")
        return value

    def preflight(self, source: Path) -> RepositoryFacts:
        source = Path(source)
        if (not source.is_absolute() or os.path.realpath(source) != str(source) or
                not source.is_dir() or source.is_symlink()):
            raise JjError("source repository must be its exact canonical directory root")
        root = self._one_line(self._run([
            "-R", str(source), "--ignore-working-copy", "root",
        ]), "repository root")
        if root != str(source):
            raise JjError("requested source is not the jj repository root")
        git_raw = self._one_line(self._run([
            "-R", str(source), "--ignore-working-copy", "git", "root",
        ]), "Git backend root")
        git_root = Path(git_raw)
        if not git_root.is_absolute():
            git_root = source / git_root
        git_root = Path(os.path.realpath(git_root))
        if not git_root.is_dir():
            raise JjError("jj repository does not expose a usable Git backend")
        return RepositoryFacts(root=source, git_root=git_root)

    def discover_root(self, start: Path) -> Path:
        """Return the canonical jj repository containing an existing directory."""
        start = Path(start)
        if (not start.is_absolute() or os.path.realpath(start) != str(start) or
                start.is_symlink() or not start.is_dir()):
            raise JjError("repository discovery start must be an exact canonical directory")
        root = self._one_line(self._run([
            "-R", str(start), "--ignore-working-copy", "root",
        ]), "repository root")
        candidate = Path(root)
        if (not candidate.is_absolute() or os.path.realpath(candidate) != str(candidate) or
                candidate.is_symlink() or not candidate.is_dir()):
            raise JjError("jj returned a non-canonical repository root")
        return self.preflight(candidate).root

    def _git_semantic_state(
        self, root: Path, *, include_jj_refs: bool = False,
    ) -> GitSemanticState:
        """Capture semantic state without Git status/diff/filter execution."""
        root = Path(root)

        def status(args: Sequence[str]) -> tuple[int, bytes]:
            returncode, stdout, _stderr = self._exact_git_status(
                root, args, limit=MAX_GIT_SEMANTIC_BYTES,
            )
            return returncode, stdout

        def checked(args: Sequence[str]) -> bytes:
            return self._exact_git_bytes(
                root, args, limit=MAX_GIT_SEMANTIC_BYTES,
            )

        def relative_path(raw_path: bytes, label: str) -> tuple[Path, str]:
            relative = Path(os.fsdecode(raw_path))
            if (
                relative.is_absolute() or not relative.parts
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise JjError(f"Git returned an unsafe {label} path")
            return relative, relative.as_posix()

        def hash_regular(path: Path, metadata: os.stat_result, label: str) -> tuple[int, str]:
            flags = (
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            fd = os.open(path, flags)
            try:
                opened = os.fstat(fd)
                initial = (
                    metadata.st_dev, metadata.st_ino, metadata.st_mode,
                    metadata.st_mtime_ns, metadata.st_size,
                )
                if (
                    opened.st_dev, opened.st_ino, opened.st_mode,
                    opened.st_mtime_ns, opened.st_size,
                ) != initial:
                    raise JjError(f"{label} changed during verification")
                digest = hashlib.sha256()
                read_bytes = 0
                while True:
                    chunk = os.read(fd, 65536)
                    if not chunk:
                        break
                    read_bytes += len(chunk)
                    digest.update(chunk)
                after = os.fstat(fd)
            finally:
                os.close(fd)
            if read_bytes != opened.st_size or (
                after.st_dev, after.st_ino, after.st_mode,
                after.st_mtime_ns, after.st_size,
            ) != (
                opened.st_dev, opened.st_ino, opened.st_mode,
                opened.st_mtime_ns, opened.st_size,
            ):
                raise JjError(f"{label} changed during verification")
            return read_bytes, digest.hexdigest()

        head = status(["rev-parse", "--verify", "HEAD^{commit}"])
        branch = status(["symbolic-ref", "--quiet", "HEAD"])
        index_debug = checked(["ls-files", "--stage", "--debug", "-z"])
        refs_raw = checked([
            "for-each-ref", "--format=%(refname)%00%(objectname)%00%(symref)",
        ])

        def valid_ref(raw: bytes) -> bool:
            if (
                not raw.startswith(b"refs/") or len(raw) > 1024
                or raw.endswith((b"/", b".", b".lock"))
                or b".." in raw or b"@{" in raw
                or any(
                    byte <= 0x20 or byte == 0x7f
                    or byte in b"~^:?*[\\"
                    for byte in raw
                )
            ):
                return False
            return all(
                component not in {b"", b"."}
                and not component.startswith(b".")
                and not component.endswith(b".lock")
                for component in raw.split(b"/")
            )

        ref_records = refs_raw.splitlines()
        if len(ref_records) > MAX_IMMUTABLE_TREE_ENTRIES:
            raise JjError("Git refs exceed the colocation preservation entry limit")
        refs_list: list[bytes] = []
        for record in ref_records:
            try:
                raw_ref, raw_oid, raw_symref = record.split(b"\0")
                oid = raw_oid.decode("ascii")
            except (ValueError, UnicodeDecodeError) as exc:
                raise JjError("Git returned a malformed semantic ref entry") from exc
            if (
                not valid_ref(raw_ref) or _COMMIT_ID.fullmatch(oid) is None
                or (raw_symref and not valid_ref(raw_symref))
            ):
                raise JjError("Git returned an invalid semantic ref entry")
            if include_jj_refs or not raw_ref.startswith(b"refs/jj/"):
                refs_list.append(record)
        refs = tuple(sorted(refs_list))

        debug_pattern = re.compile(
            rb"  ctime: ([0-9]{1,20}):([0-9]{1,9})\n"
            rb"  mtime: ([0-9]{1,20}):([0-9]{1,9})\n"
            rb"  dev: ([0-9]{1,20})\tino: ([0-9]{1,20})\n"
            rb"  uid: ([0-9]{1,20})\tgid: ([0-9]{1,20})\n"
            rb"  size: ([0-9]{1,20})\tflags: ([0-9a-f]{1,8})\n",
        )
        index_records: list[bytes] = []
        index_flags: list[tuple[str, int, int]] = []
        stage_zero: dict[str, tuple[str, str]] = {}
        indexed_paths: set[str] = set()
        debug_stats: dict[str, tuple[int, ...]] = {}
        indexed_stages: set[tuple[str, int]] = set()
        offset = 0
        while offset < len(index_debug):
            separator = index_debug.find(b"\0", offset)
            if separator < 0:
                raise JjError("Git returned malformed semantic index metadata")
            record = index_debug[offset:separator]
            try:
                header, raw_path = record.split(b"\t", 1)
                raw_mode, raw_oid, raw_stage = header.split(b" ")
                mode = raw_mode.decode("ascii")
                oid = raw_oid.decode("ascii")
                stage = int(raw_stage, 10)
            except (ValueError, UnicodeDecodeError) as exc:
                raise JjError("Git returned a malformed semantic index entry") from exc
            _relative, normalized = relative_path(raw_path, "semantic index")
            if (
                re.fullmatch(r"[0-7]{6}", mode) is None
                or _COMMIT_ID.fullmatch(oid) is None
                or raw_stage not in {b"0", b"1", b"2", b"3"}
            ):
                raise JjError("Git returned an invalid semantic index entry")
            matched = debug_pattern.match(index_debug, separator + 1)
            if matched is None:
                raise JjError("Git returned malformed or unbounded index-cache metadata")
            values = tuple(int(value, 10) for value in matched.groups()[:-1])
            flags = int(matched.group(10), 16)
            if (
                values[1] >= 1_000_000_000 or values[3] >= 1_000_000_000
                or any(value > 0xffff_ffff_ffff_ffff for value in values)
                or ((flags >> 12) & 0x3) != stage
            ):
                raise JjError("Git returned invalid bounded index-cache values or flags")
            key = (normalized, stage)
            if key in indexed_stages:
                raise JjError("Git returned duplicate semantic index stages")
            indexed_stages.add(key)
            indexed_paths.add(normalized)
            index_records.append(record + b"\0")
            index_flags.append((normalized, stage, flags))
            if stage == 0:
                if normalized in stage_zero:
                    raise JjError("Git returned duplicate stage-zero index entries")
                stage_zero[normalized] = (mode, oid)
                debug_stats[normalized] = values
            if len(index_records) > MAX_IMMUTABLE_TREE_ENTRIES:
                raise JjError(
                    "semantic index entries exceed the colocation preservation entry limit"
                )
            offset = matched.end()
        index = b"".join(index_records)

        tracked_raw = checked(["ls-files", "-z"])
        tracked_parts = tracked_raw.split(b"\0")
        if tracked_parts and tracked_parts[-1] == b"":
            tracked_parts.pop()
        if len(tracked_parts) > MAX_IMMUTABLE_TREE_ENTRIES:
            raise JjError(
                "tracked source paths exceed the colocation preservation entry limit"
            )
        tracked_modes: list[tuple[str, int, int]] = []
        tracked_paths: list[tuple[str, int, int, int, str]] = []
        tracked_seen: set[str] = set()
        for raw_path in tracked_parts:
            if not raw_path:
                raise JjError("Git returned an empty tracked source path")
            relative, normalized = relative_path(raw_path, "tracked source")
            if normalized in tracked_seen:
                # Unmerged indexes list the same working-tree path once for
                # each conflict stage. The normalized semantic index above
                # binds every stage; filesystem identity is read once.
                continue
            tracked_seen.add(normalized)
            path = root / relative
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                tracked_modes.append((normalized, -1, 0))
                tracked_paths.append((normalized, -1, 0, 0, "missing"))
                continue
            path_mode = stat.S_IMODE(metadata.st_mode)
            path_type = stat.S_IFMT(metadata.st_mode)
            tracked_modes.append((normalized, path_mode, path_type))
            cache = debug_stats.get(normalized)
            cache_matches = cache is not None and cache == (
                metadata.st_ctime_ns // 1_000_000_000,
                metadata.st_ctime_ns % 1_000_000_000,
                metadata.st_mtime_ns // 1_000_000_000,
                metadata.st_mtime_ns % 1_000_000_000,
                metadata.st_dev & 0xffff_ffff,
                metadata.st_ino & 0xffff_ffff,
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_size,
            )
            indexed = stage_zero.get(normalized)
            if cache_matches and indexed is not None:
                content_size = metadata.st_size
                content_identity = f"index:{indexed[0]}:{indexed[1]}"
            elif stat.S_ISREG(metadata.st_mode):
                content_size, raw_digest = hash_regular(
                    path, metadata, "tracked source path",
                )
                content_identity = f"raw:{raw_digest}"
            elif stat.S_ISLNK(metadata.st_mode):
                payload = os.fsencode(os.readlink(path))
                after = path.lstat()
                if (
                    metadata.st_dev, metadata.st_ino, metadata.st_mode,
                    metadata.st_mtime_ns,
                ) != (
                    after.st_dev, after.st_ino, after.st_mode, after.st_mtime_ns,
                ):
                    raise JjError("tracked source path changed during verification")
                content_size = len(payload)
                content_identity = f"raw:{hashlib.sha256(payload).hexdigest()}"
            elif stat.S_ISDIR(metadata.st_mode):
                content_size = 0
                content_identity = "gitlink-directory"
            else:
                raise JjError(
                    "tracked source contains a special path that cannot be verified"
                )
            tracked_paths.append((
                normalized, path_mode, path_type, content_size, content_identity,
            ))
        if tracked_seen != indexed_paths:
            raise JjError("Git tracked-path and semantic-index listings disagree")

        untracked_raw = checked([
            "ls-files", "--others", "--exclude-standard", "-z",
        ])
        raw_paths = untracked_raw.split(b"\0")
        if raw_paths and raw_paths[-1] == b"":
            raw_paths.pop()
        if len(raw_paths) > MAX_IMMUTABLE_TREE_ENTRIES:
            raise JjError(
                "untracked source paths exceed the colocation preservation entry limit"
            )
        entries: list[tuple[str, int, str, int]] = []
        total_bytes = 0
        seen: set[str] = set()
        for raw_path in raw_paths:
            if not raw_path:
                raise JjError("Git returned an empty untracked source path")
            relative, normalized = relative_path(raw_path, "untracked source")
            if normalized in seen:
                raise JjError("Git returned duplicate untracked source paths")
            seen.add(normalized)
            path = root / relative
            before_metadata = path.lstat()
            if stat.S_ISREG(before_metadata.st_mode):
                if before_metadata.st_size > MAX_TRACKED_BLOB_BYTES:
                    raise JjError(
                        "untracked source path exceeds the colocation preservation byte limit"
                    )
                size, path_digest = hash_regular(
                    path, before_metadata, "untracked source path",
                )
            elif stat.S_ISLNK(before_metadata.st_mode):
                payload = os.fsencode(os.readlink(path))
                after_metadata = path.lstat()
                if (
                    before_metadata.st_dev, before_metadata.st_ino,
                    before_metadata.st_mode, before_metadata.st_mtime_ns,
                ) != (
                    after_metadata.st_dev, after_metadata.st_ino,
                    after_metadata.st_mode, after_metadata.st_mtime_ns,
                ):
                    raise JjError("untracked source path changed during verification")
                path_digest = hashlib.sha256(payload).hexdigest()
                size = len(payload)
            else:
                raise JjError(
                    "untracked source contains a special path that cannot be verified"
                )
            total_bytes += size
            if total_bytes > MAX_TRACKED_TOTAL_BYTES:
                raise JjError(
                    "untracked source paths exceed the colocation preservation byte limit"
                )
            entries.append((
                normalized,
                stat.S_IMODE(before_metadata.st_mode),
                path_digest,
                stat.S_IFMT(before_metadata.st_mode),
            ))
        return GitSemanticState(
            head=head,
            branch=branch,
            index=index,
            index_flags=tuple(index_flags),
            refs=refs,
            paths=tuple(sorted(entries)),
            tracked_modes=tuple(sorted(tracked_modes)),
            tracked_paths=tuple(sorted(tracked_paths)),
        )

    def init_colocated(
        self, source: Path,
        *, expected_binding: RepositoryPreEnableBinding | None = None,
    ) -> dict[str, str]:
        """Enable one exact plain Git root as jj, retaining verified state.

        The source enablement is durable and intentionally outside task
        rollback.  A failed or ambiguous init is never removed because it may
        already have written jj state, Git refs, or index bookkeeping.
        """
        source = Path(source)
        if discover_git_root(source) != source:
            raise JjError("jj colocation requires the exact canonical Git root")
        if expected_binding is not None:
            require_pre_enable_binding(source, expected_binding)
        before = self._git_semantic_state(source)
        if expected_binding is not None:
            require_pre_enable_binding(source, expected_binding)
        try:
            self._run([
                "--config", 'snapshot.auto-track="none()"',
                "git", "init", "--colocate", str(source),
            ])
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            raise JjError(
                "jj git init --colocate failed; any partial .jj, jj Git refs, "
                "and index bookkeeping were retained for inspection. Run "
                f"`jj status` in {source} and repair or complete colocation "
                f"before retrying: {exc}"
            ) from exc
        try:
            facts = self.preflight(source)
            if facts.root != source:
                raise JjError("initialized repository root does not match the requested root")
            after = self._git_semantic_state(source)
            if expected_binding is not None:
                require_pre_enable_binding(source, expected_binding)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            raise JjError(
                "jj git init --colocate completed but strict verification failed; "
                "related repository state was retained. Run "
                f"`jj status` in {source} and inspect the repository before "
                f"retrying: {exc}"
            ) from exc
        if after != before:
            raise JjError(
                "jj git init --colocate changed semantic Git or working-tree state; "
                "verified repository enablement was retained for inspection. Run "
                f"`git status` and `jj status` in {source} before retrying"
            )
        return {
            "kind": "jj-operation",
            "detail": (
                "enabled and retained jj colocation while preserving semantic Git state"
            ),
            "operation": "git init --colocate",
        }

    def working_copy_parent(self, source: Path) -> str:
        """Read the source working copy's parent without snapshotting it."""
        output = self._run([
            "-R", str(source), "--ignore-working-copy", "log", "-r", "@-",
            "--no-graph", "-T", "commit_id",
        ])
        return self._one_line(output, "working-copy parent commit ID", _GIT_SHA1)

    def git_head(self, git_root: Path) -> str | None:
        """Read Git HEAD, returning ``None`` only for a confirmed unborn branch."""
        repository = Path(git_root)
        returncode, stdout, stderr = self._run_status(
            "git", ["-C", str(repository), "rev-parse", "--verify", "HEAD^{commit}"],
        )
        if returncode == 0:
            try:
                output = stdout.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise JjError("Git HEAD output was not UTF-8") from exc
            try:
                return self._one_line(output, "Git HEAD commit ID", _GIT_SHA1)
            except JjError as exc:
                raise JjError(
                    "Git HEAD was not exactly one full lowercase 40-hex commit ID"
                ) from exc

        # A failed rev-parse is not itself proof of an unborn repository. HEAD
        # must be a valid symbolic branch whose exact ref is absent. Detached,
        # corrupt, inaccessible, and invocation-failure cases all remain errors.
        symbolic_code, symbolic_stdout, _symbolic_stderr = self._run_status(
            "git", ["-C", str(repository), "symbolic-ref", "--quiet", "HEAD"],
        )
        if symbolic_code == 0:
            try:
                symbolic_output = symbolic_stdout.decode("utf-8")
                branch_ref = self._one_line(
                    symbolic_output, "Git HEAD symbolic ref", _GIT_BRANCH_REF,
                )
            except (UnicodeDecodeError, JjError):
                branch_ref = ""
            if branch_ref:
                ref_code, _ref_stdout, _ref_stderr = self._run_status(
                    "git", [
                        "-C", str(repository), "show-ref", "--verify", "--quiet",
                        branch_ref,
                    ],
                )
                if ref_code == 1:
                    return None
        detail = stderr[:4096].decode("utf-8", errors="replace").strip()
        raise JjError(f"Git HEAD could not be resolved: {detail or 'no diagnostic'}")

    def git_head_exact(self, root: Path) -> str | None:
        """Read HEAD through the sanitized exact-root Git preflight seam."""
        returncode, stdout, stderr = self._exact_git_status(
            root, ["rev-parse", "--verify", "--end-of-options", "HEAD^{commit}"],
        )
        if returncode == 0:
            value = _exact_ascii_line(stdout, "exact Git HEAD")
            if _COMMIT_ID.fullmatch(value) is None:
                raise JjError("exact Git HEAD was not one full object ID")
            return value
        symbolic_code, symbolic_stdout, _symbolic_stderr = self._exact_git_status(
            root, ["symbolic-ref", "--quiet", "HEAD"],
        )
        if symbolic_code == 0:
            try:
                branch_ref = _exact_ascii_line(
                    symbolic_stdout, "exact Git HEAD symbolic ref",
                )
            except JjError:
                branch_ref = ""
            if _valid_exact_git_ref(branch_ref, namespace="refs/heads/"):
                ref_code, _out, _err = self._exact_git_status(
                    root, ["show-ref", "--verify", "--quiet", "--end-of-options", branch_ref],
                )
                if ref_code == 1:
                    return None
        detail = stderr[:4096].decode("utf-8", errors="replace").strip()
        raise JjError(
            f"exact Git HEAD could not be resolved: {detail or 'no diagnostic'}"
        )

    def import_git(self, source: Path) -> tuple[dict[str, str], ...]:
        """Import Git refs once without snapshotting the source working copy."""
        self._run([
            "-R", str(source), "--ignore-working-copy", "git", "import",
        ])
        return ({
            "kind": "jj-operation",
            "detail": "recorded a jj operation-log entry for git import",
            "operation": "git import",
        },)

    def resolve_git_commit(self, root: Path, revision: str) -> str:
        """Resolve one exact explicit Git revision to an immutable commit ID."""
        if (
            not isinstance(revision, str) or not 1 <= len(revision) <= 500
            or any(ord(character) < 32 or ord(character) == 127 for character in revision)
        ):
            raise JjError("explicit base must contain 1-500 printable characters")
        returncode, stdout, _stderr = self._exact_git_status(root, [
            "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}",
        ])
        if returncode != 0:
            raise JjError(
                f"explicit base {revision!r} did not resolve as exactly one Git commit; "
                "pass an existing Git ref/tag/OID, or initialize jj manually before "
                "using a jj-only revset"
            )
        try:
            value = stdout.decode("ascii")
        except UnicodeDecodeError as exc:
            raise JjError("explicit base Git resolution returned non-ASCII output") from exc
        lines = value.splitlines()
        if len(lines) != 1 or _COMMIT_ID.fullmatch(lines[0]) is None:
            raise JjError("explicit base Git resolution was not one full object ID")
        return lines[0]

    def _exact_ref_commit(self, root: Path, ref: str) -> str | None:
        returncode, stdout, _stderr = self._exact_git_status(root, [
            "rev-parse", "--verify", "--quiet", "--end-of-options",
            f"{ref}^{{commit}}",
        ])
        if returncode == 1:
            return None
        if returncode != 0:
            raise JjError(f"Git ref {ref!r} could not be inspected exactly")
        value = _exact_ascii_line(stdout, "Git ref resolution")
        if _COMMIT_ID.fullmatch(value) is None:
            raise JjError("Git ref resolution was not one full object ID")
        return value

    @staticmethod
    def _bounded_default_candidates(
        candidates: Sequence[tuple[str, str]], *, tier: str,
    ) -> DefaultBaseResolution:
        unique = sorted(set(candidates))
        by_oid: dict[str, list[str]] = {}
        for ref, oid in unique:
            by_oid.setdefault(oid, []).append(ref)
        if len(by_oid) != 1:
            shown = ", ".join(ref for ref, _oid in unique[:8])
            if len(unique) > 8:
                shown += f", ... ({len(unique)} candidates)"
            raise JjError(
                f"default-base {tier} candidates resolve to different commits "
                f"({shown}); pass an explicit --base"
            )
        commit_id, refs = next(iter(by_oid.items()))
        return DefaultBaseResolution(tuple(refs), commit_id, tier)

    def _default_ref_commit(self, root: Path, ref: str) -> str | None:
        """Resolve an existing candidate and distinguish missing from non-commit."""
        returncode, _stdout, _stderr = self._exact_git_status(root, [
            "show-ref", "--verify", "--quiet", "--end-of-options", ref,
        ])
        if returncode == 1:
            return None
        if returncode != 0:
            raise JjError(f"Git ref {ref!r} could not be inspected exactly")
        commit_id = self._exact_ref_commit(root, ref)
        if commit_id is None:
            raise JjError(f"Git ref {ref!r} exists but does not name a commit")
        return commit_id

    def resolve_default_base(self, root: Path) -> DefaultBaseResolution:
        """Resolve an omitted base without jj, imports, fetches, or writes."""
        symbolic_code, symbolic_stdout, symbolic_stderr = self._exact_git_status(
            root, ["symbolic-ref", "--quiet", "HEAD"],
        )
        if symbolic_code == 0:
            try:
                attached_ref = _exact_ascii_line(
                    symbolic_stdout, "Git symbolic HEAD",
                )
            except JjError as exc:
                raise JjError("Git symbolic HEAD was malformed") from exc
            if not _valid_exact_git_ref(
                attached_ref, namespace="refs/heads/",
            ):
                raise JjError("Git symbolic HEAD was malformed")
            attached_oid = self._default_ref_commit(root, attached_ref)
            if attached_oid is not None:
                return DefaultBaseResolution(
                    (attached_ref,), attached_oid, "attached-local",
                )
            # A valid attached branch with no ref is an unborn HEAD. Fall through.
        elif symbolic_code != 1:
            detail = symbolic_stderr[:1024].decode(
                "utf-8", errors="replace",
            ).strip()
            raise JjError(
                f"Git symbolic HEAD could not be inspected: {detail or 'no diagnostic'}"
            )

        raw = self._exact_git_bytes(root, [
            "for-each-ref", "--format=%(refname)%00%(symref)", "refs/remotes",
        ])
        records = raw.split(b"\n")
        if records and records[-1] == b"":
            records.pop()
        remote_targets: list[tuple[str, str]] = []
        for line in records:
            if not line:
                raise JjError("Git remote-default refs were malformed")
            try:
                ref_raw, target_raw = line.split(b"\0", 1)
                ref = ref_raw.decode("ascii")
                target = target_raw.decode("ascii")
            except (ValueError, UnicodeDecodeError) as exc:
                raise JjError("Git remote-default refs were malformed") from exc
            if not _valid_exact_git_ref(ref, namespace="refs/remotes/"):
                raise JjError("Git remote-default refs were malformed")
            if not ref.endswith("/HEAD"):
                continue
            if (
                not _valid_exact_git_ref(target, namespace="refs/remotes/")
                or target.endswith("/HEAD")
            ):
                raise JjError("Git remote-default refs were malformed")
            remote_targets.append((ref, target))
            if len(remote_targets) > 128:
                raise JjError("Git has too many remote default refs")

        remote_defaults: list[tuple[str, str]] = []
        for ref, target in remote_targets:
            oid = self._default_ref_commit(root, target)
            if oid is None:
                raise JjError(f"Git remote default {ref!r} has a missing target")
            remote_defaults.append((target, oid))
        if remote_defaults:
            return self._bounded_default_candidates(
                remote_defaults, tier="remote-head",
            )

        conventional: list[tuple[str, str]] = []
        for branch in ("main", "master", "trunk"):
            ref = f"refs/heads/{branch}"
            oid = self._default_ref_commit(root, ref)
            if oid is not None:
                conventional.append((ref, oid))
        if conventional:
            return self._bounded_default_candidates(
                conventional, tier="conventional-local",
            )
        raise JjError(
            "Git has no attached local branch, remote default, or conventional "
            "local main/master/trunk; pass an explicit --base"
        )

    def resolve_plain_git_default(self, root: Path) -> tuple[str, str]:
        """Compatibility wrapper for callers that only consume one ref."""
        resolution = self.resolve_default_base(root)
        return resolution.references[0], resolution.commit_id

    @staticmethod
    def _divergence_reference(references: Sequence[str]) -> str:
        """Name a resolved baseline the way an operator names its bookmark."""
        names = [
            _bounded_display_text(
                reference[len("refs/heads/"):]
                if reference.startswith("refs/heads/") else reference,
                MAX_BASELINE_DIVERGENCE_SUMMARY,
            )
            for reference in list(references)[:MAX_BASELINE_DIVERGENCE_COMMITS]
        ]
        return ", ".join(name for name in names if name) or "the default base"

    def _divergence_commits(
        self, root: Path, head: str, baseline_commit_id: str,
    ) -> tuple[tuple[str, str], ...]:
        """Read at most a handful of bounded first lines, newest first."""
        try:
            raw = self._exact_git_bytes(root, [
                "log", "--no-show-signature", "--no-decorate",
                f"--max-count={MAX_BASELINE_DIVERGENCE_COMMITS}",
                "--format=%H%x00%s", "--end-of-options",
                head, f"^{baseline_commit_id}",
            ], limit=MAX_BASELINE_DIVERGENCE_BYTES)
        except JjError:
            # One pathological first line must not cost the count warning.
            return ()
        commits: list[tuple[str, str]] = []
        for line in raw.split(b"\n"):
            if not line:
                continue
            commit_raw, _separator, summary_raw = line.partition(b"\0")
            commit_id = commit_raw.decode("ascii", errors="replace")
            if _COMMIT_ID.fullmatch(commit_id) is None:
                return ()
            summary = _bounded_display_text(
                summary_raw.decode("utf-8", errors="replace"),
                MAX_BASELINE_DIVERGENCE_SUMMARY,
            )
            commits.append((commit_id, summary or "(no description)"))
            if len(commits) == MAX_BASELINE_DIVERGENCE_COMMITS:
                break
        return tuple(commits)

    def detect_baseline_divergence(
        self, root: Path, baseline_commit_id: str, *,
        references: Sequence[str] = (),
    ) -> BaselineDivergence | None:
        """Report landed commits sitting above an already-resolved baseline.

        Read-only and advisory (#81). The baseline is never re-selected from
        what this finds, and a probe that cannot complete reports nothing
        rather than refusing a baseline that already resolved, so a grafted or
        partial history costs an operator a warning and never the command.
        Returns ``None`` whenever the landed chain is at or behind the base.
        """
        if _COMMIT_ID.fullmatch(baseline_commit_id) is None:
            raise JjError("baseline divergence check requires a full commit ID")
        # jj exports `@-` as Git HEAD, so HEAD -- not `@` -- is the landed
        # chain. The working-copy commit itself is ordinary in-progress work
        # that no baseline is ever expected to contain.
        head = self.git_head_exact(root)
        if head is None or head == baseline_commit_id:
            return None
        returncode, stdout, _stderr = self._exact_git_status(root, [
            "rev-list", "--count", "--end-of-options",
            head, f"^{baseline_commit_id}",
        ])
        if returncode != 0:
            return None
        value = _exact_ascii_line(stdout, "Git divergence count")
        if len(value) > 12 or not value.isdigit():
            raise JjError("Git divergence count was not one bounded integer")
        ahead_count = int(value)
        if ahead_count == 0:
            return None
        return BaselineDivergence(
            reference=self._divergence_reference(references),
            baseline_commit_id=baseline_commit_id,
            working_copy_parent_commit_id=head,
            ahead_count=ahead_count,
            commits=self._divergence_commits(root, head, baseline_commit_id),
        )

    @staticmethod
    def _execution_capable_config_key(key: str) -> bool:
        lowered = key.lower()
        if lowered in {
            "core.sshcommand", "core.gitproxy", "core.fsmonitor",
            "credential.helper", "include.path", "protocol.ext.allow",
        }:
            return True
        return any(pattern.fullmatch(lowered) is not None for pattern in (
            re.compile(r"includeif\..+\.path"),
            re.compile(r"url\..+\.(?:insteadof|pushinsteadof)"),
            re.compile(r"credential\..+\.helper"),
            re.compile(r"filter\..+\.(?:clean|smudge|process)"),
            re.compile(r"diff\..+\.(?:command|textconv)"),
            re.compile(r"merge\..+\.driver"),
            re.compile(r"remote\..+\.(?:proxy|uploadpack|receivepack|vcs)"),
        ))

    @staticmethod
    def _git_config_digest(root: Path) -> str:
        binding = inspect_git_marker(Path(root))
        if binding is None:
            raise JjError("Git configuration requires an exact repository binding")
        path = binding.target / "config"
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise JjError("Git local config must be one regular file")
            if metadata.st_size > MAX_GIT_CONFIG_BYTES:
                raise JjError("Git local config exceeds its bounded size")
            fd = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except JjError:
            raise
        except OSError as exc:
            raise JjError(f"Git local config could not be inspected: {exc}") from exc
        try:
            opened = os.fstat(fd)
            if (
                opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
                opened.st_size,
            ) != (
                metadata.st_dev, metadata.st_ino, metadata.st_mode,
                metadata.st_uid, metadata.st_size,
            ):
                raise JjError("Git local config changed during inspection")
            digest = hashlib.sha256()
            read_bytes = 0
            while True:
                chunk = os.read(fd, min(65536, MAX_GIT_CONFIG_BYTES + 1 - read_bytes))
                if not chunk:
                    break
                read_bytes += len(chunk)
                if read_bytes > MAX_GIT_CONFIG_BYTES:
                    raise JjError("Git local config exceeds its bounded size")
                digest.update(chunk)
            after = os.fstat(fd)
        finally:
            os.close(fd)
        if read_bytes != opened.st_size or (
            after.st_dev, after.st_ino, after.st_mode, after.st_uid,
            after.st_size, after.st_mtime_ns,
        ) != (
            opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
            opened.st_size, opened.st_mtime_ns,
        ):
            raise JjError("Git local config changed during inspection")
        return digest.hexdigest()

    def git_remote_configuration(self, root: Path) -> GitRemoteConfiguration:
        """Read local remote URLs without rewrite expansion or helper execution."""
        config_digest = self._git_config_digest(root)
        raw = self._exact_git_bytes(
            root, ["config", "--local", "--no-includes", "--null", "--list"],
            limit=MAX_GIT_CONFIG_BYTES,
        )
        values: dict[str, list[str]] = {}
        try:
            records = raw.split(b"\0")
            if records and records[-1] == b"":
                records.pop()
            for record in records:
                raw_key, raw_value = record.split(b"\n", 1)
                key = raw_key.decode("utf-8")
                value = raw_value.decode("utf-8")
                if not key or "\x00" in value:
                    raise ValueError
                if key.lower() == "extensions.worktreeconfig":
                    raise JjError(
                        "Git extensions.worktreeConfig enables an unbound split "
                        "local config; disable it, or fetch the PR head manually "
                        "before task start"
                    )
                if self._execution_capable_config_key(key):
                    raise JjError(
                        f"Git local config {key!r} is execution-capable; remove or "
                        "disable it, or fetch the PR head manually before task start"
                    )
                matched = re.fullmatch(r"remote\.(.+)\.url", key, re.IGNORECASE)
                if matched is not None:
                    name = matched.group(1)
                    if _GIT_REMOTE_NAME.fullmatch(name) is None or ".." in name:
                        raise JjError("Git remote name uses an invalid restricted grammar")
                    values.setdefault(name, []).append(value)
        except JjError:
            raise
        except (ValueError, UnicodeDecodeError) as exc:
            raise JjError("Git returned malformed bounded local configuration") from exc
        if not values or len(values) > 128:
            raise JjError("Git repository has no configured remote URLs")
        remotes: list[tuple[str, tuple[str, ...]]] = []
        for name, urls in values.items():
            if (
                not urls or len(urls) > 128
                or any(not url or len(url) > 2048 for url in urls)
            ):
                raise JjError("Git remote URL configuration is empty or ambiguous")
            remotes.append((name, tuple(urls)))
        if self._git_config_digest(root) != config_digest:
            raise JjError("Git local config changed during PR preflight")
        return GitRemoteConfiguration(
            remotes=tuple(sorted(remotes)),
            config_digest=config_digest,
        )

    def fetch_git_ref_exact(
        self, root: Path, url: str, refspec: str, *, transport: str,
        config_digest: str,
    ) -> None:
        """Fetch one validated URL without consulting a named Git remote."""
        if (
            not isinstance(url, str) or not 1 <= len(url) <= 2048
            or any(ord(character) < 32 or ord(character) == 127 for character in url)
            or transport not in {"https", "ssh"}
            or not isinstance(config_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", config_digest) is None
        ):
            raise JjError("validated Git fetch transport is invalid")
        parsed = urlsplit(url)
        if transport == "https":
            safe_url = (
                parsed.scheme == "https" and parsed.hostname is not None
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
            )
        else:
            scp_style = re.fullmatch(
                r"git@[^/:\s]+:[^\s]+", url, re.ASCII,
            ) is not None
            safe_url = scp_style or (
                parsed.scheme == "ssh" and parsed.hostname is not None
                and parsed.username in {None, "git"} and parsed.password is None
                and not parsed.query and not parsed.fragment
            )
        if not safe_url:
            raise JjError("validated Git fetch URL does not match its safe transport")
        if (
            not isinstance(refspec, str) or not 1 <= len(refspec) <= 512
            or any(ord(character) < 32 or ord(character) == 127 for character in refspec)
            or refspec.startswith("-")
        ):
            raise JjError("Git fetch refspec uses an invalid restricted grammar")
        if self._git_config_digest(root) != config_digest:
            raise JjError(
                "Git local config changed after PR preflight; no fetch was attempted"
            )
        environment = self._exact_git_environment()
        environment["GIT_ALLOW_PROTOCOL"] = transport
        checked_bytes(
            self._exact_git_args(root, [
                "fetch", "--no-tags", "--no-write-fetch-head", url, refspec,
            ]),
            cwd=None, limit=MAX_OUTPUT_BYTES, runner=self.runner,
            error_type=JjError, env=environment,
        )

    @contextmanager
    def prerequisite_pr_head(
        self, source: Path, url: str, source_ref: str, *, transport: str,
        config_digest: str, expected_commit_id: str,
    ):
        """Fetch one PR head into an isolated object plane for read-only proof.

        The source repository is inspected before and after the fetch, but no
        source ref, object, config, jj operation, or working-tree path is
        written. The yielded repository is temporary and exists only while the
        caller builds and verifies its immutable materialization proof.
        """
        source = Path(source)
        if (
            _COMMIT_ID.fullmatch(expected_commit_id) is None
            or re.fullmatch(r"pull/[1-9][0-9]{0,9}/head", source_ref) is None
            or not isinstance(url, str) or not 1 <= len(url) <= 2048
            or any(ord(character) < 32 or ord(character) == 127 for character in url)
            or transport not in {"https", "ssh"}
            or re.fullmatch(r"[0-9a-f]{64}", config_digest or "") is None
        ):
            raise JjError("PR prerequisite fetch parameters are invalid")
        parsed = urlsplit(url)
        if transport == "https":
            safe_url = (
                parsed.scheme == "https" and parsed.hostname is not None
                and parsed.username is None and parsed.password is None
                and not parsed.query and not parsed.fragment
            )
        else:
            safe_url = re.fullmatch(
                r"git@[^/:\s]+:[^\s]+", url, re.ASCII,
            ) is not None or (
                parsed.scheme == "ssh" and parsed.hostname is not None
                and parsed.username in {None, "git"} and parsed.password is None
                and not parsed.query and not parsed.fragment
            )
        if not safe_url:
            raise JjError("PR prerequisite fetch URL does not match its safe transport")
        if self._git_config_digest(source) != config_digest:
            raise JjError(
                "Git local config changed after PR metadata preflight; no "
                "prerequisite fetch was attempted"
            )
        environment = self._exact_git_environment()
        environment["GIT_ALLOW_PROTOCOL"] = transport
        with tempfile.TemporaryDirectory(prefix="asha-control-pr-proof-") as temporary:
            root = Path(temporary).resolve() / "repository"
            checked_bytes(
                [
                    TRUSTED_GIT_EXECUTABLE, "--no-pager", "--no-optional-locks",
                    "--no-replace-objects", *_EXACT_GIT_CONFIG,
                    "init", "--quiet", "--template=", str(root),
                ],
                cwd="/", limit=MAX_OUTPUT_BYTES, runner=self.runner,
                error_type=JjError, env=environment,
            )
            os.chmod(root, 0o700)
            proof_ref = "refs/heads/asha-control-prerequisite"
            checked_bytes(
                self._exact_git_args(root, [
                    "fetch", "--no-tags", "--no-write-fetch-head", "--depth=1",
                    f"--filter=blob:limit={MAX_TRACKED_BLOB_BYTES}",
                    url, f"{source_ref}:{proof_ref}",
                ]),
                cwd=None, limit=MAX_OUTPUT_BYTES, runner=self.runner,
                error_type=JjError, env=environment,
            )
            observed = self.resolve_git_commit(root, proof_ref)
            if observed != expected_commit_id:
                raise JjError(
                    "pull-request head changed after metadata inspection; no "
                    "source repository state was changed"
                )
            if self._git_config_digest(source) != config_digest:
                raise JjError(
                    "Git local config changed during isolated PR prerequisite fetch; "
                    "no source repository state was changed"
                )
            yield root

    def resolve_base(self, source: Path, revset: str) -> str:
        if not isinstance(revset, str) or not 1 <= len(revset) <= 500:
            raise JjError("base revset must contain 1-500 characters")
        output = self._run([
            "-R", str(source), "--ignore-working-copy", "log", "-r", revset,
            "--no-graph", "-T", 'commit_id ++ "\\n"',
        ])
        if revset == DEFAULT_BASE_REVSET and not output.strip():
            raise JjError(_DEFAULT_BASE_UNRESOLVED)
        resolved = self._one_line(output, "base commit ID", _COMMIT_ID)
        # jj's default `trunk()` looks for a REMOTE bookmark, so in a repository
        # with no remote it resolves to the all-zero root commit. That commit has
        # no tree, and letting it through fails much later inside `git ls-tree`
        # with an unactionable "fatal: not a tree object". The contract requires
        # refusing an ambiguous or missing base outright, so refuse it here with
        # the remedy attached.
        if set(resolved) == {"0"}:
            if revset == DEFAULT_BASE_REVSET:
                raise JjError(_DEFAULT_BASE_UNRESOLVED)
            raise JjError(
                f"base revset {revset!r} resolved to the empty root commit; "
                "this repository has no usable trunk (jj's default trunk() needs "
                "a remote bookmark). Pass an explicit --base, for example "
                "--base main, or add a remote."
            )
        return resolved

    def untracked_remote_bookmarks(
        self, source: Path, *, remote: str = "origin",
    ) -> tuple[str, ...]:
        """List untracked bookmarks remembered for one remote without mutation.

        ``--ignore-working-copy`` is load-bearing: this delivery preflight must
        not snapshot the source merely to explain why a later push would fail.
        The template emits only untracked remote refs, and the parser keeps the
        result bounded before any name reaches a diagnostic.
        """
        if _GIT_REMOTE_NAME.fullmatch(remote) is None:
            raise JjError("remote bookmark inspection requires a valid remote name")
        output = self._run([
            "-R", str(source), "--ignore-working-copy", "--at-operation=@",
            "bookmark", "list",
            "--remote", f"exact:{remote}", "--template",
            'if(present && !tracked, name ++ "\\n")',
        ])
        names: list[str] = []
        for line in output.splitlines():
            if (
                not line
                or len(line.encode("utf-8")) > MAX_REMOTE_BOOKMARK_NAME_BYTES
                or any(
                    unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                    for character in line
                )
            ):
                raise JjError("jj returned a malformed remote bookmark name")
            names.append(line)
            if len(names) > MAX_REMOTE_BOOKMARKS:
                raise JjError(
                    f"remote bookmark inspection exceeds {MAX_REMOTE_BOOKMARKS} entries"
                )
        if len(set(names)) != len(names):
            raise JjError("jj returned duplicate remote bookmark names")
        return tuple(sorted(names))

    def require_visible_commit(self, source: Path, commit_id: str) -> None:
        """Confirm an already-resolved full commit ID is visible to this jj repo."""
        if _COMMIT_ID.fullmatch(commit_id) is None:
            raise JjError("visible commit check requires a full commit ID")
        if set(commit_id) == {"0"}:
            raise JjError("visible commit check refuses the empty root commit")
        output = self._run([
            "-R", str(source), "--ignore-working-copy", "log", "-r", commit_id,
            "--no-graph", "-T", 'commit_id ++ "\\n"',
        ])
        observed = self._one_line(output, "visible commit ID", _COMMIT_ID)
        if observed != commit_id:
            raise JjError("resolved commit ID is not visible in the repository")

    def pin_operation(self, source: Path) -> str:
        output = self._run([
            "-R", str(source), "--ignore-working-copy", "operation", "log",
            "--limit", "1", "--no-graph", "--template", 'id ++ "\\n"',
        ])
        return self._one_line(output, "full operation ID", _OPERATION_ID)

    def add_workspace(
        self,
        source: Path,
        destination: Path,
        name: str,
        base_commit_id: str | Sequence[str],
        message: str,
        operation_id: str,
    ) -> None:
        if _OPERATION_ID.fullmatch(operation_id) is None:
            raise JjError("workspace add requires a full 128-hex operation ID")
        self._run(_workspace_add_argv(
            source, destination, name,
            _workspace_base_commit_ids(base_commit_id, "workspace add"),
            message, operation_id,
        ))

    def workspace_conflicts(
        self, workspace: Path,
    ) -> tuple[bool, tuple[str, ...], bool]:
        """Whether this workspace's working-copy commit is conflicted, with paths.

        The boolean is the verdict and is read from jj's own `conflict` keyword.
        The bounded path list is descriptive only: `jj resolve --list` prints one
        column-aligned line per conflicted path, so an unreadable or oversized
        listing costs the names, never the verdict.
        """
        conflict = self._one_line(self._run([
            "-R", str(workspace), "--ignore-working-copy", "log", "-r", "@",
            "--no-graph", "-T", 'conflict ++ "\\n"',
        ]), "workspace conflict state")
        if conflict not in {"true", "false"}:
            raise JjError("jj returned invalid workspace conflict state")
        if conflict == "false":
            return False, (), False
        try:
            raw = self._run_bytes(
                self.executable,
                ["-R", str(workspace), "--ignore-working-copy", "resolve", "--list"],
                cwd=Path(workspace), limit=MAX_WORKSPACE_CONFLICT_BYTES,
            )
            listing = raw.decode("utf-8").splitlines()
        except (JjError, UnicodeError, OSError):
            return True, (), True
        paths: list[str] = []
        for line in listing:
            path = _CONFLICT_DESCRIPTOR.sub("", line)
            if (
                not path or path.startswith("/") or path.endswith("/")
                or any(
                    unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                    for character in path
                )
            ):
                return True, tuple(paths[:MAX_WORKSPACE_CONFLICT_PATHS]), True
            paths.append(path)
        return (
            True, tuple(paths[:MAX_WORKSPACE_CONFLICT_PATHS]),
            len(paths) > MAX_WORKSPACE_CONFLICT_PATHS,
        )

    def workspace_add_operation_proof(
        self, source: Path, *, pinned_operation_id: str, workspace_name: str,
        base_commit_id: str | Sequence[str], description: str, destination: Path,
    ) -> WorkspaceAddOperationProof:
        """Authenticate the public two-operation workspace-add ancestry."""
        if _OPERATION_ID.fullmatch(pinned_operation_id) is None:
            raise JjError("workspace operation ancestry requires a full pinned operation ID")
        base_commit_ids = _workspace_base_commit_ids(
            base_commit_id, "workspace operation ancestry",
        )
        template = (
            'id ++ "\\0" ++ parents.map(|p| p.id()).join(" ") ++ "\\0" ++ '
            'description.escape_json() ++ "\\0" ++ tags.escape_json() ++ "\\n"'
        )
        try:
            raw = self._run_bytes(
                self.executable,
                [
                    "-R", str(source), "--ignore-working-copy", "operation", "log",
                    "--no-graph", "--limit", "256", "--template", template,
                ],
                limit=MAX_TREE_LIST_BYTES,
            )
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise JjError("jj operation ancestry output was not UTF-8") from exc
        operations: dict[str, tuple[tuple[str, ...], str, str]] = {}
        for line in text.splitlines():
            fields = line.split("\0")
            if len(fields) != 4:
                raise JjError("jj operation ancestry output was ambiguous")
            operation_id, raw_parents, raw_description, raw_tags = fields
            parents = tuple(raw_parents.split()) if raw_parents else ()
            if (
                _OPERATION_ID.fullmatch(operation_id) is None
                or any(_OPERATION_ID.fullmatch(parent) is None for parent in parents)
                or operation_id in operations
            ):
                raise JjError("jj operation ancestry output was invalid")
            try:
                operation_description = json.loads(raw_description)
                tags = json.loads(raw_tags)
            except json.JSONDecodeError as exc:
                raise JjError("jj operation ancestry JSON was invalid") from exc
            if not isinstance(operation_description, str) or not isinstance(tags, str):
                raise JjError("jj operation ancestry text fields were invalid")
            operations[operation_id] = (parents, operation_description, tags)

        add_description = f"add workspace '{workspace_name}'"
        add_matches = [
            operation_id for operation_id, (parents, item_description, _tags) in operations.items()
            if parents == (pinned_operation_id,) and item_description == add_description
        ]
        expected_argv = ["jj", *_workspace_add_argv(
            source, destination, workspace_name, base_commit_ids,
            description, pinned_operation_id,
        )]
        checkout_matches: list[tuple[str, str]] = []
        for add_operation_id in add_matches:
            for operation_id, (parents, item_description, tags) in operations.items():
                if (
                    parents != (add_operation_id,)
                    or item_description
                    != f"create initial working-copy commit in workspace {workspace_name}"
                    or not tags.startswith("args: ")
                ):
                    continue
                try:
                    argv = shlex.split(tags[len("args: "):])
                except ValueError:
                    continue
                if argv == expected_argv:
                    checkout_matches.append((add_operation_id, operation_id))
        if len(checkout_matches) != 1:
            raise JjError(
                "exact workspace operation ancestry was not uniquely visible; retained state cannot be adopted"
            )
        return WorkspaceAddOperationProof(*checkout_matches[0])

    def materialization_plan(
        self, git_root: Path, base_commit_id: str, *, exact_root: Path,
    ) -> MaterializationPlan:
        """Read one metadata-only recursive Git tree with declared blob sizes."""
        if _COMMIT_ID.fullmatch(base_commit_id) is None:
            raise JjError("tracked-tree inspection requires a full commit ID")
        args = [
            "ls-tree", "-lrz", "--full-tree", "-r", "--end-of-options",
            base_commit_id,
        ]
        binding = inspect_git_marker(Path(exact_root))
        if binding is None or binding.target != Path(os.path.realpath(git_root)):
            raise JjError("tracked-tree Git backend differs from the exact source binding")
        raw = self._exact_git_bytes(
            Path(exact_root), args, limit=MAX_IMMUTABLE_TREE_BYTES,
        )
        blobs: list[MaterializationEntry] = []
        directories: set[str] = set()
        seen: set[str] = set()
        total = 0
        oid_length: int | None = None
        for record in raw.split(b"\0"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
                mode, kind, oid, raw_size = header.decode("ascii").split()
                path = raw_path.decode("utf-8")
                size = int(raw_size, 10)
            except (ValueError, UnicodeError) as exc:
                raise JjError("Git tree contains an unsupported metadata record") from exc
            parts = Path(path).parts
            if (
                not path or path.startswith("/") or ".." in parts or "//" in path or
                kind != "blob" or mode not in {"100644", "100755", "120000"} or
                size < 0 or size > 0x7fff_ffff_ffff_ffff or
                re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid) is None
            ):
                raise JjError("Git tree contains an unsupported entry")
            if oid_length is None:
                oid_length = len(oid)
            elif len(oid) != oid_length:
                raise JjError("Git tree mixes object ID algorithms")
            if path in seen or path in directories:
                raise JjError("Git tree contains colliding paths")
            for index in range(1, len(parts)):
                directory = "/".join(parts[:index])
                if directory in seen:
                    raise JjError("Git tree path collides with a file")
                directories.add(directory)
            seen.add(path)
            total += size
            if total > 0x7fff_ffff_ffff_ffff:
                raise JjError("Git tree declared byte count is unsupported")
            blobs.append(MaterializationEntry(path, mode, oid, size))
            if len(blobs) + len(directories) > MAX_IMMUTABLE_TREE_ENTRIES:
                raise JjError(
                    f"tracked revision exceeds {MAX_IMMUTABLE_TREE_ENTRIES} metadata entries"
                )
        entries = tuple(sorted(
            [MaterializationEntry(path, "040000", None, 0) for path in directories]
            + blobs,
            key=lambda item: item.path,
        ))
        digest = hashlib.sha256(b"asha-control-materialization-plan-v1\0")
        digest.update(base_commit_id.encode("ascii") + b"\0")
        for entry in entries:
            digest.update(entry.path.encode("utf-8") + b"\0")
            digest.update(entry.mode.encode("ascii") + b"\0")
            digest.update((entry.oid or "").encode("ascii") + b"\0")
            digest.update(str(entry.size).encode("ascii") + b"\0")
        return MaterializationPlan(
            base_commit_id=base_commit_id,
            digest=digest.hexdigest(), entries=entries,
            blob_count=len(blobs), directory_count=len(directories),
            total_blob_bytes=total,
        )

    def prove_context_compatibility(
        self, root: Path, git_root: Path, plan: MaterializationPlan, *,
        project_id: str, planned_context_paths: Sequence[str],
        private_directory_paths: Sequence[str],
    ) -> ContextCompatibilityProof:
        """Classify context paths from one immutable base and prove ignore coverage."""
        root = Path(root)
        git_root = Path(git_root)
        if not isinstance(plan, MaterializationPlan) or not project_id.strip():
            raise JjError("immutable context proof requires a bound plan and project identity")
        binding = inspect_git_marker(root)
        if binding is None or binding.target != Path(os.path.realpath(git_root)):
            raise JjError("immutable context proof Git backend differs from the source binding")
        entries = {entry.path: entry for entry in plan.entries}
        if len(entries) != len(plan.entries):
            raise JjError("immutable context proof received colliding plan entries")
        reusable_contract = {
            ".asha/config.json",
            "Memory/activeContext.md",
            "Memory/decisions.md",
        }

        def canonical_paths(values: Sequence[str], *, directories: bool) -> tuple[str, ...]:
            if isinstance(values, (str, bytes)):
                raise JjError("immutable context proof paths must be a sequence")
            normalized: list[str] = []
            for value in values:
                if not isinstance(value, str) or not value:
                    raise JjError("immutable context proof contains an invalid path")
                expected = value[:-1] if directories and value.endswith("/") else value
                if directories and not value.endswith("/"):
                    raise JjError("immutable private context directory must end with '/'")
                parts = Path(expected).parts
                if (
                    not expected or expected.startswith("/") or ".." in parts
                    or "//" in expected or "\x00" in expected
                    or os.path.normpath(expected) != expected
                ):
                    raise JjError("immutable context proof contains a non-canonical path")
                normalized.append(value)
                if len(normalized) > MAX_CONTEXT_PROOF_PATHS:
                    raise JjError("immutable context proof contains too many paths")
            result = tuple(sorted(normalized))
            if not result or len(set(result)) != len(result):
                raise JjError("immutable context proof paths must be nonempty and unique")
            try:
                encoded_size = sum(len(path.encode("utf-8")) + 1 for path in result)
            except UnicodeError as exc:
                raise JjError("immutable context proof path is not valid UTF-8") from exc
            if encoded_size > MAX_CONTEXT_PROOF_PATH_BYTES:
                raise JjError("immutable context proof paths exceed the byte limit")
            return result

        planned = canonical_paths(planned_context_paths, directories=False)
        private_directories = canonical_paths(private_directory_paths, directories=True)
        reusable = tuple(path for path in planned if path in reusable_contract)
        private = tuple(path for path in planned if path not in reusable_contract)
        if not private:
            raise JjError("immutable context proof has no controller-private file")
        for relative in (*planned, *(path[:-1] for path in private_directories)):
            parts = Path(relative).parts
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                entry = entries.get(parent)
                if entry is not None and entry.mode != "040000":
                    raise JjError(
                        f"immutable base context parent collides with a file: {parent}"
                    )
        for relative in (*private, *(path[:-1] for path in private_directories)):
            if relative in entries:
                raise JjError(
                    f"immutable base tracks a controller-private context path: {relative}"
                )

        reused: list[str] = []
        required: list[str] = [*private, *private_directories]
        for relative in reusable:
            entry = entries.get(relative)
            if entry is None:
                required.append(relative)
                continue
            if entry.mode != "100644" or entry.oid is None:
                raise JjError(
                    f"base-tracked reusable context must be a non-executable regular file: {relative}"
                )
            try:
                content = self._exact_git_bytes(
                    root, ["cat-file", "blob", entry.oid],
                    limit=min(entry.size + 1, MAX_TRACKED_BLOB_BYTES),
                )
                if len(content) != entry.size:
                    raise JjError(
                        f"base-tracked reusable context size changed: {relative}"
                    )
                validate_reusable_context_blob(
                    relative, content, project_id=project_id,
                )
            except ContextError as exc:
                raise JjError(str(exc)) from exc
            reused.append(relative)

        ignore_paths = {".gitignore"}
        for relative in required:
            parts = Path(relative.rstrip("/")).parts
            for index in range(1, len(parts)):
                ignore_paths.add("/".join((*parts[:index], ".gitignore")))
        ignore_entries = [
            entries[path] for path in sorted(ignore_paths)
            if path in entries and entries[path].mode in {"100644", "100755"}
        ]
        # Repository-local info/exclude and global excludes are mutable and do
        # not travel with the selected commit.  Keep them empty so they can
        # neither supply false durable coverage nor negate an immutable rule.
        info_exclude = b""
        required_tuple = tuple(sorted(required))
        payload = b"".join(path.encode("utf-8") + b"\0" for path in required_tuple)
        with tempfile.TemporaryDirectory(prefix="asha-context-proof-") as temporary:
            temporary_root = Path(temporary).resolve()
            os.chmod(temporary_root, 0o700)
            work = temporary_root / "work"
            metadata = temporary_root / "git"
            work.mkdir(mode=0o700)
            metadata.mkdir(mode=0o700)
            for relative in ("objects", "refs", "info"):
                (metadata / relative).mkdir(mode=0o700)
            (metadata / "HEAD").write_bytes(b"ref: refs/heads/proof\n")
            (metadata / "config").write_text(
                "[core]\n\trepositoryformatversion = 0\n\tbare = false\n",
                encoding="utf-8",
            )
            (metadata / "info" / "exclude").write_bytes(info_exclude)
            for entry in ignore_entries:
                assert entry.oid is not None
                content = self._exact_git_bytes(
                    root, ["cat-file", "blob", entry.oid],
                    limit=min(entry.size + 1, MAX_TRACKED_BLOB_BYTES),
                )
                if len(content) != entry.size:
                    raise JjError(f"immutable ignore file size changed: {entry.path}")
                target = work / entry.path
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.write_bytes(content)
                target.chmod(0o600)
            for relative in required_tuple:
                target = work / relative.rstrip("/")
                if relative.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            status, output, diagnostic = self._bound_git_status(
                work, metadata,
                ["check-ignore", "--no-index", "-z", "--stdin"],
                input_data=payload, limit=max(MAX_OUTPUT_BYTES, len(payload) + 1),
            )
        if status not in {0, 1}:
            detail = diagnostic[:4096].decode("utf-8", errors="replace").strip()
            raise JjError(
                f"immutable context ignore proof failed ({status}): {detail or 'no diagnostic'}"
            )
        try:
            matched = tuple(
                item.decode("utf-8") for item in output.split(b"\0") if item
            )
        except UnicodeError as exc:
            raise JjError("immutable context ignore proof returned non-UTF-8 paths") from exc
        if len(set(matched)) != len(matched) or any(path not in required_tuple for path in matched):
            raise JjError("immutable context ignore proof returned ambiguous paths")
        missing = tuple(sorted(set(required_tuple) - set(matched)))
        if missing:
            info_exclude_digest = hashlib.sha256(info_exclude).hexdigest()
            failure_digest = hashlib.sha256(
                b"asha-control-context-missing-positive-ignore-v1\0"
            )
            for value in (
                plan.base_commit_id, plan.digest, project_id,
                *planned, *private_directories, *sorted(reused), *required_tuple,
                *missing, info_exclude_digest,
            ):
                failure_digest.update(value.encode("utf-8") + b"\0")
            raise ContextCompatibilityError(MissingPositiveIgnoreEvidence(
                plan.base_commit_id, plan.digest, project_id, planned,
                private_directories, tuple(sorted(reused)), required_tuple,
                missing, info_exclude_digest, failure_digest.hexdigest(),
            ))
        digest = hashlib.sha256(b"asha-control-context-compatibility-v1\0")
        for value in (
            plan.base_commit_id, plan.digest, project_id,
            *planned, *private_directories, *sorted(reused), *required_tuple,
            hashlib.sha256(info_exclude).hexdigest(),
        ):
            digest.update(value.encode("utf-8") + b"\0")
        return ContextCompatibilityProof(
            plan.base_commit_id, plan.digest, project_id, planned,
            private_directories, tuple(sorted(reused)),
            required_tuple, hashlib.sha256(info_exclude).hexdigest(),
            digest.hexdigest(),
        )

    def expected_materialization(self, git_root: Path, base_commit_id: str
                                 ) -> dict[str, dict[str, Any]]:
        """Return the bounded Git-backed tree jj must materialize for ``base``."""
        if _COMMIT_ID.fullmatch(base_commit_id) is None:
            raise JjError("tracked-tree inspection requires a full commit ID")
        raw = self._run_bytes(
            "git", ["-C", str(git_root), "ls-tree", "-rz", "--full-tree", "-r", base_commit_id],
            limit=MAX_TREE_LIST_BYTES,
        )
        result: dict[str, dict[str, Any]] = {}
        total = 0
        for record in raw.split(b"\0"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
                mode, kind, oid = header.decode("ascii").split(" ")
                path = raw_path.decode("utf-8")
            except (ValueError, UnicodeError) as exc:
                raise JjError("Git tree contains an unsupported path or record") from exc
            parts = Path(path).parts
            if (not path or path.startswith("/") or ".." in parts or "//" in path or
                    kind != "blob" or mode not in {"100644", "100755", "120000"}):
                raise JjError("Git tree contains an unsupported entry")
            for index in range(1, len(parts)):
                directory = "/".join(parts[:index])
                existing = result.setdefault(directory, {"type": "directory"})
                if existing != {"type": "directory"}:
                    raise JjError("Git tree path collides with a file")
            content = self._run_bytes(
                "git", ["-C", str(git_root), "cat-file", "blob", oid],
                limit=MAX_TRACKED_BLOB_BYTES,
            )
            total += len(content)
            if total > MAX_TRACKED_TOTAL_BYTES:
                raise JjError("tracked revision exceeds the bounded materialization limit")
            if mode == "120000":
                try:
                    target = content.decode("utf-8")
                except UnicodeError as exc:
                    raise JjError("Git tree contains a non-UTF-8 symlink target") from exc
                fact = {"type": "symlink", "target": target}
            else:
                fact = {
                    "type": "file", "mode": 0o755 if mode == "100755" else 0o644,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
            if path in result:
                raise JjError("Git tree contains colliding paths")
            result[path] = fact
            if len(result) > MAX_MATERIALIZATION_ENTRIES:
                raise JjError(
                    f"tracked revision exceeds {MAX_MATERIALIZATION_ENTRIES} materialized entries"
                )
        return result

    def inspect_workspace(
        self, destination: Path, expected_name: str, *, snapshot: bool = False,
        require_empty: bool = True, exclude_control_transport: bool = False,
    ) -> WorkspaceIdentity:
        if exclude_control_transport and not snapshot:
            raise JjError("Control transport exclusion is valid only while snapshotting")
        command_options = (
            ["--config", 'snapshot.auto-track="~root:.asha"']
            if exclude_control_transport else []
        )
        prefix = [*command_options, "-R", str(destination)]
        if not snapshot:
            prefix.append("--ignore-working-copy")
        prefix.extend(["log", "-r", "@", "--no-graph"])
        change_id = self._one_line(
            self._run([*prefix, "-T", 'change_id ++ "\\n"']), "change ID", _CHANGE_ID
        )
        commit_id = self._one_line(
            self._run([*prefix, "-T", 'commit_id ++ "\\n"']), "working commit ID", _COMMIT_ID
        )
        parent_output = self._run([
            *prefix, "-T", 'parents.map(|p| p.commit_id()).join(" ") ++ "\\n"',
        ])
        parent_line = self._one_line(parent_output, "working commit parents")
        parents = tuple(parent_line.split(" "))
        if not parents or any(_COMMIT_ID.fullmatch(item) is None for item in parents):
            raise JjError("jj returned invalid working commit parents")
        description = self._run([*prefix, "-T", "description"]).rstrip("\n")
        if "\n" in description or "\r" in description:
            raise JjError("created workspace description was not one bounded line")
        diff_args = [*command_options, "-R", str(destination)]
        if not snapshot:
            diff_args.append("--ignore-working-copy")
        diff_args.extend(["diff", "-r", "@", "--summary"])
        if require_empty and self._run(diff_args):
            raise JjError("created workspace working change is not empty")
        registered = self.workspace_identities(destination)
        identity = registered.get(expected_name)
        if identity is None:
            raise JjError("created workspace is not registered under the expected name")
        if identity[:2] != (change_id, commit_id):
            raise JjError("created workspace registration identity disagrees with working copy")
        return WorkspaceIdentity(expected_name, change_id, commit_id, parents, description)

    def immutable_tree(self, repository: Path, commit_id: str) -> ImmutableTree:
        """Read one Git-backed jj commit's immutable tree without snapshotting.

        The tree digest is SHA-256 over compact canonical JSON for the sorted
        entries ``[path, mode, blob-id]``.  Git blob IDs cover regular-file
        content and symlink targets (mode ``120000``).  Directories are absent;
        jj conflicts and Git submodules (mode ``160000``) are refused because
        neither has one supported file-tree identity for Core sealing.
        """
        if _COMMIT_ID.fullmatch(commit_id) is None or set(commit_id) == {"0"}:
            raise JjError("immutable tree inspection requires a non-root full commit ID")
        repository = Path(repository)
        facts = self.preflight(repository)
        conflict = self._one_line(self._run([
            "-R", str(repository), "--ignore-working-copy", "log", "-r", commit_id,
            "--no-graph", "-T", 'conflict ++ "\\n"',
        ]), "immutable commit conflict state")
        if conflict not in {"true", "false"}:
            raise JjError("jj returned invalid immutable commit conflict state")
        if conflict == "true":
            raise JjError("immutable tree inspection refuses a conflicted jj commit")
        raw = self._bound_git_bytes(
            facts.root, facts.git_root, [
                "ls-tree", "-rz", "--full-tree", "--end-of-options", commit_id,
            ], limit=MAX_IMMUTABLE_TREE_BYTES,
        )
        entries: list[tuple[str, str, str]] = []
        seen_paths: set[str] = set()
        for record in raw.split(b"\0"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
                mode, kind, oid = header.decode("ascii").split(" ")
                path = raw_path.decode("utf-8")
            except (ValueError, UnicodeError) as exc:
                raise JjError("Git returned malformed immutable tree metadata") from exc
            parts = Path(path).parts
            if (
                not path or path.startswith("/") or "\\" in path or ".." in parts
                or "//" in path or path.endswith("/")
                or any(
                    unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                    for character in path
                )
            ):
                raise JjError("Git returned an unsafe immutable file path")
            if path in seen_paths:
                raise JjError("Git immutable tree contains a duplicate path")
            seen_paths.add(path)
            if mode == "160000":
                raise JjError("immutable tree inspection refuses Git submodules")
            if kind != "blob" or mode not in {"100644", "100755", "120000"}:
                raise JjError("Git immutable tree contains an unsupported entry")
            if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", oid) is None:
                raise JjError("Git immutable tree contains an invalid blob ID")
            entries.append((path, mode, oid))
            if len(entries) > MAX_IMMUTABLE_TREE_ENTRIES:
                raise JjError(
                    f"immutable tree exceeds {MAX_IMMUTABLE_TREE_ENTRIES} entries"
                )
        entries.sort(key=lambda item: item[0])
        canonical = json.dumps(
            entries, ensure_ascii=False, sort_keys=False, separators=(",", ":"),
        ).encode("utf-8")
        return ImmutableTree(
            commit_id=commit_id,
            digest=hashlib.sha256(canonical).hexdigest(),
            entries=tuple(entries),
        )

    def diff_summary(self, workspace_path: Path) -> DiffSummary:
        """Snapshot and summarize one exact workspace after an explicit user request."""
        try:
            workspace = Path(workspace_path)
            text = str(workspace)
            valid = (
                not any(unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                        for character in text)
                and workspace.is_absolute()
                and os.path.realpath(workspace) == text
                and not workspace.is_symlink()
                and workspace.is_dir()
            )
        except (OSError, TypeError, ValueError):
            valid = False
        if not valid:
            raise JjError(
                "diff refresh workspace must be its exact canonical directory root"
            )
        output = self._run([
            "-R", str(workspace), "diff", "--summary",
        ])
        safe = "".join(
            character if character in {"\n", "\t"} or character.isprintable() else "?"
            for character in output
        ).rstrip("\n")
        refreshed_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        return DiffSummary(summary=safe or "No changes.", refreshed_at=refreshed_at)

    def workspace_identities(self, repository: Path) -> dict[str, tuple[str, str]]:
        output = self._run([
            "-R", str(repository), "--ignore-working-copy", "workspace", "list",
            "--template", 'name ++ "\\t" ++ target.change_id() ++ "\\t" ++ target.commit_id() ++ "\\n"',
        ])
        result: dict[str, tuple[str, str]] = {}
        for line in output.splitlines():
            fields = line.split("\t")
            if (len(fields) != 3 or not fields[0] or _CHANGE_ID.fullmatch(fields[1]) is None or
                    _COMMIT_ID.fullmatch(fields[2]) is None or fields[0] in result):
                raise JjError("jj returned malformed workspace identity output")
            result[fields[0]] = (fields[1], fields[2])
        return result

    def abandon_empty_change(self, source: Path, change_id: str) -> bool:
        """Abandon Control's own change only when it is still empty.

        Rollback forgets a workspace whose working-copy commit carries the
        task goal as its description; jj keeps a described empty commit, which
        would otherwise litter the operator's log with one dead head per failed
        start. Returns True when the change was abandoned.
        """
        if _CHANGE_ID.fullmatch(change_id) is None:
            raise JjError("abandon requires a full change ID")
        output = self._run([
            "-R", str(source), "--ignore-working-copy", "log", "-r", change_id,
            "--no-graph", "-T", 'if(empty, "empty", "nonempty") ++ "\n"',
        ])
        lines = output.splitlines()
        if lines != ["empty"]:
            return False
        operation_id = self.pin_operation(source)
        self._run([
            "-R", str(source), "--ignore-working-copy",
            "--at-operation", operation_id, "abandon", "-r", change_id,
        ])
        return True

    def forget_workspace(self, source: Path, name: str) -> None:
        # Forget through the SOURCE repository, never the destination: the
        # destination is exactly the workspace rollback may be cleaning up,
        # and its .jj may be partial or gone.  jj 0.38 rewrites the colocated
        # Git index file's layout under --ignore-working-copy; staged content
        # is unchanged, and callers compare staged content, not raw bytes.
        operation_id = self.pin_operation(source)
        self._run([
            "-R", str(source), "--ignore-working-copy",
            "--at-operation", operation_id,
            "workspace", "forget", name,
        ])
