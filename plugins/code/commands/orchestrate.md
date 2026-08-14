---
name: code-orchestrate
description: "Run a bounded feature, bugfix, refactor, security, or custom workflow"
argument-hint: "[feature|bugfix|refactor|security|custom] <description>"
---

# Orchestrate Command

Coordinate specialist phases for a non-trivial code change. Select the workflow
from the task type, then add historical or review gates only when the change's
risk warrants them. This command is self-contained across all harnesses.

## Usage

```text
/code:orchestrate feature "Add user authentication"
/code:orchestrate bugfix "Fix the cache race"
/code:orchestrate refactor "Extract the payment module"
/code:orchestrate security "Audit API authorization"
/code:orchestrate custom "codebase-historian,tdd,[reviewer,reviewer]" "Redesign caching"
```

The workflow name is required. For `custom`, the first quoted argument is the
phase expression and the second is the task.

## Workflow Shapes

| Type | Core phases |
|---|---|
| `feature` | `tdd` |
| `bugfix` | `debugger` -> `tdd` |
| `refactor` | `refactor-cleaner` |
| `security` | `[reviewer (security), reviewer (correctness)]` -> `tdd` |
| `custom` | User-specified sequence; brackets denote a parallel group |

Apply the risk preflight before execution:

- Prepend `codebase-historian` when the task changes architecture, lifecycle,
  harness integration, public interfaces, schemas, migrations, a new plugin,
  multiple plugin directories, registry/namespace behavior, or established
  cross-file patterns.
- Append `reviewer` after changes to logic, security boundaries, public
  interfaces, installers, uninstallers, doctors, hooks, migrations, generated
  artifacts, or more than one plugin. Also append it after a repeated failed
  implementation attempt.
- Use a distinct security-focused reviewer when authentication, authorization,
  cryptography, secrets, untrusted input, or dependency trust is involved.
- A documentation-only or wholly mechanical edit may omit both gates. State
  that decision; do not silently treat uncertain work as low risk.

This yields common paths such as:

```text
feature:  [historian when indicated] -> tdd -> [reviewer when indicated]
bugfix:   debugger -> tdd -> reviewer
refactor: [historian when indicated] -> refactor-cleaner -> reviewer
security: [security reviewer, correctness reviewer] -> tdd -> reviewer
```

## Phase Notation

- `a,b,c` runs phases sequentially and passes a handoff between them.
- `[a,b]` runs independent phases concurrently when the harness supports it.
- On a harness without subagent spawning, perform the same charges inline and
  preserve their order. Parallel groups may run sequentially inline.

Do not parallelize agents that edit the same files. Give every writing agent
explicit file ownership and tell it not to revert concurrent changes.

## Execution

### 1. Preflight

1. Parse and validate the workflow name and custom expression.
2. Read repository instructions and inspect the relevant files or current diff.
3. Classify risk using the rules above. Repository-local orchestration rules
   may add gates but may not suppress a required historian or reviewer.
4. Resolve the exact phase sequence and report it in one line:

```text
WORKFLOW: bugfix | debugger -> tdd -> reviewer | review: logic change
```

If the task lacks a concrete success condition, ask for that condition before
starting. Do not turn a bounded implementation request into a requirements
interview merely because several files are involved.

### 2. Run phases

For each sequential phase:

1. Give the agent the task, repository constraints, owned paths, acceptance
   criteria, verification commands, and the previous handoff path.
2. Collect its result and branch on its returned status.
3. Write the normalized handoff beneath
   `Work/code-orchestrate/<run-id>/` and pass the path to the next phase.

For a parallel group, start every independent charge together where supported,
wait for all results, then merge them into one handoff. Label findings by
reviewer focus so disagreement remains visible.

### 3. Status contract

Every spawned phase must end with:

```text
STATUS: <DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED>
summary: <1-3 sentences>
files_modified: <absolute paths or []>
open_questions: <items or []>
recommendations: <items or []>
```

Handle the status rather than merely recording it:

| Status | Action |
|---|---|
| `DONE` | Continue. |
| `DONE_WITH_CONCERNS` | Continue and carry concerns into later phases and the final report. |
| `NEEDS_CONTEXT` | Gather the named context, then retry the same phase once informed. |
| `BLOCKED` | Stop and report the blocker. |

A missing status is a malformed handoff. Ask the phase to restate its result in
the contract; do not infer success from prose.

### 4. Handoffs

Use one file per phase or parallel group:

```markdown
## HANDOFF: debugger -> tdd [DONE]

### Context
What was examined or changed.

### Findings
Root cause, decisions, and evidence.

### Files Modified
Paths, or `None`.

### Open Questions
Unresolved items, or `None`.

### Recommendations
Checks or follow-up for the next phase.
```

Use filenames that preserve order, for example `01-debugger.md`,
`02-tdd.md`, and `03-review.md`. Handoffs are scratch coordination artifacts,
not project documentation and not durable metrics.

### 5. Verification and report

The implementing phase runs the narrow relevant checks. The final reviewer
reads the actual diff and test output rather than trusting the handoff. Do not
commit, push, merge, deploy, or perform destructive cleanup unless the user
explicitly requested it.

Return:

```text
ORCHESTRATION REPORT
Workflow: <type>
Phases: <resolved sequence>
Phase results: <one line each>
Files changed: <paths>
Verification: <commands and results>
Review: <SHIP | NEEDS WORK | BLOCKED, with findings>
Open questions: <items or None>
```
