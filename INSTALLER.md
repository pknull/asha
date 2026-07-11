# Symlink-Mount Installer

Flat direct-mount install model for the asha repo's primitives. This repo
does not currently package its local install path as a Codex plugin; it uses
symlinks plus generated harness-native artifacts. Codex itself supports native
plugins and marketplaces, which remain a separate future distribution path.

The installer supports four first-class harnesses: Claude Code, OpenAI Codex
CLI, GitHub Copilot CLI, and OpenCode. All launch through one `asha` dispatcher.
Source skills, agents, and commands remain shared Markdown; adapters render each
harness's native form.

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
│   ├── copilot.sh        # Copilot CLI install/uninstall logic
│   ├── opencode.sh       # OpenCode install/uninstall logic
│   ├── registry.sh       # canonical harness catalogue
│   └── generated-artifacts.sh # ownership manifests + collision safety
├── bin/                  # installed via --bin
│   └── asha              # unified dispatcher + launcher
│                         #   grammar: asha [install|uninstall] [harness] [args…]
│                         #   shims ~/.local/bin/asha-{claude,codex,copilot,opencode} → asha
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
./install.sh   --target {claude,codex,copilot,opencode,both,all} [--only ns1,ns2] [--dry-run] [--force] [--verbose]
./uninstall.sh --target {claude,codex,copilot,opencode,both,all}                   [--dry-run] [--verbose]

# Dispatcher + per-harness shims (the `asha` shell command)
./install.sh --bin {claude,codex,copilot,opencode,all} [--default {claude,codex,copilot,opencode}]

# Equivalent through the dispatcher itself (positional grammar):
asha install   {claude,codex,copilot,opencode,both,all} [flags]
asha uninstall {claude,codex,copilot,opencode,both,all} [flags]
```

`--target` defaults to `claude` (single-harness back-compat). `--bin all`
installs the `asha` dispatcher plus per-harness shims (`asha-claude`, `asha-codex`,
`asha-copilot`, `asha-opencode`, each a relative symlink to `asha`) and records the bare-`asha` default
harness (`--default`, default `claude`) in `~/.asha/config.json`. The bin installer detects a
legacy `~/bin/asha` and tells you how to retire it — it never touches
your dotfiles repo.

`install.sh` is idempotent. Re-running skips already-correct state and
refuses mismatched symlinks unless `--force`. `uninstall.sh` is also
idempotent.

### One-time migration from pre-manifest installs

This release adds ownership manifests for generated Codex, Copilot, and
OpenCode files. Existing Codex/Copilot generated files cannot be distinguished
from foreign files safely until adopted. Run the relevant install once with
`--force`:

```bash
asha install codex --force
asha install copilot --force
```

The renderer then records deterministic hashes under
`~/.asha/install-manifests/`. A direct uninstall that detects legacy generated
files without a manifest stops with this instruction instead of claiming a
successful removal whilst leaving live workflows behind.

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
├── agents/<ns>-<agent>.toml         # generated Codex custom-agent TOML
│                                       from plugins/<ns>/agents/<agent>.md
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
difference is the `-c` flag at launch; skills, custom agents, hooks, rules,
MCP configuration, projects, and sessions are single-instance.

### GitHub Copilot CLI (`--target copilot`)

```
~/.copilot/
├── skills/                          # symlinks (real skills) + dirs (command-skills)
│   ├── <plugin>-<skill>/            # → plugins/<plugin>/skills/<skill>/
│   └── <ns>-<command>/SKILL.md      # generated from commands/*.md (frontmatter stripped)
├── agents/                          # generated Copilot agent files
│   └── <plugin>-<agent>.agent.md    # from plugins/<plugin>/agents/<agent>.md
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
| `drift-check` | **Copilot-aware** | `asha doctor [copilot]` (front door for `bin/asha-drift-check.sh`) audits symlinks, command-skill freshness, and guardrails content; `--fix` self-heals. |
| Team distribution | **Additive path** | `asha build copilot` packages namespaces as native Copilot plugins (marketplace + `enabledPlugins` pinning); see [docs/distribution-copilot.md](docs/distribution-copilot.md). Repo onboarding: `asha init-repo`. |

### OpenCode (`--target opencode`)

```
~/.config/opencode/
├── skills/<declared-name>/          → plugins/<ns>/skills/<skill>/
├── command/<command>.md             # generated native slash command
├── agent/<ns>-<agent>.md            # generated native subagent (`mode: subagent`)
└── plugin/asha-guardrails.js        # native tool.execute.before bridge
```

The singular `command/`, `agent/`, and `plugin/` names are OpenCode's native
config layout; `skills/` remains plural. The adapter was plant-tested against
OpenCode 1.0.78 with `opencode agent list`.

`asha opencode` appends the merged identity and operational file through
`OPENCODE_CONFIG_CONTENT.instructions`. This preserves normal global/project
config and any user-supplied `OPENCODE_CONFIG_DIR`; plain `opencode` remains
persona-free. Manual save reads OpenCode's native directory storage under
`~/.local/share/opencode/storage`. Automatic SessionEnd save is unsupported.

Generated commands, agents, and the guardrail plugin are recorded in
`~/.asha/install-manifests/opencode.json`. Install refuses foreign collisions;
uninstall removes only byte-identical managed files and preserves modified ones.

## Namespaces

`namespaces.json` maps each plugin directory to the namespace used for
slash commands and primitive prefixes. Almost all entries are 1:1; two
exceptions preserve legacy plugin names:

| Directory | Namespace |
|---|---|
| `plugins/panel/` | `panel-system` |

So `/panel-system:panel` (Claude) and the prompt `panel-system-panel.md`
(Codex) resolve even though the source dirs are shorter.

## Persona model

| Layer | Claude | Codex | Copilot | OpenCode |
|---|---|---|---|---|
| Identity assertion | `--append-system-prompt-file` | `model_instructions_file` | `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` | `OPENCODE_CONFIG_CONTENT.instructions` |
| Scope | wrapper only | wrapper only | wrapper only | wrapper only |
| Identity merge | SessionStart + launch file | launch-time combined file | launch-time instruction directory | launch-time combined file |

Codex has no `--append-system-prompt-file` equivalent at the CLI, and
its `model_instructions_file` config field accepts only a single file
path (no `[include]` directive). The dispatcher handles both gaps:
identity-merge.sh concatenates `~/.asha/{soul,voice,keeper,keeper-voice}.md`
plus `identity/asha-identity-system-prompt.md` into a single file, and
`-c model_instructions_file=...` injects it at launch. No on-disk
overlay; both `codex` and `asha codex` use the same `~/.codex/`.

## Drift check / doctor

`asha doctor` is the front door; `bin/asha-drift-check.sh` remains at its
path for cron/systemd users. Exits 0 if clean, 1 on drift, 2 on usage error.

```bash
asha doctor [claude|codex|copilot|opencode|all] [--fix]     # default: all
asha-drift-check.sh --target {claude,codex,copilot,opencode,all} # same engine
```

(`asha claude doctor` still reaches Claude Code's own native doctor —
launch forwarding is unchanged.)

Checks (paraphrased):

- **Repo:** installer scripts present, no `CLAUDE_PLUGIN_ROOT` placeholders in markdown
- **Claude:** no legacy enabledPlugins / installed_plugins.json / marketplaces; no dangling symlinks; tagged hook command paths exist
- **Codex:** no dangling symlinks; `config.toml` parses as TOML; tagged hook paths exist; native `rules/asha.rules` installed; overlay `instructions.md` fresher than its sources; inherit symlinks intact
- **OpenCode:** skill symlinks resolve; generated-artifact manifest hashes match; native guardrail plugin and adapter exist

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

### Codex custom workflows use skills, not prompt files

Asha originally installed slash commands as `~/.codex/prompts/<ns>-<cmd>.md`.
That historical prompt-file surface is not part of the current documented
Codex customization model. Codex has built-in slash commands, but current
documentation identifies skills as the authoring format for reusable user
workflows.

Implementation: each command MD is installed as a single-file Codex skill.
The directory `~/.codex/skills/<name>/` is a real dir; the `SKILL.md`
inside is generated from the source command MD with Claude-only frontmatter
(`argument-hint`, `allowed-tools`) stripped. Codex invokes via
`$<name>` (the namespacing collapsed from `/<ns>:<cmd>` is preserved in
the skill name). Current public Codex documentation names
`$HOME/.agents/skills/` as the canonical user authoring location and supports
symlinked skill folders. The `~/.codex/skills/` path remains verified in the
installed CLI but should be treated as a compatibility path, not the current
documented standard.

Source command MDs gain a single `name: <ns>-<cmd>` line in their YAML
frontmatter, which is benign for Claude (which derives names from
filenames) and required for Codex skills.

### Codex hook events are a subset

Codex supports SessionStart, PreToolUse, PermissionRequest, PostToolUse,
PreCompact, PostCompact, UserPromptSubmit, Stop, SubagentStart, and
SubagentStop. Claude additionally has SessionEnd, Setup, etc. Hooks bound to
unsupported events are dropped during install with a warning. Asha emits the
current nested TOML shape (`[[hooks.Event]]` groups containing
`[[hooks.Event.hooks]]` handlers).

### Codex native rules are installed as a coarse fallback

Because current Codex shell execution can bypass PreToolUse, Asha also writes a
dedicated `~/.codex/rules/asha.rules` file. This uses Codex's native
`prefix_rule()` execution-policy system for approval prompts on a narrow subset
of high-risk commands (`find /home`, `bfs /home`, `git reset --hard`,
force-push, protected-branch delete). This is **not** equivalent to Asha's full
regex policy engine: Codex rules are prefix-based and apply at permission /
sandbox boundaries, not every tool call. They are a native Codex safety net,
not a replacement for hook guardrails.

### Codex agents render to native TOML

Asha source agents remain Markdown, but the Codex installer renders them into
standalone custom-agent TOML files under `~/.codex/agents/`. Each generated
file has the Codex-required `name`, `description`, and
`developer_instructions` keys. The filename is namespaced
(`<ns>-<agent>.toml`) to avoid filesystem collisions; the agent's declared
`name` stays unchanged so workflow prose can still ask for agents like
`reviewer` or `thinker`.

### Codex plugins are native, but not this install path

Codex can package skills, hooks, MCP servers, app/connector mappings, and assets
behind `.codex-plugin/plugin.json`, with marketplace installation and per-plugin
enablement. Asha's Codex target does not yet generate that package. It installs
the same components directly because this repository is the local source of
truth. Plugin packaging is a distribution option, not a missing Codex
capability.

### Output styles plugin — retired

The `output-styles` plugin was retired in the 2026-07-10 ecosystem audit
(Claude's native `/output-style` covers switching). The codex/copilot
skip-list machinery remains for any future Claude-only plugin.

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

### Output styles are mounted, not scanned

Style files (e.g. the test plugin's canary) mount into
`~/.claude/output-styles/` and are selectable via Claude's native
`/output-style`. The custom `/style` switcher was retired with the
output-styles plugin (2026-07-10 audit).
