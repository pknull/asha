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
PYTHON_USER_SITE="$(python3 -c 'import site; print(site.getusersitepackages())')"

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
  local fake_opencode="$SANDBOX/fake-opencode"
  cat >"$fake_opencode" <<'EOF'
#!/usr/bin/env bash
echo 1.17.18
EOF
  chmod +x "$fake_opencode"
  env -u XDG_CONFIG_HOME -u XDG_DATA_HOME HOME="$SANDBOX" \
    ASHA_OPENCODE_CMD="$fake_opencode" \
    PYTHONPATH="$PYTHON_USER_SITE${PYTHONPATH:+:$PYTHONPATH}" \
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
[[ -f "$SANDBOX/.config/opencode/plugins/asha.js" ]] \
  && ok "OpenCode native integration plugin exists" \
  || fail "OpenCode native integration plugin exists"
[[ -f "$SANDBOX/.config/opencode/commands/session-save.md" ]] \
  && ok "OpenCode rendered commands are non-empty" \
  || fail "OpenCode rendered commands are non-empty"
for save_workflow in \
  "$SANDBOX/.claude/commands/session/save.md" \
  "$SANDBOX/.codex/skills/session-save/SKILL.md" \
  "$SANDBOX/.copilot/skills/session-save/SKILL.md" \
  "$SANDBOX/.config/opencode/commands/session-save.md"; do
	  if [[ -f "$save_workflow" || -L "$save_workflow" ]] \
	     && grep -q 'save_identity.py' "$save_workflow" \
	     && grep -q 'SAVE_SESSION_ID' "$save_workflow" \
	     && awk '/memory_v2.py" publish/{p=NR} /save_identity.py/{i=NR} END{exit !(p && i && p < i)}' "$save_workflow" \
	     && ! grep -q -- '--capability\|learning_capability' "$save_workflow"; then
	    ok "rendered explicit save publishes before optional identity without capability: $save_workflow"
  else
    fail "rendered explicit save identity workflow missing or stale: $save_workflow"
  fi
done
jq -e --arg root "$REPO_ROOT" '.asha_root == $root' "$SANDBOX/.asha/config.json" >/dev/null \
  && ok "identity config records asha_root" \
  || fail "identity config records asha_root"
if [[ -f "$SANDBOX/.asha/soul.md" && -f "$SANDBOX/.asha/voice.md" \
      && -f "$SANDBOX/.asha/keeper.md" ]]; then
  ok "installer bootstraps the compact identity triplet"
else
  fail "installer bootstraps the compact identity triplet"
fi
if jq -e '(.version == "2.0") and (has("capture_calibration") | not)
    and (has("identity_file") | not)' "$SANDBOX/.asha/config.json" >/dev/null; then
  ok "new user config omits retired calibration and communicationStyle keys"
else
  fail "new user config omits retired calibration and communicationStyle keys"
fi
for skill_path in \
  "$SANDBOX/.claude/skills/asha-asha-reference/SKILL.md" \
  "$SANDBOX/.codex/skills/asha-reference/SKILL.md" \
  "$SANDBOX/.copilot/skills/asha-reference/SKILL.md" \
  "$SANDBOX/.config/opencode/skills/asha-reference/SKILL.md"; do
  [[ -f "$skill_path" || -L "$skill_path" ]] \
    && ok "cold identity reference skill installed: $skill_path" \
    || fail "cold identity reference skill installed: $skill_path"
done
if jq -e '
    (.hooks.sessionStart[0].bash | endswith("session-start.sh")) and
    (.hooks.userPromptSubmitted[0].bash | endswith("user-prompt-submit.sh")) and
    (.hooks.postToolUse[0].bash | endswith("post-tool-use.sh")) and
    (.hooks.sessionEnd[0].bash | endswith("session-end.sh"))
  ' "$SANDBOX/.copilot/hooks/asha-recovery.json" >/dev/null 2>&1; then
  ok "Copilot installs Memory v2 recovery callbacks"
else
  fail "Copilot installs Memory v2 recovery callbacks"
fi
[[ ! -e "$SANDBOX/.copilot/hooks/asha-nudges.json" && ! -e "$SANDBOX/.copilot/hooks/asha-lifecycle.json" ]] \
  && ok "Copilot legacy nudge/lifecycle files are pruned" \
  || fail "Copilot legacy nudge/lifecycle files are pruned"

# Reconciliation must run even when the current recovery artifact is already
# byte-identical. Retired generated files are removed only when their bytes are
# recognized; a modified legacy file remains for review.
RECOVERY_BEFORE="$(sha256sum "$SANDBOX/.copilot/hooks/asha-recovery.json")"
jq -nc --arg e "$REPO_ROOT/plugins/session/hooks/handlers/nudge-engine.sh" '{
  version:1, hooks:{
    sessionStart:[{type:"command",bash:($e + " SessionStart"),timeoutSec:10}],
    userPromptSubmitted:[{type:"command",bash:($e + " UserPromptSubmit"),timeoutSec:10}],
    postToolUse:[{type:"command",bash:($e + " PostToolUse"),timeoutSec:10}]
  }
}' > "$SANDBOX/.copilot/hooks/asha-nudges.json"
jq -nc --arg s "$REPO_ROOT/plugins/session/hooks/handlers/session-start.sh" \
       --arg e "$REPO_ROOT/plugins/session/hooks/handlers/session-end.sh" '{
  version:1, hooks:{
    sessionStart:[{type:"command",bash:$s,timeoutSec:60}],
    sessionEnd:[{type:"command",bash:$e,timeoutSec:30}]
  }
}' > "$SANDBOX/.copilot/hooks/asha-lifecycle.json"
if noop_reconcile="$(run_install --target copilot 2>&1)" \
   && [[ ! -e "$SANDBOX/.copilot/hooks/asha-nudges.json" \
      && ! -e "$SANDBOX/.copilot/hooks/asha-lifecycle.json" \
      && "$RECOVERY_BEFORE" == "$(sha256sum "$SANDBOX/.copilot/hooks/asha-recovery.json")" ]]; then
  ok "Copilot no-op reinstall still reconciles byte-matching retired hooks"
else
  fail "Copilot no-op reinstall still reconciles byte-matching retired hooks"
fi

printf '{"user_modified":true}\n' > "$SANDBOX/.copilot/hooks/asha-nudges.json"
run_install --target copilot >/dev/null 2>&1 || true
jq -e '.user_modified == true' "$SANDBOX/.copilot/hooks/asha-nudges.json" >/dev/null 2>&1 \
  && ok "Copilot reconciliation preserves modified retired hooks" \
  || fail "Copilot reconciliation preserves modified retired hooks"
rm -f "$SANDBOX/.copilot/hooks/asha-nudges.json"

printf '{"user_modified":true}\n' > "$SANDBOX/.copilot/hooks/asha-recovery.json"
modified_recovery_out="$(run_install --target copilot 2>&1 || printf '__EXPECTED_FAILURE__')"
if [[ "$modified_recovery_out" != *'__EXPECTED_FAILURE__'* ]]; then
  fail "Copilot install refuses to overwrite a modified managed recovery hook"
else
  jq -e '.user_modified == true' "$SANDBOX/.copilot/hooks/asha-recovery.json" >/dev/null 2>&1 \
    && ok "Copilot install refuses to overwrite a modified managed recovery hook" \
    || fail "Copilot install refuses to overwrite a modified managed recovery hook"
fi
if run_install --target copilot --force >/dev/null 2>&1; then
  ok "Copilot --force restores modified managed hook fixture"
else
  fail "Copilot --force restores modified managed hook fixture"
fi

# ---------------------------------------------------------------------------
# Test 2: Claude hook ownership spans the five v2 lifecycle events
# ---------------------------------------------------------------------------
echo "--- test 2: hook registration covers lifecycle events ---"
hook_count="$(asha_hook_count)"
event_count="$(asha_hook_event_count)"
[[ "$hook_count" -ge 7 ]] \
  && ok "at least 7 asha-tagged hook entries registered ($hook_count)" \
  || fail "at least 7 asha-tagged hook entries registered (got $hook_count)"
assert_eq "asha hooks span all five v2 events" "5" "$event_count"

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
[[ -f "$SANDBOX/.config/opencode/plugins/asha.js" ]] \
  && ok "OpenCode install survives Codex failure" \
  || fail "OpenCode install survives Codex failure"
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
  # Simulate a link left by a retired Session agent. Full reconciliation owns
  # broken links into this Asha source tree and must remove it.
  mkdir -p "$SANDBOX/.claude/agents/session"
  ln -s "$REPO_ROOT/plugins/session/agents/memory-curator.md" \
    "$SANDBOX/.claude/agents/session/memory-curator.md"
  if run_install --target all >/dev/null 2>&1; then
    ok "second install exits 0"
  else
    fail "second install exits 0"
  fi
  assert_eq "second install keeps the same hook count" "$hooks_first" "$(asha_hook_count)"
  [[ ! -L "$SANDBOX/.claude/agents/session/memory-curator.md" ]] \
    && ok "full install prunes retired Claude agent links" \
    || fail "full install prunes retired Claude agent links"
  # Real installations may keep the primitive root itself in a dotfiles
  # checkout. Reconciliation must follow that one declared root without
  # following arbitrary symlinks elsewhere under ~/.claude.
  rm -rf "$SANDBOX/.claude/agents"
  mkdir -p "$SANDBOX/dotfiles/claude-agents/session"
  ln -s "$SANDBOX/dotfiles/claude-agents" "$SANDBOX/.claude/agents"
  ln -s "$REPO_ROOT/plugins/session/agents/memory-steward.md" \
    "$SANDBOX/dotfiles/claude-agents/session/memory-steward.md"
  if run_install --target claude >/dev/null 2>&1 \
    && [[ ! -L "$SANDBOX/dotfiles/claude-agents/session/memory-steward.md" ]]; then
    ok "full install prunes retired links below a symlinked Claude primitive root"
  else
    fail "full install prunes retired links below a symlinked Claude primitive root"
  fi
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

# ---------------------------------------------------------------------------
# Test 6: legacy learning stores point to the reviewed v2 migration path
# ---------------------------------------------------------------------------
echo "--- test 6: legacy learning migration guidance is current ---"
mkdir -p "$SANDBOX/.asha/learnings" "$SANDBOX/.asha/learnings-archive"
printf '%s\n' '---' 'id: root-concept' '---' > "$SANDBOX/.asha/learnings/root-concept.md"
printf '%s\n' '---' 'id: old-concept' '---' > "$SANDBOX/.asha/learnings-archive/old-concept.md"
printf '# Legacy flat learning\n' > "$SANDBOX/.asha/learnings.md"
legacy_out="$(run_install --target copilot 2>&1)"
if [[ "$legacy_out" == *"/session:consolidate"* \
   && "$legacy_out" != *"migrate_learnings_to_okf.py"* ]]; then
  ok "installer inventories legacy learning stores through reviewed consolidation"
else
  fail "installer inventories legacy learning stores through reviewed consolidation"
fi
cat > "$SANDBOX/.asha/learnings/.migration-v2.json" <<'JSON'
{"version":2,"status":"reviewed-migration-complete","review_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
JSON
migrated_out="$(run_install --target copilot 2>&1)"
[[ "$migrated_out" != *"legacy learning records detected"* \
   && "$migrated_out" != *"/session:consolidate"* ]] \
  && ok "reviewed migration marker silences preserved legacy-source guidance" \
  || fail "reviewed migration marker silences preserved legacy-source guidance"
printf '{malformed}\n' > "$SANDBOX/.asha/learnings/.migration-v2.json"
malformed_marker_out="$(run_install --target copilot 2>&1)"
[[ "$malformed_marker_out" == *"/session:consolidate"* ]] \
  && ok "malformed migration marker cannot suppress legacy guidance" \
  || fail "malformed migration marker cannot suppress legacy guidance"

echo ""
echo "=== Install Test Summary ==="
echo -e "Passed: ${GREEN}$PASS${NC}"
echo -e "Failed: ${RED}$FAIL${NC}"

[[ $FAIL -eq 0 ]]
