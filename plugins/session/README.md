# Session

**Version**: 2.9.1

Compact explicit memory publication, bounded crash recovery, reviewed learning
lifecycle, policy guardrails, guarded loops, and workspace management.

## When to use it

Use Session to initialize project memory, publish a handoff, inspect recovery,
silence persistence, migrate legacy stores, run an autonomous loop, or manage a
declared multi-repository workspace. Use `/code:verify` for verification alone;
use RP commands for story-session lifecycle.

## Invocation

Claude exposes `/session:init`, `/session:save`, and the other native slash
commands. Codex, Copilot, and OpenCode render those commands as skills named
`session-init`, `session-save`, and so forth. `asha workspace …`, `asha process
route …`, and `asha capabilities match …` are harness-independent CLI verbs.

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
| `capability-broker` | Match tasks to verified harness capabilities |

Removed in v2: memory steward and curator. Context is now direct and bounded.
The reviewed v1 migration (`/session:consolidate`) was retired in 2.7.0.

## Skills

| Skill | Role |
|---|---|
| `memory-maintenance` | Memory v2 schema, recovery, learnings, and migration rules |
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
starts a save. Codex renders the shared native hooks to TOML.

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
- Legacy sources are never deleted by init or migration apply.
- A successful reviewed migration writes a private global completion marker so
  preserved legacy evidence does not produce a warning upon every reinstall.
- Generated installers prune removed Copilot/OpenCode artifacts; uninstall
  preserves modified generated files for review.
- Run `./tests/run-tests.sh`; for harness work also run Codex/OpenCode drift
  checks required by `AGENTS.md`.

## Version history

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
