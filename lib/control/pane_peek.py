"""Read-only pane capture for the session preview (#102 phase 3).

``PanePeek`` holds one validated pane id and runs exactly one fixed tmux argv, a
screen capture, which creates no tmux client: the client count and the #96
client generation that Control's own delivery and close fences read stay
unchanged. ``peek_room`` re-verifies exact Room ownership before every capture
and again after it, and discards the screen if the pane changed in between.

A capture's own command body writes nothing, but tmux runs any configured
``after-capture-pane`` hook afterwards, server-side, and a hook can type into
any pane or reach another server through ``run-shell`` (QA17 Q17-F1). The
ownership reads carry the same risk through ``after-display-message`` and
``after-show-options``. So before the ownership reads, and again immediately
before the capture, every hook scope that applies to the pane is read with
``show-hooks`` (which has no hook of its own), and a configured guarded hook,
a failed query or unreadable output refuses the preview. Operator hooks are
never cleared or changed. A hook set between the last check and the capture
can still run: docs/session-hub.md states that residual race. A tmux server
``command-alias`` can also rewrite any of these reads, ``show-hooks`` included,
into an input command (QA18 Q18-F1); nothing here detects that, which is why the
dashboard only previews when ``control.session_preview`` opts in.

A static test forbids every tmux verb here except the capture and the hook
read, so keep the vocabulary of this file small.
"""
from __future__ import annotations

import re
import time
from typing import Any, Mapping

from .rooms import _owned_state
from .tmux import TmuxAdapter, TmuxError, _validate_pane_id

MAX_LINES = 200          # scrollback lines one capture may request
LINE_CHARS = 2000        # the same per-line bound the Room screen read uses
OUTPUT_BYTES = 1024 * 1024
DEADLINE_SECONDS = 2     # shared by every tmux read of one preview
# Ownership states whose pane still exists and is ours; an exited harness keeps
# its last screen until the Room is closed.
_CAPTURABLE = frozenset({"open", "ended"})
# Hooks tmux would run for a command this module issues, in report order.
# after-has-session and after-show-hooks do not exist in tmux 3.4, and
# command-error only in later releases; they are listed so a tmux that adds
# them is refused rather than trusted.
GUARDED_HOOKS = (
    "after-capture-pane", "after-display-message", "after-show-options",
    "after-has-session", "after-show-hooks", "command-error",
)
# `show-hooks` prints `name[index] command` per configured entry and a bare
# `name` for a hook with no entries.
_HOOK_LINE = re.compile(r"([a-z][a-z-]*)(?:\[[0-9]+\] (\S.*))?")


class PeekRefused(ValueError):
    """The pane is not provably this Room's; nothing was captured or shown."""


class PeekDisabled(PeekRefused):
    """A tmux hook could run on a preview read, or the hooks could not be read."""

    def __init__(self, hook: str | None = None, *, detail: str = ""):
        self.hook = hook
        super().__init__(
            f"Preview disabled: tmux {hook} hook configured" if hook
            else f"Preview disabled: tmux hooks could not be verified ({detail})"
        )


class _Budget:
    """One deadline for a whole preview read; each call returns the time left.

    Every tmux call asks it first, so once ``cancelled`` is set (the dashboard
    closed) no further tmux command starts; one already running ends at its
    own deadline.
    """

    def __init__(self, seconds: float, cancelled=None):
        self._end = time.monotonic() + seconds
        self._cancelled = cancelled

    def __call__(self) -> float:
        if self._cancelled is not None and self._cancelled.is_set():
            raise TmuxError("preview read cancelled")
        remaining = self._end - time.monotonic()
        if remaining <= 0:
            raise TmuxError("preview read timed out")
        return remaining


def configured_hooks(output: bytes) -> set[str]:
    """Guarded hook names with at least one command in one ``show-hooks`` listing."""
    try:
        text = output.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PeekDisabled(detail="undecodable show-hooks output") from exc
    found = set()
    for line in text.splitlines():
        match = _HOOK_LINE.fullmatch(line)
        if match is None:
            raise PeekDisabled(detail="unrecognised show-hooks output")
        if match.group(2) is not None and match.group(1) in GUARDED_HOOKS:
            found.add(match.group(1))
    return found


def refuse_if_hooked(tmux: TmuxAdapter, pane: str, budget=None) -> None:
    """Read every hook scope that applies to ``pane``; refuse on any guarded hook."""
    budget = budget or _Budget(DEADLINE_SECONDS)
    scopes = (["-g"], ["-gw"], ["-t", pane], ["-w", "-t", pane], ["-p", "-t", pane])
    for scope in scopes:
        try:
            returncode, stdout, stderr = tmux._run_status(
                ['show-hooks', *scope], deadline_seconds=budget(),
            )
        except TmuxError as exc:
            raise PeekDisabled(detail=str(exc)) from exc
        if returncode != 0:
            diagnostic = stderr[:200].decode("utf-8", errors="replace").strip()
            if "can't find pane" in diagnostic.casefold():
                raise PeekRefused("pane missing: exact tmux pane is absent")
            raise PeekDisabled(detail=diagnostic)
        found = configured_hooks(stdout)
        for name in GUARDED_HOOKS:
            if name in found:
                raise PeekDisabled(name)


class PanePeek:
    """One owned pane, read-only: ``capture`` is the only operation."""

    __slots__ = ("_tmux", "_pane")

    def __init__(self, tmux: TmuxAdapter, pane_id: str):
        self._tmux = tmux
        self._pane = _validate_pane_id(pane_id)

    @staticmethod
    def argv(pane: str, lines: int) -> list[str]:
        return ['capture-pane', "-p", "-J", "-t", pane, "-S", f"-{lines}"]

    def capture(self, lines: int, *, deadline_seconds: float = DEADLINE_SECONDS) -> list[str]:
        """The last ``lines`` screen and history lines, joined where tmux wrapped them."""
        if type(lines) is not int or lines < 1:
            raise ValueError("capture line count must be a positive integer")
        lines = min(lines, MAX_LINES)
        tmux = self._tmux
        raw = tmux._run_bytes(
            tmux.executable, self.argv(self._pane, lines),
            limit=OUTPUT_BYTES, deadline_seconds=min(deadline_seconds, DEADLINE_SECONDS),
        )
        screen = raw.decode("utf-8", errors="replace").splitlines()
        while screen and not screen[-1].strip():
            screen.pop()
        return [line[:LINE_CHARS] for line in screen[-lines:]]


def owned_pane(record: Mapping[str, Any], tmux: TmuxAdapter, budget=None) -> str:
    """The Room's pane id once exact ownership is verified, else ``PeekRefused``."""
    state, detail = _owned_state(record, tmux, deadline=budget or _Budget(DEADLINE_SECONDS))
    if state not in _CAPTURABLE:
        raise PeekRefused(f"pane {state}: {detail}")
    return record["tmux"]["pane_id"]


def peek_room(record: Mapping[str, Any], tmux: TmuxAdapter, lines: int, *, cancelled=None) -> list[str]:
    """Hooks, ownership, hooks, capture, ownership: every call checks again.

    ``cancelled`` (a ``threading.Event``) stops the read before its next tmux call.
    """
    budget = _Budget(DEADLINE_SECONDS, cancelled)
    try:
        pane = _validate_pane_id(record["tmux"].get("pane_id") or "")
    except TmuxError as exc:
        raise PeekRefused(f"pane mismatch: {exc}") from exc
    refuse_if_hooked(tmux, pane, budget)
    owned_pane(record, tmux, budget)
    refuse_if_hooked(tmux, pane, budget)
    screen = PanePeek(tmux, pane).capture(lines, deadline_seconds=budget())
    try:
        owned_pane(record, tmux, budget)
    except PeekRefused as exc:
        # Q17-F2: the pane may have been replaced while it was read; never show that screen.
        raise PeekRefused(f"{exc}; capture discarded") from exc
    return screen
