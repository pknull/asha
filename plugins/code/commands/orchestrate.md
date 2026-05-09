---
name: code-orchestrate
description: "Multi-agent workflow with sequential and parallel phases"
argument-hint: "[--tier=trivial|low|medium|high] [feature|bugfix|refactor|security|custom] <description>"
---

# Orchestrate Command

Run multi-agent workflows with sequential and parallel phases. Routes by complexity (Phase 0) and records self-review calibration data per run.

## Usage

```
/orchestrate feature "Add user authentication"
/orchestrate bugfix "Fix race condition in cache"
/orchestrate refactor "Extract payment module"
/orchestrate security "Audit API endpoints"
/orchestrate custom "architect,[tdd,code-reviewer],security-auditor" "Redesign caching"

# Tier override (skip Phase 0 inference):
/orchestrate --tier=high feature "Refactor namespace registry"
/orchestrate --tier=trivial bugfix "Fix typo in panel README"
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

## Phase 0: Routing (always runs first)

Before any agent work, classify the task by complexity and select the implementation model. Full rules in [`../modules/complexity-routing.md`](../modules/complexity-routing.md).

### Routing Protocol

1. Load `.claude/orchestrate-rules.json` from repo root if present
2. Determine change scope:
   - Pre-implementation tasks: infer from task description
   - Tasks operating on an existing diff: read `git diff --name-only`
3. Apply tier rules:
   - **Trivial** → Haiku
   - **Low / Medium** → Sonnet
   - **High** → Opus plan-review (architect agent) prepended, then Sonnet implementation
4. Honor `--tier=X` user override; skip inference and note `(user override)` in declaration

### Tier escalation triggers (any one promotes to High)

- Path match in `high_complexity_paths` (project rules)
- New plugin directory created
- Cross-plugin scope (≥2 plugin dirs touched)
- Self-edit risk: installer, hooks, namespace registry, session-load code
- User override `--tier=high`

### Tier declaration

Emit before Phase 1:

```
[ROUTING]
Tier:   {Trivial|Low|Medium|High}
Model:  {Haiku|Sonnet|Opus+Sonnet}
Reason: {one-line justification — cite path matches, scope, or override}
```

For **High** tier: prepend `architect` (Opus) to the workflow as a plan-review phase. The architect produces a design handoff; Sonnet implements against it.

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

### Implementer self-review (REQUIRED before review phase)

The implementing agent (whichever produces code in this workflow) MUST end its handoff with a `claimed_status` declaration:

```markdown
### claimed_status
ready | needs-work | blocked

[If needs-work or blocked: bullet list of known issues or blockers]
```

- **ready** — implementer believes all gates will pass
- **needs-work** — known issues remain (must list them)
- **blocked** — cannot proceed without external input (must say what)

This declaration is the calibration signal — the review phase compares it against actual gate outcomes and records the delta.

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

## Phase Final: Calibration Log

After the review phase completes (pass or fail), append one JSONL row to `~/.asha/metrics/orchestrate.jsonl`:

```json
{"ts":"<ISO-8601 UTC>","workflow":"<feature|bugfix|...>","tier":"<trivial|low|medium|high>","model":"<haiku|sonnet|opus+sonnet>","claimed":"<ready|needs-work|blocked>","review":"pass|fail","gates_failed":["<gate>",...],"task":"<one-line task description>"}
```

Steps:

1. `mkdir -p ~/.asha/metrics` (defensive)
2. Compose the row from Phase 0 declaration + implementer's `claimed_status` + review outcome
3. Append (don't overwrite) to `~/.asha/metrics/orchestrate.jsonl`
4. If write fails, log to stderr and continue — calibration is observability, never a blocker

Inspect over time:

```
~/life/asha/bin/calibration              # last 30 runs summary
~/life/asha/bin/calibration --tier=high  # filter by tier
~/life/asha/bin/calibration --tail=10    # last 10 runs detail
```

Sustained false-positive rate (claimed=ready AND review=fail) above ~25% means the implementer agent's self-review is miscalibrated.

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
