#!/usr/bin/env bash
# test-workspace.sh — workspace status/doctor plus v3-v6 CLI integration.
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

echo -n "Test WS-17: workspace help advertises every integrated surface... "
out="$("$ASHA" workspace --help 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *"init|discover|doctor"* \
      && "$out" == *"knowledge init|lint"* \
      && "$out" == *"promote plan|apply|publish"* \
      && "$out" == *"worktree create|status|remove"* \
      && "$out" == *"work-item create|list|show|link|import|preview|lint|index|promote-plan|worktree-seed"* ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-18: thin dispatch preserves each Python parser's native help... "
ok=1
for command in \
  "init --help" "discover --help" "doctor --help" \
  "knowledge init --help" "knowledge lint --help" \
  "promote plan --help" "promote apply --help" "promote publish --help" \
  "worktree create --help" "worktree status --help" "worktree remove --help" \
  "work-item create --help" "work-item list --help" "work-item show --help" \
  "work-item link --help" "work-item import --help" "work-item preview --help" \
  "work-item lint --help" "work-item index --help" \
  "work-item promote-plan --help" "work-item worktree-seed --help"; do
    # Intentional shell splitting: these are fixed test literals, not user input.
    # shellcheck disable=SC2086
    "$ASHA" workspace $command >/dev/null 2>&1 || ok=0
done
if [[ $ok -eq 1 ]]; then pass; else fail "one or more nested help commands failed"; fi

echo -n "Test WS-19: nested unknown commands remain usage errors... "
ok=1
for command in "knowledge bogus" "promote bogus" "worktree bogus" "work-item bogus"; do
    # shellcheck disable=SC2086
    "$ASHA" workspace $command >/dev/null 2>&1 && rc=0 || rc=$?
    [[ $rc -eq 2 ]] || ok=0
done
if [[ $ok -eq 1 ]]; then pass; else fail "a nested unknown command did not return 2"; fi

echo -n "Test WS-20: workspace Python suites are automatically discoverable... "
counts="$(cd "$REPO_ROOT" && python3 - <<'PY'
import unittest
modules = (
    "tests.python.test_workspace_init",
    "tests.python.test_workspace_knowledge",
    "tests.python.test_workspace_manifest",
    "tests.python.test_workspace_status",
    "tests.python.test_workspace_worktree",
    "tests.python.test_workspace_workitems",
)
loader = unittest.defaultTestLoader
individual = {module: loader.loadTestsFromName(module).countTestCases() for module in modules}
discovered = loader.discover("tests/python", pattern="test_workspace_*.py").countTestCases()
print(f"discovered={discovered} expected={sum(individual.values())} " +
      " ".join(f"{name}={count}" for name, count in individual.items()))
if any(count < 1 for count in individual.values()) or discovered != sum(individual.values()):
    raise SystemExit(1)
PY
)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$counts" == discovered=* ]]; then
    pass
else fail "unexpected discovery counts: $counts"; fi

echo -n "Test WS-21: top-level help names the integrated workspace families... "
out="$("$ASHA" --help 2>&1)" && rc=0 || rc=$?
if [[ $rc -eq 0 && "$out" == *"workspace init|discover|doctor"* \
      && "$out" == *"workspace knowledge|promote|worktree|work-item"* ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-22: read-only leaf commands dispatch to their actual cores... "
ok=1
out="$("$ASHA" workspace discover --root "$WS" --max-depth 1 --json 2>&1)" && rc=0 || rc=$?
[[ $rc -eq 0 && "$(printf '%s' "$out" | jq -r '.operation // empty')" == "discover" ]] || ok=0
out="$("$ASHA" workspace knowledge lint --start "$WS" --json 2>&1)" && rc=0 || rc=$?
[[ "$(printf '%s' "$out" | jq -r '.operation // empty')" == "lint" ]] || ok=0
out="$("$ASHA" workspace work-item list --start "$WS" --json 2>&1)" && rc=0 || rc=$?
[[ "$(printf '%s' "$out" | jq -r '.operation // empty')" == "list" ]] || ok=0
out="$("$ASHA" workspace worktree status --workspace-root "$WS" --json 2>&1)" && rc=0 || rc=$?
[[ "$(printf '%s' "$out" | jq -r '.contract // empty')" == "asha.workspace-worktree-status.v1" ]] || ok=0
if [[ $ok -eq 1 ]]; then pass; else fail "one or more leaf commands missed its core"; fi

echo -n "Test WS-23: work-item worktree-seed maps to a confirmed data-only plan... "
"$ASHA" workspace work-item create seed-item --title "Seed item" --repo egregore \
  --start "$WS" --json >/dev/null 2>&1 && rc=0 || rc=$?
if [[ $rc -eq 0 ]]; then
  out="$("$ASHA" workspace work-item worktree-seed seed-item \
      --target cross-cutting/seed-item.md --evidence "$WS/egregore/f" \
      --start "$WS" --confirm-git --json 2>&1)" && rc=0 || rc=$?
else
  out="create failed"
fi
if [[ $rc -eq 0 \
      && "$(printf '%s' "$out" | jq -r '.worktree_seed.data_only // false')" == "true" \
      && "$(printf '%s' "$out" | jq -r '.canonical_write_performed == false')" == "true" ]]; then
    pass
else fail "rc=$rc out=$out"; fi

echo -n "Test WS-24: promotion help separates planning from apply/publish confirmation... "
plan_help="$("$ASHA" workspace promote plan --help 2>&1)" && plan_rc=0 || plan_rc=$?
apply_help="$("$ASHA" workspace promote apply --help 2>&1)" && apply_rc=0 || apply_rc=$?
publish_help="$("$ASHA" workspace promote publish --help 2>&1)" && publish_rc=0 || publish_rc=$?
if [[ $plan_rc -eq 0 && $apply_rc -eq 0 && $publish_rc -eq 0 \
      && "$plan_help" == *"--plan-out"* && "$plan_help" == *"--source"* \
      && "$apply_help" == *"--plan"* && "$apply_help" == *"--digest"* \
      && "$apply_help" == *"--confirm"* && "$apply_help" != *"--source"* \
      && "$apply_help" != *"--target"* \
      && "$publish_help" == *"--plan"* && "$publish_help" == *"--digest"* \
      && "$publish_help" == *"--confirm"* && "$publish_help" == *"--run-git-hooks"* \
      && "$publish_help" != *"--source"* ]]; then
    pass
else fail "plan=$plan_help apply=$apply_help publish=$publish_help"; fi

echo ""
echo -e "Passed: ${GREEN}${PASSED}${NC}  Failed: ${RED}${FAILED}${NC}"
[[ $FAILED -eq 0 ]]
