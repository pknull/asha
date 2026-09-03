from __future__ import annotations

import copy
import json
import re
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from lib.control.orchestration.actions import (
    _parse_document, build_action_document, submit_action,
)
from lib.control.orchestration.model import (
    ATTEMPT_CONTRACT,
    MAX_ARGV_ITEMS,
    MAX_ARG_BYTES,
    MAX_ATTESTATIONS,
    MAX_PATH_BYTES,
    MAX_SUMMARY_BYTES,
    record_digest,
)
from lib.control.orchestration.scheduler import (
    SchedulerError,
    _goal,
    assignment_bytes,
    consecutive_failures,
    dispatch,
    pause_for_breaker,
    readiness,
)
from lib.control.orchestration.store import ObservationOnlyPlanError
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

    def test_direct_work_and_review_dispatch_refuse_historical_plan_before_preparation(self) -> None:
        self.install_historical_active_plan()
        before_attempts = self.store.list_attempts_snapshot(self.initiative_id)

        for node_id in ("implementation-a", "review-a"):
            with self.subTest(node_id=node_id):
                document = build_action_document(
                    self.initiative(), "dispatch-node", {"node_id": node_id},
                )
                action, _ = _parse_document(document)
                with self.assertRaises(ObservationOnlyPlanError), mock.patch(
                    "lib.control.orchestration.scheduler.capture_bytes",
                ) as launch, mock.patch.object(
                    self.store, "save_attempt",
                ) as save_attempt:
                    dispatch(
                        self.store, self.config, self.initiative_id, node_id,
                        action=action,
                    )
                launch.assert_not_called()
                save_attempt.assert_not_called()
        self.assertEqual(
            self.store.list_attempts_snapshot(self.initiative_id), before_attempts,
        )

    def test_assignment_embeds_bounded_upstream_result_summary(self) -> None:
        initiative = self.initiative()
        node = self.store.read_node(self.initiative_id, "implementation-a")
        attempt = self.attempt(state="allocated")
        rendered = assignment_bytes(
            initiative, self.plan, node, attempt,
            attempt["base"]["scope_origin"]["jj_commit_id"],
            [{
                "seal_id": str(uuid.uuid4()), "outcome": "success",
                "read_only": False, "scope_origin": attempt["base"]["scope_origin"],
                "jj_commit_id": "d" * 40, "tree_digest": "e" * 64,
                "changed_paths": ["lib/change.py"],
                "cumulative_changed_paths": ["lib/change.py"],
                "result": {
                    "result_id": str(uuid.uuid4()), "payload_digest": "f" * 64,
                    "claim_status": "completed", "summary": "exact upstream work",
                    "concerns": [], "follow_up": [],
                },
            }],
        ).decode()
        self.assertIn("exact upstream work", rendered)
        self.assertIn("f" * 64, rendered)
        self.assertIn(
            "Do not run `jj status` or any other jj command that snapshots",
            rendered,
        )
        self.assertIn("The report receipt phase is `staged`", rendered)

    def test_assignment_documents_closed_verification_attestation_schema(self) -> None:
        initiative = self.initiative()
        node = self.store.read_node(self.initiative_id, "implementation-a")
        attempt = self.attempt(state="allocated")

        rendered = assignment_bytes(
            initiative, self.plan, node, attempt,
            attempt["base"]["scope_origin"]["jj_commit_id"],
        ).decode()

        key_line = next(
            line for line in rendered.splitlines()
            if line.startswith("Exact required element keys:")
        )
        self.assertEqual(
            re.findall(r"`([^`]+)`", key_line),
            ["argv", "cwd", "exit_code", "finished_at", "output_digest", "summary"],
        )
        self.assertIn(f"at most {MAX_ATTESTATIONS} elements", rendered)
        self.assertIn(f"at most {MAX_ARGV_ITEMS} unique text arguments", rendered)
        self.assertIn(f"1-{MAX_ARG_BYTES} UTF-8 bytes", rendered)
        self.assertIn(f"1-{MAX_PATH_BYTES} UTF-8 bytes", rendered)
        self.assertIn(f"1-{MAX_SUMMARY_BYTES} UTF-8 bytes", rendered)

    def test_repair_assignment_explains_attempt_local_supersession(self) -> None:
        initiative = self.initiative()
        node = self.store.read_node(self.initiative_id, "implementation-a")
        attempt = self.attempt(state="allocated")

        rendered = assignment_bytes(
            initiative, self.plan, node, attempt,
            attempt["base"]["scope_origin"]["jj_commit_id"],
            accepted_findings=[{
                "severity": "high", "location": "results.py",
                "summary": "Repair the accepted result lineage defect.",
            }],
        ).decode()

        self.assertIn("## Accepted review findings to fix", rendered)
        self.assertIn(
            "supersedes_result_id MUST be null for the first result of this "
            "attempt, including a repair or salvage attempt that follows an "
            "earlier attempt; set it only to the result_id this same attempt "
            "already had accepted when publishing a correction.",
            rendered,
        )

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

    def test_successful_dispatch_surfaces_delivery_preflight_stderr(self) -> None:
        diagnostic = (
            "Delivery preflight: untracked remote bookmarks at origin: release; "
            "remediate with: jj bookmark track NAME --remote=origin"
        )

        def capture(argv, **_kwargs):
            return 0, json.dumps(self.control_payload(argv)).encode(), diagnostic.encode()

        document = build_action_document(
            self.initiative(), "dispatch-node", {"node_id": "implementation-a"},
        )
        with mock.patch(
            "lib.control.orchestration.scheduler.storage_report",
            return_value={"pause_recommended": False},
        ), mock.patch(
            "lib.control.orchestration.scheduler.capture_bytes", side_effect=capture,
        ):
            action = submit_action(self.store, self.initiative_id, document)

        self.assertEqual(action["state"], "completed")
        self.assertEqual(json.loads(action["outcome"])["diagnostic"], diagnostic)

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
