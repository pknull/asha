"""Doctor checks for Codex's shared app-server daemon (#100)."""
import tempfile
import time
import unittest
from pathlib import Path

from lib.control import codex_daemon

ROOT = Path(__file__).resolve().parents[2]


def fake_process(proc: Path, pid: int, argv, env):
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    (entry / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")


def fake_codex(directory: Path, version: str, *, no_daemon: bool) -> Path:
    path = directory / f"codex-{version}"
    help_line = "      --no-daemon\n" if no_daemon else ""
    path.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = --version ]; then echo "codex-cli {version}"; exit 0; fi\n'
        f'if [ "$1" = --help ]; then printf "Usage: codex\\n{help_line}"; exit 0; fi\n'
        "exit 3\n", encoding="ascii")
    path.chmod(0o700)
    return path


class DaemonIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.proc = Path(temp.name) / "proc"
        self.proc.mkdir()

    def test_daemon_and_updater_carrying_a_hub_identity_are_reported(self):
        sid = "d906b791-c6f7-4965-a46f-8caf65918d9c"
        fake_process(self.proc, 808072, ["/x/bin/codex", "app-server", "--listen", "unix://", "--managed-daemon"],
                     {"ASHA_HUB_SESSION_ID": sid, "PATH": "/bin"})
        fake_process(self.proc, 1125992, ["codex", "app-server", "daemon", "pid-update-loop"],
                     {"ASHA_HUB_SESSION_ID": sid})
        # A clean daemon, a private structured app-server and a TUI are not leaks.
        fake_process(self.proc, 900, ["/x/bin/codex", "app-server", "--managed-daemon"], {"PATH": "/bin"})
        fake_process(self.proc, 901, ["codex", "app-server", "--listen", "stdio://"], {"ASHA_HUB_SESSION_ID": sid})
        fake_process(self.proc, 902, ["codex", "--no-daemon"], {"ASHA_HUB_SESSION_ID": sid})
        # Something else whose arguments merely look like a daemon is not Codex.
        fake_process(self.proc, 903, ["/usr/bin/printf", "app-server", "--managed-daemon"], {"ASHA_HUB_SESSION_ID": sid})
        fake_process(self.proc, 904, ["python3", "codex", "app-server", "--managed-daemon"], {"ASHA_HUB_SESSION_ID": sid})
        (self.proc / "self").mkdir()
        leaks = codex_daemon.daemon_identity_leaks(self.proc)
        self.assertEqual([(leak["pid"], leak["role"], leak["session_id"]) for leak in leaks],
                         [(808072, "daemon", sid), (1125992, "updater", sid)])

    def test_unreadable_or_vanished_processes_are_skipped(self):
        (self.proc / "77").mkdir()
        self.assertEqual(codex_daemon.daemon_identity_leaks(self.proc), [])
        self.assertEqual(codex_daemon.daemon_identity_leaks(self.proc / "missing"), [])


class LaunchCheckTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)

    def test_version_parsing(self):
        self.assertEqual(codex_daemon.codex_version(fake_codex(self.dir, "0.157.1", no_daemon=True)), (0, 157, 1))
        self.assertIsNone(codex_daemon.codex_version(self.dir / "absent"))

    def test_daemon_era_codex_is_launched_without_the_daemon(self):
        codex = fake_codex(self.dir, "0.157.0", no_daemon=True)
        self.assertEqual(codex_daemon.launch_problems(ROOT, str(codex)), [])

    def test_daemon_era_codex_without_the_flag_is_a_problem(self):
        codex = fake_codex(self.dir, "0.158.0", no_daemon=False)
        problems = codex_daemon.launch_problems(ROOT, str(codex))
        self.assertEqual(len(problems), 1)
        self.assertIn("--no-daemon", problems[0])

    def test_pre_daemon_codex_needs_nothing(self):
        codex = fake_codex(self.dir, "0.155.1", no_daemon=False)
        self.assertEqual(codex_daemon.launch_problems(ROOT, str(codex)), [])

    def scratch_root(self, *, drop_call_site=False, break_helper=False):
        """A checkout whose files are the real ones, except an edited bin/asha or helper."""
        root = self.dir / "root"
        (root / "bin").mkdir(parents=True)
        for entry in ROOT.iterdir():
            if entry.name not in {"bin", "lib", ".git", ".jj", "local"}:
                (root / entry.name).symlink_to(entry)
        for entry in (ROOT / "bin").iterdir():
            if entry.name != "asha":
                (root / "bin" / entry.name).symlink_to(entry)
        (root / "lib").mkdir()
        for entry in (ROOT / "lib").iterdir():
            if entry.name != "codex-launch.sh":
                (root / "lib" / entry.name).symlink_to(entry)
        launcher = (ROOT / "bin/asha").read_text()
        if drop_call_site:
            kept = [line for line in launcher.splitlines(keepends=True)
                    if "asha_codex_launch_args" not in line and "ASHA_CODEX_LAUNCH_ARGS" not in line]
            self.assertLess(len(kept), len(launcher.splitlines()))
            launcher = "".join(kept)
        (root / "bin/asha").write_text(launcher)
        (root / "bin/asha").chmod(0o755)
        helper = (ROOT / "lib/codex-launch.sh").read_text()
        if break_helper:
            helper += '\nasha_codex_launch_args() { shift; ASHA_CODEX_LAUNCH_ARGS=("$@"); }\n'
        (root / "lib/codex-launch.sh").write_text(helper)
        return root

    def test_the_real_wrapper_is_exercised_with_profile_and_resume_arguments(self):
        codex = fake_codex(self.dir, "0.157.0", no_daemon=True)
        self.assertEqual(codex_daemon.launch_problems(self.scratch_root(), str(codex)), [])

    def test_a_removed_wrapper_call_site_is_a_problem(self):
        codex = fake_codex(self.dir, "0.157.0", no_daemon=True)
        problems = codex_daemon.launch_problems(self.scratch_root(drop_call_site=True), str(codex))
        self.assertTrue(problems)
        self.assertTrue(all("bin/asha" in problem and "--no-daemon" in problem for problem in problems))

    def test_a_broken_helper_is_a_problem(self):
        codex = fake_codex(self.dir, "0.157.0", no_daemon=True)
        problems = codex_daemon.launch_problems(self.scratch_root(break_helper=True), str(codex),
                                                room_argv=lambda: ["asha", "codex", "--no-daemon", "P"])
        self.assertTrue(problems)

    def test_unrecognised_version_still_checks_a_flag_capable_codex(self):
        codex = fake_codex(self.dir, "0.157.0", no_daemon=True)
        codex.write_text(codex.read_text().replace('echo "codex-cli 0.157.0"', 'echo "codex 9"'))
        problems = codex_daemon.launch_problems(self.scratch_root(drop_call_site=True), str(codex),
                                                room_argv=lambda: ["asha", "codex", "P"])
        self.assertGreaterEqual(len(problems), 2)


class RejectionSummaryTests(unittest.TestCase):
    def test_counts_recent_rejections_only(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "rejected.jsonl"
            now = time.time()
            log.write_text(
                f'{{"at": {now - 90000}, "error": "old"}}\nnot json\n'
                f'{{"at": {now - 60}, "error": "stale or inactive session reporter"}}\n', encoding="ascii")
            self.assertEqual(codex_daemon.recent_rejections(log, now=now),
                             (1, "stale or inactive session reporter"))
            self.assertEqual(codex_daemon.recent_rejections(Path(temp) / "absent", now=now), (0, None))


if __name__ == "__main__":
    unittest.main()
