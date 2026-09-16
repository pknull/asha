"""A terminal-writable title cannot participate in supervision evidence."""
import re
import shutil
import subprocess
import sys
import time
import unittest
import uuid
from unittest import mock

from lib.control.reconcile import Evidence, LiveAdapters, reconcile_task
from lib.control.socket_reaper import TmuxSocketReaper
from lib.control.tmux import TmuxAdapter, TmuxError
from tests.python.test_control_config_model import task_record


class TitleEvidenceTests(unittest.TestCase):
    def adapter(self, title, *, dead='1', status='0', signal='', owner=True, task=None):
        task = task or task_record()
        run = task['runs'][0]
        values = {'pid': '999', 'pane_id': run['pane_id'], 'pane_pid': str(run['pid']),
                  'pane_dead': dead, 'pane_dead_status': status, 'pane_dead_signal': signal,
                  'pane_active': '1', 'session_name': task['tmux']['session'], 'session_id': '$1',
                  'window_name': 'work', 'window_id': '@1', 'pane_title': title,
                  '@asha_managed': '1', '@asha_task_id': task['task_id'] if owner else str(uuid.uuid4()),
                  '@asha_run_id': run['run_id']}

        def runner(argv, **kwargs):
            if 'has-session' in argv:
                output = ''
            elif 'capture-pane' in argv:
                output = 'fixture output\n'
            elif 'show-options' in argv:
                output = values.get(argv[-1], '') + '\n'
            elif '-F' in argv:
                output = re.sub(r'#\{([^}]+)\}', lambda match: values.get(match[1], ''), argv[-1]) + '\n'
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(argv, 0, output.encode(), b'')
        return task, TmuxAdapter(runner=runner)

    def test_hostile_titles_do_not_change_direct_or_inventory_exit_evidence(self):
        for title in ('normal', '', 'path/' * 1000, '\x1b\t\n\r\x00', '\u202e#{pane_pid}'):
            for dead, status, signal, expected in (('1', '0', '', 'exited'),
                                                   ('1', '7', '', 'failed'),
                                                   ('1', '', '9', 'failed'),
                                                   ('0', '', '', 'working')):
                with self.subTest(title=repr(title[:20]), state=expected):
                    task, adapter = self.adapter(title, dead=dead, status=status, signal=signal)
                    for reader in (adapter, adapter.inventory()):
                        live = LiveAdapters(tmux=reader)
                        live.jj = lambda task: Evidence('jj', 'match', 'exact workspace')
                        live.event = lambda task, run: Evidence('event', 'match', 'recent event', state='working')
                        with mock.patch('lib.control.reconcile.harness_api.verify_process', return_value=True):
                            result = reconcile_task(task, live)
                        self.assertEqual(result['state'], expected)
                        self.assertEqual(reader.pane_facts(task['runs'][0]['pane_id']).title, '')

    def test_dead_pane_with_foreign_ownership_still_refuses(self):
        task, adapter = self.adapter('bad\n' * 100, owner=False)
        result = reconcile_task(task, LiveAdapters(tmux=adapter))
        self.assertEqual(result['state'], 'stale')
        self.assertIn('ownership', result['blocker'])

    def test_supervision_formats_exclude_title_and_reject_invalid_exit_fields(self):
        from lib.control.tmux import _PANE_FORMAT, _INVENTORY_FORMAT
        self.assertNotIn('pane_title', _PANE_FORMAT)
        self.assertNotIn('pane_title', _INVENTORY_FORMAT)
        for changes in ({'dead': 'maybe'}, {'status': 'bad'}, {'signal': '-1'}):
            _, adapter = self.adapter('benign', **changes)
            with self.assertRaises(TmuxError):
                adapter.pane_facts('%23')

    @unittest.skipUnless(shutil.which('tmux'), 'tmux unavailable')
    def test_real_dead_pane_with_corrupted_title_remains_observable(self):
        socket = 'asha-title-test-' + uuid.uuid4().hex[:12]
        adapter = TmuxAdapter(socket=socket, config_file='/dev/null')
        with TmuxSocketReaper(socket):
            pane = adapter.create_task_session(session='asha-title-fixture', window='work',
                start_directory='/tmp', environment={}, holder_argv=['/bin/sleep', '30'],
                session_options={}, pane_options={}, pane_title='initial')
            adapter.respawn(pane, [sys.executable, '-c',
                "print(chr(27) + ']0;' + 'Qtemplates/' * 100 + chr(7), end='', flush=True)"])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if adapter._run(['display-message', '-p', '-t', pane, '#{pane_dead}']).strip() == '1':
                    break
                time.sleep(.02)
            # The process itself emits OSC title bytes before its clean exit.
            self.assertGreater(len(adapter._run(['display-message', '-p', '-t', pane, '#{pane_title}'])), 200)
            facts = adapter.pane_facts(pane)
            self.assertTrue(facts.dead)
            self.assertEqual(facts.dead_status, 0)
            self.assertEqual(adapter.inventory().pane_facts(pane).dead_status, 0)
