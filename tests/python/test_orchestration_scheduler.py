from __future__ import annotations

import copy
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.model import ATTEMPT_CONTRACT, record_digest
from lib.control.orchestration.scheduler import (
    SchedulerError,
    _goal,
    consecutive_failures,
    pause_for_breaker,
    readiness,
)
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text


class OrchestrationSchedulerTests(ExecutionFixture, unittest.TestCase):
    def attempt(self, state: str = "running", ordinal: int = 1) -> dict:
        node = self.store.read_node(self.initiative_id, "implementation-a")
        at = now_text()
        return {
            "contract": ATTEMPT_CONTRACT,
            "attempt_id": str(uuid.uuid4()),
            "initiative_id": self.initiative_id,
            "node_id": node["node_id"],
            "task_id": str(uuid.uuid4()),
            "action_id": str(uuid.uuid4()),
            "ordinal": ordinal,
            "base": copy.deepcopy(node["base"]),
            "state": state,
            "result_publication_id": None,
            "result_id": None,
            "seal_id": None,
            "created_at": at,
            "updated_at": at,
        }

    def update_limits(self, **changes) -> None:
        initiative = self.initiative()
        updated = copy.deepcopy(initiative)
        updated["limits"].update(changes)
        updated["state_revision"] += 1
        updated["updated_at"] = now_text()
        self.store.save_initiative(
            updated, expected_digest=record_digest(initiative),
        )

    def test_readiness_is_dependency_and_limit_deterministic(self) -> None:
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            first = readiness(self.store, self.initiative())
            second = readiness(self.store, self.initiative())
        self.assertEqual(first, second)
        self.assertEqual(first, {
            "implementation-a": "ready",
            "review-a": "blocked",
            "verify-a": "blocked",
        })

    def test_parallel_total_deadline_pause_and_storage_limits_block(self) -> None:
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            self.store.save_attempt(self.initiative_id, self.attempt())
            self.update_limits(max_parallel=1)
            self.assertEqual(
                readiness(self.store, self.initiative())["implementation-a"],
                "blocked",
            )

        # A pause state is itself a hard readiness gate.
        pause_for_breaker(self.store, self.initiative_id, "test breaker")
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            self.assertTrue(all(
                state != "ready" for state in readiness(self.store, self.initiative()).values()
            ))

    def test_storage_and_deadline_are_hard_gates(self) -> None:
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": True},
        ):
            self.assertEqual(
                readiness(self.store, self.initiative())["implementation-a"],
                "blocked",
            )

    def test_total_and_per_node_attempt_caps_block_new_reservations(self) -> None:
        self.store.save_attempt(
            self.initiative_id, self.attempt("launch-failed"),
        )
        self.update_limits(max_total_tasks=1, max_attempts_per_node=1)
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            effective = readiness(self.store, self.initiative())
        self.assertEqual(effective["implementation-a"], "blocked")
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        self.update_limits(deadline=past)
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ):
            self.assertEqual(
                readiness(self.store, self.initiative())["implementation-a"],
                "blocked",
            )

    def test_consecutive_failure_breaker_count_is_trailing_only(self) -> None:
        failures = [self.attempt("launch-failed", ordinal=index) for index in (1, 2, 3)]
        for index, attempt in enumerate(failures):
            attempt["created_at"] = attempt["updated_at"] = (
                datetime.now(timezone.utc) + timedelta(microseconds=index)
            ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        self.assertEqual(consecutive_failures(failures), 3)
        self.assertEqual(
            consecutive_failures([*failures, self.attempt("allocated", ordinal=4)]),
            0,
        )

    def test_control_goal_elides_long_slug_and_preserves_absolute_assignment_path(self) -> None:
        initiative = {"slug": "s" * 40}
        node = {"node_id": "n" * 40}
        attempt_id = "11111111-1111-4111-8111-111111111111"
        assignment = (
            self.config.initiatives_dir / self.initiative_id / "assignments"
            / f"{attempt_id}.md"
        )
        goal = _goal(initiative, node, assignment)
        self.assertLessEqual(len(goal), 200)
        self.assertTrue(goal.startswith("orch "))
        self.assertIn(attempt_id, goal)
        self.assertTrue(goal.endswith(str(assignment)))
        slug_part = goal.removeprefix("orch ").split(" ", 1)[0]
        self.assertLessEqual(len(slug_part), 24)
        too_long = Path("/") / ("a" * 170) / f"{attempt_id}.md"
        with self.assertRaisesRegex(SchedulerError, "absolute assignment path"):
            _goal(initiative, node, too_long)

    def test_storage_breaker_refuses_dispatch_and_pauses(self) -> None:
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        capture = mock.Mock()
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": True},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", capture,
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "refused")
        self.assertEqual(self.initiative()["state"], "paused")
        capture.assert_not_called()
        self.assertIn(
            "storage-threshold-reached",
            [event["type"] for event in self.store.list_events_snapshot(self.initiative_id)],
        )

    def test_deadline_breaker_refuses_dispatch_and_pauses(self) -> None:
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
        self.update_limits(deadline=past)
        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        capture = mock.Mock()
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", capture,
        ):
            action = submit_action(self.store, self.initiative_id, document)
        self.assertEqual(action["state"], "refused")
        self.assertEqual(self.initiative()["state"], "paused")
        capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
