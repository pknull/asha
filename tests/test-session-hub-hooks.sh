#!/usr/bin/env bash
# Session-hub hook contract.
#
# Two independent obligations:
#   1. Asha's own context hooks (Memory, recovery, workspace, learnings, nudges)
#      go quiet under ASHA_SESSION_PROFILE=worker. A worker runs the caller's
#      repository and native harness, not an Asha-managed session.
#   2. control-event.sh still bridges hub sessions. The worker early exits above
#      must never reach it: a hub session is recognized by ASHA_HUB_SESSION_ID
#      before anything else, the whole bridge is bounded to under a second, and
#      it always answers with a harmless empty object.
set -uo pipefail

# Sandbox hermeticity: an operator shell exporting these must not leak in.
unset ASHA_HOME XDG_STATE_HOME XDG_DATA_HOME ASHA_SESSION_PROFILE \
      ASHA_HUB_SESSION_ID ASHA_HUB_GENERATION ASHA_CONTROL_MANAGED 2>/dev/null || true

REPO_ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HANDLERS="$REPO_ROOT/plugins/session/hooks/handlers"
CONTROL_HANDLER="$HANDLERS/control-event.sh"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
PASS=0 FAIL=0
ok() { echo "  ✓ $1"; PASS=$((PASS + 1)); }
fail() { echo "  ✗ $1" >&2; FAIL=$((FAIL + 1)); }

PROJECT="$WORK/project"
HOME_DIR="$WORK/home"
mkdir -p "$PROJECT/.asha" "$PROJECT/Memory" "$PROJECT/Work/markers" "$HOME_DIR/.asha"
printf '{"initialized":true,"memory_version":2,"project_id":"project-test"}\n' > "$PROJECT/.asha/config.json"
printf '# Objective\nO\n# State\nS\n# Next\n- N\n# Blockers\n- None\n' > "$PROJECT/Memory/activeContext.md"
printf '# Decisions\n- D\n' > "$PROJECT/Memory/decisions.md"
printf 'operation-v2-sentinel\n' > "$HOME_DIR/.asha/operation.md"

# $1 handler, $2 payload, rest: extra environment assignments. The operator's
# own harness identity is scrubbed so no ambient session ID can stand in for
# the payload's.
run_hook() {
  local hook="$1" payload="$2"
  shift 2
  printf '%s' "$payload" | env -u CLAUDE_CODE_SESSION_ID -u ASHA_SESSION_ID \
    -u TMUX_PANE HOME="$HOME_DIR" CLAUDE_PROJECT_DIR="$PROJECT" \
    CLAUDE_PLUGIN_ROOT="$REPO_ROOT/plugins/session" ASHA_HARNESS=claude "$@" \
    "$HANDLERS/$hook" 2>/dev/null
}

echo "--- worker profile silences Asha's own context hooks ---"

START_PAYLOAD='{"session_id":"hub-start","cwd":"'"$PROJECT"'"}'
BASELINE="$(run_hook session-start.sh "$START_PAYLOAD")"
if [[ "$BASELINE" == *operation-v2-sentinel* \
   && "$BASELINE" == *"Published repository Memory v2"* ]]; then
  ok "SessionStart still injects operations and Memory outside a worker"
else
  fail "SessionStart still injects operations and Memory outside a worker"
fi

rm -rf "$PROJECT/Work/session-state"
WORKER_START="$(run_hook session-start.sh "$START_PAYLOAD" ASHA_SESSION_PROFILE=worker)"
if [[ "$WORKER_START" == '{}' ]]; then
  ok "worker SessionStart injects no operations, Memory, workspace or learnings"
else
  fail "worker SessionStart injects no operations, Memory, workspace or learnings ($WORKER_START)"
fi
if [[ ! -e "$PROJECT/Work/session-state/claude-hub-start.json" ]]; then
  ok "worker SessionStart writes no recovery snapshot"
else
  fail "worker SessionStart writes no recovery snapshot"
fi
run_hook session-start.sh "$START_PAYLOAD" >/dev/null
if [[ -e "$PROJECT/Work/session-state/claude-hub-start.json" ]]; then
  ok "non-worker SessionStart still writes its recovery snapshot"
else
  fail "non-worker SessionStart still writes its recovery snapshot"
fi

for profile in chair room; do
  KEPT="$(run_hook session-start.sh "$START_PAYLOAD" "ASHA_SESSION_PROFILE=$profile")"
  if [[ "$KEPT" == *operation-v2-sentinel* ]]; then
    ok "$profile SessionStart keeps Asha context"
  else
    fail "$profile SessionStart keeps Asha context ($KEPT)"
  fi
done

PROMPT_PAYLOAD='{"session_id":"hub-prompt","cwd":"'"$PROJECT"'","prompt":"do the thing"}'
touch "$PROJECT/Work/markers/rp-active"
if [[ "$(run_hook user-prompt-submit.sh "$PROMPT_PAYLOAD" ASHA_SESSION_PROFILE=worker)" == '{}' ]]; then
  ok "worker UserPromptSubmit adds no routing or nudge context"
else
  fail "worker UserPromptSubmit adds no routing or nudge context"
fi
if [[ "$(run_hook user-prompt-submit.sh "$PROMPT_PAYLOAD")" != '{}' ]]; then
  ok "non-worker UserPromptSubmit keeps its marker-gated context"
else
  fail "non-worker UserPromptSubmit keeps its marker-gated context"
fi
rm -f "$PROJECT/Work/markers/rp-active"

POST_PAYLOAD='{"session_id":"hub-post","cwd":"'"$PROJECT"'","tool_name":"Edit"}'
rm -rf "$PROJECT/Work/session-state"
run_hook post-tool-use.sh "$POST_PAYLOAD" ASHA_SESSION_PROFILE=worker >/dev/null
if [[ ! -e "$PROJECT/Work/session-state/claude-hub-post.json" ]]; then
  ok "worker PostToolUse writes no recovery state"
else
  fail "worker PostToolUse writes no recovery state"
fi
run_hook post-tool-use.sh "$POST_PAYLOAD" >/dev/null
if [[ -e "$PROJECT/Work/session-state/claude-hub-post.json" ]]; then
  ok "non-worker PostToolUse still writes recovery state"
else
  fail "non-worker PostToolUse still writes recovery state"
fi

END_PAYLOAD='{"session_id":"hub-end","cwd":"'"$PROJECT"'"}'
rm -rf "$PROJECT/Work/session-state"
run_hook session-end.sh "$END_PAYLOAD" ASHA_SESSION_PROFILE=worker >/dev/null
if [[ ! -e "$PROJECT/Work/session-state/claude-hub-end.json" ]]; then
  ok "worker SessionEnd seals no recovery snapshot"
else
  fail "worker SessionEnd seals no recovery snapshot"
fi
run_hook session-end.sh "$END_PAYLOAD" >/dev/null
if [[ -e "$PROJECT/Work/session-state/claude-hub-end.json" ]]; then
  ok "non-worker SessionEnd still seals its recovery snapshot"
else
  fail "non-worker SessionEnd still seals its recovery snapshot"
fi

# The declared old value has to live inside the project, or the nudge would
# stay silent for every profile and prove nothing.
printf 'stale-old-value\n' > "$PROJECT/still-here.txt"
printf '{"schema_version":1,"old":"stale-old-value","new":"replacement"}\n' \
  > "$PROJECT/Work/markers/pass-declaration.json"
STOP_PAYLOAD='{"session_id":"hub-stop","cwd":"'"$PROJECT"'"}'
if [[ "$(run_hook verify-pass-complete.sh "$STOP_PAYLOAD" ASHA_SESSION_PROFILE=worker)" == '{}' ]]; then
  ok "worker Stop raises no verification nudge"
else
  fail "worker Stop raises no verification nudge"
fi
if [[ "$(run_hook verify-pass-complete.sh "$STOP_PAYLOAD")" == *stale-old-value* ]]; then
  ok "non-worker Stop still raises the verification nudge"
else
  fail "non-worker Stop still raises the verification nudge"
fi
rm -f "$PROJECT/Work/markers/pass-declaration.json" "$PROJECT/still-here.txt"

echo "--- hub sessions reach Control through the bounded bridge ---"

FAKE_ROOT="$WORK/fake-root"
CAPTURE="$WORK/control.args"
mkdir -p "$FAKE_ROOT/bin"
cat > "$FAKE_ROOT/bin/asha" <<'STUB'
#!/usr/bin/env bash
# A bridge-timeout loss record runs detached after the hook returned; keep it
# out of the per-call capture so it can never overwrite a later assertion.
if [[ "$1 $2 $3" == "control session event-lost" ]]; then
  printf '%s\n' "$*" >> "$CONTROL_CAPTURE.lost"
  exit 0
fi
printf '%s\n' "$*" > "$CONTROL_CAPTURE"
[[ -z "${CONTROL_STUB_SLEEP:-}" ]] || sleep "$CONTROL_STUB_SLEEP"
[[ -z "${CONTROL_STUB_OUTPUT:-}" ]] || printf '%s\n' "$CONTROL_STUB_OUTPUT"
STUB
chmod +x "$FAKE_ROOT/bin/asha"

HUB_ID="9d0a8b7c-6e5f-4a3b-8c2d-1e0f9a8b7c6d"

# $1 native hook name, $2 stdin payload, rest: extra environment assignments.
run_control() {
  local event="$1" payload="$2"
  shift 2
  rm -f "$CAPTURE"
  printf '%s' "$payload" | env -u CLAUDE_CODE_SESSION_ID -u ASHA_SESSION_ID \
    -u TMUX_PANE ASHA_ROOT="$FAKE_ROOT" CONTROL_CAPTURE="$CAPTURE" \
    ASHA_HARNESS=claude "$@" "$CONTROL_HANDLER" "$event" 2>/dev/null
}
raw_captured() { cat "$CAPTURE" 2>/dev/null || true; }
# The hook's start time (best-effort close D2) varies per run; checked on its own below.
captured() { raw_captured | sed -E 's/ --emitted-at [0-9]+\.[0-9]+//'; }

OUT="$(run_control SessionStart '{"session_id":"native-abc"}' \
  ASHA_HUB_SESSION_ID="$HUB_ID" ASHA_HUB_GENERATION=2)"
if [[ "$(captured)" == "control session event --event session-start --native-id native-abc" ]]; then
  ok "hub SessionStart calls the session CLI with only event and native id"
else
  fail "hub SessionStart calls the session CLI with only event and native id ($(captured))"
fi
if [[ "$OUT" == '{}' ]]; then
  ok "hub bridge answers with an empty object"
else
  fail "hub bridge answers with an empty object ($OUT)"
fi

BRIDGED=1
for pair in "UserPromptSubmit prompt-submitted" "PreToolUse tool-started" "PostToolUse tool-completed" \
            "PostToolUseFailure tool-completed" \
            "PermissionRequest permission-requested" "Stop turn-stopped" \
            "SessionEnd session-ended"; do
  read -r NATIVE_NAME SESSION_EVENT <<<"$pair"
  # A parsed object without a session id: Stop then carries no guard flag either.
  run_control "$NATIVE_NAME" '{}' ASHA_HUB_SESSION_ID="$HUB_ID" ASHA_HUB_GENERATION=1 >/dev/null
  [[ "$(captured)" == "control session event --event $SESSION_EVENT" ]] || BRIDGED=0
done
if [[ $BRIDGED -eq 1 ]]; then
  ok "every native hook name maps to its session event without a native id"
else
  fail "every native hook name maps to its session event without a native id ($(captured))"
fi

LARGE_TOOL="$(python3 - <<'PY'
import json
print(json.dumps(dict(tool_name='Bash', tool_use_id='final-42', tool_input=dict(command=
    'asha control session handoff --outcome no-durable-update --detail Reviewed --json'), tool_response='x' * 60000)))
PY
)"
# A failed tool (Claude PostToolUseFailure) ends its tool exactly like a success.
run_control PostToolUseFailure "$LARGE_TOOL" ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
FAILED_CAPTURE="$(captured)"
run_control PostToolUse "$LARGE_TOOL" ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
if [[ "$FAILED_CAPTURE" == "$(captured)" && "$(captured)" == "control session event --event tool-completed" ]]; then
  ok "a failed tool callback reports exactly like a successful one, with no tool metadata"
else
  fail "a failed tool callback reports exactly like a successful one ($FAILED_CAPTURE vs $(captured))"
fi
if [[ "$(raw_captured)" != *Reviewed* && "$(raw_captured)" != *handoff* ]]; then
  ok "large tool output forwards neither the command nor its output"
else
  fail "large tool output forwards neither the command nor its output ($(raw_captured))"
fi
BEFORE_S="$(date +%s)"
run_control UserPromptSubmit '{}' ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
STAMP="$(raw_captured | sed -nE 's/.* --emitted-at ([0-9]+)\.[0-9]+( .*)?$/\1/p')"
if [[ -n "$STAMP" && "$STAMP" -ge "$BEFORE_S" && "$STAMP" -le "$(date +%s)" ]]; then
  ok "every hub report forwards when its hook fired (--emitted-at, D2)"
else
  fail "every hub report forwards when its hook fired ($(raw_captured))"
fi

OUT="$(run_control Nonsense '' ASHA_HUB_SESSION_ID="$HUB_ID")"
if [[ "$OUT" == '{}' && ! -e "$CAPTURE" ]]; then
  ok "an unmapped hook name calls nothing and stays harmless"
else
  fail "an unmapped hook name calls nothing and stays harmless ($OUT)"
fi

# The hub identity is checked before the legacy managed-task marker, and the
# worker early exits in Asha's other hooks must not reach this bridge.
run_control Stop '{}' ASHA_HUB_SESSION_ID="$HUB_ID" ASHA_CONTROL_MANAGED=1 \
  ASHA_SESSION_PROFILE=worker >/dev/null
if [[ "$(captured)" == "control session event --event turn-stopped" ]]; then
  ok "a worker-profile hub session branches to the session CLI first"
else
  fail "a worker-profile hub session branches to the session CLI first ($(captured))"
fi

OUT="$(run_control Stop '' ASHA_HUB_SESSION_ID="$HUB_ID" \
  CONTROL_STUB_OUTPUT='{"decision":"block","reason":"stay"}')"
if [[ "$OUT" == '{}' ]]; then
  ok "hub bridge never forwards an arbitrary controller decision or instruction"
else
  fail "hub bridge never forwards an arbitrary controller decision or instruction ($OUT)"
fi

# The one named exception: a pending graceful close request rides back on Stop
# as the harness's own block decision, in exactly one strict shape.
CLOSE_DECISION='{"decision":"block","reason":"Asha Control close request 9d0a8b7c-6e5f-4a3b-8c2d-1e0f9a8b7c6d for session x (generation 1). Leave a handoff."}'
OUT="$(run_control Stop '{"session_id":"native-abc"}' ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_OUTPUT="$CLOSE_DECISION")"
if [[ "$OUT" == "$CLOSE_DECISION" ]]; then
  ok "a pending close request is returned on Stop as the harness's block decision"
else
  fail "a pending close request is returned on Stop as the harness's block decision ($OUT)"
fi

for pair in "PostToolUse" "SessionEnd" "UserPromptSubmit"; do
  OUT="$(run_control "$pair" '' ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_OUTPUT="$CLOSE_DECISION")"
  [[ "$OUT" == '{}' ]] || fail "close decision leaked through $pair ($OUT)"
done
if [[ "$OUT" == '{}' ]]; then
  ok "the close decision is confined to Stop"
fi

for shape in '{"decision":"block","reason":"Asha Control close request","extra":1}' \
             '{"decision":"allow","reason":"Asha Control close request"}' \
             '{"decision":"block","reason":["Asha Control close request"]}' \
             $'{"decision":"block",\n"reason":"Asha Control close request"}' \
             'not json close request'; do
  OUT="$(run_control Stop '' ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_OUTPUT="$shape")"
  [[ "$OUT" == '{}' ]] || fail "malformed close decision leaked on Stop ($shape -> $OUT)"
done
if [[ "$OUT" == '{}' ]]; then
  ok "only the exact single-line block shape naming a close request passes"
fi

START_NS=$(date +%s%N)
OUT="$(run_control PostToolUse '{"session_id":"slow"}' ASHA_HUB_SESSION_ID="$HUB_ID" \
  CONTROL_STUB_SLEEP=10)"
ELAPSED_MS=$(( ($(date +%s%N) - START_NS) / 1000000 ))
if [[ "$OUT" == '{}' && $ELAPSED_MS -le 1000 ]]; then
  ok "a wedged controller cannot hold the hub bridge past one second (${ELAPSED_MS}ms)"
else
  fail "a wedged controller cannot hold the hub bridge past one second (${ELAPSED_MS}ms, out=$OUT)"
fi

# PermissionRequest carries a clipped, single-line request summary so Control
# shows what is being asked (answering it stays in the terminal, #100/#101).
run_control PermissionRequest '{"session_id":"native-perm","tool_name":"Bash","tool_input":{"command":"make clean\nmake all","description":"Rebuild"}}' \
  ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
if [[ "$(captured)" == "control session event --event permission-requested --native-id native-perm --text Permission requested: Bash: make clean make all" ]]; then
  ok "PermissionRequest forwards its tool and command as one clipped line"
else
  fail "PermissionRequest forwards its tool and command as one clipped line ($(captured))"
fi
LONG_COMMAND="$(printf 'x%.0s' {1..2000})"
run_control PermissionRequest '{"session_id":"native-perm","tool_name":"Write","tool_input":{"file_path":"/p/'"$LONG_COMMAND"'"}}' \
  ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
PERM_TEXT="$(captured)"; PERM_TEXT="${PERM_TEXT#*--text }"
if [[ ${#PERM_TEXT} -le 300 && "$PERM_TEXT" == "Permission requested: Write: /p/x"* ]]; then
  ok "a long request summary is clipped to 300 characters"
else
  fail "a long request summary is clipped (${#PERM_TEXT} chars)"
fi
run_control PostToolUse '{"session_id":"native-perm","tool_name":"Bash","tool_input":{"command":"ls"}}' ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
[[ "$(captured)" != *--text* ]] && ok "only PermissionRequest forwards request text" \
  || fail "only PermissionRequest forwards request text ($(captured))"

run_control UserPromptSubmit '{"session_id":"native-cwd","cwd":"/work/project dir"}' ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
if [[ "$(captured)" == "control session event --event prompt-submitted --native-id native-cwd --cwd /work/project dir" ]]; then
  ok "the native payload cwd is forwarded so the hub can refuse another project's thread (#100)"
else
  fail "the native payload cwd is forwarded ($(captured))"
fi
for payload in '{"session_id":"native-cwd","cwd":"relative/dir"}' '{"session_id":"native-cwd","cwd":7}' \
    '{"session_id":"native-cwd","cwd":"/a\nb"}'; do
  run_control UserPromptSubmit "$payload" ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
  [[ "$(captured)" == "control session event --event prompt-submitted --native-id native-cwd" ]] \
    || fail "an unusable cwd is not forwarded ($payload -> $(captured))"
done
ok "a relative, non-string or multi-line cwd is not forwarded"

OUT="$(run_control Stop '{"session_id":"native-abc","stop_hook_active":true}' ASHA_HUB_SESSION_ID="$HUB_ID")"
if [[ "$(captured)" == "control session event --event turn-stopped --native-id native-abc --stop-hook-active" && "$OUT" == '{}' ]]; then
  ok "stop_hook_active is forwarded so the hub never chains a second block"
else
  fail "stop_hook_active is forwarded so the hub never chains a second block ($(captured))"
fi
run_control Stop '{"session_id":"native-bg","stop_hook_active":false,"background_tasks":[{"id":"b1","type":"local_bash","status":"running","description":"suite"},{"id":"m1","type":"monitor_mcp","status":"running","description":"watch"}]}' \
  ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
if [[ "$(captured)" == "control session event --event turn-stopped --native-id native-bg --background-tasks 2" ]]; then
  ok "Stop forwards the count of outstanding background tasks (#99)"
else
  fail "Stop forwards the count of outstanding background tasks ($(captured))"
fi
NO_COUNT_OK=1
for payload in '{"session_id":"native-bg","stop_hook_active":false,"background_tasks":[]}' \
    '{"session_id":"native-bg","stop_hook_active":false,"background_tasks":"many"}' \
    '{"session_id":"native-bg","stop_hook_active":false}'; do
  run_control Stop "$payload" ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
  [[ "$(captured)" == "control session event --event turn-stopped --native-id native-bg" ]] \
    || { NO_COUNT_OK=0; fail "no background evidence forwards no count (${payload:50:40} -> $(captured))"; }
done
[[ $NO_COUNT_OK -eq 0 ]] || ok "an empty, malformed or absent background_tasks forwards no count"
run_control PostToolUse '{"session_id":"native-bg","background_tasks":[{"id":"b1"}]}' ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
[[ "$(captured)" != *--background-tasks* ]] && ok "background_tasks is read only at the Stop boundary" \
  || fail "background_tasks is read only at the Stop boundary ($(captured))"
run_control Stop '{"session_id":"native-abc","stop_hook_active":false}' ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
if [[ "$(captured)" == "control session event --event turn-stopped --native-id native-abc" ]]; then
  ok "a whole, parsed Stop payload with the guard off is forwarded without the flag"
else
  fail "a whole, parsed Stop payload with the guard off is forwarded without the flag ($(captured))"
fi

# Verifier defect 2: a Stop payload carries the last assistant message and can
# exceed the old 4 KiB read. Truncation must never drop the guard, and an
# absent, malformed or truncated payload is never authority for another block.
BIG_MESSAGE="$(head -c 300000 /dev/zero | tr '\0' 'x')"
OUT="$(run_control Stop '{"session_id":"native-big","stop_hook_active":true,"last_assistant_message":"'"$BIG_MESSAGE"'"}' \
  ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_OUTPUT="$CLOSE_DECISION")"
if [[ "$(captured)" == *"--stop-hook-active" && "$OUT" == '{}' ]]; then
  ok "an oversized Stop payload keeps the recursion guard and emits no block"
else
  fail "an oversized Stop payload keeps the recursion guard and emits no block ($(captured) -> $OUT)"
fi
# The bound is enforced by size, not by the read's time budget: a 200 KiB
# payload with the guard off still delivers.
LARGE_MESSAGE="$(head -c 200000 /dev/zero | tr '\0' 'z')"
OUT="$(run_control Stop '{"session_id":"native-large","stop_hook_active":false,"last_assistant_message":"'"$LARGE_MESSAGE"'"}' \
  ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_OUTPUT="$CLOSE_DECISION")"
if [[ "$(captured)" == "control session event --event turn-stopped --native-id native-large" && "$OUT" == "$CLOSE_DECISION" ]]; then
  ok "a 200 KiB Stop payload inside the size bound is read whole and delivers"
else
  fail "a 200 KiB Stop payload inside the size bound is read whole and delivers ($(captured) -> ${OUT:0:40})"
fi
MID_MESSAGE="$(head -c 20000 /dev/zero | tr '\0' 'y')"
run_control Stop '{"session_id":"native-mid","stop_hook_active":true,"last_assistant_message":"'"$MID_MESSAGE"'"}' \
  ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
if [[ "$(captured)" == "control session event --event turn-stopped --native-id native-mid --stop-hook-active" ]]; then
  ok "a Stop payload above 4 KiB but inside the Stop read bound is parsed whole"
else
  fail "a Stop payload above 4 KiB but inside the Stop read bound is parsed whole ($(captured))"
fi
for payload in '' 'not json at all' '{"session_id":"cut","stop_hook_active":tr'; do
  OUT="$(run_control Stop "$payload" ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_OUTPUT="$CLOSE_DECISION")"
  [[ "$(captured)" == *"--stop-hook-active" && "$OUT" == '{}' ]] || fail "unparsed Stop payload allowed a block (${payload:0:20} -> $(captured) -> $OUT)"
done
if [[ "$OUT" == '{}' ]]; then
  ok "an absent or malformed Stop payload is treated as the guard being set"
fi
run_control PostToolUse '{"session_id":"native-abc","stop_hook_active":true}' ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
if [[ "$(captured)" == "control session event --event tool-completed --native-id native-abc" ]]; then
  ok "stop_hook_active is read only at the Stop boundary"
else
  fail "stop_hook_active is read only at the Stop boundary ($(captured))"
fi

# Stop is a turn boundary and answers the close request, so it gets a larger
# budget; it is still bounded and still fails open.
START_NS=$(date +%s%N)
OUT="$(run_control Stop '{"session_id":"slow"}' ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_SLEEP=10)"
ELAPSED_MS=$(( ($(date +%s%N) - START_NS) / 1000000 ))
if [[ "$OUT" == '{}' && $ELAPSED_MS -ge 2500 && $ELAPSED_MS -le 4000 ]]; then
  ok "a wedged controller on Stop is bounded by the larger turn-boundary budget (${ELAPSED_MS}ms)"
else
  fail "a wedged controller on Stop is bounded by the larger turn-boundary budget (${ELAPSED_MS}ms, out=$OUT)"
fi

# A bridge timeout is a lost report: the hook records it through a detached,
# bounded call (cheap loss metric), and still returns within its own budget.
LOST_CAPTURE="$CAPTURE.lost"
: > "$LOST_CAPTURE"
START_NS=$(date +%s%N)
OUT="$(run_control PostToolUse '{"session_id":"slow-tool"}' ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_SLEEP=5)"
ELAPSED_MS=$(( ($(date +%s%N) - START_NS) / 1000000 ))
# Records from earlier timed-out tests may still be landing: match by native id.
for _ in $(seq 1 40); do grep -q -- '--native-id slow-tool$' "$LOST_CAPTURE" && break; sleep 0.1; done
LOST="$(grep -- '--native-id slow-tool$' "$LOST_CAPTURE" | sed -E 's/ --emitted-at [0-9]+\.[0-9]+//')"
if [[ "$OUT" == '{}' && $ELAPSED_MS -le 1500 \
   && "$LOST" == "control session event-lost --event tool-completed --reason bridge-timeout --budget 0.6 --native-id slow-tool" ]] \
   && grep -Eq -- '--emitted-at [0-9]+\.[0-9]+ --native-id slow-tool$' "$LOST_CAPTURE"; then
  ok "a timed-out bridge call is recorded as lost without extending the hook (${ELAPSED_MS}ms)"
else
  fail "a timed-out bridge call is recorded as lost without extending the hook (${ELAPSED_MS}ms, out=$OUT, lost=$LOST)"
fi
# #110: SessionStart and SessionEnd are lifecycle boundaries, not keystrokes.
# They share Stop's budget, so a call slower than the keystroke budget lands.
for lifecycle in SessionStart SessionEnd; do
  : > "$LOST_CAPTURE"
  START_NS=$(date +%s%N)
  OUT="$(run_control "$lifecycle" '{"session_id":"slow-life"}' ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_SLEEP=1.2)"
  ELAPSED_MS=$(( ($(date +%s%N) - START_NS) / 1000000 ))
  sleep 0.3
  if [[ "$OUT" == '{}' && $ELAPSED_MS -ge 1100 && $ELAPSED_MS -le 2500 ]] && ! grep -q -- 'slow-life' "$LOST_CAPTURE"; then
    ok "a slow $lifecycle report lands within the lifecycle budget (${ELAPSED_MS}ms)"
  else
    fail "a slow $lifecycle report lands within the lifecycle budget (${ELAPSED_MS}ms, lost=$(cat "$LOST_CAPTURE"))"
  fi
done

# A lost Stop is delivered late by the loss call, so the call carries what the
# Stop meant: a turn still waiting on background work is not a turn end.
: > "$LOST_CAPTURE"
OUT="$(run_control Stop '{"session_id":"slow-bg","stop_hook_active":false,"background_tasks":[{},{}]}' \
  ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_SLEEP=10)"
for _ in $(seq 1 40); do grep -q -- 'slow-bg' "$LOST_CAPTURE" && break; sleep 0.1; done
LOST="$(grep -- 'slow-bg' "$LOST_CAPTURE" | sed -E 's/ --emitted-at [0-9]+\.[0-9]+//')"
if [[ "$OUT" == '{}' && "$LOST" == "control session event-lost --event turn-stopped --reason bridge-timeout --budget 3 --native-id slow-bg --background-tasks 2" ]]; then
  ok "a lost Stop forwards its background task count to the loss call"
else
  fail "a lost Stop forwards its background task count to the loss call (out=$OUT, lost=$LOST)"
fi

: > "$LOST_CAPTURE"
run_control PostToolUse '{"session_id":"fast-tool"}' ASHA_HUB_SESSION_ID="$HUB_ID" >/dev/null
sleep 0.3
if ! grep -q -- 'fast-tool' "$LOST_CAPTURE"; then
  ok "a bridge call that answers in time records no loss"
else
  fail "a bridge call that answers in time records no loss ($(cat "$LOST_CAPTURE"))"
fi

# Worst case: a harness that never closes the payload pipe and a controller
# that never returns. Both budgets are spent and the bound still has to hold.
# The writer runs through a FIFO rather than a pipeline, so the measurement is
# the handler's own runtime and not how long the harness happens to live.
FIFO="$WORK/held-payload.pipe"
rm -f "$FIFO"
mkfifo "$FIFO"
{ printf '%s' '{"session_id":"held"}'; sleep 10; } >"$FIFO" &
WRITER_PID=$!
START_NS=$(date +%s%N)
OUT="$(env -u CLAUDE_CODE_SESSION_ID -u ASHA_SESSION_ID -u TMUX_PANE \
      ASHA_ROOT="$FAKE_ROOT" CONTROL_CAPTURE="$CAPTURE" ASHA_HARNESS=claude \
      ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_SLEEP=10 \
      "$CONTROL_HANDLER" PostToolUse <"$FIFO" 2>/dev/null)"
ELAPSED_MS=$(( ($(date +%s%N) - START_NS) / 1000000 ))
kill "$WRITER_PID" 2>/dev/null || true
wait "$WRITER_PID" 2>/dev/null || true
if [[ "$OUT" == '{}' && $ELAPSED_MS -le 1000 && "$(captured)" == *"--native-id held"* ]]; then
  ok "an open payload pipe plus a wedged controller stay inside the bound and keep the payload (${ELAPSED_MS}ms)"
else
  fail "an open payload pipe plus a wedged controller stay inside the bound and keep the payload (${ELAPSED_MS}ms, out=$OUT, $(captured))"
fi

# The same held-open pipe at the Stop boundary: the guard must be read from
# the payload that was written, not assumed because the writer never closed.
FIFO_STOP="$WORK/held-stop.pipe"
mkfifo "$FIFO_STOP"
{ printf '%s' '{"session_id":"held-stop","stop_hook_active":false}'; sleep 10; } >"$FIFO_STOP" &
WRITER_PID=$!
OUT="$(env -u CLAUDE_CODE_SESSION_ID -u ASHA_SESSION_ID -u TMUX_PANE \
      ASHA_ROOT="$FAKE_ROOT" CONTROL_CAPTURE="$CAPTURE" ASHA_HARNESS=claude \
      ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_OUTPUT="$CLOSE_DECISION" \
      "$CONTROL_HANDLER" Stop <"$FIFO_STOP" 2>/dev/null)"
kill "$WRITER_PID" 2>/dev/null || true
wait "$WRITER_PID" 2>/dev/null || true
if [[ "$(captured)" == "control session event --event turn-stopped --native-id held-stop" && "$OUT" == "$CLOSE_DECISION" ]]; then
  ok "a Stop payload on a held-open pipe is read whole and the guard is taken from it"
else
  fail "a Stop payload on a held-open pipe is read whole and the guard is taken from it ($(captured) -> ${OUT:0:40})"
fi

# Without stdbuf the bridge falls back to bash's own bounded read; that branch
# must not lose a held-open payload either.
THIN_PATH="$WORK/thin-path"
mkdir -p "$THIN_PATH"
for tool in bash env sleep timeout head jq wc date cat mkfifo printf; do
  real="$(command -v "$tool" 2>/dev/null || true)"
  [[ -z "$real" ]] || ln -s "$real" "$THIN_PATH/$tool"
done
FIFO_FALLBACK="$WORK/held-fallback.pipe"
mkfifo "$FIFO_FALLBACK"
{ printf '%s' '{"session_id":"held-fallback","stop_hook_active":false}'; sleep 10; } >"$FIFO_FALLBACK" &
WRITER_PID=$!
OUT="$(env -u CLAUDE_CODE_SESSION_ID -u ASHA_SESSION_ID -u TMUX_PANE PATH="$THIN_PATH" \
      ASHA_ROOT="$FAKE_ROOT" CONTROL_CAPTURE="$CAPTURE" ASHA_HARNESS=claude \
      ASHA_HUB_SESSION_ID="$HUB_ID" CONTROL_STUB_OUTPUT="$CLOSE_DECISION" \
      "$CONTROL_HANDLER" Stop <"$FIFO_FALLBACK" 2>/dev/null)"
kill "$WRITER_PID" 2>/dev/null || true
wait "$WRITER_PID" 2>/dev/null || true
if [[ "$(captured)" == "control session event --event turn-stopped --native-id held-fallback" && "$OUT" == "$CLOSE_DECISION" ]]; then
  ok "without stdbuf the fallback read still keeps a held-open Stop payload"
else
  fail "without stdbuf the fallback read still keeps a held-open Stop payload ($(captured) -> ${OUT:0:40})"
fi

echo "--- legacy managed-task behaviour is preserved ---"

OUT="$(run_control PermissionRequest '{"session_id":"permission-live-gate"}' \
  ASHA_CONTROL_MANAGED=1 ASHA_HARNESS=codex TMUX_PANE=%42)"
CAP="$(captured)"
if [[ "$CAP" == *"control event --event permission-requested"* \
   && "$CAP" == *"--harness codex"* && "$CAP" == *"--session-id permission-live-gate"* \
   && "$CAP" == *"--pane-id %42"* ]]; then
  ok "a managed task still reaches the legacy Control event with its facts"
else
  fail "a managed task still reaches the legacy Control event with its facts ($CAP)"
fi

BLOCK='{"decision":"block","reason":"Control wake test"}'
OUT="$(run_control Stop '' ASHA_CONTROL_MANAGED=1 CONTROL_STUB_OUTPUT="$BLOCK")"
if [[ "$OUT" == "$BLOCK" ]]; then
  ok "a managed task still passes through a valid single-line block decision"
else
  fail "a managed task still passes through a valid single-line block decision ($OUT)"
fi

OUT="$(run_control Stop '' ASHA_SESSION_PROFILE=worker)"
if [[ "$OUT" == '{}' && ! -e "$CAPTURE" ]]; then
  ok "an unmanaged session calls no controller at all"
else
  fail "an unmanaged session calls no controller at all ($OUT)"
fi

echo "--- operational layer budget: one truncation for every harness ---"

# 15-byte head then 2-byte characters: a raw byte cut at 4000 splits one.
{ printf 'OPERATION_HEAD:'; printf 'é%.0s' $(seq 1 3000); printf 'OPERATION_TAIL_SENTINEL\n'; } \
  > "$HOME_DIR/.asha/operation.md"
rm -rf "$PROJECT/Work/session-state"
OVER="$(run_hook session-start.sh "$START_PAYLOAD")"
if [[ "$OVER" == *OPERATION_HEAD:* && "$OVER" != *OPERATION_TAIL_SENTINEL* \
   && "$OVER" == *"[Truncated: operation.md"* ]]; then
  ok "SessionStart truncates an over-budget operation.md with a notice"
else
  fail "SessionStart truncates an over-budget operation.md with a notice"
fi
if printf '%s' "$OVER" | iconv -f UTF-8 -t UTF-8 >/dev/null 2>&1; then
  ok "SessionStart truncation never splits a UTF-8 character"
else
  fail "SessionStart truncation never splits a UTF-8 character"
fi
MERGED="$WORK/operational.md"
env -u ASHA_HOME HOME="$HOME_DIR" bash "$REPO_ROOT/identity/operational-merge.sh" "$MERGED" >/dev/null 2>&1
HOOK_BODY="$(printf '%s' "$OVER" | sed -n '/^OPERATION_HEAD:/,/^\[Truncated: operation.md/p')"
MERGE_BODY="$(sed -n '/^OPERATION_HEAD:/,/^\[Truncated: operation.md/p' "$MERGED" 2>/dev/null)"
if [[ -n "$HOOK_BODY" && "$HOOK_BODY" == "$MERGE_BODY" ]]; then
  ok "Claude and file-based harnesses receive identical operational text"
else
  fail "Claude and file-based harnesses receive identical operational text"
fi
printf 'operation-v2-sentinel\n' > "$HOME_DIR/.asha/operation.md"

echo "test-session-hub-hooks: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]]
