"""Structured harness transport. Terminal output is never treated as input state."""
from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

from .store import StoreError


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate protocol key")
        result[key] = value
    return result


def _finite_number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite protocol number")
    return number


CAPABILITIES = {
    "claude": {"managed": True, "events": True, "resume": True,
               "tested_version": "2.1.266",
               "steer": False, "native_approval_response": True,
               "consumption_receipt": False, "transport": "stdio-jsonl"},
    "codex": {"managed": True, "events": True, "resume": True,
              "tested_version": "0.153.4", "steer": False,
              "native_approval_response": True, "consumption_receipt": False,
              "transport": "app-server-stdio", "actor_tools": True},
    "copilot": {"managed": False, "reason": "no verified managed adapter"},
    "opencode": {"managed": False, "reason": "no verified managed adapter"},
}


def claude_argv(root: Path, native_id=None, *, native_settings=False):
    launcher = root / "bin" / "asha"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise StoreError("Asha launcher unavailable")
    command = [str(launcher), "claude", "-p", "--output-format", "stream-json", "--verbose",
               "--input-format", "stream-json",
               *([] if native_settings else ["--permission-mode", "manual"]),
               "--permission-prompts", "host", "--permission-prompt-tool", "stdio"]
    if native_id:
        if not isinstance(native_id, str) or len(native_id) > 512 or native_id.startswith("-"):
            raise StoreError("invalid native session ID")
        command += ["--resume", native_id]
    return command


def decode_claude(value):
    if not isinstance(value, dict):
        raise StoreError("provider stream record must be an object")
    kind = value.get("type")
    from .provider_recovery import claude_status
    observation = claude_status(value)
    if observation is not None:
        yield "provider-status", observation
    if kind == "system" and value.get("subtype") == "init":
        yield "initialized", {"native_id": value.get("session_id")}
    elif kind == "assistant":
        if observation is not None:
            # Error-envelope bodies are diagnostic text, not model work. Keep
            # the typed failure even if this optional body is absent or malformed.
            return
        message = value.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            raise StoreError("provider assistant message has invalid content")
        for item in message["content"]:
            if not isinstance(item, dict):
                raise StoreError("provider content item must be an object")
            if item.get("type") == "text":
                content = item.get("text", "")
                if not isinstance(content, str):
                    raise StoreError("provider text must be a string")
                for start in range(0, len(content), 16000):
                    yield "text", {"text": content[start:start + 16000]}
            elif item.get("type") == "tool_use":
                yield "tool", {"tool_id": item.get("id"), "name": item.get("name")}
    elif kind == "result":
        denied = value.get("permission_denials") or []
        failed = bool(value.get("is_error")) or value.get("subtype") != "success" or bool(denied) or observation is not None
        yield "failed" if failed else "completed", {
            "reason": "native permission denied" if denied else value.get("subtype"),
            "summary": str(value.get("result", ""))[:16000],
            "summary_truncated": len(str(value.get("result", ""))) > 16000,
            "native_id": value.get("session_id"), "cost_usd": value.get("total_cost_usd"),
        }
    elif kind == "system":
        yield "progress", {"subtype": str(value.get("subtype", "unknown"))[:200]}


class JsonLineTransport:
    """Drain input, output and stderr concurrently, with bounded memory and lifetime."""
    def __init__(self, argv, *, cwd, env, timeout=1800, structured=None, permission_timeout=86400):
        self.argv, self.cwd, self.env, self.timeout = argv, cwd, env, timeout
        self.permission_timeout = permission_timeout
        self.on_spawn = lambda pid: None
        self.structured = "--input-format" in argv if structured is None else structured
        self.native_id = None
        self.message_id = None
        self.input_started = False
        self.open_request = self.cancel_request = self.poll_responses = self.response_submitted = None

    def events(self, prompt, *, cancelled=lambda: False):
        protocol = self.make_protocol(prompt) if self.structured else None
        self.protocol = protocol
        read_fd, release_fd = os.pipe()
        try:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("session_child.py")),
                                        str(read_fd), str(os.getpid()), *self.argv],
                                       cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True, pass_fds=(read_fd,))
        except BaseException:
            os.close(release_fd)
            raise
        finally:
            os.close(read_fd)
        selector = selectors.DefaultSelector()
        remaining = memoryview(prompt.encode())
        output = bytearray()
        stderr = bytearray()
        deadline = time.monotonic() + self.timeout
        previous_tick = time.monotonic()
        permission_wait = 0.0
        terminal = False
        exited_at = None
        try:
            self.on_spawn(process.pid)
            os.write(release_fd, b"1")
            os.close(release_fd)
            release_fd = None
            for stream, flag, name in ((process.stdin, selectors.EVENT_WRITE, "stdin"),
                                      (process.stdout, selectors.EVENT_READ, "stdout"),
                                      (process.stderr, selectors.EVENT_READ, "stderr")):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, flag, name)
            while selector.get_map():
                now = time.monotonic()
                if protocol and protocol.requests:
                    elapsed = now - previous_tick
                    deadline += elapsed
                    permission_wait += elapsed
                    if permission_wait >= self.permission_timeout:
                        raise StoreError("native permission decision deadline exceeded")
                previous_tick = now
                if cancelled() or now >= deadline:
                    raise StoreError("session cancelled or turn deadline exceeded")
                if protocol and not process.stdin.closed:
                    protocol.poll()
                    registered = process.stdin in [key.fileobj for key in selector.get_map().values()]
                    if protocol.outbound and not registered:
                        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                    elif not protocol.outbound and protocol.terminal:
                        if registered:
                            selector.unregister(process.stdin)
                        process.stdin.close()
                # Inspect without reaping: the reserved leader PID also fences
                # process-group cleanup when a descendant retains a pipe.
                if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                    exited_at = exited_at or time.monotonic()
                    if time.monotonic() - exited_at > 2:
                        raise StoreError("provider exited with descendants holding its stream open")
                for key, _ in selector.select(0.2):
                    stream, name = key.fileobj, key.data
                    if name == "stdin":
                        try:
                            chunk = protocol.chunk() if protocol else remaining[:4096]
                            written = os.write(stream.fileno(), chunk) if chunk else 0
                            if protocol and written:
                                protocol.advance(written)
                            elif not protocol:
                                self.input_started = self.input_started or written > 0
                                remaining = remaining[written:]
                        except BrokenPipeError:
                            if protocol:
                                raise StoreError("native control input closed before response submission")
                            remaining = memoryview(b"")
                        if (protocol is not None and not protocol.outbound) or (protocol is None and not remaining):
                            selector.unregister(stream)
                            if protocol is None or protocol.terminal:
                                stream.close()
                        continue
                    block = os.read(stream.fileno(), 65536)
                    if not block:
                        selector.unregister(stream)
                        stream.close()
                        if name == "stdout" and output.strip():
                            raise StoreError("provider stream ended with an incomplete JSON record")
                        continue
                    if name == "stderr":
                        stderr.extend(block)
                        del stderr[:-8192]
                        continue
                    output.extend(block)
                    while b"\n" in output:
                        line, _, tail = output.partition(b"\n")
                        output[:] = tail
                        if len(line) > 1024 * 1024:
                            raise StoreError("provider stream record exceeds limit")
                        if not line.strip():
                            continue
                        try:
                            value = json.loads(line, object_pairs_hook=_unique_object,
                                               parse_float=_finite_number, parse_constant=_finite_number)
                        except (ValueError, UnicodeError, RecursionError) as exc:
                            raise StoreError("provider emitted malformed structured output") from exc
                        for event, payload in (protocol.feed(value) if protocol else self.legacy_events(value)):
                            if terminal and event != "provider-status":
                                raise StoreError("provider emitted events after its terminal result")
                            terminal = terminal or event in {"completed", "failed"}
                            yield event, payload
                    if len(output) > 1024 * 1024:
                        raise StoreError("provider stream record exceeds limit")
            # Closing all streams is not necessarily process exit. Bound it and
            # retain the leader until the owned process group has been stopped.
            if protocol and not process.stdin.closed:
                process.stdin.close()
            if not terminal:
                raise StoreError("provider exited without a structured terminal result")
            while not os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                if cancelled() or time.monotonic() >= deadline:
                    raise StoreError("session cancelled or turn deadline exceeded")
                time.sleep(0.05)
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            status = process.wait(timeout=3)
            if status != 0:
                # Raw stderr may contain credentials; retain only its presence here.
                raise StoreError(f"provider exited {status}" + (" (stderr available to provider)" if stderr else ""))
            if not terminal:
                raise StoreError("provider exited without a structured terminal result")
        finally:
            if release_fd is not None:
                os.close(release_fd)
            selector.close()
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    until = time.monotonic() + 3
                    while time.monotonic() < until:
                        if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                            break
                        time.sleep(0.05)
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
                except ProcessLookupError:
                    process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()

    @property
    def input_not_submitted(self):
        protocol = getattr(self, "protocol", None)
        return (not self.input_started if protocol is None else
                getattr(protocol, "input_not_submitted", not protocol.initialized))

    def protocol_options(self):
        return {"native_id": self.native_id, "message_id": self.message_id,
            "open_request": self.open_request, "cancel_request": self.cancel_request,
            "poll_responses": self.poll_responses, "submitted": self.response_submitted}

    def make_protocol(self, prompt):
        raise NotImplementedError()

    def legacy_events(self, value):
        raise StoreError("this adapter has no unstructured mode")


class ClaudeTransport(JsonLineTransport):
    def make_protocol(self, prompt):
        from .claude_protocol import ClaudeProtocol
        return ClaudeProtocol(prompt, **self.protocol_options())

    def legacy_events(self, value):
        return decode_claude(value)


class CodexTransport(JsonLineTransport):
    def __init__(self, argv, **kwargs):
        if kwargs.get("structured") is False:
            raise StoreError("Codex requires the app-server structured transport")
        super().__init__(argv, **{**kwargs, "structured": True})

    def make_protocol(self, prompt):
        from .codex_protocol import CodexProtocol
        return CodexProtocol(prompt, cwd=str(self.cwd), actor=getattr(self, 'actor', None),
                             native_settings=getattr(self, 'native_settings', False), **self.protocol_options())


def codex_argv(root: Path, *, native_settings=False):
    launcher = root / "bin" / "asha"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise StoreError("Asha launcher unavailable")
    # Legacy coordinators use Asha's tracked launches. Plain utilities retain
    # the user's native subagent settings as well as native execution policy;
    # the worker profile controls Asha context injection in the launcher.
    return [str(launcher), "codex", "app-server", "--listen", "stdio://",
            *([] if native_settings else ["--disable", "multi_agent"])]
