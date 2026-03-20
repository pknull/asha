# ReAct + Stop Conditions Template

Use for autonomous agents (Claude Code headless, Devin, AutoGPT). Runaway loops and scope explosion are biggest credit killers — stop conditions not optional.

## Structure

```
Objective:
[Single unambiguous goal]

Starting State:
[Current file structure / environment]

Target State:
[What should exist when done]

Allowed Actions:
- [Specific permitted actions]
- Install only packages in [requirements.txt / package.json]

Forbidden Actions:
- Do NOT modify files outside [scope]
- Do NOT run dev server or deploy
- Do NOT push to git
- Do NOT delete without showing diff
- Do NOT make architecture decisions without approval

Stop Conditions:
Pause and ask when:
- File would be permanently deleted
- New external service needs integration
- Two valid paths exist (architecture decision)
- Error not resolved in 2 attempts
- Changes needed outside stated scope

Checkpoints:
After each step output: [what was completed]
At end, output summary of every file changed.
```

## Critical Elements

- **Objective** must be single and unambiguous
- **Forbidden Actions** prevents autonomous wrong decisions
- **Stop Conditions** required for any irreversible action
- **Checkpoints** let you track progress
