# Managed sessions

This guide covers the structured backend and how its session owners start. The default [session dashboard](session-hub.md) launches plain
workers, Rooms and structured utilities. Structured utilities inherit native
permissions, sandbox settings and subagents. Utility owners exit between turns.
The initiative coordinators this backend once also served were retired with
the initiative engine on 2026-10-05 ([Control](control.md)).

## Starting work

`asha control session launch --project PROJECT --prompt TEXT --transport
structured [--harness claude|codex] --json` starts a structured utility through
the [session hub](session-hub.md); `session create` is the lower-level form
below. In Control, Enter on a structured session shows state and event pages
without changing delivery acknowledgements.

Use `asha control session admission status --json` to inspect runtime
admission. Creating a session preserves paused or stopped admission rather than
silently resuming it.

## Current work

`asha control session current --json` lists current sessions without reading
historical output. Use `--kind requests` for pending questions and native
permissions, or `--kind deliveries` for queued or uncertain messages awaiting
dispatch or reconciliation. Submitted and consumed receipts survive in `show`
and `events`; current session rows also expose their active turn and its receipt.
A finished turn's receipt is history, even when consumption was never proved.
These are separate pages, so a session page full of idle conversations
cannot hide a question. Stopped sessions remain available through `list` and
`show`; they are excluded from the current session page.

Each page accepts `--limit` (1–1000) and its returned `next_cursor` as `--after`.
Stop paging when `complete` is true; `next_cursor` alone is not a termination
signal. Selection uses the existing state indexes and prioritizes questions and
recovery before idle sessions. Cursors describe a position in mutable
current state, not a persistent snapshot: changes between pages can add or remove
rows or move them across the cursor. Refresh from the start for a new observation.
Reads neither acknowledge messages nor authorize execution. A `next_action` names
an inspection or existing validated command; it never bypasses its checks.

Rows carry session identity, project directory, generation,
responsible actor (`waiting_on`), reason and next action. `age_seconds` measures
time since the row was created, not time since the last output or proof of a
stall. `reason` is fixed explanatory text; `recovery_reason` is a bounded retained
provider/transport diagnostic. Recovery rows retain the condition and any retry time; successful
recovery still requires an explicit operator decision. Recorded running states
do not independently prove that a process is alive.

`session summary`, Control's managed-session header and the chair's activity
observation use this shared projection. The session count excludes stopped sessions.
If a page is capped, counts are explicitly lower bounds. Control's question picker
reads the same pending-request page, then reloads the exact request and digest
before accepting an answer. Startup text uses fixed labels and counts, so retained
questions and message text never become startup instructions. Shared visible rows
are allocated across sources, and stale counts survive row truncation. Optional,
uninitialized managed sources do not make legacy observations incomplete. Managed
reads cooperate with the observation deadline between indexed queries; blocking
filesystem or database calls retain their existing timeout behavior.

SQLite Room observations filter lifecycle through the state index before
applying the cap; open Rooms precede stuck creations. Unknown state projections
report unavailable evidence instead of an exact empty result.

The dashboard's `a` answers the selected session's pending question or native
permission; the Control header and chair startup include qualified global
counts. Read-only WAL recovery failures surface as unavailable rather than
triggering a repair or returning an empty success.

Managed sessions connect Asha's structured backend to Claude and Codex. Queuing
work starts an independent session owner, detached from the command that queued
it; the owner drains the harness stream, records events and runs queued
follow-ups at turn boundaries.
Control is a reader and operator interface. Closing it leaves owners running.
Rooms retain their existing tmux attach/close behavior.

## Commands

```sh
asha control projects --match NAME --json
asha control session create --cwd /absolute/project --prompt 'Assignment' --max-turns 12 --json
asha control session admission status --json
asha control session list --json
asha control session summary
asha control session show SESSION_ID --after 0 --limit 100 --json
asha control session send SESSION_ID --key delivery-1 --text 'Follow-up' --json
asha control session answer REQUEST_ID --digest QUESTION_DIGEST --text 'Answer' --json
asha control session request REQUEST_ID --json
asha control session permission REQUEST_ID --decision allow --digest INVOCATION_DIGEST --json
asha control session stop SESSION_ID --json
asha control session doctor --json
asha control session init --json
asha control session backup /absolute/private-directory/control-backup.sqlite3
asha control session migrate --json
asha control session quiesce --json
asha control session search 'manuscript review' --limit 50 --json
asha control session rebuild-search --json
```

`projects` lists the Asha projects a session can launch in, indexed from
`--root DIR`, then `ASHA_PROJECTS_ROOT`, then the configured `project_roots`,
then the current directory; `--match` selects by name, directory, relative
path or project ID.

`create`, `send`, `answer` and `resume` record work and start its owner at once
(see [Owner start and recovery](#owner-start-and-recovery)). An operator can
also run `session owner SESSION_ID` in the foreground. One owner is
fenced by its PID, process incarnation and retained generation. Duplicate delivery
keys with identical content return the existing message. Different content using
the same key is refused.
At most two managed input turns run concurrently across the database. Waiting
owners are lightweight processes and do not consume model turns. Current-work
views name the occupied turn limit when it delays retained input; that reason
clears as capacity becomes available. `init` can
initialize an empty store left by interrupted creation without admitting work;
it does not discard or reinterpret corrupt or future-version data.

Managed actors cannot sign operator actions or answer their own questions.

The managed agent asks a clarification with
`asha control session ask --question 'Question' --json`, then finishes its turn.
The client sends a typed request to the owner's private Unix endpoint; it does
not open SQLite. The endpoint accepts only `ask`. The owner checks the request's
session, retained generation and active turn before committing the question;
it does not inspect the peer process, because the local user is trusted.
Environment labels only select the endpoint. The owner services requests in a
bounded I/O thread while the harness waits on its tool process. Frames and
deadlines are bounded, database contention gets at most three exact-ID client
attempts, and lost replies do not discard committed questions. Operator answers
are unavailable over this actor channel.

A recorded answer queues exactly one follow-up. Control displays the same
summary, and the dashboard's `a` answers the selected session's pending
question. A clarification answer never approves a native tool permission.

Native tool requests appear separately in the summary. `a` opens a scrollable
review of the complete invocation, including its original input, native session,
turn, generation and digest. Press **a** to allow that invocation, **d** to deny,
or **Esc** to leave it pending. CLI callers inspect `session request` and use
`session permission` with the inspected digest. A decision is sent back to the
waiting native process in the same turn; it does not enqueue a model message or
create a persistent permission rule. The operator CLI refuses managed role labels,
the worker profile and non-chair sessions on a sandboxed harness; it does not
inspect process ancestry. This has the same-user limits described below.
Cancellation or owner loss withdraws pending requests. A lost response remains
uncertain and is never replayed automatically; `submitted` means the complete
response was written, not that the tool executed. A later provider cancellation
does not erase submission evidence. Decisions that never reached the transport
are cancelled when the turn closes; only a reserved response can be uncertain.
The active-work deadline pauses during permission waiting. A separate cumulative
24-hour decision-wait budget bounds each turn; cancellation still stops it promptly.

## State and recovery

SQLite at `$ASHA_HOME/state/control/control.sqlite3` owns managed sessions,
messages, turn reservations, questions and session events, and keeps the
retired initiative and task records as read-only evidence. Rooms are `rooms`
records in the same database; there is no other registry backend.

`show` distinguishes retained/queued input, submitted input and consumption
evidence. Claude initialization and successful results do **not** prove message
consumption or accepted task completion. Completion belongs to a model turn;
the operator decides whether work is accepted.
Counts cover all matching retained rows; completeness flags identify truncated
lists, and event cursors paginate history. Message rows show the most recent
deliveries. Text is escaped for terminal display.

If an owner is lost during a reserved turn, the next owner parks the session as
`uncertain`. It does not replay the input. Inspect native history and work effects
before requesting a new turn. Provider failures and exhausted turn budgets also
park instead of retrying indefinitely:

```sh
asha control session resume SESSION_ID --digest RECOVERY_DIGEST \
  --text 'Continue from the inspected state; the previous turn may have acted' \
  --max-turns 20 --json
```

The recovery digest is returned on `show`. Increasing this session's input-turn
budget does not amend any other budget. A native session ID allows
conversation resume; it is not proof of reattachment to a running turn.

Structured Claude rate-limit events, assistant error codes and terminal API
statuses produce durable recovery conditions. Model text and stderr never
establish a quota condition. `show` includes the recovery category, source turn,
provider observation, reset time when supplied, delivery evidence and next step.
The shared summary counts quota-blocked sessions separately. Authentication,
billing, native provider budgets, cancellation, provider errors and interrupted
transport remain distinguishable from quota.

An observed reset time is a not-before condition, not proof the provider is now
available. Early resume is refused; after the time passes, an operator still
inspects retained work and explicitly queues a fresh recovery input. If no reset
was supplied, the recovery condition says to confirm availability. A warning
does not park work. An allowed or warning observation supersedes rejection only
for the same quota window; other windows retain their rejection and reset time.
The latest retained reset across rejected windows is the not-before condition.
When account/provider failures coexist with quota, the headline explains the
account/provider repair and also names the independent quota condition.
A rejection reported after a completed turn parks subsequent
work while preserving the successful turn outcome. Owner replacement preserves
the observed provider condition and keeps uncertain submission separate from a
known terminal failure. No failed or ambiguous input is replayed automatically.

If a reset timestamp is incorrect, the operator can supply
`session resume ... --quota-reset-override 'REASON'`. This records the reason in
the recovery receipt, resolution, and event while retaining the original reset.
It applies only to a retained quota condition, changes no turn budget,
and does not assert that the provider will accept work. Managed actors cannot
use the operator resume command or override the reset through their IPC channel.

A rejection before the native initialization acknowledgment records delivery as
`not-submitted`: the assignment was never released. Its retained message is
cancelled, with its text available for inspection. Ambiguous submissions remain
`uncertain`. Both require explicitly restating any still-needed assignment in
the recovery input; neither automatically resends the original message. A later
transport error preserves any previously recorded terminal outcome and parks
the session separately.

Recovery is bound to both the observed condition and the existing turn budget.
Duplicate recovery commands queue one new input; conflicting text or a changed
budget amendment is refused. Replays after a new failure, stop, or recovery are
refused and require inspecting the current session. Event revisions distinguish
successive stop/recovery cycles even when no model turn ran between them.
The recovery input runs ahead of older queued answers and ordinary work.
Explicit recovery cancels unanswered clarifications from the failed path while
retaining their text and cancellation events for inspection; the new turn can
ask again if the question still applies. Resume also explicitly revives a stopped
session and retains its spent turn count.
All reserved turns remain counted, including failed or ambiguous submissions.
Provider metadata and recovery receipts use the existing indexed SQLite record
registry and are included in backups and migration state proofs.

The wire fields were checked against Anthropic Agent SDK revision
`6bbd3093147c2fadcd4b868599b8fb6d9db3d523`: the
[parser](https://github.com/anthropics/claude-agent-sdk-python/blob/6bbd3093147c2fadcd4b868599b8fb6d9db3d523/src/claude_agent_sdk/_internal/message_parser.py#L356)
maps `rate_limit_info.status`, `resetsAt`, and `rateLimitType` directly;
its [parser tests](https://github.com/anthropics/claude-agent-sdk-python/blob/6bbd3093147c2fadcd4b868599b8fb6d9db3d523/tests/test_message_parser.py#L928)
provide an independent warning-frame example. The
[result definitions](https://github.com/anthropics/claude-agent-sdk-python/blob/6bbd3093147c2fadcd4b868599b8fb6d9db3d523/src/claude_agent_sdk/types.py#L1337)
document HTTP error status and both cancellation terminal reasons; the budget
option documents `error_max_budget_usd`. Local tests cover those failure classes.
Acceptance uses a fake provider over real process pipes and the private request
socket; no live account quota was exhausted for testing.

Session display output uses a bounded store: each session retains at most 256
recent text/progress/tool payloads and 1 MiB of encoded payload records. Audit
events retain their sequence, timestamps, content digest and compact tool or
delivery facts. Retiring display bodies preserves questions, answers, approvals,
turn history and the recovery revision. The limit covers payload records;
SQLite search/index overhead and audit metadata are additional. Pre-existing
inline output remains readable and is preserved as legacy history.

```sh
asha control session events SESSION_ID --consumer control --limit 100 --json
asha control session ack-events SESSION_ID --consumer control --through SEQUENCE --json
```

Use a stable, distinct consumer name for each interface; a session supports up
to 32 retained consumer checkpoints. Names identify checkpoints within the
operator's shared authority, rather than separate access permissions. Event pages
start after that
consumer's retained acknowledgement unless `--after` is supplied explicitly.
Reading a page leaves the acknowledgement unchanged. After delivery, acknowledge
its `next_event_cursor`; duplicate or delayed acknowledgements cannot move the
cursor backwards. An acknowledgement is the operator's assertion of delivery
through that event; the backend validates session membership and monotonicity,
without claiming to prove what an external interface displayed. Acknowledgements
record interface delivery separately from
model consumption and queue no work. `ack-events` is an operator command.

A slow or restarted consumer can receive events whose display body expired.
Those events carry `output.available: false` and an `output_missing` payload;
`output_gaps` lists the affected sequences and retained digests. Consumers use
`event.output.available` and `output_gaps` as authoritative gap metadata; payload
fields alone are display content. Compact delivery
and tool facts remain available. Missing data without a matching retention
record raises a storage error. Event envelopes, retained bodies, retention
watermarks and consumer cursors share SQLite transactions and backups. The
`session doctor` output reports these limits and cursor capabilities.

The Linux guardian records its process incarnation before releasing the harness
startup barrier and terminates its process group if its owner dies. Recovery and
dead-owner stop refuse while a retained provider process is still live. Processes
that deliberately detach into another group remain outside this cleanup contract.

Operator runtime commands persist separately from any owner process:

| Command | Effect |
| --- | --- |
| `session admission pause` | Pause new input turns; a turn in progress finishes and idle owners exit. |
| `session admission drain` | Pause admission and let managed owners exit after their current turn. Queued inputs and questions remain. |
| `session admission resume` | Reopen admission and start owners for work queued meanwhile. It does not clear individual session stop requests. |
| `session admission stop` | Persist stop requests for managed sessions; owners cancel their turns. |
| `session admission status` | Show the durable admission policy. |

These commands accept `--json`; every verb but `status` refuses managed actors
and workers. Closing Control does not reset a pause. Use `admission resume`
after inspecting recovered state. Individual stopped or uncertain sessions
still need their own recovery action. No live session is automatically changed
by installing these code files.

### Owner start and recovery

No daemon schedules structured work. An owner starts, detached in its own
process session, where work is queued: a structured `session launch`,
`create`, `send`, `answer`, `resume`, a close request for a structured
session, the dashboard's send, answer and resume, and `session admission
resume`. It outlives the command that started it and inherits that command's
environment, without tmux, Room, hub-session and managed-actor fields, so a
harness on the caller's `PATH` is found. Each owner logs to
`session-logs/SESSION_ID.log` beneath the Control state root.

A session has one owner. A transactional launch reservation admits one of any
racing starts and holds off another start for five seconds; the owner's
generation claim fences the rest. A start that never claims backs off from five
seconds, doubling to 300, and the session fails after eight unclaimed launches.

An owner keeps custody while its session has runnable input, including input
that waits only for the managed turn limit, and gives custody back in the same
transaction that finds none. Input queued after its last turn is therefore run
by that owner or starts a new one.

An owner lost to a crash or reboot restarts on the next operator
`asha control session show ID` of that session or `session list`, as well as on
a send or resume to it. The new owner reconciles a turn its predecessor left
running to `uncertain`; nothing is replayed. Until then the session reads
`waiting_on: owner`. Reads by a worker or managed actor never start an owner,
and neither does the dashboard's refresh.

The supervisor daemon and its systemd user unit retired on 2026-10-07.
`asha control supervisor ...` prints these steps, and `asha control doctor`
reports a unit left installed:

```bash
systemctl --user disable --now asha-supervisor.service
rm "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/asha-supervisor.service"
systemctl --user daemon-reload
```

Nothing reads `supervisor.lock`, `supervisor.json` or a `supervisor.log`
beneath the Control state root any more; they may be deleted.

## Capability limits

Claude uses the Asha launcher, bidirectional structured print I/O and native
resume with manual permissions forwarded to the host. Existing tool policy still
applies. Native permission decisions use Claude's `can_use_tool` control protocol;
an allow response returns only the original invocation input. Native denials fail
visibly. Steering an active turn remains unavailable. Claude 2.1.263 has passed a
bounded native test with one exact temporary Write approval, one submitted
response, verified file content and a clean terminal result. Startup hooks and
post-result lifecycle notifications remain compatible with the Asha launcher.

Codex adapter code now implements the app-server JSON-RPC lifecycle for the
installed 0.153.4 contract: initialize, thread start/resume, turn input, scoped
events and native terminal results. The managed adapter is available after native
coordinator acceptance on this version. Fake subprocess tests cover
bidirectional delivery through the managed backend, including two turns retaining
one native thread. A native probe with no model input verified initialization,
ephemeral thread creation, the requested policy echo and clean EOF shutdown.
The codec accepts the installed server's startup notifications. Native acceptance
now verifies model start/resume, file-change event ordering, exact file approval,
command approval and denial of a second file edit in the same conversation. The
denied edit leaves the first approved contents unchanged; no persistent rules or
root grants are issued.

A separate real managed-owner test found that the normal Codex sandbox refuses
the Unix-socket connection used by `session ask`, even after command approval.
The native turn completed, but no question was retained. The owner now hosts the
native `asha_control` dynamic tool, whose only operation is `ask` (the
initiative operations it once carried retired with the engine). Call arguments
cannot select another session or invoke operator actions.
The execution sandbox remains unchanged. Two real managed turns verified a
retained question, an operator answer and a second question in the same native
conversation. Codex0.153.4 retains dynamic tools across thread resume; its resume
schema does not accept replacement definitions.

Actor calls use stable IDs and SQLite receipts. A lost execution result requires
inspection rather than blind replay. Calls run serially outside provider I/O;
capacity refusal reports that no operation started. Shutdown records queued
cancellations and waits for any active effect before finalizing the turn. This
clarification check was followed by complete native coordinator acceptance: four
turns in one Codex conversation, one question and idempotent answer, a Claude
implementation worker, independent Claude review, passed controller verification
and a delivered final report. The private production-migrated SQLite fixture
reached ready-for-integration in233 seconds, with three completed coordinator
dispatch actions and no coordinator terminal intervention. The fixture did not integrate its changes. The adapter is enabled; the later
managed-default rollout and live SQLite cutover are recorded in the migration audit.

A process-level test starts a real owner from a caller that exits at once and
verifies that the owner outlives it, opens the retained question, refuses a
second owner while it is live, gives custody back once the question parks the
session, and that a later answer starts a new owner which delivers it exactly
once in the same native conversation. (Before 2026-10-07 the same test killed
and replaced the retired supervisor; a fixture load of 250 stopped sessions and
two active owners then reserved four eligible turns in 0.25–0.50 seconds under
a one-second supervisor tick.) Provider behavior in these checks is
deterministic; the separate native Claude and Codex cycles above establish
provider compatibility.

Room creation, actual terminal attach/detach and close have also passed on an
activated SQLite registry using a dedicated real tmux server and a fixture process.

Completed Codex commands now retain their native status, signed exit code and a
bounded diagnostic tail. Status and exit code survive display-output retirement.
Malformed optional diagnostics are marked unavailable without hiding the tool's
status; tool failure remains distinct from native turn completion. Managed
instructions require waiting on an existing tool handle and confirming the
returned question ID before ending the turn.

Blocking Codex questions use `session answer-native REQUEST_ID --digest DIGEST
--answers '{"answers":{"question-id":{"answers":["text"]}}}'`; inspect the full
request with `session request REQUEST_ID --json` first. Control's managed question
picker presents each question and returns the entered answers on the same native
request. Answers do not enqueue another model turn. Native command decisions
reuse `session permission`; grants never create future command rules. Permission
profile grants retain the exact requested profile and expire at the end of the
native turn. File approvals require retained exact changes; session root grants
are refused. Command approvals require the provider to disclose the exact command
and working directory. Review data is bounded at 256 KiB per request, 4 MiB per
turn and 128 requests; the separate file-item cache holds up to 2 MiB. Larger
ordinary patches still emit tool progress, but approval requires complete retained
review bytes. Native nonblocking questions and secret inputs are explicitly
unsupported by this per-turn transport. A successful turn/start response proves
input acknowledgement only; it is not a consumption or completion receipt.

Copilot and OpenCode managed adapters remain explicitly unavailable. Existing
interactive and worker paths remain available. Live historical-state cutover
completed on 2026-09-09 ([migration plan](proposals/2026-09-07--managed-agent-sessions.md)).

The SQLite layer verifies schema identity, private file layout, WAL sidecars,
foreign keys, full synchronous writes and bounded lock waits. Backup uses SQLite's
backup API, including committed WAL data, and refuses existing destinations. The
generic record store offers digest-based compare-and-swap and bounded keyset
queries. FTS5 provides literal token-phrase search over decoded record text and
managed messages. `search --session SESSION_ID --after CURSOR` scopes and paginates
message results. `rebuild-search` reconstructs derived indexes; search never grants
authority. Doctor checks the actual search tables as well as database integrity
and foreign-key relationships.

Schema version 2 adds these search indexes and a durable admission gate; version
3 adds durable native permission decisions and response custody. Version 4 adds
unique initiative event sequences and key guards, preserving existing payload
bytes. Tail/cursor reads load only selected event payloads; full journal replay
remains available for verification. Version 5 adds a scoped lifecycle index for
per-initiative approvals and actions, preserving existing record bytes and IDs.
Doctor reports a missing or incompatible scoped index. An older
schema is refused until an operator runs `session migrate` with managed owners
stopped. Schema upgrades are transactional: interruption leaves the previous
version and canonical record bytes intact. This command upgrades the database
schema.
If an older schema prevents ordinary lifecycle commands, `session quiesce` stops
the scheduler and requests shutdown through verified owner process handles without
writing that older schema. Wait for those owners to exit, then migrate. Interrupted
sessions use digest-bound `session resume` with a fresh recovery prompt; cancelled
old inputs are retained as cancelled and are never replayed automatically.

The offline registry staging, activation, recovery and rollback commands
(`asha control registry ...`) completed the SQLite cutover on 2026-09-09 and
retired with the initiative engine (L-b). The file-backed Room registry and
backend selection followed (B7): a fresh home creates the database on its
first Room or `session init`.

Restore uses a separate, empty recovery state root:

```sh
ASHA_HOME=/absolute/recovery-home asha control session restore \
  /absolute/private-directory/control-backup.sqlite3 --json
```

Restore checks schema, integrity and foreign-key relationships before atomic
publication. It refuses to overwrite existing Control state and keeps managed
dispatch paused. Version 1 backups are upgraded in the destination without
modifying the backup. Source records and native conversation IDs are preserved. The
recovered root requires operator reconciliation before dispatch; switching live
roots and resuming recovered work is not performed by restore.

If the restore process dies during publication, its offline destination may keep
the temporary `.restore-*.sqlite3` name beside the published database. Publication
links only a complete, validated database already paused for reconciliation, so
that root opens normally, and a repeated restore into it refuses because it is not
empty. A death before publication leaves only the temporary file, which no open
mistakes for the database; repeat the restore from the unchanged backup into a new
empty recovery root. Read-only inspection accepts a restored
database's DELETE journal mode without converting it. Writable connections require
WAL. Backups cannot use the source database's own main or sidecar filenames.

The role checks protect the validated CLI. They read environment labels only and
are not an OS security boundary against arbitrary code running as the same user,
which is trusted (threat model, 2026-10-05). A sandboxed agent cannot write
Control state; harness sandbox and execution policy remain necessary.

## Native workflow acceptance

These acceptance runs exercised the retired coordinator path and are kept as
history. Claude2.1.266 completed a real four-turn coordinator cycle in a disposable
production-migrated SQLite root: clarification and idempotent answer, one headless
implementation, an independent review, controller verification and a final report
in the same native conversation. The initiative reached ready-for-integration;
no integration was performed. The run used eight exact native permission
decisions, no persistent permission grant and no terminal relay for coordination.
Existing headless workers retain their normal terminal-backed launch/evidence path.
The separate Codex acceptance and installed default rollout are recorded above.
See the migration audit for final verification and landing status.
