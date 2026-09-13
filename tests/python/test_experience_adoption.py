"""Observation provenance survives publisher changes and cross-store retries."""
import hashlib
import unittest
import unittest
from tests.python import test_learnings_manager_v2 as fixtures
lm = fixtures.lm


class AdoptionTests(unittest.TestCase):
    setUp = fixtures.LearningLifecycleTests.setUp
    tearDown = fixtures.LearningLifecycleTests.tearDown
    project_for = fixtures.LearningLifecycleTests.project_for
    capability = fixtures.LearningLifecycleTests.capability
    propose = fixtures.LearningLifecycleTests.propose
    def source(self, sid='worker-one', pid='p1', observation='observation-one', publisher='chair-one'):
        return {'session_id': sid, 'project_id': pid, 'origin_key': hashlib.sha256(observation.encode()).hexdigest(),
                'report_id': 'report-one', 'observation_key': observation, 'evidence_digest': 'a'*64,
                'adopting_save_identity': {'session_id': publisher, 'project_id': 'p1', 'publication_id': 'publication'},
                'harness': 'codex', 'harness_version': None}

    def test_publisher_and_reviewers_never_inflate_original_sessions(self):
        source = self.source()
        lm.propose('origin', 'When shared Memory changes', 'Compare the pre-draft baseline',
                   project_dir=self.project, session_id='chair-one', reason='Reviewed', source_provenance=source)
        for chair in ['chair-two', 'chair-three']:
            another = self.source(publisher=chair)
            lm.corroborate('origin', project_dir=self.project, session_id=chair, reason='Same observation', source_provenance=another)
        learning = lm.load('origin')
        self.assertEqual(len(learning.evidence), 1)
        self.assertEqual(learning.evidence[0].session_id, 'worker-one')
        self.assertFalse(lm.activate_if_eligible('origin', project_dir=self.project))

    def test_worker_batch_cannot_bypass_chair_candidate_limit(self):
        for i in range(3):
            lm.propose('rule'+str(i), 'Trigger', 'Action', project_dir=self.project, session_id='chair-one',
                       reason='Reviewed', source_provenance=self.source(sid='worker'+str(i), observation=str(i)))
        with self.assertRaisesRegex(ValueError, 'at most 3'):
            lm.propose('rule4', 'Trigger', 'Action', project_dir=self.project, session_id='chair-one', reason='Reviewed',
                       source_provenance=self.source(sid='worker4', observation='four'))

    def test_contradiction_from_prior_positive_source_is_retained(self):
        self.propose('s1','p1')
        lm.corroborate('disk-pressure', project_dir=self.project_for('p2'), session_id='s2', reason='Seen')
        lm.corroborate('disk-pressure', project_dir=self.project, session_id='s3', reason='Seen')
        self.assertTrue(lm.activate_if_eligible('disk-pressure', project_dir=self.project))
        lm.contradict('disk-pressure', project_dir=self.project, session_id='s1', reason='Contrary observation')
        self.assertEqual(lm.load('disk-pressure').evidence[-1].kind, 'contradict')
        self.assertFalse(lm.activate_if_eligible('disk-pressure', project_dir=self.project))
