"""Regression coverage for the initiative CLI's operator-facing surfaces."""

from __future__ import annotations

import copy
import io
import json
import shlex
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from lib.control.orchestration import coordinator as coordinator_module
from lib.control.orchestration.actions import append_event
from lib.control.orchestration.cli import (
    _coordinator_command,
    _operator_action,
    _snapshot,
    main,
    show_payload,
)
from lib.control.orchestration.coordinator import CoordinatorError, claim, wait
from lib.control.orchestration.doctor import _coordinator_cursor_probe
from lib.control.orchestration.model import record_digest
from lib.control.orchestration.tui_model import attention_items
from tests.python.orchestration_execution_fixtures import ExecutionFixture, now_text
from tests.python.test_orchestration_coordinator_claim import FakeTmux


class ReapingFakeTmux(FakeTmux):
    def __init__(self) -> None:
        super().__init__()
        self.killed_panes: list[str] = []

    def _run(self, args: list[str]) -> str:
        if args != ["kill-pane", "-t", self.pane_id]:
            raise AssertionError(f"unexpected tmux command: {args}")
        self.killed_panes.append(self.pane_id)
        self.missing = True
        return ""


class OrchestrationOperatorSurfaceTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def invoke(self, args: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch(
            "lib.control.orchestration.cli.JjAdapter", return_value=self.jj,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["initiative", *args], env=self.env)
        return status, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def attention_view(*, salvage: bool = False) -> dict:
        initiative_id = "99999999-9999-4999-8999-999999999999"
        return {
            "initiative": {
                "initiative_id": initiative_id,
                "slug": "attention-test",
                "state": "awaiting-plan-approval" if not salvage else "running",
            },
            "plan": {"revision": 1, "digest": "d" * 64} if not salvage else None,
            "nodes": [],
            "attempts": [],
            "links": [],
            "actions": [],
            "approvals": ([{
                "state": "requested",
                "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            }] if salvage else []),
        }

    @staticmethod
    def observation_bundle(initiative: dict) -> dict:
        return {
            "contract": "asha.orchestration-bundle.v1",
            "bundle_id": "14141414-1414-4414-8414-141414141414",
            "initiative_id": initiative["initiative_id"],
            "aggregate_spec_digest": "a" * 64,
            "active_plan_digest": "b" * 64,
            "state": "binding",
            "members": [{
                "repository_id": initiative["scope"]["repository"]["repository_id"],
                "seal_id": "11111111-1111-4111-8111-111111111111",
                "jj_commit_id": "c" * 40,
                "tree_digest": "d" * 64,
                "diff_digest": "e" * 64,
                "materialization_id": "22222222-2222-4222-8222-222222222222",
                "review_id": "33333333-3333-4333-8333-333333333333",
                "verification_id": "44444444-4444-4444-8444-444444444444",
            }],
            "controller_evidence_ids": [],
            "outcome": None,
            "bound_at": None,
        }

    def save_observation_bundle(self) -> dict:
        bundle = self.observation_bundle(self.initiative())
        self.store.save_bundle(self.initiative_id, bundle)
        return bundle

    def test_attention_human_output_preserves_complete_runnable_commands(self) -> None:
        items = attention_items([
            self.attention_view(), self.attention_view(salvage=True),
        ])
        runnable = [
            item["resolution"] for item in items
            if item["resolution"].startswith("asha initiative")
        ]
        with mock.patch(
            "lib.control.orchestration.cli._attention_payload",
            return_value={
                "contract": "asha.orchestration-attention.v1", "items": items,
            },
        ):
            status, stdout, stderr = self.invoke(["attention"])
        self.assertEqual((status, stderr), (0, ""))
        for command in runnable:
            self.assertIn(command, stdout)

        with mock.patch(
            "lib.control.orchestration.cli._resolve",
            side_effect=lambda _store, initiative_id: {"initiative_id": initiative_id},
        ), mock.patch(
            "lib.control.orchestration.cli.refuse_coordinator_pane",
        ), mock.patch(
            "lib.control.orchestration.cli.approve_plan",
            return_value={"state": "completed"},
        ), mock.patch(
            "lib.control.orchestration.cli.approve_salvage",
            return_value={"state": "approved"},
        ):
            for command in runnable:
                with self.subTest(command=command):
                    argv = shlex.split(command)
                    self.assertEqual(argv[:2], ["asha", "initiative"])
                    status, _stdout, stderr = self.invoke(argv[2:])
                    self.assertEqual((status, stderr), (0, ""))

    def test_attention_json_preserves_full_detail_resolution_and_directive_id(self) -> None:
        detail = "created workspace description was truncated before the operator could act " * 2
        directive_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        view = self.attention_view()
        view["actions"] = [{
            "action_class": "directive",
            "action_id": directive_id,
            "outcome": json.dumps({
                "delivery": "pending", "node_id": "implementation-a",
            }),
        }]
        row = SimpleNamespace(
            task={"task_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"},
            display_state="needs-input",
            reconciliation={
                "evidence": [{"state": "needs-input", "detail": detail}],
                "blocker": None,
            },
            summary={"slug": "long-detail"},
        )
        items = attention_items([view], (row,))

        with mock.patch(
            "lib.control.orchestration.cli._attention_payload",
            return_value={
                "contract": "asha.orchestration-attention.v1", "items": items,
            },
        ):
            status, stdout, stderr = self.invoke(["attention", "--json"])
        self.assertEqual((status, stderr), (0, ""))
        emitted = json.loads(stdout)["items"]
        task = next(item for item in emitted if item["kind"] == "task")
        plan = next(item for item in emitted if item["kind"] == "plan-approval")
        directive = next(item for item in emitted if item["kind"] == "directive-pending")
        self.assertIn(detail, task["detail"])
        self.assertTrue(plan["resolution"].endswith("d" * 64))
        self.assertIn(directive_id, directive["detail"])

    def test_finalize_reports_pending_nodes_resume_prerequisite_and_cancellations(self) -> None:
        self.set_running(self.initiative())
        refused, _ = _operator_action(
            "finalize",
            [self.initiative_id, "--outcome", "failed", "--reason", "Stop."],
            self.store,
        )
        self.assertEqual(refused["state"], "refused")
        for node_id, state in (
            ("implementation-a", "ready"),
            ("review-a", "blocked"),
            ("verify-a", "blocked"),
        ):
            self.assertIn(f"{node_id} ({state})", refused["outcome"])

        current = self.initiative()
        needs_input = copy.deepcopy(current)
        needs_input.update({
            "state": "needs-input",
            "state_revision": current["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(
            needs_input, expected_digest=record_digest(current),
        )
        blocked, _ = _operator_action(
            "finalize",
            [self.initiative_id, "--outcome", "failed", "--reason", "Stop."],
            self.store,
        )
        self.assertEqual(blocked["state"], "refused")
        self.assertIn("initiative is needs-input", blocked["outcome"])
        self.assertIn(
            f"asha initiative resume {self.initiative_id}", blocked["outcome"],
        )

        resumed, _ = _operator_action("resume", [self.initiative_id], self.store)
        self.assertEqual(resumed["state"], "completed")
        completed, _ = _operator_action(
            "finalize",
            [
                self.initiative_id, "--outcome", "failed", "--reason", "Stop.",
                "--cancel-pending",
            ],
            self.store,
        )
        expected = ["implementation-a", "review-a", "verify-a"]
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["cancelled_node_ids"], expected)
        self.assertEqual(self.initiative()["state"], "failed")

    def test_show_and_snapshot_expose_bundle_ids_and_member_seals(self) -> None:
        bundle = self.save_observation_bundle()

        snapshot = _snapshot(self.store, self.initiative())
        shown = show_payload(self.store, self.initiative())

        self.assertEqual(snapshot["bundles"], [bundle])
        self.assertEqual(shown["bundles"], [bundle])
        self.assertEqual(shown["bundles"][0]["bundle_id"], bundle["bundle_id"])
        self.assertEqual(
            shown["bundles"][0]["members"][0]["seal_id"],
            bundle["members"][0]["seal_id"],
        )

    def test_snapshot_without_json_renders_an_operator_summary(self) -> None:
        bundle = self.save_observation_bundle()

        status, stdout, stderr = self.invoke(["snapshot", self.initiative_id])

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn(f"Initiative: execution-test ({self.initiative_id})", stdout)
        self.assertIn("State: approved", stdout)
        self.assertIn(
            f"Plan: revision {self.plan['revision']} digest {self.plan['digest']}",
            stdout,
        )
        self.assertIn("  implementation-a: approved", stdout)
        self.assertIn(f"  {bundle['bundle_id']}: binding", stdout)
        self.assertIn(bundle["members"][0]["seal_id"], stdout)
        self.assertIn("Last event sequence:", stdout)

    def test_terminal_operator_release_reaps_but_nonterminal_release_stays_anchored(self) -> None:
        tmux = ReapingFakeTmux()
        pane_env = {**self.env, "TMUX_PANE": "%7", "ASHA_HARNESS": "claude"}
        record = claim(self.store, self.initiative(), env=pane_env, tmux=tmux)
        operator_env = {**self.env, "TMUX_PANE": "%9"}
        with self.assertRaisesRegex(
            CoordinatorError, "not inside the coordinator's anchor pane",
        ):
            _coordinator_command(
                ["release", self.initiative_id, "--json"],
                self.store, operator_env, tmux,
            )

        current = self.initiative()
        terminal = copy.deepcopy(current)
        terminal.update({
            "state": "cancelled",
            "state_revision": current["state_revision"] + 1,
            "updated_at": now_text(),
        })
        self.store.save_initiative(
            terminal, expected_digest=record_digest(current),
        )
        payload, json_output = _coordinator_command(
            ["release", self.initiative_id, "--json"],
            self.store, operator_env, tmux,
        )

        self.assertTrue(json_output)
        self.assertEqual(payload["coordinator"]["state"], "exited")
        self.assertEqual(payload["coordinator"]["coordinator_id"], record["coordinator_id"])
        self.assertEqual(payload["reaped_pane_id"], "%7")
        self.assertEqual(tmux.killed_panes, ["%7"])

    def test_doctor_honours_an_armed_watch_and_the_observed_tail(self) -> None:
        tmux = FakeTmux()
        pane_env = {**self.env, "TMUX_PANE": "%7"}
        record = claim(self.store, self.initiative(), env=pane_env, tmux=tmux)
        cursor = self.initiative()["last_event_sequence"]
        observed: list[dict] = []
        self.assertEqual(_coordinator_cursor_probe(self.config).outcome, "mismatch")

        def sleep(_seconds: float) -> None:
            if observed:
                return
            live = self.store.read_coordinator(
                self.initiative_id, record["coordinator_id"],
            )
            observed.append(copy.deepcopy(live))
            observed.append({"doctor": _coordinator_cursor_probe(self.config)})
            append_event(
                self.store, self.initiative_id, "node-ready", ["implementation-a"],
                {"node_id": "implementation-a"},
                actor_kind="controller", actor_id="test",
            )

        with mock.patch.object(coordinator_module.time, "sleep", side_effect=sleep):
            payload = wait(
                self.store, self.initiative(), env=pane_env, tmux=tmux,
                after=cursor, timeout=5,
            )

        watch = observed[0]["armed_watch"]
        self.assertEqual(watch["after"], cursor)
        self.assertGreater(
            datetime.fromisoformat(watch["deadline"].replace("Z", "+00:00")).timestamp(),
            time.time(),
        )
        self.assertEqual(observed[1]["doctor"].outcome, "match")
        self.assertEqual(len(payload["events"]), 1)
        finished = self.store.read_coordinator(
            self.initiative_id, record["coordinator_id"],
        )
        self.assertIsNone(finished["armed_watch"])
        self.assertEqual(finished["event_cursor"], payload["events"][0]["sequence"])
        self.assertEqual(_coordinator_cursor_probe(self.config).outcome, "match")


if __name__ == "__main__":
    unittest.main()
