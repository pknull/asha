"""Completion authority in the existing hub record, never a semantic memory store.

Lock order is action (when needed), observation, Memory publication, then DB.
No database writer spans process termination. Receipts are controller-produced;
neither a worker's JSON nor a native successful result can install one.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import session_closure as closure
from .registry_guards import mutation_guard
from .store import StoreError

CONTRACT = 'asha.session-completion.v1'


class CompletionPending(StoreError):
    """Valid evidence awaiting its matching native boundary, not stale work."""


def _pending_work(c, row):
    if row['transport'] != 'terminal':
        return
    record = row.get('closure') or {}
    delivery = record.get('delivery') or {}
    for message in c.execute("SELECT message_id,delivery_key FROM hub_messages WHERE session_id=? AND state='queued'",
                             (row['session_id'],)):
        if (not record or message['message_id'] != delivery.get('message_id')
                or message['delivery_key'] != closure.message_key(record)):
            raise StoreError('queued project work remains; read and acknowledge messages before finalizing')


WORKER_INSTRUCTION = (
    'Project-memory contract: before work, use the project-memory skill to read this project\'s '
    'Memory v2 through the existing reader and verify relevant claims against live sources. '
    'Do not load chair context or private recovery/transcript stores. Before explicit completion, '
    'publish authorized durable findings or attest no durable update as a standalone shell tool with `asha control session handoff '
    '--outcome no-durable-update --detail "WHY" --json`. A successful save supplies a completion receipt. '
    'Finish all other tools first; after handoff, only report completion and end the turn. '
    'New work invalidates readiness. `asha control session report --state finished --text "RESULT"` '
    'requires that receipt. If the skill is unavailable, use `asha control session handoff --read --json` '
    'and the named Memory v2 reader/validator; if memory, scope or native permissions block publication, '
    'report `handoff --outcome blocked --detail "REASON"` and needs-input, never claim completion. '
    'This workflow does not authorize Git commit/push, private-memory publication or permission bypass.'
)


# #105: a Room is an ongoing conversation; its saves are checkpoints, never completion.
ROOM_INSTRUCTION = (
    'Project-memory contract (Room): this Room is an ongoing conversation, not a one-task assignment. '
    'Before work, use the project-memory skill to read this project\'s Memory v2 through the existing reader '
    'and verify relevant claims against live sources. Do not load chair context or private recovery/transcript stores. '
    'When the Keeper asks for a save or durable project knowledge settles, publish it as a standalone shell tool '
    '(`asha control session handoff --read --json` prints the digests, then `asha control session handoff '
    '--active-file ACTIVE --decisions-file DECISIONS --expected-active DIGEST --expected-decisions DIGEST --json`). '
    'A save is a checkpoint: Control shows the Room as saved and it stays open. After a save, keep the '
    'conversation going; do not report completion and do not end the Room. The Room ends only through the close '
    'request Control delivers when the Keeper closes it; follow that request\'s own instructions. '
    'If memory, scope or native permissions block publication, say so and report '
    '`asha control session report --state needs-input --text "REASON"`. '
    'This workflow does not authorize Git commit/push, private-memory publication or permission bypass.'
)


def instruction(profile):
    """The project-memory contract appended to a terminal session's assignment."""
    return ROOM_INSTRUCTION if profile == 'room' else WORKER_INSTRUCTION


def binding(row):
    return {key: row.get(key) for key in ('session_id', 'generation', 'project_id',
                                         'assignment_epoch', 'work_epoch')}


def invalidate(hub, actor, reason):
    with hub._observation_lock(actor['session_id']):
        row = hub.get(actor['session_id'])
        if binding(row) != binding(actor):
            raise StoreError('stale completion actor')
        hub._update(row['session_id'], expected_generation=row['generation'],
                    completion=dict(status='blocked', detail=reason))


def _turn(c, row):
    if row['transport'] != 'structured':
        return None
    turn = c.execute('SELECT turn_id,generation,state FROM session_turns WHERE session_id=? '
                     'ORDER BY started_at DESC,rowid DESC LIMIT 1', (row['session_id'],)).fetchone()
    return dict(turn) if turn else None


def _scope(row):
    import save_scope
    root = closure.secure_project_root(Path(row['project']))
    plane, errors = save_scope.resolve_effective_plane('none', start=root)
    if not plane or Path(plane['plane_base']) != root:
        raise StoreError('project-memory scope unavailable: ' + (errors[0]['message'] if errors else 'different plane'))
    config = closure.memory_v2.require_v2_config(root)
    if config['project_id'] != row['project_id']:
        raise StoreError('project-memory identity changed')
    closure.memory_v2._assert_persistence_enabled(root)
    return root


@contextmanager
def snapshot(row):
    """Keep the same publication state through receipt issuance/consumption."""
    root = _scope(row)
    with closure.memory_v2._publication_lock(root):
        _scope(row)
        value = closure.memory_v2._read_published_snapshot_unlocked(root)
        yield closure.memory_v2.snapshot_digests(value)


def issue(hub, actor, *, outcome, detail, publication=None, request=None):
    """Called only by the explicit publisher or verified handoff actor."""
    sid = actor['session_id']
    with hub._observation_lock(sid):
        row = hub.get(sid)
        if binding(row) != binding(actor) or row['lifecycle'] not in {'starting', 'open', 'closing'}:
            raise StoreError('work changed during handoff; read and finalize the current turn')
        with snapshot(row) as digests, mutation_guard(hub.config), hub.database() as db, db.transaction(write=True) as c:
            current = json.loads(c.execute('SELECT payload FROM hub_sessions WHERE session_id=?', (sid,)).fetchone()[0])
            if binding(current) != binding(actor):
                raise StoreError('work changed during handoff')
            turn = _turn(c, row)
            if row['transport'] == 'structured' and (not turn or turn['state'] != 'running'
                    or turn['turn_id'] != hub.env.get('ASHA_MANAGED_TURN_ID')):
                raise StoreError('completion requires the current running structured turn')
            if publication and publication['after'] != digests:
                raise StoreError('Memory publication was superseded; re-read and finalize again')
            _pending_work(c, current)
            receipt = dict(contract=CONTRACT, receipt_id=str(uuid.uuid4()), status='ready',
                           **binding(row), turn=turn and {k: turn[k] for k in ('turn_id', 'generation')},
                           outcome=outcome, detail=detail, digests=digests,
                           publication_id=publication and publication['publication_id'],
                           request=request, finalized_at=time.time(), destination=str(Path(row['project']) / 'Memory'))
            current['completion'] = receipt
            hub._save(c, current)
            return receipt


def check(hub, row, *, digests=None, idle=False, connection=None):
    receipt = row.get('completion') or {}
    if receipt.get('status') != 'ready' or receipt.get('contract') != CONTRACT:
        raise StoreError('verified project-memory handoff required; run session handoff before reporting finished')
    unbound = _unbound_reason(row, receipt)
    if unbound:
        raise StoreError('stale project-memory handoff: ' + unbound)
    if digests is None:
        root = _scope(row)
        current = closure.memory_v2.snapshot_digests(closure.memory_v2.read_published_snapshot(root))
        return check(hub, row, digests=current, idle=idle, connection=connection)
    if receipt.get('digests') != digests:
        raise StoreError('stale project-memory handoff: Memory changed; re-read and finalize again')
    if connection is None:
        with hub.database() as db, db.transaction() as c:
            return check(hub, row, digests=digests, idle=idle, connection=c)
    c = connection
    _pending_work(c, row)
    turn = _turn(c, row)
    if row['transport'] == 'structured':
        expected = receipt.get('turn')
        if not turn or not expected or any(turn[k] != v for k, v in expected.items()):
            raise StoreError('stale project-memory handoff: structured turn changed')
        state = c.execute('SELECT state,stop_requested,generation FROM managed_sessions WHERE session_id=?',
                          (row['session_id'],)).fetchone()
        if not state or state['generation'] != expected['generation']:
            raise StoreError('stale project-memory handoff: structured owner generation changed')
        queued = c.execute("SELECT 1 FROM session_messages WHERE session_id=? AND state='queued' LIMIT 1",
                           (row['session_id'],)).fetchone()
        if queued or turn['state'] not in ({'completed'} if idle else {'running', 'completed'}):
            raise StoreError('project-memory handoff has pending or incomplete structured work')
        if idle and (state['state'] != 'idle' or state['stop_requested']):
            raise StoreError('structured session has not reached a safe idle boundary')
    if row['transport'] == 'terminal' and idle:
        if row.get('background_tasks') and row.get('native_activity') == 'working':
            # #99: the turn ended with the agent's own background work still running.
            raise CompletionPending(f"handoff finalized; waiting on {row['background_tasks']} background task(s) to end")
        # Copilot/OpenCode currently have no native Control activity bridge.
        stamp = row.get('native_observed_at')
        if (row['harness'] not in {'claude', 'codex'} or row.get('native_activity') not in {'idle', 'exited'}
                or stamp is None or stamp < receipt['finalized_at'] or row.get('activity') == 'needs-input'):
            raise CompletionPending('handoff finalized; native idle boundary unobserved, needs attach')
    return receipt


def _ready(receipt):
    return receipt.get('status') == 'ready' and receipt.get('contract') == CONTRACT


def _unbound_reason(row, receipt):
    """Why a ready receipt no longer describes this row's work, or None."""
    if any(receipt.get(k) != v for k, v in binding(row).items()):
        return 'further work or a new incarnation began'
    request = receipt.get('request')
    if request and (not row.get('closure') or request != closure.receipt_for(row['closure'])):
        return 'close request or attempt changed'
    return None


def mark_stale(row):
    """Stamp, once per receipt, the first write at which it stopped matching the row's work.

    Runs on every hub row save. Memory-side staleness is not a row write; the
    view dates it from the published files instead.
    """
    receipt = row.get('completion') or {}
    if not _ready(receipt) or (row.get('completion_stale') or {}).get('receipt_id') == receipt.get('receipt_id'):
        return row
    reason = _unbound_reason(row, receipt)
    if reason:
        row['completion_stale'] = dict(receipt_id=receipt.get('receipt_id'), at=time.time(), reason=reason)
    return row


def _memory_changed_at(row, receipt):
    """When the published Memory last changed after this receipt, if that is knowable."""
    try:
        root = closure.secure_project_root(Path(row['project']))
        stamps = [closure.secure_path(root, name).stat().st_mtime
                  for name in ('Memory/activeContext.md', 'Memory/decisions.md', 'Work/markers/silence')
                  if closure.secure_path(root, name).exists()]
    except (OSError, ValueError):
        return None
    later = [stamp for stamp in stamps if stamp >= receipt.get('finalized_at', float('inf'))]
    return max(later) if later else None


def view(hub, row):
    """Readiness plus the operator-facing receipt state: current, stale (since when) or none."""
    receipt = row.get('completion') or {}
    facts = dict(finalized_at=receipt.get('finalized_at') if _ready(receipt) else None, stale_since=None)
    try:
        found = check(hub, row)
        return dict(facts, status='ready', receipt='current', receipt_id=found['receipt_id'])
    except CompletionPending as exc:
        # Valid and bound to this work; only its idle boundary is still due.
        return dict(facts, status='stale', receipt='current', receipt_id=receipt.get('receipt_id'),
                    reason=str(exc))
    except (OSError, ValueError) as exc:
        if not _ready(receipt):
            return dict(facts, status='missing', receipt='none', reason=str(exc))
        stale = row.get('completion_stale') or {}
        since = stale.get('at') if stale.get('receipt_id') == receipt.get('receipt_id') else None
        if since is None and 'Memory' in str(exc):
            since = _memory_changed_at(row, receipt)
        return dict(facts, status='stale', receipt='stale', receipt_id=receipt.get('receipt_id'),
                    stale_since=since, reason=str(exc))


def close_finalized(hub, row, *, ended=False):
    """Return a closed row only after revalidation at the owned stop boundary."""
    if not row.get('completion'):
        return None
    with hub._observation_lock(row['session_id']):
        current = hub.get(row['session_id'])
        stopping = False
        try:
            with snapshot(current) as digests:
                with hub.database() as db, db.transaction(write=True) as c:
                    current = json.loads(c.execute('SELECT payload FROM hub_sessions WHERE session_id=?',
                                                   (row['session_id'],)).fetchone()[0])
                    receipt = check(hub, current, digests=digests,
                                    idle=current['transport'] == 'structured' or not ended, connection=c)
                    if current['transport'] == 'structured':
                        # Fence enqueue/claim_turn before releasing the writer. The
                        # ordinary stop path handles provider cleanup afterwards.
                        c.execute('UPDATE managed_sessions SET stop_requested=1 WHERE session_id=?',
                                  (current['session_id'],))
                record = current.get('closure')
                if not record or record['generation'] != current['generation']:
                    record = closure.new_closure(current)
                record = closure.transition(record, 'completed', attachment_required=False,
                    delivery=dict(channel='completion-receipt', detail='Finalized, closing',
                                  message_id=None, delivered_at=time.time()),
                    completion_receipt=receipt['receipt_id'],
                    memory=dict(record['memory'], saved=receipt['outcome'] == 'published'),
                    handoff=dict(outcome=receipt['outcome'], detail=receipt['detail'], verified=True,
                                 generation=current['generation'], acknowledged_at=receipt['finalized_at'],
                                 destination=receipt['destination'], digests=receipt['digests']))
                stopping = True
                # A graceful close never kills a terminal a person is attached to
                # (QA7): tmux refuses the kill itself; the receipt stays for a retry.
                return hub._stop(current, close=True, closure_record=record, detached_only=True)
        except (OSError, ValueError) as exc:
            if stopping:
                raise
            # No replay or invented publication. Retain the reason for attach/retry.
            hub._update(current['session_id'], completion_error=str(exc)[:1000])
    return None
