# Managed sessions

## Starting work

`asha initiative coordinator launch --project PROJECT --intent TEXT --json`
uses managed Claude by default; `--harness codex` selects the supported Codex
adapter. Control's `n` form uses the same atomic intake. The selected project must
be initialized and the operational registry must have completed SQLite activation.
Keep the returned launch ID; retry the same assignment with `--launch-id UUID`
after a lost response. Initiative, session, and opening-message custody commit
together before the supervisor is started. Paused/draining/stopped admission is
preserved, and supervisor startup failure reports the retained assignment.

`coordinator attach ID` inspects a managed session. In Control, Enter shows state
and event pages without changing delivery acknowledgements. For legacy terminal
coordination, use `coordinator launch --transport tmux --root DIR --intent TEXT`.
The scheduled trigger generator retains that explicit legacy transport.

The source default and installed harness files are updated. Live registry cutover
completed on2026-09-09; admission remains stopped. Codex/OpenCode drift checks
pass. Final repository-wide verification and landing remain pending.

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

Rows carry session and initiative identity, project directory, generation,
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

SQLite Room and live-task observations now filter lifecycle through the state
index before applying the cap. Running tasks and open Rooms precede stuck
creations; creating tasks precede failed tasks. Failed tasks that retain a live run remain candidates. A capped scan of
failed candidates is still reported as incomplete. Within each lifecycle the
sample favors recently updated records; timestamp ties are unspecified. Unknown
state projections report unavailable evidence instead of an exact empty result.
SQLite initiative heads use the same state index for chair startup and Control's
retained tree. Needs-input, plan approval and integration-ready heads precede
running and other current work; quiet retained history follows. All lifecycle
states remain eligible for All retained, and reaching the head cap still reports
incomplete coverage. Ordinary role-proof enumeration keeps its existing reader.
Within a SQLite initiative, approval reads select requested decisions before
settled history; action reads select uncertain and in-flight records first.
Control and `initiative attention` read these classes before graph history,
reserving at most a quarter of the remaining record allowance for each, capped
at 128 records per class. Class caps report incomplete coverage and leave room
for other evidence. These reads prioritize observation; approving or answering
still reloads the exact current subject and authority.
Their legacy file adapters retain bounded scans
and report incomplete coverage when history exhausts the scan. Full retained
views remain available. Room/task/initiative observations retain completeness
reporting; their remaining unified
indexing and presentation work is tracked in the migration audit.

On an active SQLite registry, `asha initiative attention --page initiatives|approvals
--limit 50 --json` reads independent global action pages. Pass the returned `next`
value as `--after`. `complete` describes this page only: accumulate unreadable
records across pages. If `retry` is true, retry the same position; a null cursor
with a timeout does not prove exhaustion. The order is lifecycle priority followed
by oldest update first. Each page has its own snapshot, so refresh from the start
when current state changes.

Control **G** browses these families with Next, Retry, Refresh and Switch family.
Select a candidate, press **r** to inspect its complete subject, then **r** again
to enter an offered decision. Plans and salvage/review-retry requests support
approval or rejection; approved initiatives offer activation. Every decision
rechecks the displayed records under the existing lifecycle lock. A changed
subject requires a fresh inspection. Plan rejection also requires a reason.
The browser retains its page position after a decision. Integration candidates
remain inspection-only. For an open legacy initiative question, **resume** records
resolution; it does not deliver a text answer or decide a paused seal.

Control **M** handles managed questions and native permissions separately, with
Next, Retry and Refresh to reach later requests. The Control header and chair
startup include qualified global counts independently of retained tree caps.
Expired, stale-plan and terminal approval requests remain inspection evidence,
not fresh approval demand. Pages expose runtime admission and explicitly report
that deep execution bindings have not been checked. Existing decision commands
revalidate those bindings. Inspection refuses an inactive, transitional or damaged
backend; read-only WAL recovery failures surface as unavailable rather than
triggering a repair or returning an empty success.

Managed sessions connect Asha's orchestrator backend to Claude through structured
stdio. The supervisor starts an independent session owner; the owner drains the
harness stream, records events and runs queued follow-ups at turn boundaries.
Control is a reader and operator interface. Closing it leaves owners running.
Rooms retain their existing tmux attach/close behavior.

## Commands

```sh
asha control session create --cwd /absolute/project --prompt 'Assignment' --max-turns 12 --json
asha control supervisor start --json
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

`create` records work; the supervisor starts its owner on the next sweep. An
operator can also run `session owner SESSION_ID` in the foreground. One owner is
fenced by its PID, process incarnation and retained generation. Duplicate delivery
keys with identical content return the existing message. Different content using
the same key is refused.
At most two managed input turns run concurrently across the database. Waiting
owners are lightweight processes and do not consume model turns. Current-work
views name the occupied turn limit when it delays retained input; that reason
clears as capacity becomes available. `init` can
initialize an empty store left by interrupted creation without admitting work;
it does not discard or reinterpret corrupt or future-version data.

Use `--initiative INITIATIVE_ID` on creation to bind a managed coordinator to an
existing initiative in the same repository. Its plan approval and worker execution
still use the established initiative actions. A live existing coordinator must
be released before switching that initiative. Managed actors cannot sign operator
actions, answer their own questions, or claim another initiative.

The managed agent asks a clarification with
`asha control session ask --question 'Question' --json`, then finishes its turn.
The client sends a typed request to the owner's private Unix endpoint; it does
not open SQLite. The owner checks Linux peer credentials, a peer process handle,
ancestry, retained generation and the active turn before committing the question.
Environment labels only select the endpoint. `session doctor` probes the required
kernel support; a sandbox that hides the necessary process identity is refused.
The owner services requests in a bounded I/O thread while the harness waits on
its tool process. Frames and deadlines are bounded, database contention gets at
most three exact-ID client attempts, and lost replies do not discard committed
questions. Operator answers are unavailable over this actor channel.

A recorded answer queues exactly one follow-up. Control displays
the same summary and offers **M** to select and answer a pending question. Plan
approval remains a separate initiative action; a clarification answer never
approves a plan or a native tool permission.

Native tool requests appear separately in the summary. **M** opens a scrollable
review of the complete invocation, including its original input, native session,
turn, generation and digest. Press **a** to allow that invocation, **d** to deny,
or **Esc** to leave it pending. CLI callers inspect `session request` and use
`session permission` with the inspected digest. A decision is sent back to the
waiting native process in the same turn; it does not enqueue a model message or
create a persistent permission rule. The operator CLI refuses managed role labels
and owner descendants, and refuses incomplete ancestry inspection; this has the
same-user limits described below.
Cancellation or owner loss withdraws pending requests. A lost response remains
uncertain and is never replayed automatically; `submitted` means the complete
response was written, not that the tool executed. A later provider cancellation
does not erase submission evidence. Decisions that never reached the transport
are cancelled when the turn closes; only a reserved response can be uncertain.
The active-work deadline pauses during permission waiting. A separate cumulative
24-hour decision-wait budget bounds each turn; cancellation still stops it promptly.

## State and recovery

SQLite at `$ASHA_HOME/state/control/control.sqlite3` owns managed sessions,
messages, turn reservations, questions and session events. The selected registry
backend owns plans, approvals, attempts, seals and accepted evidence; legacy files
remain authoritative until explicit registry activation.
An explicit legacy adapter bridges messages and relevant initiative events with
idempotent delivery keys and a retained cursor. Registry migration is explicit;
normal operation never maintains two writable registry authorities.

`show` distinguishes retained/queued input, submitted input and consumption
evidence. Claude initialization and successful results do **not** prove message
consumption or accepted task completion. Completion belongs to a model turn;
the existing review and verification gates decide whether work is accepted.
Counts cover all matching retained rows; completeness flags identify truncated
lists, and event cursors paginate history. Message rows show the most recent
deliveries. Text is escaped for terminal display.

If an owner is lost during a reserved turn, the next owner parks the session as
`uncertain`. It does not replay the input. Inspect native history and work effects
before requesting a new turn. Provider failures and exhausted turn budgets also
park instead of creating new initiatives or retrying indefinitely:

```sh
asha control session resume SESSION_ID --digest RECOVERY_DIGEST \
  --text 'Continue from the inspected state; the previous turn may have acted' \
  --max-turns 20 --json
```

The recovery digest is returned on `show`. Increasing this session's input-turn
budget does not amend any initiative or review budget. A native session ID allows
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
It applies only to a retained quota condition, changes no turn or review budget,
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

An exhausted sealed review has a separate, one-attempt amendment:

```sh
asha initiative request-review-budget INITIATIVE --node REVIEW_NODE --review FAILED_REVIEW_ID --reason 'Why another review is needed' --json
asha initiative approve-review-budget INITIATIVE --request REQUEST_ID --json
```

To decline a salvage or review-retry request from the chair or CLI, inspect its
record and use the returned digest:

```sh
asha initiative approval INITIATIVE --request REQUEST_ID --json
asha initiative reject-request INITIATIVE --request REQUEST_ID --digest REQUEST_DIGEST --json
```

Rejection preserves the request, rationale and evidence. It cannot revoke an
already approved request. Repeating a completed rejection returns the retained
decision without signing it again.
If a rejection was stored but its event write was interrupted, the request leaves
the pending queue. Inspect it with `approval` and repeat `reject-request` from the
chair or CLI to finish that journal entry; the recorded signer is preserved.

A fenced coordinator uses `--as-coordinator` on the request command, or submits
`request-review-budget` through the existing action-document interface. It
cannot sign the approval. In Control, the request appears as **review retry**;
select the initiative or review node and press `a` to inspect the seal, commit,
prior review and rationale, then type `approve` to authorize one attempt.

The request requires the latest settled failed review, exhausted ordinary
per-node or initiative task budget, and the current exact sealed target.
Accepted findings still require repair; this amendment cannot discard a verdict.
Signing preserves the original plan limits and prior evidence and reopens only
that review node. The scheduler reserves one deterministic attempt/task identity
and consumes the grant before launch. A failed extra review requires fresh
approval; the old grant cannot authorize another attempt or another node.
Dependency, concurrency, deadline, storage and runtime admission checks remain
in force. Approval does not undo an initiative pause: explicitly resume the
initiative when ready. Stale or expired requests are retired during action
reconciliation, so they do not remain as obsolete approval demands.

The Linux guardian records its process incarnation before releasing the harness
startup barrier and terminates its process group if its owner dies. Recovery and
dead-owner stop refuse while a retained provider process is still live. Processes
that deliberately detach into another group remain outside this cleanup contract.

Session owners survive scheduling-supervisor process restarts. Generated systemd
units use `KillMode=process` for that reason; existing installations need the usual
`supervisor install` update to acquire this setting. Operator runtime commands
persist separately from the supervisor process:

| Command | Effect |
| --- | --- |
| `supervisor pause` | Pause new input turns and task launches; let admitted work finish and keep session owners available. |
| `supervisor drain` | Pause admission and let managed owners exit after their current turn. Queued inputs and questions remain. |
| `supervisor resume` | Reopen admission. This does not launch a missing supervisor or clear individual session stop requests. |
| `supervisor stop` | Persist stop requests for managed sessions and stop the scheduling process. Existing headless workers use their task stop action. |
| `supervisor status` | Show scheduling-process status and durable admission policy separately. |

These commands accept `--json`. Restarting the supervisor or closing Control does
not reset a pause. Use `supervisor resume` after inspecting recovered state, and
`supervisor start` when its process is absent. Individual stopped or uncertain
sessions still need their own recovery action. No live service or session is
automatically changed by installing these code files.

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
native `asha_control` dynamic tool for ask, initiative inspection, plan proposal,
coordinator actions and explicit message receive/ack. Call arguments cannot select
another session or initiative, or invoke operator approval/integration actions.
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

The scheduling supervisor can restart while a managed owner and its provider
connection remain live. A process-level failure test now kills the supervisor,
starts a replacement, and verifies unchanged owner/generation/provider identity,
the retained pending question, and one effective delivery of a repeated answer.
Under a fixture load of250 stopped historical sessions and two active owners,
four eligible turns reserved in0.25–0.50 seconds with a1-second supervisor tick.
Provider behavior in these failure/load checks is deterministic; the separate
native Claude and Codex cycles above establish provider compatibility.

A legacy coordinator may take over a managed initiative only after that session
is stopped, its owner and provider have exited, and its latest submission is
settled. An uncertain submission remains a refusal even when the coordinator
record is stale or exited. The check binds the predecessor's state root and uses
durable reservation order rather than wall-clock order. A transport handoff keeps
the same initiative, plans and SQLite authority; it does not reactivate old files.
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
interactive and worker paths remain available. The default coordinator launcher
uses managed sessions on an activated SQLite registry. Live historical-state
cutover is complete; final repository-wide verification remains pending in the
[migration plan](proposals/2026-09-07--managed-agent-sessions.md).

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
schema; importing the legacy file registries remains separate unfinished work.
If an older schema prevents ordinary lifecycle commands, `session quiesce` stops
the scheduler and requests shutdown through verified owner process handles without
writing that older schema. Wait for those owners to exit, then migrate. Interrupted
sessions use digest-bound `session resume` with a fresh recovery prompt; cancelled
old inputs are retained as cancelled and are never replayed automatically.

Offline registry staging covers tasks, Rooms, initiative heads and all initiative
record classes, creation journals, standing authorities, prune records and
repository intents. It copies assignment/output and ownership artifacts as files, retains
historical plan observation restrictions, and checks source bytes, membership,
directory identity, live processes and event continuity before publishing its
completion manifest. Record and artifact entries use bounded ledger pages, with
counts and an integrity digest in the manifest. Version 3 also binds a complete
source tree ledger (including inode identities and original permissions) and the
logical database state independently of WAL/backup file layout. The pre-import database snapshot
is retained separately. A failed import remains durably paused and marked
incomplete; its destination is retained for inspection and cannot be reused.
Open Rooms are recorded as external conversations whose liveness must be checked
at activation. Staging does not activate a backend.

Public stores select SQLite only through an explicit root-bound backend marker;
database presence alone keeps the file backend. Normal stores refuse incomplete
transitions and offline staging roots. The operator cutover commands are:

```sh
asha control registry status --json
asha control registry stage --stage-home /absolute/private-stage-home --json
asha control registry activate --stage-home /absolute/private-stage-home --json
asha control registry recover --action resume --json
asha control registry recover --action abort --json
asha control registry rollback --json
```

Before staging, initialize the database, pause or drain admission and stop task
runs and coordinator/managed owner processes. The commands refuse active writers;
they do not stop processes implicitly. A stage uses a separate empty root and is
bound to the original Asha root. Activation revalidates the stage, retained backup,
source state and immutable ownership of open Rooms. It durably records a recovery
transition before freezing legacy files, then publishes imported records and the
backend marker in one transaction on the existing database inode. It preserves
the original admission setting; activation does not resume work.
Registry mutations are operator commands; managed sessions, task workers and
Room-hosted agents may inspect status but are refused these mutation commands.

Interrupted preparation leaves normal store construction unavailable until
`recover` resumes it or aborts it by restoring the exact original file modes.
Read-only `registry status` and the backend doctor probe report this state and
the recovery choices. Recovery refuses changed recorded source identities or
contents. Aborting activation permits harmless new entries and preserves them;
it restores permissions only on the exact recorded files. Changed recorded files
must be reconciled before recovery can proceed.
The retained stage and source database snapshot remain available for inspection.

Rollback is available only before any new database or artifact work after
activation, including admission-mode/revision changes even if no task ran.
It verifies that baseline, restores original file permissions and
retires SQLite registry authority atomically. If new work exists, rollback
refuses: it cannot make stale file records authoritative without a validated
reverse export. Interrupted rollback can also be resumed or aborted. Persistent
database triggers and shared/exclusive migration locks fence stale SQLite writers;
the source directory/file locks and permissions fence ordinary legacy writers.
These are application coordination boundaries, not isolation from arbitrary
same-user code capable of rewriting its own database or permissions.
After abort or rollback, create a fresh stage before attempting activation again:
preparation can leave empty registry roots that were absent from the old stage.
If rollback committed but its response was lost, `recover` returns the retained
completed outcome without repeating deletion or permission changes.

Activation/recovery/rollback have temporary-fixture acceptance tests and completed
cold correctness/security reviews. Review fixes passed targeted regressions,
including safe extra-file abort after directory freezing and legacy initiative
layout/ingestion-lock fencing. Live state cutover completed on2026-09-09. SQLite is now the active operational
backend; scheduling remains stopped.

SQLite stores write new assignment/output artifacts under `control/artifacts` and
new ownership sidecars under `control/materialization-ownership`. Retained files
stay at their original real paths and inodes; symlink relocation is unsupported.
The retained artifact reader permits owner-only read-only directories/files, while
ownership sidecar files must keep mode 0600 because their exact mode is bound into
the journal. Current artifact write residue can be recovered under the initiative
lock; retained source files are never swept. These adapters are covered by temporary
fixtures and have not been activated against live state.

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
roots and resuming recovered work is not performed by restore. Recovery of artifact
files and legacy registries remains necessary while those domains are file backed.

If the restore process dies during publication, its offline destination may retain
an extra temporary hard link. Normal database access refuses that incomplete root.
Repeat the restore from the unchanged backup into a new empty recovery root; do
not activate the interrupted destination. Read-only inspection accepts a restored
database's DELETE journal mode without converting it. Writable connections require
WAL. Backups cannot use the source database's own main or sidecar filenames.

The role checks protect the validated CLI. They are not an OS security boundary
against arbitrary code running as the same user and modifying the database or
initiative files directly. Deliberately detached and reparented same-user processes
also fall outside the descendant proof. A controlling terminal or a shared user
cgroup would not establish a separate principal. Harness sandbox and execution
policy remain necessary.

Activation derives required harnesses from the approved plan. Its runtime doctor
checks those executables and their installed hook surfaces; an unrelated harness
installation does not block that plan. The general doctor continues to inspect
all installed harnesses. SQL-backed initiative readiness validates the selected
SQLite registry instead of requiring the retained legacy directory to be writable.


## Native workflow acceptance

Claude2.1.266 completed a real four-turn coordinator cycle in a disposable
production-migrated SQLite root: clarification and idempotent answer, one headless
implementation, an independent review, controller verification and a final report
in the same native conversation. The initiative reached ready-for-integration;
no integration was performed. The run used eight exact native permission
decisions, no persistent permission grant and no terminal relay for coordination.
Existing headless workers retain their normal terminal-backed launch/evidence path.
The separate Codex acceptance and installed default rollout are recorded above.
See the migration audit for final verification and landing status.

Managed coordinators receive activation and resume transitions into running after
their initial turn. This matters when plan approval and activation happen at
different times: an idle coordinator no longer needs a second chair message to
notice activation. Pre-existing activation is covered by the initial state read
and does not spend another turn.
