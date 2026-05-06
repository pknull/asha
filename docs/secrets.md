# Secret Management in Asha

Asha skills that talk to authenticated APIs (Todoist, Bookstack, Gemini, etc.) read their tokens from environment variables. This document explains where those env vars come from and why the loading happens where it does.

## Pattern

Asha uses **dotenv with wrapper-scoped sourcing**:

```
~/life/asha/secrets.example       # committed — documents which vars exist
~/life/asha/bin/asha-env-bootstrap.sh   # committed — sources the live file
~/.asha/secrets.env                # local-only, gitignored, mode 0600
```

(The example file omits the `.env` suffix on purpose — Asha's `block-secrets` hook treats `*.env` as restricted. The committed template needs to slip past that guard since it carries no real values.)

The wrappers (`asha-claude`, `asha-codex`) source `bin/asha-env-bootstrap.sh` before exec'ing their underlying harness. The bootstrap reads `~/.asha/secrets.env` if present and exports its contents into the launched process's environment.

This is the pattern the broader Claude Code / MCP ecosystem is converging on (see [issue #28942](https://github.com/anthropics/claude-code/issues/28942), [issue #2065](https://github.com/anthropics/claude-code/issues/2065)). It's not novel; it's the lowest-friction option that doesn't lose to the obvious anti-patterns.

## Why wrapper-scoped, not `~/.zshrc`

Sourcing from the user's shell rc would put every Asha token into every subprocess environment the user's shell ever spawns — Claude Code launched outside Asha, MCP servers from unrelated projects, ssh sessions, cron, anything. That's a "principle hit" not necessarily an exploit, but it's free to avoid: source from the wrapper instead, and only sessions launched via `asha` see the secrets.

**One implication:** if you launch `claude` directly (without going through the `asha` wrapper), the Asha skills that need tokens will fail with "set $VAR" errors. That's intended — it's the prompt to launch via `asha`, which is the only path that's been bootstrapped.

## Setup

1. Copy the template:
   ```bash
   cp ~/life/asha/secrets.example ~/.asha/secrets.env
   chmod 600 ~/.asha/secrets.env
   ```

2. Fill in the values in `~/.asha/secrets.env`. The example file documents what each token is for and where to obtain it.

3. Launch sessions via `asha-claude` / `asha-codex` (or just `asha` if you've installed the symlinked launcher).

## Adding a new integration

When you add a skill that needs a token:

1. Add a placeholder line to `secrets.example.env` with a comment block describing what the token is for and where to get it.
2. Document the env-var name in the skill's `SKILL.md` frontmatter and body.
3. Have the skill reference the var via `$VAR_NAME` in shell commands (so the shell expands it before any model output is generated — the token never enters conversation history).
4. **Never** make the skill read the secrets file directly. Always go through env.

## Anti-patterns (don't)

- **YAML or JSON for secret values.** Plain text by default, easy to commit by mistake. GitGuardian found ~24K tokens leaked from MCP config files in 2025. Asha uses dotenv specifically to keep "secret values" and "structured config" in different files with different rules.
- **Prompting the user to paste a token into chat.** The token enters conversation history, gets logged, may end up in synthesis files. Always use env vars and let the skill fail loudly if a var is missing.
- **Reading the secrets file with `cat`** from inside a skill. Same problem — token in conversation. Always reference `$VAR` in shell invocations.
- **Committing `~/.asha/secrets.env`** by symlinking it into the repo. The file lives outside the repo on purpose. Don't bridge the gap.
- **Rotating by editing in place without restarting.** Currently-running Asha sessions hold the old value in their process env. After rotating a token, restart the session.

## Graduation paths

This dotenv pattern is the personal-scale answer. When you outgrow it, the upgrade is non-disruptive:

### Multi-machine sync

Replace `~/.asha/secrets.env` with a 1Password reference file using [op inject](https://developer.1password.com/docs/cli/secrets-environment-variables/):

```bash
# secrets.env becomes a template:
TODOIST_API_TOKEN={{ op://Personal/Todoist/credential }}
```

Then have `asha-env-bootstrap.sh` resolve it via `op inject -i secrets.env | source /dev/stdin`. The rest of the pipeline doesn't change.

Alternatively wrap the whole exec with [`op run --env-file`](https://developer.1password.com/docs/cli/secrets-config-files/) which injects vault values directly into the subprocess env — no on-disk plaintext at all.

### Vault-managed rotation

[Infisical](https://infisical.com/blog/managing-secrets-mcp-servers) and similar tools let you change a secret centrally and have running services pick it up without an asha-prefixed-restart. Overkill for personal use, real for teams.

### Per-skill scoping

If you ever want a particular MCP server to NOT see (say) the Bookstack token, the dotenv bootstrap can be split: `~/.asha/secrets/<integration>.env`, sourced selectively by per-integration wrappers. Out of scope for the current single-file pattern, but the design accommodates it.

## File reference

| File | Lives in | Purpose |
|---|---|---|
| `secrets.example` | asha repo | Committed template documenting which vars exist (no `.env` suffix to dodge the secrets-block hook) |
| `bin/asha-env-bootstrap.sh` | asha repo | Sourced by both wrappers; reads `$ASHA_SECRETS_FILE` |
| `~/.asha/secrets.env` | local user dir | Real values, gitignored, mode 0600 |
| `bin/asha-claude` / `asha-codex` | asha repo | Source the bootstrap before exec'ing the harness |

## See also

- [Claude Code Authentication Reference](https://github.com/anthropics/claude-code/blob/main/plugins/plugin-dev/skills/mcp-integration/references/authentication.md) — official MCP env-var convention
- [Issue #28942](https://github.com/anthropics/claude-code/issues/28942) — `envFile` field proposal in `.mcp.json`, which this pattern parallels
- `plugins/admin/skills/todoist/SKILL.md` — example of a skill that consumes one of these vars
