"""Repository and declared-workspace scope identity for initiatives (Increment 7).

A repository scope binds one jj-colocated Asha project: Memory v2 project id,
canonical root, and Control repository identity, folded into a stable
repository id. A workspace scope binds several such members through the
workspace manifest (`.asha/workspace.json`) under one workspace root, with a
membership digest over the ordered declared members. Detection and manifest
validation come from the session plugin's workspace tools through Control's
existing import seam; nothing here touches tmux or jj beyond preflight.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Mapping

from ..context import read_published_snapshot, validate_manifest, detect_workspace
from ..jj import JjAdapter, JjError
from ..prepare import derive_repository_identity
from .model import ModelError, scope_repositories, validate_workspace_scope


class ScopeError(ValueError):
    """Scope identity could not be derived or no longer matches its record."""


def _identity_digest(project_id: str, root: Path, identity: str) -> str:
    return hashlib.sha256(json.dumps(
        [project_id, str(root), identity], ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def repository_scope(repo: Path, jj: JjAdapter) -> dict[str, Any]:
    """Stable repository identity for one jj-colocated Asha project."""
    root = repo.expanduser().resolve()
    facts = jj.preflight(root)
    snapshot = read_published_snapshot(facts.root)
    identity, _ = derive_repository_identity(snapshot.project_id, facts.root, facts.git_root)
    initial = _identity_digest(snapshot.project_id, facts.root, identity)
    return {
        "repository_id": str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"asha-orchestration-repository-v1:{initial}"
        )),
        "project_id": snapshot.project_id,
        "root": str(facts.root), "control_repository_id": identity,
        "initial_identity_digest": initial,
    }


def membership_digest(members: list[Mapping[str, Any]], relative_paths: list[str]) -> str:
    """Digest over the ordered declared members: manifest path plus repository identity."""
    return hashlib.sha256(json.dumps(
        [[path, member["repository_id"]] for path, member in zip(relative_paths, members)],
        ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def _declared_members(root: Path) -> tuple[Path, dict[str, Any], list[str]]:
    detection = detect_workspace(root)
    if detection.root is None or detection.manifest is None:
        detail = "; ".join(f"{error.code}: {error.message}" for error in detection.errors)
        raise ScopeError(f"no valid declared workspace at {root}" + (f" ({detail})" if detail else ""))
    manifest, errors = validate_manifest(detection.manifest)
    if manifest is None:
        detail = "; ".join(f"{error.code}: {error.message}" for error in errors)
        raise ScopeError(f"workspace manifest is invalid: {detail}")
    paths = [entry["path"] for entry in manifest.get("repositories", [])]
    if not paths:
        raise ScopeError("workspace manifest declares no repositories")
    return detection.root.resolve(), manifest, paths


def workspace_scope(root: Path, jj: JjAdapter) -> dict[str, Any]:
    """Build the workspace scope from the declared manifest and each member's identity."""
    workspace_root, _manifest, paths = _declared_members(root.expanduser().resolve())
    members = []
    for relative in paths:
        member_root = (workspace_root / relative).resolve()
        try:
            members.append(repository_scope(member_root, jj))
        except (JjError, OSError, ValueError) as exc:
            raise ScopeError(f"workspace member {relative} is not a usable repository: {exc}") from exc
    try:
        project_id = read_published_snapshot(workspace_root).project_id
    except Exception:  # noqa: BLE001 - a workspace root without Memory v2 falls back to its path
        project_id = f"workspace:{hashlib.sha256(str(workspace_root).encode()).hexdigest()[:32]}"
    digest = membership_digest(members, paths)
    scope = {
        "workspace_id": str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"asha-orchestration-workspace-v1:{workspace_root}:{digest}"
        )),
        "project_id": project_id,
        "root": str(workspace_root),
        "manifest_membership_digest": digest,
        "repositories": members,
    }
    try:
        return validate_workspace_scope(scope)
    except ModelError as exc:
        raise ScopeError(str(exc)) from exc


def verify_scope_identity(
    initiative: Mapping[str, Any],
    *,
    jj: JjAdapter | None = None,
    read_snapshot: Any = None,
    derive_identity: Any = None,
) -> None:
    """Refuse when any member repository, or the workspace membership, drifted."""
    adapter = jj or JjAdapter()
    snapshot_reader = read_snapshot or read_published_snapshot
    identity_deriver = derive_identity or derive_repository_identity
    scope = initiative["scope"]
    for expected in scope_repositories(initiative):
        root = Path(expected["root"])
        try:
            facts = adapter.preflight(root)
        except (JjError, OSError, ValueError) as exc:
            detail = " ".join(str(exc).split())
            raise ScopeError(f"repository {expected['repository_id']} cannot be preflighted: {detail}") from exc
        if facts.root != root:
            raise ScopeError("initiative repository canonical root changed")
        snapshot = snapshot_reader(root)
        identity, _ = identity_deriver(snapshot.project_id, facts.root, facts.git_root)
        if (
            snapshot.project_id != expected["project_id"]
            or identity != expected["control_repository_id"]
            or _identity_digest(snapshot.project_id, facts.root, identity)
            != expected["initial_identity_digest"]
        ):
            raise ScopeError("initiative repository identity digest changed")
    if scope["kind"] == "workspace":
        workspace = scope["workspace"]
        recorded_root = Path(workspace["root"])
        detected_root, _manifest, paths = _declared_members(recorded_root)
        if detected_root != recorded_root.resolve():
            raise ScopeError(
                "workspace membership changed: the declared manifest is no longer at the recorded root"
            )
        members = workspace["repositories"]
        if len(paths) != len(members):
            raise ScopeError("workspace membership changed: declared member count differs")
        for relative, member in zip(paths, members):
            if str((Path(workspace["root"]) / relative).resolve()) != member["root"]:
                raise ScopeError(f"workspace membership changed: {relative} no longer maps to its member")
        if membership_digest(members, paths) != workspace["manifest_membership_digest"]:
            raise ScopeError("workspace membership digest changed")


__all__ = [
    "ScopeError", "membership_digest", "repository_scope", "verify_scope_identity",
    "workspace_scope",
]
