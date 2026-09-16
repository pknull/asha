# Advanced Asha Control

This guide covers the retained Room and task plane, opened with
`asha control --initiatives`. The default session launcher and dashboard are
described in [Project sessions](session-hub.md). The delivered proposals
preserve the advanced workflow design history.

## Purpose and prerequisites

Control supports persistent Rooms for fluid project collaboration and isolated
tasks for bounded agent work. A task owns
a versioned registry record, an explicit-base jj workspace and change, a
detached tmux session, and its harness runs. Closing the TUI, popup, or shell
does not end the task.

The source must already be an initialized Asha project and the exact root of a
Git repository. An existing Git-backed jj repository is used as-is; a plain
Git root is automatically colocated before task preparation. Control targets
the jj 0.38 workspace surface and requires tmux with `display-popup`, plus the selected installed harness. The
selected revision must positively ignore each task-private `.asha/`, `Memory/`,
or `Work/` path that Control will create. A regular context file already tracked
by that revision is reused byte-for-byte and does not need an ignore rule.
Fresh task preparation also requires immutable ignore coverage for
`/.asha/result.json` and the whole `/.asha/outbox/` directory, with no tracked
transport contents. Before launching a harness, Control authenticates the exact
new jj registration, add/checkout operations, selected tree, and owned no-follow
directory identity. It makes only that freshly materialized `.asha` directory
mode `0700` **before** recording immutable ownership, then creates the fixed
mode-`0700` outbox through context ownership callbacks. This works under ordinary
umask `0002`; tracked file bytes and modes are not changed. The sidecar, context
journal, and final prelaunch verification retain the resulting inode/mode facts.

This is not a permission repair API: generic context provisioning still reuses
tracked directories unchanged, and existing/sealed/foreign workspaces are never
privatized by this path. A failed or interrupted preparation never launches;
v2 recovery retains its workspace and registration for inspection. Recovery
may adopt only its already-supported exactly authenticated creation shape;
nonprivate retained context is refused rather than chmodded, and published
ownership is never rewritten to fit a later mutation. Follow the refusal's
inspection/cleanup guidance and start a fresh task when adoption is unsafe.
GitHub source modes additionally
require an installed, authenticated `gh`; ordinary ad-hoc tasks do not.

`asha task doctor` reports these local capabilities. Its `gh` probe is always
shown but is optional and never blocks ad-hoc task creation. Running it outside
a repository is also informational. Hook checks cover only the installed Claude
and Codex configurations, and live-event checks skip Copilot and OpenCode runs
because those harnesses claim process liveness only.

## Command surface

```text
asha room open NAME --project PROJECT --harness H --prompt TEXT [--json]
asha room list [--json]
asha room attach NAME|UUID [--json]
asha room close NAME|UUID [--yes] [--json]

asha task start [--repo PATH] (--pr N | --issue N | [--base REVSET])
                [--task-id UUID] [--slug SLUG]
                [--harness H|--agent H] (--goal TEXT | -- TEXT...)
                [--role ROLE] [--detach] [--headless] [--json]
asha task list [--json]
asha task show <task-id|exact-slug> [--json]
asha task attach <task-id|exact-slug> [--run RUN_ID]
asha task stop <task-id|exact-slug> [--terminate]
asha task archive <task-id|exact-slug>
asha task unarchive <task-id|exact-slug>
asha task recover <task-id|exact-slug>
                  [--adopt --yes --harness H --role ROLE --goal TEXT]
asha task prune (<task-id|exact-slug>... | --all) [--keep-workspace]
                [--dry-run] [--yes] [--json]
asha task reconcile [task-id|exact-slug] [--json]
asha task doctor [--json]

asha control --initiatives
asha control tmux
asha control event ...       internal hook-facing route
asha control supervisor {run|start|stop|pause|drain|resume|status} [--json]
asha control supervisor {install|uninstall} [--dry-run] [--json]
```

`--repo` defaults to the jj or Git repository containing the current directory.
`--harness` defaults to Asha's configured harness; `--agent` is its CLI alias.
Without `--detach`, a start inside tmux opens the new session in a popup. From
outside tmux it prints the exact attach command. `--json` keeps stdout to one
versioned machine-readable result, implies `--detach`, and includes the exact
attach command and an `existing` boolean in its payload.

Control-managed Codex launches pass a per-launch trust override for the
workspace root so a new task does not stop at Codex's directory-trust prompt.
The override applies only to that process and never edits the Codex trust store
or `~/.codex/config.toml`. Coordinator launches (`ASHA_COORDINATOR_LAUNCH` in the
pane environment) receive the same trust override for the projects root plus
an unattended posture — `-a never --sandbox danger-full-access` — so a
Control-launched Codex coordinator can run the `asha initiative` verbs
without stalling on approval prompts. Full access is deliberate, not
convenience: every Codex sandbox mode short of it runs commands in a PID
namespace and refuses the tmux socket, which makes the coordinator's own
pane and server proofs impossible from inside (verified live 2026-08-25).
The coordinator is the operator's persona-trusted agent running no foreign
code; workers keep their full sandbox.

Exit codes: `0` success (and, for `task doctor`, all required checks matched);
`1` when doctor checks complete with `ok:false`, or on an internal error; `2`
usage/refusal; and `130` interrupted.

A goal is mandatory in every mode and is the only instruction authority.
`--pr` and `--issue` provide source context, never a prompt. `--pr` conflicts
with both `--issue` and `--base`. `--issue` may be paired with `--base`; without
one it uses the same omitted-base policy as ad-hoc work. Exact Git first uses
the current attached local branch. With a detached or unborn `HEAD`, it next
uses remote symbolic `*/HEAD` targets, then conventional local
`main`/`master`/`trunk` refs. A fallback tier is accepted when all candidate
names resolve to the same immutable commit; differing OIDs require an explicit
base.
For the first start from a plain Git root, an explicitly supplied ad-hoc or
issue `--base` must resolve through exact, config-sanitized Git as one commit;
Control carries that immutable object ID through colocation and never
reinterprets the original text as a jj revset. Existing valid jj repositories
continue to accept arbitrary jj revsets. Omitted-base resolution is shared by
existing jj and first-time plain-Git starts and runs before import or colocation
mutation. Ref names are retained only as transaction/preview evidence: the task
records the legacy omitted request identity as `requested_base` and the
selected OID as `base_commit_id`. Thus identical caller-ID replay matches before
preflight, while an explicit different base does not alias it. A valid attached
branch such as `dev` is a default candidate; a detached repository with missing
or conflicting fallback candidates refuses before mutation and requests an
explicit `--base`.

## Rooms: dynamic project sessions

A Room is a detached tmux session running the full Asha persona and operational
layer directly in one initialized Memory v2 project's canonical directory. It
creates no Git or jj workspace and has no plan, seal, integration, TTL,
archive, reopen, or `/session:save` lifecycle. Detaching leaves it running;
only an explicit confirmed close ends its exact-owned tmux session.

An exact existing `PROJECT` path is validated directly, including outside the
configured roots. Friendly names, directory names, and `project_id` selectors
resolve case-insensitively through the configured multi-root project index and
must select exactly one project. Rooms do not require Git or jj.
`H` is one installed `claude`, `codex`, `copilot`, or `opencode`; their prompt
forms are positional for Claude/Codex, `--interactive PROMPT` for Copilot, and
`--prompt PROMPT` for OpenCode. The `ASHA_CLAUDE_CMD`, `ASHA_CODEX_CMD`,
`ASHA_COPILOT_CMD`, and `ASHA_OPENCODE_CMD` executable overrides are honored by
both CLI/TUI preflight and launch. `open` is detached and returns immediately
with the UUID, tmux identity, and exact attach command.

Room records live under `${ASHA_HOME:-~/.asha}/state/control/rooms/` and retain
only a digest of the opening prompt. Attach and close revalidate the recorded
immutable tmux session/pane IDs and UUID/project markers in the same tmux
server action; a readable tmux name is never ownership evidence. Missing or
ended owned Rooms remain safely closable, while a foreign collision is reported
as `mismatch` and never killed. `close --json`
and other non-interactive closes require `--yes`; the TUI requires exact
lowercase `yes` every time.

Multiple Rooms may share a project checkout. The CLI and TUI mark every live
member `shared working tree`; concurrent edits are therefore immediately
visible and need human coordination. Use a Control task or initiative when
isolation is required.

## Terminal TUI: one control tree

`asha control --initiatives` opens a single tree in the current terminal: an expanded
**Rooms** branch when at least one durable Room record exists, the current
initiatives (expandable to its nodes and attempts, each showing its linked
worker's live state inline) followed by an **Unbound tasks** branch holding
Control tasks bound to no initiative. `H` widens the middle branch to every
retained head the refresh loaded — see *Current and All retained initiatives*
below. With no initiatives on screen the branch flattens and the tree is
exactly the task list. Keys act on the selected row's kind:

| Key | Action |
|---|---|
| `Up`/`Down`, `Right`/`Left` | Move; expand or collapse (`Left` on a child returns to its parent). |
| `Enter` | Room row: attach it. Initiative row: attach its coordinator. Node/attempt/task row: open the worker popup. |
| `!` | Show only rows waiting on a human (plan approvals, needs-input, requested approvals, workers at prompts, published-awaiting-exit). A head's expansion is not a filter here: a node row waiting on you is listed with its head whether or not the head is expanded in the normal tree (attempt rows still follow their node's expansion; the node row already carries its latest attempt's ask). A paused initiative is status, not demand: its head and its parked node decisions leave this filter until it resumes, while a worker still at a prompt, an attempt awaiting exit, or a requested approval beneath it stays, collapsed or not. |
| `n` | New intent: Control starts a coordinator session at the projects root with your intent as its first message. |
| `o` | Open the Room form: enter an exact initialized project path or choose an indexed project, then set its name, installed harness, and opening prompt. Launch stays detached. |
| `N` | Open the ad-hoc task-start form. |
| `X` | Room row: after exact `yes`, kill only its exact-owned session. Worker row: after `yes`, send its quit command as your keystroke. |
| `a` | Initiative row: decide a pending plan approval (`approve`/`reject`). Task row: archive after confirmation. |
| `x` | Task row: controller-revalidated context actions. |
| `r` | Initiative row: reconcile it. Task row: reconcile the task. |
| `d` | jj diff summary of the selected row's linked workspace. |
| `e`, `c`, `v`, `t` | Initiative panes: events, candidate seals, review + verification evidence, retained storage. |
| `p` / `s` | After exact `yes`: `p` parks a running or needs-input initiative, or resumes a paused one (back to `needs-input` when the park left an operator question unanswered, otherwise `running`; the result line names the state); `s` stops the selected attempt's task. Anything else records nothing. |
| `A` | Toggle the `active` / `all` lifecycle scope for tasks. The title names it `Tasks: active` / `Tasks: all`. |
| `H` | Toggle the initiative view between `Current` (default) and `All retained`. Session-local presentation only. |
| `/` | Filter rows. `?` help. `q` exits the TUI only. |

The bottom line always labels tree focus as `[NAVIGATION]`. Every prompt and
form instead labels its active input `[TYPING] >`; while that label is visible,
all letters and punctuation belong to the field and no tree shortcut fires.
The cursor is shown only for the lifetime of the editor and restored when the
editor submits, is cancelled with `Esc`, or fails. Active input is emphasized
with reverse/bold where curses supports it; explanatory and inactive material
is dimmed, while a selected candidate keeps its own marker and emphasis. The
text labels remain authoritative on monochrome terminals.

Single-field prompts show their exact controls: `Enter` submits and `Esc`
cancels; prompts with candidates additionally support `Up`/`Down` selection
and `Tab` completion. The task and Room forms use `Tab` for the next field,
`Shift-Tab` for the previous field, `Enter` to accept (and submit on the final
field), `Esc` to cancel, and `Up`/`Down` for candidates. On the final field,
`Tab` stays in place and points to `Enter` as the submit action. Validation
stays beside the active field without discarding other field values. A Room
form is one four-field editor—Project, Room name, Harness, Opening prompt—and
launches only after all required fields are valid.

`asha initiative attention [--json]` is the CLI twin of `!`: one list of
everything waiting on a human across initiatives and tasks, each item naming
its resolution. A needs-input head is listed as `operator-decision`, quoting
the coordinator's question when its event is in the loaded tail, with or
without any node decision or approval beneath it.

The tree and the verb read one demand projection. `initiative_demand` is the
only classifier: the tree's WAITING ON text, the `need you` count, the `!`
filter, the Current/All retained decision and every `attention` item are
derived from its output for the same loaded head, so they cannot disagree
about what an ask is. Three head asks the tree wrote and the verb used to omit
are now listed as well: `integration` for a `ready-for-integration` head,
`activation` for an `approved` one, and `failed-nodes` for failed nodes under
a live head. Every item carries `certainty`: `live` when the records prove the
ask, `unknown` when the evidence is missing, stale, foreign, or self-
contradictory. An `unknown` item stays listed — absent evidence is never read
as an absent ask, and never as authority to act on one.

Two counts in this area are deliberately different and must not be read as
one. The header's `N need you` counts the *rows* on screen that are waiting;
`attention` reports *asks*, and one head can carry several (a question, a
requested approval, and two node decisions are four items on one row). The
verb is also independent of this terminal: `H`, `/` and `!` narrow what the
tree draws and never what the verb reports, which stays the complete bounded
projection over every loaded head. An archived head is loaded as metadata only
and asserts nothing in either direction. What the verb's read could not see is
reported beside the items rather than inside them — see *Bounded retained
reads*.

### Parking waiting work

`pause` is scheduling and operator-attention parking, never a resolution. A
running or needs-input initiative moves to `paused` with one
`initiative-state-changed` event naming the actual source state; every node,
attempt, decision, approval, seal, and linked task stays byte-for-byte as it
was, no worker process is stopped, and no prompt is answered. Pausing parked
work is idempotent and journals no second edge. The `needs-input -> paused`
edge is operator-only: a live coordinator generation may still pause running
work, it cannot park the operator's own question, and a fenced generation is
refused before the executor as before.

While parked, the initiative is status, not demand. Its head leaves the
`need you` count, the `!` filter, and `asha initiative attention`, and so do
its durable node demands: a node in `needs-input` and a coordinator parked on
a ready node. Live observations are never parked. A worker still at a prompt,
an attempt awaiting exit (`X` closes it), a process blocker, or a requested
salvage approval stays visible beneath the paused head, and a prompt an ended
task once showed is history, not a live ask. Under `!` those live asks are
listed with their paused head even while it is collapsed; the head still
counts as `paused`, never as `need you`, and an idle parked head stays out.
The five-second full refresh and the task-only incremental patch apply the
same rule, so the header count, the filter, the verb, and the tree cannot
drift apart.

`resume` runs the same live reconciliation as before, refuses on a live
identity conflict, and returns the initiative to `running`, with one
exception read from the durable records: when the park began in `needs-input`
because of a coordinator `request-decision` that nothing has answered since,
`resume` returns the initiative to `needs-input` and journals
`paused -> needs-input` naming the restored question event. The question was
parked, not answered, so it is the operator's attention again in the head, the
`need you` count, `!`, and `asha initiative attention`; the next `resume`
answers it exactly as before parking.

An answered question is not restored when the records prove the answer ran.
The answer is the `needs-input -> running` edge, and because only an answer
takes a waiting initiative back to `running`, any later edge that leaves or
enters `running` proves the wait ended even when a controller died inside the
answer before its own edge landed. One writer leaves no such edge at all: the
paused-seal outcome writer returns a `running` head to `needs-input` without
journaling one. For that case the answering `resume` is read as well as the
journal. It retains the exact head it observed immediately before its own
write, `state_revision` counts every head write while `last_event_sequence`
counts only events, and the growth of their difference between that
observation and the next observed head counts the head writes in between. A
`needs-input` head can only be moved by a pause, a resume, or a node
continuation, and each pause and resume retains its own observation, so a
window that holds no journaled `initiative-state-changed` edge, no other
unsettled pause or resume, and at least one head write leaves the answer as
the only writer of the head it was about to write. That interrupted answer
stays indeterminate rather than being completed or invented, and no edge is
journaled in its name; only the question it discharged is not restored.

The proof is the head write, never the retained intent, which is written
before any effect. When that evidence is absent or ambiguous the question is
restored and the operator answers it again: an answer with no head write
after its proof, a competing pause or resume that could own the write, a
journaled edge in the window, an action bound to a superseded plan or another
initiative, and an event tail that no longer reaches the head all leave the
question open. Restoring an answered question costs one repeated answer;
discharging an unanswered one loses the operator's turn, so the ambiguous case
takes the first. A needs-input head that came from a paused seal returns to
`running`, its node decision re-exposed from the node's own record.
Unresolved node decisions, approvals, and worker prompts reappear from their
own records because nothing cleared them.

A pause or resume the controller dies inside is settled by `reconcile` from
the action's own durable proof: the head it observed, the head writes since,
and the edge bound to it. With its edge retained the effect is complete; with
no head write since its proof it is refused as never started; with exactly
one head write it completes only when nothing else can own that write. The
breaker and seal-drift writers park running work with no edge and are known
by the identity they journal under, on every route they take, so an interrupted
running-origin pause followed by one of them stays indeterminate instead of
being completed from a foreign write. A completed recovery records the proof
and the result, not the interruption's transient status or reason.

The default `active` scope does not load or reconcile archived tasks. `A`
switches to `all`; archived records use their durable lifecycle projection and
display `archived` even after their session and workspace have been pruned.
The title always names the current scope, literally as `Tasks: active` or
`Tasks: all`, because `A` scopes the task branch's lifecycle while `H` scopes
which retained initiatives the tree draws. The older `Scope: active` /
`Scope: all` wording is kept beside it as a compatibility label for readers of
a whole rendered screen that have always found the task scope under that name;
it carries the same fact, is the lowest-priority piece in the title, and is the
first thing width sheds. Scope reloads preserve the selected task when it
remains visible, and the text filter remains independent.

### Current and All retained initiatives

`H` toggles the initiative view. `Current` is the default and is what the
operator's own terminal shows on open; `All retained` adds every other head
the refresh loaded. The title names the active view (`View: current` /
`View: all retained`), the footer names it beside `[NAVIGATION]`, and `?`
explains it — all three without any row being selected, because an empty
Current view is exactly when the other one is needed. The toggle is
session-local presentation: it loads nothing extra, reconciles nothing,
records nothing, and does not reach the CLI, Rooms, workers, coordinators or
native launch. Switching preserves the selected row by identity, every
expansion, the text filter and the `!` filter; a selection the new view hides
falls back to the first row.

A loaded head is **Current** when the records at hand support it, never
because of a status word:

- an ask exists for it under the one demand projection above, `live` or
  `unknown`;
- its coordinator record is in a live state and its anchor is not proved gone
  (a `None` liveness is unknown, which keeps the head);
- an attempt is in a non-terminal state — actual work, or an attempt whose
  worker contradicts it, which the operator must see either way;
- or its evidence is unexplained: the bounded read was short or partly
  unreadable, a started head returned no node records at all, or the plan
  calls a node `ready` or `dispatching` with no attempt carrying it and no
  live coordinator watching. The last case is a stall; the parked-coordinator
  rule only fires while a coordinator is live, so without it a dead
  coordinator would take its ready work off screen with it.

Everything else is retained work, reversibly held back and reachable with one
keystroke: a quiet `draft` or `planning` head is **queued unfinished**, not
completed and never counted as settled; a quiet `paused` head is parked; a
terminal head is settled. A `running` head with nothing observed running, no
coordinator and no ask is a label, not activity, and is held back too.

`All retained` means the retained heads this refresh actually read. It is not
a guaranteed global total and it does not expand history: an archived head is
carried as **head metadata only** — its nodes, attempts, events, seals, links
and actions are never loaded, its detail pane says so, its NODES column reads
`?` rather than `0/0`, and it asserts no demand in either direction. Read an
archived graph with `asha initiative show`, outside this refresh.

A head whose own graph could not be read in this refresh is treated the same
way in one direction only: its records read `?`, its detail pane names the
failure and claims nothing else, and it is never counted as quiet. Unlike an
archived head it stays in **Current**, because missing evidence is not proof
that nothing is happening.

The title carries the counts and their honesty: `Heads 1/3 · 2 hidden` names
the drawn count over the heads this refresh loaded, and how many loaded heads
are not drawn — by this view, by `!`, or by the text filter, each of which
also names itself in the title. A `(partial)` marker means the bounded read
was short or lost a record, so the denominator is a floor rather than a total;
`(unknown)` means at least one shown head is shown because its evidence was
missing, not because activity was proved.

Neither marker depends on anything being hidden. All retained holds nothing
back by construction and Current holds nothing back whenever every loaded head
is current, so a marker gated on a hidden count would have been unreachable on
exactly the screens that most need it. With nothing hidden the title reads
`Heads 2/2 (unknown)`; with nothing hidden and nothing uncertain it carries no
head count at all, so the marker still means something when it appears. At
122x28 the demand count comes first, then the labels that change what is on
screen — the `!` filter, the text filter, a non-default view or task scope —
and the quieter counts, including an unhidden head count, shed first.

### Bounded retained reads

The native tree and `asha initiative attention` read the retained store
through one loader, `_load_initiative_views` in `lib/control/tui.py`. There is
no second enumeration and no unbounded fallback: neither surface can see a
head, a record, or an ask the other cannot, and neither can outrun the caps
below.

One observation is bounded in every dimension it reads, under a single
`PresentationBudget` from `lib/control/orchestration/store.py`:

| Bound | Default | What it limits |
|---|---|---|
| `PRESENTATION_HEAD_LIMIT` | 256 | Entries enumerated in the initiative registry. |
| `PRESENTATION_PER_HEAD_LIMIT` | 512 | Entries enumerated beneath any one head. |
| `PRESENTATION_NESTED_LIMIT` | 8192 | Entries enumerated beneath every head together. |
| `PRESENTATION_TASK_LIMIT` | 512 | Control task rows admitted into the observation. |
| `PRESENTATION_EVENT_TAIL` | 50 | Event payloads read *after* a bounded enumeration. |
| `PRESENTATION_DEADLINE_SECONDS` | 2.0 | One shared cooperative wall clock. |

Every enumerated entry costs one unit, so a graph of a thousand records spends
a thousand units: a whole graph is never priced at one. That is why the head
cap alone was not boundedness — it limited how many record *sets* were opened,
not how much was read. 2.0s leaves the five-second automatic interval room for
the task and Room branches after it.

The per-record reads go through the store's bounded presentation readers,
which reuse the same descriptor-relative, no-follow, ownership-checked file
readers and the same model validators as the strict readers. They add no
validator and no record class. What they add is tolerance with an account: an
entry that is foreign, truncated, malformed, symlinked, or whose identity does
not match its filename is excluded and counted — never repaired, adopted,
renamed, removed, or presented as valid — and its readable siblings are kept.
The strict journal and control readers are untouched and still fail closed.

The deadline is **cooperative**: it is checked between directory entries, so a
single blocking filesystem syscall inside one record read is not preempted by
it and no hard containment is claimed. Reaching a cap, spending the deadline,
or failing to read a record is reported rather than absorbed. The observation's
summary names which caps it reached, whether the deadline was spent, how many
records were unavailable, and the first few failures verbatim.

Nothing already read is thrown away. A spent head cap stops the pass and keeps
the heads it read. One damaged subrecord degrades its own record class, not the
head: the head keeps its row and its readable records, and the counts say
`partial`. Even an unexpected failure while assembling one head leaves that
head on screen as unknown rather than blanking the tree behind it — a head with
records nobody could read is missing evidence, never proof of quiet, so it
stays in Current with its record counts shown as `?` instead of `0/0`.

An archived head's graph is deliberately never opened, and its counts read `?`
for the same reason: `0/0` would assert a count over records nobody read.

A capped or damaged event sample is a sample, not an exact tail. The bundle
records whether its journal was enumerated whole, parsed whole, contiguous from
sequence 1, and no longer than the tail; only then may the causal
answer-discharge classifier read it. Given anything less, an unanswered
operator question stays the operator's and is marked `unknown`, because a
sample cannot show the edge that would discharge it and reading its silence as
an answer would retire a question nobody answered.

The title marks incomplete counts `(partial)`, and the unbound-task branch is
labelled `Unbound tasks (binding partial)` because a read that could not see
every head's ownership records cannot prove which tasks are unowned.

`asha initiative attention` carries the same account beside its existing
`contract` and `items` fields, as an additive `observation` object holding
`complete`, `truncated`, `deadline_exceeded`, `caps_reached`,
`unavailable_records`, the first `failures` verbatim, the `scanned` counts, the
`limits` this pass actually spent, `heads_loaded` and `items_reported`. It is
present for zero rows as much as for many — a zero-item list is exactly where
an unread source would otherwise read as proof. Nothing is ever fabricated as
an item to carry it: an item in that list is a claim that something waits on a
human, and a short read is not that claim. The human output says
`Nothing is waiting on a human.` only when the read was complete; otherwise it
says so and names the cap, the deadline, or the unreadable records instead.

Task ownership is resolved before any presentation filter runs, over every
loaded head including the ones the current view hides, and from both durable
records: the link a dispatched attempt writes, and the attempt's own `task_id`
reservation, which exists first. Filtering first listed a hidden head's worker
as an unbound task; reading only links listed a reserved-but-unlinked worker
the same way.

### Retained directives and current demand

A `directive` action records a bounded instruction for a live attempt and
leaves `delivery` at `pending`; no harness seam is proven safe for mid-run
delivery, so the controller never types into a pane. `pending` is therefore a
durable record, not evidence that anything is still waiting — and reading
`delivery == "pending"` alone reported a directive whose target had long since
sealed as a live ask forever.

Each pending directive is now read against its own bound records — node,
attempt, active plan, and whatever observation of its Control task is at hand
— and lands in exactly one of three classes:

- **live** — the binding resolves and its target has not ended. The ask is
  listed by `attention` and drawn on the node row it binds to, including
  beneath a collapsed paused head, because a live directive is happening now
  regardless of the head's schedule.
- **deferred** — the binding resolves and proves the target already sealed
  with no live worker. This is retained history: it leaves the demand list and
  the WAITING ON column, and nothing about it is delivered, acknowledged,
  relayed, or deleted. The action record, its `directive-accepted` event and
  its `pending` delivery stay exactly as they were.
- **unknown** — the binding is missing, malformed, foreign to the loaded
  records, bound to a plan that is no longer active, or contradicted by the
  observations (a sealed attempt whose worker still reads live, an unsealed
  attempt whose worker has ended). The ask stays listed with the reason
  named, and its resolution says to verify the target rather than relay it.
  Uncertainty is never silently discharged and never confers authority.

`validate_action` constrains `outcome` only as optional text, so a foreign or
corrupted record can hold well-formed JSON of the wrong shape — `[]`,
`"pending"`, `7`. That is exactly as unreadable as a truncated record: the
`delivery` field it would have carried is not there to read. Such a record is
`unknown`, on the same branch as a parse failure. Reading a missing field as
"not pending" would have discharged a directive whose binding was never
resolved.

A directive whose node binding matches no loaded node record cannot address a
node row, so it is shown on the head instead of disappearing. Nothing in this
classification writes a record, sends a keystroke, or invents an
acknowledgement, and no directive is suppressed as a class.

### Tree mechanics

The view is text only, works on an 80x24 monochrome terminal (narrow widths
drop the middle columns, never the attention column), and reads orchestration
state through the same typed controller functions the CLI uses (`snapshot`,
`show_payload`, `reconcile_one_initiative`, `approve_plan`, `reject_plan`,
`submit_action`); it duplicates no lifecycle logic. Orchestration is imported
lazily; a malformed orchestration configuration degrades the initiative branch
to an inline note and leaves the task branch fully usable. The five-second
automatic refresh samples changed tree facts with lock-free snapshot readers;
an unchanged sample does not rebuild or repaint the tree.
`Tab` no longer switches modes — there is one view.

| Key | Action |
|---|---|
| `Up`/`Down` | Move between initiative, node, and attempt rows. |
| `Right`/`Left` | Expand or collapse the selected initiative or node; `Left` on a child returns to its parent. |
| `Enter` | Open the selected node's or attempt's linked Control task in the existing tmux popup. |
| `r` | Reconcile the selected initiative (actions, live evidence, coordinator anchor) without dispatching. |
| `d` | Read-only jj diff summary of the selected node's linked task workspace. |
| `e`, `c`, `v`, `t` | Toggle a pane: recent events, candidate seals, review + verification evidence, retained storage (sampled on demand). |
| `a` | Perform the operator act this row is waiting for: decide a pending plan approval (type `approve` or `reject` exactly), activate an approved initiative, or archive a terminal one. Every form is recorded as operator actor `tui`. |
| `p` | After `yes`, park a running or needs-input initiative, or resume a paused one. The confirmation names the action that will be recorded; anything but exact `yes` is a no-op. |
| `s` | After `yes`, ask Control to stop the selected attempt's task gracefully. |
| `/` | Filter initiative rows without mutating state. |
| `?` | Help for this mode. `q` exits the TUI only. |

The table shows `STATE`, `INITIATIVE / NODE`, `PIPELINE`, `WORKER`, `AGE`, and
`WAITING ON`. Every layout fills the terminal exactly, and columns shed by
width in a deliberate order — `WORKER` first, then `AGE` and `STATE` — because
`PIPELINE` and `WAITING ON` are why the operator looked. `PIPELINE` survives to
46 columns; below that only the demand does. Each cell keeps one column of
clearance, so a long slug can never run into its neighbour. The detail block shows the coordinator
claim and anchor liveness, the
pending approval, the latest candidate seal, the review verdict, the
verification outcome, limits, storage, and the last events as separate facts.
No key in this view reaches merge, rebase, bookmark movement, push,
publication, workspace removal, or deletion; approval keys are operator acts
and refuse nothing here because the TUI runs in the Keeper's own terminal, not
the coordinator's pane.
Every reconciliation path re-reads lifecycle under the task lock immediately
before consulting live adapters. If archive wins after an earlier list or
selection snapshot, the path returns the durable archived projection instead.

`x` builds its menu only after re-reading the task and its durable
task-to-initiative link. A terminal unowned task offers inspect, archive, and
retry. A terminal initiative-owned task replaces ordinary retry with
single-initiative reconciliation. Archived tasks offer prune, plus inspect
while their owned tmux session remains and either retry or initiative reconciliation
according to ownership. A runless failed creation has no recorded primary
harness/role to reconstruct, so it can be archived and pruned but does not
offer retry. Ambiguous or unreadable orchestration bindings refuse
the menu. Reconciliation runs actions, live evidence, then node reconciliation
in the CLI's order; it never dispatches ready work or merges, integrates,
pushes, or resurrects a coordinator. Its result names the action IDs and states
it actually reconciled: it does not claim that an earlier authorized dispatch
could not have been replayed.

For an unowned active run, `x` offers only state-valid signals. `starting`,
`working`, and `needs-input` offer **Interrupt** (SIGINT) and **Terminate**
(SIGTERM); `idle` offers **Finish** (SIGTERM, which ends the interactive
harness); `unknown` offers **Terminate**. Every signal modal names the task,
run, and signal and requires exact lowercase `yes`. Control then re-reads task,
run, ownership, and evidence before calling the shared locked `stop_task`
controller. The confirmation states that signaling neither archives the task
nor removes its workspace or change. Confirmations render the bounded action,
task/run identity, signal, preservation consequence, and exact-`yes`
instruction as wrapped context above a short active input row; at ordinary
24x80 and 24x120 geometry none of those safety facts is cropped. Tiny views
retain the input first and mark omitted context explicitly. The TUI never signals a PID directly and reports only that the
signal was requested plus the newly observed state, not that the process has
exited. An initiative-owned active task instead offers **Stop attempt via
initiative**. That path re-resolves the all-state binding and submits exactly
one versioned `stop-attempt` action with actor `tui`; it exposes no raw-signal
escape and does not run broad initiative reconciliation afterward. Completed,
refused, and indeterminate action state is reported separately from the freshly
read attempt state, its refusal reason, and observed OS exit, with the action ID
retained for explicit reconciliation.

Archive and prune remain separate operations. Archive requires exact `yes` and
retains resources. An archived task's prune action first runs the shared
`prune_task` controller in dry-run mode, displays the planned session/workspace
outcome, requires a second exact `yes`, then reassembles bindings and ownership
facts before the real controller call. The archived record, digest, jj change,
orchestration links, and seals remain. The TUI performs no direct tmux, jj, or
filesystem removal.
Malformed, oversized, non-regular, or unreadable prune history fails closed as
a bounded TUI status error; it does not terminate the curses session or permit
an action from incomplete cleanup evidence.

Ordinary retry creates a distinct task with a fresh UUID and a bounded
`<old-slug>-retry-<uuidhex>` workspace slug. The suffix encodes all 128 bits
of the fresh canonical UUID without hyphens, so distinct retry task IDs cannot
alias after slug truncation. Repository, source kind/number,
requested base, primary harness/role, and the exact stored label are
reconstructed; the old task is unchanged. PR retry intentionally re-resolves
the recorded PR's current head, which the confirmation modal states. It uses
the same detached JSON worker, cancellation, and recovery boundary as `n`.
Initiative-owned or ambiguously owned tasks cannot use ordinary retry. After
confirmation and before allocating the fresh UUID, starting a worker, or
watching source state, retry repeats both the terminal lifecycle and all-state
ownership checks.

The `n` form is one stateful repository, base, harness, role, and goal editor.
Its frozen convenience snapshot orders the current directory before unique
registry repositories by newest task use. Base candidates are the empty default
then recorded admissible bases for the selected repository; changing repository
recomputes that list. Harness candidates are the configured default then the
closed supported allowlist with installed status. Roles begin with
`implementer`, then observed roles, and still accept a grammar-valid custom
value. Candidate data is never authorization. Up/Down selects, Tab completes a
selected prefix, Enter accepts the selection or typed value, Shift-Tab moves
back without discarding values, and Escape cancels the entire form without a
worker. Harness and role prefix matching is ASCII-insensitive with canonical
candidate insertion; the typed harness value must be wholly ASCII before that
match. Repository and base matching remains case-sensitive. Candidate raw
identity is distinct from sanitized display text: exact raw values govern
deduplication, matching, completion, and submission, so display sanitization
cannot retarget a path.
Printable command keys are field text while the form is open. Its defaults are
the current directory, empty base, configured harness, and `implementer`. The
empty Base row resolves after Repo is accepted and displays bounded ref name(s)
plus an abbreviated OID. This preview is not authority: the worker independently
resolves under the controller transaction and uses the preview OID only as a
freshness assertion, refusing a race before launch. Reaccepting or changing
Repo recomputes the preview; resizing while editing Base refreshes it as well.
If no default can be previewed, Enter cannot accept the empty Base: select or
type an explicit base before continuing. Success names the authoritative full
base OID.
The form invokes the same controller validation as `asha task
start` and always supplies `--detach`, so creation does not replace the TUI
with the new task's session. Select the created task and press `Enter` to open
it. The shared cell/grapheme-aware modal clears and redraws safely across
resizes, shows at most eight candidates, and enforces one aggregate snapshot
limit of 128 raw identities plus 256 KiB of raw/display UTF-8 data. It reserves
the active input/cursor before dropping titles, context, hints, or candidates
at narrow heights, and preserves logical values even at zero- or one-column/row geometry. After all five fields are submitted,
the TUI keeps curses active while a detached JSON task start runs in an
isolated Python child process. It displays preparation progress and polls for
`Escape` every 200 ms. Escape sends one SIGTERM to that owned worker process
group and waits for the creation journal to settle; it never escalates to
SIGKILL. Cancellation before any workspace/root filesystem mutation rolls the
creation claim back. Once a v2 preparation mutation may exist, cancellation
retains the jj registration, workspace, and created-parent state, marks the task
failed and journal preserved, and names the exact `jj -R SOURCE workspace list`
and path inspection required. It names the explicit `asha task archive ID` then
`asha task prune ID --yes` route only when existing prune preconditions are
durably proven; a partial add or created-parent residue requires manual cleanup.
If possible process execution already won the race, the TUI instead
reports retained resources and attach/recovery commands. A normal completion
wins over a late buffered Escape. Once the owned worker
leader exits, pipe draining is short and bounded: descendants cannot freeze
the modal by retaining inherited descriptors. Exceptional cleanup waits have a
finite deadline and report unconfirmed termination conservatively without a
second signal or automatic SIGKILL.

Modal input uses curses wide-character reads rather than decoding byte-oriented
key events. Prompt editing is logical-text based and renders a cell-width-aware suffix
viewport. Long ASCII, wide CJK, combining characters, variation-selector and
keycap emoji, modifiers, flags, and valid ZWJ emoji remain intact as whole
display clusters. The visible line and cursor stay within the terminal's
reserved final column across resizes, including one- and two-column terminals.
Control accepts the supported person/man/woman plus laptop profession ZWJ
sequence (with an optional valid modifier). Unsupported typed ZWJ sequences
and dangling joiners are rejected at submission; preloaded unknown sequences
are measured as their separate visible glyphs rather than collapsed, but are
not accepted as durable task labels.
Every ordinary visible code point must also satisfy Python's terminal-printable
predicate at the editor, durable task model, harness argv, and tmux argv
boundaries. Line/paragraph separators and Unicode noncharacters are therefore
rejected rather than persisted and later displayed as `?`. Only validated
cluster-local joiner, selector, keycap, modifier, and regional-indicator cases
remain exceptions.

The TUI offers `a` for an ended task, a running task whose reconciled runs are
all terminal, or a failed task with no live preserved run (including a runless
failed creation). The archive controller re-reads and revalidates under the
task lock; for a running task, final reconciliation also refuses any blocker.

`Enter` selects the target pane and attaches to its persistent task session in
a popup. Closing the popup only detaches that popup client: it does not stop
the harness, archive the task, or alter the jj workspace or change. The TUI
then immediately reconciles that active task before redrawing. Archived rows
retain their lifecycle projection rather than consulting removed live state.
The popup is bound to the client attached to the caller's own tmux session and
never opens on another client. Control clears inherited `TMUX` only for the
popup child before its inner attach, so tmux does not reject that client as a
nested session. It uses the running absolute Python executable as a fixed
argv-only wrapper, mutates the child environment, and immediately `exec`s the
unchanged tmux/socket/session argv. This preserves tmux 3.2/3.2a support rather
than requiring the newer `display-popup -e` option. The parent Control
environment and `TMUX_PANE` are unchanged.
A nonzero popup result is not a normal close: `asha task attach` prints the
refusal and returns 2, while the TUI retains the numeric status plus exact
manual attach command after its immediate reconciliation. A successful
non-detached `asha task start` whose advisory popup fails still prints that
diagnostic and returns 0; the newly created task remains live.

While open, the TUI samples displayed state at a five-second monotonic cadence
on one daemon thread named `asha-control-refresh`. One `list-panes -a`
inventory per recorded tmux socket supplies session existence, immutable IDs,
ownership options, and pane facts to tasks on that server; the default-socket
inventory also serves the initiative and Room branches. Live tasks may still
require their own bounded screen-tail, process, event, or jj evidence. An
already-terminal task skips per-task tmux and jj subprocesses while still
consulting the bulk inventory's in-memory pane fact for a contradictory live
process. Terminal rows are cached by their task-record digest;
the worker also fingerprints the complete row and branch payloads, reuses
unchanged objects, sorts off the curses thread, and hands over only the changed
displayed task rows plus removal/order facts.

Each pass places its frozen result in one slot under a lock. The single worker
runs at most one pass at a time, never builds a timer queue, and a newer
completed pass replaces an unapplied older one. The next normal pass is due
five seconds after the preceding work finishes. Deltas are measured from the
last snapshot the curses thread actually applied, so replacing an unapplied
result cannot lose its task-row, initiative, or Room changes. Each external
adapter call still has a deadline. The curses loop polls without blocking
after every input poll. Applying an unchanged snapshot is a no-op; a changed
snapshot preserves stable visible-row objects and patches only changed task
rows when tree shape and filters permit, so key handling never waits for
refresh work or a whole-tree rebuild. Selection remains bound by identity.
An operator action that loads state synchronously on the curses thread fences
earlier passes: a snapshot whose generation predates that load is discarded
rather than overwriting the newer result. Snapshot application happens only in
the main curses loop after its input poll. A filter prompt, form, confirmation,
or other popup that enters its own modal key loop does not poll the snapshot
slot; background loading continues, and the latest result is applied after the
modal returns. Ordinary main-loop key handling does not deliberately pause
application, although a synchronous action naturally delays the next poll for
the duration of that action. Adapter failures are reported in the status line.
The thread does not start the separately managed Control
supervisor; the operator starts it or installs its user service explicitly.
Exit stops the thread and joins it for at most one second before daemon status
lets the process leave. A later successful automatic pass clears only its
stale automatic-refresh diagnostic; operator action and skipped-registry
messages remain. `r` remains the explicit selected-task refresh.
For an archived row, `r` refreshes only the durable lifecycle projection and
does not probe removed live resources.

The displayed state and its provenance are selected by the same reconciliation
pass. Detail names the winning source, its observation timestamp, and whether
that observation is `fresh`, `stale`, `durable`, or `unknown`; AGE is computed
from that observation timestamp, never a running task's mutation time. A
no-run creation uses its durable task timestamp because the creation journal
has no separate timestamp. If evidence is missing, malformed, unreadable,
stale past its trust window, or otherwise cannot support a current state, the
reconciliation contract, list/show output, mirrored tmux state, and TUI all
report `unknown` rather than reusing a task mutation time or old positive
state.

The TUI requires stdout attached to a TTY and importable curses support whose
`setupterm()` check succeeds. If any preflight check fails, `asha control --initiatives`
writes this diagnostic to stderr and exits 2 without opening a curses screen:

```text
asha control: terminal TUI unavailable; use `asha task list --json` as the non-interactive fallback.
```

Use `asha task list --json` directly for scripts and other non-interactive
callers. A curses failure after initialization also exits 2 and names the same
fallback.

### Colour, tiers, and the pipeline rail

`lib/control/tui_style.py` owns the whole visual vocabulary and imports no
curses; the renderer stays terminal-independent and only `_paint` reads it.

The four record classes carry 57 states (45 distinct words, since `running`,
`failed`, `approved`, `cancelled`, `dispatching`, `needs-input` and `stale` are
reused across classes). No operator holds that many words in their eye, so
`tier_for` maps every state to exactly one of five tiers, and the tier is what
colour means:

| Tier | xterm | Means |
|---|---|---|
| waiting | 214 | Nothing advances until the operator acts. The only loud tier. |
| machine | 74 | Work is in flight; visible, never urgent. |
| good | 71 | Settled and passed. |
| bad | 167 | Settled and failed. |
| inert | 245 | Not reached, held, or already history. |

Colour never carries alone where a word can carry with it. The `STATE` column
shows a short label (`awaiting-plan-approval` renders as `approve`, never as
the ambiguous stub a 10-column clip produced), and the rail shows a glyph. On a
monochrome terminal `init_colours` returns False and the two loud tiers keep
bold — bold is one attribute and cannot encode five. An 8-colour terminal gets
the coarse ANSI approximations. The 72-column layout drops `STATE`, and that is
the one place the glyph carries alone; it is a stated cost of the narrow pane.

`PIPELINE` is six fixed stages — plan, approve, build, review, verify,
integrate — one glyph each, derived by `rail_tiers` from the stored record
only. A stage is `!` when it waits on the operator, `✗` when it failed, `●`
when live, `✓` when passed, `·` when not reached. Within a stage a demand
outranks a failure, which outranks live work: the loudest true thing wins.

`approved` is a demand, not a resting state: an approved initiative advances
only when the operator activates it, so it renders amber and `a` activates it.
The count line separates `idle` (live but not started) from `settled`
(terminal), because a draft and an archived initiative are opposite things.

A stage is ticked only when its record exists, never inferred from where an
initiative ended: `draft → cancelled` is a legal transition, and an initiative
killed at draft must not claim a plan and an approval it never had.

A collapsed initiative rolls up a child's demand (`display_state`), because a
request for a human that is only visible after expanding a row is a request
nobody sees. A failing child is deliberately *not* rolled up — retries are
allocated automatically, so the initiative is still the machine's move, and the
rail already carries the `✗`. The title's counts are computed from the rendered
rows rather than the view list, and bucketed by the tier each row displays, so
a filter narrows the counts with the rows and the amber count always equals the
number of amber rows on screen.

Every cell passes through `safe_text` on its way to the terminal, so a control
code, bidi override or unassigned codepoint reaching a slug, goal or evidence
string cannot move the cursor or reorder a line, whatever the record validators
upstream accepted.

`◆ ● ◼ ✓ ✗` are East-Asian-ambiguous width: under a CJK locale, or a terminal
treating ambiguous as wide, each takes two cells and every column right of it
drifts. A CJK `LANG`/`LC_ALL`/`LC_CTYPE` selects the exact-width ASCII rail
automatically; `ASHA_CONTROL_GLYPHS=ascii` or `=unicode` overrides either way.

### Coordinator sessions

In Initiatives mode, `n` opens one Project/Harness/Assignment form. Select an
initialized project and a supported managed harness (Claude by default). Control
commits the initiative, session, and opening message together in the active
SQLite registry, then reports the retained IDs and runtime state. A paused runtime
keeps the assignment queued. Closing the interface does not stop admitted work.
`Enter` on the initiative opens its session state and paged events; `r` refreshes,
`n`/`p` change event pages, arrows scroll, and Esc closes inspection. Answer pending
questions and permissions through `M`; plan approvals remain in Control.

The CLI equivalent is `asha initiative coordinator launch --project PROJECT
--intent TEXT [--harness claude|codex] [--launch-id UUID] --json`. Keep the same
launch ID and assignment when retrying after a lost response. Unsupported managed
harnesses and uninitialized or ambiguous projects are refused before creation.
`coordinator attach ID` returns managed session state without a terminal popup.

Legacy terminal coordination remains available explicitly through `coordinator
launch --transport tmux --root DIR --intent TEXT`. These sessions use names
`<session_prefix>coord-<token>` and `@asha_coordinator_session=1`; their coordinator
resolves, creates, and claims an initiative. `coordinator sessions` lists current managed and legacy
sessions, and `coordinator attach ID | --session NAME` opens their terminal.
Rooms and worker terminal attachment keep their existing behavior.

A coordinator submits `request-decision` through `asha initiative action`.
Its payload `subject_id` must match the event subject-token grammar
`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`. Control refuses a mismatch before request
execution and before emitting the `approval-requested` event, so invalid event
subjects cannot leave the action indeterminate.

## Triggers

`asha trigger add NAME --schedule CALENDAR --root DIR --intent TEXT
[--harness H]` schedules a coordinator launch through a **systemd user
timer** (`asha-trigger-NAME.{service,timer}` under
`~/.config/systemd/user/`, `Persistent=true` so a missed window fires after
boot). Each firing starts an ordinary coordinator session that resolves the
repository, creates the initiative, and proposes a plan — then **waits at
plan approval** like every other initiative; triggers schedule proposals,
never unattended execution. `asha trigger list` shows armed triggers and
their next elapse; `asha trigger remove NAME` disables and deletes them.
Only units carrying the managed marker are ever modified; foreign units are
refused. `--dry-run` prints the unit bodies and commands. Inbound webhooks
are deliberately not built.

## Workspace trust

Every worker runs in a fresh jj workspace, which each harness treats as an
unseen directory behind its own trust prompt; a worker waiting at that prompt
looks alive. Control therefore **inherits** trust rather than inventing it: when
a task's source repository is already trusted in at least one harness store,
Control trusts the new workspace in every harness that has one (Claude's
`~/.claude.json`, Codex's `~/.codex/config.toml`, Copilot's
`~/.copilot/config.json`; OpenCode has no trust gate), so a later run under a
different harness is not blocked again. A source repository trusted nowhere is
never granted anything — the worker prompts, which is correct.

Granting is reported, never silent: as a `workspace-trust` source mutation in
the task-start payload, on the initiative's `attempt-started` event and the
completed dispatch outcome (so the coordinator sees it), and as a line in the
durable `asha.control-workspace-trust.v1` ledger at
`${ASHA_HOME:-~/.asha}/state/control/trust.jsonl`. `asha task trust [PATH]` reports
the current state per harness, and `asha task trust PATH --grant` performs an
explicit grant. Set `control.workspace_trust` to `"never"` to disable
inheritance entirely (the default is `"inherit"`).

A waiting worker is also detected rather than mistaken for a busy one:
`INPUT_PROMPT_MARKERS` covers Codex's and Claude's prompts, so reconciliation
reports `needs-input`, and the initiative journal carries that state up through
`task-status-observed`.

## Cockpit

The monitor's `n` (Coordinator sessions, above) is the front door; the cockpit
is the two-pane alternative when you want the coordinator chat visible beside
the monitor. `asha cockpit [DIR] [--session NAME] [--dry-run]` opens one tmux window: the
left pane runs `asha claude` at `DIR` (default: the current directory) and is
the coordinator's chat; the right pane runs `asha control --initiatives`, the
Keeper's monitor and approval surface. `DIR` is the projects root the
coordinator resolves intents against through `asha initiative projects`
(declared workspace manifest first, otherwise the jj-colocated Asha projects at
or within three directory levels below `DIR` by default). Every project-list
entry includes the additive `relative_path` field, which can be passed exactly
to `--match` when nested projects share an otherwise ambiguous directory name.
Inside tmux the window is added to the current
session; outside tmux a detached session named `asha-cockpit-<dir>` is created
once and attached. Before opening, a preflight runs `asha doctor claude`,
`asha initiative doctor`, and the project index for `DIR`, and refuses with
the remediation when the Claude install or the orchestration runtime is not
healthy (`--check` runs only the preflight; `--no-check` skips it;
`--dry-run` prints the tmux plan without it). The split is structural:
approvals typed in the left pane are refused because that pane carries the
coordinator claim; `Enter` on a node in the right pane opens the worker's
session popup.

`asha control --initiatives` remains accepted as a compatibility alias; the
tree is the only view, so it opens the same screen as `asha control --initiatives`.

## Task and run model

A task is the durable container. Its lifecycle is `creating`, `running`,
`ended`, `failed`, or `archived`; `creating` moves to `running` or `failed`,
`running` to `ended` or `failed`, and `ended` or `failed` (without a live
run) to `archived`, which unarchive reverses. The initial release launches one
primary mutating run. Runs carry a harness, role, tmux pane, verified process identity,
and current evidence state. A task may outlive the launching CLI, TUI, popup,
and ordinary tmux clients.

Names are display aids, not ownership. UUIDs, record digests, jj identities,
tmux user options, and live process facts establish ownership. Without an
explicit `--slug`, GitHub task slugs are derived only from repository name,
source kind, and number, such as `thallus-pr-34`; GitHub titles never enter a
slug, prompt, tmux value, harness argv, or record.

### Idempotent creation

`asha task start --task-id UUID` accepts a canonical lowercase caller-supplied
UUID. Under the task's transaction lock, Control creates the task when both its
record and creation journal are absent. An identical registered task is
returned unchanged without fetching a PR, importing Git state, creating a jj
repository, creating a jj workspace or tmux session, launching a harness, or
adding a run. A different request is refused; an interrupted `creating` task
must be recovered explicitly
with `asha task recover UUID` before retrying.

`--slug` is an optional public path-identity override. It accepts the same
1-64 character lowercase ASCII slug grammar as stored tasks and refuses the
reserved `materializations` namespace. It does not change the goal/label.
When supplied with `--task-id`, it is part of replay identity: a different
stored slug refuses before source mutation. The TUI uses this seam only to
give a distinct retry its collision-free workspace path.

## jj contract

Control accepts only the canonical jj repository root and a usable Git
backend after repository enablement. Selection of a plain Git root is read-only
until caller-supplied task-ID replay and interrupted-journal checks finish.
For a new task, Control serializes starts for that source, rechecks under the
lock, and first applies the shared source/workspace ancestry policy (including
exact uid/0700 for every existing managed destination parent), validates
published Memory, the explicit or omitted-policy Git base, PR remote selection,
prospective destination, and bounded materialization/context/journal capacity.
A refusal leaves the intent, `.jj`, task/journal, workspace, and source
semantics unchanged. Control then runs exactly one bounded argv equivalent of:

```text
jj --config 'snapshot.auto-track="none()"' git init --colocate SOURCE
```

Pre-enable authorization carries the exact source dev/inode/type/mode/owner
and complete Git marker/target facts. Control revalidates them before the
intent, immediately before invoking colocation, and afterward. All
authoritative Git reads in this transaction use the trusted absolute system
Git executable with a minimal explicit exec environment, exact git-dir/work-tree,
and read-safe overrides including disabled `core.fsmonitor`, hooks, unsafe
protocols, credentials, paging, and promisor-object lazy fetching. Inherited
`PATH`, loader, Git repository,
index, object-store, and counted-config variables are not forwarded. PR remote
configuration is selected through this seam before colocation. Only an HTTPS
or SSH URL whose repository identity matches the viewed PR is carried with the
already-read metadata and exact local-config digest; a sole mismatched remote
does not bypass this check. Execution-capable `url.*.insteadOf`, `protocol.ext`,
`core.sshCommand`, credential, filter, diff, merge, include, proxy, and custom
upload-pack configuration refuses before colocation. Git's split
`extensions.worktreeConfig` local-config plane is also refused because one
digest cannot otherwise bind the later fetch configuration. Only the fetch
remains a later reported mutation.

The command-scoped setting prevents untracked files from becoming
intent-to-add index entries. Strict jj preflight follows immediately. Control
verifies Git HEAD and symbolic branch, semantic index entries and their exact
normalized flags, every selected ref's object ID and symbolic target, and
descriptor-checked tracked/untracked filesystem state. It does not run Git
status or diff: those can invoke repository attribute filters. Bounded
plumbing lists index stages, flags, and cache facts; clean tracked content remains bound
by an exact cache match and index OID without rereading it, while changed
regular files and symlinks are hashed directly in Python. Missing paths,
tracked POSIX modes/types, staged entries, bounded untracked bytes, non-jj refs,
and (during reauthentication) `refs/jj` are all compared. Skip-worktree,
assume-unchanged, intent-to-add, conflict-stage, sparse-checkout, and same-OID
symbolic-ref target changes therefore differ. Raw Git index
bookkeeping bytes may change; operator-visible staged/unstaged and filesystem
state may not. Before mutation, Control durably writes an exact-root-bound
repository-init intent under the Control state directory and marks it verified
only after semantic comparison. For a `.git` directory it binds the directory
inode. For a regular `gitdir:` marker it additionally binds the bounded exact
marker digest, parsed canonical target path, and target inode/type/mode/owner;
editing the marker in place therefore invalidates the record. Verified state
also requires an inode-bound real `.jj` directory, never a symlink or file. A
usable `.jj` with an ambiguous Control intent is never adopted; inspect `jj
status`, Git status/refs, and the named intent record before repairing it. A
manually pre-existing valid jj repository with no Control intent remains
accepted. `KeyboardInterrupt` or SIGTERM during init/verification prints this
retained ambiguous-state diagnostic and remains an interruption (exit 130),
not a refusal. Verified colocation is a durable,
reported `jj-operation` (`git init --colocate`) retained across later task
failure or cancellation. Failed or ambiguous partial initialization is also
preserved for inspection, never recursively removed; use `jj status` and Git
status/ref inspection before retrying.

Automatic colocation refuses linked Git worktrees before writing an intent:
jj 0.38 cannot create a colocated repository there. Use the named primary
worktree as `--repo`, or manually create a supported jj repository and pass its
exact root. Regular `gitdir:` roots without a valid `commondir` marker, such as
Git submodules, remain supported. Doctor performs the same filesystem-only
classification and reads the private intent record without creating or
modifying it; ambiguous, stale, or binding-mismatched records report
`mismatch`, while verified usable state is accepted.

A verified Control record whose repository root changed only by removing a
nonempty subset of group/other write bits is reported by doctor as repairable,
without a write, only when the resulting path passes task-start path policy.
On the next task start, after the pre-enable checks, Control
requires the exact root/Git-marker/`.jj` identities, strict jj root/backend,
Git-HEAD/jj-parent synchronization, two identical config-sanitized Git semantic
captures including every ref (also `refs/jj`), and a stable jj operation ID.
It then compares the exact record bytes/digest again and rewrites `root_fact`
only while holding the exclusive source lock used by every Control intent
writer. Any loosening, mixed mode change, inode/owner/type/binding drift,
unstable semantic state, operation drift, intent state, or cooperative writer
race refuses and preserves the record bytes. POSIX provides no byte-conditional
rename, so this is not a filesystem-atomic CAS against a noncooperating same-UID
process able to rename the private source or state paths; that process is
outside Control's enforcement boundary. The remedy is to inspect `jj status`,
Git status/refs, and the named record; Control never asks the operator to delete
`.jj` or edit JSON.

Filesystem device numbers in that durable record are cached mount observations,
not repository lineage. Device and inode remain an exact pair within every
inspection, source mutation, workspace operation, and cleanup transaction. If a
reboot or remount changes one or more device numbers in an already `verified`
record, doctor reports a repairable read-only device-renumbering candidate only
when every canonical path, inode, full mode/type, owner, Git marker digest and
target, and `.jj` fact is otherwise exact. The complete old-to-current device
mapping must be coherent and one-to-one across root, marker, marker target, and
`.jj`, with at least one changed group. Task start then runs the same path,
Memory, base, destination, strict jj identity/sync, two-pass all-ref semantic,
and stable-operation authentication used for root hardening. Under the source
lock it re-reads the exact record bytes/digest and current binding immediately
before replacing only all four stored `dev` values. An `intent`, partial or
collapsing device map, mixed permission/device change, or any other fact drift
remains a refusal with the record unchanged. No Git or jj mutation is required
for this record maintenance.

Before any later source mutation, task start compares Git `HEAD` with jj
`@-` and refuses a committed divergence with a `jj status` remediation. Once
they agree (or Git `HEAD` is positively confirmed unborn while jj `@-` is the
zero root), every source mode runs one
`jj -R SOURCE --ignore-working-copy git import` after any PR fetch. Omitted
bases and explicit existing-jj revsets are resolved and pinned during universal
read-only preflight before this mutation. Existing-jj input retains arbitrary
jj revset syntax and semantics; the pinned OID is revalidated later. The import
is reported in `source_mutations` as a `jj-operation` with operation
`git import`. Base resolution is deterministic:

1. `--pr N` uses the fetched immutable PR head.
2. In an existing jj repository, `--base REVSET` must resolve to exactly one
   full commit ID. On the first plain-Git start, explicit ad-hoc/issue text must
   resolve as a Git ref/tag/OID before colocation; that exact OID is used later.
3. `--issue N` or ad-hoc work without `--base` resolves through exact Git:
   current attached local branch, then same-OID remote symbolic `*/HEAD`
   targets, then same-OID conventional local `main`/`master`/`trunk` refs.
4. Missing, ambiguous, or invalid commits refuse creation.

The task record stores both the human request (`PR #N head` or the literal
revset) and the resolved full commit ID. Before mutation, Control pins the
repository's full 128-hex operation ID. It then runs the jj 0.38 equivalent of
`workspace add --revision RESOLVED_COMMIT --message GOAL` at that operation,
creating one new empty working-copy change on the base. Source reads use
`--ignore-working-copy`; no controller path snapshots or moves the source `@`.

Control records the new change and working commit IDs. It does not create or
move a bookmark, integrate the change, push it, or remove the workspace.

Into the fresh workspace Control then provisions its bounded private context:
`.asha/config.json` and `.asha/control-task.json` (the task marker) plus the
source's published `Memory/activeContext.md` and `Memory/decisions.md`, under
`.asha/`, `Memory/`, and `Work/session-state/`. A repository that commits some
of those paths keeps them exactly as checked out from the base: an existing
tracked directory is reused and an existing tracked file is left alone, so the
change stays empty. Only the task marker itself, a symlink, or a non-regular
entry at one of those paths is a collision, and the source must ignore whatever
Control does create there or the workspace identity check refuses the task.
Before plain-Git colocation, task-state creation, destination-parent creation,
and workspace registration, Control proves that disposition from the immutable
base tree. The same early proof runs for an existing jj/Git repository before
`jj git import`, including when pending Git refs would otherwise make import
observable. It validates reusable tracked file bytes and schemas, requires
positive selected-tree ignore coverage for every exact file in the generated
context plan and the complete fixed `Work/session-state/` private subtree, and
refuses task-marker, symlink, or file-ancestor collisions. One representative
session filename never proves coverage for its siblings. The proof runs in a
private temporary Git namespace with empty repository/global/default excludes, so the
mutable worktree `.gitignore`, `.git/info/exclude`, global excludes, and verbose
negation records cannot authorize a private path. The same immutable evidence
is rechecked immediately before registration. After a successful workspace add,
root, registration, operation ancestry, materialization, and sidecar facts are
persisted before later context work can fail.
If the sole missing positive rule is `/.asha/control-task.json`, the hidden TUI
worker returns a strict task-bound refusal object on stdout. The TUI never
classifies stderr prose. Its Apply action revalidates the canonical root/Git
binding, project identity, selected ref/OID, immutable failure, working ignore
semantics, and exact `.gitignore` preimage under the source lock, then appends
one final managed block by atomic replacement. It preserves unrelated bytes
and file mode, creates no task/journal/workspace/tmux/jj state, never commits or
retries, and leaves the old immutable base unauthorized. Before creating its
descriptor-bound temporary, Apply rejects oversized intended bytes and proves
that the exact root result remains effective under a safe nested
`.asha/.gitignore`; a nested negation therefore cannot cause a knowingly
ineffective visible replacement. The rename attempt begins the indeterminate
boundary, including a syscall wrapper that reports an error after the kernel
made the replacement visible. A visible replacement
whose durability/final verification fails is reported as indeterminate. Cancel,
Escape, instructions, clean Apply refusals, and worker revalidation refusals do
not discard the filled start form. Each result is drawn as a bounded form-local
notice before a later key can acknowledge it; in particular, an indeterminate
result tells the operator to inspect `.gitignore` before retrying. SIGTERM and
SIGHUP still terminate the TUI with `128 + signal` while reporting that warning,
and `KeyboardInterrupt`/`SystemExit` retain their process-control semantics
across the rename boundary. Blank default bases are re-resolved on Enter; a
changed OID is drawn and must be accepted a second time.
Explicit diff refresh may snapshot the task workspace; background list and
reconciliation reads do not.

## tmux contract

Each task receives one detached controller-owned session with a stable `work`
window and primary pane. The operator's ordinary sessions remain separate.
The task pane uses `remain-on-exit`, so completed output remains inspectable.
Pane titles and window names are controller-generated restricted values;
automatic rename and terminal title escapes are not trusted.

Ownership is repeated in tmux user options:

```text
session: @asha_managed @asha_task_id @asha_repo @asha_workspace
         @asha_change @asha_state
pane:    @asha_run_id @asha_harness @asha_role @asha_state
server:  @asha_summary
```

Attach, stop, and other targeted operations require the registry identity and
tmux options to agree. A foreign session with the same readable name is never
adopted, signaled, renamed, or killed. `asha control tmux` prints an optional
format snippet and does not edit the user's tmux configuration.
Popups are bound to the client attached to the caller's own session and never
fall back to another tmux client.

The server summary labels itself `last-event-only`: it does not claim process
liveness. It consumes a filesystem-order sample of at most 257 directory
entries, sorts only that partial sample by UUID filename, inspects at most 256
snapshots, and reports when the directory scan cap is reached. The lookahead
entry may be non-JSON or the final entry, so this marker does not claim a known
omitted count. UUID snapshot names carry no recency, so this is neither a
globally deterministic selection nor a newest-run claim. Valid `working` and
`needs-input` snapshots older than `control.event_staleness_seconds` count as
`unknown`, using the same strict age boundary as live reconciliation; `idle`,
`exited`, and `failed` remain durable. Terminal reconciliation persists
the terminal run evidence before expiring the corresponding runtime snapshots;
archive does the same. A successful list, show, reconcile, or TUI refresh also
best-effort mirrors the derived primary run state to the pane and session only
after the managed task, exact run, session, window, and pane ownership all
match. Batch list, reconcile, and TUI refreshes publish the cached server
summary once after all rows; single-task show, manual refresh, and popup-close
paths publish it once. One sampled reconciliation time is shared by evidence
aging and summary aging for the pass. A late hook write rechecks the durable
run state before it may survive.

### Pane title and exit detection

Exit classification reads `pane_dead`, `pane_dead_status`, and `pane_dead_signal`
with the recorded pane/session ownership and process facts. Neither direct
pane reads nor bulk inventories fetch the pane title; `PaneFacts.title` remains
an empty compatibility field. Control sets a restricted title at launch for
display, but terminal output can replace it with arbitrary bytes. A long title,
control character, delimiter, or invalid UTF-8 therefore cannot invalidate
supervision evidence or strand a staged result. No title restoration is needed.
Ownership conflicts and malformed process/exit facts still refuse classification.

### Socket reaping

Short-lived helpers that create a dedicated tmux server on their own `-L`
socket (the confirm, finish, tail, doctor probe, and isolated test harnesses)
reap that socket on every exit path through `lib/control/socket_reaper.py`.
Before this, every such invocation left one socket file behind; 1,584
accumulated in `/tmp/tmux-1000` between mid-August and 2 September 2026.

The reaper is fail-closed:

- It handles only Asha-owned names (`is_asha_socket_name`: `asha-` followed
  by up to 123 name characters). `default` and every other name are refused.
- It kills the server, then proves the server is dead before unlinking. A
  refused or indeterminate connect counts as live, since absence from the
  socket table is not proof of death when a server lives in another mount
  namespace. A live socket is never unlinked.
- When `kill-server` cannot reconnect, it resolves the socket's holders
  through procfs (`unix_socket_owners`) and signals only same-user processes
  that hold that exact socket, re-verifying the owner at signal time. No
  owner visible, or procfs unavailable, is a fail-closed no-op.
- A failed close stays armed and retryable; the `atexit` hook is not
  unregistered on failure.

`TmuxSocketReaper(...).arm()` registers teardown for the lifetime of the
process and `close()` runs it; `reap_isolated_tmux_socket` is the one-shot
form. `lib/control/doctor.py` arms it on every exit path of its probe. A
harness that asserts its `-L` argv against an injected runner and starts no
server (`test_control_worker_record.py`) is deliberately left unarmed: its
bare `asha-control-test` name is shared by other fixtures.

No sweep utility exists. A sweep keyed on the `asha-` prefix alone would be
unsafe, because namespace isolation can make a live server look unreachable.

## State locations: one asha root

Everything durable lives under a single root — `$ASHA_HOME`, default
`~/.asha` — with only the ephemeral runtime dir outside it:

```text
${ASHA_HOME:-~/.asha}/config.json
${ASHA_HOME:-~/.asha}/state/control/tasks/<task-id>.json
${ASHA_HOME:-~/.asha}/state/control/tasks/<task-id>.lock
${ASHA_HOME:-~/.asha}/state/control/initiatives/<initiative-id>/...
${ASHA_HOME:-~/.asha}/state/control/authorities/<authority-id>.json
${ASHA_HOME:-~/.asha}/state/control/transactions/<task-id>.json (+ .ownership)
${ASHA_HOME:-~/.asha}/state/control/repository-inits/<root-sha256>.json
${ASHA_HOME:-~/.asha}/state/control/prunes/<task-id>.json
${ASHA_HOME:-~/.asha}/state/control/trust.jsonl
${ASHA_HOME:-~/.asha}/workspaces/<repo-key>/<task-slug>/
${ASHA_HOME:-~/.asha}/cache/                       (rendered persona files)
${XDG_RUNTIME_DIR:-/tmp/user-$UID}/asha-control/
${XDG_RUNTIME_DIR:-/tmp/user-$UID}/asha-control/events/<run-id>.json
```

`XDG_STATE_HOME` and `XDG_DATA_HOME` are no longer consumed; setting them is
ignored. `ASHA_HOME` is the one override for the root, exported once by
`bin/asha` so hooks, harnesses and worker panes agree; `ASHA_CONFIG` still
overrides the config file specifically. A symlinked `$ASHA_HOME` is not
supported (and never was: the config file's own parent guard refuses it) —
the supported dotfiles pattern is a real `.asha` directory whose leaf files
are symlinks. A group-writable `$ASHA_HOME` refuses every command with the
exact remediation (`chmod g-w,o-w ~/.asha`), because the state tree now
lives beneath it.

`control.workspace_root` in the config may replace the workspaces default. It
cannot be `/`, `$HOME`, the source, below the source, or an ancestor of the
source. Existing path components must be canonical directories without
symlink aliases or unsafe writable ancestry.

If the `/tmp/user-$UID` runtime fallback already exists but fails those safety
checks, Control refuses it and directs the operator to set `XDG_RUNTIME_DIR` to
an existing private directory.

### Migrating from the pre-consolidation layout

Installs that predate the single root keep data at
`~/.local/state/asha/control`, `~/.local/share/asha/workspaces`, and
`~/.cache/asha`. Until `asha migrate` runs, every command refuses under the
DEFAULT resolution with the remediation in the message; an explicit
`ASHA_HOME` bypasses the gate, since a deliberate redirection touches nothing
the gate protects.

`asha migrate --dry-run` prints the full plan; `asha migrate --yes` performs
it: one atomic rename of the state tree (verified by a per-file sha256
manifest staged beforehand), permission normalization (state 0700,
trust.jsonl 0600), retirement of path-bound husks — archived task records,
creation journals with their ownership sidecars, prune records — into
`state/control/retired-<date>/` with a review-digested manifest, deletion of
regenerable verification materializations after forgetting each jj workspace
by name through its source repository, and a supersession banner
(`ASHA-MOVED.md`) left at both legacy roots so a restored backup cannot
masquerade as live state. A marker at `state/.migration-v1.json` makes
re-runs no-ops; an interrupted run resumes from its phase journal. Manual
rollback before the marker: move `~/.asha/state` back and verify against the
staged manifest. Preflight refuses on live Control tmux sessions, any
non-archived task or initiative, cross-device layouts, symlinked roots, or an
existing new root without a marker. The doctor's `migration` probe reports
pending, complete, or a resurrected-decoy mismatch.

Retired records are retention, not deletion — but they are no longer visible
to the registry, deliberately: their digests are frozen into archived
initiative evidence and rewriting them would falsify it, while leaving them
in place would make every `task list` silently skip 65 records forever.

Writable ancestry is judged by mode, not ownership: a group- or other-writable
non-sticky directory anywhere on a Control path (state, runtime, workspace
root, task workspace, or source repository root) is refused, and every
component from the workspace root down must be owned by the effective user
with mode `0700`. Control creates its own directories that way and never
changes the mode of a directory it did not create; each refusal names the
path and the exact remediation (`chmod g-w,o-w <path>`). Task workspaces
created before 2026-08-17 may carry the umask mode `0775` and are skipped by
`task list` until remediated:

```text
chmod g-w,o-w "${ASHA_HOME:-~/.asha}"/workspaces/*/*
```

Task records use the `asha.control-task.v1` schema:

| Object | Fields |
|---|---|
| Task | contract, task UUID, restricted slug, operator goal label, timestamps, lifecycle |
| Repository | canonical root and stable derived identity |
| Source | exactly `kind`, `number`, and `url`; kind is `ad-hoc`, `pr`, or `issue` |
| jj | workspace name/path, requested base, resolved base commit, change ID, working commit ID |
| tmux | socket identity, session, and window |
| Runs | run UUID, harness, role, pane, PID plus process-start identity, harness session, state, evidence, timestamp |

The source title is not part of the schema. Records never contain the full
prompt, transcripts, terminal capture, tool arguments, hook bodies, or
secrets. Registry and event snapshots are bounded, private, atomically
replaced, and reject symlinked or malformed state.

## Status and evidence

Run states use a shared vocabulary:

| State | Meaning |
|---|---|
| `starting` | The pane and process exist, but no semantic start evidence is available. |
| `working` | A verified harness event reports active work. |
| `needs-input` | A verified event reports an operator decision or permission requirement, or the owned pane's visible tail shows the harness's known input prompt. |
| `idle` | A verified stop or completed-turn event occurred while the process remains live. |
| `exited` | The verified process ended normally. |
| `failed` | Launch or termination has verified failure evidence, including when the process ended by signal or vanished without a reported exit status while its pane was absent. |
| `unknown` | The process is live but current semantic evidence is missing, stale, unreadable, unavailable, or unsupported. |
| `stale` | Registry, tmux, process, event, or jj identities disagree. |

Reconciliation prefers live owned tmux evidence, verified process identity and
ancestry, live jj workspace identity, recent supported event evidence, then
stored lifecycle. Missing or conflicting evidence produces `unknown`, `stale`,
or an explicit blocker; reconciliation never mutates external state to make an
old record appear current.

"Recent" is enforced, not decorative. An in-progress event state (`working`,
`needs-input`) is trusted only while its snapshot is newer than
`control.event_staleness_seconds` (default `1800`). Past that window a live
process reconciles to `unknown` rather than a stale positive when no later
event has superseded the snapshot. `idle` (a completed turn) is a legitimate
resting state and is not aged; `exited` and `failed` are durable facts and
never age. Missing, malformed, or unreadable semantic event evidence produces
`unknown` in the frozen-shape CLI reconciliation and the TUI rather than
presenting an old stored positive as a current live observation; durable
terminal evidence remains terminal. A verified live process plus a stored
`starting` state and a verified missing semantic event (including
`SessionStart`, which carries no semantic state) remains `starting` with
process provenance. An unavailable event adapter produces `unknown`, including
for stored `starting`; neither missing nor unavailable semantic evidence can
preserve a stored `working`, `needs-input`, or `idle` positive.

Codex reports approval prompts through its live-proven `PermissionRequest`
hook, which is the primary `needs-input` observation. Reconciliation also
reads the last twelve visible lines of the owned, live pane (never scrollback,
never a dead pane) as a fallback when that hook is missed, delayed, untrusted,
or unavailable. When one of the harness's known input-prompt markers is on
screen (`lib/control/harness.py` `INPUT_PROMPT_MARKERS`), reconciliation
reports `needs-input` with tmux evidence that says the prompt was seen. A
screen observation outranks an older in-progress event snapshot but never a
verified `turn-stopped` idle, a fresh `permission-requested` event, a terminal
event, or a dead pane, and it is labelled as observation in the evidence
detail. Claude carries no screen markers; Codex retains them only for this
fallback.

The live-probed semantic claims are:

| Control event | Claude Code | OpenAI Codex |
|---|---|---|
| `session-start` | Wired from `SessionStart` | Wired from `SessionStart` |
| `prompt-submitted` | Wired from `UserPromptSubmit` | Wired from `UserPromptSubmit` |
| `tool-completed` | Wired from `PostToolUse` | Wired from `PostToolUse`; interception is known incomplete for `unified_exec` |
| `permission-requested` | Not claimed. `Notification` is multi-purpose and its payload is unverified. | Wired from `PermissionRequest`; delivery before the operator answers is live-proven on Codex 0.147.0. |
| `turn-stopped` | Wired from `Stop` | Wired from `Stop`; live-proven on Codex 0.147.0. Delivery remains subject to Codex's hash-bound interactive hook trust. |
| `session-ended` | Wired from `SessionEnd` | Codex has no equivalent event. |

Copilot and OpenCode provide process liveness only; no semantic Control event
is claimed for either. Event hooks receive opaque task/run identifiers, discard
prompt and tool bodies, and write one bounded current snapshot. Event delivery
is local, network-free, observational, and fail-open.

## GitHub source resolution

GitHub access is read-only. `gh auth status` distinguishes a missing CLI from
an installed but unauthenticated one. Metadata calls request only these fields:

```text
gh pr view N --json number,title,url,headRefOid,state,isDraft,isCrossRepository
gh issue view N --json number,title,url,state
```

All metadata is bounded and validated. Object IDs must be full lowercase Git
IDs; titles and URLs reject Unicode control characters. A title is printed
once as transient display text and discarded.

PR mode performs and reports exactly these repository mutations:

Before these source mutations, a head OID absent locally is fetched into an
isolated temporary Git object plane using the carried URL and restricted
transport. Control verifies the advertised OID, bounded tree, and immutable
context policy there, then deletes the plane. Failure changes no source ref or
jj operation. A locally present OID takes the same proof path without a fetch.

1. Trusted absolute Git fetches the carried, identity-matching HTTPS/SSH URL
   with only that protocol allowed and writes
   `pull/N/head:refs/remotes/REMOTE/asha-control-pr-N`. The remote name is used
   only to name the controller ref, not reread for transport. The exact local
   config digest must still match preflight; credential helpers/prompts,
   `protocol.ext`, URL rewrites, repository SSH commands, custom upload-pack,
   filter/diff/merge helpers, and includes are disabled or refused. A private
   PR that needs a credential helper fails closed and asks the operator to
   fetch and verify the head manually before retrying.
2. `jj -R SOURCE --ignore-working-copy git import` records the Git import in
   the jj operation log and surfaces the head as an untracked *remote*
   bookmark.
3. Task preparation creates the separate task workspace and empty change on
   the validated `headRefOid`.

The remote-tracking namespace is required, not incidental: jj only surfaces
refs from namespaces it tracks, so a commit reachable solely through a custom
namespace such as `refs/asha-control/*` never enters jj's commit graph and the
explicit-base rule could not be satisfied for a PR head jj does not already
know. Importing a remote-tracking ref creates an untracked *remote* bookmark
only — your local bookmark namespace gains no controller entry, and no existing
bookmark of either kind moves.

The fetch never checks out a tree. The pre-mutation Git `HEAD`/jj `@-` guard is
what makes the following import safe and keeps Git `HEAD`, staged content, and
source `@` untouched; Control refuses and asks the operator to run `jj status`
when those positions diverge. Issue mode performs its bounded `gh issue view`
read, resolves an omitted default from exact Git before the common reported
import, and then prepares from the pinned OID. An explicit existing-jj base
likewise resolves its arbitrary jj revset during universal preflight before
import and prepares only after the pinned OID is revalidated. Issue mode does
not fetch.

Control has no GitHub write route. It does not comment, edit, label, close,
review, merge, create a PR, or push. Subprocesses are argv-only, shell-free,
deadline-bound, and byte-capped.

## Data preservation

### Controller materializations

Orchestration may call the library-only
`lib.control.prepare.plan_materialization(config, source, name)` seam to resolve
the deterministic repository identity, workspace name, and target path without
mutation, then call
`lib.control.prepare.prepare_materialization(config, source, base_commit_id, name)`
to create a fresh, explicit-base jj workspace for controller verification.
It uses the same pinned-operation workspace-add primitive, canonical workspace
root and repository namespace, path checks, private `0700` directories, and
durable phase journaling as task preparation. The retained workspace lives at
`<workspace-root>/<repo-key>/materializations/<name>`.

A controller materialization registers no Control task or run, starts no tmux
session or harness, and receives no task context marker. Success returns only
`workspace_name`, `workspace_path`, `change_id`, and `working_commit_id`.
Failure preserves the journal and any ambiguous materialization for inspection.
After an initiative's archive outcome and inventory are durable, Control forgets
its authenticated task and materialization jj registrations through each source
repository. Materialization cleanup verifies the private creation journal and
exact live change/commit identity; task cleanup also requires the bound terminal
attempt, owned workspace root/marker, and terminal process evidence. A reused
name, uncertain process, or changed identity refuses cleanup. Run
`asha initiative reconcile ID` to finish an interrupted archive release after
resolving its refusal. Workspace directories, journals, seals, and
verification evidence remain available; there is no materialization deletion route.
`asha task doctor` reports `stale-workspaces` as an advisory warning for `asha-*`
registrations without a live owner. It never forgets registrations. Operator
workspaces and other initiatives' registrations are not archive cleanup targets.

Task creation and controller materialization inspect the selected Git tree
with one bounded metadata read. Verification streams workspace files through
the repository's Git object algorithm and compares their object IDs; it does
not invoke Git once per blob or retain tracked content in the creation journal.
Creation journal v2 stores only a compact tree-plan digest and summary. Exact
per-entry inode ownership is held in a private, atomic, digest-bound binary
sidecar under the Control transactions directory. This supports trees above
the v1 1,024-entry and 16/64 MiB hashing ceilings while preserving exact
content, mode, inode, and foreign-file evidence. V2 automatic recovery retains
that evidence, the jj registration, and all workspace/root state for manual
inspection rather than using it to mutate names or delete filesystem entries.
Archive and explicit prune are suggested only when their existing preconditions
are durably proven. Existing v1 inline journals
remain readable under their frozen automatic-recovery behavior.

Archive requires an ended task, a running task whose reconciled runs are all
terminal (`exited` or `failed`) and unblocked, or a failed task with no run or
only terminal runs (a creation that rolled back, or an interrupted creation
recovered without a live process, including a v2 workspace retained for
explicit cleanup). At that terminal edge Control persists the
reconciled run state and bounded evidence, then changes only the task's
registry lifecycle. Archive is reversible with
`asha task unarchive <selector>`, which restores `ended` (or `failed` for a
task that never had a run); the jj workspace, change, ignored files, tmux
history, and source repository remain. Stop verifies task, pane, process start
identity, and tmux ancestry, then sends `SIGINT` or explicit `SIGTERM` to that
process only. It does not kill the tmux session, archive the task, or touch jj.

### Pruning archived tasks

Task archive preserves everything, so `asha task prune` is the only route that
reclaims what an archived task leaves behind: its dead tmux session, its jj
workspace registration, and its workspace directory. The task record is not
modified and stays archived; described or non-empty jj changes remain in the
source repository (jj discards only an empty, undescribed working-copy commit
when the workspace is forgotten). Prune changes no stored task fact; after
it, `asha task show` reports the same record with live jj evidence `missing`.

Prune is per task or `--all` (every archived task), and it is idempotent:
each pass re-derives everything from live state, so an interrupted pass is
finished by running it again. Per task, in order:

1. Only an `archived` task is eligible; anything else is refused unchanged.
2. The tmux session is killed only when it exists, carries this task's
   `@asha_managed`/`@asha_task_id` options, and every pane in it is dead. A
   live pane refuses the whole task (unarchive and stop it first). A session
   with foreign ownership is left alone and reported.
3. Unless `--keep-workspace`: the workspace is removed only when the creation
   journal owns its root inode (device, inode, owner), the workspace's own
   `.asha/control-task.json` marker names this task, no other task record
   whose own root was not already reclaimed claims the same path, the path
   lies below `control.workspace_root` without
   symlink components, the source repository is readable, and no orchestration
   attempt bound to the task (by link or by reserved task id) is still
   non-terminal (an `indeterminate` or `result-missing` attempt may still be
   sealed from that workspace). All of that is verified before prune runs
   `jj workspace forget` through the source repository and then removes the
   tree by descriptor-anchored, non-following deletion that refuses foreign
   ownership, device crossings, loops, and mount points. Refusals keep the
   workspace and name the reason.

Removal is journaled in
`${ASHA_HOME:-~/.asha}/state/control/prunes/<task-id>.json`
(`asha.control-prune-record.v1`): intent before the first unlink, completion
after the root is gone. A later pass treats a completed path as absent for
the pruned task even when a successor task with the same slug reuses the
directory and its inode number; the marker and registry checks refuse the
successor independently of that record, and a directory whose marker names
the successor is reported as that task's, not as residue. A removal that
stops midway (for example on a read-only subdirectory) leaves the partial
tree in place and reports the reason; fix the cause and run prune again, and
the recorded intent lets that pass finish even though `.asha` may already be
gone.

Because removal is destructive to ignored files inside the workspace (results,
build output, notes), an interactive prune confirms once for the whole batch;
non-interactive and `--json` callers must pass `--yes`, `--dry-run`, or
`--keep-workspace`.
`--dry-run` reports every planned action without touching tmux, jj, or disk.
`--json` emits `asha.control-task-prune.v1`. Exit `2` when any selected task
was refused or only partially pruned, `0` otherwise. `asha task doctor` reports
how many archived tasks still hold a session or workspace in its `prunable`
probe; that is information, never a failed check.

Before any workspace/root filesystem mutation may have occurred, transaction
recovery can discard the creation claim without retaining filesystem state.
Once a v2 mutation may exist, automatic recovery does not unlink or rmdir
workspace entries, the workspace root, or created parents. It may authenticate
the observed jj workspace registration for diagnostic purposes, but never
forgets it; name-based forget has no atomic identity predicate. Recovery marks
the journal `preserved` and task `failed` and retains the registration, bytes,
root, and created parents for manual inspection. The diagnostic names
`jj -R SOURCE workspace list`, the workspace path, and every recorded created
parent. It suggests archiving the failed task and running explicit,
user-confirmed prune only when the existing prune preconditions are durably
proven. Partial-add state without a root fact and created-parent residue require
manual cleanup instead.
Frozen v1 journals retain their historical ownership-checked automatic removal
behavior. After launch is possible, every failure path also preserves the
workspace and records recovery facts. Outside `asha task prune`, Control never
substitutes raw recursive deletion, destructive Git, or unreviewed jj
abandonment for a removal design; prune itself removes only a journaled
workspace root through the ownership checks above.

### Interrupted creation

Ctrl-C and SIGTERM during `asha task start` run the same rollback-or-preserve
handler before the original interruption reaches the CLI. A hard process exit
can still leave a durable `creating` record and creation journal. Recover it
explicitly with:

```text
asha task recover <task-id|exact-slug>
```

V2 pre-launch phases retain workspace/root filesystem mutations and the jj
registration, then report exact registration/path inspection. Archive/prune
commands appear only when their existing preconditions are durably proven;
partial-add and created-parent residue instead report manual cleanup. Only
claims that cannot yet have mutated those paths finish as clean rollback. A
phase at or after `launch-attempted`
never kills the session or process: Control marks the task failed, preserves
the workspace, reports the exact attach/show commands, and requires the
operator to stop any live harness manually. The `transactions` doctor probe
names interrupted creation records and the command for each. Frozen v1
journals continue their original ownership-checked rollback path.

One historical retained shape has a separate forward-only recovery path: a
failed, runless v2 task whose preserved journal is still `add-intent`, has no
root/registration/materialization/context ownership facts, and never reached a
launch attempt. It is not adopted automatically. The operator must reauthorize
the exact durable goal and first-run harness/role:

```text
asha task recover <task-id|exact-slug> --adopt --yes \
  --harness H --role ROLE --goal 'the exact durable task label'
```

Under the same task, source, then repository lock order used by ordinary task
creation, this command authenticates the
exact source/Git/jj bindings, verified colocation record, immutable base plan,
mode-0700 root, registration/change/commit/parent/description, empty change,
public jj operation ancestry, streamed workspace bytes, and raw task/journal
digests. It then durably records an adoption intent and ownership sidecar,
provisions context forward, narrowly reopens only that failed creation, and
launches through the ordinary launch controller using the operator-supplied
parameters. Every intermediate adoption phase is resumable by the same exact
command. It never forgets a workspace registration or removes a path. A shape
or evidence mismatch remains preserved for manual inspection, and ordinary
`recover` plus all other failed tasks remain terminal. Doctor and the TUI show
the adoption command only for the exact durable candidate; ambiguous retained
residue is labelled manual inspection only.

### Read-only chair observation (U1a foundation)

`asha initiative inventory --json` is the bounded startup-consumer API, **not**
`list --all`, `attention`, or the full TUI assembler. It reads atomic snapshots
without acquiring write-enabled registry/transaction locks, migrating layouts,
reconciling, or creating files. It reports retained non-ended Rooms with observed
ownership status, actually live owned task runs (not historical task lifecycle
labels), nonterminal initiative heads, pending approvals/needs-input heads, and
unacknowledged addressed message IDs. It never renders criteria, plans, message
bodies, or whole event history. The Codex chair startup below consumes this
API; other harnesses and the TUI retain their existing startup behavior.

Defaults and hard upper bounds:

- 50 output rows overall (`--rows N` may narrow this).
- 256 scanned directory entries per source, including hidden/invalid entries.
  Nested approvals/messages/coordinators share their source budget across all
  sampled initiatives. Each sampled message permits two direct receipt lookups.
- 64 KiB total serialized UTF-8 JSON, including escaping and the final newline.
- Two-second **cooperative** deadline. One bounded tmux inventory subprocess uses
  the remaining deadline and a 64 KiB capture cap; subsequent ownership checks
  reuse that sample, with any fallback probe sharing the same deadline.

Every source reports scanned entries, unavailable records, truncation, and an
`observed_count` explicitly labelled **lower-bound**, never an exact total.
Registry iteration is filesystem order, not a historical sort or pagination
promise. Output-row/byte limits can hide sampled matches. Incomplete initiative
coverage also makes nested-source coverage incomplete. Snapshots from different
sources are not a transactionally consistent global view. Inaccessible tmux
counts as unavailable, not “zero running tasks.”

Supervisor status separately reports `status: running|stopped|unavailable` with
exit codes `0|1|2`; unavailable has `running: null`, not false. The owned regular
lock is opened descriptor-relative, no-follow, **O_RDONLY**, and probed with
nonblocking flock. Its inode is rechecked after the probe. Linux local-filesystem
flock supports this without a writable file descriptor. A filesystem/platform
that rejects it (including writable-descriptor requirements), inaccessible
process evidence, a held lock without a verifiable PID, or a live recorded PID
without a matching held lock is unavailable.
Start, stop, and service install refuse uncertainty; they do not launch duplicates
or signal a PID on that basis. A status probe does not create a missing lock.

These are POSIX/Linux observation seams, not a portable process-namespace oracle.
A hidden PID namespace cannot prove a held lock's owner has exited. Filesystem
syscalls and kernel stalls cannot be preempted by a Python monotonic-clock check;
the deadline limits cooperative work and external probes, not worst-case kernel
latency. Platform flock behavior is not inferred from Linux tests.

**Execution consent remains separate.** Native Codex approval/sandbox settings
may still refuse a command before Control runs, even when it is read-only. This
foundation changed no execution rules, approvals, hooks, wrappers, or worker sandbox.
A Control authorization check is not native execution consent, and native hooks
are not complete enforcement of every execution seam. The U1b/U3 candidates
below do not close native-consent or chair-owned acceptance; U4–U8 remain open.


### Bounded Codex chair startup (U3 candidate)

After **successful actual no-argument seat entry**, `bin/asha` passes a generated
current-activity observation as Codex's native initial positional `PROMPT`. The
launch decision is local and non-exported, not caller-provided `ASHA_SEAT`.
The prompt contains only fixed headings, a UTC observation timestamp/freshness,
observed lower-bound counts and coverage status; it never includes retained
labels, objectives, commands, message bodies, or criteria. It is at most 4096
UTF-8 bytes, including unavailable evidence, and uses the existing inventory's
unchanged or narrower IO bounds. Missing, capped, unavailable and known-empty
observed registries are distinct. A known-empty sample is not an atomic proof
of absence. No record text is executed.

Explicit arguments retain their exact argv and caller cwd. `--yes`, failed seat
entry, persona-off, Rooms, managed workers and coordinator launches receive no
startup prompt. Other harnesses are unchanged. There are no new hook, sandbox,
approval, terminal-typing, service or generic transport mechanisms.

The seat tests use harness fakes and establish only argument delivery and
exclusions. **Visible rendering inside a real native Codex conversation remains
chair-owned acceptance**; neither those fakes, pre-exec scrollback, instructions
files nor an assistant paraphrase establish it. This slice does not claim the
foundation integrated or the read-only transport-consent question resolved.

### Global initiative action pages

On an active SQLite registry, **G** opens independently paged initiative decisions
and approval requests, including those outside the retained tree sample. **M**
handles managed questions and native permissions, with Next, Retry and Refresh.
Global pages provide those controls plus family switching. Select a candidate,
press **r** to review its exact subject, then **r** to type an offered decision:
approve/reject a plan or salvage/review-retry request, activate an approved
initiative, or resume an open legacy initiative question. Resume records that
question as resolved; it does not send a text answer. Integration candidates
remain inspection-only. Decisions revalidate the displayed records and preserve
the page position. Partial reads and unreadable records remain visible. See
[managed session queries](managed-sessions.md) for binding and cursor semantics.
