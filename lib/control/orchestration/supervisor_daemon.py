"""flock-owned process loop and CLI for the orchestration supervisor."""

from __future__ import annotations

import errno
import fcntl
import getpass
import json
import os
import secrets
import signal
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterator, Mapping, Sequence

from ..harness import HarnessError, process_identity, verify_process
from ..process import capture_bytes
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
_SERVICE_COMMAND_LIMIT = 16 * 1024
_SERVICE_COMMAND_TIMEOUT = 10
_SERVICE_NAME = "asha-supervisor.service"
SUPERVISOR_SERVICE_MARKER = (
    "# Managed by asha control supervisor; edit via 'asha control supervisor', "
    "not by hand."
)
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


def supervisor_service_path(env: Mapping[str, str]) -> Path:
    home = Path(env.get("HOME") or str(Path.home()))
    config_home = Path(env.get("XDG_CONFIG_HOME") or str(home / ".config"))
    return config_home / "systemd" / "user" / _SERVICE_NAME


def render_supervisor_service(
    env: Mapping[str, str], asha_root: Path,
) -> str:
    home = Path(env.get("HOME") or str(Path.home()))
    asha_home = env.get("ASHA_HOME")
    asha_home_line = ""
    if asha_home and asha_home != str(home / ".asha"):
        # A non-default root must reach the service, which runs outside any
        # shell that exported it.
        asha_home_line = f'Environment="ASHA_HOME={asha_home}"\n'
    return (
        "[Unit]\n"
        f"{SUPERVISOR_SERVICE_MARKER}\n"
        "Description=Asha Control supervisor\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        'Environment="PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin"\n'
        f"{asha_home_line}"
        f"ExecStart={asha_root}/bin/asha control supervisor run\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "WorkingDirectory=%h\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _unit_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _owned_service(path: Path) -> bool:
    if not _unit_exists(path) or not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            prefix = handle.read(4097)
    except OSError:
        return False
    if len(prefix) > 4096:
        return False
    lines = prefix.decode("utf-8", errors="replace").splitlines()
    return len(lines) >= 2 and lines[1] == SUPERVISOR_SERVICE_MARKER


def _resolve_command(
    name: str, env: Mapping[str, str], which: Callable[[str], str | None] | None,
) -> str | None:
    if which is not None:
        return which(name)
    return shutil.which(name, path=env.get("PATH") or os.environ.get("PATH"))


def _capture_service_command(
    argv: list[str], runner: Callable[..., Any] | None,
) -> tuple[int, bytes, bytes]:
    return capture_bytes(
        argv, cwd=None, limit=_SERVICE_COMMAND_LIMIT, runner=runner,
        error_type=ValueError, deadline_seconds=_SERVICE_COMMAND_TIMEOUT,
    )


def supervisor_service_status(
    env: Mapping[str, str], *, runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> dict[str, bool | None]:
    systemctl = _resolve_command("systemctl", env, which)
    if systemctl is None:
        return {
            "service_present": None,
            "service_enabled": None,
            "service_active": None,
        }
    present = _unit_exists(supervisor_service_path(env))
    try:
        enabled, _stdout, _stderr = _capture_service_command(
            [systemctl, "--user", "is-enabled", _SERVICE_NAME], runner,
        )
        active, _stdout, _stderr = _capture_service_command(
            [systemctl, "--user", "is-active", _SERVICE_NAME], runner,
        )
    except ValueError:
        return {
            "service_present": present,
            "service_enabled": None,
            "service_active": None,
        }
    return {
        "service_present": present,
        "service_enabled": enabled == 0,
        "service_active": active == 0,
    }


def _service_summary(status: Mapping[str, Any]) -> str:
    def label(value: bool | None) -> str:
        return "unknown" if value is None else "yes" if value else "no"

    return (
        f"service present={label(status['service_present'])}, "
        f"enabled={label(status['service_enabled'])}, "
        f"active={label(status['service_active'])}"
    )


def _write_service(path: Path, body: str) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{secrets.token_hex(8)}")
    raw = body.encode("utf-8")
    fd = -1
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o644,
        )
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise OSError(errno.EIO, "short supervisor service write")
            offset += written
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        _close_quietly(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _linger_status(
    env: Mapping[str, str], runner: Callable[..., Any] | None,
    which: Callable[[str], str | None] | None,
) -> bool | None:
    loginctl = _resolve_command("loginctl", env, which)
    if loginctl is None:
        return None
    user = env.get("USER") or getpass.getuser()
    try:
        returncode, stdout, _stderr = _capture_service_command(
            [loginctl, "show-user", user, "--property=Linger"], runner,
        )
    except ValueError:
        return None
    if returncode != 0:
        return None
    value = stdout.decode("utf-8", errors="replace").strip()
    if value.startswith("Linger="):
        value = value.partition("=")[2]
    if value == "yes":
        return True
    if value == "no":
        return False
    return None


def _linger_advisory() -> str:
    return (
        "Without user lingering the supervisor service starts at login; "
        "with lingering it starts at boot."
    )


def install_supervisor_service(
    config: OrchestrationConfig, env: Mapping[str, str], *,
    asha_root: Path | None = None, dry_run: bool = False,
    runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> tuple[dict[str, Any], int]:
    root = asha_root or Path(__file__).resolve().parents[3]
    path = supervisor_service_path(env)
    body = render_supervisor_service(env, root)
    if _unit_exists(path) and not _owned_service(path):
        raise ValueError(f"foreign unit exists at {path}; refusing")
    commands = [
        "systemctl --user daemon-reload",
        f"systemctl --user enable --now {_SERVICE_NAME}",
    ]
    if dry_run:
        message = (
            f"would write {path}:\n{body}"
            + "".join(f"would run: {command}\n" for command in commands)
        ).rstrip()
        return {
            "installed": False, "dry_run": True, "unit_path": str(path),
            "unit_body": body, "commands": commands, "message": message,
        }, 0
    systemctl = _resolve_command("systemctl", env, which)
    if systemctl is None:
        raise ValueError("systemctl is unavailable; cannot install supervisor service")
    # Prove the user bus is reachable before stopping anything: a detached
    # shell without DBUS/XDG_RUNTIME_DIR must fail here with the manual
    # supervisor still running, never after it has been taken down.
    prior_body = path.read_text() if _unit_exists(path) else None
    _write_service(path, body)

    def _restore_unit() -> None:
        # A refusal leaves the filesystem as found; the written unit is
        # inert without enable, but residue would still confuse status.
        if prior_body is None:
            path.unlink(missing_ok=True)
        else:
            _write_service(path, prior_body)

    returncode, _stdout, stderr = _capture_service_command(
        [systemctl, "--user", "daemon-reload"], runner,
    )
    if returncode != 0:
        _restore_unit()
        raise ValueError(
            "systemctl --user daemon-reload failed: "
            f"{stderr.decode('utf-8', errors='replace').strip() or f'exit {returncode}'}"
        )
    stopped, _stop_code = stop_supervisor(config)
    if stopped.get("running") or _lock_held(config):
        _restore_unit()
        raise ValueError("supervisor is still stopping; retry service installation")
    returncode, _stdout, stderr = _capture_service_command(
        [systemctl, "--user", "enable", "--now", _SERVICE_NAME], runner,
    )
    if returncode != 0:
        raise ValueError(
            "systemctl --user enable --now failed: "
            f"{stderr.decode('utf-8', errors='replace').strip() or f'exit {returncode}'}"
        )
    linger = _linger_status(env, runner, which)
    linger_text = (
        "enabled" if linger is True else "not enabled" if linger is False else "unknown"
    )
    return {
        "installed": True, "dry_run": False, "unit_path": str(path),
        "linger_enabled": linger,
        "message": f"installed and started; user lingering is {linger_text}. "
                   f"{_linger_advisory()}",
    }, 0


def uninstall_supervisor_service(
    env: Mapping[str, str], *, dry_run: bool = False,
    runner: Callable[..., Any] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> tuple[dict[str, Any], int]:
    path = supervisor_service_path(env)
    exists = _unit_exists(path)
    if exists and not _owned_service(path):
        raise ValueError(f"foreign unit exists at {path}; refusing")
    commands = [
        f"systemctl --user disable --now {_SERVICE_NAME}",
        "systemctl --user daemon-reload",
    ]
    if dry_run:
        message = (
            f"would run: {commands[0]}\n"
            f"would remove: {path}\n"
            f"would run: {commands[1]}"
        )
        return {
            "removed": False, "dry_run": True, "unit_path": str(path),
            "commands": commands, "message": message,
        }, 0
    systemctl = _resolve_command("systemctl", env, which)
    if systemctl is None:
        raise ValueError("systemctl is unavailable; cannot uninstall supervisor service")
    _capture_service_command(
        [systemctl, "--user", "disable", "--now", _SERVICE_NAME], runner,
    )
    removed = False
    if exists:
        try:
            path.unlink()
            removed = True
        except FileNotFoundError:
            pass
    returncode, _stdout, stderr = _capture_service_command(
        [systemctl, "--user", "daemon-reload"], runner,
    )
    if returncode != 0:
        raise ValueError(
            "systemctl --user daemon-reload failed: "
            f"{stderr.decode('utf-8', errors='replace').strip() or f'exit {returncode}'}"
        )
    return {
        "removed": removed, "dry_run": False, "unit_path": str(path),
        "message": "removed" if removed else "not installed",
    }, 0


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
    # A daemon must not pin its caller's cwd or mount.
    os.chdir(Path.home())
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
    print(
        "Usage: asha control supervisor {run|start|stop|status} [--json]\n"
        "       asha control supervisor {install|uninstall} [--dry-run] [--json]",
        file=stream,
    )


def supervisor_main(
    argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    values = os.environ if env is None else env
    if not args or args[0] in {"-h", "--help", "help"}:
        _usage(sys.stdout if args else sys.stderr)
        return 0 if args else 2
    command = args[0]
    if command not in {"run", "start", "stop", "status", "install", "uninstall"}:
        _usage(sys.stderr)
        return 2
    tail = args[1:]
    json_output = "--json" in tail
    dry_run = "--dry-run" in tail
    allowed = (
        {"--json", "--dry-run"}
        if command in {"install", "uninstall"} else {"--json"}
    )
    if any(item not in allowed for item in tail) or len(tail) != len(set(tail)):
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
        elif command == "install":
            payload, code = install_supervisor_service(
                config, values, dry_run=dry_run,
            )
        elif command == "uninstall":
            payload, code = uninstall_supervisor_service(values, dry_run=dry_run)
        else:
            payload, code = supervisor_status(config)
            service = supervisor_service_status(values)
            payload.update(service)
            if not json_output:
                payload["message"] = f"{payload['message']}; {_service_summary(service)}"
        if dry_run and not json_output:
            print(payload["message"])
        else:
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
    "SUPERVISOR_SERVICE_MARKER", "install_supervisor_service",
    "render_supervisor_service", "run_supervisor", "start_supervisor",
    "status_path", "stop_supervisor", "supervisor_lock_path",
    "supervisor_main", "supervisor_service_path", "supervisor_service_status",
    "supervisor_status", "uninstall_supervisor_service",
]
