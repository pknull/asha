"""Read-only next steps from observed process state and retained receipts."""
from datetime import datetime, timezone


def memory_label(row):
    stamp = row.get('memory_saved_at')
    if stamp is None:
        return ''
    return 'Memory saved ' + datetime.fromtimestamp(stamp, timezone.utc).strftime('%H:%M UTC')


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
            and row.get('native_activity', activity) == 'idle'):
        hint = 'Close needs attach'
    elif activity == 'close-failed' or record.get('state') in {'unanswered', 'handoff-failed'}:
        hint = 'Close failed: retry or attach'
    elif activity == 'closing':
        hint = 'Closing: await handoff'
    elif finished:
        hint = 'Done: close' if process == 'live' or row.get('transport') == 'structured' else 'Done reported: inspect session'
    elif activity == 'idle':
        hint = 'Waiting for you' if row.get('profile') == 'room' else 'Stopped mid-task?'
    else:
        hint = {'working': 'Working', 'running': 'Working', 'queued': 'Queued',
                'starting': 'Starting', 'failed': 'Failed: check work',
                'blocked': 'Blocked: inspect', 'uncertain': 'Uncertain: inspect',
                'budget-exhausted': 'Budget exhausted: inspect'}.get(activity, 'Inspect session')
    return dict(row, next_step=hint, group=group)
