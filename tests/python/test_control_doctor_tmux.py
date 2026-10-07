"""The doctor's tmux probe runs on a private ``-S`` socket in a temporary directory (B3).

``list-commands`` starts a server, which exits at once with no sessions but
leaves its socket file behind. The probe's socket lives in a directory that
is removed with it, so the doctor needs no socket reaper.
"""
from __future__ import annotations

import importlib
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lib.control import doctor
from lib.control.tmux import TmuxError


class DoctorTmuxProbeTests(unittest.TestCase):
    def test_the_socket_reaper_left_control(self):
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module('lib.control.socket_reaper')

    def test_capability_probe_uses_a_private_socket_removed_on_every_exit(self):
        cases = (
            ((1, b'', b'denied'), 'unavailable'),
            ((0, b'list-commands only\n', b''), 'unavailable'),
            ((0, b'display-popup (popup)\n', b''), 'match'),
            (TmuxError('probe failed'), 'unavailable'),
        )
        for response, expected in cases:
            with self.subTest(response=response):
                seen = []

                def run(argv, **_kwargs):
                    socket = Path(argv[argv.index('-S') + 1])
                    seen.append((argv, socket, stat.S_IMODE(socket.parent.stat().st_mode)))
                    if isinstance(response, BaseException):
                        raise response
                    return response

                with mock.patch('lib.control.doctor.shutil.which', return_value='/usr/bin/tmux'), \
                        mock.patch.object(doctor.TmuxAdapter, '_run_status', autospec=True,
                                          return_value=(0, b'tmux 3.4\n', b'')), \
                        mock.patch('lib.control.doctor.capture_bytes', side_effect=run):
                    result = doctor._tmux_probe(None)

                self.assertEqual(result.outcome, expected)
                [(argv, socket, mode)] = seen
                self.assertEqual(argv, ['/usr/bin/tmux', '-S', str(socket), '-f', '/dev/null',
                                        'list-commands', 'display-popup'])
                self.assertEqual(mode, 0o700)
                self.assertFalse(socket.parent.exists())

    @unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
    def test_a_real_probe_leaves_nothing_behind(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        tmux_tmpdir = root / 'tmux'
        tmux_tmpdir.mkdir(mode=0o700)
        with mock.patch.object(tempfile, 'tempdir', str(root)), \
                mock.patch.dict(os.environ, {'TMUX_TMPDIR': str(tmux_tmpdir)}):
            result = doctor._tmux_probe(None)
        self.assertEqual(result.outcome, 'match', result.detail)
        self.assertEqual([path.name for path in root.iterdir()], ['tmux'])
        self.assertEqual(list(tmux_tmpdir.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
