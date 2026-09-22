"""C6 publication identity comes from the verified actor, not receipt input."""
import os
import unittest
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture, ACTIVE, DECISIONS
from lib.control.session_experience import Experiences, memory_v2
from lib.control.store import StoreError
from tests.python import test_experience_defaults as review_fixture


class PublicationLinkage(ClosureFixture):
    def setUp(self):
        super().setUp()
        self.row = self.launch(profile='room')
        self.sid = self.row['session_id']
        Experiences(self.hub).set_policy(str(self.project), 'capture', expected_revision=0)

    def publish(self, source='explicit-save'):
        with self.acting_as(self.sid), mock.patch('lib.control.session_hub.Hub', return_value=self.hub), \
                mock.patch.dict(os.environ, dict(self.env, ASHA_HUB_SESSION_ID=self.sid,
                                                ASHA_HUB_GENERATION='1'), clear=True):
            return memory_v2.publish(self.project, ACTIVE, DECISIONS, publication_source=source)

    def test_verified_publication_is_retained_and_close_omits_assessment(self):
        from lib.control.session_publication import verify_publication
        receipt = self.publish()
        self.assertEqual(receipt['hub_session_id'], self.sid)
        self.assertEqual(receipt['hub_generation'], 1)
        verify_publication(self.hub, self.hub.get(self.sid), receipt)
        self.assertIsNotNone(self.hub.show(self.sid)['memory_saved_at'])
        closed = self.hub.close(self.sid)
        self.assertEqual(closed['closure']['capture']['status'], 'disabled')
        self.assertEqual(closed['closure']['capture']['reason'], 'explicit-save-published')
        self.assertNotIn('--experience-file', self.hub.messages(self.sid)[0]['body'])
        self.assertEqual(self.tmux.killed, [], 'publication alone does not replace issue #92 completion receipt')
        with self.acting_as(self.sid):
            acknowledgement = self.hub.handoff(closed['closure']['request_id'], outcome='no-durable-update', detail='Already saved')
        self.assertEqual(acknowledgement['capture']['status'], 'disabled')
        self.assertEqual(acknowledgement['capture']['reason'], 'explicit-save-published')

    def test_new_assignment_invalidates_close_omission_but_retains_publication(self):
        from lib.control.session_publication import verify_publication
        receipt = self.publish()
        self.hub.send(self.sid, 'Another assignment', key='next', learning_ids=[])
        self.assertIsNone(self.hub.show(self.sid)['memory_saved_at'])
        verify_publication(self.hub, self.hub.get(self.sid), receipt)
        closing = self.hub.close(self.sid)
        self.assertTrue(closing['closure']['capture']['requested'])

    def test_resume_generation_cannot_reuse_publication(self):
        from lib.control.session_publication import verify_publication
        receipt = self.publish()
        self.hub.stop(self.sid)
        self.hub.resume(self.sid, prompt='Continue', learning_ids=[])
        self.assertIsNone(self.hub.show(self.sid)['memory_saved_at'])
        with self.assertRaises(StoreError):
            verify_publication(self.hub, self.hub.get(self.sid), receipt)
        self.assertTrue(self.hub.close(self.sid)['closure']['capture']['requested'])

    def test_new_native_assignment_needs_a_new_completion_capture_key(self):
        with self.acting_as(self.sid):
            self.hub.handoff(None, outcome='no-durable-update', detail='First read-only task')
            first = self.hub.report(state='finished', body='First result')
            self.hub.observe('prompt-submitted')
            self.hub.handoff(None, outcome='no-durable-update', detail='Second read-only task')
            second = self.hub.report(state='finished', body='Second result')
        self.assertNotEqual(first['experience_request']['key'], second['experience_request']['key'])
        self.assertEqual(second['result'], 'Second result')

    def test_native_prompt_invalidates_save_omission_even_with_same_text(self):
        self.publish()
        with self.acting_as(self.sid):
            self.hub.observe('prompt-submitted')
        self.assertTrue(self.hub.close(self.sid)['closure']['capture']['requested'])

    def test_forged_receipt_and_actor_fail_without_false_linkage(self):
        from lib.control.session_publication import verify_publication
        receipt = self.publish()
        with self.assertRaises(StoreError):
            verify_publication(self.hub, self.hub.get(self.sid), dict(receipt, after={'active': '0' * 64, 'decisions': '1' * 64}))
        before = self.digests()
        with mock.patch('lib.control.session_hub.Hub', return_value=self.hub), \
                mock.patch.object(self.hub, 'actor', side_effect=StoreError('unverified actor')), \
                mock.patch.dict(os.environ, dict(self.env, ASHA_HUB_SESSION_ID=self.sid), clear=True):
            with self.assertRaises(StoreError):
                memory_v2.publish(self.project, ACTIVE.replace('Done', 'Fake'), DECISIONS)
        self.assertEqual(before, self.digests())

    def test_ordinary_and_close_publications_cannot_authorize_explicit_save_omission(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            receipt = memory_v2.publish(self.project, ACTIVE, DECISIONS)
        self.assertNotIn('hub_session_id', receipt)
        receipt = self.publish(source='close')
        self.assertNotIn('hub_session_id', receipt)
        self.assertTrue(self.hub.close(self.sid)['closure']['capture']['requested'])

    def test_recording_failure_retains_successful_memory_receipt(self):
        with mock.patch('lib.control.session_publication.record_publication', side_effect=StoreError('unavailable')):
            receipt = self.publish()
        self.assertEqual(receipt['status'], 'published')
        self.assertEqual(receipt['hub_publication_status'], 'unavailable')
        self.assertEqual((self.memory / 'activeContext.md').read_text(), ACTIVE)
        self.assertTrue(self.hub.close(self.sid)['closure']['capture']['requested'])


class RoomSaveIntegration(review_fixture.ReviewFixture):
    def test_real_publication_review_and_disposition_in_one_verified_room(self):
        from lib.control import experience_cli, experience_adoption
        report_id = self.capture()['report_id']
        self.hub.stop(self.sid)
        room = self.launch(profile='room')
        self.hub.env.update(ASHA_HUB_SESSION_ID=room['session_id'], ASHA_HUB_GENERATION='1', ASHA_SESSION_PROFILE='room')
        with mock.patch('lib.control.harness.caller_descends_from', return_value=True), \
                mock.patch('lib.control.session_hub.Hub', return_value=self.hub), \
                mock.patch.dict(os.environ, self.hub.env, clear=True):
            publication = memory_v2.publish(self.project, ACTIVE, DECISIONS)
            result = experience_cli.manual_review(self.hub, self.pid, report_id,
                self.review_bytes(report_id), publication=publication)
            self.assertEqual(result['reviewer'], 'advisory-save-review')
            finding = experience_adoption.pending(self.hub, self.pid)['rows'][0]
            decision = {'review_id': result['review_id'], 'observation_key': 'retry',
                        'finding_digest': finding['finding_digest'], 'save_key': 'integration',
                        'disposition': 'reject', 'reason': 'Fixture scope only'}
            disposed = experience_adoption.dispose(self.hub, str(self.project), decision, publication,
                                                  save_session_id=room['session_id'])
        self.assertEqual(disposed['state'], 'completed')


if __name__ == '__main__':
    unittest.main()
