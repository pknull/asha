# Code Plugin

**Version**: 1.5.0

Development workflows for implementation, debugging, review, refactoring,
verification, PostgreSQL work, and guarded issue processing.

## Choose the right surface

| Need | Use | Why |
|---|---|---|
| Review a local diff | `/code:review` | Runs separate security, logic, edge-case, and maintainability lenses, then validates findings |
| Run repository checks | `/code:verify` | Detects the project type and runs the appropriate type, lint, test, and security tools |
| Implement a non-trivial change | `/code:orchestrate` | Routes the task through bounded specialist phases and preserves handoffs |
| Process eligible GitHub issues unattended | `/code:issue-loop` | Isolated worktrees, mechanical test gates, cold review, draft PRs only |
| Diagnose one difficult bug | Ask for the `debugger` agent | Direct specialist use is clearer than a full feature workflow |
| Review or design PostgreSQL work | Ask for the `postgres` skill | Loads database-specific guidance without starting an orchestration |

Commands coordinate work. Agents perform one bounded role. Skills add domain
instructions. Most users should start with a command rather than selecting a
chain of agents manually.

## Invocation by harness

| Harness | Invocation |
|---|---|
| Claude Code | Use the slash commands shown below, such as `/code:review --all` |
| OpenAI Codex | Ask for the operation or name the rendered skill, such as `code-review` |
| GitHub Copilot CLI | Ask for the operation or name the rendered skill, such as `code-review` |

Codex and Copilot receive command workflows as generated skills. They do not
gain custom slash commands that their harnesses do not support.

## Quick starts

```text
/code:review --all
/code:verify --full
/code:orchestrate bugfix "Fix the cache race and add a regression test"
/code:issue-loop --dry-run
```

Natural-language equivalents work on every harness:

```text
Use code-review to review every uncommitted change.
Use code-orchestrate for a bugfix: reproduce the cache race, fix it test-first,
and run the final review phase.
```

## Commands

### `/code:review [path|--all]`

- No arguments reviews staged changes.
- A path reviews that file or subtree.
- `--all` reviews every uncommitted change.
- Findings are checked against the actual files before being reported.
- The command reviews; it does not silently implement fixes.

Use it before a commit or after a risky implementation. Split diffs larger
than roughly 1,000 lines when possible so findings remain attributable.

### `/code:verify [--quick|--full] [--file PATH]`

| Mode | Intended use |
|---|---|
| `--quick` | Post-edit type and format checks |
| default | Types, lint, and tests before commit |
| `--full` | Security and dependency checks before a PR or release |
| `--file PATH` | Narrow check while editing one file |

The verifier detects TypeScript, Python, Go, Java, and Rust projects. A
repository may override the detected checks with `verify.yaml`.

### `/code:orchestrate [--tier=…] TYPE DESCRIPTION`

Supported workflow types:

| Type | Default phases |
|---|---|
| `feature` | prior art → test-first implementation → code and security review |
| `bugfix` | root-cause investigation → regression test and fix → review |
| `refactor` | prior art → bounded cleanup → code and security review |
| `security` | parallel audit → test-first remediation plan |
| `custom` | User-specified sequential and parallel agent groups |

Examples:

```text
/code:orchestrate feature "Add token rotation"
/code:orchestrate --tier=high refactor "Replace the namespace registry"
/code:orchestrate custom "codebase-historian,tdd,[reviewer,reviewer]" "Build dashboard"
```

The orchestrator writes handoffs under `Work/orchestrate/<run-id>/` and records
self-review calibration under `~/.asha/metrics/orchestrate.jsonl`. High-risk
paths and cross-plugin changes are promoted to the high tier unless explicitly
overridden.

### `/code:issue-loop [--dry-run]`

This is not the ordinary way to fix one issue. It is the guarded unattended
path for a backlog. It requires both:

1. committed repository configuration at `.asha/issue-loop.json`; and
2. an entry for that repository under `issue_loop.repos` in
   `~/.asha/config.json`.

Run `--dry-run` first. The engine creates one isolated worktree per safe issue,
requires mechanical reproduction and test gates, runs a cold review, and may
open draft PRs. It never pushes `main`/`master`, opens a ready-for-review PR, or
merges. See [engines/README.md](engines/README.md) for the operating contract.

## Agents

| Agent | Role | Direct use |
|---|---|---|
| `codebase-historian` | Find repository prior art, earlier failures, and historical decisions | Before design when existing patterns matter |
| `debugger` | Reproduce failures, test hypotheses, and isolate root causes | One difficult bug or unexplained failure |
| `refactor-cleaner` | Remove verified dead code and consolidate duplication | After behavior is pinned by tests |
| `reviewer` | Read-only correctness, security, regression, and maintainability review | Independent final pass |
| `tdd` | Test-first implementation using red, green, and refactor cycles | A bounded behavior change with clear acceptance criteria |

Direct agents do not replace the command's coordination contract. For example,
`reviewer` supplies the canonical severity and evidence rules, whilst
`/code:review` decides scope, applies multiple lenses, and validates the merged
findings.

## Skill

| Skill | Purpose | Example request |
|---|---|---|
| `postgres` (installed as `code-postgres`) | Query plans, schema design, RLS, migration safety, and database security | `Use code-postgres to review this migration and RLS policy.` |

## Recipes

Recipes under `recipes/` are orchestration definitions and reference material,
not separate slash commands.

| Recipe | Use case |
|---|---|
| `feature-implementation.yaml` | End-to-end feature work with design and review checkpoints |
| `bug-investigation.yaml` | Interactive diagnosis and regression-test workflow for one bug |
| `fix-loop.yaml` | Test-gated unattended processing of a bug backlog |
| `refactor-safe.yaml` | Cleanup with an approved deletion plan and behavior checks |
| `security-audit.yaml` | Security assessment followed by approved remediation |

Use `/code:orchestrate` for ordinary interactive orchestration and
`/code:issue-loop` for the specifically guarded unattended path.

## Installation

```bash
./install.sh --only code --target claude
./install.sh --only code --target codex
./install.sh --only code --target copilot
```

Re-run installation after changing command or agent sources because Codex and
Copilot receive generated artifacts rather than live symlinks for those forms.

## License

MIT
