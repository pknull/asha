"""Argument-safe adapter for Asha Control's tmux seam."""

from __future__ import annotations

import os
import re
import shlex
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import is_canonical_absolute_path
from .process import bounded_process, capture_bytes
from .text import terminal_text_is_complete


MAX_OUTPUT_BYTES = 64 * 1024
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
_PANE_ID = re.compile(r"%[0-9]+", re.ASCII)
_SESSION_ID = re.compile(r"\$[0-9]+", re.ASCII)
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
    "#{pane_dead_signal}\t#{session_name}\t#{window_name}\t#{pane_title}"
)
_ROOM_REFUSAL = (
    'display-message -p ASHA_ROOM_OWNERSHIP_REFUSED ; run-shell "exit 66"'
)


class TmuxError(ValueError):
    """A tmux precondition, invocation, or identity check failed."""


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
        value = self._one_line(
            self._run([
                "display-message", "-p", "-t", pane, "#{session_id}",
            ], deadline_seconds=deadline_seconds),
            "session id",
        )
        return _validate_session_id(value)

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
            f"#{{==:#{{@asha_room_project_id}},{project_marker}}}}}}}}}}}}}"
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
        pane_id: str, session_id: str,
    ) -> None:
        """Atomically revalidate immutable Room identity and kill only that session."""
        pane, session, condition = self._room_condition(
            room_id=room_id, project_marker=project_marker,
            pane_id=pane_id, session_id=session_id,
        )
        returncode, stdout, stderr = self._run_status([
            "if-shell", "-F", "-t", pane, condition,
            f"display-message -p ASHA_ROOM_OWNED ; kill-session -t {session}",
            _ROOM_REFUSAL,
        ])
        if returncode == 0 and stdout == b"ASHA_ROOM_OWNED\n":
            return
        if returncode == 66 or b"ASHA_ROOM_OWNERSHIP_REFUSED" in stdout:
            raise TmuxError("room ownership changed; no session was killed")
        self._raise_failure(returncode, stderr)

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
        if expected_pane is not None and _validate_pane_id(returned_pane) != expected_pane:
            raise TmuxError("tmux returned a different pane identity")
        _validate_pane_id(returned_pane)
        _validate_session_name(session)
        _validate_window_name(window)
        _validate_restricted_value(title)
        pane_pid = self._parse_optional_integer(raw_pid, positive=True)
        if raw_dead not in {"0", "1"}:
            raise TmuxError("tmux returned invalid pane dead state")
        dead_status = self._parse_optional_integer(raw_status, positive=False)
        dead_signal = self._parse_optional_integer(raw_signal, positive=False)
        return PaneFacts(
            pane_id=returned_pane,
            pane_pid=pane_pid,
            dead=raw_dead == "1",
            dead_status=dead_status,
            dead_signal=dead_signal,
            session=session,
            window=window,
            title=title,
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
    ) -> str:
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
        created_pane = self._one_line(self._run(args), "created pane id")
        return _validate_pane_id(created_pane)

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
