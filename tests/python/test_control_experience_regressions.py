"""Independent initial A/B lifecycle regressions; fixture state stays temporary."""
import unittest
import uuid
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture
from tests.python.test_control_session_experience import ExperienceFixture
from lib.control.store import StoreError


class ExistingDatabase(ClosureFixture):
    def test_legacy_finished_report_with_existing_schema(self):
        # Historical launches predate guidance manifests. Reproduce that setup
        # without asking today's launch path to write a deliberately absent table.
        with mock.patch('lib.control.session_experience.SCHEMA', ()), \
             mock.patch('lib.control.session_guidance.retain'):
            row = self.launch()
        with self.hub.database() as db, db.transaction() as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('hub_experiences', tables)
        self.assertNotIn('hub_guidance_exposures', tables)
        with self.acting_as(row['session_id']):
            self.assertEqual(self.hub.report(state='finished', body='Done')['activity'], 'finished')
        with self.hub.database() as db, db.transaction() as c:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn('hub_experiences', tables)
        self.assertIn('hub_guidance_exposures', tables)


class ReceiptReview(ExperienceFixture):
    def test_changed_reference_under_same_delivery_key_is_refused(self):
        first = self.capture()
        second = self.capture()
        key = str(uuid.uuid4())
        with self.acting_as(self.sid):
            self.hub.report(state='finished', body='Done', experience_ref=first['report_id'], key=key)
            changed = self.hub.report(state='finished', body='Done', experience_ref=second['report_id'], key=key)
        self.assertEqual(changed['capture']['status'], 'invalid')

    def test_memory_only_retry_retains_existing_capture_receipt(self):
        close = self.hub.close(self.sid)['closure']
        active, decisions = self.drafts()
        with self.acting_as(self.sid):
            with self.assertRaises(StoreError):
                self.hub.handoff(close['request_id'], active_file=active, decisions_file=decisions,
                                 expected={}, experience_file=self.file())
            original = self.hub.get(self.sid)['closure']['capture']['report_id']
            result = self.hub.handoff(close['request_id'], active_file=active, decisions_file=decisions,
                                      expected=self.digests())
        self.assertEqual(result['capture']['report_id'], original)



from tests.python.test_control_session_experience import report
class SecurityReview(ExperienceFixture):
    def test_missing_tmux_visibility_cannot_authorize_known_worker(self):
        with mock.patch('lib.control.harness.caller_descends_from', return_value=True):
            with self.assertRaises(StoreError):
                self.experience.set_policy(str(self.project), 'review', expected_revision=1)
            with mock.patch('lib.control.rooms._owned_state', return_value=('missing', 'no server')):
                with self.assertRaises(StoreError):
                    self.experience.set_policy(str(self.project), 'review', expected_revision=1)

    def test_close_correction_can_retain_replacement_before_memory_retry(self):
        close = self.hub.close(self.sid)['closure']
        active, decisions = self.drafts()
        with self.acting_as(self.sid):
            with self.assertRaises(StoreError):
                self.hub.handoff(close['request_id'], active_file=active, decisions_file=decisions,
                                 expected={}, experience_file=self.file())
            original = self.hub.get(self.sid)['closure']['capture']['report_id']
            changed = report()
            changed['summary'] = 'The observation is corrected before the final handoff.'
            result = self.hub.handoff(close['request_id'], active_file=active, decisions_file=decisions,
                                      expected=self.digests(), experience_file=self.file(changed),
                                      supersedes=original, key=str(uuid.uuid4()))
        self.assertEqual(result['capture']['status'], 'captured')
        self.assertNotEqual(result['capture']['report_id'], original)

    def test_policy_revision_is_read_in_capture_transaction(self):
        self.experience.set_policy(str(self.project), 'review', expected_revision=1)
        from lib.control.session_experience import read_report
        def during_read(path, row):
            value = read_report(path, row)
            self.experience.set_policy(str(self.project), 'capture', expected_revision=2)
            return value
        with mock.patch('lib.control.session_experience.read_report', side_effect=during_read):
            captured = self.capture()
        saved = self.experience.show(captured['report_id'])
        self.assertEqual(saved['policy_revision'], 3)
        self.assertEqual(saved['reviews'][0]['status'], 'disabled')



class CorrectionRetryReview(ExperienceFixture):
    def test_memory_only_retry_keeps_latest_successful_correction(self):
        close = self.hub.close(self.sid)['closure']
        active, decisions = self.drafts()
        with self.acting_as(self.sid):
            with self.assertRaises(StoreError):
                self.hub.handoff(close['request_id'], active_file=active, decisions_file=decisions,
                                 expected={}, experience_file=self.file())
            original = self.hub.get(self.sid)['closure']['capture']['report_id']
            changed = report()
            changed['summary'] = 'Corrected observation before final handoff.'
            with self.assertRaises(StoreError):
                self.hub.handoff(close['request_id'], active_file=active, decisions_file=decisions,
                                 expected={}, experience_file=self.file(changed),
                                 supersedes=original, key=str(uuid.uuid4()))
            correction = self.hub.get(self.sid)['closure']['capture']['report_id']
            self.assertNotEqual(correction, original)
            result = self.hub.handoff(close['request_id'], active_file=active, decisions_file=decisions,
                                      expected=self.digests())
        self.assertEqual(result['capture']['report_id'], correction)

    def test_policy_disable_does_not_erase_previously_captured_close_receipt(self):
        close = self.hub.close(self.sid)['closure']
        active, decisions = self.drafts()
        with self.acting_as(self.sid), self.assertRaises(StoreError):
            self.hub.handoff(close['request_id'], active_file=active, decisions_file=decisions,
                             expected={}, experience_file=self.file())
        original = self.hub.get(self.sid)['closure']['capture']['report_id']
        self.experience.set_policy(str(self.project), 'off', expected_revision=1)
        with self.acting_as(self.sid):
            result = self.hub.handoff(close['request_id'], active_file=active, decisions_file=decisions,
                                      expected=self.digests())
        self.assertEqual(result['capture']['report_id'], original)
