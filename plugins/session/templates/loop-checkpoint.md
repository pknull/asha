---
loop: {{loop-id}}
checkpoint: {{checkpoint-number}}
timestamp: {{timestamp}}
---

# Checkpoint {{checkpoint-number}}

## Progress

- **Iterations completed**: {{iteration}} of {{max}}
- **Current state**: {{state-description}}
- **Files modified**: {{file-count}}

## Metrics

| Metric | Value |
|--------|-------|
| Success rate | {{success-rate}}% |
| Errors encountered | {{error-count}} |
| Time elapsed | {{duration}} |

## Changes This Period

{{changes-list}}

## Errors (if any)

{{errors-list}}

## Decision

**{{decision}}**: {{rationale}}

- [ ] CONTINUE — Progress detected, no blockers
- [ ] PAUSE — Anomaly detected, human review needed
- [ ] STOP — Completion or failure criteria met
