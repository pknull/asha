# Orchestration Core Increment 1

Orchestration Core stores a bounded initiative and an approved dependency graph
beside Asha Control. Increment 1 creates and approves records, then presents
read-only joins to existing Control tasks. It does not dispatch work.

## Commands

```text
asha initiative create --repo PATH --slug SLUG --label TEXT --objective TEXT
  [--acceptance TEXT]... [--max-parallel N] [--max-total-tasks N]
  [--max-attempts-per-node N] [--max-repair-cycles N] [--deadline RFC3339]
asha initiative plan ID --file PLAN.json
asha initiative plan ID --show [--revision N] [--json]
asha initiative approve ID --digest SHA256 [--json]
asha initiative reject ID --digest SHA256 --reason TEXT [--json]
asha initiative list [--json]
asha initiative show ID [--json]
asha initiative events ID [--after SEQUENCE] [--json]
asha initiative reconcile ID [--json]
asha initiative storage ID [--json]
asha initiative snapshot ID --json
asha initiative doctor [--json]
```

An omitted `--acceptance` produces one criterion exactly equal to the objective.
When `--objective` exceeds 2048 UTF-8 bytes, at least one explicit
`--acceptance` is required.
Limits may lower but never exceed the orchestration configuration ceilings.
Creation accepts only the canonical root of a jj repository with a valid
published Memory v2 `.asha/config.json` project identity. Repository preflight
uses `--ignore-working-copy` and does not create a workspace. The generated
repository UUID is stable for the bound project ID, canonical root, and Control
repository identity.

`plan` validates the closed graph through the Core rule set, stores the next
immutable `proposed` revision, and stores its nodes as `proposed`. Approval
binds the exact plan digest and current state revision to an `operator`/`cli`
approval that expires after one day. The approval is written as `approved` and
immediately advanced to `consumed`; the immutable plan remains `proposed`.
Authority is the consumed approval plus `initiative.active_plan`. Rejection
returns the initiative to `planning`, leaves the proposed revision intact, and
marks that revision's proposed nodes `superseded`. Node IDs are unique for the
life of an initiative across every plan revision. A replacement plan must use
new IDs rather than reusing the rejected revision's retained IDs.
Repeated `approve` is idempotent for the exact active plan digest. If an
interrupted approval expires before completion, supplying that exact digest
expires the stale approval, creates a fresh approval, and resumes the remaining
transitions.

`activate` is reserved in the grammar and refuses with exit 2. `asha task
report`, `result`, and `seal` likewise refuse until Increment 2. There is no
coordinator, dispatch, harness launch, tmux mutation, jj workspace creation,
Control record write, pause/resume/stop, archive, action, handshake, checkpoint,
or wait surface in this increment.

## State and concurrency

The registry root is:

```text
${XDG_STATE_HOME:-~/.local/state}/asha/control/initiatives/<initiative-uuid>/
```

`initiative.json` is the mutable optimistic-concurrency snapshot. `plans/`,
`links/`, and `events/` are immutable. `nodes/`, `attempts/`, and approvals use
digest-guarded transitions. Every CLI multi-record mutation prevalidates its
records, holds the reentrant initiative lock, supplies expected record digests,
and appends exactly one event: `initiative-created`, `plan-proposed`,
`plan-approved`, or `plan-rejected`.

Reconciliation reads links, `TaskStore.peek`, `task_digest`, and Control's pure
`reconcile_task` with a `LiveAdapters` instance using the linked task's tmux
socket. A missing task or digest mismatch is reported as `stale`; an absent
link is `unlinked`. Reconciliation never calls Control's persisted view
reconciliation and never updates either record tree.

## JSON contracts

All payloads below are closed. Adding a field requires a new contract version,
except the explicitly conditional `skipped` member on the list payload.

| Command | Exact payload |
|---|---|
| `create` | `asha.orchestration-initiative-create.v1` `{contract, initiative}` |
| `plan`, `plan --show` | the stored closed `asha.orchestration-plan.v1` record |
| `approve` | `asha.orchestration-plan-approval.v1` `{contract, initiative, plan, approval}` |
| `reject` | `asha.orchestration-plan-rejection.v1` `{contract, initiative, plan_digest, reason}` |
| `list` | `asha.orchestration-initiative-list.v1` `{contract, initiatives, skipped?}` |
| `show` | `asha.orchestration-initiative-show.v1` `{contract, initiative, graph, gates, limits, evidence_counts, node_reconciliation, superseded_nodes}` |
| `events` | `asha.orchestration-event-list.v1` `{contract, initiative_id, events}` |
| `reconcile` | `asha.orchestration-reconcile-list.v1` `{contract, initiative_id, results, superseded_nodes}` |
| `storage` | `asha.orchestration-storage-report.v1` `{contract, initiative_id, inventory, workspaces, totals, thresholds, pause_recommended}` |
| `snapshot` | `asha.orchestration-snapshot.v1` `{contract, initiative, active_plan, nodes, superseded_nodes, attempts, last_event_sequence, state_revision}` |
| `doctor` | `asha.orchestration-doctor.v1` `{contract, ok, probes, limitations}` |

Each reconciliation result is the closed
`asha.orchestration-node-reconciliation.v1` object `{contract, node_id,
attempt_id, control_task_id, control_state, control_lifecycle, digest_match,
evidence}`. An unlinked result uses null attempt/task/lifecycle/digest fields
and an empty evidence list.

A storage workspace item is `{attempt_id, control_task_id, path,
workspace_name, exists, jj_workspace_registered, bytes, inodes, detail}`.
Inventory contains one `{bytes, inodes}` member per record class plus `totals`
and its own threshold-derived `pause_recommended` value.
Threshold comparison includes both sidecar storage and retained linked Control
workspace usage; filesystem traversal never follows symlinks.

The pure TUI model renders current nodes and their attempts in the main tree.
Retained rejected-revision nodes are available separately through
`superseded_rows()` and never appear as live work.

Exit status is 0 for success, 2 for usage or a deterministic refusal, 1 when a
doctor payload has `ok:false` or an internal error escapes the refusal classes,
and 130 for interruption. Human output is not a contract.
