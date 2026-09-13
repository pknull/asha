import concurrent.futures
import json
import unittest
from pathlib import Path
from unittest import mock
from tests.python import test_control_experience_review as fixtures
from lib.control import experience_review as review, sessions
from lib.control.session_store import SessionStore


class ReviewLimits(unittest.TestCase):
    def fixture(self):
        case = fixtures.ReviewTests('test_claude_restriction_removes_tools_mcp_hooks_and_native_resume')
        case.setUp(); self.addCleanup(case.doCleanups)
        return case

    def test_concurrent_reservations_and_restart_reuse_one_dispatch(self):
        case = self.fixture(); selected = case.selected()
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture')):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: review.reserve(case.hub, selected['review_id']), range(2)))
            from lib.control.session_hub import Hub
            restarted = Hub(case.config, env=case.env, tmux=case.tmux)
            again = review.reserve(restarted, selected['review_id'])
        self.assertEqual({r['utility_id'] for r in [*results, again]}, {again['utility_id']})
        with SessionStore(case.config) as store, store.db.transaction() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM managed_sessions').fetchone()[0], 1)

    def run_refusal(self, boundary):
        case = self.fixture(); selected = case.selected()
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture')):
            reserved = review.reserve(case.hub, selected['review_id'])
            with SessionStore(case.config) as store:
                state = store.claim_owner(reserved['utility_id'])
                message = store.claim_turn(state['session_id'], state['generation'])
                started = []
                class Transport:
                    input_not_submitted = False
                    def __init__(self, *args, **kwargs):
                        started.append(True)
                    def events(self, prompt, *, cancelled):
                        if boundary == 'silence':
                            marker = case.project / 'Work/markers/silence'
                            marker.parent.mkdir(parents=True, exist_ok=True); marker.touch()
                            self.assert_cancelled = cancelled()
                        yield boundary if boundary in {'tool', 'permission'} else 'completed', {'summary':'{}'}
                if boundary == 'timeout':
                    reserved['reserved_at'] = 0
                review.run_review_turn(store, store.get(state['session_id']), message, reserved,
                    env=case.env, root=Path(__file__).resolve().parents[2], transport_factory=Transport)
                if boundary == 'timeout':
                    self.assertFalse(started)
        retained = case.experience.show(selected['report_id'])['reviews'][0]
        self.assertNotEqual(retained['status'], 'completed')
        self.assertIsNone(retained['result'])
        self.assertEqual(case.hub.get(case.sid)['lifecycle'], 'open')

    def test_native_tool_and_permission_frames_are_refused(self):
        for boundary in ('tool', 'permission'):
            with self.subTest(boundary=boundary):
                self.run_refusal(boundary)

    def test_expired_reservation_never_starts_native_transport(self):
        self.run_refusal('timeout')

    def test_silence_during_review_prevents_result_persistence(self):
        self.run_refusal('silence')
