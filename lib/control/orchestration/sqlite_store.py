"""SQLite initiative records with the established orchestration authority rules.

Operational JSON is held in SQLite. Large immutable assignment/output artifacts
keep their descriptor-checked file paths. Lifecycle locks do not hold a SQLite
writer while a caller launches an external command.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import re
import os

from ..database import ControlDatabase
from ..record_registry import RecordRegistry
from ..registry_guards import mutation_guard
from ..store import _directory_fd, _managed_start, _task_lock
from . import model
from .sqlite_views import SQLiteInitiativeViews
from .artifact_files import ArtifactFiles
from .store import (InitiativeStore, StoreError, MAX_RECORD_BYTES, _canonical_bytes,
                    _LAYOUT_DIRECTORIES, _PRESENTATION_EVENT_FILENAME, _open_directory)


class SQLiteInitiativeStore(SQLiteInitiativeViews, InitiativeStore):
    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.database_config = config.control
        self.lock_root = config.control.tasks_dir.parent / "registry-locks" / "initiatives"
        self.artifacts = ArtifactFiles(config.control)
        self.retained_artifacts = ArtifactFiles(config.control, retained=True)

    @staticmethod
    def _uuid(value, label="record identity"):
        try:
            return model.canonical_uuid(value, label)
        except model.ModelError as exc:
            raise StoreError(str(exc)) from exc

    @staticmethod
    def _registry(initiative_id, directory):
        SQLiteInitiativeStore._uuid(initiative_id, "initiative_id")
        if directory == "initiative":
            return RecordRegistry("initiatives")
        if directory not in _LAYOUT_DIRECTORIES or directory in {"assignments", "outputs", "locks"}:
            raise StoreError("unknown SQLite initiative record class")
        return RecordRegistry("initiative." + directory, scope=initiative_id)

    @contextmanager
    def _write_lock(self, initiative_id, *, identity="initiative"):
        self._uuid(initiative_id, "initiative_id")
        with mutation_guard(self.database_config), _directory_fd(self.lock_root, create=True,
                managed_start=_managed_start(self.lock_root, ("control", "registry-locks", "initiatives"))) as fd:
            with _task_lock(fd, initiative_id + "-" + identity, self._lock_wait_hook):
                yield

    @contextmanager
    def transaction_lock(self, initiative_id):
        self.peek(initiative_id)
        with self._write_lock(initiative_id):
            self._sweep_current_artifacts(initiative_id)
            yield

    def _sweep_current_artifacts(self, initiative_id):
        # Callers hold the SQL initiative lifecycle lock. Read-only projections
        # never sweep, and the retained legacy tree is never modified here.
        for directory in ("assignments", "outputs"):
            with self.artifacts.directory(initiative_id, directory) as fd:
                if fd is not None:
                    self._sweep_write_residue(fd)

    @contextmanager
    def result_ingestion_lock(self, initiative_id, ingestion_id):
        self.peek(initiative_id)
        self._uuid(ingestion_id, "ingestion_id")
        with self._write_lock(initiative_id, identity="ingestion-" + ingestion_id):
            yield

    @contextmanager
    def _locked_fds(self, initiative_id, *, create=False):
        # Only inherited artifact methods use this file seam. A directory by
        # itself is never evidence that an initiative exists.
        with self.transaction_lock(initiative_id):
            root = self.artifacts.root
            with _directory_fd(root, create=True,
                    managed_start=_managed_start(root, ("control", "artifacts"))) as root_fd:
                initiative_fd = _open_directory(root_fd, initiative_id, create=True)
                try:
                    for name in ("assignments", "outputs", "evidence"):
                        child = _open_directory(initiative_fd, name, create=True)
                        os.close(child)
                    yield root_fd, initiative_fd
                finally:
                    os.close(initiative_fd)

    def _artifact(self, initiative_id, directory, identity, *, read=False):
        retained = self.retained_artifacts.inspect(initiative_id, directory, identity, read=read)
        current = self.artifacts.inspect(initiative_id, directory, identity, read=read)
        if retained is not None and current is not None:
            raise StoreError("artifact identity exists in both current and retained roots")
        return retained or current

    def assignment_path(self, initiative_id, attempt_id):
        artifact = self._artifact(initiative_id, "assignments", attempt_id)
        return artifact["path"] if artifact else self.artifacts.path(initiative_id, "assignments", attempt_id)

    def output_path(self, initiative_id, output_id):
        artifact = self._artifact(initiative_id, "outputs", output_id)
        return artifact["path"] if artifact else self.artifacts.path(initiative_id, "outputs", output_id)

    def write_assignment(self, initiative_id, attempt_id, content):
        with self.transaction_lock(initiative_id):
            artifact = self._artifact(initiative_id, "assignments", attempt_id, read=True)
            if artifact is not None:
                if not isinstance(content, bytes) or not content or artifact["content"] != content:
                    raise StoreError("retained assignment differs from the action reservation")
                try:
                    content.decode("utf-8")
                except UnicodeError as exc:
                    raise StoreError("assignment must be UTF-8") from exc
                return artifact["path"]
            return super().write_assignment(initiative_id, attempt_id, content)

    def _refuse_retained_output_write(self, initiative_id, output_id):
        self._artifact(initiative_id, "outputs", output_id)
        if self.retained_artifacts.inspect(initiative_id, "outputs", output_id) is not None:
            raise StoreError("retained output is immutable after registry migration")

    def save_output(self, initiative_id, output_id, content):
        with self.transaction_lock(initiative_id):
            self._refuse_retained_output_write(initiative_id, output_id)
            return super().save_output(initiative_id, output_id, content)

    def reserve_output(self, initiative_id, output_id):
        with self.transaction_lock(initiative_id):
            self._refuse_retained_output_write(initiative_id, output_id)
            return super().reserve_output(initiative_id, output_id)

    def finalize_reserved_output(self, initiative_id, output_id, content):
        with self.transaction_lock(initiative_id):
            self._refuse_retained_output_write(initiative_id, output_id)
            return super().finalize_reserved_output(initiative_id, output_id, content)

    def read_output(self, initiative_id, output_id):
        self.peek(initiative_id)
        artifact = self._artifact(initiative_id, "outputs", output_id, read=True)
        if artifact is None:
            raise StoreError(f"command output not found: {output_id}")
        return artifact["content"]

    def _output_evidence_exists(self, initiative_id, evidence_fd, output_id):
        # The explicit caller identity is authoritative even if an artifact
        # directory has a different visible path in a mount namespace.
        with ControlDatabase(self.database_config) as db, db.transaction() as c:
            self._head(c, initiative_id)
            return self._record(c, initiative_id, "evidence", output_id + ".json",
                                model.validate_evidence, required=False) is not None

    def _record(self, c, initiative_id, directory, name, validator, *, required=True):
        if not isinstance(name, str) or "/" in name or name in {"", ".", ".."}:
            raise StoreError("invalid initiative record key")
        row = self._registry(initiative_id, directory).read(c, name)
        if row is None:
            if required:
                raise StoreError(f"{directory} record not found: {name}")
            return None
        if len(row["raw"]) > MAX_RECORD_BYTES:
            raise StoreError("initiative record exceeds its byte limit")
        try:
            row["value"] = validator(row["value"])
        except (model.ModelError, TypeError) as exc:
            raise StoreError(f"invalid {directory} record {name}: {exc}") from exc
        if row["value"].get("initiative_id", initiative_id) != initiative_id:
            raise StoreError("record belongs to another initiative")
        return row

    def _head(self, c, initiative_id, *, required=True):
        return self._record(c, initiative_id, "initiative", initiative_id,
                            model.validate_initiative, required=required)

    def _put(self, c, initiative_id, directory, key, raw, value, *, previous=None):
        return self._registry(initiative_id, directory).put(c, key, raw,
            expected_digest=previous["digest"] if previous else None,
            state=value.get("state", value.get("status", "")),
            updated_at=value.get("updated_at", value.get("recorded_at", "")))

    def save_initiative(self, record, *, expected_digest=None):
        value, raw = _canonical_bytes(model.validate_initiative, record)
        iid = value["initiative_id"]
        with self._write_lock(iid), ControlDatabase(self.database_config) as db, db.transaction(write=True) as c:
            current = self._head(c, iid, required=False)
            old = current["value"] if current else None
            self._check_expected(old, expected_digest)
            self._validate_initiative_change(old, value)
            self._put(c, iid, "initiative", iid, raw, value, previous=current)
            return db.path

    def peek(self, initiative_id):
        with ControlDatabase(self.database_config) as db, db.transaction() as c:
            return self._head(c, initiative_id)["value"]

    def read_initiative(self, initiative_id):
        with self.transaction_lock(initiative_id):
            return self.peek(initiative_id)

    def _save_subrecord(self, initiative_id, directory, name, record, validator, *, immutable,
                       expected_digest=None, transition_machine=None, immutable_fields=(),
                       bind_once_fields=(), mutable_while_states=None, terminal_states=frozenset()):
        value, raw = _canonical_bytes(validator, record)
        if value.get("initiative_id", initiative_id) != initiative_id:
            raise StoreError("record initiative_id does not match destination initiative")
        with self.transaction_lock(initiative_id), ControlDatabase(self.database_config) as db, db.transaction(write=True) as c:
            current = self._record(c, initiative_id, directory, name, validator, required=False)
            if immutable:
                if expected_digest is not None:
                    raise StoreError("write-once records do not accept expected_digest")
                if current is not None:
                    raise StoreError(f"write-once record already exists: {name}")
            else:
                old = current["value"] if current else None
                self._check_expected(old, expected_digest)
                self._validate_subrecord_change(old, value, name=name,
                    transition_machine=transition_machine, immutable_fields=immutable_fields,
                    bind_once_fields=bind_once_fields, mutable_while_states=mutable_while_states,
                    terminal_states=terminal_states)
            self._put(c, initiative_id, directory, name, raw, value, previous=current)
            return db.path

    def _read_subrecord(self, initiative_id, directory, name, validator, *, identity_field=None, identity_value=None):
        with ControlDatabase(self.database_config) as db, db.transaction() as c:
            self._head(c, initiative_id)
            value = self._record(c, initiative_id, directory, name, validator)["value"]
            if identity_field is not None and value[identity_field] != identity_value:
                raise StoreError(f"{directory} record identity does not match its key")
            return value

    def _list_subrecords_snapshot(self, initiative_id, directory, validator, filename_pattern,
                                  identity_field, identity_parser=lambda v: v, *, problems=None):
        result, after = [], ""
        with ControlDatabase(self.database_config) as db:
            while True:
                with db.transaction() as c:
                    self._head(c, initiative_id)
                    keys = self._registry(initiative_id, directory).keys(c, after=after)
                    for key in keys:
                        try:
                            match = filename_pattern.fullmatch(key)
                            if match is None:
                                raise StoreError(f"invalid {directory} record key: {key}")
                            value = self._record(c, initiative_id, directory, key, validator)["value"]
                            if value[identity_field] != identity_parser(match.group(1)):
                                raise StoreError("record identity does not match its key")
                            result.append(value)
                        except (ValueError, KeyError) as exc:
                            if problems is None:
                                raise StoreError(f"initiative {initiative_id} {directory}/{key}: {exc}") from exc
                            problems.append({"directory": directory, "name": key,
                                "path": str(db.path), "reason": str(exc)})
                if len(keys) < 100:
                    return result
                after = keys[-1]

    def save_plan(self, initiative_id, record):
        value, _ = _canonical_bytes(model.validate_plan_record, record)
        if value["status"] != "proposed":
            raise StoreError("new plan status must be proposed")
        digest = model.plan_digest(value)
        if value["digest"] is not None and value["digest"] != digest:
            raise StoreError("plan digest does not match canonical plan bytes")
        value["digest"] = digest
        with self.transaction_lock(initiative_id):
            current = self.list_plans_snapshot(initiative_id)
            if [p["revision"] for p in current] != list(range(1, len(current) + 1)):
                raise StoreError("stored plan revisions contain a gap")
            if value["revision"] != len(current) + 1:
                raise StoreError(f"plan revision must be exactly {len(current) + 1}")
            return self._save_subrecord(initiative_id, "plans", f"{value['revision']:04d}.json",
                                        value, model.validate_plan_record, immutable=True)

    def read_plan_snapshot(self, initiative_id, revision):
        if type(revision) is not int or revision <= 0:
            raise StoreError("plan revision must be a positive integer")
        return self._read_subrecord(initiative_id, "plans", f"{revision:04d}.json",
            self._validate_stored_plan_observation, identity_field="revision", identity_value=revision)

    def message_snapshot(self, initiative_id, message_id, *, directory="messages"):
        self._uuid(message_id)
        if directory not in {"messages", "message-observations", "message-acks"}:
            raise StoreError("invalid message record class")
        validator = model.validate_message if directory == "messages" else model.validate_message_receipt
        with ControlDatabase(self.database_config) as db, db.transaction() as c:
            self._head(c, initiative_id)
            row = self._record(c, initiative_id, directory, message_id + ".json", validator, required=False)
            if row is None:
                return None
            value = row["value"]
            if value["message_id"] != message_id:
                raise StoreError("message record identity mismatch")
            if directory != "messages" and value["state"] != ("acknowledged" if directory == "message-acks" else "observed"):
                raise StoreError("message receipt class/state mismatch")
            return value

    def list_messages_snapshot(self, initiative_id):
        return self._list_subrecords_snapshot(initiative_id, "messages", model.validate_message,
            re.compile(r"([0-9a-f-]{36})\.json"), "message_id")

    def _events(self, c, initiative_id, *, after=0, tail=None):
        head = self._head(c, initiative_id)["value"]
        registry = self._registry(initiative_id, "events")
        count, first, last = c.execute(
            "SELECT count(*),min(substr(record_key,1,6)),max(substr(record_key,1,6)) "
            "FROM records WHERE domain='initiative.events' AND scope=?", (initiative_id,)).fetchone()
        if count != head["last_event_sequence"]:
            raise StoreError("event sequence disagrees with initiative snapshot")
        # The schema enforces unique six-digit positive sequences. Endpoints
        # and count therefore prove continuity without materializing all keys.
        if count and (first != "000001" or last != f"{count:06d}"):
            raise StoreError("event sequence contains a gap or duplicate")
        if after >= count or tail == 0:
            return []
        lower = f"{after + 1:06d}-"
        query = ("SELECT record_key FROM records WHERE domain=? AND scope=? AND record_key>=? "
                 "ORDER BY record_key" + (" DESC LIMIT ?" if tail is not None else ""))
        args = (registry.domain, registry.scope, lower)
        if tail is not None:
            args += (min(tail, count),)
        keys = [r[0] for r in c.execute(query, args)]
        if tail is not None:
            keys.reverse()
        selected = []
        for key in keys:
            match = _PRESENTATION_EVENT_FILENAME.fullmatch(key)
            if match is None:
                raise StoreError("invalid event key")
            selected.append((key, int(match.group(1)), match.group(2)))
        events = []
        for key, sequence, event_id in selected:
            value = self._record(c, initiative_id, "events", key, model.validate_event)["value"]
            if value["sequence"] != sequence or value["event_id"] != event_id:
                raise StoreError("event identity does not match its key")
            events.append(value)
        return events

    def append_event(self, initiative_id, event):
        with self.transaction_lock(initiative_id), ControlDatabase(self.database_config) as db, db.transaction(write=True) as c:
            self._append_event(c, initiative_id, event)
            return db.path

    def _append_event(self, c, initiative_id, event):
        """Shared event mutation for ordinary append and atomic managed intake."""
        value, raw = _canonical_bytes(model.validate_event, event)
        if value["initiative_id"] != initiative_id:
            raise StoreError("event initiative_id does not match destination initiative")
        current = self._head(c, initiative_id)
        self._events(c, initiative_id, tail=1)
        head = copy.deepcopy(current["value"])
        expected = head["last_event_sequence"] + 1
        if value["sequence"] != expected:
            raise StoreError(f"event sequence must be exactly {expected}")
        if self._time(value["recorded_at"]) < self._time(head["updated_at"]):
            raise StoreError("event recorded_at must not precede initiative updated_at")
        head.update(last_event_sequence=expected, state_revision=head["state_revision"] + 1,
                    updated_at=value["recorded_at"])
        head, head_raw = _canonical_bytes(model.validate_initiative, head)
        self._put(c, initiative_id, "events", f"{expected:06d}-{value['event_id']}.json", raw, value)
        self._put(c, initiative_id, "initiative", initiative_id, head_raw, head, previous=current)

    def list_events_snapshot(self, initiative_id, *, after=0, tail=None):
        if type(after) is not int or after < 0 or (tail is not None and (type(tail) is not int or tail < 0)):
            raise StoreError("invalid event query bounds")
        with ControlDatabase(self.database_config) as db, db.transaction() as c:
            return self._events(c, initiative_id, after=after, tail=tail)

    def list_events(self, initiative_id):
        return self.list_events_snapshot(initiative_id)

    def verify_events(self, initiative_id):
        return self.list_events_snapshot(initiative_id)

    def list_initiatives(self):
        self.skipped = []
        result, after = [], ""
        with ControlDatabase(self.database_config) as db:
            while True:
                with db.transaction() as c:
                    keys = RecordRegistry("initiatives").keys(c, after=after)
                    for key in keys:
                        try:
                            result.append(self._head(c, key)["value"])
                        except (ValueError, KeyError) as exc:
                            self.skipped.append({"name": key, "reason": str(exc)})
                if len(keys) < 100:
                    return result
                after = keys[-1]

    list = list_initiatives
    save = save_initiative
    read = read_initiative
