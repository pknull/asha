from __future__ import annotations

import copy
import hashlib
import json
import unittest

from lib.control.orchestration.composition import (
    CompositionError,
    composition_inputs,
    enforce_terminal_candidate,
)
from lib.control.orchestration.scheduler import assignment_bytes
from lib.control.orchestration.seals import _base_binding
from tests.python.test_orchestration_graph import graph_node, seal, valid_plan
from tests.python.test_orchestration_model import INITIATIVE_ID, REPOSITORY_ID, base


class FakeStore:
    def __init__(self, seals, plan=None):
        self.seals = {item["seal_id"]: item for item in seals}
        self.plan = plan

    def read_seal(self, initiative_id, seal_id):
        if initiative_id != INITIATIVE_ID:
            raise AssertionError(initiative_id)
        return copy.deepcopy(self.seals[seal_id])

    def list_seals_snapshot(self, initiative_id):
        return [copy.deepcopy(item) for item in self.seals.values()]

    def peek(self, initiative_id):
        return {"active_plan": {"revision": 1}}

    def read_plan(self, initiative_id, revision):
        return copy.deepcopy(self.plan)

    def list_plans_snapshot(self, initiative_id):
        return [copy.deepcopy(self.plan)]


def compose_binding():
    first = seal("11111111-1111-4111-8111-111111111111")
    second = seal("22222222-2222-4222-8222-222222222222")
    first["node_id"] = "writer-a"
    second["node_id"] = "writer-b"
    first["jj_commit_id"] = "1" * 40
    second["jj_commit_id"] = "2" * 40
    origin = copy.deepcopy(first["scope_origin"])
    node = graph_node("compose-a", "compose", ["writer-a", "writer-b"], terminal=True)
    node["repository_id"] = REPOSITORY_ID
    node["base"] = base("upstream-seal")
    node["base"]["upstream_node_ids"] = ["writer-a", "writer-b"]
    attempt = {
        "base": copy.deepcopy(node["base"]),
        "attempt_id": "33333333-3333-4333-8333-333333333333",
    }
    attempt["base"]["seal_inputs"] = [{
        "seal_id": item["seal_id"], "outcome": "success", "read_only": False,
        "scope_origin": copy.deepcopy(origin),
    } for item in (second, first)]
    return first, second, node, attempt


class OrchestrationCompositionTests(unittest.TestCase):
    def test_ordered_divergent_success_seals_resolve_without_reordering(self) -> None:
        first, second, node, attempt = compose_binding()
        resolved = composition_inputs(
            FakeStore([first, second]), INITIATIVE_ID, node, attempt,
        )
        self.assertEqual(
            [item["seal_id"] for item in resolved],
            [second["seal_id"], first["seal_id"]],
        )
        self.assertNotEqual(resolved[0]["jj_commit_id"], resolved[1]["jj_commit_id"])

    def test_seal_base_binds_exact_order_and_shared_scope_origin(self) -> None:
        first, second, node, attempt = compose_binding()
        store = FakeStore([first, second])
        binding, failures, base_commit = _base_binding(
            store, INITIATIVE_ID, node, attempt,
        )
        self.assertEqual(binding["kind"], "composition-inputs")
        self.assertEqual(
            binding["seal_ids"], [second["seal_id"], first["seal_id"]],
        )
        expected_digest = hashlib.sha256(json.dumps(
            [[item["seal_id"], item["jj_commit_id"], item["tree_digest"]]
             for item in (second, first)],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        self.assertEqual(binding["tree_digest"], expected_digest)
        self.assertEqual(base_commit, attempt["base"]["scope_origin"]["jj_commit_id"])
        self.assertEqual(failures, [])

    def test_scope_origin_conflict_is_refused(self) -> None:
        first, second, node, attempt = compose_binding()
        second["scope_origin"] = {"jj_commit_id": "9" * 40, "tree_digest": "8" * 64}
        with self.assertRaisesRegex(CompositionError, "scope origin"):
            composition_inputs(
                FakeStore([first, second]), INITIATIVE_ID, node, attempt,
            )

    def test_two_distinct_seals_from_one_branch_cannot_omit_an_upstream(self) -> None:
        first, second, node, attempt = compose_binding()
        duplicate_branch = copy.deepcopy(first)
        duplicate_branch["seal_id"] = "33333333-3333-4333-8333-333333333333"
        duplicate_branch["jj_commit_id"] = "3" * 40
        declaration = attempt["base"]["seal_inputs"][0]
        declaration["seal_id"] = duplicate_branch["seal_id"]
        with self.assertRaisesRegex(CompositionError, "every declared upstream"):
            composition_inputs(
                FakeStore([first, second, duplicate_branch]),
                INITIATIVE_ID, node, attempt,
            )

    def test_assignment_declares_conflict_policy_and_workspace_only_merge(self) -> None:
        first, second, node, attempt = compose_binding()
        initiative = {
            "initiative_id": INITIATIVE_ID,
            "slug": "composition-test",
            "objective": "Combine both candidates.",
            "acceptance_criteria": ["Both inputs are present."],
            "forbidden_action_classes": ["integrate"],
            "scope": {"repository": {"root": "/tmp/repo", "repository_id": REPOSITORY_ID}},
        }
        plan = valid_plan()
        plan["nested_workflow_policy"] = {"workflow": "none", "single_writer": False}
        resolved = [{
            **declaration,
            "jj_commit_id": item["jj_commit_id"],
            "tree_digest": item["tree_digest"],
            "diff_digest": item["diff_digest"],
            "base_seal_ids": [], "changed_paths": [],
            "cumulative_changed_paths": [], "result": None,
        } for declaration, item in zip(attempt["base"]["seal_inputs"], (second, first))]
        rendered = assignment_bytes(
            initiative, plan, node, attempt,
            attempt["base"]["scope_origin"]["jj_commit_id"], resolved,
        ).decode()
        self.assertIn("Conflict policy: fail-on-conflict", rendered)
        self.assertIn("Rebase or merge only inside this workspace", rendered)
        composition_section = rendered.split("## Composition contract", 1)[1]
        self.assertLess(
            composition_section.index(second["seal_id"]),
            composition_section.index(first["seal_id"]),
        )

    def test_seal_time_p014_ignores_non_candidate_input_seals(self) -> None:
        first, second, node, _attempt = compose_binding()
        plan = valid_plan()
        plan["nodes"] = [
            graph_node("writer-a", "work", []),
            graph_node("writer-b", "work", []),
            node,
        ]
        for item in plan["nodes"]:
            item["repository_id"] = REPOSITORY_ID
        enforce_terminal_candidate(
            FakeStore([first, second], plan), INITIATIVE_ID, node,
        )


if __name__ == "__main__":
    unittest.main()
