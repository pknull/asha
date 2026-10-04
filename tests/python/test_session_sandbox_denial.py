"""#112: a sandboxed caller's ownership failure names its cause and how to recover.

The proofs run for real under the real denial shapes, never skipped: bubblewrap
gives the caller its own PID namespace (as #72's repro does), and a seccomp
filter fails connect(2) with EPERM, which is how Codex's workspace-write sandbox
refuses the tmux socket ("error connecting to ... (Operation not permitted)").
"""
import json
import os
import platform
import shlex
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from lib.control.store import StoreError
from tests.python import test_control_rooms as rooms_fixture
from tests.python import test_control_session_hub as hub_fixture

ROOT = Path(__file__).resolve().parents[2]
HINT = 'inside the Codex sandbox run this asha command on its own with literal arguments'

# The caller a sandboxed agent runs: one hub call, its StoreError printed as JSON.
CALLER = '''
import json, os, sys, time
sys.path.insert(0, {root!r})
from lib.control.config import load_config
from lib.control.session_hub import Hub
from lib.control.store import StoreError
from lib.control.tmux import TmuxAdapter
hub = Hub(load_config(os.environ), env=dict(os.environ), tmux=TmuxAdapter(socket={socket!r}))
try:
    {call}
except StoreError as exc:
    print(json.dumps(dict(error=str(exc))))
else:
    print(json.dumps(dict(error=None)))
'''

# The pane's process: runs each numbered request (a bash command) and keeps the pane alive.
PANE = '''#!{python}
import json, os, subprocess, time
from pathlib import Path
exchange = Path({exchange!r})
deadline = time.monotonic() + 60
number = 1
while time.monotonic() < deadline and not (exchange / 'stop').exists():
    request = exchange / f'request.{{number}}'
    if request.exists():
        run = subprocess.run(['bash', '-c', request.read_text()], capture_output=True, text=True)
        partial = exchange / f'.result.{{number}}'
        partial.write_text(json.dumps(dict(returncode=run.returncode, stdout=run.stdout, stderr=run.stderr)))
        partial.rename(exchange / f'result.{{number}}')
        number += 1
    time.sleep(.02)
'''


def deny_connect_filter():
    """A seccomp program failing connect(2) with EPERM; None on an unmapped architecture."""
    syscalls = {'x86_64': (0xC000003E, 42), 'aarch64': (0xC00000B7, 203)}
    if platform.machine() not in syscalls:
        return None
    arch, connect = syscalls[platform.machine()]
    def op(code, jt, jf, k):
        return struct.pack('=HBBI', code, jt, jf, k)
    return b''.join((
        op(0x20, 0, 0, 4),               # A = seccomp_data.arch
        op(0x15, 0, 3, arch),            # another ABI: allow
        op(0x20, 0, 0, 0),               # A = seccomp_data.nr
        op(0x15, 0, 1, connect),
        op(0x06, 0, 0, 0x00050000 | 1),  # SECCOMP_RET_ERRNO | EPERM
        op(0x06, 0, 0, 0x7FFF0000),      # SECCOMP_RET_ALLOW
    ))


@unittest.skipUnless(shutil.which('tmux') and shutil.which('bwrap'), 'tmux and bubblewrap are required')
class SandboxedCallerTests(unittest.TestCase):
    def setUp(self):
        hub_fixture.SessionHubTests.setUp(self)
        from lib.control.socket_reaper import TmuxSocketReaper
        from lib.control.tmux import TmuxAdapter
        self.socket = 'asha-hub-test-' + uuid.uuid4().hex[:12]
        self.enterContext(TmuxSocketReaper(self.socket))
        adapter = TmuxAdapter(socket=self.socket, config_file=Path('/dev/null'))
        if adapter._run_status(['list-commands', 'new-session'])[0]:
            self.skipTest('isolated tmux sockets are unavailable in this execution sandbox')
        probe = subprocess.run(['bwrap', '--die-with-parent', '--ro-bind', '/', '/', '--unshare-pid',
                                '--proc', '/proc', '--dev-bind', '/dev', '/dev', '--', 'true'],
                               capture_output=True, text=True, check=False)
        if probe.returncode:
            self.skipTest('bubblewrap cannot create namespaces here: ' + probe.stderr.strip())
        self.hub.tmux = adapter
        self.filter = self.root / 'deny-connect.bpf'
        program = deny_connect_filter()
        if program is not None:
            self.filter.write_bytes(program)

    def launch(self):
        """A real Room pane whose process runs requests on our behalf."""
        self.exchange = self.root / 'exchange'
        self.exchange.mkdir()
        self.requests = 0
        probe_root = self.root / 'pane-probe'
        (probe_root / 'bin').mkdir(parents=True)
        pane = probe_root / 'bin' / 'asha'
        pane.write_text(PANE.format(python=sys.executable, exchange=str(self.exchange)))
        pane.chmod(0o700)
        def native_launch(**kwargs):
            kwargs.update(asha_root=probe_root, executable_finder=lambda _: sys.executable)
            from lib.control.rooms import open_room
            return open_room(**kwargs)
        with mock.patch('lib.control.session_hub.open_room', side_effect=native_launch):
            row = self.hub.launch(project=str(self.project), prompt='Probe the sandbox',
                                  name='Sandbox probe', harness='codex')
        self.addCleanup(lambda: self.hub.close(row['session_id'], force=True))
        self.addCleanup(lambda: (self.exchange / 'stop').touch())
        # Live Control keeps its WAL index open (supervisor, dashboards), which is
        # what lets a read-only sandbox read the database; keep one reader open too.
        from lib.control.database import DATABASE_NAME
        reader = sqlite3.connect(self.config.tasks_dir.parent / DATABASE_NAME)
        reader.execute('SELECT count(*) FROM hub_sessions').fetchone()
        self.addCleanup(reader.close)
        return row

    def caller(self, call):
        return [sys.executable, '-c', CALLER.format(root=str(ROOT), socket=self.socket, call=call)]

    def sandboxed(self, argv, *, deny_connect, writable=()):
        """bwrap with its own PID namespace, read-only host, optionally no connect(2)."""
        command = ['bwrap', '--die-with-parent', '--ro-bind', '/', '/']
        for path in writable:
            command += ['--bind', str(path), str(path)]
        command += ['--unshare-pid', '--proc', '/proc', '--dev-bind', '/dev', '/dev']
        shell = shlex.join(command + (['--seccomp', '9'] if deny_connect else []) + ['--', *argv])
        return 'exec ' + shell + (' 9<' + shlex.quote(str(self.filter)) if deny_connect else '')

    def in_pane(self, shell):
        self.requests += 1
        partial = self.exchange / f'.request.{self.requests}'
        partial.write_text(shell)
        partial.rename(self.exchange / f'request.{self.requests}')
        result = self.exchange / f'result.{self.requests}'
        deadline = time.monotonic() + 20
        while not result.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(result.exists(), 'the pane did not answer request %d' % self.requests)
        run = json.loads(result.read_text())
        self.assertEqual(run['returncode'], 0, run)
        return json.loads(run['stdout'])['error']

    def outside(self, shell, row):
        env = {**os.environ, **self.env, 'ASHA_HUB_SESSION_ID': row['session_id'],
               'ASHA_HUB_GENERATION': str(row['generation'])}
        run = subprocess.run(['bash', '-c', shell], env=env, capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0, run)
        return json.loads(run.stdout)['error']

    def assert_tmux_denial(self, error):
        self.assertIsNotNone(error)
        self.assertTrue(error.startswith('session ownership unavailable: '), error)
        self.assertIn('error connecting to ', error)
        self.assertIn('(Operation not permitted)', error)
        self.assertTrue(error.endswith('; ' + HINT), error)

    def test_report_refused_the_tmux_socket_names_it_and_hints_a_plain_rerun(self):
        if not self.filter.exists():
            self.skipTest('no connect(2) seccomp mapping for ' + platform.machine())
        row = self.launch()
        report = self.caller("hub.report(state='needs-input', body='probe')")
        self.assert_tmux_denial(self.in_pane(self.sandboxed(report, deny_connect=True)))
        self.assertNotEqual(self.hub.show(row['session_id'])['activity'], 'needs-input')

    def test_report_from_another_pid_namespace_hints_a_plain_rerun(self):
        row = self.launch()
        report = self.caller("hub.report(state='needs-input', body='probe')")
        self.assertEqual(self.in_pane(self.sandboxed(report, deny_connect=False)),
                         'reporter is not part of this session; ' + HINT)
        self.assertNotEqual(self.hub.show(row['session_id'])['activity'], 'needs-input')
        # The same caller, unsandboxed in the same pane, is the session.
        self.assertIsNone(self.in_pane(shlex.join(report)))
        self.assertEqual(self.hub.show(row['session_id'])['activity'], 'needs-input')

    def test_caller_outside_the_session_gets_no_hint(self):
        row = self.launch()
        report = self.caller("hub.report(state='needs-input', body='spoofed')")
        self.assertEqual(self.outside(shlex.join(report), row), 'reporter is not part of this session')

    def test_lost_stop_refused_the_tmux_socket_names_it_and_hints_a_plain_rerun(self):
        if not self.filter.exists():
            self.skipTest('no connect(2) seccomp mapping for ' + platform.machine())
        row = self.launch()
        sid = row['session_id']
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('prompt-submitted', native_id='thread-own', cwd=str(self.project.resolve()))
        lost = self.caller("hub.deliver_lost_stop(native_id='thread-own', emitted_at=time.time())")
        # The loss call runs outside the pane; Control state stays writable for its lock.
        error = self.outside(self.sandboxed(lost, deny_connect=True, writable=[self.asha_home]), row)
        self.assert_tmux_denial(error)


class OwnershipDetailTests(unittest.TestCase):
    """Without a sandbox cause the detail is carried and no hint is offered."""

    def setUp(self):
        hub_fixture.SessionHubTests.setUp(self)

    def test_absent_pane_carries_its_detail_without_a_hint(self):
        row = self.hub.launch(project=str(self.project), prompt='Probe', name='Gone', harness='claude')
        sid = row['session_id']
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('prompt-submitted', native_id='thread-own', cwd=str(self.project.resolve()))
        self.tmux.sessions.clear()
        self.hub.env.update(ASHA_HUB_SESSION_ID=sid, ASHA_HUB_GENERATION=str(row['generation']))
        for call in (lambda: self.hub.report(state='needs-input', body='probe'),
                     lambda: self.hub.deliver_lost_stop(native_id='thread-own', emitted_at=time.time())):
            with self.subTest(call=call), self.assertRaises(StoreError) as caught:
                call()
            message = str(caught.exception)
            self.assertTrue(message.startswith('session ownership unavailable: '), message)
            self.assertGreater(len(message), len('session ownership unavailable: '))
            self.assertNotIn(HINT, message)


if __name__ == '__main__':
    unittest.main()
