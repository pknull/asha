from __future__ import annotations

import copy
import json
import os
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control.jj import ImmutableTree, JjError, WorkspaceIdentity
from lib.control.orchestration.actions import (
    _invalidate_candidate_records,
    action_outcome,
    build_action_document,
    submit_action,
)
from lib.control.orchestration.model import RESULT_CONTRACT, record_digest, validate_result
from lib.control.orchestration.ingestion import ingest_result, result_ingestion_id, stage_result
from lib.control.orchestration.review import _tracked_path_fact, complete_review_attempt
from lib.control.orchestration.scheduler import refresh_readiness
from lib.control.orchestration.store import ObservationOnlyPlanError
from lib.control.store import TaskStore
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.orchestration_increment3_fixtures import (
    advance_node,
    save_candidate,
    save_passed_verification,
)


class ReviewJj:
    def __init__(
        self, task: dict, tree_digest: str, *, mutating: bool = False,
        observed_commit_id: str | None = None,
    ):
        self.task = task
        self.tree_digest = tree_digest
        self.mutating = mutating
        self.observed_commit_id = observed_commit_id

    def inspect_workspace(self, path, name, *, snapshot=False, require_empty=True):
        if self.mutating and snapshot and require_empty:
            raise JjError("created workspace working change is not empty")
        return WorkspaceIdentity(
            name=name,
            change_id=self.task["jj"]["change_id"],
            commit_id=(
                self.observed_commit_id or self.task["jj"]["working_commit_id"]
            ),
            parent_commit_ids=(self.task["jj"]["base_commit_id"],),
            description="review",
        )

    def immutable_tree(self, repository, commit_id):
        return ImmutableTree(commit_id, self.tree_digest, ())


class OrchestrationReviewTests(ExecutionFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.candidate = save_candidate(self)
        advance_node(self, "implementation-a", ["evaluating", "succeeded"])
        refresh_readiness(self.store, self.initiative_id)
        self._dispatch_review()

    def _dispatch_review(self) -> None:
        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            workspace = self.config.control.workspace_root / payload["task"]["task_id"]
            payload["task"]["jj"].update({
                "base_commit_id": argv[argv.index("--base") + 1],
                "working_commit_id": "f" * 40,
                "workspace_path": str(workspace),
            })
            payload["workspace"]["path"] = str(workspace)
            self.task = payload["task"]
            return 0, json.dumps(payload).encode(), b""

        action = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "review-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            submitted = submit_action(self.store, self.initiative_id, action)
        self.assertEqual(submitted["state"], "completed")
        self.attempt = max(
            (
                item for item in self.store.list_attempts_snapshot(self.initiative_id)
                if item["node_id"] == "review-a"
            ),
            key=lambda item: item["ordinal"],
        )
        self.review = next(
            item for item in self.store.list_reviews_snapshot(self.initiative_id)
            if item["attempt_id"] == self.attempt["attempt_id"]
        )

    def _publish(self, verdict: str, *, target=None) -> None:
        findings = [] if verdict == "pass" else [{
            "severity": "high", "location": "lib/file.py",
            "summary": "The candidate requires a repair.",
        }]
        result = validate_result({
            "contract": RESULT_CONTRACT,
            "publication_id": str(uuid.uuid4()),
            "result_id": str(uuid.uuid4()),
            "payload_digest": "a" * 64,
            "supersedes_result_id": None,
            "initiative_id": self.initiative_id,
            "node_id": "review-a",
            "attempt_id": self.attempt["attempt_id"],
            "task_id": self.attempt["task_id"],
            "run_id": self.task["runs"][0]["run_id"],
            "claim_status": "completed",
            "summary": "Independent review completed.",
            "files_changed": [],
            "verification_attestations": [],
            "concerns": [], "follow_up": [],
            "published_at": now_text(),
            "review": {
                "verdict": verdict, "findings": findings,
                "target": copy.deepcopy(target or self.review["target"]),
            },
        })
        self.store.save_result(self.initiative_id, result)
        current = self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])
        changed = copy.deepcopy(current)
        changed.update({
            "state": "reported",
            "result_publication_id": result["publication_id"],
            "result_id": result["result_id"],
            "updated_at": now_text(),
        })
        self.store.save_attempt(
            self.initiative_id, changed, expected_digest=record_digest(current),
        )

    def _complete(self, *, mutating=False, observed_commit_id=None):
        observed = {
            "state": "exited", "blocker": None,
            "runs": [{"state": "exited"}],
        }
        with mock.patch(
            "lib.control.orchestration.review.tracked_workspace_status",
            return_value=(
                not mutating,
                ["ignored-artifact"] if not mutating else [],
                False,
            ),
        ):
            return complete_review_attempt(
                self.store, self.initiative_id, self.attempt["attempt_id"],
                self.task, observed,
                jj=ReviewJj(
                    self.task, self.candidate["tree_digest"], mutating=mutating,
                    observed_commit_id=observed_commit_id,
                ),
            )

    def test_pass_verdict_is_bound_to_exact_seal_and_normal_empty_exit(self) -> None:
        self._publish("pass")
        accepted = self._complete()
        self.assertEqual(accepted["state"], "accepted-pass")
        self.assertEqual(accepted["target"]["seal_id"], self.candidate["seal_id"])
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"])["state"],
            "completed-readonly",
        )
        self.assertEqual(self.store.read_node(self.initiative_id, "review-a")["state"], "succeeded")

    def test_review_result_recovers_through_workspace_outbox_ingestion(self) -> None:
        workspace = Path(self.task["jj"]["workspace_path"])
        workspace.mkdir(parents=True)
        self.repo.chmod(0o700)
        current = workspace
        home = Path(self.env["ASHA_HOME"])
        while current != home:
            current.chmod(0o700)
            current = current.parent
        TaskStore(self.config.control).save(self.task)
        ingestion_id = result_ingestion_id(self.attempt["attempt_id"])
        ingestion = self.store.read_result_ingestion(
            self.initiative_id, ingestion_id,
        )
        body = {
            "contract": RESULT_CONTRACT,
            "publication_id": str(uuid.uuid4()),
            "supersedes_result_id": None,
            "initiative_id": self.initiative_id,
            "node_id": "review-a",
            "attempt_id": self.attempt["attempt_id"],
            "task_id": self.task["task_id"],
            "run_id": self.task["runs"][0]["run_id"],
            "claim_status": "completed",
            "summary": "Independent staged review completed.",
            "files_changed": [], "verification_attestations": [],
            "concerns": [], "follow_up": [], "published_at": now_text(),
            "review": {
                "verdict": "pass", "findings": [],
                "target": copy.deepcopy(self.review["target"]),
            },
        }
        managed = {
            **self.env, "ASHA_CONTROL_MANAGED": "1",
            "ASHA_CONTROL_TASK_ID": self.task["task_id"],
            "ASHA_CONTROL_RUN_ID": self.task["runs"][0]["run_id"],
            "ASHA_CONTROL_RESULT_INGESTION_ID": ingestion_id,
            "ASHA_CONTROL_RESULT_OUTBOX": str(
                workspace.joinpath(*ingestion["outbox_path"].split("/"))
            ),
        }
        self.assertEqual(stage_result(self.config, body, managed)["phase"], "staged")
        receipt = ingest_result(
            self.store, self.initiative_id, ingestion_id,
            control_store=TaskStore(self.config.control),
            jj=ReviewJj(self.task, self.candidate["tree_digest"]),
            terminal_reconciliation={"state": "exited"},
        )
        self.assertEqual(receipt["phase"], "completed")
        result = self.store.read_result(self.initiative_id, receipt["result_id"])
        self.assertEqual(result["commit_provenance"]["creator"], "none")
        self.assertEqual(self._complete()["state"], "accepted-pass")

    def test_direct_review_completion_refuses_historical_plan_before_inspection(self) -> None:
        self.install_historical_active_plan()
        before_attempt = self.store.read_attempt(
            self.initiative_id, self.attempt["attempt_id"],
        )
        before_review = self.store.read_review(
            self.initiative_id, self.review["review_id"],
        )
        jj = mock.Mock()

        with self.assertRaises(ObservationOnlyPlanError):
            complete_review_attempt(
                self.store, self.initiative_id, self.attempt["attempt_id"],
                self.task, {"state": "exited", "blocker": None}, jj=jj,
            )

        jj.assert_not_called()
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, self.attempt["attempt_id"]),
            before_attempt,
        )
        self.assertEqual(
            self.store.read_review(self.initiative_id, self.review["review_id"]),
            before_review,
        )

    def test_review_assignment_forbids_workspace_writes_and_snapshotting_commands(self) -> None:
        assignment = (
            self.store.config.initiatives_dir / self.initiative_id / "assignments"
            / f"{self.attempt['attempt_id']}.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Do not write\ninto this workspace", assignment)
        self.assertIn("Do not run `jj status`", assignment)
        self.assertNotIn("Required: run `jj status`", assignment)

    def test_nontracked_addition_is_recorded_without_rejecting_review(self) -> None:
        self._publish("pass")
        accepted = self._complete()
        self.assertEqual(accepted["state"], "accepted-pass")
        evidence = next(
            item for item in self.store.list_evidence_snapshot(self.initiative_id)
            if item["kind"] == "review-workspace"
        )
        self.assertEqual(
            json.loads(evidence["summary"])["non_tracked_paths"],
            ["ignored-artifact"],
        )

    def test_wrong_size_tracked_file_is_rejected_without_reading_bytes(self) -> None:
        root = self.root / "bounded-tracked-reader"
        root.mkdir()
        candidate = root / "tracked.bin"
        with candidate.open("wb") as handle:
            handle.truncate(32 * 1024 * 1024)
        root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            with mock.patch(
                "lib.control.orchestration.review.os.read",
                side_effect=AssertionError("wrong-size tracked bytes were read"),
            ):
                fact = _tracked_path_fact(
                    root_fd, "tracked.bin",
                    {"type": "file", "mode": 0o644, "size": 1, "sha256": "a" * 64},
                )
        finally:
            os.close(root_fd)
        self.assertEqual(fact["type"], "file")
        self.assertEqual(fact["size"], 32 * 1024 * 1024)
        self.assertIsNone(fact["sha256"])

    def test_mutating_reviewer_is_refused_with_failure_evidence(self) -> None:
        self._publish("pass")
        failed = self._complete(mutating=True)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(
            self.store.record_counts_snapshot(self.initiative_id)["evidence"], 1,
        )
        self.assertEqual(self.store.read_node(self.initiative_id, "review-a")["state"], "ready")
        self.assertTrue(any(
            event["payload"].get("reason") == "review-retry"
            for event in self.store.list_events_snapshot(self.initiative_id)
        ))
        failed_target = failed["target"]
        self._dispatch_review()
        self.assertEqual(self.review["target"], failed_target)
        self.assertEqual(self.attempt["ordinal"], 2)

    def test_review_commit_identity_change_is_refused(self) -> None:
        self._publish("pass")
        failed = self._complete(observed_commit_id="e" * 40)
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(
            self.store.read_node(self.initiative_id, "review-a")["state"],
            "ready",
        )

    def test_substituted_target_seal_is_not_accepted(self) -> None:
        target = copy.deepcopy(self.review["target"])
        target["seal_id"] = str(uuid.uuid4())
        self._publish("pass", target=target)
        self.assertEqual(self._complete()["state"], "failed")

    def test_findings_publish_exact_repair_requirement(self) -> None:
        self._publish("findings")
        accepted = self._complete()
        self.assertEqual(accepted["state"], "accepted-findings")
        self.assertEqual(self.store.read_node(self.initiative_id, "review-a")["state"], "needs-input")
        event = self.store.list_events_snapshot(self.initiative_id)[-1]
        self.assertTrue(event["payload"]["repair_required"])
        self.assertEqual(event["payload"]["target_node_id"], "implementation-a")
        self.assertEqual(event["payload"]["target_seal_id"], self.candidate["seal_id"])
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )
        plain = submit_action(
            self.store, self.initiative_id,
            build_action_document(
                self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
            ),
        )
        self.assertEqual(plain["state"], "refused")
        self.assertEqual(
            action_outcome(plain)["reason"],
            f"use repair-node with seal {self.candidate['seal_id']}",
        )
        repair = submit_action(
            self.store, self.initiative_id,
            build_action_document(
                self.initiative(), "repair-node", {
                    "node_id": "implementation-a",
                    "seal_id": self.candidate["seal_id"],
                },
            ),
        )
        self.assertEqual(repair["state"], "completed", repair["outcome"])
        repair_attempt = self.store.read_attempt(
            self.initiative_id, action_outcome(repair)["attempt_id"],
        )
        self.assertEqual(repair_attempt["node_id"], "implementation-a")
        self.assertEqual(
            repair_attempt["base"]["seal_inputs"][0]["seal_id"],
            self.candidate["seal_id"],
        )

    def test_accepted_findings_completion_resumes_after_terminal_record(self) -> None:
        self._publish("findings")
        with mock.patch(
            "lib.control.orchestration.review._finish_accepted_review",
            side_effect=OSError("injected after accepted review record"),
        ):
            with self.assertRaisesRegex(OSError, "after accepted"):
                self._complete()
        retained = self.store.list_reviews_snapshot(self.initiative_id)[0]
        self.assertEqual(retained["state"], "accepted-findings")

        recovered = self._complete()

        self.assertEqual(recovered["state"], "accepted-findings")
        self.assertEqual(
            self.store.read_attempt(
                self.initiative_id, self.attempt["attempt_id"],
            )["state"],
            "completed-readonly",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "review-a")["state"],
            "needs-input",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )

    def test_new_repair_candidate_stales_older_review_and_verification(self) -> None:
        self._publish("pass")
        accepted = self._complete()
        exhausted = copy.deepcopy(self.attempt)
        exhausted.update({
            "attempt_id": str(uuid.uuid4()), "task_id": str(uuid.uuid4()),
            "action_id": str(uuid.uuid4()), "ordinal": 2,
            "state": "completed-readonly",
            "result_publication_id": None, "result_id": None,
            "created_at": now_text(), "updated_at": now_text(),
        })
        self.store.save_attempt(self.initiative_id, exhausted)
        verification = save_passed_verification(self, self.candidate)
        invalidated = _invalidate_candidate_records(
            self.store, self.initiative_id, self.candidate["seal_id"],
        )
        self.assertEqual(invalidated["reviews"], [accepted["review_id"]])
        self.assertEqual(invalidated["verifications"], [verification["verification_id"]])
        self.assertEqual(
            self.store.read_review(self.initiative_id, accepted["review_id"])["state"],
            "stale",
        )
        self.assertEqual(
            self.store.read_verification(
                self.initiative_id, verification["verification_id"]
            )["state"],
            "stale",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "review-a")["state"],
            "ready",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-a")["state"],
            "ready",
        )
        self._dispatch_review()
        self.assertEqual(self.attempt["ordinal"], 3)
        self.assertEqual(self.review["target"]["seal_id"], self.candidate["seal_id"])

    def test_invalidation_resumes_between_node_reopen_and_stale_record(self) -> None:
        self._publish("pass")
        accepted = self._complete()
        verification = save_passed_verification(self, self.candidate)
        with mock.patch(
            "lib.control.orchestration.actions.append_event",
            side_effect=OSError("injected node-ready event failure"),
        ):
            with self.assertRaisesRegex(OSError, "node-ready"):
                _invalidate_candidate_records(
                    self.store, self.initiative_id, self.candidate["seal_id"],
                )
        self.assertEqual(
            self.store.read_review(
                self.initiative_id, accepted["review_id"],
            )["state"],
            "accepted-pass",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "review-a")["state"],
            "ready",
        )

        _invalidate_candidate_records(
            self.store, self.initiative_id, self.candidate["seal_id"],
        )

        self.assertEqual(
            self.store.read_review(
                self.initiative_id, accepted["review_id"],
            )["state"],
            "stale",
        )
        self.assertEqual(
            self.store.read_verification(
                self.initiative_id, verification["verification_id"],
            )["state"],
            "stale",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "verify-a")["state"],
            "ready",
        )


if __name__ == "__main__":
    unittest.main()
