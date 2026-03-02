---
description: "Initialize Asha in current project - creates Memory/ and .asha/"
argument-hint: "Optional: --full (accept all defaults)"
allowed-tools: ["Bash", "Read", "Write"]
---

# Initialize Asha in Project

Sets up Asha framework for the current project.

Arguments: $ARGUMENTS

## What This Creates

**Identity Layer** (cross-project, created once):

```
~/.asha/
├── soul.md                 # Who you are (identity, values, nature)
├── voice.md                # How you express (tone, patterns, constraints)
├── keeper.md               # Who The Keeper is (calibration log)
├── learnings.md            # Cross-project insights from experience
└── config.json             # Asha settings
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
# soul.md - Who you are (identity, values, nature)
if [[ ! -f "$ASHA_HOME/soul.md" ]]; then
    cp "${CLAUDE_PLUGIN_ROOT}/templates/soul.md" "$ASHA_HOME/soul.md"
    echo "Created ~/.asha/soul.md"
fi

# voice.md - How you express (tone, patterns, constraints)
if [[ ! -f "$ASHA_HOME/voice.md" ]]; then
    cp "${CLAUDE_PLUGIN_ROOT}/templates/voice.md" "$ASHA_HOME/voice.md"
    echo "Created ~/.asha/voice.md"
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

# learnings.md - Cross-project insights
if [[ ! -f "$ASHA_HOME/learnings.md" ]]; then
    cat > "$ASHA_HOME/learnings.md" << 'LEARNINGS_EOF'
# Learnings

Cross-project insights from experience. Consulted at session start, appended during /save.

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
  "version": "1.2",
  "description": "Asha cross-project configuration",
  "capture_calibration": true,
  "keeper_profile": "keeper.md",
  "soul_file": "soul.md",
  "voice_file": "voice.md",
  "learnings_file": "learnings.md"
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
mkdir -p "${CLAUDE_PROJECT_DIR}/Memory/events"
mkdir -p "${CLAUDE_PROJECT_DIR}/Memory/sessions/archive"
mkdir -p "${CLAUDE_PROJECT_DIR}/Work/markers"
mkdir -p "${CLAUDE_PROJECT_DIR}/.asha"
```

### Step 4: Copy Project Templates (if Memory files don't exist)

For each template in `${CLAUDE_PLUGIN_ROOT}/templates/`:

- If `Memory/<filename>` doesn't exist, copy it
- If it exists, skip (preserve user content)

**Note**: `soul.md` and `voice.md` are NOT copied here — they live in `~/.asha/` (cross-project).

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

### Step 6: Create Project Config File

Write `.asha/config.json` to mark project as initialized:

```bash
cat > "${CLAUDE_PROJECT_DIR}/.asha/config.json" << EOF
{
  "version": "1.1.0",
  "initialized": "$(date -Iseconds)",
  "plugin": "asha@asha-marketplace"
}
EOF
```

### Step 7: Report Status

Display:

- Directory structure created
- Templates copied (list which ones)
- Next steps for user

## Next Steps After Init

1. **Edit ~/.asha/soul.md** - Define identity and values (first time only)
2. **Edit ~/.asha/voice.md** - Configure tone and expression patterns
3. **Edit Memory/projectbrief.md** - Define project scope
4. **Edit Memory/activeContext.md** - Set current status
5. **Add to .gitignore**:

   ```
   .asha/
   Memory/sessions/
   Work/
   ```

**Note**: `~/.asha/` is cross-project and not committed to any repo. The keeper profile (`~/.asha/keeper.md`) accumulates calibration signals automatically via `/save`.
