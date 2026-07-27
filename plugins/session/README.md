# Session

**Version**: 1.10.1

Session management with memory persistence, pattern extraction, and operational quality.

## What It Does

- Captures session events automatically via hooks
- Synthesizes events into persistent project context (`Memory/activeContext.md`)
- Extracts cross-project learnings with confidence tracking (`~/.asha/learnings/` OKF bundle)
- Loads operational quality rules (`~/.asha/operation.md`) on every session
- Maintains calibration logs for persona files if they exist (voice.md, keeper.md)

## Installation

```bash
./install.sh
```

Then initialize in your project:

```bash
/session:init
```

## Commands

| Command | Purpose |
|---------|---------|
| `/session:init` | Initialize session management + identity in current project |
| `/session:save` | Save session context to Memory Bank |
| `/session:status` | Show current session status |
| `/session:silence` | Toggle silence mode (disable logging) |
| `/session:restore` | Re-enable logging after silence |
| `/session:loop` | Autonomous agent loop with guardrails |
| `/session:consolidate` | Periodic memory compaction: merge drift, resolve contradictions, retire concluded records, enforce index budgets |

## Loading Architecture

### Always loaded (every session)

The SessionStart hook loads these on every session:

| File | Purpose |
|------|---------|
| `~/.asha/operation.md` | Operational quality rules, thoroughness rebalancing |
| `~/.asha/learnings/` | Cross-project patterns (OKF concept bundle). Index-first injection: one capped line per concept across the whole bundle, hot-first; bodies Read on demand (the memory-lexical nudge points at them). `ASHA_LEARNINGS_INJECT=hot` reverts to the legacy top-10 full-body hot tier |

### Persona layer (optional)

When `ASHA_PERSONA=1` is set (by a persona wrapper like `~/bin/asha`), the hook also loads:

| File | Purpose |
|------|---------|
| `~/.asha/soul.md` | Identity (if exists) |
| `~/.asha/voice.md` | Voice constraints (if exists) |
| `~/.asha/keeper.md` | User profile (if exists) |

This plugin does not create persona files — install a persona plugin (e.g., `asha-persona`) for that.

## Directory Structure

### Cross-project (`~/.asha/`)

| File | Purpose | Update |
|------|---------|--------|
| `operation.md` | Operational quality rules | When rules evolve |
| `learnings/` | Patterns from experience (OKF concept bundle) | Via `/session:save` |
| `config.json` | Settings | When config changes |
| `recall_fixtures.yaml` | Cross-project question -> memory retrieval checks | With diagnosed-failure memories |

### Per-project (`Memory/`)

| File | Purpose |
|------|---------|
| `activeContext.md` | Current project state |
| `projectbrief.md` | Scope, objectives, constraints |
| `workflowProtocols.md` | Validated patterns |
| `techEnvironment.md` | Tools, paths, platform |
| `events/events.jsonl` | Session event log |

## Modules

| Module | Purpose | When to consult |
|--------|---------|-----------------|
| `CORE.md` | Bootstrap (fallback if operation.md missing) | Legacy |
| `cognitive.md` | ACE cycle, parallel execution | Complex tasks |
| `memory-ops.md` | Memory system operations | Session save |
| `research.md` | Authority and verification | Fact-checking |
| `high-stakes.md` | Dangerous operations | Git pushes, deletions |
| `verbalized-sampling.md` | Diversity recovery | Mode collapse |

## Agents

| Agent | Purpose |
|-------|---------|
| `loop-operator` | Autonomous loop with safety guardrails |

(Verification is `/code:verify`; Todoist access is the `admin-todoist` skill.)

## Skills

| Skill | Purpose |
|-------|---------|
| `memory-maintenance` (installs as `session-memory-maintenance`) | Memory file structure guidance: frontmatter schema, update triggers, file interdependencies, validation |
| `skill-creator` (installs as `session-skill-creator`) | Create or update a SKILL.md: frontmatter, progressive disclosure, bundled resources, quality validation |

## Hooks

| Hook | Purpose |
|------|---------|
| SessionStart | Load operation.md + the learnings index (index-first; `ASHA_LEARNINGS_INJECT=hot` reverts); conditionally load persona files; build Claude's compact memory-nudge index |
| PreToolUse | Guardrails (policy-guard) plus guidance nudges — the Claude-only lexical memory nudge for Grep/Bash/WebSearch is now registry row `memory-lexical` (indexes catalogue descriptions only, deduplicates per session, caps at five, fails open, disable with `ASHA_NUDGE=0`). Per-harness enforcement reach → [docs/harness-enforcement.md](../../docs/harness-enforcement.md) |
| PostToolUse | Claude-only memory-nudge acted-tracking on Read; background violation check for Write/Edit/Bash; guidance nudge row `suggest-compact` — capture moved to `/save` jsonl_reader |
| UserPromptSubmit | Guidance nudge row `rp-routing` (re-asserts the per-turn RP routing directive while `rp-active`); harness-appropriate prompt passthrough |
| Stop | Save-preflight cleanup |
| SessionEnd | Synthesize session on clean exit; clear this session's session_state |

### Guidance nudges (advisory layer)

`hooks/handlers/nudge-engine.sh` is the advisory counterpart to the policy
guard: policies constrain (deny/ask), nudges inform (context injection — a
nudge can never block a tool call or a turn). Rows live in
`hooks/nudges/rules.json`; users add or override rows in `~/.asha/nudges.json`
(merged by `id`, user wins) without touching the repo. The engine resolves the
hook event from the stdin payload's `hook_event_name` (Claude/Codex payloads
carry it — argument-free registration there) with `$1` as the override for
harnesses whose payloads do not (Copilot registrations pass the Claude event
name as an argument). Injection shape is per-harness: raw text on Claude,
raw fragments on Codex, and a top-level `{"additionalContext": ...}` JSON
response on Copilot — the only channel it injects (detected via the
`COPILOT_CLI=1` env Copilot stamps on hook processes, so bare launches work).

Per-row gates: `tool` (anchored ERE on tool_name), `match_regex` (ERE on the
event's text fields), `harnesses` allowlist, `marker_required`/`marker_off`,
silence (`silence_gated`, default on), init (`requires_init`, default on),
and an engine-managed `cooldown_hours`. Payloads: inline `inject` text, an
`inject_file` fragment under `hooks/nudges/fragments/`, or an allowlisted
builtin in `handlers/nudge-builtins.sh` for dynamic content (index queries,
stateful counters).

Kill switches, narrowest first: per-row `disable_env` (e.g. `ASHA_NUDGE=0`
for `memory-lexical`), the per-row marker `Work/markers/nudge-<id>-off`
(the legacy `rp-hook-off` marker is also honoured for `rp-routing`), and
`Work/markers/silence` for every silence-gated row.

## Persona Plugins

This plugin provides the infrastructure. Persona plugins provide identity:

- **asha-persona** — Asha, threshold guardian and knowledge custodian

Persona plugins create identity files in `~/.asha/` and provide a wrapper script that sets `ASHA_PERSONA=1`. The session plugin's save process automatically maintains any persona files that exist (voice calibration, keeper signals).

## License

MIT License
