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

### Model and effort

`--model MODEL` and `--effort LEVEL` choose the native model and reasoning effort
for one session at launch (Dashboard `n`/`o` asks for both; blank means the
harness default). Asha never picks, routes or escalates a model itself; omitted
values pass nothing, so argv and app-server parameters stay exactly as before.
Values are validated before any record, pane or process exists: a model is one
printable argument without whitespace or a leading `-` (at most 256 bytes;
OpenCode needs `provider/model`), and the complete Room respawn argv must pass
the tmux transport check (no argument may be `;` or end with `;`); Claude effort is `low|medium|high|xhigh|max`,
Copilot adds `none|minimal`, Codex effort is model-dependent and checked by shape
only, and OpenCode's interactive TUI has no effort flag, so `--effort` is refused
there. The requested values join the session spec: relaunching the same
`--session-id` with a different selection is refused, and every resume passes
the same flags again.

| Harness | Terminal flags (before the prompt) | Structured seam | Reported back |
| --- | --- | --- | --- |
| Claude | `--model M --effort E` | same flags on `-p` stream-json | `system/init` model; effort not reported |
| Codex | `-m M -c model_reasoning_effort="E"` (after `resume ID` on resume) | `thread/start`/`thread/resume` model, `turn/start` effort | thread response `model`; its `reasoningEffort` only when no turn effort was requested (it is the thread default); `model/rerouted` updates the model |
| Copilot | `--model M --effort E` | none | none |
| OpenCode | `-m provider/model` | none | none |

Session rows (`show`, `list`) and experience envelopes carry
`selection.model`/`selection.effort` (envelopes: `model`/`effort`) as
`{requested, effective, provenance}`. Provenance is `reported` when the native
stream stated the value, `requested` when Asha passed a flag that nothing
reported back (all terminal sessions), and `unknown` when neither applies. A
reroute or fallback is recorded, never refused. `experience stats --model X`
attributes each event to the selection retained with it: a close to the
snapshot taken when it was requested, a report (and its completion capture) to
its envelope, a guidance exposure to its manifest. The effective model counts
when reported, the requested one otherwise; an event with no retained evidence
uses the session's requested model or `unknown`, never a later report. `models`
is that per-event breakdown with provenance; `current_sessions` separately
lists the live rows' current selection.

Plain workers do not trigger Asha's first-run configuration. Existing native
skills and hooks remain available; a harness without the Asha hooks can still
run its assignment, with activity shown as unknown until explicitly reported.

`asha control` opens the session dashboard. Enter attaches to a terminal or
opens a structured conversation. `a` handles the selected input request;
terminal requests open the native harness. `n` starts a job, `o` a Room,
`m` sends context, `s` stops, `x` closes gracefully, `X` force-closes, and `r` resumes. `M` filters input
requests, `A` includes history, and `G` opens legacy workflows. `q` exits the
dashboard and leaves work running. The footer is one line naming the keys that
matter for the selected row; `?` opens the full key sheet, which Up/Down pages
through on a terminal too short to show it whole.

The dashboard keeps its rows between refreshes (#102). It orders them itself by
group (current, ended, history), then rows needing input, approval or a failed
close, then project, then creation time; activity never reorders the list, and
the selection follows its session. When the selected session leaves, the nearest
surviving row in the previous order is selected (the following one on a tie).
The selected row keeps its screen line, group headings included.
`session list --json` keeps the hub's
recency order. A row missing from an incomplete observation stays, marked
`stale since HH:MM:SS UTC`, until a complete page shows it has gone. Keys do not
re-read the whole list: an action re-reads only its own row (a legacy Room or a
structured session the hub does not own waits for the next refresh), and `A`
re-reads because it changes the query. The re-read row obeys the same query as
the list: closing a session while history is off removes it, and a page that
started before the close cannot bring it back.

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

Hook identity is inherited environment, so every Codex TUI launch that Asha
owns (Rooms, native resume, and the `bin/asha` chair, coordinator and Control
paths) passes `--no-daemon` when the installed Codex documents it (0.157 and
later). Codex's shared `app-server --managed-daemon` otherwise runs hooks with
the environment of whichever process first spawned it, and every later
session's events report under that frozen `ASHA_HUB_SESSION_ID` (#100). A
remote TUI (`--remote`) and non-interactive subcommands are left alone, and a
Codex that does not document the flag never receives it. The wrapper follows
Codex's own grammar (value-taking options, variadic `-i`, `--`, and the first
positional), so a profile, conversation name or prompt that spells a subcommand
is still a TUI launch. As defence in depth the hub binds each generation to its
first native conversation ID: an ordinary tool, Stop or permission event from a
different conversation is refused whatever its cwd, including events without a
cwd. Only SessionStart (an explicit new conversation such as `/clear` or `/new`)
rebinds, and only from inside the project; resume is a new generation and binds
afresh. The bound conversation may report from anywhere, because Claude's hook
cwd follows a Bash `cd` or EnterWorktree. A missed SessionStart after `/clear`
leaves the new conversation's events refused for the rest of that generation,
and a native subagent reporting under its own thread ID is refused too; both
are logged. A foreign conversation that starts inside the project before the
session's own first event can still bind; process ancestry, which requires the
reporter to descend from the session's pane, is the remaining guard. Refused hook events
are appended to `~/.asha/state/control/hub-rejected-events.jsonl` (mode 0600,
locked, trimmed in place to the newest half past 64 KiB) instead of being
discarded. A live Claude or Codex terminal session with no native hook event 90
seconds after launch is labelled `Hooks not reporting: attach` (`telemetry:
hooks-not-reporting` in JSON); worker reports do not count as hook evidence,
and Copilot/OpenCode sessions, which have no hook bridge, are never labelled.
A terminal PermissionRequest makes the session `needs-input` with a one-line,
300-character summary of the request (tool and command, path or URL) as its
question, so Control shows what is being asked; the next step is `Answer in
terminal (attach)`. Answering a terminal approval through `session permission`
is not supported: it would mean typing into the pane (see #101).
`asha doctor codex` fails when a running Codex daemon or its updater carries
`ASHA_HUB_SESSION_ID` (the executable must be Codex), or when a Codex that has
the flag would be launched without `--no-daemon`; it runs the real `bin/asha
codex` wrapper against a stub Codex in a scratch home with profile and resume
arguments, and notes recent refused events (a pane dying during
close also refuses its last hook, so the count is informational).

The dashboard derives a next step from these facts: `Done: close` for a finished
live session, `Done: close record` after its process exits, and `Ended unreported:
check work` for an exit without a current finished report. Idle Rooms say
`Waiting for you`; idle workers say `Stopped mid-task?`. Input requests direct
you to the terminal or Control, and an idle undelivered close says `Close needs
attach`. Ended sessions occupy a separate group below current sessions until
closed. Raw activity, lifecycle, and observed process state remain in JSON.

A Claude turn can end while its own background work is still running: a
`run_in_background` shell, a Monitor, a background agent. Claude's Stop payload
lists that work in `background_tasks` (running or pending, backgrounded; an
empty array when nothing is in flight; verified on Claude Code 2.1.283). The
hook bridge forwards only the count, and the hub records such a Stop as
`working` with `background_tasks: N`, reason `Turn ended; waiting on N
background task(s)` and next step `Working: background tasks` (#99). It is not
an idle boundary: idle-pane typing refuses it, a delivered close request is not
marked unanswered there, a finalized handoff does not close the session while
that work runs (`Closing: background tasks running`, not `Close needs attach`),
and the five-minute staleness rules wait instead. A pending Stop-hook close
request is still emitted at such a Stop, because a Stop block only continues
the turn and never interrupts the background job. The next hook event or worker
report clears the count; the Stop that follows the wake-up decides idle. The
wait is bounded: four hours after that Stop with no newer native event, the
usual staleness rules apply again (the row reads `unknown`, and a pending close
needs attach or force-close).
Limits: only Claude reports this (Codex, Copilot and OpenCode Stops carry no
such field, so their turn end stays idle); a truncated or unparsed Stop payload
forwards no count and reads as the plain idle Stop; a process detached from a
foreground command (`cmd &`, `nohup`) is not Claude background work and is not
seen; if Claude exits without waking, process exit ends the session as usual.
A session that keeps a long-lived Monitor or server running never reaches a
quiet Stop, so its finalized close waits until that work ends, the bound
passes, or the operator force-closes. The wake-up Stop itself has not been
probed natively; the headless probe showed the field on the turn-ending Stop
only.

`Memory saved HH:MM UTC` requires a controller-retained explicit-save receipt
for this generation and assignment, or a verified handoff in this generation.
Worker result text does not establish a save or code landing. Force-close keeps
an existing save receipt visible; it does not publish another save.

Terminal messages sent with `session send ID --text TEXT --key UUID` are
retained until read. Only with the experimental `control.idle_delivery` setting
on (default off), when a Claude or Codex terminal session is at a verified idle
boundary (see "Typing at an idle boundary" below), Control also types one
line into its pane naming the message ID and the `session messages` /
`session ack-message MESSAGE_ID` commands; the message body itself is never
typed. The result's `delivery` field says `injected` or `queued-until-read`,
with the refusal reason in `delivery_detail`. Otherwise attach to provide
interactive input, or let the worker read `session messages` and acknowledge
processed context with `session ack-message MESSAGE_ID`. Pages expose
completeness and a continuation offset.

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

Without a qualifying completion receipt, normal close records one bound request (`closure.request_id`, tied to the
session's incarnation `generation`) and moves the session to lifecycle
`closing`. The request text asks the agent to bring the current step to a safe
boundary, then acknowledge with one of:

```bash
asha control session handoff --read --json                       # live destination facts
asha control session handoff --request ID --attempt N --active-file A --decisions-file D \
    --expected-active DIGEST --expected-decisions DIGEST --json   # publish
asha control session handoff --request ID --attempt N --outcome no-durable-update --detail WHY --json
asha control session handoff --request ID --attempt N --outcome blocked --detail REASON --json
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
| Terminal Claude | Queued message, and once as the harness's own Stop-hook block decision when the current turn ends | Typed into the owned pane at a verified idle boundary (`pane-injection`); an attached pane or unproven input line needs attach |
| Terminal Codex | Queued while working; typed into the owned pane at a verified idle boundary | Typed as for Claude. An attached pane or typed input needs attach; an unrecognised screen falls back to owned stop and native resume with the close request (recent idle and a native conversation ID required) |
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
ask), `forced`, `closed-no-save-claimed` (`close --no-handoff`, see below), or
`completed`. Re-running `close` is idempotent: it reuses
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

The Codex native-resume continuation keeps one close request ID across the new
generation. Only a recent native idle observation and verified owned process
permit the wake; an explicit `finished` report alone is insufficient. Working
sessions are not stopped. Failure leaves the request unanswered with
`attachment_required` and actionable guidance for the dashboard.

### Typing at an idle boundary (experimental, off by default)

Issue #96: an ongoing Room is idle whenever its user stops talking to it, so a
close that waited for a Stop would always need a keystroke. Control can type the
request into the idle pane, but this is **experimental and disabled by
default**. It is enabled only by the Control setting `idle_delivery` in the Asha
config (`~/.asha/config.json`, or `ASHA_CONFIG`):

```json
{"control": {"idle_delivery": true}}
```

With the default (`false`), Control never reads or types into a pane for close
or send: an idle Claude close stays `pending-delivery` on its Stop-hook channel
with `input_refusal: disabled` and the dashboard shows `Close needs attach`; an
observed idle Codex terminal keeps its native-resume close continuation (below);
`session send` answers `queued-until-read`. New Rooms get no input fence: no pane
counters and no attach hooks, and the `ASHA_ROOM_INPUT_FENCE` marker is set to
`0` in the Room session and unset for the harness process, so a marker left in
the tmux server's global environment cannot switch it on. The native hook
bridge makes its tmux call only for an exact `1`. Pending-close guidance
mentions typing only while the setting is on. Rooms created while the setting was off
refuse typing as `unfenced` if it is later turned on.

It stays off because the fourth adversarial review of #96
(`Work/reports/qa4-issue-96.md`) left these findings open:

- **P1:** an older Stop report that lands after a newer working or tool-start
  report restores `idle` (and clears newer open tools) while the hub keeps the
  newer event sequence, so a delivery can be authorised against out-of-order
  state. The recorded maximum sequence does not prove event order or that every
  earlier report landed.
- **P2:** the 1.5-second limit is checked before the final Enter command, which
  has its own five-second deadline; a delayed tmux server can execute Enter
  later than the stated bound.
- **P2 (conditional):** a native event whose hook cannot bump the pane counter
  (no `TMUX_PANE`, or a denied tmux socket) after the last hub read does not
  stop an Enter already past confirmation; the missing-pane case refuses only
  deliveries that start after the unsequenced report.
- Screen-check limits remain: a hard newline where a display wrap could fall
  reads as that wrap, and Codex's `[Pasted Content N chars]` binds only length.

The native Claude/Codex idle close probes have not been run either. The rest of
this section describes the mechanism when the setting is on.

When enabled, Control may type one line into a terminal pane it owns only when
every fact holds:

- the harness is Claude or Codex (Copilot/OpenCode are never typed into);
- the last native event is an observed idle Stop, the session is not waiting for
  input, and no native tool start is still open;
- exact Room ownership verifies, the pane is not in a tmux mode, and no client is
  attached to its session;
- a capture of the visible screen proves the input line empty: Claude's `❯` line
  between its two rules with nothing after the marker (placeholders count as
  text; vim NORMAL/VISUAL mode refuses), or Codex's `›` composer holding at most
  a dim placeholder, with no continuation line and the footer's `? for shortcuts`
  hint that Codex shows only while the composer is empty (native 0.157 captures;
  extended-colour SGR parameters are parsed as units, never read as dim);
- the Room carries its input fence (below) with valid counters and hooks;
- the hub row, read again after those probes, still shows the same idle
  boundary (generation, activity, native observation and work/assignment epochs)
  with no open tool, and it has recorded the pane's current event sequence.

Rooms are created with an input fence: two counters stored as options of the
owned pane itself (`show-options -p`), the most specific tmux scope, so no
window, session or global value can stand in for them. Each must be a canonical
ASCII integer below 1,000,000,000, where tmux arithmetic still counts exactly;
a missing, non-canonical or exhausted counter refuses as `unfenced`, never
wraps or freezes.

- The attach generation `@asha_attach_gen` is incremented by the Room session's
  `client-attached` and `client-session-changed` hooks, which name the owned
  pane. Any client attaching to or switching into the Room moves it, so an
  attach-type-detach cycle is seen even within one second
  (`session_last_attached` has one-second resolution and is not used).
- The event sequence `@asha_event_seq` is incremented by the native hook bridge
  (`control-event.sh`) with one bounded tmux call before it reports the event,
  and the report carries the new value and the pane it bumped. The hub accepts
  it only for the Room's own pane and records the highest value reported for
  that pane (a new Room pane starts over); a report without one (tmux
  unavailable, or a hook environment without the Room pane) makes it unknown. Delivery requires the recorded sequence to
  equal the pane's, so an event whose report is still running, or was killed at
  the bridge's time budget, refuses as `stale` until a later sequenced report
  lands. Delivery holds no lock that reports wait on.

The counters are read with the screen. tmux then requires exact ownership, no
attached client, no tmux mode, a Room window not linked into another session
(whose clients would see the pane without attaching) and both counters unchanged
in the command that pastes (bracketed when the harness asked for it) and again
in the command that presses Enter: an attach or a native event that began in
between refuses. Immediately before each of those two commands Control also
re-reads the counters and requires both attach hooks to be exactly the installed
commands, and it re-reads the hub row right before the paste and before Enter.
Paste to Enter must fit in 1.5 seconds, or Enter is withheld (`partial`). A
Room created before this fence refuses as `unfenced` and needs attach.

What this does not cover: a harness that begins work without running its hook
bridge (the sequence cannot move); hook environments without the Room pane
(`TMUX_PANE`), which leave every Codex/Claude delivery refused as `stale`; a
change to hooks or counters made through direct tmux server access in the
instant between Control's re-check and the guarded command; and processes that
drive the tmux server directly (for example `send-keys` from another client).
Room session hooks shadow global tmux hooks of the same names for that session
only.

Before Enter, Control captures the screen again and requires the whole input
region to hold exactly the pasted text. For Claude the region is the box between
its borders: the top border is the unindented rule directly above the `❯` line,
the bottom border is the next unindented rule of the same width, and any other
unindented rule between them is ambiguous and refuses (typed draft lines are
indented, so a rule inside a draft is content, never a border). For Codex it is
the composer from the `›` line to one blank line followed by a single block of
one to three footer lines that each set SGR colour or attributes (typed composer
text never does; a bare reset does not count) and are not paste placeholders;
any other layout, including a blank line inside a draft, an unstyled trailing
block or a missing footer, refuses. Every line of the region must be part of the
text: the prompt line is the marker and one space, each continuation line the
two-space indent, and only the single space at a display wrap may be missing or
begin the next line. No other whitespace is normalised (tmux captures carry no
blanks at a line end). Limits of this screen check: without wrap metadata a
hard newline exactly where a display wrap could fall reads as that wrap; a
Claude paste placeholder (`[Pasted text #N ...]`) cannot be bound to this paste
and refuses, leaving the request unsubmitted in the input line (`partial`);
Codex's `[Pasted Content N chars]` is accepted only as the whole composer and
only with the typed length, which binds the length, not the content. A screen
read cannot prove the composer at the instant of the keypress; the counters
checked by tmux in the Enter command are what close that gap.

The typed close request is the exact retained request, flattened to one line
and bound to its request ID and `--attempt N`. A delivered attempt is never
typed twice; after `unanswered`, the next `close` re-arms and types the next
attempt, and a re-armed request accepts only a handoff naming that attempt.
Refusals are typed and recorded as `input_refusal`: `disabled` (the setting
is off; nothing is read or typed), `attached`, `mode`,
`ownership`, `unfenced`, `occupied`, `stale` (new or unrecorded native
activity), `partial` (typed but not submitted; the text stays in the input line)
or `error`. Each leaves the request pending with
`attachment_required` and never restarts the Room. Only `disabled`,
`ineligible` (no idle boundary, open tool) and `unknown` (no input line
visible) leave the Codex
native-resume fallback available, and that fallback kills the Room only through
a tmux condition that also requires no attached client and no mode. A typed
request counts as fresh native evidence for 300 seconds. No other pane input or
screen read is used. These are fixture- and tmux-tested seams; the native
Claude/Codex idle close probes in #96 have not been run.

For a Room with a controller-retained explicit save in its current generation
after its latest assignment, close omits the experience assessment and records
`disabled` / `explicit-save-published`. A new assignment invalidates the omission.
Publication linkage still proves only the save. The separate completion receipt
below supplies termination authority. Legacy Rooms and non-hub managed sessions
have no handoff seam: `close` refuses without `--force`. A publication whose
follow-up read is refused remains a historical successful publication; unavailable
or changed current Memory cannot authorize completion. The actor is verified by
Room ownership and process ancestry (terminal), or the managed-session anchor and
running turn (structured). Workers read project Memory at startup and finalize
before explicit completion, through the project-memory skill; automatic chair
context and transcript processing remain excluded.

## Completion before close

A successful explicit `memory_v2.py publish` (including `save_none.py`) inside a
verified hub actor with a sole observed standalone finalizer returns
`completion.status=ready` with a controller-produced
`asha.session-completion.v1` receipt. The no-Git handoff can also finalize before a
close request exists:

```bash
asha control session handoff --read --json
asha control session handoff --outcome no-durable-update --detail 'Reviewed with no durable change' --json
asha control session handoff --active-file A --decisions-file D --expected-active SHA --expected-decisions SHA --json
asha control session report --state finished --text 'Verified result' --json
```

Finish other tools before handoff. Use one shell command with literal arguments,
without shell composition, expansion or redirection. The matching native tool-end
event must arrive before a finished report is accepted; `ready` in the command
response alone does not prove that boundary. A standalone finished report preserves readiness;
subsequent work requires a new handoff. Saves can publish successfully while
completion retention fails; inspect both statuses. `blocked`/`failed` never satisfy
completion. An unavailable, silenced, mismatched or unauthorized Memory plane must
be reported as blocked, not no-durable-update. No handoff commits or pushes.
Explicit session-save retains only its separately authorized Git behavior.

Receipts live in the existing Control record, not a new Memory store. They bind
project ID, hub session and generation, assignment/work epochs, structured turn
and owner generation where applicable, both publication digests, and close request
and attempt when responding to close. New prompts, queued work, tools and resume
invalidate readiness. Close checks current Memory under its publication lock and
serializes terminal observations through the owned stop boundary. Concurrent saves
use the existing pre-draft CAS; a superseded save remains historical publication
evidence but cannot close the session. The controller never accepts a submitted
JSON receipt as authority.

`finish -> save -> native Stop -> close` consumes a qualifying receipt without
attachment, wake, a model turn, or force-close. `Finalized: close` and `Finalized,
closing` describe verified readiness; stale or missing evidence remains visible.
An idle request that cannot be typed (see above) says `Close needs attach`. A close already pending
requires `--request ID --attempt N` from the delivered request, not an old selector.
If Stop was not observed and the last native activity is over 300 seconds old,
both queued and acknowledged closes require attachment. A verified idle boundary
remains valid without further work; an idle receipt does not expire merely with age.

Receipt state is shown per session so the operator can see in advance whether a
close needs a turn (#101). `session list/show --json` add, under
`completion_readiness`, `receipt` (`current`, `stale` or `none`), `finalized_at`,
and `stale_since`: the first hub write at which the receipt stopped matching the
session's work (new prompt, tool, queued message, resume or changed close
request), or, when only published Memory changed, the later of the Memory files'
and the silence marker's modification times. The time is `null` when neither is
known. A receipt whose finalizer tool end has not yet arrived is `current`. The
dashboard's detail line reads `receipt current`, `receipt stale since HH:MM UTC`
or `no receipt`; a current or stale receipt is also shown on the session's row.

### Closing an idle session without a handoff turn

**Experimental and off by default (#103).** `close --no-handoff` is enabled
only by the Control setting `no_handoff_close` in the Asha config:

```json
{"control": {"no_handoff_close": true}}
```

The reason is QA11's Q11-F1: a hook that has appended its attempt byte but has
not yet taken a number, or not yet recorded that it failed to, is counted by an
older Stop's size sample. A close made while that hook is still in flight
accepts the boundary and kills the terminal, so its work is lost unseen (see
invariant 4 below). With the setting off (the default), `close --no-handoff`
refuses with a message naming the setting and #103, kills nothing, and changes
nothing; `session show/list --json` report `no_handoff.eligible: false` with that
reason; the dashboard neither lists `c` nor suggests it. Plain `close`, the
receipt close, `--force` and the Codex idle native-resume close are unaffected
by the setting. With it on, the behaviour below applies.

`close ID --no-handoff` (dashboard `c`) asks for no final turn. A current receipt
still closes as `completed` through `completion-receipt`, exactly as `close`.
Otherwise a terminal Claude or Codex session stops only at a verified native idle
boundary: the last native event is a Stop (or session start) with no open tool,
no outstanding background work (#99), no pending native question or permission,
and no worker `needs-input` report. It closes as `closed-no-save-claimed` with
delivery channel `no-handoff`. That state is distinct from `completed` (a
verified receipt) and `forced` (no boundary). It never claims a Memory save and
never needs attention. It queues no message, types nothing and resumes nothing;
any earlier verified handoff stays in the record as evidence only.

The boundary is re-read under the session's observation lock after the tmux
liveness probe, so a prompt that lands meanwhile refuses the close. The kill
is conditional inside tmux on no client being attached, so a person at the pane
(with a possible composer draft) refuses it too. A working session, a pending
question, an attached client, an unobserved session and an unproven event order
(below) are refused with the reason, and nothing is stopped. `--wait N` polls up
to N seconds for the boundary, then refuses with the last reason. `--force`
cannot be combined with it. Structured sessions, Copilot and OpenCode (which have
no native idle bridge) refuse. A harness that is no longer live closes with the
same honest state. A turn whose start hook never fired at all remains invisible
to it, as it is to the receipt close. `session show/list --json` report
`no_handoff: {eligible, reason}`, computed by the same predicate the command
applies, over the stored facts and the incarnation's event counter. With the setting on, when a pending close finds an
eligible boundary without a receipt, the next step reads `Close: attach or
--no-handoff` instead of only `Close needs attach`.

Every graceful close that terminates a live terminal, including the receipt path
of a plain `close`, uses the same tmux-conditional kill. An attached client
refuses it, the receipt is kept, and `--wait` treats the refusal as transient.
Before #101, a current receipt killed an attached session.

### Native event order and the turnless-termination invariant

Hook reports are independent processes and can arrive out of order, late,
twice, or never. Each hub incarnation gets a private counter file
(`hub-event-order/<session>/<generation>` under Control state, 0600 in 0700
directories, passed to the Room as `ASHA_HUB_EVENT_ORDER`). `control-event.sh`
takes the next number from it under `flock` when the hook starts, with no tmux
call, and forwards it as `--order`. The hook refuses a counter that is a symlink,
another user's, readable or writable by others, or not exactly one canonical
number; it then reports unsequenced. Unsequenced reports therefore arise only
from failures (lock timeout, bad or missing counter, a Room launched before this
change). The hub creates the counter and refuses to reuse an invalid existing one.

Before taking a number, every hook appends one byte to the incarnation's
attempt log (`<counter>.attempts`, created empty and 0600 beside the counter):
`O_APPEND`, no lock, so a hook that then times out on the counter lock, or whose
report is killed at the controller budget or lost, still leaves evidence. A
numbered report also forwards the log's size as `--attempts`, read under the
counter lock before the number is taken: appends are unlocked, so a size read
after the increment could count a later hook whose number and report were then
lost. A hook that gets no number appends a second byte, so a hook already
counted by a concurrent Stop still moves the size past that Stop's count. The
hook refuses a non-private or symlinked log (no attempt, no number) and stops
appending at 4 MiB (a soft bound: concurrent hooks may overshoot it by a few
bytes).

A full attempt log refuses every turnless close for the rest of that
generation: no later Stop, prompt, `/clear` or fresh handoff resets it, and the
log is never rotated. Recover by starting a new generation or forcing the
close: `asha control session stop ID` then `asha control session resume ID`
(the new generation gets an empty counter and log; a pending graceful close
must be resolved first), or `asha control session close ID --force`, which makes
no save claim. Attaching and exiting there also ends the session.

The hub applies an event's activity, tool and background effects only when its
order is newer than the last applied one. A late or duplicate report is kept in
a bounded `observation_log` without those effects, and the CLI gives an ignored
Stop no close-request decision or delivery confirmation. Numbers skipped by a
newer report are kept in `event_order.missing` until their reports arrive.

One invariant governs every termination without a turn: the receipt close
(plain `close` or `--no-handoff`), `close --no-handoff`, and the Codex idle
native-resume close. The kill is authorized only while the counter's `flock` is
held, inside the session's observation lock, through the stop itself, and only
when all of the following hold:

1. The counter's allocated value equals the applied order. A hook that took a
   number but has not reported, whether slow, killed or lost, refuses the kill.
   No hook can take a number during the kill; one that tries reports unsequenced
   to a session that is gone.
2. The last applied event is a Stop, with no open tool, outstanding background
   task, pending question, or explicit `working` report.
3. No unsequenced or out-of-order evidence stands. An unsequenced, late,
   duplicate or gapped report sets `event_order.barrier` to the counter's value
   when it arrived. Only an applied Stop numbered above the barrier clears it,
   because only that Stop is known to have been allocated after the evidence. A
   new generation also clears it. An already-allocated Stop never clears newer
   unordered work. If the counter cannot be read when the evidence arrives (a
   lock timeout, most often), `event_order.barrier_pending` is set instead; the
   next report that reads the counter fixes the barrier at its value then, so a
   later turn's Stop recovers within the same generation.
4. Every hook that started is accounted for: the attempt log's size now equals
   the size the last applied Stop reported. Hooks that appended before that Stop
   took its number are covered by it, as in 3; any hook appending after it,
   numbered or not, refuses the kill until the next Stop. A Stop that could not
   report the size refuses too.

**Known gap (Q11-F1, #103): invariant 4 is not fully proven.** The Stop counts
bytes, and a hook's first byte is written before that hook takes its number.
A hook still between that append and its allocation, or between a failed
allocation and its failure byte, is therefore covered by an older Stop, and a
close made in that window kills the terminal while the hook's work is
unreported. The window is small (a hook's own scheduling between two
consecutive steps) but real, and it is reproducible with scheduling gates. It is
why `close --no-handoff` is off by default. **The receipt close path (plain
`close` with a current receipt, and `--no-handoff` when enabled) shares this
window.** It
checks the same invariant. Sampling before the number (QA10) reduced the window
but did not close it. The receipt close is still strictly safer than before
#101, which applied none of these checks.

A completion receipt records the applied order at issue (`order_applied`). It
is current only while no report numbered after that order is missing or still
in flight. At most 64 missing numbers are kept (the newest); the highest number
dropped is kept as `event_order.missing_dropped`, which refuses only receipts
issued before it. Work numbered after it invalidates the receipt even when its report
arrives late and is otherwise ignored. Work here means a prompt, a tool start,
or a tool end other than the finalizer's; report-tool events are not work. The
finalizer's own end, report-tool events and the closing Stop necessarily follow
the receipt and do not invalidate it.

Availability costs are deliberate. A missing or corrupt counter, or a Room
launched before this change, refuses turnless closes until the session is
resumed (a new generation gets a fresh counter and attempt log). Out-of-order
evidence, or a hook that started without a number, refuses them until the next
turn's Stop. `close` then asks for a handoff turn; attach or
`--force` remain available. This is always on and independent of
`control.idle_delivery`; the typing fence's pane sequence is unchanged.
Allocation order approximates native order: two hooks that start within the
same instant can still take numbers in the opposite order to their native
events, and no Control-side sequence can detect that.

| Harness | Startup/completion instruction and skill | Receipt production | Close finalized idle session | Evidence/limits |
| --- | --- | --- | --- | --- |
| Claude terminal | Yes, worker and Room assignment | Explicit save / no-update / draft handoff | Yes, observed Stop or proven ended process | Controller/hook fixtures; native finish-save-idle probe not run |
| Codex terminal | Yes | Same, subject to native sandbox approval | Yes, observed Stop or proven ended process | Controller/hook fixtures; PreToolUse does not cover every unified_exec/tool path; native probe not run |
| Copilot terminal | Yes | Memory publication supported; completion blocked without tool bridge | Needs attach; verified finished report unsupported | No native Control tool/idle bridge; fixtures assert refusal |
| OpenCode terminal | Yes | Memory publication supported; completion blocked without tool bridge | Needs attach; verified finished report unsupported | No native Control tool/idle bridge; fixtures assert refusal |
| Claude structured | Yes, each hub turn | Verified managed CLI/save | After exact successful turn, with no queued work | Separate controller fixtures; native permissions can block the CLI |
| Codex structured | Yes, each hub turn | Verified managed CLI/save where native sandbox permits | Same controller contract | Separate fixtures; no out-of-sandbox publication proxy; native delivery not proven |
| Copilot/OpenCode structured | Unsupported | Unsupported | Unsupported | No managed transport |

Hooks remain bounded and fail-open telemetry, not a complete enforcement boundary.
No classifier grants native execution approval. Tool payload reads are bounded to
256 KiB and retain only classification and opaque identity. A finalizer is one
plain argv: shell metacharacters are allowed inside quotes (single quotes fully
literal; double quotes without `$`, backquote or backslash) and refused outside
them. Claude's `PostToolUseFailure` ends a start exactly like `PostToolUse`.
A native Stop ends every tool of its turn: unmatched starts are dropped then, and
a sole matched finalizer whose end callback was lost counts as ended. Missing,
malformed or oversized callbacks otherwise block finalization. A new prompt after
an observed idle boundary clears abandoned tool tracking and invalidates old
receipts; finalize anew. A `handoff-failed` status names the refused completion
evidence when the agent's own outcome was a valid acknowledgement. A new
structured turn or terminal incarnation also resets tracking. Do not treat scripted
fixture results as native-model proof.

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
