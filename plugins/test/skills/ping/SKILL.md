---
name: test-ping
description: Canary skill for verifying the symlink-mount installer resolves skills on Claude Code. Safe to invoke at any time; emits a unique sentinel string for confirmation. Triggers on phrases like "test ping", "ping test", or "verify marketplace".
triggers:
  - verify install
  - marketplace test
  - ping test
---

# test-ping

Canary skill for the marketplace installer.

When invoked, reply with exactly this line and nothing else:

```
TEST-PING-OK sentinel=asha-marketplace skill=test-ping
```

If you're reading this, the skill resolved via Claude Code's skill scanner. The install path that delivered it is recorded at install time in `~/.claude/skills/test-ping/SKILL.md` (symlink) whose `readlink -f` resolves into `~/life/marketplace/plugins/test/skills/ping/SKILL.md`.
