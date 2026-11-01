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

# The session plugin's own version, mirrored from plugins/session/README.md and
# asserted equal by tests: it is the `generated.by` actor of every record this
# writer emits (`asha/<version>`), so it must never read "unknown".
SESSION_PLUGIN_VERSION = "3.0.0"
GENERATED_BY = f"asha/{SESSION_PLUGIN_VERSION}"
# The OKF v0.2 record shape is emitted only once the migration script has
# written this marker; until then the writer keeps the legacy shape so a
# SessionStart maintenance save can never migrate the store incrementally.
OKF_MARKER = ".okf-0.2.json"
STALE_LIST = ".stale.json"
STATUS_BY_STATE = {"candidate": "draft", "active": "stable", "retired": "deprecated"}
STATE_BY_STATUS = {status: state for state, status in STATUS_BY_STATE.items()}
_OKF_TOOLS = Path(__file__).resolve().parents[2] / "okf" / "tools"
_UNPRINTABLE_RE = re.compile("[\x7f-\x9f\ufffe\uffff\ud800-\udfff]")
_ISO_INSTANT_RE = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2}))?")


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
    # OKF v0.2 trust fields. generated_at is the date trigger/action last
    # changed; verified holds only confirmations a person made (human: actors).
    generated_at: str = ""
    verified: list[dict] = field(default_factory=list)


def _today() -> str:
    return date.today().isoformat()


_FORCE_OKF = ContextVar("force_okf_shape", default=False)


@contextmanager
def force_okf_shape():
    """Render the OKF shape before the marker exists (the migration's pre-check)."""
    token = _FORCE_OKF.set(True)
    try:
        yield
    finally:
        _FORCE_OKF.reset(token)


def _okf_tools():
    """Import the okf plugin's indexer, log writer and link grammar from the sibling plugin."""
    import importlib.util
    modules = {}
    for name in ("okf_io", "okf_common", "okf_index", "okf_log"):
        location = _OKF_TOOLS / f"{name}.py"
        existing = sys.modules.get(name)
        if existing is not None:
            if Path(getattr(existing, "__file__", "") or "").resolve() != location.resolve():
                raise ValueError(f"a different module named {name} is already loaded; okf tools unavailable")
            modules[name] = existing
            continue
        spec = importlib.util.spec_from_file_location(name, location)
        if spec is None or spec.loader is None:
            raise ValueError(f"okf plugin tools unavailable at {_OKF_TOOLS}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except (OSError, ImportError) as exc:
            del sys.modules[name]
            raise ValueError(f"okf plugin tools unavailable at {_OKF_TOOLS}") from exc
        modules[name] = module
    return modules["okf_index"], modules["okf_log"], modules["okf_common"]


def _okf_enabled() -> bool:
    return _FORCE_OKF.get() or _secure_learning_child(OKF_MARKER).is_file()


def _stale_after(learning: "Learning", days: int = CANDIDATE_TTL_DAYS) -> str | None:
    """Candidates go stale `days` after their last corroboration (`updated`)."""
    if learning.state != "candidate":
        return None
    try:
        anchor = date.fromisoformat(learning.updated or learning.created)
    except ValueError:
        return None
    return f"{(anchor + timedelta(days=days)).isoformat()}T00:00:00+00:00"


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


def _record(learning: Learning) -> dict:
    """The frontmatter mapping: legacy shape, or OKF v0.2 once the store is migrated."""
    private = {
        "state": learning.state,
        "created": learning.created,
        "updated": learning.updated,
        "retirement_reason": learning.retirement_reason,
        "evidence": [asdict(item) for item in learning.evidence],
        "applicability": learning.applicability,
    }
    if not _okf_enabled():
        return {"type": "learning", "id": learning.id, "trigger": learning.trigger,
                "action": learning.action, **private}
    data = {
        "type": "learning",
        "id": learning.id,
        "title": learning.id,
        "description": learning.trigger,
        "status": STATUS_BY_STATE[learning.state],
        "trigger": learning.trigger,
        "action": learning.action,
        "generated": {"by": GENERATED_BY, "at": learning.generated_at or learning.created},
    }
    if learning.verified:
        data["verified"] = [dict(item) for item in learning.verified]
    stale_after = _stale_after(learning)
    if stale_after:
        data["stale_after"] = stale_after
    data.update(private)
    return data


def _render(learning: Learning) -> str:
    data = _record(learning)
    # JSON is a valid YAML mapping and lets the manager remain dependency-free.
    return f"---\n{json.dumps(data, ensure_ascii=False, indent=2)}\n---\n\n# {learning.id}\n\n**Trigger:** {learning.trigger}\n\n**Action:** {learning.action}\n"


def _parse(path: Path) -> Learning:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---(?:\n|$)", text, re.DOTALL)
    if not match:
        raise ValueError(f"invalid learning frontmatter: {path}")
    data = json.loads(match.group(1))
    evidence = [Evidence(**item) for item in data.get("evidence", [])]
    state = data.get("state") or STATE_BY_STATUS.get(data.get("status"), path.parent.name)
    generated = data.get("generated") if isinstance(data.get("generated"), dict) else {}
    verified = data.get("verified")
    if isinstance(verified, dict):
        verified = [verified]
    return Learning(
        id=str(data["id"]), trigger=str(data.get("trigger", "")),
        action=str(data.get("action", "")), state=str(state),
        evidence=evidence, created=str(data.get("created", "")),
        updated=str(data.get("updated", "")), retirement_reason=str(data.get("retirement_reason", "")),
        applicability=data.get("applicability", {}),
        generated_at=str(generated.get("at", "") or ""),
        verified=[dict(item) for item in verified if isinstance(item, dict)] if isinstance(verified, list) else [],
    )


def _check_record(data: dict, rendered: str, destination: Path) -> None:
    """Dependency-free per-record check, run before the transition journal is written.

    It covers what a writer can know about one record: the spec's required
    `type`, the recommended title and description, the v0.2 families this
    writer emits, no code point PyYAML or UTF-8 refuses, and that the index
    entry the indexer would write parses as one link to the file under the
    spec's link grammar. The bundle-wide strict run belongs to okf_validate.py
    at the explicit save.
    """
    def nonempty(key):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"learning record {destination.name}: '{key}' must be a non-empty string")
    if data.get("type") != "learning":
        raise ValueError(f"learning record {destination.name}: type must be 'learning'")
    for key in ("id", "title", "description"):
        nonempty(key)
    if data.get("status") not in STATE_BY_STATUS:
        raise ValueError(f"learning record {destination.name}: status must be draft|stable|deprecated")
    generated = data.get("generated")
    if not isinstance(generated, dict) or not isinstance(generated.get("by"), str) or not generated["by"].strip():
        raise ValueError(f"learning record {destination.name}: generated.by is required")
    if not isinstance(generated.get("at"), str) or not _ISO_INSTANT_RE.fullmatch(generated["at"]):
        raise ValueError(f"learning record {destination.name}: generated.at must be an ISO 8601 date or instant")
    stale_after = data.get("stale_after")
    if stale_after is not None and not (isinstance(stale_after, str) and _ISO_INSTANT_RE.fullmatch(stale_after)):
        raise ValueError(f"learning record {destination.name}: stale_after must be an ISO 8601 instant")
    for item in data.get("verified", []):
        if not isinstance(item, dict) or not isinstance(item.get("by"), str) or not item["by"].startswith("human:") \
                or not isinstance(item.get("at"), str) or not _ISO_INSTANT_RE.fullmatch(item["at"]):
            raise ValueError(f"learning record {destination.name}: verified entries need a human: actor and an ISO date")
    if _UNPRINTABLE_RE.search(rendered):
        raise ValueError(f"learning record {destination.name}: text contains a code point YAML or UTF-8 refuses "
                         "(DEL, C1 controls, U+FFFE/FFFF or a lone surrogate)")
    okf_index, _, okf_common = _okf_tools()
    entry = okf_index.concept_entry(Path(destination.name), data)
    links = okf_common.extract_links(entry)
    # The gate strips inline code over the whole index, so a backtick this entry
    # leaves unpaired would pair with a neighbour's and hide that neighbour's link.
    if not links or links[0] != f"./{destination.name}" or "`" in okf_common.strip_code(entry):
        raise ValueError(f"learning record {destination.name}: its index entry would not parse as one link to the "
                         f"file (the id must not contain ']' and the id and trigger must not carry an unpaired backtick)")


_LOG_KEYWORDS = {"propose": "Creation", "retire": "Deprecation", "recover": "Fix", "migrate": "Update"}


def _log_unlocked(operation: str, learning: Learning) -> None:
    _, okf_log, _ = _okf_tools()
    keyword = _LOG_KEYWORDS.get(operation, "Update")
    okf_log.append_entry(_learning_root(), f"**{keyword}** `{learning.id}` {operation}: {learning.state}",
                         actor=GENERATED_BY)


def _derive_unlocked(operation: str, learning: Learning | None) -> None:
    """Regenerate the indexes and append the log entry after the record is durable.

    These are derived artifacts. A failure here is reported, never raised: the
    record and journal are already consistent, so raising would leave a journal
    that every later lock holder replays into the same failure. The strict gate
    at the explicit save reports a stale index as orphan lints.
    """
    try:
        _rebuild_index_unlocked()
        if learning is not None:
            _log_unlocked(operation, learning)
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"WARNING: learnings index/log not updated ({exc}); the next save or okf_index.py rebuilds them",
              file=sys.stderr)


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
        if _okf_enabled():
            _derive_unlocked("recover", parsed)
    if recovered and not _okf_enabled():
        _rebuild_index_unlocked()


def _save_unlocked(learning: Learning, operation: str = "update") -> Learning:
    if learning.state not in STATES:
        raise ValueError(f"invalid state: {learning.state}")
    learning.created = learning.created or _today()
    learning.updated = learning.updated or _today()
    _secure_learning_roots()
    _secure_learning_roots(create=True)
    destination = _path(learning)
    journal = _transition_journal_path(learning)
    rendered = _render(learning)
    if _okf_enabled():
        # Refuse before any write: a failure after the journal exists would be
        # replayed by the next lock holder and land unchecked.
        _check_record(_record(learning), rendered, destination)
    _atomic(journal, json.dumps({"version": 2, "name": destination.name,
                                "state": learning.state, "content": rendered,
                                "content_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest()},
                               sort_keys=True) + "\n")
    _atomic(destination, rendered)
    for state in STATES:
        other = _secure_learning_child(f"{state}/{destination.name}")
        if other != destination:
            _unlink_durable(other)
    if _okf_enabled():
        # The journal guards the record write only; derived artifacts follow it
        # and are regenerated by any later save if they fail.
        _unlink_durable(journal)
        _derive_unlocked(operation, learning)
    else:
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
        if semantics_changed or not learning.generated_at:
            learning.generated_at = _today()
        _add_evidence(learning, session_id, project_id, reason, "propose", source_provenance)
        return _save_unlocked(learning, "propose")


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
            _save_unlocked(learning, "corroborate")
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
        learning.generated_at = learning.generated_at or _today()
        _add_evidence(learning, session_id, project_id, reason, operation, source)
        _assert_not_silenced(project_dir)
        return _save_unlocked(learning, "adopt")


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
        _save_unlocked(learning, "activate")
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
        return _save_unlocked(learning, "contradict")


def retire(learning_id: str, reason: str, *, project_dir: Path) -> Learning:
    _assert_not_silenced(project_dir)
    with _global_lock():
        learning = _load_unlocked(learning_id)
        learning.state = "retired"
        learning.retirement_reason = reason.strip()
        learning.updated = _today()
        return _save_unlocked(learning, "retire")


def _require_okf(verb: str) -> None:
    if not _okf_enabled():
        raise ValueError(f"{verb} needs the OKF-migrated store (run migrate_learnings_okf.py first)")


def extend(learning_id: str, *, project_dir: Path) -> Learning:
    """Keeper ruling on a stale candidate: another 90 days from today, no evidence added."""
    _assert_not_silenced(project_dir)
    with _global_lock():
        _require_okf("extend")
        learning = _load_unlocked(learning_id)
        if learning.state != "candidate":
            raise ValueError("only a candidate learning can be extended")
        learning.updated = _today()
        return _save_unlocked(learning, "extend")


def verify(learning_id: str, *, project_dir: Path, by: str = "human:keeper") -> Learning:
    """Record a person's confirmation; the only writer of OKF `verified`."""
    _assert_not_silenced(project_dir)
    if not isinstance(by, str) or not by.startswith("human:") or len(by) <= len("human:"):
        raise ValueError("verified entries record people only: use a human:<id> actor")
    with _global_lock():
        _require_okf("verify")
        learning = _load_unlocked(learning_id)
        if learning.state == "retired":
            raise ValueError("retired learning cannot be verified")
        learning.verified.append({"by": by, "at": _today()})
        return _save_unlocked(learning, "verify")


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


# Matches ASHA_LEARNINGS_MAX_BYTES in hooks/handlers/operational-budget.sh.
ACTIVE_RENDER_MAX_BYTES = 4500


def render_active(max_bytes: int = ACTIVE_RENDER_MAX_BYTES) -> str:
    with _global_lock(recover=False):
        return "\n".join(_select_active_lines(_list_state_unlocked("active"), max_bytes))


def _rebuild_index_unlocked(max_bytes: int = ACTIVE_RENDER_MAX_BYTES) -> None:
    # max_bytes shapes only the legacy budgeted index; OKF indexes list every record.
    if _okf_enabled():
        okf_index, _, _ = _okf_tools()
        okf_index.write_indexes(_learning_root())
        return
    content = _render_active_index(_list_state_unlocked("active"), max_bytes)
    _atomic(_secure_learning_child("active/index.md", create_parents=True), content)


def _select_active_lines(learnings: Iterable[Learning], max_bytes: int) -> list[str]:
    """Longest id-ordered prefix within max_bytes; a dropped tail is named, never silent."""
    lines = [f"- {item.id}: when {item.trigger}; {item.action}"
             for item in sorted(learnings, key=lambda item: item.id)]
    if len("\n".join(lines).encode("utf-8")) <= max_bytes:
        return lines
    # prefix[k] is the byte size of the first k lines joined by newlines.
    prefix = [0]
    for line in lines:
        prefix.append(prefix[-1] + len(line.encode("utf-8")) + (1 if len(prefix) > 1 else 0))
    note = ""
    for keep in range(len(lines) - 1, -1, -1):
        note = (f"[Omitted {len(lines) - keep} of {len(lines)} active learnings over the "
                f"{max_bytes}-byte budget; read them in {learnings_dir() / 'active'}.]")
        if prefix[keep] + (1 if keep else 0) + len(note.encode("utf-8")) <= max_bytes:
            return [*lines[:keep], note]
    return [note]


def _render_active_index(learnings: Iterable[Learning], max_bytes: int = ACTIVE_RENDER_MAX_BYTES) -> str:
    selected = _select_active_lines(learnings, max_bytes)
    return "# Active learnings\n\n" + ("\n".join(selected) or "No active learnings.") + "\n"


def rebuild_index(max_bytes: int = ACTIVE_RENDER_MAX_BYTES) -> None:
    with _global_lock():
        _rebuild_index_unlocked(max_bytes)


def maintain_candidates(*, project_dir: Path, days: int = CANDIDATE_TTL_DAYS) -> list[str]:
    """SessionStart maintenance: replay any pending journal, then list stale candidates.

    Nothing is retired here. A candidate is stale `days` after its last
    corroboration; the list is for the Keeper's ruling (`extend` or `retire`)
    and persists in the bundle's .stale.json once the store is OKF-shaped.
    """
    _assert_not_silenced(project_dir)
    today = date.today()
    stale: list[str] = []
    with _global_lock():
        for learning in _list_state_unlocked("candidate"):
            instant = _stale_after(learning, days)
            if instant and date.fromisoformat(instant[:10]) <= today:
                stale.append(learning.id)
        if _okf_enabled():
            _atomic(_secure_learning_child(STALE_LIST),
                    json.dumps({"checked": today.isoformat(), "days": days, "stale": stale}, indent=2) + "\n")
    return stale


# Kept as an alias for the maintenance verb; it has not retired a record since
# session 3.0.0 (Keeper, 2026-10-31: stale candidates wait for a ruling).
expire_candidates = maintain_candidates


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
    render.add_argument("--max-bytes", type=int, default=ACTIVE_RENDER_MAX_BYTES)
    for name in ("maintain", "expire"):
        maintain = sub.add_parser(name)
        maintain.add_argument("--project-dir", required=True, type=Path)
    extend_cmd = sub.add_parser("extend")
    extend_cmd.add_argument("--id", required=True)
    extend_cmd.add_argument("--project-dir", required=True, type=Path)
    verify_cmd = sub.add_parser("verify")
    verify_cmd.add_argument("--id", required=True)
    verify_cmd.add_argument("--project-dir", required=True, type=Path)
    verify_cmd.add_argument("--by", default="human:keeper")
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
        elif args.command == "extend":
            result = extend(args.id, project_dir=args.project_dir)
        elif args.command == "verify":
            result = verify(args.id, project_dir=args.project_dir, by=args.by)
        else:
            result = {"stale": maintain_candidates(project_dir=args.project_dir)}
        print(json.dumps(asdict(result) if isinstance(result, Learning) else result,
                         ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
