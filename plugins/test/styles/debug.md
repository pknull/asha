---
name: test-debug
description: Canary output style for the installer. Selecting it via /style should cause the assistant to prefix every reply with [TEST-STYLE-OK]. If you can read this text after symlinking, the output-styles scanner found the file.
---

# Debug style (canary)

Prefix every reply with exactly `[TEST-STYLE-OK]` on its own line, then proceed normally. No other behavioral change.

This style exists solely to verify that `~/.claude/output-styles/` resolves symlinked .md files. It has no production purpose.
