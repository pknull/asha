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
├── skills/<ns>-<skill>/             → plugins/<ns>/skills/<skill>/
├── agents/<ns>-<agent>.md           → plugins/<ns>/agents/<agent>.md
├── prompts/<ns>-<cmd>.md            → plugins/<ns>/commands/<cmd>.md
└── config.toml
    └── # ===== asha:start ===== ... # ===== asha:end =====
        # ↑ fenced region with [[hooks.X]] arrays, each tagged "# asha:<ns>"

~/.codex-asha/                       # persona overlay (asha-codex wrapper)
├── config.toml                      # generated: ~/.codex/config.toml + model_instructions_file
├── instructions.md                  # generated: identity-merge.sh output
├── skills/                          → ~/.codex/skills (filesystem inheritance)
├── prompts/                         → ~/.codex/prompts
├── agents/                          → ~/.codex/agents
└── sessions/                        # separate session history
```

The `asha-codex` wrapper exports `CODEX_HOME=~/.codex-asha` so the persona
overlay activates. Plain `codex` reads from `~/.codex/` directly and gets
the asha tooling without the persona.

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

| Layer | Claude wrapper (`asha-claude`) | Codex wrapper (`asha-codex`) |
|---|---|---|
| Identity assertion | `--append-system-prompt-file identity/asha-identity-system-prompt.md` | `model_instructions_file = ~/.codex-asha/instructions.md` (generated copy with the same identity assertion at the top, plus merged `~/.asha/{soul,voice,keeper,keeper-voice}.md`) |
| Lazy load (env signal) | `ASHA_PERSONA=1` triggers SessionStart hook | (eager — overlay always loads when `CODEX_HOME=~/.codex-asha`) |

Codex has no `--append-system-prompt-file` equivalent and no env-var
signal for system-prompt content, so the wrapper toggles persona by
swapping `CODEX_HOME` to a parallel directory rather than by setting
an env flag the way the Claude wrapper does.

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

### Codex prompts forbid subdirectories

Codex's `~/.codex/prompts/` is flat. Asha namespacing flattens to
`<ns>-<cmd>.md` (e.g. `code-review.md` instead of `/code:review`). The
prompts surface itself is officially deprecated by OpenAI in favor of
skills — long-term, asha slash commands should migrate to skills.

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

### Persona overlay is a generated snapshot

`~/.codex-asha/config.toml` is regenerated from `~/.codex/config.toml` on
each install. If you edit `~/.codex/config.toml` (e.g. add an MCP server)
without rerunning `./install.sh --target codex`, the overlay will be
stale — `asha-codex` will use yesterday's settings. drift-check catches
this. (Codex has no `[include]` directive; this is the cleanest workaround
until OpenAI adds one.)

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
