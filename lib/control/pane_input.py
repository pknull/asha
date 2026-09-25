"""Prove an idle harness input line is empty before Control types into it.

Issue #96: a graceful close (or a sent message) may be typed into an owned,
detached, idle terminal pane. This module only reads a captured screen; it
never decides ownership, attachment or idleness. Every answer other than
``empty`` refuses injection, so an unrecognised layout falls back to attach.

Captured lines may carry SGR escapes (``capture-pane -e``). Claude Code draws
its prompt as ``❯`` between two horizontal rules; any text after the marker,
including a dim placeholder, counts as occupied. Codex draws ``›`` above a
blank line and its footer. Empty needs every signal a native capture shows for
an empty composer: nothing after the marker but a dim placeholder, no
continuation line, and the footer's ``? for shortcuts`` hint (Codex hides it
while the composer holds text). Anything else under a visible composer is
``occupied``.

``composer_holds`` is the check made after pasting and before Enter: the whole
input region, up to a native boundary, must hold exactly the pasted text, so a
draft that appeared after the screen was read is never submitted. Only display
wraps are tolerated; an unbindable Claude paste placeholder refuses.
"""
from __future__ import annotations

import re

_SGR = re.compile(r"\x1b\[([0-9;:]*)m")
_ESCAPE = re.compile(r"\x1b(?:\[[0-9;:?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|.)")
_BLANK = "  \t"
# Markers are compared on the visible text of one captured line.
_MARKERS = {"claude": "❯", "codex": "›"}
INJECTABLE_HARNESSES = frozenset(_MARKERS)


def visible(line: str) -> str:
    return _ESCAPE.sub("", line)


def _border(line: str) -> int | None:
    """Width of a Claude input-box border: an unindented run of at least ten ``─``.

    Draft lines inside the box are indented under the prompt, so a rule typed
    in a draft is never a border.
    """
    text = visible(line).rstrip(_BLANK)
    return len(text) if len(text) >= 10 and set(text) == {"─"} else None


def _prompt_index(lines: list[str], marker: str) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if visible(lines[index]).lstrip(_BLANK).startswith(marker):
            return index
    return None


def _claude(lines: list[str]) -> tuple[str, str]:
    if any(("-- NORMAL --" in visible(line) or "-- VISUAL" in visible(line)) for line in lines):
        return "unknown", "vim normal or visual mode would interpret typed keys as commands"
    index = _prompt_index(lines, _MARKERS["claude"])
    if index is None:
        return "unknown", "no Claude prompt line is visible"
    top = _border(lines[index - 1]) if index else None
    if top is None:
        return "unknown", "the Claude prompt line is not inside its input box"
    if index + 1 >= len(lines) or _border(lines[index + 1]) != top:
        return "occupied", "the Claude input box holds more than one line"
    rest = visible(lines[index]).lstrip(_BLANK)[1:]
    if rest.strip(_BLANK):
        return "occupied", "the Claude input line is not empty"
    return "empty", "the Claude input line is empty"


def _sgr_params(group: str) -> list[str]:
    """SGR parameters as units: extended colours (38/48/58) swallow their arguments."""
    params, parts = [], [p for p in group.split(";")] if group else ["0"]
    index = 0
    while index < len(parts):
        part = parts[index] or "0"
        if ":" in part:
            params.append("colon:" + part)  # Colon sub-parameters form one unit.
            index += 1
            continue
        if part in {"38", "48", "58"} and index + 1 < len(parts):
            width = {"5": 2, "2": 4}.get(parts[index + 1], 1)
            params.append("colour:" + ";".join(parts[index:index + 1 + width]))
            index += 1 + width
            continue
        params.append(part)
        index += 1
    return params


def _dim_only(raw: str) -> bool:
    """True when every non-blank character after the marker is drawn dim."""
    dim, seen_marker, position = False, False, 0
    for match in _SGR.finditer(raw + "\x1b[m"):
        chunk = visible(raw[position:match.start()])
        for char in chunk:
            if not seen_marker:
                seen_marker = char == _MARKERS["codex"]
                continue
            if char not in _BLANK and not dim:
                return False
        for param in _sgr_params(match.group(1)):
            if param in {"0", "22"}:
                dim = False
            elif param == "2":
                dim = True
        position = match.end()
    return seen_marker


_CODEX_EMPTY_HINT = re.compile(r"\?\s+for shortcuts")
_CODEX_FOOTER_LINES = 3


def _codex(lines: list[str]) -> tuple[str, str]:
    index = _prompt_index(lines, _MARKERS["codex"])
    if index is None:
        return "unknown", "no Codex composer line is visible"
    tail = [visible(line).strip(_BLANK) for line in lines[index + 1:]]
    if not _dim_only(lines[index]):
        return "occupied", "the Codex composer is not empty"
    if not tail or tail[0]:
        return "occupied", "the Codex composer continues past its first line"
    footer = tail[1:]
    while footer and not footer[0]:
        footer = footer[1:]
    if (not footer or len(footer) > _CODEX_FOOTER_LINES or not all(footer)
            or not _CODEX_EMPTY_HINT.search(footer[-1])):
        return "occupied", "the Codex composer is not proven empty (unrecognised footer)"
    return "empty", "the Codex composer shows only its placeholder and the empty-composer hint"


_CLAUDE_PASTE = re.compile(r"\[Pasted text #\d+")
_CODEX_PASTE = re.compile(r"\[Pasted Content (\d+) chars\]")
_EDGE = _BLANK + "\u00a0"


def _claude_region(lines: list[str]) -> list[str] | None:
    """Visible lines from the prompt to the closing rule (both rules must show)."""
    index = _prompt_index(lines, _MARKERS["claude"])
    top = _border(lines[index - 1]) if index else None
    if top is None:
        return None
    for end in range(index + 1, len(lines)):
        width = _border(lines[end])
        if width == top:
            return [visible(line) for line in lines[index:end]]
        if width is not None:
            return None  # a border-like line of another width: ambiguous
    return None


_SGR_RESETS = frozenset({"0", "22", "23", "24", "25", "27", "28", "29", "39", "49", "59"})


def _styled(raw: str) -> bool:
    """True when the line sets real styling (a colour or attribute), not only resets."""
    return any(param not in _SGR_RESETS
               for match in _SGR.finditer(raw) for param in _sgr_params(match.group(1)))


def _codex_region(lines: list[str]) -> list[str] | None:
    """Composer lines of a non-empty Codex composer, bounded by its native footer.

    The composer runs from the prompt to the first blank line. That blank line
    counts as the boundary only when everything after it is one block of 1-3
    footer lines, each drawn with SGR styling (typed composer text never is),
    none a paste placeholder, and nothing blank among them. A blank line inside
    a draft therefore leaves a second block and refuses; so does a missing or
    unstyled footer.
    """
    index = _prompt_index(lines, _MARKERS["codex"])
    if index is None:
        return None
    rest = lines[index + 1:]
    while rest and not visible(rest[-1]).strip(_BLANK):
        rest.pop()
    blanks = [i for i, line in enumerate(rest) if not visible(line).strip(_BLANK)]
    if not blanks:
        return None
    footer = rest[blanks[0] + 1:]
    if (not footer or len(footer) > _CODEX_FOOTER_LINES
            or any(not visible(line).strip(_BLANK) or not _styled(line)
                   or _CODEX_PASTE.search(visible(line)) for line in footer)):
        return None
    return [visible(line) for line in [lines[index], *rest[:blanks[0]]]]


_INDENT = "  "  # Claude and Codex align continuation lines under the text.


def _display_parts(harness: str, region: list[str]) -> list[str] | None:
    """The text shown on each region line, or None for an unexpected layout.

    The prompt line is the marker plus one space; each continuation line is
    the two-space indent. tmux captures drop blanks at a line end, so trailing
    blanks carry no information and are trimmed; nothing else is.
    """
    first = region[0].lstrip(_BLANK)[len(_MARKERS[harness]):]
    if first[:1] in {" ", "\u00a0"}:
        first = first[1:]
    parts = [first.rstrip(_EDGE)]
    for line in region[1:]:
        if not line.startswith(_INDENT):
            return None
        parts.append(line[len(_INDENT):].rstrip(_EDGE))
    return parts


def _wrapped_equals(parts: list[str], text: str) -> bool:
    """``parts`` (display lines) show exactly ``text``.

    The only allowed difference is the one space a line was wrapped at: it may
    be missing, or begin the next line. Nothing inside a line is normalised,
    and an empty display line is an extra newline, never a wrap.
    """
    position = 0
    for number, part in enumerate(parts):
        if not part:
            return False
        if number and text.startswith(" ", position):
            position += 1
            if part.startswith(" "):
                part = part[1:]
        if not text.startswith(part, position):
            return False
        position += len(part)
    return position == len(text)


def composer_holds(harness: str, lines: list[str], text: str) -> bool:
    """True only when the whole input region holds exactly ``text`` (after a paste).

    ``text`` must already be one ``flatten``ed line. The region is bounded by
    Claude's two rules, or by Codex's styled footer (``_codex_region``); every
    line in it must be part of the text. A Claude paste placeholder cannot be
    bound to this paste and refuses. A Codex placeholder counts only as the
    whole composer and only with this text's exact character count.
    """
    if not isinstance(text, str) or not text or flatten(text) != text:
        return False
    lines = [line.rstrip("\n") for line in lines]
    region = (_claude_region if harness == "claude" else _codex_region if harness == "codex" else None)
    region = region(lines) if region else None
    parts = _display_parts(harness, region) if region else None
    if not parts:
        return False
    if harness == "claude" and any(_CLAUDE_PASTE.search(part) for part in parts):
        return False
    if harness == "codex" and len(parts) == 1:
        match = _CODEX_PASTE.fullmatch(parts[0])
        if match is not None:
            return int(match.group(1)) == len(text)
    return _wrapped_equals(parts, text)


def input_line_state(harness: str, lines: list[str]) -> tuple[str, str]:
    """``(empty|occupied|unknown, detail)`` for a captured pane screen."""
    lines = [line.rstrip("\n") for line in lines]
    while lines and not visible(lines[-1]).strip(_BLANK):
        lines.pop()
    if harness == "claude":
        return _claude(lines)
    if harness == "codex":
        return _codex(lines)
    return "unknown", f"typing into a {harness} pane is not supported"


def flatten(text: str) -> str:
    """One line with no control characters, so typing it cannot submit early."""
    text = _ESCAPE.sub("", text)
    cleaned = "".join(c if c.isprintable() else " " for c in text)
    return " ".join(cleaned.split())


__all__ = ["INJECTABLE_HARNESSES", "composer_holds", "flatten", "input_line_state", "visible"]
