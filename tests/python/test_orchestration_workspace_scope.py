"""Increment 7a: declared-workspace scope identity, create --workspace, and membership drift."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from lib.control.orchestration import cli
from lib.control.orchestration.graph import PlanError, validate_plan
from lib.control.orchestration.model import INITIATIVE_CONTRACT_V2, scope_repositories
from lib.control.orchestration.workspace_scope import (
    ScopeError,
    membership_digest,
    repository_scope,
    verify_scope_identity,
    workspace_scope,
)
from tests.python.orchestration_workspace_fixtures import MEMBERS, WorkspaceFixture, write_manifest


class WorkspaceScopeTests(WorkspaceFixture, unittest.TestCase):
    def test_workspace_scope_binds_ordered_members_with_stable_identity(self) -> None:
        scope = workspace_scope(self.workspace, self.jj)
        self.assertEqual(scope["root"], str(self.workspace))
        self.assertEqual([Path(item["root"]).name for item in scope["repositories"]], list(MEMBERS))
        ids = [item["repository_id"] for item in scope["repositories"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(scope["repositories"][0], repository_scope(self.workspace / "first", self.jj))
        self.assertEqual(
            scope["manifest_membership_digest"],
            membership_digest(scope["repositories"], list(MEMBERS)),
        )
        again = workspace_scope(self.workspace, self.jj)
        self.assertEqual(again, scope)
        self.assertTrue(scope["project_id"].startswith("workspace:"))

    def test_workspace_scope_refuses_missing_manifest_and_unusable_members(self) -> None:
        with self.assertRaisesRegex(ScopeError, "no valid declared workspace"):
            workspace_scope(self.root / "home", self.jj)
        (self.workspace / ".asha/workspace.json").write_text("{not json")
        with self.assertRaisesRegex(ScopeError, "no valid declared workspace|manifest is invalid"):
            workspace_scope(self.workspace, self.jj)
        write_manifest(self.workspace, ("first", "missing"))
        with self.assertRaisesRegex(ScopeError, "member missing is not a usable repository"):
            workspace_scope(self.workspace, self.jj)

    def test_create_workspace_persists_a_v2_initiative(self) -> None:
        initiative = self.create_initiative()
        self.assertEqual(initiative["contract"], INITIATIVE_CONTRACT_V2)
        self.assertEqual(initiative["scope"]["kind"], "workspace")
        self.assertEqual(len(scope_repositories(initiative)), 2)
        stored = self.store.peek(initiative["initiative_id"])
        self.assertEqual(stored["scope"], initiative["scope"])
        with self.assertRaisesRegex(ValueError, "exactly one of --repo or --workspace"):
            cli._create([
                "--repo", str(self.workspace / "first"), "--workspace", str(self.workspace),
                "--slug", "both", "--label", "Both", "--objective", "x",
            ], self.config, self.store, self.jj)
        with self.assertRaisesRegex(ValueError, "exactly one of --repo or --workspace"):
            cli._create(["--slug", "none", "--label", "None", "--objective", "x"], self.config, self.store, self.jj)

    def test_plan_membership_must_match_the_workspace_and_name_one_producer_per_member(self) -> None:
        initiative = self.create_initiative()
        plan = self.two_member_plan(initiative)
        validated = validate_plan(plan, config=self.config, initiative=initiative)
        self.assertEqual(len(validated["repositories"]), 2)
        one_repo = copy.deepcopy(plan)
        one_repo["repositories"] = one_repo["repositories"][:1]
        with self.assertRaisesRegex(PlanError, "membership must exactly match"):
            validate_plan(one_repo, config=self.config, initiative=initiative)
        two_producers = copy.deepcopy(plan)
        two_producers["nodes"][1]["repository_id"] = two_producers["nodes"][0]["repository_id"]
        with self.assertRaisesRegex(PlanError, "exactly one terminal candidate for repository"):
            validate_plan(two_producers, config=self.config, initiative=initiative)
        foreign_review = copy.deepcopy(plan)
        foreign_review["nodes"][3]["repository_id"] = foreign_review["nodes"][0]["repository_id"]
        with self.assertRaisesRegex(PlanError, "must bind the member of candidate impl-second"):
            validate_plan(foreign_review, config=self.config, initiative=initiative)
        unnamed_review = copy.deepcopy(plan)
        unnamed_review["nodes"][3]["repository_id"] = None
        with self.assertRaisesRegex(PlanError, "must name the member of candidate impl-second"):
            validate_plan(unnamed_review, config=self.config, initiative=initiative)
        approved = self.approve_and_run(initiative, plan)
        self.assertEqual(self.initiative()["state"], "running")
        self.assertEqual(approved["revision"], 1)

    def test_scope_identity_verification_detects_membership_drift(self) -> None:
        initiative = self.create_initiative()
        verify_scope_identity(initiative, jj=self.jj)
        write_manifest(self.workspace, ("first",))
        with self.assertRaisesRegex(ScopeError, "membership changed"):
            verify_scope_identity(initiative, jj=self.jj)
        write_manifest(self.workspace, ("second", "first"))
        with self.assertRaisesRegex(ScopeError, "membership changed"):
            verify_scope_identity(initiative, jj=self.jj)
        write_manifest(self.workspace)
        verify_scope_identity(initiative, jj=self.jj)
        # An ancestor manifest cannot stand in for the recorded workspace root.
        (self.workspace / ".asha/workspace.json").unlink()
        write_manifest(self.root, ("ws/first", "ws/second"))
        with self.assertRaisesRegex(ScopeError, "no longer at the recorded root"):
            verify_scope_identity(initiative, jj=self.jj)
        (self.root / ".asha/workspace.json").unlink()
        write_manifest(self.workspace)
        verify_scope_identity(initiative, jj=self.jj)
        config_path = self.workspace / "second" / ".asha/config.json"
        config_path.write_text(json.dumps({"initialized": True, "memory_version": 2, "project_id": "renamed"}) + "\n")
        with self.assertRaisesRegex(ScopeError, "identity digest changed"):
            verify_scope_identity(initiative, jj=self.jj)


if __name__ == "__main__":
    unittest.main()
