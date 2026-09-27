"""Read-only next steps from observed process state and retained receipts."""
from datetime import datetime, timezone


def memory_label(row):
    stamp = row.get('memory_saved_at')
    if stamp is None:
        return ''
    return 'Memory saved ' + datetime.fromtimestamp(stamp, timezone.utc).strftime('%H:%M UTC')


def saved_label(row):
    """``saved HH:MM UTC`` from the hub's save evidence (#105); empty without one."""
    stamp = row.get('memory_saved_at')
    return '' if stamp is None else 'saved ' + datetime.fromtimestamp(stamp, timezone.utc).strftime('%H:%M UTC')


def finished_label(row):
    """``Finished, saved HH:MM UTC`` or ``Finished, unsaved`` (D3): finished is never gated on a save."""
    saved = saved_label(row)
    return 'Finished, ' + (saved if saved else 'unsaved')


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
    # An open Room is an ongoing conversation: a save or finished report never ends it (#105).
    from .session_closure import TERMINAL_STATES
    room_open = (row.get('profile') == 'room' and group == 'current' and row.get('lifecycle') == 'open'
                 and record.get('state', 'completed') in TERMINAL_STATES)
    changes = {}
    if room_open and saved_label(row):
        changes['saved_label'] = saved_label(row)
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
        hint = 'Close needs attach'
    elif activity == 'close-failed' or record.get('state') in {'unanswered', 'handoff-failed'}:
        hint = 'Close failed: retry or attach'
    elif activity == 'closing' and record.get('waiting_on_background'):
        hint = 'Closing: background tasks running'
    elif activity == 'closing':
        hint = ('Finalized, closing' if row.get('completion_readiness', {}).get('status') == 'ready'
                else 'Closing: await handoff')
    elif room_open and (activity in {'finished', 'idle'} or (
            row.get('completion_readiness', {}).get('status') == 'ready'
            and activity not in {'working', 'running', 'queued', 'starting'})):
        # #105: a Room's save (or a finished report sent anyway) is a checkpoint.
        saved = saved_label(row)
        hint = saved[0].upper() + saved[1:] + ': waiting for you' if saved else 'Waiting for you'
        if activity == 'finished':
            changes.update(activity='idle', reported_activity='finished')
    elif row.get('completion_readiness', {}).get('status') == 'ready' and not room_open:
        # An open Room's save never offers completion, even while its turn goes on (QA26 Q26-F1).
        hint = 'Finalized: close'
    elif activity == 'working' and row.get('background_tasks'):
        hint = 'Working: background tasks'
    elif finished:
        hint = finished_label(row) if process == 'live' or row.get('transport') == 'structured' \
            else 'Done reported: inspect session'
    elif activity == 'unknown' and row.get('telemetry') == 'hooks-not-reporting':
        hint = 'Hooks not reporting: attach'
    elif activity == 'idle':
        hint = 'Waiting for you' if row.get('profile') == 'room' else 'Stopped mid-task?'
    else:
        hint = {'working': 'Working', 'running': 'Working', 'queued': 'Queued',
                'starting': 'Starting', 'failed': 'Failed: check work',
                'blocked': 'Blocked: inspect', 'uncertain': 'Uncertain: inspect',
                'budget-exhausted': 'Budget exhausted: inspect'}.get(activity, 'Inspect session')
    return dict(row, next_step=hint, group=group, **changes)


def _clock(stamp, pattern='%H:%M UTC'):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime(pattern)


def row_facts(row):
    """Short facts for the line under a dashboard row (#102): receipt, close, background, staleness.

    Only fields the hub already presented are read; nothing here derives eligibility.
    """
    facts = []
    if (row.get('completion_readiness') or {}).get('receipt') in {'current', 'stale'}:
        facts.append(receipt_label(row))
    record = row.get('closure') or {}
    from .session_closure import TERMINAL_STATES
    if record.get('generation') == row.get('generation') and not record.get('stale') \
            and record.get('state') and record['state'] not in TERMINAL_STATES:
        if record.get('requested_at') is not None:
            facts.append('close requested ' + _clock(record['requested_at']))
        facts += ['attempt ' + str(record.get('attempts', 1)), record['state']]
    if row.get('background_tasks'):
        facts.append(f"{row['background_tasks']} background tasks")
    if row.get('stale_since') is not None:
        facts.append('stale since ' + _clock(row['stale_since'], '%H:%M:%S UTC'))
    return facts
