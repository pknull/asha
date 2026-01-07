---
description: "Initialize Asha in current project - creates Memory/, .asha/, and databases"
argument-hint: "Optional: --minimal (skip Vector DB) or --full (accept all defaults)"
allowed-tools: ["Bash", "Read", "Write"]
---

# Initialize Asha in Project

Sets up Asha framework for the current project.

Arguments: $ARGUMENTS

## What This Creates

```
${CLAUDE_PROJECT_DIR}/
├── Memory/
│   ├── sessions/archive/
│   ├── reasoning_bank/
│   ├── vector_db/
│   ├── activeContext.md
│   ├── projectbrief.md
│   ├── communicationStyle.md
│   ├── workflowProtocols.md
│   └── techEnvironment.md
├── Work/markers/
├── .asha/
│   ├── .venv/
│   └── config.json
└── CLAUDE.md
```

## Protocol

### Step 1: Check Existing Installation

```bash
if [[ -f "${CLAUDE_PROJECT_DIR}/.asha/config.json" ]]; then
    echo "Asha already initialized in this project"
    echo "To reinitialize, delete .asha/ and run again"
    exit 0
fi
```

If already initialized, inform user and stop.

### Step 2: Create Directory Structure

```bash
mkdir -p "${CLAUDE_PROJECT_DIR}/Memory/sessions/archive"
mkdir -p "${CLAUDE_PROJECT_DIR}/Memory/reasoning_bank"
mkdir -p "${CLAUDE_PROJECT_DIR}/Memory/vector_db"
mkdir -p "${CLAUDE_PROJECT_DIR}/Work/markers"
mkdir -p "${CLAUDE_PROJECT_DIR}/.asha"
```

### Step 3: Copy Templates (if Memory files don't exist)

For each template in `${CLAUDE_PLUGIN_ROOT}/templates/`:
- If `Memory/<filename>` doesn't exist, copy it
- If it exists, skip (preserve user content)

Templates to copy:
- `activeContext.md`
- `projectbrief.md`
- `communicationStyle.md`
- `workflowProtocols.md`
- `techEnvironment.md`

```bash
for template in activeContext.md projectbrief.md communicationStyle.md workflowProtocols.md techEnvironment.md; do
    if [[ ! -f "${CLAUDE_PROJECT_DIR}/Memory/$template" ]]; then
        cp "${CLAUDE_PLUGIN_ROOT}/templates/$template" "${CLAUDE_PROJECT_DIR}/Memory/$template"
        echo "Created Memory/$template"
    else
        echo "Skipped Memory/$template (exists)"
    fi
done
```

### Step 4: Create CLAUDE.md (if doesn't exist)

```bash
if [[ ! -f "${CLAUDE_PROJECT_DIR}/CLAUDE.md" ]]; then
    cp "${CLAUDE_PLUGIN_ROOT}/templates/CLAUDE.md" "${CLAUDE_PROJECT_DIR}/CLAUDE.md"
    echo "Created CLAUDE.md"
else
    echo "Skipped CLAUDE.md (exists)"
fi
```

### Step 5: Create Python Virtual Environment

Unless `--minimal` is specified:

```bash
python3 -m venv "${CLAUDE_PROJECT_DIR}/.asha/.venv"
"${CLAUDE_PROJECT_DIR}/.asha/.venv/bin/pip" install -r "${CLAUDE_PLUGIN_ROOT}/tools/requirements.txt"
```

If venv creation fails, warn but continue (Vector DB will be unavailable).

### Step 6: Initialize Databases

```bash
# ReasoningBank
"${CLAUDE_PLUGIN_ROOT}/tools/run-python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/reasoning_bank.py" stats

# Vector DB check
"${CLAUDE_PLUGIN_ROOT}/tools/run-python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/memory_index.py" check
```

### Step 7: Create Config File

Write `.asha/config.json` to mark project as initialized:

```bash
cat > "${CLAUDE_PROJECT_DIR}/.asha/config.json" << 'EOF'
{
  "version": "1.0.0",
  "initialized": "$(date -Iseconds)",
  "plugin": "asha@asha-marketplace"
}
EOF
```

### Step 8: Report Status

Display:
- Directory structure created
- Templates copied (list which ones)
- Python venv status
- Vector DB readiness
- Next steps for user

## Next Steps After Init

1. **Edit Memory/projectbrief.md** - Define project scope
2. **Edit Memory/activeContext.md** - Set current status
3. **Run /asha:index** - Index files for semantic search (optional)
4. **Add to .gitignore**:
   ```
   .asha/
   Memory/sessions/
   Memory/vector_db/
   Memory/reasoning_bank/
   ```
