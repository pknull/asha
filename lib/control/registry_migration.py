"""Offline staging of file registries; a staged snapshot never activates itself."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re

from .database import ControlDatabase, DATABASE_NAME
from .auxiliary_migration import DOMAINS as AUXILIARY_DOMAINS, AuxiliaryImport
from .initiative_migration import DOMAINS as INITIATIVE_DOMAINS, InitiativeImport
from .model import ModelError, canonical_uuid
from .record_registry import RecordRegistry
from .rooms import RoomStore
from .runtime import read_policy
from .session_store import process_live
from .stage_ledger import write_ledger
from .registry_tree import capture_tree, verify_tree
from .registry_snapshot import state_digest, backup_state_digest
from .store import (MAX_RECORD_BYTES, StoreError, TaskStore, _directory_fd,
                    _managed_start, _open_existing_file, _registry_lock)


_FILE_DOMAINS = ("tasks", "rooms")
STAGED_DOMAINS = _FILE_DOMAINS + INITIATIVE_DOMAINS + AUXILIARY_DOMAINS
_TASK_LOCK = re.compile(r"(?:task|source|repository)-[0-9a-f]{64}\.lock")


def _read_record(fd, name, limit):
    file_fd = _open_existing_file(fd, name, "migration source record")
    try:
        if os.fstat(file_fd).st_size > limit:
            raise StoreError("migration source record exceeds its byte limit")
        parts, remaining = [], limit + 1
        while remaining:
            chunk = os.read(file_fd, min(65536, remaining))
            if not chunk:
                break
            parts.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(parts)
        if len(raw) > limit:
            raise StoreError("migration source record exceeds its byte limit")
        return raw
    finally:
        os.close(file_fd)


def _quiescent(db):
    with db.transaction() as c:
        return _quiescent_connection(c)


def _quiescent_connection(c):
    if read_policy(c)["mode"] == "running":
        raise StoreError("pause or drain admission before registry migration")
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='managed_sessions'").fetchone():
        owners = c.execute("SELECT owner_pid,owner_identity FROM managed_sessions WHERE owner_pid IS NOT NULL").fetchall()
        providers = c.execute("SELECT provider_pid,provider_identity FROM session_turns WHERE provider_pid IS NOT NULL").fetchall()
        if any(process_live(pid, identity) for pid, identity in [*owners, *providers]):
            raise StoreError("stop managed owners and providers before registry migration")
    return c.execute("PRAGMA data_version").fetchone()[0]


def stage_registries(config, destination_home, *, after_import=None):
    """Copy quiescent registries into a separate, paused SQLite snapshot.

    This is the first set of registry adapters. The manifest explicitly records
    its domains; activation must additionally cover every required operational
    domain, revalidate the source, and fence older writers. None is implied here.
    """
    target = Path(destination_home)
    source = config.asha_home
    if (not target.is_absolute() or target != target.resolve()
            or target.is_relative_to(source) or source.is_relative_to(target)):
        raise StoreError("migration destination must be canonical and separate from the source root")
    if not (config.tasks_dir.parent / DATABASE_NAME).exists():
        raise StoreError("pause admission before staging registry migration")
    from .registry_backend import selected_backend
    if selected_backend(config) != "files":
        raise StoreError("Control registries already use SQLite; legacy files cannot be imported again")
    stage_config = replace(config, asha_home=target, tasks_dir=target / "state/control/tasks")
    task_store, room_store = TaskStore(config), RoomStore(config)
    with ControlDatabase(config) as original, ExitStack() as stack:
        source_version = _quiescent(original)
        directories, source_names = {}, {}
        for domain in _FILE_DOMAINS:
            path = config.tasks_dir.parent / domain
            fd = stack.enter_context(_directory_fd(path, create=False, managed_start=_managed_start(path, ("control", domain))))
            if fd is not None:
                stack.enter_context(_registry_lock(fd))
            directories[domain] = fd
            source_names[domain] = set(os.listdir(fd)) if fd is not None else set()
        initiatives = InitiativeImport(config, stack)
        auxiliary = AuxiliaryImport(config, stack)
        source_tree = capture_tree(config)
        with original.transaction() as source_connection:
            source_database_state = state_digest(source_connection)
        with _directory_fd(target, create=True, managed_start=max(0, len(target.parts) - 2)) as home_fd:
            if os.listdir(home_fd):
                raise StoreError("registry staging requires an empty destination root")
            snapshot_name = "source-database.sqlite3"
            original.backup(target / snapshot_name)
            backup_fd = _open_existing_file(home_fd, snapshot_name, "migration source database snapshot")
            with os.fdopen(backup_fd, "rb") as backup:
                source_backup_digest = hashlib.file_digest(backup, "sha256").hexdigest()
                if backup_state_digest(backup.fileno()) != source_database_state:
                    raise StoreError("source database changed before the retained backup completed")
        # Restore publishes only an integrity-checked database with admission
        # durably paused. A later import rollback cannot undo that pause.
        ControlDatabase.restore(stage_config, target / snapshot_name)
        with ControlDatabase(stage_config) as staged, staged.transaction(write=True) as c:
            attempt = {"state": "incomplete", "source_root": str(source),
                       "source_database_snapshot": snapshot_name,
                       "source_database_digest": source_backup_digest}
            RecordRegistry("registry-migration", scope="control").put(c, "attempt",
                json.dumps(attempt, sort_keys=True).encode(), state="incomplete")
        entries, counts, names = [], {}, set()
        open_rooms = []
        with ControlDatabase(stage_config) as staged, staged.transaction(write=True) as c:
            from .registry_guards import drop_guards
            drop_guards(c)
            c.execute("UPDATE control_runtime SET mode='paused',revision=revision+1,reason='Offline registry staging; not activated' WHERE singleton=1")
            for domain, fd in directories.items():
                registry = RecordRegistry(domain)
                counts[domain] = 0
                if c.execute("SELECT 1 FROM records WHERE domain=? LIMIT 1", (domain,)).fetchone():
                    raise StoreError("staging source already contains SQLite registry rows; reconcile domain authority before import")
                if fd is None:
                    continue
                for name in sorted(source_names[domain]):
                    if domain == "tasks" and _TASK_LOCK.fullmatch(name):
                        lock_fd = _open_existing_file(fd, name, "migration source lock")
                        os.close(lock_fd)
                        continue
                    try:
                        key = canonical_uuid(name[:-5]) if name.endswith(".json") else None
                        if key is None:
                            raise ModelError("unexpected file")
                    except ModelError as exc:
                        raise StoreError(f"invalid {domain} migration record name: {name}") from exc
                    raw = _read_record(fd, name, MAX_RECORD_BYTES if domain == "tasks" else 64 * 1024)
                    value = RecordRegistry.decode(raw)
                    if domain == "tasks":
                        value = task_store._validated_value(value, key)
                        if any(process_live(run["pid"], run["process_start_identity"]) for run in value["runs"]):
                            raise StoreError("stop live task runs before staging registry migration")
                    else:
                        value = room_store._validate(value)
                        if value["room_id"] != key:
                            raise StoreError("room record name and identity differ")
                        folded = value["name"].casefold()
                        if folded in names:
                            raise StoreError("duplicate Room name in migration source")
                        names.add(folded)
                        if value["lifecycle"] == "open":
                            open_rooms.append(key)
                    digest = registry.put(c, key, raw, state=value["lifecycle"], updated_at=value["updated_at"])
                    if registry.read(c, key)["raw"] != raw:
                        raise StoreError("imported record differs from source bytes")
                    entries.append({"domain": domain, "key": key, "digest": digest, "bytes": len(raw)})
                    counts[domain] += 1
                    if after_import is not None:
                        after_import(domain, key)
            initiative_entries, initiative_counts, artifacts = initiatives.import_into(c, stage_config, after_import)
            entries.extend(initiative_entries)
            counts.update(initiative_counts)
            auxiliary_entries, auxiliary_counts, auxiliary_artifacts = auxiliary.import_into(c, stage_config, after_import)
            entries.extend(auxiliary_entries)
            counts.update(auxiliary_counts)
            artifacts.extend(auxiliary_artifacts)
            entries.sort(key=lambda row: (row["domain"], row.get("scope", "registry"), row["key"]))
            # A second pinned read detects non-cooperating source mutation. No
            # incomplete stage can acquire a completion manifest.
            for domain, fd in directories.items():
                path = config.tasks_dir.parent / domain
                if fd is None:
                    if os.path.lexists(path):
                        raise StoreError("migration source registry appeared during staging")
                else:
                    try:
                        current = os.stat(path, follow_symlinks=False)
                    except OSError as exc:
                        raise StoreError("migration source registry disappeared during staging") from exc
                    pinned = os.fstat(fd)
                    if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                        raise StoreError("migration source registry changed identity during staging")
                if fd is not None and set(os.listdir(fd)) != source_names[domain]:
                    raise StoreError("migration source registry membership changed during staging")
                actual = c.execute("SELECT count(*) FROM records WHERE domain=?", (domain,)).fetchone()[0]
                if actual != counts[domain]:
                    raise StoreError("staged registry row count differs from the source")
            for item in entries:
                if item["domain"] not in _FILE_DOMAINS:
                    continue
                raw = _read_record(directories[item["domain"]], item["key"] + ".json", MAX_RECORD_BYTES)
                if hashlib.sha256(raw).hexdigest() != item["digest"]:
                    raise StoreError("migration source changed during staging")
            initiatives.verify_source()
            auxiliary.verify_source()
            verify_tree(config, source_tree)
            if _quiescent(original) != source_version:
                raise StoreError("source database changed during registry staging")
            manifest = {"contract": "asha.registry-stage.v3", "state": "staged",
                        "source_root": str(source), "domains": list(STAGED_DOMAINS), "counts": counts,
                        "source_database_digest": source_backup_digest, "records": write_ledger(c, "records", entries),
                        "source_database_state": source_database_state,
                        "source_database_snapshot": snapshot_name, "artifacts": write_ledger(c, "artifacts", artifacts),
                        "source_tree": write_ledger(c, "source", source_tree),
                        "external_rooms": {"open_room_count": len(open_rooms), "liveness": "not-probed",
                                           "activation_requires_revalidation": True}}
            raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
            RecordRegistry("registry-migration", scope="control").put(c, "stage", raw, state="staged")
            attempt_registry = RecordRegistry("registry-migration", scope="control")
            previous = attempt_registry.read(c, "attempt")
            attempt["state"] = "staged"
            attempt_registry.put(c, "attempt", json.dumps(attempt, sort_keys=True).encode(),
                                 expected_digest=previous["digest"], state="staged")
        return manifest
