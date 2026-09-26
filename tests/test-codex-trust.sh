#!/usr/bin/env bash
# Control-managed Codex launches receive a per-launch workspace trust override.
set -euo pipefail

# Sandbox hermeticity: an operator shell exporting these must not leak in.
unset ASHA_HOME ASHA_PERSONA XDG_STATE_HOME XDG_DATA_HOME 2>/dev/null || true

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
printf 'OPERATION RULES\n' >"$HOME_DIR/.asha/operation.md"
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
COORDINATOR_MODEL_FILE="$HOME_DIR/.asha/cache/instructions-codex.md"

run_codex() {
  local cwd="$1" managed="$2" coordinator="${3:-}"
  local -a launch_env=(env -u ASHA_CONTROL_MANAGED -u ASHA_COORDINATOR_LAUNCH
    HOME="$HOME_DIR" ASHA_CODEX_CMD="$HOME_DIR/bin/codex"
    ASHA_TEST_CAPTURE="$CAPTURE")
  if [[ "$managed" == 1 ]]; then
    launch_env+=(ASHA_CONTROL_MANAGED=1)
  fi
  if [[ -n "$coordinator" ]]; then
    launch_env+=(ASHA_COORDINATOR_LAUNCH="$coordinator")
  fi
  (cd "$cwd" && "${launch_env[@]}" \
    bash "$DISPATCHER" codex PAYLOAD >/dev/null 2>"$WORK/stderr")
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

echo "--- coordinator launch ---"
COORDINATOR_ROOT="$WORK/coordinator-root"
mkdir -p "$COORDINATOR_ROOT"
run_codex "$COORDINATOR_ROOT" 0 tok123
assert_argv "coordinator argv trusts the cwd and sets unattended posture" \
  -c "model_instructions_file=\"$COORDINATOR_MODEL_FILE\"" \
  -c "projects={\"$COORDINATOR_ROOT\"={trust_level=\"trusted\"}}" \
  -a never --sandbox danger-full-access PAYLOAD

echo "--- default launch has no unattended posture ---"
run_codex "$GIT_ROOT/nested/workspace" 0
assert_argv "default argv has no coordinator posture" \
  -c "model_instructions_file=\"$MODEL_FILE\"" PAYLOAD

echo "--- managed worker has trust without unattended posture ---"
run_codex "$GIT_ROOT/nested/workspace" 1
assert_argv "managed worker argv has trust without coordinator posture" \
  -c "model_instructions_file=\"$MODEL_FILE\"" \
  -c "projects={\"$GIT_ROOT\"={trust_level=\"trusted\"}}" PAYLOAD

echo ""
echo "--- managed app-server conversation ---"
(cd "$GIT_ROOT/nested/workspace" && env -u ASHA_CONTROL_MANAGED -u ASHA_COORDINATOR_LAUNCH \
  -u ASHA_ROOM_ID -u ASHA_SEAT HOME="$HOME_DIR" ASHA_CODEX_CMD="$HOME_DIR/bin/codex" \
  ASHA_TEST_CAPTURE="$CAPTURE" ASHA_MANAGED_SESSION_ID=fixture-session \
  ASHA_PERSONA=1 ASHA_ORCHESTRATOR_STANCE=0 \
  bash "$DISPATCHER" codex app-server --listen stdio:// --disable multi_agent \
  >/dev/null 2>"$WORK/stderr")
assert_argv "managed app-server retains identity without coordinator execution overrides" \
  -c "model_instructions_file=\"$COORDINATOR_MODEL_FILE\"" \
  app-server --listen stdio:// --disable multi_agent
if rg -q 'SOUL' "$COORDINATOR_MODEL_FILE" && rg -q 'OPERATION RULES' "$COORDINATOR_MODEL_FILE"; then
  ok "managed app-server receives identity and operational instructions"
else
  fail "managed app-server receives identity and operational instructions"
fi

echo ""
echo "--- shared app-server daemon (#100) ---"
# Codex 0.157 runs TUI threads, and their hooks, inside a shared daemon that
# keeps the environment of whichever process spawned it. Every TUI launch
# through the wrapper must opt out; subcommands and old Codex must not.
cat >"$HOME_DIR/bin/codex-daemon" <<'EOF2'
#!/usr/bin/env bash
if [[ "${1:-}" == --help ]]; then
  printf '      --no-daemon\n          Run without the shared background server\n'
  exit 0
fi
printf '%s\0' "$@" >"$ASHA_TEST_CAPTURE"
EOF2
chmod +x "$HOME_DIR/bin/codex-daemon"

run_daemon_codex() {
  local executable="$1" coordinator="$2"
  shift 2
  local -a launch_env=(env -u ASHA_CONTROL_MANAGED -u ASHA_COORDINATOR_LAUNCH
    -u ASHA_ROOM_ID -u ASHA_SEAT HOME="$HOME_DIR" ASHA_CODEX_CMD="$executable"
    ASHA_TEST_CAPTURE="$CAPTURE")
  [[ -z "$coordinator" ]] || launch_env+=(ASHA_COORDINATOR_LAUNCH="$coordinator")
  (cd "$GIT_ROOT/nested/workspace" && "${launch_env[@]}" \
    bash "$DISPATCHER" codex "$@" >/dev/null 2>"$WORK/stderr")
}
DAEMON_CODEX="$HOME_DIR/bin/codex-daemon"

run_daemon_codex "$DAEMON_CODEX" "" PAYLOAD
assert_argv "chair TUI launch runs without the shared daemon" \
  -c "model_instructions_file=\"$MODEL_FILE\"" --no-daemon PAYLOAD

run_daemon_codex "$DAEMON_CODEX" "" resume thread-1 PAYLOAD
assert_argv "native resume takes its own --no-daemon" \
  -c "model_instructions_file=\"$MODEL_FILE\"" resume --no-daemon thread-1 PAYLOAD

run_daemon_codex "$DAEMON_CODEX" "" --no-daemon PAYLOAD
assert_argv "an explicit --no-daemon (Room argv) is not duplicated" \
  -c "model_instructions_file=\"$MODEL_FILE\"" --no-daemon PAYLOAD

run_daemon_codex "$DAEMON_CODEX" "" resume thread-1 --no-daemon -m gpt-5.5 PAYLOAD
assert_argv "an explicit resume --no-daemon is not duplicated" \
  -c "model_instructions_file=\"$MODEL_FILE\"" resume thread-1 --no-daemon -m gpt-5.5 PAYLOAD

run_daemon_codex "$DAEMON_CODEX" tok123 PAYLOAD
assert_argv "coordinator TUI launch runs without the shared daemon" \
  -c "model_instructions_file=\"$COORDINATOR_MODEL_FILE\"" \
  -c "projects={\"$GIT_ROOT\"={trust_level=\"trusted\"}}" \
  -a never --sandbox danger-full-access --no-daemon PAYLOAD

run_daemon_codex "$DAEMON_CODEX" "" exec PAYLOAD
assert_argv "non-interactive subcommands are left alone" \
  -c "model_instructions_file=\"$MODEL_FILE\"" exec PAYLOAD

run_daemon_codex "$DAEMON_CODEX" "" app-server --listen stdio://
assert_argv "the private structured app-server is left alone" \
  -c "model_instructions_file=\"$MODEL_FILE\"" app-server --listen stdio://

run_daemon_codex "$DAEMON_CODEX" "" --remote ws://127.0.0.1:1 PAYLOAD
assert_argv "a remote TUI (which Codex refuses with --no-daemon) is left alone" \
  -c "model_instructions_file=\"$MODEL_FILE\"" --remote ws://127.0.0.1:1 PAYLOAD

run_daemon_codex "$DAEMON_CODEX" "" --remote=ws://127.0.0.1:1
assert_argv "a remote TUI given as --remote=URL is left alone" \
  -c "model_instructions_file=\"$MODEL_FILE\"" --remote=ws://127.0.0.1:1

run_daemon_codex "$DAEMON_CODEX" "" --enable f -c x=y agents
assert_argv "a subcommand after leading options is still recognised" \
  -c "model_instructions_file=\"$MODEL_FILE\"" --enable f -c x=y agents

# Codex's own grammar decides TUI versus subcommand: option values, resume
# arguments and prompt text that happen to spell a subcommand stay TUI launches.
run_daemon_codex "$DAEMON_CODEX" "" -p review hello
assert_argv "a profile named like a subcommand is still a TUI launch" \
  -c "model_instructions_file=\"$MODEL_FILE\"" --no-daemon -p review hello

run_daemon_codex "$DAEMON_CODEX" "" resume review
assert_argv "a conversation named like a subcommand is still resumed without the daemon" \
  -c "model_instructions_file=\"$MODEL_FILE\"" resume --no-daemon review

run_daemon_codex "$DAEMON_CODEX" "" -- review
assert_argv "prompt text after -- is never a subcommand" \
  -c "model_instructions_file=\"$MODEL_FILE\"" --no-daemon -- review

run_daemon_codex "$DAEMON_CODEX" "" -m gpt-5.5 -c 'x="exec"' --search apply
assert_argv "a real subcommand after options and flags is left alone" \
  -c "model_instructions_file=\"$MODEL_FILE\"" -m gpt-5.5 -c 'x="exec"' --search apply

run_daemon_codex "$DAEMON_CODEX" "" -i a.png b.png
assert_argv "variadic image values are not mistaken for a subcommand or prompt" \
  -c "model_instructions_file=\"$MODEL_FILE\"" --no-daemon -i a.png b.png

run_daemon_codex "$DAEMON_CODEX" "" hello --remote ws://127.0.0.1:1
assert_argv "--remote after the prompt still leaves the launch alone" \
  -c "model_instructions_file=\"$MODEL_FILE\"" hello --remote ws://127.0.0.1:1

run_daemon_codex "$DAEMON_CODEX" "" -- --remote
assert_argv "--remote as prompt text after -- does not disable the fix" \
  -c "model_instructions_file=\"$MODEL_FILE\"" --no-daemon -- --remote

run_daemon_codex "$DAEMON_CODEX" "" -mgpt-5.5 exec
assert_argv "an attached short option value is not a positional" \
  -c "model_instructions_file=\"$MODEL_FILE\"" -mgpt-5.5 exec

run_daemon_codex "$HOME_DIR/bin/codex" "" --no-daemon PAYLOAD
assert_argv "a Codex without the flag (pre-0.157) never receives it" \
  -c "model_instructions_file=\"$MODEL_FILE\"" PAYLOAD

run_daemon_codex "$HOME_DIR/bin/codex" "" resume thread-1 --no-daemon PAYLOAD
assert_argv "a pre-0.157 resume also drops the flag" \
  -c "model_instructions_file=\"$MODEL_FILE\"" resume thread-1 PAYLOAD

echo "=== Codex Trust Override Test Summary ==="
echo "Passed: $PASS"
echo "Failed: $FAIL"
[[ $FAIL -eq 0 ]]
