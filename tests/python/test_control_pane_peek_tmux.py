"""#102 phase 3 on a disposable tmux server: capturing a Room pane creates no client.

Uses a private socket under TMUX_TMPDIR (the #98 pattern), never the live server.
The fixture pane carries the Room ownership options and the #96 attach-generation
hooks, so a capture that attached, even read-only, would move the counters.
"""
import os
import shlex
import shutil
import tempfile
import subprocess
import time
import unittest

from lib.control import pane_peek
from lib.control.rooms import PANE_PROJECT_OPTION, PANE_ROOM_OPTION, SESSION_ROOM_OPTION, _project_marker
from lib.control.socket_reaper import TmuxSocketReaper
from lib.control.tmux import ATTACH_GENERATION_OPTION, TmuxAdapter, _attach_hook_command

ROOM = '11111111-2222-4333-8444-555555555555'
PROJECT = 'project-peek'
CAPTURES = 25


@unittest.skipUnless(shutil.which('tmux'), 'tmux required')
class RealPanePeekTests(unittest.TestCase):
    def setUp(self):
        self.socket = f'asha-peek-{os.getpid()}-{time.time_ns()}'
        self.base = dict(os.environ)
        self.enterContext(TmuxSocketReaper(self.socket, environ=self.base))
        directory = os.path.join(self.base.get('TMUX_TMPDIR') or '/tmp', f'tmux-{os.getuid()}')
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, self.socket)
        if self.tmux('list-commands', 'new-session', check=False).returncode != 0:
            self.skipTest('isolated tmux sockets are unavailable in this execution sandbox')
        # The pane prints a marker, a title set and an OSC 52 write, then waits.
        script = ("printf 'peek-marker-1\\n\\033]2;evil-title\\007\\033]52;c;ZXZpbA==\\007after\\n'; "
                  "exec sleep 120")
        self.tmux('new-session', '-d', '-x', '100', '-y', '20', '-s', 'room', '/bin/sh', '-c', script)
        self.pane, self.session_id = self.tmux(
            'display-message', '-p', '-t', 'room', '#{pane_id}\t#{session_id}').stdout.strip().split('\t')
        self.tmux('set-option', '-t', self.session_id, SESSION_ROOM_OPTION, ROOM)
        self.tmux('set-option', '-p', '-t', self.pane, PANE_ROOM_OPTION, ROOM)
        self.tmux('set-option', '-p', '-t', self.pane, PANE_PROJECT_OPTION, _project_marker(PROJECT))
        self.tmux('set-option', '-p', '-t', self.pane, ATTACH_GENERATION_OPTION, '0')
        for hook in ('client-attached', 'client-session-changed'):
            self.tmux('set-hook', '-t', 'room', hook, _attach_hook_command(self.pane))
        self.record = {'room_id': ROOM, 'project_id': PROJECT, 'name': 'room', 'lifecycle': 'open',
                       'tmux': {'session': 'room', 'window': '0', 'pane_id': self.pane,
                                'session_id': self.session_id}}
        self.adapter = TmuxAdapter(socket=self.socket)
        deadline = time.monotonic() + 5
        while 'after' not in self.tmux('capture-pane', '-p', '-t', self.pane).stdout:
            if time.monotonic() > deadline:
                self.fail('fixture pane never printed')
            time.sleep(0.05)

    def tmux(self, *argv, check=True, **kwargs):
        return subprocess.run(['tmux', '-S', self.path, '-f', '/dev/null', *argv], capture_output=True,
                              text=True, check=check, env=self.base, **kwargs)

    def counters(self):
        return self.tmux('display-message', '-p', '-t', self.pane,
                         f'#{{session_attached}}\t#{{{ATTACH_GENERATION_OPTION}}}').stdout.strip()

    def test_captures_leave_attachment_and_attach_generation_unchanged(self):
        before = self.counters()
        self.assertEqual(before, '0\t0')
        for _ in range(CAPTURES):
            lines = pane_peek.peek_room(self.record, self.adapter, 10)
        self.assertEqual(self.counters(), before)
        self.assertIn('peek-marker-1', lines)
        self.assertFalse([line for line in lines if '\x1b' in line or 'evil-title' in line or 'ZXZpbA' in line])
        self.assertEqual(self.tmux('list-clients').stdout, '')

    def test_the_counter_discriminates_a_real_client(self):
        # Prove the fence this test reads would have moved: a control-mode client bumps it.
        self.tmux('-C', 'attach-session', '-t', 'room', input='', timeout=10)
        self.assertGreater(int(self.counters().split('\t')[1]), 0)

    def test_a_pane_that_lost_ownership_is_not_captured(self):
        self.tmux('set-option', '-p', '-t', self.pane, PANE_ROOM_OPTION, 'someone-else')
        with self.assertRaises(pane_peek.PeekRefused):
            pane_peek.peek_room(self.record, self.adapter, 10)


def private_server(test, label):
    """A disposable tmux server on its own -S path under TMUX_TMPDIR; reaped after the test."""
    socket = f'asha-peek-{label}-{os.getpid()}-{time.time_ns()}'
    environ = dict(os.environ)
    test.enterContext(TmuxSocketReaper(socket, environ=environ))
    directory = os.path.join(environ.get('TMUX_TMPDIR') or '/tmp', f'tmux-{os.getuid()}')
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, socket)

    def tmux(*argv, check=True, **kwargs):
        return subprocess.run(['tmux', '-S', path, '-f', '/dev/null', *argv], capture_output=True, text=True,
                              check=check, env=environ, **kwargs)
    return socket, path, tmux


@unittest.skipUnless(shutil.which('tmux'), 'tmux required')
class CaptureHookInjectionTests(unittest.TestCase):
    """QA17 Q17-F1 native reproductions: a configured command hook must disable the preview.

    Each sentinel pane is a raw, no-echo ``cat`` whose stdin lands in a file, so any
    input a hook sends there is recorded. Before the fix one preview wrote into the
    Room pane itself, into an unowned pane, and into a pane on a second server.
    """

    def setUp(self):
        self.socket, self.path, self.tmux = private_server(self, 'hook-a')
        if self.tmux('list-commands', 'new-session', check=False).returncode != 0:
            self.skipTest('isolated tmux sockets are unavailable in this execution sandbox')
        self.scratch = self.enterContext(tempfile.TemporaryDirectory())
        self.pane = self.sentinel(self.tmux, 'room', 'room.in')
        self.session_id = self.tmux('display-message', '-p', '-t', self.pane, '#{session_id}').stdout.strip()
        self.tmux('set-option', '-t', self.session_id, SESSION_ROOM_OPTION, ROOM)
        self.tmux('set-option', '-p', '-t', self.pane, PANE_ROOM_OPTION, ROOM)
        self.tmux('set-option', '-p', '-t', self.pane, PANE_PROJECT_OPTION, _project_marker(PROJECT))
        self.record = {'room_id': ROOM, 'project_id': PROJECT, 'name': 'room', 'lifecycle': 'open',
                       'tmux': {'session': 'room', 'window': '0', 'pane_id': self.pane,
                                'session_id': self.session_id}}
        self.adapter = TmuxAdapter(socket=self.socket)

    def sentinel(self, tmux, session, name):
        path = os.path.join(self.scratch, name)
        open(path, 'wb').close()
        tmux('new-session', '-d', '-x', '80', '-y', '10', '-s', session, '/bin/sh', '-c',
             f'stty raw -echo; echo ready > {shlex.quote(path)}.ready; exec cat > {shlex.quote(path)}')
        deadline = time.monotonic() + 5
        while not os.path.exists(path + '.ready'):
            if time.monotonic() > deadline:
                self.fail('sentinel pane never started')
            time.sleep(0.02)
        return tmux('display-message', '-p', '-t', session, '#{pane_id}').stdout.strip()

    def grown(self, path, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.getsize(path):
                return True
            time.sleep(0.05)
        return bool(os.path.getsize(path))

    def assert_refused_and_silent(self, sentinel, hook_name='after-capture-pane'):
        try:
            pane_peek.peek_room(self.record, self.adapter, 20)
            refusal = None
        except pane_peek.PeekDisabled as exc:
            refusal = str(exc)
        # The stdin sentinel first: a missed refusal must show up as the write it allowed.
        self.assertFalse(self.grown(sentinel, 0.5), 'the preview let a hook write to a pane')
        self.assertEqual(refusal, f'Preview disabled: tmux {hook_name} hook configured')

    def test_same_pane_hook_at_every_scope(self):
        sentinel = os.path.join(self.scratch, 'room.in')
        for flags in (['-g'], ['-gw'], ['-t', 'room'], ['-w', '-t', self.pane], ['-p', '-t', self.pane]):
            with self.subTest(flags=flags):
                self.tmux('set-hook', *flags, 'after-capture-pane',
                          f'send-keys -t {self.pane} -l QA17-PREVIEW-INJECTED')
                self.assert_refused_and_silent(sentinel)
                self.tmux('set-hook', '-u', *flags, 'after-capture-pane')
        # With every hook gone the preview works and still writes nothing.
        self.assertEqual(pane_peek.peek_room(self.record, self.adapter, 20), [])
        self.assertFalse(self.grown(sentinel, 0.3))

    def test_hook_into_an_unowned_pane(self):
        other = self.sentinel(self.tmux, 'unowned', 'unowned.in')
        sentinel = os.path.join(self.scratch, 'unowned.in')
        self.tmux('set-hook', '-g', 'after-capture-pane', f'send-keys -t {other} -l INTO-UNOWNED')
        self.assert_refused_and_silent(sentinel)
        # Control: the configured hook is live, so an ordinary capture does inject.
        self.tmux('capture-pane', '-p', '-t', self.pane)
        self.assertTrue(self.grown(sentinel, 5), 'fixture hook never fired; the refusal proves nothing')

    def test_hook_into_a_pane_on_another_server(self):
        _, path_b, tmux_b = private_server(self, 'hook-b')
        pane_b = self.sentinel(tmux_b, 'remote', 'remote.in')
        sentinel = os.path.join(self.scratch, 'remote.in')
        command = shlex.join([shutil.which('tmux'), '-S', path_b, 'send-keys', '-t', pane_b, '-l',
                              'CROSS-SERVER-INPUT'])
        self.tmux('set-hook', '-g', 'after-capture-pane', f'run-shell {shlex.quote(command)}')
        self.assert_refused_and_silent(sentinel)
        self.tmux('capture-pane', '-p', '-t', self.pane)
        self.assertTrue(self.grown(sentinel, 5), 'fixture hook never fired; the refusal proves nothing')

    def test_hooks_on_the_ownership_reads_also_refuse(self):
        # tmux 3.4 runs after-display-message and after-show-options too, and the
        # ownership check issues both before the capture.
        sentinel = os.path.join(self.scratch, 'room.in')
        for name in ('after-display-message', 'after-show-options'):
            with self.subTest(name=name):
                self.tmux('set-hook', '-g', name, f'send-keys -t {self.pane} -l OWNERSHIP-READ')
                self.assert_refused_and_silent(sentinel, name)
                self.tmux('set-hook', '-gu', name)

    def test_the_unset_hook_output_parses_as_unset(self):
        output = self.tmux('show-hooks', '-g', 'after-capture-pane').stdout
        self.assertEqual(output, 'after-capture-pane\n')
        self.assertEqual(pane_peek.configured_hooks(output.encode()), set())

    def test_the_hook_query_itself_cannot_carry_a_hook(self):
        refused = self.tmux('set-hook', '-g', 'after-show-hooks', 'set -g @x y', check=False)
        self.assertNotEqual(refused.returncode, 0)


if __name__ == '__main__':
    unittest.main()
