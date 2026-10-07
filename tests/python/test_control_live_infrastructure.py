"""Retirement steps L-a1 and L-b: live Control infrastructure never loads the engine.

The supervisor service, the project index and the small helpers live Control
uses import and run without ``lib.control.orchestration``, which L-b retired.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENGINE = 'lib.control.orchestration'
LIVE = (
    'lib.control.supervisor_service', 'lib.control.projects', 'lib.control.rooms',
    'lib.control.hub_cli', 'lib.control.session_hub', 'lib.control.session_tui',
    'lib.control.session_modals', 'lib.control.sessions', 'lib.control.session_store',
    'lib.control.session_experience', 'lib.control.doctor', 'lib.control.text',
    'lib.control.tmux',
)


def fresh(code, env=None):
    done = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True,
                          timeout=60, check=False, env=env)
    if done.returncode:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout.splitlines()[-1])


def loaded(names):
    return f'''
import importlib, json, sys
for name in {list(names)!r}:
    importlib.import_module(name)
print(json.dumps(sorted(n for n in sys.modules if n == {ENGINE!r} or n.startswith({ENGINE + '.'!r}))))
'''


class ImportIsolationTests(unittest.TestCase):
    def test_each_live_module_imports_without_the_engine(self):
        for name in LIVE:
            with self.subTest(module=name):
                self.assertEqual(fresh(loaded([name])), [])

    def test_the_project_index_reads_the_plugin_tools_directly(self):
        code = '''
import json, sys
import lib.control.projects
print(json.dumps(sorted(n for n in sys.modules if n.startswith('lib.control.') and n != 'lib.control.projects')))
'''
        # The task substrate's context module is not part of the project index.
        self.assertNotIn('lib.control.context', fresh(code))

    def test_supervisor_status_paths_never_load_the_engine(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            code = f'''
import contextlib, io, json, sys
from lib.control.config import load_config
from lib.control import supervisor_service as service
env = {{'HOME': {str(home)!r}, 'ASHA_HOME': {str(home / 'asha')!r}, 'PATH': '/usr/bin:/bin'}}
config = load_config(env)
status, code = service.supervisor_status(config)
with contextlib.redirect_stdout(io.StringIO()) as out:
    cli = service.supervisor_main(['status', '--json'], env=env)
print(json.dumps({{'status': status['status'], 'code': code, 'cli': cli,
                  'cli_status': json.loads(out.getvalue())['status'],
                  'engine': sorted(n for n in sys.modules if n.startswith({ENGINE!r}))}}))
'''
            result = fresh(code)
        self.assertEqual(result, {'status': 'stopped', 'code': 1, 'cli': 1,
                                  'cli_status': 'stopped', 'engine': []})


class MovedHelperTests(unittest.TestCase):
    def test_terminal_safe_escapes_controls_through_nested_values(self):
        from lib.control.text import terminal_safe
        value = {'a\x1b': ['b‮', 3], 'c': 'plain'}
        self.assertEqual(terminal_safe(value),
                         {'a\\u001b': ['b\\u202e', 3], 'c': 'plain'})


class ManagedAnchorTests(unittest.TestCase):
    def anchor(self, **changes):
        value = {'kind': 'managed-session-v1', 'session_id': '11111111-1111-4111-8111-111111111111',
                 'state_dir': '/tmp', 'owner_pid': 42, 'process_start_identity': 'boot:1',
                 'generation': 1}
        value.update(changes)
        return value

    def test_valid_anchor_is_returned_as_a_copy(self):
        from lib.control.session_store import validate_managed_anchor
        anchor = self.anchor()
        validated = validate_managed_anchor(anchor)
        self.assertEqual(validated, anchor)
        self.assertIsNot(validated, anchor)

    def test_invalid_anchors_refuse(self):
        from lib.control.session_store import validate_managed_anchor
        from lib.control.store import StoreError
        cases = {
            'extra field': self.anchor(extra=1),
            'missing field': {k: v for k, v in self.anchor().items() if k != 'generation'},
            'session id': self.anchor(session_id='11111111-1111-4111-8111-11111111111X'),
            'pid': self.anchor(owner_pid=0),
            'generation': self.anchor(generation=True),
            'identity': self.anchor(process_start_identity='x' * 201),
            'relative state': self.anchor(state_dir='tmp'),
            'state text': self.anchor(state_dir=7),
        }
        for label, anchor in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(StoreError, '^managed '):
                    validate_managed_anchor(anchor)
        with self.assertRaisesRegex(StoreError, 'managed coordinator anchor must be an object'):
            validate_managed_anchor(['not', 'an', 'object'])


if __name__ == '__main__':
    unittest.main()
