"""SQLite task registry using the existing task transition and identity rules."""
from __future__ import annotations

import copy
import hmac
import re
from contextlib import contextmanager

from .database import ControlDatabase
from .model import ModelError, canonical_uuid
from .record_registry import RecordRegistry
from .registry_guards import mutation_guard
from .store import (MAX_RECORD_BYTES, StoreError, TaskStore, _canonical_task_bytes,
                    _coordinated_transaction_lock, task_digest)


class SQLiteTaskStore(TaskStore):
    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.records = RecordRegistry("tasks")
        self.lock_root = config.tasks_dir.parent / "registry-locks" / "tasks"

    @contextmanager
    def transaction_lock(self, task_id):
        try:
            task_id = canonical_uuid(task_id)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        with mutation_guard(self.config), _coordinated_transaction_lock(self.config, "task", task_id, registry_root=self.lock_root):
            yield

    def _record(self, c, task_id):
        row = self.records.read(c, task_id)
        if row is None:
            return None
        if len(row["raw"]) > MAX_RECORD_BYTES:
            raise StoreError(f"task record exceeds {MAX_RECORD_BYTES} bytes")
        row["value"] = self._validated_value(row["value"], task_id)
        return row

    def save(self, task, *, expected_digest=None, recovery_adoption=None):
        task = copy.deepcopy(task)
        if not isinstance(task, dict):
            raise StoreError("task record must be an object")
        try:
            task_id = canonical_uuid(task.get("task_id"))
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        value = self._validated_value(task, task_id)
        raw = _canonical_task_bytes(value) + b"\n"
        if len(raw) > MAX_RECORD_BYTES:
            raise StoreError(f"task record exceeds {MAX_RECORD_BYTES} bytes")
        with self.transaction_lock(task_id), ControlDatabase(self.config) as db:
            with db.transaction(write=True) as c:
                current = self._record(c, task_id)
                if current is None:
                    if expected_digest is not None:
                        raise StoreError("expected digest supplied for a new task record")
                else:
                    if expected_digest is None:
                        raise StoreError("expected digest is required to update an existing task record")
                    if not isinstance(expected_digest, str) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
                        raise StoreError("expected digest must be 64 lowercase hexadecimal characters")
                    if not hmac.compare_digest(task_digest(current["value"]), expected_digest):
                        raise StoreError("task digest mismatch; reload the current record")
                    self._validate_update(current["value"], value, recovery_adoption=recovery_adoption)
                self.records.put(c, task_id, raw, expected_digest=current["digest"] if current else None,
                                 state=value["lifecycle"], updated_at=value["updated_at"])
            return db.path

    def peek(self, task_id):
        try:
            task_id = canonical_uuid(task_id)
        except ModelError as exc:
            raise StoreError(str(exc)) from exc
        with ControlDatabase(self.config) as db, db.transaction() as c:
            row = self._record(c, task_id)
            if row is None:
                raise StoreError(f"task not found: {task_id}")
            return row["value"]

    def read(self, task_id):
        # Negative reads and presentations do not allocate lock files.
        self.peek(task_id)
        with self.transaction_lock(task_id):
            return self.peek(task_id)

    def list(self):
        self.skipped = []
        result, after = [], ""
        with ControlDatabase(self.config) as db:
            while True:
                with db.transaction() as c:
                    keys = self.records.keys(c, after=after)
                    for key in keys:
                        try:
                            canonical_uuid(key)
                            result.append(self._record(c, key)["value"])
                        except (StoreError, ModelError) as exc:
                            self.skipped.append({"name": key, "reason": str(exc)})
                if len(keys) < 100:
                    return result
                after = keys[-1]

    def bounded_snapshots(self, budget):
        return self._bounded_snapshots(budget)

    def bounded_active_snapshots(self, budget):
        return [row for row in self._bounded_snapshots(budget, states=("running", "creating", "failed"))
                if self._active_snapshot_candidate(row)]

    def _bounded_snapshots(self, budget, *, states=None):
        result = []
        try:
            with ControlDatabase(self.config) as db, db.transaction() as c:
                cap = min(1000, max(1, budget.limit - budget.scanned))
                keys = (self.records.keys(c, limit=cap) if states is None
                        else self.records.active_root_keys(c, states, limit=cap))
                for key in keys:
                    if not budget.ready():
                        break
                    budget.scanned += 1
                    try:
                        canonical_uuid(key)
                        value = self._record(c, key)["value"]
                        if states is not None and value["lifecycle"] not in states:
                            raise StoreError("task lifecycle disagrees with its activity index")
                        result.append(value)
                    except (StoreError, ModelError):
                        budget.unavailable += 1
                if len(keys) == cap:
                    budget.truncated = True
        except (OSError, StoreError):
            budget.unavailable += 1
        return result
