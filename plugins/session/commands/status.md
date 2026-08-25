---
name: session-status
description: "Show Memory v2 publication, recovery, and learning state"
argument-hint: ""
allowed-tools: ["Bash", "Read"]
---

# Session Status

```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"
TOOLS="$ASHA_ROOT/plugins/session/tools"

python3 "$TOOLS/memory_v2.py" status --project-dir "$PROJECT_DIR"
python3 "$TOOLS/recovery_state.py" latest --project-dir "$PROJECT_DIR"
python3 "$TOOLS/learnings_manager.py" list --state candidate
python3 "$TOOLS/learnings_manager.py" list --state active
python3 "$TOOLS/learnings_manager.py" list --state retired
```

Report published file byte sizes, the stable project id, silence/RP markers,
the newest snapshot as **unpublished recovery**, and learning counts by state.
Never present recovery or candidates as authoritative memory.
