# Session

**Version**: 2.1.0

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
| `consolidate` | Reviewed, idempotent, non-destructive legacy migration |
| `loop` | Guarded autonomous workflow with explicit checkpoints |

## Agents

| Agent | Role |
|---|---|
| `loop-operator` | Operate bounded autonomous loops |
| `process-router` | Recommend a registry-backed process without executing it |
| `capability-broker` | Match tasks to verified harness capabilities |

Removed in v2: memory steward and curator. Context is now direct and bounded;
migration review is owned by `/session:consolidate`.

## Skills

| Skill | Role |
|---|---|
| `memory-maintenance` | Memory v2 schema, recovery, learnings, and migration rules |
| `orchestrate-initiative` | Run a bounded initiative as its coordinator from Asha's own tmux pane; approval stays with the Keeper's terminal |
| `skill-creator` | Create or update Codex-compatible skills |

## Hooks

| Event | Behavior |
|---|---|
| `SessionStart` | Coherently inject the project's published pair, operation rules, active learnings, workspace context, and any verify-first recovery hint; then expire stale private state |
| `UserPromptSubmit` | Update prompt recovery; directly deliver RP routing when active |
| `PostToolUse` | Update bounded paths/action/blocker recovery |
| `SessionEnd` | Seal timestamp and prune only |
| `PreToolUse` | Independent secret and policy guardrails |

Copilot receives one generated `asha-recovery.json`. OpenCode's generated
plugin calls the same four recovery handlers and seals on `dispose`; it never
starts a save. Codex renders the shared native hooks to TOML.

## Workspace boundary

Workspace operational publication uses the same two v2 files. Canonical
`knowledge/` indexes, private `memory-local/`, reviewed promotion, worktrees,
and work items remain separate infrastructure. The removed operational Memory
catalogue does not remove canonical knowledge indexes.

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
