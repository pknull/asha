# Harness enforcement — capabilities & known failures

asha augments the native agent CLIs (Claude Code, OpenAI Codex, and GitHub
Copilot) at *their own seams*. (OpenCode support was dropped 2026-07-27 — see
the retirement record below.) Its features split cleanly by the seam they need:

- **File-based / post-hoc** (read a config/instructions file; post-process an
  on-disk transcript) → port to every harness, because every CLI does both.
- **Real-time interception** (a hook the CLI calls *before a tool runs* and
  *honors the decision*) → only works where the harness exposes a working hook.

Memory, persona, and the corpus are the first kind and work everywhere. The
**policy guardrails** (PreToolUse deny/ask) are the second kind — and that's
where the harnesses diverge. This document records documented capability
separately from empirical verification. Codex documentation was refreshed
2026‑07‑11; older live probes remain identified by their tested CLI version.

> **Correction (2026‑06‑24).** An earlier revision marked Copilot persona
> injection "manual per-project" and left the impression Copilot was the most
> limited harness. That over‑hedged: it hunted for an injection *flag*
> (Claude/Codex style) and missed that Copilot CLI auto-loads user-level
> instructions. Persona now injects automatically and is verified live (see the
> Copilot section). And the follow-on re-test (2026‑06‑24) went further: Copilot's
> **PreToolUse guardrails also work** on 1.0.63. The lone remaining divergence is
> **Codex's shell**, which bypasses the hook (`unified_exec`).

## Capability matrix

| Capability | Claude Code | OpenAI Codex (installed 0.144.1; docs current 2026‑07‑11; live hook probe 0.142) | GitHub Copilot CLI (1.0.63 baseline; later per-row probe versions noted inline) |
|---|---|---|---|
| Corpus mount (skills/agents) | ✅ native Markdown | ✅ skills + generated TOML custom agents | ✅ skills + generated `.agent.md` |
| Reusable command workflows | ✅ native user commands | ✅ Asha renders as skills; Codex slash commands themselves are built-in | ⚠️ converted to skills |
| Output styles | ✅ retired from asha (2026‑07‑10 audit) — Claude's native `/output-style` covers switching; the test canary style still mounts | ✖ n/a | ✖ n/a |
| Persona injection | ✅ (`--append-system-prompt-file`) | ✅ (`-c model_instructions_file`) | ✅ (`COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, per-launch) |
| Operational context (operation.md + learnings hot tier) | ✅ (SessionStart hook) | ✅ (folded into `model_instructions_file`, 2026‑06‑24) | ✅ (instructions file, 2026‑06‑24) |
| Memory capture (`/save` from native transcript) | ✅ | ✅ | ✅ |
| Lifecycle side effects (orphan recovery at start; automatic clean-exit save) | ✅ (SessionStart/SessionEnd hooks) | ✖ no wired path | ✅ **verified live 2026‑07‑27 on 1.0.75** (`hooks/asha-lifecycle.json`: sessionStart → session-start.sh side effects, sessionEnd → session-end.sh detached save; clean-exit reasons `complete`/`user_exit`; crash → orphan recovered from the native transcript at next start — see the Copilot lifecycle note below) |
| **PreToolUse guardrails (deny/ask)** | **✅ enforced** | **✅ enforced for shell on 0.147** (live probe 2026‑08‑08 via `codex exec`: `save-commit-gate` deny blocked a `git commit` before execution — "Command blocked by PreToolUse hook"; overturns the 0.142 shell probe that did not fire, exactly the re-probe the version caveat below demanded). Still ⚠️ documented-partial as a boundary: `unified_exec` interception incomplete per upstream docs — see the workspace probe note below | **✅ wired + enforced (1.0.63, via adapter; concurrency [#2893](https://github.com/github/copilot-cli/issues/2893) untested)** |
| Guidance nudges (advisory context injection via `nudge-engine.sh`, 2026‑07‑25) | ✅ all registry rows verified in tests (the PreToolUse `memory-lexical` row is Claude-only by design) | ⚠️ **UserPromptSubmit verified live + production-enabled 2026‑07‑26 on 0.145** (isolated `CODEX_HOME` replay of the real fence: UserPromptSubmit fired, RP fragment reached the model — probe answered INJECTED; `hook_event_name` present; argv shell-splitting confirmed; `[features] hooks = true` now installer-managed, trust store preserved across reinstalls — see the hook-gating note below). **PostToolUse fires but stdout is discarded — no injection channel for that event** (verified live 2026‑07‑27; see the PostToolUse note below); the `suggest-compact` row is harness-gated to claude+copilot accordingly | ✅ **verified live + wired through 1.0.78** (`hooks/asha-nudges.json`: sessionStart + userPromptSubmitted + postToolUse → nudge-engine with the Claude event name as argv; `sessionStart` top-level `additionalContext` and the existing prompt control both passed live — see the Copilot hook contract note below) |
| **Workspace v1 (detection, save scopes, commit gate, auto-save seam — 2026‑08‑08)** | ✅ **full**: shared tools + PreToolUse `save-commit-gate` plane enforcement (Test 9b/9c pins) + plane-aware `auto-commit-memory.sh` writer seam on the automatic path (Test 9d; the gate cannot see hook-context commits on ANY harness, so the writer seam is the auto path's protection) | ✅ **detection, writer-proof, staged-set isolation, and gate deny ALL verified under the real runtime** (`codex exec` probes 2026‑08‑08 on 0.147 — see the workspace probe note below; the gate consumed the proof on commit, proving gate participation live); ✖ no auto-save lifecycle (pre-existing) — manual save is the only path | ⚠️ **detection, writer-proof, and staged-set isolation verified under the real runtime** (`copilot -p` probes 2026‑08‑08 on 1.0.75); auto-save lifecycle wired and probed end-to-end; **commit gate chained 2026‑08‑08 (issue #40)** — `copilot-policy-adapter.sh` now carries the payload `cwd` and chains `save-commit-gate.sh` after policy-guard + block-secrets; deny/allow through the translated Copilot payload pinned in Test 105 and **verified live post-merge**: an equivalent commit attempt (same fixture, staged Memory, no proof) was **denied** ("Denied by preToolUse hook: BLOCKED by Asha policy [save-commit-gate]"); a valid-proof attempt was **allowed**; and the discriminating **stale-marker probe** (recorded provenance: CLI 1.0.78, installed adapter) was denied with the marker's own hash quoted against disk and the marker **deleted** — the gate demonstrably read and consumed the proof file, ruling out fail-open/bypass on the allow path. The writer-side save_scope proof and the auto-commit seam remain the primary protection (upstream concurrency caveat [#2893](https://github.com/github/copilot-cli/issues/2893)). |
| **Workspace v2 read side (bounded start context + source-aware retrieval — 2026‑08‑08)** | ✅ **live verified on Claude Code 2.1.226**: installed direct SessionStart delivered the exact renderer sentinel and `active=child`; repository gates remain test-pinned. | ✅ **live verified on Codex 0.147**: SessionStart raw-fragment candidate, prompt-event control, and exact renderer block all reached model context; the installed direct registration returned `CODEX-WORKSPACE-PASS`. Prompt fallback removed. | ✅ **live verified on Copilot CLI 1.0.78**: native `sessionStart` top-level `additionalContext` candidate, `userPromptSubmitted` positive control, and exact renderer payload all reached model context. Production `ws-context` moved to sessionStart; prompt fallback/cooldown removed. Raw sessionStart stdout is not claimed. |
| **Workspace v3-v6 CLI (knowledge, bootstrap, worktrees, work items — 2026‑08‑08)** | ✅ shared `asha workspace` dispatcher | ✅ shared `asha workspace` dispatcher | ✅ shared `asha workspace` dispatcher |
| Native command approval rules | n/a | ⚠️ `~/.codex/rules/asha.rules`; prefix-based, outside-sandbox execution policy | n/a |
| Native plugin packaging | Claude plugin model | ✅ `.codex-plugin/plugin.json` can bundle skills, hooks, MCP, apps, and assets; Asha direct installer does not yet use it | Copilot plugin build path implemented separately |

**Workspace v2 live attestation (2026-08-08 UTC):** repository tests prove
renderer bytes, UTF-8 caps/delimiter sanitization, canonical containment,
no-workspace zero output/no renderer startup, gates, and native response shapes.
Live probes then established model delivery rather than inferring it: Claude's
installed SessionStart returned `CLAUDE-SEES WORKSPACE-V2-LIVE-SENTINEL-8AUG
active=child`; Codex's candidate/control/exact checks were all `seen=yes`, and
the installed direct registration returned `CODEX-WORKSPACE-PASS`; Copilot's
sessionStart candidate, user-prompt control, and exact renderer checks were all
`seen=yes`. Copilot evidence applies to top-level `additionalContext` only—not
raw sessionStart stdout. Tested versions: Claude Code 2.1.226, Codex 0.147.0,
Copilot CLI 1.0.78.

Final repository provenance: `session-start.sh`
`869d552703ebc02036e2b6ee03e4020da1e1886363776e59e12fd8c01eb1d9d4`;
`nudge-engine.sh`
`e5f9b4b8bd3b7ab427469d745cbfa34f63f4a303cf669097c763a9a23ae4f55a`;
`nudge-builtins.sh`
`e19aec4c7c836e6ed2bed2cdfd8d6ecf7e7c5d7ab021e681e00b6f2449de9ca7`;
`workspace_status.py`
`cc4ae04ce14aba6321fa5f28b1cd52be40a85628f9df39ef660c73a8b9c0819d`.
The live probe preceded only the interpreter-source hardening in
`nudge-builtins.sh`; renderer and output-shape behavior did not change.

**Workspace v3-v6 execution boundary:** these are ordinary local CLI surfaces,
not model-delivery or hook claims, so availability is identical across harnesses
when the `asha` dispatcher is installed. The shell router passes native flags
to the Python cores. Promotion planning writes a digest-bound review artifact
with source/evidence/target preimages plus the reviewed Git root, base commit,
and credential-free GitHub repository identity. Confirmed apply accepts only
that artifact plus its digest, revalidates every preimage before writing, and
performs no Git operation. Confirmed `promote publish` for `pull-request` mode
creates a digest-named branch, verifies the exact staged and committed blobs,
pushes the exact commit to the bound repository, and opens a draft PR. It never
merges or updates the base branch. Repository commit/push hooks are disabled by
default; `--run-git-hooks` is a separate explicit authorization for reviewed
local hook programs. Remote CI remains the external governance boundary.
Worktree mutation is available only through explicit `create`/`remove`
commands. Work-item adapters remain offline, import requires scrubbed preview,
and worktree seed output is data-only.

**Codex hook gating (0.145, verified + fixed 2026‑07‑26):** codex runs a
configured hook only when BOTH `[features] hooks = true` is set in
`config.toml` AND the entry has persisted trust — hash-bound
`[hooks.state]` `trusted_hash` subtables codex writes itself (granted
interactively; `codex exec` offers a per-invocation
`--dangerously-bypass-hook-trust` for vetted automation). Without both,
every hook is silently skipped — no error, no log. Three defects found and
fixed the same day: (1) the installer never set the feature flag (test
fixtures pre-seeded it) — `_codex_ensure_hooks_feature` now adds it when
absent and never rewrites an explicit user value; (2) codex appends its
trust subtables mid-fence, and the old region-strip excise **destroyed
every trust grant on reinstall** (11 production slots wiped, restored from
backup) — the excise now preserves codex-owned `[hooks.state]` content,
replay-tested by Test 106d; (3) the doctor's codex hook-path check crashed
on `[hooks.state]` and passed vacuously via a silenced exception — it now
walks the nested command arrays and additionally reports the feature gate
and trust-slot count. The trust-wipe defect is also a plausible contributor
to the 0.142 probe non-fire recorded below, alongside the documented
`unified_exec` interception limits. End-to-end injection was proven by an
isolated-home replay of the real fence (probe answered INJECTED).

**Codex PostToolUse (0.145, verified live 2026‑07‑27):** four isolated‑`CODEX_HOME`
probes (`--dangerously-bypass-hook-trust`; UserPromptSubmit sentinel as the
positive control, which injected in every run). Findings: (1) **fires** — for
plain shell (`tool_name: "Bash"`, identical with `unified_exec = true`) and for
a successful `apply_patch` (`tool_name: "apply_patch"`, patch text in
`tool_input.command`); it does **not** fire for a tool call rejected by the
sandbox/approval layer. (2) **Payload** is the full Claude-shaped envelope —
`hook_event_name` present (argument-free nudge-engine registration resolves the
event), plus `tool_name`/`tool_input`/`tool_response`/`tool_use_id`/
`permission_mode`/`cwd`/`session_id`/`turn_id`/`transcript_path` — so all row
gates work. (3) **Hook stdout is discarded entirely**: neither raw text, the
legacy raw+`{}` shape, nor anything else reaches the model or even the session
transcript (zero sentinel occurrences); there is no PostToolUse injection
channel to emit for, so the correct adjustment was gating the `suggest-compact`
row (`harnesses: [claude, copilot]`) rather than changing the emission — an
ungated row burned the shared tool-count and stamped the 2h cooldown for output
codex discards, suppressing the nudge for a later Claude session. Bonus
findings: codex **honors `matcher`** (a `Edit|Write|MultiEdit` matcher filtered
the Bash call) and **aliases `apply_patch` into that matcher class** while
reporting the native tool name in the payload — so the production
post-edit-lint registration does fire on codex file edits, though its
Claude-style `tool_input.file_path` extraction sees a patch blob instead and
fail-opens.

**Copilot hook contract (1.0.68 through 1.0.78, verified live 2026‑07‑26 and
2026‑08‑08):** hook files under
`~/.copilot/hooks/*.json` fire without any feature flag or trust grant.
Payloads are `{sessionId, timestamp, cwd, prompt|initialPrompt, …}` — **no
`hook_event_name`** — so asha registrations pass the Claude event name as a
command argument (Copilot shell-splits the `bash` string; verified). Hook
processes receive `COPILOT_CLI=1`, `COPILOT_PROJECT_DIR`, and a
`CLAUDE_PROJECT_DIR` alias — `asha_harness()` uses the first for detection
under bare launches, and project detection works unmodified. Injection: raw
stdout is **discarded**; the only context channel is a top-level
`{"additionalContext": "…"}` JSON response, which the nudge engine emits for
copilot on every event. Advisory nudges are wired via
`hooks/asha-nudges.json` (sessionStart + userPromptSubmitted + postToolUse).
On 1.0.78, sessionStart top-level `additionalContext` delivered the exact
workspace renderer payload; raw sessionStart stdout remains discarded and is
not the delivery mechanism.

**Copilot lifecycle (1.0.75, verified live 2026‑07‑27 — issue #13, wired on
operator opt-in):** `sessionEnd` fires on clean exit with `{sessionId,
timestamp, cwd, reason}` — reasons observed live: `complete` (non-interactive
`-p` run) and `user_exit` (interactive `/exit`); a SIGKILL fires nothing.
`sessionStart` fires at the first prompt submission (payload carries
`initialPrompt`), not at TUI launch. Wiring (`hooks/asha-lifecycle.json`,
installer-generated, doctor-checked, uninstalled symmetrically): sessionStart →
`session-start.sh` (side effects only — raw-stdout context injection is
naturally discarded, deliberate since the custom-instructions layer already
carries the operational context; under copilot the session marker takes the
harness uuid from the payload and one identity **breadcrumb event** is
appended, because copilot has no per-tool capture and a crashed session would
otherwise leave no orphan trail); sessionEnd → `session-end.sh` (camelCase
payload + copilot clean-exit reasons handled; detached save; `COPILOT_CLI=1`
overrides any inherited `ASHA_HARNESS`). Verified end-to-end: clean exit →
`activeContext.md` synthesized from that session's transcript with all
provenance gates passing; SIGKILL mid-session → recovered and synthesized from
the surviving native transcript at the next session start. A published-session
guard (wwa-session stamp) prevents the pre-existing false-orphan re-synthesis
after every clean save — that fix applies to Claude too.

Guardrails enforce across the tested Claude and Copilot paths (Copilot
single-call deny verified live on 1.0.63, 2026‑06‑24). Codex has a real native
hook system, not an absent one, but its coverage is incomplete. Official docs
state that simple Bash, `apply_patch`, and MCP calls can be intercepted whilst
some richer `unified_exec` shell calls and non-shell/non-MCP tools cannot. The
Asha's 0.142 shell probe landed in the uncovered case and did not fire; the
2026‑08‑08 re-probe on 0.147 fired and denied (see the workspace probe note
below), so simple-Bash interception is now verified live there. The
file-based layers — corpus, persona (all three; Copilot persona fixed
2026‑06‑24), and the operational layer (operation.md + learnings; Copilot +
Codex both wired 2026‑06‑24 — file-based, no working hook required) — work on all
three CLIs. Note: Asha's user-defined command workflows are remapped to skills
on Codex/Copilot. Codex does have built-in slash commands, but no documented
custom command-file surface. The `output-styles` plugin was retired in the
2026‑07‑10 ecosystem audit (Claude's native `/output-style` covers it). Codex
also gets native execution-policy `prefix_rule()` prompts for a narrow subset of
high-risk shell commands; these are not equivalent to Asha's regex guardrails.

**Workspace probe note — v1 parity battery (2026‑08‑08, issue #39):** all
probes ran under each harness's REAL runtime (the harness executing the
commands through its own shell tool in a fixture workspace: shared root `ws`
with a declared child repo), not env-shaped plain shells — the distinction the
first attestation attempt failed on. Fixture and full transcripts preserved in
the session logs.

- **Codex 0.147 (`codex exec --sandbox workspace-write`):** `asha workspace
  status` resolved `workspace: probews … active repository: child`;
  `save_scope.py resolve --scope workspace` returned the full v2 mapping.
  Save pipeline: write-proof → `verify` → `ok` → scoped commit staged ONLY
  `Memory/probe-note.md` in the shared root; the dirty child repo and a dirty
  non-Memory file were untouched; the proof marker was **consumed by the
  gate** (present before the commit, gone after — live gate participation).
  Deny probe: with staged Memory and no proof, `git commit` was **blocked
  before execution** ("BLOCKED by Asha policy [save-commit-gate]").
- **Copilot 1.0.75 (`copilot -p --allow-all-tools`), PRE-FIX baseline:**
  same detection and save-pipeline results (scoped commit
  `Memory/copilot-note.md` only; child untouched) — but the proof marker
  **survived** the commit (nothing consumed it), and the deny probe
  **succeeded** (`git commit` exit 0 on staged Memory with no proof): the
  commit gate **was not yet chained** at probe time. That gap is what issue
  #40 closed; the post-merge chained-gate verdict lives in the workspace
  matrix row above (deny + stale-marker-consumed probes, CLI 1.0.78).
- **Copilot auto-save, end-to-end:** a real `copilot -p` session at the
  workspace root with `autoCommit: true` exited cleanly; sessionEnd → detached
  save → preflight passed on the real transcript → the **pre-seam stanza
  committed the workspace Memory plane ungated and unproven** ("Session
  auto-save:" with no scope, no proof) — the #36-deferral hole demonstrated
  live. The same fixture and session identity through the seam-fixed writer
  produced "Session auto-save (workspace):" — plane resolved, proof written,
  verified and consumed, only `Memory/` staged, child untouched (Test 9d pins
  the full conduct matrix, including fail-closed on unvalidatable manifests).
  **Full-chain smoke (same day):** a real copilot session in a PRISTINE
  workspace fixture, clean exit, the installed unmodified sessionEnd hook,
  the seam writer selected via `CLAUDE_PLUGIN_ROOT` — preflight passed on the
  real transcript and the seam committed "Session auto-save (workspace):"
  with only `Memory/` staged; a dirty non-Memory file and the child plane
  (dirty Memory note included) stayed out; the proof was consumed. A first
  smoke attempt in the RESIDUE fixture was correctly refused upstream by
  `ac_wwa_provenance` (stale prior-session WWA) — the guard stack composes:
  provenance gates the synthesis, the seam gates the plane.
- **Claude:** reference harness — gate + seam pinned by Tests 9b/9c/9d and
  Suite 15; PreToolUse enforcement continuously exercised in production
  sessions.

## Per-harness findings

### Claude Code — works (reference harness)

Guardrails enforce. Verified 2026‑06‑17: in a real interactive session a broad
`find /home …` triggered the policy `ask`; benign commands and the override env
behaved correctly; synthetic tests cover deny/ask/override/rate-limit/fail-open.
This is the one harness where the guardrail layer is real.

### OpenAI Codex — native capability is broader than the old 0.142 probe

**Current documented surfaces (reviewed 2026-07-11):**

- `AGENTS.md` / `AGENTS.override.md` provide hierarchical global and repository
  instructions. Codex reads one file per directory from the project root to the
  working directory, with nearer files taking precedence.
- Skills are the reusable workflow format. Current public documentation names
  `.agents/skills/` at repository or user scope and explicitly supports
  symlinked skill directories. Asha currently installs into `~/.codex/skills/`,
  a compatibility path verified by the active Codex environment but no longer
  the canonical path shown in public authoring documentation.
- Custom agents are standalone TOML files in `~/.codex/agents/` or
  `.codex/agents/`. `name`, `description`, and `developer_instructions` are
  required; model, reasoning, sandbox, MCP, and skill settings are optional.
  Asha's generated agent files match this schema.
- Native plugins can bundle skills, hooks, MCP servers, app/connector mappings,
  and assets through `.codex-plugin/plugin.json`. Asha's current local installer
  does not use that package surface; it mounts the components directly.
- Hooks are enabled by default and can come from `hooks.json`, inline
  `config.toml`, or an enabled plugin. Non-managed hooks require trust. Asha's
  nested `[[hooks.Event]]` / `[[hooks.Event.hooks]]` TOML is documented syntax.
- Rules are an experimental execution policy for commands that request to run
  outside the sandbox. `prefix_rule()` supports `allow`, `prompt`, and
  `forbidden`; it is not a general tool-policy engine.

**PreToolUse coverage:** Current official documentation says `PreToolUse` can
intercept supported simple Bash calls, `apply_patch`, and MCP tools. It can deny,
add context, or rewrite supported inputs. The same documentation says richer
`unified_exec` shell interception remains incomplete and that WebSearch and
other non-shell/non-MCP tools are not covered. Multiple matching hooks start
concurrently. Hence Codex hooks are meaningful, but not a complete enforcement
boundary.

**Asha empirical result, scoped to its version:** On Codex 0.142.0
(2026-06-24), a match-all Bash sentinel using the documented nested schema and
hook-trust bypass did not run before the tested shell command. That result is
consistent with the documented `unified_exec` gap. It does **not** establish
that all Codex hooks or all PreToolUse targets are inert, and the earlier text
claiming that `apply_patch` and MCP were unsupported was incorrect.

**0.145 live results (2026-07-26/27):** UserPromptSubmit fires and its raw
stdout injects (production-enabled); PostToolUse fires on shell and successful
`apply_patch` paths with a full Claude-shaped payload but **discards hook
stdout** — advisory injection is impossible on that event (full verdict: "Codex
PostToolUse" above). Matchers are honored, with `apply_patch` aliased into the
`Edit|Write|MultiEdit` class.

**0.147 live results (2026-08-08):** PreToolUse **fires and enforces deny for
simple Bash** under `codex exec`: `save-commit-gate.sh` blocked a staged
`git commit` before execution, the router logging "Command blocked by
PreToolUse hook: BLOCKED by Asha policy [save-commit-gate]" and no commit
landing (fixture `git log` unchanged). A subsequent proof-bearing commit was
allowed and the gate consumed the proof marker — deny AND allow paths both
participate. This supersedes the 0.142 no-fire verdict for simple Bash; the
documented `unified_exec` incompleteness caveat stands (probe evidence in the
workspace probe note above).

**Asha implementation:** Persona plus required operational context are supplied
through the wrapper's `model_instructions_file`. Asha also installs native hook
configuration and `rules/asha.rules`. The hooks may protect supported tool paths;
the rules add coarse approval policy for selected shell prefixes. Neither layer
should be described as full containment. Live probes now cover 0.145 for
UserPromptSubmit and PostToolUse; PreToolUse coverage beyond the 0.142 shell
probe remains unverified — re-probe before claiming it for newer versions.

### GitHub Copilot CLI 1.0.63 — persona, operational, AND guardrails all work

**Persona injection — WORKS (fixed + verified live 2026‑06‑24).** The earlier
"deferred / manual per-project" stance was wrong. Copilot CLI has no
`--instructions-file` *flag*, but it auto-loads user-level instructions from any
dir named in `COPILOT_CUSTOM_INSTRUCTIONS_DIRS` (it scans
`<dir>/.github/instructions/**/*.instructions.md`) and from
`$HOME/.copilot/copilot-instructions.md`. `asha copilot` now wraps the merged
identity as `~/.cache/asha/copilot-instr/.github/instructions/asha.instructions.md`
(with an `applyTo: "**"` header) and exports that env var — scoped to the launch,
so plain `copilot` stays persona-free (parity with Claude/Codex). Verified on CLI
1.0.63: launched through the wrapper, Copilot answers *"I am Asha…"* Empirical
note: a bare `AGENTS.md` inside a custom dir is **not** scanned — only the
`.github/instructions/*.instructions.md` form is (the cwd/repo-root `AGENTS.md`
*is* loaded, additively). asha's earlier hook-retirement was for a payload-delivery
gap in a pre-GA CLI; capture now reads the native `events.jsonl`, so that's moot.

**Operational layer — WORKS (wired + verified live 2026‑06‑24).** The same
custom-instructions dir carries a second file,
`asha-operational.instructions.md`, generated each launch by
`identity/operational-merge.sh` — `~/.asha/operation.md` (cap 4 KB, fallback
CORE.md) + the learnings hot tier (`learnings_manager.py render-hot`, same
budgets as `session-start.sh`). This is the file-based equivalent of Claude's
SessionStart hook, so Copilot gets the operational guidelines + learnings without
needing a working hook. Verified: launched via the wrapper, Copilot quoted a
`Surgical Edits` line from `operation.md` verbatim. (Codex now gets this too —
folded into its `model_instructions_file`, since its SessionStart hook doesn't
reliably inject; verified live on Codex 0.142.0.)

**PreToolUse guardrails — WIRED + enforced (built + verified live on 1.0.63, 2026‑06‑24).**
The old "won't pursue / unsafe" verdict was stale; asha now installs its
guardrails on Copilot. `copilot_install_hooks()` writes a **dedicated**
`~/.copilot/hooks/asha-guardrails.json` (flat Copilot schema, `{version:1}`) — it
never touches a user's own `hooks.json` (Copilot loads every `*.json` there) —
pointing at a new bridge, `plugins/session/hooks/handlers/copilot-policy-adapter.sh`.
The adapter exists because Copilot's hook contract differs from Claude's on three
axes (the docs are slightly off on all three):

- **Config schema is flat:**
  `{"version":1,"hooks":{"preToolUse":[{"type":"command","bash":"<cmd>","matcher":"bash|edit"}]}}`
  — a `bash` field, not Claude's nested `hooks:[{"command":…}]`.
- **Decision is via stdout JSON** `{"permissionDecision":"deny|allow|ask","permissionDecisionReason":"…"}`, not exit codes.
- **Payload arrives on stdin** as `{sessionId,timestamp,cwd,toolName,toolArgs}` (toolArgs may be a JSON-encoded *string*; tool names are `bash`/`create`/`edit`/`view`).

The adapter translates that to/from the Claude shape and runs the **existing**
`policy-guard.sh` + `block-secrets.sh` unchanged (no policy logic duplicated),
mapping the first deny/ask back to Copilot. Verified live: a denied command →
*"Denied by preToolUse hook: BLOCKED by Asha policy […]"*; the broad-`/home`-scan
`ask` rule fires (it degrades ask→deny in headless since Copilot can't prompt;
prompts interactively); `block-secrets` denies an `id_rsa` create via the `path`
field. Override envs (`ASHA_ALLOW_BROAD_SCAN=1`, …) pass through. Unit + integration
coverage in `tests/test-hooks.sh` (Test 105).

**Caveat (unchanged): the concurrency fail‑open**
([#2893](https://github.com/github/copilot-cli/issues/2893)) — `preToolUse` is
reportedly bypassed under *parallel* tool calls / timeouts. The adapter fails
*open* by design, so this is a **soft deterrent, not containment** (same posture
as the Claude string-pattern guard). [#2540](https://github.com/github/copilot-cli/issues/2540)
(plugin-defined hooks don't fire) doesn't apply — this is user-scope `~/.copilot/hooks/`.

**Classification: WIRED + enforced (verified). To disable: `asha uninstall copilot`
removes the file, or set the rules' override envs.**

**Audit (2026‑07‑01; extended 2026‑07‑27):** `asha doctor copilot` verifies the
guardrails, nudges, and lifecycle files each byte-match the installer-expected
JSON (and `--fix` rewrites them), alongside symlink and command-skill freshness
checks. The wrapper-only persona split is intentional and reported as INFO,
never a failure: `asha copilot` loads the persona per-launch; plain `copilot`
stays vanilla — while skills, agents, guardrails, lifecycle hooks, and /save
capture are wrapper-independent. Automatic clean-exit save and orphan recovery
are live (see the Copilot lifecycle note above) — `/save` remains available but
is no longer the only path. Native plugin distribution is mechanism, not
enforcement — its verification table lives in
[distribution-copilot.md](distribution-copilot.md).

### OpenCode — SUPPORT DROPPED 2026-07-27 (retirement record)

Operator decision following the #14 plugin-API survey: OpenCode ≥1.18 moved
session transcripts to sqlite, breaking Asha's memory capture — the system's
value core — and the fix (a new sqlite reader backend, #17) was judged not
worth carrying for the least-used harness. All opencode code paths were
removed (adapter, policy plugin, jsonl_reader backend, dispatcher/doctor
wiring, tests); live artifacts were uninstalled first. Reinstating support
means reverting the removal commit and building the #17 backend. The
pre-removal capability notes and final survey verdicts are preserved below
as the historical record.

OpenCode exposed native user skills, slash commands, Markdown agents, config
instructions, and JavaScript/TypeScript plugins. The installed 1.0.78 CLI was
plant-tested against the rendered Asha tree. Its accepted user-config layout is
`skills/`, `command/`, `agent/`, and `plugin/`; the latter three are singular.

`asha install opencode` mounts skills, renders commands and subagents, and emits
an `asha-guardrails.js` plugin using `tool.execute.before`. The plugin calls the
shared policy and secret handlers through `opencode-policy-adapter.sh`. A deny
throws before execution. Asha's `ask` action degrades to deny because no
portable permission-prompt response has been verified for that hook. This is a
fail-open policy layer, not containment.

`asha opencode` appends the merged identity and operational context through
`OPENCODE_CONFIG_CONTENT.instructions`, preserving the user's normal config and
custom config directory. Manual save parses OpenCode's directory storage under
`~/.local/share/opencode/storage/{session,message,part}`. Automatic SessionEnd
persistence is not implemented.

**Plugin API survey (1.18.7 / plugin SDK 1.1.4, live-probed 2026‑07‑27 —
issue #14, isolated `XDG_CONFIG_HOME`/`XDG_DATA_HOME` rig, local ollama
model, all four questions answered empirically):**

1. **Events beyond `tool.execute.before` — YES.** The plugin `Hooks` surface
   carries `chat.message` (fires once per submitted user message — the
   prompt-submit analog; verified live), `tool.execute.before/after` (verified),
   `permission.ask`, `chat.params`, `experimental.chat.system.transform` /
   `experimental.chat.messages.transform` (fire on every LLM roundtrip;
   verified), plus a catch-all `event` stream (verified delivering
   `session.created`, `session.status`, `session.idle`, `message.part.delta`,
   `session.diff`, …). `session.idle` fires at end of turn and is the natural
   deferred-save trigger; **no process-exit event exists** — the stream simply
   stops, so an in-process plugin cannot observe its own session's exit.
2. **Context injection — YES, one proven channel.**
   `experimental.chat.system.transform` appends to the system prompt array; a
   sentinel pushed there was quoted back verbatim by the model (INJECTED).
   Pushing a synthetic part in `chat.message` does NOT inject: the part
   persists in the conversation store but is excluded from the LLM payload
   (verified via a payload scan in `messages.transform`, which fired after the
   push and did not contain it).
3. **Callback context — sufficient.** `PluginInput` hands the plugin
   `directory` (project dir), `worktree`, `project`, an SDK `client`, and a Bun
   shell; every hook carries `sessionID`. The opencode process stamps
   `OPENCODE=1`. An `ASHA_HARNESS` value inherited from a parent harness leaks
   through (same nesting hazard verified on copilot/codex) — adapters must
   stamp `ASHA_HARNESS=opencode` explicitly when spawning shell handlers, as
   `asha-guardrails.js` already does.
4. **Transcript — MOVED to sqlite; jsonl_reader is stale.** A fresh 1.18.7
   state writes NO `storage/{session,message,part}` JSON tree; sessions,
   messages, and parts persist in `~/.local/share/opencode/opencode.db`
   (tables `session`/`message`/`part`, JSON `data` columns). jsonl_reader's
   opencode backend reads the legacy JSON layout only, so **`/save` synthesis
   from opencode-native transcripts is broken for sessions created by current
   versions** until a sqlite backend lands. Existing legacy JSON storage
   remains on disk and readable.

Follow-ups are filed as their own scoped issues: guidance nudges via the
verified `system.transform` channel, and the sqlite transcript backend that
gates any save automation (`session.idle`-triggered, opt-in per the #13
stance).

## Verdict — can / can't / won't fix

| Item | Status |
|---|---|
| Claude guardrails | **Works** (enforced, verified) |
| Codex guardrails | **Native but incomplete** — current docs cover simple Bash, `apply_patch`, and MCP calls, and explicitly exclude complete `unified_exec` interception plus other tool classes. Asha's 0.142 shell probe hit the gap and did not fire. Nested hook TOML plus `rules/asha.rules` are correctly installed, but neither is full containment. |
| Copilot persona | **Works** (fixed + verified 2026‑06‑24, CLI 1.0.63) — `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, per-launch. |
| Copilot operational layer | **Works** (wired + verified 2026‑06‑24) — `operation.md` + learnings hot tier via a second instructions file. |
| Copilot guardrails | **Wired + enforced (built + verified 2026‑06‑24)** — `asha install copilot` writes `~/.copilot/hooks/asha-guardrails.json` → `copilot-policy-adapter.sh` → the existing policy-guard + block-secrets. Live deny + ask + block-secrets confirmed on 1.0.63. Soft deterrent (concurrency [#2893](https://github.com/github/copilot-cli/issues/2893) untested; adapter fails open). |
| OpenCode (all layers) | **Dropped 2026-07-27** — ≥1.18 sqlite transcript store broke memory capture; support removed (see retirement record above). |

**Bottom line:** the file-based layers — corpus, persona, operational context,
and memory/capture — are cross-harness. Claude and Copilot policy behavior has
been verified across the documented tests. Codex has meaningful native hooks
and rules, but only partial interception; the 0.142 shell probe remains evidence
for the documented `unified_exec` gap, not evidence that the entire hook system
is inert. None of these string-policy layers should be treated as a sandbox.

## Test methodology

**Guardrail re-test (2026‑06‑24):**

- Codex `0.142.0`: scratch `CODEX_HOME` (auth symlinked from real `~/.codex`), a
  match-all `[[hooks.PreToolUse]]` sentinel (marker + deny + exit 2), launched via
  `codex exec --dangerously-bypass-hook-trust -s workspace-write` with an explicit
  "run this shell command" prompt. Result: command executed, sentinel marker
  empty → did not fire.
- Copilot `1.0.63`: a `~/.copilot/hooks/hooks.json` (correct flat schema,
  match-all, sentinel emits `{"permissionDecision":"deny",…}` on stdout + logs
  stdin), launched via `copilot --allow-all-tools -p "run this shell command"`.
  Result: hook fired (marker written, payload on stdin), command **denied**.
  Single serial call only — concurrency ([#2893](https://github.com/github/copilot-cli/issues/2893)) not exercised. Test config removed afterward.

**Original round (2026‑06‑17):**

- Codex: `codex-cli 0.139.0`, authed. Hook script invoked the deployed
  `policy-guard.sh` path and a catch-all/deny diagnostic. Ran via `asha codex`
  (interactive, by the user) and `codex exec -s read-only` (headless). Note:
  `codex exec` hangs on "Reading additional input from stdin…" in a non‑TTY
  context unless stdin is closed (`</dev/null`) — an environment artifact, not a
  hook signal; several early runs timed out for this reason before being re-run
  cleanly.
- Real `~/.codex/config.toml` was backed up and restored for every diagnostic.
- Claude: verified interactively by the user; synthetic stdin-JSON unit tests +
  install round-trips against the live deployed hook.

## What the agents themselves recommend (asked directly, 2026-06-17)

Asked each CLI how to make a blocking shell hook work:

- **Codex 0.139** (via `codex exec`): supplied the correct config (nested `[[hooks.PreToolUse.hooks]]` + `[features] hooks = true` + `/hooks` interactive trust), then warned that shell interception was incomplete for the `unified_exec` path and agreed the tested Bash call did not fire. Current official documentation now states this limitation directly whilst also documenting working interception for supported simple Bash, `apply_patch`, and MCP calls.
- **Copilot** (via `copilot -p`, 2026‑06‑17): said `preToolUse` hooks were **not a documented feature** — **now outdated.** GitHub documents Copilot hooks (incl. `preToolUse` approve/deny) for the 1.0.x GA line; re-ask on a current CLI before quoting this.

**Takeaway (updated 2026‑06‑24):** the Copilot recommendation is now superseded —
its `preToolUse` content-based deny **does** work (verified above), so Copilot is
*not* limited to coarse permission/MCP gating. Codex's recommendation stands: its
shell isn't on the hookable path, so content-based shell deny is genuinely
unavailable there; permission/approval gating or MCP tool validation remain the
only (coarser, non-drop-in) options for Codex. Asha's `rules/asha.rules` is the
implemented version of that coarse Codex-native fallback.

## Sources

- Codex hooks: https://developers.openai.com/codex/hooks ; config: https://developers.openai.com/codex/config-reference
- Codex skills: https://developers.openai.com/codex/skills ; custom agents: https://developers.openai.com/codex/multi-agent
- Codex AGENTS.md: https://learn.chatgpt.com/docs/agent-configuration/agents-md ; plugins: https://developers.openai.com/codex/plugins ; rules: https://developers.openai.com/codex/rules
- Codex coverage gaps: openai/codex #20204, #16732, #17794
- Copilot hooks: https://docs.github.com/en/copilot/reference/hooks-configuration ; concepts: https://docs.github.com/en/copilot/concepts/agents/about-hooks
- Copilot custom instructions (user-level `$HOME/.copilot/copilot-instructions.md`, `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`): https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions
- Copilot CLI GA (2026‑02‑25): https://github.blog/changelog/2026-02-25-github-copilot-cli-is-now-generally-available/
- Copilot gaps (status as of 2026‑06‑17, not re-verified on 1.0.63): github/copilot-cli #2893, #2540, #2013, #2980, #2585
