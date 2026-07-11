#!/usr/bin/env bash
# Translate OpenCode tool.execute.before payloads into Asha's PreToolUse shape.
# OpenCode has no verified hook-mediated "ask" response, so policy-guard is run
# under the conservative deny-on-ask contract used by Codex.
set -uo pipefail

SELF_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || exit 0
command -v jq >/dev/null 2>&1 || exit 0

INPUT="$(cat 2>/dev/null || true)"
[[ -n "$INPUT" ]] || exit 0

TOOL="$(printf '%s' "$INPUT" | jq -r '.tool_name // .tool // .name // empty' 2>/dev/null || true)"
[[ -n "$TOOL" ]] || exit 0

case "$TOOL" in
  bash|shell) TOOL_NAME="Bash" ;;
  read) TOOL_NAME="Read" ;;
  edit|patch) TOOL_NAME="Edit" ;;
  write|create) TOOL_NAME="Write" ;;
  *) TOOL_NAME="$TOOL" ;;
esac

ARGS="$(printf '%s' "$INPUT" | jq -c '
  (.tool_input // .args // .input // {}) as $a
  | (if ($a | type) == "object" then $a else {} end)
  | . + {
      file_path: (.file_path // .filePath // .path // ""),
      command: (.command // .cmd // "")
    }
' 2>/dev/null || echo '{}')"
SID="$(printf '%s' "$INPUT" | jq -r '.session_id // .sessionID // empty' 2>/dev/null || true)"

CLAUDE_INPUT="$(jq -cn --arg tool "$TOOL_NAME" --arg sid "$SID" --argjson args "$ARGS" \
  '{tool_name: $tool, tool_input: $args, session_id: $sid}')" || exit 0

run_guard() {
  local guard="$1" err rc
  [[ -x "$guard" ]] || return 0
  err="$(printf '%s' "$CLAUDE_INPUT" | ASHA_HARNESS=opencode "$guard" 2>&1 >/dev/null)"
  rc=$?
  if [[ $rc -eq 2 ]]; then
    [[ -n "$err" ]] && printf '%s\n' "$err" >&2
    return 2
  fi
  return 0
}

run_guard "$SELF_DIR/policy-guard.sh" || exit $?
run_guard "$SELF_DIR/block-secrets.sh" || exit $?
exit 0
