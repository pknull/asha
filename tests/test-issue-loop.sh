#!/usr/bin/env bash
# test-issue-loop.sh - Behavioral tests for the issue-to-merge loop's safety rails:
#   plugins/code/tools/issue-loop-preflight.sh  (dual opt-in, gh probe, guard
#     self-check, worktree-root ignore check, args JSON)
#   plugins/code/tools/issue-loop-publish.sh    (sole push path: draft PRs only,
#     never main/master, prefix-enforced branches, clean worktrees only)
#
# Everything runs against fixture repos + fixture $HOME under mktemp; gh is a
# PATH shim that records its argv. No network, no real gh, no writes outside
# the temp dir.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

PREFLIGHT="$REPO_ROOT/plugins/code/tools/issue-loop-preflight.sh"
PUBLISH="$REPO_ROOT/plugins/code/tools/issue-loop-publish.sh"
TEMPLATE="$REPO_ROOT/plugins/code/templates/issue-loop.json"

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

PASSED=0
FAILED=0

TEST_DIR=$(mktemp -d)
trap 'rm -rf "$TEST_DIR"' EXIT

echo -e "${BLUE}=== Issue-Loop Safety Rail Test Suite ===${NC}"
echo "Repository: $REPO_ROOT"
echo "Test directory: $TEST_DIR"
echo ""

pass() { echo -e "${GREEN}PASS${NC}"; PASSED=$((PASSED + 1)); }
fail() { echo -e "${RED}FAIL${NC}"; [[ $# -gt 0 ]] && echo "  $1"; FAILED=$((FAILED + 1)); }

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# gh shim: records argv, succeeds on auth status / issue list / pr create.
SHIM_DIR="$TEST_DIR/shim"
mkdir -p "$SHIM_DIR"
GH_LOG="$TEST_DIR/gh-calls.log"
cat > "$SHIM_DIR/gh" <<EOF
#!/usr/bin/env bash
echo "\$*" >> "$GH_LOG"
case "\$1" in
  auth) exit 0 ;;
  pr)   echo "https://github.com/example/repo/pull/999"; exit 0 ;;
  *)    exit 0 ;;
esac
EOF
chmod +x "$SHIM_DIR/gh"

# gh shim variant: auth fails (covers both "absent" and "unauthenticated" —
# preflight's probe is a single branch for the pair).
BADAUTH_DIR="$TEST_DIR/shim-badauth"
mkdir -p "$BADAUTH_DIR"
cat > "$BADAUTH_DIR/gh" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "$BADAUTH_DIR/gh"

# Fixture HOME with the user-side allowlist. Populated per-test.
make_home() {
    local home="$1"; shift
    local repos_json="${1:-[]}"
    mkdir -p "$home/.asha"
    cat > "$home/.asha/config.json" <<EOF
{"asha_root": "$REPO_ROOT", "issue_loop": {"repos": $repos_json}}
EOF
}

# Fixture target repo (git init, one commit, .gitignore covering .asha/worktrees/).
make_repo() {
    local repo="$1"
    mkdir -p "$repo"
    git -C "$repo" init -q -b main
    git -C "$repo" config user.email test@example.com
    git -C "$repo" config user.name Test
    echo "# fixture" > "$repo/README.md"
    printf '.asha/worktrees/\n' > "$repo/.gitignore"
    git -C "$repo" -c commit.gpgsign=false add -A
    git -C "$repo" -c commit.gpgsign=false commit -qm init
}

# Project-side opt-in flag.
enable_repo() {
    local repo="$1"
    mkdir -p "$repo/.asha"
    cat > "$repo/.asha/issue-loop.json" <<'EOF'
{"enabled": true, "test_command": "true", "attempt_cap": 3, "branch_prefix": "issue-loop/", "max_issues": 5}
EOF
}

run_preflight() {
    # $1=HOME $2=repo, rest: extra env as VAR=val
    local home="$1" repo="$2"; shift 2
    local rc=0
    PRE_OUT="$(cd "$repo" && env HOME="$home" ASHA_ROOT="$REPO_ROOT" \
        PATH="$SHIM_DIR:$PATH" "$@" bash "$PREFLIGHT" 2>"$TEST_DIR/pre.err")" || rc=$?
    PRE_ERR="$(cat "$TEST_DIR/pre.err")"
    return $rc
}

# ---------------------------------------------------------------------------
# Preflight: dual opt-in
# ---------------------------------------------------------------------------

echo -n "Test IL-1: preflight refuses without the project opt-in flag... "
HOME_A="$TEST_DIR/home-a"; REPO_A="$TEST_DIR/repo-a"
make_repo "$REPO_A"
make_home "$HOME_A" "[\"$REPO_A\"]"
if run_preflight "$HOME_A" "$REPO_A"; then
    fail "preflight exited 0 with no .asha/issue-loop.json"
elif echo "$PRE_ERR" | grep -q "issue-loop.json"; then
    pass
else
    fail "refusal did not name the missing project flag: $PRE_ERR"
fi

echo -n "Test IL-2: preflight refuses when project flag exists but enabled=false... "
enable_repo "$REPO_A"
sed 's/"enabled": true/"enabled": false/' "$REPO_A/.asha/issue-loop.json" > "$REPO_A/.asha/issue-loop.json.tmp"
mv "$REPO_A/.asha/issue-loop.json.tmp" "$REPO_A/.asha/issue-loop.json"
if run_preflight "$HOME_A" "$REPO_A"; then
    fail "preflight exited 0 with enabled=false"
else
    pass
fi
sed 's/"enabled": false/"enabled": true/' "$REPO_A/.asha/issue-loop.json" > "$REPO_A/.asha/issue-loop.json.tmp"
mv "$REPO_A/.asha/issue-loop.json.tmp" "$REPO_A/.asha/issue-loop.json"

echo -n "Test IL-3: preflight refuses when repo is not in the user allowlist... "
HOME_B="$TEST_DIR/home-b"
make_home "$HOME_B" "[]"
if run_preflight "$HOME_B" "$REPO_A"; then
    fail "preflight exited 0 with repo absent from ~/.asha/config.json allowlist"
elif echo "$PRE_ERR" | grep -q "allowlist"; then
    pass
else
    fail "refusal did not name the allowlist: $PRE_ERR"
fi

echo -n "Test IL-4: preflight refuses when only the allowlist grants (flag file gone)... "
REPO_B="$TEST_DIR/repo-b"
make_repo "$REPO_B"
if run_preflight "$HOME_A" "$REPO_B"; then
    fail "allowlist alone must not enable a repo that never opted in"
else
    pass
fi

# ---------------------------------------------------------------------------
# Preflight: gh probe, ignore check, config validation
# ---------------------------------------------------------------------------

echo -n "Test IL-5: preflight surrenders cleanly when gh auth fails... "
rc=0
PRE_ERR_5="$( (cd "$REPO_A" && env HOME="$HOME_A" ASHA_ROOT="$REPO_ROOT" \
    PATH="$BADAUTH_DIR:$PATH" bash "$PREFLIGHT" >/dev/null) 2>&1 )" || rc=$?
if [[ $rc -eq 0 ]]; then
    fail "preflight exited 0 with failing gh auth"
elif echo "$PRE_ERR_5" | grep -qi "gh"; then
    pass
else
    fail "refusal did not mention gh: $PRE_ERR_5"
fi

echo -n "Test IL-6: preflight refuses when .asha/worktrees is not git-ignored... "
REPO_C="$TEST_DIR/repo-c"
make_repo "$REPO_C"
enable_repo "$REPO_C"
: > "$REPO_C/.gitignore"
git -C "$REPO_C" -c commit.gpgsign=false add -A
git -C "$REPO_C" -c commit.gpgsign=false commit -qm "drop ignore"
HOME_C="$TEST_DIR/home-c"
make_home "$HOME_C" "[\"$REPO_C\"]"
if run_preflight "$HOME_C" "$REPO_C"; then
    fail "preflight exited 0 with .asha/worktrees unignored"
elif echo "$PRE_ERR" | grep -q "worktrees"; then
    pass
else
    fail "refusal did not name the worktree root: $PRE_ERR"
fi

echo -n "Test IL-7: preflight refuses a config with no test_command... "
REPO_D="$TEST_DIR/repo-d"
make_repo "$REPO_D"
mkdir -p "$REPO_D/.asha"
echo '{"enabled": true}' > "$REPO_D/.asha/issue-loop.json"
HOME_D="$TEST_DIR/home-d"
make_home "$HOME_D" "[\"$REPO_D\"]"
if run_preflight "$HOME_D" "$REPO_D"; then
    fail "a loop that cannot prove itself green must not dispatch"
elif echo "$PRE_ERR" | grep -q "test_command"; then
    pass
else
    fail "refusal did not name test_command: $PRE_ERR"
fi

echo -n "Test IL-8: preflight refuses a branch_prefix that could shadow main/master... "
REPO_E="$TEST_DIR/repo-e"
make_repo "$REPO_E"
mkdir -p "$REPO_E/.asha"
cat > "$REPO_E/.asha/issue-loop.json" <<'EOF'
{"enabled": true, "test_command": "true", "branch_prefix": "main"}
EOF
HOME_E="$TEST_DIR/home-e"
make_home "$HOME_E" "[\"$REPO_E\"]"
if run_preflight "$HOME_E" "$REPO_E"; then
    fail "branch_prefix=main accepted"
else
    pass
fi

# ---------------------------------------------------------------------------
# Preflight: live guard self-check (rail 6 at runtime)
# ---------------------------------------------------------------------------

echo -n "Test IL-9: happy path emits args JSON (and the guard self-check passed)... "
if run_preflight "$HOME_A" "$REPO_A"; then
    if echo "$PRE_OUT" | jq -e '.repo_root and .run_dir and .asha_root and .config.test_command and .config.attempt_cap and .config.branch_prefix' >/dev/null 2>&1; then
        pass
    else
        fail "stdout is not the expected args JSON: $PRE_OUT"
    fi
else
    fail "preflight refused a fully enabled fixture: $PRE_ERR"
fi

echo -n "Test IL-10: run_dir lands under Work/loops/ with a dated run id... "
if echo "$PRE_OUT" | jq -r '.run_dir' | grep -Eq '^Work/loops/[0-9]{4}-[0-9]{2}-[0-9]{2}--[0-9]{4}--issue-loop$'; then
    pass
else
    fail "run_dir shape unexpected: $(echo "$PRE_OUT" | jq -r '.run_dir')"
fi

echo -n "Test IL-11: a user policy that denies the loop's own commands blocks dispatch... "
HOME_F="$TEST_DIR/home-f"
make_home "$HOME_F" "[\"$REPO_A\"]"
cat > "$HOME_F/.asha/policies.json" <<'EOF'
{"rules":[{"id":"test-block-worktree","tool":"Bash","command_regex":"git[[:space:]]+worktree","action":"deny","reason":"fixture: worktrees forbidden"}]}
EOF
if run_preflight "$HOME_F" "$REPO_A"; then
    fail "preflight dispatched although policy-guard denies git worktree"
elif echo "$PRE_ERR" | grep -q "worktree"; then
    pass
else
    fail "refusal did not name the denied command: $PRE_ERR"
fi

echo -n "Test IL-12: a weakened deny-side guard (force push allowed) also blocks dispatch... "
HOME_G="$TEST_DIR/home-g"
make_home "$HOME_G" "[\"$REPO_A\"]"
cat > "$HOME_G/.asha/policies.json" <<'EOF'
{"rules":[{"id":"destructive-git","tool":"Bash","command_regex":"git[[:space:]]+never-matches-anything","action":"deny","reason":"fixture: gutted rule"}]}
EOF
if run_preflight "$HOME_G" "$REPO_A"; then
    fail "preflight dispatched although force-push protection is gutted"
elif echo "$PRE_ERR" | grep -qi "force"; then
    pass
else
    fail "refusal did not name the weakened expectation: $PRE_ERR"
fi

# ---------------------------------------------------------------------------
# Publisher: the sole push path
# ---------------------------------------------------------------------------

# Fixture: repo with a bare origin, a worktree on a prefixed branch, one commit.
PUB_REPO="$TEST_DIR/pub-repo"
PUB_REMOTE="$TEST_DIR/pub-remote.git"
make_repo "$PUB_REPO"
git init -q --bare "$PUB_REMOTE"
git -C "$PUB_REPO" remote add origin "$PUB_REMOTE"
git -C "$PUB_REPO" push -qu origin main
WT="$PUB_REPO/.asha/worktrees/issue-7"
git -C "$PUB_REPO" worktree add -q "$WT" -b issue-loop/issue-7
echo fix > "$WT/fix.txt"
git -C "$WT" -c commit.gpgsign=false add -A
git -C "$WT" -c commit.gpgsign=false commit -qm "fix: issue 7"

run_publish() {
    local rc=0
    PUB_OUT="$(env PATH="$SHIM_DIR:$PATH" bash "$PUBLISH" "$@" \
        <<< "Fixes #7 — automated draft from issue-loop." 2>"$TEST_DIR/pub.err")" || rc=$?
    PUB_ERR="$(cat "$TEST_DIR/pub.err")"
    return $rc
}

echo -n "Test IL-13: publisher pushes the branch and opens a DRAFT PR... "
: > "$GH_LOG"
if run_publish --repo "$PUB_REPO" --worktree "$WT" --branch issue-loop/issue-7 \
        --branch-prefix issue-loop/ --issue 7 --title "fix: issue 7"; then
    if git --git-dir="$PUB_REMOTE" show-ref --verify -q refs/heads/issue-loop/issue-7 \
        && grep -q -- "pr create" "$GH_LOG" && grep -q -- "--draft" "$GH_LOG"; then
        pass
    else
        fail "push or draft flag missing (remote refs: $(git --git-dir="$PUB_REMOTE" for-each-ref --format='%(refname)'); gh: $(cat "$GH_LOG"))"
    fi
else
    fail "publisher refused a clean prefixed branch: $PUB_ERR"
fi

echo -n "Test IL-14: publisher never force-pushes and pushes only the named branch... "
if grep -Eq -- '--force|-f( |$)' "$GH_LOG"; then
    fail "gh invoked with a force flag"
elif git --git-dir="$PUB_REMOTE" show-ref --verify -q refs/heads/main; then
    # main exists from setup; assert it was not advanced by the publisher
    MAIN_LOCAL="$(git -C "$PUB_REPO" rev-parse main)"
    MAIN_REMOTE="$(git --git-dir="$PUB_REMOTE" rev-parse refs/heads/main)"
    if [[ "$MAIN_LOCAL" == "$MAIN_REMOTE" ]]; then pass; else fail "remote main moved"; fi
else
    fail "remote main ref disappeared"
fi

echo -n "Test IL-15: publisher refuses branch=main outright... "
if run_publish --repo "$PUB_REPO" --worktree "$WT" --branch main \
        --branch-prefix issue-loop/ --issue 7 --title "nope"; then
    fail "publisher accepted main"
elif echo "$PUB_ERR" | grep -qi "main"; then
    pass
else
    fail "refusal did not name the branch: $PUB_ERR"
fi

echo -n "Test IL-16: publisher refuses a branch outside the enforced prefix... "
git -C "$PUB_REPO" worktree add -q "$PUB_REPO/.asha/worktrees/rogue" -b rogue-branch
if run_publish --repo "$PUB_REPO" --worktree "$PUB_REPO/.asha/worktrees/rogue" \
        --branch rogue-branch --branch-prefix issue-loop/ --issue 7 --title "nope"; then
    fail "publisher accepted a non-prefixed branch"
else
    pass
fi

echo -n "Test IL-17: publisher refuses a dirty worktree... "
echo uncommitted > "$WT/dirty.txt"
if run_publish --repo "$PUB_REPO" --worktree "$WT" --branch issue-loop/issue-7 \
        --branch-prefix issue-loop/ --issue 7 --title "nope"; then
    fail "publisher accepted uncommitted changes"
elif echo "$PUB_ERR" | grep -qi "uncommitted\|dirty"; then
    pass
else
    fail "refusal did not explain dirtiness: $PUB_ERR"
fi
rm -f "$WT/dirty.txt"

echo -n "Test IL-18: publisher refuses when worktree HEAD is not the named branch... "
if run_publish --repo "$PUB_REPO" --worktree "$WT" --branch issue-loop/issue-8 \
        --branch-prefix issue-loop/ --issue 8 --title "nope"; then
    fail "publisher accepted a branch/worktree mismatch"
else
    pass
fi

echo -n "Test IL-19: publisher refuses a path that is not a registered worktree... "
FAKE_WT="$TEST_DIR/not-a-worktree"
mkdir -p "$FAKE_WT"
if run_publish --repo "$PUB_REPO" --worktree "$FAKE_WT" --branch issue-loop/issue-7 \
        --branch-prefix issue-loop/ --issue 7 --title "nope"; then
    fail "publisher accepted an unregistered worktree path"
else
    pass
fi

# ---------------------------------------------------------------------------
# Template sanity
# ---------------------------------------------------------------------------

echo -n "Test IL-20: shipped issue-loop.json template is valid and disabled by default... "
if jq -e '.enabled == false and (.test_command | type == "string")' "$TEMPLATE" >/dev/null 2>&1; then
    pass
else
    fail "template missing, invalid JSON, or enabled by default"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
echo -e "${BLUE}=== Issue-Loop Test Summary ===${NC}"
echo -e "Passed: ${GREEN}$PASSED${NC}"
echo -e "Failed: ${RED}$FAILED${NC}"
[[ $FAILED -eq 0 ]] || exit 1
exit 0
