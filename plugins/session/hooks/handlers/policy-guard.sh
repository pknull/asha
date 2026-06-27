#!/bin/bash
# policy-guard.sh — declarative PreToolUse policy engine for Asha.
#
# Reads the PreToolUse stdin JSON ({tool_name, tool_input{command|file_path}}),
# evaluates rules from:
#   <plugin>/hooks/policies/rules.json   (repo defaults)
#   ~/.asha/policies.json                (optional user layer; merged by id, user wins)
# and emits a decision:
#   - allow  -> exit 0  (no match, or a rule's override_env is set to 1)
#   - deny   -> exit 2 + stderr reason   (hard block; honored by Claude AND Codex)
#   - ask    -> Claude (or unknown harness): JSON permissionDecision="ask" + reason, exit 0
#               Codex (no permission dialog): degrade to deny (exit 2) with the override hint
#
# FAIL-OPEN: any internal error (missing/malformed rules, missing jq, parse failure)
# results in exit 0 (allow). A guardrail that fails *closed* would brick every
# matched tool call — strictly worse than the gap it closes.
#
# Harness is read from ASHA_HARNESS (set by the asha dispatcher); absent => claude.
# Rule schema: {id, tool, command_regex|file_path_regex, exclude_regex?, action: deny|ask|warn, reason, override_env?}
# action=warn => awareness-only (violation-checker logs it; this guard does not block).

set -uo pipefail   # deliberately NOT -e: we own every exit code; never die mid-eval

# Shared harness-specific output contracts. Fail-open if unavailable.
SELF_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || exit 0
[[ -f "$SELF_DIR/harness-response.sh" ]] && source "$SELF_DIR/harness-response.sh" 2>/dev/null || exit 0

# Fail-open if jq is unavailable.
command -v jq >/dev/null 2>&1 || exit 0

INPUT="$(cat 2>/dev/null || true)"
[[ -n "$INPUT" ]] || exit 0

TOOL_NAME="$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)"
[[ -n "$TOOL_NAME" ]] || exit 0

CMD="$(printf '%s' "$INPUT"  | jq -r '.tool_input.command   // empty' 2>/dev/null || true)"
FILE="$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)"
SID="$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null || true)"

# Locate rule sources relative to this script (hooks run from the source tree).
REPO_RULES="$SELF_DIR/../policies/rules.json"
USER_RULES="$HOME/.asha/policies.json"

# Ephemeral per-session counters (for max_per_session). Fail-open if absent.
[[ -f "$SELF_DIR/state.sh" ]] && source "$SELF_DIR/state.sh" 2>/dev/null || true

# Merge repo + user rules (user overrides by id). Missing user file is normal.
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

COUNT="$(printf '%s' "$RULES" | jq '.rules | length' 2>/dev/null || echo 0)"
[[ "$COUNT" =~ ^[0-9]+$ ]] || exit 0

i=0
while [[ $i -lt $COUNT ]]; do
  rule="$(printf '%s' "$RULES" | jq -c ".rules[$i]" 2>/dev/null || true)"
  i=$((i+1))
  [[ -n "$rule" && "$rule" != "null" ]] || continue

  r_id="$(printf '%s'   "$rule" | jq -r '.id // "rule"' 2>/dev/null || echo rule)"
  r_tool="$(printf '%s' "$rule" | jq -r '.tool // empty' 2>/dev/null || true)"
  r_cmdre="$(printf '%s' "$rule" | jq -r '.command_regex // empty' 2>/dev/null || true)"
  r_filere="$(printf '%s' "$rule" | jq -r '.file_path_regex // empty' 2>/dev/null || true)"
  r_action="$(printf '%s' "$rule" | jq -r '.action // "deny"' 2>/dev/null || echo deny)"
  r_reason="$(printf '%s' "$rule" | jq -r '.reason // "blocked by policy"' 2>/dev/null || echo "blocked by policy")"
  r_oenv="$(printf '%s' "$rule" | jq -r '.override_env // empty' 2>/dev/null || true)"
  r_max="$(printf '%s' "$rule" | jq -r '.max_per_session // empty' 2>/dev/null || true)"
  r_exclude="$(printf '%s' "$rule" | jq -r '.exclude_regex // empty' 2>/dev/null || true)"

  [[ -n "$r_tool" ]] || continue
  printf '%s' "$TOOL_NAME" | grep -Eq "^($r_tool)\$" 2>/dev/null || continue

  matched=0
  if [[ -n "$r_cmdre" && -n "$CMD" ]]; then
    printf '%s' "$CMD" | grep -Eq "$r_cmdre" 2>/dev/null && matched=1
  fi
  if [[ $matched -eq 0 && -n "$r_filere" && -n "$FILE" ]]; then
    printf '%s' "$FILE" | grep -Eq "$r_filere" 2>/dev/null && matched=1
  fi
  # exclude_regex: suppress a matched rule when the command/file ALSO matches the
  # exclusion (lets a rule mean "Memory/ but NOT the mutable subset").
  if [[ $matched -eq 1 && -n "$r_exclude" ]]; then
    if [[ -n "$CMD" ]]  && printf '%s' "$CMD"  | grep -Eq "$r_exclude" 2>/dev/null; then matched=0; fi
    if [[ $matched -eq 1 && -n "$FILE" ]] && printf '%s' "$FILE" | grep -Eq "$r_exclude" 2>/dev/null; then matched=0; fi
  fi
  [[ $matched -eq 1 ]] || continue

  # Override escape hatch.
  if [[ -n "$r_oenv" ]]; then
    oval="$(printenv "$r_oenv" 2>/dev/null || true)"
    [[ "$oval" == "1" ]] && exit 0
  fi

  ohint=""
  [[ -n "$r_oenv" ]] && ohint=" (override: ${r_oenv}=1)"

  # Stateful rate limit (session_state): after max_per_session matches this
  # session, hard-deny regardless of action. Fail-open if state is unavailable.
  if [[ -n "$r_max" && "$r_max" =~ ^[0-9]+$ ]] && command -v state_incr >/dev/null 2>&1; then
    n="$(state_incr "$SID" "count:${r_id}" 2>/dev/null || echo 0)"
    if [[ "$n" =~ ^[0-9]+$ && "$n" -gt "$r_max" ]]; then
      pretooluse_policy_deny "$r_id" "per-session limit reached ($n > ${r_max}). ${r_reason}" "$ohint"
      exit $?
    fi
  fi

  case "$r_action" in
    warn|log)
      # Awareness-only: violation-checker.sh logs this match post-hoc; the
      # PreToolUse guard neither blocks nor prompts. Keep scanning later rules.
      continue
      ;;
    ask)
      pretooluse_policy_ask "$r_id" "$r_reason" "$ohint"
      exit $?
      ;;
    *)
      # deny (and the deny-by-default fail-safe for an unset action).
      pretooluse_policy_deny "$r_id" "$r_reason" "$ohint"
      exit $?
      ;;
  esac
done

exit 0
