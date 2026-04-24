# test plugin — marketplace installer canary

This plugin exists solely to validate the symlink-mount installer across platforms. Every primitive is minimal and emits a unique sentinel string when resolved.

## Primitives

| Primitive | File | Sentinel |
|---|---|---|
| Skill | `skills/ping/SKILL.md` | `TEST-PING-OK` |
| Subagent | `agents/echo.md` | `TEST-ECHO-OK` |
| Slash command | `commands/ping.md` (`/test:ping`) | `TEST-PING-CMD-OK` |
| Stop hook | `hooks/stop.sh` | `touch /tmp/asha-marketplace-test-hook-fired` |
| Output style | `styles/debug.md` | `[TEST-STYLE-OK]` prefix on every reply |

## How to use

```
./install.sh --only test
# restart Claude Code
# then in a fresh session, verify each primitive:

# 1. Slash command
/test:ping                   → expect TEST-PING-CMD-OK

# 2. Skill
"run the test-ping skill"     → expect TEST-PING-OK

# 3. Subagent (via Task tool, spawned by Claude)
"spawn the test-echo agent"   → expect TEST-ECHO-OK

# 4. Hook (fires on session end)
# After ending the session:
ls -la /tmp/asha-marketplace-test-hook-fired

# 5. Output style
/output-styles:style test-debug
# replies should now be prefixed with [TEST-STYLE-OK]
```

## Cleanup

```
./uninstall.sh                # removes all marketplace symlinks incl. this one
rm -f /tmp/asha-marketplace-test-hook-fired
```

## Why it matters

The install topology under `~/.claude/` has subtle edge cases:

- user-level agent scanner may or may not descend into subdirectories
- `~/.claude/agents` and `~/.claude/hooks` may themselves be symlinks into dotfiles
- hook scripts often expect `$(dirname "$0")` to resolve to the source tree

This plugin is small enough to install/uninstall in seconds while exercising all five primitive types, so install strategy changes can be validated empirically instead of guessed.
