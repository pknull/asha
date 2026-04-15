---
description: "List running agents in tmux orchestrator session"
allowed-tools: ["Bash"]
---

# List Running Agents

Shows all agents currently running in the `orchestrator` tmux session.

## Implementation

```bash
if /usr/bin/tmux has-session -t orchestrator 2>/dev/null; then
    echo "📡 Running Agents (orchestrator session):"
    echo ""
    /usr/bin/tmux list-windows -t orchestrator -F '  #I: #W' | grep -v "^  0: main$" || echo "  (no agents running)"
    echo ""
    echo "Commands:"
    echo "  Spawn:   Tools/scripts/agent-spawn.sh <name> <type> [prompt] [dir]"
    echo "  Message: Tools/scripts/agent-msg.sh orchestrator:<index> 'message'"
    echo "  Stop:    Tools/scripts/agent-stop.sh <name|all> [--force]"
else
    echo "No orchestrator session running."
    echo ""
    echo "Start with: Tools/scripts/agent-spawn.sh <name> <type> [prompt] [dir]"
fi
```

Execute the bash command above.
