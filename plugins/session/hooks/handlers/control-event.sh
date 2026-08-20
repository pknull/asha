#!/usr/bin/env bash
[[ "${ASHA_CONTROL_MANAGED:-}" == "1" ]] || { echo '{}'; exit 0; }
# Narrow fail-open bridge from native hook names to bounded Control snapshots.
set -uo pipefail

case "${1:-}" in
  SessionStart)     CONTROL_EVENT="session-start" ;;
  UserPromptSubmit) CONTROL_EVENT="prompt-submitted" ;;
  PostToolUse)      CONTROL_EVENT="tool-completed" ;;
  PermissionRequest) CONTROL_EVENT="permission-requested" ;;
  Stop)             CONTROL_EVENT="turn-stopped" ;;
  SessionEnd)       CONTROL_EVENT="session-ended" ;;
  *) echo '{}'; exit 0 ;;
esac

# Never retain or forward payload bodies. A truncated or malformed object
# simply yields no optional session/exit facts and the controller remains open.
INPUT=""
[[ -t 0 ]] || IFS= read -r -N 4096 INPUT || true
SESSION_ID=""
EXIT_STATUS=""
if command -v jq >/dev/null 2>&1 && [[ -n "$INPUT" ]]; then
  SESSION_ID="$(printf '%s' "$INPUT" | jq -r '
    .session_id // .sessionId // .sessionID // empty
    | select(type == "string")' 2>/dev/null || true)"
  if [[ "$CONTROL_EVENT" == "session-ended" ]]; then
    EXIT_STATUS="$(printf '%s' "$INPUT" | jq -r '
      .exit_status // .exitStatus // empty
      | select(type == "number" and floor == .)' 2>/dev/null || true)"
  fi
fi
SESSION_ID="${SESSION_ID:-${ASHA_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-}}}"

ARGS=(control event --event "$CONTROL_EVENT")
# Only label the harness when the launcher actually told us which one this is.
# Guessing would write a mislabelled harness into the snapshot; the controller
# already knows the harness from the run record it owns.
[[ -z "${ASHA_HARNESS:-}" ]] || ARGS+=(--harness "$ASHA_HARNESS")
[[ -z "$SESSION_ID" ]] || ARGS+=(--session-id "$SESSION_ID")
[[ -z "$EXIT_STATUS" ]] || ARGS+=(--exit-status "$EXIT_STATUS")
[[ -z "${TMUX_PANE:-}" ]] || ARGS+=(--pane-id "$TMUX_PANE")

# Prefer the launcher's own checkout over a PATH lookup. `bin/asha` exports
# ASHA_ROOT before exec'ing the harness, so this hook child inherits it; relying
# on PATH alone would make the whole bridge vanish silently in a pane without it.
ASHA_CMD=""
if [[ -n "${ASHA_ROOT:-}" && -x "${ASHA_ROOT}/bin/asha" ]]; then
  ASHA_CMD="${ASHA_ROOT}/bin/asha"
elif command -v asha >/dev/null 2>&1; then
  ASHA_CMD="asha"
fi
if [[ -n "$ASHA_CMD" ]]; then
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM 15 "$ASHA_CMD" "${ARGS[@]}" >/dev/null 2>&1 || true
  else
    "$ASHA_CMD" "${ARGS[@]}" >/dev/null 2>&1 || true
  fi
fi
echo '{}'
exit 0
