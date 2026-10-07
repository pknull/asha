"""Best-effort close D3/D8: every successful save path retains one publication row.

Controller fixtures, not native delivery proof. The rows are the only "saved"
evidence: an explicit save, the close-path handoff, an ordinary handoff and a
no-durable-update attestation each insert one, naming their source. Finished
is never gated on them.
"""
import json
import os
import unittest
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture, ACTIVE, DECISIONS
from tests.python import test_memory_save_cas as save_fixture
from lib.control.session_closure import memory_v2
from lib.control.store import StoreError


class PublicationRowFixture(ClosureFixture):
    def rows(self, sid):
        with self.hub.database() as db, db.transaction() as c:
            found = c.execute('SELECT * FROM hub_memory_publications WHERE session_id=? ORDER BY published_at',
                              (sid,)).fetchall()
        return [dict(r, receipt=json.loads(r['receipt'])) for r in found]

    def sources(self, sid):
        return [row['receipt']['source'] for row in self.rows(sid)]


class PublicationRowTests(PublicationRowFixture):
    def test_ordinary_handoff_publication_inserts_a_handoff_row(self):
        sid = self.launch()['session_id']
        active, decisions = self.drafts()
        with self.acting_as(sid):
            result = self.hub.handoff(None, active_file=active, decisions_file=decisions, expected=self.digests())
        self.assertEqual(result['hub_publication_status'], 'recorded')
        self.assertEqual(self.sources(sid), ['handoff'])
        row, = self.rows(sid)
        self.assertEqual(row['generation'], 1)
        self.assertEqual(row['receipt']['status'], 'published')
        self.assertIsNotNone(self.hub.show(sid)['memory_saved_at'])

    def test_attestation_is_a_row_with_its_detail_and_counts_as_saved(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.handoff(None, outcome='no-durable-update', detail='Nothing durable changed')
        row, = self.rows(sid)
        self.assertEqual(row['receipt']['source'], 'attestation')
        self.assertEqual(row['receipt']['detail'], 'Nothing durable changed')
        self.assertIsNotNone(self.hub.show(sid)['memory_saved_at'])

    def test_blocked_handoff_inserts_no_row(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.handoff(None, outcome='blocked', detail='Memory is silenced')
        self.assertEqual(self.rows(sid), [])
        self.assertIsNone(self.hub.show(sid)['memory_saved_at'])

    def test_attestation_refused_under_silence_inserts_no_row(self):
        sid = self.launch()['session_id']
        marker = self.project / 'Work/markers/silence'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        with self.acting_as(sid), self.assertRaises(Exception):
            self.hub.handoff(None, outcome='no-durable-update', detail='Nothing')
        self.assertEqual(self.rows(sid), [])

    def test_close_path_handoff_inserts_a_close_row(self):
        sid = self.launch()['session_id']
        rid = self.hub.request_close(sid, wait=60)['closure']['request_id']
        active, decisions = self.drafts()
        with self.acting_as(sid):
            self.hub.handoff(rid, active_file=active, decisions_file=decisions, expected=self.digests())
        self.assertEqual(self.sources(sid), ['close'])

    def test_explicit_save_inserts_an_explicit_save_row(self):
        import os
        from unittest import mock
        from lib.control.session_closure import memory_v2
        row = self.launch(profile='room')
        with mock.patch.dict(os.environ, {'ASHA_HUB_SESSION_ID': row['session_id']}), mock.patch(
                'lib.control.session_publication.publication_actor', return_value=(self.hub, row)):
            receipt = memory_v2.publish(self.project, memory_v2.ACTIVE_TEMPLATE, memory_v2.DECISIONS_TEMPLATE)
        self.assertEqual(receipt['hub_publication_status'], 'recorded')
        self.assertNotIn('completion', receipt)
        self.assertEqual(self.sources(row['session_id']), ['explicit-save'])



class PublicationLinkageTests(PublicationRowFixture):
    """Publication identity comes from the verified actor, never receipt input.

    Moved from the retired session experience linkage tests (N2).
    """
    def setUp(self):
        super().setUp()
        self.sid = self.launch(profile='room')['session_id']

    def publish(self, source='explicit-save'):
        with self.acting_as(self.sid), mock.patch('lib.control.session_hub.Hub', return_value=self.hub), \
                mock.patch.dict(os.environ, dict(self.env, ASHA_HUB_SESSION_ID=self.sid,
                                                ASHA_HUB_GENERATION='1'), clear=True):
            return memory_v2.publish(self.project, ACTIVE, DECISIONS, publication_source=source)

    def test_unverified_actor_refuses_publication_without_writing_memory(self):
        before = self.digests()
        with mock.patch('lib.control.session_hub.Hub', return_value=self.hub), \
                mock.patch.object(self.hub, 'actor', side_effect=StoreError('unverified actor')), \
                mock.patch.dict(os.environ, dict(self.env, ASHA_HUB_SESSION_ID=self.sid), clear=True):
            with self.assertRaises(StoreError):
                memory_v2.publish(self.project, ACTIVE, DECISIONS)
        self.assertEqual(before, self.digests())
        self.assertEqual(self.rows(self.sid), [])

    def test_ordinary_and_close_source_publications_carry_no_hub_linkage(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            receipt = memory_v2.publish(self.project, ACTIVE, DECISIONS)
        self.assertNotIn('hub_session_id', receipt)
        self.assertNotIn('hub_session_id', self.publish(source='close'))
        self.assertEqual(self.rows(self.sid), [])

    def test_recording_failure_retains_successful_memory_receipt(self):
        with mock.patch('lib.control.session_publication.record_publication', side_effect=StoreError('unavailable')):
            receipt = self.publish()
        self.assertEqual(receipt['status'], 'published')
        self.assertEqual(receipt['hub_publication_status'], 'unavailable')
        self.assertEqual((self.memory / 'activeContext.md').read_text(), ACTIVE)


class PostCommitReceiptTests(unittest.TestCase):
    """Moved from the retired session experience review regressions (N2)."""
    def test_publication_receipt_survives_post_commit_config_failure(self):
        case = save_fixture.SaveCAS('test_user_publish_without_predraft_digests_refuses')
        case.setUp()
        self.addCleanup(case.doCleanups)
        publisher = save_fixture.memory_v2
        before = publisher.snapshot_digests(publisher.read_published_snapshot(case.root))
        original = publisher._remove_journal
        def after_commit(root):
            original(root)
            (root / '.asha/config.json').write_text('{broken')
        with mock.patch.object(publisher, '_remove_journal', side_effect=after_commit):
            result = publisher.publish(case.root, publisher.ACTIVE_TEMPLATE,
                                       '# Decisions\n\n- Published successfully.\n', expected_preimages=before)
        self.assertEqual(result['status'], 'published')
        self.assertIn('Published successfully', (case.root / 'Memory/decisions.md').read_text())


class FinishedAndLabelTests(PublicationRowFixture):
    def test_finished_is_ungated_and_reads_unsaved_without_a_row(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.report(state='finished', body='Done without saving')
            self.hub.observe('turn-stopped')
        shown = self.hub.show(sid)
        self.assertEqual(shown['activity'], 'finished')
        self.assertEqual(shown['next_step'], 'Finished, unsaved')

    def test_finished_after_a_save_reads_saved(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.handoff(None, outcome='no-durable-update', detail='Nothing durable')
            self.hub.report(state='finished', body='Done')
            self.hub.observe('turn-stopped')
        shown = self.hub.show(sid)
        self.assertIsNotNone(shown['memory_saved_at'])
        self.assertNotIn('unsaved', shown['next_step'])

    def test_label_shows_the_latest_save_in_the_generation_across_assignments(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.handoff(None, outcome='no-durable-update', detail='First')
        first = self.hub.show(sid)['memory_saved_at']
        self.hub._update(sid, assignment_epoch='another-assignment')
        self.assertEqual(self.hub.show(sid)['memory_saved_at'], first)
        with self.acting_as(sid):
            self.hub.handoff(None, outcome='no-durable-update', detail='Second')
        self.assertGreater(self.hub.show(sid)['memory_saved_at'], first)

    def test_a_new_generation_starts_unsaved(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.handoff(None, outcome='no-durable-update', detail='First')
        self.hub.stop(sid)
        self.hub.resume(sid, prompt='Continue', learning_ids=[])
        self.assertIsNone(self.hub.show(sid)['memory_saved_at'])
