"""Pure/injected capability probes with an isolated read-only tmux check."""

from __future__ import annotations

import sys
import os
import json
import re
import shlex
import shutil
import stat
import tomllib
import unicodedata
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Mapping, Any

from .events import EventError, read_snapshot
from .context import (
    DYNAMIC_PRIVATE_CONTEXT_DIRECTORIES, build_context_plan,
    read_published_snapshot,
)
from .prune import prunable_summary
from .jj import (
    ColocationIntentStore, ContextCompatibilityError, JjAdapter, JjError,
    LinkedGitWorktreeError, inspect_pre_enable_binding,
    colocated_sync_remediation, discover_git_root,
)
from .process import capture_bytes
from .prepare import retained_recovery_guidance
from .prepare import derive_repository_identity
from .store import StoreError, TaskStore, validate_task_paths
from .tmux import TmuxAdapter, TmuxError
from .transaction import CreationJournalStore, JournalError


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
# The jj surface Control actually depends on. Probing each leaf command's own
# help proves the semantics are present rather than trusting a version string,
# which the jj contract requires, and every one of these exits 0 outside a
# repository so the probe contacts no repository at all.
_JJ_REQUIRED_COMMANDS = (
    ("workspace", "add"),
    ("workspace", "forget"),
    ("operation", "log"),
    ("git", "init"),
    ("git", "import"),
)
_CONTROL_EVENT_HANDLER = (
    Path(__file__).resolve().parents[2]
    / "plugins/session/hooks/handlers/control-event.sh"
)
# Only these harnesses have live-proven native event delivery into Control.
_SEMANTIC_EVENT_HARNESSES = frozenset({"claude", "codex"})


def _python_probe(config) -> Probe:
    if sys.version_info >= (3, 11):
        return Probe("python", "match", f"Python {sys.version_info.major}.{sys.version_info.minor} supports the Control core")
    return Probe("python", "mismatch", "Python 3.11 or newer is required")


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


def _harness_probe(config) -> Probe:
    names = ("claude", "codex", "copilot", "opencode")
    resolved = [name for name in names if shutil.which(name) is not None]
    missing = [name for name in names if name not in resolved]
    detail = (
        f"resolved: {', '.join(resolved) if resolved else 'none'}; "
        f"missing: {', '.join(missing) if missing else 'none'}"
    )
    return Probe("harness", "match" if resolved else "unavailable", detail)


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


def _age(value: str) -> str:
    observed = datetime.fromisoformat(value[:-1] + "+00:00")
    seconds = max(0, int((datetime.now(timezone.utc) - observed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _harness_events_probe(config) -> Probe:
    if config is None:
        return Probe(
            "harness-events", "unavailable",
            "configuration was not supplied to the live event delivery probe",
        )
    try:
        metadata = config.runtime_dir.lstat()
    except FileNotFoundError:
        return Probe(
            "harness-events", "unavailable",
            "Control runtime directory does not exist; live event delivery is unavailable",
        )
    except OSError as exc:
        return Probe(
            "harness-events", "unavailable",
            f"Control runtime directory cannot be inspected: {_safe_detail(exc)}",
        )
    if not stat.S_ISDIR(metadata.st_mode):
        return Probe(
            "harness-events", "unavailable",
            "Control runtime path is not a directory",
        )
    try:
        # Terminal edges and archive expire a run's event snapshot by design
        # (issue #54), so only live, non-terminal runs of unarchived tasks are
        # expected to have one.
        registered_runs = [
            (task["task_id"], run["run_id"], run["harness"])
            for task in TaskStore(config).list()
            if task["lifecycle"] in {"running", "creating"}
            for run in task["runs"]
            if run["state"] not in {"exited", "failed"}
        ]
    except StoreError as exc:
        return Probe(
            "harness-events", "unavailable",
            f"registered runs could not be read: {_safe_detail(exc)}",
        )
    runs = [
        (task_id, run_id)
        for task_id, run_id, harness in registered_runs
        if harness in _SEMANTIC_EVENT_HARNESSES
    ]
    skipped = len(registered_runs) - len(runs)
    if registered_runs and not runs:
        return Probe(
            "harness-events", "match",
            f"registered runs have no claimed semantic event seam; "
            f"skipped {skipped} liveness-only run(s)",
        )
    if not runs:
        return Probe(
            "harness-events", "match",
            "no registered Control runs require live event delivery",
        )

    received: list[str] = []
    missing: list[str] = []
    unreadable: list[str] = []
    for task_id, run_id in runs:
        try:
            snapshot = read_snapshot(config, run_id)
        except EventError:
            unreadable.append(run_id)
            continue
        if snapshot is None:
            missing.append(run_id)
        elif snapshot["task_id"] != task_id:
            unreadable.append(run_id)
        else:
            received.append(f"{run_id[:8]}={_age(snapshot['observed_at'])}")
    summary = f"readable {len(received)}/{len(runs)}"
    if received:
        summary += "; ages " + ", ".join(received[:12])
        if len(received) > 12:
            summary += f", +{len(received) - 12} more"
    if missing:
        summary += f"; missing {len(missing)}"
    if unreadable:
        summary += f"; unreadable or mismatched {len(unreadable)}"
    if skipped:
        summary += f"; skipped {skipped} liveness-only run(s)"
    summary = summary[:500]
    if unreadable:
        outcome = "unavailable"
    elif missing:
        outcome = "missing"
    else:
        outcome = "match"
    return Probe("harness-events", outcome, summary)


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


def _hooks_probe(config) -> Probe:
    if config is None:
        return Probe(
            "hooks", "unavailable",
            "configuration was not supplied to the hook installation probe",
        )
    claude_home, codex_home = _claimed_hook_homes(config)
    claude_path = claude_home / "settings.json"
    codex_path = codex_home / "config.toml"
    installed: list[str] = []
    for name, path in (("claude", claude_path), ("codex", codex_path)):
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            pass
        installed.append(name)
    if not installed:
        return Probe(
            "hooks", "match",
            "no installed Claude or Codex configuration requires Control hook inspection",
        )
    expected_claude = {
        "SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd",
    }
    expected_codex = {
        "SessionStart", "UserPromptSubmit", "PostToolUse", "PermissionRequest", "Stop",
    }
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
            codex_value = tomllib.loads(_read_install_config(codex_path))
            codex_hooks = codex_value.get("hooks", {})
            if not isinstance(codex_hooks, dict):
                raise ValueError("Codex hooks root is not a table")
            for event in sorted(expected_codex):
                groups = codex_hooks.get(event, [])
                found = any(
                    _control_hook_command(hook.get("command"), event)
                    for group in groups if isinstance(group, dict)
                    for hook in group.get("hooks", []) if isinstance(hook, dict)
                ) if isinstance(groups, list) else False
                if not found:
                    missing.append(f"codex:{event}")
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
        f"expected {' and '.join(labels)} Control hook command paths are installed and executable",
    )


def _jj_probe(config) -> Probe:
    """Prove jj's command semantics without contacting any repository.

    The jj contract requires probing the required commands and semantics rather
    than trusting a version string alone. Each leaf command's own `--help` exits
    0 outside a repository, so this stays read-only and repository-free.
    """
    executable = shutil.which("jj")
    if executable is None:
        return Probe("jj", "unavailable", "jj executable was not found on PATH")
    def run(args: list[str]) -> tuple[int, bytes, bytes]:
        return capture_bytes(
            [executable, *args], cwd=None, limit=64 * 1024,
            runner=None, error_type=JjError,
        )

    try:
        returncode, stdout, stderr = run(["--version"])
    except JjError as exc:
        return Probe("jj", "unavailable", f"jj --version failed: {_safe_detail(str(exc))}")
    if returncode != 0:
        return Probe(
            "jj", "unavailable",
            f"jj --version failed: {_safe_detail(stderr.decode('utf-8', errors='replace'))}",
        )
    version = stdout.decode("utf-8", errors="replace").strip().splitlines()[0] if stdout else ""
    missing: list[str] = []
    for command in _JJ_REQUIRED_COMMANDS:
        try:
            code, _out, _err = run([*command, "--help"])
        except JjError:
            missing.append(" ".join(command))
            continue
        if code != 0:
            missing.append(" ".join(command))
    if missing:
        return Probe(
            "jj", "mismatch",
            f"{_safe_detail(version)} is missing required commands: " + ", ".join(missing),
        )
    return Probe(
        "jj", "match",
        f"{_safe_detail(version)} exposes workspace add/forget, operation log, and git init/import",
    )


def _repository_probe(config) -> Probe:
    """Report whether the current directory can host a Control task.

    Doctor is a standalone health check, so there is no task to inspect. The
    useful question it can answer is the one an operator is about to ask:
    can `asha task start` run here? Being outside a repository is reported as
    `unavailable`, not a failure -- doctor is legitimately run anywhere.
    """
    if shutil.which("jj") is None:
        return Probe("repository", "unavailable", "jj is unavailable so no repository was inspected")
    try:
        start = Path.cwd().resolve()
    except OSError as exc:
        return Probe("repository", "unavailable", f"working directory is unreadable: {_safe_detail(str(exc))}")
    adapter = JjAdapter()
    try:
        root = adapter.discover_root(start)
    except (JjError, ValueError):
        try:
            git_root = discover_git_root(start)
        except LinkedGitWorktreeError as exc:
            return Probe(
                "repository", "mismatch",
                _safe_detail(f"{exc}; Control made no repository changes"),
            )
        except JjError:
            git_root = None
        if git_root is not None:
            intent_problem = _colocation_intent_problem(config, git_root)
            if intent_problem is not None:
                return intent_problem
            try:
                (git_root / ".jj").lstat()
            except FileNotFoundError:
                pass
            except OSError as exc:
                return Probe(
                    "repository", "mismatch",
                    f"existing .jj metadata could not be inspected: {_safe_detail(exc)}",
                )
            else:
                return Probe(
                    "repository", "mismatch",
                    _safe_detail(
                        f"{git_root} has existing .jj metadata that strict jj discovery "
                        "could not use; task start will refuse rather than overwrite it"
                    ),
                )
            initialized = (git_root / ".asha" / "config.json").is_file() and (
                git_root / "Memory" / "activeContext.md"
            ).is_file()
            outcome = "match" if initialized else "missing"
            memory = (
                "Memory v2 appears initialized"
                if initialized else
                "Memory v2 is not initialized, so task creation will still refuse"
            )
            return Probe(
                "repository", outcome,
                _safe_detail(
                    f"{git_root} is a plain Git repository; task start will auto-colocate "
                    "it with command-scoped no-auto-track preservation, retain the verified "
                    f"repository enablement, and then run strict jj preflight; {memory}"
                ),
            )
        return Probe(
            "repository", "unavailable",
            "the working directory is not inside a jj repository; "
            "run `asha task start --repo PATH` or change directory",
        )
    intent_problem = _colocation_intent_problem(config, root)
    if intent_problem is not None:
        return intent_problem
    try:
        facts = adapter.preflight(root)
        working_copy_parent = adapter.working_copy_parent(root)
        git_head = adapter.git_head_exact(facts.root)
    except (JjError, ValueError) as exc:
        return Probe("repository", "mismatch", f"jj repository is unusable: {_safe_detail(str(exc))}")
    remediation = colocated_sync_remediation(root, git_head, working_copy_parent)
    if remediation is not None:
        return Probe(
            "repository", "mismatch", remediation,
        )
    initialized = (root / ".asha" / "config.json").is_file() and (
        root / "Memory" / "activeContext.md"
    ).is_file()
    if not initialized:
        return Probe(
            "repository", "missing",
            _safe_detail(
                f"{root} is jj-managed with a Git backend but is not "
                "Asha Memory v2 initialized; task creation will refuse"
            ),
        )
    return Probe(
        "repository", "match",
        _safe_detail(
            f"{root} is jj-managed, Git-backed at {facts.git_root}, "
            "and Memory v2 initialized"
        ),
    )


def _default_context_probe(config) -> Probe:
    """Prove the resolved default's immutable Control context without mutation."""
    if config is None:
        return Probe(
            "default-context", "unavailable",
            "configuration was not supplied; no default immutable base was inspected",
        )
    if shutil.which("git") is None:
        return Probe(
            "default-context", "unavailable",
            "Git is unavailable; no default immutable base was inspected",
        )
    try:
        start = Path.cwd().resolve()
    except OSError as exc:
        return Probe(
            "default-context", "unavailable",
            f"working directory is unreadable: {_safe_detail(exc)}",
        )
    adapter = JjAdapter()
    existing_jj = True
    try:
        root = adapter.discover_root(start)
    except JjError:
        try:
            root = discover_git_root(start)
        except (JjError, LinkedGitWorktreeError):
            root = None
        existing_jj = False
    if root is None:
        return Probe(
            "default-context", "unavailable",
            "the working directory is not inside a supported Git-backed repository",
        )
    try:
        snapshot = read_published_snapshot(root)
        binding = inspect_pre_enable_binding(root)
        default = adapter.resolve_default_base(root)
        materialization = adapter.materialization_plan(
            binding.git_binding.target, default.commit_id, exact_root=root,
        )
        repository_identity, repo_key = derive_repository_identity(
            snapshot.project_id, root, binding.git_binding.target,
        )
        destination = config.workspace_root / repo_key / "doctor-default-context"
        marker = {
            "contract": "asha.control-task-context.v1",
            "task_id": "00000000-0000-4000-8000-000000000001",
            "repository": {"root": str(root), "identity": repository_identity},
            "jj": {
                "workspace_name": "asha-doctor-default-context-00000000",
                "workspace_path": str(destination), "change_id": "k" * 32,
                "working_commit_id": "f" * 64,
            },
        }
        context = build_context_plan(root, destination, marker, snapshot=snapshot)
        adapter.prove_context_compatibility(
            root, binding.git_binding.target, materialization,
            project_id=snapshot.project_id,
            planned_context_paths=tuple(context),
            private_directory_paths=DYNAMIC_PRIVATE_CONTEXT_DIRECTORIES,
        )
    except ContextCompatibilityError as exc:
        selected = f"{', '.join(default.references)} @ {default.commit_id}"
        if exc.evidence.missing_paths != (".asha/control-task.json",):
            return Probe(
                "default-context", "mismatch",
                _safe_detail(
                    f"resolved default only ({selected}) lacks immutable context "
                    f"coverage for {', '.join(exc.evidence.missing_paths)}; no "
                    "repository or task state was changed"
                ),
            )
        return Probe(
            "default-context", "mismatch",
            _safe_detail(
                f"resolved default only ({selected}) lacks the committed positive "
                "ignore for .asha/control-task.json; run /session:init, commit "
                "the rule, then retry. No repository or task state was changed"
            ),
        )
    except (JjError, ValueError, OSError) as exc:
        return Probe(
            "default-context", "mismatch",
            _safe_detail(
                f"resolved default immutable context is not ready: {exc}; "
                "no repository or task state was changed"
            ),
        )
    selected = f"{', '.join(default.references)} @ {default.commit_id}"
    return Probe(
        "default-context", "match",
        _safe_detail(
            f"resolved default only ({selected}) has immutable Control context "
            "compatibility; explicit future bases are not certified"
        ),
    )


def _colocation_intent_problem(config, root: Path) -> Probe | None:
    """Mirror task-start's read-only Control colocation authentication gate."""
    if config is None:
        return None
    store = ColocationIntentStore(config)
    try:
        assessment = store.classify(root)
    except JjError as exc:
        return Probe(
            "repository", "mismatch",
            _safe_detail(
                "Control colocation intent is stale or binding-mismatched; "
                f"task start will refuse. Inspect {store.path(root)}, `jj status`, "
                f"and Git status/refs before repair: {exc}"
            ),
        )
    if assessment.kind == "verified_root_hardening_candidate":
        probe_workspace = config.workspace_root / ".doctor-pre-enable" / store._key(root)
        try:
            validate_task_paths(config, root, probe_workspace)
        except StoreError as exc:
            return Probe(
                "repository", "mismatch",
                _safe_detail(
                    "verified colocation root hardening remains unsafe under the "
                    f"task-start path policy and is ineligible for automatic repair: {exc}"
                ),
            )
        return Probe(
            "repository", "mismatch",
            _safe_detail(
                "repairable verified colocation root hardening detected by a "
                "read-only check; task start will reauthenticate repository/Git/jj "
                "identity, stable semantics, Memory, base, and destination before "
                "rewriting only the stored root fact"
            ),
        )
    if assessment.kind == "verified_device_rebind_candidate":
        probe_workspace = config.workspace_root / ".doctor-pre-enable" / store._key(root)
        try:
            validate_task_paths(config, root, probe_workspace)
        except StoreError as exc:
            return Probe(
                "repository", "mismatch",
                _safe_detail(
                    "verified colocation device renumbering remains unsafe under "
                    "the task-start path policy and is ineligible for automatic "
                    f"repair: {exc}"
                ),
            )
        return Probe(
            "repository", "mismatch",
            _safe_detail(
                "repairable verified colocation filesystem device renumbering "
                "detected by a read-only check; task start will reauthenticate "
                "repository/Git/jj identity, stable semantics, Memory, base, and "
                "destination before refreshing only stored device observations"
            ),
        )
    if assessment.kind == "mismatch":
        return Probe(
            "repository", "mismatch",
            _safe_detail(
                "Control colocation intent is stale or binding-mismatched; task "
                f"start will refuse. Inspect {store.path(root)}, `jj status`, and "
                f"Git status/refs; the record remains unchanged: {assessment.detail}"
            ),
        )
    if assessment.kind == "intent":
        return Probe(
            "repository", "mismatch",
            _safe_detail(
                "ambiguous Control colocation intent is retained; task start "
                f"will refuse. Inspect {store.path(root)}, `jj status`, and Git "
                "status/refs before repair"
            ),
        )
    return None


def _transactions_probe(config) -> Probe:
    if config is None:
        return Probe(
            "transactions", "unavailable",
            "configuration was not supplied to the creation transaction probe",
        )
    try:
        task_store = TaskStore(config)
        tasks = task_store.list()
        journals = CreationJournalStore(config)
        interrupted: list[str] = []
        retained: list[str] = []
        for listed in tasks:
            task_id = listed["task_id"]
            with task_store.transaction_lock(task_id):
                task = task_store.read(task_id)
                try:
                    journal = journals.read(task_id)
                except JournalError:
                    continue
                guidance = retained_recovery_guidance(task, journal)
                if guidance is not None:
                    retained.append(guidance)
                elif task["lifecycle"] == "creating":
                    interrupted.append(task_id)
    except (StoreError, JournalError) as exc:
        return Probe(
            "transactions", "unavailable",
            f"creation transactions could not be inspected: {_safe_detail(exc)}",
        )
    if not interrupted and not retained:
        return Probe(
            "transactions", "match",
            "no interrupted creation transactions are registered",
        )
    commands = "; ".join(
        f"asha task recover {task_id}"
        for task_id in interrupted[:5]
    )
    details = [
        f"{len(interrupted)} interrupted creation transaction(s); run: {commands}"
    ] if interrupted else []
    details.extend(retained[:5])
    return Probe(
        "transactions", "mismatch",
        _safe_detail("; ".join(details)),
    )


def _prunable_probe(config) -> Probe:
    """Report reclaimable residue of archived tasks; never a failure by itself."""
    if config is None:
        return Probe(
            "prunable", "unavailable",
            "configuration was not supplied to the prunable probe",
        )
    try:
        summary = prunable_summary(
            config, tasks=TaskStore(config),
            tmux_for_socket=lambda socket: TmuxAdapter(
                socket=None if socket == "default" else socket,
            ),
        )
    except (StoreError, ValueError, OSError) as exc:
        return Probe(
            "prunable", "unavailable",
            f"archived task residue could not be inspected: {_safe_detail(exc)}",
        )
    if summary["tasks"] == 0:
        return Probe(
            "prunable", "match",
            "no archived task holds a tmux session or workspace directory",
        )
    return Probe(
        "prunable", "match",
        f"{summary['tasks']} archived task(s) hold {summary['sessions']} dead tmux "
        f"session(s) and {summary['workspaces']} workspace director(y/ies); "
        "reclaim with: asha task prune --all",
    )


DEFAULT_PROBES: Mapping[str, ProbeFunction] = {
    "python": _python_probe,
    "configuration": _configuration_probe,
    "tmux": _tmux_probe,
    "harness": _harness_probe,
    "gh": _gh_probe,
    "jj": _jj_probe,
    "repository": _repository_probe,
    "default-context": _default_context_probe,
    "transactions": _transactions_probe,
    "prunable": _prunable_probe,
    "harness-events": _harness_events_probe,
    "hooks": _hooks_probe,
    "tui": _tui_probe,
}


def run_doctor(config, probes: Mapping[str, ProbeFunction] | None = None) -> dict[str, Any]:
    selected = DEFAULT_PROBES if probes is None else probes
    results: list[Probe] = []
    for name, probe in selected.items():
        if (not isinstance(name, str) or
                re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name) is None):
            raise ValueError("invalid doctor probe name")
        result = probe(config)
        if not isinstance(result, Probe) or result.name != name:
            raise ValueError(f"doctor probe {name} returned an invalid result")
        results.append(Probe(result.name, result.outcome, result.detail))
    limitations = [result.detail for result in results if result.outcome != "match"]
    # GitHub source support and absence of a repository in the caller's current
    # directory are contextual; report them without failing the general check.
    blocking = [
        result for result in results
        if (result.outcome != "match" and result.name != "gh" and
            not (result.name in {"repository", "default-context"} and
                 result.outcome == "unavailable"))
    ]
    return {
        "contract": "asha.control-doctor.v1",
        "ok": not blocking,
        "probes": [asdict(result) for result in results],
        "limitations": limitations,
    }
