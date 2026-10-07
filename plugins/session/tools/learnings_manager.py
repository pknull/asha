#!/usr/bin/env python3
"""Explicit candidate → active → retired learning lifecycle for Memory v2."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from path_safety import secure_path, secure_project_root


_SELECTED_HOME = ContextVar('learning_home', default=None)


@contextmanager
def at_home(home):
    """Use an explicit Control home without changing process-global environment."""
    token = _SELECTED_HOME.set(Path(home))
    try:
        yield
    finally:
        _SELECTED_HOME.reset(token)


def learnings_dir() -> Path:
    """The learnings bundle root, honoring ASHA_HOME.

    Resolved at call time, not import time: an import-time Path.home()
    constant could not be redirected by any caller or test.
    """
    asha_home = _SELECTED_HOME.get() or os.environ.get("ASHA_HOME")
    base = Path(asha_home) if asha_home else Path.home() / ".asha"
    return base / "learnings"

STATES = ("candidate", "active", "retired")
ACTIVATION_SESSIONS = 3
ACTIVATION_PROJECTS = 2
MAX_PROPOSALS_PER_SAVE = 3
CANDIDATE_TTL_DAYS = 90


@dataclass
class Evidence:
    date: str
    session_id: str
    project_id: str
    reason: str
    kind: str = "corroborate"
    source_provenance: dict | None = None


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
    applicability: dict = field(default_factory=dict)


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
    """Return a stable root, following a top-level link such as a dotfiles bundle."""
    root = learnings_dir()
    if not root.is_symlink():
        return root
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("broken symlinked learning bundle rejected") from exc
    if not resolved.is_dir():
        raise ValueError("symlinked learning bundle target must be a directory")
    return resolved


def _secure_learning_child(relative: str, *, create_parents: bool = False) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("learning path escapes global bundle")
    # The local user owns the bundle; links inside it are their layout
    # (threat model, 2026-10-05), so they are followed like any path.
    cursor = _learning_root().joinpath(*relative_path.parts)
    if create_parents:
        cursor.parent.mkdir(parents=True, exist_ok=True)
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
        "applicability": learning.applicability,
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
        applicability=data.get("applicability", {}),
    )


@contextmanager
def _global_lock(*, recover: bool = True):
    # Keep the coordination inode outside the bundle so a read-only render
    # never alters it.
    learnings_dir().parent.mkdir(parents=True, exist_ok=True)
    lock_parent = learnings_dir().parent.resolve(strict=True)
    lock = lock_parent / ".asha-learnings-v2.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if recover:
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
        try:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.json", journal.name):
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


def rule_version(learning: Learning) -> str:
    return hashlib.sha256(json.dumps({"trigger": learning.trigger, "action": learning.action,
        "applicability": learning.applicability}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _source(source, publisher_session, publisher_project):
    if source is None:
        return None
    required = {"session_id", "project_id", "origin_key", "report_id", "observation_key",
                "evidence_digest", "adopting_save_identity", "harness", "harness_version"}
    if not isinstance(source, dict) or not required <= source.keys() or source.keys() - required - {'subject_kind', 'reviewer'}:
        raise ValueError("invalid source provenance")
    source = json.loads(json.dumps(source))
    if 'reviewer' in source and source['reviewer'] not in ('advisory-save-review', 'operator-advisory', 'native-automatic'):
        raise ValueError('invalid source reviewer')
    if 'subject_kind' in source and source['subject_kind'] not in ('worker-observation', 'reviewer-report-assessment'):
        raise ValueError("invalid source subject kind")
    for key in ("session_id", "project_id", "report_id", "observation_key"):
        if not isinstance(source[key], str) or not source[key].strip() or len(source[key]) > 256:
            raise ValueError("invalid observation source identity")
    for key in ("origin_key", "evidence_digest"):
        if not isinstance(source[key], str) or not re.fullmatch(r"[a-f0-9]{64}", source[key]):
            raise ValueError("invalid source digest")
    identity = source['adopting_save_identity']
    if (not isinstance(identity, dict) or set(identity) != {'session_id', 'project_id', 'publication_id'}
            or identity['session_id'] != publisher_session or identity['project_id'] != publisher_project
            or not isinstance(identity['publication_id'], str) or not identity['publication_id']):
        raise ValueError("source must retain the explicit adopting save identity")
    if source['harness'] not in ('claude', 'codex', 'copilot', 'opencode', None):
        raise ValueError("invalid source harness")
    if source['harness_version'] is not None and not isinstance(source['harness_version'], str):
        raise ValueError("invalid source version")
    return source


def _publisher(item):
    if item.source_provenance:
        identity = item.source_provenance['adopting_save_identity']
        return identity['session_id'], identity['project_id']
    return item.session_id, item.project_id


def _add_evidence(learning: Learning, session_id: str, project_id: str, reason: str, kind: str,
                  source_provenance=None) -> bool:
    if not session_id or not project_id:
        raise ValueError("session_id and project_id are required")
    if source_provenance:
        origin = source_provenance['origin_key']
        if any(item.source_provenance and item.source_provenance['origin_key'] == origin
               and (item.kind == 'contradict') == (kind == 'contradict') for item in learning.evidence):
            return False
        session_id, project_id = source_provenance['session_id'], source_provenance['project_id']
    else:
        # Contradiction from an already-positive source must not be deduplicated
        # away. The latest contradiction still fences all earlier positives.
        if any((item.session_id, item.project_id) == (session_id, project_id)
               and (item.kind == 'contradict') == (kind == 'contradict')
               and (kind != 'contradict' or item.reason == reason[:500]) for item in learning.evidence):
            return False
    learning.evidence.append(Evidence(_today(), session_id, project_id, reason[:500], kind, source_provenance))
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
            session_id: str, reason: str, source_provenance=None, applicability=None) -> Learning:
    new_trigger, new_action = trigger.strip(), action.strip()
    _assert_not_silenced(project_dir)
    with _global_lock():
        session_id, project_id = _save_identity(project_dir, session_id)
        source_provenance = _source(source_provenance, session_id, project_id)
        try:
            learning = _load_unlocked(learning_id)
        except KeyError:
            learning = Learning(learning_id.strip(), new_trigger, new_action,
                                created=_today(), updated=_today())
        if learning.state == "retired":
            raise ValueError("retired learning requires a new id")
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
            _publisher(item) == (session_id, project_id)
            for item in learning.evidence
        )
        if not already_recorded:
            proposed_this_session = sum(
                1
                for existing in _list_state_unlocked()
                if any(item.kind == "propose" and _publisher(item) == (session_id, project_id) for item in existing.evidence)
            )
            if proposed_this_session >= MAX_PROPOSALS_PER_SAVE:
                raise ValueError("at most 3 learning candidates may be proposed per save")
        if applicability is not None:
            if learning.state == 'active' and learning.applicability != applicability:
                raise ValueError('active applicability cannot change through a proposal')
            if learning.applicability and learning.applicability != applicability:
                learning.evidence = [item for item in learning.evidence if item.kind == 'contradict']
            learning.applicability = applicability
        _add_evidence(learning, session_id, project_id, reason, "propose", source_provenance)
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
                reason: str, source_provenance=None) -> Learning:
    _assert_not_silenced(project_dir)
    with _global_lock():
        session_id, project_id = _save_identity(project_dir, session_id)
        source_provenance = _source(source_provenance, session_id, project_id)
        learning = _load_unlocked(learning_id)
        if learning.state == "retired":
            raise ValueError("retired learning cannot be corroborated")
        if _add_evidence(learning, session_id, project_id, reason, "corroborate", source_provenance):
            _save_unlocked(learning)
        return learning


def adopt_reviewed(learning_id: str, *, project_dir: Path, session_id: str, reason: str,
                   source_provenance: dict, operation: str, expected_version=None,
                   trigger=None, action=None, applicability=None) -> Learning:
    """One locked, idempotent explicit-save operation for a retained origin.

    The publisher owns the three-candidate guard; the original observation owns
    corroboration diversity. SQLite receipt recovery never restores file snapshots.
    """
    if operation not in {'propose', 'corroborate'}:
        raise ValueError('invalid reviewed learning operation')
    with _global_lock():
        session_id, project_id = _save_identity(project_dir, session_id)
        source = _source(source_provenance, session_id, project_id)
        try:
            learning = _load_unlocked(learning_id)
        except KeyError:
            if operation != 'propose' or expected_version is not None:
                raise ValueError('reviewed target learning is missing')
            if not isinstance(trigger, str) or not trigger.strip() or not isinstance(action, str) or not action.strip():
                raise ValueError('reviewed trigger and action are required')
            learning = Learning(learning_id, trigger.strip(), action.strip(),
                                created=_today(), updated=_today(), applicability=applicability or {})
        else:
            if learning.state == 'retired':
                raise ValueError('retired learning cannot be adopted')
            if expected_version is not None and expected_version != rule_version(learning):
                raise ValueError('reviewed learning version changed; inspect before retrying')
            if operation == 'corroborate' and expected_version is None:
                raise ValueError('corroboration requires the inspected rule version')
            if operation == 'propose' and (trigger != learning.trigger or action != learning.action
                                         or (applicability or {}) != learning.applicability):
                raise ValueError('reviewed proposal cannot overwrite existing semantics; inspect or use a new id')
        matching = [item for item in learning.evidence if item.source_provenance
                    and item.source_provenance['origin_key'] == source['origin_key']]
        if matching:
            if any(item.source_provenance['evidence_digest'] != source['evidence_digest'] for item in matching):
                raise ValueError('origin evidence changed; reconcile original observation before adoption')
            return learning
        if operation == 'propose' and not any(_publisher(e) == (session_id, project_id) for e in learning.evidence):
            count = sum(any(e.kind == 'propose' and _publisher(e) == (session_id, project_id)
                            for e in candidate.evidence) for candidate in _list_state_unlocked())
            if count >= MAX_PROPOSALS_PER_SAVE:
                raise ValueError('at most 3 learning candidates may be proposed per save')
        _add_evidence(learning, session_id, project_id, reason, operation, source)
        _assert_not_silenced(project_dir)
        return _save_unlocked(learning)


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
               reason: str, source_provenance=None) -> Learning:
    _assert_not_silenced(project_dir)
    with _global_lock():
        session_id, project_id = _save_identity(project_dir, session_id)
        source_provenance = _source(source_provenance, session_id, project_id)
        learning = _load_unlocked(learning_id)
        if learning.state == "retired":
            raise ValueError("retired learning cannot transition")
        _add_evidence(learning, session_id, project_id, reason, "contradict", source_provenance)
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
        else:
            result = {"expired": expire_candidates(project_dir=args.project_dir)}
        print(json.dumps(asdict(result) if isinstance(result, Learning) else result,
                         ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
