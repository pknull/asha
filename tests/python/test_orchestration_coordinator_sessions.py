"""Control-launched coordinator sessions: launch at the projects root, list, attach."""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from lib.control.orchestration.coordinator import (
    COORDINATOR_ATTACH_CONTRACT,
    COORDINATOR_LAUNCH_CONTRACT,
    COORDINATOR_SESSIONS_CONTRACT,
    CoordinatorError,
    attach_target,
    claim,
    launch_prompt,
    launch_session,
    list_coordinator_sessions,
    release,
)
from lib.control.orchestration import cli
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_orchestration_coordinator_claim import FakeTmux


class LaunchingTmux(FakeTmux):
    """The claim-test pane double plus the session-creation surface launch uses."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.created: list[dict] = []
        self.respawned: list[tuple[str, list[str]]] = []
        self.sessions: set[str] = {self.session}

    def create_task_session(self, **kwargs) -> str:
        self.created.append(kwargs)
        self.sessions.add(kwargs["session"])
        return "%42"

    def respawn(self, pane_id: str, argv: list[str]) -> None:
        self.respawned.append((pane_id, list(argv)))

    def list_sessions(self) -> list[str]:
        return sorted(self.sessions)

    def has_session(self, name: str) -> bool:
        return name in self.sessions


class CoordinatorSessionTests(ExecutionFixture, unittest.TestCase):
    start_running = False

    def setUp(self) -> None:
        super().setUp()
        self.tmux = LaunchingTmux()
        self.pane_env = {**self.env, "TMUX_PANE": self.tmux.pane_id}
        self.asha_root = Path(__file__).resolve().parents[2]
        self.control = self.config.control

    def test_launch_starts_a_prefixed_session_running_the_full_persona_harness_with_the_intent(self) -> None:
        launched = launch_session(
            self.control, root=self.root, intent="  update   termart:\nadd a graph  ",
            tmux=self.tmux, asha_root=self.asha_root, token="abcd1234",
        )
        self.assertEqual(launched["contract"], COORDINATOR_LAUNCH_CONTRACT)
        self.assertEqual(launched["session"], f"{self.control.session_prefix}coord-abcd1234")
        self.assertEqual(launched["intent"], "update termart: add a graph")
        self.assertEqual(launched["root"], str(self.root))
        created = self.tmux.created[0]
        self.assertEqual(created["session"], launched["session"])
        self.assertEqual(created["window"], "coordinator")
        self.assertEqual(str(created["start_directory"]), str(self.root))
        self.assertEqual(created["session_options"], {"@asha_coordinator_session": "1"})
        self.assertNotIn("@asha_managed", created["session_options"])
        self.assertEqual(created["pane_options"]["@asha_coordinator_launch"], "abcd1234")
        # The pane inherits the tmux server's env, so the launch must carry
        # the asha home explicitly alongside the coordinator token.
        self.assertEqual(created["environment"], {
            "ASHA_COORDINATOR_LAUNCH": "abcd1234",
            "ASHA_HOME": str(self.config.asha_home),
        })
        pane_id, argv = self.tmux.respawned[0]
        self.assertEqual(pane_id, "%42")
        self.assertEqual(argv[:2], [str(self.asha_root / "bin" / "asha"), "claude"])
        self.assertEqual(argv[2], launch_prompt("update termart: add a graph"))
        self.assertIn("session-orchestrate-initiative", argv[2])
        self.assertNotIn("ASHA_PERSONA", created["environment"])

    def test_launch_refuses_empty_oversized_or_rootless_intents_without_touching_tmux(self) -> None:
        for intent in ("", "   \n", "x" * 2001):
            with self.assertRaises(CoordinatorError):
                launch_session(self.control, root=self.root, intent=intent, tmux=self.tmux, asha_root=self.asha_root)
        with self.assertRaisesRegex(CoordinatorError, "not a directory"):
            launch_session(self.control, root=self.root / "missing", intent="go", tmux=self.tmux, asha_root=self.asha_root)
        self.assertEqual(self.tmux.created, [])
        self.assertEqual(self.tmux.respawned, [])

    def test_sessions_list_only_coordinator_sessions_and_bind_their_claims(self) -> None:
        launch_session(self.control, root=self.root, intent="one", tmux=self.tmux, asha_root=self.asha_root, token="11111111")
        self.tmux.sessions.add(f"{self.control.session_prefix}task-foreign")
        listed = list_coordinator_sessions(self.control, store=self.store, tmux=self.tmux)
        self.assertEqual(listed["contract"], COORDINATOR_SESSIONS_CONTRACT)
        self.assertEqual([item["session"] for item in listed["sessions"]], [f"{self.control.session_prefix}coord-11111111"])
        self.assertIsNone(listed["sessions"][0]["initiative_id"])
        # A claim anchored to the keeper pane's session binds that session to the initiative.
        record = claim(self.store, self.store.peek(self.initiative_id), env=self.pane_env, tmux=self.tmux)
        self.tmux.sessions.add(self.control.session_prefix + "coord-keeper")
        bound = list_coordinator_sessions(self.control, store=self.store, tmux=self.tmux)
        names = {item["session"]: item for item in bound["sessions"]}
        self.assertIn(f"{self.control.session_prefix}coord-keeper", names)
        self.assertEqual(record["anchor"]["session"], "keeper")

    def test_attach_target_needs_a_live_claim_or_an_existing_named_session(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "no coordinator has claimed"):
            attach_target(self.store, tmux=self.tmux, initiative_id=self.initiative_id)
        record = claim(self.store, self.store.peek(self.initiative_id), env=self.pane_env, tmux=self.tmux)
        target = attach_target(self.store, tmux=self.tmux, initiative_id=self.initiative_id)
        self.assertEqual(target["contract"], COORDINATOR_ATTACH_CONTRACT)
        self.assertEqual(target["session"], record["anchor"]["session"])
        self.assertEqual(target["pane_id"], record["anchor"]["pane_id"])
        self.assertEqual(target["generation"], 1)
        release(self.store, self.store.peek(self.initiative_id), env=self.pane_env, tmux=self.tmux)
        with self.assertRaisesRegex(CoordinatorError, "current coordinator generation is exited"):
            attach_target(self.store, tmux=self.tmux, initiative_id=self.initiative_id)
        named = attach_target(self.store, tmux=self.tmux, session="keeper")
        self.assertEqual(named["session"], "keeper")
        self.assertIsNone(named["coordinator_id"])
        with self.assertRaisesRegex(CoordinatorError, "is not running"):
            attach_target(self.store, tmux=self.tmux, session="absent")
        with self.assertRaisesRegex(CoordinatorError, "exactly one"):
            attach_target(self.store, tmux=self.tmux)

    def test_cli_verbs_route_launch_sessions_and_attach(self) -> None:
        env = {**self.env, "ASHA_ROOT": str(self.asha_root)}
        launched, as_json = cli._coordinator_command(
            ["launch", "--root", str(self.root), "--intent", "ship it", "--json"],
            self.store, env, self.tmux, config=self.config,
        )
        self.assertTrue(as_json)
        self.assertTrue(launched["session"].startswith(f"{self.control.session_prefix}coord-"))
        self.assertEqual(self.tmux.respawned[0][1][1], "claude")
        listed, _ = cli._coordinator_command(["sessions", "--json"], self.store, env, self.tmux, config=self.config)
        self.assertEqual(listed["sessions"][0]["session"], launched["session"])
        with self.assertRaisesRegex(ValueError, "missing required option"):
            cli._coordinator_command(["launch", "--json"], self.store, env, self.tmux, config=self.config)
        target, _ = cli._coordinator_command(
            ["attach", "--session", launched["session"], "--json"], self.store, env, self.tmux, config=self.config,
        )
        self.assertEqual(target["session"], launched["session"])


if __name__ == "__main__":
    unittest.main()
