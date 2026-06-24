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
> Copilot section). The narrow, still-true divergence is only **real-time
> PreToolUse guardrails**.

## Capability matrix

| Capability | Claude Code | OpenAI Codex 0.139 | GitHub Copilot CLI 1.0.63 |
|---|---|---|---|
| Corpus mount (skills/agents) | ✅ | ✅ | ✅ |
| Slash commands | ✅ native | ⚠️ converted to skills | ⚠️ converted to skills |
| Output styles (`/style`) | ✅ | ✖ skipped | ✖ skipped |
| Persona injection | ✅ (`--append-system-prompt-file`) | ✅ (`-c model_instructions_file`) | ✅ (`COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, per-launch) |
| Operational context (operation.md + learnings hot tier) | ✅ (SessionStart hook) | ✅ (folded into `model_instructions_file`, 2026‑06‑24) | ✅ (instructions file, 2026‑06‑24) |
| Memory capture (`/save` from native transcript) | ✅ | ✅ | ✅ |
| **PreToolUse guardrails (deny/ask)** | **✅ enforced** | **✖ do not fire** | **⚠️ GA; not re-tested on 1.0.63 (see below)** |

Only the guardrail row is Claude-only. The file-based layers — corpus, persona
(all three; Copilot persona fixed 2026‑06‑24), and the operational layer
(operation.md + learnings; Copilot + Codex both wired 2026‑06‑24 — file-based,
no working hook required) — work on all three CLIs. Note: slash commands are remapped to skills on
Codex/Copilot (no native command primitive), and the `output-styles` plugin is
Claude-only.

## Per-harness findings

### Claude Code — works (reference harness)

Guardrails enforce. Verified 2026‑06‑17: in a real interactive session a broad
`find /home …` triggered the policy `ask`; benign commands and the override env
behaved correctly; synthetic tests cover deny/ask/override/rate-limit/fail-open.
This is the one harness where the guardrail layer is real.

### OpenAI Codex 0.139.0 — guardrails do NOT fire (empirical)

**What works (re-verified on Codex 0.142.0, 2026‑06‑24):** persona injection
(`model_instructions_file`) and the operational layer (operation.md + learnings,
folded into the same file) both load — Codex quoted an `operation.md`-only line
verbatim. The guardrail findings below are from 0.139.0 and were **not** re-tested
on 0.142.0; the SessionStart hook still does not reliably inject (its content
reaches Codex only because we fold it into `model_instructions_file`, not via the
hook).

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

### GitHub Copilot CLI 1.0.63 — persona works; guardrail verdict is stale

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

**PreToolUse guardrails — verdict NOT re-verified on 1.0.63.** The "won't pursue"
call below dates to the issue tracker (2026‑06‑17), not a live test on the current
CLI. As of 2026‑06‑24 Copilot hooks are **GA and documented**: eight events
(`sessionStart`/`preToolUse`/`postToolUse`/…), `preToolUse` can "approve or deny
tool executions", config at `.github/hooks/*.json` (repo) or `~/.copilot/hooks/*.json`
(user). The open concern is the **concurrency fail‑open**
([#2893](https://github.com/github/copilot-cli/issues/2893)): `preToolUse`
reportedly bypassed under parallel tool calls, timeouts implicitly allowed. That
issue has not been re-checked against 1.0.63.

- [#2540](https://github.com/github/copilot-cli/issues/2540) — plugin-defined `preToolUse` hooks **don't fire** (status unverified on current CLI).
- [#2013](https://github.com/github/copilot-cli/issues/2013) — `updatedInput` rewrite ignored; [#2980](https://github.com/github/copilot-cli/issues/2980)/[#2585](https://github.com/github/copilot-cli/issues/2585) — `additionalContext` not wired.

**Classification: STALE — needs a live re-test on 1.0.63 before any verdict.
Treat "won't pursue" as not-yet-confirmed, not a current finding.**

## Verdict — can / can't / won't fix

| Item | Status |
|---|---|
| Claude guardrails | **Works** (enforced, verified) |
| Codex guardrails | **Can't fix (upstream)** — 0.139 doesn't fire PreToolUse for its shell (`unified_exec`) path; flat + nested schema + `[features] hooks=true` + trust-bypass all tested, none fire (confirmed by Codex itself). Shell/edit-only even where hooks do work ([#20204](https://github.com/openai/codex/issues/20204)). |
| Copilot persona | **Works** (fixed + verified 2026‑06‑24, CLI 1.0.63) — `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`, per-launch. |
| Copilot operational layer | **Works** (wired + verified 2026‑06‑24) — `operation.md` + learnings hot tier via a second instructions file. |
| Copilot guardrails | **Stale — needs re-test on 1.0.63.** Hooks are now GA with documented `preToolUse` deny; the prior "unsafe" call rests on the un-re-verified concurrency fail‑open ([#2893](https://github.com/github/copilot-cli/issues/2893)). Not re-tested this round. |

**Bottom line:** the file-based layers — corpus, persona (all three), and the operational layer (operation.md + learnings; Copilot + Codex both wired 2026‑06‑24), plus memory/capture — are cross-harness. **Real-time guardrail enforcement is Claude-only as of the last live test**: Codex can't (upstream `unified_exec` gap) and Copilot's verdict is stale pending a 1.0.63 re-test. The remaining gap is scoped precisely to live tool-call interception, not to asha's value on those harnesses overall.

## Test methodology (2026‑06‑17)

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

**Takeaway:** both point *away* from content-based shell hooks toward coarser native mechanisms — permission/approval gating, or MCP tool validation. Those gate tool *categories* or *MCP tools*, not the native shell command by content, so they are **not a drop-in** for the Claude regex-deny guardrail; they'd be a separate, weaker enforcement model if ever pursued.

## Sources

- Codex hooks: https://developers.openai.com/codex/hooks ; config: https://developers.openai.com/codex/config-reference
- Codex coverage gaps: openai/codex #20204, #16732, #17794
- Copilot hooks: https://docs.github.com/en/copilot/reference/hooks-configuration ; concepts: https://docs.github.com/en/copilot/concepts/agents/about-hooks
- Copilot custom instructions (user-level `$HOME/.copilot/copilot-instructions.md`, `COPILOT_CUSTOM_INSTRUCTIONS_DIRS`): https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions
- Copilot CLI GA (2026‑02‑25): https://github.blog/changelog/2026-02-25-github-copilot-cli-is-now-generally-available/
- Copilot gaps (status as of 2026‑06‑17, not re-verified on 1.0.63): github/copilot-cli #2893, #2540, #2013, #2980, #2585
