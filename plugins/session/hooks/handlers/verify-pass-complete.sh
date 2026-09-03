#!/usr/bin/env bash
# Re-prove a declared old-value replacement at the end-of-turn seam. Fail open.
set -uo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "$0")" >/dev/null 2>&1 && pwd)" || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/common.sh" 2>/dev/null || { echo '{}'; exit 0; }
source "$SCRIPT_DIR/harness-response.sh" 2>/dev/null || { echo '{}'; exit 0; }
command -v jq >/dev/null 2>&1 || { echo '{}'; exit 0; }

INPUT="$(cat 2>/dev/null || true)"
if printf '%s' "$INPUT" | jq -e '.stop_hook_active == true' >/dev/null 2>&1; then
  echo '{}'
  exit 0
fi

PROJECT_DIR="$(resolve_hook_project_dir "$INPUT" 2>/dev/null || true)"
[[ -n "$PROJECT_DIR" && -f "$PROJECT_DIR/.asha/config.json" ]] || { echo '{}'; exit 0; }
MARKER="$PROJECT_DIR/Work/markers/pass-declaration.json"
[[ -f "$MARKER" && ! -L "$MARKER" ]] || { echo '{}'; exit 0; }

OLD_VALUE="$(jq -r \
  'select(.schema_version == 1) | (.old // .old_value) | select(type == "string" and length > 0)' \
  "$MARKER" 2>/dev/null || true)"
[[ -n "$OLD_VALUE" ]] || { echo '{}'; exit 0; }
NEW_VALUE="$(jq -r '.new // .new_value // empty | select(type == "string")' \
  "$MARKER" 2>/dev/null || true)"

HITS_FILE="$(mktemp "${TMPDIR:-/tmp}/asha-verify-pass.XXXXXX" 2>/dev/null || true)"
[[ -n "$HITS_FILE" ]] || { echo '{}'; exit 0; }
trap 'rm -f "$HITS_FILE"' EXIT
grep -rIlF --exclude-dir=.git --exclude-dir=.jj --exclude-dir=Work \
  -- "$OLD_VALUE" "$PROJECT_DIR" >"$HITS_FILE" 2>/dev/null
GREP_RC=$?
if [[ $GREP_RC -eq 1 ]]; then
  rm -f "$MARKER" 2>/dev/null || true
  echo '{}'
  exit 0
fi
[[ $GREP_RC -eq 0 ]] || { echo '{}'; exit 0; }

FILES=""
while IFS= read -r hit; do
  [[ -n "$hit" ]] || continue
  rel="${hit#"$PROJECT_DIR"/}"
  FILES="${FILES:+$FILES$'\n'}- $rel"
done < "$HITS_FILE"
[[ -n "$FILES" ]] || { echo '{}'; exit 0; }

FRAGMENT="$(nudge_fragment verify-pass-complete 2>/dev/null || true)"
CONTEXT="${FRAGMENT:+$FRAGMENT$'\n\n'}Old value: $OLD_VALUE"
[[ -z "$NEW_VALUE" ]] || CONTEXT="$CONTEXT
Declared replacement: $NEW_VALUE"
CONTEXT="$CONTEXT

Files still containing the old value:
$FILES"
verify_pass_nudge "$CONTEXT"
exit 0
