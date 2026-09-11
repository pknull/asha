#!/usr/bin/env bash
# Explicit session profiles decide what a launch carries.
#
#   ASHA_SESSION_PROFILE=chair   persona + operational layer + chair stance
#   ASHA_SESSION_PROFILE=room    persona + operational layer, stance off
#   ASHA_SESSION_PROFILE=worker  the native harness alone: no identity render,
#                                no operational layer, no chair stance
#
# The profile is the launcher's explicit statement and outranks ASHA_PERSONA
# (Control sets ASHA_PERSONA=1 on every room pane, workers included). An absent
# or unrecognized profile is legacy: ASHA_PERSONA and the stance decide alone.
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
# The chair brief is owned elsewhere and rewritten often; assert against a
# fixture through the launcher's documented override so these cases test the
# profile decision rather than someone else's prose.
CHAIR_BRIEF="$HOME_DIR/.asha/chair-brief.md"
printf 'CHAIR-BRIEF-SENTINEL\n' >"$CHAIR_BRIEF"
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

_run_dispatcher() {
  local harness="$1" payload="$2"
  shift 3   # harness, payload, and the -- separator
  local -a envargs=(
    HOME="$HOME_DIR" PATH="$HOME_DIR/bin:$PATH"
    ASHA_CLAUDE_CMD="$HOME_DIR/bin/claude" ASHA_CODEX_CMD="$HOME_DIR/bin/codex"
    ASHA_COPILOT_CMD="$HOME_DIR/bin/copilot" ASHA_OPENCODE_CMD="$HOME_DIR/bin/opencode"
    ASHA_ORCHESTRATOR_BRIEF_FILE="$CHAIR_BRIEF"
    ASHA_TEST_CAPTURE="$CAPTURE" ASHA_TEST_ENV="$ENVCAP" "$@"
  )
  local -a argv=("$harness")
  [[ -z "$payload" ]] || argv+=("$payload")
  rm -f "$CAPTURE" "$ENVCAP"
  (cd "$WORK" && env -u ASHA_PERSONA -u ASHA_SESSION_PROFILE \
      -u ASHA_COORDINATOR_LAUNCH -u ASHA_ORCHESTRATOR_STANCE \
      -u ASHA_CONFIG -u ASHA_SEAT \
      -u ASHA_INSTRUCTIONS_FILE -u ASHA_CLAUDE_INSTRUCTIONS_FILE \
      -u ASHA_COPILOT_INSTRUCTIONS_FILE -u ASHA_COPILOT_INSTR_DIR \
      -u ASHA_OPENCODE_INSTRUCTIONS_FILE -u COPILOT_CUSTOM_INSTRUCTIONS_DIRS \
      -u OPENCODE_CONFIG_CONTENT -u ASHA_CONTROL_MANAGED -u ASHA_ROOM_ID \
      "${envargs[@]}" bash "$DISPATCHER" "${argv[@]}" \
      >/dev/null 2>"$WORK/stderr") || true
}

# $1 harness, rest: env assignments. A PAYLOAD argument keeps the caller's cwd
# so the seat branch stays out of the way of the profile assertions.
run_harness() {
  local harness="$1"
  shift
  _run_dispatcher "$harness" PAYLOAD -- "$@"
}

# No-argument launch: the only shape that can reach the chair seat.
run_harness_bare() {
  local harness="$1"
  shift
  _run_dispatcher "$harness" "" -- "$@"
}

argv_joined() { tr '\0' '\n' <"$CAPTURE" 2>/dev/null || true; }
env_value() { sed -n "s/^$1=//p" "$ENVCAP" 2>/dev/null | head -1; }
launched() { [[ -s "$ENVCAP" ]]; }

instructions_file() {
  local arg previous=""
  while IFS= read -r -d '' arg; do
    if [[ "$previous" == "--append-system-prompt-file" ]]; then
      printf '%s\n' "$arg"; return 0
    fi
    if [[ "$arg" =~ ^model_instructions_file=\"(.*)\"$ ]]; then
      printf '%s\n' "${BASH_REMATCH[1]}"; return 0
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

copilot_wired_files() {
  local dirs first
  dirs="$(env_value COPILOT_CUSTOM_INSTRUCTIONS_DIRS)"
  [[ -n "$dirs" ]] || return 0
  first="${dirs%%,*}"
  ls "$first/.github/instructions/" 2>/dev/null || true
}

opencode_instructions() {
  env_value OPENCODE_CONFIG_CONTENT | jq -r '(.instructions // [])[]' 2>/dev/null || true
}

echo "--- worker profile: the native harness alone ---"

# ASHA_PERSONA=1 is exactly what Control writes into every room pane. The
# explicit worker profile has to win, or every hub worker inherits the persona.
run_harness claude ASHA_SESSION_PROFILE=worker ASHA_PERSONA=1
if launched && ! argv_joined | grep -q -- '--append-system-prompt-file'; then
  ok "claude worker launches plain despite ASHA_PERSONA=1"
else
  fail "claude worker launches plain despite ASHA_PERSONA=1 ($(argv_joined | tr '\n' ' '); stderr: $(cat "$WORK/stderr"))"
fi

# The hooks read the same variable, so the launch has to hand it on unchanged.
if [[ "$(env_value ASHA_SESSION_PROFILE)" == worker ]]; then
  ok "the harness inherits the profile the launcher acted on"
else
  fail "the harness inherits the profile the launcher acted on ($(env_value ASHA_SESSION_PROFILE))"
fi

run_harness codex ASHA_SESSION_PROFILE=worker ASHA_PERSONA=1 ASHA_ROOM_ID=fixture-room
CODEX_ARGS="$(argv_joined | tr '\n' ' ')"
if launched && ! grep -qE 'model_instructions_file|trust_level|approval_policy|sandbox_mode' <<<"$CODEX_ARGS"; then
  ok "codex worker preserves native trust and policy without Asha instructions"
else
  fail "codex worker carries neither identity nor operational instructions ($CODEX_ARGS; stderr: $(cat "$WORK/stderr"))"
fi

run_harness copilot ASHA_SESSION_PROFILE=worker ASHA_PERSONA=1
COPILOT_WIRED="$(copilot_wired_files)"
if launched && [[ -z "$COPILOT_WIRED" ]]; then
  ok "copilot worker wires no Asha instructions directory"
else
  fail "copilot worker wires no Asha instructions directory ($COPILOT_WIRED; stderr: $(cat "$WORK/stderr"))"
fi

if command -v jq >/dev/null 2>&1; then
  run_harness opencode ASHA_SESSION_PROFILE=worker ASHA_PERSONA=1
  OC_INSTR="$(opencode_instructions)"
  if launched && [[ -z "$OC_INSTR" ]]; then
    ok "opencode worker adds no Asha instructions file"
  else
    fail "opencode worker adds no Asha instructions file ($OC_INSTR; stderr: $(cat "$WORK/stderr"))"
  fi
else
  echo "  - jq missing; opencode worker check skipped"
fi

# The profile has to be strictly lighter than the legacy ASHA_PERSONA=0
# worker, which still receives the operational layer where it is file-based.
run_harness codex ASHA_PERSONA=0
if argv_joined | grep -q 'instructions-codex-operational\.md'; then
  ok "legacy codex ASHA_PERSONA=0 still carries the operational layer"
else
  fail "legacy codex ASHA_PERSONA=0 still carries the operational layer ($(argv_joined | tr '\n' ' '))"
fi

run_harness copilot ASHA_PERSONA=0
if [[ "$(copilot_wired_files)" == *asha-operational.instructions.md* ]]; then
  ok "legacy copilot ASHA_PERSONA=0 still carries the operational layer"
else
  fail "legacy copilot ASHA_PERSONA=0 still carries the operational layer"
fi

echo "--- room profile: persona and project memory, stance off ---"

run_harness claude ASHA_SESSION_PROFILE=room
if instructions_contain 'SOUL' && ! instructions_contain "CHAIR-BRIEF-SENTINEL"; then
  ok "claude room keeps the identity render without the chair stance"
else
  fail "claude room keeps the identity render without the chair stance (stderr: $(cat "$WORK/stderr"))"
fi

run_harness codex ASHA_SESSION_PROFILE=room
if instructions_contain 'SOUL' && instructions_contain 'OPERATION RULES' \
   && ! instructions_contain "CHAIR-BRIEF-SENTINEL"; then
  ok "codex room keeps identity and operations without the chair stance"
else
  fail "codex room keeps identity and operations without the chair stance (stderr: $(cat "$WORK/stderr"))"
fi

if command -v jq >/dev/null 2>&1; then
  run_harness opencode ASHA_SESSION_PROFILE=room
  OC_FILE="$(opencode_instructions | tail -1)"
  if [[ -n "$OC_FILE" && -f "$OC_FILE" ]] && grep -Fq 'SOUL' "$OC_FILE" \
     && ! grep -Fq "CHAIR-BRIEF-SENTINEL" "$OC_FILE"; then
    ok "opencode room keeps the identity render without the chair stance"
  else
    fail "opencode room keeps the identity render without the chair stance ($OC_FILE)"
  fi
fi

echo "--- chair profile: persona, operations and stance ---"

run_harness claude ASHA_SESSION_PROFILE=chair
if instructions_contain 'SOUL' && instructions_contain "CHAIR-BRIEF-SENTINEL"; then
  ok "claude chair keeps identity and the chair brief"
else
  fail "claude chair keeps identity and the chair brief (stderr: $(cat "$WORK/stderr"))"
fi

run_harness codex ASHA_SESSION_PROFILE=chair
if instructions_contain 'SOUL' && instructions_contain "CHAIR-BRIEF-SENTINEL"; then
  ok "codex chair keeps identity and the chair brief"
else
  fail "codex chair keeps identity and the chair brief (stderr: $(cat "$WORK/stderr"))"
fi

echo "--- default and unknown values stay backward compatible ---"

run_harness claude
if instructions_contain 'SOUL' && instructions_contain "CHAIR-BRIEF-SENTINEL"; then
  ok "absent profile keeps today's wrapped launch unchanged"
else
  fail "absent profile keeps today's wrapped launch unchanged (stderr: $(cat "$WORK/stderr"))"
fi

run_harness claude ASHA_SESSION_PROFILE=nonsense
if instructions_contain 'SOUL' && instructions_contain "CHAIR-BRIEF-SENTINEL" \
   && grep -q 'ASHA_SESSION_PROFILE' "$WORK/stderr" \
   && ! grep -qx 'ASHA_SESSION_PROFILE=nonsense' "$ENVCAP"; then
  ok "unknown profile warns, is never handed to the harness, and falls back to legacy"
else
  fail "unknown profile warns, is never handed to the harness, and falls back to legacy (stderr: $(cat "$WORK/stderr"))"
fi

echo "--- only the chair may take the seat ---"

run_harness_bare claude ASHA_SESSION_PROFILE=worker
if launched && [[ -z "$(env_value ASHA_SEAT)" ]]; then
  ok "a bare worker launch never takes the chair seat"
else
  fail "a bare worker launch never takes the chair seat (seat=$(env_value ASHA_SEAT); stderr: $(cat "$WORK/stderr"))"
fi

run_harness_bare claude ASHA_SESSION_PROFILE=room
if launched && [[ -z "$(env_value ASHA_SEAT)" ]]; then
  ok "a bare room launch never takes the chair seat"
else
  fail "a bare room launch never takes the chair seat (seat=$(env_value ASHA_SEAT); stderr: $(cat "$WORK/stderr"))"
fi

run_harness_bare claude
if launched && [[ "$(env_value ASHA_SEAT)" == "1" ]]; then
  ok "a bare default launch still takes the chair seat"
else
  fail "a bare default launch still takes the chair seat (seat=$(env_value ASHA_SEAT); stderr: $(cat "$WORK/stderr"))"
fi

plain_home="$WORK/plain-native-home"
mkdir -p "$plain_home"
for native in claude codex copilot opencode; do
  run_harness "$native" ASHA_SESSION_PROFILE=worker HOME="$plain_home"
  if launched; then
    ok "$native worker does not require Asha configuration in its native home"
  else
    fail "$native worker incorrectly requires Asha setup ($(cat "$WORK/stderr"))"
  fi
done

echo "test-session-profiles: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
