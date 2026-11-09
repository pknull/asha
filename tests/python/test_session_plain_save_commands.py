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
# #136 trial (2026-11-09): a nested project's Memory is often gitignored and a handoff never commits.
PREIMAGE = 'a new directory `Work/component-preimage-YYYYMMDD-HHMMSS/<component.path>/`'


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


class DigestsBeforeRereadTests(unittest.TestCase):
    """#136: the compare-and-swap protects a draft only when its writer takes the digests, then rereads the pair."""

    def assert_order(self, text, *marks):
        text = ' '.join(text.split())
        found = [text.find(mark) for mark in marks]
        self.assertNotIn(-1, found, list(zip(marks, found)))
        self.assertEqual(found, sorted(found), list(zip(marks, found)))
        self.assertLess(text.rfind(marks[0]), found[1], 'the digests are taken again after the reread')

    def test_close_request_takes_digests_then_rereads_and_never_only_refreshes_them(self):
        from lib.control import session_closure as closure
        from lib.control.session_store import text as message_text
        memory = {'available': True, 'destination': '/p/Memory', 'baseline': {'activeContext.md': 'a', 'decisions.md': 'd'}}
        body = closure.request_text({'session_id': 'S', 'generation': 1}, {'request_id': 'R', 'memory': memory})
        self.assert_order(body, 'asha control session handoff --read --json', 'then reread both',
                          '--expected-active DIGEST', 'take the digests again, then reread both files, merge',
                          'never only refresh the digests')
        self.assertEqual(message_text(body, 'message'), body)  # the structured enqueue's 64 KiB cap, the only one

    def test_assignment_contracts_take_digests_then_reread(self):
        self.assert_order(ROOM_INSTRUCTION, 'asha control session handoff --read --json', 'then reread both',
                          '--expected-active DIGEST', 'take the digests again, then reread and merge',
                          'never only refresh the digests')
        self.assert_order(WORKER_INSTRUCTION, 'asha control session handoff --read --json', 'then reread the pair')

    def test_skill_nested_step_takes_the_childs_digests_then_rereads_its_pair(self):
        nested, = [p for p in SKILL.read_text(encoding='utf-8').split('\n\n') if '--read --component' in p]
        self.assert_order(nested, 'handoff --read --component PATH --json', 'then reread its pair',
                          'plus `--component PATH`', 'take its digests again, then reread and merge',
                          'never only refresh the digests')

    def test_skill_nested_step_copies_the_reread_pair_before_publishing_and_names_it(self):
        nested, = [p for p in SKILL.read_text(encoding='utf-8').split('\n\n') if '--read --component' in p]
        self.assert_order(nested, 'handoff --read --component PATH --json', 'then reread its pair',
                          'before publishing copy both files you reread, byte for byte', PREIMAGE,
                          '`component.acting_root`', 'name every such directory in your report',
                          'plus `--component PATH`',
                          'take its digests again, then reread and merge, copying that pair to a new directory too')
        nested = ' '.join(nested.split())
        for phrase in ('never over an earlier copy', 'never committed'):
            self.assertIn(phrase, nested)

    def test_session_hub_doc_names_the_preimage_copy(self):
        doc = (ROOT / 'docs/session-hub.md').read_text(encoding='utf-8')
        section = re.split(r'\n##+ ', doc.split('### Nested project Memory (#130)', 1)[1], maxsplit=1)[0]
        self.assert_order(section, 'takes the digests', 'rereads the pair',
                          'before publishing copies both files byte for byte', PREIMAGE,
                          'copies the pair it rereads to a new directory too', 'never commits a copy')


if __name__ == '__main__':
    unittest.main()
