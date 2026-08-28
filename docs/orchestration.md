# Orchestration Core: Increments 1-7

Orchestration Core stores one bounded initiative and approved dependency graph
beside Asha Control. Increment 3 adds ordered composition, independent
exact-seal review, controller-owned verification, compatible candidate bundles,
terminal readiness, finalization, and retained archive. Increment 4 adds the
coordinator claim: the operator's own Asha session, running inside a tmux pane,
claims one fenced coordinator generation per initiative and proposes plans;
approval stays an operator act from another terminal. Increment 5 opens the
bounded active set to that coordinator (dispatch, repair, salvage request,
stop, pause, continuation, decision request, outcome proposal, pending
directive), adds CAS checkpoints, and lets Control-launched workers run without
the persona. Increment 6 is the Initiatives mode of `asha control` (see
`docs/control.md`). Increment 7 lets one initiative span a declared workspace:
one terminal candidate, review, and verification materialization per member
repository, one aggregate bundle. Control remains the only owner of worker jj
workspace and tmux task creation; its run-less materialization seam owns fresh
controller verification workspaces.

## Commands

```text
asha initiative baseline --repo PATH [--revision REVSET] [--json]
asha initiative create --repo PATH | --workspace PATH --slug SLUG --label TEXT
  --objective TEXT [--acceptance TEXT]... [--max-parallel N] [--max-total-tasks N]
  [--max-attempts-per-node N] [--max-repair-cycles N] [--deadline RFC3339]
asha initiative plan ID --file PLAN.json
asha initiative plan ID --show [--revision N] [--json]
asha initiative approve ID --digest SHA256 [--json]
asha initiative approve-salvage ID --request REQUEST_ID [--json]
asha initiative reject ID --digest SHA256 --reason TEXT [--json]
asha initiative activate ID [--json]
asha initiative action ID --file ACTION.json --json
asha initiative dispatch ID --node NODE [--salvage-request REQUEST_ID] [--json]
asha initiative pause ID [--json]
asha initiative resume ID [--json]
asha initiative stop ID --attempt ATTEMPT [--json]
asha initiative cancel ID --node NODE [--json]
asha initiative finalize ID --outcome partial|failed --reason TEXT [--json]
asha initiative archive ID [--json]
asha initiative unarchive ID [--json]
asha initiative list [--all] [--json]
asha initiative show ID [--json]
asha initiative events ID [--after SEQUENCE] [--json]
asha initiative reconcile ID [--json]
asha initiative storage ID [--json]
asha initiative snapshot ID --json
asha initiative doctor [--json]
asha initiative projects [--root DIR]... [--depth N] [--match TEXT] [--json]
asha task report --file PATH [--json]
asha task ingest CONTROL_TASK_ID|INGESTION_ID [--json]
asha task result CONTROL_TASK_ID [--json]
asha task seal CONTROL_TASK_ID|ATTEMPT_ID [--json]
asha control supervisor {run|start|stop|status} [--json]
asha initiative coordinator claim ID [--harness H] [--json]     (from the Asha pane)
asha initiative coordinator release|show ID [--json]
asha initiative coordinator launch [--root DIR] --intent TEXT [--harness H] [--json]
asha initiative coordinator sessions [--json]
asha initiative coordinator attach ID | --session NAME [--json]
asha initiative propose-plan ID --file PLAN.json [--json]       (coordinator actor)
asha initiative wait ID --after SEQUENCE --timeout SECONDS --json
asha initiative checkpoint ID --file CHECKPOINT.json [--json]   (coordinator actor)
asha initiative dispatch|pause|stop ID ... --as-coordinator     (coordinator actor)
asha initiative authority add NAME --repo PATH --scope PREFIX [...]
asha initiative authority list [--all] [--json]
asha initiative authority revoke ID [--json]
```

## Plan authoring and baseline identity

Use `asha initiative baseline --repo PATH [--revision REVSET]` to obtain the
exact immutable scope origin for an `approved-baseline` node. The revision
defaults through Control's shared exact-Git omitted-base resolver: current
attached local branch, then same-OID remote symbolic `*/HEAD` targets, then
same-OID conventional local `main`/`master`/`trunk` refs. The command preflights
the canonical repository, applies Control task start's colocated Git HEAD/jj
`@-` synchronization guard, confirms the selected OID is visible in jj, and
computes its immutable tree. An explicit `--revision` remains a verbatim jj
revset.
It never imports Git refs or otherwise mutates the repository. If Git knows a
bookmark that jj cannot yet see, the refusal points to `jj status` rather than
importing it. Exact Git reads disable promisor-object lazy fetching, so a
repository-configured partial-clone remote cannot turn baseline inspection into
network access. Human output is the commit and tree digest on two labelled lines;
`--json` returns the closed `asha.orchestration-baseline.v1` contract.

When `asha initiative plan ID --file PLAN.json` validates a proposed plan, it
checks every mutating `work` or `compose` node with base policy
`approved-baseline` before writing the plan, nodes, initiative state, or event.
The node's `scope_origin.jj_commit_id` must already be visible in the initiative
repository, and that commit's immutable tree digest must equal
`scope_origin.tree_digest`. An invisible commit or digest disagreement names
the node and prints the exact
`asha initiative baseline --repo <root> --revision <commit>` command needed to
regenerate the authoring values. This check is read-only; it does not import
refs.

`activate` performs the runtime handshake before changing `approved` to
`running`: Orchestration doctor and Control doctor must pass, Control's
create-by-ID parser seam must be present, the repository must still preflight,
and its Memory project and repository identity digest must still match the
initiative. Activation then derives and persists initial node readiness.

`dispatch`, `pause`, `resume`, `stop`, and `cancel` create operator actions with
fresh UUIDs. An autonomously reserved retry supplies its already allocated
action UUID to the next `dispatch` request. `stop` asks Control to stop the one
linked task gracefully. `cancel` stops a linked live attempt, if any, then marks
the attempt and node cancelled. `resume` first requires a clean live
reconciliation.

`reconcile` is the only read-shaped command that mutates orchestration state.
It reconciles indeterminate actions, persists live Control evidence, applies
attempt and node transitions, allocates eligible retries, applies breakers,
then returns the ordinary read-only node join.

`list` omits archived initiatives by default. `list --all` includes them.

## Standing authorities

A standing authority is the operator's pre-signed approval for a narrow,
recorded plan shape. It is the deliberate autonomy lever: matched proposals are
approved (and, when granted with auto-activate, activated) without a live
operator in the loop, and everything outside the recorded shape falls back to
ordinary manual approval.

`asha initiative authority add NAME --repo PATH --scope PREFIX` grants one
immutable `asha.orchestration-standing-authority.v1` record under
`<state>/asha/control/authorities/` — outside the initiatives registry, so
initiative scans never touch it. The record pins the repository (`repository_id`
and `initial_identity_digest`), scope prefixes (clean relative paths only),
`max_nodes`, allowed harnesses, an attempt ceiling, `require_headless`, and
`auto_activate`. Records are write-once (`O_EXCL`, mode 0600); `authority
revoke ID` stamps `revoked_at` under a lock and retains the record. `authority
list` shows active grants; `--all` includes revoked ones.

Matching is deterministic and fail-closed. At the end of every accepted plan
proposal the controller consults active authorities for the initiative's
repository. A plan matches only when every check passes: the authority is not
revoked, the initiative scope is `repository` with the recorded
`repository_id` and an unchanged `initial_identity_digest`, the plan has at
most `max_nodes` nodes of types work/review/verify with no nested workflow,
every work node declares a hard write scope inside the authorized prefixes,
work and review harnesses are in the allowed set, `require_headless` (when
set) demands `interactive: false` on work and review nodes, the declared gates
include review and exactly one minimal-environment verification with real
commands, and the plan's attempt ceiling does not exceed the authority's. Any
mismatch leaves the initiative at `awaiting-plan-approval` for the operator,
with each authority's mismatch reason on stderr so a grant that never fires is
diagnosable. A damaged or unreadable authority store is reported the same way
and approves nothing.

A matched plan is approved as the operator by proxy: the stored approval
record keeps `actor_kind: operator` with `actor_id`
`standing-authority:<id8>`, because the decision is the operator's own,
pre-signed when the authority was granted. The controller journals one
`approval-decided` event citing the authority ID and label so the record shows
which grant decided. With `auto_activate` the controller then submits an
ordinary `activate-initiative` action under the same proxy actor; activation
refusals (doctor failures, identity drift) leave the approval standing and the
initiative at `approved`.

Authorities never cover integration, salvage approval, operator decisions, or
needs-input responses — those remain live operator acts. `authority add` and
`authority revoke` are refused from the coordinator's anchor pane and from any
session carrying `ASHA_ORCHESTRATION_COORDINATOR_ID`, with the session policy
guard's `coordinator-no-authority-grant` rule as the same belt the approval
verbs carry (and the same limitation: it sees only variables present in the
harness process environment at launch). A coordinator must never mint, widen,
or revoke its own approval surface. `authority list` is a read and stays
available everywhere, including inside a coordinator: knowing which shapes are
pre-approved is by design, since a matching plan is one the operator already
signed.

Combined with time triggers, this is the bounded autonomous loop: a timer
proposes, a standing authority approves and activates, gates and breakers
still bind execution, and integration still waits for the operator.

## Project roots and friendly names

`asha initiative projects` indexes one or more roots. Which roots it uses is
resolved explicit-first, per-invocation before persistent:

```text
--root DIR (repeatable)  ->  ASHA_PROJECTS_ROOT  ->  ~/.asha/config.json
                                                     project_roots  ->  cwd
```

`~/.asha/config.json` is the user's cross-project configuration — the same file
`bin/asha` already reads `default_harness` from on every bare launch — so the
directories someone keeps work in belong there:

```json
{"project_roots": ["~/Code", "~/Projects", "~/Obsidian"]}
```

At most eight roots are read, and each is discovered under its own
512-directory bound, so that bound is per root rather than global. A root that
cannot be indexed is reported in `skipped` instead of failing the listing: one
missing directory must not hide the projects in the others. A project reachable
from two roots is listed once.

A project may state a friendly name in its own `.asha/config.json`:

```json
{"initialized": true, "memory_version": 2, "project_id": "...", "name": "Ashes and Starlight"}
```

The index shows that name and keeps the directory in `directory`, so an
operator reads words they chose while the path stays available. The name is
bounded at 48 characters, whitespace-collapsed, and must be printable;
anything else falls back to the directory name rather than reaching a terminal
row. `--match` remains exact and case-insensitive against the friendly name,
the directory, or the project id — the index resolves an intent to exactly one
repository, so substring matching would resolve ambiguously.

Plain output groups by root and leads with the jj-colocated projects, because
only those can run an initiative and an operator should not pick one and then
be refused.

## Action document and journal

`action --file` accepts this closed operator document:

```json
{
  "contract": "asha.orchestration-action.v1",
  "action_id": "UUID",
  "initiative_id": "UUID",
  "actor_kind": "operator",
  "actor_id": "bounded-text",
  "action_class": "dispatch-node",
  "payload": {"node_id": "implementation-a"},
  "payload_digest": "SHA256(canonical-payload-json)",
  "active_plan_digest": "SHA256",
  "expected_state_revision": 12
}
```

Core 2b action classes and exact payload shapes are:

| Action class | Payload |
|---|---|
| `activate-initiative` | `{}` |
| `dispatch-node` | `{"node_id":"NODE"}` or `{"node_id":"NODE","salvage_request_id":"UUID"}` |
| `pause` | `{}` |
| `resume` | `{}` |
| `stop-attempt` | `{"attempt_id":"UUID"}` |
| `cancel-node` | `{"node_id":"NODE"}` |
| `repair-node` | `{"node_id":"NODE","seal_id":"SUCCESS-SEAL-UUID"}` |
| `request-salvage` | `{"node_id":"NODE","failure_seal_id":"FAILURE-SEAL-UUID","plan":"BOUNDED TEXT"}` |
| `decide` | `{"paused_seal_id":"PAUSED-SEAL-UUID","decision":"BOUNDED TEXT"}` |
| `continue-node` | `{"node_id":"NODE","paused_seal_id":"PAUSED-SEAL-UUID","decision_action_id":"UUID"}` |
| `finalize` | `{"outcome":"partial|failed","reason":"BOUNDED TEXT"}` |
| `archive` | `{}` |
| `unarchive` | `{}` |
| `request-decision` (coordinator) | `{"subject_id":"BOUNDED TEXT","question":"BOUNDED TEXT"}` |
| `propose-outcome` (coordinator) | `{"outcome":"partial|failed","reason":"BOUNDED TEXT"}` |
| `directive` (coordinator) | `{"node_id":"NODE","attempt_id":"UUID","text":"BOUNDED TEXT"}` |

Coordinator-actor documents add `coordinator_id` and `coordinator_generation`
(Increment 5); the classes marked coordinator are request classes the
coordinator submits and the operator answers through `resume`, `finalize`, or
attach.

The journal record keeps the same authority envelope without `payload`, adds
`state`, `outcome`, `received_at`, and `updated_at`, and stores the canonical
payload inside the bounded JSON-encoded `outcome` string. Its phases are
`received -> validated -> dispatching -> completed | refused`, with
`dispatching -> indeterminate`; only reconciliation may advance an
indeterminate action to `completed` or `refused`.

The action UUID binds its exact payload bytes, actor envelope, plan,
and expected revision. Replaying the same binding returns the stored phase and
outcome without repeating a side effect. Reusing the UUID with changed payload,
plan, actor, or expected revision is refused. Every configured forbidden action
class is refused before any approval lookup.

## Coordinator claims (Increment 4)

The operator side has a chair of its own: every wrapped no-argument `asha
[HARNESS]` launch seats at `$ASHA_HOME/chair` and carries the orchestrator
stance by default — the harness word selects only the tool (see INSTALLER.md,
Persona model) — the session drives this surface as the Keeper's instrument through
the `operate-control` skill, launching one fenced coordinator per piece of
work and signing operator writes only on the Keeper's word. The stance never
reaches coordinator sessions or workers, so the split below stays exact.

Codex coordinators are launch-equivalent to Claude ones (verified live
2026-08-25: prompt delivery, skill adherence, create, claim with the pane
proofs, and propose-plan all hold): `coordinator launch --harness codex`
adds the per-launch trust override and the unattended workspace-write
posture described in docs/control.md, since an unattended session cannot
answer Codex's own approval prompts.

The controller never launches a coordinator. The operator's Asha session runs
`asha initiative coordinator claim ID` from inside a tmux pane. The claim
records one `asha.orchestration-coordinator.v1` generation under
`coordinators/<coordinator-id>.json`: the calling pane's id, pid, process start
identity, session, and server pid form the anchor; the caller must descend from
the pane process. A replay from the same pane and process is idempotent. A
claim from a different pane persists generation N+1, fences a live or stale
predecessor (`coordinator-generation-fenced`), and leaves an exited or failed
predecessor untouched. The claim appends `coordinator-handshake-accepted` with
the coordinator actor and returns `ASHA_ORCHESTRATION_INITIATIVE_ID`,
`ASHA_ORCHESTRATION_COORDINATOR_ID`, and
`ASHA_ORCHESTRATION_COORDINATOR_GENERATION` for the session to export. Those
variables select records; the document plus the live anchor authorize.

Every coordinator-actor verb (`wait`, `propose-plan`, `coordinator release`,
and coordinator action documents through `action --file`) re-proves: the record
is the current generation in a live state, the caller's `TMUX_PANE` is the
anchor pane, the pane still holds the same process identity, and the caller
descends from it. `reconcile` marks a live generation `stale` when its anchor
pane or process is gone (`reconciliation-conflict` event); stale keeps running
workers and dispatches nothing until a new claim fences it.

`wait --after SEQUENCE --timeout SECONDS` polls `list_events_snapshot` without
a lock and writes no events; when events arrive it advances this generation's
durable `event_cursor` once. The timeout is capped by
`coordinator_wait_seconds`. `propose-plan` runs the same validation as `plan
--file` and records `plan-proposed` under the coordinator actor.

Coordinator-actor action documents carry `coordinator_id` and
`coordinator_generation`; `submit_action` journals them and refuses a fenced or
unknown generation. Increment 5 opens exactly `dispatch-node`, `repair-node`,
`request-salvage` (request only), `stop-attempt`, `pause`, `continue-node`,
`request-decision`, `propose-outcome`, and `directive` to the coordinator
actor; every other class stays operator-only and is journaled then refused.
The operator convenience verbs `dispatch`, `pause`, and `stop` accept
`--as-coordinator` from the anchored pane; the request classes go through
`action --file`. The coordinator's `expected_state_revision` may be behind the
current revision (the loop wakes on event sequences, not revisions); a revision
ahead of the current one is refused, and the records each class binds (plan
digest, node, attempt, and seal identities) are re-checked under the lock by
the executor. Operators keep exact revision matching. `request-decision` moves a
running initiative to `needs-input` with the question on an
`approval-requested` event; `resume` (operator) returns it to running.
`propose-outcome` records an `approval-requested` outcome proposal and changes
no state; only the operator's `finalize` ends an initiative. `directive`
records a bounded directive for a live, unsealed attempt as
`directive-accepted` with `delivery: pending`; no harness seam is proven safe
for mid-run delivery, so the controller never types into a pane, and the
fallbacks are operator attach, a new attempt carrying the directive, or
`needs-input`. `checkpoint --file` replaces this generation's
`asha.orchestration-coordinator-checkpoint.v1` under CAS
(`prior_checkpoint_digest` must equal the retained digest) and appends
`coordinator-checkpointed`; a checkpoint is a hint for re-claims, never recovery
authority. Control-task links created by a coordinator carry its generation.
Every operator write (`approve`, `reject`, `approve-salvage`, `plan --file`,
the convenience verbs without `--as-coordinator`, and operator-actor
`action --file` documents including `decide`) refuses the coordinator's anchor
pane and any session carrying `ASHA_ORCHESTRATION_COORDINATOR_ID`, so the
coordinator pane can act only as the coordinator actor and the journal never
attributes a coordinator-pane act to the operator; the Keeper approves from his
own terminal. The session plugin's policy guard additionally denies the
approval verbs when `ASHA_ORCHESTRATION_COORDINATOR_ID` is present in the
harness process environment at launch (`require_env`); an `export` inside a
session does not reach hook processes, so that rule is a belt only for
sessions launched with the variable set. The controller check is the braces.

The anchor is pane id + pane pid + process start identity on one tmux server.
A pane shell that `exec`s another program keeps its claim by design; pid reuse
is caught by the start identity; session renames and pane moves are cosmetic.
A caller on a different tmux server cannot judge the anchor: `show` reports
`anchor_live: null`, `reconcile` leaves the generation untouched, and
coordinator verbs refuse with "caller tmux server differs from the anchor
server". Only a pane that is gone or whose process identity changed, observed
from the anchor's own server, marks the generation `stale`.

Honest boundary: Control has no UID-level boundary. Fencing binds
coordinator-actor documents, waits, and claims; it is not containment against a
deliberate local process.

## Assignment and dispatch

A node may declare `"interactive": false` (work and review nodes on a harness
with a headless mode: Claude, Codex). Control then launches the worker
headless in its pane (`claude -p … --permission-mode bypassPermissions`,
`codex exec …`): the session runs one full turn and exits, so the seal follows
mechanically and the published-awaiting-exit state cannot occur. Permissions
are bypassed deliberately — the workspace is isolated and the seal enforces
hard scope and read-only review; a headless run cannot answer a prompt.
Interactive assignments now state that the worker cannot end its own session
and must ask the operator to close it (the `X` key in `asha control`).


Dispatch preallocates the attempt UUID and Control task UUID and stores both in
the action outcome before calling Control. It writes this immutable file first:

```text
${initiative_dir}/assignments/<attempt-id>.md
```

The file is UTF-8, mode `0600`, and at most 32 KiB. It contains the initiative,
node, and attempt identities; objective and node goal; repository and exact
base; hard write scope and advisory ownership; dependencies; bounded
upstream-result summaries; acceptance criteria; verification guidance;
role and workflow; nested-workflow policy; prohibited actions; and this exact
result-publication command:

```text
asha task report --file .asha/result.json
```

The result document lives in the workspace's private `.asha/` directory,
which every Control-acceptable repository ignores, so it never enters the
sealed diff; a result file written to a tracked path is a hard-scope
violation and fails the seal. The command is available only inside a
Control-managed worker environment.
The assignment also carries exact seal inputs and read-only failure-seal
evidence when dispatching approved salvage work. It explicitly tells workers
not to run `jj status`: a workspace-write sandbox can change the working tree
but cannot write the colocated source repository's Git object store. After a
normal exit, the controller snapshots the retained tree and independently
reruns declared result attestations against that exact commit.

The scheduler then invokes one argv-only bounded subprocess, without a shell:

```text
<asha_root>/bin/asha task start
  --repo <root>
  --task-id <preallocated-control-task-uuid>
  --result-ingestion-id <preallocated-ingestion-uuid>
  --result-outbox .asha/outbox/<ingestion-uuid>.json
  --base <exact-immutable-commit>
  --harness <node-harness>
  --role <node-role>
  --detach --json
  --goal "orch <slug-prefix> <attempt-id> <absolute-assignment-path>"
```

An approved-baseline or scope-baseline node uses its approved scope-origin
commit. An upstream-seal node requires one exact successful upstream seal
commit. Generic retries reuse the node's original base.

The scheduler accepts only the closed `asha.control-task-start.v1` response.
`existing:true` is the idempotent replay of the preallocated task ID. After the
response it writes one immutable `asha.orchestration-link.v1` sidecar binding
the initiative, active plan, node, attempt, action, actor, expected initiative
revision, Control task ID, immutable Control identity digest, and full Control
task record digest as evidence. A lost response or
crash before the link is proven leaves the action and attempt indeterminate.
Action reconciliation reissues the identical task ID, accepts Control's
existing task, or records `launch-failed` when neither a task nor creation
journal exists. It never allocates a replacement task for that action.

## Worker result staging and controller publication

A worker stages its result and then **ends its session**. The seal is
recorded only when the controller observes a normal exit: an interactive
harness that returns to its prompt leaves the attempt at `reported` and the
node `running` indefinitely, and stopping such an attempt seals a failure
because a killed process is not a normal exit. The assignment states this
requirement; `asha task attach` plus a normal exit is the recovery when a
worker has published but stayed open.


`asha task report` accepts one closed `asha.orchestration-result.v1` client
object. The client omits controller-owned `result_id` and `payload_digest`:

```json
{
  "contract": "asha.orchestration-result.v1",
  "publication_id": "UUID",
  "supersedes_result_id": null,
  "initiative_id": "UUID",
  "node_id": "implementation-a",
  "attempt_id": "UUID",
  "task_id": "UUID",
  "run_id": "UUID",
  "claim_status": "completed",
  "summary": "Bounded summary",
  "files_changed": ["relative/canonical/path"],
  "verification_attestations": [],
  "concerns": [],
  "follow_up": [],
  "published_at": "RFC3339 timestamp"
}
```

The command requires `ASHA_CONTROL_MANAGED=1` plus exact
`ASHA_CONTROL_TASK_ID`, `ASHA_CONTROL_RUN_ID`,
`ASHA_CONTROL_RESULT_INGESTION_ID`, and `ASHA_CONTROL_RESULT_OUTBOX` bindings
from Control. Its input must be a bounded regular non-symlink file. It proves
the caller descends from the exact managed pane whose private options bind the
task, run, ingestion UUID, and outbox-path digest, then writes only a closed
`asha.orchestration-result-candidate.v1` below the workspace's private
`.asha/outbox/`. It opens neither authoritative task nor initiative state, so
read-only Control mounts do not prevent staging. A coordinator with forged
managed environment variables is in another pane and is refused. The worker's
receipt phase is `staged`, not `completed`.

The controller supervisor (or explicit `asha task ingest`) waits for terminal
producer evidence. It verifies the unique reservation, active-plan digest,
attempt/node/task/run binding, immutable Control identity, workspace
name/path/change ID, closed result schema, hard-scope paths, publication
lineage, and exact candidate bytes. Every listed path is relative, canonical,
non-symlink, and inside the linked task workspace; `files_changed` entries
must name files, never directories. Stale, foreign, modified, duplicate, and
unreserved candidates fail closed with a precise refusal. Exact completed
replay returns the same result ID. Review results use the same path without a
commit.

Authoritative publication is an initiative-locked durable journal:
`reserved -> validating -> persisting -> completed`. Reservation binds the
client publication UUID, canonical body digest, task, run, attempt,
preallocated result UUID, receipt sequence, and attempt revision. Persisting
writes `results/<result-id>.json` once. Completion changes the active attempt
from `running` to `reported` and appends `result-published`. A restart resumes
any nonterminal journal from its retained body. Conflicting bytes at the
preallocated result path make the publication indeterminate and pause the
attempt.

Replaying the same publication UUID with identical canonical bytes returns the
same result UUID and phase. Different bytes are a replay conflict. A
superseding publication may name only the current accepted result of the same
unsealed attempt. Once a completed publication has its immutable result and
`result-published` event, later supersession does not make the older journal
incomplete: reconciliation skips it and an identical replay returns its stored
receipt without semantic revalidation. Controller-carried results additionally
bind `publication_provenance` (producer and controller/coordinator ingester),
`claimed_commit_id`, and `commit_provenance` (`worker|controller|none`). For a
controller-created commit, exact-materialization verification evidence is
mandatory and is copied into the later seal provenance. `asha task result`
returns accepted immutable results for one Control task.

## Exit evidence and immutable seals

A terminal claim does not prove success. Reconciliation moves `reported` to
`awaiting-exit`, reads Control's process evidence, then persists a
no-outcome `asha.orchestration-seal-preparation.v1` record and a
`seal-preparing` event before collecting jj evidence. Orchestration uses
Control's `JjAdapter`. A staged mutable attempt has already been snapshotted by
the ingestion controller after terminal producer evidence. Its command-scoped
auto-track fileset excludes `.asha/`, keeping the private candidate transport
out of the commit without changing repository ignore policy; later seal
inspection uses `--ignore-working-copy` and requires the result's claimed
commit and verification provenance to match exactly.

The tree digest is SHA-256 over compact canonical JSON containing entries
sorted by path. Each entry is `[path,mode,blob-id]` from one read-only
`git -C <git-root> ls-tree -rz --full-tree <jj-commit-id>` call. Git blob IDs
cover regular-file content and symlink targets (mode `120000`). Directories are
absent. Conflicted jj commits, Git submodules (mode `160000`), unsafe paths,
duplicates, more than 200000 entries, and ls-tree output beyond 64 MiB are
refused.

The immutable `asha.orchestration-seal.v1` binds the attempt, Control task and
primary run, repository, scope origin, exact base seal or baseline, read-only
failure-seal inputs, final jj commit and tree digest, attempt and cumulative
diff digests, changed paths, accepted result, process evidence, outcome, and
timestamp. Outcomes are:

* `success`: an accepted `completed` claim, normal zero exit, clean jj/task
  identity, a cumulative diff inside hard scope, and every worker-claimed
  `files_changed` path present in the sealed attempt diff.
* `paused`: an accepted `blocked` or `needs-decision` claim, normal exit,
  clean identity, and valid cumulative hard scope.
* `failure`: a failed claim, abnormal exit, missing result after grace, hard
  scope violation, or any unmet success condition.

Attempt-delta divergence from advisory ownership is retained as evidence and
does not change the outcome. Hard scope always uses the cumulative diff from
the original scope origin, including after salvage. A claimed but absent path
is retained as `claimed-but-unsealed` evidence, forces failure, and is
retriable while the node attempt cap permits. The immutable seal-evidence
record caps each path list under its 128 KiB canonical payload bound. Each
retained prefix has an omitted-count `<field>_truncated` marker and a SHA-256
`<field>_digest` over the complete canonical list, so path count cannot make a
seal permanently unpublishable. A seal retains at most 512 `changed_paths`
and 512 `cumulative_changed_paths`. Both lists have matching `_truncated` and
`_digest` fields that bind their complete canonical lists;
`cumulative_diff_digest` separately binds the full cumulative tree diff.
`seal-published` event path lists retain the first 32 entries and a
`truncated: N more` marker. The event payload is validated before the
write-once seal is saved so an oversized diagnostic cannot half-publish it.
Later commit drift appends `seal-drift-detected` and pauses affected work
without rewriting the seal. `asha task seal` accepts either a Control task UUID
or attempt UUID.

## Retry, repair, salvage, and continuation

A repair attempt's assignment carries the accepted review findings bound to
its exact upstream seal in an "Accepted review findings to fix" section — the
findings are the goal of the attempt, so the correction reaches the worker
through the assignment record, never through direct pane input. A running
attempt can additionally receive a journaled `directive` (recorded before any
relay; delivery stays pending by design).


Autonomous retries consume the configured attempt budget and resolve the
node's original approved base, never the failed attempt's inherited base.
Salvage and repair lineages are not autonomously retried; a lineage failure
moves through `evaluating` to `needs-input`. `repair-node` instead requires the
exact prior success seal and reserves an attempt from its commit. The source
candidate remains succeeded, so existing dependents stay released, until the
repair attempt publishes a success seal. Only then is the source superseded
and its bound review or verification records marked `stale`. Every retained
repair-lineage attempt counts against `max_repair_cycles`, including legacy
autonomous retries with a null `action_id`.
Review gate reruns caused by `candidate-review-staled` do not consume the gate
node's ordinary `max_attempts_per_node`; they consume the initiative's
`max_repair_cycles`. A plain dispatch of a node with a retained success seal is
refused with the exact seal ID. The operator must use `repair-node` so repair
cycle accounting and stale-evidence invalidation cannot be bypassed.

`request-salvage` creates a requested approval bound to the failure seal,
scope origin, hard scope, active plan digest, salvage plan, and action payload
digest. `approve-salvage` moves that exact request to approved. Dispatch with
`--salvage-request REQUEST_ID` consumes it atomically with reservation, starts
from the original scope baseline, and supplies the failure seal read-only.
Replays are effect-once; request substitution, plan drift, expired or consumed
authority, and cumulative scope laundering are refused. Unrelated events after
approval do not invalidate salvage authority: dispatch binds the approval's
immutable revision and binding digest rather than event adjacency.

A paused seal leaves the node in `needs-input`. A bounded `decide` action binds
the operator decision to that seal. One `continue-node` action may consume the
completed decision and paused seal, reserve a fresh attempt based on the paused
commit, and return the node to `ready`. `stop-attempt` and `cancel-node` mark
the affected attempt `cancelled` while preserving its workspace.

## Readiness, live tracking, and breakers

Readiness is deterministic from dependency states plus the initiative and plan
limits. A node is not ready unless the initiative is `running`, dependencies
are ready, parallel and total task budgets permit it, its attempt budget
permits it, the deadline has not passed, and retained storage does not recommend
a pause. An already allocated autonomous retry retains its reservation at the
total and per-node cap.

Live reconciliation peeks the immutable link and current Control task, verifies
the recorded task digest, and calls Control's pure live reconciler. Live
`starting`, `working`, `needs-input`, `idle`, and `unknown` states retain a
running attempt and append `task-status-observed` evidence. Normal process exit
without a result becomes `result-missing` after `result_grace_seconds`.
One narrow live-worker exception covers jj's mid-snapshot metadata split: when
Control reports `stale` solely because the created-workspace registration and
working-copy identities briefly disagree, the owned process still matches, and
no non-jj identity mismatches, orchestration treats that observation as
`working` for that poll. The downgrade does not alter Control's verdict. Once
the process exits, or any harder mismatch appears, the ordinary stale/conflict
path applies.
Nonzero or signal exit becomes `abnormal-exit`. Missing tasks, task digest
mismatch, and stale live identity make the attempt and node `stale` and pause
the initiative with `reconciliation-conflict`. Seal classification prefers
Control's structured process evidence state and retains dead exit status or
signal when present. Exact legacy terminal detail is parsed only as a
defensive fallback; unknown or contradictory text fails closed instead of
being treated as a normal exit.

`reported` attempts move to `awaiting-exit`. A normal or abnormal terminal
Control observation then drives seal preparation. `result-missing` after the
grace period also enters the seal path. A failure seal may allocate an
autonomous retry from the node's original base when the failure is retriable
and all caps permit it. Retriable failures include a completed result with
`claimed-but-unsealed` paths, commonly caused by omitting the required final
`jj status`; otherwise the node fails.

Dispatch pauses and emits a limit event at the deadline or total-task cap, and
emits `storage-threshold-reached` when retained storage recommends a pause.
`max_consecutive_failures` trailing retriable failures pause the initiative.
Reconciliation also pauses when more than one Control task label or marker
names the same attempt, which is the nested-workflow breaker.

The routine supervisor runs that existing progression on a clock. Each tick
re-lists active-plan initiatives, ingests terminal producers' staged results,
then calls the shared single-initiative reconciliation entry point. It retains
no cursor or timer records: CAS writes, stable event keys, and the existing
journals make a repeated tick or process restart a no-op after convergence.
One initiative's exception is retained in that tick's report without stopping
the remaining sweep. The supervisor never dispatches, submits operator
actions, records integration, or approves plans or salvage.

`asha control supervisor run` holds the foreground loop. `start` launches that
same run route detached with argv-only exec, `stop` verifies the retained Linux
boot/start-ticks process identity before SIGTERM, and `status` exits zero only
when both the flock and exact process are live. The exclusive 0600 lock and
atomic presentation status are `supervisor.lock` and `supervisor.json` beneath
the Control state root. A one-second directory-mtime poll of Control event
snapshots provides the worker-exit fast path between regular ticks. Starting is
idempotent; no session hook starts the supervisor automatically.

Orchestration configuration adds:

```json
{
  "orchestration": {
    "supervisor_interval_seconds": 15,
    "link_grace_seconds": 30,
    "result_grace_seconds": 120,
    "max_consecutive_failures": 3
  }
}
```

All four values must be positive integers. `supervisor_interval_seconds`
bounds routine full sweeps; `link_grace_seconds` bounds the
launch-to-link publication wait; result publication polls for the immutable
attempt link every 250 milliseconds before returning a retryable refusal.

## Workspace scope (Increment 7)

`create --repo PATH` binds a repository scope; `create --workspace PATH` binds
a declared workspace: the `.asha/workspace.json` manifest the session plugin
already validates, resolved through Control's existing context seam. Exactly
one of the two flags is accepted. New initiatives persist
`asha.orchestration-initiative.v2` with
`scope: {kind: repository, repository} | {kind: workspace, workspace}`; v1
records stay readable and executable unchanged. The workspace object is
`{workspace_id, project_id, root, manifest_membership_digest, repositories}`
where `repositories` is the ordered list of member repository-scope objects
(`repository_id`, `project_id`, `root`, `control_repository_id`,
`initial_identity_digest`) and the membership digest covers the ordered
manifest paths and member identities.

Workspace plans must list exactly the scope members, declare exactly one
terminal candidate producer per member, one required review gate per member
(each depending on that member's terminal candidate), and one verification
gate depending on every review gate. Dispatch passes each node's member root
as `--repo`; assignments name the member. Activation re-verifies every
member's identity and the manifest membership; a renamed, relocated, added, or
removed member is a typed refusal, never a silent rebinding. Dispatch runs in
the member root recorded at creation and inherits Control's own preflight.
Verification materializes one fresh explicit-base workspace per member
(`verify-<initiative-id>-<verification8>[-<index>]`) at that member's exact
terminal seal, records one `verification-member` evidence per member, and runs
the approved commands in every member; the candidate bundle digest binds the
ordered member seal set. Readiness requires, per member, one accepted-pass
review and the one passed verification binding that member, and writes one
ordered multi-member bundle. `storage` merges jj registrations across every
member root, lists every member materialization (primary record path plus
`verification-member` paths), and labels each workspace and materialization
with its `repository_id`. Archive retains every member materialization and the
bundle.

## Storage and JSON contracts

The registry root is:

```text
${ASHA_HOME:-~/.asha}/state/control/initiatives/<initiative-uuid>/
```

`initiative.json` is the mutable optimistic-concurrency snapshot. Plans,
assignments, links, and events are immutable. Nodes, attempts, approvals, and
actions use digest-guarded transitions. Multi-record mutations hold the
reentrant initiative lock.

All payloads below are closed. Adding a field requires a new contract version,
except the explicitly conditional `skipped` member on the list payload.

One retained Increment 1 form of `asha.orchestration-plan.v1` predates
controller verification command authority: every verification gate has exactly
`{kind,node_id,required}`. A digest-valid record with that exact historical
shape remains stored byte-for-byte and readable through `plan --show`, `show`, and
`snapshot`; no compatibility marker is added to their closed payloads. It is
observation-only. Approval, activation, dispatch, resume, repair, continuation,
and verification refuse it because commands and `environment_policy` cannot be
inferred. New plan validation and persistence remain on the current closed gate
schema below. Existing terminal cancellation, finalization, and archive paths
remain available where their ordinary lifecycle rules permit containment.

| Command | Exact payload |
|---|---|
| `baseline` | `asha.orchestration-baseline.v1` `{contract, repository: {root, control_repository_id}, jj_commit_id, tree_digest, entry_count}` |
| `create` | `asha.orchestration-initiative-create.v1` `{contract, initiative}` (`initiative` is a stored `asha.orchestration-initiative.v2` record with `scope.kind` `repository` or `workspace`) |
| `plan`, `plan --show` | stored `asha.orchestration-plan.v1` record |
| `approve` | `asha.orchestration-plan-approval.v1` `{contract, initiative, plan, approval}` |
| `reject` | `asha.orchestration-plan-rejection.v1` `{contract, initiative, plan_digest, reason}` |
| `activate`, `dispatch`, `pause`, `resume`, `stop`, `cancel`, `finalize`, `archive`, `unarchive`, `action` | stored `asha.orchestration-action.v1` journal record |
| `approve-salvage` | `asha.orchestration-salvage-approval.v1` `{contract, initiative_id, approval}` |
| `task report` | `asha.orchestration-result-publication-receipt.v1` `{contract, publication_id, result_id, phase, refusal}` |
| `task result` | `asha.orchestration-task-results.v1` `{contract, task_id, results}` |
| `task seal` | `asha.orchestration-task-seal.v1` `{contract, seal}` |
| `list` | `asha.orchestration-initiative-list.v1` `{contract, initiatives, skipped?}` |
| `show` | `asha.orchestration-initiative-show.v1` `{contract, initiative, graph, action_outcomes, gates, limits, evidence_counts, node_reconciliation, superseded_nodes}` |
| `events` | `asha.orchestration-event-list.v1` `{contract, initiative_id, events}` |
| `reconcile` | `asha.orchestration-reconcile-list.v1` `{contract, initiative_id, action_reconciliation, live_reconciliation, coordinator_reconciliation, results, superseded_nodes}` |
| `storage` | `asha.orchestration-storage-report.v1` `{contract, initiative_id, inventory, workspaces, materializations, totals, thresholds, pause_recommended}`; `workspaces[]` and `materializations[]` entries carry `repository_id` (additive label under v1, following the `coordinator_reconciliation`/`coordinator` precedent) |
| `snapshot` | `asha.orchestration-snapshot.v1` `{contract, initiative, active_plan, nodes, superseded_nodes, attempts, links, actions, coordinator, last_event_sequence, state_revision}` |
| `doctor` | `asha.orchestration-doctor.v1` `{contract, ok, probes, limitations}` |
| `projects` | `asha.orchestration-project-list.v1` `{contract, root, roots_from, source, match, groups, projects, skipped}`; each project is `{name, directory, root, project_id, role, declared, asha_project, jj_colocated}` — `source` is `manifest` (declared workspace at or above the root) or `discovery` (jj-colocated Asha projects at and below it, depth 1-3, 512-directory bound per root). `directory` is an additive label under v1, following the `repository_id` precedent, because `name` may be the project's own. `root` is null when several roots were indexed; `groups` keeps each root's projects, `projects` is their flat union, and `skipped` names roots that could not be indexed. Read-only |
| `coordinator claim` | `asha.orchestration-coordinator-claim.v1` `{contract, initiative_id, coordinator, environment}` |
| `coordinator release` | `asha.orchestration-coordinator-release.v1` `{contract, initiative_id, coordinator}` |
| `coordinator launch` | `asha.orchestration-coordinator-launch.v1` `{contract, session, pane_id, root, harness, intent, launched_at}` |
| `coordinator sessions` | `asha.orchestration-coordinator-sessions.v1` `{contract, sessions: [{session, initiative_id, slug, coordinator_id, generation, state}]}` |
| `coordinator attach` | `asha.orchestration-coordinator-attach.v1` `{contract, initiative_id, session, pane_id, coordinator_id, generation}` |
| `coordinator show` | `asha.orchestration-coordinator-show.v1` `{contract, initiative_id, coordinator, anchor_live, anchor_detail, generations}` |
| `propose-plan` | stored `asha.orchestration-plan.v1` record (event actor `coordinator`) |
| `wait` | `asha.orchestration-event-wait.v1` `{contract, initiative_id, coordinator_id, generation, after, events, last_event_sequence, state_revision, timed_out}` |
| `checkpoint` | stored `asha.orchestration-coordinator-checkpoint.v1` `{contract, initiative_id, coordinator_id, generation, plan_revision, event_cursor, nodes_under_consideration, pending_decision, rationale, prior_checkpoint_digest, recorded_at, digest}` |
| `dispatch\|pause\|stop --as-coordinator` | stored `asha.orchestration-action.v1` journal record with `actor_kind: coordinator`, `coordinator_id`, `coordinator_generation` |
| `authority add` | `asha.orchestration-authority-grant.v1` `{contract, authority}` (`authority` is a stored `asha.orchestration-standing-authority.v1` record) |
| `authority list` | `asha.orchestration-authority-list.v1` `{contract, authorities, active, total}` |
| `authority revoke` | `asha.orchestration-authority-revocation.v1` `{contract, authority}` (revocation stamps `revoked_at`; the record is retained) |

The closed `asha.orchestration-seal.v1` path representation includes
`changed_paths`, `changed_paths_truncated`, and `changed_paths_digest`.
`changed_paths_digest` binds the complete canonical path list even when the
stored list is capped. `cumulative_changed_paths` has the same 512-item cap,
`cumulative_changed_paths_truncated` omitted count, and
`cumulative_changed_paths_digest` complete-list digest. The separate
`cumulative_diff_digest` binds path and before/after tree identities rather
than only the cumulative path list.

`show.graph` is `{plan, nodes, attempts, links}`. Action reconciliation is
`{contract, initiative_id, actions}`. Live reconciliation is
`{contract, initiative_id, observations, conflicts, retries, probes}`.

## Composition, review, and repair

A `compose` attempt is an ordinary Control task created at the repository
scope-origin commit. Its assignment lists an ordered set of exact success
seals, their immutable commits and trees, and `conflict_policy`
(`fail-on-conflict` or `worker-resolves`). The worker may combine those inputs
only inside its own workspace and must publish one candidate. Its seal records
`base.kind: composition-inputs`, the same ordered seal IDs, and a cumulative
diff from the shared scope origin. Conflicts and scope failures follow the
ordinary failure, pause, retry-cap, and needs-input paths.

A `review` attempt is an ordinary read-only Control task based on the terminal
candidate seal commit. Its assignment and result bind the active plan and
specification digests, repository and seal IDs, exact commit, base-seal IDs,
and diff digest. The result must claim `completed`, list no changed files, and
carry `review.verdict` (`pass` or `findings`), bounded findings with
`severity`, `location`, and `summary`, plus the exact target object. Acceptance
also requires a normal zero exit and a filesystem capture against the target
tree; the controller never snapshots. Every expected tracked entry must retain
its bytes, mode, and type. Extra untracked or ignored paths are bounded evidence
and do not fail the review. A rejected review retains immutable failure evidence
and reopens for a fresh attempt while its ordinary attempt cap permits. Findings
retain a repair requirement against the exact target node and seal. A later
successful repair seal marks reviews and verifications bound to its predecessor
`stale`; the new seal must pass both gates again.

## Controller verification specification

A required verification gate in the approved plan has this closed form:

```json
{
  "kind": "verification",
  "node_id": "verification-a",
  "required": true,
  "environment_policy": "minimal",
  "commands": [
    {"argv": ["python3", "-c", "print('verified')"], "cwd": ".", "timeout_seconds": 60}
  ]
}
```

The controller creates a fresh run-less Control materialization at the exact
terminal seal (one per scope member) and executes only those argv arrays. `minimal` supplies exactly
`PATH`, `HOME`, and `LANG`. There is no worker, harness, tmux session, shell, or
source checkout mutation. On Linux, the runner requires a trusted, executable,
non-group/other-writable `bwrap` at `/usr/bin/bwrap` or `/bin/bwrap`; it refuses
before materialization if PID-namespace lifetime containment is unavailable.
An orchestration-owned supervisor inside that namespace records the exact child
PID, `/proc` start time, PID namespace, and signed return status. When the
supervisor exits, the namespace kills residual descendants before post-run jj
identity is inspected.

The runner refuses before execution: `git push`,
`git commit`, `git tag`, `jj git push`, `gh`, `curl`, `wget`, `ssh`, `scp`,
`rsync`, `pip install` (including `python -m pip install`), `uv pip install`,
`npm publish` or `npm install`, `twine`, `cargo publish`, `gem push`,
`poetry publish`, `docker`, `sudo`, and recursive forms of `rm`. It also refuses
environment, shell, and multicall/process wrappers (`env`, common shells,
`busybox`, `timeout`, `nice`, `nohup`, `setsid`, and `xargs`) rather than claim
visibility through a wrapper. This conservative argv policy is not hostile-code
or general filesystem containment; approved program internals remain outside
the deny classifier.

Each command retains immutable evidence binding the verification and candidate
bundle digests; repository, seal, and materialization identities; approved
argv and relative cwd; environment policy and process identity; start and
finish times; exit, signal, and timeout status; bounded output path and digest;
and pre/post jj commit and tree identities. Denial, timeout, signal, nonzero
exit, identity mismatch, or materialization mutation fails verification. A
failed identity probe is recorded as `indeterminate` with null identity fields,
never as an inferred commit or tree. Post-run identity requires unchanged jj
commit, parents, and tree plus unchanged bytes, modes, and types for every
tracked target entry. Extra ignored or untracked paths are retained as bounded
evidence and do not fail verification. Raw output streams into a pre-reserved
`0600` retained artifact while the command runs. The artifact is capped at
1 MiB and receives a `truncated` marker when the original stream exceeds that
bound, including on timeout. Its retained bytes match the command digest. The evidence and
materialization remain retained.

## Bundle, readiness, finalization, and archive

One accepted passing review per member and one passed controller
verification must name the current terminal candidate seal of every scope
member. The controller then binds one compatible bundle with one ordered
member per repository and advances only `running -> ready-for-integration`.
Core has no integrate, merge, push, or deletion verb. After the operator lands
that exact bundle externally, `record-integration --bundle` records the durable
attestation and advances `ready-for-integration -> integrated`. The same fact
recorded in another state does not move lifecycle, and `--seal --abandoned`
never advances it.
When every graph node is terminal without a qualifying candidate, the operator
may acknowledge `partial` or `failed` with `finalize --reason`; partial requires
at least one retained success seal as useful work. Failure-only evidence must
be finalized with outcome `failed`; a `partial` request is refused.

`archive` changes a terminal outcome to `archived` and records a retained
inventory without deleting records, evidence, tasks, or workspaces.
`unarchive` restores the terminal outcome recorded by the latest archive event.
Reclaiming worker and review workspaces is Control's job: once their attempts
are terminal, archive the Control tasks and run `asha task prune` (see
`docs/control.md`); prune refuses any workspace still bound to a non-terminal
attempt, and seals, repair, salvage, and verification never read a pruned
workspace because they work from the exact seal commit and fresh
materializations. The seal-drift reconciler treats an archived task whose
workspace directory is gone as reclaimed, not as drift.

## Increment 3 state-transition amendments

The node transition graph includes `succeeded -> ready` when accepted review
findings make the exact candidate repairable. The initiative transition graph
includes `ready-for-integration -> integrated` and
`archived -> ready-for-integration | integrated | partial | failed | cancelled`
when integration is recorded or `unarchive` restores the terminal outcome
retained by the latest archive cycle. These are explicit lifecycle edges, not
same-state record rewrites.

Exit status is 0 for success, 2 for usage or deterministic refusal, 3 for an
indeterminate action outcome, 1 when a doctor payload has `ok:false` or an
internal error escapes the refusal classes, and 130 for interruption. Human
output is not a contract.
