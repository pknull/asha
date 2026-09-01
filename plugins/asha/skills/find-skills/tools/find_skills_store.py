"""Canonical imported-skill store, drift checks, proposals, and approved writes."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any, Mapping, Sequence
import uuid

from find_skills_common import (
    LOCK_SCHEMA_VERSION,
    NAME_RE,
    REVISION_RE,
    SOURCE_RE,
    ApprovalError,
    CollisionError,
    ValidationError,
    json_bytes,
    parse_frontmatter,
    sha256_bytes,
    tree_digest,
    utc_now,
    validate_relative_path,
)


def lock_path(asha_home: Path) -> Path:
    return asha_home / "skills" / "imported.lock.json"


def _empty_lock() -> dict[str, Any]:
    return {"schema_version": LOCK_SCHEMA_VERSION, "skills": {}, "history": {}}


def load_lock(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValidationError(f"lockfile must not be a symlink: {path}")
    if not path.exists():
        return _empty_lock()
    if not path.is_file():
        raise ValidationError(f"lockfile must be a regular file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError(f"invalid imported skill lockfile: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != LOCK_SCHEMA_VERSION:
        raise ValidationError(f"unsupported imported skill lock schema: {path}")
    if not isinstance(data.get("skills"), dict) or not isinstance(data.get("history", {}), dict):
        raise ValidationError(f"invalid imported skill lock structure: {path}")
    for skill_name, entry in data["skills"].items():
        _validate_lock_entry(skill_name, entry)
    data.setdefault("history", {})
    return data


def _required_lock_field(entry: Mapping[str, Any], name: str, field: str) -> Any:
    if field not in entry:
        raise ValidationError(f"lock entry {name!r} is missing required field: {field}")
    return entry[field]


def _validate_lock_entry(name: Any, entry: Any) -> None:
    if not isinstance(name, str) or len(name) > 64 or not NAME_RE.fullmatch(name):
        raise ValidationError(f"invalid imported skill name in lockfile: {name!r}")
    if not isinstance(entry, Mapping):
        raise ValidationError(f"lock entry {name!r} must be an object")

    source = _required_lock_field(entry, name, "source")
    skill_id = _required_lock_field(entry, name, "skill_id")
    revision = _required_lock_field(entry, name, "revision")
    upstream_path = _required_lock_field(entry, name, "upstream_path")
    files = _required_lock_field(entry, name, "files")
    digest = _required_lock_field(entry, name, "tree_digest")
    license_report = _required_lock_field(entry, name, "license")
    state = _required_lock_field(entry, name, "state")

    if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
        raise ValidationError(f"lock entry {name!r} has invalid source")
    if not isinstance(skill_id, str):
        raise ValidationError(f"lock entry {name!r} has invalid skill_id")
    validate_relative_path(skill_id, label=f"lock entry {name!r} skill_id")
    if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
        raise ValidationError(f"lock entry {name!r} has invalid revision")
    if not isinstance(upstream_path, str):
        raise ValidationError(f"lock entry {name!r} has invalid upstream_path")
    if upstream_path != ".":
        validate_relative_path(upstream_path, label=f"lock entry {name!r} upstream_path")
    if not isinstance(files, Mapping):
        raise ValidationError(f"lock entry {name!r} has invalid files mapping")
    _validate_lock_files(name, files)
    if "SKILL.md" not in files:
        raise ValidationError(f"lock entry {name!r} has no SKILL.md file record")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValidationError(f"lock entry {name!r} has invalid tree_digest")
    if tree_digest(files) != digest:
        raise ValidationError(f"lock entry {name!r} tree_digest does not match files")
    if not isinstance(license_report, Mapping):
        raise ValidationError(f"lock entry {name!r} has invalid license evidence")
    if state != "clean":
        raise ValidationError(f"lock entry {name!r} has invalid state")
    imported_at = entry.get("imported_at")
    if imported_at is not None and (
        not isinstance(imported_at, str) or not imported_at
    ):
        raise ValidationError(f"lock entry {name!r} has invalid imported_at")


def _validate_lock_files(name: str, files: Mapping[Any, Any]) -> None:
    for relative, record in files.items():
        if not isinstance(relative, str):
            raise ValidationError(f"lock entry {name!r} has a non-string file path")
        validate_relative_path(relative, label=f"lock entry {name!r} file path")
        if not isinstance(record, Mapping):
            raise ValidationError(
                f"lock entry {name!r} file {relative!r} must be an object"
            )
        missing = [
            field for field in ("sha256", "bytes", "executable", "upstream_mode")
            if field not in record
        ]
        if missing:
            raise ValidationError(
                f"lock entry {name!r} file {relative!r} is missing required field: {missing[0]}"
            )
        if not isinstance(record["sha256"], str) or not re.fullmatch(
            r"[0-9a-f]{64}", record["sha256"]
        ):
            raise ValidationError(
                f"lock entry {name!r} file {relative!r} has invalid sha256"
            )
        if (
            not isinstance(record["bytes"], int)
            or isinstance(record["bytes"], bool)
            or record["bytes"] < 0
        ):
            raise ValidationError(
                f"lock entry {name!r} file {relative!r} has invalid byte count"
            )
        if not isinstance(record["executable"], bool):
            raise ValidationError(
                f"lock entry {name!r} file {relative!r} has invalid executable flag"
            )
        if not isinstance(record["upstream_mode"], str):
            raise ValidationError(
                f"lock entry {name!r} file {relative!r} has invalid upstream_mode"
            )


def _plugin_skill_names(repo_root: Path) -> dict[str, str]:
    owners: dict[str, str] = {}
    for skill_md in sorted(repo_root.glob("plugins/*/skills/*/SKILL.md")):
        try:
            frontmatter, _body, errors = parse_frontmatter(skill_md.read_bytes())
        except (OSError, ValidationError):
            continue
        relative = str(skill_md.relative_to(repo_root))
        name = frontmatter.get("name")
        if isinstance(name, str) and name and not errors:
            owners[name] = relative
        owners.setdefault(skill_md.parent.name, relative)
    return owners


def _read_actual_files(
    destination: Path, issues: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for file_path in sorted(destination.rglob("*")):
        if file_path.is_symlink():
            issues.append({
                "kind": "unsupported-local-symlink",
                "path": str(file_path.relative_to(destination)),
            })
            continue
        if file_path.is_file():
            relative = file_path.relative_to(destination).as_posix()
            data = file_path.read_bytes()
            records[relative] = {
                "sha256": sha256_bytes(data),
                "bytes": len(data),
                "executable": bool(file_path.stat().st_mode & stat.S_IXUSR),
            }
    return records


def _compare_files(
    actual: Mapping[str, Mapping[str, Any]], expected: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for relative, expected_record in expected.items():
        actual_record = actual.get(relative)
        if actual_record is None:
            issues.append({"kind": "missing-file", "path": relative})
            continue
        if actual_record["sha256"] != expected_record.get("sha256"):
            issues.append({
                "kind": "hash-drift", "path": relative,
                "expected": expected_record.get("sha256"), "actual": actual_record["sha256"],
            })
        if bool(actual_record["executable"]) != bool(expected_record.get("executable")):
            issues.append({"kind": "mode-drift", "path": relative})
    issues.extend(
        {"kind": "extra-file", "path": relative}
        for relative in sorted(set(actual) - set(expected))
    )
    return issues


def _status_entry(store: Path, skill_name: str, entry: Mapping[str, Any]) -> dict[str, Any]:
    destination = store / skill_name
    issues: list[dict[str, Any]] = []
    if destination.is_symlink() or not destination.is_dir():
        issues.append({"kind": "missing", "path": str(destination)})
    else:
        actual = _read_actual_files(destination, issues)
        expected = entry.get("files", {})
        if not isinstance(expected, Mapping):
            raise ValidationError(f"lock entry has invalid files mapping: {skill_name}")
        issues.extend(_compare_files(actual, expected))
        if tree_digest(actual) != entry.get("tree_digest") and not issues:
            issues.append({
                "kind": "tree-digest-drift",
                "expected": entry.get("tree_digest"), "actual": tree_digest(actual),
            })
    return {
        "name": skill_name, "source": entry.get("source"),
        "revision": entry.get("revision"),
        "state": "clean" if not issues else "drifted", "issues": issues,
    }


def _untracked_entries(store: Path, recorded: set[str]) -> list[dict[str, Any]]:
    if not store.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for child in sorted(store.iterdir()):
        if child.name.startswith(".") or not child.is_dir():
            continue
        if (child / "SKILL.md").is_file() and child.name not in recorded:
            results.append({
                "name": child.name, "source": None, "revision": None, "state": "untracked",
                "issues": [{"kind": "missing-lock-entry", "path": str(child)}],
            })
    return results


def status_store(asha_home: Path, name: str | None = None) -> dict[str, Any]:
    store = asha_home / "skills"
    path = lock_path(asha_home)
    lock = load_lock(path)
    selected: Sequence[tuple[str, Mapping[str, Any]]]
    if name:
        if name not in lock["skills"]:
            raise ValidationError(f"imported skill is not recorded: {name}")
        selected = [(name, lock["skills"][name])]
    else:
        selected = sorted(lock["skills"].items())
    results = [_status_entry(store, skill_name, entry) for skill_name, entry in selected]
    if not name:
        results.extend(_untracked_entries(store, {skill_name for skill_name, _entry in selected}))
    clean = all(item["state"] == "clean" for item in results)
    return {
        "schema_version": 1, "store": str(store), "lockfile": str(path),
        "state": "clean" if clean else "drifted", "skills": results,
    }


def _lock_entry(inspection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": inspection["source"], "skill_id": inspection["skill_id"],
        "revision": inspection["revision"], "upstream_path": inspection["upstream_path"],
        "files": inspection["files"], "tree_digest": inspection["tree_digest"],
        "license": inspection["license"], "state": "clean",
    }


def _validate_proposal_owner(
    inspection: Mapping[str, Any], repo_root: Path
) -> str:
    if not inspection.get("importable"):
        blockers = ", ".join(str(item) for item in inspection.get("import_blockers", []))
        raise ValidationError(f"candidate is not portable: {blockers}")
    name = inspection.get("name")
    if not isinstance(name, str) or len(name) > 64 or not NAME_RE.fullmatch(name):
        raise ValidationError("candidate has no valid import name")
    if len(f"imported-{name}") > 64:
        raise ValidationError(
            "imported skill mount name exceeds Agent Skills 64-character limit: "
            f"imported-{name}"
        )
    owners = _plugin_skill_names(repo_root)
    if name in owners or f"imported-{name}" in owners:
        owner = owners.get(name) or owners[f"imported-{name}"]
        raise CollisionError(f"skill name {name!r} collides with bundled Asha skill {owner}")
    return name


def _existing_import(
    name: str, inspection: Mapping[str, Any], destination: Path, lock: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    existing = lock["skills"].get(name)
    if destination.exists() and not existing:
        raise CollisionError(f"destination exists without an imported lock entry: {destination}")
    if destination.is_symlink():
        raise CollisionError(f"destination must not be a symlink: {destination}")
    identity_fields = ("source", "skill_id", "upstream_path")
    if existing and any(
        existing.get(field) != inspection.get(field) for field in identity_fields
    ):
        owner = "/".join(
            str(existing.get(field) or "<unknown>") for field in identity_fields
        )
        raise CollisionError(
            f"imported name {name!r} is already owned by {owner}"
        )
    return existing


def _proposal_action(
    existing: Mapping[str, Any] | None, inspection: Mapping[str, Any], asha_home: Path, name: str
) -> tuple[str, dict[str, Any] | None, bool]:
    if not existing:
        return "create", None, False
    local = status_store(asha_home, name)["skills"][0]
    same_tree = (
        existing.get("tree_digest") == inspection["tree_digest"]
        and existing.get("revision") == inspection["revision"]
    )
    if local["state"] == "clean" and same_tree:
        return "noop", local, False
    if local["state"] != "clean":
        return "replace-drifted", local, True
    return "update-pinned-revision", local, True


def _proposed_lock(
    lock: Mapping[str, Any], name: str, existing: Mapping[str, Any] | None,
    entry: Mapping[str, Any], action: str,
) -> dict[str, Any]:
    proposed = json.loads(json.dumps(lock))
    if action != "noop":
        if existing:
            proposed.setdefault("history", {}).setdefault(name, []).append(existing)
        proposed["skills"][name] = entry
    return proposed


def _proposed_writes(
    inspection: Mapping[str, Any], destination: Path, path: Path,
    proposed_lock: Mapping[str, Any], action: str,
) -> list[dict[str, Any]]:
    if action == "noop":
        return []
    writes = [
        {
            "path": str(destination / relative), "sha256": record["sha256"],
            "bytes": record["bytes"], "executable": record["executable"],
        }
        for relative, record in sorted(inspection["files"].items())
    ]
    lock_data = json_bytes(proposed_lock)
    writes.append({
        "path": str(path), "sha256": sha256_bytes(lock_data),
        "bytes": len(lock_data), "executable": False,
    })
    return writes


def build_import_proposal(
    inspection: Mapping[str, Any], asha_home: Path, repo_root: Path
) -> dict[str, Any]:
    name = _validate_proposal_owner(inspection, repo_root)
    destination = asha_home / "skills" / name
    path = lock_path(asha_home)
    lock = load_lock(path)
    before_digest = sha256_bytes(json_bytes(lock))
    existing = _existing_import(name, inspection, destination, lock)
    action, local_state, replace = _proposal_action(existing, inspection, asha_home, name)
    proposed_lock = _proposed_lock(
        lock, name, existing, _lock_entry(inspection), action
    )
    return {
        "schema_version": 1, "dry_run": True, "approved": False, "action": action,
        "name": name, "destination": str(destination), "lockfile": str(path),
        "source": inspection["source"], "revision": inspection["revision"],
        "tree_digest": inspection["tree_digest"], "local_state": local_state,
        "requires_replace_approval": replace,
        "writes": _proposed_writes(inspection, destination, path, proposed_lock, action),
        "_lock_document": proposed_lock, "_lock_before_sha256": before_digest,
        "_inspection": inspection,
    }


def proposal_report(proposal: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in proposal.items() if not key.startswith("_")}


def _check_approval(proposal: Mapping[str, Any], approve: bool, replace: bool) -> None:
    if not approve:
        raise ApprovalError("refusing to write without --approve")
    if proposal["requires_replace_approval"] and not replace:
        raise ApprovalError(
            "existing imported content would be replaced; inspect drift and pass --replace explicitly"
        )


def _noop_result(proposal: Mapping[str, Any]) -> dict[str, Any]:
    home = Path(proposal["destination"]).parent.parent
    if status_store(home, proposal["name"])["state"] != "clean":
        raise ApprovalError("import drifted after the proposal; inspect and propose again")
    result = proposal_report(proposal)
    result.update({"dry_run": False, "approved": True, "written": False})
    return result


def _check_write_state(proposal: Mapping[str, Any], destination: Path, path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValidationError(f"lockfile must be a regular file: {path}")
    if destination.is_symlink():
        raise CollisionError(f"destination must not be a symlink: {destination}")
    if proposal["action"] == "create" and destination.exists():
        raise CollisionError(f"destination appeared after the proposal: {destination}")
    expected = proposal["_lock_before_sha256"]
    current = sha256_bytes(json_bytes(load_lock(path)))
    if current != expected:
        raise CollisionError("imported lockfile changed after the proposal; propose again")


@contextmanager
def _writer_lock(store: Path):
    lock = store / ".imported.lock.write.lock"
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock, flags, 0o600)
    except OSError as exc:
        raise ValidationError(f"cannot open imported skill writer lock: {lock}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError(f"imported skill writer lock must be a regular file: {lock}")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _stage_files(stage: Path, payloads: Sequence[Mapping[str, Any]]) -> None:
    for item in payloads:
        relative = validate_relative_path(item["path"], label="write path")
        output = stage / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(item["data"])
        output.chmod(0o755 if item["executable"] else 0o644)


def _staged_records(stage: Path, payloads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        item["path"]: {
            "sha256": sha256_bytes((stage / item["path"]).read_bytes()),
            "bytes": (stage / item["path"]).stat().st_size,
            "executable": bool((stage / item["path"]).stat().st_mode & stat.S_IXUSR),
        }
        for item in payloads
    }


def _backup_destination(destination: Path, name: str) -> Path | None:
    if not destination.exists():
        return None
    backup_root = destination.parent / ".find-skills-backups"
    if backup_root.is_symlink():
        raise ValidationError(f"imported skill backup root must not be a symlink: {backup_root}")
    backup_root.mkdir(exist_ok=True)
    suffix = f"{utc_now().replace(':', '')}-{uuid.uuid4().hex[:8]}"
    backup = backup_root / f"{name}-{suffix}"
    destination.rename(backup)
    return backup


def _write_lock(path: Path, document: Mapping[str, Any]) -> None:
    data = json_bytes(document)
    fd, temp_name = tempfile.mkstemp(prefix=".imported.lock.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _rollback(stage: Path, destination: Path, backup: Path | None, installed: bool) -> None:
    if installed and destination.exists() and not stage.exists():
        destination.rename(stage)
    if stage.exists():
        shutil.rmtree(stage)
    if backup and backup.exists() and not destination.exists():
        backup.rename(destination)


def write_import(proposal: dict[str, Any], *, approve: bool, replace: bool) -> dict[str, Any]:
    _check_approval(proposal, approve, replace)
    if proposal["action"] == "noop":
        return _noop_result(proposal)
    inspection = proposal["_inspection"]
    destination = Path(proposal["destination"])
    store, path = destination.parent, Path(proposal["lockfile"])
    store.mkdir(parents=True, exist_ok=True)
    with _writer_lock(store):
        _check_write_state(proposal, destination, path)
        stage = Path(tempfile.mkdtemp(prefix=f".find-skills-{proposal['name']}-", dir=store))
        backup: Path | None = None
        installed = False
        try:
            _stage_files(stage, inspection["_file_payloads"])
            if tree_digest(_staged_records(stage, inspection["_file_payloads"])) != proposal["tree_digest"]:
                raise ValidationError("staged tree digest differs from the inspected proposal")
            backup = _backup_destination(destination, proposal["name"])
            stage.rename(destination)
            installed = True
            _write_lock(path, proposal["_lock_document"])
        except Exception:
            _rollback(stage, destination, backup, installed)
            raise
    result = proposal_report(proposal)
    result.update({
        "dry_run": False, "approved": True, "written": True,
        "backup": str(backup) if backup else None,
    })
    return result
