# Harness enforcement — capabilities & known failures

asha augments the native agent CLIs (Claude Code, OpenAI Codex, GitHub Copilot)
at *their own seams*. Its features split cleanly by the seam they need:

- **File-based / post-hoc** (read a config/instructions file; post-process an
  on-disk transcript) → port to every harness, because every CLI does both.
- **Real-time interception** (a hook the CLI calls *before a tool runs* and
  *honors the decision*) → only works where the harness exposes a working hook.

Memory, persona, and the corpus are the first kind and work everywhere. The
**policy guardrails** (PreToolUse deny/ask) are the second kind — and that's
where the harnesses diverge. This document records what enforces where, what
fails, and what is / isn't fixable, based on live testing (2026‑06‑17, Copilot
persona re‑tested 2026‑06‑24) plus upstream docs and issue trackers.

> **Correction (2026‑06‑24).** An earlier revision marked Copilot persona
> injection "manual per-project" and left the impression Copilot was the most
> limited harness. That over‑hedged: it hunted for an injection *flag*
> (Claude/Codex style) and missed that Copilot CLI auto-loads user-level
> instructions. Persona now injects automatically and is verified live (see the
> Copilot section). And the follow-on re-test (2026‑06‑24) went further: Copilot's
> **PreToolUse guardrails also work** on 1.0.63. The lone remaining divergence is
> **Codex's shell**, which bypasses the hook (`unified_exec`).

## Capability matrix

| Capability | Claude Code | OpenAI Codex 0.142 | GitHub Copilot CLI 1.0.63 |
|---|---|---|---|
| Corpus mount (skills/agents) | ✅ | ✅ | ✅ |
| Slash commands | ✅ native | ⚠️ converted to skills | ⚠️ converted to skills |
| Output styles (`/style`) | ✅ | ✖ skipped | ✖ skipped |
| Persona injection | ✅ (`--append-system-prompt-file`) | ✅ (`-c model_instructions_file`) | ✅ (`COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, per-launch) |
| Operational context (operation.md + learnings hot tier) | ✅ (SessionStart hook) | ✅ (folded into `model_instructions_file`, 2026‑06‑24) | ✅ (instructions file, 2026‑06‑24) |
| Memory capture (`/save` from native transcript) | ✅ | ✅ | ✅ |
| **PreToolUse guardrails (deny/ask)** | **✅ enforced** | **✖ do not fire (re-confirmed 0.142)** | **✅ wired + enforced (1.0.63, via adapter; concurrency [#2893](https://github.com/github/copilot-cli/issues/2893) untested)** |
| Native command approval rules | n/a | ⚠️ coarse `~/.codex/rules/asha.rules` fallback (prefix-based, permission/sandbox boundary only) | n/a |

Guardrails now enforce on **Claude and Copilot** (Copilot single-call deny
verified live on 1.0.63, 2026‑06‑24); only **Codex** can't (its shell runs
through `unified_exec`, off the hookable path — re-confirmed on 0.142). The
file-based layers — corpus, persona (all three; Copilot persona fixed
2026‑06‑24), and the operational layer (operation.md + learnings; Copilot +
Codex both wired 2026‑06‑24 — file-based, no working hook required) — work on all
three CLIs. Note: slash commands are remapped to skills on Codex/Copilot (no
native command primitive), and the `output-styles` plugin is Claude-only. Codex
also gets native execution-policy `prefix_rule()` prompts for a narrow subset of
high-risk shell commands; these are not equivalent to Asha's regex guardrails.

## Per-harness findings

### Claude Code — works (reference harness)

Guardrails enforce. Verified 2026‑06‑17: in a real interactive session a broad
`find /home …` triggered the policy `ask`; benign commands and the override env
behaved correctly; synthetic tests cover deny/ask/override/rate-limit/fail-open.
This is the one harness where the guardrail layer is real.

### OpenAI Codex (0.139.0 + 0.142.0) — guardrails do NOT fire (empirical)

**What works (re-verified on Codex 0.142.0, 2026‑06‑24):** persona injection
(`model_instructions_file`) and the operational layer (operation.md + learnings,
folded into the same file) both load — Codex quoted an `operation.md`-only line
verbatim. The SessionStart hook still does not reliably inject (its content
reaches Codex only because we fold it into `model_instructions_file`, not via the
hook).

**Guardrails — RE-CONFIRMED dead on 0.142.0 (2026‑06‑24).** Built a scratch
`CODEX_HOME` with a **match-all** `[[hooks.PreToolUse]]` sentinel (writes a marker,
denies, exit 2) and ran `codex exec --dangerously-bypass-hook-trust`. Codex
acknowledged the hooks (it printed the bypass-trust warning) and then executed the
shell command (`/usr/bin/zsh -lc 'echo …'` ran, output returned) **with the
sentinel marker still empty** — PreToolUse did not fire before the shell tool. So
the `unified_exec` gap from 0.139 persists in 0.142; the original finding below
stands unchanged.

**Compatibility refresh (2026‑06‑26):** Asha now emits the current documented
nested hook TOML shape (`[[hooks.Event]]` matcher group plus
`[[hooks.Event.hooks]]` command handler) instead of the earlier flat form. This
does not change the 0.142 shell verdict above, but it removes avoidable schema
drift for hook events that do fire. Asha also writes a dedicated
`~/.codex/rules/asha.rules` file using Codex's native `prefix_rule()` execution
policy for coarse prompts on `find /home`, `bfs /home`, `git reset --hard`,
force-push, and protected-branch deletes. Limitation: Codex rules are prefix
rules, not regex policy, and apply at approval/sandbox boundaries rather than as
a content-aware PreToolUse replacement.

**What we observed (live, this machine):**

- A broad `/home` scan ran **unblocked** under interactive `asha codex` (user‑confirmed) and under `codex exec` across multiple completed runs.
- asha's **flat** `config.toml` hook block (`[[hooks.PreToolUse]]` with `matcher`/`type`/`command` inline) → **did not fire**.
- The **documented nested** schema (`[[hooks.PreToolUse]]` + a nested `[[hooks.PreToolUse.hooks]]` handler), run with `--dangerously-bypass-hook-trust` and stdin closed → **still did not fire**; the command executed, sentinel log empty.
- So the obvious "asha emits the wrong TOML schema" lead is **disproven on 0.139** — the documented shape didn't fire either.

**What upstream docs/issues say:** Codex *does* have a hooks system — PreToolUse should fire for Bash (tool name `"Bash"`), deny is supported (JSON `permissionDecision:"deny"` or exit 2), and there's an interactive hook‑trust model. **Cause confirmed (2026-06-17).** Further tested the documented nested schema AND the `[features] hooks = true` enable flag (`-c features.hooks=true`), both with `--dangerously-bypass-hook-trust` and clean runs — still no fire. Asked directly, Codex confirmed: *shell interception is incomplete for the newer `unified_exec` path*. Codex's shell simply isn't on the hookable PreToolUse path in 0.139 — an **upstream gap, not a config error**; no asha-side config change fixes it.

**Even if firing worked, coverage is incomplete upstream:** PreToolUse only fires for shell + edit; **not** `apply_patch`, MCP tools, web tools, reads, planning (openai/codex [#20204](https://github.com/openai/codex/issues/20204) open; [#16732](https://github.com/openai/codex/issues/16732), [#17794](https://github.com/openai/codex/issues/17794)). An agent could pivot to an unguarded tool.

**Classification: BROKEN now; fix uncertain & non-trivial; partial at best even if fixed.** Not pursued now.

**Implication:** asha's pre-existing Codex PreToolUse hooks (notably `block-secrets`) are **also inert** — same mechanism.

**Residual unknowns:** `~/.codex/hooks.json` (same schema) and interactive `/hooks` trust weren't tried — but since the gap is the shell *execution path* (`unified_exec`), not the config file or trust, neither is expected to help shell interception.

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
distribution (verified live on CLI 1.0.65: marketplace add → plugin install →
skill fires under plain `copilot`) is mechanism, not enforcement — see
[distribution-copilot.md](distribution-copilot.md).

## Verdict — can / can't / won't fix

| Item | Status |
|---|---|
| Claude guardrails | **Works** (enforced, verified) |
| Codex guardrails | **Can't fully fix (upstream)** — re-confirmed on 0.142 (2026‑06‑24): match-all hook + trust-bypass, shell still ran, hook never fired. Shell goes through `unified_exec`, off the hookable path; shell/edit-only even where hooks do work ([#20204](https://github.com/openai/codex/issues/20204)). Asha now emits documented nested hook TOML and installs native `rules/asha.rules` as a coarse approval fallback, but not full policy parity. |
| Copilot persona | **Works** (fixed + verified 2026‑06‑24, CLI 1.0.63) — `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, per-launch. |
| Copilot operational layer | **Works** (wired + verified 2026‑06‑24) — `operation.md` + learnings hot tier via a second instructions file. |
| Copilot guardrails | **Wired + enforced (built + verified 2026‑06‑24)** — `asha install copilot` writes `~/.copilot/hooks/asha-guardrails.json` → `copilot-policy-adapter.sh` → the existing policy-guard + block-secrets. Live deny + ask + block-secrets confirmed on 1.0.63. Soft deterrent (concurrency [#2893](https://github.com/github/copilot-cli/issues/2893) untested; adapter fails open). |

**Bottom line:** the file-based layers — corpus, persona (all three), the operational layer (operation.md + learnings; Copilot + Codex both wired 2026‑06‑24), and memory/capture — are cross-harness. **Real-time guardrail enforcement now works on Claude AND Copilot** — asha installs the Copilot guardrails via an adapter over the existing policy engine (verified live on 1.0.63). **Codex is the lone holdout for full guardrail parity** — its shell bypasses the hook (`unified_exec`, re-confirmed on 0.142). The installer now uses Codex's native rules file for coarse approval prompts, but the remaining enforcement gap is still Codex content-aware shell policy, plus the untested Copilot parallel-call concurrency case (a soft-deterrent limit, not a containment guarantee, on every harness).

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

- **Codex** (via `codex exec`): supplied the correct config (nested `[[hooks.PreToolUse.hooks]]` + `[features] hooks = true` + `/hooks` interactive trust) — then **warned that shell interception is incomplete for the `unified_exec` path**, and, having read this repo's own notes, agreed PreToolUse for Bash doesn't fire. Net recommendation: do not rely on hooks as a shell enforcement boundary in 0.139.
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
- Codex coverage gaps: openai/codex #20204, #16732, #17794
- Copilot hooks: https://docs.github.com/en/copilot/reference/hooks-configuration ; concepts: https://docs.github.com/en/copilot/concepts/agents/about-hooks
- Copilot custom instructions (user-level `$HOME/.copilot/copilot-instructions.md`, `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`): https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions
- Copilot CLI GA (2026‑02‑25): https://github.blog/changelog/2026-02-25-github-copilot-cli-is-now-generally-available/
- Copilot gaps (status as of 2026‑06‑17, not re-verified on 1.0.63): github/copilot-cli #2893, #2540, #2013, #2980, #2585
