"""#112: the save path asks for plain, separate asha commands.

Codex runs an allowed `asha control session report|handoff` outside its sandbox
only when the whole command is that one plain call; chained or expanded, it
stays sandboxed and the ownership proof fails. The skill and both assignment
contracts say so, and take the save digests from `handoff --read`.
"""
import re
import unittest
from pathlib import Path

from lib.control.session_completion import ROOM_INSTRUCTION, WORKER_INSTRUCTION

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / 'plugins/session/skills/project-memory/SKILL.md'
PLAIN = 'on its own with literal arguments'
FORBIDDEN = 'no `&&`, pipes, redirection, `$VAR` or heredoc'


class PlainSaveCommandTests(unittest.TestCase):
    def test_both_assignment_contracts_ask_for_plain_separate_commands(self):
        for name, text in (('worker', WORKER_INSTRUCTION), ('room', ROOM_INSTRUCTION)):
            with self.subTest(contract=name):
                self.assertIn(PLAIN, text)
                self.assertIn(FORBIDDEN, text)
                self.assertIn('edit tool', text)
                self.assertIn('asha control session handoff --read --json', text)
                self.assertNotIn('$ASHA_ROOT', text)

    def test_skill_takes_save_digests_from_handoff_read_and_runs_commands_plainly(self):
        raw = SKILL.read_text(encoding='utf-8')
        text = ' '.join(raw.split())  # Markdown wraps prose anywhere
        self.assertIn(PLAIN, text)
        self.assertIn(FORBIDDEN, text)
        self.assertIn('edit tool', text)
        self.assertIn('memory.baseline', text)
        self.assertNotIn('retain `digests.active` and `digests.decisions`', text)
        commands = re.findall(r'`(asha control session [^`]*)`', text)
        commands += [line for block in re.findall(r'```bash\n(.*?)```', raw, re.S)
                     for line in block.splitlines() if 'asha control session' in line]
        self.assertTrue(commands)
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(re.search(r'&&|\||[<>]|\$|<<|;', command), command)


if __name__ == '__main__':
    unittest.main()
