"""flock-owned process loop and CLI for the orchestration supervisor."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Mapping, Sequence

from ..harness import HarnessError, process_identity, verify_process
from ..store import (
    StoreError, TaskStore, _close_quietly, _directory_fd, _managed_start,
    _open_existing_file, _validate_open_file,
)
from .cli import reconcile_one_initiative
from .config import OrchestrationConfig, OrchestrationConfigError, load_config
from .ingestion import ingest_pending_results
from .store import InitiativeStore
from .supervisor import tick


MAX_STATUS_BYTES = 16 * 1024
_POLL_SECONDS = 1.0
_STATUS_KEYS = frozenset({
    "pid", "process_identity", "started_at", "last_tick_at",
    "last_tick_summary",
})


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("supervisor timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds",
    ).replace("+00:00", "Z")


def _parse_timestamp(value: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z") or len(value) > 40:
        raise ValueError("supervisor timestamp is invalid")
    datetime.fromisoformat(value[:-1] + "+00:00")


def _exception_message(exc: BaseException) -> str:
    detail = "".join(character if character.isprintable() else "?" for character in str(exc))
    return (detail or type(exc).__name__)[:450]


def supervisor_lock_path(config: OrchestrationConfig) -> Path:
    return config.control.tasks_dir.parent / "supervisor.lock"


def status_path(config: OrchestrationConfig) -> Path:
    return config.control.tasks_dir.parent / "supervisor.json"


def _control_root(config: OrchestrationConfig) -> tuple[Path, int]:
    root = config.control.tasks_dir.parent
    return root, _managed_start(root, ("state", "control"))


@contextmanager
def _exclusive_lock(config: OrchestrationConfig) -> Iterator[bool]:
    root, managed_start = _control_root(config)
    with _directory_fd(root, create=True, managed_start=managed_start) as directory_fd:
        if directory_fd is None:
            raise StoreError("failed to create supervisor state directory")
        flags = (
            os.O_RDWR | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )
        created = False
        try:
            fd = os.open(
                "supervisor.lock", flags | os.O_CREAT | os.O_EXCL,
                0o600, dir_fd=directory_fd,
            )
            created = True
        except FileExistsError:
            fd = os.open("supervisor.lock", flags, dir_fd=directory_fd)
        locked = False
        try:
            if created:
                os.fchmod(fd, 0o600)
                os.fsync(fd)
                os.fsync(directory_fd)
            _validate_open_file(fd, "supervisor lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            _close_quietly(fd)


def _lock_held(config: OrchestrationConfig) -> bool:
    root, managed_start = _control_root(config)
    with _directory_fd(root, create=False, managed_start=managed_start) as directory_fd:
        if directory_fd is None:
            return False
        try:
            fd = os.open(
                "supervisor.lock", os.O_RDWR | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return False
        try:
            _validate_open_file(fd, "supervisor lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            _close_quietly(fd)


def _validate_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _STATUS_KEYS:
        raise ValueError("supervisor status has an invalid key set")
    if isinstance(value["pid"], bool) or not isinstance(value["pid"], int) or value["pid"] <= 0:
        raise ValueError("supervisor status pid is invalid")
    identity = value["process_identity"]
    if not isinstance(identity, str) or not identity or len(identity) > 200:
        raise ValueError("supervisor status process identity is invalid")
    _parse_timestamp(value["started_at"])
    if value["last_tick_at"] is not None:
        _parse_timestamp(value["last_tick_at"])
    if value["last_tick_summary"] is not None and not isinstance(
        value["last_tick_summary"], dict
    ):
        raise ValueError("supervisor last tick summary is invalid")
    return value


def _read_status(config: OrchestrationConfig) -> dict[str, Any] | None:
    root, managed_start = _control_root(config)
    with _directory_fd(root, create=False, managed_start=managed_start) as directory_fd:
        if directory_fd is None:
            return None
        try:
            fd = _open_existing_file(directory_fd, "supervisor.json", "supervisor status")
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(fd)
            if metadata.st_size > MAX_STATUS_BYTES:
                raise ValueError(f"supervisor status exceeds {MAX_STATUS_BYTES} bytes")
            chunks: list[bytes] = []
            remaining = MAX_STATUS_BYTES + 1
            while remaining:
                chunk = os.read(fd, min(4096, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            _close_quietly(fd)
    if len(raw) > MAX_STATUS_BYTES:
        raise ValueError(f"supervisor status exceeds {MAX_STATUS_BYTES} bytes")
    try:
        return _validate_status(json.loads(raw.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"cannot read supervisor status: {exc}") from exc


def _write_status(config: OrchestrationConfig, value: Mapping[str, Any]) -> None:
    raw = json.dumps(
        _validate_status(dict(value)), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8") + b"\n"
    if len(raw) > MAX_STATUS_BYTES:
        raise ValueError(f"supervisor status exceeds {MAX_STATUS_BYTES} bytes")
    root, managed_start = _control_root(config)
    temporary = f".supervisor.json.tmp.{secrets.token_hex(8)}"
    with _directory_fd(root, create=True, managed_start=managed_start) as directory_fd:
        if directory_fd is None:
            raise StoreError("failed to create supervisor state directory")
        fd = -1
        try:
            fd = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600, dir_fd=directory_fd,
            )
            os.fchmod(fd, 0o600)
            offset = 0
            while offset < len(raw):
                written = os.write(fd, raw[offset:])
                if written <= 0:
                    raise OSError(errno.EIO, "short supervisor status write")
                offset += written
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(
                temporary, "supervisor.json",
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            _close_quietly(fd)
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _default_dependencies(config: OrchestrationConfig):
    catalog = InitiativeStore(config)
    return SimpleNamespace(
        store_factory=lambda _initiative_id: InitiativeStore(config),
        control_store=TaskStore(config.control),
        now=lambda: datetime.now(timezone.utc),
        reconcile=reconcile_one_initiative,
        ingest=ingest_pending_results,
        list_initiatives=catalog.list_initiatives,
    )


def _snapshot_marker(config: OrchestrationConfig) -> tuple[int, int] | None:
    try:
        metadata = (config.control.runtime_dir / "events").stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return metadata.st_ino, metadata.st_mtime_ns


def _emit(payload: Mapping[str, Any], json_output: bool) -> None:
    if json_output:
        print(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ), flush=True)
    else:
        print(f"asha control supervisor: {payload['message']}", flush=True)


def run_supervisor(
    config: OrchestrationConfig, *, deps=None, json_output: bool = False,
) -> int:
    with _exclusive_lock(config) as acquired:
        if not acquired:
            _emit({"running": True, "message": "already running"}, json_output)
            return 0
        pid = os.getpid()
        identity = process_identity(pid)
        if identity is None:
            raise HarnessError("supervisor process identity disappeared")
        retained: dict[str, Any] = {
            "pid": pid, "process_identity": identity,
            "started_at": _timestamp(datetime.now(timezone.utc)),
            "last_tick_at": None, "last_tick_summary": None,
        }
        _write_status(config, retained)
        _emit({"running": True, "pid": pid, "message": "running"}, json_output)
        stopping = False

        def request_stop(_signum, _frame):
            nonlocal stopping
            stopping = True

        prior = {
            signum: signal.signal(signum, request_stop)
            for signum in (signal.SIGTERM, signal.SIGINT)
        }
        dependencies = deps or _default_dependencies(config)
        last_marker = _snapshot_marker(config)
        next_regular = 0.0
        first = True
        try:
            while True:
                marker = _snapshot_marker(config)
                monotonic = time.monotonic()
                if first or monotonic >= next_regular or marker != last_marker:
                    last_marker = marker
                    summary = tick(dependencies)
                    retained["last_tick_at"] = summary["finished_at"]
                    retained["last_tick_summary"] = summary["counts"]
                    _write_status(config, retained)
                    next_regular = time.monotonic() + config.supervisor_interval_seconds
                    first = False
                    if stopping:
                        break
                if stopping:
                    break
                time.sleep(_POLL_SECONDS)
        finally:
            for signum, handler in prior.items():
                signal.signal(signum, handler)
        return 0


def supervisor_status(config: OrchestrationConfig) -> tuple[dict[str, Any], int]:
    retained = _read_status(config)
    held = _lock_held(config)
    if retained is None:
        return {
            "running": False, "lock_held": held, "pid": None, "live": False,
            "last_tick_at": None, "last_tick_summary": None,
            "message": "not running",
        }, 1
    try:
        live = verify_process(retained["pid"], retained["process_identity"])
    except HarnessError:
        live = False
    healthy = held and live
    return {
        "running": healthy, "lock_held": held, "pid": retained["pid"],
        "live": live, "started_at": retained["started_at"],
        "last_tick_at": retained["last_tick_at"],
        "last_tick_summary": retained["last_tick_summary"],
        "message": "running" if healthy else "not running (stale status)",
    }, 0 if healthy else 1


def _run_argv() -> list[str]:
    library_root = Path(__file__).resolve().parents[2]
    program = (
        "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
        "runpy.run_module('control.cli',run_name='__main__')"
    )
    return [
        sys.executable, "-B", "-I", "-c", program, str(library_root),
        "control", "supervisor", "run",
    ]


def start_supervisor(
    config: OrchestrationConfig, env: Mapping[str, str],
) -> tuple[dict[str, Any], int]:
    current, code = supervisor_status(config)
    if code == 0:
        current["message"] = "already running"
        return current, 0
    child_env = dict(os.environ)
    child_env.update({key: str(value) for key, value in env.items()})
    child = subprocess.Popen(
        _run_argv(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
        env=child_env,
    )
    for _ in range(100):
        current, code = supervisor_status(config)
        if code == 0:
            current["message"] = "started"
            return current, 0
        returncode = child.poll()
        if returncode is not None:
            if current["lock_held"]:
                current["message"] = "already running"
                return current, 0
            current["message"] = f"failed to start (exit {returncode})"
            return current, 1
        time.sleep(0.05)
    return {
        "running": False, "pid": child.pid,
        "message": "start timed out before healthy status",
    }, 1


def stop_supervisor(config: OrchestrationConfig) -> tuple[dict[str, Any], int]:
    retained = _read_status(config)
    if retained is None:
        return {"running": False, "signalled": False, "message": "not running"}, 1
    try:
        live = verify_process(retained["pid"], retained["process_identity"])
    except HarnessError:
        live = False
    if not live:
        return {
            "running": False, "pid": retained["pid"], "signalled": False,
            "message": "stale status; process identity is not live",
        }, 1
    if not _lock_held(config):
        return {
            "running": False, "pid": retained["pid"], "signalled": False,
            "message": "stale status; supervisor lock is not held",
        }, 1
    try:
        live = verify_process(retained["pid"], retained["process_identity"])
    except HarnessError:
        live = False
    if not live:
        return {
            "running": False, "pid": retained["pid"], "signalled": False,
            "message": "stale status; process identity changed before signal",
        }, 1
    os.kill(retained["pid"], signal.SIGTERM)
    for _ in range(100):
        time.sleep(0.05)
        try:
            if not verify_process(retained["pid"], retained["process_identity"]):
                return {
                    "running": False, "pid": retained["pid"], "signalled": True,
                    "message": "stopped",
                }, 0
        except HarnessError:
            return {
                "running": False, "pid": retained["pid"], "signalled": True,
                "message": "stopped",
            }, 0
    return {
        "running": True, "pid": retained["pid"], "signalled": True,
        "message": "termination requested; current tick is still finishing",
    }, 0


def _usage(stream=sys.stdout) -> None:
    print("Usage: asha control supervisor {run|start|stop|status} [--json]", file=stream)


def supervisor_main(
    argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    values = os.environ if env is None else env
    if not args or args[0] in {"-h", "--help", "help"}:
        _usage(sys.stdout if args else sys.stderr)
        return 0 if args else 2
    command = args[0]
    if command not in {"run", "start", "stop", "status"}:
        _usage(sys.stderr)
        return 2
    tail = args[1:]
    json_output = tail == ["--json"]
    if tail and not json_output:
        _usage(sys.stderr)
        return 2
    try:
        config = load_config(values)
        if command == "run":
            return run_supervisor(config, json_output=json_output)
        if command == "start":
            payload, code = start_supervisor(config, values)
        elif command == "stop":
            payload, code = stop_supervisor(config)
        else:
            payload, code = supervisor_status(config)
        _emit(payload, json_output)
        return code
    except (
        HarnessError, OrchestrationConfigError, StoreError, OSError, ValueError,
    ) as exc:
        payload = {"running": False, "message": _exception_message(exc)}
        if json_output:
            _emit(payload, True)
        else:
            print(f"asha control supervisor: {payload['message']}", file=sys.stderr)
        return 2


__all__ = [
    "run_supervisor", "start_supervisor", "status_path", "stop_supervisor",
    "supervisor_lock_path", "supervisor_main", "supervisor_status",
]
