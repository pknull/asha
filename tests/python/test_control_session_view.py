"""#102 phase 1: a retained, stably ordered session view (pure; no curses)."""
import unittest

from lib.control import session_view
from lib.control.session_view import ViewModel, merge, merge_row, move, order, select_after_merge


def row(sid, *, activity='working', project='asha', created=None, updated=0.0, **changes):
    return dict(dict(session_id=sid, generation=1, activity=activity, project_name=project,
                     name='Job ' + sid, harness='claude', transport='terminal', profile='worker',
                     lifecycle='open', process_state='live', reason='Observed',
                     created_at=float(created if created is not None else ord(sid[-1])),
                     updated_at=updated), **changes)


def ids(model):
    return list(model.order)


class StableOrderTests(unittest.TestCase):
    """T1: activity (a restamped updated_at) never moves a row."""

    def test_updated_at_change_within_an_attention_class_keeps_the_order(self):
        a, b, c = row('a'), row('b'), row('c')
        model = merge(ViewModel(), [a, b, c], observed_at=10.0, complete=True)
        before = ids(model)
        # The hub lists by updated_at DESC, so a busy row arrives first every time.
        busy = dict(b, updated_at=99.0, reason='Tool completed')
        model = merge(model, [busy, c, a], observed_at=11.0, complete=True)
        self.assertEqual(ids(model), before)
        self.assertEqual(model.rows['b']['reason'], 'Tool completed')

    def test_order_ignores_input_order_and_is_deterministic(self):
        rows = [row('c', project='zeta'), row('a', project='asha'), row('b', project='asha', created=1)]
        for permutation in (rows, rows[::-1], [rows[1], rows[2], rows[0]]):
            self.assertEqual([r['session_id'] for r in order(permutation)], ['b', 'a', 'c'])

    def test_order_does_not_mutate_input_rows(self):
        rows = [row('b'), row('a')]
        original = [dict(r) for r in rows]
        order(rows)
        self.assertEqual(rows, original)

    def test_groups_keep_current_before_ended_before_history(self):
        rows = [row('h', lifecycle='closed', activity='closed', process_state='ended'),
                row('e', activity='exited', process_state='ended'), row('c')]
        self.assertEqual([r['session_id'] for r in order(rows)], ['c', 'e', 'h'])

    def test_missing_created_at_and_project_still_sort(self):
        rows = [dict(row('b'), created_at=None, project_name=None), row('a')]
        self.assertEqual(len(order(rows)), 2)


class AttentionTests(unittest.TestCase):
    """T2: a row whose class becomes needs-input moves to the head of its group."""

    def test_needs_input_moves_to_the_head_of_its_group(self):
        rows = [row('a'), row('b'), row('c')]
        model = merge(ViewModel(), rows, observed_at=1.0, complete=True)
        self.assertEqual(ids(model), ['a', 'b', 'c'])
        rows[2] = dict(rows[2], activity='needs-input')
        model = merge(model, rows, observed_at=2.0, complete=True)
        self.assertEqual(ids(model), ['c', 'a', 'b'])

    def test_attention_does_not_leave_its_group(self):
        rows = [row('a'), row('e', activity='exited', process_state='ended'),
                row('f', activity='exited', process_state='ended',
                    closure={'generation': 1, 'needs_attention': True})]
        ordered = [r['session_id'] for r in order(rows)]
        self.assertEqual(ordered, ['a', 'f', 'e'])

    def test_approval_and_close_failure_count_as_attention(self):
        for changes in ({'activity': 'permission-requested'}, {'activity': 'close-failed'},
                        {'activity': 'waiting-input'}):
            with self.subTest(changes=changes):
                rows = [row('a'), row('b', **changes)]
                self.assertEqual([r['session_id'] for r in order(rows)], ['b', 'a'])

    def test_state_grouping_puts_attention_then_working_then_the_rest(self):
        rows = [row('a', activity='idle'), row('b'), row('c', activity='needs-input')]
        self.assertEqual([r['session_id'] for r in order(rows, 'state')], ['c', 'b', 'a'])


class SelectionTests(unittest.TestCase):
    """T3: selection is an identity that survives reorder and removal."""

    def test_selection_follows_the_id_through_a_reorder(self):
        rows = [row('a'), row('b'), row('c')]
        model = merge(ViewModel(), rows, observed_at=1.0, complete=True)
        model = move(model, 1, visible=10)
        self.assertEqual(model.selected_id, 'b')
        rows[2] = dict(rows[2], activity='needs-input')
        model = merge(model, rows, observed_at=2.0, complete=True)
        self.assertEqual(model.selected_id, 'b')
        self.assertEqual(session_view.selected_index(model), 2)

    def test_removed_selection_moves_to_the_following_neighbour(self):
        rows = [row('a'), row('b'), row('c')]
        model = merge(ViewModel(), rows, observed_at=1.0, complete=True)
        model = move(model, 1, visible=10)
        model = merge(model, [rows[0], rows[2]], observed_at=2.0, complete=True)
        self.assertEqual(model.selected_id, 'c')

    def test_removed_last_row_falls_back_to_the_preceding_neighbour(self):
        rows = [row('a'), row('b'), row('c')]
        model = merge(ViewModel(), rows, observed_at=1.0, complete=True)
        model = move(model, 2, visible=10)
        model = merge(model, rows[:2], observed_at=2.0, complete=True)
        self.assertEqual(model.selected_id, 'b')

    def test_multiple_removals_select_the_nearest_survivor_by_distance(self):
        # QA12-F2: a,b,c,d,e with b selected; b,c,d leave. a is one step away, e three.
        rows = [row(s) for s in 'abcde']
        model = move(merge(ViewModel(), rows, observed_at=1.0, complete=True), 1, visible=10)
        model = merge(model, [rows[0], rows[4]], observed_at=2.0, complete=True)
        self.assertEqual(model.selected_id, 'a')
        # Mirror: d selected, b,c,d leave; e is one step away, a three.
        model = move(merge(ViewModel(), rows, observed_at=1.0, complete=True), 3, visible=10)
        model = merge(model, [rows[0], rows[4]], observed_at=2.0, complete=True)
        self.assertEqual(model.selected_id, 'e')

    def test_equal_distance_prefers_the_following_survivor(self):
        self.assertEqual(select_after_merge(tuple('abcde'), ('a', 'e'), 'c'), 'e')
        self.assertEqual(select_after_merge(tuple('abcdefg'), ('a', 'g'), 'c'), 'a')

    def test_select_after_merge_edges(self):
        self.assertIsNone(select_after_merge((), (), None))
        self.assertEqual(select_after_merge(('a',), ('b',), None), 'b')
        self.assertEqual(select_after_merge(('a', 'b'), ('b',), 'gone'), 'b')
        self.assertIsNone(select_after_merge(('a',), (), 'a'))

    def test_scroll_anchor_holds_through_a_reorder_and_clamps_on_move(self):
        rows = [row(f'r{i:02d}', created=i) for i in range(20)]
        model = merge(ViewModel(), rows, observed_at=1.0, complete=True)
        model = move(model, 5, visible=4)
        self.assertEqual(model.anchor, 3)
        model = move(model, -1, visible=4)
        self.assertEqual(model.anchor, 2)
        rows[0] = dict(rows[0], activity='needs-input')
        rows[19] = dict(rows[19], activity='needs-input')
        model = merge(model, rows, observed_at=2.0, complete=True)
        self.assertEqual(model.anchor, 2)
        model = move(model, -100, visible=4)
        self.assertEqual((model.anchor, session_view.selected_index(model)), (0, 0))

    def test_input_filter_keeps_selection_on_a_visible_row(self):
        rows = [row('a'), row('b', activity='needs-input')]
        model = merge(ViewModel(), rows, observed_at=1.0, complete=True)
        model = move(model, 1, visible=10)
        self.assertEqual(model.selected_id, 'a')
        model = session_view.with_changes(model, input_only=True)
        self.assertEqual((ids(model), model.selected_id), (['b'], 'b'))


class StaleTests(unittest.TestCase):
    """T4: an incomplete page keeps unseen rows and marks them stale."""

    def test_incomplete_page_keeps_unseen_rows_as_stale(self):
        rows = [row('a'), row('b'), row('c')]
        model = merge(ViewModel(), rows, observed_at=100.0, complete=True)
        model = merge(model, [rows[0]], observed_at=105.0, complete=False)
        self.assertEqual(ids(model), ['a', 'b', 'c'])
        self.assertEqual(dict(model.stale), {'b': 100.0, 'c': 100.0})
        shown = {r['session_id']: r for r in session_view.display_rows(model)}
        self.assertEqual(shown['b']['stale_since'], 100.0)
        self.assertNotIn('stale_since', shown['a'])
        # Stale time is when the row was last observed, not each later miss.
        model = merge(model, [], observed_at=110.0, complete=False)
        self.assertEqual(dict(model.stale), {'a': 105.0, 'b': 100.0, 'c': 100.0})

    def test_a_complete_page_clears_staleness_and_drops_unlisted_rows(self):
        rows = [row('a'), row('b')]
        model = merge(ViewModel(), rows, observed_at=1.0, complete=True)
        model = merge(model, [], observed_at=2.0, complete=False)
        model = merge(model, [rows[0]], observed_at=3.0, complete=True)
        self.assertEqual((ids(model), dict(model.stale)), (['a'], {}))

    def test_a_newer_single_row_refresh_is_not_overwritten_by_an_older_page(self):
        model = merge(ViewModel(), [row('a')], observed_at=1.0, complete=True)
        model = merge_row(model, row('a', activity='needs-input'), observed_at=5.0)
        self.assertEqual(model.rows['a']['activity'], 'needs-input')
        model = merge(model, [row('a')], observed_at=4.0, complete=True)
        self.assertEqual(model.rows['a']['activity'], 'needs-input')
        model = merge(model, [row('a')], observed_at=6.0, complete=True)
        self.assertEqual(model.rows['a']['activity'], 'working')

    def test_a_row_refreshed_after_a_complete_page_started_is_not_dropped_by_it(self):
        model = merge(ViewModel(), [row('a')], observed_at=1.0, complete=True)
        model = merge_row(model, row('new'), observed_at=5.0)
        model = merge(model, [row('a')], observed_at=4.0, complete=True)
        self.assertEqual(ids(model), ['a', 'new'])
        self.assertEqual(dict(model.stale), {})
        model = merge(model, [row('a')], observed_at=6.0, complete=True)
        self.assertEqual(ids(model), ['a'])

    def test_an_excluded_row_refresh_is_not_reinserted_by_an_older_page(self):
        # QA12-F1: an action proves the row left the active query (say it closed
        # while history is off). A page started before that cannot bring it back.
        model = merge(ViewModel(), [row('a'), row('b')], observed_at=1.0, complete=True)
        closed = row('a', lifecycle='closed', activity='closed', process_state='ended')
        model = merge_row(model, closed, observed_at=5.0, member=False)
        self.assertEqual(ids(model), ['b'])
        late = merge(model, [row('a'), row('b')], observed_at=4.0, complete=True)
        self.assertEqual(ids(late), ['b'])
        for at in (6.0, 7.0):
            model = merge(model, [row('b')], observed_at=at - 3.0, complete=False)
            self.assertEqual((ids(model), dict(model.stale)), (['b'], {}))
        # A page that started after the exclusion is newer evidence again.
        model = merge(model, [row('a'), row('b')], observed_at=6.0, complete=True)
        self.assertEqual(ids(model), ['a', 'b'])

    def test_an_excluded_row_stays_out_through_repeated_partial_pages(self):
        model = merge(ViewModel(), [row('a')], observed_at=1.0, complete=True)
        model = merge_row(model, row('a', lifecycle='closed', activity='closed'), observed_at=5.0, member=False)
        for at in (2.0, 3.0, 4.0):
            model = merge(model, [row('a')], observed_at=at, complete=False)
            self.assertEqual((ids(model), dict(model.stale)), ([], {}))

    def test_a_member_row_refresh_clears_earlier_exclusion_evidence(self):
        model = merge(ViewModel(), [row('a')], observed_at=1.0, complete=True)
        model = merge_row(model, row('a', lifecycle='closed'), observed_at=5.0, member=False)
        model = merge_row(model, row('a'), observed_at=6.0)
        self.assertEqual(ids(model), ['a'])
        self.assertEqual(ids(merge(model, [], observed_at=4.0, complete=False)), ['a'])

    def test_merge_returns_a_new_model_and_leaves_the_old_one_intact(self):
        first = merge(ViewModel(), [row('a')], observed_at=1.0, complete=True)
        second = merge(first, [row('a'), row('b')], observed_at=2.0, complete=True)
        self.assertEqual(ids(first), ['a'])
        self.assertEqual(ids(second), ['a', 'b'])
        with self.assertRaises(TypeError):
            second.rows['c'] = row('c')


if __name__ == '__main__':
    unittest.main()


class ScreenLineTests(unittest.TestCase):
    """QA12-F3: the anchor is the selected row's rendered line, headings included."""

    def rows(self, groups):
        # groups: sequence of (count, kind) where kind is current/ended/history.
        changes = {'current': {}, 'ended': dict(activity='exited', process_state='ended'),
                   'history': dict(lifecycle='closed', activity='closed', process_state='ended')}
        out, n = [], 0
        for count, kind in groups:
            for _ in range(count):
                out.append(row(f'r{n:02d}', created=n, **changes[kind]))
                n += 1
        return out

    def test_line_offset_counts_group_headings(self):
        rows = [session_view.present(r) for r in self.rows([(2, 'current'), (2, 'ended'), (2, 'history')])]
        self.assertEqual([session_view.line_offset(rows, 0, i) for i in range(6)], [0, 1, 3, 4, 6, 7])
        # A list starting inside a group repeats that group's heading.
        self.assertEqual(session_view.line_offset(rows, 3, 3), 1)

    def test_viewport_start_places_the_row_on_its_anchor_line(self):
        rows = [session_view.present(r) for r in self.rows([(8, 'current'), (8, 'ended')])]
        self.assertEqual(session_view.viewport_start(rows, 7, 7, 16), 0)
        rows[7] = session_view.present(dict(rows[7], activity='exited', process_state='ended'))
        rows = session_view.order(rows)
        start = session_view.viewport_start(rows, 7, 7, 16)
        self.assertEqual((start, session_view.line_offset(rows, start, 7)), (1, 7))

    def test_move_tracks_the_anchor_in_lines(self):
        rows = self.rows([(2, 'current'), (4, 'ended')])
        model = merge(ViewModel(), rows, observed_at=1.0, complete=True)
        model = move(model, 3, visible=10)
        # Two current rows, the Ended heading, then r02 and r03.
        self.assertEqual(model.anchor, 4)
        self.assertEqual(move(model, 30, visible=5).anchor, 4)
