# Code Plugin

**Version**: 1.2.0

Development workflows for implementation, debugging, review, refactoring, and verification.

## Agents

| Agent | Role |
|---|---|
| `codebase-historian` | Find repository prior art and historical decisions |
| `debugger` | Reproduce failures, test hypotheses, and isolate root causes |
| `refactor-cleaner` | Remove verified dead code and consolidate duplication |
| `reviewer` | Read-only correctness, security, regression, and maintainability review |
| `tdd` | Test-first implementation using red, green, and refactor cycles |

## Commands

| Command | Purpose |
|---|---|
| `/code:orchestrate` | Route multi-phase implementation and review workflows |
| `/code:review` | Apply security, logic, edge-case, and maintainability review lenses |
| `/code:verify` | Run repository-specific type, lint, test, and security checks |

Review severity and verdict rules are canonical in `agents/reviewer.md`; the review command supplies orchestration lenses rather than a second policy.

## Installation

```bash
./install.sh --only code
```

## License

MIT
