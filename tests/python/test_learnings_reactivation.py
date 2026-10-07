"""A repeated contradiction fences earlier positive learning evidence.

Moved unchanged in meaning from the retired session experience adoption
regressions (N2); it exercises only the learnings manager.
"""
import unittest

from tests.python import test_learnings_manager_v2 as lifecycle

lm = lifecycle.lm


class ReactivationTests(unittest.TestCase):
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
        self.assertFalse(activated, 'second contradiction lost, allowing immediate reactivation from earlier evidence')


if __name__ == '__main__':
    unittest.main()
