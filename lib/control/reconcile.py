"""Pure evidence reconciliation with injected, non-mutating adapters."""

from __future__ import annotations

import copy
import re
import unicodedata
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from .config import ControlConfig
from .events import EventError, read_snapshot
from . import harness as harness_api
from .harness import INPUT_PROMPT_MARKERS, INPUT_PROMPT_TAIL_LINES, HarnessError
from .jj import JjAdapter, JjError
from .model import validate_task
from .tmux import TmuxAdapter, TmuxError


Outcome = Literal["match", "missing", "mismatch", "unavailable"]
_FIELD_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
_SEMANTIC_EVENT_STATES = frozenset({"working", "needs-input", "idle"})
_TERMINAL_STATES = frozenset({"exited", "failed"})
_EVENT_STATES = _SEMANTIC_EVENT_STATES | _TERMINAL_STATES
_PROVEN_ACTIVE_STATES = frozenset({"starting", "working", "needs-input", "idle", "unknown"})
# In-progress states imply the harness is mid-turn.  A harness with no wired
# stop event never supersedes them, so they are only trustworthy while recent.
# `idle` (turn-stopped) is a legitimate resting state and is NOT aged; terminal
# states are durable facts and never age.
_AGEABLE_EVENT_STATES = frozenset({"working", "needs-input"})
_CREATION_UNSET = object()


def _bounded_field(value: str, name: str, maximum: int, *, pattern=None) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{name} must contain 1-{maximum} characters")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        raise ValueError(f"{name} must not contain Unicode control characters")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise ValueError(f"{name} uses an invalid restricted grammar")


@dataclass(frozen=True)
class Evidence:
    source: str
    outcome: Outcome
    detail: str
    state: str | None = None
    stale: bool = False

    def __post_init__(self) -> None:
        _bounded_field(self.source, "evidence source", 32, pattern=_FIELD_NAME)
        _bounded_field(self.detail, "evidence detail", 500)
        if self.outcome not in {"match", "missing", "mismatch", "unavailable"}:
            raise ValueError("invalid evidence outcome")
        if self.source == "event" and self.outcome == "match":
            if self.state not in _EVENT_STATES:
                raise ValueError("matched event evidence requires a supported event state")
        elif self.source == "process" and self.outcome == "missing":
            if self.state is not None and self.state not in _TERMINAL_STATES:
                raise ValueError("missing process evidence may carry only a terminal state")
        elif self.source == "tmux" and self.outcome == "match":
            # A matched owned pane may report the one state a screen can
            # prove: a harness input prompt is visible.
            if self.state is not None and self.state != "needs-input":
                raise ValueError("matched tmux evidence may carry only needs-input")
        elif self.state is not None:
            raise ValueError(
                "only matched event, matched tmux, or missing process evidence may carry a state"
            )
        if self.stale and not (
            self.source == "event" and self.outcome == "match" and
            self.state in _AGEABLE_EVENT_STATES
        ):
            raise ValueError("only a matched in-progress event may be marked stale")


class Adapters(Protocol):
    def tmux(self, task: dict[str, Any], run: dict[str, Any]) -> Evidence: ...
    def process(self, task: dict[str, Any], run: dict[str, Any]) -> Evidence: ...
    def jj(self, task: dict[str, Any]) -> Evidence: ...
    def event(self, task: dict[str, Any], run: dict[str, Any]) -> Evidence: ...


class UnavailableAdapters:
    """Increment 1 default: explicit unknown evidence and no external calls."""

    @staticmethod
    def _evidence(source: str) -> Evidence:
        return Evidence(source, "unavailable", f"{source} live adapter is not implemented in Increment 1")

    def tmux(self, task, run):
        return self._evidence("tmux")

    def process(self, task, run):
        return self._evidence("process")

    def jj(self, task):
        return self._evidence("jj")

    def event(self, task, run):
        return self._evidence("event")


def _safe_detail(value: Any, fallback: str) -> str:
    text = str(value)
    safe = "".join(char if char.isprintable() else "?" for char in text)[:400]
    return safe or fallback


def _tmux_target_missing(exc: BaseException) -> bool:
    detail = str(exc).casefold()
    return any(marker in detail for marker in (
        "can't find pane", "can't find session", "no server running",
        "no sessions", "no such window", "no such pane",
    ))


class LiveAdapters:
    """Read-only live evidence adapters for tmux, /proc, jj, and events."""

    def __init__(
        self,
        *,
        config: ControlConfig | None = None,
        tmux: TmuxAdapter | None = None,
        jj: JjAdapter | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.tmux_adapter = tmux or TmuxAdapter()
        self.jj_adapter = jj or JjAdapter()
        # Injectable so tests can age a snapshot deterministically; live callers
        # get wall-clock UTC, consistent with this being the live evidence source.
        self._now = now or (lambda: datetime.now(timezone.utc))

    def tmux(self, task, run):
        session = task["tmux"]["session"]
        try:
            if not self.tmux_adapter.has_session(session):
                return Evidence("tmux", "missing", "recorded tmux session is absent")
            managed = self.tmux_adapter.session_option(session, "@asha_managed")
            owner = self.tmux_adapter.session_option(session, "@asha_task_id")
            if managed != "1" or owner != task["task_id"]:
                return Evidence(
                    "tmux", "mismatch",
                    "tmux ownership options disagree with the task record",
                )
            facts = self.tmux_adapter.pane_facts(run["pane_id"])
            if facts.session != session or facts.window != task["tmux"]["window"]:
                return Evidence(
                    "tmux", "mismatch", "recorded pane belongs to a different tmux target",
                )
            prompt = None if facts.dead else self._visible_prompt(run)
            if prompt is not None:
                return Evidence(
                    "tmux", "match",
                    f"owned tmux session and pane matched; pane shows {prompt}",
                    state="needs-input",
                )
            return Evidence("tmux", "match", "owned tmux session and pane matched")
        except TmuxError as exc:
            if _tmux_target_missing(exc):
                return Evidence("tmux", "missing", "recorded tmux pane is absent")
            return Evidence(
                "tmux", "unavailable",
                f"tmux evidence unavailable: {_safe_detail(exc, 'adapter failure')}",
            )

    def _visible_prompt(self, run) -> str | None:
        """Name the harness input prompt on the pane's visible tail, if any."""
        markers = INPUT_PROMPT_MARKERS.get(run.get("harness"), ())
        reader = getattr(self.tmux_adapter, "pane_tail", None)
        if not markers or reader is None:
            return None
        try:
            tail = reader(run["pane_id"], lines=INPUT_PROMPT_TAIL_LINES)
        except (TmuxError, ValueError):
            return None
        for line in tail:
            for marker in markers:
                if marker in line:
                    return f"the {run['harness']} input prompt {marker[:40]!r}"
        return None

    def process(self, task, run):
        try:
            facts = self.tmux_adapter.pane_facts(run["pane_id"])
        except TmuxError as exc:
            if _tmux_target_missing(exc):
                try:
                    matched = harness_api.verify_process(
                        run["pid"], run["process_start_identity"],
                    )
                except HarnessError as identity_exc:
                    return Evidence(
                        "process", "unavailable",
                        "process identity evidence unavailable: "
                        f"{_safe_detail(identity_exc, 'adapter failure')}",
                    )
                if matched:
                    return Evidence(
                        "process", "match",
                        "recorded tmux pane is absent but the process identity is live",
                    )
                return Evidence(
                    "process", "missing",
                    "recorded tmux pane is absent and the process identity is gone",
                    state="failed",
                )
            return Evidence(
                "process", "unavailable",
                f"process pane evidence unavailable: {_safe_detail(exc, 'adapter failure')}",
            )
        # tmux 3.4 leaves pane_pid stale after death.  pane_dead is the
        # authoritative liveness fact and must be consulted first.
        if facts.dead:
            if facts.dead_signal is not None:
                return Evidence(
                    "process", "missing",
                    f"tmux pane process was killed by signal {facts.dead_signal}",
                    state="failed",
                )
            if facts.dead_status is not None:
                state = "exited" if facts.dead_status == 0 else "failed"
                return Evidence(
                    "process", "missing",
                    f"tmux pane process exited with status {facts.dead_status}",
                    state=state,
                )
            return Evidence("process", "missing", "tmux pane process has exited")
        if facts.pane_pid is None:
            return Evidence("process", "unavailable", "live tmux pane did not report a pid")
        if facts.pane_pid != run["pid"]:
            return Evidence("process", "mismatch", "tmux pane pid differs from the run record")
        try:
            matched = harness_api.verify_process(
                run["pid"], run["process_start_identity"],
            )
        except HarnessError as exc:
            return Evidence(
                "process", "unavailable",
                f"process identity evidence unavailable: {_safe_detail(exc, 'adapter failure')}",
            )
        if not matched:
            return Evidence("process", "mismatch", "process start identity differs from the run record")
        return Evidence("process", "match", "pid and process start identity matched")

    def jj(self, task):
        workspace = Path(task["jj"]["workspace_path"])
        try:
            if not workspace.exists():
                return Evidence("jj", "missing", "recorded jj workspace is absent")
            if workspace.is_symlink() or not workspace.is_dir():
                return Evidence("jj", "mismatch", "recorded jj workspace path is not its directory")
            identity = self.jj_adapter.inspect_workspace(
                workspace, task["jj"]["workspace_name"], require_empty=False,
            )
        except OSError as exc:
            return Evidence(
                "jj", "unavailable",
                f"jj workspace evidence unavailable: {_safe_detail(exc, 'filesystem failure')}",
            )
        except JjError as exc:
            detail = _safe_detail(exc, "adapter failure")
            unavailable = any(marker in str(exc).casefold() for marker in (
                "invocation failed", "timed out", "bounded adapter limit", "not utf-8",
            ))
            return Evidence(
                "jj", "unavailable" if unavailable else "mismatch",
                f"jj workspace evidence {'unavailable' if unavailable else 'mismatched'}: {detail}",
            )
        if identity.change_id != task["jj"]["change_id"]:
            return Evidence("jj", "mismatch", "jj workspace change identity differs from the task record")
        return Evidence("jj", "match", "jj workspace registration and change identity matched")

    def event(self, task, run):
        if self.config is None:
            return Evidence(
                "event", "unavailable",
                "Control configuration was not supplied to the event adapter",
            )
        try:
            snapshot = read_snapshot(self.config, run["run_id"])
        except EventError as exc:
            return Evidence(
                "event", "unavailable",
                f"event snapshot unavailable: {_safe_detail(exc, 'snapshot failure')}",
            )
        if snapshot is None:
            return Evidence("event", "missing", "no event snapshot exists for this run")
        if snapshot["task_id"] != task["task_id"]:
            return Evidence(
                "event", "mismatch",
                "event snapshot task identity differs from the task record",
            )
        if snapshot["state"] is None:
            return Evidence(
                "event", "missing", "session-start carries no semantic state",
            )
        if snapshot["state"] in _AGEABLE_EVENT_STATES:
            age = self._snapshot_age_seconds(snapshot)
            window = self.config.event_staleness_seconds
            if age is not None and age > window:
                # Past the recency window an in-progress state is no longer
                # trustworthy: a harness with no wired stop event would otherwise
                # report `working` forever.  Preserve the matched semantic state
                # while marking it stale so precedence can decline to trust it.
                return Evidence(
                    "event", "match",
                    f"stale {snapshot['state']} snapshot: observed {int(age)}s ago exceeds "
                    f"the {window}s recency window",
                    state=snapshot["state"],
                    stale=True,
                )
        return Evidence(
            "event", "match",
            f"verified {snapshot['event']} event snapshot",
            state=snapshot["state"],
        )

    def _snapshot_age_seconds(self, snapshot: dict[str, Any]) -> float | None:
        raw = snapshot.get("observed_at")
        if not isinstance(raw, str):
            return None
        try:
            observed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        return (self._now() - observed).total_seconds()


def _terminal_from_stored(run: dict[str, Any]) -> str | None:
    return run["state"] if run["state"] in _TERMINAL_STATES else None


def _reconcile_run(task: dict[str, Any], run: dict[str, Any], adapters: Adapters) -> dict[str, Any]:
    gathered: list[Evidence] = []

    def add(item: Evidence, expected: str) -> Evidence:
        if not isinstance(item, Evidence) or item.source != expected:
            raise ValueError(f"{expected} adapter returned invalid evidence")
        # Revalidate and copy frozen instances because a hostile adapter can
        # still use object.__setattr__ after returning them.
        safe = Evidence(item.source, item.outcome, item.detail, item.state, item.stale)
        gathered.append(safe)
        return safe

    def task_copy() -> dict[str, Any]:
        return copy.deepcopy(task)

    def run_copy() -> dict[str, Any]:
        return copy.deepcopy(run)

    def result(state: str, blocker: str | None = None) -> dict[str, Any]:
        return {
            "contract": "asha.control-run-reconciliation.v1",
            "run_id": run["run_id"],
            "state": state,
            "blocker": blocker,
            "evidence": [asdict(item) for item in gathered],
        }

    def unresolved_stale() -> dict[str, Any]:
        unavailable = ", ".join(
            item.source for item in gathered
            if item.source in {"tmux", "process", "jj"} and item.outcome == "unavailable"
        )
        return result(
            "stale",
            f"identity: stored stale state remains unresolved because {unavailable} evidence is unavailable",
        )

    tmux = add(adapters.tmux(task_copy(), run_copy()), "tmux")
    if tmux.outcome == "mismatch":
        return result("stale", f"tmux: {tmux.detail}")

    process = add(adapters.process(task_copy(), run_copy()), "process")
    if process.outcome == "mismatch":
        return result("stale", f"process: {process.detail}")
    if tmux.outcome == "missing" and process.outcome != "missing":
        return result("stale", f"tmux: {tmux.detail}")

    jj = add(adapters.jj(task_copy()), "jj")
    if jj.outcome in {"mismatch", "missing"}:
        return result("stale", f"jj: {jj.detail}")

    if run["state"] == "stale":
        # A stored ownership conflict is cleared only by affirmative identity
        # evidence at every higher-precedence seam.  Semantic events cannot
        # repair missing or unavailable ownership evidence.
        for identity in (tmux, process, jj):
            if identity.outcome in {"missing", "mismatch"}:
                return result("stale", f"{identity.source}: {identity.detail}")
        if any(identity.outcome == "unavailable" for identity in (tmux, process, jj)):
            return unresolved_stale()

    event = add(adapters.event(task_copy(), run_copy()), "event")
    if event.outcome == "mismatch":
        return result("stale", f"event: {event.detail}")
    stored_terminal = _terminal_from_stored(run)
    if stored_terminal is not None:
        if process.outcome == "match":
            return result("stale", "process: live process contradicts stored terminal state")
        if (event.outcome == "match" and event.state in _TERMINAL_STATES and
                event.state != stored_terminal):
            return result("stale", "event: state contradicts stored terminal state")
        return result(stored_terminal)
    if process.outcome == "missing":
        if process.state in _TERMINAL_STATES:
            return result(process.state)
        if event.outcome == "match" and event.state in _TERMINAL_STATES:
            return result(event.state)
        if event.outcome == "match":
            return result("stale", "event: active state contradicts missing process")
        return result("stale", "process: missing without verified terminal event")
    if (tmux.state == "needs-input" and process.outcome == "match"
            and not (event.outcome == "match" and event.state in _TERMINAL_STATES)):
        # A prompt on the live, owned screen is more current than any event
        # snapshot: the hook that would have reported it never fires for this
        # harness, and the last event only says what happened before it.
        return result("needs-input")
    if event.outcome == "match" and event.state is not None:
        if process.outcome == "match" and event.state in _TERMINAL_STATES:
            return result("stale", "event: terminal state contradicts matched live process")
        if event.stale:
            return result(
                "unknown",
                f"event: {event.state} snapshot exceeds the recency window; "
                "state is not trusted without live confirmation",
            )
        unavailable = ", ".join(
            item.source for item in (tmux, process)
            if item.outcome == "unavailable"
        )
        if unavailable and event.state not in _TERMINAL_STATES:
            return result(
                "unknown",
                f"{unavailable}: evidence unavailable; event state not trusted "
                "without live process evidence",
            )
        return result(event.state)
    if process.outcome == "match":
        if event.outcome == "unavailable":
            return result("unknown")
        if event.outcome == "missing":
            if run["state"] in _PROVEN_ACTIVE_STATES:
                return result(run["state"])
            if run["state"] == "stale":
                return result("starting")
        return result("starting")
    return result(run["state"])


_AGGREGATE_ORDER = {
    "stale": 8,
    "needs-input": 7,
    "working": 6,
    "starting": 5,
    "unknown": 4,
    "idle": 3,
    "failed": 2,
    "exited": 1,
}


def reconcile_task(
    task: dict[str, Any], adapters: Adapters | None = None, *, creation: Any = _CREATION_UNSET,
) -> dict[str, Any]:
    """Return a derived snapshot.  Neither registry nor external state changes."""
    validate_task(task)
    snapshot = copy.deepcopy(task)
    adapters = adapters or UnavailableAdapters()
    runs = [_reconcile_run(snapshot, run, adapters) for run in snapshot["runs"]]
    if runs:
        primary = max(runs, key=lambda item: _AGGREGATE_ORDER[item["state"]])
        state = primary["state"]
        blocker = primary["blocker"]
        evidence = primary["evidence"]
    elif creation is not _CREATION_UNSET:
        creation_evidence: list[dict[str, Any]] = []
        if creation is None:
            state = "stale"
            blocker = "creation journal is missing for a pre-launch task"
        elif not isinstance(creation, dict) or not isinstance(creation.get("phase"), str):
            state = "stale"
            blocker = "creation journal is invalid or unreadable"
        else:
            phase = creation["phase"]
            workspace_present = creation.get("workspace_present")
            workspace_match = creation.get("workspace_match", True)
            live_outcome = creation.get("live_outcome")
            live_detail = creation.get("live_detail")
            if live_outcome in {"match", "missing", "mismatch", "unavailable"}:
                creation_evidence.append(asdict(Evidence(
                    "jj", live_outcome,
                    live_detail or "prepared workspace live evidence has no detail",
                )))
            if phase == "ready-for-launch" and live_outcome in {
                "missing", "mismatch", "unavailable",
            }:
                state = "stale"
                blocker = f"jj: {live_detail}"
            elif phase == "ready-for-launch" and workspace_present is False:
                state = "stale"
                blocker = "prepared task workspace is missing"
            elif phase == "ready-for-launch" and workspace_match is False:
                state = "stale"
                blocker = "prepared task workspace ownership no longer matches its journal"
            elif phase == "ready-for-launch":
                state, blocker = "creating", None
            elif phase == "rolled-back" and workspace_present is True:
                state = "stale"
                blocker = "rolled-back task workspace reappeared"
            elif phase == "rolled-back":
                state = "failed"
                blocker = "pre-launch creation was rolled back"
            elif phase == "preserved":
                state = "stale"
                blocker = "pre-launch creation was preserved after an ownership ambiguity"
            elif phase == "launch-attempted":
                state = "stale"
                blocker = "launch was attempted but no run identity was recorded"
            else:
                state = "stale"
                blocker = f"creation interrupted at durable phase {phase}"
        evidence = creation_evidence
    else:
        state = snapshot["lifecycle"]
        blocker = None
        evidence = []
    if (snapshot["lifecycle"] == "failed" and state != "stale" and
            creation is _CREATION_UNSET):
        state = "failed"
        blocker = "task lifecycle failed; preserved resources require explicit recovery"
    return {
        "contract": "asha.control-reconciliation.v1",
        "task_id": snapshot["task_id"],
        "state": state,
        "blocker": blocker,
        "evidence": evidence,
        "runs": runs,
    }
