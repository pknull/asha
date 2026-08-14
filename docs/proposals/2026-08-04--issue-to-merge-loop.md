---
title: Overnight issue-to-merge loop
type: proposal
status: delivered — shipped in v2.5.0 (code plugin v1.5.0: `/code:issue-loop`, `engines/issue-loop.js`, preflight + publish rails)
date: 2026-08-04
origin: /insights on_the_horizon #3; deferral ruled by Keeper 2026-08-04
---

# Overnight issue-to-merge loop — design spec

> **Historical record — delivered and superseded as operating guidance.** The
> design below is preserved as written. Use the
> [Code plugin guide](../../plugins/code/README.md) and current implementation
> for supported behavior.

Deliberately **not built** in the session that wrote this: the build deserves a
fresh context, and half its prerequisites shipped today. This spec is the warm
start.

## Goal

A dispatcher run (headless `claude -p`, `session:loop`, or a cron routine)
that: triages open GitHub issues, spawns one worker per *safe* issue in an
isolated worktree, iterates each against the test suite to green, has a cold
reviewer check the diff for scope creep, and opens **draft PRs only**. The
human reviews merges over coffee; the machine never merges.

## Architecture

```
1. TRIAGE     score each open issue for autonomy-safety; reject ambiguity
2. DISPATCH   one worker per accepted issue, own worktree, own branch
3. ITERATE    worker: failing test FIRST, then fix, loop until suite green
              (hard attempt cap; giving up is a valid, reported outcome)
4. REVIEW     cold reviewer reads the diff + ONLY the issue text
5. PUBLISH    draft PR per pass; run report for everything else
```

## Component mapping (what already exists in asha)

| Stage | Existing piece | Gap to close |
|---|---|---|
| **GitHub transport** | `gh` CLI — present on the reference machine (2.45.0), and the toolkit's own policy rules already reference it | the loop must probe `command -v gh` + auth at startup and surrender triage cleanly when absent; some asha environments (e.g. web-sandbox sessions, per CLAUDE.md's git-workflow notes) do not have it |
| Dispatch/caps | `session:loop` + `loop-operator` agent (checkpoints, failure detection) | per-issue fan-out |
| Isolation | `loop-operator` today accepts **branch-only** isolation and merely warns when isolation is absent; worktree semantics exist but are not a portable asha seam across harnesses | ENFORCE worktree-per-worker (refuse to dispatch without it), naming + cleanup convention |
| Review | `reviewer` agent (read-only) + Change Budget module (cognitive.md) | diff-vs-issue scope check prompt |
| Verification discipline | `commission-loop`'s verdict rules (uncertainty fails; dead verifier fails) | apply to the reviewer stage |
| Guardrails | policy rules (no force-push, no destructive delete, marker exemptions) | verify coverage against the loop's OWN commands before enabling (rail 6 below) — "already active" is exactly the assumption that rule was written to forbid |
| TDD contract | `tdd` agent, agent-coordination rules | wire as the worker's required first step |

## Triage rubric (autonomy-safety score)

Accept an issue only if ALL hold; otherwise record the clarification needed in
the run report and skip — posting it as an issue comment is an outward write,
gated behind open question 3 (default off). A wrong guess implemented
overnight is worse than no progress:

1. Acceptance criteria are stated or trivially inferable from the issue text.
2. The touched area is covered by tests (worker can prove itself green).
3. Blast radius is local — no installer/uninstaller, no hook contracts, no
   cross-harness surfaces (those change behavior on machines asha doesn't see).
4. No security-sensitive surface (auth, secrets, policy rules themselves).
5. The fix does not require a product decision the Keeper hasn't made.

## Safety rails (non-negotiable, learned the hard way)

- **Failing test first.** A worker that cannot write a failing test for the
  issue does not understand the issue; it reports that instead of coding.
- **Attempt cap** (default 3 green-loop attempts) then surrender with a
  diagnosis — the RP turn loop's surrender pattern, verbatim.
- **Reviewer reads cold**: the diff and the issue text ONLY — not the worker's
  reasoning. Scope creep is judged against the Change Budget rule: any file
  outside the issue's plausible surface is a finding.
- **Draft PRs only; never push main; never merge.** The loop's output is
  reviewable proposals, same promotion discipline as commission-loop.
- **Run report always**: every decision (triaged-out, surrendered, published)
  logged to one morning-readable file. Silence is never success — a stage
  that produced nothing says so and says why.
- **Verify guards against the loop's own workflows before enabling** (the
  destructive-delete BLOCKER lesson: the delete rule denied asha's own marker
  cleanup because it was only ever tested against the motivating incident).

## Open questions for the build session

1. Dispatcher form: `session:loop` recipe vs a Workflow script (commission-loop
   precedent) vs a `code:` command. Leaning Workflow script for determinism +
   resumability.
2. Where run reports live (`Work/loops/<date>/`?) and their retention.
3. Whether triage comments on rejected issues are wanted (writes to the
   tracker = outward-facing; likely Keeper-gated, off by default).
4. Per-repo enablement: this must be opt-in per project (an `.asha/` flag),
   never a default — the loop mutates branches and opens PRs.

## Explicitly out of scope

Auto-merge (never), issue *creation*, non-GitHub trackers (v1), and running
against asha itself until the loop has a track record on lower-stakes repos.
