---
name: session-silence
description: "Toggle the master Memory v2 persistence override"
argument-hint: "[on|off|status]"
allowed-tools: ["Bash"]
---

# Silence Mode

`Work/markers/silence` is the master persistence override. Whilst present,
prompt/tool/end hooks do not update recovery snapshots and `/session:save`
does not publish Memory, mutate learnings, commit, or push.

```bash
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
case "${ARGUMENTS:-toggle}" in
  on|enable) mkdir -p "$PROJECT_DIR/Work/markers"; touch "$PROJECT_DIR/Work/markers/silence" ;;
  off|disable) rm -f "$PROJECT_DIR/Work/markers/silence" ;;
  status) ;;
  toggle)
    if [[ -f "$PROJECT_DIR/Work/markers/silence" ]]; then
      rm -f "$PROJECT_DIR/Work/markers/silence"
    else
      mkdir -p "$PROJECT_DIR/Work/markers"; touch "$PROJECT_DIR/Work/markers/silence"
    fi ;;
  *) echo "usage: /session:silence [on|off|status]"; exit 2 ;;
esac
[[ -f "$PROJECT_DIR/Work/markers/silence" ]] && echo "Memory persistence: OFF" || echo "Memory persistence: ON"
```
