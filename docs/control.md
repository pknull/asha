# Asha Control

This is the current operating guide for Asha Control. The delivered proposal
under `docs/proposals/` preserves the design history; live code and this guide
describe the supported release.

## Purpose and prerequisites

Control turns one unit of agent work into a persistent local task. A task owns
a versioned registry record, an explicit-base jj workspace and change, a
detached tmux session, and its harness runs. Closing the TUI, popup, or shell
does not end the task.

The source must already be an initialized Asha project and the exact root of a
Git-backed jj repository. Control targets the jj 0.38 workspace surface and
requires tmux with `display-popup`, plus the selected installed harness. The
source revision must ignore `.asha/`, `Memory/`, and `Work/` paths that Control
copies privately into the task workspace. GitHub source modes additionally
require an installed, authenticated `gh`; ordinary ad-hoc tasks do not.

`asha task doctor` reports these local capabilities. Its `gh` probe is always
shown but is optional and never blocks ad-hoc task creation.

## Command surface

```text
asha task start [--repo PATH] (--pr N | --issue N | [--base REVSET])
                [--harness H|--agent H] (--goal TEXT | -- TEXT...)
                [--role ROLE] [--detach] [--json]
asha task list [--json]
asha task show <task-id|exact-slug> [--json]
asha task attach <task-id|exact-slug> [--run RUN_ID]
asha task stop <task-id|exact-slug> [--terminate]
asha task archive <task-id|exact-slug>
asha task unarchive <task-id|exact-slug>
asha task recover <task-id|exact-slug>
asha task reconcile [task-id|exact-slug] [--json]
asha task doctor [--json]

asha control
asha control tmux
asha control event ...       internal hook-facing route
```

`--repo` defaults to the jj repository containing the current directory.
`--harness` defaults to Asha's configured harness; `--agent` is its CLI alias.
Without `--detach`, a start inside tmux opens the new session in a popup. From
outside tmux it prints the exact attach command. `--json` keeps stdout to one
versioned machine-readable result, implies `--detach`, and includes the exact
attach command in its payload.

Control-managed Codex launches pass a per-launch trust override for the
workspace root so a new task does not stop at Codex's directory-trust prompt.
The override applies only to that process and never edits the Codex trust store
or `~/.codex/config.toml`.

Exit codes: 0 success, 2 usage/refusal, 1 internal error, 130 interrupted.

A goal is mandatory in every mode and is the only instruction authority.
`--pr` and `--issue` provide source context, never a prompt. `--pr` conflicts
with both `--issue` and `--base`. `--issue` may be paired with `--base`; without
one it uses `trunk()`.

## Task and run model

A task is the durable container. Its lifecycle is `creating`, `running`,
`ended`, `failed`, or `archived`. The initial release launches one primary
mutating run. Runs carry a harness, role, tmux pane, verified process identity,
and current evidence state. A task may outlive the launching CLI, TUI, popup,
and ordinary tmux clients.

Names are display aids, not ownership. UUIDs, record digests, jj identities,
tmux user options, and live process facts establish ownership. GitHub task
slugs are derived only from repository name, source kind, and number, such as
`thallus-pr-34`; GitHub titles never enter a slug, prompt, tmux value, harness
argv, or record.

## jj contract

Control accepts only the canonical jj repository root and a usable Git
backend. Base resolution is deterministic:

1. `--pr N` uses the fetched immutable PR head.
2. `--base REVSET` must resolve to exactly one full commit ID.
3. `--issue N` or ad-hoc work without `--base` resolves `trunk()`.
4. Missing, ambiguous, or invalid commits refuse creation.

The task record stores both the human request (`PR #N head` or the literal
revset) and the resolved full commit ID. Before mutation, Control pins the
repository's full 128-hex operation ID. It then runs the jj 0.38 equivalent of
`workspace add --revision RESOLVED_COMMIT --message GOAL` at that operation,
creating one new empty working-copy change on the base. Source reads use
`--ignore-working-copy`; no controller path snapshots or moves the source `@`.

Control records the new change and working commit IDs. It does not create or
move a bookmark, integrate the change, push it, or remove the workspace.
Explicit diff refresh may snapshot the task workspace; background list and
reconciliation reads do not.

## tmux contract

Each task receives one detached controller-owned session with a stable `work`
window and primary pane. The operator's ordinary sessions remain separate.
The task pane uses `remain-on-exit`, so completed output remains inspectable.
Pane titles and window names are controller-generated restricted values;
automatic rename and terminal title escapes are not trusted.

Ownership is repeated in tmux user options:

```text
session: @asha_managed @asha_task_id @asha_repo @asha_workspace
         @asha_change @asha_state
pane:    @asha_run_id @asha_harness @asha_role @asha_state
server:  @asha_summary
```

Attach, stop, and other targeted operations require the registry identity and
tmux options to agree. A foreign session with the same readable name is never
adopted, signaled, renamed, or killed. `asha control tmux` prints an optional
format snippet and does not edit the user's tmux configuration.

## State and XDG locations

Defaults follow XDG paths:

```text
${XDG_STATE_HOME:-~/.local/state}/asha/control/tasks/<task-id>.json
${XDG_DATA_HOME:-~/.local/share}/asha/workspaces/<repo-key>/<task-slug>/
${XDG_RUNTIME_DIR:-/tmp/user-$UID}/asha-control/
${XDG_RUNTIME_DIR:-/tmp/user-$UID}/asha-control/events/<run-id>.json
```

`control.workspace_root` in `~/.asha/config.json` may replace the data-path
default. It cannot be `/`, `$HOME`, the source, below the source, or an ancestor
of the source. Existing path components must be canonical directories without
symlink aliases or unsafe writable ancestry.

Task records use the `asha.control-task.v1` schema:

| Object | Fields |
|---|---|
| Task | contract, task UUID, restricted slug, operator goal label, timestamps, lifecycle |
| Repository | canonical root and stable derived identity |
| Source | exactly `kind`, `number`, and `url`; kind is `ad-hoc`, `pr`, or `issue` |
| jj | workspace name/path, requested base, resolved base commit, change ID, working commit ID |
| tmux | socket identity, session, and window |
| Runs | run UUID, harness, role, pane, PID plus process-start identity, harness session, state, evidence, timestamp |

The source title is not part of the schema. Records never contain the full
prompt, transcripts, terminal capture, tool arguments, hook bodies, or
secrets. Registry and event snapshots are bounded, private, atomically
replaced, and reject symlinked or malformed state.

## Status and evidence

Run states use a shared vocabulary:

| State | Meaning |
|---|---|
| `starting` | The pane and process exist, but no semantic start evidence is available. |
| `working` | A verified harness event reports active work. |
| `needs-input` | A verified event reports an operator decision or permission requirement. |
| `idle` | A verified stop or completed-turn event occurred while the process remains live. |
| `exited` | The verified process ended normally. |
| `failed` | Launch or termination has verified failure evidence. |
| `unknown` | The process is live but the harness has no proven semantic event seam. |
| `stale` | Registry, tmux, process, event, or jj identities disagree. |

Reconciliation prefers live owned tmux evidence, verified process identity and
ancestry, live jj workspace identity, recent supported event evidence, then
stored lifecycle. Missing or conflicting evidence produces `unknown`, `stale`,
or an explicit blocker; reconciliation never mutates external state to make an
old record appear current.

"Recent" is enforced, not decorative. An in-progress event state (`working`,
`needs-input`) is trusted only while its snapshot is newer than
`control.event_staleness_seconds` (default `1800`). Past that window a live
process reconciles to `unknown` rather than a stale positive, because a harness
with no wired stop event (Codex today) never supersedes an in-progress
snapshot. `idle` (a completed turn) is a legitimate resting state and is not
aged; `exited` and `failed` are durable facts and never age.

The Increment 4 live-probed semantic claims are:

| Control event | Claude Code | OpenAI Codex |
|---|---|---|
| `session-start` | Wired from `SessionStart` | Wired from `SessionStart` |
| `prompt-submitted` | Wired from `UserPromptSubmit` | Wired from `UserPromptSubmit` |
| `tool-completed` | Wired from `PostToolUse` | Wired from `PostToolUse`; interception is known incomplete for `unified_exec` |
| `permission-requested` | Not claimed. `Notification` is multi-purpose and its payload is unverified. | Not claimed. `PermissionRequest` exists in Codex's allowlist but has no live-probe evidence and is trust-gated. |
| `turn-stopped` | Wired from `Stop` | Not claimed. `Stop` exists in Codex's allowlist but has no live-probe evidence and is trust-gated. |
| `session-ended` | Wired from `SessionEnd` | Codex has no equivalent event. |

Copilot and OpenCode provide process liveness only; no semantic Control event
is claimed for either. Event hooks receive opaque task/run identifiers, discard
prompt and tool bodies, and write one bounded current snapshot. Event delivery
is local, network-free, observational, and fail-open.

## GitHub source resolution

GitHub access is read-only. `gh auth status` distinguishes a missing CLI from
an installed but unauthenticated one. Metadata calls request only these fields:

```text
gh pr view N --json number,title,url,headRefOid,state,isDraft,isCrossRepository
gh issue view N --json number,title,url,state
```

All metadata is bounded and validated. Object IDs must be full lowercase Git
IDs; titles and URLs reject Unicode control characters. A title is printed
once as transient display text and discarded.

PR mode performs and reports exactly these repository mutations:

1. `git -C GIT_ROOT fetch origin
   pull/N/head:refs/remotes/origin/asha-control-pr-N` adds the fetched objects
   and updates one controller-owned remote-tracking ref.
2. `jj -R SOURCE --ignore-working-copy git import` records the Git import in
   the jj operation log and surfaces the head as an untracked *remote*
   bookmark.
3. Task preparation creates the separate task workspace and empty change on
   the validated `headRefOid`.

The remote-tracking namespace is required, not incidental: jj only surfaces
refs from namespaces it tracks, so a commit reachable solely through a custom
namespace such as `refs/asha-control/*` never enters jj's commit graph and the
explicit-base rule could not be satisfied for a PR head jj does not already
know. Importing a remote-tracking ref creates an untracked *remote* bookmark
only — your local bookmark namespace gains no controller entry, and no existing
bookmark of either kind moves.

The fetch never checks out a tree and never changes Git `HEAD`, staged content,
source `@`, or bookmark positions. Issue mode performs only its bounded
`gh issue view` read and resolves the explicit base or `trunk()`; it does not
fetch.

Control has no GitHub write route. It does not comment, edit, label, close,
review, merge, create a PR, or push. Subprocesses are argv-only, shell-free,
deadline-bound, and byte-capped.

## Data preservation

Control has no workspace deletion command. Archive requires an ended task or a
running task whose reconciled runs are all terminal (`exited` or `failed`) and
unblocked. At that terminal edge Control persists the reconciled run state and
bounded evidence, then changes only the task's registry lifecycle. Archive is
reversible with `asha task unarchive <selector>`; the jj workspace, change,
ignored files, tmux history, and source repository remain. Stop verifies task,
pane, process start identity, and tmux ancestry, then sends `SIGINT` or explicit
`SIGTERM` to that process only. It does not kill the tmux session, archive the
task, or touch jj.

Before a harness process may have started, transaction rollback removes only
artifacts whose exact ownership was journaled and reverified. After launch is
possible, every failure path preserves the workspace and records recovery
facts. Control never substitutes raw recursive deletion, destructive Git, or
unreviewed jj abandonment for a removal design.

### Interrupted creation

Ctrl-C and SIGTERM during `asha task start` run the same rollback-or-preserve
handler before the original interruption reaches the CLI. A hard process exit
can still leave a durable `creating` record and creation journal. Recover it
explicitly with:

```text
asha task recover <task-id|exact-slug>
```

Pre-launch phases remove only reverified transaction-owned artifacts. A phase
at or after `launch-attempted` never kills the session or process: Control marks
the task failed, preserves the workspace, reports the exact attach/show
commands, and requires the operator to stop any live harness manually. The
`transactions` doctor probe names interrupted creation records and the command
for each.
