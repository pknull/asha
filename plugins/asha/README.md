# Asha

**Version**: 1.11.0
**Commands**: `/asha:init`, `/asha:save`, `/asha:note`, `/asha:status`, `/asha:index`, `/asha:cleanup`

Cognitive scaffold framework for session coordination, memory persistence, and session monitoring.

## Installation

```bash
/plugin marketplace add pknull/asha-marketplace
/plugin install asha@asha-marketplace
```

## Commands

### /asha:init

Initialize Asha in the current project.

```bash
/asha:init                # Full setup with Vector DB
/asha:init --minimal      # Skip Vector DB setup
```

Creates:
```
project/
├── Memory/
│   ├── sessions/archive/
│   ├── reasoning_bank/
│   ├── vector_db/
│   ├── activeContext.md
│   ├── projectbrief.md
│   ├── communicationStyle.md
│   ├── workflowProtocols.md
│   ├── techEnvironment.md
│   └── scratchpad.md
├── Work/markers/
├── .asha/
│   ├── .venv/
│   └── config.json
└── CLAUDE.md
```

### /asha:save

Save current session context to Memory Bank.

```bash
/asha:save                # Standard save
/asha:save --react        # Include pattern analysis
```

**Protocol**:
1. Extract session activity (agents, files, decisions)
2. Update Memory files using Four Questions framework
3. Archive session and refresh vector index
4. Commit and push (if remote exists)

### /asha:note

Add timestamped note to scratchpad.

```bash
/asha:note Discovered auth tokens expire after 1 hour
/asha:note API rate limit is 100 requests per minute
```

Notes are appended to `Memory/scratchpad.md` with UTC timestamps. Review and migrate important notes during `/asha:save`.

### /asha:status

Show current session status and captured activity.

```bash
/asha:status
```

**Example output:**
```
## Current Session Status

**Session ID**: silent-thunder
**Started**: 2026-01-12 14:30 UTC
**Duration**: 2h 14m
**File size**: 3.2K

### Captured Activity

| Section | Count |
|---------|-------|
| Significant Operations | 12 |
| Decisions & Clarifications | 3 |
| Errors & Anomalies | 0 |

### Recent Operations (last 5)
- Edit: plugins/asha/commands/note.md
- Edit: plugins/asha/README.md
- Bash: git commit
...
```

Use before `/asha:save` to preview what's been captured.

### /asha:index

Index project files for semantic search.

```bash
/asha:index               # Incremental (changed files only)
/asha:index --full        # Complete reindex
/asha:index --check       # Verify dependencies
```

**Requirements**:
- Ollama running locally (`ollama serve`)
- Embedding model (`ollama pull nomic-embed-text`)

### /asha:cleanup

Remove legacy nested-repo installation files.

```bash
/asha:cleanup             # Remove legacy files
/asha:cleanup --dry-run   # Show what would be removed
```

**Removes**:
- `asha/` directory (old nested repo)
- `.claude/hooks/hooks.json` (if contains asha refs)
- Command symlinks pointing to `asha/`
- `.opencode/` directory

## Memory Bank Architecture

| File | Purpose |
|------|---------|
| `activeContext.md` | Current session state, recent activities |
| `projectbrief.md` | Project scope, goals, stakeholders |
| `communicationStyle.md` | Voice, tone, persona |
| `workflowProtocols.md` | Validated patterns and techniques |
| `techEnvironment.md` | Stack, conventions, constraints |
| `scratchpad.md` | Quick notes captured via `/asha:note` |

## Session Continuity

1. Each Claude session starts fresh (context resets)
2. Memory Bank is the ONLY connection to previous work
3. Session watching captures operations automatically
4. `/asha:save` synthesizes operations into persistent context

## Agent Integration

### verify-app Agent

The `verify-app` agent (bundled with Asha plugin) uses `techEnvironment.md` for verification commands.

**Workflow:**
1. Agent reads `Memory/techEnvironment.md` for `## Verification` section
2. If commands defined → executes them in sequence
3. If missing/placeholders → bootstraps by detecting project type:
   - `package.json` → npm test, npm run lint
   - `Cargo.toml` → cargo check, cargo test
   - `pyproject.toml` → pytest, ruff
   - `go.mod` → go build, go test
4. Proposes commands, user approves, writes to techEnvironment.md

**Usage:** Triggered automatically after code changes or manually via Task tool.

## Git Integration

Sessions are preserved via git:
```bash
git add Memory/
git commit -m "Session save: <summary>"
git push  # If remote configured
```

## License

MIT License
