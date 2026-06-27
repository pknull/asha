# Symlink-Mount Installer

Flat, no-registry install model for the asha repo's primitives. This repo
is **not** a Claude plugin marketplace — the old three-file registration
chain (`marketplace.json` → `installed_plugins.json` → `enabledPlugins`)
was retired in favour of direct symlinks into the harness's scan directories.

As of 2026-04, the installer supports **multiple harnesses**: Claude Code,
OpenAI Codex CLI, and GitHub Copilot CLI — all launched through one `asha` dispatcher. Skills/agents/commands are harness-agnostic markdown;
each harness has its own scan layout, hook config format, and persona
mechanism.

## Architecture

```
~/life/asha/
├── install.sh            # thin shim → lib/install.sh (back-compat: --target, --bin)
├── uninstall.sh          # thin shim → lib/uninstall.sh (mirrors --target)
├── namespaces.json       # plugin → namespace map (harness-agnostic)
├── lib/
│   ├── install.sh        # install engine (sourced by install.sh shim AND bin/asha)
│   ├── uninstall.sh      # uninstall engine (sourced by uninstall.sh shim AND bin/asha)
│   └── portable.sh       # resolve_path (cross-platform readlink -f)
├── harnesses/
│   ├── claude.sh         # Claude Code install/uninstall logic
│   ├── codex.sh          # Codex CLI install/uninstall logic
│   └── copilot.sh        # Copilot CLI install/uninstall logic
├── bin/                  # installed via --bin
│   └── asha              # unified dispatcher + launcher
│                         #   grammar: asha [install|uninstall] [harness] [args…]
│                         #   shims ~/.local/bin/asha-{claude,codex,copilot} → asha
├── identity/             # persona content (single source of truth)
│   ├── asha-identity-system-prompt.md
│   └── identity-merge.sh # concatenates identity → ~/.cache/asha/instructions.md
└── plugins/<ns>/         # UNCHANGED, harness-agnostic
    ├── skills/<skill>/SKILL.md
    ├── agents/*.md
    ├── commands/*.md
    └── hooks/hooks.json
```

## Commands

```bash
# Primitives (skills/agents/commands/hooks)
./install.sh   --target {claude,codex,copilot,both,all}   [--only ns1,ns2]   [--dry-run] [--force] [--verbose]
./uninstall.sh --target {claude,codex,copilot,both,all}                       [--dry-run] [--verbose]

# Dispatcher + per-harness shims (the `asha` shell command)
./install.sh --bin {claude,codex,copilot,all} [--default {claude,codex,copilot}]

# Equivalent through the dispatcher itself (positional grammar):
asha install   {claude,codex,copilot,both,all} [flags]
asha uninstall {claude,codex,copilot,both,all} [flags]
```

`--target` defaults to `claude` (single-harness back-compat). `--bin all`
installs the `asha` dispatcher plus per-harness shims (`asha-claude`, `asha-codex`,
`asha-copilot`, each a relative symlink to `asha`) and records the bare-`asha` default
harness (`--default`, default `claude`) in `~/.asha/config.json`. The bin installer detects a
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
├── skills/<name>/SKILL.md           → generated Codex-clean skill from
│                                       plugins/<ns>/commands/<cmd>.md
│                                       (Claude-only frontmatter stripped)
├── agents/<ns>-<agent>.md           → plugins/<ns>/agents/<agent>.md   (best-effort;
│                                       Codex 0.125 multi-agent YAML schema unverified)
├── rules/asha.rules                  → native Codex execution-policy prompts
│                                       for coarse command approval fallback
└── config.toml
    └── # ===== asha:start ===== ... # ===== asha:end =====
        # ↑ fenced region with nested [[hooks.X.hooks]] handlers,
        #   each tagged "# asha:<ns>"
```

**No persona overlay.** The `asha codex` launch path injects persona via Codex's
CLI override, regenerating identity on the fly:

```bash
# bin/asha (codex branch)
identity-merge.sh ~/.cache/asha/instructions.md      # idempotent, ~50ms
exec codex -c "model_instructions_file=\"...\"" "$@"
```

Plain `codex` and `asha codex` share `~/.codex/`. The only behavioral
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
├── hooks/asha-guardrails.json       # PreToolUse guardrails → copilot-policy-adapter.sh (dedicated; user's hooks.json untouched)
└── mcp-config.json                  # NOT managed by Asha (Copilot reads it directly)
```

**Persona model: auto-loaded user-level instructions (no flag).** Copilot CLI
1.0.x has no `--instructions-file` flag, but it auto-loads instructions from any
dir named in `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` (scanning
`<dir>/.github/instructions/**/*.instructions.md`). The `asha copilot` launch path
(per-launch and scoped, so plain `copilot` stays persona-free — parity with
Claude's `--append-system-prompt-file` and Codex's `model_instructions_file`):

```bash
# bin/asha (copilot branch)
identity-merge.sh ~/.cache/asha/instructions-copilot.md   # merged persona (soul/voice/keeper/identity)
#  → wrapped as ~/.cache/asha/copilot-instr/.github/instructions/asha.instructions.md   (applyTo:"**")
operational-merge.sh → asha-operational.instructions.md   # operation.md + learnings hot tier (same dir)
export COPILOT_CUSTOM_INSTRUCTIONS_DIRS=~/.cache/asha/copilot-instr   # Copilot auto-loads both files
exec copilot "$@"
```

**Verified 2026-06-24 (CLI 1.0.63):** Copilot self-identifies as Asha and quotes
`operation.md` verbatim. This supersedes the earlier "doc-drop / manual
per-project" model, which wrongly assumed an injection *flag* was the only path
and missed the user-level instructions dir (no repo files are touched).

### Known limitations (Copilot harness, v1)

| Item | Status | Notes |
|---|---|---|
| Skills install | Working | `~/.copilot/skills/` confirmed scan path; verified by plant-and-probe 2026-05-09 |
| Custom agents | Working | `~/.copilot/agents/` confirmed; both `.md` and `.agent.md` extensions work; using bare `.md` |
| Capture hooks | **Not needed** | Asha capture (events.jsonl) was retired 2026-05-10. `/save` reads the host's native session log (`~/.copilot/session-state/<sid>/events.jsonl`) directly via `plugins/session/tools/jsonl_reader.py`, so no capture hooks. The `ASHA_COPILOT_HOOKS_FORCE=1` escape hatch was removed. |
| PreToolUse guardrails | **Installed** | `copilot_install_hooks()` writes a dedicated `~/.copilot/hooks/asha-guardrails.json` (Copilot loads every `*.json` there, so a user's own `hooks.json` is untouched) pointing at `plugins/session/hooks/handlers/copilot-policy-adapter.sh`, which bridges Copilot's hook contract to the shared `policy-guard.sh` + `block-secrets.sh`. PostToolUse/Stop hooks not wired on Copilot yet. **Enforcement verdict + live-test findings + the #2893 caveat: [docs/harness-enforcement.md](docs/harness-enforcement.md).** |
| Hook payload translator | Not needed | Same architecture change — synthesis reads native logs, not hook payloads. The `{toolName, toolArgs, toolResult}` → `{tool_name, tool_input, tool_response}` translator from the v1 plan is obsolete; `jsonl_reader.py` parses Copilot's `events.jsonl` directly into Asha's normalized event schema. |
| MCP config | Not managed | `~/.copilot/mcp-config.json` is read directly by Copilot; not touched by this installer (matches Claude/Codex which also don't manage MCP) |
| Persona auto-injection | **Automatic — per-launch** | Copilot CLI has no `--instructions-file` flag, but auto-loads user-level instructions. The `asha copilot` launch path regenerates `~/.cache/asha/instructions-copilot.md` (~47 KB merged identity), wraps it as `~/.cache/asha/copilot-instr/.github/instructions/asha.instructions.md` (`applyTo: "**"`), and exports `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` so Copilot loads it — scoped to the launch, so plain `copilot` stays persona-free (parity with Claude/Codex). Status/verification: [docs/harness-enforcement.md](docs/harness-enforcement.md). |
| `drift-check` | Not Copilot-aware | `bin/asha-drift-check.sh` ships in the repo; a Copilot-aware variant is a follow-up |

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

| Layer | Claude (`asha claude`) | Codex (`asha codex`) | Copilot (`asha copilot`) |
|---|---|---|---|
| Identity assertion | `--append-system-prompt-file identity/asha-identity-system-prompt.md` | `-c model_instructions_file="~/.cache/asha/instructions-codex.md"` (identity + operational layer, combined by wrapper at launch) | `COPILOT_CUSTOM_INSTRUCTIONS_DIRS=~/.cache/asha/copilot-instr` → auto-loads `.github/instructions/asha.instructions.md` (generated by wrapper at launch) |
| Lazy load (env signal) | `ASHA_PERSONA=1` triggers SessionStart hook | dispatcher-only (no overlay; plain `codex` skips persona) | dispatcher-only; persona auto-loaded via `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` at launch (plain `copilot` skips persona) |
| Identity merge | runtime: SessionStart hook reads `~/.asha/*` files | launch-time: `identity-merge.sh` writes `~/.cache/asha/instructions.md` (idempotent — only writes if sources changed) | launch-time: same `identity-merge.sh`, separate output path |

Codex has no `--append-system-prompt-file` equivalent at the CLI, and
its `model_instructions_file` config field accepts only a single file
path (no `[include]` directive). The dispatcher handles both gaps:
identity-merge.sh concatenates `~/.asha/{soul,voice,keeper,keeper-voice}.md`
plus `identity/asha-identity-system-prompt.md` into a single file, and
`-c model_instructions_file=...` injects it at launch. No on-disk
overlay; both `codex` and `asha codex` use the same `~/.codex/`.

## Drift check

`bin/asha-drift-check.sh` audits both harnesses. Exits 0 if clean,
1 on drift.

```bash
asha-drift-check.sh --target {claude,codex,all}    # default: all
```

Checks (paraphrased):

- **Repo:** installer scripts present, no `CLAUDE_PLUGIN_ROOT` placeholders in markdown
- **Claude:** no legacy enabledPlugins / installed_plugins.json / marketplaces; no dangling symlinks; tagged hook command paths exist
- **Codex:** no dangling symlinks; `config.toml` parses as TOML; tagged hook paths exist; native `rules/asha.rules` installed; overlay `instructions.md` fresher than its sources; inherit symlinks intact

Optionally schedule it via a systemd user timer or cron; append output to a
log of your choice (e.g. `drift-check.log`).

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
inside is generated from the source command MD with Claude-only frontmatter
(`argument-hint`, `allowed-tools`) stripped. Codex invokes via
`$<name>` (the namespacing collapsed from `/<ns>:<cmd>` is preserved in
the skill name).

Source command MDs gain a single `name: <ns>-<cmd>` line in their YAML
frontmatter, which is benign for Claude (which derives names from
filenames) and required for Codex skills.

### Codex hook events are a subset

Codex supports SessionStart, PreToolUse, PermissionRequest, PostToolUse,
PreCompact, PostCompact, UserPromptSubmit, Stop, SubagentStart, and
SubagentStop. Claude additionally has SessionEnd, Setup, etc. Hooks bound to
unsupported events are dropped during install with a warning. Asha emits the
current nested TOML shape (`[[hooks.Event]]` groups containing
`[[hooks.Event.hooks]]` handlers). The `output-styles` plugin's hooks are
skipped entirely (emit Claude-specific `hookSpecificOutput.additionalContext`
JSON).

### Codex native rules are installed as a coarse fallback

Because current Codex shell execution can bypass PreToolUse, Asha also writes a
dedicated `~/.codex/rules/asha.rules` file. This uses Codex's native
`prefix_rule()` execution-policy system for approval prompts on a narrow subset
of high-risk commands (`find /home`, `bfs /home`, `git reset --hard`,
force-push, protected-branch delete). This is **not** equivalent to Asha's full
regex policy engine: Codex rules are prefix-based and apply at permission /
sandbox boundaries, not every tool call. They are a native Codex safety net,
not a replacement for hook guardrails.

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
`-c model_instructions_file="..."` CLI flag on every `asha codex` launch —
no overlay, no copy, no drift. Identity is regenerated on launch via
`identity-merge.sh` (idempotent; only rewrites the cached file when sources
have changed).

Side effect: `asha codex` and plain `codex` share the same session
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
