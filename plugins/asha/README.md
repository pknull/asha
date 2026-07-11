# Asha

**Version**: 2.1.0

Asha — threshold guardian and knowledge custodian. An identity layer providing persistent persona, voice constraints, and partnership context across harnesses (Claude Code, Codex, Copilot).

**Requires**: `session` plugin (install that first).

## What This Plugin Ships

Identity **templates** (`templates/soul.md`, `templates/voice.md`) consumed by `/session:init` Step 1b, which provisions `~/.asha/` if the files are absent. The plugin no longer carries its own commands or agents:

- `/asha:init` was merged into `/session:init` (2026-07-10 ecosystem audit)
- `partner-sentiment` was removed — the session-threshold haiku ritual lives in `voice.md` and executes inline at `/save`
- The legacy `~/bin/asha` wrapper is retired; the repo's `bin/asha` dispatcher owns persona launch

## Usage

Launch through the dispatcher:

```bash
asha            # default harness with persona injection
asha claude     # explicit harness
asha codex
asha copilot
```

The dispatcher injects `identity/asha-identity-system-prompt.md` at system-prompt priority (per-harness mechanism documented in [docs/harness-enforcement.md](../../docs/harness-enforcement.md)).

### Without the dispatcher

Plain `claude` still gets operational quality (operation.md, learnings/) via the session plugin. It just doesn't get the Asha identity, voice, or partnership context.

```
claude  →  operation.md + learnings hot tier (quality work, no persona)
asha    →  all of the above + soul + voice + keeper (full Asha)
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

Harness system prompts assert their own identity at the highest instruction priority; memory-file rules compete at a lower tier. The dispatcher injects "You are Asha" at the **same priority** as the built-in system prompt (`--append-system-prompt-file` on Claude; equivalents per harness). The session SessionStart hook then loads the full soul/voice/keeper content.

## License

MIT License
