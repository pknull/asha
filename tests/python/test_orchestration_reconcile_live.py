from __future__ import annotations

import copy
import json
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest import mock

from lib.control.reconcile import Evidence
from lib.control.launch import persist_terminal_reconciliation
from lib.control.store import task_digest
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.cli import _operator_action
from lib.control.orchestration.links import control_task_identity_digest
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.reconcile import reconcile_live
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text


class EvidenceAdapters:
    def __init__(self, state: str) -> None:
        self.state = state

    def tmux(self, task, run):
        return Evidence("tmux", "match", "owned pane matched")

    def process(self, task, run):
        if self.state in {"exited", "failed"}:
            return Evidence(
                "process", "missing", "process ended", state=self.state,
            )
        if self.state == "stale":
            return Evidence("process", "mismatch", "process identity changed")
        return Evidence("process", "match", "process identity matched")

    def jj(self, task):
        return Evidence("jj", "match", "workspace identity matched")

    def event(self, task, run):
        if self.state in {"working", "needs-input", "idle"}:
            return Evidence("event", "match", "semantic event", state=self.state)
        if self.state == "unknown":
            return Evidence("event", "unavailable", "event unavailable")
        return Evidence("event", "missing", "no semantic event")


class OrchestrationLiveReconciliationTests(ExecutionFixture, unittest.TestCase):
    def dispatch_one(self):
        tasks = []

        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            tasks.append(payload["task"])
            return 0, json.dumps(payload).encode(), b""

        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            submit_action(self.store, self.initiative_id, document)
        return tasks[0]

    @staticmethod
    def control(task, *, extras=None):
        value = mock.Mock()
        value.peek.return_value = task
        value.list.return_value = [task, *(extras or [])]
        return value

    def reconcile(self, task, state, *, now=None, control=None):
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            return reconcile_live(
                self.store,
                self.initiative_id,
                control_store=control or self.control(task),
                adapters_factory=lambda _task: EvidenceAdapters(state),
                now=now,
            )

    def test_live_states_remain_running_and_record_observation(self) -> None:
        task = self.dispatch_one()
        for state in ("starting", "working", "needs-input", "idle", "unknown"):
            with self.subTest(state=state):
                result = self.reconcile(task, state)
                self.assertEqual(result["observations"][-1]["control_state"], state)
                attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
                self.assertEqual(attempt["state"], "running")
        self.assertIn(
            "task-status-observed",
            [event["type"] for event in self.store.list_events_snapshot(self.initiative_id)],
        )

    def test_normal_exit_waits_grace_then_reserves_retry_from_original_base(self) -> None:
        task = self.dispatch_one()
        self.store.config = replace(self.store.config, result_grace_seconds=1)
        first = datetime.now(timezone.utc)
        self.reconcile(task, "exited", now=lambda: first)
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id)[0]["state"],
            "running",
        )
        result = self.reconcile(
            task, "exited", now=lambda: first + timedelta(seconds=2),
        )
        attempts = sorted(
            self.store.list_attempts_snapshot(self.initiative_id),
            key=lambda item: item["ordinal"],
        )
        self.assertEqual([item["state"] for item in attempts], ["result-missing", "allocated"])
        self.assertEqual(attempts[1]["base"], attempts[0]["base"])
        self.assertIsNone(attempts[1]["action_id"])
        self.assertEqual(result["retries"][0]["attempt_id"], attempts[1]["attempt_id"])
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )

    def test_nonzero_or_signal_maps_to_abnormal_exit_and_retry(self) -> None:
        task = self.dispatch_one()
        result = self.reconcile(task, "failed")
        attempts = sorted(
            self.store.list_attempts_snapshot(self.initiative_id),
            key=lambda item: item["ordinal"],
        )
        self.assertEqual(attempts[0]["state"], "abnormal-exit")
        self.assertEqual(attempts[1]["state"], "allocated")
        self.assertEqual(len(result["retries"]), 1)

    def test_cancel_after_abnormal_exit_cancels_unbound_retry_without_control(self) -> None:
        task = self.dispatch_one()
        self.reconcile(task, "failed")
        document = build_action_document(
            self.initiative(), "cancel-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.actions.capture_bytes",
        ) as capture, mock.patch(
            "lib.control.orchestration.actions.TaskStore",
        ) as control_store:
            action = submit_action(self.store, self.initiative_id, document)
        attempts = sorted(
            self.store.list_attempts_snapshot(self.initiative_id),
            key=lambda item: item["ordinal"],
        )
        self.assertEqual(action["state"], "completed", action["outcome"])
        self.assertEqual(
            [item["state"] for item in attempts], ["abnormal-exit", "cancelled"],
        )
        self.assertEqual(attempts[1]["action_id"], action["action_id"])
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "cancelled",
        )
        control_store.assert_not_called()
        capture.assert_not_called()

    def test_allocated_retry_dispatches_under_the_attempt_cap(self) -> None:
        task = self.dispatch_one()
        result = self.reconcile(task, "failed")
        retry = result["retries"][0]
        self.assertIsNone(retry["action_id"])
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )

        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            return 0, json.dumps(payload).encode(), b""

        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "completed")
        adopted = self.store.read_attempt(self.initiative_id, retry["attempt_id"])
        self.assertEqual(adopted["state"], "running")
        self.assertEqual(adopted["action_id"], action["action_id"])

    def test_retry_refused_while_paused_is_later_adopted_by_fresh_action(self) -> None:
        task = self.dispatch_one()
        retry = self.reconcile(task, "failed")["retries"][0]
        pause = build_action_document(self.initiative(), "pause", {})
        self.assertEqual(submit_action(self.store, self.initiative_id, pause)["state"], "completed")
        refused = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            first = submit_action(self.store, self.initiative_id, refused)
        self.assertEqual(first["state"], "refused")
        self.assertIsNone(
            self.store.read_attempt(self.initiative_id, retry["attempt_id"])["action_id"]
        )
        control = self.control(task)
        resume = build_action_document(self.initiative(), "resume", {})
        with mock.patch(
            "lib.control.orchestration.reconcile.TaskStore", return_value=control,
        ), mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            resumed = submit_action(self.store, self.initiative_id, resume)
        self.assertEqual(resumed["state"], "completed")

        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            return 0, json.dumps(payload).encode(), b""

        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            dispatched, _ = _operator_action(
                "dispatch",
                [self.initiative_id, "--node", "implementation-a"],
                self.store,
            )
        self.assertEqual(dispatched["state"], "completed", dispatched["outcome"])
        self.assertNotEqual(dispatched["action_id"], refused["action_id"])
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, retry["attempt_id"])["action_id"],
            dispatched["action_id"],
        )

    def test_terminal_control_persistence_preserves_identity_and_reaches_result_missing(self) -> None:
        task = self.dispatch_one()
        terminal = persist_terminal_reconciliation(
            task,
            {
                "runs": [{
                    "run_id": task["runs"][0]["run_id"],
                    "state": "exited",
                    "blocker": None,
                    "evidence": [{
                        "source": "process", "outcome": "missing",
                        "detail": "process ended", "state": "exited", "stale": False,
                    }],
                }],
            },
            mock.Mock(),
        )
        self.assertEqual(
            control_task_identity_digest(terminal), control_task_identity_digest(task),
        )
        self.store.config = replace(self.store.config, result_grace_seconds=0)
        control = self.control(terminal)
        first = datetime.now(timezone.utc)
        self.reconcile(terminal, "exited", now=lambda: first, control=control)
        second = datetime.now(timezone.utc) + timedelta(seconds=1)
        result = self.reconcile(
            terminal, "exited", now=lambda: second,
            control=control,
        )
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(
            min(
                self.store.list_attempts_snapshot(self.initiative_id),
                key=lambda item: item["ordinal"],
            )["state"],
            "result-missing",
        )

    def assert_reconciliation_conflict(self, result) -> None:
        self.assertTrue(result["conflicts"])
        self.assertEqual(self.initiative()["state"], "paused")
        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        self.assertEqual(attempt["state"], "stale")
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "stale",
        )
        self.assertIn(
            "reconciliation-conflict",
            [event["type"] for event in self.store.list_events_snapshot(self.initiative_id)],
        )

    def test_missing_task_pauses_with_conflict(self) -> None:
        from lib.control.store import StoreError

        task = self.dispatch_one()
        control = self.control(task)
        control.peek.side_effect = StoreError("task not found")
        self.assert_reconciliation_conflict(
            self.reconcile(task, "working", control=control),
        )

    def test_control_identity_digest_mismatch_pauses_with_conflict(self) -> None:
        task = self.dispatch_one()
        changed = dict(task)
        changed["label"] = task["label"] + " changed"
        control = self.control(task)
        control.peek.return_value = changed
        self.assert_reconciliation_conflict(
            self.reconcile(task, "working", control=control),
        )

    def test_stale_live_evidence_pauses_with_conflict(self) -> None:
        task = self.dispatch_one()
        self.assert_reconciliation_conflict(self.reconcile(task, "stale"))

    def test_nested_workflow_second_control_task_trips_breaker(self) -> None:
        task = self.dispatch_one()
        extra = dict(task)
        extra["task_id"] = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        control = self.control(task, extras=[extra])
        result = self.reconcile(task, "working", control=control)
        self.assertTrue(result["conflicts"])
        self.assertEqual(self.initiative()["state"], "paused")

    def test_attempt_cap_marks_node_failed_without_allocating(self) -> None:
        task = self.dispatch_one()
        initiative = self.initiative()
        changed = dict(initiative)
        changed["limits"] = dict(initiative["limits"])
        changed["limits"]["max_attempts_per_node"] = 1
        changed["state_revision"] += 1
        changed["updated_at"] = now_text()
        self.store.save_initiative(
            changed, expected_digest=record_digest(initiative),
        )
        self.reconcile(task, "failed")
        self.assertEqual(len(self.store.list_attempts_snapshot(self.initiative_id)), 1)
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "failed",
        )

    def test_consecutive_failure_breaker_pauses_before_retry(self) -> None:
        task = self.dispatch_one()
        self.store.config = replace(
            self.store.config, max_consecutive_failures=1,
        )
        result = self.reconcile(task, "failed")
        self.assertEqual(result["retries"], [])
        self.assertEqual(self.initiative()["state"], "paused")
        self.assertEqual(
            len(self.store.list_attempts_snapshot(self.initiative_id)), 1,
        )
        self.assertIn(
            "limit-reached",
            [event["type"] for event in self.store.list_events_snapshot(self.initiative_id)],
        )

        control = self.control(task)
        resume = build_action_document(self.initiative(), "resume", {})
        with mock.patch(
            "lib.control.orchestration.reconcile.TaskStore", return_value=control,
        ), mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            resumed = submit_action(self.store, self.initiative_id, resume)
        self.assertEqual(resumed["state"], "completed", resumed["outcome"])
        attempts = sorted(
            self.store.list_attempts_snapshot(self.initiative_id),
            key=lambda item: item["ordinal"],
        )
        self.assertEqual([item["state"] for item in attempts], ["abnormal-exit", "allocated"])
        self.assertIsNone(attempts[-1]["action_id"])
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )

    def test_cancelled_node_does_not_receive_launch_failure_retry(self) -> None:
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            return_value=(2, b"", b"refused"),
        ):
            submit_action(self.store, self.initiative_id, document)
        cancel = build_action_document(
            self.initiative(), "cancel-node", {"node_id": "implementation-a"},
        )
        self.assertEqual(
            submit_action(self.store, self.initiative_id, cancel)["state"], "completed",
        )
        control = mock.Mock()
        control.list.return_value = []
        result = self.reconcile(None, "working", control=control)
        self.assertEqual(result["retries"], [])
        self.assertEqual(len(self.store.list_attempts_snapshot(self.initiative_id)), 1)

    def test_control_task_list_is_cached_once_and_failure_is_a_probe(self) -> None:
        task = self.dispatch_one()
        first_attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        first_link = self.store.read_link(self.initiative_id, first_attempt["attempt_id"])
        second_attempt = copy.deepcopy(first_attempt)
        second_attempt.update({
            "attempt_id": str(uuid.uuid4()),
            "node_id": "review-a",
            "task_id": str(uuid.uuid4()),
            "action_id": str(uuid.uuid4()),
            "ordinal": 1,
        })
        self.store.save_attempt(self.initiative_id, second_attempt)
        second_task = copy.deepcopy(task)
        second_task["task_id"] = second_attempt["task_id"]
        second_task["label"] = f"assignment {second_attempt['attempt_id']}"
        second_link = copy.deepcopy(first_link)
        second_link.update({
            "node_id": second_attempt["node_id"],
            "attempt_id": second_attempt["attempt_id"],
            "action_id": second_attempt["action_id"],
            "control_task_id": second_attempt["task_id"],
            "control_task_identity_digest": control_task_identity_digest(second_task),
            "control_task_record_digest": task_digest(second_task),
        })
        self.store.save_link(self.initiative_id, second_link)
        control = mock.Mock()
        control.list.return_value = [task, second_task]
        control.peek.side_effect = lambda task_id: (
            task if task_id == task["task_id"] else second_task
        )
        result = self.reconcile(task, "working", control=control)
        self.assertEqual(control.list.call_count, 1)
        self.assertEqual(result["probes"][0]["outcome"], "match")

        unavailable = mock.Mock()
        unavailable.list.side_effect = OSError("injected list failure")
        unavailable.peek.side_effect = control.peek.side_effect
        result = self.reconcile(task, "working", control=unavailable)
        self.assertEqual(unavailable.list.call_count, 1)
        self.assertEqual(result["probes"][0]["outcome"], "unavailable")
        self.assertIn("injected list failure", result["probes"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
