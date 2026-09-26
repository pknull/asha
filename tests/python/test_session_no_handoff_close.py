"""Issue #101 (c): receipt state in Control and a graceful close without a handoff turn.

Controller fixtures only: the scripted observations stand in for native hooks
and prove no native delivery.
"""
import io
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests.python.test_control_session_closure import ACTIVE, ClosureFixture
from tests.python import test_session_completion
from lib.control import hub_cli, session_tui
from lib.control import session_closure as closure
from lib.control.session_presentation import present
from lib.control.store import StoreError


class Fixture(ClosureFixture):
    """close --no-handoff is experimental (#103): these fixtures opt in."""
    save = test_session_completion.CompletionTests.save

    def setUp(self):
        super().setUp()
        self.configure_control(no_handoff_close=True)


class ReceiptStateTests(Fixture):
    def receipt(self, sid):
        return self.hub.show(sid)['completion_readiness']

    def test_new_session_has_no_receipt(self):
        sid = self.launch()['session_id']
        state = self.receipt(sid)
        self.assertEqual(state['receipt'], 'none')
        self.assertIsNone(state['stale_since'])

    def test_saved_receipt_is_current_with_its_finalized_time(self):
        sid = self.launch()['session_id']
        before = time.time()
        self.save(sid)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        state = self.receipt(sid)
        self.assertEqual(state['receipt'], 'current')
        self.assertGreaterEqual(state['finalized_at'], before)
        self.assertIsNone(state['stale_since'])
        listed = next(r for r in self.hub.list()['rows'] if r['session_id'] == sid)
        self.assertEqual(listed['completion_readiness']['receipt'], 'current')

    def test_further_work_makes_the_receipt_stale_since_that_event(self):
        for event in ('prompt-submitted', 'tool-started', 'send'):
            with self.subTest(event=event):
                sid = self.launch()['session_id']
                self.save(sid)
                finalized = self.hub.get(sid)['completion']['finalized_at']
                before = time.time()
                with self.acting_as(sid):
                    if event == 'send':
                        self.hub.send(sid, 'More work', key='more')
                    else:
                        self.hub.observe(event)
                after = time.time()
                state = self.receipt(sid)
                self.assertEqual(state['receipt'], 'stale')
                self.assertGreaterEqual(state['stale_since'], max(before, finalized))
                self.assertLessEqual(state['stale_since'], after)
                self.assertTrue(state['reason'])
                # Later work never moves the first moment the receipt went stale.
                with self.acting_as(sid):
                    self.hub.observe('turn-stopped')
                self.assertEqual(self.receipt(sid)['stale_since'], state['stale_since'])
                self.hub.stop(sid)

    def test_a_newer_memory_publication_makes_the_receipt_stale(self):
        import memory_v2
        sid = self.launch()['session_id']
        self.save(sid)
        finalized = self.hub.get(sid)['completion']['finalized_at']
        memory_v2.publish(self.project, ACTIVE, '# Decisions\n\n- Newer.\n')
        state = self.receipt(sid)
        self.assertEqual(state['receipt'], 'stale')
        self.assertIn('Memory changed', state['reason'])
        self.assertIsNotNone(state['stale_since'])
        self.assertGreaterEqual(state['stale_since'], finalized)

    def test_blocked_handoff_is_not_a_receipt(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.handoff(None, outcome='blocked', detail='Permission denied')
        self.assertEqual(self.receipt(sid)['receipt'], 'none')

    def test_dashboard_shows_the_receipt_state(self):
        row = dict(session_id='one', generation=1, activity='idle', project_name='asha', name='Session',
                   harness='claude', transport='terminal', profile='worker', lifecycle='open',
                   reason='Observed', process_state='live')
        cases = [({'receipt': 'current', 'status': 'ready', 'finalized_at': 0.0, 'stale_since': None}, 'receipt current'),
                 ({'receipt': 'stale', 'status': 'stale', 'finalized_at': 0.0, 'stale_since': 3600.0},
                  'receipt stale since 01:00 UTC'),
                 ({'receipt': 'stale', 'status': 'stale', 'finalized_at': 0.0, 'stale_since': None}, 'receipt stale'),
                 ({'receipt': 'none', 'status': 'missing', 'stale_since': None}, 'no receipt')]
        for readiness, text in cases:
            with self.subTest(text=text):
                rendered = '\n'.join(session_tui.lines({'rows': [dict(row, completion_readiness=readiness)]}, width=140))
                self.assertIn(text, rendered)


class NoHandoffCloseTests(Fixture):
    def idle(self, harness='claude', **stop):
        sid = self.launch(harness=harness)['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.observe('turn-stopped', **stop)
        return sid

    def assert_open(self, sid):
        row = self.hub.get(sid)
        self.assertIn(row['lifecycle'], {'open', 'closing'})
        self.assertEqual(self.tmux.killed, [])
        return row

    def test_idle_session_without_receipt_closes_without_a_turn_or_claim(self):
        for harness in ('claude', 'codex'):
            with self.subTest(harness=harness):
                sid = self.idle(harness)
                starts = len(self.tmux.created)
                result = self.hub.close(sid, no_handoff=True)
                self.assertEqual(result['lifecycle'], 'closed')
                record = result['closure']
                self.assertEqual(record['state'], 'closed-no-save-claimed')
                self.assertEqual(record['delivery']['channel'], 'no-handoff')
                self.assertFalse(record['memory']['saved'])
                self.assertIsNone(record['handoff'])
                self.assertFalse(record['attention'])
                self.assertIn('no project-memory save was claimed', record['guidance'])
                self.assertEqual(self.hub.messages(sid), [])
                self.assertEqual(len(self.tmux.created), starts)
                self.assertTrue(self.tmux.killed)
                self.assertNotIn(sid, [r['session_id'] for r in self.hub.list()['rows']])
                self.assertIsNone(result.get('memory_saved_at'))
                self.assertNotIn('Memory saved', result['reason'])
                self.tmux.killed.clear()

    def test_state_is_distinct_from_completed_and_forced(self):
        self.assertIn('closed-no-save-claimed', closure.TERMINAL_STATES)
        self.assertNotIn('closed-no-save-claimed', closure.ATTENTION_STATES)
        self.assertNotIn('closed-no-save-claimed', {'completed', 'forced'})

    def test_current_receipt_still_closes_as_completed(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.save(sid)
            self.hub.observe('turn-stopped')
        result = self.hub.close(sid, no_handoff=True)
        self.assertEqual(result['closure']['state'], 'completed')
        self.assertEqual(result['closure']['delivery']['channel'], 'completion-receipt')

    def test_working_session_is_refused_and_left_running(self):
        cases = {
            'prompt': lambda sid: self.hub.observe('prompt-submitted'),
            'tool': lambda sid: self.hub.observe('tool-started'),
        }
        for name, act in cases.items():
            with self.subTest(case=name):
                sid = self.idle()
                with self.acting_as(sid):
                    act(sid)
                with self.assertRaisesRegex(StoreError, 'working'):
                    self.hub.close(sid, no_handoff=True)
                row = self.assert_open(sid)
                self.assertIsNone(row.get('closure'))
                self.hub.stop(sid)
                self.tmux.killed.clear()

    def test_explicit_working_report_is_refused(self):
        sid = self.idle()
        with self.acting_as(sid):
            self.hub.report(state='working', body='Still going')
        with self.assertRaisesRegex(StoreError, 'working'):
            self.hub.close(sid, no_handoff=True)
        self.assert_open(sid)

    def test_refused_retries_keep_one_history_entry(self):
        sid = self.idle()
        self.hub.stop(sid)
        self.hub.resume(sid, prompt='Continue')
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        self.tmux.killed.clear()
        self.tmux.attached = 1
        for _ in range(2):
            with self.assertRaisesRegex(StoreError, 'attached'):
                self.hub.close(sid, no_handoff=True)
        self.assertEqual(len(self.hub.get(sid).get('closure_history', [])), 1)
        self.tmux.attached = 0
        self.assertEqual(self.hub.close(sid, no_handoff=True)['closure']['state'], 'closed-no-save-claimed')
        self.assertEqual(len(self.hub.get(sid).get('closure_history', [])), 1)

    def test_outstanding_background_work_is_refused(self):
        sid = self.idle(background_tasks=2)
        with self.assertRaisesRegex(StoreError, 'background'):
            self.hub.close(sid, no_handoff=True)
        self.assert_open(sid)

    def test_pending_native_question_is_refused(self):
        sid = self.idle()
        with self.acting_as(sid):
            self.hub.observe('permission-requested')
        with self.assertRaisesRegex(StoreError, 'input'):
            self.hub.close(sid, no_handoff=True)
        self.assert_open(sid)

    def test_unobserved_session_is_refused(self):
        sid = self.launch()['session_id']
        with self.assertRaisesRegex(StoreError, 'idle boundary'):
            self.hub.close(sid, no_handoff=True)
        self.assert_open(sid)

    def test_harness_without_a_native_idle_bridge_is_refused(self):
        for harness in ('copilot', 'opencode'):
            with self.subTest(harness=harness):
                sid = self.launch(harness=harness)['session_id']
                with self.assertRaisesRegex(StoreError, 'no native idle'):
                    self.hub.close(sid, no_handoff=True)
                self.assert_open(sid)
                self.hub.stop(sid)
                self.tmux.killed.clear()

    def test_attached_client_is_refused_without_killing(self):
        sid = self.idle()
        self.tmux.attached = 1
        with self.assertRaisesRegex(StoreError, 'attached'):
            self.hub.close(sid, no_handoff=True)
        row = self.assert_open(sid)
        self.assertNotEqual((row.get('closure') or {}).get('state'), 'closed-no-save-claimed')

    def test_prompt_during_liveness_probe_prevents_termination(self):
        sid = self.idle()
        original = self.hub._live
        def probe(row):
            with self.acting_as(sid):
                self.hub.observe('prompt-submitted')
            return original(row)
        with mock.patch.object(self.hub, '_live', side_effect=probe):
            with self.assertRaisesRegex(StoreError, 'working'):
                self.hub.close(sid, no_handoff=True)
        self.assert_open(sid)

    def test_wait_polls_for_the_idle_boundary(self):
        sid = self.idle()
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
        calls = []
        def sleeper(seconds):
            calls.append(seconds)
            with self.acting_as(sid):
                self.hub.observe('turn-stopped')
        with mock.patch('lib.control.session_hub.time.sleep', side_effect=sleeper):
            result = self.hub.close(sid, no_handoff=True, wait=5)
        self.assertTrue(calls)
        self.assertEqual(result['closure']['state'], 'closed-no-save-claimed')

    def test_wait_expiry_refuses_with_the_last_reason(self):
        sid = self.idle()
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
        clock = iter(range(0, 1000, 2))
        with mock.patch('lib.control.session_hub.time.sleep'), \
                mock.patch('lib.control.session_hub.time.monotonic', side_effect=lambda: next(clock)):
            with self.assertRaisesRegex(StoreError, 'working'):
                self.hub.close(sid, no_handoff=True, wait=5)
        self.assert_open(sid)

    def test_pending_close_request_is_superseded_without_a_save_claim(self):
        sid = self.idle()
        request = self.hub.close(sid)['closure']
        self.assertEqual(request['state'], 'pending-delivery')
        result = self.hub.close(sid, no_handoff=True)
        record = result['closure']
        self.assertEqual(record['state'], 'closed-no-save-claimed')
        self.assertEqual(record['request_id'], request['request_id'])
        self.assertFalse(record['memory']['saved'])
        self.assertFalse(record['attention'])

    def test_force_and_no_handoff_are_exclusive(self):
        sid = self.idle()
        with self.assertRaisesRegex(StoreError, 'no-handoff'):
            self.hub.close(sid, force=True, no_handoff=True)
        self.assert_open(sid)

    def test_structured_session_is_refused(self):
        from lib.control.session_store import SessionStore
        sid = self.launch(transport='structured')['session_id']
        with SessionStore(self.config) as sessions, sessions.db.transaction(write=True) as c:
            c.execute("UPDATE managed_sessions SET state='idle' WHERE session_id=?", (sid,))
        with self.assertRaisesRegex(StoreError, 'terminal'):
            self.hub.close(sid, no_handoff=True)

    def test_cli_flag_reaches_the_graceful_no_handoff_close(self):
        sid = self.idle()
        out = io.StringIO()
        self.enterContext(mock.patch.object(hub_cli, 'Hub', return_value=self.hub))
        with redirect_stdout(out):
            self.assertEqual(hub_cli.dispatch(['close', sid, '--no-handoff', '--json'], env=self.env), 0)
        self.assertIn('closed-no-save-claimed', out.getvalue())
        err = io.StringIO()
        other = self.idle()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            self.assertEqual(hub_cli.dispatch(['close', other, '--no-handoff', '--force'], env=self.env), 2)
        self.assertEqual(self.hub.get(other)['lifecycle'], 'open')


class NoHandoffNextStepTests(unittest.TestCase):
    """The hint reads the hub's eligibility, computed by the command's own predicate."""
    def row(self, eligible=True, **changes):
        return dict(dict(session_id='one', generation=1, activity='closing', project_name='asha', name='Session',
                         harness='claude', transport='terminal', profile='worker', lifecycle='closing',
                         reason='Observed', process_state='live', native_activity='idle', native_observed_at=1.0,
                         completion_readiness={'status': 'missing', 'receipt': 'none'},
                         no_handoff={'eligible': eligible, 'reason': None if eligible else 'refused'},
                         closure={'generation': 1, 'state': 'pending-delivery'}), **changes)

    def test_eligible_idle_session_without_receipt_offers_no_handoff(self):
        self.assertEqual(present(self.row())['next_step'], 'Close: attach or --no-handoff')
        attach = self.row(closure={'generation': 1, 'state': 'delivered', 'attachment_required': True})
        self.assertEqual(present(attach)['next_step'], 'Close: attach or --no-handoff')

    def test_attach_only_where_no_handoff_cannot_apply(self):
        cases = {
            'ineligible': self.row(eligible=False),
            'missing eligibility': {k: v for k, v in self.row().items() if k != 'no_handoff'},
            'attached': self.row(closure={'generation': 1, 'state': 'pending-delivery',
                                          'attachment_required': True, 'input_refusal': 'attached'}),
        }
        for name, row in cases.items():
            with self.subTest(case=name):
                self.assertEqual(present(row)['next_step'], 'Close needs attach')

    def test_closed_without_claim_is_history(self):
        row = self.row(lifecycle='closed', activity='closed', process_state='ended',
                       closure={'generation': 1, 'state': 'closed-no-save-claimed', 'attention': False})
        self.assertEqual(present(row)['next_step'], 'Closed: view history')

    def test_guidance_never_claims_a_save(self):
        text = closure.guidance_for({'transport': 'terminal', 'harness': 'claude'},
                                    {'state': 'closed-no-save-claimed', 'handoff': None})
        self.assertIn('no project-memory save was claimed', text)
        self.assertNotIn('verified', text)


class EligibilityTests(unittest.TestCase):
    """One predicate decides the command and the dashboard offer (QA7 P2)."""
    def row(self, **changes):
        return dict(dict(session_id='one', generation=1, activity='idle', harness='claude', transport='terminal',
                         native_activity='idle', native_observed_at=1.0, active_tools={},
                         event_order={'generation': 1, 'applied': 2, 'last_event': 'turn-stopped',
                                      'missing': [], 'barrier': None, 'attempts': 2}), **changes)

    def test_eligible_only_at_an_ordered_idle_boundary(self):
        from lib.control.session_completion import no_handoff_eligibility
        from lib.control.session_order import ATTEMPT_LIMIT, Counters
        self.assertIsNone(no_handoff_eligibility(self.row(), Counters(2, 2)))
        order = lambda **c: {'generation': 1, 'applied': 2, 'last_event': 'turn-stopped', 'missing': [],
                             'barrier': None, 'attempts': 2, **c}
        both = lambda n: Counters(n, 2)
        cases = {
            'copilot': (self.row(harness='copilot'), both(2), 'no native idle'),
            'structured': (self.row(transport='structured'), both(2), 'terminal'),
            'explicit working': (self.row(activity='working'), both(2), 'working'),
            'background': (self.row(native_activity='working', background_tasks=2), both(2), 'background'),
            'question': (self.row(activity='needs-input'), both(2), 'input'),
            'report outstanding': (self.row(), both(3), 'not arrived'),
            'no counter': (self.row(), None, 'counter'),
            'no attempt log': (self.row(), Counters(2, None), 'attempt log .* unavailable'),
            'attempt log full': (self.row(), Counters(2, ATTEMPT_LIMIT), 'full'),
            'unnumbered attempt': (self.row(), Counters(2, 3), '1 native hook.*started'),
            'Stop without attempts': (self.row(event_order=order(attempts=None)), both(2), 'did not report'),
            'barrier': (self.row(event_order=order(barrier=2)), both(2), 'out-of-order'),
            'pending barrier': (self.row(event_order=order(barrier_pending=True)), both(2), 'unreadable'),
            'last event not a Stop': (self.row(event_order=order(last_event='session-start')), both(2), 'not a Stop'),
            'unordered incarnation': (self.row(event_order=None), both(0), 'no ordered'),
            'earlier generation order': (self.row(event_order=order(generation=0)), both(0), 'no ordered'),
            'unobserved': (self.row(native_activity=None, native_observed_at=None), both(2), 'idle boundary'),
        }
        for name, (row, counters, reason) in cases.items():
            with self.subTest(case=name):
                self.assertRegex(no_handoff_eligibility(row, counters) or '', reason)


class OrderingTests(Fixture):
    """QA7 P1: an older or unordered observation never authorizes a turnless kill.

    Orders are explicit here; the fixture otherwise allocates like the hook.
    """
    def idle(self, harness='claude', receipt=False):
        sid = self.launch(harness=harness)['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            if receipt:
                self.save(sid)
            self.hub.observe('turn-stopped')
        return sid

    def next_order(self, sid):
        return self.hub.get(sid)['event_order']['applied'] + 1

    def refused(self, sid, pattern, **kwargs):
        with self.assertRaisesRegex(StoreError, pattern):
            self.hub.close(sid, no_handoff=True, **kwargs)
        self.assertEqual(self.tmux.killed, [])
        self.assertIn(self.hub.get(sid)['lifecycle'], {'open', 'closing'})

    def test_older_stop_never_erases_newer_tool_work(self):
        for harness in ('claude', 'codex'):
            with self.subTest(harness=harness):
                sid = self.idle(harness)
                n = self.next_order(sid)
                with self.acting_as(sid):
                    self.hub.observe('prompt-submitted', order=n + 1)
                    self.hub.observe('tool-started', tool_kind='work', tool_token='new-tool', order=n + 2)
                    self.hub.observe('turn-stopped', order=n)          # the older Stop, arriving last
                row = self.hub.get(sid)
                self.assertEqual(row['native_activity'], 'working')
                self.assertEqual(row['active_tools'], {'new-tool': 'work'})
                self.assertEqual(row['event_order']['applied'], n + 2)
                self.assertEqual(row['observation_log'][-1]['event'], 'turn-stopped')
                self.assertEqual(row['observation_log'][-1]['ignored'], 'late')
                self.refused(sid, 'working')
                self.hub.stop(sid)
                self.tmux.killed.clear()

    def test_older_stop_never_clears_outstanding_background_work(self):
        sid = self.idle()
        n = self.next_order(sid)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted', order=n)
            self.hub.observe('turn-stopped', background_tasks=2, order=n + 2)
            self.hub.observe('turn-stopped', order=n + 1)              # older, empty Stop
        self.assertEqual(self.hub.get(sid)['background_tasks'], 2)
        self.refused(sid, 'background')

    def test_wait_is_not_satisfied_by_an_older_stop(self):
        sid = self.idle()
        n = self.next_order(sid)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted', order=n + 1)
        def sleeper(_):
            with self.acting_as(sid):
                self.hub.observe('turn-stopped', order=n)
        with mock.patch('lib.control.session_hub.time.sleep', side_effect=sleeper):
            self.refused(sid, 'working', wait=1)

    def test_duplicate_or_late_report_has_no_effect_but_holds_a_barrier(self):
        sid = self.idle()
        n = self.next_order(sid)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted', order=n)
            self.hub.observe('turn-stopped', order=n + 1)
            self.hub.observe('turn-stopped', order=n + 1)              # replayed
            self.hub.observe('prompt-submitted', order=n)              # older, replayed
        row = self.hub.get(sid)
        self.assertEqual(row['native_activity'], 'idle')
        self.assertEqual([e['ignored'] for e in row['observation_log'][-2:]], ['duplicate', 'late'])
        self.refused(sid, 'out-of-order')
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.observe('turn-stopped')                           # allocated after the evidence
        self.assertEqual(self.hub.close(sid, no_handoff=True)['closure']['state'], 'closed-no-save-claimed')

    def test_unsequenced_events_hold_a_barrier(self):
        # QA7 edges: an unsequenced older Stop erased background work.
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', background_tasks=2, order=None)
            self.hub.observe('turn-stopped', order=None)
        self.assertIsNotNone(self.hub.get(sid)['event_order']['barrier'])
        self.refused(sid, 'native event')

    def test_gap_holds_a_barrier_until_a_stop_allocated_after_it(self):
        sid = self.idle()
        n = self.next_order(sid)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', order=n + 1)              # report n not arrived
        self.assertEqual(self.hub.get(sid)['event_order']['missing'], [n])
        self.refused(sid, 'out-of-order')
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted', order=n)              # arrives late: fills, no effect
            self.hub.observe('prompt-submitted')
            self.hub.observe('turn-stopped')
        row = self.hub.get(sid)
        self.assertEqual(row['event_order']['missing'], [])
        self.assertIsNone(row['event_order']['barrier'])
        self.assertEqual(self.hub.close(sid, no_handoff=True)['closure']['state'], 'closed-no-save-claimed')

    def test_a_stop_allocated_before_unsequenced_evidence_is_no_barrier(self):
        sid = self.idle()
        n = self.next_order(sid)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', order=None)               # counter is n-1 on arrival
            self.hub.observe('turn-stopped', order=n)                  # allocated after it: clears
        self.assertIsNone(self.hub.get(sid)['event_order']['barrier'])
        self.hub.stop(sid)                                             # the fake tmux holds one Room
        sid2 = self.idle()
        m = self.next_order(sid2)
        counter = __import__('lib.control.session_order', fromlist=['x']).counter_path(
            self.config, sid2, 1)
        counter.write_text(f'{m}\n')                                   # Stop m allocated, not reported
        with self.acting_as(sid2):
            self.hub.observe('tool-started', tool_token='new', order=None)
            self.hub.observe('turn-stopped', order=m)                  # allocated before the evidence
        self.assertIsNotNone(self.hub.get(sid2)['event_order']['barrier'])

    def test_a_new_generation_starts_its_own_order(self):
        sid = self.idle()
        self.hub.stop(sid)
        self.hub.resume(sid, prompt='Continue')
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        row = self.hub.get(sid)
        self.assertEqual((row['event_order']['generation'], row['event_order']['applied'],
                          row['event_order']['barrier']), (2, 1, None))
        self.assertEqual(row['native_activity'], 'idle')

    def test_worker_reports_are_not_ordered_hook_events(self):
        sid = self.idle()
        with self.acting_as(sid):
            self.hub.report(state='needs-input', body='Which option?')
        self.assertEqual(self.hub.get(sid)['activity'], 'needs-input')
        self.assertIsNone(self.hub.get(sid)['event_order']['barrier'])

    def test_order_is_validated(self):
        sid = self.idle()
        for bad in (0, -1, 'x', 2 ** 40):
            with self.subTest(order=bad), self.acting_as(sid), self.assertRaises(StoreError):
                self.hub.observe('turn-stopped', order=bad)

    def test_dashboard_offer_matches_the_command_over_explicit_working(self):
        sid = self.idle()
        self.hub.close(sid)
        with self.acting_as(sid):
            self.hub.report(state='working', body='Working without a new native hook')
        shown = self.hub.show(sid)
        self.assertFalse(shown['no_handoff']['eligible'])
        self.assertNotIn('--no-handoff', shown['next_step'])
        self.refused(sid, 'working')

    def test_dashboard_offer_present_when_the_command_would_close(self):
        sid = self.idle()
        shown = self.hub.close(sid)
        self.assertTrue(shown['no_handoff']['eligible'])
        self.assertEqual(shown['next_step'], 'Close: attach or --no-handoff')
        self.assertEqual(self.hub.close(sid, no_handoff=True)['closure']['state'], 'closed-no-save-claimed')

    def test_dashboard_offer_absent_while_a_report_is_outstanding(self):
        sid = self.idle()
        self.hub.close(sid)
        from lib.control import session_order
        path = session_order.counter_path(self.config, sid, 1)
        path.write_text(f'{self.next_order(sid)}\n')                   # a hook took a number
        shown = self.hub.show(sid)
        self.assertFalse(shown['no_handoff']['eligible'])
        self.assertNotIn('--no-handoff', shown['next_step'])
        self.refused(sid, 'not arrived')


class ReceiptAttachmentTests(Fixture):
    """QA7 P1: a current receipt never kills an attached terminal (also plain close, #92)."""
    def saved_idle(self, harness='claude'):
        sid = self.launch(harness=harness)['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.save(sid)
            self.hub.observe('turn-stopped')
        return sid

    def assert_kept(self, sid):
        self.assertEqual(self.tmux.killed, [])
        row = self.hub.get(sid)
        self.assertNotEqual(row['lifecycle'], 'closed')
        self.assertEqual(self.hub.show(sid)['completion_readiness']['receipt'], 'current')

    def attach_now(self):
        self.tmux.attached = 1

    def attach_at_kill(self):
        self.tmux.before_kill = lambda: setattr(self.tmux, 'attached', 1)

    def test_attached_receipt_close_is_refused_then_completes_detached(self):
        for harness in ('claude', 'codex'):
            for when in ('before', 'at-kill'):
                for no_handoff in (False, True):
                    with self.subTest(harness=harness, when=when, no_handoff=no_handoff):
                        sid = self.saved_idle(harness)
                        (self.attach_now if when == 'before' else self.attach_at_kill)()
                        with self.assertRaisesRegex(StoreError, 'attached'):
                            self.hub.close(sid, no_handoff=no_handoff)
                        self.assert_kept(sid)
                        self.assertEqual(self.hub.messages(sid), [])
                        self.tmux.attached = 0
                        self.tmux.before_kill = None
                        result = self.hub.close(sid, no_handoff=no_handoff)
                        self.assertEqual(result['closure']['state'], 'completed')
                        self.assertEqual(result['closure']['delivery']['channel'], 'completion-receipt')
                        self.tmux.killed.clear()

    def test_wait_treats_attachment_as_transient(self):
        for no_handoff in (False, True):
            with self.subTest(no_handoff=no_handoff):
                sid = self.saved_idle()
                self.attach_now()
                def detach(_):
                    self.tmux.attached = 0
                with mock.patch('lib.control.session_hub.time.sleep', side_effect=detach):
                    result = self.hub.close(sid, no_handoff=no_handoff, wait=5)
                self.assertEqual(result['closure']['state'], 'completed')
                self.tmux.killed.clear()


if __name__ == '__main__':
    unittest.main()


class DefaultOffTests(ClosureFixture):
    """#103: close --no-handoff is experimental and off unless control.no_handoff_close is true."""
    save = test_session_completion.CompletionTests.save

    def idle(self, harness='claude', receipt=False):
        sid = self.launch(harness=harness)['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            if receipt:
                self.save(sid)
            self.hub.observe('turn-stopped')
        return sid

    def test_default_refuses_no_handoff_with_zero_kills(self):
        self.assertFalse(self.config.no_handoff_close)
        for harness in ('claude', 'codex'):
            for receipt in (False, True):
                with self.subTest(harness=harness, receipt=receipt):
                    sid = self.idle(harness, receipt)
                    for wait in (0, 1):
                        with self.assertRaisesRegex(StoreError, 'no_handoff_close') as refused:
                            self.hub.close(sid, no_handoff=True, wait=wait)
                        self.assertIn('no_handoff_close', str(refused.exception))
                        self.assertIn('#103', str(refused.exception))
                    self.assertEqual(self.tmux.killed, [])
                    row = self.hub.show(sid)
                    self.assertEqual(row['lifecycle'], 'open')
                    self.assertIsNone(row.get('closure'))
                    self.assertEqual(self.hub.messages(sid), [])
                    self.assertFalse(row['no_handoff']['eligible'])
                    self.assertIn('#103', row['no_handoff']['reason'])
                    self.hub.stop(sid)
                    self.tmux.killed.clear()

    def test_default_cli_refuses_and_dashboard_does_not_offer_it(self):
        sid = self.idle()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(hub_cli, 'Hub', return_value=self.hub), redirect_stdout(out), redirect_stderr(err):
            rc = hub_cli.dispatch(['close', sid, '--no-handoff'], env=self.env)
        self.assertNotEqual(rc, 0)
        self.assertIn('no_handoff_close', err.getvalue() + out.getvalue())
        self.assertEqual(self.tmux.killed, [])
        # The pending close's next step offers attach only.
        self.assertEqual(self.hub.close(sid)['next_step'], 'Close needs attach')
        rendered = '\n'.join(session_tui.lines({'rows': [self.hub.show(sid)], 'no_handoff_close': False}, width=200))
        self.assertNotIn('c close (no handoff)', rendered)
        # #102: the full binding list lives in the ? key sheet, gated the same way.
        self.assertNotIn('c close (no handoff)',
                         '\n'.join(session_tui.lines({'rows': [], 'no_handoff_close': False}, width=200, keys=True)))
        self.assertIn('c close (no handoff)',
                      '\n'.join(session_tui.lines({'rows': [], 'no_handoff_close': True}, width=200, keys=True)))

    def test_enabling_restores_the_turnless_close(self):
        for harness in ('claude', 'codex'):
            with self.subTest(harness=harness):
                self.configure_control()
                sid = self.idle(harness)
                with self.assertRaisesRegex(StoreError, 'no_handoff_close'):
                    self.hub.close(sid, no_handoff=True)
                self.assertEqual(self.tmux.killed, [])
                self.configure_control(no_handoff_close=True)
                self.assertTrue(self.hub.show(sid)['no_handoff']['eligible'])
                result = self.hub.close(sid, no_handoff=True)
                self.assertEqual(result['closure']['state'], 'closed-no-save-claimed')
                self.assertEqual(len(self.tmux.killed), 1)
                self.tmux.killed.clear()

    def test_invalid_setting_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'no_handoff_close'):
            self.configure_control(no_handoff_close='yes')

    def test_receipt_close_still_refuses_attached_sessions(self):
        for harness in ('claude', 'codex'):
            for at_kill in (False, True):
                with self.subTest(harness=harness, at_kill=at_kill):
                    sid = self.idle(harness, receipt=True)
                    if at_kill:
                        self.tmux.before_kill = lambda: setattr(self.tmux, 'attached', 1)
                    else:
                        self.tmux.attached = 1
                    with self.assertRaisesRegex(StoreError, 'attached'):
                        self.hub.close(sid)
                    self.assertEqual(self.tmux.killed, [])
                    self.assertEqual(self.hub.show(sid)['completion_readiness']['receipt'], 'current')
                    self.tmux.attached, self.tmux.before_kill = 0, None
                    result = self.hub.close(sid)
                    self.assertEqual(result['closure']['state'], 'completed')
                    self.tmux.killed.clear()
