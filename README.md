# asha

**Version**: 2.1.0
**Description**: A multi-harness agent toolkit. Persistent identity, session memory, and domain-focused plugins for Claude Code, OpenAI Codex, GitHub Copilot CLI, and OpenCode.

Asha renders or mounts skills, agents, commands, and hooks into each harness's native or compatible surfaces, ships a single `asha` dispatcher that injects a shared persona, and normalizes session activity from all four CLIs into one synthesis pipeline.

---

## Install model: native rendering across four harnesses

Plugins live in `plugins/<name>/`. The installer symlinks byte-compatible primitives and renders harness-specific forms where required:

| Harness | Mount root | Persona injection |
|---|---|---|
| **Claude Code** | `~/.claude/*` (skills, agents, hooks, settings.json entries) | `asha claude` injects via `--append-system-prompt-file` at launch |
| **OpenAI Codex** | `~/.codex/*` (skill directories, TOML custom agents, hooks, rules) | `asha codex` injects via `-c model_instructions_file=<merged-identity>` at launch |
| **GitHub Copilot CLI** | `~/.copilot/*` (skills, agents) | `asha copilot` writes the merged identity and wires it per-launch via `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` (Copilot auto-loads `<dir>/.github/instructions/*.instructions.md`); plain `copilot` stays persona-free |
| **OpenCode** | `~/.config/opencode/{skills,command,agent,plugin}` | `asha opencode` appends the merged identity through launch-scoped `OPENCODE_CONFIG_CONTENT`; plain `opencode` stays persona-free |

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

**Upgrading an existing Codex or Copilot install:** generated-file ownership is
new in this release. Run `asha install <harness> --force` once to adopt the
existing generated files into the ownership manifest before uninstalling or
using ordinary collision-safe updates.

---

## Harness support & behavior

Asha drives four agent CLIs from **one source corpus** (`plugins/<ns>/`). They don't support the same things, and each mounts the same primitive differently. First-class support means native rendering at each harness seam, not fake parity: see `harnesses/capabilities.json` for the machine-readable contract.

> **The full per-capability matrix — current status, mounting method, live-test findings, and caveats — is the single source of truth in [docs/harness-enforcement.md](docs/harness-enforcement.md).** This section explains *why* the behaviors differ (the mechanics, which rarely change); for current *status*, defer to that doc.

At a glance: skills, agents, persona, the operational layer, and manual `/save` capture work across all four harnesses, but through different forms. Asha command workflows are rendered as skills on Codex/Copilot, while OpenCode receives native files under `command/`. Codex agents are generated TOML, Copilot agents are generated `.agent.md`, OpenCode agents are generated native Markdown under `agent/`, and Claude agents retain the source Markdown. OpenCode memory is manual-save only because Asha has no OpenCode SessionEnd persistence hook.

### Why the behaviors differ

**Commands are *generated* for Codex/Copilot but *symlinked* for Claude.** A symlink is byte-identical to its source, so it only works when the artifact is already in the target harness's format. Claude commands carry Claude-only frontmatter (`argument-hint`, `allowed-tools`). Codex exposes built-in slash commands, but its documented reusable user workflow format is a skill, not a custom slash-command file. Copilot likewise receives these workflows as skills. A Claude command is therefore translated into a clean `SKILL.md`: keys stripped, `name`/`description` kept, with a harness adapter note. Agents are also rendered where the native shape differs: Codex gets TOML custom agents, Copilot gets `.agent.md`, and Claude keeps the source Markdown. Trade-off: editing a command or agent source doesn't auto-propagate to generated Codex/Copilot copies; re-run `asha install <harness>`. (The generators bump dest mtimes even when content is unchanged, so `drift-check`'s mtime comparison doesn't false-flag current generated artifacts.)

**Output styles are retired.** The former `output-styles` plugin (`/style` + 8 style files) was Claude-only by design and was retired in the 2026-07-10 ecosystem audit — Claude's native output-style switching covers the need, and Codex/Copilot never had an equivalent seam.

**Persona is injected at each harness's real seam.** Claude uses `--append-system-prompt-file`; Codex uses `model_instructions_file`; Copilot uses `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`; OpenCode receives an appended `instructions` path through `OPENCODE_CONFIG_CONTENT`. Every mechanism is wrapper-scoped, so the plain harness remains persona-free.

**The operational layer reaches all four.** `~/.asha/operation.md` + the learnings hot tier load via Claude's SessionStart hook. Codex receives them through `model_instructions_file`, Copilot through its custom instructions directory, and OpenCode through the same launch-scoped `instructions` entry as identity. Files are generated by `identity/operational-merge.sh` with the same budgets.

**Hook surfaces are harness-native.** Claude uses JSON in `settings.json`; Codex uses nested TOML hook tables; Copilot uses a dedicated `asha-guardrails.json`; OpenCode uses a JavaScript plugin with `tool.execute.before`. Transcript capture is post-hoc where possible, while policy adapters bridge each real-time hook contract to the shared rules.

**First launch requires the harness's own config to already exist** for Claude and Codex. Their installers deliberately refuse to fabricate `settings.json` / `config.toml` (the harness owns that file's format). Copilot and OpenCode use additive Asha-owned files and have no such precondition.

### Policy guardrails (PreToolUse deny/ask)

Beyond persona, Asha enforces **declarative tool-call policies** through a PreToolUse hook (`plugins/session/hooks/handlers/policy-guard.sh`). Rules live in `plugins/session/hooks/policies/rules.json` (+ an optional user layer `~/.asha/policies.json`, merged by `id` — user wins). Each rule matches a tool + a command/path regex and applies `deny`, `ask`, or a `max_per_session` rate limit (counted in session_state — see [State model](#state-model-guardrails-session_state-and-memory)), with an optional `override_env` escape hatch. The seed rule asks before broad `find`/`grep -r`/`bfs`/`fd`/`rg` scans over `/home` (slow HDD + Keybase I/O — Asha learning `no-broad-home-scans`, conf 0.95; override `ASHA_ALLOW_BROAD_SCAN=1`).

**Cross-harness enforcement status, the Copilot adapter mechanics, and the live-test caveats are in [docs/harness-enforcement.md](docs/harness-enforcement.md) (the single source of truth).** Claude and Copilot run Asha's policy hooks across their tested tool paths. Codex can run the same hooks for supported simple Bash, `apply_patch`, and MCP calls, but official documentation explicitly says `unified_exec` shell interception is incomplete and hooks are not a complete enforcement boundary. Codex also gets `~/.codex/rules/asha.rules` as a native, prefix-based approval fallback for a narrow command subset; rules govern commands that request execution outside the sandbox, not arbitrary tool calls.

The engine is **fail-open** by design — any rule/parse error allows the call, because a guardrail must never brick tool use. And it is a **soft deterrent, not a sandbox**: it regex-matches the command string, so an agent can evade it deliberately (`cd /home && find .`, long flags, indirection), and on Copilot it can be bypassed under parallel tool calls. Pair it with the harness's own permission/sandbox controls for hard containment. This is the enforced form of the "Failure-to-Guardrail" idea: a high-confidence learning becomes a rule instead of prose a model can skip past.

See **[INSTALLER.md](INSTALLER.md)** for the per-harness layout diagrams and the full rationale.

---

## Capture pipeline: read native session transcripts on `/save`

Each harness writes its own session transcript to disk:

- Claude: `~/.claude/projects/<slug>/<sid>.jsonl`
- Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- Copilot: `~/.copilot/session-state/<sid>/events.jsonl`
- OpenCode: `~/.local/share/opencode/storage/{session,message,part}/...`

The session plugin no longer captures tool calls through hooks. `/save` reads the active session's native transcript via `plugins/session/tools/jsonl_reader.py`, normalizes events into the synthesizer's schema, and pattern_analyzer.py synthesizes `Memory/activeContext.md` and `~/.asha/learnings/` updates. Hooks remain only for *intervention* (block-secrets, policy guardrails, post-edit-lint, prompt refinement, session-start context injection).

This gives all four harnesses a shared normalized event model. Claude, Codex, and Copilot retain their established capture paths; OpenCode reads its native directory storage during manual save.

---

## State model: guardrails, session_state, and memory

Asha keeps three *distinct* kinds of state. They're easy to conflate but deliberately separate — the test that tells them apart: **session_state is meant to be thrown away at session end; Memory's whole purpose is to survive it.**

| Layer | Lifespan | Holds | Written by | Read by |
|---|---|---|---|---|
| **Policy guardrails** | static (rules) | `deny`/`ask`/limit rules (`plugins/session/hooks/policies/rules.json`) | you (edit rules) | `policy-guard` hook, per tool call |
| **session_state** | ephemeral (one session) | mechanical counters/flags (`~/.asha/session-state/<sid>.json`) | hooks, automatically | hooks, mid-session |
| **Memory** | durable (cross-session) | narrative knowledge + learnings (`Memory/*.md`, `~/.asha/learnings/`, auto-memory) | `/save` synthesis, deliberate saves | session start, on-demand |

- **Guardrails** decide allow/deny/ask from the *current* tool call (a pattern match) — stateless on their own.
- **session_state** gives guardrails *memory within a single run*: e.g. a rule's `max_per_session` rate limit, or "you've done X N times this session." Volatile by design — cleared at session end (and TTL-swept), because a counter from yesterday must not affect today. It is **not** Memory: different lifespan, content, writer, and cadence (written every tool call by hooks, never at `/save`). It is working RAM, not the notebook.
- **Memory** is durable knowledge meant to *outlive* the session.

They form a pipeline, not an overlap: guardrails read session_state for in-flight decisions; when an ephemeral signal turns out to be a *recurring* pattern across sessions, `/save` can graduate it into a durable **learning** (Memory) — the "Failure-to-Guardrail" loop. session_state sits *below* Memory, feeding it, never duplicating it.

---

## Plugin Domains

| Domain | Plugin | Version | Purpose |
|--------|--------|---------|---------|
| **Core** | `session` | v1.1.0 | Session memory, `/save` synthesis, guardrail hooks, autonomous loops |
| **Identity** | `asha` | v2.1.0 | Persona templates (`soul.md`, `voice.md`) consumed by `/session:init` |
| **Research** | `panel-system` | v5.0.0 | Multi-perspective analysis, expert panels, decision-making — 6 agents |
| **Development** | `code` | v1.3.0 | Code review, orchestration patterns, TDD — 5 agents |
| **Creative** | `write` | v1.6.0 | Fiction writing, prose craft, continuity, and style analysis — 10 agents |
| **Image** | `image` | v2.0.0 | Stable Diffusion prompts, ComfyUI workflows (skill, no agents) |
| **Integrations** | `admin` | v0.1.0 | REST-direct skills: Todoist, Gemini search, Wolfram, BookStack |
| **Security** | `security` | v1.0.0 | Web-app security review checklist skill |
| **Tooling** | `test` | — | Installer canary (`/test:ping` command/skill/agent) |

### When to Use Each

**panel-system** — When you need multiple perspectives on a question

- Architecture decisions, trade-off analysis
- Creative brainstorming with diverse viewpoints
- Risk assessment, devil's advocacy

**code** — When you're building software

- Code review before commits
- Multi-agent feature implementation
- Bug investigation, refactoring, TDD

**write** — When you're writing fiction

- Chapter drafting with continuity and prose review
- Style analysis from exemplar texts
- Manuscript revision workflows

**image** — When you need AI-generated images

- Stable Diffusion prompt engineering
- ComfyUI workflow design
- LoRA/model selection guidance

**session (+ asha identity)** — Always (foundation)

- Session memory across conversations (`/session:save`)
- Cross-project identity via `~/.asha/` (asha templates, `/session:init`)
- Confidence-tracked learnings that persist (`~/.asha/learnings/`)

---

## Available Plugins

### Panel System

**Plugin Name**: `panel-system`
**Command**: `/panel`
**Version**: 5.0.0
**Domain**: Research & Analysis

Dynamic multi-perspective analysis with 3 core roles (Moderator, Analyst, Challenger) + dynamically recruited specialists. Full state persistence for resumption and audit.

```bash
/panel Should we implement GraphQL or REST for the new API
/panel --format=github "Review authentication approach"
/panel --context=docs/RFC.md "Evaluate this proposal"

# Panel management
/panel --list                    # List all panels
/panel --list --status=active    # Filter by status
/panel --resume <id>             # Resume interrupted panel
/panel --show <id>               # Display panel summary
/panel --abandon <id>            # Mark as abandoned
```

**Features**:

- 11-phase structured decision protocol
- Consensus tracking with percentage thresholds
- Output formats: markdown (default), github, json
- Context injection from files or URLs
- Dynamic specialist recruitment
- Full persistence with `--resume`, `--list`, `--show`, `--abandon`
- Per-phase state files in `Work/panels/` for audit trail

**[Full Documentation →](plugins/panel/README.md)**

---

### Code

**Plugin Name**: `code`
**Commands**: `/code:review`, `/code:verify`, `/code:orchestrate`
**Version**: 1.2.0
**Domain**: Development

Development workflows with orchestration patterns, code review, TDD, and 5 specialized agents.

```bash
/code:review              # Review staged changes
/code:review <path>       # Review specific file(s)
/code:review --all        # Review all uncommitted changes
/code:verify              # Run types, lint, tests, security
/code:orchestrate         # Multi-agent workflow (sequential + parallel phases)
```

**Agents** (5):

| Agent | Role |
|-------|------|
| **codebase-historian** | Prior art discovery — queries git history, Memory Bank, and the `~/.asha/learnings/` bundle before design work |
| **debugger** | Complex issue diagnosis, root cause analysis |
| **refactor-cleaner** | Dead code removal, duplicate consolidation, cleanup |
| **reviewer** | Code quality and security review (engine of `/code:review`) |
| **tdd** | Test-driven development (London School) |

**Skills**: `postgres` (query optimization, EXPLAIN analysis, schema design, RLS policies, migration safety — converted from the retired database-reviewer agent)

**Hooks**: `post-edit-lint` (auto-format/lint after edits)

**Recipes** (multi-agent workflows, with learnings recording):

| Recipe | Use Case |
|--------|----------|
| `feature-implementation.yaml` | New features end-to-end |
| `bug-investigation.yaml` | Bug diagnosis and fix |
| `refactor-safe.yaml` | Code cleanup with safety |
| `security-audit.yaml` | Security hardening |

**Also ships**: orchestration/complexity-routing/parallel-agents modules and harness instruction templates (`templates/copilot.md`, `cursor.md`, `devin.md`).

**[Full Documentation →](plugins/code/README.md)**

---

### Write

**Plugin Name**: `write`
**Commands**: `/write:init-novel`, `/write:review-section`
**Version**: 1.5.0
**Domain**: Creative Writing

Creative writing workflows with prose craft, style analysis, manuscript state, and 9 specialized agents.

```bash
/write:init-novel /path/to/proj  # Initialize novel state structure
/write:review-section            # Run periodic review suite
```

**Agents** (9):

| Agent | Role |
|-------|------|
| **outline-architect** | Story structure, beat sheets, chapter outlines |
| **prose-writer** | Draft generation with voice anchoring |
| **continuity-reviewer** | Manuscript continuity review and pre-writing gate |
| **developmental-editor** | Arc analysis, pacing, structural review |
| **line-editor** | Sentence craft, word choice, polish |
| **prose-analysis** | Multi-mode prose review: voice + quantified style lint, character consistency, continuity, coherence (absorbed novel-style-linter + novel-character-reviewer) |
| **intimacy-arbiter** | Adult-content arbitration — boundary rulings, heat-level consistency; review-only (slimmed from intimacy-designer) |
| **novel-state-updater** | State extraction after sections pass validation |
| **voice-analyst** | Voice bible pipeline: analyze exemplar texts + merge into unified voice.md (merged bible-merger + book-analyzer) |

**Skills**:

| Skill | Purpose |
|-------|---------|
| **style-analyzer** | Quantified prose analysis (sentence metrics, dialogue, vocabulary) |
| **novel-state** | Directory structure for manuscript state tracking |
| **languagetool** | Grammar and style checking via local server |
| **book-export** | Professional PDF/ePub export with styling profiles (absorbed book-maker's pandoc/font-embedding pipeline) |

**Recipes** (multi-agent workflows):

| Recipe | Use Case |
|--------|----------|
| `chapter-creation.yaml` | New chapter drafting and review workflow |
| `manuscript-revision.yaml` | Complete revision of existing draft |
| `character-development.yaml` | Deep character creation with voice testing |

**[Full Documentation →](plugins/write/README.md)**

---

### Image

**Plugin Name**: `image`
**Version**: 2.0.0
**Domain**: AI Image Generation

Stable Diffusion prompt engineering and ComfyUI workflow design. No agents — a single on-demand skill (converted from the retired image-engineer agent in the 2026-07-10 audit).

**Skill**: `generation` (installs as `image-generation`)

- Image generation prompts from concept descriptions
- ComfyUI workflow JSON construction
- LoRA/model selection guidance
- Prompt iteration based on output feedback
- Prompt templates for other generators (DALL-E, Midjourney, Runway, Sora)

**Usage**:

```
Design a prompt for: ethereal forest scene with bioluminescent mushrooms
Create a ComfyUI workflow for: txt2img with upscaling
```

**[Full Documentation →](plugins/image/README.md)**

---

### Session

**Plugin Name**: `session`
**Commands**: `/session:init`, `/session:save`, `/session:status`, `/session:silence`, `/session:restore`, `/session:loop`
**Version**: 1.0.0
**Domain**: Core

Session coordination and memory persistence — the foundation layer other plugins build on. Learnings persist as an OKF concept bundle (`~/.asha/learnings/`, one file per learning) with auto-suggested `## Related` cross-links at `/save`; see [`docs/memory-architecture.md`](docs/memory-architecture.md).

```bash
/session:init             # Initialize identity (~/.asha/) + project Memory/
/session:save             # Synthesize session + extract learnings
/session:status           # Show session status
/session:loop             # Start, resume, or manage autonomous agent loops
/session:silence          # Disable Memory logging
/session:restore          # Re-enable Memory logging
```

*(The former `/asha:init` identity phase, `session:spawn`/`agents`/`stop-agents`, `session:note`, `session:prime`, `task-manager`, and `verify-app` were merged or removed in the 2026-07-10 audit — verify lives on as `/code:verify`.)*

**Agent**: `loop-operator` — autonomous workflow management with safety guardrails (checkpoints, failure detection, intervention).

**Skills**: `memory-maintenance` (Memory file structure guidance), `skill-creator` (portable SKILL.md authoring).

**Hooks**: intervention + context injection (session-start, block-secrets, policy-guard, save-preflight, prompt refinement); capture is transcript-based via `/save` (see [Capture pipeline](#capture-pipeline-read-native-session-transcripts-on-save)).

**Core Modules** (general techniques):

| Module | Purpose |
|--------|---------|
| `CORE.md` | Bootstrap protocol, identity, memory architecture |
| `cognitive.md` | ACE cycle, parallel execution, tool efficiency |
| `research.md` | Authority verification, citation standards |
| `memory-ops.md` | Session synthesis, Memory Bank maintenance |
| `high-stakes.md` | Safety protocols for destructive operations |
| `verbalized-sampling.md` | Mode collapse recovery, diversity generation |

**Two-Layer Architecture**:

| Layer | Location | Purpose |
|-------|----------|---------|
| **Identity** | `~/.asha/` | Cross-project (who Asha is, who you are) |
| **Project** | `Memory/` | Per-project state, protocols, tech stack |

**Identity Layer** (`~/.asha/` — user-scope, persists across all projects):

| File | Purpose |
|------|---------|
| `soul.md` | Who Asha is (identity, values, nature) |
| `voice.md` | How Asha expresses (tone, patterns) |
| `keeper.md` | Who you are (preferences, calibration signals) |
| `learnings/` | OKF bundle — patterns with confidence tracking (0.3-0.9) |
| `config.json` | Cross-project settings, incl. `asha_root` (lets commands resolve `ASHA_ROOT` under bare launches) |

**Project Layer** (`Memory/` — git-committed):

| File | Purpose |
|------|---------|
| `activeContext.md` | Current session state |
| `projectbrief.md` | Project foundation |
| `techEnvironment.md` | Tools and platform config |
| `workflowProtocols.md` | Project-specific patterns |

**[Full Documentation →](plugins/session/README.md)**

---

### Asha

**Plugin Name**: `asha`
**Version**: 2.1.0
**Domain**: Identity

Templates-only plugin: ships the identity templates (`templates/soul.md`, `templates/voice.md`) that `/session:init` uses to provision `~/.asha/` when absent. It no longer carries commands or agents — `/asha:init` merged into `/session:init`, and `partner-sentiment` was removed (the session-threshold haiku ritual lives in `voice.md` and executes inline at `/save`). Persona launch is owned by the repo's `bin/asha` dispatcher.

**[Full Documentation →](plugins/asha/README.md)**

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
asha doctor                               # install-health audit (drift-check front door)
```

### Launch

```bash
asha                       # default harness (set via --default; else claude)
asha codex                 # Codex with Asha persona (auto-configures on first run)
asha claude                # Claude Code with Asha persona
asha copilot               # Copilot with Asha persona (auto-injected per-launch)
asha opencode              # OpenCode with Asha persona (auto-injected per-launch)
asha-codex                 # back-compat shim (== asha codex)
```

---

## Plugin Directory Structure

```
asha/
├── bin/                          # asha dispatcher, drift-check, env bootstrap
├── harnesses/                    # per-harness launch shims (claude/codex/copilot)
├── identity/                     # persona system prompt + identity/operational merge scripts
├── lib/                          # install/uninstall/doctor/build/init-repo engines
├── namespaces.json               # plugin dir → command namespace map (panel → panel-system)
├── plugins/
│   ├── admin/                    # skills/ (bookstack, gemini, todoist, wolfram)
│   ├── asha/                     # templates/ (soul.md, voice.md) — identity only
│   ├── code/                     # agents/ (5), commands/ (3), skills/ (postgres),
│   │                             #   hooks/, recipes/ (4), modules/, templates/, tools/
│   ├── image/                    # skills/ (generation)
│   ├── panel/                    # agents/ (6), commands/ (panel.md), docs/characters/, templates/
│   ├── security/                 # skills/ (security-review)
│   ├── session/                  # commands/ (6), agents/ (loop-operator), skills/ (2),
│   │                             #   hooks/, modules/, templates/, tools/
│   ├── test/                     # installer canary (ping command/skill/agent, stop hook)
│   └── write/                    # agents/ (10), commands/ (3), skills/ (5),
│                                 #   recipes/ (3), engines/, craft/, modules/
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
./tests/run-tests.sh
```

### Test Coverage

| Suite | Description |
|-------|-------------|
| Plugin + version validation | Frontmatter, namespace, structure, and version contracts |
| Python unit tests | Transcript parsing, memory policy, learnings, synthesis, and save preflight |
| Hook handlers | Lifecycle hooks, policy adapters, output contracts, and repository hygiene |
| Harness integration | Copilot build, doctor, uninstall, init-repo, and OpenCode native loading |
| Shell + JavaScript | shellcheck and writing-engine behavior |

`jsonl_reader` tests pin Claude, Codex, Copilot, and OpenCode transcript/storage contracts so host format changes fail loudly rather than producing silently degraded synthesis.

Individual test suites:

```bash
./tests/validate-plugins.sh    # Plugin configuration
./tests/validate-versions.sh   # Version consistency
./tests/test-hooks.sh          # Hook handlers
python3 -m unittest discover -s tests/python -v  # Python tests
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
