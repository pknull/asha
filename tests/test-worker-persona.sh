#!/usr/bin/env bash
# Control-managed workers launch without the persona: ASHA_PERSONA=0 skips the
# identity render on every harness wrapper while keeping the operational layer
# (where file-based) and the Codex trust override. Unset or any other value
# keeps the full persona.
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
  # $1 harness, $2 persona value ("" = unset), remaining: extra env assignments
  local harness="$1" persona="$2"
  shift 2
  local -a envargs=(
    HOME="$HOME_DIR" PATH="$HOME_DIR/bin:$PATH"
    ASHA_CLAUDE_CMD="$HOME_DIR/bin/claude" ASHA_CODEX_CMD="$HOME_DIR/bin/codex"
    ASHA_COPILOT_CMD="$HOME_DIR/bin/copilot" ASHA_OPENCODE_CMD="$HOME_DIR/bin/opencode"
    ASHA_TEST_CAPTURE="$CAPTURE" ASHA_TEST_ENV="$ENVCAP" "$@"
  )
  rm -f "$CAPTURE" "$ENVCAP"
  if [[ -n "$persona" ]]; then
    envargs+=(ASHA_PERSONA="$persona")
    (cd "$WORK" && env "${envargs[@]}" bash "$DISPATCHER" "$harness" PAYLOAD >/dev/null 2>"$WORK/stderr") || true
  else
    (cd "$WORK" && env -u ASHA_PERSONA "${envargs[@]}" bash "$DISPATCHER" "$harness" PAYLOAD >/dev/null 2>"$WORK/stderr") || true
  fi
}

argv_joined() { tr '\0' '\n' <"$CAPTURE" 2>/dev/null || true; }
env_value() { sed -n "s/^$1=//p" "$ENVCAP" 2>/dev/null | head -1; }

# --- claude ---
run_harness claude 0
if [[ -s "$CAPTURE" ]] && ! argv_joined | grep -q -- '--append-system-prompt-file'; then
  ok "claude ASHA_PERSONA=0 launches without --append-system-prompt-file"
else
  fail "claude ASHA_PERSONA=0 launches without --append-system-prompt-file ($(argv_joined | tr '\n' ' '))"
fi
run_harness claude ""
if argv_joined | grep -q -- '--append-system-prompt-file'; then
  ok "claude default keeps the persona flag"
else
  fail "claude default keeps the persona flag ($(argv_joined | tr '\n' ' '); stderr: $(cat "$WORK/stderr"))"
fi
run_harness claude 1
if argv_joined | grep -q -- '--append-system-prompt-file'; then
  ok "claude ASHA_PERSONA=1 keeps the persona flag"
else
  fail "claude ASHA_PERSONA=1 keeps the persona flag"
fi

# --- codex ---
run_harness codex 0 ASHA_CONTROL_MANAGED=1
CODEX_ARGS="$(argv_joined | tr '\n' ' ')"
if [[ -s "$CAPTURE" ]] && ! grep -q 'instructions-codex\.md\|instructions\.md' <<<"$CODEX_ARGS" \
   && grep -q 'projects=' <<<"$CODEX_ARGS"; then
  ok "codex ASHA_PERSONA=0 keeps the trust override and drops the identity file"
else
  fail "codex ASHA_PERSONA=0 keeps the trust override and drops the identity file ($CODEX_ARGS)"
fi
if grep -q 'instructions-codex-operational\.md' <<<"$CODEX_ARGS"; then
  ok "codex ASHA_PERSONA=0 carries the operational layer only"
else
  fail "codex ASHA_PERSONA=0 carries the operational layer only ($CODEX_ARGS)"
fi
run_harness codex "" ASHA_CONTROL_MANAGED=1
if argv_joined | grep -q 'instructions-codex\.md'; then
  ok "codex default keeps the identity model_instructions_file"
else
  fail "codex default keeps the identity model_instructions_file ($(argv_joined | tr '\n' ' '); stderr: $(cat "$WORK/stderr"))"
fi

# --- copilot ---
run_harness copilot 0
COPILOT_DIRS="$(env_value COPILOT_CUSTOM_INSTRUCTIONS_DIRS)"
if [[ -s "$CAPTURE" ]] && [[ -z "$COPILOT_DIRS" || "$COPILOT_DIRS" == *copilot-instr-worker* ]] \
   && [[ ! -f "$HOME_DIR/.asha/cache/copilot-instr-worker/.github/instructions/asha.instructions.md" ]]; then
  ok "copilot ASHA_PERSONA=0 never writes the identity instructions file"
else
  fail "copilot ASHA_PERSONA=0 never writes the identity instructions file (dirs=$COPILOT_DIRS)"
fi
if [[ -f "$HOME_DIR/.asha/cache/copilot-instr-worker/.github/instructions/asha-operational.instructions.md" ]]; then
  ok "copilot ASHA_PERSONA=0 carries the operational layer"
else
  fail "copilot ASHA_PERSONA=0 carries the operational layer"
fi
run_harness copilot ""
if [[ "$(env_value COPILOT_CUSTOM_INSTRUCTIONS_DIRS)" == *copilot-instr* ]] \
   && [[ -f "$HOME_DIR/.asha/cache/copilot-instr/.github/instructions/asha.instructions.md" ]]; then
  ok "copilot default writes the identity instructions file"
else
  fail "copilot default writes the identity instructions file (stderr: $(cat "$WORK/stderr"))"
fi

# --- opencode ---
if command -v jq >/dev/null 2>&1; then
  run_harness opencode 0
  OC_CFG="$(env_value OPENCODE_CONFIG_CONTENT)"
  if [[ -s "$CAPTURE" ]] && ! grep -q 'instructions-opencode\.md\|instructions-opencode-combined' <<<"$OC_CFG"; then
    ok "opencode ASHA_PERSONA=0 drops the identity instructions"
  else
    fail "opencode ASHA_PERSONA=0 drops the identity instructions ($OC_CFG)"
  fi
  if grep -q 'instructions-opencode-operational\.md' <<<"$OC_CFG"; then
    ok "opencode ASHA_PERSONA=0 carries the operational layer"
  else
    fail "opencode ASHA_PERSONA=0 carries the operational layer ($OC_CFG; stderr: $(cat "$WORK/stderr"))"
  fi
else
  echo "  - jq missing; opencode persona checks skipped"
fi

echo "test-worker-persona: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
