---
title: Asha Control
type: proposal
status: delivered — all six increments completed 2026-08-15
date: 2026-08-14
origin: Keeper design session after evaluating Workmux, ccsessionctl, and FleetView
---

# Asha Control: terminal-native task supervision with tmux and jj

> **Historical record — delivered and superseded as operating guidance.** The
> design below is preserved as written. Use the current
> [Asha Control guide](../control.md) for supported behavior.

## Status and authority

This document is the implementation contract for the first usable Asha
Control release. The Keeper approved the core interaction on 2026-08-14:

1. `asha control` runs as a terminal TUI in the pane where it was invoked.
2. A task runs in its own persistent tmux session.
3. Selecting a task or run and pressing `Enter` opens that session in a tmux
   popup.
4. Closing the popup detaches its client. It does not kill the agent pane or
   remove its workspace.
5. Mutating tasks use jj workspaces rather than Git worktrees.
6. Asha owns lifecycle and safety. FleetView is not part of the system.

Runtime probes may narrow a harness status claim, but they may not silently
weaken the data-preservation, ownership, or explicit-base rules in this
document. Record a required design amendment here before implementing behavior
that contradicts those rules.

Amendment (2026-08-17): `asha task start --task-id UUID` adds locked, idempotent create-by-ID without extending `asha.control-task.v1`.

## Problem

Asha can launch a harness and coordinate work inside one process, but it does
not own the relationship between a piece of work and its terminal, working
copy, version-control change, harness session, or current attention state.
Those facts are split across shells, tmux, Git or jj, and harness-local state.

The missing relation is:

```text
task -> repository -> jj workspace -> jj change
     -> Asha run -> harness session -> process -> tmux pane
```

Workmux demonstrates the value of joining workspace lifecycle, multiplexer
state, and an overview UI. It is much broader than this requirement and its
Git worktree and merge ownership conflict with Asha's safety model.
`ccsessionctl` contains useful terminal and session-list prior art, but its
current live-state work is Claude-specific and parses terminal output.
FleetView proved that cross-harness observation is useful, but its browser,
HTTP, and SSE architecture was a one-off and is explicitly excluded here.

## Goal

Provide one local, terminal-only Asha control plane that can:

- create an isolated jj workspace from an explicit revision;
- launch an Asha harness inside a persistent tmux task session;
- associate task, change, run, process, and pane identities;
- display bounded, evidence-qualified status in tmux and a terminal TUI;
- open a selected task or run in a popup without disturbing the controller;
- recover a truthful view after the TUI or launching shell exits;
- preserve every workspace and change until cleanup is separately authorized.

The first release optimizes for one operator on one machine. It does not need
a web service, remote coordinator, or autonomous merge engine.

## Non-goals

The first release does not:

- use or extend FleetView;
- read or parse harness transcript stores;
- scrape terminal contents to guess semantic state;
- replace `asha workspace worktree`, which remains the existing coordinated
  multi-repository Git-worktree mechanism;
- support multi-repository tasks;
- merge, rebase, bookmark, push, publish, or update a pull request
  automatically;
- delete a jj workspace or task directory;
- run multiple mutating agents in the same jj workspace;
- provide token accounting, cost accounting, or a browser interface;
- edit the user's tmux configuration automatically;
- claim that a harness hook is an enforcement boundary.

## Terms

### Task

A local execution container for one goal in one repository. A task owns one jj
workspace, one jj working-copy change, one tmux session, and one or more run
records. The first release launches one primary mutating run per task.

### Run

One Asha-wrapped harness process in one tmux pane. A run records the harness,
role, process identity, tmux target, harness session identifier when known, and
latest qualified status.

### Controller

The deterministic command layer behind `asha task ...` and `asha control`.
The controller owns validation, state, jj and tmux subprocesses, reconciliation,
and safe lifecycle transitions. The TUI calls this same layer rather than
implementing a second behavior path.

### Control TUI

The interactive list started by `asha control`. It is both the detailed
observer and the bounded interactive front end to controller commands. It is
not a process supervisor. Tmux supervises the task processes.

## User interface contract

### Starting a task

Canonical form:

```bash
asha task start \
  --repo ~/Code/Thallus \
  --pr 34 \
  --harness codex \
  --goal "Address the requested changes"
```

From inside a repository:

```bash
asha task start --pr 34 --harness codex \
  --goal "Address the requested changes"
```

Ad hoc work:

```bash
asha task start \
  --repo ~/Code/Thallus \
  --base 'trunk()' \
  --harness codex \
  -- "Investigate the intermittent session failure"
```

`--agent` may be accepted as an ergonomic alias for `--harness`, but help and
documentation use Asha's existing term, `harness`.

The machine-readable source selectors are `--pr NUMBER` and `--issue NUMBER`.
They provide context and a revision source; they do not guess intent. A goal is
still required through `--goal` or the text after `--`.

The command defaults are:

- `--repo`: the canonical repository containing the current directory;
- `--harness`: Asha's configured default harness;
- placement: a detached task session in the current tmux server;
- view: open the new task in a popup when invoked from inside tmux;
- `--detach`: create and launch without opening the popup;
- base: resolved explicitly as specified under "jj contract" below.

Successful text output names every durable identity:

```text
Task:       thallus-pr-34
Task ID:    5d5e0e58-...
Workspace: ~/.local/share/asha/workspaces/Thallus/thallus-pr-34
jj name:    asha-thallus-pr-34-5d5e0e58
Change:     qpvuntsm
Tmux:       asha-thallus-pr-34:work.0
Run:        codex-01
```

`--json` emits the same result under a versioned contract and never mixes
diagnostics into stdout.

### Command surface

```text
asha task start       create a task, workspace, tmux session, and primary run
asha task list        list registered tasks after live reconciliation
asha task show        show one task, its runs, jj identity, and blockers
asha task attach      open or attach to a task, selecting a run when requested
asha task reconcile   refresh registry facts from tmux, processes, and jj
asha task archive     hide an ended task but preserve its workspace and change
asha task stop        request graceful process termination; never remove data
asha task doctor      probe local prerequisites and report capability limits

asha control          run the terminal control TUI in the current terminal
asha control event    internal hook-facing event ingestion; not routine UX
asha control tmux     print the optional tmux-format and close-key snippet
```

`task archive` is reversible and non-destructive. A future removal command
requires a separate reviewed design for jj integration evidence, ignored files,
and recoverable deletion. It is not an alias for archive and is not part of
this release.

### Control TUI

`asha control` uses the current pane or terminal. It does not create or attach
to an `asha-control` tmux session.

Initial layout:

```text
 ASHA TASKS

 STATE       TASK                 REPOSITORY  CHANGE    HARNESS  AGE
 working     thallus-pr-34        Thallus     qpvuntsm  codex     18m
 needs-you   asha-hook-fix        asha        yostqsxw  claude     7m
 exited      servitor-issue-28    servitor    kkmpptpz  codex     42m

 thallus-pr-34
 Run:        codex-01 / implementer
 Tmux:       asha-thallus-pr-34:work.0
 Evidence:   PostToolUse heartbeat 3s ago
 Workspace:  ~/.local/share/asha/workspaces/Thallus/thallus-pr-34
 Change:     qpvuntsm, last explicit refresh: 7 files changed
```

Required keys:

| Key | Action |
|---|---|
| `Enter` | Open the selected task in a popup; select its pane first when a run is selected |
| `n` | Open the task-start form backed by the same controller validation as the CLI |
| `r` | Reconcile the selected task from live state |
| `d` | Run an explicit jj diff refresh and display it read-only |
| `a` | Archive an ended task after confirmation; preserve workspace and change |
| `/` | Filter tasks without mutating state |
| `q` | Exit the TUI without affecting tasks |
| `?` | Show keys, status evidence, and limitations |

Destructive task removal and automated integration receive no TUI binding.
The TUI must restore terminal mode on normal exit, signal, or handled error.

### Popup interaction

`Enter` invokes a dynamic equivalent of the existing persistent scratch-popup
pattern:

```bash
tmux display-popup -E -w 90% -h 85% \
  "tmux attach-session -t asha-thallus-pr-34"
```

The controller selects the requested window and pane before attaching. The
popup command is constructed as an argument vector or through a fixed wrapper;
task data is never interpolated into an evaluable shell string.

When the popup client detaches, `tmux display-popup` returns and the control TUI
redraws. The task session, panes, processes, and jj workspace remain alive.

The existing `prefix+backtick` behavior can be extended manually so that the
same key detaches when the current session is controller-owned. Ownership must
be checked through a tmux user option, not only a name prefix. `asha control
tmux` prints the exact snippet. Installation never edits `.tmux.conf`.

## Tmux contract

### Topology

```text
tmux server
├── operator's ordinary sessions
│   └── any pane may run `asha control`
└── one detached controller-owned session per task
    └── window: work
        ├── pane: primary Asha run
        └── later optional read-only or support panes
```

One task per tmux session makes a selected task directly attachable in a popup.
It also prevents task navigation in the popup from changing the operator's
ordinary session.

### Names and ownership

Session names use a bounded readable slug plus a collision-resistant suffix.
Names are presentation, not ownership evidence. Every managed object receives
tmux user options:

```text
session: @asha_managed, @asha_task_id, @asha_repo, @asha_workspace,
         @asha_change, @asha_state
pane:    @asha_run_id, @asha_harness, @asha_role, @asha_state
server:  @asha_summary
```

Before attaching, signaling, renaming, or stopping a target, the controller
must match both the task registry and the relevant tmux ownership options. A
foreign session with a colliding name is never adopted or killed.

The task session sets `remain-on-exit` so completed output remains inspectable.
The window name remains stable. Pane titles are set explicitly through tmux;
the controller does not depend on terminal title escape sequences or automatic
renaming.

### Presentation

Hooks update pane-local state and the server-level summary. Users may include
those values in their existing status and pane-border formats. Asha provides
format variables and a sample only; it never replaces the user's theme or
`status-format` settings.

## jj contract

### Preconditions

The first release supports single repositories that are already jj-managed and
Git-backed. `asha task start` requires all of these:

- `jj` is present and exposes the workspace command surface used here;
- `jj root --repository REPO` resolves to the requested repository;
- the repository has a usable Git backend for PR and issue modes;
- the source path is canonical, is not a symlink alias, and is the repository
  root;
- the destination is canonically contained beneath the configured controller
  workspace root.

The controller never runs `jj git init`, never colocates a Git repository, and
never rewrites user jj configuration. A missing jj setup is a preflight error
with an exact manual remediation command.

The command contract targets the installed jj 0.38 workspace surface. Doctor
probes required commands and semantics instead of trusting a version string
alone.

### Base resolution

Every new workspace receives `jj workspace add --revision` with an explicitly
resolved immutable commit ID. The controller never accepts the command's
implicit default, which would create a workspace on the parents of the current
working-copy commit.

Resolution order:

1. `--pr N`: the fetched immutable PR head commit;
2. `--base REVSET`: exactly one commit resolved from that revset;
3. `--issue N` or ad hoc work without `--base`: exactly one commit resolved
   from `trunk()`;
4. any ambiguous, missing, or immutable-base failure: refuse creation.

The resolved commit ID and the human input revset are both recorded. Creation
uses the following jj 0.38 equivalent. A non-mutating read first pins the full
128-hex repository operation ID; the mutating add is aimed at that operation:

```bash
OPERATION_ID="$(jj --repository SOURCE --ignore-working-copy operation log \
  --limit 1 --no-graph --template 'id ++ "\\n"')"
jj --repository SOURCE --at-operation "$OPERATION_ID" workspace add \
  --name WORKSPACE_NAME \
  --revision RESOLVED_COMMIT_ID \
  --message "TASK_LABEL" \
  DESTINATION
```

This pinned-operation form is required because jj 0.38 can partially register
a workspace and then fail when `--ignore-working-copy workspace add` is aimed
at a source working copy. Production code does not rely on that failure side
effect and does not use symbolic `@` for the mutating add.

This creates a new empty working-copy change on top of the selected base. The
controller records its jj change ID and commit ID immediately after creation.
It does not create or move a bookmark. Controller commands aimed at the source
repository use `--ignore-working-copy` whenever their contract does not require
snapshotting that source workspace.

### Observation and concurrency

Routine list refreshes must not continuously snapshot a working copy while an
agent may be editing it. Background reconciliation uses jj's
`--ignore-working-copy` read paths where possible and labels cached diff facts
with their refresh time. A full status or diff snapshot runs only on an
explicit user refresh, after a hook-defined idle point, or after the primary
process exits.

Only one mutating Asha run owns a task workspace. Additional mutating runs must
eventually receive their own child task and jj workspace. A future read-only
reviewer pane is permitted only when that harness has a tested read-only launch
mode; a label is not an enforcement mechanism.

Jj operation-log concurrency preserves repository operations, but it does not
make simultaneous filesystem mutation in one working copy safe. The controller
must not infer otherwise.

### Integration and retention

The controller reports the change ID, diff, conflicts, and bookmarks. It does
not land the change. The operator or a separately reviewed Asha workflow owns
integration and publication.

Archiving removes no jj state and no files. Workspace deletion is deferred
because ignored files are not represented by jj's change graph and may contain
user data. No implementation may substitute `rm -rf`, `jj workspace forget`,
or Workmux-style merge-and-delete behavior for the missing removal design.

## Pull request and issue sources

`--pr` and `--issue` are optional GitHub-aware read paths. They require `gh`
and an authenticated repository remote. General ad hoc tasks do not.

For `--pr N`, the resolver:

1. reads bounded metadata with `gh pr view`;
2. fetches the pull-request head object without checking it out in the source
   working copy;
3. imports the fetched Git object into jj when required;
4. resolves and records the immutable head commit;
5. creates a new jj working-copy change on top of that head.

For `--issue N`, it reads bounded issue metadata and creates the task on the
explicit or resolved trunk base. Neither mode posts comments, changes labels,
pushes, checks out the primary tree, or modifies the pull request.

PR or issue title text is display data. It is sanitized, length-bounded, and
never used as a shell command, path, session identifier, or instruction. The
operator-provided goal remains the instruction authority.

## Asha context inside a jj workspace

An additional jj workspace does not contain the source repository's ignored
`.asha/`, `Memory/`, or `Work/` state, and it is not a Git worktree visible to
`git rev-parse`. Launching without accounting for that would disable Asha's
project hooks and make bare `/session:save` follow an invalid Git path.

Task creation therefore provisions only bounded local context:

```text
WORKSPACE/.asha/config.json          copied from the initialized source project
WORKSPACE/.asha/control-task.json    controller ownership and source linkage
WORKSPACE/Memory/activeContext.md    validated creation-time snapshot
WORKSPACE/Memory/decisions.md        validated creation-time snapshot
WORKSPACE/Work/session-state/        ordinary ignored recovery state when used
```

Rules:

- The source repository must already be Asha Memory v2 initialized. Creation
  does not fabricate a project identity.
- The copied project ID remains the same because this is another working copy
  of the same project, while the controller task ID distinguishes runtime
  state.
- The Memory pair is a point-in-time task-local snapshot. It is not symlinked
  to the source project and does not mutate the source plane.
- `decisions.md` is capped at 65,536 UTF-8 bytes. This accommodates current
  binding decisions while keeping snapshots and journals deterministically
  bounded.
- The controller records hashes for every generated private file and removes
  none of them in this release.
- A managed task marker causes bare `/session:save` to use the existing
  no-Git publication behavior equivalent to `--scope none`. It must not invoke
  Git commit or push from an additional jj workspace.
- Task-local Memory remains with the retained workspace. Promotion back to the
  source project's operational Memory is explicit and outside this release.

This managed-task save behavior changes a cross-harness command primitive. Its
source, renderers, installer ownership where applicable, doctor checks, and
tests must remain synchronized under the repository's harness rules.

## State and identity

### Storage

Controller state follows XDG paths:

```text
${XDG_STATE_HOME:-~/.local/state}/asha/control/tasks/<task-id>.json
${XDG_DATA_HOME:-~/.local/share}/asha/workspaces/<repo-key>/<task-slug>/
${XDG_RUNTIME_DIR:-/tmp/user-$UID}/asha-control/   # locks and ephemeral IPC only
```

State directories are mode `0700`; task records and lock files are mode
`0600`. Paths are canonicalized and checked for symlink components before any
write. The configurable workspace root must not be `/`, `$HOME`, the source
repository itself, or an ancestor of the source repository.

Use one task record per file with atomic same-directory replace and a lock.
Avoid one global mutable JSON document. The registry stores current snapshots,
not an unbounded event log.

### Minimum task record

```json
{
  "contract": "asha.control-task.v1",
  "task_id": "uuid",
  "slug": "thallus-pr-34",
  "label": "Thallus PR #34",
  "created_at": "RFC3339 UTC",
  "updated_at": "RFC3339 UTC",
  "lifecycle": "running",
  "repository": {
    "root": "/canonical/source/root",
    "identity": "stable-derived-id"
  },
  "source": {
    "kind": "pr",
    "number": 34,
    "url": "bounded display URL"
  },
  "jj": {
    "workspace_name": "asha-thallus-pr-34-5d5e0e58",
    "workspace_path": "/canonical/workspace/path",
    "requested_base": "PR head",
    "base_commit_id": "full id",
    "change_id": "full id",
    "working_commit_id": "full id"
  },
  "tmux": {
    "socket": "server identity",
    "session": "asha-thallus-pr-34-5d5e0e58",
    "window": "work"
  },
  "runs": [
    {
      "run_id": "uuid",
      "harness": "codex",
      "role": "implementer",
      "pane_id": "%23",
      "pid": 12345,
      "process_start_identity": "platform evidence",
      "harness_session_id": null,
      "state": "starting",
      "evidence": "controller launch",
      "evidence_at": "RFC3339 UTC"
    }
  ]
}
```

The full initial prompt, transcript contents, tool arguments, terminal capture,
and secrets are never stored. Goals and external titles are bounded before
persistence.

### Reconciliation authority

The registry is ownership metadata, not proof that a process or workspace is
live. Reconciliation uses this precedence:

1. live tmux target plus matching ownership options;
2. live process identity beneath the recorded pane;
3. live jj workspace and change identity;
4. recent verified harness event;
5. stored lifecycle claim.

Conflicts produce `stale` or a specific blocker. The controller does not edit
tmux, jj, or disk merely to make live state match an old registry record.
Missing or ambiguous evidence preserves the workspace and asks for operator
resolution.

PID equality alone is insufficient because PIDs are reused. Record and verify
a process start marker available on the platform, plus tmux pane ancestry.

## Status protocol

### States

The shared vocabulary is:

| State | Meaning |
|---|---|
| `creating` | Transaction has begun but no run is yet proven live |
| `starting` | Tmux pane and harness process exist; harness start is not yet observed |
| `working` | A recent verified harness event indicates active work |
| `needs-input` | A verified harness event indicates an operator decision or permission is required |
| `idle` | A verified stop or turn-complete event occurred while the process remains live |
| `exited` | The recorded process ended normally and the retained pane is no longer live |
| `failed` | Launch or process termination produced verified failure evidence |
| `unknown` | Process is live but the harness lacks a semantic event seam |
| `stale` | Registry identity disagrees with live tmux, process, or jj evidence |

`review-ready` is not inferred merely because a model stopped generating.
Verification and review remain separate facts.

### Event binding

The launcher exports bounded opaque identifiers into the run environment:

```text
ASHA_CONTROL_TASK_ID
ASHA_CONTROL_RUN_ID
ASHA_CONTROL_STATE_DIR
ASHA_CONTROL_MANAGED=1
```

Installed hooks may submit an event only when those variables exist. The event
handler validates identifiers, resolves the owned record, verifies the pane
when available, discards prompt and tool payload bodies, and writes a bounded
current snapshot. Ordinary Asha sessions pay no state-write cost beyond the
environment check.

The initial semantic event set is:

```text
session-start
prompt-submitted
tool-completed
permission-requested
turn-stopped
session-ended
```

Mappings are harness-specific and count only after a live harness probe proves
the event and payload. Unsupported events degrade to process liveness and
`unknown`; they do not receive fabricated parity labels.

Target for the first release:

| Harness | Launch | Minimum status target |
|---|---|---|
| Claude | yes | live-probed start, working, needs-input, idle, end where native events permit |
| Codex | yes | live-probed start, working, needs-input, idle, end where native events permit |
| Copilot | yes | process liveness; semantic states only after a separate live probe |
| OpenCode | yes | process liveness; semantic states only after a separate live probe |

Hook event writes are local, bounded, fail-open, and network-free. A controller
failure must not block a prompt, tool call, permission response, or session
exit. Hooks are observation seams, not enforcement boundaries.

## Transaction and failure behavior

Task creation is journaled:

```text
1. Validate arguments, repository, Asha initialization, jj, tmux, destination,
   harness availability, and source metadata.
2. Resolve and record the explicit base commit.
3. Create a task record in `creating` state.
4. Create the destination parent and jj workspace.
5. Provision bounded task-local Asha context.
6. Create the detached owned tmux session and pane.
7. Launch `asha <harness>` using an argument vector and task environment.
8. Verify pane and process identity, record the jj change, then mark `starting`.
9. Open the popup unless `--detach` was requested.
```

Before process launch, rollback may remove only paths and registrations created
by the current transaction, and only after verifying their ownership hashes
and emptiness. Once a user process has started or the workspace may contain
changes, failure handling preserves everything, marks the task `failed`, and
reports exact recovery commands.

Task start must not change the source working-copy revision, source workspace,
bookmark positions, or index. PR fetching may add fetched objects or a
controller-owned temporary ref, and that mutation is reported.

`task stop` first requests graceful termination through the harness adapter.
It never sends a signal to a PID that fails process identity and tmux ancestry
checks. Escalation from interrupt to terminate is explicit. Stop never archives
or removes the workspace.

## Security and data-preservation requirements

- Treat repository paths, GitHub metadata, hook JSON, tmux output, and state
  files as untrusted input.
- Pass subprocess arguments as arrays. Do not construct shell commands from
  task labels, goals, paths, PR titles, session names, or hook payloads.
- Use restricted grammars for task slugs, workspace names, session names, and
  identifiers. Preserve full opaque IDs separately from display slugs.
- Reject symlinked state records, workspace destinations, and ownership files.
- Make state writes atomic and lock-protected. Interrupted writes must leave
  either the old valid record or the new valid record.
- Verify ownership through record digests and tmux options before mutation.
- Never expose the full prompt, tool arguments, terminal content, environment,
  or secrets in the registry or TUI.
- Never use raw `rm`, destructive Git, or unreviewed jj abandonment as task
  cleanup.
- Add policy coverage for future destructive Asha Control verbs before those
  verbs ship. Matching the wrapper command string must not be mistaken for
  seeing its internal jj or tmux operations.
- A live task may outlive the TUI, launching shell, and popup. Closing any of
  those interfaces is not cleanup authorization.

## Configuration and installation

User configuration extends `~/.asha/config.json` under an optional `control`
object:

```json
{
  "control": {
    "workspace_root": "~/.local/share/asha/workspaces",
    "default_harness": "codex",
    "tmux": {
      "popup_width": "90%",
      "popup_height": "85%",
      "session_prefix": "asha-"
    }
  }
}
```

Absent configuration uses safe defaults. Invalid control configuration fails
the requested control command and does not affect ordinary `asha <harness>`
launches.

Recommended code ownership:

```text
bin/asha                         thin dispatch additions for `task` and `control`
lib/control.sh                   thin shell router, matching existing CLI seams
lib/control/                     Python controller package
  cli.py                         argument parsing and versioned JSON output
  model.py                       task schema and state transitions
  store.py                       XDG paths, locks, atomic state
  jj.py                          jj adapter
  tmux.py                        tmux adapter and popup behavior
  harness.py                     launch adapters and process identity
  sources.py                     optional GitHub PR and issue resolution
  reconcile.py                   live evidence synthesis
  tui.py                         curses view and input handling
plugins/session/hooks/handlers/  narrow controller event bridge at proven seams
tests/python/test_control_*.py    pure and subprocess-adapter tests
tests/test-control.sh            isolated tmux and jj integration tests
```

This layout is a recommendation, not permission to bypass prior-art discovery.
The build session must inspect current installer and hook generation patterns
before adding files. The control engine belongs to the Asha dispatcher, not to
the identity plugin. Hook changes remain in their actual owning plugin.

The first TUI should use Python's standard curses module with a pure render and
selection model outside curses. Do not add a Rust toolchain or a large TUI
framework before the interaction proves it needs one. `task list --json`
remains a stable fallback if curses is unavailable.

`asha task doctor` checks Python/curses, tmux popup support, jj command
semantics, XDG directory safety, harness executables, hook freshness, and `gh`
only when GitHub source modes are requested. Installer and root doctor surfaces
must report controller hook drift for every affected harness.

## Verification strategy

### Unit tests

- CLI grammar, mutually exclusive options, deterministic defaults, and JSON
  stdout discipline;
- task schema validation and every legal or illegal state transition;
- canonical path containment, symlink rejection, file modes, atomic writes,
  lock behavior, and interrupted-write recovery;
- tmux and jj adapters against captured outputs and hostile identifiers;
- hook event validation, payload minimization, status mapping, and fail-open
  behavior;
- reconciliation precedence and PID-reuse defenses;
- pure TUI sorting, filtering, selection, key dispatch, and resize behavior;
- managed-task Memory snapshot and bare-save no-Git behavior.

### Integration tests

Use disposable directories and an isolated tmux server:

```text
tmux -L asha-control-test-<pid> -f /dev/null ...
```

Never inspect, attach, rename, or kill the operator's live tmux server during
tests. Build temporary Git repositories, initialize them with jj in colocated
mode, add secondary jj workspaces, and verify that the source working copy does
not move.

Required scenarios:

1. Start creates one explicit-base jj change, owned task session, run pane, and
   valid registry record.
2. Start failure before launch rolls back only verified transaction-owned
   empty artifacts.
3. Failure after launch preserves the workspace and records recovery facts.
4. Closing the popup detaches while the process and session remain live.
5. Exiting `asha control` changes no task state.
6. A missing pane becomes `stale` or `exited` from live evidence, not `working`
   from an old record.
7. A colliding foreign tmux session is refused and preserved.
8. A second mutating run in the same task is refused.
9. PR mode fetches without checking out or moving the source working copy and
   performs no GitHub writes.
10. Bare `/session:save` in a managed jj workspace publishes locally without
    Git commit or push.
11. Unsupported harness events display `unknown`, not a false semantic state.
12. Hostile titles, paths, and hook payloads cannot inject shell commands,
    tmux formats, control characters, or state fields.

### Repository ship gates

Run the narrow control tests first, followed by:

```bash
./tests/test-control.sh
./tests/test-hooks.sh
./tests/run-tests.sh
./bin/asha-drift-check.sh --target codex
```

Run target-specific drift checks for every additional harness whose installed
hooks or artifacts change. Live-probe each claimed semantic status in the
actual harness and record the exact support and limitation in
`docs/harness-enforcement.md` and `harnesses/capabilities.json`.

Before shipping, perform one manual disposable-repository acceptance pass from
the real tmux configuration:

```text
start task -> observe list -> Enter -> interact in popup -> detach ->
observe unchanged controller -> exit controller -> reopen -> reconcile
```

The acceptance pass must confirm that the original repository working copy and
bookmark positions did not move and that no workspace was removed.

## Delivery order

### Increment 1: deterministic core and read-only control surface

- Add dispatcher routing and a pure Python control package.
- Implement configuration, IDs, state records, locks, atomic writes, list,
  show, doctor, and reconciliation interfaces using faked adapters.
- Add `task list --json` before curses.

Exit criterion: state and reconciliation tests pass without invoking the live
tmux server or modifying a real repository.

### Increment 2: jj creation and task-local Asha context

- Add explicit-base jj workspace creation in disposable repositories.
- Capture the created change identity.
- Provision private context and managed-task Memory behavior.
- Implement transaction journals and ownership-checked pre-launch rollback.

Exit criterion: source working copies remain byte- and revision-stable across
success and injected failures; bare task save never invokes Git.

### Increment 3: tmux session and harness launch

- Add isolated task sessions, ownership options, `remain-on-exit`, pane titles,
  process identity, attach, popup, stop, and archive.
- Launch through the existing `asha <harness>` dispatcher with an argument
  vector and controller environment.
- Print the optional tmux integration snippet without editing dotfiles.

Exit criterion: the isolated tmux integration suite proves detach-versus-kill,
foreign-session preservation, and recovery after the launching CLI exits.

### Increment 4: hook event bridge and proven status

- Add the bounded event ingestion path.
- Wire Claude and Codex only at live-proven events.
- Update installers, ownership manifests, doctor checks, capabilities, and
  enforcement documentation for affected harnesses.

Exit criterion: real harness probes support every claimed status; unsupported
states remain `unknown`.

### Increment 5: terminal TUI

- Implement `asha control` over the tested controller interfaces.
- Add list/detail, filter, explicit refresh, task-start form, popup entry,
  archive, help, resize handling, and terminal restoration.

Exit criterion: the TUI can be killed and restarted without affecting task
sessions and can reconstruct its list from reconciled state.

### Increment 6: GitHub source adapters and final documentation

- Add read-only PR and issue resolution.
- Prove no source checkout movement and no GitHub writes.
- Update root usage, focused control documentation, changelog, doctor, and
  installation guidance.
- Run the full ship gates and cold review the complete diff.

Exit criterion: the command examples in this document pass against a
disposable GitHub test source or a controlled fixture, and the complete Asha
suite is green.

## Implementation process

This feature changes public CLI grammar, architecture, lifecycle, hooks,
installed artifacts, state schemas, and safety boundaries. The implementation
workflow therefore requires:

```text
codebase-historian -> TDD implementation by increment -> cold reviewer
```

The historian must inspect Asha's current dispatcher, installer, doctor, hook
renderers, workspace worktree ownership journal, Memory v2 save scope, and
relevant git history before settling file placement. The reviewer reads the
actual diff and test output, not only implementation handoffs.

Do not commit, push, merge, remove FleetView, or edit dotfiles as part of the
implementation unless the Keeper separately authorizes that action.

## Deferred decisions

These require evidence from the first release rather than speculation:

- multiple read-only agents in one task session;
- child tasks for parallel mutating agents;
- reviewed jj integration, bookmark, and publication workflows;
- recoverable workspace deletion and retention policy;
- cross-repository task groups;
- remote hosts or more than one tmux server;
- replacing curses with Ratatui or another TUI framework;
- retiring FleetView after feature parity is confirmed.
