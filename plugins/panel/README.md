# Panel Plugin

**Version**: 5.0.0

Multi-perspective analysis with decomposition, clarification, specialist recruitment, adversarial examination, and a recorded decision report.

## Command

```text
/panel <topic>
/panel --interview <topic>
/panel --quick <topic>
/panel --think <topic>
```

The full protocol is defined by `commands/panel.md`. Panel state is written under `Work/panels/`; decomposition state uses `Work/thinking/`.

## Agents

| Agent | Role |
|---|---|
| `thinker` | Sequential decomposition and dependency analysis |
| `questioner` | Clarifying interview |
| `examiner` | Problem-framing and assumption validation |
| `codifier` | Convert clarified decisions into a structured seed |
| `recruiter` | Score installed agent capabilities and identify gaps |
| `fabricator` | Produce a portable candidate agent definition for a justified gap |

Core panel perspectives are documented under `docs/characters/`: The Moderator, The Analyst, The Challenger, and The Thinker.

## Harness behavior

Recruitment uses the current harness's installed agent catalogue. In this repository, portable source definitions live under `plugins/*/agents/*.md`. A fabricated Markdown definition is written to the panel workspace and is not installed automatically; activation must use the target harness's native agent format.

Roles may run as spawned subagents where supported or inline where they are not. The output and evidence contracts remain the same.

## Installation

```bash
./install.sh --only panel
```

## License

MIT
