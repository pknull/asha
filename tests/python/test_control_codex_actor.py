"""Hosted actor calls retain identity and use existing coordinator authority."""
import json
import unittest
import threading
from unittest import mock

from lib.control.session_store import SessionStore
from lib.control.store import StoreError
from lib.control.orchestration import coordinator
from tests.python.orchestration_execution_fixtures import ExecutionFixture
from tests.python.test_control_managed_coordinator import NoTmux


class CodexActorTests(ExecutionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        from lib.control.codex_actor import CodexActor
        self.sessions = SessionStore(self.config.control, create=True)
        self.addCleanup(self.sessions.close)
        with mock.patch.dict('lib.control.session_harness.CAPABILITIES', {'codex': {'managed': True}}):
            session = self.sessions.create(cwd=str(self.repo), prompt='Coordinate', harness='codex',
                                           initiative_id=self.initiative_id)
        self.sid = session['session_id']
        session = self.sessions.claim_owner(self.sid)
        self.generation = session['generation']
        self.actor_env = {**self.env, 'ASHA_MANAGED_SESSION_ID': self.sid,
            'ASHA_MANAGED_GENERATION': str(self.generation),
            'ASHA_MANAGED_STATE_DIR': str(self.config.control.tasks_dir.parent)}
        self.coordinator = coordinator.claim(self.store, self.initiative(), env=self.actor_env,
                                            tmux=NoTmux(), harness='codex')
        self.turn = self.sessions.claim_turn(self.sid, self.generation)['turn_id']
        self.actor = CodexActor(self.config.control, self.sid, self.generation, self.turn, env=self.actor_env)
        self.addCleanup(self.actor.close)

    def test_question_receipt_survives_lost_reply_without_duplicate(self):
        args = {'operation': 'ask', 'question': 'Which chapter?'}
        first = self.actor.execute('call-1', args)
        self.assertTrue(first['success'])
        self.assertEqual(first, self.actor.execute('call-1', args))
        with self.sessions.db.transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM session_requests').fetchone()[0], 1)
        with self.assertRaisesRegex(StoreError, 'changed'):
            self.actor.execute('call-1', {**args, 'question': 'Different?'})

    def test_foreign_selectors_and_operator_actions_are_refused(self):
        for args in ({'operation': 'ask', 'question': 'Q', 'session_id': 'foreign'},
                     {'operation': 'inspect', 'initiative_id': 'foreign'},
                     {'operation': 'action', 'action_class': 'activate-initiative', 'payload': {}},
                     {'operation': 'approve'}):
            with self.subTest(args=args):
                self.assertFalse(self.actor.execute(json.dumps(args), args)['success'])
        self.assertEqual(self.store.list_actions_snapshot(self.initiative_id), [])

    def test_stopped_and_stale_owner_cannot_execute(self):
        self.actor.generation += 1
        with self.assertRaisesRegex(StoreError, 'stale'):
            self.actor.execute('stale', {'operation': 'ask', 'question': 'Q'})
        self.actor.generation -= 1
        self.sessions.stop(self.sid)
        with self.assertRaisesRegex(StoreError, 'stopping'):
            self.actor.execute('stopped', {'operation': 'ask', 'question': 'Q'})

    def test_action_uses_bound_coordinator_and_stable_id(self):
        args = {'operation': 'action', 'action_class': 'dispatch-node',
                'payload': {'node_id': 'implementation-a'}}
        def capture(argv, **kwargs):
            return 0, json.dumps(self.control_payload(argv)).encode(), b''
        with mock.patch('lib.control.orchestration.scheduler.storage_report', return_value={'pause_recommended': False}), \
             mock.patch('lib.control.orchestration.scheduler.capture_bytes', side_effect=capture) as dispatch:
            first = self.actor.execute('dispatch', args)
            self.assertEqual(first, self.actor.execute('dispatch', args))
        self.assertTrue(first['success'], first)
        self.assertEqual(dispatch.call_count, 1)
        actions = self.store.list_actions_snapshot(self.initiative_id)
        self.assertEqual(actions[0]['actor_kind'], 'coordinator')
        self.assertEqual(actions[0]['coordinator_id'], self.coordinator['coordinator_id'])

    def test_uncertain_receipt_is_never_reexecuted(self):
        args = {'operation': 'ask', 'question': 'Q'}
        with mock.patch.object(self.actor, '_perform', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.actor.execute('crash', args)
        with mock.patch.object(self.actor, '_perform') as perform:
            result = self.actor.execute('crash', args)
        self.assertFalse(result['success'])
        self.assertIn('uncertain', result['contentItems'][0]['text'])
        perform.assert_not_called()

    def test_inspection_pages_include_revision_and_continuation(self):
        result = self.actor.execute('inspect', {'operation': 'inspect', 'kind': 'nodes', 'limit': 1})
        self.assertTrue(result['success'], result)
        data = json.loads(result['contentItems'][0]['text'])['result']
        self.assertEqual(len(data['records']), 1)
        self.assertEqual(data['next_offset'], 1)
        self.assertEqual(data['state_revision'], self.initiative()['state_revision'])

    def test_async_dispatch_leaves_transport_poll_free_and_close_drains(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(*_):
            entered.set()
            self.assertTrue(release.wait(5))
            return {'retained': True}
        with mock.patch.object(self.actor, '_perform', side_effect=slow):
            self.actor.submit('rpc-1', 'slow', {'operation': 'inspect'})
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.actor.poll(), [])
            # Another owner connection is usable while the effect is running.
            self.assertEqual(self.sessions.get(self.sid)['state'], 'running')
            release.set()
            self.actor.close()
        replies = self.actor.poll()
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0][1]['success'])

    def test_foreign_coordinator_anchor_is_refused(self):
        foreign = {**self.coordinator, 'anchor': {**self.coordinator['anchor'], 'session_id': 'foreign'}}
        with mock.patch('lib.control.orchestration.coordinator.require_live_coordinator', return_value=foreign):
            result = self.actor.execute('foreign', {'operation': 'inspect'})
        self.assertFalse(result['success'])
        self.assertIn('does not own', result['contentItems'][0]['text'])

    def test_bounded_arguments_and_result_fit_combined_durable_receipt(self):
        args = {'operation': 'propose_plan', 'plan': {'description': '\\' * 120000}}
        with mock.patch.object(self.actor, '_perform', return_value={'body': '\\' * 120000}):
            first = self.actor.execute('large', args)
        self.assertTrue(first['success'])
        self.assertEqual(first, self.actor.execute('large', args))

    def test_capacity_refusal_creates_no_call_custody(self):
        self.actor.pending = {str(n): mock.Mock() for n in range(8)}
        self.assertFalse(self.actor.submit('ninth', 'ninth', {'operation': 'ask', 'question': 'Q'}))
        self.actor.pending.clear()
        with self.sessions.db.transaction() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM records WHERE domain='session-native-actor'").fetchone()[0], 0)

    def test_close_records_queued_cancellation_without_running_effect(self):
        from concurrent.futures import Future
        future = Future()
        self.actor.pending['queued'] = future
        self.actor.calls['queued'] = 'queued-call'
        self.actor.close()
        self.actor.close()
        self.assertTrue(future.cancelled())
        with self.sessions.db.transaction() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM records WHERE domain='session-native-actor'").fetchone()[0], 0)
        events = [e['payload'] for e in self.sessions.snapshot(self.sid)['events'] if e['kind'] == 'tool']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['status'], 'cancelled')
        self.assertEqual(events[0]['tool_id'], 'queued-call')

    def test_receipt_write_failure_response_warns_against_new_call_replay(self):
        from concurrent.futures import Future
        future = Future()
        future.set_exception(StoreError('database busy after effect'))
        self.actor.pending['failed'] = future
        self.actor.calls['failed'] = 'failed-call'
        result = self.actor.poll()[0][1]
        payload = json.loads(result['contentItems'][0]['text'])
        self.assertFalse(result['success'])
        self.assertIn('may have committed', payload['instruction'])
        self.assertTrue(payload['operation_id'])
