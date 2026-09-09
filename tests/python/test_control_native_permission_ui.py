import unittest
from unittest import mock

from lib.control import tui
from tests.python.test_control_tui_focus import FakeCurses, FakeScreen


class NativePermissionUiTests(unittest.TestCase):
    request = {"request_id": "request-1", "digest": "sealed-input",
               "cwd": "/tmp/project", "state": "pending", "response_state": "pending",
               "payload": {"request": {"tool_name": "Write", "input": {"content": "long text " * 300}}}}

    def test_wide_keys_decide_and_scroll_keeps_every_row_inside_screen(self):
        screen = FakeScreen([338, 360, 339, 262, "a"], height=8, width=30)
        frames = []
        with mock.patch.object(tui, "_draw_modal_frame", side_effect=lambda _s, _c, frame: frames.append(frame)):
            self.assertEqual(tui._native_permission_prompt(screen, FakeCurses(), self.request), "allow")
        for frame in frames:
            self.assertLessEqual(len(frame.rows), 8)
            self.assertTrue(all(tui._cell_width(row) <= 29 for row in frame.rows))
        self.assertNotEqual(frames[0].rows, frames[1].rows)
        self.assertEqual(frames[0].rows, frames[-1].rows)

    def test_idle_poll_does_not_redraw_or_dim_the_invocation(self):
        screen = FakeScreen([-1, -1, "d"], height=15, width=80)
        with mock.patch.object(tui, "_draw_modal_frame") as draw:
            self.assertEqual(tui._native_permission_prompt(screen, FakeCurses(), self.request), "deny")
        self.assertEqual(draw.call_count, 1)
        self.assertIn("content", draw.call_args.args[2].row_roles)
        self.assertEqual(tui._modal_row_attribute(FakeCurses(), "content"), 0)
        self.assertIn("/tmp/project", "\n".join(draw.call_args.args[2].rows))

    def test_small_screen_and_resize_never_decide(self):
        screen = FakeScreen(height=4, width=15)
        keys = iter(["a", FakeCurses.KEY_RESIZE, "d"])
        def read():
            key = next(keys)
            if key == FakeCurses.KEY_RESIZE:
                screen.height, screen.width = 12, 80
            return key
        screen.get_wch = read
        self.assertEqual(tui._native_permission_prompt(screen, FakeCurses(), self.request), "deny")

    def test_escape_cancels_without_a_decision(self):
        self.assertIsNone(tui._native_permission_prompt(FakeScreen(["\x1b"]), FakeCurses(), self.request))
