# Code engines

Workflow-tool scripts for the code plugin — same form as
`plugins/write/engines/` (the precedent): pure orchestration executed by the
`Workflow` tool with injected primitives (`args`, `agent`, `parallel`,
`pipeline`, `log`, `phase`). No filesystem access, no imports, no clock, no
randomness — every side effect happens inside a spawned agent, timestamps and
paths arrive via `args`, and stable per-call labels make runs resumable under
the Workflow runtime's cache.

## issue-loop

The overnight issue-to-merge loop (design spec:
`docs/proposals/2026-08-04--issue-to-merge-loop.md`).

```
1. TRIAGE     score each open issue for autonomy-safety; uncertainty rejects
2. ITERATE    one worker per accepted issue, own worktree, own branch —
              failing test FIRST, fix to green, attempt cap then surrender
3. REVIEW     cold reviewer reads the diff + ONLY the issue text
4. PUBLISH    draft PR per pass, via the guarded publisher script
5. REPORT     one morning-readable run report — silence is never success
```

**Never invoke the engine directly.** The entry point is `/code:issue-loop`,
which runs `tools/issue-loop-preflight.sh` first — the safety rails (dual
opt-in, gh probe, live policy-guard self-check over the loop's own command
set, worktree-root ignore check) live there, and its JSON output is the
engine's `args`, verbatim.

Verdict discipline (inherited from `commission-loop`):

- **Uncertainty fails** — triage marks uncertain criteria false; the cold
  reviewer fails when genuinely unsure a change belongs.
- **A dead agent fails its item** — a missing triage verdict rejects, a dead
  reviewer blocks publication; nothing rides a missing verdict forward.
- **Findings outrank the verdict label** — a `pass` carrying a hard finding
  (scope-creep / unrelated-change / missing-test / suspicious) is a fail, and
  the acceptance conjunction is recomputed engine-side from the five criteria.
- **Silence is never success** — thrown pipeline stages are rebuilt into
  indexed failure envelopes; every fetched issue appears in the report.

Write/push boundary: workers are instructed never to push; publication is
structurally confined to `tools/issue-loop-publish.sh`, which refuses
main/master, foreign prefixes, unregistered worktrees, and dirty trees, and
hardcodes `--draft`. Cleanup is `git worktree remove` only — the `rm -rf`
form stays policy-denied on purpose (pinned in Test 104).

Wiring test: `tests/js/issue-loop.test.mjs` (auto-discovered by Suite 13);
rail tests: `tests/test-issue-loop.sh` (Suite 14).
