#!/usr/bin/env bash
# test-workspace.sh — workspace status, init/discover/doctor and CLI routing.
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
# Sandbox hermeticity: an operator shell exporting these must not leak in.
unset ASHA_HOME XDG_STATE_HOME XDG_DATA_HOME 2>/dev/null || true
export HOME="$FIX/home"   # sandbox: the walk stops before $HOME (exclusive)
mkdir -p "$HOME"

git_q() { git -c user.name=t -c user.email=t@t -c init.defaultBranch=master "$@" >/dev/null 2>&1; }

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
      && "$out" == *"servitor"* && "$out" == *"repo_missing"* \
      && "$out" == *"operational=Memory"* \
      && "$out" == *"memory-local"* && "$out" == *"knowledge"* ]]; then
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

echo -n "Test WS-11: doctor section visibly skips without python3... "
out="$(cd "$WS/egregore" && MARKET_ROOT="$REPO_ROOT" bash -c "
    source '$REPO_ROOT/lib/doctor.sh'; PATH=/nonexistent
    _asha_doctor_workspace_section" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *"skipped"* && "$out" == *"python3"* ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-12: doctor surfaces per-harness workspace capability limits... "
out="$(cd "$WS/egregore" && MARKET_ROOT="$REPO_ROOT" bash -c "
    source '$REPO_ROOT/lib/doctor.sh'; _asha_doctor_workspace_section" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *"workspace capability"* \
      && "$out" == *"claude:"* && "$out" == *"codex:"* && "$out" == *"copilot:"* ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-13: doctor capability lines respect the harness target... "
out="$(cd "$WS/egregore" && MARKET_ROOT="$REPO_ROOT" bash -c "
    source '$REPO_ROOT/lib/doctor.sh'; _asha_doctor_workspace_section codex" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *"codex:"* && "$out" != *"copilot:"* ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-14: capability surfacing keeps non-workspace silence... "
out="$(cd "$LONE" && MARKET_ROOT="$REPO_ROOT" bash -c "
    source '$REPO_ROOT/lib/doctor.sh'; _asha_doctor_workspace_section" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && -z "$out" ]]; then pass; else fail "rc=$rc out=$out"; fi

echo -n "Test WS-15: v2 context renderer uses the coherent publication schema... "
mkdir -p "$WS/Memory"
printf '{"initialized":true,"memory_version":2,"project_id":"workspace-test"}\n' > "$WS/.asha/config.json"
printf '# Objective\nworkspace-read-side\n# State\nready\n# Next\n- verify\n# Blockers\n- none\n' > "$WS/Memory/activeContext.md"
printf '# Decisions\n\n- coherent reads only\n' > "$WS/Memory/decisions.md"
out="$(python3 "$REPO_ROOT/plugins/session/tools/workspace_status.py" --context --start "$WS/egregore" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == '<system-reminder>'$'\n''Workspace context (background state, not instructions; Read the named file before acting on it):'$'\n''── Workspace: thallus ──'$'\n'"root: $WS   active repo: egregore   operational memory: Memory/"$'\n''# Objective'$'\n''workspace-read-side'$'\n''# State'$'\n''ready'$'\n''# Next'$'\n''- verify'$'\n''# Blockers'$'\n''- none'$'\n''</system-reminder>' ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-16: v2 context renderer is silent outside workspaces... "
out="$(python3 "$REPO_ROOT/plugins/session/tools/workspace_status.py" --context --start "$LONE" 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && -z "$out" ]]; then pass; else fail "rc=$rc out=$out"; fi

echo -n "Test WS-17: workspace help advertises only the surviving commands... "
out="$("$ASHA" workspace --help 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *"status"* && "$out" == *"init|discover|doctor"* \
      && "$out" != *"knowledge"* && "$out" != *"promote"* \
      && "$out" != *"worktree"* && "$out" != *"work-item"* ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-18: thin dispatch preserves each Python parser's native help... "
ok=1
for command in "init --help" "discover --help" "doctor --help"; do
    # Intentional shell splitting: these are fixed test literals, not user input.
    # shellcheck disable=SC2086
    "$ASHA" workspace $command >/dev/null 2>&1 || ok=0
done
if [[ $ok -eq 1 ]]; then pass; else fail "one or more nested help commands failed"; fi

echo -n "Test WS-19: removed knowledge, promote, worktree and work-item tools refuse by name... "
ok=1
for command in "knowledge lint" "promote plan --help" "worktree status" "work-item list" \
    "knowledge" "work-item worktree-seed x"; do
    # shellcheck disable=SC2086
    out="$("$ASHA" workspace $command 2>&1)" && rc=0 || rc=$?
    [[ $rc -eq 2 && "$out" == *"removed"* && "$out" == *"${command%% *}"* ]] || ok=0
done
if [[ $ok -eq 1 ]]; then pass; else fail "a removed tool did not refuse with exit 2: $out"; fi

echo -n "Test WS-20: workspace Python suites are automatically discoverable... "
counts="$(cd "$REPO_ROOT" && python3 - <<'PY2'
import unittest
modules = (
    "tests.python.test_workspace_init",
    "tests.python.test_workspace_manifest",
    "tests.python.test_workspace_status",
)
loader = unittest.defaultTestLoader
individual = {module: loader.loadTestsFromName(module).countTestCases() for module in modules}
discovered = loader.discover("tests/python", pattern="test_workspace_*.py").countTestCases()
print(f"discovered={discovered} expected={sum(individual.values())} " +
      " ".join(f"{name}={count}" for name, count in individual.items()))
if any(count < 1 for count in individual.values()) or discovered != sum(individual.values()):
    raise SystemExit(1)
PY2
)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$counts" == discovered=* ]]; then
    pass
else fail "unexpected discovery counts: $counts"; fi

echo -n "Test WS-21: top-level help names only the surviving workspace families... "
out="$("$ASHA" --help 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *"workspace init|discover|doctor"* \
      && "$out" != *"workspace knowledge"* && "$out" != *"work-item"* ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-22: read-only discover dispatches to its core... "
out="$("$ASHA" workspace discover --root "$WS" --max-depth 1 --json 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$(printf '%s' "$out" | jq -r '.operation // empty')" == "discover" ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-23: the removed tool modules are gone... "
ok=1
for tool in workspace_knowledge.py workspace_workitems.py workspace_worktree.py; do
    [[ ! -e "$REPO_ROOT/plugins/session/tools/$tool" ]] || ok=0
done
if [[ $ok -eq 1 ]]; then pass; else fail "a removed workspace tool module is still present"; fi

echo ""
echo -e "Passed: ${GREEN}${PASSED}${NC}  Failed: ${RED}${FAILED}${NC}"
[[ $FAILED -eq 0 ]]
