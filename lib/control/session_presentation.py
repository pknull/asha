"""Read-only next steps from observed process state and retained receipts."""
from datetime import datetime, timezone


def memory_label(row):
    stamp = row.get('memory_saved_at')
    if stamp is None:
        return ''
    return 'Memory saved ' + datetime.fromtimestamp(stamp, timezone.utc).strftime('%H:%M UTC')


def receipt_label(row):
    """The completion-receipt state: whether a close would need a turn at all."""
    readiness = row.get('completion_readiness')
    if not readiness:
        return ''
    state = readiness.get('receipt')
    if state == 'current':
        return 'receipt current'
    if state == 'stale':
        since = readiness.get('stale_since')
        return 'receipt stale' + (' since ' + datetime.fromtimestamp(since, timezone.utc).strftime('%H:%M UTC')
                                  if since is not None else '')
    return 'no receipt'


def present(row):
    activity = row.get('activity', 'unknown')
    process = row.get('process_state', 'unknown')
    record = row.get('closure') or {}
    if record.get('generation') != row.get('generation'):
        record = {}
    report = row.get('completion_report')
    finished = (report.get('generation') == row.get('generation') and
                report.get('assignment_epoch') == row.get('assignment_epoch')) if report else activity == 'finished'
    ended = process == 'ended' or (process != 'live' and activity in {'exited', 'stopped', 'closed'})
    group = 'history' if row.get('lifecycle') == 'closed' and not record.get('needs_attention') else 'ended' if ended else 'current'
    if group == 'history':
        hint = 'Closed: view history'
    elif ended:
        hint = 'Done: close record' if finished else 'Ended unreported: check work'
        if record.get('needs_attention'):
            hint = 'Close failed: inspect record'
    elif activity in {'needs-input', 'permission-requested', 'waiting-input'}:
        hint = 'Answer in Control' if row.get('transport') == 'structured' else 'Answer in terminal (attach)'
    elif record.get('attachment_required') or (
            record.get('state') == 'pending-delivery' and row.get('transport') != 'structured'
            and row.get('native_activity', activity) == 'idle'
            and row.get('completion_readiness', {}).get('status') != 'ready'):
        # #101: at a verified native idle boundary the operator may also close
        # without a turn; anywhere else attachment is the only graceful path.
        # A client or pane mode that refused typing refuses that stop as well.
        # The hub computes ``no_handoff`` with the command's own predicate.
        hint = ('Close: attach or --no-handoff' if (row.get('no_handoff') or {}).get('eligible')
                and record.get('input_refusal') not in {'attached', 'mode'}
                else 'Close needs attach')
    elif activity == 'close-failed' or record.get('state') in {'unanswered', 'handoff-failed'}:
        hint = 'Close failed: retry or attach'
    elif activity == 'closing' and record.get('waiting_on_background'):
        hint = 'Closing: background tasks running'
    elif activity == 'closing':
        hint = ('Finalized, closing' if row.get('completion_readiness', {}).get('status') == 'ready'
                else 'Closing: await handoff')
    elif row.get('completion_readiness', {}).get('status') == 'ready':
        hint = 'Finalized: close'
    elif activity == 'working' and row.get('background_tasks'):
        hint = 'Working: background tasks'
    elif finished:
        hint = ('Result ready: needs handoff' if (row.get('transport') == 'structured' or 'completion_readiness' in row)
                and row.get('completion_readiness', {}).get('status') != 'ready'
                else 'Done: close' if process == 'live' else 'Done reported: inspect session')
    elif activity == 'unknown' and row.get('telemetry') == 'hooks-not-reporting':
        hint = 'Hooks not reporting: attach'
    elif activity == 'idle':
        hint = 'Waiting for you' if row.get('profile') == 'room' else 'Stopped mid-task?'
    else:
        hint = {'working': 'Working', 'running': 'Working', 'queued': 'Queued',
                'starting': 'Starting', 'failed': 'Failed: check work',
                'blocked': 'Blocked: inspect', 'uncertain': 'Uncertain: inspect',
                'budget-exhausted': 'Budget exhausted: inspect'}.get(activity, 'Inspect session')
    return dict(row, next_step=hint, group=group)
