"""Verify real Claude/Codex mounts in clean temporary homes, never live homes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class DevopsMountTests(unittest.TestCase):
    def test_claude_and_codex_mount_same_canonical_skills(self):
        with tempfile.TemporaryDirectory(prefix='asha-devops-install-') as directory:
            home = Path(directory)
            (home / '.claude').mkdir()
            (home / '.claude/settings.json').write_text('{}\n')
            (home / '.codex').mkdir()
            (home / '.codex/config.toml').write_text('# fixture\n')
            # Deliberately do not inherit ASHA_HOME, CODEX_HOME, XDG, secrets,
            # hook/session IDs or any provider configuration from this Room.
            env = {'HOME': str(home), 'PATH': '/usr/local/bin:/usr/bin:/bin',
                   'LANG': 'C.UTF-8'}
            result = subprocess.run(
                ['bash', str(ROOT / 'install.sh'), '--target', 'both', '--only', 'devops'],
                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=120)
            self.assertEqual(0, result.returncode, result.stdout)
            for harness in ('claude', 'codex'):
                for name in ('inspect', 'unifi', 'change'):
                    source = ROOT / 'plugins/devops/skills' / name
                    mount = home / ('.' + harness) / 'skills' / ('devops-' + name)
                    self.assertTrue(mount.is_symlink())
                    self.assertEqual(source.resolve(), mount.resolve())
                    self.assertEqual((source / 'SKILL.md').read_bytes(), (mount / 'SKILL.md').read_bytes())
                for name, helper in (('inspect', 'fleet.py'), ('unifi', 'unifi_read.py')):
                    path = home / ('.' + harness) / 'skills' / ('devops-' + name) / 'scripts' / helper
                    check = subprocess.run(['python3', str(path), '--help'], env=env,
                                           capture_output=True, text=True, timeout=10)
                    self.assertEqual(0, check.returncode, check.stderr)


if __name__ == '__main__':
    unittest.main()
