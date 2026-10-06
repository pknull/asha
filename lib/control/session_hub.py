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
from . import session_usage
from .database import ControlDatabase, DATABASE_NAME
from .registry_guards import mutation_guard
from .rooms import (RoomStore, _owned_state, open_room, close_room, attach_room,
                    resolve_project)
from .session_selection import evidence as selection_evidence, requested
from .session_store import identifier, text, digest
from .store import StoreError
from .config import CLOSE_WAIT_LIMIT
from .tmux import TmuxAdapter


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
# Upper bound on the background task count a Stop may report (#99). A Stop
# that listed outstanding background work proved a wake-up is owed, so the
# five-minute staleness rules (dashboard demotion, close "needs attach") wait
# up to BACKGROUND_WAIT_SECONDS for the next native event instead.
BACKGROUND_TASK_LIMIT = 10000
BACKGROUND_WAIT_SECONDS = 4 * 3600


def waiting_on_background(row):
    """The last native Stop listed background work, and it is not too old to trust."""
    stamp = row.get('native_observed_at')
    return (bool(row.get('background_tasks')) and stamp is not None
            and 0 <= time.time() - stamp <= BACKGROUND_WAIT_SECONDS)
# A session that is closing gracefully still owns its process; its reporter,
# hooks and handoff remain valid until the close terminates it.
ACTIVE_LIFECYCLES = {'starting', 'open', 'closing'}
# Close takes the session's action lock with a bound (D9) and polls this often.
CLOSE_LOCK_SECONDS = 5
CLOSE_POLL_SECONDS = 0.5
# A hook report stamped this much older than the newest applied one is skipped
# (D2); anything older still applies, bounding a backward clock step.
STALE_REPORT_SECONDS = 30
# A live terminal session is observed ``launched`` until its first native hook
# event. Past this many seconds a hook harness reads "hooks not reporting"
# (#100): SessionStart fires within seconds of a healthy launch.
HOOK_SILENCE_SECONDS = 90
# Only these harnesses have a native hook bridge into the hub; Copilot and
# OpenCode sessions report explicitly and are never "hooks not reporting".
HOOK_REPORTING_HARNESSES = frozenset({'claude', 'codex'})
# A SessionStart whose payload names one of these sources starts a new native
# conversation in the pane (Claude and Codex /clear), so it may take over the
# generation's binding (F6). A nested `claude -p` starts as ``startup``.
REBIND_SOURCES = frozenset({'clear'})
# What the hook bridge observed, in the observed axis's words.
OBSERVED = {'working': 'working', 'idle': 'waiting', 'needs-input': 'waiting', 'exited': 'ended'}
# Explicit report states; working withdraws a standing report.
REPORT_STATES = frozenset({'needs-input', 'finished', 'working'})
# Rejected hook events are kept, not discarded, in a small local diagnostic.
REJECTION_LOG_NAME = 'hub-rejected-events.jsonl'
REJECTION_LOG_BYTES = 64 * 1024
# Hook reports that never changed a row although nothing refused them: a stale
# skip (D2) and a bridge call that ran out of its budget. Same bound and format
# as the rejection log; a cheap loss metric, not an ordering mechanism.
LOSS_LOG_NAME = 'hub-lost-events.jsonl'
LOSS_REASONS = frozenset({'stale-skip', 'bridge-timeout'})


def report_of(row):
    """The report axis: None, or ``{state, text, at}`` with state needs-input or finished.

    Only explicit reports and structured completion write it. Rows stored
    before the two-axis model (subtraction B2) carry a current finished report
    as ``completion_report`` instead; it reads as one until the row is next written.
    """
    if 'report' in row:
        return row['report']
    legacy = row.get('completion_report') or {}
    if legacy.get('generation') == row.get('generation') and legacy.get('assignment_epoch') == row.get('assignment_epoch'):
        return {'state': 'finished', 'text': row.get('result'), 'at': legacy.get('reported_at') or 0}
    return None


def settled(row):
    """D7: a finished report whose turn has ended.

    A structured report is made at the managed turn boundary. A terminal report
    needs a waiting observation emitted after it: a turn-ending Stop (#109)
    that lists no background work (#99), stamped later than the report. A Stop
    emitted before the report, however late it applies, any later hook event
    (#114) and a harness without a turn-end event (Copilot) leave it unsettled.
    """
    report = report_of(row) or {}
    if report.get('state') != 'finished':
        return False
    if row.get('transport') == 'structured':
        return True
    # Rows stored before subtraction B2 stamped the turn end on the report.
    since = row['waiting_since'] if 'waiting_since' in row else (row.get('completion_report') or {}).get('turn_ended_at')
    return since is not None and since > report['at']


def _observed(row, now):
    """(observed, activity, reason) for a live pane, from hook evidence alone.

    ``launched`` holds until the first native event. A working observation
    with nothing newer for five minutes reads unknown, unless its Stop listed
    background work (#99). Worker reports never change this axis.
    """
    stamp = row.get('native_observed_at')
    if not stamp:
        silent = now - (row.get('launched_at') or now)
        if silent <= HOOK_SILENCE_SECONDS:
            return 'launched', 'starting', row.get('reason')
        if row['harness'] not in HOOK_REPORTING_HARNESSES:
            # Copilot and OpenCode report explicitly; no native event is expected.
            return 'unknown', 'unknown', row.get('reason')
        return 'launched', 'unknown', (f'Hooks not reporting: no native event in the {int(silent // 60)} min since '
                                       'launch; attach to check the terminal, then run asha doctor')
    native = row.get('native_activity')
    if native == 'working' and now - stamp > closure.STALE_OBSERVATION_SECONDS and not waiting_on_background(row):
        return 'unknown', 'unknown', 'No recent observation; the harness may still be working'
    observed = OBSERVED.get(native, 'unknown')
    return observed, native if observed != 'unknown' else 'unknown', row.get('reason')


def _present_axes(row):
    """Overlay the report axis on the observed one: the shown activity, question and reason.

    A report reads as it lands. A question (a needs-input report, or a native
    permission request with its summary) needs the operator. A finished report
    reads working while the observed turn still runs, else finished. Once the
    process has ended only a finished report still counts.
    """
    report = report_of(row) or {}
    state = report.get('state')
    row['question'] = None
    if row['lifecycle'] in {'closed', 'stopped'} or row['process_state'] == 'ended':
        if state == 'finished':
            row['reported_activity'] = 'finished'
            if row['activity'] == 'exited':
                row['activity'] = 'finished'
        return
    asking = row['observed'] == 'waiting' and row.get('native_activity') == 'needs-input'
    if asking or state == 'needs-input':
        row['question'] = (row.get('asks') if asking else None) or report.get('text')
        row.update(activity='needs-input', reason=row['question'] or row.get('reason') or 'Input requested')
    elif state == 'finished':
        row['reason'] = report.get('text') or 'Reported finished'
        if row['observed'] == 'working' or (row['observed'] == 'waiting' and not settled(row)):
            row.update(activity='working', reported_activity='finished')
        else:
            row['activity'] = 'finished'


class LockTimeout(StoreError):
    """A bounded lock wait ran out: another holder kept the session's action lock."""


def _bounded_flock(fd, timeout):
    """Take the registry flock on ``fd`` within ``timeout`` seconds, or raise ``LockTimeout``."""
    import fcntl
    from .store import _HELD_REGISTRY_LOCKS
    metadata = os.fstat(fd)
    if (metadata.st_dev, metadata.st_ino) in _HELD_REGISTRY_LOCKS.get():
        return
    until = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= until:
                raise LockTimeout('the session is busy (a handoff may be publishing); retry') from None
            time.sleep(0.05)


def rejection_log_path(config):
    return config.tasks_dir.parent / REJECTION_LOG_NAME


def _outside_project(row, cwd):
    """True only for a usable absolute cwd that resolves outside the session's project."""
    if (not isinstance(cwd, str) or not cwd.startswith('/') or len(cwd) > 4096 or not cwd.isprintable()):
        return False
    project = os.path.realpath(row['project'])
    return os.path.commonpath([project, os.path.realpath(cwd)]) != project


def native_binding(row, event, native_id, cwd, source=None):
    """The generation's bound native conversation after this hook event (#100).

    Hook identity is inherited environment, which a shared harness process (or
    any other conversation under the same pane) can carry. The first native
    conversation of a generation binds it; resume is a new generation and binds
    afresh. Events from a different conversation are refused whatever their
    cwd. Only a SessionStart rebinds (F6): one whose payload source says /clear,
    or any once the bound conversation has ended. A nested `claude -p` in the
    pane starts as ``startup`` and never takes the binding. A rebinding comes
    from inside the project; the bound conversation may report from anywhere,
    because Claude's hook cwd follows Bash ``cd`` and EnterWorktree. Returns the
    new binding, or None when nothing changes.
    """
    binding = row.get('native_binding') or {}
    bound = binding.get('native_id') if binding.get('generation') == row['generation'] else None
    if not native_id or native_id == bound:
        return None
    if bound is not None and not (event == 'session-start'
                                  and (source in REBIND_SOURCES or binding.get('ended'))):
        raise StoreError('hook event names another native conversation than this session bound')
    if _outside_project(row, cwd):
        raise StoreError("hook cwd is outside this session's project and names another conversation")
    return dict(generation=row['generation'], native_id=native_id)


def _native_id(value):
    value = text(value, 'native session ID', 512)
    if value.startswith('-') or not value.isprintable():
        raise StoreError('invalid native session ID')
    return value


def loss_log_path(config):
    return config.tasks_dir.parent / LOSS_LOG_NAME


def _clip(value, limit):
    return str(value)[:limit] if value is not None else None


def _stamp(value):
    return value if type(value) in {int, float} and 0 < value < 1e12 else None


def record_rejection(config, env, *, event, native_id, error, cwd=None):
    """Append one refused hook event; keep the newest half once over budget. Never raises."""
    entry = dict(at=time.time(), event=_clip(event, 64), session_id=_clip(env.get('ASHA_HUB_SESSION_ID'), 64),
                 generation=_clip(env.get('ASHA_HUB_GENERATION'), 16), native_id=_clip(native_id, 128),
                 cwd=_clip(cwd, 512),
                 pid=os.getpid(), ppid=os.getppid(), error=_clip(error, 300))
    _append_bounded(rejection_log_path(config), entry)


def record_loss(config, env, *, event, reason, native_id=None, emitted_at=None, newest=None, budget=None):
    """Append one hook report that changed nothing without being refused. Never raises."""
    entry = dict(at=time.time(), reason=_clip(reason, 32), event=_clip(event, 64),
                 session_id=_clip(env.get('ASHA_HUB_SESSION_ID'), 64),
                 generation=_clip(env.get('ASHA_HUB_GENERATION'), 16), native_id=_clip(native_id, 128),
                 emitted_at=_stamp(emitted_at), newest_applied=_stamp(newest),
                 budget_seconds=budget if type(budget) is float and 0 < budget <= 60 else None,
                 pid=os.getpid(), ppid=os.getppid())
    _append_bounded(loss_log_path(config), entry)


def _append_bounded(path, entry):
    """Append one JSON line under a lock; keep the newest half once over budget. Never raises."""
    line = json.dumps(entry, ensure_ascii=True) + '\n'
    try:
        import fcntl
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+', encoding='utf-8', errors='replace') as handle:
            # One writer at a time, so a trim never drops a concurrent append,
            # and the trim rewrites in place: a killed hook leaves no debris.
            fcntl.flock(handle, fcntl.LOCK_EX)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(line)
            handle.flush()
            if os.fstat(handle.fileno()).st_size > REJECTION_LOG_BYTES:
                handle.seek(0)
                tail, total = [], 0
                for item in reversed(handle.read().splitlines(keepends=True)):
                    total += len(item.encode('utf-8'))
                    if total > REJECTION_LOG_BYTES // 2:
                        break
                    tail.append(item)
                handle.seek(0)
                handle.truncate()
                handle.writelines(reversed(tail))
    except (OSError, ValueError):
        pass


def attach_refusal(row):
    """Why attaching to this row would be refused, or None: one predicate for the command and the view.

    ``Hub.attach`` enforces it for terminal sessions; the dashboard offers Enter
    only where it is None (Q14-F5). A structured row always opens its
    conversation. A legacy Room row carries the overview's observation, which
    reads an ended or missing Room as ``exited``; ``attach_room`` re-checks live.
    """
    transport = row.get('transport')
    if transport == 'terminal' and row.get('lifecycle') in {'closed', 'stopped'}:
        return 'session is closed or stopped; resume it first'
    if transport == 'room' and (row.get('lifecycle') == 'ended' or row.get('activity') == 'exited'):
        return 'Room has ended; open a new Room with continuation context'
    return None


def listed(row, *, include_closed):
    """Whether ``Hub.list`` puts this shown row on its page (#102).

    The SQL filter and this predicate agree: a closed session leaves the
    default page, and a finished structured worker does too. The dashboard
    applies it to single-row refreshes.
    """
    if include_closed:
        return True
    if row.get('lifecycle') == 'closed':
        return False
    return not (row.get('transport') == 'structured' and row.get('profile') == 'worker'
                and row.get('activity') == 'finished')


class Hub:
    def __init__(self, config, *, env=None, tmux=None):
        self.config = config
        self.env = dict(os.environ if env is None else env)
        self.tmux = tmux or TmuxAdapter()

    def database(self, *, create=False):
        return ControlDatabase(self.config, create=create, busy_timeout=0.2)

    @contextmanager
    def _action_lock(self, sid, *, timeout=None):
        """Serialize process mutations without holding a database write lock.

        ``timeout`` bounds the wait (close, D9); ``LockTimeout`` is raised when
        another holder (a handoff that is publishing) keeps it past the bound.
        """
        from .store import _directory_fd, _managed_start, _registry_lock
        root = self.config.tasks_dir.parent / 'hub-locks' / identifier(sid)
        with mutation_guard(self.config), _directory_fd(root, create=True,
                managed_start=_managed_start(root, ('control', 'hub-locks', sid))) as fd:
            if timeout is not None:
                _bounded_flock(fd, timeout)
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
        self.reconcile_closes()
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
                       lifecycle='starting', report=None, prompt_since_report=False,
                       native_id=None, reason='Awaiting native observation',
                       result=None, created_at=time.time(), spec=spec, **spec)
            self._save(c, row)
        with self._action_lock(sid):
            current = self.get(sid)
            if current['lifecycle'] != 'starting':
                return self.show(sid)
            return self._start(current, prompt)

    def _start(self, row, prompt, *, closing=False):
        from . import session_guidance as guidance
        row = self._update(row['session_id'], expected_generation=row['generation'], current_assignment=prompt,
                           assignment_epoch=str(uuid.uuid4()))
        block, manifest = guidance.resolve(self, row, row.get('learning_ids'))
        key = 'opening' if row['generation'] == 1 or row['transport'] == 'structured' else 'resume:' + str(row['generation'])
        guidance.retain(self, row, key, guidance.planned(manifest, prompt, block))
        prompt = text(prompt + block, 'assignment with selected guidance')
        if row['transport'] == 'structured':
            return self._start_structured(row, prompt)
        from .session_completion import instruction
        prompt += '\n\n' + instruction(row['profile'])
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
        self._update(row['session_id'], lifecycle='closing' if closing else 'open', launched_at=time.time())
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

    def _update(self, sid, *, expected_generation=None, closure_fn=None, **changes):
        """Merge changes into the freshly read row inside one write transaction.

        ``closure_fn(record, row)`` computes the closure record against the
        record as it is at write time, so a writer that read its row earlier
        can never overwrite a change recorded in between.
        """
        with mutation_guard(self.config), self.database() as db, db.transaction(write=True) as c:
            found = c.execute('SELECT payload FROM hub_sessions WHERE session_id=?', (sid,)).fetchone()
            if not found:
                raise StoreError('session not found')
            row = json.loads(found[0])
            if expected_generation is not None and (row['generation'] != expected_generation or row['lifecycle'] not in ACTIVE_LIFECYCLES):
                raise StoreError('stale or inactive session reporter')
            row.update(changes)
            if closure_fn is not None:
                row['closure'] = closure_fn(row.get('closure'), row)
            self._save(c, row)
        return row

    def show(self, sid, *, refresh_usage=False):
        """The presented row with its stored usage; ``refresh_usage`` (the show verb, stop and close)
        first rereads changed native records and stores the result (#111)."""
        row = self.get(sid)
        native_ids = None
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
                self._present_session(row, refresh_usage=refresh_usage)
                return row
            with SessionStore(self.config) as sessions:
                state = sessions.get(sid)
                native_ids = session_usage.remember(row, state.get('native_id'))
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
            self._present_session(row, refresh_usage=refresh_usage, native_ids=native_ids)
            return row
        if row['lifecycle'] in {'closed', 'stopped'}:
            row.update(process_state='ended', observed='ended', activity=row['lifecycle'])
        else:
            row['process_state'] = 'unknown'
            try:
                room = RoomStore(self.config).read(row['room_id'])
                state, detail = _owned_state(room, self.tmux)
                row['process_state'] = 'ended' if state in {'missing', 'ended'} else 'live' if state == 'open' else 'unknown'
                if state == 'open':
                    row['observed'], row['activity'], row['reason'] = _observed(row, time.time())
                else:
                    row.update(observed='ended' if state in {'missing', 'ended'} else 'unknown',
                               activity='exited' if state in {'missing', 'ended'} else 'unknown', reason=detail)
            except (ValueError, OSError) as exc:
                row.update(observed='unknown', activity='unknown', reason=str(exc)[:1000])
        _present_axes(row)
        with self.database() as db, db.transaction() as c:
            row['pending_messages'] = c.execute("SELECT COUNT(*) FROM hub_messages WHERE session_id=? AND state='queued'", (sid,)).fetchone()[0]
        row['capabilities'] = {'attach': 'native-terminal', 'send': 'queued-until-read',
                               'permissions': 'native', 'resume': row['harness'] in {'claude', 'codex'},
                               'close': 'stop-hook-final-turn' if row['harness'] in closure.STOP_HOOK_HARNESSES else 'queued-request-only'}
        self._present_session(row, refresh_usage=refresh_usage)
        return row

    def _present_session(self, row, *, refresh_usage=False, native_ids=None):
        from .session_presentation import present
        if refresh_usage:
            self._refresh_usage(row, session_usage.remember(row, None) if native_ids is None else native_ids)
        row['usage_line'] = session_usage.line(row.get('usage'))
        row['tokens'] = session_usage.tokens_label(row.get('usage'))
        row['selection'] = selection_evidence(row)
        from .session_publication import latest_saved_at
        # D8: the newest publication or attestation in this generation.
        row['memory_saved_at'] = latest_saved_at(self, row)
        record = row.get('closure') or {}
        handoff = record.get('handoff') or {}
        # D11: saves recorded before publication rows existed stay readable.
        if (record.get('generation') == row['generation']
                and handoff.get('generation') == row['generation']
                and handoff.get('outcome') == 'published' and handoff.get('verified') is True
                and handoff.get('digests')):
            row['memory_saved_at'] = max(row['memory_saved_at'] or 0, handoff['acknowledged_at'])
        legacy = row.get('memory_checkpoint') or {}
        if (legacy.get('outcome') == 'published' and legacy.get('publication_id')
                and legacy.get('finalized_at') is not None
                and all(legacy.get(k) == row.get(k) for k in ('session_id', 'generation'))):
            row['memory_saved_at'] = max(row['memory_saved_at'] or 0, legacy['finalized_at'])
        self._present_closure(row)
        row.update(present(row))

    def _refresh_usage(self, row, native_ids):
        """Read the worker's native records into ``row`` and store what changed (#111).

        Fail-open: any failure to read or to store leaves usage unknown or
        unstored and never refuses the show or the close that asked.
        """
        from .session_selection import record_reported
        previous = row.get('usage')
        try:
            usage = session_usage.read(row['harness'], native_ids, env=self.env, previous=previous)
        except Exception as exc:  # noqa: BLE001 - a reader defect must never block a close
            usage = session_usage.unknown(row['harness'], 'native record unreadable: ' + type(exc).__name__,
                                          native_ids)
        changes = {}
        stable = lambda value: {k: v for k, v in (value or {}).items() if k != 'read_at'}
        if usage is not previous and stable(usage) != stable(previous):
            changes['usage'] = usage
        reported = row.get('selection_reported') or {}
        if usage['status'] == 'known' and any(usage.get(f) and usage[f] != reported.get(f) for f in ('model', 'effort')):
            # The record states what actually ran: effective, not requested.
            changes['selection_reported'] = record_reported(
                row, {'model': usage.get('model'), 'effort': usage.get('effort')},
                source='native-record')['selection_reported']
        if not changes:
            return
        row.update(changes)
        try:
            self._update(row['session_id'], **changes)
        except Exception:  # noqa: BLE001 - storing is best-effort; the shown row still carries it
            pass

    def _present_closure(self, row):
        """Read-only view of the close request or its outcome; never persisted here."""
        record = row.get('closure')
        if not record:
            return
        if record.get('generation') != row['generation']:
            row['closure'] = dict(record, stale=True, guidance='Closure record from an earlier incarnation')
            return
        record = dict(record, guidance=closure.guidance_for(row, record))
        row['closure'] = record
        if row['lifecycle'] == 'closing':
            if row['activity'] == 'needs-input':
                # The final turn is exactly where a native prompt or question lands; never hide it.
                row['reason'] = 'Closing, needs input: ' + str(row.get('question') or row.get('reason') or '')
            else:
                row['activity'] = 'closing'
                row['reason'] = record['guidance']
        elif row['lifecycle'] in {'closed', 'stopped'}:
            row['reason'] = record['guidance']

    def list(self, *, include_closed=False, limit=100, deadline=None):
        if not self.initialized():
            return {'rows': [], 'complete': True}
        if not 1 <= limit <= 1000:
            raise StoreError('invalid session limit')
        with self.database() as db, db.transaction() as c:
            # Closed sessions leave the default page; an old record's attention
            # flag no longer keeps one there (D11).
            where = '' if include_closed else "WHERE lifecycle!='closed'"
            records = c.execute(f'SELECT session_id FROM hub_sessions {where} ORDER BY updated_at DESC LIMIT ?', (limit + 1,)).fetchall()
        rows, complete = [], len(records) <= limit
        for record in records[:limit]:
            if deadline is not None and time.monotonic() >= deadline:
                complete = False
                break
            try:
                rows.append(self.show(record[0], refresh_usage=False))
            except (ValueError, OSError) as exc:
                complete = False
                row = self.get(record[0])
                row.update(activity='unknown', reason=str(exc), pending_messages=0)
                rows.append(row)
        return {'rows': [r for r in rows if listed(r, include_closed=include_closed)], 'complete': complete}

    def attach(self, sid):
        row = self.get(sid)
        if row['transport'] == 'structured':
            return {'transport': 'structured', 'session_id': sid}
        refusal = attach_refusal(row)
        if refusal:
            raise StoreError(refusal)
        return attach_room(RoomStore(self.config), row['room_id'], tmux=self.tmux)

    def stop(self, sid, *, close=False):
        """Abrupt stop: ends the owned process now and never asks for a memory save.

        ``close=True`` records the session closed rather than stopped. A pending
        close request ends here too (D6).
        """
        from .sessions import refuse_managed_operator
        refuse_managed_operator(self.config, self.env)
        self.reconcile_closes(exclude=sid)
        with self._action_lock(sid, timeout=CLOSE_LOCK_SECONDS):
            row = self.get(sid)
            if row['lifecycle'] == 'closed' or (row['lifecycle'] == 'stopped' and not close):
                return self.show(sid)
            record = row.get('closure')
            closed = self._closed_record(row, record) if closure.pending(record, row) else None
            return self._stop(row, close=close, closure_record=closed)

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

    def _stop(self, row, *, close, closure_record=None):
        """Terminate the owned process; an attached terminal is killed too (D4)."""
        sid = row['session_id']
        if row['transport'] == 'structured':
            from .session_store import SessionStore
            if self._has_structured_record(sid):
                # A stop request (D10); the store's own cleanup reconciles the provider.
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
            changes['closure'] = closure_record
        self._update(sid, **changes)
        return self.show(sid, refresh_usage=True)

    def close(self, sid, *, force=False, wait=None):
        """Best-effort close: ask for a Memory save, wait a bounded time, then terminate.

        ``wait`` defaults to ``control.close_wait_seconds``; ``force`` is a zero
        wait. A repeated close joins the pending request (one request and at
        most one typed pointer per request) and finalizes it once it expired.
        The row ends closed, labelled saved or unsaved from the generation's
        publications (D8).
        """
        wait = self._close_wait(force, wait)
        from .sessions import refuse_managed_operator
        refuse_managed_operator(self.config, self.env)
        self.reconcile_closes(exclude=sid)
        row = self.request_close(sid, wait=wait)
        if row['lifecycle'] != 'closing':
            return row
        return self.await_close(sid, row['closure']['request_id'])

    def _close_wait(self, force, wait):
        if wait is not None and (type(wait) is not int or not 0 <= wait <= CLOSE_WAIT_LIMIT):
            raise StoreError(f'wait must be 0..{CLOSE_WAIT_LIMIT} seconds')
        if force and wait is not None:
            raise StoreError('--force is a zero wait; it cannot be combined with --wait')
        return 0 if force else self.config.close_wait_seconds if wait is None else wait

    def request_close(self, sid, *, wait):
        """Record the close request and deliver it; never waits (D6). Returns the shown row."""
        with self._action_lock(sid, timeout=CLOSE_LOCK_SECONDS):
            row = self.get(sid)
            if row['lifecycle'] == 'closed':
                return self.show(sid)
            record = row.get('closure')
            if row['lifecycle'] == 'closing' and closure.pending(record, row):
                if wait == 0:
                    # --force on a pending request: its deadline is now.
                    return self._finalize_locked(sid, record['request_id'])
                return self.show(sid)
            if record:
                self._update(sid, closure_history=row.get('closure_history', []) + [record])
            record = self._new_closure(row, wait)
            live = self._live(row)
            if wait == 0 or not live or self._finished_and_saved(row):
                # Nothing to wait for (D7: a current finished report with a save closes at once).
                if row['lifecycle'] in ACTIVE_LIFECYCLES and not live:
                    from .session_experience import Experiences
                    row = Experiences(self).reconcile_completion(row)
                return self._stop(row, close=True, closure_record=self._closed_record(row, record))
            record = self._deliver(row, record)
            self._update(sid, lifecycle='closing', closure=record)
            return self.show(sid)

    def await_close(self, sid, request_id):
        """Poll until a save lands after the request, the process ends, or the deadline passes."""
        from .session_publication import latest_saved_at
        while True:
            row = self.get(sid)
            record = row.get('closure') or {}
            if row['lifecycle'] != 'closing' or record.get('request_id') != request_id:
                return self.show(sid)
            if (latest_saved_at(self, row, since=record['requested_at']) is not None
                    or not self._live(row)):
                return self._finalize(sid, request_id)
            now = time.time()
            if now >= record['deadline']:
                return self._finalize(sid, request_id, grace=closure.PUBLICATION_GRACE_SECONDS)
            self._point(row, record)
            time.sleep(min(CLOSE_POLL_SECONDS, max(0.0, record['deadline'] - now)))

    def spawn_close_waiter(self, sid):
        """Start a detached ``asha control session close ID`` that waits out the request (D6)."""
        import subprocess
        asha = Path(__file__).resolve().parents[2] / 'bin' / 'asha'
        subprocess.Popen([str(asha), 'control', 'session', 'close', identifier(sid)], env=self.env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)

    def reconcile_closes(self, *, exclude=None):
        """Finalize every close whose deadline passed while nothing waited (D6).

        Runs from mutation paths only, never the cached read-only refresh.
        """
        if not self.initialized():
            return
        with self.database() as db, db.transaction() as c:
            expired = [r[0] for r in c.execute(
                "SELECT session_id FROM hub_sessions WHERE lifecycle='closing' "
                "AND json_extract(payload, '$.closure.deadline') < ?", (time.time(),))]
        for sid in expired:
            if sid == exclude:
                continue
            row = self.get(sid)
            try:
                # Same D9 grace as the foreground waiter: an in-flight save finishes first.
                self._finalize(sid, row['closure']['request_id'],
                               grace=closure.PUBLICATION_GRACE_SECONDS)
            except (ValueError, OSError):
                pass

    def _finished_and_saved(self, row):
        """D7: a finished report whose turn has ended and a save for the current assignment, in either order.

        Until a waiting observation emitted after the report shows the
        reporting turn ended, closing now could kill it (#109, #114).
        """
        if not settled(row):
            return False
        from .session_publication import saved_for_assignment
        return saved_for_assignment(self, row)

    def _point(self, row, record):
        """D1: type the one pointer line into an idle or unobserved terminal pane.

        At most one per request; a working Claude session gets the Stop-hook
        decision instead, and any other working session gets the pointer once
        it is observed idle. Only the close's own waiter types.
        """
        if (row['transport'] != 'terminal' or record.get('pointer_at') is not None
                or record['delivery'].get('delivered_at') is not None
                or (row.get('native_activity') or 'unknown') not in {'idle', 'unknown'}):
            return
        try:
            with self._action_lock(row['session_id'], timeout=CLOSE_POLL_SECONDS):
                current = self.get(row['session_id'])
                latest = current.get('closure') or {}
                if (current['lifecycle'] != 'closing' or latest.get('request_id') != record['request_id']
                        or latest.get('pointer_at') is not None):
                    return
                room = RoomStore(self.config).read(current['room_id'])
                state, detail = _owned_state(room, self.tmux)
                if state != 'open':
                    return
                # Recorded first, so a failure below never types a second line.
                self._update(row['session_id'], closure=dict(latest, pointer_at=time.time()))
                self.tmux.send_line(room['tmux']['pane_id'], closure.pointer_line(latest))
        except (LockTimeout, ValueError, OSError):
            return

    def _finalize(self, sid, request_id, *, grace=0.0):
        """Terminate for this request; a publication in flight gets ``grace`` seconds first (D9)."""
        until = time.monotonic() + grace
        if grace:
            self._await_publication(self.get(sid), until)
        try:
            with self._action_lock(sid, timeout=max(CLOSE_LOCK_SECONDS, until - time.monotonic())):
                return self._finalize_locked(sid, request_id)
        except LockTimeout:
            # A handoff still publishing past the grace: kill anyway. A Memory
            # write cut short is recovered by its journal on the next read.
            return self._finalize_locked(sid, request_id)

    def _await_publication(self, row, until):
        """Wait (to ``until``) while an explicit save holds the project's Memory publication lock."""
        import fcntl
        try:
            lock = closure.secure_path(closure.secure_project_root(Path(row['project'])),
                                       'Work/session-state/.memory-publication.lock')
            fd = os.open(lock, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        except (OSError, ValueError):
            return
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    return
                except BlockingIOError:
                    if time.monotonic() >= until:
                        return
                    time.sleep(0.05)
        finally:
            os.close(fd)

    def _finalize_locked(self, sid, request_id):
        """Idempotent: only the named request of the current incarnation is finalized."""
        row = self.get(sid)
        record = row.get('closure') or {}
        if (row['lifecycle'] != 'closing' or record.get('request_id') != request_id
                or record.get('generation') != row['generation']):
            return self.show(sid)
        if not self._live(row):
            from .session_experience import Experiences
            row = Experiences(self).reconcile_completion(row)
        return self._stop(row, close=True, closure_record=self._closed_record(row, record))

    def _closed_record(self, row, record):
        from .session_publication import latest_saved_at
        return dict(record, state='closed', saved_at=latest_saved_at(self, row), terminated_at=time.time())

    def _new_closure(self, row, wait):
        from .session_experience import Experiences
        record = closure.new_closure(row, wait=wait)
        # The selection this request was made under; statistics attribute the
        # close to it even after a later resume or reroute (#95).
        record['selection'] = selection_evidence(row)
        record['capture'] = Experiences(self).close_capture(row, record['request_id'])
        return record

    def _adopt_prompt(self, row):
        """#114: a report after a native prompt makes that prompt a new assignment.

        A prompt after a finished report only marks ``prompt_since_report``: a
        wake turn that reports nothing leaves the report and its save current.
        A Control send or resume clears the report and the mark, so their own
        assignment stands.
        """
        if not row.get('prompt_since_report'):
            return row
        with self._observation_lock(row['session_id']):
            current = self.get(row['session_id'])
            if not current.get('prompt_since_report'):
                return current
            changes = dict(prompt_since_report=False)
            if (report_of(current) or {}).get('state') == 'finished':
                changes['assignment_epoch'] = str(uuid.uuid4())
            return self._update(current['session_id'], expected_generation=row['generation'], **changes)

    def report(self, *, state, body=None, native_id=None, experience_file=None,
               experience_ref=None, key=None, supersedes=None):
        """Optional completion assessment; capture failures never erase task status."""
        if state not in REPORT_STATES:
            raise StoreError('report state must be needs-input, finished or working')
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
            row = self._adopt_prompt(row)
            # Finished is ungated (D3): the row shows whether this generation saved.
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
            result = self._report(row, state, reported_body, native_id=native_id)
            if capture is not None:
                changes = {'capture': capture}
                if request:
                    changes['experience_request'] = request
                result = self._update(row['session_id'], expected_generation=row['generation'], **changes)
            return result

    def _deliver(self, row, record):
        """Queue the close request at the supported seam: the next structured turn, or a hub message."""
        body = closure.request_text(row, record)
        key = closure.message_key(record)
        if row['transport'] == 'structured':
            from .session_store import SessionStore
            try:
                with SessionStore(self.config) as sessions:
                    message = sessions.enqueue(row['session_id'], body, key=key)
            except StoreError as exc:
                return dict(record, delivery=dict(record['delivery'], detail='not queued: ' + str(exc)[:500]))
            wake = self._wake_structured(row['session_id'])
            detail = wake['dispatch_warning'] or 'queued as the next structured turn'
            return dict(record, delivery=dict(record['delivery'], channel='structured-turn', detail=detail,
                                              message_id=message['message_id']))
        with mutation_guard(self.config), self.database() as db, db.transaction(write=True) as c:
            # One outstanding close request: an earlier request's unread copy is superseded.
            c.execute("UPDATE hub_messages SET state='superseded' WHERE session_id=? AND state='queued' AND delivery_key LIKE ? AND delivery_key!=?",
                      (row['session_id'], closure.MESSAGE_KEY_PREFIX + '%', key))
            mid = str(uuid.uuid4())
            c.execute('INSERT INTO hub_messages VALUES(?,?,?,?,?,?,?)',
                      (mid, row['session_id'], key, body, digest(body), 'queued', time.time()))
        channel = 'stop-hook' if row['harness'] in closure.STOP_HOOK_HARNESSES else 'queued-message'
        detail = ('the Stop decision when the current turn ends; an idle pane gets one pointer line'
                  if channel == 'stop-hook' else 'queued until read; an idle or unobserved pane gets one pointer line')
        return dict(record, delivery=dict(record['delivery'], channel=channel, detail=detail, message_id=mid))

    def stop_decision(self, row, *, stop_hook_active=False):
        """Stop-hook seam: the pending close request as the harness's own block decision.

        ``row`` is the verified reporter's own session. Nothing is persisted
        here: the caller prints the decision first and then calls
        ``confirm_delivery``, so a hook killed at its time budget re-emits the
        same request at the next Stop instead of recording a delivery the agent
        never saw.
        """
        if row['lifecycle'] != 'closing' or not row.get('closure'):
            return None
        with self._action_lock(row['session_id'], timeout=CLOSE_LOCK_SECONDS):
            row = self.get(row['session_id'])
            record = row.get('closure')
            if (row['lifecycle'] != 'closing' or not closure.pending(record, row)
                    or row['harness'] not in closure.STOP_HOOK_HARNESSES or stop_hook_active
                    or record['delivery'].get('delivered_at') is not None):
                # stop_hook_active means this Stop already follows a hook block; never chain blocks.
                return None
            return closure.StopDecision(closure.request_text(row, record), receipt=closure.receipt_for(record))

    def confirm_delivery(self, row, receipt=None):
        """Record that a printed Stop decision reached the harness.

        The receipt names the exact close request and incarnation the decision
        was emitted for (``StopDecision.receipt``); any other is stale and
        changes nothing. Idempotent.
        """
        if receipt is None:
            receipt = closure.receipt_for(row.get('closure')) if row.get('closure') else None
        if not receipt:
            return {'confirmed': False, 'reason': 'no delivery receipt'}
        outcome = {'confirmed': False, 'reason': 'stale receipt'}
        def mark(record, current):
            if not closure.pending(record, current) or closure.receipt_for(record) != receipt:
                return record
            if record['delivery'].get('delivered_at') is not None:
                outcome.update(reason='already delivered')
                return record
            outcome.update(confirmed=True, reason='delivered')
            return closure.mark_delivered(record, 'stop-hook', detail='emitted as the Stop hook decision',
                                          message_id=record['delivery'].get('message_id'))
        with self._action_lock(row['session_id'], timeout=CLOSE_LOCK_SECONDS):
            self._update(row['session_id'], closure_fn=mark)
        return outcome

    def resume(self, sid, *, prompt, expected_digest=None, learning_ids=None):
        from .sessions import refuse_managed_operator
        refuse_managed_operator(self.config, self.env)
        self.reconcile_closes()
        with self._action_lock(sid):
            return self._resume(sid, prompt=prompt, expected_digest=expected_digest, learning_ids=learning_ids)

    def _resume(self, sid, *, prompt, expected_digest, learning_ids=None):
        prompt = text(prompt, 'continuation')
        row = self.get(sid)
        if row['lifecycle'] == 'closing':
            raise StoreError('a close is pending; let it finish, or force-close first')
        if row['transport'] == 'structured':
            from .session_store import SessionStore
            if not self._has_structured_record(sid):
                row = self._update(sid, lifecycle='starting', generation=row['generation'] + 1,
                                   learning_ids=learning_ids, capture={}, closure=None,
                                   report=None, prompt_since_report=False,
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
                               current_assignment=prompt, report=None, prompt_since_report=False,
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
                           generation=row['generation'] + 1, learning_ids=learning_ids,
                           report=None, prompt_since_report=False, asks=None, waiting_since=None,
                           native_activity='unknown', native_observed_at=None, native_emitted_at=None, background_tasks=None,
                           reason='Resuming native conversation' if row['native_id'] else 'Starting with explicit continuation context; native resume ID unavailable')
        return self._start(row, text(prompt, 'continuation'))

    def send(self, sid, body, *, key, learning_ids=None):
        from .sessions import refuse_managed_operator
        from . import session_guidance as guidance
        refuse_managed_operator(self.config, self.env)
        self.reconcile_closes()
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
                def retained(c, message):
                    guidance.retain_in(c, row, key, manifest)
                    self._withdraw_report(c, sid)
                with SessionStore(self.config) as sessions:
                    message = sessions.enqueue(sid, text(body + block, 'guided input'), key=key, on_retained=retained)
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
                self._withdraw_report(c, sid, new_assignment=True)
                guidance.retain_in(c, row, key, manifest)
                message = dict(c.execute('SELECT * FROM hub_messages WHERE message_id=?', (mid,)).fetchone())
            # The retained message is the delivery contract; the session reads it.
            return dict(message, delivery='queued-until-read',
                        delivery_detail='queued until the session reads messages')

    def _withdraw_report(self, c, sid, *, new_assignment=False):
        """A Control send is new work: the standing report and any prompt mark go with it."""
        current = json.loads(c.execute('SELECT payload FROM hub_sessions WHERE session_id=?', (sid,)).fetchone()[0])
        current.update(report=None, prompt_since_report=False)
        if new_assignment:
            current['assignment_epoch'] = str(uuid.uuid4())
        self._save(c, current)

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
        """The reporting session, from the environment only (hub decision, subtraction B2).

        No session argument selects it and no Room marker or process ancestry
        proves it; the generation and lifecycle fences stop stale actors.
        """
        row = self.get(identifier(self.env.get('ASHA_HUB_SESSION_ID', '')))
        if str(row['generation']) != self.env.get('ASHA_HUB_GENERATION') or row['lifecycle'] not in ACTIVE_LIFECYCLES:
            raise StoreError('stale or inactive session reporter')
        return row

    def observe(self, event, *, native_id=None, state=None, body=None, cwd=None, background_tasks=None,
                emitted_at=None, source=None):
        """A native hook event (the observed axis), or with no event a worker report (the report axis)."""
        if not event:
            if emitted_at is not None:
                raise StoreError('emitted-at is hook evidence; worker reports do not carry it')
            if cwd is not None or background_tasks is not None or source is not None:
                raise StoreError('cwd, background tasks and source are hook evidence; worker reports do not carry them')
            return self._report(self.actor(), state, body, native_id=native_id)
        actor = self.actor()
        with self._observation_lock(actor['session_id']):
            row = self.get(actor['session_id'])
            if row['generation'] != actor['generation'] or row['lifecycle'] not in ACTIVE_LIFECYCLES:
                raise StoreError('stale or inactive session reporter')
            return self._observe(row, event, native_id=native_id, body=body, cwd=cwd,
                                 background_tasks=background_tasks, emitted_at=emitted_at, source=source)

    @staticmethod
    def _skipped(row, emitted_at):
        """D2: a hook report stamped older than the newest applied one, by at most 30 s.

        Equal applies; a report more than 30 s older applies (a backward clock
        step must not reject every event until time catches up); an unstamped
        report applies (older hooks in live Rooms).
        """
        stored = row.get('native_emitted_at')
        return emitted_at is not None and stored is not None and 0 < stored - emitted_at <= STALE_REPORT_SECONDS

    def _observe(self, row, event, *, native_id, body=None, cwd=None, background_tasks=None, emitted_at=None,
                 source=None):
        """Apply one hook event to the observed axis; it never writes the report axis.

        Only a native prompt touches the report: it clears a needs-input report
        and starts a new assignment, or after a finished report only marks
        ``prompt_since_report`` (#114).
        """
        activity = EVENTS.get(event)
        if activity is None:
            raise StoreError('invalid session observation')
        if background_tasks is not None and (event != 'turn-stopped' or type(background_tasks) is not int
                                             or not 0 <= background_tasks < BACKGROUND_TASK_LIMIT):
            raise StoreError('invalid background task count')
        if emitted_at is not None and (type(emitted_at) not in {int, float} or not 0 < emitted_at < 1e12):
            raise StoreError('invalid hook emission time')
        if source is not None and (event != 'session-start' or type(source) is not str
                                   or not source.isascii() or not source.isalpha() or len(source) > 16):
            raise StoreError('invalid session-start source')
        rebinding = None
        if native_id:
            native_id = _native_id(native_id)
            rebinding = native_binding(row, event, native_id, cwd, source)
        if self._skipped(row, emitted_at):
            # Every effect of an older report is suppressed: activity, background
            # tasks, question and lifecycle. The CLI gives a skipped Stop no decision.
            return dict(row, observation='ignored')
        now = time.time()
        # #99: Claude's Stop payload lists background work (shells, Monitors,
        # agents) still running or pending. Such a turn ended, but the session
        # waits for that work to wake it: it is not an idle boundary.
        outstanding = background_tasks if event == 'turn-stopped' and background_tasks else None
        if outstanding:
            activity = 'working'
        changes = dict(native_activity=activity, native_observed_at=now, background_tasks=outstanding,
                       reason=f'Turn ended; waiting on {outstanding} background task(s)' if outstanding else event,
                       # A turn-ending Stop is the waiting observation D7 compares with the report;
                       # unstamped (older hooks) it counts from when it applied. Any other event clears it.
                       waiting_since=(now if emitted_at is None else emitted_at)
                       if event == 'turn-stopped' and not outstanding else None,
                       # A native permission request names what it asks for (#101).
                       asks=text(body, 'permission request', 16000) if event == 'permission-requested' and body else None)
        if emitted_at is not None:
            changes['native_emitted_at'] = emitted_at
        if event == 'prompt-submitted':
            if (report_of(row) or {}).get('state') == 'finished':
                # #114: a wake turn (a background Monitor or task notification)
                # is not yet new work; a report in it makes it one (report).
                changes['prompt_since_report'] = True
            else:
                changes.update(report=None, prompt_since_report=False, assignment_epoch=str(uuid.uuid4()))
        if native_id:
            binding = row.get('native_binding') or {}
            if rebinding:
                changes['native_binding'] = rebinding
            elif event == 'session-ended' and binding.get('generation') == row['generation']:
                # The bound conversation ended; its successor's SessionStart may rebind (F6).
                changes['native_binding'] = dict(binding, ended=True)
            changes['native_id'] = native_id
            changes['native_ids'] = session_usage.remember(row, native_id)
        if event == 'session-ended':
            from .session_experience import Experiences
            Experiences(self).reconcile_completion(row, observed_exit=True)
        result = self._update(row['session_id'], expected_generation=row['generation'], **changes)
        result['observation'] = 'applied'
        return result

    def _report(self, row, state, body, *, native_id=None):
        """Apply one explicit report to the report axis; it never writes the observed axis.

        needs-input and finished stand until a new report, a native prompt
        (needs-input only), a Control send, a resume or a new generation;
        working withdraws the standing report.
        """
        if state not in REPORT_STATES:
            raise StoreError('report state must be needs-input, finished or working')
        if row['transport'] == 'structured' or body is not None:
            body = text(body, 'report', 16000)
        with self._observation_lock(row['session_id']):
            current = self.get(row['session_id'])
            if current['generation'] != row['generation'] or current['lifecycle'] not in ACTIVE_LIFECYCLES:
                raise StoreError('stale or inactive session reporter')
            standing = report_of(current) or {}
            if state == 'finished' and body is None and standing.get('state') == 'finished':
                body = standing.get('text')   # an experience follow-up keeps the reported result
            changes = dict(report=None if state == 'working' else dict(state=state, text=body, at=time.time()),
                           prompt_since_report=False)
            if state == 'finished' and body:
                changes['result'] = body
            if native_id:
                native_id = _native_id(native_id)
                changes['native_id'] = native_id
                changes['native_ids'] = session_usage.remember(current, native_id)
            return self._update(row['session_id'], expected_generation=row['generation'], **changes)

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
            if (closure.pending(record, current) and record['delivery'].get('delivered_at') is None
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
        pending = closure.pending(record, row)
        return {'session_id': row['session_id'], 'generation': row['generation'],
                'request_id': record['request_id'] if pending else None,
                'closure_state': record.get('state') if record else None, 'memory': memory,
                'paths': {name: (memory['destination'] + '/' + name) if memory['destination'] else None
                          for name in closure.MEMORY_FILES}}

    def handoff(self, request_id, *, outcome=None, detail=None, active_file=None, decisions_file=None, expected=None,
                experience_file=None, experience_ref=None, supersedes=None, key=None):
        """Save project Memory (or attest), optionally answering a pending close request.

        Any save lands as a publication row; a pending close closes on the
        first one after its request (D8), named or not.
        """
        request_id = identifier(request_id) if request_id else None
        actor = self._handoff_actor(request_id)
        with self._action_lock(actor['session_id']):
            row = self.get(actor['session_id'])
            if row['generation'] != actor['generation'] or row['lifecycle'] not in ACTIVE_LIFECYCLES:
                raise StoreError('stale or inactive session reporter')
            if request_id is None:
                if any((experience_file, experience_ref, supersedes, key)):
                    raise StoreError('completion experience belongs on session report --state finished')
                return self._finalize_handoff(row, outcome=outcome, detail=detail, active_file=active_file,
                                              decisions_file=decisions_file, expected=expected)
            closure.validate_handoff_request(row.get('closure'), row, request_id)
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
            return self._handoff(row, request_id, outcome=outcome, detail=detail, active_file=active_file,
                                 decisions_file=decisions_file, expected=expected)

    def _finalize_handoff(self, row, *, outcome, detail, active_file, decisions_file, expected):
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
            if outcome == 'no-durable-update':
                # An attestation passes the same scope and silence checks as a publication.
                from .session_completion import snapshot
                with snapshot(row):
                    pass
        except (ValueError, OSError) as exc:
            raise StoreError('handoff refused: ' + str(exc)) from exc
        saved = self._record_saved(row, 'handoff', outcome, detail, publication)
        return dict(session_id=row['session_id'], outcome=outcome, detail=detail, git_invoked=False, **saved)

    def _record_saved(self, row, source, outcome, detail, publication):
        """Retain a publication row for a successful handoff or attestation (D3).

        The Memory write already committed; a failure to retain the row is
        reported, never turned into a refused handoff.
        """
        from .session_publication import record
        if outcome == 'published':
            source_receipt = dict(publication or {}, status='published')
        elif outcome == 'no-durable-update':
            source, source_receipt = 'attestation', dict(status='attested', outcome=outcome, detail=detail)
        else:
            return {}
        try:
            saved = record(self, row, source, source_receipt)
        except (OSError, ValueError) as exc:
            return dict(hub_publication_status='unavailable', hub_publication_error=str(exc)[:1000])
        return dict(hub_publication_status='recorded', publication_source=saved['source'])

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
            try:
                publication = closure.publish_handoff(row['project'], active_file, decisions_file, expected=expected or {})
            except (OSError, ValueError) as exc:
                # Retryable: the request stays open; the agent re-reads and retries or reports blocked.
                raise StoreError('handoff publication refused: ' + str(exc)) from exc
            detail = detail or 'published verified project memory'
        if outcome is None:
            raise StoreError('an outcome or both draft files are required')
        detail = text(detail, 'handoff detail', 4000)
        updated = closure.record_handoff(record, row, outcome, detail, publication=publication)
        saved = self._record_saved(row, 'close', outcome, detail, publication and publication['publication'])
        self._update(row['session_id'], expected_generation=row['generation'],
                     closure_fn=lambda current, _row: dict(current, handoff=updated['handoff'])
                     if current and current.get('request_id') == request_id else current)
        return {'session_id': row['session_id'], 'request_id': request_id, 'outcome': outcome,
                'closure_state': 'closing', 'memory': updated['memory'], 'handoff': updated['handoff'],
                'capture': updated.get('capture', {'status': 'disabled', 'report_id': None}),
                'git_invoked': False, **saved}

