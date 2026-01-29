---
description: "Initialize Asha in current project - creates Memory/, .asha/, and databases"
argument-hint: "Optional: --minimal (skip Vector DB) or --full (accept all defaults)"
allowed-tools: ["Bash", "Read", "Write"]
---

# Initialize Asha in Project

Sets up Asha framework for the current project.

Arguments: $ARGUMENTS

## What This Creates

**Identity Layer** (cross-project, created once):
```
~/.asha/
├── communicationStyle.md   # Who Asha is
├── keeper.md               # Who The Keeper is (calibration log)
└── config.json             # Asha settings
```

**Project Layer** (per-project):
```
${CLAUDE_PROJECT_DIR}/
├── Memory/
│   ├── sessions/archive/
│   ├── reasoning_bank/
│   ├── vector_db/
│   ├── activeContext.md
│   ├── projectbrief.md
│   ├── workflowProtocols.md
│   └── techEnvironment.md
├── Work/markers/
├── .asha/
│   ├── .venv/
│   └── config.json
└── CLAUDE.md
```

## Protocol

### Step 1: Bootstrap Identity Layer (~/.asha/)

Create cross-project identity directory if it doesn't exist:

```bash
ASHA_HOME="$HOME/.asha"
if [[ ! -d "$ASHA_HOME" ]]; then
    mkdir -p "$ASHA_HOME"
    echo "Created ~/.asha/"
fi
```

Create identity files from templates if they don't exist:

```bash
# communicationStyle.md - Who Asha is
if [[ ! -f "$ASHA_HOME/communicationStyle.md" ]]; then
    cp "${CLAUDE_PLUGIN_ROOT}/templates/communicationStyle.md" "$ASHA_HOME/communicationStyle.md"
    echo "Created ~/.asha/communicationStyle.md"
fi

# keeper.md - Who The Keeper is (calibration log)
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

# config.json
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
```

### Step 2: Check Existing Project Installation

```bash
if [[ -f "${CLAUDE_PROJECT_DIR}/.asha/config.json" ]]; then
    echo "Asha already initialized in this project"
    echo "To reinitialize, delete .asha/ and run again"
    exit 0
fi
```

If already initialized, inform user and stop.

### Step 3: Create Project Directory Structure

```bash
mkdir -p "${CLAUDE_PROJECT_DIR}/Memory/sessions/archive"
mkdir -p "${CLAUDE_PROJECT_DIR}/Memory/reasoning_bank"
mkdir -p "${CLAUDE_PROJECT_DIR}/Memory/vector_db"
mkdir -p "${CLAUDE_PROJECT_DIR}/Work/markers"
mkdir -p "${CLAUDE_PROJECT_DIR}/.asha"
```

### Step 4: Copy Project Templates (if Memory files don't exist)

For each template in `${CLAUDE_PLUGIN_ROOT}/templates/`:
- If `Memory/<filename>` doesn't exist, copy it
- If it exists, skip (preserve user content)

**Note**: `communicationStyle.md` is NOT copied here — it lives in `~/.asha/` (cross-project).

Templates to copy:
- `activeContext.md`
- `projectbrief.md`
- `workflowProtocols.md`
- `techEnvironment.md`
- `scratchpad.md`

```bash
for template in activeContext.md projectbrief.md workflowProtocols.md techEnvironment.md scratchpad.md; do
    if [[ ! -f "${CLAUDE_PROJECT_DIR}/Memory/$template" ]]; then
        cp "${CLAUDE_PLUGIN_ROOT}/templates/$template" "${CLAUDE_PROJECT_DIR}/Memory/$template"
        echo "Created Memory/$template"
    else
        echo "Skipped Memory/$template (exists)"
    fi
done
```

### Step 5: Create CLAUDE.md (if doesn't exist)

```bash
if [[ ! -f "${CLAUDE_PROJECT_DIR}/CLAUDE.md" ]]; then
    cp "${CLAUDE_PLUGIN_ROOT}/templates/CLAUDE.md" "${CLAUDE_PROJECT_DIR}/CLAUDE.md"
    echo "Created CLAUDE.md"
else
    echo "Skipped CLAUDE.md (exists)"
fi
```

### Step 6: Create Python Virtual Environment

Unless `--minimal` is specified:

```bash
python3 -m venv "${CLAUDE_PROJECT_DIR}/.asha/.venv"
"${CLAUDE_PROJECT_DIR}/.asha/.venv/bin/pip" install -r "${CLAUDE_PLUGIN_ROOT}/tools/requirements.txt"
```

If venv creation fails, warn but continue (Vector DB will be unavailable).

### Step 7: Initialize Databases

```bash
# ReasoningBank
"${CLAUDE_PLUGIN_ROOT}/tools/run-python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/reasoning_bank.py" stats

# Vector DB check
"${CLAUDE_PLUGIN_ROOT}/tools/run-python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/memory_index.py" check
```

### Step 8: Create Project Config File

Write `.asha/config.json` to mark project as initialized:

```bash
cat > "${CLAUDE_PROJECT_DIR}/.asha/config.json" << EOF
{
  "version": "1.0.0",
  "initialized": "$(date -Iseconds)",
  "plugin": "asha@asha-marketplace"
}
EOF
```

### Step 9: Report Status

Display:
- Directory structure created
- Templates copied (list which ones)
- Python venv status
- Vector DB readiness
- Next steps for user

## Next Steps After Init

1. **Edit ~/.asha/communicationStyle.md** - Customize Asha's voice (first time only)
2. **Edit Memory/projectbrief.md** - Define project scope
3. **Edit Memory/activeContext.md** - Set current status
4. **Run /asha:index** - Index files for semantic search (optional)
5. **Add to .gitignore**:
   ```
   .asha/
   Memory/sessions/
   Memory/vector_db/
   Memory/reasoning_bank/
   ```

**Note**: `~/.asha/` is cross-project and not committed to any repo. The keeper profile (`~/.asha/keeper.md`) accumulates calibration signals automatically via `/save`.
