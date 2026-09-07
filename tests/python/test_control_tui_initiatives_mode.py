"""Increment 6: the Control TUI's Initiatives mode — pure screen model, render, keys, and loop."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from lib.control import tui
from lib.control.jj import ImmutableTree, RepositoryFacts
from lib.control.model import TASK_CONTRACT  # noqa: F401 - import guards the Control model load
from lib.control.orchestration import cli as orchestration_cli
from lib.control.orchestration.actions import SUPPORTED_ACTION_KINDS
from lib.control.orchestration.model import FORBIDDEN_ACTION_CLASSES, record_digest
from lib.control.orchestration.tui_model import InitiativesScreen
from lib.control.store import TaskStore
from lib.control.transaction import CreationJournalStore
from lib.control.tui import IntentKind, TuiModel, render
from lib.control.tui_style import INERT, WAITING
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_orchestration_graph import valid_plan


def _view(slug: str, state: str, *, nodes=None, coordinator=None, coordinator_live=None, events=None, seals=None, approvals=None, attempts=None, links=None):
    initiative_id = f"{abs(hash(slug)) % 10**8:08d}-1111-4111-8111-111111111111"
    return {
        "initiative": {
            "initiative_id": initiative_id, "slug": slug, "label": slug.replace("-", " "),
            "state": state, "limits": {"max_parallel": 2, "max_total_tasks": 6},
        },
        "plan": {"revision": 1, "digest": "d" * 64} if state == "awaiting-plan-approval" else None,
        "nodes": nodes or [
            {"node_id": "implementation-a", "state": "succeeded", "type": "work", "goal": "Implement A"},
            {"node_id": "review-a", "state": "running", "type": "review", "goal": "Review A"},
            {"node_id": "verify-a", "state": "blocked", "type": "verify", "goal": "Verify A"},
        ],
        "attempts": attempts or [
            {"attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "node_id": "implementation-a", "ordinal": 1, "state": "sealed-success"},
            {"attempt_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "node_id": "review-a", "ordinal": 1, "state": "running"},
        ],
        "links": links or [
            {"attempt_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "control_task_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
        ],
        "events": events or [
            {"sequence": 1, "type": "initiative-created", "actor_kind": "operator", "recorded_at": "2026-08-22T10:00:00Z"},
            {"sequence": 2, "type": "plan-approved", "actor_kind": "operator", "recorded_at": "2026-08-22T10:01:00Z"},
        ],
        "coordinator": coordinator,
        "coordinator_live": coordinator_live,
        "seals": seals or [
            {"seal_id": "99999999-9999-4999-8999-999999999999", "outcome": "success", "node_id": "implementation-a",
             "attempt_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "sealed_at": "2026-08-22T10:02:00Z"},
        ],
        "reviews": [], "verifications": [], "approvals": approvals or [], "storage": None,
    }


class InitiativesScreenTests(unittest.TestCase):
    def test_rooms_branch_is_expanded_and_warns_on_shared_project_checkout(self) -> None:
        rooms = [{
            "room_id": "11111111-1111-4111-8111-111111111111",
            "name": "Draft", "project_id": "novel", "project_name": "My Novel",
            "project_root": "/projects/novel", "harness": "codex", "state": "open",
            "session": "asha-control-room-11111111", "pane_id": "%7",
            "shared_working_tree": True, "detail": "verified",
        }]
        screen = InitiativesScreen([], room_rows=rooms, height=30, width=100)
        self.assertEqual([row.kind for row in screen.rows()], ["rooms-root", "room"])
        room = screen.rows()[1]
        self.assertEqual(room.label, "Draft")
        self.assertEqual(room.type, "codex")
        self.assertEqual(room.coordinator, "My Novel")
        self.assertEqual(room.attention, "shared working tree")
        screen.move_selection(1)
        self.assertIn("Project: My Novel  /projects/novel", screen.detail_lines())
        self.assertIn("Shared checkout: yes", screen.detail_lines())

    def test_rows_order_by_attention_then_slug_and_project_facts(self) -> None:
        screen = InitiativesScreen([
            _view("zeta-running", "running"),
            _view("alpha-approval", "awaiting-plan-approval"),
            _view("mid-paused", "paused"),
        ], height=30, width=100)
        rows = screen.rows()
        self.assertEqual([row.label for row in rows], ["alpha-approval", "zeta-running", "mid-paused"])
        self.assertEqual(rows[0].attention, "plan approval")
        # Parked work is status, not demand: the STATE column already says paused.
        self.assertEqual(rows[2].attention, "-")
        self.assertFalse(rows[2].needs_human)
        self.assertEqual(rows[1].nodes, "1/3")
        self.assertEqual(rows[1].coordinator, "-")

    def test_coordinator_column_and_liveness_fact(self) -> None:
        coordinator = {"harness": "claude", "generation": 2, "state": "active", "anchor": {"pane_id": "%7"}}
        screen = InitiativesScreen([_view("one", "running", coordinator=coordinator, coordinator_live=True)], height=30, width=100)
        self.assertEqual(screen.rows()[0].coordinator, "claude g2")
        self.assertIn("Coordinator: claude generation 2 active (anchor live, pane %7)", screen.detail_lines())
        fenced = dict(coordinator, state="fenced")
        screen = InitiativesScreen([_view("one", "running", coordinator=fenced)], height=30, width=100)
        self.assertEqual(screen.rows()[0].coordinator, "fenced g2")

    def test_expand_collapse_and_filter(self) -> None:
        screen = InitiativesScreen([_view("one", "running"), _view("two", "approved")], height=30, width=100)
        self.assertEqual(len(screen.rows()), 2)
        self.assertTrue(screen.expand())
        rows = screen.rows()
        self.assertEqual([row.kind for row in rows], ["initiative", "node", "node", "node", "initiative"])
        screen.move_selection(2)  # review-a node
        self.assertEqual(screen.selected_row.id, "review-a")
        self.assertTrue(screen.expand())
        self.assertEqual([row.kind for row in screen.rows()][3], "attempt")
        self.assertTrue(screen.collapse())  # collapse the node
        self.assertEqual(screen.selected_row.id, "review-a")
        self.assertTrue(screen.collapse())  # unexpanded child -> back to parent
        self.assertEqual(screen.selected_row.kind, "initiative")
        self.assertTrue(screen.collapse())
        self.assertEqual(len(screen.rows()), 2)
        screen.set_filter("two")
        self.assertEqual([row.label for row in screen.rows()], ["two"])
        screen.set_filter("nothing-here")
        self.assertEqual(screen.rows(), [])
        self.assertIsNone(screen.selected_row)

    def test_replace_views_preserves_selection_and_detail_facts(self) -> None:
        screen = InitiativesScreen([_view("one", "running"), _view("two", "running")], height=30, width=100)
        screen.move_selection(1)
        selected = screen.selected_row.key
        refreshed = [_view("two", "running"), _view("one", "paused")]
        screen.replace_views(refreshed)
        self.assertEqual(screen.selected_row.key, selected)
        lines = screen.detail_lines()
        self.assertTrue(lines[0].startswith("two  [running]"))
        self.assertIn("Candidate:  seal 99999999 success node implementation-a", lines)
        self.assertIn("Review:     pending", lines)
        self.assertIn("Verify:     pending", lines)
        self.assertTrue(any(line.startswith("Limits:     parallel 1/2 | nodes 1/3 | tasks 1/6") for line in lines))
        self.assertIn("Storage:    not sampled", lines)
        self.assertIn("Event:      #2 plan-approved (operator)", lines)
        screen.pane = "events"
        self.assertEqual(len(screen.pane_lines()), 2)
        screen.pane = "candidates"
        self.assertTrue(screen.pane_lines()[0].startswith("seal 99999999 success"))
        screen.pane = "verification"
        self.assertEqual(screen.pane_lines(), ["no review or verification evidence"])
        screen.pane = "storage"
        self.assertEqual(screen.pane_lines(), ["storage not sampled"])

    def test_approval_fact_names_the_pending_plan(self) -> None:
        screen = InitiativesScreen([_view("pending", "awaiting-plan-approval")], height=30, width=100)
        self.assertTrue(any("Approval:   plan revision 1 digest dddddddddddddddd" in line for line in screen.detail_lines()))


class InitiativesRenderAndKeyTests(unittest.TestCase):
    def model(self, views, *, height=24, width=60) -> TuiModel:
        model = TuiModel([], height=height, width=width)
        model.mode = "initiatives"
        model.initiatives = InitiativesScreen(views, height=height, width=width)
        return model

    def test_render_is_text_only_and_bounded_on_a_small_terminal(self) -> None:
        model = self.model([_view("one", "running"), _view("two", "awaiting-plan-approval")])
        lines = render(model)
        self.assertLessEqual(len(lines), 24)
        self.assertTrue(all(len(line) <= 60 for line in lines))
        # The title carries counts the operator can audit against the rows, and
        # sheds by width: at 60 columns the default scope goes before a count,
        # because a default scope tells the operator nothing.
        self.assertTrue(lines[0].startswith("ASHA CONTROL   1 need you"))
        self.assertIn("2 initiatives", lines[0])
        self.assertNotIn("Scope: active", lines[0])
        wide = render(self.model(
            [_view("one", "running"), _view("two", "awaiting-plan-approval")], width=120,
        ))
        self.assertIn("Scope: active", wide[0])
        self.assertIn("1 need you", wide[0])
        # At 60 columns STATE and WORKER have dropped; PIPELINE and WAITING ON
        # are what a narrow pane keeps, because they are why the operator looked.
        header = lines[2].lstrip()
        self.assertTrue(header.startswith("PIPELINE"), header)
        self.assertIn("WAITING ON", header)
        self.assertNotIn("WORKER", header)
        self.assertTrue(any("plan approval" in line for line in lines))
        self.assertTrue(lines[-1].startswith("Enter attach  o room  ! need  a approve"))
        model.help_visible = True
        help_lines = render(model)
        self.assertEqual(help_lines[0], "ASHA CONTROL HELP")
        self.assertTrue(any("No merge, rebase, bookmark" in line for line in help_lines))

    def test_render_degrades_initiatives_to_a_note_and_keeps_the_task_branch(self) -> None:
        from lib.control.orchestration.tui_model import InitiativesScreen

        model = TuiModel([], height=24, width=80)
        model.initiatives = InitiativesScreen(
            [], height=24, width=80, task_rows=(),
            orchestration_error="bad orchestration config",
        )
        lines = render(model)
        self.assertTrue(any("Initiatives unavailable: bad orchestration config" in line for line in lines))
        self.assertEqual(lines[0], "ASHA CONTROL  Scope: active")
        # A bare model without any screen still renders the (empty) tree.
        bare = TuiModel([], height=24, width=80)
        lines = render(bare)
        self.assertEqual(lines[0], "ASHA CONTROL  Scope: active")
        self.assertTrue(any("Nothing to show" in line for line in lines))

    def test_initiative_keys_map_only_to_bounded_intents(self) -> None:
        model = self.model([_view("one", "running")])
        # One view now: Tab is an inert hint, never a mode switch.
        tab = model.dispatch_key("\t")
        self.assertIs(tab.kind, IntentKind.NONE)
        self.assertIn("one tree", tab.reason)
        self.assertIs(model.dispatch_key("q").kind, IntentKind.QUIT)
        self.assertIs(model.dispatch_key("?").kind, IntentKind.HELP)
        self.assertIs(model.dispatch_key("/").kind, IntentKind.FILTER)
        self.assertIs(model.dispatch_key("!").kind, IntentKind.ATTENTION)
        self.assertIs(model.dispatch_key("A").kind, IntentKind.TOGGLE_SCOPE)
        self.assertIs(model.dispatch_key("N").kind, IntentKind.START)
        self.assertIs(model.dispatch_key("RIGHT").kind, IntentKind.INIT_EXPAND)
        self.assertIs(model.dispatch_key("LEFT").kind, IntentKind.INIT_COLLAPSE)
        # Enter on the initiative row attaches to its coordinator; node rows open the worker.
        self.assertIs(model.dispatch_key("ENTER").kind, IntentKind.INIT_ATTACH)
        self.assertIs(model.dispatch_key("n").kind, IntentKind.INIT_NEW)
        approve = model.dispatch_key("a")
        self.assertIs(approve.kind, IntentKind.INIT_APPROVE)
        self.assertTrue(approve.requires_confirmation)
        self.assertTrue(model.dispatch_key("p").requires_confirmation)
        self.assertTrue(model.dispatch_key("s").requires_confirmation)
        # X targets only rows with a linked worker; an initiative row refuses it.
        self.assertIs(model.dispatch_key("X").kind, IntentKind.NONE)
        for key in ("x", "m", "D", "z"):
            self.assertIs(model.dispatch_key(key).kind, IntentKind.NONE)
        # Without a screen, row keys explain themselves instead of acting.
        bare = TuiModel([], height=24, width=60)
        self.assertIs(bare.dispatch_key("a").kind, IntentKind.NONE)

    def test_room_keys_open_attach_and_close_only_room_rows(self) -> None:
        rooms = [{
            "room_id": "11111111-1111-4111-8111-111111111111",
            "name": "Draft", "project_id": "novel", "project_name": "My Novel",
            "project_root": "/projects/novel", "harness": "codex", "state": "open",
            "session": "asha-control-room-11111111", "pane_id": "%7",
            "shared_working_tree": False, "detail": "verified",
        }]
        model = TuiModel([], height=24, width=100)
        model.initiatives = InitiativesScreen([], room_rows=rooms, height=24, width=100)
        self.assertIs(model.dispatch_key("o").kind, IntentKind.ROOM_OPEN)
        self.assertIs(model.dispatch_key("ENTER").kind, IntentKind.INIT_EXPAND)
        model.initiatives.move_selection(1)
        attach = model.dispatch_key("ENTER")
        self.assertIs(attach.kind, IntentKind.ROOM_ATTACH)
        self.assertEqual(attach.target, rooms[0]["room_id"])
        close = model.dispatch_key("X")
        self.assertIs(close.kind, IntentKind.ROOM_CLOSE)
        self.assertTrue(close.requires_confirmation)

    def test_room_close_intent_requires_exact_yes_before_the_owned_close(self) -> None:
        room_id = "11111111-1111-4111-8111-111111111111"
        intent = tui.TuiIntent(IntentKind.ROOM_CLOSE, target=room_id)
        model = TuiModel([])
        config = SimpleNamespace(asha_home="/tmp/asha-room-tui-test")
        with mock.patch("lib.control.tui._prompt_line", return_value="YES"), \
                mock.patch("lib.control.rooms.close_room") as close:
            result = tui._execute_room_intent(
                intent, stdscr=mock.Mock(), curses_module=mock.Mock(), model=model,
                config=config, env={},
            )
        self.assertEqual(result, "Room close cancelled")
        close.assert_not_called()

        with mock.patch("lib.control.tui._prompt_line", return_value="yes"), \
                mock.patch("lib.control.tui._coordinator_tmux", return_value=mock.sentinel.tmux), \
                mock.patch("lib.control.rooms.close_room", return_value={"name": "Draft"}) as close, \
                mock.patch("lib.control.tui._refresh_initiatives"):
            result = tui._execute_room_intent(
                intent, stdscr=mock.Mock(), curses_module=mock.Mock(), model=model,
                config=config, env={},
            )
        self.assertIn("project files were untouched", result)
        close.assert_called_once()
        self.assertEqual(close.call_args.args[1], room_id)
        self.assertIs(close.call_args.kwargs["tmux"], mock.sentinel.tmux)

    def test_no_forbidden_action_class_is_reachable_from_the_tui(self) -> None:
        tui_classes = {"pause", "resume", "stop-attempt"}
        self.assertTrue(tui_classes <= SUPPORTED_ACTION_KINDS)
        self.assertFalse(tui_classes & set(FORBIDDEN_ACTION_CLASSES))
        forbidden_words = {"merge", "rebase", "push", "bookmark", "delete", "remove"}
        table = " ".join(kind.value for kind in tui._INITIATIVE_KEYS.values())
        self.assertFalse(any(word in table for word in forbidden_words))


class FakeCurses:
    class error(Exception):
        pass

    KEY_ENTER = 343
    KEY_RESIZE = 410
    KEY_UP = 259
    KEY_DOWN = 258
    KEY_LEFT = 260
    KEY_RIGHT = 261


class Screen:
    def __init__(self, keys: list[int]) -> None:
        self.keys = list(keys)
        self.frames: list[list[str]] = []
        self.current: list[str] = []

    def timeout(self, _ms) -> None:
        pass

    def getmaxyx(self):
        return 24, 80

    def erase(self):
        if self.current:
            self.frames.append(self.current)
        self.current = []

    def refresh(self):
        pass

    def addnstr(self, _y, _x, value, _limit, _attribute=0):
        self.current.append(value)

    def move(self, _y, _x):
        pass

    def clrtoeol(self):
        pass

    def getch(self):
        return self.keys.pop(0) if self.keys else ord("q")

    def text(self) -> str:
        return "\n".join("\n".join(frame) for frame in self.frames + [self.current])


class InitiativesLoopTests(ExecutionFixture, unittest.TestCase):
    """Drive _curses_loop with a fake screen against a real initiative store."""

    start_running = False

    def setUp(self) -> None:
        super().setUp()
        self.control_config = self.config.control
        self.tasks = TaskStore(self.control_config)
        self.journals = CreationJournalStore(self.control_config)
        self.jj = mock.Mock()
        self.jj.preflight.return_value = RepositoryFacts(root=self.repo, git_root=self.repo / ".git")
        self.jj.immutable_tree.return_value = ImmutableTree(commit_id="b" * 40, digest="c" * 64, entries=())
        created = orchestration_cli._create([
            "--repo", str(self.repo), "--slug", "awaiting-one",
            "--label", "Awaiting one", "--objective", "Await approval.",
        ], self.config, self.store, self.jj)["initiative"]
        self.pending_id = created["initiative_id"]
        plan_value = valid_plan()
        plan_value["initiative_id"] = self.pending_id
        plan_value["repositories"] = [copy.deepcopy(created["scope"]["repository"])]
        repository_id = created["scope"]["repository"]["repository_id"]
        for node in plan_value["nodes"]:
            if node["repository_id"] is not None:
                node["repository_id"] = repository_id
        plan_file = self.root / "pending-plan.json"
        plan_file.write_text(json.dumps(plan_value))
        self.pending_plan, _ = orchestration_cli._plan(
            [self.pending_id, "--file", str(plan_file)], self.store, self.config, jj=self.jj,
        )

    def run_loop(self, keys: list[int]) -> tuple[Screen, TuiModel]:
        model = TuiModel([], height=24, width=80)
        screen = Screen(keys)
        with mock.patch("lib.control.tui._load_rows", return_value=[]):
            code = tui._curses_loop(
                screen, FakeCurses, model, self.control_config, self.env,
                self.tasks, self.journals, self.jj,
            )
        self.assertEqual(code, 0)
        return screen, model

    def test_initiatives_start_mode_enters_directly_and_degrades_alone(self) -> None:
        model = TuiModel([], height=24, width=80)
        tui._enter_tree(model, self.env)
        self.assertIsNotNone(model.initiatives)
        self.assertIsNone(model.initiatives_error)
        self.assertIsNone(model.initiatives.orchestration_error)
        broken = self.root / "broken-config.json"
        broken.write_text("{not json")
        degraded = TuiModel([], height=24, width=80)
        tui._enter_tree(degraded, {**self.env, "ASHA_CONFIG": str(broken)})
        self.assertIsNotNone(degraded.initiatives, "tasks must survive a broken orchestration config")
        self.assertEqual(degraded.initiatives.views, [])
        self.assertTrue(str(degraded.initiatives_error))
        screen = Screen([ord("q")])
        with mock.patch("lib.control.tui._load_rows", return_value=[]):
            code = tui._curses_loop(
                screen, FakeCurses, degraded, self.control_config, self.env,
                self.tasks, self.journals, self.jj,
            )
        self.assertEqual(code, 0)

    def test_n_prompts_for_an_intent_launches_the_coordinator_and_opens_its_popup(self) -> None:
        calls: dict = {}

        def fake_launch(config, *, root, intent, tmux, asha_root, harness="claude", token=None):
            calls["launch"] = {"root": str(root), "intent": intent, "harness": harness}
            return {"session": "asha-coord-feedbeef", "pane_id": "%9"}

        def fake_popup(stdscr, curses_module, config, env, session, label):
            calls["popup"] = (session, label)
            return None

        self.env = {**self.env, "ASHA_PROJECTS_ROOT": str(self.root)}
        with mock.patch("lib.control.orchestration.coordinator.launch_session", fake_launch), \
             mock.patch("lib.control.tui._popup_session", fake_popup):
            _screen, model = self.run_loop([ord("n"), *map(ord, "update termart"), 10, ord("q")])
        self.assertEqual(calls["launch"], {"root": str(self.root), "intent": "update termart", "harness": "claude"})
        self.assertEqual(calls["popup"], ("asha-coord-feedbeef", "coordinator"))
        self.assertIn("coordinator session asha-coord-feedbeef started", model.message)

    def test_n_with_an_empty_intent_launches_nothing(self) -> None:
        with mock.patch("lib.control.orchestration.coordinator.launch_session") as launch:
            _screen, model = self.run_loop([9, ord("n"), 10, ord("q")])
        launch.assert_not_called()
        self.assertEqual(model.message, "intent cancelled")

    def test_enter_on_an_initiative_row_attaches_to_its_live_coordinator_or_explains(self) -> None:
        from lib.control.orchestration.coordinator import claim
        from tests.python.test_orchestration_coordinator_sessions import LaunchingTmux

        fake = LaunchingTmux()
        popups: list = []
        with mock.patch("lib.control.tui._coordinator_tmux", return_value=fake), \
             mock.patch("lib.control.tui._popup_session", lambda *args: popups.append(args[4]) or None):
            _screen, model = self.run_loop([9, 10, ord("q")])
            self.assertIn("no coordinator has claimed this initiative", model.message)
            self.assertEqual(popups, [])
            selected = model.initiatives.selected_row.initiative_id
            claim(self.store, self.store.peek(selected), env={**self.env, "TMUX_PANE": fake.pane_id}, tmux=fake)
            _screen, model = self.run_loop([9, 10, ord("q")])
        self.assertEqual(popups, ["keeper"])
        self.assertIn("coordinator popup closed", model.message)

    def records(self) -> dict:
        return {
            iid: (
                record_digest(self.store.peek(iid)),
                [record_digest(node) for node in self.store.list_nodes_snapshot(iid)],
                len(self.store.list_events_snapshot(iid)),
            )
            for iid in (self.initiative_id, self.pending_id)
        }

    def test_tab_switches_modes_and_restart_leaves_records_untouched(self) -> None:
        before = self.records()
        screen, model = self.run_loop([9, FakeCurses.KEY_RIGHT, ord("e"), ord("c"), ord("?"), ord("?"), ord("q")])
        text = screen.text()
        self.assertIn("ASHA CONTROL", text)
        self.assertIn("awaiting-one", text)
        self.assertIn("plan approval", text)
        self.assertIn("[events]", text)
        self.assertIn("[candidates]", text)
        self.assertIn("ASHA CONTROL HELP", text)
        self.assertIn("one tree", text, "Tab explains itself instead of switching modes")
        self.assertEqual(self.records(), before)
        # A second session sees the same records; no lifecycle side effect from the TUI.
        screen, _model = self.run_loop([ord("q")])
        self.assertEqual(self.records(), before)
        self.assertIn("awaiting-one", screen.text())

    def test_operator_approves_the_pending_plan_from_the_tui(self) -> None:
        keys = [9, ord("a"), *map(ord, "approve"), 10, ord("q")]
        screen, _model = self.run_loop(keys)
        text = screen.text()
        self.assertIn("Plan approval", text)
        self.assertEqual(self.store.peek(self.pending_id)["state"], "approved")
        approved = [event for event in self.store.list_events_snapshot(self.pending_id) if event["type"] == "plan-approved"]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["actor_kind"], "operator")
        self.assertIn("approved; activate from the CLI", text)

    def test_rejection_requires_a_reason_and_records_the_tui_actor(self) -> None:
        keys = [9, ord("a"), *map(ord, "reject"), 10, *map(ord, "too broad"), 10, ord("q")]
        screen, _model = self.run_loop(keys)
        self.assertEqual(self.store.peek(self.pending_id)["state"], "planning")
        rejected = [event for event in self.store.list_events_snapshot(self.pending_id) if event["type"] == "plan-rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["actor_id"], "tui")
        self.assertEqual(rejected[0]["payload"]["reason"], "too broad")
        self.assertIn("rejected", screen.text())

    def test_pause_and_resume_from_the_tui_are_confirmed_operator_actions(self) -> None:
        self.set_running(self.store.peek(self.initiative_id))
        # Running sorts after awaiting-plan-approval: move down once.
        keys = [9, FakeCurses.KEY_DOWN, ord("p"), *map(ord, "yes"), 10, ord("p"), *map(ord, "yes"), 10, ord("q")]
        screen, _model = self.run_loop(keys)
        text = screen.text()
        self.assertIn("Pause initiative", text)
        self.assertIn("Resume initiative", text)
        actions = sorted(
            self.store.list_actions_snapshot(self.initiative_id), key=lambda item: item["received_at"],
        )
        self.assertEqual([action["action_class"] for action in actions], ["pause", "resume"])
        self.assertTrue(all(action["actor_id"] == "tui" and action["state"] == "completed" for action in actions))
        self.assertEqual(self.store.peek(self.initiative_id)["state"], "running")

    def test_p_parks_a_waiting_initiative_and_resumes_only_a_paused_one(self) -> None:
        from lib.control.orchestration.actions import build_action_document, submit_action
        from lib.control.orchestration.coordinator import claim
        from tests.python.test_orchestration_coordinator_claim import FakeTmux

        self.set_running(self.store.peek(self.initiative_id))
        record = claim(
            self.store, self.store.peek(self.initiative_id),
            env={**self.env, "TMUX_PANE": "%7"}, tmux=FakeTmux(),
        )
        asked = submit_action(self.store, self.initiative_id, build_action_document(
            self.store.peek(self.initiative_id), "request-decision",
            {"subject_id": "implementation-a", "question": "Which base should the retry use?"},
            actor_id=f"coordinator:{record['coordinator_id']}", coordinator=record,
        ))
        self.assertEqual(asked["state"], "completed", asked["outcome"])
        self.assertEqual(self.store.peek(self.initiative_id)["state"], "needs-input")
        question = next(
            event for event in self.store.list_events_snapshot(self.initiative_id)
            if event["type"] == "approval-requested"
        )
        # needs-input sorts first, so the waiting initiative is already selected.
        keys = [
            9, ord("p"), *map(ord, "no"), 10,   # anything but exact yes records nothing
            ord("p"), *map(ord, "yes"), 10,    # parks the question instead of resuming it
            ord("p"), *map(ord, "yes"), 10,    # only a paused initiative resumes
            ord("p"), *map(ord, "yes"), 10,    # the restored question parks again, never resumes
            ord("q"),
        ]
        screen, model = self.run_loop(keys)
        text = screen.text()
        self.assertIn("pause cancelled", text)
        self.assertIn("Pause initiative", text)
        self.assertIn("Resume initiative", text)
        # The 80-column fake screen wraps the confirmation text mid-sentence.
        self.assertIn("returns to needs-input", text)
        # The result line names the state the resume actually recorded.
        self.assertIn("resume: completed (needs-input)", text)
        operator_actions = [
            (action["action_class"], action["actor_id"], action["state"])
            for action in sorted(
                self.store.list_actions_snapshot(self.initiative_id),
                key=lambda item: item["received_at"],
            )
            if action["actor_kind"] == "operator"
        ]
        self.assertEqual(
            operator_actions, [
                ("pause", "tui", "completed"), ("resume", "tui", "completed"),
                ("pause", "tui", "completed"),
            ],
        )
        self.assertEqual(self.store.peek(self.initiative_id)["state"], "paused")
        self.assertEqual(
            [
                (event["payload"]["from"], event["payload"]["to"])
                for event in self.store.list_events_snapshot(self.initiative_id)
                if event["type"] == "initiative-state-changed"
            ],
            [
                ("running", "needs-input"), ("needs-input", "paused"),
                ("paused", "needs-input"), ("needs-input", "paused"),
            ],
        )
        self.assertIn(
            question,
            self.store.list_events_snapshot(self.initiative_id),
            "the question is restored from its record, never re-asked",
        )
        self.assertTrue(str(model.message).startswith("pause: completed"), model.message)

    def test_initiatives_mode_degrades_when_orchestration_cannot_load(self) -> None:
        with mock.patch("lib.control.tui._load_initiative_views", side_effect=ValueError("boom")):
            screen, model = self.run_loop([ord("q")])
        self.assertIn("Initiatives unavailable: boom", screen.text())
        self.assertIsNotNone(model.initiatives, "the task branch must survive orchestration failure")
        self.assertEqual(model.initiatives.orchestration_error, "boom")

    def test_rooms_loading_failure_does_not_hide_initiatives_or_tasks(self) -> None:
        with mock.patch("lib.control.tui._load_room_rows", side_effect=ValueError("room store broken")):
            screen, model = self.run_loop([ord("q")])
        self.assertIn("Rooms unavailable: room store broken", screen.text())
        self.assertIsNotNone(model.initiatives)
        self.assertTrue(model.initiatives.views)
        self.assertEqual(model.initiatives.rooms_error, "room store broken")



class _FakeObservation:
    def __init__(self, observed_at="2026-08-24T10:00:00Z"):
        self.observed_at = observed_at
        self.run_id = "run-1"
        self.detail = "owned pane matched"
        self.source = "tmux"
        self.freshness = "fresh"


class _FakeTaskRow:
    """Duck-typed stand-in for the Tasks-side TuiRow inside the pure tree."""

    def __init__(self, task_id, slug, display_state="working", evidence=(), blocker=None,
                 harness="claude"):
        self.task = {
            "task_id": task_id, "slug": slug,
            "runs": [{"run_id": "run-1", "harness": harness, "pane_id": "%9",
                      "role": "implementer", "state": "active"}],
            "tmux": {"session": f"s-{slug}", "window": "main", "socket": "default"},
            "jj": {"workspace_path": f"/ws/{slug}", "change_id": "zz"},
            "lifecycle": "running",
        }
        self.display_state = display_state
        self.summary = {"slug": slug, "label": slug, "repository": {"root": "/r", "identity": "i"}}
        self.observation = _FakeObservation()
        self.reconciliation = {"state": display_state, "blocker": blocker,
                               "evidence": list(evidence), "runs": []}


class UnifiedTreeTests(unittest.TestCase):
    """The one-tree ruling: workers inline, unbound branch, attention filter."""

    def screen(self, views, task_rows, **kwargs):
        from lib.control.orchestration.tui_model import InitiativesScreen

        return InitiativesScreen(views, height=24, width=120, task_rows=task_rows, **kwargs)

    def test_node_rows_carry_their_worker_state_and_awaiting_exit_attention(self) -> None:
        worker = _FakeTaskRow("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "review-worker", display_state="idle")
        view = _view("smoke", "running")
        view["attempts"][1]["state"] = "reported"
        screen = self.screen([view], (worker,))
        screen.expanded.add(("initiative", view["initiative"]["initiative_id"]))
        rows = {row.id: row for row in screen.rows() if row.kind == "node"}
        review = rows["review-a"]
        self.assertEqual(review.worker, "idle")
        self.assertEqual(review.attention, "awaiting exit (X closes)")
        self.assertEqual(review.task_id, "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
        self.assertEqual(rows["implementation-a"].worker, "-")

    def test_prompt_stuck_worker_surfaces_on_its_node_row(self) -> None:
        worker = _FakeTaskRow(
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "review-worker",
            display_state="needs-input",
            evidence=[{"state": "needs-input", "detail": "pane shows the claude input prompt 'trust'"}],
        )
        view = _view("smoke", "running")
        screen = self.screen([view], (worker,))
        screen.expanded.add(("initiative", view["initiative"]["initiative_id"]))
        review = next(row for row in screen.rows() if row.id == "review-a")
        self.assertTrue(review.attention.startswith("at prompt"))
        self.assertIn("claude input prompt", review.attention)

    def test_unbound_tasks_sit_under_one_branch_when_initiatives_exist(self) -> None:
        bound = _FakeTaskRow("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "bound-worker")
        loose = _FakeTaskRow("dddddddd-dddd-4ddd-8ddd-dddddddddddd", "loose-task")
        screen = self.screen([_view("smoke", "running")], (bound, loose))
        kinds = [(row.kind, row.label) for row in screen.rows()]
        self.assertIn(("tasks-root", "Unbound tasks"), kinds)
        labels = [label for kind, label in kinds if kind == "task"]
        self.assertEqual(labels, ["loose-task"], "bound workers never appear as unbound tasks")

    def test_attention_filter_keeps_only_rows_waiting_on_a_human(self) -> None:
        stuck = _FakeTaskRow(
            "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "stuck-task", display_state="needs-input",
            evidence=[{"state": "needs-input", "detail": "prompt"}],
        )
        quiet = _FakeTaskRow("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "quiet-task")
        views = [_view("calm", "running"), _view("waiting", "awaiting-plan-approval")]
        screen = self.screen(views, (stuck, quiet), attention_only=True)
        rows = screen.rows()
        labels = {row.label for row in rows}
        self.assertIn("waiting", labels)
        self.assertIn("stuck-task", labels)
        self.assertNotIn("calm", labels)
        self.assertNotIn("quiet-task", labels)

    def test_attention_items_join_initiative_and_task_waits_with_resolutions(self) -> None:
        from lib.control.orchestration.tui_model import attention_items

        stuck = _FakeTaskRow(
            "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "stuck-task", display_state="needs-input",
            evidence=[{"state": "needs-input", "detail": "prompt"}],
        )
        views = [_view("waiting", "awaiting-plan-approval")]
        items = attention_items(views, (stuck,))
        kinds = {item["kind"] for item in items}
        self.assertIn("plan-approval", kinds)
        self.assertIn("task", kinds)
        plan = next(item for item in items if item["kind"] == "plan-approval")
        self.assertIn("asha initiative approve", plan["resolution"])
        self.assertIn("d" * 64, plan["resolution"])

    def test_ready_zero_attempt_node_distinguishes_parked_from_armed_coordinator(self) -> None:
        from lib.control.orchestration.tui_model import attention_items

        coordinator = {
            "harness": "claude", "generation": 1, "state": "active",
            "updated_at": "2000-01-01T00:00:00Z", "anchor": {"pane_id": "%7"},
        }
        view = _view(
            "parked", "running", coordinator=coordinator, coordinator_live=True,
            nodes=[{
                "node_id": "implementation-a", "state": "ready",
                "type": "work", "goal": "Implement A",
            }],
        )
        view["attempts"] = []
        view["events"] = []

        items = attention_items([view])

        self.assertEqual([item["kind"] for item in items], ["coordinator-parked"])
        self.assertIn("zero attempts", items[0]["detail"])
        self.assertIn("300 seconds", items[0]["detail"])
        self.assertIn("coordinator attach", items[0]["resolution"])
        # Under `!` the parked node is listed with its head even though the
        # head is collapsed: expansion is not a filter on demand.
        filtered = self.screen([view], (), attention_only=True).rows()
        self.assertEqual([row.label for row in filtered], ["parked", "Implement A"])
        self.assertEqual([row.attention for row in filtered], ["coordinator parked"] * 2)

        armed = copy.deepcopy(view)
        armed["coordinator"]["state"] = "waiting"
        armed["coordinator"]["updated_at"] = "2999-01-01T00:00:00Z"
        self.assertEqual(attention_items([armed]), [])
        self.assertEqual(self.screen([armed], (), attention_only=True).rows(), [])

    def parked_view(self, state: str = "paused") -> dict:
        """One initiative with every kind of child ask beneath a head in `state`."""
        return _view(
            "parked", state,
            nodes=[
                {"node_id": "decision-a", "state": "needs-input", "type": "work", "goal": "Decide A"},
                {"node_id": "review-a", "state": "running", "type": "review", "goal": "Review A"},
                {"node_id": "exit-a", "state": "evaluating", "type": "verify", "goal": "Verify A"},
                {"node_id": "ended-a", "state": "needs-input", "type": "work", "goal": "Ended A"},
            ],
            attempts=[
                {"attempt_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "node_id": "review-a", "ordinal": 1, "state": "running"},
                {"attempt_id": "11111111-1111-4111-8111-111111111111", "node_id": "exit-a", "ordinal": 1, "state": "reported"},
                {"attempt_id": "22222222-2222-4222-8222-222222222222", "node_id": "ended-a", "ordinal": 1, "state": "sealed-paused"},
            ],
            links=[
                {"attempt_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "control_task_id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc"},
                {"attempt_id": "11111111-1111-4111-8111-111111111111", "control_task_id": "33333333-3333-4333-8333-333333333333"},
                {"attempt_id": "22222222-2222-4222-8222-222222222222", "control_task_id": "44444444-4444-4444-8444-444444444444"},
            ],
        )

    @staticmethod
    def parked_workers() -> tuple:
        prompt = _FakeTaskRow(
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc", "review-worker", display_state="needs-input",
            evidence=[{"state": "needs-input", "detail": "pane shows the claude input prompt 'trust'"}],
        )
        exiting = _FakeTaskRow("33333333-3333-4333-8333-333333333333", "verify-worker", display_state="idle")
        ended = _FakeTaskRow(
            "44444444-4444-4444-8444-444444444444", "ended-worker", display_state="exited",
            evidence=[{"state": "needs-input", "detail": "historical prompt before exit"}],
        )
        ended.task["lifecycle"] = "ended"
        return prompt, exiting, ended

    def test_parking_hides_idle_node_demand_but_never_a_live_ask(self) -> None:
        from lib.control.orchestration.tui_model import attention_items

        view = self.parked_view("paused")
        initiative_id = view["initiative"]["initiative_id"]
        workers = self.parked_workers()
        screen = self.screen([view], workers)
        screen.expanded.add(("initiative", initiative_id))
        rows = {row.id: row for row in screen.rows()}
        head = rows[initiative_id]
        self.assertEqual((head.state, head.attention, head.needs_human), ("paused", "-", False))
        self.assertEqual(head.display, (INERT, "paused"))
        # Readable but parked: the pending decision is not the operator's move now.
        self.assertEqual((rows["decision-a"].state, rows["decision-a"].attention), ("needs-input", "-"))
        # A prompt on screen and an exit to close are happening now, whatever the head says.
        self.assertTrue(rows["review-a"].attention.startswith("at prompt"))
        self.assertEqual(rows["exit-a"].attention, "awaiting exit (X closes)")
        # A prompt the ended worker once showed is history, not a live ask.
        self.assertEqual((rows["ended-a"].worker, rows["ended-a"].attention), ("exited", "-"))

        filtered = self.screen([view], workers, attention_only=True)
        filtered.expanded.add(("initiative", initiative_id))
        self.assertEqual([row.id for row in filtered.rows()], [initiative_id, "exit-a", "review-a"])
        # Collapsed, the live prompt and the exit to close are still listed
        # with their paused head: hidden-by-collapse is not parking.
        collapsed = self.screen([view], workers, attention_only=True)
        self.assertNotIn(("initiative", initiative_id), collapsed.expanded)
        self.assertEqual([row.id for row in collapsed.rows()], [initiative_id, "exit-a", "review-a"])
        self.assertEqual(
            [(row.kind, row.depth, row.attention) for row in collapsed.rows()],
            [(row.kind, row.depth, row.attention) for row in filtered.rows()],
        )
        self.assertEqual(
            sorted((item["kind"], item.get("node_id")) for item in attention_items([view], workers)),
            [("worker", "exit-a"), ("worker", "review-a")],
        )
        # The normal tree keeps the collapse: only the head, readable and quiet.
        normal = self.screen([view], workers)
        self.assertEqual([row.id for row in normal.rows()], [initiative_id])
        self.assertEqual(normal.rows()[0].attention, "-")
        # With nothing live beneath it (the ended worker's old prompt is
        # history), the parked head leaves `!` collapsed and expanded alike.
        idle_workers = (workers[2],)
        self.assertEqual(self.screen([view], idle_workers, attention_only=True).rows(), [])
        idle_expanded = self.screen([view], idle_workers, attention_only=True)
        idle_expanded.expanded.add(("initiative", initiative_id))
        self.assertEqual(idle_expanded.rows(), [])
        self.assertEqual(attention_items([view], idle_workers), [])

        # Resumed, the same records expose the parked decisions again.
        resumed = self.parked_view("running")
        screen = self.screen([resumed], workers)
        screen.expanded.add(("initiative", initiative_id))
        rows = {row.id: row for row in screen.rows()}
        self.assertEqual(rows["decision-a"].attention, "needs input")
        self.assertEqual(rows["ended-a"].attention, "needs input")
        self.assertTrue(rows["review-a"].attention.startswith("at prompt"))
        self.assertEqual(rows["exit-a"].attention, "awaiting exit (X closes)")
        self.assertEqual(
            sorted((item["kind"], item.get("node_id")) for item in attention_items([resumed], workers)),
            [("needs-input", "decision-a"), ("needs-input", "ended-a"),
             ("worker", "exit-a"), ("worker", "review-a")],
        )
        # A running head hides nothing by collapse under `!` either.
        for expanded in (False, True):
            running_filter = self.screen([resumed], workers, attention_only=True)
            if expanded:
                running_filter.expanded.add(("initiative", initiative_id))
            self.assertEqual(
                [row.id for row in running_filter.rows()],
                [initiative_id, "decision-a", "ended-a", "exit-a", "review-a"],
            )

    def test_parked_approval_and_unrelated_rows_stay_while_a_parked_coordinator_does_not(self) -> None:
        from lib.control.orchestration.tui_model import attention_items

        asked = _view(
            "asked", "paused",
            approvals=[{"state": "requested", "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}],
        )
        coordinator = {
            "harness": "claude", "generation": 1, "state": "active",
            "updated_at": "2000-01-01T00:00:00Z", "anchor": {"pane_id": "%7"},
        }
        idle = _view(
            "idle", "paused", coordinator=coordinator, coordinator_live=True,
            nodes=[{"node_id": "implementation-a", "state": "ready", "type": "work", "goal": "Implement A"}],
        )
        idle["attempts"], idle["links"], idle["events"] = [], [], []
        stuck = _FakeTaskRow(
            "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "stuck-task", display_state="needs-input",
            evidence=[{"state": "needs-input", "detail": "prompt"}],
        )
        room = {
            "room_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee", "name": "aas-discussion",
            "state": "open", "harness": "claude", "project_name": "AAS",
            "shared_working_tree": False, "updated_at": "2026-08-24T10:00:00Z",
        }
        expanded = {("initiative", view["initiative"]["initiative_id"]) for view in (asked, idle)}

        normal = self.screen([asked, idle], (stuck,), room_rows=[room])
        normal.expanded |= expanded
        rows = {row.label: row for row in normal.rows()}
        self.assertEqual((rows["asked"].attention, rows["asked"].display), ("salvage approval", (WAITING, "needs you")))
        self.assertTrue(rows["asked"].needs_human)
        self.assertEqual((rows["idle"].attention, rows["idle"].display), ("-", (INERT, "paused")))
        self.assertEqual(rows["Implement A"].attention, "-", "a parked coordinator is not a demand while parked")
        self.assertIn("aas-discussion", rows)
        self.assertIn("stuck-task", rows)

        filtered = self.screen([asked, idle], (stuck,), room_rows=[room], attention_only=True)
        filtered.expanded |= expanded
        self.assertEqual(
            [(row.kind, row.label) for row in filtered.rows()],
            [("initiative", "asked"), ("tasks-root", "Unbound tasks"), ("task", "stuck-task")],
        )
        self.assertEqual(
            [(item["kind"], item["slug"]) for item in attention_items([asked, idle], (stuck,))],
            [("salvage-approval", "asked"), ("task", None)],
        )

        # The same idle coordinator under running work is parked demand again.
        running = copy.deepcopy(idle)
        running["initiative"]["state"] = "running"
        self.assertEqual([item["kind"] for item in attention_items([running])], ["coordinator-parked"])
        resumed = self.screen([running], ())
        resumed.expanded |= expanded
        self.assertEqual(
            [row.attention for row in resumed.rows()], ["coordinator parked", "coordinator parked"],
        )

    def test_close_worker_sends_the_quit_command_only_after_an_exact_yes(self) -> None:
        worker = _FakeTaskRow("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "review-worker", display_state="idle")
        sent: list = []
        adapter = mock.Mock()
        adapter.send_line = lambda pane, text: sent.append((pane, text))
        row = tui.TuiRow.from_records if False else None
        del row
        model = TuiModel([], height=24, width=100)
        with mock.patch("lib.control.tui._adapter_for_task", return_value=adapter), \
             mock.patch("lib.control.tui._prompt_line", return_value="yes"):
            message = tui._close_worker(None, None, model, mock.Mock(), {}, worker)
        self.assertEqual(sent, [("%9", "/exit")])
        self.assertIn("sent /exit to review-worker", message)
        with mock.patch("lib.control.tui._adapter_for_task", return_value=adapter), \
             mock.patch("lib.control.tui._prompt_line", return_value="no"):
            message = tui._close_worker(None, None, model, mock.Mock(), {}, worker)
        self.assertEqual(len(sent), 1, "declining must send nothing")
        self.assertEqual(message, "close cancelled")
        codex_row = _FakeTaskRow("ffffffff-ffff-4fff-8fff-ffffffffffff", "codex-w", harness="codex")
        with mock.patch("lib.control.tui._adapter_for_task", return_value=adapter), \
             mock.patch("lib.control.tui._prompt_line", return_value="yes"):
            tui._close_worker(None, None, model, mock.Mock(), {}, codex_row)
        self.assertEqual(sent[-1], ("%9", "/quit"))
        opencode_row = _FakeTaskRow("11111111-2222-4333-8444-555555555555", "oc-w", harness="opencode")
        with mock.patch("lib.control.tui._adapter_for_task", return_value=adapter):
            message = tui._close_worker(None, None, model, mock.Mock(), {}, opencode_row)
        self.assertIn("no known quit command", message)


    def test_attention_verb_emits_the_assembler_payload(self) -> None:
        import contextlib
        import io
        import json as json_module
        from lib.control.orchestration import cli as orchestration_cli

        stuck = _FakeTaskRow(
            "dddddddd-dddd-4ddd-8ddd-dddddddddddd", "stuck-task", display_state="needs-input",
            evidence=[{"state": "needs-input", "detail": "prompt"}],
        )
        import tempfile
        from pathlib import Path as _Path

        base = _Path(tempfile.mkdtemp()).resolve()
        env = {
            "HOME": str(base / "home"), "ASHA_CONFIG": str(base / "missing.json"),
            "ASHA_HOME": str(base / "asha"),
            "XDG_RUNTIME_DIR": str(base / "runtime"),
        }
        for key in ("HOME", "ASHA_HOME", "XDG_RUNTIME_DIR"):
            _Path(env[key]).mkdir(mode=0o700)
        with mock.patch("lib.control.tui._load_initiative_views", return_value=[_view("waiting", "awaiting-plan-approval")]), \
             mock.patch("lib.control.cli._load_rows_for_attention", return_value=(stuck,)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = orchestration_cli.main(["initiative", "attention", "--json"], env=env)
        self.assertEqual(rc, 0)
        payload = json_module.loads(out.getvalue())
        self.assertEqual(payload["contract"], "asha.orchestration-attention.v1")
        self.assertEqual({item["kind"] for item in payload["items"]}, {"plan-approval", "task"})


if __name__ == "__main__":
    unittest.main()
