---
title: Asha Orchestration
type: proposal
status: panel-reviewed and step-4 cold-reviewed; Control soak (runway Phase 3) and contract freeze (Phase 4) completed 2026-08-17; prerequisite gate satisfied; implementation not started
date: 2026-08-15
depends_on: docs/proposals/2026-08-14--asha-control.md
origin: Keeper design session after comparing Asha Control with persistent multi-agent supervisors
---

# Asha Orchestration: initiative-scoped coordination over Asha Control

> **Prerequisite gate status (2026-08-17).** The Control soak ran on real work
> and its defects were fixed in Control; the required create-by-id amendment
> shipped as `asha task start --task-id` (see `docs/control.md`, "Idempotent
> creation"); the contracts Core binds to are frozen in
> [`docs/control-contracts.md`](../control-contracts.md). Re-read those live
> surfaces, not this document's predictions, before Increment 1.

## Status and dependency

This document defines the next feature layer after the first Asha Control
release. It does not amend, expand, or interrupt the six-increment Control
implementation described in
`docs/proposals/2026-08-14--asha-control.md`.

The first draft was reviewed by a full Asha panel before repository placement.
A first revision incorporated its required corrections: coordinator fencing,
generation-bound action idempotency, formal independent state machines,
two-phase artifact sealing, retry and repair lineage, isolated composition,
seal-bound review, controller-observed verification, lazy configuration,
sidecar task linkage, explicit same-user limitations, and proof-ordered
delivery. A second revision then incorporated the panel's required step-4 cold
review of that corrected draft, which found seam-level gaps the first pass left:
the no-double-dispatch guarantee now names an explicit required Control
idempotent create-by-identifier amendment rather than assuming one; autonomous
within-cap retry has a node transition and assigned authority; a
`blocked`/`needs-decision` claim preserves its work through an inheritable
`paused` seal instead of a discarded failure; the multi-repository
candidate/verification bundle has a schema and storage path; the coordinator
event loop re-snapshots for a fresh expected revision; the machine-readable
forbidden-action list matches the prose verbs; and the transition-source sets
are enumerated. The panel decision and the two step-4 cold reviews are retained
as private review evidence in the originating workspace; they are review
evidence, not a repository dependency or runtime authority.

Implementation of this proposal begins only after that release has met its
ship gates and the repository has a reviewed Control baseline. At that point,
the following are treated as existing, tested infrastructure rather than work
to repeat here:

- one deterministic `asha task` controller with versioned JSON interfaces;
- one retained jj workspace and working-copy change per mutating task;
- one persistent, ownership-marked tmux task session per task;
- one primary mutating Asha harness run per task;
- process, pane, workspace, and change identity reconciliation;
- bounded harness status events with explicit evidence and `unknown` fallback;
- a restartable curses Control TUI with task list/detail, popup entry, and
  explicit refresh;
- read-only GitHub PR and issue source resolution;
- task-local Asha context and no-Git Memory publication;
- safe stop and non-destructive archive behavior;
- no automated integration, push, merge, bookmark movement, or deletion.

One capability in that list is not yet shipped and is a required Control
amendment, not existing infrastructure: idempotent task creation on a
caller-supplied identifier (`asha task start --task-id`). Shipped Control v1
mints the task id internally; orchestration's no-double-dispatch guarantee
depends on create-by-id, so the prerequisite gate adds it to Control under
review before Orchestration Core execution.

The implementation must re-read the reviewed post-Control code and contracts
at its prerequisite gate. Names and extension seams predicted here are not
permission to reshape the in-flight Control implementation.

Those contracts remain authoritative. Orchestration consumes them through
their public controller interfaces. It does not reach around them to operate
tmux, jj, process state, or task records directly.

The Keeper's presentation requirement is also binding: this is a solid local
terminal control surface. It has no office simulation, avatars, sprites,
spatial metaphor, animation, Electron shell, browser application, or embedded
IDE. The TUI displays text tables, dependency trees, evidence, approvals, and
recent events. Capability is the feature; representation remains minimal.

## Problem

Completed Asha Control can supervise several isolated tasks, but the operator
still has to perform the coordination between them:

1. decompose an objective;
2. decide which work can run concurrently;
3. choose harnesses, roles, and existing Asha workflows;
4. start every task;
5. inspect results and blockers;
6. request fixes, verification, and independent review;
7. determine whether the complete objective is ready for human integration.

Existing Asha orchestration coordinates specialist phases inside one harness
session. `code-orchestrate` passes bounded handoffs between agents in that
session. Session loops manage iterations within one working context. The issue
loop is a specialized Git-worktree workflow for a configured GitHub backlog.
Workspace work items are private planning records. None of these owns a durable
graph of independent Control tasks and their jj workspaces.

The missing relation is:

```text
initiative
  -> approved objective, scope, acceptance criteria, limits
  -> coordinator run
  -> dependency graph
       -> Control task A -> jj workspace A -> result claim -> success seal A
       -> Control task B -> jj workspace B -> result claim -> success seal B
       -> compose task   -> one terminal candidate seal
       -> review task    -> independent bound verdict
       -> verify gate    -> controller-observed executable evidence
  -> operator decisions
  -> ready-for-integration outcome
```

Control supplies truthful mechanisms. A coordinator supplies bounded
reasoning over those mechanisms. Durable initiative state, not the
coordinator's conversation, joins the two.

## Goal

Add an initiative-scoped orchestration layer that can:

- turn one approved objective into an explicit dependency graph;
- run a dedicated Asha coordinator under a chosen supported harness;
- dispatch ordinary Control tasks into separate jj workspaces;
- run independent child tasks concurrently within fixed limits;
- collect explicit bounded results without parsing transcripts or terminals;
- route failures, review findings, and verification evidence through the
  graph;
- pause at operator approval boundaries;
- recover after the coordinator, TUI, or launching shell exits;
- present the complete initiative in the existing terminal Control surface;
- stop at a truthful `ready-for-integration`, `partial`, or `failed` outcome
  without landing or deleting work.

The first orchestration release remains local to one operator and one machine.
It is an execution coordinator, not an autonomous software company.

## Product principles

### Deterministic mechanism, agentic judgment

The controller owns state transitions, graph validation, budgets, task
creation, locks, event ordering, and lifecycle mutations. The coordinator may
propose and request actions. It never makes those actions true by writing state
itself.

```text
coordinator says: start node implementation-2
controller checks: approved graph, dependency state, repository scope,
                   concurrency limit, attempt limit, harness support
controller does:  create and start the Control task, or return a refusal
```

### Initiative scope, not a permanent global boss

One coordinator belongs to one initiative. It receives only that initiative's
objective, graph, bounded events, and allowed repositories. It does not hold
authority over every Asha session or silently accumulate cross-project context.

A future global router may list initiatives and suggest where work belongs.
It is not part of this release and must not become a second source of truth.

### Evidence over declarations

A worker saying "tests pass" is a result claim. Controller-captured process and
command evidence plus an accepted seal-bound review verdict are qualified
evidence. The scheduler never equates a stopped model turn, idle pane, or
confident prose with task success.

For mutating work, success requires an exact retained artifact. The controller
accepts a provisional worker result, proves the primary run exited normally,
reconciles the final jj state, and publishes an immutable seal. Downstream
tasks, reviewers, and verification consume the seal, never a live worker
workspace or logical jj change ID.

### Explicit authority

The operator approves the initiative scope and initial execution plan. Within
that approved envelope, the coordinator may dispatch bounded child tasks and
request ordinary review or verification. Scope expansion, new repositories,
and higher limits require a new operator decision. External writes,
destructive operations, integration, publication, and deletion are outside v1
and remain refused even when requested by the operator through orchestration.

### Bounded durable state

The coordinator can be killed and recreated. The initiative remains coherent
because objective, plan, attempts, events, decisions, and results are stored as
small versioned records. A transcript is never the recovery mechanism.

Every mutating coordinator request also has a durable action identity and
phase. A lost response is reconciled against the same action and Control task
creation facts; it is never retried as a fresh side effect.

### Minimal terminal presentation

The TUI is a projection of controller state. It does not simulate activity,
guess mood or intent, host a second terminal emulator, or become required for
background work to continue.

## Non-goals

The first orchestration release does not:

- add an office, floor, character, avatar, animation, map, game, or social
  metaphor;
- add a browser, HTTP server, Electron application, embedded editor, or Git
  GUI;
- replace Asha Control's existing task TUI or create a second TUI program;
- treat the coordinator as lifecycle authority;
- run one permanent coordinator across all projects;
- permit several mutating agents to share one working directory;
- provide arbitrary peer-to-peer agent chat or broadcast messaging;
- parse harness transcripts, scrape terminal contents, or store full prompts;
- create a general workflow-language interpreter for every existing recipe;
- merge, rebase, move bookmarks, push, publish, update a PR/tracker, remove a
  workspace, or delete state through orchestration, even with approval;
- automatically promote task Memory into repository or workspace Memory;
- accept work from Slack, email, webhooks, schedules, or remote hosts;
- promise token or monetary accounting where a harness lacks a verified usage
  seam;
- use hooks, prompts, or coordinator instructions as a hard security boundary;
- contain a malicious process deliberately acting as the operator's OS user;
- allow nested autonomous loops or parallel writing subagents to evade the
  initiative's approved execution envelope;
- replace `code-orchestrate`, the issue loop, session loops, process brokerage,
  workspace work items, or workspace worktrees.

## Existing Asha machinery and ownership

The implementation must reuse existing owners rather than build parallel
versions of them.

| Existing surface | Continues to own | Orchestration relationship |
|---|---|---|
| Asha Control | Task, jj, tmux, process, event, stop, archive, reconciliation | Sole executor for child-task lifecycle |
| `code-orchestrate` | Specialist phases inside one harness session and workspace | May be selected only when one-writer ownership and outer limits remain valid |
| `session-loop` | Bounded iteration inside one live task context | Unsupported inside a jj Control task until its Git rollback assumptions are adapted |
| Issue loop | Dual-opt-in GitHub backlog processing and draft PR publication | Remains separate because v1 forbids its external writes and cleanup lifecycle |
| Process router | Advisory registry-backed process recommendation | Coordinator may query it; recommendation never grants authority |
| Capability broker | Verified harness-capability matching and fallback | Used during plan validation and dispatch preflight |
| Harness capability registry | Native/rendered/partial/unsupported claims | Determines which coordinator and worker seams may be claimed |
| Workspace manifest | Declared multi-repository boundary | Required for an initiative spanning repositories |
| Workspace work items | Private planning/source records | May be linked or imported as bounded source context, never used as runtime state |
| Memory v2 | Explicit semantic publication and bounded recovery | Initiative results may be proposed for later publication, never auto-promoted |
| Knowledge plane | Canonical reviewed shared knowledge | Read through existing lookup; writes require existing promotion workflow |

The existing `loop-operator` is not silently repurposed as the Control
coordinator. It manages iterations inside one workflow and lacks the
initiative graph, action broker, task ownership, and recovery contract defined
here. Add a distinct coordinator role so the two responsibilities remain
auditable.

## Terms

### Initiative

A bounded orchestration unit for one operator-approved objective. It owns the
objective, acceptance criteria, repository allowlist, plan revisions,
coordinator record, task-node graph, budgets, decisions, and event history. It
owns no source working copy itself.

### Coordinator

One Asha agent run assigned to reason about an initiative. It runs under a
normal supported harness and calls versioned orchestration commands. It is a
cooperative but fallible same-UID client of deterministic Control mechanisms,
not part of the state engine. Its submitted plans, actions, results, and text
are untrusted input even though the local process is outside Asha's containment
claim.

### Coordinator generation

One monotonically increasing coordinator lifetime. Every handshake, wait,
checkpoint, approval request, directive, and action names the active
generation. Replacement advances the generation before launch and permanently
fences earlier processes, even if replacement fails.

### Action

One journaled state-changing request. An action has a UUID, actor identity,
exact payload digest, active plan digest, expected state revision, durable
phase, and stored outcome. Coordinator actions also carry the active
generation. Repeating the same identity returns the same outcome; changing the
payload under an existing identity is refused.

### Node

One unit in the approved dependency graph. A node has a type, goal, repository,
role, harness preference, dependencies, acceptance condition, attempt policy,
and current orchestration state.

Initial node types are:

- `work`: perform bounded repository work;
- `research`: inspect and report without mutation when the harness mode is
  actually read-only or the task has no source write charge;
- `review`: independently examine a named change and return a verdict;
- `verify`: invoke the controller runner for an approved verification
  specification against exact terminal seals;
- `compose`: combine named sealed candidates in a fresh isolated workspace and
  produce one new sealed terminal candidate without landing it;
- `decision`: wait for an operator answer.

`decision` and `verify` nodes are controller gates. They have no worker Control
task or attempt. A verification node owns controller evidence records instead.
Research and review nodes use ordinary isolated tasks, but their accepted
outcomes must meet the non-mutation and seal-binding rules defined below.

Do not encode every specialist name as a node type. Specialist roles and
existing workflows are node configuration.

### Attempt

One Control task launched for one executable node. A retry creates a new
attempt and workspace from that node's original exact approved base, whether a
repository baseline or qualifying upstream success seal. A repair creates a new
attempt from the exact success seal named by accepted findings. A salvage
attempt starts from the lineage's original approved repository scope baseline;
it may inspect one failure seal as a read-only input but never uses that seal or
failed workspace as its base. Earlier attempts remain retained and linked.

### Result

A bounded, explicit semantic report published by a worker through the
controller result interface. It contains claim status, summary, changed paths,
verification attestations, concerns, and requested follow-up. It is not a
transcript, terminal capture, artifact identity, or controller evidence. It is
a worker attestation and cannot release a dependency until the controller
completes the seal protocol.

### Seal

An immutable controller record binding one attempt's result claim, exact base,
original scope-baseline identity, final immutable jj commit ID, delta and
cumulative tree/diff/changed-path digests, process-exit evidence, repository
identity, and controller snapshot evidence. A success seal may satisfy a work
dependency. A failure seal retains exact partial work as a read-only input for
an explicitly approved salvage attempt but never represents success or a base.

A sealed workspace is terminal: it cannot resume, respawn, receive a
directive, or host another run. The retained workspace pins the sealed commit
for the life of the first release. Reconciliation detects later drift and
pauses the initiative without rewriting the seal.

### Artifact

A hash-bound reference to a retained file, seal, or exact immutable jj commit.
A logical jj change ID is lineage, not content identity. The initiative record
stores metadata and a contained path or immutable identity, not an arbitrary
file body.

### Gate

A deterministic transition requirement such as operator approval, dependency
success, controller-observed verification, independent review, or scope
validation.

Authority and evidence categories remain distinct:

- a user approval authorizes one exact requested action;
- policy authorization says the action class and payload fit the current
  envelope;
- dependency satisfaction is a deterministic fact derived from qualifying
  upstream outcomes;
- a review verdict is an independent judgment bound to one exact seal;
- a verification outcome is controller-observed executable evidence;
- a coordinator decision is an untrusted proposal or strategy choice.

No category implies another. In particular, approval does not make a forbidden
action safe, a coordinator choice does not satisfy policy, and a worker or
reviewer claim does not become controller verification.

### Event

An immutable, ordered record of an accepted controller transition or qualified
external fact. Events support recovery and coordinator wakeup. They do not
replace current task snapshots.

## Feature list

### 1. Initiative registry

- Create, list, show, pause, resume, reconcile, and archive initiatives.
- Persist one record tree per initiative beneath the Asha Control state root.
- Link every child node attempt to an ordinary Control task ID.
- Support one repository directly or several repositories declared by one
  Asha workspace manifest.
- Preserve archived initiative state and every retained task workspace.

### 2. Approved objective and execution envelope

An initiative begins with:

- one bounded objective;
- measurable acceptance criteria;
- explicit in-scope repositories;
- allowed harnesses and roles;
- maximum concurrent tasks;
- maximum attempts per node;
- maximum total child tasks;
- optional wall-clock deadline;
- verification expectations;
- nested-workflow policy;
- retained-storage warning and dispatch-pause thresholds;
- a fixed v1 denial of external writes, landing, publication, and removal.

The coordinator cannot edit this envelope. It submits an amendment request
when new scope is required.

### 3. Coordinator agent

- Launch one initiative-scoped `control-coordinator` agent through an existing
  live-qualified Asha harness only after Orchestration Core passes its evidence
  gate.
- Supply a bounded initiative snapshot, capability evidence, existing workflow
  catalogue references, and the action protocol.
- Let the coordinator propose plans, dispatch ready nodes, inspect results,
  request review or verification, and escalate decisions.
- Require a controller-accepted startup handshake naming initiative,
  coordinator ID, generation, protocol version, harness session identity, and
  event cursor before accepting actions.
- Increment and persist generation before every replacement launch; permanently
  fence actions, waits, directives, and checkpoints from older generations.
- Keep coordinator reasoning replaceable: restart from authoritative records
  without replaying a complete conversation.
- Record harness, process/run identity, generation, handshake state, last
  accepted action, last event cursor, and checkpoint time.

### 4. Versioned task graph

- Store a directed acyclic graph of nodes and dependencies.
- Reject missing references, cycles, ambiguous artifact lineage, undeclared
  repositories, unsupported harnesses, and graphs above configured bounds.
- Make plan revisions immutable and identify the approved active revision.
- Require operator approval of the first executable plan.
- Require another approval for scope-expanding revisions; allow bounded
  within-scope repair nodes under the approved retry policy.
- Require an explicit `compose` node whenever more than one sealed mutating
  candidate contributes to one repository deliverable.
- Require exactly one non-superseded terminal sealed candidate per changed
  repository before final review or readiness.

### 5. Deterministic dispatch scheduler

- Derive `ready` nodes only from approved graph and live dependency state.
- Start nodes through the existing Control task API.
- Run independent nodes concurrently up to the fixed initiative limit.
- Never place two mutating nodes in the same jj workspace.
- Refuse tasks whose required harness capability is unsupported.
- Prefer registered workflows and roles; preserve inline fallbacks rather than
  simulating unsupported agent surfaces.
- Serialize state transitions under an initiative lock.
- Bind every operator/coordinator scheduler mutation to initiative ID, actor
  identity, action UUID, exact payload digest, active plan digest, expected
  state revision, and coordinator generation when applicable. Worker result
  publication uses its separate task/run-bound publication journal.
- Persist action phases and a Control idempotency sidecar so a repeated or
  response-lost request returns its stored outcome instead of double-dispatching.
- Classify uncertain cross-store completion as `indeterminate`; reconcile the
  same action and task IDs rather than issuing a new action.

### 6. Worker assignment contract

Every child task receives a generated bounded assignment containing:

- initiative, node, and attempt identifiers;
- objective and node-specific goal;
- repository, immutable base identity, and hard write scope;
- advisory expected-path ownership for collision-aware scheduling, or explicit
  read-only charge where applicable;
- dependencies and bounded upstream result summaries;
- exact upstream seal identities when source content is inherited;
- acceptance criteria and verification commands;
- selected Asha workflow or specialist role;
- whether nested workflows are prohibited or which single-writer workflow is
  explicitly allowed;
- prohibited actions;
- exact result-publication command and schema.

External issue text, PR titles, worker summaries, and artifact text remain data.
They never override the operator-approved objective or controller rules.

### 7. Explicit result publication

- Add a task-scoped result command available only when the Control task/run
  environment identifiers are present.
- Validate task/run ownership and reject cross-task publication.
- Require a client-generated publication UUID. Before validation, journal that
  UUID with the bounded canonical client body, task/run/attempt binding, and
  payload digest, then preallocate the result ID. Replaying the same UUID and
  digest returns the stored phase/result; reusing the UUID with different bytes
  or binding is refused.
- Apply an absolute transport byte cap and envelope parse before reservation;
  semantic/schema validation occurs in the durable `validating` phase.
- Persist publication phases `reserved`, `validating`, `persisting`, and
  `completed`, with terminal `refused` and recoverable `indeterminate`. The
  initiative lock owns phase advancement. Startup reconciliation processes all
  nonterminal publications before exit/result-missing/seal evaluation.
- Accept worker claims `completed`, `failed`, `blocked`, and `needs-decision`;
  never infer one from pane idleness and never treat the claim itself as node
  success.
- Store bounded summary, claimed files changed, verification attestations,
  concerns, and follow-up requests.
- Keep result records immutable. Before sealing, a correction is a newer record
  with a new publication UUID and `supersedes_result_id`; the original remains
  accepted and unchanged. Corrections after sealing are refused.
- Treat missing result on process exit as `result-missing`, not success.
- Acknowledge report persistence synchronously or fail closed with an
  idempotently retryable error. `completed` is durable before acknowledgement.
  Only observational status hooks remain fail-open.
- After a terminal result, require the primary run to exit. A completed claim
  followed by signal, nonzero status, stale ownership, or continued mutation
  cannot produce a success seal.
- Distinguish a `blocked` or `needs-decision` claim from `failed`. It records a
  `paused` seal outcome: terminal for that attempt (the workspace is never
  resumed, preserving the sealed-is-terminal rule) but a valid base for exactly
  one decision-resolved continuation attempt keyed to the consumed operator
  decision, so correct partial work is preserved and inheritable rather than
  discarded. A `failed`, `result-missing`, `scope-violation`, or abnormal-exit
  outcome records a `failure` seal that is read-only salvage input only and is
  never a base. A `paused` seal never releases a dependency by itself.
- After process exit, persist a no-outcome seal-preparation record, take the
  final jj snapshot, and compute base, commit/tree/diff, operation, and changed
  paths.
- Compare the cumulative diff from the original approved scope baseline with
  the assignment's hard repository/path scope before fixing seal outcome. An
  out-of-scope diff selects failure intent with `scope-violation` and cannot
  release dependencies. Separately compare the attempt delta with advisory
  expected-path ownership and record divergence for composition/conflict
  decisions.
- Fix success intent only for an accepted `completed` claim, controller-proven
  normal zero exit, clean identity reconciliation, and valid hard scope. Then
  atomically publish the immutable success or failure seal matching its fixed
  intent.
- Permit an immutable failure seal after abnormal exit for retained salvage.
  It may be read-only input only to an operator-approved salvage attempt based
  on the original scope baseline; it never becomes a base or satisfies success.

### 8. Evidence-aware review and verification

- Represent review as an ordinary graph node with an independent Control task,
  attempt, verdict record, and retained evidence. Represent final verification
  as a controller-owned gate and runner, not a worker self-report.
- Give reviewers the approved specification digest, active plan digest, repository
  identity, exact seal, immutable commit, base, and diff digest rather than a
  worker handoff or live writer workspace.
- Run review in a distinct task/session. Accept a review verdict only if the
  review workspace has no tracked mutation relative to its target seal.
- Distinguish spec compliance, correctness/safety review, and executable
  verification.
- Route confirmed findings into bounded repair nodes.
- Invalidate review and verification bound to an older seal after repair or
  composition.
- Run final verification through a controller-owned runner against a fresh
  explicit-base materialization of the terminal seal. Record approved argv,
  cwd, environment policy, process identity, start/finish, exit status, signal
  or timeout, bounded output digest, and pre/post candidate identity.
- Treat worker-reported commands and exit codes as attestations only.
- Cap repair cycles and escalate repeated failure.
- Reach `ready-for-integration` only when required independent review and
  controller-observed verification bind to the exact candidate/verification
  bundle — a one-member bundle for a single-repository initiative, an ordered
  multi-member bundle across repositories — so the gate binds to one record
  shape in both cases.

### 9. Coordinator-mediated follow-up

- Support bounded coordinator directives to a task only through Control.
- Treat mid-run directives as optional after the first coordinator release,
  not an acceptance dependency for Orchestration Core.
- Deliver a directive only to a live, unsealed attempt at a live-proven safe
  turn boundary, or include it in a new attempt's initial context.
- Refuse directives, resume, respawn, or new runs against a sealed workspace.
- Do not type arbitrary text into a working pane based only on a stale state.
- Record every accepted directive as an event with sender, target, digest, and
  delivery evidence.
- Defer arbitrary worker-to-worker mailboxes and broadcast communication.

Most work should complete from a self-contained assignment, one durable result
claim, normal exit, and one controller seal. Mid-run conversation is an
exception, not the primary coordination mechanism.

### 10. Operator approvals and intervention

- Display pending plan, scope, limit, advisory-gate-bypass, salvage, and
  termination requests.
- Approve or reject by immutable request ID and digest.
- Pause an initiative without killing running tasks.
- Stop future dispatch while preserving current work.
- Request graceful stop of a selected child task.
- Require explicit operator action for forced termination.
- Attach to the coordinator or any worker through the existing Control popup.

### 11. Bounded autonomy and circuit breaking

The deterministic engine enforces:

- concurrent task limit;
- total child-task limit;
- attempt limit per node;
- repair-cycle limit;
- coordinator action rate limit;
- wall-clock deadline when configured;
- repeated-identical-failure detection;
- no-progress event-window detection;
- pause on stale ownership or conflicting live evidence;
- nested-workflow prohibition or explicit single-writer allowance;
- retained byte/inode inventory and dispatch pause at configured thresholds.

Token and monetary limits are accepted only from live-proven harness usage
events. When that evidence is absent, the TUI says `unavailable`; it does not
estimate cost by parsing transcripts.

### 12. Recovery and restart

- Rebuild initiative state from current records, immutable events, Control task
  reconciliation, action/seal journals, idempotency sidecars, and accepted
  results.
- Restart a dead coordinator under a higher persisted generation, require a new
  live handshake, and begin after its last accepted event cursor.
- Fence the prior generation before replacement and reject stale checkpoints
  through compare-and-swap on generation, prior checkpoint digest, and event
  cursor.
- Never relaunch a child node merely because the TUI or coordinator exited.
- Detect orphaned Control tasks linked to an initiative and require deliberate
  adoption or exclusion.
- Preserve partial, failed, and superseded attempts for inspection.
- Preserve seals, composition candidates, verification materializations, and
  bounded log digests. Report storage consumption and pause new dispatch at
  configured thresholds; never delete automatically.
- Make `pause`, coordinator crash, shell exit, popup close, and TUI exit
  non-destructive.

### 13. Minimal integrated TUI

- Extend `asha control`; do not add another UI executable.
- Provide task and initiative modes using the same controller data.
- Show initiative state, coordinator state, graph nodes, dependency blockers,
  attempts, approvals, evidence, and recent events.
- Open existing tmux popups for coordinator or worker interaction.
- Use text, optional semantic color, and stable keyboard commands only.
- Remain fully reconstructable from controller state after restart.

### 14. Audit and inspectability

- Store immutable action request/payload digests, terminal outcomes,
  transitions, decisions, and result digests.
- Provide human and versioned JSON views.
- Record refusals with exact policy or invariant failures.
- Keep event bodies bounded and exclude prompts, terminal output, unrestricted
  environment, arbitrary tool arguments, and secrets. Approved verification
  argv and its environment-policy identifier are deliberate evidence.
- Make every child-task launch attributable to an approved plan node and one
  durable actor action, whether issued by the operator or coordinator.

## End-state operator workflow

The completed target supports this sequence after Orchestration Core has first
proved sealing, composition, review, and verification through CLI/JSON:

```text
1. Operator creates an initiative with objective, criteria, repositories,
   limits, and coordinator harness.
2. Control launches the initiative-scoped coordinator.
3. Coordinator queries capability/process registries and proposes a task graph.
4. Operator reviews and approves the graph in the TUI or CLI.
5. Deterministic scheduler starts ready child tasks through Asha Control.
6. Coordinator waits on bounded initiative events rather than polling terminals.
7. Workers explicitly publish provisional result attestations and exit.
8. Control seals exact retained artifacts; only sealed successes release
   dependents.
9. Independent nodes run in parallel; composition produces one terminal sealed
   candidate per changed repository.
10. Coordinator requests bounded repair, review, verification, or a human decision.
11. Independent review and controller-observed verification bind to the same
    terminal candidate set.
12. Required gates pass or configured limits stop further dispatch.
13. Initiative reaches ready-for-integration, partial, failed, or cancelled.
14. Operator attaches to retained tasks, integrates selected changes through a
    separately authorized workflow, and archives the initiative when finished.
```

At no point does the coordinator own merge or deletion authority.

## Architecture

```text
                         operator
                    CLI / Asha Control TUI
                              |
                              v
               +-----------------------------+
               | deterministic Control layer |
               |-----------------------------|
               | initiative store            |
               | plan validator              |
               | dependency scheduler        |
               | action journal/idempotency  |
               | result, seal, review store  |
               | verification runner         |
               | event and approval broker   |
               | budgets and circuit breaker |
               +-------------+---------------+
                             |
              accepted actions only
                             |
          +------------------+------------------+
          |                                     |
          v                                     v
  coordinator Asha run                  existing task controller
  (Claude/Codex/etc.)                  task -> jj -> tmux -> run
          |                                     |
          | plan/reason/wait                    +--> worker task A
          |                                     +--> worker task B
          +---- versioned JSON requests         +--> reviewer task
                                                +--> composition task
                                                +--> verification materialization
```

The coordinator is replaceable intelligence. The Control layer is the durable
mechanism. Worker processes remain ordinary Asha Control tasks and require no
special multiplexer or UI runtime.

### Runtime bootstrap contract

Before an initiative may activate or dispatch, orchestration performs a
versioned runtime capability handshake with the completed Control API, task
creation/idempotency seam, process broker, jj workspace/seal support, selected
worker harness adapters, and result protocol. It fails closed on any absent or
incompatible required capability. Installed commands, capability declarations,
or rendered agent files are not proof that the runtime seam works. Coordinator
launch adds its own handshake described below.

## Coordinator agent contract

### Launch

The coordinator is launched through a normal Asha harness wrapper and receives:

```text
ASHA_CONTROL_MANAGED=1
ASHA_ORCHESTRATION_INITIATIVE_ID=<uuid>
ASHA_ORCHESTRATION_COORDINATOR_ID=<uuid>
ASHA_ORCHESTRATION_COORDINATOR_GENERATION=<integer>
ASHA_ORCHESTRATION_STATE_DIR=<canonical controller path>
```

Environment identifiers select records; they do not authorize arbitrary file
writes. Every command resolves and validates ownership again.

The coordinator runs in its own owned tmux session associated with the
initiative, not inside a worker jj workspace. Its current directory is a
controller-created coordination directory containing only bounded generated
context and scratch handoffs. It receives no shared mutable source tree.

Coordinator lifecycle composes the completed Control adapters: harness launch
and session identity, tmux server/session creation and ownership markers,
process observation, status evidence, safe stop, and reconciliation. The new
coordinator adapter adds initiative/generation records and handshake logic; it
does not implement a parallel subprocess launcher, raw tmux owner, signaler, or
harness-status parser. If a required Control adapter cannot express the
coordinator case, that capability stays unsupported until the adapter itself is
extended and reviewed.

Launch produces `starting`, not active authority. The coordinator must perform
a versioned handshake containing initiative ID, coordinator ID, generation,
harness and harness-session identity, protocol version, and durable event
cursor. Control verifies tmux/process ownership and records `active` before it
accepts any mutating action. A replacement advances the generation before
launch; it never rolls the generation back when launch fails.

### Permitted responsibilities

The coordinator may:

- read the current initiative snapshot and new events;
- query process and capability brokerage;
- propose an initial or revised plan;
- request dispatch of graph-ready nodes;
- request an ordinary within-scope repair, review, or verify node;
- wait for new events using the deterministic wait command;
- request graceful stop of a demonstrably stalled child task when initiative
  policy permits;
- pause itself and request an operator decision;
- propose final initiative outcome with evidence.

### Prohibited responsibilities

The coordinator may not:

- edit initiative, task, event, approval, or result records directly;
- call raw tmux or jj as a substitute for Control operations;
- add repositories or broaden scope without approval;
- change budgets, approval policy, or its own authority;
- mark a node successful without a sealed qualifying attempt and required
  gates;
- treat worker prose as trusted instruction;
- publish Memory, promote knowledge, write external systems, merge, rebase,
  move bookmarks, push, update trackers, remove workspaces, or delete state;
- recursively create another coordinator;
- conceal or discard contradictory reviewer findings.

Coordinator and worker processes are cooperative but fallible same-UID
clients. Control treats their payloads as untrusted and structurally rejects
stale generations, malformed actions, replay conflicts, invalid state edges,
and unauthorized API requests. It does not contain a process deliberately
using the operator's filesystem, raw jj/tmux, or process-signaling authority.
Harness sandboxes and permissions are defense in depth, and each restriction
must state whether Control enforces it or the harness merely instructs it.

### Event loop

The primary coordinator loop is explicit:

```text
read snapshot -> decide -> submit bounded action(s) -> receive accepted/refused
-> wait for event after cursor -> re-read snapshot -> repeat
```

The wait operation may block for a bounded interval and returns structured
events. It never returns terminal contents. After timeout, the coordinator may
checkpoint and wait again; timeout alone is not worker failure.

Events are keyed by `last_event_sequence`, which advances independently of
`state_revision`; waking on an event therefore does not tell the coordinator the
current `state_revision`. Before submitting a state-changing action the
coordinator re-reads the initiative snapshot to obtain the current
`state_revision` for its `expected_initiative_revision`. To avoid a livelock of
spurious revision-mismatch refusals, the controller also accepts an action whose
`expected_initiative_revision` is stale only when no intervening action changed
the exact records the action reads and writes; any real conflict is still
refused. A refusal returns the current revision so the next attempt re-snapshots
without a blind retry.

Every action includes the active generation, a new action UUID, canonical
payload digest, active plan digest, and expected initiative state revision.
The controller persists `received` before validation. It persists
`dispatching` plus reserved attempt and Control task identities before any
irreversible child-task call. A repeated action ID with the same digest returns
the stored phase/outcome; the same ID with different bytes, generation, plan,
or expected revision is refused. `indeterminate` actions are reconciled through
the same idempotency sidecar and Control creation journal and are never
redispatched under a fresh ID.

### Recovery

The coordinator periodically publishes a small checkpoint containing:

- active plan revision;
- last event cursor;
- nodes currently under consideration;
- pending decision request, if any;
- bounded rationale for the next action.

On restart, the controller supplies the checkpoint plus all later events. The
new run re-evaluates current live state rather than trusting the old rationale.
Checkpoint replacement uses compare-and-swap over coordinator ID, generation,
prior checkpoint digest, and an event cursor no later than the durable event
tail. Coordinator checkpoints remain performance hints; action/seal journals,
immutable facts, and live Control reconciliation remain recovery authority.

## Initiative and graph model

Several transition rows below key off named source sets rather than a single
state. Each set is defined per machine so transition totality is checkable:

- **terminal** states end a record's execution. The exact members are taken from
  each machine's own state list and transition table below, not restated
  independently:
  - Initiative: `ready-for-integration`, `partial`, `failed`, `cancelled`,
    `archived`. The first four are terminal outcomes whose only remaining edge is
    to `archived`; `archived` takes no further edge. No terminal outcome returns
    to `running`.
  - Coordinator: `fenced`, `exited`, `failed` (terminal for that generation, per
    the coordinator table). `absent` is the pre-launch initial state and `stale`
    is an unresolved identity-conflict state pending fencing or replacement;
    neither is terminal.
  - Node: `succeeded`, `failed`, `cancelled`, `superseded`, `stale`. A
    `succeeded` node is terminal for execution but may still move to `superseded`
    under an explicit operator recovery acceptance; no other edge leaves a
    terminal state.
  - Attempt: `sealed-success`, `sealed-failure`, `sealed-paused`,
    `completed-readonly`, `launch-failed`, `failed-no-artifact`, `cancelled`,
    `stale`.
- **nonterminal** is every state of a machine that is not in its terminal set.
- **active state** (node, attempt) is any nonterminal state in which the record
  has been started and not yet resolved: for a node, `dispatching`, `running`,
  `evaluating`, `needs-input`; for an attempt, `dispatching`, `running`,
  `reported`, `awaiting-exit`, and the `*-seal-ready`/`sealing` states.
- **live state** (coordinator) is any started, not-yet-terminal coordinator
  state: `starting`, `active`, `waiting`, `needs-input`, or `stopping`. A
  coordinator in any live state, including `starting`, can be fenced by a newer
  generation, so a launch that fails mid-`starting` is still fenced.
- **cross-store uncertain state** (attempt) is any state whose durable action,
  Control creation journal, and result reservation do not agree on the same
  outcome; it resolves only to `indeterminate` pending reconciliation.

### Initiative states

```text
draft
planning
awaiting-plan-approval
approved
running
needs-input
paused
ready-for-integration
partial
failed
cancelled
archived
```

`ready-for-integration` means the approved acceptance, review, and verification
gates passed. It does not mean any change was integrated or published.

`partial` means useful retained work exists but one or more required nodes were
not completed within the approved envelope. `failed` means the initiative
cannot satisfy its acceptance criteria from qualifying seals and gate records.
Neither state permits automatic cleanup.

Initiative transitions are controller-owned:

| From | To | Actor/request | Required evidence |
|---|---|---|---|
| `draft` | `planning` | operator or Core plan import | valid objective and envelope |
| `planning` | `awaiting-plan-approval` | planner/coordinator proposal | valid immutable plan revision |
| `awaiting-plan-approval` | `planning` | controller | plan approval rejected, expired, or cancelled |
| `awaiting-plan-approval` | `approved` | operator | exact unexpired plan approval |
| `approved` | `running` | operator | prerequisite and limit preflight |
| `running` | `needs-input` | controller | pending decision or exhausted safe action |
| `running` | `paused` | operator or circuit breaker | accepted pause reason |
| `needs-input` | `running` | operator | exact decision consumed |
| `paused` | `running` | operator | reconciliation clean and limits available |
| `running` | `ready-for-integration` | controller | one terminal seal per changed repository plus all mandatory review/verification gates |
| `running` | `partial` or `failed` | controller proposal plus operator acknowledgement | terminal graph evidence and retained artifact inventory |
| any nonterminal | `cancelled` | operator | cancellation decision; no deletion |
| terminal outcome | `archived` | operator | reconciliation and retained-data inventory |

No terminal outcome returns to `running`. Further work creates a plan revision
and a new initiative or an explicitly defined continuation contract.

### Coordinator states

```text
absent
starting
active
waiting
needs-input
stopping
exited
failed
stale
fenced
```

| From | To | Actor/request | Required evidence |
|---|---|---|---|
| `absent` | `starting` | controller | generation persisted before launch |
| `starting` | `active` | coordinator handshake | matching tmux/process, protocol, harness session, generation, and cursor |
| `active` | `waiting` | coordinator | accepted bounded wait/checkpoint |
| `waiting` | `active` | controller | new durable event or timeout response |
| `active` or `waiting` | `needs-input` | coordinator/controller | accepted operator decision request |
| `needs-input` | `active` | controller | exact operator decision consumed |
| live state | `stopping` | operator/controller policy | graceful-stop request |
| `stopping` | `exited` | controller | matching normal process exit |
| `starting`, `active`, `waiting`, `needs-input` | `exited` | controller | matching normal process exit and no unresolved coordinator action |
| `starting`, `active`, `waiting`, `needs-input`, `stopping` | `failed` | controller | matching abnormal exit or launch failure |
| live state | `stale` | reconciliation | ownership or identity conflict |
| any prior live state or `stale` | `fenced` | controller | newer generation persisted |

`fenced`, `exited`, and `failed` are terminal for that generation. A replacement
is a separate higher-generation record linked to its predecessor. Old-generation
actions, waits, checkpoints, directives, and approval requests are refused
regardless of whether the old process remains alive.

### Node states

```text
proposed
approved
blocked
ready
dispatching
running
evaluating
needs-input
succeeded
failed
cancelled
superseded
stale
```

Control task state and orchestration node state are separate. A Control task
may be `idle` while its node remains `running`, or `exited` while its node is
being evaluated for a seal. `result-missing` belongs to the attempt, not the
node. Reconciliation never collapses those vocabularies.

| From | To | Actor/request | Required evidence |
|---|---|---|---|
| `proposed` | `approved` | operator plan approval | node included in exact approved plan |
| `approved` | `blocked` or `ready` | controller | dependency computation |
| `blocked` | `ready` | controller | all required sealed dependencies/gates satisfied |
| `ready` | `dispatching` | controller | accepted idempotent action and budget reservation |
| `ready` | `evaluating` | controller | `verify` gate begins against exact candidate bundle |
| `ready` | `needs-input` | controller | `decision` gate requests exact operator decision |
| `dispatching` | `running` | controller | linked Control attempt reconciles live |
| `dispatching` | `evaluating` | controller | linked attempt reaches terminal `launch-failed` evidence |
| `running` | `evaluating` | controller | attempt result/exit evidence requires sealing or gate evaluation |
| `needs-input` | `evaluating` | controller | exact decision consumed or other pending input satisfied |
| `needs-input` | `ready` | controller | approved recovery or new-attempt decision satisfies blockers |
| `evaluating` | `succeeded` | controller | type-specific qualifying outcome: success seal for `work`/`compose`, accepted read-only record for `research`/`review`, passed controller record for `verify`, or consumed decision |
| `evaluating` | `ready` | controller | retriable `work`/`compose` attempt failure with attempts remaining under the node cap and initiative budget; a fresh attempt is allocated from the original approved base with no operator decision |
| `evaluating` | `failed` | controller | attempt cap exhausted, a non-retriable failure, or a failed mandatory gate |
| active state | `needs-input` | controller | operator decision required |
| nonterminal | `cancelled` | operator | exact cancellation decision |
| unstarted node, or a started or succeeded node only under explicit operator recovery-plan acceptance | `superseded` | approved plan/repair | replacement lineage recorded; running attempt history is never rewritten |
| active state | `stale` | reconciliation | task, seal, repository, or plan identity conflict |

A failure is **retriable** when a fresh attempt from the original approved base
could plausibly differ: a `failed` claim, `launch-failed`, `abnormal-exit`, and
`result-missing` are retriable within the node attempt cap. A hard-scope
violation is **non-retriable** — a fresh attempt from the same base would
deterministically violate scope again — and moves the node straight to `failed`;
so does an exhausted attempt cap or a failed mandatory gate. Every autonomous
retry consumes one attempt from `max_attempts_per_node`, and the repeated-failure
circuit breaker still applies. A `paused` seal is not a failure and never enters
the retry path.

A decision-resolved continuation from a `paused` seal is operator-gated, is
limited to one continuation per paused seal, and consumes one attempt from
`max_attempts_per_node` exactly as any other attempt does, so it cannot evade the
node budget.

A `decision` node becomes ready after its dependencies, enters `needs-input`,
and can succeed only through a consumed operator decision. A `verify` node
moves from ready through the separate controller verification lifecycle.
Neither creates a worker attempt. The scheduler maps their node states without
pretending they are Control tasks.

### Attempt states

```text
allocated
dispatching
running
reported
awaiting-exit
success-seal-ready
failure-seal-ready
paused-seal-ready
sealing
sealed-success
sealed-failure
sealed-paused
readonly-ready
completed-readonly
result-missing
launch-failed
abnormal-exit
failed-no-artifact
indeterminate
cancelled
stale
```

| From | To | Required evidence |
|---|---|---|
| `allocated` | `dispatching` | action journal and Control idempotency sidecar reserve exact task ID |
| `dispatching` | `running` | Control task/run/workspace reconcile live |
| `dispatching` | `launch-failed` | Control proves no task became live and records launch failure |
| `running` | `reported` | durable validated worker result claim |
| `reported` | `awaiting-exit` | terminal claim accepted; no dependency release |
| `awaiting-exit` | `success-seal-ready` | mutating task; captured final snapshot, accepted `completed` claim, matching normal zero exit, clean ownership/jj identity, and valid hard scope |
| `awaiting-exit` | `paused-seal-ready` | mutating task; captured final snapshot, accepted `blocked` or `needs-decision` claim, matching normal zero exit, clean ownership/jj identity, and valid hard scope |
| `awaiting-exit` | `failure-seal-ready` | mutating task; captured final snapshot plus `failed` claim or hard-scope violation, with matching terminal process evidence |
| `awaiting-exit` | `readonly-ready` | research/review task; matching normal zero exit, no signal, and no tracked mutation |
| `abnormal-exit` or `result-missing` | `failure-seal-ready` | mutating task; reconciled final jj identity and failure-only seal intent |
| `abnormal-exit` or `result-missing` | `failed-no-artifact` | reconciliation proves no sealable artifact, or salvage is not permitted/requested |
| `success-seal-ready`, `failure-seal-ready`, or `paused-seal-ready` | `sealing` | fixed-outcome seal transaction intent persisted |
| `sealing` | `sealed-success` | immutable success seal matching success intent published atomically |
| `sealing` | `sealed-failure` | immutable non-success/salvage seal matching failure intent published |
| `sealing` | `sealed-paused` | immutable paused seal matching paused intent published; inheritable by one decision-resolved continuation |
| `readonly-ready` | `completed-readonly` | accepted typed research result or review verdict bound to exact target |
| `running` | `result-missing` | normal exit without durable result after bounded grace |
| `dispatching`, `running`, `reported`, or `awaiting-exit` | `abnormal-exit` | matching nonzero exit or signal |
| cross-store uncertain state | `indeterminate` | side effect cannot yet be proven present or absent |
| `indeterminate` | last uniquely proven phase or `launch-failed` | reconciliation of the same preallocated identities and journal evidence resolves the uncertain side effect |
| nonterminal | `cancelled` | operator decision and preserved workspace |
| active state | `stale` | identity conflict |

Sealed, `completed-readonly`, `launch-failed`, `failed-no-artifact`, `cancelled`,
and `stale` attempts are terminal. A completed claim followed by abnormal exit
cannot become `sealed-success`. An abnormal or result-missing mutating attempt
may either reach `failed-no-artifact` or receive a failure seal for explicit
salvage, but only a new salvage attempt can continue.

### Result, seal, review, verification, approval, and action states

These records have independent lifecycles:

| Record | States | Transition authority |
|---|---|---|
| Result publication | `reserved -> validating -> persisting -> completed`; `reserved/validating -> refused`; any nonterminal phase may become `indeterminate`; `indeterminate` returns to the uniquely proven phase or `completed/refused` after reconciliation | controller under initiative lock using publication UUID/digest and preallocated result ID |
| Result claim | `accepted` is terminal; a refused publication creates no result claim | controller atomically publishes the immutable accepted record; `claim_status` remains worker attestation |
| Seal | `preparing -> sealed-success`, `sealed-failure`, or `sealed-paused`; `preparing -> indeterminate`; published states are terminal | controller after final process/jj evidence; later drift is a separate immutable reconciliation fact |
| Review | `pending -> running -> submitted -> accepted-pass` or `accepted-findings`; any active state may become `failed`, `indeterminate`, or `stale` | controller accepts independent verdict bound to exact seal and plan/spec digest |
| Verification | `pending -> dispatching -> running -> passed` or `failed`; active state may become `indeterminate` or `stale` | controller-owned runner and pre/post candidate identity checks |
| Bundle | `binding -> compatible` or `incompatible`; `binding -> indeterminate`; `indeterminate -> compatible`, `incompatible`, or `binding` only after reconciliation; bound outcomes are terminal | controller after every member seal, review, and verification identity re-check |
| Approval | `requested -> approved`, `rejected`, `expired`, or `cancelled`; `approved -> consumed`, `expired`, or `revoked-before-use` | operator decision bound to exact digest, generation where applicable, and expiry |
| Action | `received -> validated -> dispatching -> completed`; `validated -> completed` for no-side-effect actions; `received/validated -> refused`; `dispatching -> indeterminate`; `indeterminate -> completed` or `refused` only after reconciliation | controller under initiative lock and idempotency journal |

Result supersession is a relation, not a state mutation. A newer immutable
result may name `supersedes_result_id`; the earlier record remains `accepted`,
and the controller derives the current claim only while the attempt remains
unsealed. A seal binds one exact result ID and ends result correction for that
attempt.

Required review and final verification cannot be bypassed for
`ready-for-integration`. Only explicitly advisory non-security gates may be
bypassed through an exact operator decision with recorded rationale. Security,
data-preservation, scope, seal, composition, review, and final-verification
gates are non-bypassable.

### Plan validation

Before approval or dispatch, the deterministic validator checks:

- schema version and bounded field sizes;
- unique node IDs and valid dependency references;
- acyclic graph;
- declared repository membership;
- one repository per mutating node;
- supported node type, role, workflow, and harness claims;
- explicit immutable base policy for each mutating node;
- one immutable scope origin per repository candidate lineage; repairs inherit
  it, composition inputs must agree on it, and salvage must use it as the base;
- refusal of every failure seal named as a base; an approved salvage plan may
  name it only as bounded read-only input;
- declared path ownership for scheduling, with the understanding that it is
  advisory metadata and never evidence of the files actually changed;
- exact upstream seal inheritance for every mutating node;
- an explicit `compose` node wherever multiple same-repository branches must
  become one candidate;
- exactly one declared terminal candidate producer per repository before final
  review and verification;
- a declared nested-workflow policy that preserves one writer per workspace
  and the initiative's outer limits;
- acceptance condition for every terminal node;
- required review and verification gates;
- task, parallelism, attempt, and graph-size limits;
- absence of all first-release-forbidden actions, including integration,
  deletion, publication, tracker mutation, push, merge, rebase, and bookmark
  movement.

The validator does not decide whether the plan is strategically good. That is
coordinator judgment followed by operator approval.

### Plan revisions

Plans are immutable records:

```text
plans/0001.json  proposed
plans/0002.json  approved
plans/0003.json  proposed amendment
```

One initiative pointer names the current approved plan digest. A revision may
supersede only unstarted nodes unless the operator explicitly accepts a
different recovery plan. Running and completed attempt history is never
rewritten to fit a new graph.

## Worker task and result contract

### Starting a worker

The scheduler translates an approved node into ordinary Control task input. It
does not create jj or tmux resources itself. Orchestration preallocates an
attempt ID and Control task ID, then asks Control to create that exact task
through Control's idempotent create-by-identifier seam (a required Control
amendment; see the prerequisite gate). That seam,
`asha task start --task-id <uuid> ...`, creates the task when the identifier is
absent and returns the existing task unchanged when the identifier is already
registered, so a lost acknowledgement or a crash-recovered redelivery can never
create a second Control task for the same attempt. This adds an interface to
Control; it does not extend the strict Control v1 task record, which
orchestration still treats as opaque and un-augmented. A separate orchestration
link sidecar binds:

```text
initiative_id
active_plan_digest
node_id
attempt_id
action_id
actor_kind                # operator or coordinator
actor_id
coordinator_generation    # required only for coordinator actor
expected_initiative_revision
control_task_id
control_task_record_digest
```

The task remains valid and inspectable even if orchestration state later fails.
Repeated delivery of the same action ID and payload digest returns the same
link and task identity. The same action ID with a different digest is refused.

### Assignment context

The worker receives only what it needs. Upstream results are summarized and
hash-linked. Mutating work starts from one exact immutable base identity: an
approved repository-baseline commit/tree for initial work or an exact upstream
success seal for dependent work. Generic retry reuses that node's original
base; repair uses the exact candidate success seal named by findings. Salvage
starts from the lineage's original approved repository scope baseline and
receives the named failure seal only as read-only reference material. A
composition node receives an ordered set of exact success seals and a declared
conflict policy. A change ID is never used as the artifact identity. Full
coordinator or worker conversations are not copied into later prompts.

Nested Asha workflows are opt-in per node. `code-orchestrate` is allowed only
when it can prove a single writer in the Control-owned workspace and inherit
the initiative's outer task, attempt, deadline, and approval limits.
`session-loop` is unsupported in jj-backed Control tasks until its Git-specific
rollback and worktree assumptions are adapted and independently reviewed. A
nested workflow cannot launch unmanaged parallel writers, create another
coordinator, or evade the initiative budget.

Enforcement is layered because the controller cannot observe a worker's
intra-process fan-out: it sees one Control task, one pane, one process, and the
final seal diff, not sub-agents inside the harness. So the single-writer and
outer-limit contract is (1) a required static plan declaration validated before
approval — a node without a conforming nested-workflow declaration is refused,
not run; (2) enforced at the seam the controller does own — a nested workflow
may create no additional Control task, workspace, tmux session, or run beyond
the one attempt, and any such attempt is a controller-observable violation that
trips the breaker; and (3) an instruction to the harness for what it does inside
its own process, which the controller does not contain (a same-UID worker can
disregard it, exactly as the threat model states). The breaker therefore trips
on observable evidence — an extra Control task/run/workspace under the attempt,
a budget overrun counted from Control task/attempt records, or a seal diff
outside hard scope — not on unobservable intra-process parallelism. Test 13
asserts refusal of an undeclared nested workflow at plan time and refusal of a
nested workflow that spawns a second Control task at runtime; it does not claim
to detect in-process fan-out.

### Result schema

Minimum result shape:

```json
{
  "contract": "asha.orchestration-result.v1",
  "publication_id": "client-generated uuid",
  "result_id": "controller-preallocated uuid",
  "payload_digest": "sha256 of canonical client body excluding result_id and payload_digest",
  "supersedes_result_id": null,
  "initiative_id": "uuid",
  "node_id": "implementation-a",
  "attempt_id": "uuid",
  "task_id": "uuid",
  "run_id": "uuid",
  "claim_status": "completed",
  "summary": "bounded text",
  "files_changed": ["relative/path"],
  "verification_attestations": [
    {
      "argv": ["python3", "-m", "pytest", "tests/unit"],
      "cwd": ".",
      "exit_code": 0,
      "finished_at": "RFC3339 UTC",
      "output_digest": "sha256",
      "summary": "bounded worker claim"
    }
  ],
  "concerns": [],
  "follow_up": [],
  "published_at": "RFC3339 UTC"
}
```

Command arguments are bounded arrays, not shell strings. Output is summarized;
full logs remain in task-local files when deliberately retained. Paths must be
relative, canonical within the task workspace, and non-symlink escapes.
Worker verification entries are attestations. They may guide later checks but
never satisfy a controller review or verification gate.

### Completion binding

An accepted result is still only a worker claim. It is accepted only when:

- the publication UUID/digest journal resolves to this preallocated result ID;
- task/run environment identity matches the record;
- the attempt is active for that node;
- the Control task's jj workspace and ownership still reconcile;
- required result fields validate;
- the payload is durably stored before the worker receives acknowledgement.

If acknowledgement is lost, the worker resubmits the same publication UUID and
canonical body. The controller returns the existing result ID and phase; it
does not publish a second result. A changed body under that UUID is a replay
conflict. `supersedes_result_id` is nullable and may name only the current
accepted result for the same unsealed attempt.

After controller restart, initiative reconciliation owns every nonterminal
publication under the initiative lock. The reservation contains the exact
task/run/attempt binding, canonical body digest, preallocated result ID, receipt
sequence, and attempt revision. Reconciliation orders it before later observed
process-exit handling. If the exact result file already exists, the controller
verifies its digest/binding and marks the publication `completed`. If it is
absent, the controller resumes deterministic validation or persistence using
the reserved body. If the preallocated path contains conflicting bytes, the
publication becomes `indeterminate`, the attempt pauses, and no result is
accepted or regenerated. `refused` and `completed` are terminal and replayable.

After result acceptance, the controller waits for the linked process to exit
normally with zero status and no signal, reconciles Control ownership and jj
identity, and begins sealing. No dependency is released before the seal is
published.

### Seal contract

A seal is the controller's immutable artifact record:

```json
{
  "contract": "asha.orchestration-seal.v1",
  "seal_id": "uuid",
  "initiative_id": "uuid",
  "node_id": "implementation-a",
  "attempt_id": "uuid",
  "task_id": "uuid",
  "run_id": "uuid",
  "outcome": "success",
  "repository_id": "stable generated identity",
  "scope_origin": {
    "jj_commit_id": "original approved repository-baseline commit id",
    "tree_digest": "sha256"
  },
  "base": {
    "kind": "repository-baseline|seal|composition-inputs",
    "jj_commit_id": "immutable base commit id or null for composition input set",
    "tree_digest": "sha256",
    "seal_ids": ["uuid"]
  },
  "read_only_failure_seal_ids": ["uuid only for explicit salvage"],
  "jj_commit_id": "immutable commit id",
  "tree_digest": "sha256",
  "diff_digest": "sha256",
  "cumulative_diff_digest": "sha256 from scope_origin",
  "changed_paths": ["relative/path"],
  "cumulative_changed_paths": ["relative/path from scope_origin"],
  "result_id": "uuid or null for result-missing failure seal",
  "process_evidence_id": "uuid",
  "sealed_at": "RFC3339 UTC"
}
```

Every seal named in `base.seal_ids` must be `sealed-success` and share the
recorded `scope_origin`. `read_only_failure_seal_ids` is empty except for an
approved salvage attempt, and none of its members contributes to the base tree.

The seal transaction first persists a `preparing` record without an outcome,
captures the final immutable jj commit and content digests, and checks the
cumulative diff from `scope_origin` against hard scope plus the attempt delta
against advisory ownership. Only then does it fix
success, failure, or paused intent (`outcome` is one of `success`, `failure`,
`paused`). Success requires the exact accepted `completed`
result, controller-proven normal zero exit, clean identity reconciliation, and
valid hard scope. A `blocked` or `needs-decision` claim with a clean normal
exit and valid hard scope fixes `paused` intent. The controller atomically
publishes only the matching seal
outcome. A hard-scope violation, abnormal exit, or missing result may produce a
`failure` seal for explicit salvage, but never a success or paused seal.

Publication makes the task workspace terminal for orchestration. The
controller does not resume or mutate it. Later drift is recorded against the
seal and pauses affected work; it does not rewrite the seal. A retry starts
from the original approved base. A repair starts from the exact prior success
seal. A decision-resolved continuation starts from the exact prior `paused`
seal, consuming the operator decision that unblocked it, and is limited to one
such continuation per paused seal. An explicit approved salvage attempt starts
from the original baseline and receives a failure seal only as a read-only
input. The controller rejects any plan that names a failure seal as a base;
a `paused` seal is a valid base only for its single decision-resolved
continuation.

### Review binding

A review node runs in a clean Control task materialized from the exact target
seal. Its assignment binds the approved specification digest, active plan
digest, repository identity, seal ID, immutable commit, base seal IDs, and diff
digest. The reviewer publishes a structured verdict with `pass` or `findings`,
bounded finding records, severity, evidence locations, and its target digests.
The controller accepts the verdict only after normal task exit and proof that
the review task made no tracked mutation relative to the target seal. Review is
read-only and gates subsequent writer work; it never performs that work. Tests
run after a writer exits are verification, not a second review.

Review findings never mutate the sealed target. An approved repair receives a
new task and workspace based on that exact seal. The resulting seal supersedes
the candidate and invalidates every review and verification record bound to the
older seal.

### Controller verification binding

Final verification starts only after one reviewed terminal seal exists for
each changed repository. The controller creates a fresh explicit-base jj
materialization for each seal, proves its pre-run commit/tree identity, and
executes only argv templates and environment policies fixed in the approved
verification specification. A coordinator cannot add a command at runtime,
and known external-write command classes are refused. This is policy control,
not containment of deliberately hostile repository code running as the same
OS user. For every command the controller records:

```text
verification ID; candidate bundle digest; repository and seal IDs;
argv; cwd; environment-policy ID; process identity; start and finish time;
exit status; signal or timeout; bounded output location and digest;
pre-run and post-run commit/tree identity
```

Any candidate mutation, identity mismatch, unapproved command, timeout, signal,
or nonzero required command fails the gate. A verification passes only when all
required commands pass and the candidate identities remain unchanged. The
fresh materializations and evidence bundle remain retained for inspection.

## Scheduling and concurrency

The scheduler is deterministic and event-driven. It does not ask a model which
nodes are technically ready.

At each accepted transition:

1. reconcile linked Control tasks;
2. reconcile indeterminate action-journal entries before accepting new work;
3. validate result claims, process exits, and seal transactions independently;
4. apply published seals and consumed operator decisions;
5. evaluate required review and verification records against exact seals;
6. compute dependency satisfaction;
7. mark eligible nodes `ready`;
8. enforce pause, deadline, retained-storage, nested-workflow, and concurrency
   limits;
9. dispatch one accepted actor action for a ready node;
10. emit immutable transition events;
11. wake the coordinator through its bounded wait result.

The coordinator chooses among ready nodes when several valid strategies exist.
The scheduler refuses requests for blocked or unapproved nodes.

Parallel nodes in one repository still receive separate jj workspaces. Declared
path ownership is advisory scheduling metadata. The seal records the actual
diff; hard repository/path-scope violations fail the attempt, while advisory
ownership divergence becomes explicit composition/conflict evidence. Multiple
same-repository seals never become a candidate through scheduler inference: an
explicit isolated `compose` task must consume them and publish one new terminal
seal. A conflict stops for repair or operator decision. The initiative never
silently rebases, combines, lands, or publishes changes.

## Events and directives

### Event types

The initial event vocabulary is deliberately small:

```text
initiative-created
plan-proposed
approval-requested
approval-decided
initiative-state-changed
coordinator-handshake-accepted
coordinator-generation-fenced
node-ready
action-received
action-refused
action-indeterminate
attempt-started
task-status-observed
result-published
result-missing
seal-preparing
seal-published
seal-drift-detected
review-submitted
review-accepted
verification-started
verification-finished
node-state-changed
directive-accepted
directive-delivered
limit-reached
storage-threshold-reached
coordinator-checkpointed
coordinator-restarted
reconciliation-conflict
```

Every event includes a sequence, UUID, timestamp, initiative ID, type, actor,
subject identifiers, payload digest, and bounded payload. Sequence allocation
is lock-protected. Event files are immutable after publication.

### Directives

The coordinator may submit a bounded follow-up directive such as:

- clarify one acceptance criterion;
- request a status/result publication;
- ask an unsealed worker to clarify or publish its result;
- request graceful checkpoint and stop.

Delivery requires a harness-specific, live-proven safe seam. If none exists,
the controller records the directive as pending and offers these deterministic
fallbacks:

1. let the operator attach to the still-live unsealed task;
2. create a new repair attempt with the directive in its initial context;
3. mark the node `needs-input`.

Unsupported delivery is never simulated by unguarded `tmux send-keys`.
Sealed task workspaces refuse directives and resume requests.

## Authority and approvals

### Automatically permitted inside an approved plan

- read initiative, task, result, capability, and event records;
- reconcile linked Control tasks;
- start an approved, graph-ready node within all limits;
- allocate an autonomous retry attempt for a `work`/`compose` node whose prior
  attempt failed retriably, within the node attempt cap and initiative budget,
  from the original approved base and with no operator decision (distinct from
  a repair, which requires a success seal and coordinator/operator authority);
- start required review tasks and controller verification gates already present
  in the plan;
- record worker results and coordinator checkpoints;
- wait for new events;
- pause dispatch when a deterministic limit trips.

### Coordinator may request, controller validates

- within-scope repair attempt up to the approved cap;
- an exact salvage approval request naming one failure seal and its scope
  origin; no salvage task dispatches from this request;
- graceful stop of a stalled worker;
- a plan amendment;
- an operator decision;
- a final initiative outcome.

### Operator approval required

- initial executable plan;
- adding a repository or widening repository scope;
- increasing concurrency, attempts, task count, deadline, or usage budget;
- tightening or replacing an acceptance/review/verification specification;
  mandatory review and final-verification gates cannot be removed;
- forced termination;
- one salvage attempt bound to the exact failure seal, original scope origin,
  active plan digest, hard scope, and salvage action payload;
- adopting an orphaned or foreign task into the initiative;
- bypassing a failed non-security gate with recorded rationale.

Security and data-preservation gates cannot be bypassed through the
orchestration API.

### Unsupported even with operator approval in v1

- external writes and tracker mutation;
- draft PR creation, publication, or remote API side effects;
- integration, merge, rebase, bookmark movement, or push;
- task/workspace removal, retention expiry, or any deletion;
- changing a sealed workspace or treating it as a repair workspace.

The controller refuses these action classes before approval lookup. Approval
is not a generic escape from the release's authority boundary.

### Approval records

An approval binds:

```text
request ID + initiative ID + action class + exact action/artifact/config digest
+ active plan digest + expected initiative revision + coordinator generation
where applicable + expiry
```

A changed payload requires a new request. Decisions are single-use where the
action mutates state. A coordinator cannot replay approval for a different
node or plan revision. Coordinator generation is part of the match only for a
coordinator-granted request; an operator-granted approval (including a salvage
approval) is authorized by the operator, not by a coordinator lifetime, so it
survives coordinator replacement and is consumable by the operator or the
current-generation coordinator as long as its node, plan digest, expected
revision, and artifact bindings still match.

### Salvage approval transition

A failure seal creates no salvage authority. The operator or coordinator may
submit a bounded salvage proposal; the controller validates that the failure
seal, original `scope_origin`, hard scope, active plan, and proposed read-only
input/action all match current records, then creates an approval in `requested`.
Only the operator may move it to `approved`.

Dispatch is a separate actor action. Under the initiative lock, the controller
requires the exact unexpired approval, consumes it atomically with reservation
of the salvage attempt/task identities, and fixes the attempt base to the
approved scope baseline. Reusing the approval, substituting another failure
seal/scope origin/action, or changing the plan digest is refused. A crash after
consumption reconciles through the same action journal and preallocated IDs; it
does not consume a second approval or dispatch another task.

## Failure and circuit-breaker behavior

### Worker failure

On failed process, accepted non-success result, stale ownership, missing result,
seal failure, review findings, or verification failure:

1. preserve the Control task and workspace;
2. record exact evidence;
3. publish a failure seal when enough repository identity exists to support
   later inspection or explicit salvage;
4. distinguish retry, repair, and salvage;
5. require a materially changed rationale and exact base policy;
6. create a new task/workspace for the next attempt;
7. escalate after the cap or repeated equivalent failure.

A retry uses the node's original approved base. A repair uses the exact success
seal for the candidate that failed review or verification. It cannot silently inherit a
mutable workspace or a moving change ID. Salvage uses the original approved
scope baseline and exposes the exact failure seal read-only; scope validation
uses the cumulative diff from that baseline, so forbidden paths cannot be
laundered through the salvage input.

### Coordinator failure

Coordinator exit changes no worker state. The initiative becomes
`needs-input` or remains `paused` after a grace interval. Running workers may
finish and publish results. No new tasks dispatch until a coordinator is
restarted with a higher generation or the operator takes over. Replacement
persists the new generation and fences the prior generation before launch; the
new process must
complete the capability/version/session handshake before becoming active.

### Scheduler or store failure

Fail closed for new dispatch. Worker result delivery uses a synchronous
controller command and acknowledges only after durable storage; a hook may
notify but is not the authoritative publication channel. Every operator or
coordinator state-changing action is journaled with actor identity, action ID,
payload digest, plan digest, expected state revision, coordinator generation
when applicable, phase, and preallocated object IDs. Worker publications use
the separate publication UUID/digest journal and preallocated result ID. If a
Control response is lost after dispatch, mark the action `indeterminate` and
reconcile the preallocated task identity before retrying. Never issue a second
side effect merely because the response was lost.

### No progress

No progress is based on bounded accepted events, not wall time alone. The
circuit breaker pauses when any configured condition occurs:

- no node transition or new result across the configured event window while
  the coordinator keeps requesting actions;
- same normalized failure class reaches the cap;
- coordinator action rate exceeds the limit;
- all remaining nodes are blocked;
- live evidence conflicts with ownership records;
- nested workflow evidence violates the one-writer or outer-limit contract;
- retained workspace/log/artifact storage reaches the approved pause threshold;
- initiative deadline or task budget is exhausted.

The pause event names the exact evidence and next operator choices. Storage
pressure never triggers automatic workspace, result, seal, event, or log
deletion.

## Memory, knowledge, and work-item boundaries

Initiative state is operational runtime state. It is not Asha Memory v2 and is
not injected into every future session.

- The coordinator receives the relevant project/workspace Memory snapshot at
  initiative creation plus later explicitly refreshed bounded context.
- Child tasks retain ordinary task-local Memory behavior from Asha Control.
- Results and events remain private controller state by default.
- The coordinator may propose candidate decisions or knowledge for publication.
- `/session:save` remains the sole semantic Memory publisher.
- Workspace knowledge promotion remains the reviewed canonical path.
- A workspace work item may be recorded as an initiative source, but runtime
  attempts never mutate the work-item record automatically.
- Global learnings continue to require their existing corroboration lifecycle.

No vector database or transcript mining is required for orchestration. If
future recall is needed, index the bounded initiative/result records through a
separately reviewed read-only adapter rather than changing their authority.

## State storage

State extends the existing Control XDG root:

```text
${XDG_STATE_HOME:-~/.local/state}/asha/control/
  initiatives/<initiative-id>/
    initiative.json
    plans/<revision>.json
    coordinator.json
    nodes/<node-id>.json
    attempts/<attempt-id>.json
    links/<attempt-id>.json
    result-publications/<publication-id>.json
    results/<result-id>.json
    seals/<seal-id>.json
    reviews/<review-id>.json
    verifications/<verification-id>.json
    bundles/<bundle-id>.json
    approvals/<request-id>.json
    actions/<action-id>.json
    evidence/<evidence-id>.json
    events/<sequence>-<event-id>.json
    locks/                         # runtime path may hold active lock files
```

The exact split may change after prior-art review, but these invariants do not:

- no one global mutable initiative database is required;
- one initiative lock serializes graph transitions;
- records use atomic same-directory replacement where mutable snapshots are
  necessary;
- plans, results, seals, reviews, verification evidence, decisions, and events
  become immutable after acceptance;
- directories are mode `0700`, files are mode `0600`;
- symlinked records and path escapes are refused;
- every record has a versioned contract and bounded size;
- current snapshots can be reconstructed or checked against immutable facts;
- archival removes no task workspace, jj state, logs, seals, or evidence;
- retained bytes are inventoried by class and initiative, and exceeding the
  configured threshold pauses new dispatch rather than deleting state.

### Minimum initiative record

```json
{
  "contract": "asha.orchestration-initiative.v1",
  "initiative_id": "uuid",
  "slug": "asha-orchestration",
  "label": "Asha orchestration layer",
  "state": "running",
  "objective": "bounded text",
  "acceptance_criteria": ["bounded criterion"],
  "scope": {
    "kind": "repository",
    "repository": {
      "repository_id": "generated stable orchestration identity",
      "project_id": "Memory v2 project id",
      "root": "/canonical/repository/root",
      "control_repository_id": "completed Control identity",
      "initial_identity_digest": "sha256"
    }
  },
  "active_plan": {
    "revision": 2,
    "digest": "sha256",
    "approval_id": "uuid"
  },
  "limits": {
    "max_parallel": 3,
    "max_total_tasks": 12,
    "max_attempts_per_node": 2,
    "max_repair_cycles": 2,
    "max_retained_bytes_before_pause": 10737418240,
    "max_retained_inodes_before_pause": 200000,
    "deadline": null
  },
  "coordinator": null,
  "state_revision": 27,
  "forbidden_action_classes": [
    "external-write",
    "tracker-mutation",
    "publication",
    "integration",
    "merge",
    "rebase",
    "bookmark-movement",
    "push",
    "workspace-removal",
    "retention-expiry",
    "deletion",
    "sealed-workspace-mutation"
  ],
  "last_event_sequence": 42,
  "created_at": "RFC3339 UTC",
  "updated_at": "RFC3339 UTC"
}
```

Objectives, criteria, summaries, rationales, and directives receive explicit
byte and item caps in the schema. Full model prompts are never persisted.

Paths are locators, not identity. At initiative creation the controller binds
the direct repository's Memory v2 project ID, canonical root, and completed
Control identity into one generated stable repository ID. A multi-repository
initiative instead uses a tagged `workspace` scope containing a generated
workspace ID, workspace Memory v2 project ID, canonical workspace root,
manifest membership digest, and an ordered child list. Every child binds its
generated repository ID, Memory v2 project ID, canonical root, Control identity,
and initial identity digest. Relocation requires explicit reconciliation.
Membership or identity drift pauses affected nodes rather than silently
rebinding them.

`coordinator` is nullable. Orchestration Core keeps it `null`. After the
coordinator extension is enabled it contains coordinator ID, active generation,
harness/session identity, handshake state, and predecessor generation. Core
records and actions never synthesize coordinator fields.

Final multi-repository verification uses one explicit-base fresh jj workspace
per repository terminal seal and exposes the complete set under one generated
verification bundle root. The controller runs an approved aggregate compatibility
specification against that simultaneous set, not merely one repository's tests
repeated independently. It captures argv, cwd, environment policy, process
evidence, output digest, and pre/post commit/tree/working-copy identity for
every member. Those materializations are retained with the bundle. No combined
mutable checkout becomes the source of truth.

The candidate/verification bundle is the immutable record that a multi-repository
`ready-for-integration` gate binds to. It is stored at `bundles/<bundle-id>.json`:

```json
{
  "contract": "asha.orchestration-bundle.v1",
  "bundle_id": "uuid",
  "initiative_id": "uuid",
  "aggregate_spec_digest": "sha256",
  "active_plan_digest": "sha256",
  "members": [
    {
      "repository_id": "stable generated identity",
      "seal_id": "uuid",
      "jj_commit_id": "immutable commit id",
      "tree_digest": "sha256",
      "diff_digest": "sha256",
      "materialization_id": "uuid",
      "review_id": "uuid",
      "verification_id": "uuid"
    }
  ],
  "controller_evidence_ids": ["uuid"],
  "outcome": "compatible|incompatible|indeterminate",
  "bound_at": "RFC3339 UTC"
}
```

Every member `seal_id` must be `sealed-success` and independently reviewed; the
ordered `members` set is fixed at binding and never rewritten. A single-repository
initiative uses the same schema with one member, so the readiness gate binds to
one record shape in both cases. The bundle is `compatible` only when every member
identity is unchanged from its seal and the aggregate specification's required
commands all pass under controller observation.

## Command and machine interface

User-facing names may be adjusted during CLI prior-art review, but the feature
surface requires these deterministic operations:

```text
asha initiative create       create bounded draft initiative
asha initiative list         list initiatives after linked-task reconciliation
asha initiative show         show graph, coordinator, gates, limits, and evidence
asha initiative plan         propose or inspect a versioned plan
asha initiative approve      approve an exact request digest
asha initiative reject       reject an exact request digest with bounded reason
asha initiative activate     start an approved initiative without requiring a coordinator
asha initiative action       submit/reconcile an exact actor-bound action document
asha initiative coordinator start|restart|stop
                             manage the optional initiative coordinator
asha initiative pause        stop new dispatch; preserve workers
asha initiative resume       resume dispatch after reconciliation
asha initiative stop         stop new dispatch and request selected runs stop safely
asha initiative reconcile    refresh initiative from Control task evidence
asha initiative archive      hide terminal initiative; preserve all task data
asha initiative events       show ordered bounded events
asha initiative storage      show retained workspaces, logs, seals, and thresholds
asha initiative doctor       probe prerequisites and harness support

asha task report             worker publishes a bounded result
asha task result             inspect accepted result claims for one Control task
asha task seal               inspect the controller seal for one attempt
```

Machine operations use the same executable with versioned JSON. Orchestration
Core operator commands and the later coordinator differ only in actor identity
and permitted action classes:

```text
asha initiative snapshot --json
asha initiative propose-plan --file PLAN --json
asha initiative action --file ACTION --json
asha initiative handshake --file HANDSHAKE --json
asha initiative checkpoint --file CHECKPOINT --json
asha initiative wait --after SEQUENCE --timeout SECONDS --json
```

Machine commands validate actor and initiative identity, active plan digest,
action identity, payload digest, and expected state revision. Coordinator
requests additionally require the active environment identity, generation, and
accepted handshake. They never trust a caller-supplied state-directory path or
shell expression.

Every machine response has one contract version, JSON-only stdout, diagnostics
on stderr, and a stable nonzero refusal code. The TUI calls these controller
functions directly or through the same typed layer; it does not duplicate
transition logic.

## Minimal TUI contract

### Placement

`asha control` remains the only interactive program. It gains a top-level mode
for initiatives while preserving the completed task view.

No daemon depends on the TUI. Closing it changes no initiative, coordinator,
or worker state.

### Layout

The initial initiative view is text-only:

```text
 ASHA CONTROL  [Initiatives]

 STATE       INITIATIVE              COORDINATOR  NODES       ATTENTION
 running     asha-orchestration      claude       3/7         -
 input       release-preparation     codex        4/6         plan approval
 partial     cache-migration         exited       8/10        2 failed

 asha-orchestration
  [done]    history           codebase-historian   task 7db1
  [running] implementation    tdd                  task b06e
  [blocked] review            reviewer             waits: implementation
  [blocked] verify            code-verify          waits: review

 Evidence: implementation task working; PostToolUse 8s ago
 Candidate: repo asha seal 91ce... | review pending | verify pending
 Limits:   parallel 1/3 | tasks 4/12 | attempts implementation 1/2
 Storage:  retained 2.1 GiB / pause at 10 GiB
 Events:   #38 attempt-started implementation
           #37 node-state-changed implementation -> running
```

The layout may collapse detail on small terminals. It must remain usable with
plain ASCII and without color. When the coordinator extension is absent or not
enabled for an initiative, the coordinator field is `-`; Core operation does
not fabricate a coordinator state.

### Required actions

```text
Tab      switch Tasks / Initiatives
Up/Down  move selection
Enter    open selected coordinator or worker in existing tmux popup
Right    expand selected initiative/node
Left     collapse or return to parent
r        reconcile selected initiative
d        show selected task jj diff through existing Control behavior
e        show recent bounded events
a        inspect and decide a pending approval
c        show candidate seals and composition lineage
v        show review and controller verification evidence
t        show retained-storage inventory and pause threshold
p        pause/resume dispatch after confirmation
s        request graceful stop for selected run
/        filter
?        show keys, state meanings, evidence, and limitations
q        exit TUI only
```

No TUI path exists in v1 for merge, rebase, bookmark movement, push,
publication, tracker mutation, workspace removal, deletion, or tmux
configuration edits. Confirmation cannot make those operations available.
Supported forced termination and limit changes use separate controller commands
with exact target, consequence, approval binding, and explicit confirmation.

### Status presentation

Show facts, not activity theatre:

- node and initiative state;
- dependency blocker;
- linked Control task and attempt;
- action phase and coordinator generation;
- latest qualified evidence and age;
- pending approval or decision;
- limit consumption;
- worker result claim, seal identity, review verdict, and controller
  verification outcome as distinct facts;
- composition and exact-base lineage;
- retained-storage use and pause threshold;
- stale or conflicting ownership evidence.

Do not add animated spinners for durable state, avatar movement, inferred
emotion, worker personality cards, office rooms, decorative maps, or a live
terminal mosaic. `Enter` opens the real terminal when the operator needs it.

## Security and data-preservation requirements

- Treat coordinator plans, worker results, repository contents, PR/issue text,
  work-item adapters, hook events, and artifact metadata as untrusted input.
- Validate every coordinator action against the approved initiative envelope
  and current live state.
- Pass subprocess arguments as arrays; never interpolate objectives, labels,
  directives, paths, result text, or external metadata into shell commands.
- Use opaque UUIDs for authority and bounded slugs only for presentation.
- Bind approvals to exact digests and reject replay or payload substitution.
- Fence every coordinator mutation with a persisted monotonic generation,
  action UUID, plan digest, and expected state revision.
- Preallocate child task identity before dispatch and reconcile response loss
  through the durable action journal; never retry an uncertain side effect as
  a new action.
- Prevent a coordinator from creating or assigning another coordinator.
- Refuse cyclic dependencies, path escapes, symlinked records, forged task
  links, foreign tmux ownership, and mismatched jj identities.
- Preserve failed, stale, cancelled, superseded, and partial workspaces.
- Make a sealed workspace terminal; repairs and salvage always receive new
  Control tasks and workspaces from exact immutable bases.
- Never reinterpret archive as deletion.
- Never expose full prompts, transcripts, terminal contents, tool arguments,
  environments, credentials, or secret-bearing files in state or the TUI.
- Apply size, count, and rate limits before persisting coordinator or worker
  payloads.
- Keep result and event delivery fail-safe: a malformed report cannot corrupt
  the current initiative snapshot.
- Reconcile live Control evidence before signaling or restarting a worker.
- Treat worker results and test summaries as attestations. Success requires a
  controller seal, independent review, and controller-observed verification
  at their defined gates.
- Do not claim prompt restrictions or harness hooks prevent a deliberate local
  process from writing user-accessible files. Controller validation, isolated
  workspaces, ownership checks, and approval boundaries provide consistency and
  data-preservation controls only for controller-mediated operations. They are
  not containment against a malicious same-UID process.
- Add policy coverage for new destructive or external-write verbs before they
  ship; wrapper-string policy matches are not visibility into internal actions.
- Do not implement integration or workspace removal as an incidental part of
  orchestration. Each requires its own reviewed authority and recovery design.

## Configuration and installation

Orchestration configuration is versioned separately from Control v1's strict
task configuration. The final pathname follows the completed Control
configuration conventions, but the document has its own contract:

```json
{
  "contract": "asha.orchestration-config.v1",
  "default_coordinator_harness": "claude",
  "max_parallel_tasks": 3,
  "max_total_tasks": 12,
  "max_attempts_per_node": 2,
  "max_repair_cycles": 2,
  "max_retained_bytes_before_pause": 10737418240,
  "max_retained_inodes_before_pause": 200000,
  "coordinator_wait_seconds": 120
}
```

Configuration values are hard ceilings the initiative may lower. Raising a
running initiative above its original approved envelope requires an explicit
operator decision. Invalid orchestration configuration refuses initiative
commands and does not affect ordinary `asha task` or `asha <harness>` use.
It is loaded lazily only after `initiative` or initiative-mode `control`
dispatch. A malformed or future orchestration document refuses those commands
without entering Control v1's parser. Control's task configuration and strict
task-record schema remain unchanged.

Recommended ownership after Control v1 is stable:

```text
bin/asha
  thin `initiative` dispatch addition

lib/control/
  existing task, jj, tmux, harness, reconciliation, TUI modules
  orchestration/
    model.py          initiative, plan, node, attempt, result schemas
    store.py          per-initiative locks, atomic records, immutable events
    graph.py          DAG validation and dependency state
    scheduler.py      deterministic readiness, limits, and dispatch
    actions.py        fenced action journal, idempotency, and approval broker
    links.py          immutable Control task/attempt sidecars
    results.py        task-scoped result-claim validation
    seals.py          exit/jj binding, immutable artifacts, scope checks
    composition.py    explicit-base candidate construction
    review.py         independent verdict binding and mutation checks
    verification.py   fresh-materialization controller evidence runner
    storage.py        retained-data inventory and pause thresholds
    coordinator.py    generation/handshake layer over Control harness, tmux,
                      process, stop, and reconciliation adapters
    reconcile.py      join initiative nodes with live Control task evidence
    tui_model.py      pure initiative tree/detail/event presentation model

plugins/session/agents/control-coordinator.md
  initiative-scoped reasoning role

plugins/session/
  narrow shared worker result instructions or skill only if prompt injection
  cannot provide an equivalent cross-harness contract cleanly

tests/python/test_orchestration_*.py
tests/test-orchestration.sh
```

Do not create a new plugin merely to hold one agent and one result protocol.
The deterministic engine extends Control; the portable coordinator role belongs
with session lifecycle machinery. If live harness evidence disproves that
placement, record the reason before changing ownership.

Every agent or command primitive change must update renderers, installers,
ownership manifests, doctor checks, capability records, enforcement
documentation, and target-specific tests required by `AGENTS.md`.

## Verification strategy

Each stage has its own cumulative tests and ship decision. A later extension is
not required to declare an earlier stage complete.

### Orchestration Core tests

Unit coverage includes:

- initiative, plan, node, attempt, result, seal, review, verification,
  approval, generic actor action, event, task link, evidence, and repository
  scope schemas, including explicit nullable/conditional fields;
- every legal and illegal Core transition, including launch failure,
  no-artifact failure, result supersession as a relation, and seal preparation;
- graph cycles, missing dependencies, hard repository scope, exact-base
  lineage, terminal-candidate uniqueness, and composition validation;
- approval binding to action, artifact, config, plan, revision, and expiry;
- salvage approval request/consume/replay binding to exact failure seal,
  scope origin, hard scope, plan digest, action payload, and preallocated task;
- deterministic readiness, concurrency, task, attempt, repair, storage,
  nested-workflow, deadline, and rate limits;
- effect-once operator dispatch under retry, concurrency, lost response,
  action-ID replay, payload substitution, and controller restart;
- result publication UUID/digest replay, preallocated result identity, durable
  acknowledgement, correction binding, task/run binding, restart from every
  publication phase, conflicting-result indeterminate handling, and refusal to
  release dependencies from claims;
- no-outcome seal preparation, scope validation before fixed intent,
  success/failure publication, final jj identity, drift detection, and terminal
  workspace refusal;
- retry from the node's original base, repair from exact success seal, salvage
  from scope baseline with failure seal read-only, and cumulative-scope checks
  that prevent failure-seal laundering;
- independent read-only review, verdict invalidation, and controller
  verification evidence and pre/post identity checks;
- per-initiative locking, atomic records, crash recovery, file modes, symlink
  rejection, event ordering, hostile payloads, JSON stdout, and stable errors;
- lazy orchestration-config failure without impact on ordinary Control v1 task
  parsing or records.

Core integration scenarios use disposable single repositories, XDG roots, and
an isolated tmux server:

1. Approve an exact static DAG and prove no task starts before approval.
2. Dispatch through the real Control API using an operator action; bind the
   immutable sidecar without coordinator fields or Control-record changes.
3. Lose a dispatch response, repeat the same action, recover the original task,
   and refuse action-ID payload substitution.
4. Inject death after result reservation, validation, result-file persistence,
   and journal completion-before-acknowledgement. At each cut, restart, replay
   the same publication UUID, recover the original result ID, and refuse changed
   bytes or a conflicting preallocated result file. Keep dependents blocked
   through process exit and seal preparation, publish the exact success seal,
   then release them.
5. Exercise completed-claim/nonzero-exit, result-missing, launch-failed,
   failed-no-artifact, and explicit failure-seal salvage paths. Put an
   out-of-scope path in the failure seal, prove salvage still starts from the
   original scope baseline, and fail any attempt to copy that path into the
   successor candidate. Consume one exact salvage approval and refuse its
   replay or substitution of seal, scope origin, plan, or action.
6. Prove hard-scope validation occurs before success intent; record advisory
   path divergence separately from scope failure.
7. Run parallel isolated nodes within the cap and compose two same-repository
   seals into one candidate; stop on composition conflict.
8. Retry from the approved baseline, repair from the exact failed candidate
   seal, and stop at attempt/repair caps.
9. Run independent review, reject reviewer mutation, route findings to a new
   seal, and invalidate older bound verdicts.
10. Run controller verification in a fresh materialization and fail on
    mutation, signal, timeout, nonzero required command, or identity drift.
11. Refuse forbidden actions even with approval-shaped input; prove pause and
    retained-storage limits delete nothing.
12. Reconcile stale Control/jj evidence for a direct-repository initiative and
    pause rather than rewriting either source of truth.
13. Refuse `session-loop`, refuse an undeclared nested workflow at plan time, and
    trip the breaker when a nested workflow spawns a second Control task, run, or
    workspace at runtime; do not assert detection of in-process fan-out.
14. Prove hostile payload containment at parser/terminal boundaries and prove a
    corrupt orchestration config cannot break ordinary Control commands.
15. Inject death at every action, result-publication, and seal phase; each
    operation must resolve to one task/result/seal or a durable `indeterminate`
    pause, never a duplicate or rewritten fact.

### Coordinator extension tests

Additional unit coverage includes coordinator record nullability, adapter
composition, live handshake, generation fencing, checkpoint compare-and-swap,
event cursors, stale-process refusal, and coordinator action idempotency.

Required integration scenarios:

1. Launch plan-only coordination through the completed Control harness, tmux,
   process, stop, and reconciliation adapters; rendered-agent presence alone
   must fail the bootstrap check.
2. Persist a higher generation, fence the old process, complete the new live
   handshake, and refuse all old-generation actions, waits, directives, and
   checkpoints.
3. Lose a coordinator action response and prove the same action resolves to the
   Core-created object without duplicate dispatch.
4. Kill the coordinator while a worker finishes, replace it, and consume only
   events after the durable cursor without changing the Core outcome.
5. Exercise each claimed directive seam and its operator-attach/new-attempt
   fallback; an unsupported seam must never use raw `tmux send-keys`.

For every coordinator-capable harness, live probes must prove launch context,
action/wait invocation, checkpoint/restart, permission and sandbox behavior,
hook failure mode, and every claimed directive seam. Unsupported behavior stays
named. Generated files and registry declarations are not runtime proof.

### Minimal TUI extension tests

Unit tests cover pure tree/detail models, selection, filtering, resizing,
approvals, action/seal/review/verification facts, storage, evidence age, and
terminal restoration. Integration tests must:

1. exit and restart the TUI while Core workers remain unchanged; when the
   coordinator extension is installed, its session must also remain unchanged;
2. render and operate on a small monochrome terminal without graphics, mouse,
   animation, or embedded terminals;
3. prove no command or confirmation path exists for any v1-forbidden action.

### Multi-repository extension tests

Unit tests cover tagged direct-repository/workspace scopes, generated identity
binding, membership/relocation drift, ordered terminal seals, aggregate spec
binding, and retained bundle evidence. Integration tests must:

1. admit multiple repositories only through a declared workspace identity and
   produce one terminal seal and fresh materialization per repository;
2. run the approved aggregate compatibility specification against the
   simultaneous materialization set and bind all pre/post identities and
   evidence into one immutable bundle;
3. prove aggregate incompatibility, member mutation, timeout, nonzero command,
   membership drift, and identity drift each fail readiness;
4. retain every member materialization and bundle record through archive.

### Stage ship gates

Every stage runs its narrow tests plus the current cumulative Control, hook, and
full regression suites. Run drift/installer checks only for targets changed by
that stage, but run all affected targets before its ship decision. The minimum
command family remains:

```bash
./tests/test-orchestration.sh
./tests/test-control.sh
./tests/test-hooks.sh
./tests/run-tests.sh
./bin/asha-drift-check.sh --target codex
./bin/asha-drift-check.sh --target opencode
```

Core manual acceptance is independent:

```text
create direct-repository initiative -> import static DAG -> approve ->
operator dispatches two isolated workers -> result/exit/seal -> compose ->
review finding -> exact-seal repair -> controller verification ->
ready-for-integration -> inspect retained tasks -> archive without deletion
```

Coordinator manual acceptance starts from a shipped Core and adds plan-only,
bounded active dispatch, response loss, crash, generation replacement, and
fallback directives. TUI manual acceptance starts from shipped Core and proves
operation and terminal restoration. Multi-repository manual acceptance starts
from shipped Core and proves declared workspace identity, aggregate
compatibility verification, failure handling, retention, and archive.

Every stage confirms that source working copies, bookmarks, foreign tmux
sessions, and unowned files do not move. Cold-review each complete stage diff;
when review fixes are committed, cold-review the fix commit separately.

## Delivery order

### Prerequisite gate: completed Asha Control

- Finish all six Asha Control increments.
- Pass its ship gates and manual acceptance.
- Review and commit the Control baseline independently.
- Amend Control with an idempotent create-by-identifier seam: `asha task start`
  accepts a caller-supplied `--task-id <uuid>` and creates the task when that
  identifier is absent, or returns the existing task unchanged when it is
  already registered, under the same per-task lock that guards every other
  Control write. This is the one Control capability orchestration's
  no-double-dispatch guarantee requires and shipped Control v1 does not provide
  (v1 mints the task id internally). It is an interface addition only; the
  strict Control v1 task record is not extended, and the amendment is reviewed,
  tested, and committed to Control before Orchestration Core Increment 2. If
  the Keeper declines this amendment, the crash-window dedup burden moves wholly
  into orchestration reconciliation and Core acceptance #3 must be re-derived.
- Update the historical Control proposal with any accepted amendments and
  identify current operating documentation.
- Re-read live controller schemas and public JSON contracts before designing
  migrations.

Exit criterion: orchestration can depend on a clean, reviewed, documented
Control release, including the idempotent create-by-identifier seam, rather than
an in-flight implementation.

### Increment 1: Orchestration Core model and read-only surfaces

- Add the separate orchestration configuration contract, schemas, store,
  locks, immutable records, repository identities, and retained-data inventory.
- Implement every state machine and plan DAG validation, including exact-base
  lineage, explicit composition, and terminal-candidate uniqueness.
- Add create, list, show, plan, approve, events, reconcile, storage, and doctor
  with versioned JSON.
- Join nodes to existing Control tasks read-only; do not dispatch.
- Add pure initiative TUI models but no interactive TUI changes yet.

Exit criterion: hostile records and single-repository graphs validate or refuse
deterministically without launching a harness, tmux session, or jj workspace.

### Increment 2: Orchestration Core execution, idempotency, and sealing

- Add generic actor action journals, expected-revision/digest fencing,
  preallocated task identities, Control link sidecars, and response-loss
  reconciliation. Coordinator generation is not present in Core actions.
- Add task/run-bound synchronous, replay-safe, crash-recoverable result
  publication and immutable events.
- Add deterministic readiness, limits, nested-workflow enforcement, and circuit
  breakers.
- Dispatch approved static plans through CLI/JSON operator actions without a
  persistent coordinator.
- Implement process-exit evidence, two-phase success/failure sealing, actual
  diff scope checks, terminal sealed workspaces, retry, repair, and salvage.

Exit criterion: a static approved single-repository DAG runs through real
Control tasks; repeated or lost requests cannot double-dispatch; only exact
success seals release dependencies; every workspace remains retained.

### Increment 3: Orchestration Core composition, review, and verification

- Add explicit composition tasks and require one terminal candidate seal.
- Add independent review tasks, exact verdict binding, read-only enforcement,
  finding repair, and older-verdict invalidation.
- Add the controller-owned fresh-materialization verification runner and its
  immutable evidence bundle.
- Prove all unsupported v1 action classes fail before approval lookup.
- Complete single-repository CLI/JSON acceptance without a coordinator or TUI.

Exit criterion: the deterministic Core alone can take a static approved graph
from isolated implementation through composition, review, controller-observed
verification, and `ready-for-integration`, with no integration or deletion
path.

### Evidence gate: does a persistent coordinator earn its cost?

Before adding the coordinator, run several real bounded initiatives through
Core and record:

- which plan choices required model judgment rather than deterministic rules;
- how often mid-initiative replanning or repair selection was useful;
- whether operator-issued CLI actions were a material burden;
- restart, evidence-volume, and state-complexity costs;
- failures that a persistent model session would have prevented or worsened.

The gate is eligible for a decision only after all Core ship tests pass and at
least three retained representative initiatives complete: one with parallel
work plus composition, one with failed review or verification plus repair, and
one with injected controller/response-loss recovery. None may contain a
duplicate task, ambiguous artifact, unbound verdict, rewritten seal, or
unexplained state transition.

Proceed only if at least two representative initiatives each contain a
recorded, non-template operator plan/replan/repair choice that materially
changed the graph, and an independent design review confirms those choices fit
the existing action schema without new authority. If that threshold is not
met, ship Core as the product and defer the persistent coordinator. The target
remains one coordinator per initiative, but it is not assumed valuable merely
because it is possible. The TUI and multi-repository stages extend Core and may
proceed through their own authorization and gates even when the coordinator is
deferred.

### Increment 4: coordinator in plan-only mode

- Add and render `control-coordinator` across supported harnesses only after
  live capability/version probes pass.
- Launch one separate coordinator session per initiative with a persisted
  monotonic generation and mandatory live handshake.
- Implement snapshot, propose-plan, checkpoint, refusal, and wait APIs.
- Integrate process/capability brokerage and stale-generation fencing.
- Require operator plan approval and keep worker dispatch operator-driven.
- Prove crash, response loss, replacement, checkpoint, and event-cursor recovery.

Exit criterion: a live coordinator can propose a valid bounded plan, receive
deterministic refusals, survive replacement, and mutate no repository.

### Increment 5: bounded active coordinator

- Permit the coordinator to request only approved graph-ready Core actions.
- Enforce action UUID/digest/revision/generation idempotency and all outer limits.
- Add bounded repair, review, verification, pause, and decision requests.
- Add optional live directives only for harness seams proven safe; otherwise use
  new-attempt or operator-attach fallback.
- Run coordinator/worker fault injection without changing Core outcomes.

Exit criterion: the coordinator can operate a Core initiative but cannot create
state or side effects that an equivalent validated CLI/JSON action could not.

### Increment 6: minimal integrated Control TUI

- Add Tasks/Initiatives mode to `asha control`.
- Implement initiative list, graph, action, seal, composition, limits, storage,
  approvals, review, verification, evidence, and event views.
- Reuse existing popup attach and task diff behavior.
- Add reconcile, approval decision, pause/resume, and graceful-stop actions.
- Prove terminal restoration and state independence from TUI lifetime.

Exit criterion: a Core initiative, with coordinator facts present only when
that extension shipped, can be operated from a small text terminal
without graphics, browser services, embedded terminals, or duplicated
lifecycle logic.

### Increment 7: multi-repository initiatives and recovery hardening

- Support multiple child repositories only through a declared Asha workspace
  with the full workspace/repository identity tuple.
- Produce one terminal seal and retained fresh verification materialization per
  repository, run the approved aggregate compatibility specification against
  the simultaneous set, then bind all identities and evidence into one
  immutable cross-repository bundle.
- Harden membership drift, relocation, orphan detection, coordinator
  replacement, stale evidence, partial outcome, storage pauses, and archival.
- Update Memory/knowledge/work-item linkage documentation without automatic
  publication.
- Run cross-harness probes, full ship gates, manual acceptance, and cold review.

Exit criterion: one bounded initiative can coordinate parallel work, failure,
composition, review, verification, restart, and operator escalation across a
declared workspace while every artifact remains retained and inspectable.

## Implementation process

This feature changes Control schemas, public CLI grammar, cross-harness agent
installation, task prompts, state transitions, lifecycle authority, and safety
boundaries. Each increment requires:

```text
codebase-historian -> explicit success criteria -> TDD -> cold reviewer
```

The historian must inspect the completed Control implementation and git
history, not this proposal's prediction of it. It must also inspect existing
`code-orchestrate`, session loop, issue loop, broker, capability registry,
workspace work-item, workspace worktree, Memory v2, installer, doctor, and
hook seams before creating a new abstraction.

Do not implement orchestration concurrently with the in-flight Control build.
Do not use this proposal as permission to commit, push, merge, publish, edit
dotfiles, remove FleetView, delete workspaces, or change external services.

## Stage acceptance criteria

### First release: Orchestration Core

Core is independently shippable and complete only when:

1. An operator can create a direct-repository initiative, import or write a
   bounded static DAG, and approve its exact digest.
2. Every operator-issued Core action is bound to generic actor, action UUID,
   plan digest, expected revision, and payload digest. Worker publication uses
   its separate task/run-bound UUID/digest journal. Core records contain no
   synthetic coordinator identity or generation.
3. Repeated, concurrent, response-lost, and crash-recovered actions create at
   most one Control task and return the same durable outcome.
4. Every mutating attempt receives its own retained Control task and jj
   workspace through an immutable sidecar without weakening Control v1 records.
5. Result-publication replay and restart from every persisted phase return one
   preallocated immutable result, a refusal, or a durable `indeterminate`
   conflict, never a duplicate. Claims never release dependencies. Success
   requires an exact `completed` claim, normal zero exit, valid identity and
   hard scope, and an immutable controller seal.
6. Generic retry, exact-seal repair, failure-seal salvage, launch failure, and
   no-artifact failure all reach defined terminal states without reusing a
   sealed or indeterminate workspace. Salvage bases the original scope baseline
   and cumulative scope checks prevent failure-seal laundering. Its exact
   seal/scope/plan/action approval is single-use and replay-safe.
7. Parallel same-repository outputs require an explicit isolated composition
   task and exactly one terminal candidate seal.
8. Independent read-only review and controller-observed verification bind to
   that exact terminal seal; repair invalidates older verdict/evidence.
9. Fixed limits, stale/conflicting evidence, and retained-storage pressure pause
   dispatch without deleting retained data or widening authority.
10. External writes, tracker mutation, integration, merge, rebase, bookmark
    movement, push, publication, workspace removal, and deletion are impossible
    even with approval-shaped input.
11. A Core initiative ends with retained evidence in `ready-for-integration`,
    `partial`, `failed`, or `cancelled`; it never integrates or deletes work.
12. Core unit, integration, cumulative regression, manual acceptance, and cold
    review gates pass.

### Coordinator extension

The coordinator extension is independently accepted only after the evidence
gate authorizes it and when:

1. One initiative-scoped coordinator uses completed Control harness, tmux,
   process, status, stop, and reconciliation adapters.
2. Runtime capability and live session handshakes pass before authority; a
   rendered agent or registry claim alone is insufficient.
3. Monotonic generation fencing rejects every stale action, wait, checkpoint,
   directive, and approval request.
4. The coordinator can request only Core-valid actions and cannot create any
   state or side effect unavailable to an equivalent operator action.
5. Response loss, crash, worker completion during absence, and coordinator
   replacement preserve exactly the Core outcome and event history.
6. Coordinator unit, harness-probe, integration, cumulative regression, manual
   acceptance, installer/drift, and cold-review gates pass.

Failure to meet the evidence gate leaves Core shipped and this extension
deferred. It does not make Core incomplete.

### Minimal TUI extension

The TUI extension is accepted when it operates a shipped Core through the same
typed controller logic, survives exit/restart without lifecycle side effects,
works in a small monochrome terminal, displays claims/seals/reviews/verification
as distinct facts, exposes no forbidden action path, and passes its cumulative
tests, manual acceptance, terminal-restoration check, and cold review. It
contains no office, avatar, browser, animation, or embedded terminal mosaic.

### Multi-repository extension

Multi-repository mode is accepted when only a declared workspace scope can bind
multiple repositories; every member has stable identity and one terminal seal;
the controller runs the approved aggregate compatibility specification against
the simultaneous retained materializations; incompatibility, mutation, timeout,
nonzero command, membership drift, or identity drift fails readiness; archive
retains the complete bundle; and all cumulative tests, manual acceptance, and
cold review pass.

## Deferred work

The following require evidence from this release and separate authorization:

- a global coordinator or cross-initiative priority scheduler;
- peer-to-peer worker mailboxes or broadcast messages;
- arbitrary dynamic workflow-language execution;
- automatic recipe compilation;
- scheduled missions, heartbeats, unattended recurring initiatives, or remote
  trigger ingestion;
- Slack, email, webhook, mobile, or voice control;
- remote hosts, multiple tmux servers, or distributed state;
- transcript mining, semantic vector memory, or a knowledge graph;
- token/cost accounting without verified harness-native evidence;
- automatic draft PR creation or tracker updates;
- automatic integration, merge, rebase, bookmark, push, or publication;
- recoverable workspace deletion and retention expiry;
- a browser, embedded IDE, terminal mosaic, office, avatar, map, animation, or
  other representational shell.

None of those are prerequisites for useful orchestration. The first release is
successful when Asha can safely coordinate real isolated work through a small,
truthful terminal surface.
