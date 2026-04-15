---
description: "Stop agents in tmux orchestrator session"
argument-hint: "Optional: agent name, 'all', or '--force'"
allowed-tools: ["Bash"]
---

# Stop Agents

Gracefully stops agents running in the tmux orchestrator session.

Arguments: $ARGUMENTS

## Usage

- `/stop-agents` - Stop all agents gracefully
- `/stop-agents <name>` - Stop specific agent
- `/stop-agents all --force` - Force kill all agents
- `/stop-agents <name> --force` - Force kill specific agent

## Implementation

Parse $ARGUMENTS:

**No arguments or "all"**:
```bash
Tools/scripts/agent-stop.sh all
```

**"all --force"**:
```bash
Tools/scripts/agent-stop.sh all --force
```

**Specific agent name**:
```bash
Tools/scripts/agent-stop.sh <name>
```

**Specific agent with --force**:
```bash
Tools/scripts/agent-stop.sh <name> --force
```

If orchestrator session doesn't exist:
```bash
echo "No orchestrator session running."
```
