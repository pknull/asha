"""Dashboard regions and renders (#102 phase 2). Pure; no curses.

``render`` returns one list of spans per screen line. A span is
``(x, text, role, tier, selected)``: ``x`` and every width are terminal cells,
never characters, so wide and combining project names clip to the screen.
``plain`` joins the spans into text for tests and golden files.
"""
import time
from collections import Counter, namedtuple

from . import session_view
from .session_preview import sanitize
from .session_keys import footer, sheet_lines
from .session_presentation import memory_label, present, row_facts
from .session_selection import label as selection_label
from .session_usage import line as usage_line, tokens_label
from .tui_style import BAD, GOOD, INERT, MACHINE, WAITING, tier_for

# List plus side panel from a 120-column terminal: the painter keeps the last
# column free, so the drawable width it lays out is one less.
WIDE = 119
TINY = 8            # below this height only the enlarge hint shows
HEADER = 2          # title line, then summary or attention banner
NARROW_DETAIL = 3   # blank, detail and metadata lines under a narrow list
MIN_LIST = 2        # a heading and the selected row: the detail yields lines to keep them (Q14-F2)

Layout = namedtuple('Layout', 'mode height width list_top list_height list_width side_x side_width '
                              'detail_top error_y message_y footer_y')

# Unicode glyph, ASCII fallback. The meaning is also in the next-step words.
GLYPHS = {'working': ('●', '*'), 'input': ('▲', '!'), 'done': ('✓', '+'),
          'idle': ('○', 'o'), 'failed': ('✗', 'x'), 'ended': ('·', '.'), 'unknown': ('?', '?'),
          'closing': ('…', '~'), 'open': ('▼', '-'), 'folded': ('▸', '='), 'separator': ('│', '|'),
          'ellipsis': ('…', '~'), 'dot': ('·', '-'), 'dash': ('—', '-'), 'rule': ('─', '-')}
_INPUT = frozenset({'needs-input', 'waiting-input'})
_FAILED = frozenset({'failed', 'uncertain', 'budget-exhausted'})


def layout(height, width, *, errors=False, peek=False, preview=True):
    """The screen regions for this size (§5): narrow list, wide list plus panel, or a full-width peek."""
    error_y = height - 3 if errors else None
    content = max(0, height - 2 - bool(errors) - HEADER)
    common = dict(height=height, width=width, list_top=HEADER, error_y=error_y,
                  message_y=height - 2, footer_y=height - 1)
    if height < TINY:
        return Layout('tiny', list_height=0, list_width=width, side_x=None, side_width=0, detail_top=None,
                      **dict(common, list_top=0, error_y=None))
    if width >= WIDE and preview:
        list_width = max(56, min(width - 50, width // 2))
        return Layout('wide', list_height=content, list_width=list_width, side_x=list_width + 2,
                      side_width=width - list_width - 2, detail_top=None, **common)
    if peek:
        return Layout('peek', list_height=content, list_width=width, side_x=0, side_width=width,
                      detail_top=None, **common)
    list_height = max(content - NARROW_DETAIL, min(content, MIN_LIST))
    return Layout('narrow', list_height=list_height, list_width=width, side_x=None, side_width=0,
                  detail_top=HEADER + list_height, **common)


def glyph_kind(row):
    """The state glyph for a presented row."""
    activity, step, group = row.get('activity'), row.get('next_step', ''), row.get('group')
    if group == 'history':
        return 'ended'
    if group == 'ended':
        return 'done' if step.startswith('Done') else 'ended'
    if activity in _INPUT:
        return 'input'
    if activity == 'closing' or step.startswith('Closing'):
        return 'closing'
    if step.startswith(('Done', 'Finished')):
        return 'done'
    if activity in session_view.WORKING:
        return 'working'
    if activity == 'idle':
        return 'idle'
    return 'failed' if activity in _FAILED else 'unknown'


def activity_tier(activity):
    # Session activity names differ from the advanced workflow state names.
    return {'working': MACHINE, 'queued': MACHINE, 'waiting-input': WAITING, 'finished': GOOD,
            'uncertain': BAD, 'budget-exhausted': BAD}.get(activity, tier_for(activity))


def age(stamp, now):
    """Time since the row last changed, for display only; it never orders rows."""
    if not isinstance(stamp, (int, float)) or now is None:
        return ''
    seconds = max(0, int(now - stamp))
    for size, unit in ((86400, 'd'), (3600, 'h'), (60, 'm')):
        if seconds >= size:
            return f'{seconds // size}{unit}'
    return f'{seconds}s'


def _g(name, ascii_only):
    return GLYPHS[name][1 if ascii_only else 0]


def cells(text):
    from .tui import _cell_width
    return _cell_width(text)


def fit(text, budget, ascii_only=False):
    """Safe text clipped to ``budget`` cells, with an ellipsis when clipped."""
    from .tui import _prefix_cells, _safe_text
    text = _safe_text(text)
    if budget <= 0:
        return ''
    if cells(text) <= budget:
        return text
    return _prefix_cells(text, budget - 1) + _g('ellipsis', ascii_only)


def pad(text, budget, ascii_only=False):
    text = fit(text, budget, ascii_only)
    return text + ' ' * max(0, budget - cells(text))


def _spans(parts, x=0, selected=False):
    """Place (text, role, tier) parts side by side from cell ``x``."""
    placed = []
    for text, role, tier in parts:
        if text:
            placed.append((x, text, role, tier, selected))
            x += cells(text)
    return placed


TOKENS_WIDTH = 5   # session_usage.compact: at most five cells


def _columns(width, grouping, tokens=False):
    """Cell widths of the name, harness, next-step, tokens and age columns.

    The tokens column (#111) exists only while some listed session has known
    usage, so a list with none keeps every other column's width.
    """
    harness = 7 if width >= 60 else 0
    stamp = 4 if width >= 30 else 0
    used = TOKENS_WIDTH if tokens and width >= 60 else 0
    rest = max(0, width - 4 - (harness + 2 if harness else 0) - (stamp + 1 if stamp else 0) - (used + 1 if used else 0))
    name = max(6, min(32 if grouping == 'state' else 24, int(rest * 0.42)))
    return name, harness, max(0, rest - name - 2), used, stamp


def _row_spans(row, *, selected, width, grouping, ascii_only, now, tokens=False):
    name_w, harness_w, step_w, tokens_w, age_w = _columns(width, grouping, tokens)
    tier = activity_tier(row.get('activity'))
    name = row.get('name', '')
    if not str(row.get('section', '')).startswith('project:') \
            or (row.get('project_name') and row.get('project_name') != row.get('section_title')):
        # A section that mixes projects (or spellings of one) names the project on the row.
        name = f"{row.get('project_name') or ''}/{name}"
    parts = [(('>' if selected else ' ') + ' ', 'row', None),
             (_g(glyph_kind(row), ascii_only), 'status', tier),
             (' ' + pad(name, name_w, ascii_only) + '  ', 'row', None)]
    if harness_w:
        parts.append((pad(row.get('harness', ''), harness_w, ascii_only) + '  ', 'row', None))
    parts.append((pad(row.get('next_step', ''), step_w, ascii_only), 'status', tier))
    if tokens_w:
        used = fit(tokens_label(row.get('usage')), tokens_w, ascii_only)
        parts.append((' ' + ' ' * (tokens_w - cells(used)) + used, 'muted', INERT))
    if age_w:
        stamp = fit(age(row.get('updated_at'), now), age_w, ascii_only)
        parts.append((' ' + ' ' * (age_w - cells(stamp)) + stamp, 'row', None))
    spans = _spans(parts, selected=selected)
    return _clip_spans(spans, width)


def _clip_spans(spans, width):
    """Drop or shorten spans that would pass ``width`` cells (very narrow screens)."""
    kept = []
    for x, text, role, tier, selected in spans:
        if x >= width:
            break
        kept.append((x, fit(text, width - x) if x + cells(text) > width else text, role, tier, selected))
    return kept


def _heading_spans(row, count, width, ascii_only):
    label = f"{_g('open', ascii_only)} {row.get('section_title', '')}"
    number = str(count)
    if width < cells(number) + 4:
        return _spans([(fit(label, width, ascii_only), 'section', None)])
    return _spans([(pad(label, width - cells(number) - 1, ascii_only) + ' ' + number, 'section', None)])


def _folded_spans(row, *, selected, width, ascii_only):
    extra = f", {row['attention']} need you" if row.get('attention') else ''
    text = (f"{'>' if selected else ' '} {_g('folded', ascii_only)} {row.get('section_title', '')} "
            f"({row.get('count', 0)}{extra})")
    return _spans([(pad(text, width, ascii_only), 'section', WAITING if row.get('attention') else None)],
                  selected=selected)


def _more_spans(row, *, selected, width, ascii_only):
    """A section's finished rows folded to one line (§5.6)."""
    text = f"{'>' if selected else ' '} {_g('ellipsis', ascii_only)} {row.get('count', 0)} more"
    return _spans([(pad(text, width, ascii_only), 'muted', INERT)], selected=selected)


def _facts_spans(row, width, ascii_only):
    facts = row_facts(row)
    if not facts:
        return None
    return _spans([(fit('    ' + f" {_g('dot', ascii_only)} ".join(facts), width, ascii_only), 'muted', INERT)])


def render_list(rows, *, selected, anchor, space, width, grouping, ascii_only, now):
    """The list region: section headings, rows and fact sub-lines.

    Returns (lines, shown) where ``shown`` holds the ids actually on screen.
    """
    if not rows or space <= 0:
        return [], set()
    counts = Counter()
    for row in rows:
        if row.get('kind') != 'section' or row.get('more'):
            counts[row.get('section')] += row.get('count', 1) if row.get('more') else 1
    tokens = any(tokens_label(row.get('usage')) for row in rows if row.get('kind') != 'section')
    start = session_view.viewport_start(rows, selected, space - 1 if anchor is None else anchor, space)
    lines, shown = [], set()
    for i in range(start, len(rows)):
        row = rows[i]
        heading = session_view.heading_above(rows, i, start)
        if i == selected:
            # The selected row itself is never evicted (Q14-F2): viewport_start
            # fits it whole when the space allows; otherwise its heading, then
            # its fact sub-line, give way first.
            heading = heading and len(lines) + 2 <= space
        elif len(lines) + heading + (1 if i > selected else session_view.row_height(row)) > space:
            # A row below the selected one may lose its fact sub-line to the bottom edge.
            break
        if heading:
            lines.append(_heading_spans(row, counts[row.get('section')], width, ascii_only))
        if row.get('more'):
            lines.append(_more_spans(row, selected=i == selected, width=width, ascii_only=ascii_only))
        elif row.get('kind') == 'section':
            lines.append(_folded_spans(row, selected=i == selected, width=width, ascii_only=ascii_only))
        else:
            lines.append(_row_spans(row, selected=i == selected, width=width, grouping=grouping,
                                    ascii_only=ascii_only, now=now, tokens=tokens))
            facts = _facts_spans(row, width, ascii_only)
            if facts and len(lines) < space:
                lines.append(facts)
        shown.add(row['session_id'])
    return lines, shown


def _meta(row):
    capture = (row.get('closure') or {}).get('capture') or row.get('capture') or {}
    experience = (f"capture:{capture.get('status', 'disabled')} review:{row.get('experience_review', 'none')}"
                  if capture else '')
    usage = usage_line(row.get('usage')) if tokens_label(row.get('usage')) else ''
    return [part for part in (f"{row.get('pending_messages', 0)} queued messages", experience,
                              selection_label(row), usage) if part]


def _folded_label(row):
    return f"{row.get('count', 0)} {'finished ' if row.get('more') else ''}sessions folded"


def detail_lines(row, ascii_only=False):
    """The three narrow lines under the list: a gap, what happened, and identifiers."""
    if row is None:
        return []
    if row.get('kind') == 'section':
        return [('', 'muted', INERT), (f"{row.get('section_title')}: {_folded_label(row)}; Right unfolds",
                                       'detail', None), ('', 'muted', INERT)]
    saved = memory_label(row)
    dot = f" {_g('dot', ascii_only)} "
    return [('', 'muted', INERT),
            ((saved + dot if saved else '') + row.get('reason', ''), 'detail', activity_tier(row.get('activity'))),
            (dot.join([str(row.get('session_id'))] + _meta(row)), 'muted', INERT)]


def panel_lines(row, width, ascii_only=False):
    """The side panel (wide) or full-width peek: the whole identity, state and facts."""
    from .tui import _cell_lines, _safe_text
    if row is None:
        return [('No session selected', 'muted', INERT)]
    if row.get('kind') == 'section':
        return [(f"{row.get('section_title')} {_g('dot', ascii_only)} {_folded_label(row)}",
                 'heading', None), ('Right unfolds the group', 'muted', INERT)]
    dot = f" {_g('dot', ascii_only)} "
    tier = activity_tier(row.get('activity'))
    lines = [(f"{row.get('project_name') or ''} / {row.get('name', '')}"
              + dot + dot.join(str(row.get(k)) for k in ('harness', 'profile') if row.get(k)), 'heading', None),
             (f"{_g(glyph_kind(row), ascii_only)} {row.get('next_step', '')}", 'status', tier)]
    saved = memory_label(row)
    for piece in _cell_lines(_safe_text((saved + dot if saved else '') + row.get('reason', '')), width):
        lines.append((piece, 'detail', tier))
    lines.append(('', 'muted', INERT))
    for fact in ['session ' + str(row.get('session_id'))] + _meta(row) + row_facts(row):
        lines.append((fact, 'muted', INERT))
    return lines


PREVIEW_MIN = 6     # capture lines kept below the panel before the panel yields its facts


def preview_lines(preview, width, ascii_only=False, back=0):
    """The read-only capture under the panel: a marker, the newest lines, then the capture time (§5.1).

    ``back`` is how many lines the capture is scrolled back from its newest (PgUp, #106).
    """
    source = 'events' if preview is not None and preview.source == 'events' else 'pane'
    label = f' {source}, read-only'
    marker = (_g('rule', ascii_only) * max(0, width - cells(label))) + label
    body = [('Capturing…', 'muted', INERT)] if preview is None else \
        [(preview.note, 'muted', INERT)] if preview.note else \
        [(sanitize(line), 'detail', None) for line in preview.lines] or [('(empty screen)', 'muted', INERT)]
    stamp = [] if preview is None or preview.captured_at is None else \
        [('captured ' + time.strftime('%H:%M:%S', time.localtime(preview.captured_at))
          + (f' · {back} lines back, PgDn returns' if back else ''), 'muted', INERT)]
    return [(marker, 'muted', INERT)], body, stamp


def compose_panel(head, preview, width, limit, ascii_only=False, back=0):
    """Panel facts over the capture; the facts yield lines first, the capture keeps the newest."""
    marker, body, stamp = preview_lines(preview, width, ascii_only, back)
    head = head[:max(min(len(head), 3), limit - PREVIEW_MIN - len(marker) - len(stamp))]
    room = max(0, limit - len(head) - len(marker) - len(stamp))
    body = body[-room:] if room else []
    gap = [('', 'muted', INERT)] * max(0, room - len(body))
    return head + marker + body + gap + stamp


def _prepare(rows, grouping):
    """Present raw rows and give each a section, as the model's display rows have."""
    prepared = []
    for row in rows:
        if row.get('kind') != 'section':
            row = row if 'next_step' in row and 'group' in row else present(row)
            if 'section' not in row:
                key, title = session_view.section_of(row, grouping)
                row = dict(row, section=key, section_title=title)
        prepared.append(row)
    return prepared


def _banner(rows, shown, counts, ascii_only):
    """The attention banner when a row that needs the operator is off screen or folded (§5.2)."""
    hidden = any((row.get('kind') == 'section' and row.get('attention'))
                 or (row.get('kind') != 'section' and session_view.attention_rank(row) == 0
                     and row['session_id'] not in shown) for row in rows)
    if not hidden:
        return None
    parts = []
    if counts.get('input'):
        parts.append(f"{_g('input', ascii_only)} {counts['input']} needs input")
    return '  '.join(parts or ['sessions need you']) + f" {_g('dash', ascii_only)} press ! to jump"


def _header(data, width, ascii_only):
    title = f"ASHA CONTROL {_g('dot', ascii_only)} Sessions"
    mode = 'by ' + data.get('grouping', 'project')
    if width < cells(title) + cells(mode) + 2:
        return _spans([(fit(title, width, ascii_only), 'heading', None)])
    return _spans([(pad(title, width - cells(mode) - 1, ascii_only) + ' ', 'heading', None),
                   (mode, 'muted', INERT)])


def _counts(data, rows):
    if 'attention' in data:
        return data['attention']
    live = [row for row in rows if row.get('kind') != 'section' and row.get('group') != 'history']
    return {'input': sum(row.get('activity') in _INPUT for row in live)}


def _put(screen, y, spans):
    if 0 <= y < len(screen):
        screen[y] = screen[y] + spans


def _text_block(screen, top, x, lines, width, limit, ascii_only):
    for offset, (text, role, tier) in enumerate(lines[:max(0, limit)]):
        _put(screen, top + offset, _spans([(fit(text, width, ascii_only), role, tier)], x=x))


def _tiny(height, width):
    labels = ['ASHA CONTROL', 'Enlarge terminal', 'q quit'][:height]
    return [_spans([(fit(text, width), 'heading' if i == 0 else 'muted', None)]) for i, text in enumerate(labels)]


def _sheet(data, width, height, offset):
    lines = sheet_lines(height, offset, preview=bool(data.get('session_preview')))[:height]
    return [_spans([(fit(line, width), 'heading' if i == 0 else 'muted', None if i == 0 else INERT)])
            for i, line in enumerate(lines)]


def render(data, *, selected=0, anchor=None, width=100, height=30, message='', keys=False, sheet=0,
           peek=False, preview=True):
    """Every screen line as spans for this snapshot, selection and size."""
    if height < TINY:
        return _tiny(height, width)
    if keys:
        return _sheet(data, width, height, sheet)
    ascii_only, grouping = bool(data.get('ascii')), data.get('grouping', 'project')
    rows = _prepare(data.get('rows', []), grouping)
    selected = min(selected, len(rows) - 1) if rows else 0
    current = rows[selected] if rows else None
    errors = data.get('errors', [])
    box = layout(height, width, errors=bool(errors), peek=peek, preview=preview)
    screen = [[] for _ in range(height)]
    shown = set()
    def panel(side_width):
        head = panel_lines(current, side_width, ascii_only)
        if 'preview' not in data or current is None or current.get('kind') == 'section':
            return head
        return compose_panel(head, data['preview'], side_width, box.list_height, ascii_only,
                             data.get('preview_back', 0))
    if box.mode == 'peek':
        _text_block(screen, box.list_top, 0, panel(width), width, box.list_height, ascii_only)
    else:
        listed, shown = render_list(rows, selected=selected, anchor=anchor, space=box.list_height,
                                    width=box.list_width, grouping=grouping, ascii_only=ascii_only,
                                    now=data.get('now'))
        for offset, spans in enumerate(listed):
            _put(screen, box.list_top + offset, spans)
    if box.mode == 'wide':
        for y in range(box.list_top, box.list_top + box.list_height):
            _put(screen, y, _spans([(_g('separator', ascii_only), 'muted', INERT)], x=box.list_width))
        _text_block(screen, box.list_top, box.side_x, panel(box.side_width), box.side_width, box.list_height,
                    ascii_only)
    elif box.mode == 'narrow':
        # The list may borrow detail lines on a short screen; the blank gap goes first.
        room = (box.error_y or box.message_y) - box.detail_top
        detail = detail_lines(current, ascii_only)
        _text_block(screen, box.detail_top, 0, detail[1:] if room < len(detail) else detail, width, room,
                    ascii_only)
    screen[0] = _header(data, width, ascii_only)
    banner = _banner(rows, shown, _counts(data, rows), ascii_only) if box.mode != 'peek' else None
    summary = data.get('summary', 'Reading sessions…')
    urgent = any(row.get('activity') in _INPUT for row in rows)
    screen[1] = _spans([(fit(banner or summary, width, ascii_only), 'banner' if banner else 'summary',
                         WAITING if banner or urgent else None)])
    if errors:
        screen[box.error_y] = _spans([(fit(errors[0], width, ascii_only), 'error', BAD)])
    screen[box.message_y] = _spans([(fit(message, width, ascii_only), 'message', None)])
    screen[box.footer_y] = _spans([(fit(footer(current, width=width, peek=box.mode == 'peek'), width),
                                    'muted', INERT)])
    return screen


def plain(screen):
    """Join each line's spans into text, padding gaps by cells."""
    text = []
    for spans in screen:
        line, at = '', 0
        for x, part, *_ in sorted(spans, key=lambda span: span[0]):
            line += ' ' * max(0, x - at) + part
            at = max(at, x) + cells(part)
        text.append(line)
    return text
