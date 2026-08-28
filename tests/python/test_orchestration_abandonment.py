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
from lib.control.orchestration.cli import _approve, _create, _plan, _reject
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.readiness import append_event as readiness_append_event
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
        initiative, plan = self.propose_plan(slug)
        _reject([
            initiative["initiative_id"], "--digest", plan["digest"],
            "--reason", reason,
        ], self.store)
        return self.store.peek(initiative["initiative_id"]), plan

    def propose_plan(self, slug: str) -> tuple[dict, dict]:
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

    def test_approved_never_activated_cancels_plan_nodes_and_archives(self) -> None:
        awaiting, plan = self.propose_plan("approved-abandonment")
        approved, _ = _approve([
            awaiting["initiative_id"], "--digest", plan["digest"],
        ], self.store)
        initiative = approved["initiative"]
        self.assertEqual(initiative["state"], "approved")
        self.assertEqual(self.store.list_attempts_snapshot(initiative["initiative_id"]), [])

        document, finalized = self.submit(
            initiative, "finalize",
            {"outcome": "failed", "reason": "Coordinator exited before activation."},
        )

        self.assertEqual(document["active_plan_digest"], plan["digest"])
        self.assertEqual(finalized["state"], "completed", finalized["outcome"])
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "failed")
        nodes = self.store.list_nodes_snapshot(initiative["initiative_id"])
        self.assertTrue(nodes)
        self.assertTrue(all(node["state"] == "cancelled" for node in nodes))
        events = self.store.list_events_snapshot(initiative["initiative_id"])
        cancellations = [
            event for event in events
            if event["type"] == "node-state-changed"
            and event["payload"].get("to") == "cancelled"
            and event["actor_kind"] == "controller"
            and event["actor_id"] == "finalization-gate"
        ]
        self.assertEqual(len(cancellations), len(nodes))
        terminal = [
            event for event in events
            if event["type"] == "initiative-state-changed"
            and event["payload"].get("to") == "failed"
        ][-1]
        self.assertEqual(terminal["payload"]["from"], "approved")
        self.assertEqual(
            terminal["payload"]["abandoned_plan_digest"], plan["digest"],
        )

        _, archived = self.submit(
            self.store.peek(initiative["initiative_id"]), "archive", {},
        )
        self.assertEqual(archived["state"], "completed", archived["outcome"])
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "archived")

    def test_awaiting_plan_approval_never_activated_cancels_proposed_nodes(self) -> None:
        awaiting, _ = self.propose_plan("awaiting-approval-abandonment")
        self.assertEqual(awaiting["state"], "awaiting-plan-approval")
        self.assertIsNone(awaiting["active_plan"])
        self.assertEqual(self.store.list_attempts_snapshot(awaiting["initiative_id"]), [])

        _, finalized = self.submit(
            awaiting, "finalize",
            {"outcome": "failed", "reason": "No operator approval will arrive."},
        )

        self.assertEqual(finalized["state"], "completed", finalized["outcome"])
        self.assertEqual(self.store.peek(awaiting["initiative_id"])["state"], "failed")
        self.assertTrue(all(
            node["state"] == "cancelled"
            for node in self.store.list_nodes_snapshot(awaiting["initiative_id"])
        ))
        terminal = [
            event for event in self.store.list_events_snapshot(awaiting["initiative_id"])
            if event["type"] == "initiative-state-changed"
            and event["payload"].get("to") == "failed"
        ][-1]
        self.assertEqual(terminal["payload"]["from"], "awaiting-plan-approval")
        self.assertNotIn("abandoned_plan_digest", terminal["payload"])

    def test_approved_initiative_with_an_attempt_keeps_running_only_refusal(self) -> None:
        def capture(argv, **_kwargs):
            return 0, json.dumps(self.control_payload(argv)).encode(), b""

        dispatch = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            dispatched = submit_action(self.store, self.initiative_id, dispatch)
        self.assertEqual(dispatched["state"], "completed", dispatched["outcome"])
        self.assertTrue(self.store.list_attempts_snapshot(self.initiative_id))

        running = self.initiative()
        approved = copy.deepcopy(running)
        approved.update({
            "state": "approved",
            "state_revision": running["state_revision"] + 1,
        })
        # Activation normally makes this state combination unreachable. Bypass
        # only the transition check to prove the zero-attempt gate remains the
        # independent defense if an attempt is nevertheless retained.
        with mock.patch("lib.control.orchestration.store.require_transition"):
            self.store.save_initiative(
                approved, expected_digest=record_digest(running),
            )
        _, finalized = self.submit(
            approved, "finalize",
            {"outcome": "failed", "reason": "An attempt exists."},
        )

        self.assertEqual(finalized["state"], "refused")
        self.assertEqual(
            json.loads(finalized["outcome"])["reason"],
            "only a running initiative may be finalized",
        )
        self.assertEqual(self.store.peek(self.initiative_id)["state"], "approved")

    def test_approved_ready_node_keeps_precise_abandonment_refusal(self) -> None:
        awaiting, plan = self.propose_plan("approved-ready-node-blocker")
        approved, _ = _approve([
            awaiting["initiative_id"], "--digest", plan["digest"],
        ], self.store)
        initiative = approved["initiative"]
        node = self.store.read_node(initiative["initiative_id"], "implementation-a")
        changed = copy.deepcopy(node)
        changed["state"] = "ready"
        self.store.save_node(
            initiative["initiative_id"], changed,
            expected_digest=record_digest(node),
        )

        _, finalized = self.submit(
            initiative, "finalize",
            {"outcome": "failed", "reason": "A node reached activation state."},
        )

        self.assertEqual(finalized["state"], "refused")
        self.assertEqual(
            json.loads(finalized["outcome"])["reason"],
            "abandonment blocked by non-terminal node implementation-a in state ready",
        )
        self.assertEqual(
            self.store.peek(initiative["initiative_id"])["state"], "approved",
        )

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

    def test_repeated_approved_finalize_does_not_duplicate_node_cancellations(self) -> None:
        awaiting, plan = self.propose_plan("idempotent-approved-abandonment")
        approved, _ = _approve([
            awaiting["initiative_id"], "--digest", plan["digest"],
        ], self.store)
        initiative = approved["initiative"]
        document = build_action_document(
            initiative, "finalize",
            {"outcome": "failed", "reason": "Coordinator did not activate."},
        )

        first = submit_action(self.store, initiative["initiative_id"], document)
        first_events = self.store.list_events_snapshot(initiative["initiative_id"])
        first_cancellations = [
            event for event in first_events
            if event["type"] == "node-state-changed"
            and event["payload"].get("to") == "cancelled"
            and event["actor_id"] == "finalization-gate"
        ]
        second = submit_action(self.store, initiative["initiative_id"], document)
        second_events = self.store.list_events_snapshot(initiative["initiative_id"])
        second_cancellations = [
            event for event in second_events
            if event["type"] == "node-state-changed"
            and event["payload"].get("to") == "cancelled"
            and event["actor_id"] == "finalization-gate"
        ]

        self.assertEqual(second, first)
        self.assertEqual(second_events, first_events)
        self.assertEqual(len(second_cancellations), len(first_cancellations))
        self.assertEqual(
            len(second_cancellations),
            len(self.store.list_nodes_snapshot(initiative["initiative_id"])),
        )

    def test_interrupted_approved_abandonment_recovers_node_events_once(self) -> None:
        awaiting, plan = self.propose_plan("recover-approved-abandonment")
        approved, _ = _approve([
            awaiting["initiative_id"], "--digest", plan["digest"],
        ], self.store)
        initiative = approved["initiative"]
        document = build_action_document(
            initiative, "finalize",
            {"outcome": "failed", "reason": "Coordinator did not activate."},
        )
        calls = 0

        def fail_first_node_event(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected node cancellation event failure")
            return readiness_append_event(*args, **kwargs)

        with mock.patch(
            "lib.control.orchestration.readiness.append_event",
            side_effect=fail_first_node_event,
        ):
            interrupted = submit_action(
                self.store, initiative["initiative_id"], document,
            )
        self.assertEqual(interrupted["state"], "indeterminate")
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "approved")

        recovered = reconcile_actions(
            self.store, initiative["initiative_id"],
        )["actions"][0]

        self.assertEqual(recovered["state"], "completed", recovered["outcome"])
        events = self.store.list_events_snapshot(initiative["initiative_id"])
        cancellations = [
            event for event in events
            if event["type"] == "node-state-changed"
            and event["payload"].get("to") == "cancelled"
            and event["actor_id"] == "finalization-gate"
        ]
        self.assertEqual(
            len(cancellations),
            len(self.store.list_nodes_snapshot(initiative["initiative_id"])),
        )
        self.assertEqual(
            len({event["subject_ids"][0] for event in cancellations}),
            len(cancellations),
        )

    def test_interrupted_approved_terminal_event_recovers_source_and_digest(self) -> None:
        awaiting, plan = self.propose_plan("recover-approved-terminal-event")
        approved, _ = _approve([
            awaiting["initiative_id"], "--digest", plan["digest"],
        ], self.store)
        initiative = approved["initiative"]
        document = build_action_document(
            initiative, "finalize",
            {"outcome": "failed", "reason": "Coordinator did not activate."},
        )

        def fail_terminal_event(store, initiative_id, event_type, *args, **kwargs):
            if event_type == "initiative-state-changed":
                raise OSError("injected terminal event failure")
            return readiness_append_event(
                store, initiative_id, event_type, *args, **kwargs,
            )

        with mock.patch(
            "lib.control.orchestration.readiness.append_event",
            side_effect=fail_terminal_event,
        ):
            interrupted = submit_action(
                self.store, initiative["initiative_id"], document,
            )
        self.assertEqual(interrupted["state"], "indeterminate")
        self.assertEqual(self.store.peek(initiative["initiative_id"])["state"], "failed")
        cancellation_count = sum(
            event["type"] == "node-state-changed"
            and event["payload"].get("to") == "cancelled"
            and event["actor_id"] == "finalization-gate"
            for event in self.store.list_events_snapshot(initiative["initiative_id"])
        )

        recovered = reconcile_actions(
            self.store, initiative["initiative_id"],
        )["actions"][0]

        self.assertEqual(recovered["state"], "completed", recovered["outcome"])
        events = self.store.list_events_snapshot(initiative["initiative_id"])
        self.assertEqual(
            sum(
                event["type"] == "node-state-changed"
                and event["payload"].get("to") == "cancelled"
                and event["actor_id"] == "finalization-gate"
                for event in events
            ),
            cancellation_count,
        )
        terminal = [
            event for event in events
            if event["type"] == "initiative-state-changed"
            and event["payload"].get("to") == "failed"
        ][-1]
        self.assertEqual(terminal["payload"]["from"], "approved")
        self.assertEqual(
            terminal["payload"]["abandoned_plan_digest"], plan["digest"],
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
