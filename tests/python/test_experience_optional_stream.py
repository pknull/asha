import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from tests.python import test_control_experience_review as fixtures
from tests.python.test_control_session_experience import report
from lib.control import sessions
from lib.control.session_store import SessionStore
from lib.control.session_harness import decode_claude

class OptionalStream(unittest.TestCase):
    def run_stream(self, failed):
        class Structured(fixtures.ReviewTests):
            def launch(self, **changes):
                return super().launch(**dict(changes, harness='claude', transport='structured', result_contract='asha.session-result.v1'))
        case = Structured('test_claude_restriction_removes_tools_mcp_hooks_and_native_resume')
        case.setUp(); self.addCleanup(case.doCleanups)
        value = report(); value['evidence'][0]['text'] = 'api_key=fixture-private-optional-stream'
        raw = json.dumps({'contract': 'asha.session-result.v1', 'result': 'Done', 'experience': value})
        observed = {}
        with SessionStore(case.config) as store:
            state = store.claim_owner(case.sid)
            message = store.claim_turn(case.sid, state['generation'])
            def leaked():
                with store.db.transaction() as c:
                    return bool(c.execute("SELECT 1 FROM session_events WHERE instr(payload,?)>0 LIMIT 1", ('fixture-private-optional-stream',)).fetchone()
                        or c.execute("SELECT 1 FROM records WHERE instr(CAST(payload AS TEXT),?)>0 LIMIT 1", ('fixture-private-optional-stream',)).fetchone())
            class Transport:
                input_not_submitted = False
                def __init__(self, *args, **kwargs): pass
                def events(self, prompt, *, cancelled):
                    yield 'text', {'text': raw}
                    observed['persisted_before_terminal'] = leaked()
                    yield from decode_claude({'type': 'result', 'subtype': 'error_during_execution' if failed else 'success',
                        'is_error': failed, 'result': raw})
            # Isolate only event processing: permission IPC is unrelated to
            # this synthetic stream and unsupported in the restricted sandbox.
            with mock.patch('lib.control.session_ipc.SessionRequestServer'):
                sessions.run_turn(store, store.get(case.sid), message, env=case.env,
                    root=Path(__file__).resolve().parents[2], transport_factory=Transport)
            observed['persisted_after_terminal'] = leaked()
        observed.update(failed=failed, capture=case.hub.get(case.sid).get('capture'))
        print('optional stream', json.dumps(observed))
        self.assertFalse(observed['persisted_before_terminal'])
        self.assertFalse(observed['persisted_after_terminal'], 'rejected optional evidence leaked into retained terminal output')

    def test_secret_optin_completed_stream_is_not_retained(self):
        self.run_stream(False)

    def test_secret_optin_failed_stream_is_not_retained(self):
        self.run_stream(True)

if __name__ == '__main__': unittest.main(verbosity=2)
