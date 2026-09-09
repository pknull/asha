import subprocess
import time
import unittest
from unittest import mock

from lib.control import tui, tui_action_queue
from lib.control.orchestration.current_actions import overview, page
from lib.control.orchestration.observation import current_activity, render_startup_observation
from lib.control.tmux import TmuxAdapter
from tests.python import test_orchestration_current_actions as fixtures
from tests.python.test_control_tui_focus import FakeCurses, FakeScreen


class ActionQueueTests(unittest.TestCase):
    setUp = fixtures.CurrentActionTests.setUp
    head = fixtures.CurrentActionTests.head
    approval = fixtures.CurrentActionTests.approval

    def test_global_key_is_available_without_a_selected_tree_row(self):
        self.assertEqual(tui.TuiModel([]).dispatch_key("G").kind, tui.IntentKind.CURRENT_ACTIONS)

    def test_legacy_root_browser_reports_unavailable_without_creating_database(self):
        from pathlib import Path
        env = {**self.env, "ASHA_HOME": str(Path(self.env["HOME"]) / "fresh")}
        message = tui_action_queue.inspect_actions(None, None, tui.TuiModel([]), env)
        self.assertIn("SQLite registry", message)
        self.assertFalse(Path(env["ASHA_HOME"]).exists())

    def test_overview_and_browser_construct_only_read_only_database_connections(self):
        from lib.control.database import ControlDatabase
        from lib.control.orchestration import current_actions
        def read_only(*args, **kwargs):
            self.assertTrue(kwargs.get("read_only"))
            return ControlDatabase(*args, **kwargs)
        with mock.patch.object(current_actions, "ControlDatabase", side_effect=read_only), \
             mock.patch("lib.control.registry_backend.selected_backend", side_effect=AssertionError("write-capable selector")), \
             mock.patch.object(tui, "_prompt_line", return_value=None):
            self.assertTrue(overview(self.config)["initialized"])
            tui_action_queue.inspect_actions(None, None, tui.TuiModel([]), self.env)

    def test_next_page_and_other_family_are_reachable_without_tree_loading(self):
        for index in range(1, 52):
            last = self.head(index, "needs-input")
        request = self.approval(self.head(100))
        with mock.patch.object(tui, "_prompt_line", side_effect=["next", "1", "family", "1", None]), \
             mock.patch.object(tui_action_queue, "_inspect") as inspect, \
             mock.patch.object(tui, "_load_initiative_views", side_effect=AssertionError("tree sampling")):
            tui_action_queue.inspect_actions(None, None, tui.TuiModel([]), self.env)
        self.assertEqual(inspect.call_args_list[0].args[2]["initiative_id"], last["initiative_id"])
        self.assertEqual(inspect.call_args_list[1].args[2]["request_id"], request["request_id"])

    def test_timeout_offers_retry_and_retains_cursor(self):
        self.head(1, "needs-input")
        self.head(2, "needs-input")
        first = page(self.store, limit=1)
        stalled = page(self.store, limit=1, after=first["next"], deadline=time.monotonic() - 1)
        last = page(self.store, limit=1, after=first["next"])
        with mock.patch.object(tui_action_queue, "page", side_effect=[first, stalled, last]) as read, \
             mock.patch.object(tui, "_prompt_line", side_effect=["next", "retry", None]) as prompt:
            tui_action_queue.inspect_actions(None, None, tui.TuiModel([]), self.env)
        self.assertEqual(read.call_args_list[1].kwargs["after"], first["next"])
        self.assertEqual(read.call_args_list[2].kwargs["after"], first["next"])
        self.assertIn("retry", [c.value for c in prompt.call_args_list[1].kwargs["candidates"]])

    def test_inspector_scrolls_and_approval_keys_cannot_make_decisions(self):
        screen = FakeScreen(["a", 338, "\x1b"], height=8, width=27)
        with mock.patch.object(tui, "_draw_modal_frame") as draw:
            tui_action_queue._inspect(screen, FakeCurses(), {"detail": "界👩‍💻\x1b[31m " * 100, "digest": "a" * 64})
        self.assertEqual(draw.call_count, 2)
        for call in draw.call_args_list:
            frame = call.args[2]
            self.assertLessEqual(len(frame.rows), 8)
            self.assertTrue(all(tui._cell_width(row) <= 26 for row in frame.rows))

    def test_both_header_summaries_remain_visible_and_bounded(self):
        model = tui.TuiModel([], height=24, width=80)
        model.actions_summary = "2 initiative decisions, 1 approval request"
        model.managed_summary = "3 managed sessions, 1 question"
        lines = tui.render(model)
        self.assertIn("G actions", str(lines[1]))
        self.assertIn("M questions", str(lines[2]))
        self.assertLessEqual(len(lines), 24)
        self.assertTrue(all(tui._cell_width(str(line)) <= 80 for line in lines))

    def test_review_key_supports_wide_and_integer_terminal_input(self):
        for key in ['r', ord('r')]:
            with self.subTest(key=key), mock.patch.object(tui, '_draw_modal_frame'), \
                 mock.patch.object(tui, '_read_modal_key', return_value=key):
                result = tui_action_queue._inspect(FakeScreen([]), FakeCurses(),
                                                  {'digest': 'a' * 64}, next_action='review exact action')
            self.assertEqual(result, 'review')

    def test_resolution_keeps_page_position_and_refresh_clears_old_notice(self):
        self.head(1, 'needs-input')
        self.head(2, 'needs-input')
        first = page(self.store, limit=1)
        later = page(self.store, limit=1, after=first['next'])
        with mock.patch.object(tui_action_queue, 'page', side_effect=[first, later, later, first]) as read, \
             mock.patch.object(tui, '_prompt_line', side_effect=['next', '1', 'refresh', None]) as prompt, \
             mock.patch.object(tui_action_queue, '_inspect', return_value='review'), \
             mock.patch.object(tui_action_queue, '_resolve', return_value='decision recorded'):
            tui_action_queue.inspect_actions(None, None, tui.TuiModel([]), self.env)
        self.assertEqual([c.kwargs['after'] for c in read.call_args_list],
                         [None, first['next'], first['next'], None])
        self.assertIn('decision recorded', prompt.call_args_list[2].kwargs['context'])
        self.assertNotIn('decision recorded', prompt.call_args_list[3].kwargs['context'])

    def test_overview_reports_partial_counts_and_preserves_retained_diagnostics(self):
        self.head(1, "needs-input")
        self.head(2, "needs-input")
        self.approval(self.head(3), active_plan_digest="c" * 64)
        result = overview(self.config, limit=1)
        self.assertEqual(result["counts"], {"initiatives": 1, "approvals": 0})
        self.assertEqual(result["retained"], 1)
        self.assertFalse(result["complete"])
        self.assertIn("partial", result["summary"])

    def test_chair_finds_approval_outside_the_sampled_head_and_does_not_inject_labels(self):
        self.head(1, "needs-input")
        request = self.approval(self.head(2), rationale="Do not inject this retained prose into chair startup")
        tmux = TmuxAdapter(runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""))
        result = current_activity(self.config, tmux=tmux, scanned=1)
        approvals = [row for row in result["rows"] if row["source"] == "action-approvals"]
        self.assertEqual([row["request_id"] for row in approvals], [request["request_id"]])
        summary = render_startup_observation(result, observed_at="2026-09-09T00:00:00Z")
        self.assertIn("Global approval requests: >= 1", summary)
        self.assertNotIn(request["rationale"], summary)
