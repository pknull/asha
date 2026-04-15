---
description: "Add timestamped note to Memory scratchpad"
argument-hint: "<note text>"
allowed-tools: ["Bash", "Write", "Read"]
---

# Add Note to Scratchpad

Quick capture of thoughts, decisions, discoveries, or blockers during a session.

Note content: $ARGUMENTS

## Protocol

### Step 1: Validate Input

If no arguments provided, inform user:
```
Usage: /asha:note <your note text>
Example: /asha:note Discovered auth tokens expire after 1 hour
```

### Step 2: Ensure Scratchpad Exists

```bash
SCRATCHPAD="${CLAUDE_PROJECT_DIR}/Memory/scratchpad.md"

if [[ ! -f "$SCRATCHPAD" ]]; then
    cat > "$SCRATCHPAD" << 'EOF'
---
version: "1.0"
lastUpdated: "$(date -u '+%Y-%m-%d %H:%M UTC')"
purpose: "Quick capture of thoughts, decisions, and discoveries"
---

# Scratchpad

Timestamped notes captured during sessions. Review periodically and migrate important items to appropriate Memory files.

---

EOF
fi
```

### Step 3: Append Note

Append the note with timestamp:

```bash
echo "" >> "$SCRATCHPAD"
echo "**$(date -u '+%Y-%m-%d %H:%M UTC')**: $ARGUMENTS" >> "$SCRATCHPAD"
```

### Step 4: Confirm

Display confirmation:
```
Note added to Memory/scratchpad.md
```

## Scratchpad Management Tips

- Review scratchpad during `/asha:save` to migrate important items
- Prune completed/obsolete notes periodically
- Move recurring patterns to `workflowProtocols.md`
- Move project decisions to `activeContext.md`
