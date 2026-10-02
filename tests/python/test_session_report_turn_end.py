"""Issue #109: a completion report is finished only once its turn has stopped.

Controller fixtures, not native delivery proof. A worker reports finished as a
tool call inside its last turn and may keep working after it (save, final
test). Until the turn stops the row reads reported-but-working, is not ready
to close, and a close asks instead of terminating at once. Harnesses with no
turn-end event fall back to the five-minute staleness rule.
"""
from lib.control import session_view
from tests.python.test_control_session_closure import FastClose

QUIET = 301


class ReportThenTurnEndTests(FastClose):
    def reported_mid_turn(self, sid):
        """Report finished, save, then keep working inside the same turn."""
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.observe('tool-started')
            self.hub.report(state='finished', body='Done')
            self.hub.observe('tool-completed')
            self.hub.handoff(None, outcome='no-durable-update', detail='Nothing durable')
            self.hub.observe('tool-started')   # the final test run, still in the turn

    def stop_turn(self, sid):
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')

    def assert_reported_working(self, shown):
        self.assertEqual(shown['activity'], 'working', shown['next_step'])
        self.assertEqual(shown.get('reported_activity'), 'finished')
        self.assertTrue(shown['next_step'].startswith('Working: reported finished'), shown['next_step'])
        self.assertNotEqual(session_view.state_section(shown), 'state:ready')

    def assert_finished(self, shown):
        self.assertEqual(shown['activity'], 'finished', shown['next_step'])
        self.assertTrue(shown['next_step'].startswith('Finished, saved '), shown['next_step'])
        self.assertEqual(session_view.state_section(shown), 'state:ready')

    def test_claude_report_then_turn_stop_reads_working_then_finished(self):
        sid = self.launch(harness='claude')['session_id']
        self.reported_mid_turn(sid)
        self.assert_reported_working(self.hub.show(sid))
        with self.acting_as(sid):
            self.hub.observe('tool-completed')
        self.assert_reported_working(self.hub.show(sid))
        self.stop_turn(sid)
        shown = self.hub.show(sid)
        self.assert_finished(shown)
        self.assertNotIn('Stopped mid-task?', shown['next_step'])

    def test_codex_close_after_report_waits_for_the_turn(self):
        sid = self.launch(harness='codex')['session_id']
        self.reported_mid_turn(sid)
        requested = self.request(sid)
        self.assertEqual(requested['lifecycle'], 'closing', 'a close must not terminate a running turn')
        self.hub.close(sid, force=True)

    def test_codex_close_after_report_and_turn_stop_closes_at_once(self):
        sid = self.launch(harness='codex')['session_id']
        self.reported_mid_turn(sid)
        self.stop_turn(sid)
        self.assert_finished(self.hub.show(sid))
        self.assertEqual(self.request(sid)['lifecycle'], 'closed')

    def test_background_stop_is_not_a_turn_end(self):
        sid = self.launch(harness='claude')['session_id']
        self.reported_mid_turn(sid)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', background_tasks=1)
        self.assertNotEqual(session_view.state_section(self.hub.show(sid)), 'state:ready')
        self.assertEqual(self.request(sid)['lifecycle'], 'closing')
        self.hub.close(sid, force=True)

    def quiet(self, sid, seconds=QUIET):
        """Age every observation of the row by ``seconds``."""
        row = self.hub.get(sid)
        report = dict(row['completion_report'], reported_at=row['completion_report']['reported_at'] - seconds)
        changes = dict(completion_report=report, observed_at=row['observed_at'] - seconds)
        if row.get('native_observed_at') is not None:
            changes['native_observed_at'] = row['native_observed_at'] - seconds
        self.hub._update(sid, **changes)

    def test_copilot_without_turn_end_falls_back_to_staleness(self):
        sid = self.launch(harness='copilot')['session_id']
        self.reported_mid_turn(sid)
        self.assert_reported_working(self.hub.show(sid))
        self.assertEqual(self.request(sid)['lifecycle'], 'closing')
        self.hub.close(sid, force=True)

        sid = self.launch(harness='copilot', session_id='44444444-4444-4444-8444-444444444444')['session_id']
        self.reported_mid_turn(sid)
        self.quiet(sid)
        self.assert_finished(self.hub.show(sid))
        self.assertEqual(self.request(sid)['lifecycle'], 'closed')

    def test_a_turn_end_harness_never_settles_on_staleness_alone(self):
        sid = self.launch(harness='codex')['session_id']
        self.reported_mid_turn(sid)
        self.quiet(sid)
        self.assertNotEqual(self.hub.show(sid)['activity'], 'finished')
        self.assertEqual(self.request(sid)['lifecycle'], 'closing')
        self.hub.close(sid, force=True)
