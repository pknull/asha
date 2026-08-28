"""Semantic tiers, the pipeline rail, and the coloured line primitive.

The records carry 57 state words across four classes. An operator cannot hold
57 words in their eye, so every state resolves to exactly one of five tiers,
and the tier is what colour means:

    WAITING   nothing advances until the operator acts (the only loud tier)
    MACHINE   work is in flight; visible, never urgent
    GOOD      settled and passed
    BAD       settled and failed
    INERT     real but not actionable: not reached, held, or history

Colour never carries alone where a word can carry with it: the STATE column
shows a short label and the rail shows a glyph, both of which survive a
monochrome terminal. The 72-column layout drops the STATE column and is the
one place the glyph carries alone; that is a stated cost, not an oversight.

Nothing here imports curses. `Line` is a `str` subclass, so every caller that
treats a rendered line as text keeps working; only the painter reads spans.
"""

from __future__ import annotations

import unicodedata
from typing import Any, Iterable, Mapping, Sequence

WAITING, MACHINE, GOOD, BAD, INERT = "waiting", "machine", "good", "bad", "inert"
TIERS = (WAITING, MACHINE, GOOD, BAD, INERT)

# True xterm-256 indices. The design mockups use these exact values, so what
# was approved is what a 256-colour terminal draws.
TIER_XTERM = {WAITING: 214, MACHINE: 74, GOOD: 71, BAD: 167, INERT: 245}
TIER_PAIR = {tier: index + 1 for index, tier in enumerate(TIERS)}
DECORATION_PAIR = len(TIERS) + 1
DECORATION_XTERM = 240

_STATE_TIER: dict[str, str] = {}


def _assign(tier: str, *states: str) -> None:
    for state in states:
        _STATE_TIER[state] = tier


_assign(
    WAITING,
    "awaiting-plan-approval", "needs-input", "ready-for-integration",
    "awaiting-exit", "reported", "requested",
)
_assign(
    MACHINE,
    "planning", "running", "dispatching", "evaluating", "sealing", "allocated",
    "active", "waiting", "starting", "stopping",
    # A ready node owes the machine a dispatch, not the operator a decision.
    "ready",
)
_assign(
    GOOD,
    "approved", "succeeded", "sealed-success", "success-seal-ready",
    "completed-readonly", "readonly-ready", "accepted-pass", "passed",
    "compatible", "integrated",
)
_assign(
    BAD,
    "failed", "sealed-failure", "failure-seal-ready", "abnormal-exit",
    "launch-failed", "failed-no-artifact", "result-missing", "indeterminate",
    # Finalised without full success: settled, but not a success.
    "partial",
)
_assign(
    INERT,
    "draft", "proposed", "blocked", "paused", "paused-seal-ready",
    "sealed-paused", "cancelled", "archived", "superseded", "stale", "absent",
    "exited", "fenced", "idle", "ended",
)

# Short labels for the STATE column. Only states whose own word does not fit
# an 11-column field need one; everything else prints as itself.
SHORT_LABEL = {
    "awaiting-plan-approval": "approve",
    "ready-for-integration": "integrate",
    "needs-input": "needs you",
    "sealed-success": "sealed ok",
    "sealed-failure": "seal failed",
    "success-seal-ready": "seal ready",
    "failure-seal-ready": "seal failed",
    "paused-seal-ready": "seal held",
    "sealed-paused": "held",
    "completed-readonly": "read-only",
    "readonly-ready": "read-only",
    "failed-no-artifact": "no artifact",
    "launch-failed": "no launch",
    "abnormal-exit": "crashed",
    "result-missing": "no result",
    "indeterminate": "unknown",
    "dispatching": "starting",
    "evaluating": "checking",
    "awaiting-exit": "awaiting X",
    "superseded": "superseded",
}

STATE_COLUMN = 11


def tier_for(state: str | None) -> str:
    """Every known state resolves to exactly one tier; unknown text is inert."""
    if not state:
        return INERT
    return _STATE_TIER.get(state, INERT)


def short_label(state: str | None, width: int = STATE_COLUMN) -> str:
    """The word shown in the STATE column: never truncated into ambiguity.

    `sealed-success` and `success-seal-ready` both begin 'se' and both exceed
    the column, so clipping would render them as near-identical stubs. Every
    state that cannot fit gets a deliberate short form instead.
    """
    if not state:
        return ""
    label = SHORT_LABEL.get(state, state)
    return label if len(label) <= width else label[: max(0, width - 1)] + "…"


# ---------------------------------------------------------------- the rail
# Terminal for the purposes of the count line: nothing further will happen.
TERMINAL_STATES = frozenset({"integrated", "archived", "cancelled", "partial"})

STAGES = ("plan", "approve", "build", "review", "verify", "integrate")

GLYPHS = {
    "unicode": {GOOD: "✓", WAITING: "!", MACHINE: "●", BAD: "✗", INERT: "·", "held": "◼"},
    # ◆ ● ◼ ✓ ✗ are East-Asian-ambiguous width: under a CJK locale, or any
    # terminal treating ambiguous as wide, each takes two cells and every
    # column right of it shifts. This fallback is exact-width everywhere.
    "ascii": {GOOD: "+", WAITING: "!", MACHINE: ">", BAD: "x", INERT: ".", "held": "="},
}
ROW_GLYPH = {"unicode": {WAITING: "◆", MACHINE: "●", GOOD: "✓", BAD: "✗", INERT: "◼"},
             "ascii": {WAITING: "!", MACHINE: ">", GOOD: "+", BAD: "x", INERT: "="}}

RAIL_WIDTH = len(STAGES) + 2  # the six glyphs plus their brackets

_ACTIVE = {"running", "dispatching", "evaluating", "sealing", "allocated"}
_DEMANDS = {"needs-input"}


def _work_stage(nodes: Sequence[Mapping[str, Any]], kinds: set[str]) -> str:
    """One stage's tier, read from the nodes that produce it.

    Order matters and is deliberate: a demand for the operator outranks a
    failure, which outranks live work, which outranks a hold. The stage is
    only GOOD when every node of its kind has succeeded.
    """
    relevant = [node for node in nodes if node.get("type") in kinds]
    if not relevant:
        return ""
    states = [node.get("state") for node in relevant]
    if any(state in _DEMANDS for state in states):
        return WAITING
    if any(state == "failed" for state in states):
        return BAD
    if any(state in _ACTIVE for state in states):
        return MACHINE
    if all(state in {"succeeded", "cancelled", "superseded"} for state in states):
        return GOOD if any(state == "succeeded" for state in states) else INERT
    return INERT


def rail_tiers(view: Mapping[str, Any]) -> list[str]:
    """Six stage tiers for one initiative: plan approve build review verify integrate.

    Returns a tier per stage, or "" for a stage not yet reached. Derived from
    the stored record only — never from pane text.
    """
    initiative = view.get("initiative") or {}
    state = initiative.get("state")
    nodes = list(view.get("nodes") or [])
    # Evidence, not inference: a terminal state says where an initiative ended,
    # never how it got there. `draft -> cancelled` is a legal transition, and
    # keying the ticks on the state alone made that initiative claim a plan and
    # an approval it never had. A stage is only ticked when its record exists.
    planned = view.get("plan") is not None
    reached_approval = state in {
        "approved", "running", "needs-input", "paused", "ready-for-integration",
        "integrated", "partial", "failed", "cancelled", "archived",
    }

    plan = GOOD if planned else (MACHINE if state == "planning" else "")
    if state == "awaiting-plan-approval":
        approve = WAITING if planned else ""
    elif planned and reached_approval:
        approve = GOOD
    else:
        approve = ""
    build = _work_stage(nodes, {"work"})
    review = _work_stage(nodes, {"review"})
    verify = _work_stage(nodes, {"verify"})
    if not verify and view.get("verifications"):
        outcomes = [item.get("outcome") or item.get("state") for item in view["verifications"]]
        verify = GOOD if all(item == "passed" for item in outcomes) else BAD

    integration_recorded = any(
        event.get("type") == "seal-integration-recorded"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("disposition") == "integrated"
        for event in view.get("events", []) or []
    )
    if state == "integrated" or integration_recorded:
        integrate = GOOD
    elif state == "ready-for-integration":
        integrate = WAITING
    elif state in {"failed", "partial", "cancelled"}:
        integrate = BAD
    else:
        integrate = ""

    return [plan, approve, build, review, verify, integrate]


def rail_text(view: Mapping[str, Any], *, glyphs: str = "unicode", paused: bool = False) -> str:
    """The rail as plain text, brackets included: `[++>...]`."""
    table = GLYPHS[glyphs]
    body = []
    for tier in rail_tiers(view):
        if not tier:
            body.append(table[INERT])
        elif paused and tier == MACHINE:
            body.append(table["held"])
        else:
            body.append(table[tier])
    return "[" + "".join(body) + "]"


# ---------------------------------------------------------------- the line
class Line(str):
    """A rendered line that also knows which columns carry which tier.

    Subclassing `str` is the point: every existing caller and test treats a
    rendered line as text and keeps working unchanged. Only the painter looks
    for `spans`, and a plain `str` simply has none.
    """

    __slots__ = ("spans",)

    def __new__(cls, text: str, spans: Iterable[tuple[int, int, str]] = ()) -> "Line":
        line = super().__new__(cls, text)
        line.spans = tuple(spans)  # type: ignore[misc]
        return line

    def clipped(self, width: int) -> "Line":
        """Clip to `width` columns, dropping and trimming spans to match."""
        if width <= 0:
            return Line("", ())
        if len(self) <= width:
            return self
        text = self[: max(0, width - 1)] + "…"
        limit = len(text)
        spans = [(start, min(stop, limit), tier)
                 for start, stop, tier in self.spans if start < limit]
        return Line(text, spans)


def safe_text(value: Any) -> str:
    """Neutralise anything that could move the cursor or reorder the line.

    Every cell passes through here on its way to the terminal, so a control
    code, bidi override, surrogate or unassigned codepoint in a slug, goal or
    evidence string cannot corrupt the display — regardless of whether the
    record validators upstream happened to reject it.
    """
    text = str(value).replace("\t", " ")
    return "".join(
        character if character.isprintable()
        and unicodedata.category(character) not in {"Cf", "Cs"} else "?"
        for character in text
    )


class LineBuilder:
    """Accumulate padded cells and remember each one's tier."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._spans: list[tuple[int, int, str]] = []
        self._width = 0

    def add(self, text: str, width: int = 0, tier: str | None = None) -> "LineBuilder":
        if width < 0:
            return self  # no room at all; width 0 still means natural width
        cell = safe_text(text)
        cell = cell.ljust(width)[:width] if width else cell
        if tier:
            self._spans.append((self._width, self._width + len(cell), tier))
        self._parts.append(cell)
        self._width += len(cell)
        return self

    def column(self, text: str, width: int, tier: str | None = None) -> "LineBuilder":
        """A cell that always leaves one space before the next column.

        The retired renderer joined cells with `" ".join(...)`, so a value that
        filled its column still could not touch its neighbour. Padding alone
        loses that, and a long slug runs straight into the pipeline bracket.
        """
        if width <= 1:
            return self.add(text, width, tier)
        return self.add(safe_text(text)[: width - 1], width, tier)

    def build(self) -> Line:
        return Line("".join(self._parts), self._spans)


def display_state(view: Mapping[str, Any]) -> tuple[str, str]:
    """The tier and label a row actually shows.

    Rolled up from the children in exactly one direction: an initiative whose
    node demands the operator shows that demand, because a request for a human
    that is only visible after expanding a row is a request nobody sees. A
    failing child is NOT rolled up — retries are allocated automatically, so
    the initiative is still the machine's move; the rail already shows the ✗.
    """
    initiative = view.get("initiative") or {}
    state = initiative.get("state")
    if state == "approved":
        return WAITING, "activate"
    tier = tier_for(state)
    if tier != WAITING and WAITING in rail_tiers(view):
        return WAITING, "needs you"
    return tier, short_label(state)


def summary_counts(rows: Sequence[Any]) -> dict[str, int]:
    """Counts the operator can audit against the rows in front of them.

    Counted from the RENDERED initiative rows, not from the view list, because
    the rows are what a filter narrows. Counting views while displaying rows
    made the title advertise a demand that was filtered off screen. Bucketed by
    the tier each row displays, so the amber count equals the number of amber
    rows; every counted row lands in exactly one bucket, so they sum.
    """
    counts = {"initiatives": 0, "waiting": 0, "running": 0, "failed": 0,
              "paused": 0, "settled": 0, "idle": 0}
    for row in rows:
        if getattr(row, "kind", None) != "initiative":
            continue
        counts["initiatives"] += 1
        display = getattr(row, "display", None)
        tier = display[0] if display else tier_for(getattr(row, "state", None))
        if tier == WAITING:
            counts["waiting"] += 1
        elif getattr(row, "state", None) == "paused":
            counts["paused"] += 1
        elif tier == BAD:
            counts["failed"] += 1
        elif tier == MACHINE:
            counts["running"] += 1
        elif getattr(row, "state", None) in TERMINAL_STATES:
            counts["settled"] += 1
        else:
            counts["idle"] += 1
    return counts


__all__ = [
    "WAITING", "MACHINE", "GOOD", "BAD", "INERT", "TIERS", "TIER_XTERM", "TIER_PAIR",
    "DECORATION_PAIR", "DECORATION_XTERM", "STAGES", "GLYPHS", "ROW_GLYPH", "RAIL_WIDTH",
    "STATE_COLUMN", "Line", "LineBuilder", "rail_text", "rail_tiers", "short_label",
    "summary_counts", "tier_for", "display_state", "safe_text", "TERMINAL_STATES",
]
