"""#102 phase 1: the dashboard footer, key sheet and retained refresh loop."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from lib.control import session_modals, session_tui, session_view, tui


def row(sid='one', **changes):
    return dict(dict(session_id=sid, generation=1, activity='working', project_name='asha',
                     name='Job ' + sid, harness='claude', transport='terminal', profile='worker',
                     lifecycle='open', process_state='live', reason='Observed'), **changes)


class FooterTests(unittest.TestCase):
    """T5: the footer is one line that always fits and names the row's keys."""

    STATES = [None, row(), row(activity='needs-input'), row(activity='needs-input', transport='structured'),
              row(activity='exited', process_state='ended'),
              row(lifecycle='closed', activity='closed', process_state='ended'),
              row(activity='idle', closure={'generation': 1, 'state': 'pending-delivery'}, native_activity='idle')]

    def test_footer_fits_every_width_on_one_line(self):
        for width in (40, 80, 120):
            for state in self.STATES:
                with self.subTest(width=width, state=state and state['activity']):
                    text = session_tui.footer(state, width=width)
                    self.assertNotIn('\n', text)
                    self.assertLessEqual(tui._cell_width(text), width)
                    self.assertTrue(text.endswith('? keys  q quit'))

    def test_rendered_dashboard_ends_with_the_single_footer_line(self):
        data = {'summary': 'x', 'rows': [row(str(i)) for i in range(60)]}
        for width, height in ((40, 12), (80, 24), (120, 40)):
            with self.subTest(width=width, height=height):
                rendered = session_tui.lines(data, width=width, height=height)
                self.assertEqual(len(rendered), height)
                self.assertEqual(rendered[-1], session_tui.footer(
                    session_tui.present(data['rows'][0]), width=width))
                self.assertEqual(sum('q quit' in line for line in rendered), 1)

    def test_footer_follows_the_selected_state(self):
        wide = dict(width=200)
        self.assertIn('n job', session_tui.footer(None, **wide))
        self.assertIn('a answer', session_tui.footer(session_tui.present(
            row(activity='needs-input', transport='structured')), **wide))
        self.assertIn('r resume', session_tui.footer(session_tui.present(
            row(activity='exited', process_state='ended')), **wide))
        self.assertIn('X force-close', session_tui.footer(session_tui.present(
            row(activity='exited', process_state='ended')), **wide))

    def test_no_c_close_key_is_offered(self):
        idle = session_tui.present(row(activity='idle', native_activity='idle',
                                       closure={'generation': 1, 'state': 'pending-delivery'}))
        self.assertNotIn('c close', session_tui.footer(idle, width=200))
        self.assertNotIn('c close', '\n'.join(session_tui.key_sheet()))

    def test_key_sheet_view_lists_every_binding_within_bounds(self):
        data = {'summary': 'x', 'rows': [row()]}
        rendered = session_tui.lines(data, width=60, height=30, keys=True)
        text = '\n'.join(rendered)
        for key in ('Enter', 'x ', 'X ', 's ', 'r ', 'm ', 'n ', 'o ', 'M ', 'A ', 'q '):
            self.assertIn(key, text)
        self.assertTrue(all(tui._cell_width(line) <= 60 for line in rendered))
        self.assertLessEqual(len(session_tui.lines(data, width=30, height=6, keys=True)), 6)


class KeySheetPagingTests(unittest.TestCase):
    """QA12-F4: every binding is reachable on a short terminal."""

    LABELS = ('Up/Down', 'Enter attach', 'a answer', 'm send', 'x close', 'X force-close', 's stop',
              'r resume', 'n job', 'o Room', 'M input filter', 'A history', '? keys', 'q quit')

    def pages(self, data, *, width, height):
        seen, offset = [], 0
        for _ in range(40):
            page = session_tui.lines(data, width=width, height=height, keys=True, sheet=offset)
            seen.append(page)
            following = session_tui.sheet_offset(offset + 1, height=height)
            if following == offset:
                return seen
            offset = following
        self.fail('the key sheet never reached its end')

    def test_every_binding_is_reachable_at_12_rows(self):
        for width in (40, 80):
            with self.subTest(width=width):
                data = {'summary': 'x', 'rows': [row()]}
                pages = self.pages(data, width=width, height=12)
                self.assertGreater(len(pages), 1)
                text = '\n'.join(line for page in pages for line in page)
                for label in self.LABELS:
                    self.assertIn(label, text)
                for page in pages:
                    self.assertLessEqual(len(page), 12)
                    self.assertTrue(all(tui._cell_width(line) <= width for line in page))
                    self.assertIn('Up/Down', page[-1])

    def test_a_sheet_that_fits_is_not_paged(self):
        data = {'summary': 'x', 'rows': [row()]}
        rendered = session_tui.lines(data, width=80, height=40, keys=True)
        self.assertEqual(rendered[0], 'Keys (any key returns)')
        self.assertEqual(session_tui.sheet_offset(5, height=40), 0)


class ScreenAnchorTests(unittest.TestCase):
    """QA12-F3: the selected row keeps its screen line when a group heading moves."""

    ENDED = dict(activity='exited', process_state='ended')
    CLOSED = dict(lifecycle='closed', activity='closed', process_state='ended')

    def selected_y(self, model, height=24):
        rendered = session_tui.lines({'rows': session_view.display_rows(model)},
                                     selected=session_view.selected_index(model),
                                     anchor=model.anchor, width=120, height=height)
        return next(i for i, line in enumerate(rendered) if line.startswith('> '))

    def check(self, rows, index, change, replacement):
        model = session_view.merge(session_view.ViewModel(), rows, observed_at=1.0, complete=True)
        model = session_view.move(model, index, visible=14)
        before = self.selected_y(model)
        rows = list(rows)
        rows[change] = replacement
        merged = session_view.merge(model, rows, observed_at=2.0, complete=True)
        self.assertEqual(merged.selected_id, model.selected_id)
        self.assertEqual(self.selected_y(merged), before)

    def test_a_heading_inserted_above_the_selection(self):
        rows = [row(str(i), created_at=i, **({} if i < 8 else self.ENDED)) for i in range(16)]
        self.check(rows, 7, 7, row('7', created_at=7, **self.ENDED))

    def test_a_heading_removed_above_the_selection(self):
        # Scrolled past twenty current rows; the only ended row closes into
        # history, so the Ended heading above the selection disappears.
        rows = ([row(f'c{i:02d}', created_at=i) for i in range(20)] + [row('e', created_at=20, **self.ENDED)]
                + [row(f'h{i}', created_at=21 + i, **self.CLOSED) for i in range(4)])
        self.check(rows, 23, 20, row('e', created_at=20, **self.CLOSED))


class RowMarkerTests(unittest.TestCase):
    def test_close_facts_are_shown_on_the_session_row(self):
        rows = [row('cur', lifecycle='closing', activity='closing',
                    closure={'generation': 1, 'state': 'closing', 'requested_at': 3600.0, 'deadline': 3660.0}),
                row('none')]
        # Phase 2: the facts sit on the line under their row (list column only).
        rendered = [line.split('│')[0] for line in session_tui.lines({'rows': rows}, width=160, height=30)]
        at = {name: next(i for i, line in enumerate(rendered) if 'Job ' + name in line) for name in ('cur', 'none')}
        self.assertIn('close requested 01:00 UTC', rendered[at['cur'] + 1])
        self.assertNotIn('receipt', '\n'.join(rendered))

    def test_stale_rows_are_labelled(self):
        rendered = session_tui.lines({'rows': [row(stale_since=3661.0)]}, width=100, height=30)
        at = next(i for i, line in enumerate(rendered) if 'Job one' in line)
        self.assertIn('stale since 01:01:01 UTC', rendered[at + 1])


class Screen:
    def __init__(self, keys, height=24, width=100):
        self.keys = list(keys)
        self.size = height, width

    def getmaxyx(self):
        return self.size

    def timeout(self, value):
        pass

    def getch(self):
        return self.keys.pop(0) if self.keys else ord('q')


class FakeCurses:
    KEY_DOWN, KEY_UP, KEY_ENTER = 258, 259, 343
    error = RuntimeError


class LoopTests(unittest.TestCase):
    def run_loop(self, keys, snapshot, *, hub=None):
        pool = MagicMock()
        pool.submit.return_value.done.return_value = True
        pool.submit.return_value.result.return_value = snapshot
        hub = hub or MagicMock()
        painted = []
        with patch.object(session_tui, 'Hub', return_value=hub), \
             patch.object(session_tui, 'ThreadPoolExecutor', return_value=pool), \
             patch.object(session_tui, 'curses', FakeCurses), \
             patch.object(session_modals, 'init_colours', return_value=False), \
             patch.object(session_tui, '_paint', side_effect=lambda s, snap, **kw: painted.append((snap, kw))):
            self.assertEqual(session_tui._loop(Screen(keys), object(), {}), 0)
        return pool, hub, painted

    def snapshot(self, *rows, complete=True):
        return {'rows': [session_tui.present(r) for r in rows], 'complete': complete, 'errors': [], 'summary': 's'}

    def test_navigation_keys_do_not_force_a_refresh(self):
        data = self.snapshot(row('a'), row('b'), row('c'))
        pool, _, painted = self.run_loop([FakeCurses.KEY_DOWN, FakeCurses.KEY_DOWN, FakeCurses.KEY_UP, ord('M'), ord('M')], data)
        self.assertEqual(pool.submit.call_count, 1)
        selected = [snap['rows'][kw['selected']]['session_id'] for snap, kw in painted if snap['rows']]
        self.assertEqual(selected[-1], 'b')

    def test_an_action_refreshes_only_its_row(self):
        data = self.snapshot(row('a'), row('b'))
        hub = MagicMock()
        hub.owns.return_value = True
        hub.show.return_value = row('b', activity='needs-input')
        with patch.object(session_modals, '_prompt_line', return_value='hello'), \
             patch('lib.control.sessions.refuse_managed_operator'):
            pool, hub, painted = self.run_loop([FakeCurses.KEY_DOWN, ord('m')], data, hub=hub)
        self.assertEqual(pool.submit.call_count, 1)
        hub.show.assert_called_once_with('b')
        final = painted[-1][0]
        self.assertEqual([r['session_id'] for r in final['rows']], ['b', 'a'])
        self.assertEqual(final['rows'][painted[-1][1]['selected']]['session_id'], 'b')

    def test_rows_missing_from_an_incomplete_page_stay_and_are_marked(self):
        pages = [self.snapshot(row('a'), row('b')), self.snapshot(row('a'), complete=False)]
        pool = MagicMock()
        future = MagicMock()
        future.done.return_value = True
        future.result.side_effect = pages
        pool.submit.return_value = future
        painted = []
        clock = iter(range(0, 1000, 6))   # each read passes the 5 s slow tick
        with patch.object(session_tui, 'Hub'), \
             patch.object(session_tui, 'ThreadPoolExecutor', return_value=pool), \
             patch.object(session_tui, 'curses', FakeCurses), \
             patch.object(session_tui.time, 'monotonic', side_effect=lambda: next(clock)), \
             patch.object(session_modals, 'init_colours', return_value=False), \
             patch.object(session_tui, '_paint', side_effect=lambda s, snap, **kw: painted.append(snap)):
            session_tui._loop(Screen([-1]), object(), {})
        final = painted[-1]
        self.assertEqual([r['session_id'] for r in final['rows']], ['a', 'b'])
        self.assertIn('stale_since', final['rows'][1])
        self.assertIn('1 stale', final['summary'])

    def test_failed_refresh_keeps_rows_as_stale_rather_than_dropping_them(self):
        pool = MagicMock()
        future = MagicMock()
        future.done.return_value = True
        future.result.side_effect = [self.snapshot(row('a')), OSError('database locked')]
        pool.submit.return_value = future
        painted = []
        clock = iter(range(0, 1000, 6))   # each read passes the 5 s slow tick
        with patch.object(session_tui, 'Hub'), \
             patch.object(session_tui, 'ThreadPoolExecutor', return_value=pool), \
             patch.object(session_tui, 'curses', FakeCurses), \
             patch.object(session_tui.time, 'monotonic', side_effect=lambda: next(clock)), \
             patch.object(session_modals, 'init_colours', return_value=False), \
             patch.object(session_tui, '_paint', side_effect=lambda s, snap, **kw: painted.append((snap, kw))):
            session_tui._loop(Screen([-1]), object(), {})
        snap, kw = painted[-1]
        self.assertEqual([r['session_id'] for r in snap['rows']], ['a'])
        self.assertIn('stale_since', snap['rows'][0])
        self.assertIn('database locked', kw['message'])

    def test_question_mark_toggles_the_key_sheet_without_refreshing(self):
        data = self.snapshot(row('a'))
        pool, _, painted = self.run_loop([ord('?'), ord('j')], data)
        self.assertEqual(pool.submit.call_count, 1)
        self.assertEqual([kw.get('keys', False) for _, kw in painted][-3:], [False, True, False])

    def test_arrow_keys_scroll_a_paged_key_sheet(self):
        data = self.snapshot(row('a'))
        with patch.object(Screen, 'getmaxyx', return_value=(12, 80)):
            _, _, painted = self.run_loop([ord('?'), FakeCurses.KEY_DOWN, FakeCurses.KEY_DOWN,
                                           FakeCurses.KEY_UP, ord('j')], data)
        sheets = [(kw.get('keys'), kw.get('sheet', 0)) for _, kw in painted][-5:]
        self.assertEqual(sheets, [(True, 0), (True, 1), (True, 2), (True, 1), (False, 0)])

    def test_view_order_does_not_follow_the_hub_recency_order(self):
        # Hub.list is updated_at DESC; the view keeps its own stable order.
        data = self.snapshot(row('b', created_at=2.0), row('a', created_at=1.0))
        _, _, painted = self.run_loop([], data)
        self.assertEqual([r['session_id'] for r in painted[-1][0]['rows']], ['a', 'b'])



class ClockScreen(Screen):
    """Keys arrive one per tick; time advances with the tick."""

    def __init__(self, keys, times):
        super().__init__(keys)
        self.tick, self.times = 0, times

    @property
    def now(self):
        return self.times[min(self.tick, len(self.times) - 1)]

    def getch(self):
        value = super().getch()
        self.tick += 1
        return value


class Page:
    def __init__(self, rows, *, complete=True, ready=lambda: True):
        self.value = {'rows': rows, 'complete': complete, 'summary': 's', 'errors': []}
        self.ready = ready

    def done(self):
        return self.ready()

    def result(self):
        return self.value


class ActionMembershipTests(unittest.TestCase):
    """QA12-F1: a row refresh after an action obeys the active query."""

    def setUp(self):
        from tests.python.test_control_session_closure import ClosureFixture as Fixture
        self.fixture = Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def run_loop(self, screen, pages):
        pool = MagicMock()
        pool.submit.side_effect = pages
        painted = []
        with patch.object(session_tui, 'Hub', return_value=self.fixture.hub), \
             patch.object(session_tui, 'ThreadPoolExecutor', return_value=pool), \
             patch.object(session_tui, 'curses', FakeCurses), \
             patch.object(session_tui.time, 'monotonic', side_effect=lambda: screen.now), \
             patch.object(session_tui.time, 'time', side_effect=lambda: 1000 + screen.now), \
             patch.object(session_modals, 'init_colours', return_value=False), \
             patch.object(session_modals, '_prompt_line', return_value='yes'), \
             patch('lib.control.sessions.refuse_managed_operator'), \
             patch.object(session_tui, '_paint', side_effect=lambda s, snap, **kw: painted.append(snap)):
            session_tui._loop(screen, SimpleNamespace(), {})
        return [[(r['session_id'], r['lifecycle']) for r in snap['rows']] for snap in painted]

    def test_force_close_then_a_late_complete_page(self):
        sid = self.fixture.launch()['session_id']
        old = self.fixture.hub.show(sid)
        screen = ClockScreen([-1, ord('X'), -1, ord('q')], [0, 6, 6.1, 12])
        trace = self.run_loop(screen, [Page([old]), Page([old], ready=lambda: screen.tick >= 2), Page([])])
        self.assertEqual(self.fixture.hub.get(sid)['lifecycle'], 'closed')
        self.assertEqual(trace[1], [(sid, 'open')])
        self.assertEqual(trace[2:], [[]] * len(trace[2:]))

    def test_force_close_then_repeated_partial_pages(self):
        sid = self.fixture.launch()['session_id']
        old = self.fixture.hub.show(sid)
        screen = ClockScreen([ord('X'), -1, -1, ord('q')], [0, 6, 12, 18])
        trace = self.run_loop(screen, [Page([old]), Page([], complete=False), Page([], complete=False),
                                       Page([], complete=False)])
        self.assertEqual(trace[0], [(sid, 'open')])
        self.assertEqual(trace[1:], [[]] * len(trace[1:]))

    def test_listed_follows_the_hub_list_rules(self):
        from lib.control.session_hub import listed
        closed = dict(lifecycle='closed', transport='terminal', profile='worker', activity='closed')
        self.assertFalse(listed(closed, include_closed=False))
        self.assertTrue(listed(closed, include_closed=True))
        # D11: an old record's attention flag no longer keeps a closed row listed.
        self.assertFalse(listed(dict(closed, closure={'attention': True}), include_closed=False))
        finished = dict(lifecycle='open', transport='structured', profile='worker', activity='finished')
        self.assertFalse(listed(finished, include_closed=False))
        self.assertTrue(listed(dict(finished, profile='room'), include_closed=False))
        self.assertTrue(listed(finished, include_closed=True))

if __name__ == '__main__':
    unittest.main()
