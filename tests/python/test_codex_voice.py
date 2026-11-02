"""Voice bootstrap contract: real local pipes/WebSockets, no provider or audio."""
import base64
import json
import os
from pathlib import Path
import signal
import time
import socket
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from lib import codex_voice as voice


class VoiceItemsTests(unittest.TestCase):
    # Removing the voice mutation must lose the developer identity.
    def test_appends_exact_unicode_identity_preserving_native_startup_fields(self):
        request = {"id": "start-α", "method": "thread/realtime/start", "params": {
            "threadId": "t", "version": "v3", "outputModality": "audio",
            "prompt": "native frontend", "includeStartupContext": True,
            "transport": {"type": "webrtc", "sdp": "native offer"},
            "initialItems": [{"role": "user", "text": "a prior fact"}]}}
        identity = "# Identity\nI am Asha — γνώση.\n"
        wire, error = voice.inject_start(json.dumps(request).encode(), identity)
        expected = json.loads(json.dumps(request))
        expected["params"]["initialItems"].append({"role": "developer", "text": identity})
        self.assertEqual(json.loads(wire), expected)
        self.assertIsNone(error)
        self.assertEqual(len(request["params"]["initialItems"]), 1)

    # Deduplicating starts by thread would omit identity after the first restart.
    def test_each_start_gets_one_item_without_accumulating_across_restarts(self):
        for rpc_id in (1, 2, 3):
            wire, error = voice.inject_start(json.dumps({"id": rpc_id,
                "method": "thread/realtime/start", "params": {"version": "v3"}}).encode(), "Asha")
            self.assertEqual(json.loads(wire)["params"]["initialItems"],
                             [{"role": "developer", "text": "Asha"}])
            self.assertIsNone(error)

    # Re-serializing unrelated RPC traffic would change approval replies or bytes.
    def test_nonvoice_requests_notifications_and_approval_replies_are_byte_preserved(self):
        for frame in (b'{ "id": 9, "result": {"decision":"decline"} }',
                      b'{"method":"turn/start","params":{"text":"secret"}}',
                      b'{"method":"thread/realtime/appendAudio","params":{"audio":"AA=="}}',
                      b'{"method":"thread/start","params":{"sandbox":"read-only"}}'):
            self.assertEqual(voice.inject_start(frame, "Asha"), (frame, None))

    # Forwarding unknown versions would silently launch voice without identity.
    def test_unsupported_versions_return_native_request_error_without_forwarding(self):
        for version in (None, "v1", "v2", "v4"):
            wire, error = voice.inject_start(json.dumps({"id": "7", "method": "thread/realtime/start",
                "params": {"version": version}}).encode(), "Asha")
            self.assertIsNone(wire)
            response = json.loads(error)
            self.assertEqual(response["id"], "7")
            self.assertEqual(response["error"]["code"], -32602)
            self.assertNotIn("Asha", response["error"]["message"])

    # Dropping existing items to fit a limit would destroy native conversation context.
    def test_count_overflow_is_an_error_not_truncation(self):
        wire, error = voice.inject_start(json.dumps({"id": 8, "method": "thread/realtime/start",
            "params": {"version": "v3", "initialItems": [{"role":"user","text":"x"}] * 128}}).encode(), "identity")
        self.assertIsNone(wire)
        self.assertEqual(json.loads(error)["error"]["code"], -32602)

    # Treating malformed items as absent would silently replace native data.
    def test_malformed_items_are_rejected_and_null_is_supported(self):
        for items in ({}, "bad", [None], [{"role":"tool","text":"bad"}], [{"role":"user","text":1}]):
            wire, error = voice.inject_start(json.dumps({"id": 1, "method":"thread/realtime/start",
                "params":{"version":"v3","initialItems":items}}).encode(), "identity")
            self.assertIsNone(wire)
            self.assertEqual(json.loads(error)["error"]["code"], -32602)
        wire, error = voice.inject_start(b'{"id":1,"method":"thread/realtime/start","params":{"version":"v3","initialItems":null}}', "identity")
        self.assertEqual(json.loads(wire)["params"]["initialItems"], [{"role":"developer","text":"identity"}])


class VoiceLaunchTests(unittest.TestCase):
    # Dropping raw configuration would change backend features, providers or permissions.
    def test_plan_forwards_config_to_both_processes_and_keeps_native_ui_flags(self):
        args = ["--no-daemon", "-c", 'model_reasoning_effort="high"', "--enable", "realtime_conversation",
                "--disable=multi_agent", "--model", "fixture-model", "--sandbox", "read-only",
                "--ask-for-approval", "never", "--cd", "/fixture/project", "-i", "one.png", "two.png",
                "--no-alt-screen", "--", "prompt --no-daemon"]
        plan = voice.launch_plan(args, "/fixture/project")
        self.assertEqual(plan.frontend, args[1:])
        self.assertEqual(plan.backend, ["-c", 'model_reasoning_effort="high"', "--enable", "realtime_conversation",
                                      "--disable=multi_agent"])
        self.assertEqual(plan.cwd, "/fixture/project")

    # Treating option values as commands would bypass or alter interactive prompts.
    def test_launch_parser_preserves_options_equals_short_values_and_prompt_separator(self):
        args = ["-mexec", "-cmodel_reasoning_effort=high", "--config=approval_policy=never", "--", "--remote"]
        plan = voice.launch_plan(args, "/work")
        self.assertEqual(plan.frontend, args)
        self.assertEqual(plan.backend, ["-cmodel_reasoning_effort=high", "--config=approval_policy=never"])

    # Unsupported remote semantics must not turn a constrained launch into a broader one.
    def test_unsupported_launch_choices_are_explicit(self):
        for args in (["--profile", "careful"], ["--add-dir", "/extra"], ["--worktree"],
                     ["--unknown-future-policy"]):
            with self.subTest(args=args), self.assertRaises(voice.UnsupportedLaunch):
                voice.launch_plan(args, "/work")

    # Remote resume/fork without explicit cwd loses native project filtering and resume_cwd semantics.
    def test_resume_and_fork_without_explicit_cwd_require_native_launch(self):
        for args in (["resume", "--last"], ["fork", "--last"],
                     ["resume", "--", "--cd"],
                     ["resume", "-c", 'developer_instructions="--cd /elsewhere"', "--last"],
                     ["resume", "--image=--cd", "--last"],
                     ["-m", "--cd", "resume", "--last"]):
            with self.subTest(args=args), self.assertRaises(voice.UnsupportedLaunch):
                voice.launch_plan(args, "/work")

    # An overly broad fallback would disable supported explicit-directory resume/fork launches.
    def test_explicit_cwd_options_retain_resume_and_fork_voice_support(self):
        for command in ("resume", "fork"):
            for options in (["--cd", "/work"], ["--cd=/work"], ["-C", "/work"], ["-C/work"], ["-C=/work"]):
                args = [command, "--last", *options]
                with self.subTest(args=args):
                    self.assertEqual(voice.launch_plan(args, "/work").frontend, args)
        self.assertEqual(voice.launch_plan(["--", "resume"], "/work").frontend, ["--", "resume"])

    # Remote startup must not override a different directory's native project policy.
    def test_cross_directory_cli_launch_requires_native_configuration(self):
        for args in (["--cd", "/other"], ["--cd=/other"], ["-C/other"], ["-C=/other"],
                     ["-C", "../other"], ["-C", "~"], ["-C", "missing"],
                     ["-C", "/work", "resume", "-C", "/other", "--last"]):
            with self.subTest(args=args), self.assertRaises(voice.UnsupportedLaunch):
                voice.launch_plan(args, "/work")

    # Relative same-directory spellings stay supported, while option values are never cwd flags.
    def test_same_directory_cli_and_literal_prompt_cwd_remain_supported(self):
        for value in ("/work", ".", "./", "/work/."):
            args = ["resume", "--last", "-C", value]
            self.assertEqual(voice.launch_plan(args, "/work").frontend, args)
        for args in (["--", "-C", "/other"], ["-m", "--cd", "prompt"], ["--image=/other"]):
            self.assertEqual(voice.launch_plan(args, "/work").frontend, args)

    # Symlink aliases may have different native config ancestry despite the same physical target.
    def test_symlink_directory_alias_requires_native_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            real = Path(folder) / "real"
            real.mkdir()
            link = Path(folder) / "alias"
            link.symlink_to(real)
            with self.assertRaises(voice.UnsupportedLaunch):
                voice.launch_plan(["-C", str(link)], str(real))
            foreign = Path(folder) / "foreign" / "nested"
            foreign.mkdir(parents=True)
            (real / "jump").symlink_to(foreign)
            with self.assertRaises(voice.UnsupportedLaunch):
                voice.launch_plan(["-C", "jump/.."], str(real))

    # Startup picker rotation disconnects the primary backend, so native owns picker-only startup.
    def test_startup_resume_and_fork_pickers_require_native_launch(self):
        for command in ("resume", "fork"):
            for tail in ([], ["--"], ["-m", "--last"], ["--image=--last"], ["-c", 'model="id"']):
                args = [command, "-C", "/work", *tail]
                with self.subTest(args=args), self.assertRaises(voice.UnsupportedLaunch):
                    voice.launch_plan(args, "/work")

    # Explicit targets do not rotate startup picker transports.
    def test_startup_resume_and_fork_direct_targets_retain_voice(self):
        for command in ("resume", "fork"):
            for tail in (["--last"], ["thread-id"], ["--", "thread-id"]):
                args = [command, "-C", "/work", *tail]
                self.assertEqual(voice.launch_plan(args, "/work").frontend, args)

    # Flattening root/subcommand presets can retain discarded bypass or auto-review authority.
    def test_resume_mixed_permission_presets_require_native_launch(self):
        cases = [
            ["--dangerously-bypass-approvals-and-sandbox", "resume", "-s", "read-only", "--last"],
            ["--approve-for-me", "resume", "-s", "read-only", "--last"],
            ["-a", "on-request", "resume", "--dangerously-bypass-approvals-and-sandbox", "--last"],
            ["--dangerously-bypass-approvals-and-sandbox", "resume", "--approve-for-me", "--last"],
            ["resume", "--approve-for-me", "-a=on-request", "--last"],
            ["--approve-for-me", "resume", "-c", 'sandbox_mode="read-only"', "--last"],
            ["--approve-for-me", "resume", "--config=approvals_reviewer=\"user\"", "--last"],
            ["--dangerously-bypass-approvals-and-sandbox", "resume", "-capproval_policy=on-request", "--last"],
            ["--approve-for-me", "resume", "-c", '\"approval_policy\"="on-request"', "--last"],
            ["--approve-for-me", "resume", "-c", "'sandbox_mode'='read-only'", "--last"],
            ["--approve-for-me", "resume", "-c", 'sandbox_workspace_write.network_access=false', "--last"],
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(voice.UnsupportedLaunch):
                voice.launch_plan([*args, "-C", "/work"], "/work")

    # Omitting resume restoration drops explicit native permission choices.
    def test_resume_restores_explicit_permissions_without_touching_approval_answers(self):
        plan = voice.launch_plan(["resume", "-C", "/work", "--sandbox", "read-only", "-a", "never", "thread"], "/work")
        wire, error = voice.adapt_request(b'{"id":2,"method":"thread/resume","params":{"threadId":"thread","model":"native"}}', "identity", plan)
        self.assertEqual(json.loads(wire), {"id":2,"method":"thread/resume", "params":{
            "threadId":"thread","model":"native","sandbox":"read-only","approvalPolicy":"never"}})
        approval = b'{ "id": 19, "result": {"decision":"decline"} }'
        self.assertEqual(voice.adapt_request(approval, "identity", plan), (approval, None))

    # Native CLI trims raw-string fallbacks; forwarding them as RPC fields can reject valid launches.
    def test_unparsed_resume_permission_values_require_native_launch(self):
        for setting in ('approval_policy= on-request ', 'sandbox_mode=read-only',
                        'approvals_reviewer= user ', 'approval_policy="on-request'):
            with self.subTest(setting=setting), self.assertRaises(voice.UnsupportedLaunch):
                voice.launch_plan(["resume", "-C", "/work", "--last", "-c", setting], "/work")

    # Quoted permission keys must not silently disappear from resume overrides.
    def test_quoted_resume_permission_keys_require_native_launch(self):
        for setting in ('"sandbox_mode"="read-only"', "'approval_policy'='on-request'",
                        '"approvals_reviewer"="user"', '"sandbox_workspace_write".network_access=false'):
            with self.subTest(setting=setting), self.assertRaises(voice.UnsupportedLaunch):
                voice.launch_plan(["resume", "-C", "/work", "--last", "-c", setting], "/work")

    # Parsed TOML permission strings remain safe to forward without guessing native fallback parsing.
    def test_parsed_resume_permission_strings_remain_supported(self):
        plan = voice.launch_plan(["resume", "-C", "/work", "--last", "-c", 'approval_policy= "on-request"  '], "/work")
        wire, error = voice.adapt_request(b'{"id":1,"method":"thread/resume","params":{}}', "identity", plan)
        self.assertEqual(json.loads(wire)["params"], {"approvalPolicy":"on-request"})

    # Reversing precedence would let raw config override explicit native CLI choices.
    def test_resume_structured_permission_flags_win_over_raw_config(self):
        plan = voice.launch_plan(["resume", "-C", "/work", "-s", "read-only", "-c", 'sandbox_mode="danger-full-access"',
                                  "-c", 'approval_policy="on-request"', "--last"], "/work")
        wire, error = voice.adapt_request(b'{"id":1,"method":"thread/resume","params":{}}', "identity", plan)
        self.assertEqual(json.loads(wire)["params"], {"sandbox":"read-only","approvalPolicy":"on-request"})

    # Unconditionally adapting thread/resume would alter native restored session settings.
    def test_resume_without_explicit_permissions_preserves_native_restore(self):
        plan = voice.launch_plan(["resume", "-C", "/work", "--last"], "/work")
        original = b'{ "id": 1, "method": "thread/resume", "params": {"threadId":"native"} }'
        self.assertEqual(voice.adapt_request(original, "identity", plan), (original, None))

    # Treating the equals separator as part of a short option value changes permissions.
    def test_attached_short_equals_values_preserve_resume_permissions(self):
        plan = voice.launch_plan(["resume", "-C", "/work", "-s=read-only", "-a=never", "--last"], "/work")
        wire, error = voice.adapt_request(b'{"id":1,"method":"thread/resume","params":{}}', "identity", plan)
        self.assertEqual(json.loads(wire)["params"], {"sandbox":"read-only", "approvalPolicy":"never"})

    # Searching every argument for resume would misclassify an ordinary prompt.
    def test_prompt_and_option_values_named_resume_do_not_restore_permissions(self):
        for args in (["-s", "read-only", "--", "resume"], ["-m", "resume", "-s", "read-only", "hello"]):
            plan = voice.launch_plan(args, "/work")
            original = b'{ "id": 1, "method": "thread/resume", "params": {} }'
            self.assertEqual(voice.adapt_request(original, "identity", plan), (original, None))

    # Reapplying launch overrides to a later switch would change saved thread permissions.
    def test_resume_permissions_are_only_restored_on_initial_bootstrap(self):
        plan = voice.launch_plan(["resume", "-C", "/work", "-s", "read-only", "--last"], "/work")
        first = b'{"id":1,"method":"thread/resume","params":{"threadId":"first"}}'
        later = b'{ "id":2,"method":"thread/resume","params":{"threadId":"later"} }'
        self.assertEqual(json.loads(voice.adapt_request(first, "identity", plan)[0])["params"]["sandbox"], "read-only")
        self.assertEqual(voice.adapt_request(later, "identity", plan), (later, None))


class NativeLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="asha-voice-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        (self.home / ".asha").mkdir()
        for name, content in (("soul", "SOUL — identity"), ("voice", "VOICE ώ"),
                              ("keeper", "KEEPER"), ("operation", "OPERATIONS ONLY")):
            (self.home / ".asha" / (name + ".md")).write_text(content)
        (self.home / ".codex/skills").mkdir(parents=True)
        (self.home / ".codex/config.toml").write_text("# untouched native config\n")
        (self.home / ".codex/skills/test-fixture").symlink_to(ROOT / "plugins/test")
        self.env = {"HOME":str(self.home), "PATH":os.environ["PATH"], "LANG":"C.UTF-8",
                    "ASHA_CODEX_CMD":str(ROOT / "tests/fixtures/codex-voice-native.py"),
                    "VOICE_FIXTURE_DIR":str(self.home), "ASHA_HUB_SESSION_ID":"isolated-launch"}
        (self.home / "scenario.json").write_text("[]")

    def launch(self, *args, **extra):
        return subprocess.run(["bash", str(ROOT / "bin/asha"), "codex", *args], env={**self.env, **extra},
            cwd=self.home, capture_output=True, text=True, timeout=15)

    def load(self, name):
        return json.loads((self.home / name).read_text())

    def connect_bridge(self, bridge):
        sock = socket.create_connection(("127.0.0.1", bridge.server.server_port), timeout=3)
        self.addCleanup(sock.close)
        sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: MDEyMzQ1Njc4OWFiY2RlZg==\r\n"
                      "Authorization: Bearer " + bridge.token + "\r\n\r\n").encode())
        response = b""
        while not response.endswith(b"\r\n\r\n"):
            response += self.socket_exact(sock, 1)
        self.assertIn(b"101 Switching", response)
        return sock

    @staticmethod
    def socket_exact(sock, length):
        result = b""
        while len(result) < length:
            part = sock.recv(length - len(result))
            if not part:
                raise EOFError
            result += part
        return result

    def send_rpc(self, sock, frame):
        wire = json.dumps(frame).encode()
        head = b"\x81"
        if len(wire) < 126:
            head += bytes([128 | len(wire)])
        else:
            head += b"\xfe" + struct.pack("!H", len(wire))
        mask = b"test"
        sock.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(wire)))

    def receive_rpc(self, sock):
        return json.loads(self.receive_wire(sock))

    def receive_wire(self, sock):
        opcode, length = self.socket_exact(sock, 2)
        self.assertEqual(opcode, 129)
        if length == 126:
            length = struct.unpack("!H", self.socket_exact(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.socket_exact(sock, 8))[0]
        return self.socket_exact(sock, length)

    def wait_for_backend_pid(self, previous):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                pid = self.load("backend.json")["pid"]
                if pid != previous:
                    return pid
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(0.01)
        self.fail("new private backend never started")

    # Bypassing the helper in bin/asha would leave the real TUI voice starts unmodified.
    def test_full_wrapper_adds_only_base_identity_and_keeps_thread_and_approval_frames(self):
        frames = ['{ "id": 1, "method":"thread/start", "params":{"model":"fixture","sandbox":"read-only"} }',
                  '{"id":2,"method":"thread/realtime/start","params":{"version":"v3","initialItems":[{"role":"user","text":"seed"}],"prompt":"native","includeStartupContext":true}}',
                  '{"id":3,"method":"fixture/approval","params":{}}',
                  '{ "id":"native-approval", "result":{"decision":"decline"} }',
                  '{"id":4,"method":"thread/realtime/start","params":{"version":"v3"}}']
        (self.home / "scenario.json").write_text(json.dumps(frames))
        (self.home / "fragment").touch()
        result = self.launch("--sandbox", "read-only", "-m", "fixture", "-c", 'model_reasoning_effort="high"', "--", "initial prompt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / "backend.json").exists(), "wrapper never started private voice backend")
        wire = (self.home / "wire.jsonl").read_text().splitlines()
        self.assertEqual(wire[0], frames[0])
        self.assertEqual(wire[3], frames[3])
        identity = (self.home / ".asha/cache/instructions.md").read_text()
        self.assertIn("SOUL — identity", identity)
        for index in (1, 4):
            self.assertEqual(json.loads(wire[index])["params"]["initialItems"][-1], {"role":"developer","text":identity})
            self.assertNotIn("OPERATIONS ONLY", json.loads(wire[index])["params"]["initialItems"][-1]["text"])
        self.assertEqual(json.loads(wire[1])["params"]["prompt"], "native")
        backend = self.load("backend.json")
        self.assertEqual(backend["session"], "isolated-launch")
        self.assertFalse(backend["token_present"])
        self.assertIn('model_reasoning_effort="high"', backend["args"])
        self.assertIn("OPERATIONS ONLY", " ".join(backend["args"]))
        self.assertEqual(self.load("results.json")[2], '{ "id": "native-approval", "method": "item/commandExecution/requestApproval", "params": {"command":"fixture"} }')
        self.assertEqual((self.home / ".codex/config.toml").read_text(), "# untouched native config\n")
        with self.assertRaises(ProcessLookupError):
            os.kill(backend["pid"], 0)

    # Dropping protocol failures or closing the relay would kill subsequent text requests.
    def test_unsupported_voice_version_returns_error_and_text_continues(self):
        (self.home / "scenario.json").write_text(json.dumps([
            '{"id":"bad","method":"thread/realtime/start","params":{"version":"v2"}}',
            '{"id":2,"method":"turn/start","params":{"threadId":"text-remains"}}']))
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        results = list(map(json.loads, self.load("results.json")))
        self.assertEqual(results[0]["error"]["code"], -32602)
        self.assertEqual(results[1]["result"]["echo"], {"threadId":"text-remains"})
        self.assertEqual(len((self.home / "wire.jsonl").read_text().splitlines()), 1)

    # A helper that swallows native failure would report false success.
    def test_native_frontend_exit_and_backend_crash_are_propagated(self):
        (self.home / "exit").write_text("37")
        self.assertEqual(self.launch().returncode, 37)
        (self.home / "exit").unlink()
        (self.home / "scenario.json").write_text('["{\\"id\\":1,\\"method\\":\\"fixture/crash\\"}"]')
        self.assertEqual(self.launch().returncode, 42)

    # A capability version guess could enable mutation on an incompatible protocol.
    def test_check_uses_generated_schema_and_rejects_missing_developer_role(self):
        command = [sys.executable, str(ROOT / "lib/codex_voice.py"), "--check", self.env["ASHA_CODEX_CMD"]]
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["supported"])
        (self.home / "unsupported-schema").touch()
        result = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(json.loads(result.stdout)["supported"])

    # Enabling the relay for workers or utility commands would violate persona isolation.
    def test_worker_remote_and_utilities_keep_ordinary_native_launch(self):
        for args, extra in (([], {"ASHA_SESSION_PROFILE":"worker", "ASHA_PERSONA":"1"}),
                            (["--remote", "ws://foreign:1234"], {}), (["exec", "prompt"], {}),
                            (["--help"], {}), (["--version"], {})):
            with self.subTest(args=args, extra=extra):
                # Explicit remote is a capture-only fixture path here.
                if args and args[0] == "--remote":
                    args = ["--remote=ws://foreign:1234"]
                result = self.launch(*args, **extra)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((self.home / "backend.json").exists())

    # Unsupported local CLI semantics must keep every original native argument.
    def test_old_cli_and_profile_fallback_warn_and_keep_native_arguments(self):
        for args, marker in ((["--profile", "careful", "--sandbox", "read-only"], None),
                             (["--add-dir", "../extra"], None), (["--worktree"], None),
                             (["--oss", "--local-provider", "ollama"], None),
                             (["--model", "fixture"], "old")):
            with self.subTest(args=args):
                if marker:
                    (self.home / marker).touch()
                result = self.launch(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("voice unavailable", result.stderr)
                captured = self.load("frontend.json")["args"]
                self.assertEqual(captured[-len(args):], args)
                self.assertFalse((self.home / "backend.json").exists())

    # Resume/fork directory fallback must reach native with the original CLI and configuration.
    def test_resume_and_fork_without_cwd_fall_back_with_unchanged_arguments(self):
        for command in ("resume", "fork"):
            args = ["--no-daemon", "-c", 'tui.resume_cwd="current"', command, "--last"]
            with self.subTest(command=command):
                result = self.launch(*args)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("voice unavailable", result.stderr)
                self.assertEqual(self.load("frontend.json")["args"][2:], args)
                self.assertFalse((self.home / "backend.json").exists())

    # The raw value must reach the native CLI exactly, with no invalid padded RPC override.
    def test_unparsed_resume_permission_falls_back_without_rewriting_raw_config(self):
        args = ["--no-daemon", "resume", "-C", str(self.home), "--last", "-c", "approval_policy= on-request "]
        result = self.launch(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("voice unavailable", result.stderr)
        self.assertEqual(self.load("frontend.json")["args"][2:], args)
        self.assertFalse((self.home / "backend.json").exists())

    # Cross-directory fallback must preserve every original argument for native config loading.
    def test_cross_directory_fallback_keeps_native_arguments(self):
        target = self.home / "other-project"
        target.mkdir()
        args = ["--no-daemon", "-C", str(target), "-s", "read-only", "--", "continue"]
        result = self.launch(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("voice unavailable", result.stderr)
        self.assertEqual(self.load("frontend.json")["args"][2:], args)
        self.assertFalse((self.home / "backend.json").exists())

    # A native startup picker must survive unchanged instead of being killed after selection.
    def test_startup_picker_fallback_preserves_native_arguments(self):
        args = ["--no-daemon", "resume", "-C", str(self.home), "--all"]
        result = self.launch(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("voice unavailable", result.stderr)
        self.assertEqual(self.load("frontend.json")["args"][2:], args)
        self.assertFalse((self.home / "backend.json").exists())

    # Fallback must forward the actual caller choices without converting them into weaker RPC overrides.
    def test_mixed_resume_presets_fall_back_with_every_native_argument_unchanged(self):
        args = ["--no-daemon", "-C", str(self.home), "--dangerously-bypass-approvals-and-sandbox",
                "resume", "-s", "read-only", "--last"]
        result = self.launch(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("voice unavailable", result.stderr)
        self.assertEqual(self.load("frontend.json")["args"][2:], args)
        self.assertFalse((self.home / "backend.json").exists())

    # A blanket resume fallback would unnecessarily disable the supported constrained launch.
    def test_simple_constrained_resume_keeps_voice_relay_and_explicit_permissions(self):
        (self.home / "scenario.json").write_text(json.dumps([
            '{"id":1,"method":"thread/resume","params":{"threadId":"native-thread"}}']))
        result = self.launch("--no-daemon", "resume", "-C", str(self.home), "-s", "read-only", "-a", "on-request", "--last")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("voice unavailable", result.stderr)
        self.assertTrue((self.home / "backend.json").exists())
        self.assertEqual(json.loads(self.load("results.json")[0])["result"]["echo"], {
            "threadId":"native-thread", "sandbox":"read-only", "approvalPolicy":"on-request"})

    # A missing signal handler would strand the dedicated app-server and its child.
    def test_signal_terminates_frontend_backend_and_backend_children(self):
        (self.home / "scenario.json").write_text(json.dumps(['{"id":1,"method":"fixture/hang"}']))
        process = subprocess.Popen(["bash", str(ROOT / "bin/asha"), "codex"], env=self.env, cwd=self.home,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while not (self.home / "child.json").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.025)
        self.assertTrue((self.home / "child.json").exists())
        child = self.load("child.json")
        def cleanup_owned():
            for pid in (self.load("backend.json")["pid"], self.load("frontend.json")["pid"], child):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(cleanup_owned)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=7)
        self.assertEqual(process.returncode, 143, stderr)
        for pid in (self.load("backend.json")["pid"], self.load("frontend.json")["pid"], child):
            status = Path(f"/proc/{pid}/stat")
            self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z", f"owned child {pid} survived")

    # Missing scope checks would let native remote overrides replace another project's policy.
    def test_primary_and_auxiliary_resumes_and_forks_check_native_target_directory(self):
        bridge = voice.VoiceBridge(self.env["ASHA_CODEX_CMD"], "identity",
            voice.LaunchPlan([], [], str(self.home), {"sandbox":"read-only"}), self.env)
        self.addCleanup(bridge.close)
        for client_index in range(2):
            sock = self.connect_bridge(bridge)
            for method in ("thread/resume", "thread/fork"):
                denied = {"id":"foreign-id", "method":method, "params":{"threadId":"foreign", "cwd":str(self.home)}}
                self.send_rpc(sock, denied)
                result = self.receive_rpc(sock)
                self.assertEqual(result["id"], "foreign-id")
                self.assertIn("error", result)
                self.assertIn("ordinary native", result["error"]["message"])
                allowed = {"id":17, "method":method, "params":{"threadId":"local"}}
                self.send_rpc(sock, allowed)
                expected = {"threadId":"local"}
                if client_index == 0 and method == "thread/resume":
                    expected["sandbox"] = "read-only"
                self.assertEqual(self.receive_rpc(sock), {"id":17,"result":{"echo":expected,"answer":None}})
            self.send_rpc(sock, {"id":18,"method":"turn/start","params":{"threadId":"local"}})
            self.assertEqual(self.receive_rpc(sock)["id"], 18)
        wires = [json.loads(line) for line in (self.home / "wire.jsonl").read_text().splitlines()]
        self.assertFalse(any(w.get("method") in {"thread/resume", "thread/fork"}
            and w["params"]["threadId"] == "foreign" for w in wires))
        reads = [w for w in wires if w.get("method") == "thread/read"]
        self.assertEqual(len(reads), 8)
        self.assertTrue(all(isinstance(w["id"], str) and w["id"] != "foreign-id" for w in reads))
        self.assertTrue(all(w["params"]["includeTurns"] is False for w in reads))

    # Null or foreign list filters must not expose cross-directory choices to remote pickers.
    def test_primary_and_auxiliary_thread_lists_are_scoped_to_launch_directory(self):
        bridge = voice.VoiceBridge(self.env["ASHA_CODEX_CMD"], "identity",
            voice.LaunchPlan([], [], str(self.home)), self.env)
        self.addCleanup(bridge.close)
        for index in range(2):
            sock = self.connect_bridge(bridge)
            for requested in (None, "/another"):
                self.send_rpc(sock, {"id":1,"method":"thread/list", "params":{
                    "cwd":requested,"cursor":"cursor","limit":20,"archived":False}})
                self.assertEqual(self.receive_rpc(sock)["result"]["echo"], {
                    "cwd":str(self.home),"cursor":"cursor","limit":20,"archived":False})

    # Failed or malformed metadata must never authorize a resume; internal ids stay private.
    def test_resume_metadata_failure_is_reported_under_original_id_and_text_survives(self):
        bridge = voice.VoiceBridge(self.env["ASHA_CODEX_CMD"], "identity",
            voice.LaunchPlan([], [], str(self.home)), self.env)
        self.addCleanup(bridge.close)
        sock = self.connect_bridge(bridge)
        for target in ("missing", "malformed"):
            self.send_rpc(sock, {"id":"client-id","method":"thread/resume","params":{"threadId":target}})
            response = self.receive_rpc(sock)
            self.assertEqual(response["id"], "client-id")
            self.assertIn("error", response)
        self.send_rpc(sock, {"id":9,"method":"turn/start","params":{"threadId":"local"}})
        self.assertEqual(self.receive_rpc(sock)["id"], 9)

    # Pending metadata must be correlated by full id, preserving native events and approval traffic.
    def test_out_of_order_scope_checks_preserve_events_approvals_and_unrelated_string_ids(self):
        bridge = voice.VoiceBridge(self.env["ASHA_CODEX_CMD"], "identity",
            voice.LaunchPlan([], [], str(self.home)), self.env)
        self.addCleanup(bridge.close)
        sock = self.connect_bridge(bridge)
        self.send_rpc(sock, {"id":1,"method":"thread/resume","params":{"threadId":"hold-local"}})
        self.send_rpc(sock, {"id":2,"method":"thread/fork","params":{"threadId":"hold-foreign"}})
        self.send_rpc(sock, {"id":3,"method":"fixture/release"})
        frames = [self.receive_wire(sock) for _ in range(6)]
        self.assertIn(b'{ "method":"fixture/progress", "params":{"unchanged":true} }', frames)
        self.assertIn(b'{ "id":"asha-voice-scope-unrelated", "result":{"unchanged":true} }', frames)
        messages = [json.loads(w) for w in frames]
        approval = next(m for m in messages if m.get("method") == "item/commandExecution/requestApproval")
        responses = {m["id"]:m for m in messages if "id" in m and "method" not in m}
        self.assertEqual(set(responses), {1,2,3,"asha-voice-scope-unrelated"})
        self.assertEqual(responses[1]["result"]["echo"], {"threadId":"hold-local"})
        self.assertIn("error", responses[2])
        self.send_rpc(sock, {"id":approval["id"],"result":{"decision":"decline"}})
        self.assertEqual(self.receive_rpc(sock)["result"]["answer"], {"decision":"decline"})

    # A fresh native TUI needs explicit remote cwd for same-directory resume_cwd=current switching.
    def test_fresh_launch_sets_same_directory_cwd_before_prompt_separator_and_explains_lifetime(self):
        args = ["-c", 'tui.resume_cwd="current"', "--", "prompt --cd /foreign"]
        result = self.launch(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        frontend = self.load("frontend.json")["args"]
        self.assertIn("--cd", frontend)
        self.assertEqual(frontend[frontend.index("--cd") + 1], str(self.home))
        self.assertLess(frontend.index("--cd"), frontend.index("--"))
        self.assertEqual(frontend[-len(args):], args)
        self.assertIn("scoped to the launch directory", result.stderr)
        self.assertIn("reconnect URL has expired", result.stderr)
        self.assertNotIn("ws://", result.stderr)

    # Sharing or rejecting the picker connection breaks native /resume and mixes response streams.
    def test_auxiliary_connections_have_private_backends_and_leave_primary_alive(self):
        plan = voice.launch_plan(["resume", "-C", str(self.home), "-s", "read-only", "--last"], str(self.home))
        bridge = voice.VoiceBridge(self.env["ASHA_CODEX_CMD"], "identity", plan, self.env)
        self.addCleanup(bridge.close)
        primary = self.connect_bridge(bridge)
        primary_pid = self.wait_for_backend_pid(None)
        picker = self.connect_bridge(bridge)
        picker_pid = self.wait_for_backend_pid(primary_pid)
        self.assertNotEqual(primary_pid, picker_pid)
        # The auxiliary connection must neither consume nor inherit startup permission overrides.
        self.send_rpc(picker, {"id":1,"method":"thread/resume","params":{"threadId":"picker"}})
        self.send_rpc(primary, {"id":1,"method":"thread/resume","params":{"threadId":"primary"}})
        self.assertEqual(self.receive_rpc(picker)["result"]["echo"], {"threadId":"picker"})
        self.assertEqual(self.receive_rpc(primary)["result"]["echo"], {"threadId":"primary","sandbox":"read-only"})
        self.send_rpc(picker, {"id":2,"method":"fixture/crash"})
        with self.assertRaises(EOFError):
            self.receive_rpc(picker)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(picker_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            self.fail("closed picker backend was not reaped")
        self.assertFalse(bridge.disconnected.is_set())
        self.assertFalse(bridge.failed.is_set())
        self.assertIsNone(bridge.backend.poll())
        self.send_rpc(primary, {"id":2,"method":"turn/start","params":{"threadId":"still-alive"}})
        self.assertEqual(self.receive_rpc(primary)["result"]["echo"], {"threadId":"still-alive"})
        another_picker = self.connect_bridge(bridge)
        another_pid = self.wait_for_backend_pid(picker_pid)
        self.assertNotIn(another_pid, (primary_pid, picker_pid))
        self.send_rpc(another_picker, {"id":3,"method":"thread/realtime/start","params":{"version":"v3"}})
        self.assertEqual(self.receive_rpc(another_picker)["result"]["echo"]["initialItems"],
                         [{"role":"developer","text":"identity"}])
        bridge.close()
        for pid in (primary_pid, picker_pid, another_pid):
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    # An authenticated endpoint must reject foreign and malformed tokens before claiming its one client.
    def test_authentication_rejects_foreign_clients_and_invalid_handshake(self):
        bridge = voice.VoiceBridge(self.env["ASHA_CODEX_CMD"], "identity", voice.launch_plan([], str(self.home)), self.env)
        self.addCleanup(bridge.close)
        def handshake(token, key="MDEyMzQ1Njc4OWFiY2RlZg=="):
            sock = socket.create_connection(("127.0.0.1", bridge.server.server_port), timeout=2)
            self.addCleanup(sock.close)
            sock.sendall(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + key + "\r\nAuthorization: " + token + "\r\n\r\n").encode("latin-1"))
            return sock.recv(4096)
        self.assertIn(b"401", handshake("Bearer foreign"))
        self.assertIn(b"401", handshake("Bearer \xe9"))
        self.assertIn(b"400", handshake("Bearer " + bridge.token, "invalid"))
        self.assertFalse(bridge.connected)
        self.assertIn(b"101", handshake("Bearer " + bridge.token))
        self.assertIn(b"101", handshake("Bearer " + bridge.token))


if __name__ == "__main__":
    unittest.main()
