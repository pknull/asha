"""Issue #92: controller fixtures, not native model delivery proof."""
import os
import json
import uuid
import unittest
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture, ACTIVE, DECISIONS
from lib.control.store import StoreError
from lib.control import session_closure as closure


class CompletionTests(ClosureFixture):
    def save(self, sid):
        import memory_v2
        token = str(uuid.uuid4())
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('tool-started', tool_kind='finalizer', tool_token=token)
        row = self.hub.get(sid)
        before = memory_v2.snapshot_digests(memory_v2.read_published_snapshot(self.project))
        with mock.patch.dict(os.environ, {'ASHA_HUB_SESSION_ID': sid}), mock.patch(
                'lib.control.session_publication.publication_actor', return_value=(self.hub, row)):
            result = memory_v2.publish(self.project, ACTIVE, DECISIONS, expected_preimages=before)
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('tool-completed', tool_kind='finalizer', tool_token=token)
        return result

    def test_save_idle_close_without_turn_attach_or_force(self):
        for harness in ('claude', 'codex'):
            with self.subTest(harness=harness):
                sid = self.launch(harness=harness)['session_id']
                with self.acting_as(sid):
                    self.hub.observe('prompt-submitted')
                    receipt = self.save(sid)
                    self.assertEqual(receipt['completion']['status'], 'ready')
                    self.hub.observe('turn-stopped')
                starts = len(self.tmux.created)
                closed = self.hub.close(sid)
                self.assertEqual(closed['closure']['state'], 'completed')
                self.assertEqual(closed['closure']['delivery']['channel'], 'completion-receipt')
                self.assertEqual(len(self.tmux.created), starts)
                self.assertEqual(self.hub.messages(sid), [])

    def test_no_update_receipt_required_for_explicit_finished_report(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            with self.assertRaisesRegex(StoreError, 'handoff'):
                self.hub.report(state='finished', body='Done')
            result = self.hub.handoff(None, outcome='no-durable-update', detail='Reviewed; nothing durable changed')
            self.assertEqual(result['completion']['status'], 'ready')
            self.hub.report(state='finished', body='Done')
            self.hub.observe('turn-stopped')
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')

    def test_new_prompt_send_and_tool_invalidate(self):
        for event in ('prompt-submitted', 'tool-started', 'send'):
            with self.subTest(event=event):
                sid = self.launch()['session_id']
                with self.acting_as(sid):
                    self.save(sid)
                    if event == 'send':
                        self.hub.send(sid, 'More work', key='more')
                    else:
                        self.hub.observe(event)
                    self.hub.observe('turn-stopped')
                    with self.assertRaisesRegex(StoreError, 'handoff'):
                        self.hub.report(state='finished', body='Old receipt')
                self.assertNotEqual(self.hub.close(sid)['closure']['state'], 'completed')
                self.hub.stop(sid)

    def test_concurrent_memory_update_and_silence_block_readiness(self):
        import memory_v2
        for change in ('publication', 'silence'):
            with self.subTest(change=change):
                sid = self.launch()['session_id']
                self.save(sid)
                if change == 'publication':
                    memory_v2.publish(self.project, ACTIVE, '# Decisions\n\n- Newer.\n')
                else:
                    marker = self.project / 'Work/markers/silence'
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.touch()
                with self.acting_as(sid):
                    self.hub.observe('turn-stopped')
                    with self.assertRaisesRegex(StoreError, 'handoff'):
                        self.hub.report(state='finished', body='Done')
                self.assertNotEqual(self.hub.close(sid)['closure']['state'], 'completed')
                self.hub.stop(sid)

    def test_blocked_is_retained_and_cannot_claim_completion(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            result = self.hub.handoff(None, outcome='blocked', detail='Permission denied')
            self.assertEqual(result['completion']['status'], 'blocked')
            with self.assertRaisesRegex(StoreError, 'handoff'):
                self.hub.report(state='finished', body='Done')

    def test_idle_claude_missing_receipt_requires_attach(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        result = self.hub.close(sid)
        self.assertTrue(result['closure']['attachment_required'])
        self.assertEqual(result['next_step'], 'Close needs attach')

    def test_failed_save_cannot_reuse_previous_receipt(self):
        import memory_v2
        sid = self.launch()['session_id']
        self.save(sid)
        with mock.patch.dict(os.environ, {'ASHA_HUB_SESSION_ID': sid}), mock.patch(
                'lib.control.session_publication.publication_actor', return_value=(self.hub, self.hub.get(sid))):
            with self.assertRaises(ValueError):
                memory_v2.publish(self.project, 'invalid active', DECISIONS)
        with self.acting_as(sid), self.assertRaisesRegex(StoreError, 'handoff'):
            self.hub.report(state='finished', body='No false success')

    def test_no_update_refuses_unavailable_identity_silence_and_scope(self):
        sid = self.launch()['session_id']
        config = self.project / '.asha/config.json'
        original = config.read_text()
        for cause in ('missing', 'project-id', 'silence', 'scope'):
            with self.subTest(cause=cause), self.acting_as(sid):
                if cause == 'missing':
                    config.unlink()
                elif cause == 'project-id':
                    config.write_text(json.dumps(dict(json.loads(original), project_id='different')))
                elif cause == 'silence':
                    marker = self.project / 'Work/markers/silence'
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.touch()
                else:
                    marker = self.project / '.asha/control-task.json'
                    marker.write_text('{}')
                with self.assertRaises(StoreError):
                    self.hub.handoff(None, outcome='no-durable-update', detail='Must refuse')
                self.assertNotEqual(self.hub.get(sid)['completion']['status'], 'ready')
                config.write_text(original)
                if cause in {'silence', 'scope'}:
                    marker.unlink()

    def test_receipt_does_not_survive_resume_or_another_session(self):
        sid = self.launch()['session_id']
        self.save(sid)
        receipt = self.hub.get(sid)['completion']
        self.hub.stop(sid)
        self.hub.resume(sid, prompt='Further work')
        with self.acting_as(sid), self.assertRaisesRegex(StoreError, 'handoff'):
            self.hub.report(state='finished', body='Old generation')
        self.hub.stop(sid)
        other = self.launch()['session_id']
        self.hub._update(other, completion=receipt)
        with self.acting_as(other), self.assertRaisesRegex(StoreError, 'handoff'):
            self.hub.report(state='finished', body='Other actor')

    def test_prompt_during_liveness_probe_prevents_termination(self):
        sid = self.launch()['session_id']
        self.save(sid)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        original = self.hub._live
        def probe(row):
            with self.acting_as(sid):
                self.hub.observe('prompt-submitted')
            return original(row)
        with mock.patch.object(self.hub, '_live', side_effect=probe):
            result = self.hub.close(sid)
        self.assertEqual(self.tmux.killed, [])
        self.assertNotEqual(result['closure']['state'], 'completed')

    def test_old_close_attempt_receipt_is_refused(self):
        sid = self.launch()['session_id']
        close = self.hub.close(sid)['closure']
        with self.acting_as(sid):
            self.hub.handoff(close['request_id'], attempt=1, outcome='no-durable-update', detail='Reviewed')
            old = self.hub.get(sid)['completion']
            self.hub._update(sid, closure=closure.rearm(self.hub.get(sid)['closure']))
            with self.assertRaisesRegex(StoreError, 'attempt'):
                self.hub.handoff(close['request_id'], attempt=1, outcome='no-durable-update', detail='Late')
            self.hub.observe('turn-stopped')
        result = self.hub.close(sid)
        self.assertNotEqual(result['closure']['state'], 'completed')
        self.assertEqual(self.hub.get(sid)['completion'], old)

    def test_copilot_and_opencode_receipt_does_not_invent_native_idle(self):
        for harness in ('copilot', 'opencode'):
            with self.subTest(harness=harness):
                sid = self.launch(harness=harness)['session_id']
                # These harnesses have no native Control tool callbacks.
                with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
                    result = self.hub.handoff(None, outcome='no-durable-update', detail='Reviewed')
                    self.assertEqual(result['completion']['status'], 'blocked')
                    with self.assertRaises(StoreError):
                        self.hub.report(state='finished', body='Done')
                result = self.hub.close(sid)
                self.assertTrue(result['closure']['attachment_required'])
                self.assertEqual(self.tmux.killed, [])
                self.hub.stop(sid)
                self.tmux.killed.clear()

    def test_unread_terminal_work_queued_before_finalizing_is_not_discarded(self):
        sid = self.launch()['session_id']
        message = self.hub.send(sid, 'Further work', key='more')
        with self.acting_as(sid):
            with self.assertRaisesRegex(StoreError, 'queued project work'):
                self.hub.handoff(None, outcome='no-durable-update', detail='Current work done')
            self.hub.observe('turn-stopped')
        self.assertNotEqual(self.hub.close(sid)['closure']['state'], 'completed')
        self.assertEqual(self.tmux.killed, [])
        self.assertEqual(self.hub.messages(sid)[0]['message_id'], message['message_id'])
        self.assertEqual(self.hub.messages(sid)[0]['state'], 'queued')

    def test_handoff_cannot_adopt_work_arriving_while_waiting_for_lock(self):
        from contextlib import contextmanager
        sid = self.launch()['session_id']
        original = self.hub._action_lock
        @contextmanager
        def interleave(session):
            with original(session):
                self.hub.observe('prompt-submitted')
                yield
        with self.acting_as(sid), mock.patch.object(self.hub, '_action_lock', side_effect=interleave):
            with self.assertRaisesRegex(StoreError, 'stale'):
                self.hub.handoff(None, outcome='no-durable-update', detail='Old work')

    def test_close_poll_preserves_acknowledgment_awaiting_tool_end(self):
        sid = self.launch()['session_id']
        record = self.hub.close(sid)['closure']
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('tool-started', tool_kind='finalizer', tool_token='final')
            self.hub.handoff(record['request_id'], attempt=1, outcome='no-durable-update', detail='Reviewed')
            self.assertEqual(self.hub.close(sid)['closure']['state'], 'acknowledged')
            self.assertEqual(self.tmux.killed, [])
            self.hub.observe('tool-completed', tool_kind='finalizer', tool_token='final')
            self.hub.observe('turn-stopped')
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')

    def test_lost_stop_with_stale_working_observation_requires_attach(self):
        import time
        for finalization in ('save', 'close-handoff', 'close-save'):
            with self.subTest(finalization=finalization):
                sid = self.launch()['session_id']
                if finalization != 'save':
                    record = self.hub.close(sid)['closure']
                if finalization == 'close-handoff':
                    with self.acting_as(sid):
                        self.hub.handoff(record['request_id'], attempt=1,
                            outcome='no-durable-update', detail='Reviewed')
                else:
                    self.save(sid)
                self.hub._update(sid, native_observed_at=time.time() - 301)
                result = self.hub.close(sid)
                self.assertEqual(result['next_step'], 'Close needs attach')
                self.assertTrue(result['closure']['attachment_required'])
                self.assertIn('attach', result['closure']['guidance'].lower())
                self.assertEqual(self.tmux.killed, [])
                self.hub.stop(sid)
                self.tmux.killed.clear()

    def test_proven_idle_receipt_does_not_expire_without_further_work(self):
        import time
        sid = self.launch()['session_id']
        self.save(sid)
        receipt = self.hub.get(sid)['completion']
        self.hub.close(sid)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        self.hub._update(sid, native_observed_at=time.time() - 301,
                         completion=dict(receipt, finalized_at=time.time() - 302))
        self.assertEqual(self.hub.show(sid)['next_step'], 'Finalized, closing')
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')

    def test_dashboard_stale_close_needs_attach_without_mutating_record(self):
        import time
        for requested in (False, True):
            with self.subTest(requested=requested):
                sid = self.launch()['session_id']
                if requested:
                    record = self.hub.close(sid)['closure']
                    with self.acting_as(sid):
                        self.hub.handoff(record['request_id'], attempt=1,
                            outcome='no-durable-update', detail='Reviewed')
                else:
                    self.save(sid)
                self.hub.close(sid)
                self.hub._update(sid, native_observed_at=time.time() - 301)
                before = self.hub.get(sid)
                self.assertEqual(self.hub.show(sid)['next_step'], 'Close needs attach')
                listed = next(r for r in self.hub.list()['rows'] if r['session_id'] == sid)
                self.assertEqual(listed['next_step'], 'Close needs attach')
                self.assertEqual(self.hub.get(sid), before)
                self.hub.stop(sid)

    def test_new_tool_during_report_capture_refuses_finished_state(self):
        from lib.control.session_experience import Experiences
        sid = self.launch()['session_id']
        self.save(sid)
        original = Experiences.optional_capture
        def interleave(experience, *args, **kwargs):
            self.hub.observe('tool-started', tool_kind='work', tool_token='new-work')
            return original(experience, *args, **kwargs)
        with self.acting_as(sid), mock.patch.object(Experiences, 'optional_capture', new=interleave):
            with self.assertRaisesRegex(StoreError, 'work changed'):
                self.hub.report(state='finished', body='Stale result')
        self.assertNotEqual(self.hub.get(sid)['activity'], 'finished')
        self.assertFalse(self.hub.get(sid).get('completion_report'))

    def test_missing_failed_or_oversized_callback_recovers_only_after_new_idle_turn(self):
        for callback in (None, 'unknown'):
            sid = self.launch()['session_id']
            with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
                self.hub.observe('tool-started', tool_kind='work', tool_token='lost')
                if callback:
                    self.hub.observe('tool-completed', tool_kind='work', tool_token=callback)
                self.hub.observe('prompt-submitted')
                self.assertIn('lost', self.hub.get(sid)['active_tools'])
                self.hub.observe('turn-stopped')
                self.hub.observe('prompt-submitted')
                self.assertEqual(self.hub.get(sid)['active_tools'], {})
                with self.assertRaises(StoreError):
                    self.hub.report(state='finished', body='No old receipt')
            self.save(sid)
            with self.acting_as(sid):
                self.hub.observe('turn-stopped')
            self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')

    def test_save_none_wrapper_produces_receipt_without_git(self):
        import save_none
        import memory_v2
        from pathlib import Path
        sid = self.launch()['session_id']
        active, decisions = self.drafts()
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.observe('tool-started', tool_kind='finalizer', tool_token='none-save')
            with mock.patch.dict(os.environ, {'ASHA_HUB_SESSION_ID': sid}), mock.patch(
                    'lib.control.session_publication.publication_actor', return_value=(self.hub, self.hub.get(sid))):
                result = save_none.publish_managed_none(self.project, Path(active), Path(decisions), explicit_none=True,
                    expected_preimages=memory_v2.snapshot_digests(memory_v2.read_published_snapshot(self.project)))
            self.assertFalse(result['git_invoked'])
            self.assertEqual(result['publication']['completion']['status'], 'ready')
            self.hub.observe('tool-completed', tool_kind='finalizer', tool_token='none-save')
            self.hub.observe('turn-stopped')
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')

    def test_composed_finalizer_and_parallel_work_cannot_close(self):
        for kind in ('work', 'finalizer'):
            sid = self.launch()['session_id']
            with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
                if kind == 'finalizer':
                    self.hub.observe('tool-started', tool_kind='work', tool_token='parallel')
                self.hub.observe('tool-started', tool_kind=kind, tool_token='save')
                self.hub.handoff(None, outcome='no-durable-update', detail='Attempted finalization')
                self.hub.observe('tool-completed', tool_kind=kind, tool_token='save')
                if kind == 'finalizer':
                    self.hub.observe('tool-completed', tool_kind='work', tool_token='parallel')
                self.hub.observe('turn-stopped')
                with self.assertRaises(StoreError):
                    self.hub.report(state='finished', body='Must refuse')
            self.assertNotEqual(self.hub.close(sid)['closure']['state'], 'completed')
            self.hub.stop(sid)

    def test_save_while_close_waits_suppresses_stop_request_and_closes(self):
        import threading
        import time
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
        first = self.hub.close(sid)
        errors = []
        def save_then_idle():
            try:
                time.sleep(0.1)
                self.save(sid)
                with self.acting_as(sid):
                    stopped = self.hub.observe('turn-stopped')
                    self.assertIsNone(self.hub.stop_decision(stopped))
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=save_then_idle)
        thread.start()
        result = self.hub.close(sid, wait=5)
        thread.join(timeout=5)
        self.assertFalse(errors, errors)
        self.assertEqual(result['closure']['request_id'], first['closure']['request_id'])
        self.assertEqual(result['closure']['state'], 'completed')


class StructuredCompletionTests(ClosureFixture):
    def tool_events(self, harness):
        command = 'asha control session handoff --outcome no-durable-update --detail Reviewed --json'
        if harness == 'claude':
            from lib.control.session_harness import decode_claude
            start = list(decode_claude(dict(type='assistant', message=dict(content=[
                dict(type='tool_use', id='finalizer', name='Bash', input=dict(command=command))]))))
            end = list(decode_claude(dict(type='user', message=dict(content=[
                dict(type='tool_result', tool_use_id='finalizer', content='receipt')]))))
        else:
            from tests.python.test_control_codex_protocol import CodexProtocolTests
            wire = CodexProtocolTests()
            protocol = wire.protocol()
            wire.ready(protocol)
            item = dict(type='commandExecution', id='finalizer', command=command)
            start = wire.notify(protocol, 'item/started', item=item)
            end = wire.notify(protocol, 'item/completed', item=dict(item, exitCode=0))
        return start[0], end[0]

    def start(self, harness):
        from lib.control.session_store import SessionStore
        from lib.control.session_hub import Hub
        sid = self.launch(transport='structured', harness=harness)['session_id']
        with SessionStore(self.config) as sessions:
            owner = sessions.claim_owner(sid)
            turn = sessions.claim_turn(sid, owner['generation'])
        worker = Hub(self.config, env=dict(self.env, ASHA_MANAGED_SESSION_ID=sid,
            ASHA_MANAGED_TURN_ID=turn['turn_id'], ASHA_MANAGED_GENERATION=str(owner['generation']),
            ASHA_MANAGED_STATE_DIR=str(self.config.tasks_dir.parent)), tmux=self.tmux)
        start, end = self.tool_events(harness)
        self.end_events = {**getattr(self, 'end_events', {}), sid: end}
        with SessionStore(self.config) as sessions:
            sessions.observe(sid, owner['generation'], turn['turn_id'], *start)
        return sid, owner, turn, worker

    def tool_end(self, sid, owner, turn):
        from lib.control.session_store import SessionStore
        with SessionStore(self.config) as sessions:
            sessions.observe(sid, owner['generation'], turn['turn_id'], *self.end_events[sid])

    def finish(self, sid, owner, turn):
        from lib.control.session_store import SessionStore
        with SessionStore(self.config) as sessions:
            sessions.finish(sid, owner['generation'], turn['turn_id'], success=True)
            with sessions.db.transaction(write=True) as c:
                c.execute('UPDATE managed_sessions SET owner_pid=NULL,owner_identity=NULL WHERE session_id=?', (sid,))

    def test_each_structured_adapter_finalizes_then_closes_without_another_turn(self):
        from lib.control.session_store import SessionStore
        for harness in ('claude', 'codex'):
            with self.subTest(harness=harness):
                sid, owner, turn, worker = self.start(harness)
                # Identity stub only; issuance still verifies retained running turn.
                with mock.patch.object(worker, 'structured_actor', return_value=(worker.get(sid), turn['delivery_key'])):
                    result = worker.handoff(None, outcome='no-durable-update', detail='Read-only utility')
                    self.tool_end(sid, owner, turn)
                    worker.report(state='finished', body='Reviewed')
                self.assertEqual(result['completion']['turn']['turn_id'], turn['turn_id'])
                self.finish(sid, owner, turn)
                self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')
                with SessionStore(self.config) as sessions, sessions.db.transaction() as c:
                    self.assertEqual(c.execute('SELECT count(*) FROM session_turns WHERE session_id=?', (sid,)).fetchone()[0], 1)

    def test_queued_work_failed_turn_and_new_tool_refuse_readiness(self):
        from lib.control.session_store import SessionStore
        from lib.control.session_completion import check
        for change in ('queue', 'failed', 'tool'):
            sid, owner, turn, worker = self.start('codex')
            with mock.patch.object(worker, 'structured_actor', return_value=(worker.get(sid), turn['delivery_key'])):
                worker.handoff(None, outcome='no-durable-update', detail='Reviewed')
            self.tool_end(sid, owner, turn)
            with SessionStore(self.config) as sessions:
                if change == 'queue':
                    sessions.enqueue(sid, 'More work', key='later')
                elif change == 'failed':
                    sessions.finish(sid, owner['generation'], turn['turn_id'], success=False, reason='native failure')
                else:
                    sessions.observe(sid, owner['generation'], turn['turn_id'], 'tool', {'name':'fileChange', 'status':'inProgress'})
            with self.assertRaises(StoreError):
                check(self.hub, self.hub.get(sid))
            with SessionStore(self.config) as sessions, sessions.db.transaction(write=True) as c:
                c.execute('UPDATE managed_sessions SET owner_pid=NULL,owner_identity=NULL WHERE session_id=?', (sid,))


class CompletionCommandTests(unittest.TestCase):
    def test_large_tool_result_keeps_boundary_identity(self):
        import subprocess
        import sys
        from pathlib import Path
        from completion_event import metadata
        payload = dict(tool_name='Bash', tool_use_id='native-42', tool_input=dict(command=
            'asha control session handoff --outcome no-durable-update --detail Reviewed --json'))
        expected = metadata(payload)
        payload['tool_response'] = 'output' * 10000
        tool = Path(__file__).resolve().parents[2] / 'plugins/session/tools/completion_event.py'
        result = subprocess.run([sys.executable, str(tool)], input=json.dumps(payload), text=True,
                                capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), ' '.join(expected))

    def test_only_standalone_finished_report_preserves_completion(self):
        from lib.control.session_completion import report_tool
        allowed = 'asha control session report --state finished --text "Reviewed work" --json'
        self.assertTrue(report_tool('Bash', {'command': allowed}))
        for command in (allowed + '; touch file', allowed + ' && true',
                        'echo ' + allowed, allowed.replace('finished', 'working'),
                        allowed.replace('Reviewed work', '$(touch file)'),
                        allowed + '\ntrue', allowed + ' > file',
                        allowed + ' --unknown x', allowed + ' --state finished'):
            with self.subTest(command=command):
                self.assertFalse(report_tool('Bash', {'command': command}))
        self.assertFalse(report_tool('mcp__arbitrary', {'command': allowed}))

    def test_finalizer_rejects_trailing_shell_work(self):
        from completion_event import command_kind
        command = 'asha control session handoff --outcome no-durable-update --detail Reviewed --json'
        self.assertEqual(command_kind(dict(tool_name='Bash', tool_input=dict(command=command))), 'finalizer')
        for suffix in ('; touch new-work', ' && true', ' | cat', ' &', '\ntrue', ' > receipt.json'):
            self.assertEqual(command_kind(dict(tool_name='Bash', tool_input=dict(command=command + suffix))), 'work')
