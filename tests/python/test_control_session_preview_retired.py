"""The dashboard's live session preview is deleted (Keeper ruling N9, 2026-10-07).

The side panel and the narrow full-width detail show the selected session's
facts only: no pane capture, no tmux hook read, no structured event read, at
any setting. A config that still sets ``control.session_preview`` loads.
"""
from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest import mock

from lib.control import session_keys, session_layout, session_tui
from lib.control.config import load_config
from lib.control.tmux import TmuxAdapter


class PreviewModulesTests(unittest.TestCase):
    def test_the_preview_modules_are_gone(self):
        for name in ('lib.control.session_preview', 'lib.control.pane_peek'):
            with self.subTest(module=name), self.assertRaises(ModuleNotFoundError):
                importlib.import_module(name)

    def test_the_sanitizer_survives_in_text(self):
        from lib.control.text import sanitize
        self.assertEqual(sanitize('a\x1b]52;c;ZXZpbA==\x07b'), 'ab')


class RetiredSettingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.env = {'HOME': str(root / 'home'), 'ASHA_CONFIG': str(root / 'config.json'),
                    'ASHA_HOME': str(root / 'asha'), 'XDG_RUNTIME_DIR': str(root / 'runtime')}
        for key in ('HOME', 'ASHA_HOME', 'XDG_RUNTIME_DIR'):
            Path(self.env[key]).mkdir(mode=0o700)

    def test_a_config_that_still_sets_it_loads_and_ignores_it(self):
        for value in (True, False, 'yes'):
            with self.subTest(value=value):
                path = Path(self.env['ASHA_CONFIG'])
                path.write_text(json.dumps({'control': {'session_preview': value}}))
                path.chmod(0o600)
                config = load_config(self.env)
                self.assertFalse(hasattr(config, 'session_preview'))


class TmuxSpy:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv[1:])
        return CompletedProcess(argv, 0, b'', b'')


class DashboardReadsNoScreenTests(unittest.TestCase):
    """Wide panel, hidden panel and narrow peek issue no tmux call, whatever the config says."""

    KEYS = {'wide': ([258, 259, ord(' '), ord(' '), 258, 339, 338], (30, 140)),
            'narrow peek': ([ord(' '), 258, 339, 27, ord(' ')], (24, 100))}

    def test_no_capture_and_no_preview_in_any_paint(self):
        from tests.python.test_control_session_dashboard import row as hub_row
        from tests.python.test_control_session_dashboard_keys import run
        room = '11111111-2222-4333-8444-555555555555'
        for name, (keys, size) in self.KEYS.items():
            for config in (object(), SimpleNamespace(session_preview=True)):
                with self.subTest(case=name, config=config):
                    spy = TmuxSpy()
                    hub = mock.MagicMock()
                    hub.tmux = TmuxAdapter(runner=spy)
                    rows = [hub_row('a', room_id=room), hub_row('b', room_id=room),
                            hub_row('c', transport='structured', room_id=None)]
                    with mock.patch('lib.control.rooms.RoomStore') as rooms, \
                            mock.patch('lib.control.session_store.SessionStore') as store:
                        _, painted = run(keys, rows, size=size, hub=hub, config=config)
                    self.assertEqual(spy.calls, [])
                    rooms.assert_not_called()
                    store.assert_not_called()
                    self.assertTrue(painted)
                    for snap, kw in painted:
                        self.assertFalse({'preview', 'preview_back', 'session_preview'} & set(snap))
                        text = '\n'.join(session_layout.plain(session_layout.render(
                            snap, width=size[1], height=size[0], peek=kw.get('peek', False),
                            preview=kw.get('preview', True))))
                        self.assertNotIn('read-only', text)
                        self.assertNotIn('Capturing', text)

    def test_the_dashboard_keeps_no_scrollback_state(self):
        for name in ('previews', 'back', 'back_key', 'scroll_preview', 'shown_preview', 'previewed'):
            self.assertFalse(hasattr(session_tui.Dashboard, name), name)
        self.assertFalse(hasattr(session_layout, 'compose_panel'))
        self.assertFalse(hasattr(session_layout, 'preview_lines'))


class KeySheetTests(unittest.TestCase):
    def test_space_shows_or_hides_the_detail_only(self):
        sheet = '\n'.join(session_keys.key_sheet())
        self.assertIn('show or hide the selected session detail', sheet)
        for gone in ('live screen', 'PgUp', 'PgDn'):
            self.assertNotIn(gone, sheet)
        painted = '\n'.join(session_tui.lines({'rows': [], 'session_preview': True}, width=200, keys=True))
        self.assertNotIn('live screen', painted)


if __name__ == '__main__':
    unittest.main()
