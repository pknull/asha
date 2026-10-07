"""Retirement steps L-a2 and L-b: the dashboard's modals stand on their own.

The dashboard builds its modal model without the retired legacy TUI or the
task substrate, and a prompt repaints the dashboard's own live view beneath it.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

from unittest import mock

from lib.control import session_modals, session_tui
from tests.python.control_curses_fakes import FakeCurses, FakeScreen

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


class BackdropTests(unittest.TestCase):
    def dashboard(self):
        class Quiet:
            def timeout(self, _ms):
                pass

            def getmaxyx(self):
                return (24, 100)

        with mock.patch.object(session_tui, 'Hub'), \
                mock.patch.object(session_tui, 'ThreadPoolExecutor', return_value=mock.MagicMock()), \
                mock.patch.object(session_tui, '_title_writer', return_value=mock.MagicMock()), \
                mock.patch.object(session_tui.session_modals, 'init_colours', return_value=False):
            return session_tui.Dashboard(Quiet(), object(), {})

    def test_a_dashboard_prompt_repaints_the_dashboards_own_live_view(self):
        dash = self.dashboard()
        with mock.patch.object(dash, 'paint') as paint:
            dash.model.paint_underlay(Screen(), Curses)
        paint.assert_called_once_with()

    def test_the_backdrop_repaints_on_entry_and_resize_but_not_on_idle_timeouts(self):
        painted = []
        model = session_modals.SessionModel(backdrop=lambda: painted.append('dashboard'))
        screen = FakeScreen([-1, -1, FakeCurses.KEY_RESIZE, -1, '\x1b'], height=12, width=60)
        self.assertIsNone(session_modals._prompt_line(screen, FakeCurses(), model, 'Message: ', title='Send'))
        self.assertEqual(painted, ['dashboard', 'dashboard'])

    def test_a_model_without_a_backdrop_clears_the_screen_behind_a_prompt(self):
        screen = Screen()
        session_modals.SessionModel().paint_underlay(screen, Curses)
        self.assertEqual(screen.writes, ['erase'])


if __name__ == '__main__':
    unittest.main()
