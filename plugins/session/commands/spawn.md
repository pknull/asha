---
description: "Spawn AI agent in tmux orchestrator session"
argument-hint: "<name> <type> [prompt] [working-dir]"
allowed-tools: ["Bash"]
---

# Spawn Agent

Spawns a new AI agent in the tmux orchestrator session.

Arguments: $ARGUMENTS

## Agent Types

- `claude` - Claude Code (permission bypass mode)
- `codex` - OpenAI Codex (approval bypass mode)
- `gemini` - Google Gemini CLI (auto-confirm)
- `aider` - Aider (yes mode)
- Or any custom command

## Examples

```bash
# Basic spawn
Tools/scripts/agent-spawn.sh review-claude claude

# With initial prompt
Tools/scripts/agent-spawn.sh backend-review claude "Review the auth module for security issues"

# With prompt and working directory
Tools/scripts/agent-spawn.sh frontend-fix gemini "Fix the React rendering bug" /path/to/frontend
```

## Implementation

Parse $ARGUMENTS and execute:

```bash
Tools/scripts/agent-spawn.sh $ARGUMENTS
```

If no arguments provided, show usage:

```bash
echo "Usage: /spawn <name> <type> [prompt] [working-dir]"
echo ""
echo "Agent types: claude, codex, gemini, aider, or custom command"
echo ""
echo "Examples:"
echo "  /spawn review-claude claude"
echo "  /spawn backend-api codex 'Implement auth endpoints'"
echo "  /spawn frontend gemini 'Fix React bug' /path/to/project"
```
