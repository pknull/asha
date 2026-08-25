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
# Rule schema: {id, tool, command_regex|file_path_regex, exclude_regex?, action: deny|ask|warn, reason, override_env?, require_env?}
#   require_env: the rule is evaluated only when that variable is non-empty in the session.
# action=warn => awareness-only; this guard does not block.

# fail-open by design: no set -e — a handler crash must never block the session
set -uo pipefail

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
# Bash removes `\<newline>` before executing, but grep -E matches per line —
# a continuation-split command (`git -C /ws \` NL `push --force`) would evade
# every command_regex. Normalize to what bash actually runs (2026-08-07,
# PR #30 pass-2 finding). Plain newlines are deliberately left alone: they
# separate commands, and per-line matching is correct for them.
CMD="${CMD//$'\\\n'/}"
# File tools do not agree upon a single field. Include native path aliases and
# apply_patch's patch header so a relative published-Memory edit cannot bypass
# the same rule merely by choosing another editor surface.
FILE="$(printf '%s' "$INPUT" | jq -r '
  [.tool_input.file_path?, .tool_input.path?, .tool_input.patch?]
  | map(select(type == "string" and length > 0)) | join("\n")' 2>/dev/null || true)"

# Locate rule sources relative to this script (hooks run from the source tree).
REPO_RULES="$SELF_DIR/../policies/rules.json"
USER_RULES="${ASHA_HOME:-$HOME/.asha}/policies.json"

# Merge repo + user rules (user overrides by id). Missing user file is normal.
RULES=""
if [[ -f "$REPO_RULES" && -f "$USER_RULES" ]]; then
  # User rules REPLACE base rules with the same id IN PLACE (array position is
  # evaluation priority — first match wins — so an override must not migrate to
  # the end of the list and silently change which rule's action/reason fires).
  # User rules with new ids append after the base set.
  RULES="$(jq -s '
    (.[0].rules // []) as $base | (.[1].rules // []) as $user
    | ($base | map(.id)) as $bids
    | { rules:
        (($base | map( . as $b | (first($user[] | select(.id == $b.id)) // $b) ))
         + ($user | map(select( (.id) as $i | ($bids | index($i)) | not )))) }
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
  r_reqenv="$(printf '%s' "$rule" | jq -r '.require_env // empty' 2>/dev/null || true)"
  r_exclude="$(printf '%s' "$rule" | jq -r '.exclude_regex // empty' 2>/dev/null || true)"

  [[ -n "$r_tool" ]] || continue
  # require_env: the rule applies only inside sessions that carry this variable
  # (non-empty), e.g. a coordinator session; elsewhere it is inert.
  if [[ -n "$r_reqenv" ]]; then
    reqval="$(printenv "$r_reqenv" 2>/dev/null || true)"
    [[ -n "$reqval" ]] || continue
  fi
  printf '%s' "$TOOL_NAME" | grep -Eq -- "^($r_tool)\$" 2>/dev/null || continue

  matched=0
  if [[ -n "$r_cmdre" && -n "$CMD" ]]; then
    printf '%s' "$CMD" | grep -Eq -- "$r_cmdre" 2>/dev/null && matched=1
  fi
  if [[ $matched -eq 0 && -n "$r_filere" && -n "$FILE" ]]; then
    printf '%s' "$FILE" | grep -Eq -- "$r_filere" 2>/dev/null && matched=1
  fi
  # exclude_regex: suppress a matched rule when the command/file ALSO matches the
  # exclusion (lets a rule mean "Memory/ but NOT the mutable subset").
  if [[ $matched -eq 1 && -n "$r_exclude" ]]; then
    if [[ -n "$CMD" ]]  && printf '%s' "$CMD"  | grep -Eq -- "$r_exclude" 2>/dev/null; then matched=0; fi
    if [[ $matched -eq 1 && -n "$FILE" ]] && printf '%s' "$FILE" | grep -Eq -- "$r_exclude" 2>/dev/null; then matched=0; fi
  fi
  [[ $matched -eq 1 ]] || continue

  # Override escape hatch.
  if [[ -n "$r_oenv" ]]; then
    oval="$(printenv "$r_oenv" 2>/dev/null || true)"
    [[ "$oval" == "1" ]] && continue
  fi

  ohint=""
  [[ -n "$r_oenv" ]] && ohint=" (override: ${r_oenv}=1)"

  case "$r_action" in
    warn|log)
      # Awareness-only, but not silent. Hook stderr is the portable advisory
      # channel; exit 0 preserves the requested operation.
      printf 'WARNING by Asha policy [%s]: %s%s\n' "$r_id" "$r_reason" "$ohint" >&2
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
