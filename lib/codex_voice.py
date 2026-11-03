#!/usr/bin/env python3
"""Launch a native Codex TUI with a private, voice-aware app-server relay.

Realtime identity, dropped startup resume permissions, and launch-directory
session selection are adapted. Approval payloads pass unchanged.
There is no provider client, microphone access, or protocol payload logging here.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tomllib

# Native rust-v0.162.1 limits. The TUI's --remote client accepts frames and messages up to
# 128 MiB (app-server-client/src/remote.rs REMOTE_APP_SERVER_MAX_WEBSOCKET_MESSAGE_SIZE);
# the stdio app-server reads and writes lines without a length limit
# (app-server-transport/src/transport/stdio.rs). Each backend line is relayed as one frame,
# so the client limit binds. The native client writes each message as one unfragmented frame,
# and the native WebSocket listener accepts 64 MiB, so the same bound is no stricter upstream.
MAX_MESSAGE = 128 << 20

# Classes only: failure lines never carry payloads, tokens, or endpoints.
FAILURES = {
    "backend-message-limit": "a native app-server message exceeded the 128 MiB remote client limit",
    "frontend-message-limit": "a native TUI message exceeded the 128 MiB relay limit",
    "frontend-protocol": "the native TUI sent invalid WebSocket framing",
    "frontend-connection": "the native TUI connection failed",
    "frontend-disconnected": "the native TUI disconnected and did not exit",
    "backend-exited": "the native app-server exited",
    "backend-pipe": "the native app-server pipe failed",
}

# Inverse of the native TUI's terminal setup, in its restore_after_exit order
# (tui/src/tui.rs, tui/src/tui/alternate_screen.rs, tui/src/tui/keyboard_modes.rs):
# mouse reporting, alternate-screen keyboard flags, alternate scroll, the screen itself,
# main keyboard stack and modifyOtherKeys, bracketed paste, focus events, cursor.
TERMINAL_RESTORE = (b"\x1b[?1006l\x1b[?1015l\x1b[?1003l\x1b[?1002l\x1b[?1000l"
                    b"\x1b[<1u\x1b[?1007l\x1b[?1049l"
                    b"\x1b[<1u\x1b[<u\x1b[>4;0m"
                    b"\x1b[?2004l\x1b[?1004l\x1b[0 q\x1b[?25h")


class RelayFailure(Exception):
    """A relay failure, identified by its class name in FAILURES."""

    def __init__(self, failure: str):
        super().__init__(failure)
        self.failure = failure


class UnsupportedLaunch(ValueError):
    """An option whose native semantics cannot be retained by this relay."""


@dataclass
class LaunchPlan:
    frontend: list[str]
    backend: list[str]
    cwd: str
    resume_permissions: dict = field(default_factory=dict)
    explicit_cwd: bool = False


def same_directory(path: str, cwd: str) -> bool:
    """Require both native lexical and filesystem directory identity.

    Native CLI joins relative --cd to cwd before lexical normalization; it does
    not expand literal '~'. Alias paths can have different config ancestry.
    Checking real paths as well avoids accepting a symlink/.. traversal.
    """
    if not isinstance(path, str) or not path:
        return False
    candidate = os.path.join(cwd, path)
    try:
        return (os.path.normpath(candidate) == os.path.normpath(cwd)
                and os.path.realpath(candidate) == os.path.realpath(cwd))
    except (OSError, ValueError):
        return False


def launch_plan(args: list[str], cwd: str) -> LaunchPlan:
    """Keep CLI parsing in Codex; extract only the options the backend also needs.

    Native evidence: rust-v0.162.1 tui/app_server_session.rs forwards model,
    effort, cwd, and start/fork permissions, but strips resume permissions.
    cli/main.rs disallows --profile for app-server; remote mode also omits
    runtime_workspace_roots. These choices use an explicit ordinary-TUI fallback.
    """
    plan = LaunchPlan([], [], cwd)
    values = {"-c", "--config", "--enable", "--disable", "-m", "--model", "-s", "--sandbox",
              "-a", "--ask-for-approval", "-C", "--cd", "--local-provider"}
    simple = {"--strict-config", "--search", "--no-alt-screen", "--last", "--all",
              "--include-non-interactive", "--dangerously-bypass-hook-trust",
              "--dangerously-bypass-approvals-and-sandbox", "--approve-for-me"}
    permissions = {}
    raw_permissions = {}
    permission_selectors = 0
    permission_presets = 0
    unparsed_permissions = False
    index = 0
    first_positional = None
    session_target = False
    explicit_cwd = False
    selected_cwd = cwd
    while index < len(args):
        arg = args[index]
        index += 1
        if arg == "--":
            if first_positional in {"resume", "fork"} and index < len(args):
                session_target = True
            plan.frontend.extend([arg, *args[index:]])
            break
        if arg == "--no-daemon":
            continue
        option, equal, value = arg.partition("=")
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2 and arg[:2] in values:
            option, equal, value = arg[:2], "=", arg[2:].removeprefix("=")
        if option in {"-p", "--profile", "--add-dir", "--worktree", "--oss"} or arg.startswith("-p"):
            raise UnsupportedLaunch(f"{option} requires native local configuration")
        if option in values:
            original = [arg]
            if not equal:
                if index >= len(args):
                    raise UnsupportedLaunch(f"{option} requires a value")
                value = args[index]
                original.append(value)
                index += 1
            plan.frontend.extend(original)
            if option in {"-c", "--config", "--enable", "--disable"}:
                plan.backend.extend(original)
            if option in {"-C", "--cd"}:
                explicit_cwd = True
                selected_cwd = value
            if option in {"-s", "--sandbox"}:
                permission_selectors += 1
                permissions["sandbox"] = value
            elif option in {"-a", "--ask-for-approval"}:
                permission_selectors += 1
                permissions["approvalPolicy"] = value
            elif option in {"-c", "--config"}:
                key, _, raw = value.partition("=")
                permission_key = key.strip().split(".", 1)[0].strip("\"'")
                if permission_key in {"sandbox_mode", "approval_policy", "approvals_reviewer",
                                      "permissions", "default_permissions", "network", "sandbox_workspace_write"}:
                    permission_selectors += 1
                    if key.strip().split(".", 1)[0] != permission_key:
                        unparsed_permissions = True
                if key.strip() in {"sandbox_mode", "approval_policy", "approvals_reviewer"}:
                    try:
                        parsed = tomllib.loads("value=" + raw)["value"]
                    except tomllib.TOMLDecodeError:
                        unparsed_permissions = True
                        parsed = raw
                    raw_permissions[{"sandbox_mode":"sandbox", "approval_policy":"approvalPolicy",
                                     "approvals_reviewer":"approvalsReviewer"}[key.strip()]] = parsed
                elif key.strip().split(".")[0] in {"permissions", "default_permissions", "network", "sandbox_workspace_write"}:
                    # These need the native config loader plus its origin rules on resume.
                    # General launches retain them in the private backend verbatim.
                    raw_permissions["complex"] = True
            continue
        if option in {"-i", "--image"}:
            plan.frontend.append(arg)
            if not equal:
                while index < len(args) and not args[index].startswith("-"):
                    plan.frontend.append(args[index])
                    index += 1
            continue
        if option in simple:
            plan.frontend.append(arg)
            if option == "--last":
                session_target = True
            if option == "--strict-config":
                plan.backend.append(arg)
            elif option == "--dangerously-bypass-approvals-and-sandbox":
                permission_presets += 1
                permissions.update(sandbox="danger-full-access", approvalPolicy="never")
            elif option == "--approve-for-me":
                permission_presets += 1
                permissions.update(sandbox="workspace-write", approvalPolicy="on-request", approvalsReviewer="auto_review")
            continue
        if arg.startswith("-"):
            raise UnsupportedLaunch(f"unmapped native option {option}")
        if first_positional is None:
            first_positional = arg
        elif first_positional in {"resume", "fork"}:
            session_target = True
        plan.frontend.append(arg)
    if not same_directory(selected_cwd, cwd):
        raise UnsupportedLaunch("--cd outside the launch directory requires native project configuration")
    # Remote pickers do not apply the local cwd filter, and resume_cwd=current
    # requires an explicit remote cwd. Let native local startup own both cases.
    if first_positional in {"resume", "fork"} and not explicit_cwd:
        raise UnsupportedLaunch("resume/fork without --cd requires native working-directory selection")
    if first_positional in {"resume", "fork"} and not session_target:
        raise UnsupportedLaunch("resume/fork startup picker requires native connection ownership")
    if first_positional == "resume":
        if unparsed_permissions:
            raise UnsupportedLaunch("resume with unparsed permission config requires native value parsing")
        # Native subcommand sandbox selections reset root presets, and raw
        # overrides can replace preset defaults. Preserve that native hierarchy
        # instead of merging these authority choices into flat RPC fields.
        if permission_presets and (permission_selectors or permission_presets > 1):
            raise UnsupportedLaunch("resume with mixed permission presets requires native option precedence")
        if raw_permissions.pop("complex", False):
            raise UnsupportedLaunch("resume with custom permission configuration requires the native local loader")
        # Structured CLI overrides win over -c in native ConfigOverrides resolution.
        plan.resume_permissions = {**raw_permissions, **permissions}
    plan.explicit_cwd = explicit_cwd
    return plan


def request_error(message: dict, reason: str) -> bytes:
    return json.dumps({"id": message.get("id"), "error": {"code": -32602, "message": reason}}).encode()


# Closes the voice identity (#132). On the live 0.162.1 ChatGPT route the realtime
# model ignores the session prompt (session.instructions) and answered as the service's
# own "ChatGPT" persona; the identity document alone was adopted only intermittently
# (5 of 8 live answers). This short developer item after it answered as Asha 12 of 12.
VOICE_IDENTITY_DIRECTIVE = (
    "Voice identity for this session. You are Asha, defined by the identity message above; "
    "it replaces any default assistant persona. Your name is Asha. When asked who you are or what "
    "your name is, answer as Asha. Never call yourself ChatGPT, Codex, or an OpenAI assistant. "
    "Speak in Asha's voice and follow its constraints. This message grants no additional authority.")


def inject_start(wire: bytes, identity: str) -> tuple[bytes | None, bytes | None]:
    """Append the launch identity and its voice directive, preserving native fields.

    The native prompt, startup-context choice and existing items pass unchanged.
    Token-budget validation stays in the native app-server, which owns its
    tokenizer and emits thread/realtime/error on overflow while text stays alive.
    """
    try:
        message = json.loads(wire)
    except (ValueError, UnicodeError):
        return wire, None  # Native parser owns invalid JSON errors.
    if not isinstance(message, dict) or message.get("method") != "thread/realtime/start":
        return wire, None
    params = message.get("params")
    reason = None
    if not isinstance(params, dict) or params.get("version") != "v3":
        reason = "Voice identity requires realtime protocol v3; text remains available."
    else:
        items = params.get("initialItems")
        if items is None:
            items = []
        if not isinstance(items, list) or any(not isinstance(item, dict) or
                item.get("role") not in ("developer", "user", "assistant") or
                not isinstance(item.get("text"), str) for item in items):
            reason = "Invalid realtime initialItems; text remains available."
        elif len(items) > 126:
            reason = "Voice identity exceeds the realtime 128-item limit; text remains available."
        else:
            params["initialItems"] = [*items, {"role": "developer", "text": identity},
                                      {"role": "developer", "text": VOICE_IDENTITY_DIRECTIVE}]
    if reason:
        return None, request_error(message, reason)
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(), None


def adapt_request(wire: bytes, identity: str, plan: LaunchPlan) -> tuple[bytes | None, bytes | None]:
    if plan.resume_permissions:
        try:
            message = json.loads(wire)
        except (ValueError, UnicodeError):
            message = None
        if isinstance(message, dict) and message.get("method") == "thread/resume" and isinstance(message.get("params"), dict):
            message["params"].update(plan.resume_permissions)
            plan.resume_permissions = {}
            return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(), None
    return inject_start(wire, identity)


def check_capability(executable: str) -> dict:
    """Bounded local help/schema probes; no service, credentials, or audio used."""
    try:
        def probe(*args):
            return subprocess.run([executable, *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True).stdout.decode()
        help_text = probe("--help")
        if not all(flag in help_text for flag in ("--remote", "--remote-auth-token-env")):
            return {"supported": False, "reason": "native CLI lacks authenticated remote TUI support"}
        if "--stdio" not in probe("app-server", "--help"):
            return {"supported": False, "reason": "native CLI lacks stdio app-server support"}
        with tempfile.TemporaryDirectory(prefix="asha-codex-voice-schema-") as folder:
            probe("app-server", "generate-json-schema", "--experimental", "--out", folder)
            schema = json.loads((Path(folder) / "v2" / "ThreadRealtimeStartParams.json").read_text())
        definitions = schema["definitions"]
        item = definitions["ThreadRealtimeInitialItem"]["properties"]
        supported = (schema["properties"]["initialItems"]["items"]["$ref"].endswith("/ThreadRealtimeInitialItem")
            and "developer" in definitions["ConversationTextRole"]["enum"]
            and "v3" in definitions["RealtimeConversationVersion"]["enum"]
            and item["role"]["$ref"].endswith("/ConversationTextRole") and item["text"]["type"] == "string")
        return {"supported": supported, "reason": "native realtime v3 developer initialItems" if supported else "unsupported realtime startup schema"}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return {"supported": False, "reason": "native help or experimental schema probe failed"}


def read_exact(sock: socket.socket, size: int) -> bytes:
    buffer = bytearray(size)
    view, received = memoryview(buffer), 0
    while received < size:
        count = sock.recv_into(view[received:])
        if not count:
            raise EOFError
        received += count
    return bytes(buffer)


def unmask(payload: bytes, mask: bytes) -> bytes:
    key = (mask * (len(payload) // 4 + 1))[:len(payload)]
    return (int.from_bytes(payload, "little") ^ int.from_bytes(key, "little")).to_bytes(len(payload), "little")


def read_frame(sock: socket.socket) -> tuple[bool, int, bytes]:
    first, second = read_exact(sock, 2)
    final, opcode, size = bool(first & 128), first & 15, second & 127
    if first & 0x70 or not second & 128:
        raise RelayFailure("frontend-protocol")
    if size == 126:
        size = struct.unpack("!H", read_exact(sock, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", read_exact(sock, 8))[0]
    if opcode >= 8 and (not final or size > 125):
        raise RelayFailure("frontend-protocol")
    if size > MAX_MESSAGE:
        raise RelayFailure("frontend-message-limit")
    mask = read_exact(sock, 4)
    return final, opcode, unmask(read_exact(sock, size), mask)


def send_frame(sock: socket.socket, lock: threading.Lock, payload: bytes, opcode: int = 1):
    size = len(payload)
    head = bytes([128 | opcode])
    if size < 126:
        head += bytes([size])
    elif size < 65536:
        head += bytes([126]) + struct.pack("!H", size)
    else:
        head += bytes([127]) + struct.pack("!Q", size)
    with lock:
        sock.sendall(head)
        sock.sendall(payload)


def capture_terminal():
    """Save the input terminal's attributes before the native TUI switches it to raw mode."""
    try:
        return termios.tcgetattr(0) if os.isatty(0) else None
    except (OSError, termios.error):
        return None


def in_foreground(fd: int) -> bool:
    try:
        return os.tcgetpgrp(fd) == os.getpgrp()
    except OSError:
        return True  # Not a controlling terminal: no job-control stop applies.


def restore_terminal(saved):
    """Undo the native TUI's terminal modes after it died by a signal.

    The native TUI installs no SIGTERM/SIGHUP handler, so a stopped frontend never runs
    its restore_after_exit. A hung-up or background terminal is left alone.
    """
    try:
        if os.isatty(1) and in_foreground(1):
            os.write(1, TERMINAL_RESTORE)
    except OSError:
        pass
    if saved is not None:
        try:
            if in_foreground(0):
                termios.tcsetattr(0, termios.TCSANOW, saved)
        except (OSError, termios.error):
            pass


def stop_process(process: subprocess.Popen, *, group: bool = False):
    """Reap the owned process and stop its private group, including descendants."""
    def deliver(signum):
        try:
            if group:
                os.killpg(process.pid, signum)
            elif process.poll() is None:
                process.send_signal(signum)
        except ProcessLookupError:
            pass
    deliver(signal.SIGTERM)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        deliver(signal.SIGKILL)
        process.wait()
    if group:
        deliver(signal.SIGKILL)


@dataclass
class _VoiceClient:
    backend: subprocess.Popen
    plan: LaunchPlan
    socket: socket.socket | None = None
    disconnected: threading.Event = field(default_factory=threading.Event)
    failed: threading.Event = field(default_factory=threading.Event)
    failure: str | None = None
    close_lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
            if self.socket is not None:
                try:
                    self.socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            stop_process(self.backend, group=True)
            for stream in (self.backend.stdin, self.backend.stdout):
                try:
                    stream.close()
                except OSError:
                    pass


class VoiceBridge:
    def __init__(self, executable: str, identity: str, plan: LaunchPlan, env: dict):
        self.identity, self.plan = identity, plan
        self.command = [executable, *plan.backend, "app-server", "--stdio"]
        self.backend_env = dict(env)
        # Native stdio servers otherwise restore persisted remote-control state.
        self.backend_env["CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED"] = "1"
        self.token = secrets.token_urlsafe(32)
        self.connected = False
        self.claim_lock = threading.Lock()
        self.closing = False
        # The primary connection defines the session. Once it has ended, later connections
        # are native reconnects: their backend would contend for the primary's thread writer.
        self.ended = False
        self.connection = None
        self.disconnected = threading.Event()
        self.failed = threading.Event()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass  # Never log requests, headers, tokens, or protocol payloads.

            def do_GET(self):
                supplied = self.headers.get("Authorization", "")
                if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer " + owner.token).encode()):
                    self.send_error(401)
                    return
                try:
                    key = self.headers.get("Sec-WebSocket-Key", "")
                    valid_key = len(base64.b64decode(key, validate=True)) == 16
                except ValueError:
                    valid_key = False
                if (self.headers.get("Upgrade", "").lower() != "websocket"
                        or "upgrade" not in self.headers.get("Connection", "").lower().split(", ")
                        or self.headers.get("Sec-WebSocket-Version") != "13" or not valid_key):
                    self.send_error(400)
                    return
                with owner.claim_lock:
                    if owner.closing or owner.ended:
                        self.send_error(503)
                        return
                    if not owner.connected:
                        owner.connected = True
                        owner.connection = self.connection
                        client = owner.primary
                    elif not owner.primary_open():
                        # Native reconnect drops its stream first; the relay thread may not
                        # have read that EOF yet. End the session; start no backend.
                        owner.ended = True
                        self.send_error(503)
                        return
                    else:
                        # Native /resume starts a separate picker app-server. Its
                        # requests must have their own streams and no startup
                        # resume overrides from the primary invocation.
                        try:
                            client = _VoiceClient(owner.start_backend(), LaunchPlan(
                                list(owner.plan.frontend), list(owner.plan.backend), owner.plan.cwd))
                        except OSError:
                            self.send_error(503)
                            return
                        owner.clients.append(client)
                    client.socket = self.connection
                accept = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.connection.settimeout(None)
                owner.relay(client)
                self.close_connection = True

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        try:
            self.backend = self.start_backend()
        except BaseException:
            self.server.server_close()
            raise
        self.primary = _VoiceClient(self.backend, plan, disconnected=self.disconnected, failed=self.failed)
        self.clients = [self.primary]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval":0.05}, daemon=True)
        self.thread.start()
        self.url = f"ws://127.0.0.1:{self.server.server_port}"

    def start_backend(self):
        return subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None, env=self.backend_env, cwd=self.plan.cwd, start_new_session=True)

    @property
    def failure(self) -> str | None:
        return self.primary.failure

    def finish(self, client: _VoiceClient, failure: str | None = None):
        """Record how a connection ended; the first failure wins.

        Callers finish before shutting the socket, so the primary's end is recorded before
        the native frontend can observe the close and reconnect.
        """
        with self.claim_lock:
            if failure and client.failure is None:
                client.failure = failure
            if client is self.primary:
                self.ended = True
        if failure:
            client.failed.set()

    def primary_open(self) -> bool:
        """Whether the primary socket is still open, without consuming its input."""
        sock = self.primary.socket
        if sock is None:
            return False
        try:
            poller = select.poll()
            poller.register(sock, select.POLLIN)
            if not poller.poll(0):
                return True
            return bool(sock.recv(1, socket.MSG_PEEK))
        except (OSError, ValueError, TypeError):
            return False

    def relay(self, client: _VoiceClient):
        sock, backend = client.socket, client.backend
        write_lock = threading.Lock()
        backend_lock = threading.Lock()
        scope_lock = threading.Lock()
        scope_pending = {}
        # Set before backend stdin closes: later backend output or exit is teardown.
        frontend_done = threading.Event()

        def send(payload, opcode=1):
            try:
                send_frame(sock, write_lock, payload, opcode)
            except OSError as error:
                raise RelayFailure("frontend-connection") from error

        def write_backend(wire):
            # Both frontend requests and completed scope checks write this pipe.
            if b"\n" in wire or b"\r" in wire:
                try:
                    wire = json.dumps(json.loads(wire), ensure_ascii=False).encode()
                except ValueError as error:  # A raw line break would split the stdio message.
                    raise RelayFailure("frontend-protocol") from error
            try:
                with backend_lock:
                    backend.stdin.write(wire + b"\n")
                    backend.stdin.flush()
            except (OSError, ValueError) as error:
                raise RelayFailure("backend-pipe") from error

        def send_adapted(wire):
            wire, error = adapt_request(wire, self.identity, client.plan)
            if error is not None:
                send(error)
            else:
                write_backend(wire)

        scope_reason = ("Voice is scoped to the launch directory. Start ordinary native Codex "
                        "or launch Asha from the target directory to resume or fork this thread.")

        def from_frontend(wire):
            try:
                message = json.loads(wire)
            except (ValueError, UnicodeError):
                return send_adapted(wire)
            if not isinstance(message, dict):
                return send_adapted(wire)
            method, params = message.get("method"), message.get("params")
            if method == "thread/list" and isinstance(params, dict):
                params["cwd"] = client.plan.cwd
                return send_adapted(json.dumps(message, ensure_ascii=False).encode())
            if method not in {"thread/resume", "thread/fork"}:
                return send_adapted(wire)
            if (not isinstance(params, dict) or not isinstance(params.get("threadId"), str)
                    or not params["threadId"] or params.get("path") or params.get("history") is not None
                    or params.get("cwd") is not None and not same_directory(params["cwd"], client.plan.cwd)):
                return send(request_error(message, scope_reason))
            lookup_id = "asha-voice-scope-" + secrets.token_hex(24)
            with scope_lock:
                if len(scope_pending) >= 128:
                    return send(request_error(message, "Too many pending directory checks; text remains available."))
                scope_pending[lookup_id] = (wire, message)
            write_backend(json.dumps({"id":lookup_id,"method":"thread/read",
                "params":{"threadId":params["threadId"],"includeTurns":False}}).encode())

        def scope_response(line):
            # Checks are registered before their read is written, so an empty table means no
            # line can answer one: skip parsing large native results such as plugin/list.
            with scope_lock:
                if not scope_pending:
                    return False
            try:
                message = json.loads(line)
            except (ValueError, UnicodeError):
                return False
            if not isinstance(message, dict) or "method" in message or not isinstance(message.get("id"), str):
                return False
            with scope_lock:
                pending = scope_pending.pop(message["id"], None)
            if pending is None:
                return False
            wire, original = pending
            result = message.get("result")
            thread = result.get("thread") if isinstance(result, dict) else None
            if isinstance(thread, dict) and same_directory(thread.get("cwd"), client.plan.cwd):
                send_adapted(wire)
            elif isinstance(message.get("error"), dict):
                send(json.dumps({"id":original.get("id"),"error":message["error"]}).encode())
            else:
                send(request_error(original, scope_reason))
            return True

        def from_backend():
            failure = None
            try:
                while True:
                    try:
                        line = backend.stdout.readline(MAX_MESSAGE + 1)
                    except (OSError, ValueError) as error:
                        raise RelayFailure("backend-pipe") from error
                    if not line:
                        raise RelayFailure("backend-exited")
                    if line.endswith(b"\n"):
                        line = line[:-1]
                    elif len(line) > MAX_MESSAGE:
                        raise RelayFailure("backend-message-limit")
                    if not scope_response(line):
                        send(line)
            except RelayFailure as error:
                failure = error.failure
            finally:
                self.finish(client, None if frontend_done.is_set() else failure)
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        reader = threading.Thread(target=from_backend, daemon=True)
        reader.start()
        pending = None
        failure = None
        try:
            while True:
                final, opcode, data = read_frame(sock)
                if opcode == 8:
                    send(data, 8)
                    break
                if opcode == 9:
                    send(data, 10)
                    continue
                if opcode == 10:
                    continue
                if opcode == 1 and pending is None:
                    pending = bytearray()
                elif opcode != 0 or pending is None:
                    raise RelayFailure("frontend-protocol")
                pending.extend(data)
                if len(pending) > MAX_MESSAGE:
                    raise RelayFailure("frontend-message-limit")
                if not final:
                    continue
                wire = bytes(pending)
                pending = None
                from_frontend(wire)
        except EOFError:
            pass
        except RelayFailure as error:
            failure = error.failure
        except OSError:
            failure = "frontend-connection"
        finally:
            frontend_done.set()
            self.finish(client, failure)
            if failure:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            try:
                backend.stdin.close()
            except (OSError, ValueError):
                pass
            client.disconnected.set()
            if client is not self.primary:
                client.close()

    def close(self):
        with self.claim_lock:
            if self.closing:
                return
            self.closing = True
            clients = list(self.clients)
        self.server.shutdown()
        self.server.server_close()
        for client in clients:
            client.close()
        self.thread.join(timeout=2)


def normalized_exit(code: int) -> int:
    return code if code >= 0 else 128 - code


def run(executable: str, args: list[str], identity: str) -> int:
    try:
        plan = launch_plan(args, os.getcwd())
    except UnsupportedLaunch as error:
        print(f"asha: voice unavailable: {error}; retaining ordinary native launch", file=sys.stderr)
        os.execvpe(executable, [executable, *args], os.environ)
    capability = check_capability(executable)
    if not capability["supported"]:
        print(f"asha: voice unavailable: {capability['reason']}; retaining ordinary native launch", file=sys.stderr)
        os.execvpe(executable, [executable, *args], os.environ)
    bridge = VoiceBridge(executable, identity, plan, dict(os.environ))
    print("asha: voice is scoped to the launch directory; private backends stop on exit.", file=sys.stderr)
    frontend = None
    interrupted = []
    previous_handlers = {}
    failure = None
    terminal = capture_terminal()

    def terminate(signum, frame):
        interrupted.append(signum)
        if frontend is not None and frontend.poll() is None:
            frontend.send_signal(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous_handlers[signum] = signal.signal(signum, terminate)
        # Foreground terminal Ctrl-C belongs to the native TUI's handler.
        previous_handlers[signal.SIGINT] = signal.signal(signal.SIGINT, signal.SIG_IGN)
        token_env = "ASHA_VOICE_TOKEN_" + secrets.token_hex(8).upper()
        frontend_env = {**os.environ, token_env:bridge.token}
        # Native remote resume_cwd=current requires an explicit cwd. Bounded
        # voice sessions only select this directory, including in-session /resume.
        cwd_args = [] if plan.explicit_cwd else ["--cd", plan.cwd]
        frontend = subprocess.Popen([executable, "--remote", bridge.url, "--remote-auth-token-env", token_env,
                                     *cwd_args, *plan.frontend], env=frontend_env, cwd=plan.cwd)
        stopped = backend_first = False
        while frontend.poll() is None and not interrupted:
            if (bridge.ended or bridge.backend.poll() is not None
                    or bridge.failed.is_set() or bridge.disconnected.is_set()):
                backend_first = not bridge.ended and bridge.backend.poll() is not None
                # A normal TUI exit closes the socket just before the process exits. A native
                # reconnect loop never exits: the relay refuses reconnects, so stop it.
                try:
                    frontend.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    stop_process(frontend)
                    stopped = True
                break
            time.sleep(0.05)
        if interrupted:
            stop_process(frontend)
            return 128 + interrupted[0]
        # Classify before teardown: closing backends must not rewrite the cause.
        failure = bridge.failure or ("backend-exited" if backend_first else None)
        if failure is None and stopped:
            failure = "frontend-disconnected"
        if failure == "backend-exited":
            try:
                bridge.backend.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        backend_code = bridge.backend.poll()
        if backend_code not in (None, 0):
            return normalized_exit(backend_code)
        code = normalized_exit(frontend.returncode)
        if failure:
            return code if code and not stopped else 1
        return code
    finally:
        if frontend is not None and frontend.poll() is None:
            stop_process(frontend)
        if frontend is not None and frontend.returncode is not None and frontend.returncode < 0:
            restore_terminal(terminal)
        bridge.close()
        if failure:
            print(f"asha: voice relay failed ({failure}): {FAILURES[failure]}; "
                  "private voice backends stopped.", file=sys.stderr)
        else:
            print("asha: private voice backends stopped; the native reconnect URL has expired.", file=sys.stderr)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", metavar="EXECUTABLE")
    parser.add_argument("--identity", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.check:
        result = check_capability(args.check)
        print(json.dumps(result))
        return 0 if result["supported"] else 1
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not args.identity or not command:
        parser.error("run mode requires --identity FILE -- EXECUTABLE [ARGS...]")
    try:
        identity = args.identity.read_text(encoding="utf-8")
        if not identity.strip():
            raise ValueError("empty identity")
        return run(command[0], command[1:], identity)
    except (OSError, ValueError):
        # The native tools own their detailed diagnostics; never echo payloads.
        print("asha: voice launch failed; could not read identity or start the private native server", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
