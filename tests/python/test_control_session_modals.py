"""Retirement step L-a2: the dashboard's modal helpers live outside the legacy TUI.

The dashboard must build its modal model without loading ``tui`` or the task
substrate it imports, while the legacy TUI keeps working on the same helpers
until the engine retires.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from lib.control import session_modals, tui
from lib.control.prerequisites import ControlTermination

ROOT = Path(__file__).resolve().parents[2]
# What ``tui`` loaded into a dashboard that only wanted its modal helpers.
LEGACY = ('lib.control.tui', 'lib.control.events', 'lib.control.jj', 'lib.control.launch',
          'lib.control.prepare', 'lib.control.prerequisites', 'lib.control.prune',
          'lib.control.reconcile', 'lib.control.sources', 'lib.control.transaction', 'lib.control.view')

DASHBOARD = '''
import json, sys, types
from unittest.mock import MagicMock, patch
from lib.control import session_tui
before = set(sys.modules)
class Screen:
    def timeout(self, ms): pass
    def getmaxyx(self): return (24, 100)
with patch.object(session_tui, 'Hub'), \\
     patch.object(session_tui, 'ThreadPoolExecutor', return_value=MagicMock()), \\
     patch.object(session_tui, '_title_writer', return_value=MagicMock()), \\
     patch.object(session_tui, 'curses', types.SimpleNamespace()):
    dash = session_tui.Dashboard(Screen(), object(), {})
print(json.dumps({'model': type(dash.model).__module__ + '.' + type(dash.model).__qualname__,
                  'added': sorted(set(sys.modules) - before)}))
'''


def fresh(code):
    done = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                          check=False)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


class Screen:
    def __init__(self, height=14, width=80):
        self.size = height, width
        self.writes = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.writes.append('erase')

    def addnstr(self, *args):
        self.writes.append(args)

    def refresh(self):
        self.writes.append('refresh')


class Curses:
    error = RuntimeError
    A_BOLD = 1 << 21


class IsolationTests(unittest.TestCase):
    def test_the_modal_module_loads_no_legacy_module(self):
        loaded = fresh('import json, sys; import lib.control.session_modals; print(json.dumps(sorted(sys.modules)))')
        self.assertEqual([m for m in loaded if m in LEGACY or m.startswith('lib.control.orchestration')], [])

    def test_the_dashboard_builds_its_modal_model_without_the_legacy_tui(self):
        built = fresh(DASHBOARD)
        self.assertEqual(built['model'], 'lib.control.session_modals.SessionModel')
        self.assertEqual([m for m in built['added'] if m in LEGACY], [])


class LegacyReuseTests(unittest.TestCase):
    def test_the_legacy_tui_uses_the_same_helpers(self):
        for name in ('_prompt_line', '_project_launch_form', '_managed_session_view', '_popup_room_command',
                     '_decide_managed_permission', '_select_managed_request', 'init_colours', '_attribute',
                     '_repaint_after_suspend', '_cell_width', '_cell_lines', '_prefix_cells', '_safe_text',
                     'ModalCandidate', 'ModalFrame', 'modal_frame', '_draw_modal_frame', '_read_modal_key'):
            with self.subTest(name=name):
                self.assertIs(getattr(tui, name), getattr(session_modals, name))

    def test_the_legacy_shutdown_still_crosses_repository_transactions(self):
        self.assertTrue(issubclass(tui._TuiShutdown, ControlTermination))
        self.assertTrue(issubclass(tui._TuiShutdown, session_modals._TuiShutdown))
        shutdown = tui._TuiShutdown(15)
        self.assertEqual((shutdown.signum, shutdown.detail, str(shutdown)), (15, None, '15'))


class UnderlayTests(unittest.TestCase):
    def test_a_dashboard_prompt_repaints_what_the_tui_model_did(self):
        model = session_modals.SessionModel()
        model.coloured, model.message, model.managed_summary = True, 'Started Fresh', '1 question'
        legacy = tui.TuiModel([])
        legacy.coloured, legacy.message, legacy.managed_summary = True, 'Started Fresh', '1 question'
        for height, width in ((14, 80), (6, 40), (30, 140)):
            with self.subTest(size=(height, width)):
                painted, expected = Screen(height, width), Screen(height, width)
                model.dirty = legacy.dirty = True
                model.paint_underlay(painted, Curses)
                tui._paint(expected, Curses, legacy)
                self.assertEqual(painted.writes, expected.writes)


if __name__ == '__main__':
    unittest.main()
