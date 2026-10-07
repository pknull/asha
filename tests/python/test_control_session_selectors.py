"""Session verbs accept short ID prefixes; attach attaches in a terminal (#113)."""
import contextlib
import hashlib
import io
import json
import os
import pty
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lib.control.config import load_config
from tests.python.socket_reaper import TmuxSocketReaper
from lib.control.tmux import TmuxAdapter
from tests.python import test_control_rooms as rooms_fixture

FIRST = 'e154c8af-45af-4a85-a35e-2ab21118979c'
TWIN = 'e154c8af-0000-4000-8000-000000000001'


class SessionSelectorTests(unittest.TestCase):
    def setUp(self):
        rooms_fixture.RoomTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.config = load_config(self.env)
        from lib.control.session_hub import Hub
        self.hub = Hub(self.config, env=self.env, tmux=self.tmux)
        self.enterContext(mock.patch('lib.control.session_hub.TmuxAdapter', return_value=self.tmux))
        self.enterContext(mock.patch(
            'lib.control.supervisor_service.start_supervisor',
            return_value=({'message': 'started'}, 0)))

    def launch(self, sid=FIRST, **changes):
        return self.hub.launch(project=str(self.project), prompt='Trim the games',
                               name='Termart cleanup', harness='claude', session_id=sid, **changes)

    def cli(self, *argv, env=None):
        from lib.control import sessions
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sessions.main(list(argv), env=self.env if env is None else env)
        return code, out.getvalue(), err.getvalue()

    # -- prefixes -----------------------------------------------------------

    def test_attach_json_accepts_the_short_id_that_list_shows(self):
        self.launch()
        code, out, err = self.cli('attach', FIRST[:8], '--json')
        self.assertEqual(code, 0, err)
        attached = json.loads(out)
        self.assertEqual(attached['session_id'], self.tmux.session_identity)
        self.assertIn('attach-session', attached['attach'])

    def test_option_before_the_prefix_still_resolves(self):
        self.launch()
        code, out, err = self.cli('attach', '--json', FIRST[:8])
        self.assertEqual(code, 0, err)
        self.assertIn('attach-session', json.loads(out)['attach'])

    def test_show_send_stop_and_resume_accept_a_prefix(self):
        self.launch()
        code, out, err = self.cli('show', FIRST[:8], '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['session_id'], FIRST)
        code, out, err = self.cli('send', FIRST[:8], '--text', 'Keep the clock', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.hub.messages(FIRST)), 1)
        code, out, err = self.cli('stop', FIRST[:8], '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.hub.get(FIRST)['lifecycle'], 'stopped')
        code, out, err = self.cli('resume', FIRST[:8], '--text', 'Continue', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.hub.get(FIRST)['generation'], 2)

    def test_close_accepts_a_prefix(self):
        self.launch()
        code, out, err = self.cli('close', FIRST[:8], '--force', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.hub.get(FIRST)['lifecycle'], 'closed')

    def test_prefix_is_case_insensitive_and_full_uuid_is_unchanged(self):
        self.launch()
        for selector in (FIRST[:8].upper(), FIRST):
            with self.subTest(selector=selector):
                code, out, err = self.cli('show', selector, '--json')
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)['session_id'], FIRST)

    def test_unknown_prefix_names_the_session_for_every_verb(self):
        self.launch()
        for argv in (('attach', 'deadbeef'), ('show', 'deadbeef'), ('close', 'deadbeef', '--force'),
                     ('stop', 'deadbeef'), ('resume', 'deadbeef', '--text', 'go'),
                     ('send', 'deadbeef', '--text', 'hi')):
            with self.subTest(argv=argv):
                code, out, err = self.cli(*argv)
                self.assertEqual(code, 2, out)
                self.assertIn("session 'deadbeef' was not found", err)
                self.assertNotIn('room', err)

    def test_ambiguous_prefix_lists_the_candidates_and_acts_on_none(self):
        self.launch()
        self.launch(TWIN, transport='structured')  # the fake tmux holds one Room pane
        for verb in ('attach', 'show', 'stop'):
            with self.subTest(verb=verb):
                code, out, err = self.cli(verb, 'e154c8af')
                self.assertEqual(code, 2, out)
                self.assertIn("session prefix 'e154c8af' is ambiguous", err)
                self.assertIn(FIRST, err)
                self.assertIn(TWIN, err)
        self.assertEqual(self.tmux.killed, [])
        code, out, err = self.cli('show', 'e154c8af-45', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['session_id'], FIRST)

    def test_too_short_prefix_is_refused_with_its_reason(self):
        self.launch()
        code, out, err = self.cli('show', 'e15')
        self.assertEqual(code, 2, out)
        self.assertIn("session 'e15' was not found", err)
        self.assertIn('at least 4', err)

    def test_legacy_room_prefix_and_room_name_still_attach(self):
        opened = rooms_fixture.RoomTests._open(self, room_id='77777777-1111-4111-8111-111111111111')
        self.hub.initialize()  # a live home has its Control database; this path needs one today
        for selector in ('77777777', opened['room_id'], 'Draft Room'):
            with self.subTest(selector=selector):
                code, out, err = self.cli('attach', selector, '--json')
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(out)['room_id'], opened['room_id'])

    def test_hidden_room_ids_of_hub_sessions_do_not_make_a_prefix_ambiguous(self):
        row = self.launch()
        code, out, err = self.cli('show', row['room_id'][:8])
        self.assertEqual(code, 2, out)
        self.assertIn('was not found', err)

    # -- attach attaches ----------------------------------------------------

    def attach(self, *extra, env=None, interactive=True, returncode=0):
        self.tmux.caller_client = mock.Mock(return_value='/dev/pts/7')
        self.tmux.server_identity = mock.Mock(return_value=('/tmp/tmux-1000/default', 4242))
        self.tmux.room_switch_argv = TmuxAdapter().room_switch_argv
        with mock.patch('lib.control.hub_cli._interactive', return_value=interactive), \
                mock.patch('lib.control.hub_cli.subprocess.run',
                           return_value=SimpleNamespace(returncode=returncode)) as run:
            code, out, err = self.cli('attach', FIRST[:8], *extra, env=env)
        return code, out, err, run

    def test_outside_tmux_attach_runs_the_guarded_attach_session(self):
        self.launch()
        code, out, err, run = self.attach()
        self.assertEqual(code, 0, err)
        run.assert_called_once()
        argv = run.call_args.args[0]
        self.assertIn('if-shell', argv)
        self.assertIn('attach-session -t $7', argv)
        self.assertEqual(out, '')

    def test_inside_tmux_on_the_rooms_server_switches_this_client(self):
        self.launch()
        env = dict(self.env, TMUX='/tmp/tmux-1000/default,4242,0', TMUX_PANE='%3')
        code, out, err, run = self.attach(env=env)
        self.assertEqual(code, 0, err)
        self.tmux.server_identity.assert_called_once_with('%42')
        self.tmux.caller_client.assert_called_once_with('%3')
        argv = run.call_args.args[0]
        self.assertIn('if-shell', argv)
        self.assertIn('switch-client -c /dev/pts/7 -t $7', argv)
        self.assertFalse(any('attach-session' in part for part in argv))

    def test_inside_tmux_on_another_server_uses_attach_session(self):
        self.launch()
        env = dict(self.env, TMUX='/tmp/tmux-1000/other,999,0', TMUX_PANE='%3')
        code, out, err, run = self.attach(env=env)
        self.assertEqual(code, 0, err)
        self.tmux.caller_client.assert_not_called()
        self.assertIn('attach-session -t $7', run.call_args.args[0])

    def test_same_server_without_a_client_showing_the_pane_refuses(self):
        self.launch()
        env = dict(self.env, TMUX='/tmp/tmux-1000/default,4242,0', TMUX_PANE='%3')
        self.tmux.caller_client = mock.Mock(return_value=None)
        self.tmux.server_identity = mock.Mock(return_value=('/tmp/tmux-1000/default', 4242))
        with mock.patch('lib.control.hub_cli._interactive', return_value=True), \
                mock.patch('lib.control.hub_cli.subprocess.run') as run:
            code, out, err = self.cli('attach', FIRST[:8], env=env)
        self.assertEqual(code, 2, out)
        run.assert_not_called()
        self.assertIn('no tmux client', err)
        self.assertIn('attach-session', err)

    def test_ownership_refusal_exits_two_and_says_nothing_attached(self):
        self.launch()
        code, out, err, run = self.attach(returncode=66)
        self.assertEqual(code, 2)
        self.assertIn('ownership', err)
        self.assertIn('nothing was attached', err)

    def test_print_json_and_non_interactive_stay_print_only(self):
        self.launch()
        code, out, err, run = self.attach('--print')
        self.assertEqual(code, 0, err)
        run.assert_not_called()
        self.assertIn('attach-session -t', out)
        code, out, err, run = self.attach('--json')
        run.assert_not_called()
        self.assertIn('attach-session', json.loads(out)['attach'])
        code, out, err, run = self.attach(interactive=False)
        run.assert_not_called()
        self.assertIn('attach-session -t', out)

    def test_structured_session_never_runs_tmux(self):
        self.launch(transport='structured')
        code, out, err, run = self.attach()
        self.assertEqual(code, 0, err)
        run.assert_not_called()
        self.assertIn('Open asha control', out)


@unittest.skipUnless(shutil.which('tmux'), 'tmux is required')
class RealTmuxAttachTests(unittest.TestCase):
    """The guarded switch and attach run against an isolated tmux server."""

    room_id = '88888888-1111-4111-8111-111111111111'
    project_id = 'novel-project'

    def setUp(self):
        self.socket = f'asha-attach-{uuid.uuid4().hex[:12]}'
        self.enterContext(TmuxSocketReaper(self.socket))
        self.adapter = TmuxAdapter(socket=self.socket, config_file=Path('/dev/null'))
        returncode, _stdout, _stderr = self.adapter._run_status(['list-commands', 'new-session'])
        if returncode != 0:
            self.skipTest('isolated tmux sockets are unavailable in this execution sandbox')
        self.addCleanup(self._kill_server)
        self.marker = hashlib.sha256(self.project_id.encode()).hexdigest()
        self.home_pane = self.adapter.create_task_session(
            session='home', window='main', start_directory=Path.cwd(), environment={},
            holder_argv=['sleep', '60'], session_options={}, pane_options={}, pane_title='home')
        self.room_pane = self._room('asha-room-attach', self.room_id, self.marker)
        self.room_session = self.adapter.session_id(self.room_pane)

    def _kill_server(self):
        subprocess.run(['tmux', '-L', self.socket, 'kill-server'], capture_output=True, check=False)

    def _room(self, session, room_id, marker):
        from lib.control.rooms import PANE_ROOM_OPTION, SESSION_ROOM_OPTION
        return self.adapter.create_task_session(
            session=session, window='room', start_directory=Path.cwd(), environment={},
            holder_argv=['sleep', '60'], session_options={SESSION_ROOM_OPTION: room_id},
            pane_options={PANE_ROOM_OPTION: room_id, '@asha_room_project_id': marker},
            pane_title='asha:room:attach:codex')

    def _result(self):
        argv = self.adapter.room_attach_argv(room_id=self.room_id, project_marker=self.marker,
                                             pane_id=self.room_pane, session_id=self.room_session)
        return {'room_id': self.room_id, 'project_id': self.project_id, 'pane_id': self.room_pane,
                'session_id': self.room_session, 'name': 'attach', 'attach_argv': argv,
                'attach': shlex.join(argv)}

    def _clients(self):
        output = self.adapter._run(['list-clients', '-F', '#{client_tty}\t#{session_id}'])
        return dict(line.split('\t') for line in output.splitlines() if line)

    def _wait(self, predicate, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail('timed out waiting for tmux')

    def _spawn(self, argv):
        """Run argv on a fresh pty (a real terminal client), outside any tmux."""
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        env = {k: v for k, v in os.environ.items() if not k.startswith('TMUX')}
        env['TERM'] = 'xterm'
        if os.environ.get('TMUX_TMPDIR'):
            env['TMUX_TMPDIR'] = os.environ['TMUX_TMPDIR']
        proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, env=env,
                                cwd=Path(__file__).resolve().parents[2], start_new_session=True)
        os.close(slave)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        return proc, master

    def test_server_identity_matches_what_tmux_exports_to_its_panes(self):
        from lib.control import hub_cli
        exported = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'tmux'
        self.adapter.create_task_session(
            session='probe', window='main', start_directory=Path.cwd(), environment={},
            holder_argv=['sh', '-c', f'printf %s "$TMUX" > {shlex.quote(str(exported))}; sleep 60'],
            session_options={}, pane_options={}, pane_title='probe')
        value = self._wait(lambda: exported.exists() and exported.read_text())
        self.assertTrue(hub_cli.same_server(self.adapter, self.room_pane, {'TMUX': value}))
        self.assertFalse(hub_cli.same_server(self.adapter, self.room_pane,
                                             {'TMUX': value.replace(',', '-other,', 1)}))
        self.assertFalse(hub_cli.same_server(self.adapter, self.room_pane, {}))

    def test_cli_attach_from_inside_tmux_switches_the_callers_client(self):
        from lib.control import hub_cli
        client, _master = self._spawn(['tmux', '-L', self.socket, 'attach-session', '-t', 'home'])
        tty = self._wait(lambda: next(iter(self._clients()), None))
        path, pid = self.adapter.server_identity(self.home_pane)
        env = {'TMUX': f'{path},{pid},0', 'TMUX_PANE': self.home_pane}
        self.assertEqual(hub_cli.attach_terminal(self._result(), tmux=self.adapter, env=env), 0)
        self._wait(lambda: self._clients().get(tty) == self.room_session)

    def test_switch_is_refused_when_the_room_was_replaced(self):
        from lib.control import hub_cli
        client, _master = self._spawn(['tmux', '-L', self.socket, 'attach-session', '-t', 'home'])
        tty = self._wait(lambda: next(iter(self._clients()), None))
        home = self._clients()[tty]
        result = self._result()
        self.adapter.kill_session('asha-room-attach')
        foreign = self._room('asha-room-attach', 'foreign', 'f' * 64)
        result['pane_id'], result['session_id'] = foreign, self.adapter.session_id(foreign)
        path, pid = self.adapter.server_identity(self.home_pane)
        env = {'TMUX': f'{path},{pid},0', 'TMUX_PANE': self.home_pane}
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(hub_cli.attach_terminal(result, tmux=self.adapter, env=env), 2)
        self.assertIn('nothing was attached', err.getvalue())
        self.assertEqual(self._clients()[tty], home)

    def test_cli_attach_outside_tmux_attaches_this_terminal(self):
        code = ('import json, sys; from pathlib import Path; from lib.control import hub_cli; '
                'from lib.control.tmux import TmuxAdapter; '
                f'sys.exit(hub_cli.attach_terminal(json.loads(sys.argv[1]), '
                f'tmux=TmuxAdapter(socket={self.socket!r}, config_file=Path("/dev/null")), env={{}}))')
        proc, _master = self._spawn([sys.executable, '-c', code, json.dumps(self._result())])
        tty = self._wait(lambda: next(iter(self._clients()), None))
        self.assertEqual(self._clients()[tty], self.room_session)
        self.adapter._run(['detach-client', '-t', tty])
        self.assertEqual(proc.wait(timeout=5), 0)


if __name__ == '__main__':
    unittest.main()
