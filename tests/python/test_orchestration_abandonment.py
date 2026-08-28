from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from lib.control.orchestration.actions import (
    ActionError,
    build_action_document,
    reconcile_actions,
    submit_action,
)
from lib.control.orchestration.cli import _create, _plan, _reject
from lib.control.orchestration.model import record_digest
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_orchestration_graph import valid_plan


class OrchestrationAbandonmentTests(ExecutionFixture, unittest.TestCase):
    def create_draft(self, slug: str) -> dict:
        return _create([
            "--repo", str(self.repo), "--slug", slug,
            "--label", "Abandonment test",
            "--objective", "Exercise legal initiative abandonment.",
        ], self.config, self.store, self.jj)["initiative"]

    def reject_only_plan(self, slug: str, reason: str) -> tuple[dict, dict]:
        initiative = self.create_draft(slug)
        value = valid_plan()
        value["initiative_id"] = initiative["initiative_id"]
        value["repositories"] = [copy.deepcopy(initiative["scope"]["repository"])]
        repository_id = initiative["scope"]["repository"]["repository_id"]
        for node in value["nodes"]:
            if node["repository_id"] is not None:
                node["repository_id"] = repository_id
        path = self.root / f"{slug}.json"
        path.write_text(json.dumps(value))
        plan, _ = _plan([
            initiative["initiative_id"], "--file", str(path),
        ], self.store, self.config, jj=self.jj)
        _reject([
            initiative["initiative_id"], "--digest", plan["digest"],
            "--reason", reason,
        ], self.store)
        return self.store.peek(initiative["initiative_id"]), plan

    def submit(self, initiative: dict, action_class: str, payload: dict) -> tuple[dict, dict]:
        document = build_action_document(initiative, action_class, payload)
        return document, submit_action(
            self.store, initiative["initiative_id"], document,
        )

    def test_draft_without_plan_can_finalize_archive_and_unarchive(self) -> None:
        draft = self.create_draft("draft-abandonment")

        finalize_document, finalized = self.submit(
            draft, "finalize", {"outcome": "failed", "reason": "No viable plan."},
        )

        self.assertIsNone(finalize_document["active_plan_digest"])
        self.assertEqual(finalized["state"], "completed", finalized["outcome"])
        self.assertEqual(
            self.store.peek(draft["initiative_id"])["state"], "failed",
        )
        _, archived = self.submit(
            self.store.peek(draft["initiative_id"]), "archive", {},
        )
        self.assertEqual(archived["state"], "completed", archived["outcome"])
        self.assertEqual(
            self.store.peek(draft["initiative_id"])["state"], "archived",
        )
        _, restored = self.submit(
            self.store.peek(draft["initiative_id"]), "unarchive", {},
        )
        self.assertEqual(restored["state"], "completed", restored["outcome"])
        self.assertEqual(
            self.store.peek(draft["initiative_id"])["state"], "failed",
        )

    def test_rejected_only_plan_preserves_rejection_in_terminal_event(self) -> None:
        rejection_reason = "Scope is too broad for this initiative."
        planning, rejected_plan = self.reject_only_plan(
            "rejected-plan-abandonment", rejection_reason,
        )
        self.assertEqual(planning["state"], "planning")
        self.assertIsNone(planning["active_plan"])

        _, finalized = self.submit(
            planning, "finalize",
            {"outcome": "failed", "reason": "No replacement plan will be proposed."},
        )

        self.assertEqual(finalized["state"], "completed", finalized["outcome"])
        terminal = [
            event for event in self.store.list_events_snapshot(planning["initiative_id"])
            if event["type"] == "initiative-state-changed"
            and event["payload"].get("to") == "failed"
        ][-1]
        self.assertEqual(terminal["payload"]["from"], "planning")
        self.assertEqual(
            terminal["payload"]["rejected_plan_digest"], rejected_plan["digest"],
        )
        self.assertEqual(
            terminal["payload"]["plan_rejection_reason"], rejection_reason,
        )
        _, archived = self.submit(
            self.store.peek(planning["initiative_id"]), "archive", {},
        )
        self.assertEqual(archived["state"], "completed", archived["outcome"])

    def test_nonterminal_node_blocks_abandonment_by_identity(self) -> None:
        draft = self.create_draft("live-node-blocker")
        node = copy.deepcopy(self.store.read_node(self.initiative_id, "implementation-a"))
        node.update({
            "node_id": "blocking-node",
            "repository_id": draft["scope"]["repository"]["repository_id"],
            "state": "proposed",
        })
        self.store.save_node(draft["initiative_id"], node)

        _, finalized = self.submit(
            draft, "finalize", {"outcome": "failed", "reason": "Cannot proceed."},
        )

        self.assertEqual(finalized["state"], "refused")
        self.assertEqual(
            json.loads(finalized["outcome"])["reason"],
            "abandonment blocked by non-terminal node blocking-node in state proposed",
        )
        self.assertEqual(self.store.peek(draft["initiative_id"])["state"], "draft")

        with self.assertRaisesRegex(ActionError, "initiative has no active plan"):
            build_action_document(draft, "pause", {})

    def test_success_like_outcome_is_refused_from_draft(self) -> None:
        draft = self.create_draft("success-refusal")

        _, finalized = self.submit(
            draft, "finalize", {"outcome": "success", "reason": "Not legal."},
        )

        self.assertEqual(finalized["state"], "refused")
        self.assertEqual(
            json.loads(finalized["outcome"])["reason"],
            "finalize outcome must be partial or failed",
        )
        self.assertEqual(self.store.peek(draft["initiative_id"])["state"], "draft")

    def test_partial_is_a_legal_abandonment_outcome(self) -> None:
        draft = self.create_draft("partial-abandonment")

        _, finalized = self.submit(
            draft, "finalize",
            {"outcome": "partial", "reason": "Planning retained partial value."},
        )

        self.assertEqual(finalized["state"], "completed", finalized["outcome"])
        self.assertEqual(
            self.store.peek(draft["initiative_id"])["state"], "partial",
        )

    def test_running_finalization_behavior_is_unchanged(self) -> None:
        for node in self.store.list_nodes_snapshot(self.initiative_id):
            changed = copy.deepcopy(node)
            changed["state"] = "cancelled"
            self.store.save_node(
                self.initiative_id, changed, expected_digest=record_digest(node),
            )

        _, finalized = self.submit(
            self.initiative(), "finalize",
            {"outcome": "failed", "reason": "Execution could not complete."},
        )

        self.assertEqual(finalized["state"], "completed", finalized["outcome"])
        event = self.store.list_events_snapshot(self.initiative_id)[-1]
        self.assertEqual(event["payload"]["from"], "running")
        self.assertNotIn("rejected_plan_digest", event["payload"])

    def test_repeated_finalize_and_archive_return_stored_sidecars(self) -> None:
        draft = self.create_draft("idempotent-abandonment")
        finalize_document = build_action_document(
            draft, "finalize", {"outcome": "failed", "reason": "No plan."},
        )
        first_finalize = submit_action(
            self.store, draft["initiative_id"], finalize_document,
        )
        event_count = len(self.store.list_events_snapshot(draft["initiative_id"]))
        second_finalize = submit_action(
            self.store, draft["initiative_id"], finalize_document,
        )
        self.assertEqual(second_finalize, first_finalize)
        self.assertEqual(
            len(self.store.list_events_snapshot(draft["initiative_id"])), event_count,
        )

        archive_document = build_action_document(
            self.store.peek(draft["initiative_id"]), "archive", {},
        )
        first_archive = submit_action(
            self.store, draft["initiative_id"], archive_document,
        )
        event_count = len(self.store.list_events_snapshot(draft["initiative_id"]))
        second_archive = submit_action(
            self.store, draft["initiative_id"], archive_document,
        )
        self.assertEqual(second_archive, first_archive)
        self.assertEqual(
            len(self.store.list_events_snapshot(draft["initiative_id"])), event_count,
        )

    def test_interrupted_abandonment_recovers_original_source_state(self) -> None:
        draft = self.create_draft("recover-abandonment")
        document = build_action_document(
            draft, "finalize", {"outcome": "failed", "reason": "No plan."},
        )
        with mock.patch(
            "lib.control.orchestration.readiness.append_event",
            side_effect=OSError("injected finalization event failure"),
        ):
            interrupted = submit_action(
                self.store, draft["initiative_id"], document,
            )
        self.assertEqual(interrupted["state"], "indeterminate")
        self.assertEqual(
            self.store.peek(draft["initiative_id"])["state"], "failed",
        )

        recovered = reconcile_actions(self.store, draft["initiative_id"])["actions"][0]

        self.assertEqual(recovered["state"], "completed", recovered["outcome"])
        terminal = [
            event for event in self.store.list_events_snapshot(draft["initiative_id"])
            if event["type"] == "initiative-state-changed"
            and event["payload"].get("to") == "failed"
        ][-1]
        self.assertEqual(terminal["payload"]["from"], "draft")
        self.assertIn(document["action_id"], terminal["subject_ids"])


if __name__ == "__main__":
    unittest.main()
