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
| `asha.control-task-prune.v1` | `cli.py` prune route via `lib/control/prune.py` `prune_task` (added 2026-08-18, additive) | `contract`, `dry_run`, `results[]` each `{task_id, slug, outcome, session{action, detail}, workspace{action, detail}, bindings[]}` with outcome in `pruned`, `planned`, `partial`, `refused`, `nothing-to-prune`; session action in `killed`, `would-kill`, `absent`, `refused`, `kept`; workspace action in `removed`, `would-remove`, `forgotten`, `would-forget`, `absent`, `refused`, `kept`; optional `orchestration_bindings_error` | closed |
| `asha.control-doctor.v1` | `lib/control/doctor.py` `run_doctor` | `contract`, `ok`, `probes[]` `{name, outcome, detail}` with outcome in `match`, `mismatch`, `missing`, `unavailable`, `limitations[]` | closed |

Within the existing open `jj-operation` item shape,
`operation: "git init --colocate"` reports automatic plain-Git repository
enablement. Its detail states that verified colocation is retained. This adds
no top-level field to the closed start payload. A later task failure or
cancellation does not erase or conceal that source mutation; ambiguous partial
initialization is likewise preserved for operator inspection.

`asha.control-colocation-intent.v1` is an internal recovery/authentication
record, not an orchestration payload. It is stored below Control's private
state directory, keyed by canonical repository root, and binds the root plus a
strict `git_binding`. A directory marker is inode-bound; a regular `gitdir:`
marker also binds its bounded content digest, parsed canonical target, and
target inode/type/mode/owner. `verified` additionally binds a non-symlink `.jj`
directory. `intent` is durable before mutation; `verified` is durable only
after semantic source comparison. A remaining `intent`, stale binding, or
binding mismatch makes later task starts refuse rather than silently adopt a
usable but unauthenticated `.jj`. Interruption retains exit 130 after printing
the ambiguous-state diagnostic; ordinary repository refusals remain exit 2.
The only automatic binding update is a typed `verified` root-hardening case:
same canonical root/dev/inode/uid/type, exact Git marker/target and `.jj` facts,
and only a nonempty subset of `0022` removed from the root mode. Task start
must first pass source/Memory/base/destination/capacity policy, strict jj
identity and sync, stable all-ref Git semantics, and stable jj operation
identity. It then rechecks the exact raw bytes/digest and rewrites only
`root_fact` under the exclusive source lock shared by every Control intent
writer. Doctor classifies the case read-only as repairable only when the
resulting root passes path policy. Every other mismatch, including state
`intent`, mode loosening, a mixed change, or a cooperative writer race,
preserves the record bytes and refuses. This is deliberately not described as
filesystem-atomic CAS: a noncooperating same-UID process able to rename private
source/state paths is outside the enforcement boundary.

`status` and `state` values everywhere use the run vocabulary from
`docs/control.md` (`starting`, `working`, `needs-input`, `idle`, `exited`,
`failed`, `unknown`, `stale`); task `lifecycle` is `creating`, `running`,
`ended`, `failed`, `archived`.

## Records

| Contract | Producer | Notes |
|---|---|---|
| `asha.control-task.v1` | `lib/control/model.py` `validate_task` (keys `_TASK_KEYS`) | Exact keys: `contract, task_id, slug, label, created_at, updated_at, lifecycle, repository{root, identity}, source{kind, number, url}, jj{workspace_name, workspace_path, requested_base, base_commit_id, change_id, working_commit_id}, tmux{socket, session, window}, runs[]`. Timestamps are bounded ASCII RFC3339 UTC with `Z`. Not extended by create-by-id. Amendment 2026-08-18: `archived` additionally admits an empty `runs` list (a rolled-back creation archived out of the working list); no key changed. |
| `asha.control-run.v1` | `model.py` `validate_run` (`_RUN_KEYS`) | Exact keys: `contract, run_id, harness, role, pane_id, pid, process_start_identity, harness_session_id, state, evidence, evidence_at`. |
| `asha.control-reconciliation.v1` | `lib/control/reconcile.py` `reconcile_task` | `contract, task_id, state, blocker, evidence[], runs[]`; each run is `asha.control-run-reconciliation.v1` `{contract, run_id, state, blocker, evidence[]}`; each evidence item is `{source, outcome, detail, state, stale}` with source in `tmux, process, jj, event` and outcome in `match, mismatch, missing, unavailable`; `state` is set on matched `event` evidence, on `missing` `process` evidence (terminal only), and on matched `tmux` evidence as `needs-input` when the owned pane shows a harness input prompt or as terminal when the exact owned pane is conclusively dead. Missing, foreign, or unavailable pane evidence is not terminal. Derived on every read; persisted into the task record only at a terminal edge (archive, or any reconciliation whose runs are all `exited`/`failed` and unblocked, which also expires those runs' event snapshots). |
| `asha.control-event.v1` | `lib/control/events.py` (`write_snapshot`/`read_snapshot`), written by `plugins/session/hooks/handlers/control-event.sh` | Exact keys: `contract, task_id, run_id, event, state, harness, harness_session_id, exit_status, pane_id, observed_at`; events `session-start, prompt-submitted, tool-completed, permission-requested, turn-stopped, session-ended`; `exit_status` only with `session-ended`. One bounded (4 KiB) current snapshot per run under `$XDG_RUNTIME_DIR/asha-control/events/<run-id>.json`. Trust window: `control.event_staleness_seconds` (default 1800) for in-progress states. |
| `asha.control-task-context.v1` | `lib/control/prepare.py` via `plugins/session/tools/control_task_marker.py` | The `.asha/control-task.json` marker inside a task workspace: `contract, task_id, repository{root, identity}, jj{workspace_name, workspace_path, change_id, working_commit_id}`; canonical bytes are sorted-key compact JSON + `\n`. |
| `asha.control-creation-journal.v2` | `lib/control/transaction.py` | Current internal recovery journal. It binds a compact `asha.control-materialization-plan.v1` summary to a private `asha.control-materialization-ownership.v1` fixed-width sidecar; tracked paths and contents are not embedded in the JSON. New exact post-add operation IDs and the optional `asha.control-recovery-adoption.v1` object are additive internal v2 fields; older v2 records remain readable. Once any workspace/root filesystem mutation may exist, automatic recovery retains the jj registration, every workspace entry, the root, and created parents, then records `preserved`; it performs no name-based forget or filesystem deletion. Diagnostics require `jj workspace list` plus path inspection and name archive/confirmed prune only when existing prune preconditions are durably proven; partial-add and parent residue require manual cleanup. Not an orchestration surface. |
| `asha.control-creation-journal.v1` | `lib/control/transaction.py` | Legacy inline-tree recovery journal. It remains strictly readable and recoverable under its frozen ownership-checked automatic-removal behavior but is not produced for new tasks. Not an orchestration surface. |
| `asha.control-prune-record.v1` | `lib/control/prune.py` `PruneRecordStore` | Internal removal journal for a task's workspace root (`task_id, recorded_at, workspace_removed` false at intent and true at completion, `workspace_path, workspace_name, root_fact{dev, ino, uid}, entries_removed`); consulted so a repeat prune never re-matches a reused inode and can finish an interrupted removal, and by the orchestration seal-drift reconciler to recognize a pruned sealed workspace. Not an orchestration surface. |

`asha.control-recovery-adoption.v1` is not a general task transition or public
response contract. It is an optional v2 journal object for one exact
failed/runless/preserved/add-intent/no-launch retained creation. It binds the
original task and journal digests, verified-colocation digest, explicit
harness/role/exact-goal authorization, root fact, registration change/commit,
workspace-add and checkout operation IDs, and context-plan digest. Its states
advance `intent -> context-provisioning -> context-provisioned ->
ready-for-launch`. Only a matching ready record admits the internal
`failed -> creating` completion edge; the general lifecycle transition table
is unchanged. The controller takes the task, source, then repository locks,
rechecks the exploratory task/journal bytes under all three, and never forgets
or deletes during adoption. Its immutable context proof binds every exact
generated plan path plus the whole fixed `Work/session-state/` private subtree;
Git's repository, global, and default excludes cannot supply coverage.

## Task record digest

`lib/control/store.py` `task_digest(task)` = SHA-256 hex of
`json.dumps(validate_task(task), ensure_ascii=False, sort_keys=True,
separators=(",", ":")).encode("utf-8")`. It is the optimistic-concurrency
token every store update must present (`expected_digest`) and the value an
orchestration sidecar binds to a Control task snapshot. Because it is computed
over the validated record, any field addition to `asha.control-task.v1` would
change every digest; that is one more reason the record is not extended.
`asha task prune` honours this: it kills a dead session, forgets and removes a
workspace, and leaves the archived record and its digest untouched.

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
Task, source, and repository locks use separate deterministic physical-key
domains through `TransactionCoordinator`; a caller UUID can never alias a
source or repository lock. The enforced nesting order is task, then source,
then repository. Lock files are internal coordination artifacts, not durable
record identities.
Read-only Git/jj root selection may precede that lock, but plain-Git
colocation may not. Existing-task replay and interrupted-journal refusal invoke
neither `jj git init`, fetch, import, workspace preparation, tmux, nor a
harness.
For a new plain-Git task, those replay decisions are followed under the source
lock by the read-only source/workspace policy, published Memory, prospective
destination/capacity, PR remote selection, and base checks. An explicit
ad-hoc/issue base is resolved with exact config-sanitized Git and its immutable
OID is carried through import; existing jj revset behavior is unchanged. An
omitted initial plain-Git base selects one unambiguous remote-default ref or
the first existing local `main`, `master`, or `trunk`. The selected ref is
preflight evidence only: `requested_base` retains the caller's unchanged
default expression and `base_commit_id` carries the immutable selected OID.
This keeps identical caller-ID replay stable and makes an explicit different
base a mismatch. If no candidate exists, start refuses before colocation and
requests explicit `--base`.

The pre-enable plan binds the source root and complete Git marker/target facts,
then revalidates that binding before intent creation, immediately before
colocation, and afterward. Transaction Git reads use a trusted absolute Git
executable with a minimal explicit environment, exact git-dir/work-tree, and
execution-capable read helpers disabled. Semantic authentication uses plumbing
index entries with normalized per-stage flags, ref object IDs plus symbolic
targets, and descriptor-checked raw filesystem hashes for changed tracked and
bounded untracked paths; it never invokes Git status, diff,
attributes filters, or repository filter helpers. PR metadata plus one
identity-matching HTTPS/SSH URL and its exact local-config digest are carried
across enablement. The fetch uses the URL rather than a named remote, disables
credential prompts/helpers and unsafe protocols, and refuses config drift.
Repositories using `extensions.worktreeConfig` refuse this automatic fetch:
the split local-config plane is not covered by the single carried digest.
Concurrent Control writers serialize on the source lock. A
noncooperating same-UID process capable of renaming the private source/state
paths is outside that path-based boundary.

The optional public `--slug` parameter is path identity, not task label or
goal. It uses the stored slug grammar, refuses the reserved `materializations`
namespace, and participates in explicit create-by-ID replay comparison when
present. It adds no field to `asha.control-task.v1`. Terminal retry uses a fresh
UUID and bounded `<old-slug>-retry-<uuidhex>` slug (all 128 UUID bits),
reconstructs the recorded
request, and leaves the earlier record unchanged. A PR retry resolves the
current PR head by design.

The human TUI defaults to active records. Its all-history view projects an
archived record directly as `archived`, without live reconciliation. Context
and explicit refresh re-read lifecycle under the same task lock that guards
the following live reconciliation; a concurrent archive therefore wins and is
projected without an adapter call. Context ownership is the unique immutable
orchestration link across all initiative and
attempt states; missing is unowned, while duplicate, skipped, malformed, or
identity-mismatched state refuses. A reservation and link observed in one
lock-free scan must name the same initiative and attempt. An attempt reservation
for the task without its durable link is ambiguous and also refuses. Initiative reconciliation uses the shared
CLI sequence (`reconcile_actions`, `reconcile_live`, `reconcile_nodes`) and
never initiates a new dispatch; its report includes actual reconciled action
IDs/states because an already-authorized indeterminate dispatch may be replayed.
For unowned active runs, TUI signal availability is closed by fresh evidence:
starting/working/needs-input allow SIGINT or SIGTERM, idle allows Finish via
SIGTERM, and unknown allows SIGTERM. Exact-lowercase confirmation precedes a
second ownership/run/state read and the shared locked `stop_task` call. Owned
active runs instead submit one `stop-attempt` action with actor `tui` after a
second all-state binding lookup; they never call `stop_task` or broad reconcile
from that menu action. TUI prune uses the same one-task `prune_task` assembly as the
CLI, with a dry preview and separately confirmed, freshly revalidated real
call. Unreadable prune history is a bounded, fail-closed action error rather
than a curses-session failure.

The start editor freezes bounded display candidates when opened: current/newest
repositories, repository-specific recorded bases, the closed harness allowlist
with installed status, and observed roles. The snapshot is convenience only;
the ordinary controller remains authoritative. One grapheme/cell-aware modal
frame owns start fields, action choices, and exact confirmations, with at most
8 visible candidates under one aggregate 128-entry and 256 KiB raw/display
identity budget. Raw candidate identity controls matching and submission while
sanitized text is presentation only. Confirmation safety facts are structured,
wrapped context above a short active input label rather than one crop-prone
prompt string. Normal 24x80 and 24x120 views show the action/task/run identity,
signal, preservation statement, exact-`yes` instruction, input, and cursor
together; smaller views retain input and expose an omission marker. Resize and Shift-Tab do
not alter logical field values, and Escape before submission creates no worker.
The shared terminal-text gate requires every ordinary visible code point to be
terminal-printable at editor, task-model, harness-argv, and tmux-argv seams.
U+2028/U+2029 and Unicode noncharacters refuse; ZWJ, variation selectors,
keycaps, modifiers, and regional indicators are admitted only inside the
explicit complete cluster grammar.

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
The selected tree is obtained by one bounded metadata-only Git read and each
materialized file is verified by streaming its Git object hash. Large blobs
and aggregate tracked bytes are not constrained by the legacy inline-journal
content limits; no per-blob Git subprocess is used.

## Not contracts

tmux user options (`@asha_*`), session/window/pane names, workspace directory
layout under `${XDG_DATA_HOME}/asha/workspaces/`, and the human TUI are
presentation and ownership aids. Orchestration must read task state through
the payloads above, never through tmux or the filesystem.

The TUI's selected source, observation timestamp, and freshness are an internal
projection chosen atomically with the same state returned by
`asha.control-reconciliation.v1`. The observation state and reconciliation
state may not diverge. This metadata does not add keys to the v1 payload;
serialized evidence items remain exactly
`{source, outcome, detail, state, stale}`. Missing, unavailable, unreadable, or
expired semantic evidence cannot preserve an old positive state in the v1
result. A verified process plus a verified `missing` event outcome may preserve
`starting`; an `unavailable` event adapter cannot. Neither outcome preserves a
stored `working`, `needs-input`, or `idle` without current semantic evidence.
