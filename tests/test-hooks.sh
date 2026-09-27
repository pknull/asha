#!/usr/bin/env bash
# Memory v2 hook contract tests.
set -uo pipefail

# Sandbox hermeticity: an operator shell exporting these must not leak in.
# A hub worker running this suite from inside its own session would otherwise
# leak its profile and session identity into every handler under test.
unset ASHA_HOME XDG_STATE_HOME XDG_DATA_HOME ASHA_SESSION_PROFILE \
      ASHA_HUB_SESSION_ID ASHA_HUB_GENERATION ASHA_ROOM_ID 2>/dev/null || true

REPO_ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HANDLERS="$REPO_ROOT/plugins/session/hooks/handlers"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS=0 FAIL=0
ok() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }
check() { local name="$1"; shift; if "$@"; then ok "$name"; else fail "$name"; fi; }

PROJECT="$WORK/project"
HOME_DIR="$WORK/home"
mkdir -p "$PROJECT/.asha" "$PROJECT/Memory" "$PROJECT/Work/markers" "$HOME_DIR/.asha"
printf '{"initialized":true,"memory_version":2,"project_id":"project-test"}\n' > "$PROJECT/.asha/config.json"
printf '# Objective\nO\n# State\nS\n# Next\n- N\n# Blockers\n- None\n' > "$PROJECT/Memory/activeContext.md"
printf '# Decisions\n- D\n' > "$PROJECT/Memory/decisions.md"
printf 'operation-v2-sentinel\n' > "$HOME_DIR/.asha/operation.md"

run_hook() {
  local hook="$1" payload="$2" harness="${3:-claude}"
  printf '%s' "$payload" | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
    CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" ASHA_HARNESS="$harness" \
    "$HANDLERS/$hook"
}

echo "--- Memory v2 hooks ---"

OUT="$(run_hook session-start.sh '{"session_id":"start-1","cwd":"'"$PROJECT"'"}')"
[[ "$OUT" == *operation-v2-sentinel* ]] && ok "SessionStart injects operational context" || fail "SessionStart injects operational context"
[[ "$OUT" == *"Published repository Memory v2"* && "$OUT" == *"-- activeContext.md --"* \
   && "$OUT" == *"-- decisions.md --"* && "$OUT" == *"- D"* ]] \
  && ok "SessionStart injects the coherent repository publication" \
  || fail "SessionStart injects the coherent repository publication"
CODEX_START="$(run_hook session-start.sh '{"session_id":"codex-start","cwd":"'"$PROJECT"'"}' codex)"
OPENCODE_START="$(run_hook session-start.sh '{"session_id":"opencode-start","cwd":"'"$PROJECT"'"}' opencode)"
COPILOT_START="$(run_hook session-start.sh '{"sessionId":"copilot-start","cwd":"'"$PROJECT"'"}' copilot)"
[[ "$CODEX_START" == *"Published repository Memory v2"* \
   && "$OPENCODE_START" == *"Published repository Memory v2"* ]] \
  && ok "Codex and OpenCode receive the shared project publication" \
  || fail "Codex and OpenCode receive the shared project publication"
printf '%s' "$COPILOT_START" | jq -e \
  '.additionalContext | contains("Published repository Memory v2") and contains("-- decisions.md --")' \
  >/dev/null 2>&1 \
  && ok "Copilot receives project publication through additionalContext" \
  || fail "Copilot receives project publication through additionalContext"
if [[ "$OUT" == *'\n'* ]]; then
  fail "SessionStart renders real newlines rather than literal backslash-n delimiters"
else
  ok "SessionStart renders real newlines rather than literal backslash-n delimiters"
fi
check "SessionStart writes a bounded project-local snapshot" test -f "$PROJECT/Work/session-state/claude-start-1.json"
[[ $(wc -c < "$PROJECT/Work/session-state/claude-start-1.json") -le 2048 ]] \
  && ok "SessionStart snapshot respects 2 KiB cap" || fail "SessionStart snapshot respects 2 KiB cap"

rm -f "$PROJECT/Work/session-state/claude-unknown.json"
run_hook user-prompt-submit.sh '{"cwd":"'"$PROJECT"'","prompt":"identity absent"}' >/dev/null
check "missing hook identity never collapses into unknown snapshot" test ! -e "$PROJECT/Work/session-state/claude-unknown.json"

mkdir -p "$HOME_DIR/.asha/learnings/candidate"
cat > "$HOME_DIR/.asha/learnings/candidate/expired-hook.md" <<'EOF'
---
{"type":"learning","id":"expired-hook","trigger":"t","action":"a","state":"candidate","created":"2020-01-01","updated":"2020-01-01","retirement_reason":"","evidence":[]}
---

# expired-hook

**Trigger:** t

**Action:** a
EOF
run_hook session-start.sh '{"session_id":"expiry","cwd":"'"$PROJECT"'"}' >/dev/null
check "SessionStart wires 90-day candidate expiry into lifecycle" test -f "$HOME_DIR/.asha/learnings/retired/expired-hook.md"

run_hook user-prompt-submit.sh '{"session_id":"p1","cwd":"'"$PROJECT"'","prompt":"resume src/main.py"}' >/dev/null
python3 - "$PROJECT/Work/session-state/claude-p1.json" <<'PY' \
  && ok "UserPromptSubmit records only recovery fields" || fail "UserPromptSubmit records only recovery fields"
import json, pathlib, sys
data=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert data["prompt"] == "resume src/main.py"
assert set(data) <= {"version","snapshot_name","harness_sha256","session_sha256","harness_component","session_component","harness_redacted","session_redacted","harness_truncated","session_truncated","session_id","project_id","harness","created_at","updated_at","prompt","last_action","paths","blocker"}
PY

run_hook post-tool-use.sh '{"session_id":"p1","cwd":"'"$PROJECT"'","tool_name":"Edit","tool_input":{"file_path":"src/main.py"}}' >/dev/null
python3 - "$PROJECT/Work/session-state/claude-p1.json" <<'PY' \
  && ok "PostToolUse merges the touched path" || fail "PostToolUse merges the touched path"
import json, pathlib, sys
data=json.loads(pathlib.Path(sys.argv[1]).read_text())
assert data["last_action"] == "Edit" and data["paths"] == ["src/main.py"]
PY

# Payload cwd is authoritative. Resolve initialized ancestors from nested cwd,
# never redirect to a stale ambient project, and reject HOME itself.
STALE="$WORK/stale"
mkdir -p "$PROJECT/src/nested" "$STALE/.asha" "$STALE/Work/markers"
printf '{"initialized":true,"memory_version":2,"project_id":"stale"}\n' > "$STALE/.asha/config.json"
printf '%s' '{"session_id":"nested","cwd":"'"$PROJECT/src/nested"'","prompt":"nested"}' \
  | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$STALE" CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" \
      ASHA_HARNESS=claude "$HANDLERS/user-prompt-submit.sh" >/dev/null
[[ -f "$PROJECT/Work/session-state/claude-nested.json" && ! -e "$STALE/Work/session-state/claude-nested.json" ]] \
  && ok "authoritative nested payload cwd resolves upward without ambient fallback" \
  || fail "authoritative nested payload cwd resolves upward without ambient fallback"
mkdir -p "$HOME_DIR/.asha" "$HOME_DIR/Work/markers"
printf '{"initialized":true,"memory_version":2,"project_id":"home-must-not-be-project"}\n' > "$HOME_DIR/.asha/config.json"
printf '%s' '{"session_id":"home","cwd":"'"$HOME_DIR"'","prompt":"no home writes"}' \
  | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$STALE" CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" \
      ASHA_HARNESS=claude "$HANDLERS/user-prompt-submit.sh" >/dev/null
[[ ! -e "$HOME_DIR/Work/session-state/claude-home.json" && ! -e "$STALE/Work/session-state/claude-home.json" ]] \
  && ok "authoritative HOME cwd is rejected without ambient fallback" \
  || fail "authoritative HOME cwd is rejected without ambient fallback"

touch "$PROJECT/Work/markers/rp-active"
RP="$(run_hook user-prompt-submit.sh '{"session_id":"p1","cwd":"'"$PROJECT"'","prompt":"continue"}')"
[[ "$RP" == *"RP session active"* ]] && ok "RP routing survives nudge removal" || fail "RP routing survives nudge removal"
COPILOT_RP="$(run_hook user-prompt-submit.sh '{"sessionId":"cp1","cwd":"'"$PROJECT"'","prompt":"continue"}' copilot)"
printf '%s' "$COPILOT_RP" | jq -e '.additionalContext | contains("RP session active")' >/dev/null 2>&1 \
  && ok "Copilot RP routing uses additionalContext" || fail "Copilot RP routing uses additionalContext"

PRICED="$(run_hook user-prompt-submit.sh '{"session_id":"priced","cwd":"'"$PROJECT"'","prompt":"What price must we pay to escape forever?"}')"
[[ "$PRICED" == *"PRICED STAKE"* && "$PRICED" == *"SOURCE_LOG.priced_stake_touched"* ]] \
  && ok "RP priced-stakes safeguard is re-homed at the prompt seam" \
  || fail "RP priced-stakes safeguard is re-homed at the prompt seam"
PRICED_AGAIN="$(run_hook user-prompt-submit.sh '{"session_id":"priced","cwd":"'"$PROJECT"'","prompt":"What price must we pay to escape forever?"}')"
[[ "$PRICED_AGAIN" != *"PRICED STAKE"* ]] \
  && ok "RP priced-stakes direct safeguard preserves its cooldown" \
  || fail "RP priced-stakes direct safeguard preserves its cooldown"
rm -f "$PROJECT/Work/markers/rp-active"

# A workspace child receives its repository publication plus the workspace
# publication. A launch at the workspace root receives the pair once and keeps
# only the workspace metadata wrapper beside it.
cat > "$PROJECT/.asha/workspace.json" <<'JSON'
{
  "version": 1,
  "workspace_name": "hook-workspace",
  "repositories": [{"path": "child", "docs": "knowledge/repos/child"}],
  "memory": {
    "operational_root": "Memory",
    "personal_root": "memory-local",
    "shared_root": "knowledge",
    "shared_git_root": ".",
    "promotion_mode": "pull-request"
  }
}
JSON
CHILD="$PROJECT/child"
mkdir -p "$CHILD/.asha" "$CHILD/Memory" "$CHILD/Work/markers"
printf '{"initialized":true,"memory_version":2,"project_id":"child-test"}\n' > "$CHILD/.asha/config.json"
printf '# Objective\nchild-publication-sentinel\n# State\nReady\n# Next\n- N\n# Blockers\n- None\n' > "$CHILD/Memory/activeContext.md"
printf '# Decisions\n- child-decision-sentinel\n' > "$CHILD/Memory/decisions.md"
printf '# Objective\nworkspace-publication-sentinel\n# State\nReady\n# Next\n- N\n# Blockers\n- None\n' > "$PROJECT/Memory/activeContext.md"
CHILD_OUT="$(run_hook session-start.sh '{"session_id":"ws-child","cwd":"'"$CHILD"'"}')"
[[ "$CHILD_OUT" == *"Published repository Memory v2"* \
   && "$CHILD_OUT" == *child-publication-sentinel* \
   && "$CHILD_OUT" == *workspace-publication-sentinel* \
   && "$CHILD_OUT" == *"active repo: child"* ]] \
  && ok "workspace child receives distinct project and workspace context" \
  || fail "workspace child receives distinct project and workspace context"
ROOT_OUT="$(run_hook session-start.sh '{"session_id":"ws-root","cwd":"'"$PROJECT"'"}')"
ROOT_SENTINELS="$(printf '%s' "$ROOT_OUT" | grep -o 'workspace-publication-sentinel' | wc -l)"
[[ "$ROOT_OUT" == *"Published workspace Memory v2"* \
   && "$ROOT_OUT" == *"Workspace: hook-workspace"* \
   && "$ROOT_SENTINELS" -eq 1 ]] \
  && ok "workspace root publication is injected once with workspace metadata" \
  || fail "workspace root publication is injected once with workspace metadata"
rm -rf "$CHILD"
rm -f "$PROJECT/.asha/workspace.json"
printf '# Objective\nO\n# State\nS\n# Next\n- N\n# Blockers\n- None\n' > "$PROJECT/Memory/activeContext.md"

# Compatibility alias: an existing opt-out must not silently resume workspace
# injection after upgrading from the nudge engine.
printf '{"roots":[{"path":"."}]}\n' > "$PROJECT/.asha/workspace.json"
touch "$PROJECT/Work/markers/nudge-ws-context-off"
WS_OFF="$(run_hook session-start.sh '{"session_id":"ws-off","cwd":"'"$PROJECT"'"}')"
[[ "$WS_OFF" != *"Workspace knowledge"* ]] \
  && ok "legacy nudge-ws-context-off marker remains honored" \
  || fail "legacy nudge-ws-context-off marker remains honored"
rm -f "$PROJECT/Work/markers/nudge-ws-context-off" "$PROJECT/.asha/workspace.json"

echo "--- verification-pass and style-audit hooks ---"

PASS_TOKEN="old-value-$RANDOM-should-disappear"
mkdir -p "$PROJECT/src"
printf '%s\n' "$PASS_TOKEN" > "$PROJECT/src/pass-proof.txt"
if (cd "$PROJECT" && HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
    "$REPO_ROOT/plugins/session/tools/declare-pass.sh" "$PASS_TOKEN" replacement \
    >/dev/null); then
  ok "declare-pass writes a project-local marker"
else
  fail "declare-pass writes a project-local marker"
fi
jq -e --arg old "$PASS_TOKEN" \
  '.schema_version == 1 and .old == $old and .new == "replacement"' \
  "$PROJECT/Work/markers/pass-declaration.json" >/dev/null 2>&1 \
  && ok "declare-pass records old and optional new values" \
  || fail "declare-pass records old and optional new values"

VERIFY_HIT="$(run_hook verify-pass-complete.sh \
  '{"session_id":"verify-1","cwd":"'"$PROJECT"'","stop_hook_active":false}')"
printf '%s' "$VERIFY_HIT" | jq -e --arg old "$PASS_TOKEN" \
  '.decision == "block" and (.reason | contains($old) and contains("src/pass-proof.txt"))' \
  >/dev/null 2>&1 \
  && ok "verification Stop blocks once and names remaining files" \
  || fail "verification Stop blocks once and names remaining files"
VERIFY_LOOP="$(run_hook verify-pass-complete.sh \
  '{"session_id":"verify-1","cwd":"'"$PROJECT"'","stop_hook_active":true}')"
[[ "$VERIFY_LOOP" == '{}' && -f "$PROJECT/Work/markers/pass-declaration.json" ]] \
  && ok "verification Stop honors the loop guard without clearing an unproved marker" \
  || fail "verification Stop honors the loop guard without clearing an unproved marker"
CODEX_VERIFY="$(run_hook verify-pass-complete.sh \
  '{"session_id":"verify-codex","cwd":"'"$PROJECT"'","stop_hook_active":false}' codex)"
printf '%s' "$CODEX_VERIFY" | jq -e '.decision == "block" and (.reason | length > 0)' \
  >/dev/null 2>&1 \
  && ok "Codex Stop verification emits JSON without a plain-text prefix" \
  || fail "Codex Stop verification emits JSON without a plain-text prefix"
printf 'replacement\n' > "$PROJECT/src/pass-proof.txt"
VERIFY_EMPTY="$(run_hook verify-pass-complete.sh \
  '{"session_id":"verify-2","cwd":"'"$PROJECT"'","stop_hook_active":false}')"
[[ "$VERIFY_EMPTY" == '{}' && ! -e "$PROJECT/Work/markers/pass-declaration.json" ]] \
  && ok "empty verification proof clears the marker without a done nudge" \
  || fail "empty verification proof clears the marker without a done nudge"
VERIFY_NONE="$(run_hook verify-pass-complete.sh \
  '{"session_id":"verify-none","cwd":"'"$PROJECT"'","stop_hook_active":false}')"
[[ "$VERIFY_NONE" == '{}' ]] \
  && ok "verification handler with no marker is a fail-open no-op" \
  || fail "verification handler with no marker is a fail-open no-op"
NO_MARKERS="$WORK/no-markers"
mkdir -p "$NO_MARKERS/.asha"
printf '{"initialized":true,"memory_version":2,"project_id":"no-markers"}\n' > "$NO_MARKERS/.asha/config.json"
VERIFY_NODIR_ERR="$WORK/verify-nodir.err"
VERIFY_NODIR="$(printf '%s' '{"session_id":"verify-nodir","cwd":"'"$NO_MARKERS"'","stop_hook_active":false}' \
  | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$NO_MARKERS" CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" \
    ASHA_HARNESS=claude "$HANDLERS/verify-pass-complete.sh" 2>"$VERIFY_NODIR_ERR")"
[[ "$VERIFY_NODIR" == '{}' && ! -s "$VERIFY_NODIR_ERR" ]] \
  && ok "verification handler without Work/markers is a silent no-op" \
  || fail "verification handler without Work/markers is a silent no-op"

BINARY_TOKEN="binary-old-$RANDOM-should-disappear"
printf '%s\0binary-tail\n' "$BINARY_TOKEN" > "$PROJECT/src/pass-proof.bin"
(cd "$PROJECT" && HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
  "$REPO_ROOT/plugins/session/tools/declare-pass.sh" "$BINARY_TOKEN" replacement \
  >/dev/null)
VERIFY_BINARY="$(run_hook verify-pass-complete.sh \
  '{"session_id":"verify-binary","cwd":"'"$PROJECT"'","stop_hook_active":false}')"
printf '%s' "$VERIFY_BINARY" | jq -e \
  '.decision == "block" and (.reason | contains("src/pass-proof.bin"))' \
  >/dev/null 2>&1 \
  && ok "verification proof finds fixed strings in binary/NUL-containing files" \
  || fail "verification proof finds fixed strings in binary/NUL-containing files"
rm -f "$PROJECT/src/pass-proof.bin" "$PROJECT/Work/markers/pass-declaration.json"

RACE_OLD="race-old-$RANDOM"
RACE_NEW="race-new-$RANDOM"
(cd "$PROJECT" && HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
  "$REPO_ROOT/plugins/session/tools/declare-pass.sh" "$RACE_OLD" replacement \
  >/dev/null)
RACE_BIN="$WORK/race-bin"
RACE_STARTED="$WORK/race-started"
RACE_RELEASE="$WORK/race-release"
RACE_OUTPUT="$WORK/race-output"
mkdir -p "$RACE_BIN"
cat > "$RACE_BIN/grep" <<'EOF'
#!/usr/bin/env bash
: > "$ASHA_TEST_GREP_STARTED"
for ((i = 0; i < 500; i++)); do
  [[ -e "$ASHA_TEST_GREP_RELEASE" ]] && exit 1
  sleep 0.01
done
exit 2
EOF
chmod +x "$RACE_BIN/grep"
printf '%s' '{"session_id":"verify-race","cwd":"'"$PROJECT"'","stop_hook_active":false}' \
  | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
      CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" ASHA_HARNESS=claude \
      ASHA_TEST_GREP_STARTED="$RACE_STARTED" ASHA_TEST_GREP_RELEASE="$RACE_RELEASE" \
      PATH="$RACE_BIN:$PATH" "$HANDLERS/verify-pass-complete.sh" > "$RACE_OUTPUT" &
RACE_PID=$!
for ((i = 0; i < 500; i++)); do
  [[ -e "$RACE_STARTED" ]] && break
  sleep 0.01
done
if [[ -e "$RACE_STARTED" ]] \
   && (cd "$PROJECT" && HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
       "$REPO_ROOT/plugins/session/tools/declare-pass.sh" "$RACE_NEW" replacement \
       >/dev/null); then
  touch "$RACE_RELEASE"
  wait "$RACE_PID"
  RACE_HANDLER_OUTPUT="$(cat "$RACE_OUTPUT")"
  if [[ "$RACE_HANDLER_OUTPUT" == '{}' ]] \
     && jq -e --arg old "$RACE_NEW" '.old == $old' \
          "$PROJECT/Work/markers/pass-declaration.json" >/dev/null 2>&1; then
    ok "empty proof never clears a concurrently replaced declaration"
  else
    fail "empty proof never clears a concurrently replaced declaration"
  fi
else
  touch "$RACE_RELEASE"
  wait "$RACE_PID" 2>/dev/null || true
  fail "empty proof never clears a concurrently replaced declaration"
fi
rm -f "$PROJECT/Work/markers/pass-declaration.json"

cat > "$PROJECT/.asha/style-audit" <<'EOF'
#!/usr/bin/env bash
ROOT="$(cd -P "$(dirname "$0")/.." && pwd)"
printf '%s' "$1" > "$ROOT/Work/style-audit-arg"
cat > "$ROOT/Work/style-audit-payload"
case "$(cat "$ROOT/.asha/style-audit-mode" 2>/dev/null || true)" in
  report) printf 'avoid flat cadence\n' ;;
  slow) sleep 12; printf 'too late\n' ;;
  *) : ;;
esac
EOF
chmod +x "$PROJECT/.asha/style-audit"
printf 'report\n' > "$PROJECT/.asha/style-audit-mode"
STYLE_PAYLOAD='{"session_id":"style-1","cwd":"'"$PROJECT"'","tool_name":"Edit","tool_input":{"file_path":"src/main.py"}}'
STYLE_OUT="$(run_hook post-tool-use.sh "$STYLE_PAYLOAD")"
printf '%s' "$STYLE_OUT" | jq -e \
  '.hookSpecificOutput.hookEventName == "PostToolUse"
   and (.hookSpecificOutput.additionalContext | contains("STYLE AUDIT FINDING") and contains("avoid flat cadence"))' \
  >/dev/null 2>&1 \
  && ok "executable style audit emits Claude PostToolUse additionalContext" \
  || fail "executable style audit emits Claude PostToolUse additionalContext"
[[ "$(cat "$PROJECT/Work/style-audit-arg")" == "src/main.py" ]] \
  && jq -e '.tool_name == "Edit"' "$PROJECT/Work/style-audit-payload" >/dev/null 2>&1 \
  && ok "style audit receives the edited path and full payload" \
  || fail "style audit receives the edited path and full payload"

printf 'empty\n' > "$PROJECT/.asha/style-audit-mode"
[[ "$(run_hook post-tool-use.sh "$STYLE_PAYLOAD")" == '{}' ]] \
  && ok "empty style-audit output is a no-op" \
  || fail "empty style-audit output is a no-op"
chmod -x "$PROJECT/.asha/style-audit"
[[ "$(run_hook post-tool-use.sh "$STYLE_PAYLOAD")" == '{}' ]] \
  && ok "non-executable style audit is a no-op" \
  || fail "non-executable style audit is a no-op"
mv "$PROJECT/.asha/style-audit" "$PROJECT/.asha/style-audit.absent"
[[ "$(run_hook post-tool-use.sh "$STYLE_PAYLOAD")" == '{}' ]] \
  && ok "absent style audit is a no-op" \
  || fail "absent style audit is a no-op"
mv "$PROJECT/.asha/style-audit.absent" "$PROJECT/.asha/style-audit"
chmod +x "$PROJECT/.asha/style-audit"
printf 'slow\n' > "$PROJECT/.asha/style-audit-mode"
[[ "$(run_hook post-tool-use.sh "$STYLE_PAYLOAD")" == '{}' ]] \
  && ok "timed-out style audit fails open without a nudge" \
  || fail "timed-out style audit fails open without a nudge"

printf 'report\n' > "$PROJECT/.asha/style-audit-mode"
COPILOT_STYLE_PAYLOAD='{"sessionId":"style-copilot","cwd":"'"$PROJECT"'","toolName":"edit","toolArgs":"{\"path\":\"src/copilot.py\"}"}'
COPILOT_POST="$(run_hook post-tool-use.sh "$COPILOT_STYLE_PAYLOAD" copilot)"
COPILOT_STYLE="$(run_hook user-prompt-submit.sh \
  '{"sessionId":"style-copilot","cwd":"'"$PROJECT"'","prompt":"continue"}' copilot)"
[[ "$COPILOT_POST" == '{}' ]] \
  && printf '%s' "$COPILOT_STYLE" | jq -e \
    '.additionalContext | contains("STYLE AUDIT FINDING") and contains("src/copilot.py")' \
    >/dev/null 2>&1 \
  && ok "Copilot delivers style findings at the next prompt" \
  || fail "Copilot delivers style findings at the next prompt"
printf 'unrelated marker\n' > "$PROJECT/Work/markers/keep.md"
COPILOT_HOSTILE_PAYLOAD='{"sessionId":"..","cwd":"'"$PROJECT"'","toolName":"edit","toolArgs":{"path":"src/hostile.py"}}'
COPILOT_HOSTILE_POST="$(run_hook post-tool-use.sh "$COPILOT_HOSTILE_PAYLOAD" copilot)"
COPILOT_HOSTILE_STYLE="$(run_hook user-prompt-submit.sh \
  '{"sessionId":"..","cwd":"'"$PROJECT"'","prompt":"continue"}' copilot)"
[[ "$COPILOT_HOSTILE_POST" == '{}' && -f "$PROJECT/Work/markers/keep.md" ]] \
  && printf '%s' "$COPILOT_HOSTILE_STYLE" | jq -e \
    '.additionalContext | contains("src/hostile.py")' >/dev/null 2>&1 \
  && ok "Copilot style queue confines hostile session identifiers" \
  || fail "Copilot style queue confines hostile session identifiers"
rm -f "$PROJECT/Work/markers/keep.md"
OPENCODE_STYLE="$(run_hook post-tool-use.sh \
  '{"session_id":"style-opencode","cwd":"'"$PROJECT"'","tool_name":"apply_patch","tool_input":{"patchText":"*** Update File: src/open.py\n@@"}}' opencode)"
[[ "$OPENCODE_STYLE" == *"STYLE AUDIT FINDING"* \
   && "$OPENCODE_STYLE" == *"src/open.py"* ]] \
  && ok "OpenCode apply_patch patchText produces pending style context" \
  || fail "OpenCode apply_patch patchText produces pending style context"

BEFORE="$(sha256sum "$PROJECT/Memory/activeContext.md" "$PROJECT/Memory/decisions.md")"
LAST_ACTION_BEFORE="$(jq -r '.last_action' "$PROJECT/Work/session-state/claude-p1.json")"
run_hook session-end.sh '{"session_id":"p1","cwd":"'"$PROJECT"'","reason":"logout"}' >/dev/null
AFTER="$(sha256sum "$PROJECT/Memory/activeContext.md" "$PROJECT/Memory/decisions.md")"
[[ "$BEFORE" == "$AFTER" ]] && ok "SessionEnd never publishes semantic Memory" || fail "SessionEnd never publishes semantic Memory"
jq -e --arg before "$LAST_ACTION_BEFORE" \
  '.last_action == $before and (.sealed_at | type == "string" and length > 0)' \
  "$PROJECT/Work/session-state/claude-p1.json" >/dev/null \
  && ok "SessionEnd seals without erasing the last mechanical action" \
  || fail "SessionEnd seals without erasing the last mechanical action"

touch "$PROJECT/Work/markers/silence"
rm -f "$PROJECT/Work/session-state/claude-silent.json"
run_hook user-prompt-submit.sh '{"session_id":"silent","cwd":"'"$PROJECT"'","prompt":"do not persist"}' >/dev/null
check "silence marker disables recovery persistence" test ! -e "$PROJECT/Work/session-state/claude-silent.json"
rm -f "$PROJECT/Work/markers/silence"

MALFORMED="$(run_hook post-tool-use.sh 'not-json')"; RC=$?
[[ $RC -eq 0 && "$MALFORMED" == '{}' ]] && ok "malformed hook payload fails open" || fail "malformed hook payload fails open"

HOOKS="$REPO_ROOT/plugins/session/hooks/hooks.json"
jq -e '.hooks | has("SessionStart") and has("UserPromptSubmit") and has("PostToolUse") and has("PermissionRequest") and has("SessionEnd") and has("Stop")' "$HOOKS" >/dev/null \
  && ok "hook registry carries every claimed Control observation" || fail "hook registry carries every claimed Control observation"
if ! rg -n 'pattern_analyzer|jsonl_reader|event_store|detached-save|save-session|git (commit|push)|Memory/' \
      "$HANDLERS/session-start.sh" "$HANDLERS/user-prompt-submit.sh" \
      "$HANDLERS/post-tool-use.sh" "$HANDLERS/session-end.sh" >/dev/null; then
  ok "recovery hooks contain no transcript, semantic save, Memory-write, or Git path"
else
  fail "recovery hooks contain no transcript, semantic save, Memory-write, or Git path"
fi

# Existing policy behavior remains independent of Memory persistence.
policy_decision() {
  local rc=0
  printf '%s' "$1" | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
    CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" ASHA_HARNESS=claude \
    "$HANDLERS/policy-guard.sh" >/dev/null 2>&1 || rc=$?
  [[ $rc -eq 2 ]] && printf deny || printf allow
}
POLICY_OK=1
policy_case() {
  local label="$1" payload="$2" want="$3" got
  got="$(policy_decision "$payload")"
  [[ "$got" == "$want" ]] || { POLICY_OK=0; fail "policy $label ($got != $want)"; }
}
policy_case force-push '{"tool_name":"Bash","tool_input":{"command":"git push --force"}}' deny
policy_case force-with-lease '{"tool_name":"Bash","tool_input":{"command":"git push --force-with-lease"}}' allow
policy_case broad-home '{"tool_name":"Bash","tool_input":{"command":"find /home -name x"}}' deny
policy_case scoped-home '{"tool_name":"Bash","tool_input":{"command":"find /home/alice/code -name x"}}' allow
policy_case archive-delete '{"tool_name":"Bash","tool_input":{"command":"rm backup.7z"}}' deny
policy_case marker-delete '{"tool_name":"Bash","tool_input":{"command":"rm -f Work/markers/silence"}}' allow
policy_case active-edit '{"tool_name":"Write","tool_input":{"file_path":"/p/Memory/activeContext.md"}}' deny
policy_case decisions-edit '{"tool_name":"Edit","tool_input":{"file_path":"/p/Memory/decisions.md"}}' deny
policy_case relative-active-edit '{"tool_name":"Write","tool_input":{"path":"Memory/activeContext.md"}}' deny
policy_case patch-decisions-edit '{"tool_name":"apply_patch","tool_input":{"patch":"*** Update File: Memory/decisions.md"}}' deny
policy_case legacy-edit '{"tool_name":"Write","tool_input":{"file_path":"/p/Memory/projectbrief.md"}}' allow
policy_case scratch-edit '{"tool_name":"Write","tool_input":{"file_path":"/p/Memory/scratchpad.md"}}' allow
policy_case rp-invariants-edit '{"tool_name":"Write","tool_input":{"file_path":"/p/Memory/invariants.md"}}' allow
policy_case rp-canon-layout-edit '{"tool_name":"Edit","tool_input":{"file_path":"/p/Memory/canon-layout.md"}}' allow
[[ $POLICY_OK -eq 1 ]] && ok "policy matrix preserves destructive guards and v2 Memory boundary"

# require_env rules are inert outside the session that carries the variable.
coordinator_decision() {
  local rc=0
  printf '%s' "$2" | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
    CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" ASHA_HARNESS=claude \
    ASHA_ORCHESTRATION_COORDINATOR_ID="$1" \
    "$HANDLERS/policy-guard.sh" >/dev/null 2>&1 || rc=$?
  [[ $rc -eq 2 ]] && printf deny || printf allow
}
COORD_OK=1
APPROVE='{"tool_name":"Bash","tool_input":{"command":"asha initiative approve 1b853ddc-38bf-4cb8-aff3-816e550684dc --digest abc"}}'
CLAIM='{"tool_name":"Bash","tool_input":{"command":"asha initiative coordinator claim 1b853ddc-38bf-4cb8-aff3-816e550684dc --json"}}'
[[ "$(coordinator_decision "" "$APPROVE")" == allow ]] || { COORD_OK=0; fail "approve outside a coordinator session is allowed by policy"; }
[[ "$(coordinator_decision "dddddddd-dddd-4ddd-8ddd-dddddddddddd" "$APPROVE")" == deny ]] || { COORD_OK=0; fail "approve inside a coordinator session is denied by policy"; }
[[ "$(coordinator_decision "dddddddd-dddd-4ddd-8ddd-dddddddddddd" "$CLAIM")" == allow ]] || { COORD_OK=0; fail "coordinator verbs stay allowed inside a coordinator session"; }
[[ $COORD_OK -eq 1 ]] && ok "require_env scopes the coordinator approval rule to coordinator sessions"

# A standing authority is the operator's pre-signed approval: a coordinator that
# could mint or revoke one would be approving its own plans. The read stays open.
AUTH_OK=1
AUTH_ADD='{"tool_name":"Bash","tool_input":{"command":"asha initiative authority add small-fixes --repo /p --scope lib"}}'
AUTH_REVOKE='{"tool_name":"Bash","tool_input":{"command":"asha initiative authority revoke 1b853ddc-38bf-4cb8-aff3-816e550684dc"}}'
AUTH_LIST='{"tool_name":"Bash","tool_input":{"command":"asha initiative authority list --json"}}'
COORD_ID="dddddddd-dddd-4ddd-8ddd-dddddddddddd"
[[ "$(coordinator_decision "$COORD_ID" "$AUTH_ADD")" == deny ]] || { AUTH_OK=0; fail "authority add inside a coordinator session is denied by policy"; }
[[ "$(coordinator_decision "$COORD_ID" "$AUTH_REVOKE")" == deny ]] || { AUTH_OK=0; fail "authority revoke inside a coordinator session is denied by policy"; }
[[ "$(coordinator_decision "$COORD_ID" "$AUTH_LIST")" == allow ]] || { AUTH_OK=0; fail "authority list stays readable inside a coordinator session"; }
[[ "$(coordinator_decision "" "$AUTH_ADD")" == allow ]] || { AUTH_OK=0; fail "authority add outside a coordinator session is allowed by policy"; }
[[ $AUTH_OK -eq 1 ]] && ok "standing-authority grants are refused inside coordinator sessions while the read stays open"

WARN_OUT="$WORK/policy-warn.out"
WARN_ERR="$WORK/policy-warn.err"
printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"/p/Vault/Random/x.md"}}' \
  | HOME="$HOME_DIR" ASHA_HARNESS=claude "$HANDLERS/policy-guard.sh" >"$WARN_OUT" 2>"$WARN_ERR"
WARN_RC=$?
[[ $WARN_RC -eq 0 && -z "$(cat "$WARN_OUT")" && "$(cat "$WARN_ERR")" == *"WARNING by Asha policy [vault-structure]"* ]] \
  && ok "warn policy remains awareness-producing without blocking" \
  || fail "warn policy remains awareness-producing without blocking"

cat > "$HOME_DIR/.asha/policies.json" <<'JSON'
{"rules":[{"id":"deny-random-vault","tool":"Write","file_path_regex":"/Vault/Random/","action":"deny","reason":"later deny"}]}
JSON
OVERLAP_ERR="$WORK/policy-overlap.err"
printf '%s' '{"tool_name":"Write","tool_input":{"file_path":"/p/Vault/Random/x.md"}}' \
  | HOME="$HOME_DIR" ASHA_HARNESS=claude "$HANDLERS/policy-guard.sh" >/dev/null 2>"$OVERLAP_ERR"
OVERLAP_RC=$?
[[ $OVERLAP_RC -eq 2 && "$(cat "$OVERLAP_ERR")" == *"vault-structure"* && "$(cat "$OVERLAP_ERR")" == *"deny-random-vault"* ]] \
  && ok "warn policy continues evaluation to a later deny" \
  || fail "warn policy continues evaluation to a later deny"
rm -f "$HOME_DIR/.asha/policies.json"

# Retain an adversarial slice of the pre-v2 policy suite. These rails are not
# memory machinery and do not disappear merely because the suite got shorter.
policy_case checkout-discard '{"tool_name":"Bash","tool_input":{"command":"git checkout -- src/a.py"}}' deny
policy_case restore-staged '{"tool_name":"Bash","tool_input":{"command":"git restore --staged src/a.py"}}' allow
policy_case clean-force '{"tool_name":"Bash","tool_input":{"command":"git clean -fd"}}' deny
policy_case quoted-archive '{"tool_name":"Bash","tool_input":{"command":"rm \"backup.7z\""}}' deny
policy_case mixed-archive '{"tool_name":"Bash","tool_input":{"command":"rm -rf /tmp/extract ~/Downloads/pdfs.7z"}}' deny
policy_case docker-rm '{"tool_name":"Bash","tool_input":{"command":"docker rm -f container"}}' allow
policy_case home-tilde '{"tool_name":"Bash","tool_input":{"command":"grep -r pattern ~"}}' deny
policy_case home-var-scoped '{"tool_name":"Bash","tool_input":{"command":"find \"$HOME/Code\" -name x"}}' allow
policy_case cross-repo-force '{"tool_name":"Bash","tool_input":{"command":"git -C /workspace/shared push --force origin main"}}' deny
policy_case cross-repo-lease '{"tool_name":"Bash","tool_input":{"command":"git -C /workspace/shared push --force-with-lease origin main"}}' allow
policy_case continuation-force '{"tool_name":"Bash","tool_input":{"command":"git -C /ws \\\n push --force"}}' deny
[[ $POLICY_OK -eq 1 ]] && ok "adversarial policy regression matrix remains green"

copilot_decision() {
  printf '%s' "$1" | HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
    "$HANDLERS/copilot-policy-adapter.sh" 2>/dev/null \
    | jq -r '.permissionDecision // "allow"'
}
[[ "$(copilot_decision '{"toolName":"bash","toolArgs":{"command":"git push --force"}}')" == deny \
   && "$(copilot_decision '{"toolName":"create","toolArgs":{"path":"/p/.ssh/id_rsa"}}')" == deny \
   && "$(copilot_decision '{"toolName":"apply_patch","toolArgs":{"patch":"*** Update File: Memory/activeContext.md"}}')" == deny \
   && "$(copilot_decision '{"toolName":"edit","toolArgs":{"filePath":"Memory/decisions.md"}}')" == deny \
   && "$(copilot_decision '{"toolName":"view","toolArgs":{"path":"/tmp/readme"}}')" == allow ]] \
  && ok "Copilot adapter translates policy and secret decisions" \
  || fail "Copilot adapter translates policy and secret decisions"
CP_WARN_ERR="$WORK/copilot-warn.err"
CP_WARN_OUT="$(printf '%s' '{"toolName":"create","toolArgs":{"path":"/p/Vault/Random/x.md"}}' \
  | HOME="$HOME_DIR" "$HANDLERS/copilot-policy-adapter.sh" 2>"$CP_WARN_ERR")"
[[ "$(printf '%s' "$CP_WARN_OUT" | jq -r '.permissionDecision')" == allow \
   && "$(cat "$CP_WARN_ERR")" == *"WARNING by Asha policy [vault-structure]"* ]] \
  && ok "Copilot adapter preserves warn awareness whilst allowing" \
  || fail "Copilot adapter preserves warn awareness whilst allowing"
cat > "$HOME_DIR/.asha/policies.json" <<'JSON'
{"rules":[{"id":"deny-random-vault","tool":"Write","file_path_regex":"/Vault/Random/","action":"deny","reason":"later deny"}]}
JSON
CP_OVERLAP="$(copilot_decision '{"toolName":"create","toolArgs":{"path":"/p/Vault/Random/x.md"}}')"
[[ "$CP_OVERLAP" == deny ]] \
  && ok "Copilot warn continues to later deny" \
  || fail "Copilot warn continues to later deny"
rm -f "$HOME_DIR/.asha/policies.json"
OC_WARN_ERR="$WORK/opencode-warn.err"
printf '%s' '{"tool_name":"write","args":{"path":"/p/Vault/Random/x.md"}}' \
  | HOME="$HOME_DIR" "$HANDLERS/opencode-policy-adapter.sh" >/dev/null 2>"$OC_WARN_ERR"
OC_WARN_RC=$?
[[ $OC_WARN_RC -eq 0 && "$(cat "$OC_WARN_ERR")" == *"WARNING by Asha policy [vault-structure]"* ]] \
  && ok "OpenCode adapter preserves warn awareness whilst allowing" \
  || fail "OpenCode adapter preserves warn awareness whilst allowing"
cat > "$HOME_DIR/.asha/policies.json" <<'JSON'
{"rules":[{"id":"deny-random-vault","tool":"Write","file_path_regex":"/Vault/Random/","action":"deny","reason":"later deny"}]}
JSON
printf '%s' '{"tool_name":"write","args":{"path":"/p/Vault/Random/x.md"}}' \
  | HOME="$HOME_DIR" "$HANDLERS/opencode-policy-adapter.sh" >/dev/null 2>"$WORK/oc-overlap.err"
OC_OVERLAP_RC=$?
[[ $OC_OVERLAP_RC -eq 2 ]] \
  && ok "OpenCode warn continues to later deny" \
  || fail "OpenCode warn continues to later deny"
rm -f "$HOME_DIR/.asha/policies.json"

COVERED="$(jq -r '[.hooks.PreToolUse[] | select(any(.hooks[]?; ((.command // "") | test("policy-guard\\.sh$")))) | (.matcher // "*")] | join("|")' "$HOOKS")"
REACHABLE=1
while IFS= read -r tool; do
  IFS='|' read -ra tokens <<< "$tool"
  for token in "${tokens[@]}"; do
    [[ "|$COVERED|" == *"|$token|"* || "|$COVERED|" == *"|*|"* ]] || REACHABLE=0
  done
done < <(jq -r '.rules[].tool' "$REPO_ROOT/plugins/session/hooks/policies/rules.json")
[[ $REACHABLE -eq 1 ]] \
  && ok "policy guard remains reachable for every declared tool" \
  || fail "policy guard remains reachable for every declared tool"
jq -e '.hooks.PreToolUse[] | select((._asha_harnesses // []) | index("codex"))
  | select((.matcher // "") | test("Edit|Write|apply_patch"))
  | any(.hooks[]?; (.command // "") | test("policy-guard\\.sh$"))' "$HOOKS" >/dev/null 2>&1 \
  && ok "Codex apply_patch alias reaches published-Memory policy" \
  || fail "Codex apply_patch alias reaches published-Memory policy"
jq -e '.hooks.Stop[]
  | select(._asha_harnesses == ["claude", "codex"])
  | any(.hooks[]?; (.command // "") | endswith("verify-pass-complete.sh"))' \
  "$HOOKS" >/dev/null 2>&1 \
  && ok "declared verification pass is registered only on Claude/Codex Stop" \
  || fail "declared verification pass is registered only on Claude/Codex Stop"

CONTROL_REACHABLE=1
while read -r native _control_event; do
  jq -e --arg native "$native" '.hooks[$native][]
    | any(.hooks[]?; (.command // "")
      | endswith("control-event.sh " + $native))' "$HOOKS" >/dev/null 2>&1 \
    || CONTROL_REACHABLE=0
done <<'EOF'
SessionStart session-start
UserPromptSubmit prompt-submitted
PostToolUse tool-completed
PermissionRequest permission-requested
Stop turn-stopped
SessionEnd session-ended
EOF
jq -e '.hooks.PostToolUseFailure[] | select(._asha_harnesses == ["claude"])
    | any(.hooks[]?; (.command // "") | endswith("control-event.sh PostToolUseFailure"))' "$HOOKS" >/dev/null 2>&1 \
  || CONTROL_REACHABLE=0
[[ $CONTROL_REACHABLE -eq 1 ]] \
  && ok "Control event handler is reachable from every registered native event" \
  || fail "Control event handler is reachable from every registered native event"

CONTROL_HANDLER="$HANDLERS/control-event.sh"
if grep -Fq '[[ -t 0 ]] || IFS= read -r -N 4096 INPUT || true' "$CONTROL_HANDLER" \
  && grep -Fq 'timeout --signal=TERM 15 "$ASHA_CMD" "${ARGS[@]}"' "$CONTROL_HANDLER"; then
  ok "Control event bridge guards tty input and bounds controller time"
else
  fail "Control event bridge guards tty input and bounds controller time"
fi
CONTROL_OUTPUT="$(timeout 2 env ASHA_CONTROL_MANAGED=1 ASHA_ROOT="$REPO_ROOT" \
  "$CONTROL_HANDLER" PostToolUse </dev/null)"
CONTROL_STATUS=$?
[[ $CONTROL_STATUS -eq 0 && "$CONTROL_OUTPUT" == '{}' ]] \
  && ok "Control event bridge remains fail-open with empty stdin" \
  || fail "Control event bridge remains fail-open with empty stdin"

FAKE_CONTROL_ROOT="$WORK/fake-control-root"
CONTROL_CAPTURE="$WORK/permission-requested.args"
mkdir -p "$FAKE_CONTROL_ROOT/bin"
cat > "$FAKE_CONTROL_ROOT/bin/asha" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" > "$CONTROL_CAPTURE"
[[ -z "${CONTROL_STUB_OUTPUT:-}" ]] || printf '%s\n' "$CONTROL_STUB_OUTPUT"
EOF
chmod +x "$FAKE_CONTROL_ROOT/bin/asha"
printf '%s' '{"session_id":"permission-live-gate"}' \
  | timeout 2 env ASHA_CONTROL_MANAGED=1 ASHA_ROOT="$FAKE_CONTROL_ROOT" \
      ASHA_HARNESS=codex CONTROL_CAPTURE="$CONTROL_CAPTURE" \
      "$CONTROL_HANDLER" PermissionRequest >/dev/null
[[ -f "$CONTROL_CAPTURE" \
   && "$(cat "$CONTROL_CAPTURE")" == *"control event --event permission-requested"* \
   && "$(cat "$CONTROL_CAPTURE")" == *"--harness codex"* \
   && "$(cat "$CONTROL_CAPTURE")" == *"--session-id permission-live-gate"* ]] \
  && ok "Codex PermissionRequest maps to the bounded Control event" \
  || fail "Codex PermissionRequest maps to the bounded Control event"

CONTROL_BLOCK='{"decision":"block","reason":"Control wake test"}'
CONTROL_OUTPUT="$(timeout 2 env ASHA_CONTROL_MANAGED=1 ASHA_ROOT="$FAKE_CONTROL_ROOT" \
  CONTROL_CAPTURE="$CONTROL_CAPTURE" CONTROL_STUB_OUTPUT="$CONTROL_BLOCK" \
  "$CONTROL_HANDLER" Stop </dev/null)"
[[ "$CONTROL_OUTPUT" == "$CONTROL_BLOCK" ]] \
  && ok "Control Stop bridge passes through a valid single-line block decision" \
  || fail "Control Stop bridge passes through a valid single-line block decision"
CONTROL_OUTPUT="$(timeout 2 env ASHA_CONTROL_MANAGED=1 ASHA_ROOT="$FAKE_CONTROL_ROOT" \
  CONTROL_CAPTURE="$CONTROL_CAPTURE" CONTROL_STUB_OUTPUT='not json' \
  "$CONTROL_HANDLER" Stop </dev/null)"
[[ "$CONTROL_OUTPUT" == '{}' ]] \
  && ok "Control Stop bridge degrades invalid controller output to an empty object" \
  || fail "Control Stop bridge degrades invalid controller output to an empty object"

if jq -e '[.hooks.SessionStart[], .hooks.UserPromptSubmit[], .hooks.PostToolUse[]]
    | all(.[] | select(any(.hooks[]?; (.command // "") | contains("control-event.sh")));
          has("_asha_harnesses") | not)' "$HOOKS" >/dev/null 2>&1 \
  && jq -e '.hooks.Stop[]
    | select(any(.hooks[]?; (.command // "") | contains("control-event.sh")))
    | ._asha_harnesses == ["claude", "codex"]' "$HOOKS" >/dev/null 2>&1 \
  && jq -e '.hooks.PermissionRequest[]
    | select(any(.hooks[]?; (.command // "") | contains("control-event.sh")))
    | ._asha_harnesses == ["codex"]' "$HOOKS" >/dev/null 2>&1 \
  && jq -e '.hooks.SessionEnd[]
    | select(any(.hooks[]?; (.command // "") | contains("control-event.sh")))
    | ._asha_harnesses == ["claude"]' "$HOOKS" >/dev/null 2>&1; then
  ok "cross-harness and Claude-only Control groups carry the exact tags"
else
  fail "cross-harness and Claude-only Control groups carry the exact tags"
fi

asha_harness_home() { printf '%s\n' "$WORK/codex-render-home"; }
# shellcheck source=../harnesses/codex.sh
source "$REPO_ROOT/harnesses/codex.sh"
CODEX_RENDER="$(_codex_emit_hooks_for_plugin \
  "$REPO_ROOT/plugins/session" "$HOOKS" session 2>/dev/null)"
[[ "$CODEX_RENDER" == *"control-event.sh SessionStart"* \
   && "$CODEX_RENDER" == *"control-event.sh UserPromptSubmit"* \
   && "$CODEX_RENDER" == *"control-event.sh PostToolUse"* \
   && "$CODEX_RENDER" == *"control-event.sh PermissionRequest"* \
   && "$CODEX_RENDER" == *"control-event.sh Stop"* \
   && "$CODEX_RENDER" == *"verify-pass-complete.sh"* \
   && "$CODEX_RENDER" != *"control-event.sh SessionEnd"* \
   && "$CODEX_RENDER" == *"[[hooks.PermissionRequest]]"* \
   && "$CODEX_RENDER" == *"[[hooks.Stop]]"* \
   && "$CODEX_RENDER" != *"[[hooks.SessionEnd]]"* ]] \
  && ok "Codex renderer includes the five live-proven Control events" \
  || fail "Codex renderer includes the five live-proven Control events"

TEST_HOOKS="$REPO_ROOT/plugins/test/hooks/hooks.json"
if jq -e '.hooks | has("PermissionRequest") | not' "$TEST_HOOKS" >/dev/null 2>&1 \
    && [[ ! -e "$REPO_ROOT/plugins/test/hooks/permission-request-probe.sh" ]]; then
  ok "temporary Codex PermissionRequest probe is retired"
else
  fail "temporary Codex PermissionRequest probe is retired"
fi

echo "--- Installer canary hook ---"
CANARY_HOOK="$REPO_ROOT/plugins/test/hooks/stop.sh"
LEGACY_CANARY_MARKER="/tmp/asha-marketplace-test-hook-fired"
legacy_canary_state() {
  if [[ -e "$LEGACY_CANARY_MARKER" || -L "$LEGACY_CANARY_MARKER" ]]; then
    printf 'present '
    cksum "$LEGACY_CANARY_MARKER" 2>/dev/null || ls -ld "$LEGACY_CANARY_MARKER"
  else
    printf 'absent\n'
  fi
}
LEGACY_CANARY_BEFORE="$(legacy_canary_state)"

CANARY_STDOUT="$WORK/canary-writable.stdout"
CANARY_MARKER="$WORK/canary-marker"
CANARY_RC=0
CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/test" ASHA_CANARY_MARKER="$CANARY_MARKER" \
  "$CANARY_HOOK" >"$CANARY_STDOUT" 2>"$WORK/canary-writable.stderr" || CANARY_RC=$?
[[ $CANARY_RC -eq 0 && "$(wc -c < "$CANARY_STDOUT")" -eq 3 \
   && "$(cat "$CANARY_STDOUT")" == '{}' && -s "$CANARY_MARKER" ]] \
  && ok "canary Stop hook writes an explicit marker and returns exactly {}" \
  || fail "canary Stop hook writes an explicit marker and returns exactly {}"

CANARY_STDOUT="$WORK/canary-unwritable.stdout"
CANARY_RC=0
CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/test" \
  ASHA_CANARY_MARKER="$WORK/missing/dir/marker" \
  "$CANARY_HOOK" >"$CANARY_STDOUT" 2>"$WORK/canary-unwritable.stderr" || CANARY_RC=$?
[[ $CANARY_RC -eq 0 && "$(wc -c < "$CANARY_STDOUT")" -eq 3 \
   && "$(cat "$CANARY_STDOUT")" == '{}' ]] \
  && ok "canary Stop hook fails open when its marker is unwritable" \
  || fail "canary Stop hook fails open when its marker is unwritable"

mkdir -p "$WORK/xdg"
CANARY_STDOUT="$WORK/canary-xdg.stdout"
CANARY_RC=0
env -u ASHA_CANARY_MARKER CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/test" \
  XDG_RUNTIME_DIR="$WORK/xdg" "$CANARY_HOOK" \
  >"$CANARY_STDOUT" 2>"$WORK/canary-xdg.stderr" || CANARY_RC=$?
[[ $CANARY_RC -eq 0 && "$(wc -c < "$CANARY_STDOUT")" -eq 3 \
   && "$(cat "$CANARY_STDOUT")" == '{}' \
   && -s "$WORK/xdg/asha-canary-hook-fired" ]] \
  && ok "canary Stop hook uses the caller-owned XDG runtime directory" \
  || fail "canary Stop hook uses the caller-owned XDG runtime directory"

LEGACY_CANARY_AFTER="$(legacy_canary_state)"
[[ "$LEGACY_CANARY_AFTER" == "$LEGACY_CANARY_BEFORE" \
   && -z "$(grep -F "$LEGACY_CANARY_MARKER" "$CANARY_HOOK")" ]] \
  && ok "canary Stop hook neither names nor changes the legacy shared marker" \
  || fail "canary Stop hook neither names nor changes the legacy shared marker"

# shellcheck source=../lib/install.sh
source "$REPO_ROOT/lib/install.sh"
DEFAULT_SELECTED="$(ONLY="" WITH_CANARY=0 selected_plugins)"
DEFAULT_ALL="$(ONLY="" WITH_CANARY=0 all_plugin_dirs)"
CANARY_SELECTED="$(ONLY="" WITH_CANARY=1 selected_plugins)"
CANARY_ALL="$(ONLY="" WITH_CANARY=1 all_plugin_dirs)"
ONLY_SELECTED="$(ONLY=test WITH_CANARY=0 selected_plugins)"
ONLY_ALL="$(ONLY=test WITH_CANARY=0 all_plugin_dirs)"
if ! grep -Fxq test <<<"$DEFAULT_SELECTED" && ! grep -Fxq test <<<"$DEFAULT_ALL"; then
  ok "default plugin enumeration excludes the canary"
else
  fail "default plugin enumeration excludes the canary"
fi
if grep -Fxq test <<<"$CANARY_SELECTED" && grep -Fxq test <<<"$CANARY_ALL"; then
  ok "WITH_CANARY=1 includes the canary in plugin enumeration"
else
  fail "WITH_CANARY=1 includes the canary in plugin enumeration"
fi
if grep -Fxq test <<<"$ONLY_SELECTED" && grep -Fxq test <<<"$ONLY_ALL"; then
  ok "ONLY=test includes the canary in scoped and global enumeration"
else
  fail "ONLY=test includes the canary in scoped and global enumeration"
fi

# U8: real adapter entry points in isolated HOME. No parser monkeypatches.
if python3 - "$REPO_ROOT" "$WORK" <<'PY_U8'
import hashlib, json, os, pathlib, re, shutil, site, stat, subprocess, sys, tempfile, unittest
tomllib = __import__("tomllib" if sys.version_info >= (3, 11) else "tomli")
ROOT, WORK = map(pathlib.Path, sys.argv[1:])
START = '# ===== asha:start (managed by asha installer; do not edit) ====='
END = '# ===== asha:end ====='
ENV = {'PATH': os.environ['PATH'], 'USER': os.environ.get('USER', 'test'),
       'PYTHONPATH': site.getusersitepackages()}

def snapshot(home):
    result = {}
    for p in sorted(home.rglob('*')):
        s = p.lstat()
        result[str(p.relative_to(home))] = (s.st_mode, s.st_uid, s.st_gid,
            os.readlink(p) if p.is_symlink() else
            None if p.is_dir() else p.read_bytes())
    return result

class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        (self.home / '.codex').mkdir()
        self.config = self.home / '.codex/config.toml'
        self.config.write_bytes(b'# original\n')
        self.config.chmod(0o640)
        self.hooks = self.home/'.codex/hooks.json'
        self.manifest = self.home/'.asha/install-manifests/codex.json'

    def adapter(self, statement='codex_install_hooks', extra=''):
        script = ('set -euo pipefail; source "$1/lib/install.sh"; '
                  'DRY_RUN=0; FORCE=0; VERBOSE=0; ONLY=test; WITH_CANARY=0; '
                  'source "$1/harnesses/codex.sh"; ' + extra + statement)
        return subprocess.run(['bash', '-c', script, 'u8', str(ROOT)],
            cwd=ROOT, env=dict(ENV, HOME=str(self.home)), capture_output=True, timeout=120)

    def block(self):
        p = self.adapter('_codex_build_hook_block')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        return p.stdout.decode()

    def foreign(self, raw, block):
        data = tomllib.loads(raw.decode())
        for event, groups in tomllib.loads(block).get('hooks', {}).items():
            if event in data.get('hooks', {}):
                data['hooks'][event] = [g for g in data['hooks'][event] if g not in groups]
                if not data['hooks'][event]: del data['hooks'][event]
        if not data.get('hooks'): data.pop('hooks', None)
        return data

    def test_raw_parsed_split_trust_mcp_and_reinstall(self):
        block = self.block()
        prefix = ('# foreign root\n"features"."hooks" = false\n'
                  'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n'
                  'description = """fake header\n[hooks.state.fake]\n' + START + '\n' + END + '\n"""\n')
        inside = ('# foreign inside\n[mcp_servers."play\\u0077right"]\n'
                  'command = "npx"\nargs = ["a", ["b", "c"]]\n'
                  '[hooks."state"."inside.slot"]\ntrusted_hash = "inside-hash"\n')
        outside = ('[hooks.state."outside.slot"]\ntrusted_hash = "outside-hash"\n'
                   '[mcp_servers.outside]\ncommand = "foreign"\n# trailing\n\n\n')
        decorated = ''.join(line.rstrip('\n') + '  # retained inline comment\n'
                            if line.startswith(('[[hooks.', 'command =')) else line
                            for line in block.splitlines(keepends=True))
        raw = (prefix + decorated.replace(END, inside + END) + outside).replace('\n', '\r\n').encode()
        self.config.write_bytes(raw)
        before = self.foreign(raw, block)
        p = self.adapter()
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        for span in (prefix, inside, outside):
            self.assertIn(span.replace('\n', '\r\n').encode(), self.config.read_bytes())
        self.assertEqual(self.foreign(self.config.read_bytes(), block), before)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.config.read_bytes().count(b'  # retained inline comment\r\n'),
                         raw.count(b'  # retained inline comment\r\n'))
        state = snapshot(self.home)
        p = self.adapter()
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(snapshot(self.home), state)
        p = subprocess.run([str(ROOT/'uninstall.sh'), '--target', 'codex'], cwd=ROOT,
            env=dict(ENV, HOME=str(self.home)), capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        self.assertIn(b'legacy inline hooks need update/removal', p.stderr)
        self.assertEqual(snapshot(self.home), state)
        self.assertEqual(self.config.read_bytes(), raw)
        self.assertFalse(self.hooks.exists())

    def test_missing_feature_quoted_dotted_and_multiline(self):
        for text in ('["features"] # retained header\r\nother = true\r\n\r\n',
                     'features.other = true\n# end without newline',
                     '[features.nested]\nvalue = [1, [2, 3]]\n',
                     "text = '''literal\n" + START + "\n[features]\n'''''\n"):
            with self.subTest(text=text):
                self.config.write_bytes(text.encode())
                before = tomllib.loads(text)
                p = self.adapter()
                self.assertEqual(p.returncode, 0, p.stderr.decode())
                after = tomllib.loads(self.config.read_text())
                self.assertEqual(after, before)
                self.assertEqual(self.config.read_bytes(), text.encode())
                for key, value in before.items():
                    if key == 'features':
                        self.assertEqual({k:v for k,v in after[key].items() if k != 'hooks'}, value)
                    else: self.assertEqual(after[key], value)
                self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)

    def test_untagged_nested_foreign_hook_inside_fence(self):
        block = self.block()
        foreign = ('[[hooks.Stop]]\nmatcher = "foreign"\n'
                   '[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "foreign"\n'
                   'args = [["one"], ["two", "three"]]\n# keep foreign comment\n')
        raw = ('features.hooks = false\n' + block.replace(END, foreign + END)).encode()
        self.config.write_bytes(raw)
        p = self.adapter()
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertIn(foreign.encode(), self.config.read_bytes())
        self.assertEqual(self.foreign(raw, block), self.foreign(self.config.read_bytes(), block))

    def test_refusals_preserve_artifacts_backups_and_features(self):
        bad = ['broken = [', 'a=1\na=2\n',
               '[hooks.state.a]\ntrusted_hash="x"\n[hooks.state.a]\ntrusted_hash="y"',
               'hooks.state = []\n', '[hooks.state.a]\ntrusted_hash=1\n',
               '[hooks.state.a]\nenabled="yes"\n',
               START+'\n', END+'\n', START+'\n'+START+'\n'+END+'\n',
               START+'\n'+END+'\n'+START+'\n'+END+'\n', 'features = []\n',
               START+'\n[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ncommand="unknown"\n# asha:unknown\n'+END+'\n']
        artifact = self.home/'.codex/skills/retained/SKILL.md'
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b'foreign artifact\n')
        legacy = self.home/'.codex-asha'
        legacy.mkdir()
        (legacy/'keep').write_bytes(b'legacy\n')
        for text in bad:
            for statement in ('codex_install_hooks', 'codex_install'):
                with self.subTest(text=text, entry=statement):
                    self.config.write_bytes(text.encode())
                    before = snapshot(self.home)
                    p = self.adapter(statement, 'FORCE=1; ')
                    self.assertEqual(p.returncode, 4, p.stderr.decode())
                    self.assertIn(b'preservation refused', p.stderr)
                    self.assertEqual(snapshot(self.home), before)
        self.config.write_bytes(b'bad = [')
        before = snapshot(self.home)
        p = subprocess.run([str(ROOT/'uninstall.sh'), '--target', 'codex'], cwd=ROOT,
            env=dict(ENV, HOME=str(self.home)), capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 1, p.stderr.decode())
        self.assertEqual(snapshot(self.home), before)

    def test_unsafe_identity_and_replacement_refuse(self):
        original = self.config.read_bytes()
        target = self.home/'foreign.toml'
        target.write_bytes(original)
        self.config.unlink()
        self.config.symlink_to(target)
        before = snapshot(self.home)
        p = self.adapter('codex_install', 'FORCE=1; ')
        self.assertEqual(p.returncode, 4, p.stderr.decode())
        self.assertEqual(snapshot(self.home), before)
        self.config.unlink()
        os.mkfifo(self.config)
        p = self.adapter()
        self.assertEqual(p.returncode, 4, p.stderr.decode())
        self.assertTrue(stat.S_ISFIFO(self.config.lstat().st_mode))
        self.config.unlink()
        os.link(target, self.config)
        p = self.adapter()
        self.assertEqual(p.returncode, 4, p.stderr.decode())
        self.config.unlink()
        self.config.write_bytes(original)
        linked = self.home/'linked-native'
        linked.symlink_to(self.config.parent)
        before = snapshot(self.home)
        p = self.adapter('codex_install', 'CODEX_CONFIG_FILE="$HOME/linked-native/config.toml"; ')
        self.assertEqual(p.returncode, 4, p.stderr.decode())
        self.assertEqual(snapshot(self.home), before)
    def test_dry_run_and_conditional_call_refusal(self):
        before = snapshot(self.home)
        p = self.adapter('codex_install_hooks', 'DRY_RUN=1; ')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(snapshot(self.home), before)
        self.config.write_bytes(b'bad = [')
        before = snapshot(self.home)
        p = self.adapter('if codex_install; then exit 99; else exit $?; fi')
        self.assertEqual(p.returncode, 4, p.stderr.decode())
        self.assertEqual(snapshot(self.home), before)

    def test_owned_positive_full_partial_update_and_uninstall(self):
        raw = self.config.read_bytes()
        p = self.adapter('codex_install')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        first = snapshot(self.home)
        rows = json.loads(self.manifest.read_bytes())['artifacts']
        row = next(r for r in rows if r['type'] == 'codex-hooks-json')
        self.assertEqual(row['source'], str(ROOT/'harnesses/codex.sh'))
        self.assertEqual(row['sha256'], hashlib.sha256(self.hooks.read_bytes()).hexdigest())
        self.assertNotIn('state', json.loads(self.hooks.read_bytes())['hooks'])
        p = self.adapter()
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(snapshot(self.home), first)
        # --only scopes primitives; the inherited global hook policy retains
        # every nonoptional namespace and includes explicitly selected canary.
        self.assertIn(b'/plugins/test/', self.hooks.read_bytes())
        p = self.adapter('codex_install_hooks', 'ONLY=admin; ')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertNotIn(b'/plugins/test/', self.hooks.read_bytes())
        self.assertIn(b'/plugins/session/', self.hooks.read_bytes())
        self.assertEqual([r for r in json.loads(self.manifest.read_bytes())['artifacts']
                          if r['type'] != 'codex-hooks-json'],
                         [r for r in rows if r['type'] != 'codex-hooks-json'])
        p = self.adapter('codex_install_hooks', 'ONLY=admin; WITH_CANARY=1; ')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertIn(b'/plugins/test/', self.hooks.read_bytes())
        p = subprocess.run([str(ROOT/'uninstall.sh'), '--target', 'codex'], cwd=ROOT,
            env=dict(ENV, HOME=str(self.home)), capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertFalse(self.hooks.exists())
        self.assertFalse(self.manifest.exists())
        self.assertEqual(self.config.read_bytes(), raw)

    def test_absent_native_config_empty_selection_and_caller_state(self):
        self.config.unlink()
        p = self.adapter('''
            ASHA_ARTIFACT_HARNESS=caller; ASHA_ARTIFACT_STAGE="$HOME/caller-stage";
            printf 'retained' > "$ASHA_ARTIFACT_STAGE";
            old_flags=$-; old_shell=$(set +o); old_shopt=$(shopt -p);
            codex_install_hooks; codex_install_hooks;
            [[ $ASHA_ARTIFACT_HARNESS == caller && $ASHA_ARTIFACT_STAGE == "$HOME/caller-stage" ]];
            [[ $(cat "$ASHA_ARTIFACT_STAGE") == retained && $old_flags == "$-" ]];
            [[ $old_shell == "$(set +o)" && $old_shopt == "$(shopt -p)" ]];
            [[ $FORCE == 1 && $ONLY == test ]];
        ''', 'FORCE=1; ')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertFalse(self.config.exists())
        # Real empty plugin source selection, not a mocked validator.
        empty = self.home/'empty-plugins'; empty.mkdir()
        p = self.adapter('codex_install_hooks', 'PLUGINS_DIR="$HOME/empty-plugins"; ')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(json.loads(self.hooks.read_bytes()), {'hooks': {}})
        self.assertFalse(self.config.exists())

    def test_hook_ownership_refuses_foreign_identical_modified_force_and_dryrun(self):
        p = self.adapter()
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        ledger, content = self.manifest.read_bytes(), self.hooks.read_bytes()
        for kind in ('unrecorded-identical', 'modified', 'malformed-json', 'duplicate-json', 'symlink'):
            for flags in ('', 'FORCE=1; ', 'DRY_RUN=1; FORCE=1; '):
                with self.subTest(kind=kind, flags=flags):
                    if self.hooks.is_symlink(): self.hooks.unlink()
                    self.hooks.write_bytes(content); self.manifest.write_bytes(ledger)
                    if kind == 'unrecorded-identical': self.manifest.unlink()
                    elif kind == 'modified': self.hooks.write_bytes(content+b' ')
                    elif kind == 'malformed-json': self.hooks.write_bytes(b'[')
                    elif kind == 'duplicate-json': self.hooks.write_bytes(b'{"hooks":{},"hooks":{}}')
                    elif kind == 'symlink':
                        self.hooks.unlink(); self.hooks.symlink_to(self.config)
                    before = snapshot(self.home)
                    p = self.adapter('codex_install', flags)
                    self.assertEqual(p.returncode, 4, p.stderr.decode())
                    self.assertEqual(snapshot(self.home), before)
                    p = subprocess.run([str(ROOT/'uninstall.sh'), '--target', 'codex'], cwd=ROOT,
                        env=dict(ENV, HOME=str(self.home)), capture_output=True, timeout=120)
                    self.assertEqual(p.returncode, 1, p.stderr.decode())
                    self.assertEqual(snapshot(self.home), before)

    def test_all_consumed_manifest_rows_are_structurally_safe(self):
        p = self.adapter()
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        ledger = json.loads(self.manifest.read_bytes())
        row = ledger['artifacts'][0]
        cases = [None, [], {'artifacts': []}, dict(ledger, harness='other'),
                 dict(ledger, artifacts=[row, row]), dict(ledger, artifacts=[None])]
        for key, value in [('source', str(ROOT/'wrong')), ('type', 'wrong'),
                           ('destination', str(self.config)), ('orphan', True),
                           ('sha256', 'bad')]:
            cases.append(dict(ledger, artifacts=[dict(row, **{key:value})]))
        cases.append(dict(ledger, artifacts=[row, dict(row, destination='../escape')]))
        cases.append(dict(ledger, artifacts=[row, dict(row, type='codex-command-skill',
                                                      destination=str(self.config), source=str(ROOT/'missing'))]))
        for case in cases:
            with self.subTest(case=case):
                self.manifest.write_text(json.dumps(case))
                before = snapshot(self.home)
                p = self.adapter('codex_install', 'FORCE=1; ')
                self.assertEqual(p.returncode, 4, p.stderr.decode())
                self.assertEqual(snapshot(self.home), before)
        self.manifest.write_text('{"artifacts":[],"artifacts":[]}')
        before = snapshot(self.home)
        p = self.adapter()
        self.assertEqual(p.returncode, 4, p.stderr.decode())
        self.assertEqual(snapshot(self.home), before)

    def test_legacy_selection_root_and_json_duplication_refuse(self):
        block = self.block()
        for raw in (block.replace(str(ROOT), '/old/root'),
                    block.replace('env ASHA_HARNESS=codex ', ''),
                    block.replace('# asha:session', '# asha:unknown'),
                    block.replace(END, block + END)):
            self.config.write_text(raw)
            before = snapshot(self.home)
            p = self.adapter('codex_install', 'FORCE=1; ')
            self.assertEqual(p.returncode, 4, p.stderr.decode())
            self.assertEqual(snapshot(self.home), before)
        self.config.write_text(block)
        before = snapshot(self.home)
        p = self.adapter('codex_install', 'ONLY=admin; ')
        self.assertEqual(p.returncode, 4, p.stderr.decode())
        self.assertEqual(snapshot(self.home), before)
        # A matching legacy hook operation is genuinely no-op even in full
        # install; independently requested skills/agents are still allowed.
        p = self.adapter('codex_install')
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertFalse(self.hooks.exists())
        self.assertEqual(self.config.read_text(), block)
        self.config.write_text('')
        p = self.adapter()
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.config.write_text(block)
        before = snapshot(self.home)
        p = self.adapter()
        self.assertEqual(p.returncode, 4, p.stderr.decode())
        self.assertEqual(snapshot(self.home), before)

    def test_fresh_publication_never_clobbers_new_foreign_path(self):
        tools = self.home/'fixture-tools'; tools.mkdir()
        marker = self.home/'publication-seen'
        bridge = tools/'boundary.py'
        bridge.write_text('import os,pathlib,sys\n'
            'sys.argv=sys.argv[1:]\ncode=sys.stdin.read()\n'
            'def audit(event,args):\n'
            '    if event == "os.link" and args[1] == '+repr(str(self.hooks))+':\n'
            '        pathlib.Path(args[1]).write_text("foreign appeared")\n'
            '        pathlib.Path('+repr(str(marker))+').write_text("seen")\n'
            'sys.addaudithook(audit)\nexec(compile(code,"<stdin>","exec"))\n')
        wrapper = tools/'python3'
        wrapper.write_text('#!/bin/bash\nif [[ "$1" == - ]]; then exec '+repr(sys.executable)+' '+repr(str(bridge))+' "$@"; fi\nexec '+repr(sys.executable)+' "$@"\n')
        wrapper.chmod(0o755)
        p = self.adapter('codex_install', 'PATH='+repr(str(tools))+':$PATH; export PATH; ')
        self.assertEqual(p.returncode, 4, p.stderr.decode())
        self.assertEqual(marker.read_text(), 'seen')
        self.assertEqual(self.hooks.read_text(), 'foreign appeared')
        self.assertFalse(self.manifest.exists())
        self.assertFalse((self.home/'.codex/skills').exists())

    def test_native_replacement_and_inplace_saves_survive_owned_publication(self):
        # f491's red last-config-rename test and receipt remain predecessor
        # evidence. That writer no longer exists. This fixture performs actual
        # native I/O at the NEW owned-artifact boundary, never mocks validation.
        tools = self.home/'fixture-tools'; tools.mkdir()
        marker = self.home/'native-save-seen'
        forbidden = self.home/'installer-config-write'
        bridge = tools/'boundary.py'
        concurrent = (b'# native save\r\nfeatures.hooks=false\r\n'
                      b'[mcp_servers.concurrent]\r\ncommand="keep"\r\n'
                      b'[hooks.state.concurrent]\r\ntrusted_hash="native"\r\n\r\n')
        bridge.write_text('import os,pathlib,sys\n'
            'sys.argv=sys.argv[1:]\ncode=sys.stdin.read()\n'
            'active=False\n'
            'config='+repr(str(self.config))+'\nhooks='+repr(str(self.hooks))+'\n'
            'def audit(event,args):\n'
            '    global active\n'
            '    if active: return\n'
            '    if event == "open" and args[0] == config and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC):\n'
            '        pathlib.Path('+repr(str(forbidden))+').write_text("forbidden")\n'
            '        raise RuntimeError("installer attempted config write")\n'
            '    if event in ("os.rename","os.remove","os.chmod","os.chown") and config in args[:2]:\n'
            '        pathlib.Path('+repr(str(forbidden))+').write_text("forbidden")\n'
            '        raise RuntimeError("installer attempted config mutation")\n'
            '    if ((event in ("os.link","os.rename") and args[1] == hooks) or (event == "os.remove" and args[0] == hooks)):\n'
            '        active=True\n'
            '        if os.environ.get("NATIVE_SAVE") == "replace":\n'
            '            replacement=pathlib.Path(config+".native")\n'
            '            replacement.write_bytes('+repr(concurrent)+')\n'
            '            replacement.chmod(0o640)\n'
            '            os.replace(replacement,config)\n'
            '        else: pathlib.Path(config).write_bytes('+repr(concurrent)+')\n'
            '        pathlib.Path('+repr(str(marker))+').write_text("seen")\n'
            '        active=False\n'
            'sys.addaudithook(audit)\nexec(compile(code,"<stdin>","exec"))\n')
        wrapper = tools/'python3'
        wrapper.write_text('#!/bin/bash\nif [[ "$1" == - ]]; then exec '+repr(sys.executable)+' '+repr(str(bridge))+' "$@"; fi\nexec '+repr(sys.executable)+' "$@"\n')
        wrapper.chmod(0o755)
        for mode, only in [('replace', 'test'), ('inplace', 'admin')]:
            with self.subTest(mode=mode):
                self.config.write_bytes(b'# before native save\n')
                p = self.adapter('codex_install', 'ONLY='+only+'; NATIVE_SAVE='+mode+'; export NATIVE_SAVE; PATH='+repr(str(tools))+':$PATH; export PATH; ')
                self.assertEqual(p.returncode, 0, p.stderr.decode())
                self.assertEqual(marker.read_text(), 'seen')
                marker.unlink()
                self.assertEqual(self.config.read_bytes(), concurrent)
                self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)
                self.assertEqual(tomllib.loads(self.config.read_text()), tomllib.loads(concurrent.decode()))
                self.assertFalse(forbidden.exists())
                self.assertEqual(list(self.config.parent.glob('config.toml.bak-*')), [])
        p = subprocess.run([str(ROOT/'uninstall.sh'), '--target', 'codex'], cwd=ROOT,
            env=dict(ENV, HOME=str(self.home), PATH=str(tools)+':'+ENV['PATH'], NATIVE_SAVE='replace'),
            capture_output=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr.decode())
        self.assertEqual(marker.read_text(), 'seen')
        self.assertEqual(self.config.read_bytes(), concurrent)
        self.assertFalse(forbidden.exists())

    def test_full_process_trace_proves_no_config_write_syscall(self):
        tracer = shutil.which('strace')
        self.assertIsNotNone(tracer, 'missing syscall tracer blocks config-write evidence')
        trace = self.home/'syscalls'
        env = dict(ENV, HOME=str(self.home))
        raw = self.config.read_bytes()
        for args in ([str(ROOT/'install.sh'), '--target', 'codex', '--only', 'test'],
                     [str(ROOT/'install.sh'), '--target', 'codex', '--only', 'admin'],
                     [str(ROOT/'uninstall.sh'), '--target', 'codex']):
            p = subprocess.run([tracer, '-f', '-qq', '-s', '4096', '-e', 'trace=%file',
                                '-o', str(trace), *args], cwd=ROOT, env=env,
                               capture_output=True, timeout=180)
            self.assertEqual(p.returncode, 0, p.stderr.decode())
            rows = [line for line in trace.read_text().splitlines()
                    if '"'+str(self.config)+'"' in line and 'execve(' not in line]
            self.assertTrue(any('O_RDONLY' in line for line in rows), 'must observe actual config reads')
            forbidden = [line for line in rows if re.search(
                r'O_WRONLY|O_RDWR|O_CREAT|O_TRUNC|rename\w*\(|unlink\w*\(|chmod\w*\(|chown\w*\(', line)]
            self.assertEqual(forbidden, [])
            self.assertEqual(self.config.read_bytes(), raw)
            self.assertEqual(self.config.stat().st_mode & 0o777, 0o640)

unittest.main(argv=['u8-hooks'], verbosity=2)
PY_U8
then ok "U8 raw/parsed/trust/identity preservation through real Codex entry points"
else fail "U8 raw/parsed/trust/identity preservation through real Codex entry points"
fi

echo "test-hooks: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
