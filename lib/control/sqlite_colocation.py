"""SQLite repository initialization intents with exact filesystem reauthentication."""
from __future__ import annotations

import json
from pathlib import Path

from .database import ControlDatabase, DATABASE_NAME
from .jj import ColocationIntentStore, ColocationIntentAssessment, JjError, MAX_COLOCATION_INTENT_BYTES
from .record_registry import RecordRegistry
from .registry_guards import mutation_guard
from .store import StoreError


class SQLiteColocationIntentStore(ColocationIntentStore):
    def __init__(self, config):
        super().__init__(config)
        self.registry = RecordRegistry('repository-inits')

    def path(self, root):
        return self._config.tasks_dir.parent / DATABASE_NAME

    def _row(self, c, root):
        row = self.registry.read(c, self._key(root))
        if row is not None:
            if len(row['raw']) > MAX_COLOCATION_INTENT_BYTES:
                raise JjError('colocation intent exceeds its bounded size')
            row['value'] = self._decode(row['raw'])
            if row['value']['root'] != str(root):
                raise JjError('colocation intent root does not match its key')
        return row

    def read(self, root):
        root = Path(root)
        try:
            with ControlDatabase(self._config) as db, db.transaction() as c:
                row = self._row(c, root)
                return self._validate_current_binding(root, row['value']) if row else None
        except (StoreError, OSError) as exc:
            raise JjError(str(exc)) from exc

    def classify(self, root):
        root = Path(root)
        try:
            with ControlDatabase(self._config) as db, db.transaction() as c:
                row = self._row(c, root)
                return self._assessment(root, row['raw'], row['value']) if row else ColocationIntentAssessment('missing')
        except (StoreError, OSError) as exc:
            raise JjError(str(exc)) from exc

    def _write(self, root, state, *, expected):
        # begin/mark_verified acquire the inherited source mutation lock before
        # this short writer transaction; filesystem probes run no child commands.
        value = {**self._binding(root, verified=state == 'verified'), 'state': state}
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode() + b'\n'
        if len(raw) > MAX_COLOCATION_INTENT_BYTES:
            raise JjError('colocation intent exceeds its bounded size')
        try:
            with mutation_guard(self._config), ControlDatabase(self._config) as db, db.transaction(write=True) as c:
                row = self._row(c, root)
                current = self._validate_current_binding(root, row['value']) if row else None
                if (current['state'] if current else None) != expected:
                    raise JjError('colocation intent changed; inspect retained repository state')
                self.registry.put(c, self._key(root), raw,
                                  expected_digest=row['digest'] if row else None, state=state)
        except (StoreError, OSError) as exc:
            raise JjError(str(exc)) from exc

    def _reauthenticate_verified_candidate_locked(self, root, expected):
        root = Path(root)
        raw = self._reauthentication_bytes(expected)
        try:
            with mutation_guard(self._config), ControlDatabase(self._config) as db, db.transaction(write=True) as c:
                row = self._row(c, root)
                current = (row['raw'], row['value']) if row else None
                self._validate_reauthentication(root, current, expected)
                self.registry.put(c, self._key(root), raw, expected_digest=row['digest'], state='verified')
                # The writer lock protects the record; recheck filesystem facts
                # immediately before commit as the original file writer does.
                self._validate_reauthentication(root, current, expected)
        except (StoreError, OSError) as exc:
            raise JjError(str(exc)) from exc
