"""Graceful closure of hub project sessions with a verified memory handoff."""
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
        self.supervisor = self.enterContext(mock.patch(
            'lib.control.orchestration.supervisor_daemon.start_supervisor',
            return_value=({'message': 'started'}, 0)))
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

    def launch(self, **changes):
        values = dict(project=str(self.project), prompt='Trim the games', name='Termart cleanup', harness='claude')
        values.update(changes)
        return self.hub.launch(**values)

    @contextmanager
    def acting_as(self, sid):
        # Scripted native boundary fixture. This is not native delivery proof.
        handoff = self.hub.handoff
        def finalized(*args, **kwargs):
            token = str(__import__('uuid').uuid4())
            self.hub.observe('tool-started', tool_kind='finalizer', tool_token=token)
            try:
                return handoff(*args, **kwargs)
            finally:
                self.hub.observe('tool-completed', tool_kind='finalizer', tool_token=token)
        observe = self.hub.observe
        def sequenced(event, **kwargs):
            # Like control-event.sh: bump the pane's event sequence, then report it.
            if 'sequence' not in kwargs and getattr(self.tmux, 'event_sequence', None) is not None:
                self.tmux.event_sequence = str(int(self.tmux.event_sequence) + 1)
                kwargs['sequence'] = int(self.tmux.event_sequence)
                kwargs.setdefault('sequence_pane', self.tmux.pane_id)
            return observe(event, **kwargs)
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)), \
                mock.patch.object(self.hub, 'observe', side_effect=sequenced), \
                mock.patch.object(self.hub, 'handoff', side_effect=finalized):
            yield

    def drafts(self, active=ACTIVE, decisions=DECISIONS):
        active_file, decisions_file = self.root / 'active.draft', self.root / 'decisions.draft'
        active_file.write_text(active)
        decisions_file.write_text(decisions)
        return str(active_file), str(decisions_file)

    def digests(self):
        return {name: sha((self.memory / name).read_text()) for name in closure.MEMORY_FILES}


class TerminalClosureTests(ClosureFixture):
    def test_close_requests_a_final_turn_and_does_not_kill(self):
        row = self.launch()
        closing = self.hub.close(row['session_id'])
        self.assertEqual(closing['lifecycle'], 'closing')
        self.assertEqual(self.tmux.killed, [])
        record = closing['closure']
        self.assertEqual(record['state'], 'pending-delivery')
        self.assertEqual(record['generation'], 1)
        self.assertEqual(record['delivery']['channel'], 'stop-hook')
        self.assertFalse(record['memory']['saved'])
        self.assertEqual(record['memory']['destination'], str(self.memory))
        self.assertIn('attach', record['guidance'])
        messages = self.hub.messages(row['session_id'])
        self.assertEqual([m['delivery_key'] for m in messages], ['close:' + record['request_id']])
        self.assertIn(record['request_id'], messages[0]['body'])
        self.assertIn('do not commit, push or integrate', messages[0]['body'])
        self.assertIn(closing['closure']['guidance'], closing['reason'])

    def test_repeated_close_is_idempotent(self):
        row = self.launch()
        first = self.hub.close(row['session_id'])['closure']
        again = self.hub.close(row['session_id'])['closure']
        self.assertEqual(again['request_id'], first['request_id'])
        self.assertEqual(again['attempts'], 1)
        self.assertEqual(len(self.hub.messages(row['session_id'])), 1)
        self.assertEqual(self.tmux.killed, [])

    def test_idle_worker_is_not_reached_without_a_turn_boundary(self):
        row = self.launch()
        with self.acting_as(row['session_id']):
            self.hub.observe('turn-stopped')
        self.hub.close(row['session_id'])
        shown = self.hub.show(row['session_id'])
        self.assertEqual(shown['closure']['state'], 'pending-delivery')
        self.assertEqual(shown['activity'], 'closing')
        # No proven empty input line (the fake pane shows nothing): never typed into.
        self.assertEqual(self.tmux.injected, [])
        self.assertIn('did not submit the close request', shown['closure']['guidance'])

    def test_stop_with_background_work_outstanding_is_not_an_idle_close_boundary(self):
        # #99: a Stop block only continues the turn, so the request is still
        # delivered there; but a delivered request is not called unanswered
        # while the agent waits on its own background job.
        row = self.launch()
        sid = row['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.close(sid)
            waiting = self.hub.observe('turn-stopped', background_tasks=1)
            decision = self.hub.stop_decision(waiting)
            self.assertEqual(decision['decision'], 'block')
            self.hub.confirm_delivery(waiting, decision.receipt)
            self.assertIsNone(self.hub.stop_decision(self.hub.observe('turn-stopped', background_tasks=2)))
            self.assertEqual(self.hub.show(sid)['closure']['state'], 'delivered')
            self.assertIsNone(self.hub.stop_decision(self.hub.observe('turn-stopped')))
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'unanswered')

    def test_finalized_handoff_waits_on_background_work_then_closes(self):
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        with self.acting_as(sid):
            self.hub.handoff(rid, outcome='no-durable-update', detail='Only read the code')
            self.hub.observe('turn-stopped', background_tasks=1)
        waiting = self.hub.close(sid)
        self.assertEqual(waiting['closure']['state'], 'acknowledged')
        self.assertEqual(waiting['next_step'], 'Closing: background tasks running')
        self.assertFalse(waiting['closure'].get('attachment_required'))
        self.assertEqual(self.tmux.killed, [])
        # Past the five-minute window the wait is still named, not "needs attach".
        self.hub._update(sid, native_observed_at=time.time() - 900, observed_at=time.time() - 900)
        shown = self.hub.show(sid)
        self.assertFalse(shown['closure'].get('attachment_required'))
        self.assertEqual(shown['closure']['waiting_on_background'], 1)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        self.assertEqual(self.hub.close(sid)['closure']['state'], 'completed')

    def test_busy_worker_receives_the_request_at_its_stop_boundary_once(self):
        row = self.launch()
        sid = row['session_id']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.close(sid)
            stopped = self.hub.observe('turn-stopped')
            decision = self.hub.stop_decision(stopped)
        self.assertEqual(decision['decision'], 'block')
        rid = self.hub.get(sid)['closure']['request_id']
        self.assertIn('close request ' + rid, decision['reason'])
        self.assertNotIn('\n', json.dumps(decision))
        # Nothing is recorded until the decision was actually printed: a hook
        # killed at its budget re-emits the same request at the next Stop.
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'pending-delivery')
        with self.acting_as(sid):
            self.assertEqual(self.hub.stop_decision(stopped), decision)
            self.hub.confirm_delivery(stopped)
            self.hub.confirm_delivery(stopped)
        record = self.hub.show(sid)['closure']
        self.assertEqual(record['state'], 'delivered')
        self.assertEqual(record['delivery']['channel'], 'stop-hook')
        # The continued turn ends without a handoff: visible, never a success.
        with self.acting_as(sid):
            self.assertIsNone(self.hub.stop_decision(self.hub.observe('turn-stopped')))
        record = self.hub.show(sid)['closure']
        self.assertEqual(record['state'], 'unanswered')
        self.assertEqual(self.hub.get(sid)['lifecycle'], 'closing')
        self.assertEqual(self.tmux.killed, [])
        # An explicit re-run asks again with the same request and a fresh queued copy.
        rearmed = self.hub.close(sid)['closure']
        self.assertEqual(rearmed['request_id'], rid)
        self.assertEqual(rearmed['attempts'], 2)
        self.assertEqual(rearmed['state'], 'pending-delivery')
        self.assertEqual([(m['delivery_key'], m['state']) for m in self.hub.messages(sid)],
                         [('close:' + rid, 'superseded'), ('close:' + rid + ':2', 'queued')])
        self.assertEqual(self.hub.show(sid)['pending_messages'], 1)

    def test_successful_handoff_publishes_verified_memory_then_close_terminates(self):
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        rid = self.hub.get(sid)['closure']['request_id']
        active_file, decisions_file = self.drafts()
        with self.acting_as(sid):
            facts = self.hub.handoff_read()
            self.assertEqual(facts['request_id'], rid)
            self.assertEqual(facts['memory']['baseline'], self.digests())
            result = self.hub.handoff(rid, active_file=active_file, decisions_file=decisions_file,
                                      expected=facts['memory']['baseline'])
        self.assertEqual(result['outcome'], 'published')
        self.assertFalse(result['git_invoked'])
        self.assertEqual((self.memory / 'activeContext.md').read_text(), ACTIVE)
        self.assertEqual((self.memory / 'decisions.md').read_text(), DECISIONS)
        record = self.hub.show(sid)['closure']
        self.assertEqual(record['state'], 'acknowledged')
        self.assertTrue(record['memory']['saved'])
        self.assertEqual(record['handoff']['digests'], self.digests())
        self.assertEqual(record['handoff']['destination'], str(self.memory))
        self.assertEqual(self.tmux.killed, [], 'termination waits for the operator')
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        closed = self.hub.close(sid)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['closure']['state'], 'completed')
        self.assertEqual(len(self.tmux.killed), 1)
        self.assertFalse((self.project / '.git').exists())
        self.assertEqual(self.hub.close(sid)['lifecycle'], 'closed')
        self.assertEqual(len(self.tmux.killed), 1)

    def test_late_session_end_hook_cannot_erase_a_verified_handoff(self):
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        active_file, decisions_file = self.drafts()
        with self.acting_as(sid):
            stale = self.hub.get(sid)   # the hook read its row before the handoff landed
            self.hub.handoff(rid, active_file=active_file, decisions_file=decisions_file, expected=self.digests())
            with mock.patch.object(self.hub, 'actor', return_value=stale):
                self.hub.observe('session-ended')
        record = self.hub.show(sid)['closure']
        self.assertEqual(record['state'], 'acknowledged')
        self.assertTrue(record['memory']['saved'])

    def test_codex_terminal_is_queued_only_until_its_return_channel_is_proven(self):
        row = self.launch(harness='codex')
        sid = row['session_id']
        record = self.hub.close(sid)['closure']
        self.assertEqual(record['delivery']['channel'], 'queued-message')
        with self.acting_as(sid):
            self.assertIsNone(self.hub.stop_decision(self.hub.observe('turn-stopped')))
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'unanswered')

    def test_resume_moves_an_old_closure_record_into_history(self):
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        self.hub.stop(sid)
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'forced')
        resumed = self.hub.resume(sid, prompt='Continue')
        self.assertIsNone(resumed.get('closure'))
        self.assertEqual(self.hub.get(sid)['closure_history'][0]['state'], 'forced')
        self.assertEqual(resumed['activity'], 'unknown')

    def test_closure_state_is_visible_in_the_list_and_summary(self):
        from lib.control.hub_cli import overview
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        self.assertEqual([r['activity'] for r in self.hub.list()['rows']], ['closing'])
        with self.acting_as(sid):
            self.hub.handoff(rid, outcome='blocked', detail='cannot draft')
        snapshot = overview(self.config, env=self.env, tmux=self.tmux)
        self.assertEqual([r['activity'] for r in snapshot['rows']], ['close-failed'])
        self.assertIn('1 closes need attention', snapshot['summary'])

    def test_superseding_publication_after_a_landed_save_is_not_a_failure(self):
        import memory_v2
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        active_file, decisions_file = self.drafts()
        newer = "# Objective\nNewer\n\n# State\nAfter\n\n# Next\n- Keep\n\n# Blockers\n- None\n"
        original = closure.memory_v2.publish
        def publish_then_supersede(root, active, decisions, **kwargs):
            original(root, active, decisions, **kwargs)
            original(root, newer, decisions)
        with self.acting_as(sid), mock.patch.object(closure.memory_v2, 'publish', side_effect=publish_then_supersede):
            result = self.hub.handoff(rid, active_file=active_file, decisions_file=decisions_file, expected=self.digests())
        self.assertEqual(result['outcome'], 'published')
        self.assertTrue(result['closure_state'] == 'acknowledged')
        self.assertEqual((self.memory / 'activeContext.md').read_text(), newer)

    def test_draft_paths_must_be_symlink_free_and_force_excludes_wait(self):
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        active_file, decisions_file = self.drafts()
        link = self.root / 'link.draft'
        link.symlink_to(active_file)
        with self.acting_as(sid), self.assertRaisesRegex(StoreError, 'symlink'):
            self.hub.handoff(rid, active_file=str(link), decisions_file=decisions_file, expected=self.digests())
        with self.assertRaisesRegex(StoreError, 'cannot be combined'):
            self.hub.close(sid, force=True, wait=5)
        self.assertEqual(self.hub.get(sid)['lifecycle'], 'closing')

    def test_needs_input_during_the_final_turn_is_never_hidden(self):
        from lib.control.hub_cli import overview
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        with self.acting_as(sid):
            self.hub.observe(None, state='needs-input', body='May I write outside the repo?')
        shown = self.hub.show(sid)
        self.assertEqual(shown['activity'], 'needs-input')
        self.assertIn('May I write outside the repo?', shown['reason'])
        self.assertIn('1 need input', overview(self.config, env=self.env, tmux=self.tmux)['summary'])
        started = time.monotonic()
        waited = self.hub.close(sid, wait=5)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(waited['activity'], 'needs-input')
        self.assertEqual(waited['lifecycle'], 'closing')

    def test_landed_publication_survives_a_refused_verification_read(self):
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        active_file, decisions_file = self.drafts()
        with self.acting_as(sid), mock.patch.object(closure.memory_v2, 'read_published_snapshot',
                                                    side_effect=ValueError('publication recovery is pending')):
            result = self.hub.handoff(rid, active_file=active_file, decisions_file=decisions_file, expected=self.digests())
        self.assertEqual(result['outcome'], 'published')
        self.assertTrue(result['memory']['saved'])
        self.assertFalse(result['handoff']['verified'])
        self.assertIn('recovery is pending', result['handoff']['verification_error'])
        self.assertEqual((self.memory / 'activeContext.md').read_text(), ACTIVE)

    def test_failed_close_stays_visible_until_the_operator_acknowledges_it(self):
        from lib.control.hub_cli import overview
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        with self.acting_as(sid):
            self.hub.observe('session-ended')
        self.tmux.dead = True
        closed = self.hub.close(sid)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['activity'], 'close-failed')
        self.assertTrue(closed['closure']['attention'])
        self.assertEqual([r['activity'] for r in self.hub.list()['rows']], ['close-failed'])
        self.assertIn('1 closes need attention', overview(self.config, env=self.env, tmux=self.tmux)['summary'])
        acknowledged = self.hub.close(sid, force=True)
        self.assertFalse(acknowledged['closure']['attention'])
        self.assertEqual(acknowledged['closure']['state'], 'undeliverable')
        self.assertEqual(self.hub.list()['rows'], [])
        self.assertEqual(self.hub.list(include_closed=True)['rows'][0]['session_id'], sid)

    def test_stop_hook_active_never_chains_a_second_block(self):
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        with self.acting_as(sid):
            stopped = self.hub.observe('turn-stopped')
            self.assertIsNone(self.hub.stop_decision(stopped, stop_hook_active=True))
            self.assertEqual(self.hub.show(sid)['closure']['state'], 'pending-delivery')
            self.assertIsNotNone(self.hub.stop_decision(stopped))

    def test_new_request_after_a_terminal_record_keeps_the_evidence(self):
        row = self.launch()
        sid = row['session_id']
        first = self.hub.close(sid)['closure']['request_id']
        with self.acting_as(sid):
            self.hub.observe('session-ended')
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'undeliverable')
        # The harness is live again from the operator's point of view (the fake pane never died).
        second = self.hub.close(sid)['closure']
        self.assertNotEqual(second['request_id'], first)
        history = self.hub.get(sid)['closure_history']
        self.assertEqual([h['request_id'] for h in history], [first])
        self.assertEqual([m['state'] for m in self.hub.messages(sid)], ['superseded', 'queued'])

    def test_dead_or_unobservable_terminal_is_not_painted_as_closing(self):
        from lib.control.hub_cli import overview
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        self.tmux.dead = True
        shown = self.hub.show(sid)
        self.assertEqual(shown['activity'], 'exited')
        self.assertIn('Closing (exited', shown['reason'])
        self.assertTrue(shown['closure']['needs_attention'])
        self.assertIn('1 closes need attention', overview(self.config, env=self.env, tmux=self.tmux)['summary'])

    def test_needs_input_still_names_a_retryable_failure(self):
        from lib.control.hub_cli import overview
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        with self.acting_as(sid):
            self.hub.handoff(rid, outcome='failed', detail='draft too large')
            self.hub.observe(None, state='needs-input', body='Shall I retry with a smaller draft?')
        shown = self.hub.show(sid)
        self.assertEqual(shown['activity'], 'needs-input')
        self.assertIn('Closing (handoff-failed), needs input: Shall I retry', shown['reason'])
        summary = overview(self.config, env=self.env, tmux=self.tmux)['summary']
        self.assertIn('1 need input', summary)
        self.assertIn('1 closes need attention', summary)

    def test_explicit_stop_or_force_dismisses_attention_in_one_press(self):
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        with self.acting_as(sid):
            self.hub.observe('session-ended')
        self.tmux.dead = True
        stopped = self.hub.stop(sid)
        self.assertEqual(stopped['lifecycle'], 'stopped')
        self.assertEqual(stopped['closure']['state'], 'undeliverable')
        self.assertFalse(stopped['closure']['attention'])
        self.assertNotEqual(stopped['activity'], 'close-failed')
        self.assertEqual(self.hub.list()['rows'][0]['lifecycle'], 'stopped')
        closed = self.hub.close(sid, force=True)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(self.hub.list()['rows'], [])

    def test_request_text_carries_the_bridge_token_and_unverified_wording(self):
        row = self.launch()
        sid = row['session_id']
        record = self.hub.close(sid)['closure']
        text_body = closure.request_text(self.hub.get(sid), record)
        self.assertTrue(text_body.startswith(closure.CLOSE_REQUEST_TOKEN))
        self.assertIn('close request', closure.CLOSE_REQUEST_TOKEN)   # the jq test() pattern in control-event.sh
        active_file, decisions_file = self.drafts()
        with self.acting_as(sid), mock.patch.object(closure.memory_v2, 'read_published_snapshot', side_effect=ValueError('journal pending')):
            self.hub.handoff(record['request_id'], active_file=active_file, decisions_file=decisions_file, expected=self.digests())
        self.assertIn('published but unverified', self.hub.show(sid)['closure']['guidance'])
        self.assertIn('unverified', self.hub.close(sid)['closure']['guidance'])

    def test_stale_stop_receipt_cannot_mark_a_replacement_request_delivered(self):
        # Verifier defect 1: a receipt for request A must not confirm request B.
        row = self.launch()
        sid = row['session_id']
        old = self.hub.close(sid)
        with self.acting_as(sid):
            decision = self.hub.stop_decision(old)
            self.hub.observe('session-ended')
        replacement = self.hub.close(sid)
        self.assertNotEqual(replacement['closure']['request_id'], old['closure']['request_id'])
        self.assertNotIn(replacement['closure']['request_id'], decision['reason'])
        self.assertEqual(self.hub.confirm_delivery(old, receipt=decision.receipt), {'confirmed': False, 'reason': 'stale receipt'})
        self.assertEqual(self.hub.confirm_delivery(old), {'confirmed': False, 'reason': 'stale receipt'})
        self.assertEqual(self.hub.get(sid)['closure']['state'], 'pending-delivery')
        # The replacement still gets its own Stop delivery, and its own receipt confirms it.
        with self.acting_as(sid):
            fresh = self.hub.stop_decision(self.hub.get(sid))
        self.assertIn(replacement['closure']['request_id'], fresh['reason'])
        self.assertEqual(self.hub.confirm_delivery(self.hub.get(sid), receipt=fresh.receipt)['confirmed'], True)
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'delivered')

    def test_receipt_binds_to_the_delivery_attempt(self):
        # A receipt from attempt 1 must not confirm the re-armed attempt 2 of the same request.
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        with self.acting_as(sid):
            first = self.hub.stop_decision(self.hub.get(sid))
            self.hub.confirm_delivery(self.hub.get(sid), receipt=first.receipt)
            self.assertIsNone(self.hub.stop_decision(self.hub.observe('turn-stopped')))
        rearmed = self.hub.close(sid)['closure']
        self.assertEqual(rearmed['attempts'], 2)
        self.assertFalse(self.hub.confirm_delivery(self.hub.get(sid), receipt=first.receipt)['confirmed'])
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'pending-delivery')

    def test_explicit_stop_dismisses_attention_on_an_already_closed_failure(self):
        # Verifier defect 3: stop, not only force-close, is the operator's acknowledgement.
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        with self.acting_as(sid):
            self.hub.observe('session-ended')
        self.tmux.dead = True
        closed = self.hub.close(sid)
        self.assertTrue(closed['closure']['attention'])
        kills = list(self.tmux.killed)   # the close itself ended the dead holder pane
        stopped = self.hub.stop(sid)
        self.assertEqual(stopped['lifecycle'], 'closed')
        self.assertFalse(stopped['closure']['attention'])
        self.assertFalse(stopped['closure']['needs_attention'])
        self.assertEqual(stopped['closure']['state'], 'undeliverable')
        self.assertIsNotNone(stopped['closure']['dismissed_at'])
        self.assertEqual(self.hub.list()['rows'], [])
        again = self.hub.stop(sid)
        self.assertEqual(again['closure']['dismissed_at'], stopped['closure']['dismissed_at'])
        self.assertEqual(self.tmux.killed, kills, 'dismissal touches no process')

    def test_session_end_during_the_close_probe_is_not_rolled_back(self):
        row = self.launch()
        sid = row['session_id']
        original = self.hub._live
        def live_with_hook(current):
            # The SessionEnd observation lands while close() is probing the terminal.
            with self.acting_as(sid):
                self.hub.observe('session-ended')
            return original(current)
        self.hub.close(sid)
        with mock.patch.object(self.hub, '_live', side_effect=live_with_hook):
            result = self.hub.close(sid)
        self.assertEqual(self.hub.get(sid)['closure']['state'], 'undeliverable')
        self.assertEqual(result['closure']['state'], 'undeliverable')

    def test_double_confirmation_is_named_not_stale(self):
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        with self.acting_as(sid):
            decision = self.hub.stop_decision(self.hub.get(sid))
        self.assertEqual(self.hub.confirm_delivery(self.hub.get(sid), receipt=decision.receipt)['reason'], 'delivered')
        self.assertEqual(self.hub.confirm_delivery(self.hub.get(sid), receipt=decision.receipt)['reason'], 'already delivered')

    def test_event_verb_prints_one_line_even_when_confirmation_fails(self):
        import io
        from contextlib import redirect_stdout
        from lib.control import hub_cli
        from lib.control.session_hub import Hub
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        env = dict(self.env, ASHA_HUB_SESSION_ID=sid, ASHA_HUB_GENERATION='1')
        out = io.StringIO()
        with mock.patch.object(Hub, 'actor', new=lambda self: self.get(sid)), \
             mock.patch.object(Hub, 'confirm_delivery', side_effect=StoreError('lock lost')), redirect_stdout(out):
            self.assertEqual(hub_cli.dispatch(['event', '--event', 'turn-stopped'], env=env), 0)
        self.assertEqual(out.getvalue().count('\n'), 1)
        self.assertEqual(json.loads(out.getvalue())['decision'], 'block')
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'pending-delivery')

    def test_agent_that_acknowledges_then_exits_closes_as_completed(self):
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        with self.acting_as(sid):
            self.hub.handoff(rid, outcome='no-durable-update', detail='read-only session')
            self.hub.observe('session-ended')
        self.tmux.dead = True
        closed = self.hub.close(sid)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['closure']['state'], 'completed')
        self.assertEqual(closed['closure']['handoff']['outcome'], 'no-durable-update')

    def test_explicit_no_durable_update_satisfies_the_handoff(self):
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        before = self.digests()
        with self.acting_as(sid):
            with self.assertRaisesRegex(StoreError, 'outcome or both draft files'):
                self.hub.handoff(rid)
            self.hub.handoff(rid, outcome='no-durable-update', detail='Only read the code')
        self.assertEqual(self.digests(), before)
        record = self.hub.show(sid)['closure']
        self.assertEqual(record['state'], 'acknowledged')
        self.assertFalse(record['memory']['saved'])
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        closed = self.hub.close(sid)
        self.assertEqual(closed['closure']['state'], 'completed')
        self.assertIn('no-durable-update', closed['closure']['guidance'])

    def test_failed_or_blocked_handoff_stays_visible_and_never_closes_silently(self):
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        with self.acting_as(sid):
            self.hub.handoff(rid, outcome='blocked', detail='Validator rejected the draft')
        record = self.hub.show(sid)['closure']
        self.assertEqual(record['state'], 'handoff-failed')
        self.assertEqual(record['handoff']['outcome'], 'blocked')
        self.assertFalse(record['memory']['saved'])
        self.assertEqual(self.hub.get(sid)['lifecycle'], 'closing')
        retried = self.hub.close(sid)
        self.assertEqual(retried['lifecycle'], 'closing')
        self.assertEqual(retried['closure']['attempts'], 2)
        self.assertEqual(retried['closure']['previous_handoff']['outcome'], 'blocked')
        self.assertEqual(self.tmux.killed, [])
        forced = self.hub.close(sid, force=True)
        self.assertEqual(forced['lifecycle'], 'closed')
        self.assertEqual(forced['closure']['state'], 'forced')
        self.assertFalse(forced['closure']['memory']['saved'])
        self.assertIn('no project-memory handoff was claimed', forced['closure']['guidance'])
        self.assertEqual(len(self.tmux.killed), 1)

    def test_concurrent_publication_is_not_overwritten(self):
        import memory_v2
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        stale = self.digests()
        newer = "# Objective\nNewer save\n\n# State\nLanded first\n\n# Next\n- Keep\n\n# Blockers\n- None\n"
        memory_v2.publish(self.project, newer, DECISIONS)
        active_file, decisions_file = self.drafts()
        with self.acting_as(sid):
            with self.assertRaisesRegex(StoreError, 'preimage changed'):
                self.hub.handoff(rid, active_file=active_file, decisions_file=decisions_file, expected=stale)
        self.assertEqual((self.memory / 'activeContext.md').read_text(), newer)
        record = self.hub.show(sid)['closure']
        self.assertEqual(record['state'], 'pending-delivery')
        self.assertIn('preimage changed', record['last_error'])
        self.assertIsNone(record['handoff'])
        with self.acting_as(sid):
            self.hub.handoff(rid, active_file=active_file, decisions_file=decisions_file, expected=self.digests())
        self.assertEqual((self.memory / 'activeContext.md').read_text(), ACTIVE)
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'acknowledged')

    def test_acknowledgement_binds_to_request_and_incarnation(self):
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        with self.acting_as(sid), self.assertRaisesRegex(StoreError, 'stale close request'):
            self.hub.handoff(str(__import__('uuid').uuid4()), outcome='no-durable-update', detail='wrong request')
        self.hub.stop(sid)
        resumed = self.hub.resume(sid, prompt='Continue')
        self.assertEqual(resumed['generation'], 2)
        with self.acting_as(sid), self.assertRaisesRegex(StoreError, 'no close request is pending'):
            self.hub.handoff(rid, outcome='no-durable-update', detail='old incarnation')
        second = self.hub.close(sid)['closure']
        self.assertNotEqual(second['request_id'], rid)
        self.assertEqual(second['generation'], 2)
        self.hub.env.update(ASHA_HUB_SESSION_ID=sid, ASHA_HUB_GENERATION='1')
        with mock.patch('lib.control.harness.caller_descends_from', return_value=True):
            with self.assertRaisesRegex(StoreError, 'stale or inactive'):
                self.hub.handoff(second['request_id'], outcome='no-durable-update', detail='stale generation')
        self.assertIsNone(self.hub.get(sid)['closure']['handoff'])

    def test_resume_is_refused_while_a_close_is_pending(self):
        row = self.launch()
        self.hub.close(row['session_id'])
        with self.assertRaisesRegex(StoreError, 'close request is pending'):
            self.hub.resume(row['session_id'], prompt='Continue')

    def test_silence_marker_makes_memory_unavailable_and_blocks_publication(self):
        (self.project / 'Work/markers').mkdir(parents=True)
        (self.project / 'Work/markers/silence').touch()
        row = self.launch()
        sid = row['session_id']
        record = self.hub.close(sid)['closure']
        self.assertFalse(record['memory']['available'])
        self.assertIn('silence', record['memory']['reason'])
        self.assertIn('Project memory is unavailable', self.hub.messages(sid)[0]['body'])
        active_file, decisions_file = self.drafts()
        with self.acting_as(sid):
            with self.assertRaisesRegex(StoreError, 'unavailable'):
                self.hub.handoff(record['request_id'], active_file=active_file, decisions_file=decisions_file, expected=self.digests())
            self.hub.handoff(record['request_id'], outcome='blocked', detail='silenced')
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'handoff-failed')
        self.assertNotEqual((self.memory / 'activeContext.md').read_text(), ACTIVE)

    def test_uninitialized_project_asks_for_an_explicit_no_op(self):
        row = self.launch()
        (self.project / '.asha/config.json').write_text('{"initialized": true}')
        record = self.hub.close(row['session_id'])['closure']
        self.assertFalse(record['memory']['available'])
        self.assertIsNone(record['memory']['destination'])

    def test_force_close_and_stop_never_claim_a_save(self):
        row = self.launch()
        forced = self.hub.close(row['session_id'], force=True)
        self.assertEqual(forced['lifecycle'], 'closed')
        self.assertEqual(forced['closure']['state'], 'forced')
        self.assertFalse(forced['closure']['memory']['saved'])
        self.assertIsNone(forced['closure']['handoff'])
        second = self.launch(name='Second')
        self.tmux.sessions.clear()
        self.tmux.pane_options.clear()
        # The fake models one pane; the second launch owns it now.
        other = self.launch(name='Third')
        stopped = self.hub.stop(other['session_id'])
        self.assertEqual(stopped['lifecycle'], 'stopped')
        self.assertEqual(stopped['closure']['state'], 'forced')
        self.assertFalse(stopped['closure']['memory']['saved'])
        self.assertIsNotNone(second)

    def test_force_after_verified_handoff_keeps_the_evidence(self):
        row = self.launch()
        sid = row['session_id']
        rid = self.hub.close(sid)['closure']['request_id']
        active_file, decisions_file = self.drafts()
        with self.acting_as(sid):
            self.hub.handoff(rid, active_file=active_file, decisions_file=decisions_file, expected=self.digests())
        forced = self.hub.close(sid, force=True)['closure']
        self.assertEqual(forced['state'], 'forced')
        self.assertTrue(forced['memory']['saved'])
        self.assertEqual(forced['handoff']['outcome'], 'published')
        self.assertIn('after a verified handoff', forced['guidance'])

    def test_harness_exit_before_answer_is_undeliverable_not_success(self):
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        with self.acting_as(sid):
            self.hub.observe('session-ended')
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'undeliverable')
        self.tmux.dead = True
        closed = self.hub.close(sid)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['closure']['state'], 'undeliverable')
        self.assertFalse(closed['closure']['memory']['saved'])

    def test_close_without_a_live_agent_records_unavailable(self):
        row = self.launch()
        self.tmux.sessions.clear()
        closed = self.hub.close(row['session_id'])
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['closure']['state'], 'unavailable')
        self.assertFalse(closed['closure']['memory']['saved'])

    def test_reading_the_queued_request_counts_as_delivery(self):
        row = self.launch()
        sid = row['session_id']
        record = self.hub.close(sid)['closure']
        with self.acting_as(sid):
            self.hub.acknowledge(record['delivery']['message_id'])
        record = self.hub.show(sid)['closure']
        self.assertEqual(record['state'], 'delivered')
        self.assertEqual(record['delivery']['channel'], 'queued-message')

    def test_harness_without_stop_seam_only_queues(self):
        row = self.launch(harness='copilot')
        sid = row['session_id']
        record = self.hub.close(sid)['closure']
        self.assertEqual(record['delivery']['channel'], 'queued-message')
        self.assertIn('Stop', record['guidance'])
        with self.acting_as(sid):
            self.assertIsNone(self.hub.stop_decision(self.hub.observe('turn-stopped')))
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'unanswered')

    def test_wait_finalizes_after_the_acknowledgement_arrives(self):
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        rid = self.hub.get(sid)['closure']['request_id']
        from lib.control.session_hub import Hub
        worker = Hub(self.config, env=self.env, tmux=self.tmux)
        def acknowledge():
            time.sleep(0.3)
            with mock.patch.object(worker, 'actor', side_effect=lambda: worker.get(sid)):
                worker.observe('tool-started', tool_kind='finalizer', tool_token='wait-finalizer')
                worker.handoff(rid, outcome='no-durable-update', detail='nothing durable')
                worker.observe('tool-completed', tool_kind='finalizer', tool_token='wait-finalizer')
                worker.observe('turn-stopped')
        thread = threading.Thread(target=acknowledge)
        thread.start()
        try:
            closed = self.hub.close(sid, wait=5)
        finally:
            thread.join()
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['closure']['state'], 'completed')
        with self.assertRaises(StoreError):
            self.hub.close(str(__import__('uuid').uuid4()), wait=601)

    def test_wait_returns_pending_state_without_terminating(self):
        row = self.launch()
        started = time.monotonic()
        result = self.hub.close(row['session_id'], wait=1)
        self.assertGreaterEqual(time.monotonic() - started, 0.9)
        self.assertEqual(result['lifecycle'], 'closing')
        self.assertEqual(self.tmux.killed, [])

    def test_event_verb_prints_the_decision_once_and_otherwise_an_empty_object(self):
        import io
        from contextlib import redirect_stdout
        from lib.control import hub_cli
        row = self.launch()
        sid = row['session_id']
        self.hub.close(sid)
        env = dict(self.env, ASHA_HUB_SESSION_ID=sid, ASHA_HUB_GENERATION='1')
        def run(event):
            out = io.StringIO()
            with mock.patch('lib.control.session_hub.Hub.actor', new=lambda self: self.get(sid)), redirect_stdout(out):
                self.assertEqual(hub_cli.dispatch(['event', '--event', event], env=env), 0)
            return out.getvalue()
        self.assertEqual(run('tool-completed'), '{}\n')
        first = run('turn-stopped')
        self.assertEqual(first.count('\n'), 1)
        self.assertEqual(json.loads(first)['decision'], 'block')
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'delivered')
        self.assertEqual(run('turn-stopped'), '{}\n')
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'unanswered')

    def test_cli_refuses_a_bare_close_on_a_legacy_room(self):
        import io
        from contextlib import redirect_stderr
        from lib.control import hub_cli
        from lib.control.rooms import open_room
        from pathlib import Path
        room = open_room(name='Legacy', project=str(self.project), harness='claude', prompt='Hi', config=self.config,
                         env=self.env, tmux=self.tmux, asha_root=Path(__file__).resolve().parents[2],
                         executable_finder=lambda _: '/usr/bin/true')
        self.hub.initialize()
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(hub_cli.dispatch(['close', room['room_id']], env=self.env), 2)
            self.assertEqual(hub_cli.dispatch(['close', room['room_id'], '--force', '--wait', '9'], env=self.env), 2)
        self.assertIn('--force', err.getvalue())
        self.assertEqual(self.tmux.killed, [])


class StructuredClosureTests(ClosureFixture):
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

    def test_close_queues_one_final_turn_and_waits_for_its_acknowledgement(self):
        from lib.control.session_store import SessionStore
        sid = self.structured()
        wakes = self.supervisor.call_count
        closing = self.hub.close(sid)
        self.assertEqual(closing['lifecycle'], 'closing')
        record = closing['closure']
        self.assertEqual(record['delivery']['channel'], 'structured-turn')
        self.assertEqual(self.supervisor.call_count, wakes + 1)
        self.hub.close(sid)
        with SessionStore(self.config) as sessions, sessions.db.transaction() as c:
            keys = [r[0] for r in c.execute("SELECT delivery_key FROM session_messages WHERE session_id=? AND delivery_key LIKE 'close:%'", (sid,))]
        self.assertEqual(keys, ['close:' + record['request_id']])
        self.assertEqual(self.hub.get(sid)['lifecycle'], 'closing')
        session, turn = self.consume_close_turn(sid)
        self.assertEqual(turn['delivery_key'], 'close:' + record['request_id'])
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'delivered')
        worker = self.worker_hub(sid)
        worker.env['ASHA_MANAGED_TURN_ID'] = turn['turn_id']
        with SessionStore(self.config) as sessions:
            sessions.observe(sid, session['generation'], turn['turn_id'], 'tool',
                             dict(tool_id='handoff', completion_kind='finalizer', status='inProgress'))
        with mock.patch.object(worker, 'structured_actor', return_value=(worker.get(sid), turn['delivery_key'])):
            result = worker.handoff(record['request_id'], outcome='no-durable-update', detail='utility only read')
        self.assertEqual(result['closure_state'], 'acknowledged')
        with SessionStore(self.config) as sessions:
            sessions.observe(sid, session['generation'], turn['turn_id'], 'tool',
                             dict(tool_id='handoff', completion_kind='finalizer', status='completed'))
            sessions.finish(sid, session['generation'], turn['turn_id'], success=True)
        self.release_owner(sid)
        closed = self.hub.close(sid)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['closure']['state'], 'completed')
        with SessionStore(self.config) as sessions:
            self.assertEqual(sessions.get(sid)['state'], 'stopped')

    def test_structured_turn_that_ends_without_handoff_is_unanswered_then_reasked(self):
        from lib.control.session_store import SessionStore
        sid = self.structured()
        record = self.hub.close(sid)['closure']
        session, turn = self.consume_close_turn(sid)
        with SessionStore(self.config) as sessions:
            sessions.finish(sid, session['generation'], turn['turn_id'], success=True)
        self.release_owner(sid)
        self.assertEqual(self.hub.show(sid)['closure']['state'], 'unanswered')
        with SessionStore(self.config) as sessions:
            self.assertEqual(sessions.get(sid)['state'], 'idle')
        rearmed = self.hub.close(sid)['closure']
        self.assertEqual(rearmed['attempts'], 2)
        self.assertEqual(rearmed['request_id'], record['request_id'])
        with SessionStore(self.config) as sessions, sessions.db.transaction() as c:
            queued = [r[0] for r in c.execute("SELECT delivery_key FROM session_messages WHERE session_id=? AND state='queued'", (sid,))]
        self.assertEqual(queued, ['close:' + record['request_id'] + ':2'])

    def test_structured_handoff_must_come_from_the_close_turn(self):
        sid = self.structured()
        record = self.hub.close(sid)['closure']
        worker = self.worker_hub(sid)
        with mock.patch.object(worker, 'structured_actor', return_value=(worker.get(sid), 'other-message')):
            with self.assertRaisesRegex(StoreError, 'did not receive the current close request'):
                worker.handoff(record['request_id'], outcome='no-durable-update', detail='wrong turn')
        self.assertIsNone(self.hub.get(sid)['closure']['handoff'])

    def test_interrupted_structured_launch_closes_with_guidance(self):
        from lib.control.session_store import SessionStore
        with mock.patch.object(SessionStore, '_create_in_transaction', side_effect=StoreError('quota exhausted')):
            with self.assertRaises(StoreError):
                self.launch(transport='structured')
        sid = self.hub.list(include_closed=True)['rows'][0]['session_id']
        closed = self.hub.close(sid)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['closure']['state'], 'unavailable')
        self.assertIn('No live agent', closed['closure']['guidance'])

    def test_stopped_structured_session_is_unavailable_for_handoff(self):
        from lib.control.session_store import SessionStore
        sid = self.structured()
        with SessionStore(self.config) as sessions:
            sessions.stop(sid)
        closed = self.hub.close(sid)
        self.assertEqual(closed['lifecycle'], 'closed')
        self.assertEqual(closed['closure']['state'], 'unavailable')
        self.assertFalse(closed['closure']['memory']['saved'])


if __name__ == '__main__':
    unittest.main()
