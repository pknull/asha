#!/usr/bin/env python3
"""Issue #24 workspace bootstrap, bounded discovery, and doctor core.

The module owns filesystem planning and deterministic Asha scaffolding.  It is
intentionally not wired to ``bin/asha`` here.  It never initializes Git,
changes a child checkout, stages, commits, pushes, or changes a branch.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import workspace_knowledge as wk  # noqa: E402
import workspace_manifest as wm  # noqa: E402


SCHEMA_VERSION = 1
OWNERSHIP_PATH = Path(".asha") / "workspace-init.json"
MANIFEST_PATH = Path(".asha") / "workspace.json"
IGNORE_BEGIN = "# >>> asha workspace private roots >>>"
IGNORE_END = "# <<< asha workspace private roots <<<"
IGNORE_ENTRIES = (
    "memory-local/",
    "Work/worktrees/",
    ".asha/cache/",
    ".asha/state/",
)
SKIP_DISCOVERY_DIRS = {".git", ".asha", "Work", "node_modules", ".venv", "venv"}


def _issue(code: str, message: str, *, path: Optional[str] = None) -> dict[str, Any]:
    item: dict[str, Any] = {"code": code, "message": message}
    if path is not None:
        item["path"] = path
    return item


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _sha(content: bytes) -> str:
    return wk._sha_bytes(content)


def _inert_label(value: Any, *, limit: int = 160) -> str:
    """Convert manifest-controlled display data to one inert printable line."""
    raw = str(value)
    safe = "".join(ch if ch.isascii() and (ch.isalnum() or ch in " ._/-") else "_" for ch in raw)
    safe = re.sub(r"\s+", " ", safe).strip() or "workspace"
    return safe.encode("ascii", "ignore")[:limit].decode("ascii", "ignore").rstrip() or "workspace"


def _normalize_rel(value: str, *, allow_dot: bool = False) -> Optional[str]:
    if not isinstance(value, str) or not value or "\\" in value:
        return None
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        return None
    normalized = pure.as_posix()
    if normalized == "." and not allow_dot:
        return None
    return normalized


def _explicit_root(root: Path | str) -> tuple[Optional[Path], list[dict[str, Any]]]:
    candidate = Path(root)
    try:
        resolved = candidate.resolve(strict=True)
        home = Path.home().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return None, [_issue("root_invalid", f"workspace root cannot be resolved: {exc}")]
    if not resolved.is_dir():
        return None, [_issue("root_invalid", "workspace root must be an existing directory", path=str(resolved))]
    if resolved == Path(resolved.anchor) or resolved == home:
        return None, [_issue("root_reserved", "filesystem root and HOME cannot be workspace roots", path=str(resolved))]
    return resolved, []


def _contained(root: Path, rel: str) -> Optional[Path]:
    normalized = _normalize_rel(rel, allow_dot=True)
    if normalized is None:
        return None
    try:
        candidate = (root if normalized == "." else root / normalized).resolve()
        candidate.relative_to(root.resolve())
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def _generated_target(root: Path, rel: str) -> Optional[Path]:
    """Lexical write/read target when every existing component is non-symlink."""
    normalized = _normalize_rel(rel, allow_dot=True)
    if normalized is None:
        return None
    lexical = root if normalized == "." else root / normalized
    current = root
    for part in (() if normalized == "." else PurePosixPath(normalized).parts):
        current = current / part
        if current.is_symlink():
            return None
    try:
        lexical.resolve().relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return lexical


def _git(args: list[str], cwd: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _is_git_root(path: Path) -> bool:
    top = _git(["rev-parse", "--show-toplevel"], path)
    if top is None:
        return False
    try:
        return Path(top).resolve() == path.resolve()
    except (OSError, RuntimeError, ValueError):
        return False


def _redact_remote(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    value = url.strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        try:
            parsed = urlsplit(value)
            host = parsed.hostname or ""
            if parsed.port:
                host += f":{parsed.port}"
            if parsed.username is not None or parsed.password is not None:
                host = "[REDACTED]@" + host
            safe_query = []
            for key, item in parse_qsl(parsed.query, keep_blank_values=True):
                if re.search(r"(?i)(token|secret|password|auth|key)", key):
                    item = "[REDACTED]"
                safe_query.append((key, item))
            return urlunsplit((parsed.scheme, host, parsed.path, urlencode(safe_query), ""))
        except (ValueError, UnicodeError):
            return "[REDACTED_INVALID_REMOTE]"
    # Preserve standard git@host:path SSH form (username is not a credential),
    # but redact user:password@host:path and token@host:path forms.
    if re.match(r"^git@[^:]+:.+", value):
        return value
    if "@" in value:
        return "[REDACTED]@" + value.split("@", 1)[1]
    return value


def _repo_state(path: Path, rel: str) -> dict[str, Any]:
    exists = path.is_dir()
    is_git = exists and _is_git_root(path)
    branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"], path) if is_git else None
    upstream = _git(["rev-parse", "--abbrev-ref", "@{upstream}"], path) if is_git else None
    divergence = {"ahead": None, "behind": None}
    if upstream:
        counts = _git(["rev-list", "--left-right", "--count", f"HEAD...{upstream}"], path)
        if counts:
            parts = counts.split()
            if len(parts) == 2 and all(part.isdigit() for part in parts):
                divergence = {"ahead": int(parts[0]), "behind": int(parts[1])}
    dirty_text = _git(["status", "--porcelain"], path) if is_git else None
    remote = _git(["config", "--get", "remote.origin.url"], path) if is_git else None
    remote_head = _git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], path) if is_git else None
    default_branch = remote_head.split("/", 1)[1] if remote_head and "/" in remote_head else branch
    return {
        "path": rel,
        "exists": exists,
        "is_git_worktree": is_git,
        "branch": branch,
        "default_branch": default_branch,
        "upstream": upstream,
        "ahead": divergence["ahead"],
        "behind": divergence["behind"],
        "dirty": bool(dirty_text) if dirty_text is not None else None,
        "remote_url": _redact_remote(remote),
    }


def discover_repositories(root: Path | str, *, max_depth: int = 3) -> dict[str, Any]:
    """Bounded, read-only proposal discovery.  Symlinks are never traversed."""
    resolved, errors = _explicit_root(root)
    report = {"operation": "discover", "ok": False, "root": None, "max_depth": max_depth,
              "proposals": [], "warnings": [], "errors": errors}
    if resolved is None:
        return report
    report["root"] = str(resolved)
    if max_depth < 1 or max_depth > 8:
        report["errors"] = [_issue("depth_invalid", "discovery depth must be between 1 and 8")]
        return report
    queue: deque[tuple[Path, int]] = deque([(resolved, 0)])
    while queue:
        directory, depth = queue.popleft()
        if depth >= max_depth:
            continue
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            report["warnings"].append(_issue("directory_unreadable", str(exc), path=str(directory)))
            continue
        for entry in entries:
            if entry.name in SKIP_DISCOVERY_DIRS:
                continue
            lexical = Path(entry.path)
            if entry.is_symlink():
                try:
                    target = lexical.resolve()
                    contained = target == resolved or resolved in target.parents
                except (OSError, RuntimeError, ValueError):
                    contained = False
                report["warnings"].append(_issue(
                    "symlink_skipped",
                    "repository discovery never traverses symlinks" + ("" if contained else " (target is outside workspace)"),
                    path=lexical.relative_to(resolved).as_posix(),
                ))
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if not is_dir:
                continue
            child_depth = depth + 1
            rel = lexical.relative_to(resolved).as_posix()
            if _is_git_root(lexical):
                report["proposals"].append(_repo_state(lexical, rel))
                continue
            if child_depth < max_depth:
                queue.append((lexical, child_depth))
    report["proposals"] = sorted(report["proposals"], key=lambda item: item["path"])
    report["ok"] = not report["errors"]
    return report


def _agents_template(name: str, manifest: dict[str, Any]) -> bytes:
    safe_name = _inert_label(name)
    repos = ", ".join(_inert_label(entry["path"]) for entry in manifest.get("repositories", [])) or "none declared"
    memory = manifest["memory"]
    operational = _inert_label(memory["operational_root"])
    shared = _inert_label(memory["shared_root"])
    personal = _inert_label(memory["personal_root"])
    text = f"""# {safe_name} workspace instructions

Read `.asha/workspace.json` before cross-repository work. It defines repository
ownership and memory planes. Declared repositories: {repos}.

- Operational handoff: `{operational}/activeContext.md`
- Canonical knowledge index: `{shared}/README.md`
- Private local memory: `{personal}/` (never commit)
- Do not stage child source during workspace-memory saves.
- Canonical promotion is explicit and follows manifest `promotion_mode`.
"""
    return text.encode("utf-8")


def _claude_template() -> bytes:
    return b"# Claude workspace adapter\n\nRead `AGENTS.md` and `.asha/workspace.json` before acting.\n"


def _copilot_template() -> bytes:
    return b"# Copilot workspace adapter\n\nRead `AGENTS.md` and `.asha/workspace.json` before acting.\n"


def _operational_templates(name: str, manifest: dict[str, Any]) -> dict[str, bytes]:
    operational = manifest["memory"]["operational_root"]
    personal = manifest["memory"]["personal_root"]
    return {
        f"{operational}/activeContext.md": (
            f"# {_inert_label(name)} workspace handoff\n\n## Current state\n\nNo cross-repository handoff recorded yet.\n"
        ).encode("utf-8"),
        f"{operational}/MEMORY.md": b"# Workspace memory catalogue\n\n",
        f"{personal}/.gitkeep": b"",
    }


def _ignore_entries(personal_root: str) -> tuple[str, ...]:
    return (f"{personal_root.rstrip('/')}/", *IGNORE_ENTRIES[1:])


def _merge_ignore(existing: bytes, entries: Iterable[str] = IGNORE_ENTRIES) -> bytes:
    try:
        text = existing.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f".gitignore is not UTF-8: {exc}") from exc
    managed_entries = tuple(entries)
    lines = text.splitlines()
    output: list[str] = []
    in_block = False
    for line in lines:
        if line == IGNORE_BEGIN:
            in_block = True
            continue
        if line == IGNORE_END:
            in_block = False
            continue
        if in_block or line in managed_entries:
            continue
        output.append(line)
    while output and output[-1] == "":
        output.pop()
    if output:
        output.append("")
    output.extend([IGNORE_BEGIN, *managed_entries, IGNORE_END, ""])
    return "\n".join(output).encode("utf-8")


def _manifest_payload(name: str, repositories: list[str], shared_git_root: str,
                      no_git: bool) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    data = {
        "version": 1,
        "workspace_name": name,
        "memory": {
            "operational_root": "Memory",
            "personal_root": "memory-local",
            "shared_root": "knowledge",
            "shared_git_root": shared_git_root,
            "promotion_mode": "pull-request",
        },
        "repositories": [
            {"path": rel, "docs": f"knowledge/repos/{rel}"} for rel in repositories
        ],
        "bootstrap": {"git_mode": "none" if no_git else "existing"},
    }
    manifest, errors = wm.validate_manifest(data)
    return manifest, [error._asdict() for error in errors]


def _load_metadata(root: Path) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    path = _generated_target(root, OWNERSHIP_PATH.as_posix())
    if path is None:
        return None, _issue("ownership_path_unsafe", "workspace init metadata path escapes or traverses a symlink", path=OWNERSHIP_PATH.as_posix())
    if not path.exists():
        return None, None
    data, why = wk._read_json(path)
    if why or data is None or data.get("version") != SCHEMA_VERSION \
            or not isinstance(data.get("owned"), dict) \
            or not isinstance(data.get("adopted"), dict):
        return None, _issue("ownership_invalid", f"workspace init metadata is invalid: {why or 'wrong schema'}", path=OWNERSHIP_PATH.as_posix())
    return data, None


def _knowledge_blueprint(root: Path, manifest: dict[str, Any]) -> tuple[list[str], dict[str, bytes]]:
    shared = (root / manifest["memory"]["shared_root"]).resolve()
    ctx = {"workspace_root": root, "manifest": manifest, "shared_root": shared,
           "shared_rel": manifest["memory"]["shared_root"]}
    directories, files, _ = wk._layout_spec(ctx, include_tickets=False)
    prefixed = {f"{manifest['memory']['shared_root']}/{rel}": content for rel, content in files.items()}
    return [f"{manifest['memory']['shared_root']}/{rel}" for rel in directories], prefixed


def _remove_created_dirs(created: Iterable[Path]) -> None:
    for directory in sorted(created, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def _confirm_ignore(root: Path, personal_root: str, no_git: bool) -> str:
    ignore_path = _generated_target(root, ".gitignore")
    if ignore_path is None:
        return "unsafe"
    if no_git:
        try:
            text = ignore_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return "unavailable"
        return "configured-no-git" if f"{personal_root}/" in text.splitlines() else "missing"
    try:
        probe = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "--no-index", "-q", f"{personal_root}/__probe__"],
            capture_output=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return "confirmed" if probe.returncode == 0 else "missing"


def initialize_workspace(*, root: Path | str, workspace_name: Optional[str] = None,
                         repositories: Optional[Iterable[str]] = None,
                         discover: bool = False, accept_discovered: bool = False,
                         discover_depth: int = 3, shared_git_root: Optional[str] = None,
                         no_git: bool = False, force: bool = False,
                         adopt: Optional[Iterable[str]] = None) -> dict[str, Any]:
    """Transactionally initialize one explicit workspace root."""
    resolved, root_errors = _explicit_root(root)
    report: dict[str, Any] = {
        "operation": "init", "ok": False, "root": str(resolved) if resolved else None,
        "changed": [], "collisions": [], "adopted": [], "warnings": [],
        "errors": root_errors, "requires_confirmation": False,
        "proposals": [], "plan": [], "ignore_protection": None,
    }
    if resolved is None:
        return report
    if discover:
        discovery = discover_repositories(resolved, max_depth=discover_depth)
        report["proposals"] = discovery["proposals"]
        report["warnings"].extend(discovery["warnings"])
        if not discovery["ok"]:
            report["errors"] = discovery["errors"]
            return report
        if not accept_discovered:
            report["requires_confirmation"] = True
            report["errors"] = [_issue("discovery_confirmation_required", "discovered repositories are proposals only; rerun with explicit acceptance")]
            return report
        discovered_paths = [item["path"] for item in discovery["proposals"]]
        repositories = list(repositories or []) + discovered_paths

    existing_manifest: Optional[dict[str, Any]] = None
    manifest_path = resolved / MANIFEST_PATH
    if _generated_target(resolved, MANIFEST_PATH.as_posix()) is None:
        report["errors"] = [_issue("path_escape", "workspace manifest path escapes or traverses a symlink", path=MANIFEST_PATH.as_posix())]
        return report
    if manifest_path.exists():
        try:
            existing_manifest, manifest_errors = wm.parse_manifest_text(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            existing_manifest, manifest_errors = None, [wm.ManifestError("unreadable", "", str(exc))]
        if manifest_errors or existing_manifest is None:
            report["errors"] = [_issue("manifest_invalid", "existing workspace manifest is invalid; bootstrap will not overwrite it without a separate repair", path=MANIFEST_PATH.as_posix())]
            return report

    if existing_manifest is not None:
        name = workspace_name or existing_manifest["workspace_name"]
        repo_values = list(repositories) if repositories is not None else [entry["path"] for entry in existing_manifest["repositories"]]
        if shared_git_root is None:
            shared_git_root = existing_manifest["memory"].get("shared_git_root", ".")
        existing_mode = existing_manifest.get("bootstrap", {}).get("git_mode")
        if existing_mode == "none" and not no_git:
            no_git = True
    else:
        name = workspace_name or resolved.name
        repo_values = list(repositories or [])
        shared_git_root = shared_git_root or "."
    normalized_repos: list[str] = []
    for value in repo_values:
        rel = _normalize_rel(str(value))
        if rel is None:
            report["errors"] = [_issue("repository_path_invalid", "repository paths must be workspace-relative without traversal", path=str(value))]
            return report
        normalized_repos.append(rel)
    normalized_repos = sorted(set(normalized_repos))
    sgr = _normalize_rel(shared_git_root, allow_dot=True)
    if sgr is None:
        report["errors"] = [_issue("shared_git_root_invalid", "shared_git_root must be a workspace-relative path")]
        return report
    if existing_manifest is not None:
        requested = copy.deepcopy(existing_manifest)
        if workspace_name is not None:
            requested["workspace_name"] = name
        if repositories is not None or discover:
            prior = {entry.get("path"): entry for entry in existing_manifest.get("repositories", [])}
            requested["repositories"] = [
                copy.deepcopy(prior[rel]) if rel in prior else
                {"path": rel, "docs": f"knowledge/repos/{rel}"}
                for rel in normalized_repos
            ]
        if shared_git_root is not None:
            requested.setdefault("memory", {})["shared_git_root"] = sgr
        manifest, raw_errors = wm.validate_manifest(requested)
        manifest_errors = [error._asdict() for error in raw_errors]
    else:
        manifest, manifest_errors = _manifest_payload(name, normalized_repos, sgr, no_git)
    if manifest_errors or manifest is None:
        report["errors"] = manifest_errors
        return report
    if existing_manifest is not None and existing_manifest != manifest and not force:
        report["errors"] = [_issue("manifest_collision", "existing valid manifest differs from requested bootstrap; use explicit force or match its configuration")]
        report["collisions"] = [MANIFEST_PATH.as_posix()]
        return report

    shared_git_path = _contained(resolved, sgr)
    if shared_git_path is None:
        report["errors"] = [_issue("path_escape", "shared_git_root resolves outside workspace", path=sgr)]
        return report
    if not no_git and not _is_git_root(shared_git_path):
        report["errors"] = [_issue("shared_git_root_not_git", "shared_git_root is not an existing Git worktree; use --no-git or initialize it separately", path=sgr)]
        return report

    metadata, metadata_error = _load_metadata(resolved)
    if metadata_error:
        report["errors"] = [metadata_error]
        return report
    metadata = metadata or {
        "version": SCHEMA_VERSION, "owner": "asha-workspace-init",
        "owned": {}, "adopted": {}, "mutable": [],
        "managed": {"gitignore_block": list(IGNORE_ENTRIES)},
    }
    owned: dict[str, Any] = dict(metadata.get("owned", {}))
    adopted_map: dict[str, Any] = dict(metadata.get("adopted", {}))
    adopt_set = {_normalize_rel(str(item)) for item in (adopt or [])}
    if None in adopt_set:
        report["errors"] = [_issue("adopt_path_invalid", "adopt paths must be safe workspace-relative paths")]
        return report

    desired: dict[str, bytes] = {
        MANIFEST_PATH.as_posix(): _json_bytes(manifest),
        "AGENTS.md": _agents_template(name, manifest),
        "CLAUDE.md": _claude_template(),
        ".github/copilot-instructions.md": _copilot_template(),
        **_operational_templates(name, manifest),
    }
    knowledge_dirs, knowledge_files = _knowledge_blueprint(resolved, manifest)
    desired.update(knowledge_files)
    mutable = set(_operational_templates(name, manifest))
    mutable.add(MANIFEST_PATH.as_posix())
    instruction_paths = {"AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md"}

    # Knowledge ownership is separate so promotion can update its index without
    # making workspace-init metadata stale.
    knowledge_root_rel = manifest["memory"]["shared_root"]
    knowledge_owner_rel = f"{knowledge_root_rel}/{wk.OWNERSHIP_FILE}"
    knowledge_ownership: dict[str, Any] = {"version": 1, "owner": "asha-workspace-knowledge", "files": {}}
    existing_ko_path = _generated_target(resolved, knowledge_owner_rel)
    if existing_ko_path is None:
        report["errors"] = [_issue("path_escape", "knowledge ownership path escapes or traverses a symlink", path=knowledge_owner_rel)]
        return report
    if existing_ko_path.exists():
        loaded, why = wk._read_json(existing_ko_path)
        if why or loaded is None or not isinstance(loaded.get("files"), dict):
            report["errors"] = [_issue("knowledge_ownership_invalid", f"knowledge ownership metadata is invalid: {why or 'wrong schema'}", path=knowledge_owner_rel)]
            return report
        knowledge_ownership = loaded
    knowledge_owned = dict(knowledge_ownership.get("files", {}))

    write_set: dict[Path, bytes] = {}
    for rel, content in sorted(desired.items()):
        target = _generated_target(resolved, rel)
        if target is None:
            report["errors"].append(_issue("path_escape", "generated path resolves outside workspace", path=rel))
            continue
        if target.exists() and (not target.is_file() or target.is_symlink()):
            report["collisions"].append(rel)
            continue
        is_knowledge = rel.startswith(knowledge_root_rel + "/")
        knowledge_subrel = rel[len(knowledge_root_rel) + 1:] if is_knowledge else None
        if target.exists():
            current = target.read_bytes()
            if rel == MANIFEST_PATH.as_posix():
                if current == content or existing_manifest == manifest:
                    report["plan"].append({"path": rel, "action": "preserve"})
                    continue
                if force:
                    write_set[target] = content
                    report["plan"].append({"path": rel, "action": "overwrite",
                                           "current_sha256": _sha(current), "desired_sha256": _sha(content)})
                    continue
                report["collisions"].append(rel)
                report["plan"].append({"path": rel, "action": "requires-force",
                                       "current_sha256": _sha(current), "desired_sha256": _sha(content)})
                continue
            if rel in mutable:
                report["plan"].append({"path": rel, "action": "preserve-mutable"})
                continue
            if is_knowledge and knowledge_subrel in knowledge_owned:
                if _sha(current) == knowledge_owned[knowledge_subrel]:
                    report["plan"].append({"path": rel, "action": "preserve"})
                    continue
                if force:
                    write_set[target] = content
                    knowledge_owned[knowledge_subrel] = _sha(content)
                    report["plan"].append({"path": rel, "action": "overwrite",
                                           "current_sha256": _sha(current), "desired_sha256": _sha(content)})
                else:
                    report["collisions"].append(rel)
                    report["plan"].append({"path": rel, "action": "requires-force",
                                           "current_sha256": _sha(current), "desired_sha256": _sha(content)})
                continue
            record = owned.get(rel)
            if isinstance(record, dict):
                if _sha(current) == record.get("sha256"):
                    if force and current != content:
                        write_set[target] = content
                        record = {"sha256": _sha(content), "repairable": True, "kind": "instruction"}
                        owned[rel] = record
                        report["plan"].append({"path": rel, "action": "overwrite",
                                               "current_sha256": _sha(current), "desired_sha256": _sha(content)})
                    else:
                        report["plan"].append({"path": rel, "action": "preserve"})
                    continue
                if force:
                    write_set[target] = content
                    owned[rel] = {"sha256": _sha(content), "repairable": True, "kind": "instruction"}
                    report["plan"].append({"path": rel, "action": "overwrite",
                                           "current_sha256": _sha(current), "desired_sha256": _sha(content)})
                else:
                    report["collisions"].append(rel)
                    report["plan"].append({"path": rel, "action": "requires-force",
                                           "current_sha256": _sha(current), "desired_sha256": _sha(content)})
                continue
            if rel in adopted_map:
                if force:
                    write_set[target] = content
                    adopted_map.pop(rel, None)
                    if is_knowledge:
                        knowledge_owned[knowledge_subrel] = _sha(content)
                    else:
                        owned[rel] = {"sha256": _sha(content), "repairable": rel in instruction_paths, "kind": "instruction"}
                    report["plan"].append({"path": rel, "action": "overwrite-adopted",
                                           "current_sha256": _sha(current), "desired_sha256": _sha(content)})
                else:
                    report["plan"].append({"path": rel, "action": "preserve-adopted"})
                continue
            if rel in adopt_set:
                adopted_map[rel] = {"sha256": _sha(current), "repairable": False}
                report["adopted"].append(rel)
                report["plan"].append({"path": rel, "action": "adopt",
                                       "current_sha256": _sha(current)})
            elif force:
                write_set[target] = content
                if is_knowledge:
                    knowledge_owned[knowledge_subrel] = _sha(content)
                elif rel in instruction_paths:
                    owned[rel] = {"sha256": _sha(content), "repairable": True, "kind": "instruction"}
                report["plan"].append({"path": rel, "action": "overwrite",
                                       "current_sha256": _sha(current), "desired_sha256": _sha(content)})
            else:
                report["collisions"].append(rel)
                report["plan"].append({"path": rel, "action": "requires-adopt-or-force",
                                       "current_sha256": _sha(current), "desired_sha256": _sha(content)})
        else:
            write_set[target] = content
            report["plan"].append({"path": rel, "action": "create", "desired_sha256": _sha(content)})
            if is_knowledge:
                knowledge_owned[knowledge_subrel] = _sha(content)
            elif rel in instruction_paths:
                owned[rel] = {"sha256": _sha(content), "repairable": True, "kind": "instruction"}

    if report["errors"] or report["collisions"]:
        report["collisions"] = sorted(set(report["collisions"]))
        report["errors"] = report["errors"] or [_issue("generated_file_collision", "user-owned or drifted generated files require --adopt or --force")]
        return report

    knowledge_ownership["files"] = dict(sorted(knowledge_owned.items()))
    knowledge_owner_bytes = _json_bytes(knowledge_ownership)
    if not existing_ko_path.exists() or existing_ko_path.read_bytes() != knowledge_owner_bytes:
        write_set[existing_ko_path] = knowledge_owner_bytes

    gitignore = _generated_target(resolved, ".gitignore")
    if gitignore is None:
        report["errors"] = [_issue("path_escape", ".gitignore path escapes or traverses a symlink", path=".gitignore")]
        return report
    ignore_entries = _ignore_entries(manifest["memory"]["personal_root"])
    try:
        ignore_bytes = _merge_ignore(gitignore.read_bytes() if gitignore.exists() else b"", ignore_entries)
    except (OSError, ValueError) as exc:
        report["errors"] = [_issue("gitignore_unusable", str(exc), path=".gitignore")]
        return report
    if not gitignore.exists() or gitignore.read_bytes() != ignore_bytes:
        write_set[gitignore] = ignore_bytes

    metadata.update({
        "version": SCHEMA_VERSION,
        "owner": "asha-workspace-init",
        "git_mode": "none" if no_git else "existing",
        "shared_git_root": sgr,
        "owned": dict(sorted(owned.items())),
        "adopted": dict(sorted(adopted_map.items())),
        "mutable": sorted(mutable),
        "managed": {"gitignore_block": list(ignore_entries), "knowledge_root": knowledge_root_rel},
    })
    metadata_bytes = _json_bytes(metadata)
    metadata_path = _generated_target(resolved, OWNERSHIP_PATH.as_posix())
    assert metadata_path is not None
    if not metadata_path.exists() or metadata_path.read_bytes() != metadata_bytes:
        write_set[metadata_path] = metadata_bytes

    directories = {
        resolved / ".asha", resolved / ".github", resolved / "Memory",
        resolved / "memory-local", resolved / knowledge_root_rel,
        *(resolved / rel for rel in knowledge_dirs),
    }
    for directory in directories:
        try:
            rel_dir = directory.relative_to(resolved).as_posix()
        except ValueError:
            rel_dir = ".."
        if _generated_target(resolved, rel_dir) is None:
            report["errors"] = [_issue("path_escape", "generated directory escapes or traverses a symlink", path=rel_dir)]
            return report
        try:
            directory.resolve().relative_to(resolved)
        except (OSError, RuntimeError, ValueError):
            report["errors"] = [_issue("path_escape", "generated directory resolves outside workspace", path=str(directory))]
            return report
        if directory.exists() and not directory.is_dir():
            report["errors"] = [_issue("directory_collision", "generated directory path is occupied", path=str(directory.relative_to(resolved)))]
            return report

    created_dirs: list[Path] = []
    try:
        for directory in sorted(directories, key=lambda item: len(item.parts)):
            if not directory.exists():
                directory.mkdir()
                created_dirs.append(directory)
    except OSError as exc:
        _remove_created_dirs(created_dirs)
        report["errors"] = [_issue("directory_create_failed", str(exc), path=str(directory))]
        return report
    ok, why, originals = wk._atomic_write_set(write_set)
    if not ok:
        _remove_created_dirs(created_dirs)
        report["errors"] = [_issue("bootstrap_write_failed", f"atomic bootstrap write failed: {why}")]
        return report
    ignore_state = _confirm_ignore(resolved, manifest["memory"]["personal_root"], no_git)
    if ignore_state not in {"confirmed", "configured-no-git"}:
        wk._restore_write_set(originals)
        _remove_created_dirs(created_dirs)
        report["errors"] = [_issue("private_ignore_unconfirmed", "private-root ignore probe failed; bootstrap rolled back")]
        return report
    report["ignore_protection"] = ignore_state
    report["changed"] = sorted(path.relative_to(resolved).as_posix() for path in write_set)
    for rel in normalized_repos:
        state = _repo_state(resolved / rel, rel)
        if not state["exists"]:
            report["warnings"].append(_issue("repo_missing", "declared child repository is unavailable", path=rel))
        elif not state["is_git_worktree"]:
            report["warnings"].append(_issue("repo_not_git", "declared child path is not a Git worktree", path=rel))
    if no_git:
        report["warnings"].append(_issue("review_unavailable", "--no-git workspace cannot commit workspace memory or execute reviewed promotion"))
    report["ok"] = True
    return report


def _read_manifest_exact(root: Path) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    path = _generated_target(root, MANIFEST_PATH.as_posix())
    if path is None:
        return None, [_issue("manifest_invalid", "workspace manifest path escapes or traverses a symlink", path=MANIFEST_PATH.as_posix())]
    try:
        manifest, errors = wm.parse_manifest_text(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return None, [_issue("manifest_invalid", f"workspace manifest is unavailable: {exc}", path=MANIFEST_PATH.as_posix())]
    if errors or manifest is None:
        return None, [_issue("manifest_invalid", "; ".join(error.message for error in errors), path=MANIFEST_PATH.as_posix())]
    return manifest, []


def _doctor_once(root: Path) -> dict[str, Any]:
    manifest, manifest_errors = _read_manifest_exact(root)
    report: dict[str, Any] = {
        "operation": "doctor", "ok": False, "root": str(root), "errors": manifest_errors,
        "warnings": [], "repositories": [], "generated": [], "fixed": False,
        "fixed_paths": [], "git_mode": None, "shared_git_root": None,
        "shared_git_dirty": None, "promotion_mode": None,
        "promotion_available": False, "private_ignore": "unknown",
    }
    if manifest is None:
        return report
    metadata, metadata_error = _load_metadata(root)
    if metadata_error or metadata is None:
        report["errors"].append(metadata_error or _issue("ownership_missing", "workspace init ownership metadata is missing", path=OWNERSHIP_PATH.as_posix()))
        return report
    git_mode = metadata.get("git_mode", manifest.get("bootstrap", {}).get("git_mode", "existing"))
    report["git_mode"] = git_mode
    report["promotion_mode"] = manifest["memory"]["promotion_mode"]
    sgr_rel = manifest["memory"]["shared_git_root"]
    sgr = _contained(root, sgr_rel)
    report["shared_git_root"] = str(sgr) if sgr else str(root / sgr_rel)
    if sgr is None:
        report["errors"].append(_issue("shared_git_root_escape", "shared_git_root resolves outside workspace", path=sgr_rel))
    elif git_mode == "none":
        report["warnings"].append(_issue("review_unavailable", "no-git workspace has no reviewed-promotion or workspace-commit execution"))
    elif not _is_git_root(sgr):
        report["errors"].append(_issue("shared_git_root_missing", "configured shared_git_root is not a Git worktree", path=sgr_rel))
    else:
        dirty = _git(["status", "--porcelain"], sgr)
        report["shared_git_dirty"] = bool(dirty) if dirty is not None else None
        report["promotion_available"] = True

    for key in ("operational_root", "personal_root", "shared_root"):
        rel = manifest["memory"][key]
        target = _generated_target(root, rel)
        if target is None:
            report["errors"].append(_issue("root_escape", f"{key} resolves outside workspace", path=rel))
        elif not target.is_dir():
            code = "shared_root_missing" if key == "shared_root" else "memory_root_missing"
            report["errors"].append(_issue(code, f"{key} is missing", path=rel))

    for entry in manifest.get("repositories", []):
        rel = entry["path"]
        target = _contained(root, rel)
        state = _repo_state(target or root / rel, rel)
        report["repositories"].append(state)
        if target is None:
            report["errors"].append(_issue("repo_escape", "declared repository resolves outside workspace", path=rel))
        elif not state["exists"]:
            report["warnings"].append(_issue("repo_missing", "declared repository is unavailable", path=rel))
        elif not state["is_git_worktree"]:
            report["warnings"].append(_issue("repo_not_git", "declared repository is not a Git worktree", path=rel))

    for rel, record in sorted(metadata.get("owned", {}).items()):
        target = _generated_target(root, rel)
        state = "ok"
        if target is None or not target.is_file() or target.is_symlink():
            state = "missing"
        else:
            try:
                if _sha(target.read_bytes()) != record.get("sha256"):
                    state = "drifted"
            except OSError:
                state = "unreadable"
        report["generated"].append({"path": rel, "ownership": "asha", "state": state,
                                    "repairable": bool(record.get("repairable"))})
        if state != "ok":
            report["errors"].append(_issue("generated_drift", f"Asha-owned generated file is {state}", path=rel))
    for rel, record in sorted(metadata.get("adopted", {}).items()):
        target = _generated_target(root, rel)
        state = "missing" if target is None or not target.is_file() else (
            "ok" if _sha(target.read_bytes()) == record.get("sha256") else "drifted"
        )
        report["generated"].append({"path": rel, "ownership": "adopted", "state": state, "repairable": False})
        if state != "ok":
            report["warnings"].append(_issue("adopted_drift", f"adopted user file is {state}; doctor will not overwrite it", path=rel))

    report["private_ignore"] = _confirm_ignore(root, manifest["memory"]["personal_root"], git_mode == "none")
    if report["private_ignore"] not in {"confirmed", "configured-no-git"}:
        report["errors"].append(_issue("private_ignore_missing", "private memory root is not protected by ignore rules"))

    shared = _contained(root, manifest["memory"]["shared_root"])
    if shared and shared.is_dir():
        knowledge = wk.lint_knowledge(root)
        for item in knowledge.get("blocking", []):
            report["errors"].append(_issue("knowledge_lint_blocking", f"{item['code']}: {item['message']}", path=item.get("path")))
        for item in knowledge.get("advisory", []):
            report["warnings"].append(_issue("knowledge_lint_advisory", f"{item['code']}: {item['message']}", path=item.get("path")))
    report["ok"] = not report["errors"]
    return report


def _doctor_fix(root: Path, report: dict[str, Any]) -> tuple[bool, list[str], Optional[dict[str, Any]]]:
    manifest, errors = _read_manifest_exact(root)
    metadata, metadata_error = _load_metadata(root)
    if errors or manifest is None or metadata_error or metadata is None:
        return False, [], _issue("fix_unavailable", "doctor fix requires a valid manifest and ownership metadata")
    desired = {
        "AGENTS.md": _agents_template(manifest["workspace_name"], manifest),
        "CLAUDE.md": _claude_template(),
        ".github/copilot-instructions.md": _copilot_template(),
    }
    write_set: dict[Path, bytes] = {}
    fixed: list[str] = []
    for rel, record in metadata.get("owned", {}).items():
        if not record.get("repairable") or rel not in desired:
            continue
        target = _generated_target(root, rel)
        if target is None:
            return False, [], _issue("fix_path_unsafe", "owned repair path is no longer contained", path=rel)
        content = desired[rel]
        current = target.read_bytes() if target.is_file() else None
        if current != content:
            write_set[target] = content
            record["sha256"] = _sha(content)
            fixed.append(rel)

    # Canonical layout owns its own hash registry. Repair only files named by
    # that registry. If the whole managed shared root vanished, the workspace
    # init registry is sufficient proof that recreating the default empty
    # scaffold is deterministic; no user document is overwritten.
    knowledge_root_rel = manifest["memory"]["shared_root"]
    knowledge_root = _contained(root, knowledge_root_rel)
    recreate_full_knowledge = knowledge_root is not None and not knowledge_root.is_dir()
    knowledge_dirs, knowledge_files = _knowledge_blueprint(root, manifest)
    knowledge_owner_path = _generated_target(root, f"{knowledge_root_rel}/{wk.OWNERSHIP_FILE}")
    if knowledge_owner_path is None:
        return False, [], _issue("fix_path_unsafe", "knowledge ownership path is no longer safe")
    knowledge_owned: dict[str, str] = {}
    if knowledge_root is not None and knowledge_owner_path.exists():
        knowledge_meta, why = wk._read_json(knowledge_owner_path)
        if why or knowledge_meta is None or not isinstance(knowledge_meta.get("files"), dict):
            return False, [], _issue("knowledge_ownership_invalid", f"cannot repair knowledge layout: {why or 'wrong schema'}")
        knowledge_owned = dict(knowledge_meta["files"])
    elif recreate_full_knowledge and metadata.get("managed", {}).get("knowledge_root") == knowledge_root_rel:
        for prefixed, content in knowledge_files.items():
            subrel = prefixed[len(knowledge_root_rel) + 1:]
            knowledge_owned[subrel] = _sha(content)
    for prefixed, content in knowledge_files.items():
        subrel = prefixed[len(knowledge_root_rel) + 1:]
        if subrel not in knowledge_owned:
            continue
        target = _generated_target(root, prefixed)
        if target is None:
            return False, [], _issue("fix_path_unsafe", "knowledge repair path is no longer contained", path=prefixed)
        current = target.read_bytes() if target.is_file() else None
        if current is None or _sha(current) != knowledge_owned[subrel]:
            write_set[target] = content
            knowledge_owned[subrel] = _sha(content)
            fixed.append(prefixed)
    if knowledge_owned:
        knowledge_meta_bytes = _json_bytes({
            "version": 1, "owner": "asha-workspace-knowledge",
            "files": dict(sorted(knowledge_owned.items())),
        })
        if not knowledge_owner_path.exists() or knowledge_owner_path.read_bytes() != knowledge_meta_bytes:
            write_set[knowledge_owner_path] = knowledge_meta_bytes
            fixed.append(f"{knowledge_root_rel}/{wk.OWNERSHIP_FILE}")
    gitignore = _generated_target(root, ".gitignore")
    if gitignore is None:
        return False, [], _issue("fix_path_unsafe", ".gitignore path is no longer safe")
    ignore_entries = _ignore_entries(manifest["memory"]["personal_root"])
    try:
        ignore = _merge_ignore(gitignore.read_bytes() if gitignore.exists() else b"", ignore_entries)
    except (OSError, ValueError) as exc:
        return False, [], _issue("gitignore_unusable", str(exc), path=".gitignore")
    if not gitignore.exists() or gitignore.read_bytes() != ignore:
        write_set[gitignore] = ignore
        fixed.append(".gitignore")
    if write_set:
        metadata_path = root / OWNERSHIP_PATH
        write_set[metadata_path] = _json_bytes(metadata)
        fixed.append(OWNERSHIP_PATH.as_posix())
    directories = {path.parent for path in write_set}
    if recreate_full_knowledge:
        directories.update(root / rel for rel in knowledge_dirs)
    created_dirs: list[Path] = []
    try:
        for directory in sorted(directories, key=lambda item: len(item.parts)):
            if not directory.exists():
                directory.mkdir(parents=True)
                created_dirs.append(directory)
    except OSError as exc:
        _remove_created_dirs(created_dirs)
        return False, [], _issue("fix_directory_failed", str(exc), path=str(directory))
    ok, why, originals = wk._atomic_write_set(write_set)
    if not ok:
        _remove_created_dirs(created_dirs)
        return False, [], _issue("fix_write_failed", f"atomic doctor fix failed: {why}")
    ignore_state = _confirm_ignore(root, manifest["memory"]["personal_root"], metadata.get("git_mode") == "none")
    if ignore_state not in {"confirmed", "configured-no-git"}:
        wk._restore_write_set(originals)
        _remove_created_dirs(created_dirs)
        return False, [], _issue("private_ignore_unconfirmed", "ignore repair could not be verified; changes rolled back")
    return bool(write_set), sorted(set(fixed)), None


def doctor_workspace(root: Path | str, *, fix: bool = False) -> dict[str, Any]:
    resolved, errors = _explicit_root(root)
    if resolved is None:
        return {"operation": "doctor", "ok": False, "root": None, "errors": errors,
                "warnings": [], "repositories": [], "generated": [], "fixed": False,
                "fixed_paths": []}
    report = _doctor_once(resolved)
    if not fix or (report["errors"] and report["errors"][0]["code"] == "manifest_invalid"):
        return report
    changed, paths, fix_error = _doctor_fix(resolved, report)
    if fix_error:
        report["errors"].append(fix_error)
        return report
    final = _doctor_once(resolved)
    final["fixed"] = changed
    final["fixed_paths"] = paths
    return final


def _render(report: dict[str, Any]) -> str:
    op = report.get("operation", "workspace")
    lines = [f"workspace {op}: {'PASS' if report.get('ok') else 'FAIL'}"]
    if report.get("root"):
        lines.append(f"  root: {report['root']}")
    if report.get("requires_confirmation"):
        lines.append("  discovery proposals require explicit acceptance")
    for item in report.get("proposals", []):
        lines.append(f"  proposal {item['path']}: {item.get('default_branch') or '?'} {item.get('remote_url') or '(no remote)'}")
    for item in report.get("repositories", []):
        state = item.get("branch") if item.get("is_git_worktree") else ("MISSING" if not item.get("exists") else "NOT GIT")
        lines.append(f"  repo {item['path']}: {state}")
    for item in report.get("errors", []):
        lines.append(f"  ERROR {item['code']}: {item['message']}")
    for item in report.get("warnings", []):
        lines.append(f"  WARN {item['code']}: {item['message']}")
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="workspace_init.py")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--root", default=".")
    init.add_argument("--name")
    init.add_argument("--repo", action="append")
    init.add_argument("--discover", action="store_true")
    init.add_argument("--accept-discovered", action="store_true")
    init.add_argument("--discover-depth", type=int, default=3)
    init.add_argument("--shared-git-root")
    init.add_argument("--no-git", action="store_true")
    init.add_argument("--force", action="store_true")
    init.add_argument("--adopt", action="append")
    init.add_argument("--json", action="store_true")
    discover = commands.add_parser("discover")
    discover.add_argument("--root", default=".")
    discover.add_argument("--max-depth", type=int, default=3)
    discover.add_argument("--json", action="store_true")
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--root", default=".")
    doctor.add_argument("--fix", action="store_true")
    doctor.add_argument("--json", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args((argv or sys.argv)[1:])
    if args.command == "init":
        report = initialize_workspace(
            root=args.root, workspace_name=args.name, repositories=args.repo,
            discover=args.discover, accept_discovered=args.accept_discovered,
            discover_depth=args.discover_depth, shared_git_root=args.shared_git_root,
            no_git=args.no_git, force=args.force, adopt=args.adopt,
        )
    elif args.command == "discover":
        report = discover_repositories(args.root, max_depth=args.max_depth)
    else:
        report = doctor_workspace(args.root, fix=args.fix)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        sys.stdout.write(_render(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
