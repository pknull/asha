"""Kill/restart the scheduling supervisor with a live native actor connection."""
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
    def test_supervisor_crash_preserves_owner_provider_question_and_answer_once(self):
        with tempfile.TemporaryDirectory(prefix='asha-restart-') as temporary:
            root = Path(temporary)
            env = {k: v for k, v in os.environ.items() if not k.startswith(('ASHA_', 'TMUX'))}
            env.update(ASHA_HOME=str(root / 'asha'), ASHA_CONFIG=str(root / 'config.json'),
                       XDG_RUNTIME_DIR=str(root / 'runtime'), ASHA_TEST_ROOT=str(root),
                       PYTHONPATH=str(Path(__file__).resolve().parents[2]))
            (root / 'runtime').mkdir(mode=0o700)
            (root / 'config.json').write_text(json.dumps({'orchestration': {'supervisor_interval_seconds': 1}}))
            (root / 'config.json').chmod(0o600)
            config = load_config(env)
            supervisors = []
            owners = []
            def until(predicate, message, timeout=10):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    value = predicate()
                    if value:
                        return value
                    time.sleep(.03)
                self.fail(message + '\n' + (root / 'supervisor.log').read_text())
            with SessionStore(config, create=True) as store, open(root / 'supervisor.log', 'w') as log:
                sid = store.create(cwd=str(root), prompt='Ask which tone', harness='codex', max_turns=2)['session_id']
                def start():
                    process = subprocess.Popen([sys.executable,
                        str(Path(__file__).with_name('managed_restart_fixture.py')), 'supervisor'],
                        cwd=root, env=env, stdout=log, stderr=log, start_new_session=True)
                    supervisors.append(process)
                    return process
                try:
                    first = start()
                    until(lambda: (root / 'provider-holding').exists(), 'provider did not open its question')
                    owner = store.get(sid)
                    owners.append((owner['owner_pid'], owner['owner_identity']))
                    provider_pid = int((root / 'provider-holding').read_text())
                    provider_identity = process_identity(provider_pid)
                    question = store.snapshot(sid)['requests'][0]
                    self.assertEqual(owner['state'], 'running')
                    first.kill()
                    self.assertEqual(first.wait(timeout=3), -9)
                    self.assertTrue(process_live(owner['owner_pid'], owner['owner_identity']))
                    self.assertTrue(process_live(provider_pid, provider_identity))
                    replacement = start()
                    from lib.control.orchestration.config import from_control
                    from lib.control.orchestration.supervisor_daemon import supervisor_status
                    until(lambda: supervisor_status(from_control(config))[0].get('pid') == replacement.pid,
                          'replacement supervisor did not claim its lock')
                    time.sleep(1.1)  # One actual replacement tick, below UI update bound.
                    current = store.get(sid)
                    self.assertEqual((current['owner_pid'], current['generation']),
                                     (owner['owner_pid'], owner['generation']))
                    self.assertEqual(current['turns'], 1)
                    self.assertTrue(process_live(provider_pid, provider_identity))
                    self.assertEqual(store.get_request(question['request_id'])['state'], 'pending')
                    self.assertEqual(overview(config)['questions'], 1)
                    (root / 'release-provider').touch()
                    until(lambda: store.get(sid)['state'] == 'waiting-input', 'first turn did not finish')
                    answer = store.answer(question['request_id'], 'Quiet', expected_digest=question['digest'])
                    self.assertEqual(answer, store.answer(question['request_id'], 'Quiet', expected_digest=question['digest']))
                    until(lambda: store.get(sid)['turns'] == 2 and store.get(sid)['state'] == 'idle', 'answer did not finish')
                    snapshot = store.snapshot(sid)
                    self.assertEqual(snapshot['pending_request_count'], 0)
                    self.assertEqual(len(snapshot['messages']), 2)
                    self.assertEqual([e['payload']['text'] for e in snapshot['events'] if e['kind'] == 'text'],
                                     ['Answer received once: Quiet'])
                    self.assertEqual(store.get(sid)['native_id'], 'restart-native-thread')
                    self.assertEqual(store.get(sid)['generation'], owner['generation'])
                finally:
                    store.stop(sid)
                    until(lambda: all(not process_live(pid, identity) for pid, identity in owners),
                          'owned session failed to stop', timeout=5)
                    for process in supervisors:
                        if process.poll() is None:
                            process.terminate()
                        process.wait(timeout=5)
