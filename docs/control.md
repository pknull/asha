# Asha Control

This is the current operating guide for Asha Control. The delivered proposal
under `docs/proposals/` preserves the design history; live code and this guide
describe the supported release.

## Purpose and prerequisites

Control turns one unit of agent work into a persistent local task. A task owns
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
GitHub source modes additionally
require an installed, authenticated `gh`; ordinary ad-hoc tasks do not.

`asha task doctor` reports these local capabilities. Its `gh` probe is always
shown but is optional and never blocks ad-hoc task creation. Running it outside
a repository is also informational. Hook checks cover only the installed Claude
and Codex configurations, and live-event checks skip Copilot and OpenCode runs
because those harnesses claim process liveness only.

## Command surface

```text
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

asha control
asha control tmux
asha control event ...       internal hook-facing route
asha control supervisor {run|start|stop|status} [--json]
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

## Terminal TUI: one control tree

`asha control` opens a single tree in the current terminal: every non-archived
initiative (expandable to its nodes and attempts, each showing its linked
worker's live state inline) followed by an **Unbound tasks** branch holding
Control tasks bound to no initiative. With no initiatives on screen the branch
flattens and the tree is exactly the task list. Keys act on the selected row's
kind:

| Key | Action |
|---|---|
| `Up`/`Down`, `Right`/`Left` | Move; expand or collapse (`Left` on a child returns to its parent). |
| `Enter` | Initiative row: attach its coordinator session. Node/attempt/task row: open the worker's tmux popup. |
| `!` | Show only rows waiting on a human (plan approvals, needs-input, workers at prompts, published-awaiting-exit). |
| `n` | New intent: Control starts a coordinator session at the projects root with your intent as its first message. |
| `N` | Open the ad-hoc task-start form. |
| `X` | After `yes`, send the worker's quit command (`/exit`, `/quit`) into its pane **as your keystroke** — for a published worker whose seal awaits its normal exit. |
| `a` | Initiative row: decide a pending plan approval (`approve`/`reject`). Task row: archive after confirmation. |
| `x` | Task row: controller-revalidated context actions. |
| `r` | Initiative row: reconcile it. Task row: reconcile the task. |
| `d` | jj diff summary of the selected row's linked workspace. |
| `e`, `c`, `v`, `t` | Initiative panes: events, candidate seals, review + verification evidence, retained storage. |
| `p` / `s` | After `yes`: pause/resume the initiative / stop the selected attempt's task. |
| `A` | Toggle the `active` / `all` lifecycle scope for tasks. |
| `/` | Filter rows. `?` help. `q` exits the TUI only. |

`asha initiative attention [--json]` is the CLI twin of `!`: one list of
everything waiting on a human across initiatives and tasks, each item naming
its resolution. The tree and the verb share one assembler and cannot disagree.

The default `active` scope does not load or reconcile archived tasks. `A`
switches to `all`; archived records use their durable lifecycle projection and
display `archived` even after their session and workspace have been pruned.
The title always names the current scope. Scope reloads preserve the selected
task when it remains visible, and the text filter remains independent.

### Tree mechanics

The view is text only, works on an 80x24 monochrome terminal (narrow widths
drop the middle columns, never the attention column), and reads orchestration
state through the same typed controller functions the CLI uses (`snapshot`,
`show_payload`, `reconcile_one_initiative`, `approve_plan`, `reject_plan`,
`submit_action`); it duplicates no lifecycle logic. Orchestration is imported
lazily; a malformed orchestration configuration degrades the initiative branch
to an inline note and leaves the task branch fully usable. The five-second
automatic refresh reloads the whole tree with lock-free snapshot readers.
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
| `p` | After `yes`, pause a running initiative or resume a paused / needs-input one. |
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

While open and outside a modal prompt, the TUI schedules reconciliation of all
displayed tasks at a five-second monotonic cadence. An already-read key is
dispatched before a due refresh. The refresh itself runs synchronously in the
TUI loop: each external adapter call has a deadline, but the length of the
whole pass scales with the displayed task and run count, so a key arriving
after a pass begins waits for that finite pass to finish. The TUI creates no
daemon, thread, or runtime supervisor for automatic reconciliation, never
queues missed refreshes, and reports adapter failures in the status line. The
task-start modal's bounded, signal-owned child is the deliberate exception.
Filter input, task actions, the task-start form, and confirmations pause automatic
reconciliation until the modal closes. It does not start the separately managed
Control supervisor; the operator starts that process explicitly. A later
successful automatic pass clears only its stale
automatic-refresh diagnostic; operator action and skipped-registry messages
remain. `r` remains the explicit selected-task refresh.
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
`setupterm()` check succeeds. If any preflight check fails, `asha control`
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

The monitor is the front door. In Initiatives mode, `n` asks for an intent and
Control starts the coordinator as its own tmux session at the projects root
(`ASHA_PROJECTS_ROOT` or the monitor's working directory): a full-persona
`asha claude` whose first message is the intent. That session runs the
`orchestrate-initiative` skill, resolves the repository through
`asha initiative projects`, creates and claims the initiative from its pane,
and proposes the plan. The popup opens on it immediately; `Enter` on the
initiative row reattaches later (on a node row it opens the worker popup as
before). Approvals stay in the monitor (`a`); the coordinator's own pane is
refused. CLI equivalents: `asha initiative coordinator launch [--root DIR]
--intent TEXT`, `coordinator sessions`, and `coordinator attach ID |
--session NAME` (popup inside tmux, otherwise the attach command is printed).
Coordinator sessions are named `<session_prefix>coord-<token>` on Control's
default tmux server, carry `@asha_coordinator_session=1`, and are never
Control tasks: prune and task listing ignore them; they end when the harness
session exits.

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
and one level below `DIR`). Inside tmux the window is added to the current
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
tree is the only view, so it opens the same screen as `asha control`.

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
There is no materialization deletion route.

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

Archive preserves everything, so `asha task prune` is the only route that
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
