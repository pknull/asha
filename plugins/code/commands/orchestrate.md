---
description: "Multi-agent workflow with sequential and parallel phases"
argument-hint: "[feature|bugfix|refactor|security|custom] <description>"
---

# Orchestrate Command

Run multi-agent workflows with sequential and parallel phases.

## Usage

```
/orchestrate feature "Add user authentication"
/orchestrate bugfix "Fix race condition in cache"
/orchestrate refactor "Extract payment module"
/orchestrate security "Audit API endpoints"
/orchestrate custom "architect,[tdd,code-reviewer],security-auditor" "Redesign caching"
```

## Workflow Types

| Type | Phases | Notes |
|------|--------|-------|
| `feature` | `architect` → `tdd` → `[code-reviewer, security-auditor]` | Design, test, parallel review |
| `bugfix` | `debugger` → `tdd` → `code-reviewer` | Investigate, test fix, review |
| `refactor` | `architect` → `refactor-cleaner` → `[code-reviewer, security-auditor]` | Plan, clean, parallel review |
| `security` | `[security-auditor, code-reviewer]` → `architect` | Parallel audit, then remediation plan |
| `custom` | User-specified | Use brackets for parallel groups |

## Phase Notation

- **Sequential**: `agent1` → `agent2` — run one after the other, pass handoff
- **Parallel**: `[agent1, agent2]` — run simultaneously, merge results

Example custom workflow:

```
/orchestrate custom "architect,[backend-dev,frontend-dev],[code-reviewer,security-auditor]" "Build dashboard"
```

This runs:

1. `architect` (sequential)
2. `backend-dev` + `frontend-dev` (parallel)
3. `code-reviewer` + `security-auditor` (parallel)

## Execution Protocol

### Sequential Phase

For each agent in sequence:

1. **Invoke** agent with task + context from previous phase
2. **Collect** output as handoff document
3. **Pass** handoff to next phase

### Parallel Phase

For agents in brackets `[a, b, c]`:

1. **Invoke all agents simultaneously** using multiple Task tool calls in single message
2. **Wait** for all to complete
3. **Merge** outputs into combined handoff for next phase

**IMPORTANT**: To run agents in parallel, you MUST send multiple Task tool calls in a single message. Do not wait for one to finish before starting another.

```
// CORRECT - parallel execution
<message>
  <Task agent="code-reviewer" .../>
  <Task agent="security-auditor" .../>
</message>

// WRONG - sequential execution
<message><Task agent="code-reviewer" .../></message>
<message><Task agent="security-auditor" .../></message>
```

## Handoff Format

Between phases, create handoff document:

```markdown
## HANDOFF: [previous] → [next]

### Context
[What was done]

### Findings
[Key discoveries/decisions]

### Files Modified
[List of files]

### Open Questions
[Unresolved items]

### Recommendations
[Suggested next steps]
```

For parallel phase outputs, merge into single handoff:

```markdown
## HANDOFF: [code-reviewer + security-auditor] → next

### code-reviewer Findings
[Summary]

### security-auditor Findings
[Summary]

### Combined Recommendations
[Merged next steps]
```

## Final Report

```
ORCHESTRATION REPORT
====================
Workflow: feature
Task: Add user authentication
Phases: architect → tdd → [code-reviewer, security-auditor]

PHASE RESULTS
1. architect: [summary]
2. tdd: [summary]
3. code-reviewer: [summary] (parallel)
   security-auditor: [summary] (parallel)

FILES CHANGED
[List]

TEST RESULTS
[Pass/fail, coverage]

ISSUES FOUND
- [code-reviewer] Issue 1
- [security-auditor] Issue 2

RECOMMENDATION
[SHIP | NEEDS WORK | BLOCKED]
```

## Available Agents

| Agent | Purpose |
|-------|---------|
| `architect` | Design, planning, structure |
| `tdd` | Test-driven development |
| `code-reviewer` | Code quality review |
| `security-auditor` | Security analysis |
| `debugger` | Bug investigation |
| `refactor-cleaner` | Code cleanup |
| `typescript-pro` | TypeScript specialist |
| `python-pro` | Python specialist |
| `build-error-resolver` | Build/type error fixes |

## Tips

1. **Start with `architect`** for complex features — design before building
2. **End with parallel review** — `[code-reviewer, security-auditor]` catches more issues
3. **Use `tdd` early** — tests define the contract before implementation
4. **Keep handoffs concise** — focus on what next phase needs, not full output
5. **Custom for flexibility** — mix agents as needed for your specific task
