# Changelog

This file preserves the release records formerly embedded in the active README
and engineering guide. Historical entries describe the implementation at the
time of release; they are not current operating instructions. For current
behavior, use README.md, CLAUDE.md, and the owning plugin documentation.

The two source histories are retained separately below so moving them out of
the active instruction surface loses no release detail.

## Public release history

### Unreleased

#### Project roots and friendly names

- `asha initiative projects` indexes several roots. `--root` repeats, and with
  none given the roots come from `project_roots` in `~/.asha/config.json`,
  then `ASHA_PROJECTS_ROOT`, then the working directory. Output groups by root
  and leads with the jj-colocated projects, since only those can run an
  initiative; a root that cannot be indexed is reported rather than failing
  the listing.
- A project may state a friendly `name` in its own `.asha/config.json`. The
  index shows it and keeps the directory in the additive `directory` label,
  falling back to the directory name when the stated one is unbounded,
  unprintable or absent. `--match` stays exact and now answers to the friendly
  name as well.

#### Colour and the pipeline rail in the control tree

- The control TUI never asked curses for colour at all (no `init_pair`,
  `A_BOLD` or `attron` anywhere), and its 10-wide `STATE` column truncated
  `awaiting-plan-approval` and `ready-for-integration` — the two states meaning
  the operator is the blocker — into near-identical stubs. `lib/control/
  tui_style.py` now maps all 56 record states to five semantic tiers coloured
  by whose turn it is, gives every initiative a six-stage pipeline rail
  (plan · approve · build · review · verify · integrate), shows short labels
  that never truncate, and puts auditable counts in the title. Rendered lines
  are a `str` subclass carrying tier spans, so the renderer stays
  terminal-independent and every existing caller keeps treating them as text;
  only the painter reads spans. Monochrome terminals keep bold on the two loud
  tiers, 8-colour terminals get ANSI approximations, and a CJK locale selects
  an exact-width ASCII rail. A stage is ticked only on the evidence of its
  record, so an initiative cancelled at draft claims nothing; the title's
  counts come from the rendered rows, so a filter narrows them together; and
  every cell keeps the `safe_text` sanitisation and the column clearance the
  retired renderer applied.
- `a` in the control tree now performs whichever operator act the selected row
  waits for — approve or reject a plan, activate an approved initiative, or
  archive a terminal one — so an approved initiative no longer sends the
  operator to the CLI to start it. A live run also fixed: refused actions
  reported as successes (the outcome key is `status`, not `state`), `draft` and
  `approved` initiatives counted as "settled", amber rows with no stated
  demand, and `ready` and `partial` silently falling through to the inert tier.

#### Standing authorities

- `asha initiative authority add|list|revoke` records the operator's
  pre-signed approval for a narrow plan shape
  (`asha.orchestration-standing-authority.v1`: pinned repository identity,
  scope prefixes, node/harness/attempt ceilings, optional headless
  requirement). A deterministic fail-closed matcher runs at proposal time;
  matched plans are approved as the operator by proxy
  (`standing-authority:<id8>`) with an `approval-decided` journal event, and
  optionally activated. Mismatches wait for the operator; grant and revoke are
  refused from coordinator sessions and panes (with a matching
  `coordinator-no-authority-grant` policy-guard belt); integration, salvage,
  decisions, and needs-input stay live operator acts. With a time trigger this
  closes the bounded autonomous loop while keeping gates and integration
  manual.

#### Time triggers

- `asha trigger add|list|remove` schedules coordinator launches via systemd
  user timers (marked owned units; foreign units refused; Persistent
  catch-up). Fired runs stop at plan approval — triggers schedule proposals,
  never unattended execution. Webhooks deliberately deferred.

#### Headless nodes

- A plan node may declare `interactive: false` (work/review on Claude or
  Codex): Control launches the worker headless in its pane (`claude -p
  --permission-mode bypassPermissions`, `codex exec`), the turn's end is the
  normal exit, and the seal follows without a human closing the session.
  Interactive briefs now say honestly that the worker cannot exit itself and
  must ask the operator to close it (`X` in the control tree).

#### One control tree

- `asha control` is now a single tree: initiatives expand to nodes and
  attempts with their workers' live states inline (prompt-stuck and
  published-awaiting-exit workers are visible where the work is), unbound
  tasks sit under one branch (flat when no initiatives exist), and `Tab`'s
  mode split is retired. New keys: `!` filters to rows waiting on a human,
  `X` sends a published worker its quit command as the operator's keystroke,
  `N` opens the ad-hoc task form (`n` remains new-intent). `asha initiative
  attention [--json]` is the CLI twin of `!`, sharing one assembler. Narrow
  terminals keep the attention column and drop the middle ones.

#### Repair assignments carry their findings

- A repair dispatch now embeds the accepted review findings bound to the exact
  upstream seal into the worker assignment ("Accepted review findings to
  fix") — the first live repair showed the worker otherwise repairs blind,
  the gap the original evidence-gate review predicted.

#### Workspace trust and waiting workers

- Control now inherits harness trust for the workspace it creates: when a
  task's source repository is trusted in any harness store, the new workspace
  is trusted in all of them (Claude, Codex, Copilot; OpenCode has no gate), so
  no run is blocked at a trust prompt again. A source trusted nowhere grants
  nothing. Every grant is reported as a `workspace-trust` source mutation, on
  `attempt-started` and the dispatch outcome, and in the
  `asha.control-workspace-trust.v1` ledger; `asha task trust [PATH] [--grant]`
  reports or performs one, and `control.workspace_trust: "never"` disables it.
- Claude's trust dialog and permission prompts are recognized, so a waiting
  Claude worker reconciles as `needs-input` instead of `running`.
- Assignments state that a worker must end its session after publishing: the
  seal needs a normal exit, and a killed attempt seals as a failure.

#### Coordinator sessions from the monitor

- `n` in `asha control` Initiatives mode asks for an intent and starts the
  coordinator as a Control-owned tmux session (`<prefix>coord-<token>`, full
  persona `asha claude` at the projects root, intent as the first message),
  opening its popup; `Enter` on an initiative row reattaches to its
  coordinator. `asha initiative coordinator launch|sessions|attach` are the
  CLI forms. The cockpit remains as a two-pane alternative.

#### Cockpit and project index

- `asha cockpit [DIR]` opens the coordinator's `asha claude` pane beside
  `asha control --initiatives` in one tmux window (new window inside tmux,
  named session outside; `--dry-run` prints the plan). `asha initiative
  projects [--root DIR] [--match TEXT]` is the coordinator's read-only project
  index: the declared workspace manifest when present, otherwise jj-colocated
  Asha projects found at and below the root (bounded). The
  `orchestrate-initiative` skill resolves intents through it (session plugin
  2.1.2). The cockpit preflights `asha doctor claude`, `asha initiative
  doctor`, and the project index; `asha doctor <harness>` now names source
  skills missing from an install (and `--fix` links them).

#### Dogfood fixes

- `orchestrate-initiative` ships `plan-template.json` plus a fill recipe so the
  coordinator proposes a plan instead of re-deriving the schema from the
  reference document (session plugin 2.1.1).

#### Orchestration Increment 7: workspace-scoped initiatives

- `create --workspace PATH` binds a declared `.asha/workspace.json` workspace;
  new initiatives persist `asha.orchestration-initiative.v2` with
  `scope.kind` `repository` or `workspace` (v1 records unchanged). Workspace
  plans name exactly the members, one terminal candidate producer and one
  required review gate per member, and one verification gate over all
  reviews. Dispatch targets each node's member root; activation refuses
  member identity or manifest membership drift. Verification
  materializes one fresh workspace per member at its exact terminal seal with
  `verification-member` evidence; the bundle digest binds the ordered member
  seal set; readiness binds one multi-member bundle; storage reports merge
  member roots with `repository_id` labels. Archive retains every member
  materialization.

#### Orchestration Increment 6: Initiatives mode in `asha control`

- `Tab` switches the Control TUI between Tasks and a text-only Initiatives view
  with per-mode key tables: expand/collapse the initiative tree, open a node's
  linked task popup, reconcile, jj diff, events/candidates/verification/storage
  panes, operator plan approval or rejection (typed `approve`/`reject`),
  confirmed pause/resume and stop-attempt. The view reads through the same
  typed controller functions as the CLI (`snapshot`, `show_payload`,
  `approve_plan`, `reject_plan`, `reconcile_one_initiative`, `submit_action`),
  loads orchestration lazily so a malformed configuration degrades this view
  only, and exposes no forbidden action path.

#### Orchestration Increment 5: the bounded active coordinator; persona-free workers

- The coordinator actor may now submit exactly `dispatch-node`, `repair-node`,
  `request-salvage` (request only), `stop-attempt`, `pause`, `continue-node`,
  and the new `request-decision`, `propose-outcome`, and `directive` classes;
  `dispatch|pause|stop --as-coordinator` from the anchored pane. Its expected
  revision may be behind the current one (ahead is refused; executors re-check
  the bound records under the lock); operators keep exact matching. Links
  carry the coordinator generation. `checkpoint --file` replaces the
  generation's CAS-guarded `asha.orchestration-coordinator-checkpoint.v1`.
  `resume` also returns a `needs-input` initiative to running. Directives are
  recorded as pending with deterministic fallbacks; the controller never types
  into a pane. Control-launched workers receive `ASHA_PERSONA=0` and the
  launcher skips the identity render for them (operational layer and Codex
  trust injection kept).

#### Orchestration Increment 4: Asha claims the coordinator role

- `asha initiative coordinator claim|release|show`, `wait`, and `propose-plan`.
  The operator's Asha session claims one coordinator generation per initiative
  from its own tmux pane; the pane's pid and process identity anchor the
  generation, a newer claim fences the predecessor, and every coordinator-actor
  verb re-proves the anchor. Coordinator action documents carry the generation,
  are fenced in the action journal, and are refused for every class until
  Increment 5. `approve`, `reject`, `approve-salvage`, and `decide` refuse the
  coordinator actor and pane; the policy guard gained `require_env` and a
  matching deny rule. Reconcile marks a vanished anchor stale; the doctor's
  `coordinator-seam` probe is advisory. Session plugin 2.1.0 adds the
  `orchestrate-initiative` skill (Asha as the front door).

#### Orchestration: exact retained-plan observation compatibility

- Digest-valid Increment 1 `asha.orchestration-plan.v1` records whose
  verification gates have exactly `{kind,node_id,required}` can again be read
  through `plan --show`, `show`, and `snapshot` without rewriting their bytes,
  changing their digest, or extending the closed output contracts. They remain
  observation-only: current plan creation, save, approval, activation,
  dispatch, resume, repair, continuation, and controller verification require
  immutable commands and the minimal environment policy and refuse before any
  execution effect. Ordinary terminal containment remains available.

#### Control: popup attachment keeps child tmux outside nesting state

- Popup argv now clears `TMUX` only in the `display-popup` child before its
  inner `tmux attach-session`. A fixed `sys.executable` wrapper clears the
  child variable and immediately `exec`s the unchanged tmux argv, avoiding the
  newer `display-popup -e` option so tmux 3.2/3.2a popup support remains valid.
  The Control process, caller client binding, socket selection, and
  `TMUX_PANE` handling remain unchanged. A failed popup
  attach returns one actionable manual-attach diagnostic to CLI or TUI callers
  instead of being reported as a normal popup close. Successful operator
  detach remains a normal close and never stops the task.

#### Control: omitted bases follow the current local branch

- Omitted task bases and initiative baselines now share one read-only exact-Git
  resolver: the current attached local branch wins, followed by remote symbolic
  `*/HEAD` targets that agree on one OID, then conventional local
  `main`/`master`/`trunk` refs that agree on one OID. Different fallback OIDs
  refuse before mutation. This prevents a stale packed remote branch from
  overriding the operator's current loose local branch. Explicit bases remain
  verbatim, while the legacy default revset string remains only the durable v1
  omitted-request/replay identity.
- The TUI resolves the empty Base row after repository selection, displays
  bounded ref name(s) and an abbreviated OID, and passes the full OID only as a
  freshness assertion. Controller re-resolution remains authoritative and a
  preview race refuses before launch. A failed preview cannot submit an empty
  Base, and resizing while editing Base refreshes the preview. Successful
  CLI/TUI starts name the full authoritative base OID. Retry omits `--base` only
  for the stored legacy default sentinel.
- Exact Git now disables promisor-object lazy fetching for every sanitized and
  bound read, preventing partial-clone configuration from turning default or
  baseline inspection into network access. Default refs use a bounded complete
  Git-ref grammar validator, and commit output must be one unpadded ASCII line.

#### Control: verified colocation survives device renumbering

- Strict v1 `verified` colocation records now recover from coherent filesystem
  device-number changes after reboot or remount. Control requires exact
  non-device root/Git-marker/target/`.jj` facts and an injective device map,
  then runs the existing path, Memory, base, destination, strict jj/sync,
  stable all-ref semantic, and stable-operation authentication chain before a
  source-locked exact-record rewrite refreshes only the stored device values.
  Doctor reports safe candidates read-only. Intent records, partial or
  collapsing maps, mixed permission/device drift, and all other mismatches
  still fail closed. The refresh mutates neither Git nor jj state; device and
  inode remain exact authority within each live transaction.

#### Control: immutable context preflight and retained-creation adoption

- A missing committed `/.asha/control-task.json` ignore now produces typed
  immutable-base evidence before every Git-backed start mutation, including
  existing-jj Git import. The TUI offers an explicit source-only `.gitignore`
  patch, instructions, or cancel without parsing stderr or losing its form;
  apply revalidates repository/base/project/proof/preimage facts, creates no
  task or jj state, never commits or retries, and keeps the old base refused.
  `/session:init`, drift, and `task doctor` now install or diagnose the rule.
  PR heads already local use the same early proof; remote-only heads are proved
  in a disposable object plane before any source fetch, colocation, or jj
  import, then re-proved in source before import. Apply rejects oversized or
  nested-negated intended patches before rename, creates its temporary with
  authenticated dirfd-relative operations, and classifies every ordinary
  rename-attempt exception as indeterminate. Apply/default/worker refusals retain the iterative
  five-field TUI draft and render a bounded form-local result before the next
  key can acknowledge it; blank-default changes require a second acceptance.
  SIGTERM/SIGHUP still exit with their signal status and warning across the
  rename boundary, while KeyboardInterrupt/SystemExit propagate unchanged.

- Task start now proves context compatibility from the immutable selected base
  before colocation, task state, destination parents, or workspace registration.
  Regular tracked `.asha`/Memory context files are schema-checked and reused
  without an ignore requirement; every path Control will create requires
  positive selected-tree ignore coverage. The proof binds each exact generated
  plan path and the complete fixed `Work/session-state/` private subtree rather
  than one sentinel filename. Nested rules, negations, spaces, and Unicode are
  evaluated in a private synthetic Git namespace with mutable worktree, info,
  global, and default excludes disabled.
- Immediately after a successful workspace add, Control persists the exact
  private root, change/commit, registration, public add/checkout operation
  ancestry, streamed materialization, and ownership sidecar before later
  context work. The same observation is attempted when jj reports an error
  after exact registration, without claiming an unregistered collision.
- The one historical failed/runless v2 `preserved`/`add-intent`/no-launch shape
  can be completed forward only through `task recover --adopt --yes` with an
  explicitly reauthorized harness, role, and exact durable goal. Adoption
  binds raw task/journal and verified-colocation digests, exact repository,
  immutable tree, private root, registration, empty change, and operation
  evidence before recording ownership and provisioning context. Its durable
  phases resume with the same command, then use the ordinary launch controller.
  Adoption takes task, source, and repository locks in ordinary creation order
  and rechecks its exploratory records under all three. It never forgets or
  deletes. Ordinary failed tasks remain terminal; doctor
  and the TUI distinguish the exact candidate from manual-inspection residue.
- Control task, source, and repository transactions now use domain-separated
  deterministic physical lock keys with enforced task/source/repository
  nesting. A caller-chosen task UUID cannot alias an internal repository or
  source lock.

#### Control: terminal task lifecycle actions

- The TUI defaults to an explicit active scope; `A` toggles all history, where
  archived records retain an `archived` lifecycle projection even after prune.
  Default refresh no longer reads archived live resources.
- `x` opens a controller-revalidated context menu for inspect, archive, retry,
  owning-initiative reconciliation, and archived-task prune. Archive and prune
  are separate exact-confirmation operations; prune uses the shared dry-run and
  real `prune_task` assembly. No menu action implicitly dispatches, merges,
  integrates, or pushes.
- Ordinary terminal retry creates a fresh task UUID and bounded retry slug while
  preserving the recorded request and exact label. The slug encodes the full
  fresh UUID so distinct retry identities cannot alias. PR retries intentionally
  resolve the current PR head. Initiative-owned or ambiguous tasks refuse this
  retry and instead expose the shared single-initiative reconciliation order.
- Archived lifecycle is rechecked under the task lock before every live
  reconciliation; mismatched reservation/link scans and unreadable prune
  history now fail closed with bounded operator-visible errors.
- Active unowned task actions now expose only the evidence-valid signal matrix:
  SIGINT Interrupt plus SIGTERM Terminate while active, SIGTERM Finish while
  idle, and SIGTERM Terminate for unknown state. Exact confirmation and a
  second task/run/ownership read precede the shared `stop_task` controller.
  Initiative-owned active tasks instead journal one `stop-attempt` action with
  actor `tui`; action state, freshly read attempt state/refusal, and observed
  exit are reported separately. Signal confirmation states that it does not
  archive or remove the retained workspace/change.
- Exact confirmations now render safety-critical action/task/run identity,
  signal, preservation consequences, and exact-`yes` authorization as wrapped
  modal context above a short active input. Ordinary 24x80 and 24x120 views no
  longer crop that context; tiny views retain input and mark omitted detail.
- The task-start surface is now one resize-safe, grapheme/cell-aware stateful
  editor with frozen bounded repository, repository-base, closed harness, and
  observed-role candidates. Shift-Tab preserves earlier values, Escape starts
  no worker, and candidates remain suggestions subject to the existing
  controller authorization.
- Modal input now uses curses wide-character reads. Candidate raw identity is
  never replaced by sanitized display text, harness matching rejects any
  non-ASCII typed value, and one aggregate entry/UTF-8 budget bounds the frozen
  snapshot. Tiny frames reserve the active input before decorative rows.
- Exact owned `pane_dead` evidence now persists a terminal run even when tmux
  retained no exit status, overriding an older idle event. Missing, foreign,
  or unavailable pane evidence remains fail-closed.
- Initiative reconcile status now reports the actual reconciled action IDs and
  states instead of claiming that no dispatch occurred; recovery may replay an
  already-authorized indeterminate dispatch.
- `asha task start --slug SLUG` now separates workspace path identity from the
  unchanged task label. Its validated value participates in caller-ID replay
  identity and adds no record field.

#### Control: large-tree materialization and cell-aware task goals

- Task-start prompts now render the active logical suffix by terminal cell
  width, keep the cursor inside the reserved final column across narrow resizes,
  and preserve exact ASCII, CJK, combining, variation-selector/keycap,
  modifier, flag, and ZWJ-cluster goal text.
- New creation journals use a compact metadata-only Git tree plan plus a
  private atomic digest-bound inode sidecar. Workspace verification streams
  Git-object hashes without per-blob subprocesses or the legacy 1,024-entry
  and 16/64 MiB content ceilings. V2 automatic recovery now fails closed once
  any workspace/root filesystem mutation may exist: it retains the jj
  registration as well as workspace/root/created-parent state, performs no
  name-based forget or filesystem deletion, marks the task failed and journal
  preserved, and requires exact registration/path inspection. It names archive
  plus explicit confirmed prune only when existing prune preconditions are
  durably proven; partial-add and created-parent residue require manual cleanup.
  V1 journals retain their frozen 64-entry-checkpoint
  recovery behavior. Controller materialization and orchestration review
  consume the same metadata plan.
- Prompt cluster handling covers the complete bundled Emoji_Modifier_Base set,
  preserves supported person/man/woman laptop profession sequences, rejects
  unsupported typed ZWJ sequences and duplicate/standalone modifiers, and
  measures preloaded unknown ZWJ text conservatively. Solitary regional
  indicators occupy one cell and complete pairs occupy two.
- Terminal-text validation now rejects every nonprintable ordinary code point,
  including line/paragraph separators and Unicode noncharacters, consistently
  across editor, durable task, harness argv, and tmux argv boundaries. The
  narrow valid ZWJ/selector/keycap/modifier/flag clusters remain exact.

#### Control: cancellable TUI creation and automatic plain-Git colocation

- Plain-Git task start now runs every feasible read-only refusal before the
  durable colocation intent: shared source/workspace ancestry policy, published
  Memory, explicit Git base, prospective destination, and bounded capacity.
  Exact Git reads bind the inspected git-dir/work-tree and discard inherited
  repository-selection/config environment. The trusted absolute Git executable
  receives a minimal explicit exec environment, with read-time helpers such as
  `core.fsmonitor` disabled. Explicit ad-hoc/issue bases carry one immutable Git
  OID without jj reinterpretation. Omitted plain-Git bases choose an exact
  unambiguous remote default or local `main`/`master`/`trunk` before mutation;
  `requested_base` retains the caller's unchanged default expression while the
  selected ref remains preflight evidence and its immutable OID is carried.
  Existing jj revsets remain unchanged. PR metadata and one exact
  repository-matching HTTPS/SSH URL are also carried across colocation instead
  of rereading a named remote. Execution-capable transport/helper config and
  config drift fail closed; private-PR credentials requiring a helper must be
  fetched and verified manually. Split `extensions.worktreeConfig` local
  configuration also refuses automatic fetch because it is outside the carried
  single-file config digest.
- Semantic colocation authentication no longer runs Git status or diff, closing
  repository `.gitattributes` filter execution. It compares plumbing
  HEAD/branch/index/all-ref evidence, including normalized per-stage index
  flags and symbolic-ref targets, with descriptor-checked raw hashes for changed
  tracked and bounded untracked paths, while clean tracked content stays bound
  by exact index-cache facts without an AAS-scale content walk.
- A verified colocation record hardened only by removing group/other write bits
  can be reauthenticated on task start after strict root/backend/sync checks,
  stable two-pass Git semantics including all refs, stable jj operation identity,
  and an exact-byte/digest comparison under the exclusive Control source lock.
  Only `root_fact` changes. Doctor reports safe candidates read-only; all wider
  changes and cooperative Control races refuse without rewriting the record.
  A noncooperating same-UID process able to rename private source/state paths is
  outside this lock-based enforcement boundary.

- After the five-field TUI start form, creation runs as detached JSON in one
  signal-owned Python child while curses remains active. The modal shows
  `Esc cancel`, polls at 200 ms, sends at most one SIGTERM to the worker group,
  and classifies the result from the preallocated task ID's record/journal.
  Cancellation before any possible workspace/root mutation rolls back cleanly;
  later v2 pre-launch cancellation retains the registration and filesystem
  state and reports manual inspection (plus archive/prune only when already
  eligible). Possible process execution preserves resources and
  reports attach/recovery; normal completion wins over a late buffered Escape.
- New tasks accept an exact canonical plain Git root. After caller-ID replay
  checks and under a deterministic source lock, Control runs command-scoped
  no-auto-track `jj git init --colocate`, strict-preflights it, and reports a
  `jj-operation`. HEAD/branch, semantic index and staged/unstaged state,
  non-jj refs, dirty tracked content, every tracked path's lstat POSIX mode,
  and untracked bytes/modes are verified without reading clean tracked
  content. A durable root binding records `.git` directory identity or the
  exact `gitdir:` marker digest, canonical target, and target identity; verified
  state also binds a real `.jj` directory. It prevents waiters or retries from
  adopting an interrupted or retargeted repository. Linked Git worktrees are
  refused before intent creation, while submodule gitdir roots remain
  supported. Interruptions retain status 130 after their retained-state
  diagnostic. Raw index bookkeeping
  may change. Verified or ambiguous repository
  enablement is retained across later failure/cancellation and never removed
  by task rollback.
- Doctor remains read-only, probes `jj git init` capability, and tells supported
  plain-Git operators that task start will auto-colocate. It applies linked-
  worktree classification and the same ambiguous/stale intent refusal as task
  start without creating state. The historical no-auto-init proposal policy is
  preserved with an explicit superseding note.

#### Control: live Codex permission/Stop state and observation-driven TUI refresh

- Wired the live-proven Codex 0.147.0 `PermissionRequest` and `Stop` hooks to
  Control's existing `permission-requested -> needs-input` and
  `turn-stopped -> idle` event bridge without changing Codex trust or fence
  handling. The permission hook fired before the operator answered a real
  network escalation; the same turn subsequently delivered `tool-completed`
  and production `turn-stopped`.
- A verified Stop-derived `idle` now outranks approval text left visible from
  the completed turn. Missing, stale, malformed, or unreadable semantic
  evidence projects `unknown` rather than presenting an old active state as a
  current live observation; durable terminal evidence remains terminal.
- A verified process plus an actual missing/SessionStart semantic event may
  remain `starting`; an unavailable event adapter reports `unknown` rather
  than treating unsupported observation as proof of startup state.
- State and winning provenance are selected in one reconciliation pass while
  the frozen v1 evidence shape remains unchanged. The TUI names source,
  observation time, and freshness; running-task AGE uses observation time
  rather than task mutation time.
- The TUI schedules reconciliation on a five-second monotonic local cadence
  and immediately after popup closure. Already-read input wins before a due
  refresh; a synchronous pass can still delay later keys by its bounded
  external adapter calls and task count. Modal prompts pause automatic
  reconciliation. Automatic refresh adds no daemon, thread, subprocess worker,
  or runtime supervisor; the task-start modal's bounded cancellation worker is
  separate. Refresh failures remain visible without ending the TUI.
- The bounded last-event server summary applies the same active-event age
  window as reconciliation, counting stale `working` or `needs-input` as
  `unknown` while preserving durable idle and terminal facts. Successful live
  list/show/reconcile/TUI passes mirror the derived primary state to an
  exactly owned pane/session and refresh the server summary once per batch, so
  cached tmux presentation cannot retain a stale positive after Control derives
  unknown. Each pass shares one sampled time across evidence and summary aging.
  Summary ingestion consumes a filesystem-order sample of at most 257
  directory entries and reports when the scan cap is reached; UUID snapshot
  names support neither a globally deterministic selection nor a newest-run
  claim.

#### Control: visible-pane needs-input fallback

- Reconciliation reads the last twelve visible lines of the owned, live Codex
  pane and reports `needs-input` when a known input-prompt marker is on screen
  (tmux evidence names the prompt). This remains the fallback when a
  `PermissionRequest` hook is missed or unavailable; a screen observation
  outranks an older working event but never a fresh permission event, a
  terminal event, or a dead pane.

#### Control: failed creations can be archived

- `asha task archive` accepts a `failed` task with no run or only terminal
  runs (a rolled-back or recovered creation), so it leaves the working list;
  `unarchive` restores `failed` for a task that never had a run. Contract
  amendment recorded: `archived` admits an empty `runs` list; no key changed.

#### Control: repositories that track `.asha/` or `Memory/`; fast rollback

- Workspace context provisioning reuses a tracked `.asha/` or `Memory/`
  directory and leaves tracked `Memory/*.md` or `.asha/*.json` files exactly
  as checked out (only the task marker, symlinks, and non-regular entries
  collide), so repositories that commit those paths can start tasks.
- Pre-launch rollback checkpoints its removal journal every 64 entries and
  fsyncs touched directories per checkpoint instead of per unlink (a 450-entry
  rollback on a spinning disk went from 312 s to 8 s), and abandons the empty
  described working-copy commit it created so a failed start leaves no dead
  head in the operator's `jj log`.
- `asha task start` prints terminal-only progress lines around workspace
  preparation and launch, so a long checkout is not silence.

#### Control: refusals read cause-first and the TUI wraps them

- Preparation refusals now lead with the cause and its remedy and end with
  what Control did (`(preflight refused; no task state was created)`,
  `(workspace preparation rolled back; nothing to recover)`, or the partial
  rollback with the exact `asha task recover` command) instead of burying the
  reason behind "durable recovery state".
- The TUI wraps a long status message over up to six lines (footer kept)
  rather than clipping it at the terminal edge, and no longer truncates
  controller errors at 300 characters.

#### Control: default base works in local-only repositories

- `asha task start` without `--base`, the TUI `n` form with an empty base,
  and `asha initiative baseline` without `--revision` now use
  `coalesce(trunk() ~ root(), present(main), present(master), present(trunk))`:
  jj's remote `trunk()` when the repository has one, otherwise the local
  `main`/`master`/`trunk` bookmark. A repository with neither is refused
  with the remedy (`--base main`). Explicit bases are used verbatim.

#### Control: `asha task prune`

- Added `asha task prune (<selector>... | --all) [--keep-workspace] [--dry-run]
  [--yes] [--json]`, the only route that reclaims what an archived task leaves
  behind: it kills the dead, owned tmux session, runs `jj workspace forget`
  through the source repository, and removes the journaled workspace root by
  descriptor-anchored non-following deletion; the archived record, its digest,
  and the jj change are untouched. Workspaces bound to a non-terminal
  orchestration attempt (linked or reserved), without a journaled root inode,
  whose own marker names another task, claimed by another live task record,
  outside `control.workspace_root`, or with a live pane are refused with the
  reason. Removal is journaled (`asha.control-prune-record.v1`, intent then
  completion) so an interrupted pass finishes on re-run and a same-slug
  successor reusing the directory inode is never mistaken for the pruned task.
- Added the `prunable` doctor probe (informational), `asha.control-task-prune.v1`,
  and `TmuxAdapter.session_names`/`session_pane_states`.
- The orchestration seal-drift reconciler treats a pruned archived workspace
  (prune record present) as reclaimed rather than as drift.

#### Orchestration Core complete: Increments 1-3

- Added the read-only `asha initiative baseline` authoring helper and
  plan-time visible-commit/tree-digest validation for approved baselines.
- Added ordered exact-seal composition, independent mutation-free review,
  controller-owned approved-argv verification in fresh retained
  materializations, exact-seal candidate bundles, readiness/finalization, and
  retained archive and unarchive operations.
- Added composition, review, verification, readiness, archive, and real-jj
  materialization coverage, including verification denial and failure paths.
- Closed the worker launch-to-link publication race with a bounded link grace
  period, and documented the narrow live-worker jj mid-snapshot reconciliation
  downgrade that applies only while process ownership still matches.

#### Orchestration Core Increment 2b

- Added managed worker result publication with durable restartable phases,
  immutable accepted results, Control-exit reconciliation, write-once jj seals,
  cumulative hard-scope enforcement, advisory path evidence, and seal-drift
  detection.
- Added autonomous sealed-failure retry, success-seal repair with stale
  downstream evidence, approval-bound read-only failure salvage, paused-seal
  decisions and continuation, and cancellation that preserves task workspaces.
- Added task result and seal inspection, publication/seal/salvage unit suites,
  and disposable real-Control execution coverage for success, scope violation,
  and completed-claim/nonzero-exit failure.

#### Orchestration Core Increment 2a

- Added effect-once operator action journals, immutable Control task links,
  bounded worker assignments, deterministic readiness and dispatch through
  Control's create-by-ID seam, live attempt reconciliation, autonomous
  original-base retry, activation handshake, and deadline, task, storage,
  repeated-failure, and nested-workflow breakers.

#### Orchestration Core Increment 1

- Added durable initiative, graph, node, approval, and immutable event records;
  exact-digest plan approval/rejection; read-only Control reconciliation and
  retained-storage reports; a pure initiative tree presentation model; and
  orchestration doctor probes.
- Routed `asha initiative` through the trusted Control Python entry while
  keeping orchestration configuration lazy. Dispatch, harness launch, tmux
  mutation, jj workspace creation, and Control record writes remain absent
  until later increments.

#### Instruction and workflow cleanup

- Collapsed the repository guide to project-specific invariants and made each
  plugin README the sole authority for that plugin's version.
- Removed unreachable session guidance, unused loop and harness templates, the
  retired Claude output-style mount, and a duplicate Claude hook installer.
- Replaced model-tiered code orchestration with harness-neutral risk gates and
  removed its unused append-only calibration log.
- Reduced panel persistence to one resumable state file plus its final decision
  artifact; panel transcripts, per-phase files, and the separate index are no
  longer produced.

### Asha Control — Soak fixes, contract freeze, create-by-id (2026-08-17)

- Ran the control->orchestration runway's Phase 3 soak on real work (nine
  Codex tasks across two colocated repositories, four concurrent) and fixed
  every Control defect it found in Control: Codex trust prompt skipped per
  launch for managed workspaces; a dead or absent harness reconciles to a
  terminal state (signal deaths, absent panes, archived tasks no longer read
  `stale`); popups bind to the caller's own tmux client; task start guards
  git HEAD/jj `@-` divergence in colocated sources and imports refs before
  resolving a base; `--json` payloads carry an `existing` flag.
- Added the orchestration prerequisite: idempotent
  `asha task start --task-id UUID` (create-if-absent / return-existing under
  the task lock, no record extension).
- Documented the TUI surface, and froze the Control v1 contract set
  orchestration binds to in `docs/control-contracts.md` (runway Phase 4).
- Closed cold-review issues #52, #53, #55, #56, #57, #59, and #61 as soak
  work: event snapshots are authorized against the owned task record through
  a lock-free store peek; the namespace predicate now judges writable
  ancestry by mode (pre-existing `0775` task workspaces need one
  `chmod g-w,o-w`, printed by every refusal); explicit repo/workspace saves
  bypass Control marker discovery; doctor no longer reports false negatives.

### Asha Control — Persistent jj and tmux task supervision (2026-08-15)

- Added persistent task records, explicit-base jj workspaces, isolated tmux
  sessions, process-safe stop/archive operations, live reconciliation, and the
  terminal Control TUI.
- Added bounded Claude/Codex status events with process-liveness fallback for
  Copilot and OpenCode.
- Added read-only GitHub PR and issue sources: transient bounded metadata,
  non-checkout PR-head fetches, and no GitHub write path.
- Published the current operating contract in `docs/control.md`.
- Made archive reachable through reconciled terminal evidence, added reversible
  unarchive, and added journal-driven interrupted-creation recovery and doctor
  reporting.

### Asha identity v3.0.0 — Compact identity split (2026-08-13)

- Reduced the automatic persona corpus to compact `soul.md`, `voice.md`, and
  `keeper.md` files behind a 24 KiB fail-closed merge budget.
- Moved extended identity, user profile, and writing-voice material into
  task-selected cold references exposed by the `asha-reference` skill.
- Repaired fresh identity provisioning across all four harnesses and removed
  live `communicationStyle.md` and retired memory-search instructions.

Entries below v2.7.0 describe the mechanisms shipped by those historical
releases. Where they name transcript synthesis, retrieval, generic nudges,
operational catalogues, or curator/steward agents, v2.7.0 has retired those
surfaces; they are not current usage instructions.

### v2.7.0 / Session v2.0.0 — Memory System v2 (2026-08-13)

- Made explicit `/session:save` the sole semantic publisher, with a validated
  four-section 4 KiB handoff and current-decisions file.
- Replaced transcript/event/automatic-save machinery with ignored, atomic,
  per-session 2 KiB recovery snapshots and seal-only SessionEnd behavior.
- Added candidate/active/retired learnings with three-session/two-project
  activation; removed confidence tiers, retrieval/nudges, context brokerage,
  operational catalogues, and the memory curator/steward agents.

### v2.6.0 / Session v1.21.0 — OpenCode stable-v1 support (2026-08-11)

- Reinstated OpenCode as a fourth harness against stable v1 (`>=1.15.11`):
  native plural `skills/`, `commands/`, `agents/`, and `plugins/` surfaces;
  wrapper-scoped identity; native JavaScript plugin hooks; installer,
  uninstaller, doctor, dispatcher, capability registry, and brokerage wiring.
- Added exact-session SQLite transcript synthesis from
  `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db`, including child to
  root session resolution and project-directory validation.
- Added `tool.execute.before` policy translation, buffered guidance through
  `experimental.chat.system.transform`, manual save, and best-effort clean-exit
  save through plugin `dispose`. No idle checkpointing is claimed.

### Documentation architecture sync (2026-08-08)

- Recast the root README as the installation, capability, plugin, and workspace
  map; detailed invocation and workflow instructions now live with each plugin.
- Replaced the pre-workspace memory model with explicit global, repository,
  workspace-operational, private-local, canonical-knowledge, and harness-native
  stores, including launch-point and save-scope examples.
- Synchronized command examples with installed namespaces and current
  Claude/Codex/Copilot/OpenCode save behavior.

### Session v1.20.0 — workspace management and evidence brokerage (2026-08-08)

Issues #23-#27 and #45-#50: bounded cross-harness workspace context,
source-aware retrieval, workspace bootstrap/doctor, reviewed canonical
knowledge promotion, coordinated multi-repository worktrees, private work-item
registry/adapters, and evidence-backed context/process/capability brokerage.

### Session v1.19.0 — copilot commit gate chained (2026-08-08)

Issue #40 (attended): `copilot-policy-adapter.sh` now carries the Copilot
payload's `cwd` through the Claude-shape translation (previously dropped —
the gate is cwd-sensitive, so without it the chain could not resolve the
project) and chains `save-commit-gate.sh` after policy-guard +
block-secrets. Deny (staged Memory, no proof), allow (hash-bound marker),
and self-filter (non-commit git) pinned through the translated payload in
Test 105. Live in-session deny probe deferred to the post-merge smoke —
the auto-mode classifier correctly refused a temporary live-hook redirect,
and the merged install needs no redirect at all. Copilot `workspace`
capability entry updated; upstream concurrency caveat (#2893) retained:
the gate is a deterrent layer, the writer-side proof remains primary.

### Workspace v1 complete — three-harness parity attested (2026-08-08, session v1.18.0)

Delivery issue 6 of 6 (issue #39) closes the ratified ship gate (decision 3,
PR #28). The first attestation attempt was **held in pass-2 review** (14th
consecutive batch): env-shaped probes are not harness-integration evidence,
the rendered codex/copilot save skills predated `--scope`, and the copilot
auto-save hole was unattested. The re-attestation runs everything under each
harness's REAL runtime (`codex exec` / `copilot -p` executing the probe
commands through their own shell tools in a fixture workspace):

- **Codex 0.147**: detection, save_scope proof round-trip, staged-set
  isolation, AND the commit-gate deny all verified live — the gate blocked an
  unproven Memory commit and consumed the proof on the allowed one. This
  **overturns the 0.142 "shell PreToolUse doesn't fire" verdict** (the
  re-probe the enforcement doc's version caveat demanded).
- **Copilot 1.0.75**: detection + proof + isolation verified live; the gate
  confirmed absent (ungated commit succeeded; proof survived unconsumed) —
  issue #40, attended.
- **Copilot auto-save hole closed** (the #36 deferral): a real sessionEnd
  auto-save was shown committing the workspace plane ungated; the automatic
  path now routes through a plane-aware writer seam
  (`tools/auto-commit-memory.sh`, Test 9d) — legacy no-manifest behavior
  preserved, workspace commits proof-bound + scope-staged + consume-on-use,
  manifest-present-but-unvalidatable fails closed. The gate cannot see
  hook-context commits on ANY harness, so the writer seam is the auto path's
  protection everywhere.
- `asha doctor` now prints each harness's `workspace` capability limitations
  inside its workspace section (Tests WS-12..14); `capabilities.json` entries
  rewritten from the probe verdicts (schema stays v3 per the proposal
  amendment recorded under issue #39); rendered codex/copilot `session-save`
  skills regenerated so `--scope` is reachable on their surfaces.

The workspace-memory proposal's v1 scope is now fully shipped: manifest
validator, detection consolidation + walk, status/doctor, save scopes +
plane gate, destructive-git cross-repo arm, auto-save writer seam, parity
attestation.

### Session v1.17.0 — save scopes + state-based commit gate (2026-08-08)

Workspace v1 delivery issue 4 (issue #36), design ratified via adversarial
consult. `/save` gains `--scope repo|workspace|none`; the writer-side seam
(`tools/save_scope.py`) resolves a scope into its plane mapping —
`plane_base` / `memory_root` / `commit_repo` as three distinct values —
writes a **versioned structured proof** at the plane, and verifies it
immediately before commit. `save-commit-gate.sh` becomes plane-aware by
**repository state, never command parsing** (a spoofed `-C` is irrelevant
when staged sets decide): a pure-bash existence walk keeps the no-manifest
path byte-identical at zero python cost (12-case golden corpus pins it);
manifest-present-but-unvalidatable **fails closed**; both-planes-staged
denies as ambiguous; each plane's proof satisfies only itself. The
Stop-hook net routes v2 locators to the plane's structural proof (the
session-transcript gates stay project-scoped by design). 17 save_scope
unit tests + Test 9c (19 gate pins), tests-first.

### Workspace v2 read side — bounded context + retrieval source (2026-08-08)

Sessions launched inside a valid workspace now receive one bounded background
block naming the workspace/root/active child and the first `##` section of the
workspace operational `Memory/activeContext.md`. The internal renderer is
`workspace_status.py --context`: no git enrichment, strict UTF-8, canonical
containment, delimiter sanitization, 2048-byte excerpt budget by default, and
zero output outside workspaces. `ASHA_WS_CONTEXT_MAX` changes the excerpt cap
(values below 256 or invalid values revert to 2048). `ASHA_WS_INJECT=0`,
`Work/markers/workspace-context-off`, and `Work/markers/silence` disable delivery.

The shared SessionStart handler delivers this block directly at the native hook
seam for Claude, Codex, Copilot, and OpenCode; Copilot receives its verified
top-level `additionalContext` response. The former first-prompt fallback and
cooldown are removed. Canonical workspace `knowledge/` indexes remain intact,
but the v1 retrieval service and operational catalogue were retired by Memory
v2.

### Workspace v3-v6 management CLI — explicit writes, no implicit Git (2026-08-08)

The harness-independent dispatcher exposes the remaining workspace cores:

```text
asha workspace init|discover|doctor
asha workspace knowledge init|lint
asha workspace promote plan|apply|publish
asha workspace worktree create|status|remove
asha workspace work-item create|list|show|link|import|preview|lint|index|promote-plan|worktree-seed
```

The shell layer only routes arguments; each Python core remains the authority
for its exact flags (`--help`) and validation. Knowledge `plan` writes an
explicit review artifact bound to source, evidence, target preimage, and
digest. Pull-request plans also bind the shared Git root, base commit, and
credential-free GitHub repository identity. `apply` accepts only that artifact plus its digest and explicit
confirmation, then revalidates every preimage before writing. In
`pull-request` mode, `publish` requires the same artifact, digest, and explicit
confirmation; it refuses a dirty or ambiguous shared Git root, creates a
digest-named branch, stages only the reviewed knowledge write-set, commits,
pushes that branch, and opens a draft pull request. It never merges or
direct-pushes the base branch.
The shipped review adapter is GitHub CLI; other forges fail closed rather than
being treated as equivalent review infrastructure.
Repository commit/push hooks are disabled by default because they execute
project-local programs. `publish --run-git-hooks` is the separate explicit
authorization to run configured local governance hooks; the draft PR's remote
CI remains the external review boundary either way.
Worktree commands are explicit operations, branch deletion requires its
dedicated flag, and squash-merge cleanup requires review evidence. Work-item
import is offline and requires a scrubbed preview token; `worktree-seed` emits
data only and performs no Git operation.

### Session v1.16.0 + `asha workspace status` — first workspace consumer (2026-08-08)

Workspace v1 delivery issue 3 (issue #35). New dispatcher verb
`asha workspace status [--json] [--start DIR]` (thin `lib/workspace.sh`
shim; no new fallback chain — detection stays with the shared resolver) over
new `plugins/session/tools/workspace_status.py`: manifest validity with
typed errors verbatim, active child repository (cwd-resolved), per-repo
presence/branch/dirty state (reported, never assumed), `shared_git_root`
health, manifest trackedness. `asha doctor` gains the same as a section —
silent outside workspaces, fail-closed on an invalid manifest. Implements
the ratified open-question-1 decision: **manifest committed in
`shared_git_root` by convention**; untracked warns, invalid prints guided
repair (never auto-fix). Suite 15 (10 dispatcher/doctor cases) + 12 python
unit tests, tests-first.

### Session v1.15.0 — project-root consolidation + workspace walk (2026-08-07)

Workspace v1 delivery issue 2 (issue #33). The layered Memory-root detection
algorithm previously existed in **six divergent copies** (three bash, three
Python — no two byte-identical, five distinct layer orders); workspace
detection added to one would not have propagated. It now exists exactly once
per language: `tools/project-root.sh` and `tools/project_root.py`, with each
historical caller declaring its exact layer set, so per-consumer behavior is
byte-identical — pinned by the new Test 9b (12 detector-semantics pins,
green before AND after the rewire) and the existing Python suites. New
`detect_workspace()` primitive walks upward for `.asha/workspace.json`
(stopping exclusively before `$HOME` and `/`, canonical comparison, invalid
manifest = typed verdict, never silent fallthrough) — deliberately consumed
by NOTHING yet; issues 3–4 wire it. Audit: no independent layered fallback
chain remains; exempt-by-design sites (build-root detector in verify.py,
issue-loop's git-only refuse, thin command-MD one-liners, payload-cwd
hooks) are catalogued in PR #34.

### Session v1.14.0 — workspace manifest validator (2026-08-07)

Workspace v1, second of the six increments to land (proposal delivery
item 1, issue #31): `plugins/session/tools/workspace_manifest.py`, a pure lexical
parse/validate layer for `.asha/workspace.json` — typed collected errors,
fail-closed, schema defaults, traversal/absolute-path rejection, the
containment and disjointness rules, and the v1 `operational_root == Memory`
pin, with unknown keys preserved at every level. Deliberately
filesystem-free: worktree existence and symlink canonicalization land with
detection/status (issues 2–3). 38 table-driven tests in
`tests/python/test_workspace_manifest.py`, written RED-first.

### Session v1.13.0 — destructive-git cross-repo arm (2026-08-07)

First build increment of the ratified workspace-memory proposal
(`docs/proposals/2026-08-06--workspace-memory.md`, delivery issue 5 — landed
first because it is independent and closes a live gap): `git -C <dir> push
--force` and the other `-C`/`--git-dir`/`--work-tree` forms previously
evaded `destructive-git`, because the rule required the destructive verb to
directly follow `git` (one accident excepted: a path containing a `.git`
segment re-exposed a matching substring and denied by fluke). The rule now
consumes optional cross-repo global flags (quoted, attached, `=`, repeated,
and mixed-quoted forms); plain cross-repo commit/push stays allowed by
design (workspace saves depend on it). Pass-2 codex review hardened its own
fix batch (9th consecutive fix batch with confirmed defects): mixed-quoted
path tokens (`-C "$ROOT"/shared`) evaded the first arm, exclusions could be
laundered (`… push --force && echo --force-with-lease` allowed — exclusions
are now segment-scoped, the destructive-delete v2.4.0 fix class), and
backslash-newline continuations dodged per-line matching (the evaluator now
normalizes them). The issue-loop preflight gained a cross-repo `MUST_DENY`
probe so a user overlay carrying the pre-1.13.0 rule refuses dispatch. 21
Test 104 pins (`xr_*` + `multiline_ok`). Known residuals documented in the
rule: other global flags (`-c`, `--no-pager`) still bypass — widening is a
separate decision — commit messages quoting a guarded command false-positive
safe-side, and env-prefixed forms were already caught by unanchored
matching.

### v2.5.0 — Overnight issue-to-merge loop (2026-08-05)

Builds the deferred spec in `docs/proposals/2026-08-04--issue-to-merge-loop.md`: a dispatcher that triages open GitHub issues, fixes each safe one test-first in an isolated worktree, cold-reviews the diff, and opens **draft PRs only** — the human merges over coffee, the machine never merges. Code plugin v1.5.0.

- **Safety rails before features, and rails first in the build**: `issue-loop-preflight.sh` enforces a **dual opt-in** (the target repo commits `.asha/issue-loop.json` AND the repo path is allowlisted in `~/.asha/config.json` — a cloned repo cannot self-authorize, a local allowlist cannot enable a repo that never opted in), probes `gh` auth with clean surrender, requires `.asha/worktrees/` git-ignored, and — rail 6 made *runtime* — pipes the loop's own command set through the live policy guard (user overlay merged) before every dispatch: an allow-side deny **or a gutted deny-side protection** refuses to run. 20 cases in `tests/test-issue-loop.sh` (Suite 14).
- **"Never push main / never merge" is structural, not policy**: plain `git push origin main` stays intentionally allowed for humans (pinned in Test 104), so the loop's only push path is `issue-loop-publish.sh` — refuses main/master, foreign prefixes, unregistered worktrees, and dirty trees; hardcodes `--draft`. Worktree cleanup is `git worktree remove` only; the `rm -rf` form stays denied and is now pinned as such (7 new issue-loop cases in Test 104).
- **Engine** (`plugins/code/engines/issue-loop.js`, first engine in the code plugin — write/engines precedent): Triage → Iterate → Review → Publish → Report with commission-loop's verdict discipline — uncertainty fails, a dead agent fails its item, findings outrank the verdict label (the five-criterion triage conjunction is recomputed engine-side), silence is never success (thrown stages become indexed failure envelopes; the run report is mandatory, with a caller-side fallback duty). Worker contract: failing test FIRST or report "no-failing-test"; attempt cap then surrender with diagnosis; worktree evidence checked by the engine, not trusted from the label. The reviewer is **cold** — diff + issue text only, worker reasoning structurally withheld — and judges scope against the Change Budget rule. Wiring test: `tests/js/issue-loop.test.mjs` (13 scenarios).
- **v1 has no outward-write path to the tracker** (triage comments deliberately not built — rejections and needed clarifications live in the run report at `Work/loops/<run-id>/`, manual pruning), no auto-merge ever, and the loop does not run against asha itself until it has a track record on lower-stakes repos.

### v2.4.0 — Usage-insights remediation (2026-08-04)

Five repairs driven by a `/insights` review of 145 sessions. Each maps a recurring real-world failure to the mechanism that should already have carried it.

- **`destructive-delete` policy rule** (session v1.12.0) — `rm -r/-f`, `rm` of a glob or archive, `shred`, `gh repo delete` now deny by default; `destructive-git` gains `filter-repo`/`filter-branch`. Motivating incident: two `.7z` archives deleted before extraction, forcing re-download. Exemptions are deliberate and tested — `docker rm`, `git rm`, `npm rm`, `node_modules`, `.venv`, `/tmp` — because an over-broad rule gets disabled and then protects nothing. Override: `ASHA_ALLOW_DESTRUCTIVE_DELETE=1`. 19 new cases in Test 104.
- **Negative claims require an evidence trail** (`modules/research.md`) — the severity markers only ever covered *hedged* claims; confident assertions of absence ("no update exists", "no such file", "not version-controlled") attracted no marker and were the highest-frequency correction in the review. Negative findings now carry a `Checked:` line, with an authoritative-source table. Two rules generalize the specific incidents: **a pin is a claim, not evidence** (a branch/tag in config says what was selected, never what is available) and **a cache is not its source** (absence from an index means *not indexed*).
- **`roleplay-gm` made structurally read-only** (rp v0.2.0) — was `Task, Edit, Write, Bash`, now `Task, Read, Grep, Glob`. It had write access it was never instructed to use, and lacked the `Read` its own instructions required ("Read `Memory/invariants.md` at session start"). It drafted blind against the continuity contract while able to bypass it: the turn loop has the *calling command* append only on a clean verdict, so a GM that writes its own draft skips the gate entirely. Follows the `claim-verifier` allowlist-as-enforcement pattern. Also fixed a stale `rp-validator` reference (renamed to `continuity-reviewer` in v2.1.0).
- **Decline-once directive** in the per-turn RP routing fragment — a session was abandoned after refusals oscillated mid-scene and poisoned the context. Oscillation, not refusal, is the expensive failure: state the boundary once and hold it. Paired with an explicit ban on authoring PC actions.
- **RP portability** (rp v0.2.0) — the plugin's README promised the *nouns* stay in the project; the implementation contradicted it. Canon paths now resolve through a project-owned `Memory/canon-layout.md` register (template shipped, historical defaults preserved so existing projects need no edit). Campaign proper nouns removed from shipped primitives: the `rp-priced-stakes` `match_regex` carried one campaign's vocabulary (`doorman`, `mystic door`, `dollhouse`) and so fired only there, and `roleplay-gm`/`canon-writer` hardcoded a specific setting.

**Core de-personalization.** The same rule applied to the layer every project installs, where a foreign proper noun costs the most:

- **`no-broad-home-scans` was username-hardcoded** — its regex matched `/home(/pknull)?`, so on any other machine a full scan of `/home/<someone-else>` was **allowed**. The guard protected exactly one account. Now `/home(/[^/[:space:]]+)?`, denying for any user while leaving scoped paths (`/home/<user>/code`) allowed. Regression cases added to Test 104.
- **`recall_fixtures.yaml` shipped the maintainer's benchmark to every install** — `lib/install.sh` seeds it into each new `~/.asha/`, so a fresh user received twelve fixtures expecting memories (`project_egregore_setup`, `reference_home_network`) that could never exist for them. They score 0 forever, and the file's own comment explains the cost: *"A permanently impossible fixture would conceal real score regressions."* It then shipped twelve of them. Four also referenced the retired marketplace flow. Replaced with a documented, empty starter — `recall_bench` handles zero fixtures cleanly (`score … if cases else 0.0`), and existing `~/.asha/recall_fixtures.yaml` files are untouched because install only seeds when absent.
- **Personal names removed from core prose** — an "AAS vault" aside in `pattern_analyzer.py`'s RP calibration guard, and a `reference_pk_lintop_…` memory id used as the worked example in the memory-maintenance skill.
- **`vault-structure` content root widened** — was anchored on the literal `Vault/`; now matches any of `Vault|Lore|Wiki|Codex|Compendium|Archive`, with the same bucket taxonomy as the exclude. The root stays a *named* anchor deliberately: it is what scopes the rule, and a bare wildcard degenerates the trigger into "every write not in a bucket" — measured at 129/129 markdown files in this repo, `CLAUDE.md` and every doc included. Test 104c pins that blast radius while leaving the root list extensible. Projects whose content lives elsewhere redefine the row in `~/.asha/policies.json`.

**Adversarial review pass (2026-08-04, Codex as external reviewer + self-review).** The four remediation commits were themselves reviewed before release; 13 findings, all verified against the live guard before fixing. The ones that mattered: the v1 delete rule **denied the toolkit's own marker cleanup** (`rm -f Work/markers/…` in `/rp:end`, `/session:silence`, `/session:restore`) — a guard that blocks its own shipped workflows gets overridden into uselessness; an exemption string anywhere in a command suppressed the whole rule (`rm -rf important && mkdir -p /tmp/stage` was allowed), fixed by scoping path exemptions to the rm segment and giving archives their own prior rule with **no** path exemptions; quoted archives (`rm "backup.7z"`) slipped the terminator; `~`/`$HOME`/quoted forms — the most natural phrasings — bypassed the home-scan rule entirely; the priced-stakes nudge was case-sensitive (sentence-initial capitals never fired) and unbounded (`impact` fired via `pact`), fixed with a new opt-in `match_ci` engine flag plus word-boundary discipline; scene-state maintenance was orphaned by the roleplay-gm allowlist cut, redesigned as a `SCENE_STATE_DELTA` the GM emits and `/rp:turn` applies only on a clean verdict — state now rides the same gate as prose; `/rp:turn` and the priced-stakes fragment still hardcoded the stake register the canon-layout work was meant to own; and README's per-plugin detail sections carried versions two releases stale, outside `validate-versions.sh`'s net (now its Test 5). Residual gaps are documented in the rules' `_comment` fields rather than silently carried.

**Decisions round (same day).** Working the remaining report items to explicit rulings: **`commission-loop` engine** (write v1.9.0, `engines/commission-loop.js`) — the generalized adversarial commissioning harness: N workers draft one brief from cycled angles with every claim citing a source verbatim; per-draft verifier panels are instructed to refute (uncertainty fails, a dead verifier fails the draft); only survivors are ranked, rejects return with their findings, and the engine itself never writes a file (agents are no-write by instruction, or structurally via read-only `workerAgentType`/`verifierAgentType`) — promotion is the caller's explicit act. Wiring test executes the real engine body (`tests/js/commission-wiring.test.mjs`). Plus two module lines closing the last asha-shaped report items: a **change-budget scope contract** in `cognitive.md` (file list, per-file intent, and adjacent temptations surfaced separately as "out of scope, want it?" before 3+ file work) and **project-root-relative deliverable paths** in CORE.md Output Defaults. The overnight issue-to-merge loop was deliberately deferred with a captured spec (`docs/proposals/2026-08-04--issue-to-merge-loop.md`).

**Adversarial review, round 2 (2026-08-05, Codex again — this time over the fixes and the new engine).** 19 findings, every one verified live before fixing; the reviewer also *corrected this session's own diagnosis once* (the commission-loop erasure is the thrown-stage path, not agent-null). Headlines: the engine's "structural write boundary" claim was **false for workers** (docs now honest; `workerAgentType` added for a genuinely structural boundary); `SCENE_STATE_DELTA` rode *around* the continuity gate instead of through it (now a reviewed input with a `scene_state_mismatch` category, defined merge semantics, and application on the accept-anyway path — plus `/rp:end` refuses to canonize unaccepted surrender blocks); a user-layer policy override silently migrated to lowest priority (merge now replaces in place; Test 104d); archives hid behind adjacent operators and quoted metacharacters (`rm backup.7z&&ls`, `rm "backup&old.7z"`); `find ~alice` scanned another account's home unchallenged; a verifier returning `verdict:pass` alongside a hard finding survived (findings now outrank the label); ranker duplicates/partials could silently discard verified work; and the wiring-test mock diverged from real runtime semantics (throw→null now modeled). Residuals stay documented in `_comment` fields, not silently carried.

**`write` plugin de-specialized (v1.8.0 → v1.9.0 same cycle).** The last domain plugin carrying one project's material:

- **`prose-analysis` hardcoded a voice doc** — `Vault/Docs/MasterWritingStyleGuide.md`, with a "**Always** read MasterWritingStyleGuide.md first" best-practice and a "missing → request location" fallback. In any other project the glob resolved nothing and the agent proceeded on assumed voice standards. Now a convention search (`**/*StyleGuide*.md` among generic candidates), with an explicit declaration — a manifest's `slots.voiceSpec` or a user-given path — taking precedence, and a refusal to proceed on assumed standards when nothing resolves.
- **A project's canon shipped inside a generic agent** — a "Hush-Specific Checks" block listing coined transformation-anatomy terms and their body-location constraints. Replaced by the *generalizable* half: constrained-term checking, where a term carries a constraint (location, rank, material, direction) that prose drifts on before it drifts on the term itself, and where the negative cases must be written out because the wrong answers are the adjacent ones.
- **`craft-core-universal` profile mapping** — the `rp`/`hush` column table became a template for a project's own mapping, plus the three honest relationships a mapping can express (`enforced via core` / a named category / `cf.` for adjacent-but-not-identical) and guidance on what should *stay* profile-specific. The engine itself was already generic (`profileKey = a.profile || P.mode || 'custom'`, no built-in list); only its description string claimed `Profiles: rp | hush`.

### v2.3.0 — OpenCode support dropped (2026-07-27)

Operator decision following the #14 survey: OpenCode ≥1.18 moved session transcripts to sqlite, breaking Asha's memory capture — the system's value core — and the fix (a sqlite reader backend) was judged not worth carrying for the least-used harness. Asha is now a **three-harness** toolkit (Claude Code, Codex, Copilot CLI).

- Removed: `harnesses/opencode.sh`, the `asha-guardrails.js` emission + `opencode-policy-adapter.sh`, jsonl_reader's opencode backend, dispatcher/doctor/registry/capabilities wiring, the opencode test suite, and all opencode branches in shared handlers and save tools (session plugin v1.11.0).
- Live artifacts were uninstalled from the reference machine before the code was removed; other machines: check out a pre-2.3.0 tag and run `./uninstall.sh --target opencode`, or delete the asha entries under `~/.config/opencode/` manually.
- Retirement record with the final plugin-API survey verdicts preserved in `docs/harness-enforcement.md`; follow-up issues #16/#17 closed as not planned.

### OpenCode plugin-API survey — verdicts for all four parity questions (2026-07-27)

Closes issue #14 with the probe-first method (isolated `XDG_*` rig, instrumented plugin, local ollama model). Live verdicts in `docs/harness-enforcement.md` "Plugin API survey": the plugin surface is far richer than the one `tool.execute.before` hook asha used — `chat.message` fires per user prompt, a catch-all `event` stream delivers `session.idle`, and `experimental.chat.system.transform` injects. At that release, OpenCode's move to SQLite broke `/save`; v2.6.0 later supplied the SQLite backend and reinstated support. This entry remains the historical survey record.

### Session v1.10.0 — Copilot lifecycle: auto-save + orphan recovery (2026-07-27)

Closes issue #13 (wired on operator opt-in) — the last substantive Claude-parity gap on Copilot. Live-probed on 1.0.75, then verified end-to-end:

- **sessionEnd verdict** — fires on clean exit with `{sessionId, timestamp, cwd, reason}`; reasons observed live: `complete` (non-interactive `-p`) and `user_exit` (interactive `/exit`); SIGKILL fires nothing. sessionStart fires at first prompt submission and carries `initialPrompt`.
- **Lifecycle wiring** — new installer-generated `~/.copilot/hooks/asha-lifecycle.json`: sessionStart → `session-start.sh` (side effects only; context injection naturally discarded — the custom-instructions layer already injects), sessionEnd → `session-end.sh` (camelCase payload, copilot clean-exit reasons, detached save; `COPILOT_CLI=1` overrides inherited `ASHA_HARNESS`).
- **Crash-safe orphan trail** — copilot has no per-tool event capture, so sessionStart appends one identity breadcrumb event stamped with the harness uuid; a crashed session's work is recovered from its surviving native transcript at the next session start. Verified live: SIGKILL mid-session → recovered + synthesized next start.
- **False-orphan guard (all harnesses)** — a session whose `wwa-session` stamp is already published in activeContext.md is no longer re-recovered on every post-save session start.
- **Doctor + uninstall symmetry** — `asha doctor copilot` now byte-checks the guardrails, nudges (previously uncovered), and lifecycle files, `--fix` rewrites them; uninstall removes the lifecycle file. New `test_orphan_detection.py` unit suite.

### Session v1.9.0 — Codex PostToolUse verdict: fires but discards (2026-07-27)

Closes the verification gap from the 2026-07-26 codex hook work (issue #15). Four isolated-`CODEX_HOME` probes on codex 0.145, with the proven UserPromptSubmit injection as positive control:

- **Fires, but stdout is discarded** — PostToolUse fires for plain shell (`tool_name: "Bash"`, identical under `unified_exec = true`) and successful `apply_patch` (native `tool_name: "apply_patch"`); not for sandbox-rejected calls. Hook stdout never reaches the model or the session transcript in any shape — there is no PostToolUse injection channel.
- **Payload is Claude-shaped** — `hook_event_name` present (argument-free nudge-engine registration resolves correctly), plus full `tool_name`/`tool_input`/`tool_response`/`tool_use_id` for row gates.
- **`suggest-compact` harness-gated to claude+copilot** — an ungated row burned the shared tool-count and stamped the 2h cooldown for output codex discards, suppressing the nudge for a later Claude session. New Test 92f guards the gate (codex skipped with counter untouched; copilot still fires).
- **Bonus verdicts** — codex honors `matcher`, and aliases `apply_patch` into the `Edit|Write|MultiEdit` class (so post-edit-lint's registration does fire on codex file edits). Full verdict: `docs/harness-enforcement.md` "Codex PostToolUse".

### Session v1.8.0 — Learnings durability: migration decoy fix (2026-07-27)

Fixes issue #12: `migrate-okf` relocated the learnings store to `~/.asha/learnings/` while leaving the legacy flat file behind untouched — silently dropping users out of existing backup arrangements (e.g. a dotfiles symlink covering `learnings.md`) and leaving a stale decoy that makes a restore look successful.

- **Supersession banner** — after a successful migration, each legacy flat file is stamped (idempotently) with a banner + sentinel declaring it a frozen pre-migration snapshot; original content preserved verbatim below (still the rollback path). Stamping writes through symlinks (atomic replace of the resolved target), so an externally-tracked copy becomes self-describing as stale.
- **Backup-coverage warning** — migration (including `--dry-run`) now warns on stderr and in the report JSON when a legacy file is a symlink or resolves outside `~/.asha`: the bundle directory the store moved to is outside that arrangement's coverage.
- **`legacy-status` divergence check** — new `learnings_manager.py` verb, run warn-only by `/save` and `/session:consolidate`: flags an unstamped flat file next to the live bundle (the stale-decoy state where a restore would resurrect pre-migration data).
- **Durability documented** — the bundle is local-only by default; `docs/memory-architecture.md` "Durability & backup" states it and shows how to extend a dotfiles arrangement to cover `learnings/` + `learnings-archive/`.

### Session v1.6.0 — Copilot guidance-nudge parity (2026-07-26)

- Live-probed Copilot CLI 1.0.68's hook contract: events fire with no feature flag or trust gate, payloads carry no `hook_event_name` (argv registration instead — Copilot shell-splits), hook processes receive `COPILOT_CLI=1` + `CLAUDE_PROJECT_DIR`, raw stdout is discarded, and the ONLY injection channel is a top-level `{"additionalContext": ...}` JSON response (isolated by key: `systemMessage` et al. do nothing).
- Wired accordingly: `asha_harness()` detects Copilot via `COPILOT_CLI`; the nudge engine emits the additionalContext shape for copilot on every event; new `~/.copilot/hooks/asha-nudges.json` initially registered userPromptSubmitted + postToolUse (installed/uninstalled symmetrically). Production RP probe answered INJECTED. Workspace v2 later added the now-live-verified sessionStart registration. Full contract: `docs/harness-enforcement.md` "Copilot hook contract".
- Remaining Claude-parity gap, deliberately opt-in: sessionStart/sessionEnd side-effect wiring (orphan recovery + automatic clean-exit save).

### Codex hook enablement — feature gate, trust preservation, doctor coverage (2026-07-26)

- Live verification on codex 0.145 proved the asha hook fence works end-to-end (isolated `CODEX_HOME` replay: RP fragment reached the model) and exposed three defects, all fixed: the installer never set the required `[features] hooks = true` (now `_codex_ensure_hooks_feature` — adds when absent, never rewrites an explicit value); the fence excise destroyed codex's hash-bound `[hooks.state]` trust store on every reinstall (now preserved; Test 106d replays the failure; 11 production slots restored from backup); the doctor's codex hook-path check passed vacuously (crashed on `[hooks.state]`, silenced) — now walks nested commands and reports the feature gate + trust-slot count.
- Production codex hooks are enabled on the reference machine; full verdict in `docs/harness-enforcement.md` "Codex hook gating".

### Session v1.5.0 — Memory recall economics (2026-07-26)

Three disciplines ported from harness-native memory prompts into Asha's own stores; comparison in `docs/memory-architecture.md`.

- **Index-first injection** — SessionStart now injects one capped line per learning across the WHOLE bundle (`render-index`, hot-first, honest truncation tail) instead of the top-10 full bodies. Same byte budget, ~4× concept coverage; bodies Read on demand via the memory-lexical nudge. `ASHA_LEARNINGS_INJECT=hot` reverts.
- **`/session:consolidate`** — periodic four-phase compaction (orient → gather signal → consolidate → prune/index): merge drift, contradict disproven patterns, `retire` concluded records to `~/.asha/learnings-archive/` (new manager verb; full text preserved, out of every live surface), keeper.md calibration-log folding behind interactive confirmation, index-budget enforcement.
- **Broad-entry scrutiny** — BM25-style length normalization in `memory_retrieval.rank()` plus firing gates in the nudge: sprawling catalogue entries (≥25 tokens) are score-discounted, never fire on a lone rare token, and need three agreeing tokens. Live recall benchmark held at 12/13 hit@5.

### Session v1.4.0 — Declarative guidance-nudge engine (2026-07-25)

- New advisory counterpart to the policy guard: `hooks/handlers/nudge-engine.sh` evaluates declarative rows from `hooks/nudges/rules.json` (+ user layer `~/.asha/nudges.json`, merged by id) and injects context fragments — informational only, never blocking. Pattern extracted from severity1/claude-code-prompt-improver; its payload nudges were not adopted (largely absorbed by current harness behavior).
- Three ad hoc injections migrated to registry rows and their bespoke scripts retired: `memory-lexical` (was `hooks/memory_nudge.sh`), `rp-routing` (directive text now a single-source fragment, was inlined in `harness-response.sh` and emitted by `user-prompt-submit.sh`), `suggest-compact` (was `handlers/suggest-compact.sh`; cooldown is now engine-managed).
- Generic per-row gates (tool/regex/harness/marker/silence/init/cooldown), kill switches (`disable_env`, `Work/markers/nudge-<id>-off`), and priority-merged single-response output per event. Dynamic payloads via an allowlisted `nudge-builtins.sh` dispatch.
- Event resolved from the stdin payload's `hook_event_name`: argument-free registration survives hook runners that do not shell-split command strings (Codex TOML).
- Tests: engine coverage (RP routing claude/codex, kill switches, compact threshold/cooldown/silence, user-layer merge, harness/tool/env gates) + installer assertions retargeted; full suite green.

### Admin v0.3.0 — Proton Mail Bridge skill (2026-07-23)

- Added localhost-only Proton Mail Bridge administration through a stdlib Python helper: safe reads, structured search and triage, and hash-bound two-phase draft/send/move/delete operations.
- Enforced verified STARTTLS, UID-based message identity, native MOVE, move-to-Trash deletion, bounded MIME parsing, Bcc envelope privacy, and credential redaction.

### v2.2.0 — Ecosystem audit remediation (2026-07-22)

Full-project audit (goals, effectiveness, 88-script inventory, reachability) followed by fixes for all ten findings. Also rolls up the 2026-07-21 policy-guardrail work below.

- **Session v1.3.0** — dead memory-index feature removed (`post-tool-use.sh` no longer invokes the nonexistent `memory_index.py`; scaffolding template stops promising `memory_index.py`/`reasoning_bank.py`); orphaned `run-python.sh` deleted; `jsonl_reader.py` fully self-contained (no `~/life/bin` import path); save.md baseline capture resolved via `ASHA_BASELINE_CAPTURE`/config, not a personal path; both session skills documented.
- **Installer** — per-harness failure isolation in `asha_install_main` (one harness failing no longer aborts the rest; per-harness summary, non-zero exit on partial failure).
- **Code v1.4.0** — new `asha calibration` dispatcher verb makes `bin/calibration` reachable everywhere; orchestrate/complexity-routing docs use it; postgres skill documented.
- **Admin v0.2.0** — SKILL prose de-localized (repo paths resolve via `asha_root`, not `~/life/asha`).
- **Docs** — `~/life`/`~/life/marketplace` swept from all shipped prose (INSTALLER.md, secrets.md, memory-architecture.md, test-ping); README/CLAUDE.md version tables re-synced (session detail section had drifted to 1.0.0, write to 1.5.0/9-agents).
- **Tests** — shellcheck now covers `bin/ lib/ harnesses/ identity/` + root shims; `validate-versions.sh` cross-checks every plugin README version against both top-level tables; new `test-install.sh` (sandboxed round-trip incl. failure isolation) and `test-identity-merge.sh` (merge-script smoke) suites; bash-safety flags classified and annotated repo-wide.

### Policy guardrails made reachable and enforcing (2026-07-21, rolled into v2.2.0)

- **Session v1.2.0.** Two independent defects meant **all four policy rules were doing nothing**; both are fixed and verified live.
- **Fixed: file-path rules were unreachable.** `policy-guard.sh` was registered on `matcher: "Bash"` only, so `memory-protection` (`tool: "Write|Edit"`) and `vault-structure` (`tool: "Write"`) had never received a payload. Added a second `Edit|Write|MultiEdit` registration, claude-only via `_asha_harnesses` — on Codex `pretooluse_policy_ask` degrades to a hard deny, so a Codex-side registration would over-block. `memory-protection` has now fired for the first time.
- **Fixed: `ask` rules were inert.** An `ask` decision is auto-approved without surfacing a prompt under an auto-accept permission mode. `no-broad-home-scans`, `destructive-git`, and `memory-protection` converted `ask` → `deny`; each keeps its existing `override_env` escape hatch. `vault-structure` stays `warn` (log-only by design).
- **`memory-protection` exclude list corrected** — `scratchpad.md` and `ideas.md` added. `skills/memory-maintenance/SKILL.md` declares both "free-form, model-maintained", so the new `deny` would otherwise have hard-blocked documented behavior. The inert `ask` had hidden this.
- **Fixed: a pattern beginning with `--` was silently unmatchable.** Both evaluators called `grep -Eq "$pattern"` without an end-of-options guard, so `grep` parsed a leading `--` as an option. With `grep` being ugrep here, that exits rc=2, and the existing `2>/dev/null` made it indistinguishable from "no match" — a `command_regex` starting with `--` produced a silently dead rule, an `exclude_regex` starting with `--` produced silent over-blocking. All ten call sites in `policy-guard.sh` and `violation-checker.sh` now use `grep -Eq -- "$pattern"`. Guarded by new Test 104b.
- **`destructive-git` retargeted to match how work actually gets destroyed.** It permitted the operation an agent really reaches for while blocking the safe alternative. `--force-with-lease` is now **allowed** (it was blocked only because `--force` is a literal prefix of it); `git checkout -- <path>`, `git checkout .`, `git restore <path>`, and `git clean -f/-fd` are now **blocked**, with the reason pointing at `git stash` as the recoverable substitute. `git restore --staged` and `git rebase` stay allowed — the former does not touch the working tree, the latter is reflog-recoverable.
- **New Test 107 (rule reachability)** — asserts every rule's `tool` is covered by a registered `policy-guard.sh` matcher. Verified to fail against the previous wiring (naming all three dead rules) and pass against the new one. Test 104 previously could not distinguish `deny` from `allow`, since both produce empty stdout; its helper now reads the exit code.
- Caveat: `override_env` cannot be applied inline (`ASHA_ALLOW_DESTRUCTIVE_GIT=1 git push --force` will not work) — the hook reads its own environment, not the command's prefix. Set it in the session environment at launch.

### Unreleased — Save preflight hardening (2026-07-17)

- **Session v1.1.0** — new `save-preflight-env.sh` single-entry preflight: validated layered `ASHA_ROOT` detection (stale `config.json` caught at resolution, not five steps later), required-tool manifest check with a documented manual fallback (`docs/save-manual-pipeline.md`), and a hash-bound `save-gates-ok` marker.
- **New `disk_truth` gate** in `save_preflight.py` — disk is ground truth over Memory notes; `activeContext.md` references to nonexistent paths and future `lastUpdated` stamps are flagged as contradictions (warn-level).
- **New `save-commit-gate` PreToolUse hook** — mechanically refuses any `git commit` touching `Memory/` until all continuity gates pass; the marker is invalidated automatically if `activeContext.md` changes after gates pass. Override: `ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1`. Memory commits under an active silence marker are refused outright.
- **Write v1.6.0** — new `claim-verifier` agent (structurally read-only via tool allowlist) + `verify-consistency-report.yaml` recipe: consistency reports are untrusted model output; rewrite-triggering claims get independently verified against the manuscript (not state files) into a confirmed/denied matrix before any revision proceeds.
- **Code v1.3.0** — new `fix-loop.yaml` recipe: test-gated autonomous fix loop over an issue backlog; unattended counterpart to `bug-investigation.yaml` with human checkpoints replaced by mechanical gates (reproduction-required, red-before-green, full-suite-plus-regression, revert-on-collateral) and a shipped/needs-input ledger.
- **Session modules** — ground-truth hierarchy rule (`live state > disk > notes`, correct the lower tier) in `memory-ops.md`; chunk-large-deliverables-to-files rule in CORE Output Defaults.

### Unreleased — Ecosystem audit prune (2026-07-10)

- **13 → 9 plugin namespaces** — schedule (scheduler), devops, prompt, and output-styles retired.
- **Agents 46 → ~23** — 15 removed, 7 consolidated/converted (write 17→10; code 15→5; database-reviewer → code `postgres` skill; image-engineer → image `generation` skill; book-maker absorbed into book-export).
- **Commands 23 → 14, skills 24 → 15** — `/asha:init` merged into `/session:init`; session spawn/agents/stop-agents/note/prime, code:checkpoint, partner-sentiment removed; verify-app folded into `/code:verify`.
- **Portable-first policy adopted** — a Claude-native equivalent is never sufficient grounds to remove a cross-harness component (reopened and kept: code:review, orchestrate stack, session:loop, code:verify, skill-creator, security-review).
- **Panel agents delegable** — all 6 gained frontmatter; vendored `fabricator` replaces the external agent-fabricator dependency.
- **ASHA_ROOT config fallback** — installer writes `asha_root` to `~/.asha/config.json`; commands/hooks resolve it under bare (non-dispatcher) launches.
- Full rulings: `Work/panels/2026-07-10--ecosystem-audit/`.

### Unreleased — Copilot-native distribution + doctor + init-repo (issue #3)

- **`asha build copilot`** — packages namespaces as native Copilot CLI plugins
  (`dist/copilot/`: per-plugin `plugin.json`, converted command-skills,
  `.agent.md` agents, marketplace index + `enabledPlugins` snippet). Verified
  live: local marketplace add → plugin install → skill fires under plain
  `copilot` (CLI 1.0.65). Hooks never packaged (copilot-cli#2540 + schema
  mismatch). Mechanism: [docs/distribution-copilot.md](docs/distribution-copilot.md).
- **`asha doctor`** — front door for `bin/asha-drift-check.sh`, now with a
  copilot target (symlinks, command-skill freshness, guardrails content,
  `--fix` self-heal), bin/identity sections, and a claude hook audit that
  matches by path-prefix (tag-stripped hooks are no longer invisible).
- **`asha init-repo`** — scaffolds `AGENTS.md`, team instruction stubs, and
  `.github/copilot/settings.json` into a target repo; `--check` CI mode with
  managed-marker DRIFT/LOCAL semantics; composes with native `copilot init`.
- **Persona remains wrapper-only by design** (issue #3 proposal 4 declined):
  `asha copilot` is Asha; plain `copilot` is vanilla — parity with `asha
  claude` vs `claude`.

### Unreleased — Codex compatibility refresh

- **Codex hook TOML now emits the documented nested schema** (`[[hooks.Event]]` matcher groups with nested `[[hooks.Event.hooks]]` command handlers) instead of the older flat shape.
- **Codex native execution-policy rules** — `asha install codex` writes `~/.codex/rules/asha.rules` with `prefix_rule()` prompts for narrow high-risk commands (`find /home`, `bfs /home`, destructive git). This is a coarse native fallback while PreToolUse remains unreliable for Codex shell.
- **Codex hook event list refreshed** — includes PreCompact/PostCompact/SubagentStart/SubagentStop, and unsupported Claude-only events still warn/drop.

### v1.19.0 (2026-06-24) — Cross-harness parity: persona, operational layer, Copilot guardrails

- **Copilot persona injection** — fixed (was wrongly "deferred / manual per-project"). `asha copilot` exports `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` at a cache dir whose `.github/instructions/asha.instructions.md` carries the merged identity; per-launch, so plain `copilot` stays persona-free. Verified live on CLI 1.0.63.
- **Operational layer on Codex + Copilot** — `operation.md` + the learnings hot tier now reach both. Codex supports `SessionStart`, but Asha uses the verified file-based `model_instructions_file` path for required context; Copilot receives a second `asha-operational.instructions.md`.
- **Guardrail re-tests** — Copilot 1.0.63 `preToolUse` **fires + denies** (the prior "won't pursue / unsafe" verdict was stale); Codex 0.142 still does **not** fire for shell (`unified_exec`, re-confirmed with a match-all hook + trust-bypass).
- **Copilot guardrails wired** — `copilot_install_hooks()` (was a no-op) writes a dedicated `~/.copilot/hooks/asha-guardrails.json` → new `plugins/session/hooks/handlers/copilot-policy-adapter.sh`, which bridges Copilot's hook contract (flat schema, stdout `permissionDecision`, stdin `toolName`/`toolArgs`) to the shared `policy-guard.sh` + `block-secrets.sh` — no policy logic duplicated. Soft deterrent (copilot-cli#2893, fails open). Historical test result: Claude ✅, Copilot ✅, Codex 0.142 tested shell path ✖; current Codex docs establish partial hook coverage beyond that path.
- Docs: `docs/harness-enforcement.md` rewritten with the live findings; README + INSTALLER harness rows updated. Tests: `test-hooks.sh` Test 105 (adapter); suite 84 hook tests green.

### v1.18.0 (2026-06-17)

- **Dispatcher**: unified the three `asha-{claude,codex,copilot}` launchers into one positional `asha` dispatcher — `asha [install|uninstall] [harness] [args]`. Install/uninstall engines extracted to `lib/`; top-level `install.sh`/`uninstall.sh` are thin shims; `asha-<harness>` kept as back-compat shims.
- **Policy engine**: declarative PreToolUse guardrails (`plugins/session/hooks/handlers/policy-guard.sh` + `policies/rules.json`, optional user layer `~/.asha/policies.json`) — `deny`/`ask`/`max_per_session`, fail-open. Seed rule `no-broad-home-scans`. Claude and Copilot enforcement are live-tested. Codex hooks can cover supported simple Bash, `apply_patch`, and MCP calls, but not every `unified_exec` shell path or every tool class; Asha's 0.142 shell probe did not fire.
- **session_state**: ephemeral per-session counters (`state.sh`, `~/.asha/session-state/`) that make policies stateful (rate limits); cleared at session end.
- **Docs**: new "Harness support & behavior" and "State model: guardrails, session_state, and memory" sections.

### v1.17.0 (2026-03-09)

- **Write v1.5.0**: Claude Book feature parity
  - 3 new agents: book-analyzer, bible-merger, perplexity-improver
  - style-analyzer skill (quantified prose analysis)
  - Total: 16 agents

### v1.16.0 (2026-03-08)

- **Write v1.4.0**: Novel-specific agents from AAS project
  - novel-character-reviewer, novel-continuity-reviewer
  - novel-state-updater, novel-style-linter

### v1.15.0 (2026-03-08)

- **Write v1.3.0**: Perplexity detection and novel state
  - perplexity-gate skill (local Ollama + Ministral)
  - novel-state skill (bible/state/timeline structure)
  - Removed ai-detector (replaced by local perplexity)

### v1.11.0 (2026-02-13)

- **Asha v1.18.0**: Confidence-tracked learnings
  - Learnings rise on confirmation, decay on contradiction
  - Secret scrubbing for event logs
  - ECC review integration

### v1.9.0 (2026-01-29)

- **Panel system v5.0.0**: Full persistence and panel management
  - `--resume <id>`: Continue interrupted panels
  - `--list [--status=X]`: Query panel index
  - Per-phase state files in `Work/panels/`
- **Asha v1.8.0**: Cross-project identity layer
  - `~/.asha/` for identity (soul.md, voice.md, keeper.md)
  - `/asha:save` captures keeper calibration

### v1.8.0 (2026-01-28)

- **Scheduler v0.1.0**: Cron-style task automation
  - Natural language time parsing
  - cron and systemd backend support
  - Rate limiting and security constraints

### v1.7.0 (2026-01-26)

- **Image v1.1.0**: AI image generation
  - comfyui-prompt-engineer agent
  - SD prompt crafting and workflow design

### v1.6.0 (2026-01-26)

- **Domain restructuring**: Organized by workflow type
- **Code v1.1.0**: Development workflows, 15 agents
- **Write v1.2.0**: Creative writing, prose craft

### v1.5.0 (2026-01-16)

- Fixed hook handler permissions
- Version validation script
- Asha v1.5.0 with robust memory indexing

### v1.3.0 (2026-01-07)

- Panel system v4.2.0 with --format and --context flags
- Audit and cleanup of stale references

### v1.0.0 (2025-11-08)

- Initial marketplace release

## Engineering release history

Entries below v2.7.0 are release records, not current instructions. Memory v2
supersedes their transcript synthesis, automatic save, retrieval, generic nudge,
operational catalogue/context-brief, and memory curator/steward mechanisms.

### v2.7.0 / Session v2.0.0 (2026-08-13) — Memory System v2

- Semantic publication is explicit-only and validates the two-file compact
  schema before commit/push.
- Cross-harness hooks now maintain bounded project-local recovery snapshots;
  SessionEnd only seals and prunes.
- Learnings use candidate/active/retired states with stable session/project
  evidence. Transcript synthesis, auto-save, retrieval/nudges, operational
  catalogues/context brief, and memory curator/steward surfaces were removed.

### v2.6.0 / Session v1.21.0 (2026-08-11) — OpenCode stable-v1 harness

- Reinstated OpenCode as the fourth harness with native plural config surfaces,
  generated command/subagent Markdown, plugin policy/context/lifecycle wiring,
  wrapper-scoped persona, dispatcher/install/uninstall/doctor coverage, and a
  minimum CLI version of 1.15.11.
- Added exact-session SQLite transcript parsing from `opencode.db`, child to
  root resolution, project validation, and OpenCode branches throughout the
  save/preflight pipeline.
- Lifecycle claim is deliberately bounded: manual save plus best-effort clean
  exit from plugin `dispose`; no idle checkpointing. Repository tests cover the
  renderer and backend; production plugin delivery remains un-attested.

### Session v1.20.0 (2026-08-08) — workspace v2–v6 + evidence brokerage

PR #51 closed the remaining roadmap in one batch: issues #23–#27 (registry,
adapters, brokerage) and #45–#50 (workspace read side).

**Read side (v2)** — `workspace_status.py --context` renders one bounded
background block: workspace name/root/active child plus only the first `##`
section of the workspace operational `Memory/activeContext.md`. Sanitization
precedes UTF-8 byte caps, canonical containment stops a symlinked memory path
importing foreign content, and no manifest means no output and no renderer
startup. Excerpt budget 2048 bytes (`ASHA_WS_CONTEXT_MAX`; below 256 or invalid
reverts). Kill switches: `ASHA_WS_INJECT=0`, `Work/markers/nudge-ws-context-off`,
`Work/markers/silence`. Claude and Codex deliver directly at SessionStart;
Copilot uses the direct SessionStart handler on native `sessionStart`, returning
top-level `additionalContext` (raw unshaped stdout is neither used nor claimed).
Live-verified on Claude Code 2.1.226, Codex 0.147, Copilot CLI 1.0.78. The old
first-prompt fallback and its 1 h cooldown are gone. Retrieval discovers the
contained workspace operational plane as source `workspace`, excludes it from
ordinary project memory, and orders it after `memory`/`learning` only on exact
ranking ties.

**Management CLI (v3–v6)** — `asha workspace init|discover|doctor`,
`knowledge init|lint`, `promote plan|apply|publish`, `worktree create|status|
remove`, `work-item …`. The shell layer only routes; each Python core owns its
flags and validation. Every mutation is explicit: `promote plan` writes a
digest-bound artifact (source, evidence, target preimages, base commit,
credential-free GitHub identity); `apply` takes only that artifact plus digest
plus `--confirm`, revalidates every preimage, and runs no Git; `publish` refuses
a dirty shared root, stages only the reviewed write-set on a digest-named
branch, and opens a draft PR — never merging or direct-pushing the base. GitHub
via `gh` is the only shipped review adapter; other forges fail closed rather
than being guessed equivalent. Local commit/push hooks stay off unless
`--run-git-hooks` is passed. Work-item import is offline behind a scrubbed
preview token; `worktree-seed` emits data only.

**Brokerage (#27)** — opt-in `asha context brief` / `process route` /
`capabilities match`, all deterministic inline protocols over
`plugins/session/broker/capabilities.json`. `context brief` reads catalogue
metadata only (never recursive scans, transcripts, or indexed bodies), bounds
work with `--budget-bytes`/`--timeout-ms`, reports exhaustion instead of
broadening, and emits a `source_signature` for reuse. This historical release
also shipped memory-steward/curator wrappers, which v2.7.0 later retired;
process-router and capability-broker remain available. Contract:
[docs/evidence-backed-brokerage.md](docs/evidence-backed-brokerage.md).

Seven new Python suites landed with it (broker, memory_retrieval, workspace
init/knowledge/status/workitems/worktree). **Process note**: this arrived as a
single ~10k-line agent commit with no GitHub review — its correctness/security
passes and three-harness probes were run in-session, so the evidence is not on
the PR. Against the repo's own >1000-line splitting guidance, treat the batch
boundary as the thing to avoid repeating, not the work.

### Session v1.19.0 (2026-08-08) — copilot commit gate chained (issue #40)

`copilot-policy-adapter.sh` carries payload `cwd` through the translation
(was dropped; the gate is cwd-sensitive) and chains `save-commit-gate.sh`.
Adapter-level deny/allow/self-filter pinned in Test 105; live in-session
deny probe deferred to the post-merge smoke. Writer-side proof remains the
primary protection (upstream concurrency caveat #2893).

### Workspace v1 complete (2026-08-08) — parity attested, ship gate closed (session v1.18.0)

Issue #39, second attempt — the first was HELD in pass-2 (env-shaped probes
≠ harness-integration evidence). Re-attested under each harness's real
runtime (`codex exec` / `copilot -p` running the probes through their own
shell tools): Codex 0.147 verified detection + proof + isolation + **gate
deny enforced** (overturns the 0.142 no-fire verdict); Copilot 1.0.75
verified detection + proof + isolation with the gate confirmed absent
(issue #40). The copilot auto-save hole (#36 deferral) was demonstrated
end-to-end live, then closed: `tools/auto-commit-memory.sh` is the
plane-aware writer seam for the automatic path (Test 9d) — the PreToolUse
gate cannot see hook-context commits on any harness, so the writer seam is
the auto path's only protection. Doctor surfaces per-harness workspace
capability limitations (WS-12..14); proposal amended with the
capabilities-schema ruling; rendered save skills regenerated for `--scope`.
All six delivery issues of the ratified proposal are shipped.

### Session v1.17.0 (2026-08-08) — save scopes + state-based commit gate

Workspace v1 delivery issue 4 (issue #36). `/save --scope repo|workspace|
none`; `save_scope.py` writer seam (plane mapping as three distinct values,
versioned proof, verify-before-commit); commit gate selects planes by
staged STATE, never command parsing — bash existence walk keeps no-manifest
byte-identity at zero python cost (golden corpus), manifest-present-but-
unvalidatable fails closed, ambiguity denies, proofs are plane-bound. Stop
hook routes v2 locators to the structural proof. Tests-first throughout.

### Session v1.16.0 (2026-08-08) — `asha workspace status` + doctor section

Workspace v1 delivery issue 3 (issue #35): first consumer of the detection
primitive. New `workspace` dispatcher verb (thin lib/workspace.sh shim over
`tools/workspace_status.py`), doctor workspace section (silent outside
workspaces, fail-closed on invalid manifests), and the ratified manifest
convention (committed in shared_git_root; untracked warns; invalid gets
guided repair, never auto-fix). Suite 15 + 12 unit tests, tests-first.

### Session v1.15.0 (2026-08-07) — project-root consolidation + workspace walk

Workspace v1 delivery issue 2 (issue #33). Six divergent detection copies →
one resolver per language (`tools/project-root.sh`, `tools/project_root.py`),
callers declare historical layer sets, byte-identical per Test 9b pins +
existing Python suites. New unconsumed `detect_workspace()` primitive
($HOME and / exclusive, canonical comparison, typed verdict on invalid
manifests). Exempt-by-design detection sites catalogued in the PR audit.

### Session v1.14.0 (2026-08-07) — workspace manifest validator

Workspace v1 delivery issue 1 (issue #31, ratified proposal): pure lexical
`workspace_manifest.py` — typed collected errors, fail-closed, defaults,
containment + disjointness + the `operational_root == Memory` v1 pin,
unknown keys preserved. Filesystem checks deferred to detection/status by
design. 38 RED-first tests.

### Session v1.13.0 (2026-08-07) — destructive-git cross-repo arm

Workspace-memory v1, delivery issue 5 (ratified proposal
`docs/proposals/2026-08-06--workspace-memory.md`; built first as the
independent increment closing a live gap). `destructive-git` now consumes
optional `-C`/`--git-dir`/`--work-tree` global flags between `git` and the
destructive verb — `git -C /ws push --force` previously evaded every arm.
Cross-repo plain commit/push stays allowed (workspace saves). Pass-2 codex
review of the fix itself found and fixed three more bypasses (9/9 for the
rule): mixed-quoted path tokens, exclusion laundering via a safe token in a
later segment (exclusions now segment-scoped), and backslash-newline
continuation (evaluator now normalizes to what bash executes). Issue-loop
preflight gained a cross-repo MUST_DENY probe against silently-weakened user
overlays. 21 Test 104 pins; residuals (`-c`, `--no-pager` still bypass;
escaped-space paths fail toward allow; quoted mentions false-positive
safe-side) documented in the rule's `_comment` per house style.

### v2.5.0 (2026-08-05) — Overnight issue-to-merge loop

Implements the deferred spec (`docs/proposals/2026-08-04--issue-to-merge-loop.md`). Code plugin v1.5.0: `/code:issue-loop` command + `engines/issue-loop.js` (first code-plugin engine; write/engines Workflow-script precedent) + two rail scripts.

- **Rails were built first, tests-first**: `tools/issue-loop-preflight.sh` (dual opt-in — project `.asha/issue-loop.json` AND `~/.asha/config.json` `issue_loop.repos` allowlist; gh probe; `.asha/worktrees/` must be git-ignored; **live rail-6 guard self-check**: the loop's own command set is piped through policy-guard with the user overlay merged, and either a denied loop command or a weakened deny-side protection refuses dispatch). `tools/issue-loop-publish.sh` is the sole push path — never main/master, prefix-enforced branches, registered worktrees only, clean trees only, `--draft` hardcoded. A *global* deny on pushing main was deliberately rejected: plain push is a pinned-intentional allow for humans (Test 104), so the rail is structural instead.
- **Engine verdict discipline is commission-loop's, verbatim in spirit**: uncertainty fails, dead agents fail their item, findings outrank the verdict label (triage's five-criterion conjunction recomputed engine-side; a "candidate" without failing-test/worktree/branch evidence is demoted by the engine). The reviewer is cold — diff + issue text only — and scope is judged by the Change Budget rule. Report is mandatory; a dead report agent hands the caller a `fallback_report` and the duty to write it.
- Tests: `tests/test-issue-loop.sh` (Suite 14, 20 cases, fixture repos + PATH-shimmed gh), `tests/js/issue-loop.test.mjs` (13 wiring scenarios incl. source-purity), 7 issue-loop pins in Test 104 (worktree add/remove, `gh pr create --draft`, plain `push -u` allowed; force-push and `rm -rf .asha/worktrees/…` stay denied — cleanup is `git worktree remove` by convention).
- Scope held to the proposal's v1: no tracker comments (no outward-write path exists), no auto-merge ever, reports in `Work/loops/<run-id>/` with manual pruning, and no runs against asha itself until the loop has a track record elsewhere.

### v2.4.0 (2026-08-04) — Usage-insights remediation

Five repairs from a `/insights` review of 145 sessions; each routes a recurring real-world failure to the mechanism that should already have owned it. Session v1.12.0, RP v0.2.0.

- **Guardrails**: new `destructive-delete` policy rule (`rm -r/-f`, glob/archive `rm`, `shred`, `gh repo delete`); `destructive-git` extended with `filter-repo`/`filter-branch`. Exemptions (`docker rm`, `git rm`, `npm rm`, `node_modules`, `.venv`, `/tmp`) are load-bearing — an over-broad deny gets switched off and then guards nothing. Test 104 +19 cases.
- **Verification**: `modules/research.md` gains "Negative Claims Require an Evidence Trail". The existing severity markers only covered hedged claims; confident assertions of *absence* were the top correction category. Two generalizations worth remembering — **a pin is a claim, not evidence**, and **a cache is not its source**.
- **Agent authority**: `roleplay-gm` is now `Task, Read, Grep, Glob`. It previously held `Edit, Write, Bash` it was never told to use, while *lacking* the `Read` its own instructions demanded — drafting blind against `Memory/invariants.md` yet able to write past the continuity gate that the calling command is supposed to own. When auditing an agent, check the allowlist against the instructions in both directions: unearned authority *and* missing capability are the same defect.
- **RP portability**: canon paths resolve through a project-owned `Memory/canon-layout.md` (template in `plugins/rp/templates/`); historical defaults preserved so existing projects need no edit. Campaign proper nouns removed from shipped primitives.

**Core de-personalization** (same rule, applied to the layer every project installs):

- `no-broad-home-scans` hardcoded the maintainer's account (`/home(/pknull)?`), so a full scan of any *other* user's home was allowed — the guard protected one machine. Now username-agnostic.
- `templates/recall_fixtures.yaml` seeded the maintainer's personal recall benchmark into every new `~/.asha/` (`lib/install.sh`). Its fixtures name memories no other install can have, so they score 0 permanently — the exact failure the file's own comment warns about ("a permanently impossible fixture would conceal real score regressions"). Now a documented empty starter; live user files are untouched, since install seeds only when absent.
- `pattern_analyzer.py` and the memory-maintenance skill lost their "AAS vault" / `pk_lintop` references.

**`write` plugin de-specialized (v1.8.0)** — the last domain plugin carrying one project's material:

- `prose-analysis` hardcoded `Vault/Docs/MasterWritingStyleGuide.md` *and* instructed itself to "**always** read it first". Elsewhere that resolved nothing and the agent proceeded on assumed voice standards — the silent-empty-result failure again. Now a convention search, an explicit declaration that overrides it, and a refusal to proceed when nothing resolves.
- A "Hush-Specific Checks" block shipped one project's coined transformation-anatomy terms and body-location constraints inside a generic agent. Replaced with the generalizable pattern: constrained-term checking with negative cases written out.
- `craft-core-universal`'s `rp`/`hush` mapping table became a template for a project's own. Note the engine was *already* generic — only its description string claimed a fixed profile list, which is the cheapest kind of drift to miss.

**Adversarial review pass (same day)** — the remediation itself was reviewed (Codex externally + self-review) before release; every finding verified against the live guard before fixing. Headlines: the delete rule denied the toolkit's own `rm -f Work/markers/…` cleanup (BLOCKER — an over-broad guard gets disabled and then guards nothing, this time proven against ourselves); exemption strings anywhere in a command suppressed the whole rule (now segment-scoped, archives get a prior rule with zero path exemptions); `~`/`$HOME`/quoted home scans bypassed entirely; the priced-stakes nudge never fired on sentence-initial capitals (`match_ci` engine flag added) and fired on `impact` via `pact` (word boundaries); the roleplay-gm allowlist cut orphaned scene-state maintenance (now `SCENE_STATE_DELTA`, applied by `/rp:turn` only on clean verdict); stale README detail-section versions escaped `validate-versions.sh` (new Test 5). Lesson reinforced: **verify a rule against the workflows that ship beside it, not only against the incident that motivated it** — and a reviewer's findings are claims until probed against the live mechanism — all 13 verified as real here, but two were resolved differently than proposed (the overloaded vault roots stay, being warn-only and user-overridable; `/rp:end` stages canon-writer's reported file list rather than gaining a register "root" field).

> **Rule established here**: a shipped primitive must not contain one project's proper nouns, directory tree, machine, or account name. A `match_regex` keyed to one campaign's vocabulary fires only there; a hardcoded canon path resolves to nothing elsewhere — and an empty result is indistinguishable from "no such thing exists", so the failure is silent. Provenance in a `_comment` (naming the campaign a bug was diagnosed in) is fine and useful; *operative* strings must be setting-agnostic. Projects extend shipped rules via `~/.asha/nudges.json` / `~/.asha/policies.json`, merged by id.

### v2.3.0 (2026-07-27) — OpenCode support dropped

- Operator decision after the #14 plugin-API survey: OpenCode ≥1.18 stores transcripts in sqlite, which broke memory capture; support was removed rather than maintained. Three-harness toolkit now (Claude, Codex, Copilot). All opencode code paths deleted (adapter, policy plugin, jsonl_reader backend, dispatcher/doctor wiring, tests); live artifacts uninstalled first; retirement record + final survey verdicts in `docs/harness-enforcement.md`. Session plugin v1.11.0.

### Session v1.10.0 (2026-07-27) — Copilot lifecycle: auto-save + orphan recovery (issue #13)

- Live-probed copilot 1.0.75 sessionEnd (fires on clean exit; reasons `complete`/`user_exit`; camelCase payload, no transcript_path) and wired `~/.copilot/hooks/asha-lifecycle.json`: sessionStart → session-start.sh side effects, sessionEnd → session-end.sh detached auto-save. Verified end-to-end: clean exit synthesizes `Memory/activeContext.md` with all provenance gates passing; SIGKILL mid-session recovers from the native transcript at next session start (identity breadcrumb event replaces the per-tool capture copilot lacks).
- False-orphan guard in `check_orphaned_session` (wwa-session stamp = already published) — fixes redundant post-save re-recovery on Claude too. Doctor byte-checks guardrails + nudges + lifecycle files; uninstall symmetric. Verdicts: `docs/harness-enforcement.md` "Copilot lifecycle".

### Session v1.9.0 (2026-07-27) — Codex PostToolUse verdict: fires but discards (issue #15)

- Live-probed codex 0.145 PostToolUse (isolated `CODEX_HOME`, UserPromptSubmit sentinel as positive control): the event fires for shell and successful `apply_patch` with a full Claude-shaped payload (`hook_event_name` present — argument-free registration works; native tool names: `Bash`, `apply_patch`), but hook stdout is discarded entirely — no injection channel exists for this event. Codex honors `matcher` and aliases `apply_patch` into `Edit|Write|MultiEdit`.
- `suggest-compact` row harness-gated to claude+copilot: an ungated row burned tool-count and the 2h cooldown on discarded output, suppressing the nudge for later Claude sessions. Test 92f guards the gate. Verdict: `docs/harness-enforcement.md` "Codex PostToolUse".

### Session v1.8.0 (2026-07-27) — Learnings durability: migration decoy fix (issue #12)

- `migrate-okf` now stamps legacy flat files with an idempotent supersession banner after a successful migration (content preserved verbatim; writes through symlinks so externally-tracked copies self-describe as stale) and warns — stderr + report JSON — when a legacy file resolves outside `~/.asha`, i.e. an existing backup arrangement does not cover the bundle directory the store moved to.
- New `learnings_manager.py legacy-status` divergence check, wired warn-only into `/save` and `/session:consolidate`; durability posture documented in `docs/memory-architecture.md` "Durability & backup".

### Session v1.6.0 (2026-07-26) — Copilot guidance-nudge parity

- Copilot 1.0.68 hook contract live-probed and wired: argv event names (no `hook_event_name` in payloads), `COPILOT_CLI=1` harness detection, injection solely via top-level `{"additionalContext": ...}` JSON (raw stdout discarded). New `hooks/asha-nudges.json` registration (userPromptSubmitted + postToolUse), symmetric uninstall. Production RP probe: INJECTED. Opt-in follow-up: sessionStart/sessionEnd side-effect wiring (auto-save parity).

### Codex hook enablement (2026-07-26) — feature gate, trust preservation, doctor coverage

- `_codex_ensure_hooks_feature` adds `[features] hooks = true` when absent (never rewrites an explicit value); the fence excise now preserves codex-owned `[hooks.state]` trust subtables it previously destroyed on every reinstall (Test 106d); doctor's codex hook checks fixed (nested command walk, feature-gate + trust-slot report). Verdict: `docs/harness-enforcement.md` "Codex hook gating".

### Session v1.5.0 (2026-07-26) — Memory recall economics

- Index-first injection: SessionStart injects `render-index` (one capped line per concept, whole bundle, truncation tail) instead of top-10 full bodies; `ASHA_LEARNINGS_INJECT=hot` reverts.
- New `/session:consolidate` four-phase compaction + `learnings_manager.py retire` (concluded records → `~/.asha/learnings-archive/`).
- Broad-entry scrutiny in retrieval: BM25-style length normalization in `rank()`; nudge firing gates (broad entries: no lone-rare-token fires, three agreeing tokens required). Recall bench held 12/13.

### Session v1.4.0 (2026-07-25) — Declarative guidance-nudge engine

- Advisory counterpart to the policy guard: `hooks/handlers/nudge-engine.sh` evaluates `hooks/nudges/rules.json` (+ `~/.asha/nudges.json`, merged by id) and injects context fragments — never blocking. Pattern extracted from severity1/claude-code-prompt-improver; its payload nudges were not adopted.
- Migrated to registry rows, bespoke scripts retired: `memory-lexical` (was `hooks/memory_nudge.sh`), `rp-routing` (fragment file replaces the text inlined in `harness-response.sh`), `suggest-compact` (was `handlers/suggest-compact.sh`).
- Event resolves from the stdin payload's `hook_event_name` (argument-free registration); per-row gates + `disable_env` / `nudge-<id>-off` kill switches; engine-managed cooldowns.

### Admin v0.3.0 (2026-07-23) — Proton Mail Bridge skill

- Added a localhost-only, verified-STARTTLS Proton Mail Bridge helper for safe reads and hash-bound two-phase mail writes.
- Added structured search, bounded MIME parsing, mailbox/UIDVALIDITY/UID identity, native MOVE-only moves, move-to-Trash deletion, draft APPEND, Bcc envelope privacy, and credential redaction.

### v2.2.0 (2026-07-22) — Audit remediation

- Full-project audit (Work/audit/2026-07-22--project-audit.md): all ten findings fixed.
- Session v1.3.0 (dead memory-index feature removed; run-python.sh orphan deleted; jsonl_reader self-contained; skills documented), Code v1.4.0 (`asha calibration` dispatcher verb; postgres skill documented), Admin v0.2.0 (prose de-localized).
- Installer: per-harness failure isolation in `asha_install_main` (mirrors uninstall's issue-#4 pattern).
- `~/life` paths swept from all shipped prose; version tables re-synced.
- Tests: shellcheck scope extended to bin/lib/harnesses/identity; validate-versions cross-checks plugin READMEs vs both top-level tables; new install round-trip + identity-merge smoke suites; bash-safety flags classified repo-wide.

### v2.1.0 (2026-07-10) — Ecosystem audit prune

- **13 → 9 plugin namespaces** — schedule (scheduler), devops, prompt, output-styles retired.
- **Agents 46 → ~23** — write 17→10 (consolidations: continuity-reviewer, prose-analysis, voice-analyst, intimacy-arbiter); code 15→5; database-reviewer → code `postgres` skill; image-engineer → image `generation` skill; book-maker absorbed into book-export.
- **Commands 23 → 14, skills 24 → 15** — `/asha:init` merged into `/session:init`; session spawn/agents/stop-agents/note/prime, code:checkpoint, partner-sentiment, task-manager, verify-app removed (verify lives on as `/code:verify`).
- **Portable-first policy adopted** — Claude-native equivalents are never sufficient removal grounds for cross-harness components.
- **Panel**: all 6 agents gained frontmatter (delegable on Claude); vendored `fabricator` replaces the external agent-fabricator dependency; harness-aware Role Execution Model in `/panel`.
- **ASHA_ROOT config fallback** — resolves from `~/.asha/config.json` under bare launches.
- Doc sync: marketplace-era sections (plugin.json / marketplace.json) removed from this guide. Full rulings: `Work/panels/2026-07-10--ecosystem-audit/`.

### v2.0.0 (2026-06-18) — Asha learnings: OKF bundle

- **Breaking (on-disk format):** learnings moved from a single flat `~/.asha/learnings.md` to an OKF concept bundle (`~/.asha/learnings/`, one file per learning, `type: learning`, auto-generated `index.md`). One-way migration via `plugins/session/tools/migrate_learnings_to_okf.py`; older asha versions cannot read the bundle — pin the matching version per repo.
- Upsert-by-id dedup; vendored OKF `validate.py`/`visualize.py`; warn-only validate-on-`/save`.
- Auto-suggested `## Related` cross-links at interactive `/save` (semantic, non-blocking).
- New `docs/memory-architecture.md` (scopes, lifecycle, "is it providing value?" guide).

### v1.9.0 (2026-01-29)

- **Panel system v5.0.0**: Full state persistence and panel management
  - `--resume <id>`: Continue interrupted panels from last completed phase
  - `--list [--status=X]`: Query panel index with optional filtering
  - `--show <id>`: Display panel summary
  - `--abandon <id>`: Mark panels as abandoned
  - Output moved from `Work/meetings/` to `Work/panels/` with per-phase state files
  - New files: `state.json`, `index.json`, `phase-*.md`, `transcript.md`
- **Asha v1.8.0**: Cross-project identity layer
  - New `~/.asha/` directory for user-scope identity (not committed to repos)
  - `communicationStyle.md`: Who Asha is (voice, persona, constraints)
  - `keeper.md`: Who you are (calibration signals via `/save`)
  - Session-start hook auto-injects identity files from `~/.asha/`
  - `/asha:init` bootstraps both identity layer and project Memory
  - `/asha:save` captures keeper calibration signals to `~/.asha/keeper.md`

### v1.8.0 (2026-01-28)

- **New plugin: schedule** — Cron-style task automation with natural language time parsing
  - Natural language parser (20+ expressions: "Every weekday at 9am", "Every 15 minutes", etc.)
  - Task management with rate limiting, duplicate detection, dangerous command blocking
  - systemd timer and cron backend support with automatic detection
  - Execution wrapper with timeout handling, status tracking, audit logging
  - End-to-end tested: tasks execute on schedule, Claude responds correctly

### v1.7.0 (2026-01-26)

- **New plugin: image** — Image generation workflows with Stable Diffusion prompt engineering, ComfyUI workflow design
- Standards compliance audit per Claude Code skills best practices
- Fixed hardcoded paths, added frontmatter to agent files
- All plugin versions incremented for upgrade path

### v1.6.0 (2026-01-26)

- **Domain restructuring**: Organized plugins by workflow type (panel=research, code=dev, write=creative, asha=core)
- **New plugin: code** — Development workflows with codebase-historian agent, orchestration patterns, quality gates, swarm recipes
- **New plugin: write** — Creative writing with 5 specialized agents (outline-architect, prose-writer, consistency-checker, developmental-editor, line-editor) and recipes
- **Absorbed local-review** into code plugin as `/code:review`
- **ACE cycle moved** to asha/modules/cognitive.md as general technique
- Cleaned up asha to core scaffold only (moved domain content to code/write)

### v1.5.0 (2026-01-16)

- Fixed hook handler permissions (711 → 755) and naming consistency (added .sh extensions)
- Added version validation script (tests/validate-versions.sh)
- Synchronized versions across README.md, CLAUDE.md, and plugin.json files
- Asha plugin v1.5.0 with robust memory indexing (retry logic, diagnostics)

### v1.3.0 (2026-01-07)

- Audit and cleanup: Removed stale memory-session-manager references
- Panel system v4.2.0 with --format and --context flags
- Fixed repository structure documentation

### v1.2.0 (2025-11-17)

- Removed AAS-specific universe references
- Updated character names to general-purpose versions:
  - "Asha" → "The Moderator"
  - "The Recruiter" → "The Analyst"
  - "The Adversary" → "The Challenger"
- Generalized character file conventions
- Updated all examples and task patterns with new names

### v1.0.0 (2025-11-17)

- Initial CLAUDE.md creation
- Comprehensive repository analysis
- Documentation of all conventions and patterns
- Plugin system architecture documentation
- Memory system integration guide
- Development workflows and common tasks

---

**Maintained by**: AI assistants working on asha
**Review Cycle**: Update when major structural changes occur
**Validation**: Verify against actual codebase quarterly
