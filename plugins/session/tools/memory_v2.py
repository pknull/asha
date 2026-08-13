#!/usr/bin/env python3
"""Memory v2 schema validation, atomic publication, and project initialization.

This module never derives semantic memory.  The caller supplies model-authored
text from the live conversation; this code only enforces the publication
contract and performs same-directory atomic replacement.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from path_safety import secure_path, secure_project_root


ACTIVE_LIMIT = 4096
ACTIVE_HEADINGS = ("Objective", "State", "Next", "Blockers")
ACTIVE_TEMPLATE = """# Objective

Not yet recorded.

# State

Not yet recorded.

# Next

- None.

# Blockers

- None.
"""
DECISIONS_TEMPLATE = "# Decisions\n\n- None.\n"
IGNORE_RULE = "/Work/session-state/"
MIGRATION_IGNORE_RULE = "/Work/memory-migration/"
IGNORE_MARKER = "# Asha Memory v2 recovery (managed)"


def _assert_persistence_enabled(root: Path) -> None:
    marker = secure_path(root, "Work/markers/silence")
    if marker.exists():
        raise ValueError("Memory persistence is disabled by Work/markers/silence")


def atomic_write_bytes(path: Path, content: bytes, mode: int | None = None) -> None:
    """Durably replace *path* using a temporary file beside the destination."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    target_mode = mode
    if target_mode is None:
        try:
            target_mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            target_mode = 0o644
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, target_mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, target_mode)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    atomic_write_bytes(path, content.encode("utf-8"), mode)


def _headings(text: str) -> list[str]:
    return re.findall(r"^#{1,6}\s+(.+?)\s*$", text, re.MULTILINE)


def _section(text: str, heading: str) -> str:
    match = re.search(
        rf"^# {re.escape(heading)}\s*$\n(.*?)(?=^# |\Z)", text, re.MULTILINE | re.DOTALL
    )
    return match.group(1) if match else ""


def _list_items(section: str) -> int:
    return sum(bool(re.match(r"^\s*(?:[-*+] |\d+[.)] )", line)) for line in section.splitlines())


def validate_active_context(text: str) -> None:
    if not isinstance(text, str):
        raise ValueError("activeContext must be UTF-8 text")
    size = len(text.encode("utf-8"))
    if size > ACTIVE_LIMIT:
        raise ValueError(f"activeContext exceeds 4096 UTF-8 bytes ({size})")
    if _headings(text) != list(ACTIVE_HEADINGS):
        raise ValueError("activeContext headings must be exactly Objective, State, Next, Blockers")
    for heading in ACTIVE_HEADINGS:
        if not _section(text, heading).strip():
            raise ValueError(f"activeContext {heading} section must not be empty")
    for heading in ("Next", "Blockers"):
        if _list_items(_section(text, heading)) > 5:
            raise ValueError(f"activeContext {heading} may contain at most 5 items")


def validate_decisions(text: str) -> None:
    if not isinstance(text, str):
        raise ValueError("decisions must be UTF-8 text")
    headings = _headings(text)
    if headings != ["Decisions"]:
        raise ValueError("decisions must contain current binding decisions only; history/archive sections are forbidden")


def _digest_or_none(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def publish(project_dir: Path, active_context: str, decisions: str, *,
            expected_preimages: dict[str, str | None] | None = None) -> None:
    """Publish the authoritative pair under a project lock and recovery journal."""
    validate_active_context(active_context)
    validate_decisions(decisions)
    root = secure_project_root(project_dir)
    _assert_persistence_enabled(root)
    require_v2_config(root)
    active_path, decisions_path = _publication_paths(root)
    with _publication_lock(root):
        _recover_publication_unlocked(root)
        if expected_preimages is not None:
            current = {
                "active": _digest_or_none(active_path),
                "decisions": _digest_or_none(decisions_path),
            }
            if current != expected_preimages:
                raise ValueError("publication preimage changed after reviewed migration preflight")
        _prepare_publication_journal_unlocked(root)
        try:
            atomic_write(active_path, active_context)
            atomic_write(decisions_path, decisions)
        except Exception:
            _recover_publication_unlocked(root)
            raise
        _remove_journal(root)


def read_published(project_dir: Path) -> tuple[str, str]:
    """Read the published pair coherently under the publication lock."""
    root = secure_project_root(project_dir)
    _assert_persistence_enabled(root)
    require_v2_config(root)
    with _publication_lock(root):
        _recover_publication_unlocked(root)
        active, decisions = _publication_paths(root)
        active_text = active.read_text(encoding="utf-8")
        decisions_text = decisions.read_text(encoding="utf-8")
        validate_active_context(active_text)
        validate_decisions(decisions_text)
        return active_text, decisions_text


def read_project_config(project_dir: Path) -> dict[str, Any]:
    root = secure_project_root(project_dir)
    path = secure_path(root, ".asha/config.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise ValueError(f"existing project config is unreadable or malformed: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"existing project config must be a JSON object: {path}")
    return data


def require_v2_config(project_dir: Path) -> dict[str, Any]:
    config = read_project_config(project_dir)
    if (config.get("memory_version") != 2 or
            not isinstance(config.get("project_id"), str) or
            not config["project_id"].strip()):
        raise ValueError("valid Memory v2 project config with stable project_id required")
    return config


def _ensure_ignore(project_dir: Path) -> None:
    path = secure_path(project_dir, ".gitignore", create_parents=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    desired = managed_ignore_text(existing)
    if existing != desired:
        atomic_write(path, desired)

    git_dir = project_dir / ".git"
    if git_dir.exists():
        probes = (project_dir / "Work/session-state/.asha-ignore-probe.json",
                  project_dir / "Work/memory-migration/.asha-ignore-probe.json")
        for probe in probes:
            result = subprocess.run(
                ["git", "check-ignore", "--no-index", "--quiet", "--", str(probe)],
                cwd=project_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False,
            )
            if result.returncode != 0:
                raise ValueError(f"{probe.parent.relative_to(project_dir)} is not effectively ignored by Git")


def managed_ignore_text(existing: str) -> str:
    managed = f"{IGNORE_MARKER}\n{MIGRATION_IGNORE_RULE}\n{IGNORE_RULE}\n"
    if not existing.endswith(managed):
        separator = "" if not existing or existing.endswith("\n") else "\n"
        return f"{existing}{separator}{managed}"
    return existing


def ensure_private_ignores(project_dir: Path) -> None:
    """Install and verify only the two v2 private-work ignore rules."""
    root = secure_project_root(project_dir)
    _assert_persistence_enabled(root)
    secure_path(root, ".gitignore", create_parents=True)
    _ensure_ignore(root)


def _publication_paths(root: Path) -> tuple[Path, Path]:
    return (
        secure_path(root, "Memory/activeContext.md", create_parents=True),
        secure_path(root, "Memory/decisions.md", create_parents=True),
    )


def publication_journal_path(project_dir: Path) -> Path:
    root = secure_project_root(project_dir)
    return secure_path(root, "Work/session-state/.memory-publication-transaction.json",
                       create_parents=True)


def _publication_lock_path(root: Path) -> Path:
    return secure_path(root, "Work/session-state/.memory-publication.lock", create_parents=True)


@contextmanager
def _publication_lock(root: Path) -> Iterator[None]:
    lock = _publication_lock_path(root)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"existed": False, "content_b64": ""}
    if not path.is_file():
        raise ValueError(f"published Memory target is not a regular file: {path}")
    return {"existed": True, "content_b64": base64.b64encode(path.read_bytes()).decode("ascii")}


def _prepare_publication_journal_unlocked(root: Path) -> Path:
    active, decisions = _publication_paths(root)
    journal = publication_journal_path(root)
    record = {"version": 2, "state": "prepared", "active": _snapshot(active),
              "decisions": _snapshot(decisions)}
    atomic_write(journal, json.dumps(record, sort_keys=True) + "\n", mode=0o600)
    return journal


def prepare_publication_journal(project_dir: Path) -> Path:
    root = secure_project_root(project_dir)
    _assert_persistence_enabled(root)
    with _publication_lock(root):
        _recover_publication_unlocked(root)
        return _prepare_publication_journal_unlocked(root)


def _restore(path: Path, item: dict[str, Any]) -> None:
    if item.get("existed"):
        raw = base64.b64decode(str(item.get("content_b64", "")), validate=True)
        atomic_write_bytes(path, raw)
    else:
        path.unlink(missing_ok=True)


def _remove_journal(root: Path) -> None:
    journal = publication_journal_path(root)
    journal.unlink(missing_ok=True)
    try:
        fd = os.open(journal.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _recover_publication_unlocked(root: Path) -> bool:
    journal = publication_journal_path(root)
    if not journal.exists():
        return False
    try:
        record = json.loads(journal.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("version") != 2 or record.get("state") != "prepared":
            raise ValueError("invalid publication recovery journal")
        active, decisions = _publication_paths(root)
        _restore(active, record["active"])
        _restore(decisions, record["decisions"])
        _remove_journal(root)
        return True
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"publication recovery failed closed: {journal}") from exc


def recover_publication(project_dir: Path) -> bool:
    root = secure_project_root(project_dir)
    _assert_persistence_enabled(root)
    with _publication_lock(root):
        return _recover_publication_unlocked(root)


def initialize(project_dir: Path) -> str:
    """Create only v2 operational memory and preserve a stable project id."""
    root = secure_project_root(project_dir)
    _assert_persistence_enabled(root)
    config = read_project_config(root)
    if "project_id" in config:
        project_id = config["project_id"]
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("existing project_id must be a nonblank string")
        project_id = project_id.strip()
    else:
        project_id = str(uuid.uuid4())
    # Validate every write root before the first mutation. Initialization may
    # create missing directories, but it never follows an existing symlink.
    config_path = secure_path(root, ".asha/config.json", create_parents=True)
    active, decisions = _publication_paths(root)
    secure_path(root, "Work/session-state/.keep", create_parents=True)
    secure_path(root, ".gitignore", create_parents=True)
    # Existing publications are user data.  Preserve them, but refuse to mark
    # a legacy/invalid handoff as v2 until /session:consolidate reviews it.
    if active.exists():
        try:
            validate_active_context(active.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError("legacy activeContext requires reviewed migration before v2 init") from exc
    if decisions.exists():
        try:
            validate_decisions(decisions.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError("legacy decisions file requires reviewed migration before v2 init") from exc
    config.update({"initialized": True, "memory_version": 2, "project_id": project_id})
    atomic_write(config_path, json.dumps(config, indent=2, sort_keys=True) + "\n")
    if not active.exists():
        atomic_write(active, ACTIVE_TEMPLATE)
    if not decisions.exists():
        atomic_write(decisions, DECISIONS_TEMPLATE)
    _ensure_ignore(root)
    return project_id


def status(project_dir: Path) -> dict[str, Any]:
    root = secure_project_root(project_dir)
    config = require_v2_config(root)
    silenced = secure_path(root, "Work/markers/silence").exists()
    if silenced:
        with _publication_lock(root):
            if publication_journal_path(root).exists():
                raise ValueError("publication recovery is pending whilst Memory persistence is silenced")
            active_path, decisions_path = _publication_paths(root)
            active = active_path.read_text(encoding="utf-8")
            decisions = decisions_path.read_text(encoding="utf-8")
            validate_active_context(active)
            validate_decisions(decisions)
    else:
        active, decisions = read_published(root)
    return {
        "valid": True,
        "project_id": config["project_id"].strip(),
        "active_context_bytes": len(active.encode("utf-8")),
        "decisions_bytes": len(decisions.encode("utf-8")),
        "silenced": silenced,
        "rp_active": secure_path(root, "Work/markers/rp-active").exists(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Memory v2 publication tools")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--project-dir", required=True, type=Path)
    ensure = sub.add_parser("ensure-private-ignores")
    ensure.add_argument("--project-dir", required=True, type=Path)
    validate = sub.add_parser("validate")
    validate.add_argument("--project-dir", required=True, type=Path)
    pub = sub.add_parser("publish")
    pub.add_argument("--project-dir", required=True, type=Path)
    pub.add_argument("--active-file", required=True, type=Path)
    pub.add_argument("--decisions-file", required=True, type=Path)
    status_cmd = sub.add_parser("status")
    status_cmd.add_argument("--project-dir", required=True, type=Path)
    read = sub.add_parser("read")
    read.add_argument("--project-dir", required=True, type=Path)
    read.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            print(json.dumps({"project_id": initialize(args.project_dir)}))
        elif args.command == "ensure-private-ignores":
            ensure_private_ignores(args.project_dir)
            print(json.dumps({"status": "ignored"}))
        elif args.command == "validate":
            read_published(args.project_dir)
            print("memory-v2: valid")
        elif args.command == "publish":
            publish(args.project_dir, args.active_file.read_text(encoding="utf-8"),
                    args.decisions_file.read_text(encoding="utf-8"))
            print(json.dumps({"status": "published"}))
        elif args.command == "status":
            print(json.dumps(status(args.project_dir), sort_keys=True))
        else:
            active_text, decisions_text = read_published(args.project_dir)
            if args.format == "json":
                print(json.dumps({"activeContext": active_text, "decisions": decisions_text}))
            else:
                print(active_text.rstrip() + "\n\n" + decisions_text.rstrip())
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
