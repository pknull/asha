from __future__ import annotations

import copy
import json
import os
import unittest
import uuid
from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from lib.control.orchestration.actions import (
    ActionError,
    ActionRefused,
    REQUEST_DECISION_SUBJECT_GRAMMAR,
    _parse_document,
    _repair_node,
    build_action_document,
    payload_digest,
    reconcile_actions,
    submit_action,
)
from lib.control.orchestration.cli import _usage
from lib.control.orchestration.coordinator import claim
from lib.control.orchestration.scheduler import (
    SchedulerError,
    consecutive_failures,
    pause_for_breaker,
    readiness,
    refresh_readiness,
)
from lib.control.orchestration.seals import (
    prepare_and_publish_seal,
    reconcile_seal_drift,
)
from lib.control.orchestration.model import (
    ATTEMPT_CONTRACT,
    record_digest,
    unanswered_operator_question,
    validate_result,
)
from lib.control.orchestration.store import ObservationOnlyPlanError
from lib.control.store import TaskStore
from lib.control.jj import RepositoryFacts, WorkspaceIdentity
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.orchestration_increment3_fixtures import advance_node, save_candidate
from tests.python.test_orchestration_graph import seal as graph_seal
from tests.python.test_orchestration_seals import SealJj
from tests.python.test_control_config_model import task_record
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

    def test_request_decision_refuses_invalid_event_subject_before_execution(self) -> None:
        document = self.coordinator_document(
            "request-decision",
            {"subject_id": "invalid subject", "question": "Which path?"},
        )
        before = len(self.store.list_events_snapshot(self.initiative_id))

        action = submit_action(self.store, self.initiative_id, document)

        self.assertEqual(action["state"], "refused")
        self.assertNotEqual(action["state"], "indeterminate")
        self.assertIn(
            REQUEST_DECISION_SUBJECT_GRAMMAR,
            json.loads(action["outcome"])["reason"],
        )
        events = self.store.list_events_snapshot(self.initiative_id)[before:]
        self.assertFalse(any(event["type"] == "approval-requested" for event in events))
        self.assertEqual(self.initiative()["state"], "running")

    def test_request_decision_subject_boundaries_state_grammar(self) -> None:
        for subject_id in ("", "a" * 129):
            with self.subTest(length=len(subject_id)):
                document = self.coordinator_document(
                    "request-decision",
                    {"subject_id": subject_id, "question": "Which path?"},
                )
                before = len(self.store.list_events_snapshot(self.initiative_id))

                action = submit_action(self.store, self.initiative_id, document)

                self.assertEqual(action["state"], "refused")
                self.assertNotEqual(action["state"], "indeterminate")
                self.assertIn(
                    REQUEST_DECISION_SUBJECT_GRAMMAR,
                    json.loads(action["outcome"])["reason"],
                )
                events = self.store.list_events_snapshot(self.initiative_id)[before:]
                self.assertFalse(any(
                    event["type"] == "approval-requested" for event in events
                ))
                self.assertEqual(self.initiative()["state"], "running")

    def test_coordinator_help_states_request_decision_subject_grammar(self) -> None:
        output = StringIO()

        _usage(output)

        self.assertIn("request-decision", output.getvalue())
        self.assertIn(REQUEST_DECISION_SUBJECT_GRAMMAR, output.getvalue())

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


class RetainedGateCapacityTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def customize_plan(self, plan):
        plan["declared_gates"][1]["commands"][0]["argv"] = [
            "python3", "-c", "pass", *[str(i) + '"\\é' * 900 for i in range(6)],
        ]

    def setUp(self):
        # Model-valid retained plans from the old renderer may exceed today's
        # required-text cap. Permit fixture construction, not the action tested.
        with mock.patch("lib.control.orchestration.scheduler.MAX_ASSIGNMENT_BYTES", 100000):
            super().setUp()

    def test_activation_counts_actual_gate_bytes_before_runtime_handshake(self):
        before = self.initiative()
        with mock.patch("lib.control.orchestration.actions.run_orchestration_doctor") as doctor:
            action = submit_action(self.store, self.initiative_id, build_action_document(
                before, "activate-initiative", {},
            ))
        self.assertEqual(action["state"], "refused", action["outcome"])
        self.assertRegex(action["outcome"], r"implementation-a.*bytes.*32768")
        self.assertEqual(self.initiative()["state"], "approved")
        self.assertEqual(self.store.list_attempts_snapshot(self.initiative_id), [])
        doctor.assert_not_called()

    def test_dispatch_counts_actual_gate_bytes_before_any_attempt_or_task(self):
        self.set_running(self.initiative())
        node = self.store.read_node(self.initiative_id, "implementation-a")
        with mock.patch("lib.control.orchestration.scheduler.capture_bytes") as launch, mock.patch.object(
            self.store, "write_assignment",
        ) as write_assignment:
            action = submit_action(self.store, self.initiative_id, build_action_document(
                self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
            ))
        self.assertEqual(action["state"], "refused", action["outcome"])
        self.assertRegex(action["outcome"], r"implementation-a.*bytes.*32768")
        self.assertEqual(self.store.list_attempts_snapshot(self.initiative_id), [])
        self.assertEqual(self.store.read_node(self.initiative_id, node["node_id"]), node)
        launch.assert_not_called()
        write_assignment.assert_not_called()


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



class Interrupted(BaseException):
    """The controller dying at one store write: no `submit_action` handler sees it."""


class ParkingFixture(CoordinatorEnvelope, ExecutionFixture):
    """Shared U5 parking scenario: a live coordinator, its question, and the journal."""

    QUESTION = {
        "subject_id": "implementation-a",
        "question": "Which base should the retry use?",
    }

    def retained(self) -> dict:
        """Every durable record a pause must leave byte-for-byte alone."""
        iid = self.initiative_id
        return {
            "nodes": self.store.list_nodes_snapshot(iid),
            "attempts": self.store.list_attempts_snapshot(iid),
            "seals": self.store.list_seals_snapshot(iid),
            "approvals": self.store.list_approvals_snapshot(iid),
            "links": self.store.list_links_snapshot(iid),
            "coordinators": self.store.list_coordinators_snapshot(iid),
        }

    def head(self) -> dict:
        """The initiative record minus the fields every journal append moves."""
        record = self.initiative()
        return {
            key: value for key, value in record.items()
            if key not in {"state_revision", "last_event_sequence", "updated_at"}
        }

    def dispatch_worker(self) -> dict:
        """One real operator dispatch with a captured Control task."""
        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            self.last_control_task = payload["task"]
            return 0, json.dumps(payload).encode(), b""

        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            dispatched = submit_action(self.store, self.initiative_id, build_action_document(
                self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
            ))
        self.assertEqual(dispatched["state"], "completed", dispatched["outcome"])
        return self.store.list_attempts_snapshot(self.initiative_id)[0]

    def failure_seal(self) -> dict:
        """An immutable failure seal with no result, distinct from the held candidate."""
        failure = graph_seal(str(uuid.uuid4()), outcome="failure")
        failure.update({
            "initiative_id": self.initiative_id,
            "repository_id": self.initiative()["scope"]["repository"]["repository_id"],
            "node_id": "implementation-a",
            "sealed_at": now_text(),
            "result_id": None,
        })
        self.store.save_seal(self.initiative_id, failure)
        return failure

    def events(self) -> list:
        return self.store.list_events_snapshot(self.initiative_id)

    def question_event(self) -> dict:
        """The single durable record of the coordinator's operator question."""
        questions = [
            event for event in self.events()
            if event["type"] == "approval-requested"
            and event["payload"].get("kind") == "operator-decision"
        ]
        self.assertEqual(len(questions), 1)
        return questions[0]

    def state_changes(self, since: int = 0) -> list:
        return [
            (event["payload"].get("from"), event["payload"].get("to"))
            for event in self.events()[since:]
            if event["type"] == "initiative-state-changed"
        ]

    def outcome(self, action: dict) -> dict:
        return json.loads(action["outcome"])

    def operator(self, action_class: str) -> dict:
        return submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), action_class, {}),
        )

    def ask_the_operator(self) -> dict:
        """The live coordinator's own question moves the initiative to needs-input."""
        asked = submit_action(
            self.store, self.initiative_id,
            self.coordinator_document("request-decision", self.QUESTION),
        )
        self.assertEqual(asked["state"], "completed", asked["outcome"])
        self.assertEqual(self.initiative()["state"], "needs-input")
        return asked

    def pending_work(self) -> dict:
        """A requested approval, a pending node decision, and its held seal."""
        failure = self.failure_seal()
        requested = submit_action(self.store, self.initiative_id, build_action_document(
            self.initiative(), "request-salvage", {
                "node_id": "implementation-a",
                "failure_seal_id": failure["seal_id"],
                "plan": "Recover the bounded useful work without inheriting its base.",
            },
        ))
        self.assertEqual(requested["state"], "completed", requested["outcome"])
        advance_node(self, "implementation-a", ["dispatching", "running", "needs-input"])
        held = save_candidate(self, outcome="paused")
        approval = self.store.read_approval(
            self.initiative_id, self.outcome(requested)["request_id"],
        )
        self.assertEqual(approval["state"], "requested")
        return {"failure": failure, "held": held, "approval": approval}

    def transition_edges(self, since: int = 0) -> list:
        return [
            event for event in self.events()[since:]
            if event["type"] == "initiative-state-changed"
        ]

    def action(self, action_id: str) -> dict:
        return self.store.read_action(self.initiative_id, action_id)

    def operator_document(self, action_class: str) -> dict:
        return build_action_document(self.initiative(), action_class, {})

    def submit(self, document: dict) -> dict:
        return submit_action(self.store, self.initiative_id, document)

    @contextmanager
    def interrupted(self, method: str, *, before: bool, when):
        """Kill the controller at the exact store write `when` selects.

        The real store method still runs unless `before`; the death is a
        `BaseException`, so `submit_action` retains whatever phase the action
        was in, exactly as a killed controller would.
        """
        original = getattr(self.store, method)

        def boundary(*args, **kwargs):
            if before and when(*args, **kwargs):
                raise Interrupted(f"before {method}")
            result = original(*args, **kwargs)
            if not before and when(*args, **kwargs):
                raise Interrupted(f"after {method}")
            return result

        with mock.patch.object(self.store, method, side_effect=boundary):
            yield

    @staticmethod
    def edge_to(target: str):
        """Select the `initiative-state-changed` append into `target`."""
        return lambda _initiative_id, event: (
            event["type"] == "initiative-state-changed"
            and event["payload"].get("to") == target
        )

    @staticmethod
    def head_write_to(target: str):
        """Select the initiative head write into `target`."""
        return lambda record, **_kwargs: record["state"] == target

    def reconciled(self, action_id: str) -> dict:
        """Run the public reconciliation and return the retained action record."""
        results = reconcile_actions(self.store, self.initiative_id)["actions"]
        self.assertIn(action_id, [item["action_id"] for item in results])
        return self.action(action_id)

    def assert_recovered_outcome(self, action: dict, **expected) -> None:
        outcome = self.outcome(action)
        self.assertEqual({key: outcome.get(key) for key in expected}, expected)

    def assert_reconciliation_settled(self) -> None:
        """Repeated reconciliation changes no action, event, or head record."""
        before = (
            self.store.list_actions_snapshot(self.initiative_id),
            self.events(), self.initiative(),
        )
        reconcile_actions(self.store, self.initiative_id)
        reconcile_actions(self.store, self.initiative_id)
        self.assertEqual(
            (
                self.store.list_actions_snapshot(self.initiative_id),
                self.events(), self.initiative(),
            ),
            before,
        )


class WaitingWorkParkingTests(ParkingFixture, unittest.TestCase):
    """U5: `pause` parks running or waiting work reversibly and resolves nothing.

    Every scenario goes through `submit_action` and the journal.  The
    needs-input edge exists so the operator can park a question without
    answering it, so it is operator-only at the executor: the live generation
    that asked must not be able to park its own question, a fenced generation
    is refused before the executor exactly as before, and the running-pause
    the coordinator already had stays supported.
    """

    def test_operator_parks_waiting_work_with_the_actual_source_state(self) -> None:
        pending = self.pending_work()
        self.ask_the_operator()
        before = self.retained()
        head = self.head()
        since = len(self.events())

        parked = self.operator("pause")

        self.assertEqual(parked["state"], "completed", parked["outcome"])
        self.assertEqual(parked["actor_kind"], "operator")
        outcome = self.outcome(parked)
        self.assertEqual(
            (outcome["status"], outcome["already_paused"], outcome["paused_from"]),
            ("paused", False, "needs-input"),
        )
        self.assertEqual(self.head(), {**head, "state": "paused"})
        self.assertEqual(self.state_changes(since), [("needs-input", "paused")])
        edge = next(
            event for event in self.events()[since:]
            if event["type"] == "initiative-state-changed"
        )
        self.assertEqual(
            (edge["actor_kind"], edge["actor_id"], edge["subject_ids"]),
            ("controller", "action-broker", [self.initiative_id, parked["action_id"]]),
        )
        # Parking resolves nothing: the decision, its held seal, the requested
        # approval and the coordinator that asked are exactly as they were.
        self.assertEqual(self.retained(), before)
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "needs-input",
        )
        self.assertEqual(
            self.store.read_seal(self.initiative_id, pending["held"]["seal_id"])["outcome"],
            "paused",
        )
        self.assertEqual(
            self.store.read_approval(self.initiative_id, pending["approval"]["request_id"]),
            pending["approval"],
        )

        # Pausing parked work is idempotent and journals no second edge.
        since = len(self.events())
        again = self.operator("pause")
        self.assertEqual(again["state"], "completed", again["outcome"])
        self.assertTrue(self.outcome(again)["already_paused"])
        self.assertEqual(self.state_changes(since), [])
        self.assertEqual(self.head(), {**head, "state": "paused"})
        self.assertEqual(self.retained(), before)

    def test_the_live_coordinator_still_parks_running_work(self) -> None:
        self.coordinator()
        before = self.retained()
        since = len(self.events())

        parked = submit_action(
            self.store, self.initiative_id, self.coordinator_document("pause", {}),
        )

        self.assert_coordinator_action(parked)
        self.assertEqual(parked["state"], "completed", parked["outcome"])
        self.assertEqual(self.outcome(parked)["paused_from"], "running")
        self.assertEqual(self.initiative()["state"], "paused")
        self.assertEqual(self.state_changes(since), [("running", "paused")])
        self.assertEqual(self.retained(), before)

    def test_the_live_coordinator_cannot_park_the_question_it_asked(self) -> None:
        self.ask_the_operator()
        before = self.retained()
        head = self.head()
        since = len(self.events())

        refused = submit_action(
            self.store, self.initiative_id, self.coordinator_document("pause", {}),
        )

        # The envelope is the live generation and `pause` is a coordinator
        # class, so this passed the fence; the executor owns the refusal.
        self.assert_coordinator_action(refused)
        self.assertEqual(refused["state"], "refused")
        self.assertEqual(
            self.outcome(refused)["reason"],
            "only the operator may park a needs-input initiative",
        )
        self.assertEqual(self.head(), head)
        self.assertEqual(self.initiative()["state"], "needs-input")
        self.assertEqual(self.state_changes(since), [])
        self.assertEqual(
            [event["type"] for event in self.events()[since:]
             if event["type"] == "action-refused"],
            ["action-refused"],
        )
        self.assertEqual(self.retained(), before)

    def test_a_fenced_generation_is_refused_before_the_executor(self) -> None:
        self.ask_the_operator()
        stale = self.coordinator()
        other = FakeTmux(pane_id="%8", pane_pid=os.getppid())
        current = claim(
            self.store, self.initiative(),
            env={**self.env, "TMUX_PANE": "%8"}, tmux=other,
        )
        self.assertEqual(current["generation"], 2)
        before = self.retained()
        since = len(self.events())

        fenced = submit_action(
            self.store, self.initiative_id,
            self.coordinator_document("pause", {}, record=stale),
        )
        self.assertEqual(fenced["state"], "refused")
        self.assertEqual(
            self.outcome(fenced)["reason"],
            "coordinator generation 1 is fenced; current generation is 2",
        )
        live = submit_action(
            self.store, self.initiative_id,
            self.coordinator_document("pause", {}, record=current),
        )
        self.assertEqual(live["state"], "refused")
        self.assertEqual(
            self.outcome(live)["reason"],
            "only the operator may park a needs-input initiative",
        )
        self.assertEqual(self.initiative()["state"], "needs-input")
        self.assertEqual(self.state_changes(since), [])
        self.assertEqual(self.retained(), before)

        parked = self.operator("pause")
        self.assertEqual(parked["state"], "completed", parked["outcome"])
        self.assertEqual(self.state_changes(since), [("needs-input", "paused")])

    def test_resume_after_parking_waiting_work_still_refuses_a_live_conflict(self) -> None:
        self.dispatch_worker()
        self.ask_the_operator()
        parked = self.operator("pause")
        self.assertEqual(parked["state"], "completed", parked["outcome"])
        self.assertEqual(self.outcome(parked)["paused_from"], "needs-input")
        changed_task = copy.deepcopy(self.last_control_task)
        changed_task["jj"]["change_id"] = "l" * 32
        control = mock.Mock()
        control.list.return_value = [changed_task]
        control.peek.return_value = changed_task
        since = len(self.events())

        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.actions.TaskStore", return_value=control,
        ), mock.patch(
            "lib.control.orchestration.reconcile.TaskStore", return_value=control,
        ):
            resumed = self.operator("resume")

        self.assertEqual(resumed["state"], "refused")
        self.assertEqual(
            self.outcome(resumed)["reason"],
            "resume requires a clean live reconciliation",
        )
        self.assertEqual(self.initiative()["state"], "paused")
        self.assertEqual(self.state_changes(since), [])

    def test_resume_returns_parked_waiting_work_to_running_without_clearing_it(self) -> None:
        pending = self.pending_work()
        self.ask_the_operator()
        question = self.question_event()
        parked = self.operator("pause")
        self.assertEqual(parked["state"], "completed", parked["outcome"])
        before = self.retained()
        since = len(self.events())
        # Resume's live reconciliation re-reads every sealed workspace. The
        # fixture seals name one Control task; present it, at exactly the
        # sealed commit, so the only question left is what resume changes.
        sealed_task = task_record(
            task_id=pending["held"]["task_id"], repository_root=str(self.repo),
            workspace_path=str(self.root / "workspaces" / "sealed"),
        )
        control = mock.Mock()
        control.peek.return_value = sealed_task
        control.list.return_value = []
        jj = mock.Mock()
        jj.inspect_workspace.return_value = SimpleNamespace(
            commit_id=pending["held"]["jj_commit_id"],
        )

        with mock.patch(
            "lib.control.orchestration.reconcile.TaskStore", return_value=control,
        ), mock.patch(
            "lib.control.orchestration.seals.JjAdapter", return_value=jj,
        ):
            resumed = self.operator("resume")
            # The park began in needs-input with the coordinator's question
            # unanswered, so resume restores that wait instead of running
            # past it; the node decision beneath it is untouched either way.
            self.assertEqual(resumed["state"], "completed", resumed["outcome"])
            self.assertEqual(self.outcome(resumed)["status"], "needs-input")
            self.assertEqual(self.initiative()["state"], "needs-input")
            self.assertEqual(self.state_changes(since), [("paused", "needs-input")])
            self.assertEqual(self.retained(), before)
            self.assertEqual(self.question_event(), question)
            answered = self.operator("resume")

        self.assertEqual(answered["state"], "completed", answered["outcome"])
        self.assertEqual(self.initiative()["state"], "running")
        self.assertEqual(
            self.state_changes(since), [("paused", "needs-input"), ("needs-input", "running")],
        )
        # Nothing was cleared on the way through: the decision, its held seal
        # and the requested approval are the same records that were parked.
        self.assertEqual(self.retained(), before)
        self.assertEqual(
            self.store.read_node(self.initiative_id, "implementation-a")["state"],
            "needs-input",
        )
        self.assertEqual(
            self.store.read_seal(self.initiative_id, pending["held"]["seal_id"])["outcome"],
            "paused",
        )
        self.assertEqual(
            self.store.read_approval(
                self.initiative_id, pending["approval"]["request_id"],
            )["state"],
            "requested",
        )

    def test_resume_restores_a_parked_operator_question_without_answering_it(self) -> None:
        """An initiative-only question (no node or approval demand) survives parking as attention."""
        self.ask_the_operator()
        question = self.question_event()
        self.assertEqual(
            [node["state"] for node in self.store.list_nodes_snapshot(self.initiative_id)],
            ["ready", "blocked", "blocked"],
            "nothing but the question waits on the operator",
        )
        self.assertEqual(
            [item for item in self.store.list_approvals_snapshot(self.initiative_id)
             if item["state"] == "requested"],
            [],
        )
        before = self.retained()
        head = self.head()
        parked = self.operator("pause")
        self.assertEqual(self.outcome(parked)["paused_from"], "needs-input")
        self.assertEqual(self.head(), {**head, "state": "paused"})
        since = len(self.events())

        resumed = self.operator("resume")

        self.assertEqual(resumed["state"], "completed", resumed["outcome"])
        self.assertEqual(resumed["actor_kind"], "operator")
        outcome = self.outcome(resumed)
        self.assertEqual(
            (outcome["status"], outcome["already_running"]), ("needs-input", False),
        )
        self.assertEqual(self.head(), {**head, "state": "needs-input"})
        self.assertEqual(self.state_changes(since), [("paused", "needs-input")])
        edge = next(
            event for event in self.events()[since:]
            if event["type"] == "initiative-state-changed"
        )
        self.assertEqual(
            edge["payload"],
            {"from": "paused", "to": "needs-input",
             "restored_question_event_id": question["event_id"]},
        )
        self.assertEqual(
            (edge["actor_kind"], edge["actor_id"], edge["subject_ids"]),
            ("controller", "action-broker", [self.initiative_id, resumed["action_id"]]),
        )
        # Restoration is a state, not a record: the question event is the
        # same bytes, no second question was journaled, and every other
        # record is exactly as parked.
        self.assertEqual(self.question_event(), question)
        self.assertEqual(self.retained(), before)
        self.assertEqual(
            [node["state"] for node in self.store.list_nodes_snapshot(self.initiative_id)],
            ["ready", "blocked", "blocked"],
        )

        # The restored question is answered the way it always was: resume
        # from needs-input is the operator's answer and runs readiness again.
        since = len(self.events())
        answered = self.operator("resume")
        self.assertEqual(answered["state"], "completed", answered["outcome"])
        self.assertEqual(self.outcome(answered)["status"], "running")
        self.assertEqual(self.state_changes(since), [("needs-input", "running")])
        self.assertEqual(self.question_event(), question)

    def test_resume_never_resurrects_a_question_answered_before_parking(self) -> None:
        self.ask_the_operator()
        question = self.question_event()
        answered = self.operator("resume")
        self.assertEqual(answered["state"], "completed", answered["outcome"])
        self.assertEqual(self.initiative()["state"], "running")
        before = self.retained()
        since = len(self.events())

        parked = self.operator("pause")
        self.assertEqual(self.outcome(parked)["paused_from"], "running")
        resumed = self.operator("resume")

        self.assertEqual(resumed["state"], "completed", resumed["outcome"])
        self.assertEqual(self.outcome(resumed)["status"], "running")
        self.assertEqual(self.initiative()["state"], "running")
        self.assertEqual(
            self.state_changes(since), [("running", "paused"), ("paused", "running")],
        )
        self.assertNotIn(
            "restored_question_event_id",
            [key for event in self.events()[since:] for key in event["payload"]],
        )
        self.assertEqual(self.question_event(), question)
        self.assertEqual(self.retained(), before)

    def test_resume_returns_a_parked_wait_without_a_question_to_running(self) -> None:
        """A needs-input head with no question record (the paused-seal path) is not restored."""
        initiative = self.initiative()
        waiting = copy.deepcopy(initiative)
        waiting.update({
            "state": "needs-input",
            "state_revision": initiative["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(waiting, expected_digest=record_digest(initiative))
        self.assertEqual(
            [event for event in self.events()
             if event["type"] == "approval-requested"], [],
        )
        before = self.retained()
        since = len(self.events())

        parked = self.operator("pause")
        self.assertEqual(self.outcome(parked)["paused_from"], "needs-input")
        resumed = self.operator("resume")

        self.assertEqual(resumed["state"], "completed", resumed["outcome"])
        self.assertEqual(self.outcome(resumed)["status"], "running")
        self.assertEqual(
            self.state_changes(since), [("needs-input", "paused"), ("paused", "running")],
        )
        self.assertEqual(self.retained(), before)


class InterruptedParkingRecoveryTests(ParkingFixture, unittest.TestCase):
    """U5 corrective continuation: a pause or resume the controller dies inside.

    Every scenario kills the controller at one exact store write through the
    public `submit_action`, then recovers through `reconcile_actions` or the
    next operator action.  Recovery is decided by the action's own durable
    proof (the head it observed, the head writes since, and the edge bound to
    it), never by whatever state the head happens to hold.  The first scenario
    is the chair's independent fixture (a pause interrupted after its paused
    head write, before its park edge); the restoration scenarios are the
    accepted review finding (a restored resume interrupted after its writes).
    """

    # The exact outcome keys an uninterrupted completion carries; anchored to
    # real completions in `test_recovered_completions_carry_exactly_what_an_
    # uninterrupted_completion_carries`.  A recovery adds `reconciled` and
    # nothing else: the interruption's transient `status`/`reason` are gone.
    PAUSED_FROM_RUNNING_KEYS = frozenset({
        "payload", "status", "pause_from", "pause_from_revision",
        "pause_from_sequence", "already_paused", "paused_from",
    })
    PAUSED_FROM_NEEDS_INPUT_KEYS = PAUSED_FROM_RUNNING_KEYS | {"parked_question_event_id"}
    ALREADY_PAUSED_KEYS = frozenset({
        "payload", "status", "pause_from", "pause_from_revision",
        "pause_from_sequence", "already_paused",
    })
    RESUMED_KEYS = frozenset({
        "payload", "status", "resume_from", "resume_from_revision",
        "resume_from_sequence", "resume_to", "restored_question_event_id",
        "already_running",
    })
    ALREADY_RUNNING_KEYS = frozenset({
        "payload", "status", "resume_from", "resume_from_revision",
        "resume_from_sequence", "already_running",
    })

    def assert_recovered_completion(self, action: dict, keys: frozenset, **expected) -> None:
        """A recovered completion is the proof, its result and `reconciled`: no more."""
        self.assertEqual(action["state"], "completed", action["outcome"])
        self.assertEqual(set(self.outcome(action)), set(keys) | {"reconciled"})
        self.assert_recovered_outcome(action, **expected)

    def interrupted_running_pause(self, *, before_head_write: bool) -> dict:
        """The live coordinator's pause of running work, dead at one exact write."""
        pause = self.coordinator_document("pause", {})
        if before_head_write:
            boundary = self.interrupted(
                "save_initiative", before=True, when=self.head_write_to("paused"),
            )
        else:
            boundary = self.interrupted(
                "append_event", before=True, when=self.edge_to("paused"),
            )
        with boundary:
            with self.assertRaises(Interrupted):
                self.submit(pause)
        crashed = self.action(pause["action_id"])
        self.assert_coordinator_action(crashed)
        self.assertEqual(crashed["state"], "dispatching")
        self.assert_recovered_outcome(crashed, pause_from="running")
        self.assertEqual(
            self.initiative()["state"], "running" if before_head_write else "paused",
        )
        return pause

    def breaker_park(self, event_type: str):
        """The real scheduler breaker on one route, as its callers invoke it."""
        def park() -> None:
            pause_for_breaker(
                self.store, self.initiative_id, f"{event_type} raised in test",
                event_type=event_type, subject_ids=["implementation-a"],
            )
        return park

    def seal_drift_park(self) -> None:
        """The real seal-drift reconciler over a seal whose task Control cannot find."""
        self.failure_seal()
        findings = reconcile_seal_drift(
            self.store, self.initiative_id,
            control_store=TaskStore(self.store.config.control), jj=self.jj,
        )
        self.assertEqual(len(findings), 1)

    def assert_foreign_park_leaves_interrupted_pause_indeterminate(
        self, foreign_park, *, before_head_write: bool, actor_id: str, event_type: str,
    ) -> None:
        """A writer that parks running work with no edge owns the ambiguity.

        Whether the pause died before or after its own head write, the journal
        after its proof is the same: one paused head write and the foreign
        writer's own event.  Nothing durable says whose write it is, so the
        pause is left indeterminate and no edge is journaled in its name.
        """
        self.coordinator()
        since = len(self.events())
        pause = self.interrupted_running_pause(before_head_write=before_head_write)
        proof = self.outcome(self.action(pause["action_id"]))

        foreign_park()

        self.assertEqual(self.initiative()["state"], "paused")
        self.assertEqual(self.transition_edges(since), [])
        foreign = [
            event for event in self.events()[since:] if event["type"] == event_type
        ]
        self.assertEqual(len(foreign), 1)
        self.assertEqual(
            (foreign[0]["actor_kind"], foreign[0]["actor_id"]), ("controller", actor_id),
        )
        self.assertGreater(foreign[0]["sequence"], proof["pause_from_sequence"])
        before = self.retained()
        head = self.head()

        reconciled = self.reconciled(pause["action_id"])

        self.assertEqual(reconciled["state"], "indeterminate")
        outcome = self.outcome(reconciled)
        self.assertEqual(
            {key: outcome[key] for key in
             ("pause_from", "pause_from_revision", "pause_from_sequence")},
            {key: proof[key] for key in
             ("pause_from", "pause_from_revision", "pause_from_sequence")},
        )
        self.assertEqual(outcome["status"], "indeterminate")
        self.assertEqual(self.transition_edges(since), [])
        self.assertEqual(self.head(), head)
        self.assertEqual(self.retained(), before)
        self.assert_reconciliation_settled()

        # Resume returns the parked running work; no park edge names a
        # question to restore, and the old pause is never completed by it.
        resumed = self.operator("resume")
        self.assertEqual(resumed["state"], "completed", resumed["outcome"])
        self.assertEqual(self.outcome(resumed)["status"], "running")
        (edge,) = self.transition_edges(since)
        self.assertEqual(edge["subject_ids"], [self.initiative_id, resumed["action_id"]])
        self.assertEqual(edge["payload"], {"from": "paused", "to": "running"})
        reconcile_actions(self.store, self.initiative_id)
        self.assertEqual(self.action(pause["action_id"]), reconciled)

    def fresh_fixture(self) -> None:
        """A new store and initiative for the next sub-test."""
        self.doCleanups()
        self.setUp()

    def answer_interrupted(self, *, before_head_write: bool) -> tuple[dict, dict]:
        """Ask, then die inside the operator's answer at one exact write."""
        self.ask_the_operator()
        question = self.question_event()
        answer = self.operator_document("resume")
        if before_head_write:
            boundary = self.interrupted(
                "save_initiative", before=True, when=self.head_write_to("running"),
            )
        else:
            boundary = self.interrupted(
                "append_event", before=True, when=self.edge_to("running"),
            )
        with boundary:
            with self.assertRaises(Interrupted):
                self.submit(answer)
        crashed = self.action(answer["action_id"])
        self.assertEqual(crashed["state"], "dispatching")
        self.assert_recovered_outcome(
            crashed, resume_from="needs-input", resume_to="running",
            restored_question_event_id=None,
        )
        self.assertEqual(
            self.initiative()["state"], "needs-input" if before_head_write else "running",
        )
        return question, answer

    def paused_seal_head(self) -> None:
        """The existing stand-in for a paused seal's head write: needs-input, no event."""
        original = self.initiative()
        waiting = copy.deepcopy(original)
        waiting.update({
            "state": "needs-input",
            "state_revision": original["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(waiting, expected_digest=record_digest(original))

    def test_pause_interrupted_before_its_park_edge_still_restores_the_question(self) -> None:
        self.ask_the_operator()
        question = self.question_event()
        before = self.retained()
        head = self.head()
        since = len(self.events())
        pause = self.operator_document("pause")

        with self.interrupted("append_event", before=True, when=self.edge_to("paused")):
            with self.assertRaises(Interrupted):
                self.submit(pause)

        # The head is parked, no edge says from where, and the action is still
        # dispatching with the origin proof it retained before the head write.
        self.assertEqual(self.head(), {**head, "state": "paused"})
        self.assertEqual(self.transition_edges(since), [])
        crashed = self.action(pause["action_id"])
        self.assertEqual(crashed["state"], "dispatching")
        self.assert_recovered_outcome(
            crashed, pause_from="needs-input",
            parked_question_event_id=question["event_id"],
        )

        resumed = self.operator("resume")

        # Resume reconciled the park first, so it restored the question the
        # park had left unanswered instead of running past it.
        self.assertEqual(resumed["state"], "completed", resumed["outcome"])
        self.assertEqual(self.outcome(resumed)["status"], "needs-input")
        self.assertEqual(self.head(), {**head, "state": "needs-input"})
        self.assertEqual(
            self.state_changes(since),
            [("needs-input", "paused"), ("paused", "needs-input")],
        )
        park, restore = self.transition_edges(since)
        self.assertEqual(
            (park["actor_kind"], park["actor_id"], park["subject_ids"], park["payload"]),
            (
                "controller", "action-reconciler",
                [self.initiative_id, pause["action_id"]],
                {"from": "needs-input", "to": "paused"},
            ),
        )
        self.assertEqual(
            (restore["subject_ids"], restore["payload"]),
            (
                [self.initiative_id, resumed["action_id"]],
                {
                    "from": "paused", "to": "needs-input",
                    "restored_question_event_id": question["event_id"],
                },
            ),
        )
        recovered = self.action(pause["action_id"])
        self.assert_recovered_completion(
            recovered, self.PAUSED_FROM_NEEDS_INPUT_KEYS, status="paused",
            already_paused=False, paused_from="needs-input",
            parked_question_event_id=question["event_id"], reconciled="durable-state",
        )
        self.assertEqual(self.question_event(), question)
        self.assertEqual(self.retained(), before)
        self.assert_reconciliation_settled()

        # The restored question is answered as ever; the recovered park is
        # not touched by the answer.
        since = len(self.events())
        answered = self.operator("resume")
        self.assertEqual(self.outcome(answered)["status"], "running")
        self.assertEqual(self.state_changes(since), [("needs-input", "running")])
        self.assertEqual(self.action(pause["action_id"]), recovered)
        self.assertEqual(self.question_event(), question)

    def test_pause_interrupted_before_its_head_write_is_refused_not_completed(self) -> None:
        self.ask_the_operator()
        question = self.question_event()
        before = self.retained()
        head = self.head()
        since = len(self.events())
        pause = self.operator_document("pause")

        with self.interrupted("save_initiative", before=True, when=self.head_write_to("paused")):
            with self.assertRaises(Interrupted):
                self.submit(pause)

        self.assertEqual(self.head(), head)
        self.assertEqual(self.action(pause["action_id"])["state"], "dispatching")

        reconciled = self.reconciled(pause["action_id"])

        # No head write followed the proof, so the pause had no effect: it is
        # refused, nothing is fabricated, and the question is still the
        # operator's to answer.
        self.assertEqual(reconciled["state"], "refused")
        self.assert_recovered_outcome(
            reconciled, status="not-started",
            reason="pause was interrupted before its durable state change",
        )
        self.assertEqual(self.head(), head)
        self.assertEqual(self.transition_edges(since), [])
        self.assertEqual(
            [event["type"] for event in self.events()[since:]],
            ["action-received", "action-indeterminate", "action-refused"],
        )
        self.assertEqual(
            unanswered_operator_question(self.events())["event_id"], question["event_id"],
        )
        self.assertEqual(self.retained(), before)
        self.assert_reconciliation_settled()

        # A fresh park owns its own edge and resume restores the same question.
        parked = self.operator("pause")
        self.assertEqual(self.outcome(parked)["paused_from"], "needs-input")
        (edge,) = self.transition_edges(since)
        self.assertEqual(edge["subject_ids"], [self.initiative_id, parked["action_id"]])
        resumed = self.operator("resume")
        self.assertEqual(self.outcome(resumed)["status"], "needs-input")
        self.assertEqual(self.question_event(), question)
        self.assertEqual(self.action(pause["action_id"]), reconciled)

    def test_pause_interrupted_after_its_park_edge_completes_from_that_edge(self) -> None:
        self.ask_the_operator()
        question = self.question_event()
        head = self.head()
        since = len(self.events())
        pause = self.operator_document("pause")

        with self.interrupted("append_event", before=False, when=self.edge_to("paused")):
            with self.assertRaises(Interrupted):
                self.submit(pause)

        self.assertEqual(self.head(), {**head, "state": "paused"})
        self.assertEqual(self.action(pause["action_id"])["state"], "dispatching")
        (edge,) = self.transition_edges(since)
        self.assertEqual(edge["subject_ids"], [self.initiative_id, pause["action_id"]])

        reconciled = self.reconciled(pause["action_id"])

        self.assert_recovered_completion(
            reconciled, self.PAUSED_FROM_NEEDS_INPUT_KEYS, status="paused",
            already_paused=False, paused_from="needs-input",
            parked_question_event_id=question["event_id"], reconciled="retained-edge",
        )
        self.assertEqual(self.transition_edges(since), [edge])
        self.assert_reconciliation_settled()
        resumed = self.operator("resume")
        self.assertEqual(self.outcome(resumed)["status"], "needs-input")
        self.assertEqual(
            self.transition_edges(since)[-1]["payload"].get("restored_question_event_id"),
            question["event_id"],
        )

    def test_restoration_interrupted_after_its_edge_is_its_own_completed_restoration(self) -> None:
        self.ask_the_operator()
        question = self.question_event()
        parked = self.operator("pause")
        self.assertEqual(parked["state"], "completed", parked["outcome"])
        before = self.retained()
        head = self.head()
        since = len(self.events())
        resume = self.operator_document("resume")

        with self.interrupted("append_event", before=False, when=self.edge_to("needs-input")):
            with self.assertRaises(Interrupted):
                self.submit(resume)

        self.assertEqual(self.head(), {**head, "state": "needs-input"})
        crashed = self.action(resume["action_id"])
        self.assertEqual(crashed["state"], "dispatching")
        self.assert_recovered_outcome(
            crashed, resume_from="paused", resume_to="needs-input",
            restored_question_event_id=question["event_id"],
        )
        (edge,) = self.transition_edges(since)
        self.assertEqual(edge["subject_ids"], [self.initiative_id, resume["action_id"]])

        reconciled = self.reconciled(resume["action_id"])

        # The restoration is complete on its own evidence: its target head and
        # its bound edge, not the `running` a later answer would leave.
        self.assert_recovered_completion(
            reconciled, self.RESUMED_KEYS, status="needs-input", already_running=False,
            resume_from="paused", resume_to="needs-input",
            restored_question_event_id=question["event_id"], reconciled="retained-edge",
        )
        self.assertEqual(self.transition_edges(since), [edge])
        self.assertEqual(self.head(), {**head, "state": "needs-input"})
        self.assertEqual(self.question_event(), question)
        self.assertEqual(self.retained(), before)
        self.assert_reconciliation_settled()

        answered = self.operator("resume")

        self.assertEqual(self.outcome(answered)["status"], "running")
        self.assertEqual(
            self.state_changes(since), [("paused", "needs-input"), ("needs-input", "running")],
        )
        self.assertEqual(self.transition_edges(since)[-1]["subject_ids"],
                         [self.initiative_id, answered["action_id"]])
        self.assertEqual(self.action(resume["action_id"]), reconciled)
        self.assertEqual(self.question_event(), question)

    def test_a_later_answer_never_completes_an_interrupted_restoration(self) -> None:
        """The accepted finding: interruption after restoration, then the operator answers."""
        self.ask_the_operator()
        question = self.question_event()
        self.operator("pause")
        since = len(self.events())
        resume = self.operator_document("resume")

        with self.interrupted("append_event", before=False, when=self.edge_to("needs-input")):
            with self.assertRaises(Interrupted):
                self.submit(resume)
        self.assertEqual(self.action(resume["action_id"])["state"], "dispatching")

        answered = self.operator("resume")

        self.assertEqual(answered["state"], "completed", answered["outcome"])
        self.assertEqual(self.outcome(answered)["status"], "running")
        restoration = self.action(resume["action_id"])
        self.assert_recovered_completion(
            restoration, self.RESUMED_KEYS, status="needs-input",
            restored_question_event_id=question["event_id"], reconciled="retained-edge",
        )
        self.assertEqual(
            [(edge["payload"]["from"], edge["payload"]["to"], edge["subject_ids"][1])
             for edge in self.transition_edges(since)],
            [
                ("paused", "needs-input", resume["action_id"]),
                ("needs-input", "running", answered["action_id"]),
            ],
        )
        # A reconciliation after the answer attributes nothing to the old action.
        reconcile_actions(self.store, self.initiative_id)
        self.assertEqual(self.action(resume["action_id"]), restoration)
        self.assertEqual(self.question_event(), question)

    def test_restoration_interrupted_before_its_edge_journals_its_own_truthful_edge(self) -> None:
        self.ask_the_operator()
        question = self.question_event()
        self.operator("pause")
        before = self.retained()
        head = self.head()
        since = len(self.events())
        resume = self.operator_document("resume")

        with self.interrupted("append_event", before=True, when=self.edge_to("needs-input")):
            with self.assertRaises(Interrupted):
                self.submit(resume)

        self.assertEqual(self.head(), {**head, "state": "needs-input"})
        self.assertEqual(self.transition_edges(since), [])
        self.assertEqual(self.action(resume["action_id"])["state"], "dispatching")

        reconciled = self.reconciled(resume["action_id"])

        self.assert_recovered_completion(
            reconciled, self.RESUMED_KEYS, status="needs-input", already_running=False,
            resume_from="paused", resume_to="needs-input",
            restored_question_event_id=question["event_id"], reconciled="durable-state",
        )
        (edge,) = self.transition_edges(since)
        self.assertEqual(
            (edge["actor_id"], edge["subject_ids"], edge["payload"]),
            (
                "action-reconciler", [self.initiative_id, resume["action_id"]],
                {
                    "from": "paused", "to": "needs-input",
                    "restored_question_event_id": question["event_id"],
                },
            ),
        )
        self.assertEqual(self.head(), {**head, "state": "needs-input"})
        self.assertEqual(self.question_event(), question)
        self.assertEqual(self.retained(), before)
        self.assert_reconciliation_settled()

        # The exact unanswered question survives another park and resume.
        since = len(self.events())
        self.assertEqual(self.outcome(self.operator("pause"))["paused_from"], "needs-input")
        resumed = self.operator("resume")
        self.assertEqual(self.outcome(resumed)["status"], "needs-input")
        self.assertEqual(
            self.transition_edges(since)[-1]["payload"]["restored_question_event_id"],
            question["event_id"],
        )
        self.assertEqual(self.question_event(), question)

    def test_resume_interrupted_before_its_head_write_is_refused_and_the_next_resume_restores(
        self,
    ) -> None:
        self.ask_the_operator()
        question = self.question_event()
        self.operator("pause")
        head = self.head()
        since = len(self.events())
        resume = self.operator_document("resume")

        with self.interrupted("save_initiative", before=True, when=self.head_write_to("needs-input")):
            with self.assertRaises(Interrupted):
                self.submit(resume)

        # The target was retained before the write; the write never happened.
        self.assertEqual(self.head(), {**head, "state": "paused"})
        crashed = self.action(resume["action_id"])
        self.assertEqual(crashed["state"], "dispatching")
        self.assert_recovered_outcome(crashed, resume_from="paused", resume_to="needs-input")

        restored = self.operator("resume")

        self.assertEqual(self.outcome(restored)["status"], "needs-input")
        refused = self.action(resume["action_id"])
        self.assertEqual(refused["state"], "refused")
        self.assert_recovered_outcome(
            refused, status="not-started",
            reason="resume was interrupted before its durable state change",
        )
        (edge,) = self.transition_edges(since)
        self.assertEqual(edge["subject_ids"], [self.initiative_id, restored["action_id"]])
        self.assertEqual(edge["payload"]["restored_question_event_id"], question["event_id"])

        answered = self.operator("resume")

        self.assertEqual(self.outcome(answered)["status"], "running")
        reconcile_actions(self.store, self.initiative_id)
        self.assertEqual(self.action(resume["action_id"]), refused)
        self.assertEqual(self.question_event(), question)
        self.assert_reconciliation_settled()

    def test_running_resume_interrupted_before_its_edge_completes_to_running(self) -> None:
        parked = self.submit(self.coordinator_document("pause", {}))
        self.assertEqual(self.outcome(parked)["paused_from"], "running")
        head = self.head()
        nodes = self.store.list_nodes_snapshot(self.initiative_id)
        since = len(self.events())
        resume = self.operator_document("resume")

        with self.interrupted("append_event", before=True, when=self.edge_to("running")):
            with self.assertRaises(Interrupted):
                self.submit(resume)

        self.assertEqual(self.head(), {**head, "state": "running"})
        self.assertEqual(self.transition_edges(since), [])

        reconciled = self.reconciled(resume["action_id"])

        self.assert_recovered_completion(
            reconciled, self.RESUMED_KEYS, status="running", already_running=False,
            resume_from="paused", resume_to="running",
            restored_question_event_id=None, reconciled="durable-state",
        )
        (edge,) = self.transition_edges(since)
        self.assertEqual(
            (edge["actor_id"], edge["subject_ids"], edge["payload"]),
            (
                "action-reconciler", [self.initiative_id, resume["action_id"]],
                {"from": "paused", "to": "running"},
            ),
        )
        self.assertEqual(self.store.list_nodes_snapshot(self.initiative_id), nodes)
        self.assert_reconciliation_settled()

    def test_an_interrupted_idempotent_pause_completes_without_any_effect(self) -> None:
        self.operator("pause")
        head = self.head()
        since = len(self.events())
        pause = self.operator_document("pause")

        def completion(_initiative_id, record, **_kwargs):
            return record["action_id"] == pause["action_id"] and record["state"] == "completed"

        with self.interrupted("save_action", before=True, when=completion):
            with self.assertRaises(Interrupted):
                self.submit(pause)
        self.assertEqual(self.action(pause["action_id"])["state"], "dispatching")

        reconciled = self.reconciled(pause["action_id"])

        self.assertEqual(reconciled["state"], "completed")
        self.assertEqual(set(self.outcome(reconciled)), set(self.ALREADY_PAUSED_KEYS))
        self.assert_recovered_outcome(
            reconciled, status="paused", already_paused=True, pause_from="paused",
        )
        self.assertEqual(self.head(), head)
        self.assertEqual(self.transition_edges(since), [])
        self.assert_reconciliation_settled()

    def test_a_failed_park_edge_write_is_reconciled_from_the_durable_head(self) -> None:
        """The in-line indeterminate path: the journal write fails with an OSError."""
        self.ask_the_operator()
        question = self.question_event()
        head = self.head()
        since = len(self.events())
        original = self.store.append_event

        def failing(initiative_id, event):
            if self.edge_to("paused")(initiative_id, event):
                raise OSError("journal write failed")
            return original(initiative_id, event)

        with mock.patch.object(self.store, "append_event", side_effect=failing):
            parked = self.operator("pause")

        self.assertEqual(parked["state"], "indeterminate")
        self.assertEqual(self.outcome(parked)["reason"], "journal write failed")
        self.assertEqual(self.head(), {**head, "state": "paused"})
        self.assertEqual(self.transition_edges(since), [])

        reconciled = self.reconciled(parked["action_id"])

        self.assert_recovered_completion(
            reconciled, self.PAUSED_FROM_NEEDS_INPUT_KEYS, status="paused",
            already_paused=False, paused_from="needs-input",
            parked_question_event_id=question["event_id"], reconciled="durable-state",
        )
        (edge,) = self.transition_edges(since)
        self.assertEqual(edge["subject_ids"], [self.initiative_id, parked["action_id"]])
        self.assert_reconciliation_settled()
        resumed = self.operator("resume")
        self.assertEqual(self.outcome(resumed)["status"], "needs-input")
        self.assertEqual(self.question_event(), question)

    def test_recovery_refuses_foreign_and_earlier_park_evidence(self) -> None:
        self.ask_the_operator()
        question = self.question_event()
        # An earlier park edge out of needs-input and its restoration already
        # sit in the journal; neither belongs to the pause below.
        self.operator("pause")
        self.assertEqual(self.outcome(self.operator("resume"))["status"], "needs-input")
        head = self.head()
        since = len(self.events())
        stale = self.operator_document("pause")

        with self.interrupted("save_initiative", before=True, when=self.head_write_to("paused")):
            with self.assertRaises(Interrupted):
                self.submit(stale)
        # A foreign operator pause makes the single head write and owns its edge.
        foreign = self.operator("pause")
        self.assertEqual(self.outcome(foreign)["paused_from"], "needs-input")
        self.assertEqual(self.head(), {**head, "state": "paused"})

        reconciled = self.reconciled(stale["action_id"])

        self.assertEqual(reconciled["state"], "refused")
        self.assert_recovered_outcome(
            reconciled, status="not-started",
            reason="pause was interrupted before its durable state change",
        )
        (edge,) = self.transition_edges(since)
        self.assertEqual(edge["subject_ids"], [self.initiative_id, foreign["action_id"]])
        self.assertEqual(
            [event["subject_ids"] for event in self.transition_edges()
             if stale["action_id"] in event["subject_ids"]],
            [],
        )
        self.assert_reconciliation_settled()
        resumed = self.operator("resume")
        self.assertEqual(self.outcome(resumed)["status"], "needs-input")
        self.assertEqual(self.question_event(), question)
        self.assertEqual(self.action(stale["action_id"]), reconciled)

    def test_recovery_never_completes_from_a_head_it_cannot_prove_it_wrote(self) -> None:
        self.ask_the_operator()
        question = self.question_event()
        self.operator("pause")
        since = len(self.events())
        resume = self.operator_document("resume")

        with self.interrupted("append_event", before=True, when=self.edge_to("needs-input")):
            with self.assertRaises(Interrupted):
                self.submit(resume)
        # Before anything reconciles it, the operator parks the restored
        # question again; pause runs no reconciliation, so two head writes now
        # follow the interrupted resume's proof.
        parked = self.operator("pause")
        self.assertEqual(self.outcome(parked)["paused_from"], "needs-input")

        reconciled = self.reconciled(resume["action_id"])

        self.assertEqual(reconciled["state"], "indeterminate")
        self.assertEqual(
            [edge["subject_ids"][1] for edge in self.transition_edges(since)],
            [parked["action_id"]],
        )
        self.assert_reconciliation_settled()

        # The question is restored from the later park regardless, and neither
        # that restoration nor the answer after it completes the old action.
        restored = self.operator("resume")
        self.assertEqual(self.outcome(restored)["status"], "needs-input")
        self.assertEqual(
            self.transition_edges(since)[-1]["payload"]["restored_question_event_id"],
            question["event_id"],
        )
        answered = self.operator("resume")
        self.assertEqual(self.outcome(answered)["status"], "running")
        reconcile_actions(self.store, self.initiative_id)
        self.assertEqual(self.action(resume["action_id"]), reconciled)
        self.assertEqual(self.question_event(), question)


    def test_a_failed_restoration_edge_write_is_reconciled_from_the_durable_head(self) -> None:
        """The in-line indeterminate path of a resume: its journal write fails."""
        self.ask_the_operator()
        question = self.question_event()
        self.operator("pause")
        head = self.head()
        since = len(self.events())
        original = self.store.append_event

        def failing(initiative_id, event):
            if self.edge_to("needs-input")(initiative_id, event):
                raise OSError("journal write failed")
            return original(initiative_id, event)

        with mock.patch.object(self.store, "append_event", side_effect=failing):
            resumed = self.operator("resume")

        self.assertEqual(resumed["state"], "indeterminate")
        self.assertEqual(self.outcome(resumed)["reason"], "journal write failed")
        self.assertEqual(self.head(), {**head, "state": "needs-input"})
        self.assertEqual(self.transition_edges(since), [])

        reconciled = self.reconciled(resumed["action_id"])

        self.assert_recovered_completion(
            reconciled, self.RESUMED_KEYS, status="needs-input", already_running=False,
            resume_from="paused", resume_to="needs-input",
            restored_question_event_id=question["event_id"], reconciled="durable-state",
        )
        (edge,) = self.transition_edges(since)
        self.assertEqual(
            (edge["actor_id"], edge["subject_ids"], edge["payload"]),
            (
                "action-reconciler", [self.initiative_id, resumed["action_id"]],
                {
                    "from": "paused", "to": "needs-input",
                    "restored_question_event_id": question["event_id"],
                },
            ),
        )
        self.assertEqual(self.question_event(), question)
        self.assert_reconciliation_settled()
        answered = self.operator("resume")
        self.assertEqual(self.outcome(answered)["status"], "running")

    def test_recovered_completions_carry_exactly_what_an_uninterrupted_completion_carries(
        self,
    ) -> None:
        """The key sets the recovery assertions pin come from real completions."""
        self.ask_the_operator()
        parked = self.operator("pause")
        self.assertEqual(set(self.outcome(parked)), set(self.PAUSED_FROM_NEEDS_INPUT_KEYS))
        again = self.operator("pause")
        self.assertEqual(set(self.outcome(again)), set(self.ALREADY_PAUSED_KEYS))
        restored = self.operator("resume")
        self.assertEqual(self.outcome(restored)["status"], "needs-input")
        self.assertEqual(set(self.outcome(restored)), set(self.RESUMED_KEYS))
        answered = self.operator("resume")
        self.assertEqual(self.outcome(answered)["status"], "running")
        self.assertEqual(set(self.outcome(answered)), set(self.RESUMED_KEYS))
        already = self.operator("resume")
        self.assertTrue(self.outcome(already)["already_running"])
        self.assertEqual(set(self.outcome(already)), set(self.ALREADY_RUNNING_KEYS))
        running_park = self.submit(self.coordinator_document("pause", {}))
        self.assertEqual(self.outcome(running_park)["paused_from"], "running")
        self.assertEqual(set(self.outcome(running_park)), set(self.PAUSED_FROM_RUNNING_KEYS))
        for action in (parked, again, restored, answered, already, running_park):
            self.assertEqual(action["state"], "completed", action["outcome"])
            self.assertNotIn("reason", self.outcome(action))

    def test_an_interrupted_running_pause_completes_from_its_own_head_write(self) -> None:
        """No writer that parks without an edge followed the proof: the write is the pause's."""
        self.coordinator()
        before = self.retained()
        since = len(self.events())
        pause = self.interrupted_running_pause(before_head_write=False)
        head = self.head()

        reconciled = self.reconciled(pause["action_id"])

        # The reconciler's own `action-indeterminate` followed the proof; it
        # is a controller event from a writer that never parks, so the single
        # paused head write is provably the pause's own.
        self.assertEqual(
            [event["type"] for event in self.events()[since:]],
            ["action-received", "action-indeterminate", "initiative-state-changed"],
        )
        self.assert_recovered_completion(
            reconciled, self.PAUSED_FROM_RUNNING_KEYS, status="paused",
            already_paused=False, paused_from="running", reconciled="durable-state",
        )
        (edge,) = self.transition_edges(since)
        self.assertEqual(
            (edge["actor_id"], edge["subject_ids"], edge["payload"]),
            (
                "action-reconciler", [self.initiative_id, pause["action_id"]],
                {"from": "running", "to": "paused"},
            ),
        )
        self.assertEqual(self.head(), head)
        self.assertEqual(self.retained(), before)
        self.assert_reconciliation_settled()
        resumed = self.operator("resume")
        self.assertEqual(self.outcome(resumed)["status"], "running")
        self.assertEqual(self.action(pause["action_id"]), reconciled)

    def test_an_interrupted_running_pause_is_never_completed_from_a_reconciliation_conflict_park(
        self,
    ) -> None:
        """The accepted finding: the breaker's reconciliation-conflict route after no own write."""
        self.assert_foreign_park_leaves_interrupted_pause_indeterminate(
            self.breaker_park("reconciliation-conflict"), before_head_write=True,
            actor_id="scheduler", event_type="reconciliation-conflict",
        )

    def test_a_reconciliation_conflict_park_after_the_pause_wrote_is_equally_ambiguous(
        self,
    ) -> None:
        self.assert_foreign_park_leaves_interrupted_pause_indeterminate(
            self.breaker_park("reconciliation-conflict"), before_head_write=False,
            actor_id="scheduler", event_type="reconciliation-conflict",
        )

    def test_every_breaker_route_is_bound_by_the_scheduler_identity_not_its_event_name(
        self,
    ) -> None:
        for event_type in ("limit-reached", "storage-threshold-reached"):
            for before_head_write in (True, False):
                with self.subTest(event_type=event_type, before_head_write=before_head_write):
                    self.fresh_fixture()
                    self.assert_foreign_park_leaves_interrupted_pause_indeterminate(
                        self.breaker_park(event_type), before_head_write=before_head_write,
                        actor_id="scheduler", event_type=event_type,
                    )

    def test_seal_drift_after_an_interrupted_running_pause_is_bound_by_its_writer(self) -> None:
        for before_head_write in (True, False):
            with self.subTest(before_head_write=before_head_write):
                self.fresh_fixture()
                self.assert_foreign_park_leaves_interrupted_pause_indeterminate(
                    self.seal_drift_park, before_head_write=before_head_write,
                    actor_id="seal-reconciler", event_type="seal-drift-detected",
                )

    def test_an_answer_interrupted_before_its_edge_never_comes_back_as_a_restored_question(
        self,
    ) -> None:
        """The operator-confirmed reproduction: the answer's head write survived, its edge did not."""
        question, answer = self.answer_interrupted(before_head_write=False)

        # The operator parks and resumes the running work.  That resume
        # reconciles the interrupted answer, which stays indeterminate: two
        # head writes follow its proof and nothing proves which is its own.
        parked = self.operator("pause")
        self.assertEqual(self.outcome(parked)["paused_from"], "running")
        resumed = self.operator("resume")
        self.assertEqual(self.outcome(resumed)["status"], "running")
        stale = self.action(answer["action_id"])
        self.assertEqual(stale["state"], "indeterminate")
        self.assert_recovered_outcome(
            stale, resume_from="needs-input", resume_to="running",
            restored_question_event_id=None,
        )

        # A paused seal returns the running head to needs-input with no
        # question and no event; the operator parks and resumes that wait.
        since = len(self.events())
        self.paused_seal_head()
        parked_again = self.operator("pause")
        self.assert_recovered_outcome(
            parked_again, status="paused", paused_from="needs-input",
            parked_question_event_id=None,
        )
        restored = self.operator("resume")

        # The park edges journaled after the lost answer prove the initiative
        # ran again, which only an answer can cause: the question is
        # discharged, not restored, and the wait returns to running.
        self.assertEqual(restored["state"], "completed", restored["outcome"])
        self.assert_recovered_outcome(
            restored, status="running", resume_to="running",
            restored_question_event_id=None,
        )
        self.assertEqual(self.initiative()["state"], "running")
        self.assertEqual(
            self.state_changes(since), [("needs-input", "paused"), ("paused", "running")],
        )
        self.assertNotIn(
            "restored_question_event_id",
            [key for event in self.events()[since:] for key in event["payload"]],
        )
        self.assertEqual(self.question_event(), question)
        self.assertIsNone(unanswered_operator_question(self.events()))
        # Honesty is kept: the interrupted answer is neither completed nor
        # given an edge, and no `needs-input -> running` edge was invented.
        self.assertEqual(self.action(answer["action_id"]), stale)
        self.assertEqual(
            [event for event in self.transition_edges()
             if answer["action_id"] in event["subject_ids"]],
            [],
        )
        self.assertNotIn(("needs-input", "running"), self.state_changes())
        self.assert_reconciliation_settled()

    def test_an_answer_interrupted_before_its_head_write_leaves_the_question_open(self) -> None:
        """The twin: an answer that never wrote proves nothing ran, and the question is restored."""
        question, answer = self.answer_interrupted(before_head_write=True)
        since = len(self.events())

        parked = self.operator("pause")
        self.assert_recovered_outcome(
            parked, paused_from="needs-input", parked_question_event_id=question["event_id"],
        )
        restored = self.operator("resume")

        self.assertEqual(self.outcome(restored)["status"], "needs-input")
        self.assertEqual(
            self.outcome(restored)["restored_question_event_id"], question["event_id"],
        )
        refused = self.action(answer["action_id"])
        self.assertEqual(refused["state"], "refused")
        self.assert_recovered_outcome(
            refused, status="not-started",
            reason="resume was interrupted before its durable state change",
        )
        self.assertEqual(
            unanswered_operator_question(self.events())["event_id"], question["event_id"],
        )
        self.assertEqual(self.question_event(), question)
        answered = self.operator("resume")
        self.assertEqual(self.outcome(answered)["status"], "running")
        self.assertEqual(
            self.state_changes(since),
            [("needs-input", "paused"), ("paused", "needs-input"), ("needs-input", "running")],
        )
        self.assert_reconciliation_settled()

    def edge_less_answer(self, *, before_head_write: bool) -> tuple[dict, dict]:
        """Ask, die inside the answer, then let a paused seal take the head back.

        The recorded U5 variant: no `initiative-state-changed` edge is
        journaled between the question's own opening edge and the operator's
        next pause, so nothing in the journal alone can show that the answer
        ran.  `paused_seal_head` is the labelled stand-in for the writer;
        `RealPausedSealAnswerTests` runs the production writer over the same
        sequence.
        """
        question, answer = self.answer_interrupted(before_head_write=before_head_write)
        if not before_head_write:
            self.paused_seal_head()
        self.assertEqual(self.initiative()["state"], "needs-input")
        return question, answer

    def test_an_immediately_edge_less_answer_is_not_restored_as_a_question(self) -> None:
        """The accepted high finding: the answer's own proof is the only witness."""
        question, answer = self.edge_less_answer(before_head_write=False)
        proof = self.outcome(self.action(answer["action_id"]))
        since = len(self.events())
        self.assertEqual(self.transition_edges(since - 1), [])

        parked = self.operator("pause")

        # The park is taken with no question to park: the answer's retained
        # origin proof and the head writes since it name the discharge.
        self.assert_recovered_outcome(
            parked, status="paused", paused_from="needs-input",
            parked_question_event_id=None,
        )
        restored = self.operator("resume")

        self.assertEqual(restored["state"], "completed", restored["outcome"])
        self.assert_recovered_outcome(
            restored, status="running", resume_to="running",
            restored_question_event_id=None,
        )
        self.assertEqual(self.initiative()["state"], "running")
        self.assertEqual(
            self.state_changes(since), [("needs-input", "paused"), ("paused", "running")],
        )
        self.assertIsNone(unanswered_operator_question(
            self.events(),
            actions=self.store.list_actions_snapshot(self.initiative_id),
            initiative=self.initiative(),
        ))
        # Nothing was manufactured: no answer edge, no completion, and the
        # interrupted answer keeps the exact proof it retained before dying.
        stale = self.action(answer["action_id"])
        self.assertEqual(stale["state"], "indeterminate")
        self.assertEqual(
            {key: self.outcome(stale).get(key) for key in proof if key != "status"},
            {key: value for key, value in proof.items() if key != "status"},
        )
        self.assertNotIn(("needs-input", "running"), self.state_changes())
        self.assertEqual(
            [event for event in self.transition_edges()
             if answer["action_id"] in event["subject_ids"]],
            [],
        )
        self.assertEqual(self.question_event(), question)
        self.assert_reconciliation_settled()

    def test_an_immediately_edge_less_interruption_before_the_head_write_keeps_it(self) -> None:
        """The twin negative: the same journal, one head write fewer."""
        question, answer = self.edge_less_answer(before_head_write=True)
        since = len(self.events())

        parked = self.operator("pause")

        self.assert_recovered_outcome(
            parked, paused_from="needs-input",
            parked_question_event_id=question["event_id"],
        )
        restored = self.operator("resume")

        self.assert_recovered_outcome(
            restored, status="needs-input", resume_to="needs-input",
            restored_question_event_id=question["event_id"],
        )
        self.assertEqual(self.initiative()["state"], "needs-input")
        self.assertEqual(
            unanswered_operator_question(
                self.events(),
                actions=self.store.list_actions_snapshot(self.initiative_id),
                initiative=self.initiative(),
            )["event_id"],
            question["event_id"],
        )
        refused = self.action(answer["action_id"])
        self.assertEqual(refused["state"], "refused")
        self.assert_recovered_outcome(
            refused, status="not-started",
            reason="resume was interrupted before its durable state change",
        )
        self.assertEqual(
            self.state_changes(since),
            [("needs-input", "paused"), ("paused", "needs-input")],
        )
        self.assert_reconciliation_settled()

    def test_two_interrupted_answers_leave_the_edge_less_question_open(self) -> None:
        """A competing answer makes the head write unattributable, so it is not attributed.

        Both answers observed the same waiting head and both lost their edge
        to a paused seal, so the head writes since either proof could be the
        other's.  Restoring the question is the safe direction and the only
        honest one: the operator answers a question that was answered rather
        than losing one that was not.
        """
        question, first = self.edge_less_answer(before_head_write=False)
        second = self.operator_document("resume")
        with self.interrupted(
            "append_event", before=True, when=self.edge_to("running"),
        ):
            with self.assertRaises(Interrupted):
                self.submit(second)
        self.assertEqual(self.initiative()["state"], "running")
        self.paused_seal_head()
        self.assertEqual(self.initiative()["state"], "needs-input")

        parked = self.operator("pause")

        self.assert_recovered_outcome(
            parked, parked_question_event_id=question["event_id"],
        )
        restored = self.operator("resume")
        self.assert_recovered_outcome(
            restored, status="needs-input",
            restored_question_event_id=question["event_id"],
        )
        for document in (first, second):
            self.assertEqual(self.action(document["action_id"])["state"], "indeterminate")
        self.assert_reconciliation_settled()

    def test_a_competing_park_owns_the_only_head_write_after_an_answer_that_never_wrote(
        self,
    ) -> None:
        """The pause observed the same head later and wrote it; the answer did not."""
        question, answer = self.answer_interrupted(before_head_write=True)
        park = self.operator_document("pause")
        with self.interrupted(
            "append_event", before=True, when=self.edge_to("paused"),
        ):
            with self.assertRaises(Interrupted):
                self.submit(park)
        self.assertEqual(self.initiative()["state"], "paused")
        self.assertEqual(self.transition_edges()[-1]["payload"]["to"], "needs-input")

        restored = self.operator("resume")

        # Reconciliation completes the park from its own proof and journals
        # its edge; the answer is refused, and the question comes back.
        self.assertEqual(
            self.outcome(self.action(park["action_id"]))["status"], "paused",
        )
        self.assertEqual(self.action(answer["action_id"])["state"], "refused")
        self.assert_recovered_outcome(
            restored, status="needs-input",
            restored_question_event_id=question["event_id"],
        )
        self.assertEqual(self.initiative()["state"], "needs-input")
        self.assert_reconciliation_settled()

    def test_a_foreign_writer_event_alone_never_discharges_the_question(self) -> None:
        """The breaker journals after the proof and writes no head while work waits."""
        question, answer = self.answer_interrupted(before_head_write=True)
        since = len(self.events())

        pause_for_breaker(
            self.store, self.initiative_id, "limit-reached raised in test",
            event_type="limit-reached", subject_ids=["implementation-a"],
        )

        foreign = self.events()[since:]
        self.assertEqual(
            [(event["type"], event["actor_kind"], event["actor_id"]) for event in foreign],
            [("limit-reached", "controller", "scheduler")],
        )
        self.assertEqual(self.initiative()["state"], "needs-input")
        parked = self.operator("pause")
        self.assert_recovered_outcome(
            parked, parked_question_event_id=question["event_id"],
        )
        restored = self.operator("resume")
        self.assert_recovered_outcome(
            restored, status="needs-input",
            restored_question_event_id=question["event_id"],
        )
        self.assertEqual(self.action(answer["action_id"])["state"], "refused")
        self.assert_reconciliation_settled()

    def test_a_newer_question_is_never_discharged_by_the_older_answer(self) -> None:
        """The answer observed a head older than the question it would have to discharge."""
        question, answer = self.answer_interrupted(before_head_write=False)
        asked_again = self.submit(
            self.coordinator_document("request-decision", {
                "subject_id": "implementation-a", "question": "Retry at all?",
            }),
        )
        self.assertEqual(asked_again["state"], "completed", asked_again["outcome"])
        newer = [
            event for event in self.events()
            if event["type"] == "approval-requested"
            and event["payload"].get("kind") == "operator-decision"
        ][-1]
        self.assertNotEqual(newer["event_id"], question["event_id"])

        parked = self.operator("pause")

        self.assert_recovered_outcome(
            parked, parked_question_event_id=newer["event_id"],
        )
        restored = self.operator("resume")
        self.assert_recovered_outcome(
            restored, status="needs-input",
            restored_question_event_id=newer["event_id"],
        )
        self.assertEqual(
            unanswered_operator_question(
                self.events(),
                actions=self.store.list_actions_snapshot(self.initiative_id),
                initiative=self.initiative(),
            )["event_id"],
            newer["event_id"],
        )
        self.assertEqual(self.action(answer["action_id"])["state"], "indeterminate")
        self.assert_reconciliation_settled()


class RealPausedSealAnswerTests(ParkingFixture, unittest.TestCase):
    """The production paused-seal writer over the edge-less answer sequence.

    `paused_seal_head` in `InterruptedParkingRecoveryTests` is a labelled
    stand-in: it writes the head the way the seal does and journals nothing.
    This fixture runs the real writer instead.  A worker reports
    `needs-decision`, `prepare_and_publish_seal` publishes the paused seal
    while the initiative already waits, and the replay of that same published
    seal after the operator's answer has written the `running` head is the
    production path that takes the head back to `needs-input` with no
    `initiative-state-changed` event.  Only the Control task lookups, the
    workspace identity probe, and the task reconciliation are mocked; every
    orchestration record and every event is real.
    """

    def customize_plan(self, plan_value: dict) -> None:
        for node in plan_value["nodes"]:
            if node["node_id"] == "implementation-a":
                node["hard_write_scope"] = ["lib"]
                node["advisory_path_ownership"] = ["lib/control/orchestration"]

    def setUp(self) -> None:
        super().setUp()
        self.task = None
        self.publish_paused_seal = self._prepare_paused_seal()

    def _prepare_paused_seal(self):
        """Dispatch one worker, report `needs-decision`, and bind its seal inputs."""
        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            payload["task"]["jj"]["base_commit_id"] = argv[argv.index("--base") + 1]
            payload["task"]["jj"]["working_commit_id"] = "d" * 40
            self.task = payload["task"]
            return 0, json.dumps(payload).encode(), b""

        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            dispatched = self.submit(build_action_document(
                self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
            ))
        self.assertEqual(dispatched["state"], "completed", dispatched["outcome"])
        attempt = self.store.list_attempts_snapshot(self.initiative_id)[0]
        result = validate_result({
            "contract": "asha.orchestration-result.v1",
            "publication_id": str(uuid.uuid4()), "result_id": str(uuid.uuid4()),
            "payload_digest": "a" * 64, "supersedes_result_id": None,
            "initiative_id": self.initiative_id, "node_id": attempt["node_id"],
            "attempt_id": attempt["attempt_id"], "task_id": attempt["task_id"],
            "run_id": self.task["runs"][0]["run_id"], "claim_status": "needs-decision",
            "summary": "The worker needs an operator decision.", "files_changed": [],
            "verification_attestations": [], "concerns": [], "follow_up": [],
            "published_at": now_text(),
        })
        self.store.save_result(self.initiative_id, result)
        reported = copy.deepcopy(attempt)
        reported.update({
            "state": "reported", "result_publication_id": result["publication_id"],
            "result_id": result["result_id"], "updated_at": now_text(),
        })
        self.store.save_attempt(
            self.initiative_id, reported, expected_digest=record_digest(attempt),
        )
        observed = {
            "contract": "asha.control-reconciliation.v1",
            "task_id": self.task["task_id"], "state": "exited", "blocker": None,
            "evidence": [],
            "runs": [{
                "contract": "asha.control-run-reconciliation.v1",
                "run_id": self.task["runs"][0]["run_id"], "state": "exited",
                "blocker": None,
                "evidence": [
                    {"source": "tmux", "outcome": "missing",
                     "detail": "tmux pane process exited with status 0",
                     "state": None, "stale": False},
                    {"source": "process", "outcome": "missing",
                     "detail": "process absent", "state": None, "stale": False},
                    {"source": "jj", "outcome": "match", "detail": "jj identity",
                     "state": None, "stale": False},
                ],
            }],
        }
        origin = attempt["base"]["scope_origin"]["tree_digest"]

        def publish() -> dict:
            return prepare_and_publish_seal(
                self.store, self.initiative_id, attempt["attempt_id"], self.task,
                observed, jj=SealJj(self.task, origin, ()),
            )

        return publish

    @contextmanager
    def settled_control_task(self):
        """The sealed worker's Control task and workspace, observed as settled."""
        control = mock.Mock()
        control.peek.return_value = self.task
        control.list.return_value = [self.task]
        workspace = mock.Mock()
        workspace.inspect_workspace.return_value = WorkspaceIdentity(
            name=self.task["jj"]["workspace_name"],
            change_id=self.task["jj"]["change_id"],
            commit_id=self.task["jj"]["working_commit_id"],
            parent_commit_ids=(self.task["jj"]["base_commit_id"],),
            description="sealed",
        )
        with mock.patch(
            "lib.control.orchestration.actions.TaskStore", return_value=control,
        ), mock.patch(
            "lib.control.orchestration.reconcile.TaskStore", return_value=control,
        ), mock.patch(
            "lib.control.orchestration.seals.TaskStore", return_value=control,
        ), mock.patch(
            "lib.control.orchestration.seals.JjAdapter", return_value=workspace,
        ), mock.patch(
            "lib.control.orchestration.reconcile.reconcile_task",
            return_value={"state": "exited", "blocker": None, "evidence": []},
        ):
            yield

    def operator(self, action_class: str) -> dict:
        with self.settled_control_task():
            return super().operator(action_class)

    def test_the_real_paused_seal_writer_never_resurrects_the_answered_question(
        self,
    ) -> None:
        self.ask_the_operator()
        question = self.question_event()
        first = self.publish_paused_seal()
        self.assertEqual(first["outcome"], "paused")
        self.assertEqual(self.initiative()["state"], "needs-input")
        since = len(self.events())

        answer = self.operator_document("resume")
        with self.settled_control_task(), self.interrupted(
            "append_event", before=True, when=self.edge_to("running"),
        ):
            with self.assertRaises(Interrupted):
                self.submit(answer)
        self.assertEqual(self.initiative()["state"], "running")

        replayed = self.publish_paused_seal()

        # The production writer, on the production branch: the running head
        # goes back to needs-input and no edge is journaled for it.
        self.assertEqual(replayed["seal_id"], first["seal_id"])
        self.assertEqual(self.initiative()["state"], "needs-input")
        self.assertEqual(self.transition_edges(since), [])

        parked = self.operator("pause")
        self.assert_recovered_outcome(
            parked, status="paused", paused_from="needs-input",
            parked_question_event_id=None,
        )
        restored = self.operator("resume")

        self.assertEqual(restored["state"], "completed", restored["outcome"])
        self.assert_recovered_outcome(
            restored, status="running", resume_to="running",
            restored_question_event_id=None,
        )
        self.assertEqual(self.initiative()["state"], "running")
        self.assertEqual(
            self.state_changes(since),
            [("needs-input", "paused"), ("paused", "running")],
        )
        self.assertEqual(self.question_event(), question)
        self.assertNotIn(("needs-input", "running"), self.state_changes())
        self.assertEqual(self.action(answer["action_id"])["state"], "indeterminate")
        self.assertEqual(
            self.store.read_seal(self.initiative_id, first["seal_id"]), first,
            "the published seal is immutable across the replay",
        )
        with self.settled_control_task():
            self.assert_reconciliation_settled()

    def test_the_real_writer_leaves_an_answer_that_never_wrote_unanswered(self) -> None:
        """The same production writer, one head write fewer, keeps the question."""
        self.ask_the_operator()
        question = self.question_event()
        self.assertEqual(self.publish_paused_seal()["outcome"], "paused")

        answer = self.operator_document("resume")
        with self.settled_control_task(), self.interrupted(
            "save_initiative", before=True, when=self.head_write_to("running"),
        ):
            with self.assertRaises(Interrupted):
                self.submit(answer)
        self.assertEqual(self.initiative()["state"], "needs-input")
        self.assertEqual(self.publish_paused_seal()["outcome"], "paused")

        parked = self.operator("pause")
        self.assert_recovered_outcome(
            parked, parked_question_event_id=question["event_id"],
        )
        restored = self.operator("resume")

        self.assert_recovered_outcome(
            restored, status="needs-input",
            restored_question_event_id=question["event_id"],
        )
        self.assertEqual(self.initiative()["state"], "needs-input")
        self.assertEqual(self.action(answer["action_id"])["state"], "refused")
        with self.settled_control_task():
            self.assert_reconciliation_settled()


class ParkingFromApprovedTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def test_only_running_or_waiting_work_may_be_parked(self) -> None:
        self.assertEqual(self.initiative()["state"], "approved")
        since = len(self.store.list_events_snapshot(self.initiative_id))

        refused = submit_action(
            self.store, self.initiative_id,
            build_action_document(self.initiative(), "pause", {}),
        )

        self.assertEqual(refused["state"], "refused")
        self.assertEqual(
            json.loads(refused["outcome"])["reason"],
            "only a running or needs-input initiative may pause",
        )
        self.assertEqual(self.initiative()["state"], "approved")
        self.assertEqual(
            [event["payload"] for event in
             self.store.list_events_snapshot(self.initiative_id)[since:]
             if event["type"] == "initiative-state-changed"],
            [],
        )


if __name__ == "__main__":
    unittest.main()
