import json
import os
import sys
import subprocess
import time
import tempfile
import unittest
from pathlib import Path

from lib.control.session_harness import ClaudeTransport, claude_argv, decode_claude
from lib.control.store import StoreError


class TransportTests(unittest.TestCase):
    def test_utility_does_not_override_native_settings_or_subagents(self):
        from lib.control.session_harness import codex_argv
        root = Path(__file__).resolve().parents[2]
        claude = claude_argv(root, native_settings=True)
        self.assertNotIn('--permission-mode', claude)
        self.assertNotIn('bypassPermissions', claude)
        codex = codex_argv(root, native_settings=True)
        self.assertNotIn('multi_agent', codex)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', codex)

    def transport(self, script, timeout=5):
        return ClaudeTransport([sys.executable, "-c", script], cwd="/tmp", env=dict(os.environ), timeout=timeout)

    def test_pipes_drain_while_large_input_and_stderr_are_written(self):
        script = "import sys,json; sys.stderr.write('x'*100000); sys.stderr.flush(); sys.stdin.read(); print(json.dumps({'type':'system','subtype':'init','session_id':'native'})); print(json.dumps({'type':'result','subtype':'success','result':'done'}))"
        events = list(self.transport(script).events("p" * 65536))
        self.assertEqual([k for k, _ in events], ["initialized", "completed"])

    def test_exit_zero_without_terminal_record_is_not_success(self):
        with self.assertRaisesRegex(StoreError, "without a structured terminal"):
            list(self.transport("import sys; sys.stdin.read()").events("hello"))

    def test_invalid_json_fails(self):
        with self.assertRaisesRegex(StoreError, "malformed"):
            list(self.transport("print('not-json')").events("hello"))

    def test_ambiguous_or_nonfinite_json_is_rejected(self):
        for raw in ('{"type":"result","subtype":"error","subtype":"success"}',
                    '{"type":"result","subtype":"success","total_cost_usd":NaN}',
                    '{"type":"result","subtype":"success","total_cost_usd":1e999}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(StoreError, "malformed"):
                list(self.transport(f"print({raw!r})").events("hello"))

    def test_partial_json_fails(self):
        with self.assertRaisesRegex(StoreError, "incomplete"):
            list(self.transport("import sys; sys.stdout.write('{')").events("hello"))

    def test_cancel_reaps_the_process(self):
        with self.assertRaisesRegex(StoreError, "cancelled"):
            list(self.transport("import time; time.sleep(60)").events("hello", cancelled=lambda: True))

    def test_unanswered_permission_has_its_own_bounded_wait(self):
        script = '''import json,sys
def send(value): print(json.dumps(value),flush=True)
initialize=json.loads(sys.stdin.readline())
send({'type':'control_response','response':{'subtype':'success','request_id':initialize['request_id']}})
json.loads(sys.stdin.readline())
send({'type':'control_request','request_id':'native-1','request':{'subtype':'can_use_tool','tool_name':'Write','input':{}}})
sys.stdin.read()
'''
        transport = self.transport(script, timeout=2)
        transport.structured = True
        transport.permission_timeout = 0.1
        transport.open_request = lambda *_: None
        transport.poll_responses = lambda: []
        with self.assertRaisesRegex(StoreError, "permission decision deadline"):
            list(transport.events("Assignment"))

    def test_permission_denials_are_explicit_failures(self):
        events = list(decode_claude({"type": "result", "subtype": "success", "permission_denials": [{"tool": "Bash"}]}))
        self.assertEqual(events[0][0], "failed")
        self.assertIn("permission", events[0][1]["reason"])

    def test_malformed_content_uses_the_transport_error_contract(self):
        for message in (None, {"content": [None]}, {"content": [{"type": "text", "text": 42}]}):
            with self.assertRaises(StoreError):
                list(decode_claude({"type": "assistant", "message": message}))

    def test_failed_custody_never_releases_provider_start(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "must-not-exist"
            transport = self.transport(f"from pathlib import Path; Path({str(target)!r}).touch()")
            def fail(_pid):
                raise StoreError("database unavailable")
            transport.on_spawn = fail
            with self.assertRaisesRegex(StoreError, "database unavailable"):
                list(transport.events("input"))
            self.assertFalse(target.exists())

    def test_no_consumption_claim_from_init_or_completion(self):
        events = list(decode_claude({"type": "system", "subtype": "init", "session_id": "n"}))
        events += list(decode_claude({"type": "result", "subtype": "success"}))
        self.assertNotIn("consumed", [k for k, _ in events])

    def test_resume_preserves_asha_launcher_without_bypassing_permissions(self):
        root = Path(__file__).resolve().parents[2]
        argv = claude_argv(root, "native-session")
        self.assertEqual(argv[:2], [str(root / "bin/asha"), "claude"])
        self.assertIn("--resume", argv)
        self.assertNotIn("bypassPermissions", argv)

    def test_owner_death_kills_provider_group(self):
        from lib.control.harness import process_identity
        from lib.control.session_store import process_live
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pids = root / "provider-pids.json"
            ready = root / "ready"
            provider = (
                "import os,sys,subprocess,json,time; from pathlib import Path; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                f"Path({str(pids)!r}).write_text(json.dumps([os.getpid(),child.pid])); "
                "print(json.dumps({'type':'system','subtype':'init','session_id':'native'}),flush=True); time.sleep(60)"
            )
            owner_code = (
                "import os,sys,time; from pathlib import Path; "
                "from lib.control.session_harness import ClaudeTransport; "
                f"stream=ClaudeTransport([sys.executable,'-c',{provider!r}],cwd={directory!r},env=dict(os.environ)).events('go'); "
                f"next(stream); Path({str(ready)!r}).touch(); time.sleep(60)"
            )
            owner = subprocess.Popen([sys.executable, "-c", owner_code], cwd=Path(__file__).resolve().parents[2])
            try:
                until = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < until:
                    time.sleep(0.02)
                self.assertTrue(ready.exists(), "provider never reached running state")
                processes = [(pid, process_identity(pid)) for pid in json.loads(pids.read_text())]
                owner.kill()
                owner.wait(timeout=3)
                until = time.monotonic() + 5
                while any(process_live(pid, identity) for pid, identity in processes) and time.monotonic() < until:
                    time.sleep(0.02)
                self.assertTrue(all(not process_live(pid, identity) for pid, identity in processes))
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
