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
fails, and what is / isn't fixable, based on live testing (2026‑06‑17) plus
upstream docs and issue trackers.

## Capability matrix

| Capability | Claude Code | OpenAI Codex 0.139 | GitHub Copilot CLI |
|---|---|---|---|
| Corpus mount (skills/agents/commands) | ✅ | ✅ | ✅ |
| Persona injection | ✅ (`--append-system-prompt-file`) | ✅ (`-c model_instructions_file`) | ⚠️ manual per-project |
| Memory capture (`/save` from native transcript) | ✅ | ✅ | ✅ |
| SessionStart context injection (hook) | ✅ | ❓ uncertain (hook firing, see below) | ✖ hooks retired |
| **PreToolUse guardrails (deny/ask)** | **✅ enforced** | **✖ do not fire** | **✖ unreliable upstream** |

Only the last row fails. Everything asha needs that is file-based works on all
three; only real-time guardrail enforcement is Claude-only.

## Per-harness findings

### Claude Code — works (reference harness)
Guardrails enforce. Verified 2026‑06‑17: in a real interactive session a broad
`find /home …` triggered the policy `ask`; benign commands and the override env
behaved correctly; synthetic tests cover deny/ask/override/rate-limit/fail-open.
This is the one harness where the guardrail layer is real.

### OpenAI Codex 0.139.0 — guardrails do NOT fire (empirical)
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

### GitHub Copilot CLI — guardrails unreliable upstream; hooks retired in asha
asha retired Copilot hooks earlier (the then-current CLI fired hooks but never
delivered the tool payload). The newer Copilot CLI *does* expose `preToolUse`
hooks (deny supported, fail‑closed on crash), but with critical **open upstream
bugs** that defeat safety:
- [#2893](https://github.com/github/copilot-cli/issues/2893) — `preToolUse` silently **bypassed under parallel tool calls**; hooks exceeding the timeout are implicitly **allowed (fail‑open)**. Safety is silently lost under concurrency.
- [#2540](https://github.com/github/copilot-cli/issues/2540) — plugin-defined `preToolUse` hooks **don't fire**.
- [#2013](https://github.com/github/copilot-cli/issues/2013) — `updatedInput` rewrite ignored; [#2980](https://github.com/github/copilot-cli/issues/2980)/[#2585](https://github.com/github/copilot-cli/issues/2585) — `additionalContext` not wired.

Not live-tested here (no Copilot runs this session).

**Classification: revival possible but UNSAFE to rely on (concurrency fail‑open). Blocked upstream. Won't pursue.**

## Verdict — can / can't / won't fix

| Item | Status |
|---|---|
| Claude guardrails | **Works** (enforced, verified) |
| Codex guardrails | **Can't fix (upstream)** — 0.139 doesn't fire PreToolUse for its shell (`unified_exec`) path; flat + nested schema + `[features] hooks=true` + trust-bypass all tested, none fire (confirmed by Codex itself). Shell/edit-only even where hooks do work ([#20204](https://github.com/openai/codex/issues/20204)). |
| Copilot guardrails | **Won't pursue** — upstream concurrency fail‑open ([#2893](https://github.com/github/copilot-cli/issues/2893)) makes it unsafe; hooks retired. |

**Bottom line:** real-time guardrail enforcement is **Claude-only**, and is expected to stay that way until the Codex/Copilot hook systems mature upstream. The non-enforcement layers (corpus, persona, memory/capture) remain fully cross-harness — so the gap is scoped precisely to live tool-call interception, not to asha's value on those harnesses overall.

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
- **Copilot** (via `copilot -p`): says `preToolUse` hooks are **not a documented feature** of the CLI it knows; recommends **permission gates** (allowed-tools / approval mode) or **MCP-server tool-level validation** instead.

**Takeaway:** both point *away* from content-based shell hooks toward coarser native mechanisms — permission/approval gating, or MCP tool validation. Those gate tool *categories* or *MCP tools*, not the native shell command by content, so they are **not a drop-in** for the Claude regex-deny guardrail; they'd be a separate, weaker enforcement model if ever pursued.

## Sources
- Codex hooks: https://developers.openai.com/codex/hooks ; config: https://developers.openai.com/codex/config-reference
- Codex coverage gaps: openai/codex #20204, #16732, #17794
- Copilot hooks: https://docs.github.com/en/copilot/reference/hooks-configuration
- Copilot gaps: github/copilot-cli #2893, #2540, #2013, #2980, #2585
