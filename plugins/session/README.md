# Session

**Version**: 1.0.0

Session management with memory persistence, pattern extraction, and operational quality.

## What It Does

- Captures session events automatically via hooks
- Synthesizes events into persistent project context (`Memory/activeContext.md`)
- Extracts cross-project learnings with confidence tracking (`~/.asha/learnings.md`)
- Loads operational quality rules (`~/.asha/operation.md`) on every session
- Maintains calibration logs for persona files if they exist (voice.md, keeper.md)

## Installation

```bash
/plugin marketplace add pknull/asha-marketplace
/plugin install session@asha-marketplace
```

Then initialize in your project:

```bash
/session:init
```

## Commands

| Command | Purpose |
|---------|---------|
| `/session:init` | Initialize session management in current project |
| `/session:save` | Save session context to Memory Bank |
| `/session:note` | Add timestamped note to scratchpad |
| `/session:status` | Show current session status |
| `/session:prime` | Interactive codebase exploration |
| `/session:silence` | Toggle silence mode (disable logging) |
| `/session:restore` | Re-enable logging after silence |
| `/session:spawn` | Spawn agent in tmux orchestrator |
| `/session:agents` | List running agents |
| `/session:stop-agents` | Stop agents in orchestrator |
| `/session:loop` | Autonomous agent loop with guardrails |

## Loading Architecture

### Always loaded (every session)

The SessionStart hook loads these on every session:

| File | Purpose |
|------|---------|
| `~/.asha/operation.md` | Operational quality rules, thoroughness rebalancing |
| `~/.asha/learnings.md` | Cross-project patterns with confidence tracking |

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
| `learnings.md` | Patterns from experience | Via `/session:save` |
| `config.json` | Settings | When config changes |

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
| `verify-app` | Run tests, type checks, lints after changes |
| `task-manager` | Todoist integration for task retrieval |
| `loop-operator` | Autonomous loop with safety guardrails |

## Hooks

| Hook | Purpose |
|------|---------|
| SessionStart | Load operation.md + learnings.md; conditionally load persona files |
| PostToolUse | Capture session events to JSONL |
| UserPromptSubmit | Track user interaction patterns |
| SessionEnd | Synthesize session on clean exit |

## Persona Plugins

This plugin provides the infrastructure. Persona plugins provide identity:

- **asha-persona** — Asha, threshold guardian and knowledge custodian

Persona plugins create identity files in `~/.asha/` and provide a wrapper script that sets `ASHA_PERSONA=1`. The session plugin's save process automatically maintains any persona files that exist (voice calibration, keeper signals).

## License

MIT License
