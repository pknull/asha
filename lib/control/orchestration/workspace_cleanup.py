"""Archive-time release of exact-owned registrations; never delete evidence."""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..jj import JjAdapter
from ..prepare import PreparationError, _open_absolute_directory
from ..prune import PruneError, _marker_task_id, verify_owned_root
from ..reconcile import LiveAdapters, reconcile_task
from ..store import TaskStore, TransactionCoordinator, validate_task_paths
from ..tmux import TmuxAdapter
from ..transaction import CreationJournalStore
from ..workspace_cleanup import release_materialization
from .links import control_task_identity_digest
from .model import ATTEMPT_TERMINAL_STATES, scope_repositories


def materialization_candidates(store, initiative):
    """Match journal names to this initiative, including ingestion/composition.

    Names select candidates only. Release independently authenticates the private
    namespace, journal, live registration and workspace identity.
    """
    initiative_id = initiative['initiative_id']
    prefixes = tuple(f'{kind}-{initiative_id}-' for kind in ('verify', 'compose', 'cross'))
    ingestions = {f"ingest-{item['ingestion_id'][:8]}" for item in
                  store.list_result_ingestions_snapshot(initiative_id)}
    result = []
    for member in scope_repositories(initiative):
        source = Path(member['root'])
        readable = re.sub(r'[^a-z0-9]+', '-', source.name.lower()).strip('-')
        readable = (readable or 'repository')[:40].rstrip('-')
        key = f"{readable}-{member['control_repository_id'][5:21]}"
        directory = store.config.control.workspace_root / key / 'materializations' / '.journals'
        try:
            fd = _open_absolute_directory(directory)
        except PreparationError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                continue
            raise
        try:
            for filename in os.listdir(fd):
                if not filename.endswith('.json'):
                    continue
                name = filename[:-5]
                if name.startswith(prefixes) or name in ingestions:
                    result.append((source, name))
        finally:
            os.close(fd)
    return sorted(set(result))


def release_workspaces(store, initiative, *, control_store=None, jj=None):
    if initiative['state'] != 'archived':
        raise ValueError('workspace release requires a retained archive outcome')
    adapter = jj or JjAdapter()
    config = store.config.control
    tasks = control_store or TaskStore(config)
    coordinator = TransactionCoordinator(config)
    attempts = {item['attempt_id']: item for item in
                store.list_attempts_snapshot(initiative['initiative_id'])}
    roots = {member['root'] for member in scope_repositories(initiative)}
    outcomes = []
    for link in store.list_links_snapshot(initiative['initiative_id']):
        attempt = attempts.get(link['attempt_id'])
        if not attempt or attempt['state'] not in ATTEMPT_TERMINAL_STATES:
            raise ValueError('workspace still belongs to a nonterminal attempt')
        task_id = link['control_task_id']
        with tasks.transaction_lock(task_id):
            task = tasks.peek(task_id)
            if (task['repository']['root'] not in roots or
                    control_task_identity_digest(task) != link['control_task_identity_digest']):
                raise ValueError('archive workspace task identity differs from retained link')
            name = task['jj']['workspace_name']
            if not name.startswith('asha-'):
                raise ValueError('archive refuses an operator workspace name')
            source, path = Path(task['repository']['root']), Path(task['jj']['workspace_path'])
            with coordinator.source_lock(source), coordinator.repository_lock(task['repository']['identity']):
                registered = adapter.workspace_identities(source).get(name)
                if registered is None:
                    outcomes.append({'workspace_name': name, 'outcome': 'absent'})
                    continue
                # Archived lifecycle alone does not prove the process is gone.
                live_task = dict(task, lifecycle='running')
                live = reconcile_task(live_task, LiveAdapters(config=config, jj=adapter,
                    tmux=TmuxAdapter(socket=task['tmux']['socket'])))
                if not live['runs'] or any(run['state'] not in {'exited', 'failed'} or run['blocker']
                                           for run in live['runs']):
                    raise ValueError('archive workspace has live or uncertain process evidence')
                journal = CreationJournalStore(config).read(task_id)
                if (journal['repository']['root'] != str(source)
                        or journal['workspace']['name'] != name
                        or journal['workspace']['path'] != str(path)
                        or not journal['workspace']['root_fact']
                        or journal['jj']['change_id'] != task['jj']['change_id']):
                    raise ValueError('archive creation journal identity differs')
                validate_task_paths(config, source, path)
                try:
                    verify_owned_root(path, journal['workspace']['root_fact'])
                    marker = _marker_task_id(path)
                except PruneError as exc:
                    raise ValueError(str(exc)) from exc
                if marker != task_id:
                    raise ValueError('archive workspace marker identity differs')
                identity = adapter.inspect_workspace(path, name, snapshot=False, require_empty=False)
                if (identity.change_id != task['jj']['change_id'] or
                        (identity.change_id, identity.commit_id) != registered):
                    raise ValueError('archive workspace registration identity differs')
                adapter.forget_workspace(source, name)
                outcomes.append({'workspace_name': name, 'outcome': 'forgotten'})
    for source, name in materialization_candidates(store, initiative):
        outcome = release_materialization(config, source, name, jj=adapter)
        outcomes.append({'materialization': name, 'outcome': outcome})
    return outcomes
