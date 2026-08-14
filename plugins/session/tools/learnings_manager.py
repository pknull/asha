#!/usr/bin/env python3
"""Explicit candidate → active → retired learning lifecycle for Memory v2."""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from path_safety import secure_path, secure_project_root


LEARNINGS_DIR = Path.home() / ".asha" / "learnings"
STATES = ("candidate", "active", "retired")
ACTIVATION_SESSIONS = 3
ACTIVATION_PROJECTS = 2
MAX_PROPOSALS_PER_SAVE = 3
CANDIDATE_TTL_DAYS = 90
MIGRATION_MARKER = ".migration-v2.json"


@dataclass
class Evidence:
    date: str
    session_id: str
    project_id: str
    reason: str
    kind: str = "corroborate"


@dataclass
class Learning:
    id: str
    trigger: str
    action: str
    state: str = "candidate"
    evidence: list[Evidence] = field(default_factory=list)
    created: str = ""
    updated: str = ""
    retirement_reason: str = ""


def _today() -> str:
    return date.today().isoformat()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise ValueError("learning id must contain a letter or number")
    return slug


def _storage_name(learning_id: str) -> str:
    raw = learning_id.strip()
    slug = _slug(raw)
    if raw == slug:
        return f"{slug}.md"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}.md"


def _path(learning: Learning) -> Path:
    return _secure_learning_child(f"{learning.state}/{_storage_name(learning.id)}")


def _learning_root() -> Path:
    """Return a stable root, allowing an intentional top-level dotfiles link."""
    root = LEARNINGS_DIR
    if not root.is_symlink():
        return root
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("broken symlinked learning bundle rejected") from exc
    if not resolved.is_dir():
        raise ValueError("symlinked learning bundle target must be a directory")
    if resolved.stat().st_uid != os.getuid():
        raise ValueError("symlinked learning bundle target must be owned by the current user")
    return resolved


def _secure_learning_child(relative: str, *, create_parents: bool = False) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("learning path escapes global bundle")
    cursor = _learning_root()
    if cursor.is_symlink():
        raise ValueError("symlinked learning bundle rejected")
    for part in relative_path.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"symlinked learning transaction root rejected: {cursor}")
    if create_parents:
        cursor.parent.mkdir(parents=True, exist_ok=True)
        if cursor.parent.is_symlink():
            raise ValueError(f"symlinked learning transaction root rejected: {cursor.parent}")
    return cursor


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.tmp.", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_directory(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _unlink_durable(path: Path, *, missing_ok: bool = True) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        if not missing_ok:
            raise
    else:
        _fsync_directory(path.parent)


def _rmtree_durable(path: Path) -> None:
    if path.exists():
        parent = path.parent
        shutil.rmtree(path)
        _fsync_directory(parent)


def _secure_learning_roots(*, create: bool = False) -> None:
    """Validate all v2 learning control/state roots before any write."""
    for relative in (".transactions/.root", *(f"{state}/.root" for state in STATES)):
        _secure_learning_child(relative, create_parents=create)


def _render(learning: Learning) -> str:
    data = {
        "type": "learning",
        "id": learning.id,
        "trigger": learning.trigger,
        "action": learning.action,
        "state": learning.state,
        "created": learning.created,
        "updated": learning.updated,
        "retirement_reason": learning.retirement_reason,
        "evidence": [asdict(item) for item in learning.evidence],
    }
    # JSON is a valid YAML mapping and lets the manager remain dependency-free.
    return f"---\n{json.dumps(data, ensure_ascii=False, indent=2)}\n---\n\n# {learning.id}\n\n**Trigger:** {learning.trigger}\n\n**Action:** {learning.action}\n"


def _parse(path: Path) -> Learning:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---(?:\n|$)", text, re.DOTALL)
    if not match:
        raise ValueError(f"invalid learning frontmatter: {path}")
    data = json.loads(match.group(1))
    evidence = [Evidence(**item) for item in data.get("evidence", [])]
    return Learning(
        id=str(data["id"]), trigger=str(data.get("trigger", "")),
        action=str(data.get("action", "")), state=str(data.get("state", path.parent.name)),
        evidence=evidence, created=str(data.get("created", "")),
        updated=str(data.get("updated", "")), retirement_reason=str(data.get("retirement_reason", "")),
    )


@contextmanager
def _global_lock(*, recover: bool = True):
    # Keep the coordination inode outside the legacy root OKF corpus. A
    # read-only render before reviewed migration must not alter that bundle.
    LEARNINGS_DIR.parent.mkdir(parents=True, exist_ok=True)
    lock_parent = LEARNINGS_DIR.parent.resolve(strict=True)
    lock = lock_parent / ".asha-learnings-v2.lock"
    if lock.is_symlink():
        raise ValueError(f"symlinked learning lock rejected: {lock}")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if recover:
            _recover_migration_transactions_unlocked()
            _recover_transitions_unlocked()
        elif _secure_learning_child(".transactions").is_dir() and any(
                _secure_learning_child(".transactions").glob("*.json")):
            raise ValueError("pending learning transition requires an explicit mutation recovery")
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _transition_journal_path(learning: Learning) -> Path:
    return _secure_learning_child(
        f".transactions/{Path(_storage_name(learning.id)).stem}.json", create_parents=True
    )


def _recover_transitions_unlocked() -> None:
    _secure_learning_roots()
    directory = _secure_learning_child(".transactions")
    if not directory.is_dir():
        return
    recovered = False
    for journal in sorted(directory.glob("*.json")):
        if journal.name.startswith("migration-"):
            continue
        try:
            if journal.is_symlink() or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.json", journal.name):
                raise ValueError
            record = json.loads(journal.read_text(encoding="utf-8"))
            name = str(record["name"])
            state = str(record["state"])
            if state not in STATES or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.md", name):
                raise ValueError
            destination = _secure_learning_child(f"{state}/{name}")
            content = str(record["content"])
            expected_digest = str(record.get("content_sha256", ""))
            if not expected_digest or hashlib.sha256(content.encode("utf-8")).hexdigest() != expected_digest:
                raise ValueError
            destination_valid = (
                destination.is_file() and
                hashlib.sha256(destination.read_bytes()).hexdigest() == expected_digest
            )
            if not destination_valid:
                _atomic(destination, content)
            parsed = _parse(destination)
            if parsed.state != state or _storage_name(parsed.id) != name:
                raise ValueError
            for other_state in STATES:
                other = _secure_learning_child(f"{other_state}/{name}")
                if other != destination:
                    _unlink_durable(other)
            _unlink_durable(journal, missing_ok=False)
            recovered = True
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"learning transition recovery failed: {journal}") from exc
    if recovered:
        _rebuild_index_unlocked()


def _save_unlocked(learning: Learning) -> Learning:
    if learning.state not in STATES:
        raise ValueError(f"invalid state: {learning.state}")
    learning.created = learning.created or _today()
    learning.updated = learning.updated or _today()
    _secure_learning_roots()
    _secure_learning_roots(create=True)
    destination = _path(learning)
    journal = _transition_journal_path(learning)
    rendered = _render(learning)
    _atomic(journal, json.dumps({"version": 2, "name": destination.name,
                                "state": learning.state, "content": rendered,
                                "content_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest()},
                               sort_keys=True) + "\n")
    _atomic(destination, rendered)
    for state in STATES:
        other = _secure_learning_child(f"{state}/{destination.name}")
        if other != destination:
            _unlink_durable(other)
    _rebuild_index_unlocked()
    _unlink_durable(journal)
    return learning


def _assert_not_silenced(project_dir: Path) -> Path:
    root = secure_project_root(project_dir)
    if secure_path(root, "Work/markers/silence").exists():
        raise ValueError("learning persistence is disabled by Work/markers/silence")
    return root


def save(learning: Learning, *, project_dir: Path) -> Learning:
    _assert_not_silenced(project_dir)
    with _global_lock():
        return _save_unlocked(learning)


def _load_unlocked(learning_id: str) -> Learning:
    name = _storage_name(learning_id)
    found: list[Path] = []
    for state in STATES:
        path = _secure_learning_child(f"{state}/{name}")
        if path.is_file():
            found.append(path)
    if len(found) > 1:
        raise ValueError(f"learning exists in multiple states: {learning_id}")
    if found:
        learning = _parse(found[0])
        if learning.id != learning_id.strip():
            raise ValueError(f"learning id does not match collision-safe record: {learning_id}")
        return learning
    raise KeyError(learning_id)


def load(learning_id: str) -> Learning:
    with _global_lock(recover=False):
        return _load_unlocked(learning_id)


def _add_evidence(learning: Learning, session_id: str, project_id: str, reason: str, kind: str) -> bool:
    if not session_id or not project_id:
        raise ValueError("session_id and project_id are required")
    key = (session_id, project_id)
    if any((item.session_id, item.project_id) == key for item in learning.evidence):
        return False
    learning.evidence.append(Evidence(_today(), session_id, project_id, reason[:500], kind))
    learning.updated = _today()
    return True


def _save_identity(project_dir: Path, session_id: str) -> tuple[str, str]:
    """Return heuristic save evidence bound to the project's stable id.

    Session ids and local Memory files remain user-controlled.  They are a
    corroboration heuristic, not a security authority.
    """
    root = _assert_not_silenced(project_dir)
    config_path = secure_path(root, ".asha/config.json")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("valid .asha/config.json project identity required") from exc
    if not isinstance(config, dict) or not isinstance(config.get("project_id"), str) or not config["project_id"].strip():
        raise ValueError("valid .asha/config.json project identity required")
    if not isinstance(session_id, str) or not session_id.strip() or session_id.strip() == "unknown":
        raise ValueError("nonblank explicit-save session_id required")
    return session_id.strip(), config["project_id"].strip()


def propose(learning_id: str, trigger: str, action: str, *, project_dir: Path,
            session_id: str, reason: str) -> Learning:
    new_trigger, new_action = trigger.strip(), action.strip()
    _assert_not_silenced(project_dir)
    with _global_lock():
        session_id, project_id = _save_identity(project_dir, session_id)
        try:
            learning = _load_unlocked(learning_id)
        except KeyError:
            learning = Learning(learning_id.strip(), new_trigger, new_action,
                                created=_today(), updated=_today())
        if learning.state == "retired":
            raise ValueError("retired learning requires reviewed migration or a new id")
        if learning.state == "active" and (
                (new_trigger and new_trigger != learning.trigger) or
                (new_action and new_action != learning.action)):
            raise ValueError("active semantic text cannot change through one proposal; use a new id")
        semantics_changed = (
            (new_trigger and new_trigger != learning.trigger) or
            (new_action and new_action != learning.action)
        )
        if learning.state == "candidate" and semantics_changed:
            # Evidence corroborates semantic text, not merely a stable slug.
            # Keep counterevidence visible, but require the revised rule to
            # earn its own positive threshold.
            learning.evidence = [item for item in learning.evidence if item.kind == "contradict"]
        if learning.state == "candidate":
            learning.trigger = new_trigger or learning.trigger
            learning.action = new_action or learning.action
        already_recorded = any(
            item.session_id == session_id and item.project_id == project_id
            for item in learning.evidence
        )
        if not already_recorded:
            proposed_this_session = sum(
                1
                for existing in _list_state_unlocked()
                if any(item.kind == "propose" and item.session_id == session_id and
                       item.project_id == project_id for item in existing.evidence)
            )
            if proposed_this_session >= MAX_PROPOSALS_PER_SAVE:
                raise ValueError("at most 3 learning candidates may be proposed per save")
        _add_evidence(learning, session_id, project_id, reason, "propose")
        return _save_unlocked(learning)


def propose_many(proposals: Iterable[dict[str, Any]], *, project_dir: Path,
                 session_id: str) -> list[Learning]:
    rows = list(proposals)
    if len(rows) > MAX_PROPOSALS_PER_SAVE:
        raise ValueError("at most 3 learning candidates may be proposed per save")
    return [propose(str(row["id"]), str(row["trigger"]), str(row["action"]),
                    project_dir=project_dir, session_id=session_id,
                    reason=str(row["reason"])) for row in rows]


def corroborate(learning_id: str, *, project_dir: Path, session_id: str,
                reason: str) -> Learning:
    _assert_not_silenced(project_dir)
    with _global_lock():
        session_id, project_id = _save_identity(project_dir, session_id)
        learning = _load_unlocked(learning_id)
        if learning.state == "retired":
            raise ValueError("retired learning cannot be corroborated")
        if _add_evidence(learning, session_id, project_id, reason, "corroborate"):
            _save_unlocked(learning)
        return learning


def activate_if_eligible(learning_id: str, *, project_dir: Path) -> bool:
    _assert_not_silenced(project_dir)
    with _global_lock():
        learning = _load_unlocked(learning_id)
        latest_contradiction = max(
            (index for index, item in enumerate(learning.evidence) if item.kind == "contradict"),
            default=-1,
        )
        positive = [item for item in learning.evidence[latest_contradiction + 1:]
                    if item.kind in ("propose", "corroborate")]
        sessions = {item.session_id for item in positive}
        projects = {item.project_id for item in positive}
        if learning.state != "candidate" or len(sessions) < ACTIVATION_SESSIONS or len(projects) < ACTIVATION_PROJECTS:
            return False
        learning.state = "active"
        learning.updated = _today()
        _save_unlocked(learning)
        return True


def contradict(learning_id: str, *, project_dir: Path, session_id: str,
               reason: str) -> Learning:
    _assert_not_silenced(project_dir)
    with _global_lock():
        session_id, project_id = _save_identity(project_dir, session_id)
        learning = _load_unlocked(learning_id)
        if learning.state == "retired":
            raise ValueError("retired learning cannot transition")
        _add_evidence(learning, session_id, project_id, reason, "contradict")
        learning.state = "candidate"
        return _save_unlocked(learning)


def retire(learning_id: str, reason: str, *, project_dir: Path) -> Learning:
    _assert_not_silenced(project_dir)
    with _global_lock():
        learning = _load_unlocked(learning_id)
        learning.state = "retired"
        learning.retirement_reason = reason.strip()
        learning.updated = _today()
        return _save_unlocked(learning)


def _list_state_unlocked(state: str | None = None) -> list[Learning]:
    selected = (state,) if state else STATES
    result: list[Learning] = []
    for item_state in selected:
        if item_state not in STATES:
            raise ValueError(f"invalid state: {item_state}")
        state_root = _secure_learning_child(item_state)
        if not state_root.exists():
            continue
        if not state_root.is_dir():
            raise ValueError(f"invalid learning state root: {state_root}")
        for path in sorted(state_root.glob("*.md")):
            if path.is_symlink():
                raise ValueError(f"symlinked learning record rejected: {path}")
            if path.name == "index.md":
                continue
            try:
                learning = _parse(path)
                if path.name != _storage_name(learning.id):
                    raise ValueError("learning filename does not match raw id")
                result.append(learning)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"invalid learning record: {path}") from exc
    return result


def list_state(state: str | None = None) -> list[Learning]:
    with _global_lock(recover=False):
        return _list_state_unlocked(state)


def render_active(max_bytes: int = 3000) -> str:
    lines: list[str] = []
    with _global_lock(recover=False):
        for learning in _list_state_unlocked("active"):
            line = f"- {learning.id}: when {learning.trigger}; {learning.action}"
            candidate = "\n".join([*lines, line])
            if len(candidate.encode("utf-8")) > max_bytes:
                break
            lines.append(line)
    return "\n".join(lines)


def _rebuild_index_unlocked(max_bytes: int = 3000) -> None:
    content = _render_active_index(_list_state_unlocked("active"), max_bytes)
    _atomic(_secure_learning_child("active/index.md", create_parents=True), content)


def _render_active_index(learnings: Iterable[Learning], max_bytes: int = 3000) -> str:
    lines = [f"- {item.id}: when {item.trigger}; {item.action}"
             for item in sorted(learnings, key=lambda item: item.id)]
    selected: list[str] = []
    for line in lines:
        if len("\n".join([*selected, line]).encode("utf-8")) > max_bytes:
            break
        selected.append(line)
    return "# Active learnings\n\n" + ("\n".join(selected) or "No active learnings.") + "\n"


def rebuild_index(max_bytes: int = 3000) -> None:
    with _global_lock():
        _rebuild_index_unlocked(max_bytes)


def expire_candidates(*, project_dir: Path, days: int = CANDIDATE_TTL_DAYS) -> list[str]:
    _assert_not_silenced(project_dir)
    cutoff = date.today() - timedelta(days=days)
    expired: list[str] = []
    with _global_lock():
        for learning in _list_state_unlocked("candidate"):
            try:
                stale = date.fromisoformat(learning.updated) < cutoff
            except ValueError:
                stale = False
            if stale:
                learning.state = "retired"
                learning.retirement_reason = f"candidate expired after {days} days"
                learning.updated = _today()
                _save_unlocked(learning)
                expired.append(learning.id)
    return expired


_FLAT_ENTRY_RE = re.compile(
    r"^###\s+(?P<id>[A-Za-z0-9_-]+)\s*$\n(?P<body>.*?)(?=^###\s+|^##\s+|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _migration_items_for_file(candidate: Path, *, archived: bool) -> list[dict[str, Any]]:
    raw = candidate.read_bytes()
    source_hash = hashlib.sha256(raw).hexdigest()
    base = {
        "source": str(candidate.resolve()),
        "source_sha256": source_hash,
        "source_bytes": len(raw),
        "legacy_kind": "archive" if archived else (
            "flat" if candidate.name in ("learnings.md", "learnings-archive.md") else "okf"),
        "decision": "defer",
        "proposed_state": "retired" if archived else "candidate",
    }
    if candidate.name in ("activeContext.md", "decisions.md"):
        base.update({
            "item_type": "project-publication",
            "publication_role": "activeContext" if candidate.name == "activeContext.md" else "decisions",
        })
        return [base]
    base["item_type"] = "legacy-evidence"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [base]
    if candidate.name not in ("learnings.md", "learnings-archive.md"):
        trigger = re.search(r"^\*\*Trigger:\*\*\s*(.+?)\s*$", text, re.MULTILINE)
        action = re.search(r"^\*\*Action:\*\*\s*(.+?)\s*$", text, re.MULTILINE)
        if trigger and action:
            base["item_type"] = "learning"
            base["proposal"] = {
                "id": _slug(candidate.stem),
                "trigger": trigger.group(1).strip(),
                "action": action.group(1).strip(),
                "reason": f"Reviewed legacy migration from {candidate.name}",
            }
        return [base]
    entries: list[dict[str, Any]] = []
    for match in _FLAT_ENTRY_RE.finditer(text):
        body = match.group("body")
        trigger = re.search(r"^- \*\*Trigger\*\*:\s*(.+?)\s*$", body, re.MULTILINE)
        action = re.search(r"^- \*\*Action\*\*:\s*(.+?)\s*$", body, re.MULTILINE)
        fragment = match.group(0)
        item = dict(base)
        item.update({
            "item_type": "learning",
            "source_fragment": match.group("id"),
            "source_fragment_sha256": hashlib.sha256(fragment.encode("utf-8")).hexdigest(),
            "proposal": {
                "id": _slug(match.group("id")),
                "trigger": trigger.group(1).strip() if trigger else "",
                "action": action.group(1).strip() if action else "",
                "reason": f"Reviewed legacy migration from {candidate.name}#{match.group('id')}",
            },
        })
        entries.append(item)
    return entries or [base]


def migrate_plan(legacy_paths: Iterable[Path], *, project_dir: Path) -> dict[str, Any]:
    """Inventory legacy inputs without changing or deleting them."""
    _assert_not_silenced(project_dir)
    items: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for supplied in legacy_paths:
        path = Path(supplied).expanduser()
        if not path.exists():
            continue
        if path.is_dir():
            candidates = sorted(item for item in path.rglob("*") if item.is_file())
        else:
            candidates = [path]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen or candidate.name == "index.md" or candidate.name.startswith("."):
                continue
            relative_parts = candidate.relative_to(path).parts if path.is_dir() else ()
            if relative_parts and (relative_parts[0] in STATES or
                                   any(part.startswith(".") for part in relative_parts)):
                continue
            seen.add(resolved)
            archived = "archive" in "-".join(part.lower() for part in candidate.parts)
            items.extend(_migration_items_for_file(candidate, archived=archived))
    return {"version": 2, "created_at": datetime.now(timezone.utc).isoformat(), "items": items}


def write_migration_plan(review: dict[str, Any], *, project_dir: Path,
                         output: Path | None = None, replace: bool = False) -> Path:
    """Atomically stage a private review plan without truncating prior review."""
    root = _assert_not_silenced(project_dir)
    import memory_v2
    memory_v2.ensure_private_ignores(root)
    directory = secure_path(root, "Work/memory-migration/.keep", create_parents=True).parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    desired = output or (directory / "review.json")
    desired = Path(desired)
    try:
        desired.resolve(strict=False).relative_to(directory.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("migration review path must remain under Work/memory-migration") from exc
    if desired.exists() and not replace:
        raise ValueError(f"existing migration review preserved: {desired}")
    _atomic(desired, json.dumps(review, ensure_ascii=False, indent=2) + "\n")
    os.chmod(desired, 0o600)
    return desired


def amend_migration_plan(review: dict[str, Any], *, project_dir: Path, output: Path,
                         active_context: str, decisions: str) -> Path:
    """Bind reviewed drafts and atomically replace an existing private plan."""
    import memory_v2

    memory_v2.validate_active_context(active_context)
    memory_v2.validate_decisions(decisions)
    if not isinstance(review, dict) or review.get("version") != 2 or not isinstance(review.get("items"), list):
        raise ValueError("invalid migration review format")
    if not Path(output).is_file():
        raise ValueError("migrate-amend requires an existing durable migration plan")
    amended = json.loads(json.dumps(review))
    amended["publication"] = {
        "active_context_sha256": hashlib.sha256(active_context.encode("utf-8")).hexdigest(),
        "decisions_sha256": hashlib.sha256(decisions.encode("utf-8")).hexdigest(),
    }
    return write_migration_plan(amended, project_dir=project_dir, output=output, replace=True)


def _snapshot_file(path: Path) -> tuple[bool, bytes]:
    return (path.exists(), path.read_bytes() if path.exists() else b"")


def _restore_file(path: Path, snapshot: tuple[bool, bytes]) -> None:
    existed, content = snapshot
    if existed:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.rollback.", dir=path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            _fsync_directory(path.parent)
        finally:
            tmp.unlink(missing_ok=True)
    else:
        _unlink_durable(path)


def _snapshot_record(snapshot: tuple[bool, bytes], *, expected: Iterable[bytes] = (),
                     expected_missing: bool = False,
                     final: Iterable[bytes] | None = None,
                     final_missing: bool | None = None) -> dict[str, Any]:
    existed, content = snapshot
    expected_values = tuple(expected)
    final_values = expected_values if final is None else tuple(final)
    return {
        "existed": existed,
        "content_b64": base64.b64encode(content).decode("ascii"),
        "preimage_sha256": hashlib.sha256(content).hexdigest() if existed else None,
        "expected_post_sha256": sorted({hashlib.sha256(value).hexdigest() for value in expected_values}),
        "expected_post_missing": expected_missing,
        "final_post_sha256": sorted({hashlib.sha256(value).hexdigest() for value in final_values}),
        "final_post_missing": expected_missing if final_missing is None else final_missing,
    }


def _snapshot_from_record(record: Any) -> tuple[bool, bytes]:
    if not isinstance(record, dict) or not isinstance(record.get("existed"), bool):
        raise ValueError("invalid migration transaction snapshot")
    content = base64.b64decode(str(record.get("content_b64", "")), validate=True)
    return record["existed"], content


def _current_digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _restore_if_unchanged(path: Path, record: dict[str, Any]) -> bool:
    snapshot = _snapshot_from_record(record)
    allowed = set(record.get("expected_post_sha256") or [])
    allowed.add(record.get("preimage_sha256"))
    if record.get("expected_post_missing") is True:
        allowed.add(None)
    if _current_digest(path) not in allowed:
        return False
    _restore_file(path, snapshot)
    return True


def _record_allowed_digests(record: dict[str, Any]) -> set[str | None]:
    _snapshot_from_record(record)
    allowed: set[str | None] = set(record.get("expected_post_sha256") or [])
    allowed.add(record.get("preimage_sha256"))
    if record.get("expected_post_missing") is True:
        allowed.add(None)
    return allowed


def _record_matches_current(path: Path, record: dict[str, Any]) -> bool:
    return _current_digest(path) in _record_allowed_digests(record)


def _record_matches_post(path: Path, record: dict[str, Any]) -> bool:
    expected: set[str | None] = set(record.get("final_post_sha256") or [])
    if record.get("final_post_missing") is True:
        expected.add(None)
    return _current_digest(path) in expected


def _recover_migration_transactions_unlocked() -> int:
    """Recover global journals only when the entire transaction is coherent."""
    import memory_v2

    _secure_learning_roots()
    transactions = _secure_learning_child(".transactions")
    if not transactions.is_dir():
        return 0
    recovered = 0
    for journal in sorted(transactions.glob("migration-*.json")):
        if journal.is_symlink():
            raise ValueError(f"symlinked migration journal rejected: {journal}")
        if not re.fullmatch(r"migration-[0-9a-f]{32}\.json", journal.name):
            continue
        try:
            record = json.loads(journal.read_text(encoding="utf-8"))
            if (record.get("version") != 2 or record.get("state") != "prepared" or
                    record.get("ready") is not True):
                raise ValueError
            root = secure_project_root(Path(record["project_root"]))
            if secure_path(root, "Work/markers/silence").exists():
                raise ValueError("recorded project is silenced; recovery evidence preserved")
            secure_path(root, "Work/memory-migration")
            backup_root = secure_path(root, "Work/memory-migration/backups")
            secure_path(root, "Work/memory-migration/.transactions")
            applied_root = secure_path(root, "Work/memory-migration/applied")
            backup_dir = secure_path(root, f"Work/memory-migration/backups/{record['backup_name']}")
            receipt = secure_path(root, f"Work/memory-migration/applied/{record['review_sha256']}.json")
            project_paths = {
                "config": secure_path(root, ".asha/config.json"),
                "active": secure_path(root, "Memory/activeContext.md"),
                "decisions": secure_path(root, "Memory/decisions.md"),
                "gitignore": secure_path(root, ".gitignore"),
            }
            project_records = record["project_records"]
            learning_records = record["learning_records"]
            backup_records = record["backup_records"]
            receipt_record = record["receipt_record"]
            if (not isinstance(project_records, dict) or not isinstance(learning_records, list) or
                    not isinstance(backup_records, list) or
                    not re.fullmatch(r"[0-9A-Za-z.:-]+-[0-9a-f]{12}", backup_dir.name) or
                    not re.fullmatch(r"[0-9a-f]{64}", str(record["review_sha256"])) or
                    backup_root.is_symlink() or applied_root.is_symlink()):
                raise ValueError
            parsed_learning: list[tuple[Path, dict[str, Any]]] = []
            seen_learning_paths: set[Path] = set()
            learning_root = _learning_root().absolute()
            for item in learning_records:
                supplied = Path(str(item["path"]))
                relative = supplied.absolute().relative_to(learning_root)
                path = _secure_learning_child(str(relative))
                if path in seen_learning_paths:
                    raise ValueError
                seen_learning_paths.add(path)
                _snapshot_from_record(item)
                parsed_learning.append((path, item))
            parsed_backups: list[tuple[Path, dict[str, Any]]] = []
            seen_backup_paths: set[Path] = set()
            for item in backup_records:
                path = secure_path(root, str(item["relative_path"]))
                if not path.is_relative_to(backup_dir):
                    raise ValueError
                if path in seen_backup_paths:
                    raise ValueError
                seen_backup_paths.add(path)
                _snapshot_from_record(item)
                parsed_backups.append((path, item))
            for name in project_paths:
                _snapshot_from_record(project_records[name])
            _snapshot_from_record(receipt_record)
            expected_backup_names = {path.name for path, _ in parsed_backups}
            if backup_dir.exists():
                if not backup_dir.is_dir() or backup_dir.is_symlink():
                    raise ValueError
                actual_names = {path.name for path in backup_dir.iterdir()}
                if not actual_names.issubset(expected_backup_names):
                    raise ValueError
        except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            detail = str(exc)
            if "silenced" in detail:
                raise ValueError(f"migration recovery blocked: {detail}") from exc
            raise ValueError(f"migration recovery failed closed; repair evidence preserved: {journal}") from exc

        with memory_v2._publication_lock(root):
            if secure_path(root, "Work/markers/silence").exists():
                raise ValueError(
                    f"migration recovery blocked: recorded project is silenced; recovery evidence preserved: {journal}"
                )
            all_records = [
                *((path, project_records[name]) for name, path in project_paths.items()),
                *parsed_learning,
                *parsed_backups,
                (receipt, receipt_record),
            ]
            if memory_v2.publication_journal_path(root).exists() or any(
                    not _record_matches_current(path, item) for path, item in all_records):
                raise ValueError(
                    f"migration recovery conflict; no state changed and repair evidence preserved: {journal}"
                )
            if secure_path(root, "Work/markers/silence").exists():
                raise ValueError(
                    f"migration recovery blocked: recorded project is silenced; recovery evidence preserved: {journal}"
                )

            receipt_complete = _record_matches_post(receipt, receipt_record)
            if receipt_complete:
                if not all(_record_matches_post(path, item) for path, item in all_records):
                    raise ValueError(
                        f"migration recovery conflict; completed receipt effects drifted and repair evidence preserved: {journal}"
                    )
                _unlink_durable(journal, missing_ok=False)
                recovered += 1
                continue

            # Recheck as one unit whilst the publication and global learning
            # locks are still held, then restore every record or none.
            if any(not _record_matches_current(path, item) for path, item in all_records):
                raise ValueError(
                    f"migration recovery conflict; no state changed and repair evidence preserved: {journal}"
                )
            for name, path in project_paths.items():
                _restore_file(path, _snapshot_from_record(project_records[name]))
            for path, item in parsed_learning:
                _restore_file(path, _snapshot_from_record(item))
            for path, item in parsed_backups:
                _restore_file(path, _snapshot_from_record(item))
            _restore_file(receipt, _snapshot_from_record(receipt_record))
            if backup_dir.exists():
                _rmtree_durable(backup_dir)
            _unlink_durable(journal, missing_ok=False)
            recovered += 1
    return recovered

def _migration_roots(root: Path, *, create: bool = False) -> tuple[Path, Path, Path]:
    """Validate every project-local migration control directory."""
    secure_path(root, "Work/memory-migration/.root", create_parents=create)
    backups = secure_path(
        root, "Work/memory-migration/backups/.root", create_parents=create
    ).parent
    transactions = secure_path(
        root, "Work/memory-migration/.transactions/.root", create_parents=create
    ).parent
    applied = secure_path(
        root, "Work/memory-migration/applied/.root", create_parents=create
    ).parent
    return backups, transactions, applied


def _transition_content(learning: Learning) -> tuple[bytes, bytes]:
    rendered = _render(learning)
    journal = json.dumps({
        "version": 2,
        "name": _path(learning).name,
        "state": learning.state,
        "content": rendered,
        "content_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    }, sort_keys=True) + "\n"
    return rendered.encode("utf-8"), journal.encode("utf-8")


def _validate_receipt_effects(root: Path, value: dict[str, Any], *,
                              review_digest: str,
                              expected_publication: dict[str, str]) -> None:
    """Fail closed when an idempotence receipt no longer matches live effects."""
    import memory_v2

    try:
        if (value.get("status") != "applied" or value.get("review_sha256") != review_digest or
                value.get("publication") != expected_publication):
            raise ValueError
        effects = value["effects"]
        config_path = secure_path(root, ".asha/config.json")
        if (_current_digest(config_path) != effects["config_sha256"] or
                memory_v2.require_v2_config(root)["project_id"].strip() != value["project_id"]):
            raise ValueError
        publication_effect = effects.get("publication")
        if publication_effect is not None:
            active = secure_path(root, "Memory/activeContext.md")
            decisions = secure_path(root, "Memory/decisions.md")
            with memory_v2._publication_lock(root):
                if memory_v2.publication_journal_path(root).exists():
                    raise ValueError
                if ({"active_context_sha256": _current_digest(active),
                     "decisions_sha256": _current_digest(decisions)} != publication_effect):
                    raise ValueError
        for item in effects["learnings"]:
            path = _secure_learning_child(str(item["relative_path"]))
            if _current_digest(path) != item["sha256"]:
                raise ValueError
            learning = _parse(path)
            if learning.id != item["id"] or learning.state != item["state"]:
                raise ValueError
        if sorted(value["applied_learnings"]) != sorted(
                item["id"] for item in effects["learnings"]):
            raise ValueError
        backup_root = secure_path(root, "Work/memory-migration/backups")
        backup_dir = secure_path(root, Path(value["backup_dir"]).absolute().relative_to(root))
        if backup_dir.parent != backup_root:
            raise ValueError
        for item in value["sources"]:
            path = secure_path(root, Path(item["backup"]).absolute().relative_to(root))
            if path.parent != backup_dir or _current_digest(path) != item["source_sha256"]:
                raise ValueError
        manifest = secure_path(root, Path(effects["backup_manifest"]).absolute().relative_to(root))
        if (manifest.parent != backup_dir or manifest.name != "manifest.json" or
                _current_digest(manifest) != effects["backup_manifest_sha256"]):
            raise ValueError
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise ValueError("migration receipt effects are missing or drifted") from exc


def _final_snapshot_record(snapshot: tuple[bool, bytes], final: bytes | None) -> dict[str, Any]:
    if final is None:
        return _snapshot_record(snapshot, expected_missing=True)
    return _snapshot_record(snapshot, expected=(final,))


def _ensure_migration_marker(result: dict[str, Any]) -> Path:
    """Write a stable informational marker after a reviewed migration commits."""
    review_digest = str(result.get("review_sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", review_digest):
        raise ValueError("migration result lacks a valid review digest")
    sources = result.get("sources")
    if not isinstance(sources, list):
        raise ValueError("migration result lacks reviewed sources")
    marker = _secure_learning_child(MIGRATION_MARKER)
    payload = {
        "version": 2,
        "status": "reviewed-migration-complete",
        "review_sha256": review_digest,
        "project_id": str(result.get("project_id", "")),
        "applied_learning_ids": sorted(str(value) for value in result.get("applied_learnings", [])),
        "reviewed_source_sha256": sorted(
            str(item.get("source_sha256")) for item in sources
            if isinstance(item, dict) and item.get("source_sha256")
        ),
    }
    _atomic(marker, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    os.chmod(marker, 0o600)
    return marker


def _repair_migration_marker(result: dict[str, Any]) -> bool:
    """Best-effort installer hint; migration authority stays in the receipt."""
    try:
        _ensure_migration_marker(result)
    except (OSError, ValueError):
        return False
    return True


def migrate_apply(review: dict[str, Any], *, session_id: str, project_dir: Path,
                  active_context: str, decisions: str) -> dict[str, Any]:
    """Apply one typed, hash-bound review as a rollback-safe transaction."""
    import memory_v2

    if not isinstance(session_id, str) or not session_id.strip() or session_id.strip() == "unknown":
        raise ValueError("reviewed migration attestation requires a session id")
    root = _assert_not_silenced(project_dir)
    memory_v2.validate_active_context(active_context)
    memory_v2.validate_decisions(decisions)
    if not isinstance(review, dict) or review.get("version") != 2 or not isinstance(review.get("items"), list):
        raise ValueError("invalid migration review format")

    publication = review.get("publication")
    expected_publication = {
        "active_context_sha256": hashlib.sha256(active_context.encode("utf-8")).hexdigest(),
        "decisions_sha256": hashlib.sha256(decisions.encode("utf-8")).hexdigest(),
    }
    if not isinstance(publication, dict) or any(
            publication.get(key) != digest for key, digest in expected_publication.items()):
        raise ValueError("migration publication digest does not match both reviewed drafts")

    review_digest = hashlib.sha256(json.dumps(review, sort_keys=True).encode("utf-8")).hexdigest()
    _migration_roots(root)
    receipt = secure_path(root, f"Work/memory-migration/applied/{review_digest}.json")
    journal = _secure_learning_child(f".transactions/migration-{uuid.uuid4().hex}.json")

    with _global_lock():
        if receipt.is_file():
            try:
                value = json.loads(receipt.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("migration receipt failed closed") from exc
            _validate_receipt_effects(root, value, review_digest=review_digest,
                                      expected_publication=expected_publication)
            _repair_migration_marker(value)
            return value

        config_path = secure_path(root, ".asha/config.json")
        active_path = secure_path(root, "Memory/activeContext.md")
        decisions_path = secure_path(root, "Memory/decisions.md")
        gitignore = secure_path(root, ".gitignore")
        publication_targets = {"activeContext": active_path, "decisions": decisions_path}

        # Preflight the entire review. Source bytes are captured exactly once;
        # backups never reopen the mutable source path.
        config = memory_v2.read_project_config(root)
        if "project_id" in config:
            if not isinstance(config["project_id"], str) or not config["project_id"].strip():
                raise ValueError("existing project_id must be a nonblank string")
            project_id = config["project_id"].strip()
        else:
            project_id = str(uuid.uuid4())
        accepted_sources: dict[Path, tuple[str, bytes]] = {}
        prepared: list[Learning] = []
        accepted_ids: set[str] = set()
        publication_authorizations: dict[str, tuple[str, Path]] = {}
        for item in review["items"]:
            if not isinstance(item, dict):
                raise ValueError("every migration item must be an object")
            decision = item.get("decision")
            if decision not in ("accept", "reject", "defer"):
                raise ValueError("every migration item requires accept, reject, or defer")
            if decision != "accept":
                continue
            item_type = item.get("item_type")
            if item_type not in ("project-publication", "learning", "legacy-evidence"):
                raise ValueError("accepted migration item requires a typed mapping")
            role = item.get("publication_role") if item_type == "project-publication" else None
            if role is not None and role not in publication_targets:
                raise ValueError("project-publication item requires a publication_role")
            if item_type == "project-publication" and item.get("create") is True:
                target = Path(str(item.get("target", ""))).absolute()
                expected_target = publication_targets[str(role)]
                if target != expected_target or expected_target.exists():
                    raise ValueError("explicit publication create mapping requires its absent exact target")
                if str(role) in publication_authorizations:
                    raise ValueError("duplicate project-publication role")
                publication_authorizations[str(role)] = ("create", target)
                continue

            source = Path(str(item.get("source", "")))
            expected = str(item.get("source_sha256", ""))
            if not source.is_file() or source.is_symlink() or not expected:
                raise ValueError(f"accepted migration source is missing, symlinked, or unhashed: {source}")
            raw = source.read_bytes()
            actual = hashlib.sha256(raw).hexdigest()
            if actual != expected:
                raise ValueError(f"migration source changed since review: {source}")
            resolved_source = source.resolve(strict=True)
            prior = accepted_sources.setdefault(resolved_source, (expected, raw))
            if prior != (expected, raw):
                raise ValueError(f"conflicting hashes for migration source: {source}")
            if item_type == "project-publication":
                if role is None or str(role) in publication_authorizations:
                    raise ValueError("project-publication item requires one unique publication_role")
                publication_authorizations[str(role)] = ("source", resolved_source)
                continue
            if item_type == "legacy-evidence":
                continue
            proposal = item.get("proposal") or {}
            if not all(proposal.get(key) for key in ("id", "trigger", "action", "reason")):
                raise ValueError(f"accepted migration item lacks a complete proposal: {source}")
            desired_state = str(item.get("proposed_state") or "candidate")
            if desired_state not in STATES:
                raise ValueError(f"invalid reviewed migration state: {desired_state}")
            learning_id = str(proposal["id"]).strip()
            if learning_id in accepted_ids:
                raise ValueError(f"duplicate accepted migration learning: {learning_id}")
            accepted_ids.add(learning_id)
            try:
                learning = _load_unlocked(learning_id)
                if (learning.trigger, learning.action) != (str(proposal["trigger"]), str(proposal["action"])):
                    raise ValueError(f"reviewed migration conflicts with existing learning: {learning_id}")
            except KeyError:
                learning = Learning(learning_id, str(proposal["trigger"]), str(proposal["action"]),
                                    created=_today(), updated=_today())
            _add_evidence(learning, session_id.strip(), project_id, str(proposal["reason"]),
                          "legacy-attestation")
            learning.state = desired_state
            if desired_state == "retired":
                learning.retirement_reason = str(proposal["reason"])
            prepared.append(learning)

        if publication_authorizations and set(publication_authorizations) != set(publication_targets):
            raise ValueError("review must accept both project-publication roles")
        if config.get("memory_version") != 2 and set(publication_authorizations) != set(publication_targets):
            raise ValueError("legacy initialization requires both project-publication roles")
        publish_outputs = bool(publication_authorizations)
        if publish_outputs:
            for role, target in publication_targets.items():
                kind, authorized = publication_authorizations[role]
                if target.exists():
                    if kind != "source" or authorized != target.resolve(strict=True):
                        raise ValueError(f"existing publication target must be its explicit accepted hash-bound source: {target}")
                elif kind != "create" or authorized != target:
                    raise ValueError(f"absent publication target requires an explicit create mapping: {target}")

        _secure_learning_roots()
        old_ignore = gitignore.read_bytes() if gitignore.exists() else b""
        try:
            desired_ignore = memory_v2.managed_ignore_text(old_ignore.decode("utf-8")).encode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("existing .gitignore must be UTF-8 before migration") from exc
        new_config = dict(config)
        new_config.update({"initialized": True, "memory_version": 2, "project_id": project_id})
        new_config_bytes = (json.dumps(new_config, indent=2, sort_keys=True) + "\n").encode("utf-8")

        project_snapshots = {
            "config": _snapshot_file(config_path), "active": _snapshot_file(active_path),
            "decisions": _snapshot_file(decisions_path), "gitignore": _snapshot_file(gitignore),
        }
        if publish_outputs:
            for role, target in publication_targets.items():
                snapshot = project_snapshots["active" if role == "activeContext" else "decisions"]
                kind, _authorized = publication_authorizations[role]
                if kind == "source":
                    expected_digest, reviewed_bytes = accepted_sources[target.resolve(strict=True)]
                    if (not snapshot[0] or snapshot[1] != reviewed_bytes or
                            hashlib.sha256(snapshot[1]).hexdigest() != expected_digest):
                        raise ValueError("publication target changed after reviewed source preflight")
                elif snapshot[0]:
                    raise ValueError("explicit publication create target appeared after review preflight")
        active_final = active_context.encode("utf-8") if publish_outputs else (
            project_snapshots["active"][1] if project_snapshots["active"][0] else None
        )
        decisions_final = decisions.encode("utf-8") if publish_outputs else (
            project_snapshots["decisions"][1] if project_snapshots["decisions"][0] else None
        )
        project_records = {
            "config": _final_snapshot_record(project_snapshots["config"], new_config_bytes),
            "active": _final_snapshot_record(project_snapshots["active"], active_final),
            "decisions": _final_snapshot_record(project_snapshots["decisions"], decisions_final),
            "gitignore": _final_snapshot_record(project_snapshots["gitignore"], desired_ignore),
        }

        learning_records_by_path: dict[Path, dict[str, Any]] = {}
        learning_effects: list[dict[str, str]] = []
        active_after = {item.id: item for item in _list_state_unlocked("active")}
        index_path = _secure_learning_child("active/index.md")
        index_expected: list[bytes] = []
        for learning in prepared:
            rendered, transition = _transition_content(learning)
            name = _storage_name(learning.id)
            for state in STATES:
                path = _secure_learning_child(f"{state}/{name}")
                final = rendered if state == learning.state else None
                learning_records_by_path.setdefault(path, _final_snapshot_record(_snapshot_file(path), final))
            transition_path = _secure_learning_child(f".transactions/{Path(name).stem}.json")
            learning_records_by_path.setdefault(
                transition_path,
                _snapshot_record(_snapshot_file(transition_path), expected=(transition,),
                                 expected_missing=True, final=(), final_missing=True),
            )
            active_after.pop(learning.id, None)
            if learning.state == "active":
                active_after[learning.id] = learning
            index_expected.append(_render_active_index(active_after.values()).encode("utf-8"))
            learning_effects.append({
                "id": learning.id, "state": learning.state,
                "relative_path": str(Path(learning.state) / name),
                "sha256": hashlib.sha256(rendered).hexdigest(),
            })
        if prepared:
            learning_records_by_path[index_path] = _snapshot_record(
                _snapshot_file(index_path), expected=index_expected,
                final=(index_expected[-1],)
            )

        backup_name = (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") +
                       "-" + uuid.uuid4().hex[:12])
        backup_dir = secure_path(root, f"Work/memory-migration/backups/{backup_name}")
        if backup_dir.exists():
            raise ValueError("collision-safe migration backup unexpectedly exists")
        backup_rows: list[dict[str, str]] = []
        backup_payloads: list[tuple[Path, bytes]] = []
        for index, (source, (digest, raw)) in enumerate(sorted(accepted_sources.items(), key=lambda row: str(row[0]))):
            destination = secure_path(
                root, f"Work/memory-migration/backups/{backup_name}/{index:04d}-{source.name}"
            )
            backup_payloads.append((destination, raw))
            backup_rows.append({"source": str(source), "source_sha256": digest,
                                "backup": str(destination)})
        manifest_path = secure_path(root, f"Work/memory-migration/backups/{backup_name}/manifest.json")
        manifest_bytes = (json.dumps({"version": 2, "sources": backup_rows},
                                     indent=2, sort_keys=True) + "\n").encode("utf-8")
        backup_payloads.append((manifest_path, manifest_bytes))
        backup_records = [
            dict(_final_snapshot_record(_snapshot_file(path), raw),
                 relative_path=str(path.relative_to(root)))
            for path, raw in backup_payloads
        ]
        effects = {
            "config_sha256": hashlib.sha256(new_config_bytes).hexdigest(),
            "publication": expected_publication if publish_outputs else None,
            "learnings": learning_effects,
            "backup_manifest": str(manifest_path),
            "backup_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        }
        result = {
            "status": "applied", "review_sha256": review_digest,
            "publication": expected_publication,
            "project_id": project_id, "applied_learnings": [item.id for item in prepared],
            "backup_dir": str(backup_dir), "sources": backup_rows, "effects": effects,
        }
        receipt_bytes = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        receipt_record = _final_snapshot_record(_snapshot_file(receipt), receipt_bytes)
        transaction = {
            "version": 2, "state": "prepared", "ready": True,
            "project_root": str(root), "review_sha256": review_digest,
            "backup_name": backup_name, "project_records": project_records,
            "learning_records": [dict(record, path=str(path))
                                 for path, record in learning_records_by_path.items()],
            "backup_records": backup_records, "receipt_record": receipt_record,
        }
        transaction_ready = False
        try:
            _migration_roots(root, create=True)
            _secure_learning_roots(create=True)
            _atomic(journal, json.dumps(transaction, sort_keys=True) + "\n")
            os.chmod(journal, 0o600)
            transaction_ready = True

            memory_v2.ensure_private_ignores(root)
            backup_dir.mkdir(parents=False, mode=0o700)
            _fsync_directory(backup_dir.parent)
            for destination, raw in backup_payloads:
                memory_v2.atomic_write_bytes(destination, raw, mode=0o600)
            _fsync_directory(backup_dir)
            memory_v2.atomic_write(config_path, new_config_bytes.decode("utf-8"))
            if publish_outputs:
                memory_v2.publish(
                    root, active_context, decisions,
                    expected_preimages={
                        "active": project_records["active"]["preimage_sha256"],
                        "decisions": project_records["decisions"]["preimage_sha256"],
                    },
                )
            for learning in prepared:
                _save_unlocked(learning)
            _atomic(receipt, receipt_bytes.decode("utf-8"))
            os.chmod(receipt, 0o600)
            _unlink_durable(journal, missing_ok=False)
            transaction_ready = False
            _repair_migration_marker(result)
            return result
        except Exception as original:
            if transaction_ready:
                try:
                    _recover_migration_transactions_unlocked()
                except Exception as recovery:
                    raise ValueError(f"{original}; {recovery}") from recovery
            else:
                _unlink_durable(journal)
            raise

def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Asha Memory v2 learnings manager")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("propose", "corroborate", "contradict"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--id", required=True)
        cmd.add_argument("--project-dir", required=True, type=Path)
        cmd.add_argument("--reason", required=True)
        cmd.add_argument("--session-id", required=True)
        if name == "propose":
            cmd.add_argument("--trigger", required=True)
            cmd.add_argument("--action", required=True)
    activate = sub.add_parser("activate-if-eligible")
    activate.add_argument("--id", required=True)
    activate.add_argument("--project-dir", required=True, type=Path)
    retire_cmd = sub.add_parser("retire")
    retire_cmd.add_argument("--id", required=True)
    retire_cmd.add_argument("--reason", required=True)
    retire_cmd.add_argument("--project-dir", required=True, type=Path)
    listing = sub.add_parser("list")
    listing.add_argument("--state", choices=STATES)
    render = sub.add_parser("render-active")
    render.add_argument("--max-bytes", type=int, default=3000)
    expire = sub.add_parser("expire")
    expire.add_argument("--project-dir", required=True, type=Path)
    plan = sub.add_parser("migrate-plan")
    plan.add_argument("--project-dir", required=True, type=Path)
    plan.add_argument("--output", type=Path)
    plan.add_argument("--replace-plan", action="store_true")
    plan.add_argument("paths", nargs="+")
    amend = sub.add_parser("migrate-amend")
    amend.add_argument("--project-dir", required=True, type=Path)
    amend.add_argument("--review", required=True, type=Path)
    amend.add_argument("--output", required=True, type=Path)
    amend.add_argument("--active-file", required=True, type=Path)
    amend.add_argument("--decisions-file", required=True, type=Path)
    apply = sub.add_parser("migrate-apply")
    apply.add_argument("--review", required=True, type=Path)
    apply.add_argument("--session-id", required=True)
    apply.add_argument("--project-dir", required=True, type=Path)
    apply.add_argument("--active-file", required=True, type=Path)
    apply.add_argument("--decisions-file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "propose":
            result: Any = propose(args.id, args.trigger, args.action,
                                  project_dir=args.project_dir, session_id=args.session_id,
                                  reason=args.reason)
        elif args.command == "corroborate":
            result = corroborate(args.id, project_dir=args.project_dir,
                                 session_id=args.session_id, reason=args.reason)
        elif args.command == "contradict":
            result = contradict(args.id, project_dir=args.project_dir,
                                session_id=args.session_id, reason=args.reason)
        elif args.command == "activate-if-eligible":
            result = {"activated": activate_if_eligible(args.id, project_dir=args.project_dir)}
        elif args.command == "retire":
            result = retire(args.id, args.reason, project_dir=args.project_dir)
        elif args.command == "list":
            result = [asdict(item) for item in list_state(args.state)]
        elif args.command == "render-active":
            print(render_active(args.max_bytes))
            return 0
        elif args.command == "expire":
            result = {"expired": expire_candidates(project_dir=args.project_dir)}
        elif args.command == "migrate-plan":
            result = migrate_plan((Path(item) for item in args.paths), project_dir=args.project_dir)
            if args.output:
                path = write_migration_plan(result, project_dir=args.project_dir,
                                            output=args.output, replace=args.replace_plan)
                result = {"status": "planned", "path": str(path),
                          "items": len(result["items"])}
        elif args.command == "migrate-amend":
            path = amend_migration_plan(
                json.loads(args.review.read_text(encoding="utf-8")),
                project_dir=args.project_dir, output=args.output,
                active_context=args.active_file.read_text(encoding="utf-8"),
                decisions=args.decisions_file.read_text(encoding="utf-8"),
            )
            result = {"status": "amended", "path": str(path)}
        else:
            result = migrate_apply(json.loads(args.review.read_text()),
                                   session_id=args.session_id,
                                   project_dir=args.project_dir,
                                   active_context=args.active_file.read_text(encoding="utf-8"),
                                   decisions=args.decisions_file.read_text(encoding="utf-8"))
        print(json.dumps(asdict(result) if isinstance(result, Learning) else result,
                         ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
