"""Control configuration and path validation without filesystem mutation."""

from __future__ import annotations

import json
import os
import posixpath
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Any


HARNESSES = frozenset({"claude", "codex", "copilot", "opencode"})
MAX_CONFIG_BYTES = 64 * 1024
_PERCENT = re.compile(r"(?:[1-9][0-9]?|100)%")
_SESSION_PREFIX = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30})?")
_CONFIG_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class ConfigError(ValueError):
    """Control configuration is invalid or unsafe."""


class _DuplicateJsonKey(ValueError):
    pass


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey
        result[key] = value
    return result


# Retired settings (best-effort close, D5; session preview, N9): accepted and
# ignored for one release so an existing config still loads.
IGNORED_CONTROL_KEYS = frozenset({"idle_delivery", "no_handoff_close", "session_preview"})
# How long a close waits for a Memory save before it terminates (D5).
# control.workspace_trust stays a validated key so existing configuration keeps
# loading; the task substrate that read it was retired (L-b).
TRUST_MODES = ("inherit", "never")
CLOSE_WAIT_DEFAULT = 60
CLOSE_WAIT_LIMIT = 600


@dataclass(frozen=True)
class ControlConfig:
    config_path: Path
    home: Path
    asha_home: Path
    tasks_dir: Path
    workspace_root: Path
    runtime_dir: Path
    default_harness: str
    popup_width: str
    popup_height: str
    session_prefix: str
    event_staleness_seconds: int
    workspace_trust: str
    close_wait_seconds: int = CLOSE_WAIT_DEFAULT


def _absolute(value: str, name: str, *, home: Path, allow_tilde: bool = True) -> Path:
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ConfigError(f"{name} must not contain Unicode control characters")
    if value.startswith("~") and not allow_tilde:
        raise ConfigError(f"{name} must be an absolute canonical path with one leading slash")
    if value == "~":
        value = str(home)
    elif value.startswith("~/"):
        value = f"{str(home).rstrip('/')}/{value[2:]}"
    elif value.startswith("~"):
        raise ConfigError(f"{name} supports only '~' or '~/' home expansion")
    if not is_canonical_absolute_path(value):
        raise ConfigError(f"{name} must be an absolute canonical path with one leading slash")
    return Path(value)


def is_canonical_absolute_path(value: Any, *, resolved: bool = False) -> bool:
    """Recognize the one stable POSIX spelling for an absolute path."""
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        return False
    if value != "/" and value.endswith("/"):
        return False
    if posixpath.normpath(value) != value:
        return False
    return not resolved or os.path.realpath(value) == value


def reject_symlink_components(path: Path, name: str = "path") -> None:
    """Reject any existing symlink component without requiring the leaf."""
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ConfigError(f"cannot inspect {name} component {current}: {exc}") from exc
        if stat.S_ISLNK(mode):
            raise ConfigError(f"symlink component rejected in {name}: {current}")


def require_existing_directory_components(path: Path, name: str = "path") -> None:
    """Require each existing path component, including the leaf, to be a directory.

    Symlinks are followed; callers that refuse them run reject_symlink_components.
    """
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.stat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ConfigError(f"cannot inspect {name} directory component: {exc}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise ConfigError(f"existing {name} component must be a directory: {current}")


def _open_config_file(path: Path) -> tuple[int, os.stat_result] | None:
    """Open the config file, following symlinks; None when it is absent.

    Where the config lives (a symlinked .asha, a dotfiles leaf, its modes and
    links) is the trusted local user's choice (threat model, 2026-10-05).
    Only a non-regular file is refused: O_NONBLOCK plus S_ISREG keep a FIFO
    from hanging every load.
    """
    try:
        fd = os.open(path, _CONFIG_FLAGS)
    except FileNotFoundError:
        # A dangling leaf symlink is a broken install, not an absent config.
        if os.path.islink(path):
            raise ConfigError("ASHA_CONFIG symlink target does not exist") from None
        return None
    except OSError as exc:
        raise ConfigError(f"cannot open ASHA_CONFIG: {exc}") from exc
    try:
        metadata = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise ConfigError("cannot inspect opened ASHA_CONFIG") from exc
    if not stat.S_ISREG(metadata.st_mode):
        os.close(fd)
        raise ConfigError("ASHA_CONFIG is not a regular file")
    return fd, metadata


def _read_json(path: Path) -> dict[str, Any]:
    opened = _open_config_file(path)
    if opened is None:
        return {}
    fd, metadata = opened
    try:
        if metadata.st_size > MAX_CONFIG_BYTES:
            raise ConfigError(f"ASHA_CONFIG exceeds {MAX_CONFIG_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ConfigError(f"ASHA_CONFIG exceeds {MAX_CONFIG_BYTES} bytes")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except _DuplicateJsonKey as exc:
        raise ConfigError("invalid ASHA_CONFIG JSON: duplicate JSON key") from exc
    except RecursionError as exc:
        raise ConfigError("invalid ASHA_CONFIG JSON: nesting exceeds supported limit") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid ASHA_CONFIG JSON: {exc}") from exc
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    if not isinstance(value, dict):
        raise ConfigError("ASHA_CONFIG root must be an object")
    return value


def load_config(env: Mapping[str, str] | None = None) -> ControlConfig:
    """Parse Control configuration using only the supplied environment."""
    values = os.environ if env is None else env
    raw_home = values.get("HOME", "")
    if not raw_home:
        raise ConfigError("HOME is required")
    home = _absolute(raw_home, "HOME", home=Path("/"), allow_tilde=False)
    reject_symlink_components(home, "HOME")
    require_existing_directory_components(home, "HOME")

    # The root check here is canonical form only. Ancestor modes, owners and
    # symlinks are the trusted local user's layout (threat model, 2026-10-05):
    # a symlinked ASHA_HOME is followed like any other path.
    asha_home = _absolute(
        values.get("ASHA_HOME") or str(home / ".asha"),
        "ASHA_HOME",
        home=home,
    )
    if asha_home == Path("/"):
        raise ConfigError("ASHA_HOME must not be filesystem root")
    if asha_home == home:
        raise ConfigError("ASHA_HOME must not be HOME itself")

    config_path = _absolute(
        values.get("ASHA_CONFIG") or str(asha_home / "config.json"),
        "ASHA_CONFIG",
        home=home,
    )
    root = _read_json(config_path)
    control = root.get("control", {})
    if not isinstance(control, dict):
        raise ConfigError("control must be an object")
    supported_control = {
        "workspace_root", "default_harness", "tmux", "event_staleness_seconds",
        "workspace_trust", "close_wait_seconds",
    } | IGNORED_CONTROL_KEYS
    unknown_control = set(control) - supported_control
    if unknown_control:
        raise ConfigError(f"control has {len(unknown_control)} unsupported field(s)")

    runtime_default = f"/tmp/user-{os.getuid()}"
    using_runtime_fallback = not values.get("XDG_RUNTIME_DIR")
    runtime_home = _absolute(
        values.get("XDG_RUNTIME_DIR") or runtime_default,
        "XDG_RUNTIME_DIR",
        home=home,
    )
    for name, path in (
        ("XDG_RUNTIME_DIR", runtime_home),
    ):
        try:
            reject_symlink_components(path, name)
            require_existing_directory_components(path, name)
            if path == Path("/"):
                raise ConfigError(f"{name} must not be filesystem root")
        except ConfigError as exc:
            if name == "XDG_RUNTIME_DIR" and using_runtime_fallback:
                raise ConfigError(
                    f"{exc}; set XDG_RUNTIME_DIR to an existing private directory"
                ) from exc
            raise

    # Task workspaces were retired (L-b); the key still parses so existing
    # configuration loads.
    raw_workspace = control.get("workspace_root", str(asha_home / "workspaces"))
    if not isinstance(raw_workspace, str) or not raw_workspace:
        raise ConfigError("control.workspace_root must be a non-empty string")
    workspace_root = _absolute(raw_workspace, "control.workspace_root", home=home)

    root_harness = root.get("default_harness")
    if "default_harness" in control:
        nested_harness = control["default_harness"]
        if not isinstance(nested_harness, str) or nested_harness not in HARNESSES:
            raise ConfigError("control.default_harness must name a supported harness")
        default_harness = nested_harness
    elif isinstance(root_harness, str) and root_harness in HARNESSES:
        default_harness = root_harness
    else:
        default_harness = "claude"

    tmux = control.get("tmux", {})
    if not isinstance(tmux, dict):
        raise ConfigError("control.tmux must be an object")
    supported_tmux = {"popup_width", "popup_height", "session_prefix"}
    unknown_tmux = set(tmux) - supported_tmux
    if unknown_tmux:
        raise ConfigError(f"control.tmux has {len(unknown_tmux)} unsupported field(s)")
    popup_width = tmux.get("popup_width", "90%")
    popup_height = tmux.get("popup_height", "85%")
    for name, value in (("popup_width", popup_width), ("popup_height", popup_height)):
        if not isinstance(value, str) or _PERCENT.fullmatch(value) is None:
            raise ConfigError(f"control.tmux.{name} must be a percentage from 1% through 100%")
    session_prefix = tmux.get("session_prefix", "asha-")
    if (not isinstance(session_prefix, str) or not session_prefix.endswith("-") or
            _SESSION_PREFIX.fullmatch(session_prefix) is None):
        raise ConfigError("control.tmux.session_prefix must be a bounded lowercase slug ending in '-'")

    # Recency bound for in-progress semantic event evidence.  A harness without
    # a wired stop/exit event (Codex today) never supersedes a `working` or
    # `needs-input` snapshot, so reconciliation must age it to `unknown` past
    # this window rather than report a stale positive state indefinitely.
    raw_staleness = control.get("event_staleness_seconds", 1800)
    if isinstance(raw_staleness, bool) or not isinstance(raw_staleness, int):
        raise ConfigError("control.event_staleness_seconds must be an integer number of seconds")
    if not 1 <= raw_staleness <= 86400:
        raise ConfigError("control.event_staleness_seconds must be from 1 through 86400")

    close_wait = control.get("close_wait_seconds", CLOSE_WAIT_DEFAULT)
    if isinstance(close_wait, bool) or not isinstance(close_wait, int) or not 0 <= close_wait <= CLOSE_WAIT_LIMIT:
        raise ConfigError(f"control.close_wait_seconds must be an integer from 0 through {CLOSE_WAIT_LIMIT}")

    workspace_trust = control.get("workspace_trust", "inherit")
    if workspace_trust not in TRUST_MODES:
        raise ConfigError(
            "control.workspace_trust must be one of " + ", ".join(TRUST_MODES)
        )

    return ControlConfig(
        config_path=config_path,
        home=home,
        asha_home=asha_home,
        tasks_dir=asha_home / "state/control/tasks",
        workspace_root=workspace_root,
        runtime_dir=runtime_home / "asha-control",
        default_harness=default_harness,
        popup_width=popup_width,
        popup_height=popup_height,
        session_prefix=session_prefix,
        event_staleness_seconds=raw_staleness,
        workspace_trust=workspace_trust,
        close_wait_seconds=close_wait,
    )
