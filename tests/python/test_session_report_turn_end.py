"""Issue #109 on two axes: a report's turn has ended only at a later Stop.

Controller fixtures, not native delivery proof. A worker reports finished as a
tool call inside its last turn and may keep working after it (save, final
test). The report axis reads finished when the report lands; the observed axis
says whether the turn still runs. While it runs the row reads
reported-but-working. A close terminates at once (D7) only when a waiting
observation emitted after the report shows the turn ended; otherwise it asks
and waits. Harnesses with no turn-end event therefore always ask.
"""
import time

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
        self.assertEqual(shown['report']['state'], 'finished')
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

    def test_a_stop_emitted_before_the_report_never_settles_it(self):
        # D7 happens-after: only a waiting observation emitted after the report
        # ends the reporting turn. A slow bridge can apply an earlier
        # boundary's Stop after the report; a close must not kill the turn.
        sid = self.launch(harness='claude')['session_id']
        earlier = time.time() - 5
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted', emitted_at=earlier - 1)
            self.hub.report(state='finished', body='Done')
            self.hub.handoff(None, outcome='no-durable-update', detail='Nothing durable')
            self.hub.observe('turn-stopped', emitted_at=earlier)
        self.assertNotEqual(session_view.state_section(self.hub.show(sid)), 'state:ready')
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', emitted_at=time.time())
        self.assert_finished(self.hub.show(sid))
        self.assertEqual(self.request(sid)['lifecycle'], 'closed')

    def test_a_late_stop_does_not_close_the_reporting_turn(self):
        sid = self.launch(harness='codex')['session_id']
        earlier = time.time() - 5
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted', emitted_at=earlier - 1)
            self.hub.report(state='finished', body='Done')
            self.hub.handoff(None, outcome='no-durable-update', detail='Nothing durable')
            self.hub.observe('turn-stopped', emitted_at=earlier)
        self.assertEqual(self.request(sid)['lifecycle'], 'closing', 'the Stop predates the report')
        self.hub.close(sid, force=True)

    def test_background_stop_is_not_a_turn_end(self):
        sid = self.launch(harness='claude')['session_id']
        self.reported_mid_turn(sid)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped', background_tasks=1)
        self.assertNotEqual(session_view.state_section(self.hub.show(sid)), 'state:ready')
        self.assertEqual(self.request(sid)['lifecycle'], 'closing')
        self.hub.close(sid, force=True)

    def quiet(self, sid, seconds=QUIET):
        """Age the row's last native observation by ``seconds``."""
        row = self.hub.get(sid)
        self.hub._update(sid, native_observed_at=row['native_observed_at'] - seconds)

    def test_copilot_without_turn_end_reads_finished_but_closes_by_asking(self):
        # Copilot has no turn-end hook, so nothing ends the reporting turn:
        # once the observation is stale the report reads finished, but a close
        # asks and waits instead of terminating at once.
        sid = self.launch(harness='copilot')['session_id']
        self.reported_mid_turn(sid)
        self.assert_reported_working(self.hub.show(sid))
        self.quiet(sid)
        self.assert_finished(self.hub.show(sid))
        self.assertEqual(self.hub.show(sid)['observed'], 'unknown')
        self.assertEqual(self.request(sid)['lifecycle'], 'closing')
        self.hub.close(sid, force=True)

    def test_staleness_never_ends_the_reporting_turn(self):
        sid = self.launch(harness='codex')['session_id']
        self.reported_mid_turn(sid)
        self.quiet(sid)
        self.assertEqual(self.hub.show(sid)['observed'], 'unknown')
        self.assertEqual(self.request(sid)['lifecycle'], 'closing')
        self.hub.close(sid, force=True)
