# Project sessions

`asha codex` or `asha claude` opens Asha's conversational chair. Give it work,
discuss a project, or ask for a result. It can work directly, use native
subagents, or launch another project harness. Ordinary sessions create no
initiative and require no Asha plan approval.

| Use | Execution | Asha context |
| --- | --- | --- |
| Independent job | Native interactive harness in the project | Skills on demand; no persona or automatic memory |
| Project Room | Native interactive harness in the project | Asha personality and project memory |
| Short utility | Claude structured stdio or Codex app-server | Assignment and optional clarification tool |

```bash
asha control session launch --project termart --prompt 'Remove the games; retain status monitoring' --harness claude --json
asha control session launch --project termart --prompt 'Discuss the status display' --profile room --harness codex --json
asha control session launch --project termart --prompt 'Summarize open issues' --transport structured --harness claude --json
asha control session list --json
asha control session show SESSION_ID --json
```

Project names resolve through Asha's existing index; canonical initialized
project paths also work. Ambiguous names require a choice. Ordinary jobs run
in that checkout, without a jj workspace requirement. The project's own
instructions and the user's native permission settings apply. A worker can
create its own worktree when its assignment calls for one.

Plain workers do not trigger Asha's first-run configuration. Existing native
skills and hooks remain available; a harness without the Asha hooks can still
run its assignment, with activity shown as unknown until explicitly reported.

`asha control` opens the session dashboard. Enter attaches to a terminal or
opens a structured conversation. `a` handles the selected input request;
terminal requests open the native harness. `n` starts a job, `o` a Room,
`m` sends context, `s` stops, `x` closes gracefully, `X` force-closes, and `r` resumes. `M` filters input
requests, `A` includes history, and `G` opens legacy workflows. `q` exits the
dashboard and leaves work running. Help wraps to fit the terminal.

The backend retains session identity and message records in SQLite. Terminal
ownership uses the existing verified Room/tmux adapter. There is no Redis
service, terminal scraping scheduler, or initiative lifecycle on this path.
Launch, stop and resume serialize by session; a reused pane cannot be killed
or attached as though it were the old session. Existing Room, task and
initiative records remain intact. Their advanced UI is still available via
`asha control --initiatives`.

## Status and input

Claude and Codex hooks provide best-effort observations. Copilot and OpenCode
terminal workers currently need optional explicit reports for activity detail.
Missing or stale telemetry shows `unknown`; it never stops a worker. An idle
native turn is not a completed assignment. An explicit finished report or
successful structured utility yields `finished`. Completed structured utilities
leave the current list; their results remain under `show ID` and `list --all`.

The dashboard derives a next step from these facts: `Done: close` for a finished
live session, `Done: close record` after its process exits, and `Ended unreported:
check work` for an exit without a current finished report. Idle Rooms say
`Waiting for you`; idle workers say `Stopped mid-task?`. Input requests direct
you to the terminal or Control, and an idle undelivered close says `Close needs
attach`. Ended sessions occupy a separate group below current sessions until
closed. Raw activity, lifecycle, and observed process state remain in JSON.

`Memory saved HH:MM UTC` requires a controller-retained explicit-save receipt
for this generation and assignment, or a verified handoff in this generation.
Worker result text does not establish a save or code landing. Force-close keeps
an existing save receipt visible; it does not publish another save.

Terminal messages sent with `session send ID --text TEXT --key UUID` remain
queued until read. They cannot wake an idle harness and are never typed into a
pane. Attach to provide interactive input, or let the worker read
`session messages` and acknowledge processed context with `session ack-message
MESSAGE_ID`. Pages expose completeness and a continuation offset.

Worker launch, resume and send automatically supply up to three compatible active
learnings (3 KiB), ordered by project scope, harness scope, source-session evidence
count and rule ID. Repeated `--learning ID` selects explicitly; `--no-learning`
supplies none. Rooms keep their existing context behavior. Queued guidance becomes
supplied only through the existing delivery acknowledgement; supply is not use.

Structured utilities retain messages for eligible turn boundaries and expose
native permission/clarification requests through Control. They inherit the
harness's permission settings; Asha does not choose an automatic bypass.
Native prompts still require the user's decision. There is no advertised
mid-turn steering or consumption receipt.

Optional terminal reporting:

```bash
asha control session report --state needs-input --text 'Which chapter?'
asha control session report --state finished --text 'Updated the scanner; checks passed.'
```

The reporter checks the Room's ownership markers and process ancestry, plus
the hub session's generation. Environment labels alone cannot establish
reporter identity. Native hooks bound reporting time and fail open when the
hub is unavailable. An explicit report is retained across the report command's
own completion hooks.

With effective experience policy enabled, a finished report without an assessment
returns one bounded assessment request and controller key. Follow up with
`report --state finished --experience-file FILE --key KEY`; it preserves result
text and deduplicates retries. An unanswered request followed by exit records
`missing` / `exited-before-capture`. Structured and review utilities are excluded.

## Recovery and operation

`session stop ID` ends the owned execution now and retains history for
resume. `session close ID` is the graceful path: it asks the session's own
agent for one final turn and terminates only after a verified project-memory
handoff. `session close ID --force` terminates now, hides the session, and
records that no memory save was claimed. Native harness exit is observed as
exit, without guessing that the assignment succeeded.

## Graceful close and the memory handoff

A normal close records one bound request (`closure.request_id`, tied to the
session's incarnation `generation`) and moves the session to lifecycle
`closing`. The request text asks the agent to bring the current step to a safe
boundary, then acknowledge with one of:

```bash
asha control session handoff --read --json                       # live destination facts
asha control session handoff --request ID --active-file A --decisions-file D \
    --expected-active DIGEST --expected-decisions DIGEST --json   # publish
asha control session handoff --request ID --outcome no-durable-update --detail WHY --json
asha control session handoff --request ID --outcome blocked --detail REASON --json
```

Publication runs through the shared Memory v2 validator with a compare-and-swap
on both files: a digest that changed since the agent read it refuses the write,
so a newer save is never overwritten; the agent re-reads, merges and retries.
Control verifies the published bytes and records the destination and digests.
A valid explicit `no-durable-update` also satisfies the handoff. `failed` or
`blocked` outcomes are retained as `handoff-failed` and never become a
successful closure. Nothing on this path commits, pushes or integrates code;
Git publication remains the chair's separate, explicit decision.

Delivery is honest about each seam:

| Session | Delivery | Idle agent |
| --- | --- | --- |
| Terminal Claude | Queued message, and once as the harness's own Stop-hook block decision when the current turn ends | Cannot be woken; attach and hand it the request, or force-close |
| Terminal Codex | Queued while working; observed idle sessions with a native conversation ID use owned stop and native resume with the close request | Same conversation continues; unknown activity/missing ID/resume failure requires attach |
| Terminal Copilot/OpenCode | Queued message only (no Stop seam or supported native resume) | `unanswered`, attachment required |
| Structured Claude/Codex | The request becomes the next structured turn | Same path; the turn is scheduled by the supervisor |

The Stop decision is printed before delivery is recorded, so a hook killed at
its time budget re-emits the same request at the next guard-free Stop rather
than blaming the agent for a request it never saw; a Stop that follows a
delivered block carries `stop_hook_active` and emits nothing, so an unattended
worker may need another user turn or an attach. The seam needs `jq` in the
worker's PATH; without it the request stays queued. `closure.state` shows `pending-delivery`, `delivered`, `acknowledged`,
`unanswered` (the turn ended without a handoff), `handoff-failed`,
`undeliverable` (the harness exited first), `unavailable` (no live agent to
ask), `forced`, or `completed`. Re-running `close` is idempotent: it reuses
the request, re-asks after `unanswered`/`handoff-failed` with a fresh queued
copy, and terminates only once the state is `acknowledged`. `close --wait N`
polls for that acknowledgement first and returns early when the session needs
input. Dashboard `x` closes gracefully; `X` force-closes; the STATUS column
shows `closing` or `close-failed`, a native prompt or question during the
final turn still shows as `needs-input`, and the summary counts closes needing
attention. A close that terminated without a verified save (`unanswered`,
`handoff-failed`, `undeliverable`, `unavailable`) stays on the default page as
`close-failed` until the operator acknowledges it with `close ID --force`
(dashboard `X`) or `stop ID`, on a live or an already-closed row alike; an
explicit stop or force was the operator's own choice and needs no further
acknowledgement, and the closure evidence itself is retained. A terminal that exited during a
close keeps `exited` as its status with the closure guidance in its reason. The Stop decision is never emitted when the harness reports
`stop_hook_active`, so a block is never chained onto a block; a Stop payload
that is absent, malformed, or larger than the bridge's bounded read is treated
as if that guard were set, never as permission to block again, and the bridge
itself relays no block while the guard holds. Delivery is confirmed against
the exact request, attempt and incarnation the decision was emitted for, so a
late receipt for an earlier request cannot mark its replacement delivered.

The idle continuation keeps one close request ID across the new generation. Only
a recent native idle observation and verified owned process permit the wake;
an explicit `finished` report alone is insufficient. Working sessions are not
stopped. Failure leaves the request unanswered with `attachment_required` and
actionable guidance for the dashboard. These are fixture-tested seams, without a
paid native acceptance claim. No pane input or screen reads are used.

For a Room with a controller-retained explicit save in its current generation
after its latest assignment, close omits the experience assessment and records
`disabled` / `explicit-save-published`. A new assignment invalidates the omission.
This linkage does not authorize termination. [Issue #92](https://github.com/pknull/asha/issues/92)
owns verified completion readiness; `_close_once` marks its integration point
before any final-turn wake. Dashboard hints and ended-session grouping are
presentation only and do not grant completion or termination authority.
Legacy Rooms and
non-hub managed sessions have no handoff seam: `close` refuses without
`--force`. A verified publication whose follow-up verification read is refused
by another publisher's in-flight journal is still recorded as published, with
`verified: false` and the reason. Acknowledgements are verified by Room ownership and process
ancestry (terminal) or the managed-session anchor plus the running close turn
(structured); a stale generation or request is refused. Workers omit Asha's
automatic memory routines, so this final-turn request is the only memory work
a worker performs, and only on the operator's explicit close.

`session resume ID --text CONTINUATION` retains the hub ID and starts another
owned terminal incarnation. Claude/Codex resume the native conversation when
a native ID was captured. Otherwise Asha explicitly starts a fresh harness
with the assignment, last reported result and continuation context. It does
not claim to recover an unrecorded transcript. Terminal conversation history
is otherwise owned by the native harness.

Structured utilities use the existing supervisor and admission policy. Paused
admission retains new work without launching it. The owner exits between turns
and a later message can start another owner. Recovery after a failure or stop
requires inspecting `show ID` and providing its `recovery_digest` with
`resume ID --digest DIGEST --text CONTINUATION`; uncertain input is never
automatically replayed. Existing quota and turn-budget limits still apply.

Terminal jobs do not need the supervisor or the dashboard to keep running.
Closing either UI is separate from stopping work. This change does not resume
old initiatives, clear old questions, or migrate an existing registry backend.

## Optional session experience

[Session experience and reviewed learning](session-experience.md) documents user
defaults and project overrides, bounded report/close capture, save-time advisory
reviews, explicit-save dispositions, automatic guidance and coverage metrics.
Policy defaults to off; native automatic review remains gated pending separately approved probes.
Ordinary and scope-none Memory publication require both pre-draft snapshot digests;
close remains independent of successful capture or completed review.
