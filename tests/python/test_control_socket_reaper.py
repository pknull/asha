"""Short-lived tmux socket teardown without requiring a live tmux server."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from lib.control import doctor as control_doctor
from lib.control.socket_reaper import (
    SocketOwner,
    TmuxSocketReaper,
    _signal_process_owner,
    _signal_socket_owners,
    is_asha_socket_name,
    reap_isolated_tmux_socket,
    tmux_socket_path,
    unix_socket_is_live,
    unix_socket_owners,
)


class SocketReaperUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environ = {"TMUX_TMPDIR": str(self.root)}
        self.uid = 1234

    def socket_file(self, name: str) -> Path:
        path = tmux_socket_path(name, environ=self.environ, uid=self.uid)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text("stale", encoding="ascii")
        return path

    def process(self, proc: Path, pid: int, start_ticks: int) -> Path:
        process = proc / str(pid)
        (process / "fd").mkdir(parents=True)
        fields = ["S", *("0" for _ in range(18)), str(start_ticks)]
        (process / "stat").write_text(
            f"{pid} (tmux: server) {' '.join(fields)}\n", encoding="ascii",
        )
        return process

    def test_ownership_rule_is_restricted_to_safe_asha_names(self) -> None:
        for accepted in ("asha-probe", "asha-test-1", "asha-a.b_c"):
            with self.subTest(accepted=accepted):
                self.assertTrue(is_asha_socket_name(accepted))
        for refused in (
            "default", "control", "asha", "asha-", "asha-/default",
            "../asha-owned", "/tmp/asha-owned", "Asha-owned", None,
        ):
            with self.subTest(refused=refused):
                self.assertFalse(is_asha_socket_name(refused))

    def test_refused_name_never_invokes_tmux_probes_or_unlinks(self) -> None:
        default = self.socket_file("default")
        calls: list[str] = []

        removed = reap_isolated_tmux_socket(
            "default", environ=self.environ, uid=self.uid,
            runner=lambda *_args, **_kwargs: calls.append("runner"),
            liveness_probe=lambda _path: calls.append("probe") or False,
        )

        self.assertFalse(removed)
        self.assertTrue(default.exists())
        self.assertEqual(calls, [])

    def test_dead_socket_is_killed_verified_unlinked_and_idempotent(self) -> None:
        name = "asha-dead-test"
        stale = self.socket_file(name)
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(argv)

        options = {
            "environ": self.environ,
            "uid": self.uid,
            "runner": runner,
            "liveness_probe": lambda path: False,
        }
        self.assertTrue(reap_isolated_tmux_socket(name, **options))
        self.assertFalse(stale.exists())
        self.assertTrue(reap_isolated_tmux_socket(name, **options))
        self.assertEqual(calls[0][-1], "kill-server")
        self.assertEqual(calls[0][1:3], ["-L", name])

    def test_live_or_indeterminate_socket_is_never_unlinked(self) -> None:
        for outcome in (True, RuntimeError("probe failed")):
            with self.subTest(outcome=outcome):
                name = "asha-live-test"
                path = self.socket_file(name)

                def probe(_path):
                    if isinstance(outcome, BaseException):
                        raise outcome
                    return outcome

                removed = reap_isolated_tmux_socket(
                    name, environ=self.environ, uid=self.uid,
                    runner=lambda *_args, **_kwargs: None,
                    liveness_probe=probe, sleeper=lambda _seconds: None,
                )
                self.assertFalse(removed)
                self.assertTrue(path.exists())

    def test_kernel_socket_table_probe_needs_no_live_tmux_server(self) -> None:
        path = tmux_socket_path("asha-kernel-test", environ=self.environ, uid=self.uid)
        table = self.root / "proc-net-unix"
        table.write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            f"0000: 00000002 00000000 00010000 0001 01 1 {path}\n",
            encoding="utf-8",
        )
        self.assertTrue(unix_socket_is_live(path, proc_net_unix=table))
        table.write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n",
            encoding="utf-8",
        )
        with mock.patch(
            "lib.control.socket_reaper._socket_connects", return_value=True,
        ) as connects:
            self.assertTrue(unix_socket_is_live(path, proc_net_unix=table))
        connects.assert_called_once_with(path)

    def test_socket_owner_probe_joins_exact_kernel_inode_to_same_user_pid(self) -> None:
        path = tmux_socket_path("asha-owner-test", environ=self.environ, uid=self.uid)
        table = self.root / "proc-net-unix"
        table.write_text(
            "Num RefCount Protocol Flags Type St Inode Path\n"
            f"0000: 2 0 10000 1 01 24680 {path}\n"
            f"0001: 2 0 10000 1 01 13579 {path}-other\n",
            encoding="utf-8",
        )
        proc = self.root / "proc"
        matching = self.process(proc, 4321, 123456) / "fd"
        (matching / "7").symlink_to("socket:[24680]")
        other = self.process(proc, 4322, 234567) / "fd"
        (other / "8").symlink_to("socket:[13579]")

        self.assertEqual(
            unix_socket_owners(
                path, proc_net_unix=table, proc_root=proc, uid=os.getuid(),
            ),
            (SocketOwner(pid=4321, uid=os.getuid(), start_ticks=123456),),
        )

    def test_process_owner_signal_uses_pidfd_for_retained_identity(self) -> None:
        proc = self.root / "proc"
        self.process(proc, 4321, 123456)
        owner = SocketOwner(pid=4321, uid=os.getuid(), start_ticks=123456)

        with mock.patch(
            "lib.control.socket_reaper.os.pidfd_open", return_value=87,
        ) as open_pidfd, mock.patch(
            "lib.control.socket_reaper.signal.pidfd_send_signal",
        ) as send, mock.patch(
            "lib.control.socket_reaper.os.close",
        ) as close:
            _signal_process_owner(owner, signal.SIGTERM, proc_root=proc)

        open_pidfd.assert_called_once_with(4321)
        send.assert_called_once_with(87, signal.SIGTERM)
        close.assert_called_once_with(87)

    def test_process_owner_signal_refuses_reused_pid(self) -> None:
        owner = SocketOwner(pid=4321, uid=os.getuid(), start_ticks=123456)
        replacement = SocketOwner(pid=4321, uid=os.getuid(), start_ticks=999999)

        with mock.patch(
            "lib.control.socket_reaper.os.pidfd_open", return_value=87,
        ), mock.patch(
            "lib.control.socket_reaper._process_identity", return_value=replacement,
        ), mock.patch(
            "lib.control.socket_reaper.signal.pidfd_send_signal",
        ) as send, mock.patch("lib.control.socket_reaper.os.close") as close:
            _signal_process_owner(owner, signal.SIGKILL)

        send.assert_not_called()
        close.assert_called_once_with(87)

    def test_process_owner_signal_refuses_non_user_identity(self) -> None:
        owner = SocketOwner(pid=4321, uid=os.getuid() + 1, start_ticks=123456)

        with mock.patch(
            "lib.control.socket_reaper.os.pidfd_open",
        ) as open_pidfd, mock.patch(
            "lib.control.socket_reaper.signal.pidfd_send_signal",
        ) as send:
            _signal_process_owner(owner, signal.SIGTERM)

        open_pidfd.assert_not_called()
        send.assert_not_called()

    def test_socket_owner_is_revalidated_before_signaling(self) -> None:
        owner = SocketOwner(pid=4321, uid=os.getuid(), start_ticks=123456)
        probes = iter(((owner,), ()))
        signaler = mock.Mock()

        _signal_socket_owners(
            self.root / "asha-owner-test", signal.SIGTERM,
            owner_probe=lambda _path: next(probes),
            process_signaler=signaler,
        )

        signaler.assert_not_called()

    def test_denied_kill_server_directly_terminates_exact_socket_owner(self) -> None:
        name = "asha-direct-kill-test"
        stale = self.socket_file(name)
        live = iter((True, False))
        signals: list[tuple[int, int]] = []

        removed = reap_isolated_tmux_socket(
            name, environ=self.environ, uid=self.uid,
            runner=mock.Mock(side_effect=PermissionError("connect denied")),
            liveness_probe=lambda _path: next(live),
            owner_probe=lambda path: (
                (SocketOwner(4321, os.getuid(), 123456),)
                if path == stale else ()
            ),
            process_signaler=lambda owner, signum: signals.append(
                (owner.pid, signum)
            ),
            sleeper=lambda _seconds: None,
        )

        self.assertTrue(removed)
        self.assertFalse(stale.exists())
        self.assertEqual(signals, [(4321, signal.SIGTERM)])

    def test_teardown_swallows_kill_boundary_failure(self) -> None:
        name = "asha-error-test"
        self.socket_file(name)

        def broken_runner(*_args, **_kwargs):
            raise OSError("tmux failed")

        reaper = TmuxSocketReaper(
            name, environ=self.environ, uid=self.uid, runner=broken_runner,
            liveness_probe=lambda _path: False,
        )
        self.assertTrue(reaper.close())
        self.assertTrue(reaper.close())

    def test_failed_close_remains_retryable_until_cleanup_succeeds(self) -> None:
        reaper = TmuxSocketReaper("asha-retry-test")
        with mock.patch(
                "lib.control.socket_reaper.reap_isolated_tmux_socket",
                side_effect=(False, True),
        ) as reap, mock.patch(
                "lib.control.socket_reaper.atexit.register",
        ) as register, mock.patch(
                "lib.control.socket_reaper.atexit.unregister",
        ) as unregister, mock.patch(
                "lib.control.socket_reaper.signal.getsignal",
                return_value=signal.SIG_DFL,
        ), mock.patch("lib.control.socket_reaper.signal.signal"):
            reaper.arm()
            self.assertFalse(reaper.close())
            unregister.assert_not_called()
            self.assertTrue(reaper.close())
            self.assertTrue(reaper.close())
        register.assert_called_once_with(reaper.close)
        unregister.assert_called_once_with(reaper.close)
        self.assertEqual(reap.call_count, 2)

    def test_doctor_reaps_the_capability_socket_on_every_inner_exit(self) -> None:
        cases = (
            ((1, b"", b"denied"), "unavailable"),
            ((0, b"list-commands only\n", b""), "unavailable"),
            ((0, b"display-popup (popup)\n", b""), "match"),
            (OSError("probe failed"), "unavailable"),
        )
        for capability, expected in cases:
            with self.subTest(capability=capability):
                responses = iter(((0, b"tmux 3.4\n", b""), capability))

                def run_status(_adapter, _args):
                    response = next(responses)
                    if isinstance(response, BaseException):
                        raise response
                    return response

                with mock.patch(
                    "lib.control.doctor.shutil.which", return_value="/usr/bin/tmux",
                ), mock.patch.object(
                    control_doctor.TmuxAdapter,
                    "_run_status",
                    autospec=True,
                    side_effect=run_status,
                ), mock.patch(
                    "lib.control.socket_reaper.reap_isolated_tmux_socket",
                    return_value=True,
                ) as reap:
                    result = control_doctor._tmux_probe(None)

                self.assertEqual(result.outcome, expected)
                reap.assert_called_once()
                self.assertRegex(reap.call_args.args[0], r"^asha-doctor-probe-[0-9]+$")
                self.assertEqual(reap.call_args.kwargs["executable"], "/usr/bin/tmux")


class SocketReaperProcessTests(unittest.TestCase):
    CHILD = """
import os, signal, sys, time
from pathlib import Path
from lib.control.socket_reaper import TmuxSocketReaper, tmux_socket_path

name, root, ready, mode = sys.argv[1:]
env = {"TMUX_TMPDIR": root}
def no_tmux(*_args, **_kwargs):
    return None
reaper = TmuxSocketReaper(
    name, environ=env, runner=no_tmux, liveness_probe=lambda _path: False,
).arm()
socket_path = tmux_socket_path(name, environ=env)
socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
socket_path.write_text("stale", encoding="ascii")
Path(ready).write_text(str(socket_path), encoding="utf-8")
if mode == "signal":
    while True:
        time.sleep(1)
raise SystemExit(23 if mode == "nonzero" else 0)
"""

    def invoke(self, mode: str) -> tuple[subprocess.Popen, Path]:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        ready = root / "ready"
        name = f"asha-helper-{mode}-{os.getpid()}"
        process = subprocess.Popen(
            [sys.executable, "-c", self.CHILD, name, str(root), str(ready), mode],
            cwd=Path(__file__).resolve().parents[2],
        )
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists(), f"helper exited early with {process.poll()}")
        return process, Path(ready.read_text(encoding="utf-8"))

    def test_helper_exit_zero_and_nonzero_leave_no_socket_file(self) -> None:
        for mode, expected in (("zero", 0), ("nonzero", 23)):
            with self.subTest(mode=mode):
                process, socket_path = self.invoke(mode)
                self.assertEqual(process.wait(timeout=5), expected)
                self.assertFalse(socket_path.exists())

    def test_helper_termination_signal_leaves_no_socket_file(self) -> None:
        process, socket_path = self.invoke("signal")
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=5), -signal.SIGTERM)
        self.assertFalse(socket_path.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
