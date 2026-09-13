import unittest
import uuid
from unittest import mock
from tests.python import test_control_experience_review as fixtures
from tests.python.test_control_session_experience import report
import learnings_manager as lm


class ExperienceMetrics(unittest.TestCase):
    def fixture(self):
        case = fixtures.ReviewTests('test_claude_restriction_removes_tools_mcp_hooks_and_native_resume')
        case.setUp(); self.addCleanup(case.doCleanups)
        return case

    def test_reserved_dispatch_is_not_reported_as_observed_native_launch(self):
        from lib.control import experience_review as review
        case = self.fixture(); selected = case.selected()
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture')):
            review.reserve(case.hub, selected['review_id'])
        cost = case.experience.stats(case.pid)['cost']
        self.assertEqual(cost['reservations'], 1)
        self.assertEqual(cost['launches'], 0)
        self.assertEqual(cost['unknown_launches'], 1)

    def test_feedback_is_one_current_attestation_per_generation_and_rule(self):
        case = self.fixture()
        self.enterContext(mock.patch.object(lm, 'learnings_dir', return_value=case.root / 'rules'))
        lm.save(lm.Learning('metric-rule', 'Before publication', 'Compare the baseline', state='active'), project_dir=case.project)
        for _ in range(2):
            message = case.hub.send(case.sid, 'Continue', key=str(uuid.uuid4()), learning_ids=['metric-rule'])
            emitted = next(m for m in case.hub.messages(case.sid) if m['message_id'] == message['message_id'])
            with case.acting_as(case.sid):
                case.hub.acknowledge(message['message_id'], delivery_digest=emitted['delivery_digest'])
        version = lm.rule_version(lm.load('metric-rule'))
        body = report()
        body['guidance_feedback'] = [{'id':'metric-rule', 'version':version, 'use':'applied',
                                      'evidence_ids':['check'], 'target_failure':'not-observed'}]
        case.capture(body); case.capture(body)
        stats = case.experience.stats(case.pid)
        self.assertEqual(stats['guidance']['supplied'], 2)
        self.assertEqual(stats['guidance']['reported_use']['applied'], 1)
        self.assertEqual(stats['guidance']['reported_use']['unknown'], 0)
        self.assertEqual(stats['guidance']['assignment_use_unknown'], 2)
        self.assertEqual(stats['recurrence']['not-observed'], 1)
        self.assertIsNone(stats['recurrence']['comparable_tasks'])
        other_revision = case.experience.stats(case.pid, policy_revision=999)
        self.assertEqual(other_revision['guidance']['supplied'], 0)
