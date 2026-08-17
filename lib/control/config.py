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
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_CONFIG_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
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


@dataclass(frozen=True)
class ControlConfig:
    config_path: Path
    home: Path
    tasks_dir: Path
    workspace_root: Path
    runtime_dir: Path
    default_harness: str
    popup_width: str
    popup_height: str
    session_prefix: str
    event_staleness_seconds: int


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
    """Require each existing path component, including the leaf, to be a directory."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ConfigError(f"cannot inspect {name} directory component: {exc}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise ConfigError(f"existing {name} component must be a directory: {current}")


def namespace_ancestor_problem(
    metadata: os.stat_result, euid: int, *, root_uid: int | None = None
) -> str | None:
    """Return the safety failure for an existing namespace ancestor, if any."""
    problem, _ = namespace_safety_step(
        metadata, euid, private_boundary=False, root_uid=root_uid
    )
    return problem


def namespace_safety_step(
    metadata: os.stat_result,
    euid: int,
    private_boundary: bool,
    *,
    root_uid: int | None = None,
) -> tuple[str | None, bool]:
    """Validate one ancestor and carry forward a private namespace boundary."""
    # User namespaces can map the filesystem-root owner away from numeric UID 0.
    if root_uid is None:
        root_uid = os.stat("/").st_uid
    if metadata.st_uid not in {0, root_uid, euid}:
        return "ancestor is not owned by root or the effective user", private_boundary
    mode = metadata.st_mode
    is_directory = stat.S_ISDIR(mode)
    sticky = bool(mode & stat.S_ISVTX)
    # Other-writable is never tolerable, boundary or not: any user on the system
    # could substitute an entry here, so no amount of trust established further
    # up the path makes it safe. Sticky directories (/tmp) are exempt because
    # only an entry's own owner may remove or rename it.
    if is_directory and mode & 0o002 and not sticky:
        return "world-writable non-sticky ancestor", private_boundary
    # Group-writable is tolerable only beneath an established private boundary,
    # and only for a directory we own.
    if (is_directory and mode & 0o020 and not sticky
            and not (private_boundary and metadata.st_uid == euid)):
        return "writable non-sticky ancestor", private_boundary
    # The boundary answers path SUBSTITUTION, which requires write access, so
    # the test is group/other WRITABILITY (0o022) rather than total group/other
    # access (0o077). Demanding 0700 rejected an ordinary 0750 home and with it
    # every repository beneath one, while 0750 already denies creation and
    # replacement to everyone but the owner. Pairing this with the absolute
    # other-writable refusal above keeps the property the original 0700 rule
    # protected: a permissive descendant is never silently trusted just because
    # some ancestor was.
    private_boundary = private_boundary or (
        is_directory and metadata.st_uid == euid and not mode & 0o022
    )
    return None, private_boundary


def reject_unsafe_writable_ancestors(path: Path, name: str = "path") -> None:
    """Reject existing untrusted namespace ancestors."""
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    current = Path(path.anchor)
    private_boundary = False
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ConfigError(f"cannot inspect {name} ancestor {current}: {exc}") from exc
        problem, private_boundary = namespace_safety_step(
            metadata, os.geteuid(), private_boundary
        )
        if problem:
            raise ConfigError(f"{problem} rejected in {name}: {current}")


def require_owned_directory_ancestors(path: Path, name: str) -> None:
    """Require a read-only path's existing parents to be root/euid-owned directories."""
    if not path.is_absolute():
        raise ConfigError(f"{name} must be an absolute path")
    allowed_owners = {0, os.stat("/").st_uid, os.geteuid()}
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ConfigError(f"cannot inspect {name} ancestor: {exc}") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise ConfigError(f"existing {name} ancestor must be a directory: {current}")
        if metadata.st_uid not in allowed_owners:
            raise ConfigError(f"{name} ancestor is not owned by root or the effective user")


def validate_workspace_root(
    workspace_root: Path,
    *,
    home: Path,
    repository: Path | None = None,
) -> Path:
    if not is_canonical_absolute_path(str(workspace_root)):
        raise ConfigError("control.workspace_root must be an absolute canonical path")
    if not is_canonical_absolute_path(str(home)):
        raise ConfigError("HOME must be an absolute canonical path")
    if workspace_root == Path("/"):
        raise ConfigError("control.workspace_root must not be filesystem root")
    if workspace_root == home:
        raise ConfigError("control.workspace_root must not be HOME")
    if repository is not None:
        if not is_canonical_absolute_path(str(repository), resolved=True):
            raise ConfigError("source repository must be an exact resolved canonical path")
        if workspace_root == repository:
            raise ConfigError("control.workspace_root must not be the source repository")
        if repository.is_relative_to(workspace_root):
            raise ConfigError("control.workspace_root must not be an ancestor of the source repository")
        if workspace_root.is_relative_to(repository):
            raise ConfigError("control.workspace_root must not be below the source repository")
    reject_symlink_components(workspace_root, "control.workspace_root")
    require_existing_directory_components(workspace_root, "control.workspace_root")
    reject_unsafe_writable_ancestors(workspace_root, "control.workspace_root")
    return workspace_root


def _resolved_config_target(path: Path, raw_target: str) -> Path:
    if (not raw_target or len(raw_target) > 4096 or
            any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in raw_target)):
        raise ConfigError("ASHA_CONFIG symlink target is invalid")
    if raw_target.startswith("/"):
        if not is_canonical_absolute_path(raw_target):
            raise ConfigError("ASHA_CONFIG symlink target must be an absolute canonical path")
        target = Path(raw_target)
    else:
        target = Path(posixpath.normpath(f"{path.parent}/{raw_target}"))
        if not is_canonical_absolute_path(str(target)):
            raise ConfigError("ASHA_CONFIG symlink target did not resolve to a canonical path")
    if not is_canonical_absolute_path(str(target), resolved=True):
        raise ConfigError("symlink chain or parent alias rejected in ASHA_CONFIG target")
    reject_symlink_components(target, "ASHA_CONFIG target")
    require_existing_directory_components(target.parent, "ASHA_CONFIG target parent")
    require_owned_directory_ancestors(target.parent, "ASHA_CONFIG target")
    return target


def _open_config_file(path: Path) -> tuple[int, os.stat_result] | None:
    """Open a direct config or one owned leaf symlink without following aliases."""
    reject_symlink_components(path.parent, "ASHA_CONFIG parent")
    require_existing_directory_components(path.parent, "ASHA_CONFIG parent")
    require_owned_directory_ancestors(path.parent, "ASHA_CONFIG parent")
    try:
        leaf_metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"cannot inspect ASHA_CONFIG: {exc}") from exc

    link_target: str | None = None
    open_path = path
    if stat.S_ISLNK(leaf_metadata.st_mode):
        if leaf_metadata.st_uid != os.geteuid():
            raise ConfigError("ASHA_CONFIG symlink is not owned by the effective user")
        try:
            link_target = os.readlink(path)
        except OSError as exc:
            raise ConfigError(f"cannot read ASHA_CONFIG symlink: {exc}") from exc
        open_path = _resolved_config_target(path, link_target)

    parts = open_path.parts[1:]
    if not parts:
        raise ConfigError("ASHA_CONFIG must name a regular file")
    try:
        parent_fd = os.open("/", _DIRECTORY_FLAGS)
    except OSError as exc:
        raise ConfigError(f"cannot open ASHA_CONFIG root: {exc}") from exc
    try:
        for part in parts[:-1]:
            try:
                child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise ConfigError(f"symlink or invalid parent rejected in ASHA_CONFIG: {exc}") from exc
            try:
                parent_metadata = os.fstat(child_fd)
                if parent_metadata.st_uid not in {
                    0, os.stat("/").st_uid, os.geteuid()
                }:
                    raise ConfigError(
                        "ASHA_CONFIG parent is not owned by root or the effective user"
                    )
            except Exception:
                os.close(child_fd)
                raise
            os.close(parent_fd)
            parent_fd = child_fd
        try:
            fd = os.open(parts[-1], _CONFIG_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            if link_target is None:
                return None
            raise ConfigError("ASHA_CONFIG symlink target does not exist") from exc
        except OSError as exc:
            raise ConfigError(f"cannot open ASHA_CONFIG without following symlinks: {exc}") from exc
        try:
            metadata = os.fstat(fd)
        except OSError as exc:
            os.close(fd)
            raise ConfigError("cannot inspect opened ASHA_CONFIG") from exc
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            raise ConfigError("ASHA_CONFIG is not a regular file")
        if metadata.st_uid != os.geteuid():
            os.close(fd)
            raise ConfigError("ASHA_CONFIG file is not owned by the effective user")
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            os.close(fd)
            raise ConfigError("ASHA_CONFIG file must not be group/world-writable")
        if metadata.st_nlink != 1:
            os.close(fd)
            raise ConfigError("ASHA_CONFIG file link count must be exactly 1")
        if link_target is None:
            if (metadata.st_dev, metadata.st_ino) != (
                leaf_metadata.st_dev, leaf_metadata.st_ino
            ):
                os.close(fd)
                raise ConfigError("ASHA_CONFIG changed while it was being opened")
        else:
            try:
                current_link = path.lstat()
                current_target = os.readlink(path)
            except OSError as exc:
                os.close(fd)
                raise ConfigError("ASHA_CONFIG symlink changed while it was being opened") from exc
            if ((current_link.st_dev, current_link.st_ino, current_link.st_uid) !=
                    (leaf_metadata.st_dev, leaf_metadata.st_ino, leaf_metadata.st_uid) or
                    current_target != link_target):
                os.close(fd)
                raise ConfigError("ASHA_CONFIG symlink changed while it was being opened")
        return fd, metadata
    finally:
        os.close(parent_fd)


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

    config_path = _absolute(
        values.get("ASHA_CONFIG") or str(home / ".asha/config.json"),
        "ASHA_CONFIG",
        home=home,
    )
    root = _read_json(config_path)
    control = root.get("control", {})
    if not isinstance(control, dict):
        raise ConfigError("control must be an object")
    supported_control = {
        "workspace_root", "default_harness", "tmux", "event_staleness_seconds",
    }
    unknown_control = set(control) - supported_control
    if unknown_control:
        raise ConfigError(f"control has {len(unknown_control)} unsupported field(s)")

    state_home = _absolute(
        values.get("XDG_STATE_HOME") or str(home / ".local/state"),
        "XDG_STATE_HOME",
        home=home,
    )
    data_home = _absolute(
        values.get("XDG_DATA_HOME") or str(home / ".local/share"),
        "XDG_DATA_HOME",
        home=home,
    )
    runtime_default = f"/tmp/user-{os.getuid()}"
    runtime_home = _absolute(
        values.get("XDG_RUNTIME_DIR") or runtime_default,
        "XDG_RUNTIME_DIR",
        home=home,
    )
    for name, path in (
        ("XDG_STATE_HOME", state_home),
        ("XDG_DATA_HOME", data_home),
        ("XDG_RUNTIME_DIR", runtime_home),
    ):
        reject_symlink_components(path, name)
        require_existing_directory_components(path, name)
        reject_unsafe_writable_ancestors(path, name)
        if path == Path("/"):
            raise ConfigError(f"{name} must not be filesystem root")

    raw_workspace = control.get("workspace_root", str(data_home / "asha/workspaces"))
    if not isinstance(raw_workspace, str) or not raw_workspace:
        raise ConfigError("control.workspace_root must be a non-empty string")
    workspace_root = validate_workspace_root(
        _absolute(raw_workspace, "control.workspace_root", home=home),
        home=home,
    )

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

    return ControlConfig(
        config_path=config_path,
        home=home,
        tasks_dir=state_home / "asha/control/tasks",
        workspace_root=workspace_root,
        runtime_dir=runtime_home / "asha-control",
        default_harness=default_harness,
        popup_width=popup_width,
        popup_height=popup_height,
        session_prefix=session_prefix,
        event_staleness_seconds=raw_staleness,
    )
