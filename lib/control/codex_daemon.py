"""Doctor checks for Codex's shared app-server daemon (#100).

Codex 0.157 runs interactive threads, and the hooks they fire, inside a shared
``codex app-server --managed-daemon``. That daemon, and its pid-update-loop
updater, keep the environment of whichever process first spawned them. When
that process was an Asha session, every later Codex thread's hooks report
under the frozen ASHA_HUB_SESSION_ID. Asha launches Codex TUIs with
``--no-daemon``; these checks find a leaked identity and a launch path that
would still join the daemon.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DAEMON_VERSION = (0, 157, 0)
_VERSION = re.compile(rb"codex-cli (\d+)\.(\d+)\.(\d+)")
_IDENTITY = b"ASHA_HUB_SESSION_ID="


def _role(argv: list[bytes]) -> str | None:
    # Only a Codex executable is a Codex daemon: arguments alone can be mimicked.
    if not argv or argv[0].rsplit(b"/", 1)[-1] != b"codex" or b"app-server" not in argv[1:]:
        return None
    if b"--managed-daemon" in argv:
        return "daemon"
    if b"pid-update-loop" in argv:
        return "updater"
    return None


def daemon_identity_leaks(proc_root: Path = Path("/proc")) -> list[dict]:
    """Running Codex daemons/updaters whose environment carries a hub identity."""
    leaks = []
    try:
        entries = sorted((p for p in proc_root.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    except OSError:
        return []
    for entry in entries:
        try:
            role = _role((entry / "cmdline").read_bytes().split(b"\0"))
            if role is None:
                continue
            environ = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        for item in environ:
            if item.startswith(_IDENTITY):
                session = item[len(_IDENTITY):].decode("ascii", "replace")[:64]
                leaks.append({"pid": int(entry.name), "role": role, "session_id": session})
                break
    return leaks


def _run(argv: list[str]) -> bytes | None:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def codex_version(executable) -> tuple[int, ...] | None:
    output = _run([str(executable), "--version"])
    match = _VERSION.search(output or b"")
    return tuple(int(part) for part in match.groups()) if match else None


# Representative TUI launches through the real bin/asha codex wrapper, and
# the argument each must end up carrying. Profile values and conversation
# names that spell a subcommand are included on purpose.
_WRAPPER_CASES = (
    (["-p", "review", "hello"], ["--no-daemon", "-p", "review", "hello"]),
    (["resume", "review"], ["resume", "--no-daemon", "review"]),
)
_STUB = """#!/bin/sh
if [ "$1" = --help ]; then printf '      --no-daemon\\n'; exit 0; fi
for arg in "$@"; do printf '%s\\n' "$arg"; done > "$ASHA_DOCTOR_CAPTURE"
"""


def _wrapper_problems(asha_root: Path) -> list[str]:
    """Run the real bin/asha codex wrapper against a stub Codex in a scratch home.

    Nothing touches the live home: HOME and ASHA_HOME are temporary, the stub
    stands in for Codex, and an install manifest satisfies the launcher's
    freshness gate without installing anything.
    """
    root = os.path.realpath(asha_root)
    launcher = Path(root) / "bin" / "asha"
    problems = []
    with tempfile.TemporaryDirectory(prefix="asha-codex-doctor-") as scratch:
        home = Path(scratch) / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "config.toml").write_text("\n")
        manifests = home / ".asha" / "install-manifests"
        manifests.mkdir(parents=True)
        (manifests / "codex.json").write_text(json.dumps({"artifacts": [{"source": root + "/plugins/doctor"}]}))
        stub = Path(scratch) / "codex"
        stub.write_text(_STUB)
        stub.chmod(0o700)
        capture = Path(scratch) / "argv"
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home),
               "ASHA_HOME": str(home / ".asha"), "ASHA_CODEX_CMD": str(stub),
               "ASHA_DOCTOR_CAPTURE": str(capture), "LANG": "C.UTF-8", "TERM": "dumb"}
        for args, expected in _WRAPPER_CASES:
            capture.unlink(missing_ok=True)
            try:
                subprocess.run(["bash", str(launcher), "codex", *args], env=env, cwd=scratch,
                               stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
                argv = capture.read_text().splitlines()
            except (OSError, subprocess.SubprocessError):
                argv = []
            if argv[-len(expected):] != expected:
                problems.append(f"bin/asha codex {' '.join(args)} would launch Codex without --no-daemon "
                                f"(got: {' '.join(argv[-6:]) or 'no launch'})"[:300])
    return problems


def _room_args(asha_root: Path) -> list[str]:
    from .rooms import room_launch_argv
    return room_launch_argv(Path(asha_root).resolve(), "codex", "doctor probe")


def launch_problems(asha_root, executable: str, *, room_argv=None) -> list[str]:
    """Why an Asha-owned Codex TUI launch would join the shared daemon, if it would."""
    version = codex_version(executable)
    help_text = _run([executable, "--help"]) or b""
    supports = b"--no-daemon" in help_text
    # An unrecognised --version is not proof of an old Codex: whenever the flag
    # exists, Asha's launches must use it.
    if not supports and (version is None or version < DAEMON_VERSION):
        return []
    label = ".".join(map(str, version)) if version else "(unrecognised version)"
    if not supports:
        return [f"codex {label} runs a shared app-server daemon but does not document --no-daemon; "
                "Codex hooks cannot be kept in their own pane"]
    problems = _wrapper_problems(Path(asha_root))
    try:
        room = room_argv() if room_argv else _room_args(Path(asha_root))
    except Exception as exc:  # the doctor reports; it never crashes on a probe
        room = []
        problems.append(f"Room launch argv unavailable: {str(exc)[:200]}")
    else:
        if "--no-daemon" not in room:
            problems.append(f"Control Rooms would launch codex {label} without --no-daemon")
    return problems


def recent_rejections(log: Path, *, now: float | None = None, window: float = 86400) -> tuple[int, str | None]:
    """Count rejected hub events in the window and return the newest error."""
    now = time.time() if now is None else now
    count, last = 0, None
    try:
        lines = log.read_text(encoding="ascii", errors="replace").splitlines()
    except OSError:
        return 0, None
    for line in lines:
        try:
            entry = json.loads(line)
            at = float(entry["at"])
        except (ValueError, KeyError, TypeError):
            continue
        if now - at <= window:
            count += 1
            last = str(entry.get("error") or "")[:200]
    return count, last


def doctor(asha_root: Path, executable: str, *, rejection_log: Path | None = None,
           proc_root: Path = Path("/proc")) -> int:
    failed = False
    for problem in launch_problems(asha_root, executable):
        print(f"FAIL  {problem}")
        failed = True
    leaks = daemon_identity_leaks(proc_root)
    for leak in leaks:
        print(f"FAIL  Codex app-server {leak['role']} pid {leak['pid']} carries ASHA_HUB_SESSION_ID="
              f"{leak['session_id']}; its hooks report under that session. Stop it "
              "(codex app-server daemon stop) once no Codex session depends on it")
        failed = True
    if not failed:
        print("PASS  Codex TUI launches run without the shared app-server daemon; no daemon carries a hub identity")
    if rejection_log is not None:
        count, last = recent_rejections(rejection_log)
        if count:
            # Informational: a pane dying during close also refuses its last hook.
            print(f"NOTE  {count} Control hook event(s) refused in the last 24h (latest: {last}); see {rejection_log}")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: codex_daemon ASHA_ROOT CODEX_EXECUTABLE [REJECTION_LOG]", file=sys.stderr)
        return 2
    log = Path(argv[2]) if len(argv) == 3 else None
    return doctor(Path(argv[0]), argv[1], rejection_log=log)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
