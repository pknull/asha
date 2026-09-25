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

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq not available" >&2; exit 1; }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
PYTHON_USER_SITE="$(python3 -c 'import site; print(site.getusersitepackages())')"

reset_sandbox() {
  # Keep mktemp's private ancestor: recreating it under 0002 makes it unsafe.
  # Explicit globs include hidden/double-dot entries, not dot or dotdot.
  local entry
  for entry in "$SANDBOX"/* "$SANDBOX"/.[!.]* "$SANDBOX"/..?*; do
    [[ -e "$entry" || -L "$entry" ]] || continue
    rm -rf -- "$entry"
  done
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

run_mklink() {
  env HOME="$SANDBOX" ASHA_HOME="$SANDBOX/.asha" \
    bash -c '
      set -euo pipefail
      source "$1/lib/install.sh"
      FORCE=1 DRY_RUN=0 VERBOSE=0
      mklink "$2" "$3" "$4"
    ' _ "$REPO_ROOT" "$1" "$2" "$3"
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
echo "--- fixture: repeated resets preserve private root under umask 0002 ---"
root_identity="$(python3 -c 'import os,sys; s=os.stat(sys.argv[1]); print(s.st_dev,s.st_ino,s.st_mode & 0o777)' "$SANDBOX")"
for reset_round in 1 2; do
  (
    umask 0002
    mkdir -p "$SANDBOX/visible/child" "$SANDBOX/.hidden" "$SANDBOX/..hidden"
    touch "$SANDBOX/.dotfile"
    ln -s missing "$SANDBOX/.dangling"
    reset_sandbox
  )
  if python3 - "$SANDBOX" "$root_identity" <<'PY_RESET'
import os, pathlib, sys
p = pathlib.Path(sys.argv[1]); s = p.stat()
assert f'{s.st_dev} {s.st_ino} {s.st_mode & 0o777}' == sys.argv[2]
assert s.st_mode & 0o777 == 0o700
assert not list(p.iterdir())
PY_RESET
  then ok "reset $reset_round retains private inode and removes hidden entries"
  else fail "reset $reset_round retains private inode and removes hidden entries"
  fi
done
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
for skill_home in "$SANDBOX/.claude" "$SANDBOX/.codex" "$SANDBOX/.copilot" "$SANDBOX/.config/opencode"; do
  if [[ -f "$skill_home/skills/session-project-memory/SKILL.md" ]] \
      && [[ "$(readlink -f "$skill_home/skills/session-project-memory")" == "$REPO_ROOT/plugins/session/skills/project-memory" ]]; then
    ok "project-memory uses the source skill in $skill_home"
  else
    fail "project-memory skill missing or detached from source in $skill_home"
  fi
done
for hook_file in "$SANDBOX/.claude/settings.json" "$SANDBOX/.codex/hooks.json"; do
  if grep -Fq 'control-event.sh PreToolUse' "$hook_file"; then
    ok "completion invalidation PreToolUse registered in $hook_file"
  else
    fail "completion invalidation PreToolUse missing in $hook_file"
  fi
done
if grep -Fq "control-event.sh PermissionRequest" "$SANDBOX/.codex/hooks.json" \
    && grep -Fq "control-event.sh Stop" "$SANDBOX/.codex/hooks.json" \
    && grep -Fq "verify-pass-complete.sh" "$SANDBOX/.codex/hooks.json"; then
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
	     && grep -q -- '--expected-active' "$save_workflow" \
	     && grep -q -- '--expected-decisions' "$save_workflow" \
	     && grep -q 'experience dispose' "$save_workflow" \
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
  "$SANDBOX/.codex/skills/asha-asha-reference/SKILL.md" \
  "$SANDBOX/.copilot/skills/asha-asha-reference/SKILL.md" \
  "$SANDBOX/.config/opencode/skills/asha-asha-reference/SKILL.md"; do
  [[ -f "$skill_path" || -L "$skill_path" ]] \
    && ok "cold identity reference skill installed: $skill_path" \
    || fail "cold identity reference skill installed: $skill_path"
done
for skills_root in \
  "$SANDBOX/.claude/skills" \
  "$SANDBOX/.codex/skills" \
  "$SANDBOX/.copilot/skills" \
  "$SANDBOX/.config/opencode/skills"; do
  if [[ -L "$skills_root/code-postgres" && ! -e "$skills_root/postgres" ]]; then
    ok "plugin skill uses one portable destination name: $skills_root/code-postgres"
  else
    fail "plugin skill uses one portable destination name: $skills_root/code-postgres"
  fi
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
for inline_skill in \
  "$SANDBOX/.claude/skills/write-inline-review/SKILL.md" \
  "$SANDBOX/.codex/skills/write-inline-review/SKILL.md" \
  "$SANDBOX/.copilot/skills/write-inline-review/SKILL.md" \
  "$SANDBOX/.config/opencode/skills/write-inline-review/SKILL.md"; do
  if [[ -f "$inline_skill" ]] \
      && grep -q 'name: write-inline-review' "$inline_skill" \
      && grep -q 'asha-inline-review/v1' "$inline_skill" \
      && [[ -f "$(dirname "$inline_skill")/scripts/inline_review.py" ]]; then
    ok "inline-review skill and shared parser resolve: $inline_skill"
  else
    fail "inline-review skill and shared parser resolve: $inline_skill"
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
if run_install --target copilot >/dev/null 2>&1 \
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
assert_eq "asha hooks span all seven registered events" "7" "$event_count"
jq -e '.hooks.PostToolUseFailure[]?.hooks[]?
    | select((.source // "") | startswith("asha:"))
    | select((.command // "") | endswith("control-event.sh PostToolUseFailure"))' \
    "$SANDBOX/.claude/settings.json" >/dev/null \
  && ok "Claude failed-tool callbacks reach the Control bridge" \
  || fail "Claude failed-tool callbacks reach the Control bridge"

# ---------------------------------------------------------------------------
# Test 3: a Codex failure does not abort Claude, Copilot, or OpenCode
# ---------------------------------------------------------------------------
echo "--- test 3: per-harness failure isolation ---"
reset_sandbox
seed_native_configs
printf 'broken = [' > "$SANDBOX/.codex/config.toml"
mkdir -p "$SANDBOX/.codex/skills/keep" "$SANDBOX/.asha/install-manifests"
printf 'preserve generated artifact\n' > "$SANDBOX/.codex/skills/keep/SKILL.md"
printf '{"schema_version":1,"harness":"codex","artifacts":[]}\n' > "$SANDBOX/.asha/install-manifests/codex.json"
isolation_before="$(python3 - "$SANDBOX" <<'PY'
import os, pathlib, sys
home = pathlib.Path(sys.argv[1])
paths = sorted((home/'.codex').rglob('*')) + [home/'.asha/install-manifests/codex.json']
print([(str(p.relative_to(home)), p.lstat().st_mode, p.lstat().st_uid,
        p.lstat().st_gid, os.readlink(p) if p.is_symlink() else
        None if p.is_dir() else p.read_bytes()) for p in paths])
PY
)"
isolation_rc=0
isolation_out="$(run_install --target all 2>&1)" || isolation_rc=$?
assert_eq "install reports rc1 for the safely refused Codex target" "1" "$isolation_rc"
[[ "$isolation_out" == *"Codex preservation refused:"* && "$isolation_out" == *"Invalid value"* ]] \
  && ok "Codex failure names the malformed TOML reason" \
  || fail "Codex failure names the malformed TOML reason (output: $isolation_out)"
isolation_after="$(python3 - "$SANDBOX" <<'PY'
import os, pathlib, sys
home = pathlib.Path(sys.argv[1])
paths = sorted((home/'.codex').rglob('*')) + [home/'.asha/install-manifests/codex.json']
print([(str(p.relative_to(home)), p.lstat().st_mode, p.lstat().st_uid,
        p.lstat().st_gid, os.readlink(p) if p.is_symlink() else
        None if p.is_dir() else p.read_bytes()) for p in paths])
PY
)"
assert_eq "refused Codex config/artifacts/ledger bytes and metadata survive" "$isolation_before" "$isolation_after"
[[ -n "$(find "$SANDBOX/.claude/skills" -mindepth 1 -maxdepth 1 -type l -print -quit 2>/dev/null)" ]] \
  && ok "Claude mounts survive Codex failure" \
  || fail "Claude mounts survive Codex failure"
[[ -f "$SANDBOX/.config/opencode/plugins/asha.js" ]] \
  && ok "OpenCode install survives Codex failure" \
  || fail "OpenCode install survives Codex failure"
grep -q '^install summary:$' <<<"$isolation_out" && grep -q '^  codex: FAILED$' <<<"$isolation_out" \
  && ok "per-harness summary names the Codex failure" \
  || fail "per-harness summary names the Codex failure"
grep -q '^  copilot: ok$' <<<"$isolation_out" \
  && [[ -f "$SANDBOX/.copilot/hooks/asha-recovery.json" ]] \
  && ok "independently requested Copilot succeeds despite Codex refusal" \
  || fail "independently requested Copilot succeeds despite Codex refusal"

echo "--- test 3a: absent Codex config is a supported positive install ---"
reset_sandbox
absent_rc=0
absent_out="$(run_install --target codex 2>&1)" || absent_rc=$?
if [[ $absent_rc -eq 0 && ! -e "$SANDBOX/.codex/config.toml" \
      && ! -L "$SANDBOX/.codex/config.toml" ]] \
    && jq -e '.hooks.Stop | length > 0' "$SANDBOX/.codex/hooks.json" >/dev/null \
    && jq -e '[.artifacts[] | select(.type == "codex-hooks-json")] | length == 1' \
      "$SANDBOX/.asha/install-manifests/codex.json" >/dev/null; then
  ok "absent config installs owned JSON successfully without creating config.toml"
else
  fail "absent config positive install (rc=$absent_rc; output: $absent_out)"
fi

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
# Test 5b: disabling an optional plugin retires only installer-owned artifacts
# ---------------------------------------------------------------------------
echo "--- test 5b: default reinstall retires the opt-in canary ---"
reset_sandbox
seed_native_configs
if ! run_install --target all --with-canary >/dev/null 2>&1; then
  fail "with-canary retirement fixture installs"
else
  canary_artifacts=(
    "$SANDBOX/.claude/skills/test-ping"
    "$SANDBOX/.claude/agents/test/echo.md"
    "$SANDBOX/.claude/commands/test/ping.md"
    "$SANDBOX/.codex/skills/test-ping"
    "$SANDBOX/.codex/agents/test-test-echo.toml"
    "$SANDBOX/.copilot/skills/test-ping"
    "$SANDBOX/.copilot/agents/test-test-echo.agent.md"
    "$SANDBOX/.config/opencode/skills/test-ping"
    "$SANDBOX/.config/opencode/commands/test-ping.md"
    "$SANDBOX/.config/opencode/agents/test-test-echo.md"
  )
  canary_missing=0
  for canary_path in "${canary_artifacts[@]}"; do
    [[ -e "$canary_path" || -L "$canary_path" ]] || canary_missing=$((canary_missing + 1))
  done
  canary_hooks="$(jq -r '[.hooks // {} | .[] | .[]? | .hooks[]?
    | select((.source // "") == "asha:test")] | length' \
    "$SANDBOX/.claude/settings.json")"
  [[ $canary_missing -eq 0 && $canary_hooks -gt 0 ]] \
    && ok "with-canary install exposes every canary primitive" \
    || fail "with-canary install exposes every canary primitive ($canary_missing missing; hooks=$canary_hooks)"

  # A foreign symlink under a scanned primitive root is not installer-owned:
  # its target is outside the repository plugins tree and must survive.
  mkdir -p "$SANDBOX/user-skill"
  ln -s "$SANDBOX/user-skill" "$SANDBOX/.claude/skills/user-canary-reference"

  if run_install --target all --dry-run >/dev/null 2>&1; then
    dry_run_missing=0
    for canary_path in "${canary_artifacts[@]}"; do
      [[ -e "$canary_path" || -L "$canary_path" ]] || dry_run_missing=$((dry_run_missing + 1))
    done
    dry_run_hooks="$(jq -r '[.hooks // {} | .[] | .[]? | .hooks[]?
      | select((.source // "") == "asha:test")] | length' \
      "$SANDBOX/.claude/settings.json")"
    [[ $dry_run_missing -eq 0 && $dry_run_hooks -eq "$canary_hooks" ]] \
      && ok "dry-run reports retirement without changing canary artifacts" \
      || fail "dry-run leaves canary artifacts unchanged ($dry_run_missing missing; hooks=$dry_run_hooks)"
  else
    fail "dry-run retirement fixture exits 0"
  fi

  if run_install --target all >/dev/null 2>&1; then
    canary_left=0
    for canary_path in "${canary_artifacts[@]}"; do
      [[ -e "$canary_path" || -L "$canary_path" ]] && canary_left=$((canary_left + 1))
    done
    canary_hooks="$(jq -r '[.hooks // {} | .[] | .[]? | .hooks[]?
      | select((.source // "") == "asha:test")] | length' \
      "$SANDBOX/.claude/settings.json")"
    manifest_canary=0
    for manifest in "$SANDBOX"/.asha/install-manifests/*.json; do
      [[ -f "$manifest" ]] || continue
      count="$(jq -r '[.artifacts[]? | select(.source | contains("/plugins/test/"))] | length' "$manifest")"
      manifest_canary=$((manifest_canary + count))
    done
    if [[ $canary_left -eq 0 && $canary_hooks -eq 0 && $manifest_canary -eq 0 \
       && -L "$SANDBOX/.claude/skills/user-canary-reference" ]]; then
      ok "default reinstall retires canary links, generated artifacts, hooks, and ownership records"
      ok "default reinstall preserves foreign symlinks"
    else
      fail "default reinstall fully retires owned canary artifacts (left=$canary_left; hooks=$canary_hooks; manifest=$manifest_canary)"
      [[ -L "$SANDBOX/.claude/skills/user-canary-reference" ]] \
        && ok "default reinstall preserves foreign symlinks" \
        || fail "default reinstall preserves foreign symlinks"
    fi
  else
    fail "default canary-retirement reinstall exits 0"
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

# ---------------------------------------------------------------------------
# Test 11: --force never deletes a foreign real skill directory
# ---------------------------------------------------------------------------
echo "--- test 11: real destination ownership is manifest-gated ---"
reset_sandbox
seed_native_configs
foreign_skill="$SANDBOX/.codex/skills/code-postgres"
mkdir -p "$foreign_skill"
printf 'user-owned\n' > "$foreign_skill/keep.txt"
foreign_out=""
foreign_rc=0
foreign_out="$(run_mklink "$REPO_ROOT/plugins/code/skills/postgres" \
  "$foreign_skill" codex-skill 2>&1)" || foreign_rc=$?
if [[ $foreign_rc -eq 2 \
   && -d "$foreign_skill" && ! -L "$foreign_skill" \
   && "$(cat "$foreign_skill/keep.txt")" == user-owned \
   && "$foreign_out" == *"not recorded as Asha-generated"* ]]; then
  ok "--force refuses and preserves a user-owned real skill directory"
else
  fail "--force preserves foreign real skill directories (rc=$foreign_rc; output: $(tail -5 <<<"$foreign_out"))"
fi

reset_sandbox
seed_native_configs
managed_skill="$SANDBOX/.codex/skills/code-postgres"
managed_file="$managed_skill/SKILL.md"
mkdir -p "$managed_skill" "$SANDBOX/.asha/install-manifests"
printf 'previous generated bytes\n' > "$managed_file"
managed_hash="$(sha256sum "$managed_file" | awk '{print $1}')"
jq -n \
  --arg source "$REPO_ROOT/plugins/code/commands/review.md" \
  --arg destination "$managed_file" \
  --arg sha256 "$managed_hash" \
  '{schema_version: 1, harness: "codex", artifacts: [{source: $source, destination: $destination, type: "codex-command-skill", sha256: $sha256, orphan: false}]}' \
  > "$SANDBOX/.asha/install-manifests/codex.json"
if managed_out="$(run_mklink "$REPO_ROOT/plugins/code/skills/postgres" \
     "$managed_skill" codex-skill 2>&1)" \
   && [[ -L "$managed_skill" \
      && "$(readlink -f "$managed_skill")" == "$(readlink -f "$REPO_ROOT/plugins/code/skills/postgres")" ]]; then
  ok "--force replaces a manifest-recorded generated skill directory"
else
  fail "--force replaces a manifest-recorded generated skill directory (output: $(tail -5 <<<"${managed_out:-}"))"
fi

echo ""
# Real finalizer boundaries, including the conditional context used by Codex.
# Faults use test-owned filesystem permissions or failing I/O commands; no
# renderer, preflight, reconciliation, or validator is replaced with success.
if python3 - "$REPO_ROOT" "$SANDBOX" <<'PY_FINALIZE'
import hashlib, json, os, pathlib, re, shutil, site, subprocess, sys, tempfile, unittest
ROOT, WORK = map(pathlib.Path, sys.argv[1:])
ENV = {'PATH': os.environ['PATH'], 'PYTHONPATH': site.getusersitepackages()}

class FinalizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        self.manifest = self.home/'.asha/install-manifests/codex.json'
        self.manifest.parent.mkdir(parents=True)
        self.retired = self.home/'.codex/skills/retired/SKILL.md'
        self.retired.parent.mkdir(parents=True)
        self.retired.write_bytes(b'retired generated bytes\n')
        self.config = self.home/'.codex/config.toml'
        self.config.write_bytes(b'# native bytes\r\n[features]\r\nhooks = false\r\n'
            b'[hooks.state."native.slot"]\r\ntrusted_hash = "keep"\r\n')
        self.config.chmod(0o640)
        self.row = {'source':str(ROOT/'plugins/code/commands/review.md'),
            'destination':str(self.retired), 'type':'codex-command-skill',
            'sha256':hashlib.sha256(self.retired.read_bytes()).hexdigest(), 'orphan':False}
        self.original = (json.dumps({'schema_version':1, 'harness':'codex',
            'artifacts':[self.row]}, indent=3)+'\n').encode()
        self.stage = self.home/'stage.jsonl'
        self.env = dict(ENV, HOME=str(self.home))
        # Permission-based failures must actually be enforceable, never skipped.
        self.assertNotEqual(os.geteuid(), 0, 'run finalizer permission fixtures as an ordinary user')

    def seed(self):
        self.manifest.write_bytes(self.original)
        self.retired.parent.mkdir(parents=True, exist_ok=True)
        self.retired.write_bytes(b'retired generated bytes\n')
        self.stage.write_bytes(b'')

    def source(self, statements):
        return subprocess.run(['bash', '-c',
            'set -euo pipefail; source "$1/lib/install.sh"; '
            'DRY_RUN=0; FORCE=0; VERBOSE=0; ONLY=""; WITH_CANARY=0; '
            'ASHA_ARTIFACT_STAGE="$HOME/stage.jsonl"; '+statements,
            'finalizer-test', str(ROOT)], cwd=ROOT, env=self.env,
            capture_output=True, timeout=180)

    def call(self, command, context):
        if context == 'ordinary': return command
        if context == 'or-list': return command+' || exit $?'
        return 'if '+command+'; then exit 0; else exit $?; fi'

    def test_helper_failure_boundaries_and_conditional_status(self):
        cases = ('manifest-path', 'dirname', 'directory', 'output', 'manifest-read',
                 'manifest-json', 'stage-json', 'artifact-read', 'artifact-unlink', 'publication')
        for context in ('ordinary', 'or-list', 'if'):
            for case in cases:
                with self.subTest(context=context, case=case):
                    self.seed()
                    setup, rc, reason = '', 1, b'Permission denied'
                    if case == 'manifest-path':
                        setup = 'asha_artifact_manifest_path() { return 71; }; '
                        rc, reason = 71, b''
                    elif case == 'dirname':
                        setup = 'dirname() { return 72; }; '
                        rc, reason = 72, b''
                    elif case == 'directory':
                        # Real ensure_dir invokes a failing mkdir boundary.
                        (self.home/'not-directory').write_bytes(b'block mkdir')
                        setup = 'mkdir() { command mkdir "$HOME/not-directory/child"; }; '
                        reason = b'Not a directory'
                    elif case == 'output':
                        self.manifest.parent.chmod(0o500)
                    elif case == 'manifest-read':
                        self.manifest.chmod(0o000)
                    elif case == 'manifest-json':
                        self.manifest.write_bytes(b'{broken ledger')
                        reason = b'JSONDecodeError'
                    elif case == 'stage-json':
                        self.stage.write_bytes(b'{broken row\n')
                        reason = b'JSONDecodeError'
                    elif case == 'artifact-read':
                        self.retired.chmod(0o000)
                    elif case == 'artifact-unlink':
                        self.retired.parent.chmod(0o500)
                    elif case == 'publication':
                        # Permit the real Python reconciliation/output, then
                        # deny the real rename without changing the old ledger.
                        setup = ('mv() { chmod 500 "$HOME/.asha/install-manifests" || return $?; '
                                 'command mv "$@"; }; ')
                        # Partial finalize keeps the retired file for this case.
                    before = b'{broken ledger' if case == 'manifest-json' else self.original
                    stage_before = self.stage.read_bytes()
                    try:
                        command = 'asha_artifact_finalize codex '+('0' if case == 'publication' else '1')
                        p = self.source(setup+self.call(command, context))
                    finally:
                        # Restore only this new fixture, never live/sealed paths.
                        self.manifest.parent.chmod(0o700)
                        self.manifest.chmod(0o600)
                        if self.retired.parent.exists(): self.retired.parent.chmod(0o700)
                        if self.retired.exists(): self.retired.chmod(0o600)
                    self.assertEqual(p.returncode, rc, p.stderr.decode())
                    self.assertEqual(self.manifest.read_bytes(), before)
                    self.assertEqual(self.stage.read_bytes(), stage_before)
                    self.assertIn(reason, p.stderr)
                    outputs = list(self.manifest.parent.glob('codex.json.tmp.*'))
                    if case == 'publication':
                        self.assertTrue(any(json.loads(f.read_bytes())['artifacts'] == [self.row]
                                            for f in outputs if f.stat().st_size))
                    print(f'finalizer {context}/{case}: rc={p.returncode}; original ledger and stage preserved')
                    for path in outputs: path.unlink()

    def test_healthy_finalize_partial_full_and_fresh(self):
        for context in ('ordinary', 'or-list', 'if'):
            for full in (0, 1):
                with self.subTest(context=context, full=full):
                    self.seed()
                    p = self.source(self.call('asha_artifact_finalize codex '+str(full), context))
                    self.assertEqual(p.returncode, 0, p.stderr.decode())
                    self.assertEqual(json.loads(self.manifest.read_bytes())['artifacts'], [] if full else [self.row])
                    self.assertEqual(self.retired.exists(), not full)
                    self.assertFalse(self.stage.exists())
                    self.assertFalse(list(self.manifest.parent.glob('codex.json.tmp.*')))
        self.manifest.unlink()
        self.stage.write_bytes((json.dumps(self.row)+'\n').encode())
        p = self.source('asha_artifact_finalize codex 0')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(json.loads(self.manifest.read_bytes())['artifacts'], [self.row])

    def check_adapter_failure(self, p, expected_rc, native_before):
        self.assertEqual(p.returncode, expected_rc, p.stdout.decode()+p.stderr.decode())
        self.assertIn(b'Permission denied', p.stderr)
        self.assertEqual(self.manifest.read_bytes(), self.original)
        self.assertEqual((self.config.read_bytes(), self.config.stat().st_mode), native_before)
        # Proves preflight/publication really ran, rather than an earlier refusal.
        hooks = self.home/'.codex/hooks.json'
        self.assertTrue(json.loads(hooks.read_bytes())['hooks']['Stop'])
        matches = re.findall(rb'staging evidence retained at ([^\n]+)', p.stderr)
        self.assertEqual(len(matches), 1, p.stderr.decode())
        retained = pathlib.Path(os.fsdecode(matches[0]))
        self.addCleanup(shutil.rmtree, retained)
        stages = list(retained.glob('asha-artifacts-codex-*.jsonl'))
        self.assertEqual(len(stages), 1)
        rows = [json.loads(line) for line in stages[0].read_text().splitlines()]
        self.assertTrue(any(row['type'] == 'codex-hooks-json' for row in rows))
        print(f'public adapter rc={p.returncode}; native config, original ledger, and populated failed stage preserved')

    def test_real_codex_full_and_public_callers_propagate_reconciliation_failure(self):
        for entry in ('codex_install', 'asha_install_main --target codex', 'standalone'):
            for context in ('ordinary', 'or-list'):
                for fault in ('read', 'unlink'):
                    with self.subTest(entry=entry, context=context, fault=fault):
                        self.seed()
                        hooks = self.home/'.codex/hooks.json'
                        if hooks.exists(): hooks.unlink()  # only unrecorded test publication from prior case
                        if fault == 'read': self.retired.chmod(0o000)
                        else: self.retired.parent.chmod(0o500)
                        native_before = self.config.read_bytes(), self.config.stat().st_mode
                        try:
                            if entry == 'standalone':
                                p = subprocess.run([str(ROOT/'install.sh'), '--target', 'codex'],
                                    cwd=ROOT, env=self.env, capture_output=True, timeout=180)
                            else:
                                p = self.source('source "$1/harnesses/codex.sh"; '+self.call(entry, context))
                        finally:
                            self.retired.parent.chmod(0o700)
                            self.retired.chmod(0o600)
                        self.check_adapter_failure(p, 1, native_before)

    def test_sourced_hooks_only_publication_failure_preserves_stage(self):
        self.seed()
        native_before = self.config.read_bytes(), self.config.stat().st_mode
        try:
            p = self.source('source "$1/harnesses/codex.sh"; '
                'mv() { chmod 500 "$HOME/.asha/install-manifests" || return $?; command mv "$@"; }; '
                'codex_install_hooks || exit $?')
        finally:
            self.manifest.parent.chmod(0o700)
        self.check_adapter_failure(p, 1, native_before)

unittest.main(argv=['finalizer-boundaries'], verbosity=2)
PY_FINALIZE
then ok "finalizer real helper and public Codex failure/success boundaries"
else fail "finalizer real helper and public Codex failure/success boundaries"
fi

# Parent-engine launcher contract: real adapters, isolated HOME, direct public
# source calls, and exact bytes/modes/link destinations rather than grep-only
# success. Unattempted --bin requests are intentionally NOT adapter successes.
if python3 - "$REPO_ROOT" "$SANDBOX" <<'PY_U8_INSTALL'
import json, os, pathlib, site, subprocess, sys, tempfile, unittest
ROOT, WORK = map(pathlib.Path, sys.argv[1:])
HARNESSES = ('claude', 'codex', 'copilot', 'opencode')
ENV = {'PATH': os.environ['PATH'], 'USER': os.environ.get('USER', 'test'),
       'PYTHONPATH': site.getusersitepackages()}

def snapshot(root):
    return {str(p.relative_to(root)): (p.lstat().st_mode, p.lstat().st_uid,
            p.lstat().st_gid, os.readlink(p) if p.is_symlink() else
            None if p.is_dir() else p.read_bytes()) for p in root.rglob('*')}

class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        self.opencode = self.home/'fixture-opencode'
        self.opencode.write_text('#!/bin/sh\nprintf "1.17.18\\n"\n')
        self.opencode.chmod(0o700)
        self.env = dict(ENV, HOME=str(self.home), ASHA_OPENCODE_CMD=str(self.opencode))
        self.bin = self.home/'.local/bin'
        self.bin.mkdir(parents=True)
        (self.home/'.codex').mkdir()
        self.codex = self.home/'.codex/config.toml'
        self.codex.write_bytes(b'# native config\n')
        self.codex.chmod(0o640)
        (self.home/'.claude').mkdir()
        (self.home/'.claude/settings.json').write_bytes(b'{}\n')
        (self.home/'.asha').mkdir()
        self.cfg = self.home/'.asha/config.json'
        self.cfg.write_text(json.dumps({'asha_root':str(ROOT), 'default_harness':'claude', 'foreign':42}, indent=3)+'\n')
        self.cfg.chmod(0o640)

    def run_install(self, *args):
        return subprocess.run([str(ROOT/'install.sh'), *args], cwd=ROOT,
            env=self.env, capture_output=True, timeout=180,
            preexec_fn=lambda: os.umask(0o002))

    def source(self, statements):
        return subprocess.run(['bash', '-c', 'set -euo pipefail; source "$1/lib/install.sh"; '+statements,
            'u8', str(ROOT)], cwd=ROOT, env=self.env,
            capture_output=True, timeout=240)

    def route(self, target=None):
        (self.bin/'asha').symlink_to(target or ROOT/'bin/asha')
        for h in HARNESSES: (self.bin/('asha-'+h)).symlink_to('asha')

    def assert_shims(self, names):
        for h in names: self.assertEqual(os.readlink(self.bin/('asha-'+h)), 'asha')

    def test_new_codex_generated_directories_survive_group_writable_caller_umask(self):
        p = self.run_install('--target', 'codex', '--only', 'session')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        for directory in (self.home/'.codex/skills',
                          self.home/'.codex/skills/session-init',
                          self.home/'.asha/install-manifests'):
            self.assertEqual(directory.stat().st_mode & 0o022, 0, str(directory))
        p = self.run_install('--target', 'codex', '--only', 'session')
        self.assertEqual(p.returncode, 0, p.stderr.decode())

    def test_documented_default_target_bin_all_and_public_source(self):
        before = self.codex.read_bytes()
        p = self.run_install('--bin', 'all', '--only', 'test')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assert_shims(HARNESSES)
        self.assertEqual(self.codex.read_bytes(), before)  # unattempted, requested
        self.assertEqual(self.codex.stat().st_mode & 0o777, 0o640)
        p = self.source('DRY_RUN=0; FORCE=0; VERBOSE=0; install_bin all; install_bin all')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assert_shims(HARNESSES)

    def test_mixed_failure_reuses_current_routing_and_keeps_failed_target(self):
        self.route()
        self.codex.write_bytes(b'broken = [')
        artifact = self.home/'.codex/skills/keep/SKILL.md'
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b'foreign\n')
        before = snapshot(self.home/'.codex')
        routing = snapshot(self.bin), self.cfg.read_bytes(), self.cfg.stat().st_mode
        p = self.run_install('--target', 'all', '--bin', 'all', '--only', 'test', '--force')
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        self.assertIn(b'codex: FAILED', p.stdout)
        for h in ('claude', 'copilot', 'opencode'):
            self.assertIn((h+': ok').encode(), p.stdout, p.stderr.decode())
        self.assertEqual(snapshot(self.home/'.codex'), before)
        self.assertEqual((snapshot(self.bin), self.cfg.read_bytes(), self.cfg.stat().st_mode), routing)
        # Failed default requests do not rewrite a compatible shared config.
        p = self.run_install('--target', 'codex', '--bin', 'copilot', '--default', 'codex', '--only', 'test')
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        self.assertEqual(self.cfg.read_bytes(), routing[1])

    def test_stale_foreign_broken_dispatchers_and_root_refuse(self):
        for kind in ('stale', 'foreign', 'broken', 'root'):
            with self.subTest(kind=kind):
                dispatcher = self.bin/'asha'
                if dispatcher.exists() or dispatcher.is_symlink(): dispatcher.unlink()
                if kind == 'foreign': dispatcher.write_bytes(b'foreign dispatcher\n')
                elif kind in ('stale', 'broken'): dispatcher.symlink_to(self.home/'old/bin/asha')
                else: dispatcher.symlink_to(ROOT/'bin/asha')
                shim = self.bin/'asha-codex'
                if not shim.is_symlink(): shim.symlink_to('asha')
                self.cfg.write_text(json.dumps({'asha_root':str(self.home/'old') if kind == 'root' else str(ROOT),
                    'default_harness':'codex', 'foreign':42})+'\n')
                self.codex.write_bytes(b'bad = [')
                before = snapshot(self.bin), self.cfg.read_bytes(), snapshot(self.home/'.codex')
                p = self.run_install('--target', 'both', '--bin', 'all', '--force', '--only', 'test')
                self.assertEqual(p.returncode, 1, p.stderr.decode())
                self.assertIn(b'claude: ok', p.stdout)
                self.assertIn(b'launcher routing refused', p.stderr)
                self.assertEqual((snapshot(self.bin), self.cfg.read_bytes(), snapshot(self.home/'.codex')), before)

    def test_unrequested_consumers_versus_requested_unattempted_default(self):
        self.route()
        before = self.cfg.read_bytes()
        # Codex is unattempted but independently requested by --default.
        p = self.run_install('--bin', 'all', '--default', 'codex', '--only', 'test')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(json.loads(self.cfg.read_text())['default_harness'], 'codex')
        saved = snapshot(self.bin), self.cfg.read_bytes()
        p = self.run_install('--target', 'claude', '--bin', 'claude', '--default', 'claude', '--only', 'test')
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        self.assertEqual((snapshot(self.bin), self.cfg.read_bytes()), saved)
        # No --bin: explicit --default keeps the pre-existing no-write behavior.
        p = self.run_install('--target', 'claude', '--default', 'claude', '--only', 'test')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(self.cfg.read_bytes(), saved[1])

    def test_all_attempted_failed_dry_run_and_source_repeat_no_sticky_state(self):
        self.route()
        self.codex.write_bytes(b'bad = [')
        before = snapshot(self.home)
        p = self.run_install('--target', 'codex', '--bin', 'all', '--force', '--dry-run')
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        self.assertEqual(snapshot(self.home), before)
        p = self.source('set +e; asha_install_main --target codex --bin all --only test; rc=$?; '
            '[[ $rc == 1 ]] || exit 90; set -e; install_bin all; '
            'asha_install_main --bin all --only test')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assert_shims(HARNESSES)

    def test_every_attempted_adapter_fails_without_launcher_mutation(self):
        self.route()
        self.codex.write_bytes(b'bad = [')
        for native in ('.claude', '.copilot', '.config/opencode'):
            conflict = self.home/native/'skills/test-ping'
            conflict.mkdir(parents=True)
            (conflict/'foreign').write_bytes(b'must survive\n')
        before = snapshot(self.bin), self.cfg.read_bytes(), snapshot(self.home/'.codex')
        p = self.run_install('--target','all','--bin','all','--only','test')
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        for h in HARNESSES:
            self.assertIn((h+': FAILED').encode(), p.stdout)
        self.assertEqual((snapshot(self.bin), self.cfg.read_bytes(), snapshot(self.home/'.codex')), before)

    def test_requested_unattempted_shim_force_and_dry_run(self):
        self.route()
        shim = self.bin/'asha-codex'
        for target in ('/usr/bin/env', 'broken-old-shim'):
            shim.unlink()
            shim.symlink_to(target)
            before = snapshot(self.bin)
            p = self.run_install('--bin','all','--only','test','--force','--dry-run')
            self.assertEqual(p.returncode, 0, p.stderr.decode())
            self.assertEqual(snapshot(self.bin), before)
            p = self.run_install('--bin','all','--only','test','--force')
            self.assertEqual(p.returncode, 0, p.stderr.decode())
            self.assertEqual(os.readlink(shim), 'asha')
        shim.unlink()
        shim.write_bytes(b'foreign executable\n')
        shim.chmod(0o750)
        before = snapshot(self.bin)
        p = self.run_install('--bin','all','--only','test','--force')
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        self.assertEqual(snapshot(self.bin), before)

    def test_unknown_consumer_blocks_shared_redirect_even_with_bin_all(self):
        self.route(self.home/'old/bin/asha')
        (self.bin/'custom-wrapper').symlink_to('asha')
        before = snapshot(self.bin), self.cfg.read_bytes()
        p = self.run_install('--bin','all','--force','--only','test')
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        self.assertIn(b'launcher routing refused', p.stderr)
        self.assertEqual((snapshot(self.bin), self.cfg.read_bytes()), before)

    def test_unknown_default_consumers_are_not_authorized_by_known_bin_all(self):
        self.route()
        for name in ('custom-wrapper', '.custom-wrapper', '..custom-wrapper', 'asha-unknown'):
            alias = self.bin/name
            alias.symlink_to('asha')
            try:
                for flags in ((), ('--force',), ('--force', '--dry-run')):
                    with self.subTest(name=name, flags=flags):
                        before = snapshot(self.bin), snapshot(self.home/'.codex'), snapshot(self.home/'.asha')
                        p = self.run_install('--bin','all','--default','codex','--only','test', *flags)
                        self.assertEqual(p.returncode, 1, p.stderr.decode())
                        self.assertIn(b'default change would redirect a protected consumer', p.stderr)
                        # Identity bootstrap is outside launcher rollback scope;
                        # routing bytes/mode and the unattempted adapter are not.
                        self.assertEqual(snapshot(self.bin), before[0])
                        self.assertEqual(snapshot(self.home/'.codex'), before[1])
                        self.assertEqual(snapshot(self.home/'.asha')['config.json'], before[2]['config.json'])
                # A protected alias is compatible with an unchanged default.
                before = snapshot(self.bin), self.cfg.read_bytes(), self.cfg.stat().st_mode
                p = self.run_install('--bin','all','--default','claude','--only','test')
                self.assertEqual(p.returncode, 0, p.stderr.decode())
                self.assertEqual((snapshot(self.bin), self.cfg.read_bytes(), self.cfg.stat().st_mode), before)
            finally:
                alias.unlink()

    def test_hidden_consumers_block_stale_dispatcher_without_changing_shell_options(self):
        self.route(self.home/'old/bin/asha')
        for name in ('.custom-wrapper', '..custom-wrapper'):
            alias = self.bin/name
            alias.symlink_to('asha')
            try:
                for flags in (('--force',), ('--force', '--dry-run')):
                    with self.subTest(name=name, flags=flags):
                        before = snapshot(self.bin), self.cfg.read_bytes(), self.cfg.stat().st_mode
                        p = self.run_install('--bin','all','--only','test', *flags)
                        self.assertEqual(p.returncode, 1, p.stderr.decode())
                        self.assertIn(b'dispatcher change would redirect a protected consumer', p.stderr)
                        self.assertEqual((snapshot(self.bin), self.cfg.read_bytes(), self.cfg.stat().st_mode), before)
                p = self.source('DRY_RUN=0; FORCE=1; VERBOSE=0; '
                    'for mode in -u -s; do shopt "$mode" dotglob; before=$(shopt -p); '
                    'source "$1/lib/installer-launchers.sh"; '
                    'if install_bin all; then exit 90; else rc=$?; fi; '
                    '[[ $rc == 1 && "$(shopt -p)" == "$before" ]] || exit 91; done')
                self.assertEqual(p.returncode, 0, p.stderr.decode())
            finally:
                alias.unlink()

unittest.main(argv=['u8-install'], verbosity=2)
PY_U8_INSTALL
then ok "U8 parent launcher request/failure/source/routing matrix"
else fail "U8 parent launcher request/failure/source/routing matrix"
fi

echo "=== Install Test Summary ==="
echo -e "Passed: ${GREEN}$PASS${NC}"
echo -e "Failed: ${RED}$FAIL${NC}"

[[ $FAIL -eq 0 ]]
