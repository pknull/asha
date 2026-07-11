# Harness enforcement — capabilities & known failures

asha augments the native agent CLIs (Claude Code, OpenAI Codex, GitHub Copilot,
and OpenCode)
at *their own seams*. Its features split cleanly by the seam they need:

- **File-based / post-hoc** (read a config/instructions file; post-process an
  on-disk transcript) → port to every harness, because every CLI does both.
- **Real-time interception** (a hook the CLI calls *before a tool runs* and
  *honors the decision*) → only works where the harness exposes a working hook.

Memory, persona, and the corpus are the first kind and work everywhere, albeit
OpenCode persistence is manual-save only. The
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

| Capability | Claude Code | OpenAI Codex (installed 0.144.1; docs current 2026‑07‑11; live hook probe 0.142) | GitHub Copilot CLI 1.0.63 |
|---|---|---|---|
| Corpus mount (skills/agents) | ✅ native Markdown | ✅ skills + generated TOML custom agents | ✅ skills + generated `.agent.md` |
| Reusable command workflows | ✅ native user commands | ✅ Asha renders as skills; Codex slash commands themselves are built-in | ⚠️ converted to skills |
| Output styles | ✅ retired from asha (2026‑07‑10 audit) — Claude's native `/output-style` covers switching; the test canary style still mounts | ✖ n/a | ✖ n/a |
| Persona injection | ✅ (`--append-system-prompt-file`) | ✅ (`-c model_instructions_file`) | ✅ (`COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, per-launch) |
| Operational context (operation.md + learnings hot tier) | ✅ (SessionStart hook) | ✅ (folded into `model_instructions_file`, 2026‑06‑24) | ✅ (instructions file, 2026‑06‑24) |
| Memory capture (`/save` from native transcript) | ✅ | ✅ | ✅ |
| **PreToolUse guardrails (deny/ask)** | **✅ enforced** | **⚠️ native but partial: documented for simple Bash, `apply_patch`, and MCP; `unified_exec` interception incomplete. Asha's 0.142 shell probe did not fire.** | **✅ wired + enforced (1.0.63, via adapter; concurrency [#2893](https://github.com/github/copilot-cli/issues/2893) untested)** |
| Native command approval rules | n/a | ⚠️ `~/.codex/rules/asha.rules`; prefix-based, outside-sandbox execution policy | n/a |
| Native plugin packaging | Claude plugin model | ✅ `.codex-plugin/plugin.json` can bundle skills, hooks, MCP, apps, and assets; Asha direct installer does not yet use it | Copilot plugin build path implemented separately |

Guardrails enforce across the tested Claude and Copilot paths (Copilot
single-call deny verified live on 1.0.63, 2026‑06‑24). Codex has a real native
hook system, not an absent one, but its coverage is incomplete. Official docs
state that simple Bash, `apply_patch`, and MCP calls can be intercepted whilst
some richer `unified_exec` shell calls and non-shell/non-MCP tools cannot. The
Asha 0.142 shell probe landed in the uncovered case and did not fire. The
file-based layers — corpus, persona (all four; Copilot persona fixed
2026‑06‑24), and the operational layer (operation.md + learnings; Copilot +
Codex both wired 2026‑06‑24 — file-based, no working hook required) — work on all
four CLIs. Note: Asha's user-defined command workflows are remapped to skills
on Codex/Copilot and native commands on OpenCode. Codex does have built-in slash commands, but no documented
custom command-file surface. The `output-styles` plugin was retired in the
2026‑07‑10 ecosystem audit (Claude's native `/output-style` covers it). Codex
also gets native execution-policy `prefix_rule()` prompts for a narrow subset of
high-risk shell commands; these are not equivalent to Asha's regex guardrails.

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

**Asha implementation:** Persona plus required operational context are supplied
through the wrapper's `model_instructions_file`. Asha also installs native hook
configuration and `rules/asha.rules`. The hooks may protect supported tool paths;
the rules add coarse approval policy for selected shell prefixes. Neither layer
should be described as full containment. A fresh live hook matrix should be run
before claiming behavior for Codex versions newer than the recorded 0.142 probe.

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

**Audit (2026‑07‑01):** `asha doctor copilot` verifies the guardrails file
byte-matches the installer-expected JSON (and `--fix` rewrites it), alongside
symlink and command-skill freshness checks. The wrapper-only persona split is
intentional and reported as INFO, never a failure: `asha copilot` loads the
persona per-launch; plain `copilot` stays vanilla — while skills, agents,
guardrails, and /save capture are wrapper-independent. Native plugin
distribution is mechanism, not enforcement — its verification table lives in
[distribution-copilot.md](distribution-copilot.md).

### OpenCode 1.0.78 — native corpus and hooks, manual-save memory

OpenCode exposes native user skills, slash commands, Markdown agents, config
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

## Verdict — can / can't / won't fix

| Item | Status |
|---|---|
| Claude guardrails | **Works** (enforced, verified) |
| Codex guardrails | **Native but incomplete** — current docs cover simple Bash, `apply_patch`, and MCP calls, and explicitly exclude complete `unified_exec` interception plus other tool classes. Asha's 0.142 shell probe hit the gap and did not fire. Nested hook TOML plus `rules/asha.rules` are correctly installed, but neither is full containment. |
| Copilot persona | **Works** (fixed + verified 2026‑06‑24, CLI 1.0.63) — `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, per-launch. |
| Copilot operational layer | **Works** (wired + verified 2026‑06‑24) — `operation.md` + learnings hot tier via a second instructions file. |
| Copilot guardrails | **Wired + enforced (built + verified 2026‑06‑24)** — `asha install copilot` writes `~/.copilot/hooks/asha-guardrails.json` → `copilot-policy-adapter.sh` → the existing policy-guard + block-secrets. Live deny + ask + block-secrets confirmed on 1.0.63. Soft deterrent (concurrency [#2893](https://github.com/github/copilot-cli/issues/2893) untested; adapter fails open). |
| OpenCode corpus/persona | **Works** — native skills, commands, agents, and wrapper-scoped instructions; loader verified on 1.0.78. |
| OpenCode guardrails | **Wired, synthetic verification only** — native `tool.execute.before` plugin bridges to shared policy; deny is implemented, ask degrades to deny. |
| OpenCode memory | **Partial** — manual save parses native storage; no automatic SessionEnd persistence. |

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
