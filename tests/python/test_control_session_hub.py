import contextlib
import io
import unittest
import os
import json
import shutil
import sys
import time
import uuid
from unittest import mock

from lib.control.config import load_config
from lib.control.store import StoreError
from tests.python import test_control_rooms as rooms_fixture


class SessionHubTests(unittest.TestCase):
    def setUp(self):
        rooms_fixture.RoomTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.config = load_config(self.env)
        from lib.control.session_hub import Hub
        self.hub = Hub(self.config, env=self.env, tmux=self.tmux)
        self.supervisor = self.enterContext(mock.patch(
            'lib.control.orchestration.supervisor_daemon.start_supervisor',
            return_value=({'message': 'started'}, 0)))

    def launch(self, **changes):
        return self.hub.launch(project=str(self.project), prompt='Trim the games',
                               name='Termart cleanup', harness='claude', **changes)

    def test_launch_is_project_bound_without_initiative_and_idempotent(self):
        row = self.launch()
        self.assertEqual(row['profile'], 'worker')
        self.assertEqual(row['transport'], 'terminal')
        self.assertEqual(len(self.tmux.created), 1)
        env = self.tmux.created[0]['environment']
        self.assertEqual(env['ASHA_SESSION_PROFILE'], 'worker')
        self.assertEqual(env['ASHA_HUB_SESSION_ID'], row['session_id'])
        again = self.launch(session_id=row['session_id'])
        self.assertEqual(again['session_id'], row['session_id'])
        self.assertEqual(len(self.tmux.created), 1)
        with self.hub.database() as db, db.transaction() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM sqlite_master WHERE name='managed_sessions'").fetchone())

    def test_queued_message_is_not_claimed_delivered(self):
        row = self.launch()
        message = self.hub.send(row['session_id'], 'Keep the clock', key='followup')
        self.assertEqual(message['state'], 'queued')
        # delivery/delivery_detail describe each call; the retained message is identical.
        retained = lambda value: {k: v for k, v in value.items() if not k.startswith('delivery')}
        again = self.hub.send(row['session_id'], 'Keep the clock', key='followup')
        self.assertEqual(retained(again), retained(message))
        self.assertEqual(message['delivery'], 'queued-until-read')
        self.assertEqual(again['delivery'], 'retained')
        with self.assertRaises(StoreError):
            self.hub.send(row['session_id'], 'Different', key='followup')
        self.assertEqual(len(self.tmux.respawned), 1)

    def test_missing_telemetry_does_not_stop_or_complete_work(self):
        row = self.launch()
        observed = self.hub.show(row['session_id'])
        self.assertEqual(observed['activity'], 'unknown')
        self.assertEqual(self.tmux.killed, [])

    def test_finished_process_exit_is_distinct_from_unreported_exit(self):
        row = self.launch()
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(row['session_id'])):
            self.hub.observe(None, state='finished', body='Memory saved and work landed')
        self.tmux.sessions.clear()
        ended = self.hub.show(row['session_id'])
        self.assertEqual(ended['process_state'], 'ended')
        self.assertEqual(ended['next_step'], 'Done: close record')
        self.assertEqual(ended['group'], 'ended')
        self.assertIsNone(ended['memory_saved_at'], 'worker prose cannot manufacture a save receipt')
        self.hub._update(row['session_id'], activity='exited', activity_source='hook',
                         assignment_epoch='new-assignment')
        unreported = self.hub.show(row['session_id'])
        self.assertEqual(unreported['next_step'], 'Ended unreported: check work')

    def test_dashboard_save_uses_current_receipt_and_force_close_keeps_it_visible(self):
        from lib.control.session_closure import memory_v2
        row = self.launch(profile='room')
        with mock.patch.dict(os.environ, {'ASHA_HUB_SESSION_ID': row['session_id']}), mock.patch(
                'lib.control.session_publication.publication_actor', return_value=(self.hub, row)):
            memory_v2.publish(self.project, memory_v2.ACTIVE_TEMPLATE, memory_v2.DECISIONS_TEMPLATE)
        shown = self.hub.show(row['session_id'])
        self.assertIsNotNone(shown['memory_saved_at'])
        closed = self.hub.close(row['session_id'], force=True)
        self.assertIn('Memory saved', closed['reason'])
        self.assertNotIn('no project-memory handoff was claimed', closed['reason'])
        self.hub._update(row['session_id'], assignment_epoch='later-assignment')
        self.assertIsNone(self.hub.show(row['session_id'])['memory_saved_at'])

    def test_close_missing_session_preserves_record_and_messages(self):
        row = self.launch()
        self.hub.send(row['session_id'], 'Retain me', key='one')
        self.tmux.sessions.clear()
        self.hub.close(row['session_id'])
        self.assertEqual(self.hub.get(row['session_id'])['lifecycle'], 'closed')
        self.assertEqual(len(self.hub.messages(row['session_id'])), 1)
        # No agent was left to hand off memory: the close stays visible until acknowledged.
        self.assertEqual([r['activity'] for r in self.hub.list()['rows']], ['close-failed'])
        self.hub.close(row['session_id'], force=True)
        self.assertEqual(self.hub.list()['rows'], [])

    def test_stop_keeps_history_and_resume_keeps_session_identity(self):
        row = self.launch()
        self.hub._update(row['session_id'], background_tasks=3)
        self.hub.stop(row['session_id'])
        resumed = self.hub.resume(row['session_id'], prompt='Continue with my followup')
        self.assertIsNone(self.hub.get(row['session_id']).get('background_tasks'))
        self.assertEqual(resumed['session_id'], row['session_id'])
        self.assertEqual(resumed['generation'], 2)
        self.assertNotEqual(resumed['room_id'], row['room_id'])

    def test_close_refuses_reused_pane(self):
        row = self.launch()
        self.tmux.session_identity = '$99'
        with self.assertRaises(ValueError):
            self.hub.close(row['session_id'])
        self.assertEqual(self.hub.get(row['session_id'])['lifecycle'], 'open')
        self.assertEqual(self.tmux.killed, [])

    def test_repeated_friendly_names_create_distinct_sessions(self):
        first = self.launch()
        # The shared Room fake models one pane at a time; a graceful close would
        # leave the first agent running, so end it explicitly.
        self.hub.close(first['session_id'], force=True)
        second = self.launch()
        self.assertEqual(first['name'], second['name'])
        self.assertNotEqual(first['session_id'], second['session_id'])
        self.assertEqual(len(self.tmux.created), 2)

    def test_explicit_result_survives_report_command_hooks(self):
        row = self.launch()
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(row['session_id'])):
            self.hub.observe(None, state='finished', body='Games removed')
            for event in ('tool-completed', 'turn-stopped', 'session-ended'):
                self.hub.observe(event)
            retained = self.hub.show(row['session_id'])
            self.assertEqual(retained['activity'], 'finished')
            self.assertEqual(retained['result'], 'Games removed')
            self.hub.observe('prompt-submitted')
            self.assertEqual(self.hub.show(row['session_id'])['activity'], 'working')

    def test_explicit_question_survives_report_command_hooks(self):
        row = self.launch()
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(row['session_id'])):
            self.hub.observe(None, state='needs-input', body='Keep the clock?')
            self.hub.observe('tool-completed')
            self.hub.observe('turn-stopped')
        self.assertEqual(self.hub.show(row['session_id'])['question'], 'Keep the clock?')
        self.assertEqual(self.hub.show(row['session_id'])['activity'], 'needs-input')

    def test_native_permission_clears_after_tool_completion(self):
        row = self.launch()
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(row['session_id'])):
            self.hub.observe('permission-requested')
            self.assertEqual(self.hub.show(row['session_id'])['activity'], 'needs-input')
            self.hub.observe('tool-completed')
            self.assertEqual(self.hub.show(row['session_id'])['activity'], 'working')
            self.hub.observe('turn-stopped')
            self.assertEqual(self.hub.show(row['session_id'])['activity'], 'idle')

    def test_stop_with_background_work_outstanding_is_not_idle(self):
        row = self.launch()
        sid = row['session_id']
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('prompt-submitted')
            self.hub.observe('turn-stopped', background_tasks=2)
            waiting = self.hub.show(sid)
            self.assertEqual((waiting['activity'], waiting['native_activity']), ('working', 'working'))
            self.assertEqual(waiting['background_tasks'], 2)
            self.assertIn('2 background task(s)', waiting['reason'])
            self.assertEqual(waiting['next_step'], 'Working: background tasks')
            # A long suite outlives the five-minute staleness rule: the Stop
            # payload proved a wake-up is still owed, so it is not demoted,
            # up to a bound: an unproven wake-up cannot hold the row forever.
            from lib.control.session_hub import BACKGROUND_WAIT_SECONDS
            self.hub._update(sid, observed_at=time.time() - 3600, native_observed_at=time.time() - 3600)
            self.assertEqual(self.hub.show(sid)['activity'], 'working')
            late = time.time() - BACKGROUND_WAIT_SECONDS - 60
            self.hub._update(sid, observed_at=late, native_observed_at=late)
            self.assertEqual(self.hub.show(sid)['activity'], 'unknown')
            # A worker report is fresher evidence and drops the stale count.
            self.hub.observe(None, state='working', body='Still testing')
            self.assertIsNone(self.hub.get(sid).get('background_tasks'))
            self.hub.observe('turn-stopped', background_tasks=2)
            # The wake-up turn's own Stop with nothing outstanding is idle.
            self.hub.observe('tool-started', tool_token='t1')
            self.assertIsNone(self.hub.get(sid).get('background_tasks'))
            self.hub.observe('tool-completed', tool_token='t1')
            self.hub.observe('turn-stopped', background_tasks=0)
            idle = self.hub.show(sid)
            self.assertEqual(idle['activity'], 'idle')
            self.assertIsNone(idle.get('background_tasks'))
            self.hub.observe('turn-stopped', background_tasks=1)
            self.hub.observe('session-ended')
            self.assertEqual(self.hub.get(sid)['activity'], 'exited')
            with self.assertRaises(StoreError):
                self.hub.observe('turn-stopped', background_tasks=-1)

    def test_explicit_input_state_without_text_survives_its_own_hooks(self):
        row = self.launch()
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(row['session_id'])):
            self.hub.observe(None, state='needs-input')
            self.hub.observe('tool-completed')
            self.hub.observe('turn-stopped')
        self.assertEqual(self.hub.show(row['session_id'])['activity'], 'needs-input')

    def test_report_does_not_overwrite_successor_generation(self):
        row = self.launch()
        self.hub.stop(row['session_id'])
        self.hub.resume(row['session_id'], prompt='Continue')
        with mock.patch.object(self.hub, 'actor', return_value=row), self.assertRaisesRegex(StoreError, 'stale'):
            self.hub.observe(None, state='finished', body='Old result')
        self.assertIsNone(self.hub.get(row['session_id'])['result'])

    def test_environment_labels_alone_cannot_report(self):
        row = self.launch()
        self.hub.env.update(ASHA_HUB_SESSION_ID=row['session_id'], ASHA_HUB_GENERATION='1')
        with mock.patch('lib.control.harness.caller_descends_from', return_value=False), self.assertRaises(StoreError):
            self.hub.observe(None, state='finished', body='Spoofed')

    def test_first_native_conversation_binds_the_generation(self):
        # A hook run by a shared Codex daemon, or any other conversation under
        # the same pane, inherits this session's identity (#100). The first
        # native conversation of a generation binds it; ordinary events from a
        # different conversation are refused whatever their cwd.
        row = self.launch()
        sid = row['session_id']
        project = self.project.resolve()
        (project / 'sub').mkdir(exist_ok=True)
        outside = self.root / 'elsewhere'
        outside.mkdir(exist_ok=True)
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('prompt-submitted', native_id='thread-own', cwd=str(project))
            for event, cwd in (('turn-stopped', str(project)), ('tool-started', None),
                               ('permission-requested', str(project / 'sub')), ('tool-completed', str(outside))):
                with self.subTest(event=event, cwd=cwd), \
                        self.assertRaisesRegex(StoreError, 'another native conversation'):
                    self.hub.observe(event, native_id='thread-foreign', cwd=cwd)
            current = self.hub.get(sid)
            self.assertEqual((current['activity'], current['native_id']), ('working', 'thread-own'))
            # The bound conversation may report from anywhere (Claude's hook
            # cwd follows Bash cd and EnterWorktree), or without an ID at all.
            self.hub.observe('tool-started', native_id='thread-own', cwd=str(outside), tool_token='t')
            self.hub.observe('tool-completed', native_id='thread-own', cwd=str(outside), tool_token='t')
            self.hub.observe('turn-stopped', cwd='relative/ignored')
            self.assertEqual(self.hub.get(sid)['activity'], 'idle')
            # An explicit new conversation (/clear, /new) rebinds inside the
            # project; a foreign SessionStart from another project does not.
            with self.assertRaisesRegex(StoreError, 'outside this session'):
                self.hub.observe('session-start', native_id='thread-far', cwd=str(outside))
            self.hub.observe('session-start', native_id='thread-next', cwd=str(project / 'sub'))
            with self.assertRaisesRegex(StoreError, 'another native conversation'):
                self.hub.observe('turn-stopped', native_id='thread-own')
            self.hub.observe('turn-stopped', native_id='thread-next')
            self.assertEqual(self.hub.get(sid)['native_id'], 'thread-next')
            with self.assertRaises(StoreError):
                self.hub.observe(None, state='working', cwd=str(outside))
        # Resume is a new generation: its first conversation binds afresh.
        self.hub.stop(sid)
        self.hub.resume(sid, prompt='Continue')
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('prompt-submitted', native_id='thread-resumed')
            self.assertEqual(self.hub.get(sid)['native_id'], 'thread-resumed')
            with self.assertRaisesRegex(StoreError, 'another native conversation'):
                self.hub.observe('turn-stopped', native_id='thread-next')

    def test_unbound_generation_refuses_a_foreign_first_event_from_outside(self):
        row = self.launch()
        outside = self.root / 'elsewhere'
        outside.mkdir(exist_ok=True)
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(row['session_id'])):
            with self.assertRaisesRegex(StoreError, 'outside this session'):
                self.hub.observe('turn-stopped', native_id='thread-foreign', cwd=str(outside))
            self.assertIsNone(self.hub.get(row['session_id'])['native_id'])
            self.hub.observe('session-start', native_id='thread-own', cwd=str(self.project.resolve()))
            self.assertEqual(self.hub.get(row['session_id'])['native_binding'],
                             {'generation': 1, 'native_id': 'thread-own'})

    def test_permission_request_shows_needs_input_with_its_request_text(self):
        # Answering a terminal approval through Control is out of scope (#101);
        # the request must still be visible in Control, not only in the pane.
        row = self.launch()
        text = 'Permission requested: Bash: make clean'
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(row['session_id'])):
            self.hub.observe('prompt-submitted', native_id='thread-own')
            self.hub.observe('permission-requested', native_id='thread-own', body=text)
        shown = self.hub.show(row['session_id'])
        self.assertEqual(shown['activity'], 'needs-input')
        self.assertEqual(shown['question'], text)
        self.assertIn('make clean', shown['reason'])
        self.assertEqual(shown['next_step'], 'Answer in terminal (attach)')

    def test_terminal_session_without_hook_evidence_is_labelled_hooks_not_reporting(self):
        from lib.control import session_hub
        row = self.launch()
        fresh = self.hub.show(row['session_id'])
        self.assertEqual(fresh['activity'], 'unknown')
        self.assertNotEqual(fresh.get('telemetry'), 'hooks-not-reporting')
        self.assertIsNotNone(self.hub.get(row['session_id'])['launched_at'])
        self.hub._update(row['session_id'], launched_at=time.time() - session_hub.HOOK_SILENCE_SECONDS - 5)
        silent = self.hub.show(row['session_id'])
        self.assertEqual(silent['activity'], 'unknown')
        self.assertEqual(silent['telemetry'], 'hooks-not-reporting')
        self.assertIn('Hooks not reporting', silent['reason'])
        self.assertEqual(silent['next_step'], 'Hooks not reporting: attach')
        # A worker report is not hook evidence; one native event is.
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(row['session_id'])):
            self.hub.observe(None, state='working', body='Started')
            self.assertEqual(self.hub.show(row['session_id'])['telemetry'], 'hooks-not-reporting')
            self.hub.observe('prompt-submitted')
        observed = self.hub.show(row['session_id'])
        self.assertEqual(observed['activity'], 'working')
        self.assertIsNone(observed.get('telemetry'))

    def test_harnesses_without_a_hook_bridge_are_never_labelled_hooks_not_reporting(self):
        from lib.control import session_hub
        row = self.launch()
        for harness in ('copilot', 'opencode'):
            with self.subTest(harness=harness):
                self.hub._update(row['session_id'], harness=harness,
                                 launched_at=time.time() - session_hub.HOOK_SILENCE_SECONDS - 5)
                shown = self.hub.show(row['session_id'])
                self.assertIsNone(shown.get('telemetry'))
                self.assertNotEqual(shown['next_step'], 'Hooks not reporting: attach')

    def test_rejected_hook_event_is_logged_to_a_bounded_local_diagnostic(self):
        from lib.control import hub_cli, session_hub
        row = self.launch()
        env = dict(self.env, ASHA_HUB_SESSION_ID=row['session_id'], ASHA_HUB_GENERATION='7')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = hub_cli.dispatch(['event', '--event', 'turn-stopped', '--native-id', 'thread-x',
                                     '--cwd', '/elsewhere/project'], env=env)
        self.assertEqual((code, out.getvalue().strip()), (0, '{}'))
        log = session_hub.rejection_log_path(self.config)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        entry = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual((entry['event'], entry['session_id'], entry['generation'], entry['native_id']),
                         ('turn-stopped', row['session_id'], '7', 'thread-x'))
        self.assertIn('stale or inactive session reporter', entry['error'])
        self.assertEqual(entry['cwd'], '/elsewhere/project')
        for _ in range(3000):
            session_hub.record_rejection(self.config, env, event='tool-started', native_id='n' * 200,
                                         error='x' * 2000, cwd='/' + 'c' * 900)
        self.assertLessEqual(log.stat().st_size, session_hub.REJECTION_LOG_BYTES)
        self.assertTrue(all(json.loads(line)['event'] for line in log.read_text().splitlines()))

    def test_message_count_and_page_do_not_hide_more_than_100_queued(self):
        row = self.launch()
        # Bulk insertion represents an established inbox, independent of the
        # send API's idempotency test above.
        import uuid
        from lib.control.session_store import digest
        with self.hub.database() as db, db.transaction(write=True) as c:
            c.executemany('INSERT INTO hub_messages VALUES(?,?,?,?,?,?,?)', [
                (str(uuid.uuid4()), row['session_id'], str(i), 'context', digest('context'), 'queued', i)
                for i in range(105)])
        self.assertEqual(self.hub.show(row['session_id'])['pending_messages'], 105)
        first = self.hub.message_page(row['session_id'])
        self.assertFalse(first['complete'])
        self.assertEqual(first['next_offset'], 100)
        second = self.hub.message_page(row['session_id'], offset=100)
        self.assertEqual(len(second['messages']), 5)
        self.assertTrue(second['complete'])

    def test_structured_result_is_retained_without_initiative_or_open_utility(self):
        from lib.control.session_store import SessionStore
        row = self.launch(transport='structured')
        sid = row['session_id']
        with SessionStore(self.config) as sessions:
            session = sessions.claim_owner(sid)
            message = sessions.claim_turn(sid, session['generation'])
            sessions.observe(sid, session['generation'], message['turn_id'], 'completed', {'summary': 'Important email: editor reply'})
            sessions.finish(sid, session['generation'], message['turn_id'], success=True)
            self.assertIsNone(sessions.get(sid)['initiative_id'])
            # This in-process fixture impersonated an owner; model its exit
            # before acting as the operator again.
            with sessions.db.transaction(write=True) as c:
                c.execute('UPDATE managed_sessions SET owner_pid=NULL,owner_identity=NULL WHERE session_id=?', (sid,))
        completed = self.hub.show(sid)
        self.assertEqual(completed['activity'], 'finished')
        self.assertEqual(completed['result'], 'Important email: editor reply')
        self.assertEqual(self.hub.list()['rows'], [])
        self.assertEqual(self.hub.list(include_closed=True)['rows'][0]['session_id'], sid)
        self.hub.send(sid, 'Now summarize that reply', key='followup')
        self.assertNotEqual(self.hub.show(sid)['activity'], 'finished')
        self.assertEqual(self.supervisor.call_count, 2)

    def test_failed_launch_can_resume_after_ownership_check(self):
        sid = str(uuid.uuid4())
        with mock.patch('lib.control.session_hub.open_room', side_effect=ValueError('missing harness')):
            with self.assertRaisesRegex(ValueError, 'missing harness'):
                self.launch(session_id=sid)
        self.assertEqual(self.hub.get(sid)['lifecycle'], 'interrupted')
        resumed = self.hub.resume(sid, prompt='Now installed; continue')
        self.assertEqual(resumed['lifecycle'], 'open')
        self.assertEqual(resumed['generation'], 2)
        self.assertEqual(len(self.tmux.created), 1)

    def test_failed_supervisor_is_reported_with_durable_assignment(self):
        self.supervisor.return_value = ({'message': 'failed to start'}, 1)
        row = self.launch(transport='structured')
        self.assertEqual(row['reason'], 'failed to start')
        self.assertEqual(row['pending_messages'], 1)
        self.supervisor.return_value = ({'message': 'started'}, 0)
        receipt = self.hub.send(row['session_id'], 'More context', key='followup')
        self.assertIsNone(receipt['dispatch_warning'])
        self.assertIsNone(self.hub.get(row['session_id'])['runtime_warning'])

    @unittest.skipUnless(shutil.which('tmux'), 'tmux is required')
    def test_real_terminal_report_message_ack_finish_and_close(self):
        from lib.control.rooms import open_room
        from lib.control.socket_reaper import TmuxSocketReaper
        from lib.control.tmux import TmuxAdapter
        socket = 'asha-hub-test-' + uuid.uuid4().hex[:12]
        self.enterContext(TmuxSocketReaper(socket))
        adapter = TmuxAdapter(socket=socket, config_file=rooms_fixture.Path('/dev/null'))
        if adapter._run_status(['list-commands', 'new-session'])[0]:
            self.skipTest('isolated tmux sockets are unavailable in this execution sandbox')
        self.hub.tmux = adapter
        probe_root = self.root / 'native-probe'
        (probe_root / 'bin').mkdir(parents=True)
        ready, go, done = (self.root / name for name in ('ready', 'go', 'done'))
        probe = probe_root / 'bin' / 'asha'
        probe.write_text(f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, {str(self.asha_root)!r})
from lib.control.config import load_config
from lib.control.session_hub import Hub
from lib.control.tmux import TmuxAdapter
hub = Hub(load_config(os.environ), tmux=TmuxAdapter(socket={socket!r}))
deadline = time.monotonic() + 8
while not Path({str(go)!r}).exists() and time.monotonic() < deadline: time.sleep(.02)
hub.observe(None, state='needs-input', body='Keep the clock?', native_id='native-test')
Path({str(ready)!r}).write_text(json.dumps(dict(profile=os.environ['ASHA_SESSION_PROFILE'], cwd=os.getcwd())))
while time.monotonic() < deadline:
    messages = hub.messages(os.environ['ASHA_HUB_SESSION_ID'])
    if messages:
        hub.acknowledge(messages[0]['message_id'])
        hub.observe(None, state='finished', body='Clock retained')
        hub.observe('tool-completed')
        hub.observe('session-ended')
        Path({str(done)!r}).write_text('done')
        break
    time.sleep(.02)
''')
        probe.chmod(0o700)
        def native_launch(**kwargs):
            kwargs.update(asha_root=probe_root, executable_finder=lambda _: sys.executable)
            return open_room(**kwargs)
        with mock.patch('lib.control.session_hub.open_room', side_effect=native_launch):
            row = self.launch()
        self.addCleanup(lambda: self.hub.close(row['session_id']))
        go.touch()
        def await_file(path):
            deadline = time.monotonic() + 5
            while not path.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(path.exists(), 'native probe did not reach ' + path.name)
        await_file(ready)
        self.assertEqual(json.loads(ready.read_text()), {'profile': 'worker', 'cwd': str(self.project)})
        self.assertEqual(self.hub.show(row['session_id'])['activity'], 'needs-input')
        self.hub.send(row['session_id'], 'Yes, keep it', key='answer')
        await_file(done)
        self.assertEqual(self.hub.show(row['session_id'])['activity'], 'finished')
        self.assertEqual(self.hub.messages(row['session_id'])[0]['state'], 'acknowledged')
        self.hub.close(row['session_id'])
        self.assertEqual(self.hub.get(row['session_id'])['result'], 'Clock retained')


class SessionDashboardTests(unittest.TestCase):
    def test_action_feedback_survives_full_list_and_observation_error(self):
        from lib.control.session_tui import lines
        row = dict(session_id='id', activity='unknown', name='Job', project_name='termart', harness='claude')
        rendered = lines({'rows': [row] * 100, 'errors': ['Observation incomplete']},
                         height=24, width=80, message='Queued; not delivered yet')
        self.assertIn('Queued; not delivered yet', rendered)

    def test_help_wraps_without_overrun_or_terminal_escape_sequences(self):
        from lib.control.session_tui import lines
        snapshot = {'summary': '1 session', 'rows': [dict(session_id='id', activity='working',
            name='unsafe\x1b[2Jname', project_name='termart', harness='claude', reason='still working')]}
        for width, height in ((40, 20), (80, 24), (120, 30), (20, 5)):
            with self.subTest(width=width, height=height):
                rendered = lines(snapshot, width=width, height=height)
                self.assertLessEqual(len(rendered), height)
                self.assertTrue(all(len(line) <= width and '\x1b' not in line for line in rendered))
                self.assertIn('q quit', '\n'.join(rendered))
