"""#102 phase 2: dashboard regions, grouping renders and cell-safe clipping (pure; no curses).

Golden renders live in tests/python/golden/session_dashboard/. After an intended
layout change, regenerate them with ASHA_UPDATE_GOLDEN=1 and review the diff.
"""
import os
import unittest
from pathlib import Path

from lib.control import session_layout, session_view, tui
from lib.control.session_view import ViewModel, merge, with_changes

GOLDEN = Path(__file__).resolve().parent / 'golden' / 'session_dashboard'
NOW = 1_000_000.0
SIZES = ((40, 12), (80, 24), (140, 40))


def row(sid, name, *, project='asha', harness='claude', created, age=5, **changes):
    return dict(dict(session_id=sid, generation=1, activity='working', project_name=project, name=name,
                     harness=harness, transport='terminal', profile='worker', lifecycle='open',
                     process_state='live', reason='Observed', pending_messages=0,
                     created_at=float(created), updated_at=NOW - age), **changes)


def fixture_rows():
    return [
        row('00000000-0001', 'lint-sweep', harness='codex', created=1, age=60, activity='permission-requested',
            reason='Run make lint in /home/pknull/Code/asha'),
        row('00000000-0002', 'docs-room', created=2, age=180, activity='needs-input', transport='structured',
            profile='room', reason='Which branch?'),
        row('00000000-0003', 'issue-102', created=3, age=4, reason='Tool: Bash'),
        row('00000000-0004', 'close-audit', created=4, age=540, activity='idle', memory_saved_at=NOW - 540,
            completion_readiness={'receipt': 'current', 'status': 'ready'}),
        row('00000000-0005', 'wrap-up', created=5, age=60, lifecycle='closing', activity='closing',
            closure={'generation': 1, 'state': 'delivered', 'attempts': 1, 'requested_at': NOW - 60}),
        row('00000000-0006', 'refactor-io', project='servitor', harness='codex', created=6, age=12,
            background_tasks=2),
        row('00000000-0007', 'smoke-3', project='servitor', created=7, age=360, activity='unknown',
            telemetry='hooks-not-reporting'),
        row('00000000-0008', 'old-worker', project='servitor', harness='codex', created=8, age=1320,
            activity='exited', process_state='ended', reason='Exited'),
        row('00000000-0009', '幅広い名前のセッション', project='測試プロジェクト', created=9, age=90000),
    ]


def snapshot(model, **extra):
    return dict({'rows': session_view.display_rows(model), 'summary': '7 current; 1 ended; 1 need input',
                 'errors': [], 'grouping': model.grouping, 'now': NOW,
                 'attention': session_view.attention_counts(model)}, **extra)


def model(grouping='project', rows=None):
    return with_changes(merge(ViewModel(), rows or fixture_rows(), observed_at=1.0, complete=True),
                        grouping=grouping)


def render(data, *, width, height, selected=0, anchor=None, **options):
    return session_layout.plain(session_layout.render(
        data, selected=selected, anchor=anchor, width=width, height=height, **options))


class RegionTests(unittest.TestCase):
    def test_breakpoints(self):
        self.assertEqual(session_layout.layout(12, 40).mode, 'narrow')
        # A 120-column terminal paints 119 cells (the last column stays free).
        self.assertEqual(session_layout.layout(24, 118).mode, 'narrow')
        self.assertEqual(session_layout.layout(24, 119).mode, 'wide')
        wide = session_layout.layout(40, 140)
        self.assertEqual(wide.mode, 'wide')
        self.assertGreaterEqual(wide.side_x, wide.list_width)
        self.assertEqual(wide.side_x + wide.side_width, 140)
        self.assertEqual(session_layout.layout(40, 140, preview=False).mode, 'narrow')
        self.assertEqual(session_layout.layout(24, 80, peek=True).mode, 'peek')
        self.assertEqual(session_layout.layout(7, 140).mode, 'tiny')

    def test_the_wide_list_gains_the_detail_lines(self):
        self.assertGreater(session_layout.layout(24, 140).list_height, session_layout.layout(24, 100).list_height)

    def test_errors_take_a_line_from_the_list(self):
        self.assertEqual(session_layout.layout(24, 80, errors=True).list_height + 1,
                         session_layout.layout(24, 80).list_height)


class BoundsTests(unittest.TestCase):
    def test_every_size_and_grouping_fits_with_one_footer(self):
        for grouping in ('project', 'state'):
            data = snapshot(model(grouping))
            for width, height in SIZES + ((20, 8), (8, 5), (1, 1), (119, 30), (120, 30)):
                for selected in (0, 4, 8):
                    with self.subTest(grouping=grouping, width=width, height=height, selected=selected):
                        rendered = render(data, width=width, height=height, selected=selected)
                        self.assertLessEqual(len(rendered), height)
                        self.assertTrue(all(tui._cell_width(line) <= width for line in rendered), rendered)
                        self.assertTrue(all('\x1b' not in line for line in rendered))
                        if height >= 8:
                            self.assertEqual(len(rendered), height)
                            self.assertIn('q quit', rendered[-1])

    def test_wide_character_project_names_clip_to_cells(self):
        wide = [row('w1', '名' * 40, project='測試' * 30, created=1),
                row('w2', 'é' * 30, project='ｅｍｏｊｉ😀' * 12, created=2)]
        for grouping in ('project', 'state'):
            data = snapshot(model(grouping, wide))
            for width in (40, 61, 80, 99, 120, 140):
                for selected in (0, 1):
                    with self.subTest(grouping=grouping, width=width, selected=selected):
                        rendered = render(data, width=width, height=24, selected=selected)
                        for line in rendered:
                            self.assertLessEqual(tui._cell_width(line), width, line)
                        self.assertTrue(any('…' in line for line in rendered))


class SelectedVisibleTests(unittest.TestCase):
    """Q14-F2: whatever the height, errors or fact sub-lines, the selected row is painted."""

    RECEIPT = dict(completion_readiness={'receipt': 'current', 'status': 'ready'}, activity='idle',
                   memory_saved_at=NOW - 60)

    def cases(self):
        single = [row('s1', 'solo', created=1, **self.RECEIPT)]
        many = [row(f's{i}', f'name{i}', project='p' + str(i % 3), created=i,
                    **(self.RECEIPT if i % 2 else dict(background_tasks=i))) for i in range(1, 9)]
        yield 'single', single
        yield 'many', many

    def test_the_selected_row_is_on_screen(self):
        missing = []
        for label, rows in self.cases():
            for grouping in ('project', 'state'):
                view = model(grouping, rows)
                shown = session_view.display_rows(view)
                for width in (40, 140):
                    for height in range(8, 15):
                        for errors in ([], ['Observation failed: x']):
                            for selected in range(len(shown)):
                                for anchor in (None, 0, 3, 99):
                                    lines = render(snapshot(view, errors=errors), width=width, height=height,
                                                   selected=selected, anchor=anchor)
                                    name = shown[selected]['name']
                                    marked = [line for line in lines if line.startswith('> ') and name in line]
                                    if len(marked) != 1:
                                        missing.append((label, grouping, width, height, bool(errors), selected,
                                                        anchor))
        self.assertEqual(missing, [])

    def test_qa14_case_a_receipt_on_a_40x9_screen(self):
        view = model('project', [row('s1', 'solo', created=1, **self.RECEIPT)])
        for height, errors in ((9, []), (10, ['Observation failed: x'])):
            with self.subTest(height=height, errors=errors):
                lines = render(snapshot(view, errors=errors), width=40, height=height, selected=0)
                self.assertTrue(any(line.startswith('> ') and 'solo' in line for line in lines), lines)
                self.assertEqual(len(lines), height)


class GoldenTests(unittest.TestCase):
    """Golden plain renders at 40x12, 80x24 and 140x40 for each grouping."""

    def test_golden_renders(self):
        update = os.environ.get('ASHA_UPDATE_GOLDEN') == '1'
        for grouping in ('project', 'state'):
            for width, height in SIZES:
                # As the dashboard paints it: fitted to the list height first (§5.6).
                view = session_view.fit(model(grouping), session_layout.layout(height, width).list_height)
                with self.subTest(grouping=grouping, width=width, height=height):
                    text = '\n'.join(render(snapshot(view), width=width, height=height,
                                            selected=session_view.selected_index(view))) + '\n'
                    path = GOLDEN / f'{grouping}-{width}x{height}.txt'
                    if update:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text, encoding='utf-8')
                    self.assertEqual(text, path.read_text(encoding='utf-8'))


class ContentTests(unittest.TestCase):
    def test_project_headings_count_and_rows_carry_glyph_harness_and_age(self):
        rendered = render(snapshot(model()), width=100, height=30)
        text = '\n'.join(rendered)
        heading = next(line for line in rendered if line.startswith('▼ asha'))
        self.assertTrue(heading.rstrip().endswith('5'))
        lint = next(line for line in rendered if 'lint-sweep' in line)
        self.assertIn('◆', lint)
        self.assertIn('codex', lint)
        self.assertTrue(lint.rstrip().endswith('1m'))
        self.assertIn('▼ Ended', text)
        self.assertLess(text.index('▼ servitor'), text.index('▼ Ended'))

    def test_state_grouping_prefixes_the_project(self):
        rendered = render(snapshot(model('state')), width=100, height=30)
        self.assertTrue(any(line.startswith('▼ Needs you') for line in rendered))
        self.assertTrue(any('servitor/refactor-io' in line for line in rendered))
        self.assertIn('by state', rendered[0])

    def test_facts_sub_line_sits_under_its_row(self):
        rendered = render(snapshot(model()), width=100, height=30)
        at = next(i for i, line in enumerate(rendered) if 'close-audit' in line)
        self.assertIn('receipt current', rendered[at + 1])
        at = next(i for i, line in enumerate(rendered) if 'wrap-up' in line)
        self.assertIn('attempt 1', rendered[at + 1])

    def test_selection_marker_and_side_panel_identity(self):
        view = model()
        index = view.order.index('00000000-0006')
        rendered = render(snapshot(view), width=140, height=40, selected=index)
        selected = [line for line in rendered if line.startswith('> ')]
        self.assertEqual(len(selected), 1)
        self.assertIn('refactor-io', selected[0])
        self.assertTrue(any('servitor / refactor-io · codex · worker' in line for line in rendered))
        self.assertTrue(any('00000000-0006' in line for line in rendered))
        self.assertTrue(any('2 background tasks' in line.split('│', 1)[-1] for line in rendered))

    def test_banner_announces_attention_scrolled_off_screen(self):
        rows = [row(f'a{i:02d}', f'job-{i}', project='alpha', created=i) for i in range(30)]
        rows.append(row('zz', 'asks', project='zulu', created=99, activity='needs-input'))
        data = snapshot(model(rows=rows))
        top = render(data, width=80, height=16, selected=0)
        self.assertIn('! to jump', top[1])
        self.assertIn('1 needs input', top[1])
        bottom = render(data, width=80, height=16, selected=30)
        self.assertNotIn('! to jump', bottom[1])

    def test_banner_announces_attention_in_a_folded_section(self):
        view = model(rows=[row('a', 'one', created=1), row('b', 'two', project='zulu', created=2,
                                                            activity='permission-requested')])
        view = session_view.fold(session_view.move(view, 1, visible=10), fold=True)
        rendered = render(snapshot(view), width=80, height=24, selected=session_view.selected_index(view))
        self.assertIn('1 approval', rendered[1])
        heading = next(line for line in rendered if 'zulu' in line)
        self.assertTrue(heading.startswith('> ▸ zulu (1'))

    def test_ascii_fallback_has_no_structural_unicode(self):
        view = model(rows=[r for r in fixture_rows() if r['project_name'] != '測試プロジェクト'])
        rendered = render(snapshot(view, ascii=True), width=140, height=40)
        self.assertTrue(all(line.isascii() for line in rendered), [l for l in rendered if not l.isascii()])
        self.assertTrue(any(line.startswith('- asha') for line in rendered))

    def test_peek_shows_the_detail_full_width(self):
        view = model()
        rendered = render(snapshot(view), width=80, height=24, peek=True, selected=2)
        self.assertTrue(any('asha / issue-102 · claude · worker' in line for line in rendered))
        self.assertTrue(rendered[-1].startswith('Esc back'))

    def test_raw_rows_without_sections_still_render(self):
        rendered = render({'rows': [dict(session_id='x', activity='working', project_name='asha', name='Job',
                                         harness='claude')]}, width=80, height=24)
        self.assertTrue(any('Job' in line for line in rendered))


if __name__ == '__main__':
    unittest.main()
