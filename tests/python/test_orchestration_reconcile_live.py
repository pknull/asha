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
from lib.control.store import StoreError, task_digest
from lib.control.orchestration.actions import (
    _parse_document,
    build_action_document,
    submit_action,
)
from lib.control.orchestration.cli import _operator_action
from lib.control.orchestration.links import control_task_identity_digest
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.reconcile import reconcile_live
from lib.control.orchestration.scheduler import (
    consecutive_failures,
    readiness,
    refresh_readiness,
)
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.orchestration_increment3_fixtures import (
    advance_node,
    save_candidate,
    save_passed_verification,
)


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


class SnapshotWindowAdapters(EvidenceAdapters):
    def __init__(self) -> None:
        super().__init__("working")

    def jj(self, task):
        del task
        return Evidence(
            "jj", "mismatch",
            "jj workspace evidence mismatched: created workspace registration "
            "identity disagrees with working copy",
        )


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

    def test_unchanged_live_status_is_observed_once_until_evidence_changes(self) -> None:
        task = self.dispatch_one()

        for _ in range(3):
            self.reconcile(task, "working")

        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        observed = [
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "task-status-observed"
            and attempt["attempt_id"] in event["subject_ids"]
        ]
        self.assertEqual(len(observed), 1)

        self.reconcile(task, "needs-input")

        changed = [
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "task-status-observed"
            and attempt["attempt_id"] in event["subject_ids"]
        ]
        self.assertEqual(len(changed), 2)
        self.assertEqual(changed[-1]["payload"]["control_state"], "needs-input")

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
        self.assertEqual([item["state"] for item in attempts], ["failed-no-artifact", "allocated"])
        self.assertEqual(attempts[1]["base"], attempts[0]["base"])
        self.assertIsNone(attempts[1]["action_id"])
        self.assertEqual(result["retries"][0]["attempt_id"], attempts[1]["attempt_id"])
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )
        missing = next(
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "result-missing"
            and attempts[0]["attempt_id"] in event["subject_ids"]
        )
        self.assertIsNone(missing["payload"]["refused_ingestion_id"])
        self.assertIsNone(missing["payload"]["refusal"])

    def test_result_missing_event_links_the_refused_ingestion(self) -> None:
        task = self.dispatch_one()
        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        ingestion = self.store.list_result_ingestions_snapshot(self.initiative_id)[0]
        refusal = "authoritative result publication refused: lineage mismatch"
        refused = copy.deepcopy(ingestion)
        refused.update({
            "state": "refused",
            "publication_id": str(uuid.uuid4()),
            "refusal": refusal,
            "updated_at": now_text(),
        })
        self.store.save_result_ingestion(
            self.initiative_id, refused, expected_digest=record_digest(ingestion),
        )
        self.store.config = replace(self.store.config, result_grace_seconds=1)
        first = datetime.now(timezone.utc)
        self.reconcile(task, "exited", now=lambda: first)

        self.reconcile(
            task, "exited", now=lambda: first + timedelta(seconds=2),
        )

        event = next(
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "result-missing"
            and attempt["attempt_id"] in event["subject_ids"]
        )
        self.assertEqual(event["payload"], {
            "grace_seconds": 1,
            "refused_ingestion_id": ingestion["ingestion_id"],
            "refusal": refusal,
        })

    def test_nonzero_or_signal_maps_to_abnormal_exit_and_retry(self) -> None:
        task = self.dispatch_one()
        result = self.reconcile(task, "failed")
        attempts = sorted(
            self.store.list_attempts_snapshot(self.initiative_id),
            key=lambda item: item["ordinal"],
        )
        self.assertEqual(attempts[0]["state"], "failed-no-artifact")
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
            [item["state"] for item in attempts], ["failed-no-artifact", "cancelled"],
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
            "failed-no-artifact",
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

    def test_live_jj_snapshot_split_waits_for_result_or_process_exit(self) -> None:
        task = self.dispatch_one()
        result = reconcile_live(
            self.store, self.initiative_id,
            control_store=self.control(task),
            adapters_factory=lambda _task: SnapshotWindowAdapters(),
        )
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(result["observations"][-1]["control_state"], "working")
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id)[0]["state"],
            "running",
        )
        self.assertEqual(self.initiative()["state"], "running")

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
        self.assertEqual([item["state"] for item in attempts], ["failed-no-artifact", "allocated"])
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


class OrchestrationStrandRecoveryTests(ExecutionFixture, unittest.TestCase):
    """The retroactive half of the stop-attempt strand fix.

    `_release_stopped_node` prevents the contradiction at the two sites that
    create it, but it does nothing for a node already stranded: that attempt is
    already `cancelled`, and `cancelled` is deliberately outside the
    latest-failure acted-on set.  These tests drive the recovery sweep, whose
    trigger is the node/attempt contradiction itself.
    """

    def dispatch(self, node_id="implementation-a"):
        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            self.task = payload["task"]
            return 0, json.dumps(payload).encode(), b""

        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": node_id},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            return submit_action(self.store, self.initiative_id, document)

    def latest_attempt(self, node_id):
        return max(
            (
                item for item in self.store.list_attempts_snapshot(self.initiative_id)
                if item["node_id"] == node_id
            ),
            key=lambda item: (item["ordinal"], item["attempt_id"]),
        )

    def strand(self, node_id):
        """Reproduce the pre-fix shape exactly: attempt cancelled, node untouched.

        This is what `_stop_attempt` used to write, so the fixture builds the
        strand the same way rather than through the now-repaired verb.
        """
        attempt = self.latest_attempt(node_id)
        cancelled = copy.deepcopy(attempt)
        cancelled.update({"state": "cancelled", "updated_at": now_text()})
        self.store.save_attempt(
            self.initiative_id, cancelled, expected_digest=record_digest(attempt),
        )
        return cancelled

    def advance_attempt(self, node_id, states):
        """Walk an attempt through legal ATTEMPT_TRANSITIONS edges."""
        current = self.latest_attempt(node_id)
        for state in states:
            changed = copy.deepcopy(current)
            changed.update({"state": state, "updated_at": now_text()})
            self.store.save_attempt(
                self.initiative_id, changed, expected_digest=record_digest(current),
            )
            current = changed
        return current

    def set_node_state(self, node_id, state):
        node = self.store.read_node(self.initiative_id, node_id)
        changed = copy.deepcopy(node)
        changed["state"] = state
        self.store.save_node(
            self.initiative_id, changed, expected_digest=record_digest(node),
        )
        return changed

    def received_stop(self, attempt_id, *, outcome=...):
        """Save the exact record `submit_action` persists at receipt.

        `_parse_document` builds it, so the fixture cannot drift from the real
        receipt shape.  `outcome` overrides that record's retained outcome; the
        adverse probe from the accepted finding passes `None`, which
        `validate_action` accepts because the action schema declares `outcome`
        optional.
        """
        document = build_action_document(
            self.initiative(), "stop-attempt", {"attempt_id": attempt_id},
        )
        record, _payload = _parse_document(document)
        self.assertEqual(record["state"], "received")
        if outcome is not ...:
            record["outcome"] = outcome
        self.store.save_action(self.initiative_id, record)
        return self.store.read_action(self.initiative_id, record["action_id"])

    def settle_action(self, action, state, outcome):
        settled = copy.deepcopy(action)
        settled.update({
            "state": state, "outcome": json.dumps(
                outcome, sort_keys=True, separators=(",", ":"),
            ), "updated_at": now_text(),
        })
        self.store.save_action(
            self.initiative_id, settled, expected_digest=record_digest(action),
        )
        return settled

    def control(self, extras=None):
        value = mock.Mock()
        value.peek.return_value = self.task
        value.list.return_value = [self.task, *(extras or [])]
        return value

    def sweep(self, *, state="working", control=None):
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            return reconcile_live(
                self.store,
                self.initiative_id,
                control_store=control or self.control(),
                adapters_factory=lambda _task: EvidenceAdapters(state),
            )

    def recovery_events(self, node_id):
        return [
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "node-state-changed"
            and node_id in event["subject_ids"]
            and event["payload"].get("reason") == "stranded-node-recovered"
        ]

    def assert_untouched(self, node_id, state):
        result = self.sweep()
        self.assertEqual(result["recoveries"], [])
        self.assertEqual(self.recovery_events(node_id), [])
        self.assertEqual(
            self.store.read_node(self.initiative_id, node_id)["state"], state,
        )

    def test_stranded_node_is_released_and_dispatchable_again(self) -> None:
        self.dispatch()
        cancelled = self.strand("implementation-a")
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "running",
        )

        result = self.sweep()

        self.assertEqual(
            result["recoveries"],
            [{
                "node_id": "implementation-a",
                "from": "running",
                "retired_review_ids": [],
            }],
        )
        self.assertEqual(
            [
                (event["payload"]["from"], event["payload"]["to"])
                for event in self.recovery_events("implementation-a")
            ],
            [("running", "evaluating"), ("evaluating", "ready")],
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )
        self.assertEqual(
            readiness(self.store, self.initiative())["implementation-a"], "ready",
        )

        redispatched = self.dispatch()

        self.assertEqual(redispatched["state"], "completed", redispatched["outcome"])
        self.assertEqual(
            sorted(
                item["state"]
                for item in self.store.list_attempts_snapshot(self.initiative_id)
                if item["node_id"] == "implementation-a"
            ),
            ["cancelled", "running"],
        )
        self.assertNotEqual(
            self.latest_attempt("implementation-a")["attempt_id"],
            cancelled["attempt_id"],
        )

    def test_recovery_reserves_no_retry_and_trips_no_breaker(self) -> None:
        self.dispatch()
        self.strand("implementation-a")
        before = len(self.store.list_events_snapshot(self.initiative_id))

        result = self.sweep()

        self.assertEqual(result["retries"], [])
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(len(self.store.list_attempts_snapshot(self.initiative_id)), 1)
        self.assertEqual(
            consecutive_failures(
                self.store.list_attempts_snapshot(self.initiative_id),
            ),
            0,
        )
        self.assertEqual(self.initiative()["state"], "running")
        added = self.store.list_events_snapshot(self.initiative_id)[before:]
        self.assertEqual(
            sorted({item["type"] for item in added}), ["node-state-changed"],
        )
        self.assertEqual(
            sorted({
                (item["actor_kind"], item["actor_id"]) for item in added
            }),
            [("controller", "live-reconciler")],
        )

    def test_recovery_is_idempotent_across_passes(self) -> None:
        self.dispatch()
        self.strand("implementation-a")
        self.sweep()

        result = self.sweep()

        self.assertEqual(result["recoveries"], [])
        self.assertEqual(len(self.recovery_events("implementation-a")), 2)

    def test_a_node_stranded_while_dispatching_is_released_too(self) -> None:
        """A stop can land before the node ever reaches `running`.

        `dispatching -> evaluating -> ready` is the same walk, and both edges
        are legal, so the sweep must cover the shorter strand as well.
        """
        self.dispatch()
        self.strand("implementation-a")
        self.sweep()
        self.set_node_state("implementation-a", "dispatching")

        result = self.sweep()

        self.assertEqual(
            [item["node_id"] for item in result["recoveries"]], ["implementation-a"],
        )
        self.assertEqual(result["recoveries"][0]["from"], "dispatching")
        self.assertEqual(
            [
                (event["payload"]["from"], event["payload"]["to"])
                for event in self.recovery_events("implementation-a")[-2:]
            ],
            [("dispatching", "evaluating"), ("evaluating", "ready")],
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )

    def test_sweep_leaves_a_node_holding_an_allocated_reservation(self) -> None:
        self.dispatch()
        cancelled = self.strand("implementation-a")
        reservation = copy.deepcopy(cancelled)
        reservation.update({
            "attempt_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "action_id": None,
            "ordinal": cancelled["ordinal"] + 1,
            "state": "allocated",
            "result_publication_id": None,
            "result_id": None,
            "seal_id": None,
            "created_at": now_text(),
            "updated_at": now_text(),
        })
        self.store.save_attempt(self.initiative_id, reservation)

        self.assert_untouched("implementation-a", "running")

    def test_sweep_leaves_an_evaluating_node_whose_newest_attempt_is_no_stop(
        self,
    ) -> None:
        """`evaluating` alone is never the trigger; the newest attempt is.

        A node evaluating a published seal has exactly the shape the sweep
        selects on -- every attempt terminal, no reservation, no action -- and
        must be left to its evaluation.  Only a `cancelled` newest attempt
        marks the state as a release walk that stopped halfway.
        """
        self.dispatch()
        self.advance_attempt("implementation-a", [
            "reported", "awaiting-exit", "success-seal-ready", "sealing",
            "sealed-success",
        ])
        self.set_node_state("implementation-a", "evaluating")

        self.assert_untouched("implementation-a", "evaluating")

    def test_sweep_finishes_a_release_walk_interrupted_between_its_writes(self) -> None:
        """The sweep's own walk is two writes and must itself be restartable.

        Injecting the failure into the second `save_node` reproduces the
        half-released node -- attempt `cancelled`, node `evaluating` -- that
        would otherwise be stranded exactly like the strand this sweep exists
        to repair, one edge further along.
        """
        self.dispatch()
        self.strand("implementation-a")
        real = self.store.save_node
        writes = []

        def save_node(initiative_id, record, **kwargs):
            writes.append(record["state"])
            if len(writes) > 1:
                raise OSError("injected node write failure")
            return real(initiative_id, record, **kwargs)

        with mock.patch.object(self.store, "save_node", side_effect=save_node):
            # The store reports the failed write through its lock teardown.
            with self.assertRaises(StoreError):
                self.sweep()
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "evaluating",
        )

        result = self.sweep()

        self.assertEqual(
            result["recoveries"],
            [{
                "node_id": "implementation-a",
                "from": "evaluating",
                "retired_review_ids": [],
            }],
        )
        self.assertEqual(
            [
                (event["payload"]["from"], event["payload"]["to"])
                for event in self.recovery_events("implementation-a")
            ],
            [("running", "evaluating"), ("evaluating", "ready")],
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )

        redispatched = self.dispatch()

        self.assertEqual(redispatched["state"], "completed", redispatched["outcome"])

    def test_recovery_defers_a_failure_retry_to_the_following_pass(self) -> None:
        """Recovery reserves nothing, including for a node whose attempt failed.

        The sweep's predicate is a node/attempt contradiction, not a failure
        verdict, so the pass that releases a node must not also charge that
        node's retry budget through `_failure_target`.  The retry is not lost:
        the next pass sees an ordinary `ready` node with a latest failure and
        reserves it with the ordinary accounting.
        """
        self.dispatch()
        self.advance_attempt("implementation-a", ["indeterminate", "launch-failed"])
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "running",
        )

        recovering = self.sweep()

        self.assertEqual(
            [item["node_id"] for item in recovering["recoveries"]],
            ["implementation-a"],
        )
        self.assertEqual(recovering["retries"], [])
        self.assertEqual(len(self.store.list_attempts_snapshot(self.initiative_id)), 1)
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )

        following = self.sweep()

        self.assertEqual(following["recoveries"], [])
        self.assertEqual(len(following["retries"]), 1)
        reserved = self.latest_attempt("implementation-a")
        self.assertEqual(reserved["state"], "allocated")
        self.assertEqual(reserved["ordinal"], 2)

    def test_sweep_leaves_an_evaluating_node_owned_by_a_live_verification(self) -> None:
        """A verification owns the `evaluating` node it moved there.

        `_cancel_node` refuses an evaluating node that owns a non-stale
        verification for the same reason: the verification path writes the
        node's next state itself.
        """
        self.dispatch()
        self.strand("implementation-a")
        self.set_node_state("implementation-a", "evaluating")
        save_passed_verification(
            self, save_candidate(self), node_id="implementation-a",
        )

        self.assert_untouched("implementation-a", "evaluating")

    def test_sweep_leaves_a_needs_input_node_to_continue_node(self) -> None:
        self.dispatch()
        self.strand("implementation-a")
        self.set_node_state("implementation-a", "needs-input")

        self.assert_untouched("implementation-a", "needs-input")

    def test_sweep_leaves_a_node_whose_attempt_is_still_live(self) -> None:
        self.dispatch()
        self.assertEqual(
            self.latest_attempt("implementation-a")["state"], "running",
        )

        self.assert_untouched("implementation-a", "running")

    def test_sweep_leaves_a_node_owned_by_an_unsettled_action(self) -> None:
        """An interrupted stop is `reconcile_actions`' to finish, not the sweep's.

        That path completes the same release through `_release_stopped_node`.
        Both repairs firing on one node would race for its transition.
        """
        self.dispatch()
        cancelled = self.strand("implementation-a")
        document = build_action_document(
            self.initiative(), "stop-attempt",
            {"attempt_id": cancelled["attempt_id"]},
        )
        interrupted = {
            key: value for key, value in document.items() if key != "payload"
        }
        interrupted.update({
            "state": "indeterminate",
            "outcome": json.dumps({
                "payload": {"attempt_id": cancelled["attempt_id"]},
                "status": "indeterminate",
            }, sort_keys=True, separators=(",", ":")),
            "received_at": now_text(),
            "updated_at": now_text(),
        })
        self.store.save_action(self.initiative_id, interrupted)

        self.assert_untouched("implementation-a", "running")

    def test_sweep_leaves_a_node_owned_by_an_ordinary_received_stop(self) -> None:
        """`received` is a durable state, not an instant on the way to another.

        `submit_action` writes the receipt record and only then validates,
        dispatches and completes the action, so a controller that dies anywhere
        in that sequence leaves an ordinary `received` stop-attempt behind.  It
        still owns the attempt it names.
        """
        self.dispatch()
        cancelled = self.strand("implementation-a")
        self.received_stop(cancelled["attempt_id"])

        self.assert_untouched("implementation-a", "running")

    def test_sweep_fails_closed_on_a_received_stop_with_no_retained_outcome(
        self,
    ) -> None:
        """The adverse probe: ownership the predicate cannot read is still ownership.

        An action names its target only through its retained outcome, and the
        action schema declares that outcome optional -- `validate_action` runs
        it through `_optional_text`, so `None` is a legal retained value and
        `action_outcome` answers it with an empty object.  A predicate that only
        looks for a positive node/attempt binding therefore reads "nothing owns
        this node" for an unsettled stop that owns it, and the sweep releases
        the node to `ready` while a live stop is still entitled to write it.
        That is the accepted finding this test closes: ambiguity must block.
        """
        self.dispatch()
        cancelled = self.strand("implementation-a")
        received = self.received_stop(cancelled["attempt_id"], outcome=None)
        self.assertEqual(received["state"], "received")
        self.assertIsNone(received["outcome"])

        self.assert_untouched("implementation-a", "running")

        # And the block is temporary, not a wedge: settling the action -- here
        # by the ordinary `received -> refused` edge -- hands the node back.
        self.settle_action(received, "refused", {
            "payload": {"attempt_id": cancelled["attempt_id"]}, "status": "refused",
            "reason": "recovered after controller interruption",
        })

        result = self.sweep()

        self.assertEqual(
            [item["node_id"] for item in result["recoveries"]], ["implementation-a"],
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )

    def test_sweep_fails_closed_on_an_unsettled_action_with_no_payload(self) -> None:
        """The same rule one step in: a retained outcome that names nothing.

        A truncated or half-written outcome object is the same ownership
        ambiguity as a null one, and gets the same answer.
        """
        self.dispatch()
        cancelled = self.strand("implementation-a")
        received = self.received_stop(
            cancelled["attempt_id"], outcome=json.dumps({"status": "received"}),
        )

        self.assert_untouched("implementation-a", "running")

        self.settle_action(received, "refused", {
            "payload": {"attempt_id": cancelled["attempt_id"]}, "status": "refused",
            "reason": "recovered after controller interruption",
        })
        self.assertEqual(
            [item["node_id"] for item in self.sweep()["recoveries"]],
            ["implementation-a"],
        )


class OrchestrationStrandedReviewRecoveryTests(ExecutionFixture, unittest.TestCase):
    """The exact live shape: a stranded review node still owning a running review."""

    def setUp(self) -> None:
        super().setUp()
        self.candidate = save_candidate(self)
        advance_node(self, "implementation-a", ["evaluating", "succeeded"])
        refresh_readiness(self.store, self.initiative_id)
        self.dispatch_review()

    def dispatch_review(self):
        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            payload["task"]["jj"].update({
                "base_commit_id": argv[argv.index("--base") + 1],
                "working_commit_id": "f" * 40,
            })
            self.task = payload["task"]
            return 0, json.dumps(payload).encode(), b""

        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "review-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            return submit_action(self.store, self.initiative_id, document)

    def review_for(self, attempt_id):
        return next(
            item for item in self.store.list_reviews_snapshot(self.initiative_id)
            if item["attempt_id"] == attempt_id
        )

    def test_stranded_review_node_heals_to_ready_with_a_stale_review(self) -> None:
        attempt = max(
            (
                item for item in self.store.list_attempts_snapshot(self.initiative_id)
                if item["node_id"] == "review-a"
            ),
            key=lambda item: item["ordinal"],
        )
        review = self.review_for(attempt["attempt_id"])
        self.assertEqual(review["state"], "running")
        self.assertEqual(
            self.store.read_node(self.initiative_id, "review-a")["state"], "running",
        )
        cancelled = copy.deepcopy(attempt)
        cancelled.update({"state": "cancelled", "updated_at": now_text()})
        self.store.save_attempt(
            self.initiative_id, cancelled, expected_digest=record_digest(attempt),
        )

        control = mock.Mock()
        control.peek.return_value = self.task
        control.list.return_value = [self.task]
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            # The fixture's candidate seal is synthetic and has no jj workspace
            # to re-measure. Seal drift is orthogonal to strand recovery, and
            # letting it pause the initiative would mask the re-dispatch.
            "lib.control.orchestration.reconcile.reconcile_seal_drift",
            return_value=[],
        ):
            result = reconcile_live(
                self.store, self.initiative_id, control_store=control,
                adapters_factory=lambda _task: EvidenceAdapters("working"),
            )

        self.assertEqual(
            result["recoveries"],
            [{
                "node_id": "review-a",
                "from": "running",
                "retired_review_ids": [review["review_id"]],
            }],
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "review-a")["state"], "ready",
        )
        retired = self.store.read_review(self.initiative_id, review["review_id"])
        self.assertEqual(retired["state"], "stale")
        self.assertIsNone(retired["verdict"])
        self.assertEqual(retired["findings"], [])
        self.assertEqual(result["retries"], [])
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(self.initiative()["state"], "running")

        redispatched = self.dispatch_review()

        self.assertEqual(redispatched["state"], "completed", redispatched["outcome"])
        fresh = max(
            (
                item for item in self.store.list_attempts_snapshot(self.initiative_id)
                if item["node_id"] == "review-a"
            ),
            key=lambda item: item["ordinal"],
        )
        self.assertNotEqual(fresh["attempt_id"], attempt["attempt_id"])
        self.assertEqual(fresh["state"], "running")
        self.assertEqual(self.review_for(fresh["attempt_id"])["state"], "running")
        self.assertEqual(
            sorted(
                item["state"]
                for item in self.store.list_reviews_snapshot(self.initiative_id)
            ),
            ["running", "stale"],
        )


if __name__ == "__main__":
    unittest.main()
