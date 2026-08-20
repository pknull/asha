"""Shared bounded, argv-only subprocess capture for Control adapters."""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


def bounded_process(
    argv: list[str], *, cwd: Path | None, limit: int,
    error_type: type[ValueError], deadline_seconds: float = 60,
    env: dict[str, str] | None = None,
    input_data: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Capture each pipe incrementally without allocating beyond ``limit``."""
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_data is not None else None,
            shell=False,
            env=env,
        )
    except OSError as exc:
        raise error_type(f"command invocation failed: {exc}") from exc
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    collected = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + deadline_seconds
    input_offset = 0
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        if input_data is not None:
            assert process.stdin is not None
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise error_type("command timed out")
            events = selector.select(min(remaining, 1.0))
            if not events and process.poll() is not None:
                events = [
                    (key, selectors.EVENT_READ)
                    for key in selector.get_map().values()
                ]
            for key, _ in events:
                if key.data == "stdin":
                    try:
                        written = os.write(
                            key.fileobj.fileno(), input_data[input_offset:input_offset + 65536],
                        )
                    except (BrokenPipeError, OSError) as exc:
                        raise error_type(f"command input failed: {exc}") from exc
                    input_offset += written
                    if input_offset >= len(input_data):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = collected[key.data]
                if len(target) + len(chunk) > limit:
                    raise error_type(
                        f"command {key.data} exceeded the bounded adapter limit"
                    )
                target.extend(chunk)
        wait_remaining = deadline - time.monotonic()
        if wait_remaining <= 0:
            raise error_type("command timed out")
        return (
            process.wait(timeout=wait_remaining),
            bytes(collected["stdout"]),
            bytes(collected["stderr"]),
        )
    except subprocess.TimeoutExpired as exc:
        raise error_type("command timed out") from exc
    except OSError as exc:
        raise error_type(f"command invocation failed: {exc}") from exc
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()


def capture_bytes(
    argv: list[str], *, cwd: Path | None, limit: int,
    runner: Callable[..., Any] | None, error_type: type[ValueError],
    deadline_seconds: float = 60,
    env: dict[str, str] | None = None,
    input_data: bytes | None = None,
) -> tuple[int, bytes, bytes]:
    """Run one exact argv through an injected or production bounded runner."""
    if runner is None:
        return bounded_process(
            argv, cwd=cwd, limit=limit, error_type=error_type,
            deadline_seconds=deadline_seconds,
            env=env,
            input_data=input_data,
        )
    try:
        kwargs: dict[str, Any] = {
            "cwd": str(cwd) if cwd is not None else None,
            "capture_output": True,
            "text": False,
            "timeout": deadline_seconds,
            "check": False,
            "shell": False,
        }
        if env is not None:
            kwargs["env"] = env
        if input_data is not None:
            kwargs["input"] = input_data
        result = runner(argv, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise error_type(f"command invocation failed: {exc}") from exc
    stdout = result.stdout or b""
    stderr = result.stderr or b""
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8", errors="replace")
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8", errors="replace")
    if len(stdout) > limit or len(stderr) > limit:
        raise error_type("command output exceeded the bounded adapter limit")
    return result.returncode, stdout, stderr


def checked_bytes(
    argv: list[str], *, cwd: Path | None, limit: int,
    runner: Callable[..., Any] | None, error_type: type[ValueError],
    env: dict[str, str] | None = None,
) -> bytes:
    """Return bounded stdout or raise a bounded adapter-specific failure."""
    returncode, stdout, stderr = capture_bytes(
        argv,
        cwd=cwd,
        limit=limit,
        runner=runner,
        error_type=error_type,
        env=env,
    )
    if returncode != 0:
        detail = stderr[:4096].decode("utf-8", errors="replace").strip()
        raise error_type(
            f"command failed ({returncode}): {detail or 'no diagnostic'}"
        )
    return stdout
