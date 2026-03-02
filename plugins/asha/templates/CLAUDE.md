# Project Context

This project uses the **Asha** framework for session coordination and memory persistence.

## Quick Reference

**Framework:** Asha plugin provides CORE.md via session-start hook.

**Memory Bank:** Project context stored in `Memory/*.md` files.

## Commands

| Command | Purpose |
|---------|---------|
| `/asha:save` | Save session context to Memory Bank, archive session |
| `/asha:init` | Initialize Asha in new projects |

## Memory Files

| File | Purpose | Update Frequency |
|------|---------|------------------|
| `Memory/activeContext.md` | Current project state, recent activities | Every session |
| `Memory/projectbrief.md` | Project scope, objectives, constraints | Rarely |
| `~/.asha/soul.md` | Identity, values, nature (cross-project) | Rarely |
| `~/.asha/voice.md` | Voice, tone, constraints (cross-project) | When calibrated |
| `~/.asha/learnings.md` | Cross-project insights from experience | Additive via /save |
| `Memory/workflowProtocols.md` | Validated patterns, anti-patterns | When patterns discovered |
| `Memory/techEnvironment.md` | Technical stack, conventions | When stack changes |

## Session Workflow

1. **Start:** Read `Memory/activeContext.md` for context
2. **Work:** Operations logged automatically via hooks
3. **End:** Run `/asha:save` to synthesize and persist learnings

## Code Style

- Follow existing patterns in the codebase
- Use authority markers when uncertain: `[Inference]`, `[Speculation]`, `[Unverified]`
- Reference code locations as `file_path:line_number`
