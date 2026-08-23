"""Coordinator claim/release/show from the Asha pane, fencing, and the approval split."""

from __future__ import annotations

import os
import subprocess
import unittest

from lib.control.orchestration import cli
from lib.control.orchestration.actions import build_action_document, submit_action
from lib.control.orchestration.coordinator import (
    ENV_COORDINATOR_ID,
    ENV_GENERATION,
    ENV_INITIATIVE_ID,
    PANE_COORDINATOR_OPTION,
    PANE_GENERATION_OPTION,
    PANE_INITIATIVE_OPTION,
    CoordinatorError,
    claim,
    refuse_coordinator_pane,
    release,
    show,
)
from lib.control.harness import caller_descends_from
from lib.control.tmux import PaneFacts, TmuxError
from tests.python.orchestration_execution_fixtures import ExecutionFixture


class FakeTmux:
    """One pane whose process is this test process (or an ancestor of it)."""

    def __init__(
        self, *, pane_id: str = "%7", pane_pid: int | None = None, session: str = "keeper",
        server: int | None = None,
    ) -> None:
        self.socket = None
        self.pane_id = pane_id
        self.pane_pid = os.getpid() if pane_pid is None else pane_pid
        self.session = session
        # The "server" must be a real process so its start identity exists;
        # the test's parent stands in for the tmux server.
        self.server = os.getppid() if server is None else server
        self.dead = False
        self.missing = False
        self.options: dict[tuple[str, str], str] = {}

    def pane_facts(self, pane_id: str) -> PaneFacts:
        if self.missing or pane_id != self.pane_id:
            raise TmuxError(f"can't find pane: {pane_id}")
        return PaneFacts(
            pane_id=self.pane_id, pane_pid=self.pane_pid, dead=self.dead,
            dead_status=None, dead_signal=None, session=self.session, window="0", title="",
        )

    def server_pid(self) -> int:
        return self.server

    def set_pane_option(self, pane_id: str, option: str, value: str) -> None:
        self.options[(pane_id, option)] = value

    def pane_option(self, pane_id: str, option: str) -> str | None:
        return self.options.get((pane_id, option))


class CoordinatorClaimTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def setUp(self) -> None:
        super().setUp()
        self.tmux = FakeTmux()
        self.pane_env = {**self.env, "TMUX_PANE": "%7", "ASHA_HARNESS": "claude"}

    def events(self) -> list[dict]:
        return self.store.list_events_snapshot(self.initiative_id)

    def test_claim_requires_a_tmux_pane(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "TMUX_PANE is unset"):
            claim(self.store, self.initiative(), env=self.env, tmux=self.tmux)
        self.assertIsNone(self.store.current_coordinator(self.initiative_id))

    def test_first_claim_anchors_the_pane_and_marks_it(self) -> None:
        before = len(self.events())
        record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        self.assertEqual(record["generation"], 1)
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["harness"], "claude")
        self.assertEqual(record["anchor"]["pane_id"], "%7")
        self.assertEqual(record["anchor"]["pane_pid"], os.getpid())
        self.assertEqual(record["anchor"]["session"], "keeper")
        self.assertIsNone(record["predecessor_coordinator_id"])
        self.assertEqual(self.store.current_coordinator(self.initiative_id), record)
        events = self.events()
        self.assertEqual(len(events), before + 1)
        self.assertEqual(events[-1]["type"], "coordinator-handshake-accepted")
        self.assertEqual(events[-1]["actor_kind"], "coordinator")
        self.assertEqual(events[-1]["actor_id"], f"coordinator:{record['coordinator_id']}")
        self.assertEqual(record["event_cursor"], events[-1]["sequence"] - 1)
        self.assertEqual(
            self.tmux.options[("%7", PANE_COORDINATOR_OPTION)], record["coordinator_id"],
        )
        self.assertEqual(self.tmux.options[("%7", PANE_INITIATIVE_OPTION)], self.initiative_id)
        self.assertEqual(self.tmux.options[("%7", PANE_GENERATION_OPTION)], "1")
        snapshot = cli._snapshot(self.store, self.initiative())
        self.assertEqual(snapshot["coordinator"], record)

    def test_claim_replay_from_the_same_pane_is_idempotent(self) -> None:
        first = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        count = len(self.events())
        again = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        self.assertEqual(again, first)
        self.assertEqual(len(self.events()), count)
        self.assertEqual(len(self.store.list_coordinators_snapshot(self.initiative_id)), 1)

    def test_new_pane_fences_the_live_generation(self) -> None:
        first = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        other = FakeTmux(pane_id="%8", pane_pid=os.getppid())
        second = claim(
            self.store, self.initiative(),
            env={**self.env, "TMUX_PANE": "%8"}, tmux=other, harness="codex",
        )
        self.assertEqual(second["generation"], 2)
        self.assertEqual(second["predecessor_coordinator_id"], first["coordinator_id"])
        self.assertEqual(second["harness"], "codex")
        fenced = self.store.read_coordinator(self.initiative_id, first["coordinator_id"])
        self.assertEqual(fenced["state"], "fenced")
        types = [event["type"] for event in self.events()[-3:]]
        self.assertEqual(
            types,
            ["coordinator-handshake-accepted", "coordinator-generation-fenced",
             "coordinator-handshake-accepted"],
        )
        self.assertEqual(self.store.current_coordinator(self.initiative_id), second)

    def test_exited_predecessor_is_left_alone(self) -> None:
        first = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        released = release(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        self.assertEqual(released["state"], "exited")
        self.assertEqual(self.tmux.options[("%7", PANE_COORDINATOR_OPTION)], "")
        count = len(self.events())
        second = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        self.assertEqual(second["generation"], 2)
        self.assertEqual(second["predecessor_coordinator_id"], first["coordinator_id"])
        self.assertEqual(
            self.store.read_coordinator(self.initiative_id, first["coordinator_id"])["state"],
            "exited",
        )
        self.assertEqual([e["type"] for e in self.events()[count:]], ["coordinator-handshake-accepted"])

    def test_release_requires_the_anchored_caller(self) -> None:
        claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        with self.assertRaisesRegex(CoordinatorError, "not inside the coordinator's anchor pane"):
            release(self.store, self.initiative(), env={**self.env, "TMUX_PANE": "%9"}, tmux=self.tmux)
        wrong_generation = {**self.pane_env, ENV_GENERATION: "7"}
        with self.assertRaisesRegex(CoordinatorError, "does not select this generation"):
            release(self.store, self.initiative(), env=wrong_generation, tmux=self.tmux)
        wrong_id = {**self.pane_env, ENV_COORDINATOR_ID: "ffffffff-ffff-4fff-8fff-ffffffffffff"}
        with self.assertRaisesRegex(CoordinatorError, "does not select this coordinator"):
            release(self.store, self.initiative(), env=wrong_id, tmux=self.tmux)
        replaced = FakeTmux(pane_id="%7", pane_pid=1)
        with self.assertRaisesRegex(CoordinatorError, "anchor pane identity changed"):
            release(self.store, self.initiative(), env=self.pane_env, tmux=replaced)
        self.tmux.missing = True
        with self.assertRaisesRegex(CoordinatorError, "anchor pane unavailable"):
            release(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        self.tmux.missing = False
        release(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        with self.assertRaisesRegex(CoordinatorError, "no live coordinator generation"):
            release(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)

    def test_show_reports_liveness_and_history(self) -> None:
        payload = show(self.store, self.initiative(), tmux=self.tmux)
        self.assertIsNone(payload["coordinator"])
        self.assertFalse(payload["anchor_live"])
        record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        payload = show(self.store, self.initiative(), tmux=self.tmux)
        self.assertEqual(payload["coordinator"], record)
        self.assertTrue(payload["anchor_live"])
        self.assertEqual(payload["generations"][0]["generation"], 1)
        self.tmux.missing = True
        payload = show(self.store, self.initiative(), tmux=self.tmux)
        self.assertFalse(payload["anchor_live"])
        self.assertIn("unavailable", payload["anchor_detail"])

    def test_reconcile_marks_a_vanished_anchor_stale_and_the_next_claim_fences_it(self) -> None:
        record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        live = cli.reconcile_one_initiative(self.store, self.initiative_id, tmux=self.tmux)
        self.assertEqual(live["coordinator_reconciliation"]["state"], "active")
        self.assertTrue(live["coordinator_reconciliation"]["anchor_live"])
        self.tmux.missing = True
        count = len(self.events())
        stale = cli.reconcile_one_initiative(self.store, self.initiative_id, tmux=self.tmux)
        self.assertEqual(stale["coordinator_reconciliation"]["state"], "stale")
        self.assertFalse(stale["coordinator_reconciliation"]["anchor_live"])
        self.assertEqual(self.store.read_coordinator(self.initiative_id, record["coordinator_id"])["state"], "stale")
        self.assertEqual(self.events()[-1]["type"], "reconciliation-conflict")
        self.assertEqual(self.events()[-1]["payload"]["subject"], "coordinator")
        self.assertEqual(len(self.events()), count + 1)
        # Stale stays stale on repeated reconcile (no second event), and cannot act.
        again = cli.reconcile_one_initiative(self.store, self.initiative_id, tmux=self.tmux)
        self.assertEqual(again["coordinator_reconciliation"]["state"], "stale")
        self.assertEqual(len(self.events()), count + 1)
        with self.assertRaisesRegex(CoordinatorError, "no live coordinator generation"):
            release(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        # A new pane claims generation 2 and fences the stale predecessor.
        other = FakeTmux(pane_id="%8", pane_pid=os.getppid())
        second = claim(self.store, self.initiative(), env={**self.env, "TMUX_PANE": "%8"}, tmux=other)
        self.assertEqual(second["generation"], 2)
        self.assertEqual(self.store.read_coordinator(self.initiative_id, record["coordinator_id"])["state"], "fenced")

    def test_caller_must_descend_from_the_pane_process(self) -> None:
        # A pane whose process is a sibling (not an ancestor) of this test process.
        child = subprocess.Popen(["sleep", "30"])
        self.addCleanup(child.kill)
        self.assertFalse(caller_descends_from(child.pid))
        self.assertTrue(caller_descends_from(os.getpid()))
        self.assertTrue(caller_descends_from(os.getppid()))
        sibling = FakeTmux(pane_id="%7", pane_pid=child.pid)
        with self.assertRaisesRegex(CoordinatorError, "does not descend from its tmux pane"):
            claim(self.store, self.initiative(), env=self.pane_env, tmux=sibling)
        self.assertIsNone(self.store.current_coordinator(self.initiative_id))
        # Claimed from the real pane, then the pane process is "replaced" by the sibling.
        claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        replaced = FakeTmux(pane_id="%7", pane_pid=child.pid)
        with self.assertRaisesRegex(CoordinatorError, "anchor pane identity changed"):
            release(self.store, self.initiative(), env=self.pane_env, tmux=replaced)

    def test_a_different_tmux_server_cannot_judge_the_anchor(self) -> None:
        record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        elsewhere = FakeTmux(pane_id="%7", server=9999)
        payload = show(self.store, self.initiative(), tmux=elsewhere)
        self.assertIsNone(payload["anchor_live"])
        self.assertIn("differs from the anchor server", payload["anchor_detail"])
        count = len(self.events())
        result = cli.reconcile_one_initiative(self.store, self.initiative_id, tmux=elsewhere)
        self.assertEqual(result["coordinator_reconciliation"]["state"], "active")
        self.assertIsNone(result["coordinator_reconciliation"]["anchor_live"])
        self.assertEqual(len(self.events()), count)
        self.assertEqual(self.store.read_coordinator(self.initiative_id, record["coordinator_id"])["state"], "active")
        with self.assertRaisesRegex(CoordinatorError, "differs from the anchor server"):
            release(self.store, self.initiative(), env=self.pane_env, tmux=elsewhere)

    def test_session_rename_does_not_change_identity(self) -> None:
        record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        self.tmux.session = "renamed"
        again = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        self.assertEqual(again["coordinator_id"], record["coordinator_id"])
        result = cli.reconcile_one_initiative(self.store, self.initiative_id, tmux=self.tmux)
        self.assertTrue(result["coordinator_reconciliation"]["anchor_live"])
        released = release(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        self.assertEqual(released["state"], "exited")

    def test_operator_verbs_refuse_the_coordinator_pane_wholesale(self) -> None:
        claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)
        for verb, args in (("pause", []), ("activate", []), ("dispatch", ["--node", "implementation-a"])):
            with self.subTest(verb=verb), self.assertRaisesRegex(CoordinatorError, "refused from the coordinator's pane"):
                cli._operator_action(verb, [self.initiative_id, *args], self.store, self.pane_env, self.tmux)
        plan_file = self.root / "plan.json"
        with self.assertRaisesRegex(CoordinatorError, "refused from the coordinator's pane"):
            cli._plan([self.initiative_id, "--file", str(plan_file)], self.store, self.config, jj=None, env=self.pane_env, tmux=self.tmux)
        shown, _ = cli._plan([self.initiative_id, "--show"], self.store, self.config, jj=None, env=self.pane_env, tmux=self.tmux)
        self.assertEqual(shown["revision"], self.plan["revision"])
        self.assertEqual(self.store.list_actions_snapshot(self.initiative_id), [])

    def test_cli_coordinator_verbs_round_trip(self) -> None:
        payload, json_output = cli._coordinator_command(
            ["claim", self.initiative_id, "--json"], self.store, self.pane_env, self.tmux,
        )
        self.assertTrue(json_output)
        self.assertEqual(payload["contract"], cli.COORDINATOR_CLAIM_CONTRACT)
        self.assertEqual(payload["environment"][ENV_INITIATIVE_ID], self.initiative_id)
        self.assertEqual(payload["environment"][ENV_GENERATION], "1")
        shown, _ = cli._coordinator_command(["show", self.initiative_id], self.store, self.pane_env, self.tmux)
        self.assertEqual(shown["coordinator"]["coordinator_id"], payload["coordinator"]["coordinator_id"])
        released, _ = cli._coordinator_command(["release", self.initiative_id], self.store, self.pane_env, self.tmux)
        self.assertEqual(released["coordinator"]["state"], "exited")
        with self.assertRaisesRegex(ValueError, "requires claim, release, or show"):
            cli._coordinator_command(["start", self.initiative_id], self.store, self.pane_env, self.tmux)
        with self.assertRaisesRegex(ValueError, "does not accept --harness"):
            cli._coordinator_command(
                ["release", self.initiative_id, "--harness", "codex"], self.store, self.pane_env, self.tmux,
            )


class ApprovalSplitTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def setUp(self) -> None:
        super().setUp()
        self.tmux = FakeTmux()
        self.pane_env = {**self.env, "TMUX_PANE": "%7"}
        self.record = claim(self.store, self.initiative(), env=self.pane_env, tmux=self.tmux)

    def test_approval_verbs_refuse_the_coordinator_pane_and_session(self) -> None:
        digest = self.plan["digest"]
        for call in (
            lambda env: cli._approve([self.initiative_id, "--digest", digest], self.store, env, self.tmux),
            lambda env: cli._reject(
                [self.initiative_id, "--digest", digest, "--reason", "no"], self.store, env, self.tmux,
            ),
            lambda env: cli._approve_salvage_command(
                [self.initiative_id, "--request", "ffffffff-ffff-4fff-8fff-ffffffffffff"],
                self.store, env, self.tmux,
            ),
        ):
            with self.assertRaisesRegex(CoordinatorError, "refused from the coordinator's pane"):
                call(self.pane_env)
            with self.assertRaisesRegex(CoordinatorError, "refused inside a coordinator session"):
                call({**self.env, ENV_COORDINATOR_ID: self.record["coordinator_id"]})
        # The Keeper's own pane passes the split; the verb then proceeds on its own rules.
        refuse_coordinator_pane(self.store, self.initiative_id, {**self.env, "TMUX_PANE": "%2"}, self.tmux)
        refuse_coordinator_pane(self.store, self.initiative_id, self.env, self.tmux)

    def test_decide_via_action_file_refuses_the_coordinator_pane(self) -> None:
        document = build_action_document(self.initiative(), "decide", {"decision_action_id": "x"})
        path = self.root / "decide.json"
        import json

        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(CoordinatorError, "refused from the coordinator's pane"):
            cli._action_command([self.initiative_id, "--file", str(path), "--json"], self.store, self.pane_env, self.tmux)

    def test_operator_only_classes_are_journaled_then_refused_for_the_coordinator(self) -> None:
        operator_only = {
            "activate-initiative": {}, "resume": {}, "cancel-node": {"node_id": "implementation-a"},
            "finalize": {"outcome": "failed", "reason": "x"}, "archive": {}, "unarchive": {},
            "decide": {"paused_seal_id": "ffffffff-ffff-4fff-8fff-ffffffffffff", "decision": "go"},
        }
        for action_class, payload in operator_only.items():
            document = build_action_document(
                self.initiative(), action_class, payload,
                actor_id=f"coordinator:{self.record['coordinator_id']}", coordinator=self.record,
            )
            result = submit_action(self.store, self.initiative_id, document)
            with self.subTest(action_class=action_class):
                self.assertEqual(result["actor_kind"], "coordinator")
                self.assertEqual(result["state"], "refused")
                self.assertIn("not available to the coordinator actor", result["outcome"])
                stored = self.store.read_action(self.initiative_id, document["action_id"])
                self.assertEqual(stored["state"], "refused")
                self.assertEqual(stored["coordinator_generation"], 1)
            replay = submit_action(self.store, self.initiative_id, document)
            self.assertEqual(replay, result)

    def test_fenced_and_unknown_generations_are_refused(self) -> None:
        other = FakeTmux(pane_id="%8", pane_pid=os.getppid())
        second = claim(self.store, self.initiative(), env={**self.env, "TMUX_PANE": "%8"}, tmux=other)
        stale = build_action_document(
            self.initiative(), "pause", {}, actor_id="coordinator:stale", coordinator=self.record,
        )
        result = submit_action(self.store, self.initiative_id, stale)
        self.assertEqual(result["state"], "refused")
        self.assertIn("generation 1 is fenced; current generation is 2", result["outcome"])
        unknown = build_action_document(
            self.initiative(), "pause", {}, actor_id="coordinator:ghost",
            coordinator={"coordinator_id": "ffffffff-ffff-4fff-8fff-ffffffffffff", "generation": 2},
        )
        result = submit_action(self.store, self.initiative_id, unknown)
        self.assertEqual(result["state"], "refused")
        self.assertIn("is fenced", result["outcome"])
        self.assertEqual(second["generation"], 2)

    def test_action_file_from_the_wrong_pane_is_refused_before_journaling(self) -> None:
        import json

        document = build_action_document(
            self.initiative(), "pause", {},
            actor_id=f"coordinator:{self.record['coordinator_id']}", coordinator=self.record,
        )
        path = self.root / "pause.json"
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(CoordinatorError, "not inside the coordinator's anchor pane"):
            cli._action_command(
                [self.initiative_id, "--file", str(path), "--json"], self.store,
                {**self.env, "TMUX_PANE": "%9"}, self.tmux,
            )
        with self.assertRaises(Exception):
            self.store.read_action(self.initiative_id, document["action_id"])


if __name__ == "__main__":
    unittest.main()
