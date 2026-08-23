"""Pure terminal UI model plus a thin, lazily imported curses driver."""

from __future__ import annotations

import copy
import importlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TextIO

from .config import ControlConfig, load_config
from .jj import (
    DEFAULT_BASE_REVSET, DiffSummary, JjAdapter, JjError, discover_git_root,
)
from .harness import HARNESSES, validate_role
from .launch import archive_task, stop_task
from .model import GIT_OBJECT_ID_PATTERN, new_uuid, retry_task_slug
from .prepare import retained_recovery_guidance, v2_retention_diagnostic
from .prerequisites import (
    ControlTermination, PrerequisiteApplyIndeterminate, StartPrerequisiteOffer,
    StartPrerequisiteRefusal,
    apply_ignore_prerequisite, decode_worker_refusal,
)
from .prune import (
    PruneError, PruneRecordStore, assemble_prune_context, prune_one_task,
)
from .reconcile import LiveAdapters, StateObservation
from .store import StoreError, TaskStore
from .tmux import TmuxAdapter, TmuxError
from .transaction import CreationJournalStore, JournalError
from .text import (
    prompt_character_allowed as _shared_prompt_character_allowed,
    terminal_text_is_complete,
)
from . import view


_STATE_ORDER = {
    "stale": 0,
    "needs-input": 1,
    "working": 2,
    "starting": 3,
    "unknown": 4,
    "idle": 5,
    "failed": 6,
    "exited": 7,
    "creating": 8,
    "ended": 9,
    "archived": 10,
}
_FIXED_SCREEN_LINES = 15
_INPUT_POLL_MS = 200
_AUTO_REFRESH_SECONDS = 5.0
_START_OUTPUT_BYTES = 64 * 1024
_START_DRAIN_SECONDS = 0.25
_START_CLEANUP_SECONDS = 0.5
_DEFERRED_START_WORKERS: list[subprocess.Popen] = []
_DEGRADE_MESSAGE = (
    "asha control: terminal TUI unavailable; use `asha task list --json` "
    "as the non-interactive fallback."
)


class IntentKind(Enum):
    NONE = "none"
    OPEN = "open"
    START = "start"
    RECONCILE = "reconcile"
    DIFF = "diff"
    ARCHIVE = "archive"
    ACTIONS = "actions"
    TOGGLE_SCOPE = "toggle-scope"
    FILTER = "filter"
    QUIT = "quit"
    HELP = "help"
    TOGGLE_MODE = "toggle-mode"
    INIT_OPEN = "initiative-open"
    INIT_RECONCILE = "initiative-reconcile"
    INIT_DIFF = "initiative-diff"
    INIT_EVENTS = "initiative-events"
    INIT_APPROVE = "initiative-approve"
    INIT_CANDIDATES = "initiative-candidates"
    INIT_VERIFICATION = "initiative-verification"
    INIT_STORAGE = "initiative-storage"
    INIT_PAUSE = "initiative-pause"
    INIT_STOP = "initiative-stop"
    INIT_EXPAND = "initiative-expand"
    INIT_COLLAPSE = "initiative-collapse"


@dataclass(frozen=True)
class TuiIntent:
    kind: IntentKind
    task_id: str | None = None
    run_id: str | None = None
    requires_confirmation: bool = False
    reason: str | None = None
    initiative_id: str | None = None
    target: str | None = None


# Initiatives-mode key table (proposal "Minimal TUI contract"); bindings are
# per mode, so Tasks keeps a=archive/x=actions/n=start/A=scope untouched.
_INITIATIVE_KEYS: dict[str, IntentKind] = {
    "r": IntentKind.INIT_RECONCILE, "d": IntentKind.INIT_DIFF, "e": IntentKind.INIT_EVENTS,
    "a": IntentKind.INIT_APPROVE, "c": IntentKind.INIT_CANDIDATES,
    "v": IntentKind.INIT_VERIFICATION, "t": IntentKind.INIT_STORAGE,
    "p": IntentKind.INIT_PAUSE, "s": IntentKind.INIT_STOP,
}


@dataclass(frozen=True)
class TuiRow:
    task: dict[str, Any]
    reconciliation: dict[str, Any]
    observation: StateObservation

    @classmethod
    def from_records(
        cls, task: dict[str, Any], reconciliation: dict[str, Any],
        observation: StateObservation | None = None,
    ) -> "TuiRow":
        selected = observation or StateObservation(
            reconciliation["state"],
            task["runs"][-1]["run_id"] if task["runs"] else None,
            "unknown", None, "unknown",
            "presentation provenance was not supplied",
        )
        if (selected.run_id is not None and
                all(run["run_id"] != selected.run_id for run in task["runs"])):
            raise ValueError("TUI observation run does not belong to the task")
        if selected.state != reconciliation["state"]:
            raise ValueError("TUI observation state does not match reconciliation")
        return cls(
            copy.deepcopy(task), copy.deepcopy(reconciliation),
            copy.deepcopy(selected),
        )

    @property
    def summary(self) -> dict[str, Any]:
        return view.task_summary(self.task, self.reconciliation)

    @property
    def display_state(self) -> str:
        return self.observation.state


def lifecycle_row(task: dict[str, Any]) -> TuiRow:
    """Project archived history without consulting mutable live resources."""
    reconciliation, observation = view.archived_lifecycle_projection(task)
    return TuiRow.from_records(task, reconciliation, observation)


@dataclass(frozen=True)
class DetailProjection:
    task_id: str
    slug: str
    run_id: str | None
    role: str | None
    tmux: str
    evidence: str
    source: str
    observed_at: str | None
    freshness: str
    workspace: str
    change: str
    blocker: str | None
    diff_summary: str | None
    diff_refreshed_at: str | None


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


@dataclass(frozen=True)
class StartCandidateSnapshot:
    repositories: tuple[ModalCandidate, ...]
    bases: Mapping[str, tuple[ModalCandidate, ...]]
    harnesses: tuple[ModalCandidate, ...]
    roles: tuple[str, ...]

    def bases_for(self, repository: str) -> tuple[ModalCandidate, ...]:
        return self.bases.get(repository, (ModalCandidate("", "default"),))


def _row_sort_key(row: TuiRow) -> tuple[Any, ...]:
    summary = row.summary
    return (
        _STATE_ORDER.get(row.display_state, 99),
        summary["slug"].casefold(),
        summary["repository"]["root"].casefold(),
        summary["task_id"],
    )


def sort_rows(rows: Iterable[TuiRow]) -> tuple[TuiRow, ...]:
    """Return detached rows in one stable, evidence-priority order."""
    detached = [
        TuiRow.from_records(row.task, row.reconciliation, row.observation)
        for row in rows
    ]
    return tuple(sorted(detached, key=_row_sort_key))


def filter_rows(rows: Iterable[TuiRow], filter_string: str) -> tuple[TuiRow, ...]:
    """Filter without altering or reordering the supplied row collection."""
    query = filter_string.casefold().strip()
    if not query:
        return tuple(rows)
    result: list[TuiRow] = []
    for row in rows:
        summary = row.summary
        harnesses = " ".join(run["harness"] for run in row.task["runs"])
        searchable = " ".join((
            row.display_state, summary["slug"], summary["label"],
            summary["repository"]["root"], summary["repository"]["identity"],
            row.task["jj"]["change_id"] or "", harnesses,
        )).casefold()
        if query in searchable:
            result.append(row)
    return tuple(result)


class TuiModel:
    """Terminal-independent task list, selection, filter, and detail state."""

    def __init__(
        self,
        rows: Iterable[TuiRow] = (),
        *,
        selection: int | None = 0,
        filter_string: str = "",
        height: int = 24,
        width: int = 100,
        now: datetime | None = None,
        include_archived: bool = False,
    ) -> None:
        self.rows = sort_rows(rows)
        self.selection = selection
        self.filter_string = filter_string
        self.height = max(0, int(height))
        self.width = max(0, int(width))
        self.now = now or datetime.now(timezone.utc)
        self.include_archived = bool(include_archived)
        self._clock_pinned = now is not None
        self.scroll_offset = 0
        self.diffs: dict[str, DiffSummary] = {}
        self.message: str | None = None
        self.automatic_refresh_error: str | None = None
        self.help_visible = False
        # Initiatives mode (Increment 6): loaded lazily so a malformed
        # orchestration configuration degrades this mode, never Tasks.
        self.mode = "tasks"
        self.initiatives: Any = None
        self.initiatives_error: str | None = None
        self._clamp_selection()

    @property
    def filtered_rows(self) -> tuple[TuiRow, ...]:
        return filter_rows(self.rows, self.filter_string)

    @property
    def visible_capacity(self) -> int:
        return max(0, self.height - _FIXED_SCREEN_LINES)

    @property
    def visible_rows(self) -> tuple[TuiRow, ...]:
        rows = self.filtered_rows
        if self.visible_capacity == 0:
            return ()
        return rows[self.scroll_offset:self.scroll_offset + self.visible_capacity]

    @property
    def selected_row(self) -> TuiRow | None:
        rows = self.filtered_rows
        if self.selection is None or not rows:
            return None
        return rows[self.selection]

    @property
    def detail(self) -> DetailProjection | None:
        row = self.selected_row
        if row is None:
            return None
        task = row.task
        run = next(
            (item for item in task["runs"]
             if item["run_id"] == row.observation.run_id),
            None,
        )
        evidence = row.observation.detail
        diff = self.diffs.get(task["task_id"])
        return DetailProjection(
            task_id=task["task_id"],
            slug=task["slug"],
            run_id=None if run is None else run["run_id"],
            role=None if run is None else run["role"],
            tmux=f"{task['tmux']['session']}:{task['tmux']['window']}"
                 + ("" if run is None else f" {run['pane_id']}"),
            evidence=evidence,
            source=row.observation.source,
            observed_at=row.observation.observed_at,
            freshness=row.observation.freshness,
            workspace=task["jj"]["workspace_path"],
            change=task["jj"]["change_id"] or "not recorded",
            blocker=row.reconciliation["blocker"],
            diff_summary=None if diff is None else diff.summary,
            diff_refreshed_at=None if diff is None else diff.refreshed_at,
        )

    def _clamp_selection(self) -> None:
        count = len(self.filtered_rows)
        if count == 0:
            self.selection = None
            self.scroll_offset = 0
            return
        if self.selection is None:
            self.selection = 0
        self.selection = min(max(int(self.selection), 0), count - 1)
        self._ensure_visible()

    def _ensure_visible(self) -> None:
        if self.selection is None:
            self.scroll_offset = 0
            return
        capacity = self.visible_capacity
        if capacity == 0:
            self.scroll_offset = self.selection
        elif self.selection < self.scroll_offset:
            self.scroll_offset = self.selection
        elif self.selection >= self.scroll_offset + capacity:
            self.scroll_offset = self.selection - capacity + 1
        maximum = max(0, len(self.filtered_rows) - max(1, capacity))
        self.scroll_offset = min(max(0, self.scroll_offset), maximum)

    def move_selection(self, delta: int) -> TuiRow | None:
        rows = self.filtered_rows
        if not rows:
            self.selection = None
            return None
        current = 0 if self.selection is None else self.selection
        self.selection = min(max(current + int(delta), 0), len(rows) - 1)
        self._ensure_visible()
        return self.selected_row

    def set_filter(self, value: str) -> None:
        self.filter_string = value
        self.selection = 0
        self.scroll_offset = 0
        self._clamp_selection()

    def resize(self, height: int, width: int) -> tuple[TuiRow, ...]:
        self.height = max(0, int(height))
        self.width = max(0, int(width))
        self._ensure_visible()
        return self.visible_rows

    def replace_rows(self, rows: Iterable[TuiRow]) -> None:
        selected_task = None if self.selected_row is None else self.selected_row.task["task_id"]
        if not self._clock_pinned:
            # AGE is relative to now; a clock frozen at TUI start showed 0s
            # forever for any task started afterwards.
            self.now = datetime.now(timezone.utc)
        self.rows = sort_rows(rows)
        visible = self.filtered_rows
        self.selection = next(
            (index for index, row in enumerate(visible)
             if row.task["task_id"] == selected_task),
            0 if visible else None,
        )
        self._clamp_selection()

    def replace_row(self, row: TuiRow) -> None:
        task_id = row.task["task_id"]
        retained = [item for item in self.rows if item.task["task_id"] != task_id]
        retained.append(row)
        self.replace_rows(retained)

    def select_task(self, task_id: str) -> bool:
        for index, row in enumerate(self.filtered_rows):
            if row.task["task_id"] == task_id:
                self.selection = index
                self._ensure_visible()
                return True
        return False

    def record_diff(self, task_id: str, diff: DiffSummary) -> None:
        if all(row.task["task_id"] != task_id for row in self.rows):
            raise ValueError("diff summary does not belong to a displayed task")
        self.diffs[task_id] = diff

    def dispatch_key(self, key: str) -> TuiIntent:
        normalized = "ENTER" if key in {"\n", "\r", "ENTER"} else key
        if normalized in {"\t", "TAB"}:
            return TuiIntent(IntentKind.TOGGLE_MODE)
        if self.mode == "initiatives":
            return self._dispatch_initiatives_key(normalized)
        row = self.selected_row
        detail = self.detail
        if normalized == "ENTER":
            if row is None:
                return TuiIntent(IntentKind.NONE, reason="no task is selected")
            return TuiIntent(
                IntentKind.OPEN, row.task["task_id"],
                None if detail is None else detail.run_id,
            )
        if normalized == "n":
            return TuiIntent(IntentKind.START)
        if normalized == "r":
            return self._selected_intent(IntentKind.RECONCILE)
        if normalized == "d":
            return self._selected_intent(IntentKind.DIFF)
        if normalized == "a":
            if row is None:
                return TuiIntent(IntentKind.NONE, reason="no task is selected")
            terminal_runs = all(
                run["state"] in {"exited", "failed"}
                for run in row.reconciliation["runs"]
            )
            eligible = (
                row.task["lifecycle"] == "ended" or
                (row.task["lifecycle"] == "running" and
                 bool(row.reconciliation["runs"]) and terminal_runs) or
                (row.task["lifecycle"] == "failed" and terminal_runs)
            )
            if not eligible:
                return TuiIntent(
                    IntentKind.NONE, task_id=row.task["task_id"],
                    reason="only a task whose runs have all exited can be archived",
                )
            return TuiIntent(
                IntentKind.ARCHIVE, task_id=row.task["task_id"],
                requires_confirmation=True,
            )
        if normalized == "x":
            return self._selected_intent(IntentKind.ACTIONS)
        if normalized == "A":
            return TuiIntent(IntentKind.TOGGLE_SCOPE)
        if normalized == "/":
            return TuiIntent(IntentKind.FILTER)
        if normalized == "q":
            return TuiIntent(IntentKind.QUIT)
        if normalized == "?":
            return TuiIntent(IntentKind.HELP)
        return TuiIntent(IntentKind.NONE)

    def _dispatch_initiatives_key(self, key: str) -> TuiIntent:
        if key == "q":
            return TuiIntent(IntentKind.QUIT)
        if key == "?":
            return TuiIntent(IntentKind.HELP)
        if key == "/":
            return TuiIntent(IntentKind.FILTER)
        if key == "RIGHT":
            return TuiIntent(IntentKind.INIT_EXPAND)
        if key == "LEFT":
            return TuiIntent(IntentKind.INIT_COLLAPSE)
        screen = self.initiatives
        row = None if screen is None else screen.selected_row
        if row is None:
            return TuiIntent(IntentKind.NONE, reason="no initiative is selected")
        if key == "ENTER":
            return TuiIntent(IntentKind.INIT_OPEN, initiative_id=row.initiative_id, target=row.id)
        kind = _INITIATIVE_KEYS.get(key)
        if kind is None:
            return TuiIntent(IntentKind.NONE)
        confirm = kind in {IntentKind.INIT_PAUSE, IntentKind.INIT_STOP, IntentKind.INIT_APPROVE}
        return TuiIntent(
            kind, initiative_id=row.initiative_id, target=row.id, requires_confirmation=confirm,
        )

    def _selected_intent(self, kind: IntentKind) -> TuiIntent:
        row = self.selected_row
        if row is None:
            return TuiIntent(IntentKind.NONE, reason="no task is selected")
        detail = self.detail
        return TuiIntent(
            kind, task_id=row.task["task_id"],
            run_id=None if detail is None else detail.run_id,
        )


def _safe_text(value: Any) -> str:
    text = str(value).replace("\t", " ")
    return "".join(
        character if character.isprintable() and
        unicodedata.category(character) not in {"Cf", "Cs"} else "?"
        for character in text
    )


def _age(value: str | None, now: datetime) -> str:
    try:
        observed = datetime.fromisoformat(value[:-1] + "+00:00")
    except (TypeError, ValueError):
        return "?"
    seconds = max(0, int((now - observed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    safe = _safe_text(value)
    return safe if len(safe) <= width else safe[:max(0, width - 1)] + "…"


def _table_line(
    values: tuple[str, str, str, str, str, str], width: int,
) -> str:
    task_width = max(8, width - 54)
    widths = (11, task_width, 14, 10, 9, 5)
    cells = [
        _clip(value, cell_width).ljust(cell_width)
        for value, cell_width in zip(values, widths)
    ]
    return " ".join(cells).rstrip()


def render(model: TuiModel) -> list[str]:
    """Render the current model to bounded plain text without terminal calls."""
    if model.mode == "initiatives":
        return _render_initiatives(model)
    if model.help_visible:
        lines = [
            "ASHA CONTROL HELP",
            "",
            "Keys: Enter inspect | x context actions | A active/all scope | Tab initiatives",
            "      n start | r reconcile | d diff | a archive | / filter | q quit | ? help",
            "",
            "Status: every state is derived from qualified tmux, process, jj, and event evidence.",
            "Actions: x offers fresh-state signals or initiative stop; every stop is confirmed.",
            "Limitations: prune requires separate confirmation; no automated integration.",
            "Refresh: synchronous bounded adapter calls may delay keys after a pass starts.",
            "Modal prompts pause automatic reconciliation until they close.",
            "Closing a popup detaches only. SIGKILL and hard crashes cannot restore terminal mode.",
        ]
        return [_clip(line, model.width) for line in lines[:model.height]]

    title = f"ASHA TASKS  Scope: {'all' if model.include_archived else 'active'}"
    if model.filter_string:
        title += f"  Filter: {model.filter_string}"
    title += "  (Tab: initiatives)"
    lines = [title, ""]
    lines.append(_table_line(
        ("STATE", "TASK", "REPOSITORY", "CHANGE", "HARNESS", "AGE"),
        model.width,
    ))
    visible = model.visible_rows
    for offset, row in enumerate(visible):
        summary = row.summary
        run = next(
            (
                item for item in row.task["runs"]
                if item["run_id"] == row.observation.run_id
            ),
            None,
        )
        absolute_index = model.scroll_offset + offset
        marker = ">" if absolute_index == model.selection else " "
        repository = Path(summary["repository"]["root"]).name or "/"
        line = _table_line((
            row.display_state, summary["slug"], repository,
            row.task["jj"]["change_id"] or "-",
            "-" if run is None else run["harness"],
            _age(row.observation.observed_at, model.now),
        ), max(0, model.width - 2))
        lines.append(f"{marker} {line}")
    if not model.filtered_rows:
        lines.append("No tasks match the current filter.")
    lines.append("")
    detail = model.detail
    if detail is not None:
        lines.extend([
            detail.slug,
            f"Run:        {detail.run_id or 'none'}"
            + ("" if detail.role is None else f" / {detail.role}"),
            f"Tmux:       {detail.tmux}",
            f"Evidence:   {detail.evidence}",
            f"Source:     {detail.source}",
            f"Observed:   {detail.observed_at or 'unknown'}",
            f"Freshness:  {detail.freshness}",
            f"Workspace:  {detail.workspace}",
            f"Change:     {detail.change}, last explicit refresh: "
            f"{detail.diff_refreshed_at or 'never'}",
            f"Blocker:    {detail.blocker or 'none'}",
        ])
        if detail.diff_summary is not None:
            diff_lines = detail.diff_summary.splitlines() or ["No changes."]
            lines.extend(f"Diff:       {line}" for line in diff_lines[:3])
    footer = "Enter inspect  x actions  A scope  n start  r reconcile  d diff  a archive  / filter  ? help  q quit"
    # Automatic failures are actionable and must not disappear below a long
    # task/detail body or operator message. Reserve their lines first, then the
    # ordinary status, truncating lower-priority body content as needed.
    error_lines = (
        _wrap_status(model.automatic_refresh_error, model.width)
        if model.automatic_refresh_error else []
    )
    message_lines = (
        _wrap_status(model.message, model.width) if model.message else []
    )
    available = max(0, model.height - 1)
    status_limit = min(_STATUS_MAX_LINES, available)
    all_status_lines = error_lines + message_lines
    status_lines = all_status_lines[:status_limit]
    if len(all_status_lines) > status_limit and status_lines:
        status_lines[-1] = _clip(
            status_lines[-1][:-1] + "…", model.width,
        )
    body_budget = max(0, available - len(status_lines))
    lines = lines[:body_budget] + status_lines
    # The footer is the operator's escape route. Preserve it even when the
    # terminal is shorter than the detail projection.
    if model.height:
        lines.append(footer)
    return [_clip(line, model.width) for line in lines]


_STATUS_MAX_LINES = 6
_INITIATIVE_FOOTER = (
    "Enter open  Right/Left expand  r reconcile  d diff  e events  a approval  "
    "c candidates  v verify  t storage  p pause/resume  s stop  / filter  Tab tasks  ? help  q quit"
)


def _initiative_table_line(values: tuple[str, str, str, str, str], width: int) -> str:
    # STATE, INITIATIVE, COORDINATOR, NODES, ATTENTION with four separators:
    # the name column absorbs the width so the attention column never clips.
    name_width = max(8, width - 47)
    widths = (10, name_width, 12, 5, 16)
    cells = [_clip(value, cell_width).ljust(cell_width) for value, cell_width in zip(values, widths)]
    return " ".join(cells).rstrip()


def _render_initiatives(model: TuiModel) -> list[str]:
    """Text-only Initiatives mode: table, fact detail, optional pane, footer."""
    screen = model.initiatives
    if model.help_visible:
        lines = [
            "ASHA CONTROL HELP  [Initiatives]",
            "",
            "Keys: Tab tasks | Up/Down select | Right/Left expand/collapse | Enter open worker task popup",
            "      r reconcile | d diff | e events | a approval decision | c candidate seals",
            "      v review+verification evidence | t retained storage | p pause/resume | s stop attempt",
            "      / filter | ? help | q quit",
            "",
            "Facts: claim, seal, review verdict, and verification outcome are shown separately.",
            "Approval: the operator decides here or in the CLI; the coordinator pane is refused.",
            "No merge, rebase, bookmark, push, publication, workspace removal, or deletion exists here.",
        ]
        return [_clip(line, model.width) for line in lines[:model.height]]
    title = "ASHA CONTROL  [Initiatives]"
    if screen is not None and screen.filter_string:
        title += f"  Filter: {screen.filter_string}"
    lines = [title, ""]
    if screen is None:
        lines.append(model.initiatives_error or "Initiatives unavailable.")
    else:
        lines.append(_initiative_table_line(
            ("STATE", "INITIATIVE", "COORDINATOR", "NODES", "ATTENTION"), model.width,
        ))
        visible = screen.visible_rows
        for offset, row in enumerate(visible):
            absolute_index = screen.scroll_offset + offset
            marker = ">" if absolute_index == screen.selection else " "
            indent = "  " * row.depth
            if row.kind == "initiative":
                cells = (row.state, indent + row.label, row.coordinator, row.nodes, row.attention)
            else:
                cells = (row.state, indent + f"{row.id}  {row.label}", row.type, "", "")
            lines.append(f"{marker} {_initiative_table_line(cells, max(0, model.width - 2))}")
        if not screen.rows():
            lines.append("No initiatives match the current filter.")
        lines.append("")
        lines.extend(screen.detail_lines())
        pane = screen.pane_lines()
        if pane:
            lines.append("")
            lines.append(f"[{screen.pane}]")
            lines.extend(pane)
    error_lines = _wrap_status(model.automatic_refresh_error, model.width) if model.automatic_refresh_error else []
    message_lines = _wrap_status(model.message, model.width) if model.message else []
    available = max(0, model.height - 1)
    status_limit = min(_STATUS_MAX_LINES, available)
    all_status_lines = error_lines + message_lines
    status_lines = all_status_lines[:status_limit]
    if len(all_status_lines) > status_limit and status_lines:
        status_lines[-1] = _clip(status_lines[-1][:-1] + "…", model.width)
    body_budget = max(0, available - len(status_lines))
    lines = lines[:body_budget] + status_lines
    if model.height:
        lines.append(_INITIATIVE_FOOTER)
    return [_clip(line, model.width) for line in lines]


def _load_initiative_views(env: Mapping[str, str], *, tmux=None) -> list[dict[str, Any]]:
    """Lock-free per-initiative bundles for Initiatives mode; orchestration is imported lazily."""
    from .orchestration.cli import snapshot as initiative_snapshot
    from .orchestration.config import load_config as load_orchestration_config
    from .orchestration.coordinator import anchor_liveness
    from .orchestration.model import COORDINATOR_LIVE_STATES
    from .orchestration.store import InitiativeStore

    config = load_orchestration_config(env)
    store = InitiativeStore(config)
    views: list[dict[str, Any]] = []
    adapter = tmux or TmuxAdapter()
    for initiative in store.list_initiatives():
        if initiative.get("state") == "archived":
            continue
        initiative_id = initiative["initiative_id"]
        current = initiative_snapshot(store, initiative)
        coordinator = current.get("coordinator")
        coordinator_live: bool | None = None
        if coordinator and coordinator.get("state") in COORDINATOR_LIVE_STATES:
            state, _detail = anchor_liveness(coordinator["anchor"], adapter)
            coordinator_live = None if state == "unknown" else state == "live"
        plans = store.list_plans_snapshot(initiative_id)
        views.append({
            "initiative": initiative,
            "plan": plans[-1] if plans else None,
            "nodes": current["nodes"],
            "attempts": current["attempts"],
            "links": current["links"],
            "events": store.list_events_snapshot(initiative_id)[-50:],
            "coordinator": coordinator,
            "coordinator_live": coordinator_live,
            "seals": store.list_seals_snapshot(initiative_id),
            "reviews": store.list_reviews_snapshot(initiative_id),
            "verifications": store.list_verifications_snapshot(initiative_id),
            "approvals": store.list_approvals_snapshot(initiative_id),
            "storage": None,
        })
    return views



def _enter_initiatives(model: TuiModel, env: Mapping[str, str]) -> None:
    """Switch to Initiatives mode; a failed orchestration load degrades this mode only."""
    try:
        _refresh_initiatives(model, env)
    except Exception as exc:  # noqa: BLE001 - degrade this mode only
        model.initiatives = None
        model.initiatives_error = f"initiatives unavailable: {_safe_error(exc)}"
    model.mode = "initiatives"


def _refresh_initiatives(model: TuiModel, env: Mapping[str, str], *, tmux=None) -> None:
    from .orchestration.tui_model import InitiativesScreen

    views = _load_initiative_views(env, tmux=tmux)
    if model.initiatives is None:
        model.initiatives = InitiativesScreen(views, height=model.height, width=model.width)
    else:
        model.initiatives.resize(model.height, model.width)
        model.initiatives.replace_views(views)
    model.initiatives_error = None


def _wrap_status(message: str, width: int) -> list[str]:
    """Wrap a status message under a `Status: ` label into bounded lines."""
    label = "Status: "
    usable = max(1, width - 1 - len(label))
    words = _safe_text(message).split(" ")
    wrapped: list[str] = []
    current = ""
    for word in words:
        while len(word) > usable:
            if current:
                wrapped.append(current)
                current = ""
            wrapped.append(word[:usable])
            word = word[usable:]
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= usable:
            current = candidate
        else:
            wrapped.append(current)
            current = word
    if current:
        wrapped.append(current)
    if len(wrapped) > _STATUS_MAX_LINES:
        wrapped = wrapped[:_STATUS_MAX_LINES]
        wrapped[-1] = wrapped[-1][:max(0, usable - 1)] + "…"
    return [
        (label if index == 0 else " " * len(label)) + line
        for index, line in enumerate(wrapped)
    ]


class _TuiShutdown(ControlTermination):
    def __init__(self, signum: int, detail: str | None = None) -> None:
        super().__init__(detail or signum)
        self.signum = signum
        self.detail = detail


def _safe_error(exc: BaseException) -> str:
    return _safe_text(exc)[:1200] or "controller failure"


def _adapter_for_task(task: dict[str, Any]) -> TmuxAdapter:
    socket = task["tmux"]["socket"]
    return TmuxAdapter(socket=None if socket == "default" else socket)


def _read_row(
    config: ControlConfig,
    store: TaskStore,
    journals: CreationJournalStore,
    listed: dict[str, Any],
    jj: JjAdapter,
    *,
    adapter: TmuxAdapter | None = None,
    sampled_at: datetime | None = None,
    publish_summary: bool = True,
) -> TuiRow:
    presentation = adapter or _adapter_for_task(listed)
    observed = sampled_at or datetime.now(timezone.utc)
    clock: Callable[[], datetime] = lambda: observed
    live = LiveAdapters(config=config, tmux=presentation, jj=jj, now=clock)
    task, reconciliation, observation = view.locked_reconciliation_observation(
        store, journals, listed["task_id"], live, jj,
        presentation=presentation, publish_summary=publish_summary,
        presentation_now=clock,
    )
    return TuiRow.from_records(task, reconciliation, observation)


def _load_rows(
    config: ControlConfig, store: TaskStore,
    journals: CreationJournalStore, jj: JjAdapter,
    *, include_archived: bool = False,
) -> list[TuiRow]:
    observed = datetime.now(timezone.utc)
    clock: Callable[[], datetime] = lambda: observed
    rows: list[TuiRow] = []
    summary_adapter: TmuxAdapter | None = None
    for listed in store.list():
        if listed["lifecycle"] == "archived":
            if include_archived:
                rows.append(lifecycle_row(listed))
            continue
        adapter = _adapter_for_task(listed)
        row = _read_row(
            config, store, journals, listed, jj, adapter=adapter,
            sampled_at=observed, publish_summary=False,
        )
        if row.task["lifecycle"] == "archived":
            if include_archived:
                rows.append(row)
            continue
        if summary_adapter is None:
            summary_adapter = adapter
        rows.append(row)
    view.publish_server_summary(
        config, summary_adapter or TmuxAdapter(), now=clock,
    )
    return rows


def _surface_skipped(model: TuiModel, store: TaskStore) -> None:
    if not store.skipped:
        return
    detail = f"{len(store.skipped)} registry entries skipped"
    model.message = f"{model.message}; {detail}" if model.message else detail


def _paint(stdscr, curses_module, model: TuiModel) -> None:
    height, width = stdscr.getmaxyx()
    model.resize(height, width)
    if model.initiatives is not None:
        model.initiatives.resize(height, width)
    stdscr.erase()
    for y, line in enumerate(render(model)):
        if y >= height or width <= 1:
            break
        try:
            stdscr.addnstr(y, 0, line, width - 1)
        except curses_module.error:
            pass
    stdscr.refresh()


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
        return ModalFrame((), None, 0, 0)
    bounded = _bounded_modal_candidates(candidates)
    if selected is not None and bounded:
        selected = min(max(0, int(selected)), len(bounded) - 1)
    else:
        selected = None

    prefix = f"{label}: " if label else ""
    viewport, cursor_x = _prompt_viewport(
        prefix if prompt is None else prompt,
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
            "… " + (prefix if prompt is None else prompt),
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
        for index in range(start, end):
            candidate = bounded[index]
            marker = "> " if index == selected else "  "
            shown = candidate.display_value or "(default)"
            if candidate.detail:
                shown += f"  {candidate.detail}"
            candidate_rows.append(_prefix_cells(marker + shown, budget))
        if start and candidate_rows:
            candidate_rows[0] = _prefix_cells("↑ " + candidate_rows[0], budget)
        if end < len(bounded) and candidate_rows:
            candidate_rows[-1] = _prefix_cells("↓ " + candidate_rows[-1], budget)
        rows = tuple((header + candidate_rows)[:height])
        return ModalFrame(rows, cursor, start, end)
    return ModalFrame(tuple(header), cursor, 0, 0)


def _dedupe_candidates(
    values: Iterable[tuple[str, str]],
) -> tuple[ModalCandidate, ...]:
    result: list[ModalCandidate] = []
    seen: set[str] = set()
    for value, detail in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(ModalCandidate(value, detail))
    return _bounded_modal_candidates(result)


def freeze_start_candidates(
    config: ControlConfig,
    store: TaskStore,
    *,
    cwd: Path | None = None,
    executable: Callable[[str], str | None] = shutil.which,
) -> StartCandidateSnapshot:
    """Freeze bounded convenience candidates from one task registry snapshot."""
    current = (cwd or Path.cwd()).resolve()
    records = store.list()
    repository_latest: dict[str, str] = {}
    base_latest_by_repository: dict[str, dict[str, str]] = {}
    role_latest: dict[str, str] = {}
    for item in records:
        root = item["repository"]["root"]
        repository_latest[root] = max(
            repository_latest.get(root, ""), item.get("updated_at", ""),
        )
        value = item["jj"].get("requested_base")
        if isinstance(value, str) and not value.startswith("PR #"):
            bases = base_latest_by_repository.setdefault(root, {})
            bases[value] = max(bases.get(value, ""), item.get("updated_at", ""))
        for run in item["runs"]:
            role = run.get("role")
            if isinstance(role, str) and role != "implementer":
                role_latest[role] = max(
                    role_latest.get(role, ""), item.get("updated_at", ""),
                )
    repository_order = sorted(repository_latest)
    repository_order.sort(key=lambda item: repository_latest[item], reverse=True)

    count = 0
    used_bytes = 0

    def admit(candidate: ModalCandidate) -> ModalCandidate | None:
        nonlocal count, used_bytes
        bounded = _bounded_modal_candidates((candidate,))
        if not bounded:
            return None
        prepared = bounded[0]
        size = (
            len(prepared.value.encode("utf-8")) +
            len(prepared.display_value.encode("utf-8")) +
            len(prepared.detail.encode("utf-8"))
        )
        if count >= _MAX_MODAL_CANDIDATES or used_bytes + size > _MAX_CANDIDATE_BYTES:
            return None
        count += 1
        used_bytes += size
        return prepared

    harness_names = [config.default_harness] + sorted(
        HARNESSES - {config.default_harness},
    )
    harnesses = tuple(filter(None, (
        admit(ModalCandidate(
            name,
            "installed" if executable(name) is not None else "not installed",
        ))
        for name in harness_names
    )))
    roles: list[str] = []
    implementer = admit(ModalCandidate("implementer", "default role"))
    if implementer is not None:
        roles.append(implementer.value)

    repository_inputs = [(str(current), "current directory")]
    repository_inputs.extend(
        (root, f"last used {repository_latest[root]}")
        for root in repository_order if root != str(current)
    )
    repositories_list: list[ModalCandidate] = []
    by_repository: dict[str, tuple[ModalCandidate, ...]] = {}
    seen_repositories: set[str] = set()
    for root, detail in repository_inputs:
        if root in seen_repositories:
            continue
        seen_repositories.add(root)
        repository = admit(ModalCandidate(root, detail))
        if repository is None:
            continue
        default = admit(ModalCandidate("", "resolve after repository selection"))
        if default is None:
            # Do not expose a repository whose required default base candidate
            # was not included in the aggregate frozen snapshot.
            count -= 1
            used_bytes -= (
                len(repository.value.encode("utf-8")) +
                len(repository.display_value.encode("utf-8")) +
                len(repository.detail.encode("utf-8"))
            )
            continue
        repositories_list.append(repository)
        bases_for_repository = [default]
        base_latest = base_latest_by_repository.get(root, {})
        stored = sorted(base_latest)
        stored.sort(key=lambda item: base_latest[item], reverse=True)
        for value in stored:
            if value in {"", DEFAULT_BASE_REVSET}:
                continue
            candidate = admit(ModalCandidate(value, "recorded"))
            if candidate is None:
                break
            bases_for_repository.append(candidate)
        by_repository[root] = tuple(bases_for_repository)

    ordered_roles = sorted(role_latest)
    ordered_roles.sort(key=lambda item: role_latest[item], reverse=True)
    for role in ordered_roles:
        candidate = admit(ModalCandidate(role, "recorded role"))
        if candidate is None:
            break
        roles.append(candidate.value)
    return StartCandidateSnapshot(
        tuple(repositories_list), by_repository, harnesses, tuple(roles),
    )


def _default_base_candidate(
    repository: str | Path, *, jj: JjAdapter | None = None,
) -> tuple[ModalCandidate, str | None]:
    """Build bounded advisory default detail and its non-authoritative OID."""
    try:
        candidate = Path(repository).expanduser()
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = candidate.resolve()
        root = discover_git_root(candidate)
        if root is None:
            raise JjError("no Git repository was found")
        resolution = (jj or JjAdapter()).resolve_default_base(root)
        tier = {
            "attached-local": "current",
            "remote-head": "remote default",
            "conventional-local": "local fallback",
        }.get(resolution.tier, "default")
        references = ", ".join(resolution.references)
        detail = f"{tier} {references} @ {resolution.commit_id[:12]}"
        bounded = _bounded_modal_candidates((ModalCandidate("", detail),))
        return bounded[0], resolution.commit_id
    except (OSError, ValueError, JjError) as exc:
        detail = _safe_text(str(exc))
        if len(detail) > 180:
            detail = detail[:177] + "..."
        candidate = ModalCandidate(
            "", f"unavailable; select/type explicit base: {detail}",
        )
        bounded = _bounded_modal_candidates((candidate,))
        return (bounded[0] if bounded else ModalCandidate("", "unavailable")), None


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


def _prompt_line(
    stdscr, curses_module, model: TuiModel, prompt: str,
    *, initial: str = "", maximum: int = 500, hint: str | None = None,
    candidates: Iterable[ModalCandidate] = (), selected: int | None = None,
    title: str = "", context: str = "",
) -> str | None:
    value = list(initial[:maximum])
    bounded_candidates = _bounded_modal_candidates(candidates)
    candidate_selection = selected
    while True:
        _paint(stdscr, curses_module, model)
        height, width = stdscr.getmaxyx()
        if height:
            frame = modal_frame(
                title=title, context=context, label="", hint=hint or "",
                value="".join(value), candidates=bounded_candidates,
                selected=candidate_selection, height=height, width=width,
                prompt=prompt,
            )
            try:
                start = max(0, height - len(frame.rows))
                for offset, line in enumerate(frame.rows):
                    y = start + offset
                    stdscr.move(y, 0)
                    stdscr.clrtoeol()
                    if line and width > 1:
                        stdscr.addnstr(y, 0, line, len(line))
                if frame.cursor is not None:
                    cursor_y, cursor_x = frame.cursor
                    stdscr.move(start + cursor_y, cursor_x)
                stdscr.refresh()
            except curses_module.error:
                pass
        key = _read_modal_key(stdscr, curses_module)
        if key == -1:
            continue
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


def _open_popup(
    stdscr,
    curses_module,
    config: ControlConfig,
    row: TuiRow,
    run_id: str | None,
    env: Mapping[str, str],
) -> str | None:
    adapter = _adapter_for_task(row.task)
    target = view.attach_target(row.task, run_id, adapter=adapter)
    adapter.select_target(target.session, target.window, target.pane_id)
    curses_module.endwin()
    try:
        # Imported lazily to avoid a module cycle while the CLI routes into this driver.
        from .cli import _run_popup
        return _run_popup(
            adapter, config, target.session, row.task["slug"], env,
        )
    finally:
        _repaint_after_suspend(stdscr)


def _start_worker_argv(arguments: list[str], env: Mapping[str, str]) -> list[str]:
    raw_root = env.get("ASHA_ROOT")
    root = Path(__file__).resolve().parents[2] if raw_root is None else Path(raw_root)
    if not root.is_absolute() or root.resolve() != root:
        raise ValueError("ASHA_ROOT must be an exact canonical absolute path")
    # This is the same isolated bootstrap as lib/control.sh, executed directly
    # as one Python argv. It neither trusts cwd/PYTHONPATH nor creates a shell
    # process between the TUI and the signal-owned worker group.
    return [
        sys.executable, "-B", "-I", "-c",
        (
            "import runpy,sys; sys.path.insert(0, sys.argv.pop(1)); "
            'runpy.run_module("control.cli", run_name="__main__")'
        ),
        str(root / "lib"), "task", "start", *arguments,
        "--tui-worker",
    ]


def _start_progress(stdscr, curses_module, model: TuiModel, *, cancelling: bool) -> None:
    previous = model.message
    model.message = (
        "Esc cancel requested; waiting for durable recovery..."
        if cancelling else
        "Preparing task in isolated worker... Esc cancel"
    )
    try:
        _paint(stdscr, curses_module, model)
    finally:
        model.message = previous


def _read_worker_state(
    config: ControlConfig, task_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    tasks = TaskStore(config)
    journals = CreationJournalStore(config)
    task: dict[str, Any] | None = None
    journal: dict[str, Any] | None = None
    with tasks.transaction_lock(task_id):
        try:
            task = tasks.read(task_id)
        except StoreError as exc:
            if str(exc) != f"task not found: {task_id}":
                raise
        try:
            journal = journals.read(task_id)
        except JournalError as exc:
            if str(exc) != f"creation journal not found: {task_id}":
                raise
    return task, journal


def _successful_worker_message(stdout: bytes, task_id: str) -> str:
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("task start worker returned invalid JSON") from exc
    if (not isinstance(payload, dict) or
            payload.get("contract") != "asha.control-task-start.v1"):
        raise ValueError("task start worker returned the wrong JSON contract")
    task = payload.get("task")
    if (not isinstance(task, dict) or task.get("task_id") != task_id or
            not isinstance(task.get("slug"), str)):
        raise ValueError("task start worker returned a mismatched task identity")
    task_jj = task.get("jj")
    base_commit_id = (
        task_jj.get("base_commit_id") if isinstance(task_jj, dict) else None
    )
    if (
        not isinstance(base_commit_id, str)
        or GIT_OBJECT_ID_PATTERN.fullmatch(base_commit_id) is None
    ):
        raise ValueError("task start worker returned an invalid base commit")
    mutations = payload.get("source_mutations")
    enabled = isinstance(mutations, list) and any(
        isinstance(item, dict) and item.get("operation") == "git init --colocate"
        for item in mutations
    )
    suffix = "; repository jj-enabled and retained" if enabled else ""
    return (
        f"task started: {task['slug']} ({task_id}); base {base_commit_id}{suffix}"
    )


def _classify_start_worker_exit(
    config: ControlConfig,
    task_id: str,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
    *,
    cancelled: bool,
    output_truncated: bool = False,
    source: Path | None = None,
    source_was_plain_git: bool = False,
) -> str:
    """Classify cancellation only from the durable creation boundary."""
    task, journal = _read_worker_state(config, task_id)

    # A recorded normal completion wins over a late Escape already read from
    # the terminal input buffer.
    if returncode == 0:
        if output_truncated:
            raise ValueError("task start worker output exceeded the bounded capture limit")
        return _successful_worker_message(stdout, task_id)

    phase = None if journal is None else journal.get("phase")
    launch_attempted = bool(
        journal is not None and journal.get("launch_attempted") is True
    )
    if launch_attempted or phase in {"launch-attempted", "run-recorded"}:
        source_note = (
            "; jj repository enablement retained"
            if source_was_plain_git else ""
        )
        return (
            f"task launch may have occurred; resources preserved{source_note}; "
            f"attach with `asha task attach {task_id}`; inspect/recover with "
            f"`asha task recover {task_id}`"
        )
    if phase == "preserved":
        outcome = "task start cancelled" if cancelled else "task start failed"
        source_note = (
            "; jj repository enablement retained"
            if source_was_plain_git else ""
        )
        return (
            f"{outcome}; workspace retained for safety{source_note}; "
            f"{v2_retention_diagnostic(task_id, journal)}"
        )
    if cancelled and source_was_plain_git:
        location = "the selected repository" if source is None else str(source)
        return (
            "task start cancelled; jj repository enablement may be partial and "
            f"was retained at {location}; run `jj status` there and inspect "
            "Git status/refs before retrying"
        )
    if cancelled and (journal is None or phase == "rolled-back"):
        return "task start cancelled"
    if cancelled:
        return (
            f"task start was interrupted during {phase or 'unknown'} recovery; "
            f"run `asha task recover {task_id}`"
        )

    if (
        not output_truncated and task is None and journal is None
        and returncode == 2 and stdout
    ):
        try:
            offer = decode_worker_refusal(stdout, task_id)
        except ValueError:
            pass
        else:
            raise StartPrerequisiteRefusal(offer, task_id=task_id, tui_worker=True)

    detail = stderr.decode("utf-8", errors="replace").strip()
    if output_truncated:
        detail = (detail + "; " if detail else "") + "worker output was truncated"
    if task is not None or journal is not None:
        recovery = f"; inspect with `asha task recover {task_id}`"
    else:
        recovery = ""
    raise ValueError(
        f"task start exited with status {returncode}: {detail or 'no diagnostic'}{recovery}"
    )


def _supervise_start_process(
    stdscr,
    curses_module,
    model: TuiModel,
    config: ControlConfig,
    argv: list[str],
    task_id: str,
    env: Mapping[str, str],
    *,
    source: Path | None = None,
    source_was_plain_git: bool = False,
) -> str:
    """Run one isolated task-start worker while curses polls for Escape."""
    _reap_deferred_start_workers()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
            env=dict(env),
        )
    except OSError as exc:
        raise ValueError(f"task start worker could not be invoked: {exc}") from exc
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    output_truncated = False
    cancellation_attempted = False
    cancellation_sent = False

    def drain(timeout: float = 0.0) -> None:
        nonlocal output_truncated
        for key, _mask in selector.select(timeout):
            try:
                chunk = os.read(key.fileobj.fileno(), 65536)
            except OSError:
                chunk = b""
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            target = captured[key.data]
            remaining = max(0, _START_OUTPUT_BYTES - len(target))
            target.extend(chunk[:remaining])
            if len(chunk) > remaining:
                output_truncated = True

    def close_pipes() -> None:
        for key in tuple(selector.get_map().values()):
            try:
                selector.unregister(key.fileobj)
            except (KeyError, ValueError):
                pass
        process.stdout.close()
        process.stderr.close()

    def drain_after_exit() -> None:
        deadline = time.monotonic() + _START_DRAIN_SECONDS
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            drain(min(0.05, remaining))
        close_pipes()

    try:
        try:
            stdscr.timeout(_INPUT_POLL_MS)
        except AttributeError:
            pass
        while True:
            drain()
            returncode = process.poll()
            if returncode is not None:
                break
            _start_progress(
                stdscr, curses_module, model, cancelling=cancellation_sent,
            )
            key = _read_modal_key(stdscr, curses_module)
            # Completion wins if it occurred while getch was waiting, even if
            # that read returned a late buffered Escape.
            returncode = process.poll()
            if returncode is not None:
                break
            if key == 27 and not cancellation_attempted:
                cancellation_attempted = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                else:
                    cancellation_sent = True
        process.wait(timeout=_START_CLEANUP_SECONDS)
        drain_after_exit()
        return _classify_start_worker_exit(
            config, task_id, process.returncode,
            bytes(captured["stdout"]), bytes(captured["stderr"]),
            cancelled=cancellation_sent, output_truncated=output_truncated,
            source=source, source_was_plain_git=source_was_plain_git,
        )
    except BaseException as exc:
        leader_reaped = process.poll() is not None
        if process.poll() is None:
            if not cancellation_attempted:
                cancellation_attempted = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=_START_CLEANUP_SECONDS)
            except subprocess.TimeoutExpired:
                leader_reaped = False
            else:
                leader_reaped = True
        if leader_reaped:
            drain_after_exit()
        else:
            _DEFERRED_START_WORKERS.append(process)
            close_pipes()
            if isinstance(exc, _TuiShutdown):
                raise _TuiShutdown(
                    exc.signum,
                    "task start supervisor shutdown left owned worker "
                    "termination unconfirmed; state may still be changing. "
                    f"Do not retry; inspect `asha task recover {task_id}` and "
                    "the selected repository before acting",
                ) from exc
            raise ValueError(
                "task start supervisor failed and owned worker termination "
                f"unconfirmed; state may still be changing. Do not retry; "
                f"inspect `asha task recover {task_id}` and the selected "
                "repository before acting"
            ) from exc
        raise
    finally:
        if not process.stdout.closed or not process.stderr.closed:
            close_pipes()
        selector.close()


def _reap_deferred_start_workers() -> None:
    """Non-blockingly reap leaders whose one-shot cleanup deadline elapsed."""
    retained: list[subprocess.Popen] = []
    for process in _DEFERRED_START_WORKERS:
        if process.poll() is None:
            retained.append(process)
    _DEFERRED_START_WORKERS[:] = retained


def _source_colocation_watch(
    value: str, config: ControlConfig,
) -> tuple[Path | None, bool]:
    if value == "~":
        candidate = config.home
    elif value.startswith("~/"):
        candidate = config.home / value[2:]
    else:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
    try:
        candidate = candidate.resolve()
        git_root = discover_git_root(candidate)
        if git_root is None:
            return candidate, False
        try:
            (git_root / ".jj").lstat()
        except FileNotFoundError:
            return git_root, True
        return git_root, False
    except (JjError, OSError):
        return None, False


_START_FIELDS = ("Repo", "Base", "Harness", "Role", "Goal")
_START_MAXIMUMS = (4096, 500, 32, 64, 200)


def _ascii_prefix(candidate: str, prefix: str) -> bool:
    try:
        return candidate.encode("ascii").lower().startswith(
            prefix.encode("ascii").lower(),
        )
    except UnicodeEncodeError:
        return False


def _draw_modal_frame(stdscr, curses_module, frame: ModalFrame) -> None:
    height, width = stdscr.getmaxyx()
    stdscr.erase()
    if width > 1:
        for y, row in enumerate(frame.rows[:height]):
            try:
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


def _start_field_candidates(
    snapshot: StartCandidateSnapshot, values: list[str], field: int,
) -> tuple[ModalCandidate, ...]:
    if field == 0:
        return snapshot.repositories
    if field == 1:
        return snapshot.bases_for(values[0])
    if field == 2:
        return snapshot.harnesses
    if field == 3:
        return tuple(ModalCandidate(item, "recorded role") for item in snapshot.roles)
    return ()


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


def _prerequisite_action_modal(
    stdscr, curses_module, model: TuiModel, offer: StartPrerequisiteOffer,
) -> str:
    """Return an explicit repair action; instructions and Escape never mutate."""
    candidates = (
        ModalCandidate("Apply patch", "patch only the source working tree"),
        ModalCandidate("Show instructions", "show commit/select guidance"),
        ModalCandidate("Cancel", "return to the filled start form"),
    )
    selected = 2
    instruction_note = ""
    references = (
        offer.requested_base if offer.default_base_resolution is None else
        ", ".join(offer.default_base_resolution.references)
    )
    while True:
        height, width = stdscr.getmaxyx()
        frame = modal_frame(
            title="Control prerequisite missing",
            context=(
                f"Repository: {offer.root}\n"
                f"Selected base: {references} @ {offer.base_commit_id}\n"
                f"File: {offer.target}\nAdd: {offer.rules[0]}\n"
                "Effect: patches only the source working tree. It does not "
                "commit, move a ref, retry task creation, enable jj, import "
                "Git, or authorize the old base."
                f"{instruction_note}"
            ),
            label="Action", hint="Enter selects; Esc returns to form",
            value=candidates[selected].value, candidates=candidates,
            selected=selected, height=height, width=width,
        )
        _draw_modal_frame(stdscr, curses_module, frame)
        key = _read_modal_key(stdscr, curses_module)
        if key == -1:
            continue
        if key == 27:
            return "cancel"
        if key == getattr(curses_module, "KEY_RESIZE", -998):
            continue
        if key in {
            getattr(curses_module, "KEY_UP", -997),
            getattr(curses_module, "KEY_DOWN", -996),
        }:
            selected = min(max(
                0, selected + (-1 if key == getattr(curses_module, "KEY_UP", -997) else 1),
            ), len(candidates) - 1)
            continue
        if key in {10, 13, getattr(curses_module, "KEY_ENTER", -995)}:
            if selected == 0:
                return "apply"
            if selected == 1:
                instruction_note = (
                    f"\nInstructions: add {offer.rules[0]} to {offer.target}; "
                    "commit it on the branch or select a containing commit, then "
                    f"retry. The old base {offer.base_commit_id} remains unauthorized."
                )
                continue
            return "cancel"


def _start_form(
    stdscr, curses_module, model: TuiModel,
    env: Mapping[str, str], config: ControlConfig, *,
    _retained_values: tuple[str, str, str, str, str] | None = None,
) -> str:
    snapshot = freeze_start_candidates(config, TaskStore(config))
    values = list(_retained_values) if _retained_values is not None else [
        str(Path.cwd().resolve()), "", config.default_harness, "implementer", "",
    ]
    accepted_repository = values[0]
    expected_default_commit_id: str | None = None
    field = 1 if _retained_values is not None else 0
    selected: int | None = None
    form_notice = ""

    def set_form_notice(message: str) -> None:
        nonlocal form_notice
        form_notice = _safe_text(message)[:1200]
        model.message = form_notice

    def install_default_preview(
        repository: str, candidate: ModalCandidate, commit_id: str | None,
    ) -> None:
        nonlocal expected_default_commit_id, snapshot, selected
        expected_default_commit_id = commit_id
        bases = dict(snapshot.bases)
        explicit_candidates = tuple(
            item for item in bases.get(repository, ()) if item.value
        )
        bases[repository] = (candidate, *explicit_candidates)
        snapshot = StartCandidateSnapshot(
            snapshot.repositories, bases, snapshot.harnesses, snapshot.roles,
        )
        selected = None

    def refresh_default_preview(repository: str) -> None:
        candidate, commit_id = _default_base_candidate(repository)
        install_default_preview(repository, candidate, commit_id)

    if _retained_values is not None:
        refresh_default_preview(accepted_repository)

    while True:
        if field >= len(_START_FIELDS):
            repo, base, harness, role, goal = values
            arguments = ["--repo", repo]
            if base.strip():
                arguments.extend(["--base", base.strip()])
            elif expected_default_commit_id is not None:
                arguments.extend(["--expected-default", expected_default_commit_id])
            task_id = new_uuid()
            arguments.extend([
                "--harness", harness, "--role", role, "--goal", goal,
                "--task-id", task_id, "--detach", "--json",
            ])
            source, source_was_plain_git = _source_colocation_watch(repo, config)
            try:
                return _supervise_start_process(
                    stdscr, curses_module, model, config,
                    _start_worker_argv(arguments, env), task_id, env,
                    source=source, source_was_plain_git=source_was_plain_git,
                )
            except StartPrerequisiteRefusal as refusal:
                action = _prerequisite_action_modal(
                    stdscr, curses_module, model, refusal.offer,
                )
                if action == "apply":
                    try:
                        set_form_notice(apply_ignore_prerequisite(
                            config, refusal.offer,
                        ))
                    except PrerequisiteApplyIndeterminate as exc:
                        set_form_notice(
                            "INDETERMINATE: inspect .gitignore before retrying. "
                            f"{exc}"
                        )
                    except ValueError as exc:
                        set_form_notice(
                            f"Prerequisite repair refused: {exc}. "
                            "Start form values retained."
                        )
                else:
                    set_form_notice(
                        "Prerequisite repair cancelled; start form values retained."
                    )
                # No action retries. The same frame returns to Base with all
                # five logical values and a freshly inspected default.
                field = 1
                refresh_default_preview(accepted_repository)
                continue
            except ValueError as exc:
                # A worker-side revalidation refusal is not repair eligibility
                # and stderr is never decoded as such. Keep the draft in this
                # form and return to Base so the operator can review it.
                set_form_notice(
                    f"Task start refused: {exc}. Form values retained."
                )
                field = 1
                refresh_default_preview(accepted_repository)
                continue

        candidates = _start_field_candidates(snapshot, values, field)
        height, width = stdscr.getmaxyx()
        frame = modal_frame(
            title="Start task",
            context=(
                f"Field {field + 1}/5  Up/Down select  Tab complete  "
                "Shift-Tab back  Esc cancel"
                + (f"\nNotice: {form_notice}" if form_notice else "")
            ),
            label=_START_FIELDS[field], hint="Enter accepts",
            value=values[field], candidates=candidates, selected=selected,
            height=height, width=width,
        )
        _draw_modal_frame(stdscr, curses_module, frame)
        key = _read_modal_key(stdscr, curses_module)
        if key == -1:
            continue
        if key == getattr(curses_module, "KEY_RESIZE", -998):
            if field == 1:
                refresh_default_preview(accepted_repository)
            continue
        # The notice was rendered in the frame above. A real subsequent key is
        # the explicit acknowledgement boundary; any new outcome below installs
        # its own notice for the next frame.
        form_notice = ""
        if key == 27:
            return "task start cancelled"
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
        if key == 9 and candidates:
            matches = [
                index for index, item in enumerate(candidates)
                if (
                    _ascii_prefix(item.value, values[field])
                    if field in {2, 3} else
                    item.value.startswith(values[field])
                )
            ]
            if matches:
                selected = matches[0] if selected not in matches else selected
                values[field] = candidates[selected].value
            continue
        if key in {10, 13, getattr(curses_module, "KEY_ENTER", -995)}:
            accepted = (
                candidates[selected].value if selected is not None and candidates
                else values[field]
            )
            canonical = _canonical_field_value(field, accepted, candidates)
            if field == 4 and not terminal_text_is_complete(accepted):
                canonical = None
            if canonical is None:
                model.message = (
                    "Harness must be one installed-status candidate from the closed allowlist."
                    if field == 2 else
                    "Role uses an invalid restricted grammar."
                    if field == 3 else
                    "Goal ends with an unsupported Unicode cluster."
                )
                selected = None
                continue
            if field == 1 and not canonical.strip():
                observed_candidate, observed_commit_id = _default_base_candidate(
                    accepted_repository,
                )
                if observed_commit_id is None:
                    install_default_preview(
                        accepted_repository, observed_candidate, None,
                    )
                    set_form_notice(
                        "Default base preview is unavailable. Select or type an "
                        "explicit Base before continuing."
                    )
                    selected = None
                    continue
                if observed_commit_id != expected_default_commit_id:
                    previous = expected_default_commit_id or "unavailable"
                    install_default_preview(
                        accepted_repository, observed_candidate,
                        observed_commit_id,
                    )
                    set_form_notice(
                        f"Default base changed from {previous} to "
                        f"{observed_commit_id}; review it and press Enter again."
                    )
                    selected = None
                    continue
            if field == 0:
                if canonical != accepted_repository:
                    values[1] = ""
                accepted_repository = canonical
                refresh_default_preview(canonical)
            elif field == 1:
                model.message = ""
            values[field] = canonical
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
                len(values[field]) < _START_MAXIMUMS[field]:
            character = key if isinstance(key, str) else chr(key)
            logical = list(values[field])
            if _prompt_character_allowed(logical, character):
                values[field] += character
                selected = None
def _retry_arguments(task: dict[str, Any], task_id: str) -> list[str]:
    """Reconstruct one recorded task request under a fresh path identity."""
    arguments = ["--repo", task["repository"]["root"]]
    source = task["source"]
    if source["kind"] in {"pr", "issue"}:
        arguments.extend([f"--{source['kind']}", str(source["number"])])
    # PR start resolves its current head and owns its generated requested-base
    # description; the public CLI intentionally refuses --pr plus --base.
    if (
        source["kind"] != "pr"
        and task["jj"]["requested_base"] != DEFAULT_BASE_REVSET
    ):
        arguments.extend(["--base", task["jj"]["requested_base"]])
    if not task["runs"]:
        raise ValueError("task without a recorded primary run cannot be retried")
    primary = task["runs"][0]
    arguments.extend([
        "--harness", primary["harness"], "--role", primary["role"],
        "--goal", task["label"], "--slug", retry_task_slug(task["slug"], task_id),
        "--task-id", task_id, "--detach", "--json",
    ])
    return arguments


def _lookup_task_binding(
    env: Mapping[str, str], task_id: str, store: TaskStore,
):
    """Resolve durable orchestration ownership once, without a grace wait."""
    from .orchestration.config import load_config as load_orchestration_config
    from .orchestration.results import locate_task_binding_now

    orchestration = load_orchestration_config(env)
    return locate_task_binding_now(
        orchestration, task_id, control_store=store,
    )


def _archived_resources_remain(config: ControlConfig, task: dict[str, Any]) -> bool:
    record = PruneRecordStore(config).read(task["task_id"])
    if record is not None and record.get("workspace_removed"):
        return False
    try:
        return _adapter_for_task(task).has_session(task["tmux"]["session"])
    except (TmuxError, OSError, ValueError):
        return False


def _replace_loaded_rows(
    model: TuiModel, config: ControlConfig, store: TaskStore,
    journals: CreationJournalStore, jj: JjAdapter,
) -> None:
    model.replace_rows(_load_rows(
        config, store, journals, jj,
        include_archived=model.include_archived,
    ))
    _surface_skipped(model, store)


def _retry_task(
    *,
    stdscr, curses_module, model: TuiModel, config: ControlConfig,
    env: Mapping[str, str], store: TaskStore, journals: CreationJournalStore,
    jj: JjAdapter, task_id: str,
) -> str:
    def revalidate() -> dict[str, Any]:
        selected = store.read(task_id)
        if selected["lifecycle"] not in {"ended", "failed", "archived"}:
            raise ValueError("only a terminal task can be retried")
        if selected["lifecycle"] == "failed" and any(
            run["state"] not in {"exited", "failed"} for run in selected["runs"]
        ):
            raise ValueError(
                "failed task still records a preserved live run and cannot be retried"
            )
        if _lookup_task_binding(env, task_id, store) is not None:
            raise ValueError(
                "initiative-owned tasks cannot use ordinary retry; reconcile the initiative"
            )
        return selected

    current = revalidate()
    source_note = (
        " This reconstructs the recorded PR request and resolves its current PR head."
        if current["source"]["kind"] == "pr" else
        " This reconstructs the recorded task request."
    )
    answer = _prompt_line(
        stdscr, curses_module, model,
        "Confirm [yes/N]: ",
        title="Retry task",
        context=(
            f"Task: {current['slug']} ({current['task_id']})\n"
            f"Action: create a distinct task.{source_note}\n"
            "Authorization: type exact yes (lowercase)."
        ),
        maximum=4,
    )
    if answer != "yes":
        return "retry cancelled"
    current = revalidate()
    fresh_id = new_uuid()
    arguments = _retry_arguments(current, fresh_id)
    source, plain_git = _source_colocation_watch(
        current["repository"]["root"], config,
    )
    message = _supervise_start_process(
        stdscr, curses_module, model, config,
        _start_worker_argv(arguments, env), fresh_id, env,
        source=source, source_was_plain_git=plain_git,
    )
    _replace_loaded_rows(model, config, store, journals, jj)
    if message.startswith("task started"):
        model.select_task(fresh_id)
    return message


def _prune_archived_task(
    *,
    stdscr, curses_module, model: TuiModel, config: ControlConfig,
    env: Mapping[str, str], store: TaskStore, journals: CreationJournalStore,
    jj: JjAdapter, task_id: str,
) -> str:
    current = store.read(task_id)
    preview_context = assemble_prune_context(
        config, tasks=store, env=dict(env), remove_workspace=True,
    )
    preview = prune_one_task(
        config, current, tasks=store, journals=journals, jj=jj,
        remove_workspace=True, dry_run=True, context=preview_context,
    )
    if preview.outcome in {"refused", "partial"}:
        return (
            f"prune {preview.outcome}: session {preview.session.action} "
            f"({preview.session.detail}); workspace {preview.workspace.action} "
            f"({preview.workspace.detail})"
        )
    answer = _prompt_line(
        stdscr, curses_module, model,
        "Confirm [yes/N]: ",
        title="Prune archived task",
        context=(
            f"Task: {current['slug']} ({current['task_id']})\n"
            f"Preview: session {preview.session.action}; "
            f"workspace {preview.workspace.action}.\n"
            "Preservation: task record, change, links, and seals remain.\n"
            "Authorization: type exact yes (lowercase)."
        ),
        maximum=4,
    )
    if answer != "yes":
        return "prune cancelled"
    # Reassemble every external binding and path-owner input. The destructive
    # controller also re-reads the task under its task lock.
    real_context = assemble_prune_context(
        config, tasks=store, env=dict(env), remove_workspace=True,
    )
    result = prune_one_task(
        config, current, tasks=store, journals=journals, jj=jj,
        remove_workspace=True, dry_run=False, context=real_context,
    )
    _replace_loaded_rows(model, config, store, journals, jj)
    model.select_task(task_id)
    return (
        f"prune {result.outcome}: session {result.session.action} "
        f"({result.session.detail}); workspace {result.workspace.action} "
        f"({result.workspace.detail})"
    )


def _reconcile_task_initiative(
    *, env: Mapping[str, str], store: TaskStore, task_id: str,
) -> str:
    # Re-read both Control identity and durable link immediately before the
    # mutating reconciliation sequence.
    store.read(task_id)
    binding = _lookup_task_binding(env, task_id, store)
    if binding is None:
        raise ValueError("task is not owned by an initiative")
    initiative_store, initiative, link, attempt, _node, _task = binding
    from .orchestration.cli import reconcile_one_initiative

    result = reconcile_one_initiative(
        initiative_store, initiative["initiative_id"],
    )
    after_node = initiative_store.read_node(
        initiative["initiative_id"], link["node_id"],
    )
    live = result.get("live_reconciliation", {})
    retries = live.get("retries", []) if isinstance(live, dict) else []
    allocated = sum(
        1 for item in retries
        if isinstance(item, dict) and item.get("state") == "allocated"
    )
    ready = 1 if after_node.get("state") == "ready" else 0
    action_reconciliation = result.get("action_reconciliation", {})
    reconciled_actions = (
        action_reconciliation.get("actions", [])
        if isinstance(action_reconciliation, dict) else []
    )
    action_evidence = ", ".join(
        f"{item.get('action_class', '?')}:{item.get('action_id', '?')}:"
        f"{item.get('state', '?')}"
        for item in reconciled_actions if isinstance(item, dict)
    ) or "no pending action records"
    return (
        f"initiative {initiative.get('slug', initiative['initiative_id'])} reconciled; "
        f"node {link['node_id']}; attempt {link['attempt_id']} was "
        f"{attempt['state']}; allocated {allocated}; ready {ready}; "
        f"action evidence: {action_evidence}"
    )


_UNOWNED_SIGNAL_ACTIONS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "starting": (("I", "Interrupt", False), ("t", "Terminate", True)),
    "working": (("I", "Interrupt", False), ("t", "Terminate", True)),
    "needs-input": (("I", "Interrupt", False), ("t", "Terminate", True)),
    "idle": (("f", "Finish", True),),
    "unknown": (("t", "Terminate", True),),
}


def _fresh_active_row(
    *, model: TuiModel, config: ControlConfig, store: TaskStore,
    journals: CreationJournalStore, jj: JjAdapter, task_id: str,
) -> TuiRow:
    current = store.read(task_id)
    row = _read_row(config, store, journals, current, jj)
    model.replace_row(row)
    return row


def _signal_task_action(
    *, key: str, expected_run_id: str, stdscr, curses_module,
    model: TuiModel, config: ControlConfig, env: Mapping[str, str],
    store: TaskStore, journals: CreationJournalStore, jj: JjAdapter,
    task_id: str,
) -> str:
    row = _fresh_active_row(
        model=model, config=config, store=store, journals=journals, jj=jj,
        task_id=task_id,
    )
    if _lookup_task_binding(env, task_id, store) is not None:
        raise ValueError(
            "initiative ownership changed; use Stop attempt via initiative"
        )
    action = next(
        (item for item in _UNOWNED_SIGNAL_ACTIONS.get(row.display_state, ())
         if item[0] == key),
        None,
    )
    if action is None or row.observation.run_id != expected_run_id:
        raise ValueError("task run/state changed before signal confirmation")
    _key, label, terminate = action
    signal_name = "SIGTERM" if terminate else "SIGINT"
    answer = _prompt_line(
        stdscr, curses_module, model,
        "Confirm [yes/N]: ",
        title=f"{label} task",
        context=(
            f"Task: {row.task['slug']} ({row.task['task_id']})\n"
            f"Run: {expected_run_id}\n"
            f"Signal: {signal_name}\n"
            "Preservation: this signal does not archive the task and does not "
            "remove its workspace or change.\n"
            "Authorization: type exact yes (lowercase)."
        ),
        maximum=4,
    )
    if answer != "yes":
        return "signal cancelled"

    # Re-read ownership, lifecycle, observation, and run identity after the
    # modal. stop_task performs the final locked ownership/process validation.
    row = _fresh_active_row(
        model=model, config=config, store=store, journals=journals, jj=jj,
        task_id=task_id,
    )
    if _lookup_task_binding(env, task_id, store) is not None:
        raise ValueError(
            "initiative ownership changed; no raw signal was sent"
        )
    allowed = next(
        (item for item in _UNOWNED_SIGNAL_ACTIONS.get(row.display_state, ())
         if item[0] == key),
        None,
    )
    if allowed is None or row.observation.run_id != expected_run_id:
        raise ValueError("task run/state changed; no signal was sent")
    result = stop_task(
        config, row.task, tmux=_adapter_for_task(row.task), tasks=store,
        terminate=terminate,
    )
    observed = _fresh_active_row(
        model=model, config=config, store=store, journals=journals, jj=jj,
        task_id=task_id,
    )
    message = (
        f"{signal_name} requested for {row.task['slug']} run {result['run_id']}; "
        f"current observed state: {observed.display_state}"
    )
    model.message = message
    return message


def _stop_owned_attempt(
    *, initial_binding, stdscr, curses_module, model: TuiModel,
    config: ControlConfig, env: Mapping[str, str], store: TaskStore,
    journals: CreationJournalStore, jj: JjAdapter, task_id: str,
) -> str:
    _initiative_store, initiative, link, attempt, _node, task = initial_binding
    answer = _prompt_line(
        stdscr, curses_module, model,
        "Confirm [yes/N]: ",
        title="Stop attempt via initiative",
        context=(
            f"Task: {task['slug']} ({task['task_id']})\n"
            f"Initiative: {initiative.get('slug', initiative['initiative_id'])}\n"
            f"Node: {link['node_id']}\n"
            f"Attempt: {link['attempt_id']}\n"
            "Authorization: type exact yes (lowercase)."
        ),
        maximum=4,
    )
    if answer != "yes":
        return "initiative stop cancelled"
    current_binding = _lookup_task_binding(env, task_id, store)
    if current_binding is None:
        raise ValueError("initiative ownership changed; no stop action was submitted")
    initiative_store, current_initiative, current_link, current_attempt, _node, _task = (
        current_binding
    )
    if (
        current_initiative["initiative_id"] != initiative["initiative_id"]
        or current_link["attempt_id"] != link["attempt_id"]
        or current_link["node_id"] != link["node_id"]
    ):
        raise ValueError("initiative attempt binding changed; no stop action was submitted")
    if current_attempt["state"] in {"cancelled", "succeeded", "failed", "superseded"}:
        raise ValueError("initiative attempt became terminal before stop submission")
    from .orchestration.actions import (
        action_outcome, build_action_document, submit_action,
    )

    document = build_action_document(
        current_initiative, "stop-attempt",
        {"attempt_id": current_link["attempt_id"]}, actor_id="tui",
    )
    action = submit_action(
        initiative_store, current_initiative["initiative_id"], document,
    )
    outcome = action_outcome(action)
    action_id = action["action_id"]
    latest_attempt = initiative_store.read_attempt(
        current_initiative["initiative_id"], current_link["attempt_id"],
    )
    observed = _fresh_active_row(
        model=model, config=config, store=store, journals=journals, jj=jj,
        task_id=task_id,
    )
    os_evidence = f"OS state observed: {observed.display_state}"
    refusal = latest_attempt.get("refusal")
    if refusal is None:
        refusal = outcome.get("reason") or outcome.get("refusal")
    refusal_evidence = (
        "refusal: none" if refusal is None else f"refusal: {_safe_text(refusal)[:500]}"
    )
    attempt_evidence = f"attempt {latest_attempt['state']}"
    if action["state"] == "indeterminate":
        return (
            f"stop action {action_id} action indeterminate; {attempt_evidence}; "
            f"{refusal_evidence}; explicitly reconcile initiative "
            f"{current_initiative['initiative_id']}; {os_evidence}"
        )
    return (
        f"stop action {action_id} action {action['state']}; {attempt_evidence}; "
        f"{refusal_evidence}; {os_evidence}"
    )


def _context_actions(
    *,
    stdscr, curses_module, model: TuiModel, config: ControlConfig,
    env: Mapping[str, str], store: TaskStore, journals: CreationJournalStore,
    jj: JjAdapter, task_id: str,
) -> str | None:
    current = store.read(task_id)
    archived = current["lifecycle"] == "archived"
    terminal = (
        archived or current["lifecycle"] == "ended" or
        (
            current["lifecycle"] == "failed" and
            all(run["state"] in {"exited", "failed"} for run in current["runs"])
        )
    )
    active_row: TuiRow | None = None
    if not terminal:
        active_row = _fresh_active_row(
            model=model, config=config, store=store, journals=journals, jj=jj,
            task_id=task_id,
        )
        current = active_row.task
        archived = current["lifecycle"] == "archived"
        terminal = (
            archived or current["lifecycle"] == "ended" or
            (
                current["lifecycle"] == "failed" and
                all(run["state"] in {"exited", "failed"} for run in current["runs"])
            )
        )
    binding = _lookup_task_binding(env, task_id, store)
    owned = binding is not None
    actions: list[tuple[str, str]] = []
    if not archived or _archived_resources_remain(config, current):
        actions.append(("i", "inspect"))
    if not terminal and active_row is not None:
        if owned:
            actions.append(("s", "Stop attempt via initiative"))
        else:
            actions.extend(
                (key, label)
                for key, label, _terminate in
                _UNOWNED_SIGNAL_ACTIONS.get(active_row.display_state, ())
            )
    if terminal and not archived:
        actions.append(("a", "archive"))
    if terminal:
        if owned:
            actions.append(("c", "reconcile initiative"))
        elif current["runs"]:
            actions.append(("r", "retry"))
    if archived:
        actions.append(("p", "prune"))
    context = ""
    if binding is not None:
        _initiative_store, initiative, link, attempt, _node, _task = binding
        context = (
            f" initiative={initiative.get('slug', initiative['initiative_id'])}"
            f" node={link['node_id']} attempt={link['attempt_id']}"
            f" state={attempt['state']}"
        )
    choices = " | ".join(f"{key} {label}" for key, label in actions)
    answer = _prompt_line(
        stdscr, curses_module, model,
        "Action: ",
        title="Task actions",
        context=(
            f"Task: {current['slug']} ({current['task_id']}){context}\n"
            f"Choices: {choices}"
        ),
        maximum=1,
        candidates=tuple(ModalCandidate(key, label) for key, label in actions),
    )
    if answer is None:
        return "actions cancelled"
    allowed = {key for key, _label in actions}
    if answer not in allowed:
        return "action unavailable; task state was revalidated"
    if answer == "i":
        selected = model.selected_row
        if selected is None or selected.task["task_id"] != task_id:
            raise ValueError("selected task changed before inspection")
        if not current["runs"]:
            try:
                journal = journals.read(task_id)
            except JournalError as exc:
                if current["lifecycle"] == "failed":
                    return (
                        "retained creation journal cannot be authenticated; manual "
                        f"inspection only: {exc}"
                    )
            else:
                guidance = retained_recovery_guidance(current, journal)
                if guidance is not None:
                    return guidance
        refusal = _open_popup(
            stdscr, curses_module, config, selected,
            selected.observation.run_id, env,
        )
        model.replace_row(_read_row(config, store, journals, current, jj))
        return refusal or "popup closed; task resources were left untouched"
    if answer in {"I", "t", "f"}:
        if active_row is None or active_row.observation.run_id is None:
            raise ValueError("task has no fresh run identity to signal")
        return _signal_task_action(
            key=answer, expected_run_id=active_row.observation.run_id,
            stdscr=stdscr, curses_module=curses_module, model=model,
            config=config, env=env, store=store, journals=journals, jj=jj,
            task_id=task_id,
        )
    if answer == "s":
        if binding is None:
            raise ValueError("task is no longer owned by an initiative")
        return _stop_owned_attempt(
            initial_binding=binding, stdscr=stdscr,
            curses_module=curses_module, model=model, config=config, env=env,
            store=store, journals=journals, jj=jj, task_id=task_id,
        )
    if answer == "a":
        archive_intent = TuiIntent(
            IntentKind.ARCHIVE, task_id=task_id, requires_confirmation=True,
        )
        _execute_intent(
            archive_intent, stdscr=stdscr, curses_module=curses_module,
            model=model, config=config, env=env, store=store,
            journals=journals, jj=jj,
        )
        return None
    if answer == "r":
        return _retry_task(
            stdscr=stdscr, curses_module=curses_module, model=model,
            config=config, env=env, store=store, journals=journals, jj=jj,
            task_id=task_id,
        )
    if answer == "c":
        return _reconcile_task_initiative(
            env=env, store=store, task_id=task_id,
        )
    if answer == "p":
        return _prune_archived_task(
            stdscr=stdscr, curses_module=curses_module, model=model,
            config=config, env=env, store=store, journals=journals, jj=jj,
            task_id=task_id,
        )
    return "action unavailable"


_INITIATIVE_INTENTS = frozenset({
    IntentKind.INIT_OPEN, IntentKind.INIT_RECONCILE, IntentKind.INIT_DIFF,
    IntentKind.INIT_EVENTS, IntentKind.INIT_APPROVE, IntentKind.INIT_CANDIDATES,
    IntentKind.INIT_VERIFICATION, IntentKind.INIT_STORAGE, IntentKind.INIT_PAUSE,
    IntentKind.INIT_STOP, IntentKind.INIT_EXPAND, IntentKind.INIT_COLLAPSE,
})


def _linked_task_for_row(screen, row) -> tuple[str | None, str | None]:
    """(control_task_id, attempt_id) for a node or attempt row, from the initiative's links."""
    view = screen.view_for(row.initiative_id)
    if view is None:
        return None, None
    links = view.get("links", [])
    attempts = view.get("attempts", [])
    if row.kind == "attempt":
        link = next((item for item in links if item["attempt_id"] == row.id), None)
        return (None if link is None else link["control_task_id"]), row.id
    if row.kind == "node":
        node_attempts = sorted(
            (item for item in attempts if item["node_id"] == row.id),
            key=lambda item: (item.get("ordinal", 0), item["attempt_id"]),
        )
        for attempt in reversed(node_attempts):
            link = next((item for item in links if item["attempt_id"] == attempt["attempt_id"]), None)
            if link is not None:
                return link["control_task_id"], attempt["attempt_id"]
    return None, None


def _execute_initiative_intent(
    intent: TuiIntent,
    *,
    stdscr,
    curses_module,
    model: TuiModel,
    config: ControlConfig,
    env: Mapping[str, str],
    store: TaskStore,
    journals: CreationJournalStore,
    jj: JjAdapter,
) -> str | None:
    screen = model.initiatives
    if screen is None:
        return model.initiatives_error or "initiatives unavailable"
    if intent.kind is IntentKind.INIT_EXPAND:
        return None if screen.expand() else "nothing to expand"
    if intent.kind is IntentKind.INIT_COLLAPSE:
        return None if screen.collapse() else "nothing to collapse"
    row = screen.selected_row
    if row is None:
        return "no initiative is selected"
    if intent.kind in {IntentKind.INIT_EVENTS, IntentKind.INIT_CANDIDATES, IntentKind.INIT_VERIFICATION, IntentKind.INIT_STORAGE}:
        pane = {
            IntentKind.INIT_EVENTS: "events", IntentKind.INIT_CANDIDATES: "candidates",
            IntentKind.INIT_VERIFICATION: "verification", IntentKind.INIT_STORAGE: "storage",
        }[intent.kind]
        screen.pane = "summary" if screen.pane == pane else pane
        if screen.pane == "storage":
            view = screen.view_for(row.initiative_id)
            if view is not None and view.get("storage") is None:
                from .orchestration.config import load_config as load_orchestration_config
                from .orchestration.storage import storage_report
                from .orchestration.store import InitiativeStore

                initiative_store = InitiativeStore(load_orchestration_config(env))
                view["storage"] = storage_report(view["initiative"], store=initiative_store, jj=jj)
        return None
    from .orchestration.config import load_config as load_orchestration_config
    from .orchestration.store import InitiativeStore

    initiative_store = InitiativeStore(load_orchestration_config(env))
    initiative = initiative_store.peek(row.initiative_id)
    if intent.kind is IntentKind.INIT_RECONCILE:
        from .orchestration.cli import reconcile_one_initiative

        result = reconcile_one_initiative(initiative_store, row.initiative_id)
        _refresh_initiatives(model, env)
        coordinator = result.get("coordinator_reconciliation")
        return "initiative reconciled" + ("" if not coordinator else f"; coordinator {coordinator['state']}")
    if intent.kind in {IntentKind.INIT_OPEN, IntentKind.INIT_DIFF}:
        task_id, _attempt_id = _linked_task_for_row(screen, row)
        if task_id is None:
            return "select a node or attempt with a linked Control task"
        task = store.peek(task_id)
        if intent.kind is IntentKind.INIT_DIFF:
            diff = jj.diff_summary(Path(task["jj"]["workspace_path"]))
            return f"jj diff for {task['slug']} at {diff.refreshed_at}: " + (diff.summary.splitlines() or ["no changes"])[0]
        task_row = _read_row(config, store, journals, task, jj)
        refusal = _open_popup(stdscr, curses_module, config, task_row, None, env)
        return refusal or "popup closed; task resources were left untouched"
    if intent.kind is IntentKind.INIT_APPROVE:
        from .orchestration.cli import _latest_plan, approve_plan, reject_plan

        if initiative["state"] != "awaiting-plan-approval":
            return "no plan is awaiting approval"
        plan = _latest_plan(initiative_store, row.initiative_id)
        answer = _prompt_line(
            stdscr, curses_module, model, "Decision [approve/reject/N]: ",
            title="Plan approval",
            context=(
                f"Initiative: {initiative['slug']} ({initiative['initiative_id']})\n"
                f"Plan: revision {plan['revision']} digest {plan['digest']}\n"
                "Authorization: type exact approve or reject (lowercase)."
            ),
            maximum=7,
        )
        if answer == "approve":
            approve_plan(initiative_store, initiative, plan["digest"], actor_id="tui")
            _refresh_initiatives(model, env)
            return f"plan revision {plan['revision']} approved; activate from the CLI when ready"
        if answer == "reject":
            reason = _prompt_line(stdscr, curses_module, model, "Reason: ", maximum=200, title="Plan rejection")
            if not reason:
                return "rejection cancelled"
            reject_plan(initiative_store, initiative, plan["digest"], reason, actor_id="tui")
            _refresh_initiatives(model, env)
            return f"plan revision {plan['revision']} rejected"
        return "approval decision cancelled"
    if intent.kind is IntentKind.INIT_PAUSE:
        from .orchestration.actions import action_outcome, build_action_document, submit_action

        target = "resume" if initiative["state"] in {"paused", "needs-input"} else "pause"
        answer = _prompt_line(
            stdscr, curses_module, model, "Confirm [yes/N]: ", title=f"{target.capitalize()} initiative",
            context=f"Initiative: {initiative['slug']} ({initiative['initiative_id']})\nState: {initiative['state']}",
            maximum=4,
        )
        if answer != "yes":
            return f"{target} cancelled"
        document = build_action_document(initiative, target, {}, actor_id="tui")
        outcome = submit_action(initiative_store, row.initiative_id, document)
        _refresh_initiatives(model, env)
        return f"{target}: {outcome['state']} ({action_outcome(outcome).get('reason') or action_outcome(outcome).get('status')})"
    if intent.kind is IntentKind.INIT_STOP:
        from .orchestration.actions import action_outcome, build_action_document, submit_action

        _task_id, attempt_id = _linked_task_for_row(screen, row)
        if attempt_id is None:
            return "select a node or attempt with a live linked task"
        answer = _prompt_line(
            stdscr, curses_module, model, "Confirm [yes/N]: ", title="Stop attempt",
            context=f"Attempt: {attempt_id}\nControl asks the worker to stop gracefully; nothing is deleted.",
            maximum=4,
        )
        if answer != "yes":
            return "stop cancelled"
        document = build_action_document(initiative, "stop-attempt", {"attempt_id": attempt_id}, actor_id="tui")
        outcome = submit_action(initiative_store, row.initiative_id, document)
        _refresh_initiatives(model, env)
        return f"stop-attempt: {outcome['state']} ({action_outcome(outcome).get('reason') or action_outcome(outcome).get('status')})"
    return "action unavailable"


def _execute_intent(
    intent: TuiIntent,
    *,
    stdscr,
    curses_module,
    model: TuiModel,
    config: ControlConfig,
    env: Mapping[str, str],
    store: TaskStore,
    journals: CreationJournalStore,
    jj: JjAdapter,
) -> bool:
    row = model.selected_row
    if intent.kind is IntentKind.NONE:
        if intent.reason:
            model.message = intent.reason
        return True
    if intent.kind is IntentKind.QUIT:
        return False
    if intent.kind is IntentKind.HELP:
        model.help_visible = not model.help_visible
        return True
    if intent.kind is IntentKind.TOGGLE_SCOPE:
        model.include_archived = not model.include_archived
        model.replace_rows(_load_rows(
            config, store, journals, jj,
            include_archived=model.include_archived,
        ))
        _surface_skipped(model, store)
        return True
    if intent.kind is IntentKind.FILTER:
        if model.mode == "initiatives" and model.initiatives is not None:
            value = _prompt_line(
                stdscr, curses_module, model, "Filter: ",
                initial=model.initiatives.filter_string, maximum=200,
            )
            if value is not None:
                model.initiatives.set_filter(value)
            return True
        value = _prompt_line(
            stdscr, curses_module, model, "Filter: ",
            initial=model.filter_string, maximum=200,
        )
        if value is not None:
            model.set_filter(value)
        return True
    if intent.kind is IntentKind.START:
        model.message = _start_form(stdscr, curses_module, model, env, config)
        model.replace_rows(_load_rows(
            config, store, journals, jj,
            include_archived=model.include_archived,
        ))
        _surface_skipped(model, store)
        return True
    if intent.kind is IntentKind.TOGGLE_MODE:
        if model.mode == "initiatives":
            model.mode = "tasks"
            model.help_visible = False
            return True
        _enter_initiatives(model, env)
        model.help_visible = False
        return True
    if intent.kind in _INITIATIVE_INTENTS:
        model.message = _execute_initiative_intent(
            intent, stdscr=stdscr, curses_module=curses_module, model=model,
            config=config, env=env, store=store, journals=journals, jj=jj,
        )
        return True
    if row is None:
        model.message = "no task is selected"
        return True
    if intent.kind is IntentKind.ACTIONS:
        message = _context_actions(
            stdscr=stdscr, curses_module=curses_module, model=model,
            config=config, env=env, store=store, journals=journals, jj=jj,
            task_id=row.task["task_id"],
        )
        if message is not None:
            model.message = message
        return True
    if intent.kind is IntentKind.OPEN:
        refusal = _open_popup(
            stdscr, curses_module, config, row, intent.run_id, env,
        )
        refreshed = _read_row(config, store, journals, row.task, jj)
        model.replace_row(refreshed)
        if refreshed.task["lifecycle"] == "archived":
            suffix = "archived lifecycle refreshed; live resources were not reconciled"
        else:
            suffix = "live evidence reconciled"
        model.message = (
            f"{refusal}; {suffix}" if refusal else
            f"popup closed; {suffix}; task resources were left untouched"
        )
        return True
    if intent.kind is IntentKind.RECONCILE:
        refreshed = _read_row(config, store, journals, row.task, jj)
        model.replace_row(refreshed)
        if refreshed.task["lifecycle"] == "archived":
            model.message = "archived lifecycle refreshed; live resources were not reconciled"
        else:
            model.message = "live evidence reconciled"
        return True
    if intent.kind is IntentKind.DIFF:
        diff = jj.diff_summary(Path(row.task["jj"]["workspace_path"]))
        model.record_diff(row.task["task_id"], diff)
        model.message = f"jj diff refreshed at {diff.refreshed_at}"
        return True
    if intent.kind is IntentKind.ARCHIVE:
        answer = _prompt_line(
            stdscr, curses_module, model,
            "Confirm [yes/N]: ",
            title="Archive task",
            context=(
                f"Task: {row.task['slug']} ({row.task['task_id']})\n"
                "Preservation: archive retains the workspace and change.\n"
                "Authorization: type exact yes (lowercase)."
            ),
            maximum=4,
        )
        if answer != "yes":
            model.message = "archive cancelled"
            return True
        presentation = _adapter_for_task(row.task)
        archive_task(
            config, row.task, tasks=store,
            adapters=LiveAdapters(
                config=config, tmux=presentation, jj=jj,
            ),
            journals=journals, jj=jj, presentation=presentation,
        )
        model.replace_rows(_load_rows(
            config, store, journals, jj,
            include_archived=model.include_archived,
        ))
        model.message = "task archived; workspace and change preserved"
        _surface_skipped(model, store)
        return True
    return True


def _curses_loop(
    stdscr,
    curses_module,
    model: TuiModel,
    config: ControlConfig,
    env: Mapping[str, str],
    store: TaskStore,
    journals: CreationJournalStore,
    jj: JjAdapter,
) -> int:
    stdscr.timeout(_INPUT_POLL_MS)
    next_refresh = time.monotonic() + _AUTO_REFRESH_SECONDS
    while True:
        _reap_deferred_start_workers()
        _paint(stdscr, curses_module, model)
        key = stdscr.getch()
        if key != -1:
            if key == getattr(curses_module, "KEY_RESIZE", -998):
                height, width = stdscr.getmaxyx()
                model.resize(height, width)
                continue
            if key == getattr(curses_module, "KEY_UP", -997):
                if model.mode == "initiatives" and model.initiatives is not None:
                    model.initiatives.move_selection(-1)
                else:
                    model.move_selection(-1)
                continue
            if key == getattr(curses_module, "KEY_DOWN", -996):
                if model.mode == "initiatives" and model.initiatives is not None:
                    model.initiatives.move_selection(1)
                else:
                    model.move_selection(1)
                continue
            if key in {10, 13, getattr(curses_module, "KEY_ENTER", -995)}:
                value = "ENTER"
            elif key == 9:
                value = "\t"
            elif key == getattr(curses_module, "KEY_RIGHT", -994):
                value = "RIGHT"
            elif key == getattr(curses_module, "KEY_LEFT", -993):
                value = "LEFT"
            elif 0 <= key <= 0x10FFFF:
                value = chr(key)
            else:
                value = ""
            intent = model.dispatch_key(value)
            try:
                if not _execute_intent(
                    intent, stdscr=stdscr, curses_module=curses_module,
                    model=model, config=config, env=env, store=store,
                    journals=journals, jj=jj,
                ):
                    return 0
            except (PruneError, ValueError, OSError) as exc:
                model.message = _safe_error(exc)
            continue
        if time.monotonic() >= next_refresh:
            # One refresh per elapsed boundary: no catch-up loop and no
            # background worker. A slow adapter cannot create an unbounded
            # queue of pending reconciliations.
            try:
                model.replace_rows(_load_rows(
                    config, store, journals, jj,
                    include_archived=model.include_archived,
                ))
                _surface_skipped(model, store)
                model.automatic_refresh_error = None
                if model.mode == "initiatives" and model.initiatives is not None:
                    _refresh_initiatives(model, env)
            except (ValueError, OSError) as exc:
                model.automatic_refresh_error = (
                    f"automatic reconciliation failed: {_safe_error(exc)}"
                )
            finally:
                # Schedule from completion, not the pre-load boundary. A slow
                # pass must not trigger an immediate continuous refresh loop.
                next_refresh = time.monotonic() + _AUTO_REFRESH_SECONDS


def _degrade(stderr: TextIO) -> int:
    print(_DEGRADE_MESSAGE, file=stderr)
    return 2


def run_tui(
    env: Mapping[str, str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    curses_module=None,
    initial_mode: str = "tasks",
) -> int:
    """Preflight and run curses, restoring signal dispositions on every exit."""
    values = os.environ if env is None else env
    input_stream = sys.stdin if stdin is None else stdin
    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    if curses_module is None:
        try:
            curses_module = importlib.import_module("curses")
        except (ImportError, OSError):
            return _degrade(errors)
    if (not getattr(input_stream, "isatty", lambda: False)() or
            not getattr(output, "isatty", lambda: False)()):
        return _degrade(errors)
    try:
        curses_module.setupterm()
    except Exception:
        return _degrade(errors)

    config = load_config(values)
    store = TaskStore(config)
    journals = CreationJournalStore(config)
    jj = JjAdapter()
    model = TuiModel(_load_rows(config, store, journals, jj))
    _surface_skipped(model, store)
    if initial_mode == "initiatives":
        _enter_initiatives(model, values)

    previous: dict[int, Any] = {}

    def shutdown(signum, _frame) -> None:
        raise _TuiShutdown(signum)

    try:
        for signum in (signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, shutdown)
        try:
            return curses_module.wrapper(
                _curses_loop, curses_module, model, config, values,
                store, journals, jj,
            )
        except _TuiShutdown as exc:
            if exc.detail is not None:
                print(f"asha control: {exc.detail}", file=errors)
            return 128 + exc.signum
        except curses_module.error as exc:
            print(
                f"asha control: terminal TUI failed: {_safe_error(exc)}; "
                "use `asha task list --json`.",
                file=errors,
            )
            return 2
    finally:
        for signum, disposition in previous.items():
            signal.signal(signum, disposition)


__all__ = [
    "DetailProjection", "IntentKind", "ModalCandidate", "ModalFrame",
    "StartCandidateSnapshot", "TuiIntent", "TuiModel", "TuiRow",
    "filter_rows", "freeze_start_candidates", "modal_frame", "render",
    "run_tui", "sort_rows",
]
