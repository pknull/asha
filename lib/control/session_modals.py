"""Modal helpers the session dashboard shares with the legacy TUI.

Prompts, the project launch form, the session reader, question answering and
the permission review, plus the text and colour helpers they draw with. They
moved out of ``tui`` without behaviour changes (retirement step L-a2) so the
dashboard no longer loads the legacy TUI and the task substrate it imports;
``tui`` imports them back until it retires. Branches only the legacy TUI
reaches still call into ``tui`` lazily.
"""

from __future__ import annotations

import json
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Any, Iterable, Mapping

from .config import ControlConfig
from .harness import HARNESSES, validate_role
from .store import StoreError
from .text import (
    prompt_character_allowed as _shared_prompt_character_allowed,
    terminal_text_is_complete,
)
from .tui_style import (
    BAD, DECORATION_PAIR, DECORATION_XTERM, GOOD, INERT, MACHINE, TIER_PAIR, TIER_XTERM, WAITING,
)


class ModalHost:
    """The screen a modal helper draws over.

    Editors mark the host ``dirty`` when they close, and an overlay prompt asks
    it to repaint what the prompt covers.
    """

    def paint_underlay(self, stdscr, curses_module) -> None:
        raise NotImplementedError


class SessionModel(ModalHost):
    """The session dashboard's modal state, in place of ``tui.TuiModel``.

    ``message`` carries a modal's outcome back to the dashboard. Until the
    legacy TUI retires, an overlay prompt repaints the empty legacy control tree
    beneath it with this model's colour, message and summary, as it did while
    the dashboard held a ``TuiModel``; that is this model's only use of ``tui``.
    """

    def __init__(self) -> None:
        self.coloured = False
        self.message: str | None = None
        self.managed_summary: str | None = None
        self.dirty = True
        self._underlay = None

    def paint_underlay(self, stdscr, curses_module) -> None:
        from . import tui
        if self._underlay is None:
            self._underlay = tui.TuiModel([])
        underlay = self._underlay
        underlay.coloured, underlay.message = self.coloured, self.message
        underlay.managed_summary = self.managed_summary
        underlay.dirty = True
        tui._paint(stdscr, curses_module, underlay)


class _TuiShutdown(Exception):
    """A terminating signal raised out of a curses loop; ``signum`` sets the exit status."""

    def __init__(self, signum: int, detail: str | None = None) -> None:
        super().__init__(detail or signum)
        self.signum = signum
        self.detail = detail


# An 8-colour terminal still separates the tiers, just coarsely.
_BASIC_TIER_COLOUR = {WAITING: 3, MACHINE: 6, GOOD: 2, BAD: 1, INERT: 7}


@dataclass(frozen=True)
class ModalCandidate:
    """One frozen candidate with distinct raw identity and safe presentation."""

    value: str
    detail: str = ""
    display: str | None = None

    @property
    def display_value(self) -> str:
        return _safe_text(self.value) if self.display is None else self.display


@dataclass(frozen=True)
class ModalFrame:
    """Terminal-independent, cell-bounded modal projection."""

    rows: tuple[str, ...]
    cursor: tuple[int, int] | None
    visible_start: int
    visible_end: int
    row_roles: tuple[str, ...] = ()


def _safe_text(value: Any) -> str:
    text = str(value).replace("\t", " ")
    return "".join(
        character if character.isprintable() and
        unicodedata.category(character) not in {"Cf", "Cs"} else "?"
        for character in text
    )


def _clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    safe = _safe_text(value)
    return safe if len(safe) <= width else safe[:max(0, width - 1)] + "…"


def _safe_error(exc: BaseException) -> str:
    return _safe_text(exc)[:1200] or "controller failure"


def init_colours(curses_module) -> bool:
    """Five tier pairs plus one decoration pair, or nothing on a mono terminal.

    Returns whether colour is available. Every failure path leaves the screen
    exactly as it renders today: the glyph and the short label already carry
    the tier, so a terminal without colour loses emphasis, never meaning.
    """
    has_colors = getattr(curses_module, "has_colors", None)
    start_color = getattr(curses_module, "start_color", None)
    init_pair = getattr(curses_module, "init_pair", None)
    error = getattr(curses_module, "error", Exception)
    if not callable(has_colors) or not callable(start_color) or not callable(init_pair):
        return False
    try:
        if not has_colors():
            return False
        start_color()
        background = 0
        use_default = getattr(curses_module, "use_default_colors", None)
        if callable(use_default):
            try:
                use_default()
                background = -1
            except error:
                background = 0
        usable = getattr(curses_module, "COLORS", 8) or 8
        for tier, index in TIER_PAIR.items():
            colour = TIER_XTERM[tier] if usable >= 256 else _BASIC_TIER_COLOUR[tier]
            init_pair(index, colour, background)
        decoration = DECORATION_XTERM if usable >= 256 else getattr(curses_module, "COLOR_BLUE", 4)
        init_pair(DECORATION_PAIR, decoration, background)
    except error:
        return False
    except Exception:
        return False
    return True


def _attribute(curses_module, tier: str | None, coloured: bool) -> int:
    """A tier's screen attribute, or 0 where the terminal offers none.

    Attributes are decoration: a curses build without them, or a terminal
    without colour, still shows the glyph and the short label, which is where
    the meaning lives. Bold marks only the two loud tiers, because bold is one
    attribute and cannot encode five.
    """
    if tier is None:
        return 0
    bold = getattr(curses_module, "A_BOLD", 0) if tier in {WAITING, BAD} else 0
    if not coloured:
        return bold
    colour_pair = getattr(curses_module, "color_pair", None)
    if not callable(colour_pair):
        return bold
    try:
        return colour_pair(TIER_PAIR[tier]) | bold
    except Exception:
        return bold


_ZWJ = "\u200d"
_KEYCAP = "\u20e3"


def _is_variation_selector(character: str) -> bool:
    codepoint = ord(character)
    return 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF


def _is_emoji_modifier(character: str) -> bool:
    return 0x1F3FB <= ord(character) <= 0x1F3FF


def _is_regional_indicator(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def _is_emoji_base(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x1F000 <= codepoint <= 0x1FAFF or
        0x2600 <= codepoint <= 0x27FF or
        codepoint in {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x3030, 0x303D}
    )


def _is_emoji_modifier_base(character: str) -> bool:
    codepoint = ord(character)
    return (
        codepoint in {0x261D, 0x26F9, 0x1F385, 0x1F3C7, 0x1F47C, 0x1F48F,
                      0x1F491, 0x1F4AA,
                      0x1F57A, 0x1F590, 0x1F6A3, 0x1F6C0, 0x1F6CC, 0x1F926,
                      0x1F90C, 0x1F90F, 0x1F977, 0x1F9BB} or
        0x270A <= codepoint <= 0x270D or
        0x1F3C2 <= codepoint <= 0x1F3C4 or
        0x1F3CA <= codepoint <= 0x1F3CC or
        0x1F442 <= codepoint <= 0x1F443 or
        0x1F446 <= codepoint <= 0x1F450 or
        0x1F466 <= codepoint <= 0x1F478 or
        0x1F481 <= codepoint <= 0x1F483 or
        0x1F485 <= codepoint <= 0x1F487 or
        0x1F574 <= codepoint <= 0x1F575 or
        0x1F595 <= codepoint <= 0x1F596 or
        0x1F645 <= codepoint <= 0x1F647 or
        0x1F64B <= codepoint <= 0x1F64F or
        0x1F6B4 <= codepoint <= 0x1F6B6 or
        0x1F918 <= codepoint <= 0x1F91F or
        0x1F930 <= codepoint <= 0x1F939 or
        0x1F93C <= codepoint <= 0x1F93E or
        0x1F9B5 <= codepoint <= 0x1F9B6 or
        0x1F9B8 <= codepoint <= 0x1F9B9 or
        0x1F9CD <= codepoint <= 0x1F9CF or
        0x1F9D1 <= codepoint <= 0x1F9DD or
        0x1FAC3 <= codepoint <= 0x1FAC5 or
        0x1FAF0 <= codepoint <= 0x1FAF8
    )


_SUPPORTED_PROFESSION_ZWJ_BASES = frozenset({"👨", "👩", "🧑"})
_PROFESSION_ZWJ_TARGET = "💻"


def _is_supported_zwj_prefix(value: str) -> bool:
    if value.count(_ZWJ) != 1 or not value.endswith(_ZWJ):
        return False
    left = value[:-1]
    bases = [
        character for character in left
        if not _is_variation_selector(character) and
        not _is_emoji_modifier(character)
    ]
    modifiers = [character for character in left if _is_emoji_modifier(character)]
    return (
        len(bases) == 1 and bases[0] in _SUPPORTED_PROFESSION_ZWJ_BASES and
        len(modifiers) <= 1
    )


def _is_supported_zwj_sequence(value: str) -> bool:
    if value.count(_ZWJ) != 1:
        return False
    left, right = value.split(_ZWJ)
    if not _is_supported_zwj_prefix(left + _ZWJ):
        return False
    right_without_selectors = "".join(
        character for character in right
        if not _is_variation_selector(character)
    )
    return right_without_selectors == _PROFESSION_ZWJ_TARGET


def _is_cluster_extension(character: str) -> bool:
    return (
        bool(unicodedata.combining(character)) or
        unicodedata.category(character) in {"Mn", "Me"} or
        _is_variation_selector(character)
    )


def _display_clusters(value: str) -> list[str]:
    """Group terminal graphemes needed by Control's sanitized prompt input."""
    clusters: list[str] = []
    for character in value:
        if not clusters:
            clusters.append(character)
            continue
        current = clusters[-1]
        if _is_emoji_modifier(character):
            visible = [
                item for item in current
                if (
                    not _is_cluster_extension(item) and item != _ZWJ and
                    not _is_emoji_modifier(item) and item != _KEYCAP
                )
            ]
            if (
                _ZWJ not in current and visible and
                _is_emoji_modifier_base(visible[-1]) and
                not any(_is_emoji_modifier(item) for item in current)
            ):
                clusters[-1] += character
            else:
                clusters.append(character)
            continue
        if character == _KEYCAP:
            without_selectors = "".join(
                item for item in current if not _is_variation_selector(item)
            )
            if (
                not current.endswith(_ZWJ) and _KEYCAP not in current and
                len(without_selectors) == 1 and
                without_selectors in "#*0123456789"
            ):
                clusters[-1] += character
            else:
                clusters.append(character)
            continue
        if current.endswith(_ZWJ) and _is_supported_zwj_sequence(current + character):
            clusters[-1] += character
            continue
        if character == _ZWJ:
            if _is_supported_zwj_prefix(current + character):
                clusters[-1] += character
            else:
                clusters.append(character)
            continue
        if _is_cluster_extension(character):
            clusters[-1] += character
            continue
        if _is_regional_indicator(character):
            regional_count = sum(_is_regional_indicator(item) for item in current)
            if regional_count % 2 == 1 and all(
                _is_regional_indicator(item) for item in current
            ):
                clusters[-1] += character
                continue
        clusters.append(character)
    return clusters


def _cluster_width(cluster: str) -> int:
    visible = [
        character for character in cluster
        if (
            not _is_cluster_extension(character) and character != _ZWJ and
            not _is_emoji_modifier(character) and character != _KEYCAP
        )
    ]
    if not visible:
        return 2 if any(_is_emoji_modifier(item) for item in cluster) else 0
    if (
        (_KEYCAP in cluster and visible[0] in "#*0123456789") or
        sum(_is_regional_indicator(character) for character in visible) >= 2 or
        (
            any(_is_emoji_modifier(character) for character in cluster) and
            any(_is_emoji_modifier_base(character) for character in visible)
        ) or
        _is_supported_zwj_sequence(cluster) or
        ("\ufe0f" in cluster and any(_is_emoji_base(character) for character in visible))
    ):
        return 2
    width = sum(
        2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        for character in visible
    )
    if any(_is_emoji_modifier(character) for character in cluster):
        width += 2
    return width


def _cell_width(value: str) -> int:
    """Return terminal cells for prompt-safe extended grapheme clusters."""
    return sum(_cluster_width(cluster) for cluster in _display_clusters(value))


_MAX_MODAL_CANDIDATES = 128
_MAX_VISIBLE_CANDIDATES = 8
_MAX_CANDIDATE_BYTES = 256 * 1024


def _cell_lines(value: str, budget: int) -> list[str]:
    """Wrap complete display clusters without exceeding a cell budget."""
    if budget <= 0:
        return [""] if value else []
    rows: list[str] = []
    current: list[str] = []
    used = 0
    for cluster in _display_clusters(value):
        width = _cluster_width(cluster)
        if current and used + width > budget:
            rows.append("".join(current))
            current = []
            used = 0
        if width > budget:
            # A complete wide cluster cannot be drawn in this viewport.
            if current:
                rows.append("".join(current))
                current = []
                used = 0
            continue
        current.append(cluster)
        used += width
    if current or not rows:
        rows.append("".join(current))
    return rows


def _bounded_modal_candidates(
    candidates: Iterable[ModalCandidate],
) -> tuple[ModalCandidate, ...]:
    retained: list[ModalCandidate] = []
    used = 0
    for candidate in candidates:
        if len(retained) >= _MAX_MODAL_CANDIDATES:
            break
        if not isinstance(candidate.value, str):
            continue
        value = candidate.value
        display = _safe_text(candidate.display_value)
        detail = _safe_text(candidate.detail)
        size = (
            len(value.encode("utf-8")) + len(display.encode("utf-8")) +
            len(detail.encode("utf-8"))
        )
        if used + size > _MAX_CANDIDATE_BYTES:
            break
        retained.append(ModalCandidate(value, detail, display))
        used += size
    return tuple(retained)


def modal_frame(
    *, title: str, context: str, label: str, hint: str, value: str,
    candidates: Iterable[ModalCandidate] = (), selected: int | None = None,
    height: int, width: int, prompt: str | None = None,
) -> ModalFrame:
    """Return one cell-aware modal frame for forms, menus, and confirmations."""
    height = max(0, int(height))
    width = max(0, int(width))
    budget = max(0, width - 1)
    if height == 0:
        return ModalFrame((), None, 0, 0, ())
    bounded = _bounded_modal_candidates(candidates)
    if selected is not None and bounded:
        selected = min(max(0, int(selected)), len(bounded) - 1)
    else:
        selected = None

    field_prompt = f"{label}: " if label else ""
    prefix = f"[TYPING] > {field_prompt if prompt is None else prompt}"
    viewport, cursor_x = _prompt_viewport(
        prefix,
        None if prompt is None else hint,
        value, budget,
    )
    # Input is the only modal row whose loss can make the editor unusable.
    # Reserve it first, then spend remaining rows on explanatory decoration.
    decoration: list[str] = []
    for text in (title, context):
        if text:
            logical_lines = str(text).splitlines() or [""]
            for logical_line in logical_lines:
                decoration.extend(_cell_lines(_safe_text(logical_line), budget))
    if hint and prompt is None:
        decoration.extend(_cell_lines(_safe_text(hint), budget))

    decoration_capacity = max(0, height - 1)
    decoration_omitted = len(decoration) > decoration_capacity
    if decoration_omitted and decoration_capacity == 0:
        viewport, cursor_x = _prompt_viewport(
            "… " + prefix,
            None if prompt is None else hint,
            value, budget,
        )
    if decoration_omitted and decoration_capacity:
        retained = decoration[:max(0, decoration_capacity - 1)]
        retained.append(_prefix_cells("… additional context omitted", budget))
        decoration = retained
    else:
        decoration = decoration[:decoration_capacity]
    header: list[str] = [viewport, *decoration]
    header_roles: list[str] = ["input", *("context" for _ in decoration)]
    cursor = (0, min(cursor_x, budget))

    remaining = max(0, height - len(header))
    visible_count = (
        0 if decoration_omitted else
        min(_MAX_VISIBLE_CANDIDATES, remaining, len(bounded))
    )
    if visible_count:
        if selected is None:
            start = 0
        else:
            start = min(
                max(0, selected - visible_count + 1),
                len(bounded) - visible_count,
            )
        end = start + visible_count
        candidate_rows: list[str] = []
        candidate_roles: list[str] = []
        for index in range(start, end):
            candidate = bounded[index]
            marker = "> " if index == selected else "  "
            shown = candidate.display_value or "(default)"
            if candidate.detail:
                shown += f"  {candidate.detail}"
            candidate_rows.append(_prefix_cells(marker + shown, budget))
            candidate_roles.append("selected" if index == selected else "inactive")
        if start and candidate_rows:
            candidate_rows[0] = _prefix_cells("↑ " + candidate_rows[0], budget)
        if end < len(bounded) and candidate_rows:
            candidate_rows[-1] = _prefix_cells("↓ " + candidate_rows[-1], budget)
        rows = tuple((header + candidate_rows)[:height])
        roles = tuple((header_roles + candidate_roles)[:height])
        return ModalFrame(rows, cursor, start, end, roles)
    return ModalFrame(tuple(header), cursor, 0, 0, tuple(header_roles))


def _prompt_character_allowed(value: list[str], character: str) -> bool:
    return _shared_prompt_character_allowed("".join(value), character)


def _prefix_cells(value: str, budget: int) -> str:
    result: list[str] = []
    used = 0
    for cluster in _display_clusters(value):
        width = _cell_width(cluster)
        if used + width > budget:
            break
        result.append(cluster)
        used += width
    return "".join(result)


def _suffix_cells(value: str, budget: int) -> str:
    result: list[str] = []
    used = 0
    for cluster in reversed(_display_clusters(value)):
        width = _cell_width(cluster)
        if used + width > budget:
            break
        result.append(cluster)
        used += width
    return "".join(reversed(result))


def _prompt_viewport(
    prompt: str, hint: str | None, value: str, budget: int,
) -> tuple[str, int]:
    """Render an append-only prompt with its active suffix and caret visible."""
    budget = max(0, budget)
    if hint:
        stripped = prompt.rstrip()
        if stripped.endswith(":"):
            full_prompt = f"{stripped[:-1]} ({hint}): "
        else:
            full_prompt = f"{prompt}{hint}: "
    else:
        full_prompt = prompt
    full = full_prompt + value
    if _cell_width(full) <= budget:
        return full, _cell_width(full)
    if budget == 0:
        return "", 0

    if value:
        last = _display_clusters(value)[-1]
        last_width = _cell_width(last)
        if last_width <= budget and budget <= last_width:
            return last, last_width

    marker = "…"
    marker_width = _cell_width(marker)
    if marker_width > budget:
        return "", 0
    label_budget = max(0, budget - marker_width)
    compact = _prefix_cells(prompt, label_budget)
    if value:
        last = _display_clusters(value)[-1]
        needed = _cell_width(last)
        if budget - _cell_width(compact) - marker_width < needed:
            compact = _prefix_cells(
                prompt, max(0, budget - marker_width - needed),
            )
    suffix = _suffix_cells(
        value, max(0, budget - _cell_width(compact) - marker_width),
    )
    line = compact + marker + suffix
    return line, _cell_width(line)


def _read_modal_key(stdscr, curses_module) -> int | str:
    """Read one wide curses key while retaining narrow test-double support."""
    # `Mock` fabricates any requested attribute. Inspect the concrete type so
    # a narrow legacy/test double cannot accidentally become an infinite
    # wide-input source merely because `getattr()` manufactured `get_wch`.
    wide_method = getattr(type(stdscr), "get_wch", None)
    reader = stdscr.get_wch if callable(wide_method) else stdscr.getch
    try:
        key = reader()
    except curses_module.error:
        return -1
    if isinstance(key, str):
        if len(key) != 1:
            return -1
        if ord(key) < 32 or key == "\x7f":
            return ord(key)
        return key
    return key if isinstance(key, int) else -1


@contextmanager
def _visible_cursor(curses_module):
    """Temporarily show the cursor, preserving nested editor ownership."""
    setter = getattr(curses_module, "curs_set", None)
    changed = False
    previous = 0
    if callable(setter):
        try:
            observed = setter(1)
            previous = observed if isinstance(observed, int) else 0
            changed = True
        except Exception:
            pass
    try:
        yield
    finally:
        if changed:
            try:
                setter(previous)
            except Exception:
                pass


def _cursor_editor(function):
    """Give every synchronous editor the same cursor and repaint contract."""
    @wraps(function)
    def wrapped(stdscr, curses_module, *args, **kwargs):
        try:
            with _visible_cursor(curses_module):
                return function(stdscr, curses_module, *args, **kwargs)
        finally:
            # Editors draw outside the tree painter. Their final frame must be
            # cleared even when the returned status message did not change.
            model = args[0] if args else kwargs.get("model")
            if isinstance(model, ModalHost):
                model.dirty = True
    return wrapped


def _modal_controls(*, candidates: bool = False) -> str:
    controls = "Controls: Enter submit  Esc cancel"
    if candidates:
        controls += "  Up/Down candidates  Tab complete"
    return controls


@_cursor_editor
def _prompt_line(
    stdscr, curses_module, model: ModalHost, prompt: str,
    *, initial: str = "", maximum: int = 500, hint: str | None = None,
    candidates: Iterable[ModalCandidate] = (), selected: int | None = None,
    title: str = "", context: str = "",
) -> str | None:
    value = list(initial[:maximum])
    bounded_candidates = _bounded_modal_candidates(candidates)
    candidate_selection = selected
    redraw = True
    repaint_underlay = True
    while True:
        if redraw:
            if repaint_underlay:
                # Prompts are overlays. Restore the tree once on entry and
                # after resize, including when a preceding full-screen modal
                # erased it, but never on an idle input timeout.
                model.dirty = True
                model.paint_underlay(stdscr, curses_module)
                repaint_underlay = False
            height, width = stdscr.getmaxyx()
            if height:
                controls = _modal_controls(candidates=bool(bounded_candidates))
                modal_context = f"{context}\n{controls}" if context else controls
                frame = modal_frame(
                    title=title, context=modal_context, label="", hint=hint or "",
                    value="".join(value), candidates=bounded_candidates,
                    selected=candidate_selection, height=height, width=width,
                    prompt=prompt,
                )
                try:
                    start = max(0, height - len(frame.rows))
                    # Overlay prompts keep explanatory material above the active
                    # input. This preserves the long-standing bottom-line
                    # viewport/cursor contract while forms can still lead with
                    # their active field after clearing the screen.
                    order = [*range(1, len(frame.rows)), 0] if frame.rows else []
                    for offset, frame_index in enumerate(order):
                        line = frame.rows[frame_index]
                        y = start + offset
                        stdscr.move(y, 0)
                        stdscr.clrtoeol()
                        if line and width > 1:
                            role = (
                                frame.row_roles[frame_index]
                                if frame_index < len(frame.row_roles) else "inactive"
                            )
                            attribute = _modal_row_attribute(curses_module, role)
                            try:
                                stdscr.addnstr(y, 0, line, len(line), attribute)
                            except TypeError:
                                stdscr.addnstr(y, 0, line, len(line))
                    if frame.cursor is not None:
                        cursor_y, cursor_x = frame.cursor
                        physical_y = order.index(cursor_y) if cursor_y in order else 0
                        stdscr.move(start + physical_y, cursor_x)
                    stdscr.refresh()
                except curses_module.error:
                    pass
            redraw = False
        key = _read_modal_key(stdscr, curses_module)
        if key == -1:
            continue
        redraw = True
        if key in {10, 13, getattr(curses_module, "KEY_ENTER", -999)}:
            if candidate_selection is not None and bounded_candidates:
                return bounded_candidates[candidate_selection].value
            logical = "".join(value)
            if terminal_text_is_complete(logical):
                return logical
            continue
        if key == 27:
            return None
        if key == getattr(curses_module, "KEY_RESIZE", -998):
            repaint_underlay = True
            continue
        if key in {
            getattr(curses_module, "KEY_UP", -996),
            getattr(curses_module, "KEY_DOWN", -995),
        } and bounded_candidates:
            delta = (
                -1 if key == getattr(curses_module, "KEY_UP", -996) else 1
            )
            if candidate_selection is None:
                candidate_selection = (
                    len(bounded_candidates) - 1 if delta < 0 else 0
                )
            else:
                candidate_selection = min(
                    max(0, candidate_selection + delta),
                    len(bounded_candidates) - 1,
                )
            continue
        if key == 9 and bounded_candidates:
            prefix = "".join(value)
            matches = [
                index for index, item in enumerate(bounded_candidates)
                if item.value.startswith(prefix)
            ]
            if matches:
                candidate_selection = matches[0]
                value = list(bounded_candidates[candidate_selection].value[:maximum])
            continue
        if key in {8, 127, getattr(curses_module, "KEY_BACKSPACE", -997)}:
            if value:
                value = list("".join(_display_clusters("".join(value))[:-1]))
            candidate_selection = None
            continue
        if ((isinstance(key, str) and len(key) == 1) or
                (isinstance(key, int) and 0 <= key <= 0x10FFFF)) and len(value) < maximum:
            character = key if isinstance(key, str) else chr(key)
            if _prompt_character_allowed(value, character):
                value.append(character)
                candidate_selection = None


def _repaint_after_suspend(stdscr) -> None:
    try:
        stdscr.clearok(True)
    except AttributeError:
        pass
    stdscr.touchwin()
    stdscr.refresh()


def _ascii_prefix(candidate: str, prefix: str) -> bool:
    try:
        return candidate.encode("ascii").lower().startswith(
            prefix.encode("ascii").lower(),
        )
    except UnicodeEncodeError:
        return False


def _modal_row_attribute(curses_module, role: str) -> int:
    def supported(name: str) -> int:
        value = getattr(curses_module, name, 0)
        return value if isinstance(value, int) else 0

    if role == "input":
        return supported("A_REVERSE") | supported("A_BOLD")
    if role == "selected":
        return supported("A_BOLD") | supported("A_UNDERLINE")
    if role == "content":
        return 0
    return supported("A_DIM")


def _draw_modal_frame(stdscr, curses_module, frame: ModalFrame) -> None:
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    if width > 1:
        for y, row in enumerate(frame.rows[:height]):
            try:
                role = frame.row_roles[y] if y < len(frame.row_roles) else "inactive"
                attribute = _modal_row_attribute(curses_module, role)
                try:
                    stdscr.addnstr(y, 0, row, len(row), attribute)
                except TypeError:
                    stdscr.addnstr(y, 0, row, len(row))
            except curses_module.error:
                pass
    if frame.cursor is not None and height and width:
        y, x = frame.cursor
        if y < height:
            try:
                stdscr.move(y, min(x, max(0, width - 1)))
            except curses_module.error:
                pass
    try:
        stdscr.refresh()
    except curses_module.error:
        pass


def _native_permission_prompt(stdscr, curses_module, request):
    """Scrollable exact invocation; resizes and navigation never decide it."""
    question = request.get("question")
    if question and request["payload"].get("request", {}).get("protocol") == "codex-app-server-v2":
        # The Codex question ends in the same full payload shown below. Keep
        # scope notices and the prompt, render the exact invocation once.
        question = question.rsplit("\n", 1)[0]
    content = json.dumps({"request_id": request["request_id"], "digest": request["digest"],
                          "question": question,
                          "session_directory": request["cwd"], "observed_state": request["state"],
                          "response_state": request["response_state"],
                          "invocation": request["payload"]}, ensure_ascii=True, indent=2)
    offset = 0
    previous_frame = None
    previous_size = None
    while True:
        height, width = stdscr.getmaxyx()
        budget = max(1, width - 1)
        if previous_size != (height, width):
            # ensure_ascii above makes every displayed character one cell.
            # Avoid scanning Unicode clusters over a large review on resize.
            lines = [line[start:start + budget] for line in content.splitlines()
                     for start in range(0, max(1, len(line)), budget)]
            previous_size = (height, width)
        available = max(1, height - 3)
        offset = min(offset, max(0, len(lines) - available))
        enough_space = height >= 6 and width >= 24
        rows = [_clip("Native permission: exact invocation", budget),
                *lines[offset:offset + available],
                _clip(f"{offset + 1}-{min(len(lines), offset + available)}/{len(lines)} Up/Down PgUp/PgDn", budget),
                _clip("a allow d deny Esc back" if enough_space else "Resize to review and decide", budget)]
        roles = ("selected", *("content" for _ in lines[offset:offset + available]), "inactive", "selected")
        frame = ModalFrame(tuple(rows[:height]), None, offset, offset + available, roles[:height])
        if frame != previous_frame:
            _draw_modal_frame(stdscr, curses_module, frame)
            previous_frame = frame
        key = _read_modal_key(stdscr, curses_module)
        if key == 27:
            return None
        if enough_space and key in {"a", "d", ord("a"), ord("d")}:
            return "allow" if key in {"a", ord("a")} else "deny"
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


def _canonical_field_value(
    field: int, value: str, candidates: tuple[ModalCandidate, ...],
) -> str | None:
    if field == 2:
        try:
            folded = value.encode("ascii").lower()
        except UnicodeEncodeError:
            return None
        return next(
            (item.value for item in candidates
             if item.value.encode("ascii").lower() == folded),
            None,
        )
    if field == 3:
        matched = next(
            (item.value for item in candidates if _ascii_prefix(item.value, value)
             and len(item.value) == len(value)),
            value,
        )
        try:
            return validate_role(matched)
        except ValueError:
            return None
    return value


def _managed_session_view(stdscr, curses_module, config, session_id):
    """Read bounded event pages without terminal attach or changing acknowledgements."""
    from .session_store import SessionStore

    after, history, offset = 0, [], 0
    reload = True
    redraw = True
    while True:
        if reload:
            try:
                with SessionStore(config) as sessions:
                    snapshot = sessions.snapshot(session_id, after=after, limit=100)
            except (OSError, ValueError, StoreError) as exc:
                return f'session inspection unavailable: {_safe_error(exc)}'
            reload = False
        if redraw:
            state = snapshot['sessions'][0]
            height, width = stdscr.getmaxyx()
            body = [f"Session {session_id}", f"Project: {state['cwd']}",
                    f"{state['harness']} · {state['state']} · {snapshot['pending_request_count']} pending requests",
                    'Use M in Control to answer pending questions and permissions.']
            for request in snapshot['requests']:
                body.append(f"Pending {request['kind']}: {request['question']}")
            for event in snapshot['events']:
                payload = event['payload']
                content = payload.get('text') or payload.get('message') or json.dumps(payload, ensure_ascii=False)
                body.append(f"{event['sequence']} {event['kind']}: {content}")
            lines = [line for text in body for line in _cell_lines(_safe_text(text), max(1, width - 1))]
            capacity = max(0, height - 1)
            offset = min(offset, max(0, len(lines) - max(1, capacity)))
            stdscr.erase()
            for y, line in enumerate(['[READING] ↑/↓ scroll  n/p events  r refresh  Esc close',
                                      *lines[offset:offset + capacity]][:height]):
                try:
                    stdscr.addnstr(y, 0, _prefix_cells(line, max(0, width - 1)), max(0, width - 1))
                except curses_module.error:
                    pass
            stdscr.refresh()
            redraw = False
        key = _read_modal_key(stdscr, curses_module)
        if isinstance(key, str):
            key = ord(key)
        if key == -1:
            continue
        redraw = True
        if key in {27, ord('q')}:
            return 'session inspection closed; runtime policy is unchanged'
        if key == getattr(curses_module, 'KEY_DOWN', -996):
            offset += 1
        elif key == getattr(curses_module, 'KEY_UP', -997):
            offset = max(0, offset - 1)
        elif key == ord('r'):
            reload = True
        elif key == ord('n') and not snapshot['complete']['events'] and snapshot['events']:
            history.append(after)
            after, offset, reload = snapshot['events'][-1]['sequence'], 0, True
        elif key == ord('p') and history:
            after, offset, reload = history.pop(), 0, True


def _popup_room_command(
    stdscr, curses_module, config: ControlConfig, env: Mapping[str, str],
    adapter, command: list[str], attach: str, label: str,
) -> str | None:
    from .cli import _run_command_popup

    curses_module.endwin()
    try:
        return _run_command_popup(
            adapter, config, command, attach, label, env,
        )
    finally:
        _repaint_after_suspend(stdscr)


@_cursor_editor
def _project_launch_form(
    stdscr, curses_module, model: ModalHost, config: ControlConfig,
    env: Mapping[str, str], *, session: Mapping[str, Any],
) -> str:
    """The session dashboard's launch form (#102).

    Project, Harness, Assignment (or Topic), then optional Model and Effort.
    ``session['launch']`` receives the accepted values and returns the status
    message; a refusal it raises stays on the form beside the field.
    """
    from .projects import list_projects_across, resolve_roots
    from .rooms import RoomError, resolve_project, room_harness_available

    roots, source = resolve_roots(env=env)
    payload = list_projects_across(roots, depth=3, source_of_roots=source)
    project_candidates = _bounded_modal_candidates(
        ModalCandidate(
            item["root"],
            f"{item.get('name') or item.get('directory')}  {item.get('project_id') or 'uninitialized'}",
        )
        for item in payload["projects"]
        if item.get("asha_project") and item.get("project_id")
    )
    title, subject = session['title'], 'session'
    harness_candidates = _bounded_modal_candidates(
        ModalCandidate(name, "installed")
        for name in sorted(HARNESSES)
        if room_harness_available(name, env)
    )
    if not harness_candidates:
        return f"{subject} launch refused: no supported harness is available"

    fields = ("Project", "Harness", session['prompt_label'], "Model", "Effort")
    maximums = (4096, 16, 4000, 256, 64)
    default_harness = next((item.value for item in harness_candidates if item.value == 'claude'),
                           harness_candidates[0].value)
    values = [project_candidates[0].value if project_candidates else "", default_harness, '', '', '']
    harness_field = 1
    prompt_field = 2
    field = 0
    selected: int | None = 0 if project_candidates else None
    form_notice = ""
    redraw = True

    def candidates_for(index: int) -> tuple[ModalCandidate, ...]:
        if index == 0:
            return project_candidates
        if index == harness_field:
            return harness_candidates
        return ()

    while field < len(fields):
        candidates = candidates_for(field)
        if redraw:
            height, width = stdscr.getmaxyx()
            completed = "  ".join(
                f"{fields[index]}: {values[index]}"
                for index in range(field) if values[index]
            )
            context_lines = [
                f"Field {field + 1}/{len(fields)}  Controls: Tab next  Shift-Tab previous  "
                "Enter accept/submit  Esc cancel  Up/Down candidates",
            ]
            if completed:
                context_lines.append(completed)
            if form_notice:
                context_lines.append(f"Error beside {fields[field]}: {form_notice}")
            context_lines.append(session['hint'])
            frame = modal_frame(
                title=title, context="\n".join(context_lines),
                label=fields[field], hint="", value=values[field],
                candidates=candidates, selected=selected,
                height=height, width=width,
            )
            _draw_modal_frame(stdscr, curses_module, frame)
            redraw = False
        key = _read_modal_key(stdscr, curses_module)
        if key == -1:
            continue
        redraw = True
        if key == getattr(curses_module, "KEY_RESIZE", -998):
            continue
        form_notice = ""
        if key == 27:
            return f"{subject} launch cancelled"
        if key == getattr(curses_module, "KEY_BTAB", -994):
            if field:
                field -= 1
            selected = None
            continue
        if key in {
            getattr(curses_module, "KEY_UP", -997),
            getattr(curses_module, "KEY_DOWN", -996),
        } and candidates:
            delta = -1 if key == getattr(curses_module, "KEY_UP", -997) else 1
            if selected is None:
                selected = len(candidates) - 1 if delta < 0 else 0
            else:
                selected = min(max(0, selected + delta), len(candidates) - 1)
            continue
        if key == 9 or key in {10, 13, getattr(curses_module, "KEY_ENTER", -995)}:
            if key == 9 and field == len(fields) - 1:
                form_notice = f"Tab has no next field; Enter submits the {subject}."
                model.message = form_notice
                selected = None
                continue
            if key == 9 and candidates and selected is None:
                matches = [
                    index for index, item in enumerate(candidates)
                    if (
                        _ascii_prefix(item.value, values[field])
                        if field == harness_field else item.value.startswith(values[field])
                    )
                ]
                if matches:
                    selected = matches[0]
            accepted = (
                candidates[selected].value if selected is not None and candidates
                else values[field]
            )
            canonical: str | None = accepted
            if field == 0:
                try:
                    canonical = resolve_project(accepted, env=env)["root"]
                except RoomError as exc:
                    canonical = None
                    form_notice = _safe_error(exc)
            elif field == harness_field:
                canonical = _canonical_field_value(2, accepted, candidates)
                if canonical is None:
                    form_notice = "Harness must be one installed supported candidate."
            elif field == prompt_field and (
                not accepted.strip() or not terminal_text_is_complete(accepted)
            ):
                canonical = None
                form_notice = (
                    f"{fields[prompt_field]} is required and must end with a complete "
                    "supported Unicode cluster."
                )
            elif field > prompt_field:
                # Model and effort are optional; blank keeps the harness default (#95).
                canonical = accepted.strip()
            if canonical is None:
                model.message = form_notice
                selected = None
                continue
            values[field] = canonical
            if field == len(fields) - 1:
                try:
                    return session['launch'](project=values[0], harness=values[1], prompt=values[2],
                                             model=values[3] or None, effort=values[4] or None)
                except (ValueError, OSError, StoreError) as exc:
                    form_notice = _safe_error(exc)
                    model.message = form_notice
                    selected = None
                    continue
            field += 1
            selected = None
            continue
        if key in {8, 127, getattr(curses_module, "KEY_BACKSPACE", -997)}:
            clusters = _display_clusters(values[field])
            if clusters:
                values[field] = "".join(clusters[:-1])
            selected = None
            continue
        if ((isinstance(key, str) and len(key) == 1) or
                (isinstance(key, int) and 0 <= key <= 0x10FFFF)) and \
                len(values[field]) < maximums[field]:
            character = key if isinstance(key, str) else chr(key)
            logical = list(values[field])
            if _prompt_character_allowed(logical, character):
                values[field] += character
                selected = None

    return f"{subject} launch cancelled"


def _select_managed_request(stdscr, curses_module, model, config):
    """Page questions independently of session counts; selection never resolves one."""
    from .session_store import SessionStore

    after = None
    while True:
        with SessionStore(config) as sessions:
            snapshot = sessions.current_work(kind="requests", limit=_MAX_MODAL_CANDIDATES, after=after)
        pending = snapshot["rows"]
        candidates = [ModalCandidate(r["request_id"], _safe_text(r["question"])[:160],
                                    display=r["session_id"][:8]) for r in pending]
        next_cursor = snapshot["next_cursor"]
        more = not snapshot["complete"]
        advancing = more and next_cursor is not None and next_cursor != after
        if advancing:
            candidates.append(ModalCandidate("next", "Next request page"))
        elif more:
            candidates.append(ModalCandidate("retry", "Retry this partial page"))
        candidates.append(ModalCandidate("refresh", "Refresh from the beginning"))
        selected = _prompt_line(
            stdscr, curses_module, model, "Request: ", maximum=36,
            title="Managed questions and permissions",
            context=(f"{len(pending)} requests on this page; " +
                     ("more requests may be unread" if more else "end of current requests") +
                     ". Pages can change between reads. Enter a request ID or select one."),
            candidates=tuple(candidates),
        )
        if selected == "next" and advancing:
            after = next_cursor
        elif selected == "retry" and more and not advancing:
            continue
        elif selected == "refresh":
            after = None
        elif selected in {"next", "retry"}:
            continue
        else:
            return selected


def _decide_managed_permission(stdscr, curses_module, model, config, env, request_id):
    """Shared exact-invocation review for the selected head's a key and global M."""
    from .sessions import overview, refuse_managed_operator
    from .session_store import SessionStore
    from .native_requests import NativeRequests
    refuse_managed_operator(config, env)
    with SessionStore(config) as sessions:
        detail = NativeRequests(sessions).get(request_id)
    if detail['state'] != 'pending':
        return "request is no longer pending"
    decision = _native_permission_prompt(stdscr, curses_module, detail)
    if decision is None:
        return "permission decision cancelled"
    with SessionStore(config) as sessions:
        NativeRequests(sessions).decide(request_id, decision, expected_digest=detail['digest'])
    model.managed_summary = overview(config)['summary']
    return "native permission decision retained; response delivery is pending"


def _answer_session_request(stdscr, curses_module, model, config, env, request_id=None) -> None:
    """Answer one pending session question or permission; the outcome is ``model.message``.

    Without a request the legacy TUI's ``M`` key pages the pending ones first.
    """
    from .sessions import overview, refuse_managed_operator
    from .session_store import SessionStore
    refuse_managed_operator(config, env)
    current = overview(config)
    if not current["questions"] and not current.get("permissions", 0):
        model.message = current["summary"]
        return
    selected_request = request_id or _select_managed_request(stdscr, curses_module, model, config)
    if not selected_request:
        model.message = "question selection cancelled"
        return
    with SessionStore(config) as sessions:
        request = sessions.get_request(selected_request)
    if request["state"] != "pending":
        model.message = "request is no longer pending"
        return
    if request["kind"] == "native-clarification":
        from .native_requests import NativeRequests
        with SessionStore(config) as sessions:
            detail = NativeRequests(sessions).get(request["request_id"])
        answers = {}
        for question in detail["payload"]["request"]["params"]["questions"]:
            context = question["question"]
            if question.get("options"):
                context += "\n" + "\n".join(option["label"] + ": " + option["description"]
                                             for option in question["options"])
            answer = _prompt_line(stdscr, curses_module, model, "Answer: ",
                title="Answer Codex question", maximum=4000,
                context=_safe_text(context))
            if answer is None or not answer.strip():
                model.message = "answer cancelled"
                return
            answers[question["id"]] = {"answers": [answer]}
        with SessionStore(config) as sessions:
            NativeRequests(sessions).answer_native(request["request_id"], {"answers": answers},
                expected_digest=detail["digest"])
        model.managed_summary = overview(config)["summary"]
        model.message = "native answers retained; response delivery is pending"
        return
    if request["kind"] == "native-permission":
        model.message = _decide_managed_permission(
            stdscr, curses_module, model, config, env, request['request_id'])
        return
    answer = _prompt_line(stdscr, curses_module, model, "Answer: ",
                          title="Answer session question", maximum=4000,
                          context=_safe_text(request["question"]))
    if answer is None or not answer.strip():
        model.message = "answer cancelled"
        return
    with SessionStore(config) as sessions:
        sessions.answer(request["request_id"], answer, expected_digest=request["digest"])
    model.managed_summary = overview(config)["summary"]
    model.message = "answer retained; backend resumes the session when eligible"
