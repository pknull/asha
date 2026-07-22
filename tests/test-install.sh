#!/usr/bin/env bash
# test-install.sh — sandboxed install round-trip and failure-isolation tests.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

PASS=0
FAIL=0

ok()   { echo -e "  ${GREEN}✓${NC} $1"; PASS=$((PASS + 1)); }
fail() { echo -e "  ${RED}✗${NC} $1" >&2; FAIL=$((FAIL + 1)); }

assert_eq() { # desc expected actual
  if [[ "$2" == "$3" ]]; then ok "$1"; else fail "$1 (expected: $2, got: $3)"; fi
}

command -v jq >/dev/null 2>&1 || { echo "SKIP: jq not available" >&2; exit 0; }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

reset_sandbox() {
  rm -rf "$SANDBOX"
  mkdir -p "$SANDBOX"
}

seed_native_configs() {
  mkdir -p "$SANDBOX/.claude" "$SANDBOX/.codex"
  printf '{}\n' > "$SANDBOX/.claude/settings.json"
  printf '# sandbox codex config\n' > "$SANDBOX/.codex/config.toml"
}

run_install() {
  env -u XDG_CONFIG_HOME -u XDG_DATA_HOME HOME="$SANDBOX" \
    bash "$REPO_ROOT/install.sh" "$@"
}

asha_hook_count() {
  jq -r '[.hooks // {} | .[] | .[]? | .hooks[]?
    | select((.source // "") | startswith("asha:"))] | length' \
    "$SANDBOX/.claude/settings.json"
}

asha_hook_event_count() {
  jq -r '[.hooks // {} | to_entries[]
    | select([.value[]? | .hooks[]?
      | select((.source // "") | startswith("asha:"))] | length > 0)] | length' \
    "$SANDBOX/.claude/settings.json"
}

# ---------------------------------------------------------------------------
# Test 1: all harnesses install into an isolated HOME
# ---------------------------------------------------------------------------
echo "--- test 1: full install mounts every harness ---"
reset_sandbox
seed_native_configs
if full_out="$(run_install --target all 2>&1)"; then
  ok "install --target all exits 0"
else
  fail "install --target all exits 0 (got $?; output: $(tail -5 <<<"$full_out"))"
fi

[[ -n "$(find "$SANDBOX/.claude/skills" -mindepth 1 -maxdepth 1 -type l -print -quit 2>/dev/null)" ]] \
  && ok "Claude skills include a symlink mount" \
  || fail "Claude skills include a symlink mount"
[[ -n "$(find "$SANDBOX/.claude/commands/session" -mindepth 1 -maxdepth 1 -type l -print -quit 2>/dev/null)" ]] \
  && ok "Claude session commands include a symlink mount" \
  || fail "Claude session commands include a symlink mount"
[[ -n "$(find "$SANDBOX/.codex/agents" -mindepth 1 -maxdepth 1 -type f -print -quit 2>/dev/null)" ]] \
  && ok "Codex generated agents are non-empty" \
  || fail "Codex generated agents are non-empty"
[[ -f "$SANDBOX/.codex/rules/asha.rules" ]] \
  && ok "Codex native rules file exists" \
  || fail "Codex native rules file exists"
[[ -n "$(find "$SANDBOX/.config/opencode/skills" -mindepth 1 -maxdepth 1 -type l -print -quit 2>/dev/null)" ]] \
  && ok "OpenCode skills include a symlink mount" \
  || fail "OpenCode skills include a symlink mount"
jq -e --arg root "$REPO_ROOT" '.asha_root == $root' "$SANDBOX/.asha/config.json" >/dev/null \
  && ok "identity config records asha_root" \
  || fail "identity config records asha_root"

# ---------------------------------------------------------------------------
# Test 2: Claude hook ownership spans the six lifecycle events
# ---------------------------------------------------------------------------
echo "--- test 2: hook registration covers lifecycle events ---"
hook_count="$(asha_hook_count)"
event_count="$(asha_hook_event_count)"
[[ "$hook_count" -ge 10 ]] \
  && ok "at least 10 asha-tagged hook entries registered ($hook_count)" \
  || fail "at least 10 asha-tagged hook entries registered (got $hook_count)"
assert_eq "asha hooks span all six events" "6" "$event_count"

# ---------------------------------------------------------------------------
# Test 3: a Codex failure does not abort Claude, Copilot, or OpenCode
# ---------------------------------------------------------------------------
echo "--- test 3: per-harness failure isolation ---"
reset_sandbox
mkdir -p "$SANDBOX/.claude"
printf '{}\n' > "$SANDBOX/.claude/settings.json"
if isolation_out="$(run_install --target all 2>&1)"; then
  fail "install reports a non-zero status when Codex is uninitialized"
else
  ok "install reports a non-zero status when Codex is uninitialized"
fi
[[ -n "$(find "$SANDBOX/.claude/skills" -mindepth 1 -maxdepth 1 -type l -print -quit 2>/dev/null)" ]] \
  && ok "Claude mounts survive Codex failure" \
  || fail "Claude mounts survive Codex failure"
[[ -n "$(find "$SANDBOX/.config/opencode/skills" -mindepth 1 -maxdepth 1 -type l -print -quit 2>/dev/null)" ]] \
  && ok "OpenCode runs after Codex failure" \
  || fail "OpenCode runs after Codex failure"
grep -q '^install summary:$' <<<"$isolation_out" && grep -q '^  codex: FAILED$' <<<"$isolation_out" \
  && ok "per-harness summary names the Codex failure" \
  || fail "per-harness summary names the Codex failure"

# ---------------------------------------------------------------------------
# Test 4: --only limits mounts without disturbing the globally owned hooks
# ---------------------------------------------------------------------------
echo "--- test 4: --only admin scopes mounts and preserves hooks ---"
reset_sandbox
seed_native_configs
if ! run_install --target claude >/dev/null 2>&1; then
  fail "scoping fixture full Claude install exits 0"
else
  rm -rf "$SANDBOX/.claude/skills" "$SANDBOX/.claude/agents" \
         "$SANDBOX/.claude/commands" "$SANDBOX/.claude/output-styles"
  mkdir -p "$SANDBOX/.claude/skills" "$SANDBOX/.claude/agents" \
           "$SANDBOX/.claude/commands" "$SANDBOX/.claude/output-styles"
  hooks_before="$(asha_hook_count)"
  if run_install --target claude --only admin >/dev/null 2>&1; then
    ok "scoped Claude install exits 0"
  else
    fail "scoped Claude install exits 0"
  fi

  admin_links=0
  non_admin_links=0
  while IFS= read -r -d '' link; do
    target="$(readlink "$link")"
    case "$target" in
      "$REPO_ROOT/plugins/admin/"*) admin_links=$((admin_links + 1)) ;;
      *) non_admin_links=$((non_admin_links + 1)) ;;
    esac
  done < <(find "$SANDBOX/.claude" -type l -print0)
  [[ $admin_links -gt 0 && $non_admin_links -eq 0 ]] \
    && ok "only admin plugin skills are mounted" \
    || fail "only admin plugin skills are mounted (admin=$admin_links, other=$non_admin_links)"
  assert_eq "scoped install leaves hook count unchanged" "$hooks_before" "$(asha_hook_count)"
fi

# ---------------------------------------------------------------------------
# Test 5: repeat installation is clean and does not duplicate hook groups
# ---------------------------------------------------------------------------
echo "--- test 5: repeated install is idempotent ---"
reset_sandbox
seed_native_configs
if ! run_install --target all >/dev/null 2>&1; then
  fail "first idempotency install exits 0"
else
  hooks_first="$(asha_hook_count)"
  if run_install --target all >/dev/null 2>&1; then
    ok "second install exits 0"
  else
    fail "second install exits 0"
  fi
  assert_eq "second install keeps the same hook count" "$hooks_first" "$(asha_hook_count)"
  if jq -e '
      [.hooks // {} | to_entries[] as $event | $event.value[]?
       | select([.hooks[]? | select((.source // "") | startswith("asha:"))] | length > 0)
       | {event: $event.key, matcher: (.matcher // null), hooks: .hooks}] as $groups
      | ($groups | length) == ($groups | unique | length)
    ' "$SANDBOX/.claude/settings.json" >/dev/null; then
    ok "no duplicate asha hook groups"
  else
    fail "no duplicate asha hook groups"
  fi
fi

echo ""
echo "=== Install Test Summary ==="
echo -e "Passed: ${GREEN}$PASS${NC}"
echo -e "Failed: ${RED}$FAIL${NC}"

[[ $FAIL -eq 0 ]]
