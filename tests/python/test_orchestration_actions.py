from __future__ import annotations

import copy
import json
import unittest
import uuid
from unittest import mock

from lib.control.orchestration.actions import (
    ActionRefused,
    _parse_document,
    _repair_node,
    build_action_document,
    payload_digest,
    reconcile_actions,
    submit_action,
)
from lib.control.orchestration.scheduler import SchedulerError
from lib.control.orchestration.model import ATTEMPT_CONTRACT, record_digest
from lib.control.orchestration.store import ObservationOnlyPlanError
from lib.control.jj import RepositoryFacts
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text


class OrchestrationActionTests(ExecutionFixture, unittest.TestCase):
    def dispatch_one(self):
        payloads = []

        def capture(argv, **_kwargs):
            payload = self.control_payload(argv)
            payloads.append(payload)
            self.last_control_task = payload["task"]
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
        return self.store.list_attempts_snapshot(self.initiative_id)[0]

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


if __name__ == "__main__":
    unittest.main()
