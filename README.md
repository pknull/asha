# asha

**Description**: A multi-harness agent toolkit. Persistent identity, session memory, and domain-focused plugins for Claude Code, OpenAI Codex, GitHub Copilot CLI, and OpenCode.

Asha renders or mounts skills, agents, commands, and hooks into each harness's native surfaces, ships a single `asha` dispatcher that injects a shared persona, and maintains one explicit compact Memory contract across all four CLIs.

---

## Start here

A wrapped launch with **no arguments** is the operator's chair: it seats the
session at `~/.asha/chair` with the orchestrator stance, whichever harness you
name. The harness word selects the tool, not the stance.

```bash
asha                           # the chair (default harness)
asha claude                    # the chair, held by Claude Code
asha codex                     # the chair, held by Codex
```

From the chair you converse: Asha can work directly, launch an independent
project harness, open a project Room, or return a short utility's result.
`asha control` monitors those sessions and handles input, attachment and
closure. Ordinary work starts without an initiative or plan approval.
See [Project sessions](docs/session-hub.md).

To work **directly in a repository** instead, pass arguments (arguments keep
the caller's working directory):

```bash
cd /path/to/repository
asha claude "fix the failing test"   # direct session in this repo
```

Inside a multi-repository workspace, direct launches follow the same rule:
child repository for repository work, workspace root for cross-repository
planning. See [Memory and workspace use](#memory-and-workspace-use).

Asha exposes four kinds of reusable surface:

| Surface | What it is | How to use it |
|---|---|---|
| **Command workflow** | An explicit multi-step operation such as review, save, or panel analysis | Claude/OpenCode: use the rendered slash command. Codex/Copilot: name the rendered skill or ask for the operation directly. |
| **Agent** | A bounded specialist used by a workflow or delegated directly | Usually let a command choose it. Name it explicitly only when you need that one role. |
| **Skill** | On-demand instructions and tools selected from task intent | Ask for the task naturally or name the skill. |
| **Recipe/engine** | A longer orchestration pattern behind a command | Start through the owning plugin's documented command; do not invoke internal files directly unless developing Asha. |

The root README is the map. Each plugin README is the authority for that
plugin's version, detailed usage, inventory, prerequisites, and safety
boundaries:
[Session](plugins/session/README.md), [Code](plugins/code/README.md),
[Write](plugins/write/README.md), [Panel](plugins/panel/README.md),
[RP](plugins/rp/README.md), [Image](plugins/image/README.md),
[Admin](plugins/admin/README.md), [Security](plugins/security/README.md),
[Asha identity](plugins/asha/README.md), and [Test](plugins/test/README.md).

---

## Install model: native rendering across four harnesses

Plugins live in `plugins/<name>/`. The installer symlinks byte-compatible primitives and renders harness-specific forms where required:

| Harness | Mount root | Persona injection |
|---|---|---|
| **Claude Code** | `~/.claude/*` (skills, agents, hooks, settings.json entries) | `asha claude` injects the hot identity via `--append-system-prompt-file`; SessionStart supplies the operational layer |
| **OpenAI Codex** | `~/.codex/*` (skill directories, TOML custom agents, hooks, rules) | `asha codex` supplies one launch-time `model_instructions_file` containing hot identity plus the operational layer |
| **GitHub Copilot CLI** | `~/.copilot/*` (skills, agents, hook JSON) | `asha copilot` wires separate identity and operational instruction files through `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` |
| **OpenCode** | `${XDG_CONFIG_HOME:-~/.config}/opencode/*` (skills, commands, agents, plugin) | `asha opencode` appends a combined identity and operational file through `OPENCODE_CONFIG_CONTENT` |

Install commands:

```bash
./install.sh                                # mount into ~/.claude/* (default target)
./install.sh --target codex                 # mount into ~/.codex/*
./install.sh --target copilot               # mount into ~/.copilot/*
./install.sh --target opencode              # mount into ~/.config/opencode/*
./install.sh --target all                   # mount into all four
./install.sh --bin all --default claude     # install the asha dispatcher + harness shims in ~/.local/bin
./uninstall.sh                              # remove asha-tagged symlinks/entries
```

After `./install.sh --bin all` you'll have:

| Command | Effect |
|---|---|
| `asha` | the chair: launch the default harness seated at `~/.asha/chair` with the orchestrator stance (no-argument launches only; arguments keep your cwd) |
| `asha <harness>` | launch `claude`/`codex`/`copilot`/`opencode`; interactive first use offers to configure a missing or stale target |
| `asha install <target>` | provision a harness (`claude`/`codex`/`copilot`/`opencode`/`both`/`all`) |
| `asha uninstall <target>` | remove Asha from a harness |
| `asha-claude` · `asha-codex` · `asha-copilot` · `asha-opencode` | harness shims (each ≡ `asha <harness>`) |

Grammar is positional: launch as `asha [--yes] [harness] [args…]`, and put an
Asha management verb before its target (`asha install codex`, `asha doctor
codex`). A verb *after* the harness is passed through, so `asha claude install`
runs `claude install` rather than the Asha installer.

Non-interactive first use cannot answer the configuration prompt; pass `--yes`
before the harness (or set `ASHA_YES=1`). Claude and Codex must first be run
plain once so their harness-owned `settings.json` or `config.toml` exists. Asha
does not fabricate either file.

See **[INSTALLER.md](INSTALLER.md)** for the full install model, per-harness limitations, and the bin/wrapper details.

**Adopting a pre-manifest Codex, Copilot, or OpenCode install:** run
`asha install <harness> --force` once to record existing generated files in the
ownership manifest before uninstalling or using ordinary collision-safe
updates.

---

## Control: sessions and Rooms

The default `asha control` dashboard shows project harness sessions. Launch
with `asha control session launch --project PROJECT --prompt ASSIGNMENT`,
add `--profile room` for an Asha conversation, or `--transport structured`
for a Claude/Codex utility. Enter attaches, `a` opens input, `m` sends context,
`x` closes, and `q` leaves work running. See the
[session guide](docs/session-hub.md) for status and delivery semantics.

| Command | Purpose |
|---|---|
| `asha control session launch\|list\|show\|attach\|send\|close\|stop\|resume …` | Launch and steer project sessions ([session guide](docs/session-hub.md)). |
| `asha room open NAME --project PROJECT --harness H --prompt TEXT` | Start a Room detached in exactly one initialized Memory v2 project, selected by exact path or indexed name/ID. `list`, `attach`, and confirmed `close` manage it. |
| `asha control projects` | List the projects a session or Room can launch in. |
| `asha control doctor` | Check Control's dependencies, hooks and state. |
| `asha control supervisor …` | Start owners for structured sessions; `install` manages its systemd user service ([managed sessions](docs/managed-sessions.md)). |
| `asha cockpit [DIR]` | The chair beside the session dashboard in one tmux window. |
| `asha initiative list\|show\|export` | Read the evidence of retired initiatives (read-only). |

The legacy initiative engine, the `asha task` substrate and `asha trigger` were
retired on 2026-10-05. Their records stay in place as evidence and
`asha initiative export` writes all of them; see
**[docs/control.md](docs/control.md)** for Rooms, the doctor, the state layout
and the retired evidence.

---

## Harness support & behavior

Asha drives four agent CLIs from **one source corpus** (`plugins/<ns>/`). They don't support the same things, and each mounts the same primitive differently. First-class support means native rendering at each harness seam, not fake parity: see `harnesses/capabilities.json` for the machine-readable contract.

> **The full per-capability matrix — current status, mounting method, live-test findings, and caveats — is the single source of truth in [docs/harness-enforcement.md](docs/harness-enforcement.md).** This section explains *why* the behaviors differ (the mechanics, which rarely change); for current *status*, defer to that doc.

At a glance: skills, agents, persona, the operational layer, explicit `/session:save`, and bounded recovery work across all four harnesses, but through different forms. Asha command workflows are rendered as skills on Codex/Copilot and native command Markdown on OpenCode. Codex agents are generated TOML, Copilot agents are generated `.agent.md`, OpenCode agents are generated Markdown subagents, and Claude agents retain the source Markdown. Every harness requires explicit semantic publication; clean exit only seals unpublished recovery.

### Why the behaviors differ

**Commands are *generated* for Codex/Copilot/OpenCode but *symlinked* for Claude.** A symlink is byte-identical to its source, so it only works when the artifact is already in the target harness's format. Claude commands carry Claude-only frontmatter (`argument-hint`, `allowed-tools`). Codex and Copilot receive these workflows as skills. OpenCode has a native command surface, so it receives cleaned command Markdown. Agents are also rendered where the native shape differs: Codex gets TOML custom agents, Copilot gets `.agent.md`, OpenCode gets Markdown with `mode: subagent`, and Claude keeps the source Markdown. Trade-off: editing a command or agent source doesn't auto-propagate to generated copies; re-run `asha install <harness>`.

**Output styles are retired.** The former `output-styles` plugin (`/style` + 8 style files) was Claude-only by design and was retired in the 2026-07-10 ecosystem audit — Claude's native output-style switching covers the need, and the other harnesses never had an equivalent Asha seam.

**Persona is injected at each harness's real seam.** Claude uses `--append-system-prompt-file`; Codex uses `model_instructions_file`; Copilot uses `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`; OpenCode uses `OPENCODE_CONFIG_CONTENT.instructions`. Every mechanism is wrapper-scoped, so the plain harness remains persona-free.

The hot identity is the repository assertion in
`identity/asha-identity-system-prompt.md` plus three user-owned files:
`~/.asha/soul.md`, `~/.asha/voice.md`, and `~/.asha/keeper.md`. Extended Asha
and Keeper material under `~/.asha/reference/` remains cold until the
`asha-reference` skill selects a task-relevant file. Cold reference material is
private and is never copied into project or workspace Memory.

**The operational layer reaches all four.** `~/.asha/operation.md` plus a
capped rendering of active `~/.asha/learnings/` records load at session start.
Candidates and retired records are never injected as instructions. Claude receives the layer through SessionStart, Codex through
`model_instructions_file`, Copilot through its custom instructions directory,
and OpenCode through the wrapper-scoped instructions array. Files are generated
by `identity/operational-merge.sh` with the same budgets.

**Hook surfaces are harness-native.** Claude uses JSON in `settings.json`; Codex uses nested TOML hook tables; Copilot uses dedicated hook JSON; OpenCode uses `plugins/asha.js`. Prompt/tool hooks update bounded recovery state, whilst policy adapters bridge each real-time hook contract to the shared rules.

**First launch requires the harness's own config to already exist** for Claude and Codex. Their installers deliberately refuse to fabricate `settings.json` / `config.toml` (the harness owns that file's format). Copilot and OpenCode use additive Asha-owned files and have no such precondition.

### Policy guardrails (PreToolUse deny/ask)

Beyond persona, Asha enforces **declarative tool-call policies** through a PreToolUse hook (`plugins/session/hooks/handlers/policy-guard.sh`). Rules live in `plugins/session/hooks/policies/rules.json` (+ an optional user layer `~/.asha/policies.json`, merged by `id` — user wins). Each rule matches a tool plus a command/path regex and applies `deny`, `ask`, or advisory `warn`, with an optional `override_env` escape hatch. The seed rule blocks broad `find`/`grep -r`/`bfs`/`fd`/`rg` scans over `/home`; override it with `ASHA_ALLOW_BROAD_SCAN=1` when the operation is deliberate. Rules are user-tunable.

**Prefer `deny` over `ask` for rules that must bite.** An `ask` decision is auto-approved without surfacing a prompt in any session running an auto-accept permission mode, which makes the rule silently inert. `deny` (exit 2) blocks regardless of mode. The shipped rules use `deny` with `override_env` escape hatches for exactly this reason; `ask` remains in the schema for rules whose value is the prompt itself.

**Cross-harness enforcement status and caveats are in [docs/harness-enforcement.md](docs/harness-enforcement.md) (the single source of truth).** Claude and Copilot run Asha's policy hooks across their tested tool paths. OpenCode routes `tool.execute.before` through the shared policy and secret guards; an `ask` decision degrades to deny because no interactive permission response is verified at that seam. Codex can run the same hooks for supported simple Bash, `apply_patch`, and MCP calls, but `unified_exec` shell interception is incomplete and hooks are not a complete enforcement boundary. Codex also gets `~/.codex/rules/asha.rules`: native prefix rules that prompt for a narrow command subset, pin `asha` to the installed launcher, and let exactly `asha control session report` and `handoff` run outside the sandbox.

The engine is **fail-open** by design — any rule/parse error allows the call, because a guardrail must never brick tool use. And it is a **soft deterrent, not a sandbox**: it regex-matches the command string, so an agent can evade it deliberately (`cd /home && find .`, long flags, indirection), and on Copilot it can be bypassed under parallel tool calls. Pair it with the harness's own permission/sandbox controls for hard containment. A reviewed learning can become a mechanical rule instead of prose a model can skip past.

See **[INSTALLER.md](INSTALLER.md)** for the per-harness layout diagrams and the full rationale.

---

## Memory v2: publication and recovery

Asha does not read host transcripts. Semantic memory is published only when the
user explicitly invokes `/session:save`. The live model authors and validates
`Memory/activeContext.md` (Objective, State, Next, Blockers; at most 4 KiB) and
`Memory/decisions.md` (current binding decisions), then commits and pushes by
default. Shipped readers use the publication lock through `memory_v2.py read`;
hooks never publish semantic Memory or invoke Git.

Prompt/tool hooks atomically maintain ignored, mode-0600 recovery snapshots in
`Work/session-state/`. Each is at most 2 KiB, expires after seven days, and is
lower authority than published Memory. SessionEnd only seals and prunes.

Cross-project learnings use candidate, active, and retired states. Activation
uses a user-controlled save-evidence heuristic requiring three distinct
session ids across two stable project identities; only active learnings enter
the startup instruction layer.

### The lifecycle

```text
initialize once
      ↓
start → read the last explicit publication → verify it against live disk
      ↓
work  → hooks replace one small ignored recovery snapshot
      ↓
save  → author + validate the two Memory files → publish as one locked pair
      ↓                                             ↓
next start ← current handoff + active learnings   optional commit + push
```

1. **Initialize.** `/session:init` creates a stable project identity, the two
   publication files, and narrow Git ignores for private recovery and migration
   state. It refuses to reinterpret legacy Memory, which must be rewritten in
   the v2 format first.
2. **Orient.** Project and workspace readers acquire the publication lock and
   read the pair coherently. The handoff is a starting claim, not ground truth:
   the agent checks it against current files and live state. SessionStart also
   expires old candidates and may surface the newest recovery hint with an
   explicit verify-first label.
3. **Work.** Prompt and post-tool callbacks replace a session-local recovery
   snapshot containing only bounded prompt hints, touched paths, the last
   mechanical action, and a blocker indicator. This is crash residue, not a
   transcript and not semantic Memory.
4. **Publish deliberately.** `/session:save` drafts both files from the live
   session, verifies claims, validates the schemas, and replaces the pair under
   one project lock with rollback journaling. It commits and pushes by default;
   `--no-push` retains the local commit and `--scope none` performs no Git work.
5. **Carry only what remains useful.** The next session receives the compact
   current handoff and active global learnings. Superseded project state leaves
   the publication; Git owns history. Learning candidates need corroboration
   before activation, and retired learnings remain on disk without loading.

There is no append-only Asha transcript anywhere in this cycle. Harness-native
transcripts may still exist because the host owns them, but Asha neither parses
nor copies them.

### How each harness participates

| Harness | Save surface | Recovery/context seam | End behavior |
|---|---|---|---|
| **Claude Code** | Native `/session:save` command | Native `SessionStart`, `UserPromptSubmit`, and `PostToolUse` hooks; SessionStart injects the coherent project pair, operational layer, active learnings, workspace context, and any verify-first recovery hint | Native `SessionEnd` seals recovery only |
| **OpenAI Codex** | Rendered `session-save` skill | Native TOML hooks call the shared startup/recovery handlers where Codex exposes the event; wrapper instructions supply operation and active learnings | No supported SessionEnd hook; the next start sweeps stale snapshots |
| **GitHub Copilot CLI** | Rendered `session-save` skill | `asha-recovery.json`; SessionStart returns the coherent project pair plus workspace/recovery context through `additionalContext`, whilst wrapper instructions supply operation and active learnings | `sessionEnd` seals recovery only |
| **OpenCode** | Rendered native `session-save` command | Generated `plugins/asha.js` calls the shared startup/recovery handlers and injects their context through system-prompt transformation | `dispose` seals recovery only |

The seams differ; the authority model does not. Every harness uses the same
schemas, publisher, recovery writer, and learning manager, and none may turn a
lifecycle callback into a semantic save.

---

## Plugin Domains

| Domain | Plugin | Purpose |
|--------|--------|---------|
| **Core** | `session` | Explicit compact publication, bounded recovery, learning lifecycle, guardrails, loops, and workspace management |
| **Identity** | `asha` | Capped hot identity plus task-selected cold references |
| **Research** | `panel-system` | Multi-perspective analysis, expert panels, and decision-making |
| **Development** | `code` | Code review, orchestration patterns, TDD, and guarded issue processing |
| **Creative** | `write` | Fiction writing, prose craft, continuity, and style analysis |
| **Creative** | `rp` | Live roleplay lifecycle, continuity gating, and canon ratification |
| **Image** | `image` | Local ComfyUI workflows and on-demand paid fal.ai generation |
| **Integrations** | `admin` | Todoist, grounded search, computation, knowledge, and mail integrations |
| **Security** | `security` | Web-application security review patterns |
| **Tooling** | `test` | Installer canary primitives |

## Plugin guides

The owning guide below is the catalogue for each plugin.

| Plugin | Primary entry point | Use it for | Detailed instructions |
|---|---|---|---|
| `session` | `/session:*` or rendered `session-*` skills; `asha workspace …` | Memory v2 lifecycle, workspace management, guarded loops, process routing | [Session guide](plugins/session/README.md) |
| `panel-system` | `/panel-system:panel` or `panel-system-panel` | Decomposition, interviews, adversarial analysis, recorded decisions | [Panel guide](plugins/panel/README.md) |
| `code` | `/code:*` or rendered `code-*` skills | Implementation orchestration, debugging, review, verification, PostgreSQL work | [Code guide](plugins/code/README.md) |
| `write` | `/write:*` or rendered `write-*` skills | Fiction state, drafting workflows, editorial review, style analysis, export | [Write guide](plugins/write/README.md) |
| `rp` | `/rp:*` or rendered `rp-*` skills | Live roleplay lifecycle, continuity gates, canon ratification | [RP guide](plugins/rp/README.md) |
| `image` | `image-generation` or `image-fal` skill | Stable Diffusion prompts and ComfyUI workflows (local first); priced, user-approved fal.ai images, sprites, textures, 3D, rigging, audio and video | [Image guide](plugins/image/README.md) |
| `admin` | Name the required skill | Todoist, Gemini, Wolfram, BookStack, and Proton Mail operations | [Admin guide](plugins/admin/README.md) |
| `security` | `security-review` skill | Security-sensitive implementation and review | [Security guide](plugins/security/README.md) |
| `asha` | `asha <harness>`; `asha-reference` when needed | Compact identity and task-selected private reference material | [Identity guide](plugins/asha/README.md) |
| `test` | `/test:ping` or rendered canary skills | Installer verification only | [Test guide](plugins/test/README.md) |

Commands are the user-facing workflows. Agents are their specialist parts;
skills are selected on demand. The plugin guides explain when direct agent use
is appropriate and when the owning command should coordinate the work.

---

## Memory and workspace use

Asha manages several stores because they have different owners and publication
rules. They are not interchangeable:

| Store | Scope | Default location | Commit policy | Typical content |
|---|---|---|---|---|
| **Global identity and learnings** | User, all projects | `~/.asha/` | Separate user-managed store | Identity, operation rules, preferences, candidate/active/retired learnings |
| **Machine state** | This machine | `~/.asha/state/`, `~/.asha/workspaces/`, `~/.asha/cache/` | Never commit; machine-managed | Control session, Room and retired-initiative records, retired task workspaces, rendered persona cache — everything under one `ASHA_HOME` root since the single-root migration |
| **Repository operational memory** | One repository | `<repo>/Memory/` | Explicit save commit | Four-section handoff and current binding decisions |
| **Workspace operational memory** | A group of repositories | `<workspace>/Memory/` | Explicit workspace-scope save | Cross-repository handoff and binding decisions |
| **Private workspace memory** | User-local workspace material | `<workspace>/memory-local/` | Never commit | Private notes, material not ready for shared review |
| **Shared workspace notes** | Shared/team workspace notes | `<workspace>/knowledge/` (manifest `shared_root`) | Ordinary commits; Asha only keeps it trackable | Cross-repository documentation as plain Markdown |

Harness-owned memory, such as Claude's auto-memory, is a separate sixth store.
Asha does not write it or depend upon it.

### Single repository

```bash
cd /path/to/repository
asha claude                         # or codex / copilot
/session:init                       # first use only
/session:save --no-push             # explicit checkpoint
```

Outside a workspace, use bare `/session:save`; workspace-only `--scope` flags
are rejected rather than silently reinterpreted.

### Multi-repository workspace

Initialize the workspace once from its common parent:

```bash
cd /path/to/workspace
asha workspace discover --root .    # inspect proposed child repositories
asha workspace init --root . --name example --repo child-a --repo child-b
asha workspace doctor --root .
```

For a component repository whose local workspace state must stay private, merge
`"memory_visibility": "private"` into that project's `.asha/config.json` before
workspace init or repair. Then run `asha workspace doctor --root . --fix` for an
existing workspace. This ignores `Memory/`, `Work/`, `knowledge/`, `memory-local/`
and `.asha/workspace*.json`; repair keeps them ignored. Omit the setting (or use
`"tracked"`) in the umbrella and ordinary projects to retain current behavior.
The setting is local to that project, does not propagate to children, and does
not untrack existing files or remove Git history. See
[workspace visibility](docs/memory-architecture.md#workspace-git-visibility).

Then choose the launch point by ownership:

```bash
cd /path/to/workspace/child-a
asha codex                          # work owned by child-a

cd /path/to/workspace
asha codex                          # cross-repository or workspace-owned work
```

Within a workspace:

```text
/session:save                 same as --scope repo; run inside a declared child
/session:save --scope repo    save only the active child repository's Memory/
/session:save --scope workspace
                              save only the workspace operational Memory/
/session:save --scope none    publish validated Memory without Git
```

The workspace root has no implicit active child. A repository-scoped save from
the root fails with guidance rather than guessing. Workspace SessionStart
context is bounded to the operational handoff; private `memory-local/` and
shared `knowledge/` notes are not dumped into every prompt.

The shared `knowledge/` folder is ordinary Markdown: workspace init creates it
empty, the ignore policy keeps it trackable (or ignored under private
visibility), and Asha neither lints nor writes it. Use `asha workspace --help`
and each leaf command's `--help` for exact flags. The complete ownership, read,
write, and save model is documented in
[Memory architecture](docs/memory-architecture.md).

---

## Installation

The legacy `/plugin marketplace add` flow is retired. Installation is now a direct symlink-mount via `./install.sh`. See **[INSTALLER.md](INSTALLER.md)** for the full model.

### Quick start

```bash
# Clone the repo somewhere stable (this path becomes the symlink source root)
git clone https://github.com/pknull/asha.git ~/some/dir/asha
cd ~/some/dir/asha

# Install primitives into all four harnesses + launch wrappers into ~/.local/bin
./install.sh --target all --bin all --default claude
```

### Selective install

```bash
./install.sh                              # ~/.claude/* only (default)
./install.sh --target codex               # ~/.codex/* only
./install.sh --target copilot             # ~/.copilot/* only
./install.sh --target opencode            # ~/.config/opencode/* only
./install.sh --only code,session          # restrict to specific plugins
./install.sh --dry-run                    # preview the action plan
```

### Verify installation

```bash
ls ~/.local/bin/asha*                     # wrappers (if --bin was used)
ls ~/.claude/skills/                      # claude-mounted skills
ls ~/.codex/skills/                       # codex-mounted skills
ls ~/.copilot/skills/                     # copilot-mounted skills
ls ~/.config/opencode/skills/             # opencode-mounted skills
asha doctor                               # install-health audit (drift-check front door)
```

### Launch

```bash
asha                       # default harness (set via --default; else claude)
asha codex                 # Codex with wrapper-scoped hot identity
asha claude                # Claude Code with wrapper-scoped hot identity
asha copilot               # Copilot with wrapper-scoped hot identity
asha opencode              # OpenCode with wrapper-scoped hot identity (requires >=1.15.11)
asha-codex                 # back-compat shim (== asha codex)
```

These launchers offer interactive configuration when the target is absent or
stale. Use `asha --yes <harness>` for an unattended first launch. Plain harness
commands remain persona-free but still see any installed skills and hooks.
Claude's native management subcommands are forwarded without persona injection
so their own TUI or one-shot behavior remains intact.

---

## Plugin Directory Structure

```
asha/
├── bin/                          # asha dispatcher, drift-check, env bootstrap
├── harnesses/                    # per-harness adapters (claude/codex/copilot/opencode)
├── identity/                     # persona system prompt + identity/operational merge scripts
├── lib/                          # install/uninstall/doctor/build/init-repo engines
├── namespaces.json               # plugin dir → command namespace map (panel → panel-system)
├── plugins/
│   ├── admin/                    # skills/ (bookstack, gemini, proton-mail, todoist, wolfram)
│   ├── asha/                     # compact identity templates + on-demand reference skill
│   ├── code/                     # development workflows and specialists
│   ├── image/                    # skills/ (fal, generation)
│   ├── panel/                    # panel workflows, characters, and templates
│   ├── rp/                       # live-roleplay lifecycle and continuity gates
│   ├── security/                 # skills/ (security-review)
│   ├── session/                  # Memory, policy, loop, and workspace tools
│   ├── test/                     # installer canary (ping command/skill/agent, stop hook)
│   └── write/                    # fiction, editorial, and style workflows
├── docs/                         # harness-enforcement.md, memory-architecture.md, …
├── tests/                        # validation suites + python unit tests
├── install.sh / uninstall.sh     # thin shims over lib/
├── README.md
├── CLAUDE.md
└── LICENSE
```

---

## Testing

Run the full test suite:

```bash
python3 -m pip install -r requirements.txt
./tests/run-tests.sh
```

### Test Coverage

| Suite | Description |
|-------|-------------|
| Plugin validation | Frontmatter, namespace, structure, and plugin-README version contracts |
| Python unit tests | Memory v2 schemas, recovery, learnings, workspace, and save scope |
| Hook handlers | Recovery callbacks, policy adapters, output contracts, and repository hygiene |
| Harness integration | OpenCode install/runtime bridge, Copilot build, doctor, uninstall, and init-repo |
| Shell + JavaScript | shellcheck and writing-engine behavior |

Individual test suites:

```bash
./tests/validate-plugins.sh    # Plugin configuration
./tests/validate-versions.sh   # Plugin README semver and namespace coverage
./tests/test-hooks.sh          # Hook handlers
python3 -m unittest discover -s tests/python -v  # Python tests
```

The authenticated Copilot runtime canary is opt-in because it sends one prompt
to the local Copilot CLI. It verifies the custom-instructions directory used
by `asha copilot`:

```bash
ASHA_LIVE_COPILOT=1 ./tests/test-copilot-live.sh
```

---

## Contributing

To propose new plugins or improvements:

1. Fork this repository
2. Create plugin in new subdirectory following structure
3. Add the directory → namespace mapping to `namespaces.json`
4. Run `./tests/run-tests.sh` to verify all tests pass
5. Submit pull request with documentation

---

## License

Individual plugins licensed separately. See each plugin's LICENSE file (MIT throughout: admin, asha, code, image, panel, rp, security, session, test, write).

---

## Support

**Issues and feature requests**: https://github.com/pknull/asha/issues

**Documentation**:

- Asha Control: [docs/control.md](docs/control.md); sessions:
  [docs/session-hub.md](docs/session-hub.md) and
  [docs/managed-sessions.md](docs/managed-sessions.md)
- Panel system: `plugins/panel/README.md`
- Code workflows: `plugins/code/README.md`
- Writing workflows: `plugins/write/README.md`
- Image generation: `plugins/image/README.md`
- Session & memory: `plugins/session/README.md`
- Development guide: `CLAUDE.md`

---

## Release history

The historical release record lives in [CHANGELOG.md](CHANGELOG.md). Plugin
versions live only in their owning plugin README files.
