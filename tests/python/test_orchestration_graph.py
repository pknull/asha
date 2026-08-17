from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from lib.control.orchestration.config import load_config
from lib.control.orchestration.graph import (
    RULE_ACYCLIC,
    RULE_ACCEPTANCE,
    RULE_ADVISORY_OWNERSHIP,
    RULE_BASE_POLICY,
    RULE_COMPOSITION,
    RULE_DEPENDENCY_REFERENCES,
    RULE_FAILURE_SEAL_BASE,
    RULE_FORBIDDEN_ACTIONS,
    RULE_LINEAGE,
    RULE_LIMITS,
    RULE_NESTED_WORKFLOW,
    RULE_REPOSITORY_MEMBERSHIP,
    RULE_REQUIRED_GATES,
    RULE_SUPPORTED_CLAIMS,
    RULE_TERMINAL_CANDIDATE,
    RULE_UNIQUE_NODE_IDS,
    RULE_UPSTREAM_INHERITANCE,
    PlanError,
    dependency_states,
    validate_plan,
)
from lib.control.orchestration.model import PLAN_CONTRACT, SEAL_CONTRACT
from tests.python.test_orchestration_model import (
    DIGEST,
    INITIATIVE_ID,
    NODE_ID,
    REPOSITORY_ID,
    TIMESTAMP,
    base,
    initiative,
    limits,
    node,
    repository,
)


def graph_node(
    node_id: str,
    node_type: str,
    dependencies: list[str],
    *,
    terminal: bool = False,
) -> dict:
    value = node()
    value.update({
        "node_id": node_id,
        "type": node_type,
        "dependencies": dependencies,
        "terminal_candidate": terminal,
    })
    if node_type == "work":
        value["role"] = "implementer"
    elif node_type == "compose":
        value["role"] = "composer"
    elif node_type == "review":
        value.update({
            "role": "reviewer",
            "base": None,
            "advisory_path_ownership": [],
            "hard_write_scope": [],
        })
    elif node_type == "verify":
        value.update({
            "role": "controller",
            "harness": None,
            "base": None,
            "advisory_path_ownership": [],
            "hard_write_scope": [],
        })
    return value


def valid_plan() -> dict:
    return {
        "contract": PLAN_CONTRACT,
        "initiative_id": INITIATIVE_ID,
        "revision": 1,
        "digest": None,
        "status": "proposed",
        "repositories": [repository()],
        "limits": limits(),
        "declared_gates": [
            {"kind": "review", "node_id": "review-a", "required": True},
            {"kind": "verification", "node_id": "verify-a", "required": True},
        ],
        "nested_workflow_policy": {"workflow": "none", "single_writer": False},
        "acceptance_conditions": ["Required review and verification pass."],
        "action_classes": ["task-start", "review", "verification"],
        "nodes": [
            graph_node(NODE_ID, "work", [], terminal=True),
            graph_node("review-a", "review", [NODE_ID]),
            graph_node("verify-a", "verify", ["review-a"]),
        ],
    }


def seal(
    seal_id: str = "44444444-4444-4444-8444-444444444444",
    *,
    outcome: str = "success",
    repository_id: str = REPOSITORY_ID,
    sealed_at: str = TIMESTAMP,
) -> dict:
    return {
        "contract": SEAL_CONTRACT,
        "seal_id": seal_id,
        "initiative_id": INITIATIVE_ID,
        "node_id": NODE_ID,
        "attempt_id": "55555555-5555-4555-8555-555555555555",
        "task_id": "66666666-6666-4666-8666-666666666666",
        "run_id": "77777777-7777-4777-8777-777777777777",
        "outcome": outcome,
        "repository_id": repository_id,
        "scope_origin": {"jj_commit_id": "b" * 40, "tree_digest": "c" * 64},
        "base": {
            "kind": "repository-baseline",
            "jj_commit_id": "b" * 40,
            "tree_digest": "c" * 64,
            "seal_ids": [],
        },
        "read_only_failure_seal_ids": [],
        "jj_commit_id": "d" * 40,
        "tree_digest": "e" * 64,
        "diff_digest": "f" * 64,
        "cumulative_diff_digest": "0" * 64,
        "changed_paths": ["lib/file.py"],
        "cumulative_changed_paths": ["lib/file.py"],
        "result_id": (
            "88888888-8888-4888-8888-888888888888"
            if outcome in {"success", "paused"}
            else None
        ),
        "process_evidence_id": "99999999-9999-4999-8999-999999999999",
        "sealed_at": sealed_at,
    }


def plan_with_external_seal(seal_id: str) -> dict:
    value = valid_plan()
    writer = value["nodes"][0]
    writer["base"] = base("upstream-seal")
    writer["base"]["seal_inputs"] = [{
        "seal_id": seal_id,
        "outcome": "success",
        "read_only": False,
        "scope_origin": copy.deepcopy(writer["base"]["scope_origin"]),
    }]
    return value


class OrchestrationGraphTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        env = {
            "HOME": str(root / "home"),
            "ASHA_CONFIG": str(root / "missing.json"),
            "XDG_STATE_HOME": str(root / "state"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
        for key in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
            Path(env[key]).mkdir(mode=0o700)
        self.config = load_config(env)
        self.initiative = initiative()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_rule(self, rule: str, value: dict, *, seals: dict | None = None) -> None:
        with self.assertRaisesRegex(PlanError, f"^{rule}"):
            validate_plan(
                value,
                config=self.config,
                initiative=self.initiative,
                seals={} if seals is None else seals,
            )

    def test_valid_plan_returns_computed_digest(self) -> None:
        value = validate_plan(valid_plan(), config=self.config, initiative=self.initiative)
        self.assertRegex(value["digest"], r"^[0-9a-f]{64}$")

    def test_missing_dependencies_and_cycles_refuse(self) -> None:
        missing = valid_plan()
        missing["nodes"][1]["dependencies"] = ["missing"]
        self.assert_rule(RULE_DEPENDENCY_REFERENCES, missing)

        cycle = valid_plan()
        cycle["nodes"][0]["dependencies"] = ["verify-a"]
        cycle["nodes"][0]["base"] = base("upstream-seal")
        cycle["nodes"][0]["base"]["upstream_node_ids"] = []
        self.assert_rule(RULE_ACYCLIC, cycle)

        duplicate = valid_plan()
        duplicate["nodes"][1]["node_id"] = NODE_ID
        self.assert_rule(RULE_UNIQUE_NODE_IDS, duplicate)

    def test_repository_scope_lineage_and_forbidden_actions_refuse(self) -> None:
        scope = valid_plan()
        scope["nodes"][0]["repository_id"] = "33333333-3333-4333-8333-333333333333"
        self.assert_rule(RULE_REPOSITORY_MEMBERSHIP, scope)

        for field, changed in (
            ("project_id", "wrong-project"),
            ("root", "/tmp/wrong-repository"),
            ("control_repository_id", "wrong-control-repository"),
            ("initial_identity_digest", "9" * 64),
        ):
            same_id_different_scope = valid_plan()
            same_id_different_scope["repositories"][0][field] = changed
            with self.subTest(repository_field=field):
                self.assert_rule(
                    RULE_REPOSITORY_MEMBERSHIP, same_id_different_scope
                )

        lineage = valid_plan()
        second = graph_node("implementation-b", "work", [], terminal=False)
        second["base"]["scope_origin"]["tree_digest"] = "d" * 64
        lineage["nodes"].insert(1, second)
        self.assert_rule(RULE_LINEAGE, lineage)

        forbidden = valid_plan()
        forbidden["action_classes"].append("merge")
        self.assert_rule(RULE_FORBIDDEN_ACTIONS, forbidden)

    def test_terminal_uniqueness_and_composition_refuse(self) -> None:
        duplicate = valid_plan()
        duplicate["nodes"].insert(
            1, graph_node("implementation-b", "work", [], terminal=True)
        )
        self.assert_rule(RULE_TERMINAL_CANDIDATE, duplicate)

        no_compose = valid_plan()
        second = graph_node("implementation-b", "work", [], terminal=False)
        no_compose["nodes"].insert(1, second)
        terminal = no_compose["nodes"][0]
        terminal["dependencies"] = ["implementation-b"]
        terminal["base"] = base("upstream-seal")
        terminal["base"]["upstream_node_ids"] = ["implementation-b"]
        # Two branches are declared but a work node, not compose, joins them.
        third = graph_node("implementation-c", "work", [], terminal=False)
        no_compose["nodes"].insert(2, third)
        terminal["dependencies"].append("implementation-c")
        terminal["base"]["upstream_node_ids"].append("implementation-c")
        self.assert_rule(RULE_COMPOSITION, no_compose)

    def test_explicit_composition_is_valid(self) -> None:
        value = valid_plan()
        first = graph_node("branch-a", "work", [], terminal=False)
        second = graph_node("branch-b", "work", [], terminal=False)
        compose = graph_node("compose-a", "compose", ["branch-a", "branch-b"], terminal=True)
        compose["base"] = base("upstream-seal")
        compose["base"]["upstream_node_ids"] = ["branch-a", "branch-b"]
        value["nodes"] = [
            first,
            second,
            compose,
            graph_node("review-a", "review", ["compose-a"]),
            graph_node("verify-a", "verify", ["review-a"]),
        ]
        validated = validate_plan(value, config=self.config, initiative=self.initiative)
        self.assertEqual(validated["nodes"][2]["type"], "compose")

    def test_base_failure_scope_and_inheritance_rules_refuse(self) -> None:
        missing_base = valid_plan()
        missing_base["nodes"][0]["base"] = None
        self.assert_rule(RULE_BASE_POLICY, missing_base)

        failure_base = valid_plan()
        failure_base["nodes"][0]["base"] = base("scope-baseline")
        failure_base["nodes"][0]["base"]["seal_inputs"] = [{
            "seal_id": "33333333-3333-4333-8333-333333333333",
            "outcome": "failure",
            "read_only": False,
            "scope_origin": failure_base["nodes"][0]["base"]["scope_origin"],
        }]
        self.assert_rule(RULE_FAILURE_SEAL_BASE, failure_base)

        no_ownership = valid_plan()
        no_ownership["nodes"][0]["advisory_path_ownership"] = []
        self.assert_rule(RULE_ADVISORY_OWNERSHIP, no_ownership)

        inheritance = valid_plan()
        research = graph_node("research-a", "review", [])
        research["type"] = "research"
        research["role"] = "researcher"
        inheritance["nodes"].insert(0, research)
        inheritance["nodes"][1]["dependencies"] = ["research-a"]
        inheritance["nodes"][1]["base"] = base("upstream-seal")
        inheritance["nodes"][1]["base"]["upstream_node_ids"] = ["research-a"]
        self.assert_rule(RULE_UPSTREAM_INHERITANCE, inheritance)

    def test_supported_nested_acceptance_gate_and_limit_rules_refuse(self) -> None:
        unsupported = valid_plan()
        unsupported["nodes"][0]["harness"] = "unknown-harness"
        self.assert_rule(RULE_SUPPORTED_CLAIMS, unsupported)

        nested = valid_plan()
        nested["nested_workflow_policy"] = {
            "workflow": "session-loop",
            "single_writer": True,
        }
        self.assert_rule(RULE_NESTED_WORKFLOW, nested)

        acceptance = valid_plan()
        acceptance["nodes"][-1]["acceptance"] = None
        self.assert_rule(RULE_ACCEPTANCE, acceptance)

        gates = valid_plan()
        gates["declared_gates"] = [gates["declared_gates"][1]]
        self.assert_rule(RULE_REQUIRED_GATES, gates)

        detached_review = valid_plan()
        detached_review["nodes"][1]["dependencies"] = []
        self.assert_rule(RULE_REQUIRED_GATES, detached_review)

        detached_verification = valid_plan()
        detached_verification["nodes"][2]["dependencies"] = [NODE_ID]
        self.assert_rule(RULE_REQUIRED_GATES, detached_verification)

        limits_plan = valid_plan()
        limits_plan["limits"]["max_parallel"] = 4
        self.assert_rule(RULE_LIMITS, limits_plan)

    def test_plan_seal_inputs_resolve_and_match_role_scope_and_repository(self) -> None:
        external = seal()
        value = plan_with_external_seal(external["seal_id"])
        validated = validate_plan(
            value,
            config=self.config,
            initiative=self.initiative,
            seals={external["seal_id"]: external},
        )
        self.assertEqual(validated["nodes"][0]["base"]["policy"], "upstream-seal")

        self.assert_rule(RULE_FAILURE_SEAL_BASE, value, seals={})

        failed = seal(outcome="failure")
        failure_as_upstream = copy.deepcopy(value)
        failure_as_upstream["nodes"][0]["base"]["seal_inputs"][0].update({
            "outcome": "failure",
            "read_only": False,
        })
        self.assert_rule(
            RULE_FAILURE_SEAL_BASE,
            failure_as_upstream,
            seals={failed["seal_id"]: failed},
        )

        wrong_repository = seal(
            repository_id="33333333-3333-4333-8333-333333333333"
        )
        self.assert_rule(
            RULE_FAILURE_SEAL_BASE,
            value,
            seals={wrong_repository["seal_id"]: wrong_repository},
        )

        wrong_origin = seal()
        wrong_origin["scope_origin"]["tree_digest"] = "9" * 64
        self.assert_rule(
            RULE_FAILURE_SEAL_BASE,
            value,
            seals={wrong_origin["seal_id"]: wrong_origin},
        )

    def test_dependency_state_uses_latest_success_seal_and_is_deterministic(self) -> None:
        value = validate_plan(valid_plan(), config=self.config, initiative=self.initiative)
        states = {NODE_ID: "succeeded", "review-a": "blocked", "verify-a": "blocked"}
        self.assertEqual(
            dependency_states(value, states, {}),
            {NODE_ID: "ready", "review-a": "blocked", "verify-a": "blocked"},
        )
        first = seal(sealed_at="2026-08-17T15:00:00Z")
        second = seal(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            sealed_at="2026-08-17T16:00:00Z",
        )
        states["review-a"] = "succeeded"
        seals = {first["seal_id"]: first, second["seal_id"]: second}
        ready = dependency_states(value, states, seals)
        self.assertEqual(ready["review-a"], "ready")
        self.assertEqual(ready["verify-a"], "ready")
        self.assertEqual(ready, dependency_states(value, states, seals))


if __name__ == "__main__":
    unittest.main()
