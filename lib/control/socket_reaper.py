"""Fail-closed teardown for short-lived Asha-owned tmux sockets."""

from __future__ import annotations

import atexit
import errno
import os
import re
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any


_ASHA_SOCKET = re.compile(r"asha-[A-Za-z0-9][A-Za-z0-9._-]{0,122}", re.ASCII)
_TERMINATING_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
_LIVENESS_ATTEMPTS = 20
_FORCE_KILL_ATTEMPT = _LIVENESS_ATTEMPTS // 2


@dataclass(frozen=True, slots=True)
class SocketOwner:
    """One retained Linux process identity holding an exact socket inode."""

    pid: int
    uid: int
    start_ticks: int


def is_asha_socket_name(value: Any) -> bool:
    """Return whether *value* is one restricted, Asha-owned socket name."""
    return isinstance(value, str) and _ASHA_SOCKET.fullmatch(value) is not None


def tmux_socket_path(
    name: str, *, environ: Mapping[str, str] | None = None, uid: int | None = None,
) -> Path:
    """Resolve the path tmux uses for ``-L name`` without touching the server."""
    values = os.environ if environ is None else environ
    parent = values.get("TMUX_TMPDIR") or "/tmp"
    return Path(parent) / f"tmux-{os.getuid() if uid is None else uid}" / name


def _socket_connects(path: Path) -> bool:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(0.1)
    try:
        client.connect(str(path))
    except OSError as exc:
        if exc.errno in {
            errno.ENOENT,
            errno.ENOTDIR,
            errno.ECONNREFUSED,
            errno.ENOTSOCK,
        }:
            return False
        return True
    finally:
        client.close()
    return True


def unix_socket_is_live(
    path: Path, *, proc_net_unix: Path = Path("/proc/net/unix"),
) -> bool:
    """Check kernel UNIX-socket state before a fail-closed connection probe."""
    try:
        lines = proc_net_unix.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        pass
    else:
        expected = str(path)
        for line in lines[1:]:
            fields = line.split(maxsplit=7)
            if len(fields) == 8 and fields[7] == expected:
                return True
    # /proc may describe only this process's network namespace while the tmux
    # socket lives in a shared mount namespace.  Absence from the table is
    # therefore not proof of death; a refused/indeterminate connect stays live.
    return _socket_connects(path)


def _process_identity(process_dir: Path) -> SocketOwner:
    """Read the procfs identity needed to distinguish reuse of one PID."""
    metadata = process_dir.stat()
    raw = (process_dir / "stat").read_text(encoding="utf-8")
    fields = raw[raw.rfind(")") + 2:].split()
    start_ticks = int(fields[19])
    if start_ticks < 0:
        raise ValueError("process start ticks are invalid")
    return SocketOwner(
        pid=int(process_dir.name), uid=metadata.st_uid, start_ticks=start_ticks,
    )


def unix_socket_owners(
    path: Path,
    *,
    proc_net_unix: Path = Path("/proc/net/unix"),
    proc_root: Path = Path("/proc"),
    uid: int | None = None,
) -> tuple[SocketOwner, ...]:
    """Return same-user process identities holding the socket at *path*.

    The kernel socket inode, rather than the unrelated filesystem inode, joins
    ``/proc/net/unix`` to process file descriptors.  An empty tuple is the
    fail-closed result when procfs is unavailable or no exact owner is visible.
    """
    try:
        lines = proc_net_unix.read_text(
            encoding="utf-8", errors="replace",
        ).splitlines()
    except OSError:
        return ()
    expected = str(path)
    inodes: set[str] = set()
    for line in lines[1:]:
        fields = line.split(maxsplit=7)
        if len(fields) == 8 and fields[7] == expected and fields[6].isdigit():
            inodes.add(fields[6])
    if not inodes:
        return ()

    expected_uid = os.getuid() if uid is None else uid
    owners: set[SocketOwner] = set()
    try:
        entries = tuple(proc_root.iterdir())
    except OSError:
        return ()
    for process_dir in entries:
        if not process_dir.name.isdigit():
            continue
        pid = int(process_dir.name)
        if pid <= 1 or pid == os.getpid():
            continue
        try:
            identity = _process_identity(process_dir)
            if identity.uid != expected_uid:
                continue
            descriptors = tuple((process_dir / "fd").iterdir())
        except (OSError, ValueError, IndexError):
            continue
        owns_socket = False
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                if target[8:-1] in inodes:
                    owns_socket = True
                    break
        if not owns_socket:
            continue
        try:
            # The process can exit while its descriptors are being inspected.
            # Retain an identity only if the same process still occupies the
            # PID after the exact socket descriptor has been observed.
            if _process_identity(process_dir) == identity:
                owners.add(identity)
        except (OSError, ValueError, IndexError):
            continue
    return tuple(sorted(owners, key=lambda owner: owner.pid))


def unix_socket_owner_pids(
    path: Path,
    *,
    proc_net_unix: Path = Path("/proc/net/unix"),
    proc_root: Path = Path("/proc"),
    uid: int | None = None,
) -> tuple[int, ...]:
    """Return same-user owner PIDs for compatibility with older callers."""
    return tuple(
        owner.pid for owner in unix_socket_owners(
            path, proc_net_unix=proc_net_unix, proc_root=proc_root, uid=uid,
        )
    )


def _signal_process_owner(
    owner: SocketOwner, signum: int, *, proc_root: Path = Path("/proc"),
) -> None:
    """Signal the retained process instance without a PID-reuse race."""
    if owner.uid != os.getuid():
        return
    descriptor: int | None = None
    try:
        # A pidfd pins the process instance even if it exits and its numeric PID
        # is reused before the signal.  Revalidation after opening the pidfd
        # proves that it was opened for the identity observed holding the socket.
        descriptor = os.pidfd_open(owner.pid)
        if _process_identity(proc_root / str(owner.pid)) != owner:
            return
        signal.pidfd_send_signal(descriptor, signum)
    except (AttributeError, OSError, ValueError, IndexError):
        # Kernels or Python builds without pidfds fail closed: falling back to
        # os.kill would reintroduce the exact PID-reuse race this path prevents.
        return
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _signal_socket_owners(
    path: Path,
    signum: int,
    *,
    owner_probe: Callable[[Path], Iterable[SocketOwner]],
    process_signaler: Callable[[SocketOwner, int], Any],
) -> None:
    """Best-effort direct termination of only the exact socket's owners."""
    try:
        owners = tuple(owner_probe(path))
    except BaseException:
        return
    for owner in set(owners):
        if (
            not isinstance(owner, SocketOwner)
            or owner.pid <= 1
            or owner.pid == os.getpid()
        ):
            continue
        try:
            # The owner may have closed the socket, exited, or had its PID
            # reused since the first procfs walk.  Require the exact retained
            # identity to still own the socket immediately before signaling.
            if owner not in set(owner_probe(path)):
                continue
            process_signaler(owner, signum)
        except BaseException:
            # A vanished process and a denied signal are both resolved by the
            # authoritative liveness proof below.  Teardown must never escape.
            continue


def reap_isolated_tmux_socket(
    name: str,
    *,
    executable: str = "tmux",
    config_file: str | Path = "/dev/null",
    environ: Mapping[str, str] | None = None,
    uid: int | None = None,
    runner: Callable[..., Any] = subprocess.run,
    liveness_probe: Callable[[Path], bool] = unix_socket_is_live,
    owner_probe: Callable[[Path], Iterable[SocketOwner]] = unix_socket_owners,
    process_signaler: Callable[[SocketOwner, int], Any] = _signal_process_owner,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Kill, verify, and unlink one private socket; never raise from teardown.

    The ownership grammar is checked before invoking tmux or inspecting a path.
    A failed or indeterminate liveness probe retains the socket.  The return
    value reports that the socket was confirmed dead and is now absent.
    """
    if not is_asha_socket_name(name):
        return False
    try:
        values = dict(os.environ if environ is None else environ)
        path = tmux_socket_path(name, environ=values, uid=uid)
    except BaseException:
        return False
    try:
        runner(
            [
                executable, "-L", name, "-f", str(config_file),
                "kill-server",
            ],
            capture_output=True,
            check=False,
            env=values,
            timeout=1.0,
        )
    except BaseException:
        # kill-server is best effort; the liveness proof below is authoritative.
        pass

    for attempt in range(_LIVENESS_ATTEMPTS):
        try:
            live = liveness_probe(path)
        except BaseException:
            return False
        if not isinstance(live, bool):
            return False
        if not live:
            try:
                path.unlink(missing_ok=True)
            except BaseException:
                return False
            return True
        if attempt == 0:
            # A managed sandbox can allow tmux to bind the socket and then
            # deny both the initiating client and a later kill-server client.
            # Resolve the listening socket's exact same-user owner through
            # procfs so that cleanup does not depend solely on reconnecting.
            _signal_socket_owners(
                path, signal.SIGTERM,
                owner_probe=owner_probe, process_signaler=process_signaler,
            )
        elif attempt == _FORCE_KILL_ATTEMPT:
            _signal_socket_owners(
                path, signal.SIGKILL,
                owner_probe=owner_probe, process_signaler=process_signaler,
            )
        if attempt + 1 < _LIVENESS_ATTEMPTS:
            try:
                sleeper(0.025)
            except BaseException:
                return False
    return False


class TmuxSocketReaper:
    """Arm idempotent atexit and terminating-signal cleanup for one socket."""

    def __init__(self, name: str, **reap_options: Any) -> None:
        self.name = name
        self.reap_options = reap_options
        self._armed = False
        self._closed = False
        self._result: bool | None = None
        self._previous_handlers: dict[int, Any] = {}

    @property
    def result(self) -> bool | None:
        return self._result

    def arm(self) -> TmuxSocketReaper:
        if self._armed or self._closed:
            return self
        self._armed = True
        atexit.register(self.close)
        for signum in _TERMINATING_SIGNALS:
            try:
                previous = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
            except (OSError, RuntimeError, ValueError):
                continue
            self._previous_handlers[signum] = previous
        return self

    def close(self) -> bool:
        if self._closed:
            return bool(self._result)
        try:
            self._result = reap_isolated_tmux_socket(self.name, **self.reap_options)
        except BaseException:
            self._result = False
        if not self._result:
            # Keep an armed reaper registered, and keep explicit close calls
            # retryable, until cleanup has actually proved the server dead.
            return False
        self._closed = True
        self._armed = False
        self._restore_signal_handlers()
        try:
            atexit.unregister(self.close)
        except BaseException:
            pass
        return True

    def _restore_signal_handlers(self) -> None:
        for signum, previous in self._previous_handlers.items():
            try:
                if signal.getsignal(signum) == self._handle_signal:
                    signal.signal(signum, previous)
            except (OSError, RuntimeError, ValueError):
                pass
        self._previous_handlers.clear()

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        previous = self._previous_handlers.get(signum, signal.SIG_DFL)
        self.close()
        if previous == signal.SIG_IGN:
            return
        if callable(previous):
            previous(signum, frame)
            return
        try:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
        except (OSError, RuntimeError, ValueError):
            raise SystemExit(128 + signum)

    def __enter__(self) -> TmuxSocketReaper:
        return self.arm()

    def __exit__(self, _exc_type, _exc, _traceback) -> bool:
        self.close()
        return False
