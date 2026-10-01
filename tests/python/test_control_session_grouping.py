"""#102 phase 2: project/state sections, folding and the attention jump (pure; no curses)."""
import unittest
import unittest.mock

from lib.control import session_view
from lib.control.session_presentation import present, row_facts
from lib.control.session_view import ViewModel, merge, move, with_changes

ENDED = dict(activity='exited', process_state='ended')
CLOSED = dict(lifecycle='closed', activity='closed', process_state='ended')


def row(sid, *, project='asha', created=None, **changes):
    return dict(dict(session_id=sid, generation=1, activity='working', project_name=project,
                     name='Job ' + sid, harness='claude', transport='terminal', profile='worker',
                     lifecycle='open', process_state='live', reason='Observed',
                     created_at=float(created if created is not None else ord(sid[-1]))), **changes)


def model_of(rows, **changes):
    return with_changes(merge(ViewModel(), rows, observed_at=1.0, complete=True), **changes)


def replace_selected(model, sid):
    from dataclasses import replace
    return replace(model, selected_id=sid)


def sections(model):
    return [(r['section'], r.get('kind', 'row'), r['session_id']) for r in session_view.display_rows(model)]


class ProjectSectionTests(unittest.TestCase):
    """Default grouping is by project; attention leads inside its project."""

    def test_project_is_the_default_grouping(self):
        self.assertEqual(ViewModel().grouping, 'project')

    def test_rows_are_grouped_by_project_with_attention_first_inside_each(self):
        rows = [row('a', project='zeta'), row('b', project='asha'),
                row('c', project='zeta', activity='needs-input'), row('d', project='asha')]
        model = model_of(rows)
        self.assertEqual(list(model.order), ['b', 'd', 'c', 'a'])
        self.assertEqual([s for s, _, _ in sections(model)],
                         ['project:asha', 'project:asha', 'project:zeta', 'project:zeta'])

    def test_ended_and_history_are_their_own_sections_after_the_projects(self):
        rows = [row('h', **CLOSED), row('e', project='asha', **ENDED), row('c', project='zeta')]
        self.assertEqual([(s, sid) for s, _, sid in sections(model_of(rows))],
                         [('project:zeta', 'c'), ('ended', 'e'), ('history', 'h')])

    def test_section_titles_name_the_project(self):
        shown = session_view.display_rows(model_of([row('a', project='servitor')]))
        self.assertEqual(shown[0]['section_title'], 'servitor')
        shown = session_view.display_rows(model_of([row('a', project=None)]))
        self.assertEqual(shown[0]['section_title'], '(no project)')

    def test_case_equivalent_project_names_form_one_contiguous_section(self):
        # Q14-F4: identity and sort key agree, so Foo and foo are one section, shown once.
        rows = [row('a', project='Foo', created=1), row('b', project='foo', created=2),
                row('c', project='Foo', created=3), row('d', project='bar', created=4),
                row('e', project='FOO', created=5, activity='needs-input')]
        model = model_of(rows)
        self.assertEqual(list(model.order), ['d', 'e', 'a', 'b', 'c'])
        shown = session_view.display_rows(model)
        keys = [r['section'] for r in shown]
        self.assertEqual(len(set(keys[1:])), 1)
        self.assertEqual({r['section_title'] for r in shown[1:]}, {'FOO'})   # one stable title
        from lib.control import session_layout
        lines = session_layout.plain(session_layout.render({'rows': shown}, width=80, height=24))
        headings = [line for line in lines if line.startswith('▼')]
        self.assertEqual([h.split()[1:] for h in headings], [['bar', '1'], ['FOO', '4']])
        # A row whose spelling differs from the section title still names its project.
        self.assertTrue(any('foo/Job b' in line for line in lines), lines)
        self.assertTrue(any('▲ Job e' in line for line in lines), lines)   # spelled as the title: no prefix
        folded = session_view.fold(replace_selected(model, 'a'), fold=True)
        self.assertEqual([r.get('count') for r in session_view.display_rows(folded) if r.get('kind')], [4])


class StateSectionTests(unittest.TestCase):
    """`g` groups by the presented next step, so the words and the group agree."""

    CASES = [
        (dict(activity='needs-input'), 'state:needs'),
        (dict(activity='permission-requested'), 'state:needs'),
        (dict(activity='working'), 'state:working'),
        (dict(activity='queued'), 'state:working'),
        (dict(activity='unknown', telemetry='hooks-not-reporting'), 'state:working'),
        (dict(activity='closing', lifecycle='closing'), 'state:closing'),
        (dict(activity='finished'), 'state:ready'),
        (dict(activity='idle', profile='room'), 'state:idle'),
        (dict(activity='idle'), 'state:idle'),
        (ENDED, 'ended'),
        (CLOSED, 'history'),
    ]

    def test_every_presented_state_maps_to_one_section(self):
        for changes, expected in self.CASES:
            with self.subTest(expected=expected, changes=changes):
                presented = present(row('a', **changes))
                self.assertEqual(session_view.section_of(presented, 'state')[0], expected,
                                 presented['next_step'])

    def test_state_sections_order_needs_working_closing_ready_idle_ended_history(self):
        rows = [row(str(i), created=i, **changes) for i, (changes, _) in enumerate(reversed(self.CASES))]
        model = model_of(rows, grouping='state')
        seen = []
        for key, _, _ in sections(model):
            if key not in seen:
                seen.append(key)
        self.assertEqual(seen, ['state:needs', 'state:working', 'state:closing', 'state:ready', 'state:idle',
                                'ended', 'history'])

    def test_toggling_grouping_keeps_the_selected_identity(self):
        model = model_of([row('a', project='zeta', activity='needs-input'), row('b', project='asha')])
        model = move(model, 1, visible=10)
        self.assertEqual(model.selected_id, 'a')
        self.assertEqual(list(model.order), ['b', 'a'])
        flipped = with_changes(model, grouping='state')
        self.assertEqual((flipped.selected_id, list(flipped.order)), ('a', ['a', 'b']))
        with self.assertRaises(ValueError):
            with_changes(model, grouping='nope')


class FoldTests(unittest.TestCase):
    def rows(self):
        return [row('a'), row('b'), row('c', project='zeta'), row('e', **ENDED)]

    def test_a_folded_section_becomes_one_selectable_heading(self):
        model = move(model_of(self.rows()), 1, visible=10)
        folded = session_view.fold(model, fold=True)
        token = session_view.TOKEN + 'project:asha'
        self.assertEqual(list(folded.order), [token, 'c', 'e'])
        self.assertEqual(folded.selected_id, token)
        heading = session_view.display_rows(folded)[0]
        self.assertEqual((heading['kind'], heading['count'], heading['section_title']), ('section', 2, 'asha'))

    def test_unfolding_restores_the_rows_and_selects_the_first(self):
        folded = session_view.fold(model_of(self.rows()), fold=True)
        opened = session_view.fold(folded, fold=False)
        self.assertEqual(list(opened.order), ['a', 'b', 'c', 'e'])
        self.assertEqual(opened.selected_id, 'a')

    def test_folding_is_retained_across_merges_and_counts_attention(self):
        folded = session_view.fold(model_of(self.rows()), fold=True)
        rows = self.rows()
        rows[1] = dict(rows[1], activity='needs-input')
        merged = merge(folded, rows, observed_at=2.0, complete=True)
        heading = session_view.display_rows(merged)[0]
        self.assertEqual((heading['count'], heading['attention']), (2, 1))

    def test_fold_on_an_empty_view_is_a_no_op(self):
        self.assertEqual(session_view.fold(ViewModel(), fold=True), ViewModel())


DONE = dict(activity='finished')
TOKEN = session_view.TOKEN


class AutoFoldTests(unittest.TestCase):
    """Q14-F3, design §5.6: a short list folds Ended and History first, then Finished rows into `… N more`."""

    def rows(self):
        return [row('w', created=1), row('f1', created=2, **DONE), row('f2', created=3, **DONE),
                row('e1', created=4, **ENDED), row('e2', created=5, **ENDED),
                row('h1', created=6, **CLOSED), row('h2', created=7, **CLOSED)]

    def test_qa14_short_tail_folds_ended_at_40x12(self):
        from lib.control import session_layout
        rows = [row('w', created=0)] + [row(f'e{i:02}', created=i + 1, **ENDED) for i in range(15)]
        space = session_layout.layout(12, 39).list_height
        fitted = session_view.fit(model_of(rows), space)
        self.assertEqual(list(fitted.order), ['w', TOKEN + 'ended'])
        self.assertLessEqual(session_view.list_lines(fitted), space)
        lines = session_layout.plain(session_layout.render(
            {'rows': session_view.display_rows(fitted)}, width=39, height=12))
        self.assertTrue(any('▸ Ended (15)' in line for line in lines), lines)

    def test_history_then_ended_then_finished(self):
        view = model_of(self.rows())
        self.assertEqual(session_view.list_lines(view), 10)
        expected = {10: [], 9: ['history'], 7: ['ended', 'history'],
                    5: ['ended', 'finished:project:asha', 'history']}
        for space, auto in expected.items():
            with self.subTest(space=space):
                fitted = session_view.fit(view, space)
                self.assertEqual(sorted(fitted.auto), auto)
                self.assertLessEqual(session_view.list_lines(fitted), space)
        self.assertEqual(list(session_view.fit(view, 5).order),
                         ['w', TOKEN + 'finished:project:asha', TOKEN + 'ended', TOKEN + 'history'])

    def test_attention_and_working_rows_are_never_folded(self):
        rows = [row(str(i), created=i, **({'activity': 'needs-input'} if i % 2 else {})) for i in range(8)]
        rows += [row('x', created=9, **dict(ENDED, activity='needs-input'))]
        view = model_of(rows)
        fitted = session_view.fit(view, 2)
        self.assertEqual(fitted.auto, frozenset())
        self.assertEqual(fitted.order, view.order)

    def test_the_selected_rows_group_is_not_folded_under_it(self):
        view = replace_selected(model_of(self.rows()), 'e1')
        fitted = session_view.fit(view, 5)
        self.assertNotIn('ended', fitted.auto)
        self.assertEqual(fitted.selected_id, 'e1')
        self.assertIn('e1', fitted.order)

    def test_a_taller_screen_releases_automatic_folds(self):
        view = model_of(self.rows())
        short = session_view.fit(view, 5)
        tall = session_view.fit(short, 40)
        self.assertEqual((tall.auto, tall.order), (frozenset(), view.order))

    def test_a_selected_automatic_heading_that_opens_selects_its_first_row(self):
        short = session_view.fit(model_of(self.rows()), 5)
        on_heading = replace_selected(short, TOKEN + 'ended')
        self.assertEqual(session_view.fit(on_heading, 40).selected_id, 'e1')

    def test_an_explicit_unfold_beats_the_automatic_fold(self):
        short = session_view.fit(model_of(self.rows()), 5)
        opened = session_view.fold(replace_selected(short, TOKEN + 'ended'), fold=False)
        self.assertEqual(opened.selected_id, 'e1')
        refit = session_view.fit(opened, 5)
        self.assertIn('e1', refit.order)
        self.assertNotIn('ended', refit.auto)
        # Folding it again by hand is an explicit fold that a tall screen keeps.
        folded = session_view.fold(replace_selected(refit, 'e1'), fold=True)
        self.assertIn(TOKEN + 'ended', session_view.fit(folded, 40).order)

    def test_the_more_row_unfolds_to_the_finished_rows(self):
        short = session_view.fit(model_of(self.rows()), 5)
        more = TOKEN + 'finished:project:asha'
        shown = {r['session_id']: r for r in session_view.display_rows(short)}
        self.assertEqual((shown[more]['kind'], shown[more]['more'], shown[more]['count'],
                          shown[more]['section']), ('section', True, 2, 'project:asha'))
        opened = session_view.fold(replace_selected(short, more), fold=False)
        self.assertEqual(opened.selected_id, 'f1')
        self.assertEqual(list(opened.order)[:3], ['w', 'f1', 'f2'])

    def test_state_grouping_compacts_ready_to_close(self):
        fitted = session_view.fit(model_of(self.rows(), grouping='state'), 5)
        self.assertIn('finished:state:ready', fitted.auto)
        from lib.control import session_layout
        lines = session_layout.plain(session_layout.render(
            {'rows': session_view.display_rows(fitted), 'grouping': 'state'}, width=60, height=12))
        at = next(i for i, line in enumerate(lines) if 'Ready to close' in line)
        self.assertIn('… 2 more', lines[at + 1])

    def test_fit_returns_the_same_model_when_nothing_changes(self):
        fitted = session_view.fit(model_of(self.rows()), 5)
        self.assertIs(session_view.fit(fitted, 5), fitted)


class QA15AutoFoldTests(unittest.TestCase):
    """QA15: automatic folds never hide the selection, yield to an explicit open, and keep one spelling."""

    def space(self, height=9, width=40):
        from lib.control import session_layout
        return session_layout.layout(height, width - 1).list_height

    def test_q15_f2_a_lifecycle_change_never_hides_the_selected_row(self):
        # The selected working row w finishes, stops or leaves for history while
        # its destination is already folded; both merge paths must keep it selected
        # and on screen, before and after the next fit.
        others = [row('f1', created=2, **DONE), row('f2', created=3, **DONE),
                  row('z', project='zeta', created=4), row('e1', created=5, **ENDED),
                  row('e2', created=6, **ENDED), row('h1', created=7, **CLOSED)]
        transitions = {'finished': DONE, 'ended': ENDED, 'history': CLOSED}
        for grouping in ('project', 'state'):
            for name, change in transitions.items():
                for path in ('page', 'row'):
                    with self.subTest(grouping=grouping, transition=name, path=path):
                        rows = [row('w', created=1)] + others
                        view = replace_selected(model_of(rows, grouping=grouping), 'w')
                        short = session_view.fit(view, self.space())
                        after = row('w', created=1, **change)
                        unit = session_view._unit(present(after), grouping, short.auto)
                        self.assertIsNotNone(unit, 'precondition: w moves into an automatic fold')
                        if path == 'page':
                            moved = merge(short, [after] + others, observed_at=2.0, complete=True)
                        else:
                            moved = session_view.merge_row(short, after, observed_at=2.0)
                        for model in (moved, session_view.fit(moved, self.space())):
                            self.assertEqual(model.selected_id, 'w')
                            self.assertIn('w', model.order)
                        # Explicit folds still hide it: only the automatic ones yield.
                        self.assertNotIn(unit, session_view.fit(moved, self.space()).auto)

    def test_q15_f2_an_explicit_fold_still_hides_the_selected_row(self):
        rows = [row('w', created=1), row('f1', created=2, **DONE), row('e1', created=5, **ENDED)]
        view = replace_selected(model_of(rows), 'e1')
        folded = session_view.fold(view, fold=True)
        moved = session_view.merge_row(replace_selected(folded, 'w'), row('w', created=1, **ENDED),
                                       observed_at=2.0)
        self.assertNotIn('w', moved.order)
        self.assertIn(TOKEN + 'ended', moved.order)

    def test_q15_f3_opening_a_project_suppresses_its_finished_fold(self):
        rows = [row('w', created=1), row('f1', created=2, **DONE), row('f2', created=3, **DONE)]
        for height in (8, 9):
            with self.subTest(height=height):
                short = session_view.fit(replace_selected(model_of(rows), 'w'), self.space(height))
                self.assertEqual(list(short.order), ['w', TOKEN + 'finished:project:asha'])
                closed = session_view.fold(short, fold=True)
                opened = session_view.fold(closed, fold=False)
                self.assertIn('project:asha', opened.unfolded)
                refit = session_view.fit(opened, self.space(height))
                self.assertEqual(list(refit.order), ['w', 'f1', 'f2'])
                # Refresh, grow and shrink again: the explicit open still wins.
                again = merge(refit, rows, observed_at=2.0, complete=True)
                for space in (self.space(height), 40, self.space(height)):
                    again = session_view.fit(again, space)
                    self.assertEqual(list(again.order), ['w', 'f1', 'f2'])
                # Folding it by hand again drops the preference; a short screen then compacts once more.
                refolded = session_view.fold(replace_selected(again, 'w'), fold=True)
                reopened = session_view.fold(refolded, fold=True)
                self.assertNotIn('project:asha', reopened.unfolded)

    def test_q15_f4_one_spelling_per_project_whatever_is_compacted(self):
        from lib.control import session_layout
        rows = [row('w', project='Foo', created=1), row('f1', project='foo', created=2, **DONE),
                row('f2', project='foo', created=3, **DONE)]
        space = self.space()
        short = session_view.fit(replace_selected(model_of(rows), 'w'), space)
        shown = session_view.display_rows(short)
        self.assertEqual({r['section_title'] for r in shown}, {'Foo'})
        # Moving onto `… 2 more` scrolls the section heading off: the renderer
        # repeats it from the compacted row, which must spell it the same way.
        down = move(short, 1, visible=space)
        titles = set()
        for model in (short, down):
            rendered = session_layout.render({'rows': session_view.display_rows(model)},
                                             selected=session_view.selected_index(model), anchor=model.anchor,
                                             width=39, height=9)
            titles |= {line.split()[1] for line in session_layout.plain(rendered) if line.startswith('▼')}
        self.assertEqual(titles, {'Foo'})
        # A folded whole section, and the compacted row alone, spell it the same too.
        folded = session_view.fold(short, fold=True)
        self.assertEqual({r['section_title'] for r in session_view.display_rows(folded)}, {'Foo'})


class AttentionJumpTests(unittest.TestCase):
    def test_jump_cycles_attention_rows_in_display_order(self):
        rows = [row('a'), row('b', activity='needs-input'), row('c', project='zeta'),
                row('d', project='zeta', activity='permission-requested')]
        model = model_of(rows)
        self.assertEqual(model.selected_id, 'b')
        model = session_view.jump_attention(model)
        self.assertEqual(model.selected_id, 'd')
        model = session_view.jump_attention(model)
        self.assertEqual(model.selected_id, 'b')

    def test_jump_unfolds_the_section_holding_the_attention_row(self):
        rows = [row('a'), row('c', project='zeta', activity='needs-input')]
        model = session_view.fold(move(model_of(rows), 1, visible=10), fold=True)
        self.assertIn(session_view.TOKEN + 'project:zeta', model.order)
        model = session_view.jump_attention(model)
        self.assertEqual(model.selected_id, 'c')
        self.assertIn('c', model.order)

    def test_jump_without_attention_rows_changes_nothing(self):
        model = model_of([row('a')])
        self.assertIs(session_view.jump_attention(model), model)

    def test_attention_counts_split_input_and_approval(self):
        rows = [row('a', activity='needs-input'), row('b', activity='permission-requested'),
                row('c', activity='waiting-input', transport='structured'), row('d')]
        self.assertEqual(session_view.attention_counts(model_of(rows)), {'input': 2, 'approval': 1})


class RowFactTests(unittest.TestCase):
    """Sub-line facts come from presented fields only (close, background, staleness)."""

    def test_facts(self):
        self.assertEqual(row_facts(present(row('a'))), [])
        closing = present(row('a', lifecycle='closing', activity='closing', closure={'generation': 1, 'state': 'closing', 'requested_at': 3600.0, 'deadline': 3660.0}))
        self.assertEqual(row_facts(closing), ['close requested 01:00 UTC', 'closes by 01:01 UTC'])
        done = present(row('a', closure={'generation': 1, 'state': 'closed', 'requested_at': 0.0, 'deadline': 60.0}))
        self.assertEqual(row_facts(done), [])
        old = present(row('a', generation=2, closure={'generation': 1, 'state': 'closing', 'deadline': 1.0}))
        self.assertEqual(row_facts(old), [])
        busy = present(row('a', background_tasks=2))
        self.assertEqual(row_facts(busy), ['2 background tasks'])
        self.assertEqual(row_facts(dict(present(row('a')), stale_since=3661.0)), ['stale since 01:01:01 UTC'])


class LineMathTests(unittest.TestCase):
    """Every section has a heading line and a fact sub-line is a line too."""

    def test_line_offset_counts_headings_and_sub_lines(self):
        rows = session_view.display_rows(model_of([
            row('a'), row('b', lifecycle='closing', activity='closing', closure={'generation': 1, 'state': 'closing', 'requested_at': 3600.0, 'deadline': 3660.0}),
            row('c'), row('d', project='zeta')]))
        # asha heading, a, b + fact, c, zeta heading, d
        self.assertEqual([session_view.line_offset(rows, 0, i) for i in range(4)], [1, 2, 4, 6])
        self.assertEqual(session_view.row_height(rows[1]), 2)

    def test_the_selected_row_and_its_sub_line_stay_inside_the_space(self):
        rows = session_view.display_rows(model_of(
            [row(f'r{i:02d}', created=i, lifecycle='closing', activity='closing', closure={'generation': 1, 'state': 'closing', 'requested_at': 3600.0, 'deadline': 3660.0})
             for i in range(12)]))
        for anchor in range(0, 30):
            start = session_view.viewport_start(rows, 9, anchor, 6)
            self.assertLessEqual(session_view.line_offset(rows, start, 9) + 2, 6)


class FoldScalingTests(unittest.TestCase):
    """#106 (Q16-F1/Q17-F7): automatic folding skips folds that save no line and never rebuilds per candidate."""

    @staticmethod
    def singletons(count):
        # One working row, then one finished row in each of ``count`` other projects.
        return [row('w', project='a-working', created=0)] + [
            row(f's{i:04}', project=f'p{i:04}', created=i + 1, **DONE) for i in range(count)]

    def test_a_fold_that_saves_no_line_is_not_applied(self):
        view = replace_selected(model_of(self.singletons(25)), 'w')
        fitted = session_view.fit(view, 17)
        # Each singleton's `… 1 more` line costs what its row did, so nothing folds.
        self.assertEqual(fitted.auto, frozenset())
        self.assertIs(fitted, view)

    def test_fit_rebuilds_a_bounded_number_of_times(self):
        view = replace_selected(model_of(self.singletons(200)), 'w')
        calls = []
        real = session_view._rebuild
        with unittest.mock.patch.object(session_view, '_rebuild',
                                        side_effect=lambda *a, **k: calls.append(1) or real(*a, **k)):
            session_view.fit(view, 17)
        self.assertLessEqual(len(calls), 2)

    def test_fit_stays_fast_with_many_singleton_projects(self):
        import time
        view = replace_selected(model_of(self.singletons(400)), 'w')
        start = time.perf_counter()
        session_view.fit(view, 17)
        # QA measured 2.4 s at 201 rows and a 15 s cutoff at 401; this bound is generous.
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_painting_many_folded_units_stays_linear(self):
        import time
        rows = [row('w', project='a-working', created=0)]
        for i in range(1000):
            rows += [row(f'x{i:04}', project=f'p{i:04}', created=2 * i + 1, **DONE),
                     row(f'y{i:04}', project=f'p{i:04}', created=2 * i + 2, **DONE)]
        fitted = session_view.fit(replace_selected(model_of(rows), 'w'), 17)
        self.assertEqual(len(fitted.auto), 1000)
        start = time.perf_counter()
        shown = session_view.display_rows(fitted)
        # Each heading once counted its members by scanning the whole list (O(rows x folds)).
        self.assertLess(time.perf_counter() - start, 0.25)
        self.assertEqual({(r['count'], r['more']) for r in shown if r.get('kind') == 'section'}, {(2, True)})

    def test_folds_that_save_lines_still_apply_in_policy_order(self):
        # Two finished rows per project: each `… 2 more` saves one line.
        rows = [row('w', project='a-working', created=0)]
        for i in range(30):
            rows += [row(f'x{i:03}', project=f'p{i:03}', created=2 * i + 1, **DONE),
                     row(f'y{i:03}', project=f'p{i:03}', created=2 * i + 2, **DONE)]
        view = replace_selected(model_of(rows), 'w')
        fitted = session_view.fit(view, 80)
        self.assertLessEqual(session_view.list_lines(fitted), 80)
        # Bottom-up: the last projects fold first, and only as many as needed.
        self.assertEqual(len(fitted.auto), session_view.list_lines(view) - 80)
        self.assertIn('finished:project:p029', fitted.auto)
        self.assertNotIn('finished:project:p000', fitted.auto)

    def test_matches_the_greedy_policy_line_for_line(self):
        """Oracle: the §5.6 greedy fold over real line counts, skipping folds that save nothing."""
        import random
        from dataclasses import replace

        def reference(model, space):
            auto, trial = [], replace(model, auto=frozenset())
            lines = lambda keys: session_view.list_lines(session_view._rebuild(trial, auto=frozenset(keys)))
            for key in session_view._candidates(model):
                if lines(auto) <= space:
                    break
                if lines(auto + [key]) < lines(auto):
                    auto.append(key)
            return frozenset(auto)

        kinds = [{}, DONE, DONE, ENDED, CLOSED, {'activity': 'needs-input'}, {'activity': 'idle'},
                 dict(DONE, background_tasks=2), dict(ENDED, background_tasks=1)]
        rng = random.Random(106)
        for case in range(300):
            rows = [row(f'r{i:03}', project=rng.choice('ABCDEFG'), created=i, **rng.choice(kinds))
                    for i in range(rng.randint(1, 30))]
            view = model_of(rows, grouping=rng.choice(('project', 'state')), input_only=rng.random() < 0.1)
            if view.order and rng.random() < 0.7:
                view = replace_selected(view, rng.choice(view.order))
            if view.order and rng.random() < 0.3:
                view = session_view.fold(view, fold=rng.random() < 0.5)
            for space in (1, 3, 8, 15, 40):
                with self.subTest(case=case, space=space):
                    fitted = session_view.fit(view, space)
                    self.assertEqual(fitted.auto, reference(view, space))


if __name__ == '__main__':
    unittest.main()
