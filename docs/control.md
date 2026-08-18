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
shown but is optional and never blocks ad-hoc task creation. Running it outside
a repository is also informational. Hook checks cover only the installed Claude
and Codex configurations, and live-event checks skip Copilot and OpenCode runs
because those harnesses claim process liveness only.

## Command surface

```text
asha task start [--repo PATH] (--pr N | --issue N | [--base REVSET])
                [--task-id UUID]
                [--harness H|--agent H] (--goal TEXT | -- TEXT...)
                [--role ROLE] [--detach] [--json]
asha task list [--json]
asha task show <task-id|exact-slug> [--json]
asha task attach <task-id|exact-slug> [--run RUN_ID]
asha task stop <task-id|exact-slug> [--terminate]
asha task archive <task-id|exact-slug>
asha task unarchive <task-id|exact-slug>
asha task recover <task-id|exact-slug>
asha task prune (<task-id|exact-slug>... | --all) [--keep-workspace]
                [--dry-run] [--yes] [--json]
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
attach command and an `existing` boolean in its payload.

Control-managed Codex launches pass a per-launch trust override for the
workspace root so a new task does not stop at Codex's directory-trust prompt.
The override applies only to that process and never edits the Codex trust store
or `~/.codex/config.toml`.

Exit codes: `0` success (and, for `task doctor`, all required checks matched);
`1` when doctor checks complete with `ok:false`, or on an internal error; `2`
usage/refusal; and `130` interrupted.

A goal is mandatory in every mode and is the only instruction authority.
`--pr` and `--issue` provide source context, never a prompt. `--pr` conflicts
with both `--issue` and `--base`. `--issue` may be paired with `--base`; without
one it uses `trunk()`.

## Terminal TUI

`asha control` opens the task supervisor in the current terminal. Use the up
and down arrow keys to move between tasks. The remaining keys are:

| Key | Action |
|---|---|
| `Enter` | Open the selected task and run in a tmux popup. |
| `n` | Open the task-start form. |
| `r` | Reconcile the selected task from live state. |
| `d` | Refresh and display a read-only jj diff summary. |
| `a` | After confirmation, archive the selected eligible task; preserve its workspace and change. |
| `/` | Filter the task list without mutating task state. |
| `q` | Exit the TUI without affecting tasks. |
| `?` | Toggle help for the keys, status evidence, and limitations. |

The `n` form prompts for repository, base, harness, role, and goal. Its defaults
are the current directory, `trunk()`, the configured default harness, and the
`implementer` role. It invokes the same controller validation as `asha task
start` and always supplies `--detach`, so creation does not replace the TUI
with the new task's session. Select the created task and press `Enter` to open
it. Press `Escape` at any prompt to cancel the form.

The TUI offers `a` only for a `running` or `ended` task that has runs and whose
reconciled runs are all `exited` or `failed`. For a running task, final archive
reconciliation also refuses any run blocker.

`Enter` selects the target pane and attaches to its persistent task session in
a popup. Closing the popup only detaches that popup client: it does not stop
the harness, archive the task, or alter the jj workspace or change. The TUI
then redraws.
The popup is bound to the client attached to the caller's own tmux session and
never opens on another client.

The TUI requires stdout attached to a TTY and importable curses support whose
`setupterm()` check succeeds. If any preflight check fails, `asha control`
writes this diagnostic to stderr and exits 2 without opening a curses screen:

```text
asha control: terminal TUI unavailable; use `asha task list --json` as the non-interactive fallback.
```

Use `asha task list --json` directly for scripts and other non-interactive
callers. A curses failure after initialization also exits 2 and names the same
fallback.

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

### Idempotent creation

`asha task start --task-id UUID` accepts a canonical lowercase caller-supplied
UUID. Under the task's transaction lock, Control creates the task when both its
record and creation journal are absent. An identical registered task is
returned unchanged without fetching a PR, importing Git state, creating a jj
workspace or tmux session, launching a harness, or adding a run. A different
request is refused; an interrupted `creating` task must be recovered explicitly
with `asha task recover UUID` before retrying.

## jj contract

Control accepts only the canonical jj repository root and a usable Git
backend. Before any source mutation, task start compares Git `HEAD` with jj
`@-` and refuses a committed divergence with a `jj status` remediation. Once
they agree (or Git `HEAD` is positively confirmed unborn while jj `@-` is the
zero root), every source mode runs one
`jj -R SOURCE --ignore-working-copy git import` after any PR fetch and before
base resolution; the import is reported in `source_mutations` as a
`jj-operation` with operation `git import`. Base resolution is deterministic:

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
Popups are bound to the client attached to the caller's own session and never
fall back to another tmux client.

The server summary labels itself `last-event-only`: it does not claim process
liveness, and it reports when more than 256 snapshots exist. Terminal
reconciliation persists the terminal run evidence before expiring the
corresponding runtime snapshots; archive does the same. Both refresh the
cached server summary after cleanup, and a late hook write rechecks the durable
run state before it may survive.

## State and XDG locations

Defaults follow XDG paths:

```text
${XDG_STATE_HOME:-~/.local/state}/asha/control/tasks/<task-id>.json
${XDG_STATE_HOME:-~/.local/state}/asha/control/tasks/<task-id>.lock
${XDG_STATE_HOME:-~/.local/state}/asha/control/prunes/<task-id>.json
${XDG_DATA_HOME:-~/.local/share}/asha/workspaces/<repo-key>/<task-slug>/
${XDG_RUNTIME_DIR:-/tmp/user-$UID}/asha-control/
${XDG_RUNTIME_DIR:-/tmp/user-$UID}/asha-control/events/<run-id>.json
```

`control.workspace_root` in `~/.asha/config.json` may replace the data-path
default. It cannot be `/`, `$HOME`, the source, below the source, or an ancestor
of the source. Existing path components must be canonical directories without
symlink aliases or unsafe writable ancestry.

If the `/tmp/user-$UID` runtime fallback already exists but fails those safety
checks, Control refuses it and directs the operator to set `XDG_RUNTIME_DIR` to
an existing private directory.

Writable ancestry is judged by mode, not ownership: a group- or other-writable
non-sticky directory anywhere on a Control path (state, runtime, workspace
root, task workspace, or source repository root) is refused, and every
component from the workspace root down must be owned by the effective user
with mode `0700`. Control creates its own directories that way and never
changes the mode of a directory it did not create; each refusal names the
path and the exact remediation (`chmod g-w,o-w <path>`). Task workspaces
created before 2026-08-17 may carry the umask mode `0775` and are skipped by
`task list` until remediated:

```text
chmod g-w,o-w "${XDG_DATA_HOME:-~/.local/share}"/asha/workspaces/*/*
```

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
| `failed` | Launch or termination has verified failure evidence, including when the process ended by signal or vanished without a reported exit status while its pane was absent. |
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

1. `git -C GIT_ROOT fetch REMOTE
   pull/N/head:refs/remotes/REMOTE/asha-control-pr-N` adds the fetched objects
   and updates one controller-owned remote-tracking ref. `REMOTE` is the sole
   configured remote, or the configured remote whose URL matches the viewed
   pull-request repository when more than one exists.
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

The fetch never checks out a tree. The pre-mutation Git `HEAD`/jj `@-` guard is
what makes the following import safe and keeps Git `HEAD`, staged content, and
source `@` untouched; Control refuses and asks the operator to run `jj status`
when those positions diverge. Issue mode performs its bounded `gh issue view`
read and the common reported import, then resolves the explicit base or
`trunk()`; it does not fetch.

Control has no GitHub write route. It does not comment, edit, label, close,
review, merge, create a PR, or push. Subprocesses are argv-only, shell-free,
deadline-bound, and byte-capped.

## Data preservation

### Controller materializations

Orchestration may call the library-only
`lib.control.prepare.plan_materialization(config, source, name)` seam to resolve
the deterministic repository identity, workspace name, and target path without
mutation, then call
`lib.control.prepare.prepare_materialization(config, source, base_commit_id, name)`
to create a fresh, explicit-base jj workspace for controller verification.
It uses the same pinned-operation workspace-add primitive, canonical workspace
root and repository namespace, path checks, private `0700` directories, and
durable phase journaling as task preparation. The retained workspace lives at
`<workspace-root>/<repo-key>/materializations/<name>`.

A controller materialization registers no Control task or run, starts no tmux
session or harness, and receives no task context marker. Success returns only
`workspace_name`, `workspace_path`, `change_id`, and `working_commit_id`.
Failure preserves the journal and any ambiguous materialization for inspection.
There is no materialization deletion route.

Archive requires an ended task or a running task whose reconciled runs are all
terminal (`exited` or `failed`) and unblocked. At that terminal edge Control
persists the reconciled run state and bounded evidence, then changes only the
task's registry lifecycle. Archive is reversible with
`asha task unarchive <selector>`; the jj workspace, change, ignored files, tmux
history, and source repository remain. Stop verifies task, pane, process start
identity, and tmux ancestry, then sends `SIGINT` or explicit `SIGTERM` to that
process only. It does not kill the tmux session, archive the task, or touch jj.

### Pruning archived tasks

Archive preserves everything, so `asha task prune` is the only route that
reclaims what an archived task leaves behind: its dead tmux session, its jj
workspace registration, and its workspace directory. The task record is not
modified and stays archived; described or non-empty jj changes remain in the
source repository (jj discards only an empty, undescribed working-copy commit
when the workspace is forgotten). Prune changes no stored task fact; after
it, `asha task show` reports the same record with live jj evidence `missing`.

Prune is per task or `--all` (every archived task), and it is idempotent:
each pass re-derives everything from live state, so an interrupted pass is
finished by running it again. Per task, in order:

1. Only an `archived` task is eligible; anything else is refused unchanged.
2. The tmux session is killed only when it exists, carries this task's
   `@asha_managed`/`@asha_task_id` options, and every pane in it is dead. A
   live pane refuses the whole task (unarchive and stop it first). A session
   with foreign ownership is left alone and reported.
3. Unless `--keep-workspace`: the workspace is removed only when the creation
   journal owns its root inode (device, inode, owner), the workspace's own
   `.asha/control-task.json` marker names this task, no other task record
   whose own root was not already reclaimed claims the same path, the path
   lies below `control.workspace_root` without
   symlink components, the source repository is readable, and no orchestration
   attempt bound to the task (by link or by reserved task id) is still
   non-terminal (an `indeterminate` or `result-missing` attempt may still be
   sealed from that workspace). All of that is verified before prune runs
   `jj workspace forget` through the source repository and then removes the
   tree by descriptor-anchored, non-following deletion that refuses foreign
   ownership, device crossings, loops, and mount points. Refusals keep the
   workspace and name the reason.

Removal is journaled in
`${XDG_STATE_HOME}/asha/control/prunes/<task-id>.json`
(`asha.control-prune-record.v1`): intent before the first unlink, completion
after the root is gone. A later pass treats a completed path as absent for
the pruned task even when a successor task with the same slug reuses the
directory and its inode number; the marker and registry checks refuse the
successor independently of that record, and a directory whose marker names
the successor is reported as that task's, not as residue. A removal that
stops midway (for example on a read-only subdirectory) leaves the partial
tree in place and reports the reason; fix the cause and run prune again, and
the recorded intent lets that pass finish even though `.asha` may already be
gone.

Because removal is destructive to ignored files inside the workspace (results,
build output, notes), an interactive prune confirms once for the whole batch;
non-interactive and `--json` callers must pass `--yes`, `--dry-run`, or
`--keep-workspace`.
`--dry-run` reports every planned action without touching tmux, jj, or disk.
`--json` emits `asha.control-task-prune.v1`. Exit `2` when any selected task
was refused or only partially pruned, `0` otherwise. `asha task doctor` reports
how many archived tasks still hold a session or workspace in its `prunable`
probe; that is information, never a failed check.

Before a harness process may have started, transaction rollback removes only
artifacts whose exact ownership was journaled and reverified. After launch is
possible, every failure path preserves the workspace and records recovery
facts. Outside `asha task prune`, Control never substitutes raw recursive
deletion, destructive Git, or unreviewed jj abandonment for a removal design;
prune itself removes only a journaled workspace root through the ownership
checks above.

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
