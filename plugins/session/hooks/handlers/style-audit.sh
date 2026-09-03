#!/usr/bin/env bash
# Run an optional project-local style audit after a file edit. Always fail open.
set -uo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/common.sh" 2>/dev/null || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/harness-response.sh" 2>/dev/null || { echo '{}'; exit 0; }
command -v jq >/dev/null 2>&1 || { echo '{}'; exit 0; }
command -v timeout >/dev/null 2>&1 || { echo '{}'; exit 0; }

INPUT="$(cat 2>/dev/null || true)"
[[ -n "$INPUT" ]] || { echo '{}'; exit 0; }
PROJECT_DIR="$(resolve_hook_project_dir "$INPUT" 2>/dev/null || true)"
[[ -n "$PROJECT_DIR" && -f "$PROJECT_DIR/.asha/config.json" ]] || { echo '{}'; exit 0; }

TOOL_NAME="$(printf '%s' "$INPUT" | jq -r \
  '.tool_name // .toolName // .tool // empty | select(type == "string")' \
  2>/dev/null || true)"
TOOL_KEY="$(printf '%s' "$TOOL_NAME" | tr '[:upper:]' '[:lower:]' 2>/dev/null || true)"
case "$TOOL_KEY" in
  edit|write|multiedit|multi_edit|apply_patch|create|str_replace|str_replace_editor|patch) ;;
  *) echo '{}'; exit 0 ;;
esac

EDITED_PATH="$(printf '%s' "$INPUT" | jq -r '
  (.tool_input // .toolArgs // .args // {}) as $raw
  | (if ($raw | type) == "string" then ($raw | fromjson? // {})
     elif ($raw | type) == "object" then $raw else {} end) as $a
  | [
      $a.file_path?, $a.filePath?, $a.path?,
      $a.edits[]?.file_path?, $a.edits[]?.filePath?, $a.edits[]?.path?
    ]
  | map(select(type == "string" and length > 0))
  | first // empty
' 2>/dev/null || true)"
if [[ -z "$EDITED_PATH" ]]; then
  PATCH="$(printf '%s' "$INPUT" | jq -r '
    (.tool_input // .toolArgs // .args // {}) as $raw
    | (if ($raw | type) == "string" then ($raw | fromjson? // {})
       elif ($raw | type) == "object" then $raw else {} end)
    | .patch // empty
  ' 2>/dev/null || true)"
  EDITED_PATH="$(printf '%s\n' "$PATCH" | sed -nE \
    's/^\*\*\* (Update|Add|Delete) File: (.*)$/\2/p' | head -1)"
fi
[[ -n "$EDITED_PATH" ]] || { echo '{}'; exit 0; }

AUDITOR="$PROJECT_DIR/.asha/style-audit"
[[ -f "$AUDITOR" && -x "$AUDITOR" ]] || { echo '{}'; exit 0; }

OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/asha-style-audit.XXXXXX" 2>/dev/null || true)"
[[ -n "$OUTPUT_FILE" ]] || { echo '{}'; exit 0; }
trap 'rm -f "$OUTPUT_FILE"' EXIT
printf '%s' "$INPUT" | timeout --signal=TERM --kill-after=1 10 \
  "$AUDITOR" "$EDITED_PATH" \
  >"$OUTPUT_FILE" 2>/dev/null || true
AUDIT_OUTPUT="$(cat "$OUTPUT_FILE" 2>/dev/null || true)"
[[ -n "$AUDIT_OUTPUT" ]] || { echo '{}'; exit 0; }

FRAGMENT="$(nudge_fragment style-audit 2>/dev/null || true)"
CONTEXT="${FRAGMENT:+$FRAGMENT$'\n\n'}Edited path: $EDITED_PATH

$AUDIT_OUTPUT"
SESSION_ID="$(printf '%s' "$INPUT" | jq -r \
  '.session_id // .sessionId // .sessionID // empty' 2>/dev/null || true)"
SESSION_ID="${SESSION_ID:-${ASHA_SESSION_ID:-${CLAUDE_CODE_SESSION_ID:-unknown}}}"
posttooluse_nudge "$CONTEXT" "$PROJECT_DIR" "$SESSION_ID"
exit 0
