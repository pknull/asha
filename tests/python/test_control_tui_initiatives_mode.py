"""Increment 6: the Control TUI's Initiatives mode — pure screen model, render, keys, and loop."""

from __future__ import annotations

import copy
import json
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
    def test_rows_order_by_attention_then_slug_and_project_facts(self) -> None:
        screen = InitiativesScreen([
            _view("zeta-running", "running"),
            _view("alpha-approval", "awaiting-plan-approval"),
            _view("mid-paused", "paused"),
        ], height=30, width=100)
        rows = screen.rows()
        self.assertEqual([row.label for row in rows], ["alpha-approval", "zeta-running", "mid-paused"])
        self.assertEqual(rows[0].attention, "plan approval")
        self.assertEqual(rows[2].attention, "paused")
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
        self.assertEqual(lines[0], "ASHA CONTROL  [Initiatives]")
        self.assertTrue(lines[2].startswith("STATE"))
        self.assertTrue(any("plan approval" in line for line in lines))
        self.assertTrue(lines[-1].startswith("Enter open"))
        model.help_visible = True
        help_lines = render(model)
        self.assertEqual(help_lines[0], "ASHA CONTROL HELP  [Initiatives]")
        self.assertTrue(any("No merge, rebase, bookmark" in line for line in help_lines))

    def test_render_without_a_loaded_screen_shows_the_error_and_keeps_tasks_intact(self) -> None:
        model = TuiModel([], height=24, width=60)
        model.mode = "initiatives"
        model.initiatives_error = "initiatives unavailable: bad orchestration config"
        lines = render(model)
        self.assertIn("initiatives unavailable: bad orchestration config", lines)
        model.mode = "tasks"
        self.assertTrue(render(model)[0].startswith("ASHA TASKS"))

    def test_initiative_keys_map_only_to_bounded_intents(self) -> None:
        model = self.model([_view("one", "running")])
        self.assertIs(model.dispatch_key("\t").kind, IntentKind.TOGGLE_MODE)
        self.assertIs(model.dispatch_key("q").kind, IntentKind.QUIT)
        self.assertIs(model.dispatch_key("?").kind, IntentKind.HELP)
        self.assertIs(model.dispatch_key("/").kind, IntentKind.FILTER)
        self.assertIs(model.dispatch_key("RIGHT").kind, IntentKind.INIT_EXPAND)
        self.assertIs(model.dispatch_key("LEFT").kind, IntentKind.INIT_COLLAPSE)
        self.assertIs(model.dispatch_key("ENTER").kind, IntentKind.INIT_OPEN)
        approve = model.dispatch_key("a")
        self.assertIs(approve.kind, IntentKind.INIT_APPROVE)
        self.assertTrue(approve.requires_confirmation)
        self.assertTrue(model.dispatch_key("p").requires_confirmation)
        self.assertTrue(model.dispatch_key("s").requires_confirmation)
        for key in ("x", "n", "A", "m", "D", "!"):
            self.assertIs(model.dispatch_key(key).kind, IntentKind.NONE)
        # Tasks-mode bindings are untouched by the initiatives table.
        tasks = TuiModel([], height=24, width=60)
        self.assertIs(tasks.dispatch_key("a").kind, IntentKind.NONE)  # no task selected
        self.assertIs(tasks.dispatch_key("\t").kind, IntentKind.TOGGLE_MODE)

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

    def addnstr(self, _y, _x, value, _limit):
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
        screen, model = self.run_loop([9, FakeCurses.KEY_RIGHT, ord("e"), ord("c"), ord("?"), ord("?"), 9, ord("q")])
        text = screen.text()
        self.assertIn("ASHA CONTROL  [Initiatives]", text)
        self.assertIn("awaiting-one", text)
        self.assertIn("plan approval", text)
        self.assertIn("[events]", text)
        self.assertIn("[candidates]", text)
        self.assertIn("ASHA CONTROL HELP  [Initiatives]", text)
        self.assertEqual(model.mode, "tasks")
        self.assertEqual(self.records(), before)
        # A second session sees the same records; no lifecycle side effect from the TUI.
        screen, _model = self.run_loop([9, ord("q")])
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

    def test_initiatives_mode_degrades_when_orchestration_cannot_load(self) -> None:
        with mock.patch("lib.control.tui._load_initiative_views", side_effect=ValueError("boom")):
            screen, model = self.run_loop([9, ord("q")])
        self.assertIn("initiatives unavailable: boom", screen.text())
        self.assertIsNone(model.initiatives)


if __name__ == "__main__":
    unittest.main()
