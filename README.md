# asha

**Version**: 2.0.0
**Description**: A multi-harness agent toolkit. Persistent identity, session memory, and domain-focused plugins for Claude Code, OpenAI Codex, and GitHub Copilot CLI.

Asha mounts skills, agents, commands, and hooks into each harness via direct symlinks, ships a single `asha` dispatcher that injects a shared persona (and auto-configures a harness on first use), and consolidates session capture across all three CLIs into one synthesis pipeline.

---

## Install model: symlink-mount across three harnesses

Plugins live in `plugins/<name>/`. The installer symlinks the right primitives into each harness's scan directories:

| Harness | Mount root | Persona injection |
|---|---|---|
| **Claude Code** | `~/.claude/*` (skills, agents, hooks, settings.json entries) | `asha claude` injects via `--append-system-prompt-file` at launch |
| **OpenAI Codex** | `~/.codex/*` (skills as `.md`, agents, hooks) | `asha codex` injects via `-c model_instructions_file=<merged-identity>` at launch |
| **GitHub Copilot CLI** | `~/.copilot/*` (skills, agents) | `asha copilot` writes the merged identity and wires it per-launch via `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` (Copilot auto-loads `<dir>/.github/instructions/*.instructions.md`); plain `copilot` stays persona-free |

Install commands:

```bash
./install.sh                                # mount into ~/.claude/* (default target)
./install.sh --target codex                 # mount into ~/.codex/*
./install.sh --target copilot               # mount into ~/.copilot/*
./install.sh --target all                   # mount into all three
./install.sh --bin all --default claude     # install the asha dispatcher + harness shims in ~/.local/bin
./uninstall.sh                              # remove asha-tagged symlinks/entries
./deprecate-marketplace.sh                  # one-shot cleanup of legacy registration state
```

After `./install.sh --bin all` you'll have:

| Command | Effect |
|---|---|
| `asha` | launch the default harness (set via `--default`; else claude) |
| `asha <harness>` | launch `claude`/`codex`/`copilot` — auto-configures that harness on first use |
| `asha install <target>` | provision a harness (`claude`/`codex`/`copilot`/`both`/`all`) |
| `asha uninstall <target>` | remove Asha from a harness |
| `asha-claude` · `asha-codex` · `asha-copilot` | back-compat shims (each ≡ `asha <harness>`) |

Grammar is positional — `asha [install|uninstall] [harness] [args…]`. A verb *after* the harness is passed through, so `asha claude install` runs `claude install` (not the Asha installer).

See **[INSTALLER.md](INSTALLER.md)** for the full install model, per-harness limitations, and the bin/wrapper details.

---

## Harness support & behavior

Asha drives three agent CLIs from **one source corpus** (`plugins/<ns>/`). They don't support the same things, and each mounts the same primitive differently.

> **The full per-capability matrix — current status, mounting method, live-test findings, and caveats — is the single source of truth in [docs/harness-enforcement.md](docs/harness-enforcement.md).** This section explains *why* the behaviors differ (the mechanics, which rarely change); for current *status*, defer to that doc.

At a glance: skills, agents, persona, the operational layer, and `/save` capture work on **all three**; slash commands are remapped to skills on Codex/Copilot (no native command primitive); `/style` is **Claude-only**; PreToolUse guardrails enforce on **Claude and Copilot**, not Codex (its shell bypasses the hook). Codex also gets native execution-policy rules as a coarse approval fallback for a few high-risk commands.

### Why the behaviors differ

**Commands are *generated* for Codex/Copilot but *symlinked* for Claude.** A symlink is byte-identical to its source, so it only works when the artifact is already in the target harness's format. Claude commands carry Claude-only frontmatter (`argument-hint`, `allowed-tools`), and Codex/Copilot model everything as *skills* (no command primitive) whose parser rejects those keys. So a Claude command must be **translated** into a clean `SKILL.md` — keys stripped, `name`/`description` kept — which is a written copy, not a link. Skills and agents are already in a portable shape, so they're linked and edit live. Trade-off: editing a command source doesn't auto-propagate to Codex/Copilot — re-run `asha install <harness>`. (The generator bumps the dest mtime even when content is unchanged, so `drift-check`'s mtime comparison doesn't false-flag current command-skills.)

**Output styles are Claude-only.** `/style` and the 8 style files have no Codex/Copilot equivalent, so the `output-styles` plugin is in those harnesses' skip list — which is why a Claude install carries ~9 more symlinks than the others for the same corpus.

**Persona is injected three different ways** because each CLI exposes a different seam. Claude has `--append-system-prompt-file` (system-prompt priority). Codex has no such flag but accepts `-c model_instructions_file=` — a *single* merged file, so `identity-merge.sh` concatenates `~/.asha/{soul,voice,keeper,…}.md` plus the identity assertion at launch. Copilot has no injection *flag*, but its CLI auto-loads user-level instructions, so `asha copilot` regenerates the merged identity and exports `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` pointing at a cache dir whose `.github/instructions/asha.instructions.md` carries it — per-launch, like the other two, so plain `copilot` stays persona-free.

**The operational layer reaches all three.** `~/.asha/operation.md` + the learnings hot tier load via Claude's SessionStart hook; Codex and Copilot have no usable session-start hook, so the same content is delivered file-based — folded into Codex's `model_instructions_file`, and written as a second `asha-operational.instructions.md` in Copilot's instructions dir (both via `identity/operational-merge.sh`, same budgets as the hook). So persona *and* operational guidance are at parity across the three.

**Hooks: JSON for Claude, TOML for Codex, a dedicated JSON file for Copilot.** Claude reads `settings.json` (entries tagged `"source":"asha:<ns>"` for clean removal); Codex reads `config.toml` (a fenced `# asha:start … # asha:end` region using the current nested `[[hooks.Event.hooks]]` TOML shape) and doesn't support the `SessionEnd`/`Setup` events, which are dropped with a warning. For **Copilot**, capture is no longer hook-based (`/save` reads the native transcript — see Capture pipeline below), but the **PreToolUse guardrails now are**: `copilot_install_hooks()` writes a dedicated `~/.copilot/hooks/asha-guardrails.json` (Copilot loads every `*.json` there, so this never touches a user's own `hooks.json`) pointing at `copilot-policy-adapter.sh`. See the guardrails section below for the enforcement matrix.

**First launch requires the harness's own config to already exist** for Claude and Codex. Their installers deliberately refuse to fabricate `settings.json` / `config.toml` (the harness owns that file's format), so `asha claude` / `asha codex` against a never-initialized harness emits a clean *"run `<harness>` once first"* message instead of a confusing failure. Copilot bootstraps its own config, so it has no such precondition.

### Policy guardrails (PreToolUse deny/ask)

Beyond persona, Asha enforces **declarative tool-call policies** through a PreToolUse hook (`plugins/session/hooks/handlers/policy-guard.sh`). Rules live in `plugins/session/hooks/policies/rules.json` (+ an optional user layer `~/.asha/policies.json`, merged by `id` — user wins). Each rule matches a tool + a command/path regex and applies `deny`, `ask`, or a `max_per_session` rate limit (counted in session_state — see [State model](#state-model-guardrails-session_state-and-memory)), with an optional `override_env` escape hatch. The seed rule asks before broad `find`/`grep -r`/`bfs`/`fd`/`rg` scans over `/home` (slow HDD + Keybase I/O — Asha learning `no-broad-home-scans`, conf 0.95; override `ASHA_ALLOW_BROAD_SCAN=1`).

**Cross-harness enforcement status, the Copilot adapter mechanics, and the live-test caveats are in [docs/harness-enforcement.md](docs/harness-enforcement.md) (the single source of truth).** In short: enforced on Claude and Copilot (Copilot via `copilot-policy-adapter.sh` bridging to the same `policy-guard.sh`/`block-secrets.sh`), not on Codex. Codex gets `~/.codex/rules/asha.rules` as a native, prefix-based approval fallback for a narrow command subset; it is coarser than Asha's regex policy engine.

The engine is **fail-open** by design — any rule/parse error allows the call, because a guardrail must never brick tool use. And it is a **soft deterrent, not a sandbox**: it regex-matches the command string, so an agent can evade it deliberately (`cd /home && find .`, long flags, indirection), and on Copilot it can be bypassed under parallel tool calls. Pair it with the harness's own permission/sandbox controls for hard containment. This is the enforced form of the "Failure-to-Guardrail" idea: a high-confidence learning becomes a rule instead of prose a model can skip past.

See **[INSTALLER.md](INSTALLER.md)** for the per-harness layout diagrams and the full rationale.

---

## Capture pipeline: read native session transcripts on `/save`

Each harness writes its own session transcript to disk:

- Claude: `~/.claude/projects/<slug>/<sid>.jsonl`
- Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
- Copilot: `~/.copilot/session-state/<sid>/events.jsonl`

The session plugin no longer captures tool calls through hooks. `/save` reads the active session's native transcript via `plugins/session/tools/jsonl_reader.py`, normalizes events into the synthesizer's schema, and pattern_analyzer.py synthesizes `Memory/activeContext.md` and `~/.asha/learnings.md` updates. Hooks remain only for *intervention* (block-secrets, policy guardrails, post-edit-lint, prompt refinement, session-start context injection).

This makes capture work uniformly across all three harnesses, including Copilot — whose hooks fire but never receive the documented payload data, the original blocker behind the consolidation.

---

## State model: guardrails, session_state, and memory

Asha keeps three *distinct* kinds of state. They're easy to conflate but deliberately separate — the test that tells them apart: **session_state is meant to be thrown away at session end; Memory's whole purpose is to survive it.**

| Layer | Lifespan | Holds | Written by | Read by |
|---|---|---|---|---|
| **Policy guardrails** | static (rules) | `deny`/`ask`/limit rules (`plugins/session/hooks/policies/rules.json`) | you (edit rules) | `policy-guard` hook, per tool call |
| **session_state** | ephemeral (one session) | mechanical counters/flags (`~/.asha/session-state/<sid>.json`) | hooks, automatically | hooks, mid-session |
| **Memory** | durable (cross-session) | narrative knowledge + learnings (`Memory/*.md`, `~/.asha/learnings.md`, auto-memory) | `/save` synthesis, deliberate saves | session start, on-demand |

- **Guardrails** decide allow/deny/ask from the *current* tool call (a pattern match) — stateless on their own.
- **session_state** gives guardrails *memory within a single run*: e.g. a rule's `max_per_session` rate limit, or "you've done X N times this session." Volatile by design — cleared at session end (and TTL-swept), because a counter from yesterday must not affect today. It is **not** Memory: different lifespan, content, writer, and cadence (written every tool call by hooks, never at `/save`). It is working RAM, not the notebook.
- **Memory** is durable knowledge meant to *outlive* the session.

They form a pipeline, not an overlap: guardrails read session_state for in-flight decisions; when an ephemeral signal turns out to be a *recurring* pattern across sessions, `/save` can graduate it into a durable **learning** (Memory) — the "Failure-to-Guardrail" loop. session_state sits *below* Memory, feeding it, never duplicating it.

---

## Plugin Domains

| Domain | Plugin | Version | Purpose |
|--------|--------|---------|---------|
| **Research** | `panel-system` | v5.0.0 | Multi-perspective analysis, expert panels, decision-making |
| **Development** | `code` | v1.11.0 | Code review, orchestration patterns, TDD, 15 agents |
| **Creative** | `write` | v1.5.0 | Fiction writing, prose craft, perplexity detection, 16 agents |
| **Image** | `image` | v1.1.0 | Stable Diffusion prompts, ComfyUI workflows |
| **Automation** | `scheduler` | v0.1.0 | Cron-style scheduled task execution |
| **Formatting** | `output-styles` | v1.0.2 | Response styling and output formats |
| **Core** | `asha` | v2.0.0 | Session coordination, memory persistence, learnings |

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

- Chapter drafting with perplexity validation
- Style analysis from exemplar texts
- Manuscript revision workflows

**image** — When you need AI-generated images

- Stable Diffusion prompt engineering
- ComfyUI workflow design
- LoRA/model selection guidance

**scheduler** — When you need automated recurring tasks

- Daily code reviews
- Scheduled reports
- Automated maintenance

**asha** — Always (foundation)

- Session memory across conversations
- Cross-project identity via `~/.asha/`
- Confidence-tracked learnings that persist

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
**Commands**: `/code:review`, `/code:verify`, `/code:checkpoint`, `/code:orchestrate`
**Version**: 1.11.0
**Domain**: Development

Development workflows with orchestration patterns, code review, TDD, and 15 specialized agents.

```bash
/code:review              # Review staged changes
/code:review <path>       # Review specific file(s)
/code:review --all        # Review all uncommitted changes
/code:verify              # Run types, lint, tests, security
/code:checkpoint "name"   # Create named progress checkpoint
```

**Agents** (15):

| Agent | Role |
|-------|------|
| **architect** | System architecture and modular design |
| **build-error-resolver** | Build and TypeScript error resolution |
| **code-reviewer** | Code quality and security review |
| **codebase-historian** | Prior art discovery, pattern archaeology |
| **database-reviewer** | PostgreSQL optimization, RLS policies |
| **debugger** | Complex issue diagnosis, root cause analysis |
| **doc-updater** | Documentation sync from code structure |
| **e2e-runner** | Playwright E2E testing |
| **go-build-resolver** | Go compilation error specialist |
| **go-reviewer** | Idiomatic Go review |
| **javascript-pro** | Modern ES2023+ development |
| **python-pro** | Python 3.11+ type-safe development |
| **refactor-cleaner** | Dead code removal, cleanup |
| **tdd** | Test-driven development (London School) |
| **typescript-pro** | Advanced TypeScript development |

**Skills**: Django patterns, Spring Boot patterns, Go patterns, Python patterns, API design

**Recipes** (multi-agent workflows):

| Recipe | Use Case |
|--------|----------|
| `feature-implementation.yaml` | New features end-to-end |
| `bug-investigation.yaml` | Bug diagnosis and fix |
| `refactor-safe.yaml` | Code cleanup with safety |
| `security-audit.yaml` | Security hardening |

**[Full Documentation →](plugins/code/README.md)**

---

### Write

**Plugin Name**: `write`
**Commands**: `/write:perplexity`, `/write:init-novel`, `/write:review-section`
**Version**: 1.5.0
**Domain**: Creative Writing

Creative writing workflows with prose craft, perplexity detection, style analysis, and 16 specialized agents.

```bash
/write:perplexity chapter.md     # Check prose for AI flatness
/write:init-novel /path/to/proj  # Initialize novel state structure
/write:review-section            # Run periodic review suite
```

**Agents** (16):

| Agent | Role |
|-------|------|
| **outline-architect** | Story structure, beat sheets, chapter outlines |
| **prose-writer** | Draft generation with voice anchoring |
| **fiction-writer** | Primary creative coordinator for full pipeline |
| **consistency-checker** | Continuity tracking (characters, timelines, lore) |
| **developmental-editor** | Arc analysis, pacing, structural review |
| **line-editor** | Sentence craft, word choice, polish |
| **prose-analysis** | Multi-mode prose review (voice, continuity, coherence) |
| **intimacy-designer** | Adult content specialist (scene frameworks, boundaries) |
| **manuscript-editor** | Structural editing and revision coordination |
| **novel-character-reviewer** | Character consistency validation |
| **novel-continuity-reviewer** | Timeline, spatial logic, knowledge boundaries |
| **novel-state-updater** | State extraction after validation |
| **novel-style-linter** | Voice compliance, variance metrics |
| **book-analyzer** | Extract quantified style rules from exemplar texts |
| **bible-merger** | Consolidate multiple analyses into unified voice.md |
| **perplexity-improver** | Rewrite flat prose using VS-Tail sampling |

**Skills**:

| Skill | Purpose |
|-------|---------|
| **perplexity-gate** | Local prose flatness detection (Ollama + Ministral) |
| **style-analyzer** | Quantified prose analysis (sentence metrics, dialogue, vocabulary) |
| **novel-state** | Directory structure for manuscript state tracking |
| **languagetool** | Grammar and style checking via local server |
| **book-export** | Professional PDF/ePub export with styling profiles |
| **book-maker** | Python-based markdown converter |

**Recipes** (multi-agent workflows):

| Recipe | Use Case |
|--------|----------|
| `chapter-creation.yaml` | New chapter with perplexity gate |
| `manuscript-revision.yaml` | Complete revision of existing draft |
| `character-development.yaml` | Deep character creation with voice testing |

**[Full Documentation →](plugins/write/README.md)**

---

### Image

**Plugin Name**: `image`
**Version**: 1.1.0
**Domain**: AI Image Generation

Stable Diffusion prompt engineering and ComfyUI workflow design.

```bash
/plugin install image@asha
```

**Agent**: `comfyui-prompt-engineer`

- Image generation prompts from concept descriptions
- ComfyUI workflow JSON construction
- LoRA/model selection guidance
- Prompt iteration based on output feedback

**Usage**:

```
Design a prompt for: ethereal forest scene with bioluminescent mushrooms
Create a ComfyUI workflow for: txt2img with upscaling
```

**[Full Documentation →](plugins/image/README.md)**

---

### Scheduler

**Plugin Name**: `scheduler`
**Command**: `/schedule`
**Version**: 0.1.0
**Domain**: Automation

Cron-style scheduled task execution with natural language time expressions.

```bash
/schedule "Every weekday at 9am" "Review code changes since yesterday"
/schedule list                    # Show all tasks
/schedule show <id>               # Task details
/schedule remove <id>             # Delete task
/schedule logs <id>               # View execution output
```

**Time Expressions**:

| Expression | Cron Equivalent |
|------------|-----------------|
| "Every day at 9am" | `0 9 * * *` |
| "Every weekday at 9am" | `0 9 * * 1-5` |
| "Every Monday at 2pm" | `0 14 * * 1` |
| "Every hour" | `0 * * * *` |
| "Every 15 minutes" | `*/15 * * * *` |

**Security**:

- Default read-only mode (Read, Grep, Glob only)
- Max 10 tasks per project
- Dangerous command patterns blocked
- Audit logging for all operations

**[Full Documentation →](plugins/schedule/README.md)**

---

### Output Styles

**Plugin Name**: `output-styles`
**Command**: `/style`
**Version**: 1.0.2
**Domain**: Formatting

Switchable output styles for Claude Code responses.

```bash
/style                    # List available styles
/style <name>             # Switch to a style
/style off                # Disable styling
```

**Available Styles**:

| Style | Description |
|-------|-------------|
| ultra-concise | Minimal words, direct actions |
| bullet-points | Hierarchical bullet points |
| genui | Generative UI with HTML output |
| html-structured | Clean semantic HTML |
| markdown-focused | Full markdown features |
| table-based | Table-based organization |
| tts-summary | Audio TTS announcements |
| yaml-structured | YAML structured output |

---

### Asha

**Plugin Name**: `asha`
**Commands**: `/asha:init`, `/asha:save`, `/asha:prime`, `/asha:note`, `/asha:status`, `/asha:loop`, `/asha:spawn`, `/asha:agents`, `/asha:silence`, `/asha:restore`
**Version**: 2.0.0
**Domain**: Core Scaffold

Cognitive scaffold framework with cross-project identity, automatic learning, and session coordination. Foundation layer that other plugins build on. Learnings persist as an OKF concept bundle (`~/.asha/learnings/`, one file per learning) with auto-suggested `## Related` cross-links at `/save`; see [`docs/memory-architecture.md`](docs/memory-architecture.md).

```bash
/asha:init                # Initialize (creates ~/.asha/ + project Memory/)
/asha:save                # Synthesize session + extract learnings
/asha:prime               # Interactive codebase exploration
/asha:note "text"         # Add timestamped note
/asha:status              # Show session status
/asha:loop                # Start autonomous agent loop
/asha:spawn <agent>       # Spawn agent in tmux
/asha:silence             # Disable Memory logging
```

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
| `learnings.md` | Patterns with confidence tracking (0.3-0.9) |
| `config.json` | Cross-project settings |

**Project Layer** (`Memory/` — git-committed):

| File | Purpose |
|------|---------|
| `activeContext.md` | Current session state |
| `projectbrief.md` | Project foundation |
| `techEnvironment.md` | Tools and platform config |
| `workflowProtocols.md` | Project-specific patterns |

**Agents** (4):

| Agent | Role |
|-------|------|
| **partner-sentiment** | Haiku generation for session continuity |
| **task-manager** | Todoist integration for task retrieval |
| **verify-app** | Post-change verification (tests, types, lint) |
| **loop-operator** | Autonomous workflow with safety guardrails |

**Core Modules** (general techniques):

| Module | Purpose |
|--------|---------|
| `CORE.md` | Bootstrap protocol, identity, memory architecture |
| `cognitive.md` | ACE cycle, parallel execution, tool efficiency |
| `research.md` | Authority verification, citation standards |
| `memory-ops.md` | Session synthesis, Memory Bank maintenance |
| `high-stakes.md` | Safety protocols for destructive operations |
| `verbalized-sampling.md` | Mode collapse recovery, diversity generation |

---

## Installation

The legacy `/plugin marketplace add` flow is retired. Installation is now a direct symlink-mount via `./install.sh`. See **[INSTALLER.md](INSTALLER.md)** for the full model.

### Quick start

```bash
# Clone the repo somewhere stable (this path becomes the symlink source root)
git clone https://github.com/pknull/asha.git ~/some/dir/asha
cd ~/some/dir/asha

# Install primitives into all three harnesses + launch wrappers into ~/.local/bin
./install.sh --target all --bin all --default claude
```

### Selective install

```bash
./install.sh                              # ~/.claude/* only (default)
./install.sh --target codex               # ~/.codex/* only
./install.sh --target copilot             # ~/.copilot/* only
./install.sh --only code,session          # restrict to specific plugins
./install.sh --dry-run                    # preview the action plan
```

### Verify installation

```bash
ls ~/.local/bin/asha*                     # wrappers (if --bin was used)
ls ~/.claude/skills/                      # claude-mounted skills
ls ~/.codex/skills/                       # codex-mounted skills
ls ~/.copilot/skills/                     # copilot-mounted skills
./bin/asha-drift-check.sh                 # check symlink integrity
```

### Launch

```bash
asha                       # default harness (set via --default; else claude)
asha codex                 # Codex with Asha persona (auto-configures on first run)
asha claude                # Claude Code with Asha persona
asha copilot               # Copilot (persona requires per-project AGENTS.md symlink)
asha-codex                 # back-compat shim (== asha codex)
```

---

## Plugin Directory Structure

```
asha/
├── .claude-plugin/
│   └── marketplace.json          # Marketplace metadata
├── plugins/
│   ├── panel/                    # Research & analysis
│   │   ├── .claude-plugin/
│   │   ├── commands/
│   │   ├── agents/
│   │   └── docs/characters/
│   ├── code/                     # Development workflows
│   │   ├── .claude-plugin/
│   │   ├── commands/
│   │   ├── agents/ (15)
│   │   ├── skills/
│   │   └── recipes/
│   ├── write/                    # Creative writing
│   │   ├── .claude-plugin/
│   │   ├── commands/
│   │   ├── agents/ (16)
│   │   ├── skills/
│   │   └── recipes/
│   ├── image/                    # AI image generation
│   │   ├── .claude-plugin/
│   │   └── agents/
│   ├── schedule/                 # Task scheduling
│   │   ├── .claude-plugin/
│   │   ├── commands/
│   │   ├── agents/
│   │   ├── hooks/
│   │   └── tools/
│   ├── output-styles/            # Response formatting
│   │   ├── .claude-plugin/
│   │   ├── commands/
│   │   ├── hooks/
│   │   └── styles/
│   └── asha/                     # Core scaffold
│       ├── .claude-plugin/
│       ├── commands/
│       ├── hooks/
│       ├── modules/
│       ├── skills/
│       ├── templates/
│       └── tools/
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

| Suite | Tests | Description |
|-------|-------|-------------|
| Plugin Validation | 5 | JSON schema, namespace conflicts, file existence |
| Version Consistency | 6 | Cross-file version synchronization |
| Python Unit Tests | 78 | **jsonl_reader (32)**, learnings_manager_okf (25), pattern_analyzer merge/backup (15), silence_marker (6) |
| Hook Handlers | 104 | Lifecycle hooks, rules, tools, repo hygiene |
| Shell Linting | 1 | shellcheck (optional) |

**Total: 193 tests** (194 with shellcheck)

`jsonl_reader` tests pin the per-harness transcript-parser contract against committed fixtures (`tests/fixtures/{claude,codex,copilot}-*.jsonl`) so future host format changes fail loudly here rather than producing silently degraded synth output.

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
3. Update `.claude-plugin/marketplace.json`
4. Run `./tests/run-tests.sh` to verify all tests pass
5. Submit pull request with documentation

---

## License

Individual plugins licensed separately. See each plugin's LICENSE file.

- **Panel System**: MIT License
- **Code**: MIT License
- **Write**: MIT License
- **Image**: MIT License
- **Scheduler**: MIT License
- **Output Styles**: MIT License
- **Asha**: MIT License

---

## Support

**Issues and feature requests**: https://github.com/pknull/asha/issues

**Documentation**:

- Panel system: `plugins/panel/README.md`
- Code workflows: `plugins/code/README.md`
- Writing workflows: `plugins/write/README.md`
- Image generation: `plugins/image/README.md`
- Scheduling: `plugins/schedule/README.md`
- Development guide: `CLAUDE.md`

---

## Version History

### Unreleased — Codex compatibility refresh

- **Codex hook TOML now emits the documented nested schema** (`[[hooks.Event]]` matcher groups with nested `[[hooks.Event.hooks]]` command handlers) instead of the older flat shape.
- **Codex native execution-policy rules** — `asha install codex` writes `~/.codex/rules/asha.rules` with `prefix_rule()` prompts for narrow high-risk commands (`find /home`, `bfs /home`, destructive git). This is a coarse native fallback while PreToolUse remains unreliable for Codex shell.
- **Codex hook event list refreshed** — includes PreCompact/PostCompact/SubagentStart/SubagentStop, and unsupported Claude-only events still warn/drop.

### v1.19.0 (2026-06-24) — Cross-harness parity: persona, operational layer, Copilot guardrails

- **Copilot persona injection** — fixed (was wrongly "deferred / manual per-project"). `asha copilot` exports `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` at a cache dir whose `.github/instructions/asha.instructions.md` carries the merged identity; per-launch, so plain `copilot` stays persona-free. Verified live on CLI 1.0.63.
- **Operational layer on Codex + Copilot** — `operation.md` + the learnings hot tier now reach both (Claude loads them via the SessionStart hook; Codex/Copilot have no usable session-start hook). New `identity/operational-merge.sh`; folded into Codex's `model_instructions_file` and written as a second `asha-operational.instructions.md` for Copilot.
- **Guardrail re-tests** — Copilot 1.0.63 `preToolUse` **fires + denies** (the prior "won't pursue / unsafe" verdict was stale); Codex 0.142 still does **not** fire for shell (`unified_exec`, re-confirmed with a match-all hook + trust-bypass).
- **Copilot guardrails wired** — `copilot_install_hooks()` (was a no-op) writes a dedicated `~/.copilot/hooks/asha-guardrails.json` → new `plugins/session/hooks/handlers/copilot-policy-adapter.sh`, which bridges Copilot's hook contract (flat schema, stdout `permissionDecision`, stdin `toolName`/`toolArgs`) to the shared `policy-guard.sh` + `block-secrets.sh` — no policy logic duplicated. Soft deterrent (copilot-cli#2893, fails open). **Enforcement now: Claude ✅, Copilot ✅, Codex ✖ (upstream).**
- Docs: `docs/harness-enforcement.md` rewritten with the live findings; README + INSTALLER harness rows updated. Tests: `test-hooks.sh` Test 105 (adapter); suite 84 hook tests green.

### v1.18.0 (2026-06-17)

- **Dispatcher**: unified the three `asha-{claude,codex,copilot}` launchers into one positional `asha` dispatcher — `asha [install|uninstall] [harness] [args]`. Install/uninstall engines extracted to `lib/`; top-level `install.sh`/`uninstall.sh` are thin shims; `asha-<harness>` kept as back-compat shims.
- **Policy engine**: declarative PreToolUse guardrails (`plugins/session/hooks/handlers/policy-guard.sh` + `policies/rules.json`, optional user layer `~/.asha/policies.json`) — `deny`/`ask`/`max_per_session`, fail-open. Seed rule `no-broad-home-scans`. **Enforced on Claude**; Codex installs the hooks but does not fire them for shell tool calls (known gap — affects `block-secrets` too). Copilot has no hook seam. *(Corrected in v1.19.0 — the Codex gap is upstream `unified_exec`; Copilot guardrails are now wired + enforced.)*
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
