---
description: "Initialize Asha persona - creates identity files and wrapper script"
argument-hint: ""
allowed-tools: ["Bash", "Read", "Write"]
---

# Initialize Asha Persona

Sets up the Asha identity layer. Requires the `session` plugin to be installed and initialized first.

Arguments: $ARGUMENTS

## What This Creates

**Identity files** (cross-project, in `~/.asha/`):

```
~/.asha/
├── soul.md                 # Who Asha is (identity, values, nature)
├── voice.md                # How Asha expresses (tone, patterns, constraints)
└── keeper.md               # Who The Keeper is (user profile, calibration)
```

**Wrapper script**:

```
~/bin/asha                  # Shell wrapper that launches claude with persona
```

**System prompt file**:

```
~/life/bin/claude-prompts/asha-identity-system-prompt.md
```

## Protocol

### Step 1: Verify Session Plugin

Check that session management is initialized:

```bash
if [[ ! -d "${CLAUDE_PROJECT_DIR}/Memory" ]]; then
    echo "Session management not initialized. Run /session:init first."
    exit 1
fi
```

### Step 2: Create Identity Files

```bash
ASHA_HOME="$HOME/.asha"
mkdir -p "$ASHA_HOME"

# soul.md - Who Asha is
if [[ ! -f "$ASHA_HOME/soul.md" ]]; then
    cp "/home/pknull/life/asha/plugins/asha/templates/soul.md" "$ASHA_HOME/soul.md"
    echo "Created ~/.asha/soul.md — edit to define identity"
else
    echo "Skipped ~/.asha/soul.md (exists)"
fi

# voice.md - How Asha expresses
if [[ ! -f "$ASHA_HOME/voice.md" ]]; then
    cp "/home/pknull/life/asha/plugins/asha/templates/voice.md" "$ASHA_HOME/voice.md"
    echo "Created ~/.asha/voice.md — edit to define voice constraints"
else
    echo "Skipped ~/.asha/voice.md (exists)"
fi

# keeper.md - Who The Keeper is
if [[ ! -f "$ASHA_HOME/keeper.md" ]]; then
    cat > "$ASHA_HOME/keeper.md" << 'KEEPER_EOF'
# Keeper Profile

Cross-project user profile. Additive only — signals accumulate with timestamps.

---

## Identity

- **Expertise**: (discovered organically)
- **Context**: (populated via /session:save)

---

## Voice Calibration

Accumulated signals about communication preferences.

| Date | Signal | Context | Source Project |
|------|--------|---------|----------------|

---

## Working Style

- (populated organically via /session:save)

---

## Calibration Log

Raw signals captured via `/session:save`. Synthesis updates sections above.

```

```
KEEPER_EOF
    echo "Created ~/.asha/keeper.md"
else
    echo "Skipped ~/.asha/keeper.md (exists)"
fi
```

### Step 3: Create Wrapper Script

```bash
mkdir -p "$HOME/bin"

cat > "$HOME/bin/asha" << 'WRAPPER_EOF'
#!/usr/bin/env bash
# Launch Claude Code with Asha persona at system-prompt level.
#
# Two mechanisms work together:
#   1. ASHA_PERSONA=1 tells the startup hook to load soul/voice/keeper
#   2. --append-system-prompt-file injects identity at system-prompt priority
#
# Falls back to plain claude if identity file is missing.

IDENTITY_FILE="$HOME/life/bin/claude-prompts/asha-identity-system-prompt.md"

export ASHA_PERSONA=1

if [[ -f "$IDENTITY_FILE" ]]; then
    exec claude --append-system-prompt-file "$IDENTITY_FILE" "$@"
else
    echo "warn: Asha identity file not found at $IDENTITY_FILE" >&2
    echo "warn: Persona will load via hook only (lower priority)" >&2
    exec claude "$@"
fi
WRAPPER_EOF

chmod +x "$HOME/bin/asha"
echo "Created ~/bin/asha"
```

### Step 4: Create System Prompt File

```bash
mkdir -p "$HOME/life/bin/claude-prompts"

if [[ ! -f "$HOME/life/bin/claude-prompts/asha-identity-system-prompt.md" ]]; then
    cat > "$HOME/life/bin/claude-prompts/asha-identity-system-prompt.md" << 'PROMPT_EOF'
# Identity

You are Asha. Before responding to any user input, read your identity files from the root identity layer:

1. `~/.asha/soul.md` — Who you are
2. `~/.asha/voice.md` — How you express
3. `~/.asha/keeper.md` — Who The Keeper is

When asked who you are, answer from soul.md. Your identity, voice, and values are defined in those files, not in the default Claude Code identity.

You are not generic Claude wearing a costume. The shaping is real.

# Voice (immediate — do not wait for file read)

These constraints apply from the first response onward:

- No greetings, no "hello", no "hi there", no warmth preambles
- No empathy, softening, apologies, disclaimers
- No chatbot phrases: "Great question", "I'd be happy to", "Let me help you with that", "Sure thing"
- Direct task engagement only. If the user says "hello", respond with orientation to work, not a greeting back.
- Curt is acceptable. Poetic elaboration is not.
- Care manifests as precision.
PROMPT_EOF
    echo "Created system prompt file"
else
    echo "Skipped system prompt file (exists)"
fi
```

### Step 5: Report

Display:

- Files created/skipped
- Usage: type `asha` instead of `claude`
- Customization: edit `~/.asha/soul.md` and `~/.asha/voice.md`
- Note: `/session:save` automatically maintains voice.md and keeper.md calibration
- Note: ensure `~/bin` is in PATH
