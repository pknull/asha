"""SQLite creation journals sharing the established recovery/ownership rules."""
from __future__ import annotations

from contextlib import contextmanager
import copy
import json

from .database import ControlDatabase, DATABASE_NAME
from .model import canonical_uuid
from .record_registry import RecordRegistry
from .registry_guards import mutation_guard
from .store import StoreError, _directory_fd, _managed_start, _task_lock
from .transaction import CreationJournalStore, JournalError, MAX_JOURNAL_BYTES, validate_journal


class SQLiteCreationJournalStore(CreationJournalStore):
    def __init__(self, config):
        super().__init__(config)
        self.registry = RecordRegistry('creation-journals')
        self.lock_root = config.tasks_dir.parent / 'registry-locks' / 'journals'

    @staticmethod
    def _identity(task_id):
        try:
            return canonical_uuid(task_id)
        except ValueError as exc:
            raise JournalError(str(exc)) from exc

    def path(self, task_id):
        self._identity(task_id)
        return self.config.tasks_dir.parent / DATABASE_NAME

    @contextmanager
    def _lock(self, task_id):
        self._identity(task_id)
        with mutation_guard(self.config), _directory_fd(self.lock_root, create=True,
                managed_start=_managed_start(self.lock_root, ('control', 'registry-locks', 'journals'))) as fd:
            with _task_lock(fd, task_id):
                yield

    def _row(self, c, task_id):
        row = self.registry.read(c, task_id)
        if row is None:
            return None
        if len(row['raw']) > MAX_JOURNAL_BYTES:
            raise JournalError(f'creation journal exceeds {MAX_JOURNAL_BYTES} bytes')
        row['value'] = validate_journal(row['value'], config=self.config, sqlite_artifacts=True)
        if row['value']['task_id'] != task_id:
            raise JournalError('creation journal task ID does not match its key')
        return row

    def read(self, task_id):
        self._identity(task_id)
        try:
            with ControlDatabase(self.config) as db, db.transaction() as c:
                row = self._row(c, task_id)
                if row is None:
                    raise JournalError(f'creation journal not found: {task_id}')
                return row['value']
        except StoreError as exc:
            raise JournalError(str(exc)) from exc

    def save(self, value, *, expected_phase=None, expected_digest=None, allow_recovery_adoption=False):
        journal = copy.deepcopy(value)
        try:
            validate_journal(journal, config=self.config, sqlite_artifacts=True)
            raw = json.dumps(journal, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'
            if len(raw) > MAX_JOURNAL_BYTES:
                raise JournalError(f'creation journal exceeds {MAX_JOURNAL_BYTES} bytes')
            task_id = journal['task_id']
            with self._lock(task_id), ControlDatabase(self.config) as db, db.transaction(write=True) as c:
                current = self._row(c, task_id)
                self._validate_change(current['value'] if current else None, journal,
                    expected_phase=expected_phase, expected_digest=expected_digest,
                    allow_recovery_adoption=allow_recovery_adoption)
                self.registry.put(c, task_id, raw, expected_digest=current['digest'] if current else None,
                                  state=journal['phase'],
                                  updated_at=journal.get('recorded_at', journal.get('created_at', '')))
                return db.path
        except (StoreError, ValueError) as exc:
            raise JournalError(str(exc)) from exc
