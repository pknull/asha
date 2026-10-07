from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from lib.control.cli import main as control_main
from lib.control.config import load_config
from lib.control.harness import process_identity
from lib.control.store import StoreError
from lib.control.supervisor_service import (
    SUPERVISOR_SERVICE_MARKER,
    install_supervisor_service,
    render_supervisor_service,
    run_supervisor,
    status_path,
    supervisor_service_path,
    supervisor_service_status,
    supervisor_lock_path,
    sweep,
    uninstall_supervisor_service,
)


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ControlFixture:
    """A private Control home with a project directory to run from."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "home").mkdir()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.env = {
            "HOME": str(self.root / "home"), "ASHA_CONFIG": str(self.root / "missing.json"),
            "ASHA_HOME": str(self.root / "asha"), "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.config = load_config(self.env)


class SupervisorProcessTests(ControlFixture, unittest.TestCase):
    def test_run_changes_cwd_to_home_before_the_first_sweep(self) -> None:
        observed = []

        def one_sweep(_config):
            observed.append(Path.cwd())
            os.kill(os.getpid(), signal.SIGTERM)
            return {"finished_at": now_text(), "counts": {"errors": 0}}

        with contextlib.chdir(self.repo), \
                mock.patch(
                    "lib.control.supervisor_service.Path.home",
                    return_value=Path(self.env["HOME"]),
                ), \
                mock.patch(
                    "lib.control.supervisor_service.sweep",
                    side_effect=one_sweep,
                ), redirect_stdout(StringIO()):
            result = run_supervisor(self.config)

        self.assertEqual(result, 0)
        self.assertEqual(observed, [Path(self.env["HOME"])])
        retained = json.loads(status_path(self.config).read_text())
        self.assertEqual(retained["last_tick_summary"], {"errors": 0})

    def test_sweeps_repeat_on_the_interval(self) -> None:
        calls = []

        def counted(_config):
            calls.append(time.monotonic())
            if len(calls) == 3:
                os.kill(os.getpid(), signal.SIGTERM)
            return {"finished_at": now_text(), "counts": {"errors": 0}}

        with contextlib.chdir(self.repo), \
                mock.patch("lib.control.supervisor_service.Path.home",
                           return_value=Path(self.env["HOME"])), \
                mock.patch("lib.control.supervisor_service.sweep", side_effect=counted), \
                mock.patch("lib.control.supervisor_service._SWEEP_SECONDS", 0.01), \
                mock.patch("lib.control.supervisor_service._POLL_SECONDS", 0.005), \
                redirect_stdout(StringIO()):
            self.assertEqual(run_supervisor(self.config), 0)
        self.assertEqual(len(calls), 3)

    def test_a_failed_owner_start_is_counted_and_reported_without_ending_the_sweep(self) -> None:
        stderr = StringIO()
        with mock.patch("lib.control.sessions.ensure_owners",
                        side_effect=StoreError("managed state unreadable")), \
                contextlib.redirect_stderr(stderr):
            summary = sweep(self.config)
        self.assertEqual(summary["counts"]["errors"], 1)
        self.assertIn("managed state unreadable", summary["counts"]["managed_error"])
        self.assertIn("managed session delivery unavailable", stderr.getvalue())

    def test_second_run_refuses_while_another_process_holds_the_flock(self) -> None:
        path = supervisor_lock_path(self.config)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        program = (
            "import fcntl,os,signal,sys\n"
            "fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600)\n"
            "fcntl.flock(fd,fcntl.LOCK_EX)\n"
            "print('ready',flush=True)\n"
            "signal.pause()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", program, str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert child.stdout is not None
        assert child.stderr is not None
        child_stdout = child.stdout
        child_stderr = child.stderr

        def cleanup_child():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child_stdout.close()
            child_stderr.close()

        self.addCleanup(cleanup_child)
        self.assertEqual(child_stdout.readline().strip(), "ready")
        output = StringIO()

        with contextlib.chdir(self.repo), redirect_stdout(output):
            status = control_main(
                ["control", "supervisor", "run", "--json"], env=self.env,
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["message"], "already running")

    def test_stop_reports_stale_dead_process_identity_without_signalling(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        identity = None
        for _ in range(100):
            identity = process_identity(child.pid)
            if identity is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(identity)
        child.terminate()
        child.wait(timeout=5)
        path = status_path(self.config)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "pid": child.pid,
            "process_identity": identity,
            "started_at": now_text(),
            "last_tick_at": None,
            "last_tick_summary": None,
        }) + "\n")
        path.chmod(0o600)
        output = StringIO()

        with mock.patch(
            "lib.control.supervisor_service.os.kill",
        ) as kill, redirect_stdout(output):
            result = control_main(
                ["control", "supervisor", "stop", "--json"], env=self.env,
            )

        self.assertEqual(result, 1)
        self.assertFalse(json.loads(output.getvalue())["signalled"])
        self.assertIn("stale", json.loads(output.getvalue())["message"])
        kill.assert_not_called()


class SupervisorLauncherTests(unittest.TestCase):
    """The unit's ExecStart is bin/asha; systemd's MainPID must be the supervisor."""

    def test_supervisor_run_replaces_the_launcher_shell(self) -> None:
        # KillMode=process signals MainPID only. A supervisor forked beneath
        # the launcher shell outlived restart and held the flock (#121).
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        home = root / "home"
        home.mkdir(mode=0o750)
        stub = root / "bin"
        stub.mkdir()
        record = root / "record"
        python = stub / "python3"
        python.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$$\" \"$@\" >'{record}'\n")
        python.chmod(0o755)
        repository = Path(__file__).resolve().parents[2]
        launcher = subprocess.Popen(
            [str(repository / "bin" / "asha"), "control", "supervisor", "run"],
            env={"HOME": str(home), "PATH": f"{stub}:/usr/bin:/bin", "LANG": "C.UTF-8"},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        _, stderr = launcher.communicate(timeout=30)

        self.assertEqual(launcher.returncode, 0, stderr)
        lines = record.read_text().splitlines()
        self.assertEqual(lines[0], str(launcher.pid))
        self.assertEqual(lines[1:4], ["-B", "-I", "-c"])
        self.assertEqual(
            lines[-4:], [str(repository / "lib"), "control", "supervisor", "run"],
        )


class SupervisorServiceTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.config_home = self.root / "config"
        self.home.mkdir(mode=0o700)
        self.home.chmod(0o700)
        self.env = {
            "HOME": str(self.home),
            "ASHA_HOME": str(self.home / ".asha"),
            "ASHA_CONFIG": str(self.root / "missing.json"),
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "USER": "keeper",
        }
        Path(self.env["XDG_RUNTIME_DIR"]).mkdir(mode=0o700)
        Path(self.env["XDG_RUNTIME_DIR"]).chmod(0o700)
        self.asha_root = Path("/opt/asha")
        self.config = load_config(self.env)
        self.calls: list[list[str]] = []

    def which(self, command: str) -> str | None:
        if command in {"systemctl", "loginctl"}:
            return f"/usr/bin/{command}"
        return None

    def runner(self, argv, **_kwargs):
        self.calls.append(list(argv))
        stdout = b"Linger=yes\n" if Path(argv[0]).name == "loginctl" else b""
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    def expected_unit(self, asha_home_line: str = "", jj_line: str = "",
                      harness_lines: str = "") -> str:
        return (
            "[Unit]\n"
            f"{SUPERVISOR_SERVICE_MARKER}\n"
            "Description=Asha Control supervisor\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            'Environment="PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin"\n'
            f"{jj_line}"
            f"{harness_lines}"
            f"{asha_home_line}"
            "ExecStart=/opt/asha/bin/asha control supervisor run\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "KillMode=process\n"
            "WorkingDirectory=%h\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def test_unit_body_renders_exactly_for_default_and_nondefault_asha_home(self) -> None:
        self.assertEqual(
            render_supervisor_service(self.env, self.asha_root, which=self.which),
            self.expected_unit(),
        )

        custom = dict(self.env, ASHA_HOME=str(self.root / "custom-asha"))
        self.assertEqual(
            render_supervisor_service(custom, self.asha_root, which=self.which),
            self.expected_unit(
                f'Environment="ASHA_HOME={self.root / "custom-asha"}"\n',
            ),
        )

    def test_unit_pins_install_time_jj_path_for_the_sanitized_daemon_path(self) -> None:
        def which(command: str) -> str | None:
            if command == "jj":
                return str(self.home / "bin" / "jj")
            return self.which(command)

        self.assertEqual(
            render_supervisor_service(self.env, self.asha_root, which=which),
            self.expected_unit(
                jj_line=f'Environment="ASHA_JJ={self.home / "bin" / "jj"}"\n',
            ),
        )

    def test_unit_pin_normalizes_dotdot_path_entries_to_the_real_binary(self) -> None:
        def which(command: str) -> str | None:
            if command == "jj":
                return str(self.home / ".local" / "share" / ".." / "bin" / "jj")
            return self.which(command)

        self.assertEqual(
            render_supervisor_service(self.env, self.asha_root, which=which),
            self.expected_unit(
                jj_line=(
                    f'Environment="ASHA_JJ={self.home / ".local" / "bin" / "jj"}"\n'
                ),
            ),
        )

    def test_unit_omits_jj_pin_for_unresolvable_or_unit_unsafe_paths(self) -> None:
        for resolved in (None, "/home/user name/jj", '/home/a"b/jj',
                         "/home/%h/jj"):
            with self.subTest(resolved=resolved):
                self.assertEqual(
                    render_supervisor_service(
                        self.env, self.asha_root,
                        which=lambda command, value=resolved: (
                            value if command == "jj" else self.which(command)
                        ),
                    ),
                    self.expected_unit(),
                )

    def test_unit_pins_install_time_structured_harness_commands(self) -> None:
        # #120: structured owners inherit the sanitized service PATH, where an
        # asdf/npm codex is absent ("exec: codex: not found", exit 127).
        versions = self.home / ".local" / "share" / "claude" / "versions"
        versions.mkdir(parents=True)
        (versions / "9.9.9").write_text("")
        launcher = self.home / ".local" / "bin" / "claude"
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(versions / "9.9.9")
        shim = self.home / ".asdf" / "shims" / "codex"

        def which(command: str) -> str | None:
            return {"claude": str(launcher), "codex": str(shim)}.get(
                command, self.which(command))

        # The stable launcher path, not its versioned target: Claude updates
        # repoint the link and asdf shims select the version at exec time.
        self.assertEqual(
            render_supervisor_service(self.env, self.asha_root, which=which),
            self.expected_unit(harness_lines=(
                f'Environment="ASHA_CLAUDE_CMD={launcher}"\n'
                f'Environment="ASHA_CODEX_CMD={shim}"\n'
            )),
        )

    def test_harness_pin_resolves_its_directory_but_not_the_launcher(self) -> None:
        # A PATH entry such as ~/.local/share/../bin must still pin, as jj's does.
        share = self.home / ".local" / "share"
        share.mkdir(parents=True)
        (self.home / ".local" / "bin").mkdir()
        found = str(share / ".." / "bin" / "claude")
        self.assertEqual(
            render_supervisor_service(
                self.env, self.asha_root,
                which=lambda command: found if command == "claude" else self.which(command),
            ),
            self.expected_unit(
                harness_lines=f'Environment="ASHA_CLAUDE_CMD={self.home / ".local" / "bin" / "claude"}"\n',
            ),
        )

    def test_unit_pin_honors_the_operators_harness_override(self) -> None:
        env = dict(self.env, ASHA_CODEX_CMD="/opt/codex/bin/codex")
        self.assertEqual(
            render_supervisor_service(
                env, self.asha_root,
                which=lambda command: (
                    command if command == "/opt/codex/bin/codex" else self.which(command)
                ),
            ),
            self.expected_unit(
                harness_lines='Environment="ASHA_CODEX_CMD=/opt/codex/bin/codex"\n',
            ),
        )

    def test_unit_omits_harness_pin_for_unresolvable_or_unit_unsafe_paths(self) -> None:
        for resolved in (None, "bin/codex", "../bin/codex", "/home/user name/codex",
                         '/home/a"b/codex', "/home/%h/codex"):
            with self.subTest(resolved=resolved):
                self.assertEqual(
                    render_supervisor_service(
                        self.env, self.asha_root,
                        which=lambda command, value=resolved: (
                            value if command == "codex" else self.which(command)
                        ),
                    ),
                    self.expected_unit(),
                )

    def test_install_refuses_foreign_unit_and_replaces_owned_unit(self) -> None:
        path = supervisor_service_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_text("[Unit]\nDescription=foreign\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "foreign unit"):
            install_supervisor_service(
                self.config, self.env, asha_root=self.asha_root,
                runner=self.runner, which=self.which,
            )
        self.assertEqual(path.read_text(encoding="utf-8"), "[Unit]\nDescription=foreign\n")
        self.assertEqual(self.calls, [])

        path.write_text(
            f"[Unit]\n{SUPERVISOR_SERVICE_MARKER}\nDescription=old\n",
            encoding="utf-8",
        )
        with mock.patch(
            "lib.control.supervisor_service.stop_supervisor",
            return_value=({"running": False}, 1),
        ), mock.patch(
            "lib.control.supervisor_service._lock_held",
            return_value=False,
        ):
            payload, code = install_supervisor_service(
                self.config, self.env, asha_root=self.asha_root,
                runner=self.runner, which=self.which,
            )

        self.assertEqual(code, 0)
        self.assertTrue(payload["linger_enabled"])
        self.assertIn("without user lingering", payload["message"].lower())
        self.assertIn("with lingering it starts at boot", payload["message"])
        self.assertEqual(path.read_text(encoding="utf-8"), self.expected_unit())

    def test_install_stops_manual_supervisor_before_enable_now(self) -> None:
        ordering: list[str] = []

        def stop(_config):
            ordering.append("stop")
            return {"running": False, "message": "stopped"}, 0

        def runner(argv, **kwargs):
            ordering.append(" ".join(argv[1:]))
            return self.runner(argv, **kwargs)

        with mock.patch(
            "lib.control.supervisor_service.stop_supervisor",
            side_effect=stop,
        ), mock.patch(
            "lib.control.supervisor_service._lock_held",
            return_value=False,
        ):
            install_supervisor_service(
                self.config, self.env, asha_root=self.asha_root,
                runner=runner, which=self.which,
            )

        # The bus must be proven reachable (daemon-reload) BEFORE the manual
        # supervisor is stopped, and the stop must precede enable --now: a
        # dead bus must never leave the plane with no supervisor at all.
        self.assertLess(
            ordering.index("--user daemon-reload"), ordering.index("stop"),
        )
        self.assertLess(ordering.index("stop"), ordering.index(
            "--user enable --now asha-supervisor.service",
        ))

    def test_install_refuses_while_the_single_instance_lock_remains_held(self) -> None:
        path = supervisor_service_path(self.env)
        with mock.patch(
            "lib.control.supervisor_service.stop_supervisor",
            return_value=({"running": False, "message": "not running"}, 1),
        ), mock.patch(
            "lib.control.supervisor_service._lock_held",
            return_value=True,
        ):
            value, code = install_supervisor_service(
                self.config, self.env, asha_root=self.asha_root,
                runner=self.runner, which=self.which,
            )
            self.assertEqual(code, 2)
            self.assertEqual(value["status"], "unavailable")
        # Unknown ownership now refuses before any unit or service mutation.
        self.assertFalse(path.exists())
        self.assertEqual(self.calls, [])

    def test_uninstall_removes_only_owned_unit_and_is_idempotent(self) -> None:
        path = supervisor_service_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_text("[Unit]\nDescription=foreign\n", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "foreign unit"):
            uninstall_supervisor_service(
                self.env, runner=self.runner, which=self.which,
            )
        self.assertTrue(path.exists())
        self.assertEqual(self.calls, [])

        path.write_text(self.expected_unit(), encoding="utf-8")
        first, first_code = uninstall_supervisor_service(
            self.env, runner=self.runner, which=self.which,
        )
        second, second_code = uninstall_supervisor_service(
            self.env, runner=self.runner, which=self.which,
        )

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertFalse(path.exists())
        self.assertTrue(first["removed"])
        self.assertFalse(second["removed"])

    def test_install_dry_run_writes_nothing_and_prints_commands(self) -> None:
        path = supervisor_service_path(self.env)
        with mock.patch(
            "lib.control.supervisor_service.stop_supervisor",
        ) as stop:
            payload, code = install_supervisor_service(
                self.config, self.env, asha_root=self.asha_root, dry_run=True,
                runner=self.runner, which=self.which,
            )

        self.assertEqual(code, 0)
        self.assertFalse(path.exists())
        self.assertEqual(self.calls, [])
        stop.assert_not_called()
        self.assertIn(self.expected_unit(), payload["message"])
        self.assertIn("would run: systemctl --user daemon-reload", payload["message"])
        self.assertIn(
            "would run: systemctl --user enable --now asha-supervisor.service",
            payload["message"],
        )

        output = StringIO()
        with redirect_stdout(output):
            cli_code = control_main(
                ["control", "supervisor", "install", "--dry-run"], env=self.env,
            )
        self.assertEqual(cli_code, 0)
        self.assertTrue(output.getvalue().startswith(f"would write {path}:\n[Unit]\n"))
        self.assertNotIn("asha control supervisor:", output.getvalue())
        self.assertFalse(path.exists())

    def test_service_status_uses_is_enabled_and_is_active(self) -> None:
        path = supervisor_service_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_text(self.expected_unit(), encoding="utf-8")

        def runner(argv, **_kwargs):
            self.calls.append(list(argv))
            returncode = 3 if argv[2] == "is-active" else 0
            return subprocess.CompletedProcess(argv, returncode, b"", b"")

        status = supervisor_service_status(
            self.env, runner=runner, which=self.which,
        )

        self.assertEqual(status, {
            "service_present": True,
            "service_enabled": True,
            "service_active": False,
        })
        self.assertEqual(self.calls, [
            ["/usr/bin/systemctl", "--user", "is-enabled", "asha-supervisor.service"],
            ["/usr/bin/systemctl", "--user", "is-active", "asha-supervisor.service"],
        ])

    def test_service_status_fields_are_null_without_systemctl(self) -> None:
        path = supervisor_service_path(self.env)
        path.parent.mkdir(parents=True)
        path.write_text(self.expected_unit(), encoding="utf-8")

        status = supervisor_service_status(
            self.env, runner=self.runner, which=lambda _command: None,
        )

        self.assertEqual(status, {
            "service_present": None,
            "service_enabled": None,
            "service_active": None,
        })
        self.assertEqual(self.calls, [])

        output = StringIO()
        with mock.patch(
            "lib.control.supervisor_service.shutil.which",
            return_value=None,
        ), redirect_stdout(output):
            code = control_main(
                ["control", "supervisor", "status", "--json"], env=self.env,
            )
        self.assertEqual(code, 1)
        payload = json.loads(output.getvalue())
        self.assertIsNone(payload["service_present"])
        self.assertIsNone(payload["service_enabled"])
        self.assertIsNone(payload["service_active"])

        output = StringIO()
        with mock.patch(
            "lib.control.supervisor_service.shutil.which",
            return_value=None,
        ), redirect_stdout(output):
            control_main(["control", "supervisor", "status"], env=self.env)
        self.assertIn(
            "service present=unknown, enabled=unknown, active=unknown",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()


class ReadOnlyObservationTests(ControlFixture, unittest.TestCase):

    def retained(self):
        from lib.control import supervisor_service as daemon
        daemon._write_status(self.config, {
            "pid": os.getpid(), "process_identity": process_identity(os.getpid()),
            "started_at": now_text(), "last_tick_at": None, "last_tick_summary": None,
        })

    def test_readonly_open_observes_the_same_owned_lock(self):
        import errno
        from lib.control import supervisor_service as daemon
        self.retained()
        real_open = os.open
        attempted = []

        def readonly(path, flags, *args, **kwargs):
            if path == "supervisor.lock":
                attempted.append(flags)
                if flags & (os.O_RDWR | os.O_WRONLY | os.O_CREAT):
                    raise OSError(errno.EROFS, "Read-only file system")
            return real_open(path, flags, *args, **kwargs)

        with daemon._exclusive_lock(self.config) as held:
            self.assertTrue(held)
            with mock.patch.object(daemon.os, "open", side_effect=readonly):
                value, code = daemon.supervisor_status(self.config)
        self.assertEqual(code, 0)
        self.assertEqual(value["status"], "running")
        self.assertTrue(value["lock_held"])
        self.assertTrue(attempted)
        self.assertTrue(all(flags & os.O_ACCMODE == os.O_RDONLY for flags in attempted))

    def test_faults_are_unavailable_and_never_launch_signal_or_install(self):
        import errno
        from lib.control.harness import HarnessError
        from lib.control import supervisor_service as daemon
        self.retained()
        faults = [OSError(errno.EROFS, "readonly"), PermissionError("denied"),
                  OSError(errno.EOPNOTSUPP, "unsupported flock"),
                  OSError(errno.EBADF, "platform requires writable fd")]
        for fault in faults:
            with self.subTest(fault=fault), mock.patch.object(daemon, "_lock_held", side_effect=fault), \
                    mock.patch.object(daemon.subprocess, "Popen") as spawn, \
                    mock.patch.object(daemon.os, "kill") as kill, \
                    mock.patch.object(daemon, "_write_service") as install:
                value, code = daemon.supervisor_status(self.config)
                self.assertEqual((value["status"], value["running"], code), ("unavailable", None, 2))
                self.assertEqual(daemon.start_supervisor(self.config, self.env)[1], 2)
                self.assertEqual(daemon.stop_supervisor(self.config)[1], 2)
                self.assertEqual(daemon.install_supervisor_service(self.config, self.env)[1], 2)
                spawn.assert_not_called(); kill.assert_not_called(); install.assert_not_called()
        with mock.patch.object(daemon, "verify_process", side_effect=HarnessError("proc denied")):
            self.assertEqual(daemon.supervisor_status(self.config)[1], 2)

    def test_missing_status_or_invisible_pid_with_held_lock_is_not_stopped(self):
        from lib.control import supervisor_service as daemon
        with daemon._exclusive_lock(self.config):
            self.assertEqual(daemon.supervisor_status(self.config)[1], 2)
            self.retained()
            with mock.patch.object(daemon, "verify_process", return_value=False):
                self.assertEqual(daemon.supervisor_status(self.config)[1], 2)
        self.assertEqual(daemon.supervisor_status(self.config)[1], 2)
        with mock.patch.object(daemon, "verify_process", return_value=False):
            self.assertEqual(daemon.supervisor_status(self.config)[1], 1)

    def test_live_process_without_lock_never_authorizes_duplicate_start(self):
        from lib.control import supervisor_service as daemon
        self.retained()
        with mock.patch.object(daemon.subprocess, "Popen") as spawn:
            value, code = daemon.start_supervisor(self.config, self.env)
        self.assertEqual((value["status"], code), ("unavailable", 2))
        spawn.assert_not_called()

    def test_a_symlinked_lock_is_followed_not_refused(self):
        # The lock's no-follow and inode re-checks only defended Control's
        # private state against the trusted local user (threat model,
        # 2026-10-05); the flock and the process identity still decide.
        from lib.control import supervisor_service as daemon
        config = self.config
        lock = daemon.supervisor_lock_path(config)
        lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.root / "elsewhere-lock"
        target.write_text(""); target.chmod(0o600)
        lock.symlink_to(target)
        self.assertEqual(daemon.supervisor_status(config)[1], 1)
        self.retained()
        with daemon._exclusive_lock(config) as held:
            self.assertTrue(held)
            value, code = daemon.supervisor_status(config)
        self.assertEqual((value["status"], value["lock_held"], code), ("running", True, 0))
        self.assertTrue(lock.is_symlink())
