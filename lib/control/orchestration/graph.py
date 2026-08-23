"""Deterministic validation and dependency readiness for orchestration DAGs."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Mapping

from ..config import HARNESSES
from .config import OrchestrationConfig
from .model import (
    FORBIDDEN_ACTION_CLASSES,
    MUTATING_NODE_TYPES,
    NODE_TYPES,
    SUPPORTED_ROLES,
    WORKFLOWS,
    ModelError,
    plan_digest,
    validate_initiative,
    validate_plan_record,
    validate_seal,
)


RULE_SCHEMA = "O1-P001-schema-bounds"
RULE_UNIQUE_NODE_IDS = "O1-P002-unique-node-ids"
RULE_DEPENDENCY_REFERENCES = "O1-P003-dependency-references"
RULE_ACYCLIC = "O1-P004-acyclic"
RULE_REPOSITORY_MEMBERSHIP = "O1-P005-repository-membership"
RULE_ONE_REPOSITORY = "O1-P006-one-repository-per-mutating-node"
RULE_SUPPORTED_CLAIMS = "O1-P007-supported-claims"
RULE_BASE_POLICY = "O1-P008-immutable-base-policy"
RULE_LINEAGE = "O1-P009-scope-origin-lineage"
RULE_FAILURE_SEAL_BASE = "O1-P010-failure-seal-base"
RULE_ADVISORY_OWNERSHIP = "O1-P011-advisory-path-ownership"
RULE_UPSTREAM_INHERITANCE = "O1-P012-upstream-seal-inheritance"
RULE_COMPOSITION = "O1-P013-explicit-composition"
RULE_TERMINAL_CANDIDATE = "O1-P014-terminal-candidate-uniqueness"
RULE_NESTED_WORKFLOW = "O1-P015-nested-workflow-policy"
RULE_ACCEPTANCE = "O1-P016-terminal-acceptance"
RULE_REQUIRED_GATES = "O1-P017-required-gates"
RULE_LIMITS = "O1-P018-limits"
RULE_FORBIDDEN_ACTIONS = "O1-P019-forbidden-actions"


class PlanError(ValueError):
    """A plan failed one stable deterministic validation rule."""


def _fail(rule: str, detail: str) -> None:
    raise PlanError(f"{rule}: {detail}")


def _node_map(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {node["node_id"]: node for node in nodes}


def _check_cycle(nodes: list[dict[str, Any]], by_id: Mapping[str, dict[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            _fail(RULE_ACYCLIC, f"dependency cycle reaches {node_id}")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in by_id[node_id]["dependencies"]:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node in nodes:
        visit(node["node_id"])


def _mutating_frontier(
    node: dict[str, Any], by_id: Mapping[str, dict[str, Any]]
) -> set[str]:
    """Nearest inherited mutating producers through intervening read-only gates."""
    frontier: set[str] = set()
    seen: set[str] = set()

    def walk(node_id: str) -> None:
        if node_id in seen:
            return
        seen.add(node_id)
        candidate = by_id[node_id]
        if candidate["type"] in MUTATING_NODE_TYPES:
            frontier.add(node_id)
            return
        for dependency in candidate["dependencies"]:
            walk(dependency)

    for dependency in node["dependencies"]:
        walk(dependency)
    return frontier


def _ancestors(node_id: str, by_id: Mapping[str, dict[str, Any]]) -> set[str]:
    result: set[str] = set()

    def walk(current: str) -> None:
        for dependency in by_id[current]["dependencies"]:
            if dependency not in result:
                result.add(dependency)
                walk(dependency)

    walk(node_id)
    return result


def _check_gate_wiring(
    required_reviews: list[str],
    required_verifications: list[str],
    terminal_candidates: Mapping[str, str],
    by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    """One required review gate per member terminal candidate; one verification over all reviews."""
    members = len(terminal_candidates)
    if len(required_reviews) != members or len(required_verifications) != 1:
        scope = "Core plan" if members == 1 else f"workspace plan with {members} members"
        _fail(
            RULE_REQUIRED_GATES,
            f"{scope} requires exactly {members} required review gate(s), one per terminal candidate,"
            " and one verification gate",
        )
    reviewed: dict[str, str] = {}
    for review_node_id in required_reviews:
        targets = [
            candidate for candidate in terminal_candidates.values()
            if candidate in by_id[review_node_id]["dependencies"]
        ]
        if len(targets) != 1:
            _fail(
                RULE_REQUIRED_GATES,
                f"review gate {review_node_id} must depend on exactly one terminal candidate",
            )
        if targets[0] in reviewed:
            _fail(
                RULE_REQUIRED_GATES,
                f"terminal candidate {targets[0]} is reviewed by more than one required gate",
            )
        review_repository = by_id[review_node_id].get("repository_id")
        candidate_repository = by_id[targets[0]]["repository_id"]
        if review_repository is not None and review_repository != candidate_repository:
            _fail(
                RULE_REQUIRED_GATES,
                f"review gate {review_node_id} must bind the member of candidate {targets[0]}",
            )
        if review_repository is None and members > 1:
            _fail(
                RULE_REQUIRED_GATES,
                f"review gate {review_node_id} must name the member of candidate {targets[0]}",
            )
        reviewed[targets[0]] = review_node_id
    missing = [candidate for candidate in terminal_candidates.values() if candidate not in reviewed]
    if missing:
        _fail(RULE_REQUIRED_GATES, f"terminal candidate {missing[0]} has no required review gate")
    verification_node_id = required_verifications[0]
    for review_node_id in required_reviews:
        if review_node_id not in by_id[verification_node_id]["dependencies"]:
            _fail(
                RULE_REQUIRED_GATES,
                f"verification gate {verification_node_id} must depend on review {review_node_id}",
            )


def validate_plan(
    plan: Any,
    *,
    config: OrchestrationConfig,
    initiative: Any,
    seals: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate every proposal plan rule in proposal order and bind its digest.

    P010: A paused-seal continuation base is deferred to Increment 2.
    P014: Core plans must contain a mutating terminal candidate producer; research-only plans are refused.
    """
    try:
        normalized = validate_plan_record(plan)
        initiative_record = validate_initiative(initiative)
    except ModelError as exc:
        _fail(RULE_SCHEMA, str(exc))

    computed_digest = plan_digest(normalized)
    if normalized["digest"] is not None and normalized["digest"] != computed_digest:
        _fail(RULE_SCHEMA, "declared plan digest does not match canonical plan bytes")

    nodes = normalized["nodes"]
    node_ids = [node["node_id"] for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        _fail(RULE_UNIQUE_NODE_IDS, "node IDs must be unique")
    by_id = _node_map(nodes)

    for node in nodes:
        for dependency in node["dependencies"]:
            if dependency not in by_id:
                _fail(
                    RULE_DEPENDENCY_REFERENCES,
                    f"node {node['node_id']} names missing dependency {dependency}",
                )
            if dependency == node["node_id"]:
                _fail(RULE_ACYCLIC, f"node {node['node_id']} depends on itself")
    _check_cycle(nodes, by_id)

    repository_ids = [item["repository_id"] for item in normalized["repositories"]]
    if len(repository_ids) != len(set(repository_ids)):
        _fail(RULE_REPOSITORY_MEMBERSHIP, "repository membership contains duplicates")
    from .model import scope_repositories

    scope_members = scope_repositories(initiative_record)
    if normalized["repositories"] != scope_members:
        _fail(
            RULE_REPOSITORY_MEMBERSHIP,
            "plan repository membership must exactly match the initiative scope",
        )
    if len(scope_members) > 1:
        producers_by_repository: dict[str, int] = {}
        for candidate in nodes:
            if candidate.get("terminal_candidate") is True:
                producers_by_repository[candidate["repository_id"]] = (
                    producers_by_repository.get(candidate["repository_id"], 0) + 1
                )
        for member in scope_members:
            if producers_by_repository.get(member["repository_id"], 0) != 1:
                _fail(
                    RULE_REPOSITORY_MEMBERSHIP,
                    f"workspace plan needs exactly one terminal candidate for repository {member['repository_id']}",
                )
    if normalized["initiative_id"] != initiative_record["initiative_id"]:
        _fail(RULE_REPOSITORY_MEMBERSHIP, "plan belongs to another initiative")
    for node in nodes:
        if node["repository_id"] is not None and node["repository_id"] not in repository_ids:
            _fail(
                RULE_REPOSITORY_MEMBERSHIP,
                f"node {node['node_id']} names an undeclared repository",
            )

    for node in nodes:
        if node["type"] in MUTATING_NODE_TYPES and node["repository_id"] is None:
            _fail(
                RULE_ONE_REPOSITORY,
                f"mutating node {node['node_id']} must name exactly one repository",
            )

    for node in nodes:
        if node["type"] not in NODE_TYPES:
            _fail(RULE_SUPPORTED_CLAIMS, f"unsupported node type on {node['node_id']}")
        if node["role"] not in SUPPORTED_ROLES:
            _fail(RULE_SUPPORTED_CLAIMS, f"unsupported role on {node['node_id']}")
        if node["workflow"] not in WORKFLOWS:
            _fail(RULE_SUPPORTED_CLAIMS, f"unsupported workflow on {node['node_id']}")
        if node["type"] in {"verify", "decision"}:
            if node["harness"] is not None:
                _fail(RULE_SUPPORTED_CLAIMS, f"controller gate {node['node_id']} must not name a harness")
        elif node["harness"] not in HARNESSES:
            _fail(RULE_SUPPORTED_CLAIMS, f"unsupported harness on {node['node_id']}")

    for node in nodes:
        base = node["base"]
        if node["type"] in MUTATING_NODE_TYPES:
            if base is None:
                _fail(RULE_BASE_POLICY, f"mutating node {node['node_id']} lacks an immutable base policy")
            if base["policy"] == "approved-baseline" and (
                base["upstream_node_ids"] or base["seal_inputs"]
            ):
                _fail(RULE_BASE_POLICY, f"baseline node {node['node_id']} names upstream inputs")
            if base["policy"] == "upstream-seal" and not (
                base["upstream_node_ids"] or base["seal_inputs"]
            ):
                _fail(RULE_BASE_POLICY, f"upstream node {node['node_id']} names no immutable input")
            if base["policy"] == "scope-baseline" and base["upstream_node_ids"]:
                _fail(RULE_BASE_POLICY, f"salvage node {node['node_id']} cannot inherit an upstream base")
        elif base is not None:
            _fail(RULE_BASE_POLICY, f"non-mutating node {node['node_id']} must not declare a writer base")

    origins: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if node["type"] not in MUTATING_NODE_TYPES:
            continue
        repository_id = node["repository_id"]
        origin = node["base"]["scope_origin"]
        prior = origins.setdefault(repository_id, origin)
        if origin != prior:
            _fail(RULE_LINEAGE, f"repository {repository_id} has more than one scope origin")
        for item in node["base"]["seal_inputs"]:
            if item["scope_origin"] != origin:
                _fail(RULE_LINEAGE, f"node {node['node_id']} seal input changes scope origin")

    if seals is not None and not isinstance(seals, Mapping):
        _fail(RULE_FAILURE_SEAL_BASE, "seal lookup must be a seal-ID mapping")
    seal_lookup: dict[str, dict[str, Any]] = {}
    for lookup_id, value in ({} if seals is None else seals).items():
        try:
            seal = validate_seal(value)
        except ModelError as exc:
            _fail(RULE_FAILURE_SEAL_BASE, f"invalid seal lookup record: {exc}")
        if lookup_id != seal["seal_id"]:
            _fail(
                RULE_FAILURE_SEAL_BASE,
                f"seal lookup key {lookup_id} does not match its record ID",
            )
        seal_lookup[lookup_id] = seal

    for node in nodes:
        if node["base"] is None:
            continue
        for item in node["base"]["seal_inputs"]:
            seal = seal_lookup.get(item["seal_id"])
            if seal is None:
                _fail(
                    RULE_FAILURE_SEAL_BASE,
                    f"seal input {item['seal_id']} is unknown",
                )
            if seal["initiative_id"] != normalized["initiative_id"]:
                _fail(
                    RULE_FAILURE_SEAL_BASE,
                    f"seal input {item['seal_id']} belongs to another initiative",
                )
            if seal["repository_id"] != node["repository_id"]:
                _fail(
                    RULE_FAILURE_SEAL_BASE,
                    f"seal input {item['seal_id']} belongs to another repository",
                )
            if seal["scope_origin"] != node["base"]["scope_origin"]:
                _fail(
                    RULE_FAILURE_SEAL_BASE,
                    f"seal input {item['seal_id']} changes scope origin",
                )
            if seal["scope_origin"] != item["scope_origin"]:
                _fail(
                    RULE_FAILURE_SEAL_BASE,
                    f"seal input {item['seal_id']} declaration changes scope origin",
                )
            if seal["outcome"] != item["outcome"]:
                _fail(
                    RULE_FAILURE_SEAL_BASE,
                    f"seal input {item['seal_id']} outcome does not match its record",
                )
            if item["outcome"] == "failure" and not (
                item["read_only"] and node["base"]["policy"] == "scope-baseline"
            ):
                _fail(
                    RULE_FAILURE_SEAL_BASE,
                    f"failure seal {item['seal_id']} may be read-only salvage input only",
                )
            if item["outcome"] != "failure" and item["read_only"]:
                _fail(RULE_FAILURE_SEAL_BASE, "only failure seals use read-only salvage input")
            if node["base"]["policy"] == "upstream-seal" and (
                seal["outcome"] != "success" or item["read_only"]
            ):
                _fail(
                    RULE_FAILURE_SEAL_BASE,
                    f"upstream seal input {item['seal_id']} must be mutable success",
                )
            if node["base"]["policy"] == "scope-baseline" and (
                seal["outcome"] != "failure" or not item["read_only"]
            ):
                _fail(
                    RULE_FAILURE_SEAL_BASE,
                    f"scope-baseline input {item['seal_id']} must be read-only failure salvage",
                )

    for node in nodes:
        if node["type"] in MUTATING_NODE_TYPES:
            if not node["advisory_path_ownership"]:
                _fail(RULE_ADVISORY_OWNERSHIP, f"mutating node {node['node_id']} lacks advisory ownership")
            if not node["hard_write_scope"]:
                _fail(RULE_ADVISORY_OWNERSHIP, f"mutating node {node['node_id']} lacks hard write scope")
        elif node["hard_write_scope"]:
            _fail(
                RULE_ADVISORY_OWNERSHIP,
                f"read-only node {node['node_id']} declares hard write scope",
            )

    for node in nodes:
        if node["type"] not in MUTATING_NODE_TYPES:
            continue
        expected = _mutating_frontier(node, by_id)
        declared = set(node["base"]["upstream_node_ids"])
        if expected != declared:
            _fail(
                RULE_UPSTREAM_INHERITANCE,
                f"node {node['node_id']} must inherit exactly {sorted(expected)}",
            )
        if expected and node["base"]["policy"] != "upstream-seal":
            _fail(
                RULE_UPSTREAM_INHERITANCE,
                f"dependent node {node['node_id']} must use upstream-seal policy",
            )
        if (
            not expected
            and node["base"]["policy"] == "upstream-seal"
            and not node["base"]["seal_inputs"]
        ):
            _fail(
                RULE_UPSTREAM_INHERITANCE,
                f"node {node['node_id']} has no exact upstream seal source",
            )

    for node in nodes:
        if node["type"] in MUTATING_NODE_TYPES:
            frontier = _mutating_frontier(node, by_id)
            if len(frontier) > 1 and node["type"] != "compose":
                _fail(
                    RULE_COMPOSITION,
                    f"node {node['node_id']} converges mutating branches without compose",
                )
            if node["type"] == "compose" and len(frontier) < 2:
                _fail(RULE_COMPOSITION, f"compose node {node['node_id']} needs at least two mutating inputs")

    mutating_by_repository: dict[str, list[dict[str, Any]]] = defaultdict(list)
    terminal_candidates: dict[str, str] = {}
    for node in nodes:
        if node["type"] in MUTATING_NODE_TYPES:
            mutating_by_repository[node["repository_id"]].append(node)
    for repository_id in repository_ids:
        candidates = mutating_by_repository.get(repository_id, [])
        terminal = [node for node in candidates if node["terminal_candidate"]]
        if len(terminal) != 1:
            _fail(
                RULE_TERMINAL_CANDIDATE,
                f"repository {repository_id} must declare exactly one terminal candidate producer",
            )
        terminal_node = terminal[0]
        terminal_candidates[repository_id] = terminal_node["node_id"]
        ancestors = _ancestors(terminal_node["node_id"], by_id)
        uncomposed = [
            node["node_id"] for node in candidates
            if node is not terminal_node and node["node_id"] not in ancestors
        ]
        if uncomposed:
            _fail(
                RULE_TERMINAL_CANDIDATE,
                f"terminal candidate omits mutating lineage {sorted(uncomposed)}",
            )
    for node in nodes:
        if node["terminal_candidate"] and node["type"] not in MUTATING_NODE_TYPES:
            _fail(RULE_TERMINAL_CANDIDATE, f"non-mutating node {node['node_id']} cannot produce a candidate")

    policy = normalized["nested_workflow_policy"]
    if policy["workflow"] == "session-loop":
        _fail(RULE_NESTED_WORKFLOW, "session-loop is unsupported in Control workspaces")
    if policy["workflow"] == "none" and policy["single_writer"]:
        _fail(RULE_NESTED_WORKFLOW, "none policy cannot claim a nested writer")
    if policy["workflow"] == "code-orchestrate" and not policy["single_writer"]:
        _fail(RULE_NESTED_WORKFLOW, "code-orchestrate requires a single-writer declaration")
    for node in nodes:
        if node["workflow"] == "session-loop":
            _fail(RULE_NESTED_WORKFLOW, f"node {node['node_id']} requests session-loop")
        if node["workflow"] != "none" and node["workflow"] != policy["workflow"]:
            _fail(RULE_NESTED_WORKFLOW, f"node {node['node_id']} requests an undeclared nested workflow")

    dependent_ids = {dependency for node in nodes for dependency in node["dependencies"]}
    terminal_graph_nodes = [node for node in nodes if node["node_id"] not in dependent_ids]
    for node in terminal_graph_nodes:
        if not node["acceptance"]:
            _fail(RULE_ACCEPTANCE, f"terminal node {node['node_id']} lacks an acceptance condition")
    if not normalized["acceptance_conditions"]:
        _fail(RULE_ACCEPTANCE, "plan has no initiative acceptance conditions")

    required = {(gate["kind"], gate["node_id"]) for gate in normalized["declared_gates"] if gate["required"]}
    required_kinds = {kind for kind, _ in required}
    if not {"review", "verification"}.issubset(required_kinds):
        _fail(RULE_REQUIRED_GATES, "required review and verification gates must both be declared")
    for kind, node_id in required:
        expected_type = "verify" if kind == "verification" else "review"
        if node_id not in by_id or by_id[node_id]["type"] != expected_type:
            _fail(RULE_REQUIRED_GATES, f"declared {kind} gate {node_id} has no matching node")
    required_reviews = sorted(node_id for kind, node_id in required if kind == "review")
    required_verifications = sorted(
        node_id for kind, node_id in required if kind == "verification"
    )
    _check_gate_wiring(required_reviews, required_verifications, terminal_candidates, by_id)

    plan_limits = normalized["limits"]
    initiative_limits = initiative_record["limits"]
    ceilings = {
        "max_parallel": config.max_parallel_tasks,
        "max_total_tasks": config.max_total_tasks,
        "max_attempts_per_node": config.max_attempts_per_node,
        "max_repair_cycles": config.max_repair_cycles,
        "max_retained_bytes_before_pause": config.max_retained_bytes_before_pause,
        "max_retained_inodes_before_pause": config.max_retained_inodes_before_pause,
    }
    for field, ceiling in ceilings.items():
        if plan_limits[field] > ceiling or plan_limits[field] > initiative_limits[field]:
            _fail(RULE_LIMITS, f"plan {field} exceeds its approved ceiling")
    if plan_limits["deadline"] is not None:
        plan_deadline = datetime.fromisoformat(
            plan_limits["deadline"][:-1] + "+00:00"
        )
        initiative_deadline = (
            None
            if initiative_limits["deadline"] is None
            else datetime.fromisoformat(initiative_limits["deadline"][:-1] + "+00:00")
        )
        if initiative_deadline is None or plan_deadline > initiative_deadline:
            _fail(RULE_LIMITS, "plan deadline exceeds its approved ceiling")
    executable = [node for node in nodes if node["type"] not in {"verify", "decision"}]
    if len(nodes) > plan_limits["max_total_tasks"] or len(executable) > plan_limits["max_total_tasks"]:
        _fail(RULE_LIMITS, "graph size exceeds max_total_tasks")
    if plan_limits["max_parallel"] > plan_limits["max_total_tasks"]:
        _fail(RULE_LIMITS, "parallel task limit exceeds total task limit")

    forbidden = set(FORBIDDEN_ACTION_CLASSES)
    requested_forbidden = [item for item in normalized["action_classes"] if item in forbidden]
    if requested_forbidden:
        _fail(RULE_FORBIDDEN_ACTIONS, f"plan requests forbidden actions {requested_forbidden}")
    if tuple(initiative_record["forbidden_action_classes"]) != FORBIDDEN_ACTION_CLASSES:
        _fail(RULE_FORBIDDEN_ACTIONS, "initiative does not preserve the fixed Core denial set")

    normalized["digest"] = computed_digest
    return normalized


def dependency_states(
    plan: Any,
    node_states: Mapping[str, str],
    seals: Mapping[str, Any] | list[Any],
) -> dict[str, str]:
    """Return pure blocked/ready dependency state from exact success seals."""
    try:
        normalized = validate_plan_record(plan)
    except ModelError as exc:
        raise PlanError(f"{RULE_SCHEMA}: {exc}") from exc
    by_id = _node_map(normalized["nodes"])
    if isinstance(seals, Mapping):
        candidates = list(seals.values())
    else:
        candidates = list(seals)
    current_seals: dict[tuple[str, str], dict[str, Any]] = {}
    for value in candidates:
        try:
            seal = validate_seal(value)
        except ModelError as exc:
            raise PlanError(f"{RULE_SCHEMA}: invalid dependency seal: {exc}") from exc
        if seal["outcome"] == "success" and seal["initiative_id"] == normalized["initiative_id"]:
            key = (seal["node_id"], seal["repository_id"])
            current = current_seals.get(key)
            ordering = (
                datetime.fromisoformat(seal["sealed_at"][:-1] + "+00:00"),
                seal["seal_id"],
            )
            if current is None:
                current_seals[key] = seal
            else:
                current_ordering = (
                    datetime.fromisoformat(current["sealed_at"][:-1] + "+00:00"),
                    current["seal_id"],
                )
                if ordering > current_ordering:
                    current_seals[key] = seal

    result: dict[str, str] = {}
    for node in normalized["nodes"]:
        ready = True
        for dependency_id in node["dependencies"]:
            dependency = by_id.get(dependency_id)
            if dependency is None or node_states.get(dependency_id) != "succeeded":
                ready = False
                break
            if dependency["type"] in MUTATING_NODE_TYPES:
                current_seal = current_seals.get(
                    (dependency_id, dependency["repository_id"])
                )
                if current_seal is None:
                    ready = False
                    break
        result[node["node_id"]] = "ready" if ready else "blocked"
    return result


__all__ = [name for name in globals() if name.startswith("RULE_")] + [
    "PlanError", "validate_plan", "dependency_states",
]
