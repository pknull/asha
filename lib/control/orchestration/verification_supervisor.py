"""Process-status and bounded-output shim for one verification command."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path


STATUS_PREFIX = "ASHA_VERIFICATION_PROCESS_V1:"


def _start_ticks(pid: int) -> int:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    fields = raw[raw.rfind(")") + 2 :].split()
    return int(fields[19])


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("short retained-output write")
        offset += written


def _open_output(path: str) -> int:
    if not Path(path).is_absolute():
        raise OSError("retained output path must be absolute")
    descriptor = os.open(
        path,
        os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size != 0
    ):
        os.close(descriptor)
        raise OSError("retained output destination identity is invalid")
    return descriptor


def _append_streamed(descriptor: int, retained: list[int], chunk: bytes, limit: int) -> None:
    remaining = limit - retained[0]
    if remaining <= 0:
        return
    piece = chunk[:remaining]
    _write_all(descriptor, piece)
    retained[0] += len(piece)
    os.fsync(descriptor)


def _bounded_output(
    stdout: bytearray,
    stderr: bytearray,
    stdout_total: int,
    stderr_total: int,
    limit: int,
) -> tuple[bytes, bool, int]:
    separator = b"\n--- stderr ---\n"
    original_bytes = stdout_total + len(separator) + stderr_total
    combined = bytes(stdout) + separator + bytes(stderr)
    truncated = original_bytes > limit
    if truncated:
        marker = (
            f"\n[truncated by Asha verification; original bytes={original_bytes}]\n"
        ).encode("ascii")
        combined = combined[:limit - len(marker)] + marker
    return combined, truncated, original_bytes


def _parse(argv: list[str]) -> tuple[str, int, float, list[str]]:
    if len(argv) < 7 or argv[0] != "--output" or argv[2] != "--limit":
        raise ValueError("verification supervisor arguments are invalid")
    if argv[4] != "--timeout" or argv[6] != "--" or len(argv) == 7:
        raise ValueError("verification supervisor command is missing")
    limit = int(argv[3])
    timeout = float(argv[5])
    if limit != 1024 * 1024 or timeout <= 0:
        raise ValueError("verification supervisor bounds are invalid")
    return argv[1], limit, timeout, argv[7:]


def _emit(status: dict[str, object]) -> None:
    raw = json.dumps(
        status, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    sys.stderr.write(f"\n{STATUS_PREFIX}{raw}\n")
    sys.stderr.flush()


def main(argv: list[str]) -> int:
    try:
        output_path, limit, timeout, command = _parse(argv)
        output_fd = _open_output(output_path)
    except (OSError, ValueError) as exc:
        _emit({
            "pid": None, "start_ticks": None, "pid_namespace": None,
            "returncode": None, "invocation_error": str(exc)[:1000],
            "timed_out": False, "output_truncated": False,
            "output_original_bytes": 0, "output_digest": None,
        })
        return 0

    prefixes = {"stdout": bytearray(), "stderr": bytearray()}
    totals = {"stdout": 0, "stderr": 0}
    retained = [0]
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    invocation_error: str | None = None
    pid: int | None = None
    start_ticks: int | None = None
    pid_namespace: str | None = None
    returncode: int | None = None
    selector = selectors.DefaultSelector()
    try:
        try:
            process = subprocess.Popen(
                command, shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            assert process.stdout is not None and process.stderr is not None
            pid = process.pid
            namespace = os.stat(f"/proc/{pid}/ns/pid")
            pid_namespace = f"{namespace.st_dev}:{namespace.st_ino}"
            start_ticks = _start_ticks(pid)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            deadline = time.monotonic() + timeout
            post_exit_drains = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 and process.poll() is None:
                    timed_out = True
                    process.kill()
                    process.wait()
                events = selector.select(0 if process.poll() is not None else min(remaining, 0.25))
                for key, _mask in events:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    stream = key.data
                    totals[stream] += len(chunk)
                    room = limit - len(prefixes[stream])
                    if room > 0:
                        prefixes[stream].extend(chunk[:room])
                    _append_streamed(output_fd, retained, chunk, limit)
                if process.poll() is not None:
                    post_exit_drains += 1
                    # A detached descendant may retain or continuously write
                    # the inherited pipes. Drain a bounded final window for
                    # the command, then let PID-namespace teardown kill every
                    # remaining descendant when this supervisor exits.
                    if not events or post_exit_drains >= 32:
                        break
            returncode = process.wait()
        except OSError as exc:
            invocation_error = str(exc)[:1000]
            diagnostic = f"verification invocation failed: {invocation_error}".encode(
                "utf-8", errors="replace",
            )
            prefixes["stderr"].extend(diagnostic[:limit])
            totals["stderr"] += len(diagnostic)

        output, truncated, original_bytes = _bounded_output(
            prefixes["stdout"], prefixes["stderr"],
            totals["stdout"], totals["stderr"], limit,
        )
        os.ftruncate(output_fd, 0)
        os.lseek(output_fd, 0, os.SEEK_SET)
        _write_all(output_fd, output)
        os.fsync(output_fd)
        status = {
            "pid": pid, "start_ticks": start_ticks,
            "pid_namespace": pid_namespace, "returncode": returncode,
            "invocation_error": invocation_error, "timed_out": timed_out,
            "output_truncated": truncated,
            "output_original_bytes": original_bytes,
            "output_digest": hashlib.sha256(output).hexdigest(),
        }
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        os.close(output_fd)
    _emit(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
