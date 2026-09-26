"""#102 phase 2: the terminal title attention count (off inside tmux unless set-titles is on)."""
import io
import os
import shutil
import subprocess
import time
import unittest

from lib.control import session_title, session_tui
from lib.control.socket_reaper import TmuxSocketReaper


def probe(value, panes='%3 $1'):
    """A fake tmux: ``panes`` answers the pane listing, ``value`` every option read."""
    calls = []

    def run(argv):
        calls.append(argv)
        return panes if argv[0] == 'list-panes' else value
    run.calls = calls
    return run


class PolicyTests(unittest.TestCase):
    def supported(self, env, tmux='off', tsl=None):
        return session_title.title_supported(env, tmux=probe(tmux), tigetstr=lambda name: tsl)

    def test_known_terminals_outside_tmux_default_on(self):
        for term in ('xterm-256color', 'alacritty', 'foot', 'xterm-kitty', 'rxvt-unicode', 'vte-256color'):
            with self.subTest(term=term):
                self.assertTrue(self.supported({'TERM': term}))

    def test_unknown_or_dumb_terminals_are_off_unless_terminfo_has_a_status_line(self):
        for term in ('', 'dumb', 'linux', 'vt100'):
            with self.subTest(term=term):
                self.assertFalse(self.supported({'TERM': term}))
        self.assertTrue(self.supported({'TERM': 'vt100'}, tsl=b'\x1b]2;'))
        self.assertFalse(self.supported({'TERM': 'dumb'}, tsl=b'\x1b]2;'))

    def test_inside_tmux_it_follows_set_titles(self):
        env = {'TERM': 'tmux-256color', 'TMUX': '/tmp/tmux-1000/default,1,0', 'TMUX_PANE': '%3'}
        off = probe('off')
        self.assertFalse(session_title.title_supported(env, tmux=off, tigetstr=lambda n: None))
        # Q14-F1: the effective value for the dashboard's own session, not the global one.
        self.assertEqual(off.calls[-1], ['show-options', '-Av', '-t', '$1', 'set-titles'])
        self.assertTrue(self.supported(env, tmux='on'))
        self.assertFalse(self.supported(env, tmux=None))       # tmux unreadable: off

    def test_inside_tmux_the_session_must_be_the_panes_only_session(self):
        # Q15-F1: a pane ID does not name a session; a pane shared by grouped
        # sessions or a linked window has no single policy, so the title is off.
        env = {'TERM': 'tmux-256color', 'TMUX': '/tmp/tmux-1000/default,1,0', 'TMUX_PANE': '%3'}
        cases = {'%3 $1': True, '%0 $0\n%3 $1\n%4 $1': True, '%3 $1\n%3 $2': False, '%3 $1\n%3 $1': True,
                 '%0 $0': False, '': False, None: False}
        for panes, expected in cases.items():
            with self.subTest(panes=panes):
                tmux = probe('on', panes)
                self.assertEqual(session_title.title_supported(env, tmux=tmux, tigetstr=lambda n: None), expected)
                if not expected:
                    self.assertFalse(any(argv[0] == 'show-options' for argv in tmux.calls))
        # Without TMUX_PANE nothing identifies the dashboard's session: off, not the server's guess.
        no_pane = probe('on')
        self.assertFalse(session_title.title_supported({k: v for k, v in env.items() if k != 'TMUX_PANE'},
                                                       tmux=no_pane, tigetstr=lambda n: None))
        self.assertEqual(no_pane.calls, [])

    def test_inside_tmux_the_terminal_must_still_take_a_title(self):
        # Q14-F1: tmux saying on does not bypass the terminal-capability test.
        tmux = {'TMUX': '/tmp/tmux-1000/default,1,0', 'TMUX_PANE': '%3'}
        for term in ('vt100', 'dumb', 'linux', ''):
            with self.subTest(term=term):
                self.assertFalse(self.supported(dict(tmux, TERM=term), tmux='on'))
        for term in ('tmux-256color', 'screen-256color', 'xterm-256color'):
            with self.subTest(term=term):
                self.assertTrue(self.supported(dict(tmux, TERM=term), tmux='on'))

    def test_a_control_managed_session_and_the_opt_out_are_off(self):
        self.assertFalse(self.supported({'TERM': 'xterm', 'ASHA_HUB_SESSION_ID': 'x'}))
        self.assertFalse(self.supported({'TERM': 'xterm', 'ASHA_CONTROL_TITLE': '0'}))

    def test_title_text(self):
        self.assertEqual(session_title.title_text({'input': 0, 'approval': 0}), 'asha control')
        self.assertEqual(session_title.title_text({'input': 1, 'approval': 1}), '2 awaiting input · asha control')


class WriterTests(unittest.TestCase):
    def test_disabled_writer_writes_nothing(self):
        stream = io.BytesIO()
        writer = session_title.TitleWriter(stream, enabled=False)
        writer.set('2 awaiting input')
        writer.close()
        self.assertEqual(stream.getvalue(), b'')

    def test_writes_only_on_change_and_restores_on_close(self):
        stream = io.BytesIO()
        writer = session_title.TitleWriter(stream, enabled=True)
        writer.set('asha control')
        writer.set('asha control')
        writer.set('1 awaiting input · asha control')
        writer.close()
        self.assertEqual(stream.getvalue(), b'\x1b[22;0t\x1b]2;asha control\x07'
                                            b'\x1b]2;1 awaiting input \xc2\xb7 asha control\x07\x1b[23;0t')

    def test_tmux_restores_the_previous_pane_title_instead_of_popping(self):
        stream = io.BytesIO()
        writer = session_title.TitleWriter(stream, enabled=True, restore='previous')
        writer.set('asha control')
        writer.close()
        self.assertEqual(stream.getvalue(), b'\x1b]2;asha control\x07\x1b]2;previous\x07')

    def test_control_characters_never_reach_the_terminal(self):
        stream = io.BytesIO()
        writer = session_title.TitleWriter(stream, enabled=True, restore='a\x07b\x1b]52;c;x')
        writer.set('x\x1b]2;evil\x07')
        writer.close()
        self.assertEqual(stream.getvalue().count(b'\x1b'), 2)
        self.assertEqual(stream.getvalue().count(b'\x07'), 2)

    def test_terminfo_status_line_strings_are_used_when_present(self):
        stream = io.BytesIO()
        writer = session_title.TitleWriter(stream, enabled=True, sequence=(b'<tsl>', b'<fsl>'))
        writer.set('t')
        self.assertIn(b'<tsl>t<fsl>', stream.getvalue())

    def test_write_errors_disable_the_writer(self):
        class Broken(io.BytesIO):
            def write(self, data):
                raise OSError('closed')
        writer = session_title.TitleWriter(Broken(), enabled=True)
        writer.set('t')
        writer.set('u')
        writer.close()
        self.assertFalse(writer.enabled)


@unittest.skipUnless(shutil.which('tmux'), 'tmux required')
class RealTmuxPolicyTests(unittest.TestCase):
    """Q14-F1 on a disposable tmux server: a session override beats the global value."""

    def setUp(self):
        self.socket = f'asha-title-{os.getpid()}-{time.time_ns()}'
        self.base = dict(os.environ)
        self.enterContext(TmuxSocketReaper(self.socket, environ=self.base))
        # An explicit private -S socket: the path the reaper derives for its -L name.
        directory = os.path.join(self.base.get('TMUX_TMPDIR') or '/tmp', f'tmux-{os.getuid()}')
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, self.socket)
        if self.tmux('list-commands', 'new-session', check=False).returncode != 0:
            self.skipTest('isolated tmux sockets are unavailable in this execution sandbox')
        self.tmux('new-session', '-d', '-s', 'q14', '/bin/sleep', '120')
        pid, path, pane = self.tmux('display-message', '-p', '-t', 'q14', '#{pid}\t#{socket_path}\t#{pane_id}'
                                    ).stdout.strip().split('\t')
        self.env = {'PATH': os.environ.get('PATH', ''), 'TMUX_TMPDIR': os.environ.get('TMUX_TMPDIR', ''),
                    'TERM': 'tmux-256color', 'TMUX': f'{path},{pid},0', 'TMUX_PANE': pane}

    def tmux(self, *argv, check=True):
        return subprocess.run(['tmux', '-S', self.path, '-f', '/dev/null', *argv], capture_output=True,
                              text=True, check=check, env=self.base)

    def output(self, global_value, session_value):
        self.tmux('set-option', '-g', 'set-titles', global_value)
        self.tmux('set-option', '-t', 'q14', 'set-titles', session_value)
        return self.write()

    def write(self, env=None):
        stream = io.BytesIO()
        env = env or self.env
        writer = session_title.open_writer(env, stream=stream, tmux=session_tui._tmux_reader(env),
                                           tigetstr=lambda name: None)
        writer.set('1 awaiting input · asha control')
        writer.close()
        return writer, stream.getvalue()

    def test_the_session_value_decides_not_the_global_one(self):
        for global_value, session_value in (('on', 'off'), ('off', 'on'), ('off', 'off'), ('on', 'on')):
            with self.subTest(global_value=global_value, session_value=session_value):
                writer, written = self.output(global_value, session_value)
                if session_value == 'on':
                    self.assertIn(b'\x1b]2;1 awaiting input', written)
                else:
                    self.assertFalse(writer.enabled)
                    self.assertEqual(written, b'')

    def test_a_pane_shared_by_sessions_has_no_title(self):
        # Q15-F1: grouped sessions and a linked window put one pane in two
        # sessions; which one the dashboard is seen in is unknowable, so off.
        # No combination of values, in either direction, may emit a byte.
        for share in ('group', 'link'):
            for global_value in ('on', 'off'):
                for mine, other in (('off', 'on'), ('on', 'off'), ('on', 'on')):
                    with self.subTest(share=share, global_value=global_value, mine=mine, other=other):
                        if share == 'group':
                            self.tmux('new-session', '-d', '-s', 'mirror', '-t', 'q14')
                        else:
                            self.tmux('new-session', '-d', '-s', 'mirror', '/bin/sleep', '120')
                            self.tmux('link-window', '-s', 'q14:0', '-t', 'mirror:9')
                        try:
                            self.tmux('set-option', '-g', 'set-titles', global_value)
                            self.tmux('set-option', '-t', 'q14', 'set-titles', mine)
                            self.tmux('set-option', '-t', 'mirror', 'set-titles', other)
                            for env in (self.env, {k: v for k, v in self.env.items() if k != 'TMUX_PANE'}):
                                writer, written = self.write(env)
                                self.assertFalse(writer.enabled)
                                self.assertEqual(written, b'')
                        finally:
                            self.tmux('kill-session', '-t', 'mirror')
        # Unshared again, the pane's one session decides as before.
        self.assertIn(b'\x1b]2;', self.output('off', 'on')[1])

    def test_an_unset_session_value_inherits_the_global_one(self):
        for value in ('on', 'off'):
            with self.subTest(value=value):
                self.tmux('set-option', '-g', 'set-titles', value)
                self.tmux('set-option', '-u', '-t', 'q14', 'set-titles')
                reader = session_tui._tmux_reader(self.env)
                self.assertEqual(session_title.title_supported(self.env, tmux=reader, tigetstr=lambda n: None),
                                 value == 'on')


if __name__ == '__main__':
    unittest.main()
