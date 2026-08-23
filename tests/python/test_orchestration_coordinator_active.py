"""Increment 5: the bounded active coordinator — dispatch, pause, requests, checkpoints."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from lib.control.orchestration import cli
from lib.control.orchestration.actions import (
    COORDINATOR_ACTION_KINDS,
    build_action_document,
    submit_action,
)
from lib.control.orchestration.coordinator import CoordinatorError, claim
from lib.control.orchestration.model import checkpoint_digest
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_orchestration_coordinator_claim import FakeTmux


class ActiveCoordinatorTests(ExecutionFixture, unittest.TestCase):
    """Running initiative; Asha's pane holds generation 1."""

    def setUp(self) -> None:
        super().setUp()
        self.tmux = FakeTmux()
        self.pane_env = {**self.env, "TMUX_PANE": "%7"}
        self.record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)

    def events(self) -> list[dict]:
        return self.store.list_events_snapshot(self.initiative_id)

    def fake_capture(self, calls: list[list[str]], tasks: list[dict]):
        def run(argv, **_kwargs):
            calls.append(list(argv))
            payload = self.control_payload(argv, existing=len(calls) > 1)
            tasks.append(payload["task"])
            return 0, json.dumps(payload, sort_keys=True).encode(), b""

        return run

    def coordinator_document(self, action_class: str, payload: dict) -> dict:
        return build_action_document(
            self.initiative(), action_class, payload,
            actor_id=f"coordinator:{self.record['coordinator_id']}", coordinator=self.record,
        )

    def test_coordinator_dispatch_creates_the_task_and_links_its_generation(self) -> None:
        calls: list[list[str]] = []
        tasks: list[dict] = []
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=self.fake_capture(calls, tasks),
        ):
            result, _ = cli._operator_action(
                "dispatch", [self.initiative_id, "--node", "implementation-a", "--as-coordinator", "--json"],
                self.store, self.pane_env, self.tmux,
            )
        self.assertEqual(result["state"], "completed", result["outcome"])
        self.assertEqual(result["actor_kind"], "coordinator")
        self.assertEqual(result["coordinator_generation"], 1)
        self.assertEqual(len(calls), 1)
        links = self.store.list_links_snapshot(self.initiative_id)
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["actor_kind"], "coordinator")
        self.assertEqual(links[0]["coordinator_generation"], 1)
        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        self.assertEqual(attempt["state"], "running")
        # A directive against the live attempt is accepted as pending, never delivered.
        directive = self.coordinator_document(
            "directive", {"node_id": "implementation-a", "attempt_id": attempt["attempt_id"], "text": "publish early"},
        )
        accepted = submit_action(self.store, self.initiative_id, directive)
        self.assertEqual(accepted["state"], "completed", accepted["outcome"])
        self.assertIn('"delivery":"pending"', accepted["outcome"])
        self.assertEqual(self.events()[-1]["type"], "directive-accepted")
        self.assertEqual(self.events()[-1]["payload"]["delivery"], "pending")
        self.assertNotIn("publish early", json.dumps(self.events()[-1]))
        # stop-attempt is coordinator-callable; the fake Control has no stop route, so it refuses.
        stop = self.coordinator_document("stop-attempt", {"attempt_id": attempt["attempt_id"]})
        with mock.patch(
            "lib.control.orchestration.actions.capture_bytes",
            return_value=(1, b"", b"fake control cannot stop"),
        ):
            stopped = submit_action(self.store, self.initiative_id, stop)
        self.assertEqual(stopped["state"], "refused")
        self.assertIn("stop", stopped["outcome"].lower())
        self.assertEqual(stopped["actor_kind"], "coordinator")

    def test_wrong_pane_cannot_dispatch_as_coordinator(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "not inside the coordinator's anchor pane"):
            cli._operator_action(
                "dispatch", [self.initiative_id, "--node", "implementation-a", "--as-coordinator"],
                self.store, {**self.env, "TMUX_PANE": "%9"}, self.tmux,
            )
        self.assertEqual(self.store.list_actions_snapshot(self.initiative_id), [])

    def test_pause_request_decision_and_operator_resume(self) -> None:
        paused, _ = cli._operator_action(
            "pause", [self.initiative_id, "--as-coordinator"], self.store, self.pane_env, self.tmux,
        )
        self.assertEqual(paused["state"], "completed", paused["outcome"])
        self.assertEqual(self.initiative()["state"], "paused")
        # Resume stays operator-only.
        resume = self.coordinator_document("resume", {})
        refused = submit_action(self.store, self.initiative_id, resume)
        self.assertEqual(refused["state"], "refused")
        self.assertIn("not available to the coordinator actor", refused["outcome"])
        resumed, _ = cli._operator_action("resume", [self.initiative_id], self.store, {**self.env}, None)
        self.assertEqual(resumed["state"], "completed", resumed["outcome"])
        self.assertEqual(self.initiative()["state"], "running")
        # Escalate to the operator: running -> needs-input with the question on the event.
        asked = submit_action(
            self.store, self.initiative_id,
            self.coordinator_document(
                "request-decision", {"subject_id": "implementation-a", "question": "Proceed without tests?"},
            ),
        )
        self.assertEqual(asked["state"], "completed", asked["outcome"])
        self.assertEqual(self.initiative()["state"], "needs-input")
        types = [event["type"] for event in self.events()[-2:]]
        self.assertEqual(types, ["approval-requested", "initiative-state-changed"])
        requested = self.events()[-2]
        self.assertEqual(requested["actor_kind"], "coordinator")
        self.assertEqual(requested["payload"]["question"], "Proceed without tests?")
        self.assertEqual(requested["payload"]["kind"], "operator-decision")
        # The operator answers in conversation and resumes from needs-input.
        resumed, _ = cli._operator_action("resume", [self.initiative_id], self.store, {**self.env}, None)
        self.assertEqual(resumed["state"], "completed", resumed["outcome"])
        self.assertEqual(self.initiative()["state"], "running")
        self.assertEqual(self.events()[-1]["payload"], {"from": "needs-input", "to": "running"})

    def test_propose_outcome_records_a_request_and_changes_no_state(self) -> None:
        before = self.initiative()["state"]
        proposed = submit_action(
            self.store, self.initiative_id,
            self.coordinator_document("propose-outcome", {"outcome": "partial", "reason": "one node blocked"}),
        )
        self.assertEqual(proposed["state"], "completed", proposed["outcome"])
        self.assertEqual(self.initiative()["state"], before)
        self.assertEqual(self.events()[-1]["type"], "approval-requested")
        self.assertEqual(self.events()[-1]["payload"]["kind"], "outcome-proposal")
        bad = submit_action(
            self.store, self.initiative_id,
            self.coordinator_document("propose-outcome", {"outcome": "ready-for-integration", "reason": "x"}),
        )
        self.assertEqual(bad["state"], "refused")
        self.assertIn("partial or failed", bad["outcome"])

    def test_request_payloads_are_bounded_text(self) -> None:
        for payload in (
            {"subject_id": "implementation-a", "question": ""},
            {"subject_id": "implementation-a", "question": "x" * 2049},
            {"subject_id": "implementation-a", "question": "bad\x07bell"},
        ):
            result = submit_action(
                self.store, self.initiative_id, self.coordinator_document("request-decision", payload),
            )
            with self.subTest(payload=payload):
                self.assertEqual(result["state"], "refused")

    def test_checkpoint_replaces_under_cas_and_rejects_a_stale_prior(self) -> None:
        first = {
            "plan_revision": self.plan["revision"], "event_cursor": self.initiative()["last_event_sequence"],
            "nodes_under_consideration": ["implementation-a"], "pending_decision": None,
            "rationale": "dispatch implementation-a next", "prior_checkpoint_digest": None,
        }
        path = self.root / "checkpoint.json"
        path.write_text(json.dumps(first))
        record, _ = cli._checkpoint_command(
            [self.initiative_id, "--file", str(path), "--json"], self.store, self.pane_env, self.tmux,
        )
        self.assertEqual(record["generation"], 1)
        self.assertEqual(record["digest"], checkpoint_digest(record))
        self.assertEqual(self.store.read_checkpoint(self.initiative_id, self.record["coordinator_id"]), record)
        self.assertEqual(self.events()[-1]["type"], "coordinator-checkpointed")
        stale = dict(first, rationale="second thoughts")
        path.write_text(json.dumps(stale))
        with self.assertRaisesRegex(CoordinatorError, "prior_checkpoint_digest does not match"):
            cli._checkpoint_command([self.initiative_id, "--file", str(path), "--json"], self.store, self.pane_env, self.tmux)
        chained = dict(stale, prior_checkpoint_digest=record["digest"])
        path.write_text(json.dumps(chained))
        second, _ = cli._checkpoint_command(
            [self.initiative_id, "--file", str(path), "--json"], self.store, self.pane_env, self.tmux,
        )
        self.assertEqual(second["rationale"], "second thoughts")
        self.assertEqual(second["prior_checkpoint_digest"], record["digest"])
        beyond = dict(chained, event_cursor=10_000, prior_checkpoint_digest=second["digest"])
        path.write_text(json.dumps(beyond))
        with self.assertRaisesRegex(CoordinatorError, "beyond the durable tail"):
            cli._checkpoint_command([self.initiative_id, "--file", str(path), "--json"], self.store, self.pane_env, self.tmux)
        with self.assertRaisesRegex(CoordinatorError, "not inside the coordinator's anchor pane"):
            cli._checkpoint_command(
                [self.initiative_id, "--file", str(path), "--json"], self.store, {**self.env, "TMUX_PANE": "%9"}, self.tmux,
            )

    def test_coordinator_action_set_is_exactly_the_bounded_set(self) -> None:
        self.assertEqual(COORDINATOR_ACTION_KINDS, frozenset({
            "dispatch-node", "repair-node", "request-salvage", "stop-attempt", "pause",
            "continue-node", "request-decision", "propose-outcome", "directive",
        }))


if __name__ == "__main__":
    unittest.main()
