"""Offline import of recovery journals, standing grants and cleanup metadata."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import struct

from .jj import ColocationIntentStore, MAX_COLOCATION_INTENT_BYTES
from .model import canonical_uuid
from .orchestration.authority import validate_authority
from .orchestration.store import InitiativeStore
from .prune import PruneRecordStore, _MAX_MARKER_BYTES
from .record_registry import RecordRegistry
from .store import StoreError, _directory_fd, _managed_start, _registry_lock, _validate_open_file
from .transaction import (MaterializationOwnershipStore, MAX_JOURNAL_BYTES,
                          MAX_OWNERSHIP_SIDECAR_BYTES, validate_journal)


FOLDERS = {'transactions': 'creation-journals', 'authorities': 'authorities',
           'prunes': 'prunes', 'repository-inits': 'repository-inits'}
DOMAINS = tuple(FOLDERS.values())


def _read(fd, name, limit, *, legacy_mode=False):
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
    except OSError as exc:
        raise StoreError(f'cannot open auxiliary migration record {name}: {exc}') from exc
    try:
        mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
        metadata = _validate_open_file(descriptor, 'auxiliary migration record', required_mode=mode)
        if metadata.st_size > limit:
            raise StoreError('auxiliary migration record exceeds its byte limit')
        chunks, remaining = [], limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(chunks)
        if len(raw) > limit:
            raise StoreError('auxiliary migration record exceeds its byte limit')
        # Legacy revoke uses the process umask. Read-only group/other bits do
        # not grant write authority beneath this pinned owner-only directory.
        allowed_mode = (mode & ~0o044 == 0o600) if legacy_mode else mode == 0o600
        # The empty legacy flock file carries no grant and is not imported.
        inert_lock = legacy_mode and name == '.lock' and not raw and mode & ~0o666 == 0
        if not (allowed_mode or inert_lock):
            raise StoreError('unsafe auxiliary migration record mode')
        return raw
    finally:
        os.close(descriptor)


class AuxiliaryImport:
    def __init__(self, config, stack):
        self.config, self.directories, self.names, self.digests = config, {}, {}, {}
        self.sidecar_facts = {}
        for folder in FOLDERS:
            path = config.tasks_dir.parent / folder
            fd = stack.enter_context(_directory_fd(path, create=False,
                managed_start=_managed_start(path, ('control', folder))))
            if fd is not None:
                stack.enter_context(_registry_lock(fd))
            self.directories[folder] = fd
            self.names[folder] = set(os.listdir(fd)) if fd is not None else set()

    def _read(self, folder, fd, name):
        limit = {'transactions': MAX_JOURNAL_BYTES, 'authorities': 1024 * 1024,
                 'prunes': _MAX_MARKER_BYTES, 'repository-inits': MAX_COLOCATION_INTENT_BYTES}[folder]
        if folder == 'transactions' and name.endswith('.ownership'):
            limit = MAX_OWNERSHIP_SIDECAR_BYTES
        return _read(fd, name, limit, legacy_mode=folder == 'authorities')

    def _validated(self, folder, name, raw):
        if not name.endswith('.json'):
            raise StoreError('unexpected auxiliary migration filename')
        key = name[:-5]
        value = RecordRegistry.decode(raw)
        if folder == 'repository-inits':
            value = ColocationIntentStore._decode(raw)
            root = value['root']
            if (not isinstance(root, str) or '\x00' in root or not Path(root).is_absolute()
                    or str(Path(root)) != root or os.path.normpath(root) != root):
                raise StoreError('repository intent root is not canonical')
            if ColocationIntentStore._key(Path(root)) != key:
                raise StoreError('repository intent root does not match its key')
        else:
            canonical_uuid(key)
            if folder == 'transactions':
                value = validate_journal(value, config=self.config)
                identity = value['task_id']
                if value.get('materialization_ownership') is not None:
                    binding = value['materialization_ownership']['sidecar']
                    MaterializationOwnershipStore(self.config).read(binding)
                    self.sidecar_facts[identity + '.ownership'] = binding['file_fact']
            elif folder == 'authorities':
                value = validate_authority(value)
                identity = value['authority_id']
            else:
                value = PruneRecordStore._validated_value(value, key)
                identity = value['task_id']
            if identity != key:
                raise StoreError('auxiliary migration identity differs from filename')
        return key, value

    def _ownership(self, fd, name, raw, stage_config):
        identity = canonical_uuid(name[:-len('.ownership')])
        store = MaterializationOwnershipStore(self.config)
        validated_raw, file_fact = store._read_raw(fd, name)
        if raw != validated_raw or len(raw) < 96:
            raise StoreError('ownership artifact changed or has invalid framing')
        count = struct.unpack('>Q', raw[56:64])[0]
        binding = store._binding(identity, raw[24:56].hex(), raw, count, file_fact)
        # This verifies framing, checksum, task/plan identity and exact inode.
        # Do not publish a new ownership grant from an orphan artifact; the
        # original journal remains the sole source of its authority binding.
        store.read(binding)
        if name in self.sidecar_facts and file_fact != self.sidecar_facts[name]:
            raise StoreError('ownership artifact identity differs from its journal')
        self.sidecar_facts[name] = file_fact
        destination = stage_config.tasks_dir.parent / 'transactions'
        with _directory_fd(destination, create=True,
                managed_start=_managed_start(destination, ('control', 'transactions'))) as target_fd:
            InitiativeStore._write_once(target_fd, name, raw)
        return {'path': 'transactions/' + name, 'digest': hashlib.sha256(raw).hexdigest(),
                'bytes': len(raw), 'kind': 'ownership', 'source_file_fact': file_fact}

    def import_into(self, c, stage_config, after_import=None):
        entries, artifacts, counts = [], [], dict.fromkeys(DOMAINS, 0)
        for folder, domain in FOLDERS.items():
            registry = RecordRegistry(domain)
            if c.execute('SELECT 1 FROM records WHERE domain=? LIMIT 1', (domain,)).fetchone():
                raise StoreError('staging source already contains SQLite registry rows; reconcile domain authority before import')
            fd = self.directories[folder]
            if fd is None:
                continue
            for name in sorted(self.names[folder]):
                raw = self._read(folder, fd, name)
                digest = hashlib.sha256(raw).hexdigest()
                self.digests[folder, name] = digest
                if folder == 'authorities' and name == '.lock' and not raw:
                    continue
                try:
                    if folder == 'transactions' and name.endswith('.ownership'):
                        artifacts.append(self._ownership(fd, name, raw, stage_config))
                        continue
                    key, value = self._validated(folder, name, raw)
                except ValueError as exc:
                    raise StoreError(f'{folder}/{name}: {exc}') from exc
                state = ('revoked' if value['revoked_at'] else 'active') if domain == 'authorities' else value.get('phase', value.get('state', 'retained'))
                registry.put(c, key, raw, state=state,
                             updated_at=value.get('revoked_at') or value.get('recorded_at', value.get('created_at', '')))
                if registry.read(c, key)['raw'] != raw:
                    raise StoreError('imported auxiliary record differs from source bytes')
                entries.append({'domain': domain, 'scope': registry.scope, 'key': key,
                                'digest': digest, 'bytes': len(raw), 'path': folder + '/' + name})
                counts[domain] += 1
                if after_import is not None:
                    after_import(domain, key)
            if c.execute('SELECT count(*) FROM records WHERE domain=?', (domain,)).fetchone()[0] != counts[domain]:
                raise StoreError('staged auxiliary row count differs from source')
        return entries, counts, artifacts

    def verify_source(self):
        for folder, fd in self.directories.items():
            path = self.config.tasks_dir.parent / folder
            if fd is None:
                if os.path.lexists(path):
                    raise StoreError('auxiliary migration source appeared during staging')
                continue
            current, pinned = os.stat(path, follow_symlinks=False), os.fstat(fd)
            if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise StoreError('auxiliary migration source changed identity during staging')
            if set(os.listdir(fd)) != self.names[folder]:
                raise StoreError('auxiliary migration source membership changed during staging')
            for name in self.names[folder]:
                raw = self._read(folder, fd, name)
                if hashlib.sha256(raw).hexdigest() != self.digests[folder, name]:
                    raise StoreError('auxiliary migration source changed during staging')
                if folder == 'transactions' and name in self.sidecar_facts:
                    _, fact = MaterializationOwnershipStore._read_raw(fd, name)
                    if fact != self.sidecar_facts[name]:
                        raise StoreError('ownership artifact identity changed during staging')
