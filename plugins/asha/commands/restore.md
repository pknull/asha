---
description: "Re-enable Memory logging after silence mode"
allowed-tools: ["Bash", "Read"]
---

# Restore Memory Logging

Re-enables Memory logging after silence mode.

## What This Does

- **Restores hooks**: User prompt and tool usage logging resume
- **Restores session watching**: Automatic operation tracking to `current-session.md`
- **Restores Memory synthesis**: `/save` will have full session context
- **Preserves context markers**: Does NOT remove `project-active` or `rp-active` markers

## Status After Restore

After restore, normal marker hierarchy applies:

- If `rp-active` exists: RP logging mode (hooks disabled, session watching may vary)
- If `project-active` exists: Project context loaded, normal logging
- If no markers: Default logging behavior

## Statusline Update

Statusline will reflect active context after silence removed:

- `[ProjectName]` if project active
- `[🎭 RP]` if RP active
- Normal prompt if no markers

---

**Execute**: Invoke workspace-manager skill.

Command: `silence off`

ARGUMENTS: {command_args}
