# Code Plugin

Development workflows for software engineering: orchestration patterns, code review, TDD, and specialized agents.

## Installation

```bash
/plugin install code@asha-marketplace
```

## Features

### Commands

- `/code:review` - Parallel code review with 4 specialized reviewers (security, logic, edge cases, style)

### Agents

- **codebase-historian** - Pattern archaeologist for prior art discovery. Surfaces what was tried before, what worked, what failed.

### Modules

- **code.md** - Convention matching, code comments, reference format (uses `asha/cognitive.md` for ACE cycle)
- **orchestration.md** - Quality gates, Socratic planning, scale-adaptive workflows, two-stage review, failure tracking

### Recipes

Pre-defined multi-agent workflows:

| Recipe | Complexity | Use Case |
|--------|------------|----------|
| `feature-implementation.yaml` | full | New features end-to-end |
| `bug-investigation.yaml` | standard | Bug diagnosis and fix |
| `refactor-safe.yaml` | standard | Code cleanup with safety |
| `security-audit.yaml` | full | Security hardening |

## Usage

### Code Review

```bash
/code:review              # Review staged changes
/code:review <path>       # Review specific file(s)
/code:review --all        # Review all uncommitted changes
```

### Using Recipes

1. Read the recipe matching your task type
2. Follow the agent sequence and state transitions
3. Respect checkpoints (human approval gates)
4. Handle failures per the recipe's failure policy

## Patterns

### Quality Gates

Mandatory checkpoints that block progression until passed:

- Pre-Design: Prior art check (codebase-historian)
- Pre-Implementation: Design approval
- Pre-Commit: Code review + security scan
- Pre-Push: Build + tests pass
- Pre-Merge: Full review (>500 lines)

### Socratic Planning

Question before building:

1. What problem does this solve?
2. Who encounters this problem?
3. What does success look like?
4. What's out of scope?

### Scale-Adaptive Planning

Automatically adjust depth based on complexity:

- **Quick Flow** (simple): Clarify → Implement → Review
- **Standard Flow** (moderate): + Design approval, TDD
- **Full Flow** (complex): + Research, architecture review, staged rollout

## License

MIT
