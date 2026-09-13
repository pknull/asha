import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from tests.python import test_control_experience_review as fixtures
from tests.python.test_control_session_experience import report
from lib.control import experience_review as review, sessions, experience_cli
from lib.control.session_store import SessionStore
from lib.control.session_harness import decode_claude

class Boundaries(unittest.TestCase):
    def fixture(self, cls=None):
        case = (cls or fixtures.ReviewTests)('test_claude_restriction_removes_tools_mcp_hooks_and_native_resume')
        case.setUp(); self.addCleanup(case.doCleanups)
        return case

    def result(self, case, row, packet_hash):
        body = case.experience.show(row['report_id'])
        return {'contract': 'asha.experience-review.v1', 'report_digest': body['digest'],
            'packet_digest': packet_hash, 'findings': [{'key': 'retry', 'verdict': 'supported',
            'evidence_ids': ['check'], 'contradictory_evidence_ids': [], 'inference': 'May help',
            'uncertainty': 'Fixture', 'scope': 'Project', 'destination': 'code-test', 'check': 'Check',
            'benefit': 'Benefit', 'regressions': 'Unknown'}]}

    def test_manual_review_obeys_policy_off(self):
        case = self.fixture()
        row = case.selected()
        raw = json.dumps(self.result(case, row, review.packet_digest(review.packet(case.hub, row['review_id'])))).encode()
        case.experience.set_policy(str(case.project), 'off', expected_revision=2)
        receipt = None
        try:
            receipt = experience_cli.manual_review(case.hub, case.pid, row['report_id'], raw)
        except (ValueError, review.StoreError): pass
        print('manual off', json.dumps({'receipt': receipt, 'status': case.experience.show(row['report_id'])['reviews'][0]['status']}))
        self.assertIsNone(receipt, 'manual review persists new result under policy off')

    def test_native_cost_telemetry_cannot_store_known_secrets(self):
        case = self.fixture()
        row = case.selected()
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture')):
            reserved = review.reserve(case.hub, row['review_id'])
            valid = json.dumps(self.result(case, row, reserved['packet_digest']))
            class Transport:
                input_not_submitted = False
                def __init__(self, *args, **kwargs): pass
                def events(self, prompt, *, cancelled):
                    yield from decode_claude({'type': 'result', 'subtype': 'success', 'result': valid,
                        'total_cost_usd': 'api_key=fixture-only-secret'})
            with SessionStore(case.config) as store:
                state = store.claim_owner(reserved['utility_id'])
                message = store.claim_turn(state['session_id'], state['generation'])
                sessions.run_turn(store, store.get(state['session_id']), message, env=case.env,
                    root=Path(__file__).resolve().parents[2], transport_factory=Transport)
            saved = case.experience.show(row['report_id'])['reviews'][0]
        print('native telemetry', json.dumps({'status': saved['status'], 'secret_retained': 'fixture-only-secret' in (saved['result'] or '')}))
        self.assertNotIn('fixture-only-secret', saved['result'] or '', 'untyped native telemetry bypassed content protection')

    def test_structured_optin_rejects_truncated_native_envelope(self):
        class Structured(fixtures.ReviewTests):
            def launch(self, **changes):
                return super().launch(**dict(changes, transport='structured', result_contract='asha.session-result.v1'))
        case = self.fixture(Structured)
        valid = json.dumps({'contract': 'asha.session-result.v1', 'result': 'Done', 'experience': report()})
        output = valid + (' ' * (17000-len(valid))) + ' INVALID TRAILING CLAIM'
        class Transport:
            input_not_submitted = False
            def __init__(self, *args, **kwargs): pass
            def events(self, prompt, *, cancelled):
                yield from decode_claude({'type': 'result', 'subtype': 'success', 'result': output})
        with SessionStore(case.config) as store:
            state = store.claim_owner(case.sid)
            message = store.claim_turn(case.sid, state['generation'])
            from lib.control.session_experience import structured_completion
            kind, payload = next(iter(decode_claude({'type': 'result', 'subtype': 'success', 'result': output})))
            # Actual decoder -> parser boundary used by sessions.run_turn. Full
            # owner IPC is unavailable in this sandbox and is not claimed here.
            structured_completion(case.hub, case.hub.get(case.sid), message['turn_id'], payload['summary'], truncated=payload.get('summary_truncated', False))
        captured = case.experience.page(case.pid)['total']
        print('structured truncation', json.dumps({'native_bytes': len(output.encode()), 'captured': captured, 'capture': case.hub.get(case.sid).get('capture')}))
        self.assertEqual(captured, 0, 'truncated invalid native envelope was accepted as valid experience')

if __name__ == '__main__': unittest.main(verbosity=2)
