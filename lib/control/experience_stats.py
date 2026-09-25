"""Coverage counts and explicit unknowns; never causal effectiveness claims."""
from __future__ import annotations
import json
from collections import Counter
from .session_experience import Experiences
from .session_selection import evidence as selection_evidence, known_model
from .store import StoreError


def _attributed(item, row):
    """``(model, provenance)`` from retained evidence, else the session's request, else unknown."""
    if isinstance(item, dict) and (item.get('effective') or item.get('requested')):
        return item.get('effective') or item.get('requested'), item.get('provenance') or 'unknown'
    asked = ((row or {}).get('spec') or {}).get('model')
    return (asked, 'requested') if asked else ('unknown', 'unknown')


def stats(hub, project_id, *, since=0, until=None, policy_revision=None, harness=None, model=None):
    import time
    until = time.time() if until is None else until
    if since < 0 or until < since:
        raise StoreError('invalid experience statistics interval')
    if harness is not None and harness not in {'claude', 'codex', 'copilot', 'opencode'}:
        raise StoreError('invalid harness filter')
    result = {'scope': {'project_id': project_id, 'since': since, 'until': until,
                       'policy_revision': policy_revision, 'harness': harness, 'model': model},
              'capture': {'requested_closes': 0, 'assessment_receipts': 0, 'states': {}, 'closure_states': {}},
              'completions': {'explicit_reports': 0, 'assessment_receipts': 0, 'states': {}},
              'reviewers': {}, 'save_review_skips': {}, 'reviews': {}, 'yield': {'completed_reviews': 0, 'supported_actionable_findings': 0, 'destinations': {}, 'routine_sample_actionable': 0},
              'adoption': {'states': {}, 'decisions': {}, 'adopted_origins': 0},
              'guidance': {'selected': 0, 'supplied': 0, 'queued': 0, 'excluded': 0,
                           'assignment_use_unknown': 0, 'feedback_cohorts': 0,
                           'reported_use': {'applied': 0, 'not-applied': 0, 'not-applicable': 0, 'unknown': 0},
                           'feedback_scope': 'agent attestation per session generation and rule version; assignment-level use remains unknown'},
              'recurrence': {'observed': 0, 'not-observed': 0, 'unknown': 0, 'comparable_tasks': None,
                             'verified_improvement': None},
              'cost': {'launches': 0, 'reservations': 0, 'unknown_launches': 0, 'elapsed_seconds': 0, 'tokens': None, 'dollars': None, 'known_dollars': 0,
                       'unknown_usage_reviews': 0}, 'storage_bytes': 0,
              'time_basis': 'Report creation cohort; closes by request time, completions by receipt time, exposures by assignment time. Review/disposition counts use that report cohort.',
              'models': {}, 'current_sessions': {},
              'model_basis': ('models: closes, reports and completion captures by the selection evidence retained with each '
                              '(close request snapshot, report envelope); without it, the session\'s requested model or unknown, '
                              'never a later report. current_sessions: the live rows\' current selection, not history.'),
              'interpretation': 'Coverage of submitted observations only; supply is not use, and silence is not success.'}
    if not Experiences(hub).available():
        return result
    with hub.database() as db, db.transaction() as c:
        # Stable session lineage, not a resumed generation or reviewer identity.
        rows = c.execute("SELECT payload FROM hub_sessions WHERE json_extract(payload,'$.project_id')=?", (project_id,)).fetchall()
        selected_sessions = {}
        capture_states = Counter(); closure_states = Counter()
        models = {}
        current = {}

        def attribute(item, row, kind):
            """Count one event under the model evidence retained with it (#95)."""
            name, provenance = _attributed(item, row)
            if model is not None and name != model:
                return False
            entry = models.setdefault(name, {'closes': 0, 'reports': 0, 'completions': 0, 'provenance': Counter()})
            entry[kind] += 1
            entry['provenance'][provenance] += 1
            return True

        for stored in rows:
            row = json.loads(stored[0])
            if harness and row['harness'] != harness:
                continue
            selected_sessions[row['session_id']] = row
            known = known_model(row) or 'unknown'
            if model is None or known == model:
                entry = current.setdefault(known, {'sessions': 0, 'provenance': Counter()})
                entry['sessions'] += 1
                entry['provenance'][selection_evidence(row)['model']['provenance']] += 1
            for close in [*row.get('closure_history', []), row.get('closure')]:
                if not close or not since <= close['requested_at'] <= until:
                    continue
                capture = close.get('capture', {'status': 'disabled', 'requested': False, 'policy_revision': 0})
                if policy_revision is not None and capture.get('policy_revision') != policy_revision:
                    continue
                if not attribute((close.get('selection') or {}).get('model'), row, 'closes'):
                    continue
                capture_states[capture['status']] += 1; closure_states[close['state']] += 1
                if capture.get('requested'):
                    result['capture']['requested_closes'] += 1
                    result['capture']['assessment_receipts'] += bool(capture.get('report_id'))
        result['current_sessions'] = {name: {'sessions': entry['sessions'], 'provenance': dict(entry['provenance'])}
                                      for name, entry in sorted(current.items())}
        result['capture'].update(states=dict(capture_states), closure_states=dict(closure_states))
        reports = c.execute('SELECT * FROM hub_experiences WHERE project_id=? AND created_at>=? AND created_at<=? ORDER BY created_at,report_id', (project_id, since, until)).fetchall()
        report_ids = set()
        envelopes = {}
        feedback = {}
        for item in reports:
            if item['session_id'] not in selected_sessions or policy_revision is not None and item['policy_revision'] != policy_revision:
                continue
            envelopes[item['report_id']] = envelope = json.loads(item['envelope'])
            if not attribute(envelope.get('model'), selected_sessions[item['session_id']], 'reports'):
                continue
            report_ids.add(item['report_id'])
            result['storage_bytes'] += len(item['body'].encode()) + len(item['envelope'].encode())
            if not c.execute('SELECT 1 FROM hub_experiences WHERE supersedes=?', (item['report_id'],)).fetchone():
                for observation in json.loads(item['body']).get('guidance_feedback', []):
                    feedback[(item['session_id'], item['generation'], observation['id'], observation['version'])] = observation
        completions = c.execute("SELECT * FROM hub_experience_captures WHERE project_id=? AND source='completion' AND created_at>=? AND created_at<=?", (project_id, since, until)).fetchall()
        completion_states = Counter()
        for item in completions:
            if item['session_id'] not in selected_sessions or policy_revision is not None and item['policy_revision'] != policy_revision:
                continue
            # A capture with a report carries that report's envelope; one without
            # falls back to the session's immutable request, never a later report.
            envelope = envelopes.get(item['report_id']) if item['report_id'] else None
            if envelope is None and item['report_id']:
                stored = c.execute('SELECT envelope FROM hub_experiences WHERE report_id=?', (item['report_id'],)).fetchone()
                envelope = json.loads(stored[0]) if stored else None
            if not attribute((envelope or {}).get('model'), selected_sessions[item['session_id']], 'completions'):
                continue
            completion_states[item['status']] += 1
            result['completions']['assessment_receipts'] += bool(item['report_id'])
        result['completions'].update(explicit_reports=sum(completion_states.values()), states=dict(completion_states))
        reviews = c.execute('SELECT r.* FROM hub_experience_reviews r JOIN hub_experiences e USING(report_id) WHERE e.project_id=?', (project_id,)).fetchall()
        statuses = Counter(); destinations = Counter(); reviewers = Counter()
        for row in reviews:
            if row['report_id'] not in report_ids or policy_revision is not None and row['policy_revision'] != policy_revision:
                continue
            statuses[row['status']] += 1
            if row['reserved_at'] is not None:
                result['cost']['reservations'] += 1
                observed = c.execute('SELECT 1 FROM session_turns WHERE session_id=? AND provider_pid IS NOT NULL LIMIT 1', (row['utility_id'],)).fetchone()
                result['cost']['launches'] += bool(observed)
                result['cost']['unknown_launches'] += not bool(observed)
            if row['finished_at'] and row['reserved_at']:
                result['cost']['elapsed_seconds'] += max(0, row['finished_at'] - row['reserved_at'])
            if row['result']:
                output = json.loads(row['result']); result['storage_bytes'] += len(row['result'].encode())
                reviewers[output.get('reviewer') or 'native-automatic'] += 1
                cost = output.get('cost_usd')
                if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                    result['cost']['known_dollars'] += cost
                else:
                    result['cost']['unknown_usage_reviews'] += 1
                for finding in output['review']['findings']:
                    if finding['verdict'] == 'supported' and finding['destination'] != 'no-change':
                        destinations[finding['destination']] += 1
                        result['yield']['routine_sample_actionable'] += row['reason'] == 'routine-sample'
            elif row['reserved_at'] is not None:
                result['cost']['unknown_usage_reviews'] += 1
        result['reviews'] = dict(statuses)
        result['reviewers'] = dict(reviewers)
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='hub_experience_save_reviews'").fetchone():
            skips = c.execute("SELECT reason,report_id FROM hub_experience_save_reviews WHERE project_id=? AND status='skipped'", (project_id,)).fetchall()
            result['save_review_skips'] = dict(Counter(r['reason'] for r in skips if r['report_id'] in report_ids))
        result['yield'].update(completed_reviews=statuses['completed'], supported_actionable_findings=sum(destinations.values()), destinations=dict(destinations))
        dispositions = c.execute('SELECT * FROM hub_experience_dispositions WHERE project_id=? AND created_at>=? AND created_at<=?', (project_id, since, until)).fetchall()
        states = Counter(); decisions = Counter(); origins = set()
        included_reviews = {r['review_id'] for r in reviews if r['report_id'] in report_ids and (policy_revision is None or r['policy_revision'] == policy_revision)}
        finding_keys = {(r['review_id'], f['key']) for r in reviews if r['review_id'] in included_reviews and r['result']
                        and not c.execute('SELECT 1 FROM hub_experiences WHERE supersedes=?', (r['report_id'],)).fetchone()
                        for f in json.loads(r['result'])['review']['findings']}
        disposed = set()
        for row in dispositions:
            if row['review_id'] not in included_reviews:
                continue
            value = json.loads(row['payload']); states[row['state']] += 1; decisions[value['disposition']] += 1
            disposed.add((row['review_id'], value['observation_key']))
            if row['state'] == 'completed' and value['disposition'] in {'propose', 'corroborate'}:
                origins.add(value['source']['origin_key'])
        result['adoption'].update(states=dict(states), decisions=dict(decisions), adopted_origins=len(origins))
        result['adoption']['pending_findings'] = len(finding_keys - disposed)
        exposures = c.execute('SELECT * FROM hub_guidance_exposures WHERE project_id=? AND created_at>=? AND created_at<=?', (project_id, since, until)).fetchall()
        cohorts = set()
        for row in exposures:
            if row['session_id'] not in selected_sessions:
                continue
            manifest = json.loads(row['manifest'])
            if policy_revision is not None and manifest.get('policy_revision') != policy_revision:
                continue
            if model is not None and _attributed(manifest.get('model'), selected_sessions[row['session_id']])[0] != model:
                continue
            result['guidance']['selected'] += len(manifest['selected'])
            result['guidance']['excluded'] += len(manifest['excluded'])
            result['guidance']['supplied' if row['status'] == 'supplied' else 'queued'] += len(manifest['supplied'])
            if row['status'] == 'supplied':
                cohorts.update((row['session_id'], row['generation'], rule['id'], rule['version']) for rule in manifest['supplied'])
        result['guidance']['assignment_use_unknown'] = result['guidance']['supplied']
        result['guidance']['feedback_cohorts'] = len(cohorts)
        for cohort in cohorts:
            item = feedback.get(cohort, {})
            result['guidance']['reported_use'][item.get('use', 'unknown')] += 1
            failure = item.get('target_failure', 'unknown')
            result['recurrence'][failure] += 1
    result['models'] = {name: dict(entry, provenance=dict(entry['provenance']))
                        for name, entry in sorted(models.items())}
    return result
