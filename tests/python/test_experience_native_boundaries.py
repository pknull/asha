import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from tests.python import test_control_experience_review as fixtures
from lib.control import experience_review as review, sessions
from lib.control.session_store import SessionStore
from lib.control.session_harness import decode_claude

class Boundaries(unittest.TestCase):
    def fixture(self):
        case = fixtures.ReviewTests('test_claude_restriction_removes_tools_mcp_hooks_and_native_resume')
        case.setUp(); self.addCleanup(case.doCleanups)
        return case

    def test_same_revision_explicit_backfill_selects_unlaunched_report(self):
        case = self.fixture()
        row = case.selected()
        review.reconcile(case.hub)
        self.assertEqual(case.experience.show(row['report_id'])['reviews'][0]['status'], 'unsupported')
        receipt = review.backfill(case.hub, case.pid, [row['report_id']])
        current = case.experience.show(row['report_id'])['reviews'][0]
        print('backfill', json.dumps({'receipt': receipt, 'status': current['status']}))
        self.assertEqual(current['status'], 'selected', 'explicit unlaunched backfill silently did nothing')

    def test_oversized_native_result_is_not_accepted_after_decoder_truncation(self):
        case = self.fixture()
        row = case.selected()
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture')):
            reserved = review.reserve(case.hub, row['review_id'])
            report = case.experience.show(row['report_id'])
            valid = json.dumps({'contract': 'asha.experience-review.v1', 'report_digest': report['digest'],
                'packet_digest': reserved['packet_digest'], 'findings': [{'key': 'retry', 'verdict': 'supported',
                'evidence_ids': ['check'], 'contradictory_evidence_ids': [], 'inference': 'May help',
                'uncertainty': 'Fixture', 'scope': 'Project', 'destination': 'code-test', 'check': 'Check',
                'benefit': 'Benefit', 'regressions': 'Unknown'}]})
            output = valid + (' ' * (17000-len(valid))) + ' INVALID TRAILING CLAIM'
            class Transport:
                input_not_submitted = False
                def __init__(self, *args, **kwargs): pass
                def events(self, prompt, *, cancelled):
                    yield from decode_claude({'type': 'result', 'subtype': 'success', 'result': output, 'total_cost_usd': None})
            with SessionStore(case.config) as store:
                state = store.claim_owner(reserved['utility_id'])
                message = store.claim_turn(state['session_id'], state['generation'])
                sessions.run_turn(store, store.get(state['session_id']), message, env=case.env,
                    root=Path(__file__).resolve().parents[2], transport_factory=Transport)
            status = case.experience.show(row['report_id'])['reviews'][0]['status']
        print('native result', json.dumps({'native_bytes': len(output.encode()), 'status': status}))
        self.assertEqual(status, 'review-failed', 'truncated invalid native output became an accepted review')

if __name__ == '__main__': unittest.main(verbosity=2)
