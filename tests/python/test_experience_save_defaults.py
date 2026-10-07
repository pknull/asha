"""Rendered Codex approval rules: the pinned report/handoff allow and nothing more.

The file keeps its name from the retired experience save gate (N2); its tests
cover the Codex rules the installer renders.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

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

    @staticmethod
    def render_rules(home):
        """The rules ``codex_render_rules`` prints for ``home``, parsed into (text, rules, host executables)."""
        script = 'source lib/install.sh\nsource harnesses/codex.sh\nHOME="$1"\ncodex_render_rules\n'
        text = subprocess.run(['bash', '-euc', script, 'render', home], cwd=ROOT, check=True,
                              capture_output=True, text=True, env=dict(os.environ)).stdout
        rules, hosts = [], []
        exec(text, {'prefix_rule': lambda **value: rules.append(value),
                    'host_executable': lambda **value: hosts.append(value)})
        return text, rules, hosts

    @staticmethod
    def allows(rules, command):
        """Prefix match as Codex does it: a list element in a pattern is a set of alternatives."""
        args = shlex.split(command)
        def element(pattern, arg):
            return arg in pattern if isinstance(pattern, list) else arg == pattern
        return any(len(args) >= len(rule['pattern'])
                   and all(element(p, a) for p, a in zip(rule['pattern'], args))
                   for rule in rules if rule['decision'] == 'allow')

    def test_rendered_rules_allow_only_report_and_handoff(self):
        with tempfile.TemporaryDirectory(dir=os.environ['HOME']) as home:
            script = ('source lib/install.sh\nsource harnesses/codex.sh\n'
                      'DRY_RUN=0\nVERBOSE=0\ncodex_install_rules\n')
            subprocess.run(['bash', '-euc', script], cwd=ROOT, check=True, capture_output=True,
                           env=dict(os.environ, HOME=home))
            rules = []
            exec((Path(home) / '.codex/rules/asha.rules').read_text(),
                 {'prefix_rule': lambda **value: rules.append(value),
                  'host_executable': lambda **value: None})
            def matches(command):
                return self.allows(rules, command)
            # Experience capture is retired (N2): none of its former read allows renders.
            self.assertNotIn('experience', (Path(home) / '.codex/rules/asha.rules').read_text())
            for suffix in ('policy --read-only --project P --json', 'pending --project P --json', 'show REPORT --json',
                           'unreviewed --project P', 'packet REPORT --json', 'policy --mode capture --project P'):
                self.assertFalse(matches('asha control session experience ' + suffix), suffix)
            # #112: these two act only for their environment's session and write Control state, so they run outside the sandbox.
            self.assertTrue(matches('asha control session report --state finished --text DONE'))
            self.assertTrue(matches('asha control session handoff --read --json'))
            self.assertTrue(matches('asha control session handoff --outcome no-durable-update --detail WHY --json'))
            for verb in ('launch --project P --prompt X', 'send SESSION TEXT', 'close SESSION', 'stop SESSION',
                         'attach SESSION', 'list', 'messages'):
                self.assertFalse(matches('asha control session ' + verb), verb)
            for command in ('asha control session', 'asha control report --state finished',
                            'asha session report --state finished', 'asha control session reports'):
                self.assertFalse(matches(command), command)

    def test_rules_pin_asha_to_the_user_bin_and_omit_allows_without_a_home(self):
        with tempfile.TemporaryDirectory(dir=os.environ['HOME']) as home:
            _, rules, hosts = self.render_rules(home)
            self.assertEqual(hosts, [{'name': 'asha', 'paths': [home + '/.local/bin/asha']}])
            self.assertTrue(any(rule['decision'] == 'allow' for rule in rules))
            _, _, slashed = self.render_rules(home + '/')
            self.assertEqual(slashed, hosts, 'a trailing slash must not change the pinned path')
        for home in ('', '/', '//', 'relative/home'):
            with self.subTest(home=home):
                text, rules, hosts = self.render_rules(home)
                self.assertEqual(hosts, [])
                self.assertEqual([r for r in rules if r['decision'] == 'allow'], [])
                self.assertNotIn('decision = "allow"', text)
                self.assertTrue(any(rule['decision'] == 'prompt' for rule in rules),
                                'prompt rules still render without a home')

    def test_codex_execpolicy_resolves_only_the_pinned_asha(self):
        codex = shutil.which('codex')
        if codex is None:
            self.skipTest('codex is absent: the native execpolicy pin check needs the codex CLI')
        with tempfile.TemporaryDirectory(dir=os.environ['HOME']) as home:
            text, _, _ = self.render_rules(home)
            rules = Path(home) / 'asha.rules'
            rules.write_text(text)
            def decision(*argv):
                result = subprocess.run(
                    [codex, 'execpolicy', 'check', '--resolve-host-executables', '-r', str(rules), *argv],
                    cwd=home, env=dict(os.environ, HOME=home), capture_output=True, text=True, check=True)
                return json.loads(result.stdout).get('decision')
            pinned = home + '/.local/bin/asha'
            for program in ('asha', pinned):
                for verb in (['report', '--state', 'finished', '--text', 'DONE'], ['handoff', '--read', '--json']):
                    with self.subTest(program=program, verb=verb[0]):
                        self.assertEqual(decision(program, 'control', 'session', *verb), 'allow')
                for verb in ('launch', 'send', 'close', 'stop', 'attach'):
                    with self.subTest(program=program, verb=verb):
                        self.assertIsNone(decision(program, 'control', 'session', verb, 'X'))
            for planted in ('./asha', 'Work/asha', '/tmp/x/asha'):
                for verb in ('report', 'handoff'):
                    with self.subTest(planted=planted, verb=verb):
                        self.assertIsNone(decision(planted, 'control', 'session', verb, '--json'))
                with self.subTest(planted=planted, verb='experience'):
                    self.assertIsNone(decision(planted, 'control', 'session', 'experience', 'pending'))


if __name__ == '__main__':
    unittest.main()
