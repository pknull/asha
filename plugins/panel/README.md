# Panel Plugin

**Version**: 5.0.0

Recorded multi-perspective analysis with decomposition, clarification,
specialist recruitment, adversarial examination, and decision synthesis.

## When to use it

Use Panel when the problem has competing frames, hidden assumptions, several
stakeholders, or a decision that should remain auditable. Do not convene it for
a factual lookup, a routine code edit, or a decision already made.

| Need | Mode |
|---|---|
| Full decomposition and deliberation | `/panel-system:panel TOPIC` |
| Fast adversarial analysis without decomposition | `/panel-system:panel --quick TOPIC` |
| Decomposition only | `/panel-system:panel --think TOPIC` |
| Requirements interview only | `/panel-system:panel --interview TOPIC` |
| Resume or inspect recorded work | `/panel-system:panel --list`, `--show`, or `--resume` |

## Invocation by harness

| Harness | Invocation |
|---|---|
| Claude Code | `/panel-system:panel …` |
| OpenAI Codex | Request or name the rendered `panel-system-panel` skill |
| GitHub Copilot CLI | Request or name the rendered `panel-system-panel` skill |

Examples:

```text
/panel-system:panel "Should this service expose REST or GraphQL?"
/panel-system:panel --quick "Pressure-test the proposed cache invalidation rule"
/panel-system:panel --think "Decompose the work required for a multi-repository release"
/panel-system:panel --interview "Specify a task-management CLI"
```

Management:

```text
/panel-system:panel --list
/panel-system:panel --list --status=active
/panel-system:panel --show <id>
/panel-system:panel --resume <id>
/panel-system:panel --abandon <id>
```

Each run writes one resumable `state.json` beneath `Work/panels/<id>/` and one
final `decision.md`. Interview mode also writes `seed.yaml`. No auxiliary
history files, separate thinking tree, or discovery index are required.

## How the workflow works

The full path separates problem definition from solution selection:

1. `thinker` decomposes the problem and its dependencies.
2. `questioner` gathers missing requirements without proposing solutions.
3. `examiner` tests the problem frame, root cause, prerequisites, and hidden assumptions.
4. `recruiter` matches required expertise against the installed agent catalogue.
5. `fabricator` may draft a candidate agent definition only when the recruiter proves a genuine capability gap.
6. The Moderator and Challenger frame the deliberation and pressure-test the recruited specialists.

Not every mode runs every stage. `--think` and `--interview` deliberately stop
before deliberation; `--quick` skips the early definition machinery. The
`codifier` writes `seed.yaml` only for a SOUND interview.

## Agents

| Agent | Role | Direct use |
|---|---|---|
| `thinker` | Sequential decomposition and dependency analysis | Break a large problem into a numbered dependency-aware plan |
| `questioner` | Clarifying interview | Requirements are vague and should be gathered one question at a time |
| `examiner` | Problem-framing and assumption validation | The stated problem may be a symptom or category error |
| `codifier` | Convert accepted decisions into a structured seed | A validated interview needs an immutable handoff |
| `recruiter` | Score installed capabilities and identify justified gaps | Planning a specialist workforce |
| `fabricator` | Draft a portable agent definition for a proven gap | Only after recruiter justification |

Ordinary users should invoke `/panel-system:panel`; it chooses the required agents. Direct
agent use is appropriate when only that phase is wanted.

## Outputs and authority

- A fabricated agent is written to the panel workspace and is not installed automatically.
- Panel recommendations are recorded analysis, not execution permission.

## Installation

```bash
./install.sh --only panel --target claude
./install.sh --only panel --target codex
./install.sh --only panel --target copilot
```

## License

MIT
