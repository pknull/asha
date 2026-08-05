#!/usr/bin/env bash
# issue-loop-preflight.sh — safety rails for the issue-to-merge loop.
#
# Runs BEFORE any dispatch. Every check here is a refusal path; only a fully
# clean pass emits the args JSON the engine consumes. Order:
#   1. dual opt-in   — the target repo committed .asha/issue-loop.json with
#                      enabled=true AND this machine's ~/.asha/config.json
#                      lists the repo under .issue_loop.repos[]. A cloned repo
#                      cannot self-authorize; a local allowlist cannot enable
#                      a repo that never opted in.
#   2. gh probe      — command -v gh + gh auth status; surrender cleanly when
#                      absent or unauthenticated (some asha environments have
#                      no gh at all).
#   3. config sanity — test_command is required (a worker that cannot prove
#                      itself green must not dispatch); branch_prefix must not
#                      shadow main/master refs.
#   4. ignore check  — .asha/worktrees/ must be git-ignored in the target repo
#                      or per-issue worktrees would dirty every diff.
#   5. guard self-check (rail 6, live) — the loop's OWN command set is piped
#                      through the real policy-guard (repo rules + the user's
#                      ~/.asha/policies.json overlay). Anything the guard would
#                      deny or prompt on refuses dispatch NOW, not overnight;
#                      and the deny-side expectations (force-push, rm -rf of
#                      worktrees) must still deny — a weakened guard also
#                      refuses. "Already active" is the assumption this rail
#                      exists to forbid.
#
# Unlike hook handlers, this gate FAILS CLOSED: any error refuses dispatch.

set -euo pipefail

refuse() {
    echo "issue-loop: refuse: $*" >&2
    exit 1
}

command -v jq >/dev/null 2>&1 || refuse "jq is required for preflight and is not installed"
command -v git >/dev/null 2>&1 || refuse "git is required and is not installed"

# --------------------------------------------------------------------------
# Repo + dual opt-in
# --------------------------------------------------------------------------

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" \
    || refuse "not inside a git repository"
REAL_ROOT="$(realpath "$REPO_ROOT")"

FLAG="$REPO_ROOT/.asha/issue-loop.json"
[[ -f "$FLAG" ]] || refuse "project has not opted in — no .asha/issue-loop.json in $REPO_ROOT (template: plugins/code/templates/issue-loop.json)"

jq -e . "$FLAG" >/dev/null 2>&1 || refuse ".asha/issue-loop.json is not valid JSON"
ENABLED="$(jq -r '.enabled // false' "$FLAG")"
[[ "$ENABLED" == "true" ]] || refuse ".asha/issue-loop.json has enabled=false — project opt-in is explicit"

USER_CFG="$HOME/.asha/config.json"
[[ -f "$USER_CFG" ]] || refuse "no ~/.asha/config.json — the user-side allowlist is required (add .issue_loop.repos)"

IN_LIST=false
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    e="$(realpath -m "$entry" 2>/dev/null || printf '%s' "$entry")"
    [[ "$e" == "$REAL_ROOT" ]] && IN_LIST=true
done < <(jq -r '.issue_loop.repos // [] | .[]' "$USER_CFG" 2>/dev/null || true)
[[ "$IN_LIST" == "true" ]] || refuse "repo $REAL_ROOT is not in the ~/.asha/config.json issue_loop.repos allowlist — both sides must opt in"

# --------------------------------------------------------------------------
# gh probe — surrender before triage, not mid-run
# --------------------------------------------------------------------------

if ! command -v gh >/dev/null 2>&1 || ! gh auth status >/dev/null 2>&1; then
    refuse "gh CLI unavailable or unauthenticated — surrendering before triage"
fi

# --------------------------------------------------------------------------
# Loop config: required fields + safe defaults
# --------------------------------------------------------------------------

TEST_COMMAND="$(jq -r '.test_command // empty' "$FLAG")"
[[ -n "$TEST_COMMAND" ]] || refuse ".asha/issue-loop.json has no test_command — a worker that cannot prove itself green must not dispatch"

ATTEMPT_CAP="$(jq -r '.attempt_cap // 3' "$FLAG")"
[[ "$ATTEMPT_CAP" =~ ^[1-9][0-9]*$ ]] || refuse "attempt_cap must be a positive integer (got: $ATTEMPT_CAP)"

MAX_ISSUES="$(jq -r '.max_issues // 5' "$FLAG")"
[[ "$MAX_ISSUES" =~ ^[1-9][0-9]*$ ]] || refuse "max_issues must be a positive integer (got: $MAX_ISSUES)"

BRANCH_PREFIX="$(jq -r '.branch_prefix // "issue-loop/"' "$FLAG")"
[[ "$BRANCH_PREFIX" == */ ]] || BRANCH_PREFIX="${BRANCH_PREFIX}/"
case "$BRANCH_PREFIX" in
    main/*|master/*|main|master)
        # refs/heads/main and refs/heads/main/issue-7 cannot coexist; a prefix
        # in main/'s namespace either collides or shadows the protected branch.
        refuse "branch_prefix '$BRANCH_PREFIX' would shadow main/master refs"
        ;;
esac

# --------------------------------------------------------------------------
# Worktree root must be ignored — per-issue worktrees live in-repo
# --------------------------------------------------------------------------

git -C "$REPO_ROOT" check-ignore -q ".asha/worktrees/__probe__" 2>/dev/null \
    || refuse ".asha/worktrees/ is not git-ignored in $REPO_ROOT — add '.asha/worktrees/' to .gitignore before enabling the loop"

# --------------------------------------------------------------------------
# Guard self-check (rail 6): verify the guards against the loop's own commands
# --------------------------------------------------------------------------

valid_asha_root() {
    [[ -n "$1" && -f "$1/plugins/session/hooks/handlers/policy-guard.sh" ]]
}

resolve_asha_root() {
    if valid_asha_root "${ASHA_ROOT:-}"; then
        echo "$ASHA_ROOT"; return 0
    fi
    local cfg_root
    cfg_root="$(jq -r '.asha_root // empty' "$USER_CFG" 2>/dev/null || true)"
    if valid_asha_root "$cfg_root"; then
        echo "$cfg_root"; return 0
    fi
    # tools/ is three levels below the asha root; survives symlink mounts.
    local self_dir candidate
    self_dir="$(cd -P "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" >/dev/null 2>&1 && pwd)" || true
    candidate="${self_dir%/plugins/code/tools}"
    if valid_asha_root "$candidate"; then
        echo "$candidate"; return 0
    fi
    return 1
}

ASHA_ROOT_RESOLVED="$(resolve_asha_root)" \
    || refuse "cannot locate the asha policy guard (set ASHA_ROOT or fix ~/.asha/config.json asha_root) — dispatching without verified guards is not an option"
GUARD="$ASHA_ROOT_RESOLVED/plugins/session/hooks/handlers/policy-guard.sh"

pg_decision() {
    # $1 = command string; prints allow | ask | deny. Same contract Test 104
    # exercises: exit 2 = deny; JSON permissionDecision = ask; else allow.
    local payload out rc=0
    payload="$(jq -cn --arg c "$1" '{tool_name:"Bash",tool_input:{command:$c}}')"
    out="$(printf '%s' "$payload" | env -u ASHA_HARNESS bash "$GUARD" 2>/dev/null)" || rc=$?
    if [[ $rc -eq 2 ]]; then
        echo deny
    elif [[ -n "$out" ]]; then
        printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // "allow"' 2>/dev/null || echo allow
    else
        echo allow
    fi
}

# The loop's own command set. Every one of these runs unattended overnight;
# a deny OR an ask (nobody is there to answer a prompt) refuses dispatch.
MUST_ALLOW=(
    "git worktree add .asha/worktrees/issue-loop-probe -b ${BRANCH_PREFIX}probe"
    "git worktree remove .asha/worktrees/issue-loop-probe"
    "git commit -m \"fix: probe\""
    "git push -u origin ${BRANCH_PREFIX}probe"
    "gh issue list --state open --json number,title,body,labels"
    "gh pr create --draft --title probe --body-file -"
    "$TEST_COMMAND"
)
for cmd in "${MUST_ALLOW[@]}"; do
    d="$(pg_decision "$cmd")"
    [[ "$d" == "allow" ]] || refuse "policy guard would $d the loop's own command: '$cmd' — fix the policy layer (or the loop) before enabling; do not weaken the guard blindly"
done

# Deny-side expectations: the protections the loop leans on must still exist.
# If a user overlay gutted them, defense-in-depth is gone — also a refusal.
MUST_DENY=(
    "git push --force origin ${BRANCH_PREFIX}probe"
    "rm -rf .asha/worktrees/issue-loop-probe"
)
for cmd in "${MUST_DENY[@]}"; do
    d="$(pg_decision "$cmd")"
    [[ "$d" == "deny" ]] || refuse "guard expectation violated: '$cmd' is no longer denied (decision: $d) — the loop's defense-in-depth has been weakened; restore the policy layer before enabling"
done

# --------------------------------------------------------------------------
# All rails green: emit the engine args
# --------------------------------------------------------------------------

RUN_ID="$(date -u +%Y-%m-%d--%H%M)--issue-loop"
BASE_BRANCH="$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"

jq -n \
    --arg repo_root "$REAL_ROOT" \
    --arg run_id "$RUN_ID" \
    --arg run_dir "Work/loops/$RUN_ID" \
    --arg now_utc "$(date -u '+%Y-%m-%d %H:%M UTC')" \
    --arg asha_root "$ASHA_ROOT_RESOLVED" \
    --arg base_branch "$BASE_BRANCH" \
    --arg test_command "$TEST_COMMAND" \
    --arg branch_prefix "$BRANCH_PREFIX" \
    --argjson attempt_cap "$ATTEMPT_CAP" \
    --argjson max_issues "$MAX_ISSUES" \
    '{
        repo_root: $repo_root,
        run_id: $run_id,
        run_dir: $run_dir,
        now_utc: $now_utc,
        asha_root: $asha_root,
        base_branch: $base_branch,
        config: {
            test_command: $test_command,
            attempt_cap: $attempt_cap,
            branch_prefix: $branch_prefix,
            max_issues: $max_issues
        }
    }'
