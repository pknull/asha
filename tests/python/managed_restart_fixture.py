"""Deterministic provider behind real owner and supervisor processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest import mock


def emit(value):
    print(json.dumps(value), flush=True)


def provider():
    thread = 'restart-native-thread'
    turn = 'native-' + os.environ['ASHA_MANAGED_TURN_ID']
    root = Path(os.environ['ASHA_TEST_ROOT'])
    def completed():
        emit({'method': 'turn/completed', 'params': {'threadId': thread,
              'turn': {'id': turn, 'status': 'completed', 'items': []}}})
    for line in sys.stdin:
        frame = json.loads(line)
        method = frame.get('method')
        if method == 'initialize':
            emit({'id': frame['id'], 'result': {}})
        elif method in {'thread/start', 'thread/resume'}:
            params = frame['params']
            emit({'id': frame['id'], 'result': {'thread': {'id': thread}, 'cwd': params['cwd'],
                  'approvalPolicy': 'untrusted', 'approvalsReviewer': 'user',
                  'sandbox': {'type': 'workspaceWrite', 'networkAccess': False, 'writableRoots': []}}})
        elif method == 'turn/start':
            emit({'id': frame['id'], 'result': {'turn': {'id': turn, 'status': 'inProgress'}}})
            prompt = frame['params']['input'][0]['text']
            if 'Answer to request ' in prompt:
                assert 'Quiet' in prompt
                emit({'method': 'item/agentMessage/delta', 'params': {
                    'threadId': thread, 'turnId': turn, 'itemId': 'answer', 'delta': 'Answer received once: Quiet'}})
                completed()
            else:
                emit({'id': 99, 'method': 'item/tool/call', 'params': {
                    'threadId': thread, 'turnId': turn, 'callId': 'question-call',
                    'tool': 'asha_control', 'arguments': {'operation': 'ask', 'question': 'Which tone?'}}})
        elif frame.get('id') == 99:
            assert frame['result']['success'], frame
            (root / 'provider-holding').write_text(str(os.getpid()))
            deadline = time.monotonic() + 25
            while not (root / 'release-provider').exists():
                if time.monotonic() >= deadline:
                    raise RuntimeError('fixture release deadline')
                time.sleep(.02)
            completed()


def owner(sid):
    from lib.control.config import load_config
    from lib.control.session_harness import CodexTransport
    from lib.control.sessions import run_owner
    def factory(_argv, **kwargs):
        return CodexTransport([sys.executable, str(Path(__file__).resolve()), 'provider'], timeout=30, **kwargs)
    return run_owner(load_config(), sid, transport_factory=factory)


def supervisor():
    from lib.control.orchestration.config import load_config
    from lib.control.orchestration.supervisor_daemon import run_supervisor
    real_popen = subprocess.Popen
    def launch(argv, **kwargs):
        if argv[:3] == [sys.executable, '-m', 'lib.control.sessions'] and argv[3] == 'owner':
            argv = [sys.executable, str(Path(__file__).resolve()), 'owner', argv[4]]
        return real_popen(argv, **kwargs)
    # Only the test provider selection differs. Ownership launch, process groups,
    # persisted state, supervisor lock, polling, and reconciliation are real.
    with mock.patch('lib.control.sessions.subprocess.Popen', side_effect=launch):
        return run_supervisor(load_config(), json_output=True)


if __name__ == '__main__':
    mode = sys.argv[1]
    raise SystemExit(provider() if mode == 'provider' else owner(sys.argv[2]) if mode == 'owner' else supervisor())
