"""Silence must preserve ordinary terminal message acknowledgement."""
import unittest
import uuid
from tests.python import test_experience_guidance_regressions as fixtures

class SilenceAck(fixtures.GuidanceReview):
    def test_silenced_read_receipt_does_not_break_ordinary_ack(self):
        sid = self.launch()['session_id']
        message = self.hub.send(sid, 'Continue work', key=str(uuid.uuid4()), learning_ids=['review-rule'])
        marker = self.project / 'Work' / 'markers' / 'silence'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('fixture')
        emitted = next(m for m in self.hub.messages(sid) if m['message_id'] == message['message_id'])
        with self.acting_as(sid):
            result = self.hub.acknowledge(message['message_id'], delivery_digest=emitted.get('delivery_digest'))
        self.assertEqual('acknowledged', result['state'])
        retained = next(item for item in self.hub.show(sid)['guidance']
                        if item['delivery_key'] == message['delivery_key'])
        self.assertNotEqual('supplied', retained['status'])

if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([
        SilenceAck('test_silenced_read_receipt_does_not_break_ordinary_ack')]))
    raise SystemExit(not result.wasSuccessful())

def load_tests(loader, tests, pattern):
    return unittest.TestSuite(SilenceAck(name) for name in SilenceAck.__dict__ if name.startswith("test_"))
