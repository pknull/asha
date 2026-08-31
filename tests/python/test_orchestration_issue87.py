"""Regression coverage for issue #87's stranded orchestration rails."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
import uuid
from unittest import mock

from lib.control.jj import ImmutableTree
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.cli import (
    _approve, _create, _plan, _record_integration_command, approve_plan,
    propose_plan,
)
from lib.control.orchestration.integration import (
    integration_snapshot, record_fallback_integration,
)
from lib.control.orchestration.model import (
    EVIDENCE_CONTRACT, FALLBACK_INTEGRATION_CONTRACT, record_digest,
)
from lib.control.orchestration.scheduler import readiness
from lib.control.orchestration.seals import immutable_tree_diff
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.test_orchestration_graph import seal as graph_seal, valid_plan


class GatePreflightTests(ExecutionFixture, unittest.TestCase):
    def _new_initiative(self, slug: str) -> dict:
        return _create([
            "--repo", str(self.repo), "--slug", slug,
            "--label", slug, "--objective", "Reproduce issue 87.",
        ], self.config, self.store, self.jj)["initiative"]

    def _plan_value(self, initiative: dict, commands: list[dict]) -> dict:
        value = valid_plan()
        value["initiative_id"] = initiative["initiative_id"]
        value["repositories"] = [copy.deepcopy(initiative["scope"]["repository"])]
        repository_id = initiative["scope"]["repository"]["repository_id"]
        value["declared_gates"][1]["commands"] = commands
        for node in value["nodes"]:
            if node["repository_id"] is not None:
                node["repository_id"] = repository_id
        return value

    def _propose(self, initiative: dict, value: dict, name: str) -> dict:
        path = self.root / name
        path.write_text(json.dumps(value))
        plan, _json = _plan([
            initiative["initiative_id"], "--file", str(path),
        ], self.store, self.config, jj=self.jj)
        return plan

    def test_find_skills_shell_gate_is_refused_before_persistence_and_approval(self) -> None:
        initiative = self._new_initiative("find-skills-reproduction")
        impossible = self._plan_value(initiative, [
            {
                "argv": ["sh", "-c", "python3 -m unittest tests.python.test_find_skills"],
                "cwd": ".", "timeout_seconds": 30,
            },
            {
                "argv": ["bash", "tests/validate-plugins.sh"],
                "cwd": ".", "timeout_seconds": 30,
            },
        ])

        with self.assertRaisesRegex(
            ValueError,
            r'command index 0 argv \["sh","-c",.*denied.*: sh',
        ):
            self._propose(initiative, impossible, "find-skills-impossible.json")
        self.assertEqual(
            self.store.list_plans_snapshot(initiative["initiative_id"]), [],
        )
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "draft")

        # Defense at the approval boundary is independent of proposal-time
        # validation, so a retained pre-fix plan still cannot acquire authority.
        with mock.patch(
            "lib.control.orchestration.verification.preflight_verification_gates",
            return_value=[],
        ):
            retained = self._propose(
                initiative, impossible, "find-skills-retained.json",
            )
        before = self.store.peek(initiative["initiative_id"])
        with self.assertRaisesRegex(ValueError, "command index 0.*argv.*sh"):
            _approve([
                initiative["initiative_id"], "--digest", retained["digest"],
            ], self.store)
        self.assertEqual(self.store.peek(initiative["initiative_id"]), before)
        self.assertEqual(
            self.store.list_approvals_snapshot(initiative["initiative_id"]), [],
        )

    def test_direct_script_must_exist_be_executable_and_have_a_usable_shebang(self) -> None:
        initiative = self._new_initiative("direct-script-preflight")
        command = [{
            "argv": ["./checks/run"], "cwd": ".", "timeout_seconds": 30,
        }]
        value = self._plan_value(initiative, command)
        with self.assertRaisesRegex(ValueError, "does not exist at the declared cwd"):
            self._propose(initiative, value, "missing-direct.json")

        script = self.repo / "checks" / "run"
        script.parent.mkdir()
        script.write_text("print('no shebang')\n")
        script.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "no usable shebang"):
            self._propose(initiative, value, "bad-shebang.json")

        script.write_text("#!/usr/bin/env python3\nprint('verified')\n")
        script.chmod(0o755)
        retained = self._propose(initiative, value, "usable-direct.json")
        self.assertEqual(retained["declared_gates"][1]["commands"][0]["argv"], ["./checks/run"])

    def test_activation_repeats_preflight_before_any_runtime_handshake(self) -> None:
        initiative = self._new_initiative("activation-preflight")
        value = self._plan_value(initiative, [{
            "argv": ["bash", "tests/run-tests.sh"],
            "cwd": ".", "timeout_seconds": 30,
        }])
        with mock.patch(
            "lib.control.orchestration.verification.preflight_verification_gates",
            return_value=[],
        ):
            plan = self._propose(initiative, value, "activation-impossible.json")
            approved, _ = _approve([
                initiative["initiative_id"], "--digest", plan["digest"],
            ], self.store)
        document = build_action_document(
            approved["initiative"], "activate-initiative", {},
        )
        action = submit_action(self.store, initiative["initiative_id"], document)
        self.assertEqual(action["state"], "refused")
        self.assertIn("command index 0", action["outcome"])
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "approved")


class InvalidGateRecoveryTests(ExecutionFixture, unittest.TestCase):
    def setUp(self) -> None:
        original = valid_plan

        def invalid_plan() -> dict:
            value = original()
            value["declared_gates"][1]["commands"][0]["argv"] = [
                "sh", "-c", "python3 -m unittest tests.python.test_find_skills",
            ]
            return value

        with mock.patch(
            "tests.python.orchestration_execution_fixtures.valid_plan",
            side_effect=invalid_plan,
        ), mock.patch(
            "lib.control.orchestration.verification.preflight_verification_gates",
            return_value=[],
        ):
            super().setUp()

    def _replacement(self) -> dict:
        replacement = copy.deepcopy(self.plan)
        replacement.pop("digest")
        replacement.pop("revision")
        replacement["declared_gates"][1]["commands"][0]["argv"] = [
            "python3", "-c", "print('verified')",
        ]
        return replacement

    def test_coordinator_revision_requires_operator_and_rebinds_only_uncompleted_nodes(self) -> None:
        original_plan = copy.deepcopy(self.plan)
        original_approval = self.store.read_approval(
            self.initiative_id, self.initiative()["active_plan"]["approval_id"],
        )
        proposed = propose_plan(
            self.store, self.initiative(), self._replacement(),
            config=self.config, jj=self.jj, actor_kind="coordinator",
            actor_id="coordinator:issue87",
        )
        still_running = self.initiative()
        self.assertEqual(still_running["state"], "running")
        self.assertEqual(still_running["active_plan"]["digest"], self.plan["digest"])
        self.assertTrue(all(
            state == "blocked"
            for state in readiness(self.store, still_running).values()
        ))
        self.assertTrue(any(
            event["type"] == "plan-gate-invalid"
            for event in self.store.list_events_snapshot(self.initiative_id)
        ))

        approved = approve_plan(
            self.store, self.initiative(), proposed["digest"], actor_id="cli",
        )
        self.assertEqual(approved["initiative"]["state"], "running")
        self.assertEqual(
            approved["initiative"]["active_plan"]["digest"], proposed["digest"],
        )
        self.assertEqual(self.store.read_plan(self.initiative_id, 1), original_plan)
        self.assertEqual(
            self.store.read_approval(
                self.initiative_id, original_approval["request_id"],
            ),
            original_approval,
        )
        self.assertTrue(all(
            self.store.read_node(self.initiative_id, node_id)["state"]
            in {"ready", "blocked"}
            for node_id in ("implementation-a", "review-a", "verify-a")
        ))
        self.assertTrue(any(
            event["type"] == "plan-gate-superseded"
            and event["payload"]["invalid_plan_digest"] == self.plan["digest"]
            for event in self.store.list_events_snapshot(self.initiative_id)
        ))

        # A known-invalid revision can never be proposed as its own recovery.
        impossible = copy.deepcopy(self.plan)
        impossible.pop("digest")
        impossible.pop("revision")
        with self.assertRaisesRegex(
            ValueError, "known-invalid plan|different proposed revision",
        ):
            propose_plan(
                self.store, still_running, impossible,
                config=self.config, jj=self.jj, actor_kind="coordinator",
                actor_id="coordinator:issue87",
            )

    def test_operator_approval_replay_finishes_after_active_plan_was_rebound(self) -> None:
        proposed = propose_plan(
            self.store, self.initiative(), self._replacement(),
            config=self.config, jj=self.jj, actor_kind="coordinator",
            actor_id="coordinator:issue87",
        )
        original = self.store.save_approval
        interrupted = False

        def fail_consumption(*args, expected_digest=None):
            nonlocal interrupted
            initiative_id = args[-2]
            record = args[-1]
            if record["state"] == "consumed" and not interrupted:
                interrupted = True
                raise OSError("injected approval tail interruption")
            return original(
                initiative_id, record, expected_digest=expected_digest,
            )

        with mock.patch.object(
            self.store, "save_approval", side_effect=fail_consumption,
        ), self.assertRaisesRegex(Exception, "injected"):
            approve_plan(
                self.store, self.initiative(), proposed["digest"], actor_id="cli",
            )
        rebound = self.initiative()
        self.assertEqual(rebound["active_plan"]["digest"], proposed["digest"])
        approval_id = rebound["active_plan"]["approval_id"]
        self.assertEqual(
            self.store.read_approval(self.initiative_id, approval_id)["state"],
            "approved",
        )

        completed = approve_plan(
            self.store, rebound, proposed["digest"], actor_id="cli",
        )
        self.assertEqual(completed["approval"]["state"], "consumed")
        self.assertTrue(any(
            event["type"] == "plan-gate-superseded"
            for event in self.store.list_events_snapshot(self.initiative_id)
        ))

    def test_coordinator_proposal_replays_new_nodes_after_event_interruption(self) -> None:
        replacement = self._replacement()
        replacement["declared_gates"][1]["node_id"] = "verify-recovery"
        replacement["nodes"][2]["node_id"] = "verify-recovery"
        original = self.store.append_event
        interrupted = False

        def fail_proposal_event(initiative_id, event):
            nonlocal interrupted
            if event["type"] == "plan-proposed" and not interrupted:
                interrupted = True
                raise OSError("injected proposal event interruption")
            return original(initiative_id, event)

        with mock.patch.object(
            self.store, "append_event", side_effect=fail_proposal_event,
        ), self.assertRaisesRegex(Exception, "injected"):
            propose_plan(
                self.store, self.initiative(), replacement,
                config=self.config, jj=self.jj, actor_kind="coordinator",
                actor_id="coordinator:issue87",
            )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-recovery")["state"],
            "proposed",
        )

        replayed = propose_plan(
            self.store, self.initiative(), replacement,
            config=self.config, jj=self.jj, actor_kind="coordinator",
            actor_id="coordinator:issue87",
        )
        self.assertEqual(replayed["nodes"][2]["node_id"], "verify-recovery")
        self.assertEqual(len([
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "plan-proposed"
            and event["payload"].get("digest") == replayed["digest"]
        ]), 1)


class FallbackIntegrationTests(ExecutionFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.baseline_tree = ImmutableTree(
            commit_id="b" * 40, digest="c" * 64, entries=(),
        )
        self.candidate_tree = ImmutableTree(
            commit_id="d" * 40, digest="e" * 64,
            entries=(("lib/control/orchestration/file.py", "100644", "1" * 40),),
        )
        self.target_tree = ImmutableTree(
            commit_id="f" * 40, digest="a" * 64,
            entries=(
                ("docs/unrelated.md", "100644", "2" * 40),
                ("lib/control/orchestration/file.py", "100644", "1" * 40),
            ),
        )
        paths, diff_digest = immutable_tree_diff(
            self.baseline_tree, self.candidate_tree,
        )
        evidence_id = str(uuid.uuid4())
        summary = json.dumps({"hard_scope_valid": True}, separators=(",", ":"))
        self.store.save_evidence(self.initiative_id, {
            "contract": EVIDENCE_CONTRACT,
            "evidence_id": evidence_id,
            "initiative_id": self.initiative_id,
            "kind": "seal-evidence",
            "subject_id": str(uuid.uuid4()),
            "digest": hashlib.sha256(summary.encode()).hexdigest(),
            "summary": summary,
            "recorded_at": now_text(),
        })
        self.failure = graph_seal(str(uuid.uuid4()), outcome="failure")
        self.failure.update({
            "initiative_id": self.initiative_id,
            "repository_id": self.initiative()["scope"]["repository"]["repository_id"],
            "node_id": "implementation-a",
            "attempt_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
            "scope_origin": {
                "jj_commit_id": self.baseline_tree.commit_id,
                "tree_digest": self.baseline_tree.digest,
            },
            "base": {
                "kind": "repository-baseline",
                "jj_commit_id": self.baseline_tree.commit_id,
                "tree_digest": self.baseline_tree.digest,
                "seal_ids": [],
            },
            "jj_commit_id": self.candidate_tree.commit_id,
            "tree_digest": self.candidate_tree.digest,
            "diff_digest": diff_digest,
            "cumulative_diff_digest": diff_digest,
            "changed_paths": paths,
            "cumulative_changed_paths": paths,
            "changed_paths_digest": hashlib.sha256(json.dumps(
                paths, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest(),
            "cumulative_changed_paths_digest": hashlib.sha256(json.dumps(
                paths, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest(),
            "result_id": None,
            "process_evidence_id": evidence_id,
            "sealed_at": now_text(),
        })
        self.store.save_seal(self.initiative_id, self.failure)
        self.prior_failure = copy.deepcopy(self.failure)
        self.prior_failure.update({
            "seal_id": str(uuid.uuid4()),
            "attempt_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "run_id": str(uuid.uuid4()),
        })
        self.store.save_seal(self.initiative_id, self.prior_failure)
        current = self.initiative()
        paused = copy.deepcopy(current)
        paused.update({
            "state": "paused",
            "state_revision": current["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(
            paused, expected_digest=record_digest(current),
        )
        self.jj.immutable_tree.side_effect = lambda _root, commit: {
            self.baseline_tree.commit_id: self.baseline_tree,
            self.candidate_tree.commit_id: self.candidate_tree,
            self.target_tree.commit_id: self.target_tree,
        }[commit]
        self.attestation = {
            "contract": FALLBACK_INTEGRATION_CONTRACT,
            "attestation_id": str(uuid.uuid4()),
            "initiative_id": self.initiative_id,
            "repository_id": self.failure["repository_id"],
            "active_plan_digest": self.plan["digest"],
            "baseline": copy.deepcopy(self.failure["scope_origin"]),
            "candidate": {
                "jj_commit_id": self.candidate_tree.commit_id,
                "tree_digest": self.candidate_tree.digest,
                "diff_digest": diff_digest,
                "changed_paths": paths,
            },
            "hard_write_scope": ["lib/control/orchestration"],
            "failure_seal_ids": [
                self.prior_failure["seal_id"], self.failure["seal_id"],
            ],
            "review": {
                "reviewer_id": "independent-reviewer",
                "independent": True,
                "verdict": "pass",
                "evidence_digest": "3" * 64,
                "summary": "Cold review found no findings on the exact candidate.",
                "reviewed_at": now_text(),
            },
            "verification": [{
                "argv": ["python3", "-m", "unittest", "tests.python.test_orchestration_issue87"],
                "cwd": ".", "exit_code": 0, "finished_at": now_text(),
                "output_digest": "4" * 64,
                "summary": "Focused issue #87 regression tests passed.",
            }],
            "integration_target": {
                "repository_id": self.failure["repository_id"],
                "ref": "refs/heads/master",
                "jj_commit_id": self.target_tree.commit_id,
                "tree_digest": self.target_tree.digest,
            },
            "attested_at": now_text(),
        }

    def test_token_countdown_fallback_preserves_failures_and_closes_the_rail(self) -> None:
        seal_before = record_digest(self.failure)
        event = record_fallback_integration(
            self.store, self.initiative_id, self.attestation, jj=self.jj,
        )
        self.assertEqual(event["payload"]["disposition"], "fallback-integrated")
        self.assertEqual(self.initiative()["state"], "integrated")
        retained = self.store.read_seal(self.initiative_id, self.failure["seal_id"])
        self.assertEqual(retained["outcome"], "failure")
        self.assertEqual(record_digest(retained), seal_before)
        self.assertEqual(
            self.store.read_seal(
                self.initiative_id, self.prior_failure["seal_id"],
            )["outcome"],
            "failure",
        )
        evidence = self.store.read_evidence(
            self.initiative_id, self.attestation["attestation_id"],
        )
        self.assertEqual(evidence["kind"], "fallback-integration-attestation")
        self.assertEqual(
            integration_snapshot(self.store, self.initiative_id)
            .facts[self.failure["seal_id"]]["disposition"],
            "fallback-integrated",
        )
        event_count = len(self.store.list_events_snapshot(self.initiative_id))
        replay = record_fallback_integration(
            self.store, self.initiative_id, self.attestation, jj=self.jj,
        )
        self.assertEqual(replay, event)
        self.assertEqual(
            len(self.store.list_events_snapshot(self.initiative_id)), event_count,
        )

    def test_fallback_refuses_target_drift_before_writing_evidence(self) -> None:
        drifted = copy.deepcopy(self.attestation)
        drifted["integration_target"]["tree_digest"] = "9" * 64
        before = self.initiative()
        with self.assertRaisesRegex(ValueError, "integration target tree differs"):
            record_fallback_integration(
                self.store, self.initiative_id, drifted, jj=self.jj,
            )
        self.assertEqual(self.initiative(), before)
        with self.assertRaisesRegex(Exception, "not found"):
            self.store.read_evidence(
                self.initiative_id, drifted["attestation_id"],
            )

    def test_fallback_cli_accepts_only_the_closed_attestation_file(self) -> None:
        path = self.root / "fallback.json"
        path.write_text(json.dumps(self.attestation))
        event, json_output = _record_integration_command([
            self.initiative_id, "--fallback", str(path), "--json",
        ], self.store, self.env, jj=self.jj)
        self.assertTrue(json_output)
        self.assertEqual(event["payload"]["disposition"], "fallback-integrated")
        self.assertEqual(self.initiative()["state"], "integrated")

    def test_fallback_replays_an_exact_orphaned_attestation_evidence_prefix(self) -> None:
        from lib.control.orchestration import actions

        original = actions.append_event
        interrupted = False

        def interrupt_after_evidence(
            store, initiative_id, event_type, subject_ids, payload, *,
            actor_kind, actor_id,
        ):
            nonlocal interrupted
            if event_type == "seal-integration-recorded" and not interrupted:
                interrupted = True
                raise OSError("injected event interruption")
            return original(
                store, initiative_id, event_type, subject_ids, payload,
                actor_kind=actor_kind, actor_id=actor_id,
            )

        with mock.patch.object(
            actions, "append_event", side_effect=interrupt_after_evidence,
        ), self.assertRaisesRegex(Exception, "injected"):
            record_fallback_integration(
                self.store, self.initiative_id, self.attestation, jj=self.jj,
            )
        self.store.read_evidence(
            self.initiative_id, self.attestation["attestation_id"],
        )
        self.assertFalse(any(
            item["type"] == "seal-integration-recorded"
            for item in self.store.list_events_snapshot(self.initiative_id)
        ))

        event = record_fallback_integration(
            self.store, self.initiative_id, self.attestation, jj=self.jj,
        )
        self.assertEqual(event["payload"]["disposition"], "fallback-integrated")
        self.assertEqual(self.initiative()["state"], "integrated")

    def test_fallback_replay_completes_an_interrupted_terminal_transition(self) -> None:
        original = self.store.save_initiative
        failed = False

        def interrupt_transition(record, *, expected_digest=None):
            nonlocal failed
            if record["state"] == "integrated" and not failed:
                failed = True
                raise OSError("injected state transition interruption")
            return original(record, expected_digest=expected_digest)

        with mock.patch.object(
            self.store, "save_initiative", side_effect=interrupt_transition,
        ), self.assertRaisesRegex(Exception, "injected"):
            record_fallback_integration(
                self.store, self.initiative_id, self.attestation, jj=self.jj,
            )
        self.assertEqual(self.initiative()["state"], "paused")
        accepted = [
            item for item in self.store.list_events_snapshot(self.initiative_id)
            if item["type"] == "seal-integration-recorded"
        ]
        self.assertEqual(len(accepted), 1)

        original_tree_reader = self.jj.immutable_tree.side_effect
        drifted_candidate = ImmutableTree(
            commit_id=self.candidate_tree.commit_id,
            digest="8" * 64,
            entries=self.candidate_tree.entries,
        )
        self.jj.immutable_tree.side_effect = lambda root, commit: (
            drifted_candidate
            if commit == self.candidate_tree.commit_id
            else original_tree_reader(root, commit)
        )
        with self.assertRaisesRegex(ValueError, "candidate tree differs"):
            record_fallback_integration(
                self.store, self.initiative_id, self.attestation, jj=self.jj,
            )
        self.assertEqual(self.initiative()["state"], "paused")
        self.jj.immutable_tree.side_effect = original_tree_reader

        replay = record_fallback_integration(
            self.store, self.initiative_id, self.attestation, jj=self.jj,
        )
        self.assertEqual(replay, accepted[0])
        self.assertEqual(self.initiative()["state"], "integrated")
        self.assertEqual(len([
            item for item in self.store.list_events_snapshot(self.initiative_id)
            if item["type"] == "seal-integration-recorded"
        ]), 1)


if __name__ == "__main__":
    unittest.main()
