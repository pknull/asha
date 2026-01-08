# Asha

**Version**: 1.0.1
**Commands**: `/asha:init`, `/asha:save`, `/asha:index`, `/asha:cleanup`

Cognitive scaffold framework for session coordination and memory persistence.

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
│   └── techEnvironment.md
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

## Session Continuity

1. Each Claude session starts fresh (context resets)
2. Memory Bank is the ONLY connection to previous work
3. Session watching captures operations automatically
4. `/asha:save` synthesizes operations into persistent context

## Git Integration

Sessions are preserved via git:
```bash
git add Memory/
git commit -m "Session save: <summary>"
git push  # If remote configured
```

## License

MIT License
