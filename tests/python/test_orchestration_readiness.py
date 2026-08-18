from __future__ import annotations

import copy
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control.orchestration.actions import (
    SUPPORTED_ACTION_KINDS,
    build_action_document,
    reconcile_actions,
    submit_action,
)
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.readiness import (
    ReadinessError,
    archive_initiative,
    bind_readiness,
    finalize_initiative,
    unarchive_initiative,
)
from lib.control.orchestration.storage import storage_report
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.orchestration_increment3_fixtures import (
    advance_node,
    save_accepted_review,
    save_candidate,
    save_passed_verification,
)


class OrchestrationReadinessTests(ExecutionFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.candidate = save_candidate(
            self,
            outcome=(
                "failure" if "failure_only_partial" in self._testMethodName
                else "success"
            ),
        )
        if (
            "finalize" in self._testMethodName
            or "archive" in self._testMethodName
            or "failure_only_partial" in self._testMethodName
        ):
            advance_node(self, "implementation-a", ["evaluating", "succeeded"])
            advance_node(self, "review-a", ["cancelled"])
            advance_node(self, "verify-a", ["cancelled"])
            return
        advance_node(self, "implementation-a", ["evaluating", "succeeded"])
        advance_node(self, "review-a", ["ready", "evaluating", "succeeded"])
        advance_node(self, "verify-a", ["ready", "evaluating", "succeeded"])
        self.review = save_accepted_review(self, self.candidate)
        self.verification = save_passed_verification(self, self.candidate)

    def test_exact_pass_and_verification_bind_one_member_bundle_and_readiness(self) -> None:
        bundle = bind_readiness(self.store, self.initiative_id)
        self.assertEqual(bundle["outcome"], "compatible")
        self.assertEqual(len(bundle["members"]), 1)
        member = bundle["members"][0]
        self.assertEqual(member["seal_id"], self.candidate["seal_id"])
        self.assertEqual(member["review_id"], self.review["review_id"])
        self.assertEqual(member["verification_id"], self.verification["verification_id"])

    def test_resume_binds_readiness_proven_while_paused(self) -> None:
        paused = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "pause", {}),
        )
        self.assertEqual(paused["state"], "completed")
        self.assertEqual(self.initiative()["state"], "paused")
        with mock.patch(
            "lib.control.orchestration.reconcile.reconcile_live",
            return_value={"conflicts": []},
        ):
            resumed = submit_action(
                self.store, self.initiative_id,
                build_action_document(self.initiative(), "resume", {}),
            )
        self.assertEqual(resumed["state"], "completed", resumed["outcome"])
        self.assertEqual(self.initiative()["state"], "ready-for-integration")
        self.assertEqual(self.initiative()["state"], "ready-for-integration")
        event = self.store.list_events_snapshot(self.initiative_id)[-1]
        self.assertEqual(event["type"], "initiative-state-changed")
        self.assertEqual(event["payload"]["to"], "ready-for-integration")

    def test_review_and_verification_must_name_the_exact_current_seal(self) -> None:
        current = self.store.read_review(self.initiative_id, self.review["review_id"])
        stale = copy.deepcopy(current)
        stale.update({"state": "stale", "verdict": None, "findings": [], "updated_at": now_text()})
        self.store.save_review(
            self.initiative_id, stale, expected_digest=record_digest(current),
        )
        substituted = copy.deepcopy(current)
        substituted["review_id"] = str(uuid.uuid4())
        substituted["target"]["seal_id"] = str(uuid.uuid4())
        substituted.update({"created_at": now_text(), "updated_at": now_text()})
        self.store.save_review(self.initiative_id, substituted)
        with self.assertRaisesRegex(ReadinessError, "exact terminal seal"):
            bind_readiness(self.store, self.initiative_id)

    def test_incomplete_verification_command_evidence_does_not_qualify(self) -> None:
        current = self.store.read_verification(
            self.initiative_id, self.verification["verification_id"],
        )
        stale = copy.deepcopy(current)
        stale.update({"state": "stale", "outcome": None, "updated_at": now_text()})
        self.store.save_verification(
            self.initiative_id, stale, expected_digest=record_digest(current),
        )
        incomplete = copy.deepcopy(current)
        incomplete["verification_id"] = str(uuid.uuid4())
        incomplete["commands"] = []
        incomplete["evidence_ids"] = []
        incomplete.update({"created_at": now_text(), "updated_at": now_text()})
        self.store.save_verification(self.initiative_id, incomplete)
        with self.assertRaisesRegex(ReadinessError, "per-command evidence"):
            bind_readiness(self.store, self.initiative_id)

    def test_finalize_partial_requires_retained_seal_and_reason(self) -> None:
        finalized = finalize_initiative(
            self.store, self.initiative_id, "partial", "Useful candidate lacks gates.",
        )
        self.assertEqual(finalized["state"], "partial")
        self.assertEqual(
            self.store.list_events_snapshot(self.initiative_id)[-1]["payload"]["reason"],
            "Useful candidate lacks gates.",
        )

    def test_finalize_failed_terminal_graph(self) -> None:
        finalized = finalize_initiative(
            self.store, self.initiative_id, "failed", "No candidate can qualify.",
        )
        self.assertEqual(finalized["state"], "failed")

    def test_failure_only_partial_is_refused(self) -> None:
        with self.assertRaisesRegex(ReadinessError, "success seal"):
            finalize_initiative(
                self.store, self.initiative_id, "partial",
                "Failure evidence is not useful retained work.",
            )
        action = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "finalize", {
                "outcome": "partial",
                "reason": "Failure evidence is not useful retained work.",
            }),
        )
        self.assertEqual(action["state"], "refused")
        self.assertIn("success seal", action["outcome"])
        self.assertEqual(self.initiative()["state"], "running")

    def test_archive_retains_inventory_and_unarchive_restores_outcome(self) -> None:
        finalize_initiative(
            self.store, self.initiative_id, "partial", "Retain partial result.",
        )
        archived, inventory = archive_initiative(self.store, self.initiative_id)
        self.assertEqual(archived["state"], "archived")
        self.assertGreater(inventory["records"]["totals"]["inodes"], 0)
        event = self.store.list_events_snapshot(self.initiative_id)[-1]
        self.assertEqual(event["payload"]["to"], "archived")
        self.assertIn("retained_inventory", event["payload"])
        restored = unarchive_initiative(self.store, self.initiative_id)
        self.assertEqual(restored["state"], "partial")

    def test_second_archive_cycle_recovers_its_own_missing_event(self) -> None:
        finalize_initiative(
            self.store, self.initiative_id, "partial", "Retain partial result.",
        )
        first = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "archive", {}),
        )
        self.assertEqual(first["state"], "completed")
        restored = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "unarchive", {}),
        )
        self.assertEqual(restored["state"], "completed")
        second_document = build_action_document(
            self.initiative(), "archive", {},
        )
        with mock.patch(
            "lib.control.orchestration.readiness.append_event",
            side_effect=OSError("injected second archive event failure"),
        ):
            interrupted = submit_action(
                self.store, self.initiative_id, second_document,
            )
        self.assertEqual(interrupted["state"], "indeterminate")
        self.assertEqual(self.initiative()["state"], "archived")

        reconciled = reconcile_actions(self.store, self.initiative_id)

        self.assertEqual(reconciled["actions"][0]["state"], "completed")
        archive_events = [
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "initiative-state-changed"
            and event["payload"].get("to") == "archived"
        ]
        self.assertEqual(len(archive_events), 2)
        self.assertIn(first["action_id"], archive_events[0]["subject_ids"])
        self.assertIn(
            second_document["action_id"], archive_events[1]["subject_ids"],
        )

    def test_ready_retained_inventory_counts_materialization(self) -> None:
        materialization = Path(self.verification["materialization_path"])
        materialization.mkdir(mode=0o700)
        (materialization / "retained.txt").write_text("verification evidence\n")
        bind_readiness(self.store, self.initiative_id)
        live_report = storage_report(self.initiative(), store=self.store)
        archived, inventory = archive_initiative(self.store, self.initiative_id)
        self.assertEqual(archived["state"], "archived")
        self.assertEqual(inventory["materialization_count"], 1)
        self.assertGreater(inventory["workspace_bytes"], 0)
        self.assertEqual(inventory["totals"], live_report["totals"])
        self.assertTrue(materialization.is_dir())

    def test_no_integration_action_exists(self) -> None:
        self.assertNotIn("integrate", SUPPORTED_ACTION_KINDS)
        self.assertNotIn("merge", SUPPORTED_ACTION_KINDS)
        self.assertNotIn("push", SUPPORTED_ACTION_KINDS)


if __name__ == "__main__":
    unittest.main()
