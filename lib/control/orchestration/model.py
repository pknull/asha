"""Closed orchestration v1 records and explicit state-transition graphs.

All textual and collection bounds are constants in this module.  Text limits
are UTF-8 byte limits, not Python character counts.
"""

from __future__ import annotations

import copy
import hashlib
import json
import posixpath
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from ..config import is_canonical_absolute_path


INITIATIVE_CONTRACT = "asha.orchestration-initiative.v1"
INITIATIVE_CONTRACT_V2 = "asha.orchestration-initiative.v2"
INITIATIVE_CONTRACTS = frozenset({INITIATIVE_CONTRACT, INITIATIVE_CONTRACT_V2})
PLAN_CONTRACT = "asha.orchestration-plan.v1"
NODE_CONTRACT = "asha.orchestration-node.v1"
ATTEMPT_CONTRACT = "asha.orchestration-attempt.v1"
RESULT_PUBLICATION_CONTRACT = "asha.orchestration-result-publication.v1"
RESULT_CONTRACT = "asha.orchestration-result.v1"
RESULT_INGESTION_CONTRACT = "asha.orchestration-result-ingestion.v1"
RESULT_CANDIDATE_CONTRACT = "asha.orchestration-result-candidate.v1"
SEAL_CONTRACT = "asha.orchestration-seal.v1"
SEAL_PREPARATION_CONTRACT = "asha.orchestration-seal-preparation.v1"
REVIEW_CONTRACT = "asha.orchestration-review.v1"
VERIFICATION_CONTRACT = "asha.orchestration-verification.v1"
APPROVAL_CONTRACT = "asha.orchestration-approval.v1"
ACTION_CONTRACT = "asha.orchestration-action.v1"
EVENT_CONTRACT = "asha.orchestration-event.v1"
LINK_CONTRACT = "asha.orchestration-link.v1"
EVIDENCE_CONTRACT = "asha.orchestration-evidence.v1"
BUNDLE_CONTRACT = "asha.orchestration-bundle.v1"
COORDINATOR_CONTRACT = "asha.orchestration-coordinator.v1"
COORDINATOR_CHECKPOINT_CONTRACT = "asha.orchestration-coordinator-checkpoint.v1"
COORDINATOR_PROTOCOL_VERSION = 1
MAX_CHECKPOINT_RATIONALE_BYTES = 4096
MAX_CHECKPOINT_NODES = 64

# Text byte caps.
MAX_SLUG_BYTES = 64
MAX_LABEL_BYTES = 200
MAX_OBJECTIVE_BYTES = 8192
MAX_CRITERION_BYTES = 2048
MAX_GOAL_BYTES = 4096
MAX_ACCEPTANCE_BYTES = 4096
MAX_SUMMARY_BYTES = 8192
MAX_CONCERN_BYTES = 2048
MAX_FOLLOW_UP_BYTES = 2048
MAX_PROJECT_ID_BYTES = 200
MAX_CONTROL_REPOSITORY_ID_BYTES = 200
MAX_ROLE_BYTES = 64
MAX_ACTOR_ID_BYTES = 200
MAX_TOKEN_BYTES = 128
MAX_PATH_BYTES = 4096
MAX_ARG_BYTES = 4096
MAX_FINDING_BYTES = 4096
MAX_EVIDENCE_LOCATION_BYTES = 4096
MAX_EVENT_PAYLOAD_BYTES = 16384
MAX_ACTION_OUTCOME_BYTES = 8192
MAX_REFUSAL_BYTES = 2048
MAX_RESULT_TRANSPORT_BYTES = 256 * 1024
MAX_EVIDENCE_SUMMARY_BYTES = 128 * 1024

# Array item caps.
MAX_ACCEPTANCE_CRITERIA_ITEMS = 64
MAX_REPOSITORIES = 32
MAX_NODES = 256
MAX_DEPENDENCIES = 64
MAX_PATH_ITEMS = 512
MAX_DECLARED_GATES = 64
MAX_ACTION_CLASSES = 64
MAX_SEAL_INPUTS = 64
MAX_ATTESTATIONS = 64
MAX_ARGV_ITEMS = 128
MAX_CONCERNS = 64
MAX_FOLLOW_UP_ITEMS = 64
MAX_FINDINGS = 128
MAX_EVIDENCE_IDS = 256
MAX_SUBJECT_IDS = 64
MAX_BUNDLE_MEMBERS = 32
MAX_VERIFICATION_COMMANDS = 128
MAX_VERIFICATION_TIMEOUT_SECONDS = 3600
MAX_VERIFICATION_COMMAND_SPEC_BYTES = 32 * 1024

MAX_EVENT_SEQUENCE = 999999
MAX_STATE_REVISION = 2**63 - 1

FORBIDDEN_ACTION_CLASSES = (
    "external-write",
    "tracker-mutation",
    "publication",
    "integration",
    "merge",
    "rebase",
    "bookmark-movement",
    "push",
    "workspace-removal",
    "retention-expiry",
    "deletion",
    "sealed-workspace-mutation",
)
# Closure stays journalable when no plan ever became active.
NULL_ACTIVE_PLAN_ACTION_CLASSES = frozenset({"finalize", "archive", "unarchive"})

NODE_TYPES = frozenset({"work", "research", "review", "compose", "verify", "decision"})
MUTATING_NODE_TYPES = frozenset({"work", "compose"})
WORKFLOWS = frozenset({"none", "code-orchestrate", "session-loop"})
SUPPORTED_ROLES = frozenset({
    "worker", "implementer", "researcher", "reviewer", "composer",
    "controller", "operator", "verifier", "decision-maker", "tdd",
    "debugger", "codebase-historian", "refactor-cleaner", "claim-verifier",
    "security-reviewer",
})

_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)
_GIT_OBJECT_ID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", re.ASCII)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z",
    re.ASCII,
)


class ModelError(ValueError):
    """An orchestration record violates its closed v1 contract."""


def new_uuid() -> str:
    return str(uuid.uuid4())


def canonical_uuid(value: Any, name: str = "identifier") -> str:
    if not isinstance(value, str):
        raise ModelError(f"{name} must be a canonical UUID string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ModelError(f"{name} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ModelError(f"{name} must be a canonical UUID")
    return value


def _object(value: Any, name: str, keys: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError(f"{name} must be an object")
    missing = keys - value.keys()
    extra = value.keys() - keys
    if missing:
        raise ModelError(f"{name} is missing {len(missing)} required field(s)")
    if extra:
        raise ModelError(f"{name} has {len(extra)} unexpected field(s)")
    return value


def _text(
    value: Any,
    name: str,
    *,
    maximum: int,
    minimum: int = 1,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise ModelError(f"{name} must be text")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ModelError(f"{name} must not contain Unicode control characters")
    size = len(value.encode("utf-8"))
    if not minimum <= size <= maximum:
        raise ModelError(f"{name} must contain {minimum}-{maximum} UTF-8 bytes")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ModelError(f"{name} uses an invalid restricted grammar")
    return value


def _optional_text(value: Any, name: str, *, maximum: int) -> str | None:
    if value is None:
        return None
    return _text(value, name, maximum=maximum)


def validate_slug(value: Any, name: str = "slug") -> str:
    return _text(value, name, maximum=MAX_SLUG_BYTES, pattern=_SLUG)


def _token(value: Any, name: str) -> str:
    return _text(value, name, maximum=MAX_TOKEN_BYTES, pattern=_TOKEN)


def _digest(value: Any, name: str) -> str:
    return _text(value, name, maximum=64, pattern=_DIGEST)


def _git_object_id(value: Any, name: str) -> str:
    return _text(value, name, maximum=64, pattern=_GIT_OBJECT_ID)


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ModelError(f"{name} must use bounded ASCII RFC3339 UTC Z form")
    try:
        result = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ModelError(f"{name} must be RFC3339 UTC") from exc
    if result.tzinfo != timezone.utc:
        raise ModelError(f"{name} must be RFC3339 UTC")
    return result


def _integer(
    value: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_STATE_REVISION,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ModelError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _array(value: Any, name: str, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        raise ModelError(f"{name} must be an array")
    if len(value) > maximum:
        raise ModelError(f"{name} must contain at most {maximum} items")
    return value


def _unique(values: list[str], name: str) -> None:
    if len(values) != len(set(values)):
        raise ModelError(f"{name} must not contain duplicates")


def _canonical_path(value: Any, name: str) -> str:
    path = _text(value, name, maximum=MAX_PATH_BYTES)
    if not is_canonical_absolute_path(path, resolved=True):
        raise ModelError(f"{name} must be an exact resolved canonical path")
    return path


def _relative_path(value: Any, name: str) -> str:
    path = _text(value, name, maximum=MAX_PATH_BYTES)
    if (
        path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or (path != "." and (path.endswith("/") or posixpath.normpath(path) != path))
        or path == ".."
        or path.startswith("../")
    ):
        raise ModelError(f"{name} must be a canonical relative POSIX path")
    return path


def _string_list(
    value: Any,
    name: str,
    *,
    maximum_items: int,
    maximum_bytes: int,
    validator: Callable[[Any, str], str] | None = None,
    unique: bool = True,
) -> list[str]:
    result = _array(value, name, maximum_items)
    checked: list[str] = []
    for index, item in enumerate(result):
        if validator is None:
            checked.append(_text(item, f"{name}[{index}]", maximum=maximum_bytes))
        else:
            checked.append(validator(item, f"{name}[{index}]"))
    if unique:
        _unique(checked, name)
    return checked


def _nullable_uuid(value: Any, name: str) -> str | None:
    return None if value is None else canonical_uuid(value, name)


def _validate_json_value(value: Any, name: str, *, depth: int = 0) -> None:
    if depth > 16:
        raise ModelError(f"{name} nesting exceeds 16 levels")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if abs(value) > MAX_STATE_REVISION:
            raise ModelError(f"{name} integer is outside the supported range")
        return
    if isinstance(value, float):
        raise ModelError(f"{name} must not contain floating-point values")
    if isinstance(value, str):
        _text(value, name, maximum=MAX_EVENT_PAYLOAD_BYTES, minimum=0)
        return
    if isinstance(value, list):
        if len(value) > 256:
            raise ModelError(f"{name} array exceeds 256 items")
        for index, item in enumerate(value):
            _validate_json_value(item, f"{name}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 256:
            raise ModelError(f"{name} object exceeds 256 fields")
        for key, item in value.items():
            _text(key, f"{name} key", maximum=MAX_TOKEN_BYTES)
            _validate_json_value(item, f"{name}.{key}", depth=depth + 1)
        return
    raise ModelError(f"{name} contains a non-JSON value")


def _bounded_payload(value: Any, name: str) -> Any:
    _validate_json_value(value, name)
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ModelError(f"{name} is not canonical JSON") from exc
    if len(raw) > MAX_EVENT_PAYLOAD_BYTES:
        raise ModelError(f"{name} exceeds {MAX_EVENT_PAYLOAD_BYTES} UTF-8 bytes")
    return value


def _transport_payload(value: Any, name: str) -> Any:
    """Validate a strict JSON value at the result transport cap only."""
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ModelError(f"{name} is not strict canonical JSON") from exc
    if len(raw) > MAX_RESULT_TRANSPORT_BYTES:
        raise ModelError(
            f"{name} exceeds {MAX_RESULT_TRANSPORT_BYTES} UTF-8 bytes"
        )
    return value


def validate_repository_scope(value: Any) -> dict[str, Any]:
    repository = _object(value, "repository scope", frozenset({
        "repository_id", "project_id", "root", "control_repository_id",
        "initial_identity_digest",
    }))
    canonical_uuid(repository["repository_id"], "repository_id")
    _text(repository["project_id"], "project_id", maximum=MAX_PROJECT_ID_BYTES)
    _canonical_path(repository["root"], "repository root")
    _text(
        repository["control_repository_id"],
        "control_repository_id",
        maximum=MAX_CONTROL_REPOSITORY_ID_BYTES,
        pattern=_TOKEN,
    )
    _digest(repository["initial_identity_digest"], "initial_identity_digest")
    return copy.deepcopy(repository)


_WORKSPACE_SCOPE_KEYS = frozenset({
    "workspace_id", "project_id", "root", "manifest_membership_digest", "repositories",
})


def validate_workspace_scope(value: Any) -> dict[str, Any]:
    """A declared Asha workspace binding several member repositories (Increment 7)."""
    workspace = _object(value, "workspace scope", _WORKSPACE_SCOPE_KEYS)
    canonical_uuid(workspace["workspace_id"], "workspace_id")
    _text(workspace["project_id"], "workspace project_id", maximum=MAX_PROJECT_ID_BYTES)
    _canonical_path(workspace["root"], "workspace root")
    _digest(workspace["manifest_membership_digest"], "workspace manifest_membership_digest")
    members = _array(workspace["repositories"], "workspace repositories", MAX_REPOSITORIES)
    if len(members) < 1:
        raise ModelError("workspace scope requires at least one member repository")
    seen: list[str] = []
    for index, member in enumerate(members):
        checked = validate_repository_scope(member)
        if not checked["root"].startswith(workspace["root"].rstrip("/") + "/"):
            raise ModelError(f"workspace repositories[{index}] root is outside the workspace root")
        seen.append(checked["repository_id"])
    _unique(seen, "workspace repository IDs")
    return copy.deepcopy(workspace)


def scope_repositories(initiative: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Ordered member repository scopes of an initiative (one for repository kind)."""
    scope = initiative["scope"]
    if scope.get("kind", "repository") == "workspace":
        return [copy.deepcopy(item) for item in scope["workspace"]["repositories"]]
    return [copy.deepcopy(scope["repository"])]


def repository_by_id(initiative: Mapping[str, Any], repository_id: str) -> dict[str, Any]:
    for repository in scope_repositories(initiative):
        if repository["repository_id"] == repository_id:
            return repository
    raise ModelError(f"repository {repository_id} is not in the initiative scope")


_LIMIT_KEYS = frozenset({
    "max_parallel", "max_total_tasks", "max_attempts_per_node",
    "max_repair_cycles", "max_retained_bytes_before_pause",
    "max_retained_inodes_before_pause", "deadline",
})


def validate_limits(value: Any, name: str = "limits") -> dict[str, Any]:
    limits = _object(value, name, _LIMIT_KEYS)
    for field in _LIMIT_KEYS - {"deadline"}:
        _integer(limits[field], f"{name}.{field}", minimum=1)
    if limits["deadline"] is not None:
        _timestamp(limits["deadline"], f"{name}.deadline")
    return copy.deepcopy(limits)


_SCOPE_ORIGIN_KEYS = frozenset({"jj_commit_id", "tree_digest"})


def _scope_origin(value: Any, name: str = "scope_origin") -> dict[str, Any]:
    origin = _object(value, name, _SCOPE_ORIGIN_KEYS)
    _git_object_id(origin["jj_commit_id"], f"{name}.jj_commit_id")
    _digest(origin["tree_digest"], f"{name}.tree_digest")
    return copy.deepcopy(origin)


_SEAL_INPUT_KEYS = frozenset({"seal_id", "outcome", "read_only", "scope_origin"})


def _seal_input(value: Any, name: str) -> dict[str, Any]:
    item = _object(value, name, _SEAL_INPUT_KEYS)
    canonical_uuid(item["seal_id"], f"{name}.seal_id")
    if item["outcome"] not in {"success", "failure", "paused"}:
        raise ModelError(f"{name}.outcome is invalid")
    if not isinstance(item["read_only"], bool):
        raise ModelError(f"{name}.read_only must be boolean")
    _scope_origin(item["scope_origin"], f"{name}.scope_origin")
    return copy.deepcopy(item)


_BASE_POLICY_KEYS = frozenset({
    "policy", "scope_origin", "upstream_node_ids", "seal_inputs",
})


def validate_base_policy(value: Any) -> dict[str, Any]:
    base = _object(value, "node base", _BASE_POLICY_KEYS)
    if base["policy"] not in {"approved-baseline", "upstream-seal", "scope-baseline"}:
        raise ModelError("node base policy is invalid")
    _scope_origin(base["scope_origin"], "node base.scope_origin")
    _string_list(
        base["upstream_node_ids"], "node base.upstream_node_ids",
        maximum_items=MAX_DEPENDENCIES, maximum_bytes=MAX_SLUG_BYTES,
        validator=validate_slug,
    )
    inputs = _array(base["seal_inputs"], "node base.seal_inputs", MAX_SEAL_INPUTS)
    seen: list[str] = []
    for index, item in enumerate(inputs):
        checked = _seal_input(item, f"node base.seal_inputs[{index}]")
        seen.append(checked["seal_id"])
    _unique(seen, "node base seal IDs")
    return copy.deepcopy(base)


INITIATIVE_STATES = (
    "draft", "planning", "awaiting-plan-approval", "approved", "running",
    "needs-input", "paused", "ready-for-integration", "integrated", "partial",
    "failed", "cancelled", "archived",
)
COORDINATOR_STATES = (
    "absent", "starting", "active", "waiting", "needs-input", "stopping",
    "exited", "failed", "stale", "fenced",
)
NODE_STATES = (
    "proposed", "approved", "blocked", "ready", "dispatching", "running",
    "evaluating", "needs-input", "succeeded", "failed", "cancelled",
    "superseded", "stale",
)
ATTEMPT_STATES = (
    "allocated", "dispatching", "running", "reported", "awaiting-exit",
    "success-seal-ready", "failure-seal-ready", "paused-seal-ready", "sealing",
    "sealed-success", "sealed-failure", "sealed-paused", "readonly-ready",
    "completed-readonly", "result-missing", "launch-failed", "abnormal-exit",
    "failed-no-artifact", "indeterminate", "cancelled", "stale",
)


def _edges(states: tuple[str, ...], pairs: list[tuple[str, str]]) -> dict[str, frozenset[str]]:
    result: dict[str, set[str]] = {state: set() for state in states}
    for source, target in pairs:
        if source != target:
            result[source].add(target)
    return {state: frozenset(targets) for state, targets in result.items()}


_INITIATIVE_TERMINAL = frozenset({
    "ready-for-integration", "integrated", "partial", "failed", "cancelled",
    "archived",
})
_INITIATIVE_NONTERMINAL = frozenset(INITIATIVE_STATES) - _INITIATIVE_TERMINAL
INITIATIVE_TERMINAL_STATES = _INITIATIVE_TERMINAL
INITIATIVE_NONTERMINAL_STATES = _INITIATIVE_NONTERMINAL
_initiative_pairs = [
    ("draft", "planning"), ("planning", "awaiting-plan-approval"),
    ("awaiting-plan-approval", "planning"), ("awaiting-plan-approval", "approved"),
    ("approved", "running"), ("running", "needs-input"), ("running", "paused"),
    ("needs-input", "running"), ("paused", "running"),
    ("running", "ready-for-integration"), ("running", "partial"),
    ("running", "failed"), ("draft", "failed"), ("draft", "partial"),
    ("planning", "failed"), ("planning", "partial"),
    ("ready-for-integration", "integrated"),
]
_initiative_pairs += [(state, "cancelled") for state in _INITIATIVE_NONTERMINAL]
_initiative_pairs += [
    (state, "archived")
    for state in ("ready-for-integration", "integrated", "partial", "failed", "cancelled")
]
_initiative_pairs += [
    ("archived", state)
    for state in ("ready-for-integration", "integrated", "partial", "failed", "cancelled")
]
INITIATIVE_TRANSITIONS = _edges(INITIATIVE_STATES, _initiative_pairs)

_COORDINATOR_LIVE = frozenset({"starting", "active", "waiting", "needs-input", "stopping"})
COORDINATOR_TERMINAL_STATES = frozenset({"fenced", "exited", "failed"})
COORDINATOR_LIVE_STATES = _COORDINATOR_LIVE
_coordinator_pairs = [
    ("absent", "starting"), ("starting", "active"), ("active", "waiting"),
    ("waiting", "active"), ("active", "needs-input"),
    ("waiting", "needs-input"), ("needs-input", "active"),
    ("stopping", "exited"),
]
_coordinator_pairs += [(state, "stopping") for state in _COORDINATOR_LIVE]
_coordinator_pairs += [
    (state, "exited") for state in ("starting", "active", "waiting", "needs-input")
]
_coordinator_pairs += [(state, "failed") for state in _COORDINATOR_LIVE]
_coordinator_pairs += [(state, "stale") for state in _COORDINATOR_LIVE]
_coordinator_pairs += [
    (state, "fenced") for state in _COORDINATOR_LIVE | {"stale"}
]
COORDINATOR_TRANSITIONS = _edges(COORDINATOR_STATES, _coordinator_pairs)

_NODE_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "superseded", "stale"})
_NODE_NONTERMINAL = frozenset(NODE_STATES) - _NODE_TERMINAL
_NODE_ACTIVE = frozenset({"dispatching", "running", "evaluating", "needs-input"})
NODE_TERMINAL_STATES = _NODE_TERMINAL
NODE_NONTERMINAL_STATES = _NODE_NONTERMINAL
NODE_ACTIVE_STATES = _NODE_ACTIVE
_node_pairs = [
    ("proposed", "approved"), ("approved", "blocked"), ("approved", "ready"),
    ("blocked", "ready"), ("ready", "dispatching"), ("ready", "evaluating"),
    ("ready", "needs-input"), ("dispatching", "running"),
    ("dispatching", "evaluating"), ("running", "evaluating"),
    ("needs-input", "evaluating"), ("needs-input", "ready"),
    ("evaluating", "succeeded"), ("evaluating", "ready"),
    ("evaluating", "failed"), ("succeeded", "ready"),
]
_node_pairs += [(state, "needs-input") for state in _NODE_ACTIVE]
_node_pairs += [(state, "cancelled") for state in _NODE_NONTERMINAL]
_node_pairs += [(state, "superseded") for state in _NODE_NONTERMINAL | {"succeeded"}]
_node_pairs += [(state, "stale") for state in _NODE_ACTIVE]
NODE_TRANSITIONS = _edges(NODE_STATES, _node_pairs)

_ATTEMPT_TERMINAL = frozenset({
    "sealed-success", "sealed-failure", "sealed-paused", "completed-readonly",
    "launch-failed", "failed-no-artifact", "cancelled", "stale",
})
_ATTEMPT_NONTERMINAL = frozenset(ATTEMPT_STATES) - _ATTEMPT_TERMINAL
_ATTEMPT_ACTIVE = frozenset({
    "dispatching", "running", "reported", "awaiting-exit", "success-seal-ready",
    "failure-seal-ready", "paused-seal-ready", "sealing",
})
ATTEMPT_TERMINAL_STATES = _ATTEMPT_TERMINAL
ATTEMPT_NONTERMINAL_STATES = _ATTEMPT_NONTERMINAL
ATTEMPT_ACTIVE_STATES = _ATTEMPT_ACTIVE
_attempt_pairs = [
    ("allocated", "dispatching"), ("dispatching", "running"),
    ("dispatching", "launch-failed"), ("running", "reported"),
    ("reported", "awaiting-exit"), ("awaiting-exit", "success-seal-ready"),
    ("awaiting-exit", "paused-seal-ready"), ("awaiting-exit", "failure-seal-ready"),
    ("awaiting-exit", "readonly-ready"), ("abnormal-exit", "failure-seal-ready"),
    ("result-missing", "failure-seal-ready"), ("abnormal-exit", "failed-no-artifact"),
    ("result-missing", "failed-no-artifact"), ("success-seal-ready", "sealing"),
    ("failure-seal-ready", "sealing"), ("paused-seal-ready", "sealing"),
    ("sealing", "sealed-success"), ("sealing", "sealed-failure"),
    ("sealing", "sealed-paused"), ("readonly-ready", "completed-readonly"),
    ("running", "result-missing"), ("dispatching", "abnormal-exit"),
    ("running", "abnormal-exit"), ("reported", "abnormal-exit"),
    ("awaiting-exit", "abnormal-exit"),
]
_attempt_pairs += [
    (state, "indeterminate") for state in _ATTEMPT_NONTERMINAL - {"indeterminate"}
]
_attempt_pairs += [
    ("indeterminate", state)
    for state in (_ATTEMPT_NONTERMINAL - {"indeterminate"}) | {"launch-failed"}
]
_attempt_pairs += [(state, "cancelled") for state in _ATTEMPT_NONTERMINAL]
_attempt_pairs += [(state, "stale") for state in _ATTEMPT_ACTIVE]
ATTEMPT_TRANSITIONS = _edges(ATTEMPT_STATES, _attempt_pairs)


def _simple_machine(states: tuple[str, ...], pairs: list[tuple[str, str]]) -> dict[str, frozenset[str]]:
    return _edges(states, pairs)


RESULT_PUBLICATION_STATES = (
    "reserved", "validating", "persisting", "completed", "refused", "indeterminate",
)
RESULT_INGESTION_STATES = (
    "reserved", "ingesting", "completed", "refused", "indeterminate",
)
RESULT_STATES = ("accepted",)
SEAL_STATES = (
    "preparing", "sealed-success", "sealed-failure", "sealed-paused", "indeterminate",
)
REVIEW_STATES = (
    "pending", "running", "submitted", "accepted-pass", "accepted-findings",
    "failed", "indeterminate", "stale",
)
VERIFICATION_STATES = (
    "pending", "dispatching", "running", "passed", "failed", "indeterminate", "stale",
)
BUNDLE_STATES = ("binding", "compatible", "incompatible", "indeterminate")
APPROVAL_STATES = (
    "requested", "approved", "rejected", "expired", "cancelled", "consumed",
    "revoked-before-use",
)
ACTION_STATES = (
    "received", "validated", "dispatching", "completed", "refused", "indeterminate",
)
_rp_pairs = [
    ("reserved", "validating"), ("validating", "persisting"),
    ("persisting", "completed"), ("reserved", "refused"),
    ("validating", "refused"),
]
_rp_pairs += [(state, "indeterminate") for state in ("reserved", "validating", "persisting")]
_rp_pairs += [
    ("indeterminate", state)
    for state in ("reserved", "validating", "persisting", "completed", "refused")
]
RESULT_PUBLICATION_TRANSITIONS = _simple_machine(RESULT_PUBLICATION_STATES, _rp_pairs)
RESULT_INGESTION_TRANSITIONS = _simple_machine(
    RESULT_INGESTION_STATES,
    [
        ("reserved", "ingesting"), ("reserved", "refused"),
        ("ingesting", "completed"), ("ingesting", "refused"),
        ("ingesting", "indeterminate"),
        ("indeterminate", "ingesting"), ("indeterminate", "completed"),
        ("indeterminate", "refused"),
    ],
)
RESULT_TRANSITIONS = {"accepted": frozenset()}
SEAL_TRANSITIONS = _simple_machine(
    SEAL_STATES,
    [
        ("preparing", "sealed-success"), ("preparing", "sealed-failure"),
        ("preparing", "sealed-paused"), ("preparing", "indeterminate"),
    ],
)
_review_pairs = [
    ("pending", "running"), ("running", "submitted"),
    ("submitted", "accepted-pass"), ("submitted", "accepted-findings"),
]
_review_pairs += [
    (state, target)
    for state in ("pending", "running", "submitted")
    for target in ("failed", "indeterminate", "stale")
]
_review_pairs += [
    (state, "stale")
    for state in ("accepted-pass", "accepted-findings", "failed", "indeterminate")
]
REVIEW_TRANSITIONS = _simple_machine(REVIEW_STATES, _review_pairs)
_verification_pairs = [
    ("pending", "dispatching"), ("dispatching", "running"),
    ("running", "passed"), ("running", "failed"),
]
_verification_pairs += [
    (state, target)
    for state in ("pending", "dispatching", "running")
    for target in ("indeterminate", "stale")
]
_verification_pairs += [
    (state, "stale") for state in ("passed", "failed", "indeterminate")
]
VERIFICATION_TRANSITIONS = _simple_machine(VERIFICATION_STATES, _verification_pairs)
BUNDLE_TRANSITIONS = _simple_machine(
    BUNDLE_STATES,
    [
        ("binding", "compatible"), ("binding", "incompatible"),
        ("binding", "indeterminate"), ("indeterminate", "compatible"),
        ("indeterminate", "incompatible"), ("indeterminate", "binding"),
    ],
)
APPROVAL_TRANSITIONS = _simple_machine(
    APPROVAL_STATES,
    [
        ("requested", "approved"), ("requested", "rejected"),
        ("requested", "expired"), ("requested", "cancelled"),
        ("approved", "consumed"), ("approved", "expired"),
        ("approved", "revoked-before-use"),
    ],
)
ACTION_TRANSITIONS = _simple_machine(
    ACTION_STATES,
    [
        ("received", "validated"), ("validated", "dispatching"),
        ("dispatching", "completed"), ("validated", "completed"),
        ("received", "refused"), ("validated", "refused"),
        ("dispatching", "indeterminate"), ("indeterminate", "completed"),
        ("indeterminate", "refused"),
    ],
)

MACHINES = {
    "initiative": INITIATIVE_TRANSITIONS,
    "coordinator": COORDINATOR_TRANSITIONS,
    "node": NODE_TRANSITIONS,
    "attempt": ATTEMPT_TRANSITIONS,
    "result-publication": RESULT_PUBLICATION_TRANSITIONS,
    "result-ingestion": RESULT_INGESTION_TRANSITIONS,
    "result": RESULT_TRANSITIONS,
    "seal": SEAL_TRANSITIONS,
    "review": REVIEW_TRANSITIONS,
    "verification": VERIFICATION_TRANSITIONS,
    "bundle": BUNDLE_TRANSITIONS,
    "approval": APPROVAL_TRANSITIONS,
    "action": ACTION_TRANSITIONS,
}


def require_transition(
    machine: str | Mapping[str, frozenset[str]], current: str, target: str
) -> None:
    transitions = MACHINES.get(machine) if isinstance(machine, str) else machine
    if transitions is None or current not in transitions or target not in transitions[current]:
        name = machine if isinstance(machine, str) else "record"
        raise ModelError(f"illegal {name} transition: {current} -> {target}")


_INITIATIVE_KEYS = frozenset({
    "contract", "initiative_id", "slug", "label", "state", "objective",
    "acceptance_criteria", "scope", "active_plan", "limits", "coordinator",
    "state_revision", "forbidden_action_classes", "last_event_sequence",
    "created_at", "updated_at",
})


def validate_initiative(value: Any) -> dict[str, Any]:
    record = _object(value, "initiative", _INITIATIVE_KEYS)
    if record["contract"] not in INITIATIVE_CONTRACTS:
        raise ModelError(
            f"initiative contract must be {INITIATIVE_CONTRACT} or {INITIATIVE_CONTRACT_V2}"
        )
    canonical_uuid(record["initiative_id"], "initiative_id")
    validate_slug(record["slug"], "initiative slug")
    _text(record["label"], "initiative label", maximum=MAX_LABEL_BYTES)
    if record["state"] not in INITIATIVE_TRANSITIONS:
        raise ModelError("initiative state is invalid")
    _text(record["objective"], "initiative objective", maximum=MAX_OBJECTIVE_BYTES)
    _string_list(
        record["acceptance_criteria"], "initiative acceptance_criteria",
        maximum_items=MAX_ACCEPTANCE_CRITERIA_ITEMS, maximum_bytes=MAX_CRITERION_BYTES,
    )
    if not record["acceptance_criteria"]:
        raise ModelError("initiative acceptance_criteria must not be empty")
    if not isinstance(record["scope"], dict):
        raise ModelError("initiative scope must be an object")
    kind = record["scope"].get("kind")
    if kind == "repository":
        scope = _object(record["scope"], "initiative scope", frozenset({"kind", "repository"}))
        validate_repository_scope(scope["repository"])
    elif kind == "workspace" and record["contract"] == INITIATIVE_CONTRACT_V2:
        scope = _object(record["scope"], "initiative scope", frozenset({"kind", "workspace"}))
        validate_workspace_scope(scope["workspace"])
    elif kind == "workspace":
        raise ModelError("workspace scope requires the asha.orchestration-initiative.v2 contract")
    else:
        raise ModelError("initiative scope kind must be repository or workspace")
    if record["active_plan"] is not None:
        active = _object(
            record["active_plan"], "initiative active_plan",
            frozenset({"revision", "digest", "approval_id"}),
        )
        _integer(active["revision"], "active plan revision", minimum=1)
        _digest(active["digest"], "active plan digest")
        canonical_uuid(active["approval_id"], "active plan approval_id")
    validate_limits(record["limits"], "initiative limits")
    if record["coordinator"] is not None:
        raise ModelError("Core initiative coordinator must be null")
    _integer(record["state_revision"], "initiative state_revision")
    actions = _string_list(
        record["forbidden_action_classes"], "initiative forbidden_action_classes",
        maximum_items=MAX_ACTION_CLASSES, maximum_bytes=MAX_TOKEN_BYTES,
        validator=_token,
    )
    if tuple(actions) != FORBIDDEN_ACTION_CLASSES:
        raise ModelError(
            "initiative forbidden_action_classes must match the fixed Core v1 denial set"
        )
    _integer(
        record["last_event_sequence"], "initiative last_event_sequence",
        maximum=MAX_EVENT_SEQUENCE,
    )
    created = _timestamp(record["created_at"], "initiative created_at")
    updated = _timestamp(record["updated_at"], "initiative updated_at")
    if updated < created:
        raise ModelError("initiative updated_at must not precede created_at")
    return copy.deepcopy(record)


_NODE_KEYS = frozenset({
    "contract", "node_id", "type", "goal", "role", "workflow", "harness",
    "repository_id", "base", "dependencies", "advisory_path_ownership",
    "hard_write_scope", "acceptance", "terminal_candidate", "state",
})


def validate_node(value: Any) -> dict[str, Any]:
    expected = _NODE_KEYS | ({"conflict_policy"} if isinstance(value, dict) and value.get("type") == "compose" else set())
    expected = expected | ({"interactive"} if isinstance(value, dict) and "interactive" in value else set())
    node = _object(value, "node", frozenset(expected))
    if "interactive" in node and not isinstance(node["interactive"], bool):
        raise ModelError("node interactive must be a boolean")
    if node["contract"] != NODE_CONTRACT:
        raise ModelError(f"node contract must be {NODE_CONTRACT}")
    validate_slug(node["node_id"], "node_id")
    if node["type"] not in NODE_TYPES:
        raise ModelError("node type is invalid")
    if node["type"] == "compose":
        if node["conflict_policy"] not in {"fail-on-conflict", "worker-resolves"}:
            raise ModelError("compose conflict_policy is invalid")
    _text(node["goal"], "node goal", maximum=MAX_GOAL_BYTES)
    _text(node["role"], "node role", maximum=MAX_ROLE_BYTES, pattern=_TOKEN)
    if node["workflow"] not in WORKFLOWS:
        raise ModelError("node workflow is invalid")
    if node["harness"] is not None:
        _token(node["harness"], "node harness")
    if node["repository_id"] is not None:
        canonical_uuid(node["repository_id"], "node repository_id")
    if node["base"] is not None:
        validate_base_policy(node["base"])
    _string_list(
        node["dependencies"], "node dependencies", maximum_items=MAX_DEPENDENCIES,
        maximum_bytes=MAX_SLUG_BYTES, validator=validate_slug,
    )
    for field in ("advisory_path_ownership", "hard_write_scope"):
        _string_list(
            node[field], f"node {field}", maximum_items=MAX_PATH_ITEMS,
            maximum_bytes=MAX_PATH_BYTES, validator=_relative_path,
        )
    _optional_text(node["acceptance"], "node acceptance", maximum=MAX_ACCEPTANCE_BYTES)
    if not isinstance(node["terminal_candidate"], bool):
        raise ModelError("node terminal_candidate must be boolean")
    if node["state"] not in NODE_TRANSITIONS:
        raise ModelError("node state is invalid")
    return copy.deepcopy(node)


_PLAN_KEYS = frozenset({
    "contract", "initiative_id", "revision", "digest", "status", "repositories",
    "limits", "declared_gates", "nested_workflow_policy", "acceptance_conditions",
    "action_classes", "nodes",
})


def _validate_plan_record(
    value: Any, *, historical_verification_gates: bool,
) -> dict[str, Any]:
    plan = _object(value, "plan", _PLAN_KEYS)
    if plan["contract"] != PLAN_CONTRACT:
        raise ModelError(f"plan contract must be {PLAN_CONTRACT}")
    canonical_uuid(plan["initiative_id"], "plan initiative_id")
    _integer(plan["revision"], "plan revision", minimum=1)
    if plan["digest"] is not None:
        _digest(plan["digest"], "plan digest")
    if plan["status"] not in {"proposed", "approved", "superseded"}:
        raise ModelError("plan status is invalid")
    repositories = _array(plan["repositories"], "plan repositories", MAX_REPOSITORIES)
    if not repositories:
        raise ModelError("plan repositories must not be empty")
    for item in repositories:
        validate_repository_scope(item)
    validate_limits(plan["limits"], "plan limits")
    gates = _array(plan["declared_gates"], "plan declared_gates", MAX_DECLARED_GATES)
    for index, gate_value in enumerate(gates):
        gate_kind = gate_value.get("kind") if isinstance(gate_value, dict) else None
        gate_keys = {"kind", "node_id", "required"}
        if gate_kind == "verification" and not historical_verification_gates:
            gate_keys.update({"commands", "environment_policy"})
        gate = _object(
            gate_value, f"plan declared_gates[{index}]",
            frozenset(gate_keys),
        )
        if gate["kind"] not in {"review", "verification"}:
            raise ModelError("declared gate kind is invalid")
        validate_slug(gate["node_id"], "declared gate node_id")
        if not isinstance(gate["required"], bool):
            raise ModelError("declared gate required must be boolean")
        if gate["kind"] == "verification" and not historical_verification_gates:
            if gate["environment_policy"] != "minimal":
                raise ModelError("verification environment_policy must be minimal")
            commands = _array(
                gate["commands"], "verification gate commands",
                MAX_VERIFICATION_COMMANDS,
            )
            if not commands:
                raise ModelError("verification gate commands must not be empty")
            for command_index, command_value in enumerate(commands):
                command = _object(
                    command_value,
                    f"verification gate commands[{command_index}]",
                    frozenset({"argv", "cwd", "timeout_seconds"}),
                )
                argv = _string_list(
                    command["argv"], "verification gate argv",
                    maximum_items=MAX_ARGV_ITEMS, maximum_bytes=MAX_ARG_BYTES,
                    unique=False,
                )
                if not argv:
                    raise ModelError("verification gate argv must not be empty")
                _relative_path(command["cwd"], "verification gate cwd")
                _integer(
                    command["timeout_seconds"], "verification gate timeout_seconds",
                    minimum=1, maximum=MAX_VERIFICATION_TIMEOUT_SECONDS,
                )
                if len(_canonical_bytes(command)) > MAX_VERIFICATION_COMMAND_SPEC_BYTES:
                    raise ModelError(
                        "verification gate command exceeds the immutable evidence capacity"
                    )
    nested = _object(
        plan["nested_workflow_policy"], "plan nested_workflow_policy",
        frozenset({"workflow", "single_writer"}),
    )
    if nested["workflow"] not in WORKFLOWS:
        raise ModelError("nested workflow policy is invalid")
    if not isinstance(nested["single_writer"], bool):
        raise ModelError("nested workflow single_writer must be boolean")
    _string_list(
        plan["acceptance_conditions"], "plan acceptance_conditions",
        maximum_items=MAX_ACCEPTANCE_CRITERIA_ITEMS, maximum_bytes=MAX_CRITERION_BYTES,
    )
    _string_list(
        plan["action_classes"], "plan action_classes", maximum_items=MAX_ACTION_CLASSES,
        maximum_bytes=MAX_TOKEN_BYTES, validator=_token,
    )
    nodes = _array(plan["nodes"], "plan nodes", MAX_NODES)
    if not nodes:
        raise ModelError("plan nodes must not be empty")
    for item in nodes:
        validate_node(item)
    return copy.deepcopy(plan)


def validate_plan_record(value: Any) -> dict[str, Any]:
    return _validate_plan_record(value, historical_verification_gates=False)


def _validate_retained_plan_observation(value: Any) -> dict[str, Any]:
    """Validate current plans or the exact retained Increment 1 gate shape."""
    try:
        return validate_plan_record(value)
    except ModelError:
        return _validate_plan_record(value, historical_verification_gates=True)


validate_plan = validate_plan_record


_ATTEMPT_KEYS = frozenset({
    "contract", "attempt_id", "initiative_id", "node_id", "task_id", "action_id",
    "ordinal", "base", "state", "result_publication_id", "result_id", "seal_id",
    "created_at", "updated_at",
})


def validate_attempt(value: Any) -> dict[str, Any]:
    attempt = _object(value, "attempt", _ATTEMPT_KEYS)
    if attempt["contract"] != ATTEMPT_CONTRACT:
        raise ModelError(f"attempt contract must be {ATTEMPT_CONTRACT}")
    for field in ("attempt_id", "initiative_id", "task_id"):
        canonical_uuid(attempt[field], f"attempt {field}")
    validate_slug(attempt["node_id"], "attempt node_id")
    _integer(attempt["ordinal"], "attempt ordinal", minimum=1)
    validate_base_policy(attempt["base"])
    if attempt["state"] not in ATTEMPT_TRANSITIONS:
        raise ModelError("attempt state is invalid")
    if attempt["action_id"] is None:
        if attempt["state"] != "allocated":
            raise ModelError("only an allocated attempt may have a null action_id")
    else:
        canonical_uuid(attempt["action_id"], "attempt action_id")
    for field in ("result_publication_id", "result_id", "seal_id"):
        _nullable_uuid(attempt[field], f"attempt {field}")
    created = _timestamp(attempt["created_at"], "attempt created_at")
    updated = _timestamp(attempt["updated_at"], "attempt updated_at")
    if updated < created:
        raise ModelError("attempt updated_at must not precede created_at")
    return copy.deepcopy(attempt)


_ATTESTATION_KEYS = frozenset({
    "argv", "cwd", "exit_code", "finished_at", "output_digest", "summary",
})


def _attestation(value: Any, name: str) -> dict[str, Any]:
    item = _object(value, name, _ATTESTATION_KEYS)
    _string_list(
        item["argv"], f"{name}.argv", maximum_items=MAX_ARGV_ITEMS,
        maximum_bytes=MAX_ARG_BYTES,
    )
    _relative_path(item["cwd"], f"{name}.cwd")
    _integer(item["exit_code"], f"{name}.exit_code", minimum=-(2**31), maximum=2**31 - 1)
    _timestamp(item["finished_at"], f"{name}.finished_at")
    _digest(item["output_digest"], f"{name}.output_digest")
    _text(item["summary"], f"{name}.summary", maximum=MAX_SUMMARY_BYTES)
    return copy.deepcopy(item)


_RESULT_KEYS = frozenset({
    "contract", "publication_id", "result_id", "payload_digest",
    "supersedes_result_id", "initiative_id", "node_id", "attempt_id", "task_id",
    "run_id", "claim_status", "summary", "files_changed",
    "verification_attestations", "concerns", "follow_up", "published_at",
})
_RESULT_PROVENANCE_KEYS = frozenset({
    "publication_provenance", "claimed_commit_id", "commit_provenance",
})
_PUBLICATION_PROVENANCE_KEYS = frozenset({
    "method", "producer_run_id", "ingestion_id", "ingester_actor_kind",
    "ingester_actor_id", "ingester_coordinator_generation",
})
_COMMIT_PROVENANCE_KEYS = frozenset({
    "creator", "actor_id", "verification_evidence_ids",
})

_REVIEW_FINDING_KEYS = frozenset({"severity", "location", "summary"})
_REVIEW_RESULT_TARGET_KEYS = frozenset({
    "specification_digest", "active_plan_digest", "repository_id", "seal_id",
    "jj_commit_id", "base_seal_ids", "diff_digest",
})
_REVIEW_RESULT_KEYS = frozenset({"verdict", "findings", "target"})


def _review_findings(value: Any, name: str) -> list[dict[str, Any]]:
    findings = _array(value, name, MAX_FINDINGS)
    for index, item_value in enumerate(findings):
        item = _object(item_value, f"{name}[{index}]", _REVIEW_FINDING_KEYS)
        if item["severity"] not in {"low", "medium", "high", "critical"}:
            raise ModelError("review finding severity is invalid")
        _relative_path(item["location"], "review finding location")
        _text(item["summary"], "review finding summary", maximum=MAX_FINDING_BYTES)
    return copy.deepcopy(findings)


def _review_target(value: Any, name: str) -> dict[str, Any]:
    target = _object(value, name, _REVIEW_RESULT_TARGET_KEYS)
    for field in ("specification_digest", "active_plan_digest", "diff_digest"):
        _digest(target[field], f"{name}.{field}")
    for field in ("repository_id", "seal_id"):
        canonical_uuid(target[field], f"{name}.{field}")
    _git_object_id(target["jj_commit_id"], f"{name}.jj_commit_id")
    _string_list(
        target["base_seal_ids"], f"{name}.base_seal_ids",
        maximum_items=MAX_SEAL_INPUTS, maximum_bytes=36, validator=canonical_uuid,
    )
    return copy.deepcopy(target)


def _publication_provenance(value: Any, name: str) -> dict[str, Any]:
    provenance = _object(value, name, _PUBLICATION_PROVENANCE_KEYS)
    if provenance["method"] not in {"direct-worker", "controller-ingestion"}:
        raise ModelError(f"{name}.method is invalid")
    canonical_uuid(provenance["producer_run_id"], f"{name}.producer_run_id")
    if provenance["method"] == "direct-worker":
        if any(provenance[field] is not None for field in (
            "ingestion_id", "ingester_actor_kind", "ingester_actor_id",
            "ingester_coordinator_generation",
        )):
            raise ModelError(f"{name} direct-worker method cannot name an ingester")
    else:
        canonical_uuid(provenance["ingestion_id"], f"{name}.ingestion_id")
        if provenance["ingester_actor_kind"] not in {"controller", "coordinator"}:
            raise ModelError(f"{name}.ingester_actor_kind is invalid")
        _text(
            provenance["ingester_actor_id"], f"{name}.ingester_actor_id",
            maximum=MAX_ACTOR_ID_BYTES,
        )
        generation = provenance["ingester_coordinator_generation"]
        if provenance["ingester_actor_kind"] == "coordinator":
            _integer(generation, f"{name}.ingester_coordinator_generation", minimum=1)
        elif generation is not None:
            raise ModelError(f"{name} controller ingester cannot name a coordinator generation")
    return copy.deepcopy(provenance)


def _commit_provenance(value: Any, name: str) -> dict[str, Any]:
    provenance = _object(value, name, _COMMIT_PROVENANCE_KEYS)
    if provenance["creator"] not in {"worker", "controller", "none"}:
        raise ModelError(f"{name}.creator is invalid")
    actor_id = provenance["actor_id"]
    if provenance["creator"] == "none":
        if actor_id is not None:
            raise ModelError(f"{name} without a commit cannot name a creator actor")
    else:
        _text(actor_id, f"{name}.actor_id", maximum=MAX_ACTOR_ID_BYTES)
    evidence_ids = _string_list(
        provenance["verification_evidence_ids"],
        f"{name}.verification_evidence_ids", maximum_items=MAX_EVIDENCE_IDS,
        maximum_bytes=36, validator=canonical_uuid,
    )
    if provenance["creator"] == "controller" and not evidence_ids:
        raise ModelError(f"{name} controller-created commit requires verification evidence")
    if provenance["creator"] != "controller" and evidence_ids:
        raise ModelError(f"{name} verification evidence belongs only to controller-created commits")
    return copy.deepcopy(provenance)


def validate_result(value: Any) -> dict[str, Any]:
    has_provenance = isinstance(value, dict) and bool(_RESULT_PROVENANCE_KEYS & value.keys())
    provenance_keys = _RESULT_PROVENANCE_KEYS if has_provenance else set()
    expected = (
        _RESULT_KEYS | provenance_keys
        | ({"review"} if isinstance(value, dict) and "review" in value else set())
    )
    result = _object(value, "result", frozenset(expected))
    if result["contract"] != RESULT_CONTRACT:
        raise ModelError(f"result contract must be {RESULT_CONTRACT}")
    for field in ("publication_id", "result_id", "initiative_id", "attempt_id", "task_id", "run_id"):
        canonical_uuid(result[field], f"result {field}")
    _digest(result["payload_digest"], "result payload_digest")
    _nullable_uuid(result["supersedes_result_id"], "result supersedes_result_id")
    if result["supersedes_result_id"] == result["result_id"]:
        raise ModelError("result cannot supersede itself")
    validate_slug(result["node_id"], "result node_id")
    if result["claim_status"] not in {"completed", "failed", "blocked", "needs-decision"}:
        raise ModelError("result claim_status is invalid")
    _text(result["summary"], "result summary", maximum=MAX_SUMMARY_BYTES)
    _string_list(
        result["files_changed"], "result files_changed", maximum_items=MAX_PATH_ITEMS,
        maximum_bytes=MAX_PATH_BYTES, validator=_relative_path,
    )
    attestations = _array(
        result["verification_attestations"], "result verification_attestations",
        MAX_ATTESTATIONS,
    )
    for index, item in enumerate(attestations):
        _attestation(item, f"result verification_attestations[{index}]")
    _string_list(
        result["concerns"], "result concerns", maximum_items=MAX_CONCERNS,
        maximum_bytes=MAX_CONCERN_BYTES,
    )
    _string_list(
        result["follow_up"], "result follow_up", maximum_items=MAX_FOLLOW_UP_ITEMS,
        maximum_bytes=MAX_FOLLOW_UP_BYTES,
    )
    _timestamp(result["published_at"], "result published_at")
    if has_provenance:
        _publication_provenance(
            result["publication_provenance"], "result publication_provenance",
        )
        commit = result["claimed_commit_id"]
        commit_provenance = _commit_provenance(
            result["commit_provenance"], "result commit_provenance",
        )
        if commit is None:
            if commit_provenance["creator"] != "none":
                raise ModelError("result without a claimed commit requires creator none")
        else:
            _git_object_id(commit, "result claimed_commit_id")
            if commit_provenance["creator"] == "none":
                raise ModelError("result claimed commit requires a creator")
        if result["publication_provenance"]["producer_run_id"] != result["run_id"]:
            raise ModelError("result publication producer must name its run")
    if "review" in result:
        payload = _object(result["review"], "result review", _REVIEW_RESULT_KEYS)
        if payload["verdict"] not in {"pass", "findings"}:
            raise ModelError("result review verdict is invalid")
        findings = _review_findings(payload["findings"], "result review findings")
        _review_target(payload["target"], "result review target")
        if payload["verdict"] == "pass" and findings:
            raise ModelError("passing result review must not contain findings")
        if payload["verdict"] == "findings" and not findings:
            raise ModelError("findings result review requires findings")
        if result["claim_status"] != "completed" or result["files_changed"]:
            raise ModelError("review result requires completed claim and no changed files")
    return copy.deepcopy(result)


_PUBLICATION_KEYS = frozenset({
    "contract", "publication_id", "result_id", "payload_digest", "initiative_id",
    "node_id", "attempt_id", "task_id", "run_id", "state", "body_digest",
    "body", "receipt_sequence", "attempt_revision", "refusal", "created_at",
    "updated_at",
})


def validate_result_publication(value: Any) -> dict[str, Any]:
    has_provenance = isinstance(value, dict) and bool(_RESULT_PROVENANCE_KEYS & value.keys())
    expected = _PUBLICATION_KEYS | (_RESULT_PROVENANCE_KEYS if has_provenance else set())
    record = _object(value, "result publication", frozenset(expected))
    if record["contract"] != RESULT_PUBLICATION_CONTRACT:
        raise ModelError(f"result publication contract must be {RESULT_PUBLICATION_CONTRACT}")
    for field in ("publication_id", "result_id", "initiative_id", "attempt_id", "task_id", "run_id"):
        canonical_uuid(record[field], f"result publication {field}")
    validate_slug(record["node_id"], "result publication node_id")
    _digest(record["payload_digest"], "result publication payload_digest")
    _digest(record["body_digest"], "result publication body_digest")
    _transport_payload(record["body"], "result publication body")
    if record["state"] not in RESULT_PUBLICATION_TRANSITIONS:
        raise ModelError("result publication state is invalid")
    _integer(record["receipt_sequence"], "result publication receipt_sequence", minimum=1)
    _digest(record["attempt_revision"], "result publication attempt_revision")
    _optional_text(record["refusal"], "result publication refusal", maximum=MAX_REFUSAL_BYTES)
    created = _timestamp(record["created_at"], "result publication created_at")
    updated = _timestamp(record["updated_at"], "result publication updated_at")
    if updated < created:
        raise ModelError("result publication updated_at must not precede created_at")
    if has_provenance:
        _publication_provenance(
            record["publication_provenance"],
            "result publication publication_provenance",
        )
        commit = record["claimed_commit_id"]
        commit_provenance = _commit_provenance(
            record["commit_provenance"], "result publication commit_provenance",
        )
        if commit is None:
            if commit_provenance["creator"] != "none":
                raise ModelError(
                    "result publication without a claimed commit requires creator none"
                )
        else:
            _git_object_id(commit, "result publication claimed_commit_id")
            if commit_provenance["creator"] == "none":
                raise ModelError("result publication claimed commit requires a creator")
        if record["publication_provenance"]["producer_run_id"] != record["run_id"]:
            raise ModelError("result publication producer must name its run")
    return copy.deepcopy(record)


_RESULT_CANDIDATE_KEYS = frozenset({
    "contract", "ingestion_id", "attempt_id", "task_id", "run_id",
    "publication_id", "body_digest", "body", "staged_at",
})


def validate_result_candidate(value: Any) -> dict[str, Any]:
    candidate = _object(value, "result candidate", _RESULT_CANDIDATE_KEYS)
    if candidate["contract"] != RESULT_CANDIDATE_CONTRACT:
        raise ModelError(f"result candidate contract must be {RESULT_CANDIDATE_CONTRACT}")
    for field in (
        "ingestion_id", "attempt_id", "task_id", "run_id", "publication_id",
    ):
        canonical_uuid(candidate[field], f"result candidate {field}")
    _digest(candidate["body_digest"], "result candidate body_digest")
    _transport_payload(candidate["body"], "result candidate body")
    _timestamp(candidate["staged_at"], "result candidate staged_at")
    return copy.deepcopy(candidate)


_INGESTER_KEYS = frozenset({"actor_kind", "actor_id", "coordinator_generation"})
_RESULT_INGESTION_KEYS = frozenset({
    "contract", "ingestion_id", "initiative_id", "node_id", "attempt_id",
    "task_id", "run_id", "active_plan_digest", "control_task_identity_digest",
    "workspace_path", "workspace_name", "change_id", "outbox_path", "state",
    "candidate_digest", "publication_id", "result_id", "claimed_commit_id",
    "claimed_tree_digest", "commit_creator", "verification_evidence_ids",
    "ingester", "refusal",
    "created_at", "updated_at",
})


def validate_result_ingestion(value: Any) -> dict[str, Any]:
    record = _object(value, "result ingestion", _RESULT_INGESTION_KEYS)
    if record["contract"] != RESULT_INGESTION_CONTRACT:
        raise ModelError(f"result ingestion contract must be {RESULT_INGESTION_CONTRACT}")
    for field in ("ingestion_id", "initiative_id", "attempt_id", "task_id", "run_id"):
        canonical_uuid(record[field], f"result ingestion {field}")
    validate_slug(record["node_id"], "result ingestion node_id")
    _digest(record["active_plan_digest"], "result ingestion active_plan_digest")
    _digest(
        record["control_task_identity_digest"],
        "result ingestion control_task_identity_digest",
    )
    if (
        not isinstance(record["workspace_path"], str)
        or not is_canonical_absolute_path(record["workspace_path"], resolved=True)
    ):
        raise ModelError("result ingestion workspace_path must be canonical and absolute")
    _text(record["workspace_name"], "result ingestion workspace_name", maximum=MAX_LABEL_BYTES)
    _text(record["change_id"], "result ingestion change_id", maximum=64, pattern=re.compile(r"[k-z]{32}", re.ASCII))
    _relative_path(record["outbox_path"], "result ingestion outbox_path")
    if record["state"] not in RESULT_INGESTION_TRANSITIONS:
        raise ModelError("result ingestion state is invalid")
    for field in ("candidate_digest", "claimed_tree_digest"):
        if record[field] is not None:
            _digest(record[field], f"result ingestion {field}")
    for field in ("publication_id", "result_id"):
        _nullable_uuid(record[field], f"result ingestion {field}")
    if record["claimed_commit_id"] is not None:
        _git_object_id(record["claimed_commit_id"], "result ingestion claimed_commit_id")
    if record["commit_creator"] not in {None, "worker", "controller", "none"}:
        raise ModelError("result ingestion commit_creator is invalid")
    _string_list(
        record["verification_evidence_ids"],
        "result ingestion verification_evidence_ids",
        maximum_items=MAX_EVIDENCE_IDS, maximum_bytes=36, validator=canonical_uuid,
    )
    ingester = record["ingester"]
    if ingester is not None:
        ingester = _object(ingester, "result ingestion ingester", _INGESTER_KEYS)
        if ingester["actor_kind"] not in {"controller", "coordinator"}:
            raise ModelError("result ingestion ingester actor_kind is invalid")
        _text(
            ingester["actor_id"], "result ingestion ingester actor_id",
            maximum=MAX_ACTOR_ID_BYTES,
        )
        generation = ingester["coordinator_generation"]
        if ingester["actor_kind"] == "coordinator":
            _integer(generation, "result ingestion ingester coordinator_generation", minimum=1)
        elif generation is not None:
            raise ModelError("controller result ingester cannot name a coordinator generation")
    _optional_text(record["refusal"], "result ingestion refusal", maximum=MAX_REFUSAL_BYTES)
    created = _timestamp(record["created_at"], "result ingestion created_at")
    updated = _timestamp(record["updated_at"], "result ingestion updated_at")
    if updated < created:
        raise ModelError("result ingestion updated_at must not precede created_at")
    if (record["claimed_commit_id"] is None) != (record["claimed_tree_digest"] is None):
        raise ModelError("result ingestion claimed commit and tree must be bound together")
    if record["commit_creator"] in {"worker", "controller"} and record["claimed_commit_id"] is None:
        raise ModelError("result ingestion commit creator requires a claimed commit")
    if record["commit_creator"] == "none" and record["claimed_commit_id"] is not None:
        raise ModelError("result ingestion creator none cannot bind a commit")
    if record["state"] == "reserved" and any(record[field] is not None for field in (
        "candidate_digest", "publication_id", "result_id", "claimed_commit_id",
        "claimed_tree_digest", "commit_creator", "ingester", "refusal",
    )):
        raise ModelError("reserved result ingestion cannot carry completion fields")
    if record["state"] == "reserved" and record["verification_evidence_ids"]:
        raise ModelError("reserved result ingestion cannot carry verification evidence")
    if record["state"] == "completed" and any(record[field] is None for field in (
        "candidate_digest", "publication_id", "result_id", "commit_creator", "ingester",
    )):
        raise ModelError("completed result ingestion requires its accepted binding")
    if record["state"] == "refused" and record["refusal"] is None:
        raise ModelError("refused result ingestion requires a refusal")
    return copy.deepcopy(record)


_SEAL_PREPARATION_KEYS = frozenset({
    "contract", "seal_id", "initiative_id", "node_id", "attempt_id", "task_id",
    "run_id", "repository_id", "scope_origin", "base",
    "read_only_failure_seal_ids", "result_id", "process_evidence_id", "state",
    "refusal", "created_at", "updated_at",
})
SEAL_PREPARATION_STATES = frozenset({"preparing", "indeterminate", "completed"})


def validate_seal_preparation(value: Any) -> dict[str, Any]:
    record = _object(value, "seal preparation", _SEAL_PREPARATION_KEYS)
    if record["contract"] != SEAL_PREPARATION_CONTRACT:
        raise ModelError(
            f"seal preparation contract must be {SEAL_PREPARATION_CONTRACT}"
        )
    for field in (
        "seal_id", "initiative_id", "attempt_id", "task_id", "run_id",
        "repository_id", "process_evidence_id",
    ):
        canonical_uuid(record[field], f"seal preparation {field}")
    validate_slug(record["node_id"], "seal preparation node_id")
    _scope_origin(record["scope_origin"], "seal preparation scope_origin")
    base = _object(record["base"], "seal preparation base", _SEAL_BASE_KEYS)
    if base["kind"] not in {"repository-baseline", "seal", "composition-inputs"}:
        raise ModelError("seal preparation base kind is invalid")
    if base["jj_commit_id"] is not None:
        _git_object_id(base["jj_commit_id"], "seal preparation base jj_commit_id")
    _digest(base["tree_digest"], "seal preparation base tree_digest")
    _string_list(
        base["seal_ids"], "seal preparation base seal_ids",
        maximum_items=MAX_SEAL_INPUTS, maximum_bytes=36, validator=canonical_uuid,
    )
    if base["kind"] == "repository-baseline":
        if base["jj_commit_id"] is None or base["seal_ids"]:
            raise ModelError(
                "repository-baseline seal preparation requires a commit and no seal IDs"
            )
    elif base["kind"] == "seal":
        if base["jj_commit_id"] is None or len(base["seal_ids"]) != 1:
            raise ModelError(
                "seal preparation base requires a commit and exactly one seal ID"
            )
    elif base["jj_commit_id"] is not None or len(base["seal_ids"]) < 2:
        raise ModelError(
            "composition-inputs seal preparation requires null commit and at least two seal IDs"
        )
    _string_list(
        record["read_only_failure_seal_ids"],
        "seal preparation read_only_failure_seal_ids",
        maximum_items=MAX_SEAL_INPUTS, maximum_bytes=36, validator=canonical_uuid,
    )
    _nullable_uuid(record["result_id"], "seal preparation result_id")
    if record["state"] not in SEAL_PREPARATION_STATES:
        raise ModelError("seal preparation state is invalid")
    _optional_text(
        record["refusal"], "seal preparation refusal", maximum=MAX_REFUSAL_BYTES,
    )
    created = _timestamp(record["created_at"], "seal preparation created_at")
    updated = _timestamp(record["updated_at"], "seal preparation updated_at")
    if updated < created:
        raise ModelError("seal preparation updated_at must not precede created_at")
    return copy.deepcopy(record)


_SEAL_BASE_KEYS = frozenset({"kind", "jj_commit_id", "tree_digest", "seal_ids"})
_SEAL_KEYS = frozenset({
    "contract", "seal_id", "initiative_id", "node_id", "attempt_id", "task_id",
    "run_id", "outcome", "repository_id", "scope_origin", "base",
    "read_only_failure_seal_ids", "jj_commit_id", "tree_digest", "diff_digest",
    "cumulative_diff_digest", "changed_paths", "changed_paths_truncated",
    "changed_paths_digest", "cumulative_changed_paths",
    "cumulative_changed_paths_truncated", "cumulative_changed_paths_digest",
    "result_id", "process_evidence_id", "sealed_at",
})


def validate_seal(value: Any) -> dict[str, Any]:
    has_provenance = isinstance(value, dict) and "commit_provenance" in value
    expected = _SEAL_KEYS | ({"commit_provenance"} if has_provenance else set())
    seal = _object(value, "seal", frozenset(expected))
    if seal["contract"] != SEAL_CONTRACT:
        raise ModelError(f"seal contract must be {SEAL_CONTRACT}")
    for field in (
        "seal_id", "initiative_id", "attempt_id", "task_id", "run_id",
        "repository_id", "process_evidence_id",
    ):
        canonical_uuid(seal[field], f"seal {field}")
    validate_slug(seal["node_id"], "seal node_id")
    if seal["outcome"] not in {"success", "failure", "paused"}:
        raise ModelError("seal outcome is invalid")
    _scope_origin(seal["scope_origin"], "seal scope_origin")
    base = _object(seal["base"], "seal base", _SEAL_BASE_KEYS)
    if base["kind"] not in {"repository-baseline", "seal", "composition-inputs"}:
        raise ModelError("seal base kind is invalid")
    if base["jj_commit_id"] is not None:
        _git_object_id(base["jj_commit_id"], "seal base jj_commit_id")
    _digest(base["tree_digest"], "seal base tree_digest")
    _string_list(
        base["seal_ids"], "seal base seal_ids", maximum_items=MAX_SEAL_INPUTS,
        maximum_bytes=36, validator=canonical_uuid,
    )
    if base["kind"] == "repository-baseline":
        if base["jj_commit_id"] is None or base["seal_ids"]:
            raise ModelError("repository-baseline seal base requires a commit and no seal IDs")
    elif base["kind"] == "seal":
        if base["jj_commit_id"] is None or len(base["seal_ids"]) != 1:
            raise ModelError("seal base requires a commit and exactly one seal ID")
    elif base["jj_commit_id"] is not None or len(base["seal_ids"]) < 2:
        raise ModelError("composition-inputs base requires null commit and at least two seal IDs")
    _string_list(
        seal["read_only_failure_seal_ids"], "seal read_only_failure_seal_ids",
        maximum_items=MAX_SEAL_INPUTS, maximum_bytes=36, validator=canonical_uuid,
    )
    _git_object_id(seal["jj_commit_id"], "seal jj_commit_id")
    for field in ("tree_digest", "diff_digest", "cumulative_diff_digest"):
        _digest(seal[field], f"seal {field}")
    for field in ("changed_paths", "cumulative_changed_paths"):
        _string_list(
            seal[field], f"seal {field}", maximum_items=MAX_PATH_ITEMS,
            maximum_bytes=MAX_PATH_BYTES, validator=_relative_path,
        )
    for field in ("changed_paths", "cumulative_changed_paths"):
        truncated_field = f"{field}_truncated"
        digest_field = f"{field}_digest"
        _integer(
            seal[truncated_field], f"seal {truncated_field}",
            minimum=0, maximum=2**31 - 1,
        )
        _digest(seal[digest_field], f"seal {digest_field}")
        if seal[truncated_field]:
            if len(seal[field]) != MAX_PATH_ITEMS:
                raise ModelError(
                    f"truncated seal {field} must retain exactly the path cap"
                )
        elif seal[digest_field] != hashlib.sha256(
            _canonical_bytes(seal[field])
        ).hexdigest():
            raise ModelError(
                f"seal {digest_field} does not match the complete path list"
            )
    _nullable_uuid(seal["result_id"], "seal result_id")
    if seal["outcome"] in {"success", "paused"} and seal["result_id"] is None:
        raise ModelError("success and paused seals require an accepted result")
    _timestamp(seal["sealed_at"], "seal sealed_at")
    if has_provenance:
        _commit_provenance(seal["commit_provenance"], "seal commit_provenance")
    return copy.deepcopy(seal)


_REVIEW_TARGET_KEYS = frozenset({
    "seal_id", "active_plan_digest", "specification_digest", "repository_id",
    "jj_commit_id", "base_seal_ids", "diff_digest",
})
_REVIEW_KEYS = frozenset({
    "contract", "review_id", "initiative_id", "node_id", "attempt_id", "task_id",
    "run_id", "state", "target", "verdict", "findings", "created_at", "updated_at",
})


def validate_review(value: Any) -> dict[str, Any]:
    review = _object(value, "review", _REVIEW_KEYS)
    if review["contract"] != REVIEW_CONTRACT:
        raise ModelError(f"review contract must be {REVIEW_CONTRACT}")
    for field in ("review_id", "initiative_id"):
        canonical_uuid(review[field], f"review {field}")
    validate_slug(review["node_id"], "review node_id")
    if review["state"] not in REVIEW_TRANSITIONS:
        raise ModelError("review state is invalid")
    for field in ("attempt_id", "task_id", "run_id"):
        _nullable_uuid(review[field], f"review {field}")
        if review["state"] != "pending" and review[field] is None:
            raise ModelError(f"review {field} is required from running onward")
    _review_target(review["target"], "review target")
    if review["verdict"] not in {None, "pass", "findings"}:
        raise ModelError("review verdict is invalid")
    findings = _review_findings(review["findings"], "review findings")
    if review["verdict"] == "pass" and findings:
        raise ModelError("passing review must not contain findings")
    if review["verdict"] == "findings" and not findings:
        raise ModelError("findings verdict requires at least one finding")
    if review["state"] == "accepted-pass" and review["verdict"] != "pass":
        raise ModelError("accepted-pass review requires pass verdict")
    if review["state"] == "accepted-findings" and review["verdict"] != "findings":
        raise ModelError("accepted-findings review requires findings verdict")
    if review["state"] not in {"accepted-pass", "accepted-findings"} and review["verdict"] is not None:
        raise ModelError("non-accepted review must not fix a verdict")
    created = _timestamp(review["created_at"], "review created_at")
    updated = _timestamp(review["updated_at"], "review updated_at")
    if updated < created:
        raise ModelError("review updated_at must not precede created_at")
    return copy.deepcopy(review)


_VERIFICATION_COMMAND_KEYS = frozenset({
    "argv", "cwd", "environment_policy_id", "started_at", "finished_at",
    "exit_code", "signal", "timed_out", "output_digest", "pre_identity_digest",
    "post_identity_digest", "command_id", "timeout_seconds", "process_identity",
    "output_path", "pre_jj_commit_id", "pre_tree_digest", "post_jj_commit_id",
    "post_tree_digest", "pre_identity_status", "post_identity_status",
    "output_truncated", "output_original_bytes",
})
_VERIFICATION_KEYS = frozenset({
    "contract", "verification_id", "initiative_id", "node_id", "bundle_digest",
    "active_plan_digest", "state", "commands", "evidence_ids", "outcome",
    "repository_id", "seal_id", "materialization_id", "materialization_path",
    "created_at", "updated_at",
})


def validate_verification(value: Any) -> dict[str, Any]:
    record = _object(value, "verification", _VERIFICATION_KEYS)
    if record["contract"] != VERIFICATION_CONTRACT:
        raise ModelError(f"verification contract must be {VERIFICATION_CONTRACT}")
    canonical_uuid(record["verification_id"], "verification_id")
    canonical_uuid(record["initiative_id"], "verification initiative_id")
    for field in ("repository_id", "seal_id", "materialization_id"):
        canonical_uuid(record[field], f"verification {field}")
    _canonical_path(record["materialization_path"], "verification materialization_path")
    validate_slug(record["node_id"], "verification node_id")
    _digest(record["bundle_digest"], "verification bundle_digest")
    _digest(record["active_plan_digest"], "verification active_plan_digest")
    if record["state"] not in VERIFICATION_TRANSITIONS:
        raise ModelError("verification state is invalid")
    commands = _array(record["commands"], "verification commands", MAX_VERIFICATION_COMMANDS)
    for index, value in enumerate(commands):
        item = _object(value, f"verification commands[{index}]", _VERIFICATION_COMMAND_KEYS)
        _string_list(
            item["argv"], "verification argv", maximum_items=MAX_ARGV_ITEMS,
            maximum_bytes=MAX_ARG_BYTES, unique=False,
        )
        _relative_path(item["cwd"], "verification cwd")
        _token(item["environment_policy_id"], "verification environment_policy_id")
        canonical_uuid(item["command_id"], "verification command_id")
        _integer(
            item["timeout_seconds"], "verification timeout_seconds",
            minimum=1, maximum=MAX_VERIFICATION_TIMEOUT_SECONDS,
        )
        _token(item["process_identity"], "verification process_identity")
        _canonical_path(item["output_path"], "verification output_path")
        _timestamp(item["started_at"], "verification started_at")
        _timestamp(item["finished_at"], "verification finished_at")
        if item["exit_code"] is not None:
            _integer(
                item["exit_code"], "verification exit_code",
                minimum=-(2**31), maximum=2**31 - 1,
            )
        if item["signal"] is not None:
            _integer(item["signal"], "verification signal", minimum=1, maximum=255)
        if not isinstance(item["timed_out"], bool):
            raise ModelError("verification timed_out must be boolean")
        if not isinstance(item["output_truncated"], bool):
            raise ModelError("verification output_truncated must be boolean")
        _integer(
            item["output_original_bytes"], "verification output_original_bytes",
            minimum=0, maximum=2**63 - 1,
        )
        _digest(item["output_digest"], "verification output_digest")
        for side in ("pre", "post"):
            status = item[f"{side}_identity_status"]
            if status not in {"observed", "indeterminate"}:
                raise ModelError(
                    f"verification {side}_identity_status is invalid"
                )
            fields = (
                f"{side}_identity_digest", f"{side}_jj_commit_id",
                f"{side}_tree_digest",
            )
            if status == "indeterminate":
                if any(item[field] is not None for field in fields):
                    raise ModelError(
                        f"indeterminate verification {side} identity must be null"
                    )
                continue
            _digest(item[fields[0]], f"verification {fields[0]}")
            _git_object_id(item[fields[1]], f"verification {fields[1]}")
            _digest(item[fields[2]], f"verification {fields[2]}")
    _string_list(
        record["evidence_ids"], "verification evidence_ids", maximum_items=MAX_EVIDENCE_IDS,
        maximum_bytes=36, validator=canonical_uuid,
    )
    if record["outcome"] not in {None, "passed", "failed", "indeterminate"}:
        raise ModelError("verification outcome is invalid")
    expected_outcome = {
        "passed": "passed", "failed": "failed", "indeterminate": "indeterminate"
    }.get(record["state"])
    if expected_outcome is not None and record["outcome"] != expected_outcome:
        raise ModelError("terminal verification state and outcome must agree")
    if expected_outcome is None and record["outcome"] is not None:
        raise ModelError("nonterminal verification must not fix an outcome")
    created = _timestamp(record["created_at"], "verification created_at")
    updated = _timestamp(record["updated_at"], "verification updated_at")
    if updated < created:
        raise ModelError("verification updated_at must not precede created_at")
    return copy.deepcopy(record)


_APPROVAL_KEYS = frozenset({
    "contract", "request_id", "initiative_id", "action_class", "binding_digest",
    "active_plan_digest", "expected_state_revision", "actor_kind", "actor_id",
    "state", "expires_at", "rationale", "created_at", "updated_at",
})


def validate_approval(value: Any) -> dict[str, Any]:
    record = _object(value, "approval", _APPROVAL_KEYS)
    if record["contract"] != APPROVAL_CONTRACT:
        raise ModelError(f"approval contract must be {APPROVAL_CONTRACT}")
    canonical_uuid(record["request_id"], "approval request_id")
    canonical_uuid(record["initiative_id"], "approval initiative_id")
    _token(record["action_class"], "approval action_class")
    _digest(record["binding_digest"], "approval binding_digest")
    _digest(record["active_plan_digest"], "approval active_plan_digest")
    _integer(record["expected_state_revision"], "approval expected_state_revision")
    if record["actor_kind"] != "operator":
        raise ModelError("Core approval actor_kind must be operator")
    _text(record["actor_id"], "approval actor_id", maximum=MAX_ACTOR_ID_BYTES)
    if record["state"] not in APPROVAL_TRANSITIONS:
        raise ModelError("approval state is invalid")
    expires = _timestamp(record["expires_at"], "approval expires_at")
    _optional_text(record["rationale"], "approval rationale", maximum=MAX_SUMMARY_BYTES)
    created = _timestamp(record["created_at"], "approval created_at")
    updated = _timestamp(record["updated_at"], "approval updated_at")
    if updated < created:
        raise ModelError("approval updated_at must not precede created_at")
    if expires <= created:
        raise ModelError("approval expires_at must follow created_at")
    return copy.deepcopy(record)


_ACTION_KEYS = frozenset({
    "contract", "action_id", "initiative_id", "actor_kind", "actor_id",
    "action_class", "payload_digest", "active_plan_digest", "expected_state_revision",
    "state", "outcome", "received_at", "updated_at",
})


_ACTION_COORDINATOR_KEYS = frozenset({"coordinator_id", "coordinator_generation"})


def validate_action(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError("action must be an object")
    coordinator_keys = (
        _ACTION_COORDINATOR_KEYS if value.get("actor_kind") == "coordinator" else frozenset()
    )
    record = _object(value, "action", _ACTION_KEYS | coordinator_keys)
    if record["contract"] != ACTION_CONTRACT:
        raise ModelError(f"action contract must be {ACTION_CONTRACT}")
    canonical_uuid(record["action_id"], "action_id")
    canonical_uuid(record["initiative_id"], "action initiative_id")
    if record["actor_kind"] not in {"operator", "coordinator"}:
        raise ModelError("action actor_kind must be operator or coordinator")
    if record["actor_kind"] == "coordinator":
        canonical_uuid(record["coordinator_id"], "action coordinator_id")
        _integer(record["coordinator_generation"], "action coordinator_generation", minimum=1)
    _text(record["actor_id"], "action actor_id", maximum=MAX_ACTOR_ID_BYTES)
    _token(record["action_class"], "action class")
    _digest(record["payload_digest"], "action payload_digest")
    if record["active_plan_digest"] is None:
        if record["action_class"] not in NULL_ACTIVE_PLAN_ACTION_CLASSES:
            raise ModelError(
                "action active_plan_digest may be null only for finalize, archive, or unarchive"
            )
    else:
        _digest(record["active_plan_digest"], "action active_plan_digest")
    _integer(record["expected_state_revision"], "action expected_state_revision")
    if record["state"] not in ACTION_TRANSITIONS:
        raise ModelError("action state is invalid")
    _optional_text(record["outcome"], "action outcome", maximum=MAX_ACTION_OUTCOME_BYTES)
    received = _timestamp(record["received_at"], "action received_at")
    updated = _timestamp(record["updated_at"], "action updated_at")
    if updated < received:
        raise ModelError("action updated_at must not precede received_at")
    return copy.deepcopy(record)


_EVENT_KEYS = frozenset({
    "contract", "sequence", "event_id", "initiative_id", "type", "actor_kind",
    "actor_id", "subject_ids", "payload_digest", "payload", "recorded_at",
})

EVENT_TYPES = frozenset({
    "initiative-created", "plan-proposed", "plan-approved", "plan-rejected",
    "approval-requested", "approval-decided",
    "initiative-state-changed", "coordinator-handshake-accepted",
    "coordinator-generation-fenced", "node-ready", "action-received",
    "action-refused", "action-indeterminate", "attempt-started",
    "task-status-observed", "result-published", "result-missing", "seal-preparing",
    "seal-published", "seal-drift-detected", "review-submitted", "review-accepted",
    "verification-started", "verification-finished", "node-state-changed",
    "directive-accepted", "directive-delivered", "limit-reached",
    "storage-threshold-reached", "coordinator-checkpointed", "coordinator-restarted",
    "reconciliation-conflict", "seal-integration-recorded",
})


def validate_event(value: Any) -> dict[str, Any]:
    record = _object(value, "event", _EVENT_KEYS)
    if record["contract"] != EVENT_CONTRACT:
        raise ModelError(f"event contract must be {EVENT_CONTRACT}")
    _integer(record["sequence"], "event sequence", minimum=1, maximum=MAX_EVENT_SEQUENCE)
    canonical_uuid(record["event_id"], "event_id")
    canonical_uuid(record["initiative_id"], "event initiative_id")
    if record["type"] not in EVENT_TYPES:
        raise ModelError("event type is invalid")
    if record["actor_kind"] not in {"operator", "coordinator", "controller", "worker"}:
        raise ModelError("event actor_kind is invalid")
    _text(record["actor_id"], "event actor_id", maximum=MAX_ACTOR_ID_BYTES)
    _string_list(
        record["subject_ids"], "event subject_ids", maximum_items=MAX_SUBJECT_IDS,
        maximum_bytes=MAX_TOKEN_BYTES, validator=_token,
    )
    _digest(record["payload_digest"], "event payload_digest")
    _bounded_payload(record["payload"], "event payload")
    payload_digest = hashlib.sha256(_canonical_bytes(record["payload"])).hexdigest()
    if record["payload_digest"] != payload_digest:
        raise ModelError(
            "event payload_digest does not match canonical payload bytes"
        )
    _timestamp(record["recorded_at"], "event recorded_at")
    return copy.deepcopy(record)


_LINK_BASE_KEYS = frozenset({
    "contract", "initiative_id", "active_plan_digest", "node_id", "attempt_id",
    "action_id", "actor_kind", "actor_id", "expected_initiative_revision",
    "control_task_id", "control_task_identity_digest", "control_task_record_digest",
})


def validate_link(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError("link must be an object")
    coordinator_keys = (
        {"coordinator_generation"}
        if value.get("actor_kind") == "coordinator"
        else set()
    )
    expected = _LINK_BASE_KEYS | coordinator_keys
    link = _object(value, "link", frozenset(expected))
    if link["contract"] != LINK_CONTRACT:
        raise ModelError(f"link contract must be {LINK_CONTRACT}")
    for field in ("initiative_id", "attempt_id", "action_id", "control_task_id"):
        canonical_uuid(link[field], f"link {field}")
    _digest(link["active_plan_digest"], "link active_plan_digest")
    validate_slug(link["node_id"], "link node_id")
    if link["actor_kind"] not in {"operator", "coordinator"}:
        raise ModelError("link actor_kind is invalid")
    _text(link["actor_id"], "link actor_id", maximum=MAX_ACTOR_ID_BYTES)
    _integer(link["expected_initiative_revision"], "link expected_initiative_revision")
    _digest(link["control_task_identity_digest"], "link control_task_identity_digest")
    _digest(link["control_task_record_digest"], "link control_task_record_digest")
    if link["actor_kind"] == "coordinator":
        _integer(link["coordinator_generation"], "link coordinator_generation", minimum=1)
    return copy.deepcopy(link)


_EVIDENCE_KEYS = frozenset({
    "contract", "evidence_id", "initiative_id", "kind", "subject_id", "digest",
    "summary", "recorded_at",
})


def validate_evidence(value: Any) -> dict[str, Any]:
    record = _object(value, "evidence", _EVIDENCE_KEYS)
    if record["contract"] != EVIDENCE_CONTRACT:
        raise ModelError(f"evidence contract must be {EVIDENCE_CONTRACT}")
    canonical_uuid(record["evidence_id"], "evidence_id")
    canonical_uuid(record["initiative_id"], "evidence initiative_id")
    _token(record["kind"], "evidence kind")
    _token(record["subject_id"], "evidence subject_id")
    _digest(record["digest"], "evidence digest")
    _text(
        record["summary"], "evidence summary",
        maximum=(
            MAX_EVIDENCE_SUMMARY_BYTES
            if record["kind"] in {"seal-evidence", "verification-command"}
            else MAX_SUMMARY_BYTES
        ),
    )
    if record["digest"] != hashlib.sha256(
        record["summary"].encode("utf-8")
    ).hexdigest():
        raise ModelError("evidence digest does not match its summary")
    _timestamp(record["recorded_at"], "evidence recorded_at")
    return copy.deepcopy(record)


_BUNDLE_MEMBER_KEYS = frozenset({
    "repository_id", "seal_id", "jj_commit_id", "tree_digest", "diff_digest",
    "materialization_id", "review_id", "verification_id",
})
_BUNDLE_KEYS = frozenset({
    "contract", "bundle_id", "initiative_id", "aggregate_spec_digest",
    "active_plan_digest", "state", "members", "controller_evidence_ids",
    "outcome", "bound_at",
})


def validate_bundle(value: Any) -> dict[str, Any]:
    bundle = _object(value, "bundle", _BUNDLE_KEYS)
    if bundle["contract"] != BUNDLE_CONTRACT:
        raise ModelError(f"bundle contract must be {BUNDLE_CONTRACT}")
    canonical_uuid(bundle["bundle_id"], "bundle_id")
    canonical_uuid(bundle["initiative_id"], "bundle initiative_id")
    _digest(bundle["aggregate_spec_digest"], "bundle aggregate_spec_digest")
    _digest(bundle["active_plan_digest"], "bundle active_plan_digest")
    if bundle["state"] not in BUNDLE_TRANSITIONS:
        raise ModelError("bundle state is invalid")
    members = _array(bundle["members"], "bundle members", MAX_BUNDLE_MEMBERS)
    if not members:
        raise ModelError("bundle members must not be empty")
    repository_ids: list[str] = []
    for index, value in enumerate(members):
        item = _object(value, f"bundle members[{index}]", _BUNDLE_MEMBER_KEYS)
        for field in ("repository_id", "seal_id", "materialization_id", "review_id", "verification_id"):
            canonical_uuid(item[field], f"bundle member {field}")
        repository_ids.append(item["repository_id"])
        _git_object_id(item["jj_commit_id"], "bundle member jj_commit_id")
        _digest(item["tree_digest"], "bundle member tree_digest")
        _digest(item["diff_digest"], "bundle member diff_digest")
    _unique(repository_ids, "bundle repository IDs")
    _string_list(
        bundle["controller_evidence_ids"], "bundle controller_evidence_ids",
        maximum_items=MAX_EVIDENCE_IDS, maximum_bytes=36, validator=canonical_uuid,
    )
    if bundle["outcome"] not in {None, "compatible", "incompatible", "indeterminate"}:
        raise ModelError("bundle outcome is invalid")
    expected_outcome = {
        "compatible": "compatible",
        "incompatible": "incompatible",
        "indeterminate": "indeterminate",
    }.get(bundle["state"])
    if expected_outcome is None:
        if bundle["outcome"] is not None or bundle["bound_at"] is not None:
            raise ModelError("binding bundle must not fix an outcome or bound_at")
    else:
        if bundle["outcome"] != expected_outcome:
            raise ModelError("terminal bundle state and outcome must agree")
        if bundle["bound_at"] is None:
            raise ModelError("terminal bundle requires bound_at")
        _timestamp(bundle["bound_at"], "bundle bound_at")
    return copy.deepcopy(bundle)


_COORDINATOR_KEYS = frozenset({
    "contract", "initiative_id", "coordinator_id", "generation", "state", "harness",
    "anchor", "protocol_version", "claimed_at", "event_cursor",
    "last_accepted_action_id", "predecessor_coordinator_id", "created_at", "updated_at",
})
_COORDINATOR_ANCHOR_KEYS = frozenset({
    "tmux_socket", "session", "pane_id", "pane_pid", "process_start_identity",
    "server_pid", "server_start_identity",
})
_PANE_ID = re.compile(r"%[0-9]+", re.ASCII)
MAX_PID = 2**22
MAX_PROCESS_IDENTITY_BYTES = 200
MAX_SESSION_NAME_BYTES = 200


def validate_coordinator(value: Any) -> dict[str, Any]:
    """Validate one retained coordinator-generation record.

    The anchor binds the generation to the claiming tmux pane and its process
    identity; every coordinator-actor verb re-verifies it before acting.
    """
    record = _object(value, "coordinator", _COORDINATOR_KEYS)
    if record["contract"] != COORDINATOR_CONTRACT:
        raise ModelError(f"coordinator contract must be {COORDINATOR_CONTRACT}")
    canonical_uuid(record["initiative_id"], "coordinator initiative_id")
    coordinator_id = canonical_uuid(record["coordinator_id"], "coordinator_id")
    _integer(record["generation"], "coordinator generation", minimum=1)
    if record["state"] == "absent" or record["state"] not in COORDINATOR_TRANSITIONS:
        raise ModelError("coordinator state is invalid")
    _token(record["harness"], "coordinator harness")
    anchor = _object(record["anchor"], "coordinator anchor", _COORDINATOR_ANCHOR_KEYS)
    _optional_text(anchor["tmux_socket"], "coordinator anchor tmux_socket", maximum=MAX_PATH_BYTES)
    _text(anchor["session"], "coordinator anchor session", maximum=MAX_SESSION_NAME_BYTES)
    _text(anchor["pane_id"], "coordinator anchor pane_id", maximum=24, pattern=_PANE_ID)
    _integer(anchor["pane_pid"], "coordinator anchor pane_pid", minimum=1, maximum=MAX_PID)
    _text(
        anchor["process_start_identity"], "coordinator anchor process_start_identity",
        maximum=MAX_PROCESS_IDENTITY_BYTES,
    )
    _integer(anchor["server_pid"], "coordinator anchor server_pid", minimum=1, maximum=MAX_PID)
    _text(
        anchor["server_start_identity"], "coordinator anchor server_start_identity",
        maximum=MAX_PROCESS_IDENTITY_BYTES,
    )
    if record["protocol_version"] != COORDINATOR_PROTOCOL_VERSION:
        raise ModelError(f"coordinator protocol_version must be {COORDINATOR_PROTOCOL_VERSION}")
    claimed = _timestamp(record["claimed_at"], "coordinator claimed_at")
    _integer(record["event_cursor"], "coordinator event_cursor", maximum=MAX_EVENT_SEQUENCE)
    if record["last_accepted_action_id"] is not None:
        canonical_uuid(record["last_accepted_action_id"], "coordinator last_accepted_action_id")
    if record["predecessor_coordinator_id"] is not None:
        predecessor = canonical_uuid(
            record["predecessor_coordinator_id"], "coordinator predecessor_coordinator_id"
        )
        if predecessor == coordinator_id:
            raise ModelError("coordinator predecessor_coordinator_id must differ from coordinator_id")
    created = _timestamp(record["created_at"], "coordinator created_at")
    updated = _timestamp(record["updated_at"], "coordinator updated_at")
    if updated < created:
        raise ModelError("coordinator updated_at must not precede created_at")
    if claimed < created:
        raise ModelError("coordinator claimed_at must not precede created_at")
    return copy.deepcopy(record)


_CHECKPOINT_KEYS = frozenset({
    "contract", "initiative_id", "coordinator_id", "generation", "plan_revision",
    "event_cursor", "nodes_under_consideration", "pending_decision", "rationale",
    "prior_checkpoint_digest", "recorded_at", "digest",
})


def _checkpoint_content_digest(record: Mapping[str, Any]) -> str:
    content = {key: value for key, value in record.items() if key != "digest"}
    return hashlib.sha256(_canonical_bytes(content)).hexdigest()


def validate_coordinator_checkpoint(value: Any) -> dict[str, Any]:
    """One bounded coordinator checkpoint; a performance hint, never recovery authority."""
    record = _object(value, "checkpoint", _CHECKPOINT_KEYS)
    if record["contract"] != COORDINATOR_CHECKPOINT_CONTRACT:
        raise ModelError(f"checkpoint contract must be {COORDINATOR_CHECKPOINT_CONTRACT}")
    canonical_uuid(record["initiative_id"], "checkpoint initiative_id")
    canonical_uuid(record["coordinator_id"], "checkpoint coordinator_id")
    _integer(record["generation"], "checkpoint generation", minimum=1)
    if record["plan_revision"] is not None:
        _integer(record["plan_revision"], "checkpoint plan_revision", minimum=1)
    _integer(record["event_cursor"], "checkpoint event_cursor", maximum=MAX_EVENT_SEQUENCE)
    _string_list(
        record["nodes_under_consideration"], "checkpoint nodes_under_consideration",
        maximum_items=MAX_CHECKPOINT_NODES, maximum_bytes=MAX_SLUG_BYTES,
        validator=validate_slug,
    )
    _optional_text(record["pending_decision"], "checkpoint pending_decision", maximum=MAX_CONCERN_BYTES)
    _text(record["rationale"], "checkpoint rationale", maximum=MAX_CHECKPOINT_RATIONALE_BYTES)
    if record["prior_checkpoint_digest"] is not None:
        _digest(record["prior_checkpoint_digest"], "checkpoint prior_checkpoint_digest")
    _timestamp(record["recorded_at"], "checkpoint recorded_at")
    _digest(record["digest"], "checkpoint digest")
    if record["digest"] != _checkpoint_content_digest(record):
        raise ModelError("checkpoint digest does not match canonical checkpoint bytes")
    return copy.deepcopy(record)


def checkpoint_digest(value: Mapping[str, Any]) -> str:
    """Content digest of a checkpoint, excluding its own digest field."""
    return _checkpoint_content_digest(value)


_VALIDATORS: dict[str, Callable[[Any], dict[str, Any]]] = {
    INITIATIVE_CONTRACT: validate_initiative,
    INITIATIVE_CONTRACT_V2: validate_initiative,
    COORDINATOR_CONTRACT: validate_coordinator,
    COORDINATOR_CHECKPOINT_CONTRACT: validate_coordinator_checkpoint,
    PLAN_CONTRACT: validate_plan_record,
    NODE_CONTRACT: validate_node,
    ATTEMPT_CONTRACT: validate_attempt,
    RESULT_PUBLICATION_CONTRACT: validate_result_publication,
    RESULT_INGESTION_CONTRACT: validate_result_ingestion,
    RESULT_CANDIDATE_CONTRACT: validate_result_candidate,
    RESULT_CONTRACT: validate_result,
    SEAL_PREPARATION_CONTRACT: validate_seal_preparation,
    SEAL_CONTRACT: validate_seal,
    REVIEW_CONTRACT: validate_review,
    VERIFICATION_CONTRACT: validate_verification,
    APPROVAL_CONTRACT: validate_approval,
    ACTION_CONTRACT: validate_action,
    EVENT_CONTRACT: validate_event,
    LINK_CONTRACT: validate_link,
    EVIDENCE_CONTRACT: validate_evidence,
    BUNDLE_CONTRACT: validate_bundle,
}


def validate_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ModelError("record must be an object")
    contract = value.get("contract")
    validator = _VALIDATORS.get(contract)
    if validator is None:
        raise ModelError("record has an unsupported or future contract")
    return validator(value)


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def record_digest(record: Any) -> str:
    """Digest canonical compact sorted-key JSON of a validated record."""
    return hashlib.sha256(_canonical_bytes(validate_record(record))).hexdigest()


def plan_digest(plan: Any) -> str:
    """Bind plan content, excluding self-reference and mutable lifecycle status."""
    validated = validate_plan_record(plan)
    without_digest = dict(validated)
    without_digest.pop("digest")
    without_digest.pop("status")
    return hashlib.sha256(_canonical_bytes(without_digest)).hexdigest()


__all__ = [name for name in globals() if name.isupper()] + [
    "ModelError", "new_uuid", "canonical_uuid", "validate_slug",
    "validate_repository_scope", "validate_workspace_scope", "scope_repositories",
    "repository_by_id", "validate_limits", "validate_base_policy",
    "validate_initiative", "validate_plan_record", "validate_plan", "validate_node",
    "validate_attempt", "validate_result_publication", "validate_result_ingestion",
    "validate_result_candidate", "validate_result",
    "validate_seal", "validate_review", "validate_verification", "validate_approval",
    "validate_action", "validate_event", "validate_link", "validate_evidence",
    "validate_bundle", "validate_coordinator", "validate_coordinator_checkpoint",
    "checkpoint_digest", "validate_record", "record_digest",
    "plan_digest", "require_transition",
]
