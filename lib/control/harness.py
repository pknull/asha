"""Harness names and Linux process-identity primitives."""

from __future__ import annotations

import errno
import os
import re
import unicodedata
from pathlib import Path
from typing import Any



HARNESSES = frozenset({"claude", "codex", "copilot", "opencode"})

# Harnesses with a one-turn headless mode: the structured-session owners the
# supervisor starts run these (its unit pins their commands).
HEADLESS_HARNESSES = frozenset({"claude", "codex"})
# Harnesses whose Asha launches run commands inside a native sandbox (Codex
# Rooms, workers and chair; subtraction panel E5). Claude, Copilot and OpenCode
# run unsandboxed under native permissions. Session operator verbs refuse a
# non-chair caller on one of these (K4, 2026-10-05): run outside the sandbox
# through an allow rule, they would hand it authority the sandbox withholds.
SANDBOXED_HARNESSES = frozenset({"codex"})
PROC_ROOT = Path("/proc")
MAX_PROC_BYTES = 64 * 1024
_BOOT_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.ASCII,
)
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
class HarnessError(ValueError):
    """A harness launch or process-identity precondition failed."""


def _has_unicode_control(value: str) -> bool:
    return any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in value)


def _validate_pid(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HarnessError("process id is invalid")
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


def caller_descends_from(
    ancestor_pid: int, *, start_pid: int | None = None, limit: int = 64,
    require_complete: bool = False,
) -> bool:
    """True when the calling process has ``ancestor_pid`` in its parent chain.

    Walks ``/proc`` parent links from ``start_pid`` (default: this process) for
    at most ``limit`` hops. Negative authorization checks must require a complete
    walk: disappearing links, cycles and exhausted bounds are then errors.
    """
    ancestor_pid = _validate_pid(ancestor_pid)
    pid = os.getpid() if start_pid is None else _validate_pid(start_pid)
    for _ in range(limit):
        if pid == ancestor_pid:
            return True
        fields = _process_stat_fields(pid)
        if fields is None:
            if require_complete:
                raise HarnessError("process ancestry is unavailable")
            return False
        parent = _stat_integer(fields, 1)
        if parent == pid:
            if require_complete:
                raise HarnessError("process ancestry contains a cycle")
            return False
        if parent <= 1:
            return False
        pid = parent
    if require_complete:
        raise HarnessError("process ancestry exceeds the inspection limit")
    return False

