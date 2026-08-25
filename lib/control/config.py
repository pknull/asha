"""Control configuration and path validation without filesystem mutation."""

from __future__ import annotations

import json
import os
import posixpath
import re
import stat
import unicodedata
from .trust import TRUST_MODES
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
    namespace_root: bool = False,
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
    # A leading sticky directory such as /tmp may precede a private boundary:
    # its owner cannot replace an entry owned by the effective user. It is not
    # safe as the managed namespace root or below a private boundary, where an
    # attacker can pre-create the exact child name Control intends to use.
    if is_directory and mode & 0o002 and not sticky:
        return "world-writable non-sticky ancestor", private_boundary
    if is_directory and mode & 0o020 and not sticky:
        return "writable non-sticky ancestor", private_boundary
    if (is_directory and mode & 0o022 and sticky
            and (private_boundary or namespace_root)):
        return "writable sticky ancestor", private_boundary
    # The boundary answers path SUBSTITUTION, which requires write access, so
    # the test is group/other WRITABILITY (0o022) rather than total group/other
    # access (0o077). Demanding 0700 rejected an ordinary 0750 home and with it
    # every repository beneath one, while 0750 already denies creation and
    # replacement to everyone but the owner. A writable descendant never
    # inherits trust from this boundary; the checks above reject it.
    private_boundary = private_boundary or (
        is_directory and metadata.st_uid == euid and not mode & 0o022
    )
    return None, private_boundary


def namespace_remediation(problem: str | None, path: Path) -> str:
    """Name the operator command that clears a writable-ancestor refusal.

    Group- and other-writable directories are the operator's to fix; Control
    never changes the mode of a directory it did not create. Returned text is
    appended to the refusal so `task start`, `task doctor`, and every other
    verb print the same exact remediation.
    """
    if problem is None or "writable" not in problem:
        return ""
    if "sticky" in problem and "non-sticky" not in problem:
        return (
            "; a writable sticky directory cannot host the managed namespace: "
            "point control.workspace_root or XDG state at a private directory"
        )
    return f"; remediate with: chmod g-w,o-w {path}"


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
            metadata, os.geteuid(), private_boundary,
            namespace_root=current == path,
        )
        if problem:
            raise ConfigError(
                f"{problem} rejected in {name}: {current}"
                f"{namespace_remediation(problem, current)}"
            )


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


LEGACY_BANNER_NAME = "ASHA-MOVED.md"
LEGACY_REMEDIATION = "run 'asha migrate' to relocate it; nothing was created"


def legacy_roots(values: Mapping[str, str], home: Path) -> dict[str, Path]:
    """Where a pre-consolidation install kept its trees.

    Detection still honors the old XDG variables: a user who ran with
    XDG_STATE_HOME set has their legacy data there, and the whole point of
    this enumeration is to find what actually exists. It is shared by the
    load_config gate, the doctor probe, drift-check, and `asha migrate`, so
    no two of them can disagree about what "legacy" means.
    """
    state_home = Path(values.get("XDG_STATE_HOME") or (home / ".local/state"))
    data_home = Path(values.get("XDG_DATA_HOME") or (home / ".local/share"))
    return {
        "state": state_home / "asha",
        "control": state_home / "asha" / "control",
        "workspaces": data_home / "asha" / "workspaces",
        "cache": home / ".cache" / "asha",
    }


def migration_layout(values: Mapping[str, str] | None = None) -> dict[str, Path]:
    """Every path `asha migrate` and its probes reason about, in one place.

    Pure derivation — no validation, no filesystem writes — so the migrator
    can run while load_config's own gate is refusing. Legacy locations honor
    the retired XDG variables (that is where a pre-consolidation install kept
    data); new locations derive from ASHA_HOME exactly as load_config does.
    """
    env = os.environ if values is None else values
    raw_home = env.get("HOME", "")
    if not raw_home:
        raise ConfigError("HOME is required")
    home = Path(raw_home)
    asha_home = Path(env.get("ASHA_HOME") or (home / ".asha"))
    legacy = legacy_roots(env, home)
    return {
        "home": home,
        "asha_home": asha_home,
        "legacy_state": legacy["state"],
        "legacy_control": legacy["control"],
        "legacy_workspaces": legacy["workspaces"],
        "legacy_cache": legacy["cache"],
        "new_state": asha_home / "state",
        "new_control": asha_home / "state/control",
        "new_workspaces": asha_home / "workspaces",
        "new_cache": asha_home / "cache",
        "marker": asha_home / "state/.migration-v1.json",
        "journal": asha_home / ".migrate-journal.json",
        "staging_manifest": asha_home / ".migrate-manifest.json",
    }


def legacy_populated(path: Path) -> bool:
    """True when a legacy directory holds anything besides the moved banner.

    `asha migrate` recreates each old root containing exactly one
    ASHA-MOVED.md, so a banner-only directory is evidence of a completed
    migration, never a reason to refuse.
    """
    try:
        entries = [item.name for item in path.iterdir()]
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True  # unreadable legacy data is still legacy data: fail closed
    return any(name != LEGACY_BANNER_NAME for name in entries)


def _refuse_legacy_layout(
    values: Mapping[str, str], home: Path, asha_home: Path, workspace_root: Path,
) -> None:
    """Refuse to run past un-migrated data rather than silently re-home onto
    an empty tree — the CHANGELOG's own post-mortem class of failure."""
    legacy = legacy_roots(values, home)
    if legacy_populated(legacy["control"]) and not (asha_home / "state/control").exists():
        raise ConfigError(
            f"legacy Asha state detected at {legacy['control']}; the root moved to "
            f"{asha_home}/state/control — {LEGACY_REMEDIATION}"
        )
    default_workspaces = asha_home / "workspaces"
    if (workspace_root == default_workspaces
            and legacy_populated(legacy["workspaces"])
            and not default_workspaces.exists()):
        raise ConfigError(
            f"legacy Asha workspaces detected at {legacy['workspaces']}; the root "
            f"moved to {default_workspaces} — {LEGACY_REMEDIATION}"
        )


def load_config(
    env: Mapping[str, str] | None = None, *, check_legacy: bool = True,
) -> ControlConfig:
    """Parse Control configuration using only the supplied environment."""
    values = os.environ if env is None else env
    raw_home = values.get("HOME", "")
    if not raw_home:
        raise ConfigError("HOME is required")
    home = _absolute(raw_home, "HOME", home=Path("/"), allow_tilde=False)
    reject_symlink_components(home, "HOME")
    require_existing_directory_components(home, "HOME")

    # The root check here is canonical form plus ancestor writability. A
    # symlinked ASHA_HOME needs no extra rejection: the config file's own
    # parent guard in _open_config_file refuses it (as it always refused a
    # symlinked ~/.asha), and the state store's traversal validators plus the
    # orchestration doctor's symlink-free root probe govern the machine-state
    # subtree. The supported dotfiles pattern is a REAL .asha directory whose
    # leaf files are symlinks.
    asha_home = _absolute(
        values.get("ASHA_HOME") or str(home / ".asha"),
        "ASHA_HOME",
        home=home,
    )
    if asha_home == Path("/"):
        raise ConfigError("ASHA_HOME must not be filesystem root")
    if asha_home == home:
        raise ConfigError("ASHA_HOME must not be HOME itself")
    # The ancestor-safety walk runs AFTER the config file is read, preserving
    # the long-standing error precedence: a malformed config file reports as
    # such even when a fixture home would also fail the namespace rule.

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
        "workspace_trust",
    }
    unknown_control = set(control) - supported_control
    if unknown_control:
        raise ConfigError(f"control has {len(unknown_control)} unsupported field(s)")

    reject_unsafe_writable_ancestors(asha_home, "ASHA_HOME")

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
            reject_unsafe_writable_ancestors(path, name)
            if path == Path("/"):
                raise ConfigError(f"{name} must not be filesystem root")
        except ConfigError as exc:
            if name == "XDG_RUNTIME_DIR" and using_runtime_fallback:
                raise ConfigError(
                    f"{exc}; set XDG_RUNTIME_DIR to an existing private directory "
                    "owned by the effective user"
                ) from exc
            raise

    raw_workspace = control.get("workspace_root", str(asha_home / "workspaces"))
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

    workspace_trust = control.get("workspace_trust", "inherit")
    if workspace_trust not in TRUST_MODES:
        raise ConfigError(
            "control.workspace_trust must be one of " + ", ".join(TRUST_MODES)
        )

    # The gate protects the DEFAULT LOCATION, judged by value, not by whether
    # the variable is set: bin/asha exports ASHA_HOME unconditionally so that
    # children agree, which would otherwise make every CLI invocation look
    # "explicit" and neuter the gate on exactly the path real operators use
    # (verified live: task list sailed onto an empty tree past 65 un-migrated
    # records). A redirection to somewhere else — tests, sandboxes, expert
    # layouts — still bypasses, because it touches nothing the gate protects.
    if check_legacy and asha_home == home / ".asha":
        _refuse_legacy_layout(values, home, asha_home, workspace_root)

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
    )
