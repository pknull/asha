"""The local chair profile is explicit, launch-scoped, and permission-neutral."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class OpenCodeChairProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.asha = self.home / '.asha'
        self.asha.mkdir()
        for name in ('soul', 'voice', 'keeper', 'operation'):
            (self.asha / f'{name}.md').write_text(f'FULL-{name.upper()}\n')
        (self.asha / 'config.json').write_text('{}')
        skills = self.home / '.config/opencode/skills'
        skills.mkdir(parents=True)
        (skills / 'fixture').symlink_to(ROOT / 'plugins/session')
        self.capture = self.home / 'capture.json'
        self.native = self.home / 'opencode'
        self.native.write_text('''#!/usr/bin/python3
import json, os, pathlib, sys
if sys.argv[1:] == ['--version']:
    print('1.18.34')
else:
    cfg = json.loads(os.environ.get('OPENCODE_CONFIG_CONTENT', '{}'))
    pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps({
        'config': cfg, 'argv': sys.argv[1:],
        'instructions': [pathlib.Path(p).read_text() for p in cfg.get('instructions', [])],
    }))
''')
        self.native.chmod(0o700)

    def launch(self, profile='chair', variant=None, **extra):
        env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
               'ASHA_SESSION_PROFILE': profile, 'ASHA_OPENCODE_CMD': str(self.native),
               'CAPTURE': str(self.capture), 'OPENCODE_CONFIG_CONTENT': json.dumps({
                   'model': 'fixture/local', 'permission': {'bash': 'ask'},
                   'provider': {'fixture': {'options': {'baseURL': 'http://localhost:1'}}},
               })}
        if variant is not None:
            env['ASHA_OPENCODE_CHAIR_PROFILE'] = variant
        env.update(extra)
        self.capture.unlink(missing_ok=True)
        result = subprocess.run(['bash', str(ROOT / 'bin/asha'), 'opencode', 'PAYLOAD'],
                                cwd=self.home, env=env, capture_output=True, text=True)
        captured = json.loads(self.capture.read_text()) if self.capture.exists() else None
        return result, captured

    def test_compact_chair_is_explicit_and_keeps_native_settings(self):
        result, captured = self.launch(variant='compact')
        self.assertEqual(result.returncode, 0, result.stderr)
        text = '\n'.join(captured['instructions'])
        self.assertIn('# Asha: compact OpenCode chair', text)
        self.assertNotIn('FULL-SOUL', text)
        self.assertIn('FULL-OPERATION', text)
        self.assertIn(str(self.asha / 'soul.md'), text)
        self.assertIn('session-operate-control', text)
        self.assertIn('session-save', text)
        self.assertEqual(captured['config']['model'], 'fixture/local')
        self.assertEqual(captured['config']['permission'], {'bash': 'ask'})
        self.assertEqual(captured['config']['provider']['fixture']['options']['baseURL'],
                         'http://localhost:1')
        self.assertEqual(captured['argv'], ['PAYLOAD'])
        self.assertEqual((self.asha / 'soul.md').read_text(), 'FULL-SOUL\n')

    def test_full_remains_default(self):
        for variant in (None, 'full'):
            with self.subTest(variant=variant):
                result, captured = self.launch(variant=variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                text = '\n'.join(captured['instructions'])
                self.assertIn('FULL-SOUL', text)
                self.assertIn('FULL-OPERATION', text)
                self.assertNotIn('# Asha: compact OpenCode chair', text)

    def test_room_and_worker_do_not_inherit_compact_chair(self):
        for profile in ('room', 'worker'):
            with self.subTest(profile=profile):
                result, captured = self.launch(profile=profile, variant='compact')
                self.assertEqual(result.returncode, 0, result.stderr)
                text = '\n'.join(captured['instructions'])
                self.assertNotIn('# Asha: compact OpenCode chair', text)
                self.assertEqual('FULL-SOUL' in text, profile == 'room')

    def test_unknown_profile_refuses_before_native_launch(self):
        result, captured = self.launch(variant='compcat')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(captured)
        self.assertIn('ASHA_OPENCODE_CHAIR_PROFILE', result.stderr)

    def test_compact_requires_identity_authority(self):
        (self.asha / 'soul.md').unlink()
        result, captured = self.launch(variant='compact')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(captured)

    def test_identity_directory_refuses_but_regular_file_symlink_works(self):
        source = self.asha / 'soul.md'
        source.unlink()
        source.mkdir()
        result, captured = self.launch(variant='compact')
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(captured)
        source.rmdir()
        real_source = self.home / 'actual-soul.md'
        real_source.write_text('SYNTHETIC-SOUL\n')
        source.symlink_to(real_source)
        result, captured = self.launch(variant='compact')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(captured)

    def test_compact_does_not_override_disabled_stance(self):
        result, captured = self.launch(variant='compact', ASHA_ORCHESTRATOR_STANCE='0')
        self.assertEqual(result.returncode, 0, result.stderr)
        text = '\n'.join(captured['instructions'])
        self.assertNotIn('# Asha: compact OpenCode chair', text)
        self.assertIn('FULL-SOUL', text)

    def test_custom_brief_remains_authoritative(self):
        brief = self.home / 'custom-brief.md'
        brief.write_text('OPERATOR-CUSTOM-REQUIREMENT\n')
        result, captured = self.launch(variant='compact',
                                       ASHA_ORCHESTRATOR_BRIEF_FILE=str(brief))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('OPERATOR-CUSTOM-REQUIREMENT', '\n'.join(captured['instructions']))
        brief.unlink()
        result, captured = self.launch(variant='compact',
                                       ASHA_ORCHESTRATOR_BRIEF_FILE=str(brief))
        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(captured)

    def test_existing_instructions_are_retained(self):
        existing = self.home / 'existing.md'
        existing.write_text('EXISTING-NATIVE-INSTRUCTION\n')
        result, captured = self.launch(variant='compact', OPENCODE_CONFIG_CONTENT=json.dumps({
            'instructions': [str(existing)], 'permission': {'*': 'deny'},
        }))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(captured['config']['permission'], {'*': 'deny'})
        self.assertIn(str(existing), captured['config']['instructions'])
        self.assertIn('EXISTING-NATIVE-INSTRUCTION', '\n'.join(captured['instructions']))


if __name__ == '__main__':
    unittest.main()
