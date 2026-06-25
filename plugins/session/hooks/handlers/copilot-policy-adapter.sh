#!/usr/bin/env bash
# copilot-policy-adapter.sh — bridge Copilot's preToolUse hook to Asha's policy handlers.
#
# Copilot's preToolUse contract differs from Claude's, so this shim translates in
# both directions and lets the policy logic stay in ONE place (policy-guard.sh +
# policies/rules.json, and block-secrets.sh):
#
#   Copilot stdin : {sessionId, timestamp, cwd, toolName, toolArgs}
#                   - toolArgs may be a JSON-ENCODED STRING, not an object
#                   - tool names are bash / create / edit / view / … (lowercase)
#   Claude stdin  : {tool_name, tool_input:{command,file_path}, session_id}
#                   - tool names are Bash / Write / Edit / Read
#   Copilot out   : stdout JSON {"permissionDecision":"allow|deny|ask","permissionDecisionReason":…}
#   Claude out    : deny => exit 2 + stderr reason ; ask => stdout hookSpecificOutput JSON
#
# We run each Claude-shaped handler with a translated payload; the first deny/ask
# wins and is re-emitted in Copilot's format.
#
# FAIL-OPEN: any error → allow. A guardrail that failed *closed* would brick every
# Copilot tool call — strictly worse than the gap it closes. (And note Copilot's
# own upstream caveat: preToolUse can be bypassed under parallel tool calls /
# timeouts — github/copilot-cli#2893 — so this is a soft deterrent, not containment.)

set -uo pipefail

allow() { echo '{"permissionDecision":"allow"}'; exit 0; }

command -v jq >/dev/null 2>&1 || allow
SELF_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || allow

INPUT="$(cat 2>/dev/null || true)"
[[ -n "$INPUT" ]] || allow

# Translate Copilot payload → Claude shape. Map tool names; pull command (shell)
# and file path (edits) into tool_input; carry the session id through.
CLAUDE_JSON="$(printf '%s' "$INPUT" | jq -c '
  (.toolName // "")  as $tn
  | (.toolArgs // {}) as $raw
  | (if ($raw | type) == "string" then ($raw | fromjson? // {}) else $raw end) as $a
  | ($tn | ascii_downcase) as $t
  | {
      tool_name: (
        if   $t == "bash" or $t == "shell" then "Bash"
        elif $t == "create" or $t == "write" then "Write"
        elif $t == "edit" or $t == "str_replace" or $t == "str_replace_editor" or $t == "multi_edit" or $t == "apply_patch" then "Edit"
        elif $t == "view" or $t == "read" then "Read"
        else $tn end
      ),
      tool_input: {
        command:   ($a.command // ""),
        file_path: ($a.path // $a.file_path // "")
      },
      session_id: (.sessionId // "")
    }
' 2>/dev/null || true)"
[[ -n "$CLAUDE_JSON" ]] || allow

EMIT_DECISION="allow"
EMIT_REASON=""

# Run one Claude-shaped handler; set EMIT_* and return 1 if it decided (deny/ask).
consult() {
  local h="$1" to te out err rc
  [[ -x "$h" ]] || return 0
  to="$(mktemp)" || return 0
  te="$(mktemp)" || { rm -f "$to"; return 0; }
  # ASHA_HARNESS=claude so policy-guard emits its ask decision as JSON (rather than
  # degrading ask→deny as it does for codex). We translate it below.
  printf '%s' "$CLAUDE_JSON" | ASHA_HARNESS=claude "$h" >"$to" 2>"$te"
  rc=$?
  out="$(cat "$to" 2>/dev/null || true)"
  err="$(cat "$te" 2>/dev/null || true)"
  rm -f "$to" "$te"

  if [[ "$rc" -eq 2 ]]; then
    EMIT_DECISION="deny"
    EMIT_REASON="${err:-Blocked by Asha policy}"
    return 1
  fi

  local pd
  pd="$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecision // .permissionDecision // empty' 2>/dev/null || true)"
  if [[ "$pd" == "ask" || "$pd" == "deny" ]]; then
    EMIT_DECISION="$pd"
    EMIT_REASON="$(printf '%s' "$out" | jq -r '.hookSpecificOutput.permissionDecisionReason // .permissionDecisionReason // empty' 2>/dev/null || true)"
    return 1
  fi
  return 0
}

for handler in policy-guard.sh block-secrets.sh; do
  consult "$SELF_DIR/$handler" || break
done

if [[ "$EMIT_DECISION" == "deny" || "$EMIT_DECISION" == "ask" ]]; then
  jq -nc --arg d "$EMIT_DECISION" --arg r "${EMIT_REASON:-Blocked by Asha policy}" \
    '{permissionDecision: $d, permissionDecisionReason: $r}'
else
  echo '{"permissionDecision":"allow"}'
fi
exit 0
