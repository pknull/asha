"""Review and resolve global candidates through existing operator actions."""
from __future__ import annotations

import hashlib
import json

from .store import StoreError
from .orchestration.model import record_digest
from .orchestration.current_actions import _head_demand, approval_demand


def prepare(store, candidate):
    """Read a candidate's exact subject; never retain a lock during human review."""
    iid = candidate['initiative_id']
    head = store.peek(iid)
    if record_digest(head) != candidate['head_digest']:
        raise StoreError('initiative changed; refresh the action queue')
    subject = {}
    if candidate.get('request_id'):
        approval = store.read_approval(iid, candidate['request_id'])
        if record_digest(approval) != candidate['digest']:
            raise StoreError('request changed; refresh the action queue')
        demand = approval_demand(approval, head)
        if not demand or demand['disposition'] != 'pending-review':
            raise StoreError('request is no longer awaiting approval')
        kind = demand['kind']
        subject['approval'] = approval
        if kind == 'review-budget-approval':
            from .orchestration.review_budget import validate_request
            _, subject['binding'] = validate_request(store, iid, approval['request_id'])
        elif kind == 'salvage-approval':
            from .orchestration.actions import _salvage_request_records
            node, seal = _salvage_request_records(store, head, approval)
            subject.update(node=node, failure_seal=seal)
        else:
            raise StoreError('request has no supported approval handler')
        choices = ['approve', 'reject']
    else:
        if record_digest(head) != candidate['digest']:
            raise StoreError('initiative selection changed; refresh the action queue')
        if head['state'] not in {'awaiting-plan-approval', 'approved', 'needs-input', 'ready-for-integration'}:
            raise StoreError('initiative no longer awaits an operator action')
        kind = _head_demand(head)['kind']
        if kind in {'plan-approval', 'activation'}:
            from .orchestration.cli import _latest_plan
            subject['plan'] = _latest_plan(store, iid)
            choices = ['approve', 'reject'] if kind == 'plan-approval' else ['activate']
        elif kind == 'operator-decision':
            from .orchestration.actions import _open_operator_question
            subject['question'] = _open_operator_question(store, iid, head)
            subject['effect'] = ('Resume records the initiative question as resolved. '
                                 'This does not answer a managed session question or decide a paused seal.')
            # A paused seal has its own typed decision contract. Do not turn an
            # unexplained needs-input head into a generic approval button.
            choices = ['resume'] if subject['question'] is not None else []
        else:
            subject.update(seals=store.list_seals_snapshot(iid),
                           reviews=store.list_reviews_snapshot(iid),
                           verifications=store.list_verifications_snapshot(iid))
            subject['effect'] = 'Review these exact candidates to prepare integration; this browser does not merge work.'
            choices = []
    if kind != candidate['kind']:
        raise StoreError('action kind changed; refresh the action queue')
    reviewed = {'initiative': head, 'kind': kind, 'subject': subject, 'choices': choices}
    digest = hashlib.sha256(json.dumps(reviewed, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return {**reviewed, 'review_digest': digest}


def resolve(store, candidate, review_digest, choice, *, env, tmux, reason=None):
    """Recheck displayed records under the lifecycle lock before a typed act."""
    from .sessions import refuse_managed_operator
    from .orchestration.coordinator import refuse_coordinator_pane
    from .orchestration.actions import action_outcome, build_action_document, submit_action

    iid = candidate['initiative_id']
    refuse_managed_operator(store.config.control, env)
    refuse_coordinator_pane(store, iid, env, tmux)
    with store.transaction_lock(iid):
        current = prepare(store, candidate)
        if current['review_digest'] != review_digest:
            raise StoreError('reviewed subject changed; inspect the current action again')
        if choice not in current['choices']:
            raise StoreError('decision is not offered for this exact request')
        kind, head, subject = current['kind'], current['initiative'], current['subject']
        if kind == 'plan-approval':
            from .orchestration.cli import approve_plan, reject_plan
            digest = subject['plan']['digest']
            if choice == 'approve':
                approve_plan(store, head, digest, actor_id='tui')
                return 'plan approved; activation remains a separate action'
            if not isinstance(reason, str) or not reason.strip():
                raise StoreError('plan rejection requires a reason')
            reject_plan(store, head, digest, reason, actor_id='tui')
            return 'plan rejected'
        if kind in {'review-budget-approval', 'salvage-approval'}:
            if choice == 'reject':
                from .orchestration.request_decisions import reject
                reject(store, iid, subject['approval']['request_id'],
                       expected_digest=record_digest(subject['approval']), actor_id='tui')
                return 'request rejected'
            if kind == 'review-budget-approval':
                from .orchestration.review_budget import approve
            else:
                from .orchestration.actions import approve_salvage as approve
            approve(store, iid, subject['approval']['request_id'], actor_id='tui')
            return 'one review retry approved' if kind == 'review-budget-approval' else 'salvage request approved'
        action = {'activation': 'activate-initiative', 'operator-decision': 'resume'}.get(kind)
        if action is None:
            raise StoreError('action kind has no operator handler')
        record = submit_action(store, iid, build_action_document(head, action, {}, actor_id='tui'))
        outcome = action_outcome(record)
        return f"{choice}: {record['state']} ({outcome.get('reason') or outcome.get('status') or 'inspect action evidence'})"
