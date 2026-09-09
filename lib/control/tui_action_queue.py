"""Global action browsing and explicit decisions, independent of tree sampling."""
from __future__ import annotations

import json

from .orchestration.config import load_config
from .orchestration.current_actions import inspection_store, page


def _inspect(stdscr, curses_module, row, *, next_action=None):
    from . import tui

    content = json.dumps(row, ensure_ascii=True, indent=2)
    offset, previous = 0, None
    while True:
        height, width = stdscr.getmaxyx()
        budget, available = max(1, width - 1), max(1, height - 3)
        lines = [line[start:start + budget] for line in content.splitlines()
                 for start in range(0, max(1, len(line)), budget)]
        offset = min(offset, max(0, len(lines) - available))
        body = lines[offset:offset + available]
        rows = [tui._clip("Recorded action: inspect current binding before deciding", budget), *body,
                tui._clip(f"{offset + 1}-{min(len(lines), offset + available)}/{len(lines)} Up/Down PgUp/PgDn", budget),
                tui._clip(("r " + next_action + "; Esc back") if next_action else
                          "Enter/Esc back; browsing does not approve", budget)]
        roles = ("selected", *("content" for _ in body), "inactive", "selected")
        frame = tui.ModalFrame(tuple(rows[:height]), None, offset, offset + available, roles[:height])
        if frame != previous:
            tui._draw_modal_frame(stdscr, curses_module, frame)
            previous = frame
        key = tui._read_modal_key(stdscr, curses_module)
        if key in {'r', ord('r')} and next_action:
            return 'review'
        if key in {27, 10, 13, "\n", "\r", getattr(curses_module, "KEY_ENTER", 343)}:
            return
        if key == getattr(curses_module, "KEY_DOWN", 258):
            offset += 1
        elif key == getattr(curses_module, "KEY_UP", 259):
            offset = max(0, offset - 1)
        elif key == getattr(curses_module, "KEY_NPAGE", 338):
            offset += available
        elif key == getattr(curses_module, "KEY_PPAGE", 339):
            offset = max(0, offset - available)
        elif key == getattr(curses_module, "KEY_HOME", 262):
            offset = 0
        elif key == getattr(curses_module, "KEY_END", 360):
            offset = len(lines)


def _resolve(stdscr, curses_module, model, env, store, candidate):
    from . import tui
    from .store import StoreError
    from .tui_action_resolution import prepare, resolve

    attempted = False
    try:
        reviewed = prepare(store, candidate)
        choices = reviewed['choices']
        if _inspect(stdscr, curses_module, reviewed,
                    next_action='choose decision' if choices else None) != 'review':
            return 'action review closed'
        decision = tui._prompt_line(
            stdscr, curses_module, model, 'Decision: ', maximum=8,
            title='Confirm exact action', context=(
                f"Initiative: {reviewed['initiative']['slug']}\n"
                f"Reviewed digest: {reviewed['review_digest']}\n"
                'Type the decision exactly: ' + ', '.join(choices) + '. Esc cancels.'),
        )
        if decision not in choices:
            return 'action cancelled'
        reason = None
        if decision == 'reject' and reviewed['kind'] == 'plan-approval':
            reason = tui._prompt_line(stdscr, curses_module, model, 'Reason: ',
                                      title='Plan rejection', maximum=200)
            if not reason:
                return 'plan rejection cancelled'
        attempted = True
        result = resolve(store, candidate, reviewed['review_digest'], decision,
                         env=env, tmux=tui._coordinator_tmux(), reason=reason)
        try:
            tui._refresh_initiatives(model, env)
        except (StoreError, ValueError, OSError) as exc:
            return result + '; display refresh unavailable: ' + tui._safe_error(exc)
        return result
    except (StoreError, ValueError, OSError) as exc:
        prefix = ('decision not confirmed; inspect current records before retrying: '
                  if attempted else 'action unavailable: ')
        return prefix + tui._safe_error(exc)


def inspect_actions(stdscr, curses_module, model, env):
    """Page each family explicitly; a partial or timed-out page is never empty proof."""
    from . import tui

    store = inspection_store(load_config(env))
    if store is None:
        return "Global action pages require the active SQLite registry backend; ! filters the retained tree"
    family, after, unavailable = "initiatives", None, 0
    notice = ''
    snapshot = None
    while True:
        if snapshot is None:
            snapshot = page(store, family=family, limit=50, after=after)
            unavailable += snapshot["unavailable_records"]
        selected_rows = {str(index): row for index, row in enumerate(snapshot["rows"], 1)}
        candidates = [tui.ModalCandidate(key, tui._safe_text(
            f"{row['slug']}: {row['kind']} ({row['disposition']})"), display=row["initiative_id"][:8])
            for key, row in selected_rows.items()]
        if snapshot["next"] is not None and not snapshot["retry"]:
            candidates.append(tui.ModalCandidate("next", "Next page"))
        if snapshot["retry"]:
            candidates.append(tui.ModalCandidate("retry", "Retry this page"))
        candidates.extend((tui.ModalCandidate("refresh", "Refresh from the beginning"),
                           tui.ModalCandidate("family", "Switch to " + ("approvals" if family == "initiatives" else "initiatives"))))
        status = "end of this family" if snapshot["complete"] else "partial page; more work may be unread"
        context = (f"{family}: {len(selected_rows)} recorded candidates; {status}. "
                   f"{unavailable} unreadable records encountered. Runtime {snapshot['admission']['mode']}.\n"
                   "Oldest updated first within each lifecycle state. Pages can change between reads.\n"
                   "Inspecting a candidate grants no authority; its resolution command rechecks the binding.")
        if notice:
            context += '\n' + notice
        choice = tui._prompt_line(stdscr, curses_module, model, "Inspect: ", maximum=16,
                                  title="Global initiative actions", context=context, candidates=candidates)
        if choice is None:
            return notice or "action browser closed"
        if choice in selected_rows:
            if _inspect(stdscr, curses_module, selected_rows[choice],
                        next_action='review exact action') == 'review':
                notice = _resolve(stdscr, curses_module, model, env, store, selected_rows[choice])
                snapshot = None
        elif choice == "next" and snapshot["next"] is not None and not snapshot["retry"]:
            after, snapshot = snapshot["next"], None
            notice = ''
        elif choice == "retry" and snapshot["retry"]:
            snapshot = None
        elif choice in {"refresh", "family"}:
            if choice == "family":
                family = "approvals" if family == "initiatives" else "initiatives"
            after, snapshot, unavailable = None, None, 0
            notice = ''
