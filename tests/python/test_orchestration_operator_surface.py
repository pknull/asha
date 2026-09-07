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

    def test_attention_verb_parks_idle_demand_but_keeps_live_asks_under_a_paused_head(self) -> None:
        """`asha initiative attention` runs the real assembler over parked heads."""
        def head(index: str, slug: str, state: str, **extra) -> dict:
            return {
                "initiative": {
                    "initiative_id": f"{index * 8}-1111-4111-8111-111111111111",
                    "slug": slug, "state": state,
                },
                "plan": None, "nodes": [], "attempts": [], "links": [], "actions": [],
                "approvals": [], "events": [], "coordinator": None, "coordinator_live": None,
                **extra,
            }

        worker_task = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        attempt = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
        idle_coordinator = {
            "harness": "claude", "generation": 1, "state": "active",
            "updated_at": "2000-01-01T00:00:00Z", "anchor": {"pane_id": "%7"},
        }
        idle_nodes = [
            {"node_id": "decision-a", "state": "needs-input", "type": "work"},
            {"node_id": "ready-a", "state": "ready", "type": "work"},
        ]
        question = {
            "sequence": 3, "event_id": "77777777-7777-4777-8777-777777777777",
            "type": "approval-requested", "actor_kind": "coordinator",
            "recorded_at": "2026-09-07T10:00:00Z",
            "payload": {
                "kind": "operator-decision", "subject_id": "implementation-a",
                "question": "Which base should the retry use?",
            },
        }
        views = [
            head("1", "parked-idle", "paused", nodes=idle_nodes,
                 coordinator=idle_coordinator, coordinator_live=True),
            head("2", "parked-asked", "paused", approvals=[{
                "state": "requested", "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            }]),
            head("3", "parked-live", "paused",
                 nodes=[{"node_id": "review-a", "state": "running", "type": "review"}],
                 attempts=[{"attempt_id": attempt, "node_id": "review-a", "ordinal": 1, "state": "running"}],
                 links=[{"attempt_id": attempt, "control_task_id": worker_task}]),
            head("4", "resumed", "running", nodes=idle_nodes,
                 coordinator=idle_coordinator, coordinator_live=True),
            # An initiative-only question: no node or approval demand at all.
            head("5", "asked-operator", "needs-input", events=[question]),
            # The same question parked: status, not demand, until it resumes.
            head("6", "parked-question", "paused", events=[question]),
            # A needs-input head whose question event is not in the loaded tail.
            head("7", "waiting-plain", "needs-input"),
        ]
        row = SimpleNamespace(
            task={"task_id": worker_task},
            display_state="needs-input",
            reconciliation={
                "evidence": [{"state": "needs-input", "detail": "pane shows the input prompt"}],
                "blocker": None,
            },
            summary={"slug": "review-worker"},
        )
        with mock.patch(
            "lib.control.tui._load_initiative_views", return_value=views,
        ), mock.patch(
            "lib.control.cli._load_rows_for_attention", return_value=(row,),
        ):
            status, stdout, stderr = self.invoke(["attention", "--json"])
        self.assertEqual((status, stderr), (0, ""))
        items = json.loads(stdout)["items"]
        by_slug: dict = {}
        for item in items:
            by_slug.setdefault(item["slug"], []).append((item["kind"], item.get("node_id")))
        # The decision and the parked coordinator are parked with their head.
        self.assertNotIn("parked-idle", by_slug)
        self.assertEqual(by_slug["parked-asked"], [("salvage-approval", None)])
        self.assertEqual(by_slug["parked-live"], [("worker", "review-a")])
        self.assertEqual(
            sorted(by_slug["resumed"]),
            [("coordinator-parked", "ready-a"), ("needs-input", "decision-a")],
        )
        live = next(item for item in items if item["slug"] == "parked-live")
        self.assertEqual(live["detail"], "at prompt: pane shows the input prompt")
        self.assertEqual(live["task_id"], worker_task)
        # The operator's own question is demand with nothing beneath it;
        # parked, it leaves the verb exactly as the head leaves the tree.
        self.assertEqual(by_slug["asked-operator"], [("operator-decision", None)])
        self.assertEqual(by_slug["waiting-plain"], [("operator-decision", None)])
        self.assertNotIn("parked-question", by_slug)
        asked = next(item for item in items if item["slug"] == "asked-operator")
        self.assertEqual(asked["detail"], "operator decision: Which base should the retry use?")
        self.assertEqual(
            asked["resolution"],
            "answer, then asha initiative resume 55555555-1111-4111-8111-111111111111",
        )
        plain = next(item for item in items if item["slug"] == "waiting-plain")
        self.assertEqual(plain["detail"], "initiative waits on the operator")
        self.assertEqual(
            plain["resolution"],
            "answer, then asha initiative resume 77777777-1111-4111-8111-111111111111",
        )
        # The human renderer prints the same items with their resolutions whole.
        with mock.patch(
            "lib.control.tui._load_initiative_views", return_value=views,
        ), mock.patch(
            "lib.control.cli._load_rows_for_attention", return_value=(row,),
        ):
            status, text, stderr = self.invoke(["attention"])
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("operator-decision", text)
        self.assertIn("asked-operator", text)
        self.assertIn("operator decision: Which base should the retry use?", text)
        self.assertIn(
            "-> answer, then asha initiative resume 55555555-1111-4111-8111-111111111111", text,
        )
        self.assertNotIn("parked-question", text)
        self.assertNotIn("parked-idle", text)

    def test_attention_verb_reads_an_edge_less_answer_the_journal_cannot_show(self) -> None:
        """`asha initiative attention` uses the same classifier the tree and park use.

        The paused-seal writer returns a `running` head to `needs-input` with
        no `initiative-state-changed` event, so an answer interrupted after
        its own head write leaves no edge for the journal to read.  The verb
        must not quote the answered question back at the operator, and it
        must still quote one whose answer never wrote.
        """
        initiative_id = "88888888-1111-4111-8111-111111111111"
        plan_digest = "e" * 64
        question = {
            "sequence": 6, "event_id": "77777777-7777-4777-8777-777777777777",
            "type": "approval-requested", "actor_kind": "coordinator",
            "recorded_at": "2026-09-07T10:00:00Z",
            "payload": {
                "kind": "operator-decision", "subject_id": "implementation-a",
                "question": "Which base should the retry use?",
            },
        }
        opened = {
            "sequence": 7, "event_id": "66666666-6666-4666-8666-666666666666",
            "type": "initiative-state-changed", "actor_kind": "controller",
            "recorded_at": "2026-09-07T10:00:01Z",
            "payload": {"from": "running", "to": "needs-input"},
        }

        def view(*, state_revision: int) -> dict:
            return {
                "initiative": {
                    "initiative_id": initiative_id, "slug": "edge-less-answer",
                    "state": "needs-input", "state_revision": state_revision,
                    "last_event_sequence": 7,
                    "active_plan": {"revision": 1, "digest": plan_digest},
                },
                "plan": None, "nodes": [], "attempts": [], "links": [],
                "approvals": [], "events": [question, opened],
                "coordinator": None, "coordinator_live": None,
                "actions": [{
                    "action_id": "55555555-5555-4555-8555-555555555555",
                    "initiative_id": initiative_id, "action_class": "resume",
                    "active_plan_digest": plan_digest, "state": "indeterminate",
                    "outcome": json.dumps({
                        "resume_from": "needs-input", "resume_from_revision": 20,
                        "resume_from_sequence": 7, "resume_to": "running",
                        "restored_question_event_id": None, "status": "indeterminate",
                    }),
                }],
            }

        def details(views: list[dict]) -> list[str]:
            with mock.patch(
                "lib.control.tui._load_initiative_views", return_value=views,
            ), mock.patch(
                "lib.control.cli._load_rows_for_attention", return_value=(),
            ):
                status, stdout, stderr = self.invoke(["attention", "--json"])
            self.assertEqual((status, stderr), (0, ""))
            return [item["detail"] for item in json.loads(stdout)["items"]]

        # Two head writes after the answer's proof: its own, then the seal's.
        self.assertEqual(details([view(state_revision=22)]), ["initiative waits on the operator"])
        # The same records with no head write since the proof: unanswered.
        self.assertEqual(
            details([view(state_revision=20)]),
            ["operator decision: Which base should the retry use?"],
        )
        # The assembler and the model agree record for record.
        self.assertEqual(
            [item["detail"] for item in attention_items([view(state_revision=22)])],
            ["initiative waits on the operator"],
        )

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
