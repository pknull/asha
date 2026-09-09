"""SQLite standing-authority and prune registries with retained identities."""
from __future__ import annotations

import json

from .database import ControlDatabase, DATABASE_NAME
from .model import canonical_uuid
from .orchestration.authority import AuthorityError, _now as authority_now, validate_authority
from .prune import PruneRecordStore, PruneError, PRUNE_RECORD_CONTRACT, _MAX_MARKER_BYTES, _now as prune_now
from .record_registry import RecordRegistry
from .registry_guards import mutation_guard


def _raw(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'


class SQLiteAuthorityStore:
    def __init__(self, config):
        self.config = config.control
        self.registry = RecordRegistry('authorities')

    def _row(self, c, identity):
        canonical_uuid(identity)
        row = self.registry.read(c, identity)
        if row is None:
            raise AuthorityError(f'no authority {identity}')
        row['value'] = validate_authority(row['value'])
        if row['value']['authority_id'] != identity:
            raise AuthorityError('authority identity does not match its key')
        return row

    def create(self, record):
        try:
            value = validate_authority(record)
            if value['revoked_at'] is not None:
                raise AuthorityError('new authority must not be revoked')
            with mutation_guard(self.config), ControlDatabase(self.config) as db, db.transaction(write=True) as c:
                self.registry.put(c, value['authority_id'], _raw(value), state='active', updated_at=value['created_at'])
            return value
        except ValueError as exc:
            raise AuthorityError(str(exc)) from exc

    def list(self, *, include_revoked=False):
        result, after = [], ''
        try:
            with ControlDatabase(self.config) as db, db.transaction() as c:
                while True:
                    keys = self.registry.keys(c, after=after)
                    for key in keys:
                        value = self._row(c, key)['value']
                        if include_revoked or value['revoked_at'] is None:
                            result.append(value)
                    if len(keys) < 100:
                        return result
                    after = keys[-1]
        except ValueError as exc:
            raise AuthorityError(str(exc)) from exc

    def revoke(self, identity):
        try:
            with mutation_guard(self.config), ControlDatabase(self.config) as db, db.transaction(write=True) as c:
                row = self._row(c, identity)
                if row['value']['revoked_at'] is not None:
                    return row['value']
                value = validate_authority({**row['value'], 'revoked_at': authority_now()})
                self.registry.put(c, identity, _raw(value), expected_digest=row['digest'],
                                  state='revoked', updated_at=value['revoked_at'])
                return value
        except ValueError as exc:
            raise AuthorityError(str(exc)) from exc


class SQLitePruneRecordStore(PruneRecordStore):
    def __init__(self, config):
        super().__init__(config)
        self.config = config
        self.registry = RecordRegistry('prunes')

    def path(self, task_id):
        canonical_uuid(task_id)
        return self.config.tasks_dir.parent / DATABASE_NAME

    def _row(self, c, identity):
        row = self.registry.read(c, identity)
        if row is not None:
            if len(row['raw']) > _MAX_MARKER_BYTES:
                raise PruneError('prune record exceeds its byte limit')
            row['value'] = self._validated_value(row['value'], identity)
        return row

    def read(self, task_id):
        try:
            canonical_uuid(task_id)
            with ControlDatabase(self.config) as db, db.transaction() as c:
                row = self._row(c, task_id)
                return row['value'] if row else None
        except ValueError as exc:
            raise PruneError(str(exc)) from exc

    def write(self, task_id, facts):
        try:
            value = self._validated_value({'contract': PRUNE_RECORD_CONTRACT,
                'task_id': canonical_uuid(task_id), 'recorded_at': prune_now(), **facts}, task_id)
            raw = _raw(value)
            if len(raw) > _MAX_MARKER_BYTES:
                raise PruneError('prune record exceeds its byte limit')
            with mutation_guard(self.config), ControlDatabase(self.config) as db, db.transaction(write=True) as c:
                current = self._row(c, task_id)
                self.registry.put(c, task_id, raw, expected_digest=current['digest'] if current else None,
                                  state='retained', updated_at=value['recorded_at'])
        except ValueError as exc:
            raise PruneError(str(exc)) from exc
