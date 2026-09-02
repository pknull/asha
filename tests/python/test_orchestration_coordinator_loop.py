"""Coordinator loop verbs: bounded wait with a durable cursor, and coordinator-actor plan proposals."""

from __future__ import annotations

import copy
import json
import time
import unittest
from dataclasses import replace
from unittest import mock

from lib.control.jj import ImmutableTree, RepositoryFacts
from lib.control.orchestration import cli
from lib.control.orchestration.actions import append_event
from lib.control.orchestration import coordinator as coordinator_module
from lib.control.orchestration.coordinator import CoordinatorError, claim, wait
from lib.control.orchestration.model import record_digest
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_orchestration_coordinator_claim import FakeTmux
from tests.python.test_orchestration_graph import valid_plan


class CoordinatorWaitTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def setUp(self) -> None:
        super().setUp()
        self.tmux = FakeTmux()
        self.pane_env = {**self.env, "TMUX_PANE": "%7"}

    def tail(self) -> int:
        return self.initiative()["last_event_sequence"]

    def test_wait_requires_a_live_anchored_coordinator(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "no live coordinator generation"):
            wait(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux, after=0, timeout=0)
        claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        with self.assertRaisesRegex(CoordinatorError, "not inside the coordinator's anchor pane"):
            wait(self.store, self.initiative(), env={**self.env, "TMUX_PANE": "%9"}, tmux=self.tmux, after=0, timeout=0)

    def test_timeout_returns_no_events_and_leaves_the_coordinator_active(self) -> None:
        record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        cursor = self.tail()
        before = self.store.read_coordinator(self.initiative_id, record["coordinator_id"])
        started = time.monotonic()
        payload = wait(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux, after=cursor, timeout=0)
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(payload["timed_out"])
        self.assertEqual(payload["events"], [])
        self.assertEqual(payload["after"], cursor)
        self.assertEqual(payload["last_event_sequence"], cursor)
        self.assertEqual(payload["contract"], "asha.orchestration-event-wait.v1")
        after = self.store.read_coordinator(self.initiative_id, record["coordinator_id"])
        self.assertEqual(after["state"], "active")
        self.assertEqual(after["event_cursor"], before["event_cursor"])
        self.assertGreaterEqual(after["updated_at"], before["updated_at"])
        self.assertEqual(self.tail(), cursor)

    def test_wait_state_distinguishes_an_armed_watch_from_a_parked_session(self) -> None:
        record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        cursor = self.tail()
        observed: list[str] = []

        def sleep(_seconds: float) -> None:
            observed.append(
                self.store.read_coordinator(
                    self.initiative_id, record["coordinator_id"],
                )["state"]
            )
            append_event(
                self.store, self.initiative_id, "node-ready", ["implementation-a"],
                {"node_id": "implementation-a"},
                actor_kind="controller", actor_id="test",
            )

        with mock.patch.object(coordinator_module.time, "sleep", side_effect=sleep):
            payload = wait(
                self.store, self.initiative(), env=self.pane_env, tmux=self.tmux,
                after=cursor, timeout=5,
            )

        self.assertEqual(observed, ["waiting"])
        self.assertFalse(payload["timed_out"])
        self.assertEqual(
            self.store.read_coordinator(
                self.initiative_id, record["coordinator_id"],
            )["state"],
            "active",
        )

    def test_arrival_returns_events_after_the_cursor_and_advances_it(self) -> None:
        record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        cursor = self.tail()
        first = append_event(
            self.store, self.initiative_id, "node-ready", ["implementation-a"], {"node_id": "implementation-a"},
            actor_kind="controller", actor_id="test",
        )
        second = append_event(
            self.store, self.initiative_id, "limit-reached", [], {"limit": "max_parallel"},
            actor_kind="controller", actor_id="test",
        )
        payload = wait(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux, after=cursor, timeout=5)
        self.assertFalse(payload["timed_out"])
        self.assertEqual([event["sequence"] for event in payload["events"]], [first["sequence"], second["sequence"]])
        self.assertEqual(payload["last_event_sequence"], second["sequence"])
        advanced = self.store.read_coordinator(self.initiative_id, record["coordinator_id"])
        self.assertEqual(advanced["event_cursor"], second["sequence"])
        # A later, narrower cursor read never moves the durable cursor backward.
        again = wait(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux, after=first["sequence"], timeout=0)
        self.assertEqual([event["sequence"] for event in again["events"]], [second["sequence"]])
        self.assertEqual(
            self.store.read_coordinator(self.initiative_id, record["coordinator_id"])["event_cursor"],
            second["sequence"],
        )

    def test_cursor_and_timeout_bounds(self) -> None:
        claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        with self.assertRaisesRegex(CoordinatorError, "outside the durable event tail"):
            wait(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux, after=self.tail() + 5, timeout=0)
        with self.assertRaisesRegex(CoordinatorError, "non-negative"):
            wait(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux, after=0, timeout=-1)

    def test_wait_outlives_multiple_revalidation_segments_and_restarts_exactly(self) -> None:
        claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        cursor = self.tail()
        segmented = replace(self.store.config, coordinator_wait_seconds=1)
        clock = [0.0]
        appended: list[dict] = []

        def sleep(seconds: float) -> None:
            clock[0] += seconds
            if clock[0] > 2.0 and not appended:
                appended.append(append_event(
                    self.store, self.initiative_id, "node-ready", ["implementation-a"],
                    {"node_id": "implementation-a"},
                    actor_kind="controller", actor_id="test",
                ))

        with mock.patch.object(self.store, "config", segmented), \
                mock.patch.object(coordinator_module.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(coordinator_module.time, "sleep", side_effect=sleep):
            payload = wait(
                self.store, self.initiative(), env=self.pane_env, tmux=self.tmux,
                after=cursor, timeout=3,
            )

        self.assertEqual(clock[0], 2.25)
        self.assertEqual(payload["events"], appended)
        self.assertFalse(payload["timed_out"])
        restart_cursor = payload["last_event_sequence"]
        later = append_event(
            self.store, self.initiative_id, "limit-reached", [],
            {"limit": "max_parallel"}, actor_kind="controller", actor_id="test",
        )
        restarted = wait(
            self.store, self.initiative(), env=self.pane_env, tmux=self.tmux,
            after=restart_cursor, timeout=0,
        )
        self.assertEqual(
            [event["sequence"] for event in restarted["events"]], [later["sequence"]],
        )

    def test_stale_generation_ends_at_the_next_segment_boundary(self) -> None:
        record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        cursor = self.tail()
        segmented = replace(self.store.config, coordinator_wait_seconds=1)
        clock = [0.0]
        changed = False

        def sleep(seconds: float) -> None:
            nonlocal changed
            clock[0] += seconds
            if not changed:
                live = self.store.read_coordinator(
                    self.initiative_id, record["coordinator_id"],
                )
                stale = copy.deepcopy(live)
                stale.update({"state": "stale", "updated_at": live["updated_at"]})
                self.store.save_coordinator(
                    self.initiative_id, stale, expected_digest=record_digest(live),
                )
                changed = True

        with mock.patch.object(self.store, "config", segmented), \
                mock.patch.object(coordinator_module.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(coordinator_module.time, "sleep", side_effect=sleep):
            payload = wait(
                self.store, self.initiative(), env=self.pane_env, tmux=self.tmux,
                after=cursor, timeout=30,
            )

        self.assertEqual(payload["ended"], "stale-generation")
        self.assertFalse(payload["timed_out"])
        self.assertEqual(clock[0], 1.0)

    def test_terminal_initiative_ends_at_the_next_segment_boundary(self) -> None:
        claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        cursor = self.tail()
        segmented = replace(self.store.config, coordinator_wait_seconds=1)
        clock = [0.0]
        changed = False

        def sleep(seconds: float) -> None:
            nonlocal changed
            clock[0] += seconds
            if not changed:
                current = self.initiative()
                terminal = copy.deepcopy(current)
                terminal.update({
                    "state": "cancelled",
                    "state_revision": current["state_revision"] + 1,
                    "updated_at": current["updated_at"],
                })
                self.store.save_initiative(
                    terminal, expected_digest=record_digest(current),
                )
                changed = True

        with mock.patch.object(self.store, "config", segmented), \
                mock.patch.object(coordinator_module.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(coordinator_module.time, "sleep", side_effect=sleep):
            payload = wait(
                self.store, self.initiative(), env=self.pane_env, tmux=self.tmux,
                after=cursor, timeout=30,
            )

        self.assertEqual(payload["ended"], "terminal-initiative")
        self.assertFalse(payload["timed_out"])
        self.assertEqual(clock[0], 1.0)
        already_terminal = wait(
            self.store, self.initiative(), env=self.pane_env, tmux=self.tmux,
            after=cursor, timeout=0,
        )
        self.assertEqual(already_terminal["ended"], "terminal-initiative")

    def test_default_timeout_remains_the_configured_segment(self) -> None:
        claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        expected = {"timed_out": True}
        with mock.patch.object(cli, "wait_for_events", return_value=expected) as waiting:
            payload, json_output = cli._wait_command(
                [self.initiative_id, "--after", str(self.tail()), "--json"],
                self.store, self.pane_env, self.tmux,
            )
        self.assertTrue(json_output)
        self.assertEqual(payload, expected)
        self.assertEqual(
            waiting.call_args.kwargs["timeout"],
            self.store.config.coordinator_wait_seconds,
        )

    def test_wait_hard_ceiling_is_one_hour(self) -> None:
        self.assertEqual(coordinator_module.MAX_COORDINATOR_WAIT_SECONDS, 3600)
        claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        cursor = self.tail()
        long_segment = replace(self.store.config, coordinator_wait_seconds=4000)
        clock = [0.0]

        def sleep(seconds: float) -> None:
            clock[0] += seconds

        with mock.patch.object(self.store, "config", long_segment), \
                mock.patch.object(coordinator_module, "_WAIT_TICK_SECONDS", 1000), \
                mock.patch.object(coordinator_module.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(coordinator_module.time, "sleep", side_effect=sleep):
            payload = wait(
                self.store, self.initiative(), env=self.pane_env, tmux=self.tmux,
                after=cursor, timeout=7200,
            )

        self.assertTrue(payload["timed_out"])
        self.assertEqual(clock[0], 3600)

    def test_fenced_generation_cannot_wait(self) -> None:
        claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        other = FakeTmux(pane_id="%8")
        claim(self.store, self.initiative(), env={**self.env, "TMUX_PANE": "%8"}, tmux=other)
        with self.assertRaisesRegex(CoordinatorError, "not inside the coordinator's anchor pane"):
            wait(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux, after=0, timeout=0)

    def test_cli_wait_requires_json_and_integers(self) -> None:
        claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        with self.assertRaisesRegex(ValueError, "wait requires --json"):
            cli._wait_command([self.initiative_id, "--after", "0", "--timeout", "0"], self.store, self.pane_env, self.tmux)
        with self.assertRaisesRegex(ValueError, "--after must be a non-negative integer"):
            cli._wait_command([self.initiative_id, "--after", "-1", "--timeout", "0", "--json"], self.store, self.pane_env, self.tmux)
        payload, json_output = cli._wait_command(
            [self.initiative_id, "--after", str(self.tail()), "--timeout", "0", "--json"],
            self.store, self.pane_env, self.tmux,
        )
        self.assertTrue(json_output)
        self.assertTrue(payload["timed_out"])


class CoordinatorProposePlanTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def setUp(self) -> None:
        super().setUp()
        self.tmux = FakeTmux()
        self.pane_env = {**self.env, "TMUX_PANE": "%7"}
        self.jj = mock.Mock()
        self.jj.preflight.return_value = RepositoryFacts(root=self.repo, git_root=self.repo / ".git")
        self.jj.immutable_tree.return_value = ImmutableTree(commit_id="b" * 40, digest="c" * 64, entries=())
        created = cli._create([
            "--repo", str(self.repo), "--slug", "coordinator-planned",
            "--label", "Coordinator planned", "--objective", "Let the coordinator plan.",
        ], self.config, self.store, self.jj)["initiative"]
        self.draft_id = created["initiative_id"]
        plan_value = valid_plan()
        plan_value["initiative_id"] = self.draft_id
        plan_value["repositories"] = [copy.deepcopy(created["scope"]["repository"])]
        repository_id = created["scope"]["repository"]["repository_id"]
        for node in plan_value["nodes"]:
            if node["repository_id"] is not None:
                node["repository_id"] = repository_id
        self.plan_file = self.root / "coordinator-plan.json"
        self.plan_file.write_text(json.dumps(plan_value))

    def draft(self) -> dict:
        return self.store.peek(self.draft_id)

    def test_propose_plan_requires_a_live_anchored_coordinator(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "no live coordinator generation"):
            cli._propose_plan_command(
                [self.draft_id, "--file", str(self.plan_file)], self.store, self.config,
                self.pane_env, self.tmux, jj=self.jj,
            )
        self.assertEqual(self.draft()["state"], "draft")

    def test_coordinator_proposal_is_recorded_under_the_coordinator_actor(self) -> None:
        record = claim(self.store, self.draft(), env=self.pane_env, tmux=self.tmux)
        plan, json_output = cli._propose_plan_command(
            [self.draft_id, "--file", str(self.plan_file), "--json"], self.store, self.config,
            self.pane_env, self.tmux, jj=self.jj,
        )
        self.assertTrue(json_output)
        self.assertEqual(plan["revision"], 1)
        self.assertEqual(plan["status"], "proposed")
        self.assertEqual(self.draft()["state"], "awaiting-plan-approval")
        proposed = [
            event for event in self.store.list_events_snapshot(self.draft_id)
            if event["type"] == "plan-proposed"
        ]
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0]["actor_kind"], "coordinator")
        self.assertEqual(proposed[0]["actor_id"], f"coordinator:{record['coordinator_id']}")
        self.assertEqual(proposed[0]["payload"]["digest"], plan["digest"])
        # Approval stays an operator act from another pane; the coordinator pane is refused.
        with self.assertRaisesRegex(CoordinatorError, "refused from the coordinator's pane"):
            cli._approve([self.draft_id, "--digest", plan["digest"]], self.store, self.pane_env, self.tmux)
        approved, _ = cli._approve(
            [self.draft_id, "--digest", plan["digest"]], self.store, {**self.env, "TMUX_PANE": "%2"}, self.tmux,
        )
        self.assertEqual(approved["initiative"]["state"], "approved")
        # The coordinator record is untouched by planning and approval.
        self.assertEqual(
            record_digest(self.store.read_coordinator(self.draft_id, record["coordinator_id"])),
            record_digest(record),
        )

    def test_operator_plan_path_still_records_the_operator_actor(self) -> None:
        plan, _ = cli._plan([self.draft_id, "--file", str(self.plan_file)], self.store, self.config, jj=self.jj)
        proposed = [
            event for event in self.store.list_events_snapshot(self.draft_id)
            if event["type"] == "plan-proposed"
        ]
        self.assertEqual(proposed[0]["actor_kind"], "operator")
        self.assertEqual(proposed[0]["actor_id"], "cli")
        self.assertEqual(proposed[0]["payload"]["digest"], plan["digest"])


if __name__ == "__main__":
    unittest.main()
