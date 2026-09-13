"""Read-only reviewer: temporary fixtures and no native providers."""
import json
import subprocess
import sys
import unittest
import uuid
from tests.python import test_experience_guidance_regressions as fixtures
ACTION = fixtures.ACTION
from lib.control import session_guidance as guidance
from lib.control.session_store import SessionStore
from lib.control.store import StoreError
import learnings_manager as lm

class ReceiptFollowup(fixtures.GuidanceReview):
    def pending(self):
        sid = self.launch()['session_id']
        message = self.hub.send(sid, 'Perform the assigned work', key=str(uuid.uuid4()), learning_ids=['review-rule'])
        return sid, message
    def emitted(self, sid, message):
        return next(m for m in self.hub.messages(sid) if m['message_id'] == message['message_id'])
    def exposure(self, sid, message):
        return next(x for x in self.hub.show(sid)['guidance'] if x['delivery_key'] == message['delivery_key'])
    def silence(self):
        marker = self.project / 'Work' / 'markers' / 'silence'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('review fixture')
    def test_new_reader_does_not_replace_explicit_previous_receipt(self):
        sid, message = self.pending()
        first = self.emitted(sid, message)
        lm.retire('review-rule', 'Retired between reads', project_dir=self.project)
        second = self.emitted(sid, message)
        self.assertNotEqual(first['delivery_digest'], second['delivery_digest'])
        with self.acting_as(sid):
            self.hub.acknowledge(message['message_id'], delivery_digest=first['delivery_digest'])
        retained = self.exposure(sid, message)
        self.assertEqual(first['delivery_digest'], retained['manifest']['delivery_digest'])
        self.assertIn(ACTION, json.dumps(retained['manifest']['supplied']))
    def test_no_read_acknowledgement_keeps_supply_unknown(self):
        sid, message = self.pending()
        with self.acting_as(sid):
            result = self.hub.acknowledge(message['message_id'])
        self.assertEqual('unknown', result['guidance_status'])
        self.assertEqual('queued', self.exposure(sid, message)['status'])
    def test_unknown_digest_refuses_without_acknowledging_message(self):
        sid, message = self.pending()
        with self.acting_as(sid), self.assertRaises(StoreError):
            self.hub.acknowledge(message['message_id'], delivery_digest='0' * 64)
        self.assertEqual('queued', self.emitted(sid, message)['state'])
    def test_supplied_receipt_retry_is_idempotent(self):
        sid, message = self.pending()
        first = self.emitted(sid, message)
        with self.acting_as(sid):
            self.hub.acknowledge(message['message_id'], delivery_digest=first['delivery_digest'])
            result = self.hub.acknowledge(message['message_id'], delivery_digest=first['delivery_digest'])
        self.assertEqual('supplied', result['guidance_status'])
        self.assertEqual(first['delivery_digest'], self.exposure(sid, message)['manifest']['delivery_digest'])
    def test_silenced_resume_keeps_retired_old_queued_guidance_excluded(self):
        sid = self.launch(transport='structured', learning_ids=['review-rule'])['session_id']
        from lib.control.runtime import set_admission
        set_admission(self.config, 'running')
        child = "from lib.control.config import load_config; from lib.control.session_store import SessionStore; import sys,json; c=load_config(json.loads(sys.argv[1])); s=SessionStore(c); o=s.claim_owner(sys.argv[2]); s.fail_owner(sys.argv[2],o['generation'],'Fixture owner failed before native input')"
        subprocess.run([sys.executable, '-c', child, json.dumps(self.env), sid], check=True)
        with SessionStore(self.config) as store:
            expected = store.recovery_digest(store.get(sid))
        lm.retire('review-rule', 'Retired before silenced recovery', project_dir=self.project)
        original_exposure = self.hub.show(sid)['guidance']
        self.silence()
        self.hub.resume(sid, prompt='Continue the queued work', expected_digest=expected)
        with SessionStore(self.config) as store:
            owner = store.claim_owner(sid)
            recovery = store.claim_turn(sid, owner['generation'])
            store.finish(sid, owner['generation'], recovery['turn_id'], success=True)
            queued = store.claim_turn(sid, owner['generation'])
        self.assertEqual('opening', queued['delivery_key'])
        rendered, manifest = guidance.delivery(self.hub, self.hub.get(sid), queued['delivery_key'], queued['body'])
        self.assertNotIn(ACTION, rendered, 'silence must not erase custody needed to exclude retired input')
        self.assertEqual(original_exposure, self.hub.show(sid)['guidance'])

if __name__ == '__main__':
    names = [n for n in ReceiptFollowup.__dict__ if n.startswith('test_')]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(ReceiptFollowup(n) for n in names))
    raise SystemExit(not result.wasSuccessful())

def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ReceiptFollowup(name) for name in ReceiptFollowup.__dict__ if name.startswith("test_"))
