---
name: session-restore
description: "Re-enable Memory logging after silence mode"
allowed-tools: ["Bash", "Read"]
---

# Restore Memory Logging

Re-enables Memory logging after silence mode.

This is a compatibility alias for `/session:silence off`. It removes only the
durable silence marker; it does not reconstruct events from the silenced window.

```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
rm -f "$PROJECT_DIR/Work/markers/silence"
echo "Memory synthesis enabled"
```

Manual transcript synthesis is then available on Claude, Codex, Copilot, and
OpenCode. Claude also regains clean-exit automatic save. Other harnesses require
an explicit `/session:save` because Asha has no SessionEnd lifecycle hook there.

ARGUMENTS: {command_args}
