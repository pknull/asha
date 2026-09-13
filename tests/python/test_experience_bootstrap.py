"""Read-only review of retry after a failed structured-record creation."""
import unittest
import uuid
from unittest import mock
from tests.python import test_experience_guidance_regressions as fixtures
ACTION = fixtures.ACTION
from lib.control.store import StoreError
from lib.control.session_store import SessionStore
from lib.control import session_guidance as guidance

class BootstrapRecovery(fixtures.GuidanceReview):
    def test_resume_after_closing_failed_bootstrap_preserves_prior_closure(self):
        sid = str(uuid.uuid4())
        with mock.patch.object(SessionStore, '_create_in_transaction', side_effect=StoreError('fixture creation failure')):
            with self.assertRaises(StoreError):
                self.launch(session_id=sid, transport='structured')
        closed = self.hub.close(sid)['closure']
        resumed = self.hub.resume(sid, prompt='Start after the failed launch was closed')
        self.assertIsNone(resumed.get('closure'))
        self.assertEqual(len(resumed['closure_history']), 1)
        self.assertEqual(resumed['closure_history'][0]['request_id'], closed['request_id'])
        self.assertEqual(resumed['closure_history'][0]['generation'], closed['generation'])
        self.assertEqual(resumed['closure_history'][0]['state'], closed['state'])

    def test_failed_structured_creation_resume_rebuilds_guidance_assignment(self):
        sid = str(uuid.uuid4())
        with mock.patch.object(SessionStore, '_create_in_transaction', side_effect=OSError('fixture transient creation failure')):
            with self.assertRaises(StoreError):
                self.launch(session_id=sid, transport='structured', learning_ids=['review-rule'])
        self.assertEqual('interrupted', self.hub.get(sid)['lifecycle'])
        self.hub.resume(sid, prompt='Continue after initial creation failure', learning_ids=['review-rule'])
        with SessionStore(self.config) as store, store.db.transaction() as c:
            queued = dict(c.execute("SELECT * FROM session_messages WHERE session_id=? AND delivery_key='opening'", (sid,)).fetchone())
        # Native sessions.run_turn uses this exact helper before creating transport.
        rendered, manifest = guidance.delivery(self.hub, self.hub.get(sid), queued['delivery_key'], queued['body'])
        self.assertIn(ACTION, rendered)
        self.assertIn('Continue after initial creation failure', rendered)

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        BootstrapRecovery('test_failed_structured_creation_resume_rebuilds_guidance_assignment')]))
    raise SystemExit(not result.wasSuccessful())

def load_tests(loader, tests, pattern):
    return unittest.TestSuite(BootstrapRecovery(name) for name in BootstrapRecovery.__dict__ if name.startswith("test_"))
