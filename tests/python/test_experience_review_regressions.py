import json
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.python import test_control_experience_review as fixtures
from tests.python import test_memory_save_cas as save_fixture
memory_v2 = save_fixture.memory_v2
from lib.control import experience_review as review
from lib.control.session_store import SessionStore
from lib.control import sessions, runtime
from tests.python.test_control_session_experience import report


class CDBoundaries(unittest.TestCase):
    def fixture(self):
        case = fixtures.ReviewTests('test_claude_restriction_removes_tools_mcp_hooks_and_native_resume')
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def run_cancel(self, mode):
        case = self.fixture()
        selected = case.selected()
        probes = {}
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture')):
            row = review.reserve(case.hub, selected['review_id'])
            with SessionStore(case.config) as store:
                state = store.claim_owner(row['utility_id'])
                message = store.claim_turn(row['utility_id'], state['generation'])
                body = case.experience.show(row['report_id'])
                result = {'contract': 'asha.experience-review.v1', 'report_digest': body['digest'],
                          'packet_digest': row['packet_digest'], 'findings': [
                    {'key': 'retry', 'verdict': 'supported', 'evidence_ids': ['check'],
                     'contradictory_evidence_ids': [], 'inference': 'May help', 'uncertainty': 'Fixture',
                     'scope': 'Project', 'destination': 'code-test', 'check': 'A check',
                     'benefit': 'Fewer errors', 'regressions': 'Unknown'}]}
                class Transport:
                    input_not_submitted = False
                    def __init__(self, *args, **kwargs):
                        pass
                    def events(self, prompt, *, cancelled):
                        if mode == 'session-stop':
                            store.stop(row['utility_id'])
                        else:
                            runtime.set_admission(case.config, 'stopped')
                        probes['cancelled'] = cancelled()
                        yield 'completed', {'summary': json.dumps(result)}
                sessions.run_turn(store, store.get(row['utility_id']), message, env=case.env,
                    root=Path(__file__).resolve().parents[2], transport_factory=Transport)
                probes['managed_state'] = store.get(row['utility_id'])['state']
                probes['stop_requested'] = store.get(row['utility_id'])['stop_requested']
            probes['review_status'] = case.experience.show(row['report_id'])['reviews'][0]['status']
        print(mode, json.dumps(probes))
        self.assertTrue(probes['cancelled'], 'stop did not reach native cancellation callback')
        self.assertNotEqual(probes['review_status'], 'completed')

    def test_managed_stop_cancels_reviewer(self):
        self.run_cancel('session-stop')

    def test_runtime_stop_cancels_reviewer(self):
        self.run_cancel('runtime-stop')

    def test_publication_receipt_survives_post_commit_config_failure(self):
        case = save_fixture.SaveCAS('test_user_publish_without_predraft_digests_refuses')
        case.setUp()
        self.addCleanup(case.doCleanups)
        before = memory_v2.snapshot_digests(memory_v2.read_published_snapshot(case.root))
        original = memory_v2._remove_journal
        def after_commit(root):
            original(root)
            (root / '.asha/config.json').write_text('{broken')
        result = None
        error = None
        with mock.patch.object(memory_v2, '_remove_journal', side_effect=after_commit):
            try:
                result = memory_v2.publish(case.root, memory_v2.ACTIVE_TEMPLATE,
                    '# Decisions\n\n- Published successfully.\n', expected_preimages=before)
            except ValueError as exc:
                error = str(exc)
        persisted = (case.root / 'Memory/decisions.md').read_text()
        print('post-commit receipt', json.dumps({'receipt': result, 'error': error, 'persisted': persisted}))
        self.assertIsNotNone(result, 'committed publication lost its receipt to a subsequent config read')


class Concurrency(unittest.TestCase):
    def test_supersession_does_not_release_running_inference_slot(self):
        case = fixtures.ReviewTests('test_claude_restriction_removes_tools_mcp_hooks_and_native_resume')
        case.setUp(); self.addCleanup(case.doCleanups)
        selected = case.selected()
        probes = {}
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture')):
            row = review.reserve(case.hub, selected['review_id'])
            with SessionStore(case.config) as store:
                state = store.claim_owner(row['utility_id'])
                message = store.claim_turn(row['utility_id'], state['generation'])
                class Transport:
                    input_not_submitted = False
                    def __init__(self, *args, **kwargs): pass
                    def events(self, prompt, *, cancelled):
                        correction = report(); correction['summary'] = 'Corrected observation'
                        saved = case.capture(correction, supersedes=row['report_id'])
                        next_review = case.experience.show(saved['report_id'])['reviews'][0]
                        second = review.reserve(case.hub, next_review['review_id'])
                        probes['first_session_state'] = store.get(row['utility_id'])['state']
                        probes['first_review_status'] = case.experience.show(row['report_id'])['reviews'][0]['status']
                        probes['second_review_status'] = second['status']
                        probes['second_utility_id'] = second['utility_id']
                        yield 'failed', {}
                sessions.run_turn(store, store.get(row['utility_id']), message, env=case.env,
                    root=Path(__file__).resolve().parents[2], transport_factory=Transport)
        print(json.dumps(probes))
        self.assertEqual(probes['second_review_status'], 'budget-deferred',
                         'reserved another review before the superseded running provider finished')


class PacketSecrets(unittest.TestCase):
    def test_assignment_known_secret_is_not_copied_to_review_packet(self):
        class SecretFixture(fixtures.ReviewTests):
            def launch(self, **changes):
                return super().launch(**dict(changes, prompt='Run the check using api_key=fixture-only-not-real-secret'))
        case = SecretFixture('test_claude_restriction_removes_tools_mcp_hooks_and_native_resume')
        case.setUp(); self.addCleanup(case.doCleanups)
        selected = case.selected()
        try:
            packet = review.packet(case.hub, selected['review_id'])
        except ValueError:
            return
        self.assertNotIn('fixture-only-not-real-secret', packet,
                         'known secret assignment was copied into reviewer input')


class UncertainProvider(unittest.TestCase):
    def test_uncertain_provider_retains_concurrency_slot(self):
        case = fixtures.ReviewTests('test_claude_restriction_removes_tools_mcp_hooks_and_native_resume')
        case.setUp(); self.addCleanup(case.doCleanups)
        first, second = case.selected(), case.selected()
        with mock.patch.object(review, 'backend_support', return_value=(True, 'fixture')):
            reserved = review.reserve(case.hub, first['review_id'])
            with SessionStore(case.config) as store:
                state = store.claim_owner(reserved['utility_id'])
                message = store.claim_turn(state['session_id'], state['generation'])
                # Simulate durable owner-recovery state while its bound native
                # provider remains live. The liveness seam is mocked, so no
                # provider or subprocess is launched by this reproduction.
                with store.db.transaction(write=True) as c:
                    c.execute("UPDATE session_turns SET state='uncertain',provider_pid=999999,provider_identity='fixture-provider' WHERE turn_id=?", (message['turn_id'],))
                    c.execute("UPDATE managed_sessions SET state='uncertain' WHERE session_id=?", (state['session_id'],))
                    c.execute("UPDATE hub_experience_reviews SET status='uncertain' WHERE review_id=?", (first['review_id'],))
                with mock.patch('lib.control.session_store.process_live', return_value=True):
                    with store.db.transaction() as c:
                        self.assertTrue(store._provider_live(c, state['session_id']))
                    result = review.reserve(case.hub, second['review_id'])
        print('uncertain provider', json.dumps({'next_status': result['status']}))
        self.assertEqual(result['status'], 'budget-deferred', 'live provider no longer counts once delivery becomes uncertain')
