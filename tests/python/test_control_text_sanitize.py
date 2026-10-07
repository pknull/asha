"""``text.sanitize``: untrusted terminal output made safe to paint (moved from the retired preview, N9)."""
import unittest

from lib.control.text import sanitize


class SanitizeTests(unittest.TestCase):
    def test_osc52_clipboard_write_is_removed(self):
        for terminator in ('\x07', '\x1b\\', ''):
            with self.subTest(terminator=repr(terminator)):
                text = sanitize('a\x1b]52;c;ZXZpbA==' + terminator + 'b')
                self.assertNotIn('\x1b', text)
                self.assertNotIn('52;', text)
                self.assertTrue(text.startswith('a'))

    def test_title_set_is_removed(self):
        for sequence in ('\x1b]0;evil\x07', '\x1b]2;evil\x1b\\', '\x1bkevil\x1b\\', '\x9d2;evil\x9c'):
            with self.subTest(sequence=repr(sequence)):
                self.assertEqual(sanitize('x' + sequence + 'y'), 'xy')

    def test_csi_cursor_moves_and_sgr_are_removed(self):
        for sequence in ('\x1b[2J', '\x1b[10;5H', '\x1b[3A', '\x1b[?1049h', '\x1b[31;1m', '\x9b2J', '\x1b7',
                         '\x1b8', '\x1bc', '\x1b(0', '\x1bP1$qm\x1b\\', '\x1b_apc\x1b\\'):
            with self.subTest(sequence=repr(sequence)):
                self.assertEqual(sanitize('<' + sequence + '>'), '<>')

    def test_c0_c1_and_format_controls_are_removed(self):
        text = sanitize('a\x00b\x07c\x08d\re\x7ff\x85g‮h i\x1b\tk')
        self.assertNotRegex(text, r'[\x00-\x1f\x7f-\x9f‮ ]')
        self.assertEqual(text, 'abcdefghi k')
        # ESC plus one final byte is itself a sequence (ESC j), so the byte goes too.
        self.assertEqual(sanitize('i\x1bjk'), 'ik')

    def test_plain_and_wide_text_is_kept(self):
        self.assertEqual(sanitize('• Ran rg -n 工程 ✓'), '• Ran rg -n 工程 ✓')


if __name__ == '__main__':
    unittest.main()
