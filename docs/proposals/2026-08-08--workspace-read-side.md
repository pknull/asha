---
title: Workspace read-side context injection (v2)
type: proposal
status: proposed — ratification by merging this PR
date: 2026-08-08
origin: epic #23, the "v2 read-side" row of the ratified workspace-memory proposal (docs/proposals/2026-08-06--workspace-memory.md, Deferred increments); drafted after workspace v1 shipped complete (issues #31/#33/#29/#35/#36/#39; PRs 30/32/34/37/38/41/42)
---

# Workspace read-side context injection (v2) — design spec

v1 gave sessions a *write* contract: detection, save scopes, plane-bound
proofs, gates, and the auto-save seam. A session inside a workspace can now
commit memory to the right plane — but it still *starts blind*: nothing
tells the model it is inside a workspace, which child repo is active, or
what the workspace's operational memory currently says. v2 closes the read
side, and nothing else.

**Precedence**: this document pins v2's decisions; the epic's prose links
here. **Ratification mechanics**: merging this PR constitutes ratification
and flips `status` to `ratified`. Delivery issues are filed only after
ratification, one decision per issue, loop-grade hygiene throughout.

## Decisions to ratify

1. **v2 scope = read-side only.** Session-start workspace detection, one
   bounded context injection, and source-aware retrieval ranking. No new
   write paths, no new commands, no manifest changes. `shared_root` stays
   inert (v3's canonical index is the first thing that may read it — re-pin
   from v1). Anything else that looks adjacent (bootstrap, worktrees,
   work-items) stays in its own increment.
2. **Injection rides each harness's PROVEN injection channel — probed, not
   assumed.** Claude: SessionStart stdout (verified surface). Codex and
   Copilot: sessionStart injection support is UNVERIFIED — probe it first;
   if it does not inject, fall back to a once-per-session nudge-engine row
   on the harness's verified channel (Codex UserPromptSubmit raw fragment;
   Copilot userPromptSubmitted `additionalContext`). The #39 lesson is the
   bar: a channel verdict counts only when the harness itself delivered the
   payload in a live session; verdicts land in `docs/harness-enforcement.md`
   before wiring is called done.
3. **The injection is a bounded INDEX, never bodies.** One workspace header
   line (name, root, active child repo, memory plane roots) plus a capped
   render of the workspace operational plane's `activeContext.md` — hard
   byte budget, headline-first, explicit truncation tail. Default budget
   2048 bytes, override `ASHA_WS_CONTEXT_MAX`. This is the v1.5.0 recall-
   economics rule applied to a new source: index-first injection, bodies
   Read on demand.
4. **Zero cost and zero output outside workspaces.** No manifest above the
   project → the renderer emits nothing and the hook adds no python to the
   hot path (bash existence walk first, same as the commit gate). Golden
   byte-identity for single-project sessions is a pinned regression
   surface, not an aspiration.
5. **Source-aware retrieval ranking.** Retrieval results carry a `source`
   field (`project` | `workspace` | `learnings`); ranking gains a modest
   workspace-plane term so workspace hits surface without displacing
   project-local ones (ties break project-first). The recall bench gains
   workspace fixtures; the existing 12/13 floor may not regress.
6. **Kill switches, narrowest first** (nudge-layer conventions):
   `ASHA_WS_INJECT=0` env; `Work/markers/nudge-ws-context-off` at the
   workspace plane; the workspace plane's `Work/markers/silence` gates
   injection like every other silence-gated surface.
7. **Injected workspace memory is context, not instructions.** The render
   is prefixed as background state (same framing as the learnings index).
   An `activeContext.md` that contains directive-shaped text is data; the
   injection layer never marks it as anything more authoritative than the
   file it came from.

## Contract

Renderer: a pure function over (start dir) → context block or nothing.
Ships as a mode of the existing status tool (`workspace_status.py
--context`) rather than a new file — same detection core, same typed
verdicts, same fail-closed posture on invalid manifests (an invalid
manifest renders a one-line warning pointing at `asha workspace status`,
never a partial context block).

Injection block shape (illustrative, not byte-pinned):

```
── Workspace: <name> ──
root: <path>   active repo: <child>   memory: operational=Memory
<capped activeContext headline render>
[… truncated at 2048 bytes — read <root>/Memory/activeContext.md]
```

Wiring: session-start.sh gains one guarded stanza (bash walk → python
renderer only when a manifest exists). Codex/Copilot wiring per decision 2
after their probes.

## Cross-harness parity (v2 ship gate)

Same discipline as v1, scoped to what exists per harness: the injection is
proven by a live probe in which the harness's own session start (or
fallback channel) delivered the workspace block to the model — an
env-shaped rehearsal is not evidence. Codex has no SessionEnd but does
have session_start; if BOTH codex channels fail to inject at session
start, the fallback row fires on first prompt and that verdict is recorded
as the honest limitation. Verdicts + probe evidence: harness-enforcement.md
capability row and the `workspace` capability entries.

## Delivery issues (filed after ratification)

1. **Renderer** — `workspace_status.py --context`: bounded render, budgets,
   truncation tail, invalid-manifest warning, no-workspace empty output.
   Pure + tested; a realistic issue-loop candidate (first one of the
   workspace line, per the v1 delivery note).
2. **Claude wiring** — session-start.sh stanza + live probe. Attended
   (hook surface).
3. **Codex/Copilot channel probes + wiring** — sessionStart injection
   verdicts, fallback rows if needed. Attended (hook surface).
4. **Source-aware ranking** — retrieval `source` field, workspace term,
   recall-bench workspace fixtures. Loop-candidate.
5. **Parity attestation** — ship gate; live verdicts recorded. Attended.

## Security posture

Read-only increment: no new commit paths, no new push paths, no state
mutation beyond nudge cooldowns. The injection renderer is still
guardrail-adjacent (it decides what enters context), so hook-side wiring
stays attended; the pure renderer and ranking pieces are loop-eligible.
Injection content originates from files the user's own workspace commits —
decision 7 pins its framing.
