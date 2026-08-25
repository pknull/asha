"""Validated harness launch and Linux process-identity primitives."""

from __future__ import annotations

import errno
import os
import re
import unicodedata
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .config import is_canonical_absolute_path
from .model import ModelError, canonical_uuid
from .text import terminal_text_is_complete


HARNESSES = frozenset({"claude", "codex", "copilot", "opencode"})

# Visible-screen markers that mean the harness is waiting for a person. Codex
# raises approval prompts in its own TUI without any hook, so Control cannot
# learn `needs-input` from events for it; the owned pane's visible tail is the
# only signal. Claude reports permission requests through hooks and needs no
# marker. Matching is exact substring on the pane's last visible lines.
INPUT_PROMPT_MARKERS: dict[str, tuple[str, ...]] = {
    "codex": (
        "Press enter to confirm or esc to cancel",
        "Would you like to run the following command?",
        "Do you trust the contents of this directory",
    ),
    # Claude Code's per-directory trust dialog fires in every fresh Control
    # workspace, and its permission prompts share one footer line. Without
    # these a Claude worker waits at a prompt while the pane still looks alive.
    "claude": (
        "Is this a project you created or one you trust?",
        "Enter to confirm \u00b7 Esc to cancel",
        "Do you want to proceed?",
    ),
    "copilot": (),
    "opencode": (),
}
INPUT_PROMPT_TAIL_LINES = 12
# Graceful end-of-session composer commands per interactive harness. Sent only
# by an explicit operator action (the close-worker key): the operator types
# through the TUI; the controller itself still never writes into a pane.
QUIT_SEQUENCES: dict[str, str] = {
    "claude": "/exit",
    "codex": "/quit",
}
# Harnesses with a real headless (one-turn, exits-on-completion) mode. A
# headless worker's exit is structural, so its seal never waits on a human.
HEADLESS_HARNESSES = frozenset({"claude", "codex"})
PROC_ROOT = Path("/proc")
MAX_PROC_BYTES = 64 * 1024
_BOOT_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.ASCII,
)
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_ROLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)


class HarnessError(ValueError):
    """A harness launch or process-identity precondition failed."""


def _has_unicode_control(value: str) -> bool:
    return any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in value)


def _validate_pid(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HarnessError("process id is invalid")
    return value


def _canonical_path(value: Any, message: str) -> Path:
    if not isinstance(value, Path):
        raise HarnessError(message)
    text = str(value)
    if (_has_unicode_control(text) or
            not is_canonical_absolute_path(text, resolved=True)):
        raise HarnessError(message)
    return value


def _read_bounded(path: Path, *, missing_is_none: bool) -> bytes | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_PROC_BYTES + 1)
    except FileNotFoundError:
        if missing_is_none:
            return None
        raise HarnessError("required proc identity file is missing") from None
    except OSError as exc:
        if missing_is_none and exc.errno == errno.ESRCH:
            return None
        raise HarnessError(f"cannot read proc identity file: {exc}") from exc
    if len(raw) > MAX_PROC_BYTES:
        raise HarnessError("proc identity file exceeds the bounded read limit")
    return raw


def validate_harness(name: Any) -> str:
    if not isinstance(name, str) or name not in HARNESSES:
        raise HarnessError("unsupported harness")
    return name


def validate_role(role: Any) -> str:
    if not isinstance(role, str) or _ROLE.fullmatch(role) is None:
        raise HarnessError("run role uses an invalid restricted grammar")
    return role


def launch_argv(
    asha_root: Path, harness: str, extra: Sequence[str] = (),
    *, headless: bool = False,
) -> list[str]:
    root = _canonical_path(asha_root, "Asha root is not an absolute canonical path")
    harness = validate_harness(harness)
    executable = root / "bin" / "asha"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise HarnessError("Asha launcher is missing or not executable")
    if isinstance(extra, (str, bytes)) or not isinstance(extra, Sequence):
        raise HarnessError("harness extra arguments are invalid")
    arguments = list(extra)
    if any(not isinstance(item, str) or not terminal_text_is_complete(item)
           for item in arguments):
        raise HarnessError("harness extra arguments are invalid")
    if arguments and arguments[0].startswith("-"):
        raise HarnessError("the first goal argument must not begin with '-'")
    if headless:
        if harness not in HEADLESS_HARNESSES:
            raise HarnessError(f"{harness} has no headless mode")
        if harness == "claude":
            # Print mode runs one full agentic turn and exits. Permissions are
            # bypassed deliberately: the workspace is isolated, hard scope and
            # read-only review are enforced by the seal, and a headless run
            # cannot answer a prompt.
            return [
                str(executable), harness, "-p", *arguments,
                "--permission-mode", "bypassPermissions",
            ]
        return [str(executable), harness, "exec", *arguments]
    return [str(executable), harness, *arguments]


def controller_env(
    *, task_id: str, run_id: str, state_dir: Path, asha_home: Path | None = None,
) -> dict[str, str]:
    try:
        task_id = canonical_uuid(task_id)
        run_id = canonical_uuid(run_id)
    except ModelError as exc:
        raise HarnessError("control identifiers must be canonical UUIDs") from exc
    state = _canonical_path(
        state_dir, "control state directory is not an absolute canonical path",
    )
    environment = {
        "ASHA_CONTROL_TASK_ID": task_id,
        "ASHA_CONTROL_RUN_ID": run_id,
        "ASHA_CONTROL_STATE_DIR": str(state),
        "ASHA_CONTROL_MANAGED": "1",
        # Control-launched workers run from an assignment brief, not a persona:
        # the launcher skips the identity render and keeps the operational layer.
        "ASHA_PERSONA": "0",
    }
    if asha_home is not None:
        # Panes inherit the tmux SERVER's environment, not the controller's: a
        # non-default ASHA_HOME would otherwise desync the worker's derived
        # tasks_dir and every `asha control event` it sends would be refused.
        environment["ASHA_HOME"] = str(_canonical_path(
            asha_home, "asha home is not an absolute canonical path",
        ))
    return environment


def boot_id() -> str:
    raw = _read_bounded(
        PROC_ROOT / "sys" / "kernel" / "random" / "boot_id",
        missing_is_none=False,
    )
    assert raw is not None
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise HarnessError("boot id is malformed") from exc
    if _BOOT_ID.fullmatch(value) is None:
        raise HarnessError("boot id is malformed")
    return value


def _process_stat_fields(pid: int) -> list[bytes] | None:
    pid = _validate_pid(pid)
    raw = _read_bounded(PROC_ROOT / str(pid) / "stat", missing_is_none=True)
    if raw is None:
        return None
    closing = raw.rfind(b")")
    expected_prefix = f"{pid} (".encode("ascii")
    if (not raw.startswith(expected_prefix) or closing < len(expected_prefix) or
            raw[closing + 1:closing + 2] != b" "):
        raise HarnessError("process stat line is malformed")
    fields = raw[closing + 2:].split()
    if len(fields) < 20:
        raise HarnessError("process stat line is malformed")
    return fields


def _stat_integer(fields: list[bytes], index: int) -> int:
    try:
        value = int(fields[index])
    except (ValueError, IndexError) as exc:
        raise HarnessError("process stat line is malformed") from exc
    if value < 0:
        raise HarnessError("process stat line is malformed")
    return value


def process_start_ticks(pid: int) -> int | None:
    fields = _process_stat_fields(pid)
    if fields is None:
        return None
    # The suffix begins at field 3, so field 22 is suffix index 19.
    return _stat_integer(fields, 19)


def process_identity(pid: int) -> str | None:
    ticks = process_start_ticks(pid)
    if ticks is None:
        return None
    identity = f"boot:{boot_id()}:start:{ticks}"
    if len(identity) > 200 or _has_unicode_control(identity):
        raise HarnessError("process identity is invalid")
    return identity


def verify_process(pid: int, expected_identity: str) -> bool:
    if (not isinstance(expected_identity, str) or not expected_identity or
            len(expected_identity) > 200 or _has_unicode_control(expected_identity)):
        return False
    return process_identity(pid) == expected_identity


def pane_ancestry_ok(pane_pid: int, server_pid: int) -> bool:
    pane_pid = _validate_pid(pane_pid)
    server_pid = _validate_pid(server_pid)
    fields = _process_stat_fields(pane_pid)
    if fields is None:
        return False
    # The suffix begins at field 3, so field 4 is suffix index 1.
    return _stat_integer(fields, 1) == server_pid


def caller_descends_from(
    ancestor_pid: int, *, start_pid: int | None = None, limit: int = 64,
) -> bool:
    """True when the calling process has ``ancestor_pid`` in its parent chain.

    Walks ``/proc`` parent links from ``start_pid`` (default: this process) for
    at most ``limit`` hops; a missing process or reaching init ends the walk.
    """
    ancestor_pid = _validate_pid(ancestor_pid)
    pid = os.getpid() if start_pid is None else _validate_pid(start_pid)
    for _ in range(limit):
        if pid == ancestor_pid:
            return True
        fields = _process_stat_fields(pid)
        if fields is None:
            return False
        parent = _stat_integer(fields, 1)
        if parent <= 1 or parent == pid:
            return False
        pid = parent
    return False


def stop_signal_allowed(
    *,
    pid: int,
    expected_identity: str,
    pane_pid: int,
    server_pid: int,
    pane_dead: bool,
) -> bool:
    if pane_dead is not False or pid != pane_pid:
        return False
    try:
        return (
            verify_process(pid, expected_identity)
            and pane_ancestry_ok(pane_pid, server_pid)
        )
    except HarnessError:
        return False
