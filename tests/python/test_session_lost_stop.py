"""Issue #110: a Stop the bridge gave up on is delivered late, never lost for good.

Controller fixtures, not native delivery proof. The bridge abandons a slow
`session event` at its budget and records the loss through a detached
`session event-lost` call. For a turn-stopped that call also delivers the Stop,
so a finished report (#109) settles instead of reading "Working: reported
finished" forever. The detached call is not part of the session's process tree,
so it proves identity by the generation's bound native conversation instead,
and it applies only when no newer hook report has been applied.
"""
import io
from contextlib import redirect_stdout
from unittest import mock

from lib.control import session_view
from tests.python.test_control_session_closure import FastClose

THREAD = 'thread-110'


class LostStopTests(FastClose):
    def act(self, sid, event, **kwargs):
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            return self.hub.observe(event, **kwargs)

    def reported_mid_turn(self, harness='claude'):
        """Prompt (binding the native conversation), report finished, save, keep working."""
        sid = self.launch(harness=harness)['session_id']
        self.act(sid, 'prompt-submitted', native_id=THREAD, emitted_at=1000.0)
        self.act(sid, 'tool-started', native_id=THREAD, emitted_at=1001.0)
        with mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)):
            self.hub.report(state='finished', body='Done')
            self.hub.handoff(None, outcome='no-durable-update', detail='Nothing durable')
        self.act(sid, 'tool-completed', native_id=THREAD, emitted_at=1002.0)
        return sid

    def lost(self, sid, *argv):
        """The bridge's detached loss call: no process-tree proof, so no actor patch."""
        from lib.control import hub_cli
        out = io.StringIO()
        env = dict(self.env, ASHA_HUB_SESSION_ID=sid, ASHA_HUB_GENERATION='1')
        # The CLI builds its Hub from the caller's environment.
        with mock.patch.object(hub_cli, 'Hub', return_value=self.hub), mock.patch.object(self.hub, 'env', env), \
                mock.patch.object(self.hub, 'actor', side_effect=AssertionError('no process-tree proof')), \
                redirect_stdout(out):
            code = hub_cli.dispatch(['event-lost', '--reason', 'bridge-timeout', '--budget', '3.0', *argv], env=env)
        self.assertEqual((code, out.getvalue().strip()), (0, '{}'))

    def lost_stop(self, sid, emitted_at='1003.0', native_id=THREAD, *extra):
        argv = ['--event', 'turn-stopped', '--emitted-at', emitted_at]
        if native_id:
            argv += ['--native-id', native_id]
        self.lost(sid, *argv, *extra)

    def assert_unsettled(self, sid):
        shown = self.hub.show(sid)
        self.assertTrue(shown['next_step'].startswith('Working: reported finished'), shown['next_step'])
        self.assertNotEqual(session_view.state_section(shown), 'state:ready')

    def test_a_dropped_stop_settles_the_report_through_the_loss_record(self):
        sid = self.reported_mid_turn()
        self.assert_unsettled(sid)          # the bridge dropped the Stop
        self.lost_stop(sid)
        shown = self.hub.show(sid)
        self.assertEqual(shown['activity'], 'finished', shown['next_step'])
        self.assertTrue(shown['next_step'].startswith('Finished, saved '), shown['next_step'])
        self.assertEqual(self.request(sid)['lifecycle'], 'closed')

    def test_a_dropped_stop_without_a_report_reads_idle(self):
        sid = self.launch(harness='codex')['session_id']
        self.act(sid, 'prompt-submitted', native_id=THREAD, emitted_at=1000.0)
        self.lost_stop(sid)
        self.assertEqual(self.hub.get(sid)['activity'], 'idle')

    def test_the_loss_is_still_counted(self):
        from lib.control import session_hub
        sid = self.reported_mid_turn()
        self.lost_stop(sid)
        import json
        entries = [json.loads(line) for line in session_hub.loss_log_path(self.config).read_text().splitlines()]
        self.assertEqual([(e['reason'], e['event']) for e in entries], [('bridge-timeout', 'turn-stopped')])

    def test_another_conversation_cannot_settle_the_report(self):
        sid = self.reported_mid_turn()
        self.lost_stop(sid, native_id='other-thread')
        self.assert_unsettled(sid)

    def test_an_unnamed_stop_is_not_delivered(self):
        sid = self.reported_mid_turn()
        self.lost_stop(sid, native_id=None)
        self.assert_unsettled(sid)

    def test_a_stop_for_an_unbound_generation_is_not_delivered(self):
        sid = self.launch(harness='claude')['session_id']
        self.act(sid, 'prompt-submitted', emitted_at=1000.0)   # no native ID: nothing bound
        self.lost_stop(sid)
        self.assertEqual(self.hub.get(sid)['activity'], 'working')

    def test_an_unstamped_stop_is_not_delivered(self):
        sid = self.reported_mid_turn()
        self.lost(sid, '--event', 'turn-stopped', '--native-id', THREAD)
        self.assert_unsettled(sid)

    def test_a_newer_applied_report_wins_at_any_age(self):
        sid = self.reported_mid_turn()
        # The next prompt was applied first, even far more than 30 s later.
        self.act(sid, 'prompt-submitted', native_id=THREAD, emitted_at=1100.0)
        self.lost_stop(sid)
        current = self.hub.get(sid)
        self.assertEqual(current['activity'], 'working')
        self.assertEqual(current['native_emitted_at'], 1100.0)

    def test_a_stop_listing_background_work_is_not_a_turn_end(self):
        sid = self.reported_mid_turn()
        self.lost_stop(sid, '1003.0', THREAD, '--background-tasks', '2')
        self.assert_unsettled(sid)
        self.assertEqual(self.hub.get(sid)['background_tasks'], 2)

    def test_a_stale_generation_cannot_deliver(self):
        sid = self.reported_mid_turn()
        from lib.control import hub_cli
        env = dict(self.env, ASHA_HUB_SESSION_ID=sid, ASHA_HUB_GENERATION='2')
        with mock.patch.object(hub_cli, 'Hub', return_value=self.hub), mock.patch.object(self.hub, 'env', env), \
                redirect_stdout(io.StringIO()):
            hub_cli.dispatch(['event-lost', '--event', 'turn-stopped', '--reason', 'bridge-timeout',
                              '--emitted-at', '1003.0', '--native-id', THREAD], env=env)
        self.assert_unsettled(sid)

    def test_other_lost_events_change_nothing(self):
        sid = self.reported_mid_turn()
        before = self.hub.get(sid)
        for event in ('tool-completed', 'session-start', 'prompt-submitted'):
            self.lost(sid, '--event', event, '--emitted-at', '1003.0', '--native-id', THREAD)
        self.assertEqual(self.hub.get(sid)['updated_at'], before['updated_at'])
