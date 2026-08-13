---
name: session-restore
description: "Re-enable Memory v2 persistence after silence mode"
allowed-tools: ["Bash"]
---

# Restore Memory Persistence

Compatibility alias for `/session:silence off`:

```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
rm -f "$PROJECT_DIR/Work/markers/silence"
echo "Memory persistence: ON"
```

No silenced prompt/tool history is reconstructed. Semantic publication remains
explicit through `/session:save` on every harness.
