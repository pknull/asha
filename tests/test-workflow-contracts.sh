#!/usr/bin/env bash
# Static contract tests for the compact code-orchestration and panel workflows.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

PASS=0
FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }

assert_contains() {
  local description="$1" needle="$2" file="$3"
  grep -Fq -- "$needle" "$file" && ok "$description" || fail "$description"
}

assert_absent() {
  local description="$1" needle="$2"; shift 2
  if grep -RFq -- "$needle" "$@" 2>/dev/null; then
    fail "$description"
  else
    ok "$description"
  fi
}

CODE_COMMAND="$REPO_ROOT/plugins/code/commands/orchestrate.md"
CODE_RECIPES="$REPO_ROOT/plugins/code/recipes"
PANEL_COMMAND="$REPO_ROOT/plugins/panel/commands/panel.md"
PANEL_CODIFIER="$REPO_ROOT/plugins/panel/agents/codifier.md"

echo "--- compact code orchestration ---"
for workflow in feature bugfix refactor security custom; do
  assert_contains "keeps $workflow workflow" "\`$workflow\`" "$CODE_COMMAND"
done
assert_contains "risk routing can require historian" 'codebase-historian' "$CODE_COMMAND"
assert_contains "risk routing can require reviewer" 'Append `reviewer`' "$CODE_COMMAND"
assert_absent "tier override removed" '--tier' "$CODE_COMMAND" "$REPO_ROOT/plugins/code/README.md"
assert_absent "claimed-status metrics removed" 'claimed_status' "$CODE_COMMAND"
assert_absent "harness model labels removed" 'Haiku' "$CODE_COMMAND" "$CODE_RECIPES"
assert_absent "harness model labels removed (Sonnet)" 'Sonnet' "$CODE_COMMAND" "$CODE_RECIPES"
assert_absent "harness model labels removed (Opus)" 'Opus' "$CODE_COMMAND" "$CODE_RECIPES"
assert_absent "recipes have no per-agent model fields" 'model:' "$CODE_RECIPES"
[[ ! -e "$REPO_ROOT/plugins/code/modules/complexity-routing.md" ]] && ok "uninstalled routing dependency removed" || fail "uninstalled routing dependency removed"
[[ ! -e "$REPO_ROOT/plugins/code/modules/code.md" ]] && ok "orphan code module removed" || fail "orphan code module removed"
[[ ! -e "$REPO_ROOT/plugins/code/modules/parallel-agents.md" ]] && ok "orphan parallel-agents module removed" || fail "orphan parallel-agents module removed"

echo "--- compact panel persistence ---"
for interface in '--quick' '--think' '--interview' '--list' '--show' '--resume' '--abandon'; do
  assert_contains "keeps panel $interface interface" "$interface" "$PANEL_COMMAND"
done
assert_contains "panel persists resumable state" 'state.json' "$PANEL_COMMAND"
assert_contains "panel writes final decision" 'decision.md' "$PANEL_COMMAND"
assert_contains "interview additionally writes seed" 'seed.yaml' "$PANEL_COMMAND"
assert_contains "panel can atomically replace state" '"Bash"' "$PANEL_COMMAND"
assert_contains "full mode loops on REVISE" 'On `REVISE`' "$PANEL_COMMAND"
assert_contains "legacy panels remain discoverable" 'Work/thinking/<id>/' "$PANEL_COMMAND"
assert_contains "legacy panel import preserves source" 'state.legacy.json' "$PANEL_COMMAND"
assert_contains "codifier embeds the installed schema" 'ontology_schema:' "$PANEL_CODIFIER"
[[ ! -e "$REPO_ROOT/plugins/panel/templates/seed.yaml" ]] && ok "uninstalled seed dependency removed" || fail "uninstalled seed dependency removed"
assert_absent "thought JSONL persistence removed" 'thoughts.jsonl' "$REPO_ROOT/plugins/panel"
assert_absent "full transcript artifact removed" 'transcript.md' "$REPO_ROOT/plugins/panel"
assert_absent "per-phase artifact names removed" 'phase-00-' "$REPO_ROOT/plugins/panel"
assert_absent "panel index artifact removed" 'index.json' "$REPO_ROOT/plugins/panel"
[[ ! -e "$REPO_ROOT/plugins/panel/docs/_template.md" ]] && ok "duplicated agent schema removed" || fail "duplicated agent schema removed"
[[ ! -e "$REPO_ROOT/plugins/panel/docs/characters/The Analyst.md" ]] && ok "duplicated Analyst biography removed" || fail "duplicated Analyst biography removed"
[[ ! -e "$REPO_ROOT/plugins/panel/docs/characters/The Thinker.md" ]] && ok "duplicated Thinker biography removed" || fail "duplicated Thinker biography removed"

echo "--- dispatcher retirement ---"
[[ ! -e "$REPO_ROOT/bin/calibration" ]] && ok "calibration reader removed" || fail "calibration reader removed"
help_output="$(bash "$REPO_ROOT/bin/asha" --help)"
if grep -Fq 'asha calibration' <<<"$help_output"; then
  fail "dispatcher help omits retired calibration command"
else
  ok "dispatcher help omits retired calibration command"
fi
assert_absent "dispatcher no longer scans output styles" 'output-styles' "$REPO_ROOT/bin/asha"

echo ""
echo "Passed: $PASS"
echo "Failed: $FAIL"
[[ $FAIL -eq 0 ]]
