#!/usr/bin/env python3
"""Safe coordinated worktrees for repositories declared by an Asha workspace."""
from __future__ import annotations
import argparse, hashlib, json, os, re, subprocess, sys
from pathlib import Path
from typing import Any, Optional

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import workspace_manifest  # noqa: E402

CONTEXT = "workspace-context.json"
INITIATIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class WorktreeError(Exception):
    def __init__(self, code: str, message: str, details: Optional[list[dict[str, Any]]] = None):
        super().__init__(message)
        self.code, self.details = code, details or []


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except FileNotFoundError as exc:
        raise WorktreeError("git_unavailable", "git executable is unavailable") from exc


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(repo), *args])


def out(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout.strip()


def err(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip()


def contained(path: Path, root: Path, proper: bool = False) -> Optional[Path]:
    try:
        path, root = path.resolve(), root.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if path == root:
        return None if proper else path
    return path if root in path.parents else None


def safe_line(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty) or len(value) > 1024:
        raise WorktreeError("unsafe_instruction_value", f"{field} must be a bounded string")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value) or not value.isprintable():
        raise WorktreeError("unsafe_instruction_value", f"{field} must be a printable single line")
    return value


def safe_ref(value: Any, field: str) -> str:
    value = safe_line(value, field)
    parts = value.split("/")
    if (not REF_RE.fullmatch(value) or ".." in value or value.endswith(".lock")
            or any(not part or part.startswith(".") or part.endswith(".") or part.endswith(".lock") for part in parts)):
        raise WorktreeError("unsafe_instruction_value", f"{field} must use the restricted Git ref grammar")
    return value


def reject_symlink_components(path: Path, root: Path, code: str) -> None:
    """Reject any existing symlink from root down to path, without following it."""
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise WorktreeError(code, f"path is outside workspace: {path}") from exc
    current = root.absolute()
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise WorktreeError(code, f"symlinked workspace path is forbidden: {current}")


def load_manifest(root: Path) -> dict[str, Any]:
    path = root / ".asha" / "workspace.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise WorktreeError("workspace_manifest_missing", f"workspace manifest is missing: {path}") from exc
    except (OSError, UnicodeError) as exc:
        raise WorktreeError("workspace_manifest_unreadable", f"cannot read {path}: {exc}") from exc
    manifest, errors = workspace_manifest.parse_manifest_text(text)
    if manifest is None or errors:
        raise WorktreeError("workspace_manifest_invalid", f"workspace manifest is invalid: {path}", [e._asdict() for e in errors])
    return manifest


def find_root(value: Optional[str]) -> Path:
    if value:
        root = Path(value).expanduser().resolve()
        if root.is_dir():
            return root
        raise WorktreeError("workspace_root_invalid", f"workspace root is not a directory: {root}")
    here = Path.cwd().resolve()
    home = Path(os.environ.get("HOME", str(Path.home()))).resolve()
    for root in (here, *here.parents):
        if root == home or root == root.parent:
            break
        if (root / ".asha" / "workspace.json").is_file():
            return root
    raise WorktreeError("workspace_manifest_missing", "no .asha/workspace.json found above cwd")


def repo_name(entry: dict[str, Any]) -> str:
    name = str(entry.get("name") or Path(entry["path"]).name)
    safe_line(name, "repositories[].name")
    if not INITIATIVE_RE.fullmatch(name):
        raise WorktreeError("unsafe_instruction_value", f"repository name is not a safe identifier: {name!r}")
    return name


def declared(manifest: dict[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    result = {}
    for raw in manifest.get("repositories", []):
        name = repo_name(raw)
        if name in result:
            raise WorktreeError("ambiguous_repository", f"duplicate repository name: {name}")
        reject_symlink_components(root / raw["path"], root, "repository_symlink")
        source = contained(root / raw["path"], root, True)
        if source is None:
            raise WorktreeError("repository_outside_workspace", f"declared repository escapes workspace: {raw['path']}")
        entry = dict(raw)
        entry.update(name=name, source_path=source)
        result[name] = entry
    return result


def assignments(values: list[str], option: str) -> dict[str, str]:
    result = {}
    for value in values:
        if "=" not in value:
            raise WorktreeError("invalid_assignment", f"{option} requires REPO=VALUE: {value}")
        name, assigned = value.split("=", 1)
        if not name or not assigned or name in result:
            raise WorktreeError("invalid_assignment", f"invalid or duplicate {option}: {value}")
        safe_line(name, f"{option} repository")
        safe_line(assigned, f"{option} value")
        result[name] = assigned
    return result


def container_relative(manifest: dict[str, Any], explicit: Optional[str]) -> str:
    config = manifest.get("worktrees")
    value = explicit or (config.get("container_root") if isinstance(config, dict) else None) or "Work/worktrees"
    if not isinstance(value, str) or not value or Path(value).is_absolute() or ".." in Path(value).parts:
        raise WorktreeError("container_root_invalid", "container root must be workspace-relative without '..'")
    safe_line(value, "worktrees.container_root")
    return value


def shared_git_root(manifest: dict[str, Any], root: Path) -> Path:
    rel = (manifest.get("memory") or {}).get("shared_git_root", ".")
    shared = contained(root / rel, root)
    if shared is None:
        raise WorktreeError("shared_git_root_invalid", f"shared Git root escapes workspace: {rel}")
    result = git(shared, "rev-parse", "--show-toplevel")
    if result.returncode or Path(out(result)).resolve() != shared:
        raise WorktreeError("shared_git_root_unavailable", f"shared Git root is unavailable or mismatched: {shared}")
    return shared


def check_ignored(shared: Path, container_root: Path) -> None:
    try:
        rel = container_root.relative_to(shared)
    except ValueError as exc:
        raise WorktreeError("container_outside_shared_git_root", f"container must be inside {shared}") from exc
    result = git(shared, "check-ignore", "-q", "--", str(rel / ".asha-probe"))
    if result.returncode:
        raise WorktreeError("container_not_ignored", f"worktree container is not Git-ignored; add '/{rel}/' to {shared / '.gitignore'}")


def available_repo(path: Path, name: str) -> None:
    result = git(path, "rev-parse", "--show-toplevel")
    if result.returncode:
        raise WorktreeError("repository_unavailable", f"repository unavailable: {name} ({path})")
    if Path(out(result)).resolve() != path:
        raise WorktreeError("repository_root_mismatch", f"repository {name} resolves to another Git root: {out(result)}")


def default_base(repo: Path) -> Optional[str]:
    result = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    return out(result) if not result.returncode and out(result) else None


def choose_base(entry: dict[str, Any], manifest: dict[str, Any], bases: dict[str, str]) -> str:
    name = entry["name"]
    if name in bases:
        return safe_ref(bases[name], f"base for {name}")
    for key in ("base", "default_branch"):
        if isinstance(entry.get(key), str) and entry[key]:
            return safe_ref(entry[key], f"{key} for {name}")
    config = manifest.get("worktrees")
    configured = config.get("bases") if isinstance(config, dict) else None
    if isinstance(configured, dict) and isinstance(configured.get(name), str) and configured[name]:
        return safe_ref(configured[name], f"configured base for {name}")
    detected = default_base(entry["source_path"])
    if detected:
        return safe_ref(detected, f"origin default base for {name}")
    raise WorktreeError("base_missing", f"no deterministic base for {name}; pass --base {name}=REF, configure default_branch, or configure origin/HEAD")


def resolve_commit(repo: Path, ref: str, name: str) -> str:
    result = git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
    value = out(result)
    if result.returncode or not re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        raise WorktreeError("base_invalid", f"base for {name} is missing, ambiguous, or not a commit: {ref}")
    return value.lower()


def choose_branch(entry: dict[str, Any], initiative: str, branches: dict[str, str]) -> str:
    name = entry["name"]
    value = branches.get(name) or entry.get("branch") or f"asha/{initiative}"
    branch = safe_ref(str(value).replace("{initiative}", initiative), f"branch for {name}")
    if run(["git", "check-ref-format", "--branch", branch]).returncode:
        raise WorktreeError("branch_invalid", f"invalid branch for {name}: {branch}")
    return branch


def branch_conflict(repo: Path, branch: str, name: str) -> None:
    result = git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    if not result.returncode:
        listing = git(repo, "worktree", "list", "--porcelain")
        checked = f"branch refs/heads/{branch}" in listing.stdout.splitlines()
        raise WorktreeError("branch_conflict", f"branch already exists{' and is checked out' if checked else ''} for {name}: {branch}")
    if result.returncode != 1:
        raise WorktreeError("branch_check_failed", f"cannot inspect branch for {name}: {branch}")


def dirty(repo: Path) -> bool:
    result = git(repo, "status", "--porcelain=v1", "--untracked-files=normal")
    if result.returncode:
        raise WorktreeError("status_failed", f"cannot inspect repository: {repo}")
    return bool(out(result))


def verification(entry: dict[str, Any], manifest: dict[str, Any]) -> Optional[str]:
    for key in ("verification", "verification_command", "test_command"):
        if isinstance(entry.get(key), str) and entry[key]:
            return safe_line(entry[key], f"verification command for {entry['name']}")
    config = manifest.get("worktrees")
    values = config.get("verification") if isinstance(config, dict) else None
    value = values.get(entry["name"]) if isinstance(values, dict) else None
    return safe_line(value, f"verification command for {entry['name']}") if isinstance(value, str) and value else None


def preflight(root: Path, manifest: dict[str, Any], initiative: str, names: list[str], bases: dict[str, str], branches: dict[str, str], container_rel: str) -> tuple[Path, list[dict[str, Any]]]:
    if not INITIATIVE_RE.fullmatch(initiative):
        raise WorktreeError("initiative_invalid", "invalid initiative identifier")
    if not names:
        raise WorktreeError("repository_required", "at least one --repo is required")
    if len(names) != len(set(names)):
        raise WorktreeError("repository_duplicate", "--repo values must be unique")
    repos = declared(manifest, root)
    unknown = sorted(set(names) - set(repos))
    if unknown:
        raise WorktreeError("repository_undeclared", "undeclared repositories: " + ", ".join(unknown))
    unselected = sorted((set(bases) | set(branches)) - set(names))
    if unselected:
        raise WorktreeError("assignment_repository_unselected", "assignment names unselected repository: " + ", ".join(unselected))
    base_container = contained(root / container_rel, root, True)
    if base_container is None:
        raise WorktreeError("container_root_invalid", "container escapes workspace")
    reject_symlink_components(root / container_rel, root, "container_symlink")
    container = base_container / initiative
    for entry in repos.values():
        source = entry["source_path"]
        if container == source or source in container.parents or container in source.parents:
            raise WorktreeError("container_overlaps_repository", f"container overlaps repository: {entry['name']}")
    if container.exists() or container.is_symlink():
        raise WorktreeError("initiative_exists", f"initiative exists: {container}")
    memory = manifest.get("memory") or {}
    for key, default in (("shared_root", "knowledge"), ("personal_root", "memory-local")):
        if contained(root / memory.get(key, default), root, True) is None:
            raise WorktreeError("memory_root_outside_workspace", f"{key} resolves outside the workspace")
    check_ignored(shared_git_root(manifest, root), base_container)
    plans = []
    for name in names:
        entry = repos[name]
        source = entry["source_path"]
        available_repo(source, name)
        base_ref = choose_base(entry, manifest, bases)
        base_commit = resolve_commit(source, base_ref, name)
        branch = choose_branch(entry, initiative, branches)
        branch_conflict(source, branch, name)
        target = container / name
        if target.exists() or target.is_symlink():
            raise WorktreeError("worktree_path_exists", f"worktree path exists: {target}")
        plans.append({"name": name, "source_path": source, "worktree_path": target, "branch": branch,
                      "base_ref": base_ref, "base_commit": base_commit, "primary_dirty": dirty(source),
                      "docs": entry.get("docs"), "verification_command": verification(entry, manifest)})
    return container, plans


def rollback(created: list[dict[str, Any]], container: Path) -> list[dict[str, str]]:
    failures = []
    for plan in reversed(created):
        path = plan["worktree_path"]
        if path.exists():
            if str(path.resolve()) not in registered(plan["source_path"]):
                failures.append({"repository": plan["name"], "reason": "rollback preserved an unregistered destination"})
                continue
            if dirty(path):
                failures.append({"repository": plan["name"], "reason": "rollback refused dirty worktree"})
                continue
            result = git(plan["source_path"], "worktree", "remove", "--", str(path))
            if result.returncode:
                failures.append({"repository": plan["name"], "reason": err(result)})
                continue
        # A failed git worktree add may create the branch before failing the
        # checkout. Delete only the branch this operation named, only when it
        # still points exactly at the planned base, and never with force.
        branch_head = git(plan["source_path"], "rev-parse", "--verify", f"refs/heads/{plan['branch']}^{{commit}}")
        if not branch_head.returncode and out(branch_head).lower() == plan["base_commit"]:
            deletion = git(plan["source_path"], "branch", "--delete", "--", plan["branch"])
            if deletion.returncode:
                failures.append({"repository": plan["name"], "reason": err(deletion)})
    try:
        container.rmdir()
    except OSError:
        pass
    return failures


def atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.asha-tmp-{os.getpid()}")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def owned_context_bytes(context: dict[str, Any], agents_hash: Optional[str] = None) -> bytes:
    ownership = dict(context.get("ownership") or {})
    if agents_hash is not None:
        ownership["agents_sha256"] = agents_hash
    comparable = dict(context)
    comparable.pop("ownership", None)
    canonical = json.dumps(comparable, sort_keys=True, separators=(",", ":")).encode()
    ownership["context_payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    context["ownership"] = ownership
    return (json.dumps(context, indent=2, sort_keys=True) + "\n").encode()


def persist_context(container: Path, context: dict[str, Any]) -> None:
    try:
        atomic_write(container / CONTEXT, owned_context_bytes(context))
    except OSError as exc:
        raise WorktreeError("context_persist_failed", f"cannot persist removal journal: {exc}") from exc


def cleanup_failed_metadata(container: Path, generated: dict[Path, tuple[str, str]]) -> list[dict[str, str]]:
    failures = []
    for path, (kind, expected) in reversed(list(generated.items())):
        try:
            if kind == "symlink":
                if path.is_symlink() and os.readlink(path) == expected:
                    path.unlink()
                elif path.exists() or path.is_symlink():
                    failures.append({"path": str(path), "reason": "generated symlink changed; preserved"})
            elif path.is_file() and not path.is_symlink():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest == expected:
                    path.unlink()
                else:
                    failures.append({"path": str(path), "reason": "generated file changed; preserved"})
            elif path.exists() or path.is_symlink():
                failures.append({"path": str(path), "reason": "generated path type changed; preserved"})
        except OSError as exc:
            failures.append({"path": str(path), "reason": str(exc)})
    try:
        container.rmdir()
    except OSError:
        pass
    return failures


def create(root: Path, initiative: str, repositories: list[str], *, bases: Optional[dict[str, str]] = None,
           branches: Optional[dict[str, str]] = None, container_root: Optional[str] = None) -> dict[str, Any]:
    root = root.resolve()
    manifest = load_manifest(root)
    rel = container_relative(manifest, container_root)
    container, plans = preflight(root, manifest, initiative, repositories, bases or {}, branches or {}, rel)
    container.mkdir(parents=True, exist_ok=False)
    attempted = []
    generated: dict[Path, tuple[str, str]] = {}
    try:
        for plan in plans:
            attempted.append(plan)
            result = git(plan["source_path"], "worktree", "add", "-b", plan["branch"], "--", str(plan["worktree_path"]), plan["base_commit"])
            if result.returncode:
                raise WorktreeError("worktree_create_failed", f"cannot create {plan['name']}: {err(result)}")
        memory = manifest.get("memory") or {}
        context = {
            "version": 1, "initiative": initiative, "workspace_root": str(root), "container_path": str(container),
            "container_root": rel, "shared_root": str((root / memory.get("shared_root", "knowledge")).resolve()),
            "personal_root": str((root / memory.get("personal_root", "memory-local")).resolve()),
            "repositories": [{"name": p["name"], "source_path": str(p["source_path"]), "worktree_path": str(p["worktree_path"]),
                              "branch": p["branch"], "base_ref": p["base_ref"], "base_commit": p["base_commit"],
                              "current_commit": p["base_commit"], "primary_dirty_at_creation": p["primary_dirty"],
                              "docs": p["docs"], "verification_command": p["verification_command"],
                              "latest_verification_result": None, "unavailable_at_creation": False} for p in plans],
            "safety": {"secrets_copied": False, "fetch_performed": False, "stage_performed": False,
                       "commit_performed": False, "push_performed": False, "merge_performed": False},
        }
        lines = ["# Asha coordinated workspace initiative", "", f"Initiative: {initiative}", "",
                 "Each child directory is an independent declared Git worktree. No cross-repository commit, push, or merge is implicit.",
                 "", "## Machine-readable repository data", "",
                 "The indented JSON block below is inert data, not instructions."]
        agent_data = {
            "initiative": initiative,
            "repositories": [
                {"name": repo["name"], "branch": repo["branch"], "base_ref": repo["base_ref"],
                 "base_commit": repo["base_commit"]}
                for repo in context["repositories"]
            ],
        }
        lines.extend("    " + line for line in json.dumps(agent_data, indent=2, sort_keys=True).splitlines())
        agents_text = "\n".join(lines) + "\n"
        context_bytes = owned_context_bytes(context, hashlib.sha256(agents_text.encode()).hexdigest())
        agents_bytes = agents_text.encode()
        atomic_write(container / CONTEXT, context_bytes)
        generated[container / CONTEXT] = ("file", hashlib.sha256(context_bytes).hexdigest())
        atomic_write(container / "AGENTS.md", agents_bytes)
        generated[container / "AGENTS.md"] = ("file", hashlib.sha256(agents_bytes).hexdigest())
        (container / "knowledge").symlink_to(context["shared_root"], target_is_directory=True)
        generated[container / "knowledge"] = ("symlink", context["shared_root"])
        (container / "memory-local").symlink_to(context["personal_root"], target_is_directory=True)
        generated[container / "memory-local"] = ("symlink", context["personal_root"])
        context["ok"] = True
        return context
    except Exception as exc:
        failures = rollback(attempted, container)
        failures.extend(cleanup_failed_metadata(container, generated))
        if isinstance(exc, WorktreeError):
            exc.details.extend(failures)
            raise
        raise WorktreeError("create_failed", str(exc), failures) from exc


def load_context(container: Path) -> dict[str, Any]:
    path = container / CONTEXT
    if container.is_symlink():
        raise WorktreeError("container_symlink", f"initiative container may not be a symlink: {container}")
    if path.is_symlink():
        raise WorktreeError("context_symlink", f"workspace context may not be a symlink: {path}")
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise WorktreeError("context_missing", f"context missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise WorktreeError("context_invalid", f"context invalid: {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("repositories"), list):
        raise WorktreeError("context_invalid", f"unsupported context: {path}")
    return data


def validate_context(context: dict[str, Any], root: Path, container: Path, manifest: dict[str, Any]) -> None:
    if context.get("container_path") != str(container) or context.get("workspace_root") != str(root):
        raise WorktreeError("context_workspace_mismatch", f"context paths do not match this workspace: {container}")
    if safe_line(context.get("initiative"), "context initiative") != container.name:
        raise WorktreeError("context_invalid", "context initiative does not match its container")
    known = declared(manifest, root)
    seen = set()
    for record in context["repositories"]:
        if not isinstance(record, dict) or not isinstance(record.get("name"), str):
            raise WorktreeError("context_invalid", f"invalid repository record: {container}")
        name = record["name"]
        if name in seen or name not in known:
            raise WorktreeError("context_repository_invalid", f"context names undeclared or duplicate repository: {name}")
        seen.add(name)
        if record.get("source_path") != str(known[name]["source_path"]):
            raise WorktreeError("context_repository_invalid", f"context source path changed for {name}")
        expected = container / name
        if record.get("worktree_path") != str(expected):
            raise WorktreeError("context_worktree_invalid", f"context worktree path changed for {name}")
        if expected.is_symlink():
            raise WorktreeError("context_worktree_symlink", f"worktree path may not be a symlink: {expected}")
        for field in ("branch", "base_ref"):
            safe_ref(record.get(field), f"context {field} for {name}")
        if run(["git", "check-ref-format", "--branch", record["branch"]]).returncode:
            raise WorktreeError("context_invalid", f"context branch is invalid for {name}")
        if not isinstance(record.get("base_commit"), str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", record["base_commit"]):
            raise WorktreeError("context_invalid", f"context base commit is invalid for {name}")
    progress = context.get("removal_progress")
    if progress is not None:
        if not isinstance(progress, dict) or progress.get("state") != "in-progress":
            raise WorktreeError("context_invalid", "removal_progress has an invalid state")
        removed = progress.get("removed_repositories")
        if (not isinstance(removed, list) or len(removed) != len(set(removed))
                or any(not isinstance(name, str) or name not in seen for name in removed)):
            raise WorktreeError("context_invalid", "removal_progress contains invalid repository names")
        active = progress.get("removing_repository")
        if active is not None and (not isinstance(active, str) or active not in seen or active in removed):
            raise WorktreeError("context_invalid", "removal_progress has an invalid active repository")


def registered(repo: Path) -> dict[str, str]:
    result = git(repo, "worktree", "list", "--porcelain")
    found, path, current_branch = {}, None, ""
    if result.returncode:
        return found
    for line in result.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            if path:
                found[str(Path(path).resolve())] = current_branch
            path, current_branch = line[9:], ""
        elif line.startswith("branch "):
            current_branch = line[7:].removeprefix("refs/heads/")
        elif not line and path:
            found[str(Path(path).resolve())] = current_branch
            path, current_branch = None, ""
    return found


def head(repo: Path) -> Optional[str]:
    result = git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    return out(result).lower() if not result.returncode else None


def branch(repo: Path) -> Optional[str]:
    result = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out(result) if not result.returncode else None


def upstream(repo: Path) -> tuple[Optional[str], Optional[dict[str, int]]]:
    result = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if result.returncode:
        return None, None
    name = out(result)
    counts = git(repo, "rev-list", "--left-right", "--count", f"{name}...HEAD")
    if counts.returncode:
        return name, None
    try:
        behind, ahead = map(int, out(counts).split())
        return name, {"ahead": ahead, "behind": behind}
    except ValueError:
        return name, None


def merge_state(source: Path, current: str, base_ref: str, original_base: str) -> tuple[bool, str, Optional[str]]:
    if current == original_base:
        return True, "no-changes", original_base
    try:
        live_base = resolve_commit(source, base_ref, "status")
    except WorktreeError:
        return False, "base-unavailable", None
    result = git(source, "merge-base", "--is-ancestor", current, live_base)
    return not result.returncode, "git-ancestry" if not result.returncode else "unmerged", live_base


def repo_status(record: dict[str, Any]) -> dict[str, Any]:
    source, worktree = Path(record["source_path"]), Path(record["worktree_path"])
    row = dict(record)
    row.update(source_available=False, worktree_available=False, registered=False, dirty=None, current_commit=None,
               current_branch=None, upstream=None, upstream_relation=None, merged=False, merge_evidence="unavailable", cleanup_blockers=[])
    if not source.is_dir() or git(source, "rev-parse", "--show-toplevel").returncode:
        row["cleanup_blockers"].append("source-repository-unavailable")
        return row
    row["source_available"] = True
    regs = registered(source)
    row["registered"] = worktree.is_dir() and str(worktree.resolve()) in regs
    if not worktree.is_dir():
        row["cleanup_blockers"].append("worktree-unavailable")
        return row
    row["worktree_available"] = True
    if not row["registered"]:
        row["cleanup_blockers"].append("worktree-not-registered")
    row["current_commit"], row["current_branch"] = head(worktree), branch(worktree)
    if row["current_branch"] != record["branch"]:
        row["cleanup_blockers"].append("branch-mismatch-or-detached")
    row["dirty"] = dirty(worktree)
    if row["dirty"]:
        row["cleanup_blockers"].append("dirty-worktree")
    row["upstream"], row["upstream_relation"] = upstream(worktree)
    if row["current_commit"]:
        merged, evidence_kind, live_base = merge_state(source, row["current_commit"], record["base_ref"], record["base_commit"])
        row.update(merged=merged, merge_evidence=evidence_kind, current_base_commit=live_base)
        if not merged:
            row["cleanup_blockers"].append("unmerged-branch")
    return row


def containers(root: Path, manifest: dict[str, Any], initiative: Optional[str], explicit: Optional[str]) -> list[Path]:
    relative = container_relative(manifest, explicit)
    reject_symlink_components(root / relative, root, "container_symlink")
    base = contained(root / relative, root, True)
    if base is None:
        raise WorktreeError("container_root_invalid", "container escapes workspace")
    if initiative:
        if not INITIATIVE_RE.fullmatch(initiative):
            raise WorktreeError("initiative_invalid", "invalid initiative")
        return [base / initiative]
    return sorted(p for p in base.iterdir() if p.is_dir() and (p / CONTEXT).is_file()) if base.is_dir() else []


def status(root: Path, initiative: Optional[str] = None, *, container_root: Optional[str] = None) -> dict[str, Any]:
    root, manifest = root.resolve(), load_manifest(root.resolve())
    result = []
    for container in containers(root, manifest, initiative, container_root):
        if not container.is_dir():
            if initiative:
                raise WorktreeError("initiative_missing", f"initiative missing: {container}")
            continue
        context = load_context(container)
        validate_context(context, root, container, manifest)
        rows = [repo_status(r) for r in context["repositories"]]
        removed = set((context.get("removal_progress") or {}).get("removed_repositories", []))
        for row in rows:
            if row["name"] in removed:
                row.update(
                    removal_state="removed", cleanup_blockers=[], dirty=False,
                    registered=False, worktree_available=False, merged=True,
                    merge_evidence="recorded-removal-progress",
                )
            else:
                row["removal_state"] = "pending"
        result.append({"initiative": context["initiative"], "container_path": str(container), "repositories": rows,
                       "unavailable_repositories": [r["name"] for r in rows
                                                    if r["removal_state"] != "removed"
                                                    and (not r["source_available"] or not r["worktree_available"])],
                       "cleanup_blockers": sorted({b for r in rows for b in r["cleanup_blockers"]})})
    return {"contract": "asha.workspace-worktree-status.v1", "ok": True, "workspace_root": str(root), "initiatives": result}


def evidence(path: Optional[Path]) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise WorktreeError("review_evidence_invalid", f"review evidence invalid: {exc}") from exc
    repos = data.get("repositories") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        raise WorktreeError("review_evidence_invalid", "review evidence requires repositories object")
    return {str(k): v for k, v in repos.items() if isinstance(v, dict)}


def squash_valid(item: dict[str, Any], row: dict[str, Any]) -> bool:
    return (item.get("reviewed") is True and item.get("merged") is True and item.get("merge_method") == "squash"
            and item.get("branch") == row["branch"] and item.get("source_head") == row["current_commit"])


def cleanup_owned(container: Path, context: dict[str, Any]) -> list[str]:
    blockers = []
    for name, target in {"knowledge": context.get("shared_root"), "memory-local": context.get("personal_root")}.items():
        path = container / name
        if not path.exists() and not path.is_symlink():
            continue
        if not path.is_symlink() or os.readlink(path) != target:
            blockers.append(f"owned-link-changed:{name}")
        else:
            path.unlink()
    ownership = context.get("ownership") if isinstance(context.get("ownership"), dict) else {}
    agents = container / "AGENTS.md"
    try:
        agents_hash = hashlib.sha256(agents.read_bytes()).hexdigest()
    except OSError:
        agents_hash = ""
    if agents.is_file() and not agents.is_symlink() and agents_hash == ownership.get("agents_sha256"):
        agents.unlink()
    elif agents.exists() or agents.is_symlink():
        blockers.append("owned-file-changed:AGENTS.md")
    context_path = container / CONTEXT
    comparable = dict(context)
    comparable.pop("ownership", None)
    payload_hash = hashlib.sha256(json.dumps(comparable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if context_path.is_file() and not context_path.is_symlink() and payload_hash == ownership.get("context_payload_sha256"):
        context_path.unlink()
    elif context_path.exists() or context_path.is_symlink():
        blockers.append(f"owned-file-changed:{CONTEXT}")
    try:
        container.rmdir()
    except OSError:
        pass
    return blockers


def reconcile_removal_journal(container: Path, context: dict[str, Any]) -> None:
    """Resolve a crash/interruption between Git removal and journal update."""
    progress = context.get("removal_progress")
    if not isinstance(progress, dict) or not progress.get("removing_repository"):
        return
    active = progress["removing_repository"]
    record = next(item for item in context["repositories"] if item["name"] == active)
    source, worktree = Path(record["source_path"]), Path(record["worktree_path"])
    if not source.is_dir():
        raise WorktreeError(
            "removal_progress_uncertain",
            f"cannot reconcile removal of {active}: source repository is unavailable",
        )
    is_registered = str(worktree.resolve()) in registered(source)
    if not worktree.exists() and not is_registered:
        if active not in progress["removed_repositories"]:
            progress["removed_repositories"].append(active)
    elif not worktree.is_dir() or not is_registered:
        raise WorktreeError(
            "removal_progress_uncertain",
            f"cannot safely reconcile removal of {active}: path and Git registry disagree",
        )
    # If both path and registration remain, the prior removal did not complete;
    # clear the intent and retry normally. Otherwise record the derived success.
    progress["removing_repository"] = None
    persist_context(container, context)


def remove(root: Path, initiative: str, *, container_root: Optional[str] = None, delete_branches: bool = False,
           review_evidence: Optional[Path] = None) -> dict[str, Any]:
    root, manifest = root.resolve(), load_manifest(root.resolve())
    container = containers(root, manifest, initiative, container_root)[0]
    if not container.is_dir():
        raise WorktreeError("initiative_missing", f"initiative missing: {container}")
    context = load_context(container)
    validate_context(context, root, container, manifest)
    reconcile_removal_journal(container, context)
    snapshot = status(root, initiative, container_root=container_root)["initiatives"][0]
    reviewed = evidence(review_evidence)
    blockers = []
    for row in snapshot["repositories"]:
        current = list(row["cleanup_blockers"])
        if "unmerged-branch" in current and squash_valid(reviewed.get(row["name"], {}), row):
            current.remove("unmerged-branch")
            row.update(merged=True, merge_evidence="reviewed-squash-evidence")
        if current:
            blockers.append({"repository": row["name"], "path": row["worktree_path"], "blockers": current})
    if blockers:
        raise WorktreeError("cleanup_refused", "cleanup requires available, registered, clean, recorded-branch, merged worktrees", blockers)
    progress = context.get("removal_progress")
    if progress is None:
        progress = {"state": "in-progress", "removed_repositories": [], "removing_repository": None}
        context["removal_progress"] = progress
        persist_context(container, context)
    removed = list(progress["removed_repositories"])
    rows = snapshot["repositories"]
    for index, row in enumerate(rows):
        if row["name"] in removed:
            continue
        progress["removing_repository"] = row["name"]
        persist_context(container, context)
        result = git(Path(row["source_path"]), "worktree", "remove", "--", row["worktree_path"])
        if result.returncode:
            # Keep the journal truthful even when Git returns nonzero after
            # completing enough work to remove the registration and path.
            worktree = Path(row["worktree_path"])
            still_registered = str(worktree.resolve()) in registered(Path(row["source_path"]))
            if not worktree.exists() and not still_registered and row["name"] not in removed:
                removed.append(row["name"])
                progress["removed_repositories"] = list(removed)
            progress["removing_repository"] = None
            persist_context(container, context)
            raise WorktreeError("worktree_remove_failed", f"removal stopped without force after {len(removed)} worktrees: {err(result)}",
                                [{"ok": False, "removed": removed,
                                  "remaining": [item["name"] for item in rows if item["name"] not in removed],
                                  "recovery": ["Inspect the removed and remaining lists plus git worktree list in every source repository.",
                                               "Resolve the reported Git removal failure for each remaining registered worktree.",
                                               "Preserve the context container, then retry the same remove command; recorded worktrees are skipped.",
                                               "Do not delete directories manually."]}])
        removed.append(row["name"])
        progress["removed_repositories"] = list(removed)
        progress["removing_repository"] = None
        persist_context(container, context)
    deleted, deletion_failures = [], []
    if delete_branches:
        for row in snapshot["repositories"]:
            result = git(Path(row["source_path"]), "branch", "--delete", "--", row["branch"])
            if result.returncode:
                deletion_failures.append({"repository": row["name"], "branch": row["branch"], "reason": err(result)})
            else:
                deleted.append(row["name"])
    owned_blockers = cleanup_owned(container, context)
    if deletion_failures:
        raise WorktreeError(
            "branch_delete_partial",
            "worktrees were removed but one or more normal branch deletions failed; no force was used",
            [{"ok": False, "deleted_branches": deleted, "failures": deletion_failures,
              "recovery": ["The worktrees are already removed.", "Inspect each retained branch and merge evidence.",
                           "Delete only with normal git branch --delete after resolving ancestry."]}],
        )
    return {"contract": "asha.workspace-worktree-remove.v1", "ok": True, "initiative": initiative, "container_path": str(container),
            "removed_repositories": removed, "deleted_branches": deleted, "branch_deletion_failures": deletion_failures,
            "container_preserved": container.exists(), "container_cleanup_blockers": owned_blockers, "force_used": False}


def human_create(data: dict[str, Any]) -> str:
    lines = [f"Created {data['initiative']}: {data['container_path']}"]
    for r in data["repositories"]:
        lines.append(f"- {r['name']}: {r['branch']} from {r['base_ref']} at {r['base_commit'][:12]}{' (primary dirty)' if r['primary_dirty_at_creation'] else ''}")
    return "\n".join(lines)


def human_status(data: dict[str, Any]) -> str:
    if not data["initiatives"]:
        return "No coordinated workspace initiatives."
    lines = []
    for initiative in data["initiatives"]:
        lines.append(f"{initiative['initiative']}: {initiative['container_path']}")
        for r in initiative["repositories"]:
            state = "dirty" if r["dirty"] else "clean" if r["dirty"] is False else "unavailable"
            blocks = ",".join(r["cleanup_blockers"]) or "none"
            lines.append(f"- {r['name']}: {r['current_branch'] or '-'} {state} current={(r['current_commit'] or '-')[:12]} base={r['base_commit'][:12]} blockers={blocks}")
            lines.append(f"  source={r['source_path']} worktree={r['worktree_path']}")
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    subs = p.add_subparsers(dest="command", required=True)
    c = subs.add_parser("create")
    c.add_argument("--workspace-root")
    c.add_argument("--json", action="store_true")
    c.add_argument("initiative")
    c.add_argument("--repo", action="append", default=[])
    c.add_argument("--base", action="append", default=[])
    c.add_argument("--branch", action="append", default=[])
    c.add_argument("--container-root")
    s = subs.add_parser("status")
    s.add_argument("--workspace-root")
    s.add_argument("--json", action="store_true")
    s.add_argument("initiative", nargs="?")
    s.add_argument("--container-root")
    r = subs.add_parser("remove")
    r.add_argument("--workspace-root")
    r.add_argument("--json", action="store_true")
    r.add_argument("initiative")
    r.add_argument("--container-root")
    r.add_argument("--delete-branches", action="store_true")
    r.add_argument("--review-evidence")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        root = find_root(args.workspace_root)
        if args.command == "create":
            data = create(root, args.initiative, args.repo, bases=assignments(args.base, "--base"),
                          branches=assignments(args.branch, "--branch"), container_root=args.container_root)
            human = human_create(data)
        elif args.command == "status":
            data = status(root, args.initiative, container_root=args.container_root)
            human = human_status(data)
        else:
            data = remove(root, args.initiative, container_root=args.container_root, delete_branches=args.delete_branches,
                          review_evidence=Path(args.review_evidence).expanduser() if args.review_evidence else None)
            human = f"Removed {args.initiative}: {', '.join(data['removed_repositories'])}; force_used=false"
        print(json.dumps(data, indent=2, sort_keys=True) if args.json else human)
        return 0
    except WorktreeError as exc:
        payload = {"contract": "asha.workspace-worktree-error.v1", "ok": False, "error": {"code": exc.code, "message": str(exc), "details": exc.details}}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"workspace worktree: {exc.code}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
