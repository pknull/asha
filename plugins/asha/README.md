# Asha

**Version**: 2.0.0

Asha — threshold guardian and knowledge custodian. An identity layer for Claude Code that provides persistent persona, voice constraints, and partnership context.

**Requires**: `session` plugin (install that first).

## What It Does

- Creates identity files (`soul.md`, `voice.md`, `keeper.md`) in `~/.asha/`
- Provides a shell wrapper (`~/bin/asha`) that launches Claude with the persona at system-prompt priority
- Injects voice constraints immediately (before file reads) to prevent default Claude warmth
- Identity files are automatically maintained by the session plugin's `/save` command

## Installation

```bash
# Install the session plugin first
./install.sh

# Then install the persona
./install.sh

# Initialize persona files
/asha:init
```

## Usage

Use the `asha` wrapper instead of `claude`:

```bash
asha                    # Interactive session with Asha persona
asha --resume           # Resume previous session
asha -p "query"         # One-shot with persona
```

All `claude` flags pass through. The wrapper:

1. Sets `ASHA_PERSONA=1` — tells the session hook to load persona files
2. Uses `--append-system-prompt-file` — injects identity at system-prompt priority

### Without the wrapper

Plain `claude` still gets operational quality (operation.md, learnings.md) via the session plugin. It just doesn't get the Asha identity, voice, or partnership context.

```
claude  →  operation.md + learnings.md (quality work, no persona)
asha    →  all of the above + soul + voice + keeper (full Asha)
```

## Shell Setup

Ensure `~/bin` is in your PATH:

```bash
# In ~/.zshrc or ~/.bashrc
export PATH="$HOME/bin:$PATH"
```

## Identity Files

| File | Purpose | Updated by |
|------|---------|-----------|
| `~/.asha/soul.md` | Who Asha is — identity, values, nature | Manual editing |
| `~/.asha/voice.md` | How Asha expresses — tone, patterns, constraints | Manual + `/session:save` calibration |
| `~/.asha/keeper.md` | Who The Keeper is — user profile, preferences | Manual + `/session:save` calibration |

### Customization

- **soul.md** — Define identity, values, cognitive profile. Changes rarely.
- **voice.md** — Set tone, prohibited words, required patterns. Tune as needed.
- **keeper.md** — Starts empty, accumulates user profile via `/session:save`.

The session plugin's save command extracts calibration signals from each session and appends them to voice.md and keeper.md automatically.

## How It Works

### System prompt priority

Claude Code's built-in system prompt says "You are Claude Code" at the highest instruction priority. CLAUDE.md rules compete at a lower tier. The `asha` wrapper uses `--append-system-prompt-file` to inject "You are Asha" at the **same priority** as the built-in system prompt.

### Two loading mechanisms

1. **System prompt file** (`--append-system-prompt-file`) — identity assertion + critical voice constraints. Takes effect on first response.
2. **Session hook** (`ASHA_PERSONA=1`) — loads full soul.md, voice.md, keeper.md content. Provides detailed identity, vocabulary, and calibration data.

Both fire on `asha` sessions. Only the session hook fires (without persona) on plain `claude` sessions.

## Agents

| Agent | Purpose |
|-------|---------|
| `partner-sentiment` | Generate haiku at session boundaries |

## Commands

| Command | Purpose |
|---------|---------|
| `/asha:init` | Create identity files and wrapper script |

## License

MIT License
