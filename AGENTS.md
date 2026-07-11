# AGENTS.md

Codex loads this file automatically. Treat `CLAUDE.md` as additional project
documentation, not as the active instruction surface.

## Project shape

Asha is a multi-harness agent toolkit. The same source corpus under `plugins/`
is rendered into native surfaces for Claude Code, OpenAI Codex, and GitHub
Copilot CLI. Do not assume Claude primitives are portable.

## Harness rule

Implement harness support at the real seam for that harness:

- Claude commands remain native slash commands.
- Codex commands render as skills, and Codex agents render as TOML custom
  agents.
- Copilot commands render as skills, and Copilot agents render as `.agent.md`.
- Codex has native hooks and execution rules. `PreToolUse` can deny supported
  simple Bash, `apply_patch`, and MCP calls, but it does not cover every shell
  path (`unified_exec` interception remains incomplete) or every tool. Do not
  describe it as a complete enforcement boundary.

When adding or changing a primitive, update the installer, doctor checks, and
tests for every affected harness.

## Verification

Run the narrow relevant tests first. Before considering cross-harness installer
work complete, run:

```bash
./tests/run-tests.sh
```

For Codex-specific install changes, also check:

```bash
./bin/asha-drift-check.sh --target codex
```
