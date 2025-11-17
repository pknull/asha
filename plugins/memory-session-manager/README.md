# Memory+Session Manager Plugin

**Maintain persistent Memory across multi-session AI projects with automated session capture and synthesis workflows.**

## What This Plugin Does

This plugin provides tools for **maintaining** Memory files and session data in multi-session AI projects. It does NOT change how Claude reads Memory (that stays in your framework) - it provides the infrastructure for CREATING and UPDATING Memory systematically.

**Key Features**:
- 📝 Automatic session watching (captures operations, decisions, errors as they happen)
- 💾 `/save` command with guided synthesis workflow
- 🔇 `/silence` command for toggling Memory logging (experimental/debugging sessions)
- 🗂️ Memory file structure specification (frontmatter schema, required files)
- 📚 Documentation for cross-platform adaptation (Claude.ai, ChatGPT, Gemini)

## Installation

### Claude Code

1. Add the asha-marketplace to your plugin marketplaces:
```bash
/plugin marketplace add ~/Code/asha-marketplace
```

2. Install the plugin:
```bash
/plugin install memory-session-manager@asha-marketplace
```

3. **Restart Claude Code** to activate hooks

4. The plugin provides:
   - `/save` command for session synthesis
   - `/silence` command for toggling Memory logging
   - Automatic session watching hooks
   - Memory maintenance skill (auto-invoked when updating Memory files)

#### Quick Start (First Session)

After installation and restart, start a conversation. The session file (`Memory/sessions/current-session.md`) should auto-create on first significant operation.

**If session file doesn't auto-create**, manually initialize:
```bash
cat > Memory/sessions/current-session.md <<'EOF'
---
sessionStart: $(date -u '+%Y-%m-%d %H:%M UTC')
sessionID: manual-$(head /dev/urandom | tr -dc a-f0-9 | head -c 8)
---

## Significant Operations

## Decisions & Clarifications

## Discoveries & Patterns

## Errors & Anomalies

## Candidates for Next Steps
EOF
```

Then manually log operations during session, or let hooks take over after restart. See `docs/TROUBLESHOOTING.md` for diagnosis.

### Other Platforms

See `docs/PLATFORM-ADAPTERS.md` for manual implementation guides for:
- Claude.ai (Projects feature)
- ChatGPT (Custom GPT Knowledge)
- Gemini (manual workflow)

## Components

### Commands
- **`/save`**: Interactive session synthesis workflow
  - Four Questions Protocol for Memory updates
  - Archive management
  - Git integration (Claude Code only)
- **`/silence [on|off|toggle|status]`**: Toggle Memory logging
  - Controls `Memory/markers/silence` marker
  - Disables session capture when enabled
  - Auto-removed at session end

### Skills
- **`memory-maintenance`**: Autonomous Memory file structure guidance
  - Frontmatter schema
  - Required vs optional files
  - Update triggers

### Hooks
- **PostToolUse**: Captures agent deployments and file modifications
- **UserPromptSubmit**: Captures user decisions
- **SessionEnd**: Reminds to `/save` if session data exists

### Scripts
- **`save-session.sh`**: Automation for Claude Code platform
  - Archive session watching file
  - Update systemMonitoring.md
  - Reset for next session

## How It Works

### Session Watching (Automatic)

The plugin automatically captures significant operations to `Memory/sessions/current-session.md`:

- Agent deployments (Task tool usage)
- File modifications (Edit/Write to Vault/Memory/Tools)
- User decisions (AskUserQuestion responses)
- ACE cognitive cycles
- Panel sessions
- Errors and failures

**Marker Overrides**:
- `Memory/markers/silence` disables all Memory logging
- `Memory/markers/rp-active` disables session watching (RP sessions)

### Session Save (Manual with `/save`)

At end of session, run `/save` to:

1. Review captured operations in watching file
2. Answer Four Questions Protocol:
   - What did we accomplish?
   - What did we learn?
   - What's next?
   - What context is critical?
3. Update `Memory/activeContext.md` with session summary
4. Archive watching file to `Memory/sessions/archive/`
5. Commit to git (Claude Code only)

### Memory File Structure

The plugin documents but doesn't enforce this structure:

**Required Files**:
- `Memory/activeContext.md` - Current project state, recent activities, next steps
- `Memory/projectbrief.md` - Scope, objectives, constraints
- `Memory/communicationStyle.md` - Voice, persona, tone

**Optional Files**:
- `Memory/workflowProtocols.md` - Project-specific patterns
- `Memory/techEnvironment.md` - Stack conventions
- `Memory/productContext.md` - Product details
- Custom files as needed

**Frontmatter Schema** (all Memory files):
```yaml
---
version: "X.Y"
lastUpdated: "YYYY-MM-DD HH:MM UTC"
lifecycle: "initiation|planning|execution|maintenance"
stakeholder: "technical|business|regulatory|all"
changeTrigger: "≥25% code impact|pattern discovery|user request|context ambiguity"
validatedBy: "human|ai|system"
dependencies: ["file1.md", "file2.md"]
---
```

## Documentation

- `docs/MEMORY-STRUCTURE.md` - Detailed file specifications
- `docs/SESSION-CAPTURE.md` - Session watching protocol
- `docs/SESSION-SAVE.md` - Synthesis workflow guide
- `docs/PLATFORM-ADAPTERS.md` - Cross-platform implementation
- `docs/TROUBLESHOOTING.md` - Common issues and solutions

## Philosophy

**Separation of Concerns**:
- Your framework (AGENTS.md) tells Claude to READ Memory
- This plugin tells Claude HOW TO MAINTAIN Memory

**Cross-Platform**:
- Core protocol works anywhere (manual synthesis)
- Enhanced automation on capable platforms (Claude Code)
- Graceful degradation strategy

**Session Continuity**:
- Every session begins fresh
- Memory is the ONLY connection to previous work
- Session watching prevents recall failures
- Synthesis transforms operations into persistent context

## Requirements

**Minimum** (all platforms):
- Persistent text storage
- Ability to read/update files

**Enhanced** (Claude Code):
- File system access (Memory/sessions/ directory)
- Bash execution (save-session.sh)
- Hooks (automatic capture)
- Git integration (commits)

## License

MIT

## Version

1.0.0
