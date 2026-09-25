"""Issue #96: the idle input line must be proven empty before Control types into it."""
import unittest

from lib.control.pane_input import composer_holds, flatten, input_line_state

RULE = "─" * 40
CLAUDE_IDLE = ["● Done.", "", RULE, "❯ ", RULE, "  v 2.1.283 | Opus", "  -- INSERT -- auto mode on"]

# Native Codex 0.157 captures (capture-pane -p -e, 110 columns, isolated tmux).
_STATUS = "  \x1b[38;5;223mGPT-6-Astra xhigh\x1b[39m · \x1b[38;5;151m~/Code/asha\x1b[39m"
_WARNING = " " * 85 + "⚠ \x1b[38;5;179m1 warning\x1b[39m · \x1b[1mf2\x1b[0m to view"
_HINT = ("  \x1b[1m←\x1b[0m for agents · \x1b[1m?\x1b[0m for shortcuts" + " " * 53
         + "⚠ \x1b[38;5;179m1 warning\x1b[39m · \x1b[1mf2\x1b[0m to view")
CODEX_NATIVE_IDLE = ["", "\x1b[1m›\x1b[0m \x1b[2mAsk Codex to do anything\x1b[0m", "", _STATUS, _HINT]
CODEX_NATIVE_TYPED = ["", "\x1b[1m›\x1b[0m first draft line", "", _STATUS, _WARNING]
CODEX_NATIVE_MULTILINE = ["", "\x1b[1m›\x1b[0m first draft line", "  second line", "", _STATUS, _WARNING]
CODEX_NATIVE_BLANK_FIRST = ["", "\x1b[1m›\x1b[0m", "  DRAFT AFTER BLANK", "", _STATUS, _WARNING]
CODEX_NATIVE_PASTED = ["", "\x1b[1m›\x1b[0m \x1b[38;5;6m[Pasted Content 1631 chars]\x1b[39m", "", _STATUS, _WARNING]


class PaneInputTests(unittest.TestCase):
    def test_claude_empty_prompt_between_rules_is_empty(self):
        self.assertEqual(input_line_state("claude", CLAUDE_IDLE)[0], "empty")

    def test_claude_typed_text_or_placeholder_is_occupied(self):
        for text in ("❯ half a thought", '❯ Try "fix lint errors"'):
            with self.subTest(text=text):
                lines = [RULE, text, RULE]
                self.assertEqual(input_line_state("claude", lines)[0], "occupied")

    def test_claude_multi_line_input_is_occupied(self):
        lines = [RULE, "❯ first line", "  second line", RULE]
        self.assertEqual(input_line_state("claude", lines)[0], "occupied")

    def test_claude_dialog_or_missing_prompt_is_unknown(self):
        for lines in ([], ["Do you want to proceed?", "❯ 1. Yes", "  2. No"], ["plain output"]):
            with self.subTest(lines=lines):
                self.assertNotEqual(input_line_state("claude", lines)[0], "empty")

    def test_claude_vim_normal_mode_is_never_typed_into(self):
        lines = [RULE, "❯ ", RULE, "  -- NORMAL --"]
        self.assertEqual(input_line_state("claude", lines)[0], "unknown")

    def test_codex_empty_or_dim_placeholder_is_empty(self):
        self.assertEqual(input_line_state("codex", ["", "› ", "", "  ? for shortcuts"])[0], "empty")
        dim = "\x1b[1m›\x1b[0m \x1b[2mImplement {feature}\x1b[0m"
        self.assertEqual(input_line_state("codex", ["", dim, "", "  ? for shortcuts"])[0], "empty")

    def test_codex_typed_text_is_occupied(self):
        typed = "\x1b[1m›\x1b[0m half typed"
        self.assertEqual(input_line_state("codex", ["", typed, ""])[0], "occupied")

    def test_codex_native_captures(self):
        self.assertEqual(input_line_state("codex", CODEX_NATIVE_IDLE)[0], "empty")
        for screen in (CODEX_NATIVE_TYPED, CODEX_NATIVE_MULTILINE, CODEX_NATIVE_BLANK_FIRST, CODEX_NATIVE_PASTED):
            with self.subTest(screen=screen[1]):
                self.assertEqual(input_line_state("codex", screen)[0], "occupied")

    def test_codex_continuation_lines_are_never_ignored(self):
        # QA #96: a blank first line followed by an unsent draft.
        for screen in (["› ", "  DO NOT SUBMIT THIS DRAFT", "", "  ? for shortcuts"],
                       ["› ", "", "  DRAFT", "", "  ? for shortcuts"],
                       ["› ", "", "  ? for shortcuts", "", "  status"]):
            with self.subTest(screen=screen):
                self.assertNotEqual(input_line_state("codex", screen)[0], "empty")

    def test_codex_extended_colour_parameters_are_not_dim(self):
        for sgr in ("38;2;255;200;200", "38;5;2", "48;2;2;2;2", "38:2::255:2:2"):
            with self.subTest(sgr=sgr):
                line = f"› \x1b[{sgr}mDO NOT SUBMIT THIS DRAFT\x1b[0m"
                self.assertNotEqual(input_line_state("codex", [line, "", "  ? for shortcuts"])[0], "empty")

    def test_codex_dim_text_without_the_empty_footer_is_not_a_placeholder(self):
        dim = "› \x1b[2mdim draft\x1b[0m"
        self.assertNotEqual(input_line_state("codex", [dim, "", _STATUS, _WARNING])[0], "empty")

    def test_composer_holds_exactly_the_typed_text(self):
        text = flatten("Asha Control close request abc " + "word " * 3)
        claude = [RULE, "❯ " + text[:20], "  " + text[20:], RULE, "  footer"]
        self.assertTrue(composer_holds("claude", claude, text))
        # A soft wrap at a space drops that space from the display.
        soft = [RULE, "❯ Asha Control close", "  request abc word word word", RULE]
        self.assertTrue(composer_holds("claude", soft, text))
        self.assertFalse(composer_holds("claude", [RULE, "❯ my draft " + text, RULE], text))
        self.assertFalse(composer_holds("claude", [RULE, "❯ " + text, "  and more", RULE], text))
        codex = ["\x1b[1m›\x1b[0m " + text, "", _STATUS]
        self.assertTrue(composer_holds("codex", codex, text))
        self.assertTrue(composer_holds("codex", ["\x1b[1m›\x1b[0m " + text, "", _STATUS, _WARNING], text))
        self.assertFalse(composer_holds("codex", ["› draft " + text, "", _STATUS], text))
        self.assertFalse(composer_holds("codex", ["› " + text, "  draft", "", _STATUS], text))

    def test_composer_holds_refuses_text_that_is_not_already_one_flat_line(self):
        self.assertFalse(composer_holds("claude", [RULE, "❯ two  spaces", RULE], "two  spaces"))

    def test_composer_holds_native_codex_paste_placeholder_only_for_its_length(self):
        self.assertTrue(composer_holds("codex", CODEX_NATIVE_PASTED, "x" * 1631))
        self.assertFalse(composer_holds("codex", CODEX_NATIVE_PASTED, "x" * 1630))
        drafted = ["\x1b[1m›\x1b[0m my draft \x1b[38;5;6m[Pasted Content 1631 chars]\x1b[39m", "", _STATUS]
        self.assertFalse(composer_holds("codex", drafted, "x" * 1631))

    # QA2 #96 finding 3: the post-paste check must validate the whole composer.
    def test_codex_draft_after_a_blank_line_inside_the_composer_is_refused(self):
        text = "Asha Control close request r1 for session s1"
        screens = (
            ["› " + text, "", "  DO NOT SUBMIT DRAFT", "", "  GPT-6-Astra xhigh"],
            ["\x1b[1m›\x1b[0m " + text, "", "  DO NOT SUBMIT DRAFT", "", _STATUS],
            # A plain trailing group is a draft, not the (styled) native footer.
            ["\x1b[1m›\x1b[0m " + text, "", "  DO NOT SUBMIT DRAFT"],
            # A styled paste placeholder is composer content, not footer.
            ["\x1b[1m›\x1b[0m " + text, "", "  \x1b[38;5;6m[Pasted Content 9 chars]\x1b[39m"],
            # Two blank lines before the footer: something sits between them.
            ["\x1b[1m›\x1b[0m " + text, "", "", _STATUS],
            # No footer at all: the composer end is not proven.
            ["\x1b[1m›\x1b[0m " + text],
        )
        for screen in screens:
            with self.subTest(screen=screen):
                self.assertFalse(composer_holds("codex", screen, text))

    def test_whitespace_changes_inside_a_line_are_refused(self):
        text = "Asha Control close request r1"
        self.assertFalse(composer_holds("claude", [RULE, "❯ AshaControl close request r1", RULE], text))
        self.assertFalse(composer_holds("claude", [RULE, "❯ Asha Control  close request r1", RULE], text))
        self.assertFalse(composer_holds("codex", ["› Asha Controlclose request r1", "", _STATUS], text))
        # A blank line inside the Claude box is an extra newline, never a wrap.
        self.assertFalse(composer_holds("claude", [RULE, "❯ Asha Control", "", "  close request r1", RULE], text))

    def test_only_the_wrap_space_may_move_at_a_line_break(self):
        text = "Asha Control close request r1"
        self.assertTrue(composer_holds("claude", [RULE, "❯ Asha Control", "   close request r1", RULE], text))
        # Extra blanks at the break, or a continuation without the native indent.
        self.assertFalse(composer_holds("claude", [RULE, "❯ Asha Control", "    close request r1", RULE], text))
        self.assertFalse(composer_holds("claude", [RULE, "❯ Asha Control", "close request r1", RULE], text))
        self.assertFalse(composer_holds("claude", [RULE, "❯  Asha Control close request r1", RULE], text))

    # QA3 #96 finding 2: a rule typed inside a draft is not the input box's border.
    def test_claude_rule_inside_a_draft_is_not_the_closing_border(self):
        text = "Asha Control close request r1"
        screens = (
            [RULE, "❯ " + text, "  " + RULE, "  DO NOT SUBMIT", RULE],
            [RULE, "❯ " + text, "  " + RULE, RULE],
            # A border narrower or wider than the top border is ambiguous.
            [RULE, "❯ " + text, RULE[:-3], "  DO NOT SUBMIT", RULE],
            [RULE, "❯ " + text, RULE + "──", RULE],
            # An indented top border is not the input box either.
            ["  " + RULE, "❯ " + text, RULE],
        )
        for screen in screens:
            with self.subTest(screen=screen):
                self.assertFalse(composer_holds("claude", screen, text))
        self.assertTrue(composer_holds("claude", [RULE, "❯ " + text, RULE, "  footer"], text))
        # The empty-prompt classifier uses the same borders.
        self.assertNotEqual(input_line_state("claude", ["  " + RULE, "❯ ", "  " + RULE])[0], "empty")

    def test_codex_footer_needs_real_styling_not_a_bare_reset(self):
        text = "Asha Control close request r1"
        self.assertFalse(composer_holds("codex", ["› " + text, "", "  \x1b[0mDO NOT SUBMIT"], text))
        self.assertFalse(composer_holds("codex", ["› " + text, "", "  \x1b[mDO NOT SUBMIT\x1b[0m"], text))

    def test_claude_paste_placeholders_cannot_be_bound_and_are_refused(self):
        text = "Asha Control close request r1"
        for token in ("[Pasted text #1 +10 lines]", "[Pasted text #1]", "[Pasted text #3 +1 line]"):
            with self.subTest(token=token):
                self.assertFalse(composer_holds("claude", [RULE, "❯ " + token, RULE], text))

    def test_unsupported_harness_is_unknown(self):
        for harness in ("copilot", "opencode"):
            self.assertEqual(input_line_state(harness, CLAUDE_IDLE)[0], "unknown")

    def test_flatten_makes_one_submit_free_line(self):
        self.assertEqual(flatten("a\nb\r\n  c\t"), "a b c")
        self.assertNotIn("\x1b", flatten("x\x1b[2my"))


if __name__ == "__main__":
    unittest.main()
