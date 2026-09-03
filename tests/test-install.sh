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

seed_imported_lock() {
  local name="$1" skill_file file_sha file_bytes tree_sha
  skill_file="$SANDBOX/.asha/skills/$name/SKILL.md"
  file_sha="$(sha256sum "$skill_file" | awk '{print $1}')"
  file_bytes="$(wc -c < "$skill_file")"
  tree_sha="$(printf 'SKILL.md\0%s\n' "$file_sha" | sha256sum | awk '{print $1}')"
  jq -n \
    --arg name "$name" \
    --arg file_sha "$file_sha" \
    --argjson file_bytes "$file_bytes" \
    --arg tree_sha "$tree_sha" \
    '{
      schema_version: 1,
      skills: {
        ($name): {
          source: "fixture/repo",
          skill_id: $name,
          revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          upstream_path: ("skills/" + $name),
          files: {
            "SKILL.md": {
              sha256: $file_sha,
              bytes: $file_bytes,
              executable: false,
              upstream_mode: "100644"
            }
          },
          tree_digest: $tree_sha,
          license: {},
          state: "clean"
        }
      },
      history: {}
    }' > "$SANDBOX/.asha/skills/imported.lock.json"
}

frontmatter_name_is() {
  python3 - "$1" "$2" <<'PY'
import sys, yaml
with open(sys.argv[1], encoding="utf-8") as handle:
    frontmatter = handle.read().split("---", 2)[1]
raise SystemExit(0 if yaml.safe_load(frontmatter).get("name") == sys.argv[2] else 1)
PY
}

seed_imported_skill() {
  mkdir -p "$SANDBOX/.asha/skills/demo" "$SANDBOX/.asha/skills/untracked"
  cat > "$SANDBOX/.asha/skills/demo/SKILL.md" <<'EOF'
---
name: demo
description: Imported installer integration fixture.
---
# Demo
EOF
  cat > "$SANDBOX/.asha/skills/untracked/SKILL.md" <<'EOF'
---
name: untracked
description: Must never mount without provenance.
---
# Untracked
EOF
  seed_imported_lock demo
}

seed_imported_skill_with_name_key() {
  local key="$1" indent="${2:-}"
  mkdir -p "$SANDBOX/.asha/skills/demo"
  printf '%s\n' \
    '---' \
    "${indent}${key}: demo" \
    "${indent}description: Quoted YAML name fixture." \
    '---' \
    '# Demo' \
    > "$SANDBOX/.asha/skills/demo/SKILL.md"
  seed_imported_lock demo
}

seed_imported_skill_with_aliases() {
  mkdir -p "$SANDBOX/.asha/skills/demo"
  cat > "$SANDBOX/.asha/skills/demo/SKILL.md" <<'EOF'
---
name: &skill_name demo
description: *skill_name
metadata: {name: nested-value}
---
# Demo
EOF
  seed_imported_lock demo
}

seed_imported_skill_with_merge() {
  mkdir -p "$SANDBOX/.asha/skills/demo"
  cat > "$SANDBOX/.asha/skills/demo/SKILL.md" <<'EOF'
---
<<: &identity
  name: demo
  description: Merged imported fixture.
---
# Demo
EOF
  seed_imported_lock demo
}

seed_imported_skill_with_flow_merge() {
  mkdir -p "$SANDBOX/.asha/skills/demo"
  cat > "$SANDBOX/.asha/skills/demo/SKILL.md" <<'EOF'
---
{base: &identity {name: demo, description: Flow merged imported fixture.}, <<: *identity}
---
# Demo
EOF
  seed_imported_lock demo
}

seed_imported_skill_with_indented_merge() {
  mkdir -p "$SANDBOX/.asha/skills/demo"
  cat > "$SANDBOX/.asha/skills/demo/SKILL.md" <<'EOF'
---
  base: &identity
    name: demo
    description: Indented merged imported fixture.
  <<: *identity
---
# Demo
EOF
  seed_imported_lock demo
}

seed_imported_skill_with_length() {
  local length="$1"
  printf -v SEEDED_NAME '%*s' "$length" ''
  SEEDED_NAME="${SEEDED_NAME// /a}"
  mkdir -p "$SANDBOX/.asha/skills/$SEEDED_NAME"
  printf '%s\n' \
    '---' \
    "name: $SEEDED_NAME" \
    'description: Imported name-length fixture.' \
    '---' \
    '# Length fixture' \
    > "$SANDBOX/.asha/skills/$SEEDED_NAME/SKILL.md"
  seed_imported_lock "$SEEDED_NAME"
}

seed_legacy_imported_mounts() {
  local path
  for path in \
    "$SANDBOX/.claude/skills/imported-demo" \
    "$SANDBOX/.codex/skills/imported-demo" \
    "$SANDBOX/.copilot/skills/imported-demo" \
    "$SANDBOX/.config/opencode/skills/imported-demo"; do
    mkdir -p "$(dirname "$path")"
    ln -s "$SANDBOX/.asha/skills/demo" "$path"
  done
}

run_install() {
  local fake_opencode="$SANDBOX/fake-opencode" pythonpath
  cat >"$fake_opencode" <<'EOF'
#!/usr/bin/env bash
echo 1.17.18
EOF
  chmod +x "$fake_opencode"
  pythonpath="${ASHA_TEST_PYTHONPATH:-$PYTHON_USER_SITE${PYTHONPATH:+:$PYTHONPATH}}"
  env -u XDG_CONFIG_HOME -u XDG_DATA_HOME -u XDG_STATE_HOME -u ASHA_HOME HOME="$SANDBOX" \
    ASHA_OPENCODE_CMD="$fake_opencode" \
    PYTHONPATH="$pythonpath" \
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
seed_imported_skill
seed_legacy_imported_mounts
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
[[ ! -e "$SANDBOX/.claude/output-styles" ]] \
  && ok "Claude install does not create the retired output-styles mount" \
  || fail "Claude install does not create the retired output-styles mount"
[[ -n "$(find "$SANDBOX/.codex/agents" -mindepth 1 -maxdepth 1 -type f -print -quit 2>/dev/null)" ]] \
  && ok "Codex generated agents are non-empty" \
  || fail "Codex generated agents are non-empty"
[[ -f "$SANDBOX/.codex/rules/asha.rules" ]] \
  && ok "Codex native rules file exists" \
  || fail "Codex native rules file exists"
if grep -Fq "control-event.sh PermissionRequest" "$SANDBOX/.codex/config.toml" \
    && grep -Fq "control-event.sh Stop" "$SANDBOX/.codex/config.toml" \
    && grep -Fq "verify-pass-complete.sh" "$SANDBOX/.codex/config.toml"; then
  ok "Codex install renders PermissionRequest and both Stop handlers"
else
  fail "Codex install renders PermissionRequest and both Stop handlers"
fi
if [[ -f "$SANDBOX/.config/opencode/plugins/asha.js" ]] \
    && grep -q 'session.idle' "$SANDBOX/.config/opencode/plugins/asha.js" \
    && grep -q 'verify-pass-complete.sh' "$SANDBOX/.config/opencode/plugins/asha.js"; then
  ok "OpenCode integration plugin renders idle verification"
else
  fail "OpenCode integration plugin renders idle verification"
fi
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
	     && grep -q 'control-task.json' "$save_workflow" \
	     && grep -q 'effective.*scope' "$save_workflow" \
	     && grep -q 'save_none.py' "$save_workflow" \
	     && grep -q -- '--scope none' "$save_workflow" \
	     && grep -q 'identity_status=skipped' "$save_workflow" \
	     && grep -q 'do not invoke `git diff`' "$save_workflow" \
	     && awk '/memory_v2.py" publish/{p=NR} /save_identity.py/{i=NR} END{exit !(p && i && p < i)}' "$save_workflow" \
	     && ! grep -q -- '--capability\|learning_capability' "$save_workflow"; then
    ok "rendered explicit save includes managed no-Git scope and identity ordering: $save_workflow"
  else
    fail "rendered explicit save managed-scope or identity workflow missing/stale: $save_workflow"
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
for revision_skill in \
  "$SANDBOX/.claude/skills/write-revision-pass/SKILL.md" \
  "$SANDBOX/.codex/skills/write-revision-pass/SKILL.md" \
  "$SANDBOX/.copilot/skills/write-revision-pass/SKILL.md" \
  "$SANDBOX/.config/opencode/skills/write-revision-pass/SKILL.md"; do
  if [[ -f "$revision_skill" || -L "$revision_skill" ]] \
      && grep -q 'exactly one read-only review agent to each act' "$revision_skill" \
      && grep -q 'DECISIONS.md' "$revision_skill" \
      && grep -q 'Record the exact command and its empty output' "$revision_skill"; then
    ok "revision-pass skill carries the complete contract: $revision_skill"
  else
    fail "revision-pass skill carries the complete contract: $revision_skill"
  fi
done
for review_surface in \
  "$SANDBOX/.claude/commands/write/review-section.md" \
  "$SANDBOX/.codex/skills/write-review-section/SKILL.md" \
  "$SANDBOX/.copilot/skills/write-review-section/SKILL.md" \
  "$SANDBOX/.config/opencode/commands/write-review-section.md"; do
  grep -q 'one agent per section' "$review_surface" \
    && ok "multi-section review fan-out is rendered: $review_surface" \
    || fail "multi-section review fan-out is rendered: $review_surface"
done
for turn_surface in \
  "$SANDBOX/.claude/commands/rp/turn.md" \
  "$SANDBOX/.codex/skills/rp-turn/SKILL.md" \
  "$SANDBOX/.copilot/skills/rp-turn/SKILL.md" \
  "$SANDBOX/.config/opencode/commands/rp-turn.md"; do
  grep -q 'Scene: <scene> | Location: <location> | Present:' "$turn_surface" \
    && ok "RP walkthrough anchor contract is rendered: $turn_surface" \
    || fail "RP walkthrough anchor contract is rendered: $turn_surface"
done
for imported_path in \
  "$SANDBOX/.claude/skills/imported-demo" \
  "$SANDBOX/.codex/skills/imported-demo" \
  "$SANDBOX/.copilot/skills/imported-demo" \
  "$SANDBOX/.config/opencode/skills/imported-demo"; do
  if [[ -L "$imported_path" \
     && "$(readlink "$imported_path")" == "$SANDBOX/.asha/skills/.mounts/imported-demo" \
     && "$(sed -n 's/^name: //p' "$imported_path/SKILL.md")" == imported-demo ]]; then
    ok "portable imported skill adapter mounted: $imported_path"
  else
    fail "portable imported skill adapter mounted: $imported_path"
  fi
done
if [[ "$(sed -n 's/^name: //p' "$SANDBOX/.asha/skills/demo/SKILL.md")" == demo ]]; then
  ok "import adapter preserves canonical upstream frontmatter"
else
  fail "import adapter preserves canonical upstream frontmatter"
fi
for untracked_path in \
  "$SANDBOX/.claude/skills/imported-untracked" \
  "$SANDBOX/.codex/skills/imported-untracked" \
  "$SANDBOX/.copilot/skills/imported-untracked" \
  "$SANDBOX/.config/opencode/skills/imported-untracked"; do
  [[ ! -e "$untracked_path" && ! -L "$untracked_path" ]] \
    && ok "untracked user skill is not mounted: $untracked_path" \
    || fail "untracked user skill is not mounted: $untracked_path"
done
if jq -e '
    (.hooks.sessionStart[0].bash | endswith("session-start.sh")) and
    (.hooks.userPromptSubmitted[0].bash | endswith("user-prompt-submit.sh")) and
    (.hooks.userPromptSubmitted[1].bash | endswith("verify-pass-complete.sh")) and
    (.hooks.postToolUse[0].bash | endswith("post-tool-use.sh")) and
    (.hooks.postToolUse[0].timeoutSec == 15) and
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
# Test 2: Claude hook ownership spans recovery plus Control Stop observation
# ---------------------------------------------------------------------------
echo "--- test 2: hook registration covers lifecycle events ---"
hook_count="$(asha_hook_count)"
event_count="$(asha_hook_event_count)"
[[ "$hook_count" -ge 7 ]] \
  && ok "at least 7 asha-tagged hook entries registered ($hook_count)" \
  || fail "at least 7 asha-tagged hook entries registered (got $hook_count)"
assert_eq "asha hooks span all six registered events" "6" "$event_count"

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
         "$SANDBOX/.claude/commands"
  mkdir -p "$SANDBOX/.claude/skills" "$SANDBOX/.claude/agents" \
           "$SANDBOX/.claude/commands"
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

# ---------------------------------------------------------------------------
# Test 7: imported store ownership covers retirement and uninstall
# ---------------------------------------------------------------------------
echo "--- test 7: imported skill links are fully owned ---"
reset_sandbox
seed_native_configs
seed_imported_skill
if run_install --target all >/dev/null 2>&1; then
  ok "imported ownership fixture installs"
else
  fail "imported ownership fixture installs"
fi
# Retire the lock entry while retaining its canonical source. A full install
# must prune the now-revoked links while leaving canonical content alone.
jq '.skills = {}' "$SANDBOX/.asha/skills/imported.lock.json" \
  > "$SANDBOX/.asha/skills/imported.lock.json.tmp"
mv "$SANDBOX/.asha/skills/imported.lock.json.tmp" \
   "$SANDBOX/.asha/skills/imported.lock.json"
if run_install --target all >/dev/null 2>&1; then
  retired_left=0
  for retired_path in \
    "$SANDBOX/.claude/skills/imported-demo" \
    "$SANDBOX/.codex/skills/imported-demo" \
    "$SANDBOX/.copilot/skills/imported-demo" \
    "$SANDBOX/.config/opencode/skills/imported-demo"; do
    [[ -L "$retired_path" || -e "$retired_path" ]] && retired_left=$((retired_left + 1))
  done
  [[ $retired_left -eq 0 && -f "$SANDBOX/.asha/skills/demo/SKILL.md" ]] \
    && ok "full install prunes retired imported skill links" \
    || fail "full install prunes retired imported skill links ($retired_left remain)"
else
  fail "full install reconciles a retired imported skill"
fi

# Restore one active lock-recorded import, then prove uninstall claims its links
# without touching the canonical source directory.
jq '.skills = {demo: {source: "fixture/repo"}}' \
  "$SANDBOX/.asha/skills/imported.lock.json" \
  > "$SANDBOX/.asha/skills/imported.lock.json.tmp"
mv "$SANDBOX/.asha/skills/imported.lock.json.tmp" \
   "$SANDBOX/.asha/skills/imported.lock.json"
run_install --target all >/dev/null 2>&1 || true
if env -u XDG_CONFIG_HOME -u XDG_DATA_HOME -u XDG_STATE_HOME -u ASHA_HOME \
     HOME="$SANDBOX" PYTHONPATH="$PYTHON_USER_SITE${PYTHONPATH:+:$PYTHONPATH}" \
     bash "$REPO_ROOT/uninstall.sh" --target all >/dev/null 2>&1; then
  imported_left=0
  for imported_path in \
    "$SANDBOX/.claude/skills/imported-demo" \
    "$SANDBOX/.codex/skills/imported-demo" \
    "$SANDBOX/.copilot/skills/imported-demo" \
    "$SANDBOX/.config/opencode/skills/imported-demo"; do
    [[ -L "$imported_path" || -e "$imported_path" ]] && imported_left=$((imported_left + 1))
  done
  if [[ $imported_left -eq 0 && -f "$SANDBOX/.asha/skills/demo/SKILL.md" ]]; then
    ok "uninstall removes imported mounts but preserves canonical content"
  else
    fail "uninstall removes imported mounts but preserves canonical content"
  fi
else
  fail "uninstall owns active imported skill links"
fi

# ---------------------------------------------------------------------------
# Test 8: quoted YAML name keys mount through every harness
# ---------------------------------------------------------------------------
echo "--- test 8: imported adapters accept quoted YAML name keys ---"
quoted_keys=("'name'" '"name"')
quoted_indents=('' '  ')
for quoted_index in 0 1; do
  reset_sandbox
  seed_native_configs
  quoted_key="${quoted_keys[$quoted_index]}"
  quoted_indent="${quoted_indents[$quoted_index]}"
  seed_imported_skill_with_name_key "$quoted_key" "$quoted_indent"
  if quoted_out="$(run_install --target all 2>&1)"; then
    quoted_ok=1
    for quoted_path in \
      "$SANDBOX/.claude/skills/imported-demo" \
      "$SANDBOX/.codex/skills/imported-demo" \
      "$SANDBOX/.copilot/skills/imported-demo" \
      "$SANDBOX/.config/opencode/skills/imported-demo"; do
      [[ -L "$quoted_path" ]] \
        && frontmatter_name_is "$quoted_path/SKILL.md" imported-demo \
        || quoted_ok=0
    done
    [[ $quoted_ok -eq 1 ]] \
      && ok "quoted imported name key mounts on every harness: $quoted_key" \
      || fail "quoted imported name key mounts on every harness: $quoted_key"
    grep -Fq "${quoted_indent}${quoted_key}: demo" \
      "$SANDBOX/.asha/skills/demo/SKILL.md" \
      && ok "quoted adapter leaves canonical frontmatter unchanged: $quoted_key" \
      || fail "quoted adapter leaves canonical frontmatter unchanged: $quoted_key"
  else
    fail "quoted imported name key installs: $quoted_key (output: $(tail -5 <<<"$quoted_out"))"
  fi
done

reset_sandbox
seed_native_configs
seed_imported_skill_with_aliases
if alias_out="$(run_install --target all 2>&1)"; then
  alias_adapter="$SANDBOX/.asha/skills/.mounts/imported-demo/SKILL.md"
  if python3 - "$alias_adapter" <<'PY'
import sys, yaml
with open(sys.argv[1], encoding="utf-8") as handle:
    frontmatter = handle.read().split("---", 2)[1]
parsed = yaml.safe_load(frontmatter)
assert parsed["name"] == "imported-demo"
assert parsed["description"] == "demo"
assert parsed["metadata"]["name"] == "nested-value"
PY
  then
    ok "adapter rewrites only the top-level semantic name"
  else
    fail "adapter preserves anchored aliases and nested name keys"
  fi
  grep -Fq 'name: &skill_name demo' "$SANDBOX/.asha/skills/demo/SKILL.md" \
    && grep -Fq 'description: *skill_name' "$SANDBOX/.asha/skills/demo/SKILL.md" \
    && ok "structural adapter leaves canonical anchored YAML unchanged" \
    || fail "structural adapter leaves canonical anchored YAML unchanged"
else
  fail "structural imported name installs (output: $(tail -5 <<<"$alias_out"))"
fi

reset_sandbox
seed_native_configs
seed_imported_skill_with_merge
if merge_out="$(run_install --target all 2>&1)"; then
  merge_adapter="$SANDBOX/.asha/skills/.mounts/imported-demo/SKILL.md"
  if frontmatter_name_is "$merge_adapter" imported-demo \
     && grep -Fq '<<: &identity' "$merge_adapter" \
     && grep -Fq 'name: demo' "$SANDBOX/.asha/skills/demo/SKILL.md"; then
    ok "adapter materializes a top-level name over merged YAML identity"
  else
    fail "adapter preserves merged YAML while overriding its semantic name"
  fi
else
  fail "merged imported name installs (output: $(tail -5 <<<"$merge_out"))"
fi

for merge_shape in flow indented; do
  reset_sandbox
  seed_native_configs
  "seed_imported_skill_with_${merge_shape}_merge"
  shaped_out=""
  if shaped_out="$(run_install --target all 2>&1)"; then
    shaped_adapter="$SANDBOX/.asha/skills/.mounts/imported-demo/SKILL.md"
    if frontmatter_name_is "$shaped_adapter" imported-demo \
       && frontmatter_name_is "$SANDBOX/.asha/skills/demo/SKILL.md" demo; then
      ok "$merge_shape merged frontmatter mounts with a derived name"
    else
      fail "$merge_shape merged adapter is valid YAML with the derived name"
    fi
  else
    fail "$merge_shape merged imported name installs (output: $(tail -5 <<<"$shaped_out"))"
  fi
done

# ---------------------------------------------------------------------------
# Test 9: imported locks require complete provenance before mounting
# ---------------------------------------------------------------------------
echo "--- test 9: malformed imported provenance never mounts ---"
for malformed_kind in scalar missing-source; do
  reset_sandbox
  seed_native_configs
  seed_imported_skill
  if [[ "$malformed_kind" == scalar ]]; then
    jq '.skills.demo = 42' "$SANDBOX/.asha/skills/imported.lock.json" \
      > "$SANDBOX/.asha/skills/imported.lock.json.tmp"
  else
    jq 'del(.skills.demo.source)' "$SANDBOX/.asha/skills/imported.lock.json" \
      > "$SANDBOX/.asha/skills/imported.lock.json.tmp"
  fi
  mv "$SANDBOX/.asha/skills/imported.lock.json.tmp" \
     "$SANDBOX/.asha/skills/imported.lock.json"
  malformed_out=""
  if malformed_out="$(run_install --target claude 2>&1)"; then
    fail "malformed imported lock entry is refused: $malformed_kind"
  else
    [[ "$malformed_out" == *"invalid imported skill lockfile"* \
       && ! -e "$SANDBOX/.claude/skills/imported-demo" \
       && ! -e "$SANDBOX/.asha/skills/.mounts" ]] \
      && ok "malformed imported lock entry is refused before mounts: $malformed_kind" \
      || fail "malformed imported lock refusal is clear and write-free: $malformed_kind"
  fi
done

reset_sandbox
seed_native_configs
seed_imported_skill
printf '\n# local drift\n' >> "$SANDBOX/.asha/skills/demo/SKILL.md"
drift_out=""
if drift_out="$(run_install --target claude 2>&1)"; then
  if [[ "$drift_out" == *"imported skill has drifted: demo at $SANDBOX/.asha/skills/demo"* \
     && "$drift_out" != *"invalid imported skill lockfile"* \
     && -L "$SANDBOX/.claude/skills/asha-find-skills" \
     && ! -e "$SANDBOX/.claude/skills/imported-demo" ]]; then
    ok "imported drift reports its store path without aborting repository plugins"
  else
    fail "imported drift is isolated with its true cause (output: $(tail -8 <<<"$drift_out"))"
  fi
else
  fail "imported drift does not abort the Claude target (output: $(tail -8 <<<"$drift_out"))"
fi

# ---------------------------------------------------------------------------
# Test 10: imported mount names enforce the Agent Skills 64-character cap
# ---------------------------------------------------------------------------
echo "--- test 10: imported mount names stay within 64 characters ---"
for name_length in 55 56 64 65; do
  reset_sandbox
  seed_native_configs
  seed_imported_skill_with_length "$name_length"
  length_out=""
  if length_out="$(run_install --target claude 2>&1)"; then
    if [[ $name_length -eq 55 \
       && ${#SEEDED_NAME} -eq 55 \
       && -L "$SANDBOX/.claude/skills/imported-$SEEDED_NAME" \
       && $((9 + ${#SEEDED_NAME})) -eq 64 ]]; then
      ok "55-character upstream name mounts at exactly 64 characters"
    else
      fail "$name_length-character upstream name is refused when over limit"
    fi
  else
    if [[ $name_length -eq 56 || $name_length -eq 64 ]]; then
      [[ "$length_out" == *"mount name exceeds Agent Skills 64-character limit"* \
         && ! -e "$SANDBOX/.asha/skills/.mounts" ]] \
        && ok "$name_length-character upstream name is refused before adapter writes" \
        || fail "$name_length-character upstream name refusal is clear and write-free"
    elif [[ $name_length -eq 65 ]]; then
      [[ "$length_out" == *"must be 1-64 characters"* \
         && ! -e "$SANDBOX/.asha/skills/.mounts" ]] \
        && ok "65-character upstream name is rejected before adapter writes" \
        || fail "65-character upstream name rejection is clear and write-free"
    else
      fail "55-character upstream name mounts successfully (output: $(tail -5 <<<"$length_out"))"
    fi
  fi
done

reset_sandbox
seed_native_configs
seed_imported_skill
outside_skill="$SANDBOX/outside.txt"
printf 'foreign bytes\n' > "$outside_skill"
outside_before="$(sha256sum "$outside_skill" | awk '{print $1}')"
ln -s "$outside_skill" "$SANDBOX/.asha/skills/demo/link.txt"
symlink_out=""
if symlink_out="$(run_install --target claude 2>&1)"; then
  outside_after="$(sha256sum "$outside_skill" | awk '{print $1}')"
  [[ "$symlink_out" == *"imported skill has unsafe symlink drift: demo at $SANDBOX/.asha/skills/demo"* \
     && "$outside_before" == "$outside_after" \
     && -L "$SANDBOX/.claude/skills/asha-find-skills" \
     && ! -e "$SANDBOX/.claude/skills/imported-demo" \
     && ! -e "$SANDBOX/.asha/skills/.mounts" ]] \
    && ok "imported symlink drift is isolated without touching its target" \
    || fail "imported symlink drift isolation is clear and write-free (output: $(tail -8 <<<"$symlink_out"))"
else
  fail "imported symlink drift does not abort the Claude target (output: $(tail -8 <<<"$symlink_out"))"
fi

reset_sandbox
seed_native_configs
seed_imported_skill
touch "$SANDBOX/.asha/skills/demo/secret.txt"
chmod 000 "$SANDBOX/.asha/skills/demo/secret.txt"
probe_out=""
if probe_out="$(run_install --target claude 2>&1)"; then
  fail "hard imported drift-probe I/O failures abort the target"
else
  [[ "$probe_out" == *"imported skill drift probe failed: $SANDBOX/.asha/skills"* \
     && "$probe_out" != *"Traceback"* \
     && "$probe_out" != *"imported skill has drifted"* \
     && ! -e "$SANDBOX/.claude/skills/imported-demo" \
     && ! -e "$SANDBOX/.claude/skills/asha-find-skills" ]] \
    && ok "hard imported drift-probe failures are distinct and abort safely" \
    || fail "hard imported drift-probe failure is clear and distinct (output: $(tail -8 <<<"$probe_out"))"
fi
chmod 600 "$SANDBOX/.asha/skills/demo/secret.txt"

reset_sandbox
seed_native_configs
seed_imported_skill
mkdir -p "$SANDBOX/no-pyyaml"
cat > "$SANDBOX/no-pyyaml/yaml.py" <<'PY'
raise ImportError("PyYAML intentionally unavailable")
PY
ASHA_TEST_PYTHONPATH="$SANDBOX/no-pyyaml"
pyyaml_out=""
if pyyaml_out="$(run_install --only imported --target claude 2>&1)"; then
  fail "installer refuses an imported mount when PyYAML is unavailable"
else
  [[ "$pyyaml_out" == *"PyYAML is required to adapt imported skill imported-demo"* \
     && "$pyyaml_out" == *"install PyYAML for python3 and retry"* \
     && "$pyyaml_out" != *"Traceback"* \
     && ! -e "$SANDBOX/.claude/skills/imported-demo" ]] \
    && ok "installer names the PyYAML mount dependency and remedy" \
    || fail "installer PyYAML refusal is clear and write-free (output: $(tail -8 <<<"$pyyaml_out"))"
fi
unset ASHA_TEST_PYTHONPATH

echo ""
echo "=== Install Test Summary ==="
echo -e "Passed: ${GREEN}$PASS${NC}"
echo -e "Failed: ${RED}$FAIL${NC}"

[[ $FAIL -eq 0 ]]
