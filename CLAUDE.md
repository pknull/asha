# CLAUDE.md — Repository Guide

`AGENTS.md` is the active instruction surface for Codex. This file is shared
repository documentation for assistants working in Asha. Keep it limited to
project-specific invariants; usage detail belongs in the owning plugin README
or focused document.

## Project contract

Asha is a multi-harness agent toolkit. One source corpus under `plugins/` is
installed or rendered into native surfaces for:

- Claude Code
- OpenAI Codex
- GitHub Copilot CLI
- OpenCode stable v1

Portability means equivalent intent at each harness's real seam. It does not
mean copying Claude primitives into every target unchanged.

When changing a primitive, update the installer, doctor checks, generated-file
ownership, and tests for every affected harness. Never claim cross-harness
support from source inspection alone.

## Authority map

Use these sources instead of copying their facts into new catalogues:

| Question | Authority |
|---|---|
| Plugin purpose, usage, inventory, and version | `plugins/<plugin>/README.md` |
| Plugin directory to public namespace | `namespaces.json` |
| Harness capability contract | `harnesses/capabilities.json` |
| Tested enforcement verdicts and caveats | `docs/harness-enforcement.md` |
| Install and launcher mechanics | `INSTALLER.md`, then live scripts |
| Memory authority and workspace planes | `docs/memory-architecture.md` |
| Frozen Control v1 contracts orchestration binds to | `docs/control-contracts.md` |
| Orchestration Core operating surface and JSON wrappers | `docs/orchestration.md` |
| Release history | `CHANGELOG.md` |
| Current behavior | Live state and repository code |

Plugin README files are the sole plugin-version authority. Root documentation
may list plugin domains and link their guides, but must not duplicate plugin
version tables.

Implementation and live state outrank documentation. Correct stale prose when
the code proves it wrong; do not change code merely to preserve an old claim.

## Repository shape

```text
asha/
├── bin/                    # dispatcher, doctor front door, env bootstrap
├── harnesses/              # target adapters and capability registry
├── identity/               # hot identity and operational merge scripts
├── lib/                    # install, uninstall, build, and shared engines
├── plugins/<plugin>/       # harness-agnostic source corpus
├── docs/                   # focused architecture and mechanism documents
├── tests/                  # validators and integration tests
├── namespaces.json         # directory -> public namespace
├── install.sh              # thin installer shim
└── uninstall.sh            # thin uninstaller shim
```

Important shared seams:

- `bin/asha` owns dispatch and wrapper-scoped persona injection.
- `harnesses/registry.sh` owns recognized harnesses and their homes.
- `harnesses/generated-artifacts.sh` owns collision-safe generated files.
- `lib/install.sh` and `lib/uninstall.sh` coordinate target adapters.
- `plugins/session/hooks/` contains shared policy and recovery machinery.
- `identity/identity-merge.sh` owns the capped hot identity render.
- `identity/operational-merge.sh` owns operation rules plus active learnings.

## Harness seams

| Primitive | Claude | Codex | Copilot | OpenCode |
|---|---|---|---|---|
| Commands | Native command Markdown | Rendered skills | Rendered skills | Native cleaned command Markdown |
| Agents | Source Markdown | Generated TOML | Generated `.agent.md` | Generated Markdown subagents |
| Skills | Symlinked directories | Symlinked directories | Symlinked directories | Symlinked directories |
| Hooks | Tagged `settings.json` entries | Nested TOML hook tables | Dedicated hook JSON | Generated `plugins/asha.js` bridge |
| Persona | Append-system-prompt file | `model_instructions_file` | Custom instructions directory | Wrapper-scoped instructions array |

Claude commands may retain Claude-only frontmatter. Renderers must strip or
translate unsupported fields for other harnesses. Generated command skills and
agents do not update when their source changes; reinstall the affected target.

Codex hooks and rules are useful guardrails, not a complete enforcement
boundary. In particular, not every shell execution path is intercepted.

OpenCode commands and agents live under plural `commands/` and `agents/`.
Integration hooks belong in the generated `plugins/asha.js` bridge.

Generated artifacts must be recorded in the target ownership manifest.
Installers refuse foreign collisions unless the user explicitly supplies
`--force`; uninstallers remove only owned artifacts.

## Dispatcher and identity

The dispatcher grammar is positional:

```text
asha [HARNESS]                   the chair: chosen harness seated at ~/.asha/chair
asha [HARNESS] args...           launch a harness from the caller's cwd
asha install <target> [...]      provision primitives and wrappers
asha uninstall <target> [...]    remove owned installation state
asha doctor [target] [--fix]     audit installation drift
asha task <subcommand> [...]     manage persistent jj/tmux tasks
asha control [...]               open or integrate the Control TUI
```

`asha claude install` launches Claude and forwards `install`; it is not the
same command as `asha install claude`.

`asha <harness>` checks installation freshness before launch. Interactive
first use offers to configure the target; non-interactive first use requires
`--yes` or `ASHA_YES=1`. Claude and Codex must already have their harness-owned
native config files, which Asha deliberately does not fabricate.

Plain `claude`, `codex`, `copilot`, and `opencode` remain persona-free. Installed
skills and hooks still exist because the plain and wrapped commands share the
same harness home.

Claude native management subcommands are forwarded without the append-prompt
flag so their own TUI or one-shot behavior remains intact.

The hot identity consists of the repository assertion
`identity/asha-identity-system-prompt.md` plus three user-owned files:

- `~/.asha/soul.md`
- `~/.asha/voice.md`
- `~/.asha/keeper.md`

`identity/identity-merge.sh` combines those sources under a byte budget. It
does not load `keeper-voice.md` or the extended reference corpus.

Cold material under `~/.asha/reference/` is private and task-selected through
the `asha-reference` skill. Do not copy it into project or workspace Memory.

The operational layer is separate from identity: `~/.asha/operation.md` plus
active learnings. Claude receives it through SessionStart; file-based harness
launchers combine or colocate it with their wrapper instructions as required.

## Plugin contract

Each plugin uses only the directories it needs:

```text
plugins/<plugin>/
├── README.md
├── LICENSE
├── commands/*.md
├── agents/*.md
├── skills/<skill>/SKILL.md
├── hooks/hooks.json
├── tools/
├── recipes/
└── templates/
```

Rules:

1. A plugin README is required and owns that plugin's documentation.
2. A versioned plugin declares one strict `**Version**: X.Y.Z` line there.
3. Every plugin directory has an entry in `namespaces.json`; stale entries fail
   validation as well.
4. Public primitive names use the mapped namespace, not necessarily the
   directory name. `panel` maps to `panel-system`.
5. Skill directories contain `SKILL.md` with YAML frontmatter and a stable
   `name`.
6. Command source remains compatible with Claude; adapters render other forms.
7. Agent source remains Markdown; adapters own native target serialization.
8. Relative paths in installed primitives must resolve from the installed
   topology, including packaged Copilot distribution when supported.

Do not add a marketplace or per-plugin metadata file merely to hold a version.
The direct install path discovers primitives by convention.

## Change invariants

- Preserve user data. Destructive operations require explicit confirmation.
- Search for existing helpers before creating another implementation.
- Make targeted fixes surgically; do not reformat unrelated code.
- Treat symlink resolution as a portability concern. Bootstrap scripts may
  duplicate the marked pre-library symlink walk; keep those copies synchronized.
- Keep source and generated twins synchronized through the renderer, never by
  hand-editing installed output.
- Do not add persona, profile, or private workspace material to committed
  project Memory.
- Lifecycle hooks may maintain bounded recovery state; they must never publish
  semantic Memory or invoke Git.
- Capability and policy claims need the exact tested harness/path qualifier.
- Delivered proposals are historical records, not current operating authority.

## Verification

Run the narrowest relevant test first. Common checks:

```bash
./tests/validate-plugins.sh
./tests/validate-versions.sh
./tests/test-hooks.sh
```

Before declaring cross-harness installer work complete:

```bash
./tests/run-tests.sh
```

For target-specific install changes also run:

```bash
./bin/asha-drift-check.sh --target codex
./bin/asha-drift-check.sh --target opencode
```

Use `--dry-run` when validating a mutating installer plan. Test both a clean
install and collision/uninstall behavior when ownership semantics change.

## Focused documentation

- [Root orientation and plugin map](README.md)
- [Installer and launcher model](INSTALLER.md)
- [Harness enforcement](docs/harness-enforcement.md)
- [Memory architecture](docs/memory-architecture.md)
- [Manual save fallback](docs/save-manual-pipeline.md)
- [Copilot distribution](docs/distribution-copilot.md)
- [Secrets](docs/secrets.md)
- [Release history](CHANGELOG.md)

Historical proposals under `docs/proposals/` preserve design context after
delivery. Follow their banners to current guides before using them as an
implementation reference.
