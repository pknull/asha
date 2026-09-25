"""Bounded, advisory session observations in the existing private Control DB.

No transcript discovery, semantic extraction, publication, or native launch occurs
on reads or capture. Actor proofs belong to Hub; every write rechecks incarnation.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import uuid
from pathlib import Path

from .session_closure import memory_v2, secure_path, secure_project_root
from .session_store import identifier
from .store import StoreError
from .registry_guards import mutation_guard

CONTRACT = 'asha.session-experience.v1'
REPORT_LIMIT = 16 * 1024
REVIEW_INPUT_LIMIT = 64 * 1024
EVIDENCE_LIMIT = 32 * 1024
KINDS = {'correction', 'failure-recovery', 'verification-conflict', 'context-gap',
         'orchestration-failure', 'unexpected-improvement'}
HARNESSES = {'claude', 'codex', 'copilot', 'opencode'}
ASSESSMENT_KEY = '@report-assessment'
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS hub_experience_policies (
       project_id TEXT PRIMARY KEY, project TEXT NOT NULL, mode TEXT NOT NULL, revision INTEGER NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS hub_experience_policy_epochs (
       project_id TEXT PRIMARY KEY, highwater INTEGER NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS hub_experiences (
       report_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES hub_sessions(session_id),
       generation INTEGER NOT NULL, project_id TEXT NOT NULL, source TEXT NOT NULL, delivery_key TEXT NOT NULL,
       close_request_id TEXT, policy_revision INTEGER NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL,
       envelope TEXT NOT NULL, supersedes TEXT REFERENCES hub_experiences(report_id),
       origin_report_id TEXT NOT NULL, created_at REAL NOT NULL,
       UNIQUE(session_id,generation,source,delivery_key))''',
    'CREATE INDEX IF NOT EXISTS hub_experience_project ON hub_experiences(project_id,created_at,report_id)',
    '''CREATE TABLE IF NOT EXISTS hub_experience_reviews (
       review_id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES hub_experiences(report_id),
       report_digest TEXT NOT NULL, policy_revision INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
       reason TEXT NOT NULL, status TEXT NOT NULL, utility_id TEXT UNIQUE,
       created_at REAL NOT NULL, reserved_at REAL, finished_at REAL, packet_digest TEXT, result TEXT,
       UNIQUE(report_id,policy_revision,attempt))''',
    '''CREATE TABLE IF NOT EXISTS hub_experience_save_reviews (
       project_id TEXT NOT NULL, publication_id TEXT NOT NULL, report_id TEXT NOT NULL REFERENCES hub_experiences(report_id),
       save_session_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, created_at REAL NOT NULL,
       PRIMARY KEY(project_id,publication_id,report_id))''',
    'CREATE INDEX IF NOT EXISTS hub_experience_review_queue ON hub_experience_reviews(status,created_at,review_id)',
    '''CREATE TABLE IF NOT EXISTS hub_experience_dispositions (
       disposition_id TEXT PRIMARY KEY, review_id TEXT NOT NULL REFERENCES hub_experience_reviews(review_id),
       observation_key TEXT NOT NULL, finding_digest TEXT NOT NULL, save_key TEXT NOT NULL,
       project_id TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
       UNIQUE(review_id,observation_key,save_key))''',
    'CREATE INDEX IF NOT EXISTS hub_experience_disposition_project ON hub_experience_dispositions(project_id,created_at,disposition_id)',
    '''CREATE TABLE IF NOT EXISTS hub_experience_captures (
       session_id TEXT NOT NULL REFERENCES hub_sessions(session_id), generation INTEGER NOT NULL,
       source TEXT NOT NULL, delivery_key TEXT NOT NULL, project_id TEXT NOT NULL,
       requested INTEGER NOT NULL, policy_revision INTEGER NOT NULL, status TEXT NOT NULL,
       report_id TEXT REFERENCES hub_experiences(report_id), reason TEXT, created_at REAL NOT NULL, close_request_id TEXT,
       PRIMARY KEY(session_id,generation,source,delivery_key))''',
    'CREATE INDEX IF NOT EXISTS hub_experience_capture_project ON hub_experience_captures(project_id,created_at)',
    '''CREATE TABLE IF NOT EXISTS hub_guidance_exposures (
       exposure_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES hub_sessions(session_id),
       generation INTEGER NOT NULL, delivery_key TEXT NOT NULL, project_id TEXT NOT NULL,
       status TEXT NOT NULL, manifest TEXT NOT NULL, created_at REAL NOT NULL,
       UNIQUE(session_id,generation,delivery_key))''',
    'CREATE INDEX IF NOT EXISTS hub_guidance_project ON hub_guidance_exposures(project_id,created_at,exposure_id)',
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode('utf-8')).hexdigest()


def strict_json(raw, maximum=REPORT_LIMIT):
    if len(raw) > maximum:
        raise ValueError('bounded JSON exceeds byte limit')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def invalid(_):
        raise ValueError('nonfinite JSON value')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('invalid UTF-8 JSON') from exc


def shape(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError('invalid contract fields')


def string(value, maximum=4000, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value.encode()) > maximum or '\x00' in value:
        raise ValueError('invalid bounded contract text')
    return value


def enum(value, choices):
    if not isinstance(value, str) or value not in choices:
        raise ValueError('invalid contract enumeration')


def array(value, maximum):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError('invalid bounded contract list')
    return value


def ids(value, permitted, maximum=4):
    array(value, maximum)
    if any(not isinstance(v, str) for v in value) or len(set(value)) != len(value) or not set(value) <= set(permitted):
        raise ValueError('unknown or duplicate evidence reference')


def safe_content(value):
    """Best-effort known-secret exclusion; rejected bytes never enter diagnostics."""
    from recovery_state import _redact
    if isinstance(value, str):
        if _redact(value) != value:
            raise ValueError('known secret-bearing content omitted')
    elif isinstance(value, dict):
        for key, item in value.items():
            safe_content(key)
            safe_content(item)
    elif isinstance(value, list):
        for item in value:
            safe_content(item)


def decode_report(raw):
    value = strict_json(raw)
    shape(value, ('contract', 'assessment', 'outcome', 'summary', 'observations', 'evidence'), ('guidance_feedback',))
    enum(value['contract'], {CONTRACT})
    enum(value['assessment'], {'observations', 'none-observed', 'insufficient-evidence'})
    enum(value['outcome'], {'succeeded', 'partial', 'failed', 'unknown'})
    string(value['summary'])
    observations = array(value['observations'], 3)
    if bool(observations) != (value['assessment'] == 'observations'):
        raise ValueError('assessment and observations disagree')
    evidence = array(value['evidence'], 4)
    known = []
    for item in evidence:
        shape(item, ('id', 'kind'), ('text', 'path', 'sha256'))
        string(item['id'], 128)
        if item['id'] in known:
            raise ValueError('duplicate evidence id')
        known.append(item['id'])
        enum(item['kind'], {'agent-attestation', 'project-file'})
        if item['kind'] == 'agent-attestation':
            shape(item, ('id', 'kind', 'text'))
            string(item['text'], REPORT_LIMIT)
        else:
            shape(item, ('id', 'kind', 'path', 'sha256'))
            string(item['path'], 1024)
            validate_digest(item['sha256'])
    keys = set()
    for item in observations:
        shape(item, ('key', 'kind', 'observed', 'evidence_ids', 'uncertainty'), ('explanation', 'lesson', 'applicability'))
        string(item['key'], 128)
        if item['key'] in keys or item['key'] == ASSESSMENT_KEY:
            raise ValueError('duplicate or controller-reserved observation key')
        keys.add(item['key'])
        enum(item['kind'], KINDS)
        string(item['observed']); string(item['uncertainty'])
        ids(item['evidence_ids'], known)
        if 'explanation' in item:
            string(item['explanation'])
        if 'lesson' in item:
            shape(item['lesson'], ('trigger', 'action'))
            for text in item['lesson'].values():
                string(text, 1000)
        if 'applicability' in item:
            scope = item['applicability']
            shape(scope, ('harnesses', 'task_kind', 'limitations'), ('project_ids', 'versions'))
            ids(scope['harnesses'], HARNESSES)
            string(scope['task_kind'], 128); string(scope['limitations'], 1000)
            for pid in array(scope.get('project_ids', []), 8):
                string(pid, 128)
            for version in array(scope.get('versions', []), 8):
                string(version, 128)
    for feedback in array(value.get('guidance_feedback', []), 3):
        shape(feedback, ('id', 'version', 'use', 'evidence_ids'), ('target_failure',))
        string(feedback['id'], 128); validate_digest(feedback['version'])
        enum(feedback['use'], {'applied', 'not-applied', 'not-applicable', 'unknown'})
        ids(feedback['evidence_ids'], known)
        if 'target_failure' in feedback:
            enum(feedback['target_failure'], {'observed', 'not-observed', 'unknown'})
    safe_content(value)
    return value


def review_subjects(report):
    """Declare review scope without adding an observation to the worker's report."""
    observations = report['body']['observations']
    if observations:
        return [{'key': item['key'], 'subject_kind': 'worker-observation',
                 'applicability': item.get('applicability', {})} for item in observations]
    envelope = report['envelope']
    scope = {'project_ids': [report['project_id']], 'harnesses': [envelope['harness']],
             'task_kind': 'unspecified',
             'limitations': 'Reviewer assessment of this frozen report; broader applicability is unverified.'}
    if envelope.get('harness_version'):
        scope['versions'] = [envelope['harness_version']]
    return [{'key': ASSESSMENT_KEY, 'subject_kind': 'reviewer-report-assessment', 'applicability': scope}]


def validate_digest(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('invalid sha256 digest')
    return value


def safe_read(path, maximum):
    """Open each component with no-follow dirfds, then read and hash the same bytes."""
    target = Path(path)
    if not target.is_absolute() or '..' in target.parts:
        raise ValueError('absolute symlink-free input required')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in target.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        item = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            meta = os.fstat(item)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_nlink != 1 or meta.st_size > maximum:
                raise ValueError('input must be an owned bounded regular file')
            raw = b''
            while len(raw) <= maximum:
                chunk = os.read(item, min(65536, maximum + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
            if len(raw) > maximum:
                raise ValueError('input exceeds byte limit')
            raw.decode('utf-8')
            return raw
        finally:
            os.close(item)
    finally:
        os.close(fd)


def read_report(path, row):
    return capture_body(safe_read(path, REPORT_LIMIT), row)


def capture_body(raw, row):
    value = decode_report(raw)
    body_digest = sha(canonical(value))
    total = 0
    for item in value['evidence']:
        if item['kind'] != 'project-file':
            continue
        relative = Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('evidence must remain in the verified project root')
        root = secure_project_root(Path(row['project']))
        raw = safe_read(root / relative, 16 * 1024)
        total += len(raw)
        if total > EVIDENCE_LIMIT or sha(raw) != item['sha256']:
            raise ValueError('evidence exceeds bounds or source drifted')
        content = raw.decode('utf-8'); safe_content(content)
        item.update(kind='controller-captured-source', text=content, excerpt=False, omitted=False)
    return value, body_digest


def silenced(project):
    try:
        return secure_path(secure_project_root(Path(project)), 'Work/markers/silence').exists()
    except (OSError, ValueError):
        return True  # Unsafe project routing cannot authorize learning persistence.


class Experiences:
    def __init__(self, hub):
        self.hub = hub

    def available(self):
        if not self.hub.initialized():
            return False
        with self.hub.database() as db, db.transaction() as c:
            return c.execute("SELECT 1 FROM sqlite_master WHERE name='hub_experiences'").fetchone() is not None

    def operator(self):
        from .sessions import refuse_managed_operator
        refuse_managed_operator(self.hub.config, self.hub.env)
        env = self.hub.env
        if env.get('ASHA_HUB_SESSION_ID') or env.get('ASHA_SESSION_PROFILE') == 'worker':
            raise StoreError('executing workers cannot perform experience operator actions')
        self._refuse_worker_ancestry()

    def _refuse_worker_ancestry(self):
        # Terminal workers cannot escape by dropping environment labels.
        if self.hub.initialized():
            from .harness import caller_descends_from
            from .rooms import RoomStore, _owned_state
            with self.hub.database() as db, db.transaction() as c:
                rows = [json.loads(r[0]) for r in c.execute("SELECT payload FROM hub_sessions WHERE lifecycle IN ('starting','open','closing')")]
            for row in rows:
                if row['transport'] != 'terminal' or row['profile'] != 'worker':
                    continue
                room = RoomStore(self.hub.config).read(row['room_id'])
                if _owned_state(room, self.hub.tmux)[0] != 'open':
                    raise StoreError('worker ownership is unavailable; operator ancestry cannot be established')
                else:
                    pid = self.hub.tmux.pane_facts(room['tmux']['pane_id']).pane_pid
                    if pid and caller_descends_from(pid, require_complete=True):
                        raise StoreError('worker ancestry refuses experience operator actions')

    def saving_actor(self, project_id, *, required=False, project=None):
        """Chair native identity is a save heuristic, subordinate to operator proof."""
        if self.hub.env.get('ASHA_SESSION_PROFILE') == 'worker':
            raise StoreError('executing workers cannot perform experience save actions')
        if self.hub.env.get('ASHA_HUB_SESSION_ID'):
            from .sessions import refuse_managed_operator
            refuse_managed_operator(self.hub.config, self.hub.env)
            actor = self.hub.actor()
            if actor['transport'] != 'terminal' or actor['profile'] != 'room':
                raise StoreError('experience saving authority requires a terminal Room')
            if actor['project_id'] != project_id:
                raise StoreError('Room experience authority is scoped to its own project')
            self._refuse_worker_ancestry()
            return {'session_id': actor['session_id'], 'hub_actor': actor}
        self.operator()
        import save_identity
        identity = next((self.hub.env[name].strip() for name in save_identity.ENV_SEAMS
                         if self.hub.env.get(name, '').strip() not in {'', 'unknown'}), None)
        if required and identity is None and project is not None:
            identity = save_identity.resolve(Path(project), self.hub.env.get('ASHA_HARNESS', ''))
        if required and identity is None:
            raise StoreError('native explicit-save session identity unavailable')
        return {'session_id': identity, 'hub_actor': None}

    def own_lineage(self, report, saver):
        source = self.hub.get(report['session_id'])
        actor = saver.get('hub_actor') or {}
        identities = {value for value in (saver.get('session_id'), actor.get('session_id'), actor.get('native_id')) if value}
        envelope = report['envelope']
        envelope = json.loads(envelope) if isinstance(envelope, str) else envelope
        return bool(identities.intersection({source['session_id'], source.get('native_id'), envelope.get('native_session_id')}))

    def default_policy(self, pid, *, epoch=0):
        from .orchestration.projects import experience_default
        mode, source, _ = experience_default(self.hub.env)
        # Nonpositive fingerprints cannot collide with new project overrides.
        # User config is external, read-only state: returning to a previous mode
        # within the same persisted epoch deliberately restores its fingerprint.
        revision = -(epoch * 3 + {'off': 0, 'capture': 1, 'review': 2}[mode])
        return {'project_id': pid, 'mode': mode, 'revision': revision, 'source': source}

    @staticmethod
    def policy_epoch(c, pid):
        """Retained high-water, with read-only discovery for pre-amendment stores."""
        tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'hub_experience_policy_epochs' in tables:
            saved = c.execute('SELECT highwater FROM hub_experience_policy_epochs WHERE project_id=?', (pid,)).fetchone()
            if saved:
                return saved[0]
        # Initialize from surviving history only on the next authorized policy
        # mutation. Reads never create an epoch row or migrate an old database.
        epoch = 0
        for table, column in (('hub_experience_policies', 'revision'),
                              ('hub_experiences', 'policy_revision'),
                              ('hub_experience_captures', 'policy_revision')):
            if table in tables:
                maximum = c.execute(f'SELECT MAX({column}) FROM {table} WHERE project_id=?', (pid,)).fetchone()[0]
                epoch = max(epoch, maximum or 0)
        if {'hub_experiences', 'hub_experience_reviews'} <= tables:
            maximum = c.execute('SELECT MAX(r.policy_revision) FROM hub_experience_reviews r '
                                'JOIN hub_experiences e USING(report_id) WHERE e.project_id=?', (pid,)).fetchone()[0]
            epoch = max(epoch, maximum or 0)
        return epoch

    def policy_in(self, c, pid):
        row = c.execute('SELECT * FROM hub_experience_policies WHERE project_id=?', (pid,)).fetchone()
        return dict(row, source='project') if row else self.default_policy(pid, epoch=self.policy_epoch(c, pid))

    def policy(self, pid):
        if not self.available():
            return self.default_policy(pid)
        with self.hub.database() as db, db.transaction() as c:
            return self.policy_in(c, pid)

    def clear_policy(self, project, *, expected_revision=None):
        return self.set_policy(project, None, expected_revision=expected_revision, clear=True)

    def set_policy(self, project, mode, *, expected_revision=None, clear=False):
        self.operator()
        if not clear:
            enum(mode, {'off', 'capture', 'review'})
        from .rooms import resolve_project
        selected = resolve_project(project, env=self.hub.env)
        self.hub.initialize()
        with mutation_guard(self.hub.config), self.hub.database() as db, db.transaction(write=True) as c:
            old = self.policy_in(c, selected['project_id'])
            if expected_revision is not None and old['revision'] != expected_revision:
                raise StoreError('policy revision changed; inspect before retrying')
            epoch = max(self.policy_epoch(c, selected['project_id']), old['revision'], 0)
            if clear:
                epoch += old['source'] == 'project'
                c.execute('DELETE FROM hub_experience_policies WHERE project_id=?', (selected['project_id'],))
                mode = self.default_policy(selected['project_id'], epoch=epoch)['mode']
            else:
                changed = old['mode'] != mode or old['source'] != 'project' or old['revision'] <= 0
                revision = epoch + 1 if changed else old['revision']
                epoch = max(epoch, revision)
                c.execute('INSERT OR REPLACE INTO hub_experience_policies VALUES(?,?,?,?)',
                          (selected['project_id'], selected['root'], mode, revision))
            c.execute('INSERT OR REPLACE INTO hub_experience_policy_epochs VALUES(?,?)',
                      (selected['project_id'], epoch))
            if mode != 'review':
                # Only owned learning utilities are cancelled, never hub workers.
                owned = c.execute("SELECT v.utility_id FROM hub_experience_reviews v JOIN hub_experiences e USING(report_id) WHERE e.project_id=? AND v.status IN ('selected','reserved','running','budget-deferred')", (selected['project_id'],)).fetchall()
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='managed_sessions'").fetchone():
                    for row in owned:
                        if row[0]:
                            c.execute('UPDATE managed_sessions SET stop_requested=1 WHERE session_id=?', (row[0],))
                c.execute("UPDATE hub_experience_reviews SET status='policy-deferred' WHERE report_id IN (SELECT report_id FROM hub_experiences WHERE project_id=?) AND status IN ('selected','reserved','running','budget-deferred')", (selected['project_id'],))
        return self.policy(selected['project_id'])

    def completion_enabled(self, row):
        return (row['transport'] == 'terminal' and row.get('purpose') != 'experience-review'
                and self.policy(row['project_id'])['mode'] != 'off' and not silenced(row['project']))

    @staticmethod
    def completion_text(key=None):
        command = 'asha control session report --state finished --experience-file FILE'
        command += ' --key ' + (key or 'UUID')
        return ('Provide one bounded UTF-8 JSON assessment with contract="asha.session-experience.v1": '
                'assessment observations|none-observed|insufficient-evidence, outcome succeeded|partial|failed|unknown, '
                'summary, observations (at most three), evidence (at most four); at most 16 KiB. '
                'Observations require key, kind, observed, evidence_ids and uncertainty; evidence is an '
                'agent-attestation with id/kind/text or a project-file with id/kind/path/sha256. '
                'Keep facts, hypotheses and uncertainty separate. Submit with `' + command + '`. '
                'Capture is advisory and independent of task completion.')

    def reconcile_completion(self, row, *, observed_exit=False):
        """Record unanswered completion on verified exit, never infer report content."""
        request = row.get('experience_request') or {}
        if (row['transport'] != 'terminal' or request.get('generation') != row['generation']
                or request.get('status') != 'pending'):
            return row
        if not observed_exit:
            from .rooms import RoomStore, _owned_state
            room = RoomStore(self.hub.config).read(row['room_id'])
            if _owned_state(room, self.hub.tmux)[0] not in {'ended', 'missing'}:
                return row
        with mutation_guard(self.hub.config), self.hub.database() as db, db.transaction(write=True) as c:
            current = self.current(c, row)
            fresh = current.get('experience_request') or {}
            if fresh.get('key') != request['key'] or fresh.get('status') != 'pending':
                return current
            c.execute("UPDATE hub_experience_captures SET reason='exited-before-capture' "
                      "WHERE session_id=? AND generation=? AND source='completion' AND delivery_key=? "
                      "AND status='missing' AND report_id IS NULL", (row['session_id'], row['generation'], request['key']))
            capture = c.execute("SELECT status,report_id,reason FROM hub_experience_captures "
                      "WHERE session_id=? AND generation=? AND source='completion' AND delivery_key=?",
                      (row['session_id'], row['generation'], request['key'])).fetchone()
            if capture and capture['reason'] == 'exited-before-capture':
                current['capture'] = dict(capture)
                current['experience_request'] = dict(fresh, status='missing', reason='exited-before-capture')
                self.hub._save(c, current)
            return current

    def close_capture(self, row, request_id):
        policy = self.policy(row['project_id'])
        from .session_publication import saved_current_assignment
        if saved_current_assignment(self.hub, row):
            return {'status': 'disabled', 'requested': False, 'reason': 'explicit-save-published',
                    'policy_revision': policy['revision'], 'report_id': None}
        requested = policy['mode'] != 'off' and not silenced(row['project']) and row.get('purpose') != 'experience-review'
        return {'status': 'missing' if requested else 'disabled', 'requested': requested,
                'policy_revision': policy['revision'], 'report_id': None}

    def retained_close_capture(self, row, request_id):
        capture = row.get('closure', {}).get('capture', {})
        if not capture.get('report_id') or not self.available():
            return None
        with self.hub.database() as db, db.transaction() as c:
            saved = c.execute("""SELECT e.* FROM hub_experiences e JOIN hub_experience_captures a
                ON a.report_id=e.report_id WHERE a.session_id=? AND a.generation=? AND a.source='close'
                AND a.close_request_id=? AND a.report_id=?""",
                (row['session_id'], row['generation'], request_id, capture['report_id'])).fetchone()
            if not saved or saved['digest'] != capture.get('digest'):
                raise StoreError('retained close capture scope or digest differs')
            if c.execute('SELECT 1 FROM hub_experiences WHERE supersedes=?', (saved['report_id'],)).fetchone():
                raise StoreError('retained close capture was superseded; explicitly attach the correction')
            return self.receipt(dict(saved))

    @staticmethod
    def current(c, row):
        fresh = c.execute('SELECT payload FROM hub_sessions WHERE session_id=?', (row['session_id'],)).fetchone()
        current = json.loads(fresh[0]) if fresh else {}
        if current.get('generation') != row['generation'] or current.get('lifecycle') not in {'starting', 'open', 'closing'}:
            raise StoreError('stale or inactive session reporter')
        return current

    @staticmethod
    def selection(body, report_id, revision):
        if body['assessment'] == 'insufficient-evidence':
            return 'insufficient-evidence', 'assessment'
        if any(o['kind'] in KINDS or o.get('lesson') for o in body['observations']):
            return 'selected', 'observation-trigger'
        sampled = int(sha(report_id + ':' + str(revision))[:8], 16) % 100 < 10
        return ('selected', 'routine-sample') if sampled else ('not-selected', 'routine-not-sampled')

    def capture(self, row, *, source, key, experience_file=None, experience_ref=None, supersedes=None, requested=False, close_request_id=None, submitted_body=None):
        self.hub.initialize()  # Additive migration on an authorized mutation, never on reads.
        policy = self.policy(row['project_id'])
        disabled = policy['mode'] == 'off' or silenced(row['project']) or row.get('purpose') == 'experience-review'
        saved = source == 'close' and row.get('closure', {}).get('capture', {}).get('reason') == 'explicit-save-published'
        disabled = disabled or saved
        receipt = {'status': 'disabled' if disabled else 'missing', 'report_id': None}
        if saved:
            receipt['reason'] = 'explicit-save-published'
        value = body_digest = None
        try:
            string(key, 256)
            if not disabled:
                if experience_file and experience_ref or supersedes and not experience_file:
                    raise ValueError('choose one report file or unchanged reference')
                if submitted_body is not None:
                    value, body_digest = capture_body(submitted_body, row)
                elif experience_file:
                    value, body_digest = read_report(experience_file, row)
                elif experience_ref:
                    identifier(experience_ref)
        except (ValueError, OSError):
            receipt.update(status='invalid', reason='optional report refused: invalid, unsafe, oversized or known secret-bearing input omitted')
        with mutation_guard(self.hub.config), self.hub.database() as db, db.transaction(write=True) as c:
            current = self.current(c, row)
            if source == 'close':
                record = current.get('closure')
                if not record or record['request_id'] != close_request_id or record.get('handoff'):
                    raise StoreError('stale close capture')
            policy = self.policy_in(c, row['project_id'])
            if silenced(row['project']) or policy['mode'] == 'off':
                value = None; experience_ref = None
                receipt = {'status': 'disabled', 'report_id': None}
            old_capture = c.execute('SELECT * FROM hub_experience_captures WHERE session_id=? AND generation=? AND source=? AND delivery_key=?',
                (row['session_id'], row['generation'], source, key)).fetchone()
            if old_capture and old_capture['report_id'] and receipt['status'] != 'disabled':
                saved = dict(c.execute('SELECT * FROM hub_experiences WHERE report_id=?', (old_capture['report_id'],)).fetchone())
                if submitted_body is None and not experience_file and not experience_ref and not supersedes:
                    return self.receipt(saved)
                if experience_ref == saved['report_id'] or (value is not None and body_digest == saved['digest'] and supersedes == saved['supersedes']):
                    return self.receipt(saved)
                return {'status': 'invalid', 'report_id': None, 'reason': 'delivery key already binds different content'}
            if value is not None or (experience_ref and receipt['status'] != 'invalid'):
                c.execute('SAVEPOINT optional_experience')
                try:
                    receipt = self._retain(c, current, source, key, policy, value, body_digest, experience_ref, supersedes, close_request_id)
                except ValueError:
                    c.execute('ROLLBACK TO optional_experience')
                    receipt = {'status': 'invalid', 'report_id': None, 'reason': 'optional report refused: scope, digest, reference or exposure mismatch'}
                finally:
                    c.execute('RELEASE optional_experience')
            old = c.execute('SELECT * FROM hub_experience_captures WHERE session_id=? AND generation=? AND source=? AND delivery_key=?',
                            (row['session_id'], row['generation'], source, key)).fetchone()
            # Invalid retries cannot erase a durable assessment receipt.
            if not old or not old['report_id']:
                c.execute('INSERT OR REPLACE INTO hub_experience_captures VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                          (row['session_id'], row['generation'], source, key, row['project_id'], int(requested), policy['revision'],
                           receipt['status'], receipt['report_id'], receipt.get('reason'), time.time(), close_request_id))
        return receipt

    def optional_capture(self, row, **arguments):
        from .database import DatabaseError
        try:
            return self.capture(row, **arguments)
        except DatabaseError:
            # The transaction has rolled back. Ordinary hub acknowledgement still
            # has to persist successfully through its existing authority seam.
            return {'status': 'invalid', 'report_id': None, 'reason': 'optional capture storage failed; no capture success claimed'}

    def _retain(self, c, row, source, key, policy, value, body_digest, ref, supersedes, close_request_id):
        old = c.execute('SELECT * FROM hub_experiences WHERE session_id=? AND generation=? AND source=? AND delivery_key=?',
                        (row['session_id'], row['generation'], source, key)).fetchone()
        if old:
            if body_digest != old['digest'] or supersedes != old['supersedes']:
                raise ValueError('delivery key content differs')
            return self.receipt(dict(old))
        if ref:
            old = c.execute('SELECT * FROM hub_experiences WHERE report_id=?', (ref,)).fetchone()
            if not old or old['session_id'] != row['session_id'] or old['generation'] != row['generation'] or old['project_id'] != row['project_id']:
                raise ValueError('reference scope differs')
            return self.receipt(dict(old))
        previous = None
        if supersedes:
            previous = c.execute('SELECT * FROM hub_experiences WHERE report_id=?', (supersedes,)).fetchone()
            if not previous or previous['session_id'] != row['session_id'] or previous['generation'] != row['generation']:
                raise ValueError('correction scope differs')
            if c.execute('SELECT 1 FROM hub_experiences WHERE supersedes=?', (supersedes,)).fetchone():
                raise ValueError('correction already replaced')
        versions = c.execute("SELECT DISTINCT json_extract(g.value,'$.id'),json_extract(g.value,'$.version') FROM hub_guidance_exposures e,json_each(e.manifest,'$.supplied') g WHERE e.session_id=? AND e.generation=? AND e.status='supplied' ORDER BY 1,2",
                             (row['session_id'], row['generation'])).fetchall()
        supplied = {(r[0], r[1]) for r in versions}
        if any((g['id'], g['version']) not in supplied for g in value.get('guidance_feedback', [])):
            raise ValueError('feedback claims unsupplied guidance version')
        rid = str(uuid.uuid4())
        from .session_selection import evidence as selection_evidence
        selection = selection_evidence(row)
        # Requested/effective/provenance (#95); unreported stays explicitly unknown.
        envelope = {'harness': row['harness'], 'harness_version': None, 'model': selection['model'],
                    'effort': selection['effort'], 'model_version': None,
                    'native_session_id': row.get('native_id'),
                    'version_provenance': 'unknown', 'supplied_guidance': [{'id': r[0], 'version': r[1]} for r in versions[:64]],
                    'supplied_guidance_count': len(versions), 'supplied_guidance_complete': len(versions) <= 64,
                    'evidence_digest': sha(canonical(value['evidence']))}
        assignment = row.get('current_assignment', row.get('prompt'))
        try:
            string(assignment, 16000)
            safe_content(assignment)
        except ValueError:
            envelope.update(assignment=None, assignment_coverage='omitted: unavailable, oversized or private assignment')
        else:
            envelope.update(assignment=assignment, assignment_coverage='latest Control assignment; native conversational followups unknown')
        c.execute('INSERT INTO hub_experiences VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (rid, row['session_id'], row['generation'], row['project_id'], source, key,
                   close_request_id if source == 'close' else None, policy['revision'], body_digest, canonical(value), canonical(envelope),
                   supersedes, previous['origin_report_id'] if previous else rid, time.time()))
        status, reason = self.selection(value, rid, policy['revision'])
        if policy['mode'] != 'review' and status == 'selected':
            status = 'disabled'
        c.execute('INSERT INTO hub_experience_reviews(review_id,report_id,report_digest,policy_revision,reason,status,created_at) VALUES(?,?,?,?,?,?,?)',
                  (str(uuid.uuid4()), rid, body_digest, policy['revision'], reason, status, time.time()))
        if previous:
            c.execute("UPDATE hub_experience_reviews SET status='superseded' WHERE report_id=? AND status!='completed'", (supersedes,))
        return {'status': 'captured' if value['assessment'] == 'observations' else value['assessment'], 'report_id': rid, 'digest': body_digest}

    @staticmethod
    def receipt(row):
        body = json.loads(row['body'])
        return {'status': 'captured' if body['assessment'] == 'observations' else body['assessment'],
                'report_id': row['report_id'], 'digest': row['digest']}

    def show(self, rid):
        identifier(rid)
        with self.hub.database() as db, db.transaction() as c:
            row = c.execute('SELECT * FROM hub_experiences WHERE report_id=?', (rid,)).fetchone()
            if not row:
                raise StoreError('experience report not found')
            result = dict(row)
            result['body'] = json.loads(result['body']); result['envelope'] = json.loads(result['envelope'])
            result['reviews'] = [dict(r) for r in c.execute('SELECT * FROM hub_experience_reviews WHERE report_id=? ORDER BY created_at,review_id', (rid,))]
        return result

    def stats(self, pid, **filters):
        from .experience_stats import stats
        return stats(self.hub, pid, **filters)

    def page(self, pid, *, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise StoreError('invalid experience page')
        if not self.available():
            return {'rows': [], 'complete': True, 'next_offset': None, 'total': 0}
        with self.hub.database() as db, db.transaction() as c:
            rows = [dict(r) for r in c.execute('SELECT report_id,session_id,generation,source,digest,created_at FROM hub_experiences WHERE project_id=? ORDER BY created_at,report_id LIMIT ? OFFSET ?', (pid, limit, offset))]
            total = c.execute('SELECT COUNT(*) FROM hub_experiences WHERE project_id=?', (pid,)).fetchone()[0]
        end = offset + len(rows)
        return {'rows': rows, 'total': total, 'complete': end >= total, 'next_offset': end if end < total else None}


def structured_completion(hub, row, delivery_key, summary, *, truncated=False):
    """Only an explicitly selected result contract is parsed as an envelope."""
    if row.get('result_contract') != 'asha.session-result.v1':
        return {'summary': summary}
    try:
        if truncated:
            raise ValueError('native structured envelope was truncated')
        envelope = strict_json(summary.encode(), maximum=32 * 1024)
        shape(envelope, ('contract', 'result'), ('experience',))
        enum(envelope['contract'], {'asha.session-result.v1'})
        string(envelope['result'], 16000)
        raw = canonical(envelope['experience']).encode() if 'experience' in envelope else None
        capture = Experiences(hub).optional_capture(row, source='completion', key=delivery_key, submitted_body=raw)
        return {'summary': envelope['result'], 'capture': capture}
    except (ValueError, OSError):
        # Keep structured lifecycle success separate from malformed optional capture.
        capture = Experiences(hub).optional_capture(row, source='completion', key=delivery_key, submitted_body=b'{}')
        return {'summary': 'Explicit structured result envelope was invalid; optional content omitted', 'capture': capture}


class StructuredResult:
    """Collect only explicitly selected envelopes at both native event seams.

    Text may contain private optional evidence. Validate before releasing any
    result to ordinary retained output; oversize/malformed envelopes leave a gap.
    """
    def __init__(self, hub, row, key):
        self.hub, self.row, self.key = hub, row, key
        self.selected = row.get('result_contract') == 'asha.session-result.v1'
        self.parts = []
        self.size = 0
        self.overflow = False

    def event(self, kind, payload):
        if not self.selected:
            return payload
        if kind == 'text':
            text = payload.get('text', '')
            self.size += len(text.encode())
            if self.size > 32 * 1024:
                self.overflow = True
                self.parts.clear()
            elif not self.overflow:
                self.parts.append(text)
            return None
        if kind == 'completed':
            summary = ''.join(self.parts) if self.parts else payload.get('summary', '')
            value = structured_completion(self.hub, self.row, self.key, summary,
                truncated=self.overflow or payload.get('summary_truncated', False))
            self.hub._update(self.row['session_id'], expected_generation=self.row['generation'], capture=value['capture'])
            return dict(payload, summary=value['summary'])
        if kind == 'failed':
            # Native failure summaries can contain the same rejected envelope.
            value = structured_completion(self.hub, self.row, self.key, '', truncated=True)
            self.hub._update(self.row['session_id'], expected_generation=self.row['generation'], capture=value['capture'])
            return {'summary': 'Native task failed; optional result content omitted', 'reason': 'native task failed'}
        return payload
