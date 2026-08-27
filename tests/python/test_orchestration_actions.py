from __future__ import annotations

import copy
import json
import os
import unittest
import uuid
from unittest import mock

from lib.control.orchestration.actions import (
    ActionError,
    ActionRefused,
    _parse_document,
    _repair_node,
    build_action_document,
    payload_digest,
    reconcile_actions,
    submit_action,
)
from lib.control.orchestration.coordinator import claim
from lib.control.orchestration.scheduler import (
    SchedulerError,
    consecutive_failures,
    readiness,
    refresh_readiness,
)
from lib.control.orchestration.model import ATTEMPT_CONTRACT, record_digest
from lib.control.orchestration.store import ObservationOnlyPlanError
from lib.control.jj import RepositoryFacts
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.orchestration_increment3_fixtures import advance_node, save_candidate
from tests.python.test_orchestration_coordinator_claim import FakeTmux


class CoordinatorEnvelope:
    """Submit stops the way a live coordinator generation actually submits them.

    The stop regressions below exist to prove a coordinator's stop releases its
    node.  Building the document without a coordinator record makes it an
    operator action: `build_action_document` writes `actor_kind=operator`, the
    document carries no `coordinator_id`/`coordinator_generation`, and
    `submit_action` never reaches `_coordinator_fence`.  Such a regression stays
    green even when the real coordinator action would be refused outright, which
    is the opposite of what it claims to cover.  So the fixture claims a real
    generation from a real anchored pane and every stop goes through its
    envelope.
    """

    def setUp(self) -> None:
        super().setUp()
        self.tmux = FakeTmux()
        self.pane_env = {**self.env, "TMUX_PANE": "%7"}
        self._coordinator = None

    def coordinator(self):
        """The live generation for this initiative, claimed on first use."""
        if self._coordinator is None:
            self._coordinator = claim(
                self.store, self.initiative(), env=self.pane_env, tmux=self.tmux,
            )
        return self._coordinator

    def coordinator_document(self, action_class, payload, *, record=None):
        record = self.coordinator() if record is None else record
        document = build_action_document(
            self.initiative(), action_class, payload,
            actor_id=f"coordinator:{record['coordinator_id']}", coordinator=record,
        )
        assert document["actor_kind"] == "coordinator"
        assert document["coordinator_id"] == record["coordinator_id"]
        assert document["coordinator_generation"] == record["generation"]
        return document

    def assert_coordinator_action(self, action):
        """The stored record proves the envelope, not just the document."""
        self.assertEqual(action["actor_kind"], "coordinator")
        self.assertEqual(action["coordinator_id"], self.coordinator()["coordinator_id"])
        self.assertEqual(
            action["coordinator_generation"], self.coordinator()["generation"],
        )


class OrchestrationActionTests(CoordinatorEnvelope, ExecutionFixture, unittest.TestCase):
    def dispatch_action(self, node_id: str = "implementation-a", *, coordinator=False):
        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            self.last_control_task = payload["task"]
            return 0, json.dumps(payload).encode(), b""

        document = (
            self.coordinator_document("dispatch-node", {"node_id": node_id})
            if coordinator
            else build_action_document(
                self.initiative(), "dispatch-node", {"node_id": node_id},
            )
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            return submit_action(self.store, self.initiative_id, document)

    def dispatch_one(self):
        self.dispatch_action()
        return self.store.list_attempts_snapshot(self.initiative_id)[0]

    def stop_action(self, attempt_id: str, *, record=None):
        """Submit stop-attempt through the live coordinator generation's envelope."""
        document = self.coordinator_document(
            "stop-attempt", {"attempt_id": attempt_id}, record=record,
        )
        return self.submit_stop(document)

    def submit_stop(self, document, *, capture=None):
        with mock.patch(
            "lib.control.orchestration.actions.capture_bytes",
            **(capture or {"return_value": (0, b"", b"")}),
        ), mock.patch(
            "lib.control.orchestration.actions.TaskStore",
        ) as control_store:
            control_store.return_value.peek.return_value = self.last_control_task
            return submit_action(self.store, self.initiative_id, document)

    def reconcile_stop(self):
        with mock.patch(
            "lib.control.orchestration.actions.TaskStore",
        ) as control_store, mock.patch(
            "lib.control.orchestration.actions.reconcile_task",
            return_value={"state": "exited", "blocker": None, "evidence": []},
        ):
            control_store.return_value.peek.return_value = self.last_control_task
            return reconcile_actions(self.store, self.initiative_id)["actions"][0]

    def node_state_changes(self, node_id: str, since: int = 0):
        return [
            (event["payload"].get("from"), event["payload"].get("to"))
            for event in self.store.list_events_snapshot(self.initiative_id)[since:]
            if event["type"] == "node-state-changed" and node_id in event["subject_ids"]
        ]

    def test_historical_execution_actions_refuse_before_any_execution_effect(self) -> None:
        retained, raw = self.install_historical_active_plan()
        plan_path = (
            self.config.initiatives_dir / self.initiative_id / "plans" / "0001.json"
        )
        identity = "33333333-3333-4333-8333-333333333333"
        cases = (
            ("activate-initiative", {}),
            ("dispatch-node", {"node_id": "implementation-a"}),
            ("dispatch-node", {"node_id": "review-a"}),
            ("dispatch-node", {"node_id": "verify-a"}),
            ("resume", {}),
            ("repair-node", {"node_id": "implementation-a", "seal_id": identity}),
            ("continue-node", {
                "node_id": "implementation-a", "paused_seal_id": identity,
                "decision_action_id": "44444444-4444-4444-8444-444444444444",
            }),
        )

        with mock.patch(
            "lib.control.orchestration.scheduler.dispatch",
        ) as dispatch, mock.patch(
            "lib.control.orchestration.verification.prevalidate_verification",
        ) as verify, mock.patch(
            "lib.control.orchestration.verification.prepare_verification_intent",
        ) as prepare, mock.patch(
            "lib.control.orchestration.verification.run_verification",
        ) as run:
            for action_class, payload in cases:
                with self.subTest(action_class=action_class, payload=payload):
                    before_attempts = self.store.list_attempts_snapshot(self.initiative_id)
                    action = submit_action(
                        self.store, self.initiative_id,
                        build_action_document(
                            self.initiative(), action_class, payload,
                        ),
                    )
                    self.assertEqual(action["state"], "refused")
                    reason = json.loads(action["outcome"])["reason"]
                    self.assertIn("is observation-only", reason)
                    self.assertIn("execution authority cannot be inferred", reason)
                    self.assertEqual(
                        self.store.list_attempts_snapshot(self.initiative_id),
                        before_attempts,
                    )
        dispatch.assert_not_called()
        verify.assert_not_called()
        prepare.assert_not_called()
        run.assert_not_called()
        self.assertEqual(plan_path.read_bytes(), raw)
        self.assertEqual(self.initiative()["active_plan"]["digest"], retained["digest"])

    def test_historical_cancel_action_remains_safe_terminal_containment(self) -> None:
        _, raw = self.install_historical_active_plan()
        path = (
            self.config.initiatives_dir / self.initiative_id / "plans" / "0001.json"
        )
        before_attempts = self.store.list_attempts_snapshot(self.initiative_id)

        with mock.patch(
            "lib.control.orchestration.scheduler.dispatch",
        ) as dispatch, mock.patch(
            "lib.control.orchestration.actions._stop_task",
        ) as stop_task, mock.patch(
            "lib.control.orchestration.verification.run_verification",
        ) as verify:
            action = submit_action(
                self.store, self.initiative_id,
                build_action_document(
                    self.initiative(), "cancel-node",
                    {"node_id": "implementation-a"},
                ),
            )

        self.assertEqual(action["state"], "completed")
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "cancelled",
        )
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id), before_attempts,
        )
        self.assertEqual(path.read_bytes(), raw)
        dispatch.assert_not_called()
        stop_task.assert_not_called()
        verify.assert_not_called()

    def test_direct_repair_retains_strict_historical_plan_defense(self) -> None:
        self.install_historical_active_plan()
        identity = "33333333-3333-4333-8333-333333333333"
        document = build_action_document(
            self.initiative(), "repair-node",
            {"node_id": "implementation-a", "seal_id": identity},
        )
        action, _ = _parse_document(document)
        before_attempts = self.store.list_attempts_snapshot(self.initiative_id)

        with self.assertRaises(ObservationOnlyPlanError), mock.patch.object(
            self.store, "save_attempt",
        ) as save_attempt:
            _repair_node(
                self.store, action, "implementation-a", identity,
            )

        save_attempt.assert_not_called()
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id), before_attempts,
        )

    def test_same_id_same_digest_returns_stored_outcome_without_effect(self) -> None:
        document = build_action_document(self.initiative(), "pause", {})
        first = submit_action(self.store, self.initiative_id, document)
        event_count = len(self.store.list_events_snapshot(self.initiative_id))
        second = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "completed")
        self.assertEqual(
            len(self.store.list_events_snapshot(self.initiative_id)), event_count,
        )

    def test_same_id_changed_envelope_is_refused_without_mutating_original(self) -> None:
        document = build_action_document(self.initiative(), "pause", {})
        stored = submit_action(self.store, self.initiative_id, document)
        changed = copy.deepcopy(document)
        changed["payload"] = {"substitution": True}
        changed["payload_digest"] = payload_digest(changed["payload"])
        with self.assertRaises(ActionRefused):
            submit_action(self.store, self.initiative_id, changed)
        self.assertEqual(
            self.store.read_action(self.initiative_id, stored["action_id"]), stored,
        )

    def test_forbidden_class_refusal_precedes_stale_envelope_checks(self) -> None:
        document = build_action_document(self.initiative(), "push", {})
        document["active_plan_digest"] = "0" * 64
        document["expected_state_revision"] -= 1
        result = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(result["state"], "refused")
        self.assertEqual(
            json.loads(result["outcome"])["reason"],
            "action class is forbidden in Core v1",
        )

    def test_expected_revision_mismatch_is_durably_refused(self) -> None:
        document = build_action_document(self.initiative(), "pause", {})
        document["expected_state_revision"] -= 1
        result = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(result["state"], "refused")
        self.assertIn("expected state revision", result["outcome"])

    def test_indeterminate_dispatch_reconciles_absent_creation_to_refusal(self) -> None:
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=SchedulerError("command timed out"),
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "indeterminate")
        reconciled = reconcile_actions(self.store, self.initiative_id)
        self.assertEqual(reconciled["actions"][0]["state"], "refused")
        attempts = self.store.list_attempts_snapshot(self.initiative_id)
        self.assertEqual(attempts[0]["state"], "launch-failed")

    def test_interrupted_control_creation_stays_indeterminate_with_recovery_command(self) -> None:
        calls = []

        def timeout(argv, **_kwargs):
            calls.append(list(argv))
            raise SchedulerError("command timed out")

        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=timeout,
        ):
            action = submit_action(self.store, self.initiative_id, document)
        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        creating = self.control_payload(calls[0])["task"]
        creating["lifecycle"] = "creating"
        control = mock.Mock()
        control.peek.return_value = creating
        with mock.patch(
            "lib.control.orchestration.actions.TaskStore", return_value=control,
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
        ) as start:
            result = reconcile_actions(self.store, self.initiative_id)
        reconciled = result["actions"][0]
        self.assertEqual(action["state"], "indeterminate")
        self.assertEqual(reconciled["state"], "indeterminate")
        self.assertEqual(
            json.loads(reconciled["outcome"])["remediation"],
            f"asha task recover {attempt['task_id']}",
        )
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, attempt["attempt_id"])["state"],
            "indeterminate",
        )
        start.assert_not_called()

    def test_creation_journal_without_task_stays_indeterminate(self) -> None:
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes",
            side_effect=SchedulerError("command timed out"),
        ):
            submit_action(self.store, self.initiative_id, document)
        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        control = mock.Mock()
        from lib.control.store import StoreError as ControlStoreError

        control.peek.side_effect = ControlStoreError(f"task not found: {attempt['task_id']}")
        journal_store = mock.Mock()
        journal_store.read.return_value = {"phase": "launch-attempted"}
        with mock.patch(
            "lib.control.orchestration.actions.TaskStore", return_value=control,
        ), mock.patch(
            "lib.control.orchestration.actions.CreationJournalStore",
            return_value=journal_store,
        ):
            result = reconcile_actions(self.store, self.initiative_id)["actions"][0]
        self.assertEqual(result["state"], "indeterminate")
        self.assertEqual(
            json.loads(result["outcome"])["remediation"],
            f"asha task recover {attempt['task_id']}",
        )

    def test_resume_refuses_live_identity_conflict(self) -> None:
        self.dispatch_one()
        pause = build_action_document(self.initiative(), "pause", {})
        submit_action(self.store, self.initiative_id, pause)
        changed_task = copy.deepcopy(self.last_control_task)
        changed_task["jj"]["change_id"] = "l" * 32
        control = mock.Mock()
        control.list.return_value = [changed_task]
        control.peek.return_value = changed_task
        resume = build_action_document(self.initiative(), "resume", {})
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.actions.TaskStore", return_value=control,
        ), mock.patch(
            "lib.control.orchestration.reconcile.TaskStore", return_value=control,
        ):
            action = submit_action(self.store, self.initiative_id, resume)
        self.assertEqual(action["state"], "refused")
        self.assertEqual(
            json.loads(action["outcome"])["reason"],
            "resume requires a clean live reconciliation",
        )
        self.assertEqual(self.initiative()["state"], "paused")
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id)[0]["state"], "stale",
        )

    def test_resume_reconciles_crash_before_link_without_staling_attempt(self) -> None:
        class SimulatedDeath(BaseException):
            pass

        calls = []
        first_payload = None

        def capture(argv, **_kwargs):
            nonlocal first_payload
            calls.append(list(argv))
            if first_payload is None:
                first_payload = self.control_payload(argv)
            payload = copy.deepcopy(first_payload)
            payload["existing"] = len(calls) > 1
            return 0, json.dumps(payload).encode(), b""

        real_save_link = self.store.save_link
        writes = 0

        def die_once(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 1:
                raise SimulatedDeath
            return real_save_link(*args, **kwargs)

        dispatch = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ), mock.patch.object(
            self.store, "save_link", side_effect=die_once,
        ):
            with self.assertRaises(SimulatedDeath):
                submit_action(self.store, self.initiative_id, dispatch)
            submit_action(
                self.store, self.initiative_id,
                build_action_document(self.initiative(), "pause", {}),
            )
            control = mock.Mock()
            control.peek.return_value = first_payload["task"]
            control.list.return_value = [first_payload["task"]]
            resume = build_action_document(self.initiative(), "resume", {})
            with mock.patch(
                "lib.control.orchestration.actions.TaskStore", return_value=control,
            ), mock.patch(
                "lib.control.orchestration.reconcile.TaskStore", return_value=control,
            ), mock.patch(
                "lib.control.orchestration.reconcile.reconcile_task",
                return_value={"state": "working", "blocker": None, "evidence": []},
            ):
                resumed = submit_action(self.store, self.initiative_id, resume)
        self.assertEqual(resumed["state"], "completed", resumed["outcome"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.store.list_attempts_snapshot(self.initiative_id)), 1)
        self.assertEqual(len(self.store.list_links_snapshot(self.initiative_id)), 1)
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id)[0]["state"], "running",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"], "running",
        )

    def test_stop_attempt_uses_control_graceful_stop_argv(self) -> None:
        attempt = self.dispatch_one()
        document = build_action_document(
            self.initiative(), "stop-attempt", {"attempt_id": attempt["attempt_id"]},
        )
        calls = []

        def stop(argv, **_kwargs):
            calls.append(argv)
            return 0, b"", b""

        with mock.patch(
            "lib.control.orchestration.actions.capture_bytes", side_effect=stop,
        ), mock.patch(
            "lib.control.orchestration.actions.TaskStore",
        ) as control_store:
            control_store.return_value.peek.return_value = self.last_control_task
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "completed")
        self.assertEqual(calls[0][1:3], ["task", "stop"])
        self.assertEqual(calls[0][-1], attempt["task_id"])
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, attempt["attempt_id"])["state"],
            "cancelled",
        )

    def test_stop_attempt_releases_its_node_and_leaves_it_dispatchable(self) -> None:
        """The live stranding shape: a coordinator stops a running attempt.

        Before the fix the attempt went terminal while the node stayed
        `running`, and `reconcile_live` never acts on a `cancelled` attempt, so
        no coordinator verb could recover the node.  The stop is submitted
        through the live generation's envelope, so the refusal path this
        regression must not silently take -- fence, generation, coordinator
        action class -- is actually executed.
        """
        self.coordinator()
        attempt = self.dispatch_one()
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, attempt["attempt_id"])["state"],
            "running",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "running",
        )
        before = len(self.store.list_events_snapshot(self.initiative_id))

        stopped = self.stop_action(attempt["attempt_id"])

        self.assertEqual(stopped["state"], "completed", stopped["outcome"])
        self.assert_coordinator_action(stopped)
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, attempt["attempt_id"])["state"],
            "cancelled",
        )
        self.assertEqual(
            self.node_state_changes("implementation-a", before),
            [("running", "evaluating"), ("evaluating", "ready")],
        )
        node = self.store.read_node(self.initiative_id, "implementation-a")
        self.assertEqual(node["state"], "ready")
        self.assertEqual(
            readiness(self.store, self.initiative())["implementation-a"], "ready",
        )

        redispatched = self.dispatch_action(coordinator=True)

        self.assertEqual(redispatched["state"], "completed", redispatched["outcome"])
        self.assert_coordinator_action(redispatched)
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "running",
        )
        states = sorted(
            item["state"]
            for item in self.store.list_attempts_snapshot(self.initiative_id)
            if item["node_id"] == "implementation-a"
        )
        self.assertEqual(states, ["cancelled", "running"])

    def test_stop_attempt_from_a_fenced_generation_is_refused_before_any_effect(
        self,
    ) -> None:
        """The envelope is load-bearing: a stale generation may not stop anything.

        This is the case that proves the two release regressions are not merely
        green because their document skipped `_coordinator_fence`.  The same
        stop, resubmitted from the live generation, then succeeds.
        """
        stale = self.coordinator()
        attempt = self.dispatch_one()
        successor = claim(
            self.store, self.initiative(),
            env={**self.env, "TMUX_PANE": "%8"},
            tmux=FakeTmux(pane_id="%8", pane_pid=os.getppid()),
        )
        self.assertEqual(successor["generation"], stale["generation"] + 1)
        before = len(self.store.list_events_snapshot(self.initiative_id))

        refused = self.stop_action(attempt["attempt_id"], record=stale)

        self.assertEqual(refused["state"], "refused")
        self.assertIn(
            f"coordinator generation {stale['generation']} is fenced", refused["outcome"],
        )
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, attempt["attempt_id"])["state"],
            "running",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "running",
        )
        self.assertEqual(self.node_state_changes("implementation-a", before), [])

        self._coordinator = successor
        accepted = self.stop_action(attempt["attempt_id"])

        self.assertEqual(accepted["state"], "completed", accepted["outcome"])
        self.assertEqual(accepted["coordinator_generation"], successor["generation"])
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )

    def test_stop_attempt_charges_neither_failure_breaker_nor_retry_budget(self) -> None:
        self.coordinator()
        attempt = self.dispatch_one()
        before = len(self.store.list_events_snapshot(self.initiative_id))

        self.stop_action(attempt["attempt_id"])

        node_attempts = [
            item for item in self.store.list_attempts_snapshot(self.initiative_id)
            if item["node_id"] == "implementation-a"
        ]
        self.assertEqual(len(node_attempts), 1)
        self.assertEqual(
            consecutive_failures(
                self.store.list_attempts_snapshot(self.initiative_id),
            ),
            0,
        )
        self.assertEqual(self.initiative()["state"], "running")
        added = self.store.list_events_snapshot(self.initiative_id)[before:]
        self.assertEqual(
            sorted({item["type"] for item in added}),
            ["action-received", "node-state-changed"],
        )
        self.assertEqual(
            sorted({
                (item["actor_kind"], item["actor_id"]) for item in added
                if item["type"] == "node-state-changed"
            }),
            [("controller", "action-broker")],
        )

    def assert_stop_leaves_node(self, node_state: str) -> None:
        self.coordinator()
        attempt = self.dispatch_one()
        node = self.store.read_node(self.initiative_id, "implementation-a")
        moved = copy.deepcopy(node)
        moved["state"] = node_state
        self.store.save_node(
            self.initiative_id, moved, expected_digest=record_digest(node),
        )
        before = len(self.store.list_events_snapshot(self.initiative_id))

        stopped = self.stop_action(attempt["attempt_id"])

        self.assertEqual(stopped["state"], "completed", stopped["outcome"])
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, attempt["attempt_id"])["state"],
            "cancelled",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            node_state,
        )
        self.assertEqual(self.node_state_changes("implementation-a", before), [])

    def test_stop_attempt_leaves_a_terminal_node_untouched(self) -> None:
        self.assert_stop_leaves_node("stale")

    def test_stop_attempt_leaves_a_needs_input_node_to_continue_node(self) -> None:
        self.assert_stop_leaves_node("needs-input")

    def test_interrupted_stop_reconciles_attempt_and_node_together(self) -> None:
        """The second call site: a stop whose command never returned.

        Fixing only `_stop_attempt` ships half the fix, so this shape submits
        the same real coordinator envelope and then completes through
        `reconcile_actions`.
        """
        self.coordinator()
        attempt = self.dispatch_one()
        document = self.coordinator_document(
            "stop-attempt", {"attempt_id": attempt["attempt_id"]},
        )

        action = self.submit_stop(
            document, capture={"side_effect": ActionError("command timed out")},
        )

        self.assertEqual(action["state"], "indeterminate")
        self.assert_coordinator_action(action)
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "running",
        )
        before = len(self.store.list_events_snapshot(self.initiative_id))

        reconciled = self.reconcile_stop()

        self.assertEqual(reconciled["state"], "completed", reconciled["outcome"])
        self.assert_coordinator_action(reconciled)
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, attempt["attempt_id"])["state"],
            "cancelled",
        )
        self.assertEqual(
            self.node_state_changes("implementation-a", before),
            [("running", "evaluating"), ("evaluating", "ready")],
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )

        redispatched = self.dispatch_action(coordinator=True)

        self.assertEqual(redispatched["state"], "completed", redispatched["outcome"])
        self.assert_coordinator_action(redispatched)
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "running",
        )
        self.assertEqual(
            sorted(
                item["state"]
                for item in self.store.list_attempts_snapshot(self.initiative_id)
                if item["node_id"] == "implementation-a"
            ),
            ["cancelled", "running"],
        )

    def failing_second_node_write(self):
        """Persist the first release edge, then fail exactly like a dying process."""
        real = self.store.save_node
        writes = []

        def save_node(initiative_id, record, **kwargs):
            writes.append(record["state"])
            if len(writes) > 1:
                raise OSError("injected node write failure")
            return real(initiative_id, record, **kwargs)

        return mock.patch.object(self.store, "save_node", side_effect=save_node)

    def test_release_interrupted_between_its_two_writes_still_reaches_ready(self) -> None:
        """The walk is two persisted writes, so it must be restartable.

        `dispatching`/`running` -> `ready` has no single edge, so a failure
        after `evaluating` is persisted leaves a cancelled attempt on an
        `evaluating` node.  That pairing is produced by nothing but this walk,
        so `reconcile_actions` finishes it instead of treating the node as an
        interrupted seal's and stranding it permanently.
        """
        self.coordinator()
        attempt = self.dispatch_one()
        document = self.coordinator_document(
            "stop-attempt", {"attempt_id": attempt["attempt_id"]},
        )
        with self.failing_second_node_write():
            action = self.submit_stop(document)

        self.assertEqual(action["state"], "indeterminate")
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, attempt["attempt_id"])["state"],
            "cancelled",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "evaluating",
        )
        before = len(self.store.list_events_snapshot(self.initiative_id))

        reconciled = self.reconcile_stop()

        self.assertEqual(reconciled["state"], "completed", reconciled["outcome"])
        self.assertEqual(
            self.node_state_changes("implementation-a", before),
            [("evaluating", "ready")],
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )

        redispatched = self.dispatch_action(coordinator=True)

        self.assertEqual(redispatched["state"], "completed", redispatched["outcome"])
        self.assertEqual(
            sorted(
                item["state"]
                for item in self.store.list_attempts_snapshot(self.initiative_id)
                if item["node_id"] == "implementation-a"
            ),
            ["cancelled", "running"],
        )

    def test_release_does_not_seize_an_evaluating_node_it_did_not_write(self) -> None:
        """The resume is keyed on the newest attempt, not on `evaluating` alone.

        A node evaluating a seal from a newer attempt keeps that evaluation
        even when an older attempt of the same node is stopped.
        """
        self.coordinator()
        attempt = self.dispatch_one()
        indeterminate = copy.deepcopy(attempt)
        indeterminate.update({"state": "indeterminate", "updated_at": now_text()})
        self.store.save_attempt(
            self.initiative_id, indeterminate, expected_digest=record_digest(attempt),
        )
        newer = copy.deepcopy(indeterminate)
        newer.update({
            "attempt_id": str(uuid.uuid4()),
            "task_id": str(uuid.uuid4()),
            "action_id": None,
            "ordinal": attempt["ordinal"] + 1,
            "state": "allocated",
            "created_at": now_text(),
            "updated_at": now_text(),
        })
        self.store.save_attempt(self.initiative_id, newer)
        node = self.store.read_node(self.initiative_id, "implementation-a")
        evaluating = copy.deepcopy(node)
        evaluating["state"] = "evaluating"
        self.store.save_node(
            self.initiative_id, evaluating, expected_digest=record_digest(node),
        )
        before = len(self.store.list_events_snapshot(self.initiative_id))

        stopped = self.stop_action(attempt["attempt_id"])

        self.assertEqual(stopped["state"], "completed", stopped["outcome"])
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, attempt["attempt_id"])["state"],
            "cancelled",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "evaluating",
        )
        self.assertEqual(self.node_state_changes("implementation-a", before), [])

    def test_cancel_node_stops_live_attempt_then_cancels_both(self) -> None:
        attempt = self.dispatch_one()
        document = build_action_document(
            self.initiative(), "cancel-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.actions.capture_bytes",
            return_value=(0, b"", b""),
        ), mock.patch(
            "lib.control.orchestration.actions.TaskStore",
        ) as control_store:
            control_store.return_value.peek.return_value = self.last_control_task
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "completed")
        self.assertEqual(
            self.store.read_attempt(self.initiative_id, attempt["attempt_id"])["state"],
            "cancelled",
        )
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "cancelled",
        )

    def test_terminal_node_cancel_and_allocated_attempt_stop_refuse_without_control(self) -> None:
        node = self.store.read_node(self.initiative_id, "implementation-a")
        evaluating = copy.deepcopy(node)
        evaluating["state"] = "evaluating"
        self.store.save_node(
            self.initiative_id, evaluating, expected_digest=record_digest(node),
        )
        failed = copy.deepcopy(evaluating)
        failed["state"] = "failed"
        self.store.save_node(
            self.initiative_id, failed, expected_digest=record_digest(evaluating),
        )
        calls = mock.Mock()
        cancel = build_action_document(
            self.initiative(), "cancel-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.actions.capture_bytes", calls,
        ):
            cancelled = submit_action(self.store, self.initiative_id, cancel)
        self.assertEqual(cancelled["state"], "refused")
        calls.assert_not_called()

        node = self.store.read_node(self.initiative_id, "review-a")
        attempt = {
            "contract": ATTEMPT_CONTRACT,
            "attempt_id": str(uuid.uuid4()),
            "initiative_id": self.initiative_id,
            "node_id": node["node_id"],
            "task_id": str(uuid.uuid4()),
            "action_id": None,
            "ordinal": 1,
            "base": copy.deepcopy(node["base"] or self.plan["nodes"][0]["base"]),
            "state": "allocated",
            "result_publication_id": None,
            "result_id": None,
            "seal_id": None,
            "created_at": now_text(),
            "updated_at": now_text(),
        }
        attempt["updated_at"] = attempt["created_at"]
        self.store.save_attempt(self.initiative_id, attempt)
        stop = build_action_document(
            self.initiative(), "stop-attempt", {"attempt_id": attempt["attempt_id"]},
        )
        calls.reset_mock()
        with mock.patch(
            "lib.control.orchestration.actions.capture_bytes", calls,
        ):
            stopped = submit_action(self.store, self.initiative_id, stop)
        self.assertEqual(stopped["state"], "refused")
        calls.assert_not_called()

    def test_stop_refuses_control_task_already_observed_exited_without_call(self) -> None:
        attempt = self.dispatch_one()
        ended = copy.deepcopy(self.last_control_task)
        ended["lifecycle"] = "ended"
        ended["runs"][0]["state"] = "exited"
        control = mock.Mock()
        control.peek.return_value = ended
        stop = build_action_document(
            self.initiative(), "stop-attempt", {"attempt_id": attempt["attempt_id"]},
        )
        capture = mock.Mock()
        with mock.patch(
            "lib.control.orchestration.actions.TaskStore", return_value=control,
        ), mock.patch(
            "lib.control.orchestration.actions.capture_bytes", capture,
        ):
            action = submit_action(self.store, self.initiative_id, stop)
        self.assertEqual(action["state"], "refused")
        capture.assert_not_called()


class OrchestrationActivationTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def test_activation_runs_full_handshake_and_refreshes_readiness(self) -> None:
        document = build_action_document(
            self.initiative(), "activate-initiative", {},
        )
        jj = mock.Mock()
        jj.preflight.return_value = RepositoryFacts(
            root=self.repo, git_root=self.repo / ".git",
        )
        doctor = {
            "contract": "asha.orchestration-doctor.v1",
            "ok": True,
            "probes": [],
            "limitations": [],
        }
        with mock.patch(
            "lib.control.orchestration.actions.run_orchestration_doctor",
            return_value=doctor,
        ), mock.patch(
            "lib.control.orchestration.actions.JjAdapter", return_value=jj,
        ), mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "completed")
        self.assertEqual(self.initiative()["state"], "running")
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "ready",
        )

    def test_activation_refuses_goal_capacity_before_runtime_handshake(self) -> None:
        document = build_action_document(
            self.initiative(), "activate-initiative", {},
        )
        doctor = mock.Mock()
        with mock.patch(
            "lib.control.orchestration.scheduler.validate_goal_capacity",
            side_effect=SchedulerError("absolute assignment path exceeds goal limit"),
        ), mock.patch(
            "lib.control.orchestration.actions.run_orchestration_doctor", doctor,
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "refused")
        self.assertIn("assignment path", action["outcome"])
        self.assertEqual(self.initiative()["state"], "approved")
        doctor.assert_not_called()

    def test_activation_refuses_identity_drift(self) -> None:
        document = build_action_document(
            self.initiative(), "activate-initiative", {},
        )
        jj = mock.Mock()
        jj.preflight.return_value = RepositoryFacts(
            root=self.repo, git_root=self.repo / ".git",
        )
        with mock.patch(
            "lib.control.orchestration.actions.run_orchestration_doctor",
            return_value={
                "contract": "asha.orchestration-doctor.v1", "ok": True,
                "probes": [], "limitations": [],
            },
        ), mock.patch(
            "lib.control.orchestration.actions.JjAdapter", return_value=jj,
        ), mock.patch(
            "lib.control.orchestration.actions.derive_repository_identity",
            return_value=("repo:changed", "changed"),
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "refused")
        self.assertEqual(self.initiative()["state"], "approved")


class OrchestrationStoppedReviewNodeTests(
    CoordinatorEnvelope, ExecutionFixture, unittest.TestCase
):
    """A stopped review attempt can never settle the review it was running.

    Prevention releases the review node to `ready`, which makes it immediately
    re-dispatchable, so the retirement the recovery sweep performs must happen
    at the stop sites as well.  Without it a redispatch registers a second
    `running` review for the same target beside the first.  Both shapes submit
    the stop through the live coordinator generation's envelope for the same
    reason the implementation-node regressions do.
    """

    def setUp(self) -> None:
        super().setUp()
        self.candidate = save_candidate(self)
        advance_node(self, "implementation-a", ["evaluating", "succeeded"])
        refresh_readiness(self.store, self.initiative_id)
        self.dispatch_review()
        self.coordinator()

    def dispatch_review(self):
        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            payload["task"]["jj"].update({
                "base_commit_id": argv[argv.index("--base") + 1],
                "working_commit_id": "f" * 40,
            })
            self.last_control_task = payload["task"]
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

    def latest_review_attempt(self):
        return max(
            (
                item for item in self.store.list_attempts_snapshot(self.initiative_id)
                if item["node_id"] == "review-a"
            ),
            key=lambda item: (item["ordinal"], item["attempt_id"]),
        )

    def review_for(self, attempt_id):
        return next(
            item for item in self.store.list_reviews_snapshot(self.initiative_id)
            if item["attempt_id"] == attempt_id
        )

    def assert_retired_then_redispatchable(self, attempt, review):
        retired = self.store.read_review(self.initiative_id, review["review_id"])
        self.assertEqual(retired["state"], "stale")
        self.assertIsNone(retired["verdict"])
        self.assertEqual(retired["findings"], [])
        self.assertEqual(
            self.store.read_node(self.initiative_id, "review-a")["state"], "ready",
        )

        redispatched = self.dispatch_review()

        self.assertEqual(redispatched["state"], "completed", redispatched["outcome"])
        fresh = self.latest_review_attempt()
        self.assertNotEqual(fresh["attempt_id"], attempt["attempt_id"])
        self.assertEqual(self.review_for(fresh["attempt_id"])["state"], "running")
        self.assertEqual(
            sorted(
                item["state"]
                for item in self.store.list_reviews_snapshot(self.initiative_id)
            ),
            ["running", "stale"],
        )

    def submit_stop(self, document, *, capture=None):
        with mock.patch(
            "lib.control.orchestration.actions.capture_bytes",
            **(capture or {"return_value": (0, b"", b"")}),
        ), mock.patch(
            "lib.control.orchestration.actions.TaskStore",
        ) as control_store:
            control_store.return_value.peek.return_value = self.last_control_task
            return submit_action(self.store, self.initiative_id, document)

    def test_stop_retires_the_review_its_attempt_can_no_longer_settle(self) -> None:
        attempt = self.latest_review_attempt()
        review = self.review_for(attempt["attempt_id"])
        self.assertEqual(review["state"], "running")
        document = self.coordinator_document(
            "stop-attempt", {"attempt_id": attempt["attempt_id"]},
        )

        stopped = self.submit_stop(document)

        self.assertEqual(stopped["state"], "completed", stopped["outcome"])
        self.assert_coordinator_action(stopped)
        self.assert_retired_then_redispatchable(attempt, review)

    def test_interrupted_stop_retires_the_review_at_the_reconciled_site(self) -> None:
        attempt = self.latest_review_attempt()
        review = self.review_for(attempt["attempt_id"])
        document = self.coordinator_document(
            "stop-attempt", {"attempt_id": attempt["attempt_id"]},
        )

        interrupted = self.submit_stop(
            document, capture={"side_effect": ActionError("command timed out")},
        )

        self.assertEqual(interrupted["state"], "indeterminate")
        self.assert_coordinator_action(interrupted)
        self.assertEqual(
            self.store.read_review(self.initiative_id, review["review_id"])["state"],
            "running",
        )

        with mock.patch(
            "lib.control.orchestration.actions.TaskStore",
        ) as control_store, mock.patch(
            "lib.control.orchestration.actions.reconcile_task",
            return_value={"state": "exited", "blocker": None, "evidence": []},
        ):
            control_store.return_value.peek.return_value = self.last_control_task
            reconciled = reconcile_actions(self.store, self.initiative_id)["actions"][0]

        self.assertEqual(reconciled["state"], "completed", reconciled["outcome"])
        self.assert_retired_then_redispatchable(attempt, review)


if __name__ == "__main__":
    unittest.main()
