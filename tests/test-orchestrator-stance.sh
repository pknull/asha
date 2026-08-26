#!/usr/bin/env bash
# Wrapped persona launches receive the orchestrator's chair brief by
# default. Control coordinators and persona-free workers do not, and explicit
# environment settings override the user config.
set -euo pipefail

# Sandbox hermeticity: an operator shell exporting these must not leak in.
unset ASHA_HOME XDG_STATE_HOME XDG_DATA_HOME 2>/dev/null || true

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
DISPATCHER="$REPO_ROOT/bin/asha"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
ok() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }

HOME_DIR="$WORK/home"
CAPTURE="$WORK/argv"
ENVCAP="$WORK/env"
mkdir -p "$HOME_DIR/.asha" "$HOME_DIR/bin" "$HOME_DIR/.asha/cache"
printf 'SOUL\n' >"$HOME_DIR/.asha/soul.md"
printf 'VOICE\n' >"$HOME_DIR/.asha/voice.md"
printf 'KEEPER\n' >"$HOME_DIR/.asha/keeper.md"
printf 'OPERATION RULES\n' >"$HOME_DIR/.asha/operation.md"
printf '{}\n' >"$HOME_DIR/.asha/config.json"
# Satisfy the launcher's freshness gate: native config where required, and one
# skill symlink into this checkout so each harness counts as configured.
for home in "$HOME_DIR/.claude" "$HOME_DIR/.codex" "$HOME_DIR/.copilot" "$HOME_DIR/.config/opencode"; do
  mkdir -p "$home/skills"
  ln -s "$REPO_ROOT/plugins/test" "$home/skills/test-fixture"
done
printf '{}\n' >"$HOME_DIR/.claude/settings.json"
printf '\n' >"$HOME_DIR/.codex/config.toml"

for harness in claude codex copilot opencode; do
  cat >"$HOME_DIR/bin/$harness" <<'EOF'
#!/usr/bin/env bash
# The OpenCode wrapper probes `--version` before launching; answer it.
if [[ "${1:-}" == "--version" ]]; then echo "1.20.0"; exit 0; fi
printf '%s\0' "$@" >"$ASHA_TEST_CAPTURE"
env >"$ASHA_TEST_ENV"
EOF
  chmod +x "$HOME_DIR/bin/$harness"
done

run_harness() {
  local harness="$1"
  shift
  local -a envargs=(
    HOME="$HOME_DIR" PATH="$HOME_DIR/bin:$PATH"
    ASHA_CLAUDE_CMD="$HOME_DIR/bin/claude" ASHA_CODEX_CMD="$HOME_DIR/bin/codex"
    ASHA_COPILOT_CMD="$HOME_DIR/bin/copilot" ASHA_OPENCODE_CMD="$HOME_DIR/bin/opencode"
    ASHA_TEST_CAPTURE="$CAPTURE" ASHA_TEST_ENV="$ENVCAP" "$@"
  )
  rm -f "$CAPTURE" "$ENVCAP"
  if (cd "$WORK" && env -u ASHA_PERSONA -u ASHA_COORDINATOR_LAUNCH \
      -u ASHA_ORCHESTRATOR_STANCE -u ASHA_ORCHESTRATOR_BRIEF_FILE \
      -u ASHA_CONFIG -u ASHA_INSTRUCTIONS_FILE -u ASHA_CLAUDE_INSTRUCTIONS_FILE \
      -u ASHA_COPILOT_INSTRUCTIONS_FILE -u ASHA_COPILOT_INSTR_DIR \
      -u ASHA_OPENCODE_INSTRUCTIONS_FILE -u COPILOT_CUSTOM_INSTRUCTIONS_DIRS \
      -u OPENCODE_CONFIG_CONTENT \
      "${envargs[@]}" bash "$DISPATCHER" "$harness" PAYLOAD \
      >/dev/null 2>"$WORK/stderr"); then
    LAST_STATUS=0
  else
    LAST_STATUS=$?
  fi
}

argv_joined() { tr '\0' '\n' <"$CAPTURE" 2>/dev/null || true; }
env_value() { sed -n "s/^$1=//p" "$ENVCAP" 2>/dev/null | head -1; }

instructions_file() {
  local arg previous=""
  while IFS= read -r -d '' arg; do
    if [[ "$previous" == "--append-system-prompt-file" ]]; then
      printf '%s\n' "$arg"
      return 0
    fi
    if [[ "$arg" =~ ^model_instructions_file=\"(.*)\"$ ]]; then
      printf '%s\n' "${BASH_REMATCH[1]}"
      return 0
    fi
    previous="$arg"
  done <"$CAPTURE"
  return 1
}

instructions_contain() {
  local marker="$1" file
  file="$(instructions_file)" || return 1
  [[ -f "$file" ]] && grep -Fq "$marker" "$file"
}

copilot_instructions_dir() {
  local dirs
  dirs="$(env_value COPILOT_CUSTOM_INSTRUCTIONS_DIRS)"
  printf '%s\n' "${dirs%%,*}"
}

opencode_instructions_file() {
  env_value OPENCODE_CONFIG_CONTENT | jq -r '.instructions[-1] // empty'
}

run_harness claude
if instructions_contain 'SOUL' \
   && instructions_contain "orchestrator's chair" \
   && ! grep -Fq "orchestrator's chair" "$HOME_DIR/.asha/cache/instructions.md"; then
  ok "claude default adds the chair brief without changing canonical identity"
else
  fail "claude default adds the chair brief without changing canonical identity (stderr: $(cat "$WORK/stderr"))"
fi

run_harness codex
if instructions_contain 'SOUL' && instructions_contain "orchestrator's chair"; then
  ok "codex default instructions contain identity and the chair brief"
else
  fail "codex default instructions contain identity and the chair brief (stderr: $(cat "$WORK/stderr"))"
fi

run_harness claude ASHA_COORDINATOR_LAUNCH=tok123
if instructions_contain 'SOUL' && ! instructions_contain "orchestrator's chair"; then
  ok "claude coordinator launch omits the chair brief"
else
  fail "claude coordinator launch omits the chair brief (stderr: $(cat "$WORK/stderr"))"
fi

run_harness codex ASHA_COORDINATOR_LAUNCH=tok123
if instructions_contain 'SOUL' && ! instructions_contain "orchestrator's chair"; then
  ok "codex coordinator launch omits the chair brief"
else
  fail "codex coordinator launch omits the chair brief (stderr: $(cat "$WORK/stderr"))"
fi

run_harness claude ASHA_PERSONA=0
if [[ -s "$CAPTURE" ]] && ! argv_joined | grep -q -- '--append-system-prompt-file'; then
  ok "claude ASHA_PERSONA=0 keeps the worker launch path unchanged"
else
  fail "claude ASHA_PERSONA=0 keeps the worker launch path unchanged ($(argv_joined | tr '\n' ' '))"
fi

run_harness codex ASHA_PERSONA=0
codex_worker_file="$(instructions_file || true)"
if [[ $LAST_STATUS -eq 0 && -s "$CAPTURE" && "$codex_worker_file" != *chair* ]] \
   && ! argv_joined | grep -Eq 'model_instructions_file=.*chair' \
   && { [[ -z "$codex_worker_file" ]] \
        || { [[ -f "$codex_worker_file" ]] \
             && ! grep -Fq 'SOUL' "$codex_worker_file" \
             && ! grep -Fq "orchestrator's chair" "$codex_worker_file"; }; }; then
  ok "codex ASHA_PERSONA=0 uses no chair or identity instructions"
else
  fail "codex ASHA_PERSONA=0 uses no chair or identity instructions ($(argv_joined | tr '\n' ' '))"
fi

printf '{"orchestrator_stance": false}\n' >"$HOME_DIR/.asha/config.json"
run_harness claude
if instructions_contain 'SOUL' && ! instructions_contain "orchestrator's chair"; then
  ok "claude config false disables the chair brief"
else
  fail "claude config false disables the chair brief (stderr: $(cat "$WORK/stderr"))"
fi
printf '{}\n' >"$HOME_DIR/.asha/config.json"

printf '{"orchestrator_stance": false}\n' >"$HOME_DIR/.asha/config.json"
run_harness claude ASHA_ORCHESTRATOR_STANCE=1
if instructions_contain "orchestrator's chair"; then
  ok "claude stance environment enable overrides config false"
else
  fail "claude stance environment enable overrides config false (stderr: $(cat "$WORK/stderr"))"
fi
printf '{}\n' >"$HOME_DIR/.asha/config.json"

run_harness claude ASHA_ORCHESTRATOR_STANCE=0
if instructions_contain 'SOUL' && ! instructions_contain "orchestrator's chair"; then
  ok "claude stance environment disable overrides default config"
else
  fail "claude stance environment disable overrides default config (stderr: $(cat "$WORK/stderr"))"
fi

run_harness claude ASHA_COORDINATOR_LAUNCH=tok123 ASHA_ORCHESTRATOR_STANCE=1
if instructions_contain 'SOUL' && ! instructions_contain "orchestrator's chair"; then
  ok "claude coordinator guard overrides forced chair stance"
else
  fail "claude coordinator guard overrides forced chair stance (stderr: $(cat "$WORK/stderr"))"
fi

run_harness claude ASHA_ORCHESTRATOR_BRIEF_FILE=/nonexistent
if [[ $LAST_STATUS -eq 0 && -s "$CAPTURE" ]] \
   && instructions_contain 'SOUL' \
   && ! instructions_contain "orchestrator's chair"; then
  ok "claude missing brief degrades to identity-only instructions"
else
  fail "claude missing brief degrades to identity-only instructions (status=$LAST_STATUS; stderr: $(cat "$WORK/stderr"))"
fi

claude_chair_path="$HOME_DIR/.asha/cache/instructions-claude-chair.md"
rm -f "$claude_chair_path"
mkdir "$claude_chair_path"
run_harness claude
if [[ $LAST_STATUS -eq 0 && -s "$CAPTURE" ]] \
   && [[ "$(instructions_file)" == "$HOME_DIR/.asha/cache/instructions.md" ]] \
   && instructions_contain 'SOUL' \
   && ! instructions_contain "orchestrator's chair"; then
  ok "claude chair render failure falls back to canonical identity"
else
  fail "claude chair render failure falls back to canonical identity (status=$LAST_STATUS; stderr: $(cat "$WORK/stderr"))"
fi
rmdir "$claude_chair_path"

run_harness codex
codex_chair_file="$(instructions_file)"
run_harness codex ASHA_COORDINATOR_LAUNCH=tok123
codex_coordinator_file="$(instructions_file)"
if [[ "$codex_chair_file" == *chair* && -f "$codex_chair_file" ]] \
   && grep -Fq "orchestrator's chair" "$codex_chair_file" \
   && [[ "$codex_coordinator_file" != *chair* && -f "$codex_coordinator_file" ]] \
   && ! grep -Fq "orchestrator's chair" "$codex_coordinator_file"; then
  ok "codex chair and coordinator launches use role-distinct files"
else
  fail "codex chair and coordinator launches use role-distinct files (chair=$codex_chair_file; coordinator=$codex_coordinator_file)"
fi

run_harness copilot
copilot_dir="$(copilot_instructions_dir)"
copilot_chair_file="$copilot_dir/.github/instructions/asha-chair.instructions.md"
if [[ -f "$copilot_chair_file" ]] \
   && grep -Fq "orchestrator's chair" "$copilot_chair_file"; then
  ok "copilot default adds the chair brief in its custom instructions directory"
else
  fail "copilot default adds the chair brief in its custom instructions directory (dir=$copilot_dir; stderr: $(cat "$WORK/stderr"))"
fi

run_harness copilot ASHA_COORDINATOR_LAUNCH=tok123
copilot_dir="$(copilot_instructions_dir)"
copilot_chair_file="$copilot_dir/.github/instructions/asha-chair.instructions.md"
if [[ -n "$copilot_dir" && ! -e "$copilot_chair_file" ]]; then
  ok "copilot coordinator launch removes the stale chair instructions file"
else
  fail "copilot coordinator launch removes the stale chair instructions file (dir=$copilot_dir; stderr: $(cat "$WORK/stderr"))"
fi

run_harness opencode
opencode_chair_file="$(opencode_instructions_file)"
if [[ "$opencode_chair_file" == *chair* && -f "$opencode_chair_file" ]] \
   && grep -Fq 'SOUL' "$opencode_chair_file" \
   && grep -Fq "orchestrator's chair" "$opencode_chair_file"; then
  ok "opencode default uses chair instructions containing identity and brief"
else
  fail "opencode default uses chair instructions containing identity and brief (file=$opencode_chair_file; stderr: $(cat "$WORK/stderr"))"
fi

run_harness opencode ASHA_ORCHESTRATOR_STANCE=0
opencode_combined_file="$(opencode_instructions_file)"
if [[ -f "$opencode_combined_file" && "$opencode_combined_file" != *chair* ]] \
   && grep -Fq 'SOUL' "$opencode_combined_file" \
   && ! grep -Fq "orchestrator's chair" "$opencode_combined_file"; then
  ok "opencode stance disable uses a brief-free non-chair instructions file"
else
  fail "opencode stance disable uses a brief-free non-chair instructions file (file=$opencode_combined_file; stderr: $(cat "$WORK/stderr"))"
fi

echo "test-orchestrator-stance: $PASS passed, $FAIL failed"
[[ $PASS -eq 17 && $FAIL -eq 0 ]]
