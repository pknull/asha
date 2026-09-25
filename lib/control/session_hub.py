"""Project sessions without an initiative lifecycle or a model-turn scheduler.

Terminal ownership remains with Rooms. SQLite retains conversation identity,
observations and messages; missing observations never gate harness execution.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import session_closure as closure
from .database import ControlDatabase, DATABASE_NAME
from .registry_guards import mutation_guard
from .rooms import (RoomStore, _action_identity, _owned_state, open_room, close_room, attach_room,
                    resolve_project)
from .session_selection import evidence as selection_evidence, requested
from .session_store import identifier, text, digest
from .store import StoreError
from .tmux import FENCE_LIMIT, RoomInputRefused, TmuxAdapter


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS hub_sessions (session_id TEXT PRIMARY KEY, lifecycle TEXT NOT NULL, updated_at REAL NOT NULL, payload TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS hub_session_state ON hub_sessions(lifecycle,updated_at,session_id)",
    "CREATE INDEX IF NOT EXISTS hub_session_project ON hub_sessions(json_extract(payload, '$.project_id'),updated_at,session_id)",
    "CREATE TABLE IF NOT EXISTS hub_messages (message_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES hub_sessions(session_id), delivery_key TEXT NOT NULL, body TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(session_id,delivery_key))",
    "CREATE INDEX IF NOT EXISTS hub_session_messages ON hub_messages(session_id,created_at,message_id)",
)
EVENTS = {'session-start': 'idle', 'prompt-submitted': 'working', 'tool-started': 'working',
          'tool-completed': 'working', 'permission-requested': 'needs-input',
          'turn-stopped': 'idle', 'session-ended': 'exited'}
# A session that is closing gracefully still owns its process; its reporter,
# hooks and handoff remain valid until the close terminates it.
ACTIVE_LIFECYCLES = {'starting', 'open', 'closing'}
CLOSE_WAIT_LIMIT = 600
# Idle-input refusals (#96) after which an automatic native-resume restart may
# still run: nothing showed a person or a draft at the pane. ``disabled`` (the
# default: idle typing is off) keeps the pre-#96 Codex native-resume close.
# Every other refusal (attached, mode, ownership, occupied, stale, partial,
# error) keeps the Room and asks for attach.
RESTARTABLE_REFUSALS = frozenset({'disabled', 'ineligible', 'unknown'})


def _open_tools(row):
    return any(kind != 'report' for kind in (row.get('active_tools') or {}).values())


def _idle_fence(row):
    """The observed idle boundary a typed delivery is bound to; any native event moves it."""
    return (row['generation'], row['lifecycle'], row['activity'], row.get('native_activity'),
            row.get('native_observed_at'), row.get('work_epoch'), row.get('assignment_epoch'))


class Hub:
    def __init__(self, config, *, env=None, tmux=None):
        self.config = config
        self.env = dict(os.environ if env is None else env)
        self.tmux = tmux or TmuxAdapter()

    def database(self, *, create=False):
        return ControlDatabase(self.config, create=create, busy_timeout=0.2)

    @contextmanager
    def _action_lock(self, sid):
        """Serialize process mutations without holding a database write lock."""
        from .store import _directory_fd, _managed_start, _registry_lock
        root = self.config.tasks_dir.parent / 'hub-locks' / identifier(sid)
        with mutation_guard(self.config), _directory_fd(root, create=True,
                managed_start=_managed_start(root, ('control', 'hub-locks', sid))) as fd:
            with _registry_lock(fd):
                yield

    @contextmanager
    def _observation_lock(self, sid):
        """Serialize observed activity with the brief automatic-stop boundary."""
        from .store import _directory_fd, _managed_start, _registry_lock
        root = self.config.tasks_dir.parent / 'hub-observation-locks' / identifier(sid)
        with mutation_guard(self.config), _directory_fd(root, create=True,
                managed_start=_managed_start(root, ('control', 'hub-observation-locks', sid))) as fd:
            with _registry_lock(fd):
                yield

    def initialized(self):
        if not (self.config.tasks_dir.parent / DATABASE_NAME).exists():
            return False
        with self.database() as db, db.transaction() as c:
            return c.execute("SELECT 1 FROM sqlite_master WHERE name='hub_sessions'").fetchone() is not None

    def initialize(self):
        with mutation_guard(self.config), self.database(create=True) as db, db.transaction(write=True) as c:
            from .session_experience import SCHEMA as EXPERIENCE_SCHEMA
            from .session_publication import SCHEMA as PUBLICATION_SCHEMA
            for statement in (*SCHEMA, *EXPERIENCE_SCHEMA, *PUBLICATION_SCHEMA):
                c.execute(statement)
            if EXPERIENCE_SCHEMA and 'close_request_id' not in {r[1] for r in c.execute('PRAGMA table_info(hub_experience_captures)')}:
                c.execute('ALTER TABLE hub_experience_captures ADD COLUMN close_request_id TEXT')

    @staticmethod
    def _save(c, row):
        row['updated_at'] = time.time()
        c.execute('INSERT INTO hub_sessions VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET lifecycle=excluded.lifecycle,updated_at=excluded.updated_at,payload=excluded.payload',
                  (row['session_id'], row['lifecycle'], row['updated_at'], json.dumps(row)))

    def get(self, sid):
        identifier(sid)
        with self.database() as db, db.transaction() as c:
            row = c.execute('SELECT payload FROM hub_sessions WHERE session_id=?', (sid,)).fetchone()
        if row is None:
            raise StoreError('session not found')
        return json.loads(row[0])

    def owns(self, sid):
        if not self.initialized():
            return False
        with self.database() as db, db.transaction() as c:
            return c.execute('SELECT 1 FROM hub_sessions WHERE session_id=?', (sid,)).fetchone() is not None

    def launch(self, *, project, prompt, name=None, harness='claude', profile='worker', session_id=None, transport='terminal', learning_ids=None, result_contract=None,
               model=None, effort=None):
        from .sessions import refuse_managed_operator
        refuse_managed_operator(self.config, self.env)
        selected = resolve_project(project, env=self.env)
        prompt = text(prompt, 'assignment')
        if profile not in {'worker', 'room'}:
            raise StoreError('profile must be worker or room')
        from .harness import validate_harness
        validate_harness(harness)
        if transport not in {'terminal', 'structured'} or (transport == 'structured' and harness not in {'claude', 'codex'}):
            raise StoreError('structured execution is supported only for Claude and Codex')
        if transport == 'structured' and profile != 'worker':
            raise StoreError('Rooms use interactive terminal sessions')
        from .session_selection import normalize
        # Refused before any record, pane or process exists (#95), including
        # values the tmux transport could not carry into the Room pane.
        selection = normalize(harness, transport, model=model, effort=effort)
        if transport == 'terminal':
            from .rooms import RoomError, room_respawn_argv
            try:
                room_respawn_argv(Path(__file__).resolve().parents[2], harness, prompt,
                                  hub_session=True, selection=selection)
            except RoomError as exc:
                raise StoreError(str(exc)) from exc
        sid = identifier(session_id) if session_id else str(uuid.uuid4())
        spec = dict(project=selected['root'], prompt=prompt, harness=harness, profile=profile,
                    name=text(name, 'session name', 256) if name is not None else ' '.join(prompt.split())[:64], transport=transport)
        # Only requested values enter the spec, so an omitted selection keeps
        # the idempotency key and every native argv exactly as before.
        spec.update(selection)
        if result_contract:
            if transport != 'structured' or result_contract != 'asha.session-result.v1':
                raise StoreError('explicit result contract requires structured execution')
            spec['result_contract'] = result_contract
        if learning_ids is not None:
            spec['learning_ids'] = learning_ids
        self.initialize()
        with mutation_guard(self.config), self.database() as db, db.transaction(write=True) as c:
            existing = c.execute('SELECT payload FROM hub_sessions WHERE session_id=?', (sid,)).fetchone()
            if existing:
                row = json.loads(existing[0])
                if row['spec'] != spec:
                    raise StoreError('session ID already has another assignment')
                return self.show(sid)
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='managed_sessions'").fetchone() and c.execute('SELECT 1 FROM managed_sessions WHERE session_id=?', (sid,)).fetchone():
                raise StoreError('session ID belongs to an existing managed conversation')
            row = dict(session_id=sid, room_id=str(uuid.uuid4()), room_history=[], generation=1,
                       project_name=selected['name'], project_id=selected['project_id'],
                       lifecycle='starting', activity='unknown',
                       native_id=None, observed_at=None, reason='Awaiting native observation',
                       result=None, question=None, created_at=time.time(), spec=spec, **spec)
            self._save(c, row)
        with self._action_lock(sid):
            current = self.get(sid)
            if current['lifecycle'] != 'starting':
                return self.show(sid)
            return self._start(current, prompt)

    def _start(self, row, prompt, *, closing=False):
        from . import session_guidance as guidance
        row = self._update(row['session_id'], expected_generation=row['generation'], current_assignment=prompt,
                           assignment_epoch=str(uuid.uuid4()), active_tools={})
        block, manifest = guidance.resolve(self, row, row.get('learning_ids'))
        key = 'opening' if row['generation'] == 1 or row['transport'] == 'structured' else 'resume:' + str(row['generation'])
        guidance.retain(self, row, key, guidance.planned(manifest, prompt, block))
        prompt = text(prompt + block, 'assignment with selected guidance')
        if row['transport'] == 'structured':
            return self._start_structured(row, prompt)
        from .session_completion import WORKER_INSTRUCTION
        prompt += '\n\n' + WORKER_INSTRUCTION
        brief = prompt
        if row['profile'] == 'worker':
            brief += ('\n\nOptional session tools: `asha control session report --state needs-input --text "question"` '
                      'or `--state finished --text "result"`; `asha control session messages` reads queued context. '
                      'Work normally using this repository and your native harness. No initiative or per-turn report is required.')
            from .session_experience import Experiences
            if Experiences(self).completion_enabled(row):
                brief += '\n\n' + Experiences.completion_text()
        try:
            # Friendly labels may repeat. Rooms retain every incarnation.
            room_name = f"session-{row['session_id']}-{row['generation']}"
            open_room(name=room_name, project=row['project'], harness=row['harness'], prompt=brief,
                      config=self.config, env=self.env, tmux=self.tmux,
                      asha_root=Path(__file__).resolve().parents[2], room_id=row['room_id'],
                      profile=row['profile'], hub_session_id=row['session_id'],
                      hub_generation=row['generation'], resume_id=row.get('native_id'),
                      selection=requested(row.get('spec')))
        except BaseException as exc:
            self._update(row['session_id'], lifecycle='interrupted', reason=str(exc)[:1000])
            raise
        self._update(row['session_id'], lifecycle='closing' if closing else 'open')
        guidance.supplied(self, row, key)
        return self.show(row['session_id'])

    def _start_structured(self, row, prompt):
        from .session_store import SessionStore
        try:
            with mutation_guard(self.config), SessionStore(self.config, create=True) as sessions:
                with sessions.db.transaction(write=True) as c:
                    sessions._create_in_transaction(c, cwd=row['project'], prompt=prompt,
                        harness=row['harness'], initiative_id=None, session_id=row['session_id'])
        except (ValueError, OSError) as exc:
            self._update(row['session_id'], lifecycle='interrupted', reason=str(exc)[:1000])
            raise
        self._update(row['session_id'], lifecycle='open')
        self._wake_structured(row['session_id'])
        return self.show(row['session_id'])

    def _has_structured_record(self, sid):
        with self.database() as db, db.transaction() as c:
            return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='managed_sessions'").fetchone() and
                        c.execute('SELECT 1 FROM managed_sessions WHERE session_id=?', (sid,)).fetchone())

    def _wake_structured(self, sid):
        from .orchestration.config import from_control
        from .orchestration.supervisor_daemon import start_supervisor
        # Admission is an operator runtime preference, never an initiative gate.
        from .runtime import admission
        mode = admission(self.config)['mode']
        warning = None
        if mode == 'running':
            try:
                outcome, code = start_supervisor(from_control(self.config), self.env)
                if code:
                    warning = outcome.get('message', 'Supervisor could not start')
            except (ValueError, OSError) as exc:
                warning = str(exc)
        else:
            warning = 'Queued; runtime admission is ' + mode
        self._update(sid, runtime_warning=warning)
        return {'admission': mode, 'dispatch_warning': warning}

    def _update(self, sid, *, expected_generation=None, closure_fn=None, validate_fn=None, **changes):
        """Merge changes into the freshly read row inside one write transaction.

        ``closure_fn(record, row)`` computes the closure transition against the
        record as it is at write time, so a hook that read its row earlier can
        never overwrite an acknowledgement recorded in between.
        """
        with mutation_guard(self.config), self.database() as db, db.transaction(write=True) as c:
            found = c.execute('SELECT payload FROM hub_sessions WHERE session_id=?', (sid,)).fetchone()
            if not found:
                raise StoreError('session not found')
            row = json.loads(found[0])
            if expected_generation is not None and (row['generation'] != expected_generation or row['lifecycle'] not in ACTIVE_LIFECYCLES):
                raise StoreError('stale or inactive session reporter')
            if validate_fn is not None:
                validate_fn(c, row)
            row.update(changes)
            if closure_fn is not None:
                row['closure'] = closure_fn(row.get('closure'), row)
            self._save(c, row)
        return row

    def show(self, sid):
        row = self.get(sid)
        capture = (row.get('closure') or {}).get('capture') or row.get('capture') or {}
        if capture.get('report_id'):
            with self.database() as db, db.transaction() as c:
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='hub_experience_reviews'").fetchone():
                    review = c.execute('SELECT status FROM hub_experience_reviews WHERE report_id=? ORDER BY created_at DESC,review_id DESC LIMIT 1',
                                       (capture['report_id'],)).fetchone()
                    row['experience_review'] = review['status'] if review else 'unknown'
        from .session_guidance import history
        row['guidance'] = history(self, sid)
        row['guidance_complete'] = len(row['guidance']) <= 100
        row['guidance'] = row['guidance'][:100]
        if row['transport'] == 'structured':
            from .session_store import SessionStore
            from .session_output import project
            if not self._has_structured_record(sid):
                row.update(activity=row['lifecycle'], pending_messages=0, capabilities={'resume': True})
                self._present_session(row)
                return row
            with SessionStore(self.config) as sessions:
                state = sessions.get(sid)
                row['recovery_digest'] = sessions.recovery_digest(state)
                row['recovery'] = state.get('recovery')
                row['activity'] = state['state']
                with sessions.db.transaction() as c:
                    request = c.execute("SELECT question FROM session_requests WHERE session_id=? AND state='pending' LIMIT 1", (sid,)).fetchone()
                    row['pending_messages'] = c.execute("SELECT COUNT(*) FROM session_messages WHERE session_id=? AND state='queued'", (sid,)).fetchone()[0]
                    last = c.execute("SELECT * FROM session_events WHERE session_id=? AND kind IN ('completed','failed') ORDER BY sequence DESC LIMIT 1", (sid,)).fetchone()
                    if last:
                        event = dict(last)
                        project(c, event)
                        row['result'] = event['payload'].get('summary')
                        if not row['result']:
                            output = c.execute("SELECT * FROM session_events WHERE session_id=? AND turn_id=? AND kind='text' ORDER BY sequence DESC LIMIT 1", (sid, last['turn_id'])).fetchone()
                            if output:
                                output = dict(output)
                                project(c, output)
                                row['result'] = output['payload'].get('text')
                        if state['state'] == 'idle' and not row['pending_messages'] and last['kind'] == 'completed':
                            row['activity'] = 'finished'
                    if request:
                        row.update(activity='needs-input', question=request[0], reason=request[0])
            if row['lifecycle'] == 'closed':
                row['activity'] = 'closed'
            elif row['activity'] == 'finished':
                row['reason'] = row['result'] or 'Native turn completed'
            else:
                row['reason'] = row.get('question') or state.get('state', 'unknown')
            if row.get('runtime_warning') and row['pending_messages']:
                row['reason'] = row['runtime_warning']
            row['capabilities'] = {'attach': 'conversation-view', 'send': 'turn-boundary', 'permissions': 'native',
                                   'close': 'final-structured-turn'}
            self._present_session(row)
            return row
        if row['lifecycle'] in {'closed', 'stopped'}:
            row['activity'] = row['lifecycle']
            row['process_state'] = 'ended'
        else:
            row['process_state'] = 'unknown'
            try:
                room = RoomStore(self.config).read(row['room_id'])
                state, detail = _owned_state(room, self.tmux)
                row['process_state'] = 'ended' if state in {'missing', 'ended'} else 'live' if state == 'open' else 'unknown'
                if state in {'missing', 'ended'}:
                    row['activity'] = 'finished' if row['activity'] == 'finished' else 'exited'
                    row['reason'] = detail
                elif state != 'open':
                    row.update(activity='unknown', reason=detail)
                elif row['observed_at'] and time.time() - row['observed_at'] > 300 and row['activity'] == 'working':
                    row.update(activity='unknown', reason='No recent observation; the harness may still be working')
            except (ValueError, OSError) as exc:
                row.update(activity='unknown', reason=str(exc)[:1000])
        with self.database() as db, db.transaction() as c:
            row['pending_messages'] = c.execute("SELECT COUNT(*) FROM hub_messages WHERE session_id=? AND state='queued'", (sid,)).fetchone()[0]
        row['capabilities'] = {'attach': 'native-terminal', 'send': 'queued-until-read',
                               'permissions': 'native', 'resume': row['harness'] in {'claude', 'codex'},
                               'close': 'stop-hook-final-turn' if row['harness'] in closure.STOP_HOOK_HARNESSES else 'queued-request-only'}
        self._present_session(row)
        return row

    def _present_session(self, row):
        from .session_presentation import memory_label, present
        row['selection'] = selection_evidence(row)
        from .session_publication import latest_saved_at
        row['memory_saved_at'] = latest_saved_at(self, row)
        from .session_completion import view
        if row['lifecycle'] not in {'closed', 'stopped'}:
            row['completion_readiness'] = view(self, row)
        record = row.get('closure') or {}
        handoff = record.get('handoff') or {}
        if (record.get('generation') == row['generation']
                and handoff.get('generation') == row['generation']
                and handoff.get('outcome') == 'published' and handoff.get('verified') is True
                and handoff.get('digests')):
            row['memory_saved_at'] = max(row['memory_saved_at'] or 0, handoff['acknowledged_at'])
        self._present_closure(row)
        if (record.get('generation') == row['generation'] and record.get('state') == 'forced'
                and row['memory_saved_at'] is not None):
            guidance = ((row['closure']['guidance'] + '; ') if handoff.get('outcome') in closure.ACKNOWLEDGED
                        else 'Force-closed; ') + memory_label(row)
            row['closure'] = dict(row['closure'], guidance=guidance)
            row['reason'] = guidance
        row.update(present(row))

    def _present_closure(self, row):
        """Read-only view: refresh structured delivery facts, attach guidance, surface the state."""
        record = row.get('closure')
        if not record:
            return
        if record['generation'] != row['generation']:
            row['closure'] = dict(record, stale=True, guidance='Closure record from an earlier incarnation')
            return
        if row['transport'] == 'structured' and self._has_structured_record(row['session_id']):
            record = self._structured_delivery(row, record)
        if (row['transport'] == 'terminal' and row.get('process_state') != 'ended'
                and record['state'] in {'pending-delivery', 'delivered', 'acknowledged'}
                and not closure.recent_native_observation(row)):
            from .session_completion import check
            try:
                # A proven idle boundary remains valid without periodic callbacks.
                check(self, row, idle=True)
            except (OSError, ValueError):
                record = dict(record, attachment_required=True,
                    last_error='native activity is unknown or stale; no verified idle boundary, needs attach')
        record = dict(record, guidance=closure.guidance_for(
            row, record, idle_delivery=getattr(self.config, 'idle_delivery', False) is True))
        state = record['state']
        row['closure'] = record
        if row['lifecycle'] in {'closing', 'interrupted'}:
            failing = state in closure.RETRYABLE_STATES or state == 'undeliverable'
            phase = 'Closing' + (f' ({state})' if failing else '')
            if row['activity'] == 'needs-input':
                # The final turn is exactly where a native prompt or question lands; never hide it.
                row['reason'] = phase + ', needs input: ' + str(row.get('question') or row.get('reason') or '')
            elif row['activity'] == 'exited':
                # A dead terminal is not "the machine's turn"; keep the observed fact.
                row['reason'] = phase + f" (exited: {row.get('reason', '')}); " + record['guidance']
            else:
                # 'unknown' is merely missing telemetry; the closure state is the better fact.
                row['activity'] = 'close-failed' if failing else 'closing'
                row['reason'] = phase + ': ' + record['guidance']
            record['needs_attention'] = failing or row['activity'] == 'exited'
        elif row['lifecycle'] in {'closed', 'stopped'}:
            row['reason'] = record['guidance']
            record['needs_attention'] = bool(record.get('attention'))
            if record.get('attention'):
                row['activity'] = 'close-failed'

    def _structured_delivery(self, row, record):
        """Derive delivery from the retained close message and its turn; never persisted here."""
        if record['state'] not in {'pending-delivery', 'delivered'}:
            return record
        from .session_store import SessionStore
        with SessionStore(self.config) as sessions, sessions.db.transaction() as c:
            message = c.execute('SELECT * FROM session_messages WHERE session_id=? AND delivery_key=?',
                                (row['session_id'], closure.message_key(record))).fetchone()
            turn = None
            if message and message['turn_id']:
                turn = c.execute('SELECT * FROM session_turns WHERE turn_id=?', (message['turn_id'],)).fetchone()
        if message is None:
            return record
        if message['state'] in {'cancelled', 'uncertain'} or (turn and turn['state'] not in {'running', 'completed'}):
            return closure.transition(record, 'undeliverable', last_error=f"close turn {turn['state'] if turn else message['state']}")
        if turn is None:
            return record
        delivered = record if record['state'] == 'delivered' else closure.mark_delivered(
            record, 'structured-turn', detail='close request consumed by turn ' + turn['turn_id'], message_id=message['message_id'])
        if turn['state'] == 'completed' and not delivered.get('handoff'):
            return closure.transition(delivered, 'unanswered')
        return delivered

    def list(self, *, include_closed=False, limit=100, deadline=None):
        if not self.initialized():
            return {'rows': [], 'complete': True}
        if not 1 <= limit <= 1000:
            raise StoreError('invalid session limit')
        with self.database() as db, db.transaction() as c:
            # A close that failed its handoff stays on the default page until the
            # operator acknowledges it with a force-close; silence is never success.
            where = '' if include_closed else "WHERE lifecycle!='closed' OR json_extract(payload, '$.closure.attention')=1"
            records = c.execute(f'SELECT session_id FROM hub_sessions {where} ORDER BY updated_at DESC LIMIT ?', (limit + 1,)).fetchall()
        rows, complete = [], len(records) <= limit
        for record in records[:limit]:
            if deadline is not None and time.monotonic() >= deadline:
                complete = False
                break
            try:
                rows.append(self.show(record[0]))
            except (ValueError, OSError) as exc:
                complete = False
                row = self.get(record[0])
                row.update(activity='unknown', reason=str(exc), pending_messages=0)
                rows.append(row)
        if not include_closed:
            rows = [r for r in rows if not (r['transport'] == 'structured' and r['profile'] == 'worker' and r['activity'] == 'finished')]
        return {'rows': rows, 'complete': complete}

    def attach(self, sid):
        row = self.get(sid)
        if row['transport'] == 'structured':
            return {'transport': 'structured', 'session_id': sid}
        if row['lifecycle'] in {'closed', 'stopped'}:
            raise StoreError('session is closed or stopped; resume it first')
        return attach_room(RoomStore(self.config), row['room_id'], tmux=self.tmux)

    def stop(self, sid, *, close=False):
        """Abrupt stop: ends the owned process now and never claims a memory save.

        ``close=True`` is the explicit force-close; a bare ``close()`` is the
        graceful path with a verified handoff.
        """
        from .sessions import refuse_managed_operator
        refuse_managed_operator(self.config, self.env)
        with self._action_lock(sid):
            row = self.get(sid)
            if row['lifecycle'] == 'closed' or (row['lifecycle'] == 'stopped' and not close):
                record = row.get('closure')
                if record and record.get('attention'):
                    # Any explicit stop or force-close is the operator accepting that no
                    # memory was saved; the closure evidence itself is kept untouched.
                    self._update(sid, closure=dict(record, attention=False, dismissed_at=time.time()))
                return self.show(sid)
            record = row.get('closure')
            if self._live(row) or (record and record['state'] not in closure.TERMINAL_STATES):
                # A verified handoff already recorded is kept as evidence; a
                # force never claims one that did not happen.
                record = closure.transition(record or self._new_closure(row), 'forced', terminated_at=time.time())
            # An explicit operator stop or force-close is itself the acknowledgement.
            return self._stop(row, close=close, closure_record=record, dismiss=True)

    def _live(self, row):
        """Is there a live agent this session could ask for a final turn?"""
        if row['lifecycle'] not in ACTIVE_LIFECYCLES:
            return False
        if row['transport'] == 'structured':
            if not self._has_structured_record(row['session_id']):
                return False
            from .session_store import SessionStore
            with SessionStore(self.config) as sessions:
                state = sessions.get(row['session_id'])
            return state['state'] not in {'stopped', 'failed'} and not state['stop_requested']
        if row['lifecycle'] == 'starting':
            return False
        try:
            state, _ = _owned_state(RoomStore(self.config).read(row['room_id']), self.tmux)
        except (ValueError, OSError):
            return False
        return state == 'open'

    def _stop(self, row, *, close, closure_record=None, dismiss=False):
        """Terminate the owned process; record the closure outcome honestly."""
        sid = row['session_id']
        if row['transport'] == 'structured':
            from .session_store import SessionStore
            if self._has_structured_record(sid):
                with SessionStore(self.config) as sessions:
                    sessions.stop(sid)
        else:
            rooms = RoomStore(self.config)
            # A process cannot be spawned before its Room intent is durable. A
            # failed validation can leave only the hub intent. Never touch a
            # colliding or uncertain tmux target in that case.
            if row['lifecycle'] in {'starting', 'interrupted'} and not any(r['room_id'] == row['room_id'] for r in rooms.list()):
                session = f"{self.config.session_prefix}room-{row['room_id'].replace('-', '')[:8]}"
                if self.tmux.has_session(session):
                    raise StoreError('launch ownership is uncertain; inspect the retained session')
            else:
                close_room(rooms, row['room_id'], tmux=self.tmux)
        changes = dict(lifecycle='closed' if close else 'stopped')
        if closure_record is not None:
            attention = (closure_record['state'] in closure.ATTENTION_STATES and not closure_record['memory'].get('saved')
                         and not dismiss)
            changes['closure'] = dict(closure_record, terminated_at=closure_record.get('terminated_at') or time.time(),
                                      attention=attention)
        self._update(sid, **changes)
        return self.show(sid)

    def close(self, sid, *, force=False, wait=0):
        """Graceful close: request a final handoff turn; terminate only after a verified acknowledgement.

        Repeated calls are idempotent: one request per incarnation, no duplicate
        messages. ``wait`` polls for the acknowledgement (bounded) before the
        final termination. ``force`` terminates now and records that no
        memory save was claimed.
        """
        if type(wait) is not int or not 0 <= wait <= CLOSE_WAIT_LIMIT:
            raise StoreError(f'wait must be 0..{CLOSE_WAIT_LIMIT} seconds')
        if force:
            if wait:
                raise StoreError('--force terminates now; it cannot be combined with --wait')
            return self.stop(sid, close=True)
        from .sessions import refuse_managed_operator
        refuse_managed_operator(self.config, self.env)
        with self._action_lock(sid):
            result = self._close_once(sid)
        deadline = time.monotonic() + wait
        while result['lifecycle'] == 'closing' and time.monotonic() < deadline:
            state = result['closure']['state']
            if result['activity'] == 'needs-input':
                break
            if state == 'acknowledged':
                with self._action_lock(sid):
                    result = self._close_once(sid)
                if result['lifecycle'] == 'closing':
                    time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
                continue
            if state not in {'pending-delivery', 'delivered'}:
                break
            # A queued-only channel is satisfied only when the worker reads messages; poll it gently.
            interval = 0.5 if result['closure']['delivery'].get('channel') in {'stop-hook', 'structured-turn'} else 2.0
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            with self._action_lock(sid):
                result = self._close_once(sid)
        return result

    def _close_once(self, sid):
        row = self.get(sid)
        if row['lifecycle'] == 'closed':
            return self.show(sid)
        record = row.get('closure')
        observed = record   # the record as read; a hook may move it while the tmux probe runs
        current = record and record['generation'] == row['generation'] and record['state'] not in closure.TERMINAL_STATES
        live = self._live(row)
        from .session_completion import close_finalized
        finalized = close_finalized(self, row, ended=not live)
        if finalized is not None:
            return finalized
        if not live:
            from .session_experience import Experiences
            if row['lifecycle'] in ACTIVE_LIFECYCLES:
                row = Experiences(self).reconcile_completion(row)
            # Nothing can be asked. Retain whatever the request reached, never a success.
            if current and record['state'] in {'delivered', 'pending-delivery'}:
                record = closure.transition(record, 'undeliverable', last_error='the harness is no longer live')
            elif current and record['state'] == 'acknowledged':
                record = closure.transition(record, 'handoff-failed',
                    last_error='completion receipt missing or stale; inspect Memory and handoff')
            elif not record or record['generation'] != row['generation']:
                record = closure.transition(self._new_closure(row), 'unavailable')
            return self._stop(row, close=True, closure_record=record)
        if current and row['transport'] == 'structured':
            record = self._structured_delivery(row, record)
        if not current:
            if record:
                self._update(sid, closure_history=row.get('closure_history', []) + [record])
            record = self._deliver(row, self._new_closure(row))
        elif record['state'] in closure.RETRYABLE_STATES or record['state'] == 'undeliverable':
            record = self._deliver(row, closure.rearm(record))
        elif (row['transport'] == 'terminal' and record['state'] == 'pending-delivery'
              and record['delivery'].get('channel') == 'queued-message'):
            record = self._continue_idle_close(row, record)
        elif (row['transport'] == 'terminal' and record['state'] == 'pending-delivery'
              and record['delivery'].get('channel') == 'stop-hook' and row.get('native_activity') == 'idle'):
            # Same request and attempt: an earlier refusal (attached, typed input) may have cleared.
            record = self._inject_close(row, record)
        if record['generation'] != row['generation']:
            # Internal close continuation changed incarnation, retaining request
            # identity. Its actor may already have acknowledged during startup.
            row = self.get(sid)
            record = row['closure']
            observed = record
            if row['lifecycle'] == 'interrupted':
                return self.show(sid)
        if record['state'] == 'acknowledged':
            from .session_completion import check, CompletionPending
            try:
                check(self, self.get(sid))
            except CompletionPending:
                pass
            except (ValueError, OSError) as exc:
                record = closure.transition(record, 'handoff-failed', last_error=str(exc), attachment_required=True)
        if (row['transport'] == 'terminal' and record['state'] in {'pending-delivery', 'delivered', 'acknowledged'}
                and not record.get('attachment_required') and not closure.recent_injection(record)
                and not closure.recent_native_observation(self.get(sid))):
            record = closure.transition(record, record['state'], attachment_required=True,
                last_error='native activity is unknown or stale; no verified idle boundary, needs attach')
        if record['state'] == 'undeliverable':
            # The queue refused the request: there is no agent left to ask.
            return self._stop(row, close=True, closure_record=record)
        # Apply only against the record this call read. A SessionEnd observation
        # that landed meanwhile (it writes without the action lock) must not be
        # rolled back to the older snapshot; the next close re-derives from it.
        self._update(sid, lifecycle='closing',
                     closure_fn=lambda current, _row: record if current == observed else current)
        return self.show(sid)

    def _new_closure(self, row):
        from .session_experience import Experiences
        record = closure.new_closure(row)
        # The selection this request was made under; statistics attribute the
        # close to it even after a later resume or reroute (#95).
        record['selection'] = selection_evidence(row)
        record['capture'] = Experiences(self).close_capture(row, record['request_id'])
        return record

    def report(self, *, state, body=None, native_id=None, experience_file=None,
               experience_ref=None, key=None, supersedes=None):
        """Optional completion assessment; capture failures never erase task status."""
        actor = self.structured_actor()[0] if self.env.get('ASHA_MANAGED_SESSION_ID') else self.actor()
        if (experience_file or experience_ref or supersedes) and state != 'finished':
            raise StoreError('experience requires an explicit finished report')
        if (experience_file or experience_ref) and not key:
            raise StoreError('experience completion requires a stable --key')
        with self._action_lock(actor['session_id']):
            from .session_experience import Experiences
            row = self.get(actor['session_id'])
            if row['generation'] != actor['generation'] or row['lifecycle'] not in ACTIVE_LIFECYCLES:
                raise StoreError('stale or inactive session reporter')
            if state == 'finished':
                from .session_completion import check
                try:
                    check(self, row)
                except (ValueError, OSError) as exc:
                    raise StoreError('verified project-memory handoff required: ' + str(exc)) from exc
            experiences = Experiences(self)
            capture = None
            request = row.get('experience_request') or {}
            if (request.get('generation') != row['generation']
                    or request.get('assignment_epoch') != row.get('assignment_epoch')):
                request = {}
            attachment = bool(experience_file or experience_ref)
            followup = attachment and request.get('key') == key
            if state == 'finished':
                if not attachment and experiences.completion_enabled(row):
                    if not request:
                        issued = str(uuid.uuid4())
                        request = {'key': issued, 'generation': row['generation'], 'status': 'pending',
                                   'assignment_epoch': row.get('assignment_epoch'),
                                   'text': experiences.completion_text(issued)}
                    key = request['key']
                capture = experiences.optional_capture(row, source='completion', key=key or str(uuid.uuid4()),
                    experience_file=experience_file, experience_ref=experience_ref, supersedes=supersedes,
                    requested=bool(request))
                if followup and capture.get('report_id'):
                    request = dict(request, status='answered')
            # Issued-key attachments amend capture, never the original task result.
            reported_body = None if followup else body
            from contextlib import ExitStack
            with ExitStack() as stack:
                validate = None
                if state == 'finished':
                    from .session_completion import binding, snapshot
                    stack.enter_context(self._observation_lock(row['session_id']))
                    digests = stack.enter_context(snapshot(row))
                    def validate(c, current):
                        if binding(current) != binding(row):
                            raise StoreError('work changed during completion reporting; finalize again')
                        check(self, current, digests=digests, connection=c)
                if row['transport'] == 'structured':
                    result = self._update(row['session_id'], expected_generation=row['generation'], validate_fn=validate,
                                          result=text(reported_body, 'report', 16000), activity=state)
                elif state == 'finished':
                    result = self._observe(self.get(row['session_id']), None, state=state, body=reported_body,
                        native_id=native_id, tool_kind='report', tool_token='unknown', validate_fn=validate)
                else:
                    result = self.observe(None, state=state, body=reported_body, native_id=native_id)
            if capture is not None:
                changes = {'capture': capture}
                if request:
                    changes['experience_request'] = request
                result = self._update(row['session_id'], expected_generation=row['generation'], **changes)
            return result

    def _deliver(self, row, record):
        """Queue the close request at the supported seam; delivery itself is observed later."""
        body = closure.request_text(row, record)
        key = closure.message_key(record)
        if row['transport'] == 'structured':
            from .session_store import SessionStore
            try:
                with SessionStore(self.config) as sessions:
                    message = sessions.enqueue(row['session_id'], body, key=key)
            except StoreError as exc:
                return closure.transition(record, 'undeliverable', last_error=str(exc))
            wake = self._wake_structured(row['session_id'])
            detail = wake['dispatch_warning'] or 'queued for the next structured turn'
            return closure.transition(record, 'pending-delivery', delivery=dict(record['delivery'], channel='structured-turn',
                                      detail=detail, message_id=message['message_id']))
        with mutation_guard(self.config), self.database() as db, db.transaction(write=True) as c:
            # A re-armed request supersedes its earlier unread copies; one outstanding instruction per request.
            c.execute("UPDATE hub_messages SET state='superseded' WHERE session_id=? AND state='queued' AND delivery_key LIKE ? AND delivery_key!=?",
                      (row['session_id'], closure.MESSAGE_KEY_PREFIX + '%', key))
            old = c.execute('SELECT message_id FROM hub_messages WHERE session_id=? AND delivery_key=?', (row['session_id'], key)).fetchone()
            if old:
                mid = old[0]
            else:
                mid = str(uuid.uuid4())
                c.execute('INSERT INTO hub_messages VALUES(?,?,?,?,?,?,?)',
                          (mid, row['session_id'], key, body, digest(body), 'queued', time.time()))
        channel = 'stop-hook' if row['harness'] in closure.STOP_HOOK_HARNESSES else 'queued-message'
        detail = ('delivered as the Stop decision when the current turn ends, or when the worker reads messages'
                  if channel == 'stop-hook' else 'queued until the worker reads messages; no Stop seam on this harness')
        record = closure.transition(record, 'pending-delivery', delivery=dict(record['delivery'], channel=channel,
                                    detail=detail, message_id=mid))
        if channel == 'queued-message':
            return self._continue_idle_close(row, record)
        if row.get('native_activity', row['activity']) != 'working' or not closure.recent_native_observation(row):
            return self._inject_close(row, record)
        return record

    def _idle_input(self, row, line):
        """Type one line into an owned, detached, idle terminal pane (#96).

        Returns ``(outcome, reason)``. ``typed`` means pasted and submitted.
        ``ineligible`` (no idle boundary, open tool, unsupported harness) and
        ``unknown`` (no input line visible) are the only refusals after which an
        automatic restart may still be considered (``RESTARTABLE_REFUSALS``).
        Every other outcome may mean a person or a draft is at the pane:
        ``ownership``, ``attached`` (including an attach-detach cycle), ``mode``,
        ``unfenced`` (no attach generation to guard with), ``occupied``,
        ``stale`` (new native activity after the idle boundary), ``partial``
        (typed but not submitted) and ``error``.

        Delivery is fenced to the observed idle boundary without holding any
        lock that native hook reports wait on. The native hook bumps the pane's
        event sequence before it reports, and the hub records the sequence it
        reported; delivery requires the recorded sequence to equal the pane's
        (no report pending, none lost) and tmux re-checks that sequence, the
        attach generation, ownership, detachment and mode in the same commands
        that paste and press Enter. The hub row is re-read right before the
        paste and before Enter, and the whole input region must hold exactly
        the typed text before Enter. Nothing here retries or waits.
        """
        from .pane_input import INJECTABLE_HARNESSES, composer_holds, flatten, input_line_state
        if getattr(self.config, 'idle_delivery', False) is not True:
            # Experimental and off by default: no tmux read or input at all.
            return 'disabled', 'idle-pane typing is disabled (control.idle_delivery is off)'
        if row['transport'] != 'terminal' or row['harness'] not in INJECTABLE_HARNESSES:
            return 'ineligible', f"typing into a {row['harness']} {row['transport']} session is not supported"
        if (row['lifecycle'] not in {'open', 'closing'} or row.get('native_activity') != 'idle'
                or row['activity'] == 'needs-input'):
            return 'ineligible', 'no observed idle turn boundary'
        if _open_tools(row):
            return 'ineligible', 'a native tool is still open'
        sid, fence, text = row['session_id'], _idle_fence(row), flatten(line)
        try:
            record = RoomStore(self.config).read(row['room_id'])
            state, detail = _owned_state(record, self.tmux)
            if state != 'open':
                return 'ownership', 'room ownership is ' + state + ': ' + detail
            facts = self.tmux.room_input_facts(record['tmux']['pane_id'])
            if facts.attached:
                return 'attached', 'the pane is attached by a client'
            status, detail = input_line_state(row['harness'], facts.screen)
            if status != 'empty':
                return status, 'the input line is not proven empty (' + detail + ')'
            sequence = int(facts.event_sequence)

            def moved():
                current = self.get(sid)
                if _idle_fence(current) != fence or _open_tools(current):
                    return 'new native activity was observed after the idle boundary'
                if (current.get('event_sequence') != sequence
                        or current.get('event_sequence_pane') != record['tmux']['pane_id']):
                    return (f'a native event is pending or its observation was lost (pane event '
                            f'{sequence}, recorded {current.get("event_sequence")})')
                return None

            def confirm(screen):
                if not composer_holds(row['harness'], screen, text):
                    return 'the input region does not hold exactly the typed text'
                return moved()

            stale = moved()
            if stale:
                return 'stale', stale
            self.tmux.inject_owned_room_input(**_action_identity(record), text=text,
                attach_generation=facts.attach_generation, event_sequence=facts.event_sequence,
                ready=moved, confirm=confirm)
        except RoomInputRefused as exc:
            return exc.category, str(exc)[:500]
        except (AttributeError, OSError, ValueError) as exc:
            return 'error', str(exc)[:500]
        return 'typed', 'typed into the owned idle pane'

    def _inject_close(self, row, record):
        """Deliver a pending close request by typing it at a verified idle boundary."""
        outcome, reason = self._idle_input(self.get(row['session_id']), closure.request_text(row, record))
        if outcome != 'typed':
            return dict(record, attachment_required=True, input_refusal=outcome,
                        last_error='Control did not submit the close request: ' + reason + '; needs attach')
        with mutation_guard(self.config), self.database() as db, db.transaction(write=True) as c:
            c.execute("UPDATE hub_messages SET state='acknowledged' WHERE message_id=? AND state='queued'",
                      (record['delivery']['message_id'],))
        delivered = closure.mark_delivered(record, closure.INJECTION_CHANNEL,
            detail='typed into the owned idle pane as a prompt', message_id=record['delivery']['message_id'])
        return dict(delivered, last_error=None)

    def _continue_idle_close(self, row, record):
        """C7: wake only an observed idle owned conversation, never type into a pane."""
        row = self.get(row['session_id'])  # A hook may have observed new work during the ownership probe.
        current = row.get('closure')
        if current and current['request_id'] == record['request_id'] and current.get('handoff'):
            return current
        activity = row.get('native_activity', row['activity'])
        at = row.get('native_observed_at', row.get('observed_at'))
        recent = at is not None and 0 <= time.time() - at <= 300
        if recent and (activity == 'working' or row['activity'] == 'needs-input'):
            return record
        if activity == 'idle' and row['activity'] != 'needs-input':
            # Typing at the idle prompt keeps the same process; a person at the
            # pane (attached, or text in the input line) forbids the restart too.
            typed = self._inject_close(row, record)
            if typed['state'] == 'delivered' or typed.get('input_refusal') not in RESTARTABLE_REFUSALS:
                return typed
        if (not recent or activity != 'idle' or row['harness'] not in {'claude', 'codex'} or not row.get('native_id')):
            return closure.transition(record, 'unanswered', attachment_required=True,
                last_error='no proven Stop channel; idle native resume unavailable or activity unknown; attachment required')
        sid = row['session_id']
        self._update(sid, expected_generation=row['generation'], lifecycle='closing', closure=record)
        # Observe uses this same short lock. No database transaction spans the
        # Room operation: SQLite-backed Room custody needs its own writer.
        with self._observation_lock(sid):
            fresh = self.get(sid)
            latest = fresh.get('closure')
            if latest and latest.get('handoff'):
                return latest
            observed_at = fresh.get('native_observed_at', fresh.get('observed_at'))
            idle = fresh.get('native_activity', fresh['activity']) == 'idle'
            if (fresh['generation'] != row['generation'] or fresh['lifecycle'] != 'closing'
                    or not idle or fresh['activity'] == 'needs-input' or observed_at is None
                    or not 0 <= time.time() - observed_at <= 300):
                return latest or record
            try:
                # Never kill a Room a person attached to after the probes.
                close_room(RoomStore(self.config), fresh['room_id'], tmux=self.tmux, detached_only=True)
            except RoomInputRefused as exc:
                refused = dict(record, attachment_required=True, input_refusal=exc.category,
                               last_error='Control did not restart the session for close: ' + str(exc)[:300] + '; needs attach')
                # The closing row above already holds ``record``; persist the refusal itself.
                return self._update(sid, expected_generation=row['generation'], closure=refused)['closure']
            self._update(sid, expected_generation=row['generation'], lifecycle='stopped')
        generation = row['generation'] + 1
        record = dict(record, generation=generation, attachment_required=False, input_refusal=None,
                      last_error=None, continued_from_generation=row['generation'],
                      delivery=dict(record['delivery'], channel='native-resume',
                                    detail='continuing the same native conversation for close'))
        next_row = self._update(sid, lifecycle='starting', generation=generation, closure=record,
            room_id=str(uuid.uuid4()), room_history=row.get('room_history', []) + [row['room_id']],
            activity='unknown', observed_at=None, native_activity='unknown', native_observed_at=None, event_sequence=None,
            question=None, learning_ids=[], experience_request=None, capture={})
        try:
            self._start(next_row, closure.request_text(next_row, record), closing=True)
        except (OSError, ValueError) as exc:
            record = closure.transition(record, 'unanswered', attachment_required=True,
                                        last_error='native close resume failed: ' + str(exc)[:500])
            # A failure before Room creation has no live attach target. Preserve
            # interrupted recovery so stop/force-close can dismiss its intent.
            self._update(sid, lifecycle='interrupted', closure=record)
            return record
        def delivered(current, latest):
            if (current and current['request_id'] == record['request_id'] and current['generation'] == generation
                    and current['state'] == 'pending-delivery'):
                return closure.mark_delivered(current, 'native-resume',
                    detail='close request supplied as native conversation continuation', message_id=record['delivery']['message_id'])
            return current
        latest = self._update(sid, expected_generation=generation, closure_fn=delivered)
        with mutation_guard(self.config), self.database() as db, db.transaction(write=True) as c:
            c.execute("UPDATE hub_messages SET state='acknowledged' WHERE message_id=? AND state='queued'",
                      (record['delivery']['message_id'],))
        return latest['closure']

    def stop_decision(self, row, *, stop_hook_active=False):
        """Stop-hook seam: the pending close request as the harness's own block decision.

        ``row`` is the verified reporter's own session. Nothing is persisted
        here: the caller prints the decision first and then calls
        ``confirm_delivery``, so a hook killed at its time budget re-emits the
        same request at the next Stop instead of recording a delivery the agent
        never saw. A delivered request whose continued turn ends without a
        handoff is marked ``unanswered`` at that next Stop.
        """
        if row['lifecycle'] != 'closing' or not row.get('closure'):
            return None
        with self._action_lock(row['session_id']):
            row = self.get(row['session_id'])
            record = row.get('closure')
            if row['lifecycle'] != 'closing' or not record or record['generation'] != row['generation']:
                return None
            from .session_completion import check
            try:
                check(self, row, idle=True)
                return None  # An explicit save already finalized this turn.
            except (ValueError, OSError):
                pass
            if record['state'] == 'pending-delivery' and row['harness'] in closure.STOP_HOOK_HARNESSES and not stop_hook_active:
                # stop_hook_active means this Stop already follows a hook block; never chain blocks.
                return closure.StopDecision(closure.request_text(row, record), receipt=closure.receipt_for(record))
            if record['state'] == 'delivered' and not record.get('handoff'):
                self._update(row['session_id'], expected_generation=row['generation'],
                             closure_fn=lambda current, _row: closure.transition(current, 'unanswered')
                             if current and current['state'] == 'delivered' and not current.get('handoff') else current)
        return None

    def confirm_delivery(self, row, receipt=None):
        """Record that a printed Stop decision reached the harness.

        The receipt names the exact close request and delivery attempt the
        decision was emitted for (``StopDecision.receipt``); without one it is
        taken from the row the caller acted on. A receipt for any other request,
        attempt or generation is stale and changes nothing, so a replacement
        request keeps its own pending delivery. Idempotent.
        """
        if receipt is None:
            receipt = closure.receipt_for(row.get('closure')) if row.get('closure') else None
        if not receipt:
            return {'confirmed': False, 'reason': 'no delivery receipt'}
        outcome = {'confirmed': False, 'reason': 'stale receipt'}
        def mark(record, current):
            if not record or closure.receipt_for(record) != receipt or record['generation'] != current['generation']:
                return record
            if record['state'] != 'pending-delivery' or current['lifecycle'] != 'closing':
                outcome.update(reason='already delivered' if record['state'] != 'pending-delivery' else 'session no longer closing')
                return record
            outcome.update(confirmed=True, reason='delivered')
            return closure.mark_delivered(record, 'stop-hook', detail='emitted as the Stop hook decision',
                                          message_id=record['delivery'].get('message_id'))
        with self._action_lock(row['session_id']):
            self._update(row['session_id'], closure_fn=mark)
        return outcome

    def resume(self, sid, *, prompt, expected_digest=None, learning_ids=None):
        from .sessions import refuse_managed_operator
        refuse_managed_operator(self.config, self.env)
        with self._action_lock(sid):
            return self._resume(sid, prompt=prompt, expected_digest=expected_digest, learning_ids=learning_ids)

    def _resume(self, sid, *, prompt, expected_digest, learning_ids=None):
        prompt = text(prompt, 'continuation')
        row = self.get(sid)
        if row['lifecycle'] == 'closing':
            raise StoreError('a close request is pending; let it finish, re-run close, or force-close first')
        if row['transport'] == 'structured':
            from .session_store import SessionStore
            if not self._has_structured_record(sid):
                row = self._update(sid, lifecycle='starting', generation=row['generation'] + 1,
                                   learning_ids=learning_ids, capture={}, closure=None,
                                   closure_history=row.get('closure_history', []) +
                                   ([row['closure']] if row.get('closure') else []))
                return self._start(row, row['prompt'] + '\nContinuation:\n' + prompt)
            if not expected_digest:
                raise StoreError('Inspect session recovery and supply --digest to resume structured execution')
            from . import session_guidance as guidance
            self.initialize()
            next_row = dict(row, generation=row['generation'] + 1)
            block, manifest = guidance.resolve(self, next_row, learning_ids)
            manifest = guidance.planned(manifest, prompt, block)
            def retained(c, message):
                current = json.loads(c.execute('SELECT payload FROM hub_sessions WHERE session_id=?', (sid,)).fetchone()[0])
                current.update(generation=next_row['generation'], lifecycle='open', closure=None, capture={},
                               current_assignment=prompt,
                               closure_history=current.get('closure_history', []) + ([current['closure']] if current.get('closure') else []))
                self._save(c, current)
                guidance.carry_queued_in(c, current, row['generation'])
                guidance.retain_in(c, current, message['delivery_key'], manifest)
            with SessionStore(self.config) as sessions:
                sessions.resume(sid, prompt=text(prompt + block, 'guided continuation'), expected_digest=expected_digest,
                                on_retained=retained)
            self._update(sid, lifecycle='open')
            self._wake_structured(sid)
            return self.show(sid)
        if row['lifecycle'] in {'starting', 'interrupted'} or self.show(sid)['activity'] == 'exited':
            self._stop(row, close=False)
            row = self.get(sid)
        if row['lifecycle'] not in {'stopped', 'closed'}:
            raise StoreError('stop the previous session before resuming')
        if row['native_id'] and row['harness'] not in {'claude', 'codex'}:
            raise StoreError('native resume unavailable for this harness; launch a new conversation')
        if not row['native_id']:
            prompt = ('Previous assignment:\n' + row['prompt'] +
                      ('\nPrevious reported result:\n' + row['result'] if row.get('result') else '') +
                      '\nOperator continuation:\n' + prompt)
        row = self._update(sid, lifecycle='starting', room_id=str(uuid.uuid4()),
                           room_history=row.get('room_history', []) + [row['room_id']],
                           closure=None, closure_history=row.get('closure_history', []) + ([row['closure']] if row.get('closure') else []),
                           generation=row['generation'] + 1, activity='unknown', observed_at=None, learning_ids=learning_ids,
                           native_activity='unknown', native_observed_at=None, event_sequence=None,
                           question=None, reason='Resuming native conversation' if row['native_id'] else 'Starting with explicit continuation context; native resume ID unavailable')
        return self._start(row, text(prompt, 'continuation'))

    def send(self, sid, body, *, key, learning_ids=None):
        from .sessions import refuse_managed_operator
        from . import session_guidance as guidance
        refuse_managed_operator(self.config, self.env)
        with self._action_lock(sid):
            row = self.get(sid)
            if row['lifecycle'] == 'closed':
                raise StoreError('session is closed')
            body, key = text(body, 'message'), text(key, 'delivery key', 256)
            self.initialize()
            with self.database() as db, db.transaction() as c:
                frozen = c.execute('SELECT manifest FROM hub_guidance_exposures WHERE session_id=? AND generation=? AND delivery_key=?',
                                   (sid, row['generation'], key)).fetchone()
            if frozen:
                manifest = json.loads(frozen[0])
                selection = ('automatic' if learning_ids is None and row['profile'] == 'worker'
                             else 'explicit' if learning_ids else 'none')
                if (manifest.get('selection', 'explicit' if manifest['selected'] else 'none') != selection
                        or (selection == 'explicit' and manifest['selected'] != learning_ids)
                        or manifest.get('base_digest') != digest(body)):
                    raise StoreError('delivery key already has different content or guidance')
                block = manifest.get('planned_block', '')
            else:
                block, manifest = guidance.resolve(self, row, learning_ids)
                manifest = guidance.planned(manifest, body, block)
            if row['transport'] == 'structured':
                from .session_store import SessionStore
                with SessionStore(self.config) as sessions:
                    message = sessions.enqueue(sid, text(body + block, 'guided input'), key=key,
                        on_retained=lambda c, message: guidance.retain_in(c, row, key, manifest))
                return {**message, **self._wake_structured(sid)}
            with mutation_guard(self.config), self.database() as db, db.transaction(write=True) as c:
                old = c.execute('SELECT * FROM hub_messages WHERE session_id=? AND delivery_key=?', (sid, key)).fetchone()
                exposure = c.execute('SELECT manifest FROM hub_guidance_exposures WHERE session_id=? AND generation=? AND delivery_key=?',
                                     (sid, row['generation'], key)).fetchone()
                if old:
                    if old['digest'] != digest(body) or (json.loads(exposure[0])['selected'] if exposure else []) != manifest['selected']:
                        raise StoreError('delivery key already has different content or guidance')
                    return dict(old, delivery='retained', delivery_detail='already retained; not typed again')
                mid = str(uuid.uuid4())
                c.execute('INSERT INTO hub_messages VALUES(?,?,?,?,?,?,?)',
                          (mid, sid, key, body, digest(body), 'queued', time.time()))
                current = json.loads(c.execute('SELECT payload FROM hub_sessions WHERE session_id=?', (sid,)).fetchone()[0])
                current['assignment_epoch'] = str(uuid.uuid4())
                self._save(c, current)
                guidance.retain_in(c, row, key, manifest)
                message = dict(c.execute('SELECT * FROM hub_messages WHERE message_id=?', (mid,)).fetchone())
            # The retained message stays the delivery contract; an idle session is
            # told where to read and acknowledge it, never handed the body as keys.
            outcome, reason = self._idle_input(self.get(sid), (
                f'Asha Control message {mid} is queued for this session. Read it with '
                '`asha control session messages`, act on it, then acknowledge it with '
                f'`asha control session ack-message {mid}`.'))
            if outcome == 'typed':
                return dict(message, delivery='injected', delivery_detail=reason)
            return dict(message, delivery='queued-until-read', delivery_detail='not typed: ' + reason)

    def messages(self, sid, *, offset=0, limit=100):
        identifier(sid)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise StoreError('invalid message page')
        with self.database() as db, db.transaction() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM hub_messages WHERE session_id=? ORDER BY created_at,message_id LIMIT ? OFFSET ?', (sid, limit, offset))]
        from .session_guidance import delivery, offered
        current = self.get(sid)
        for message in rows:
            rendered, manifest = delivery(self, current, message['delivery_key'], message['body'])
            message['body'] = rendered
            if manifest:
                retained = False
                if message['state'] == 'queued' and current['lifecycle'] in ACTIVE_LIFECYCLES:
                    retained = offered(self, current, message['delivery_key'], manifest)
                message['guidance_delivery'] = {k: v for k, v in manifest.items() if k != 'planned_block'}
                if retained:
                    message['delivery_digest'] = manifest['delivery_digest']
        return rows

    def message_page(self, sid, *, offset=0, limit=100):
        rows = self.messages(sid, offset=offset, limit=limit)
        with self.database() as db, db.transaction() as c:
            total = c.execute('SELECT COUNT(*) FROM hub_messages WHERE session_id=?', (sid,)).fetchone()[0]
        end = offset + len(rows)
        return dict(messages=rows, total=total, complete=end >= total, next_offset=end if end < total else None,
                    delivery='Reading does not acknowledge; use ack-message after processing')

    def actor(self):
        from .harness import caller_descends_from
        sid = self.env.get('ASHA_HUB_SESSION_ID', '')
        row = self.get(identifier(sid))
        if str(row['generation']) != self.env.get('ASHA_HUB_GENERATION') or row['lifecycle'] not in ACTIVE_LIFECYCLES:
            raise StoreError('stale or inactive session reporter')
        record = RoomStore(self.config).read(row['room_id'])
        state, _ = _owned_state(record, self.tmux)
        if state != 'open':
            raise StoreError('session ownership unavailable')
        facts = self.tmux.pane_facts(record['tmux']['pane_id'])
        if not facts.pane_pid or not caller_descends_from(facts.pane_pid, require_complete=True):
            raise StoreError('reporter is not part of this session')
        return row

    def observe(self, event, *, native_id=None, state=None, body=None, tool_kind='work', tool_token='unknown',
                sequence=None, sequence_pane=None):
        actor = self.actor()
        if sequence is not None:
            # Only a bump of this Room's own pane counts; any other pane's
            # counter could coincide with ours and hide an event.
            try:
                room_pane = RoomStore(self.config).read(actor['room_id'])['tmux']['pane_id']
            except (KeyError, OSError, TypeError, ValueError):
                room_pane = None
            if room_pane is None or sequence_pane != room_pane:
                sequence = None
        with self._observation_lock(actor['session_id']):
            row = self.get(actor['session_id'])
            if row['generation'] != actor['generation'] or row['lifecycle'] not in ACTIVE_LIFECYCLES:
                raise StoreError('stale or inactive session reporter')
            return self._observe(row, event, native_id=native_id, state=state, body=body,
                                 tool_kind=tool_kind, tool_token=tool_token, sequence=sequence,
                                 sequence_pane=sequence_pane)

    def _observe(self, row, event, *, native_id, state, body, tool_kind, tool_token, validate_fn=None,
                 sequence=None, sequence_pane=None):
        activity = EVENTS.get(event) if event else state
        if activity not in {'idle', 'working', 'needs-input', 'finished', 'exited'}:
            raise StoreError('invalid session observation')
        changes = dict(activity=activity, activity_source='hook' if event else 'report',
                       observed_at=time.time(), reason=event or 'Reported by worker')
        if event:
            changes.update(native_activity=activity, native_observed_at=changes['observed_at'])
            # The pane event sequence the native hook reported (#96). An event
            # without one leaves the sequence unknown, which refuses typing
            # until a sequenced event lands; a lower late one never rewinds it.
            # The sequence is bound to the pane it counts: a new Room restarts it.
            valid = type(sequence) is int and 0 < sequence < FENCE_LIMIT and bool(sequence_pane)
            previous = row.get('event_sequence')
            same_pane = previous is not None and row.get('event_sequence_pane') == sequence_pane
            changes['event_sequence'] = (None if not valid
                                         else max(previous, sequence) if same_pane else sequence)
            changes['event_sequence_pane'] = sequence_pane if valid else None
        if event == 'turn-stopped' and row.get('active_tools'):
            # A native Stop means no tool of that turn is still running. Starts
            # without an end (failed, denied or interrupted calls) are stale, so
            # a Stop-hook continuation can still finalize with a sole handoff.
            from .session_completion import observe_stop
            observed = observe_stop(dict(row))
            changes.update(active_tools=observed['active_tools'])
            if observed.get('completion') is not row.get('completion'):
                changes['completion'] = observed['completion']
        if event == 'prompt-submitted':
            changes['assignment_epoch'] = str(uuid.uuid4())
            if row.get('native_activity') in {'idle', 'exited'}:
                # A new turn after an observed stop recovers dropped/failed tool
                # callbacks. It never revives the prior turn's completion.
                changes['active_tools'] = {}
        if event == 'prompt-submitted' or (not event and state == 'working'):
            changes['work_epoch'] = str(uuid.uuid4())
            changes['completion_report'] = None
        if event in {'tool-started', 'tool-completed'}:
            from .session_completion import observe_tool
            if tool_kind not in {'work', 'report', 'finalizer'} or not isinstance(tool_token, str) or len(tool_token) > 128:
                raise StoreError('invalid tool completion metadata')
            observed = observe_tool(dict(row), event, tool_kind, tool_token)
            for field in ('active_tools', 'work_epoch', 'completion_report', 'completion'):
                if field in observed:
                    changes[field] = observed[field]
        if not event and activity == 'finished':
            changes['completion_report'] = dict(generation=row['generation'],
                assignment_epoch=row.get('assignment_epoch'), reported_at=changes['observed_at'])
        elif not event:
            changes['completion_report'] = None
        if event == 'permission-requested':
            changes['question'] = None
        if native_id:
            native_id = text(native_id, 'native session ID', 512)
            if native_id.startswith('-') or not native_id.isprintable():
                raise StoreError('invalid native session ID')
            changes['native_id'] = native_id
        if body:
            changes['reason'] = text(body, 'report', 16000)
            if activity == 'finished':
                changes['result'] = body
            if activity == 'needs-input':
                changes['question'] = body
        if activity in {'working', 'idle', 'finished'}:
            changes['question'] = None
        # The report command itself fires a PostToolUse hook. Only new user
        # input (or another explicit report) clears a retained result/question.
        explicit_question = row['activity'] == 'needs-input' and (
            row.get('activity_source') == 'report' or row.get('question'))
        if event in {'tool-completed', 'turn-stopped', 'session-ended'} and (row['activity'] == 'finished' or explicit_question):
            changes.update(activity=row['activity'], reason=row['reason'], question=row['question'],
                           activity_source=row.get('activity_source', 'report'))
        closure_fn = None
        if event == 'session-ended':
            from .session_experience import Experiences
            Experiences(self).reconcile_completion(row, observed_exit=True)
            def closure_fn(record, current):
                if (current['lifecycle'] == 'closing' and record and record['generation'] == current['generation']
                        and record['state'] in {'pending-delivery', 'delivered'}):
                    return closure.transition(record, 'undeliverable',
                                              last_error='the harness exited before answering the close request')
                return record
        return self._update(row['session_id'], expected_generation=row['generation'], closure_fn=closure_fn,
                            validate_fn=validate_fn, **changes)

    def acknowledge(self, mid, *, delivery_digest=None):
        row = self.actor()
        from .session_guidance import receipt_in, supplied
        with self.database() as db, db.transaction() as c:
            message = c.execute('SELECT * FROM hub_messages WHERE message_id=? AND session_id=?', (mid, row['session_id'])).fetchone()
        if not message:
            raise StoreError('message does not belong to this session')
        with mutation_guard(self.config), self.database() as db, db.transaction(write=True) as c:
            current = json.loads(c.execute('SELECT payload FROM hub_sessions WHERE session_id=?', (row['session_id'],)).fetchone()[0])
            if current['generation'] != row['generation'] or current['lifecycle'] not in ACTIVE_LIFECYCLES:
                raise StoreError('stale or inactive session reporter')
            manifest = receipt_in(c, row, message['delivery_key'], delivery_digest)
            current['current_assignment'] = message['body']
            self._save(c, current)
            changed = c.execute("UPDATE hub_messages SET state='acknowledged' WHERE message_id=? AND session_id=?", (mid, row['session_id']))
            if not changed.rowcount:
                raise StoreError('message does not belong to this session')
            record = current.get('closure')
            # Reading the close request is delivery evidence for the queued channel.
            if (record and record['state'] == 'pending-delivery' and record['generation'] == current['generation']
                    and record['delivery'].get('message_id') == mid):
                current['closure'] = closure.mark_delivered(record, 'queued-message', detail='close request read and acknowledged', message_id=mid)
                self._save(c, current)
        recorded = supplied(self, row, message['delivery_key'], manifest) if manifest else False
        return {'message_id': mid, 'state': 'acknowledged', 'guidance_status': 'supplied' if recorded else 'unknown'}

    def structured_actor(self):
        """A structured worker proves itself through the managed-session anchor and its running close turn."""
        from .session_store import SessionStore, caller_anchor
        if self.env.get('ASHA_MANAGED_STATE_DIR') != str(self.config.tasks_dir.parent):
            raise StoreError('managed actor selected a different state root')
        anchor = caller_anchor(self.env)
        row = self.get(anchor['session_id'])
        if row['transport'] != 'structured' or row['lifecycle'] not in ACTIVE_LIFECYCLES:
            raise StoreError('stale or inactive session reporter')
        turn_id = self.env.get('ASHA_MANAGED_TURN_ID', '')
        identifier(turn_id)
        with SessionStore(self.config) as sessions, sessions.db.transaction() as c:
            turn = c.execute("SELECT t.state, m.delivery_key FROM session_turns t JOIN session_messages m ON m.message_id=t.message_id WHERE t.turn_id=? AND t.session_id=?", (turn_id, row['session_id'])).fetchone()
        if turn is None or turn['state'] != 'running':
            raise StoreError('handoff must come from the running close turn')
        return row, turn['delivery_key']

    def _handoff_actor(self, request_id=None):
        if self.env.get('ASHA_MANAGED_SESSION_ID'):
            row, key = self.structured_actor()
            record = row.get('closure')
            if request_id and (not record or key != closure.message_key(record)):
                raise StoreError('this turn did not receive the current close request')
            return row
        return self.actor()

    def handoff_read(self):
        """Live destination facts for the acting session; never writes."""
        row = self._handoff_actor()
        record = row.get('closure')
        memory = closure.memory_destination(row['project'])
        return {'session_id': row['session_id'], 'generation': row['generation'],
                'request_id': record['request_id'] if record else None,
                'attempt': record.get('attempts', 1) if record else None,
                'closure_state': record['state'] if record else None, 'memory': memory,
                'paths': {name: (memory['destination'] + '/' + name) if memory['destination'] else None
                          for name in closure.MEMORY_FILES}}

    def handoff(self, request_id, *, attempt=None, outcome=None, detail=None, active_file=None, decisions_file=None, expected=None,
                experience_file=None, experience_ref=None, supersedes=None, key=None):
        """Finalize current work, optionally acknowledging a bound close request."""
        request_id = identifier(request_id) if request_id else None
        actor = self._handoff_actor(request_id)
        with self._action_lock(actor['session_id']):
            row = self.get(actor['session_id'])
            from .session_completion import binding
            if binding(row) != binding(actor) or row['lifecycle'] not in ACTIVE_LIFECYCLES:
                raise StoreError('stale or inactive session reporter')
            if request_id is None:
                if any((experience_file, experience_ref, supersedes, key)):
                    raise StoreError('completion experience belongs on session report --state finished')
                if row.get('closure') and row['closure']['state'] not in closure.TERMINAL_STATES:
                    raise StoreError('a close request is pending; handoff must name its --request')
                return self._finalize(row, outcome=outcome, detail=detail, active_file=active_file,
                                      decisions_file=decisions_file, expected=expected)
            closure.validate_handoff_request(row.get('closure'), row, request_id)
            attempts = row['closure'].get('attempts', 1)
            if attempt is None and attempts > 1:
                # A re-issued request binds only an explicit selector; an old
                # reply that omits it must not satisfy the current attempt.
                raise StoreError(f'this close request was re-issued; name --attempt {attempts} from the delivered request')
            if attempt is not None and attempt != attempts:
                raise StoreError('stale close delivery attempt')
            from .session_experience import Experiences
            capture = None
            if not any((experience_file, experience_ref, supersedes, key)):
                capture = Experiences(self).retained_close_capture(row, request_id)
            if capture is None:
                capture = Experiences(self).optional_capture(row, source='close', key=key or request_id, close_request_id=request_id,
                    requested=bool(row['closure'].get('capture', {}).get('requested')),
                    experience_file=experience_file, experience_ref=experience_ref, supersedes=supersedes)
            row = self._update(row['session_id'], expected_generation=row['generation'],
                closure_fn=lambda record, current: dict(record, capture={**record.get('capture', {}), **capture}))
            if binding(row) != binding(actor):
                raise StoreError('work changed during handoff; finalize the current turn')
            return self._handoff(row, request_id, outcome=outcome, detail=detail, active_file=active_file,
                                 decisions_file=decisions_file, expected=expected)

    def _finalize(self, row, *, outcome, detail, active_file, decisions_file, expected):
        from .session_completion import issue, invalidate
        invalidate(self, row, 'handoff in progress')
        publication = None
        try:
            if active_file or decisions_file:
                if outcome not in {None, 'published'} or not (active_file and decisions_file):
                    raise StoreError('publication requires both draft files and no other outcome')
                # Scope/identity/silence are checked before any write.
                from .session_completion import snapshot
                with snapshot(row):
                    pass
                publication = closure.publish_handoff(row['project'], active_file, decisions_file,
                                                       expected=expected or {})['publication']
                outcome = 'published'
                detail = detail or 'Published verified project memory'
            if outcome not in closure.OUTCOMES or (outcome == 'published' and not publication):
                raise StoreError('an explicit outcome or both draft files are required')
            detail = text(detail, 'handoff detail', 4000)
            if outcome in closure.ACKNOWLEDGED:
                receipt = issue(self, row, outcome=outcome, detail=detail, publication=publication)
            else:
                receipt = dict(status=outcome, detail=detail)
                self._update(row['session_id'], completion=receipt)
        except (ValueError, OSError) as exc:
            self._update(row['session_id'], completion=dict(status='blocked', detail=str(exc)[:1000]))
            raise StoreError('handoff refused: ' + str(exc)) from exc
        return dict(session_id=row['session_id'], outcome=outcome, completion=receipt, git_invoked=False)

    def _handoff(self, row, request_id, *, outcome, detail, active_file, decisions_file, expected):
        record = row.get('closure')
        closure.validate_handoff_request(record, row, request_id)
        publication = None
        if active_file or decisions_file or outcome == 'no-durable-update':
            from .session_completion import snapshot
            try:
                with snapshot(row):
                    pass
            except (ValueError, OSError) as exc:
                raise StoreError('project memory is unavailable: ' + str(exc)) from exc
        if active_file or decisions_file:
            if outcome not in {None, 'published'} or not (active_file and decisions_file):
                raise StoreError('publication requires both draft files and no other outcome')
            outcome = 'published'
            if not record['memory']['available']:
                raise StoreError('project memory is unavailable: ' + str(record['memory']['reason']))
            try:
                publication = closure.publish_handoff(row['project'], active_file, decisions_file, expected=expected or {})
            except (OSError, ValueError) as exc:
                # Retryable: the request stays open; the agent re-reads and retries or reports blocked.
                self._update(row['session_id'], expected_generation=row['generation'],
                             closure=closure.transition(record, record['state'], last_error=str(exc)[:1000]))
                raise StoreError('handoff publication refused: ' + str(exc)) from exc
            detail = detail or 'published verified project memory'
        if outcome is None:
            raise StoreError('an outcome or both draft files are required')
        detail = text(detail, 'handoff detail', 4000)
        updated = closure.record_handoff(record, row, outcome, detail, publication=publication)
        from .session_completion import issue
        if outcome in closure.ACKNOWLEDGED:
            try:
                completion = issue(self, row, outcome=outcome, detail=detail,
                    publication=publication and publication['publication'], request=closure.receipt_for(record))
                if completion['status'] != 'ready':
                    updated = closure.transition(updated, 'handoff-failed', last_error=completion['detail'])
            except (ValueError, OSError) as exc:
                completion = dict(status='blocked', detail=str(exc))
                updated = closure.transition(updated, 'handoff-failed', last_error=str(exc))
        else:
            completion = dict(status=outcome, detail=detail)
        self._update(row['session_id'], completion=completion)
        self._update(row['session_id'], expected_generation=row['generation'], closure=updated)
        return {'session_id': row['session_id'], 'request_id': request_id, 'outcome': outcome,
                'closure_state': updated['state'], 'memory': updated['memory'], 'handoff': updated['handoff'],
                'completion': completion,
                'capture': updated.get('capture', {'status': 'disabled', 'report_id': None}),
                'git_invoked': False}
