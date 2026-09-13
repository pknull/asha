import json
import sys
import unittest
from tests.python import test_control_experience_review as fixtures
from lib.control import experience_review as review

class AssignmentContext(unittest.TestCase):
    def fixture(self):
        case = fixtures.ReviewTests('test_claude_restriction_removes_tools_mcp_hooks_and_native_resume')
        case.setUp(); self.addCleanup(case.doCleanups)
        return case

    def test_report_packet_uses_actual_generation_assignment_and_freezes_it(self):
        case = self.fixture()
        first = case.selected()
        first_packet = review.packet(case.hub, first['review_id'])
        case.hub.stop(case.sid)
        case.hub.resume(case.sid, prompt='SECOND_GENERATION_ASSIGNMENT: inspect deployment documentation')
        second = case.selected()
        packet = review.packet(case.hub, second['review_id'])
        # Existing frozen report must not change when a later generation runs.
        self.assertEqual(first_packet, review.packet(case.hub, first['review_id']))
        actual = case.tmux.created[-1]['prompt'] if 'prompt' in case.tmux.created[-1] else str(case.tmux.created[-1])
        print('assignment context', json.dumps({'generation': case.hub.get(case.sid)['generation'],
            'continuation_present_in_packet': 'SECOND_GENERATION_ASSIGNMENT' in packet,
            'hub_original_prompt': case.hub.get(case.sid)['prompt']}))
        self.assertIn('SECOND_GENERATION_ASSIGNMENT', packet,
            'second generation review lacks the actual continuation assignment')

if __name__ == '__main__': unittest.main(verbosity=2)
