# Orchestration Module — Multi-Agent Workflow Patterns

**Applies to**: Complex tasks requiring multiple agents, phased workflows, quality gates, or adaptive planning depth.

## Quality Gates (Blocking Checkpoints)

Quality gates are **mandatory checkpoints** that block progression until passed. Unlike advisory reviews, gates require explicit sign-off.

### Gate Definitions

| Phase | Gate | Agent/Check | Blocking | Bypass |
|-------|------|-------------|----------|--------|
| Pre-Design | Prior Art | codebase-historian | Yes | "No prior art needed" |
| Pre-Implementation | Design Approval | User or architect | Yes | Never |
| Pre-Commit | Code Review | code-reviewer | Yes | Trivial changes (<10 lines) |
| Pre-Commit | Security Scan | security-auditor | Yes (if auth/crypto/input) | Never for security-sensitive |
| Pre-Push | Build Passes | `build` command | Yes | Never |
| Pre-Push | Tests Pass | `test` command | Yes | Never |
| Pre-Merge (>500 lines) | Full Review | code-reviewer --thorough | Yes | Split into smaller PRs |

### Gate Protocol

```
[GATE: {gate-name}]
Status: PENDING | PASSED | FAILED | BYPASSED
Agent: {agent or check}
Result: {outcome summary}
Blocking: {yes/no}

[If FAILED]
Reason: {why it failed}
Required: {what must change}
-> DO NOT PROCEED until gate passes or user explicitly bypasses with rationale

[If BYPASSED]
Rationale: {user-provided justification}
Risk accepted: {acknowledged}
```

### Gate Enforcement

1. Before each phase transition, check applicable gates
2. Run gate agent/check
3. If FAILED: stop, report, wait for fix or explicit bypass
4. If PASSED: record and proceed
5. If BYPASSED: record rationale, proceed with warning

Gates are not suggestions. Proceeding past a failed gate without bypass rationale is a protocol violation.

---

## Socratic Planning (Question Before Building)

Before generating plans or implementations, **step back and clarify intent**.

### Activation Triggers

- New feature requests
- Ambiguous requirements
- Tasks with multiple valid interpretations
- User says "I want..." without specifics
- Refactoring requests without clear scope

### Socratic Protocol

**Phase 1: Understanding**
```
Before planning, I want to understand the goal:

1. What problem does this solve? (not what feature, but what pain point)
2. Who encounters this problem? (user type, frequency)
3. What does success look like? (measurable outcome)
4. What's out of scope? (boundaries)
```

Wait for response. Do not assume answers.

**Phase 2: Constraints**
```
Understanding constraints:

1. Must integrate with: [existing systems?]
2. Must avoid: [patterns, dependencies, approaches?]
3. Timeline pressure: [quick fix vs proper solution?]
4. Future considerations: [extensibility needs?]
```

**Phase 3: Chunked Presentation**

When presenting designs:
- Show in digestible chunks (3-5 points max per section)
- Get sign-off on each chunk before proceeding
- Do not present monolithic plans

### Anti-Patterns

- Generating full implementation plans without clarification
- Assuming user intent from ambiguous statements
- Presenting 50-line design docs without checkpoints
- "I'll just build what I think you want"

---

## Scale-Adaptive Planning

Automatically adjust planning depth based on project complexity and risk.

### Complexity Assessment

| Factor | Low (1) | Medium (2) | High (3) |
|--------|---------|------------|----------|
| Scope | Single file | Multi-file | Multi-system |
| Risk | Reversible | Requires rollback plan | Data loss possible |
| Domain | Well-understood | Some unknowns | Novel territory |
| Dependencies | None | Internal only | External systems |
| Users affected | Developer only | Team | Production users |

**Score**: Sum factors. Range 5-15.

### Planning Depth by Score

**Quick Flow (Score 5-7)**:
```
1. Clarify (1-2 questions max)
2. Implement
3. Review
```

Skip: formal design docs, architecture review, extensive test planning.
Example: Bug fix, config change, documentation update.

**Standard Flow (Score 8-11)**:
```
1. Clarify (Socratic Phase 1)
2. Design (1 chunk, informal)
3. Gate: User approval
4. Implement with TDD
5. Gate: Code review
6. Merge
```

Example: New feature, refactoring, API changes.

**Full Flow (Score 12-15)**:
```
1. Clarify (Full Socratic protocol)
2. Research (codebase-historian)
3. Gate: Prior art acknowledged
4. Design (chunked, multi-phase)
5. Gate: Architecture approval
6. Implement with TDD
7. Gate: Code review + security audit
8. Gate: Integration testing
9. Staged rollout plan
10. Merge with monitoring
```

Example: Authentication system, data migration, payment integration.

---

## Two-Stage Review

Separate concerns: first spec compliance, then code quality.

### Stage 1: Spec Compliance

Does the implementation match what was requested?

```
[SPEC COMPLIANCE REVIEW]

Requirements from design:
- [ ] Requirement A: {status}
- [ ] Requirement B: {status}
- [ ] Requirement C: {status}

Deviations:
- {any differences from spec, with rationale}

Missing:
- {anything in spec not implemented}

Verdict: COMPLIANT | PARTIAL | NON-COMPLIANT
```

If NON-COMPLIANT: stop. Fix implementation or update spec with user approval.

### Stage 2: Code Quality

Is the compliant code well-written?

```
[CODE QUALITY REVIEW]

Structure:
- [ ] Functions < 50 lines
- [ ] Files < 800 lines
- [ ] Nesting depth <= 4
- [ ] Clear naming

Safety:
- [ ] Error handling present
- [ ] No hardcoded secrets
- [ ] Input validation (if applicable)
- [ ] SQL injection / XSS prevention (if applicable)

Style:
- [ ] Matches project conventions
- [ ] No debug statements
- [ ] No commented-out code

Verdict: APPROVED | CHANGES REQUESTED
```

### Why Two Stages?

Single-pass reviews conflate "wrong feature" with "poorly written feature." Two stages ensure:
1. We built the right thing (Stage 1)
2. We built it right (Stage 2)

---

## Failure Tracking (3-Strike Rule)

Track consecutive failures. Escalate or skip after threshold.

### Failure States

| Failures | Status | Action |
|----------|--------|--------|
| 0 | ACTIVE | Normal processing |
| 1 | RETRY | Attempt with adjustment |
| 2 | WARNING | Flag for attention, try different approach |
| 3 | BLOCKED | Stop attempts, escalate to user |

### Escalation Protocol

When task reaches BLOCKED or STALLED:

1. Stop attempting the task
2. Summarize what was tried and why it failed
3. Present to user with options:
   - Provide additional context/guidance
   - Try completely different approach
   - Defer task
   - Cancel task

Do not keep retrying the same failing approach. Three failures means the approach is wrong, not that persistence will help.

---

## Swarm Recipes

Pre-defined multi-agent workflows in `recipes/` directory. Each recipe encodes a complete pattern:

| Recipe | Complexity | Use Case |
|--------|------------|----------|
| `feature-implementation.yaml` | full | New features end-to-end |
| `bug-investigation.yaml` | standard | Bug diagnosis and fix |
| `refactor-safe.yaml` | standard | Code cleanup with safety |
| `security-audit.yaml` | full | Security hardening |

See `recipes/README.md` for full schema and customization guide.
