"""Read-only reviewer reproductions: only ClosureFixture temporary state is written."""
import json
import unittest
import uuid
import subprocess
import sys
from tests.python import test_experience_guidance_regressions as fixtures
ACTION = fixtures.ACTION
from lib.control.session_store import SessionStore
from lib.control import session_guidance as guidance
import learnings_manager as lm

class GuidanceCustodyReview(fixtures.GuidanceReview):
    def test_terminal_ack_binds_to_body_read_before_rule_retirement(self):
        sid = self.launch()['session_id']
        original = self.hub.send(sid, 'Use the chosen guidance', key=str(uuid.uuid4()), learning_ids=['review-rule'])
        emitted = next(m for m in self.hub.messages(sid) if m['message_id'] == original['message_id'])
        self.assertIn(ACTION, emitted['body'])
        lm.retire('review-rule', 'Retired after the worker read this input.', project_dir=self.project)
        with self.acting_as(sid):
            self.hub.acknowledge(original['message_id'], delivery_digest=emitted['delivery_digest'])
        retained = next(x for x in self.hub.show(sid)['guidance'] if x['delivery_key'] == original['delivery_key'])
        self.assertEqual('supplied', retained['status'])
        self.assertEqual(emitted['delivery_digest'], retained['manifest']['delivery_digest'],
                         'ack must record the exact body the worker received, not re-resolve a different body')
        self.assertIn(ACTION, json.dumps(retained['manifest']['supplied']))

    def test_structured_old_queued_generation_revalidates_retired_guidance(self):
        sid = self.launch(transport='structured', learning_ids=['review-rule'])['session_id']
        from lib.control.runtime import set_admission
        set_admission(self.config, 'running')
        # A real throwaway Python owner exits after recording a failure; no native backend.
        child = "from lib.control.config import load_config; from lib.control.session_store import SessionStore; import sys,json; c=load_config(json.loads(sys.argv[1])); s=SessionStore(c); o=s.claim_owner(sys.argv[2]); s.fail_owner(sys.argv[2],o['generation'],'Fixture owner failed before native input')"
        subprocess.run([sys.executable, '-c', child, json.dumps(self.env), sid], check=True)
        with SessionStore(self.config) as store:
            expected = store.recovery_digest(store.get(sid))
        lm.retire('review-rule', 'Retired before the queued opening was delivered.', project_dir=self.project)
        self.hub.resume(sid, prompt='Continue the queued work', expected_digest=expected)
        with SessionStore(self.config) as store:
            owner = store.claim_owner(sid)
            recovery = store.claim_turn(sid, owner['generation'])
            self.assertTrue(recovery['delivery_key'].startswith('recovery:'))
            store.finish(sid, owner['generation'], recovery['turn_id'], success=True)
            queued = store.claim_turn(sid, owner['generation'])
        self.assertEqual('opening', queued['delivery_key'])
        self.assertEqual(2, self.hub.get(sid)['generation'])
        # This is the exact helper seam sessions.run_turn invokes on a queued message.
        rendered, manifest = guidance.delivery(self.hub, self.hub.get(sid), queued['delivery_key'], queued['body'])
        self.assertNotIn(ACTION, rendered, 'a generation change must not bypass active-rule validation')

    def test_terminal_body_equal_to_planned_suffix_retains_body_custody(self):
        sid = self.launch()['session_id']
        block, _ = guidance.resolve(self.hub, self.hub.get(sid), ['review-rule'])
        body = 'Please discuss this existing excerpt:' + block
        original = self.hub.send(sid, body, key=str(uuid.uuid4()), learning_ids=['review-rule'])
        emitted = next(m for m in self.hub.messages(sid) if m['message_id'] == original['message_id'])
        self.assertEqual(body + block, emitted['body'])

if __name__ == '__main__':
    names = [
      'test_terminal_ack_binds_to_body_read_before_rule_retirement',
      'test_structured_old_queued_generation_revalidates_retired_guidance',
      'test_terminal_body_equal_to_planned_suffix_retains_body_custody',
    ]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(GuidanceCustodyReview(name) for name in names))
    raise SystemExit(not result.wasSuccessful())

def load_tests(loader, tests, pattern):
    return unittest.TestSuite(GuidanceCustodyReview(name) for name in (
        "test_terminal_ack_binds_to_body_read_before_rule_retirement",
        "test_structured_old_queued_generation_revalidates_retired_guidance",
        "test_terminal_body_equal_to_planned_suffix_retains_body_custody"))
