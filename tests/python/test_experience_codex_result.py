import json
import sys
import unittest
from tests.python import test_control_experience_review as fixtures
from tests.python import test_control_codex_protocol as wire_fixture
from tests.python.test_control_session_experience import report
from lib.control.session_store import SessionStore
from lib.control.session_experience import StructuredResult

class CodexResult(unittest.TestCase):
    def test_actual_codex_text_and_completion_capture_optin_envelope(self):
        class Structured(fixtures.ReviewTests):
            def launch(self, **changes):
                return super().launch(**dict(changes, harness='codex', transport='structured', result_contract='asha.session-result.v1'))
        case = Structured('test_claude_restriction_removes_tools_mcp_hooks_and_native_resume')
        case.setUp(); self.addCleanup(case.doCleanups)
        wire = wire_fixture.CodexProtocolTests('test_handshake_does_not_release_input_before_thread_identity')
        protocol = wire.protocol(); wire.ready(protocol)
        output = json.dumps({'contract': 'asha.session-result.v1', 'result': 'Done', 'experience': report()})
        events = wire.notify(protocol, 'item/agentMessage/delta', delta=output)
        events += wire.notify(protocol, 'turn/completed', turn={'id': 'turn-1', 'status': 'completed'})
        self.assertEqual(events[0], ('text', {'text': output}))
        with SessionStore(case.config) as store:
            state = store.claim_owner(case.sid)
            message = store.claim_turn(case.sid, state['generation'])
            collector = StructuredResult(case.hub, case.hub.get(case.sid), message['turn_id'])
            for kind, payload in events:
                result = collector.event(kind, payload)
                if kind == 'text':
                    self.assertIsNone(result, 'raw optional envelope text must not reach retained session events')
            self.assertEqual(result['summary'], 'Done')
        captured = case.experience.page(case.pid)['total']
        print('codex structured result', json.dumps({'completed_fields': list(payload), 'summary': result.get('summary'), 'reports': captured}))
        self.assertEqual(captured, 1, 'valid opt-in Codex result lost because its terminal event has no summary')

if __name__ == '__main__': unittest.main(verbosity=2)
