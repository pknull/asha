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
/orchestrate custom "general-purpose,[tdd,reviewer],reviewer" "Redesign caching"  # final reviewer: security focus

# Tier override (skip Phase 0 inference):
/orchestrate --tier=high feature "Refactor namespace registry"
/orchestrate --tier=trivial bugfix "Fix typo in panel README"
```

## Workflow Types

| Type | Phases | Notes |
|------|--------|-------|
| `feature` | `general-purpose` (design charge) → `tdd` → `[reviewer, reviewer (security focus)]` | Design, test, parallel review |
| `bugfix` | `debugger` → `tdd` → `reviewer` | Investigate, test fix, review |
| `refactor` | `general-purpose` (design charge) → `refactor-cleaner` → `[reviewer, reviewer (security focus)]` | Plan, clean, parallel review |
| `security` | `[reviewer (security focus), reviewer]` → `general-purpose` (remediation-plan charge) | Parallel audit, then remediation plan |
| `custom` | User-specified | Use brackets for parallel groups |

## Phase Notation

- **Sequential**: `agent1` → `agent2` — run one after the other, pass handoff
- **Parallel**: `[agent1, agent2]` — run simultaneously, merge results

Example custom workflow:

```
/orchestrate custom "general-purpose,[backend-dev,frontend-dev],[reviewer,reviewer]" "Build dashboard"
```

This runs:

1. `general-purpose` (sequential, design charge)
2. `backend-dev` + `frontend-dev` (parallel)
3. `reviewer` + `reviewer` (parallel — charge one with security focus)

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
   - **High** → Opus plan-review (`general-purpose`, design charge) prepended, then Sonnet implementation
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

For **High** tier: prepend `general-purpose` (Opus, design charge) to the workflow as a plan-review phase. The design phase produces a design handoff; Sonnet implements against it.

## Execution Protocol

> **Harness note**: On Claude, phases MAY spawn subagents in parallel via the Agent tool. On harnesses without subagent spawning, execute each phase sequentially inline (same prompts, same charges). Output contracts are identical either way.

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
  <Task agent="reviewer" .../>            // code-quality charge
  <Task agent="reviewer" .../>            // security-focus charge
</message>

// WRONG - sequential execution
<message><Task agent="reviewer" .../></message>
<message><Task agent="reviewer" .../></message>
```

## Subagent Return Contract (REQUIRED)

Every Task invocation in this workflow MUST instruct the subagent to use the return contract defined in [`~/.asha/agent-coordination.md`](../../../.asha/agent-coordination.md) (load it if you need the full spec). Append this verbatim to each agent's task prompt:

> End your response with a status line and envelope:
> `STATUS: <DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED>`
> then `summary` (1-3 sentences), `files_modified` (absolute paths; `[]` if read-only), `open_questions` (`[]` if none), `recommendations` (`[]` if none). All four fields always present, even when empty.

The orchestrator MUST branch on the returned STATUS - never ignore it:

| STATUS | Orchestrator action |
|---|---|
| `DONE` | Proceed to next phase. |
| `DONE_WITH_CONCERNS` | Proceed, but carry `open_questions` + `recommendations` into the next phase's context AND the final report. Never silently swallow. |
| `NEEDS_CONTEXT` | Do NOT re-invoke blindly. Gather the context named in `open_questions`, then re-invoke the same phase. |
| `BLOCKED` | HALT the phase chain. Report the blocker to the user. Do not downgrade to `DONE_WITH_CONCERNS` to keep the chain moving. |

This is the contract whose adoption **Phase Final** records. A run whose subagents emit no STATUS line is a contract miss - log it as such (see Calibration Log).

## Handoff Format

Between phases, write the handoff to a **file** in the orchestration scratch dir (`Work/orchestrate/<run-id>/handoff.md`, or the panel dir if running under one) so handoff content stays out of caller context. Consumers read it by filename. The status token from the return contract MUST appear in the header:

```markdown
## HANDOFF: [previous] → [next] [<STATUS>]

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

**Richer phases write named artifacts** (per the contract), also in the scratch dir:

- Design phases (`general-purpose`, design charge) -> `plan-summary.md` (Goal, Approach, Interfaces, Open decisions, Out of scope).
- `reviewer` (code-quality and security-focus runs) -> `review-findings.md` (Verdict `SHIP|NEEDS WORK|BLOCKED`, Critical issues w/ file:line, Concerns, Nits, False-positive log). Parallel reviewers each write their own (`review-findings-code.md`, `review-findings-security.md`); orchestrator merges.

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

`claimed_status` is the implementer's pre-review self-assessment (it feeds the calibration log); the return-contract `STATUS` (see Subagent Return Contract) is the phase's actual return token. Map them 1:1 — `ready` <-> `DONE`, `needs-work` <-> `DONE_WITH_CONCERNS`, `blocked` <-> `BLOCKED` — and emit both consistently.

For parallel phase outputs, merge into single handoff:

```markdown
## HANDOFF: [reviewer (code) + reviewer (security)] → next

### reviewer (code) Findings
[Summary]

### reviewer (security) Findings
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
Phases: general-purpose (design) → tdd → [reviewer, reviewer (security)]

PHASE RESULTS
1. general-purpose (design): [summary]
2. tdd: [summary]
3. reviewer (code): [summary] (parallel)
   reviewer (security): [summary] (parallel)

FILES CHANGED
[List]

TEST RESULTS
[Pass/fail, coverage]

ISSUES FOUND
- [reviewer/code] Issue 1
- [reviewer/security] Issue 2

RECOMMENDATION
[SHIP | NEEDS WORK | BLOCKED]
```

## Phase Final: Calibration Log

After the review phase completes (pass or fail), append one JSONL row to `~/.asha/metrics/orchestrate.jsonl`:

```json
{"ts":"<ISO-8601 UTC>","workflow":"<feature|bugfix|...>","tier":"<trivial|low|medium|high>","model":"<haiku|sonnet|opus+sonnet>","claimed":"<ready|needs-work|blocked>","review":"pass|fail","gates_failed":["<gate>",...],"contract":{"statuses":["<returned STATUS per phase, in order>"],"artifacts":["<artifact basenames written, e.g. handoff.md, plan-summary.md>"]},"task":"<one-line task description>"}
```

Steps:

1. `mkdir -p ~/.asha/metrics` (defensive)
2. Compose the row from Phase 0 declaration + implementer's `claimed_status` + review outcome
3. Populate `contract.statuses` with each phase's returned STATUS token (in phase order) and `contract.artifacts` with the artifact files written this run (`handoff.md`, `plan-summary.md`, `review-findings*.md`). A run whose subagents emitted no STATUS leaves `statuses` empty — that is the adoption-miss signal.
4. Append (don't overwrite) to `~/.asha/metrics/orchestrate.jsonl`
5. If write fails, log to stderr and continue — calibration is observability, never a blocker

Inspect over time:

```
~/life/asha/bin/calibration              # last 30 runs summary
~/life/asha/bin/calibration --tier=high  # filter by tier
~/life/asha/bin/calibration --tail=10    # last 10 runs detail
```

Sustained false-positive rate (claimed=ready AND review=fail) above ~25% means the implementer agent's self-review is miscalibrated.

**Return-contract adoption** is measured directly from this log: the fraction of runs with a non-empty `contract.statuses`. This replaces transcript-grepping for A3 acceptance — query `orchestrate.jsonl` instead.

## Available Agents

| Agent | Purpose |
|-------|---------|
| `general-purpose` | Design/planning (design charge), language-specific implementation, build fixes |
| `thinker` | Requirements breakdown, approach planning |
| `tdd` | Test-driven development |
| `reviewer` | Code quality review; security analysis when charged with security focus |
| `debugger` | Bug investigation |
| `refactor-cleaner` | Code cleanup and refactoring |

## Tips

1. **Start with a design phase** (`general-purpose`, design charge) for complex features — design before building
2. **End with parallel review** — `[reviewer, reviewer (security focus)]` catches more issues
3. **Use `tdd` early** — tests define the contract before implementation
4. **Keep handoffs concise** — focus on what next phase needs, not full output
5. **Custom for flexibility** — mix agents as needed for your specific task
