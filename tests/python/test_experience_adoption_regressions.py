import json
import sys
import unittest
import uuid
from unittest import mock

from tests.python import test_experience_end_to_end as journey
from tests.python import test_learnings_manager_v2 as lifecycle
from lib.control import experience_adoption as adoption
lm = lifecycle.lm
memory_v2 = lifecycle.memory_v2

class AdoptionBoundaries(unittest.TestCase):
    def test_policy_off_blocks_new_adoption(self):
        case = journey.ExperienceJourney('test_interrupted_disposition_reconciles_once_and_guidance_reaches_assignment')
        case.setUp(); self.addCleanup(case.doCleanups)
        self.enterContext(mock.patch.object(lm, 'learnings_dir', return_value=case.root / 'learning-fixture'))
        captured, row = case.reviewed()
        finding = adoption.pending(case.hub, case.pid)['rows'][0]
        case.experience.set_policy(str(case.project), 'off', expected_revision=2)
        snapshot = memory_v2.read_published_snapshot(case.project)
        publication = memory_v2.publish(case.project, snapshot.active_context.decode(), snapshot.decisions.decode(),
            expected_preimages=memory_v2.snapshot_digests(snapshot))
        decision = {'review_id': row['review_id'], 'observation_key': 'retry', 'finding_digest': finding['finding_digest'],
            'save_key': str(uuid.uuid4()), 'disposition': 'propose', 'reason': 'Inspected',
            'rule_id': 'policy-off-rule', 'trigger': 'When publishing', 'action': 'Compare baseline'}
        receipt = None
        try:
            receipt = adoption.dispose(case.hub, str(case.project), decision, publication, save_session_id='chair')
        except (ValueError, adoption.StoreError):
            pass
        print('policy-off', json.dumps({'receipt': receipt, 'files': [str(p.relative_to(case.root)) for p in (case.root / 'learning-fixture').rglob('*.md')]}))
        self.assertIsNone(receipt, 'new learning adopted after experience policy was switched off')

    def test_second_contradiction_from_same_source_resets_recent_positive_evidence(self):
        case = lifecycle.LearningLifecycleTests('test_proposal_is_candidate_without_confidence_or_tier')
        case.setUp(); self.addCleanup(case.tearDown)
        case.propose('s1', 'p1')
        lm.contradict('disk-pressure', project_dir=case.project, session_id='s1', reason='First contrary result')
        for sid, pid in [('s2', 'p1'), ('s3', 'p2'), ('s4', 'p2')]:
            lm.corroborate('disk-pressure', project_dir=case.project_for(pid), session_id=sid, reason='Later positive')
        self.assertTrue(lm.activate_if_eligible('disk-pressure', project_dir=case.project))
        lm.contradict('disk-pressure', project_dir=case.project, session_id='s1', reason='New contrary result after reactivation')
        activated = lm.activate_if_eligible('disk-pressure', project_dir=case.project)
        print('repeated contradiction', json.dumps({'reactivated': activated, 'evidence': [(e.session_id, e.kind, e.reason) for e in lm.load('disk-pressure').evidence]}))
        self.assertFalse(activated, 'second contradiction lost, allowing immediate reactivation from earlier evidence')


import concurrent.futures
import json
import sys
import threading
import unittest
import uuid
from unittest import mock

from tests.python import test_experience_end_to_end as fixture
from lib.control import experience_adoption as adoption
lm = fixture.lm
memory_v2 = fixture.memory_v2

class ReceiptRace(unittest.TestCase):
    def test_identical_concurrent_dispositions_have_one_immutable_receipt(self):
        case = fixture.ExperienceJourney('test_interrupted_disposition_reconciles_once_and_guidance_reaches_assignment')
        case.setUp(); self.addCleanup(case.doCleanups)
        self.enterContext(mock.patch.object(lm, 'learnings_dir', return_value=case.root / 'learning-fixture'))
        captured, row = case.reviewed()
        finding = adoption.pending(case.hub, case.pid)['rows'][0]
        snapshot = memory_v2.read_published_snapshot(case.project)
        publication = memory_v2.publish(case.project, snapshot.active_context.decode(), snapshot.decisions.decode(),
            expected_preimages=memory_v2.snapshot_digests(snapshot))
        lm.propose('receipt-race', 'When publishing', 'Compare baseline', project_dir=case.project,
            session_id='earlier-session', reason='Evidence', applicability=fixture.report()['observations'][0]['applicability'])
        second = case.root / 'project-two'; second.mkdir(); memory_v2.initialize(second)
        lm.corroborate('receipt-race', project_dir=second, session_id='second-session', reason='Evidence')
        decision = {'review_id': row['review_id'], 'observation_key': 'retry', 'finding_digest': finding['finding_digest'],
            'save_key': str(uuid.uuid4()), 'disposition': 'propose', 'reason': 'Inspected',
            'rule_id': 'receipt-race', 'trigger': 'When publishing', 'action': 'Compare baseline'}
        barrier = threading.Barrier(2)
        original = adoption._complete
        def completing(*args):
            barrier.wait(timeout=5)
            return original(*args)
        def execute():
            return adoption.dispose(case.hub, str(case.project), decision, publication, save_session_id='chair')
        with mock.patch.object(adoption, '_complete', side_effect=completing), concurrent.futures.ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(execute), pool.submit(execute)]
            receipts = [future.result(timeout=10) for future in futures]
        print('concurrent receipts', json.dumps(receipts))
        self.assertEqual(receipts[0], receipts[1], 'same disposition identity returned two different completed receipts')
