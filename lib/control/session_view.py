"""A retained, stably ordered model of the session dashboard (#102). Pure; no curses.

The hub lists sessions by ``updated_at DESC`` and every hook event restamps that
column, so ordering by the hub makes a busy row climb on each tool call. The view
orders by a key that activity cannot change and keeps selection as an identity.
Every function returns a new model; none mutates its input.
"""
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from .session_presentation import present

GROUP_RANK = {'current': 0, 'ended': 1, 'history': 2}
ATTENTION = frozenset({'needs-input', 'permission-requested', 'waiting-input', 'close-failed'})
WORKING = frozenset({'working', 'running', 'queued', 'starting', 'closing'})
GROUPINGS = ('project', 'state')

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


def attention_rank(row):
    """0 when the row needs the operator; changes only with its attention class."""
    return 0 if row.get('activity') in ATTENTION or row.get('next_step', '').startswith('Close failed') else 1


def _state_rank(row):
    return 0 if attention_rank(row) == 0 else 1 if row.get('activity') in WORKING else 2


def sort_key(row, grouping='project'):
    created = row.get('created_at')
    return (GROUP_RANK.get(row.get('group'), len(GROUP_RANK)),
            attention_rank(row) if grouping == 'project' else _state_rank(row),
            str(row.get('project_name') or '').casefold(),
            created is None, created if isinstance(created, (int, float)) else 0,
            str(row.get('session_id')))


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


def _visible(rows, model):
    chosen = [row for row in rows.values() if not model.input_only or row.get('activity') in ATTENTION]
    return tuple(row['session_id'] for row in order(chosen, model.grouping))


def _rebuild(model, **changes):
    """Recompute visible order and selection; the anchor is kept deliberately."""
    updated = replace(model, **changes)
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


def selected_index(model):
    return model.order.index(model.selected_id) if model.selected_id in model.order else 0


def _heading(rows, index, start):
    """Whether the renderer puts a group heading above row ``index``."""
    group = rows[index].get('group')
    return group != 'current' and (index == start or rows[index - 1].get('group') != group)


def line_offset(rows, start, index):
    """Screen lines from the first list line to row ``index`` when row ``start`` is shown first."""
    return sum(1 + _heading(rows, i, start) for i in range(start, index)) + _heading(rows, index, start)


def viewport_start(rows, index, anchor, space):
    """The first row to show so row ``index`` sits on list line ``anchor``.

    With too few rows above it the row sits as near the anchor as they allow;
    it always stays inside ``space`` lines. Headings count as lines, so a
    heading appearing or vanishing above the row does not move it (#102).
    """
    limit = min(max(0, anchor), max(0, space - 1))
    # Offsets only grow as the start moves up, so walk up while the row still
    # fits. ``below`` is the lines from the start row's own line to the target.
    start, below = index, 0
    while start > 0:
        below += 1 + _heading(rows, start, start - 1)
        if below + _heading(rows, start - 1, start - 1) > limit:
            break
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
    anchor = min(line_offset(rows, start, new), max(0, visible - 1))
    return replace(model, selected_id=model.order[new], anchor=anchor)


def display_rows(model):
    """Rows in display order; stale rows carry ``stale_since``."""
    return [dict(model.rows[sid], stale_since=model.stale[sid]) if sid in model.stale else model.rows[sid]
            for sid in model.order]
