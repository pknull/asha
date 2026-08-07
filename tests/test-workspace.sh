#!/usr/bin/env bash
# test-workspace.sh — `asha workspace status` dispatcher + doctor section
# (workspace v1, delivery issue 3 — issue #35).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'
PASSED=0
FAILED=0

pass() { echo -e "${GREEN}PASS${NC}"; PASSED=$((PASSED + 1)); }
fail() { echo -e "${RED}FAIL${NC}"; echo "  $1"; FAILED=$((FAILED + 1)); }

FIX="$(mktemp -d)"
trap 'rm -rf "$FIX"' EXIT
export HOME="$FIX/home"   # sandbox: the walk stops before $HOME (exclusive)
mkdir -p "$HOME"

git_q() { git -c user.name=t -c user.email=t@t "$@" >/dev/null 2>&1; }

# Fixture: a valid workspace with one present child repo and one declared-
# but-missing repo; manifest committed in shared_git_root (the convention).
WS="$HOME/Code/thallus"
mkdir -p "$WS/.asha" "$WS/egregore"
cat > "$WS/.asha/workspace.json" <<'EOF'
{
  "version": 1,
  "workspace_name": "thallus",
  "repositories": [
    {"path": "egregore", "role": "svc"},
    {"path": "servitor", "role": "svc"}
  ]
}
EOF
git_q init "$WS"
( cd "$WS" && echo x > README.md && git_q add README.md .asha/workspace.json && git_q commit -m init )
git_q init "$WS/egregore"
( cd "$WS/egregore" && echo x > f && git_q add f && git_q commit -m init )

# Fixture: an invalid workspace.
BAD="$HOME/Code/badws"
mkdir -p "$BAD/.asha" "$BAD/child"
printf '{"version": 2, "workspace_name": "bad"}' > "$BAD/.asha/workspace.json"

# Fixture: no workspace at all.
LONE="$HOME/Code/solo"
mkdir -p "$LONE"

ASHA="$REPO_ROOT/bin/asha"

echo -n "Test WS-1: no workspace -> exit 0, single-project line... "
out="$("$ASHA" workspace status --start "$LONE" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *"workspace: none"* ]]; then pass; else fail "rc=$rc out=$out"; fi

echo -n "Test WS-2: valid workspace -> exit 0, essentials present... "
out="$("$ASHA" workspace status --start "$WS/egregore" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *"thallus"* && "$out" == *"egregore"* \
      && "$out" == *"servitor"* && "$out" == *"repo_missing"* ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-3: --json parses; active repo + tracked manifest... "
out="$("$ASHA" workspace status --json --start "$WS/egregore" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 ]] \
   && [[ "$(printf '%s' "$out" | jq -r '.ok')" == "true" ]] \
   && [[ "$(printf '%s' "$out" | jq -r '.active_repository')" == "egregore" ]] \
   && [[ "$(printf '%s' "$out" | jq -r '.manifest_tracked')" == "true" ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-4: untracked manifest warns per convention... "
( cd "$WS" && git_q rm --cached .asha/workspace.json )
out="$("$ASHA" workspace status --json --start "$WS/egregore" 2>&1)" && rc=0 || rc=$?
codes="$(printf '%s' "$out" | jq -r '.warnings[].code' 2>/dev/null || true)"
( cd "$WS" && git_q add .asha/workspace.json )   # restore for later tests
if [[ $rc -eq 0 && "$codes" == *"manifest_untracked"* ]]; then pass; else fail "rc=$rc codes=$codes"; fi

echo -n "Test WS-5: invalid manifest -> exit 1 with guided repair... "
out="$("$ASHA" workspace status --start "$BAD/child" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 1 && "$out" == *"repair"* && "$out" == *"workspace.json"* \
      && "$out" == *"unsupported_version"* ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-6: unknown subcommand -> usage error 2... "
"$ASHA" workspace bogus >/dev/null 2>&1 && rc=0 || rc=$?
if [[ $rc -eq 2 ]]; then pass; else fail "rc=$rc"; fi

echo -n "Test WS-7: bare 'asha workspace' -> usage error 2... "
"$ASHA" workspace >/dev/null 2>&1 && rc=0 || rc=$?
if [[ $rc -eq 2 ]]; then pass; else fail "rc=$rc"; fi

echo -n "Test WS-8: doctor section silent pass outside workspaces... "
out="$(cd "$LONE" && MARKET_ROOT="$REPO_ROOT" bash -c "
    source '$REPO_ROOT/lib/doctor.sh'; _asha_doctor_workspace_section" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && -z "$out" ]]; then pass; else fail "rc=$rc out=$out"; fi

echo -n "Test WS-9: doctor section fails closed on invalid workspace... "
out="$(cd "$BAD/child" && MARKET_ROOT="$REPO_ROOT" bash -c "
    source '$REPO_ROOT/lib/doctor.sh'; _asha_doctor_workspace_section" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 1 && "$out" == *"Workspace"* && "$out" == *"repair"* ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-10: doctor section reports valid workspace, rc 0... "
out="$(cd "$WS/egregore" && MARKET_ROOT="$REPO_ROOT" bash -c "
    source '$REPO_ROOT/lib/doctor.sh'; _asha_doctor_workspace_section" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *"thallus"* ]]; then pass; else fail "rc=$rc out=$out"; fi

echo ""
echo -e "Passed: ${GREEN}${PASSED}${NC}  Failed: ${RED}${FAILED}${NC}"
[[ $FAILED -eq 0 ]]
