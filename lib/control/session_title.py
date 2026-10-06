"""The terminal title attention count (#102 phase 2), as Claude's agent view does.

Off inside tmux unless ``set-titles`` is on for the dashboard's own tmux
session (the effective value, so a session override beats the global one): there
a title escape renames the pane, which is only visible (and only worth doing)
when tmux forwards pane titles to the outer terminal. A pane that grouped
sessions or a linked window put in several sessions has no one policy, so it
gets no title. The terminal must take a title escape inside tmux as outside
it. Always off in a Control-managed session, whose pane title is Control's own
evidence, and when ``ASHA_CONTROL_TITLE=0``.
"""
import unicodedata

# Terminals whose title escape (OSC 2) is known; terminfo rarely declares it.
_KNOWN = ('xterm', 'rxvt', 'alacritty', 'foot', 'kitty', 'wezterm', 'vte', 'gnome', 'konsole', 'st-',
          'tmux', 'screen', 'ghostty', 'contour', 'iterm')
_OFF = frozenset({'', 'dumb', 'linux', 'cons25', 'emacs'})
PUSH, POP = b'\x1b[22;0t', b'\x1b[23;0t'   # XTWINOPS save/restore title
OSC_TITLE = (b'\x1b]2;', b'\x07')


def title_supported(env, *, tmux, tigetstr):
    """Whether to show the count. ``tmux(argv)`` reads a tmux value; ``tigetstr`` is terminfo."""
    term = env.get('TERM', '')
    if env.get('ASHA_CONTROL_TITLE') == '0' or env.get('ASHA_HUB_SESSION_ID') or term in _OFF:
        return False
    if not (term.startswith(_KNOWN) or _osc(tigetstr('tsl'))):
        return False
    if env.get('TMUX'):
        session = _own_session(env, tmux)
        # -A: the value in effect for that session, inherited from the global one when unset.
        return session is not None and tmux(['show-options', '-Av', '-t', session, 'set-titles']) == 'on'
    return True


def _own_session(env, tmux):
    """The one tmux session holding the dashboard's pane, or None.

    A pane ID does not name a session: grouped sessions and linked windows put
    one pane in several, and nothing says which of them the operator sees the
    dashboard through (Q15-F1). Only a pane in exactly one session has a policy.
    """
    pane = env.get('TMUX_PANE')
    listing = tmux(['list-panes', '-a', '-F', '#{pane_id} #{session_id}']) if pane else None
    sessions = {line.partition(' ')[2] for line in (listing or '').splitlines() if line.partition(' ')[0] == pane}
    sessions.discard('')
    return sessions.pop() if len(sessions) == 1 else None


def _osc(value):
    # Only an OSC status line is a window title; a real status line takes a column parameter.
    return bool(value) and value.startswith(b'\x1b]')


def title_text(counts):
    waiting = (counts or {}).get('input', 0)
    return f'{waiting} awaiting input · asha control' if waiting else 'asha control'


def _clean(text):
    return ''.join(c for c in str(text) if c.isprintable() and unicodedata.category(c) not in {'Cf', 'Cs'})


class TitleWriter:
    """Writes the title on change only; ``close`` restores the prior title.

    ``restore`` is the previous title when it could be read (tmux pane title);
    otherwise the xterm title stack is pushed on first write and popped on close.
    A write error disables the writer rather than disturbing the dashboard.
    """

    def __init__(self, stream, *, enabled, restore=None, sequence=OSC_TITLE):
        self.stream, self.enabled, self.restore, self.sequence = stream, enabled, restore, sequence
        self.last, self.pushed = None, False

    def _write(self, data):
        try:
            self.stream.write(data)
            self.stream.flush()
        except (OSError, ValueError):
            self.enabled = False

    def _title(self, text):
        start, end = self.sequence
        return start + _clean(text).encode('utf-8', 'replace') + end

    def set(self, text):
        if not self.enabled or text == self.last:
            return
        if self.restore is None and not self.pushed:
            self.pushed = True
            self._write(PUSH)
        self.last = text
        if self.enabled:
            self._write(self._title(text))

    def close(self):
        if not self.enabled or self.last is None:
            return
        self._write(self._title(self.restore) if self.restore is not None else POP)


def open_writer(env, *, stream, tmux, tigetstr):
    """A writer configured for this terminal, disabled where the policy says off."""
    if not title_supported(env, tmux=tmux, tigetstr=tigetstr):
        return TitleWriter(stream, enabled=False)
    restore = None
    if env.get('TMUX') and env.get('TMUX_PANE'):
        restore = tmux(['display-message', '-p', '-t', env['TMUX_PANE'], '#{pane_title}'])
    start, end = tigetstr('tsl'), tigetstr('fsl')
    sequence = (start, end) if _osc(start) and end else OSC_TITLE
    return TitleWriter(stream, enabled=True, restore=restore, sequence=sequence)
