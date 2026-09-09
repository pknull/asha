# Donmai runtime study for Asha

Status: source study, not an adoption proposal or implementation approval.

Donmai revision: `6250f7aa71f41a0a6222d000d23664a2a782bfec`.
Asha revision: `c8460393b0c6082146a22c4e6a7e43c6f2fc40d4`.
The working managed-session proposal is additional, uncommitted documentation.
Reference code and selected test bodies were read; Donmai was not installed,
executed, benchmarked, or tested in Asha's environment. Its comments describe
past incidents, but this study does not independently confirm those incidents.

## Findings that change the comparison

Donmai has useful implemented boundaries between session hosting, provider
operations, delivery, and controller authority. Those boundaries are appropriate
references for Asha. Its delivery guarantees vary by execution mode, however:

- Interactive Claude notices are acknowledged after transcript evidence of
  consumption. Headless/interview injects can be acknowledged while buffered in
  memory. These are different acknowledgement contracts.
- Interactive PTY events can be coarse while headless provider events are rich.
  Replacing tmux with a PTY does not supply structured agent state by itself.
- The inspected Codex approval bridge answers from policy; it is not a durable
  human question/resume service. Its prompt default declines when no UI is wired.
- The Codex event emitter can silently drop events when its channel is full.
- A terminal-result outbox distinguishes transport delivery from application of
  the result. This is a useful distinction for Asha's existing evidence pipeline.

These findings narrow the earlier conversational description of Donmai as a more
complete runtime. That judgment concerns specific interfaces and ownership paths,
not a demonstrated end-to-end reliability advantage on our task.

## 1. Session ownership and controller restart

Donmai's interactive session shim owns the harness process group, PTY, output
sequence, replay window, and terminal observation. The daemon connects as a
controller. In `sessionshim/shim.go`, `handshake` accepts only an advancing
controller generation, replaces the previous connection, and closes the old
controller's socket. `readControl` checks generation on mutations. The wire
protocol distinguishes hello, adoption, output, input, stop, heartbeat, gap,
snapshot, and exit.

Output progress has its own acknowledgement: with protocol v3 or later,
`persistHeartbeatAck` rejects regressed or impossible cursors and persists a live
cursor before returning its receipt.
`Registry.publish` uses temporary publication, file sync, rename, and directory
sync. This is acknowledgement of retained output, not acknowledgement that an
agent understood a message. Terminal tombstones preserve a final observation
for later reconciliation.

Sources: [shim ownership and handshake](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/sessionshim/shim.go#L845),
[wire vocabulary](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/shimwire/message.go),
[durable cursor](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/sessionshim/ack.go).

**Apply to Asha:** retain the existing supervisor. Introduce a small session owner
and a stable session ID distinct from process identity and controller generation.
Map these to `orchestration/coordinator.py` and the anchor schema in
`orchestration/model.py`. Add a versioned managed anchor; preserve legacy pane
anchors. A new controller must fence the prior owner connection before mutation.
Reconcile existing owners before offering their capacity for another task.

**Limit:** the inspected shim is specifically a PTY owner. It does not prove that
all Donmai headless runs survive daemon restart. Asha must test ownership of its
structured harness connection directly. Keep the owner independent of the UI;
defer a full terminal emulator, binary attach protocol, and scrollback replay.

## 2. Provider operations and idle conversations

`agent.Handle` exposes native session identity, events, injection, and stop;
capabilities describe which operations a provider actually supports. The Codex
provider starts an app-server subprocess and creates/resumes threads. Its current
`Handle.Inject` returns unsupported; follow-ups can use provider resume. Claude's
shared CLI adapter launches a process with stdin/stdout/stderr pipes, parses
JSONL, and implements injection through a subsequent `--resume` invocation.
That is a new process continuing a conversation, not typing into the original UI.

The interview loop streams a turn, waits on an input channel, starts the follow-up,
and returns to waiting. Inputs are serialized by that loop. This is the useful
shape for an idle coordinator: an ordinary runtime waits while the model is idle.
The inspected inject call can itself block until its subprocess exits, so Asha
should specify its own asynchronous submission/event contract instead of assuming
the method name implies nonblocking behavior.

Sources: [session handle](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/agent/handle.go),
[CLI resume implementation](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/provider/harness/clijsonl/handle.go#L438),
[interview loop](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/runner/interview_loop.go#L232),
[Claude mode capabilities](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/provider/harness/claude/manifest.go).

**Apply to Asha:** extend `harness.py` and `launch.py` with adapters rather than
replace headless launch. Return a turn handle promptly and consume events
concurrently. Route idle coordinator work from the supervisor's SQLite queue.
Record capabilities by harness version and run mode: follow-up after completion,
mid-turn steering, native approval response, and reconnect are separate abilities.
Preserve Asha's persona, tools, sandbox, and project configuration on every resume;
verify them rather than assume a saved native session restores all launch options.

## 3. Message delivery and acknowledgements

Interactive delivery follows this path:

1. The runner accepts a delivery ID without acknowledging consumption.
2. It checks the declared notice channel. Unsupported channels produce an explicit
   dead-letter disposition instead of simulated agent keystrokes.
3. For Claude's pull channel, `Offer` publishes one hook response. The hook claims
   it by rename; only one invocation can claim that offer.
4. `Consumed` reads the transcript path supplied by the hook. It requires a
   synthetic user message containing the notice, rather than a hook-output record,
   and advances a scan cursor to avoid crediting the same record again.
5. Only then does the interactive runner acknowledge delivery upstream.

The queue distinguishes refusal attempts from polls waiting for consumption:
30 refusal attempts versus 450 consumption polls, on a two-second retry clock.
Asha should borrow separate counters, not those particular durations. Waiting
for an agent to finish real work must not spend a failed-delivery budget.

Sources: [notice queue](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/runner/interactive_inject.go#L444),
[transcript consumption check](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/provider/harness/claude/stophook.go#L240).

There are material limits. The Stop hook cannot collect a message while an
already-idle session has no turn ending. Transcript content matching is
harness-specific evidence, not proof of comprehension or action. At its poll cap,
the runner dead-letters even when withdrawal did not succeed; that source path
alone cannot rule out late consumption. Asha should record uncertainty when it
cannot prove withdrawal, rather than invite automatic duplicate execution.

**Correction to the earlier comparison:** `runner/loop.go` passes
`ackOnBuffer=true` for noninteractive injects. A successful send to an in-memory
channel can therefore trigger acknowledgement before the follow-up turn consumes
it. This creates a crash window in that path; whole-system recovery was not
established by this study. [Acceptor implementation](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/runner/loop.go#L1468).

**Apply to Asha:** extend `orchestration/messages.py` without changing the meaning
of its existing explicit receive/ack receipts. Store custody, submission,
consumption evidence, and requested-action resolution separately. A committed
SQLite row can acknowledge durable custody; an in-memory queue cannot acknowledge
consumption. Preserve message ID, digest, recipient generation, and turn linkage.
No timer, successful write, or normal process exit upgrades the evidence level.

## 4. Events, questions, and approvals

Donmai's `handleServerRequest` correlates a Codex response with the native server
request ID. It evaluates tool approvals through a policy bridge and emits events
describing the decision. The bridge defaults to allowing calls not denied by its
rules when no explicit policy is provided; a configured prompt default declines
without a wired UI. The same handler cancels MCP elicitation in autonomous mode
and rejects unhandled request methods. This inspected path does not implement the
Keeper-facing question queue we need.

Its event emitter uses a nonblocking channel send with an empty default branch.
Events can be dropped under consumer pressure. That is distinct from the more
careful output acknowledgement in the PTY shim; do not transfer the shim's
guarantees to this event path.

Sources: [approval handling and event emitter](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/provider/harness/codex/handle.go#L375),
[approval policy](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/provider/harness/codex/approval.go#L124).

**Apply to Asha:** keep the native request ID and scope digest in an outstanding
request row. Link to Asha's existing approval/decision actions. Persist an answer
before attempting the native reply; reconcile an uncertain reply without granting
an unrelated request. A plan approval never automatically approves every tool.
Do not adopt Donmai's policy defaults as a way to reduce prompts.

Persist state-changing events before advancing their receipt cursor. Stream text
may be batched, but completion, pending requests, decisions, delivery receipts,
and failure evidence cannot disappear when a display falls behind. A full or
unwritable durable store must expose degraded operation and stop new dispatch.
Keep the provider reader and command/reply path responsive; implement a bounded
spool or explicit overflow failure rather than block a shared RPC loop indefinitely.

## 5. Completion, retry, and resource release

The runner observes provider events, reads an agent-written turn-result manifest,
and can fall back to `WORK_RESULT` markers. It classifies deliberately blocked
work separately and may invoke steering or a commit/PR backstop according to work
type. These are workflow policies, not equivalents of Asha's exact-seal review.
Manifest validation does not independently establish that the work is correct.

The terminal-workarea protocol is a useful stronger example: its outbox stores
an immutable result body and digest, receiver identity, attempts, deadline, and
next attempt time. Delivery and application have separate states: a receiver can
accept transport while the result is not authoritative or is rejected.

Sources: [result resolution](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/runner/loop.go#L1253),
[terminal outbox](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/runtime/workarea/terminal_protocol.go#L608),
[budget reporting](https://github.com/RenseiAI/donmai/blob/6250f7aa71f41a0a6222d000d23664a2a782bfec/runner/budget.go).

**Apply to Asha:** preserve `results.py`, `ingestion.py`, `seals.py`, `review.py`,
and `verification.py` semantics behind the SQLite store. Keep process exit, result
publication, result acceptance, review verdict, verified candidate, and integration
distinct. Retrying a result publication must not rerun the model. Retrying a review
must remain bound to its seal and budget. Quota wait, delivery retry, and repair
are different recovery actions with different owners and costs.

## Concrete changes to Asha's plan

| Decision | Implementation seam | Acceptance proof |
| --- | --- | --- |
| Keep Asha; use Donmai as a reference | Native adapter modules under Control; no Donmai dependency | Asha's existing workflows and authority actions remain callable. |
| SQLite holds operational state and durable custody | Control store interfaces and supervisor | Crash after custody receipt preserves pending delivery. |
| One independently owned harness connection per managed session | Managed anchor, session owner, generation checks | Restart controller, fence old input, retain live turn. |
| Capabilities depend on provider and execution mode | `harness.py`, adapter probes, doctor | Unsupported steering never becomes terminal keystrokes. |
| Submission and event reading run concurrently | Adapter turn handle and owner event loop | Sustained output cannot deadlock submission or lose terminal state. |
| Waiting does not consume refusal/repair budget | Delivery queue and existing attempt accounting | Long valid turn receives its follow-up without a new initiative. |
| Evidence determines acknowledgement strength | `messages.py`, delivery and receipt tables | Buffer/write/timeout alone never marks consumption. |
| Native requests and Asha decisions share explicit correlation | Existing actions plus outstanding request projection | Duplicate answer resumes once; stale answer grants nothing. |
| Result transport remains separate from accepted work | Existing result, seal, review, and verification paths | Replayed publication does not execute work again or bypass review. |
| Direct PTY hosting stays an optional later adapter | Rooms/session interface | Terminal replacement does not require another scheduler or database. |

## Tests to borrow as scenarios, not copied implementations

Read tests include Donmai's `TestInteractive_PullChannelAcksOnlyOnConsumption`,
`TestInteractive_PullChannelWaitsPastTheRefusalCap`,
`TestInteractive_UndrivenChannelRefusesWithoutWritingOrAcking`, and durable-cursor
tests for stale, regressed, and ahead-of-output acknowledgements. These test names
are a source inventory, not a claim that the suites passed here.

For Asha add: crash after durable custody but before native submission; crash
after submission before receipt; duplicate identical text with distinct delivery
IDs; late consumption after failed withdrawal; event consumer overload; native
approval response lost after persistence; controller restart while awaiting a
human; and replay of an accepted result against the same immutable seal.

Use fake providers and clocks for these deterministic tests. The bounded real
Claude smoke test then verifies actual stream, resume, role context, and approval
capabilities. This study provides no reason to start a fleet or add initiatives.

The implementation sequence remains in the
[managed-session plan](2026-09-07--managed-agent-sessions.md).
