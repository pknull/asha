"""Controller linkage for explicit Memory saves; completion revalidates separately."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .store import StoreError
from .registry_guards import mutation_guard

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS hub_memory_publications (
       publication_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES hub_sessions(session_id),
       generation INTEGER NOT NULL, project_id TEXT NOT NULL, assignment_epoch TEXT NOT NULL,
       published_at REAL NOT NULL, receipt TEXT NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS hub_memory_publication_session ON hub_memory_publications(session_id,generation,published_at)',
)


def assignment_epoch(row):
    return str(row.get('assignment_epoch', row['created_at']))


def publication_actor(project_dir):
    """Verify before publication; environment labels select, never prove, identity."""
    if not (os.environ.get('ASHA_HUB_SESSION_ID') or os.environ.get('ASHA_MANAGED_SESSION_ID')):
        return None
    from .config import load_config
    from .session_hub import Hub
    hub = Hub(load_config(os.environ), env=os.environ)
    actor = hub.structured_actor()[0] if os.environ.get('ASHA_MANAGED_SESSION_ID') else hub.actor()
    if Path(actor['project']).resolve() != Path(project_dir).resolve():
        raise StoreError('publication actor is outside this project plane')
    return hub, actor


def record_publication(hub, actor, receipt):
    """Retain the receipt issued from validated bytes under the Memory lock."""
    from .session_experience import Experiences, canonical
    if (receipt.get('source') != 'explicit-save' or receipt.get('status') != 'published'
            or receipt.get('project_id') != actor['project_id']
            or receipt.get('hub_session_id') != actor['session_id']
            or receipt.get('hub_generation') != actor['generation']):
        raise StoreError('publication linkage scope differs')
    hub.initialize()
    with mutation_guard(hub.config), hub.database() as db, db.transaction(write=True) as c:
        Experiences.current(c, actor)
        c.execute('INSERT INTO hub_memory_publications VALUES(?,?,?,?,?,?,?)',
                  (receipt['publication_id'], actor['session_id'], actor['generation'], actor['project_id'],
                   assignment_epoch(actor), time.time(), canonical(receipt)))
    from .session_completion import issue
    from .session_closure import receipt_for, TERMINAL_STATES
    close = actor.get('closure')
    request = receipt_for(close) if close and close['state'] not in TERMINAL_STATES else None
    return issue(hub, actor, outcome='published', detail='Explicit save published Memory v2',
                 publication=receipt, request=request)


def _available(c):
    return c.execute("SELECT 1 FROM sqlite_master WHERE name='hub_memory_publications'").fetchone() is not None


def verify_publication(hub, actor, publication):
    """A submitted JSON file cannot manufacture a controller publication receipt."""
    from .session_experience import Experiences, canonical
    with hub.database() as db, db.transaction() as c:
        Experiences.current(c, actor)
        saved = c.execute('SELECT * FROM hub_memory_publications WHERE publication_id=?',
                          (publication.get('publication_id'),)).fetchone() if _available(c) else None
    if (not saved or saved['session_id'] != actor['session_id'] or saved['generation'] != actor['generation']
            or saved['project_id'] != actor['project_id']
            or saved['receipt'] != canonical({k: v for k, v in publication.items() if k != 'completion'})):
        raise StoreError('verified explicit-save publication receipt required for this Room generation')
    return dict(saved)


def saved_current_assignment(hub, row):
    """Read-only C6 assessment suppression; never a completion readiness test."""
    if row['profile'] != 'room' or not hub.initialized():
        return False
    with hub.database() as db, db.transaction() as c:
        if not _available(c):
            return False
        return c.execute('SELECT 1 FROM hub_memory_publications WHERE session_id=? AND generation=? '
                         'AND project_id=? AND assignment_epoch=? LIMIT 1',
                         (row['session_id'], row['generation'], row['project_id'], assignment_epoch(row))).fetchone() is not None


def latest_saved_at(hub, row):
    """Presentation evidence only; never a readiness or termination decision."""
    with hub.database() as db, db.transaction() as c:
        if not _available(c):
            return None
        saved = c.execute('SELECT published_at,receipt FROM hub_memory_publications WHERE session_id=? AND generation=? '
                          'AND project_id=? AND assignment_epoch=? ORDER BY published_at DESC LIMIT 1',
                          (row['session_id'], row['generation'], row['project_id'], assignment_epoch(row))).fetchone()
    if saved:
        receipt = json.loads(saved['receipt'])
        if (receipt.get('source') == 'explicit-save' and receipt.get('status') == 'published'
                and receipt.get('hub_session_id') == row['session_id']
                and receipt.get('hub_generation') == row['generation']
                and receipt.get('project_id') == row['project_id']):
            return saved['published_at']
    return None
