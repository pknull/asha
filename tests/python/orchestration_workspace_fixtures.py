"""Shared two-repository declared-workspace fixture for Increment 7 tests."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from lib.control.jj import ImmutableTree, RepositoryFacts
from lib.control.orchestration.cli import _approve, _create, _plan
from lib.control.orchestration.config import load_config
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.review import review_target
from lib.control.orchestration.store import InitiativeStore
from tests.python.orchestration_execution_fixtures import now_text
from tests.python.test_orchestration_graph import graph_node, seal as graph_seal, valid_plan

MEMBERS = ("first", "second")


def write_member(root: Path, project_id: str) -> None:
    (root / ".asha").mkdir(parents=True)
    (root / "Memory").mkdir()
    (root / "Work/session-state").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / ".asha/config.json").write_text(json.dumps({
        "initialized": True, "memory_version": 2, "project_id": project_id,
    }) + "\n")
    (root / "Memory/activeContext.md").write_text(
        "# Objective\n\nO\n\n# State\n\nS\n\n# Next\n\n- N\n\n# Blockers\n\n- None.\n"
    )
    (root / "Memory/decisions.md").write_text("# Decisions\n\n- One.\n")


def write_manifest(workspace: Path, members: tuple[str, ...] = MEMBERS) -> None:
    (workspace / ".asha").mkdir(parents=True, exist_ok=True)
    (workspace / ".asha/workspace.json").write_text(json.dumps({
        "version": 1, "workspace_name": "fixture-workspace",
        "repositories": [{"path": member} for member in members],
    }) + "\n")


class WorkspaceFixture:
    """Temp env, a declared workspace with two member repositories, and a mock jj."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env = {
            "HOME": str(self.root / "home"),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_STATE_HOME": str(self.root / "state"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        for key in ("HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
            Path(self.env[key]).mkdir(mode=0o700)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        write_manifest(self.workspace)
        for member in MEMBERS:
            write_member(self.workspace / member, f"project-{member}")
        self.config = load_config(self.env)
        self.store = InitiativeStore(self.config)
        self.jj = mock.Mock()
        self.jj.preflight.side_effect = lambda root: RepositoryFacts(
            root=Path(root).resolve(), git_root=Path(root).resolve() / ".git",
        )
        self.jj.immutable_tree.return_value = ImmutableTree(
            commit_id="b" * 40, digest="c" * 64, entries=(),
        )

    def create_initiative(self, slug: str = "workspace-test") -> dict:
        return _create([
            "--workspace", str(self.workspace), "--slug", slug,
            "--label", "Workspace test", "--objective", "Change both members.",
            "--acceptance", "Both members verify.",
        ], self.config, self.store, self.jj)["initiative"]

    def two_member_plan(self, initiative: dict) -> dict:
        members = initiative["scope"]["workspace"]["repositories"]
        first, second = members[0]["repository_id"], members[1]["repository_id"]
        plan = valid_plan()
        plan["initiative_id"] = initiative["initiative_id"]
        plan["repositories"] = copy.deepcopy(members)
        plan["limits"] = copy.deepcopy(initiative["limits"])
        impl_first = graph_node("impl-first", "work", [], terminal=True)
        impl_first["repository_id"] = first
        impl_second = graph_node("impl-second", "work", [], terminal=True)
        impl_second["repository_id"] = second
        review_first = graph_node("review-first", "review", ["impl-first"])
        review_first["repository_id"] = first
        review_second = graph_node("review-second", "review", ["impl-second"])
        review_second["repository_id"] = second
        verify = graph_node("verify-a", "verify", ["review-first", "review-second"])
        verify["repository_id"] = first
        plan["nodes"] = [impl_first, impl_second, review_first, review_second, verify]
        plan["declared_gates"] = [
            {"kind": "review", "node_id": "review-first", "required": True},
            {"kind": "review", "node_id": "review-second", "required": True},
            plan["declared_gates"][1],
        ]
        return plan

    def approve_and_run(self, initiative: dict, plan_value: dict) -> dict:
        plan_file = self.root / f"plan-{initiative['slug']}.json"
        plan_file.write_text(json.dumps(plan_value))
        plan, _ = _plan([initiative["initiative_id"], "--file", str(plan_file)], self.store, self.config, jj=self.jj)
        approved, _ = _approve([initiative["initiative_id"], "--digest", plan["digest"]], self.store)
        running = copy.deepcopy(approved["initiative"])
        running.update({
            "state": "running", "state_revision": approved["initiative"]["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(running, expected_digest=record_digest(approved["initiative"]))
        self.initiative_id = initiative["initiative_id"]
        self.plan = plan
        return plan

    def initiative(self) -> dict:
        return self.store.peek(self.initiative_id)

    def save_member_candidate(self, node_id: str, repository_id: str, *, tree_digest: str | None = None) -> dict:
        candidate = graph_seal(str(uuid.uuid4()))
        candidate.update({
            "initiative_id": self.initiative_id,
            "repository_id": repository_id,
            "node_id": node_id,
            "attempt_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
            "result_id": str(uuid.uuid4()),
            "sealed_at": now_text(),
            "outcome": "success",
        })
        if tree_digest is not None:
            candidate["tree_digest"] = tree_digest
        result = {
            "contract": "asha.orchestration-result.v1",
            "publication_id": str(uuid.uuid4()),
            "result_id": candidate["result_id"],
            "payload_digest": "a" * 64,
            "supersedes_result_id": None,
            "initiative_id": self.initiative_id,
            "node_id": node_id,
            "attempt_id": candidate["attempt_id"],
            "task_id": candidate["task_id"],
            "run_id": candidate["run_id"],
            "claim_status": "completed",
            "summary": "Candidate completed.",
            "files_changed": list(candidate["changed_paths"]),
            "verification_attestations": [],
            "concerns": [], "follow_up": [],
            "published_at": now_text(),
        }
        self.store.save_result(self.initiative_id, result)
        self.store.save_seal(self.initiative_id, candidate)
        return candidate

    def save_member_review(self, review_node_id: str) -> dict:
        _seal, target = review_target(
            self.store, self.initiative(), self.plan,
            self.store.read_node(self.initiative_id, review_node_id),
        )
        review = {
            "contract": "asha.orchestration-review.v1",
            "review_id": str(uuid.uuid4()),
            "initiative_id": self.initiative_id,
            "node_id": review_node_id,
            "attempt_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
            "state": "accepted-pass",
            "target": target,
            "verdict": "pass",
            "findings": [],
            "created_at": now_text(),
            "updated_at": now_text(),
        }
        self.store.save_review(self.initiative_id, review)
        return review


def digest_of(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
