import copy
import unittest
from unittest import mock

from lib.control.orchestration.current_actions import _head_demand
from lib.control.orchestration.model import record_digest
from lib.control.store import StoreError
from lib.control.tui_action_resolution import prepare, resolve
from tests.python.orchestration_execution_fixtures import ExecutionFixture


class ActionResolutionTests(ExecutionFixture, unittest.TestCase):
    approve_in_setup = False

    def candidate(self):
        head = self.store.peek(self.initiative_id)
        return {**_head_demand(head), 'initiative_id': self.initiative_id,
                'head_digest': record_digest(head), 'digest': record_digest(head)}

    def decide(self, row, review, choice, **kwargs):
        return resolve(self.store, row, review['review_digest'], choice,
                       env=self.env, tmux=mock.Mock(), **kwargs)

    def test_exact_plan_approval_changes_records_and_next_action_is_activation(self):
        row = self.candidate()
        review = prepare(self.store, row)
        self.assertEqual(review['subject']['plan']['digest'], self.plan['digest'])
        self.assertEqual(self.store.peek(self.initiative_id)['state'], 'awaiting-plan-approval')
        self.assertIn('plan approved', self.decide(row, review, 'approve'))
        self.assertEqual(self.store.peek(self.initiative_id)['state'], 'approved')
        next_review = prepare(self.store, self.candidate())
        self.assertEqual(next_review['choices'], ['activate'])
        with self.assertRaises(StoreError):
            self.decide(row, review, 'approve')

    def test_activation_uses_the_existing_doctor_gate_and_records_refusal(self):
        row = self.candidate()
        self.decide(row, prepare(self.store, row), 'approve')
        row = self.candidate(); review = prepare(self.store, row)
        with mock.patch('lib.control.orchestration.actions.run_orchestration_doctor',
                        return_value={'ok': False, 'probes': [{'name': 'runtime', 'outcome': 'mismatch'}]}):
            result = self.decide(row, review, 'activate')
        self.assertIn('refused', result)
        self.assertEqual(self.store.peek(self.initiative_id)['state'], 'approved')

    def test_stale_display_refuses_before_approval(self):
        row = self.candidate(); review = prepare(self.store, row)
        original = self.store.peek(self.initiative_id)
        changed = copy.deepcopy(original)
        changed.update(state='planning', state_revision=original['state_revision'] + 1)
        self.store.save_initiative(changed, expected_digest=record_digest(original))
        with self.assertRaisesRegex(StoreError, 'initiative changed'):
            self.decide(row, review, 'approve')
        self.assertEqual(self.store.list_approvals_snapshot(self.initiative_id), [])

    def test_replaced_review_token_and_wrong_action_are_not_authorization(self):
        row = self.candidate(); review = prepare(self.store, row)
        with self.assertRaisesRegex(StoreError, 'reviewed subject changed'):
            self.decide(row, {**review, 'review_digest': '0' * 64}, 'approve')
        with self.assertRaisesRegex(StoreError, 'not offered'):
            self.decide(row, review, 'activate')
        self.assertEqual(self.store.list_approvals_snapshot(self.initiative_id), [])

    def test_managed_coordinator_cannot_use_global_queue_to_self_approve(self):
        row = self.candidate(); review = prepare(self.store, row)
        with self.assertRaisesRegex(StoreError, 'managed actors'):
            resolve(self.store, row, review['review_digest'], 'approve',
                    env={**self.env, 'ASHA_MANAGED_SESSION_ID': 'actor'}, tmux=mock.Mock())
        self.assertEqual(self.store.list_approvals_snapshot(self.initiative_id), [])

    def test_rejection_requires_reason_and_uses_existing_plan_rejection(self):
        row = self.candidate(); review = prepare(self.store, row)
        with self.assertRaisesRegex(StoreError, 'reason'):
            self.decide(row, review, 'reject')
        self.assertEqual(self.decide(row, review, 'reject', reason='Narrow the scope'), 'plan rejected')
        self.assertEqual(self.store.peek(self.initiative_id)['state'], 'planning')

    def test_control_review_then_explicit_approval_updates_the_same_records(self):
        from lib.control import tui, tui_action_queue
        row = self.candidate()
        with mock.patch.object(tui_action_queue, '_inspect', return_value='review') as inspect, \
             mock.patch.object(tui, '_prompt_line', return_value='approve'), \
             mock.patch.object(tui, '_coordinator_tmux', return_value=mock.Mock()), \
             mock.patch.object(tui, '_refresh_initiatives'):
            result = tui_action_queue._resolve(None, None, tui.TuiModel([]), self.env, self.store, row)
        self.assertIn('plan approved', result)
        self.assertEqual(inspect.call_args.args[2]['subject']['plan']['digest'], self.plan['digest'])
        self.assertEqual(self.store.peek(self.initiative_id)['state'], 'approved')

    def test_closing_review_does_not_approve_or_open_a_confirmation(self):
        from lib.control import tui, tui_action_queue
        with mock.patch.object(tui_action_queue, '_inspect', return_value=None), \
             mock.patch.object(tui, '_prompt_line') as prompt:
            tui_action_queue._resolve(None, None, tui.TuiModel([]), self.env, self.store, self.candidate())
        prompt.assert_not_called()
        self.assertEqual(self.store.list_approvals_snapshot(self.initiative_id), [])

    def test_refresh_failure_does_not_misreport_a_committed_approval(self):
        from lib.control import tui, tui_action_queue
        with mock.patch.object(tui_action_queue, '_inspect', return_value='review'), \
             mock.patch.object(tui, '_prompt_line', return_value='approve'), \
             mock.patch.object(tui, '_coordinator_tmux', return_value=mock.Mock()), \
             mock.patch.object(tui, '_refresh_initiatives', side_effect=StoreError('unavailable')):
            result = tui_action_queue._resolve(None, None, tui.TuiModel([]), self.env, self.store, self.candidate())
        self.assertIn('plan approved', result)
        self.assertIn('display refresh unavailable', result)
        self.assertEqual(self.store.peek(self.initiative_id)['state'], 'approved')


class SQLiteActionResolutionTests(ActionResolutionTests):
    def setUp(self):
        from lib.control.database import ControlDatabase
        from lib.control.orchestration.sqlite_store import SQLiteInitiativeStore
        def factory(config):
            with ControlDatabase(config.control, create=True):
                pass
            return SQLiteInitiativeStore(config)
        with mock.patch('tests.python.orchestration_execution_fixtures.InitiativeStore', side_effect=factory):
            super().setUp()


class ReviewRetryResolutionTests(ExecutionFixture, unittest.TestCase):
    from tests.python.test_orchestration_review_budget import ReviewBudgetTests as _fixture
    _dispatch_review = _fixture._dispatch_review
    _publish = _fixture._publish
    _complete = _fixture._complete
    exhaust = _fixture.exhaust
    request = _fixture.request

    def setUp(self):
        from tests.python.orchestration_increment3_fixtures import advance_node, save_candidate
        from lib.control.orchestration.scheduler import refresh_readiness
        super().setUp()
        self.candidate_seal = save_candidate(self)
        self.candidate = self.candidate_seal
        advance_node(self, 'implementation-a', ['evaluating', 'succeeded'])
        refresh_readiness(self.store, self.initiative_id)
        self._dispatch_review()

    def test_queue_approves_only_the_displayed_exhausted_review_request(self):
        from lib.control.orchestration.actions import action_outcome
        from lib.control.orchestration.current_actions import approval_demand
        self.exhaust()
        _, action = self.request()
        request = self.store.read_approval(self.initiative_id, action_outcome(action)['request_id'])
        head = self.initiative()
        row = {**approval_demand(request, head), 'initiative_id': self.initiative_id,
               'head_digest': record_digest(head), 'digest': record_digest(request)}
        review = prepare(self.store, row)
        self.assertEqual(review['subject']['binding']['target']['seal_id'], self.candidate_seal['seal_id'])
        result = resolve(self.store, row, review['review_digest'], 'approve', env=self.env, tmux=mock.Mock())
        self.assertEqual(result, 'one review retry approved')
        self.assertEqual(self.store.read_approval(self.initiative_id, request['request_id'])['state'], 'approved')
        self.assertEqual(self.store.read_seal(self.initiative_id, self.candidate_seal['seal_id']), self.candidate_seal)
        with self.assertRaises(StoreError):
            resolve(self.store, row, review['review_digest'], 'approve', env=self.env, tmux=mock.Mock())


class SalvageResolutionTests(ExecutionFixture, unittest.TestCase):
    from tests.python.test_orchestration_salvage import OrchestrationRecoveryActionTests as _fixture
    seal = _fixture.seal
    request = _fixture.request

    def test_queue_approval_preserves_the_exact_failure_seal(self):
        from lib.control.orchestration.current_actions import approval_demand
        failure = self.seal('failure')
        _, rid = self.request(failure)
        approval = self.store.read_approval(self.initiative_id, rid)
        head = self.initiative()
        row = {**approval_demand(approval, head), 'initiative_id': self.initiative_id,
               'head_digest': record_digest(head), 'digest': record_digest(approval)}
        reviewed = prepare(self.store, row)
        self.assertEqual(reviewed['subject']['failure_seal'], failure)
        self.assertEqual(resolve(self.store, row, reviewed['review_digest'], 'approve',
                                 env=self.env, tmux=mock.Mock()), 'salvage request approved')
        self.assertEqual(self.store.read_approval(self.initiative_id, rid)['state'], 'approved')
        self.assertEqual(self.store.read_seal(self.initiative_id, failure['seal_id']), failure)

    def test_queue_can_decline_a_request_without_discarding_its_evidence(self):
        from lib.control.orchestration.current_actions import approval_demand
        failure = self.seal('failure')
        _, rid = self.request(failure)
        approval = self.store.read_approval(self.initiative_id, rid)
        head = self.initiative()
        row = {**approval_demand(approval, head), 'initiative_id': self.initiative_id,
               'head_digest': record_digest(head), 'digest': record_digest(approval)}
        reviewed = prepare(self.store, row)
        result = resolve(self.store, row, reviewed['review_digest'], 'reject', env=self.env, tmux=mock.Mock())
        self.assertEqual(result, 'request rejected')
        retained = self.store.read_approval(self.initiative_id, rid)
        self.assertEqual(retained['state'], 'rejected')
        self.assertEqual(retained['rationale'], approval['rationale'])
        self.assertEqual(self.store.read_seal(self.initiative_id, failure['seal_id']), failure)

    def test_rejection_cli_inspection_and_replay_preserve_one_decision(self):
        import io
        import json
        from contextlib import redirect_stdout
        from lib.control.orchestration import cli
        failure = self.seal('failure')
        _, rid = self.request(failure)
        def invoke(args):
            out = io.StringIO()
            with mock.patch.object(cli, 'InitiativeStore', return_value=self.store), redirect_stdout(out):
                code = cli.main(['initiative', *args], env=self.env)
            return code, json.loads(out.getvalue())
        status, inspected = invoke(['approval', self.initiative_id, '--request', rid, '--json'])
        self.assertEqual(status, 0)
        self.assertEqual(inspected['digest'], record_digest(inspected['approval']))
        args = ['reject-request', self.initiative_id, '--request', rid, '--digest', inspected['digest'], '--json']
        first = invoke(args)
        self.assertEqual(first, invoke(args))
        self.assertEqual(first[1]['approval']['state'], 'rejected')
        edges = [e for e in self.store.list_events_snapshot(self.initiative_id)
                 if e['type'] == 'approval-decided' and rid in e['subject_ids']]
        self.assertEqual(len(edges), 1)

    def test_rejection_refuses_a_request_changed_since_inspection(self):
        from lib.control.orchestration.actions import ActionRefused
        from lib.control.orchestration.request_decisions import reject
        _, rid = self.request(self.seal('failure'))
        original = self.store.read_approval(self.initiative_id, rid)
        changed = {**original, 'rationale': 'Updated evidence to inspect'}
        self.store.save_approval(self.initiative_id, changed, expected_digest=record_digest(original))
        with self.assertRaisesRegex(ActionRefused, 'request changed'):
            reject(self.store, self.initiative_id, rid, expected_digest=record_digest(original))
        self.assertEqual(self.store.read_approval(self.initiative_id, rid), changed)

    def test_cli_managed_actor_cannot_reject_requests(self):
        from lib.control.orchestration.cli import _reject_request_command
        _, rid = self.request(self.seal('failure'))
        original = self.store.read_approval(self.initiative_id, rid)
        with self.assertRaisesRegex(ValueError, 'operator verbs are refused'):
            _reject_request_command(
                [self.initiative_id, '--request', rid, '--digest', record_digest(original)],
                self.store, {**self.env, 'ASHA_MANAGED_SESSION_ID': 'actor'}, mock.Mock())
        self.assertEqual(self.store.read_approval(self.initiative_id, rid), original)

    def test_rejection_cannot_revoke_an_approved_request(self):
        from lib.control.orchestration.actions import ActionRefused, approve_salvage
        from lib.control.orchestration.request_decisions import reject
        _, rid = self.request(self.seal('failure'))
        approve_salvage(self.store, self.initiative_id, rid)
        original = self.store.read_approval(self.initiative_id, rid)
        with self.assertRaisesRegex(ActionRefused, 'cannot revoke'):
            reject(self.store, self.initiative_id, rid, expected_digest=record_digest(original))
        self.assertEqual(self.store.read_approval(self.initiative_id, rid), original)

    def test_rejection_retry_repairs_a_missing_event_without_resigning(self):
        from lib.control.orchestration import request_decisions
        _, rid = self.request(self.seal('failure'))
        original = self.store.read_approval(self.initiative_id, rid)
        digest = record_digest(original)
        with mock.patch.object(request_decisions, 'append_event', side_effect=OSError('interrupted journal write')):
            with self.assertRaisesRegex(StoreError, 'interrupted journal write'):
                request_decisions.reject(self.store, self.initiative_id, rid,
                                         expected_digest=digest, actor_id='tui')
        retained = self.store.read_approval(self.initiative_id, rid)
        self.assertEqual(retained['state'], 'rejected')
        for _ in range(2):
            replay = request_decisions.reject(self.store, self.initiative_id, rid,
                                              expected_digest=digest, actor_id='cli')
            self.assertEqual(replay, retained)
        edges = [e for e in self.store.list_events_snapshot(self.initiative_id)
                 if e['type'] == 'approval-decided' and rid in e['subject_ids']]
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]['actor_id'], 'tui')

    def test_non_object_event_payload_is_not_a_rejection_receipt(self):
        import hashlib
        from lib.control.orchestration.actions import append_event
        from lib.control.orchestration.request_decisions import reject
        _, rid = self.request(self.seal('failure'))
        original = self.store.read_approval(self.initiative_id, rid)
        with mock.patch.object(self.store, 'append_event'):
            event = append_event(self.store, self.initiative_id, 'approval-decided', [rid], {},
                                 actor_kind='controller', actor_id='controller')
        event.update(payload=None, payload_digest=hashlib.sha256(b'null').hexdigest())
        self.store.append_event(self.initiative_id, event)
        result = reject(self.store, self.initiative_id, rid, expected_digest=record_digest(original))
        self.assertEqual(result['state'], 'rejected')
        events = self.store.list_events_snapshot(self.initiative_id)
        self.assertEqual(events[-1]['payload']['decision'], 'rejected')


class SQLiteSalvageResolutionTests(SalvageResolutionTests):
    def setUp(self):
        from lib.control.database import ControlDatabase
        from lib.control.orchestration.sqlite_store import SQLiteInitiativeStore
        def factory(config):
            with ControlDatabase(config.control, create=True):
                pass
            return SQLiteInitiativeStore(config)
        with mock.patch('tests.python.orchestration_execution_fixtures.InitiativeStore', side_effect=factory):
            super().setUp()
