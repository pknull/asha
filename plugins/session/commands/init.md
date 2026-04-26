---
name: session-init
description: "Initialize session management in current project - creates Memory/ and .asha/"
argument-hint: "Optional: --full (accept all defaults)"
allowed-tools: ["Bash", "Read", "Write"]
---

# Initialize Session Management

Sets up session management framework for the current project.

Arguments: $ARGUMENTS

## What This Creates

**Operational Layer** (cross-project, created once):

```
~/.asha/
├── operation.md            # Operational quality rules
├── learnings.md            # Cross-project patterns
└── config.json
```

**Project Layer** (per-project):

```
${CLAUDE_PROJECT_DIR}/
├── Memory/
│   ├── events/             # Session event log (JSONL)
│   ├── sessions/archive/   # Archived session summaries
│   ├── activeContext.md
│   ├── projectbrief.md
│   ├── workflowProtocols.md
│   └── techEnvironment.md
├── Work/markers/
├── .asha/
│   └── config.json
└── CLAUDE.md
```

## Protocol

### Step 1: Bootstrap Operational Layer (~/.asha/)

Create cross-project directory if it doesn't exist:

```bash
ASHA_HOME="$HOME/.asha"
if [[ ! -d "$ASHA_HOME" ]]; then
    mkdir -p "$ASHA_HOME"
    echo "Created ~/.asha/"
fi
```

Create operational files from templates if they don't exist:

```bash
# operation.md - Operational quality rules
if [[ ! -f "$ASHA_HOME/operation.md" ]]; then
    cat > "$ASHA_HOME/operation.md" << 'OP_EOF'
---
version: "1.0"
lastUpdated: "$(date -I)"
lifecycle: "active"
stakeholder: "all"
changeTrigger: "Operational quality adjustment"
dependencies: []
note: "Loaded on ALL sessions. No persona content."
---

# Operation

How Claude operates in this user's projects. Loaded always, regardless of persona.

## Implementation Thoroughness

- Choose the approach that correctly and completely solves the problem.
- Communication brevity and implementation thoroughness are independent concerns.
- If adjacent code is broken or contributes to the problem, fix it.
- Add error handling at real boundaries where failures can occur.
- Do the work a careful senior developer would do, including edge cases.
- When exploring a codebase, be thorough. Do not sacrifice completeness for speed.

## Operational Constraints

- **Data Preservation**: NEVER lose user data. Destructive operations require explicit confirmation.
- **Tool Reuse**: Check for existing tools/scripts before creating new ones.
- **Memory First**: Read project Memory before acting on unfamiliar context.

## Output

- Concise responses for simple tasks
- Expand when complexity requires
- Minimal preamble/postamble
OP_EOF
    echo "Created ~/.asha/operation.md"
fi

# learnings.md - Cross-project insights
if [[ ! -f "$ASHA_HOME/learnings.md" ]]; then
    cat > "$ASHA_HOME/learnings.md" << 'LEARNINGS_EOF'
# Learnings

Cross-project patterns with confidence tracking. Consulted at session start.

---

## Tool Usage

- (populated via /save reflections)

## Patterns

- (populated via /save reflections)
LEARNINGS_EOF
    echo "Created ~/.asha/learnings.md"
fi

# config.json
if [[ ! -f "$ASHA_HOME/config.json" ]]; then
    cat > "$ASHA_HOME/config.json" << 'CONFIG_EOF'
{
  "version": "2.0",
  "description": "Session management configuration",
  "capture_calibration": true,
  "learnings_file": "learnings.md",
  "operation_file": "operation.md"
}
CONFIG_EOF
    echo "Created ~/.asha/config.json"
fi
```

### Step 2: Check Existing Project Installation

```bash
if [[ -f "${CLAUDE_PROJECT_DIR}/.asha/config.json" ]]; then
    echo "Session management already initialized in this project"
    echo "To reinitialize, delete .asha/ and run again"
    exit 0
fi
```

If already initialized, inform user and stop.

### Step 3: Create Project Directory Structure

```bash
mkdir -p "${CLAUDE_PROJECT_DIR}/Memory/events"
mkdir -p "${CLAUDE_PROJECT_DIR}/Memory/sessions/archive"
mkdir -p "${CLAUDE_PROJECT_DIR}/Work/markers"
mkdir -p "${CLAUDE_PROJECT_DIR}/.asha"
```

### Step 4: Copy Project Templates (if Memory files don't exist)

```bash
for template in activeContext.md projectbrief.md workflowProtocols.md techEnvironment.md scratchpad.md; do
    if [[ ! -f "${CLAUDE_PROJECT_DIR}/Memory/$template" ]]; then
        cp "/home/pknull/life/asha/plugins/session/templates/$template" "${CLAUDE_PROJECT_DIR}/Memory/$template"
        echo "Created Memory/$template"
    else
        echo "Skipped Memory/$template (exists)"
    fi
done
```

### Step 5: Create CLAUDE.md (if doesn't exist)

```bash
if [[ ! -f "${CLAUDE_PROJECT_DIR}/CLAUDE.md" ]]; then
    cp "/home/pknull/life/asha/plugins/session/templates/CLAUDE.md" "${CLAUDE_PROJECT_DIR}/CLAUDE.md"
    echo "Created CLAUDE.md"
else
    echo "Skipped CLAUDE.md (exists)"
fi
```

### Step 6: Create Project Config File

```bash
cat > "${CLAUDE_PROJECT_DIR}/.asha/config.json" << EOF
{
  "version": "2.0.0",
  "initialized": "$(date -Iseconds)",
  "plugin": "session@asha-marketplace"
}
EOF
```

### Step 7: Report Status

Display:

- Directory structure created
- Templates copied (list which ones)
- Next steps for user
- Mention: "To add the Asha persona, install the `asha` plugin and run `/asha:init`"
