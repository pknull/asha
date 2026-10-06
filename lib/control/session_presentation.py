"""Read-only next steps from observed process state and publication evidence."""
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


def present(row):
    """The Keeper's words for a shown row. Hub rows arrive with both axes already combined:
    ``activity`` as shown and ``reported_activity`` when a finished report stands behind it."""
    activity = row.get('activity', 'unknown')
    process = row.get('process_state', 'unknown')
    record = row.get('closure') or {}
    if record.get('generation') != row.get('generation'):
        record = {}
    finished = activity == 'finished' or row.get('reported_activity') == 'finished'
    ended = process == 'ended' or (process != 'live' and activity in {'exited', 'stopped', 'closed'})
    group = 'history' if row.get('lifecycle') == 'closed' else 'ended' if ended else 'current'
    # An open Room is an ongoing conversation: a save or finished report never ends it (#105).
    room_open = row.get('profile') == 'room' and group == 'current' and row.get('lifecycle') == 'open'
    changes = {}
    if room_open and saved_label(row):
        changes['saved_label'] = saved_label(row)
    if group == 'history':
        hint = 'Closed: view history'
    elif ended:
        hint = 'Done: close record' if finished else 'Ended unreported: check work'
    elif activity in {'needs-input', 'waiting-input'}:
        hint = 'Answer in Control' if row.get('transport') == 'structured' else 'Answer in terminal (attach)'
    elif activity == 'closing':
        saved = row.get('memory_saved_at')
        hint = ('Closing: saved' if record.get('deadline') is not None and saved is not None
                and saved >= record.get('requested_at', float('inf')) else 'Closing: waiting for a save')
    elif room_open and activity in {'finished', 'idle'}:
        # #105: a Room's save (or a finished report sent anyway) is a checkpoint.
        saved = saved_label(row)
        hint = saved[0].upper() + saved[1:] + ': waiting for you' if saved else 'Waiting for you'
        if activity == 'finished':
            changes.update(activity='idle', reported_activity='finished')
    elif activity == 'working' and row.get('background_tasks'):
        hint = 'Working: background tasks'
    elif activity == 'working' and finished:
        hint = 'Working: reported finished'
    elif activity == 'finished':
        hint = finished_label(row) if process == 'live' or row.get('transport') == 'structured' \
            else 'Done reported: inspect session'
    elif activity == 'unknown' and row.get('observed') == 'launched':
        hint = 'Hooks not reporting: attach'
    elif activity == 'idle':
        hint = 'Waiting for you' if row.get('profile') == 'room' else 'Stopped mid-task?'
    else:
        hint = {'working': 'Working', 'running': 'Working', 'queued': 'Queued',
                'starting': 'Starting', 'failed': 'Failed: check work',
                'uncertain': 'Uncertain: inspect',
                'budget-exhausted': 'Budget exhausted: inspect'}.get(activity, 'Inspect session')
    return dict(row, next_step=hint, group=group, **changes)


def _clock(stamp, pattern='%H:%M UTC'):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime(pattern)


def row_facts(row):
    """Short facts for the line under a dashboard row (#102): close, background, staleness.

    Only fields the hub already presented are read; nothing here derives eligibility.
    """
    facts = []
    record = row.get('closure') or {}
    if (record.get('generation') == row.get('generation') and not record.get('stale')
            and row.get('lifecycle') == 'closing' and record.get('deadline') is not None):
        facts += ['close requested ' + _clock(record['requested_at']), 'closes by ' + _clock(record['deadline'])]
    if row.get('background_tasks'):
        facts.append(f"{row['background_tasks']} background tasks")
    if row.get('stale_since') is not None:
        facts.append('stale since ' + _clock(row['stale_since'], '%H:%M:%S UTC'))
    return facts
