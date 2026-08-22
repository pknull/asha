"""Prepare isolated task workspaces and persist rollback-safe creation state."""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import ControlConfig, validate_workspace_root
from .context import (
    DIRECTORY_MODES, DYNAMIC_PRIVATE_CONTEXT_DIRECTORIES, build_context_plan,
    provision_context, read_published_snapshot,
)
from .jj import (
    ColocationIntentStore, ContextCompatibilityError, ContextCompatibilityProof,
    DEFAULT_BASE_REVSET,
    DefaultBaseResolution, JjAdapter, JjError, MaterializationEntry,
    MaterializationPlan, MAX_IMMUTABLE_TREE_ENTRIES, MAX_MATERIALIZATION_ENTRIES,
    MAX_TRACKED_BLOB_BYTES, MAX_TRACKED_TOTAL_BYTES, inspect_git_marker,
    RepositoryPreEnableBinding, inspect_pre_enable_binding,
    require_pre_enable_binding,
)
from .model import (
    GIT_OBJECT_ID_PATTERN, TASK_CONTRACT, canonical_uuid, validate_task,
    validate_task_slug,
)
from .store import (
    TaskStore, TransactionCoordinator, StoreError, task_digest,
    validate_task_paths,
)
from .sources import ValidatedPrRemote
from .transaction import (
    CreationJournalStore, JOURNAL_CONTRACT, JOURNAL_V1_CONTRACT, JournalError,
    MaterializationOwnershipStore, MAX_JOURNAL_BYTES, RECOVERY_ADOPTION_CONTRACT,
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
    """Legacy undomained repository UUID retained only for replay regression tests."""
    return (
        f"{repository_identity[5:13]}-{repository_identity[13:17]}-"
        f"{repository_identity[17:21]}-{repository_identity[21:25]}-"
        f"{repository_identity[25:37]}"
    )


class PreparationError(ValueError):
    pass


class PreparationPrerequisiteError(PreparationError):
    """Repairable immutable-base refusal with the planning facts still bound."""

    def __init__(
        self, *, request: "PrepareRequest", base_explicit: bool,
        existing_jj: bool, source_binding: RepositoryPreEnableBinding,
        selected_base: str, resolved_base_commit_id: str,
        default_base_resolution: DefaultBaseResolution | None,
        materialization_plan: MaterializationPlan,
        source_object_available: bool,
        pr_remote_config_digest: str | None,
        error: ContextCompatibilityError,
    ) -> None:
        self.request = request
        self.base_explicit = base_explicit
        self.existing_jj = existing_jj
        self.source_binding = source_binding
        self.selected_base = selected_base
        self.resolved_base_commit_id = resolved_base_commit_id
        self.default_base_resolution = default_base_resolution
        self.materialization_plan = materialization_plan
        self.source_object_available = source_object_available
        self.pr_remote_config_digest = pr_remote_config_digest
        self.evidence = error.evidence
        super().__init__(
            f"{error}; add /.asha/control-task.json to .gitignore, commit the "
            "rule or select a commit that contains it, then retry "
            f"(selected immutable base {resolved_base_commit_id}; pre-enable "
            "preflight refused; repository and task state were unchanged)"
        )


@dataclass(frozen=True)
class PrepareRequest:
    repository: Path
    requested_base: str = DEFAULT_BASE_REVSET
    task_id: str = ""
    slug: str = ""
    label: str = ""
    source: dict[str, Any] = field(default_factory=lambda: {
        "kind": "ad-hoc", "number": None, "url": None,
    })
    resolved_base_commit_id: str | None = None
    github_title: str | None = None
    pr_remote: ValidatedPrRemote | None = None
    expected_default_commit_id: str | None = None


@dataclass(frozen=True)
class PlainGitPreEnablePlan:
    """Read-only facts proven before Control enables jj in a plain Git root."""

    repository_identity: str
    repo_key: str
    destination: Path
    source_binding: RepositoryPreEnableBinding
    selected_base: str
    resolved_base_commit_id: str | None
    default_base_deferred: bool
    materialization_plan: MaterializationPlan | None
    context_compatibility: ContextCompatibilityProof | None = None
    default_base_resolution: DefaultBaseResolution | None = None
    source_object_available: bool = True
    pr_remote_config_digest: str | None = None


def revalidate_plain_git_pre_enable_plan(
    plan: PlainGitPreEnablePlan, *, jj: JjAdapter | None = None,
) -> None:
    """Recheck the exact source/Git marker authorized by read-only planning."""
    if not isinstance(plan, PlainGitPreEnablePlan):
        raise PreparationError("plain-Git pre-enable plan is missing or invalid")
    try:
        require_pre_enable_binding(plan.source_binding.root, plan.source_binding)
        if plan.default_base_resolution is not None:
            adapter = jj or JjAdapter()
            observed_default = adapter.resolve_default_base(plan.source_binding.root)
            if observed_default != plan.default_base_resolution:
                raise JjError(
                    "default base changed after preflight; review the new default "
                    "or pass an explicit --base"
                )
        if (
            plan.materialization_plan is not None
            and plan.context_compatibility is not None
            and plan.source_object_available
        ):
            adapter = jj or JjAdapter()
            observed = adapter.prove_context_compatibility(
                plan.source_binding.root, plan.source_binding.git_binding.target,
                plan.materialization_plan,
                project_id=plan.context_compatibility.project_id,
                planned_context_paths=plan.context_compatibility.planned_context_paths,
                private_directory_paths=plan.context_compatibility.private_directory_paths,
            )
            if observed != plan.context_compatibility:
                raise JjError("immutable context compatibility evidence changed")
        elif not plan.source_object_available:
            if plan.pr_remote_config_digest is None:
                raise JjError("isolated PR prerequisite plan lacks remote binding")
            adapter = jj or JjAdapter()
            configured = adapter.git_remote_configuration(plan.source_binding.root)
            if configured.config_digest != plan.pr_remote_config_digest:
                raise JjError("Git remote configuration changed after PR prerequisite proof")
    except JjError as exc:
        raise PreparationError(
            f"{exc} (pre-enable plan invalidated; no replacement was initialized)"
        ) from exc


def preflight_plain_git_enablement(
    config: ControlConfig, request: PrepareRequest, *, jj: JjAdapter,
    base_explicit: bool, existing_jj: bool = False,
) -> PlainGitPreEnablePlan:
    """Run every feasible source/task refusal before durable colocation."""
    source = Path(request.repository)
    try:
        canonical_uuid(request.task_id)
        slug = validate_task_slug(request.slug)
        if not request.label or len(request.label) > 200:
            raise ValueError("task label must contain 1-200 characters")
        if not isinstance(request.source, dict):
            raise ValueError("task source metadata must be an object")
        # The repository/workspace namespace policy does not depend on Memory
        # or jj. Apply it first through a non-existent deterministic probe path
        # so an unsafe source can never reach a durable enablement attempt.
        validate_task_paths(
            config, source,
            config.workspace_root / ".pre-enable" / request.task_id / slug,
        )
        snapshot = read_published_snapshot(source)
        source_binding = inspect_pre_enable_binding(source)
        git_binding = source_binding.git_binding
        repository_identity, repo_key = derive_repository_identity(
            snapshot.project_id, source, git_binding.target,
        )
        destination = config.workspace_root / repo_key / slug
        validate_task_paths(config, source, destination)
        _validate_layout(config, source, destination, repo_key, slug)
        if destination.exists() or destination.is_symlink():
            raise ValueError("workspace destination already exists")
        missing_ancestors = _count_missing_destination_ancestors(
            config, source, destination, repo_key, slug,
        )
        if missing_ancestors > 8:
            raise ValueError(
                "workspace destination requires more than eight created ancestors"
            )
        selected_base = request.requested_base
        default_base_resolution = None
        if request.resolved_base_commit_id is not None:
            resolved = request.resolved_base_commit_id
            if GIT_OBJECT_ID_PATTERN.fullmatch(resolved) is None:
                raise ValueError("resolved base commit ID must be a full Git object ID")
        elif not base_explicit:
            default_base_resolution = jj.resolve_default_base(source)
            selected_base = default_base_resolution.references[0]
            resolved = default_base_resolution.commit_id
            if (
                request.expected_default_commit_id is not None
                and request.expected_default_commit_id != resolved
            ):
                raise ValueError(
                    "default base changed after the TUI preview; review the new "
                    "default or select/type an explicit --base"
                )
        elif existing_jj:
            resolved = jj.resolve_base(source, request.requested_base)
        else:
            resolved = jj.resolve_git_commit(source, request.requested_base)
        marker = {
            "contract": "asha.control-task-context.v1", "task_id": request.task_id,
            "repository": {"root": str(source), "identity": repository_identity},
            "jj": {
                "workspace_name": f"asha-{slug}-{request.task_id[:8]}",
                "workspace_path": str(destination), "change_id": "k" * 32,
                "working_commit_id": "f" * 64,
            },
        }
        capacity_context_plan = build_context_plan(
            source, destination, marker, snapshot=snapshot,
        )
        capacity_plan = _planned_manifest(capacity_context_plan)
        materialization = None
        context_compatibility = None
        source_object_available = True
        pr_remote_config_digest = None

        def immutable_proof(proof_root: Path) -> tuple[
            MaterializationPlan, ContextCompatibilityProof,
        ]:
            nonlocal materialization
            proof_binding = inspect_pre_enable_binding(proof_root)
            materialization = jj.materialization_plan(
                proof_binding.git_binding.target, resolved, exact_root=proof_root,
            )
            if materialization.entry_count > MAX_IMMUTABLE_TREE_ENTRIES:
                raise ValueError(
                    f"tracked revision exceeds the {MAX_IMMUTABLE_TREE_ENTRIES}-entry capacity"
                )
            proof = jj.prove_context_compatibility(
                proof_root, proof_binding.git_binding.target, materialization,
                project_id=snapshot.project_id,
                planned_context_paths=tuple(capacity_context_plan),
                private_directory_paths=DYNAMIC_PRIVATE_CONTEXT_DIRECTORIES,
            )
            return materialization, proof

        if request.source.get("kind") == "pr":
            remote = request.pr_remote
            number = request.source.get("number")
            if remote is None or type(number) is not int or not 1 <= number <= 9_999_999_999:
                raise ValueError("pull-request prerequisite proof lacks a validated remote")
            try:
                local_commit = jj.resolve_git_commit(source, resolved)
            except JjError:
                source_object_available = False
                pr_remote_config_digest = remote.config_digest
                with jj.prerequisite_pr_head(
                    source, remote.url, f"pull/{number}/head",
                    transport=remote.transport,
                    config_digest=remote.config_digest,
                    expected_commit_id=resolved,
                ) as proof_root:
                    materialization, context_compatibility = immutable_proof(proof_root)
            else:
                if local_commit != resolved:
                    raise ValueError("local pull-request object differs from selected head")
                materialization, context_compatibility = immutable_proof(source)
        else:
            materialization, context_compatibility = immutable_proof(source)
        materialization_record = (
            materialization.record() if materialization is not None else {
                "contract": "asha.control-materialization-plan.v1",
                "base_commit_id": "f" * 40, "digest": "f" * 64,
                "blob_count": MAX_IMMUTABLE_TREE_ENTRIES,
                "directory_count": 0,
                "entry_count": MAX_IMMUTABLE_TREE_ENTRIES,
                "total_blob_bytes": MAX_TRACKED_TOTAL_BYTES,
            }
        )
        prospective_journal = {
            "contract": JOURNAL_CONTRACT, "task_id": request.task_id,
            "invocation_id": "f" * 32, "phase": "intent",
            "launch_attempted": False,
            "config": {
                "workspace_root": str(config.workspace_root),
                "tasks_dir": str(config.tasks_dir),
                "runtime_dir": str(config.runtime_dir),
            },
            "repository": {
                "root": str(source), "identity": repository_identity,
                "git_root": str(git_binding.target), "repo_key": repo_key,
            },
            "task": {
                "record_path": str(config.tasks_dir / f"{request.task_id}.json"),
                "slug": slug, "label": request.label,
                "digest": None, "failure": None,
            },
            "workspace": {
                "path": str(destination),
                "name": f"asha-{slug}-{request.task_id[:8]}",
                "root_fact": None, "created_parents": [],
            },
            "jj": {
                "pinned_operation_id": "f" * 128,
                "base_commit_id": resolved or "f" * 40,
                "change_id": None, "working_commit_id": None,
                "description": request.label, "registration_state": "absent",
                "last_registration": None,
            },
            "materialization_plan": materialization_record,
            "materialization_ownership": None, "recovery_owned": None,
            "planned_context": None, "context_owned": {},
            "removal": {
                "entries_removed": 0, "root_removed": False,
                "parents_removed": 0,
            },
        }
        _ensure_creation_journal_capacity(prospective_journal, capacity_plan)
    except ContextCompatibilityError as exc:
        assert materialization is not None
        assert resolved is not None
        raise PreparationPrerequisiteError(
            request=request, base_explicit=base_explicit, existing_jj=existing_jj,
            source_binding=source_binding, selected_base=selected_base,
            resolved_base_commit_id=resolved,
            default_base_resolution=default_base_resolution,
            materialization_plan=materialization,
            source_object_available=source_object_available,
            pr_remote_config_digest=pr_remote_config_digest,
            error=exc,
        ) from exc
    except (OSError, ValueError, JjError, StoreError) as exc:
        raise PreparationError(
            f"{exc} (pre-enable preflight refused; repository and task state were unchanged)"
        ) from exc
    return PlainGitPreEnablePlan(
        repository_identity, repo_key, destination, source_binding,
        selected_base, resolved, default_base_deferred=False,
        materialization_plan=materialization,
        context_compatibility=context_compatibility,
        default_base_resolution=default_base_resolution,
        source_object_available=source_object_available,
        pr_remote_config_digest=pr_remote_config_digest,
    )


def revalidate_pr_source_proof_after_fetch(
    plan: PlainGitPreEnablePlan, *, jj: JjAdapter | None = None,
) -> None:
    """Bind fetched source objects to the already-authorized PR proof before import."""
    if (
        not isinstance(plan, PlainGitPreEnablePlan)
        or plan.materialization_plan is None
        or plan.context_compatibility is None
        or plan.resolved_base_commit_id is None
    ):
        raise PreparationError("pull-request start lacks an immutable prerequisite proof")
    adapter = jj or JjAdapter()
    root = plan.source_binding.root
    try:
        current = inspect_pre_enable_binding(root)
        expected_git = plan.source_binding.git_binding
        if (
            current.git_binding.target != expected_git.target
            or current.git_binding.target_fact != expected_git.target_fact
        ):
            raise JjError("Git backend changed after pull-request prerequisite proof")
        snapshot = read_published_snapshot(root)
        if snapshot.project_id != plan.context_compatibility.project_id:
            raise JjError("project identity changed after pull-request prerequisite proof")
        observed_plan = adapter.materialization_plan(
            current.git_binding.target, plan.resolved_base_commit_id,
            exact_root=root,
        )
        if observed_plan != plan.materialization_plan:
            raise JjError("fetched pull-request materialization differs from prerequisite proof")
        observed_proof = adapter.prove_context_compatibility(
            root, current.git_binding.target, observed_plan,
            project_id=plan.context_compatibility.project_id,
            planned_context_paths=plan.context_compatibility.planned_context_paths,
            private_directory_paths=plan.context_compatibility.private_directory_paths,
        )
        if observed_proof != plan.context_compatibility:
            raise JjError("fetched pull-request context differs from prerequisite proof")
    except (OSError, ValueError, JjError, StoreError) as exc:
        if isinstance(exc, PreparationError):
            raise
        raise PreparationError(
            f"{exc} (pull-request source proof invalidated before jj import; "
            "no task or workspace state was created)"
        ) from exc


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


def _open_relative_parent(root_fd: int, relative: str) -> tuple[int, str]:
    parts = Path(relative).parts
    if not parts or relative.startswith("/") or ".." in parts:
        raise PreparationError("materialization plan contains an unsafe path")
    parent_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = child
        return parent_fd, parts[-1]
    except BaseException:
        os.close(parent_fd)
        raise


def _git_blob_digest(oid: str, size: int, chunks) -> str:
    digest = hashlib.sha1() if len(oid) == 40 else hashlib.sha256()
    digest.update(f"blob {size}\0".encode("ascii"))
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def _verify_plan_entry(
    root_fd: int, entry: MaterializationEntry, expected_fact: list[int] | None = None,
    *, require_ownership: bool = True,
) -> list[int]:
    parent_fd, name = _open_relative_parent(root_fd, entry.path)
    try:
        return _verify_plan_entry_at(
            parent_fd, name, entry, expected_fact,
            require_ownership=require_ownership,
        )
    finally:
        os.close(parent_fd)


def _verify_plan_entry_at(
    parent_fd: int, name: str, entry: MaterializationEntry,
    expected_fact: list[int] | None = None, *, require_ownership: bool = True,
) -> list[int]:
    """Authenticate an entry through the parent descriptor used by its caller."""
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise PreparationError(
            f"jj materialization is missing selected revision path {entry.path}; preserved"
        ) from exc
    fact = [
        metadata.st_dev, metadata.st_ino,
        stat.S_IMODE(metadata.st_mode), metadata.st_uid,
    ]
    if expected_fact is not None and fact != expected_fact:
        raise PreparationError(
            f"workspace entry identity changed at {entry.path}; preserved"
        )
    if require_ownership and metadata.st_uid != os.geteuid():
        raise PreparationError(
            f"workspace entry is not controller-owned at {entry.path}; preserved"
        )
    if entry.type == "directory":
        if not stat.S_ISDIR(metadata.st_mode):
            raise PreparationError(
                f"jj materialization differs from selected revision at {entry.path}; preserved"
            )
        visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not _same_inode(metadata, visible) or
            metadata.st_mode != visible.st_mode or metadata.st_uid != visible.st_uid
        ):
            raise PreparationError("workspace directory changed during ownership inspection")
        return fact
    if entry.type == "symlink":
        if not stat.S_ISLNK(metadata.st_mode):
            raise PreparationError(
                f"jj materialization differs from selected revision at {entry.path}; preserved"
            )
        target = os.readlink(name, dir_fd=parent_fd)
        raw_target = os.fsencode(target)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not _same_inode(metadata, after) or
            metadata.st_mode != after.st_mode or metadata.st_uid != after.st_uid
        ):
            raise PreparationError("workspace symlink changed during ownership inspection")
        if (
            len(raw_target) != entry.size or entry.oid is None or
            _git_blob_digest(entry.oid, entry.size, (raw_target,)) != entry.oid
        ):
            raise PreparationError(
                f"jj materialization differs from selected revision at {entry.path}; preserved"
            )
        return fact
    if (
        not stat.S_ISREG(metadata.st_mode) or
        (require_ownership and metadata.st_nlink != 1)
    ):
        raise PreparationError(
            f"jj materialization differs from selected revision at {entry.path}; preserved"
        )
    executable = bool(metadata.st_mode & 0o111)
    if (
        metadata.st_size != entry.size or entry.oid is None or
        executable != (entry.mode == "100755")
    ):
        raise PreparationError(
            f"jj materialization differs from selected revision at {entry.path}; preserved"
        )
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) |
        getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        before = (
            opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid,
            opened.st_nlink, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns,
        )
        if not _same_inode(metadata, opened) or not stat.S_ISREG(opened.st_mode):
            raise PreparationError("workspace entry changed during ownership inspection")
        remaining = entry.size

        def chunks():
            nonlocal remaining
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise PreparationError("workspace file shortened during ownership inspection")
                remaining -= len(chunk)
                yield chunk

        actual_oid = _git_blob_digest(entry.oid, entry.size, chunks())
        if os.read(descriptor, 1):
            raise PreparationError("workspace file grew during ownership inspection")
        after = os.fstat(descriptor)
        after_fact = (
            after.st_dev, after.st_ino, after.st_mode, after.st_uid,
            after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
        )
        if before != after_fact:
            raise PreparationError("workspace file changed during ownership inspection")
        visible = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not _same_inode(opened, visible) or
            opened.st_mode != visible.st_mode or opened.st_uid != visible.st_uid
        ):
            raise PreparationError("workspace entry changed during ownership inspection")
    finally:
        os.close(descriptor)
    if actual_oid != entry.oid:
        raise PreparationError(
            f"jj materialization differs from selected revision at {entry.path}; preserved"
        )
    return fact


def _workspace_paths(root_fd: int, maximum: int) -> set[str]:
    paths: set[str] = set()

    def visit(directory_fd: int, prefix: str) -> None:
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            raise PreparationError(f"cannot enumerate task workspace: {exc}") from exc
        for name in names:
            relative = f"{prefix}/{name}" if prefix else name
            paths.add(relative)
            if len(paths) > maximum:
                raise PreparationError("workspace contains too many entries; preserved")
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child)
                    visible = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False,
                    )
                    if not _same_inode(metadata, opened) or not _same_inode(opened, visible):
                        raise PreparationError("workspace directory changed during inspection")
                    visit(child, relative)
                finally:
                    os.close(child)

    visit(root_fd, "")
    return paths


def _selected_fact(root_fd: int, relative: str, budget: list[int]) -> dict[str, Any]:
    parent_fd, name = _open_relative_parent(root_fd, relative)
    try:
        return _file_fact_at(parent_fd, name, budget)
    finally:
        os.close(parent_fd)


def _verify_plan_materialization(
    destination: Path,
    source: Path,
    plan: MaterializationPlan,
    *,
    expected_root: dict[str, Any] | None = None,
    expected_facts: list[list[int]] | None = None,
    other_owned: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[list[int]], dict[str, dict[str, Any]], dict[str, int]]:
    """Stream-verify Git objects while retaining only fixed-width inode facts."""
    if any(entry.path == ".jj" or entry.path.startswith(".jj/") for entry in plan.entries):
        raise PreparationError("selected revision tracks the reserved .jj path")
    if expected_facts is not None and len(expected_facts) != plan.entry_count:
        raise PreparationError("ownership sidecar count differs from materialization plan; preserved")
    root_fd = _open_absolute_directory(destination)
    try:
        root_metadata = os.fstat(root_fd)
        if root_metadata.st_uid != os.geteuid():
            raise PreparationError("task workspace is not owned by the effective user")
        root_fact = _inode_fact(root_metadata)
        if expected_root is not None and root_fact != expected_root:
            raise PreparationError("task workspace root identity changed; preserved")
        extra = other_owned or {}
        allowed = {entry.path for entry in plan.entries} | set(_JJ_PATHS) | set(extra)
        actual_paths = _workspace_paths(root_fd, len(allowed))
        if actual_paths != allowed:
            raise PreparationError(
                "workspace contains content outside the expected jj materialization; preserved"
            )
        facts = [
            _verify_plan_entry(
                root_fd, entry,
                None if expected_facts is None else expected_facts[index],
            )
            for index, entry in enumerate(plan.entries)
        ]
        budget = [0]
        private = {
            relative: _selected_fact(root_fd, relative, budget)
            for relative in sorted(_JJ_PATHS)
        }
        for relative, kind in _JJ_PATHS.items():
            if private[relative]["type"] != kind:
                raise PreparationError("jj working-copy binding has an unexpected type; preserved")
        for relative, expected in extra.items():
            if _selected_fact(root_fd, relative, budget) != expected:
                raise PreparationError(f"workspace entry changed at {relative}; preserved")
    finally:
        os.close(root_fd)
    expected_repo = str(source / ".jj" / "repo").encode("utf-8")
    if _read_small_exact(destination / ".jj" / "repo") != expected_repo:
        raise PreparationError("jj workspace repository binding is foreign; preserved")
    if _read_small_exact(destination / ".jj" / "working_copy" / "type") != b"local":
        raise PreparationError("jj workspace working-copy binding is foreign; preserved")
    return facts, private, root_fact


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
    current = Path("/")
    managed_start = len(config.workspace_root.parts) - 2
    try:
        for index, part in enumerate(parts):
            try:
                child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                return len(parts) - index
            current /= part
            metadata = os.fstat(child)
            if index >= managed_start:
                if metadata.st_uid != os.geteuid():
                    os.close(child)
                    raise PreparationError(
                        "workspace destination parent is not owned by the effective user: "
                        f"{current}"
                    )
                if stat.S_IMODE(metadata.st_mode) != 0o700:
                    os.close(child)
                    raise PreparationError(
                        f"workspace destination parent must have mode 0700: {current}"
                    )
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
    if journal["contract"] == JOURNAL_CONTRACT:
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
        if "workspace_add_operation_id" in maximum["jj"]:
            maximum["jj"].update({
                "workspace_add_operation_id": "f" * 128,
                "checkout_operation_id": "f" * 128,
            })
        count = maximum["materialization_plan"]["entry_count"]
        maximum["materialization_ownership"] = {
            "sidecar": {
                "contract": "asha.control-materialization-ownership.v1",
                "path": str(
                    Path(maximum["config"]["tasks_dir"]).parent / "transactions" /
                    f"{maximum['task_id']}.ownership"
                ),
                "digest": "f" * 64,
                "plan_digest": maximum["materialization_plan"]["digest"],
                "entry_count": count, "size": 64 + count * 32 + 32,
                "state": "bound", "file_fact": dict(_MAX_FACT),
            },
            "private": _worst_private_ownership(),
        }
        maximum["recovery_owned"] = copy.deepcopy(_worst_private_ownership())
        maximum["planned_context"] = copy.deepcopy(planned_context)
        maximum["context_owned"] = {
            path: dict(_MAX_FACT) for path in planned_context
        }
        maximum["removal"] = {
            "entries_removed": count + len(_JJ_PATHS) + len(planned_context),
            "root_removed": True, "parents_removed": 8,
        }
        raw = json.dumps(
            maximum, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        if len(raw) > MAX_JOURNAL_BYTES:
            raise PreparationError(
                f"creation journal worst case exceeds the {MAX_JOURNAL_BYTES}-byte capacity"
            )
        return len(raw)

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


def _attempt_mark_preserved(
    journals: CreationJournalStore, tasks: TaskStore, journal: dict[str, Any],
) -> BaseException | None:
    try:
        _mark_preserved(journals, tasks, journal)
    except BaseException as exc:
        return exc
    return None


def _bounded_exception_detail(exc: BaseException, maximum: int = 240) -> str:
    detail = " ".join(str(exc).split()) or exc.__class__.__name__
    if len(detail) > maximum:
        detail = detail[:maximum - 3] + "..."
    return f"{exc.__class__.__name__}: {detail}"


def _note_preservation_failure(
    original: BaseException, persistence_error: BaseException,
) -> None:
    try:
        original.add_note(
            "Control could not confirm durable v2 preservation after a secondary "
            f"persistence failure ({_bounded_exception_detail(persistence_error)}); "
            "inspect the journal and task record"
        )
    except BaseException:
        # A diagnostic note is subordinate to the already-caught signal.
        pass


def v2_retention_diagnostic(
    task_id: str, journal: dict[str, Any], reason: str | None = None,
) -> str:
    prefix = f"{reason}; " if reason else ""
    repository = journal["repository"]["root"]
    workspace = journal["workspace"]["path"]
    created_parents = journal["workspace"].get("created_parents", [])
    parent_paths = ", ".join(item["path"] for item in created_parents) or "none recorded"
    inspection = (
        "manual inspection and cleanup required: inspect registrations with "
        f"`jj -R {shlex.quote(repository)} workspace list`, inspect workspace path "
        f"`{workspace}`, and inspect created-parent residue ({parent_paths})"
    )
    prune_proven = bool(
        journal["workspace"].get("root_fact") is not None and
        not created_parents and
        ".asha/control-task.json" in journal.get("context_owned", {})
    )
    cleanup = (
        f"; the existing prune preconditions are durably recorded, so after "
        f"inspection archive with `asha task archive {task_id}`, then run "
        f"`asha task prune {task_id} --yes` for explicit user-confirmed cleanup"
        if prune_proven else
        "; automatic prune eligibility is not durably proven; do not assume "
        "the archived-task cleanup route can remove this partial state"
    )
    return (
        f"{prefix}v2 automatic recovery retained the jj workspace registration "
        "and all workspace/root filesystem state because neither name-based "
        "forget nor filesystem deletion has an atomic identity predicate; "
        f"{inspection}{cleanup}"
    )


def _v2_mutation_may_exist(journal: dict[str, Any], destination: Path) -> bool:
    if journal["contract"] != JOURNAL_CONTRACT:
        return False
    if journal["workspace"]["created_parents"]:
        return True
    try:
        if destination.exists() or destination.is_symlink():
            return True
    except OSError:
        return True
    if journal["jj"]["registration_state"] != "absent":
        return True
    return journal["phase"] not in {"intent", "task-recorded"}


_REMOVAL_JOURNAL_BATCH = 64


def _remove_owned_tree(
    destination: Path, journal: dict[str, Any], journals: CreationJournalStore,
    failure_injector: Callable[[str], None] | None,
) -> None:
    """Apply the frozen v1 recovery removal contract."""
    if journal["contract"] != JOURNAL_V1_CONTRACT:
        raise PreparationError("automatic workspace removal is unavailable for v2; preserved")
    expected = _owned_manifest(journal)
    removed = journal["removal"]["entries_removed"]
    ordered = sorted(
        (path for path in expected if expected[path]["type"] != "directory"),
        key=lambda path: (path.count("/"), path), reverse=True,
    ) + sorted(
        (path for path in expected if expected[path]["type"] == "directory"),
        key=lambda path: (path.count("/"), path), reverse=True,
    )
    if removed > len(ordered):
        raise PreparationError("workspace removal journal exceeds its owned manifest; preserved")
    root_fd = _open_absolute_directory(destination)
    try:
        if _inode_fact(os.fstat(root_fd)) != journal["workspace"]["root_fact"]:
            raise PreparationError("workspace root identity changed; preserved")
        pending = 0
        dirty_parents: dict[str, None] = {}

        def sync_parents() -> None:
            for relative in list(dirty_parents):
                fd = os.dup(root_fd)
                try:
                    try:
                        for part in Path(relative).parts:
                            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=fd)
                            os.close(fd)
                            fd = child
                    except FileNotFoundError:
                        continue
                    os.fsync(fd)
                finally:
                    os.close(fd)
            dirty_parents.clear()

        def checkpoint(force: bool = False) -> None:
            nonlocal pending
            journal["removal"]["entries_removed"] = removed
            if pending and (force or pending >= _REMOVAL_JOURNAL_BATCH):
                sync_parents()
                journals.save(journal, expected_phase=journal["phase"])
                pending = 0

        while removed < len(ordered):
            relative = ordered[removed]
            parent_fd, name = _open_relative_parent(root_fd, relative)
            try:
                try:
                    os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    removed += 1
                    pending += 1
                    checkpoint()
                    continue
                actual = _file_fact_at(parent_fd, name, [0])
                if actual != expected[relative]:
                    checkpoint(force=True)
                    raise PreparationError(f"workspace entry changed at {relative}; preserved")
                if actual["type"] == "directory":
                    os.rmdir(name, dir_fd=parent_fd)
                else:
                    os.unlink(name, dir_fd=parent_fd)
                dirty_parents[str(Path(relative).parent)] = None
            finally:
                os.close(parent_fd)
            if failure_injector is not None:
                failure_injector(f"removed:{relative}")
            removed += 1
            pending += 1
            checkpoint()
        checkpoint(force=True)
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


def _rollback_locked_impl(
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
        if journal["contract"] == JOURNAL_CONTRACT:
            raise PreparationError(v2_retention_diagnostic(task_id, journal))
        raise PreparationError("creation transaction was preserved for explicit recovery")

    name = journal["workspace"]["name"]
    try:
        retain_v2 = _v2_mutation_may_exist(journal, destination)
    except BaseException as exc:
        if journal["contract"] != JOURNAL_CONTRACT:
            raise
        persistence_error = _attempt_mark_preserved(journals, tasks, journal)
        if persistence_error is not None:
            # The outer mutation-possible envelope owns the final retry and
            # decides whether durable uncertainty remains.
            raise
        if not isinstance(exc, Exception):
            raise
        raise PreparationError(v2_retention_diagnostic(
            task_id, journal, f"retained-state evidence read failed: {exc}",
        )) from exc
    if retain_v2:
        try:
            registrations = adapter.workspace_identities(source)
            observed_registration = registrations.get(name)
            if observed_registration is None:
                reason = "jj workspace registration was not visible during inspection"
            else:
                reason = (
                    "jj workspace registration was observed and retained because "
                    "name-only forget is not identity-safe"
                )
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
            registration_matches = observed_registration is not None and not (
                (known_tuple is not None and observed_registration != known_tuple) or
                (recorded is not None and observed_registration != recorded)
            )
            if observed_registration is not None and not registration_matches:
                reason = "jj workspace registration may be foreign and was retained"
            elif observed_registration is not None:
                if not destination.is_dir() or destination.is_symlink():
                    reason = (
                        "workspace destination is not the exact inspectable directory; "
                        "registration was retained"
                    )
                else:
                    observed_identity = adapter.inspect_workspace(
                        destination, name, require_empty=False,
                    )
                    if (
                        observed_identity.parent_commit_ids !=
                        (journal["jj"]["base_commit_id"],) or
                        observed_identity.description != journal["jj"]["description"] or
                        observed_registration != (
                            observed_identity.change_id, observed_identity.commit_id,
                        )
                    ):
                        reason = (
                            "jj workspace registration identity may be foreign and "
                            "was retained"
                        )
        except BaseException as exc:
            persistence_error = _attempt_mark_preserved(journals, tasks, journal)
            if persistence_error is not None:
                # The outer mutation-possible envelope owns the final retry and
                # decides whether durable uncertainty remains.
                raise
            if not isinstance(exc, Exception):
                raise
            raise PreparationError(v2_retention_diagnostic(
                task_id, journal, f"retained-state inspection failed: {exc}",
            )) from exc
        _mark_preserved(journals, tasks, journal)
        raise PreparationError(v2_retention_diagnostic(task_id, journal, reason))

    registrations = adapter.workspace_identities(source)
    observed_registration = registrations.get(name)

    if journal["contract"] == JOURNAL_CONTRACT:
        # The only v2 state reaching this branch has no durable or live
        # indication that a workspace/root filesystem mutation can exist.
        # Advance its journal to a clean terminal state without entering any
        # v1 filesystem-removal routine.
        if journal["phase"] not in {"rollback-intent", "workspace-forgotten"}:
            _save_phase(journals, journal, "rollback-intent", failure_injector)
        if journal["phase"] == "rollback-intent":
            _save_phase(journals, journal, "workspace-forgotten", failure_injector)
        journal["removal"]["root_removed"] = True
        journals.save(journal, expected_phase=journal["phase"])
        _fail_task(tasks, journals, journal)
        _save_phase(journals, journal, "rolled-back", failure_injector)
        return

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
                journal["materialized_owned"] = owned
            except (PreparationError, JournalError, JjError) as exc:
                _mark_preserved(journals, tasks, journal)
                if isinstance(exc, PreparationError):
                    raise
                raise PreparationError(
                    f"workspace ownership evidence is unavailable: {exc}; preserved"
                ) from exc
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
        change_id = journal["jj"].get("change_id")
        if change_id is not None:
            # Best effort: the empty described commit Control created for this
            # workspace is Control's own; leaving it makes a dead head per
            # failed start. A non-empty change is never touched.
            try:
                adapter.abandon_empty_change(source, change_id)
            except (JjError, OSError, ValueError):
                pass
    if journal["phase"] == "rollback-intent":
        _save_phase(journals, journal, "workspace-forgotten", failure_injector)
    if journal["phase"] == "workspace-forgotten":
        _save_phase(journals, journal, "removing", failure_injector)
    try:
        ownership_established = journal["materialized_owned"] is not None
        if destination.exists() and ownership_established:
            _remove_owned_tree(destination, journal, journals, failure_injector)
            _remove_workspace_root(destination, journal, journals, failure_injector)
        elif destination.exists() or destination.is_symlink():
            raise PreparationError("workspace ownership was never established; preserved")
        else:
            journal["removal"]["root_removed"] = True
            journals.save(journal, expected_phase=journal["phase"])
        _remove_created_parents(journal, journals, failure_injector)
    except (PreparationError, JournalError, JjError) as exc:
        _mark_preserved(journals, tasks, journal)
        if isinstance(exc, PreparationError):
            raise
        raise PreparationError(f"workspace ownership evidence is unavailable: {exc}; preserved") from exc
    except OSError as exc:
        if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
            raise
        _mark_preserved(journals, tasks, journal)
        raise PreparationError(
            "foreign data prevented owned workspace removal; preserved"
        ) from exc
    _fail_task(tasks, journals, journal)
    _save_phase(journals, journal, "rolled-back", failure_injector)


def _rollback_locked(
    config: ControlConfig, task_id: str, adapter: JjAdapter,
    *, invocation_id: str | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> None:
    """Run recovery with one fail-closed v2 interruption boundary."""
    journals = CreationJournalStore(config)
    tasks = TaskStore(config)
    initial = journals.read(task_id)
    retain_on_failure = bool(
        initial["contract"] == JOURNAL_CONTRACT and (
            initial["workspace"]["created_parents"] or
            initial["jj"]["registration_state"] != "absent" or
            initial["phase"] not in {"intent", "task-recorded"}
        ) and (invocation_id is None or initial["invocation_id"] == invocation_id)
    )
    try:
        _rollback_locked_impl(
            config, task_id, adapter, invocation_id=invocation_id,
            failure_injector=failure_injector,
        )
    except BaseException as exc:
        if not retain_on_failure:
            raise
        persistence_error: BaseException | None = None
        try:
            current = journals.read(task_id)
            try:
                bound_task = tasks.read(task_id)
            except StoreError:
                bound_task = None
            if bound_task is not None:
                _assert_task_binding(bound_task, current)
            persistence_error = _attempt_mark_preserved(journals, tasks, current)
        except BaseException as cleanup_exc:
            persistence_error = cleanup_exc
        if not isinstance(exc, Exception):
            if persistence_error is not None:
                _note_preservation_failure(exc, persistence_error)
            raise
        if persistence_error is not None:
            raise PreparationError(
                "v2 retention persistence failed; durable preserved/failed state "
                f"is not confirmed: {_bounded_exception_detail(persistence_error)}"
            ) from persistence_error
        if isinstance(exc, PreparationError) and "v2 automatic recovery retained" in str(exc):
            raise
        raise PreparationError(v2_retention_diagnostic(
            task_id, current, f"retained-state recovery failed: {exc}",
        )) from exc


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


def _context_plan_digest(plan: dict[str, dict[str, Any]]) -> str:
    raw = json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"asha-control-context-plan-v1\0" + raw).hexdigest()


def _require_initial_recovery_adoption_shape(
    task: dict[str, Any], journal: dict[str, Any], *, goal: str,
) -> None:
    if goal != task["label"]:
        raise PreparationError("adoption --goal must exactly match the durable task label")
    if (
        task["lifecycle"] != "failed" or task["runs"]
        or journal["contract"] != JOURNAL_CONTRACT
        or journal["phase"] != "preserved"
        or journal["launch_attempted"]
        or journal["jj"]["registration_state"] != "add-intent"
        or journal["workspace"]["root_fact"] is not None
        or journal["materialization_ownership"] is not None
        or journal["recovery_owned"] is not None
        or journal["planned_context"] is not None
        or journal["context_owned"]
        or journal["jj"]["change_id"] is not None
        or journal["jj"]["working_commit_id"] is not None
        or journal["jj"]["last_registration"] is not None
        or journal["jj"].get("workspace_add_operation_id") is not None
        or journal["jj"].get("checkout_operation_id") is not None
        or journal["removal"] != {
            "entries_removed": 0, "root_removed": False, "parents_removed": 0,
        }
    ):
        raise PreparationError(
            "retained task is not the exact runless v2 preserved/add-intent shape eligible for adoption"
        )


def _require_resumed_recovery_adoption_shape(
    task: dict[str, Any], journal: dict[str, Any], *,
    harness: str, role: str, goal: str,
) -> None:
    adoption = journal.get("adoption")
    if (
        not isinstance(adoption, dict)
        or adoption.get("authorization")
        != {"harness": harness, "role": role, "goal": goal}
        or journal["contract"] != JOURNAL_CONTRACT
        or journal["launch_attempted"] or task["runs"]
        or task["lifecycle"] not in {"failed", "creating"}
        or journal["phase"] not in {"preserved", "ready-for-launch"}
        or journal["jj"]["registration_state"] != "present"
        or journal["workspace"]["root_fact"] != adoption.get("root_fact")
        or journal["jj"].get("change_id")
        != adoption.get("registration", {}).get("change_id")
        or journal["jj"].get("working_commit_id")
        != adoption.get("registration", {}).get("working_commit_id")
        or journal["jj"].get("workspace_add_operation_id")
        != adoption.get("operations", {}).get("workspace_add")
        or journal["jj"].get("checkout_operation_id")
        != adoption.get("operations", {}).get("checkout")
        or journal["materialization_ownership"] is None
        or journal["planned_context"] is None
        or journal["removal"] != {
            "entries_removed": 0, "root_removed": False, "parents_removed": 0,
        }
    ):
        raise PreparationError(
            "retained adoption record or task state differs from its exact resumable shape"
        )
    if (
        journal["phase"] == "ready-for-launch"
    ) != (adoption["state"] == "ready-for-launch"):
        raise PreparationError("retained adoption phase and authorization state disagree")


def retained_recovery_guidance(
    task: dict[str, Any], journal: dict[str, Any],
) -> str | None:
    """Classify durable records for read-only operator guidance.

    This is intentionally only a shape classifier.  The adoption controller
    reauthenticates repository, workspace, operation, and content evidence
    under its locks before it mutates any record.
    """
    if not isinstance(task, dict) or not isinstance(journal, dict):
        return None
    adoption = journal.get("adoption")
    authorization: dict[str, str] | None = None
    try:
        if adoption is None:
            _require_initial_recovery_adoption_shape(
                task, journal, goal=task["label"],
            )
            _assert_task_binding(task, journal)
        else:
            candidate = adoption["authorization"]
            authorization = {
                "harness": candidate["harness"],
                "role": candidate["role"],
                "goal": candidate["goal"],
            }
            _require_resumed_recovery_adoption_shape(
                task, journal, **authorization,
            )
            _assert_task_binding(task, journal)
    except (KeyError, TypeError, ValueError, PreparationError):
        if (
            journal.get("contract") == JOURNAL_CONTRACT
            and journal.get("phase") in {"preserved", "ready-for-launch"}
            and not journal.get("launch_attempted", True)
            and not task.get("runs")
            and (task.get("lifecycle") == "failed" or adoption is not None)
        ):
            return (
                "retained creation is not the exact authenticated forward-adoption "
                "candidate; manual inspection only: inspect the workspace path and "
                "registration with `jj workspace list`"
            )
        return None

    if authorization is None:
        harness = "<harness>"
        role = "<role>"
        goal = task["label"]
    else:
        harness = shlex.quote(authorization["harness"])
        role = shlex.quote(authorization["role"])
        goal = authorization["goal"]
    command = (
        f"asha task recover {task['task_id']} --adopt --yes "
        f"--harness {harness} --role {role} --goal {shlex.quote(goal)}"
    )
    return (
        "retained creation is eligible for explicit authenticated forward-adoption; "
        f"replace launch placeholders and run: {command}"
    )


def _authenticate_adoption_context_residue(
    destination: Path, materialization: MaterializationPlan,
    planned: dict[str, dict[str, Any]], known: dict[str, dict[str, Any]],
) -> dict[str, dict[str, int]]:
    """Bind exact context created after an adoption intent but before its fact save."""
    selected = {entry.path for entry in materialization.entries}
    root_fd = _open_absolute_directory(destination)
    discovered: dict[str, dict[str, int]] = {}
    budget = [0]
    try:
        for relative in sorted(planned):
            if relative in selected:
                continue
            try:
                actual = _selected_fact(root_fd, relative, budget)
            except FileNotFoundError:
                continue
            expected = planned[relative]
            if any(actual.get(key) != value for key, value in expected.items()):
                raise PreparationError(
                    f"retained adoption context residue differs from intent: {relative}"
                )
            inode = {key: actual[key] for key in ("dev", "ino", "mode", "uid")}
            if relative in known and known[relative] != inode:
                raise PreparationError(
                    f"retained adoption context ownership changed: {relative}"
                )
            discovered[relative] = inode
    finally:
        os.close(root_fd)
    return discovered


def _adopt_preserved_task_workspace(
    config: ControlConfig, task_id: str, *, harness: str, role: str, goal: str,
    jj: JjAdapter | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Explicitly authenticate and complete one narrow retained creation."""
    adapter = jj or JjAdapter()
    task_id = canonical_uuid(task_id)
    tasks = TaskStore(config)
    journals = CreationJournalStore(config)
    ownership_store = MaterializationOwnershipStore(config)
    intents = ColocationIntentStore(config)
    coordinator = TransactionCoordinator(config)

    # The unlocked read discovers the deterministic source/repository lock
    # names only. Exact record digests are rechecked after acquiring the same
    # task -> source -> repository order used by normal task creation.
    entry_task = tasks.read(task_id)
    entry_journal = journals.read(task_id)
    entry_task_digest = task_digest(entry_task)
    entry_journal_digest = journals.digest(entry_journal)
    source = Path(entry_journal["repository"]["root"])
    destination = Path(entry_journal["workspace"]["path"])
    repository_identity = entry_journal["repository"]["identity"]

    with tasks.transaction_lock(task_id):
        with intents.mutation_lock(source), coordinator.repository_lock(repository_identity):
            # Exact raw-record CAS values are captured again under all three
            # cooperative Control locks before ownership is claimed.
            task = tasks.read(task_id)
            journal = journals.read(task_id)
            if (
                task_digest(task) != entry_task_digest
                or journals.digest(journal) != entry_journal_digest
            ):
                raise PreparationError("task or creation journal changed before adoption")
            resumed = journal.get("adoption") is not None
            if resumed:
                _require_resumed_recovery_adoption_shape(
                    task, journal, harness=harness, role=role, goal=goal,
                )
                original_task_digest = journal["adoption"]["original_task_digest"]
                original_journal_digest = journal["adoption"]["original_journal_digest"]
            else:
                _require_initial_recovery_adoption_shape(task, journal, goal=goal)
                _assert_task_binding(task, journal)
                original_task_digest = task_digest(task)
                original_journal_digest = journals.digest(journal)
            assessment = intents.classify(source)
            if assessment.kind != "verified" or assessment.digest is None:
                raise PreparationError(
                    "verified Control colocation binding is required for retained-state adoption"
                )
            colocation_digest = assessment.digest
            if resumed and colocation_digest != journal["adoption"]["colocation_intent_digest"]:
                raise PreparationError("verified colocation record changed since adoption intent")
            snapshot = read_published_snapshot(source)
            repository = adapter.preflight(source)
            identity, repo_key = derive_repository_identity(
                snapshot.project_id, repository.root, repository.git_root,
            )
            if (
                identity != journal["repository"]["identity"]
                or repo_key != journal["repository"]["repo_key"]
                or str(repository.git_root) != journal["repository"]["git_root"]
            ):
                raise PreparationError("retained repository binding differs from the creation request")
            _validate_layout(config, source, destination, repo_key, journal["task"]["slug"])
            plan = adapter.materialization_plan(
                repository.git_root, journal["jj"]["base_commit_id"], exact_root=source,
            )
            if plan.record() != journal["materialization_plan"]:
                raise PreparationError("retained workspace base materialization plan changed")
            try:
                root_metadata = destination.lstat()
            except OSError as exc:
                raise PreparationError(f"retained workspace root is unavailable: {exc}") from exc
            if (
                not stat.S_ISDIR(root_metadata.st_mode)
                or root_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(root_metadata.st_mode) != 0o700
            ):
                raise PreparationError(
                    "retained workspace root must be the exact owned mode-0700 directory"
                )
            registrations = adapter.workspace_identities(source)
            observed_registration = registrations.get(journal["workspace"]["name"])
            if observed_registration is None:
                raise PreparationError("retained workspace registration is missing")
            workspace_identity = adapter.inspect_workspace(
                destination, journal["workspace"]["name"], require_empty=True,
            )
            if (
                observed_registration
                != (workspace_identity.change_id, workspace_identity.commit_id)
                or workspace_identity.parent_commit_ids
                != (journal["jj"]["base_commit_id"],)
                or workspace_identity.description != journal["jj"]["description"]
            ):
                raise PreparationError("retained workspace registration or empty change identity differs")
            operation_proof = adapter.workspace_add_operation_proof(
                source,
                pinned_operation_id=journal["jj"]["pinned_operation_id"],
                workspace_name=journal["workspace"]["name"],
                base_commit_id=journal["jj"]["base_commit_id"],
                description=journal["jj"]["description"],
                destination=destination,
            )
            marker = {
                "contract": "asha.control-task-context.v1", "task_id": task_id,
                "repository": task["repository"],
                "jj": {
                    "workspace_name": journal["workspace"]["name"],
                    "workspace_path": str(destination),
                    "change_id": workspace_identity.change_id,
                    "working_commit_id": workspace_identity.commit_id,
                },
            }
            context_plan = build_context_plan(
                source, destination, marker, snapshot=snapshot,
            )
            planned_context = _planned_manifest(context_plan)
            adapter.prove_context_compatibility(
                source, repository.git_root, plan, project_id=snapshot.project_id,
                planned_context_paths=tuple(context_plan),
                private_directory_paths=DYNAMIC_PRIVATE_CONTEXT_DIRECTORIES,
            )
            if resumed:
                adoption = journal["adoption"]
                if (
                    journal["planned_context"] != planned_context
                    or adoption["context_plan_digest"]
                    != _context_plan_digest(planned_context)
                    or adoption["registration"] != {
                        "change_id": workspace_identity.change_id,
                        "working_commit_id": workspace_identity.commit_id,
                    }
                    or adoption["operations"] != {
                        "workspace_add": operation_proof.workspace_add_operation_id,
                        "checkout": operation_proof.checkout_operation_id,
                    }
                ):
                    raise PreparationError("retained adoption evidence changed before resume")
                root_fact = adoption["root_fact"]
                if _inode_fact(root_metadata) != root_fact:
                    raise PreparationError("retained adoption workspace root identity changed")
                sidecar = journal["materialization_ownership"]["sidecar"]
                private = journal["materialization_ownership"]["private"]
                stored_facts = ownership_store.read(sidecar)
                discovered = _authenticate_adoption_context_residue(
                    destination, plan, planned_context, journal["context_owned"],
                )
                if any(
                    journal["context_owned"].get(path) != fact
                    for path, fact in discovered.items()
                ):
                    journal["context_owned"].update(discovered)
                    journals.save(
                        journal, expected_phase=journal["phase"],
                        expected_digest=journals.digest(journals.read(task_id)),
                        allow_recovery_adoption=True,
                    )
                other_owned = {
                    **private,
                    **{
                        path: {**planned_context[path], **ownership}
                        for path, ownership in journal["context_owned"].items()
                    },
                }
                _verify_plan_materialization(
                    destination, source, plan, expected_root=root_fact,
                    expected_facts=stored_facts, other_owned=other_owned,
                )
            else:
                facts, private, root_fact = _verify_plan_materialization(
                    destination, source, plan,
                )
                _ensure_creation_journal_capacity(journal, planned_context)
                sidecar = ownership_store.write(
                    task_id, plan.digest, facts,
                    failure_injector=failure_injector,
                )
                stored_facts = facts
                adoption = {
                    "contract": RECOVERY_ADOPTION_CONTRACT,
                    "state": "intent",
                    "original_task_digest": original_task_digest,
                    "original_journal_digest": original_journal_digest,
                    "colocation_intent_digest": colocation_digest,
                    "authorization": {"harness": harness, "role": role, "goal": goal},
                    "root_fact": root_fact,
                    "registration": {
                        "change_id": workspace_identity.change_id,
                        "working_commit_id": workspace_identity.commit_id,
                    },
                    "operations": {
                        "workspace_add": operation_proof.workspace_add_operation_id,
                        "checkout": operation_proof.checkout_operation_id,
                    },
                    "context_plan_digest": _context_plan_digest(planned_context),
                }
                journal["workspace"]["root_fact"] = root_fact
                journal["materialization_ownership"] = {
                    "sidecar": sidecar, "private": private,
                }
                journal["jj"].update({
                    "change_id": workspace_identity.change_id,
                    "working_commit_id": workspace_identity.commit_id,
                    "registration_state": "present",
                    "last_registration": {
                        "change_id": workspace_identity.change_id,
                        "working_commit_id": workspace_identity.commit_id,
                    },
                    "workspace_add_operation_id": operation_proof.workspace_add_operation_id,
                    "checkout_operation_id": operation_proof.checkout_operation_id,
                })
                journal["planned_context"] = planned_context
                journal["adoption"] = adoption
                journals.save(
                    journal, expected_phase="preserved",
                    expected_digest=original_journal_digest,
                    allow_recovery_adoption=True,
                )
                if failure_injector is not None:
                    failure_injector("adoption:intent")

            # Reauthenticate every claimed fact after the intent replacement.
            current_task = tasks.read(task_id)
            if current_task["lifecycle"] == "failed" and task_digest(current_task) != original_task_digest:
                raise PreparationError("task record changed after adoption intent")
            current_assessment = intents.classify(source)
            if current_assessment.kind != "verified" or current_assessment.digest != colocation_digest:
                raise PreparationError("colocation intent changed after adoption intent")
            stored_facts = ownership_store.read(sidecar)
            _verify_plan_materialization(
                destination, source, plan, expected_root=root_fact,
                expected_facts=stored_facts,
                other_owned={
                    **private,
                    **{
                        path: {**planned_context[path], **ownership}
                        for path, ownership in journal["context_owned"].items()
                    },
                },
            )
            if read_published_snapshot(source) != snapshot:
                raise PreparationError("published context snapshot changed before adoption provisioning")
            if adapter.workspace_add_operation_proof(
                source,
                pinned_operation_id=journal["jj"]["pinned_operation_id"],
                workspace_name=journal["workspace"]["name"],
                base_commit_id=journal["jj"]["base_commit_id"],
                description=journal["jj"]["description"],
                destination=destination,
            ) != operation_proof:
                raise PreparationError("workspace operation ancestry changed after adoption intent")

            if adoption["state"] == "ready-for-launch":
                current = tasks.read(task_id)
                if current["lifecycle"] == "failed" and task_digest(current) == original_task_digest:
                    changed = copy.deepcopy(current)
                    changed["lifecycle"] = "creating"
                    changed["updated_at"] = _now()
                    changed["jj"]["change_id"] = workspace_identity.change_id
                    changed["jj"]["working_commit_id"] = workspace_identity.commit_id
                    tasks.save(
                        changed, expected_digest=original_task_digest,
                        recovery_adoption=adoption,
                    )
                    current = changed
                elif (
                    current["lifecycle"] != "creating"
                    or current["jj"]["change_id"] != workspace_identity.change_id
                    or current["jj"]["working_commit_id"] != workspace_identity.commit_id
                ):
                    raise PreparationError("ready adoption task identity changed before launch")
                if journal["task"]["digest"] != task_digest(current):
                    journal["task"]["digest"] = task_digest(current)
                    journals.save(
                        journal, expected_phase="ready-for-launch",
                        expected_digest=journals.digest(journals.read(task_id)),
                        allow_recovery_adoption=True,
                    )
                return current

            if adoption["state"] in {"intent", "context-provisioning"}:
                adoption["state"] = "context-provisioning"
                journals.save(
                    journal, expected_phase="preserved",
                    expected_digest=journals.digest(journals.read(task_id)),
                    allow_recovery_adoption=True,
                )

                discovered = _authenticate_adoption_context_residue(
                    destination, plan, planned_context, journal["context_owned"],
                )
                if any(
                    journal["context_owned"].get(path) != fact
                    for path, fact in discovered.items()
                ):
                    journal["context_owned"].update(discovered)
                    current_journal = journals.read(task_id)
                    journals.save(
                        journal, expected_phase="preserved",
                        expected_digest=journals.digest(current_journal),
                        allow_recovery_adoption=True,
                    )

                def record_context(relative: str, fact: dict[str, Any]) -> None:
                    planned = journal["planned_context"][relative]
                    projection = {key: fact[key] for key in planned if key in fact}
                    if any(projection.get(key) != value for key, value in planned.items()):
                        raise PreparationError(f"adopted context differs from intent: {relative}")
                    journal["context_owned"][relative] = {
                        key: fact[key] for key in ("dev", "ino", "mode", "uid")
                    }
                    current = journals.read(task_id)
                    journals.save(
                        journal, expected_phase="preserved",
                        expected_digest=journals.digest(current),
                        allow_recovery_adoption=True,
                    )
                    if failure_injector is not None:
                        failure_injector(f"adoption:context-owned:{relative}")

                provision_context(
                    source, destination, marker, snapshot=snapshot,
                    after_entry=record_context,
                )
            other_owned = {
                **private,
                **{
                    path: {**planned_context[path], **ownership}
                    for path, ownership in journal["context_owned"].items()
                },
            }
            _verify_plan_materialization(
                destination, source, plan, expected_root=root_fact,
                expected_facts=stored_facts, other_owned=other_owned,
            )
            if adoption["state"] != "context-provisioned":
                final_identity = adapter.inspect_workspace(
                    destination, journal["workspace"]["name"],
                    snapshot=True, require_empty=True,
                )
                if final_identity != workspace_identity:
                    raise PreparationError("adopted workspace identity changed during context provisioning")
                adoption["state"] = "context-provisioned"
                journals.save(
                    journal, expected_phase="preserved",
                    expected_digest=journals.digest(journals.read(task_id)),
                    allow_recovery_adoption=True,
                )
                if failure_injector is not None:
                    failure_injector("adoption:context-provisioned")

            adoption["state"] = "ready-for-launch"
            journal["phase"] = "ready-for-launch"
            journals.save(
                journal, expected_phase="preserved",
                expected_digest=journals.digest(journals.read(task_id)),
                allow_recovery_adoption=True,
            )
            changed = copy.deepcopy(task)
            changed["lifecycle"] = "creating"
            changed["updated_at"] = _now()
            changed["jj"]["change_id"] = workspace_identity.change_id
            changed["jj"]["working_commit_id"] = workspace_identity.commit_id
            tasks.save(
                changed, expected_digest=original_task_digest,
                recovery_adoption=adoption,
            )
            journal["task"]["digest"] = task_digest(changed)
            journals.save(
                journal, expected_phase="ready-for-launch",
                expected_digest=journals.digest(journals.read(task_id)),
                allow_recovery_adoption=True,
            )
            if failure_injector is not None:
                failure_injector("adoption:ready-for-launch")
            return changed


def adopt_preserved_task_workspace(
    config: ControlConfig, task_id: str, *, harness: str, role: str, goal: str,
    jj: JjAdapter | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Normalize authenticated-adoption boundary failures without masking signals."""
    try:
        return _adopt_preserved_task_workspace(
            config, task_id, harness=harness, role=role, goal=goal, jj=jj,
            failure_injector=failure_injector,
        )
    except PreparationError:
        raise
    except (JjError, StoreError, JournalError, OSError) as exc:
        raise PreparationError(f"retained-state adoption refused: {exc}") from exc


def prepare_task_workspace(
    config: ControlConfig, request: PrepareRequest, *, jj: JjAdapter | None = None,
    failure_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    adapter = jj or JjAdapter()
    try:
        task_id = canonical_uuid(request.task_id)
        slug = validate_task_slug(request.slug)
    except ValueError as exc:
        raise PreparationError(str(exc)) from exc
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
        destination = config.workspace_root / repo_key / slug
        workspace_name = f"asha-{slug}-{task_id[:8]}"
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
        materialization_plan = adapter.materialization_plan(
            repository.git_root, base_commit_id, exact_root=source,
        )
        if materialization_plan.entry_count > MAX_IMMUTABLE_TREE_ENTRIES:
            raise ValueError(
                f"tracked revision exceeds the {MAX_IMMUTABLE_TREE_ENTRIES}-entry capacity"
            )
        prospective_marker = {
            "contract": "asha.control-task-context.v1", "task_id": task_id,
            "repository": {"root": str(source), "identity": repository_identity},
            "jj": {
                "workspace_name": workspace_name, "workspace_path": str(destination),
                "change_id": "k" * 32, "working_commit_id": "f" * 64,
            },
        }
        prospective_context_plan = build_context_plan(
            source, destination, prospective_marker, snapshot=snapshot,
        )
        context_compatibility = adapter.prove_context_compatibility(
            source, repository.git_root, materialization_plan,
            project_id=snapshot.project_id,
            planned_context_paths=tuple(prospective_context_plan),
            private_directory_paths=DYNAMIC_PRIVATE_CONTEXT_DIRECTORIES,
        )
    except (OSError, ValueError, JjError) as exc:
        raise PreparationError(f"{exc} (preflight refused; no task state was created)") from exc
    _validate_layout(config, source, destination, repo_key, slug)
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
            "workspace_add_operation_id": None, "checkout_operation_id": None,
        },
        "materialization_plan": materialization_plan.record(),
        "materialization_ownership": None, "recovery_owned": None,
        "planned_context": None, "context_owned": {},
        "removal": {"entries_removed": 0, "root_removed": False, "parents_removed": 0},
    }
    try:
        capacity_plan = _planned_manifest(prospective_context_plan)
        _ensure_creation_journal_capacity(journal, capacity_plan)
        missing_ancestors = _count_missing_destination_ancestors(
            config, source, destination, repo_key, slug,
        )
        if missing_ancestors > 8:
            raise PreparationError(
                "workspace destination requires more than eight created ancestors"
            )
    except (OSError, ValueError) as exc:
        raise PreparationError(f"{exc} (preflight refused; no task state was created)") from exc
    journals = CreationJournalStore(config)
    ownership_store = MaterializationOwnershipStore(config)
    tasks = TaskStore(config)
    coordinator = TransactionCoordinator(config)
    claimed = False

    def phase(next_phase: str) -> None:
        _save_phase(journals, journal, next_phase, failure_injector)
        # Retain the original Increment 2 injection vocabulary as well.
        if failure_injector is not None:
            failure_injector(next_phase)

    def persist_added_workspace_identity(*, inject_sidecar: bool) -> Any:
        """Authenticate and durably bind a completed or indeterminate add."""
        identity = adapter.inspect_workspace(destination, workspace_name)
        if identity.parent_commit_ids != (base_commit_id,) or identity.description != request.label:
            raise PreparationError("created workspace is not the exact empty requested change")
        _make_workspace_private(destination)
        operation_proof = adapter.workspace_add_operation_proof(
            source, pinned_operation_id=operation_id,
            workspace_name=workspace_name, base_commit_id=base_commit_id,
            description=request.label, destination=destination,
        )
        facts, private, root_fact = _verify_plan_materialization(
            destination, source, materialization_plan,
        )
        sidecar = ownership_store.write(
            task_id, materialization_plan.digest, facts,
            failure_injector=failure_injector if inject_sidecar else None,
        )
        journal["workspace"]["root_fact"] = root_fact
        journal["materialization_ownership"] = {
            "sidecar": sidecar, "private": private,
        }
        journal["jj"].update({
            "change_id": identity.change_id, "working_commit_id": identity.commit_id,
            "registration_state": "present",
            "last_registration": {
                "change_id": identity.change_id, "working_commit_id": identity.commit_id,
            },
            "workspace_add_operation_id": operation_proof.workspace_add_operation_id,
            "checkout_operation_id": operation_proof.checkout_operation_id,
        })
        if inject_sidecar:
            phase("workspace-added")
            phase("workspace-recorded")
        else:
            _save_phase(journals, journal, "workspace-added")
            _save_phase(journals, journal, "workspace-recorded")
        return identity

    try:
        # Task identity serializes recovery; repository identity separately
        # serializes jj registration and shared workspace-parent creation.
        # Neither lock holds the registry directory flock during those steps.
        with tasks.transaction_lock(task_id), coordinator.repository_lock(repository_identity):
            _validate_layout(config, source, destination, repo_key, slug)
            if destination.exists() or destination.is_symlink():
                raise PreparationError("workspace destination already exists; no task was created")
            # This is the last fallible compatibility read.  It is deliberately
            # ahead of journal/task/parent mutation; the immediately pre-add
            # check below is then a pure assertion over immutable carried facts.
            authoritative_context_compatibility = adapter.prove_context_compatibility(
                source, repository.git_root, materialization_plan,
                project_id=snapshot.project_id,
                planned_context_paths=context_compatibility.planned_context_paths,
                private_directory_paths=context_compatibility.private_directory_paths,
            )
            if authoritative_context_compatibility != context_compatibility:
                raise PreparationError(
                    "immutable context compatibility evidence changed before registration"
                )
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
            if (
                authoritative_context_compatibility.base_commit_id != base_commit_id
                or authoritative_context_compatibility.materialization_digest
                != materialization_plan.digest
            ):
                raise PreparationError(
                    "immutable context compatibility evidence is not bound to workspace add"
                )
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
                    persist_added_workspace_identity(inject_sidecar=False)
                except (OSError, JjError, PreparationError):
                    try:
                        _make_registered_workspace_private(
                            adapter, source, destination, workspace_name,
                            base_commit_id, request.label,
                        )
                    except (OSError, JjError, PreparationError):
                        pass
                raise
            identity = persist_added_workspace_identity(inject_sidecar=True)
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
            stored_facts = ownership_store.read(
                journal["materialization_ownership"]["sidecar"],
            )
            other_owned = {
                **journal["materialization_ownership"]["private"],
                **{
                    path: {**journal["planned_context"][path], **ownership}
                    for path, ownership in journal["context_owned"].items()
                },
            }
            _verify_plan_materialization(
                destination, source, materialization_plan,
                expected_root=journal["workspace"]["root_fact"],
                expected_facts=stored_facts,
                other_owned=other_owned,
            )
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
        # Cause first, then what Control did about it: the operator reads the
        # refusal and its remedy before the recovery framing.
        recovery = ""
        if claimed:
            try:
                rollback_prelaunch(
                    config, task_id, jj=adapter, invocation_id=invocation_id,
                )
            except PreparationError as rollback_exc:
                if "v2 automatic recovery retained" in str(rollback_exc):
                    recovery = f" (workspace retained for safety: {rollback_exc})"
                else:
                    recovery = (
                        f" (workspace preparation rolled back only partially: "
                        f"{rollback_exc}; run: asha task recover {task_id})"
                    )
            else:
                recovery = " (workspace preparation rolled back; nothing to recover)"
        if not isinstance(exc, Exception):
            raise
        if not claimed:
            raise PreparationError(
                f"{exc} (creation intent was not claimed; existing state preserved)"
            ) from exc
        raise PreparationError(f"{exc}{recovery}") from exc


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
        materialization_plan = adapter.materialization_plan(
            repository.git_root, base_commit_id, exact_root=source,
        )
    except (OSError, ValueError, JjError) as exc:
        raise PreparationError(f"materialization preflight failed without mutation: {exc}") from exc

    repository_parent = config.workspace_root / repo_key
    materializations = repository_parent / "materializations"
    coordinator = TransactionCoordinator(config)

    with coordinator.repository_lock(repository_identity):
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
            _verify_plan_materialization(destination, source, materialization_plan)
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
