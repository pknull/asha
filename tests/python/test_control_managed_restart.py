"""Real owners started on demand outlive their caller and serve a question once."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from lib.control.config import load_config
from lib.control.harness import process_identity
from lib.control.session_store import SessionStore, process_live
from lib.control.sessions import overview


class ManagedRestartTests(unittest.TestCase):
    def test_an_owner_started_by_an_exited_caller_serves_its_question_and_one_answer(self):
        with tempfile.TemporaryDirectory(prefix='asha-restart-') as temporary:
            root = Path(temporary)
            env = {k: v for k, v in os.environ.items() if not k.startswith(('ASHA_', 'TMUX'))}
            env.update(ASHA_HOME=str(root / 'asha'), ASHA_CONFIG=str(root / 'config.json'),
                       XDG_RUNTIME_DIR=str(root / 'runtime'), ASHA_TEST_ROOT=str(root),
                       PYTHONPATH=str(Path(__file__).resolve().parents[2]))
            (root / 'runtime').mkdir(mode=0o700)
            (root / 'config.json').write_text('{}')
            (root / 'config.json').chmod(0o600)
            config = load_config(env)
            owners = []
            log = config.tasks_dir.parent / 'session-logs'
            def until(predicate, message, timeout=10):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    value = predicate()
                    if value:
                        return value
                    time.sleep(.03)
                logs = ''.join(p.read_text() for p in log.glob('*.log')) if log.is_dir() else ''
                self.fail(message + '\n' + logs)
            def wake(sid):
                # The caller exits at once; nothing it leaves running schedules work.
                done = subprocess.run([sys.executable, str(Path(__file__).with_name('managed_restart_fixture.py')),
                                       'wake', sid], cwd=root, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(done.returncode, 0, done.stderr)
                return json.loads(done.stdout)
            with SessionStore(config, create=True) as store:
                sid = store.create(cwd=str(root), prompt='Ask which tone', harness='codex', max_turns=2)['session_id']
                try:
                    self.assertEqual(wake(sid)['owners_started'], 1)
                    until(lambda: (root / 'provider-holding').exists(), 'provider did not open its question')
                    owner = store.get(sid)
                    owners.append((owner['owner_pid'], owner['owner_identity']))
                    self.assertTrue(process_live(owner['owner_pid'], owner['owner_identity']))
                    provider_pid = int((root / 'provider-holding').read_text())
                    provider_identity = process_identity(provider_pid)
                    question = store.snapshot(sid)['requests'][0]
                    self.assertEqual(owner['state'], 'running')
                    # A second caller while the owner is live starts nothing.
                    self.assertEqual(wake(sid)['owners_started'], 0)
                    self.assertEqual(store.get(sid)['generation'], owner['generation'])
                    self.assertTrue(process_live(provider_pid, provider_identity))
                    self.assertEqual(overview(config)['questions'], 1)
                    (root / 'release-provider').touch()
                    until(lambda: store.get(sid)['state'] == 'waiting-input', 'first turn did not finish')
                    # With nothing runnable the owner hands custody back and exits.
                    until(lambda: store.get(sid)['owner_pid'] is None, 'owner kept custody without work')
                    until(lambda: not process_live(owner['owner_pid'], owner['owner_identity']), 'owner did not exit')
                    answer = store.answer(question['request_id'], 'Quiet', expected_digest=question['digest'])
                    self.assertEqual(answer, store.answer(question['request_id'], 'Quiet', expected_digest=question['digest']))
                    self.assertEqual(wake(sid)['owners_started'], 1)
                    until(lambda: store.get(sid)['turns'] == 2 and store.get(sid)['state'] == 'idle'
                          and store.get(sid)['owner_pid'] is None, 'answer did not finish')
                    snapshot = store.snapshot(sid)
                    self.assertEqual(snapshot['pending_request_count'], 0)
                    self.assertEqual(len(snapshot['messages']), 2)
                    self.assertEqual([e['payload']['text'] for e in snapshot['events'] if e['kind'] == 'text'],
                                     ['Answer received once: Quiet'])
                    # Two owners, one native conversation.
                    self.assertEqual(store.get(sid)['native_id'], 'restart-native-thread')
                    self.assertEqual(store.get(sid)['generation'], owner['generation'] + 1)
                finally:
                    (root / 'release-provider').touch()
                    store.stop(sid)
                    until(lambda: all(not process_live(pid, identity) for pid, identity in owners),
                          'owned session failed to stop', timeout=5)
