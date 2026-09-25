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
from lib.control.tmux import PaneFacts, RoomInputFacts, RoomInputRefused, TmuxError
from lib.control.tmux import TmuxAdapter
from lib.control.socket_reaper import TmuxSocketReaper
from lib.control import cli, tui


def _typed_into(line: str, typed: str) -> str:
    """A fake composer line after a paste: the marker followed by the text."""
    for marker in ("❯", "›"):
        if marker in line:
            return line[:line.index(marker) + 1] + " " + typed
    return line


# Codex hides its empty-composer hint once the composer holds text; the status
# line (styled, like the native capture) is the composer's lower boundary.
_CODEX_TYPED_FOOTER = ["", "  \x1b[38;5;223mGPT-6-Astra xhigh\x1b[39m · ~/Code"]


def _after_paste(screen: list[str], typed: str) -> list[str]:
    for index, line in enumerate(screen):
        if "›" in line:
            return [*screen[:index], _typed_into(line, typed), *_CODEX_TYPED_FOOTER]
    return [_typed_into(line, typed) for line in screen]


class FakeTmux:
    def __init__(self) -> None:
        self.sessions: set[str] = set()
        self.created: list[dict] = []
        self.respawned: list[tuple[str, list[str]]] = []
        self.killed: list[str] = []
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
        # Idle-input injection (#96): no screen means the input line is unproven.
        self.attached = 0
        # tmux attach generation (@asha_attach_gen); None: no fence installed.
        self.attach_generation: str | None = "0"
        # Pane event sequence (@asha_event_seq) that the native hook bumps.
        self.event_sequence: str | None = "0"
        self.screen: list[str] = []
        self.injected: list[tuple[str, str]] = []
        # Two-phase delivery: pasted text, then a guarded Enter. Hooks let a
        # test interleave a client attach, new work or a draft at each seam.
        self.pasted: list[tuple[str, str]] = []
        self.before_paste = None
        self.after_paste = None
        self.before_enter = None
        self.before_kill = None
        self.paste_prefix = ""

    executable = "tmux"
    socket = None

    def has_session(self, name: str) -> bool:
        return name in self.sessions

    def create_task_session(self, **kwargs) -> str:
        if kwargs["session"] in self.sessions:
            raise TmuxError("duplicate session")
        self.created.append(kwargs)
        self.sessions.add(kwargs["session"])
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

    def kill_owned_room(self, *, detached_only: bool = False, **identity) -> None:
        if self.before_kill is not None:
            self.before_kill()
        if detached_only and self.attached:
            raise RoomInputRefused("attached", "a client is attached; no session was killed")
        if self.replace_before_owned_action:
            session = next(iter(self.sessions))
            self.session_options[(session, SESSION_ROOM_OPTION)] = "foreign"
        session = next(iter(self.sessions), None)
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

    def room_input_facts(self, pane_id: str) -> RoomInputFacts:
        if pane_id != self.pane_id or not self.sessions:
            raise TmuxError("missing pane")
        if self.attach_generation is None:
            raise RoomInputRefused("unfenced", "no attach generation fence; nothing was typed")
        return RoomInputFacts(self.attached, self.attach_generation, self.event_sequence, list(self.screen))

    def _owns(self, identity) -> bool:
        session = next(iter(self.sessions), None)
        return (
            identity["session_id"] == self.session_identity
            and identity["pane_id"] == self.pane_id
            and session is not None
            and self.session_options.get((session, SESSION_ROOM_OPTION))
            == identity["room_id"]
        )

    def _guard(self, identity, attach_generation, event_sequence, stage) -> None:
        if not self._owns(identity):
            raise RoomInputRefused("ownership", f"room ownership changed; {stage}")
        if self.attached or self.attach_generation != attach_generation:
            raise RoomInputRefused("attached", f"a client attached; {stage}")
        if self.event_sequence != event_sequence:
            raise RoomInputRefused("stale", f"a native event began; {stage}")

    def inject_owned_room_input(
        self, *, text: str, attach_generation: str, event_sequence: str, confirm,
        ready=None, **identity,
    ) -> None:
        if self.before_paste is not None:
            self.before_paste()
        reason = ready() if ready is not None else None
        if reason:
            raise RoomInputRefused("stale", "nothing was typed: " + reason)
        self._guard(identity, attach_generation, event_sequence, "nothing was typed")
        before = list(self.screen)
        self.pasted.append((identity["pane_id"], text))
        typed = self.paste_prefix + text
        self.screen = _after_paste(before, typed)
        if self.after_paste is not None:
            self.after_paste()
        reason = confirm(list(self.screen))
        if reason:
            raise RoomInputRefused("partial", "typed but not submitted: " + reason)
        if self.before_enter is not None:
            self.before_enter()
        try:
            self._guard(identity, attach_generation, event_sequence, "typed but not submitted")
        except RoomInputRefused as exc:
            raise RoomInputRefused("partial", str(exc)) from exc
        self.screen = before
        self.injected.append((identity["pane_id"], text))

    def session_option(self, session: str, option: str) -> str | None:
        if session == self.session_identity:
            session = next(iter(self.sessions), session)
        return self.session_options.get((session, option))

    def pane_option(self, pane: str, option: str) -> str | None:
        return self.pane_options.get((pane, option))

    def pane_facts(self, pane: str) -> PaneFacts:
        if pane != self.pane_id or not self.sessions:
            raise TmuxError("missing pane")
        session = next(iter(self.sessions))
        return PaneFacts(pane, 1234, self.dead, 0 if self.dead else None, None,
                         session, "room", "asha:room")

    def window_pane_facts(self, session: str, window: str) -> PaneFacts:
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
            "codex": [prompt],
            "copilot": ["--interactive", prompt],
            "opencode": ["--prompt", prompt],
        }
        for harness, tail in expected.items():
            with self.subTest(harness=harness):
                self.assertEqual(
                    room_launch_argv(self.asha_root, harness, prompt),
                    [str(self.asha_root / "bin/asha"), harness, *tail],
                )

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
            # Idle typing (#96) is off by default: "0" overrides a global marker.
            "ASHA_ROOM_INPUT_FENCE": "0",
            "ASHA_CODEX_CMD": "codex",
        })
        argv = self.tmux.respawned[0][1]
        for key in {
            "ASHA_ROOM_INPUT_FENCE",
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

    def test_tui_room_form_uses_the_same_harness_override_preflight(self) -> None:
        override = self.root / "bin" / "custom-codex"
        override.parent.mkdir()
        override.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        override.chmod(0o700)

        class Curses:
            KEY_RESIZE = 410
            KEY_UP = 259
            KEY_DOWN = 258
            KEY_ENTER = 343
            KEY_BACKSPACE = 263
            KEY_BTAB = 353
            error = RuntimeError

        screen = unittest.mock.Mock()
        screen.getmaxyx.return_value = (18, 80)
        frames = []
        # Accept Project; type and advance Room name; replace whichever
        # installed harness sorts first with codex; type the opening prompt.
        keys = iter(
            [10, *"Draft Room", 9] + [127] * 16 +
            [*"codex", 10, *"Revise the chapter", 10]
        )

        launched = {
            "name": "Draft Room", "project_name": "My Novel",
        }
        env = {**self.env, "ASHA_CODEX_CMD": str(override)}
        project_payload = {"projects": [{
            "root": str(self.project), "name": "My Novel",
            "directory": "novel", "project_id": "novel-project",
            "asha_project": True,
        }]}
        with unittest.mock.patch(
            "lib.control.orchestration.projects.resolve_roots",
            return_value=([str(self.project.parent)], "test"),
        ), unittest.mock.patch(
            "lib.control.orchestration.projects.list_projects_across",
            return_value=project_payload,
        ), unittest.mock.patch.object(
            tui, "_draw_modal_frame",
            side_effect=lambda _screen, _curses, frame: frames.append(frame),
        ), unittest.mock.patch.object(
            tui, "_read_modal_key", side_effect=lambda *_args: next(keys),
        ), unittest.mock.patch(
            "lib.control.rooms.open_room", return_value=launched,
        ) as open_call, unittest.mock.patch("lib.control.tui._refresh_initiatives"):
            result = tui._open_room_form(
                screen, Curses(), unittest.mock.Mock(),
                self.config, env,
            )

        harness_frames = [
            frame for frame in frames if any("Harness" in row for row in frame.rows)
        ]
        self.assertTrue(any(
            "codex" in row and "installed" in row
            for frame in harness_frames for row in frame.rows
        ))
        self.assertIn("started detached", result)
        self.assertEqual(open_call.call_args.kwargs["env"]["ASHA_CODEX_CMD"], str(override))

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
    def test_real_tmux_input_injection_types_one_line_only_into_the_owned_pane(self) -> None:
        socket = f"asha-room-input-{uuid.uuid4().hex[:12]}"
        self.enterContext(TmuxSocketReaper(socket))
        adapter = TmuxAdapter(socket=socket, config_file=Path("/dev/null"))
        returncode, _stdout, _stderr = adapter._run_status([
            "list-commands", "new-session",
        ])
        if returncode != 0:
            self.skipTest(
                "isolated tmux sockets are unavailable in this execution sandbox"
            )
        probe = self.root / "typed.txt"
        room_id = "66666666-1111-4111-8111-111111111111"
        project_marker = hashlib.sha256(b"novel-project").hexdigest()
        pane = adapter.create_task_session(
            session="asha-room-input", window="room", start_directory=self.project,
            environment={},
            holder_argv=["sh", "-c", f"IFS= read -r line; printf '%s' \"$line\" > '{probe}'; sleep 30"],
            session_options={SESSION_ROOM_OPTION: room_id},
            pane_options={PANE_ROOM_OPTION: room_id, "@asha_room_project_id": project_marker},
            pane_title="asha:room:input", attach_fence=True,
        )
        session_id = adapter.session_id(pane)
        facts = adapter.room_input_facts(pane)
        self.assertEqual(facts.attached, 0)
        self.assertIsInstance(facts.screen, list)
        identity = dict(room_id=room_id, project_marker=project_marker, pane_id=pane, session_id=session_id)
        text = "Asha Control close request (x); $HOME `y` -- stays one line"
        seen: list[list[str]] = []
        def accept(screen):
            seen.append(screen)
            return None
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(
                **dict(identity, room_id="77777777-1111-4111-8111-111111111111"),
                text=text, attach_generation=facts.attach_generation, event_sequence=facts.event_sequence, confirm=accept,
            )
        self.assertEqual(refused.exception.category, "ownership")
        # A moved attach fence refuses before anything is typed.
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(
                **identity, text=text, attach_generation="999", event_sequence=facts.event_sequence, confirm=accept,
            )
        self.assertEqual(refused.exception.category, "attached")
        time.sleep(0.2)
        self.assertFalse(probe.exists(), "a refused injection must type nothing")
        self.assertEqual(seen, [])
        # A refusal must not leave the pane in view-mode for the next viewer.
        self.assertEqual(adapter.room_input_facts(pane).attached, 0)
        # A pane in a tmux mode refuses reads, paste and a detached-only kill.
        adapter._run(["copy-mode", "-t", pane])
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.room_input_facts(pane)
        self.assertEqual(refused.exception.category, "mode")
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.kill_owned_room(**identity, detached_only=True)
        self.assertEqual(refused.exception.category, "mode")
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(
                **identity, text=text, attach_generation=facts.attach_generation, event_sequence=facts.event_sequence, confirm=accept,
            )
        self.assertEqual(refused.exception.category, "mode")
        adapter._run(["send-keys", "-t", pane, "-X", "cancel"])
        with self.assertRaisesRegex(TmuxError, "one printable line"):
            adapter.inject_owned_room_input(
                **identity, text="two\nlines", attach_generation=facts.attach_generation, event_sequence=facts.event_sequence, confirm=accept,
            )
        # A failed confirmation between paste and Enter leaves the text unsubmitted.
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(
                **identity, text="held back", attach_generation=facts.attach_generation, event_sequence=facts.event_sequence,
                confirm=lambda screen: "the input line holds more than the typed text",
            )
        self.assertEqual(refused.exception.category, "partial")
        time.sleep(0.2)
        self.assertFalse(probe.exists(), "an unconfirmed paste must not be submitted")
        adapter._run(["send-keys", "-t", pane, "C-u"])
        adapter.inject_owned_room_input(
            **identity, text=text, attach_generation=facts.attach_generation, event_sequence=facts.event_sequence, confirm=accept,
        )
        self.assertEqual(len(seen), 1)
        deadline = time.monotonic() + 3
        while not probe.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(probe.read_text(), text)
        buffers = adapter._run(["list-buffers", "-F", "#{buffer_name}"])
        self.assertNotIn("asha-input-", buffers)
        # With no client and no mode, the detached-only kill proceeds.
        adapter.kill_owned_room(**identity, detached_only=True)
        self.assertFalse(adapter.has_session("asha-room-input"))

    def _real_fenced_room(self, *, fence: bool = True):
        """An owned real-tmux Room pane running a tiny raw composer (not a harness)."""
        socket = f"asha-room-fence-{uuid.uuid4().hex[:12]}"
        self.enterContext(TmuxSocketReaper(socket))
        adapter = TmuxAdapter(socket=socket, config_file=Path("/dev/null"))
        returncode, _stdout, _stderr = adapter._run_status(["list-commands", "new-session"])
        if returncode != 0:
            self.skipTest("isolated tmux sockets are unavailable in this execution sandbox")
        receiver, received = self.root / "composer.py", self.root / "submitted.json"
        receiver.write_text(
            "import json, os, sys, tty\n"
            "from pathlib import Path\n"
            "tty.setraw(0)\n"
            "line = ''\n"
            "def draw():\n"
            "    rule = '\u2500' * 40\n"
            "    os.write(1, ('\\x1b[2J\\x1b[H' + rule + '\\r\\n\u276f ' + line + '\\r\\n' + rule + '\\r\\n').encode())\n"
            "draw()\n"
            "while True:\n"
            "    char = os.read(0, 1).decode()\n"
            "    if char in '\\r\\n':\n"
            "        Path(sys.argv[1]).write_text(json.dumps(line))\n"
            "        line = ''\n"
            "    elif char != '\\x1b' and char.isprintable():\n"
            "        line += char\n"
            "    draw()\n",
            encoding="utf-8",
        )
        room_id = str(uuid.uuid4())
        marker = hashlib.sha256(b"fence-project").hexdigest()
        name = "asha-room-fence"
        pane = adapter.create_task_session(
            session=name, window="room", start_directory=self.project, environment={},
            holder_argv=[sys.executable, str(receiver), str(received)],
            session_options={SESSION_ROOM_OPTION: room_id},
            pane_options={PANE_ROOM_OPTION: room_id, "@asha_room_project_id": marker},
            pane_title="asha:room:fence", attach_fence=fence,
        )
        identity = dict(room_id=room_id, project_marker=marker, pane_id=pane,
                        session_id=adapter.session_id(pane))
        clients: list = []

        def cleanup():
            for process, master in clients:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=2)
                os.close(master)
        self.addCleanup(cleanup)

        def cycle(draft: str | None = None) -> None:
            """A real PTY client attaches, optionally types, and detaches."""
            import pty
            master, slave = pty.openpty()
            process = subprocess.Popen(
                [adapter.executable, *adapter._socket_args(), "attach-session", "-t", name],
                stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                env=dict(os.environ, TERM="xterm-256color"),
            )
            os.close(slave)
            clients.append((process, master))
            deadline = time.monotonic() + 3
            while adapter._run(["display-message", "-p", "-t", pane, "#{session_attached}"]).strip() == "0":
                self.assertIsNone(process.poll(), "tmux client failed to attach")
                self.assertLess(time.monotonic(), deadline, "tmux client attach timed out")
                time.sleep(0.002)
            if draft:
                adapter._run(["send-keys", "-t", pane, "-l", "--", draft])
            adapter._run(["detach-client", "-s", name])
            process.wait(timeout=3)

        def wait_screen() -> list[str]:
            deadline = time.monotonic() + 3
            while True:
                screen = adapter.room_input_facts(pane).screen
                if any("\u276f" in line for line in screen) or time.monotonic() > deadline:
                    return screen
                time.sleep(0.02)

        return adapter, identity, cycle, wait_screen, received

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_same_second_attach_cycle_after_confirmation_is_never_submitted(self) -> None:
        # QA2 #96 finding 2: session_last_attached has one-second resolution.
        from lib.control.pane_input import composer_holds, input_line_state
        adapter, identity, cycle, wait_screen, received = self._real_fenced_room()
        time.sleep(1.02 - time.time() % 1)  # keep both attachments inside one second
        cycle()
        self.assertEqual(input_line_state("claude", wait_screen())[0], "empty")
        facts = adapter.room_input_facts(identity["pane_id"])
        before = adapter._run(["display-message", "-p", "-t", identity["pane_id"], "#{session_last_attached}"])

        def confirm(screen):
            self.assertTrue(composer_holds("claude", screen, "CLOSE"), screen)
            cycle("DRAFT")
            after = adapter._run(["display-message", "-p", "-t", identity["pane_id"], "#{session_last_attached}"])
            self.assertEqual(after, before, "both attachments must share one timestamp")
            return None

        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(**identity, text="CLOSE",
                                            attach_generation=facts.attach_generation, event_sequence=facts.event_sequence, confirm=confirm)
        self.assertEqual(refused.exception.category, "partial")
        time.sleep(0.3)
        self.assertFalse(received.exists(), "an attach cycle after confirmation must block Enter")

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_same_second_attach_cycle_before_paste_refuses_the_paste(self) -> None:
        adapter, identity, cycle, wait_screen, received = self._real_fenced_room()
        time.sleep(1.02 - time.time() % 1)
        cycle()
        wait_screen()
        facts = adapter.room_input_facts(identity["pane_id"])
        cycle("DRAFT")
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(**identity, text="CLOSE",
                                            attach_generation=facts.attach_generation, event_sequence=facts.event_sequence,
                                            confirm=lambda screen: None)
        self.assertEqual(refused.exception.category, "attached")
        screen = "\n".join(adapter.room_input_facts(identity["pane_id"]).screen)
        self.assertNotIn("CLOSE", screen)
        self.assertFalse(received.exists())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_room_window_linked_into_another_session_is_never_typed_into(self) -> None:
        # A client of the other session sees and types into the pane without attaching.
        adapter, identity, _cycle, wait_screen, received = self._real_fenced_room()
        wait_screen()
        facts = adapter.room_input_facts(identity["pane_id"])
        adapter._run(["new-session", "-d", "-s", "asha-room-viewer", "--", "sleep", "30"])
        adapter._run(["link-window", "-s", "asha-room-fence:room", "-t", "asha-room-viewer:"])
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.room_input_facts(identity["pane_id"])
        self.assertEqual(refused.exception.category, "attached")
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(**identity, text="CLOSE",
                                            attach_generation=facts.attach_generation, event_sequence=facts.event_sequence,
                                            confirm=lambda screen: None)
        self.assertEqual(refused.exception.category, "attached")
        with self.assertRaises(RoomInputRefused):
            adapter.kill_owned_room(**identity, detached_only=True)
        self.assertTrue(adapter.has_session("asha-room-fence"))
        self.assertFalse(received.exists())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_room_without_an_attach_generation_is_never_typed_into(self) -> None:
        adapter, identity, _cycle, _wait, received = self._real_fenced_room(fence=False)
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.room_input_facts(identity["pane_id"])
        self.assertEqual(refused.exception.category, "unfenced")
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(**identity, text="CLOSE", attach_generation="0", event_sequence="0",
                                            confirm=lambda screen: None)
        self.assertEqual(refused.exception.category, "unfenced")
        self.assertFalse(received.exists())

    # QA3 #96 finding 3: the fence counters are exact pane-local integers.
    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_non_canonical_or_unsafe_fence_values_refuse(self) -> None:
        from lib.control.tmux import ATTACH_GENERATION_OPTION, EVENT_SEQUENCE_OPTION
        adapter, identity, _cycle, wait_screen, _received = self._real_fenced_room()
        wait_screen()
        pane = identity["pane_id"]
        for option in (ATTACH_GENERATION_OPTION, EVENT_SEQUENCE_OPTION):
            for value in ("-1", "bogus", "1.0", "01", "\uff11\uff12", " 1", "9007199254740992", "1000000000"):
                with self.subTest(option=option, value=value):
                    adapter._run(["set-option", "-p", "-t", pane, option, value])
                    with self.assertRaises(RoomInputRefused) as refused:
                        adapter.room_input_facts(pane)
                    self.assertEqual(refused.exception.category, "unfenced")
            adapter._run(["set-option", "-p", "-t", pane, option, "7"])
        self.assertEqual(adapter.room_input_facts(pane).attach_generation, "7")

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_generation_at_its_bound_refuses_instead_of_freezing(self) -> None:
        from lib.control.pane_input import composer_holds
        from lib.control.tmux import ATTACH_GENERATION_OPTION
        adapter, identity, cycle, wait_screen, received = self._real_fenced_room()
        wait_screen()
        pane = identity["pane_id"]
        adapter._run(["set-option", "-p", "-t", pane, ATTACH_GENERATION_OPTION, "999999998"])
        facts = adapter.room_input_facts(pane)

        def confirm(screen):
            self.assertTrue(composer_holds("claude", screen, "CLOSE"), screen)
            cycle("DRAFT")  # two hook runs: 999999998 -> 1000000000, past the bound
            return None

        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(**identity, text="CLOSE", attach_generation=facts.attach_generation,
                                            event_sequence=facts.event_sequence, confirm=confirm)
        self.assertEqual(refused.exception.category, "partial")
        time.sleep(0.3)
        self.assertFalse(received.exists())
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.room_input_facts(pane)
        self.assertEqual(refused.exception.category, "unfenced")
        self.assertIn("exhausted", str(refused.exception))

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_inherited_or_reset_counters_never_hide_an_attach(self) -> None:
        from lib.control.pane_input import composer_holds
        from lib.control.tmux import ATTACH_GENERATION_OPTION
        adapter, identity, cycle, wait_screen, received = self._real_fenced_room()
        wait_screen()
        pane, session = identity["pane_id"], identity["session_id"]
        # A session/window value never stands in for the owned pane's own counter.
        adapter._run(["set-option", "-p", "-u", "-t", pane, ATTACH_GENERATION_OPTION])
        adapter._run(["set-option", "-t", session, ATTACH_GENERATION_OPTION, "0"])
        adapter._run(["set-option", "-w", "-t", pane, ATTACH_GENERATION_OPTION, "0"])
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.room_input_facts(pane)
        self.assertEqual(refused.exception.category, "unfenced")
        # QA3 pane-shadow: resetting the pane counter before the read is still counted.
        adapter._run(["set-option", "-p", "-t", pane, ATTACH_GENERATION_OPTION, "0"])
        facts = adapter.room_input_facts(pane)

        def confirm(screen):
            self.assertTrue(composer_holds("claude", screen, "CLOSE"), screen)
            cycle("DRAFT")
            return None

        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(**identity, text="CLOSE", attach_generation=facts.attach_generation,
                                            event_sequence=facts.event_sequence, confirm=confirm)
        self.assertEqual(refused.exception.category, "partial")
        time.sleep(0.3)
        self.assertFalse(received.exists())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_hooks_removed_after_confirmation_block_enter(self) -> None:
        from lib.control.pane_input import composer_holds
        adapter, identity, cycle, wait_screen, received = self._real_fenced_room()
        wait_screen()
        facts = adapter.room_input_facts(identity["pane_id"])

        def confirm(screen):
            self.assertTrue(composer_holds("claude", screen, "CLOSE"), screen)
            for hook in ("client-attached", "client-session-changed"):
                adapter._run(["set-hook", "-u", "-t", identity["session_id"], hook])
            cycle("DRAFT")  # no hook ran: the generation did not move
            return None

        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(**identity, text="CLOSE", attach_generation=facts.attach_generation,
                                            event_sequence=facts.event_sequence, confirm=confirm)
        self.assertEqual(refused.exception.category, "partial")
        time.sleep(0.3)
        self.assertFalse(received.exists())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_native_event_during_delivery_refuses_paste_and_enter(self) -> None:
        from lib.control.tmux import EVENT_SEQUENCE_OPTION
        adapter, identity, _cycle, wait_screen, received = self._real_fenced_room()
        wait_screen()
        pane = identity["pane_id"]
        bump = ["set-option", "-p", "-t", pane, "-F", EVENT_SEQUENCE_OPTION,
                "#{e|+:#{" + EVENT_SEQUENCE_OPTION + "},1}"]
        facts = adapter.room_input_facts(pane)
        adapter._run(bump)
        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(**identity, text="CLOSE", attach_generation=facts.attach_generation,
                                            event_sequence=facts.event_sequence, confirm=lambda screen: None)
        self.assertEqual(refused.exception.category, "stale")
        self.assertNotIn("CLOSE", "\n".join(adapter.room_input_facts(pane).screen))
        facts = adapter.room_input_facts(pane)

        def confirm(screen):
            adapter._run(bump)
            return None

        with self.assertRaises(RoomInputRefused) as refused:
            adapter.inject_owned_room_input(**identity, text="CLOSE", attach_generation=facts.attach_generation,
                                            event_sequence=facts.event_sequence, confirm=confirm)
        self.assertEqual(refused.exception.category, "partial")
        time.sleep(0.3)
        self.assertFalse(received.exists())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_tmux_control_event_hook_bumps_the_pane_sequence_before_reporting(self) -> None:
        adapter, identity, _cycle, wait_screen, _received = self._real_fenced_room()
        wait_screen()
        pane = identity["pane_id"]
        socket_path, server_pid = adapter._run(
            ["display-message", "-p", "-t", pane, "#{socket_path}\t#{pid}"]).strip().split("\t")
        launcher_root = self.root / "event-launcher"
        (launcher_root / "bin").mkdir(parents=True)
        argv_file = self.root / "event-argv"
        (launcher_root / "bin/asha").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > '" + str(argv_file) + "'\necho '{}'\n", encoding="utf-8")
        (launcher_root / "bin/asha").chmod(0o700)
        hook = Path("plugins/session/hooks/handlers/control-event.sh").resolve()
        env = dict(os.environ, ASHA_ROOT=str(launcher_root), ASHA_HUB_SESSION_ID=str(uuid.uuid4()),
                   TMUX=f"{socket_path},{server_pid},0", TMUX_PANE=pane)
        # Without the Room's opt-in marker the hook makes no tmux call at all.
        subprocess.run(["bash", str(hook), "PreToolUse"], input=b"{}", env=env, capture_output=True, timeout=5)
        self.assertNotIn("--sequence", argv_file.read_text().split("\n"))
        self.assertEqual(adapter.room_input_facts(pane).event_sequence, "0")
        env["ASHA_ROOM_INPUT_FENCE"] = "1"
        for expected in ("1", "2"):
            result = subprocess.run(["bash", str(hook), "PreToolUse"], input=b"{}", env=env,
                                    capture_output=True, timeout=5)
            self.assertEqual(result.stdout.decode().strip(), "{}")
            argv = argv_file.read_text().split("\n")
            self.assertEqual(argv[argv.index("--sequence") + 1], expected)
            self.assertEqual(argv[argv.index("--sequence-pane") + 1], pane)
            self.assertEqual(adapter.room_input_facts(pane).event_sequence, expected)

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
    def test_real_tmux_default_room_ignores_a_server_global_input_fence_marker(self) -> None:
        """QA5 inherited-server-flag: idle typing is off, so nothing may bump the pane."""
        launcher_root = self.root / "fence-probe-launcher"
        (launcher_root / "bin").mkdir(parents=True)
        probe = self.root / "fence-probe.json"
        (launcher_root / "bin/asha").write_text(
            f"#!{sys.executable}\n"
            "import json, os, pathlib, time\n"
            f"pathlib.Path({str(probe)!r}).write_text(json.dumps(dict(os.environ)))\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        (launcher_root / "bin/asha").chmod(0o700)
        socket = f"asha-room-fence-env-{uuid.uuid4().hex[:12]}"
        self.enterContext(TmuxSocketReaper(socket))
        adapter = TmuxAdapter(socket=socket, config_file=Path("/dev/null"))
        returncode, _stdout, _stderr = adapter._run_status(["new-session", "-d", "-s", "keeper", "--", "sleep", "60"])
        if returncode != 0:
            self.skipTest("isolated tmux sockets are unavailable in this execution sandbox")
        adapter._run(["set-environment", "-g", "ASHA_ROOM_INPUT_FENCE", "1"])
        self.assertFalse(getattr(self.config, "idle_delivery", False))
        opened = self._open(tmux=adapter, asha_root=launcher_root, name="Default Room",
                            room_id="44444444-1111-4111-8111-111111111111")
        deadline = time.monotonic() + 3
        while not probe.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child = json.loads(probe.read_text())
        self.assertNotIn("ASHA_ROOM_INPUT_FENCE", child)
        pane = RoomStore(self.config).read(opened["room_id"])["tmux"]["pane_id"]
        options = adapter._run(["show-options", "-p", "-t", pane])
        self.assertNotIn("@asha_event_seq", options)
        self.assertNotIn("@asha_attach_gen", options)
        # A real hook callback under the child's own environment stays inert.
        recorder_root = self.root / "fence-recorder"
        (recorder_root / "bin").mkdir(parents=True)
        argv_file = self.root / "fence-recorder-argv"
        (recorder_root / "bin/asha").write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > '" + str(argv_file) + "'\necho '{}'\n", encoding="utf-8")
        (recorder_root / "bin/asha").chmod(0o700)
        socket_path, server_pid = adapter._run(
            ["display-message", "-p", "-t", pane, "#{socket_path}\t#{pid}"]).strip().split("\t")
        hook_env = dict(child, ASHA_ROOT=str(recorder_root), ASHA_HUB_SESSION_ID=str(uuid.uuid4()),
                        TMUX=f"{socket_path},{server_pid},0", TMUX_PANE=pane,
                        PATH=os.environ.get("PATH", "/usr/bin:/bin"))
        hook = Path("plugins/session/hooks/handlers/control-event.sh").resolve()
        result = subprocess.run(["bash", str(hook), "PreToolUse"], input=b"{}", env=hook_env,
                                capture_output=True, timeout=5)
        self.assertEqual(result.stdout.decode().strip(), "{}")
        self.assertNotIn("--sequence", argv_file.read_text().split("\n"))
        self.assertEqual(adapter._run(["show-options", "-p", "-t", pane]), options)
        # Only an exact "1" marker would count; anything else stays inert too.
        for marker in ("0", "1 ", "true"):
            subprocess.run(["bash", str(hook), "PreToolUse"], input=b"{}", timeout=5, capture_output=True,
                           env=dict(hook_env, ASHA_ROOM_INPUT_FENCE=marker))
            self.assertEqual(adapter._run(["show-options", "-p", "-t", pane]), options)
        close_room(RoomStore(self.config), opened["room_id"], tmux=adapter)

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
            self.assertEqual(evidence["argv"], ["codex", "line one\nline two;"])
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
