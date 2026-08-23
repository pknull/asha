"""Increment 7b: one terminal seal per member, member reviews, one verification, one bundle."""

from __future__ import annotations

import copy
import unittest

from lib.control.orchestration.model import record_digest
from lib.control.orchestration.readiness import ReadinessError, bind_readiness, prevalidate_finalization
from lib.control.orchestration.verification import (
    VerificationError,
    candidate_bundle_digest,
    terminal_seals,
    verification_members,
)
from tests.python.orchestration_increment3_fixtures import advance_node, save_passed_verification
from tests.python.orchestration_workspace_fixtures import WorkspaceFixture


class MultiRepositoryReadinessTests(WorkspaceFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        created = self.create_initiative()
        self.approve_and_run(created, self.two_member_plan(created))
        members = self.initiative()["scope"]["workspace"]["repositories"]
        self.first_id, self.second_id = members[0]["repository_id"], members[1]["repository_id"]
        for node_id in ("impl-first", "impl-second", "review-first", "review-second", "verify-a"):
            advance_node(self, node_id, ["ready", "dispatching", "running", "evaluating", "succeeded"])

    def full_evidence(self) -> tuple[dict, dict, dict]:
        first = self.save_member_candidate("impl-first", self.first_id)
        second = self.save_member_candidate("impl-second", self.second_id, tree_digest="9" * 64)
        self.save_member_review("review-first")
        self.save_member_review("review-second")
        verification = save_passed_verification(self, [first, second])
        return first, second, verification

    def test_terminal_seals_follow_scope_order_and_the_bundle_digest_binds_both(self) -> None:
        first, second, verification = self.full_evidence()
        ordered = terminal_seals(self.store, self.initiative_id, self.plan, self.initiative())
        self.assertEqual([item["seal_id"] for item in ordered], [first["seal_id"], second["seal_id"]])
        gate = next(item for item in self.plan["declared_gates"] if item["kind"] == "verification")
        both = candidate_bundle_digest(self.initiative(), self.plan, ordered, gate)
        self.assertEqual(verification["bundle_digest"], both)
        self.assertNotEqual(both, candidate_bundle_digest(self.initiative(), self.plan, first, gate))
        members = verification_members(self.store, self.initiative_id, verification["verification_id"])
        self.assertEqual([item["seal_id"] for item in members], [first["seal_id"], second["seal_id"]])
        self.assertEqual(len(verification["commands"]), 2)

    def test_readiness_binds_a_two_member_bundle_once(self) -> None:
        first, second, verification = self.full_evidence()
        bundle = bind_readiness(self.store, self.initiative_id)
        self.assertEqual(bundle["state"], "compatible")
        self.assertEqual([item["repository_id"] for item in bundle["members"]], [self.first_id, self.second_id])
        self.assertEqual([item["seal_id"] for item in bundle["members"]], [first["seal_id"], second["seal_id"]])
        self.assertEqual({item["verification_id"] for item in bundle["members"]}, {verification["verification_id"]})
        self.assertEqual(len({item["review_id"] for item in bundle["members"]}), 2)
        self.assertEqual(self.initiative()["state"], "ready-for-integration")
        again = bind_readiness(self.store, self.initiative_id)
        self.assertEqual(again["bundle_id"], bundle["bundle_id"])
        self.assertEqual(len(self.store.list_bundles_snapshot(self.initiative_id)), 1)
        with self.assertRaisesRegex(ReadinessError, "only a running initiative may be finalized"):
            prevalidate_finalization(self.store, self.initiative_id, "partial", "x")

    def test_finalize_refuses_while_two_member_evidence_qualifies(self) -> None:
        self.full_evidence()
        with self.assertRaisesRegex(ReadinessError, "qualifying exact-seal evidence must bind readiness"):
            prevalidate_finalization(self.store, self.initiative_id, "partial", "x")

    def test_missing_member_seal_or_review_fails_readiness(self) -> None:
        first = self.save_member_candidate("impl-first", self.first_id)
        self.save_member_review("review-first")
        with self.assertRaisesRegex(ReadinessError, "no qualifying success seal"):
            bind_readiness(self.store, self.initiative_id)
        second = self.save_member_candidate("impl-second", self.second_id)
        with self.assertRaisesRegex(ReadinessError, "lacks one accepted-pass review"):
            bind_readiness(self.store, self.initiative_id)
        self.save_member_review("review-second")
        with self.assertRaisesRegex(ReadinessError, "lacks one passed controller verification"):
            bind_readiness(self.store, self.initiative_id)
        save_passed_verification(self, [first, second])
        self.assertEqual(bind_readiness(self.store, self.initiative_id)["state"], "compatible")

    def test_member_drift_after_verification_fails_readiness(self) -> None:
        first, second, _verification = self.full_evidence()
        # A newer success seal for the second member changes the terminal set: the
        # retained verification no longer binds it.
        self.save_member_candidate("impl-second", self.second_id, tree_digest="8" * 64)
        with self.assertRaisesRegex(ReadinessError, "lacks one accepted-pass review|lacks one passed controller verification"):
            bind_readiness(self.store, self.initiative_id)
        self.assertEqual(self.initiative()["state"], "running")
        self.assertEqual(first["seal_id"] != second["seal_id"], True)

    def test_single_member_verification_cannot_pass_a_two_member_set(self) -> None:
        first = self.save_member_candidate("impl-first", self.first_id)
        second = self.save_member_candidate("impl-second", self.second_id)
        self.save_member_review("review-first")
        self.save_member_review("review-second")
        lone = save_passed_verification(self, first)
        with self.assertRaisesRegex(ReadinessError, "lacks one passed controller verification"):
            bind_readiness(self.store, self.initiative_id)
        self.assertEqual(lone["seal_id"], first["seal_id"])
        self.assertNotEqual(second["seal_id"], first["seal_id"])

    def test_terminal_seals_refuse_a_member_without_its_producer(self) -> None:
        self.save_member_candidate("impl-first", self.first_id)
        self.save_member_candidate("impl-second", self.second_id)
        plan = copy.deepcopy(self.plan)
        plan["nodes"] = [node for node in plan["nodes"] if node["node_id"] != "impl-second"]
        with self.assertRaisesRegex(VerificationError, "needs exactly one terminal candidate producer"):
            terminal_seals(self.store, self.initiative_id, plan, self.initiative())

    def test_bundle_members_are_retained_through_archive(self) -> None:
        from lib.control.orchestration.readiness import archive_initiative

        self.full_evidence()
        bundle = bind_readiness(self.store, self.initiative_id)
        before = record_digest(bundle)
        archive_initiative(self.store, self.initiative_id)
        self.assertEqual(self.initiative()["state"], "archived")
        retained = self.store.list_bundles_snapshot(self.initiative_id)
        self.assertEqual(len(retained), 1)
        self.assertEqual(record_digest(retained[0]), before)
        self.assertEqual(len(verification_members(self.store, self.initiative_id, bundle["members"][0]["verification_id"])), 2)


if __name__ == "__main__":
    unittest.main()
