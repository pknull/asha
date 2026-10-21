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
            ({'activity': 'finished'}, 'Finished, unsaved'),
            ({'activity': 'finished', 'process_state': 'ended'}, 'Done: close record'),
            ({'activity': 'exited', 'process_state': 'ended'}, 'Ended unreported: check work'),
            ({'activity': 'idle', 'profile': 'room'}, 'Waiting for you'),
            ({'activity': 'idle'}, 'Stopped mid-task?'),
            ({'activity': 'closing', 'lifecycle': 'closing', 'closure': {
                'generation': 1, 'state': 'closing', 'requested_at': 0.0, 'deadline': 60.0}}, 'Closing: waiting for a save'),
            ({'activity': 'needs-input'}, 'Answer in terminal (attach)'),
            ({'activity': 'working'}, 'Working'),
            # #109 on two axes: the hub shows a finished report whose turn still runs as working.
            ({'activity': 'working', 'reported_activity': 'finished'}, 'Working: reported finished'),
            # Observed `launched`: no native event yet, then silent past the window.
            ({'activity': 'starting', 'observed': 'launched'}, 'Starting'),
            ({'activity': 'unknown', 'observed': 'launched'}, 'Hooks not reporting: attach'),
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

    def test_finished_label_names_the_generation_save_or_its_absence(self):
        from lib.control.session_presentation import present
        self.assertEqual(present(self.row(activity='finished'))['next_step'], 'Finished, unsaved')
        self.assertEqual(present(self.row(activity='finished', memory_saved_at=0))['next_step'],
                         'Finished, saved 00:00 UTC')

    def test_ended_sessions_have_a_separate_group(self):
        live = self.row(activity='working')
        ended = self.row(session_id='two', activity='exited', process_state='ended')
        lines = session_tui.lines({'rows': [live, ended]}, width=110)
        text = '\n'.join(lines)
        self.assertIn('▼ Ended', text)
        self.assertLess(text.index('Working'), text.index('▼ Ended'))
        self.assertLess(text.index('▼ Ended'), text.index('Ended unreported'))

    def test_an_ended_legacy_room_is_history_not_an_action(self):
        # #127: a legacy Room has no state past ended, so nothing can act on it.
        from lib.control.session_presentation import present
        room = dict(session_id='r1', room_id='r1', transport='room', profile='room', harness='claude',
                    lifecycle='ended', activity='exited', reason='exact tmux pane is absent',
                    pending_messages=0, observed_at=None)
        shown = present(room)
        self.assertEqual((shown['group'], shown['next_step']), ('history', 'Closed: view history'))
        live = present(dict(room, lifecycle='open', activity='unknown'))
        self.assertEqual(live['group'], 'current')
        # An ended record whose owned pane survived still needs a look.
        survived = present(dict(room, activity='unknown'))
        self.assertNotEqual(survived['group'], 'history')

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
