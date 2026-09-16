"""C7 owned native continuation fixtures; no paid native probes."""
import time
import unittest
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture
from lib.control import session_hub


class IdleClose(ClosureFixture):
    def idle(self, harness='codex', native_id='native-conversation'):
        row = self.launch(harness=harness)
        with self.acting_as(row['session_id']):
            self.hub.observe('turn-stopped', native_id=native_id)
        return self.hub.get(row['session_id'])

    def test_idle_codex_resumes_same_conversation_and_preserves_request_until_ack(self):
        row = self.idle()
        with mock.patch.object(session_hub, 'open_room', wraps=session_hub.open_room) as start:
            result = self.hub.close(row['session_id'])
        record = result['closure']
        self.assertEqual(record['delivery']['channel'], 'native-resume')
        self.assertEqual(record['state'], 'delivered')
        self.assertEqual(result['generation'], row['generation'] + 1)
        self.assertEqual(record['generation'], result['generation'])
        self.assertEqual(start.call_args.kwargs['resume_id'], 'native-conversation')
        self.assertIn(record['request_id'], start.call_args.kwargs['prompt'])
        self.assertEqual(len(self.tmux.killed), 1)
        self.assertEqual(self.hub.close(row['session_id'])['closure']['request_id'], record['request_id'])
        self.assertEqual(len(self.tmux.killed), 1, 'repeated close must not launch another continuation')
        with self.acting_as(row['session_id']):
            self.hub.handoff(record['request_id'], outcome='no-durable-update', detail='No durable change')
        self.assertEqual(self.hub.show(row['session_id'])['closure']['state'], 'acknowledged')
        self.assertEqual(self.hub.close(row['session_id'])['closure']['state'], 'completed')

    def test_busy_codex_is_queued_without_stopping_owned_process(self):
        row = self.idle()
        with self.acting_as(row['session_id']):
            self.hub.observe('prompt-submitted')
        closing = self.hub.close(row['session_id'])
        self.assertEqual(closing['closure']['state'], 'pending-delivery')
        self.assertEqual(self.tmux.killed, [])

    def test_finished_report_is_not_idle_until_a_native_stop_is_observed(self):
        row = self.launch(harness='codex')
        with self.acting_as(row['session_id']):
            self.hub.report(state='finished', body='Done', native_id='conversation')
        first = self.hub.close(row['session_id'])
        self.assertEqual(self.tmux.killed, [])
        self.assertTrue(first['closure']['attachment_required'])
        with self.acting_as(row['session_id']):
            self.hub.observe('turn-stopped')
        resumed = self.hub.close(row['session_id'])
        self.assertEqual(resumed['closure']['delivery']['channel'], 'native-resume')
        self.assertEqual(resumed['closure']['request_id'], first['closure']['request_id'])

    def test_unknown_stale_and_unsupported_idle_need_attachment(self):
        for harness, state in [('codex', 'unknown'), ('codex', 'stale'), ('copilot', 'idle'), ('opencode', 'idle')]:
            with self.subTest(harness=harness, state=state):
                row = self.idle(harness)
                if state == 'unknown':
                    self.hub._update(row['session_id'], activity='unknown', observed_at=None,
                                     native_activity='unknown', native_observed_at=None)
                elif state == 'stale':
                    self.hub._update(row['session_id'], observed_at=time.time() - 301, native_observed_at=time.time() - 301)
                result = self.hub.close(row['session_id'])
                self.assertEqual(result['closure']['state'], 'unanswered')
                self.assertTrue(result['closure']['attachment_required'])
                self.assertIn('attach', result['closure']['guidance'].lower())
                self.assertEqual(self.tmux.killed, [])
                self.hub.stop(row['session_id'])
                self.tmux.killed.clear()

    def test_missing_native_id_never_starts_a_fresh_conversation(self):
        row = self.idle(native_id=None)
        with mock.patch.object(session_hub, 'open_room') as start:
            result = self.hub.close(row['session_id'])
        start.assert_not_called()
        self.assertEqual(result['closure']['state'], 'unanswered')
        self.assertTrue(result['closure']['attachment_required'])
        self.assertEqual(self.tmux.killed, [])

    def test_failed_native_resume_retains_request_and_requires_attachment(self):
        row = self.idle()
        with mock.patch.object(session_hub, 'open_room', side_effect=OSError('fixture resume failed')):
            result = self.hub.close(row['session_id'])
        self.assertEqual(result['closure']['state'], 'unanswered')
        self.assertTrue(result['closure']['attachment_required'])
        self.assertEqual(result['closure']['generation'], result['generation'])
        self.assertIn('resume', result['closure']['last_error'])
        self.assertEqual(self.hub.stop(row['session_id'])['lifecycle'], 'stopped')

    def test_failed_resume_before_room_creation_can_be_force_closed(self):
        row = self.idle()
        with mock.patch.object(session_hub, 'open_room', side_effect=OSError('fixture precreation failure')):
            failed = self.hub.close(row['session_id'])
        closed = self.hub.close(row['session_id'], force=True)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['closure']['request_id'], failed['closure']['request_id'])

    def test_work_observed_during_liveness_probe_prevents_stop(self):
        row = self.idle()
        original = self.hub._live
        def became_busy(current):
            with self.acting_as(row['session_id']):
                self.hub.observe('prompt-submitted')
            return original(current)
        with mock.patch.object(self.hub, '_live', side_effect=became_busy):
            result = self.hub.close(row['session_id'])
        self.assertEqual(self.tmux.killed, [])
        self.assertEqual(result['closure']['state'], 'pending-delivery')

    def test_pending_close_is_reconsidered_when_working_session_becomes_idle(self):
        row = self.idle()
        with self.acting_as(row['session_id']):
            self.hub.observe('prompt-submitted')
        first = self.hub.close(row['session_id'])
        with self.acting_as(row['session_id']):
            self.hub.observe('turn-stopped')
        second = self.hub.close(row['session_id'])
        self.assertEqual(second['closure']['request_id'], first['closure']['request_id'])
        self.assertEqual(second['closure']['delivery']['channel'], 'native-resume')

    def test_new_generation_requires_its_own_idle_observation(self):
        row = self.idle()
        self.hub.stop(row['session_id'])
        resumed = self.hub.resume(row['session_id'], prompt='New work')
        kills = list(self.tmux.killed)
        closing = self.hub.close(row['session_id'])
        self.assertEqual(closing['generation'], resumed['generation'])
        self.assertEqual(self.tmux.killed, kills)
        self.assertTrue(closing['closure']['attachment_required'])

    def test_new_work_at_automatic_stop_boundary_cancels_wake(self):
        row = self.idle()
        original = self.hub._update
        def work_before_stop(sid, **changes):
            result = original(sid, **changes)
            if changes.get('lifecycle') == 'closing' and changes.get('closure'):
                with self.acting_as(sid):
                    self.hub.observe('prompt-submitted')
            return result
        with mock.patch.object(self.hub, '_update', side_effect=work_before_stop):
            closing = self.hub.close(row['session_id'])
        self.assertEqual(self.tmux.killed, [])
        self.assertEqual(closing['generation'], row['generation'])
        self.assertEqual(closing['closure']['state'], 'pending-delivery')

    def test_owned_stop_allows_registry_to_use_its_own_database_transaction(self):
        row = self.idle()
        original = self.tmux.kill_owned_room
        def registry_stop(**identity):
            # SQLite-backed Room custody needs its own writer transaction.
            with self.hub.database() as db, db.transaction(write=True) as c:
                c.execute('UPDATE hub_sessions SET updated_at=updated_at WHERE session_id=?', (row['session_id'],))
            return original(**identity)
        with mock.patch.object(self.tmux, 'kill_owned_room', side_effect=registry_stop):
            result = self.hub.close(row['session_id'])
        self.assertEqual(result['closure']['delivery']['channel'], 'native-resume')


if __name__ == '__main__':
    unittest.main()
