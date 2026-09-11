"""Session dashboard colours reuse the shared palette without hiding state text."""
import unittest
from unittest.mock import MagicMock, patch

from lib.control import session_tui, tui
from lib.control.tui_style import BAD, GOOD, INERT, MACHINE, TIER_PAIR, WAITING


class FakeCurses:
    error = RuntimeError
    A_BOLD = 1 << 21
    A_REVERSE = 1 << 22
    COLORS = 256

    def __init__(self):
        self.pairs = {}

    def has_colors(self):
        return True

    def start_color(self):
        pass

    def use_default_colors(self):
        pass

    def init_pair(self, index, foreground, background):
        self.pairs[index] = (foreground, background)

    @staticmethod
    def color_pair(index):
        return index << 8


class Screen:
    def __init__(self, height=24, width=100):
        self.size = height, width
        self.writes = []

    def getmaxyx(self):
        return self.size

    def erase(self):
        self.writes.clear()

    def refresh(self):
        pass

    def addnstr(self, y, x, text, limit, attr=0):
        self.writes.append((y, x, text[:limit], attr))


def snapshot(*states):
    return dict(summary='Observed sessions', rows=[dict(
        session_id=f'id-{i}', activity=state, project_name='asha', name=f'Job {i}',
        harness='claude', reason='Tool completed', pending_messages=0,
    ) for i, state in enumerate(states)])


class SessionColourTests(unittest.TestCase):
    def test_eight_colour_terminal_uses_supported_shared_pairs(self):
        module = FakeCurses()
        module.COLORS = 8
        self.assertTrue(tui.init_colours(module))
        self.assertTrue(all(0 <= fg < 8 for fg, _ in module.pairs.values()))

    def test_palette_initialization_failure_falls_back_to_monochrome(self):
        module = FakeCurses()
        module.init_pair = MagicMock(side_effect=module.error('unsupported'))
        self.assertFalse(tui.init_colours(module))
        self.paint(snapshot('working', 'needs-input'), coloured=False)

    def test_loop_initializes_palette_and_passes_result_to_painter(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                screen = MagicMock()
                screen.getch.return_value = ord('q')
                pool = MagicMock()
                pool.submit.return_value.done.return_value = True
                pool.submit.return_value.result.return_value = snapshot('working')
                with patch.object(session_tui, 'Hub'), \
                     patch.object(session_tui, 'ThreadPoolExecutor', return_value=pool), \
                     patch.object(tui, 'init_colours', return_value=enabled) as init, \
                     patch.object(session_tui, '_paint') as paint:
                    self.assertEqual(session_tui._loop(screen, object(), {}), 0)
                init.assert_called_once_with(session_tui.curses)
                self.assertEqual(paint.call_args.kwargs['coloured'], enabled)
                pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def paint(self, data, *, coloured=True, selected=0, height=24, width=100):
        screen = Screen(height, width)
        with patch.object(session_tui, 'curses', FakeCurses):
            session_tui._paint(screen, data, selected=selected, coloured=coloured)
        return screen

    def test_status_palette_covers_native_and_structured_states(self):
        expected = {'working': MACHINE, 'running': MACHINE, 'queued': MACHINE,
                    'needs-input': WAITING, 'waiting-input': WAITING,
                    'finished': GOOD, 'failed': BAD, 'blocked': BAD,
                    'uncertain': BAD, 'budget-exhausted': BAD,
                    'idle': INERT, 'closed': INERT, 'unknown': INERT,
                    'future-state': INERT}
        for state, tier in expected.items():
            with self.subTest(state=state):
                screen = self.paint(snapshot(state))
                status = [w for w in screen.writes if w[0] == 3 and w[1] == 2]
                self.assertEqual(len(status), 1)
                self.assertEqual(status[0][2].strip(), state)
                self.assertEqual(status[0][3], tui._attribute(FakeCurses, tier, True))

    def test_selection_background_does_not_reverse_status_colour(self):
        screen = self.paint(snapshot('working', 'needs-input'), selected=1)
        self.assertTrue(any(y == 4 and x == 0 and attr & FakeCurses.A_REVERSE
                            for y, x, _, attr in screen.writes))
        self.assertFalse(any(y == 3 and attr & FakeCurses.A_REVERSE
                             for y, _, _, attr in screen.writes))
        status = next(w for w in screen.writes if w[:2] == (4, 2))
        self.assertFalse(status[3] & FakeCurses.A_REVERSE)
        self.assertTrue(status[3] & FakeCurses.A_BOLD)

    def test_title_is_bold_metadata_muted_and_errors_red(self):
        data = snapshot('working')
        data['errors'] = ['Observation incomplete']
        screen = self.paint(data)
        self.assertTrue(screen.writes[0][3] & FakeCurses.A_BOLD)
        metadata = next(w for w in screen.writes if 'queued messages' in w[2])
        self.assertEqual(metadata[3], tui._attribute(FakeCurses, INERT, True))
        error = next(w for w in screen.writes if 'Observation incomplete' in w[2])
        self.assertEqual(error[3], tui._attribute(FakeCurses, BAD, True))

    def test_monochrome_retains_words_selection_and_urgent_bold(self):
        data = snapshot('needs-input', 'failed', 'finished')
        screen = self.paint(data, coloured=False)
        allowed = FakeCurses.A_BOLD | FakeCurses.A_REVERSE
        self.assertTrue(all(not (attr & ~allowed) for _, _, _, attr in screen.writes))
        self.assertTrue(any(attr & FakeCurses.A_REVERSE for _, _, _, attr in screen.writes))
        plain = [text for _, x, text, _ in screen.writes if x == 0]
        self.assertEqual(plain, session_tui.lines(data, width=99, height=24))

    def test_tiny_resized_and_unicode_views_keep_safe_text_and_bounds(self):
        data = snapshot(*(['working', 'needs-input'] * 40))
        data['rows'][50]['name'] = '測試 e\u0301 unsafe\x1b[2Jname'
        for width, height in ((1, 1), (8, 5), (20, 8), (40, 20), (100, 24)):
            with self.subTest(width=width, height=height):
                screen = self.paint(data, selected=50, width=width, height=height)
                for y, x, text, _ in screen.writes:
                    self.assertLess(y, height)
                    self.assertLessEqual(x + tui._cell_width(text), max(0, width - 1))
                    self.assertNotIn('\x1b', text)
                if height >= 8 and width >= 40:
                    row = next(w for w in screen.writes if w[1] == 0 and w[2].startswith('> '))
                    self.assertTrue(row[3] & FakeCurses.A_REVERSE)

    def test_empty_dashboard_renders_without_a_selected_row(self):
        screen = self.paint(snapshot())
        self.assertFalse(any(attr & FakeCurses.A_REVERSE for _, _, _, attr in screen.writes))

    def test_summary_emphasizes_outstanding_input(self):
        screen = self.paint(snapshot('working', 'needs-input'))
        summary = next(w for w in screen.writes if w[0] == 1)
        self.assertEqual(summary[3], tui._attribute(FakeCurses, WAITING, True))

    def test_curses_resize_error_does_not_abort_the_painter(self):
        screen = Screen()
        screen.addnstr = MagicMock(side_effect=FakeCurses.error('resized'))
        with patch.object(session_tui, 'curses', FakeCurses):
            session_tui._paint(screen, snapshot('working'), coloured=True)


if __name__ == '__main__':
    unittest.main()
