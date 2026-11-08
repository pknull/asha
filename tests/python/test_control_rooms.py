"""Project-bound, full-persona Asha Rooms."""

from __future__ import annotations

import hashlib
import contextlib
import io
import json
import os
import tempfile
import subprocess
import shutil
import sys
import threading
import time
import unittest
import unittest.mock
import uuid
from pathlib import Path
from types import SimpleNamespace

from lib.control import rooms as rooms_module
from lib.control.rooms import (
    PANE_ROOM_OPTION,
    ROOM_LIST_CONTRACT,
    SESSION_ROOM_OPTION,
    RoomError,
    RoomStore,
    attach_room,
    close_room,
    list_rooms,
    open_room,
    room_launch_argv,
)
from lib.control.tmux import PaneFacts, TmuxError, tmux_server_absent
from lib.control.tmux import TmuxAdapter
from tests.python.socket_reaper import TmuxSocketReaper
from lib.control import cli


class FakeTmux:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.created: list[dict] = []
        self.respawned: list[tuple[str, list[str]]] = []
        self.killed: list[str] = []
        # Every kill_owned_room call, refused or not.
        self.kill_attempts: list[str] = []
        self.session_options: dict[tuple[str, str], str] = {}
        self.pane_options: dict[tuple[str, str], str] = {}
        self.pane_id = "%42"
        self.dead = False
        self.fail_respawn = False
        self.fail_create_after_markers = False
        self.session_id_exceptions: list[BaseException] = []
        self.respawn_exception: BaseException | None = None
        self.session_identity = "$7"
        self.replace_before_owned_action = False
        # Clients attached to the Room session.
        self.attached = 0
        self.before_kill = None
        # Lines typed through the operator-initiated ``send_line`` seam.
        self.sent: list[tuple[str, str]] = []
        # The diagnostic every pane read fails with while no server answers (#140);
        # creating a session starts a server again, as tmux does.
        self.server_error: str | None = None
        # Session name by ID, and the IDs the server hands the next new pane.
        self.session_names: dict[str, str] = {}
        self.next_ids: list[tuple[str, str]] = []

    executable = "tmux"
    socket = None

    def _server(self) -> None:
        if self.server_error is not None:
            raise TmuxError(self.server_error)

    def _current_session(self) -> str | None:
        name = self.session_names.get(self.session_identity)
        return name if name in self.sessions else next(iter(self.sessions), None)

    def has_session(self, name: str) -> bool:
        # The real adapter reads a missing server as no session.
        return self.server_error is None and name in self.sessions

    def create_task_session(self, **kwargs) -> str:
        self.server_error = None
        if kwargs["session"] in self.sessions:
            raise TmuxError("duplicate session")
        if self.next_ids:
            self.pane_id, self.session_identity = self.next_ids.pop(0)
        self.created.append(kwargs)
        self.sessions.add(kwargs["session"])
        self.session_names[self.session_identity] = kwargs["session"]
        for key, value in kwargs["session_options"].items():
            self.session_options[(kwargs["session"], key)] = value
        for key, value in kwargs["pane_options"].items():
            self.pane_options[(self.pane_id, key)] = value
        if self.fail_create_after_markers:
            raise TmuxError("creation reply lost")
        return self.pane_id

    def respawn(self, pane_id: str, argv: list[str]) -> None:
        self.respawned.append((pane_id, list(argv)))
        if self.respawn_exception is not None:
            raise self.respawn_exception
        if self.fail_respawn:
            raise TmuxError("respawn uncertain")

    def session_id(self, pane_id: str) -> str:
        self._server()
        if self.session_id_exceptions:
            raise self.session_id_exceptions.pop(0)
        if pane_id != self.pane_id or not self.sessions:
            raise TmuxError("missing pane")
        return self.session_identity

    def room_attach_argv(self, **identity) -> list[str]:
        return [
            self.executable, "if-shell", "-F", "-t", identity["pane_id"],
            f"owned:{identity['room_id']}:{identity['project_marker']}",
            f"attach-session -t {identity['session_id']}",
            "display-message -p ASHA_ROOM_REFUSED ; run-shell \"exit 66\"",
        ]

    def kill_owned_room(self, **identity) -> None:
        self.kill_attempts.append(identity["session_id"])
        self._server()
        if self.before_kill is not None:
            self.before_kill()
        if self.replace_before_owned_action:
            session = next(iter(self.sessions))
            self.session_options[(session, SESSION_ROOM_OPTION)] = "foreign"
        session = self._current_session()
        if (
            identity["session_id"] != self.session_identity
            or identity["pane_id"] != self.pane_id
            or session is None
            or self.session_options.get((session, SESSION_ROOM_OPTION))
            != identity["room_id"]
        ):
            raise TmuxError("room ownership changed; no session was killed")
        self.killed.append(identity["session_id"])
        self.sessions.clear()

    def send_line(self, pane_id: str, text: str) -> None:
        self._server()
        if pane_id != self.pane_id or not self.sessions:
            raise TmuxError("missing pane")
        self.sent.append((pane_id, text))

    def session_option(self, session: str, option: str) -> str | None:
        self._server()
        if session == self.session_identity:
            session = self._current_session() or session
        return self.session_options.get((session, option))

    def pane_option(self, pane: str, option: str) -> str | None:
        self._server()
        return self.pane_options.get((pane, option))

    def pane_facts(self, pane: str) -> PaneFacts:
        self._server()
        if pane != self.pane_id or not self.sessions:
            raise TmuxError("missing pane")
        session = self._current_session()
        return PaneFacts(pane, 1234, self.dead, 0 if self.dead else None, None,
                         session, "room", "asha:room")

    def window_pane_facts(self, session: str, window: str) -> PaneFacts:
        self._server()
        if session not in self.sessions or window != "room":
            raise TmuxError("missing window")
        return PaneFacts(self.pane_id, 1234, False, None, None,
                         session, window, "asha:room")

    def kill_session(self, name: str) -> None:
        self.killed.append(name)
        self.sessions.discard(name)


class RoomTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.asha_home = self.root / "asha-home"
        self.project = self.root / "projects" / "novel"
        (self.project / ".asha").mkdir(parents=True)
        (self.project / "Memory").mkdir()
        (self.project / ".asha/config.json").write_text(json.dumps({
            "initialized": True,
            "memory_version": 2,
            "project_id": "novel-project",
            "name": "My Novel",
        }), encoding="utf-8")
        self.config = SimpleNamespace(
            asha_home=self.asha_home,
            session_prefix="asha-control-",
            popup_width="90%",
            popup_height="85%",
        )
        self.env = {
            "HOME": str(self.root),
            "ASHA_HOME": str(self.asha_home),
            "ASHA_PROJECTS_ROOT": str(self.root / "projects"),
        }
        self.asha_root = Path(__file__).resolve().parents[2]
        self.tmux = FakeTmux()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _open(self, **overrides):
        values = {
            "name": "Draft Room",
            "project": str(self.project),
            "harness": "codex",
            "prompt": "Help me revise chapter one.",
            "config": self.config,
            "env": self.env,
            "tmux": self.tmux,
            "asha_root": self.asha_root,
            "executable_finder": lambda _name: "/usr/bin/true",
            "room_id": "11111111-1111-4111-8111-111111111111",
        }
        values.update(overrides)
        return open_room(**values)

    def test_harness_specific_room_argv_keeps_prompt_one_argument(self) -> None:
        prompt = "line one\nline two; still data"
        expected = {
            "claude": [prompt],
            "codex": ["--no-daemon", prompt],
            "copilot": ["--interactive", prompt],
            "opencode": ["--prompt", prompt],
        }
        for harness, tail in expected.items():
            with self.subTest(harness=harness):
                self.assertEqual(
                    room_launch_argv(self.asha_root, harness, prompt),
                    [str(self.asha_root / "bin/asha"), harness, *tail],
                )

    def test_codex_room_argv_never_joins_the_shared_app_server_daemon(self) -> None:
        # Codex 0.157 runs daemon threads' hooks with the daemon's frozen
        # environment, so they report under another session's identity (#100).
        launcher = str(self.asha_root / "bin/asha")
        self.assertEqual(room_launch_argv(self.asha_root, "codex", "P"),
                         [launcher, "codex", "--no-daemon", "P"])
        self.assertEqual(room_launch_argv(self.asha_root, "codex", "P", resume_id="thread-1"),
                         [launcher, "codex", "resume", "--no-daemon", "thread-1", "P"])
        for harness in ("claude", "copilot", "opencode"):
            with self.subTest(harness=harness):
                self.assertNotIn("--no-daemon", room_launch_argv(self.asha_root, harness, "P"))

    def test_open_persists_identity_then_launches_detached_in_exact_project(self) -> None:
        result = self._open()

        self.assertEqual(result["room_id"], "11111111-1111-4111-8111-111111111111")
        self.assertEqual(result["state"], "open")
        self.assertEqual(result["project_root"], str(self.project))
        self.assertIn(result["room_id"], result["attach"])
        self.assertIn("$7", result["attach"])
        self.assertNotIn(f"-t {result['session']}", result["attach"])
        created = self.tmux.created[0]
        self.assertEqual(created["start_directory"], self.project)
        self.assertEqual(created["session_options"], {SESSION_ROOM_OPTION: result["room_id"]})
        self.assertEqual(created["pane_options"][PANE_ROOM_OPTION], result["room_id"])
        self.assertEqual(created["environment"], {
            "ASHA_HOME": str(self.asha_home),
            "ASHA_PERSONA": "1", "ASHA_SESSION_PROFILE": "room",
            "ASHA_ORCHESTRATOR_STANCE": "0",
            "ASHA_ROOM_ID": result["room_id"],
            "ASHA_CODEX_CMD": "codex",
        })
        argv = self.tmux.respawned[0][1]
        for key in {
            "ASHA_SEAT", "ASHA_COORDINATOR_LAUNCH", "ASHA_CONTROL_MANAGED",
            "ASHA_CONTROL_TASK_ID", "ASHA_CONTROL_RUN_ID",
            "ASHA_CONTROL_STATE_DIR", "ASHA_CONTROL_RESULT_TOKEN",
            "ASHA_CONTROL_RESULT_OUTBOX", "ASHA_CONTROL_RESULT_INGESTION_ID",
            "ASHA_ORCHESTRATION_INITIATIVE_ID",
            "ASHA_ORCHESTRATION_COORDINATOR_ID",
            "ASHA_ORCHESTRATION_COORDINATOR_GENERATION",
            "ASHA_VERIFICATION_PROCESS_V1",
        }:
            self.assertIn(["-u", key], [argv[index:index + 2] for index in range(len(argv) - 1)])
        self.assertNotIn("Help me revise chapter one.", argv)

        record = RoomStore(self.config).read(result["room_id"])
        self.assertEqual(record["contract"], "asha.room.v1")
        self.assertEqual(record["lifecycle"], "open")
        self.assertEqual(record["prompt_digest"], hashlib.sha256(
            b"Help me revise chapter one.").hexdigest())
        self.assertNotIn("prompt", record)
        self.assertNotIn("Help me", json.dumps(record))

    def test_exact_initialized_path_outside_configured_roots_bypasses_index(self) -> None:
        external = self.root / "external" / "notes"
        (external / ".asha").mkdir(parents=True)
        (external / "Memory").mkdir()
        (external / ".asha/config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2,
            "project_id": "external-notes", "name": "External Notes",
        }), encoding="utf-8")

        opened = self._open(project=str(external))

        self.assertEqual(opened["project_root"], str(external))
        self.assertEqual(opened["project_id"], "external-notes")

    def test_all_harness_command_overrides_drive_preflight_and_room_environment(self) -> None:
        keys = {
            "claude": "ASHA_CLAUDE_CMD", "codex": "ASHA_CODEX_CMD",
            "copilot": "ASHA_COPILOT_CMD", "opencode": "ASHA_OPENCODE_CMD",
        }
        for index, (harness, key) in enumerate(keys.items(), 5):
            with self.subTest(harness=harness):
                override = str(self.root / "bin" / f"custom-{harness}")
                looked_up: list[str] = []

                def finder(command: str) -> str | None:
                    looked_up.append(command)
                    return command if command == override else None

                tmux = FakeTmux()
                opened = self._open(
                    name=f"Override {harness}", harness=harness, tmux=tmux,
                    room_id=f"{index:08d}-1111-4111-8111-111111111111",
                    env={**self.env, key: override}, executable_finder=finder,
                )

                self.assertEqual(opened["state"], "open")
                self.assertEqual(looked_up, [override])
                self.assertEqual(tmux.created[0]["environment"][key], override)

    def test_project_resolution_accepts_exact_friendly_name_directory_and_id(self) -> None:
        for index, selector in enumerate(("my novel", "NOVEL", "NOVEL-PROJECT"), 2):
            with self.subTest(selector=selector):
                tmux = FakeTmux()
                result = self._open(
                    name=f"room-{index}", project=selector, tmux=tmux,
                    room_id=f"{index:08d}-1111-4111-8111-111111111111",
                )
                self.assertEqual(result["project_id"], "novel-project")

    def test_open_refuses_uninitialized_ambiguous_or_missing_harness_before_tmux(self) -> None:
        other = self.root / "projects" / "other"
        (other / ".asha").mkdir(parents=True)
        (other / "Memory").mkdir()
        (other / ".asha/config.json").write_text(json.dumps({
            "initialized": True, "memory_version": 2,
            "project_id": "other", "name": "My Novel",
        }), encoding="utf-8")
        with self.assertRaisesRegex(RoomError, "ambiguous"):
            self._open(project="my novel")
        with self.assertRaisesRegex(RoomError, "not installed"):
            self._open(executable_finder=lambda _name: None)
        bare = self.root / "projects" / "bare"
        bare.mkdir()
        with self.assertRaisesRegex(RoomError, "initialized Memory v2"):
            self._open(project=str(bare))
        self.assertEqual(self.tmux.created, [])

    def test_session_show_resolves_a_legacy_room_as_list_shows_it(self) -> None:
        # #127: `session list --all` shows ended legacy Rooms, so `show` resolves them,
        # read-only and in the same row shape.
        from lib.control import hub_cli
        opened = self._open()
        close_room(RoomStore(self.config), opened["room_id"], tmux=self.tmux)
        killed = list(self.tmux.killed)
        hub = unittest.mock.MagicMock()
        hub.owns.return_value = False
        hub.tmux = self.tmux
        out = io.StringIO()
        with unittest.mock.patch.object(hub_cli, "Hub", return_value=hub), \
                contextlib.redirect_stdout(out):
            code = hub_cli.dispatch(["show", opened["room_id"], "--json"], env=self.env)
        self.assertEqual(code, 0)
        shown = json.loads(out.getvalue())
        self.assertEqual((shown["session_id"], shown["room_id"], shown["transport"]),
                         (opened["room_id"], opened["room_id"], "room"))
        self.assertEqual((shown["group"], shown["next_step"]), ("history", "Closed: view history"))
        self.assertEqual(self.tmux.killed, killed)

    def test_session_show_leaves_options_to_the_parser(self) -> None:
        from lib.control import hub_cli
        hub = unittest.mock.MagicMock()
        hub.owns.return_value = False
        with unittest.mock.patch.object(hub_cli, "Hub", return_value=hub), \
                unittest.mock.patch.object(hub_cli, "_listed_matches", side_effect=AssertionError("looked up")):
            self.assertIsNone(hub_cli.dispatch(["show", "--help"], env=self.env))

    def test_post_respawn_failure_leaves_exact_owned_record_closable(self) -> None:
        self.tmux.fail_respawn = True
        with self.assertRaisesRegex(RoomError, "launch outcome is uncertain"):
            self._open()
        record = RoomStore(self.config).read("11111111-1111-4111-8111-111111111111")
        self.assertEqual(record["lifecycle"], "creating")
        self.assertIn(record["tmux"]["session"], self.tmux.sessions)

        closed = close_room(
            RoomStore(self.config), record["room_id"], tmux=self.tmux,
        )
        self.assertEqual(closed["state"], "ended")
        self.assertEqual(self.tmux.killed, [record["tmux"]["session_id"]])

    def test_post_respawn_persistence_failure_reports_unmasked_recovery(self) -> None:
        original = RoomStore.save
        calls = 0

        def fail_final_save(store, record, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise RoomError("simulated durable write failure")
            return original(store, record, *args, **kwargs)

        identity = "11111111-1111-4111-8111-111111111111"
        with unittest.mock.patch.object(RoomStore, "save", fail_final_save):
            with self.assertRaises(RoomError) as raised:
                self._open()
        message = str(raised.exception)
        self.assertIn("launch outcome is uncertain", message)
        self.assertIn(identity, message)
        self.assertIn("asha room list --json", message)
        self.assertIn(f"asha room close {identity} --yes", message)

    def test_pre_respawn_partial_creation_cleans_only_exact_owned_residue(self) -> None:
        self.tmux.fail_create_after_markers = True
        with self.assertRaisesRegex(RoomError, "before harness start"):
            self._open()
        record = RoomStore(self.config).read("11111111-1111-4111-8111-111111111111")
        self.assertEqual(record["lifecycle"], "ended")
        self.assertEqual(record["tmux"]["pane_id"], "%42")
        self.assertEqual(record["tmux"]["session_id"], "$7")
        self.assertEqual(self.tmux.killed, ["$7"])
        self.assertNotIn(record["tmux"]["session"], self.tmux.sessions)
        listed = list_rooms(RoomStore(self.config), tmux=self.tmux)["rooms"][0]
        self.assertEqual(listed["state"], "ended")
        first = close_room(RoomStore(self.config), record["room_id"], tmux=self.tmux)
        second = close_room(RoomStore(self.config), record["room_id"], tmux=self.tmux)
        self.assertTrue(first["already_closed"])
        self.assertTrue(second["already_closed"])

        foreign = FakeTmux()
        foreign.fail_create_after_markers = True
        original = foreign.create_task_session

        def collide(**kwargs):
            try:
                return original(**kwargs)
            finally:
                foreign.session_options[(kwargs["session"], SESSION_ROOM_OPTION)] = "foreign"

        foreign.create_task_session = collide
        with self.assertRaisesRegex(RoomError, "before harness start"):
            self._open(
                name="Foreign", tmux=foreign,
                room_id="22222222-1111-4111-8111-111111111111",
            )
        self.assertEqual(foreign.killed, [], "a foreign collision must never be killed")

    def test_session_id_reply_failure_recovers_and_persists_a_complete_pair(self) -> None:
        self.tmux.session_id_exceptions = [TmuxError("session id reply lost")]

        with self.assertRaisesRegex(RoomError, "before harness start"):
            self._open()

        record = RoomStore(self.config).read("11111111-1111-4111-8111-111111111111")
        self.assertEqual(record["tmux"]["pane_id"], "%42")
        self.assertEqual(record["tmux"]["session_id"], "$7")
        self.assertEqual(record["lifecycle"], "ended")
        self.assertEqual(list_rooms(RoomStore(self.config), tmux=self.tmux)["rooms"][0]["state"], "ended")
        self.assertTrue(close_room(
            RoomStore(self.config), record["room_id"], tmux=self.tmux,
        )["already_closed"])

    def test_zero_id_live_holder_is_recovered_persisted_then_exactly_closed(self) -> None:
        self.tmux.session_id_exceptions = [
            TmuxError("first session id reply lost"),
            TmuxError("recovery session id reply lost"),
        ]
        identity = "11111111-1111-4111-8111-111111111111"
        with self.assertRaisesRegex(RoomError, "before harness start"):
            self._open()
        store = RoomStore(self.config)
        stranded = store.read(identity)
        self.assertIsNone(stranded["tmux"]["pane_id"])
        self.assertIsNone(stranded["tmux"]["session_id"])
        self.assertIn(stranded["tmux"]["session"], self.tmux.sessions)
        self.assertEqual(self.tmux.killed, [])

        listed = list_rooms(store, tmux=self.tmux)["rooms"][0]
        self.assertEqual(listed["state"], "ended")
        self.assertIn("recoverable", listed["detail"])

        events: list[tuple] = []
        original_save = RoomStore.save
        original_kill = self.tmux.kill_owned_room

        def observed_save(target, record, **kwargs):
            events.append((
                "save", record["tmux"]["pane_id"],
                record["tmux"]["session_id"],
            ))
            return original_save(target, record, **kwargs)

        def observed_kill(**identity_values):
            durable = store.read(identity)
            events.append((
                "kill", durable["tmux"]["pane_id"],
                durable["tmux"]["session_id"],
            ))
            return original_kill(**identity_values)

        self.tmux.kill_owned_room = observed_kill
        with unittest.mock.patch.object(RoomStore, "save", observed_save):
            closed = close_room(store, identity, tmux=self.tmux)

        self.assertEqual(events[:2], [
            ("save", "%42", "$7"),
            ("kill", "%42", "$7"),
        ])
        self.assertEqual(events[2], ("save", "%42", "$7"))
        self.assertEqual(closed["state"], "ended")
        self.assertTrue(close_room(store, identity, tmux=self.tmux)["already_closed"])

    def test_zero_id_recovery_unavailable_or_foreign_fails_closed_with_retry(self) -> None:
        self.tmux.session_id_exceptions = [
            TmuxError("first session id reply lost"),
            TmuxError("recovery session id reply lost"),
        ]
        identity = "11111111-1111-4111-8111-111111111111"
        with self.assertRaisesRegex(RoomError, "before harness start"):
            self._open()
        store = RoomStore(self.config)

        self.tmux.session_id_exceptions = [TmuxError("tmux identity unavailable")]
        with self.assertRaisesRegex(RoomError, "retry.*no session was killed"):
            close_room(store, identity, tmux=self.tmux)
        self.assertEqual(self.tmux.killed, [])

        session = store.read(identity)["tmux"]["session"]
        self.tmux.session_options[(session, SESSION_ROOM_OPTION)] = "foreign"
        with self.assertRaisesRegex(RoomError, "retry.*no session was killed"):
            close_room(store, identity, tmux=self.tmux)
        self.assertEqual(self.tmux.killed, [], "foreign readable-name collision must survive")

    def test_pre_respawn_interrupts_cleanup_and_preserve_original_semantics(self) -> None:
        cases = ((KeyboardInterrupt(), 2), (SystemExit(23), 3))
        for interruption, index in cases:
            with self.subTest(interruption=type(interruption).__name__):
                tmux = FakeTmux()
                tmux.session_id_exceptions = [interruption]
                identity = f"{index:08d}-2222-4222-8222-222222222222"
                with self.assertRaises(type(interruption)) as raised:
                    self._open(
                        name=f"Interrupted {index}", tmux=tmux, room_id=identity,
                    )
                if isinstance(interruption, SystemExit):
                    self.assertEqual(raised.exception.code, 23)
                self.assertIn(identity, getattr(raised.exception, "asha_room_guidance"))
                record = RoomStore(self.config).read(identity)
                self.assertEqual(record["tmux"]["pane_id"], "%42")
                self.assertEqual(record["tmux"]["session_id"], "$7")
                self.assertEqual(record["lifecycle"], "ended")
                self.assertEqual(tmux.killed, ["$7"])

    def test_post_respawn_interrupts_retain_residue_and_preserve_original_semantics(self) -> None:
        cases = ((KeyboardInterrupt(), 4), (SystemExit(29), 5))
        for interruption, index in cases:
            with self.subTest(interruption=type(interruption).__name__):
                tmux = FakeTmux()
                tmux.respawn_exception = interruption
                identity = f"{index:08d}-2222-4222-8222-222222222222"
                with self.assertRaises(type(interruption)) as raised:
                    self._open(
                        name=f"Uncertain {index}", tmux=tmux, room_id=identity,
                    )
                if isinstance(interruption, SystemExit):
                    self.assertEqual(raised.exception.code, 29)
                guidance = getattr(raised.exception, "asha_room_guidance")
                self.assertIn(identity, guidance)
                self.assertIn("asha room list --json", guidance)
                self.assertIn(f"asha room close {identity} --yes", guidance)
                record = RoomStore(self.config).read(identity)
                self.assertEqual(record["lifecycle"], "creating")
                self.assertIn(record["tmux"]["session"], tmux.sessions)
                self.assertEqual(close_room(
                    RoomStore(self.config), identity, tmux=tmux,
                )["state"], "ended")

    def test_list_reconciles_dead_missing_and_foreign_mismatch_honestly(self) -> None:
        opened = self._open()
        store = RoomStore(self.config)
        listed = list_rooms(store, tmux=self.tmux)
        self.assertEqual(listed["contract"], ROOM_LIST_CONTRACT)
        self.assertEqual(listed["rooms"][0]["state"], "open")
        self.tmux.dead = True
        self.assertEqual(list_rooms(store, tmux=self.tmux)["rooms"][0]["state"], "ended")
        self.tmux.dead = False
        self.tmux.sessions.clear()
        self.assertEqual(list_rooms(store, tmux=self.tmux)["rooms"][0]["state"], "missing")
        self.tmux.sessions.add(opened["session"])
        self.tmux.session_options[(opened["session"], SESSION_ROOM_OPTION)] = "foreign"
        mismatch = list_rooms(store, tmux=self.tmux)["rooms"][0]
        self.assertEqual(mismatch["state"], "mismatch")
        with self.assertRaisesRegex(RoomError, "ownership mismatch"):
            close_room(store, opened["room_id"], tmux=self.tmux)
        self.assertEqual(self.tmux.killed, [])

    # #140: a host reboot kills the tmux server. The new server restarts its
    # pane and session IDs from zero, so a Room's recorded IDs can name a
    # foreign pane, or no server answers at all. Pane IDs are never reused
    # within one server, so a pane at the recorded ID without this Room's pane
    # marker proves the owned pane is gone.
    NO_SERVER = (
        "command failed (1): error connecting to /tmp/tmux-1000/default "
        "(No such file or directory)"
    )

    def _assert_orphan_closes_without_kill(self, opened, survivors) -> None:
        store = RoomStore(self.config)
        listed = list_rooms(store, tmux=self.tmux)["rooms"][0]
        self.assertEqual(listed["state"], "missing", listed["detail"])
        with self.assertRaisesRegex(RoomError, "is missing"):
            attach_room(store, opened["room_id"], tmux=self.tmux)
        closed = close_room(store, opened["room_id"], tmux=self.tmux)
        self.assertEqual(closed["state"], "ended")
        self.assertFalse(closed["killed"])
        self.assertEqual(closed["observed_state"], "missing")
        self.assertEqual(self.tmux.kill_attempts, [])
        self.assertEqual(self.tmux.killed, [])
        self.assertEqual(self.tmux.sessions, survivors)
        # The ended record still names the reused IDs; it never reads foreign again.
        again = close_room(store, opened["room_id"], tmux=self.tmux)
        self.assertTrue(again["already_closed"])
        self.assertEqual(list_rooms(store, tmux=self.tmux)["rooms"][0]["state"], "ended")
        self.assertEqual(self.tmux.kill_attempts, [])

    def _rebooted_since(self, opened) -> None:
        """The host booted after this Room was created."""
        self.enterContext(unittest.mock.patch.object(
            rooms_module, "_boot_time", return_value=time.time(),
        ))

    def _hand_ids_to_another_room(self) -> None:
        other = "65ec305e-f3fe-47e1-8b97-b8c77c3087d7"
        self.tmux.sessions = {"asha-room-65ec305e"}
        self.tmux.session_options = {("asha-room-65ec305e", SESSION_ROOM_OPTION): other}
        self.tmux.pane_options = {
            (self.tmux.pane_id, PANE_ROOM_OPTION): other,
            (self.tmux.pane_id, "@asha_room_project_id"): "f" * 64,
        }

    def test_restarted_server_handing_both_ids_to_another_room_reads_missing(self) -> None:
        opened = self._open()
        self._hand_ids_to_another_room()
        self._rebooted_since(opened)
        self._assert_orphan_closes_without_kill(opened, {"asha-room-65ec305e"})

    def test_restarted_server_handing_the_pane_id_to_an_unmarked_session_reads_missing(self) -> None:
        opened = self._open()
        self.tmux.sessions = {"0"}
        self.tmux.session_identity = "$0"
        self.tmux.session_options = {}
        self.tmux.pane_options = {}
        self._rebooted_since(opened)
        self._assert_orphan_closes_without_kill(opened, {"0"})

    def test_reused_ids_in_this_boot_stay_a_refused_mismatch(self) -> None:
        # The old server may only be hidden (deleted socket, another
        # TMUX_TMPDIR) while a newer one reuses its IDs: refuse, as before.
        opened = self._open()
        self._hand_ids_to_another_room()
        store = RoomStore(self.config)
        listed = list_rooms(store, tmux=self.tmux)["rooms"][0]
        self.assertEqual(listed["state"], "mismatch", listed["detail"])
        with self.assertRaisesRegex(RoomError, "ownership mismatch"):
            close_room(store, opened["room_id"], tmux=self.tmux)
        self.assertEqual(self.tmux.kill_attempts, [])
        self.assertEqual(store.read(opened["room_id"])["lifecycle"], "open")

    def test_absent_tmux_server_socket_after_a_reboot_reads_missing(self) -> None:
        opened = self._open()
        self.tmux.sessions.clear()
        self.tmux.server_error = self.NO_SERVER
        self._rebooted_since(opened)
        self._assert_orphan_closes_without_kill(opened, set())

    def test_absent_tmux_server_socket_in_this_boot_stays_unavailable(self) -> None:
        # A deleted socket file or another TMUX_TMPDIR hides a live server:
        # the Room created in this boot may still be running, so refuse.
        opened = self._open()
        self.tmux.server_error = self.NO_SERVER
        store = RoomStore(self.config)
        listed = list_rooms(store, tmux=self.tmux)["rooms"][0]
        self.assertEqual(listed["state"], "unavailable", listed["detail"])
        with self.assertRaisesRegex(RoomError, "ownership is unavailable"):
            close_room(store, opened["room_id"], tmux=self.tmux)
        self.assertEqual(self.tmux.kill_attempts, [])
        self.assertEqual(store.read(opened["room_id"])["lifecycle"], "open")

    def test_boot_proof_fails_closed_without_a_boot_time_or_creation_time(self) -> None:
        record = {"created_at": "2026-11-07T05:09:30.860715Z"}
        with unittest.mock.patch.object(rooms_module, "_boot_time", return_value=None):
            self.assertFalse(rooms_module._created_before_boot(record))
        with unittest.mock.patch.object(rooms_module, "_boot_time", return_value=1794097904.0):
            self.assertTrue(rooms_module._created_before_boot(record))
            self.assertFalse(rooms_module._created_before_boot(
                {"created_at": "2026-11-08T00:34:37.255538Z"}))
            for bad in (None, "", "2026-11-07 05:09:30", 7):
                self.assertFalse(rooms_module._created_before_boot({"created_at": bad}))
        self.assertIsInstance(rooms_module._boot_time(), (float, type(None)))

    def test_server_absent_predicate_matches_only_a_missing_server(self) -> None:
        for text in (
            "no server running on /tmp/tmux-1000/default",
            "error connecting to /tmp/tmux-1000/default (No such file or directory)",
            "error connecting to /tmp/tmux-1000/default (Connection refused)",
            "failed to connect to server: No such file or directory",
        ):
            self.assertTrue(tmux_server_absent(text), text)
        for text in (
            "error connecting to /tmp/tmux-1000/default (Permission denied)",
            "error connecting to /tmp/tmux-1000/default (Too many open files)",
            "can't find pane: %1", "no such pane: %1", "",
        ):
            self.assertFalse(tmux_server_absent(text), text)

    def test_kill_that_fails_while_the_pane_vanishes_reports_an_unknown_kill(self) -> None:
        opened = self._open()
        self.tmux.before_kill = self.tmux.sessions.clear
        closed = close_room(RoomStore(self.config), opened["room_id"], tmux=self.tmux)
        self.assertEqual(closed["state"], "ended")
        self.assertIsNone(closed["killed"])
        self.assertEqual(closed["observed_state"], "missing")
        self.assertEqual(self.tmux.killed, [])

    def test_foreign_pane_marker_in_an_owned_session_stays_a_refused_mismatch(self) -> None:
        opened = self._open()
        self.tmux.pane_options[(self.tmux.pane_id, PANE_ROOM_OPTION)] = "foreign"
        store = RoomStore(self.config)
        listed = list_rooms(store, tmux=self.tmux)["rooms"][0]
        self.assertEqual(listed["state"], "mismatch", listed["detail"])
        with self.assertRaisesRegex(RoomError, "pane ownership mismatch"):
            close_room(store, opened["room_id"], tmux=self.tmux)
        self.assertEqual(self.tmux.kill_attempts, [])

    def test_own_pane_moved_into_another_session_stays_a_refused_mismatch(self) -> None:
        # The pane still carries this Room's marker: it is this Room's pane,
        # wherever it now sits, so close refuses rather than calling it gone.
        opened = self._open()
        self.tmux.session_identity = "$0"
        store = RoomStore(self.config)
        self.assertEqual(list_rooms(store, tmux=self.tmux)["rooms"][0]["state"], "mismatch")
        with self.assertRaisesRegex(RoomError, "ownership mismatch"):
            close_room(store, opened["room_id"], tmux=self.tmux)
        self.assertEqual(self.tmux.killed, [])
        self.assertEqual(store.read(opened["room_id"])["lifecycle"], "open")

    def test_attach_requires_exact_ownership_and_close_is_safely_idempotent(self) -> None:
        opened = self._open()
        store = RoomStore(self.config)
        target = attach_room(store, "draft room", tmux=self.tmux)
        self.assertEqual(target["session"], opened["session"])
        self.assertEqual(target["attach"], opened["attach"])
        first = close_room(store, opened["room_id"], tmux=self.tmux)
        second = close_room(store, opened["room_id"], tmux=self.tmux)
        self.assertEqual(first["state"], "ended")
        self.assertEqual(second["state"], "ended")
        self.assertTrue(second["already_closed"])
        with self.assertRaisesRegex(RoomError, "is ended"):
            attach_room(store, opened["room_id"], tmux=self.tmux)

    def test_replacement_after_validation_is_refused_by_atomic_close(self) -> None:
        opened = self._open()
        self.tmux.replace_before_owned_action = True

        with self.assertRaisesRegex(RoomError, "ownership changed"):
            close_room(RoomStore(self.config), opened["room_id"], tmux=self.tmux)

        self.assertEqual(self.tmux.killed, [])
        self.assertIn(opened["session"], self.tmux.sessions)

    def test_open_and_close_are_serialized_so_confirmed_close_wins(self) -> None:
        entered_respawn = threading.Event()
        allow_respawn = threading.Event()
        original_respawn = self.tmux.respawn

        def blocked_respawn(pane_id: str, argv: list[str]) -> None:
            entered_respawn.set()
            self.assertTrue(allow_respawn.wait(5))
            original_respawn(pane_id, argv)

        self.tmux.respawn = blocked_respawn
        opened: list[dict] = []
        closed: list[dict] = []
        failures: list[BaseException] = []

        def launch() -> None:
            try:
                opened.append(self._open())
            except BaseException as exc:
                failures.append(exc)

        def close() -> None:
            try:
                closed.append(close_room(
                    RoomStore(self.config),
                    "11111111-1111-4111-8111-111111111111", tmux=self.tmux,
                ))
            except BaseException as exc:
                failures.append(exc)

        launch_thread = threading.Thread(target=launch)
        launch_thread.start()
        self.assertTrue(entered_respawn.wait(5))
        close_thread = threading.Thread(target=close)
        close_thread.start()
        time.sleep(0.1)
        self.assertFalse(closed, "close must wait for the in-flight open transaction")
        allow_respawn.set()
        launch_thread.join(5)
        close_thread.join(5)

        self.assertEqual(failures, [])
        self.assertEqual(opened[0]["state"], "open")
        self.assertEqual(closed[0]["state"], "ended")
        self.assertEqual(
            RoomStore(self.config).read(opened[0]["room_id"])["lifecycle"],
            "ended",
        )

    def test_room_store_compare_and_swap_refuses_a_stale_lifecycle_write(self) -> None:
        opened = self._open()
        store = RoomStore(self.config)
        current = store.read(opened["room_id"])
        stale = store.read(opened["room_id"])
        expected = store.digest(current)
        current["lifecycle"] = "ended"
        store.save(current, expected_digest=expected)

        with self.assertRaisesRegex(RoomError, "changed concurrently"):
            store.save(stale, expected_digest=expected)

    def test_cli_routes_open_list_attach_and_confirmed_close(self) -> None:
        stdout = io.StringIO()
        with unittest.mock.patch("lib.control.cli.load_config", return_value=self.config), \
                unittest.mock.patch("lib.control.cli.TmuxAdapter", return_value=self.tmux), \
                unittest.mock.patch("lib.control.rooms.shutil.which", return_value="/usr/bin/true"), \
                contextlib.redirect_stdout(stdout):
            rc = cli.main([
                "room", "open", "Draft Room", "--project", str(self.project),
                "--harness", "codex", "--prompt", "Revise chapter one", "--json",
            ], env={**self.env, "ASHA_ROOT": str(self.asha_root)})
        self.assertEqual(rc, 0)
        opened = json.loads(stdout.getvalue())
        self.assertEqual(opened["state"], "open")

        stdout = io.StringIO()
        with unittest.mock.patch("lib.control.cli.load_config", return_value=self.config), \
                unittest.mock.patch("lib.control.cli.TmuxAdapter", return_value=self.tmux), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["room", "list", "--json"], env=self.env), 0)
        self.assertEqual(json.loads(stdout.getvalue())["rooms"][0]["name"], "Draft Room")

        stdout = io.StringIO()
        with unittest.mock.patch("lib.control.cli.load_config", return_value=self.config), \
                unittest.mock.patch("lib.control.cli.TmuxAdapter", return_value=self.tmux), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main(["room", "attach", "Draft Room", "--json"], env=self.env), 0)
        self.assertEqual(json.loads(stdout.getvalue())["session"], opened["session"])

        stderr = io.StringIO()
        with unittest.mock.patch("lib.control.cli.load_config", return_value=self.config), \
                unittest.mock.patch("lib.control.cli.TmuxAdapter", return_value=self.tmux), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(cli.main(["room", "close", "Draft Room", "--json"], env=self.env), 2)
        self.assertIn("requires --yes", stderr.getvalue())

        stdout = io.StringIO()
        with unittest.mock.patch("lib.control.cli.load_config", return_value=self.config), \
                unittest.mock.patch("lib.control.cli.TmuxAdapter", return_value=self.tmux), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(cli.main([
                "room", "close", "Draft Room", "--yes", "--json",
            ], env=self.env), 0)
        self.assertEqual(json.loads(stdout.getvalue())["state"], "ended")

    def test_cli_open_and_close_refuse_worker_and_sandboxed_harness_sessions(self) -> None:
        # K4 (2026-10-05): the Room surface launches and ends sessions too.
        argvs = (
            ["room", "open", "Escape", "--project", str(self.project), "--harness", "claude",
             "--prompt", "Escape", "--json"],
            ["room", "close", "Draft Room", "--yes", "--json"],
        )
        for extra in ({"ASHA_SESSION_PROFILE": "worker"}, {"ASHA_HARNESS": "codex"}):
            for argv in argvs:
                stderr = io.StringIO()
                with self.subTest(extra=extra, verb=argv[1]), \
                        unittest.mock.patch("lib.control.cli.load_config", return_value=self.config), \
                        unittest.mock.patch("lib.control.cli.TmuxAdapter", return_value=self.tmux), \
                        unittest.mock.patch("lib.control.rooms.shutil.which", return_value="/usr/bin/true"), \
                        contextlib.redirect_stderr(stderr):
                    self.assertEqual(cli.main(argv, env={
                        **self.env, "ASHA_ROOT": str(self.asha_root), **extra}), 2)
                    self.assertIn("cannot perform session operator actions", stderr.getvalue())
        self.assertEqual(self.tmux.created, [])

    def test_cli_attach_inside_tmux_uses_caller_bound_popup(self) -> None:
        opened = self._open()
        self.tmux.caller_client = unittest.mock.Mock(return_value="/dev/pts/7")
        self.tmux.popup_command_argv = unittest.mock.Mock(return_value=["tmux", "display-popup"])
        with unittest.mock.patch("lib.control.cli.load_config", return_value=self.config), \
                unittest.mock.patch("lib.control.cli.TmuxAdapter", return_value=self.tmux), \
                unittest.mock.patch("lib.control.cli.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            self.assertEqual(cli.main(
                ["room", "attach", opened["room_id"]],
                env={**self.env, "TMUX": "socket", "TMUX_PANE": "%7"},
            ), 0)
        self.tmux.caller_client.assert_called_once_with("%7")
        self.tmux.popup_command_argv.assert_called_once_with(
            client="/dev/pts/7", command=unittest.mock.ANY,
            width="90%", height="85%",
        )

    def test_cli_attach_popup_and_no_client_refusals_return_two(self) -> None:
        opened = self._open()
        base_env = {**self.env, "TMUX": "socket", "TMUX_PANE": "%7"}
        for client, popup_status, expected in (
            (None, None, "no tmux client"),
            ("/dev/pts/7", 66, "status 66"),
        ):
            with self.subTest(client=client, popup_status=popup_status):
                self.tmux.caller_client = unittest.mock.Mock(return_value=client)
                self.tmux.popup_command_argv = unittest.mock.Mock(
                    return_value=["tmux", "display-popup"],
                )
                stderr = io.StringIO()
                with unittest.mock.patch("lib.control.cli.load_config", return_value=self.config), \
                        unittest.mock.patch("lib.control.cli.TmuxAdapter", return_value=self.tmux), \
                        unittest.mock.patch("lib.control.cli.subprocess.run") as run, \
                        contextlib.redirect_stderr(stderr):
                    run.return_value = SimpleNamespace(returncode=popup_status)
                    status = cli.main(
                        ["room", "attach", opened["room_id"]], env=base_env,
                    )
                self.assertEqual(status, 2)
                self.assertIn(expected, stderr.getvalue())
                if client is None:
                    run.assert_not_called()

    def test_cli_keyboard_interrupt_keeps_status_130_and_prints_room_recovery(self) -> None:
        identity = "66666666-2222-4222-8222-222222222222"
        self.tmux.respawn_exception = KeyboardInterrupt()
        stderr = io.StringIO()
        with unittest.mock.patch("lib.control.cli.load_config", return_value=self.config), \
                unittest.mock.patch("lib.control.cli.TmuxAdapter", return_value=self.tmux), \
                unittest.mock.patch("lib.control.rooms.shutil.which", return_value="/usr/bin/true"), \
                unittest.mock.patch("lib.control.rooms.uuid.uuid4", return_value=uuid.UUID(identity)), \
                contextlib.redirect_stderr(stderr):
            status = cli.main([
                "room", "open", "Interrupted", "--project", str(self.project),
                "--harness", "codex", "--prompt", "Draft",
            ], env={**self.env, "ASHA_ROOT": str(self.asha_root)})
        self.assertEqual(status, 130)
        self.assertIn("asha control: interrupted", stderr.getvalue())
        self.assertIn(f"asha room close {identity} --yes", stderr.getvalue())

    def test_public_dispatcher_routes_room_help(self) -> None:
        result = subprocess.run(
            [str(self.asha_root / "bin/asha"), "room", "--help"],
            env={**self.env, "PATH": os.environ.get("PATH", "")},
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("asha room open NAME --project PROJECT", result.stdout)
        self.assertIn("close NAME|UUID [--yes]", result.stdout)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_replacement_is_refused_by_atomic_attach_and_kill(self) -> None:
        socket = f"asha-room-race-{uuid.uuid4().hex[:12]}"
        self.enterContext(TmuxSocketReaper(socket))
        adapter = TmuxAdapter(
            socket=socket,
            config_file=Path("/dev/null"),
        )
        returncode, _stdout, _stderr = adapter._run_status([
            "list-commands", "new-session",
        ])
        if returncode != 0:
            self.skipTest(
                "isolated tmux sockets are unavailable in this execution sandbox"
            )
        room_id = "44444444-1111-4111-8111-111111111111"
        project_marker = hashlib.sha256(b"novel-project").hexdigest()
        session = "asha-room-race"
        pane = adapter.create_task_session(
            session=session, window="room", start_directory=self.project,
            environment={}, holder_argv=["sleep", "30"],
            session_options={SESSION_ROOM_OPTION: room_id},
            pane_options={
                PANE_ROOM_OPTION: room_id,
                "@asha_room_project_id": project_marker,
            },
            pane_title="asha:room:race:codex",
        )
        session_id = adapter.session_id(pane)
        attach = adapter.room_attach_argv(
            room_id=room_id, project_marker=project_marker,
            pane_id=pane, session_id=session_id,
        )
        adapter.kill_session(session)
        adapter.create_task_session(
            session=session, window="room", start_directory=self.project,
            environment={}, holder_argv=["sleep", "30"],
            session_options={SESSION_ROOM_OPTION: "foreign"},
            pane_options={
                PANE_ROOM_OPTION: "foreign",
                "@asha_room_project_id": "f" * 64,
            },
            pane_title="foreign",
        )
        try:
            attempted = subprocess.run(
                attach, text=True, capture_output=True, check=False, timeout=3,
            )
            self.assertEqual(attempted.returncode, 66)
            with self.assertRaisesRegex(TmuxError, "no session was killed|can't find pane"):
                adapter.kill_owned_room(
                    room_id=room_id, project_marker=project_marker,
                    pane_id=pane, session_id=session_id,
                )
            self.assertTrue(adapter.has_session(session), "foreign replacement must survive")
        finally:
            if adapter.has_session(session):
                adapter.kill_session(session)

    def test_vanished_pane_facts_report_missing_pane_not_invalid_id(self) -> None:
        # tmux 3.4 answers display-message for an unknown pane id with exit 0
        # and empty fields; that is evidence of absence, not a malformed id.
        adapter = TmuxAdapter(socket="asha-unused", config_file=Path("/dev/null"))
        with unittest.mock.patch.object(adapter, "_run", return_value="\t" * 7 + "\n"):
            with self.assertRaisesRegex(TmuxError, "can't find pane: %9"):
                adapter.pane_facts("%9")
        with unittest.mock.patch.object(adapter, "_run", return_value="\n"):
            with self.assertRaisesRegex(TmuxError, "can't find pane: %9"):
                adapter.session_id("%9")

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_exited_room_with_vanished_pane_closes(self) -> None:
        launcher_root = self.root / "sleep-launcher"
        (launcher_root / "bin").mkdir(parents=True)
        launcher = launcher_root / "bin/asha"
        launcher.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
        launcher.chmod(0o700)
        socket = f"asha-room-gone-{uuid.uuid4().hex[:12]}"
        self.enterContext(TmuxSocketReaper(socket))
        adapter = TmuxAdapter(socket=socket, config_file=Path("/dev/null"))
        returncode, _stdout, _stderr = adapter._run_status([
            "list-commands", "new-session",
        ])
        if returncode != 0:
            self.skipTest(
                "isolated tmux sockets are unavailable in this execution sandbox"
            )
        # A sentinel keeps the tmux server reachable after the Room is gone.
        adapter._run(["new-session", "-d", "-s", "sentinel", "sleep", "30"])
        opened = self._open(
            tmux=adapter, asha_root=launcher_root, name="Gone Room",
            room_id="55555555-1111-4111-8111-111111111111",
        )
        adapter.kill_session(opened["session"])
        listed = list_rooms(RoomStore(self.config), tmux=adapter)["rooms"]
        self.assertEqual(listed[0]["state"], "missing")
        closed = close_room(RoomStore(self.config), opened["room_id"], tmux=adapter)
        self.assertEqual(closed["state"], "ended")
        self.assertTrue(adapter.has_session("sentinel"), "unrelated session must survive")
        again = close_room(RoomStore(self.config), opened["room_id"], tmux=adapter)
        self.assertTrue(again["already_closed"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_absent_server_socket_reads_missing(self) -> None:
        # No server ever listened here; tmux answers "error connecting to
        # PATH (No such file or directory)", which has_session already reads
        # as no server. No query below starts a server.
        opened = self._open()
        adapter = TmuxAdapter(
            socket=f"asha-room-absent-{uuid.uuid4().hex[:12]}",
            config_file=Path("/dev/null"),
        )
        self.assertFalse(adapter.has_session(opened["session"]))
        store = RoomStore(self.config)
        # Created in this boot: the absent socket alone proves nothing.
        listed = list_rooms(store, tmux=adapter)["rooms"][0]
        self.assertEqual(listed["state"], "unavailable", listed["detail"])
        self.assertIn("error connecting", listed["detail"])
        self._rebooted_since(opened)
        listed = list_rooms(store, tmux=adapter)["rooms"][0]
        self.assertEqual(listed["state"], "missing", listed["detail"])
        closed = close_room(store, opened["room_id"], tmux=adapter)
        self.assertEqual(closed["state"], "ended")
        self.assertFalse(closed["killed"])

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_server_restart_reusing_recorded_ids_closes_without_kill(self) -> None:
        # #140 reproduced on a private server: the Room records `$1`/`%1`,
        # the server dies with its socket, and a new server hands `$0` to a
        # plain terminal and `$1`/`%1` to another Room.
        launcher_root = self.root / "sleep-launcher"
        (launcher_root / "bin").mkdir(parents=True)
        launcher = launcher_root / "bin/asha"
        launcher.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
        launcher.chmod(0o700)
        socket = f"asha-room-reboot-{uuid.uuid4().hex[:12]}"
        self.enterContext(TmuxSocketReaper(socket))
        adapter = TmuxAdapter(socket=socket, config_file=Path("/dev/null"))
        returncode, _stdout, _stderr = adapter._run_status([
            "list-commands", "new-session",
        ])
        if returncode != 0:
            self.skipTest(
                "isolated tmux sockets are unavailable in this execution sandbox"
            )
        adapter._run(["new-session", "-d", "-s", "sentinel", "sleep", "30"])
        opened = self._open(
            tmux=adapter, asha_root=launcher_root, name="Thorne Room",
            room_id="66666666-1111-4111-8111-111111111111",
        )
        recorded = (opened["session_id"], opened["pane_id"])
        store = RoomStore(self.config)
        self.assertEqual(list_rooms(store, tmux=adapter)["rooms"][0]["state"], "open")
        adapter._run_status(["kill-server"])
        gone = list_rooms(store, tmux=adapter)["rooms"][0]
        self.assertEqual(gone["state"], "missing", gone["detail"])

        adapter._run(["new-session", "-d", "-s", "0", "sleep", "30"])
        other = "77777777-1111-4111-8111-111111111111"
        other_pane = adapter.create_task_session(
            session="asha-room-other", window="room", start_directory=self.project,
            environment={}, holder_argv=["sleep", "30"],
            session_options={SESSION_ROOM_OPTION: other},
            pane_options={
                PANE_ROOM_OPTION: other,
                "@asha_room_project_id": hashlib.sha256(b"other-project").hexdigest(),
            },
            pane_title="asha:room:other:codex",
        )
        self.assertEqual(
            (adapter.session_id(other_pane), other_pane), recorded,
            "setup: the new server must hand the recorded IDs to the other Room",
        )
        # A same-boot restart cannot prove the old server dead: refused.
        reused = list_rooms(store, tmux=adapter)["rooms"][0]
        self.assertEqual(reused["state"], "mismatch", reused["detail"])
        self._rebooted_since(opened)
        reused = list_rooms(store, tmux=adapter)["rooms"][0]
        self.assertEqual(reused["state"], "missing", reused["detail"])
        closed = close_room(store, opened["room_id"], tmux=adapter)
        self.assertEqual(closed["state"], "ended")
        self.assertFalse(closed["killed"])
        self.assertTrue(adapter.has_session("asha-room-other"), "foreign Room must survive")
        self.assertTrue(adapter.has_session("0"), "foreign terminal must survive")
        self.assertFalse(adapter.pane_facts(other_pane).dead)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_child_scrubs_inherited_roles_and_starts_in_project(self) -> None:
        launcher_root = self.root / "probe-launcher"
        (launcher_root / "bin").mkdir(parents=True)
        probe = self.root / "probe.json"
        launcher = launcher_root / "bin/asha"
        launcher.write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, sys, time\n"
            "pathlib.Path(os.environ['ROOM_PROBE']).write_text(json.dumps({\n"
            " 'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
            " 'ASHA_HOME': os.environ.get('ASHA_HOME'),\n"
            " 'ASHA_PERSONA': os.environ.get('ASHA_PERSONA'),\n"
            " 'ASHA_ORCHESTRATOR_STANCE': os.environ.get('ASHA_ORCHESTRATOR_STANCE'),\n"
            " 'ASHA_ROOM_ID': os.environ.get('ASHA_ROOM_ID'),\n"
            " 'ASHA_SEAT': os.environ.get('ASHA_SEAT'),\n"
            " 'ASHA_COORDINATOR_LAUNCH': os.environ.get('ASHA_COORDINATOR_LAUNCH'),\n"
            " 'ASHA_CONTROL_MANAGED': os.environ.get('ASHA_CONTROL_MANAGED'),\n"
            " 'ASHA_CONTROL_RESULT_TOKEN': os.environ.get('ASHA_CONTROL_RESULT_TOKEN'),\n"
            " 'ASHA_CONTROL_RESULT_OUTBOX': os.environ.get('ASHA_CONTROL_RESULT_OUTBOX'),\n"
            " 'ASHA_ORCHESTRATION_INITIATIVE_ID': os.environ.get('ASHA_ORCHESTRATION_INITIATIVE_ID'),\n"
            " 'ASHA_ORCHESTRATION_COORDINATOR_ID': os.environ.get('ASHA_ORCHESTRATION_COORDINATOR_ID'),\n"
            " 'ASHA_VERIFICATION_PROCESS_V1': os.environ.get('ASHA_VERIFICATION_PROCESS_V1'),\n"
            "}))\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        launcher.chmod(0o700)
        socket = f"asha-room-test-{uuid.uuid4().hex[:12]}"
        self.enterContext(TmuxSocketReaper(socket))
        adapter = TmuxAdapter(socket=socket, config_file=Path("/dev/null"))
        returncode, _stdout, _stderr = adapter._run_status([
            "list-commands", "new-session",
        ])
        if returncode != 0:
            self.skipTest(
                "isolated tmux sockets are unavailable in this execution sandbox"
            )
        inherited = {
            "ROOM_PROBE": str(probe), "ASHA_SEAT": "1",
            "ASHA_COORDINATOR_LAUNCH": "stale", "ASHA_CONTROL_MANAGED": "1",
            "ASHA_CONTROL_RESULT_TOKEN": "stale-token",
            "ASHA_CONTROL_RESULT_OUTBOX": ".asha/outbox/stale.json",
            "ASHA_ORCHESTRATION_INITIATIVE_ID": "stale-initiative",
            "ASHA_ORCHESTRATION_COORDINATOR_ID": "stale-coordinator",
            "ASHA_VERIFICATION_PROCESS_V1": "1",
        }
        with unittest.mock.patch.dict(os.environ, inherited, clear=False):
            started = time.monotonic()
            opened = self._open(
                tmux=adapter, asha_root=launcher_root, name="Real Room",
                room_id="33333333-1111-4111-8111-111111111111",
                prompt="line one\nline two;",
            )
            self.assertLess(time.monotonic() - started, 3.0, "open must return detached")
            deadline = time.monotonic() + 3
            while not probe.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(probe.exists(), "the respawned child did not start")
            evidence = json.loads(probe.read_text())
            self.assertEqual(evidence["argv"], ["codex", "--no-daemon", "line one\nline two;"])
            self.assertEqual(evidence["cwd"], str(self.project))
            self.assertEqual(evidence["ASHA_HOME"], str(self.asha_home))
            self.assertEqual(evidence["ASHA_PERSONA"], "1")
            self.assertEqual(evidence["ASHA_ORCHESTRATOR_STANCE"], "0")
            self.assertEqual(evidence["ASHA_ROOM_ID"], opened["room_id"])
            self.assertIsNone(evidence["ASHA_SEAT"])
            self.assertIsNone(evidence["ASHA_COORDINATOR_LAUNCH"])
            self.assertIsNone(evidence["ASHA_CONTROL_MANAGED"])
            self.assertIsNone(evidence["ASHA_CONTROL_RESULT_TOKEN"])
            self.assertIsNone(evidence["ASHA_CONTROL_RESULT_OUTBOX"])
            self.assertIsNone(evidence["ASHA_ORCHESTRATION_INITIATIVE_ID"])
            self.assertIsNone(evidence["ASHA_ORCHESTRATION_COORDINATOR_ID"])
            self.assertIsNone(evidence["ASHA_VERIFICATION_PROCESS_V1"])
            close_room(RoomStore(self.config), opened["room_id"], tmux=adapter)


if __name__ == "__main__":
    unittest.main()
