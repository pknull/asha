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
asha control session attach SESSION_ID
```

`show`, `attach`, `close`, `stop`, `resume` and `send` accept a full session ID
or a unique prefix of at least four characters, such as the eight that `list`
and the dashboard show. An unknown or ambiguous prefix fails naming the
session, and an ambiguous one lists the matching IDs; nothing acts on a guess.
Put the ID right after the verb.
`attach` and `close` still take an exact legacy Room name.

In a terminal, `attach` attaches: inside tmux on the Room's own server it
switches your client (`switch-client`), anywhere else it runs `attach-session`
(tmux itself refuses to nest inside another server). Both repeat the Room
ownership check first and attach nothing when it fails. `--json` keeps the
print-only result for scripts and the dashboard; `--print` prints the verified
tmux command instead of running it, as does any non-terminal caller.

`launch`, `send`, `stop`, `close`, `resume` and the other operator verbs, and
`asha room open|close`, refuse a worker-profile session on any harness and a
non-chair session on a sandboxed harness, such as a Codex Room (Keeper ruling
K4, 2026-10-05). Run outside the sandbox, they would let that session start
an unsandboxed one or type into another. The chair keeps them on every
harness: a Codex chair reaches them only through a sandbox escalation the
Keeper approves at the native prompt, which is consent. A Claude, Copilot or
OpenCode Room, which already runs with native permissions, and the operator's
own terminal keep them too. A session's own `report`, `handoff`, `messages`
and `ack-message` are unaffected. The refusal reads `ASHA_SESSION_PROFILE`
and `ASHA_HARNESS`, which every Asha launch sets; a plain `codex` started
outside the wrapper carries neither.

Project names resolve through Asha's existing index; canonical initialized
project paths also work. Ambiguous names require a choice. Ordinary jobs run
in that checkout, without a jj workspace requirement. The project's own
instructions and the user's native permission settings apply. A worker can
create its own worktree when its assignment calls for one.

### Model and effort

`--model MODEL` and `--effort LEVEL` choose the native model and reasoning effort
for one session at launch (the dashboard's `n`/`o` launch form has optional Model
and Effort fields; blank means the harness default). Asha never picks, routes or escalates a model itself; omitted
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

Session rows (`show`, `list`) carry `selection.model`/`selection.effort` as
`{requested, effective, provenance}`. Provenance is `reported` when the native
stream stated the value, `requested` when Asha passed a flag that nothing
reported back (all terminal sessions), and `unknown` when neither applies. A
reroute or fallback is recorded, never refused. A close keeps the selection
snapshot taken when it was requested, and a guidance exposure keeps it in its
manifest.

### Token use (#111)

`session show` and every stop or close read the worker's native records,
read-only, and store `usage` on the session row: token totals (`input`
uncached, `cache_read`, `cache_write`, `output`, and `reasoning`, which is part
of output), `cache_hit_ratio` (cache reads over all input), and the effective
`model` and `effort` where the record states them. Those also enter
`selection_reported` with source `native-record`, so terminal sessions stop
reading `requested`/`unknown` once a record exists. `show` adds a compact
`usage_line`; `list` and the dashboard present the stored value without
reading records (a `tokens` field, and a tokens column while any listed session
has known usage). Tokens only, never prices.

| Harness | Record | Notes |
| --- | --- | --- |
| Claude | `~/.claude/projects/*/<native_id>.jsonl` (`CLAUDE_CONFIG_DIR`) | `message.usage` counted once per `message.id` (one line per content block repeats it); top-level `effort`; `<native_id>/subagents/` transcripts are added to the totals |
| Codex | `~/.codex/sessions/*/*/*/rollout-*-<thread>.jsonl` or `archived_sessions/` (`CODEX_HOME`) | cumulative `token_count` totals restart when a thread is resumed into the same file, so each run's final total is summed; `input_tokens` includes cached input; `turn_context` model and effort; spawned subagent threads are not counted |
| Copilot, OpenCode | not checked yet | always `unknown` |

Every native conversation a session bound (`native_ids`, across resumes and
`/clear`) is summed. Records are reread only when their size or mtime changed.
A missing, oversized (over 256 MiB) or unparsable record leaves usage `unknown`
with a reason and never blocks a show, stop or close.

Plain workers do not trigger Asha's first-run configuration. Existing native
skills and hooks remain available; a harness without the Asha hooks can still
run its assignment, with activity shown as unknown until explicitly reported.

`asha control` opens the session dashboard. Enter attaches to a terminal or
opens a structured conversation; it is offered only where the hub would accept
it, so a closed or stopped terminal session offers `r` resume instead, and an
ended legacy Room offers no attach. `a` handles the selected input request;
terminal requests open the native harness. `n` starts a job and `o` a Room
through the project launch form (Project, Harness, Assignment or Topic, then
optional Model and Effort); `m` sends context, `s` stops, `x` closes gracefully,
`X` force-closes, and `r` resumes. `M` filters input requests and `A` includes
history. `q` exits the dashboard and leaves work
running. The footer is one line naming the keys that matter for the selected
row; `?` opens the full key sheet, which Up/Down pages through on a terminal too
short to show it whole. A resize while the sheet is open keeps it open at its
place, clamped to the new height.

Layout (#102 phase 2). The first line names the view and its grouping; the
second is the observation summary, or an attention banner (`▲ 1 needs input
◆ 1 approval — press ! to jump`) while a session that needs you is scrolled off
screen or inside a folded group. `!` selects the next such session, unfolding
its group. From 120 columns the list shares the screen with a side panel
showing the selected session's identity, next step, reason and facts; `Space`
hides or shows it. Narrower, the list takes the full width with a short detail
under it, and `Space` swaps to a full-width detail of the selected row (`Esc`
or `Space` returns). Each row shows a state glyph (`● working`, `▲ needs
input`, `◆ approval`, `✓ finished`, `… closing`, `○ idle`, `✗ failed`, `· ended`,
`? unknown`; ASCII `* ! # + ~ o x . ?` when the locale is not UTF-8), the
session name, the harness (from 60 columns), the next step and the time since it
last changed. That time never reorders rows. The model and effort selection is
shown in the detail and side panel, not on the row. A line under the row carries
close-request (requested time and deadline), background-task and staleness
facts when there are any. Names are clipped by terminal cells, so wide or
combining project names cannot overrun a column. The selected row is always on
screen: on a short terminal the narrow detail lends the list lines, and the
selected row's group heading, then its fact line, give way before the row does.

The side panel and the narrow full-width detail show the selected session's
facts only. The live preview of a session's screen (#102 phase 3) was removed
(Keeper ruling N9, 2026-10-07): the dashboard never captures a pane or shows a
session's output; attach (`Enter`) to see it.

Rows are grouped by project by default; `g` switches to grouping by state
(`Needs you`, `Working`, `Closing`, `Ready to close`, `Idle`), which follows the
presented next step, so a row's words and its group agree. Project names that
differ only in case form one group, titled by one spelling chosen from all its
rows, so the heading reads the same whether or not its finished rows are folded;
a row spelled otherwise names its project. Ended and retained history rows have their
own groups after the others and name their project. Left folds the selected
row's group to one heading that shows its count and how many of its sessions
need you; Right, or Enter on the heading, unfolds it. Row actions do nothing on
a folded heading.

When the list is taller than the screen, the dashboard folds automatically:
History, then Ended, then each group's finished rows (ready to close) into one
`… N more` line, from the bottom up, until it fits. A fold that would save no
line, such as a group's single one-line finished row, is skipped, so many small
finished projects keep their rows instead of trading each for `… 1 more`. Rows
that are working or need you are never folded, nor is the group holding the selected row; when a
refresh or an action moves the selected row into an automatically folded group
(it finished, stopped or left for history), that fold opens and the selection
stays on the row. Automatic folds are recomputed on every paint, so a taller
terminal opens them again; Right or Enter opens one for good (opening a whole
project group also keeps its finished rows open), and a group folded with Left
stays folded, hiding even the selected row.

The terminal title shows `N awaiting input · asha control` (input requests plus
approvals). Outside tmux it is on for terminals known to take a title escape
(xterm-compatible, VTE, kitty, foot, alacritty and similar, or a terminfo OSC
status line). Inside tmux the terminal must still qualify, and the title is off
unless `set-titles` is on for the dashboard's own tmux session (the effective
value, so a session override beats the global one; unreadable means off),
because the escape renames the dashboard's pane. That session is the one tmux
session holding the dashboard's pane (`TMUX_PANE`); when the pane is in several
sessions, as grouped sessions and linked windows make it, or its session cannot
be found, the title is off. It is always off in a Control-managed
session, whose pane title is Control's own evidence, and with
`ASHA_CONTROL_TITLE=0`. On exit the previous title is restored (the pane's
previous title inside tmux, the xterm title stack elsewhere).

The dashboard keeps its rows between refreshes (#102). It orders them itself by
group (current, ended, history), then by project (or by state section with
`g`), then rows needing input or approval, then creation time;
activity never reorders the list, and the selection follows its session. When the selected session leaves, the nearest
surviving row in the previous order is selected (the following one on a tie).
The selected row keeps its screen line, group headings and fact lines included.
`session list --json` keeps the hub's
recency order. A row missing from an incomplete observation stays, marked
`stale since HH:MM:SS UTC`, until a complete page shows it has gone. Keys do not
re-read the whole list: an action re-reads only its own row (a legacy Room or a
structured session the hub does not own waits for the next page), and `A`
re-reads because it changes the query. The re-read row obeys the same query as
the list: closing a session while history is off removes it, and a page that
started before the close cannot bring it back.

Refresh is change-driven (#102 phase 4, `session_refresh.py`):

- *Fast tick, every 250 ms.* The dashboard keeps one read-only connection to
  `control.sqlite3` open and reads `PRAGMA data_version`. If no other connection
  committed, it does nothing more: no query, no row read. After a commit it
  lists the sessions written since its cursors (hub rows by `updated_at`,
  read through a covering index; structured sessions by
  `session_events.sequence` and `managed_sessions.updated_at`) and re-shows
  only those rows, reusing the last terminal inventory (a row new to the view
  takes a fresh one).
- *Slow tick, every 5 s.* Some facts change because time passed or a process
  died, and nothing writes a row: liveness, `Hooks not reporting` (90 s after
  launch without a native event), staleness windows, legacy Rooms. The slow tick takes one bounded tmux
  inventory and re-reads the whole page, so these appear within 5 s plus one
  page read. Pages start 5 s apart, measured from each start; a page that
  outlasts the interval is followed at once, never overlapped.
- One worker thread does every read, one at a time, and returns row deltas. A
  failed row read marks that row `stale since …` and keeps it; the next page
  clears the marker. A change the dashboard cannot read as a single row (a
  managed session the hub does not own, a pruned row) brings the page
  forward, at most once every 2 s (the old refresh period).
- `A` pressed while a page is being read is no longer lost: the next page uses
  the new query (Q17-F6).
- The view writes nothing. Its cursors live only in the dashboard process: it
  consumes no event queue and acknowledges nothing.

Limits: the page summary counts (`N current; …`) change with the page, so they
can trail a row delta by up to 5 s; the title count and rows are current. The
optional `hub_events` timeline table from the design is not built.

The backend retains session identity and message records in SQLite. Terminal
ownership uses the existing verified Room/tmux adapter. There is no Redis
service, background terminal scraping, or initiative lifecycle on this path.
Launch, stop and resume serialize by session; a reused pane cannot be killed
or attached as though it were the old session. Existing Room, task and
initiative records remain intact; the retired task and initiative records are
read-only evidence ([Control](control.md)).

## Status and input

Claude and Codex hooks provide best-effort observations. OpenCode's generated
plugin reports session start, tool start/end and idle (as a guarded Stop)
through the same bridge; it is not live-proven, and OpenCode has no Stop block
seam, so its close requests are queued-only. Copilot's recovery hook file
reports session start, prompt submission, tool completion and session end the
same way; Copilot has no turn-end hook, so a finished turn reads working until
the next event or the staleness rules, and explicit reports remain the reliable
activity signal. Neither bridge is live-proven.
Missing or stale telemetry shows `unknown`; it never stops a worker. An idle
native turn is not a completed assignment. An explicit finished report or
successful structured utility yields `finished`.

A terminal row keeps two separate axes, and neither writes the other:

- **Observed** (`observed` in `show --json`), from native hooks and tmux
  liveness only: `launched` until the first native event, then `working`,
  `waiting` (a turn ended, or a permission prompt is open), `ended`, or
  `unknown` (tmux unreadable, or a working observation with nothing newer for
  five minutes).
- **Report** (`report`), from explicit reports and structured completion only:
  none, `needs-input` with its text, or `finished` with its text and time. A
  new report replaces it and `report --state working` withdraws it; a Control
  `send`, launch, resume and a new generation clear it. A native prompt clears
  only a needs-input report.

The dashboard words combine the two. A finished report reads `Finished, saved
HH:MM UTC` (or `Finished, unsaved`) as soon as it lands, unless the observed
turn still runs: a worker sends its report from inside its last turn and may
keep working after it (save, final test). While the observation is `working`,
or a `waiting` one older than the report, the row shows `activity: "working"`
with `reported_activity: "finished"` and the next step `Working: reported
finished` (#109). The report is **settled** only by a turn-ending Stop (one
that lists no background work) emitted after the report; a Stop emitted
before it, however late it applies, does not count. Only a settled report lets
a close terminate at once (D7, below). Harnesses without a turn-end event
never settle: Copilot (no turn-end hook) reads finished once its observation
is stale, and a close asks and waits. Structured sessions report finished at
their managed turn boundary and settle at once. Completed structured utilities
leave the current list; their results remain under `show ID` and `list --all`.

A finished worker can start another turn without new work: Claude wakes for a
background Monitor or task notification, and that turn reaches the hub as
`prompt-submitted` or starts with a tool call (#114). Such a turn leaves the
report standing but unsettled (`Working: reported finished`, a close asks) and
settles again at its own clean Stop. A prompt after a finished report only
marks `prompt_since_report`, so the assignment does not change and an earlier
save still counts. If the worker reports in that turn (working, needs-input or
finished), the prompt becomes a new assignment and the report replaces the old
one. A Control assignment (`send`) or a resume clears the report.

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
cwd. Only a SessionStart rebinds (F6), and only from inside the project: one
whose payload source is `clear` (`/clear` on Claude, and on Codex 0.120 and
later), or any SessionStart once the bound conversation has sent SessionEnd. A
nested `claude -p` run inside the pane starts with source `startup` and is
refused, as are its other events. Resume is a new generation and binds afresh.
The bound conversation may report from anywhere, because Claude's hook cwd
follows a Bash `cd` or EnterWorktree. A missed SessionStart after `/clear`
leaves the new conversation's events refused for the rest of that generation,
and a native subagent reporting under its own thread ID is refused too; both
are logged. Codex `/new` is not verified to report source `clear`; if it does
not, its new conversation is refused the same way. A foreign conversation that
starts inside the project before the session's own first event can still
bind. Refused hook events
are appended to `~/.asha/state/control/hub-rejected-events.jsonl` (mode 0600,
locked, trimmed in place to the newest half past 64 KiB) instead of being
discarded. A live terminal session reads `Starting` until its first native
hook event (observed `launched`). A live Claude or Codex session still without
one 90 seconds after launch reads `Hooks not reporting: attach`, with the
silent minutes in its reason; worker reports do not count as hook evidence.
Copilot and OpenCode sessions, whose bridges are not live-proven, read
`unknown` instead.
A terminal PermissionRequest makes the session `needs-input` (observed
`waiting`) with a one-line, 300-character summary of the request (tool and
command, path or URL) as its question, so Control shows what is being asked;
the next step is `Answer in terminal (attach)`. The next hook event clears it. Answering a terminal approval through `session permission`
is not supported: it would mean typing into the pane.
`asha doctor codex` fails when a running Codex daemon or its updater carries
`ASHA_HUB_SESSION_ID` (the executable must be Codex), or when a Codex that has
the flag would be launched without `--no-daemon`; it runs the real `bin/asha
codex` wrapper against a stub Codex in a scratch home with profile and resume
arguments, and notes recent refused events (a pane dying during
close also refuses its last hook, so the count is informational).

The dashboard derives a next step from these facts: `Finished, saved HH:MM UTC`
or `Finished, unsaved` for a finished live session (a finished report is never
gated on a save), `Done: close record` after its process exits, and `Ended
unreported: check work` for an exit without a current finished report. Idle Rooms say
`Waiting for you`; idle workers say `Stopped mid-task?`. A Room is an ongoing
conversation, so its Memory saves are checkpoints, not completion (#105): an
open Room with a save in its current assignment (an explicit save, or a
handoff that published Memory) says `Saved HH:MM UTC: waiting for you` and is
listed as idle, never as finished or under `Ready to close`. A Room that reports `finished` anyway is presented the same way while
it is open: JSON shows `activity: "idle"` with the added field
`reported_activity: "finished"`, and `saved_label` carries `saved HH:MM UTC`.
A Room that is closing or has ended keeps the close and ended hints below.
Input requests direct you to the terminal or Control, and a pending close says
`Closing: waiting for a save` or `Closing: saved`. Ended sessions occupy a separate group below current sessions until
closed. Raw activity, lifecycle, and observed process state remain in JSON.

A Claude turn can end while its own background work is still running: a
`run_in_background` shell, a Monitor, a background agent. Claude's Stop payload
lists that work in `background_tasks` (running or pending, backgrounded; an
empty array when nothing is in flight; verified on Claude Code 2.1.283). The
hook bridge forwards only the count, and the hub records such a Stop as
`working` with `background_tasks: N`, reason `Turn ended; waiting on N
background task(s)` and next step `Working: background tasks` (#99). It is not
an idle boundary: a close does not type its pointer there, and the five-minute
staleness rules wait instead. A pending Stop-hook close request is still
emitted at such a Stop, because a Stop block only continues the turn and never
interrupts the background job. The next hook event clears the count (a worker
report is the other axis and leaves it); the Stop that follows the wake-up
decides idle. The
wait is bounded: four hours after that Stop with no newer native event, the
usual staleness rules apply again (the row reads `unknown`).
Limits: only Claude reports this (Codex, Copilot and OpenCode Stops carry no
such field, so their turn end stays idle); a truncated or unparsed Stop payload
forwards no count and reads as the plain idle Stop; a process detached from a
foreground command (`cmd &`, `nohup`) is not Claude background work and is not
seen; if Claude exits without waking, process exit ends the session as usual.
A close still ends at its deadline. The wake-up Stop itself has not been
probed natively; the headless probe showed the field on the turn-ending Stop
only.

`Memory saved HH:MM UTC` requires a publication row (an explicit save, a
handoff or a `no-durable-update` attestation) in this generation; a later
assignment does not erase it. Worker result text does not establish a save or
code landing. Force-close keeps an existing save visible; it does not publish
another save.

Terminal messages sent with `session send ID --text TEXT --key UUID` are
retained until read (`delivery: queued-until-read`); nothing is typed into the
pane. Attach to provide interactive input, or let the worker read `session
messages` and acknowledge processed context with `session ack-message
MESSAGE_ID`. Pages expose
completeness and a continuation offset.

Worker launch, resume and send automatically supply up to three compatible active
learnings (3 KiB), ordered by project scope, harness scope, source-session evidence
count and rule ID. Repeated `--learning ID` selects explicitly; `--no-learning`
supplies none. Rooms keep their existing context behavior. Queued guidance becomes
supplied only through the existing delivery acknowledgement; supply is not use.
Candidate, retired, stale-version and incompatible selections are excluded with
their reasons. `show ID --json` lists each delivery's manifest under `guidance`
(selection, supplied rule versions, exclusions, delivery state). Terminal
`session messages` returns a `delivery_digest` for a retained emitted body;
acknowledge that exact body with `session ack-message MESSAGE_ID
--delivery-digest DIGEST`. An acknowledgement without a digest keeps ordinary
message handling and leaves guidance supply unknown; an unknown or evicted
digest is refused, so read again before retrying.

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

Reports, handoffs and message reads take their session from the environment
only (`ASHA_HUB_SESSION_ID` and `ASHA_HUB_GENERATION`): no session argument
selects it, and no Room marker or process-ancestry check proves it. The
generation and lifecycle fences stop a stale actor: a reporter from an earlier
generation, or of a closed or stopped session, is refused. Inside the Codex
sandbox Control state is read-only, so a report still succeeds only as the one
plain command its rules run outside the sandbox. Native hooks bound reporting
time and fail open when the hub is unavailable. A report is the report axis
alone, so the report command's own hooks never change it.

## Recovery and operation

`session stop ID` ends the owned execution now and retains history for
resume. `session close ID` is the best-effort close below. `session close ID
--force` is the same close with a zero wait: it terminates now without asking
for a save. Native harness exit is observed as exit, without guessing that the
assignment succeeded.

## Best-effort close

Control observes native sessions from outside (tmux panes plus hook reports),
so close does not try to prove that a session is quiet. It asks the session to
save project Memory, waits a bounded time, then terminates:

1. `close ID` records `closure = {request_id, generation, requested_at,
   deadline}` and moves the session to lifecycle `closing`. The wait is
   `--wait SECONDS`, defaulting to `control.close_wait_seconds` (60, at most
   600); `--force` is a zero wait and cannot be combined with `--wait`.
2. The request goes out through the seams that exist. A terminal session gets
   a queued hub message (read with `session messages`). A working Claude
   session also gets it as the harness's own Stop-hook block decision when its
   turn ends. An idle session, or one whose activity is unknown, gets one
   bounded pointer line typed into its pane naming the request and `session
   messages`; any other working session (Codex) gets that pointer once it is
   observed idle within the wait. At most one pointer is typed per request, and
   only by the close's own waiter after it verified Room ownership; a refresh
   never types. A session waiting at a native question or permission prompt is
   not typed into. A structured session gets the request as its next
   structured turn.
3. The close ends at the first publication or attestation recorded after the
   request (a handoff that names the request or not, or an explicit save), when
   the process ends, or at the deadline. A publication still in flight at the
   deadline gets 10 more seconds, then the session is terminated anyway.
4. Termination kills the Room even when a terminal is attached (the operator's
   close authorizes it; `--wait` extends it). A structured session is asked to
   stop; its store's own cleanup reconciles the provider, and the closed record
   does not claim a confirmed process exit.
5. The row reads `Closed, saved HH:MM UTC` or `Closed, unsaved`. The time is
   the newest publication in the generation, so an earlier save still shows,
   with its age visible; `unsaved` means the generation has none.

A session whose finished report is settled (a turn-ending Stop emitted after
the report, above; #109, #114) and whose current assignment already has a
publication, in either order, closes at once without a request (D7). Any other
finished report gets an ordinary close request, so a close never kills the
turn that is finishing. The documented worker
sequence is save, then `report --state finished`.

The request text asks the agent to save and end its turn:

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
Scope, identity and silence are checked before any write. A `no-durable-update`
attestation passes the same checks and counts as saved; `failed` or `blocked`
save nothing. Nothing on this path commits, pushes or integrates code; Git
publication remains the chair's separate, explicit decision. The handoff need
not be a standalone tool call, and `--attempt` is accepted and ignored for one
release.

A kill during a publication is recoverable, not pair-atomic: publication is two
file replacements under a recovery journal, and readers refuse with a recovery
command until `memory_v2.py recover` (or the next publication) restores the
pair.

The wait never blocks the dashboard: `x` records the request and starts a
detached `asha control session close ID` waiter, then returns; `X` is `close
--force`. The CLI waits in the foreground. Re-running `close` joins the pending
request (no second request or pointer) and finalizes it once it has expired.
If no waiter is left, any later `close` or `stop`, and every launch, send or
resume, finalizes expired closes; the cached dashboard refresh never does, so a
close is recovered when a command runs again, not terminated while nothing
runs. Finalization names its request and generation, so a stale waiter cannot
end a newer request. `stop ID` on a pending close ends it as stopped.

Every successful publication path records one `hub_memory_publications` row
naming its source: `explicit-save`, `close` (a handoff naming the request),
`handoff` (an ordinary handoff) or `attestation` (`no-durable-update`, with its
detail). "Saved" is read from these rows only.

Compatibility for one release: the retired `control.idle_delivery`,
`control.no_handoff_close` and `control.session_preview` settings are accepted
and ignored; hooks in live
Rooms that still pass `--order`, `--attempts`, `--tool-kind`, `--tool-token`,
`--sequence` or `--sequence-pane` are accepted and ignored; `report` and
`handoff` from workers briefed before session experience was retired accept and
ignore `--experience-file`, `--experience-ref`, `--supersedes` and `--key`; stale
`hub-event-order/` directories and `ASHA_HUB_EVENT_ORDER` in a Room's
environment are ignored. Close records from earlier versions (states such as
`acknowledged` or `closed-no-save-claimed`, attempts, `attention`) are read
only: they present through the same saved/unsaved label, a verified save they
recorded still counts, and `attention` no longer keeps a closed row on the
default page. A `closing` row from an earlier version has no deadline; run
`close` again to give it a request.

### Late hook reports

Hooks run as separate processes, so their reports can arrive out of order.
Each hook stamps when it fired (`--emitted-at`, from `date +%s.%N` on its
first line). The hub stores the newest applied stamp and skips a report whose
stamp is older by at most 30 seconds: none of its effects apply (activity,
background tasks, question, lifecycle), and a skipped Stop returns no close
decision. An equal stamp applies. A report more than 30 seconds older applies,
so a backward clock step cannot reject every event until time catches up. An
unstamped report (an older hook) applies. A new generation starts without a
stamp. Explicit `session report` calls are authoritative and bypass the check.

Reports that change nothing without being refused are counted, not discarded.
A skipped late report, and a bridge call that ran out of its budget (0.6 s on
tool, prompt and permission events; 3 s at the session-start, Stop and
session-end lifecycle boundaries), each append one line to
`~/.asha/state/control/hub-lost-events.jsonl` (reason `stale-skip` or
`bridge-timeout`, with event, session, generation, native ID and stamps; same
bound, mode and trimming as the rejection log). The bridge records a timeout
through a detached `session event-lost` call bounded at 20 s, so the hook
itself still returns within its budget; a timeout whose record also fails is
not counted. It is a loss metric only: nothing reorders, retries, waits on or
late-delivers a lost report. A lost Stop leaves a finished report unsettled:
the row reads `Working: reported finished`, then `Finished` once the
observation is stale, and a close asks and waits its bound instead of closing
at once. A pending close request is re-emitted at the next Stop.

## Retired: session experience capture

Session experience capture, review, adoption, dispositions, statistics, the
`session experience` command, the structured `--result-contract` envelope and
the native review gate were retired on 2026-10-07 (subtraction N2). Briefs and
close requests no longer ask for an assessment, and a `session_experience`
default in the user config is ignored. Records captured before then stay in
the Control database (`hub_experience*` tables); nothing reads or writes them.
Selected guidance (above) and the learnings it draws on are unchanged.
