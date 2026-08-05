#!/usr/bin/env bash
# issue-loop-publish.sh — the loop's SOLE push path.
#
# The "never push main, never merge, draft PRs only" rail is enforced here
# STRUCTURALLY rather than by a global policy rule: a policy deny on
# `git push origin main` would break the human's own legitimate pushes (that
# allowance is pinned intentionally in Test 104), so instead every publish the
# loop performs must route through this script, which:
#   - refuses main/master and any branch outside the enforced prefix
#   - refuses worktree paths not registered with `git worktree list`
#   - refuses when the worktree's HEAD is not the named branch
#   - refuses dirty worktrees (workers commit; publishers publish)
#   - pushes with plain `git push -u origin <branch>` — no force, ever
#   - opens the PR with `gh pr create --draft` — the flag is hardcoded
#
# PR body arrives on stdin. Fails closed: any check error refuses the publish.

set -euo pipefail

refuse() {
    echo "issue-loop-publish: refuse: $*" >&2
    exit 1
}

usage() {
    echo "usage: issue-loop-publish.sh --repo <root> --worktree <path> --branch <name> --branch-prefix <prefix> --issue <number> --title <title>  (PR body on stdin)" >&2
    exit 1
}

REPO="" WORKTREE="" BRANCH="" PREFIX="" ISSUE="" TITLE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo)          REPO="${2:-}"; shift 2 ;;
        --worktree)      WORKTREE="${2:-}"; shift 2 ;;
        --branch)        BRANCH="${2:-}"; shift 2 ;;
        --branch-prefix) PREFIX="${2:-}"; shift 2 ;;
        --issue)         ISSUE="${2:-}"; shift 2 ;;
        --title)         TITLE="${2:-}"; shift 2 ;;
        *) usage ;;
    esac
done
[[ -n "$REPO" && -n "$WORKTREE" && -n "$BRANCH" && -n "$PREFIX" && -n "$ISSUE" && -n "$TITLE" ]] || usage

command -v git >/dev/null 2>&1 || refuse "git not found"
command -v gh  >/dev/null 2>&1 || refuse "gh not found"

# --- Branch identity: never main/master, always inside the loop's namespace --
case "$BRANCH" in
    main|master) refuse "branch '$BRANCH' is a protected branch — the loop never pushes main/master" ;;
esac
[[ "$BRANCH" == "$PREFIX"* ]] || refuse "branch '$BRANCH' is outside the enforced prefix '$PREFIX'"
[[ "$BRANCH" != "$PREFIX" ]] || refuse "branch '$BRANCH' is the bare prefix — no issue segment"

# --- Worktree identity: registered, on the named branch, clean ---------------
REAL_WT="$(realpath "$WORKTREE" 2>/dev/null)" || refuse "worktree path does not exist: $WORKTREE"
git -C "$REPO" worktree list --porcelain 2>/dev/null | grep -Fxq "worktree $REAL_WT" \
    || refuse "path is not a registered worktree of $REPO: $REAL_WT"

HEAD_BRANCH="$(git -C "$REAL_WT" rev-parse --abbrev-ref HEAD 2>/dev/null)" \
    || refuse "cannot resolve HEAD in worktree $REAL_WT"
[[ "$HEAD_BRANCH" == "$BRANCH" ]] \
    || refuse "worktree HEAD is '$HEAD_BRANCH', not the named branch '$BRANCH'"

[[ -z "$(git -C "$REAL_WT" status --porcelain 2>/dev/null)" ]] \
    || refuse "worktree has uncommitted changes — workers commit before publish"

# --- Publish: plain push, draft PR -------------------------------------------
git -C "$REAL_WT" push -u origin "$BRANCH" \
    || refuse "push failed for $BRANCH (no retry here — the run report records the failure)"

PR_URL="$(cd "$REAL_WT" && gh pr create --draft --title "$TITLE" --body-file - --head "$BRANCH")" \
    || refuse "gh pr create failed for $BRANCH after push — branch is on the remote; report and continue"

echo "issue-loop-publish: draft PR for issue #$ISSUE: $PR_URL"
