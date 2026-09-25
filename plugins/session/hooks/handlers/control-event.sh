#!/usr/bin/env bash
# Narrow fail-open bridge from native hook names to bounded Control snapshots.
#
# Two consumers, checked in this order:
#   1. a hub session (ASHA_HUB_SESSION_ID) -> `asha control session event`
#   2. a Control-managed task (ASHA_CONTROL_MANAGED=1) -> `asha control event`
#
# The hub path is deliberately the thinner of the two: session identity and
# generation are inherited environment. Event name, optional native session ID,
# tool classification and opaque boundary token are forwarded; no body is retained. The
# bridge is bounded (under a second on keystroke-facing events, a few seconds
# at the Stop turn boundary), and the answer is a harmless empty object — with
# one named exception. On Stop only, a pending graceful close request is
# returned as the harness's own single-line block decision, so the session's
# final memory-handoff turn starts at the turn boundary the harness itself
# declared. The hub emits it once per confirmed delivery attempt and never
# when the harness reports stop_hook_active; every other event, and every
# other shape, still answers '{}'.
#
# ASHA_SESSION_PROFILE is not consulted here. The worker profile silences
# Asha's own context hooks; silencing this one too would make a worker
# invisible to the operator watching the session.
set -uo pipefail

case "${1:-}" in
  SessionStart)      CONTROL_EVENT="session-start" ;;
  UserPromptSubmit)  CONTROL_EVENT="prompt-submitted" ;;
  PreToolUse)        CONTROL_EVENT="tool-started" ;;
  PostToolUse)       CONTROL_EVENT="tool-completed" ;;
  # Claude reports a failed tool here instead of PostToolUse; either way the
  # start has ended, and an unmatched start would block a later sole handoff.
  PostToolUseFailure) CONTROL_EVENT="tool-completed" ;;
  PermissionRequest) CONTROL_EVENT="permission-requested" ;;
  Stop)              CONTROL_EVENT="turn-stopped" ;;
  SessionEnd)        CONTROL_EVENT="session-ended" ;;
  *) echo '{}'; exit 0 ;;
esac

HUB_SESSION="${ASHA_HUB_SESSION_ID:-}"
# Tool-start telemetry is only a hub completion seam, not a legacy task event.
if [[ "$CONTROL_EVENT" == "tool-started" && -z "$HUB_SESSION" ]]; then
  echo '{}'
  exit 0
fi
if [[ -z "$HUB_SESSION" && "${ASHA_CONTROL_MANAGED:-}" != "1" ]]; then
  echo '{}'
  exit 0
fi

# Before anything else, make this native event visible to Control's idle-pane
# typing (#96): bump the Room pane's own event sequence. Control types into an
# idle pane only while the sequence equals the one the hub last recorded, and
# tmux re-checks it when pasting and pressing Enter, so an event whose report
# is slow or is killed at the budget below still stops the typing. One bounded
# tmux call; any failure just omits --sequence, which makes the hub treat the
# sequence as unknown and refuse to type until a sequenced event lands.
# Only Rooms created with Control's experimental idle typing (control.idle_delivery)
# carry the fence; everywhere else this costs nothing.
HUB_SEQUENCE=""
if [[ -n "$HUB_SESSION" && "${ASHA_ROOM_INPUT_FENCE:-}" == "1" && -n "${TMUX:-}" && "${TMUX_PANE:-}" =~ ^%[0-9]+$ ]] \
    && command -v tmux >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1; then
  HUB_SEQUENCE="$(
    timeout --signal=KILL 0.2 tmux set-option -p -t "$TMUX_PANE" -F @asha_event_seq \
      '#{e|+:#{@asha_event_seq},1}' ';' show-options -p -v -t "$TMUX_PANE" @asha_event_seq \
      2>/dev/null || true
  )"
  [[ "$HUB_SEQUENCE" =~ ^[1-9][0-9]{0,8}$ ]] || HUB_SEQUENCE=""
fi

# A hub session sits in front of the operator's own keystrokes, so its share of
# the bridge is fixed and small. Worst case is a harness that never closes the
# payload pipe AND a wedged controller: the two budgets together still leave
# room under one second for jq and the interpreter. A real observation costs
# about 0.13s, so the controller budget is roughly five times what it needs.
HUB_READ_SECONDS=0.15
HUB_CONTROLLER_SECONDS=0.6
# Stop is a turn boundary, not a keystroke: the harness is already idle, and the
# hub path there does ownership checks (several tmux calls), takes the session
# lock and answers the pending close request. It gets a larger, still bounded,
# budget; a kill here re-emits the same request at the next Stop.
HUB_STOP_SECONDS=3
[[ "$CONTROL_EVENT" != "turn-stopped" ]] || HUB_CONTROLLER_SECONDS="$HUB_STOP_SECONDS"

# Never retain or forward payload bodies. A truncated or malformed object
# simply yields no optional session/exit facts and the controller remains open.
# Stop and tool results carry potentially large text. Read a bounded 256 KiB
# for those events so ordinary output does not hide boundary identity. Oversized
# or failed callbacks remain unverified until a new turn after observed idle.
HUB_READ_CHARS=4096
if [[ -n "$HUB_SESSION" && ( "$CONTROL_EVENT" == "turn-stopped" || "$CONTROL_EVENT" == "tool-started" || "$CONTROL_EVENT" == "tool-completed" ) ]]; then
  HUB_READ_CHARS=262144
fi
INPUT=""
if [[ -n "$HUB_SESSION" ]]; then
  # Bounded in both axes: bash's own `read` fetches a pipe one byte at a time
  # and would spend the whole time budget at roughly 100 KiB, so the size bound
  # is enforced by head(1) and the time bound by timeout(1). Without timeout
  # the payload is not read at all rather than read unbounded.
  # head's stdout must be unbuffered: a harness that holds the pipe open leaves
  # head to be killed at the deadline, and a buffered head dies with the payload
  # still in its buffer. Without stdbuf, fall back to bash's own bounded read,
  # which is slower on a pipe but never loses what was written.
  if [[ ! -t 0 ]]; then
    if command -v timeout >/dev/null 2>&1 && command -v stdbuf >/dev/null 2>&1 && command -v head >/dev/null 2>&1; then
      INPUT="$(timeout --signal=TERM --kill-after=0.1 "$HUB_READ_SECONDS" stdbuf -o0 head -c "$HUB_READ_CHARS" 2>/dev/null || true)"
    else
      IFS= read -r -t "$HUB_READ_SECONDS" -N "$HUB_READ_CHARS" INPUT || true
    fi
  fi
else
  [[ -t 0 ]] || IFS= read -r -N 4096 INPUT || true
fi
INPUT_TRUNCATED=""
[[ "$(printf '%s' "$INPUT" | wc -c)" -lt "$HUB_READ_CHARS" ]] || INPUT_TRUNCATED=1
TOOL_KIND="work"
TOOL_TOKEN="unknown"
if [[ "$CONTROL_EVENT" == "tool-started" || "$CONTROL_EVENT" == "tool-completed" ]]; then
  if [[ -z "$INPUT_TRUNCATED" ]]; then
    CLASSIFIER="$(dirname -- "${BASH_SOURCE[0]}")/../../tools/completion_event.py"
    CLASSIFICATION="$(printf '%s' "$INPUT" | python3 "$CLASSIFIER" 2>/dev/null || true)"
    read -r TOOL_KIND TOOL_TOKEN <<< "$CLASSIFICATION"
    TOOL_KIND="${TOOL_KIND:-work}"
    TOOL_TOKEN="${TOOL_TOKEN:-unknown}"
  fi
fi
SESSION_ID=""
EXIT_STATUS=""
STOP_HOOK_ACTIVE=""
if command -v jq >/dev/null 2>&1 && [[ -n "$INPUT" ]]; then
  SESSION_ID="$(printf '%s' "$INPUT" | jq -r '
    .session_id // .sessionId // .sessionID // empty
    | select(type == "string")' 2>/dev/null || true)"
fi
# The harness's own guard against chained Stop blocks. Only a payload that was
# read whole and parsed as an object can prove the guard is off; an absent,
# truncated or malformed payload is never authority to issue another block, so
# it is reported as if the guard were set. One extra jq, only at the turn
# boundary, where the budget allows it.
if [[ -n "$HUB_SESSION" && "$CONTROL_EVENT" == "turn-stopped" ]]; then
  STOP_HOOK_ACTIVE=1
  if [[ -z "$INPUT_TRUNCATED" && -n "$INPUT" ]] && command -v jq >/dev/null 2>&1 \
      && printf '%s' "$INPUT" | jq -e 'type == "object" and .stop_hook_active != true' >/dev/null 2>&1; then
    STOP_HOOK_ACTIVE=""
  fi
fi
if command -v jq >/dev/null 2>&1 && [[ -n "$INPUT" ]]; then
  # The hub records lifecycle from the event name alone; the exit status is a
  # managed-task fact and is not worth a second jq in the latency-bound path.
  if [[ -z "$HUB_SESSION" && "$CONTROL_EVENT" == "session-ended" ]]; then
    EXIT_STATUS="$(printf '%s' "$INPUT" | jq -r '
      .exit_status // .exitStatus // empty
      | select(type == "number" and floor == .)' 2>/dev/null || true)"
  fi
fi
SESSION_ID="${SESSION_ID:-${ASHA_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}}"

# Prefer the launcher's own checkout over a PATH lookup. `bin/asha` exports
# ASHA_ROOT before exec'ing the harness, so this hook child inherits it; relying
# on PATH alone would make the whole bridge vanish silently in a pane without it.
ASHA_CMD=""
if [[ -n "${ASHA_ROOT:-}" && -x "${ASHA_ROOT}/bin/asha" ]]; then
  ASHA_CMD="${ASHA_ROOT}/bin/asha"
elif command -v asha >/dev/null 2>&1; then
  ASHA_CMD="asha"
fi

if [[ -n "$HUB_SESSION" ]]; then
  # Without timeout(1) there is no way to hold the promised bound, so the
  # observation is dropped rather than allowed to run unbounded. A missing
  # observation never gates the session; a wedged hook would.
  HUB_RESPONSE=""
  if [[ -n "$ASHA_CMD" ]] && command -v timeout >/dev/null 2>&1; then
    HUB_ARGS=(control session event --event "$CONTROL_EVENT")
    if [[ "$CONTROL_EVENT" == "tool-started" || "$CONTROL_EVENT" == "tool-completed" ]]; then
      HUB_ARGS+=(--tool-kind "$TOOL_KIND" --tool-token "$TOOL_TOKEN")
    fi
    [[ -z "$SESSION_ID" ]] || HUB_ARGS+=(--native-id "$SESSION_ID")
    [[ -z "$STOP_HOOK_ACTIVE" ]] || HUB_ARGS+=(--stop-hook-active)
    [[ -z "$HUB_SEQUENCE" ]] || HUB_ARGS+=(--sequence "$HUB_SEQUENCE" --sequence-pane "$TMUX_PANE")
    HUB_RESPONSE="$(
      timeout --signal=TERM --kill-after=0.1 "$HUB_CONTROLLER_SECONDS" \
        "$ASHA_CMD" "${HUB_ARGS[@]}" 2>/dev/null || true
    )"
  fi
  # Stop is the one seam where a close request may ride back. The shape is
  # checked strictly: one line, one object, exactly decision=block plus a
  # string reason. Anything else, on any event, collapses to '{}'.
  # The bridge itself also refuses to relay a block while the guard is (or
  # must be assumed) set, independently of what the controller answered.
  if [[ "$CONTROL_EVENT" == "turn-stopped" && -z "$STOP_HOOK_ACTIVE" ]] && command -v jq >/dev/null 2>&1 \
      && [[ "$HUB_RESPONSE" == \{* && "$HUB_RESPONSE" != *$'\n'* ]] \
      && printf '%s' "$HUB_RESPONSE" | jq -e '
           type == "object" and (keys == ["decision", "reason"])
           and .decision == "block" and (.reason | type == "string")
           and (.reason | test("close request"))' >/dev/null 2>&1; then
    printf '%s\n' "$HUB_RESPONSE"
    exit 0
  fi
  echo '{}'
  exit 0
fi

ARGS=(control event --event "$CONTROL_EVENT")
# Only label the harness when the launcher actually told us which one this is.
# Guessing would write a mislabelled harness into the snapshot; the controller
# already knows the harness from the run record it owns.
[[ -z "${ASHA_HARNESS:-}" ]] || ARGS+=(--harness "$ASHA_HARNESS")
[[ -z "$SESSION_ID" ]] || ARGS+=(--session-id "$SESSION_ID")
[[ -z "$EXIT_STATUS" ]] || ARGS+=(--exit-status "$EXIT_STATUS")
[[ -z "${TMUX_PANE:-}" ]] || ARGS+=(--pane-id "$TMUX_PANE")

CONTROL_RESPONSE=""
if [[ -n "$ASHA_CMD" ]]; then
  if command -v timeout >/dev/null 2>&1; then
    CONTROL_RESPONSE="$(
      timeout --signal=TERM 15 "$ASHA_CMD" "${ARGS[@]}" 2>/dev/null || true
    )"
  else
    CONTROL_RESPONSE="$("$ASHA_CMD" "${ARGS[@]}" 2>/dev/null || true)"
  fi
fi
if command -v jq >/dev/null 2>&1 \
    && [[ "$CONTROL_RESPONSE" == \{* && "$CONTROL_RESPONSE" != *$'\n'* ]] \
    && printf '%s' "$CONTROL_RESPONSE" | jq -e type >/dev/null 2>&1; then
  printf '%s\n' "$CONTROL_RESPONSE"
  exit 0
fi
echo '{}'
exit 0
