"""Pure/injected capability probes with an isolated read-only tmux check."""

from __future__ import annotations

import sys
import os
import json
import re
import shlex
import shutil
import stat
import subprocess
import tomllib
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Any

from .socket_reaper import TmuxSocketReaper
from .store import StoreError
from .tmux import TmuxAdapter, TmuxError


@dataclass(frozen=True)
class Probe:
    name: str
    outcome: str
    detail: str

    def __post_init__(self) -> None:
        if (not isinstance(self.name, str) or
                re.fullmatch(r"[a-z][a-z0-9-]{0,31}", self.name) is None):
            raise ValueError("doctor probe name uses an invalid restricted grammar")
        if (not isinstance(self.detail, str) or not self.detail or len(self.detail) > 500 or
                any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in self.detail)):
            raise ValueError("doctor probe detail must be 1-500 printable characters")
        if self.outcome not in {"match", "missing", "mismatch", "unavailable"}:
            raise ValueError("invalid doctor probe outcome")


ProbeFunction = Callable[[Any], Probe]
_CONTROL_EVENT_HANDLER = (
    Path(__file__).resolve().parents[2]
    / "plugins/session/hooks/handlers/control-event.sh"
)
def supervisor_service_status(
    values: Mapping[str, str], *, runner=None, which=None,
) -> dict[str, bool | None]:
    # Keep the supervisor service and its unit logic out of doctor import.
    from .supervisor_service import supervisor_service_status as inspect
    return inspect(values, runner=runner, which=which)


def _python_probe(config) -> Probe:
    if sys.version_info >= (3, 11):
        return Probe("python", "match", f"Python {sys.version_info.major}.{sys.version_info.minor} supports the Control core")
    return Probe("python", "mismatch", "Python 3.11 or newer is required")


def _experience_configuration_probe(config, *, env=None) -> Probe:
    from .projects import experience_default
    values = dict(os.environ if env is None else env)
    if config is not None:
        values['HOME'] = str(config.home)
    mode, source, error = experience_default(values)
    return Probe('session-experience', 'mismatch' if error else 'match', error or
                 f"Session experience default: {mode} ({source}); native automatic review remains gated")


def _configuration_probe(config) -> Probe:
    if config is None:
        return Probe("configuration", "unavailable", "configuration was not supplied to the pure probe")
    return Probe("configuration", "match", "Control configuration parsed and paths passed static safety validation")


def _safe_detail(value: Any) -> str:
    text = "".join(char if char.isprintable() else "?" for char in str(value))
    return text[:400] or "no diagnostic"


def _tmux_probe(config) -> Probe:
    executable = shutil.which("tmux")
    if executable is None:
        return Probe("tmux", "unavailable", "tmux executable was not found on PATH")
    try:
        version_adapter = TmuxAdapter(executable=executable)
        returncode, stdout, stderr = version_adapter._run_status(["-V"])
        if returncode != 0:
            return Probe(
                "tmux", "unavailable",
                f"tmux -V failed: {_safe_detail(stderr.decode('utf-8', errors='replace'))}",
            )
        version = stdout.decode("utf-8").strip()
        if re.fullmatch(r"tmux [0-9]+(?:\.[0-9]+)?[a-z]?", version) is None:
            return Probe("tmux", "unavailable", "tmux -V returned an unrecognized version")
        socket = f"asha-doctor-probe-{os.getpid()}"
        with TmuxSocketReaper(socket, executable=executable):
            probe = TmuxAdapter(
                executable=executable, socket=socket, config_file=Path("/dev/null"),
            )
            returncode, popup, stderr = probe._run_status([
                "list-commands", "display-popup",
            ])
            if returncode != 0:
                return Probe(
                    "tmux", "unavailable",
                    "tmux display-popup capability probe failed: "
                    + _safe_detail(stderr.decode("utf-8", errors="replace")),
                )
            output = popup.decode("utf-8")
            if "display-popup" not in output:
                return Probe("tmux", "unavailable", "tmux does not report display-popup support")
            return Probe(
                "tmux", "match",
                f"{version} resolves and supports display-popup on an isolated no-server probe",
            )
    except (TmuxError, UnicodeError, OSError) as exc:
        return Probe(
            "tmux", "unavailable",
            f"tmux capability probe unavailable: {_safe_detail(exc)}",
        )


def _harness_probe(config, *, required_harnesses=None) -> Probe:
    names = required_harnesses if required_harnesses is not None else ("claude", "codex", "copilot", "opencode")
    resolved = [name for name in names if shutil.which(name) is not None]
    missing = [name for name in names if name not in resolved]
    detail = (
        f"resolved: {', '.join(resolved) if resolved else 'none'}; "
        f"missing: {', '.join(missing) if missing else 'none'}"
    )
    available = not missing if required_harnesses is not None else bool(resolved)
    return Probe("harness", "match" if available else "unavailable", detail)


def _gh_probe(config) -> Probe:
    executable = shutil.which("gh")
    scope = "gh is required only for --pr and --issue; ad-hoc tasks do not require it"
    if executable is None:
        return Probe("gh", "unavailable", f"gh executable was not found on PATH; {scope}")
    return Probe(
        "gh", "match",
        f"gh resolves on PATH; authentication is checked when GitHub source mode starts; {scope}",
    )


def _tui_probe(config) -> Probe:
    try:
        import curses
    except (ImportError, OSError) as exc:
        return Probe(
            "tui", "unavailable",
            f"Python curses support is unavailable: {_safe_detail(exc)}",
        )
    try:
        curses.setupterm()
    except (curses.error, OSError) as exc:
        return Probe(
            "tui", "unavailable",
            f"terminal capability database is unavailable: {_safe_detail(exc)}",
        )
    return Probe(
        "tui", "match",
        "Python curses and the terminal capability database are available",
    )


def _read_install_config(path: Path) -> str:
    fd = -1
    try:
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{path} is not a regular file")
        chunks: list[bytes] = []
        remaining = 1024 * 1024 + 1
        while remaining:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
    if len(raw) > 1024 * 1024:
        raise ValueError(f"{path} exceeds the bounded installation probe limit")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not UTF-8") from exc


def _control_hook_command(command: Any, native_event: str) -> bool:
    if not isinstance(command, str) or len(command) > 4096:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    for index, token in enumerate(tokens[:-1]):
        path = Path(token)
        if (path.name == "control-event.sh" and path.is_absolute() and
                path == _CONTROL_EVENT_HANDLER and
                path.is_file() and os.access(path, os.X_OK) and
                tokens[index + 1] == native_event):
            return True
    return False


def _claimed_hook_homes(config) -> tuple[Path, Path]:
    claude = config.home / ".claude"
    codex = config.home / ".codex"
    # Honor harness-specific homes only when this probe is using the process's
    # real HOME. Injected doctor tests must remain confined to their config.
    if os.environ.get("HOME") == str(config.home):
        claude = Path(os.environ.get("CLAUDE_HOME", str(claude)))
        codex = Path(os.environ.get("CODEX_HOME", str(codex)))
    return claude, codex


def codex_hooks_probe(home: Path, asha_home: Path, *, user_home: Path,
                      root: Path | None = None, with_canary: bool = False) -> Probe:
    """Read-only installed drift, shared by Control and the installed doctor.

    The real adapter supplies the expected commands, the native config bytes and
    the installed hooks.json only when the ledger records exactly those bytes.
    No copied renderer can turn an empty extraction into green. No staging or
    native trust operation runs.
    """
    root = root or Path(__file__).resolve().parents[2]
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(user_home),
           "CODEX_HOME": str(home), "ASHA_HOME": str(asha_home),
           "PYTHONDONTWRITEBYTECODE": "1"}
    script = ('source "$1/lib/install.sh"; DRY_RUN=1; FORCE=0; VERBOSE=0; ONLY=""; '
              'WITH_CANARY="$2"; source "$1/harnesses/codex.sh"; '
              '_codex_prepare_hooks inspect')
    try:
        result = subprocess.run(
            ["bash", "-c", script, "codex-hooks-probe", str(root), str(int(with_canary))],
            env=env, cwd=root, capture_output=True, timeout=45,
        )
        if result.returncode:
            raise ValueError(result.stderr.decode("utf-8", "replace")[-2000:])
        plan = json.loads(result.stdout)
        expected = json.loads(plan["content"])["hooks"]
        value = tomllib.loads(plan["config_text"])
        groups = (json.loads(plan["json_text"])["hooks"]
                  if plan["json_text"] is not None else {})
        missing = [event for event, wanted in expected.items()
                   if any(groups.get(event, []).count(group) != 1 for group in wanted)]
        if not expected or missing:
            return Probe("hooks", "missing", "Codex missing/duplicate expected hook groups: " +
                         (", ".join(missing) or "empty source selection"))
        if groups != expected:
            return Probe("hooks", "mismatch", "Codex owned hooks.json differs from selected source commands/filters")
        commands = [h["command"] for blocks in expected.values()
                    for group in blocks for h in group["hooks"]]
        for command in commands:
            words = shlex.split(command)
            if words[:2] != ["env", "ASHA_HARNESS=codex"] or len(words) < 3:
                raise ValueError("invalid expected Codex harness command seam")
            executable = Path(words[2])
            if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
                return Probe("hooks", "missing", "Codex expected hook executable missing: " + str(executable)[-350:])
        again = subprocess.run(
            ["bash", "-c", script, "codex-hooks-probe", str(root), str(int(with_canary))],
            env=env, cwd=root, capture_output=True, timeout=45,
        )
        if again.returncode or json.loads(again.stdout) != plan:
            raise ValueError("Codex hook evidence changed while inspecting")
        feature = value.get("features", {}).get("hooks")
        if feature is False:
            return Probe("hooks", "mismatch", "Codex hooks registered but disabled: explicit features.hooks=false; trust/execution unverified")
        feature_detail = "explicit features.hooks=true"
        if feature is None:
            binary = shutil.which("codex", path=env["PATH"])
            if not binary:
                return Probe("hooks", "unavailable", "Codex hooks registered; absent feature flag and native version unavailable; trust/execution unverified")
            version = subprocess.run([binary, "--version"], env=env, capture_output=True, timeout=5)
            if version.returncode or version.stdout.strip() != b"codex-cli 0.153.4":
                return Probe("hooks", "unavailable", "Codex hooks registered; absent feature flag default unsupported for this native version; trust/execution unverified")
            feature_detail = "0.153.4 default-true evidence only"
        mixed = "; mixed foreign inline/JSON sources" if value.get("hooks", {}).keys() - {"state"} else ""
        return Probe("hooks", "match", f"Codex {len(commands)} expected commands registered, executable paths verified; verification Stop and recovery PostToolUse checked; {feature_detail}{mixed}; native trust and execution NOT verified")
    except (OSError, ValueError, TypeError, KeyError, RecursionError, subprocess.TimeoutExpired) as exc:
        return Probe("hooks", "unavailable", "Codex hook inspection refused: " + _safe_detail(exc)[:460])


def _hooks_probe(config, *, required_harnesses=None) -> Probe:
    if config is None:
        return Probe(
            "hooks", "unavailable",
            "configuration was not supplied to the hook installation probe",
        )
    claude_home, codex_home = _claimed_hook_homes(config)
    claude_path = claude_home / "settings.json"
    codex_path = codex_home / "config.toml"
    installed: list[str] = []
    for name, path in (("claude", claude_path), ("codex", codex_path),
                       ("codex", codex_home / "hooks.json"),
                       ("codex", config.asha_home / "install-manifests/codex.json")):
        if required_harnesses is not None and name not in required_harnesses:
            continue
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            pass
        if name not in installed:
            installed.append(name)
    if required_harnesses is not None:
        required_hooks = set(required_harnesses) & {"claude", "codex"}
        absent = sorted(required_hooks - set(installed))
        if absent:
            return Probe("hooks", "mismatch", "required harness hook installation is absent: " + ", ".join(absent))
        if not required_hooks:
            return Probe("hooks", "match", "selected harnesses claim no Claude/Codex hook contract; other harness hooks are not inspected by this probe")
    if not installed:
        return Probe(
            "hooks", "match",
            "no installed Claude or Codex configuration requires Control hook inspection",
        )
    expected_claude = {
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
        "Stop", "SessionEnd",
    }
    codex_probe = None
    missing: list[str] = []
    try:
        if "claude" in installed:
            claude_value = json.loads(_read_install_config(claude_path))
            if not isinstance(claude_value, dict):
                raise ValueError("Claude settings root is not an object")
            claude_hooks = claude_value.get("hooks", {})
            if not isinstance(claude_hooks, dict):
                raise ValueError("Claude hooks root is not an object")
            for event in sorted(expected_claude):
                groups = claude_hooks.get(event, [])
                found = any(
                    _control_hook_command(hook.get("command"), event)
                    for group in groups if isinstance(group, dict)
                    for hook in group.get("hooks", []) if isinstance(hook, dict)
                ) if isinstance(groups, list) else False
                if not found:
                    missing.append(f"claude:{event}")

        if "codex" in installed:
            codex_probe = codex_hooks_probe(codex_home, config.asha_home, user_home=config.home)
            if codex_probe.outcome != "match":
                return codex_probe
    except (ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError, RecursionError) as exc:
        return Probe(
            "hooks", "unavailable",
            f"Control hook installation could not be inspected: {_safe_detail(exc)}",
        )
    if missing:
        detail = "missing expected Control hooks: " + ", ".join(missing)
        return Probe("hooks", "missing", detail[:500])
    labels = [name.title() for name in installed]
    return Probe(
        "hooks", "match",
        (("Claude Control hooks checked; " if "claude" in installed else "") + codex_probe.detail)[:500]
        if codex_probe else f"expected {' and '.join(labels)} Control hook command paths are installed and executable",
    )


def _rooms_registry_probe(config) -> Probe:
    """Authenticate durable Room records without mutating or contacting tmux."""
    if config is None:
        return Probe(
            "rooms-registry", "unavailable",
            "configuration was not supplied to the Rooms registry probe",
        )
    from .rooms import RoomError, RoomStore

    try:
        rooms = RoomStore(config).list()
    except (RoomError, StoreError, OSError) as exc:
        return Probe(
            "rooms-registry", "mismatch",
            "durable Room records could not be authenticated: " + _safe_detail(exc),
        )
    open_count = sum(1 for room in rooms if room.get("lifecycle") != "ended")
    return Probe(
        "rooms-registry", "match",
        f"{len(rooms)} durable Room record(s) authenticated; {open_count} not closed",
    )


def _migration_probe(config) -> Probe:
    """Whether the single-root migration is complete, pending, or disturbed."""
    from .config import LEGACY_BANNER_NAME, legacy_populated, migration_layout

    if config is None:
        return Probe("migration", "unavailable", "configuration was not supplied")
    try:
        layout = migration_layout({"HOME": str(config.home), "ASHA_HOME": str(config.asha_home)})
    except Exception as exc:  # noqa: BLE001 - a probe reports, never raises
        return Probe("migration", "unavailable", f"layout underivable: {exc}")
    marker = layout["marker"].is_file()
    control_live = legacy_populated(layout["legacy_control"])
    workspaces_live = legacy_populated(layout["legacy_workspaces"])
    if control_live or workspaces_live:
        where = layout["legacy_control"] if control_live else layout["legacy_workspaces"]
        if marker:
            return Probe(
                "migration", "mismatch",
                f"legacy data reappeared at {where} after the migration marker; a "
                f"restore resurrected superseded files — compare against {layout['marker']}",
            )
        return Probe(
            "migration", "mismatch",
            f"legacy Control state remains at {where}; run: asha migrate",
        )
    if marker:
        return Probe("migration", "match", "single-root migration complete; legacy roots hold only banners")
    if layout["legacy_state"].exists() and not (layout["legacy_state"] / LEGACY_BANNER_NAME).is_file():
        return Probe("migration", "match", "no legacy state and no marker: nothing ever needed migrating")
    return Probe("migration", "match", "no legacy Asha state present")


def _supervisor_service_probe(config, *, env=None, runner=None, which=None) -> Probe:
    values = dict(os.environ if env is None else env)
    if config is not None:
        values["HOME"] = str(config.home)
        values["ASHA_HOME"] = str(config.asha_home)
    status = supervisor_service_status(values, runner=runner, which=which)
    present = status["service_present"]
    enabled = status["service_enabled"]
    active = status["service_active"]
    if present is None:
        return Probe(
            "supervisor-service", "unavailable",
            "systemctl is unavailable; supervisor service state was not probed",
        )
    def label(value: bool | None) -> str:
        return "unknown" if value is None else "yes" if value else "no"

    detail = (
        f"supervisor service present={label(present)}, "
        f"enabled={label(enabled)}, active={label(active)}"
    )
    if not present:
        outcome = "missing"
    elif enabled and active:
        outcome = "match"
    else:
        outcome = "mismatch"
    return Probe("supervisor-service", outcome, detail)


def _managed_sessions_probe(config) -> Probe:
    if config is None:
        return Probe("managed-sessions", "unavailable", "configuration was not supplied")
    if not (config.tasks_dir.parent / "control.sqlite3").exists():
        return Probe("managed-sessions", "match", "optional managed-session database not initialized; session doctor reports adapter capabilities")
    try:
        from .database import ControlDatabase
        from .sessions import overview
        from .session_ipc import capability_probe
        with ControlDatabase(config) as database:
            health = database.health()
        summary = overview(config)["summary"]
        if health["integrity"] != "ok" or health["relationships"] != "ok":
            return Probe("managed-sessions", "mismatch", "SQLite integrity check failed")
        ipc = capability_probe()
        if not ipc["supported"]:
            return Probe("managed-sessions", "mismatch", _safe_detail("managed request IPC unavailable: " + ipc["reason"]))
        return Probe("managed-sessions", "match", _safe_detail(
            f"SQLite {health['sqlite_version']} schema {health['schema_version']}, WAL/FULL, FTS5; {summary}"))
    except (StoreError, OSError, ValueError) as exc:
        return Probe("managed-sessions", "mismatch", _safe_detail(
            f"managed state unavailable: {exc}; session init can repair empty initialization"))


DEFAULT_PROBES: Mapping[str, ProbeFunction] = {
    "python": _python_probe,
    "configuration": _configuration_probe,
    "session-experience": _experience_configuration_probe,
    "migration": _migration_probe,
    "supervisor-service": _supervisor_service_probe,
    "tmux": _tmux_probe,
    "harness": _harness_probe,
    "gh": _gh_probe,
    "rooms-registry": _rooms_registry_probe,
    "managed-sessions": _managed_sessions_probe,
    "hooks": _hooks_probe,
    "tui": _tui_probe,
}


def run_doctor(
    config, probes: Mapping[str, ProbeFunction] | None = None, *,
    env: Mapping[str, str] | None = None, runner=None, which=None, required_harnesses=None,
) -> dict[str, Any]:
    if required_harnesses is not None:
        if (not isinstance(required_harnesses, (tuple, list)) or not required_harnesses
                or any(name not in ("claude", "codex", "copilot", "opencode") for name in required_harnesses)):
            raise ValueError("required harnesses must name supported execution harnesses")
    selected = DEFAULT_PROBES if probes is None else probes
    results: list[Probe] = []
    for name, probe in selected.items():
        if (not isinstance(name, str) or
                re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name) is None):
            raise ValueError("invalid doctor probe name")
        if probe is _experience_configuration_probe:
            result = probe(config, env=env)
        elif probe is _supervisor_service_probe:
            result = probe(config, env=env, runner=runner, which=which)
        elif probe in (_harness_probe, _hooks_probe) and required_harnesses is not None:
            result = probe(config, required_harnesses=required_harnesses)
        else:
            result = probe(config)
        if not isinstance(result, Probe) or result.name != name:
            raise ValueError(f"doctor probe {name} returned an invalid result")
        results.append(Probe(result.name, result.outcome, result.detail))
    limitations = [result.detail for result in results if result.outcome != "match"]
    # GitHub support and supervisor service state are contextual; report them
    # without failing the general check.
    blocking = [
        result for result in results
        if result.outcome != "match" and result.name not in {"gh", "supervisor-service"}
    ]
    return {
        "contract": "asha.control-doctor.v1",
        "ok": not blocking,
        "probes": [asdict(result) for result in results],
        "limitations": limitations,
    }
