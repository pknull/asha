# Code Plugin

**Version**: 1.7.0

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
| Read or act on GitHub with `gh` | Ask for the `github-cli` skill | Portable gh foundation: bounded reads, explicit writes, separately authorized merge and release |

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

The verifier detects TypeScript, Python, Go, Java, and Rust projects from
their project files. It reads no configuration file; when detection does not
fit, the command infers narrow checks from the repository's own manifests.

### `/code:orchestrate TYPE DESCRIPTION`

Supported workflow types:

| Type | Default phases |
|---|---|
| `feature` | test-first implementation with risk-triggered prior art and review |
| `bugfix` | root-cause investigation → regression test and fix → review |
| `refactor` | bounded cleanup with risk-triggered prior art and review |
| `security` | parallel audit → test-first remediation plan |
| `custom` | User-specified sequential and parallel agent groups |

Examples:

```text
/code:orchestrate feature "Add token rotation"
/code:orchestrate refactor "Replace the namespace registry"
/code:orchestrate custom "codebase-historian,tdd,[reviewer,reviewer]" "Build dashboard"
```

The orchestrator writes scratch handoffs under
`Work/code-orchestrate/<run-id>/`. Architecture, lifecycle, public-interface,
and cross-plugin changes receive prior-art and independent-review gates. It
does not write telemetry or durable self-assessment records.

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

## Skills

| Skill | Purpose | Example request |
|---|---|---|
| `postgres` (installed as `code-postgres`) | Query plans, schema design, RLS, migration safety, and database security | `Use code-postgres to review this migration and RLS policy.` |
| `github-cli` (installed as `code-github-cli`) | Portable GitHub CLI foundation: discovery and authentication checks, bounded repository, issue, PR, review, Actions and release reads, explicit draft PRs, comments and review requests; merge, approval and release need separate authorization | `Use code-github-cli to summarise the failing checks on PR 42 in OWNER/REPO.` |

`github-cli` needs no MCP server, persona, Memory or issue-loop configuration.
Repository policy and workflow limits stay with their owners: `/code:issue-loop`
uses it only for gh discovery, setup and authentication and keeps its draft-only,
never-merge rule.

## Installation

```bash
./install.sh --only code --target claude
./install.sh --only code --target codex
./install.sh --only code --target copilot
```

Re-run installation after changing command or agent sources because Codex and
Copilot receive generated artifacts rather than live symlinks for those forms.

To use `verify.py`, the `github-cli` skill or the debugging guidance without
Asha, see [Standalone reuse](../../docs/standalone-reuse.md).

## License

MIT
