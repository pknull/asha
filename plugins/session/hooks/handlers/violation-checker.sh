#!/bin/bash
set -uo pipefail
# violation-checker.sh — soft, non-blocking awareness logger for Asha.
#
# Called from post-tool-use.sh (PostToolUse) for Write/Edit/Bash. Evaluates the
# action against the SAME declarative rules as policy-guard.sh
# (<plugin>/hooks/policies/rules.json + ~/.asha/policies.json, merged by id) and
# *logs* every matching rule to the session file for context. It NEVER blocks —
# policy-guard.sh (PreToolUse) owns enforcement; this is post-hoc awareness only.
#
# Args: $1 = TOOL_NAME, $2 = TOOL_INPUT (JSON). Fail-open (exit 0) on any error.

source "$(dirname "$0")/common.sh" 2>/dev/null || exit 0
command -v jq >/dev/null 2>&1 || exit 0

TOOL_NAME="${1:-}"
TOOL_INPUT="${2:-}"
[[ -n "$TOOL_NAME" ]] || exit 0

PROJECT_DIR="$(detect_project_dir 2>/dev/null || true)"
[[ -n "$PROJECT_DIR" ]] || exit 0
is_asha_initialized 2>/dev/null || exit 0

SESSION_FILE="$PROJECT_DIR/Memory/sessions/current-session.md"
[[ -f "$SESSION_FILE" ]] || exit 0
# Respect the silence marker (no logging when silenced).
[[ -f "$PROJECT_DIR/Work/markers/silence" ]] && exit 0

SELF_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || exit 0
REPO_RULES="$SELF_DIR/../policies/rules.json"
USER_RULES="$HOME/.asha/policies.json"

# Merge repo + user rules (user overrides by id) — mirrors policy-guard.sh.
RULES=""
if [[ -f "$REPO_RULES" && -f "$USER_RULES" ]]; then
  RULES="$(jq -s '
    (.[0].rules // []) as $base | (.[1].rules // []) as $user
    | ($user | map(.id)) as $uids
    | { rules: (($base | map(select((.id) as $i | ($uids | index($i)) | not))) + $user) }
  ' "$REPO_RULES" "$USER_RULES" 2>/dev/null || true)"
elif [[ -f "$REPO_RULES" ]]; then
  RULES="$(jq '{rules: (.rules // [])}' "$REPO_RULES" 2>/dev/null || true)"
fi
[[ -n "$RULES" ]] || exit 0

# Matchable fields by tool type.
CMD=""; FILE=""
case "$TOOL_NAME" in
  Write|Edit|MultiEdit) FILE="$(printf '%s' "$TOOL_INPUT" | jq -r '.file_path // empty' 2>/dev/null || true)" ;;
  Bash)                 CMD="$(printf '%s'  "$TOOL_INPUT" | jq -r '.command   // empty' 2>/dev/null || true)" ;;
esac

COUNT="$(printf '%s' "$RULES" | jq '.rules | length' 2>/dev/null || echo 0)"
[[ "$COUNT" =~ ^[0-9]+$ ]] || exit 0

i=0
while [[ $i -lt $COUNT ]]; do
  rule="$(printf '%s' "$RULES" | jq -c ".rules[$i]" 2>/dev/null || true)"
  i=$((i+1))
  [[ -n "$rule" && "$rule" != "null" ]] || continue

  r_id="$(printf '%s'     "$rule" | jq -r '.id // "rule"' 2>/dev/null || echo rule)"
  r_tool="$(printf '%s'   "$rule" | jq -r '.tool // empty' 2>/dev/null || true)"
  r_cmdre="$(printf '%s'  "$rule" | jq -r '.command_regex // empty' 2>/dev/null || true)"
  r_filere="$(printf '%s' "$rule" | jq -r '.file_path_regex // empty' 2>/dev/null || true)"
  r_action="$(printf '%s' "$rule" | jq -r '.action // "match"' 2>/dev/null || echo match)"
  r_reason="$(printf '%s' "$rule" | jq -r '.reason // "policy match"' 2>/dev/null || echo "policy match")"
  r_exclude="$(printf '%s' "$rule" | jq -r '.exclude_regex // empty' 2>/dev/null || true)"

  [[ -n "$r_tool" ]] || continue
  printf '%s' "$TOOL_NAME" | grep -Eq "^($r_tool)\$" 2>/dev/null || continue

  matched=0
  if [[ -n "$r_cmdre" && -n "$CMD" ]]; then
    printf '%s' "$CMD"  | grep -Eq "$r_cmdre"  2>/dev/null && matched=1
  fi
  if [[ $matched -eq 0 && -n "$r_filere" && -n "$FILE" ]]; then
    printf '%s' "$FILE" | grep -Eq "$r_filere" 2>/dev/null && matched=1
  fi
  if [[ $matched -eq 1 && -n "$r_exclude" ]]; then
    if [[ -n "$CMD" ]]  && printf '%s' "$CMD"  | grep -Eq "$r_exclude" 2>/dev/null; then matched=0; fi
    if [[ $matched -eq 1 && -n "$FILE" ]] && printf '%s' "$FILE" | grep -Eq "$r_exclude" 2>/dev/null; then matched=0; fi
  fi
  [[ $matched -eq 1 ]] || continue

  timestamp="$(date -u '+%H:%M UTC' 2>/dev/null || true)"
  {
    echo ""
    echo "> [!warning] Violation [$r_action] $timestamp"
    echo "> **$r_id**: $r_reason"
  } >> "$SESSION_FILE" 2>/dev/null || true
done

exit 0
