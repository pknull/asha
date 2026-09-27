"""Best-effort close D2: a bounded timestamp-ignore for late hook reports; D5/D11 compatibility.

Controller fixtures, not native delivery proof. Each hook forwards when it
fired; the hub skips a report older than the newest one it applied by at most
30 s, with no files, locks or proofs.
"""
import io
import json
from contextlib import redirect_stdout
from unittest import mock

from lib.control.session_hub import STALE_REPORT_SECONDS
from tests.python.test_control_session_closure import ClosureFixture


class EmittedAtTests(ClosureFixture):
    def setUp(self):
        super().setUp()
        self.sid = self.launch()['session_id']
        self.actor = self.enterContext(mock.patch.object(self.hub, 'actor',
                                                         side_effect=lambda: self.hub.get(self.sid)))

    def test_an_older_report_within_the_window_changes_nothing(self):
        self.hub.observe('prompt-submitted', emitted_at=1000.0)
        before = self.hub.get(self.sid)
        result = self.hub.observe('turn-stopped', emitted_at=990.0, background_tasks=2)
        self.assertEqual(result['observation'], 'ignored')
        after = self.hub.get(self.sid)
        self.assertEqual(after['activity'], 'working')
        self.assertEqual(after['native_emitted_at'], 1000.0)
        self.assertIsNone(after.get('background_tasks'))
        self.assertEqual(after['updated_at'], before['updated_at'], 'a skipped report writes nothing')

    def test_equal_and_newer_stamps_apply(self):
        self.hub.observe('prompt-submitted', emitted_at=1000.0)
        self.assertEqual(self.hub.observe('turn-stopped', emitted_at=1000.0)['observation'], 'applied')
        self.assertEqual(self.hub.get(self.sid)['activity'], 'idle')
        self.hub.observe('prompt-submitted', emitted_at=1001.5)
        self.assertEqual(self.hub.get(self.sid)['native_emitted_at'], 1001.5)

    def test_a_report_more_than_30_seconds_older_applies(self):
        # A backward clock step must not reject every later event.
        self.hub.observe('prompt-submitted', emitted_at=1000.0)
        stepped = 1000.0 - STALE_REPORT_SECONDS - 0.5
        self.assertEqual(self.hub.observe('turn-stopped', emitted_at=stepped)['observation'], 'applied')
        current = self.hub.get(self.sid)
        self.assertEqual(current['activity'], 'idle')
        self.assertEqual(current['native_emitted_at'], stepped)

    def test_an_unstamped_report_applies_unconditionally(self):
        self.hub.observe('prompt-submitted', emitted_at=1000.0)
        self.assertEqual(self.hub.observe('turn-stopped')['observation'], 'applied')
        self.assertEqual(self.hub.get(self.sid)['activity'], 'idle')

    def test_a_skipped_report_suppresses_question_and_lifecycle_effects(self):
        self.hub.observe('prompt-submitted', emitted_at=1000.0)
        self.hub.observe('permission-requested', emitted_at=995.0, body='May I?')
        self.assertIsNone(self.hub.get(self.sid).get('question'))
        self.hub.close(self.sid)
        self.hub.observe('session-ended', emitted_at=995.0)
        self.assertEqual(self.hub.get(self.sid)['closure']['state'], 'pending-delivery')

    def test_explicit_reports_bypass_the_guard(self):
        self.hub.observe('prompt-submitted', emitted_at=1000.0)
        self.hub.observe(None, state='needs-input', body='Which branch?')
        self.assertEqual(self.hub.get(self.sid)['activity'], 'needs-input')

    def test_a_worker_report_cannot_carry_a_stamp(self):
        with self.assertRaisesRegex(Exception, 'hook evidence'):
            self.hub.observe(None, state='working', emitted_at=1000.0)

    def test_a_new_generation_resets_the_stamp(self):
        self.hub.observe('prompt-submitted', emitted_at=1000.0)
        self.hub.stop(self.sid)
        self.hub.resume(self.sid, prompt='Continue', learning_ids=[])
        self.assertIsNone(self.hub.get(self.sid)['native_emitted_at'])
        self.assertEqual(self.hub.observe('turn-stopped', emitted_at=990.0)['observation'], 'applied')


class HookCompatibilityTests(ClosureFixture):
    def event(self, sid, *argv):
        from lib.control import hub_cli
        out = io.StringIO()
        with mock.patch.object(hub_cli, 'Hub', return_value=self.hub), \
                mock.patch.object(self.hub, 'actor', side_effect=lambda: self.hub.get(sid)), redirect_stdout(out):
            code = hub_cli.dispatch(['event', *argv], env=self.env)
        return code, out.getvalue().strip()

    def test_obsolete_hook_arguments_are_accepted_and_ignored(self):
        sid = self.launch()['session_id']
        code, out = self.event(sid, '--event', 'tool-started', '--tool-kind', 'finalizer', '--tool-token', 'x',
                               '--order', '7', '--attempts', '9', '--sequence', '3', '--sequence-pane', '%1')
        self.assertEqual((code, out), (0, '{}'))
        self.assertEqual(self.hub.get(sid)['activity'], 'working')

    def test_a_skipped_stop_returns_no_close_decision(self):
        sid = self.launch()['session_id']
        self.hub.close(sid)
        self.event(sid, '--event', 'prompt-submitted', '--emitted-at', '1000.0')
        code, out = self.event(sid, '--event', 'turn-stopped', '--emitted-at', '995.0')
        self.assertEqual((code, out), (0, '{}'))
        self.assertEqual(self.hub.get(sid)['closure']['state'], 'pending-delivery')
        code, out = self.event(sid, '--event', 'turn-stopped', '--emitted-at', '1001.0')
        self.assertEqual(json.loads(out)['decision'], 'block')

    def test_retired_control_settings_still_load(self):
        self.configure_control(idle_delivery=True, no_handoff_close=True)
        self.assertFalse(hasattr(self.config, 'idle_delivery'))
        self.assertFalse(hasattr(self.config, 'no_handoff_close'))
