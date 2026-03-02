# Asha

**Version**: 1.14.0

Cognitive scaffold framework for session coordination and memory persistence.

## Installation

```bash
/plugin marketplace add pknull/asha-marketplace
/plugin install asha@asha-marketplace
```

## Commands

| Command | Purpose |
|---------|---------|
| `/asha:init` | Initialize Asha in current project |
| `/asha:save` | Save session context to Memory Bank |
| `/asha:note` | Add timestamped note to scratchpad |
| `/asha:status` | Show current session status |
| `/asha:prime` | Interactive codebase exploration |
| `/asha:silence` | Toggle silence mode (disable logging) |
| `/asha:restore` | Re-enable logging after silence |
| `/asha:spawn` | Spawn agent in tmux orchestrator |
| `/asha:agents` | List running agents |
| `/asha:stop-agents` | Stop agents in orchestrator |

### /asha:init

Initialize Asha in the current project.

```bash
/asha:init
```

Creates:

```
~/.asha/                    # Cross-project identity (created once)
├── soul.md                 # Who you are (identity, values)
├── voice.md                # How you express (tone, patterns)
├── keeper.md               # Who The Keeper is (calibration)
├── learnings.md            # Cross-project insights
└── config.json

project/                    # Per-project
├── Memory/
│   ├── events/             # Session event log (JSONL)
│   ├── sessions/archive/
│   ├── activeContext.md
│   ├── projectbrief.md
│   ├── workflowProtocols.md
│   ├── techEnvironment.md
│   └── scratchpad.md
├── Work/markers/
├── .asha/config.json
└── CLAUDE.md
```

### /asha:save

Save current session context to Memory Bank.

**Protocol**:

1. Extract session activity (agents, files, decisions)
2. Update Memory files using Four Questions framework
3. Extract keeper signals (cross-project calibration)
4. Extract voice calibration signals
5. Capture session learnings to `~/.asha/learnings.md`
6. Archive session events
7. Commit and push (if remote exists)

### /asha:note

Add timestamped note to scratchpad.

```bash
/asha:note Discovered auth tokens expire after 1 hour
```

Notes are appended to `Memory/scratchpad.md` with UTC timestamps.

### /asha:status

Show current session status and captured activity.

## Memory Architecture

### Identity Layer (`~/.asha/` — cross-project)

| File | Purpose | Update Frequency |
|------|---------|------------------|
| `soul.md` | Who you are (identity, values, nature) | Rarely |
| `voice.md` | How you express (tone, patterns) | When voice needs tuning |
| `keeper.md` | Who The Keeper is (preferences, calibration) | Additive via /save |
| `learnings.md` | Insights from experience | Additive via /save |

### Project Layer (`Memory/` — per-project)

| File | Purpose |
|------|---------|
| `activeContext.md` | Current project state, recent activities |
| `projectbrief.md` | Scope, objectives, constraints |
| `workflowProtocols.md` | Validated patterns, anti-patterns |
| `techEnvironment.md` | Tools, paths, platform capabilities |
| `events/events.jsonl` | Session event log (auto-captured) |

## Session Continuity

1. Each Claude session starts fresh (context resets)
2. Memory Bank is the ONLY connection to previous work
3. Session events captured automatically via hooks
4. `/asha:save` synthesizes events into persistent context
5. Learnings accumulate in `~/.asha/learnings.md` across all projects

## Modules

| Module | Purpose |
|--------|---------|
| `CORE.md` | Bootstrap protocol, identity, universal constraints |
| `cognitive.md` | ACE cycle, parallel execution, tool efficiency |
| `memory-ops.md` | Memory system operations, trimming protocols |
| `research.md` | Authority and verification |
| `high-stakes.md` | Dangerous operations protocol |
| `verbalized-sampling.md` | Diversity recovery technique |

## Agents

| Agent | Purpose |
|-------|---------|
| `verify-app` | Run tests, type checks, lints after changes |
| `task-manager` | Todoist integration for task retrieval |
| `partner-sentiment` | Session haiku generation |

## Hooks

- **SessionStart**: Injects CORE.md and identity files
- **PostToolUse**: Captures session events to JSONL

## Git Integration

Sessions are preserved via git:

```bash
git add Memory/
git commit -m "Session save: <summary>"
git push
```

## License

MIT License
