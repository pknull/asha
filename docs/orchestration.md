# Orchestration Core Increment 2b

Orchestration Core stores one bounded initiative and approved dependency graph
beside Asha Control. Increment 2b adds worker result publication, exit-bound
immutable seals, autonomous retry, repair, salvage, and paused-work
continuation to the effect-once dispatch machinery. Control remains the only
owner of jj workspace and tmux task creation.

## Commands

```text
asha initiative create --repo PATH --slug SLUG --label TEXT --objective TEXT
  [--acceptance TEXT]... [--max-parallel N] [--max-total-tasks N]
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
asha initiative list [--json]
asha initiative show ID [--json]
asha initiative events ID [--after SEQUENCE] [--json]
asha initiative reconcile ID [--json]
asha initiative storage ID [--json]
asha initiative snapshot ID --json
asha initiative doctor [--json]
asha task report --file PATH [--json]
asha task result CONTROL_TASK_ID [--json]
asha task seal CONTROL_TASK_ID|ATTEMPT_ID [--json]
```

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

## Assignment and dispatch

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
evidence when dispatching approved salvage work. It instructs the worker to
run `jj status` in the workspace before `asha task report` and after every
later edit. The controller never snapshots the worker workspace.

The scheduler then invokes one argv-only bounded subprocess, without a shell:

```text
<asha_root>/bin/asha task start
  --repo <root>
  --task-id <preallocated-control-task-uuid>
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

## Worker result publication

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
`ASHA_CONTROL_TASK_ID` and `ASHA_CONTROL_RUN_ID` bindings from Control. Its
input must be a bounded regular non-symlink file. Every listed path is
relative, canonical, non-symlink, and inside the linked task workspace.
`files_changed` entries must name files, never directories; an existing
directory claim is refused during publication so the worker can correct and
republish it before exit.

Publication is an initiative-locked durable journal:
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
receipt without semantic revalidation. `asha task result` returns accepted
immutable results for one Control task.

## Exit evidence and immutable seals

A terminal claim does not prove success. Reconciliation moves `reported` to
`awaiting-exit`, reads Control's process evidence, then persists a
no-outcome `asha.orchestration-seal-preparation.v1` record and a
`seal-preparing` event before collecting jj evidence. Orchestration uses
Control's read-only `JjAdapter` with `--ignore-working-copy`; it does not
snapshot or mutate the workspace.

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

Orchestration configuration adds:

```json
{
  "orchestration": {
    "result_grace_seconds": 120,
    "max_consecutive_failures": 3
  }
}
```

Both values must be positive integers.

## Storage and JSON contracts

The registry root is:

```text
${XDG_STATE_HOME:-~/.local/state}/asha/control/initiatives/<initiative-uuid>/
```

`initiative.json` is the mutable optimistic-concurrency snapshot. Plans,
assignments, links, and events are immutable. Nodes, attempts, approvals, and
actions use digest-guarded transitions. Multi-record mutations hold the
reentrant initiative lock.

All payloads below are closed. Adding a field requires a new contract version,
except the explicitly conditional `skipped` member on the list payload.

| Command | Exact payload |
|---|---|
| `create` | `asha.orchestration-initiative-create.v1` `{contract, initiative}` |
| `plan`, `plan --show` | stored `asha.orchestration-plan.v1` record |
| `approve` | `asha.orchestration-plan-approval.v1` `{contract, initiative, plan, approval}` |
| `reject` | `asha.orchestration-plan-rejection.v1` `{contract, initiative, plan_digest, reason}` |
| `activate`, `dispatch`, `pause`, `resume`, `stop`, `cancel`, `action` | stored `asha.orchestration-action.v1` journal record |
| `approve-salvage` | `asha.orchestration-salvage-approval.v1` `{contract, initiative_id, approval}` |
| `task report` | `asha.orchestration-result-publication-receipt.v1` `{contract, publication_id, result_id, phase, refusal}` |
| `task result` | `asha.orchestration-task-results.v1` `{contract, task_id, results}` |
| `task seal` | `asha.orchestration-task-seal.v1` `{contract, seal}` |
| `list` | `asha.orchestration-initiative-list.v1` `{contract, initiatives, skipped?}` |
| `show` | `asha.orchestration-initiative-show.v1` `{contract, initiative, graph, action_outcomes, gates, limits, evidence_counts, node_reconciliation, superseded_nodes}` |
| `events` | `asha.orchestration-event-list.v1` `{contract, initiative_id, events}` |
| `reconcile` | `asha.orchestration-reconcile-list.v1` `{contract, initiative_id, action_reconciliation, live_reconciliation, results, superseded_nodes}` |
| `storage` | `asha.orchestration-storage-report.v1` `{contract, initiative_id, inventory, workspaces, totals, thresholds, pause_recommended}` |
| `snapshot` | `asha.orchestration-snapshot.v1` `{contract, initiative, active_plan, nodes, superseded_nodes, attempts, links, actions, last_event_sequence, state_revision}` |
| `doctor` | `asha.orchestration-doctor.v1` `{contract, ok, probes, limitations}` |

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

Increment 3 adds composition, independent review, and verification. Core 2b
does not compose candidates, create review or verification records, or run a
coordinator. Repair only stales retained review and verification records that
were created by a later increment.

Exit status is 0 for success, 2 for usage or deterministic refusal, 3 for an
indeterminate action outcome, 1 when a doctor payload has `ok:false` or an
internal error escapes the refusal classes, and 130 for interruption. Human
output is not a contract.
