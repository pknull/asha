"""Sampled empty assessments can reveal misses without inventing worker observations."""
import json
import unittest
import uuid
from unittest import mock

from tests.python.test_control_session_experience import ExperienceFixture, report
from lib.control import session_experience as experience
from lib.control import experience_review as review, experience_cli, experience_adoption as adoption
from lib.control.session_closure import memory_v2
import learnings_manager as lm

SUBJECT = '@report-assessment'


class RoutineSample(ExperienceFixture):
    def setUp(self):
        super().setUp()
        self.experience.set_policy(str(self.project), 'review', expected_revision=1)

    def sampled(self):
        body = report('none-observed')
        body['summary'] = 'Completed; verification covered only the happy path.'
        body['evidence'][0]['text'] = 'Agent attestation: required empty-input behavior was never checked.'
        revision = self.experience.policy(self.pid)['revision']
        candidates = (uuid.uuid5(uuid.NAMESPACE_URL, 'routine-fixture:' + str(i)) for i in range(1000))
        rid = next(value for value in candidates
                   if experience.Experiences.selection(body, str(value), revision) == ('selected', 'routine-sample'))
        review_id = uuid.uuid4()
        retain = experience.Experiences._retain
        def controller_ids(*args, **kwargs):
            with mock.patch.object(experience.uuid, 'uuid4', side_effect=[rid, review_id]):
                return retain(*args, **kwargs)
        with mock.patch.object(experience.Experiences, '_retain', new=controller_ids):
            captured = self.capture(body, key=str(uuid.uuid4()))
        saved = self.experience.show(captured['report_id'])
        packet = review.packet(self.hub, saved['reviews'][0]['review_id'])
        finding = {'key': SUBJECT, 'verdict': 'supported', 'evidence_ids': ['check'],
            'contradictory_evidence_ids': [], 'inference': 'The attested checks omitted a required boundary.',
            'uncertainty': 'Agent attestation only; runtime behavior remains unknown.',
            'scope': 'This reported assignment.', 'destination': 'code-test',
            'check': 'Run the required empty-input case.', 'benefit': 'Verify a required boundary.',
            'regressions': 'Additional test maintenance.'}
        result = {'contract': 'asha.experience-review.v1', 'report_digest': saved['digest'],
            'packet_digest': review.packet_digest(packet), 'findings': [finding]}
        return body, saved, result, packet

    def test_sample_miss_reaches_explicit_adoption_once_with_original_lineage(self):
        body, saved, result, packet = self.sampled()
        self.assertIn(SUBJECT, packet)
        experience_cli.manual_review(self.hub, self.pid, saved['report_id'], json.dumps(result).encode())
        page = adoption.pending(self.hub, self.pid)
        self.assertEqual(page['total'], 1)
        finding = page['rows'][0]
        self.assertEqual(finding['subject_kind'], 'reviewer-report-assessment')
        self.assertEqual(self.experience.stats(self.pid)['yield']['routine_sample_actionable'], 1)
        self.assertEqual(self.experience.show(saved['report_id'])['body'], body)
        snapshot = memory_v2.read_published_snapshot(self.project)
        publication = memory_v2.publish(self.project, snapshot.active_context.decode(), snapshot.decisions.decode(),
            expected_preimages=memory_v2.snapshot_digests(snapshot))
        decision = {'review_id': finding['review_id'], 'observation_key': SUBJECT,
            'finding_digest': finding['finding_digest'], 'save_key': str(uuid.uuid4()),
            'disposition': 'propose', 'reason': 'Inspected the reported verification gap.',
            'rule_id': 'check-required-boundary', 'trigger': 'Before declaring the required case verified',
            'action': 'Run and inspect the required boundary check'}
        bundle = self.root / 'sample-learnings'
        with mock.patch.object(lm, 'learnings_dir', return_value=bundle):
            first = adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id='chair')
            self.assertEqual(first, adoption.dispose(self.hub, str(self.project), decision, publication, save_session_id='chair'))
            learning = lm.load(decision['rule_id'])
        self.assertEqual(learning.state, 'candidate')
        self.assertEqual(len(learning.evidence), 1)
        source = learning.evidence[0].source_provenance
        self.assertEqual(source['subject_kind'], 'reviewer-report-assessment')
        self.assertEqual(source['session_id'], self.sid)
        self.assertEqual(source['project_id'], self.pid)
        self.assertEqual(source['report_id'], saved['report_id'])
        self.assertEqual(source['observation_key'], SUBJECT)
        self.assertEqual(learning.applicability['project_ids'], [self.pid])
        self.assertEqual(learning.applicability['harnesses'], [saved['envelope']['harness']])
        print('ROUTINE_SAMPLE', json.dumps({'actionable': 1, 'pending_before_save': 1,
            'evidence_origins': 1, 'source_kind': source['subject_kind'], 'report_unchanged': True,
            'same_receipt': True, 'state': learning.state}))

    def test_empty_assessment_requires_one_declared_subject(self):
        _, saved, result, _ = self.sampled()
        for findings in ([], [dict(result['findings'][0], key='invented')], result['findings'] * 2):
            with self.subTest(findings=findings), self.assertRaises(ValueError):
                review.decode_result(json.dumps(dict(result, findings=findings)).encode(), saved, result['packet_digest'])

    def test_no_action_and_insufficient_evidence_are_valid_assessments(self):
        _, saved, result, _ = self.sampled()
        for verdict in ('no-action', 'insufficient-evidence'):
            finding = dict(result['findings'][0], verdict=verdict, evidence_ids=[], destination='no-change')
            value = dict(result, findings=[finding])
            self.assertEqual(review.decode_result(json.dumps(value).encode(), saved, result['packet_digest']), value)

    def test_positive_assessment_requires_known_evidence(self):
        _, saved, result, _ = self.sampled()
        for evidence_ids in ([], ['unknown']):
            value = dict(result, findings=[dict(result['findings'][0], evidence_ids=evidence_ids)])
            with self.subTest(evidence_ids=evidence_ids), self.assertRaisesRegex(ValueError, 'evidence'):
                review.decode_result(json.dumps(value).encode(), saved, result['packet_digest'])

    def test_worker_cannot_claim_controller_assessment_key(self):
        body = report(); body['observations'][0]['key'] = SUBJECT
        self.assertEqual(self.capture(body)['status'], 'invalid')

    def test_legacy_empty_review_stays_historical(self):
        body, saved, result, _ = self.sampled()
        old = experience.canonical({'review': dict(result, findings=[]), 'reviewer': 'operator-advisory',
                                    'cost_usd': None, 'tokens': None})
        with self.hub.database() as db, db.transaction(write=True) as c:
            c.execute("UPDATE hub_experience_reviews SET status='completed',result=? WHERE review_id=?",
                (old, saved['reviews'][0]['review_id']))
        self.assertEqual(adoption.pending(self.hub, self.pid)['total'], 0)
        self.assertEqual(self.experience.stats(self.pid)['yield']['routine_sample_actionable'], 0)
        self.assertEqual(self.experience.show(saved['report_id'])['body'], body)

    def test_provenance_subject_kind_is_strict_and_legacy_omission_survives(self):
        from tests.python.test_experience_adoption import AdoptionTests
        source = AdoptionTests().source()
        self.assertEqual(lm._source(source, 'chair-one', 'p1'), source)
        for kind in ('reviewer-report-assessment', 'worker-observation'):
            value = dict(source, subject_kind=kind)
            self.assertEqual(lm._source(value, 'chair-one', 'p1'), value)
        for kind in ('independently-verified', None, {}, True):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                lm._source(dict(source, subject_kind=kind), 'chair-one', 'p1')


if __name__ == '__main__':
    unittest.main()
