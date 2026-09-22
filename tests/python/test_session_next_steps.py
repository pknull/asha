import unittest

from lib.control import session_tui


class NextStepRenderingTests(unittest.TestCase):
    def row(self, **changes):
        return dict(dict(session_id='one', generation=1, activity='unknown',
                         project_name='asha', name='Session', harness='codex',
                         transport='terminal', profile='worker', lifecycle='open',
                         reason='Observed', process_state='live'), **changes)

    def test_every_hint_state_renders_without_replacing_raw_activity(self):
        cases = [
            ({'activity': 'finished'}, 'Done: close'),
            ({'activity': 'finished', 'completion_readiness': {'status': 'stale'}}, 'Result ready: needs handoff'),
            ({'activity': 'finished', 'completion_readiness': {'status': 'ready'}}, 'Finalized: close'),
            ({'activity': 'finished', 'process_state': 'ended'}, 'Done: close record'),
            ({'activity': 'exited', 'process_state': 'ended'}, 'Ended unreported: check work'),
            ({'activity': 'idle', 'profile': 'room'}, 'Waiting for you'),
            ({'activity': 'idle'}, 'Stopped mid-task?'),
            ({'activity': 'closing', 'native_activity': 'idle', 'closure': {
                'generation': 1, 'state': 'pending-delivery'}}, 'Close needs attach'),
            ({'activity': 'needs-input'}, 'Answer in terminal (attach)'),
            ({'activity': 'permission-requested'}, 'Answer in terminal (attach)'),
            ({'activity': 'working'}, 'Working'),
        ]
        for changes, hint in cases:
            with self.subTest(hint=hint):
                row = self.row(**changes)
                original = dict(row)
                rendered = '\n'.join(session_tui.lines({'rows': [row]}, width=110))
                self.assertIn(hint, rendered)
                self.assertEqual(row, original)
                self.assertNotIn('Memory saved', rendered)
                self.assertNotIn('landed', rendered)

    def test_ended_sessions_have_a_separate_group(self):
        live = self.row(activity='working')
        ended = self.row(session_id='two', activity='exited', process_state='ended')
        lines = session_tui.lines({'rows': [live, ended]}, width=110)
        text = '\n'.join(lines)
        self.assertIn('Ended sessions', text)
        self.assertLess(text.index('Working'), text.index('Ended sessions'))
        self.assertLess(text.index('Ended sessions'), text.index('Ended unreported'))

    def test_save_label_requires_publication_evidence_not_a_status_or_result(self):
        row = self.row(activity='finished', result='Saved and pushed', memory_saved_at=0)
        self.assertIn('Memory saved 00:00 UTC', '\n'.join(session_tui.lines({'rows': [row]})))
        row.pop('memory_saved_at')
        row['closure'] = {'generation': 1, 'state': 'acknowledged',
                          'handoff': {'outcome': 'no-durable-update'}}
        self.assertNotIn('Memory saved', '\n'.join(session_tui.lines({'rows': [row]})))

    def test_overview_orders_current_before_ended_without_changing_raw_fields(self):
        from unittest import mock
        from lib.control.hub_cli import overview
        ended = self.row(session_id='ended', activity='exited', process_state='ended')
        live = self.row(session_id='live', activity='working')
        hub = mock.Mock()
        hub.list.return_value = {'rows': [ended, live], 'complete': True}
        hub.initialized.return_value = False
        with mock.patch('lib.control.hub_cli.Hub', return_value=hub), \
                mock.patch('lib.control.rooms.RoomStore') as rooms, \
                mock.patch('lib.control.sessions.overview', return_value={}):
            rooms.return_value.bounded_active_snapshots.return_value = []
            result = overview(object(), tmux=object())
        self.assertEqual([r['session_id'] for r in result['rows']], ['live', 'ended'])
        self.assertEqual(result['rows'][1]['activity'], 'exited')
        self.assertIn('1 current; 1 ended', result['summary'])
