---
loop: {{loop-id}}
status: {{final-status}}
completed: {{timestamp}}
duration: {{total-duration}}
---

# Loop Completion: {{loop-id}}

## Outcome

- **Status**: {{final-status}}
- **Objective**: {{objective}}
- **Iterations**: {{iterations-run}} of {{max-iterations}}
- **Duration**: {{total-duration}}

## Accomplishments

{{accomplishments-list}}

## Remaining (if partial)

{{remaining-list}}

## Error Summary

| Error | Count | Resolution |
|-------|-------|------------|
{{error-summary}}

## Artifacts

- **Checkpoints**: Work/loops/{{loop-id}}/checkpoint-*.md
- **Files modified**: {{file-count}}
- **Rollback point**: {{rollback-ref}}

## Lessons Learned

{{lessons}}
