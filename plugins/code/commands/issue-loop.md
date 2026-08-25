---
name: code-issue-loop
description: "Overnight issue-to-merge loop: triage open GitHub issues, one worker per safe issue in an isolated worktree, cold review, draft PRs only — the loop never merges"
argument-hint: "[--dry-run]"
allowed-tools: ["Bash", "Read", "Write", "Workflow"]
---

# /code:issue-loop

Dispatch the issue-to-merge loop against the current repository: triage open
GitHub issues for autonomy-safety, fix each accepted issue test-first in its
own worktree, cold-review every diff against the issue text alone, and open
**draft PRs only**. The human reviews merges over coffee; the machine never
merges.

## Usage

```
/code:issue-loop            # full run
/code:issue-loop --dry-run  # safety rails + preflight only; nothing dispatched
```

## Preconditions (dual opt-in — both sides, always)

1. **Project side**: the target repo has committed `.asha/issue-loop.json`
   with `enabled: true` (template: `plugins/code/templates/issue-loop.json`;
   `test_command` is required — it is how a worker proves itself green).
2. **User side**: the repo's absolute path is listed under
   `issue_loop.repos` in `~/.asha/config.json` on this machine.

A cloned repo cannot self-authorize, and a local allowlist cannot enable a
repo that never opted in. Also required: `gh` authenticated, and
`.asha/worktrees/` git-ignored in the target repo.

## Behavior

### Step 1: Safety rails (preflight)

```bash
ASHA_ROOT="${ASHA_ROOT:-$(jq -r '.asha_root // empty' "${ASHA_HOME:-$HOME/.asha}/config.json" 2>/dev/null)}"
ARGS_JSON="$(bash "$ASHA_ROOT/plugins/code/tools/issue-loop-preflight.sh")"
```

Preflight enforces the dual opt-in, probes `gh`, checks the worktree-root
ignore, and pipes the loop's **own command set** through the live policy
guard (repo rules + the user's `~/.asha/policies.json` overlay) — anything
the guard would deny or prompt on refuses dispatch now, not overnight, and
gutted deny-side protections (force-push, `rm -rf` of worktrees) refuse too.

**If preflight refuses: STOP.** Relay its stderr verbatim — the refusal is
the answer. Do not weaken a policy, hand-edit a config, or work around the
gate to make the run happen; every refusal message names the legitimate fix.

With `--dry-run`: print `ARGS_JSON` and stop here.

### Step 2: Dispatch the engine

Invoke the **Workflow** tool with:

- `scriptPath`: `$ASHA_ROOT/plugins/code/engines/issue-loop.js`
- `args`: the parsed `ARGS_JSON` object, verbatim (actual JSON, not a string)

The engine runs Triage → Iterate → Review → Publish → Report. Its stages are
prompt- and engine-enforced: failing test before any fix, attempt cap then
surrender, worktree evidence checked engine-side, cold review that fails on
uncertainty and on dead reviewers, publication only through
`issue-loop-publish.sh` (which refuses main/master and hardcodes `--draft`).

### Step 3: The report is non-optional

- If the result has `report_written: false`, write `fallback_report` to
  `<repo>/<run_dir>/report.md` yourself (`mkdir -p` first).
- If the Workflow itself died, write a minimal failure report to the same
  path stating what ran and where it stopped.

Silence is never success — a run with no report never happened cleanly.

### Step 4: Relay

Report to the user: the stats line (fetched / dispatched / published /
surrendered / rejected / deferred), the report path, and each draft-PR link.
Surrendered and errored worktrees stay on disk for inspection — list them,
and clean up only with `git worktree remove` after the human has read the
report.

## What this loop will never do

- Merge anything, push main/master, or force-push (structurally: its only
  push path is `issue-loop-publish.sh`).
- Open a non-draft PR (the `--draft` flag is hardcoded in the publisher).
- Comment on issues or write to the tracker beyond the draft PRs (v1 has no
  outward-write path at all; rejections live in the run report).
- Delete worktrees with `rm -rf` (cleanup is `git worktree remove`, and the
  policy guard denies the rm form — that denial is pinned in Test 104).
- Run without both halves of the opt-in.
