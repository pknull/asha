"""Release proven Control jj registrations, retaining workspace bytes and changes."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .jj import JjAdapter
from .prepare import (
    PreparationError, _materialization_owner, _materialization_parents,
    _open_absolute_directory, plan_materialization,
)
from .store import TransactionCoordinator


def _read_journal(path: Path) -> dict:
    parent = _open_absolute_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            metadata = os.fstat(fd)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
                    or metadata.st_size > 65536):
                raise PreparationError("materialization journal is not private and regular")
            value = json.loads(os.read(fd, 65537))
        finally:
            os.close(fd)
    finally:
        os.close(parent)
    if not isinstance(value, dict):
        raise PreparationError("materialization journal is not an object")
    return value


def release_materialization(config, source: Path, name: str, *, jj=None) -> str:
    """Called only after the consumer has retained its terminal evidence.

    Keep the journal and directory for inspection. A forgotten materialization
    cannot be adopted for another verification; a new execution needs a new name.
    """
    adapter = jj or JjAdapter()
    target = plan_materialization(config, source, name, jj=adapter)
    coordinator = TransactionCoordinator(config)
    with coordinator.source_lock(source), coordinator.repository_lock(target['repository_identity']):
        path = Path(target['workspace_path'])
        _materialization_owner(path.parent, target['repository_key'], create=False)
        journal = _read_journal(path.parent / '.journals' / f'{name}.json')
        expected = {
            'contract': 'asha.control-materialization-journal.v1', 'name': name,
            'source': str(source), 'workspace_name': target['workspace_name'],
            'workspace_path': str(path), 'phase': 'ready',
        }
        if any(journal.get(key) != value for key, value in expected.items()):
            raise PreparationError('materialization journal identity differs; registration retained')
        registered = adapter.workspace_identities(source).get(target['workspace_name'])
        if registered is None:
            return 'absent'
        if registered != (journal.get('change_id'), journal.get('working_commit_id')):
            raise PreparationError('materialization registration identity differs; retained')
        identity = adapter.inspect_workspace(path, target['workspace_name'], snapshot=False, require_empty=False)
        if ((identity.change_id, identity.commit_id) != registered
                or identity.parent_commit_ids != _materialization_parents(journal.get('base_commit_id'))
                or identity.description != f'controller materialization {name}'):
            raise PreparationError('materialization workspace identity differs; retained')
        adapter.forget_workspace(source, target['workspace_name'])
        return 'forgotten'
