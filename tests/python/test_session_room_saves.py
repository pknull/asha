"""Issue #105: a Room's Memory save is a checkpoint, not completion.

Controller fixtures, not native delivery proof. A Room is an ongoing
conversation: its contract never asks it to report finished after a save, and a
save (or a finished report sent anyway) shows the open Room as saved and idle,
never as finished or ready to close. Workers keep their completion contract.
"""
import json
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture
from lib.control import session_tui, session_view
from lib.control.hub_cli import overview


class RoomSaveFixture(ClosureFixture):
    def room_drafts(self):
        read = self.hub.handoff_read()
        active = self.project.parent / 'draft-active.md'
        decisions = self.project.parent / 'draft-decisions.md'
        active.write_text("# Objective\nStart\n\n# State\nDecided X\n\n# Next\n- Continue\n\n# Blockers\n- None\n")
        decisions.write_text("# Decisions\n\n- Use X.\n")
        return dict(active_file=str(active), decisions_file=str(decisions),
                    expected={'activeContext.md': read['memory']['baseline']['activeContext.md'],
                              'decisions.md': read['memory']['baseline']['decisions.md']})

    def save(self, sid, *, report, stop=True):
        """Publish Memory through the ordinary handoff, then optionally report finished.

        ``stop=False`` inspects the row after the finalizer ends but before the
        native Stop: the agent is still answering after its save (QA26 Q26-F1).
        """
        with self.acting_as(sid):
            self.hub.observe('prompt-submitted')
            self.hub.observe('tool-started')
            self.hub.observe('tool-completed')
            result = self.hub.handoff(None, **self.room_drafts())
            self.assertEqual(result['hub_publication_status'], 'recorded', result)
            if report:
                self.hub.report(state='finished', body='Saved the X decision')
            if stop:
                self.hub.observe('turn-stopped')
        return self.hub.show(sid)

    def listed(self, sid):
        return next(r for r in self.hub.list()['rows'] if r['session_id'] == sid)

    def overview_row(self, sid):
        page = overview(self.config, env=self.env, tmux=self.tmux)
        # The CLI's --json output is this page serialized.
        return next(r for r in json.loads(json.dumps(page))['rows'] if r['session_id'] == sid)

    def dashboard(self, row):
        return '\n'.join(session_tui.lines({'rows': [row]}, width=120))


class RoomContractTests(RoomSaveFixture):
    def briefs(self, profile):
        from lib.control import session_hub
        seen = []
        real = session_hub.open_room
        def capture(**kwargs):
            seen.append(kwargs['prompt'])
            return real(**kwargs)
        with mock.patch('lib.control.session_hub.open_room', side_effect=capture):
            self.launch(profile=profile)
        return seen

    def test_room_contract_treats_saves_as_checkpoints(self):
        brief, = self.briefs('room')
        self.assertNotIn('report completion and end the turn', brief)
        self.assertNotIn('--state finished', brief)
        self.assertIn('checkpoint', brief)
        self.assertIn('asha control session handoff', brief)

    def test_worker_contract_is_unchanged(self):
        from lib.control.session_completion import WORKER_INSTRUCTION
        brief, = self.briefs('worker')
        self.assertIn(WORKER_INSTRUCTION, brief)
        self.assertIn('report completion and end the turn', brief)
        self.assertIn('--state finished', brief)


class RoomSavePresentationTests(RoomSaveFixture):
    def assert_open_and_saved(self, row):
        self.assertEqual(row['activity'], 'idle')
        self.assertEqual(row['group'], 'current')
        self.assertTrue(row['next_step'].startswith('Saved '), row['next_step'])
        self.assertIn('waiting for you', row['next_step'])
        self.assertTrue(row['saved_label'].startswith('saved '), row.get('saved_label'))
        self.assertIsNotNone(row['memory_saved_at'])
        self.assertEqual(session_view.state_section(row), 'state:idle')
        self.assertFalse(session_view.finished(row))

    def test_room_save_then_finished_report_shows_open_and_saved(self):
        sid = self.launch(profile='room')['session_id']
        shown = self.save(sid, report=True)
        self.assert_open_and_saved(shown)
        self.assertEqual(shown['reported_activity'], 'finished', 'the raw report stays visible in JSON')
        self.assert_open_and_saved(self.listed(sid))
        self.assert_open_and_saved(self.overview_row(sid))
        text = self.dashboard(shown)
        self.assertIn('Saved ', text)
        for word in ('Finished', 'Done', 'finished', 'Ready to close'):
            self.assertNotIn(word, text)

    def test_room_save_without_report_is_not_ready_to_close(self):
        sid = self.launch(profile='room')['session_id']
        shown = self.save(sid, report=False)
        self.assert_open_and_saved(shown)
        self.assertNotIn('reported_activity', shown)
        self.assertNotIn('Finished', self.dashboard(shown))

    def assert_saved_and_working(self, row):
        """The post-save turn: still working, saved, never finished or ready to close."""
        self.assertEqual(row['activity'], 'working')
        self.assertEqual(row['group'], 'current')
        self.assertTrue(row['next_step'].startswith('Working'), row['next_step'])
        self.assertTrue(row['saved_label'].startswith('saved '), row.get('saved_label'))
        self.assertEqual(session_view.state_section(row), 'state:working')
        self.assertFalse(session_view.finished(row))

    def saved_before_stop(self, harness):
        """QA26 Q26-F1: the finalizer ended and the turn continues."""
        sid = self.launch(profile='room', harness=harness)['session_id']
        shown = self.save(sid, report=False, stop=False)
        self.assert_saved_and_working(shown)
        self.assert_saved_and_working(self.listed(sid))
        self.assert_saved_and_working(self.overview_row(sid))
        text = self.dashboard(shown)
        for word in ('Finished', 'Ready to close', 'Done'):
            self.assertNotIn(word, text)
        with self.acting_as(sid):
            self.hub.observe('turn-stopped')
        self.assert_open_and_saved(self.hub.show(sid))

    def test_claude_room_after_save_before_stop_is_working_not_ready(self):
        self.saved_before_stop('claude')

    def test_codex_room_after_save_before_stop_is_working_not_ready(self):
        self.saved_before_stop('codex')

    def later_handoff_keeps_the_last_save_time(self, outcome):
        """QA26 Q26-F2: a later handoff in the same assignment never erases the checkpoint.

        An attestation is itself a save (D3), so it only moves the time forward.
        """
        sid = self.launch(profile='room')['session_id']
        saved = self.save(sid, report=False)['memory_saved_at']
        self.assertIsNotNone(saved)
        epoch = self.hub.get(sid)['assignment_epoch']
        with self.acting_as(sid):
            # Same assignment: no new prompt, as in QA26's continuation cases.
            self.hub.handoff(None, outcome=outcome, detail='Nothing new')
            self.hub.observe('turn-stopped')
        again = self.hub.show(sid)
        self.assertEqual(again['assignment_epoch'], epoch)
        if outcome == 'no-durable-update':
            self.assertGreater(again['memory_saved_at'], saved)
        else:
            self.assertEqual(again['memory_saved_at'], saved)
        self.assert_open_and_saved(again)
        self.assertEqual(self.listed(sid)['memory_saved_at'], again['memory_saved_at'])

    def test_later_no_durable_update_keeps_the_last_save_time(self):
        self.later_handoff_keeps_the_last_save_time('no-durable-update')

    def test_later_blocked_handoff_keeps_the_last_save_time(self):
        self.later_handoff_keeps_the_last_save_time('blocked')

    def test_saved_room_with_a_finished_report_closes_at_once(self):
        sid = self.launch(profile='room')['session_id']
        self.save(sid, report=True)
        closed = self.hub.request_close(sid, wait=60)
        self.assertEqual((closed['lifecycle'], closed['closure']['state']), ('closed', 'closed'))
        self.assertTrue(closed['reason'].startswith('Closed, saved '))

    def test_ended_room_keeps_its_ended_presentation(self):
        sid = self.launch(profile='room')['session_id']
        self.save(sid, report=True)
        self.tmux.sessions.clear()
        ended = self.hub.show(sid)
        self.assertEqual(ended['group'], 'ended')
        self.assertEqual(ended['next_step'], 'Done: close record')


class WorkerCompletionTests(RoomSaveFixture):
    def test_worker_save_and_report_is_finished_and_saved(self):
        sid = self.launch()['session_id']
        shown = self.save(sid, report=True)
        self.assertEqual(shown['activity'], 'finished')
        self.assertTrue(shown['next_step'].startswith('Finished, saved '), shown['next_step'])
        self.assertNotIn('reported_activity', shown)
        self.assertNotIn('saved_label', shown)
        self.assertEqual(session_view.state_section(shown), 'state:ready')
        self.assertEqual(self.overview_row(sid)['activity'], 'finished')
        self.assertIn('Finished, saved', self.dashboard(shown))
        self.assertEqual(self.hub.request_close(sid, wait=60)['lifecycle'], 'closed')
