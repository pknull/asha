"""Exercise missing bundled helpers through the real installer and doctor."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPERS = (
    'skills/novel-state/scripts/artifact_freshness.py',
    'skills/revision-pass/scripts/revision_compare.py',
    'skills/inline-review/scripts/inline_review.py',
)


class WritingAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'source'
        self.repo.mkdir()
        # Only the affected source directories are copied. Other public source
        # entries are links; neither the live source nor the live HOME is edited.
        for entry in ROOT.iterdir():
            if entry.name.startswith('.') or entry.name in ('Work', 'Memory', 'memory-local'):
                continue
            if entry.name in ('lib', 'bin', 'plugins'):
                target = self.repo / entry.name
                target.mkdir()
                for child in entry.iterdir():
                    dest = target / child.name
                    if (entry.name, child.name) in (('lib', 'install.sh'), ('bin', 'asha-drift-check.sh')):
                        shutil.copy2(child, dest)
                    elif entry.name == 'plugins' and child.name == 'write':
                        shutil.copytree(child, dest)
                    else:
                        dest.symlink_to(child, target_is_directory=child.is_dir())
            elif entry.name == 'install.sh':
                shutil.copy2(entry, self.repo / entry.name)
            else:
                (self.repo / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
        self.home = self.base / 'home'
        self.home.mkdir()
        self.env = {'PATH': os.environ['PATH'], 'HOME': str(self.home),
                    'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}

    def run_command(self, *args):
        return subprocess.run(args, cwd=self.repo, env=self.env,
                              text=True, capture_output=True, timeout=120)

    def test_missing_helper_refuses_write_install_on_every_harness_before_mutation(self):
        missing = self.repo / 'plugins/write' / HELPERS[0]
        missing.unlink()
        for harness in ('claude', 'codex', 'copilot', 'opencode'):
            with self.subTest(harness=harness):
                result = self.run_command('bash', str(self.repo / 'install.sh'),
                                          '--only', 'write', '--target', harness)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn('writing helper unavailable: ' + HELPERS[0], result.stderr)
                self.assertEqual(list(self.home.iterdir()), [], result.stdout)

    def test_unselected_write_does_not_block_another_plugin(self):
        (self.repo / 'plugins/write' / HELPERS[0]).unlink()
        (self.home / '.claude').mkdir()
        (self.home / '.claude/settings.json').write_text('{}\n')
        result = self.run_command('bash', str(self.repo / 'install.sh'), '--only', 'test',
                                  '--target', 'claude', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('writing helper unavailable', result.stderr)

    def test_doctor_reports_each_missing_bundled_dependency(self):
        for helper in HELPERS:
            (self.repo / 'plugins/write' / helper).unlink()
        result = self.run_command('bash', str(self.repo / 'bin/asha-drift-check.sh'), '--target', 'all')
        self.assertNotEqual(result.returncode, 0)
        for helper in HELPERS:
            self.assertIn('writing helper unavailable: ' + helper, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
