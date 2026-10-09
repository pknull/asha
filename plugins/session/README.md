# Session

**Version**: 2.10.0

Compact explicit memory publication, bounded crash recovery, reviewed learning
lifecycle, policy guardrails, guarded loops, and workspace management.

## When to use it

Use Session to initialize project memory, publish a handoff, inspect recovery,
silence persistence, maintain reviewed learnings, run an autonomous loop, or
manage a declared multi-repository workspace. Use `/code:verify` for
verification alone; use RP commands for story-session lifecycle.

## Invocation

Claude exposes `/session:init`, `/session:save`, and the other native slash
commands. Codex, Copilot, and OpenCode render those commands as skills named
`session-init`, `session-save`, and so forth. `asha workspace …`, `asha process
route …`, `asha capabilities match …` and `asha capabilities plan …` are
harness-independent CLI verbs.

## Memory v2

Published semantic memory exists only after explicit `/session:save`:

```text
Memory/activeContext.md   # <=4096 bytes; Objective/State/Next/Blockers
Memory/decisions.md       # current binding decisions only
```

The command authors both files from live model context, validates them, and
publishes them under a project lock with rollback/recovery journaling before
showing the diff. It then commits and pushes by default. `--no-push` stops after commit;
`--scope none` publishes without Git. No lifecycle hook, transcript parser,
timer, or background process may publish semantic memory.

Hooks maintain ignored crash-recovery hints:

```text
Work/session-state/<harness>-<session>.json   # <=2048 bytes, mode 0600, 7 days
```

Snapshots record bounded prompt hints, touched paths, last mechanical action,
and a blocker indicator. They are unpublished and must be verified against
disk. `Work/markers/silence` disables all persistence.

Global learnings are explicit files under
`~/.asha/learnings/{candidate,active,retired}/`. Evidence is deduplicated by
stable `(session_id, project_id)`. A candidate activates only after three
distinct sessions across two projects. Evidence uses a local session-id
heuristic resolved by explicit save; it is not a security authority.
Only active learnings load at start. SessionStart also reads the initialized
project's published pair through the shared lock and retires candidates older
than 90 days.

## Commands

| Command | Role |
|---|---|
| `init` | Create/preserve the v2 files, stable project id, and recovery ignore rule |
| `save` | Sole semantic publisher; validate, commit, and push explicitly |
| `status` | Report publication validation, newest recovery hint, and learning states |
| `silence` / `restore` | Disable or re-enable all persistence |
| `loop` | Guarded autonomous workflow with explicit checkpoints |

## Agents

| Agent | Role |
|---|---|
| `loop-operator` | Operate bounded autonomous loops |
| `process-router` | Recommend a registry-backed process without executing it |
| `capability-broker` | Match tasks to verified harness capabilities; show one named capability's read-only dependency plan |

Removed in v2: memory steward and curator. Context is now direct and bounded.
The reviewed v1 migration (`/session:consolidate`) was retired in 2.7.0; see
[Legacy Memory](../../docs/memory-architecture.md#legacy-memory).

## Skills

| Skill | Role |
|---|---|
| `memory-maintenance` | Memory v2 schema, recovery and learning rules, and how init treats legacy files |
| `operate-control` | Launch, inspect and steer project sessions from the chair: jobs, Rooms, structured utilities, questions and results |
| `skill-creator` | Create or update Codex-compatible skills |

## Hooks

| Event | Behavior |
|---|---|
| `SessionStart` | Coherently inject the project's published pair, operation rules, active learnings, workspace context, and any verify-first recovery hint; then expire stale private state |
| `UserPromptSubmit` | Update prompt recovery; directly deliver RP routing when active |
| `PostToolUse` | Update bounded paths/action/blocker recovery |
| `Stop` verification pass | Recheck `Work/markers/pass-declaration.json`; block one Claude/Codex stop retry while the old value remains, or clear the marker after an empty proof |
| `SessionEnd` | Seal timestamp and prune only |
| `PreToolUse` | Independent secret and policy guardrails |

Copilot receives one generated `asha-recovery.json`. OpenCode's generated
plugin calls the same four recovery handlers and seals on `dispose`; it never
starts a save. Codex renders the shared native hooks into an owned `hooks.json`.

## Workspace boundary

Workspace operational publication uses the same two v2 files. The shared
`knowledge/` notes and private `memory-local/` are separate planes: workspace
init creates both (only `memory-local/.gitkeep` inside) and the ignore policy
governs them; Asha writes nothing else there. The workspace CLI is
`asha workspace status|init|discover|doctor`.

A session launched at a workspace root receives that workspace publication
once plus workspace metadata. A session launched in a declared child receives
the child's project publication and the workspace publication. This is two
intentional planes, not a duplicated copy: project state answers what is true
for the repository; workspace state answers what coordinates the repositories.

## End-to-end example

```text
/session:init
# work normally; hooks maintain only ignored recovery hints
/session:status
/session:save --no-push "Implement parser boundary"
# inspect/test the local commit, then push deliberately
```

## Configuration and safety

- `.asha/config.json` must carry a stable `project_id` and `memory_version: 2`.
- Publication is limited to the selected plane's two files.
- Init never deletes legacy (pre-v2) files. It refuses a published
  `activeContext.md` or `decisions.md` that is not valid v2 until you rewrite it
  in the v2 format; nothing migrates it (see
  [Legacy Memory](../../docs/memory-architecture.md#legacy-memory)).
- Publication runs only through `/session:save` or a Control session handoff.
- Generated installers prune removed Copilot/OpenCode artifacts; uninstall
  preserves modified generated files for review.
- Run `./tests/run-tests.sh`; for harness work also run Codex/OpenCode drift
  checks required by `AGENTS.md`.

## Version history

### 2.10.0

Control's bulk owner start and stop walks (#122) each read a partial index in
`(created_at, session_id)` order, `managed_session_runnable` or
`managed_session_stopping`, and resume with a row-value cursor, so their query
work is linear in the walk's rows; an existing database gains the indexes on
its next write open (structured launch, `session create` or `session init`).
The Memory architecture guide now names Codex's owned `hooks.json` (#123).

`asha capabilities plan <id>` (issue #124) returns a read-only dependency plan
for one explicitly named broker capability: typed dependency edges with their
conditions, a dependencies-first order, human prerequisites, configuration
presence, missing and unverified items, approvals, blockers and fallback.
`plugins/session/broker/capabilities.json` is the only dependency authority:
edges are validated at load (unknown targets, duplicates, cycles, inactive
edges included) and overrides cannot change them. Declared harness support is
reported apart from availability, which stays unverified; `--probe` looks
conditional commands up on `PATH` without running them, root-first, and a
missing required command reads `needs-foundation`. The plan writes nothing,
telemetry included. `process route` output is unchanged; `capabilities match`
is unchanged except that tasks naming GitHub now also select `github-cli`
(pinned against 562869a0 by a fixture). The registry gains `github-cli` and
its conditional `github-cli-setup` foundation, and the capability-broker agent
documents the plan.

### 2.9.3

Current guidance describes the shipped system (parity review P3/P4): the
"migrate legacy stores" use and the migration completion-marker promise are
gone, the `memory-maintenance` row names what init does with legacy files, and
Codex hooks are described as an owned `hooks.json`, not TOML.

### 2.9.2

The `operate-control` skill describes on-demand structured owners (subtraction
value call N1): queued work starts its owner, `session show` or `session list`
restarts one lost to a crash or reboot, and runtime admission lives under
`asha control session admission`. The supervisor daemon it named is retired.

### 2.9.1

Same-user path shapes no longer refuse Memory, saves or hooks (Keeper threat
model, 2026-10-05): a project reached through a symlinked directory resolves to
its real tree, Memory and draft reads ignore link counts and the file owner,
opt-in broker telemetry appends through a symlinked events file, and
`verify-pass-complete.sh` follows a symlinked pass declaration (an empty proof
removes the link, never its target). `secure_path` still refuses symlinks below
a project root, home is still never a project root, and reads still refuse
FIFOs and oversized files without blocking.

### 2.9.0

Removed the workspace knowledge, promotion, work-item and worktree tools
(`asha workspace knowledge|promote|work-item|worktree`, subtraction value call
N3): `workspace_knowledge.py`, `workspace_workitems.py` and
`workspace_worktree.py` with their tests. The old nouns refuse by name. Workspace
init still creates the manifest's shared root, now as an empty folder with no
scaffold or ownership registry, and the generated `AGENTS.md` no longer points
at a knowledge index or promotion; doctor no longer lints or repairs that
folder and drops `promotion_available`. Existing `knowledge/` folders stay as
ordinary Markdown. Init and doctor `--fix` now roll back correctly when the
private-root ignore probe fails (the rollback called a helper that did not
exist).

### 2.8.0

Session experience capture retired (subtraction N2): `/session:save` no longer
reads experience policy, reviews reports or records dispositions (former steps
2 and 6); save-authored learning proposals are unchanged. The `operate-control`
skill drops the capture, policy and review guidance and keeps selected guidance.

### 2.7.0

Retired the one-shot v1-to-v2 migration (subtraction batch B7, Keeper K5):
`/session:consolidate` and the learning manager's `migrate-plan`,
`migrate-amend` and `migrate-apply` verbs, with their journals, receipts and
marker. Initialization still refuses a published file that is not valid v2;
rewrite it in the v2 format first. The learning manager follows links inside
the bundle (a symlinked bundle root was already supported) and no longer
checks the bundle target's owner. The installer no longer inventories v1
learning stores. Reinstall each harness (`asha install <target>`) to prune the
retired command's link.

### 2.6.0

Retired with the legacy initiative engine (subtraction step L-b): the
`orchestrate-initiative` skill, the `operate-control` advanced-workflow
reference, the hub bridge's legacy `asha control event` branch (outside a hub
session `control-event.sh` now answers `{}` and calls nothing), and the two
coordinator policy rules (`coordinator-no-operator-approval`,
`coordinator-no-authority-grant`). The `operate-control` skill points at the
read-only `asha initiative list|show|export` evidence instead. Reinstall each
harness (`asha install <target>`) to prune the retired skill's link.

### 2.5.1

The `operate-control` skill resolves a project through
`asha control projects --match NAME --json`, the project index's new Control
verb, instead of the retiring `asha initiative projects`.

### 2.5.0

The hub bridge (`control-event.sh`) forwards a SessionStart's payload source,
so Control rebinds a session's native conversation only after `/clear` or once
the bound conversation ended, and a bridge timeout is now a loss metric only:
the late Stop delivery and its background task count are gone. The
`operate-control` and `project-memory` skills describe the two-axis session
status (observed and report) and environment-only reporter identity.

### 2.4.0

Removed the project-local style audit nudge (`style-audit.sh`, its
post-tool wiring and the Copilot next-prompt queue) and the project
`.asha/.venv` interpreter preference. Hooks can run outside the harness
sandbox, so they no longer execute files a sandboxed agent can write. The
`operate-control` skill notes that worker-profile sessions and Codex Rooms
cannot launch, send or run operator verbs; the chair can on every harness.

### 2.3.0

Added the harness-neutral declared revision-pass check and project-local style
audit nudges. Claude/Codex use native Stop/PostToolUse response shapes, Copilot
delivers through its next-prompt context seam, and OpenCode queues handler
output through its generated bridge. Both handlers fail open.

### 2.2.0

Added the `operate-control` skill — the operator-side twin of
`orchestrate-initiative`: a wrapped session drives the Control plane as the
Keeper's chair (launch fenced coordinators, monitor by reading, perform
operator writes only on the Keeper's explicit word, prepare but never
perform integration). Loaded by default into wrapped launches through the
dispatcher's orchestrator-stance brief.

### 2.1.0

Added the `orchestrate-initiative` skill: Asha's own session claims the
coordinator generation of an initiative from its tmux pane, proposes plans,
waits on events in the background, and reports evidence; approval verbs stay
with the Keeper's terminal. The policy guard gained `require_env` so the
coordinator-approval deny rule is inert outside coordinator sessions.

### 2.0.0

Clean break to explicit compact publication, project-local recovery snapshots,
and evidence-gated learning states. Removed transcript/event synthesis,
automatic semantic saves, memory retrieval/nudges, operational catalogues,
confidence tiers, and the memory curator/steward agents.
