# asha

**Version**: 2.6.0
**Description**: A multi-harness agent toolkit. Persistent identity, session memory, and domain-focused plugins for Claude Code, OpenAI Codex, GitHub Copilot CLI, and OpenCode.

Asha renders or mounts skills, agents, commands, and hooks into each harness's native or compatible surfaces, ships a single `asha` dispatcher that injects a shared persona, and normalizes session activity from all four CLIs into one synthesis pipeline.

---

## Start here

Launch Asha from the directory whose context should own the work:

```bash
cd /path/to/repository
asha claude                    # or: asha codex / asha copilot / asha opencode
```

Inside a multi-repository workspace, launch from a declared child repository
for repository work. Launch from the workspace root for cross-repository
planning, shared operational memory, knowledge promotion, or coordinated
worktrees. See [Memory and workspace use](#memory-and-workspace-use).

Asha exposes four kinds of reusable surface:

| Surface | What it is | How to use it |
|---|---|---|
| **Command workflow** | An explicit multi-step operation such as review, save, or panel analysis | Claude/OpenCode: use the rendered slash command. Codex/Copilot: name the rendered skill or ask for the operation directly. |
| **Agent** | A bounded specialist used by a workflow or delegated directly | Usually let a command choose it. Name it explicitly only when you need that one role. |
| **Skill** | On-demand instructions and tools selected from task intent | Ask for the task naturally or name the skill. |
| **Recipe/engine** | A longer orchestration pattern behind a command | Start through the owning plugin's documented command; do not invoke internal files directly unless developing Asha. |

The root README is the map. Each plugin README owns its detailed usage,
examples, agents, skills, prerequisites, and safety boundaries:
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
| **Claude Code** | `~/.claude/*` (skills, agents, hooks, settings.json entries) | `asha claude` injects via `--append-system-prompt-file` at launch |
| **OpenAI Codex** | `~/.codex/*` (skill directories, TOML custom agents, hooks, rules) | `asha codex` injects via `-c model_instructions_file=<merged-identity>` at launch |
| **GitHub Copilot CLI** | `~/.copilot/*` (skills, agents) | `asha copilot` writes the merged identity and wires it per-launch via `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` (Copilot auto-loads `<dir>/.github/instructions/*.instructions.md`); plain `copilot` stays persona-free |
| **OpenCode** | `${XDG_CONFIG_HOME:-~/.config}/opencode/*` (skills, commands, agents, plugin) | `asha opencode` appends a merged instruction file through `OPENCODE_CONFIG_CONTENT`; plain `opencode` stays persona-free |

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
| `asha` | launch the default harness (set via `--default`; else claude) |
| `asha <harness>` | launch `claude`/`codex`/`copilot`/`opencode` — auto-configures that harness on first use |
| `asha install <target>` | provision a harness (`claude`/`codex`/`copilot`/`opencode`/`both`/`all`) |
| `asha uninstall <target>` | remove Asha from a harness |
| `asha-claude` · `asha-codex` · `asha-copilot` · `asha-opencode` | harness shims (each ≡ `asha <harness>`) |

Grammar is positional — `asha [install|uninstall] [harness] [args…]`. A verb *after* the harness is passed through, so `asha claude install` runs `claude install` (not the Asha installer).

See **[INSTALLER.md](INSTALLER.md)** for the full install model, per-harness limitations, and the bin/wrapper details.

**Upgrading an existing Codex, Copilot, or OpenCode install:** generated-file ownership is
new in this release. Run `asha install <harness> --force` once to adopt the
existing generated files into the ownership manifest before uninstalling or
using ordinary collision-safe updates.

---

## Harness support & behavior

Asha drives four agent CLIs from **one source corpus** (`plugins/<ns>/`). They don't support the same things, and each mounts the same primitive differently. First-class support means native rendering at each harness seam, not fake parity: see `harnesses/capabilities.json` for the machine-readable contract.

> **The full per-capability matrix — current status, mounting method, live-test findings, and caveats — is the single source of truth in [docs/harness-enforcement.md](docs/harness-enforcement.md).** This section explains *why* the behaviors differ (the mechanics, which rarely change); for current *status*, defer to that doc.

At a glance: skills, agents, persona, the operational layer, and manual `/session:save` capture work across all four harnesses, but through different forms. Asha command workflows are rendered as skills on Codex/Copilot and native command Markdown on OpenCode. Codex agents are generated TOML, Copilot agents are generated `.agent.md`, OpenCode agents are generated Markdown subagents, and Claude agents retain the source Markdown. Automatic clean-exit save and orphan recovery run on Claude and Copilot; OpenCode adds best-effort clean-exit save through plugin `dispose`. Codex remains manual-save only.

### Why the behaviors differ

**Commands are *generated* for Codex/Copilot/OpenCode but *symlinked* for Claude.** A symlink is byte-identical to its source, so it only works when the artifact is already in the target harness's format. Claude commands carry Claude-only frontmatter (`argument-hint`, `allowed-tools`). Codex and Copilot receive these workflows as skills. OpenCode has a native command surface, so it receives cleaned command Markdown. Agents are also rendered where the native shape differs: Codex gets TOML custom agents, Copilot gets `.agent.md`, OpenCode gets Markdown with `mode: subagent`, and Claude keeps the source Markdown. Trade-off: editing a command or agent source doesn't auto-propagate to generated copies; re-run `asha install <harness>`.

**Output styles are retired.** The former `output-styles` plugin (`/style` + 8 style files) was Claude-only by design and was retired in the 2026-07-10 ecosystem audit — Claude's native output-style switching covers the need, and the other harnesses never had an equivalent Asha seam.

**Persona is injected at each harness's real seam.** Claude uses `--append-system-prompt-file`; Codex uses `model_instructions_file`; Copilot uses `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`; OpenCode uses `OPENCODE_CONFIG_CONTENT.instructions`. Every mechanism is wrapper-scoped, so the plain harness remains persona-free.

**The operational layer reaches all four.** `~/.asha/operation.md` + a capped,
index-first view of `~/.asha/learnings/` load at session start. Learning bodies
are read on demand; `ASHA_LEARNINGS_INJECT=hot` restores the legacy full-body
hot-tier mode. Claude receives the layer through SessionStart, Codex through
`model_instructions_file`, Copilot through its custom instructions directory,
and OpenCode through the wrapper-scoped instructions array. Files are generated
by `identity/operational-merge.sh` with the same budgets.

**Hook surfaces are harness-native.** Claude uses JSON in `settings.json`; Codex uses nested TOML hook tables; Copilot uses dedicated hook JSON; OpenCode uses `plugins/asha.js`. Transcript capture is post-hoc, while policy adapters bridge each real-time hook contract to the shared rules.

**First launch requires the harness's own config to already exist** for Claude and Codex. Their installers deliberately refuse to fabricate `settings.json` / `config.toml` (the harness owns that file's format). Copilot and OpenCode use additive Asha-owned files and have no such precondition.

### Policy guardrails (PreToolUse deny/ask)

Beyond persona, Asha enforces **declarative tool-call policies** through a PreToolUse hook (`plugins/session/hooks/handlers/policy-guard.sh`). Rules live in `plugins/session/hooks/policies/rules.json` (+ an optional user layer `~/.asha/policies.json`, merged by `id` — user wins). Each rule matches a tool + a command/path regex and applies `deny`, `ask`, or a `max_per_session` rate limit (counted in session_state — see [State model](#state-model-guardrails-session_state-and-memory)), with an optional `override_env` escape hatch. The seed rule blocks broad `find`/`grep -r`/`bfs`/`fd`/`rg` scans over `/home` (rotational disk with background sync I/O — Asha learning `no-broad-home-scans`, conf 0.95; override `ASHA_ALLOW_BROAD_SCAN=1`). Rules are user-tunable, and this one is an example rather than a universal: adjust or drop it in `~/.asha/policies.json` if your `/home` is on SSD.

**Prefer `deny` over `ask` for rules that must bite.** An `ask` decision is auto-approved without surfacing a prompt in any session running an auto-accept permission mode, which makes the rule silently inert. `deny` (exit 2) blocks regardless of mode. The shipped rules use `deny` with `override_env` escape hatches for exactly this reason; `ask` remains in the schema for rules whose value is the prompt itself.

**Cross-harness enforcement status and caveats are in [docs/harness-enforcement.md](docs/harness-enforcement.md) (the single source of truth).** Claude and Copilot run Asha's policy hooks across their tested tool paths. OpenCode routes `tool.execute.before` through the shared policy, secret, and save-commit guards; an `ask` decision degrades to deny because no interactive permission response is verified at that seam. Codex can run the same hooks for supported simple Bash, `apply_patch`, and MCP calls, but official documentation explicitly says `unified_exec` shell interception is incomplete and hooks are not a complete enforcement boundary. Codex also gets `~/.codex/rules/asha.rules` as a native, prefix-based approval fallback for a narrow command subset.

The engine is **fail-open** by design — any rule/parse error allows the call, because a guardrail must never brick tool use. And it is a **soft deterrent, not a sandbox**: it regex-matches the command string, so an agent can evade it deliberately (`cd /home && find .`, long flags, indirection), and on Copilot it can be bypassed under parallel tool calls. Pair it with the harness's own permission/sandbox controls for hard containment. This is the enforced form of the "Failure-to-Guardrail" idea: a high-confidence learning becomes a rule instead of prose a model can skip past.

See **[INSTALLER.md](INSTALLER.md)** for the per-harness layout diagrams and the full rationale.

---

## Capture pipeline: read native session transcripts on `/session:save`

Each harness writes its own session transcript to disk:

- Claude: `~/.claude/projects/<slug>/<sid>.jsonl`
- Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- Copilot: `~/.copilot/session-state/<sid>/events.jsonl`

- OpenCode: `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db` (exact session rows from `session`/`message`/`part`)

The session plugin no longer captures tool calls through hooks. `/session:save` reads the active session's native transcript via `plugins/session/tools/jsonl_reader.py`, normalizes events into the synthesizer's schema, and pattern_analyzer.py synthesizes `Memory/activeContext.md` and `~/.asha/learnings/` updates. Hooks remain only for *intervention* (block-secrets, policy guardrails, post-edit-lint, prompt refinement, session-start context injection).

This gives all four harnesses a shared normalized event model.

---

## State model: guardrails, session_state, and memory

Asha keeps three *distinct* kinds of state. They're easy to conflate but deliberately separate — the test that tells them apart: **session_state is meant to be thrown away at session end; Memory's whole purpose is to survive it.**

| Layer | Lifespan | Holds | Written by | Read by |
|---|---|---|---|---|
| **Policy guardrails** | static (rules) | `deny`/`ask`/limit rules (`plugins/session/hooks/policies/rules.json`) | you (edit rules) | `policy-guard` hook, per tool call |
| **session_state** | ephemeral (one session) | mechanical counters/flags (`~/.asha/session-state/<sid>.json`) | hooks, automatically | hooks, mid-session |
| **Asha Memory** | durable (cross-session) | repository/workspace context, private workspace notes, canonical knowledge, and global learnings | `/session:save`, deliberate edits, reviewed promotion | session start, brokerage, on-demand |

- **Guardrails** decide allow/deny/ask from the *current* tool call (a pattern match) — stateless on their own.
- **session_state** gives guardrails *memory within a single run*: e.g. a rule's `max_per_session` rate limit, or "you've done X N times this session." Volatile by design — cleared at session end (and TTL-swept), because a counter from yesterday must not affect today. It is **not** Memory: different lifespan, content, writer, and cadence (written every tool call by hooks, never at `/session:save`). It is working RAM, not the notebook.
- **Memory** is durable knowledge meant to *outlive* the session.

Harness-native memory, such as Claude's auto-memory, is separate. Asha neither
writes it nor depends upon it.

They form a pipeline, not an overlap: guardrails read session_state for in-flight decisions; when an ephemeral signal turns out to be a *recurring* pattern across sessions, `/session:save` can graduate it into a durable **learning** (Memory) — the "Failure-to-Guardrail" loop. session_state sits *below* Memory, feeding it, never duplicating it.

---

## Plugin Domains

| Domain | Plugin | Version | Purpose |
|--------|--------|---------|---------|
| **Core** | `session` | v1.21.0 | Session memory, `/session:save` synthesis, `/session:consolidate` compaction, guardrail + guidance-nudge hooks, autonomous loops, workspace context + management CLI, evidence brokerage — 5 agents |
| **Identity** | `asha` | v2.1.0 | Persona templates (`soul.md`, `voice.md`) consumed by `/session:init` |
| **Research** | `panel-system` | v5.0.0 | Multi-perspective analysis, expert panels, decision-making — 6 agents |
| **Development** | `code` | v1.5.0 | Code review, orchestration patterns, TDD, overnight issue-to-merge loop — 5 agents |
| **Creative** | `write` | v1.9.0 | Fiction writing, prose craft, continuity, and style analysis — 10 agents |
| **Creative** | `rp` | v0.2.0 | Live-interactive roleplay: session lifecycle, per-turn continuity gating, canon ratification — 6 agents |
| **Image** | `image` | v2.0.0 | Stable Diffusion prompts, ComfyUI workflows (skill, no agents) |
| **Integrations** | `admin` | v0.3.0 | Direct skills: Todoist, Gemini search, Wolfram, BookStack, Proton Mail Bridge |
| **Security** | `security` | v1.0.0 | Web-app security review checklist skill |
| **Tooling** | `test` | — | Installer canary (`/test:ping` command/skill/agent) |

## Plugin guides

Current source inventory: **33 agents, 19 command workflows, and 15 skills**.
The owning guide below is the catalogue for each batch.

| Plugin | Primary entry point | Use it for | Detailed instructions |
|---|---|---|---|
| `session` | `/session:*` or rendered `session-*` skills; `asha workspace …` | Memory lifecycle, workspace management, guarded loops, context brokerage | [Session guide](plugins/session/README.md) |
| `panel-system` | `/panel-system:panel` or `panel-system-panel` | Decomposition, interviews, adversarial analysis, recorded decisions | [Panel guide](plugins/panel/README.md) |
| `code` | `/code:*` or rendered `code-*` skills | Implementation orchestration, debugging, review, verification, PostgreSQL work | [Code guide](plugins/code/README.md) |
| `write` | `/write:*` or rendered `write-*` skills | Fiction state, drafting workflows, editorial review, style analysis, export | [Write guide](plugins/write/README.md) |
| `rp` | `/rp:*` or rendered `rp-*` skills | Live roleplay lifecycle, continuity gates, canon ratification | [RP guide](plugins/rp/README.md) |
| `image` | `image-generation` skill | Stable Diffusion prompts and ComfyUI workflows | [Image guide](plugins/image/README.md) |
| `admin` | Name the required skill | Todoist, Gemini, Wolfram, BookStack, and Proton Mail operations | [Admin guide](plugins/admin/README.md) |
| `security` | `security-review` skill | Security-sensitive implementation and review | [Security guide](plugins/security/README.md) |
| `asha` | `asha <harness>` | Optional identity/persona layer | [Identity guide](plugins/asha/README.md) |
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
| **Global identity and learnings** | User, all projects | `~/.asha/` | Separate user-managed store | Identity, operation rules, preferences, confidence-tracked learnings |
| **Repository operational memory** | One repository | `<repo>/Memory/` | Committed with that repository | Current state, project brief, environment, repository workflow |
| **Workspace operational memory** | A group of repositories | `<workspace>/Memory/` | Committed in the manifest's `shared_git_root` | Cross-repository handoff, shared status, coordination decisions |
| **Private workspace memory** | User-local workspace material | `<workspace>/memory-local/` | Never commit | Private notes, work-item state, material not ready for shared review |
| **Canonical workspace knowledge** | Shared/team workspace knowledge | `<workspace>/knowledge/` | Explicit reviewed promotion; pull request by default | Stable cross-repository documentation and repository knowledge indexes |

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
/session:save --scope none    synthesize without staging, committing, or pushing
```

The workspace root has no implicit active child. A repository-scoped save from
the root fails with guidance rather than guessing. Workspace SessionStart
context is bounded to the operational handoff; private `memory-local/` and
canonical `knowledge/` bodies are not dumped into every prompt.

Canonical publication is deliberate:

```bash
asha workspace knowledge lint --start .
asha workspace promote plan --help
asha workspace promote apply --help
asha workspace promote publish --help   # reviewed branch + draft PR; never merge
```

Promotion commands require explicit review artifacts and confirmations. Use
`asha workspace --help` and the leaf command's `--help` for exact flags.
The complete ownership, read, write, and save model is documented in
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
asha codex                 # Codex with Asha persona (auto-configures on first run)
asha claude                # Claude Code with Asha persona
asha copilot               # Copilot with Asha persona (auto-injected per-launch)
asha opencode              # OpenCode with Asha persona (requires OpenCode >=1.15.11)
asha-codex                 # back-compat shim (== asha codex)
```

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
│   ├── asha/                     # templates/ (soul.md, voice.md) — identity only
│   ├── code/                     # agents/ (5), commands/ (4), skills/ (1), recipes/ (5)
│   ├── image/                    # skills/ (generation)
│   ├── panel/                    # agents/ (6), commands/ (panel.md), docs/characters/, templates/
│   ├── rp/                       # agents/ (6), commands/ (4), live-roleplay lifecycle
│   ├── security/                 # skills/ (security-review)
│   ├── session/                  # commands/ (7), agents/ (5), skills/ (2), workspace tools
│   ├── test/                     # installer canary (ping command/skill/agent, stop hook)
│   └── write/                    # agents/ (10), commands/ (2), skills/ (4), recipes/ (4)
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
| Plugin + version validation | Frontmatter, namespace, structure, and version contracts |
| Python unit tests | Transcript parsing, memory policy, learnings, synthesis, and save preflight |
| Hook handlers | Lifecycle hooks, policy adapters, output contracts, and repository hygiene |
| Harness integration | OpenCode install/runtime bridge, Copilot build, doctor, uninstall, and init-repo |
| Shell + JavaScript | shellcheck and writing-engine behavior |

`jsonl_reader` tests pin the supported Claude, Codex, Copilot, and OpenCode transcript
contracts so host format changes fail loudly rather than producing silently
degraded synthesis.

Individual test suites:

```bash
./tests/validate-plugins.sh    # Plugin configuration
./tests/validate-versions.sh   # Version consistency
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

Individual plugins licensed separately. See each plugin's LICENSE file (MIT throughout: admin, asha, code, image, panel, security, session, test, write).

---

## Support

**Issues and feature requests**: https://github.com/pknull/asha/issues

**Documentation**:

- Panel system: `plugins/panel/README.md`
- Code workflows: `plugins/code/README.md`
- Writing workflows: `plugins/write/README.md`
- Image generation: `plugins/image/README.md`
- Session & memory: `plugins/session/README.md`
- Development guide: `CLAUDE.md`

---

## Version History

### v2.6.0 / Session v1.21.0 — OpenCode stable-v1 support (2026-08-11)

- Reinstated OpenCode as a fourth harness against stable v1 (`>=1.15.11`):
  native plural `skills/`, `commands/`, `agents/`, and `plugins/` surfaces;
  wrapper-scoped identity; native JavaScript plugin hooks; installer,
  uninstaller, doctor, dispatcher, capability registry, and brokerage wiring.
- Added exact-session SQLite transcript synthesis from
  `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db`, including child to
  root session resolution and project-directory validation.
- Added `tool.execute.before` policy translation, buffered guidance through
  `experimental.chat.system.transform`, manual save, and best-effort clean-exit
  save through plugin `dispose`. No idle checkpointing is claimed.

### Documentation architecture sync (2026-08-08)

- Recast the root README as the installation, capability, plugin, and workspace
  map; detailed invocation and workflow instructions now live with each plugin.
- Replaced the pre-workspace memory model with explicit global, repository,
  workspace-operational, private-local, canonical-knowledge, and harness-native
  stores, including launch-point and save-scope examples.
- Synchronized command examples with installed namespaces and current
  Claude/Codex/Copilot/OpenCode save behavior.

### Session v1.20.0 — workspace management and evidence brokerage (2026-08-08)

Issues #23-#27 and #45-#50: bounded cross-harness workspace context,
source-aware retrieval, workspace bootstrap/doctor, reviewed canonical
knowledge promotion, coordinated multi-repository worktrees, private work-item
registry/adapters, and evidence-backed context/process/capability brokerage.

### Session v1.19.0 — copilot commit gate chained (2026-08-08)

Issue #40 (attended): `copilot-policy-adapter.sh` now carries the Copilot
payload's `cwd` through the Claude-shape translation (previously dropped —
the gate is cwd-sensitive, so without it the chain could not resolve the
project) and chains `save-commit-gate.sh` after policy-guard +
block-secrets. Deny (staged Memory, no proof), allow (hash-bound marker),
and self-filter (non-commit git) pinned through the translated payload in
Test 105. Live in-session deny probe deferred to the post-merge smoke —
the auto-mode classifier correctly refused a temporary live-hook redirect,
and the merged install needs no redirect at all. Copilot `workspace`
capability entry updated; upstream concurrency caveat (#2893) retained:
the gate is a deterrent layer, the writer-side proof remains primary.

### Workspace v1 complete — three-harness parity attested (2026-08-08, session v1.18.0)

Delivery issue 6 of 6 (issue #39) closes the ratified ship gate (decision 3,
PR #28). The first attestation attempt was **held in pass-2 review** (14th
consecutive batch): env-shaped probes are not harness-integration evidence,
the rendered codex/copilot save skills predated `--scope`, and the copilot
auto-save hole was unattested. The re-attestation runs everything under each
harness's REAL runtime (`codex exec` / `copilot -p` executing the probe
commands through their own shell tools in a fixture workspace):

- **Codex 0.147**: detection, save_scope proof round-trip, staged-set
  isolation, AND the commit-gate deny all verified live — the gate blocked an
  unproven Memory commit and consumed the proof on the allowed one. This
  **overturns the 0.142 "shell PreToolUse doesn't fire" verdict** (the
  re-probe the enforcement doc's version caveat demanded).
- **Copilot 1.0.75**: detection + proof + isolation verified live; the gate
  confirmed absent (ungated commit succeeded; proof survived unconsumed) —
  issue #40, attended.
- **Copilot auto-save hole closed** (the #36 deferral): a real sessionEnd
  auto-save was shown committing the workspace plane ungated; the automatic
  path now routes through a plane-aware writer seam
  (`tools/auto-commit-memory.sh`, Test 9d) — legacy no-manifest behavior
  preserved, workspace commits proof-bound + scope-staged + consume-on-use,
  manifest-present-but-unvalidatable fails closed. The gate cannot see
  hook-context commits on ANY harness, so the writer seam is the auto path's
  protection everywhere.
- `asha doctor` now prints each harness's `workspace` capability limitations
  inside its workspace section (Tests WS-12..14); `capabilities.json` entries
  rewritten from the probe verdicts (schema stays v3 per the proposal
  amendment recorded under issue #39); rendered codex/copilot `session-save`
  skills regenerated so `--scope` is reachable on their surfaces.

The workspace-memory proposal's v1 scope is now fully shipped: manifest
validator, detection consolidation + walk, status/doctor, save scopes +
plane gate, destructive-git cross-repo arm, auto-save writer seam, parity
attestation.

### Session v1.17.0 — save scopes + state-based commit gate (2026-08-08)

Workspace v1 delivery issue 4 (issue #36), design ratified via adversarial
consult. `/save` gains `--scope repo|workspace|none`; the writer-side seam
(`tools/save_scope.py`) resolves a scope into its plane mapping —
`plane_base` / `memory_root` / `commit_repo` as three distinct values —
writes a **versioned structured proof** at the plane, and verifies it
immediately before commit. `save-commit-gate.sh` becomes plane-aware by
**repository state, never command parsing** (a spoofed `-C` is irrelevant
when staged sets decide): a pure-bash existence walk keeps the no-manifest
path byte-identical at zero python cost (12-case golden corpus pins it);
manifest-present-but-unvalidatable **fails closed**; both-planes-staged
denies as ambiguous; each plane's proof satisfies only itself. The
Stop-hook net routes v2 locators to the plane's structural proof (the
session-transcript gates stay project-scoped by design). 17 save_scope
unit tests + Test 9c (19 gate pins), tests-first.

### Workspace v2 read side — bounded context + retrieval source (2026-08-08)

Sessions launched inside a valid workspace now receive one bounded background
block naming the workspace/root/active child and the first `##` section of the
workspace operational `Memory/activeContext.md`. The internal renderer is
`workspace_status.py --context`: no git enrichment, strict UTF-8, canonical
containment, delimiter sanitization, 2048-byte excerpt budget by default, and
zero output outside workspaces. `ASHA_WS_CONTEXT_MAX` changes the excerpt cap
(values below 256 or invalid values revert to 2048). `ASHA_WS_INJECT=0`,
`Work/markers/nudge-ws-context-off`, and `Work/markers/silence` disable delivery.

Claude and Codex are wired directly at SessionStart; installed end-to-end
delivery passed on Claude Code 2.1.226 and Codex 0.147. Copilot CLI 1.0.78 uses
the nudge engine upon native `sessionStart`, where its top-level
`additionalContext` delivery passed with the exact renderer block. The former
Codex/Copilot first-prompt fallback and 1 h cooldown are removed. Retrieval now
discovers the contained workspace operational plane as source `workspace`,
excludes it from ordinary project memory, and orders it after
`memory`/`learning` only on exact ranking ties.
The pinned issue-#49 oracle is in `test_memory_retrieval.py`; the user-owned
live recall bench remains advisory.

### Workspace v3-v6 management CLI — explicit writes, no implicit Git (2026-08-08)

The harness-independent dispatcher exposes the remaining workspace cores:

```text
asha workspace init|discover|doctor
asha workspace knowledge init|lint
asha workspace promote plan|apply|publish
asha workspace worktree create|status|remove
asha workspace work-item create|list|show|link|import|preview|lint|index|promote-plan|worktree-seed
```

The shell layer only routes arguments; each Python core remains the authority
for its exact flags (`--help`) and validation. Knowledge `plan` writes an
explicit review artifact bound to source, evidence, target preimage, and
digest. Pull-request plans also bind the shared Git root, base commit, and
credential-free GitHub repository identity. `apply` accepts only that artifact plus its digest and explicit
confirmation, then revalidates every preimage before writing. In
`pull-request` mode, `publish` requires the same artifact, digest, and explicit
confirmation; it refuses a dirty or ambiguous shared Git root, creates a
digest-named branch, stages only the reviewed knowledge write-set, commits,
pushes that branch, and opens a draft pull request. It never merges or
direct-pushes the base branch.
The shipped review adapter is GitHub CLI; other forges fail closed rather than
being treated as equivalent review infrastructure.
Repository commit/push hooks are disabled by default because they execute
project-local programs. `publish --run-git-hooks` is the separate explicit
authorization to run configured local governance hooks; the draft PR's remote
CI remains the external review boundary either way.
Worktree commands are explicit operations, branch deletion requires its
dedicated flag, and squash-merge cleanup requires review evidence. Work-item
import is offline and requires a scrubbed preview token; `worktree-seed` emits
data only and performs no Git operation.

### Session v1.16.0 + `asha workspace status` — first workspace consumer (2026-08-08)

Workspace v1 delivery issue 3 (issue #35). New dispatcher verb
`asha workspace status [--json] [--start DIR]` (thin `lib/workspace.sh`
shim; no new fallback chain — detection stays with the shared resolver) over
new `plugins/session/tools/workspace_status.py`: manifest validity with
typed errors verbatim, active child repository (cwd-resolved), per-repo
presence/branch/dirty state (reported, never assumed), `shared_git_root`
health, manifest trackedness. `asha doctor` gains the same as a section —
silent outside workspaces, fail-closed on an invalid manifest. Implements
the ratified open-question-1 decision: **manifest committed in
`shared_git_root` by convention**; untracked warns, invalid prints guided
repair (never auto-fix). Suite 15 (10 dispatcher/doctor cases) + 12 python
unit tests, tests-first.

### Session v1.15.0 — project-root consolidation + workspace walk (2026-08-07)

Workspace v1 delivery issue 2 (issue #33). The layered Memory-root detection
algorithm previously existed in **six divergent copies** (three bash, three
Python — no two byte-identical, five distinct layer orders); workspace
detection added to one would not have propagated. It now exists exactly once
per language: `tools/project-root.sh` and `tools/project_root.py`, with each
historical caller declaring its exact layer set, so per-consumer behavior is
byte-identical — pinned by the new Test 9b (12 detector-semantics pins,
green before AND after the rewire) and the existing Python suites. New
`detect_workspace()` primitive walks upward for `.asha/workspace.json`
(stopping exclusively before `$HOME` and `/`, canonical comparison, invalid
manifest = typed verdict, never silent fallthrough) — deliberately consumed
by NOTHING yet; issues 3–4 wire it. Audit: no independent layered fallback
chain remains; exempt-by-design sites (build-root detector in verify.py,
issue-loop's git-only refuse, thin command-MD one-liners, payload-cwd
hooks) are catalogued in PR #34.

### Session v1.14.0 — workspace manifest validator (2026-08-07)

Workspace v1, second of the six increments to land (proposal delivery
item 1, issue #31): `plugins/session/tools/workspace_manifest.py`, a pure lexical
parse/validate layer for `.asha/workspace.json` — typed collected errors,
fail-closed, schema defaults, traversal/absolute-path rejection, the
containment and disjointness rules, and the v1 `operational_root == Memory`
pin, with unknown keys preserved at every level. Deliberately
filesystem-free: worktree existence and symlink canonicalization land with
detection/status (issues 2–3). 38 table-driven tests in
`tests/python/test_workspace_manifest.py`, written RED-first.

### Session v1.13.0 — destructive-git cross-repo arm (2026-08-07)

First build increment of the ratified workspace-memory proposal
(`docs/proposals/2026-08-06--workspace-memory.md`, delivery issue 5 — landed
first because it is independent and closes a live gap): `git -C <dir> push
--force` and the other `-C`/`--git-dir`/`--work-tree` forms previously
evaded `destructive-git`, because the rule required the destructive verb to
directly follow `git` (one accident excepted: a path containing a `.git`
segment re-exposed a matching substring and denied by fluke). The rule now
consumes optional cross-repo global flags (quoted, attached, `=`, repeated,
and mixed-quoted forms); plain cross-repo commit/push stays allowed by
design (workspace saves depend on it). Pass-2 codex review hardened its own
fix batch (9th consecutive fix batch with confirmed defects): mixed-quoted
path tokens (`-C "$ROOT"/shared`) evaded the first arm, exclusions could be
laundered (`… push --force && echo --force-with-lease` allowed — exclusions
are now segment-scoped, the destructive-delete v2.4.0 fix class), and
backslash-newline continuations dodged per-line matching (the evaluator now
normalizes them). The issue-loop preflight gained a cross-repo `MUST_DENY`
probe so a user overlay carrying the pre-1.13.0 rule refuses dispatch. 21
Test 104 pins (`xr_*` + `multiline_ok`). Known residuals documented in the
rule: other global flags (`-c`, `--no-pager`) still bypass — widening is a
separate decision — commit messages quoting a guarded command false-positive
safe-side, and env-prefixed forms were already caught by unanchored
matching.

### v2.5.0 — Overnight issue-to-merge loop (2026-08-05)

Builds the deferred spec in `docs/proposals/2026-08-04--issue-to-merge-loop.md`: a dispatcher that triages open GitHub issues, fixes each safe one test-first in an isolated worktree, cold-reviews the diff, and opens **draft PRs only** — the human merges over coffee, the machine never merges. Code plugin v1.5.0.

- **Safety rails before features, and rails first in the build**: `issue-loop-preflight.sh` enforces a **dual opt-in** (the target repo commits `.asha/issue-loop.json` AND the repo path is allowlisted in `~/.asha/config.json` — a cloned repo cannot self-authorize, a local allowlist cannot enable a repo that never opted in), probes `gh` auth with clean surrender, requires `.asha/worktrees/` git-ignored, and — rail 6 made *runtime* — pipes the loop's own command set through the live policy guard (user overlay merged) before every dispatch: an allow-side deny **or a gutted deny-side protection** refuses to run. 20 cases in `tests/test-issue-loop.sh` (Suite 14).
- **"Never push main / never merge" is structural, not policy**: plain `git push origin main` stays intentionally allowed for humans (pinned in Test 104), so the loop's only push path is `issue-loop-publish.sh` — refuses main/master, foreign prefixes, unregistered worktrees, and dirty trees; hardcodes `--draft`. Worktree cleanup is `git worktree remove` only; the `rm -rf` form stays denied and is now pinned as such (7 new issue-loop cases in Test 104).
- **Engine** (`plugins/code/engines/issue-loop.js`, first engine in the code plugin — write/engines precedent): Triage → Iterate → Review → Publish → Report with commission-loop's verdict discipline — uncertainty fails, a dead agent fails its item, findings outrank the verdict label (the five-criterion triage conjunction is recomputed engine-side), silence is never success (thrown stages become indexed failure envelopes; the run report is mandatory, with a caller-side fallback duty). Worker contract: failing test FIRST or report "no-failing-test"; attempt cap then surrender with diagnosis; worktree evidence checked by the engine, not trusted from the label. The reviewer is **cold** — diff + issue text only, worker reasoning structurally withheld — and judges scope against the Change Budget rule. Wiring test: `tests/js/issue-loop.test.mjs` (13 scenarios).
- **v1 has no outward-write path to the tracker** (triage comments deliberately not built — rejections and needed clarifications live in the run report at `Work/loops/<run-id>/`, manual pruning), no auto-merge ever, and the loop does not run against asha itself until it has a track record on lower-stakes repos.

### v2.4.0 — Usage-insights remediation (2026-08-04)

Five repairs driven by a `/insights` review of 145 sessions. Each maps a recurring real-world failure to the mechanism that should already have carried it.

- **`destructive-delete` policy rule** (session v1.12.0) — `rm -r/-f`, `rm` of a glob or archive, `shred`, `gh repo delete` now deny by default; `destructive-git` gains `filter-repo`/`filter-branch`. Motivating incident: two `.7z` archives deleted before extraction, forcing re-download. Exemptions are deliberate and tested — `docker rm`, `git rm`, `npm rm`, `node_modules`, `.venv`, `/tmp` — because an over-broad rule gets disabled and then protects nothing. Override: `ASHA_ALLOW_DESTRUCTIVE_DELETE=1`. 19 new cases in Test 104.
- **Negative claims require an evidence trail** (`modules/research.md`) — the severity markers only ever covered *hedged* claims; confident assertions of absence ("no update exists", "no such file", "not version-controlled") attracted no marker and were the highest-frequency correction in the review. Negative findings now carry a `Checked:` line, with an authoritative-source table. Two rules generalize the specific incidents: **a pin is a claim, not evidence** (a branch/tag in config says what was selected, never what is available) and **a cache is not its source** (absence from an index means *not indexed*).
- **`roleplay-gm` made structurally read-only** (rp v0.2.0) — was `Task, Edit, Write, Bash`, now `Task, Read, Grep, Glob`. It had write access it was never instructed to use, and lacked the `Read` its own instructions required ("Read `Memory/invariants.md` at session start"). It drafted blind against the continuity contract while able to bypass it: the turn loop has the *calling command* append only on a clean verdict, so a GM that writes its own draft skips the gate entirely. Follows the `claim-verifier` allowlist-as-enforcement pattern. Also fixed a stale `rp-validator` reference (renamed to `continuity-reviewer` in v2.1.0).
- **Decline-once directive** in the per-turn RP routing fragment — a session was abandoned after refusals oscillated mid-scene and poisoned the context. Oscillation, not refusal, is the expensive failure: state the boundary once and hold it. Paired with an explicit ban on authoring PC actions.
- **RP portability** (rp v0.2.0) — the plugin's README promised the *nouns* stay in the project; the implementation contradicted it. Canon paths now resolve through a project-owned `Memory/canon-layout.md` register (template shipped, historical defaults preserved so existing projects need no edit). Campaign proper nouns removed from shipped primitives: the `rp-priced-stakes` `match_regex` carried one campaign's vocabulary (`doorman`, `mystic door`, `dollhouse`) and so fired only there, and `roleplay-gm`/`canon-writer` hardcoded a specific setting.

**Core de-personalization.** The same rule applied to the layer every project installs, where a foreign proper noun costs the most:

- **`no-broad-home-scans` was username-hardcoded** — its regex matched `/home(/pknull)?`, so on any other machine a full scan of `/home/<someone-else>` was **allowed**. The guard protected exactly one account. Now `/home(/[^/[:space:]]+)?`, denying for any user while leaving scoped paths (`/home/<user>/code`) allowed. Regression cases added to Test 104.
- **`recall_fixtures.yaml` shipped the maintainer's benchmark to every install** — `lib/install.sh` seeds it into each new `~/.asha/`, so a fresh user received twelve fixtures expecting memories (`project_egregore_setup`, `reference_home_network`) that could never exist for them. They score 0 forever, and the file's own comment explains the cost: *"A permanently impossible fixture would conceal real score regressions."* It then shipped twelve of them. Four also referenced the retired marketplace flow. Replaced with a documented, empty starter — `recall_bench` handles zero fixtures cleanly (`score … if cases else 0.0`), and existing `~/.asha/recall_fixtures.yaml` files are untouched because install only seeds when absent.
- **Personal names removed from core prose** — an "AAS vault" aside in `pattern_analyzer.py`'s RP calibration guard, and a `reference_pk_lintop_…` memory id used as the worked example in the memory-maintenance skill.
- **`vault-structure` content root widened** — was anchored on the literal `Vault/`; now matches any of `Vault|Lore|Wiki|Codex|Compendium|Archive`, with the same bucket taxonomy as the exclude. The root stays a *named* anchor deliberately: it is what scopes the rule, and a bare wildcard degenerates the trigger into "every write not in a bucket" — measured at 129/129 markdown files in this repo, `CLAUDE.md` and every doc included. Test 104c pins that blast radius while leaving the root list extensible. Projects whose content lives elsewhere redefine the row in `~/.asha/policies.json`.

**Adversarial review pass (2026-08-04, Codex as external reviewer + self-review).** The four remediation commits were themselves reviewed before release; 13 findings, all verified against the live guard before fixing. The ones that mattered: the v1 delete rule **denied the toolkit's own marker cleanup** (`rm -f Work/markers/…` in `/rp:end`, `/session:silence`, `/session:restore`) — a guard that blocks its own shipped workflows gets overridden into uselessness; an exemption string anywhere in a command suppressed the whole rule (`rm -rf important && mkdir -p /tmp/stage` was allowed), fixed by scoping path exemptions to the rm segment and giving archives their own prior rule with **no** path exemptions; quoted archives (`rm "backup.7z"`) slipped the terminator; `~`/`$HOME`/quoted forms — the most natural phrasings — bypassed the home-scan rule entirely; the priced-stakes nudge was case-sensitive (sentence-initial capitals never fired) and unbounded (`impact` fired via `pact`), fixed with a new opt-in `match_ci` engine flag plus word-boundary discipline; scene-state maintenance was orphaned by the roleplay-gm allowlist cut, redesigned as a `SCENE_STATE_DELTA` the GM emits and `/rp:turn` applies only on a clean verdict — state now rides the same gate as prose; `/rp:turn` and the priced-stakes fragment still hardcoded the stake register the canon-layout work was meant to own; and README's per-plugin detail sections carried versions two releases stale, outside `validate-versions.sh`'s net (now its Test 5). Residual gaps are documented in the rules' `_comment` fields rather than silently carried.

**Decisions round (same day).** Working the remaining report items to explicit rulings: **`commission-loop` engine** (write v1.9.0, `engines/commission-loop.js`) — the generalized adversarial commissioning harness: N workers draft one brief from cycled angles with every claim citing a source verbatim; per-draft verifier panels are instructed to refute (uncertainty fails, a dead verifier fails the draft); only survivors are ranked, rejects return with their findings, and the engine itself never writes a file (agents are no-write by instruction, or structurally via read-only `workerAgentType`/`verifierAgentType`) — promotion is the caller's explicit act. Wiring test executes the real engine body (`tests/js/commission-wiring.test.mjs`). Plus two module lines closing the last asha-shaped report items: a **change-budget scope contract** in `cognitive.md` (file list, per-file intent, and adjacent temptations surfaced separately as "out of scope, want it?" before 3+ file work) and **project-root-relative deliverable paths** in CORE.md Output Defaults. The overnight issue-to-merge loop was deliberately deferred with a captured spec (`docs/proposals/2026-08-04--issue-to-merge-loop.md`).

**Adversarial review, round 2 (2026-08-05, Codex again — this time over the fixes and the new engine).** 19 findings, every one verified live before fixing; the reviewer also *corrected this session's own diagnosis once* (the commission-loop erasure is the thrown-stage path, not agent-null). Headlines: the engine's "structural write boundary" claim was **false for workers** (docs now honest; `workerAgentType` added for a genuinely structural boundary); `SCENE_STATE_DELTA` rode *around* the continuity gate instead of through it (now a reviewed input with a `scene_state_mismatch` category, defined merge semantics, and application on the accept-anyway path — plus `/rp:end` refuses to canonize unaccepted surrender blocks); a user-layer policy override silently migrated to lowest priority (merge now replaces in place; Test 104d); archives hid behind adjacent operators and quoted metacharacters (`rm backup.7z&&ls`, `rm "backup&old.7z"`); `find ~alice` scanned another account's home unchallenged; a verifier returning `verdict:pass` alongside a hard finding survived (findings now outrank the label); ranker duplicates/partials could silently discard verified work; and the wiring-test mock diverged from real runtime semantics (throw→null now modeled). Residuals stay documented in `_comment` fields, not silently carried.

**`write` plugin de-specialized (v1.8.0 → v1.9.0 same cycle).** The last domain plugin carrying one project's material:

- **`prose-analysis` hardcoded a voice doc** — `Vault/Docs/MasterWritingStyleGuide.md`, with a "**Always** read MasterWritingStyleGuide.md first" best-practice and a "missing → request location" fallback. In any other project the glob resolved nothing and the agent proceeded on assumed voice standards. Now a convention search (`**/*StyleGuide*.md` among generic candidates), with an explicit declaration — a manifest's `slots.voiceSpec` or a user-given path — taking precedence, and a refusal to proceed on assumed standards when nothing resolves.
- **A project's canon shipped inside a generic agent** — a "Hush-Specific Checks" block listing coined transformation-anatomy terms and their body-location constraints. Replaced by the *generalizable* half: constrained-term checking, where a term carries a constraint (location, rank, material, direction) that prose drifts on before it drifts on the term itself, and where the negative cases must be written out because the wrong answers are the adjacent ones.
- **`craft-core-universal` profile mapping** — the `rp`/`hush` column table became a template for a project's own mapping, plus the three honest relationships a mapping can express (`enforced via core` / a named category / `cf.` for adjacent-but-not-identical) and guidance on what should *stay* profile-specific. The engine itself was already generic (`profileKey = a.profile || P.mode || 'custom'`, no built-in list); only its description string claimed `Profiles: rp | hush`.

### v2.3.0 — OpenCode support dropped (2026-07-27)

Operator decision following the #14 survey: OpenCode ≥1.18 moved session transcripts to sqlite, breaking Asha's memory capture — the system's value core — and the fix (a sqlite reader backend) was judged not worth carrying for the least-used harness. Asha is now a **three-harness** toolkit (Claude Code, Codex, Copilot CLI).

- Removed: `harnesses/opencode.sh`, the `asha-guardrails.js` emission + `opencode-policy-adapter.sh`, jsonl_reader's opencode backend, dispatcher/doctor/registry/capabilities wiring, the opencode test suite, and all opencode branches in shared handlers and save tools (session plugin v1.11.0).
- Live artifacts were uninstalled from the reference machine before the code was removed; other machines: check out a pre-2.3.0 tag and run `./uninstall.sh --target opencode`, or delete the asha entries under `~/.config/opencode/` manually.
- Retirement record with the final plugin-API survey verdicts preserved in `docs/harness-enforcement.md`; follow-up issues #16/#17 closed as not planned.

### OpenCode plugin-API survey — verdicts for all four parity questions (2026-07-27)

Closes issue #14 with the probe-first method (isolated `XDG_*` rig, instrumented plugin, local ollama model). Live verdicts in `docs/harness-enforcement.md` "Plugin API survey": the plugin surface is far richer than the one `tool.execute.before` hook asha used — `chat.message` fires per user prompt, a catch-all `event` stream delivers `session.idle`, and `experimental.chat.system.transform` injects. At that release, OpenCode's move to SQLite broke `/save`; v2.6.0 later supplied the SQLite backend and reinstated support. This entry remains the historical survey record.

### Session v1.10.0 — Copilot lifecycle: auto-save + orphan recovery (2026-07-27)

Closes issue #13 (wired on operator opt-in) — the last substantive Claude-parity gap on Copilot. Live-probed on 1.0.75, then verified end-to-end:

- **sessionEnd verdict** — fires on clean exit with `{sessionId, timestamp, cwd, reason}`; reasons observed live: `complete` (non-interactive `-p`) and `user_exit` (interactive `/exit`); SIGKILL fires nothing. sessionStart fires at first prompt submission and carries `initialPrompt`.
- **Lifecycle wiring** — new installer-generated `~/.copilot/hooks/asha-lifecycle.json`: sessionStart → `session-start.sh` (side effects only; context injection naturally discarded — the custom-instructions layer already injects), sessionEnd → `session-end.sh` (camelCase payload, copilot clean-exit reasons, detached save; `COPILOT_CLI=1` overrides inherited `ASHA_HARNESS`).
- **Crash-safe orphan trail** — copilot has no per-tool event capture, so sessionStart appends one identity breadcrumb event stamped with the harness uuid; a crashed session's work is recovered from its surviving native transcript at the next session start. Verified live: SIGKILL mid-session → recovered + synthesized next start.
- **False-orphan guard (all harnesses)** — a session whose `wwa-session` stamp is already published in activeContext.md is no longer re-recovered on every post-save session start.
- **Doctor + uninstall symmetry** — `asha doctor copilot` now byte-checks the guardrails, nudges (previously uncovered), and lifecycle files, `--fix` rewrites them; uninstall removes the lifecycle file. New `test_orphan_detection.py` unit suite.

### Session v1.9.0 — Codex PostToolUse verdict: fires but discards (2026-07-27)

Closes the verification gap from the 2026-07-26 codex hook work (issue #15). Four isolated-`CODEX_HOME` probes on codex 0.145, with the proven UserPromptSubmit injection as positive control:

- **Fires, but stdout is discarded** — PostToolUse fires for plain shell (`tool_name: "Bash"`, identical under `unified_exec = true`) and successful `apply_patch` (native `tool_name: "apply_patch"`); not for sandbox-rejected calls. Hook stdout never reaches the model or the session transcript in any shape — there is no PostToolUse injection channel.
- **Payload is Claude-shaped** — `hook_event_name` present (argument-free nudge-engine registration resolves correctly), plus full `tool_name`/`tool_input`/`tool_response`/`tool_use_id` for row gates.
- **`suggest-compact` harness-gated to claude+copilot** — an ungated row burned the shared tool-count and stamped the 2h cooldown for output codex discards, suppressing the nudge for a later Claude session. New Test 92f guards the gate (codex skipped with counter untouched; copilot still fires).
- **Bonus verdicts** — codex honors `matcher`, and aliases `apply_patch` into the `Edit|Write|MultiEdit` class (so post-edit-lint's registration does fire on codex file edits). Full verdict: `docs/harness-enforcement.md` "Codex PostToolUse".

### Session v1.8.0 — Learnings durability: migration decoy fix (2026-07-27)

Fixes issue #12: `migrate-okf` relocated the learnings store to `~/.asha/learnings/` while leaving the legacy flat file behind untouched — silently dropping users out of existing backup arrangements (e.g. a dotfiles symlink covering `learnings.md`) and leaving a stale decoy that makes a restore look successful.

- **Supersession banner** — after a successful migration, each legacy flat file is stamped (idempotently) with a banner + sentinel declaring it a frozen pre-migration snapshot; original content preserved verbatim below (still the rollback path). Stamping writes through symlinks (atomic replace of the resolved target), so an externally-tracked copy becomes self-describing as stale.
- **Backup-coverage warning** — migration (including `--dry-run`) now warns on stderr and in the report JSON when a legacy file is a symlink or resolves outside `~/.asha`: the bundle directory the store moved to is outside that arrangement's coverage.
- **`legacy-status` divergence check** — new `learnings_manager.py` verb, run warn-only by `/save` and `/session:consolidate`: flags an unstamped flat file next to the live bundle (the stale-decoy state where a restore would resurrect pre-migration data).
- **Durability documented** — the bundle is local-only by default; `docs/memory-architecture.md` "Durability & backup" states it and shows how to extend a dotfiles arrangement to cover `learnings/` + `learnings-archive/`.

### Session v1.6.0 — Copilot guidance-nudge parity (2026-07-26)

- Live-probed Copilot CLI 1.0.68's hook contract: events fire with no feature flag or trust gate, payloads carry no `hook_event_name` (argv registration instead — Copilot shell-splits), hook processes receive `COPILOT_CLI=1` + `CLAUDE_PROJECT_DIR`, raw stdout is discarded, and the ONLY injection channel is a top-level `{"additionalContext": ...}` JSON response (isolated by key: `systemMessage` et al. do nothing).
- Wired accordingly: `asha_harness()` detects Copilot via `COPILOT_CLI`; the nudge engine emits the additionalContext shape for copilot on every event; new `~/.copilot/hooks/asha-nudges.json` initially registered userPromptSubmitted + postToolUse (installed/uninstalled symmetrically). Production RP probe answered INJECTED. Workspace v2 later added the now-live-verified sessionStart registration. Full contract: `docs/harness-enforcement.md` "Copilot hook contract".
- Remaining Claude-parity gap, deliberately opt-in: sessionStart/sessionEnd side-effect wiring (orphan recovery + automatic clean-exit save).

### Codex hook enablement — feature gate, trust preservation, doctor coverage (2026-07-26)

- Live verification on codex 0.145 proved the asha hook fence works end-to-end (isolated `CODEX_HOME` replay: RP fragment reached the model) and exposed three defects, all fixed: the installer never set the required `[features] hooks = true` (now `_codex_ensure_hooks_feature` — adds when absent, never rewrites an explicit value); the fence excise destroyed codex's hash-bound `[hooks.state]` trust store on every reinstall (now preserved; Test 106d replays the failure; 11 production slots restored from backup); the doctor's codex hook-path check passed vacuously (crashed on `[hooks.state]`, silenced) — now walks nested commands and reports the feature gate + trust-slot count.
- Production codex hooks are enabled on the reference machine; full verdict in `docs/harness-enforcement.md` "Codex hook gating".

### Session v1.5.0 — Memory recall economics (2026-07-26)

Three disciplines ported from harness-native memory prompts into Asha's own stores; comparison in `docs/memory-architecture.md`.

- **Index-first injection** — SessionStart now injects one capped line per learning across the WHOLE bundle (`render-index`, hot-first, honest truncation tail) instead of the top-10 full bodies. Same byte budget, ~4× concept coverage; bodies Read on demand via the memory-lexical nudge. `ASHA_LEARNINGS_INJECT=hot` reverts.
- **`/session:consolidate`** — periodic four-phase compaction (orient → gather signal → consolidate → prune/index): merge drift, contradict disproven patterns, `retire` concluded records to `~/.asha/learnings-archive/` (new manager verb; full text preserved, out of every live surface), keeper.md calibration-log folding behind interactive confirmation, index-budget enforcement.
- **Broad-entry scrutiny** — BM25-style length normalization in `memory_retrieval.rank()` plus firing gates in the nudge: sprawling catalogue entries (≥25 tokens) are score-discounted, never fire on a lone rare token, and need three agreeing tokens. Live recall benchmark held at 12/13 hit@5.

### Session v1.4.0 — Declarative guidance-nudge engine (2026-07-25)

- New advisory counterpart to the policy guard: `hooks/handlers/nudge-engine.sh` evaluates declarative rows from `hooks/nudges/rules.json` (+ user layer `~/.asha/nudges.json`, merged by id) and injects context fragments — informational only, never blocking. Pattern extracted from severity1/claude-code-prompt-improver; its payload nudges were not adopted (largely absorbed by current harness behavior).
- Three ad hoc injections migrated to registry rows and their bespoke scripts retired: `memory-lexical` (was `hooks/memory_nudge.sh`), `rp-routing` (directive text now a single-source fragment, was inlined in `harness-response.sh` and emitted by `user-prompt-submit.sh`), `suggest-compact` (was `handlers/suggest-compact.sh`; cooldown is now engine-managed).
- Generic per-row gates (tool/regex/harness/marker/silence/init/cooldown), kill switches (`disable_env`, `Work/markers/nudge-<id>-off`), and priority-merged single-response output per event. Dynamic payloads via an allowlisted `nudge-builtins.sh` dispatch.
- Event resolved from the stdin payload's `hook_event_name`: argument-free registration survives hook runners that do not shell-split command strings (Codex TOML).
- Tests: engine coverage (RP routing claude/codex, kill switches, compact threshold/cooldown/silence, user-layer merge, harness/tool/env gates) + installer assertions retargeted; full suite green.

### Admin v0.3.0 — Proton Mail Bridge skill (2026-07-23)

- Added localhost-only Proton Mail Bridge administration through a stdlib Python helper: safe reads, structured search and triage, and hash-bound two-phase draft/send/move/delete operations.
- Enforced verified STARTTLS, UID-based message identity, native MOVE, move-to-Trash deletion, bounded MIME parsing, Bcc envelope privacy, and credential redaction.

### v2.2.0 — Ecosystem audit remediation (2026-07-22)

Full-project audit (goals, effectiveness, 88-script inventory, reachability) followed by fixes for all ten findings. Also rolls up the 2026-07-21 policy-guardrail work below.

- **Session v1.3.0** — dead memory-index feature removed (`post-tool-use.sh` no longer invokes the nonexistent `memory_index.py`; scaffolding template stops promising `memory_index.py`/`reasoning_bank.py`); orphaned `run-python.sh` deleted; `jsonl_reader.py` fully self-contained (no `~/life/bin` import path); save.md baseline capture resolved via `ASHA_BASELINE_CAPTURE`/config, not a personal path; both session skills documented.
- **Installer** — per-harness failure isolation in `asha_install_main` (one harness failing no longer aborts the rest; per-harness summary, non-zero exit on partial failure).
- **Code v1.4.0** — new `asha calibration` dispatcher verb makes `bin/calibration` reachable everywhere; orchestrate/complexity-routing docs use it; postgres skill documented.
- **Admin v0.2.0** — SKILL prose de-localized (repo paths resolve via `asha_root`, not `~/life/asha`).
- **Docs** — `~/life`/`~/life/marketplace` swept from all shipped prose (INSTALLER.md, secrets.md, memory-architecture.md, test-ping); README/CLAUDE.md version tables re-synced (session detail section had drifted to 1.0.0, write to 1.5.0/9-agents).
- **Tests** — shellcheck now covers `bin/ lib/ harnesses/ identity/` + root shims; `validate-versions.sh` cross-checks every plugin README version against both top-level tables; new `test-install.sh` (sandboxed round-trip incl. failure isolation) and `test-identity-merge.sh` (merge-script smoke) suites; bash-safety flags classified and annotated repo-wide.

### Policy guardrails made reachable and enforcing (2026-07-21, rolled into v2.2.0)

- **Session v1.2.0.** Two independent defects meant **all four policy rules were doing nothing**; both are fixed and verified live.
- **Fixed: file-path rules were unreachable.** `policy-guard.sh` was registered on `matcher: "Bash"` only, so `memory-protection` (`tool: "Write|Edit"`) and `vault-structure` (`tool: "Write"`) had never received a payload. Added a second `Edit|Write|MultiEdit` registration, claude-only via `_asha_harnesses` — on Codex `pretooluse_policy_ask` degrades to a hard deny, so a Codex-side registration would over-block. `memory-protection` has now fired for the first time.
- **Fixed: `ask` rules were inert.** An `ask` decision is auto-approved without surfacing a prompt under an auto-accept permission mode. `no-broad-home-scans`, `destructive-git`, and `memory-protection` converted `ask` → `deny`; each keeps its existing `override_env` escape hatch. `vault-structure` stays `warn` (log-only by design).
- **`memory-protection` exclude list corrected** — `scratchpad.md` and `ideas.md` added. `skills/memory-maintenance/SKILL.md` declares both "free-form, model-maintained", so the new `deny` would otherwise have hard-blocked documented behavior. The inert `ask` had hidden this.
- **Fixed: a pattern beginning with `--` was silently unmatchable.** Both evaluators called `grep -Eq "$pattern"` without an end-of-options guard, so `grep` parsed a leading `--` as an option. With `grep` being ugrep here, that exits rc=2, and the existing `2>/dev/null` made it indistinguishable from "no match" — a `command_regex` starting with `--` produced a silently dead rule, an `exclude_regex` starting with `--` produced silent over-blocking. All ten call sites in `policy-guard.sh` and `violation-checker.sh` now use `grep -Eq -- "$pattern"`. Guarded by new Test 104b.
- **`destructive-git` retargeted to match how work actually gets destroyed.** It permitted the operation an agent really reaches for while blocking the safe alternative. `--force-with-lease` is now **allowed** (it was blocked only because `--force` is a literal prefix of it); `git checkout -- <path>`, `git checkout .`, `git restore <path>`, and `git clean -f/-fd` are now **blocked**, with the reason pointing at `git stash` as the recoverable substitute. `git restore --staged` and `git rebase` stay allowed — the former does not touch the working tree, the latter is reflog-recoverable.
- **New Test 107 (rule reachability)** — asserts every rule's `tool` is covered by a registered `policy-guard.sh` matcher. Verified to fail against the previous wiring (naming all three dead rules) and pass against the new one. Test 104 previously could not distinguish `deny` from `allow`, since both produce empty stdout; its helper now reads the exit code.
- Caveat: `override_env` cannot be applied inline (`ASHA_ALLOW_DESTRUCTIVE_GIT=1 git push --force` will not work) — the hook reads its own environment, not the command's prefix. Set it in the session environment at launch.

### Unreleased — Save preflight hardening (2026-07-17)

- **Session v1.1.0** — new `save-preflight-env.sh` single-entry preflight: validated layered `ASHA_ROOT` detection (stale `config.json` caught at resolution, not five steps later), required-tool manifest check with a documented manual fallback (`docs/save-manual-pipeline.md`), and a hash-bound `save-gates-ok` marker.
- **New `disk_truth` gate** in `save_preflight.py` — disk is ground truth over Memory notes; `activeContext.md` references to nonexistent paths and future `lastUpdated` stamps are flagged as contradictions (warn-level).
- **New `save-commit-gate` PreToolUse hook** — mechanically refuses any `git commit` touching `Memory/` until all continuity gates pass; the marker is invalidated automatically if `activeContext.md` changes after gates pass. Override: `ASHA_ALLOW_UNGATED_MEMORY_COMMIT=1`. Memory commits under an active silence marker are refused outright.
- **Write v1.6.0** — new `claim-verifier` agent (structurally read-only via tool allowlist) + `verify-consistency-report.yaml` recipe: consistency reports are untrusted model output; rewrite-triggering claims get independently verified against the manuscript (not state files) into a confirmed/denied matrix before any revision proceeds.
- **Code v1.3.0** — new `fix-loop.yaml` recipe: test-gated autonomous fix loop over an issue backlog; unattended counterpart to `bug-investigation.yaml` with human checkpoints replaced by mechanical gates (reproduction-required, red-before-green, full-suite-plus-regression, revert-on-collateral) and a shipped/needs-input ledger.
- **Session modules** — ground-truth hierarchy rule (`live state > disk > notes`, correct the lower tier) in `memory-ops.md`; chunk-large-deliverables-to-files rule in CORE Output Defaults.

### Unreleased — Ecosystem audit prune (2026-07-10)

- **13 → 9 plugin namespaces** — schedule (scheduler), devops, prompt, and output-styles retired.
- **Agents 46 → ~23** — 15 removed, 7 consolidated/converted (write 17→10; code 15→5; database-reviewer → code `postgres` skill; image-engineer → image `generation` skill; book-maker absorbed into book-export).
- **Commands 23 → 14, skills 24 → 15** — `/asha:init` merged into `/session:init`; session spawn/agents/stop-agents/note/prime, code:checkpoint, partner-sentiment removed; verify-app folded into `/code:verify`.
- **Portable-first policy adopted** — a Claude-native equivalent is never sufficient grounds to remove a cross-harness component (reopened and kept: code:review, orchestrate stack, session:loop, code:verify, skill-creator, security-review).
- **Panel agents delegable** — all 6 gained frontmatter; vendored `fabricator` replaces the external agent-fabricator dependency.
- **ASHA_ROOT config fallback** — installer writes `asha_root` to `~/.asha/config.json`; commands/hooks resolve it under bare (non-dispatcher) launches.
- Full rulings: `Work/panels/2026-07-10--ecosystem-audit/`.

### Unreleased — Copilot-native distribution + doctor + init-repo (issue #3)

- **`asha build copilot`** — packages namespaces as native Copilot CLI plugins
  (`dist/copilot/`: per-plugin `plugin.json`, converted command-skills,
  `.agent.md` agents, marketplace index + `enabledPlugins` snippet). Verified
  live: local marketplace add → plugin install → skill fires under plain
  `copilot` (CLI 1.0.65). Hooks never packaged (copilot-cli#2540 + schema
  mismatch). Mechanism: [docs/distribution-copilot.md](docs/distribution-copilot.md).
- **`asha doctor`** — front door for `bin/asha-drift-check.sh`, now with a
  copilot target (symlinks, command-skill freshness, guardrails content,
  `--fix` self-heal), bin/identity sections, and a claude hook audit that
  matches by path-prefix (tag-stripped hooks are no longer invisible).
- **`asha init-repo`** — scaffolds `AGENTS.md`, team instruction stubs, and
  `.github/copilot/settings.json` into a target repo; `--check` CI mode with
  managed-marker DRIFT/LOCAL semantics; composes with native `copilot init`.
- **Persona remains wrapper-only by design** (issue #3 proposal 4 declined):
  `asha copilot` is Asha; plain `copilot` is vanilla — parity with `asha
  claude` vs `claude`.

### Unreleased — Codex compatibility refresh

- **Codex hook TOML now emits the documented nested schema** (`[[hooks.Event]]` matcher groups with nested `[[hooks.Event.hooks]]` command handlers) instead of the older flat shape.
- **Codex native execution-policy rules** — `asha install codex` writes `~/.codex/rules/asha.rules` with `prefix_rule()` prompts for narrow high-risk commands (`find /home`, `bfs /home`, destructive git). This is a coarse native fallback while PreToolUse remains unreliable for Codex shell.
- **Codex hook event list refreshed** — includes PreCompact/PostCompact/SubagentStart/SubagentStop, and unsupported Claude-only events still warn/drop.

### v1.19.0 (2026-06-24) — Cross-harness parity: persona, operational layer, Copilot guardrails

- **Copilot persona injection** — fixed (was wrongly "deferred / manual per-project"). `asha copilot` exports `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` at a cache dir whose `.github/instructions/asha.instructions.md` carries the merged identity; per-launch, so plain `copilot` stays persona-free. Verified live on CLI 1.0.63.
- **Operational layer on Codex + Copilot** — `operation.md` + the learnings hot tier now reach both. Codex supports `SessionStart`, but Asha uses the verified file-based `model_instructions_file` path for required context; Copilot receives a second `asha-operational.instructions.md`.
- **Guardrail re-tests** — Copilot 1.0.63 `preToolUse` **fires + denies** (the prior "won't pursue / unsafe" verdict was stale); Codex 0.142 still does **not** fire for shell (`unified_exec`, re-confirmed with a match-all hook + trust-bypass).
- **Copilot guardrails wired** — `copilot_install_hooks()` (was a no-op) writes a dedicated `~/.copilot/hooks/asha-guardrails.json` → new `plugins/session/hooks/handlers/copilot-policy-adapter.sh`, which bridges Copilot's hook contract (flat schema, stdout `permissionDecision`, stdin `toolName`/`toolArgs`) to the shared `policy-guard.sh` + `block-secrets.sh` — no policy logic duplicated. Soft deterrent (copilot-cli#2893, fails open). Historical test result: Claude ✅, Copilot ✅, Codex 0.142 tested shell path ✖; current Codex docs establish partial hook coverage beyond that path.
- Docs: `docs/harness-enforcement.md` rewritten with the live findings; README + INSTALLER harness rows updated. Tests: `test-hooks.sh` Test 105 (adapter); suite 84 hook tests green.

### v1.18.0 (2026-06-17)

- **Dispatcher**: unified the three `asha-{claude,codex,copilot}` launchers into one positional `asha` dispatcher — `asha [install|uninstall] [harness] [args]`. Install/uninstall engines extracted to `lib/`; top-level `install.sh`/`uninstall.sh` are thin shims; `asha-<harness>` kept as back-compat shims.
- **Policy engine**: declarative PreToolUse guardrails (`plugins/session/hooks/handlers/policy-guard.sh` + `policies/rules.json`, optional user layer `~/.asha/policies.json`) — `deny`/`ask`/`max_per_session`, fail-open. Seed rule `no-broad-home-scans`. Claude and Copilot enforcement are live-tested. Codex hooks can cover supported simple Bash, `apply_patch`, and MCP calls, but not every `unified_exec` shell path or every tool class; Asha's 0.142 shell probe did not fire.
- **session_state**: ephemeral per-session counters (`state.sh`, `~/.asha/session-state/`) that make policies stateful (rate limits); cleared at session end.
- **Docs**: new "Harness support & behavior" and "State model: guardrails, session_state, and memory" sections.

### v1.17.0 (2026-03-09)

- **Write v1.5.0**: Claude Book feature parity
  - 3 new agents: book-analyzer, bible-merger, perplexity-improver
  - style-analyzer skill (quantified prose analysis)
  - Total: 16 agents

### v1.16.0 (2026-03-08)

- **Write v1.4.0**: Novel-specific agents from AAS project
  - novel-character-reviewer, novel-continuity-reviewer
  - novel-state-updater, novel-style-linter

### v1.15.0 (2026-03-08)

- **Write v1.3.0**: Perplexity detection and novel state
  - perplexity-gate skill (local Ollama + Ministral)
  - novel-state skill (bible/state/timeline structure)
  - Removed ai-detector (replaced by local perplexity)

### v1.11.0 (2026-02-13)

- **Asha v1.18.0**: Confidence-tracked learnings
  - Learnings rise on confirmation, decay on contradiction
  - Secret scrubbing for event logs
  - ECC review integration

### v1.9.0 (2026-01-29)

- **Panel system v5.0.0**: Full persistence and panel management
  - `--resume <id>`: Continue interrupted panels
  - `--list [--status=X]`: Query panel index
  - Per-phase state files in `Work/panels/`
- **Asha v1.8.0**: Cross-project identity layer
  - `~/.asha/` for identity (soul.md, voice.md, keeper.md)
  - `/asha:save` captures keeper calibration

### v1.8.0 (2026-01-28)

- **Scheduler v0.1.0**: Cron-style task automation
  - Natural language time parsing
  - cron and systemd backend support
  - Rate limiting and security constraints

### v1.7.0 (2026-01-26)

- **Image v1.1.0**: AI image generation
  - comfyui-prompt-engineer agent
  - SD prompt crafting and workflow design

### v1.6.0 (2026-01-26)

- **Domain restructuring**: Organized by workflow type
- **Code v1.1.0**: Development workflows, 15 agents
- **Write v1.2.0**: Creative writing, prose craft

### v1.5.0 (2026-01-16)

- Fixed hook handler permissions
- Version validation script
- Asha v1.5.0 with robust memory indexing

### v1.3.0 (2026-01-07)

- Panel system v4.2.0 with --format and --context flags
- Audit and cleanup of stale references

### v1.0.0 (2025-11-08)

- Initial marketplace release
