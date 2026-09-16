"""Read-only reviewer cases for bootstrap deselection and silence acknowledgement."""
import unittest
import uuid
from unittest import mock
from tests.python import test_experience_guidance_regressions as fixtures
ACTION = fixtures.ACTION
from lib.control.session_store import SessionStore
from lib.control.store import StoreError
from lib.control import session_guidance as guidance

class FinalRecoveryReview(fixtures.GuidanceReview):
    def test_bootstrap_recovery_without_reselecting_old_guidance(self):
        sid = str(uuid.uuid4())
        with mock.patch.object(SessionStore, '_create_in_transaction', side_effect=StoreError('fixture creation failed')):
            with self.assertRaises(StoreError):
                self.launch(session_id=sid, transport='structured', learning_ids=['review-rule'])
        self.hub.resume(sid, prompt='Continue without selecting old guidance', learning_ids=[])
        with SessionStore(self.config) as store, store.db.transaction() as c:
            queued = dict(c.execute("SELECT * FROM session_messages WHERE session_id=? AND delivery_key='opening'", (sid,)).fetchone())
        rendered, manifest = guidance.delivery(self.hub, self.hub.get(sid), queued['delivery_key'], queued['body'])
        self.assertNotIn(ACTION, rendered)
        self.assertIn('Continue without selecting old guidance', rendered)
    def test_read_before_silence_ack_does_not_claim_unretained_supply_status(self):
        sid = self.launch()['session_id']
        message = self.hub.send(sid, 'Continue work', key=str(uuid.uuid4()), learning_ids=['review-rule'])
        emitted = next(m for m in self.hub.messages(sid) if m['message_id'] == message['message_id'])
        marker = self.project / 'Work' / 'markers' / 'silence'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('fixture')
        with self.acting_as(sid):
            result = self.hub.acknowledge(message['message_id'], delivery_digest=emitted['delivery_digest'])
        self.assertEqual('acknowledged', result['state'])
        retained = next(x for x in self.hub.show(sid)['guidance'] if x['delivery_key'] == message['delivery_key'])
        self.assertEqual('queued', retained['status'])
        self.assertNotEqual('supplied', result['guidance_status'], 'suppressed recording must be explicit in returned guidance status')

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(FinalRecoveryReview(n) for n in FinalRecoveryReview.__dict__ if n.startswith('test_')))
    raise SystemExit(not result.wasSuccessful())

def load_tests(loader, tests, pattern):
    return unittest.TestSuite(FinalRecoveryReview(name) for name in FinalRecoveryReview.__dict__ if name.startswith("test_"))
