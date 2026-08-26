#!/usr/bin/env bash
# A completely bare Asha launch enters the stable operator seat. Explicit
# harness selection and passthrough arguments preserve the caller's cwd.
set -euo pipefail

# Sandbox hermeticity: an operator shell exporting these must not leak in.
unset ASHA_HOME XDG_STATE_HOME XDG_DATA_HOME ASHA_SEAT 2>/dev/null || true

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
SCRATCH="$WORK/caller"
CHAIR_DIR="$HOME_DIR/.asha/chair"
CAPTURE="$WORK/argv"
ENVCAP="$WORK/env"
mkdir -p "$HOME_DIR/.asha/cache" "$HOME_DIR/bin" "$SCRATCH"
printf 'SOUL\n' >"$HOME_DIR/.asha/soul.md"
printf 'VOICE\n' >"$HOME_DIR/.asha/voice.md"
printf 'KEEPER\n' >"$HOME_DIR/.asha/keeper.md"
printf 'OPERATION RULES\n' >"$HOME_DIR/.asha/operation.md"

# Satisfy the launcher's freshness gate: native config plus one skill symlink
# into this checkout for each harness home used by the suite.
for home in "$HOME_DIR/.claude" "$HOME_DIR/.codex"; do
  mkdir -p "$home/skills"
  ln -s "$REPO_ROOT/plugins/test" "$home/skills/test-fixture"
done
printf '{}\n' >"$HOME_DIR/.claude/settings.json"
printf '\n' >"$HOME_DIR/.codex/config.toml"

for harness in claude codex; do
  cat >"$HOME_DIR/bin/$harness" <<'EOF'
#!/usr/bin/env bash
printf '%s\0' "$@" >"$ASHA_TEST_CAPTURE"
env >"$ASHA_TEST_ENV"
EOF
  chmod +x "$HOME_DIR/bin/$harness"
done

run_asha() {
  local cwd="$1"
  shift
  rm -f "$CAPTURE" "$ENVCAP"
  if (cd "$cwd" && env -u ASHA_HOME -u ASHA_SEAT -u ASHA_PERSONA \
      -u ASHA_CONTROL_MANAGED -u ASHA_COORDINATOR_LAUNCH \
      -u ASHA_ORCHESTRATOR_STANCE -u ASHA_ORCHESTRATOR_BRIEF_FILE \
      -u ASHA_CONFIG -u ASHA_INSTRUCTIONS_FILE -u ASHA_CLAUDE_INSTRUCTIONS_FILE \
      HOME="$HOME_DIR" PATH="$HOME_DIR/bin:$PATH" \
      ASHA_CLAUDE_CMD="$HOME_DIR/bin/claude" ASHA_CODEX_CMD="$HOME_DIR/bin/codex" \
      ASHA_TEST_CAPTURE="$CAPTURE" ASHA_TEST_ENV="$ENVCAP" \
      bash "$DISPATCHER" "$@" >/dev/null 2>"$WORK/stderr"); then
    LAST_STATUS=0
  else
    LAST_STATUS=$?
  fi
}

env_value() { sed -n "s/^$1=//p" "$ENVCAP" 2>/dev/null | head -1; }
env_has() { grep -q "^$1=" "$ENVCAP" 2>/dev/null; }

argv_ends_with() {
  local expected_first="$1" expected_second="$2"
  local -a actual=()
  mapfile -d '' -t actual <"$CAPTURE"
  local count="${#actual[@]}"
  [[ $count -ge 2 \
     && "${actual[$((count - 2))]}" == "$expected_first" \
     && "${actual[$((count - 1))]}" == "$expected_second" ]]
}

codex_has_chair_trust() {
  local expected="projects={\"$CHAIR_DIR\"={trust_level=\"trusted\"}}"
  local previous="" arg
  while IFS= read -r -d '' arg; do
    if [[ "$previous" == "-c" && "$arg" == "$expected" ]]; then
      return 0
    fi
    previous="$arg"
  done <"$CAPTURE"
  return 1
}

argv_has_exact() {
  local expected="$1" arg
  while IFS= read -r -d '' arg; do
    [[ "$arg" == "$expected" ]] && return 0
  done <"$CAPTURE"
  return 1
}

printf '{"default_harness": "claude"}\n' >"$HOME_DIR/.asha/config.json"
run_asha "$SCRATCH"
if [[ $LAST_STATUS -eq 0 \
   && "$(env_value PWD)" == "$CHAIR_DIR" \
   && "$(env_value ASHA_SEAT)" == "1" \
   && -d "$CHAIR_DIR" \
   && "$(stat -c '%a' "$CHAIR_DIR")" == "700" ]]; then
  ok "bare launch creates and enters the mode-700 seat"
else
  fail "bare launch creates and enters the mode-700 seat (status=$LAST_STATUS; pwd=$(env_value PWD); seat=$(env_value ASHA_SEAT); stderr=$(cat "$WORK/stderr"))"
fi

run_asha "$SCRATCH" claude
if [[ $LAST_STATUS -eq 0 \
   && "$(env_value PWD)" == "$SCRATCH" ]] \
   && ! env_has ASHA_SEAT; then
  ok "explicit harness launch preserves the caller cwd without ASHA_SEAT"
else
  fail "explicit harness launch preserves the caller cwd without ASHA_SEAT (status=$LAST_STATUS; pwd=$(env_value PWD); stderr=$(cat "$WORK/stderr"))"
fi

run_asha "$SCRATCH" -p hi
if [[ $LAST_STATUS -eq 0 \
   && "$(env_value PWD)" == "$SCRATCH" ]] \
   && ! env_has ASHA_SEAT \
   && argv_ends_with -p hi; then
  ok "default harness passthrough arguments preserve the caller cwd and argv"
else
  fail "default harness passthrough arguments preserve the caller cwd and argv (status=$LAST_STATUS; pwd=$(env_value PWD); stderr=$(cat "$WORK/stderr"))"
fi

printf '{"default_harness": "codex"}\n' >"$HOME_DIR/.asha/config.json"
run_asha "$SCRATCH"
if [[ $LAST_STATUS -eq 0 \
   && "$(env_value PWD)" == "$CHAIR_DIR" ]] \
   && codex_has_chair_trust \
   && ! argv_has_exact -a \
   && ! argv_has_exact never \
   && ! argv_has_exact --sandbox; then
  ok "bare Codex seat trusts the chair without coordinator posture"
else
  fail "bare Codex seat trusts the chair without coordinator posture (status=$LAST_STATUS; pwd=$(env_value PWD); stderr=$(cat "$WORK/stderr"))"
fi

printf '{"default_harness": "claude"}\n' >"$HOME_DIR/.asha/config.json"
mkdir -p "$CHAIR_DIR"
chmod 750 "$CHAIR_DIR"
run_asha "$SCRATCH"
if [[ $LAST_STATUS -eq 0 \
   && "$(env_value PWD)" == "$CHAIR_DIR" \
   && "$(stat -c '%a' "$CHAIR_DIR")" == "750" ]]; then
  ok "pre-existing seat keeps its customized mode"
else
  fail "pre-existing seat keeps its customized mode (status=$LAST_STATUS; pwd=$(env_value PWD); mode=$(stat -c '%a' "$CHAIR_DIR"); stderr=$(cat "$WORK/stderr"))"
fi

echo "test-seat: $PASS passed, $FAIL failed"
[[ $PASS -eq 5 && $FAIL -eq 0 ]]
