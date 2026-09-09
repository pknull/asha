"""Operator rejection of a retained salvage or review-budget request."""
from __future__ import annotations

import copy

from .actions import ActionRefused, append_event, _now
from .model import OPERATOR_SURFACE_ACTOR_IDS, record_digest


def reject(store, iid, request_id, *, expected_digest, actor_id='cli'):
    if actor_id not in OPERATOR_SURFACE_ACTOR_IDS:
        raise ActionRefused('request rejection requires an operator surface')
    if (not isinstance(expected_digest, str) or len(expected_digest) != 64
            or any(c not in '0123456789abcdef' for c in expected_digest)):
        raise ActionRefused('request rejection requires the inspected record digest')
    with store.transaction_lock(iid):
        approval = store.read_approval(iid, request_id)
        if approval['action_class'] not in {'salvage', 'review-budget'}:
            raise ActionRefused('only salvage and review-budget requests use this rejection command')
        if approval['state'] != 'rejected':
            if approval['state'] != 'requested':
                raise ActionRefused('request is no longer pending; rejection cannot revoke an approval')
            if record_digest(approval) != expected_digest:
                raise ActionRefused('request changed; inspect before rejecting')
            changed = copy.deepcopy(approval)
            changed.update(state='rejected', updated_at=_now(),
                           decided_by={'actor_kind': 'operator', 'actor_id': actor_id})
            store.save_approval(iid, changed, expected_digest=record_digest(approval))
            approval = changed
        # A retry can finish the journal edge after a lost response or a crash
        # between the record and event writes. It never re-signs the decision.
        signer = approval.get('decided_by')
        if not signer or signer.get('actor_kind') != 'operator' or signer.get('actor_id') not in OPERATOR_SURFACE_ACTOR_IDS:
            raise ActionRefused('retained rejection has no supported signer; inspect its provenance')
        if not any(e['type'] == 'approval-decided' and request_id in e['subject_ids']
                   and isinstance(e['payload'], dict)
                   and e['payload'].get('decision') == 'rejected'
                   for e in store.list_events_snapshot(iid)):
            append_event(store, iid, 'approval-decided', [request_id],
                         {'action_class': approval['action_class'], 'decision': 'rejected'},
                         actor_kind='operator', actor_id=signer['actor_id'])
        return approval
