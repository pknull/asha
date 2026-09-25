"""Bounded active-learning selections at native assignment seams."""
from __future__ import annotations
import json
import time
import uuid
from .session_experience import canonical, sha, safe_content, string, silenced
from .store import StoreError
from .registry_guards import mutation_guard
from .session_selection import evidence as selection_evidence


def resolve(hub, row, selections):
    import learnings_manager as lm
    from .session_experience import Experiences
    automatic = selections is None and row['profile'] == 'worker'
    selection = 'automatic' if automatic else 'explicit' if selections else 'none'
    if automatic:
        if silenced(row['project']):
            selections = []
        else:
            with lm.at_home(hub.config.asha_home):
                active = lm.list_state('active')
            def order(rule):
                scope = rule.applicability
                sources = {e.session_id for e in rule.evidence if e.session_id not in {'', 'unknown'}}
                return (not bool(scope.get('project_ids')), not bool(scope.get('harnesses')), -len(sources), rule.id)
            selections = [rule.id for rule in sorted(active, key=order)
                          if (not rule.applicability.get('project_ids') or row['project_id'] in rule.applicability['project_ids'])
                          and (not rule.applicability.get('harnesses') or row['harness'] in rule.applicability['harnesses'])]
    elif selections is None:
        selections = []
    if not isinstance(selections, list) or (not automatic and len(selections) > 20):
        raise StoreError('at most 20 explicit selections may be inspected; at most three supplied')
    manifest = {'selection': selection, 'selected': selections, 'supplied': [], 'excluded': [], 'harness': row['harness'],
                'policy_revision': Experiences(hub).policy(row['project_id'])['revision'],
                'harness_version': None, 'version_provenance': 'unknown', **selection_evidence(row)}
    block = ''
    seen = set()
    for selected in selections:
        string(selected, 256)
        lid, _, expected = selected.partition('@')
        reason = None
        if silenced(row['project']):
            reason = 'silence'
        elif lid in seen:
            reason = 'duplicate-selection'
        seen.add(lid)
        try:
            with lm.at_home(hub.config.asha_home):
                learning = lm.load(lid) if reason is None else None
        except (ValueError, KeyError, OSError):
            learning = None; reason = 'unavailable'
        if learning:
            version = lm.rule_version(learning)
            scope = learning.applicability
            if learning.state != 'active':
                reason = 'not-active'
            elif expected and expected != version:
                reason = 'stale-version'
            elif scope.get('harnesses') and row['harness'] not in scope['harnesses']:
                reason = 'incompatible-harness'
            elif scope.get('project_ids') and row['project_id'] not in scope['project_ids']:
                reason = 'incompatible-project'
            elif len(manifest['supplied']) >= 3:
                reason = 'rule-count-limit'
            if reason is None:
                rule = {'id': lid, 'version': version, 'trigger': learning.trigger, 'action': learning.action,
                        'applicability': scope, 'version_applicability': 'unknown' if scope.get('versions') else 'unspecified'}
                try:
                    safe_content(rule)
                except ValueError:
                    reason = 'private-content-omitted'
                else:
                    candidate = '\nSelected active guidance (scope limits apply):\n' + canonical([*manifest['supplied'], rule])
                    if len(candidate.encode()) > 3 * 1024:
                        reason = 'byte-limit'
                    else:
                        manifest['supplied'].append(rule); block = candidate
        if reason:
            manifest['excluded'].append({'selection': selected, 'reason': reason})
    manifest['digest'] = sha(block)
    return block, manifest


def planned(manifest, body, block):
    return dict(manifest, planned_block=block, base_digest=sha(body), assignment_digest=sha(body + block))


def retain_in(c, row, key, manifest, *, status='queued'):
    old = c.execute('SELECT * FROM hub_guidance_exposures WHERE session_id=? AND generation=? AND delivery_key=?',
                    (row['session_id'], row['generation'], key)).fetchone()
    if old:
        prior = json.loads(old['manifest'])
        if (prior['selected'] != manifest['selected'] or prior.get('selection', 'explicit') != manifest.get('selection', 'explicit')
                or prior.get('assignment_digest') != manifest.get('assignment_digest')):
            raise StoreError('guidance delivery key already binds another manifest')
        return dict(old)
    if silenced(row['project']):
        return None
    if not manifest['selected']:
        status = 'none'
    c.execute('INSERT INTO hub_guidance_exposures VALUES(?,?,?,?,?,?,?,?)',
              (str(uuid.uuid4()), row['session_id'], row['generation'], key, row['project_id'], status,
               canonical(manifest), time.time()))
    return manifest


def retain(hub, row, key, manifest, *, status='queued'):
    if silenced(row['project']):
        return None
    hub.initialize()
    with mutation_guard(hub.config), hub.database() as db, db.transaction(write=True) as c:
        return retain_in(c, row, key, manifest, status=status)


def delivery(hub, row, key, body):
    """Resolve frozen selections immediately before rendering native input.

    Immutable queued messages retain custody digests. The rendered input has its
    own digest; changes exclude obsolete rules rather than substitute new semantics.
    """
    from .session_experience import Experiences
    if not Experiences(hub).available():
        return body, None
    with hub.database() as db, db.transaction() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='hub_guidance_exposures'").fetchone():
            return body, None
        found = c.execute('SELECT * FROM hub_guidance_exposures WHERE session_id=? AND generation=? AND delivery_key=?',
                          (row['session_id'], row['generation'], key)).fetchone()
        if not found:
            # Silence may suppress new learning records during recovery. Existing
            # queued bytes still need their original custody to exclude old rules.
            found = c.execute("SELECT * FROM hub_guidance_exposures WHERE session_id=? AND generation<? AND delivery_key=? AND json_extract(manifest,'$.assignment_digest')=? ORDER BY generation DESC LIMIT 1",
                              (row['session_id'], row['generation'], key, sha(body))).fetchone()
    if not found:
        return body, None
    old = json.loads(found['manifest'])
    suffix = old.get('planned_block', '')
    base = body
    if sha(body) != old.get('base_digest') and suffix and body.endswith(suffix):
        base = body[:-len(suffix)]
    if old.get('base_digest') and sha(base) != old['base_digest']:
        raise StoreError('message differs from the retained guidance assignment')
    pins = [rule['id'] + '@' + rule['version'] for rule in old['supplied']]
    block, current = resolve(hub, row, pins)
    current['selected'] = old['selected']
    current['selection'] = old.get('selection', 'explicit' if old['selected'] else 'none')
    current['excluded'] = old['excluded'] + current['excluded']
    current.update(base_digest=sha(base), assignment_digest=old.get('assignment_digest'),
                   planned_block=block, delivery_digest=sha(base + block))
    return base + block, current


def offered(hub, row, key, manifest):
    """Retain at most eight emitted bodies; an acknowledgement names its digest.

    Three pinned rules can only be included or excluded (eight combinations).
    Eviction refuses a late acknowledgement rather than crediting another body.
    This is delivery custody, not a supply or use receipt.
    """
    if silenced(row['project']):
        return False
    from .session_experience import Experiences
    with mutation_guard(hub.config), hub.database() as db, db.transaction(write=True) as c:
        Experiences.current(c, row)
        found = c.execute("SELECT * FROM hub_guidance_exposures WHERE session_id=? AND generation=? AND delivery_key=? AND status='queued'",
                          (row['session_id'], row['generation'], key)).fetchone()
        if not found or silenced(row['project']):
            return False
        old = json.loads(found['manifest'])
        receipts = old.setdefault('emitted', {})
        digest = manifest['delivery_digest']
        if digest in receipts:
            return True
        if len(receipts) >= 8:
            receipts.pop(next(iter(receipts)))
        receipts[digest] = manifest
        c.execute('UPDATE hub_guidance_exposures SET manifest=? WHERE exposure_id=?', (canonical(old), found['exposure_id']))
        return True


def receipt_in(c, row, key, digest):
    if digest is None:
        return None  # Legacy acknowledgement provides no exact guidance receipt.
    found = c.execute('SELECT * FROM hub_guidance_exposures WHERE session_id=? AND generation=? AND delivery_key=?',
                      (row['session_id'], row['generation'], key)).fetchone()
    if found:
        old = json.loads(found['manifest'])
        if found['status'] == 'supplied' and old.get('delivery_digest') == digest:
            return old
        if digest in old.get('emitted', {}):
            return old['emitted'][digest]
    raise StoreError('guidance delivery digest is unknown or evicted; read the current message again')


def carry_queued_in(c, row, previous_generation):
    """Recovery creates new exposure records without rewriting prior history."""
    rows = c.execute("SELECT e.* FROM hub_guidance_exposures e JOIN session_messages m ON m.session_id=e.session_id AND m.delivery_key=e.delivery_key WHERE e.session_id=? AND e.generation=? AND m.state='queued'",
                     (row['session_id'], previous_generation)).fetchall()
    for old in rows:
        manifest = json.loads(old['manifest'])
        manifest.pop('emitted', None)
        manifest['carried_from'] = old['exposure_id']
        retain_in(c, row, old['delivery_key'], manifest)


def supplied(hub, row, key, manifest=None):
    from .session_experience import Experiences
    if not Experiences(hub).available() or silenced(row['project']):
        return False
    with mutation_guard(hub.config), hub.database() as db, db.transaction(write=True) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='hub_guidance_exposures'").fetchone():
            return False
        fresh = Experiences.current(c, row)
        if fresh['generation'] != row['generation']:
            raise StoreError('guidance receipt is stale')
        if manifest is None:
            c.execute("UPDATE hub_guidance_exposures SET status='supplied' WHERE session_id=? AND generation=? AND delivery_key=? AND status='queued'",
                      (row['session_id'], row['generation'], key))
        else:
            c.execute("UPDATE hub_guidance_exposures SET status='supplied',manifest=? WHERE session_id=? AND generation=? AND delivery_key=? AND status='queued'",
                      (canonical(manifest), row['session_id'], row['generation'], key))
        return bool(c.execute("SELECT 1 FROM hub_guidance_exposures WHERE session_id=? AND generation=? AND delivery_key=? AND status='supplied'",
                              (row['session_id'], row['generation'], key)).fetchone())


def history(hub, sid):
    if not hub.initialized():
        return []
    with hub.database() as db, db.transaction() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='hub_guidance_exposures'").fetchone():
            return []
        rows = [dict(r) for r in c.execute('SELECT * FROM hub_guidance_exposures WHERE session_id=? ORDER BY created_at,exposure_id LIMIT 101', (sid,))]
    for row in rows:
        row['manifest'] = json.loads(row['manifest'])
    return rows
