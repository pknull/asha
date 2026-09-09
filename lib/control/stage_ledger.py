"""Bounded pages and a streaming integrity digest for offline import manifests."""
from __future__ import annotations

import hashlib
import json
import re

from .record_registry import RecordRegistry
from .store import StoreError


PAGE_ENTRIES = 100
PAGE_BYTES = 64 * 1024


def _registry(kind):
    if kind not in {'records', 'artifacts', 'source'}:
        raise StoreError('unknown registry stage ledger')
    return RecordRegistry('registry-migration-ledger', scope=kind)


def _encoded(entry):
    if not isinstance(entry, dict):
        raise StoreError('stage ledger entry must be an object')
    return json.dumps(entry, sort_keys=True, separators=(',', ':'), allow_nan=False).encode() + b'\n'


def write_ledger(c, kind, entries, *, registry=None):
    registry = _registry(kind) if registry is None else registry
    if registry.keys(c, limit=1):
        raise StoreError('stage ledger already exists; use a fresh staging root')
    count, pages, size = 0, 0, 0
    digest, page = hashlib.sha256(), []
    def publish():
        nonlocal pages
        pages += 1
        registry.put(c, f'{pages:012d}', json.dumps({'entries': page}, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode(), state='staged')
    for entry in entries:
        raw = _encoded(entry)
        if len(raw) > PAGE_BYTES - 32:
            raise StoreError('stage ledger entry exceeds page capacity')
        if page and (len(page) == PAGE_ENTRIES or size + len(raw) > PAGE_BYTES - 32):
            publish()
            page, size = [], 0
        page.append(entry)
        size += len(raw)
        count += 1
        digest.update(raw)
    if page:
        publish()
    return {'entry_count': count, 'page_count': pages, 'digest': digest.hexdigest()}


def iter_ledger(c, kind, summary, *, registry=None):
    """Yield entries; exhaustion validates page membership, count and digest.

    Consumers needing a verified whole snapshot must exhaust this iterator before
    acting on it. RecordRegistry additionally verifies each page's byte digest.
    """
    if (not isinstance(summary, dict) or set(summary) != {'entry_count', 'page_count', 'digest'}
            or any(type(summary[k]) is not int or summary[k] < 0 for k in ('entry_count', 'page_count'))
            or not isinstance(summary['digest'], str) or re.fullmatch('[0-9a-f]{64}', summary['digest']) is None):
        raise StoreError('invalid stage ledger summary')
    registry, after = (_registry(kind) if registry is None else registry), ''
    digest, count, pages = hashlib.sha256(), 0, 0
    while True:
        keys = registry.keys(c, after=after)
        for key in keys:
            pages += 1
            if key != f'{pages:012d}' or pages > summary['page_count']:
                raise StoreError('stage ledger pages are missing or unexpected')
            row = registry.read(c, key)
            value = row['value']
            if (len(row['raw']) > PAGE_BYTES or set(value) != {'entries'}
                    or not isinstance(value['entries'], list) or not 1 <= len(value['entries']) <= PAGE_ENTRIES):
                raise StoreError('invalid stage ledger page')
            for entry in value['entries']:
                digest.update(_encoded(entry))
                count += 1
                if count > summary['entry_count']:
                    raise StoreError('stage ledger has unexpected entries')
                yield entry
        if len(keys) < 100:
            break
        after = keys[-1]
    if count != summary['entry_count'] or pages != summary['page_count'] or digest.hexdigest() != summary['digest']:
        raise StoreError('stage ledger count or digest differs from manifest')
