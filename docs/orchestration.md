# Orchestration Core Increment 2a

Orchestration Core stores one bounded initiative and approved dependency graph
beside Asha Control. Increment 2a adds effect-once operator actions, assignment
files, Control task dispatch, live attempt tracking, autonomous retry, and
circuit breakers. Control remains the only owner of jj workspace and tmux task
creation.

## Commands

```text
asha initiative create --repo PATH --slug SLUG --label TEXT --objective TEXT
  [--acceptance TEXT]... [--max-parallel N] [--max-total-tasks N]
  [--max-attempts-per-node N] [--max-repair-cycles N] [--deadline RFC3339]
asha initiative plan ID --file PLAN.json
asha initiative plan ID --show [--revision N] [--json]
asha initiative approve ID --digest SHA256 [--json]
asha initiative reject ID --digest SHA256 --reason TEXT [--json]
asha initiative activate ID [--json]
asha initiative action ID --file ACTION.json --json
asha initiative dispatch ID --node NODE [--json]
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

Core 2a action classes and exact payload shapes are:

| Action class | Payload |
|---|---|
| `activate-initiative` | `{}` |
| `dispatch-node` | `{"node_id":"NODE"}` |
| `pause` | `{}` |
| `resume` | `{}` |
| `stop-attempt` | `{"attempt_id":"UUID"}` |
| `cancel-node` | `{"node_id":"NODE"}` |

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
base; hard write scope and advisory ownership; dependencies; empty bounded
upstream-result summaries for 2a; acceptance criteria; verification guidance;
role and workflow; nested-workflow policy; prohibited actions; and this exact
result-publication command:

```text
asha task report --file RESULT.json
```

That command is part of the worker contract but remains reserved until
Increment 2b.

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
the initiative with `reconciliation-conflict`.

`result-missing`, `abnormal-exit`, and proven `launch-failed` attempts move the
node through `evaluating`. When the original-base retry remains within node and
initiative budgets, reconciliation creates one new allocated attempt and moves
the node to `ready`; otherwise it fails the node. No seal is created in 2a.

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
| `list` | `asha.orchestration-initiative-list.v1` `{contract, initiatives, skipped?}` |
| `show` | `asha.orchestration-initiative-show.v1` `{contract, initiative, graph, action_outcomes, gates, limits, evidence_counts, node_reconciliation, superseded_nodes}` |
| `events` | `asha.orchestration-event-list.v1` `{contract, initiative_id, events}` |
| `reconcile` | `asha.orchestration-reconcile-list.v1` `{contract, initiative_id, action_reconciliation, live_reconciliation, results, superseded_nodes}` |
| `storage` | `asha.orchestration-storage-report.v1` `{contract, initiative_id, inventory, workspaces, totals, thresholds, pause_recommended}` |
| `snapshot` | `asha.orchestration-snapshot.v1` `{contract, initiative, active_plan, nodes, superseded_nodes, attempts, links, actions, last_event_sequence, state_revision}` |
| `doctor` | `asha.orchestration-doctor.v1` `{contract, ok, probes, limitations}` |

`show.graph` is `{plan, nodes, attempts, links}`. Action reconciliation is
`{contract, initiative_id, actions}`. Live reconciliation is
`{contract, initiative_id, observations, conflicts, retries, probes}`.

Increment 2b adds task-scoped result publication, immutable results and seals,
normal-exit/result ordering, review, and verification. Core 2a does not publish
results, create seals, review work, verify candidates, or run a coordinator.

Exit status is 0 for success, 2 for usage or deterministic refusal, 3 for an
indeterminate action outcome, 1 when a doctor payload has `ok:false` or an
internal error escapes the refusal classes, and 130 for interruption. Human
output is not a contract.
