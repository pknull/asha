# Symlink-Mount Installer

Flat direct-mount install model for the asha repo's primitives. This repo
does not currently package its local install path as a Codex plugin; it uses
symlinks plus generated harness-native artifacts. Codex itself supports native
plugins and marketplaces, which remain a separate future distribution path.

The installer supports four first-class harnesses: Claude Code, OpenAI Codex
CLI, GitHub Copilot CLI, and OpenCode stable v1. All launch through one `asha` dispatcher.
Source skills, agents, and commands remain shared Markdown; adapters render each
harness's native form.

Asha Control adds no install target. The existing dispatcher exposes `asha
task` and `asha control`; using them requires local `jj` and `tmux`. The `gh`
CLI is required only for the optional `--pr` and `--issue` source modes.

## Architecture

```
<asha repo>/                       # checkout path; recorded as asha_root in ~/.asha/config.json
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
│   ├── opencode.sh       # OpenCode stable-v1 install/uninstall + plugin renderer
│   ├── registry.sh       # canonical harness catalogue
│   └── generated-artifacts.sh # ownership manifests + collision safety
├── bin/                  # installed via --bin
│   └── asha              # unified dispatcher + launcher
│                         #   grammar: asha [install|uninstall] [harness] [args…]
│                         #   shims ~/.local/bin/asha-{claude,codex,copilot,opencode} → asha
├── identity/             # repo-owned identity assertion + merge scripts
│   ├── asha-identity-system-prompt.md
│   ├── identity-merge.sh # assertion + three hot user files → cached instructions
│   └── operational-merge.sh # operation.md + active learnings
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

Launching with `asha <harness>` checks whether that target is installed and
fresh. Interactive first use offers to configure it; non-interactive first use
requires `asha --yes <harness>` or `ASHA_YES=1`. Claude and Codex must already
have their harness-owned native config files, created by running the plain
harness once. Asha will not fabricate those files.

If an installer finds pre-v2 global learning sources, it points to the
reviewed `/session:consolidate` path rather than interpreting them. Migration
preserves those sources and writes `~/.asha/learnings/.migration-v2.json` only
after the hash-bound review commits; a valid marker suppresses repeat upgrade
warnings without deleting the evidence or backups.

### One-time migration from pre-manifest installs

Generated Codex, Copilot, and OpenCode files use ownership manifests. Existing
generated files cannot be distinguished
from foreign files safely until adopted. Run the relevant install once with
`--force`:

```bash
asha install codex --force
asha install copilot --force
asha install opencode --force
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

**No persona overlay.** The `asha codex` launch path regenerates the capped hot
identity, renders the operational layer separately, combines them for Codex's
single-file instruction seam, and injects the result through a CLI override:

```bash
# bin/asha (codex branch)
identity-merge.sh ~/.asha/cache/instructions.md
operational-merge.sh <temporary-file>
# combined output: ~/.asha/cache/instructions-codex.md
exec codex -c "model_instructions_file=\"<combined-or-identity-file>\"" "$@"
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
├── hooks/asha-recovery.json         # Memory v2 recovery + direct context/RP callbacks
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
identity-merge.sh ~/.asha/cache/instructions-copilot.md   # assertion + soul/voice/keeper
#  → wrapped as ~/.asha/cache/copilot-instr/.github/instructions/asha.instructions.md   (applyTo:"**")
operational-merge.sh → asha-operational.instructions.md   # operation.md + active learnings (same dir)
export COPILOT_CUSTOM_INSTRUCTIONS_DIRS=~/.asha/cache/copilot-instr   # Copilot auto-loads both files
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
| Custom agents | Working | Generated `.agent.md` files under `~/.copilot/agents/` |
| Recovery hooks | **Installed** | `asha-recovery.json` records only bounded, secret-scrubbed mechanical recovery state under the initialized project's ignored `Work/session-state/`. It never reads host transcripts or publishes semantic Memory; `/session:save` remains explicit. |
| PreToolUse guardrails | **Installed** | `copilot_install_hooks()` writes a dedicated `~/.copilot/hooks/asha-guardrails.json` (Copilot loads every `*.json` there, so a user's own `hooks.json` is untouched) pointing at `plugins/session/hooks/handlers/copilot-policy-adapter.sh`, which bridges Copilot's hook contract to the shared `policy-guard.sh` + `block-secrets.sh`. Recovery uses separate prompt/PostToolUse/SessionEnd callbacks; no Stop auto-save exists. **Enforcement verdict + live-test findings + the #2893 caveat: [docs/harness-enforcement.md](docs/harness-enforcement.md).** |
| Hook payload normalization | **Installed** | Native camelCase session/tool payloads are normalized at the recovery and policy seams, including string/object `toolArgs`, touched paths, results, and error fields. |
| MCP config | Not managed | `~/.copilot/mcp-config.json` is read directly by Copilot; not touched by this installer (matches Claude/Codex which also don't manage MCP) |
| Persona auto-injection | **Automatic — per-launch** | `asha copilot` regenerates the capped hot identity, wraps it as `~/.asha/cache/copilot-instr/.github/instructions/asha.instructions.md` (`applyTo: "**"`), writes the operational layer as a separate instruction file, and exports `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`. Plain `copilot` stays persona-free. Status/verification: [docs/harness-enforcement.md](docs/harness-enforcement.md). |
| `drift-check` | **Copilot-aware** | `asha doctor [copilot]` (front door for `bin/asha-drift-check.sh`) audits symlinks, command-skill freshness, and guardrails content; `--fix` self-heals. |
| Team distribution | **Additive path** | `asha build copilot` packages namespaces as native Copilot plugins (marketplace + `enabledPlugins` pinning); see [docs/distribution-copilot.md](docs/distribution-copilot.md). Repo onboarding: `asha init-repo`. |

### OpenCode stable v1 (`--target opencode`)

Requires OpenCode `>=1.15.11`. The home resolves in this order:
`ASHA_OPENCODE_HOME`, `OPENCODE_CONFIG_DIR`, then
`${XDG_CONFIG_HOME:-~/.config}/opencode`.

```
~/.config/opencode/
├── skills/<declared-name>/              → plugins/<ns>/skills/<skill>/
├── commands/<name>.md                   # generated clean command Markdown
├── agents/<ns>-<agent>.md               # generated with mode: subagent
└── plugins/asha.js                      # generated lifecycle/policy/context bridge
```

The plugin bridges `tool.execute.before` to the shared policy and secret guards;
injects session identity into `shell.env`; buffers guidance and visible policy
warnings for `experimental.chat.system.transform`; invokes SessionStart recovery
maintenance; and seals mechanical recovery state from `dispose`. An `ask` policy result is
treated as deny because no interactive ask response is verified at this hook
seam. The policy layer is fail-open on adapter failure and is not containment.

Manual save uses live model context through `/session:save`; no OpenCode SQLite
transcript is read. There is no automatic semantic save or idle checkpointing.

`asha opencode` appends a combined hot-identity and operational file to
`OPENCODE_CONFIG_CONTENT.instructions`; plain `opencode` remains persona-free.

## Namespaces

`namespaces.json` maps each plugin directory to the namespace used for
slash commands and primitive prefixes. Almost all entries are 1:1; one
exception preserves a legacy plugin name:

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
| Delivery | launch-time identity file; operation via SessionStart | launch-time combined file | launch-time instruction directory with separate files | launch-time combined file |
| Orchestrator stance | combined chair file (default on) | combined chair file (default on) | not injected | not injected |

Wrapped `asha claude` and `asha codex` launches also carry the orchestrator
stance by default: `identity/orchestrator-brief.md` — the operator's-chair
brief pointing at the `operate-control` skill — is appended to the launch
instructions (a separate combined cache file; the canonical identity render
stays identity-only). It is suppressed for Control-launched coordinator
sessions (`ASHA_COORDINATOR_LAUNCH`), for Control-managed workers
(`ASHA_PERSONA=0`), by `"orchestrator_stance": false` in
`~/.asha/config.json`, or per-launch via `ASHA_ORCHESTRATOR_STANCE=0`
(`=1` overrides a config `false`; the coordinator suppression beats both).
The config read needs `jq` and a well-formed file — without them the
default (on) applies silently; `ASHA_ORCHESTRATOR_STANCE=0` is the
dependable per-launch kill switch. Plain harness commands never receive
the stance.

Codex has no `--append-system-prompt-file` equivalent at the CLI, and
its `model_instructions_file` config field accepts only a single file
path (no `[include]` directive). The dispatcher handles both gaps:
identity-merge.sh concatenates the repository assertion
`identity/asha-identity-system-prompt.md` plus the three user-owned hot files
`~/.asha/{soul,voice,keeper}.md` into a capped file. It never includes the
cold reference corpus or `keeper-voice.md`. The dispatcher then
`-c model_instructions_file=...` injects it at launch. No on-disk
overlay; both `codex` and `asha codex` use the same `~/.codex/`. Extended
identity and Keeper calibration under `~/.asha/reference/` load only through
the `asha-reference` skill and remain private task context.

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
- **Codex:** no dangling symlinks; `config.toml` parses as TOML; tagged hook paths exist; native `rules/asha.rules` installed; cached identity instructions are fresh; inherit symlinks intact
- **OpenCode:** no dangling skills; generated-artifact manifest matches; plugin carries policy/session/dispose hooks; CLI version satisfies the stable-v1 floor

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
