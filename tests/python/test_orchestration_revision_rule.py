"""The coordinator's stale-revision rule: behind is accepted, ahead is refused, operators stay exact."""

from __future__ import annotations

import unittest

from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.coordinator import claim
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_orchestration_coordinator_claim import FakeTmux


class RevisionRuleTests(ExecutionFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tmux = FakeTmux()
        self.pane_env = {**self.env, "TMUX_PANE": "%7"}
        self.record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)

    def document(self, action_class: str, payload: dict, *, coordinator: bool, revision_offset: int = 0) -> dict:
        initiative = self.initiative()
        document = build_action_document(
            initiative, action_class, payload,
            actor_id=f"coordinator:{self.record['coordinator_id']}" if coordinator else "cli",
            coordinator=self.record if coordinator else None,
        )
        document["expected_state_revision"] = initiative["state_revision"] + revision_offset
        return document

    def test_coordinator_behind_is_accepted_when_the_bound_records_still_hold(self) -> None:
        result = submit_action(
            self.store, self.initiative_id, self.document("pause", {}, coordinator=True, revision_offset=-3),
        )
        self.assertEqual(result["state"], "completed", result["outcome"])
        self.assertEqual(self.initiative()["state"], "paused")

    def test_coordinator_ahead_is_refused_before_any_effect(self) -> None:
        result = submit_action(
            self.store, self.initiative_id, self.document("pause", {}, coordinator=True, revision_offset=+2),
        )
        self.assertEqual(result["state"], "refused")
        self.assertIn("is ahead of current revision", result["outcome"])
        self.assertEqual(self.initiative()["state"], "running")

    def test_coordinator_behind_is_refused_by_the_class_check_when_the_target_changed(self) -> None:
        # Pause once (target changes), then a stale second pause is idempotent and a stale
        # dispatch of a node that is no longer dispatchable is refused by dispatch's own check.
        first = submit_action(self.store, self.initiative_id, self.document("pause", {}, coordinator=True))
        self.assertEqual(first["state"], "completed", first["outcome"])
        stale_dispatch = self.document(
            "dispatch-node", {"node_id": "implementation-a"}, coordinator=True, revision_offset=-1,
        )
        result = submit_action(self.store, self.initiative_id, stale_dispatch)
        self.assertEqual(result["state"], "refused")
        self.assertIn("initiative must be running to dispatch", result["outcome"])
        self.assertEqual(self.store.list_attempts_snapshot(self.initiative_id), [])

    def test_operator_revision_stays_exact(self) -> None:
        behind = submit_action(
            self.store, self.initiative_id, self.document("pause", {}, coordinator=False, revision_offset=-1),
        )
        self.assertEqual(behind["state"], "refused")
        self.assertIn("does not match current revision", behind["outcome"])
        ahead = submit_action(
            self.store, self.initiative_id, self.document("pause", {}, coordinator=False, revision_offset=+1),
        )
        self.assertEqual(ahead["state"], "refused")
        exact = submit_action(self.store, self.initiative_id, self.document("pause", {}, coordinator=False))
        self.assertEqual(exact["state"], "completed", exact["outcome"])


if __name__ == "__main__":
    unittest.main()
