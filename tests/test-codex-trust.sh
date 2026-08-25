#!/usr/bin/env bash
# Control-managed Codex launches receive a per-launch workspace trust override.
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
CAPTURE="$WORK/codex.args"
mkdir -p "$HOME_DIR/.asha" "$HOME_DIR/.codex/skills" "$HOME_DIR/bin"
printf 'SOUL\n' >"$HOME_DIR/.asha/soul.md"
printf 'VOICE\n' >"$HOME_DIR/.asha/voice.md"
printf 'KEEPER\n' >"$HOME_DIR/.asha/keeper.md"
printf '\n' >"$HOME_DIR/.codex/config.toml"
ln -s "$REPO_ROOT/plugins/test" "$HOME_DIR/.codex/skills/test-fixture"

cat >"$HOME_DIR/bin/codex" <<'EOF'
#!/usr/bin/env bash
printf '%s\0' "$@" >"$ASHA_TEST_CAPTURE"
EOF
chmod +x "$HOME_DIR/bin/codex"

# Default persona launches carry the orchestrator stance, so the combined
# render is the chair file (identity + operational + brief).
MODEL_FILE="$HOME_DIR/.asha/cache/instructions-codex-chair.md"

run_codex() {
  local cwd="$1" managed="$2"
  if [[ "$managed" == 1 ]]; then
    (cd "$cwd" && HOME="$HOME_DIR" ASHA_CODEX_CMD="$HOME_DIR/bin/codex" \
      ASHA_TEST_CAPTURE="$CAPTURE" ASHA_CONTROL_MANAGED=1 \
      bash "$DISPATCHER" codex PAYLOAD >/dev/null 2>"$WORK/stderr")
  else
    (cd "$cwd" && env -u ASHA_CONTROL_MANAGED HOME="$HOME_DIR" \
      ASHA_CODEX_CMD="$HOME_DIR/bin/codex" ASHA_TEST_CAPTURE="$CAPTURE" \
      bash "$DISPATCHER" codex PAYLOAD >/dev/null 2>"$WORK/stderr")
  fi
}

assert_argv() {
  local label="$1"
  shift
  local -a actual
  mapfile -d '' -t actual <"$CAPTURE"
  if [[ ${#actual[@]} -ne $# ]]; then
    fail "$label (expected $# args, got ${#actual[@]})"
    return
  fi
  local index=0 expected
  for expected in "$@"; do
    if [[ "${actual[$index]}" != "$expected" ]]; then
      fail "$label (arg $((index + 1)): expected '$expected', got '${actual[$index]}')"
      return
    fi
    index=$((index + 1))
  done
  ok "$label"
}

GIT_ROOT="$WORK/git-root"
mkdir -p "$GIT_ROOT/nested/workspace"
git -C "$GIT_ROOT" init -q

echo "--- unmanaged launch ---"
run_codex "$GIT_ROOT/nested/workspace" 0
assert_argv "unmanaged argv is unchanged" \
  -c "model_instructions_file=\"$MODEL_FILE\"" PAYLOAD

echo "--- managed launch in a Git repository ---"
run_codex "$GIT_ROOT/nested/workspace" 1
assert_argv "managed argv trusts the Git toplevel" \
  -c "model_instructions_file=\"$MODEL_FILE\"" \
  -c "projects={\"$GIT_ROOT\"={trust_level=\"trusted\"}}" PAYLOAD

echo "--- managed launch outside a Git repository ---"
PLAIN_ROOT="$WORK/plain\"root\\segment"
mkdir -p "$PLAIN_ROOT"
run_codex "$PLAIN_ROOT" 1
escaped_plain="${PLAIN_ROOT//\\/\\\\}"
escaped_plain="${escaped_plain//\"/\\\"}"
assert_argv "managed argv trusts and TOML-escapes the plain cwd" \
  -c "model_instructions_file=\"$MODEL_FILE\"" \
  -c "projects={\"$escaped_plain\"={trust_level=\"trusted\"}}" PAYLOAD

echo ""
echo "=== Codex Trust Override Test Summary ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
[[ $FAIL -eq 0 ]]
