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
| **PreToolUse guardrails (deny/ask)** | **✅ enforced** | **✖ do not fire (re-confirmed 0.142)** | **✅ fires + denies (1.0.63, single-call; concurrency [#2893](https://github.com/github/copilot-cli/issues/2893) untested)** |

Guardrails now enforce on **Claude and Copilot** (Copilot single-call deny
verified live on 1.0.63, 2026‑06‑24); only **Codex** can't (its shell runs
through `unified_exec`, off the hookable path — re-confirmed on 0.142). The
file-based layers — corpus, persona (all three; Copilot persona fixed
2026‑06‑24), and the operational layer (operation.md + learnings; Copilot +
Codex both wired 2026‑06‑24 — file-based, no working hook required) — work on all
three CLIs. Note: slash commands are remapped to skills on Codex/Copilot (no
native command primitive), and the `output-styles` plugin is Claude-only.

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

**PreToolUse guardrails — WORK (verified live on 1.0.63, 2026‑06‑24).** The old
"won't pursue / unsafe" verdict was stale. A sentinel `preToolUse` hook at
`~/.copilot/hooks/hooks.json` **fired and denied** a shell command — Copilot
printed *"Denied by preToolUse hook: asha-guardtest sentinel"* and refused to run
it. So content-based deny genuinely enforces on Copilot now. Empirical schema
notes (matter for any future asha wiring — the docs are slightly off):

- **Config schema is NOT the Claude shape.** Copilot wants
  `{"version":1,"hooks":{"preToolUse":[{"type":"command","bash":"<cmd>","matcher":"bash|edit"}]}}`
  — a **flat** entry with a `bash` field (not a nested `hooks:[{"command":…}]`
  array). asha's current copilot hook *emitter* still writes the Claude-style
  nested shape, so it would need a rewrite before install.
- **Decision is via stdout JSON:** `{"permissionDecision":"deny|allow|ask","permissionDecisionReason":"…"}`
  (not exit codes). asha's `policy-guard.sh`/`block-secrets.sh` emit Claude's
  decision shape, so they'd need an output adapter.
- **Payload arrives on stdin** (contradicting the older "payload never delivered"
  finding and the current docs that say arg/env): the hook received
  `{"sessionId","timestamp","cwd","toolName":"bash","toolArgs":{…command…}}` on
  stdin.

**Still untested: the concurrency fail‑open**
([#2893](https://github.com/github/copilot-cli/issues/2893)) — `preToolUse`
reportedly bypassed under *parallel* tool calls / timeouts. My test was a single
serial call, so this remains the open safety caveat. [#2540](https://github.com/github/copilot-cli/issues/2540)
(plugin-defined hooks don't fire) doesn't apply to user-scope `~/.copilot/hooks/`.

**Classification: WORKS for single-call deny (verified). Asha could wire its
guardrails on Copilot — but it needs (a) the correct flat schema, (b) a
`permissionDecision`-JSON output adapter, and (c) a decision on the unmitigated
concurrency fail‑open. That's a separate feature, not yet built.**

## Verdict — can / can't / won't fix

| Item | Status |
|---|---|
| Claude guardrails | **Works** (enforced, verified) |
| Codex guardrails | **Can't fix (upstream)** — re-confirmed on 0.142 (2026‑06‑24): match-all hook + trust-bypass, shell still ran, hook never fired. Shell goes through `unified_exec`, off the hookable path; shell/edit-only even where hooks do work ([#20204](https://github.com/openai/codex/issues/20204)). |
| Copilot persona | **Works** (fixed + verified 2026‑06‑24, CLI 1.0.63) — `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, per-launch. |
| Copilot operational layer | **Works** (wired + verified 2026‑06‑24) — `operation.md` + learnings hot tier via a second instructions file. |
| Copilot guardrails | **Work at the CLI level (verified 2026‑06‑24)** — a `~/.copilot/hooks/` `preToolUse` sentinel fired and denied a shell command on 1.0.63. **Not yet wired in asha:** needs the flat Copilot schema + a `permissionDecision`-JSON output adapter + a call on the untested concurrency fail‑open ([#2893](https://github.com/github/copilot-cli/issues/2893)). |

**Bottom line:** the file-based layers — corpus, persona (all three), the operational layer (operation.md + learnings; Copilot + Codex both wired 2026‑06‑24), and memory/capture — are cross-harness. **Real-time guardrail enforcement now works on Claude AND Copilot** (Copilot single-call deny verified on 1.0.63; asha install of it is a separate, not-yet-built feature). **Codex remains the lone holdout** — its shell bypasses the hook (`unified_exec`, re-confirmed on 0.142). The only enforcement gap left is Codex shell, plus the untested Copilot concurrency case.

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
only (coarser, non-drop-in) options for Codex.

## Sources

- Codex hooks: https://developers.openai.com/codex/hooks ; config: https://developers.openai.com/codex/config-reference
- Codex coverage gaps: openai/codex #20204, #16732, #17794
- Copilot hooks: https://docs.github.com/en/copilot/reference/hooks-configuration ; concepts: https://docs.github.com/en/copilot/concepts/agents/about-hooks
- Copilot custom instructions (user-level `$HOME/.copilot/copilot-instructions.md`, `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`): https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions
- Copilot CLI GA (2026‑02‑25): https://github.blog/changelog/2026-02-25-github-copilot-cli-is-now-generally-available/
- Copilot gaps (status as of 2026‑06‑17, not re-verified on 1.0.63): github/copilot-cli #2893, #2540, #2013, #2980, #2585
