"""Standing authorities: operator-pre-approved plan shapes (auto-approval).

An authority is the operator's durable, revocable signature over a plan
*shape* for one exact repository identity: scope prefixes, node ceilings,
harnesses, and gate requirements. When a freshly proposed plan matches an
active authority exactly, the controller records the operator's pre-signed
approval (actor_id names the authority) and, when granted, activates. The
matcher is deterministic and fail-closed; anything off-shape falls back to
ordinary manual approval and the mismatch reason is reported. Authorities
never cover integration, salvage, decisions, or needs-input.

Records live beside the initiative registry (never inside it) so initiative
listing stays uncontaminated. Creation is write-once; revocation is a CAS
under an exclusive lock.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..jj import JjAdapter
from .model import ModelError, _digest, _object, _text, _timestamp, canonical_uuid, record_digest
from .workspace_scope import repository_scope

AUTHORITY_CONTRACT = "asha.orchestration-standing-authority.v1"
MAX_AUTHORITY_NODES = 12
_LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,39}", re.ASCII)
_ALLOWED_NODE_TYPES = frozenset({"work", "review", "verify"})
_AUTHORITY_KEYS = frozenset({
    "contract", "authority_id", "label", "repository", "constraints",
    "auto_activate", "created_at", "revoked_at",
})
_CONSTRAINT_KEYS = frozenset({
    "scope_prefixes", "max_nodes", "harnesses", "max_attempts_per_node",
    "require_headless",
})
_REPOSITORY_KEYS = frozenset({
    "repository_id", "project_id", "root", "control_repository_id",
    "initial_identity_digest",
})


class AuthorityError(ValueError):
    """A standing authority could not be created, read, or applied."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _clean_prefix(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 300:
        raise ModelError("authority scope prefix must be bounded text")
    prefix = value.strip("/")
    parts = prefix.split("/")
    if not prefix or value.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ModelError("authority scope prefix must be a clean repository-relative path")
    return prefix


def validate_authority(value: Any) -> dict[str, Any]:
    record = _object(value, "authority", _AUTHORITY_KEYS)
    if record["contract"] != AUTHORITY_CONTRACT:
        raise ModelError(f"authority contract must be {AUTHORITY_CONTRACT}")
    canonical_uuid(record["authority_id"], "authority_id")
    _text(record["label"], "authority label", maximum=40, pattern=_LABEL)
    repository = _object(record["repository"], "authority repository", _REPOSITORY_KEYS)
    canonical_uuid(repository["repository_id"], "authority repository_id")
    _digest(repository["initial_identity_digest"], "authority identity digest")
    constraints = _object(record["constraints"], "authority constraints", _CONSTRAINT_KEYS)
    prefixes = constraints["scope_prefixes"]
    if not isinstance(prefixes, list) or not prefixes or len(prefixes) > 16:
        raise ModelError("authority scope_prefixes must be a bounded non-empty list")
    for prefix in prefixes:
        _clean_prefix(prefix)
    if not isinstance(constraints["max_nodes"], int) or not 1 <= constraints["max_nodes"] <= MAX_AUTHORITY_NODES:
        raise ModelError(f"authority max_nodes must be 1..{MAX_AUTHORITY_NODES}")
    harnesses = constraints["harnesses"]
    if (not isinstance(harnesses, list) or not harnesses
            or any(item not in {"claude", "codex", "copilot", "opencode"} for item in harnesses)):
        raise ModelError("authority harnesses must name supported harnesses")
    if not isinstance(constraints["max_attempts_per_node"], int) or not 1 <= constraints["max_attempts_per_node"] <= 5:
        raise ModelError("authority max_attempts_per_node must be 1..5")
    if not isinstance(constraints["require_headless"], bool):
        raise ModelError("authority require_headless must be a boolean")
    if not isinstance(record["auto_activate"], bool):
        raise ModelError("authority auto_activate must be a boolean")
    _timestamp(record["created_at"], "authority created_at")
    if record["revoked_at"] is not None:
        revoked = _timestamp(record["revoked_at"], "authority revoked_at")
        if revoked < _timestamp(record["created_at"], "authority created_at"):
            raise ModelError("authority revoked_at must not precede created_at")
    return copy.deepcopy(record)


def authorities_dir(config: Any) -> Path:
    return Path(config.initiatives_dir).parent / "authorities"


def _read(path: Path) -> dict[str, Any]:
    try:
        return validate_authority(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ModelError) as exc:
        raise AuthorityError(f"authority record unreadable at {path.name}: {exc}") from exc


def add_authority(
    config: Any, *, root: Path, label: str, scope_prefixes: list[str],
    max_nodes: int = 5, harnesses: list[str] | None = None,
    max_attempts_per_node: int = 2, require_headless: bool = False,
    auto_activate: bool = True, jj: JjAdapter | None = None,
) -> dict[str, Any]:
    """Create one standing authority bound to the repository's current identity."""
    _text(label, "authority label", maximum=40, pattern=_LABEL)
    if not isinstance(scope_prefixes, list) or not scope_prefixes:
        raise ModelError("authority requires at least one scope prefix")
    prefixes = [_clean_prefix(item) for item in scope_prefixes]
    record = validate_authority({
        "contract": AUTHORITY_CONTRACT,
        "authority_id": str(uuid.uuid4()),
        "label": label,
        "repository": repository_scope(Path(root), jj or JjAdapter()),
        "constraints": {
            "scope_prefixes": prefixes,
            "max_nodes": max_nodes,
            "harnesses": sorted(set(harnesses or ["claude", "codex"])),
            "max_attempts_per_node": max_attempts_per_node,
            "require_headless": require_headless,
        },
        "auto_activate": auto_activate,
        "created_at": _now(),
        "revoked_at": None,
    })
    directory = authorities_dir(config)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{record['authority_id']}.json"
    body = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, body.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return record


def list_authorities(config: Any, *, include_revoked: bool = False) -> list[dict[str, Any]]:
    directory = authorities_dir(config)
    if not directory.is_dir():
        return []
    if not os.access(directory, os.R_OK | os.X_OK):
        raise AuthorityError(f"authority store at {directory} is not readable")
    records = []
    for path in sorted(directory.glob("*.json")):
        record = _read(path)
        if record["revoked_at"] is None or include_revoked:
            records.append(record)
    return records


def revoke_authority(config: Any, authority_id: str) -> dict[str, Any]:
    """Mark one authority revoked; the record is retained, never deleted."""
    canonical_uuid(authority_id, "authority_id")
    directory = authorities_dir(config)
    path = directory / f"{authority_id}.json"
    if not path.is_file():
        raise AuthorityError(f"no authority {authority_id}")
    lock_path = directory / ".lock"
    with open(lock_path, "a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        record = _read(path)
        if record["revoked_at"] is not None:
            return record
        changed = dict(record, revoked_at=_now())
        validate_authority(changed)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(changed, ensure_ascii=False, sort_keys=True, indent=1), encoding="utf-8")
        os.replace(temporary, path)
        return changed


def _paths_within(paths: list[str], prefixes: list[str]) -> bool:
    for raw in paths:
        if not isinstance(raw, str) or raw.startswith("/") or ".." in raw.split("/"):
            return False
        cleaned = raw.strip("/")
        if not any(cleaned == prefix or cleaned.startswith(prefix + "/") for prefix in prefixes):
            return False
    return True


def match_plan(
    authority: Mapping[str, Any], initiative: Mapping[str, Any], plan: Mapping[str, Any],
) -> tuple[bool, str]:
    """Deterministic, fail-closed shape match. Returns (matched, reason)."""
    if authority.get("revoked_at") is not None:
        return False, "authority is revoked"
    scope = initiative.get("scope") or {}
    if scope.get("kind", "repository") != "repository":
        return False, "authorities cover single-repository initiatives only"
    repository = scope.get("repository") or {}
    expected = authority["repository"]
    if repository.get("repository_id") != expected["repository_id"]:
        return False, "repository identity differs"
    if repository.get("initial_identity_digest") != expected["initial_identity_digest"]:
        return False, "repository identity digest drifted since the authority was granted"
    constraints = authority["constraints"]
    nodes = plan.get("nodes") or []
    if not nodes or len(nodes) > constraints["max_nodes"]:
        return False, f"plan has {len(nodes)} nodes; authority allows at most {constraints['max_nodes']}"
    prefixes = [prefix.strip("/") for prefix in constraints["scope_prefixes"]]
    for node in nodes:
        if node.get("type") not in _ALLOWED_NODE_TYPES:
            return False, f"node {node.get('node_id')} type {node.get('type')} is outside the authority"
        if node.get("workflow") not in {None, "none"}:
            return False, f"node {node.get('node_id')} requests a nested workflow"
        writes = list(node.get("hard_write_scope") or []) + list(node.get("advisory_path_ownership") or [])
        if node.get("type") == "work":
            if not node.get("hard_write_scope"):
                return False, f"work node {node.get('node_id')} has no hard write scope"
            if not _paths_within(writes, prefixes):
                return False, f"node {node.get('node_id')} writes outside the authorized scope"
        elif writes:
            return False, f"non-work node {node.get('node_id')} declares a write scope"
        if node.get("type") in {"work", "review"}:
            if node.get("harness") not in constraints["harnesses"]:
                return False, f"node {node.get('node_id')} harness is not authorized"
            if constraints["require_headless"] and node.get("interactive") is not False:
                return False, f"node {node.get('node_id')} must be headless under this authority"
    gates = plan.get("declared_gates") or []
    verification = [item for item in gates if item.get("kind") == "verification" and item.get("required")]
    reviews = [item for item in gates if item.get("kind") == "review" and item.get("required")]
    if not reviews or len(verification) != 1:
        return False, "authority requires the mandatory review and verification gates"
    commands = verification[0].get("commands") or []
    if not commands or verification[0].get("environment_policy") != "minimal":
        return False, "authority requires real minimal-environment verification commands"
    limits = plan.get("limits") or {}
    if limits.get("max_attempts_per_node", 99) > constraints["max_attempts_per_node"]:
        return False, "plan attempt ceiling exceeds the authority"
    return True, "matched"


def find_matching_authority(
    config: Any, initiative: Mapping[str, Any], plan: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """First active matching authority, plus the mismatch reasons worth reading.

    Reasons are reported only for authorities bound to this initiative's own
    repository. A grant for some other repository is not a near miss, and
    listing it on every proposal would bury the one reason the operator
    actually needs.
    """
    reasons: list[str] = []
    scope = initiative.get("scope") or {}
    repository_id = (scope.get("repository") or {}).get("repository_id")
    for authority in list_authorities(config):
        matched, reason = match_plan(authority, initiative, plan)
        if matched:
            return authority, reasons
        if authority["repository"]["repository_id"] == repository_id:
            reasons.append(f"{authority['label']}: {reason}")
    return None, reasons


__all__ = [
    "AUTHORITY_CONTRACT", "AuthorityError", "add_authority", "authorities_dir",
    "find_matching_authority", "list_authorities", "match_plan",
    "revoke_authority", "validate_authority",
]
