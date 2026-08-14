# Orchestration Module

**Applies to**: code workflows that coordinate more than one bounded role.
`/code:orchestrate` owns the interactive command contract; recipes may reuse
these mechanics.

## Core rules

1. Start from a named workflow and concrete acceptance criteria.
2. Resolve historical and review gates from the risk preflight embedded in the
   `code-orchestrate` command. Gates are based on risk, not task size alone.
3. Give each writing agent explicit file ownership and verification commands.
4. Run dependent phases sequentially. Run phases concurrently only when their
   inputs and write scopes are independent.
5. Pass normalized handoff files rather than replaying a full conversation.
6. Read the live diff and test output at review time; a handoff is context, not
   evidence that work passed.

On harnesses without subagent spawning, execute the same role charges inline.
Preserve phase order and output contracts.

## Gates

A gate has four fields:

```text
GATE: <name>
STATUS: PASSED | FAILED | BYPASSED
EVIDENCE: <command, file location, or finding>
REQUIRED: <next action when failed>
```

- `FAILED` stops the chain until fixed or explicitly accepted by the user.
- `BYPASSED` requires a user-supplied rationale and recorded risk.
- Security and destructive-operation gates cannot be silently bypassed.
- Documentation-only and wholly mechanical changes may omit independent
  review when the orchestrator states why.

Typical gates are prior-art evidence, spec compliance, correctness review,
security review, build, and tests. Only apply gates relevant to the task.

## Parallel work

Before starting a parallel group:

- prove that no phase depends on another phase's result;
- assign non-overlapping ownership or make every phase read-only;
- tell agents they are not alone and must not revert concurrent edits;
- merge results once, preserving contradictory findings and their evidence.

If the harness cannot start the group together, run it sequentially without
pretending execution was concurrent.

## Review order

When both checks apply, separate them:

1. **Spec compliance**: does the diff implement the requested behavior and
   stay within scope?
2. **Code quality and safety**: is that behavior correct, maintainable, and
   safe at its boundaries?

A non-compliant change is not rescued by clean code. Fix the behavior or obtain
an explicit scope correction before proceeding.

## Failure handling

Do not repeat an unchanged failing approach. After each failure, record the
hypothesis, evidence, and adjustment. After three failures on the same blocker,
stop and report:

- what was attempted;
- why each attempt failed;
- the missing context or external change;
- viable alternatives.

## Recipes

Recipes under `plugins/code/recipes/` provide longer reference workflows for
feature implementation, bug investigation, refactoring, and security audit.
They do not override the command's risk routing, status handling, or user
authority boundaries.
