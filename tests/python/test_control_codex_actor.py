"""Hosted actor calls retain identity; the only operation is ask (L-b)."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from lib.control.config import load_config
from lib.control.session_store import SessionStore
from lib.control.store import StoreError


class CodexActorTests(unittest.TestCase):
    def setUp(self):
        from lib.control.codex_actor import CodexActor
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        (root / 'home').mkdir()
        self.repo = root / 'repo'
        self.repo.mkdir()
        self.env = {'HOME': str(root / 'home'), 'ASHA_CONFIG': str(root / 'missing.json'),
                    'ASHA_HOME': str(root / 'asha'), 'XDG_RUNTIME_DIR': str(root / 'runtime')}
        self.config = load_config(self.env)
        self.sessions = SessionStore(self.config, create=True)
        self.addCleanup(self.sessions.close)
        with mock.patch.dict('lib.control.session_harness.CAPABILITIES', {'codex': {'managed': True}}):
            session = self.sessions.create(cwd=str(self.repo), prompt='Answer a question', harness='codex')
        self.sid = session['session_id']
        session = self.sessions.claim_owner(self.sid)
        self.generation = session['generation']
        self.actor_env = {**self.env, 'ASHA_MANAGED_SESSION_ID': self.sid,
            'ASHA_MANAGED_GENERATION': str(self.generation),
            'ASHA_MANAGED_STATE_DIR': str(self.config.tasks_dir.parent)}
        self.turn = self.sessions.claim_turn(self.sid, self.generation)['turn_id']
        self.actor = CodexActor(self.config, self.sid, self.generation, self.turn, env=self.actor_env)
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

    def test_foreign_selectors_and_every_operation_but_ask_are_refused(self):
        for args in ({'operation': 'ask', 'question': 'Q', 'session_id': 'foreign'},
                     {'operation': 'inspect', 'kind': 'head'},
                     {'operation': 'propose_plan', 'plan': {}},
                     {'operation': 'action', 'action_class': 'dispatch-node', 'payload': {}},
                     {'operation': 'receive_message', 'message_id': 'm'},
                     {'operation': 'approve'}):
            with self.subTest(args=args):
                result = self.actor.execute(json.dumps(args), args)
                self.assertFalse(result['success'])
                self.assertIn('unsupported actor operation', result['contentItems'][0]['text'])
        with self.sessions.db.transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM session_requests').fetchone()[0], 0)

    def test_stopped_and_stale_owner_cannot_execute(self):
        self.actor.generation += 1
        with self.assertRaisesRegex(StoreError, 'stale'):
            self.actor.execute('stale', {'operation': 'ask', 'question': 'Q'})
        self.actor.generation -= 1
        self.sessions.stop(self.sid)
        with self.assertRaisesRegex(StoreError, 'stopping'):
            self.actor.execute('stopped', {'operation': 'ask', 'question': 'Q'})

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

    def test_async_dispatch_leaves_transport_poll_free_and_close_drains(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow(*_):
            entered.set()
            self.assertTrue(release.wait(5))
            return {'retained': True}
        with mock.patch.object(self.actor, '_perform', side_effect=slow):
            self.actor.submit('rpc-1', 'slow', {'operation': 'ask', 'question': 'Q'})
            self.assertTrue(entered.wait(3))
            self.assertEqual(self.actor.poll(), [])
            # Another owner connection is usable while the effect is running.
            self.assertEqual(self.sessions.get(self.sid)['state'], 'running')
            release.set()
            self.actor.close()
        replies = self.actor.poll()
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0][1]['success'])

    def test_bounded_arguments_and_result_fit_combined_durable_receipt(self):
        args = {'operation': 'ask', 'question': '\\' * 120000}
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
