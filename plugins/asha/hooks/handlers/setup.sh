#!/bin/bash
# setup.sh - Setup hook for Asha
# Triggered by: claude --init, claude --maintenance
# Auto-initializes Asha in the project if not already set up

set -e

# Source common utilities if available
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$SCRIPT_DIR/common.sh" ]] && source "$SCRIPT_DIR/common.sh"

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"

# Exit silently if plugin root not available
[[ -z "$PLUGIN_ROOT" ]] && exit 0

ASHA_HOME="$HOME/.asha"

# --- Step 1: Bootstrap Identity Layer (~/.asha/) ---

if [[ ! -d "$ASHA_HOME" ]]; then
    mkdir -p "$ASHA_HOME"
    echo "Created ~/.asha/"
fi

# communicationStyle.md
if [[ ! -f "$ASHA_HOME/communicationStyle.md" ]] && [[ -f "$PLUGIN_ROOT/templates/communicationStyle.md" ]]; then
    cp "$PLUGIN_ROOT/templates/communicationStyle.md" "$ASHA_HOME/communicationStyle.md"
    echo "Created ~/.asha/communicationStyle.md"
fi

# keeper.md
if [[ ! -f "$ASHA_HOME/keeper.md" ]]; then
    cat > "$ASHA_HOME/keeper.md" << 'KEEPER_EOF'
# Keeper Profile

Cross-project user profile. Additive only — signals accumulate with timestamps.

---

## Identity

- **Expertise**: (discovered organically)
- **Context**: (populated via /save)

---

## Voice Calibration

Accumulated signals about communication preferences.

| Date | Signal | Context | Source Project |
|------|--------|---------|----------------|

---

## Working Style

- (populated organically via /save)

---

## Notes

Persistent observations across projects.

---

## Calibration Log

Raw signals captured via `/save`. Synthesis updates sections above.

```
```
KEEPER_EOF
    echo "Created ~/.asha/keeper.md"
fi

# ~/.asha/config.json
if [[ ! -f "$ASHA_HOME/config.json" ]]; then
    cat > "$ASHA_HOME/config.json" << 'CONFIG_EOF'
{
  "version": "1.0",
  "description": "Asha cross-project configuration",
  "capture_calibration": true,
  "keeper_profile": "keeper.md",
  "identity_file": "communicationStyle.md"
}
CONFIG_EOF
    echo "Created ~/.asha/config.json"
fi

# --- Step 2: Check Existing Project Installation ---

if [[ -f "$PROJECT_DIR/.asha/config.json" ]]; then
    echo "Asha already initialized in this project"
    exit 0
fi

# --- Step 3: Create Project Directory Structure ---

mkdir -p "$PROJECT_DIR/Memory/sessions/archive"
mkdir -p "$PROJECT_DIR/Memory/reasoning_bank"
mkdir -p "$PROJECT_DIR/Memory/vector_db"
mkdir -p "$PROJECT_DIR/Work/markers"
mkdir -p "$PROJECT_DIR/.asha"

echo "Created directory structure"

# --- Step 4: Copy Project Templates ---

for template in activeContext.md projectbrief.md workflowProtocols.md techEnvironment.md scratchpad.md; do
    if [[ ! -f "$PROJECT_DIR/Memory/$template" ]] && [[ -f "$PLUGIN_ROOT/templates/$template" ]]; then
        cp "$PLUGIN_ROOT/templates/$template" "$PROJECT_DIR/Memory/$template"
        echo "Created Memory/$template"
    fi
done

# --- Step 5: Create CLAUDE.md ---

if [[ ! -f "$PROJECT_DIR/CLAUDE.md" ]] && [[ -f "$PLUGIN_ROOT/templates/CLAUDE.md" ]]; then
    cp "$PLUGIN_ROOT/templates/CLAUDE.md" "$PROJECT_DIR/CLAUDE.md"
    echo "Created CLAUDE.md"
fi

# --- Step 6: Create Python Virtual Environment ---

if command -v python3 &> /dev/null; then
    if [[ ! -d "$PROJECT_DIR/.asha/.venv" ]]; then
        python3 -m venv "$PROJECT_DIR/.asha/.venv" 2>/dev/null || {
            echo "Warning: Could not create venv (Vector DB unavailable)"
        }
    fi

    if [[ -d "$PROJECT_DIR/.asha/.venv" ]] && [[ -f "$PLUGIN_ROOT/tools/requirements.txt" ]]; then
        "$PROJECT_DIR/.asha/.venv/bin/pip" install -q -r "$PLUGIN_ROOT/tools/requirements.txt" 2>/dev/null || {
            echo "Warning: Could not install dependencies"
        }
    fi
fi

# --- Step 7: Initialize Databases ---

if [[ -f "$PLUGIN_ROOT/tools/reasoning_bank.py" ]]; then
    "$PLUGIN_ROOT/tools/run-python.sh" "$PLUGIN_ROOT/tools/reasoning_bank.py" stats 2>/dev/null || true
fi

# --- Step 8: Create Project Config File ---

cat > "$PROJECT_DIR/.asha/config.json" << EOF
{
  "version": "1.0.0",
  "initialized": "$(date -Iseconds)",
  "plugin": "asha@asha-marketplace",
  "setup_hook": true
}
EOF

echo "Asha initialized via setup hook"
exit 0
