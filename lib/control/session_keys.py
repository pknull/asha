"""The dashboard's key surface (#102): a one-line state-aware footer and a paged key sheet. Pure."""
from .session_hub import attach_refusal

# Footer keys by priority; lower-priority keys drop first on a narrow terminal.
TAIL = '? keys  q quit'
_ANSWERABLE = frozenset({'needs-input', 'waiting-input'})


def attachable(row):
    """Whether Enter can act: the hub's own attach predicate decides (T6, Q14-F5)."""
    return attach_refusal(row) is None


def row_keys(row):
    """The action keys, most important first, for the selected row's state."""
    if row is None:
        return ['n job', 'o Room', 'A history']
    if row.get('kind') == 'section':
        return ['Right unfold', 'g group', '! jump']
    step, group = row.get('next_step', ''), row.get('group', 'current')
    answer = ['a answer'] if row.get('activity') in _ANSWERABLE and row.get('transport') == 'structured' else []
    if group == 'history':
        keys = ['r resume', 'Enter view', 'A history']
    elif group == 'ended':
        keys = ['x close', 'r resume', 'X force-close', 'Enter attach']
    else:
        keys = answer + ['Enter attach', 'm send', 'x close', 's stop']
    if not attachable(row):
        # Never advertise Enter where the hub would refuse it (Q14-F5).
        keys = [key for key in keys if not key.startswith('Enter ')]
    return keys


def footer(row, *, width, peek=False):
    """One state-aware line: the keys that matter for the selected row (#102)."""
    from .session_modals import _cell_width
    keys = (['Esc back'] if peek else []) + row_keys(row)
    while keys and _cell_width('  '.join(keys + [TAIL])) > width:
        keys.pop()
    return '  '.join(keys + [TAIL])


def key_sheet():
    """Every binding, labelled as the footer labels it."""
    entries = [('Up/Down', 'select a session'),
               ('Enter attach', 'open the terminal or structured conversation'),
               ('a answer', 'answer the pending input request'),
               ('m send', 'queue a message for the session'),
               ('x close', 'close, requesting a memory handoff'),
               ('X force-close', 'close without a new handoff'),
               ('s stop', 'stop the session; history is retained'),
               ('r resume', 'resume with a continuation'),
               ('n job', 'start a project job'), ('o Room', 'open a project Room'),
               ('g group', 'group by project or by state'),
               ('Left fold', 'fold the selected group to one line'),
               ('Right unfold', 'unfold the selected group'),
               ('! jump', 'select the next session that needs you'),
               ('Space preview', 'show or hide the selected session detail'),
               ('Esc back', 'leave the full-width preview'),
               ('M input filter', 'show only sessions that need input'),
               ('A history', 'include retained history'),
               ('? keys', 'this sheet'), ('q quit', 'leave; sessions keep running')]
    return ['Keys (any key returns)'] + [f'  {key:<22}{text}' for key, text in entries]


def _sheet_page(height):
    """Entries per page when the sheet needs a heading and a position line."""
    return max(1, height - 2)


def sheet_offset(offset, *, height):
    """Clamp a key-sheet scroll offset for this height; 0 when the sheet fits."""
    entries = len(key_sheet()) - 1
    if entries + 1 <= height:
        return 0
    return max(0, min(offset, entries - _sheet_page(height)))


def sheet_lines(height, offset):
    """The key sheet, paged on a short terminal so every binding stays reachable."""
    sheet = key_sheet()
    if len(sheet) <= height:
        return sheet
    entries = sheet[1:]
    first = sheet_offset(offset, height=height)
    page = entries[first:first + _sheet_page(height)]
    return (['Keys (Up/Down scroll; other keys return)', *page,
             f'  {first + 1}-{first + len(page)} of {len(entries)} · Up/Down for more'])
