"""Project-bound operator intake committed before any supervisor/provider launch."""
from __future__ import annotations

import json
import os
import shutil
import uuid

from .jj import JjAdapter
from .record_registry import RecordRegistry
from .registry_backend import selected_backend, selection
from .registry_guards import mutation_guard
from .rooms import resolve_project, room_harness_command
from .runtime import admission
from .session_harness import CAPABILITIES
from .session_store import SessionStore, SessionsUninitialized, identifier
from .sessions import refuse_managed_operator
from .store import StoreError
from .orchestration.config import from_control
from .orchestration.coordinator import normalize_intent
from .orchestration.store import InitiativeStore, _canonical_bytes
from .orchestration.model import validate_initiative


CONTRACT = 'asha.managed-launch.v1'


def harness_available(harness, env):
    """Probe the executable selected by the same Asha launcher override."""
    _, command = room_harness_command(harness, env)
    return shutil.which(command, path=env.get('PATH', os.environ.get('PATH'))) is not None


def launch_managed(config, *, project, intent, env, harness='claude', launch_id=None, jj=None):
    """Retain one assignment; an explicit launch ID makes lost-response retries safe."""
    refuse_managed_operator(config, env)
    if not CAPABILITIES.get(harness, {}).get('managed'):
        raise StoreError('harness has no supported managed adapter')
    intent = normalize_intent(intent)
    root = resolve_project(project, env=env)['root']
    launch_id = identifier(launch_id) if launch_id is not None else str(uuid.uuid4())
    namespace = uuid.UUID(launch_id)
    iid = str(uuid.uuid5(namespace, 'initiative'))
    sid = str(uuid.uuid5(namespace, 'session'))
    spec = {'root': root, 'harness': harness, 'intent': intent}
    receipt = {'contract': CONTRACT, 'launch_id': launch_id, 'initiative_id': iid,
               'session_id': sid, 'spec': spec}
    if selected_backend(config) != 'sqlite':
        raise StoreError('managed launch requires the active SQLite registry; use registry stage/activate first')
    orchestration = from_control(config)
    store = InitiativeStore(orchestration)
    registry = RecordRegistry('managed-launch', scope='control')
    # Existing session schemas need only a read. Fence first-time DDL with the
    # migration lock, then release it before repository probes and intake.
    with mutation_guard(config):
        try:
            sessions = SessionStore(config)
        except SessionsUninitialized:
            sessions = SessionStore(config, create=True)
    with sessions:
        with sessions.db.transaction() as c:
            previous = registry.read(c, launch_id)
        if previous is not None and previous['value'] != receipt:
            raise StoreError('launch ID content changed; use the original assignment or a new launch ID')
        # Repository/VCS probes happen before the short write transaction, and
        # are unnecessary when custody was already committed on an earlier try.
        initiative = None
        if previous is None:
            if not harness_available(harness, env):
                raise StoreError(f'{harness} executable is unavailable; install it or select another harness')
            from .orchestration.cli import _prepare_initiative
            initiative = _prepare_initiative([
                '--repo', root, '--slug', 'work-' + launch_id,
                '--label', intent[:48], '--objective', intent, '--acceptance', intent,
            ], orchestration, jj or JjAdapter(), initiative_id=iid)
        with store._write_lock(iid), sessions.db.transaction(write=True) as c:
            if selection(c, config) is None:
                raise StoreError('Control registry changed before managed launch')
            previous = registry.read(c, launch_id)
            if previous is not None:
                if previous['value'] != receipt:
                    raise StoreError('launch ID content changed; retry the original assignment')
            else:
                if initiative is None:
                    raise StoreError('managed launch receipt disappeared; inspect registry recovery')
                value, raw = _canonical_bytes(validate_initiative, initiative)
                store._validate_initiative_change(None, value)
                store._put(c, iid, 'initiative', iid, raw, value)
                from .orchestration.cli import _event
                store._append_event(c, iid, _event(initiative, 'initiative-created',
                                    {'slug': initiative['slug']}, initiative['created_at']))
                prompt = (
                    f'Your assigned initiative already exists: {iid}. Project: {root}.\n'
                    'Use the session-orchestrate-initiative workflow for this existing initiative. '
                    'Inspect the project, propose a bounded plan, report its approval state, and '
                    'finish this turn if operator input is required. The runtime owns your '
                    'coordinator claim and will deliver answers and subsequent work.\n'
                    f'Assignment: {intent}'
                )
                sessions._create_in_transaction(c, cwd=root, prompt=prompt, harness=harness,
                                               initiative_id=iid, session_id=sid)
                registry.put(c, launch_id, json.dumps(receipt, sort_keys=True).encode())
        try:
            state = sessions.get(sid)['state']
        except (OSError, ValueError, StoreError):
            state = 'unavailable'
    try:
        policy = admission(config)
    except (OSError, ValueError, StoreError) as exc:
        policy = {'mode': 'unavailable', 'message': str(exc)}
    supervisor = {'running': False, 'message': f"work retained; runtime is {policy['mode']}"}
    if policy['mode'] == 'unavailable':
        supervisor['message'] += ': ' + policy['message']
    if policy['mode'] == 'running':
        from .orchestration.supervisor_daemon import start_supervisor
        try:
            supervisor, code = start_supervisor(orchestration, env)
            supervisor = {**supervisor, 'exit_code': code}
        except (OSError, ValueError, StoreError) as exc:
            supervisor = {'running': False, 'message': str(exc)}
    return {**receipt, **spec, 'transport': 'managed', 'state': state,
            'admission': policy, 'supervisor': supervisor}
