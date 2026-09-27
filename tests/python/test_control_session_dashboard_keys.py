"""#102 phase 2: controller keys (grouping, jump, folds, preview), resize and the title count."""
import io
import unittest
from unittest.mock import MagicMock, patch

from lib.control import session_actions, session_title, session_tui, session_view, tui
from tests.python.test_control_session_dashboard import row

KEY_RESIZE, KEY_LEFT, KEY_RIGHT = 410, 260, 261


class Screen:
    """Keys, plus optional terminal sizes that take effect as each key is read."""

    def __init__(self, keys, size=(24, 100)):
        self.keys = [key if isinstance(key, tuple) else (key, None) for key in keys]
        self.size = size

    def getmaxyx(self):
        return self.size

    def timeout(self, value):
        pass

    def getch(self):
        if not self.keys:
            return ord('q')
        key, size = self.keys.pop(0)
        if size:
            self.size = size
        return key


class FakeCurses:
    KEY_DOWN, KEY_UP, KEY_ENTER, KEY_RESIZE, KEY_LEFT, KEY_RIGHT = 258, 259, 343, KEY_RESIZE, KEY_LEFT, KEY_RIGHT
    error = RuntimeError


def run(keys, rows, *, size=(24, 100), hub=None, title=None, config=None):
    pool = MagicMock()
    pool.submit.return_value.done.return_value = True
    pool.submit.return_value.result.return_value = {
        'rows': [session_tui.present(r) for r in rows], 'complete': True, 'errors': [], 'summary': 's'}
    painted = []
    writer = title or session_title.TitleWriter(None, enabled=False)
    with patch.object(session_tui, 'Hub', return_value=hub or MagicMock()), \
         patch.object(session_tui, 'ThreadPoolExecutor', return_value=pool), \
         patch.object(session_tui, 'curses', FakeCurses), \
         patch.object(session_tui, '_title_writer', return_value=writer), \
         patch.object(tui, 'init_colours', return_value=False), \
         patch.object(session_tui, '_paint', side_effect=lambda s, snap, **kw: painted.append((snap, kw))):
        screen = Screen(keys, size)
        self_check = session_tui._loop(screen, object() if config is None else config, {})
    assert self_check == 0
    return pool, painted


def selected(painted, index=-1):
    snap, kw = painted[index]
    return snap['rows'][kw['selected']]['session_id'] if snap['rows'] else None


class AutoFoldControllerTests(unittest.TestCase):
    """Q14-F3: the dashboard fits its list to the screen before each paint (design §5.6)."""

    def test_a_short_screen_folds_the_ended_tail_and_a_tall_one_opens_it(self):
        ended = dict(activity='exited', process_state='ended')
        rows = [row('w')] + [row(f'e{i:02}', created_at=float(i), **ended) for i in range(15)]
        _, painted = run([(KEY_RESIZE, (40, 100))], rows, size=(12, 40))
        short = [r['session_id'] for r in painted[0][0]['rows']]
        self.assertEqual(short, ['w', session_view.TOKEN + 'ended'])
        tall = [r['session_id'] for r in painted[-1][0]['rows']]
        self.assertEqual(len(tall), 16)


    def test_q15_f2_an_acted_on_row_that_stops_stays_selected_at_40x9(self):
        # QA15: w is selected, the Ended tail and finished rows are auto-folded;
        # w stops and its single-row refresh puts it in Ended. It stays selected
        # and painted, and the next paint does not fold it away.
        ended, done = dict(activity='exited', process_state='ended'), dict(
            activity='idle', completion_readiness={'status': 'ready'})
        rows = [row('w', created_at=1.0), row('f1', created_at=2.0, **done), row('f2', created_at=3.0, **done),
                row('e1', created_at=4.0, **ended), row('e2', created_at=5.0, **ended)]
        hub = MagicMock()
        hub.show.return_value = session_tui.present(row('w', created_at=1.0, **ended))
        pool = MagicMock()
        pool.submit.return_value.done.return_value = True
        pool.submit.return_value.result.return_value = {
            'rows': [session_tui.present(r) for r in rows], 'complete': True, 'errors': [], 'summary': 's'}
        painted = []
        with patch.object(session_tui, 'Hub', return_value=hub), \
             patch.object(session_tui, 'ThreadPoolExecutor', return_value=pool), \
             patch.object(session_tui, 'curses', FakeCurses), \
             patch.object(session_tui, '_title_writer', return_value=session_title.TitleWriter(None, enabled=False)), \
             patch.object(tui, 'init_colours', return_value=False), \
             patch.object(session_tui, '_paint', side_effect=lambda s, snap, **kw: painted.append((snap, kw))):
            dash = session_tui.Dashboard(Screen([], size=(9, 40)), object(), {})
            dash.poll()
            dash.paint()
            self.assertIn(session_view.TOKEN + 'ended', dash.view.order)
            self.assertEqual(selected(painted), 'w')
            dash.refresh_row('w', 'terminal')
            dash.paint()
            self.assertEqual(dash.view.rows['w']['group'], 'ended')
            self.assertEqual(selected(painted), 'w')
            dash.paint()
            self.assertEqual(selected(painted), 'w')


class ResizeSheetTests(unittest.TestCase):
    """QA13 Q13-F1: KEY_RESIZE while the paged key sheet is open keeps it and clamps its offset."""

    def test_resize_keeps_the_sheet_and_clamps_the_offset(self):
        keys = [ord('?'), FakeCurses.KEY_DOWN, FakeCurses.KEY_DOWN, (KEY_RESIZE, (9, 80)),
                FakeCurses.KEY_DOWN, (KEY_RESIZE, (60, 80)), ord('j')]
        _, painted = run(keys, [row('a')], size=(12, 80))
        sheets = [(kw['keys'], kw['sheet']) for _, kw in painted][-7:]
        clamped = session_tui.sheet_offset(2, height=9)
        self.assertEqual(sheets, [(True, 0), (True, 1), (True, 2), (True, clamped),
                                  (True, session_tui.sheet_offset(clamped + 1, height=9)), (True, 0), (False, 0)])

    def test_resize_outside_the_sheet_changes_nothing(self):
        pool, painted = run([FakeCurses.KEY_DOWN, (KEY_RESIZE, (30, 90))], [row('a'), row('b')])
        self.assertEqual(pool.submit.call_count, 1)
        self.assertEqual((selected(painted), painted[-1][1]['keys']), ('b', False))


class GroupingKeyTests(unittest.TestCase):
    ROWS = [row('a', project_name='zeta'), row('b', project_name='asha'),
            row('c', project_name='zeta', activity='needs-input')]

    def test_g_toggles_grouping_keeping_the_selection_without_a_refresh(self):
        pool, painted = run([FakeCurses.KEY_DOWN, ord('g'), ord('g')], self.ROWS)
        self.assertEqual(pool.submit.call_count, 1)
        groupings = [snap['grouping'] for snap, _ in painted][-3:]
        self.assertEqual(groupings, ['project', 'state', 'project'])
        self.assertEqual({selected(painted, i) for i in (-3, -2, -1)}, {'c'})

    def test_default_grouping_is_by_project(self):
        _, painted = run([], self.ROWS)
        self.assertEqual(painted[-1][0]['grouping'], 'project')
        self.assertEqual([r['session_id'] for r in painted[-1][0]['rows']], ['b', 'c', 'a'])

    def test_bang_jumps_to_the_next_attention_row(self):
        _, painted = run([ord('!')], self.ROWS)
        self.assertEqual(selected(painted), 'c')
        _, painted = run([ord('!'), ord('!')], [row('a')])
        self.assertIn('No session needs you', painted[-1][1]['message'])

    def test_left_folds_and_enter_on_the_heading_unfolds_without_acting(self):
        hub = MagicMock()
        _, painted = run([KEY_LEFT, 10, KEY_LEFT, KEY_RIGHT], self.ROWS, hub=hub)
        shapes = [[r.get('kind', 'row') for r in snap['rows']] for snap, _ in painted][-4:]
        self.assertEqual(shapes[0], ['section', 'row', 'row'])
        self.assertEqual(shapes[1], ['row', 'row', 'row'])
        self.assertEqual(shapes[2], ['section', 'row', 'row'])
        self.assertEqual(shapes[3], ['row', 'row', 'row'])
        hub.attach.assert_not_called()
        hub.show.assert_not_called()

    def test_row_actions_are_inert_on_a_folded_heading(self):
        hub = MagicMock()
        with patch.object(tui, '_prompt_line', return_value='yes'):
            run([KEY_LEFT, ord('x'), ord('m'), ord('s')], self.ROWS, hub=hub)
        hub.close.assert_not_called()
        hub.send.assert_not_called()
        hub.stop.assert_not_called()


class PreviewKeyTests(unittest.TestCase):
    def test_space_peeks_on_a_narrow_terminal_and_esc_returns(self):
        _, painted = run([ord(' '), 27], [row('a')], size=(24, 100))
        self.assertEqual([kw['peek'] for _, kw in painted][-3:], [False, True, False])

    def test_space_toggles_the_side_panel_on_a_wide_terminal(self):
        _, painted = run([ord(' '), ord(' ')], [row('a')], size=(40, 160))
        self.assertEqual([(kw['peek'], kw['preview']) for _, kw in painted][-3:],
                         [(False, True), (False, False), (False, True)])

    def test_the_move_capacity_follows_the_layout_list_height(self):
        rows = [row(f'r{i:02d}', created_at=i) for i in range(40)]
        _, painted = run([FakeCurses.KEY_DOWN] * 30, rows, size=(24, 100))
        box = session_tui.session_layout.layout(24, 99)
        self.assertLessEqual(painted[-1][1]['anchor'], box.list_height - 1)


class TitleTests(unittest.TestCase):
    def test_title_count_follows_attention_and_is_restored(self):
        stream = io.BytesIO()
        writer = session_title.TitleWriter(stream, enabled=True)
        run([-1], [row('a', activity='needs-input'), row('b', activity='permission-requested'), row('c')],
            title=writer)
        written = stream.getvalue()
        self.assertIn(b'\x1b]2;2 awaiting input \xc2\xb7 asha control\x07', written)
        self.assertEqual(written.count(b'awaiting input'), 1)
        self.assertTrue(written.endswith(session_title.POP))

    def test_a_disabled_title_never_writes(self):
        stream = io.BytesIO()
        run([-1], [row('a', activity='needs-input')], title=session_title.TitleWriter(stream, enabled=False))
        self.assertEqual(stream.getvalue(), b'')

    def test_loop_title_policy_is_off_without_a_terminal_type(self):
        writer = session_tui._title_writer({})
        self.assertFalse(writer.enabled)


class LaunchKeyTests(unittest.TestCase):
    def test_n_and_o_open_the_session_launch_form_and_refresh_the_new_row(self):
        for key, profile, label in ((ord('n'), 'worker', 'Assignment'), (ord('o'), 'room', 'Topic')):
            with self.subTest(profile=profile):
                hub = MagicMock()
                hub.launch.return_value = {'session_id': 'new', 'name': 'Fresh'}
                hub.show.return_value = row('new')
                seen = {}

                def form(screen, curses_module, model, config, env, *, session):
                    seen.update(session)
                    return session['launch'](project='/p', harness='codex', prompt='Do it',
                                             model='gpt-5', effort=None)
                with patch.object(tui, '_project_launch_form', side_effect=form):
                    _, painted = run([key], [row('a')], hub=hub)
                self.assertEqual(seen['prompt_label'], label)
                hub.launch.assert_called_once_with(project='/p', prompt='Do it', harness='codex', profile=profile,
                                                   model='gpt-5', effort=None)
                hub.show.assert_called_once_with('new')
                self.assertEqual(painted[-1][1]['message'], 'Started Fresh')
                self.assertIn('new', [r['session_id'] for r in painted[-1][0]['rows']])

    def test_a_cancelled_form_launches_nothing(self):
        hub = MagicMock()
        with patch.object(tui, '_project_launch_form', return_value='session launch cancelled'):
            _, painted = run([ord('n')], [row('a')], hub=hub)
        hub.launch.assert_not_called()
        self.assertEqual(painted[-1][1]['message'], 'session launch cancelled')


class LaunchFormTests(unittest.TestCase):
    """The session variant of the shared project form: optional model and effort fields."""

    class Curses:
        KEY_UP, KEY_DOWN, KEY_ENTER, KEY_BTAB, KEY_BACKSPACE, KEY_RESIZE = 259, 258, 343, 353, 263, 410

    def drive(self, keys, launch):
        frames = []
        payload = {'projects': [{'root': '/proj', 'name': 'Proj', 'directory': 'proj', 'project_id': 'proj-id',
                                 'asha_project': True}]}
        stream = iter(keys)
        with patch('lib.control.orchestration.projects.resolve_roots', return_value=(['/'], 'test')), \
             patch('lib.control.orchestration.projects.list_projects_across', return_value=payload), \
             patch('lib.control.rooms.resolve_project', return_value={'root': '/proj'}), \
             patch('lib.control.rooms.room_harness_available', side_effect=lambda name, env: name in {'claude', 'codex'}), \
             patch.object(tui, '_draw_modal_frame', side_effect=lambda _s, _c, frame: frames.append(frame)), \
             patch.object(tui, '_read_modal_key', side_effect=lambda *_: next(stream)):
            screen = MagicMock()
            screen.getmaxyx.return_value = (18, 80)
            form = dict(title='New project job', prompt_label='Assignment', launch=launch, hint='optional')
            result = tui._project_launch_form(screen, self.Curses(), MagicMock(), object(), {}, session=form)
        return result, frames

    def test_blank_model_and_effort_mean_the_harness_default(self):
        calls = []
        result, frames = self.drive([10, 10, *'Fix it', 10, 10, 10],
                                    lambda **kw: calls.append(kw) or 'Started job')
        self.assertEqual(result, 'Started job')
        self.assertEqual(calls, [dict(project='/proj', harness='claude', prompt='Fix it', model=None, effort=None)])
        text = '\n'.join(line for frame in frames for line in frame.rows)
        for label in ('Project', 'Harness', 'Assignment', 'Model', 'Effort', 'Field 5/5'):
            self.assertIn(label, text)

    def test_model_and_effort_pass_through_and_a_refusal_stays_on_the_form(self):
        attempts = []

        def launch(**kw):
            attempts.append(kw)
            if len(attempts) == 1:
                raise ValueError('effort must be one of low, medium, high')
            return 'Started job'
        keys = [10, 10, *'Fix it', 10, *'opus', 10, *'max', 10, 127, 127, 127, *'high', 10]
        result, _ = self.drive(keys, launch)
        self.assertEqual(result, 'Started job')
        self.assertEqual([(a['model'], a['effort']) for a in attempts], [('opus', 'max'), ('opus', 'high')])

    def test_the_assignment_is_required_and_escape_cancels(self):
        result, frames = self.drive([10, 10, 10, 27], lambda **kw: self.fail('launched'))
        self.assertEqual(result, 'session launch cancelled')
        self.assertTrue(any('Assignment is required' in row for frame in frames for row in frame.rows))


if __name__ == '__main__':
    unittest.main()
