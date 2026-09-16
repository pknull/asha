"""C8 executable save gate and rendered Codex approval rules."""
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.python.test_control_session_closure import ClosureFixture
from lib.control.experience_cli import dispatch

ROOT = Path(__file__).resolve().parents[2]


class SaveGate(unittest.TestCase):
    def test_drift_reports_stale_execution_rules_without_rewriting(self):
        with tempfile.TemporaryDirectory(dir=os.environ['HOME']) as home:
            rules = Path(home) / '.codex/rules/asha.rules'
            rules.parent.mkdir(parents=True)
            rules.write_text('# stale operator fixture\n')
            result = subprocess.run(['./bin/asha-drift-check.sh', '--target', 'codex'], cwd=ROOT,
                text=True, capture_output=True, env=dict(os.environ, HOME=home))
            self.assertIn('Codex execution rules', result.stdout)
            self.assertEqual(rules.read_text(), '# stale operator fixture\n')

    def test_save_gate_uses_one_read_and_silence_uses_none(self):
        document = (ROOT / 'plugins/session/commands/save.md').read_text()
        gate = next((block for block in re.findall(r'```bash\n(.*?)```', document, re.S)
                     if 'EXPERIENCE_ENABLED=0' in block), None)
        self.assertIsNotNone(gate, 'save needs an executable single policy gate')
        self.assertGreaterEqual(document.count('EXPERIENCE_ENABLED'), 3,
                                'both experience steps must use the same gate')
        with tempfile.TemporaryDirectory(dir=os.environ['HOME']) as home:
            project = Path(home) / 'project'
            project.mkdir()
            for mode, silence, expected in [('off', False, '0'), ('capture', False, '1'),
                                            ('review', False, '1'), ('capture', True, '0')]:
                with self.subTest(mode=mode, silence=silence):
                    marker = project / 'Work/markers/silence'
                    if silence:
                        marker.parent.mkdir(parents=True)
                        marker.touch()
                    script = ('asha() { printf "%s\\n" "$*" >> "$HOME/commands"; '
                              'printf \'{"mode":"%s"}\\n\' "$TEST_MODE"; }\n' + gate +
                              '\nprintf "%s" "$EXPERIENCE_ENABLED"')
                    log = Path(home) / 'commands'
                    log.write_text('')
                    result = subprocess.run(['bash', '-euc', script], text=True, capture_output=True,
                        env=dict(os.environ, HOME=home, PLANE_BASE=str(project), TEST_MODE=mode), check=True)
                    self.assertEqual(result.stdout, expected)
                    commands = log.read_text().splitlines()
                    self.assertEqual(len(commands), 0 if silence else 1)
                    if commands:
                        self.assertIn('experience policy --read-only', commands[0])

    def test_rendered_rules_allow_reads_but_never_experience_writes(self):
        with tempfile.TemporaryDirectory(dir=os.environ['HOME']) as home:
            script = ('source lib/install.sh\nsource harnesses/codex.sh\n'
                      'DRY_RUN=0\nVERBOSE=0\ncodex_install_rules\n')
            subprocess.run(['bash', '-euc', script], cwd=ROOT, check=True, capture_output=True,
                           env=dict(os.environ, HOME=home))
            rules = []
            exec((Path(home) / '.codex/rules/asha.rules').read_text(),
                 {'prefix_rule': lambda **value: rules.append(value)})
            allowed = [rule['pattern'] for rule in rules if rule['decision'] == 'allow']
            def matches(command):
                args = shlex.split(command)
                return any(args[:len(prefix)] == prefix for prefix in allowed)
            for suffix in ('policy --read-only --project P --json', 'pending --project P --json', 'show REPORT --json'):
                self.assertTrue(matches('asha control session experience ' + suffix), suffix)
            for suffix in ('policy --mode capture --project P', 'policy --clear --project P',
                           'dispose --project P --decision-file D --publication-file R',
                           'review --project P --report R --result-file F'):
                self.assertFalse(matches('asha control session experience ' + suffix), suffix)
            self.assertFalse(matches('asha control session report --state finished'))


class PolicyReadOnly(ClosureFixture):
    def test_policy_read_only_mode_cannot_be_combined_with_mutations(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dispatch(self.hub, ['policy', '--read-only', '--project', str(self.project), '--json']), 0)
            for flags in (['--mode', 'capture'], ['--clear']):
                with self.subTest(flags=flags), mock.patch('lib.control.session_experience.Experiences.set_policy') as setter:
                    with self.assertRaises(SystemExit):
                        dispatch(self.hub, ['policy', '--read-only', '--project', str(self.project)] + flags)
                    setter.assert_not_called()


if __name__ == '__main__':
    unittest.main()
