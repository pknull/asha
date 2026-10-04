"""Issue #114: a settled completion survives a later turn that makes no new report.

Controller fixtures, not native delivery proof. A finished worker can be woken
for another turn without new work: Claude starts one when a background Monitor
expires or a background task completes, and that turn reaches the hub as
``prompt-submitted``. Such a turn must not erase the completion report. While
it runs the row reads reported-but-working and a close asks instead of
terminating; when it ends without a new report the row is finished again. Only
a new report, a new Control assignment or a resume replaces the report. Each
harness is driven with the events its hooks actually emit: Claude (Stop with a
background task count), Codex (Stop, no background count) and Copilot (no
turn-end event: the five-minute staleness fallback of #109).
"""
from lib.control import session_view
from tests.python.test_control_session_closure import FastClose

QUIET = 301


class LaterTurnFixture(FastClose):
    def finish(self, sid, *, stop=True, background_tasks=None):
        """Report finished and save inside the last turn, then end that turn."""
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.observe('tool-started')
            self.hub.report(state='finished', body='Done')
            self.hub.observe('tool-completed')
            self.hub.handoff(None, outcome='no-durable-update', detail='Nothing durable')
            if stop:
                self.hub.observe('turn-stopped', background_tasks=background_tasks)

    def wake(self, sid, *events):
        """A later turn with no new report, e.g. a Monitor expiry notification."""
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            for event in events:
                self.hub.observe(event)

    def quiet(self, sid, seconds=QUIET):
        """Age every observation of the row by ``seconds``."""
        row = self.hub.get(sid)
        report = dict(row['completion_report'], reported_at=row['completion_report']['reported_at'] - seconds)
        changes = dict(completion_report=report, observed_at=row['observed_at'] - seconds)
        if row.get('native_observed_at') is not None:
            changes['native_observed_at'] = row['native_observed_at'] - seconds
        self.hub._update(sid, **changes)

    def assert_finished(self, sid):
        shown = self.hub.show(sid)
        self.assertEqual(shown['activity'], 'finished', shown['next_step'])
        self.assertTrue(shown['next_step'].startswith('Finished, saved '), shown['next_step'])
        self.assertEqual(session_view.state_section(shown), 'state:ready')
        self.assertIsNotNone(shown['completion_report'])
        self.assertEqual(shown['result'], 'Done')
        return shown

    def assert_reported_working(self, sid):
        shown = self.hub.show(sid)
        self.assertEqual(shown['activity'], 'working', shown['next_step'])
        self.assertEqual(shown.get('reported_activity'), 'finished')
        self.assertTrue(shown['next_step'].startswith('Working: reported finished'), shown['next_step'])
        self.assertNotEqual(session_view.state_section(shown), 'state:ready')
        return shown


class LaterTurnTests(LaterTurnFixture):
    def test_claude_wake_after_a_settled_report_stays_finished(self):
        sid = self.launch(harness='claude')['session_id']
        self.finish(sid)
        epoch = self.assert_finished(sid)['assignment_epoch']
        self.wake(sid)
        self.assert_reported_working(sid)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        shown = self.assert_finished(sid)
        self.assertNotIn('Stopped mid-task?', shown['next_step'])
        self.assertEqual(shown['assignment_epoch'], epoch, 'a turn without a report is not a new assignment')
        self.assertEqual(self.request(sid)['lifecycle'], 'closed', 'the save still covers the assignment (D7)')

    def test_claude_report_settles_at_the_monitor_wake_turn(self):
        # The #114 evidence: the reporting turn ended with its Monitor still
        # running, so the report settled only at the turn the expiry woke.
        sid = self.launch(harness='claude')['session_id']
        self.finish(sid, background_tasks=1)
        self.assertNotEqual(session_view.state_section(self.hub.show(sid)), 'state:ready')
        self.wake(sid, 'tool-started', 'tool-completed')
        self.assert_reported_working(sid)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        self.assert_finished(sid)
        self.assertEqual(self.request(sid)['lifecycle'], 'closed')

    def test_a_wake_that_leaves_background_work_settles_at_the_next_clean_stop(self):
        sid = self.launch(harness='claude')['session_id']
        self.finish(sid)
        self.wake(sid)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', background_tasks=1)
        self.assertNotEqual(session_view.state_section(self.hub.show(sid)), 'state:ready')
        self.wake(sid, 'turn-stopped')
        self.assert_finished(sid)

    def test_a_running_later_turn_is_not_closed_at_once(self):
        sid = self.launch(harness='claude')['session_id']
        self.finish(sid)
        self.wake(sid, 'tool-started')
        self.assertEqual(self.request(sid)['lifecycle'], 'closing', 'a close must not kill the later turn')
        self.hub.close(sid, force=True)

    def test_codex_wake_without_a_report_stays_finished(self):
        sid = self.launch(harness='codex')['session_id']
        self.finish(sid)
        self.assert_finished(sid)
        self.wake(sid, 'tool-started', 'tool-completed')
        self.assert_reported_working(sid)
        self.assertEqual(self.request(sid)['lifecycle'], 'closing')
        self.hub.close(sid, force=True)

        sid = self.launch(harness='codex', session_id='44444444-4444-4444-8444-444444444444')['session_id']
        self.finish(sid)
        self.wake(sid, 'tool-started', 'tool-completed', 'turn-stopped')
        self.assert_finished(sid)
        self.assertEqual(self.request(sid)['lifecycle'], 'closed')

    def test_copilot_wake_without_a_report_settles_on_staleness(self):
        sid = self.launch(harness='copilot')['session_id']
        self.finish(sid, stop=False)
        self.quiet(sid)
        self.assert_finished(sid)
        self.wake(sid, 'tool-completed')
        self.assert_reported_working(sid)
        self.assertEqual(self.request(sid)['lifecycle'], 'closing')
        self.hub.close(sid, force=True)

        sid = self.launch(harness='copilot', session_id='44444444-4444-4444-8444-444444444444')['session_id']
        self.finish(sid, stop=False)
        self.quiet(sid)
        self.wake(sid, 'tool-completed')
        self.quiet(sid)
        self.assert_finished(sid)
        self.assertEqual(self.request(sid)['lifecycle'], 'closed')


class LaterTurnReplacementTests(LaterTurnFixture):
    """Only a new report, a new Control assignment or a resume replaces a completion."""

    def test_a_new_working_report_in_the_later_turn_replaces_it(self):
        sid = self.launch(harness='claude')['session_id']
        self.finish(sid)
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.report(state='working', body='Picking up the follow-up')
            self.hub.observe('turn-stopped')
        shown = self.hub.show(sid)
        self.assertIsNone(shown['completion_report'])
        self.assertEqual(shown['next_step'], 'Stopped mid-task?')

    def test_a_new_finished_report_in_the_later_turn_is_a_new_assignment(self):
        sid = self.launch(harness='claude')['session_id']
        self.finish(sid)
        epoch = self.hub.get(sid)['assignment_epoch']
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.report(state='finished', body='Second result')
            self.hub.observe('turn-stopped')
        shown = self.hub.show(sid)
        self.assertNotEqual(shown['assignment_epoch'], epoch)
        self.assertEqual(shown['completion_report']['assignment_epoch'], shown['assignment_epoch'])
        self.assertEqual(shown['result'], 'Second result')
        self.assertTrue(shown['next_step'].startswith('Finished, saved '), shown['next_step'])
        # The save belonged to the earlier assignment, so a close asks again.
        self.assertEqual(self.request(sid)['lifecycle'], 'closing')
        self.hub.close(sid, force=True)

    def test_a_control_assignment_replaces_it(self):
        sid = self.launch(harness='claude')['session_id']
        self.finish(sid)
        self.hub.send(sid, 'Another assignment', key='next', learning_ids=[])
        self.wake(sid, 'turn-stopped')
        shown = self.hub.show(sid)
        self.assertNotEqual(shown['activity'], 'finished')
        self.assertEqual(shown['next_step'], 'Stopped mid-task?')

    def test_a_control_assignment_during_the_later_turn_keeps_its_epoch(self):
        sid = self.launch(harness='claude')['session_id']
        self.finish(sid)
        self.wake(sid)
        self.hub.send(sid, 'Another assignment', key='next', learning_ids=[])
        sent = self.hub.get(sid)['assignment_epoch']
        with self.acting_as(sid):
            self.hub.report(state='finished', body='Second result')
        self.assertEqual(self.hub.get(sid)['assignment_epoch'], sent)
        self.assertEqual(self.hub.get(sid)['completion_report']['assignment_epoch'], sent)

    def test_a_resume_replaces_it(self):
        sid = self.launch(harness='claude')['session_id']
        self.finish(sid)
        self.hub.stop(sid)
        self.hub.resume(sid, prompt='Continue', learning_ids=[])
        self.wake(sid, 'turn-stopped')
        shown = self.hub.show(sid)
        self.assertNotEqual(shown['activity'], 'finished')
        self.assertEqual(shown['next_step'], 'Stopped mid-task?')
