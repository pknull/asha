# Asha Hooks

Platform-agnostic session tracking hooks for Claude Code and OpenCode.

## Architecture

```
asha/
├── bridges/
│   ├── claude.json      # Claude Code hook configuration template
│   └── opencode.ts      # OpenCode plugin bridge
├── hooks/
│   ├── common.sh        # Shared utilities (project detection)
│   ├── post-tool-use    # Logs file changes, agent deployments, errors
│   ├── session-end      # Archives session on clean exit
│   ├── user-prompt-submit # Prompt logging + LanguageTool correction
│   ├── violation-checker  # Rule enforcement (called by post-tool-use)
│   └── README.md        # This file
├── rules/
│   ├── memory-protection.sh  # Protects immutable Memory files
│   ├── destructive-git.sh    # Flags dangerous git operations
│   ├── vault-structure.sh    # Enforces Vault directory structure
│   └── file-header.sh        # Checks for documentation headers
└── install.sh           # Sets up hooks for both platforms
```

## Installation

Run from project root:

```bash
./asha/install.sh
```

This creates:
- `.claude/hooks/hooks.json` - Claude Code configuration
- `.opencode/plugin/asha-hooks.ts` - OpenCode bridge plugin

## How It Works

### Claude Code
Claude Code reads `.claude/hooks/hooks.json` and executes the shell scripts directly.

### OpenCode
OpenCode loads `.opencode/plugin/asha-hooks.ts` which:
1. Reads the same `hooks.json` configuration
2. Translates OpenCode events to Claude Code JSON format
3. Spawns the same `asha/hooks/` shell scripts
4. Handles output injection for UserPromptSubmit

Both platforms execute the same scripts with the same JSON format.

## Environment Variables

| Variable | Set By | Purpose |
|----------|--------|---------|
| `CLAUDE_PROJECT_DIR` | Claude Code (native), OpenCode bridge | Project root path |
| `OPENCODE_PROJECT_DIR` | OpenCode bridge only | Project root path |

Scripts check both variables via `common.sh`.

## Hook Events

| Hook | Trigger | What It Does |
|------|---------|--------------|
| PostToolUse | After any tool execution | Logs operations, runs violation checker |
| UserPromptSubmit | When user sends a message | Logs prompts, applies LanguageTool corrections |
| SessionEnd | Session exit/idle | Archives session file |

## JSON Format

All hooks receive JSON on stdin:

```json
{
  "session_id": "...",
  "cwd": "/path/to/project",
  "hook_event_name": "PostToolUse",
  "tool_name": "Edit",
  "tool_input": { "file_path": "..." },
  "tool_response": { "output": "..." }
}
```

## Violation Checker

The violation checker runs after Write/Edit/Bash operations and logs rule violations to the session file. Rules are defined in `asha/rules/*.sh`.

Each rule script exports a `check_violation()` function:

```bash
check_violation() {
    local tool_name="$1"
    local input="$2"      # file_path or command
    local project_dir="$3"
    
    # Return 0 and echo message if violation detected
    # Return 1 if no violation
}
```

## Adding New Hooks

1. Create script in `asha/hooks/`
2. Add to `asha/bridges/claude.json`
3. Update `asha/bridges/opencode.ts` if needed
4. Run `./asha/install.sh` to update configurations

## Adding New Rules

1. Create `asha/rules/your-rule.sh`
2. Add header comment with `# Severity: HIGH|MEDIUM|LOW`
3. Export `check_violation()` function
4. Rules are automatically picked up by violation-checker
