"""Argument-safe adapter for Asha Control's tmux seam."""

from __future__ import annotations

import os
import re
import shlex
import sys
import time
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import is_canonical_absolute_path
from .process import bounded_process, capture_bytes
from .text import terminal_text_is_complete


MAX_OUTPUT_BYTES = 64 * 1024
INVENTORY_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
_PANE_ID = re.compile(r"%[0-9]+", re.ASCII)
_SESSION_ID = re.compile(r"\$[0-9]+", re.ASCII)
_WINDOW_ID = re.compile(r"@[0-9]+", re.ASCII)
_ROOM_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.ASCII,
)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_USER_OPTION = re.compile(r"@[a-z][a-z0-9_]{0,63}", re.ASCII)
_ENVIRONMENT_KEY = re.compile(r"[A-Z][A-Z0-9_]{0,63}", re.ASCII)
_RESULT_STAGING_TOKEN = re.compile(r"[0-9a-f]{64}", re.ASCII)
_PERCENT = re.compile(r"(?:[1-9][0-9]?|100)%", re.ASCII)
_SESSION_PREFIX = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30})?", re.ASCII)
# Every other path this adapter handles reaches tmux as one argv element and is
# never re-parsed.  A pipe-pane destination is different: it is interpolated
# into a command string that tmux first expands as a FORMAT and then hands to
# `/bin/sh -c`.  So the spelling is restricted to characters that mean nothing
# to either expander before it is quoted for the shell.  '#' is excluded for
# the format pass exactly as '$' and backtick are for the shell pass.
_PIPE_PATH = re.compile(r"[A-Za-z0-9 ./_@+,:=-]{1,4096}", re.ASCII)
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})
_POPUP_CHILD_EXEC = (
    "(__import__('os').environ.__setitem__('TMUX',''))or"
    "(__import__('os').execvp(__import__('sys').argv[1],"
    "__import__('sys').argv[1:]))"
)
_PANE_FORMAT = (
    "#{pane_id}\t#{pane_pid}\t#{pane_dead}\t#{pane_dead_status}\t"
    # Keep the final compatibility field empty. Terminal output can change the
    # title to arbitrary bytes, including delimiters and invalid UTF-8; do not
    # fetch it into the supervision record at all.
    "#{pane_dead_signal}\t#{session_name}\t#{window_name}\t"
)
_INVENTORY_SESSION_OPTIONS = (
    "@asha_managed", "@asha_task_id", "@asha_room_session_id",
)
_INVENTORY_PANE_OPTIONS = (
    "@asha_run_id", "@asha_room_id", "@asha_room_project_id",
)
_INVENTORY_FORMAT = "\t".join((
    "#{pid}", "#{session_name}", "#{session_id}", "#{window_name}",
    "#{window_id}", "#{pane_id}", "#{pane_pid}", "#{pane_dead}",
    "#{pane_dead_status}", "#{pane_dead_signal}", "#{pane_active}",
    *tuple(f"#{{{option}}}" for option in _INVENTORY_SESSION_OPTIONS),
    *tuple(f"#{{{option}}}" for option in _INVENTORY_PANE_OPTIONS),
))
# Idle-input injection (#96): bounded screen read and one submitted line.
_INPUT_SCREEN_LINES = 80
_INPUT_TEXT_LIMIT = 8192
_INPUT_SUBMIT_DELAY = 0.3
_INPUT_DEADLINE = 5
# Paste to Enter must fit well inside the native hooks' own budgets, so a
# native event reported meanwhile is still being recorded, not long gone.
_INPUT_BUDGET = 1.5
# Unlike _ROOM_REFUSAL, never run-shell here: a failing run-shell leaves the
# target pane in view-mode, in front of whoever attaches next.
_INPUT_REFUSAL = "display-message -p ASHA_ROOM_OWNERSHIP_REFUSED"
_ROOM_REFUSAL = (
    'display-message -p ASHA_ROOM_OWNERSHIP_REFUSED ; run-shell "exit 66"'
)
# Room input fence (#96). Two exact counters live as options of the owned
# pane itself, the most specific tmux scope, so no window, session or global
# value can stand in for them:
# - the attach generation, bumped by the Room session's hooks on every client
#   attach or switch-in (session_last_attached has one-second resolution);
# - the event sequence, bumped by control-event.sh before each native event is
#   reported, so an event whose report is pending or was lost stays visible.
ATTACH_GENERATION_OPTION = "@asha_attach_gen"
EVENT_SEQUENCE_OPTION = "@asha_event_seq"
# Canonical ASCII integers only, below a bound that tmux arithmetic (double
# precision) still counts exactly. Reaching it refuses instead of freezing.
FENCE_LIMIT = 1_000_000_000
_FENCE_VALUE = re.compile(r"0|[1-9][0-9]*", re.ASCII)
_ATTACH_HOOKS = ("client-attached", "client-session-changed")


def _attach_hook_command(pane: str) -> str:
    return (f"set-option -p -t {pane} -F {ATTACH_GENERATION_OPTION} "
            f"'#{{e|+:#{{{ATTACH_GENERATION_OPTION}}},1}}'")


def _attach_hook_shown(pane: str) -> frozenset[str]:
    """How ``show-hooks`` may render the installed command (tmux re-quotes it)."""
    increment = f'"#{{e|+:#{{{ATTACH_GENERATION_OPTION}}},1}}"'
    return frozenset({
        f'set-option -Fp -t "{pane}" {ATTACH_GENERATION_OPTION} {increment}',
        f'set-option -pF -t "{pane}" {ATTACH_GENERATION_OPTION} {increment}',
    })


def _fence_value(value: str) -> tuple[str | None, str]:
    """``(value, "")`` for a usable counter, else ``(None, why)``."""
    if not isinstance(value, str) or _FENCE_VALUE.fullmatch(value) is None:
        return None, "is missing or not a canonical integer"
    if len(value) > 18 or int(value) >= FENCE_LIMIT:
        return None, "is exhausted (at its safe bound)"
    return value, ""


class TmuxError(ValueError):
    """A tmux precondition, invocation, or identity check failed."""


# Why an owned-pane action refused a person-sensitive step (#96). ``partial``
# means text was pasted but not submitted; every other category typed nothing.
# ``unfenced`` means the Room's fence counters or hooks are missing, invalid
# or exhausted: typing cannot be guarded. ``stale`` means a native event began
# (its sequence moved) after the probe; nothing was typed.
ROOM_INPUT_REFUSALS = frozenset({"attached", "mode", "ownership", "partial", "stale", "unfenced"})


class RoomInputRefused(TmuxError):
    """An owned-pane input or detached-only kill refused; ``category`` says why."""

    def __init__(self, category: str, message: str) -> None:
        if category not in ROOM_INPUT_REFUSALS:
            raise ValueError(f"unknown room input refusal: {category}")
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class RoomInputFacts:
    """A detached pane's screen with the fence counters it was read under."""

    attached: int
    attach_generation: str
    event_sequence: str
    screen: list[str]


@dataclass(frozen=True)
class PaneFacts:
    pane_id: str
    pane_pid: int | None
    dead: bool
    dead_status: int | None
    dead_signal: int | None
    session: str
    window: str
    title: str


class TmuxInventory:
    """One immutable tmux-server sample reused by every refresh consumer.

    The inventory owns identity, ownership-option, and pane facts from one
    ``list-panes -a`` subprocess.  Screen-tail reads for a live harness and
    operator writes remain explicit calls on the source adapter; routine
    existence and ownership checks never spawn a subprocess per record.
    """

    def __init__(
        self,
        source: "TmuxAdapter",
        *,
        server_pid: int | None,
        sessions: dict[str, dict[str, Any]],
        session_ids: dict[str, str],
        panes: dict[str, dict[str, Any]],
        windows: dict[tuple[str, str], str],
    ) -> None:
        self._source = source
        self._server_pid = server_pid
        self._server_pid_checked = server_pid is not None
        self._server_pid_error: str | None = None
        self._sessions = sessions
        self._session_ids = session_ids
        self._panes = panes
        self._windows = windows
        self.executable = source.executable
        self.socket = source.socket
        self.config_file = source.config_file

    def _session(self, target: str) -> dict[str, Any]:
        selected = _validate_session_target(target)
        name = self._session_ids.get(selected, selected)
        session = self._sessions.get(name)
        if session is None:
            raise TmuxError(f"can't find session: {selected}")
        return session

    def server_pid(self) -> int:
        if self._server_pid_error is not None:
            raise TmuxError(self._server_pid_error)
        if not self._server_pid_checked:
            # An empty list-panes result cannot distinguish an absent server
            # from a pane-free test double. This remains at most one extra
            # server-wide probe, never one probe per record.
            self._server_pid_checked = True
            try:
                self._server_pid = self._source.server_pid()
            except TmuxError as exc:
                self._server_pid_error = str(exc)
                raise
        if self._server_pid is None:
            raise TmuxError("tmux inventory has no server identity")
        return self._server_pid

    def list_sessions(self) -> list[str]:
        return sorted(self._sessions)

    def session_names(self) -> list[str]:
        return self.list_sessions()

    def has_session(self, name: str) -> bool:
        selected = _validate_session_target(name)
        return (
            selected in self._sessions
            or selected in self._session_ids
        )

    def session_option(
        self, name: str, option: str, *, deadline_seconds: float = 60,
    ) -> str | None:
        del deadline_seconds
        key = _validate_user_option_key(option)
        if key not in _INVENTORY_SESSION_OPTIONS:
            raise TmuxError(
                f"session option is not present in the bulk inventory: {key}"
            )
        return self._session(name)["options"].get(key)

    def pane_option(
        self, pane_id: str, option: str, *, deadline_seconds: float = 60,
    ) -> str | None:
        del deadline_seconds
        pane = _validate_pane_id(pane_id)
        key = _validate_user_option_key(option)
        if key not in _INVENTORY_PANE_OPTIONS:
            raise TmuxError(
                f"pane option is not present in the bulk inventory: {key}"
            )
        record = self._panes.get(pane)
        if record is None:
            raise TmuxError(f"can't find pane: {pane}")
        return record["options"].get(key)

    def pane_facts(
        self, pane_id: str, *, deadline_seconds: float = 60,
    ) -> PaneFacts:
        del deadline_seconds
        pane = _validate_pane_id(pane_id)
        record = self._panes.get(pane)
        if record is None:
            raise TmuxError(f"can't find pane: {pane}")
        return record["facts"]

    def window_pane_facts(
        self, session: str, window: str, *, deadline_seconds: float = 60,
    ) -> PaneFacts:
        del deadline_seconds
        name = self._session(session)["name"]
        window_name = _validate_window_name(window)
        pane = self._windows.get((name, window_name))
        if pane is None:
            raise TmuxError(f"can't find pane for window: {name}:{window_name}")
        return self._panes[pane]["facts"]

    def session_id(
        self, pane_id: str, *, deadline_seconds: float = 60,
    ) -> str:
        del deadline_seconds
        pane = _validate_pane_id(pane_id)
        record = self._panes.get(pane)
        if record is None:
            raise TmuxError(f"can't find pane: {pane}")
        return record["session_id"]

    def pane_tail(self, pane_id: str, *, lines: int = 12) -> list[str]:
        return self._source.pane_tail(pane_id, lines=lines)

    def set_server_summary(
        self, value: str, *, deadline_seconds: float = 60,
    ) -> None:
        self._source.set_server_summary(value, deadline_seconds=deadline_seconds)

    def set_pane_option(
        self, pane_id: str, option: str, value: str, *,
        deadline_seconds: float = 60,
    ) -> None:
        self._source.set_pane_option(
            pane_id, option, value, deadline_seconds=deadline_seconds,
        )

    def set_session_option(
        self, name: str, option: str, value: str, *,
        deadline_seconds: float = 60,
    ) -> None:
        self._source.set_session_option(
            name, option, value, deadline_seconds=deadline_seconds,
        )

    def room_attach_argv(self, **kwargs) -> list[str]:
        return self._source.room_attach_argv(**kwargs)


def _has_unicode_control(value: str) -> bool:
    return any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in value)


def _validate_socket_name(value: Any) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise TmuxError("tmux socket name is invalid")
    return value


def _validate_session_name(value: Any) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise TmuxError("tmux session name is invalid")
    return value


def _validate_session_target(value: Any) -> str:
    if isinstance(value, str) and (
        _NAME.fullmatch(value) is not None or _SESSION_ID.fullmatch(value) is not None
    ):
        return value
    raise TmuxError("tmux session target is invalid")


def _validate_window_name(value: Any) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise TmuxError("tmux window name is invalid")
    return value


def _validate_pane_id(value: Any) -> str:
    if not isinstance(value, str) or _PANE_ID.fullmatch(value) is None:
        raise TmuxError("tmux pane id is invalid")
    return value


def _validate_session_id(value: Any) -> str:
    if not isinstance(value, str) or _SESSION_ID.fullmatch(value) is None:
        raise TmuxError("tmux session id is invalid")
    return value


def _validate_window_id(value: Any) -> str:
    if not isinstance(value, str) or _WINDOW_ID.fullmatch(value) is None:
        raise TmuxError("tmux window id is invalid")
    return value


def _validate_client_tty(value: Any) -> str:
    if not isinstance(value, str):
        raise TmuxError("tmux client tty is invalid")
    parts = Path(value).parts
    if (len(parts) < 3 or parts[:2] != ("/", "dev") or ".." in parts or
            len(value) > 4096 or _has_unicode_control(value)):
        raise TmuxError("tmux client tty is invalid")
    return value


def _validate_user_option_key(value: Any) -> str:
    if not isinstance(value, str) or _USER_OPTION.fullmatch(value) is None:
        raise TmuxError("tmux user option key is invalid")
    return value


def _validate_environment_key(value: Any) -> str:
    if not isinstance(value, str) or _ENVIRONMENT_KEY.fullmatch(value) is None:
        raise TmuxError("tmux environment key is invalid")
    return value


def _validate_restricted_value(value: Any) -> str:
    if (not isinstance(value, str) or len(value) > 200 or
            _has_unicode_control(value) or ";" in value or "\n" in value or
            "#{" in value):
        raise TmuxError("tmux value is invalid")
    return value


def _validate_environment_value(value: Any) -> str:
    if (not isinstance(value, str) or len(value) > 4096 or
            _has_unicode_control(value) or ";" in value or "\n" in value or
            "#{" in value):
        raise TmuxError("tmux environment value is invalid")
    return value


def _validate_start_directory(value: Any) -> str:
    if not isinstance(value, (str, Path)):
        raise TmuxError("tmux start directory is invalid")
    text = str(value)
    if (_has_unicode_control(text) or
            not is_canonical_absolute_path(text, resolved=True)):
        raise TmuxError("tmux start directory is invalid")
    return text


def _validate_argv(value: Any) -> list[str]:
    if (not isinstance(value, list) or not value or
            any(not isinstance(item, str) or not terminal_text_is_complete(item) or
                item == ";" or item.endswith(";") for item in value)):
        raise TmuxError("tmux command argv is invalid")
    return list(value)


def validate_command_argv(value: Any) -> list[str]:
    """The argv rule ``respawn`` applies, for callers that must refuse before any pane exists."""
    return _validate_argv(value)


def _validate_popup_dimension(value: Any) -> str:
    if not isinstance(value, str) or _PERCENT.fullmatch(value) is None:
        raise TmuxError("tmux popup dimension is invalid")
    return value


def _validate_session_prefix(value: Any) -> str:
    if (not isinstance(value, str) or not value.endswith("-") or
            _SESSION_PREFIX.fullmatch(value) is None):
        raise TmuxError("tmux session prefix is invalid")
    return value


def _validate_config_file(value: Any) -> str:
    if not isinstance(value, (str, Path)):
        raise TmuxError("tmux config file path is invalid")
    text = str(value)
    if (_has_unicode_control(text) or len(text) > 4096 or
            not is_canonical_absolute_path(text, resolved=True)):
        raise TmuxError("tmux config file path is invalid")
    return text


def _validate_pipe_path(value: Any) -> str:
    if not isinstance(value, (str, Path)):
        raise TmuxError("tmux pipe destination path is invalid")
    text = str(value)
    if (_has_unicode_control(text) or len(text) > 4096 or
            _PIPE_PATH.fullmatch(text) is None or
            not is_canonical_absolute_path(text, resolved=True)):
        raise TmuxError("tmux pipe destination path is invalid")
    return text


class TmuxAdapter:
    def __init__(
        self,
        *,
        executable: str = "tmux",
        socket: str | None = None,
        config_file: str | Path | None = None,
        runner: Callable[..., Any] | None = None,
    ):
        self.executable = executable
        self.socket = None if socket is None else _validate_socket_name(socket)
        self.config_file = (
            None if config_file is None else _validate_config_file(config_file)
        )
        self.runner = runner

    @staticmethod
    def _bounded_process(
        argv: list[str], *, cwd: Path | None, limit: int,
    ) -> tuple[int, bytes, bytes]:
        return bounded_process(argv, cwd=cwd, limit=limit, error_type=TmuxError)

    def _socket_args(self) -> list[str]:
        result = [] if self.socket is None else ["-L", self.socket]
        if self.config_file is not None:
            result.extend(["-f", self.config_file])
        return result

    def _capture_bytes(
        self,
        executable: str,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        limit: int = MAX_OUTPUT_BYTES,
        deadline_seconds: float = 60,
        input_data: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        argv = [executable, *self._socket_args(), *map(str, args)]
        return capture_bytes(
            argv, cwd=cwd, limit=limit, runner=self.runner, error_type=TmuxError,
            deadline_seconds=deadline_seconds,
            input_data=input_data,
        )

    def _run_bytes(
        self,
        executable: str,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        limit: int = MAX_OUTPUT_BYTES,
        deadline_seconds: float = 60,
    ) -> bytes:
        returncode, stdout, stderr = self._capture_bytes(
            executable, args, cwd=cwd, limit=limit,
            deadline_seconds=deadline_seconds,
        )
        if returncode != 0:
            self._raise_failure(returncode, stderr)
        return stdout

    def _run(self, args: Sequence[str], *, deadline_seconds: float = 60) -> str:
        try:
            return self._run_bytes(
                self.executable, args, deadline_seconds=deadline_seconds,
            ).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TmuxError("tmux output was not UTF-8") from exc

    def _run_status(
        self, args: Sequence[str], *, deadline_seconds: float = 60,
    ) -> tuple[int, bytes, bytes]:
        return self._capture_bytes(
            self.executable, args, deadline_seconds=deadline_seconds,
        )

    @staticmethod
    def _raise_failure(returncode: int, stderr: bytes) -> None:
        detail = stderr[:4096].decode("utf-8", errors="replace").strip()
        raise TmuxError(
            f"command failed ({returncode}): {detail or 'no diagnostic'}"
        )

    @staticmethod
    def _one_line(output: str, label: str) -> str:
        lines = output.splitlines()
        if len(lines) != 1 or not lines[0]:
            raise TmuxError(f"tmux returned ambiguous {label}")
        return lines[0]

    @staticmethod
    def _option_output(stdout: bytes) -> str:
        try:
            value = stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TmuxError("tmux option output was not UTF-8") from exc
        if value.endswith("\n"):
            value = value[:-1]
        if "\n" in value or "\r" in value:
            raise TmuxError("tmux returned ambiguous option output")
        return value

    def server_pid(self) -> int:
        value = self._one_line(
            self._run(["display-message", "-p", "#{pid}"]), "server pid",
        )
        try:
            pid = int(value)
        except ValueError as exc:
            raise TmuxError("tmux returned invalid server pid") from exc
        if pid <= 0:
            raise TmuxError("tmux returned invalid server pid")
        return pid

    def list_sessions(self) -> list[str]:
        """Session names on this server; an absent server is an empty list."""
        returncode, stdout, stderr = self._run_status(
            ["list-sessions", "-F", "#{session_name}"],
        )
        if returncode != 0:
            diagnostic = stderr.decode("utf-8", errors="replace").casefold()
            if any(marker in diagnostic for marker in ("no server running", "no sessions", "error connecting")):
                return []
            raise TmuxError("tmux list-sessions failed: " + diagnostic.strip()[:200])
        names = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and _NAME.fullmatch(line):
                names.append(line)
        return names

    def inventory(self) -> TmuxInventory:
        """Capture every session/pane fact needed by Control in one process."""
        returncode, stdout, stderr = self._capture_bytes(
            self.executable,
            ["list-panes", "-a", "-F", _INVENTORY_FORMAT],
            limit=INVENTORY_MAX_OUTPUT_BYTES,
            deadline_seconds=15,
        )
        if returncode != 0:
            diagnostic = stderr.decode("utf-8", errors="replace").casefold()
            if any(marker in diagnostic for marker in (
                "no server running", "no sessions", "error connecting",
                "failed to connect to server",
            )):
                return TmuxInventory(
                    self, server_pid=None, sessions={}, session_ids={},
                    panes={}, windows={},
                )
            self._raise_failure(returncode, stderr)
        try:
            output = stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TmuxError("tmux inventory output was not UTF-8") from exc
        expected_fields = 11 + len(_INVENTORY_SESSION_OPTIONS) + len(
            _INVENTORY_PANE_OPTIONS
        )
        sessions: dict[str, dict[str, Any]] = {}
        session_ids: dict[str, str] = {}
        panes: dict[str, dict[str, Any]] = {}
        active_windows: dict[tuple[str, str], str] = {}
        fallback_windows: dict[tuple[str, str], str] = {}
        window_names: dict[tuple[str, str], tuple[str, str]] = {}
        server_pid: int | None = None
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) != expected_fields:
                raise TmuxError("tmux returned malformed inventory facts")
            (
                raw_server_pid, session_name, raw_session_id, window_name,
                raw_window_id, pane_id, raw_pane_pid, raw_dead, raw_status, raw_signal,
                raw_active, *option_values,
            ) = fields
            # This server may also contain ordinary operator sessions. Their
            # names are outside Control's accepted target grammar, so they are
            # irrelevant inventory rows rather than corruption of the bulk
            # sample. This deliberately matches list_sessions()'s tolerance.
            if (_NAME.fullmatch(session_name) is None or
                    _NAME.fullmatch(window_name) is None):
                continue
            try:
                current_server_pid = int(raw_server_pid)
            except ValueError as exc:
                raise TmuxError("tmux returned invalid inventory server pid") from exc
            if current_server_pid <= 0:
                raise TmuxError("tmux returned invalid inventory server pid")
            if server_pid is not None and server_pid != current_server_pid:
                raise TmuxError("tmux inventory crossed server identities")
            server_pid = current_server_pid
            name = _validate_session_name(session_name)
            identity = _validate_session_id(raw_session_id)
            window = _validate_window_name(window_name)
            window_identity = _validate_window_id(raw_window_id)
            pane = _validate_pane_id(pane_id)
            if raw_active not in {"0", "1"}:
                raise TmuxError("tmux returned invalid inventory active-pane state")
            facts = self._parse_pane_fields(
                pane, raw_pane_pid, raw_dead, raw_status, raw_signal,
                name, window, "",
            )
            session_option_values = option_values[:len(_INVENTORY_SESSION_OPTIONS)]
            pane_option_values = option_values[len(_INVENTORY_SESSION_OPTIONS):]
            for value in option_values:
                _validate_restricted_value(value)
            session_options = {
                key: value for key, value in zip(
                    _INVENTORY_SESSION_OPTIONS, session_option_values,
                ) if value != ""
            }
            pane_options = {
                key: value for key, value in zip(
                    _INVENTORY_PANE_OPTIONS, pane_option_values,
                ) if value != ""
            }
            existing = sessions.get(name)
            if existing is None:
                sessions[name] = {
                    "name": name, "session_id": identity,
                    "options": session_options,
                }
            elif (
                existing["session_id"] != identity
                or existing["options"] != session_options
            ):
                raise TmuxError("tmux returned inconsistent session inventory")
            if identity in session_ids and session_ids[identity] != name:
                raise TmuxError("tmux returned duplicate session identity")
            session_ids[identity] = name
            if pane in panes:
                raise TmuxError("tmux returned duplicate pane identity")
            panes[pane] = {
                "facts": facts, "options": pane_options,
                "session_id": identity,
            }
            # Window names are not unique inside a tmux session. Track the
            # active pane against tmux's immutable identities, then retain one
            # deterministic name-based candidate for the legacy recovery API.
            window_key = (identity, window_identity)
            label = (name, window)
            prior_label = window_names.setdefault(window_key, label)
            if prior_label != label:
                raise TmuxError("tmux returned inconsistent window inventory")
            fallback_windows.setdefault(window_key, pane)
            if raw_active == "1":
                if window_key in active_windows:
                    raise TmuxError("tmux returned multiple active panes for one window")
                active_windows[window_key] = pane
        windows: dict[tuple[str, str], str] = {}
        for key, pane in fallback_windows.items():
            windows.setdefault(window_names[key], active_windows.get(key, pane))
        return TmuxInventory(
            self, server_pid=server_pid, sessions=sessions,
            session_ids=session_ids, panes=panes, windows=windows,
        )

    def has_session(self, name: str) -> bool:
        session = _validate_session_target(name)
        returncode, _stdout, stderr = self._run_status(
            ["has-session", "-t", session],
        )
        if returncode == 0:
            return True
        diagnostic = stderr.decode("utf-8", errors="replace").casefold()
        missing = any(marker in diagnostic for marker in (
            "can't find session", "no server running", "no sessions",
        ))
        connection_absent = (
            ("failed to connect to server" in diagnostic or
             "error connecting to" in diagnostic)
            and ("no such file or directory" in diagnostic or
                 "connection refused" in diagnostic)
        )
        if missing or connection_absent:
            return False
        self._raise_failure(returncode, stderr)

    def session_option(
        self, name: str, option: str, *, deadline_seconds: float = 60,
    ) -> str | None:
        session = _validate_session_target(name)
        key = _validate_user_option_key(option)
        returncode, stdout, stderr = self._run_status(
            ["show-options", "-v", "-t", session, key],
            deadline_seconds=deadline_seconds,
        )
        if returncode == 0:
            return self._option_output(stdout)
        if b"invalid option" in stderr.lower():
            return None
        self._raise_failure(returncode, stderr)

    def pane_option(
        self, pane_id: str, option: str, *, deadline_seconds: float = 60,
    ) -> str | None:
        pane = _validate_pane_id(pane_id)
        key = _validate_user_option_key(option)
        returncode, stdout, stderr = self._run_status(
            ["show-options", "-p", "-v", "-t", pane, key],
            deadline_seconds=deadline_seconds,
        )
        if returncode == 0:
            return self._option_output(stdout)
        if b"invalid option" in stderr.lower():
            return None
        self._raise_failure(returncode, stderr)

    def set_server_summary(
        self, value: str, *, deadline_seconds: float = 60,
    ) -> None:
        summary = _validate_restricted_value(value)
        self._run(
            ["set-option", "-s", "@asha_summary", summary],
            deadline_seconds=deadline_seconds,
        )

    def set_pane_option(
        self, pane_id: str, option: str, value: str, *,
        deadline_seconds: float = 60,
    ) -> None:
        """Set one pane-scoped user option. Callers verify ownership first."""
        pane = _validate_pane_id(pane_id)
        key = _validate_user_option_key(option)
        setting = _validate_restricted_value(value)
        self._run(
            ["set-option", "-p", "-t", pane, key, setting],
            deadline_seconds=deadline_seconds,
        )

    def set_session_option(
        self, name: str, option: str, value: str, *,
        deadline_seconds: float = 60,
    ) -> None:
        """Set one session-scoped user option. Callers verify ownership first."""
        session = _validate_session_name(name)
        key = _validate_user_option_key(option)
        setting = _validate_restricted_value(value)
        self._run(
            ["set-option", "-t", session, key, setting],
            deadline_seconds=deadline_seconds,
        )

    def pane_facts(
        self, pane_id: str, *, deadline_seconds: float = 60,
    ) -> PaneFacts:
        expected_pane = _validate_pane_id(pane_id)
        return self._target_facts(
            expected_pane, expected_pane=expected_pane,
            deadline_seconds=deadline_seconds,
        )

    def session_id(
        self, pane_id: str, *, deadline_seconds: float = 60,
    ) -> str:
        """Return tmux's server-scoped immutable session identifier."""
        pane = _validate_pane_id(pane_id)
        output = self._run([
            "display-message", "-p", "-t", pane, "#{session_id}",
        ], deadline_seconds=deadline_seconds)
        if output in {"", "\n"}:
            raise TmuxError(f"can't find pane: {pane}")
        return _validate_session_id(self._one_line(output, "session id"))

    @staticmethod
    def _room_condition(
        *, room_id: str, project_marker: str, pane_id: str, session_id: str,
    ) -> tuple[str, str, str]:
        if not isinstance(room_id, str) or _ROOM_UUID.fullmatch(room_id) is None:
            raise TmuxError("room id is invalid")
        if (not isinstance(project_marker, str)
                or _SHA256.fullmatch(project_marker) is None):
            raise TmuxError("room project marker is invalid")
        pane = _validate_pane_id(pane_id)
        session = _validate_session_id(session_id)
        condition = (
            f"#{{&&:#{{==:#{{session_id}},{session}}},"
            f"#{{&&:#{{==:#{{pane_id}},{pane}}},"
            f"#{{&&:#{{==:#{{@asha_room_session_id}},{room_id}}},"
            f"#{{&&:#{{==:#{{@asha_room_id}},{room_id}}},"
            f"#{{==:#{{@asha_room_project_id}},{project_marker}}}}}}}}}}}"
        )
        return pane, session, condition

    def room_attach_argv(
        self, *, room_id: str, project_marker: str,
        pane_id: str, session_id: str,
    ) -> list[str]:
        """One fail-closed server action: revalidate ownership, then attach."""
        pane, session, condition = self._room_condition(
            room_id=room_id, project_marker=project_marker,
            pane_id=pane_id, session_id=session_id,
        )
        return [
            self.executable, *self._socket_args(),
            "if-shell", "-F", "-t", pane, condition,
            f"attach-session -t {session}", _ROOM_REFUSAL,
        ]

    def kill_owned_room(
        self, *, room_id: str, project_marker: str,
        pane_id: str, session_id: str, detached_only: bool = False,
    ) -> None:
        """Atomically revalidate immutable Room identity and kill only that session.

        ``detached_only`` adds "no client attached, pane not in a mode" to the
        same tmux condition: an automatic fallback never kills a Room a person
        is using. That refusal raises ``RoomInputRefused``.
        """
        pane, session, condition = self._room_condition(
            room_id=room_id, project_marker=project_marker,
            pane_id=pane_id, session_id=session_id,
        )
        if detached_only:
            condition = self._detached(condition, None)
        returncode, stdout, stderr = self._run_status([
            "if-shell", "-F", "-t", pane, condition,
            f"display-message -p ASHA_ROOM_OWNED ; kill-session -t {session}",
            _INPUT_REFUSAL if detached_only else _ROOM_REFUSAL,
        ])
        if returncode == 0 and stdout == b"ASHA_ROOM_OWNED\n":
            return
        if returncode == 66 or b"ASHA_ROOM_OWNERSHIP_REFUSED" in stdout:
            if detached_only:
                category = self._refusal_category(pane, None)
                raise RoomInputRefused(
                    category, f"room kill refused ({category}): a client is attached, the pane is "
                    "in a tmux mode or ownership changed; no session was killed",
                )
            raise TmuxError("room ownership changed; no session was killed")
        self._raise_failure(returncode, stderr)

    @staticmethod
    def _detached(
        condition: str, attach_generation: str | None = None, event_sequence: str | None = None,
    ) -> str:
        """``condition`` plus detached, not in a mode, window not linked into another
        session (a client there would see the pane without attaching), and
        (optionally) unchanged fence counters. The counters are pane options of
        the target pane, so the format reads exactly them."""
        fence = "#{&&:#{==:#{pane_in_mode},0},#{==:#{window_linked},0}}"
        for option, value in ((ATTACH_GENERATION_OPTION, attach_generation),
                              (EVENT_SEQUENCE_OPTION, event_sequence)):
            if value is not None:
                fence = f"#{{&&:{fence},#{{==:#{{{option}}},{value}}}}}"
        return f"#{{&&:{condition},#{{&&:#{{==:#{{session_attached}},0}},{fence}}}}}"

    def _input_state(self, pane: str) -> dict[str, str]:
        """Attachment, mode and the pane-local fence counters, read in one tmux command.

        ``attached`` is the session's client count, or ``linked`` when the
        pane's window is also linked into another session. Counters come from
        ``show-options -p`` (the pane's own values, never inherited), each
        framed by a marker line because an unset option prints nothing.
        """
        output = self._run([
            "display-message", "-p", "-t", pane,
            "#{pane_id}\t#{session_id}\t#{session_attached}\t#{pane_in_mode}\t#{window_linked}",
            ";", "display-message", "-p", "ASHA_FENCE_GENERATION",
            ";", "show-options", "-q", "-p", "-v", "-t", pane, ATTACH_GENERATION_OPTION,
            ";", "display-message", "-p", "ASHA_FENCE_SEQUENCE",
            ";", "show-options", "-q", "-p", "-v", "-t", pane, EVENT_SEQUENCE_OPTION,
            ";", "display-message", "-p", "ASHA_FENCE_END",
        ], deadline_seconds=_INPUT_DEADLINE)
        lines = output.split("\n")
        if not lines or not lines[0] or lines[0].startswith("\t"):
            raise TmuxError(f"can't find pane: {pane}")
        fields = lines[0].split("\t")
        try:
            start, middle, end = (lines.index(marker) for marker in (
                "ASHA_FENCE_GENERATION", "ASHA_FENCE_SEQUENCE", "ASHA_FENCE_END"))
        except ValueError:
            raise TmuxError("tmux returned malformed pane input facts") from None
        generation, sequence = lines[start + 1:middle], lines[middle + 1:end]
        if (len(fields) != 5 or fields[0] != pane or not fields[2].isascii() or not fields[2].isdigit()
                or fields[3] not in {"0", "1"} or fields[4] not in {"0", "1"}
                or start != 1 or len(generation) > 1 or len(sequence) > 1 or lines[end + 1:] not in ([], [""])):
            raise TmuxError("tmux returned malformed pane input facts")
        return {
            "session": _validate_session_id(fields[1]),
            "attached": "linked" if fields[4] == "1" else fields[2],
            "in_mode": fields[3],
            "generation": generation[0] if generation else "",
            "sequence": sequence[0] if sequence else "",
        }

    def _refusal_category(
        self, pane: str, attach_generation: str | None, event_sequence: str | None = None,
    ) -> str:
        """Name why a guarded pane action refused; anything unproven is ``ownership``."""
        try:
            state = self._input_state(pane)
        except TmuxError:
            return "ownership"
        if state["attached"] != "0":
            return "attached"
        if attach_generation is not None and _fence_value(state["generation"])[0] is None:
            return "unfenced"
        if attach_generation is not None and state["generation"] != attach_generation:
            return "attached"
        if state["in_mode"] != "0":
            return "mode"
        if event_sequence is not None and state["sequence"] != event_sequence:
            return "stale"
        return "ownership"

    def _fence_problem(self, pane: str, session: str, state: dict[str, str]) -> str | None:
        """Why this pane's fence cannot guard typing, or None when it can."""
        for name, key in (("attach generation", "generation"), ("event sequence", "sequence")):
            value, why = _fence_value(state[key])
            if value is None:
                return f"room {name} {why}"
        expected = _attach_hook_shown(pane)
        for hook in _ATTACH_HOOKS:
            returncode, stdout, _stderr = self._run_status(
                ["show-hooks", "-t", session, hook], deadline_seconds=_INPUT_DEADLINE)
            lines = stdout.decode("utf-8", "replace").splitlines()
            if (returncode != 0 or len(lines) != 1 or not lines[0].startswith(f"{hook}[0] ")
                    or lines[0][len(hook) + 4:] not in expected):
                return "room attach hooks are missing or changed"
        return None

    def _require_fence(self, pane: str, stage: str) -> None:
        """Re-verify hooks and counters immediately before a guarded command."""
        state = self._input_state(pane)
        category, problem = None, None
        if state["attached"] != "0":
            category, problem = "attached", "a client is attached or the window is linked elsewhere"
        elif state["in_mode"] != "0":
            category, problem = "mode", "the pane is in a tmux mode"
        else:
            category, problem = "unfenced", self._fence_problem(pane, state["session"], state)
        if problem:
            raise RoomInputRefused(
                category if stage == "nothing was typed" else "partial",
                f"room input refused ({problem}); {stage}",
            )

    def room_input_facts(
        self, pane_id: str, *, deadline_seconds: float = 5,
    ) -> RoomInputFacts:
        """Attached-client count, fence counters and visible screen (with SGR) of a pane.

        Read-only. Callers verify Room ownership first; the injection itself
        re-checks ownership, detachment and both counters inside tmux. A Room
        whose counters are missing, non-canonical or exhausted, or whose attach
        hooks are not exactly the installed ones, refuses ``unfenced``.
        """
        pane = _validate_pane_id(pane_id)
        state = self._input_state(pane)
        if state["in_mode"] != "0":
            raise RoomInputRefused("mode", "room pane is in a tmux mode; nothing was typed")
        if state["attached"] == "linked":
            raise RoomInputRefused(
                "attached", "room window is linked into another session; nothing was typed",
            )
        problem = self._fence_problem(pane, state["session"], state)
        if problem:
            raise RoomInputRefused("unfenced", problem + "; nothing was typed")
        screen = self._run(
            ["capture-pane", "-p", "-e", "-t", pane],
            deadline_seconds=min(deadline_seconds, _INPUT_DEADLINE),
        ).splitlines()
        return RoomInputFacts(
            int(state["attached"]), state["generation"], state["sequence"],
            [raw[:2000] for raw in screen[-_INPUT_SCREEN_LINES:]],
        )

    def inject_owned_room_input(
        self, *, room_id: str, project_marker: str, pane_id: str,
        session_id: str, text: str, attach_generation: str, event_sequence: str,
        confirm: Callable[[list[str]], str | None],
        ready: Callable[[], str | None] | None = None,
    ) -> None:
        """Type one line into an owned, detached pane and submit it.

        Ownership, ``session_attached == 0``, "not in a tmux mode", an unlinked
        window, and unchanged pane counters (attach generation and native event
        sequence, read with the screen) are evaluated by tmux in the same
        command that pastes, and again in the one that presses Enter: any
        attach, even an attach-type-detach cycle within one second, or any
        native event that began, refuses. Hook integrity and counter validity
        are re-verified immediately before each of those commands, and
        ``ready`` (the caller's own state check) runs right before the paste.
        Between paste and Enter, ``confirm`` receives the captured screen and
        returns a refusal reason unless the input holds exactly the pasted
        text. The whole operation must finish within ``_INPUT_BUDGET``
        seconds or Enter is withheld. The text travels as a tmux buffer (argv
        data, never a command string), pasted bracketed when requested.
        Refusals raise ``RoomInputRefused``; ``partial`` leaves text unsubmitted.
        """
        started = time.monotonic()
        pane, _session, condition = self._room_condition(
            room_id=room_id, project_marker=project_marker,
            pane_id=pane_id, session_id=session_id,
        )
        if (not isinstance(text, str) or not text or len(text) > _INPUT_TEXT_LIMIT
                or not text.isprintable()):
            raise TmuxError("room input text must be one printable line")
        for name, value in (("attach generation", attach_generation), ("event sequence", event_sequence)):
            if _fence_value(value)[0] is None:
                raise RoomInputRefused("unfenced", f"room {name} is invalid; nothing was typed")
        guarded = self._detached(condition, attach_generation, event_sequence)
        self._require_fence(pane, "nothing was typed")
        reason = ready() if ready is not None else None
        if reason:
            raise RoomInputRefused("stale", "room input refused: " + reason + "; nothing was typed")
        buffer = "asha-input-" + uuid.uuid4().hex[:16]
        # Every command is short-bounded; the caller holds no lock across them.
        self._run(["set-buffer", "-b", buffer, "--", text], deadline_seconds=_INPUT_DEADLINE)
        try:
            returncode, stdout, stderr = self._run_status([
                "if-shell", "-F", "-t", pane, guarded,
                f"display-message -p ASHA_ROOM_OWNED ; "
                f"paste-buffer -p -d -b {buffer} -t {pane}",
                _INPUT_REFUSAL,
            ], deadline_seconds=_INPUT_DEADLINE)
        finally:
            self._run_status(["delete-buffer", "-b", buffer], deadline_seconds=_INPUT_DEADLINE)
        if not (returncode == 0 and stdout == b"ASHA_ROOM_OWNED\n"):
            if b"ASHA_ROOM_OWNERSHIP_REFUSED" in stdout:
                category = self._refusal_category(pane, attach_generation, event_sequence)
                raise RoomInputRefused(
                    category, f"room input refused ({category}); nothing was typed",
                )
            self._raise_failure(returncode, stderr)
        # Let the harness consume the paste, then prove the input line holds
        # exactly this text before anything is submitted.
        time.sleep(_INPUT_SUBMIT_DELAY)
        try:
            screen = self._run(["capture-pane", "-p", "-e", "-t", pane],
                               deadline_seconds=_INPUT_DEADLINE).splitlines()
            reason = confirm([raw[:2000] for raw in screen[-_INPUT_SCREEN_LINES:]])
            if not reason:
                self._require_fence(pane, "typed but not submitted")
        except RoomInputRefused:
            raise
        except (OSError, ValueError) as exc:
            reason = str(exc)[:200] or type(exc).__name__
        if not reason and time.monotonic() - started > _INPUT_BUDGET:
            reason = f"delivery took longer than {_INPUT_BUDGET} seconds"
        if reason:
            raise RoomInputRefused(
                "partial", "room input typed but not submitted: " + reason
                + "; the text remains in the input line",
            )
        returncode, stdout, stderr = self._run_status([
            "if-shell", "-F", "-t", pane, guarded,
            f"display-message -p ASHA_ROOM_OWNED ; send-keys -t {pane} Enter",
            _INPUT_REFUSAL,
        ], deadline_seconds=_INPUT_DEADLINE)
        if returncode == 0 and stdout == b"ASHA_ROOM_OWNED\n":
            return
        raise RoomInputRefused(
            "partial", "room input typed but not submitted: a client attached (or attached "
            "and detached), a native event began, the pane entered a mode or ownership "
            "changed; the text remains in the input line",
        )

    def window_pane_facts(
        self, session: str, window: str, *, deadline_seconds: float = 60,
    ) -> PaneFacts:
        """Facts for the active pane of an owned session window.

        Recovery paths know only the session and window they recorded, never a
        pane id, so this resolves ``session:window`` and verifies that tmux
        answered for exactly that window.
        """
        name = _validate_session_name(session)
        window_name = _validate_window_name(window)
        facts = self._target_facts(
            f"{name}:{window_name}", expected_pane=None,
            deadline_seconds=deadline_seconds,
        )
        if facts.session != name or facts.window != window_name:
            raise TmuxError("tmux returned a different window identity")
        return facts

    def _target_facts(
        self, target: str, *, expected_pane: str | None, deadline_seconds: float,
    ) -> PaneFacts:
        line = self._one_line(
            self._run([
                "display-message", "-p", "-t", target, "-F", _PANE_FORMAT,
            ], deadline_seconds=deadline_seconds),
            "pane facts",
        )
        fields = line.split("\t")
        if len(fields) != 8:
            raise TmuxError("tmux returned malformed pane facts")
        returned_pane, raw_pid, raw_dead, raw_status, raw_signal, session, window, title = fields
        if expected_pane is not None and not any(fields):
            # tmux answers an unknown pane id with exit 0 and empty fields.
            # That is evidence the exact pane is gone, not a malformed id.
            raise TmuxError(f"can't find pane: {expected_pane}")
        if expected_pane is not None and _validate_pane_id(returned_pane) != expected_pane:
            raise TmuxError("tmux returned a different pane identity")
        return self._parse_pane_fields(
            returned_pane, raw_pid, raw_dead, raw_status, raw_signal,
            session, window, title,
        )

    @classmethod
    def _parse_pane_fields(
        cls,
        pane_id: str,
        raw_pid: str,
        raw_dead: str,
        raw_status: str,
        raw_signal: str,
        session: str,
        window: str,
        title: str,
    ) -> PaneFacts:
        pane = _validate_pane_id(pane_id)
        name = _validate_session_name(session)
        window_name = _validate_window_name(window)
        pane_pid = cls._parse_optional_integer(raw_pid, positive=True)
        if raw_dead not in {"0", "1"}:
            raise TmuxError("tmux returned invalid pane dead state")
        dead_status = cls._parse_optional_integer(raw_status, positive=False)
        dead_signal = cls._parse_optional_integer(raw_signal, positive=False)
        return PaneFacts(
            pane_id=pane,
            pane_pid=pane_pid,
            dead=raw_dead == "1",
            dead_status=dead_status,
            dead_signal=dead_signal,
            session=name,
            window=window_name,
            title="",  # Compatibility only; a title is never ownership or exit evidence.
        )

    @staticmethod
    def _parse_optional_integer(value: str, *, positive: bool) -> int | None:
        if value == "":
            return None
        try:
            result = int(value)
        except ValueError as exc:
            raise TmuxError("tmux returned invalid numeric pane fact") from exc
        if result < (1 if positive else 0):
            raise TmuxError("tmux returned invalid numeric pane fact")
        return result

    def create_task_session(
        self,
        *,
        session: str,
        window: str,
        start_directory: str | Path,
        environment: Mapping[str, str],
        holder_argv: list[str],
        session_options: Mapping[str, str],
        pane_options: Mapping[str, str],
        pane_title: str,
        attach_fence: bool = False,
    ) -> str:
        """Create a detached session; ``attach_fence`` installs the Room input fence.

        The fence (pane-local attach generation and event sequence, plus the
        session hooks that bump the generation) is installed right after the
        pane exists. Session hooks shadow same-named global hooks for this
        session only.
        """
        session = _validate_session_name(session)
        window = _validate_window_name(window)
        directory = _validate_start_directory(start_directory)
        holder = _validate_argv(holder_argv)
        title = _validate_restricted_value(pane_title)
        environment_items = self._validated_environment(environment)
        session_option_items = self._validated_options(session_options)
        pane_option_items = self._validated_options(pane_options)
        session_target = f"{session}:"
        pane_target = f"{session}:{window}"

        args = [
            "new-session", "-d", "-P", "-F", "#{pane_id}",
            "-s", session, "-n", window, "-c", directory,
        ]
        for key, value in environment_items:
            args.extend(["-e", f"{key}={value}"])
        args.extend(["--", *holder])
        args.extend([
            ";", "set-option", "-t", session_target, "remain-on-exit", "on",
            ";", "set-option", "-t", session_target, "automatic-rename", "off",
        ])
        for key, value in session_option_items:
            args.extend([";", "set-option", "-t", session, key, value])
        for key, value in pane_option_items:
            args.extend([";", "set-option", "-p", "-t", pane_target, key, value])
        args.extend([";", "select-pane", "-t", pane_target, "-T", title])
        created_pane = _validate_pane_id(self._one_line(self._run(args), "created pane id"))
        if attach_fence:
            # Hooks name the exact pane, which exists only now. Attaches before
            # this point precede any fence read and so cannot hide from it.
            fence = [
                "set-option", "-p", "-t", created_pane, ATTACH_GENERATION_OPTION, "0",
                ";", "set-option", "-p", "-t", created_pane, EVENT_SEQUENCE_OPTION, "0",
            ]
            for hook in _ATTACH_HOOKS:
                fence.extend([";", "set-hook", "-t", session_target, hook, _attach_hook_command(created_pane)])
            self._run(fence)
        return created_pane

    def set_result_staging_token(self, session: str, token: str) -> None:
        """Set the private session token over stdin so it never enters argv."""
        name = _validate_session_name(session)
        if (
            not isinstance(token, str)
            or _RESULT_STAGING_TOKEN.fullmatch(token) is None
        ):
            raise TmuxError("result staging token is invalid")
        command = (
            f"set-environment -t {name} ASHA_CONTROL_RESULT_TOKEN {token}\n\n"
        ).encode("ascii")
        returncode, stdout, _stderr = self._capture_bytes(
            self.executable,
            ["-C", "attach-session", "-t", name],
            input_data=command,
        )
        protocol_error = any(
            line.startswith(b"%error ") for line in stdout.splitlines()
        )
        if returncode != 0 or protocol_error:
            # Control-mode diagnostics may echo input; never relay them.
            raise TmuxError("tmux private session environment update failed")

    def clear_result_staging_token(self, session: str) -> None:
        """Drop the session copy after the worker process has inherited it."""
        name = _validate_session_name(session)
        self._run([
            "set-environment", "-u", "-t", name,
            "ASHA_CONTROL_RESULT_TOKEN",
        ])

    @staticmethod
    def _validated_environment(
        environment: Mapping[str, str],
    ) -> list[tuple[str, str]]:
        if not isinstance(environment, Mapping):
            raise TmuxError("tmux environment is invalid")
        result: list[tuple[str, str]] = []
        for key, value in environment.items():
            result.append((
                _validate_environment_key(key),
                _validate_environment_value(value),
            ))
        return result

    @staticmethod
    def _validated_options(options: Mapping[str, str]) -> list[tuple[str, str]]:
        if not isinstance(options, Mapping):
            raise TmuxError("tmux options are invalid")
        result: list[tuple[str, str]] = []
        for key, value in options.items():
            result.append((
                _validate_user_option_key(key),
                _validate_restricted_value(value),
            ))
        return result

    def respawn(self, pane_id: str, argv: list[str]) -> None:
        pane = _validate_pane_id(pane_id)
        command = _validate_argv(argv)
        self._run(["respawn-pane", "-k", "-t", pane, "--", *command])

    def select_target(
        self, session: str, window: str, pane_id: str | None = None,
    ) -> None:
        session = _validate_session_name(session)
        window = _validate_window_name(window)
        args = ["select-window", "-t", f"{session}:{window}"]
        if pane_id is not None:
            pane = _validate_pane_id(pane_id)
            args.extend([";", "select-pane", "-t", pane])
        self._run(args)

    def caller_client(self, pane: str) -> str | None:
        """Return the first client tty attached to the caller pane's session."""
        pane_id = _validate_pane_id(pane)
        session = _validate_session_name(self._one_line(
            self._run([
                "display-message", "-p", "-t", pane_id, "#{session_name}",
            ]),
            "session name",
        ))
        output = self._run([
            "list-clients", "-t", session, "-F", "#{client_tty}",
        ])
        if output == "":
            return None
        lines = output.split("\n")
        if not lines or not lines[0]:
            raise TmuxError("tmux returned invalid client tty")
        return _validate_client_tty(lines[0])

    def popup_argv(
        self, *, client: str, session: str, width: str, height: str,
    ) -> list[str]:
        session = _validate_session_name(session)
        socket_args = self._socket_args()
        command = [
            self.executable, *socket_args,
            "attach-session", "-t", session,
        ]
        return self.popup_command_argv(
            client=client, command=command, width=width, height=height,
        )

    def popup_command_argv(
        self, *, client: str, command: list[str], width: str, height: str,
    ) -> list[str]:
        """Run one already-tokenized command in the caller-bound popup seam."""
        client = _validate_client_tty(client)
        child = _validate_argv(command)
        width = _validate_popup_dimension(width)
        height = _validate_popup_dimension(height)
        return [
            self.executable, *self._socket_args(),
            "display-popup", "-c", client, "-E",
            "-w", width, "-h", height, "--",
            sys.executable, "-I", "-S", "-c", _POPUP_CHILD_EXEC,
            *child,
        ]

    def send_line(self, pane_id: str, text: str) -> None:
        """Type one bounded line plus Enter into an owned pane.

        OPERATOR-ONLY SEAM: this exists for explicit human actions relayed by
        the TUI (the close-worker key). Controller code paths must never call
        it; the controller's no-pane-input rule is a design invariant, not a
        missing feature.
        """
        pane = _validate_pane_id(pane_id)
        if not isinstance(text, str) or not text or len(text) > 200 or any(
            ord(char) < 32 or ord(char) == 127 for char in text
        ):
            raise TmuxError("pane input must be one bounded printable line")
        self._run(["send-keys", "-t", pane, "-l", text])
        self._run(["send-keys", "-t", pane, "Enter"])

    def pipe_pane(self, pane_id: str, path: str | Path) -> None:
        """Append every byte an owned pane emits to a durable file.

        ``pipe-pane -o`` opens the pipe only when the pane has none, and with
        neither ``-I`` nor ``-O`` given tmux connects the pane's OUTPUT to the
        command's stdin.  The pipe starts at the pane's next byte, so callers
        open it before the pane's real command is respawned.  The destination
        is an ordinary file rather than tmux-resident state, so the record
        outlives the pane process, the session, and the server.

        Callers verify pane ownership first.
        """
        pane = _validate_pane_id(pane_id)
        destination = _validate_pipe_path(path)
        self._run([
            "pipe-pane", "-o", "-t", pane, f"cat >> {shlex.quote(destination)}",
        ])

    def pane_tail(self, pane_id: str, *, lines: int = 12) -> list[str]:
        """Last non-empty visible lines of an owned pane (no scrollback).

        Callers verify pane ownership first; this reads the screen only, never
        writes to it, and bounds the result.
        """
        pane = _validate_pane_id(pane_id)
        if not 1 <= lines <= 200:
            raise ValueError("pane tail line count must be within 1-200")
        output = self._run(["capture-pane", "-p", "-t", pane])
        collected: list[str] = []
        for raw in output.splitlines():
            text = "".join(
                character if character.isprintable() else "?" for character in raw
            ).rstrip()
            if text:
                collected.append(text[:400])
        return collected[-lines:]

    def session_names(self) -> list[str]:
        """Names of every session on this server; empty when no server runs."""
        returncode, stdout, stderr = self._run_status(
            ["list-sessions", "-F", "#{session_name}"],
        )
        if returncode != 0:
            diagnostic = stderr.decode("utf-8", errors="replace").casefold()
            if ("no server running" in diagnostic or "no sessions" in diagnostic
                    or "error connecting to" in diagnostic
                    or "failed to connect to server" in diagnostic):
                return []
            self._raise_failure(returncode, stderr)
        names: list[str] = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            if line:
                names.append(line)
        return names

    def session_pane_states(self, name: str) -> list[bool]:
        """Dead flags for every pane in a session, across all its windows."""
        session = _validate_session_name(name)
        output = self._run(["list-panes", "-s", "-t", session, "-F", "#{pane_dead}"])
        states: list[bool] = []
        for line in output.splitlines():
            if line not in {"0", "1"}:
                raise TmuxError("tmux returned invalid pane dead state")
            states.append(line == "1")
        if not states:
            raise TmuxError("tmux returned no panes for the session")
        return states

    def kill_session(self, name: str) -> None:
        """Kill a session; callers are required to verify ownership first."""
        session = _validate_session_name(name)
        self._run(["kill-session", "-t", session])

    def integration_snippet(self, *, session_prefix: str) -> str:
        prefix = _validate_session_prefix(session_prefix)
        return (
            "# Optional Asha Control Prefix + ` binding.\n"
            f"# Managed names use {prefix}; names are not ownership evidence.\n"
            "bind-key ` if-shell -F '#{==:#{@asha_managed},1}' "
            "'detach-client' "
            "'display-message \"Current session is not Asha-managed\"'\n"
        )
