# Code Plugin

**Version**: 1.5.0

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
| `/code:issue-loop` | Overnight issue-to-merge loop: triage → worker-per-issue worktrees → cold review → draft PRs only |
| `/code:orchestrate` | Route multi-phase implementation and review workflows |
| `/code:review` | Apply security, logic, edge-case, and maintainability review lenses |
| `/code:verify` | Run repository-specific type, lint, test, and security checks |

Review severity and verdict rules are canonical in `agents/reviewer.md`; the review command supplies orchestration lenses rather than a second policy.

## Engines

| Engine | Purpose |
|---|---|
| `issue-loop` | Workflow-tool script behind `/code:issue-loop` — triage, test-first iteration, cold review, guarded draft-PR publish, mandatory run report. See `engines/README.md`. |

The loop is **dual opt-in** and disabled everywhere by default: the target
repo commits `.asha/issue-loop.json` (template in `templates/issue-loop.json`)
AND the repo is listed under `issue_loop.repos` in `~/.asha/config.json`.
`tools/issue-loop-preflight.sh` enforces both, probes `gh`, and verifies the
policy guards against the loop's own command set before anything dispatches;
`tools/issue-loop-publish.sh` is the loop's sole push path (never main/master,
draft PRs hardcoded). Rails tested in `tests/test-issue-loop.sh`, engine in
`tests/js/issue-loop.test.mjs`, guard pins in Test 104.

## Skills

| Skill | Purpose |
|---|---|
| `postgres` (installs as `code-postgres`) | PostgreSQL review and design guidance: query optimization, EXPLAIN analysis, schema design, RLS policies, migration safety |

## Installation

```bash
./install.sh --only code
```

## License

MIT
