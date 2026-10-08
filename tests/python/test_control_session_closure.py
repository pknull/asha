"""Best-effort close of hub project sessions (D1, D4-D11); the shared session fixture."""
import json
import hashlib
import subprocess
import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock

from lib.control.config import load_config
from lib.control.store import StoreError
from lib.control import session_closure as closure
from tests.python import test_control_rooms as rooms_fixture

ACTIVE = "# Objective\nShip the closure\n\n# State\nDone\n\n# Next\n- Review\n\n# Blockers\n- None\n"
DECISIONS = "# Decisions\n\n- Handoffs publish through the validator.\n"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


class ClosureFixture(unittest.TestCase):
    def setUp(self):
        rooms_fixture.RoomTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.config = load_config(self.env)
        from lib.control.session_hub import Hub
        self.hub = Hub(self.config, env=self.env, tmux=self.tmux)
        self.owner_start = self.enterContext(mock.patch(
            'lib.control.sessions.ensure_owners',
            return_value={'managed_sessions': 1, 'owners_started': 1}))
        # Closure never commits, pushes or integrates: any git invocation is a failure.
        original_run, original_popen = subprocess.run, subprocess.Popen
        def guard(factory):
            def wrapped(argv, *args, **kwargs):
                if argv and str(argv[0]).endswith('git'):
                    raise AssertionError('closure invoked git: ' + ' '.join(map(str, argv)))
                return factory(argv, *args, **kwargs)
            return wrapped
        self.enterContext(mock.patch('subprocess.run', guard(original_run)))
        self.enterContext(mock.patch('subprocess.Popen', guard(original_popen)))
        self.memory = self.project / 'Memory'
        (self.memory / 'activeContext.md').write_text("# Objective\nStart\n\n# State\nNew\n\n# Next\n- Begin\n\n# Blockers\n- None\n")
        (self.memory / 'decisions.md').write_text("# Decisions\n\n- None.\n")

    def configure_control(self, **control):
        """Write the Asha config's control object and rebuild the hub from it."""
        self.asha_home.mkdir(parents=True, exist_ok=True)
        self.asha_home.chmod(0o700)
        path = self.asha_home / 'config.json'
        path.write_text(json.dumps({'control': control}))
        path.chmod(0o600)
        self.config = load_config(self.env)
        from lib.control.session_hub import Hub
        self.hub = Hub(self.config, env=self.env, tmux=self.tmux)

    def launch(self, **changes):
        values = dict(project=str(self.project), prompt='Trim the games', name='Termart cleanup', harness='claude')
        values.update(changes)
        return self.hub.launch(**values)

    @contextmanager
    def acting_as(self, sid):
        # Scripted native boundary fixture. This is not native delivery proof.
        # The handoff runs as a tool call, so its hooks report around it.
        handoff = self.hub.handoff
        def reported(*args, **kwargs):
            self.hub.observe('tool-started')
            try:
                return handoff(*args, **kwargs)
            finally:
                try:
                    self.hub.observe('tool-completed')
                except StoreError:
                    pass   # the save closed the session; its last hook report is refused
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)), \
                self.ordered_hooks(sid), \
                mock.patch.object(self.hub, 'handoff', side_effect=reported):
            yield

    # Hook arguments the hub no longer takes (best-effort close D11): the CLI
    # accepts and ignores them, so reports that still carry them apply.
    OBSOLETE_HOOK_ARGUMENTS = ('order', 'attempts', 'tool_kind', 'tool_token', 'sequence', 'sequence_pane')

    @contextmanager
    def ordered_hooks(self, sid, hub=None):
        """Report ``hub.observe`` hook events as the CLI does: obsolete arguments dropped."""
        hub = hub or self.hub
        observe = hub.observe
        def reported(event, **kwargs):
            for name in self.OBSOLETE_HOOK_ARGUMENTS:
                kwargs.pop(name, None)
            return observe(event, **kwargs)
        with mock.patch.object(hub, 'observe', side_effect=reported):
            yield

    def drafts(self, active=ACTIVE, decisions=DECISIONS):
        active_file, decisions_file = self.root / 'active.draft', self.root / 'decisions.draft'
        active_file.write_text(active)
        decisions_file.write_text(decisions)
        return str(active_file), str(decisions_file)

    def digests(self):
        return {name: sha((self.memory / name).read_text()) for name in closure.MEMORY_FILES}

    def request(self, sid, wait=60):
        """A pending close request, as the dashboard records it before its waiter starts."""
        return self.hub.request_close(sid, wait=wait)

    def save(self, sid, outcome='no-durable-update', request=None):
        with self.acting_as(sid):
            return self.hub.handoff(request, outcome=outcome, detail='Nothing durable changed')


class FastClose(ClosureFixture):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch('lib.control.session_hub.CLOSE_POLL_SECONDS', 0.02))
        self.enterContext(mock.patch.object(closure, 'PUBLICATION_GRACE_SECONDS', 0.3))

    def closing_in_thread(self, sid, **kwargs):
        result = []
        def run():
            try:
                result.append(self.hub.close(sid, **kwargs))
            except BaseException as exc:   # surfaced by the test's own assertions
                result.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        return worker, result

    def until(self, predicate, timeout=20):
        end = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > end:
                self.fail('condition not reached')
            time.sleep(0.01)


class RequestTests(FastClose):
    def test_request_records_a_deadline_and_queues_the_request_without_killing(self):
        sid = self.launch()['session_id']
        closing = self.request(sid, wait=45)
        record = closing['closure']
        self.assertEqual(closing['lifecycle'], 'closing')
        self.assertEqual(record['state'], 'closing')
        self.assertAlmostEqual(record['deadline'] - record['requested_at'], 45, places=3)
        messages = self.hub.messages(sid)
        self.assertEqual([m['delivery_key'] for m in messages], ['close:' + record['request_id']])
        self.assertIn('do not commit, push or integrate', messages[0]['body'])
        self.assertNotIn('--attempt', messages[0]['body'])
        self.assertNotIn('only command in its own tool call', messages[0]['body'])
        self.assertEqual(self.tmux.killed, [])
        self.assertTrue(closing['next_step'].startswith('Closing'))

    def test_a_repeated_close_joins_the_pending_request(self):
        sid = self.launch()['session_id']
        first = self.request(sid)['closure']
        again = self.request(sid)['closure']
        self.assertEqual(again['request_id'], first['request_id'])
        self.assertEqual(again['deadline'], first['deadline'])
        self.assertEqual(len(self.hub.messages(sid)), 1)

    def test_configured_default_wait_and_bounds(self):
        self.configure_control(close_wait_seconds=7)
        sid = self.launch()['session_id']
        self.assertEqual(self.hub._close_wait(False, None), 7)
        self.assertEqual(self.hub._close_wait(True, None), 0)
        for bad in (-1, 601, True, 1.5):
            with self.subTest(wait=bad), self.assertRaisesRegex(StoreError, 'wait must be'):
                self.hub.close(sid, wait=bad)
        with self.assertRaisesRegex(StoreError, 'cannot be combined'):
            self.hub.close(sid, force=True, wait=5)
        self.assertEqual(self.hub.get(sid)['lifecycle'], 'open')

    def test_close_wait_setting_is_validated(self):
        from lib.control.config import ConfigError
        for bad in (-1, 601, 'x', True):
            with self.subTest(value=bad), self.assertRaises(ConfigError):
                self.configure_control(close_wait_seconds=bad)

    def test_cli_wait_default_is_unset_and_force_with_wait_is_a_usage_error(self):
        from lib.control import hub_cli
        sid = self.launch()['session_id']
        with mock.patch.object(hub_cli, 'Hub', return_value=self.hub), \
                mock.patch.object(self.hub, 'close', return_value={'lifecycle': 'closed'}) as close:
            hub_cli.dispatch(['close', sid, '--json'], env=self.env)
            self.assertEqual(close.call_args.kwargs, {'force': False, 'wait': None})
            with self.assertRaises(SystemExit):
                hub_cli.dispatch(['close', sid, '--force', '--wait', '3'], env=self.env)


class PointerTests(FastClose):
    """D1: one bounded pointer line into an idle or unobserved pane, only from the close's waiter."""

    def test_idle_claude_gets_exactly_one_pointer(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        worker, result = self.closing_in_thread(sid, wait=1)
        self.until(lambda: self.tmux.sent)
        worker.join()
        pane, line = self.tmux.sent[0]
        self.assertEqual(len(self.tmux.sent), 1)
        self.assertEqual(pane, self.tmux.pane_id)
        self.assertLessEqual(len(line), 200)
        rid = result[0]['closure']['request_id']
        self.assertIn(rid, line)
        self.assertIn('asha control session messages', line)
        self.assertIsNotNone(result[0]['closure']['pointer_at'])

    def test_refresh_and_a_second_waiter_never_retype(self):
        sid = self.launch()['session_id']
        record = self.request(sid)['closure']
        row = self.hub.get(sid)
        self.hub._point(row, record)
        self.hub._point(self.hub.get(sid), self.hub.get(sid)['closure'])
        self.hub.show(sid)
        self.hub.list()
        self.assertEqual(len(self.tmux.sent), 1)

    def test_working_claude_gets_the_stop_decision_not_a_pointer(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
        record = self.request(sid)['closure']
        self.hub._point(self.hub.get(sid), record)
        self.assertEqual(self.tmux.sent, [])
        with self.acting_as(sid):
            stopped = self.hub.observe('turn-stopped')
        decision = self.hub.stop_decision(stopped)
        self.assertEqual(decision['decision'], 'block')
        self.assertIn(closure.CLOSE_REQUEST_TOKEN, decision['reason'])
        self.assertTrue(self.hub.confirm_delivery(stopped, receipt=decision.receipt)['confirmed'])
        self.assertIsNone(self.hub.stop_decision(self.hub.get(sid)), 'one decision per request')
        current = self.hub.get(sid)
        self.hub._point(current, current['closure'])
        self.assertEqual(self.tmux.sent, [], 'a delivered request gets no pointer')

    def test_stop_hook_active_never_chains_a_block(self):
        sid = self.launch()['session_id']
        self.request(sid)
        with self.acting_as(sid):
            stopped = self.hub.observe('turn-stopped')
        self.assertIsNone(self.hub.stop_decision(stopped, stop_hook_active=True))

    def test_working_codex_gets_the_pointer_once_observed_idle(self):
        sid = self.launch(harness='codex')['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
        record = self.request(sid)['closure']
        self.assertEqual(record['delivery']['channel'], 'queued-message')
        with self.acting_as(sid):
            self.assertIsNone(self.hub.stop_decision(self.hub.observe('turn-stopped', background_tasks=0)))
        self.hub._point(self.hub.get(sid), record)
        self.assertEqual(len(self.tmux.sent), 1)

    def test_an_unobserved_session_gets_the_pointer(self):
        sid = self.launch(harness='copilot')['session_id']
        self.hub._point(self.hub.get(sid), self.request(sid)['closure'])
        self.assertEqual(len(self.tmux.sent), 1)

    def test_a_session_waiting_for_input_gets_no_pointer(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.observe('permission-requested', body='May I?')
        self.hub._point(self.hub.get(sid), self.request(sid)['closure'])
        self.assertEqual(self.tmux.sent, [])

    def test_no_pointer_without_verified_room_ownership(self):
        sid = self.launch()['session_id']
        record = self.request(sid)['closure']
        self.tmux.session_identity = '$99'   # the Room's recorded identity no longer matches
        self.hub._point(self.hub.get(sid), record)
        self.assertEqual(self.tmux.sent, [])


class OutcomeTests(FastClose):
    """D8: waits for a save after the request; the label shows the generation's latest save."""

    def test_a_save_during_the_wait_closes_saved(self):
        sid = self.launch()['session_id']
        worker, result = self.closing_in_thread(sid, wait=30)
        self.until(lambda: self.hub.get(sid)['lifecycle'] == 'closing')
        started = time.monotonic()
        rid = self.hub.get(sid)['closure']['request_id']
        active, decisions = self.drafts()
        with self.acting_as(sid):
            self.hub.handoff(rid, active_file=active, decisions_file=decisions, expected=self.digests())
        worker.join(10)
        self.assertLess(time.monotonic() - started, 10)
        closed = result[0]
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertTrue(closed['reason'].startswith('Closed, saved '), closed['reason'])
        self.assertIsNotNone(closed['closure']['saved_at'])
        self.assertEqual(len(self.tmux.killed), 1)
        self.assertFalse((self.project / '.git').exists())

    def test_an_unnamed_save_during_the_wait_also_closes(self):
        sid = self.launch()['session_id']
        worker, result = self.closing_in_thread(sid, wait=30)
        self.until(lambda: self.hub.get(sid)['lifecycle'] == 'closing')
        self.save(sid)
        worker.join(10)
        self.assertTrue(result[0]['reason'].startswith('Closed, saved '))

    def test_no_save_by_the_deadline_closes_unsaved_and_kills_an_attached_terminal(self):
        sid = self.launch()['session_id']
        self.tmux.attached = 1          # D4: the operator's close authorizes the kill
        closed = self.hub.close(sid, wait=1)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['reason'], 'Closed, unsaved')
        self.assertIsNone(closed['closure']['saved_at'])
        self.assertEqual(len(self.tmux.killed), 1)
        self.assertNotIn(sid, [r['session_id'] for r in self.hub.list()['rows']])

    def test_an_earlier_save_in_the_generation_labels_a_close_that_got_none(self):
        sid = self.launch()['session_id']
        self.save(sid)
        saved = self.hub.show(sid)['memory_saved_at']
        closed = self.hub.close(sid, wait=1)
        self.assertEqual(closed['memory_saved_at'], saved)
        self.assertTrue(closed['reason'].startswith('Closed, saved '))

    def test_force_is_a_zero_wait(self):
        sid = self.launch()['session_id']
        closed = self.hub.close(sid, force=True)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertTrue(closed['closure']['forced'])
        self.assertEqual(self.hub.messages(sid), [], 'nothing is asked when nothing is waited for')
        self.assertEqual(closed['reason'], 'Closed, unsaved')

    def test_force_on_a_pending_request_closes_it_now(self):
        sid = self.launch()['session_id']
        rid = self.request(sid)['closure']['request_id']
        closed = self.hub.close(sid, force=True)
        self.assertEqual((closed['lifecycle'], closed['closure']['request_id']), ('closed', rid))

    def test_a_process_that_exits_during_the_wait_closes_at_once(self):
        sid = self.launch()['session_id']
        worker, result = self.closing_in_thread(sid, wait=30)
        self.until(lambda: self.hub.get(sid)['lifecycle'] == 'closing')
        self.tmux.sessions.clear()
        worker.join(10)
        self.assertEqual(result[0]['lifecycle'], 'closed')

    def test_a_missing_session_closes_without_asking(self):
        sid = self.launch()['session_id']
        self.hub.send(sid, 'Retain me', key='one')
        self.tmux.sessions.clear()
        closed = self.hub.close(sid)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(len(self.hub.messages(sid)), 1)
        self.assertEqual(self.hub.list()['rows'], [])

    def test_stop_ends_a_pending_close(self):
        sid = self.launch()['session_id']
        self.request(sid)
        stopped = self.hub.stop(sid)
        self.assertEqual(stopped['lifecycle'], 'stopped')
        self.assertEqual(stopped['closure']['state'], 'closed')
        self.assertTrue(stopped['reason'].startswith('Stopped, '))


class EarlyReturnTests(FastClose):
    """D7: a current finished report and a save for the current assignment close without asking."""

    def test_finished_then_saved_and_saved_then_finished_close_at_once(self):
        for order in ('report-first', 'save-first'):
            with self.subTest(order=order):
                sid = self.launch(session_id=str(__import__('uuid').uuid4()))['session_id']
                with self.acting_as(sid):
                    steps = [lambda: self.hub.report(state='finished', body='Done'),
                             lambda: self.hub.handoff(None, outcome='no-durable-update', detail='Nothing')]
                    for step in (steps if order == 'report-first' else reversed(steps)):
                        step()
                    self.hub.observe('turn-stopped')   # #109: finished once the reporting turn ends
                closed = self.hub.close(sid)
                self.assertEqual(closed['lifecycle'], 'closed')
                self.assertEqual([m for m in self.hub.messages(sid) if m['delivery_key'].startswith('close:')], [])

    def test_new_activity_after_the_report_asks_again(self):
        sid = self.launch()['session_id']
        with self.acting_as(sid):
            self.hub.handoff(None, outcome='no-durable-update', detail='Nothing')
            self.hub.report(state='finished', body='Done')
            self.hub.observe('prompt-submitted')
        self.assertEqual(self.request(sid)['lifecycle'], 'closing')


class ReconcileTests(FastClose):
    """D6: an expired close is finalized by a later mutation, never by a read."""

    def expire(self, sid):
        record = self.hub.get(sid)['closure']
        self.hub._update(sid, closure=dict(record, deadline=time.time() - 1))

    def test_a_read_never_finalizes_and_a_later_mutation_does(self):
        sid = self.launch()['session_id']
        self.request(sid)
        self.expire(sid)
        self.hub.show(sid)
        self.hub.list()
        self.assertEqual(self.hub.get(sid)['lifecycle'], 'closing')
        other = self.launch(session_id='33333333-3333-4333-8333-333333333333')['session_id']
        self.assertEqual(self.hub.get(sid)['lifecycle'], 'closed')
        self.hub.send(other, 'hello', key='k')

    def test_a_later_close_finalizes_an_expired_request(self):
        sid = self.launch()['session_id']
        rid = self.request(sid)['closure']['request_id']
        self.expire(sid)
        closed = self.hub.close(sid)
        self.assertEqual((closed['lifecycle'], closed['closure']['request_id']), ('closed', rid))

    def test_finalization_checks_request_and_generation(self):
        sid = self.launch()['session_id']
        self.request(sid)
        self.assertEqual(self.hub._finalize(sid, 'not-the-request')['lifecycle'], 'closing')
        self.assertEqual(self.tmux.killed, [])

    def test_an_old_closing_row_without_a_deadline_gets_a_new_request_only_on_close(self):
        sid = self.launch()['session_id']
        legacy = dict(request_id='11111111-1111-4111-8111-111111111111', generation=1, state='delivered',
                      attempts=2, requested_at=time.time() - 3600, memory={'available': True},
                      delivery={'channel': 'stop-hook', 'message_id': None, 'delivered_at': None})
        self.hub._update(sid, lifecycle='closing', closure=legacy)
        self.hub.reconcile_closes()
        self.assertEqual(self.hub.get(sid)['closure']['request_id'], legacy['request_id'])
        shown = self.hub.show(sid)
        self.assertIn('earlier version', shown['reason'])
        fresh = self.request(sid)['closure']
        self.assertNotEqual(fresh['request_id'], legacy['request_id'])
        self.assertEqual(self.hub.get(sid)['closure_history'][-1]['request_id'], legacy['request_id'])

    def test_dashboard_close_records_and_hands_the_wait_to_a_detached_waiter(self):
        from lib.control import session_actions
        sid = self.launch()['session_id']
        ctx = mock.Mock(hub=self.hub, config=self.config, env=self.env)
        ctx.prompt.return_value = 'yes'
        with mock.patch('lib.control.sessions.refuse_managed_operator'), \
                mock.patch.object(self.hub, 'spawn_close_waiter') as waiter:
            message = session_actions.close_or_stop(ctx, ord('x'), self.hub.show(sid))
        waiter.assert_called_once_with(sid)
        self.assertEqual(self.hub.get(sid)['lifecycle'], 'closing')
        self.assertIn('Closing', message)
        with mock.patch('subprocess.Popen') as popen:
            self.hub.spawn_close_waiter(sid)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[1:], ['control', 'session', 'close', sid])
        self.assertTrue(popen.call_args.kwargs['start_new_session'])


class InFlightTests(FastClose):
    """D9: a publication in flight at the deadline gets a bounded grace, then the kill."""

    def test_a_handoff_holding_the_lock_past_the_deadline_gets_the_grace(self):
        sid = self.launch()['session_id']
        rid = self.request(sid, wait=1)['closure']['request_id']
        held, release = threading.Event(), threading.Event()
        def handoff():
            with self.hub._action_lock(sid):
                held.set()
                release.wait(5)
        holder = threading.Thread(target=handoff)
        holder.start()
        held.wait(5)
        threading.Timer(0.15, release.set).start()
        started = time.monotonic()
        closed = self.hub._finalize(sid, rid, grace=closure.PUBLICATION_GRACE_SECONDS)
        holder.join()
        self.assertGreaterEqual(time.monotonic() - started, 0.1, 'finalization waited for the in-flight handoff')
        self.assertEqual(closed['lifecycle'], 'closed')

    def test_a_reconciled_close_gives_the_inflight_publication_its_grace(self):
        sid = self.launch()['session_id']
        record = self.request(sid, wait=1)['closure']
        held, release, released = threading.Event(), threading.Event(), threading.Event()
        def publisher():
            with closure.memory_v2._publication_lock(self.project):
                held.set()
                release.wait(5)
            released.set()
        thread = threading.Thread(target=publisher)
        thread.start()
        self.assertTrue(held.wait(5))
        timer = threading.Timer(0.15, release.set)
        try:
            time.sleep(max(0, record['deadline'] - time.time()) + 0.02)
            timer.start()
            started = time.monotonic()
            self.hub.reconcile_closes()
            elapsed = time.monotonic() - started
            self.assertEqual(self.hub.get(sid)['lifecycle'], 'closed')
            self.assertTrue(released.is_set(), 'reconciliation killed while the publication held its lock')
            self.assertGreaterEqual(elapsed, 0.1)
        finally:
            release.set()
            timer.cancel()
            thread.join(5)

    def test_a_lock_held_past_the_grace_is_killed_anyway(self):
        sid = self.launch()['session_id']
        rid = self.request(sid, wait=1)['closure']['request_id']
        held, release = threading.Event(), threading.Event()
        def handoff():
            with self.hub._action_lock(sid):
                held.set()
                release.wait(5)
        holder = threading.Thread(target=handoff)
        holder.start()
        held.wait(5)
        with mock.patch('lib.control.session_hub.CLOSE_LOCK_SECONDS', 0.2):
            closed = self.hub._finalize(sid, rid, grace=0.2)
        release.set()
        holder.join()
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(len(self.tmux.killed), 1)

    def test_a_publication_cut_short_by_the_kill_is_recovered_not_torn(self):
        import memory_v2
        sid = self.launch()['session_id']
        before = (self.memory / 'activeContext.md').read_text()
        # A save killed between its two replacements leaves the journal and one new file.
        memory_v2.prepare_publication_journal(self.project)
        (self.memory / 'activeContext.md').write_text(ACTIVE)
        self.assertEqual(self.hub.close(sid, wait=1)['lifecycle'], 'closed')
        with self.assertRaisesRegex(ValueError, 'recovery is pending'):
            memory_v2.read_published_snapshot(self.project)
        memory_v2.recover_publication(self.project)
        self.assertEqual((self.memory / 'activeContext.md').read_text(), before)
        memory_v2.read_published_snapshot(self.project)


class LegacyRecordTests(FastClose):
    """D11: records from earlier versions are read-only and never gate anything."""

    def test_old_states_attention_and_close_saves_present_through_the_label(self):
        sid = self.launch()['session_id']
        self.hub.stop(sid, close=True)
        now = time.time()
        for state, handoff, label in (
                ('completed', dict(outcome='published', verified=True, digests={'a': 'b'}, generation=1,
                                   acknowledged_at=now), 'Closed, saved '),
                ('closed-no-save-claimed', None, 'Closed, unsaved'),
                ('handoff-failed', dict(outcome='failed', generation=1, acknowledged_at=now), 'Closed, unsaved')):
            with self.subTest(state=state):
                self.hub._update(sid, closure=dict(request_id='x', generation=1, state=state, attempts=3,
                                                   attention=True, handoff=handoff))
                shown = self.hub.show(sid)
                self.assertTrue(shown['reason'].startswith(label), shown['reason'])
                self.assertNotEqual(shown['activity'], 'close-failed')
                self.assertEqual(self.hub.list()['rows'], [], 'attention no longer keeps a closed row listed')

    def test_old_completion_and_checkpoint_fields_are_ignored_or_read_only(self):
        sid = self.launch(profile='room')['session_id']
        row = self.hub.get(sid)
        self.hub._update(sid, completion=dict(status='ready', receipt_id='r'), tool_starts={'t': {}},
                         event_order={'applied': 5},
                         memory_checkpoint=dict(session_id=sid, generation=1, outcome='published',
                                                publication_id='p', finalized_at=1000.0))
        shown = self.hub.show(sid)
        self.assertEqual(shown['memory_saved_at'], 1000.0)
        self.assertNotIn('completion_readiness', shown)
        self.assertEqual(row['generation'], shown['generation'])


class StructuredClosureTests(FastClose):
    def structured(self):
        from lib.control.session_store import SessionStore
        row = self.launch(transport='structured')
        sid = row['session_id']
        with SessionStore(self.config) as sessions:
            with sessions.db.transaction(write=True) as c:
                c.execute("UPDATE session_messages SET state='consumed' WHERE session_id=?", (sid,))
                c.execute("UPDATE managed_sessions SET state='idle' WHERE session_id=?", (sid,))
        return sid

    def consume_close_turn(self, sid):
        from lib.control.session_store import SessionStore
        with SessionStore(self.config) as sessions:
            session = sessions.claim_owner(sid)
            turn = sessions.claim_turn(sid, session['generation'])
            self.assertIsNotNone(turn)
        return session, turn

    def worker_hub(self, sid):
        # The structured worker acts through its managed environment, never as the operator.
        from lib.control.session_hub import Hub
        return Hub(self.config, env=dict(self.env, ASHA_MANAGED_SESSION_ID=sid), tmux=self.tmux)

    def release_owner(self, sid):
        from lib.control.session_store import SessionStore
        with SessionStore(self.config) as sessions, sessions.db.transaction(write=True) as c:
            c.execute('UPDATE managed_sessions SET owner_pid=NULL,owner_identity=NULL WHERE session_id=?', (sid,))

    def test_close_queues_the_next_structured_turn_and_closes_on_its_save(self):
        from lib.control.session_store import SessionStore
        sid = self.structured()
        wakes = self.owner_start.call_count
        closing = self.request(sid)
        record = closing['closure']
        self.assertEqual(record['delivery']['channel'], 'structured-turn')
        self.assertEqual(self.owner_start.call_count, wakes + 1)
        self.request(sid)
        with SessionStore(self.config) as sessions, sessions.db.transaction() as c:
            keys = [r[0] for r in c.execute("SELECT delivery_key FROM session_messages WHERE session_id=? AND delivery_key LIKE 'close:%'", (sid,))]
        self.assertEqual(keys, ['close:' + record['request_id']])
        session, turn = self.consume_close_turn(sid)
        worker = self.worker_hub(sid)
        worker.env['ASHA_MANAGED_TURN_ID'] = turn['turn_id']
        with mock.patch.object(worker, 'structured_actor', return_value=(worker.get(sid), turn['delivery_key'])):
            worker.handoff(record['request_id'], outcome='no-durable-update', detail='utility only read')
        with SessionStore(self.config) as sessions:
            sessions.finish(sid, session['generation'], turn['turn_id'], success=True)
        self.release_owner(sid)
        closed = self.hub.close(sid)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertTrue(closed['reason'].startswith('Closed, saved '))
        self.assertNotIn('process_exited', closed['closure'])
        with SessionStore(self.config) as sessions:
            self.assertTrue(sessions.get(sid)['stop_requested'] or sessions.get(sid)['state'] == 'stopped')

    def test_structured_handoff_must_come_from_the_close_turn(self):
        sid = self.structured()
        record = self.request(sid)['closure']
        worker = self.worker_hub(sid)
        with mock.patch.object(worker, 'structured_actor', return_value=(worker.get(sid), 'other-message')):
            with self.assertRaisesRegex(StoreError, 'did not receive the current close request'):
                worker.handoff(record['request_id'], outcome='no-durable-update', detail='wrong turn')
        self.assertIsNone(self.hub.get(sid)['closure']['handoff'])

    def test_structured_close_without_a_save_stops_at_the_deadline(self):
        from lib.control.session_store import SessionStore
        sid = self.structured()
        closed = self.hub.close(sid, wait=1)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['reason'], 'Closed, unsaved')
        with SessionStore(self.config) as sessions:
            state = sessions.get(sid)
        self.assertTrue(state['stop_requested'] or state['state'] == 'stopped')
