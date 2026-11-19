# AGENTS.md

Codex loads this file automatically. Treat `CLAUDE.md` as additional project
documentation, not as the active instruction surface.

## Project shape

Asha is a multi-harness agent toolkit. The same source corpus under `plugins/`
is rendered into native surfaces for Claude Code, OpenAI Codex, GitHub
Copilot CLI, and OpenCode stable v1. Do not assume Claude primitives are portable.

## Harness rule

Implement harness support at the real seam for that harness:

- Claude commands remain native slash commands.
- Codex commands render as skills, and Codex agents render as TOML custom
  agents.
- Copilot commands render as skills, and Copilot agents render as `.agent.md`.
- OpenCode commands and agents render as native Markdown under plural
  `commands/` and `agents/`; integration hooks live in `plugins/asha.js`.
- Codex has native hooks and execution rules. `PreToolUse` can deny supported
  simple Bash, `apply_patch`, and MCP calls, but it does not cover every shell
  path (`unified_exec` interception remains incomplete) or every tool. Do not
  describe it as a complete enforcement boundary.

When adding or changing a primitive, update the installer, doctor checks, and
tests for every affected harness.

## Asha is public

No tracked file names the owner's private projects: no campaign, vault,
character, book or private repository names, no citations of live play (what
happened on a beat or day, quotes from it), and no vault's directory layout as a
default. A plain beat or day number in a fixture names nothing and may stay.
Examples and test fixtures use invented names and text; a measurement cites "a
live log".
`tests/check-private-names.py` (validate-plugins Test 7, and a local git
pre-push hook) checks the tree against a local, never-committed name list and
skips where there is none. Install the hook once per clone with
`cp tests/hooks/pre-push .git/hooks/pre-push` (a copy: a symlink vanishes on an
old checkout), and push asha with `git push`: `jj git push` runs no git hooks.

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

For OpenCode-specific install changes, also check:

```bash
./bin/asha-drift-check.sh --target opencode
```

### Test environment gotchas

Run suites in a scrubbed environment. Each item below has cost a full run:

- Start from `env -i`, then pass back only what the suite needs: `PATH`
  including `~/.asdf/shims`, `~/.local/bin` and `~/bin` (jj and harness CLIs
  live there), `ASDF_DATA_DIR=~/.asdf` and `ASDF_NODEJS_VERSION` from
  `~/.tool-versions`, `LANG=C.UTF-8`. Without the asdf pair, `test-opencode.sh`
  fails its node checks under a sandbox HOME.
- Use a throwaway `HOME` outside `/tmp` (bwrap mounts a tmpfs over `/tmp`,
  failing `test_home_is_preserved_inside_containment`), with no group-writable
  ancestor (Control refuses them) and its own `.gitconfig` user.
- Keep `TMUX_TMPDIR` short, such as `/tmp/t1` mode 0700: a long path exceeds the
  Unix socket limit (`File name too long`).
- Never let `ASHA_HOME`, `ASHA_CONFIG`, `XDG_CONFIG_HOME` or `CODEX_HOME` reach
  `install.sh`, `uninstall.sh` or a harness adapter. Hub sessions export them,
  and the installer honours them independently of `HOME`, so an install meant for
  a sandbox rewrites the live install.
- Run the baseline on an unchanged checkout in its own workspace, never on the
  working copy you are editing.
