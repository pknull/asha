"""Explicit-save disposition receipts bridging private SQLite and learning files."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from .session_experience import (Experiences, canonical, sha, shape, string, enum,
    silenced, safe_content, validate_digest, memory_v2, review_subjects)
from .store import StoreError
from .registry_guards import mutation_guard

DISPOSITIONS = {'propose', 'corroborate', 'project-decision', 'code-test-followup', 'reject', 'defer'}


def pending(hub, project_id, *, offset=0, limit=50):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise StoreError('invalid finding page')
    if not Experiences(hub).available():
        return {'rows': [], 'total': 0, 'complete': True, 'next_offset': None}
    with hub.database() as db, db.transaction() as c:
        query = """FROM hub_experience_reviews r JOIN hub_experiences e USING(report_id),
                   json_each(json_extract(r.result,'$.review.findings')) f
                   WHERE e.project_id=? AND r.status='completed'
                   AND NOT EXISTS (SELECT 1 FROM hub_experiences newer WHERE newer.supersedes=e.report_id)"""
        rows = c.execute('SELECT r.review_id,r.report_id,e.digest AS report_digest,f.value AS finding, '
                         'e.body AS report_body,e.envelope AS report_envelope,e.project_id ' + query +
                         " ORDER BY r.created_at,r.review_id,f.key LIMIT ? OFFSET ?", (project_id, limit, offset)).fetchall()
        total = c.execute('SELECT COUNT(*) ' + query, (project_id,)).fetchone()[0]
        result = []
        for row in rows:
            value = dict(row); value['finding'] = json.loads(value['finding']); value['finding_digest'] = sha(canonical(value['finding']))
            report = {'body': json.loads(value.pop('report_body')), 'envelope': json.loads(value.pop('report_envelope')),
                      'project_id': value.pop('project_id')}
            subject = next((item for item in review_subjects(report) if item['key'] == value['finding']['key']), None)
            if subject is None:
                raise StoreError('reviewed finding subject is unavailable')
            value['subject_kind'] = subject['subject_kind']
            disposition = c.execute('SELECT state,payload FROM hub_experience_dispositions WHERE review_id=? AND observation_key=? ORDER BY created_at DESC,disposition_id DESC LIMIT 1', (value['review_id'], value['finding']['key'])).fetchone()
            value['disposition'] = {'state': disposition['state'], 'decision': json.loads(disposition['payload'])['disposition']} if disposition else None
            result.append(value)
    end = offset + len(result)
    return {'rows': result, 'total': total, 'complete': end >= total, 'next_offset': end if end < total else None}


def _complete(hub, disposition_id, receipt, project):
    if silenced(project):
        raise StoreError('learning disposition completion deferred by silence; retain intent for reconciliation')
    with mutation_guard(hub.config), hub.database() as db, db.transaction(write=True) as c:
        if silenced(project):
            raise StoreError('learning disposition completion deferred by silence')
        row = c.execute('SELECT state,payload,project_id FROM hub_experience_dispositions WHERE disposition_id=?', (disposition_id,)).fetchone()
        value = json.loads(row['payload'])
        if row['state'] == 'completed':
            return {'disposition_id': disposition_id, 'state': 'completed', **value['receipt']}
        if Experiences.policy_in(c, row['project_id'])['mode'] == 'off':
            raise StoreError('learning disposition deferred by policy off; intent retained')
        value['receipt'] = receipt
        c.execute("UPDATE hub_experience_dispositions SET state='completed',payload=? WHERE disposition_id=?",
                  (canonical(value), disposition_id))
    return {'disposition_id': disposition_id, 'state': 'completed', **receipt}


def dispose(hub, project, decision, publication, *, save_session_id):
    """Publication is the saving agent's validated receipt, never a new credential.

    Operator ancestry and project plane remain authoritative. This operation is
    deliberately invoked by the explicit save procedure; it never publishes Memory.
    """
    import learnings_manager as lm
    experiences = Experiences(hub); experiences.operator()
    from .rooms import resolve_project
    selected = resolve_project(project, env=hub.env)
    root, pid = Path(selected['root']), selected['project_id']
    if silenced(root) or experiences.policy(pid)['mode'] == 'off':
        raise StoreError('learning adoption is disabled by silence or policy off')
    string(save_session_id, 256)
    if save_session_id == 'unknown':
        raise StoreError('explicit save identity is unavailable')
    shape(decision, ('review_id', 'observation_key', 'finding_digest', 'save_key', 'disposition', 'reason'),
          ('rule_id', 'rule_version', 'trigger', 'action'))
    enum(decision['disposition'], DISPOSITIONS)
    for key in ('review_id', 'observation_key', 'save_key', 'reason'):
        string(decision[key], 1000)
    validate_digest(decision['finding_digest']); safe_content(decision)
    if (not isinstance(publication, dict) or publication.get('contract') != 'asha.memory-publication.v1'
            or publication.get('status') != 'published' or publication.get('source') != 'explicit-save'
            or publication.get('project_id') != pid):
        raise StoreError('successful explicit project-save publication receipt required')
    try:
        uuid.UUID(publication['publication_id'])
        for value in publication['after'].values():
            validate_digest(value)
        if set(publication['after']) != {'active', 'decisions'}:
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreError('invalid publication receipt') from exc
    publisher = {'session_id': save_session_id, 'project_id': pid, 'publication_id': publication['publication_id']}
    with mutation_guard(hub.config), hub.database() as db, db.transaction(write=True) as c:
        if silenced(root) or experiences.policy_in(c, pid)['mode'] == 'off':
            raise StoreError('adoption deferred by policy or silence')
        review = c.execute("SELECT r.*,e.project_id,e.origin_report_id,e.session_id,e.body,e.envelope FROM hub_experience_reviews r JOIN hub_experiences e USING(report_id) WHERE review_id=?", (decision['review_id'],)).fetchone()
        if not review or review['project_id'] != pid or review['status'] != 'completed':
            raise StoreError('reviewed finding is unavailable in this project plane')
        if c.execute('SELECT 1 FROM hub_experiences WHERE supersedes=?', (review['report_id'],)).fetchone():
            raise StoreError('superseded review cannot authorize adoption')
        findings = json.loads(review['result'])['review']['findings']
        finding = next((f for f in findings if f['key'] == decision['observation_key']), None)
        if not finding or sha(canonical(finding)) != decision['finding_digest']:
            raise StoreError('finding digest changed; inspect original evidence before disposition')
        if decision['disposition'] in {'propose', 'corroborate'} and finding['verdict'] != 'supported':
            raise StoreError('adoption requires an evidence-supported reviewed finding')
        envelope = json.loads(review['envelope'])
        subjects = review_subjects({'body': json.loads(review['body']), 'envelope': envelope, 'project_id': pid})
        subject = next((item for item in subjects if item['key'] == finding['key']), None)
        if subject is None:
            raise StoreError('reviewed finding subject is unavailable')
        source = {'session_id': review['session_id'], 'project_id': pid,
            'origin_key': sha(canonical([pid, review['session_id'], review['origin_report_id'], finding['key']])),
            'report_id': review['report_id'], 'observation_key': finding['key'],
            'evidence_digest': envelope['evidence_digest'], 'adopting_save_identity': publisher,
            'harness': envelope['harness'], 'harness_version': envelope['harness_version']}
        if subject['subject_kind'] == 'reviewer-report-assessment':
            source['subject_kind'] = subject['subject_kind']
        intent = dict(decision, source=source, publication_id=publication['publication_id'])
        old = c.execute('SELECT * FROM hub_experience_dispositions WHERE review_id=? AND observation_key=? AND save_key=?',
                        (decision['review_id'], finding['key'], decision['save_key'])).fetchone()
        if old:
            prior = json.loads(old['payload'])
            if {k: v for k, v in prior.items() if k != 'receipt'} != intent:
                raise StoreError('disposition key already binds different intent')
            if old['state'] == 'completed':
                return {'disposition_id': old['disposition_id'], 'state': 'completed', **prior['receipt']}
            did = old['disposition_id']
        else:
            did = str(uuid.uuid4())
            import time
            c.execute('INSERT INTO hub_experience_dispositions VALUES(?,?,?,?,?,?,?,?,?)',
                      (did, decision['review_id'], finding['key'], decision['finding_digest'], decision['save_key'],
                       pid, 'intent', canonical(intent), time.time()))
    if silenced(root):
        raise StoreError('adoption deferred by silence')
    receipt = {'disposition': decision['disposition'], 'origin_key': source['origin_key'],
               'rule_id': None, 'rule_version': None, 'activated': False}
    action = decision['disposition']
    if action in {'propose', 'corroborate'}:
        rule_id = string(decision.get('rule_id'), 128)
        expected = decision.get('rule_version')
        # Check and apply under the manager's existing global lock. propose and
        # corroborate take that lock themselves; use a single manager operation
        # with expected semantic version so a concurrent editor is never undone.
        with hub.database() as db, db.transaction(write=True) as c, lm.at_home(hub.config.asha_home):
            if silenced(root) or experiences.policy_in(c, pid)['mode'] == 'off':
                raise StoreError('adoption deferred by policy or silence')
            learning = lm.adopt_reviewed(rule_id, project_dir=root, session_id=save_session_id,
                reason=('Reviewed report assessment ' if source.get('subject_kind') else 'Reviewed observation ') + source['origin_key'], source_provenance=source,
                operation=action, expected_version=expected, trigger=decision.get('trigger'),
                action=decision.get('action'), applicability=subject['applicability'])
            receipt.update(rule_id=learning.id, rule_version=lm.rule_version(learning))
            # Eligibility remains the manager's deliberate save operation. An
            # already-active rule is never rewritten to match this observation.
            lm.activate_if_eligible(rule_id, project_dir=root)
            receipt['activated'] = lm.load(rule_id).state == 'active'
    # Other destinations are dispositions, not issue creation or Memory edits.
    return _complete(hub, did, receipt, root)
