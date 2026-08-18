"""Pure terminal UI model plus a thin, lazily imported curses driver."""

from __future__ import annotations

import contextlib
import copy
import importlib
import io
import os
import signal
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, TextIO

from .config import ControlConfig, load_config
from .jj import DiffSummary, JjAdapter
from .launch import archive_task
from .reconcile import LiveAdapters
from .store import TaskStore
from .tmux import TmuxAdapter
from .transaction import CreationJournalStore
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
_FIXED_SCREEN_LINES = 12
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
    FILTER = "filter"
    QUIT = "quit"
    HELP = "help"


@dataclass(frozen=True)
class TuiIntent:
    kind: IntentKind
    task_id: str | None = None
    run_id: str | None = None
    requires_confirmation: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class TuiRow:
    task: dict[str, Any]
    reconciliation: dict[str, Any]

    @classmethod
    def from_records(
        cls, task: dict[str, Any], reconciliation: dict[str, Any],
    ) -> "TuiRow":
        return cls(copy.deepcopy(task), copy.deepcopy(reconciliation))

    @property
    def summary(self) -> dict[str, Any]:
        return view.task_summary(self.task, self.reconciliation)


@dataclass(frozen=True)
class DetailProjection:
    task_id: str
    slug: str
    run_id: str | None
    role: str | None
    tmux: str
    evidence: str
    workspace: str
    change: str
    blocker: str | None
    diff_summary: str | None
    diff_refreshed_at: str | None


def _row_sort_key(row: TuiRow) -> tuple[Any, ...]:
    summary = row.summary
    return (
        _STATE_ORDER.get(summary["status"], 99),
        summary["slug"].casefold(),
        summary["repository"]["root"].casefold(),
        summary["task_id"],
    )


def sort_rows(rows: Iterable[TuiRow]) -> tuple[TuiRow, ...]:
    """Return detached rows in one stable, evidence-priority order."""
    detached = [TuiRow.from_records(row.task, row.reconciliation) for row in rows]
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
            summary["status"], summary["slug"], summary["label"],
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
    ) -> None:
        self.rows = sort_rows(rows)
        self.selection = selection
        self.filter_string = filter_string
        self.height = max(0, int(height))
        self.width = max(0, int(width))
        self.now = now or datetime.now(timezone.utc)
        self._clock_pinned = now is not None
        self.scroll_offset = 0
        self.diffs: dict[str, DiffSummary] = {}
        self.message: str | None = None
        self.help_visible = False
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
        run = task["runs"][-1] if task["runs"] else None
        derived = None
        if run is not None:
            derived = next(
                (item for item in row.reconciliation["runs"]
                 if item["run_id"] == run["run_id"]),
                None,
            )
        if run is None:
            evidence = "No run has been recorded."
        else:
            evidence = f"{run['evidence']} @ {run['evidence_at']}"
            if derived is not None and derived["evidence"]:
                item = derived["evidence"][-1]
                evidence += f"; {item['source']}={item['outcome']}: {item['detail']}"
        diff = self.diffs.get(task["task_id"])
        return DetailProjection(
            task_id=task["task_id"],
            slug=task["slug"],
            run_id=None if run is None else run["run_id"],
            role=None if run is None else run["role"],
            tmux=f"{task['tmux']['session']}:{task['tmux']['window']}"
                 + ("" if run is None else f" {run['pane_id']}"),
            evidence=evidence,
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

    def record_diff(self, task_id: str, diff: DiffSummary) -> None:
        if all(row.task["task_id"] != task_id for row in self.rows):
            raise ValueError("diff summary does not belong to a displayed task")
        self.diffs[task_id] = diff

    def dispatch_key(self, key: str) -> TuiIntent:
        normalized = "ENTER" if key in {"\n", "\r", "ENTER"} else key
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
            terminal_runs = bool(row.reconciliation["runs"]) and all(
                run["state"] in {"exited", "failed"}
                for run in row.reconciliation["runs"]
            )
            if (row.task["lifecycle"] not in {"running", "ended"} or
                    not terminal_runs):
                return TuiIntent(
                    IntentKind.NONE, task_id=row.task["task_id"],
                    reason="only a task whose runs have all exited can be archived",
                )
            return TuiIntent(
                IntentKind.ARCHIVE, task_id=row.task["task_id"],
                requires_confirmation=True,
            )
        if normalized == "/":
            return TuiIntent(IntentKind.FILTER)
        if normalized == "q":
            return TuiIntent(IntentKind.QUIT)
        if normalized == "?":
            return TuiIntent(IntentKind.HELP)
        return TuiIntent(IntentKind.NONE)

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


def _age(value: str, now: datetime) -> str:
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
    if model.help_visible:
        lines = [
            "ASHA CONTROL HELP",
            "",
            "Keys: Enter open popup | n start | r reconcile | d diff refresh",
            "      a archive task after every run exits | / filter | q quit | ? help",
            "",
            "Status: every state is derived from qualified tmux, process, jj, and event evidence.",
            "Limitations: no destructive removal or automated integration; archive preserves data.",
            "Closing a popup detaches only. SIGKILL and hard crashes cannot restore terminal mode.",
        ]
        return [_clip(line, model.width) for line in lines[:model.height]]

    title = "ASHA TASKS"
    if model.filter_string:
        title += f"  Filter: {model.filter_string}"
    lines = [title, ""]
    lines.append(_table_line(
        ("STATE", "TASK", "REPOSITORY", "CHANGE", "HARNESS", "AGE"),
        model.width,
    ))
    visible = model.visible_rows
    for offset, row in enumerate(visible):
        summary = row.summary
        run = row.task["runs"][-1] if row.task["runs"] else None
        absolute_index = model.scroll_offset + offset
        marker = ">" if absolute_index == model.selection else " "
        repository = Path(summary["repository"]["root"]).name or "/"
        line = _table_line((
            summary["status"], summary["slug"], repository,
            row.task["jj"]["change_id"] or "-",
            "-" if run is None else run["harness"],
            _age(summary["updated_at"], model.now),
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
            f"Workspace:  {detail.workspace}",
            f"Change:     {detail.change}, last explicit refresh: "
            f"{detail.diff_refreshed_at or 'never'}",
            f"Blocker:    {detail.blocker or 'none'}",
        ])
        if detail.diff_summary is not None:
            diff_lines = detail.diff_summary.splitlines() or ["No changes."]
            lines.extend(f"Diff:       {line}" for line in diff_lines[:3])
    footer = "Enter open  n start  r reconcile  d diff  a archive  / filter  ? help  q quit"
    status_lines: list[str] = []
    if model.message:
        # Wrap rather than clip: a refusal carries its remedy at the END of
        # the sentence, which a single clipped line would hide.
        status_lines = _wrap_status(model.message, model.width)
    budget = max(0, model.height - len(lines) - 1)
    if len(status_lines) > budget:
        status_lines = status_lines[:budget]
        if status_lines:
            status_lines[-1] = _clip(status_lines[-1][:-1] + "…", model.width)
    lines.extend(status_lines)
    lines.append(footer)
    return [_clip(line, model.width) for line in lines[:model.height]]


_STATUS_MAX_LINES = 6


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


class _TuiShutdown(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


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
) -> TuiRow:
    adapter = _adapter_for_task(listed)
    live = LiveAdapters(config=config, tmux=adapter, jj=jj)
    task, reconciliation = view.locked_reconciliation(
        store, journals, listed["task_id"], live, jj, presentation=adapter,
    )
    return TuiRow.from_records(task, reconciliation)


def _load_rows(
    config: ControlConfig, store: TaskStore,
    journals: CreationJournalStore, jj: JjAdapter,
) -> list[TuiRow]:
    return [
        _read_row(config, store, journals, listed, jj)
        for listed in store.list()
        if listed["lifecycle"] != "archived"
    ]


def _surface_skipped(model: TuiModel, store: TaskStore) -> None:
    if not store.skipped:
        return
    detail = f"{len(store.skipped)} registry entries skipped"
    model.message = f"{model.message}; {detail}" if model.message else detail


def _paint(stdscr, curses_module, model: TuiModel) -> None:
    height, width = stdscr.getmaxyx()
    model.resize(height, width)
    stdscr.erase()
    for y, line in enumerate(render(model)):
        if y >= height or width <= 1:
            break
        try:
            stdscr.addnstr(y, 0, line, width - 1)
        except curses_module.error:
            pass
    stdscr.refresh()


def _prompt_line(
    stdscr, curses_module, model: TuiModel, prompt: str,
    *, initial: str = "", maximum: int = 500,
) -> str | None:
    value = list(initial[:maximum])
    while True:
        _paint(stdscr, curses_module, model)
        height, width = stdscr.getmaxyx()
        if height:
            line = _clip(prompt + "".join(value), max(0, width - 1))
            try:
                stdscr.move(height - 1, 0)
                stdscr.clrtoeol()
                stdscr.addnstr(height - 1, 0, line, max(0, width - 1))
                stdscr.refresh()
            except curses_module.error:
                pass
        key = stdscr.getch()
        if key == -1:
            continue
        if key in {10, 13, getattr(curses_module, "KEY_ENTER", -999)}:
            return "".join(value)
        if key == 27:
            return None
        if key == getattr(curses_module, "KEY_RESIZE", -998):
            continue
        if key in {8, 127, getattr(curses_module, "KEY_BACKSPACE", -997)}:
            if value:
                value.pop()
            continue
        if 0 <= key <= 0x10FFFF and len(value) < maximum:
            character = chr(key)
            if character.isprintable() and unicodedata.category(character) not in {
                "Cc", "Cf", "Cs",
            }:
                value.append(character)


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


def _start_form(
    stdscr, curses_module, model: TuiModel,
    env: Mapping[str, str], config: ControlConfig,
) -> str:
    repo = _prompt_line(
        stdscr, curses_module, model, "Repository: ",
        initial=str(Path.cwd()), maximum=4096,
    )
    if repo is None:
        return "task start cancelled"
    base = _prompt_line(
        stdscr, curses_module, model, "Base (empty = default trunk/main): ",
        initial="", maximum=500,
    )
    if base is None:
        return "task start cancelled"
    harness = _prompt_line(
        stdscr, curses_module, model, "Harness: ",
        initial=config.default_harness, maximum=32,
    )
    if harness is None:
        return "task start cancelled"
    role = _prompt_line(
        stdscr, curses_module, model, "Role: ",
        initial="implementer", maximum=64,
    )
    if role is None:
        return "task start cancelled"
    goal = _prompt_line(
        stdscr, curses_module, model, "Goal: ", maximum=200,
    )
    if goal is None:
        return "task start cancelled"
    arguments = ["--repo", repo]
    if base.strip():
        arguments.extend(["--base", base.strip()])
    arguments.extend([
        "--harness", harness, "--role", role, "--goal", goal, "--detach",
    ])
    output = io.StringIO()
    curses_module.endwin()
    try:
        from .cli import _start_command
        with contextlib.redirect_stdout(output):
            status = _start_command(
                arguments, env, preserve_sigterm_handler=True,
            )
        if status != 0:
            raise ValueError(f"task start exited with status {status}")
    finally:
        _repaint_after_suspend(stdscr)
    first = output.getvalue().splitlines()
    return first[0] if first else "task started"


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
    if intent.kind is IntentKind.FILTER:
        value = _prompt_line(
            stdscr, curses_module, model, "Filter: ",
            initial=model.filter_string, maximum=200,
        )
        if value is not None:
            model.set_filter(value)
        return True
    if intent.kind is IntentKind.START:
        model.message = _start_form(stdscr, curses_module, model, env, config)
        model.replace_rows(_load_rows(config, store, journals, jj))
        _surface_skipped(model, store)
        return True
    if row is None:
        model.message = "no task is selected"
        return True
    if intent.kind is IntentKind.OPEN:
        refusal = _open_popup(
            stdscr, curses_module, config, row, intent.run_id, env,
        )
        model.message = (
            refusal or "popup closed; task resources were left untouched"
        )
        return True
    if intent.kind is IntentKind.RECONCILE:
        model.replace_row(_read_row(config, store, journals, row.task, jj))
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
            f"Archive {row.task['slug']} and preserve its workspace/change? [yes/N] ",
            maximum=3,
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
        model.replace_rows(_load_rows(config, store, journals, jj))
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
    stdscr.timeout(200)
    while True:
        _paint(stdscr, curses_module, model)
        key = stdscr.getch()
        if key == -1:
            continue
        if key == getattr(curses_module, "KEY_RESIZE", -998):
            height, width = stdscr.getmaxyx()
            model.resize(height, width)
            continue
        if key == getattr(curses_module, "KEY_UP", -997):
            model.move_selection(-1)
            continue
        if key == getattr(curses_module, "KEY_DOWN", -996):
            model.move_selection(1)
            continue
        if key in {10, 13, getattr(curses_module, "KEY_ENTER", -995)}:
            value = "ENTER"
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
        except (ValueError, OSError) as exc:
            model.message = _safe_error(exc)


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
    "DetailProjection", "IntentKind", "TuiIntent", "TuiModel", "TuiRow",
    "filter_rows", "render", "run_tui", "sort_rows",
]
