"""Row actions for the session dashboard (#102): attach, answer, send, close, stop, resume, launch.

Moved out of the event loop unchanged in behaviour. Every destructive action
still needs a typed ``yes``; nothing here derives eligibility, which comes
from the hub's presented row.
"""
import uuid
from dataclasses import dataclass
from typing import Any

from .session_hub import attach_refusal
from .session_presentation import memory_label

ENTER = (10, 13)
ACTION_KEYS = frozenset(map(ord, 'amxXsr'))
_CLOSE_LABELS = {'x': 'Close (asks for a Memory save, then closes) ', 'X': 'Force-close (no wait) ', 's': 'Stop '}


@dataclass
class Context:
    screen: Any
    curses: Any
    model: Any             # tui.TuiModel, for the shared modal helpers
    config: Any
    env: dict
    hub: Any

    def prompt(self, label, title):
        from . import tui
        height, width = self.screen.getmaxyx()
        self.model.resize(height, width)
        return tui._prompt_line(self.screen, self.curses, self.model, label, title=title, maximum=4000)


def launch_selection(model_text, effort_text):
    """Optional launch fields (#95): a blank answer means the harness default."""
    return {'model': (model_text or '').strip() or None, 'effort': (effort_text or '').strip() or None}


def launch(ctx, key):
    """Open the project launch form for a job (`n`) or a Room (`o`); returns (message, session id)."""
    from . import tui
    room = key == ord('o')
    started = {}

    def submit(*, project, harness, prompt, model, effort):
        created = ctx.hub.launch(project=project, prompt=prompt, harness=harness,
                                 profile='room' if room else 'worker', model=model, effort=effort)
        started['sid'] = created['session_id']
        return 'Started ' + created['name']

    form = dict(title='Open project Room' if room else 'New project job',
                prompt_label='Topic' if room else 'Assignment', launch=submit,
                hint='Model and effort are optional; blank keeps the harness default.')
    message = tui._project_launch_form(ctx.screen, ctx.curses, ctx.model, ctx.config, ctx.env, session=form)
    return message, started.get('sid')


def attach(ctx, row):
    from . import tui
    from .rooms import RoomStore, attach_room
    if row['transport'] == 'structured':
        return tui._managed_session_view(ctx.screen, ctx.curses, ctx.config, row['session_id'])
    target = ctx.hub.attach(row['session_id']) if row['transport'] == 'terminal' else attach_room(
        RoomStore(ctx.config), row['room_id'], tmux=ctx.hub.tmux)
    return tui._popup_room_command(ctx.screen, ctx.curses, ctx.config, ctx.env, ctx.hub.tmux,
                                   target['attach_argv'], target['attach'], target['name']) \
        or 'Session detached; work continues'


def answer(ctx, row):
    from . import tui
    from .session_store import SessionStore
    if row['transport'] != 'structured':
        return attach(ctx, row)
    with SessionStore(ctx.config) as sessions:
        requests = sessions.snapshot(row['session_id'])['requests']
    request = next((r for r in requests if r['state'] == 'pending'), None)
    if request and request['kind'] == 'native-permission':
        return tui._decide_managed_permission(ctx.screen, ctx.curses, ctx.model, ctx.config, ctx.env,
                                              request['request_id'])
    if request:
        tui._execute_intent(tui.TuiIntent(tui.IntentKind.SESSION_QUESTIONS, task_id=request['request_id']),
                            stdscr=ctx.screen, curses_module=ctx.curses, model=ctx.model, config=ctx.config,
                            env=ctx.env, store=None, journals=None, jj=None)
        return ctx.model.message or ''
    return 'No input request is recorded; Enter opens the conversation'


def send(ctx, row):
    from .session_store import SessionStore
    from .sessions import refuse_managed_operator
    body = ctx.prompt('Message: ', 'Send context to ' + row['name'])
    if not body:
        return None
    refuse_managed_operator(ctx.config, ctx.env)
    if row['transport'] == 'terminal':
        ctx.hub.send(row['session_id'], body, key=str(uuid.uuid4()))
        return 'Queued; attach to the session or let its worker read messages. Not delivered yet.'
    if row['transport'] == 'structured':
        if ctx.hub.owns(row['session_id']):
            ctx.hub.send(row['session_id'], body, key=str(uuid.uuid4()))
        else:
            with SessionStore(ctx.config) as sessions:
                sessions.enqueue(row['session_id'], body, key=str(uuid.uuid4()))
        return 'Message retained for the next eligible turn'
    return 'Enter attaches to this legacy Room to provide input directly'


def close_or_stop(ctx, key, row):
    from .rooms import RoomStore, close_room
    from .session_store import SessionStore
    from .sessions import refuse_managed_operator
    letter = chr(key)
    if ctx.prompt('Type yes: ', _CLOSE_LABELS[letter] + row['name']) != 'yes':
        return None
    refuse_managed_operator(ctx.config, ctx.env)
    if ctx.hub.owns(row['session_id']):
        if letter == 'x':
            # D6: record the request and hand the wait to a detached waiter; never block the dashboard.
            hub = ctx.hub
            requested = hub.request_close(row['session_id'], wait=hub._close_wait(False, None))
            if requested['lifecycle'] == 'closing':
                hub.spawn_close_waiter(row['session_id'])
            return (requested.get('closure') or {}).get('guidance') or requested.get('reason') or 'Session closed'
        if letter == 'X':
            closed = ctx.hub.close(row['session_id'], force=True)
            return (closed.get('closure') or {}).get('guidance') or 'Session closed'
        stopped = ctx.hub.stop(row['session_id'])
        return 'Session stopped; history retained; ' + (memory_label(stopped) or 'no memory save claimed')
    if letter == 'x':
        return 'No handoff seam for a legacy Room or managed session; X force-closes, s stops'
    if row['transport'] == 'room':
        close_room(RoomStore(ctx.config), row['room_id'], tmux=ctx.hub.tmux)
        return 'Room closed; history retained; no memory handoff claimed'
    with SessionStore(ctx.config) as sessions:
        sessions.stop(row['session_id'])
    return 'Session stopped; history retained; no memory handoff claimed'


def resume(ctx, row):
    from .session_store import SessionStore
    from .sessions import refuse_managed_operator
    body = ctx.prompt('Continuation: ', 'Resume ' + row['name'])
    if not body:
        return None
    if row['transport'] == 'terminal':
        ctx.hub.resume(row['session_id'], prompt=body)
        return 'Session resumed'
    if row['transport'] != 'structured':
        return 'Open a new Room with continuation context'
    refuse_managed_operator(ctx.config, ctx.env)
    with SessionStore(ctx.config) as sessions:
        state = sessions.get(row['session_id'])
        digest = sessions.recovery_digest(state)
    detail = state.get('recovery') or state['state']
    if ctx.prompt('Type yes: ', 'Resume with new context; retained state: ' + str(detail)) != 'yes':
        return None
    if ctx.hub.owns(row['session_id']):
        ctx.hub.resume(row['session_id'], prompt=body, expected_digest=digest)
    else:
        with SessionStore(ctx.config) as sessions:
            sessions.resume(row['session_id'], prompt=body, expected_digest=digest)
    return 'Continuation queued; prior uncertain input will not be replayed'


def act(ctx, key, row):
    """Run the row action bound to ``key``; returns the status message, or None to keep the old one."""
    enter = key in ENTER or key == getattr(ctx.curses, 'KEY_ENTER', -1)
    if enter:
        # The footer does not offer Enter here; say why instead of failing inside the hub.
        return attach_refusal(row) or attach(ctx, row) or ''
    if key == ord('a'):
        return answer(ctx, row) or ''
    if key == ord('m'):
        return send(ctx, row)
    if key in map(ord, 'xXs'):
        return close_or_stop(ctx, key, row)
    if key == ord('r'):
        return resume(ctx, row)
    return None
