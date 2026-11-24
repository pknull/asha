"""Execute the documented panel actor-resolution command, including failures."""
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]


class PanelCompletionTests(unittest.TestCase):
    def run_resolution(self, root=None):
        text = (ROOT / 'plugins/panel/commands/panel.md').read_text()
        blocks = [block for block in re.findall(r'```bash\n(.*?)```', text, re.S)
                  if '--print-actor' in block]
        self.assertEqual(len(blocks), 1)
        with tempfile.TemporaryDirectory() as home:
            env = {'PATH': '/usr/bin:/bin', 'HOME': home, 'LANG': 'C.UTF-8',
                   'PYTHONDONTWRITEBYTECODE': '1'}
            if root is not None:
                env['ASHA_ROOT'] = str(root)
            return subprocess.run(['bash', '-c', textwrap.dedent(blocks[0])],
                                  cwd=home, env=env, text=True, capture_output=True,
                                  timeout=10)

    def test_uses_installed_plugin_version_without_project_metadata(self):
        result = self.run_resolution(ROOT)
        version = re.search(r'^\*\*Version\*\*: (\S+)$',
                            (ROOT / 'plugins/panel/README.md').read_text(), re.M)[1]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ACTOR: asha/' + version + '\n', result.stdout)
        self.assertRegex(result.stdout, r'NOW: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')

    def test_unavailable_resolver_does_not_emit_successful_trust_fields(self):
        with tempfile.TemporaryDirectory() as empty_root:
            result = self.run_resolution(empty_root)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('ACTOR:', result.stdout)
        self.assertNotIn('NOW:', result.stdout)

    def test_unresolved_root_is_a_failure(self):
        result = self.run_resolution()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('ACTOR:', result.stdout)
        self.assertNotIn('NOW:', result.stdout)


if __name__ == '__main__':
    unittest.main()
