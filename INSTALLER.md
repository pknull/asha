# Symlink-Mount Installer

Flat, no-registry install model for the asha repo's primitives. This repo
is **not** a Claude plugin marketplace — the old three-file registration
chain (`marketplace.json` → `installed_plugins.json` → `enabledPlugins`)
was retired in favour of direct symlinks into the harness's scan directories.

As of 2026-04, the installer supports **multiple harnesses**: Claude Code
and OpenAI Codex CLI. Skills/agents/commands are harness-agnostic markdown;
each harness has its own scan layout, hook config format, and persona
mechanism.

## Architecture

```
~/life/asha/
├── install.sh            # multi-harness dispatcher (--target, --bin)
├── uninstall.sh          # mirrors --target
├── namespaces.json       # plugin → namespace map (harness-agnostic)
├── harnesses/
│   ├── claude.sh         # Claude Code install/uninstall logic
│   └── codex.sh          # Codex CLI install/uninstall logic + overlay
├── bin/                  # harness-aware wrappers (installed via --bin)
│   ├── asha-claude       # ASHA_PERSONA=1 + --append-system-prompt-file
│   └── asha-codex        # CODEX_HOME=~/.codex-asha (persona overlay)
├── identity/             # persona content (single source of truth)
│   ├── asha-identity-system-prompt.md
│   └── identity-merge.sh # concatenates identity → ~/.codex-asha/instructions.md
└── plugins/<ns>/         # UNCHANGED, harness-agnostic
    ├── skills/<skill>/SKILL.md
    ├── agents/*.md
    ├── commands/*.md
    └── hooks/hooks.json
```

## Commands

```bash
# Primitives (skills/agents/commands/hooks)
./install.sh   --target {claude,codex,both}   [--only ns1,ns2]   [--dry-run] [--force] [--verbose]
./uninstall.sh --target {claude,codex,both}                       [--dry-run] [--verbose]

# Wrappers (the `asha` shell command)
./install.sh --bin {claude,codex,all} [--default {claude,codex}]
```

`--target` defaults to `claude` (single-harness back-compat). `--bin all`
installs both wrappers and points the bare `asha` command at the harness
named by `--default` (default: `claude`). The bin installer detects a
legacy `~/bin/asha` and tells you how to retire it — it never touches
your dotfiles repo.

`install.sh` is idempotent. Re-running skips already-correct state and
refuses mismatched symlinks unless `--force`. `uninstall.sh` is also
idempotent.

## Per-harness install layout

### Claude Code (`--target claude`)

```
~/.claude/
├── skills/<ns>-<skill>/             → plugins/<ns>/skills/<skill>/
├── agents/<ns>/<agent>.md           → plugins/<ns>/agents/<agent>.md
├── commands/<ns>/<cmd>.md           → plugins/<ns>/commands/<cmd>.md
├── output-styles/<ns>-<style>.md    → plugins/<ns>/styles/<style>.md
└── settings.json
    └── hooks.<Lifecycle>[].hooks[]  # tagged "source": "asha:<ns>"
                                     # command = abs path into plugins/<ns>/hooks/
```

### Codex CLI (`--target codex`)

```
~/.codex/
├── skills/<name>/                   → plugins/<ns>/skills/<dir>/   (whole-dir symlink)
│                                       — name from SKILL.md `name:` field, falls back
│                                         to <ns>-<dir> when frontmatter has no name
├── skills/<name>/SKILL.md           → plugins/<ns>/commands/<cmd>.md   (single-file
│                                       symlink; the dir is a real dir, the SKILL.md
│                                       inside is the symlink to the source command MD)
├── agents/<ns>-<agent>.md           → plugins/<ns>/agents/<agent>.md   (best-effort;
│                                       Codex 0.125 multi-agent YAML schema unverified)
└── config.toml
    └── # ===== asha:start ===== ... # ===== asha:end =====
        # ↑ fenced region with [[hooks.X]] arrays, each tagged "# asha:<ns>"
```

**No persona overlay.** The `asha-codex` wrapper injects persona via Codex's
CLI override, regenerating identity on the fly:

```bash
# bin/asha-codex
identity-merge.sh ~/.cache/asha/instructions.md      # idempotent, ~50ms
exec codex -c "model_instructions_file=\"...\"" "$@"
```

Plain `codex` and `asha-codex` share `~/.codex/`. The only behavioral
difference is the `-c` flag at launch — skills, prompts, agents, hooks,
MCP, projects, sessions are single-instance.

### GitHub Copilot CLI (`--target copilot`)

```
~/.copilot/
├── skills/                          # symlinks (real skills) + dirs (command-skills)
│   ├── <plugin>-<skill>/            # → plugins/<plugin>/skills/<skill>/
│   └── <ns>-<command>/SKILL.md      # generated from commands/*.md (frontmatter stripped)
├── agents/                          # symlinks
│   └── <plugin>-<agent>.md          # → plugins/<plugin>/agents/<agent>.md
├── hooks/hooks.json                 # NOT WRITTEN by default — see "Known limitations"
└── mcp-config.json                  # NOT managed by Asha (Copilot reads it directly)
```

**Persona model: doc-drop, not flag.** Copilot CLI 1.0.x has no `--instructions-file`
flag (verified empirically against `copilot --help` 2026-05-09). It auto-loads
project-scope `AGENTS.md` and `.github/copilot-instructions.md`. The `asha-copilot`
wrapper:

```bash
# bin/asha-copilot
identity-merge.sh ~/.cache/asha/instructions-copilot.md   # idempotent, regenerates merged identity
echo "[asha-copilot] merged identity available at: ..."   # tells user where to find it
exec copilot "$@"                                         # plain exec, no persona flag
```

To load Asha persona for a project: run `copilot init` once to generate
`.github/copilot-instructions.md`, then concatenate the merged identity into it
(or append to project `AGENTS.md`). This is a deliberate scope limit — auto-injection
into every project's instructions would silently mutate user files.

### Known limitations (Copilot harness, v1)

| Item | Status | Notes |
|---|---|---|
| Skills install | Working | `~/.copilot/skills/` confirmed scan path; verified by plant-and-probe 2026-05-09 |
| Custom agents | Working | `~/.copilot/agents/` confirmed; both `.md` and `.agent.md` extensions work; using bare `.md` |
| Hooks install | **Deferred — blocked on Copilot** | Empirically verified 2026-05-10 against Copilot CLI v1.0.44: hooks fire from `~/.copilot/hooks/hooks.json` (user-scope, the architect's default was right) BUT Copilot does NOT pipe JSON payload data to hook scripts. fd 0 (stdin) is a socket but never written to; no `COPILOT_TOOL_NAME`/`COPILOT_PROMPT`/etc. env vars exposed. Hooks would fire as empty heartbeats — session-watching captures timestamps with no tool/prompt content. Worse than no hooks because it implies you have data when you don't. Wait for v1.1+ to ship the documented payload data flow, then enable. `ASHA_COPILOT_HOOKS_FORCE=1` escape hatch installs anyway if you want hollow events. |
| Hook payload translator | Not built | Even when Copilot ships payload data, Claude's `{tool_name, tool_input, tool_response}` differs from documented Copilot shape `{toolName, toolArgs, toolResult: {resultType, textResultForLlm}}`. Translator layer needed at install time. |
| MCP config | Not managed | `~/.copilot/mcp-config.json` is read directly by Copilot; not touched by this installer (matches Claude/Codex which also don't manage MCP) |
| Persona auto-injection | Not built | Wrapper only regenerates the merged identity file and points the user at it. AGENTS.md / `.github/copilot-instructions.md` integration is per-project user action |
| `drift-check` | Not Copilot-aware | `~/life/bin/asha-drift-check.sh` lives outside this repo; a Copilot-aware variant is a follow-up |

## Namespaces

`namespaces.json` maps each plugin directory to the namespace used for
slash commands and primitive prefixes. Almost all entries are 1:1; two
exceptions preserve legacy plugin names:

| Directory | Namespace |
|---|---|
| `plugins/panel/` | `panel-system` |
| `plugins/schedule/` | `scheduler` |

So `/panel-system:panel` (Claude) and the prompt `panel-system-panel.md`
(Codex) resolve even though the source dirs are shorter.

## Persona model

| Layer | Claude wrapper (`asha-claude`) | Codex wrapper (`asha-codex`) | Copilot wrapper (`asha-copilot`) |
|---|---|---|---|
| Identity assertion | `--append-system-prompt-file identity/asha-identity-system-prompt.md` | `-c model_instructions_file="~/.cache/asha/instructions.md"` (generated by wrapper at launch) | none — Copilot CLI 1.0.x has no instructions-file flag; doc-drop only |
| Lazy load (env signal) | `ASHA_PERSONA=1` triggers SessionStart hook | wrapper-only (no overlay; plain `codex` skips persona) | wrapper-only; persona regenerated to `~/.cache/asha/instructions-copilot.md`, user pastes into AGENTS.md / `.github/copilot-instructions.md` per project |
| Identity merge | runtime: SessionStart hook reads `~/.asha/*` files | wrapper-time: `identity-merge.sh` writes `~/.cache/asha/instructions.md` (idempotent — only writes if sources changed) | wrapper-time: same `identity-merge.sh`, separate output path |

Codex has no `--append-system-prompt-file` equivalent at the CLI, and
its `model_instructions_file` config field accepts only a single file
path (no `[include]` directive). The wrapper handles both gaps:
identity-merge.sh concatenates `~/.asha/{soul,voice,keeper,keeper-voice}.md`
plus `identity/asha-identity-system-prompt.md` into a single file, and
`-c model_instructions_file=...` injects it at launch. No on-disk
overlay; both `codex` and `asha-codex` use the same `~/.codex/`.

## Drift check

`~/life/bin/asha-drift-check.sh` audits both harnesses. Exits 0 if clean,
1 on drift.

```bash
asha-drift-check.sh --target {claude,codex,all}    # default: all
```

Checks (paraphrased):

- **Repo:** installer scripts present, no `CLAUDE_PLUGIN_ROOT` placeholders in markdown
- **Claude:** no legacy enabledPlugins / installed_plugins.json / marketplaces; no dangling symlinks; tagged hook command paths exist
- **Codex:** no dangling symlinks; `config.toml` parses as TOML; tagged hook paths exist; overlay `instructions.md` fresher than its sources; overlay `config.toml` fresher than `~/.codex/config.toml`; inherit symlinks intact

Scheduled via `asha-drift-check.timer` (systemd user timer, persistent
across reboots). Output appends to `~/life/asha/drift-check.log`.

## Backups

Every mutating operation backs up the affected file with a timestamped
suffix before editing:

- `~/.claude/settings.json` → `.bak-<YYYYMMDD-HHMMSS>`
- `~/.codex/config.toml` → `.bak-<YYYYMMDD-HHMMSS>`

## Test plugin

`plugins/test/` ships one of every primitive emitting a unique sentinel
string. Smoke test:

```bash
./install.sh --only test --target both
# restart Claude Code / Codex CLI
/test:ping            # Claude — expect TEST-PING-CMD-OK
test-ping             # Codex prompt — same expectation
```

## Known limitations

### Codex 0.125 removed the user prompts surface

Asha originally installed slash commands as `~/.codex/prompts/<ns>-<cmd>.md`.
Direct binary probe of Codex 0.125 (`@openai/codex-linux-x64`) shows the
binary has zero references to `~/.codex/prompts/`, `$CODEX_HOME/prompts`,
or any prompts-as-files discovery path — only MCP server-protocol method
strings (`prompts/list`, `prompts/get`) which are unrelated. The user
file surface was removed entirely in 0.125.

Workaround: each command MD is installed as a single-file Codex skill.
The directory `~/.codex/skills/<name>/` is a real dir; the `SKILL.md`
inside is a symlink to the source command MD. Codex invokes via
`$<name>` (the namespacing collapsed from `/<ns>:<cmd>` is preserved in
the skill name).

Source command MDs gain a single `name: <ns>-<cmd>` line in their YAML
frontmatter, which is benign for Claude (which derives names from
filenames) and required for Codex skills.

### Codex hook events are a subset

Codex 0.125 supports SessionStart, PreToolUse, PostToolUse, Stop,
UserPromptSubmit, PermissionRequest. Claude additionally has SessionEnd,
Setup, etc. Hooks bound to unsupported events are dropped during install
with a warning. The `output-styles` plugin's hooks are skipped entirely
(emit Claude-specific `hookSpecificOutput.additionalContext` JSON).

### Codex multi-agent YAML schema is unverified

The 4 specifically-translated agents from the plan (reviewer, architect,
tdd, partner-sentiment) plus all other agent markdown are symlinked into
`~/.codex/agents/<ns>-<agent>.md`. Codex 0.125 may or may not discover
them as multi-agent definitions — the schema is poorly documented at
this version. If translation is needed, the YAML format can be retrofitted
without changing the install layout.

### Output styles plugin is Claude-only

The `/style` command and 8 output-style files don't port to Codex (no
equivalent feature). The `output-styles` plugin is in the codex install's
skip list.

### Persona overlay was eliminated in Step 7-revised

Earlier versions used `~/.codex-asha/` as a parallel CODEX_HOME, with a
generated config.toml that copied the user's main config plus a
`model_instructions_file` line. This drifted whenever you edited the main
config without reinstalling. Step 7-revised replaced it with a
`-c model_instructions_file="..."` CLI flag on every `asha-codex` launch —
no overlay, no copy, no drift. Identity is regenerated on launch via
`identity-merge.sh` (idempotent; only rewrites the cached file when sources
have changed).

Side effect: `asha-codex` and plain `codex` share the same session
history under `~/.codex/sessions/`. If you want them visually separated,
you could pass `-c sessions_path="..."` from the wrapper (untested).

### Plugin-skill / command-skill name collision

If a plugin's `skills/<dir>/SKILL.md` declares the same `name:` as one
of its commands, the plugin skill wins (it's the more substantive
artifact). The colliding command is silently skipped during install.
The `test` plugin is the only known case: `name: test-ping` appears
in both `skills/ping/SKILL.md` and `commands/ping.md`; the plugin
skill's content prevails.

### Dotfiles-backed `agents/` and `hooks/`

`~/.claude/agents` and `~/.claude/hooks` may themselves be symlinks into
a separately-tracked dotfiles repo. The installer writes per-plugin
subdirectories there (`~/.claude/agents/<ns>/`). Either add
`claude/.claude/agents/*/` to the dotfiles `.gitignore`, or break the
dotfiles symlink and let `~/.claude/agents` be a real directory with
per-file symlinks into dotfiles for the user's curated list.

### Output styles are plugin-local in Claude

The `/output-styles:style` command reads from its *own plugin's* `styles/`
directory only. Cross-plugin output styles via the symlink-mount model
are visible in `~/.claude/output-styles/` but no scanner sees them.
Either teach `/style` to scan `~/.claude/output-styles/`, or move all
styles into the `output-styles` plugin's `styles/` dir.
