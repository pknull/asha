"""A retained, stably ordered model of the session dashboard (#102). Pure; no curses.

The hub lists sessions by ``updated_at DESC`` and every hook event restamps that
column, so ordering by the hub makes a busy row climb on each tool call. The view
orders by a key that activity cannot change and keeps selection as an identity.
Every function returns a new model; none mutates its input.
"""
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from .session_presentation import present, row_facts

GROUP_RANK = {'current': 0, 'ended': 1, 'history': 2}
ATTENTION = frozenset({'needs-input', 'waiting-input'})
WORKING = frozenset({'working', 'running', 'queued', 'starting', 'closing'})
GROUPINGS = ('project', 'state')
# Phase 2 sections (#102). State sections follow the presented next step.
STATE_SECTIONS = (('state:needs', 'Needs you'), ('state:working', 'Working'), ('state:closing', 'Closing'),
                  ('state:ready', 'Ready to close'), ('state:idle', 'Idle'))
TAIL_SECTIONS = (('ended', 'Ended'), ('history', 'History'))
_SECTION_RANK = {key: rank for rank, (key, _) in enumerate(STATE_SECTIONS + TAIL_SECTIONS)}
_NEEDS_STEPS = ('Answer', 'Uncertain', 'Failed', 'Budget exhausted')
_READY_STEPS = ('Done reported', 'Finished')
# A folded section is one selectable heading in ``order``; session ids are never prefixed so.
TOKEN = 'section:'
# A section's finished rows fold to one `… N more` row (§5.6) under this key prefix.
FINISHED = 'finished:'
# §5.6: a short list folds these whole sections first, in this order, then finished rows.
TAIL_FOLDS = ('history', 'ended')

_EMPTY = MappingProxyType({})


@dataclass(frozen=True)
class ViewModel:
    rows: MappingProxyType = _EMPTY      # session_id -> presented row
    order: tuple = ()                    # visible ids in display order
    selected_id: object = None
    anchor: int = 0                      # selected row's screen line within the list, headings included
    grouping: str = 'project'
    input_only: bool = False
    stale: MappingProxyType = _EMPTY     # session_id -> when it was last observed
    seen: MappingProxyType = field(default=_EMPTY)  # session_id -> observation time of the retained row
    excluded: MappingProxyType = _EMPTY  # session_id -> when an action proved it left the active query
    folded: frozenset = frozenset()      # fold keys the operator folded
    unfolded: frozenset = frozenset()    # fold keys the operator opened; automatic folding leaves them
    auto: frozenset = frozenset()        # fold keys the height policy applied (§5.6); see ``fit``


def attention_rank(row):
    """0 when the row needs the operator; changes only with its attention class."""
    return 0 if row.get('activity') in ATTENTION else 1


def _room_open(row):
    """An open Room is an ongoing conversation; it is never ready to close (#105, QA26 Q26-F1)."""
    return row.get('profile') == 'room' and row.get('lifecycle') == 'open' and row.get('group') == 'current'


def state_section(row):
    """The state section for a presented current row; the next step decides it."""
    step = row.get('next_step', '')
    if attention_rank(row) == 0 or step.startswith(_NEEDS_STEPS):
        return 'state:needs'
    if step.startswith('Closing'):
        return 'state:closing'
    if step.startswith(_READY_STEPS) and not _room_open(row):
        return 'state:ready'
    if row.get('activity') in WORKING or step.startswith(('Working', 'Queued', 'Starting', 'Hooks not reporting')):
        return 'state:working'
    return 'state:idle'


def section_of(row, grouping='project'):
    """(key, title) of the section a presented row is listed under."""
    group = row.get('group')
    for key, title in TAIL_SECTIONS:
        if group == key:
            return key, title
    if grouping == 'state':
        key = state_section(row)
        return key, dict(STATE_SECTIONS)[key]
    # Case-equivalent names are one section, as they sort together (Q14-F4).
    name = str(row.get('project_name') or '')
    return 'project:' + name.casefold(), name or '(no project)'


def _titles(rows, grouping):
    """One title per section whatever its members' spelling: the least exact name, so it is stable."""
    titles = {}
    for row in rows:
        key, title = section_of(row, grouping)
        titles[key] = min(titles.get(key, title), title)
    return titles


def sort_key(row, grouping='project'):
    created = row.get('created_at')
    project = str(row.get('project_name') or '').casefold()
    tail = (created is None, created if isinstance(created, (int, float)) else 0, str(row.get('session_id')))
    if grouping == 'state':
        return (_SECTION_RANK[section_of(row, 'state')[0]], project) + tail
    return (GROUP_RANK.get(row.get('group'), len(GROUP_RANK)), project, attention_rank(row)) + tail


def order(rows, grouping='project'):
    """Rows sorted by the stable key; ``updated_at`` never participates."""
    presented = [row if 'group' in row and 'next_step' in row else present(row) for row in rows]
    return sorted(presented, key=lambda row: sort_key(row, grouping))


def select_after_merge(previous_order, new_order, selected_id):
    """Keep the selected identity; if it left, take its nearest surviving neighbour.

    Nearest is by distance in the previous order; at equal distance the
    following row wins, as it would for a single removal.
    """
    if not new_order:
        return None
    if selected_id in new_order:
        return selected_id
    survivors = set(new_order)
    if selected_id in previous_order:
        at = previous_order.index(selected_id)
        for distance in range(1, len(previous_order)):
            for candidate in (at + distance, at - distance):
                if 0 <= candidate < len(previous_order) and previous_order[candidate] in survivors:
                    return previous_order[candidate]
    return new_order[0]


def _listed(rows, model):
    """Rows passing the input filter, in display order, folded or not."""
    chosen = [row for row in rows.values() if not model.input_only or row.get('activity') in ATTENTION]
    return order(chosen, model.grouping)


def finished(row):
    """A current row that is done and only waits to be closed: the first rows §5.6 compacts."""
    return row.get('group') == 'current' and state_section(row) == 'state:ready'


def _parent(key):
    return key[len(FINISHED):] if key.startswith(FINISHED) else key


def in_unit(row, key, grouping):
    """Whether fold key ``key`` (a section, or a section's finished rows) holds the row."""
    return section_of(row, grouping)[0] == _parent(key) and (not key.startswith(FINISHED) or finished(row))


def _unit(row, grouping, folds):
    """The fold key hiding this row, or None when it is shown."""
    key = section_of(row, grouping)[0]
    if key in folds:
        return key
    return FINISHED + key if FINISHED + key in folds and finished(row) else None


def _visible(rows, model):
    visible, seen, folds = [], set(), model.folded | model.auto
    for row in _listed(rows, model):
        unit = _unit(row, model.grouping, folds)
        if unit is None:
            visible.append(row['session_id'])
        elif unit not in seen:
            seen.add(unit)
            visible.append(TOKEN + unit)
    return tuple(visible)


def _rebuild(model, **changes):
    """Recompute visible order and selection; the anchor is kept deliberately.

    An automatic fold never hides the selected row: one that would, say after
    the row finished or ended into it, is released (Q15-F2); ``fit`` then
    leaves it open. Only the operator's own folds hide a selected row.
    """
    updated = replace(model, **changes)
    chosen = updated.rows.get(updated.selected_id)
    if chosen is not None and updated.auto:
        updated = replace(updated, auto=frozenset(key for key in updated.auto
                                                  if not in_unit(chosen, key, updated.grouping)))
    visible = _visible(updated.rows, updated)
    # An empty view (say, a filter nothing matches) keeps the identity to return to.
    selected = select_after_merge(model.order, visible, updated.selected_id) if visible else updated.selected_id
    return replace(updated, order=visible, selected_id=selected)


def with_changes(model, **changes):
    if changes.get('grouping', model.grouping) not in GROUPINGS:
        raise ValueError('unknown grouping')
    return _rebuild(model, **changes)


def merge(model, rows, *, observed_at, complete):
    """Merge one page observed from ``observed_at``.

    An incomplete page cannot prove a row is gone, so unseen rows stay and are
    marked stale. A row refreshed after the page started is newer evidence and
    is kept whether or not the page lists it, and a row an action proved out
    of the active query after the page started stays out.
    """
    excluded = {sid: at for sid, at in model.excluded.items() if at > observed_at}
    fresh = {row['session_id']: present(row) for row in rows if row['session_id'] not in excluded}
    newer = {sid for sid, at in model.seen.items() if at > observed_at and sid in model.rows}
    retained, seen, stale = {}, {}, {}
    for sid, row in model.rows.items():
        if sid in newer or (not complete and sid not in fresh):
            retained[sid], seen[sid] = row, model.seen.get(sid, observed_at)
    for sid, row in fresh.items():
        if sid not in newer:
            retained[sid], seen[sid] = row, observed_at
    if not complete:
        # Stale since the row was last observed, not since each later miss.
        stale = {sid: model.stale.get(sid, seen[sid]) for sid in retained
                 if sid not in fresh and sid not in newer}
    return _rebuild(model, rows=MappingProxyType(retained), stale=MappingProxyType(stale),
                    seen=MappingProxyType(seen), excluded=MappingProxyType(excluded))


def merge_row(model, row, *, observed_at, member=True):
    """Replace one row after an action on it, without re-reading the page.

    ``member`` is the active query's verdict on the refreshed row. A row that
    left the query is removed and remembered, so a page already in flight
    cannot put it back.
    """
    sid = row['session_id']
    others = lambda mapping: {k: v for k, v in mapping.items() if k != sid}
    rows, seen, excluded = others(model.rows), others(model.seen), others(model.excluded)
    if member:
        rows[sid], seen[sid] = present(row), observed_at
    else:
        excluded[sid] = observed_at
    return _rebuild(model, rows=MappingProxyType(rows), stale=MappingProxyType(others(model.stale)),
                    seen=MappingProxyType(seen), excluded=MappingProxyType(excluded))


def merge_changed(model, rows, *, observed_at, member):
    """Merge rows re-read after a change was seen (#102 phase 4).

    A row already refreshed after this read began, or proved out of the
    query since, is newer evidence and is kept. ``member`` is the active
    query's verdict on each row.
    """
    for row in rows:
        sid = row['session_id']
        if model.seen.get(sid, float('-inf')) > observed_at or model.excluded.get(sid, float('-inf')) > observed_at:
            continue
        model = merge_row(model, row, observed_at=observed_at, member=member(row))
    return model


def mark_stale(model, ids):
    """Keep rows whose read failed, marked stale since they were last observed."""
    marked = {sid: model.stale.get(sid, model.seen.get(sid)) for sid in ids if sid in model.rows}
    if not marked or all(sid in model.stale for sid in marked):
        return model
    return replace(model, stale=MappingProxyType({**model.stale, **marked}))


def selected_index(model):
    return model.order.index(model.selected_id) if model.selected_id in model.order else 0


def fold(model, *, fold):
    """Fold (or unfold) the selected row's section; the heading keeps the selection."""
    if not model.order:
        return model
    selected = model.selected_id if model.selected_id in model.order else model.order[0]
    key = selected[len(TOKEN):] if selected.startswith(TOKEN) else section_of(model.rows[selected], model.grouping)[0]
    if fold:
        # Left folds the whole section, from a row or from its `… N more` line.
        key = _parent(key)
        return _rebuild(replace(model, selected_id=TOKEN + key), folded=model.folded | {key},
                        unfolded=model.unfolded - {key, FINISHED + key}, auto=model.auto - {key})
    return _open(model, key)


def _open(model, key):
    """Unfold ``key`` as the operator's choice, selecting the first row it hid."""
    opened = _rebuild(model, folded=model.folded - {key}, auto=model.auto - {key},
                      unfolded=model.unfolded | {key})
    first = next((sid for sid in opened.order
                  if sid in opened.rows and in_unit(opened.rows[sid], key, opened.grouping)), None)
    return replace(opened, selected_id=first or opened.selected_id)


def _is_attention(row):
    return attention_rank(row) == 0


def attention_counts(model):
    rows = [row for row in model.rows.values() if row.get('group') != 'history']
    return {'input': sum(row.get('activity') in ATTENTION for row in rows)}


def jump_attention(model):
    """Select the next row that needs the operator, unfolding its section (`!`)."""
    listed = [row for row in _listed(model.rows, model) if _is_attention(row)]
    if not listed:
        return model
    ids = [row['session_id'] for row in _listed(model.rows, model)]
    here = model.selected_id
    if here in ids:
        position = ids.index(here)
    elif here in model.order:  # a folded heading: start from the first row it hides
        key = here[len(TOKEN):]
        position = next((i for i, sid in enumerate(ids)
                         if in_unit(model.rows[sid], key, model.grouping)), -1) - 1
    else:
        position = -1
    target = next((row for row in listed if ids.index(row['session_id']) > position), listed[0])
    key = section_of(target, model.grouping)[0]
    return _rebuild(replace(model, selected_id=target['session_id']), folded=model.folded - {key},
                    auto=model.auto - {key}, unfolded=model.unfolded | {key})


def list_lines(model):
    """Screen lines the whole list takes: headings, rows and fact sub-lines."""
    rows = display_rows(model)
    return sum(_heading(rows, i, 0) + row_height(rows[i]) for i in range(len(rows)))


def _candidates(model):
    """Automatic fold keys in §5.6's order: History, Ended, then finished rows from the bottom up.

    Never a unit holding an attention row, the selected row, or one the
    operator folded or opened; working and attention rows are never folded.
    """
    listed = _listed(model.rows, model)
    selected = model.rows.get(model.selected_id)
    keys = [key for key in TAIL_FOLDS
            if any(section_of(row, model.grouping)[0] == key for row in listed)
            and not any(_is_attention(row) for row in listed if section_of(row, model.grouping)[0] == key)]
    sections = []
    for row in listed:
        key = section_of(row, model.grouping)[0]
        if finished(row) and FINISHED + key not in sections:
            sections.append(FINISHED + key)
    # Opening a whole section opens its finished rows too (Q15-F3).
    return [key for key in keys + sections[::-1]
            if key not in model.folded | model.unfolded and _parent(key) not in model.folded | model.unfolded
            and not (selected and in_unit(selected, key, model.grouping))]


def _fold_savings(rows):
    """Lines the unfolded list takes, and the lines each automatic fold key would save.

    Units are disjoint (finished rows are current, never Ended or History), so
    savings add up: a whole section becomes its one heading line, and a
    section's finished rows become one `… N more` line under the kept heading.
    """
    total = sum(_heading(rows, i, 0) + row_height(rows[i]) for i in range(len(rows)))
    saving = {}
    for shown in rows:
        if shown.get('kind') == 'section':
            continue
        key = shown['section']
        if key in TAIL_FOLDS:
            saving[key] = saving.get(key, 0) + row_height(shown)
        elif finished(shown):
            # The first finished row's height pays for the `… N more` line.
            saving[FINISHED + key] = saving.get(FINISHED + key, -1) + row_height(shown)
    return total, saving


def fit(model, space):
    """Apply the §5.6 height policy for a list of ``space`` lines.

    Automatic folds are recomputed from none on every call, so a taller screen
    opens them again; explicit folds and unfolds always win. Returns ``model``
    itself when nothing changes.
    """
    rows = display_rows(_rebuild(replace(model, auto=frozenset())))
    total, saving = _fold_savings(rows)
    auto = []
    for key in _candidates(model):
        if total <= space:
            break
        # A fold that saves no line (a lone one-line finished row) only hides the row (Q16-F1).
        if saving.get(key, 0) > 0:
            auto.append(key)
            total -= saving[key]
    auto = frozenset(auto)
    if auto == model.auto:
        return model
    selected = model.selected_id
    if isinstance(selected, str) and selected.startswith(TOKEN) and selected not in model.rows:
        key = selected[len(TOKEN):]
        if key not in model.folded | auto:
            # Its automatic fold opened: select the first row it hid, as an unfold does.
            selected = next((row['session_id'] for row in _listed(model.rows, model)
                             if in_unit(row, key, model.grouping)), selected)
    return _rebuild(replace(model, selected_id=selected), auto=auto)


def _section(row):
    return row.get('section') or section_of(row)[0]


def _heading(rows, index, start):
    """Whether the renderer puts a section heading above row ``index``; a folded heading is its own row."""
    if rows[index].get('kind') == 'section' and not rows[index].get('more'):
        return False
    return index == start or _section(rows[index - 1]) != _section(rows[index])


heading_above = _heading


def row_height(row):
    """Lines a row takes: one, plus one for its fact sub-line."""
    return 1 if row.get('kind') == 'section' else 1 + bool(row_facts(row))


def line_offset(rows, start, index):
    """Screen lines from the first list line to row ``index`` when row ``start`` is shown first."""
    return sum(_heading(rows, i, start) + row_height(rows[i]) for i in range(start, index)) \
        + _heading(rows, index, start)


def viewport_start(rows, index, anchor, space):
    """The first row to show so row ``index`` sits on list line ``anchor``.

    With too few rows above it the row sits as near the anchor as they allow;
    it always stays inside ``space`` lines, sub-line included. Headings count
    as lines, so a heading appearing or vanishing above the row does not move
    it (#102).
    """
    limit = min(max(0, anchor), max(0, space - row_height(rows[index])))
    start = index
    # Offsets only grow as the start moves up; walk up while the row still fits.
    while start > 0 and line_offset(rows, start - 1, index) <= limit:
        start -= 1
    return start


def move(model, delta, *, visible):
    """Move the selection; the anchor follows its screen line inside the viewport."""
    if not model.order:
        return model
    rows = display_rows(model)
    old = selected_index(model)
    new = max(0, min(len(model.order) - 1, old + delta))
    start = min(viewport_start(rows, old, model.anchor, visible), new)
    anchor = min(line_offset(rows, start, new), max(0, visible - row_height(rows[new])))
    return replace(model, selected_id=model.order[new], anchor=anchor)


def _unit_counts(model, listed):
    """(members, attention members) per fold key that folds them, from one pass over the list."""
    counts = {}
    for row in listed:
        key = section_of(row, model.grouping)[0]
        for unit in (key, FINISHED + key) if finished(row) else (key,):
            members, attention = counts.get(unit, (0, 0))
            counts[unit] = (members + 1, attention + _is_attention(row))
    return counts


def _heading_row(model, token, counts, titles):
    key = token[len(TOKEN):]
    members, attention = counts.get(key, (0, 0))
    # The section's one title, not its hidden members' own (Q15-F4).
    title = titles.get(_parent(key), key)
    # ``more``: a section's finished rows as one `… N more` line under the section heading.
    return dict(kind='section', session_id=token, section=_parent(key), section_title=title,
                count=members, attention=attention, more=key.startswith(FINISHED))


def display_rows(model):
    """Rows in display order with their section; stale rows carry ``stale_since``.

    A folded section appears as one ``kind='section'`` row naming its count.
    """
    counts = _unit_counts(model, _listed(model.rows, model)) if model.folded or model.auto else {}
    titles = _titles(model.rows.values(), model.grouping)
    shown = []
    for sid in model.order:
        if sid.startswith(TOKEN) and sid not in model.rows:
            shown.append(_heading_row(model, sid, counts, titles))
            continue
        row = model.rows[sid]
        key = section_of(row, model.grouping)[0]
        extra = dict(section=key, section_title=titles[key])
        if sid in model.stale:
            extra['stale_since'] = model.stale[sid]
        shown.append(dict(row, **extra))
    return shown
