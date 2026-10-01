"""#102 phase 3: the read-only session preview.

The preview must be incapable of sending input to a pane: it only ever runs
``tmux capture-pane`` with fixed flags after a Room ownership check, never
acknowledges structured events, and sanitizes untrusted pane output before it
reaches the dashboard's own terminal.
"""
import ast
import json
import re
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest import mock

from lib.control import pane_peek, session_layout, session_preview
from lib.control.config import ConfigError, load_config
from lib.control.rooms import PANE_PROJECT_OPTION, PANE_ROOM_OPTION, SESSION_ROOM_OPTION, _project_marker
from lib.control.tmux import TmuxAdapter, TmuxError

ROOT = Path(__file__).resolve().parents[2]
ROOM = '11111111-2222-4333-8444-555555555555'
PROJECT = 'project-asha'
READ_ONLY_VERBS = {'capture-pane', 'display-message', 'show-options', 'show-hooks', 'has-session', 'list-panes'}
HOOK_SCOPES = {('-g',): 'global', ('-gw',): 'global-window', ('-t', '%7'): 'session',
               ('-w', '-t', '%7'): 'window', ('-p', '-t', '%7'): 'pane'}
# What tmux 3.4 prints for `show-hooks -g` with nothing configured: bare names (verified live).
UNSET_GLOBAL_HOOKS = b'after-bind-key\nafter-capture-pane\nafter-display-message\nafter-show-options\n'


def record(pane='%7', session_id='$3'):
    return {'room_id': ROOM, 'project_id': PROJECT, 'name': 'r', 'lifecycle': 'open',
            'tmux': {'session': 'asha-room-11111111', 'window': 'main', 'pane_id': pane, 'session_id': session_id}}


class FakeTmuxRunner:
    """A tmux binary double: answers ownership probes and captures, records every argv."""

    def __init__(self, *, owner=ROOM, screen=b'line one\nline two\n', fail_capture=False, dead='0', hooks=None,
                 after_capture=None, on_call=None, hook_stderr=b'server exited unexpectedly'):
        self.calls, self.owner, self.screen, self.fail_capture, self.dead = [], owner, screen, fail_capture, dead
        self.hooks = dict(hooks or {})      # scope name -> show-hooks stdout, or an int exit status
        self.after_capture, self.on_call, self.hook_stderr, self.timeouts = after_capture, on_call, hook_stderr, []

    def __call__(self, argv, **kwargs):
        args = argv[1:]
        self.calls.append(args)
        self.timeouts.append(kwargs.get('timeout'))
        if self.on_call:
            self.on_call(self, args)
        verb = args[0]
        if verb == 'show-hooks':
            scope = HOOK_SCOPES.get(tuple(args[1:]))
            if scope is None:
                return CompletedProcess(argv, 1, b'', b'unexpected show-hooks form')
            output = self.hooks.get(scope, UNSET_GLOBAL_HOOKS if scope.startswith('global') else b'')
            if isinstance(output, int):
                return CompletedProcess(argv, output, b'', self.hook_stderr)
            return CompletedProcess(argv, 0, output, b'')
        if verb == 'capture-pane':
            if self.fail_capture:
                raise OSError('boom')
            if self.after_capture:
                self.after_capture(self)
            return CompletedProcess(argv, 0, self.screen, b'')
        if verb == 'show-options':
            key = args[-1]
            value = {SESSION_ROOM_OPTION: self.owner, PANE_ROOM_OPTION: self.owner,
                     PANE_PROJECT_OPTION: _project_marker(PROJECT)}.get(key)
            return CompletedProcess(argv, 0, (value + '\n').encode(), b'') if value else \
                CompletedProcess(argv, 1, b'', b'invalid option')
        if verb == 'display-message':
            fmt = args[-1]
            if fmt == '#{session_id}':
                return CompletedProcess(argv, 0, b'$3\n', b'')
            fields = {'pane_id': '%7', 'pane_pid': '4242', 'pane_dead': self.dead, 'pane_dead_status': '',
                      'pane_dead_signal': '', 'session_name': 'asha-room-11111111', 'window_name': 'main',
                      'session_id': '$3', 'window_id': '@1', 'pane_active': '1'}
            text = re.sub(r'#\{([a-z_]+)\}', lambda m: fields.get(m.group(1), ''), fmt)
            return CompletedProcess(argv, 0, (text + '\n').encode(), b'')
        if verb in {'has-session', 'list-panes'}:
            return CompletedProcess(argv, 0, b'', b'')
        return CompletedProcess(argv, 1, b'', b'unexpected verb')


class PanePeekArgvTests(unittest.TestCase):
    def test_capture_issues_exactly_the_fixed_capture_argv(self):
        runner = FakeTmuxRunner()
        tmux = TmuxAdapter(runner=runner)
        lines = pane_peek.PanePeek(tmux, '%7').capture(12)
        self.assertEqual(runner.calls, [['capture-pane', '-p', '-J', '-t', '%7', '-S', '-12']])
        self.assertEqual(lines, ['line one', 'line two'])

    def test_every_call_on_the_ownership_checked_path_is_read_only(self):
        for kwargs in ({}, {'fail_capture': True}, {'owner': 'someone-else'}, {'dead': '1'}):
            with self.subTest(**kwargs):
                runner = FakeTmuxRunner(**kwargs)
                tmux = TmuxAdapter(runner=runner)
                try:
                    pane_peek.peek_room(record(), tmux, 5)
                except (pane_peek.PeekRefused, TmuxError):
                    pass
                verbs = {call[0] for call in runner.calls}
                self.assertTrue(verbs <= READ_ONLY_VERBS, verbs)
                for call in runner.calls:
                    if call[0] == 'capture-pane':
                        self.assertEqual(call, ['capture-pane', '-p', '-J', '-t', '%7', '-S', '-5'])

    def test_ownership_is_checked_before_every_capture(self):
        runner = FakeTmuxRunner()
        tmux = TmuxAdapter(runner=runner)
        for _ in range(3):
            pane_peek.peek_room(record(), tmux, 4)
        captures = [i for i, call in enumerate(runner.calls) if call[0] == 'capture-pane']
        self.assertEqual(len(captures), 3)
        previous = -1
        for index in captures:
            probes = [c for c in runner.calls[previous + 1:index] if c[0] == 'show-options']
            self.assertTrue(any(PANE_ROOM_OPTION in c for c in probes), 'no pane ownership probe before capture')
            previous = index

    def test_a_foreign_pane_is_never_captured(self):
        runner = FakeTmuxRunner(owner='another-room')
        with self.assertRaises(pane_peek.PeekRefused):
            pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5)
        self.assertNotIn('capture-pane', {call[0] for call in runner.calls})

    def test_line_count_and_width_are_bounded(self):
        runner = FakeTmuxRunner(screen=('x' * 5000 + '\n').encode() * 3)
        lines = pane_peek.PanePeek(TmuxAdapter(runner=runner), '%7').capture(10_000)
        self.assertEqual(runner.calls[0][-1], f'-{pane_peek.MAX_LINES}')
        self.assertTrue(all(len(line) == pane_peek.LINE_CHARS for line in lines))
        with self.assertRaises(ValueError):
            pane_peek.PanePeek(TmuxAdapter(runner=runner), '%7').capture(0)

    def test_pane_identity_is_validated(self):
        for bad in ('7', '%7; kill-server', '%', 'main:0', '%7 -X'):
            with self.subTest(bad=bad), self.assertRaises(TmuxError):
                pane_peek.PanePeek(TmuxAdapter(runner=FakeTmuxRunner()), bad)

    def test_invalid_utf8_and_trailing_blank_screen_lines(self):
        runner = FakeTmuxRunner(screen=b'ok \xff\nlast\n\n\n\n')
        lines = pane_peek.PanePeek(TmuxAdapter(runner=runner), '%7').capture(10)
        self.assertEqual(lines, ['ok \ufffd', 'last'])


class PanePeekStaticTests(unittest.TestCase):
    """The module has no code path that could inject: no such word appears in it at all."""

    SOURCE = (ROOT / 'lib' / 'control' / 'pane_peek.py').read_text(encoding='utf-8')

    def test_no_injecting_verb_is_referenced(self):
        for word in ('send-keys', 'send_keys', 'paste', 'attach', 'popup', 'pipe-pane', 'pipe_pane',
                     'select-pane', 'select_pane', 'set-buffer', 'load-buffer', 'switch-client', 'inject'):
            with self.subTest(word=word):
                self.assertNotIn(word, self.SOURCE.lower())

    def test_imports_nothing_that_injects(self):
        tree = ast.parse(self.SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        self.assertFalse({name for name in imported if re.search(r'inject|input|attach|paste|popup', name)})

    def test_the_only_tmux_verbs_are_capture_and_the_hook_read(self):
        verbs = set(re.findall(r"'([a-z]+-[a-z]+)'", self.SOURCE)) - set(pane_peek.GUARDED_HOOKS)
        self.assertEqual(verbs, {'capture-pane', 'show-hooks'})


INJECT = b'after-capture-pane[0] send-keys -t %9 -l QA17-PREVIEW-INJECTED\n'


class HookParseTests(unittest.TestCase):
    """Q17-F1: `name[index] command` is configured; a bare name is unset; anything else refuses."""

    def test_the_bare_name_tmux_prints_when_unset_is_unset(self):
        self.assertEqual(pane_peek.configured_hooks(b'after-capture-pane\n'), set())
        self.assertEqual(pane_peek.configured_hooks(UNSET_GLOBAL_HOOKS), set())
        self.assertEqual(pane_peek.configured_hooks(b''), set())

    def test_an_indexed_entry_with_a_command_is_configured(self):
        self.assertEqual(pane_peek.configured_hooks(INJECT), {'after-capture-pane'})
        self.assertEqual(pane_peek.configured_hooks(
            b'after-capture-pane\nafter-show-options[3] run-shell "tmux -S /x send-keys q"\n'),
            {'after-show-options'})

    def test_unguarded_hooks_are_not_reported(self):
        # Control's own attach-generation hooks live on Room sessions; they fire on attach, not on a read.
        self.assertEqual(pane_peek.configured_hooks(b'client-attached[0] run-shell "echo"\n'), set())

    def test_unparseable_output_refuses(self):
        for output in (b'after-capture-pane[0]\n', b'after-capture-pane[x] cmd\n', b' after-capture-pane\n',
                       b'\xff\xfe\n', b'after-capture-pane[0]send-keys\n', b'weird line with spaces\n'):
            with self.subTest(output=output), self.assertRaises(pane_peek.PeekDisabled):
                pane_peek.configured_hooks(output)


class HookGuardTests(unittest.TestCase):
    """Q17-F1: a configured after-capture hook (or one on the ownership reads) disables the preview."""

    def verbs(self, runner):
        return [call[0] for call in runner.calls]

    def test_a_configured_hook_at_any_scope_refuses_before_any_other_read(self):
        for scope in HOOK_SCOPES.values():
            for name in pane_peek.GUARDED_HOOKS:
                with self.subTest(scope=scope, name=name):
                    runner = FakeTmuxRunner(hooks={scope: name.encode() + b'[0] send-keys -t %9 -l X\n'})
                    with self.assertRaises(pane_peek.PeekDisabled) as caught:
                        pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5)
                    self.assertEqual(str(caught.exception), f'Preview disabled: tmux {name} hook configured')
                    self.assertEqual(set(self.verbs(runner)), {'show-hooks'})

    def test_every_scope_is_queried_before_ownership_and_again_right_before_capture(self):
        runner = FakeTmuxRunner()
        pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5)
        verbs = self.verbs(runner)
        capture = verbs.index('capture-pane')
        scopes = {HOOK_SCOPES[tuple(call[1:])] for call in runner.calls[capture - 5:capture]}
        self.assertEqual(scopes, set(HOOK_SCOPES.values()))
        self.assertEqual(verbs[:5], ['show-hooks'] * 5)
        self.assertLess(verbs.index('show-hooks'), verbs.index('display-message'))

    def test_a_hook_set_after_the_ownership_check_still_refuses_the_capture(self):
        def arm(runner, args):
            if args[0] == 'show-options' and PANE_PROJECT_OPTION in args:
                runner.hooks['pane'] = INJECT
        runner = FakeTmuxRunner(on_call=arm)
        with self.assertRaises(pane_peek.PeekDisabled):
            pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5)
        self.assertNotIn('capture-pane', self.verbs(runner))

    def test_a_failed_or_unreadable_hook_query_refuses(self):
        for hooks in ({'global': 1}, {'pane': 1}, {'window': b'??\n'}, {'global-window': b'\xff\n'}):
            with self.subTest(hooks=hooks):
                runner = FakeTmuxRunner(hooks=hooks)
                with self.assertRaises(pane_peek.PeekDisabled):
                    pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5)
                self.assertNotIn('capture-pane', self.verbs(runner))

    def test_a_vanished_pane_is_refused_as_missing(self):
        runner = FakeTmuxRunner(hooks={'session': 1}, hook_stderr=b"can't find pane: %7")
        with self.assertRaises(pane_peek.PeekRefused) as caught:
            pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5)
        self.assertNotIsInstance(caught.exception, pane_peek.PeekDisabled)
        self.assertIn('pane missing', str(caught.exception))
        self.assertEqual({call[0] for call in runner.calls}, {'show-hooks'})

    def test_a_query_that_raises_refuses(self):
        def boom(runner, args):
            if args[0] == 'show-hooks':
                raise OSError('tmux vanished')
        runner = FakeTmuxRunner(on_call=boom)
        with self.assertRaises(pane_peek.PeekDisabled):
            pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5)
        self.assertNotIn('capture-pane', self.verbs(runner))


class OwnershipAfterCaptureTests(unittest.TestCase):
    """Q17-F2: ownership of the same pane is re-read after the capture; a change discards the screen."""

    def test_a_pane_that_changed_owner_during_the_capture_is_discarded(self):
        def swap(runner):
            runner.owner = None                 # a replacement pane with no Room markers
        runner = FakeTmuxRunner(screen=b'FOREIGN-REPLACEMENT\n', after_capture=swap)
        with self.assertRaises(pane_peek.PeekRefused):
            pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5)
        verbs = [call[0] for call in runner.calls]
        self.assertIn('show-options', verbs[verbs.index('capture-pane'):])

    def test_an_unchanged_pane_is_published(self):
        runner = FakeTmuxRunner()
        self.assertEqual(pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5), ['line one', 'line two'])


class ReadDeadlineTests(unittest.TestCase):
    """Q17-F3: every tmux read of one preview shares the short capture deadline."""

    def test_no_read_waits_longer_than_the_preview_deadline(self):
        runner = FakeTmuxRunner()
        pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5)
        self.assertTrue(runner.timeouts)
        self.assertTrue(all(0 < t <= pane_peek.DEADLINE_SECONDS for t in runner.timeouts), runner.timeouts)

    def test_the_deadline_is_shared_across_the_whole_read(self):
        clock = [0.0]

        def slow(runner, args):
            clock[0] += 0.6                      # each tmux call takes 0.6 s of the budget
        runner = FakeTmuxRunner(on_call=slow)
        with mock.patch.object(pane_peek.time, 'monotonic', lambda: clock[0]):
            with self.assertRaises(pane_peek.PeekRefused):
                pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5)
        self.assertNotIn('capture-pane', [call[0] for call in runner.calls])
        self.assertLessEqual(len(runner.calls), 4)


class SanitizeTests(unittest.TestCase):
    def test_osc52_clipboard_write_is_removed(self):
        for terminator in ('\x07', '\x1b\\', ''):
            with self.subTest(terminator=repr(terminator)):
                text = session_preview.sanitize('a\x1b]52;c;ZXZpbA==' + terminator + 'b')
                self.assertNotIn('\x1b', text)
                self.assertNotIn('52;', text)
                self.assertTrue(text.startswith('a'))

    def test_title_set_is_removed(self):
        for sequence in ('\x1b]0;evil\x07', '\x1b]2;evil\x1b\\', '\x1bkevil\x1b\\', '\x9d2;evil\x9c'):
            with self.subTest(sequence=repr(sequence)):
                self.assertEqual(session_preview.sanitize('x' + sequence + 'y'), 'xy')

    def test_csi_cursor_moves_and_sgr_are_removed(self):
        for sequence in ('\x1b[2J', '\x1b[10;5H', '\x1b[3A', '\x1b[?1049h', '\x1b[31;1m', '\x9b2J', '\x1b7',
                         '\x1b8', '\x1bc', '\x1b(0', '\x1bP1$qm\x1b\\', '\x1b_apc\x1b\\'):
            with self.subTest(sequence=repr(sequence)):
                self.assertEqual(session_preview.sanitize('<' + sequence + '>'), '<>')

    def test_c0_c1_and_format_controls_are_removed(self):
        text = session_preview.sanitize('a\x00b\x07c\x08d\re\x7ff\x85g\u202eh\u2028i\x1b\tk')
        self.assertNotRegex(text, r'[\x00-\x1f\x7f-\x9f\u202e\u2028]')
        self.assertEqual(text, 'abcdefghi k')
        # ESC plus one final byte is itself a sequence (ESC j), so the byte goes too.
        self.assertEqual(session_preview.sanitize('i\x1bjk'), 'ik')

    def test_plain_and_wide_text_is_kept(self):
        self.assertEqual(session_preview.sanitize('• Ran rg -n 工程 ✓'), '• Ran rg -n 工程 ✓')


class StructuredPreviewTests(unittest.TestCase):
    class Store:
        def __init__(self, pages):
            self.pages, self.calls = list(pages), []

        def events(self, sid, **kwargs):
            self.calls.append(kwargs)
            return self.pages.pop(0) if self.pages else {'events': [], 'complete': True,
                                                         'next_event_cursor': kwargs.get('after')}

        def acknowledge_events(self, *args, **kwargs):
            raise AssertionError('a preview must never acknowledge events')

    @staticmethod
    def event(sequence, kind, payload, turn='t1'):
        return {'sequence': sequence, 'kind': kind, 'payload': payload, 'turn_id': turn}

    def test_reads_never_pass_a_consumer_and_never_acknowledge(self):
        store = self.Store([{'events': [self.event(1, 'text', {'text': 'hello\nwor'}),
                                        self.event(2, 'text', {'text': 'ld'})],
                             'complete': True, 'next_event_cursor': 2}])
        tail = session_preview.StructuredTail()
        lines = tail.read(store, 'sid')
        tail.read(store, 'sid')
        for call in store.calls:
            self.assertNotIn('consumer', call)
        self.assertEqual([call['after'] for call in store.calls], [0, 2])
        self.assertEqual(lines, ['hello', 'world'])

    def test_tool_and_turn_events_become_short_lines_and_text_is_sanitized(self):
        store = self.Store([{'events': [self.event(1, 'text', {'text': 'a\x1b]52;c;x\x07b'}),
                                        self.event(2, 'tool', {'name': 'Bash', 'status': 'completed'}),
                                        self.event(3, 'request-opened', {'question': 'Which?'}),
                                        self.event(4, 'turn-finished', {'outcome': 'idle'})],
                             'complete': True, 'next_event_cursor': 4}])
        lines = session_preview.StructuredTail().read(store, 'sid')
        self.assertEqual(lines[0], 'ab')
        self.assertIn('Bash', lines[1])
        self.assertIn('Which?', lines[2])
        self.assertIn('idle', lines[3])

    def test_retained_text_is_bounded(self):
        many = [self.event(i, 'text', {'text': f'line {i}\n'}) for i in range(1, 600)]
        store = self.Store([{'events': many[:300], 'complete': False, 'next_event_cursor': 300},
                            {'events': many[300:], 'complete': True, 'next_event_cursor': 599}])
        lines = session_preview.StructuredTail().read(store, 'sid')
        self.assertLessEqual(len(lines), session_preview.MAX_LINES)
        self.assertEqual(lines[-1], 'line 599')

    def test_a_tail_still_behind_after_its_page_budget_says_so(self):
        pages = [{'events': [self.event(i, 'text', {'text': f'{i}\n'})], 'complete': False, 'next_event_cursor': i}
                 for i in range(1, 10)]
        store = self.Store(pages)
        lines = session_preview.StructuredTail().read(store, 'sid')
        self.assertEqual(len(store.calls), session_preview.EVENT_PAGES)
        self.assertIn('older events', lines[-1])

    def test_the_structured_source_uses_the_store_without_a_consumer(self):
        # The code path the dashboard runs: grep the source, not only the double.
        source = (ROOT / 'lib' / 'control' / 'session_preview.py').read_text(encoding='utf-8')
        self.assertNotIn('acknowledge', source)
        self.assertNotRegex(source, r'consumer\s*=')


class ReaderTests(unittest.TestCase):
    """The production reader, with its stores doubled: the same seams the dashboard runs."""

    def test_structured_rows_read_events_without_a_consumer(self):
        store = StructuredPreviewTests.Store([{'events': [StructuredPreviewTests.event(1, 'text', {'text': 'hi'})],
                                               'complete': True, 'next_event_cursor': 1}])
        with mock.patch('lib.control.session_store.SessionStore', return_value=mock.MagicMock(
                __enter__=lambda s: store, __exit__=lambda *a: None)):
            lines = session_preview.reader_for(object())(session(transport='structured'), 10,
                                                         session_preview.StructuredTail())
        self.assertEqual(lines, ['hi'])
        self.assertEqual(store.calls, [{'after': 0, 'limit': session_preview.EVENT_PAGE}])

    def test_pane_rows_check_ownership_through_the_room_record(self):
        runner = FakeTmuxRunner(screen=b'\x1b]52;c;eA==\x07visible\n')
        with mock.patch('lib.control.rooms.RoomStore') as rooms:
            rooms.return_value.read.return_value = record()
            lines = session_preview.reader_for(object(), TmuxAdapter(runner=runner))(session(), 10, None)
        rooms.return_value.read.assert_called_once_with(ROOM)
        self.assertEqual(lines, ['visible'])
        self.assertTrue({call[0] for call in runner.calls} <= READ_ONLY_VERBS)


class CancelTests(unittest.TestCase):
    """#106 (QA18 follow-up): closing the dashboard cancels a running read, not just its result."""

    def test_a_cancelled_peek_issues_no_further_tmux_command(self):
        import threading
        cancelled = threading.Event()

        def close_mid_read(runner, args):
            if args[0] == 'display-message':
                cancelled.set()             # the dashboard closes during the ownership read
        runner = FakeTmuxRunner(on_call=close_mid_read)
        with self.assertRaises(pane_peek.PeekRefused):
            pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5, cancelled=cancelled)
        verbs = [call[0] for call in runner.calls]
        self.assertNotIn('capture-pane', verbs)
        self.assertEqual(verbs[-1], 'display-message')

    def test_a_peek_cancelled_before_it_starts_runs_nothing(self):
        import threading
        cancelled = threading.Event()
        cancelled.set()
        runner = FakeTmuxRunner()
        with self.assertRaises(pane_peek.PeekRefused):
            pane_peek.peek_room(record(), TmuxAdapter(runner=runner), 5, cancelled=cancelled)
        self.assertEqual(runner.calls, [])

    def test_close_cancels_the_running_production_read(self):
        import threading
        entered, release = threading.Event(), threading.Event()

        def stall(runner, args):
            if args[0] == 'display-message' and not entered.is_set():
                entered.set()
                release.wait(5)             # the read is running when the dashboard closes
        runner = FakeTmuxRunner(on_call=stall)
        with mock.patch('lib.control.rooms.RoomStore') as rooms:
            rooms.return_value.read.return_value = record()
            cancelled = threading.Event()
            poller = session_preview.Poller(
                reader=session_preview.reader_for(object(), TmuxAdapter(runner=runner), cancelled=cancelled),
                cancelled=cancelled)
            poller.tick(session(), lines=5)
            self.assertTrue(entered.wait(5))
            poller.close()
            release.set()
            for _ in range(200):
                if not any(t.name == 'asha-session-preview' and t.is_alive() for t in threading.enumerate()):
                    break
                threading.Event().wait(0.01)
        self.assertNotIn('capture-pane', [call[0] for call in runner.calls])

    def test_a_queued_read_never_starts_after_close(self):
        reads, pool = [], ManualPool()
        poller = session_preview.Poller(reader=lambda *a: reads.append(a) or [], pool=pool, clock=Clock())
        poller.tick(session(), lines=5)
        poller.close()
        pool.run()
        self.assertEqual(reads, [])

    def test_a_structured_read_stops_between_pages_once_cancelled(self):
        import threading
        cancelled = threading.Event()
        page = {'events': [StructuredPreviewTests.event(1, 'text', {'text': 'x\n'})], 'complete': False,
                'next_event_cursor': 1}
        store = StructuredPreviewTests.Store([page] * session_preview.EVENT_PAGES)
        real = store.events

        def events(*args, **kwargs):
            cancelled.set()                 # close lands while the first page is read
            return real(*args, **kwargs)
        store.events = events
        with self.assertRaises(session_preview.Cancelled):
            session_preview.StructuredTail().read(store, 's1', cancelled=cancelled)
        self.assertEqual(len(store.calls), 1)


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class ManualPool:
    """An executor double: work runs only when the test says so."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args, **kwargs):
        future = Future()
        self.jobs.append((future, fn, args, kwargs))
        return future

    def run(self, index=0):
        future, fn, args, kwargs = self.jobs.pop(index)
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)

    def shutdown(self, **kwargs):
        pass


def session(sid='s1', **changes):
    return dict(dict(session_id=sid, transport='terminal', lifecycle='open', room_id=ROOM, generation=1,
                     activity='working'), **changes)


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.pool, self.reads = Clock(), ManualPool(), []

        def reader(row, lines, tail):
            self.reads.append((row['session_id'], lines))
            return [f"{row['session_id']} screen"]
        self.poller = session_preview.Poller(reader=reader, pool=self.pool, clock=self.clock,
                                             wall=lambda: 1_700_000_000.0)

    def test_only_the_selected_row_is_read_and_at_most_every_500ms(self):
        row = session()
        self.poller.tick(row, lines=10)
        self.poller.tick(row, lines=10)      # one in flight: nothing new
        self.assertEqual(len(self.pool.jobs), 1)
        self.pool.run()
        self.clock.now += 0.2
        self.poller.tick(row, lines=10)      # result collected; too soon for another
        self.assertEqual(len(self.pool.jobs), 0)
        self.clock.now += 0.31
        self.poller.tick(row, lines=10)
        self.assertEqual(len(self.pool.jobs), 1)
        self.assertEqual(self.poller.current(row).lines, ['s1 screen'])

    def test_a_result_for_a_previous_selection_is_discarded(self):
        first, second = session('s1'), session('s2')
        self.poller.tick(first, lines=5)
        self.clock.now += 1
        self.pool.run()
        self.poller.tick(second, lines=5)   # selection moved before we collected
        self.assertIsNone(self.poller.current(second))
        self.assertEqual(len(self.pool.jobs), 0)   # paced from the end of the discarded read
        self.clock.now += session_preview.INTERVAL
        self.poller.tick(second, lines=5)
        self.assertEqual(len(self.pool.jobs), 1)
        self.pool.run()
        self.clock.now += 1
        self.poller.tick(second, lines=5)
        self.assertEqual(self.poller.current(second).lines, ['s2 screen'])
        self.assertIsNone(self.poller.current(first))

    def test_a_stale_result_never_replaces_the_current_rows_screen(self):
        first, second = session('s1'), session('s2')
        self.poller = session_preview.Poller(reader=lambda row, lines, tail: [row['session_id']],
                                             pool=self.pool, clock=self.clock)
        self.poller.tick(second, lines=5)
        self.pool.run()
        self.clock.now += 1
        self.poller.tick(second, lines=5)          # s2 shown; an s2 read is now in flight
        self.pool.jobs.clear()
        self.poller._inflight = (session_preview.preview_key(first), self.done(['s1']))
        self.poller.tick(second, lines=5)          # a finished read for s1 arrives while s2 is selected
        self.assertEqual(self.poller.current(second).lines, ['s2'])

    @staticmethod
    def done(value):
        future = Future()
        future.set_result(value)
        return future

    def test_a_new_generation_is_a_new_preview(self):
        row = session()
        self.poller.tick(row, lines=5)
        self.pool.run()
        self.clock.now += 1
        self.poller.tick(row, lines=5)
        self.assertIsNotNone(self.poller.current(row))
        self.assertIsNone(self.poller.current(dict(row, generation=2, room_id='other')))

    def test_nothing_is_read_for_ended_rows_or_sections(self):
        for row in (session(lifecycle='closed'), session(lifecycle='stopped'), session(transport='room',
                    lifecycle='ended'), {'kind': 'section', 'section': 'x'}, None,
                    session(room_id=None)):
            with self.subTest(row=row):
                self.poller.tick(row, lines=5)
        self.assertEqual(self.pool.jobs, [])
        preview = self.poller.current(session(lifecycle='closed'))
        self.assertEqual(preview.source, 'ended')

    def test_a_failed_read_is_a_note_not_a_crash(self):
        def failing(row, lines, tail):
            raise pane_peek.PeekRefused('pane ownership mismatch')
        poller = session_preview.Poller(reader=failing, pool=self.pool, clock=self.clock)
        row = session()
        poller.tick(row, lines=5)
        self.pool.run()
        self.clock.now += 1
        poller.tick(row, lines=5)
        self.assertIn('ownership', poller.current(row).note)

    def test_the_qa17_late_worker_sequence(self):
        started = []
        poller = session_preview.Poller(reader=lambda row, lines, tail: started.append(self.clock.now) or ['x'],
                                        pool=self.pool, clock=self.clock)
        row = session()
        poller.tick(row, lines=5)            # submitted at 100.00
        self.clock.now = 100.49
        self.pool.run()                      # the capture actually runs at 100.49
        self.clock.now = 100.50
        poller.tick(row, lines=5)
        self.assertEqual(self.pool.jobs, [])  # the next read waits for the last one's end + 500 ms
        self.clock.now = 100.98
        poller.tick(row, lines=5)
        self.assertEqual(self.pool.jobs, [])
        self.clock.now = 101.0
        poller.tick(row, lines=5)
        self.pool.run()
        self.assertEqual(started, [100.49, 101.0])

    def test_a_configured_hook_shows_the_disabled_note(self):
        def disabled(row, lines, tail):
            raise pane_peek.PeekDisabled('after-capture-pane')
        poller = session_preview.Poller(reader=disabled, pool=self.pool, clock=self.clock)
        row = session()
        poller.tick(row, lines=5)
        self.pool.run()
        self.clock.now += 1
        poller.tick(row, lines=5)
        self.assertEqual(poller.current(row).note, 'Preview disabled: tmux after-capture-pane hook configured')

    def test_a_read_finishing_after_close_is_ignored_and_nothing_new_starts(self):
        # Q17-F3: close cancels the in-flight read's publication and stops scheduling.
        row = session()
        self.poller.tick(row, lines=5)
        self.poller.close()
        self.pool.run()
        self.clock.now += 1
        self.poller.tick(row, lines=5)
        self.assertIsNone(self.poller.current(row))
        self.assertEqual(self.pool.jobs, [])

    def test_a_hung_read_does_not_hold_interpreter_exit_after_close(self):
        # Q17-F3: QA measured an 8 s ownership stall holding exit after close returned in 0.27 s.
        script = ('import sys, time, threading\n'
                  'sys.path.insert(0, %r)\n'
                  'from lib.control import session_preview\n'
                  'gate = threading.Event()\n'
                  'def hung(row, lines, tail):\n'
                  '    gate.set(); time.sleep(30)\n'
                  'p = session_preview.Poller(reader=hung)\n'
                  "p.tick({'session_id': 's', 'transport': 'terminal', 'lifecycle': 'open', "
                  "'room_id': 'r', 'generation': 1}, lines=5)\n"
                  'gate.wait(5)\n'
                  'p.close()\n') % str(ROOT)
        import subprocess
        import sys
        import time
        start = time.monotonic()
        subprocess.run([sys.executable, '-c', script], check=True, timeout=60)
        self.assertLess(time.monotonic() - start, 10)

    def test_the_dashboard_reads_off_the_ui_thread(self):
        # Poller defaults to its own single worker thread; tick never runs the reader inline.
        called = []
        poller = session_preview.Poller(reader=lambda *a: called.append(1) or [], clock=self.clock)
        try:
            with mock.patch.object(poller._pool, 'submit', wraps=poller._pool.submit) as submit:
                poller.tick(session(), lines=5)
                self.assertEqual(submit.call_count, 1)
        finally:
            poller.close()


class PreviewRenderTests(unittest.TestCase):
    def data(self, preview, **row_changes):
        row = dict(session(), name='issue-102', project_name='asha', harness='claude', profile='worker',
                   reason='Observed', next_step='Working', group='current', created_at=1.0, updated_at=2.0,
                   **row_changes)
        return {'rows': [row], 'summary': '1 working', 'errors': [], 'grouping': 'project', 'now': 3.0,
                'preview': preview}

    def captured(self, lines):
        return session_preview.Preview(key=('s1',), lines=lines, captured_at=1_700_000_000.0, source='pane',
                                       note='')

    def test_wide_panel_shows_the_capture_read_only_marker_and_timestamp(self):
        text = '\n'.join(session_layout.plain(session_layout.render(
            self.data(self.captured(['• Ran rg -n updated_at', 'Allow command?  make lint'])),
            width=140, height=30)))
        self.assertIn('pane, read-only', text)
        self.assertIn('Allow command?  make lint', text)
        self.assertRegex(text, r'captured \d\d:\d\d:\d\d')

    def test_narrow_peek_shows_the_capture_full_width(self):
        text = '\n'.join(session_layout.plain(session_layout.render(
            self.data(self.captured(['hello pane'])), width=80, height=24, peek=True)))
        self.assertIn('hello pane', text)
        self.assertIn('pane, read-only', text)

    def test_capture_lines_clip_by_cells_and_show_the_newest(self):
        lines = [f'old {i}' for i in range(100)] + ['工程' * 200]
        screen = session_layout.render(self.data(self.captured(lines)), width=140, height=30)
        for spans in screen:
            for x, part, *_ in spans:
                self.assertLessEqual(x + session_layout.cells(part), 140)
        text = '\n'.join(session_layout.plain(screen))
        self.assertIn('工程', text)
        self.assertNotIn('old 0\n', text + '\n')

    def test_escape_bytes_never_reach_a_span(self):
        preview = self.captured(['\x1b]52;c;ZXZpbA==\x07x\x1b[2J\x1b]2;t\x07'])
        for width, peek in ((140, False), (80, True)):
            screen = session_layout.render(self.data(preview), width=width, height=24, peek=peek)
            for spans in screen:
                for _, part, *_ in spans:
                    self.assertNotRegex(part, r'[\x00-\x1f\x7f-\x9f]')

    def test_pending_ended_and_failed_previews_say_so(self):
        cases = ((None, 'Capturing'),
                 (session_preview.Preview(('s1',), [], None, 'ended', 'Session ended; no live screen'),
                  'no live screen'),
                 (session_preview.Preview(('s1',), [], None, 'pane', 'Preview unavailable: pane ownership'),
                  'Preview unavailable'))
        for preview, expected in cases:
            with self.subTest(expected=expected):
                text = '\n'.join(session_layout.plain(session_layout.render(self.data(preview), width=140,
                                                                              height=30)))
                self.assertIn(expected, text)

    def test_without_preview_data_the_panel_is_unchanged(self):
        data = self.data(None)
        del data['preview']
        text = '\n'.join(session_layout.plain(session_layout.render(data, width=140, height=30)))
        self.assertNotIn('read-only', text)


class DashboardWiringTests(unittest.TestCase):
    """The dashboard ticks the poller only for the selected row and only while the preview shows."""

    def run_dashboard(self, keys, size):
        from tests.python.test_control_session_dashboard import row as hub_row
        from tests.python.test_control_session_dashboard_keys import run
        from lib.control import session_tui
        poller = mock.MagicMock()
        poller.current.return_value = None
        rows = [hub_row('a', room_id=ROOM), hub_row('b', room_id=ROOM)]
        with mock.patch.object(session_tui.session_preview, 'Poller', return_value=poller):
            _, painted = run(keys, rows, size=size, config=PREVIEW_ON)
        return poller, painted

    def test_wide_screen_ticks_the_selected_row_with_the_panel_height(self):
        poller, painted = self.run_dashboard([258], size=(30, 140))
        ticked = [call.args[0]['session_id'] for call in poller.tick.call_args_list if call.args[0]]
        self.assertEqual(ticked[-1], 'b')
        self.assertTrue(all(call.kwargs['lines'] >= 1 for call in poller.tick.call_args_list))
        self.assertIn('preview', painted[-1][0])
        poller.close.assert_called_once()

    def test_narrow_list_never_reads_until_space_opens_the_peek(self):
        poller, painted = self.run_dashboard([], size=(24, 100))
        poller.tick.assert_not_called()
        self.assertNotIn('preview', painted[-1][0])
        poller, painted = self.run_dashboard([ord(' ')], size=(24, 100))
        self.assertEqual(poller.tick.call_args_list[-1].args[0]['session_id'], 'a')
        self.assertIn('preview', painted[-1][0])

    def test_the_key_sheet_pauses_reads(self):
        poller, _ = self.run_dashboard([ord('?')], size=(30, 140))
        count = poller.tick.call_count
        poller2, _ = self.run_dashboard([ord('?'), 258, 258], size=(30, 140))
        self.assertEqual(poller2.tick.call_count, count)

    def test_wide_screen_with_the_panel_hidden_reads_nothing_new(self):
        poller, _ = self.run_dashboard([ord(' ')], size=(30, 140))
        count = poller.tick.call_count
        poller2, _ = self.run_dashboard([ord(' '), 258, 258], size=(30, 140))
        self.assertEqual(poller2.tick.call_count, count)


class PreviewPagingTests(unittest.TestCase):
    """#106 (QA17 Q17-F5, design §4.2): PgUp/PgDn scroll back through the bounded capture."""

    PGUP, PGDN, DOWN = 339, 338, 258

    def run_dashboard(self, keys, *, size=(30, 140), lines=100, config=None):
        from tests.python.test_control_session_dashboard import row as hub_row
        from tests.python.test_control_session_dashboard_keys import run
        from lib.control import session_tui
        poller = mock.MagicMock()
        poller.current.return_value = session_preview.Preview(
            ('a',), [f'line {i}' for i in range(lines)], 1_700_000_000.0, 'pane', '')
        rows = [hub_row('a', room_id=ROOM), hub_row('b', room_id=ROOM)]
        with mock.patch.object(session_tui.session_preview, 'Poller', return_value=poller):
            _, painted = run(keys, rows, size=size, config=PREVIEW_ON if config is None else config)
        return poller, painted

    @staticmethod
    def shown(painted):
        snap = painted[-1][0]
        return snap['preview'].lines, snap.get('preview_back', 0)

    def test_pgup_scrolls_back_and_pgdn_returns_to_the_newest_line(self):
        _, painted = self.run_dashboard([self.PGUP])
        lines, back = self.shown(painted)
        self.assertGreater(back, 0)
        self.assertEqual(lines[-1], f'line {99 - back}')
        _, painted = self.run_dashboard([self.PGUP, self.PGDN])
        self.assertEqual(self.shown(painted), ([f'line {i}' for i in range(100)], 0))

    def test_scrolling_back_reads_the_bounded_history(self):
        poller, _ = self.run_dashboard([])
        self.assertLess(poller.tick.call_args.kwargs['lines'], session_preview.MAX_LINES)
        poller, _ = self.run_dashboard([self.PGUP])
        self.assertEqual(poller.tick.call_args.kwargs['lines'], session_preview.MAX_LINES)

    def test_scrolling_stops_at_the_oldest_captured_page(self):
        _, painted = self.run_dashboard([self.PGUP] * 30)
        lines, back = self.shown(painted)
        self.assertEqual(lines[0], 'line 0')
        self.assertGreater(len(lines), 1)
        _, again = self.run_dashboard([self.PGUP] * 31)
        self.assertEqual(self.shown(again), (lines, back))

    def test_a_short_capture_that_fits_does_not_scroll(self):
        _, painted = self.run_dashboard([self.PGUP], lines=3)
        self.assertEqual(self.shown(painted), (['line 0', 'line 1', 'line 2'], 0))

    def test_a_new_selection_starts_at_the_newest_line(self):
        _, painted = self.run_dashboard([self.PGUP, self.DOWN])
        self.assertEqual(self.shown(painted)[1], 0)

    def test_the_narrow_peek_scrolls_too(self):
        _, painted = self.run_dashboard([ord(' '), self.PGUP], size=(24, 100))
        self.assertGreater(self.shown(painted)[1], 0)

    def test_paging_keys_do_nothing_with_the_preview_off(self):
        from tests.python.test_control_session_dashboard import row as hub_row
        from tests.python.test_control_session_dashboard_keys import run
        _, painted = run([self.PGUP, self.PGDN], [hub_row('a'), hub_row('b')], size=(30, 140))
        self.assertNotIn('preview', painted[-1][0])
        self.assertNotIn('preview_back', painted[-1][0])

    def test_the_panel_says_how_far_back_it_is(self):
        row = dict(session(), name='issue-102', project_name='asha', harness='claude', profile='worker',
                   reason='Observed', next_step='Working', group='current', created_at=1.0, updated_at=2.0)
        preview = session_preview.Preview(('s1',), ['older'], 1_700_000_000.0, 'pane', '')
        data = {'rows': [row], 'summary': '', 'errors': [], 'grouping': 'project', 'now': 3.0,
                'preview': preview, 'preview_back': 12}
        text = '\n'.join(session_layout.plain(session_layout.render(data, width=140, height=30)))
        self.assertIn('12 lines back', text)
        self.assertIn('PgDn', text)


PREVIEW_ON = SimpleNamespace(session_preview=True)


class SessionPreviewSettingTests(unittest.TestCase):
    """control.session_preview: off by default, parsed like the sibling control settings."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        self.env = {'HOME': str(root / 'home'), 'ASHA_CONFIG': str(root / 'config.json'),
                    'ASHA_HOME': str(root / 'asha'), 'XDG_RUNTIME_DIR': str(root / 'runtime')}
        for key in ('HOME', 'ASHA_HOME', 'XDG_RUNTIME_DIR'):
            Path(self.env[key]).mkdir(mode=0o700)

    def write(self, control):
        path = Path(self.env['ASHA_CONFIG'])
        path.write_text(json.dumps({'control': control}))
        path.chmod(0o600)

    def test_the_setting_defaults_off(self):
        self.write({})
        config = load_config(self.env)
        self.assertIs(config.session_preview, False)
        self.assertFalse(session_preview.enabled(config))

    def test_true_enables_it_alongside_the_sibling_settings(self):
        self.write({'session_preview': True, 'idle_delivery': False, 'no_handoff_close': False})
        config = load_config(self.env)
        self.assertIs(config.session_preview, True)
        self.assertTrue(session_preview.enabled(config))

    def test_a_non_boolean_is_refused_like_the_siblings(self):
        for value in ('yes', 1, 0, None, 'true'):
            with self.subTest(value=value):
                self.write({'session_preview': value})
                with self.assertRaisesRegex(ConfigError, r'control\.session_preview must be true or false'):
                    load_config(self.env)

    def test_only_a_literal_true_enables_a_config_object(self):
        for config in (object(), SimpleNamespace(session_preview=1), SimpleNamespace(session_preview='true')):
            self.assertFalse(session_preview.enabled(config))


class InlinePool:
    """Runs each preview read at submission, so a dashboard pass issues its tmux calls synchronously."""

    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:  # noqa: BLE001
            future.set_exception(exc)
        return future

    def shutdown(self, **kwargs):
        pass


class DisabledPreviewDashboardTests(unittest.TestCase):
    """With control.session_preview off the dashboard issues no preview tmux call and shows no panel."""

    KEYS = {'wide': ([258, 259, ord(' '), ord(' '), 258], (30, 140)),
            'narrow peek': ([ord(' '), 258, 27, ord(' ')], (24, 100))}

    def run_dashboard(self, config, keys, size):
        from tests.python.test_control_session_dashboard import row as hub_row
        from tests.python.test_control_session_dashboard_keys import run
        spy = FakeTmuxRunner()
        hub = mock.MagicMock()
        hub.tmux = TmuxAdapter(runner=spy)
        rows = [hub_row('a', room_id=ROOM), hub_row('b', room_id=ROOM),
                hub_row('c', transport='structured', room_id=None)]
        with mock.patch.object(session_preview, 'DaemonPool', InlinePool), \
             mock.patch('lib.control.rooms.RoomStore') as rooms, \
             mock.patch('lib.control.session_store.SessionStore') as store:
            rooms.return_value.read.return_value = record()
            _, painted = run(keys, rows, size=size, hub=hub, config=config)
        return spy, rooms, store, painted

    def test_off_issues_no_capture_or_hook_read_and_paints_no_preview(self):
        for name, (keys, size) in self.KEYS.items():
            for config in (object(), SimpleNamespace(session_preview=False)):
                with self.subTest(case=name, config=config):
                    spy, rooms, store, painted = self.run_dashboard(config, keys, size)
                    self.assertEqual(spy.calls, [])
                    rooms.return_value.read.assert_not_called()
                    store.assert_not_called()
                    self.assertTrue(painted)
                    for snap, _ in painted:
                        self.assertNotIn('preview', snap)
                    text = '\n'.join('\n'.join(session_layout.plain(session_layout.render(
                        snap, width=size[1], height=size[0], peek=kw.get('peek', False),
                        preview=kw.get('preview', True)))) for snap, kw in painted)
                    self.assertNotIn('read-only', text)
                    self.assertNotIn('Capturing', text)

    def test_on_reads_through_the_same_seam_so_the_spy_discriminates(self):
        for name, (keys, size) in self.KEYS.items():
            with self.subTest(case=name):
                spy, rooms, _, painted = self.run_dashboard(PREVIEW_ON, keys, size)
                verbs = [call[0] for call in spy.calls]
                self.assertIn('capture-pane', verbs)
                self.assertIn('show-hooks', verbs)
                self.assertTrue(set(verbs) <= READ_ONLY_VERBS)
                self.assertTrue(any('preview' in snap for snap, _ in painted))

    def test_the_key_sheet_describes_space_by_the_setting(self):
        from lib.control import session_tui
        off = '\n'.join(session_tui.key_sheet())
        on = '\n'.join(session_tui.key_sheet(preview=True))
        self.assertNotIn('live screen', off)
        self.assertIn('live screen', on)
        self.assertEqual(len(session_tui.key_sheet()), len(session_tui.key_sheet(preview=True)))
        sheet = '\n'.join(session_tui.lines({'rows': [], 'session_preview': True}, width=200, keys=True))
        self.assertIn('live screen', sheet)
        self.assertNotIn('live screen', '\n'.join(session_tui.lines({'rows': []}, width=200, keys=True)))


if __name__ == '__main__':
    unittest.main()
