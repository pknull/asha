import os
from pathlib import Path
import sys
import tempfile
import unittest

from lib.control.session_harness import CodexTransport
from lib.control.store import StoreError


class CodexTransportTests(unittest.TestCase):
    def run_fixture(self, body):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "server.py"
            path.write_text("import sys,json,os\n" + body)
            transport = CodexTransport([sys.executable, str(path)], cwd=directory, env=dict(os.environ), timeout=5)
            transport.message_id = "message-1"
            return list(transport.events("Assignment")), transport

    def test_real_pipe_handshake_and_turn_without_a_model(self):
        events, transport = self.run_fixture('''
def read(): return json.loads(sys.stdin.readline())
def send(frame): print(json.dumps(frame),flush=True)
init=read()
assert init['method']=='initialize'
send({'id':init['id'],'result':{'userAgent':'fake'}})
assert read()['method']=='initialized'
start=read()
assert start['method']=='thread/start'
cwd=start['params']['cwd']
send({'id':start['id'],'result':{'thread':{'id':'native-1'},'cwd':cwd,
 'approvalPolicy':'untrusted','approvalsReviewer':'user','sandbox':{'type':'workspaceWrite','networkAccess':False}}})
turn=read()
assert turn['params']['input'][0]['text']=='Assignment'
assert turn['params']['clientUserMessageId']=='message-1'
send({'id':turn['id'],'result':{'turn':{'id':'turn-1','status':'inProgress','items':[]}}})
send({'method':'item/agentMessage/delta','params':{'threadId':'native-1','turnId':'turn-1','itemId':'text-1','delta':'Finished'}})
send({'method':'turn/completed','params':{'threadId':'native-1','turn':{'id':'turn-1','status':'completed','items':[]}}})
assert sys.stdin.read()==''
''')
        self.assertEqual([kind for kind, _ in events], ["initialized", "progress", "text", "completed"])
        self.assertFalse(transport.input_not_submitted)

    def test_exit_without_native_terminal_is_not_completion(self):
        with self.assertRaisesRegex(StoreError, "without a structured terminal"):
            self.run_fixture("sys.stdin.readline()\n")

    def test_malformed_protocol_is_not_terminal_text(self):
        with self.assertRaisesRegex(StoreError, "malformed structured"):
            self.run_fixture("sys.stdin.readline()\nprint('not JSON',flush=True)\n")

    def test_protocol_construction_failure_is_known_not_submitted(self):
        transport = CodexTransport(["unused"], cwd="/tmp", env={})
        with self.assertRaises(StoreError):
            list(transport.events("x" * (256 * 1024 + 1)))
        self.assertTrue(transport.input_not_submitted)
