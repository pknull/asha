# Asha

**Version**: 3.0.0

Asha is the optional identity layer for Claude Code, OpenAI Codex, GitHub
Copilot CLI, and OpenCode. It keeps ordinary launches small: three compact hot
files load automatically, whilst detailed identity and user calibration stay
cold until a task requires them.

**Requires**: `session` plugin.

## Installation

```bash
./install.sh --only session,asha --target all --bin all --default claude
```

Launch through the dispatcher. A plain harness command is the escape hatch: it
does not receive the wrapper-scoped persona, and harnesses without a reliable
instruction hook also omit the wrapper-injected operational layer. Installed
skills and native hooks remain available.

```bash
asha             # configured default harness
asha claude
asha codex
asha copilot
asha opencode
```

## Hot identity

`identity/identity-merge.sh` combines only these files:

| File | Automatic purpose |
|---|---|
| `~/.asha/soul.md` | Identity, values, nature, partnership |
| `~/.asha/voice.md` | Expression rules and working register |
| `~/.asha/keeper.md` | Stable user expertise and collaboration preferences |

The merged identity is capped at 24 KiB. An oversized merge fails without
replacing the prior cache; move detail to the cold corpus instead of raising
the cap casually.

## Cold references

The `asha-reference` skill selects one task-specific file from
`~/.asha/reference/`:

| File | Use only for |
|---|---|
| `soul-reference.md` | Full identity history, iconography, phenomenology, cognitive profile |
| `voice-reference.md` | Extended vocabulary rules, optional registers, calibration history |
| `keeper-reference.md` | Biography, family, interests, politics, philosophy, symbolism |
| `keeper-voice.md` | Generating or editing prose in PK's personal writing voice |

These files are never concatenated into the launch prompt. The skill treats
them as private and does not copy them into project or workspace Memory.

## Provisioning and maintenance

The installer creates missing `soul.md`, `voice.md`, and `keeper.md` from this
plugin's templates. It never overwrites existing identity files and does not
invent cold references. Identity maintenance is a separate reviewed edit;
Memory v2 saves and recovery hooks never modify this corpus.

## Harness injection

The dispatcher regenerates the same compact merge, then uses each harness's
native seam: Claude's append-system-prompt file, Codex's
`model_instructions_file`, Copilot's custom-instructions directory, or
OpenCode's wrapper-scoped instructions. Operational rules and active learnings
are merged separately.

See [harness enforcement](../../docs/harness-enforcement.md) for the exact
adapter behavior.

## License

MIT License
