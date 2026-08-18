# Asha Control v1 contracts (frozen)

Status: frozen 2026-08-17 at the end of the Control soak (runway Phase 4).
Every identifier below was re-read from live code on that date; the file
paths name the single producer of each contract. Orchestration Core binds to
these surfaces and to nothing else in Control.

## Freeze rule

- A `.v1` identifier is immutable: its field set, field grammars, and
  documented semantics change only under a new identifier (`.v2`) with the
  old one still produced until every consumer has moved.
- Consumers must ignore fields they do not know only where this document says
  a payload is *open*; every other payload is *closed* and adding a field is a
  version bump.
- Human (non-`--json`) output is never a contract.
- Exit codes are part of the CLI contract: 0 success, 2 usage/refusal,
  1 internal error, 130 interrupted.

## Command payloads (`--json`)

| Contract | Producer | Shape | Open? |
|---|---|---|---|
| `asha.control-task-start.v1` | `lib/control/cli.py` `_start_command_inner` | `contract`, `task` (a `asha.control-task.v1` record), `run` (its primary `asha.control-run.v1`), `workspace` `{name, path, change_id}`, `session`, `pane`, `attach` (exact tmux attach argv joined by `shlex.join`), `source_mutations` (list of `{kind, detail, …}` with `kind` in `fetched-objects`, `controller-ref`, `jj-operation`), `existing` (bool: create-by-id returned an already-registered task) | closed |
| `asha.control-task-list.v1` | `cli.py` list route via `lib/control/view.py` `task_summary` | `contract`, `tasks[]` each `{task_id, slug, label, lifecycle, status, updated_at, repository{root, identity}, run_count, blocker}`, optional `skipped[]` (unreadable records) | closed |
| `asha.control-task-show.v1` | `cli.py` show route | `contract`, `task` (record), `reconciliation` (`asha.control-reconciliation.v1`) | closed |
| `asha.control-reconcile-list.v1` | `cli.py` reconcile route | `contract`, `results[]` (`asha.control-reconciliation.v1`) | closed |
| `asha.control-doctor.v1` | `lib/control/doctor.py` `run_doctor` | `contract`, `ok`, `probes[]` `{name, outcome, detail}` with outcome in `match`, `mismatch`, `missing`, `unavailable`, `limitations[]` | closed |

`status` and `state` values everywhere use the run vocabulary from
`docs/control.md` (`starting`, `working`, `needs-input`, `idle`, `exited`,
`failed`, `unknown`, `stale`); task `lifecycle` is `creating`, `running`,
`ended`, `failed`, `archived`.

## Records

| Contract | Producer | Notes |
|---|---|---|
| `asha.control-task.v1` | `lib/control/model.py` `validate_task` (keys `_TASK_KEYS`) | Exact keys: `contract, task_id, slug, label, created_at, updated_at, lifecycle, repository{root, identity}, source{kind, number, url}, jj{workspace_name, workspace_path, requested_base, base_commit_id, change_id, working_commit_id}, tmux{socket, session, window}, runs[]`. Timestamps are bounded ASCII RFC3339 UTC with `Z`. Not extended by create-by-id. |
| `asha.control-run.v1` | `model.py` `validate_run` (`_RUN_KEYS`) | Exact keys: `contract, run_id, harness, role, pane_id, pid, process_start_identity, harness_session_id, state, evidence, evidence_at`. |
| `asha.control-reconciliation.v1` | `lib/control/reconcile.py` `reconcile_task` | `contract, task_id, state, blocker, evidence[], runs[]`; each run is `asha.control-run-reconciliation.v1` `{contract, run_id, state, blocker, evidence[]}`; each evidence item is `{source, outcome, detail, state, stale}` with source in `tmux, process, jj, event` and outcome in `match, mismatch, missing, unavailable`. Derived on every read; persisted into the task record only at a terminal edge (archive, or any reconciliation whose runs are all `exited`/`failed` and unblocked, which also expires those runs' event snapshots). |
| `asha.control-event.v1` | `lib/control/events.py` (`write_snapshot`/`read_snapshot`), written by `plugins/session/hooks/handlers/control-event.sh` | Exact keys: `contract, task_id, run_id, event, state, harness, harness_session_id, exit_status, pane_id, observed_at`; events `session-start, prompt-submitted, tool-completed, permission-requested, turn-stopped, session-ended`; `exit_status` only with `session-ended`. One bounded (4 KiB) current snapshot per run under `$XDG_RUNTIME_DIR/asha-control/events/<run-id>.json`. Trust window: `control.event_staleness_seconds` (default 1800) for in-progress states. |
| `asha.control-task-context.v1` | `lib/control/prepare.py` via `plugins/session/tools/control_task_marker.py` | The `.asha/control-task.json` marker inside a task workspace: `contract, task_id, repository{root, identity}, jj{workspace_name, workspace_path, change_id, working_commit_id}`; canonical bytes are sorted-key compact JSON + `\n`. |
| `asha.control-creation-journal.v1` | `lib/control/transaction.py` | Internal recovery journal; not an orchestration surface. |

## Task record digest

`lib/control/store.py` `task_digest(task)` = SHA-256 hex of
`json.dumps(validate_task(task), ensure_ascii=False, sort_keys=True,
separators=(",", ":")).encode("utf-8")`. It is the optimistic-concurrency
token every store update must present (`expected_digest`) and the value an
orchestration sidecar binds to a Control task snapshot. Because it is computed
over the validated record, any field addition to `asha.control-task.v1` would
change every digest; that is one more reason the record is not extended.

## Process environment given to a harness

`lib/control/harness.py` `controller_env`: `ASHA_CONTROL_TASK_ID`,
`ASHA_CONTROL_RUN_ID`, `ASHA_CONTROL_STATE_DIR`, `ASHA_CONTROL_MANAGED=1`.
Hook handlers and any future task-scoped result command key on exactly these.

## Create-by-id seam (orchestration prerequisite)

`asha task start --task-id <uuid>` creates the task under that identifier when
it is absent and, when it is already registered with the same parameters,
returns it unchanged with `existing: true` and no mutation. A registered task
with different parameters, or one whose creation was interrupted, is refused
with exit 2 (the latter names `asha task recover`). The check runs under the
per-task transaction lock before any source mutation.

## Controller-materialization library seam

`lib/control/prepare.py` exposes
`plan_materialization(config, source, name)` and
`prepare_materialization(config, source, base_commit_id, name)`. The planner
returns the deterministic repository identity, repository key, workspace name,
and workspace path without mutation so a caller can journal intent before
creation. Preparation creates one
fresh explicit-base jj workspace below
`<workspace-root>/<repo-key>/materializations/<name>` and returns the closed
Python mapping `workspace_name`, `workspace_path`, `change_id`, and
`working_commit_id`. It uses Control's operation pinning, jj adapter, namespace
and path validation, private modes, and retained journaling, but creates no
`asha.control-task.v1` or `asha.control-run.v1` record and invokes no harness or
tmux operation. This is a library seam, not a CLI or JSON contract.

## Not contracts

tmux user options (`@asha_*`), session/window/pane names, workspace directory
layout under `${XDG_DATA_HOME}/asha/workspaces/`, and the human TUI are
presentation and ownership aids. Orchestration must read task state through
the payloads above, never through tmux or the filesystem.
